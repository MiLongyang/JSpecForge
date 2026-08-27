"""处理器抽象基类与共享数据类。

设计依据:

- ``docs/design/系统架构与调度设计.md`` §三 / §5.2:风格 B
  (独立 processor + 队列),每个 processor 实现
  ``process(task) → (result, next_stage)``
- ``docs/plan/P6-处理器.md`` plan §1.2: BaseProcessor 接口契约
- ``docs/plan/P7-编排器.md`` 信号钩子归属 + InfraError 处理
- P7 启动会话用户决议(2026-05-01):
    * 任务级 InfraError 重试 ``retry.infra_error_max_per_task`` 次仍失败
      → 任务**保持 status=pending**(不标 failed)+ 写 last_error_detail
    * 全局滑动窗口由 :class:`InfraErrorTracker` 兜底
    * Worker 循环每次取任务前检查 ``shutdown_event``

**P6 ↔ P7 契约**(plan §4.3):

- P6 子类只实现 ``process(task)`` 单步函数
- P7 编排器调 :meth:`BaseProcessor.run` 启动长驻 worker 循环

**关键不变量**:

- ``process()`` 内部完成 DB 写入 + 文件写入 + Task 字段修改后返回
- ``process()`` **不**阻塞等待其他 processor,**不**调度其他任务
- 同一个 Task 实例在 ``process()`` 期间只被一个线程持有(P7 保证)
- ``process()`` 抛 ``*InfraError`` 时**不消耗任何业务计数器**(design §四 F)
"""

from __future__ import annotations

import queue
import time
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Optional, Type

from jspecforge.config.settings import AppConfig
from jspecforge.core.task_entity import Task


# ---------------------------------------------------------------------------
# next_stage 枚举常量
# ---------------------------------------------------------------------------

#: TaskA 处理器的输出队列(黑名单命中 → rac;否则 → oj)
NEXT_TASK_A: str = "task_a"
NEXT_OJ: str = "oj"
NEXT_RAC: str = "rac"
NEXT_INPUT_GEN: str = "input_gen"
NEXT_PBT: str = "pbt"
NEXT_MT: str = "mt"
NEXT_FINAL: str = "final"

#: OJ 直通终态(OJProcessor 自己 mark_success,不进 final_queue)
NEXT_DONE_OJ_PASS: str = "done_oj_pass"

#: FinalProcessor 完成后的终态
NEXT_DONE: str = "done"

#: 全部合法 next_stage 取值集合(P7 路由器校验用)
NEXT_STAGES: FrozenSet[str] = frozenset(
    {
        NEXT_TASK_A,
        NEXT_OJ,
        NEXT_RAC,
        NEXT_INPUT_GEN,
        NEXT_PBT,
        NEXT_MT,
        NEXT_FINAL,
        NEXT_DONE_OJ_PASS,
        NEXT_DONE,
    }
)


# ---------------------------------------------------------------------------
# 处理结果
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProcessResult:
    """单步处理结果。

    Attributes:
        next_stage: 下一队列名称,取值见 :data:`NEXT_STAGES`。
        task: 修改后的 Task 实体(计数器、状态、当前 JML 等)。同一引用
            返回,方便 P7 直接复用。
        diagnostic: 给 monitor / 日志看的简短描述(如 ``"OJ exit=0 → SUCCESS"``)。
            可为 ``None``。
    """

    next_stage: str
    task: Task
    diagnostic: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.next_stage not in NEXT_STAGES:
            raise ValueError(
                f"非法 next_stage: {self.next_stage!r},"
                f"应为 {sorted(NEXT_STAGES)} 之一"
            )


# ---------------------------------------------------------------------------
# 处理器基类
# ---------------------------------------------------------------------------


class BaseProcessor:
    """所有处理器的抽象基类。

    子类需要:

    1. 在 ``__init__`` 中接受所需的依赖(``conn`` / ``llm_client`` /
       ``runner`` 等),通过依赖注入便于测试
    2. 实现 :meth:`process`: 单步处理一个 Task

    P7 编排器在长驻 worker 循环中重复调用 ``process(task)``——见 :meth:`run`。
    """

    def __init__(self, config: AppConfig) -> None:
        """构造。

        子类应当调用 ``super().__init__(config)`` 并自行接受额外依赖。

        Args:
            config: ``AppConfig`` 实例。
        """
        self._config = config

    # ------------------------------------------------------------------
    # 子类必须实现
    # ------------------------------------------------------------------

    def process(self, task: Task) -> ProcessResult:
        """处理单个 Task。

        Args:
            task: 待处理的 Task 实例。

        Returns:
            :class:`ProcessResult`,含 ``next_stage`` 与(可能修改过的)
            ``task``。

        Raises:
            NotImplementedError: 子类未实现。
        """
        raise NotImplementedError(
            f"{type(self).__name__}.process() 未实现"
        )

    # ------------------------------------------------------------------
    # 长驻 worker 循环(P7 编排器使用)
    # ------------------------------------------------------------------

    def run(
        self,
        *,
        input_stage: str,
        queue_manager: "Any",
        router: "Any",
        shutdown_event: "Any",
        infra_error_tracker: "Optional[Any]" = None,
        infra_error_class: Optional[Type[Exception]] = None,
        worker_name: Optional[str] = None,
        queue_get_timeout: float = 1.0,
    ) -> None:
        """长驻 worker 循环。直到 ``shutdown_event`` 被 set 才退出。

        循环逻辑:

        1. 检查 ``shutdown_event`` 是否 set,是 → 退出
        2. ``queue.get(input_stage, timeout=1)`` 取任务
        3. 调 ``TaskDAO.mark_in_progress`` (design §九,首次取出时 status→in_progress)
        4. 调 ``self.process(task)`` 单步处理
        5. 抓 ``infra_error_class`` 异常时:
            a. 累计 ``task.extra["_infra_retries"]``
            b. ``< retry.infra_error_max_per_task`` → wait + 重新入原队列
            c. ``>= max_per_task`` → 保持 status=in_progress、写 last_error_detail、
               不入队、调用 ``infra_error_tracker.record()``(可能触发停机)
        6. 抓其他异常时: log.error 然后 continue(防御兜底,不应发生——
           processor 应保证 ``process()`` 内部不会泄漏未知异常)
        7. 正常返回 ``ProcessResult`` → 调 ``router.route()``

        Args:
            input_stage: 本 worker 消费的队列名(如 ``"task_a"``)。
            queue_manager: :class:`QueueManager` 实例。
            router: :class:`Router` 实例。
            shutdown_event: 全局 :class:`threading.Event`。
            infra_error_tracker: :class:`InfraErrorTracker` 实例。``None``
                表示不计入全局滑动窗口(测试场景)。
            infra_error_class: 该 processor 抛的 InfraError 子类
                (``OJInfraError`` / ``RACInfraError`` / 等)。``None`` 表示
                不区分,任何 ``Exception`` 都按业务异常处理。
            worker_name: worker 标识(给日志用,如 ``"oj-worker-0"``)。
            queue_get_timeout: ``queue.get`` 的阻塞超时(秒)。
        """
        # 延迟 import 避免循环依赖(base 不应依赖 core/db)
        from jspecforge.utils.log_utils import get_logger

        logger = get_logger(f"processor.run.{input_stage}")
        worker_label = worker_name or f"{input_stage}-worker"
        max_per_task = self._config.retry.infra_error_max_per_task
        wait_seconds = self._config.retry.infra_error_wait_seconds

        logger.info("[%s] worker started (input_stage=%s)", worker_label, input_stage)

        while not shutdown_event.is_set():
            try:
                task = queue_manager.get(input_stage, timeout=queue_get_timeout)
            except queue.Empty:
                continue

            # ── 首次取出: mark_in_progress（design §九 写入时序）────────
            self._mark_in_progress_safe(task, logger)

            # ── 处理一个任务 ────────────────────────────────────
            try:
                result = self.process(task)
            except Exception as exc:  # noqa: BLE001
                # 区分 InfraError 与其他异常
                is_infra = (
                    infra_error_class is not None
                    and isinstance(exc, infra_error_class)
                )
                if is_infra:
                    self._handle_infra_error(
                        task=task,
                        exc=exc,
                        input_stage=input_stage,
                        queue_manager=queue_manager,
                        infra_error_tracker=infra_error_tracker,
                        max_per_task=max_per_task,
                        wait_seconds=wait_seconds,
                        worker_label=worker_label,
                        logger=logger,
                    )
                else:
                    # 非 InfraError 的未捕获异常: 这是 processor 实现 bug
                    # 记录后继续(避免 worker 整个挂掉)
                    logger.exception(
                        "[%s] Task[%d] processor 抛未预期异常: %s",
                        worker_label,
                        task.id,
                        exc,
                    )
                queue_manager.task_done(input_stage)
                continue

            # ── 路由 ────────────────────────────────────────────
            try:
                # 成功路径: 重置 InfraError 重试计数
                task.extra.pop("_infra_retries", None)
                conn = getattr(self, "_conn", None)
                if conn is not None and hasattr(router, "route_with_conn"):
                    router.route_with_conn(result, conn)
                else:
                    router.route(result)
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "[%s] Task[%d] router.route 抛异常: %s",
                    worker_label,
                    task.id,
                    exc,
                )
                shutdown_event.set()
            finally:
                queue_manager.task_done(input_stage)

        logger.info("[%s] worker stopped (shutdown_event set)", worker_label)

    # ------------------------------------------------------------------
    # mark_in_progress（design §九 写入时序）
    # ------------------------------------------------------------------

    def _mark_in_progress_safe(self, task: Task, logger: "Any") -> None:
        """worker 首次取出任务时调 :meth:`TaskDAO.mark_in_progress`。

        幂等：``started_at = COALESCE(started_at, CURRENT_TIMESTAMP)``
        只首次设 started_at，后续调用不覆盖。

        任何异常只 log 不抛——不应阻断主流程。
        """
        from jspecforge.db.task_dao import TaskDAO

        conn = getattr(self, "_conn", None)
        if conn is None:
            logger.warning("Task[%d] 无 self._conn,无法 mark_in_progress", task.id)
            return
        try:
            TaskDAO.mark_in_progress(conn, task.id)
            conn.commit()
            task.status = "in_progress"
        except Exception as exc:  # noqa: BLE001
            logger.error("Task[%d] mark_in_progress 失败: %s", task.id, exc)
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # InfraError 处理(私有)
    # ------------------------------------------------------------------

    def _handle_infra_error(
        self,
        *,
        task: Task,
        exc: Exception,
        input_stage: str,
        queue_manager: "Any",
        infra_error_tracker: "Optional[Any]",
        max_per_task: int,
        wait_seconds: int,
        worker_label: str,
        logger: "Any",
    ) -> None:
        """InfraError 处理流程(P7 设计 §C):

        1. 累计 task.extra["_infra_retries"]
        2. 若未达 max_per_task: wait → 重新入原队列(等于让本 worker 自己
           在下一轮循环里取出来再试)
        3. 若已达 max_per_task:
            a. 不调 mark_failed(任务保持 status=in_progress,下次启动按
               current_step 恢复)
            b. last_error_detail 写一条 "infra: ..." 给用户排查
            c. 调 infra_error_tracker.record() 触发全局滑动窗口计数
            d. 重置 task.extra["_infra_retries"]
        """
        retries = int(task.extra.get("_infra_retries", 0)) + 1
        task.extra["_infra_retries"] = retries

        err_summary = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "[%s] Task[%d] InfraError #%d/%d at %s: %s",
            worker_label,
            task.id,
            retries,
            max_per_task,
            input_stage,
            err_summary,
        )

        if retries < max_per_task:
            # 等待后重新入原队列
            if wait_seconds > 0:
                time.sleep(wait_seconds)
            queue_manager.put(input_stage, task)
            return

        # 达上限: 保持 in_progress + 写 last_error_detail + 全局计数
        task.extra.pop("_infra_retries", None)
        self._write_infra_in_progress_via_helper(
            task=task,
            err_summary=err_summary,
            logger=logger,
        )

        if infra_error_tracker is not None:
            try:
                infra_error_tracker.record(
                    stage=input_stage,
                    task_id=task.id,
                    error_msg=err_summary,
                )
            except Exception as track_exc:  # noqa: BLE001
                logger.error(
                    "[%s] InfraErrorTracker.record 失败: %s",
                    worker_label,
                    track_exc,
                )

    def _write_infra_in_progress_via_helper(
        self,
        *,
        task: Task,
        err_summary: str,
        logger: "Any",
    ) -> None:
        """把 task.last_error_detail 写回 DB,**保持** status=in_progress。

        只写 ``last_error_detail`` 字段(经 ``UPDATE tasks SET ...``),不动
        status / failure_stage——任务保持 in_progress,下次启动按
        current_step 恢复到对应队列继续处理。

        子类可以覆盖此方法以使用不同的 DAO 或自带连接。默认实现期望
        子类有 ``self._conn``(几乎所有 P6 processor 都有),否则只 log。
        """
        conn = getattr(self, "_conn", None)
        if conn is None:
            logger.warning(
                "Task[%d] 无 self._conn,无法写 last_error_detail (infra: %s)",
                task.id,
                err_summary[:200],
            )
            return

        # 用一条简单 SQL 更新 last_error_detail。task_dao 没暴露此操作,
        # 我们直接 raw SQL(明确受影响行数=1)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE tasks SET last_error_detail = %s, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                    (f"infra: {err_summary[:1000]}", task.id),
                )
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Task[%d] 写 last_error_detail 失败: %s",
                task.id,
                exc,
            )
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # 只读访问
    # ------------------------------------------------------------------

    @property
    def config(self) -> AppConfig:
        """暴露 config 给子类(避免子类自己存一份)。"""
        return self._config

"""路由器（P7.0）。

设计依据:

- ``docs/design/系统架构与调度设计.md`` §2.2 Router 抽象
- ``docs/design/系统架构与调度设计.md`` §5.2 队列拓扑
- ``docs/design/数据库表字段设计.md`` §九 写入时序(``current_step`` 在每阶段
  开始前 UPDATE,保证中断恢复语义)
- ``docs/plan/P6-处理器.md`` §6.5 next_stage 路由表

**职责**:

接收 Worker 抛出的 :class:`ProcessResult`,按 ``next_stage`` 字段:

1. ``done_oj_pass``: OJ 直通终态。OJProcessor 已自行 ``mark_success``,
   Router **不**投递、不更新 step,仅记一条 INFO 日志。
2. ``done``: FinalProcessor 完成的终态。Router **不**投递,仅记日志。
3. 其余 6 个有队列 stage(``task_a`` / ``oj`` / ``rac`` / ``input_gen`` /
   ``pbt`` / ``final``): UPDATE ``tasks.current_step`` → 投递到目标队列。

**线程安全**: Router 实例可被多个 Worker 共享。内部不持有可变状态,DB
操作通过传入的 ``conn`` 执行。
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Protocol

from jspecforge.core.queue_manager import QUEUE_STAGES, QueueManager
from jspecforge.core.task_entity import Task
from jspecforge.db.task_dao import TaskDAO
from jspecforge.processors.base import (
    NEXT_DONE,
    NEXT_DONE_OJ_PASS,
    NEXT_FINAL,
    NEXT_INPUT_GEN,
    NEXT_MT,
    NEXT_OJ,
    NEXT_PBT,
    NEXT_RAC,
    NEXT_STAGES,
    NEXT_TASK_A,
    ProcessResult,
)
from jspecforge.utils.log_utils import get_logger

_logger = get_logger("core.router")


class _TaskReleaser(Protocol):
    """释放活跃任务计数的协议（避免循环导入）。"""

    def release(self) -> None: ...


class _TaskIdReleaser(Protocol):
    """释放已入队 task ID 的协议（避免循环导入）。"""

    def release_id(self, task_id: int) -> None: ...


#: next_stage → tasks.current_step 的映射。
#: design 数据库表字段设计 §三: ``current_step`` 取值
#: ``task_a / openjml / rac / input_gen / pbt / final``。
#: 注意 OJ 队列名是 ``oj``,但 DB 字段值是 ``openjml``——见此映射的第一行。
_STAGE_TO_DB_STEP: Dict[str, str] = {
    NEXT_TASK_A: "task_a",
    NEXT_OJ: "openjml",
    NEXT_RAC: "rac",
    NEXT_INPUT_GEN: "input_gen",
    NEXT_PBT: "pbt",
    NEXT_MT: "mt",
    NEXT_FINAL: "final",
}


class Router:
    """ProcessResult → 队列 + DB current_step 路由。"""

    def __init__(
        self,
        queue_manager: QueueManager,
        conn: Any,
        *,
        task_dao: type = TaskDAO,
        task_releaser: Optional[_TaskReleaser] = None,
        task_id_releaser: Optional[_TaskIdReleaser] = None,
    ) -> None:
        """构造。

        Args:
            queue_manager: :class:`QueueManager` 实例。
            conn: PyMySQL 连接,Router 用它执行 ``update_step``。多线程共享
                时,各 worker 应使用各自的连接(本实例只持有一个引用)。
                **生产代码**: Router 不必只用一个连接——可以让 Worker 在调
                ``route()`` 时传入自己的 conn(见 :meth:`route_with_conn`)。
            task_dao: 默认 :class:`TaskDAO`,可注入 mock。
            task_releaser: 活跃任务计数释放器（可选）。
            task_id_releaser: 已入队 task ID 释放器（可选）。
        """
        self._queue_manager = queue_manager
        self._conn = conn
        self._task_dao = task_dao
        self._task_releaser = task_releaser
        self._task_id_releaser = task_id_releaser

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def route(self, result: ProcessResult) -> None:
        """根据 ``ProcessResult.next_stage`` 路由。

        约定:

        - 终态 (``done_oj_pass`` / ``done``): 仅记日志,不入队、不更新 step。
        - 6 个有队列 stage: 先 ``UPDATE current_step`` → 再 ``put`` 到目标队列。
          顺序保证中断恢复时 DB 中的 ``current_step`` 一定先于 in-queue 状态。

        Args:
            result: 从 :meth:`BaseProcessor.process` 返回的结果。

        Raises:
            ValueError: ``result.next_stage`` 不在 :data:`NEXT_STAGES` 中
                (理论上 ``ProcessResult.__post_init__`` 已挡住,这里是兜底)。
        """
        self.route_with_conn(result, self._conn)

    def route_with_conn(self, result: ProcessResult, conn: Any) -> None:
        """同 :meth:`route`,但允许调用方注入连接(给多线程 worker 用)。

        Args:
            result: ProcessResult。
            conn: 用于 ``update_step`` 的 PyMySQL 连接。

        Raises:
            ValueError: ``result.next_stage`` 不合法。
        """
        next_stage = result.next_stage
        task = result.task

        if next_stage not in NEXT_STAGES:
            raise ValueError(
                f"非法 next_stage: {next_stage!r},应为 "
                f"{sorted(NEXT_STAGES)} 之一"
            )

        # ── 终态: OJ 直通 ───────────────────────────────────────
        if next_stage == NEXT_DONE_OJ_PASS:
            _logger.info(
                "Task[%d] OJ 直通成功(path=openjml),已由 OJProcessor mark_success",
                task.id,
            )
            if self._task_releaser:
                self._task_releaser.release()
            if self._task_id_releaser:
                self._task_id_releaser.release_id(task.id)
            return

        # ── 终态: FinalProcessor 完成 ──────────────────────────
        if next_stage == NEXT_DONE:
            _logger.info(
                "Task[%d] 已完成最终裁定(status=%s)",
                task.id,
                task.status,
            )
            if self._task_releaser:
                self._task_releaser.release()
            if self._task_id_releaser:
                self._task_id_releaser.release_id(task.id)
            return

        # ── 6 个有队列 stage: 更新 current_step → 投递 ─────────
        if next_stage not in QUEUE_STAGES:
            # 理论上 NEXT_STAGES 已校验,这里防御性兜底
            raise ValueError(
                f"next_stage={next_stage!r} 既非终态也无对应队列"
            )

        db_step = _STAGE_TO_DB_STEP[next_stage]
        self._update_step_with_retry(conn, task.id, db_step)

        task.current_step = db_step
        self._queue_manager.put(next_stage, task)
        _logger.debug(
            "Task[%d] routed to %s queue (diagnostic=%r)",
            task.id,
            next_stage,
            result.diagnostic,
        )

    def _update_step_with_retry(
        self,
        conn: Any,
        task_id: int,
        db_step: str,
        *,
        max_attempts: int = 3,
    ) -> None:
        """Update current_step before enqueueing, retrying transient DB errors."""
        last_exc: Optional[Exception] = None
        for attempt in range(1, max_attempts + 1):
            try:
                self._task_dao.update_step(conn, task_id, db_step)
                conn.commit()
                return
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                _logger.error(
                    "Task[%d] update_step(%s) 失败 (%d/%d): %s",
                    task_id,
                    db_step,
                    attempt,
                    max_attempts,
                    exc,
                )
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
                if attempt < max_attempts:
                    try:
                        conn.ping(reconnect=True)
                    except Exception:  # noqa: BLE001
                        pass

        raise RuntimeError(
            f"update_step({db_step}) failed after {max_attempts} attempts"
        ) from last_exc

    # ------------------------------------------------------------------
    # 启动期辅助: 从 DB current_step 决定初始入哪个队列
    # ------------------------------------------------------------------

    def initial_stage_for(self, task: Task) -> Optional[str]:
        """根据 task 的 status + current_step 返回应入哪个队列。

        Orchestrator 启动时用此函数决定 in_progress / pending 任务的归属:

        - ``status='pending'``: 进 ``task_a`` 队列(全新任务)
        - ``status='in_progress'``: 按 ``current_step`` 还原。
          ``current_step IN ('task_a',None)`` → ``task_a``;
          ``openjml`` → ``oj``;``rac`` / ``input_gen`` / ``pbt`` / ``final`` → 同名队列
        - ``status='success'`` / ``'failed'``: 返回 None(已结束)

        Args:
            task: 从 DB 加载的 Task 实例。

        Returns:
            目标 stage 名;``None`` 表示不入任何队列。
        """
        if task.status in ("success", "failed"):
            return None

        if task.status == "pending":
            return NEXT_TASK_A

        # in_progress: 按 current_step 决定
        step = task.current_step
        if step is None or step == "task_a":
            return NEXT_TASK_A
        if step == "openjml":
            return NEXT_OJ
        if step == "rac":
            return NEXT_RAC
        if step == "input_gen":
            return NEXT_INPUT_GEN
        if step == "pbt":
            return NEXT_PBT
        if step == "final":
            return NEXT_FINAL
        if step == "mt":
            return NEXT_MT

        # 未知 step,保险起见从头开始
        _logger.warning(
            "Task[%d] 未知 current_step=%r,fallback 入 task_a 队列",
            task.id,
            step,
        )
        return NEXT_TASK_A

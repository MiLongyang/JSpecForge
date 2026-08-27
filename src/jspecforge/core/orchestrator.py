"""编排器（P7.1）。

设计依据:

- ``docs/design/系统架构与调度设计.md`` §五 队列拓扑、§八 中断恢复
- ``docs/plan/P6-处理器.md`` §6.6 P7 待做清单
- P7 启动会话用户决议(2026-05-01):
    * 任务来源:tasks 表 SELECT status IN ('pending','in_progress')
    * pending → task_a 队列;in_progress → 按 current_step 路由到对应队列
    * 每个 worker 启动时从池借一根连接长期持有(MySQL pool ping=1 自动重连)
    * 多 worker 共享 llm_client / blacklist_runtime / qm / router / tracker

**3 个核心方法**:

1. :meth:`Orchestrator.load_existing_tasks`:
   从 DB 加载 status IN ('pending','in_progress') → 调
   :meth:`Router.initial_stage_for` → 入对应队列
2. :meth:`Orchestrator.start_workers`:
   按 ``config.workers`` 启动 6 类 N 个 worker 线程
3. :meth:`Orchestrator.join`:
   等所有 worker 线程退出(``shutdown_event`` set 后 worker 自然退出循环)

**ProcessorFactory**: 每个 worker 自己构造 processor 实例(注入自己的 conn),
而 llm_client / blacklist_runtime / dao class 等无状态组件由 Orchestrator
共享给所有 worker。
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List, Optional

from jspecforge.config.settings import AppConfig
from jspecforge.core.queue_manager import QueueManager
from jspecforge.core.router import Router
from jspecforge.core.shutdown import InfraErrorTracker
from jspecforge.core.task_entity import Task
from jspecforge.db.task_dao import TaskDAO
from jspecforge.llm.client import BaseLLMClient
from jspecforge.processors.base import (
    NEXT_FINAL,
    NEXT_INPUT_GEN,
    NEXT_MT,
    NEXT_OJ,
    NEXT_PBT,
    NEXT_RAC,
    NEXT_TASK_A,
    BaseProcessor,
)
from jspecforge.processors.final_processor import FinalProcessor
from jspecforge.processors.input_gen_processor import (
    InputGenInfraError,
    InputGenProcessor,
)
from jspecforge.processors.mt_processor import MTInfraError, MTProcessor
from jspecforge.processors.openjml_processor import OJInfraError, OJProcessor
from jspecforge.processors.pbt_processor import PBTInfraError, PBTProcessor
from jspecforge.processors.rac_processor import RACInfraError, RACProcessor
from jspecforge.processors.task_a_processor import TaskAProcessor
from jspecforge.tools.blacklist_runtime import BlacklistRuntime
from jspecforge.utils.log_utils import get_logger

_logger = get_logger("core.orchestrator")


# ---------------------------------------------------------------------------
# Worker 配置: stage 名 → (processor 类, InfraError 类, workers 字段名)
# ---------------------------------------------------------------------------


_STAGE_TO_WORKERS_FIELD: Dict[str, str] = {
    NEXT_TASK_A: "task_a",
    NEXT_OJ: "openjml_esc",
    NEXT_RAC: "rac",
    NEXT_INPUT_GEN: "input_gen",
    NEXT_PBT: "pbt",
    NEXT_MT: "mt",
    NEXT_FINAL: "final",
}


_STAGE_TO_INFRA_ERROR_CLASS: Dict[str, Optional[type]] = {
    NEXT_TASK_A: None,  # TaskAProcessor 不抛 InfraError
    NEXT_OJ: OJInfraError,
    NEXT_RAC: RACInfraError,
    NEXT_INPUT_GEN: InputGenInfraError,
    NEXT_PBT: PBTInfraError,
    NEXT_MT: MTInfraError,
    NEXT_FINAL: None,  # FinalProcessor 也不抛
}


# ---------------------------------------------------------------------------
# 全局活跃任务计数器
# ---------------------------------------------------------------------------


class ActiveTaskTracker:
    """线程安全的活跃任务计数器，配合 max_active_tasks 限流。"""

    def __init__(self, max_active: int = 0) -> None:
        self._max = max_active  # 0 = 不限制
        self._count = 0
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)

    @property
    def count(self) -> int:
        with self._lock:
            return self._count

    @property
    def max_active(self) -> int:
        return self._max

    def has_capacity(self) -> bool:
        if self._max <= 0:
            return True
        with self._lock:
            return self._count < self._max

    def acquire(self, n: int = 1) -> int:
        """增加计数，返回实际增加的数量（受 max 限制）。"""
        if self._max <= 0:
            with self._lock:
                self._count += n
            return n
        with self._lock:
            available = self._max - self._count
            actual = min(n, available)
            self._count += actual
            return actual

    def release(self) -> None:
        """减少计数并通知等待的 feeder 线程。"""
        with self._condition:
            self._count = max(0, self._count - 1)
            self._condition.notify()

    def wait_for_capacity(self, timeout: float = 5.0) -> bool:
        """阻塞等待直到有空位或超时。"""
        if self._max <= 0:
            return True
        with self._condition:
            return self._condition.wait_for(
                lambda: self._count < self._max or False,
                timeout=timeout,
            )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class Orchestrator:
    """主编排器:加载 + 启动 worker 线程池 + join。

    用法(典型 main.py 流程)::

        orch = Orchestrator(
            config=cfg,
            queue_manager=qm,
            router=router,
            shutdown_event=shutdown_event,
            infra_error_tracker=tracker,
            llm_client=llm_client,
            blacklist_runtime=bl_runtime,
            connection_factory=lambda: get_connection(cfg.database),
        )
        loaded = orch.load_existing_tasks()
        threads = orch.start_workers()
        orch.join()
    """

    def __init__(
        self,
        config: AppConfig,
        *,
        queue_manager: QueueManager,
        router: Router,
        shutdown_event: threading.Event,
        infra_error_tracker: InfraErrorTracker,
        llm_client: BaseLLMClient,
        blacklist_runtime: BlacklistRuntime,
        connection_factory: Callable[[], Any],
        task_dao: type = TaskDAO,
        processor_factory_override: Optional[Callable[..., BaseProcessor]] = None,
    ) -> None:
        """构造。

        Args:
            config: ``AppConfig``。
            queue_manager: :class:`QueueManager`。
            router: :class:`Router`。
            shutdown_event: 全局停机信号。
            infra_error_tracker: :class:`InfraErrorTracker`。
            llm_client: :class:`BaseLLMClient`(线程安全,可被多个 worker 共享)。
            blacklist_runtime: :class:`BlacklistRuntime` 单例。
            connection_factory: 无参 callable,每次调用返回一根 DB 连接
                (典型实现是 ``lambda: get_connection(config.database)``)。
                每个 worker 启动时调一次借一根连接长期持有。
            task_dao: 默认 :class:`TaskDAO`,可注入 mock。
            processor_factory_override: 测试用——覆盖默认的 processor 构造逻辑。
                签名: ``(stage, conn) -> BaseProcessor``。生产代码留 None。
        """
        self._config = config
        self._qm = queue_manager
        self._router = router
        self._shutdown_event = shutdown_event
        self._infra_tracker = infra_error_tracker
        self._llm_client = llm_client
        self._bl_runtime = blacklist_runtime
        self._connection_factory = connection_factory
        self._task_dao = task_dao
        self._processor_factory_override = processor_factory_override

        self._worker_threads: List[threading.Thread] = []
        self._worker_conns: List[Any] = []  # 每个 worker 的连接,join 后归还

        max_active = config.task_loading.max_active_tasks
        self._task_tracker = ActiveTaskTracker(max_active)
        self._feeder_thread: Optional[threading.Thread] = None

        # 防止同一 task 被重复入队（feeder 与 load_existing_tasks 竞争）
        self._enqueued_ids_lock = threading.Lock()
        self._enqueued_ids: set[int] = set()

    @property
    def task_tracker(self) -> ActiveTaskTracker:
        return self._task_tracker

    def release_id(self, task_id: int) -> None:
        """从已入队集合中移除 task_id（终态时由 Router 调用）。"""
        with self._enqueued_ids_lock:
            self._enqueued_ids.discard(task_id)

    # ------------------------------------------------------------------
    # 加载任务(中断恢复 + 全新)
    # ------------------------------------------------------------------

    def load_existing_tasks(self, conn: Optional[Any] = None) -> Dict[str, int]:
        """从 DB 加载 pending + in_progress 任务,按 current_step 入队。

        中断恢复 (status='in_progress') 时,会从 DB 重建 Task 实例的临时字段:
        ``current_jml_java`` / ``current_input_gen_code`` / ``current_message_id``,
        否则下游 processor 拿到空字段会抛 InfraError 死循环。

        Args:
            conn: 可选连接。``None`` 时从 connection_factory 借一根用完归还。

        Returns:
            ``{stage_name: count}`` 字典,记录每队列入队数(给监控用)。
        """
        owned_conn = conn is None
        if owned_conn:
            conn = self._connection_factory()

        try:
            assert conn is not None
            pending_rows = self._task_dao.list_by_status(conn, "pending")
            in_progress_rows = self._task_dao.list_in_progress(conn)
            # 中断恢复任务: hydrate 临时字段
            recovered_tasks = []
            for row in in_progress_rows:
                task = Task.from_db_row(row)
                self._hydrate_resumed_task(conn, task)
                recovered_tasks.append(task)
            pending_tasks = [Task.from_db_row(r) for r in pending_rows]
        finally:
            if owned_conn and conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass

        counts: Dict[str, int] = {}
        # in_progress 任务必须全部恢复（中断恢复语义）
        for task in recovered_tasks:
            stage = self._router.initial_stage_for(task)
            if stage is None:
                continue
            with self._enqueued_ids_lock:
                self._enqueued_ids.add(task.id)
            self._qm.put(stage, task)
            self._task_tracker.acquire(1)
            counts[stage] = counts.get(stage, 0) + 1

        # pending 任务受 max_active_tasks 限制
        for task in pending_tasks:
            stage = self._router.initial_stage_for(task)
            if stage is None:
                continue
            if not self._task_tracker.has_capacity():
                break
            with self._enqueued_ids_lock:
                self._enqueued_ids.add(task.id)
            self._task_tracker.acquire(1)
            self._qm.put(stage, task)
            counts[stage] = counts.get(stage, 0) + 1

        _logger.info(
            "Orchestrator.load_existing_tasks: pending=%d, in_progress=%d, "
            "enqueued by stage=%s",
            len(pending_rows),
            len(in_progress_rows),
            counts,
        )
        return counts

    def _hydrate_resumed_task(self, conn: Any, task: Task) -> None:
        """中断恢复: 从 DB 重建 Task 临时字段。

        根据 ``current_step``:

        - ``openjml`` / ``rac`` / ``input_gen`` / ``pbt`` / ``final``:
          需要 ``current_jml_java`` (OJ/RAC 直接用,PBT/final 也需要)。
          从最新 task_a assistant 提取 JML 后 merge 出完整 java
        - ``pbt`` 额外需要 ``current_input_gen_code``: 从最新 input_gen
          assistant (a_round=task.a_round-1) 提取方法体
        - ``current_message_id``: 设为最新 task_a assistant 的 id
          (供 OJ/PBT 验证完成后回填 is_oj_pass / is_pbt_pass)

        任何步骤失败只 log 不抛——下游 processor 仍会抛 InfraError,
        InfraError 达上限后任务保持 in_progress (不会被错误标记 failed)。

        **PBT 回退 RAC** (Phase 4 hydrate fix, 2026-05-14):
            ``rac_class_name`` / ``rac_output_dir`` 是内存态字段,
            STOP-resume 后无法从 DB 还原, PBTProcessor 会抛 InfraError 死循环。
            修法: ``step == 'pbt'`` 时把 DB 与内存的 current_step 都回退到
            ``rac``, 让 RACProcessor 重新编译产出 rac_class_name /
            rac_output_dir, 再自然走 input_gen passthrough → PBT。
            额外开销 RAC 编译 ~1-2s, 可接受。
        """
        from jspecforge.db.message_dao import MessageDAO
        from jspecforge.utils.jml_utils import (
            extract_jml_block,
            is_source_method_fidelity_preserved,
            merge_jml_into_code,
        )

        step = task.current_step
        if step is None or step == "task_a":
            # task_a 阶段不需要 hydrate (TaskAProcessor 自己有 _resume_from_pending_assistant)
            return

        # ── PBT 阶段回退 RAC (Phase 4 hydrate fix, 2026-05-14) ──
        # rac_class_name / rac_output_dir 是内存态字段, STOP-resume 后无法
        # 从 DB 还原, PBTProcessor 第 3/4 个 InfraError 守卫必抛, 任务卡死。
        # 在所有 jml 解析步骤之前回退到 rac, 即便 jml 解析失败也确保该 task
        # 流入 RAC 队列而非 PBT 队列(RACProcessor 会重新编译产出那两个字段)。
        if step == "pbt":
            try:
                self._task_dao.update_step(conn, task.id, "rac")
                conn.commit()
                task.current_step = "rac"
                step = "rac"
                _logger.info(
                    "Task[%d] hydrate: PBT 回退 → RAC (重建 rac_class_name/rac_output_dir)",
                    task.id,
                )
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "Task[%d] hydrate: PBT→RAC 回退 update_step 失败: %s",
                    task.id, exc,
                )
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass

        # ── MT 阶段回退 task_a (Phase 5, 2026-05-16) ──
        # last_mt_path / current_jml_java 是内存态字段, 无法从 DB 可靠恢复。
        # 回退到 task_a 代价低(多一轮 LLM 调用), MT 计数器在 DB 中不丢失。
        if step == "mt":
            try:
                self._task_dao.update_step(conn, task.id, "task_a")
                conn.commit()
                task.current_step = "task_a"
                _logger.info(
                    "Task[%d] hydrate: MT 回退 → task_a (last_mt_path 不可恢复)",
                    task.id,
                )
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "Task[%d] hydrate: MT→task_a 回退 update_step 失败: %s",
                    task.id, exc,
                )
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
            return

        # 1. 取最新 task_a assistant -> 重建 current_jml_java + current_message_id
        try:
            last_a = MessageDAO.get_latest_assistant(
                conn, task.id, "task_a", a_round=0,
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "Task[%d] hydrate: 查 task_a 最新 assistant 失败: %s",
                task.id, exc,
            )
            return

        if last_a is None:
            _logger.warning(
                "Task[%d] hydrate: 无 task_a assistant,但 current_step=%s 需要 JML",
                task.id, step,
            )
            return

        task.current_message_id = int(last_a["id"])

        content = last_a.get("content")
        if not isinstance(content, str) or not content:
            _logger.warning(
                "Task[%d] hydrate: task_a assistant content 缺失或非字符串",
                task.id,
            )
            return
        jml_block = extract_jml_block(content)
        if jml_block:
            full_java = merge_jml_into_code(task.full_code, jml_block)
            if full_java:
                if not is_source_method_fidelity_preserved(
                    task.method_code, full_java
                ):
                    current_failures = (
                        self._count_source_fidelity_failures(conn, task.id) + 1
                    )
                    next_step = (
                        "final"
                        if current_failures >= self._config.retry.source_fidelity_max
                        else "task_a"
                    )
                    try:
                        MessageDAO.update_task_a_result(
                            conn,
                            task.current_message_id,
                            is_oj_pass=False,
                            is_pbt_pass=None,
                            action_taken="retry_source_fidelity",
                        )
                        self._task_dao.update_step(conn, task.id, next_step)
                        conn.commit()
                    except Exception as exc:  # noqa: BLE001
                        _logger.warning(
                            "Task[%d] hydrate: source fidelity rollback failed: %s",
                            task.id,
                            exc,
                        )
                        try:
                            conn.rollback()
                        except Exception:  # noqa: BLE001
                            pass
                    task.current_jml_java = None
                    task.current_step = next_step
                    if next_step == "final":
                        task.pending_termination = "failed_source_fidelity"
                    task.last_source_fidelity_error = (
                        "The previous answer modified the original Java method source. "
                        "Only JML annotations may be changed."
                    )
                    return
                task.current_jml_java = full_java
            else:
                _logger.warning(
                    "Task[%d] hydrate: merge_jml_into_code 失败 (LLM 跑题?)",
                    task.id,
                )
        else:
            _logger.warning(
                "Task[%d] hydrate: extract_jml_block 失败 (LLM content 不含 JML?)",
                task.id,
            )

        # 2. 若 current_step in ('pbt',) 或后续阶段, 重建 current_input_gen_code
        # 注意: step=='pbt' 已在前面被回退到 'rac', 此分支当前不可达;
        # 保留以兼容未来若有不回退的 pbt-recover 路径。
        if step == "pbt":
            ig_a_round = max(task.a_round - 1, 0)
            try:
                last_ig = MessageDAO.get_latest_assistant(
                    conn, task.id, "input_gen", a_round=ig_a_round,
                )
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "Task[%d] hydrate: 查 input_gen assistant 失败: %s",
                    task.id, exc,
                )
                return
            if last_ig is None:
                _logger.warning(
                    "Task[%d] hydrate: pbt 阶段但无 input_gen assistant",
                    task.id,
                )
                return
            from jspecforge.utils.jml_utils import extract_method_block
            ig_content = last_ig.get("content")
            if not isinstance(ig_content, str) or not ig_content:
                _logger.warning(
                    "Task[%d] hydrate: input_gen assistant content 缺失或非字符串",
                    task.id,
                )
                return
            method = extract_method_block(ig_content)
            if method:
                task.current_input_gen_code = method
            else:
                _logger.warning(
                    "Task[%d] hydrate: input_gen extract_method_block 失败",
                    task.id,
                )

        _logger.info(
            "Task[%d] hydrated: step=%s, jml=%s, ig_code=%s, msg_id=%s",
            task.id,
            task.current_step,
            "yes" if task.current_jml_java else "no",
            "yes" if task.current_input_gen_code else "no",
            task.current_message_id,
        )

    def _count_source_fidelity_failures(self, conn: Any, task_id: int) -> int:
        from jspecforge.db.message_dao import MessageDAO

        try:
            rows = MessageDAO.list_session(
                conn,
                task_id=task_id,
                session_type="task_a",
                a_round=0,
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "Task[%d] hydrate: count source fidelity failures failed: %s",
                task_id,
                exc,
            )
            return 0
        return sum(
            1
            for row in rows
            if row.get("role") == "assistant"
            and row.get("action_taken") == "retry_source_fidelity"
        )

    # ------------------------------------------------------------------
    # 启动 worker 线程池
    # ------------------------------------------------------------------

    def start_workers(self) -> List[threading.Thread]:
        """按 ``config.workers`` 启动 6 类 N 个 worker 线程。

        每个 worker 内部:

        1. 调 ``connection_factory`` 借一根 DB 连接(长期持有)
        2. 用本类的 :meth:`_build_processor` 构造对应 processor 实例
        3. 调 ``processor.run(...)`` 进入长驻循环

        Returns:
            启动的 worker 线程列表(供测试或调试用)。
        """
        threads: List[threading.Thread] = []

        for stage, workers_field in _STAGE_TO_WORKERS_FIELD.items():
            n_workers = getattr(self._config.workers, workers_field, 0)
            if n_workers < 1:
                _logger.warning(
                    "stage=%s workers=%d < 1,跳过启动",
                    stage,
                    n_workers,
                )
                continue
            for i in range(n_workers):
                worker_name = f"{stage}-worker-{i}"
                t = threading.Thread(
                    target=self._worker_main,
                    args=(stage, worker_name),
                    name=worker_name,
                    daemon=True,
                )
                threads.append(t)
                t.start()

        self._worker_threads = list(threads)
        _logger.info(
            "Orchestrator.start_workers: 启动 %d 个 worker 线程",
            len(threads),
        )

        # 启动 feeder 线程（仅 max_active_tasks > 0 时）
        if self._task_tracker.max_active > 0:
            self._feeder_thread = threading.Thread(
                target=self._task_feeder_main,
                name="task-feeder",
                daemon=True,
            )
            self._feeder_thread.start()
            _logger.info(
                "task-feeder started (max_active_tasks=%d)",
                self._task_tracker.max_active,
            )

        return threads

    def _worker_main(self, stage: str, worker_name: str) -> None:
        """单个 worker 线程主体。"""
        try:
            conn = self._connection_factory()
        except Exception as exc:  # noqa: BLE001
            _logger.error(
                "[%s] 借取 DB 连接失败,worker 无法启动: %s",
                worker_name,
                exc,
            )
            return

        self._worker_conns.append(conn)

        try:
            processor = self._build_processor(stage, conn)
        except Exception as exc:  # noqa: BLE001
            _logger.error(
                "[%s] 构造 processor 失败: %s",
                worker_name,
                exc,
            )
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            return

        infra_class = _STAGE_TO_INFRA_ERROR_CLASS.get(stage)

        try:
            processor.run(
                input_stage=stage,
                queue_manager=self._qm,
                router=self._router,
                shutdown_event=self._shutdown_event,
                infra_error_tracker=self._infra_tracker,
                infra_error_class=infra_class,
                worker_name=worker_name,
            )
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def _task_feeder_main(self) -> None:
        """后台线程：当活跃任务数低于上限时，从 DB 加载更多 pending 任务。"""
        conn = None
        try:
            conn = self._connection_factory()
        except Exception as exc:  # noqa: BLE001
            _logger.error("task-feeder: 借取 DB 连接失败: %s", exc)
            return

        try:
            while not self._shutdown_event.is_set():
                self._task_tracker.wait_for_capacity(timeout=5.0)
                if self._shutdown_event.is_set():
                    break
                if not self._task_tracker.has_capacity():
                    continue

                try:
                    pending_rows = self._task_dao.list_by_status(conn, "pending")
                    conn.commit()
                except Exception as exc:  # noqa: BLE001
                    _logger.warning("task-feeder: DB 查询失败: %s", exc)
                    try:
                        conn.rollback()
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        conn = self._connection_factory()
                    except Exception:  # noqa: BLE001
                        break
                    continue

                loaded = 0
                for row in pending_rows:
                    if self._shutdown_event.is_set():
                        break
                    if not self._task_tracker.has_capacity():
                        break
                    task = Task.from_db_row(row)
                    with self._enqueued_ids_lock:
                        if task.id in self._enqueued_ids:
                            continue
                        self._enqueued_ids.add(task.id)
                    stage = self._router.initial_stage_for(task)
                    if stage is None:
                        with self._enqueued_ids_lock:
                            self._enqueued_ids.discard(task.id)
                        continue
                    self._task_tracker.acquire(1)
                    self._qm.put(stage, task)
                    loaded += 1

                if loaded > 0:
                    _logger.info(
                        "task-feeder: 加载 %d 个新任务 (active=%d/%d)",
                        loaded,
                        self._task_tracker.count,
                        self._task_tracker.max_active,
                    )
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass

    def _build_processor(self, stage: str, conn: Any) -> BaseProcessor:
        """根据 stage 名构造对应 processor 实例。

        Args:
            stage: 队列名。
            conn: 该 worker 专属的 DB 连接。

        Returns:
            构造好的 :class:`BaseProcessor` 子类实例。
        """
        if self._processor_factory_override is not None:
            return self._processor_factory_override(stage, conn)

        cfg = self._config

        if stage == NEXT_TASK_A:
            return TaskAProcessor(
                cfg,
                conn=conn,
                llm_client=self._llm_client,
                blacklist_runtime=self._bl_runtime,
            )
        if stage == NEXT_OJ:
            return OJProcessor(
                cfg,
                conn=conn,
                blacklist_runtime=self._bl_runtime,
            )
        if stage == NEXT_RAC:
            return RACProcessor(cfg, conn=conn)
        if stage == NEXT_INPUT_GEN:
            return InputGenProcessor(cfg, conn=conn)
        if stage == NEXT_PBT:
            return PBTProcessor(
                cfg,
                conn=conn,
                blacklist_runtime=self._bl_runtime,
            )
        if stage == NEXT_MT:
            return MTProcessor(cfg, conn=conn)
        if stage == NEXT_FINAL:
            return FinalProcessor(cfg, conn=conn)

        raise ValueError(f"未知 stage: {stage!r}")

    # ------------------------------------------------------------------
    # 等待 worker 退出
    # ------------------------------------------------------------------

    def join(self, timeout: Optional[float] = None) -> None:
        """等所有 worker 线程退出。

        通常调用方式:主程序在 main thread 收到 shutdown_event 后(由信号
        handler / STOP 文件 / InfraError 全局上限触发)调 ``join()``。
        每个 worker 在下一轮 ``queue.get(timeout=1)`` 检查到
        ``shutdown_event`` 是 set 状态后会自然退出。

        Args:
            timeout: 单线程 join 超时;``None`` 表示无限等。
        """
        for t in self._worker_threads:
            t.join(timeout=timeout)

        n_alive = sum(1 for t in self._worker_threads if t.is_alive())
        if n_alive > 0:
            _logger.warning(
                "Orchestrator.join 完成,但仍有 %d 个 worker 未退出",
                n_alive,
            )
        else:
            _logger.info(
                "Orchestrator.join 完成,所有 %d 个 worker 已退出",
                len(self._worker_threads),
            )

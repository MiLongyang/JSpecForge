"""JSpecForge 主程序入口（P7.3）。

CLI 子命令:

- ``run``: 启动主流水线(可选 ``--load-from`` 先加载数据集)
- ``load-tasks``: 仅加载数据集,不跑流水线(等价于 scripts/load_tasks.py)

典型用法::

    # 第一次:加载 200 条 jsonl 后开跑
    python -m jspecforge.main run --load-from D:/.../batch_001.jsonl

    # 后续启动:不传 --load-from,直接读 DB pending 接着跑
    python -m jspecforge.main run

    # 仅加载,不跑
    python -m jspecforge.main load-tasks --jsonl D:/.../batch_002.jsonl

启动流程(``run`` 子命令):

1. 加载 ``config.yaml``
2. 配置 logging (``utils.log_utils.setup_logging``)
3. ``path_utils.ensure_paths()`` 创建运行时目录
4. ``blacklist_runtime.init()`` + ``register_shutdown_hooks()``(P3 钩子链)
5. ``prompts.init_default_loader()``
6. (可选) ``run_load(config, args.load_from)`` 加载数据集
7. 创建 LLM client(``OpenAICompatClient``)
8. 注册 P7 信号 handler(叠加在 P3 之上)
9. 创建 :class:`QueueManager` / :class:`Router` / :class:`InfraErrorTracker`
10. 创建 :class:`Orchestrator`,调 ``load_existing_tasks`` 入队
11. 启 :class:`StopFileWatcher`
12. (可选) 启 :class:`Dashboard` 监控
13. ``orchestrator.start_workers()`` → ``orchestrator.join()``
14. 收尾: ``client.shutdown()`` / ``watcher.stop()`` / ``monitor.stop()`` /
    ``close_all_pools()``
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
from pathlib import Path
from typing import Optional

from jspecforge.config.loader import load_config
from jspecforge.config.settings import AppConfig
from jspecforge.core.orchestrator import Orchestrator
from jspecforge.core.queue_manager import QueueManager
from jspecforge.core.router import Router
from jspecforge.core.shutdown import (
    InfraErrorTracker,
    StopFileWatcher,
    get_shutdown_event,
    in_test_mode,
    register_signal_handlers,
)
from jspecforge.db.connection import close_all_pools, get_connection
from jspecforge.llm.circuit_breaker import CircuitBreaker
from jspecforge.llm.client import OpenAICompatClient
from jspecforge.llm.vendor_router import build_router_from_csv
from jspecforge.prompts.template_loader import init_default_loader
from jspecforge.tools import blacklist_runtime as bl_module
from jspecforge.tools.path_utils import ensure_paths
from jspecforge.utils.log_utils import get_logger, setup_logging

logger = get_logger("main")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jspecforge",
        description="JSpecForge — LLM 驱动的 JML 规约生成与验证流水线",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.yaml"),
        help="config.yaml 路径(默认: ./config.yaml)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # run 子命令
    run_p = sub.add_parser(
        "run",
        help="启动主流水线",
        description="启动主流水线: 加载 pending/in_progress 任务 → 启动 worker 池 → 等停机",
    )
    run_p.add_argument(
        "--load-from",
        type=Path,
        default=None,
        help="启动前先加载指定 jsonl 数据集(可选; 不传则直接读 DB)",
    )
    run_p.add_argument(
        "--reset-pending",
        action="store_true",
        help="加载前先 DELETE 所有 status=pending 任务(配合 --load-from)",
    )
    run_p.add_argument(
        "--no-monitor",
        action="store_true",
        help="禁用控制台监控面板(覆盖 config.monitor.enabled)",
    )
    run_p.add_argument(
        "--auto-shutdown",
        action="store_true",
        help="所有任务跑完后自动停机(默认不开,保持长驻服务语义)",
    )
    run_p.add_argument(
        "--idle-seconds",
        type=int,
        default=30,
        help="--auto-shutdown 下,持续无新任务多少秒后停机(默认 30)",
    )
    run_p.add_argument(
        "--auto-shutdown-poll",
        type=int,
        default=5,
        help="--auto-shutdown 下,DB 查询间隔秒数(默认 5,范围 2~60)",
    )
    run_p.add_argument(
        "--export-on-finish",
        action="store_true",
        help=(
            "所有 worker join 后自动调一次 scripts/export_dataset.py 导出 "
            "success/failed jsonl(默认关)。常配合 --auto-shutdown 使用: "
            "全部任务跑完 → 自动退出 → 自动导出。"
            "导出输出到 config.paths.output_dir/<时间戳>/ 子目录。"
            "导出过程中的异常不阻塞主进程退出"
        ),
    )

    # load-tasks 子命令(便利包装)
    load_p = sub.add_parser(
        "load-tasks",
        help="加载 jsonl 数据集到 tasks 表(不跑流水线)",
    )
    load_p.add_argument(
        "--jsonl",
        type=Path,
        required=True,
        help="待加载的 jsonl 文件路径",
    )
    load_p.add_argument(
        "--reset-pending",
        action="store_true",
        help="加载前先 DELETE 所有 status=pending 任务",
    )
    load_p.add_argument(
        "--max-rows",
        type=int,
        default=0,
        help="只加载前 N 行(调试用),0=全部",
    )

    return parser


# ---------------------------------------------------------------------------
# 启动子命令: run
# ---------------------------------------------------------------------------


def _setup_environment(config: AppConfig) -> None:
    """阶段 2~5: logging / paths / blacklist / prompts。"""
    setup_logging(config.logging, log_dir=config.paths.log_dir)
    logger.info("=" * 60)
    logger.info("JSpecForge 启动 (platform=%s)", config.platform)
    logger.info("=" * 60)

    ensure_paths(config.paths)
    logger.info("运行时目录已就绪")

    bl_runtime = bl_module.init(config)
    bl_module.register_shutdown_hooks(bl_runtime)
    logger.info(
        "blacklist runtime initialized (active=%d 关键字)",
        len(bl_runtime.active),
    )

    init_default_loader(Path(config.paths.prompts_dir))
    logger.info("prompts loader initialized")


def _build_llm_client(config: AppConfig) -> OpenAICompatClient:
    """阶段 7: 创建 LLM 客户端。"""
    vendor_router_obj = build_router_from_csv(config.llm.vendors_csv)
    breaker = CircuitBreaker(
        cooldown_seconds=config.llm.model_cooldown_seconds,
        trigger_failures=config.llm.cooldown_trigger_failures,
    )
    from jspecforge.llm.client import LLMCallLogger

    call_logger = LLMCallLogger(config.paths.log_dir)
    client = OpenAICompatClient(
        config=config.llm,
        router=vendor_router_obj,
        breaker=breaker,
        call_logger=call_logger,
    )
    logger.info(
        "LLM client initialized (vendors=%d, max_concurrent_global=%d)",
        len(vendor_router_obj.vendors),
        config.llm.max_concurrent_global,
    )
    return client


def _ensure_pool_capacity(config: AppConfig) -> None:
    """检查 pool_size ≥ sum(workers)+5,不足则 WARN 并自动升。

    PooledDB(blocking=True) 超限会永久阻塞,必须留余量。
    """
    workers_cfg = config.workers
    n_workers = (
        workers_cfg.task_a + workers_cfg.openjml_esc + workers_cfg.rac
        + workers_cfg.input_gen + workers_cfg.pbt + workers_cfg.mt
        + workers_cfg.final
    )
    # +5: router_conn(1) + monitor 瞬时连接(1) + 余量(3)
    needed = n_workers + 5
    current = config.database.pool_size
    if current < needed:
        logger.warning(
            "database.pool_size=%d < 需要的 %d (workers=%d + 余量5),"
            "自动升到 %d",
            current, needed, n_workers, needed,
        )
        # frozen dataclass 不能赋值,用 object.__setattr__ 绕过
        object.__setattr__(config.database, "pool_size", needed)
    else:
        logger.info(
            "连接池容量校验通过: pool_size=%d ≥ %d (workers=%d)",
            current, needed, n_workers,
        )


class IdleShutdownWatcher:
    """``--auto-shutdown`` 后台 watcher: DB 无活跃任务 + 队列全空 + 持续 N 秒
    → 触发 ``request_shutdown``。

    检查条件 (每次 poll 时):
    1. DB: ``SELECT 1 FROM tasks WHERE status IN ('pending', 'in_progress') LIMIT 1``
       —— 有任意一行 → 不算空闲
    2. 内存队列: ``qm.total_pending()`` —— 有任务在飞 → 不算空闲

    两条都满足"空闲"才累加空闲时间;任意一条不满足则**重置**空闲计时器。
    空闲累计 ≥ ``idle_seconds`` → ``request_shutdown("auto_shutdown_idle")``。

    Args:
        config: AppConfig (拿 DB 连接配置).
        queue_manager: 用于检查内存队列深度.
        idle_seconds: 持续空闲多久后停机.
        poll_interval: DB 检查间隔 (秒,clamp 到 [2, 60]).
    """

    def __init__(
        self,
        config: AppConfig,
        *,
        queue_manager: QueueManager,
        idle_seconds: int = 30,
        poll_interval: int = 5,
    ) -> None:
        from jspecforge.core.shutdown import request_shutdown
        self._config = config
        self._qm = queue_manager
        self._idle_required = max(1, idle_seconds)
        self._poll = max(2, min(60, poll_interval))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._request_shutdown = request_shutdown
        # 累计空闲时间 (秒); 每次 poll 间隔加上 _poll, 不空闲时重置 0
        self._idle_accumulated = 0.0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        t = threading.Thread(
            target=self._loop,
            name="auto-shutdown-watcher",
            daemon=True,
        )
        self._thread = t
        t.start()
        logger.info(
            "IdleShutdownWatcher started (idle=%ds, poll=%ds)",
            self._idle_required, self._poll,
        )

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _is_idle(self) -> bool:
        # 1. 内存队列必须为空
        if self._qm.total_pending() > 0:
            return False
        # 2. DB 中无 pending / in_progress
        try:
            with get_connection(self._config.database) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT 1 FROM tasks "
                        "WHERE status IN ('pending', 'in_progress') LIMIT 1"
                    )
                    row = cur.fetchone()
            return row is None
        except Exception as exc:  # noqa: BLE001
            logger.warning("IdleShutdownWatcher DB 查询失败: %s,本轮不算空闲", exc)
            return False

    def _loop(self) -> None:
        from jspecforge.core.shutdown import get_shutdown_event
        shutdown_event = get_shutdown_event()
        # 启动时先等一个 poll 周期,避免 worker 还没拿到任务就误判空闲
        self._stop.wait(self._poll)
        while not self._stop.is_set() and not shutdown_event.is_set():
            if self._is_idle():
                self._idle_accumulated += self._poll
                logger.debug(
                    "auto-shutdown: idle %.0f/%d 秒",
                    self._idle_accumulated, self._idle_required,
                )
                if self._idle_accumulated >= self._idle_required:
                    logger.info(
                        "auto-shutdown: 持续空闲 %.0f 秒,触发优雅停机",
                        self._idle_accumulated,
                    )
                    self._request_shutdown("auto_shutdown_idle")
                    break
            else:
                if self._idle_accumulated > 0:
                    logger.debug("auto-shutdown: 检测到活动,重置空闲计时器")
                self._idle_accumulated = 0.0
            self._stop.wait(self._poll)


def _try_start_monitor(
    config: AppConfig,
    qm: QueueManager,
    tracker: InfraErrorTracker,
    llm_client: OpenAICompatClient,
) -> Optional[object]:
    """阶段 12: 启 monitor(可选, P7.4 实现后才有)。"""
    if not config.monitor.enabled:
        logger.info("monitor: disabled")
        return None
    try:
        from jspecforge.monitor.dashboard import Dashboard
    except ImportError:
        logger.warning("monitor: Dashboard 模块未实现,跳过(待 P7.4)")
        return None

    try:
        dashboard = Dashboard(
            config=config,
            queue_manager=qm,
            infra_error_tracker=tracker,
            llm_client=llm_client,
        )
        dashboard.start()
        logger.info("monitor: Dashboard 已启动")
        return dashboard
    except Exception as exc:  # noqa: BLE001
        logger.error("monitor 启动失败,继续无 monitor 运行: %s", exc)
        return None


def cmd_run(config: AppConfig, args: argparse.Namespace) -> int:
    """``run`` 子命令主流程。"""
    _setup_environment(config)

    # ── 连接池容量校验必须在任何 DB 操作之前 ──
    # PooledDB 一旦创建就永久缓存,后续改 pool_size 无效。所以必须在
    # run_load / get_connection 之前完成校验和升容。
    _ensure_pool_capacity(config)

    if args.load_from is not None:
        from scripts.load_tasks import run_load

        logger.info("加载数据集: %s", args.load_from)
        stats = run_load(
            config,
            args.load_from,
            reset_pending_first=args.reset_pending,
            max_rows=0,
        )
        logger.info(
            "数据加载完成: read=%d inserted=%d skipped=%d deleted_pending=%d",
            stats["read"],
            stats["inserted"],
            stats["skipped"],
            stats["deleted_pending"],
        )

    llm_client = _build_llm_client(config)

    if not in_test_mode():
        register_signal_handlers()
    shutdown_event = get_shutdown_event()

    qm = QueueManager()

    router_conn = get_connection(config.database)
    router = Router(qm, router_conn)
    tracker = InfraErrorTracker.from_config(config)

    bl_runtime = bl_module.get_instance()
    orchestrator = Orchestrator(
        config,
        queue_manager=qm,
        router=router,
        shutdown_event=shutdown_event,
        infra_error_tracker=tracker,
        llm_client=llm_client,
        blacklist_runtime=bl_runtime,
        connection_factory=lambda: get_connection(config.database),
    )
    # 将活跃任务计数器注入 Router（终态时释放计数）
    router._task_releaser = orchestrator.task_tracker
    router._task_id_releaser = orchestrator
    enqueue_counts = orchestrator.load_existing_tasks()
    if not enqueue_counts:
        logger.warning(
            "DB 中没有 pending / in_progress 任务,worker 会空转直到收到停机信号。"
            "如需加载新批次,先用 --load-from 参数或 load-tasks 子命令。"
        )

    stop_file_path = config.paths.stop_file
    watcher: Optional[StopFileWatcher] = None
    if stop_file_path:
        watcher = StopFileWatcher(stop_file_path, check_interval=2.0)
        watcher.start()

    # --auto-shutdown: 跑完自动退出 (默认关)
    idle_watcher: Optional[IdleShutdownWatcher] = None
    if args.auto_shutdown:
        idle_watcher = IdleShutdownWatcher(
            config,
            queue_manager=qm,
            idle_seconds=args.idle_seconds,
            poll_interval=args.auto_shutdown_poll,
        )
        idle_watcher.start()

    # 先启 monitor (pause console handler),再启 workers (避免 worker 日志撕烂面板)
    monitor_obj = None
    if not args.no_monitor:
        monitor_obj = _try_start_monitor(config, qm, tracker, llm_client)

    try:
        orchestrator.start_workers()
        logger.info("worker 池已启动,等待 shutdown_event(Ctrl+C / STOP 文件 / 全局阈值)")
        # 主线程短轮询 wait,防止某些环境下 shutdown_event.wait() 被信号阻塞;
        # try/except KeyboardInterrupt 是兜底——某些情况下 SIGINT handler 没生效
        # 时,KeyboardInterrupt 会冒到这里
        try:
            while not shutdown_event.is_set():
                shutdown_event.wait(timeout=0.5)
        except KeyboardInterrupt:
            logger.warning("主线程捕获 KeyboardInterrupt,触发 shutdown")
            shutdown_event.set()

        logger.info("shutdown_event 已触发,等所有 worker 结束")
        # **关键**: 立即停 monitor,让控制台恢复,后续日志能正常显示
        if monitor_obj is not None:
            try:
                monitor_obj.stop(timeout=2.0)
            except Exception as exc:  # noqa: BLE001
                logger.error("monitor.stop 失败: %s", exc)
            monitor_obj = None  # 避免 finally 重复 stop

        orchestrator.join(timeout=60.0)

        # ── --export-on-finish: worker 全部 join 之后,在 close_all_pools 之前
        #    调一次导出脚本。任何异常都不能阻断后续的资源清理 ──
        if getattr(args, "export_on_finish", False):
            try:
                from scripts.export_dataset import run_export

                logger.info("export-on-finish: 开始导出数据集...")
                stats = run_export(config)
                logger.info(
                    "export-on-finish 完成: success=%d failed=%d → %s",
                    stats["success_count"],
                    stats["failed_count"],
                    stats["output_dir"],
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "export-on-finish 失败(不影响主进程退出): %s",
                    exc,
                    exc_info=True,
                )
    finally:
        logger.info("开始清理资源...")
        if idle_watcher is not None:
            try:
                idle_watcher.stop(timeout=3.0)
            except Exception as exc:  # noqa: BLE001
                logger.error("idle_watcher.stop 失败: %s", exc)
        try:
            llm_client.shutdown()
        except Exception as exc:  # noqa: BLE001
            logger.error("llm_client.shutdown 失败: %s", exc)
        if watcher is not None:
            try:
                watcher.stop(timeout=3.0)
            except Exception as exc:  # noqa: BLE001
                logger.error("watcher.stop 失败: %s", exc)
        if monitor_obj is not None:
            try:
                monitor_obj.stop()
            except Exception as exc:  # noqa: BLE001
                logger.error("monitor.stop 失败: %s", exc)
        try:
            router_conn.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            close_all_pools()
        except Exception as exc:  # noqa: BLE001
            logger.error("close_all_pools 失败: %s", exc)
        logger.info("JSpecForge 退出")

    return 0


# ---------------------------------------------------------------------------
# 启动子命令: load-tasks
# ---------------------------------------------------------------------------


def cmd_load_tasks(config: AppConfig, args: argparse.Namespace) -> int:
    """``load-tasks`` 子命令,转发到 scripts/load_tasks.py。"""
    from scripts.load_tasks import run_load

    setup_logging(config.logging, log_dir=config.paths.log_dir)
    stats = run_load(
        config,
        args.jsonl,
        reset_pending_first=args.reset_pending,
        max_rows=args.max_rows,
    )
    logger.info("=" * 60)
    logger.info("加载完成:")
    logger.info("  读取 jsonl 行数:     %d", stats["read"])
    if stats["deleted_pending"]:
        logger.info("  删除 pending 任务:   %d", stats["deleted_pending"])
    logger.info("  新增任务:           %d", stats["inserted"])
    logger.info("  跳过(已存在):       %d", stats["skipped"])
    logger.info("=" * 60)
    return 0


# ---------------------------------------------------------------------------
# main 入口
# ---------------------------------------------------------------------------


def main(argv: Optional[list] = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    try:
        config = load_config(args.config)
    except FileNotFoundError as exc:
        logger.error("config.yaml 加载失败: %s", exc)
        return 2

    if args.command == "run":
        return cmd_run(config, args)
    if args.command == "load-tasks":
        return cmd_load_tasks(config, args)

    parser.error(f"未知子命令: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())

"""控制台监控面板（E.1 重做版,2026-05-03）。

设计依据:

- 用户实测后(issues-2026-05-02 问题 1.x)给出的具体模板
- 数据采集与渲染分离, rich 库渲染

**架构**:

- :class:`MonitorData`: 不可变快照
- :class:`DataCollector`: 从 DB / queue / tracker 采集数据
- :class:`Dashboard`: 后台线程 + rich.Live 渲染

**面板布局**(用户要求, 对照原模板):

::

    ════════════════════════════════════════════════════════════
      JSpecForge Monitor   |   运行时长: HH:MM:SS
    ════════════════════════════════════════════════════════════
      【全局任务总览】
      总任务: N | 已完成: N | 待排队: N | 处理中: N | 成功: N | 失败: N
    ────────────────────────────────────────────────────────────
      【失败原因统计】           【各阶段队列数量】
      exhausted_syntax  : N      task_a    : N
      exhausted_rounds  : N      oj        : N
      exhausted_pbt_compile: N   rac       : N
      其他              : N      input_gen : N
                                 pbt       : N
                                 final     : N
    ────────────────────────────────────────────────────────────
      【当前正在处理任务明细】(in_progress 任务)
      任务ID | 数据集来源 | 阶段 | 逻辑 | 语法 | PBT编译 | 创建时间 | 厂商/模型
    ════════════════════════════════════════════════════════════
      InfraError: N/M (窗口 600s)
      [Ctrl+C 优雅停机]  [touch STOP 文件停机]

**线程**: 独立后台线程, 对核心逻辑零侵入。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from jspecforge.config.settings import AppConfig
from jspecforge.core.queue_manager import QueueManager
from jspecforge.core.shutdown import InfraErrorTracker, get_shutdown_event
from jspecforge.db.connection import get_connection
from jspecforge.db.task_dao import TaskDAO
from jspecforge.utils.log_utils import (
    get_logger,
    pause_console_handler,
    resume_console_handler,
)

_logger = get_logger("monitor.dashboard")


# ---------------------------------------------------------------------------
# 不可变快照
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskRowData:
    """单条 in_progress 任务的展示数据。"""

    task_id: int
    source_dataset: str
    current_step: Optional[str]
    logic_retry: int
    syntax_retry: int
    source_fidelity_retry: int
    pbt_compile_retry: int
    started_at: Optional[str]  # 已格式化为 HH:MM:SS
    vendor_model: str  # 当前/最近的 LLM 厂商+模型, 否则 "--"


@dataclass(frozen=True)
class MonitorData:
    """某一时刻的全局状态快照。"""

    runtime_seconds: float = 0.0
    # 全局任务总览(DB)
    status_counts: Dict[str, int] = field(default_factory=dict)
    # 失败原因分布(只看 status=failed 的任务,按 failure_stage GROUP BY)
    failure_stage_counts: Dict[str, int] = field(default_factory=dict)
    # 各阶段队列数量(qm.qsize_snapshot)
    queue_sizes: Dict[str, int] = field(default_factory=dict)
    # 当前正在处理的任务明细
    in_progress_tasks: List[TaskRowData] = field(default_factory=list)
    # InfraError 全局窗口
    infra_count: int = 0
    infra_max: int = 0
    infra_window_seconds: int = 0
    # 错误信息(采集失败时给提示)
    collect_error: Optional[str] = None


# ---------------------------------------------------------------------------
# 数据采集
# ---------------------------------------------------------------------------


def _format_time_hms(dt: Any) -> Optional[str]:
    """把 datetime / str → 'HH:MM:SS'。"""
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except ValueError:
            return dt[-8:]  # 退化:取最后 8 字符
    if hasattr(dt, "strftime"):
        return dt.strftime("%H:%M:%S")
    return str(dt)[-8:]


def _step_display(current_step: Optional[str]) -> str:
    """current_step 美化显示。"""
    if not current_step:
        return "--"
    mapping = {
        "task_a": "TaskA",
        "openjml": "OJ",
        "rac": "RAC",
        "input_gen": "InputGen",
        "pbt": "PBT",
        "final": "Final",
    }
    return mapping.get(current_step, current_step)


class DataCollector:
    """收集监控数据。每次 ``collect()`` 重新查 DB。"""

    def __init__(
        self,
        config: AppConfig,
        *,
        queue_manager: QueueManager,
        infra_error_tracker: InfraErrorTracker,
        llm_client: Any,
        task_dao: type = TaskDAO,
    ) -> None:
        self._config = config
        self._qm = queue_manager
        self._tracker = infra_error_tracker
        self._llm = llm_client  # 保留接口兼容(暂不用)
        self._task_dao = task_dao
        self._start_monotonic = time.monotonic()

    def collect(self) -> MonitorData:
        """采集一次快照。任何子项失败都不抛异常。"""
        runtime = time.monotonic() - self._start_monotonic
        queue_sizes = self._qm.qsize_snapshot()
        infra_count = self._tracker.current_count()
        infra_max = self._tracker._global_max  # noqa: SLF001
        infra_window = self._tracker._window  # noqa: SLF001

        status_counts: Dict[str, int] = {}
        failure_stage_counts: Dict[str, int] = {}
        in_progress_tasks: List[TaskRowData] = []
        collect_error: Optional[str] = None

        try:
            with get_connection(self._config.database) as conn:
                status_counts = self._task_dao.count_by_status(conn)
                failure_stage_counts = self._query_failure_stage_counts(conn)
                in_progress_tasks = self._query_in_progress_tasks(conn)
        except Exception as exc:  # noqa: BLE001
            collect_error = f"DB 采集失败: {type(exc).__name__}: {exc}"
            _logger.warning("monitor DB 采集失败: %s", exc)

        return MonitorData(
            runtime_seconds=runtime,
            status_counts=status_counts,
            failure_stage_counts=failure_stage_counts,
            queue_sizes=queue_sizes,
            in_progress_tasks=in_progress_tasks,
            infra_count=infra_count,
            infra_max=infra_max,
            infra_window_seconds=infra_window,
            collect_error=collect_error,
        )

    # ------------------------------------------------------------------
    # 内部 SQL
    # ------------------------------------------------------------------

    @staticmethod
    def _query_failure_stage_counts(conn: Any) -> Dict[str, int]:
        """SELECT failure_stage, COUNT(*) FROM tasks WHERE status='failed' GROUP BY..."""
        sql = (
            "SELECT failure_stage, COUNT(*) AS cnt "
            "FROM tasks WHERE status='failed' "
            "GROUP BY failure_stage"
        )
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall() or []
        return {
            (r.get("failure_stage") or "(unknown)"): int(r.get("cnt") or 0)
            for r in rows
        }

    def _query_in_progress_tasks(self, conn: Any) -> List[TaskRowData]:
        """查 in_progress 任务明细 + 厂商/模型(从最新 assistant.call_meta)。"""
        # 主查询: in_progress 任务基础信息
        sql = (
            "SELECT id, source_dataset_name, current_step, "
            "       task_a_logic_retry, task_a_syntax_retry_total, "
            "       pbt_compile_retry_total, started_at "
            "FROM tasks WHERE status='in_progress' "
            "ORDER BY started_at"
        )
        with conn.cursor() as cur:
            cur.execute(sql)
            base_rows = cur.fetchall() or []

        if not base_rows:
            return []

        # 子查询: 每个 task 最新一条 assistant 的 call_meta
        # 仅对 current_step IN ('task_a','input_gen') 取(其他阶段不在调 LLM)
        ids_csv = ",".join(str(int(r["id"])) for r in base_rows)

        source_fidelity_sql = (
            f"SELECT task_id, COUNT(*) AS cnt "
            f"FROM task_llm_messages "
            f"WHERE task_id IN ({ids_csv}) "
            f"  AND role='assistant' "
            f"  AND action_taken='retry_source_fidelity' "
            f"GROUP BY task_id"
        )
        source_fidelity_map: Dict[int, int] = {}
        with conn.cursor() as cur:
            cur.execute(source_fidelity_sql)
            for sr in cur.fetchall() or []:
                source_fidelity_map[int(sr["task_id"])] = int(sr.get("cnt") or 0)
        # 拿每个 task 最新的 assistant id (相关子查询)
        meta_sql = (
            f"SELECT m.task_id, m.call_meta "
            f"FROM task_llm_messages m "
            f"INNER JOIN ("
            f"  SELECT task_id, MAX(id) AS max_id "
            f"  FROM task_llm_messages "
            f"  WHERE task_id IN ({ids_csv}) AND role='assistant' "
            f"  GROUP BY task_id "
            f") last ON last.max_id = m.id"
        )
        meta_map: Dict[int, str] = {}
        with conn.cursor() as cur:
            cur.execute(meta_sql)
            for mr in cur.fetchall() or []:
                meta_map[int(mr["task_id"])] = mr.get("call_meta")

        out: List[TaskRowData] = []
        for r in base_rows:
            tid = int(r["id"])
            current_step = r.get("current_step")
            # 仅 task_a / input_gen 阶段显示厂商+模型(其他阶段在跑 OJ/RAC/PBT 不调 LLM)
            vendor_model = "--"
            if current_step in ("task_a", "input_gen"):
                meta_raw = meta_map.get(tid)
                if meta_raw:
                    try:
                        meta = (
                            json.loads(meta_raw)
                            if isinstance(meta_raw, (str, bytes, bytearray))
                            else meta_raw
                        )
                        vendor = meta.get("vendor_name") or "?"
                        model = meta.get("model_used") or "?"
                        vendor_model = f"{vendor}/{model}"
                    except Exception:  # noqa: BLE001
                        pass
            out.append(
                TaskRowData(
                    task_id=tid,
                    source_dataset=str(r.get("source_dataset_name") or "--"),
                    current_step=current_step,
                    logic_retry=int(r.get("task_a_logic_retry") or 0),
                    syntax_retry=int(r.get("task_a_syntax_retry_total") or 0),
                    source_fidelity_retry=source_fidelity_map.get(tid, 0),
                    pbt_compile_retry=int(r.get("pbt_compile_retry_total") or 0),
                    started_at=_format_time_hms(r.get("started_at")),
                    vendor_model=vendor_model,
                )
            )
        return out


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def _format_runtime(seconds: float) -> str:
    rt = int(seconds)
    h, rem = divmod(rt, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _build_overview_table(data: MonitorData) -> Table:
    """全局任务总览(单行)。"""
    sc = data.status_counts
    total = sum(sc.values()) if sc else 0
    pending = sc.get("pending", 0)
    in_progress = sc.get("in_progress", 0)
    success = sc.get("success", 0)
    failed = sc.get("failed", 0)
    done = success + failed

    table = Table.grid(padding=(0, 2))
    table.add_column(justify="left")
    text = Text()
    text.append("总任务: ", style="bold")
    text.append(f"{total}")
    text.append("  |  已完成: ", style="bold")
    text.append(f"{done}")
    text.append("  |  待排队: ", style="bold")
    text.append(f"{pending}")
    text.append("  |  处理中: ", style="bold")
    text.append(f"{in_progress}", style="cyan")
    text.append("  |  成功: ", style="bold")
    text.append(f"{success}", style="green")
    text.append("  |  失败: ", style="bold")
    text.append(f"{failed}", style="red")
    table.add_row(text)
    return table


def _build_failure_and_queue_table(data: MonitorData) -> Table:
    """失败原因统计 + 各阶段队列数量(并排两列)。"""
    fail_table = Table(show_header=True, header_style="bold red", padding=(0, 1))
    fail_table.add_column("失败原因", min_width=30)
    fail_table.add_column("数量", justify="right", min_width=6)
    if data.failure_stage_counts:
        for stage, cnt in sorted(data.failure_stage_counts.items()):
            fail_table.add_row(stage, str(cnt))
    else:
        fail_table.add_row("[dim]无[/]", "0")

    queue_table = Table(show_header=True, header_style="bold cyan", padding=(0, 1))
    queue_table.add_column("队列阶段", min_width=14)
    queue_table.add_column("数量", justify="right", min_width=6)
    qsz = data.queue_sizes
    for stage in ("task_a", "oj", "rac", "input_gen", "pbt", "final"):
        queue_table.add_row(stage, str(qsz.get(stage, 0)))

    # 把两个 table 横向放在一个 grid 里
    grid = Table.grid(padding=(0, 4))
    grid.add_column()
    grid.add_column()
    grid.add_row(fail_table, queue_table)
    return grid


def _build_in_progress_table(data: MonitorData) -> Table:
    """当前正在处理任务明细表。"""
    table = Table(
        show_header=True,
        header_style="bold magenta",
        title="【当前正在处理任务明细】",
        title_style="bold",
        padding=(0, 1),
    )
    table.add_column("任务ID", justify="right", min_width=6)
    table.add_column("数据集", min_width=12)
    table.add_column("阶段", min_width=10)
    table.add_column("逻辑重试", justify="right", min_width=6)
    table.add_column("语法重试", justify="right", min_width=6)
    table.add_column("源码失真", justify="right", min_width=8)
    table.add_column("PBT编译重试", justify="right", min_width=8)
    table.add_column("开始时间", min_width=10)
    table.add_column("厂商/模型", min_width=20)

    if not data.in_progress_tasks:
        table.add_row("[dim]空[/]", "", "", "", "", "", "", "", "")
        return table

    for r in data.in_progress_tasks:
        table.add_row(
            str(r.task_id),
            r.source_dataset,
            _step_display(r.current_step),
            str(r.logic_retry),
            str(r.syntax_retry),
            str(r.source_fidelity_retry),
            str(r.pbt_compile_retry),
            r.started_at or "--",
            r.vendor_model,
        )
    return table


def _build_footer_text(data: MonitorData) -> Text:
    """底部 InfraError + 操作提示。"""
    text = Text()
    text.append("InfraError: ", style="bold")
    infra_color = "white"
    if data.infra_count > 0:
        infra_color = "yellow" if data.infra_count < data.infra_max else "bold red"
    text.append(
        f"{data.infra_count}/{data.infra_max}",
        style=infra_color,
    )
    text.append(f" (窗口 {data.infra_window_seconds}s)", style="dim")
    if data.infra_count >= data.infra_max and data.infra_max > 0:
        text.append(" [触发优雅停机]", style="bold red")
    text.append("    ")
    text.append("[Ctrl+C 优雅停机]  [touch STOP 文件停机]", style="dim")
    return text


def render_dashboard(data: MonitorData) -> Panel:
    """根据 MonitorData 渲染 rich Panel。"""
    title = Text()
    title.append("JSpecForge Monitor", style="bold cyan")
    title.append("   |   运行时长: ", style="dim")
    title.append(_format_runtime(data.runtime_seconds), style="bold")

    overview = _build_overview_table(data)
    fail_queue = _build_failure_and_queue_table(data)
    in_progress = _build_in_progress_table(data)
    footer = _build_footer_text(data)

    # 把 4 块用 Group 垂直拼起来,每块之间一条分隔线
    sep = Text("─" * 100, style="dim")

    blocks = [
        Text("【全局任务总览】", style="bold"),
        overview,
        sep,
        fail_queue,
        sep,
        in_progress,
        sep,
        footer,
    ]

    if data.collect_error:
        blocks.insert(0, Text(f"⚠ 采集错误: {data.collect_error}", style="red"))

    body = Group(*blocks)
    return Panel(
        body,
        title=title,
        border_style="cyan",
        padding=(1, 2),
    )


# ---------------------------------------------------------------------------
# Dashboard 主类
# ---------------------------------------------------------------------------


class Dashboard:
    """rich 监控面板(独立后台线程)。"""

    def __init__(
        self,
        config: AppConfig,
        *,
        queue_manager: QueueManager,
        infra_error_tracker: InfraErrorTracker,
        llm_client: Any,
        task_dao: type = TaskDAO,
        console: Optional[Console] = None,
    ) -> None:
        self._config = config
        self._collector = DataCollector(
            config,
            queue_manager=queue_manager,
            infra_error_tracker=infra_error_tracker,
            llm_client=llm_client,
            task_dao=task_dao,
        )
        self._refresh = max(1, config.monitor.refresh_interval)
        # force_terminal=True 让 IDE/PyCharm 等非 TTY 终端也输出 ANSI 控制序列
        # (清屏 + 重画依赖 ANSI)
        self._console = console or Console(force_terminal=True, color_system="auto")
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """启动后台渲染线程。重复调用 no-op。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        if pause_console_handler():
            _logger.info("Dashboard: 已暂停 console 日志输出(日志只写文件)")
        t = threading.Thread(
            target=self._loop,
            name="monitor-dashboard",
            daemon=True,
        )
        self._thread = t
        t.start()
        _logger.info("Dashboard started (refresh=%ds)", self._refresh)

    def stop(self, timeout: float = 5.0) -> None:
        """通知线程退出并 join。"""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        if resume_console_handler():
            _logger.info("Dashboard: 已恢复 console 日志输出")

    def render_once(self) -> Panel:
        data = self._collector.collect()
        return render_dashboard(data)

    def _loop(self) -> None:
        """主循环: 手动清屏 + 重画(避免 rich.Live 在非 TTY 终端下不刷新)。

        rich.Live 在 PyCharm/IDE 终端下经常 ``is_terminal=False``,
        退化为静态打印,不会动态刷新。改用手动 ANSI 清屏 + Console.print
        实现真实刷新效果。
        """
        shutdown_event = get_shutdown_event()
        # ANSI: 移光标到 (1,1) + 清屏到末尾
        clear_seq = "\x1b[H\x1b[2J\x1b[3J"
        first = True
        try:
            while not self._stop.is_set() and not shutdown_event.is_set():
                if not first:
                    self._stop.wait(self._refresh)
                    if self._stop.is_set() or shutdown_event.is_set():
                        break
                first = False
                try:
                    panel = self._collector_render_safe()
                    # 清屏后重画
                    self._console.file.write(clear_seq)
                    self._console.print(panel)
                    self._console.file.flush()
                except Exception as exc:  # noqa: BLE001
                    _logger.error("Dashboard 渲染异常: %s", exc)
        except Exception as exc:  # noqa: BLE001
            _logger.error("Dashboard 渲染异常,退出循环: %s", exc)

    def _collector_render_safe(self) -> Panel:
        """采集 + 渲染。任何一步失败返回错误 Panel。"""
        try:
            data = self._collector.collect()
        except Exception as exc:  # noqa: BLE001
            _logger.error("Dashboard collect 失败: %s", exc)
            data = MonitorData(collect_error=f"collect 异常: {exc}")
        try:
            return render_dashboard(data)
        except Exception as exc:  # noqa: BLE001
            _logger.error("Dashboard render 失败: %s", exc)
            return Panel(
                Text(f"render 异常: {exc}", style="red"),
                title="JSpecForge Monitor (渲染错误)",
                border_style="red",
            )

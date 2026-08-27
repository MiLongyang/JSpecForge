"""黑名单运行时（进程级单例 + 跨进程持久化）。

依据 ``docs/design/核心工作流程设计.md`` §7.3 / §7.4 与 ``docs/design/
config.yaml配置规格.md`` §10b。

三层来源合并：

  ::

    runtime_blacklist = set(v1) ∪ load_persisted(dynamic_persist_file)
    runtime_blacklist -= set(protected)

- ``v1``：预置黑名单，永久启用，不写回持久化文件
- 持久化文件：上次进程退出时落盘的"动态新增"关键字
- ``protected``：OJ 实际支持的关键字，绝对禁止进入运行时黑名单（gotchas §4.4）

落盘只写"运行期间新增的部分"（即既不在 ``v1`` 也不在 ``protected``、确实
是本进程或上次进程动态吸入的关键字），避免重复落盘 v1。

钩子注册：``register_shutdown_hooks`` 同时注册 ``atexit`` /
``signal.SIGTERM`` / ``signal.SIGINT``。**P3 阶段直接接管全部三个**；
P7 编排器若需要自己的关停流程，应当显式调用 :func:`save_to_disk` 后再
继续，避免动态黑名单丢失（详见 docs/plan/P7-核心调度.md 的备注）。
"""

from __future__ import annotations

import atexit
import json
import re
import signal
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Set, Tuple

from jspecforge.config.settings import AppConfig, BlacklistConfig
from jspecforge.utils.log_utils import get_logger

_logger = get_logger("tools.blacklist_runtime")

# 持久化 JSON 文件 schema 版本
PERSIST_SCHEMA_VERSION: int = 1


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass
class BlacklistRuntime:
    """运行时黑名单状态。

    实例字段都是可变的（用 set 维护当前生效集合），但不应直接修改外部传入的
    set，所有状态修改请走 ``check`` / ``update`` 等方法以保证线程安全。

    Attributes:
        v1: 配置中的预置黑名单（不可变，落盘时排除）。
        protected: 配置中的保护列表（不可变，永远从运行时集合中扣除）。
        active: 当前生效的黑名单 set（v1 ∪ persisted - protected）。
        persist_file: 持久化文件路径，``None`` 表示禁用持久化。
        enabled: 黑名单总开关。False 时 :meth:`check` 恒不命中、:meth:`update`
            恒不扩充。
    """

    v1: frozenset[str] = field(default_factory=frozenset)
    protected: frozenset[str] = field(default_factory=frozenset)
    active: Set[str] = field(default_factory=set)
    persist_file: Optional[Path] = None
    enabled: bool = True
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    # ---- 公开方法 -----------------------------------------------------

    def check(self, jml_text: str) -> Tuple[bool, List[str]]:
        """对 JML 文本做字符串匹配，返回 (是否命中, 命中的关键字列表)。

        Args:
            jml_text: 待检测的 JML 文本（通常是 ``full_java_with_jml`` 中
                的 JML 注解部分，但本函数不假设来源，用整个 .java 内容
                也能工作）。

        Returns:
            ``(hit, hits)`` —— ``hit`` 为是否至少命中一个；``hits`` 为
            按字典序排序的命中关键字列表（无命中时为空列表）。
        """
        if not self.enabled:
            return (False, [])
        with self._lock:
            current = sorted(self.active)
        hits = [kw for kw in current if kw in jml_text]
        return (len(hits) > 0, hits)

    def update(self, new_keywords: Iterable[str]) -> List[str]:
        """加入新关键字，过 protected 过滤后返回真正新增的部分。

        Args:
            new_keywords: 待加入的关键字（通常来自
                ``oj_classifier.extract_unsupported_keywords``）。

        Returns:
            真正新增到 ``active`` 中的关键字列表（按首次出现顺序）。
            被 ``protected`` 拦截或已在 ``active`` 中的不计入。
        """
        if not self.enabled:
            return []
        added: List[str] = []
        with self._lock:
            for kw in new_keywords:
                if not kw:
                    continue
                if kw in self.protected:
                    _logger.warning(
                        "blacklist update: 关键字 %r 在 protected 中，"
                        "拒绝加入",
                        kw,
                    )
                    continue
                if kw in self.active:
                    continue
                self.active.add(kw)
                added.append(kw)
        if added:
            _logger.info("blacklist update: 新增动态关键字 %s", added)
        return added

    def snapshot_dynamic(self) -> List[str]:
        """返回当前 active 中的"动态新增"关键字（排除 v1 与 protected）。

        Returns:
            按字典序排序的关键字列表，可直接落盘。
        """
        with self._lock:
            dynamic = self.active - set(self.v1) - set(self.protected)
        return sorted(dynamic)


# ---------------------------------------------------------------------------
# 模块级单例
# ---------------------------------------------------------------------------


_INSTANCE: Optional[BlacklistRuntime] = None
_INSTANCE_LOCK = threading.Lock()
_HOOKS_REGISTERED: bool = False


def init(config: AppConfig) -> BlacklistRuntime:
    """初始化全局单例（合并三层来源）。

    重复调用是幂等的：如果已初始化则直接返回现有实例；要重置请调用
    :func:`reset_for_testing`（仅测试用）。

    Args:
        config: ``AppConfig`` 实例。

    Returns:
        全局 ``BlacklistRuntime`` 实例。
    """
    global _INSTANCE
    with _INSTANCE_LOCK:
        if _INSTANCE is not None:
            return _INSTANCE
        _INSTANCE = _build_runtime(config.blacklist)
    return _INSTANCE


def get_instance() -> BlacklistRuntime:
    """获取已初始化的单例。未 ``init`` 则抛错。

    Raises:
        RuntimeError: 单例尚未初始化。
    """
    if _INSTANCE is None:
        raise RuntimeError(
            "BlacklistRuntime 未初始化，请先调用 blacklist_runtime.init(config)"
        )
    return _INSTANCE


def reset_for_testing() -> None:
    """重置全局状态，仅供测试使用。

    清空单例与"已注册钩子"标记，便于在测试用例之间隔离。**生产代码禁用**。
    """
    global _INSTANCE, _HOOKS_REGISTERED
    with _INSTANCE_LOCK:
        _INSTANCE = None
        _HOOKS_REGISTERED = False


# ---------------------------------------------------------------------------
# 持久化（独立函数，便于单测直接调用，不必走 atexit）
# ---------------------------------------------------------------------------


def load_persisted(path: Optional[Path]) -> Set[str]:
    """从 JSON 持久化文件中读取"动态新增"关键字集合。

    Args:
        path: 持久化文件路径；``None`` 或文件不存在时返回空集。

    Returns:
        关键字 set；JSON 损坏时返回空集并记 WARNING 日志（不抛异常，
        避免阻塞启动）。
    """
    if path is None:
        return set()
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _logger.warning(
            "blacklist persist: 读取 %s 失败 (%s)，将以空集合启动",
            path,
            exc,
        )
        return set()
    keywords = data.get("keywords", []) if isinstance(data, dict) else []
    if not isinstance(keywords, list):
        _logger.warning(
            "blacklist persist: %s 中 keywords 字段不是列表，已忽略",
            path,
        )
        return set()
    return {kw for kw in keywords if isinstance(kw, str) and kw}


def save_to_disk(runtime: BlacklistRuntime, path: Optional[Path]) -> None:
    """把"运行期新增"关键字落盘到 JSON 文件。

    JSON 格式：

      .. code-block:: json

         {
           "version": 1,
           "updated_at": "2026-04-30T12:34:56+08:00",
           "keywords": ["\\\\bsum", "\\\\sum"]
         }

    时区采用本地时区（带偏移量），与 docs §7.4 示例一致。

    Args:
        runtime: 运行时实例。
        path: 持久化文件路径；``None`` 表示禁用持久化（直接返回）。
    """
    if path is None:
        return
    dynamic = runtime.snapshot_dynamic()
    payload = {
        "version": PERSIST_SCHEMA_VERSION,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "keywords": dynamic,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        _logger.warning(
            "blacklist persist: 写入 %s 失败 (%s)，丢弃 %d 个动态关键字",
            path,
            exc,
            len(dynamic),
        )
        return
    _logger.info(
        "blacklist persist: 已落盘 %d 个动态关键字 → %s",
        len(dynamic),
        path,
    )


# ---------------------------------------------------------------------------
# 关停钩子（atexit + SIGTERM + SIGINT）
# ---------------------------------------------------------------------------


def register_shutdown_hooks(runtime: BlacklistRuntime) -> None:
    """注册 atexit + SIGTERM + SIGINT 三个关停钩子。

    幂等：重复调用只注册一次。SIGTERM/SIGINT 收到信号后，先执行落盘，再
    用默认行为终止进程（``signal.default_int_handler`` 抛
    ``KeyboardInterrupt``；SIGTERM 用 ``raise SystemExit``）。

    Args:
        runtime: 运行时实例。

    Note:
        在 Windows 上 ``signal.SIGTERM`` 行为有限（Python 不保证拦截 taskkill
        发出的 SIGTERM），但 ``atexit`` 仍会在正常退出时触发，可作为兜底。
    """
    global _HOOKS_REGISTERED
    if _HOOKS_REGISTERED:
        return

    persist_file = runtime.persist_file

    def _on_exit() -> None:
        save_to_disk(runtime, persist_file)

    atexit.register(_on_exit)

    def _on_signal(signum: int, _frame: object) -> None:
        _logger.info(
            "blacklist runtime: 收到信号 %s，落盘动态黑名单后退出",
            signal.Signals(signum).name,
        )
        save_to_disk(runtime, persist_file)
        # 抬手让原默认行为接管退出（不直接 sys.exit，避免 atexit 二次执行）
        if signum == signal.SIGINT:
            raise KeyboardInterrupt
        raise SystemExit(128 + signum)

    # SIGINT 与 SIGTERM 在所有平台都受支持（Python 在 Windows 上对 SIGTERM
    # 的支持是"原生"信号集的一部分，主要用于 Ctrl+Break 等场景）。
    try:
        signal.signal(signal.SIGINT, _on_signal)
    except (ValueError, OSError) as exc:
        _logger.warning("无法注册 SIGINT 钩子: %s", exc)
    try:
        signal.signal(signal.SIGTERM, _on_signal)
    except (ValueError, OSError) as exc:
        _logger.warning("无法注册 SIGTERM 钩子: %s", exc)

    _HOOKS_REGISTERED = True
    _logger.info(
        "blacklist runtime: 已注册 atexit + SIGTERM + SIGINT 三个关停钩子"
    )


# ---------------------------------------------------------------------------
# 内部构建逻辑
# ---------------------------------------------------------------------------


_PLACEHOLDER_RE: re.Pattern[str] = re.compile(r"\{(\w+)\}")


def _resolve_persist_path(persist_value: Optional[str]) -> Optional[Path]:
    """把 ``BlacklistConfig.dynamic_persist_file`` 解析为 ``Path``。

    config.loader 已对 ``{workspace_dir}`` 占位符做过插值，本函数仅做
    类型转换 + 容错（None / 空串视为禁用）。
    """
    if persist_value is None:
        return None
    text = str(persist_value).strip()
    if not text or text.lower() == "null":
        return None
    if _PLACEHOLDER_RE.search(text):
        # 占位符未被插值（理论上不会发生，config.loader 已处理）
        _logger.warning(
            "blacklist persist: dynamic_persist_file 仍含未插值占位符: %r，"
            "将禁用持久化",
            text,
        )
        return None
    return Path(text)


def _build_runtime(blacklist: BlacklistConfig) -> BlacklistRuntime:
    """构建 ``BlacklistRuntime`` 实例。

    合并顺序：``v1 ∪ persisted - protected``，与 docs §7.3 一致。

    ``blacklist.enabled=False`` 时完全关闭：返回空 active、禁用持久化，
    ``check``/``update`` 均因 ``enabled=False`` 直接短路，静态 v1 与动态扩充
    一并失效。
    """
    if not getattr(blacklist, "enabled", True):
        _logger.info(
            "blacklist runtime: disabled (blacklist.enabled=false) → "
            "空黑名单，静态 v1 / 动态扩充 / 持久化全部关闭"
        )
        return BlacklistRuntime(
            v1=frozenset(),
            protected=frozenset(),
            active=set(),
            enabled=False,
            persist_file=None,
        )

    v1 = frozenset(blacklist.v1 or [])
    protected = frozenset(blacklist.protected or [])
    persist_path = _resolve_persist_path(blacklist.dynamic_persist_file)
    persisted = load_persisted(persist_path)

    active: Set[str] = set(v1) | persisted
    active -= set(protected)

    runtime = BlacklistRuntime(
        v1=v1,
        protected=protected,
        active=active,
        persist_file=persist_path,
    )
    _logger.info(
        "blacklist runtime: init done (v1=%d, persisted=%d, protected=%d, "
        "active=%d, persist_file=%s)",
        len(v1),
        len(persisted),
        len(protected),
        len(active),
        persist_path,
    )
    return runtime

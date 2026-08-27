"""厂商配置加载（vendors.csv）+ 跨任务轮询路由。

依据 ``docs/design/LLM调用与厂商管理设计.md`` §一（vendors.csv 8 字段格式）+
§六（厂商轮询分配），并兼容 ``D:\\JSpecForge\\vendor_checker`` 项目的实际
落地格式（实测可用，与 design 文档口径有少量出入，以实测为准）。

vendors.csv 严格 8 列：

    vendor_name, base_url, api_key, available_models, unavailable_models,
    enabled, use_proxy, user_enabled

**解析容差**（与实测保持一致）：

- **模型分隔符**：优先 ``|``；回落 ``;``。两者不能在同一个字段里混用（不允许）
- **文件编码**：``utf-8-sig``（兼容带 BOM 的 CSV）
- **bool 字段**：大小写不敏感（``TRUE`` / ``true`` / ``1`` / ``yes`` 都接受）
- **空 ``available_models``**：当且仅当 ``enabled=False`` 或 ``user_enabled=False``
  时允许（这一行反正会被过滤掉）。否则 fail-fast
- **重名 ``vendor_name``**：**允许**（实际场景：同品牌多 base_url 或多 api_key
  代表不同模型组）。系统按 ``(base_url, api_key)`` 做内部唯一性；同名厂商
  在 :attr:`Vendor.unique_id` 中加 host 后缀做区分（如 ``Joverna (jiuuij.de5.net)``）

加载规则（design §1.3）：

- 启动时一次性读取，不动态重载
- 过滤：``enabled = true`` AND ``user_enabled = true``
- 模型过滤：从 ``available_models`` 中扣除 ``unavailable_models``，扣空的
  vendor 直接跳过
- 解析错误（列数不对、必填空、bool 字段非合法值、(base_url, api_key) 重复等）
  → ``ValueError`` fail-fast

API key 直接从 CSV 读取，**不入 git**（``.gitignore`` 已忽略
``vendors.csv``）。
"""

from __future__ import annotations

import csv
import threading
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from jspecforge.utils.log_utils import get_logger

_logger = get_logger("llm.vendor_router")


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Vendor:
    """单个厂商的不可变配置。

    Attributes:
        name: 厂商名（vendors.csv 第 1 列，**不强制唯一**，仅作显示标签）。
        base_url: API 基址（含协议，如 ``https://api.deepseek.com``）。
        api_key: API key（直接来自 CSV）。
        models: 过滤后有效的模型列表（首选模型 = 第 0 个）。
        use_proxy: 是否需要走代理。
        unique_id: 内部唯一标识，用作熔断/信号量的字典键。当 ``name`` 在加载
            的全部 vendor 中唯一时，``unique_id == name``；否则 ``unique_id =
            f"{name} ({host})"``，其中 host 从 ``base_url`` 提取。直接构造
            Vendor 时若不提供，``__post_init__`` 自动用 ``name`` 兜底。
    """

    name: str
    base_url: str
    api_key: str
    models: Tuple[str, ...]
    use_proxy: bool
    unique_id: str = ""  # 由 _assign_unique_ids 在加载阶段填入；直接构造时 __post_init__ 兜底

    def __post_init__(self) -> None:
        # frozen=True 下用 object.__setattr__ 设置默认 unique_id
        if not self.unique_id:
            object.__setattr__(self, "unique_id", self.name)

    @property
    def primary_model(self) -> str:
        """首选模型（available_models 第一个）。"""
        return self.models[0]

    def chat_completions_url(self) -> str:
        """OpenAI 兼容的 chat completions 端点。

        统一 ``{base_url}/v1/chat/completions``，与 design §2.1 一致。
        若 ``base_url`` 末尾已含 ``/v1``，会避免双 ``/v1``。
        """
        url = self.base_url.rstrip("/")
        if url.endswith("/v1"):
            return f"{url}/chat/completions"
        return f"{url}/v1/chat/completions"


@dataclass(frozen=True)
class ModelChoice:
    """单次调用待尝试的 (vendor, model) 组合。"""

    vendor: Vendor
    model: str

    @property
    def key(self) -> str:
        """形如 ``Joverna (jiuuij.de5.net)/Qwen3.6-Plus`` 的唯一键，用于熔断器记录。

        使用 ``vendor.unique_id``（自动消歧），不直接用 ``vendor.name``。
        """
        return f"{self.vendor.unique_id}/{self.model}"


# ---------------------------------------------------------------------------
# CSV 解析
# ---------------------------------------------------------------------------


_EXPECTED_HEADER: Tuple[str, ...] = (
    "vendor_name",
    "base_url",
    "api_key",
    "available_models",
    "unavailable_models",
    "enabled",
    "use_proxy",
    "user_enabled",
)


def _parse_bool(value: str, *, field_name: str, vendor_hint: str) -> bool:
    v = (value or "").strip().lower()
    if v in ("true", "1", "yes"):
        return True
    if v in ("false", "0", "no", ""):
        return False
    raise ValueError(
        f"vendors.csv: 厂商 {vendor_hint!r} 字段 {field_name!r} 不是合法 bool: {value!r}"
    )


def _parse_models(text: str) -> List[str]:
    """按分隔符拆分模型列表。优先 ``|``，回落 ``;``。

    单字段内不允许混用 ``|`` 与 ``;``：探测到混用时抛 ``ValueError``。
    """
    s = (text or "").strip()
    if not s:
        return []
    has_pipe = "|" in s
    has_semicolon = ";" in s
    if has_pipe and has_semicolon:
        raise ValueError(
            f"vendors.csv: 模型字段 {s!r} 同时含 | 和 ; 两种分隔符，不允许混用"
        )
    sep = "|" if has_pipe else ";"
    return [m.strip() for m in s.split(sep) if m.strip()]


def _short_host(base_url: str) -> str:
    """从 ``base_url`` 提取 host 部分（不含 scheme/port/path）。

    用作同名 vendor 的消歧后缀。
    """
    try:
        parsed = urlparse(base_url)
        host = parsed.hostname or base_url
    except ValueError:
        host = base_url
    return host or base_url


def _assign_unique_ids(vendors: List[Vendor]) -> List[Vendor]:
    """给同名 vendor 加 host 后缀消歧，返回带 ``unique_id`` 的新列表。

    Vendor 是 frozen dataclass，因此通过 ``dataclasses.replace`` 重建。
    """
    from dataclasses import replace as dc_replace

    name_counts = Counter(v.name for v in vendors)
    out: List[Vendor] = []
    seen_ids: set[str] = set()
    for v in vendors:
        if name_counts[v.name] == 1:
            uid = v.name
        else:
            uid = f"{v.name} ({_short_host(v.base_url)})"
        # 极端兜底：连 host 也撞了，再追加序号
        if uid in seen_ids:
            n = 2
            while f"{uid}#{n}" in seen_ids:
                n += 1
            uid = f"{uid}#{n}"
        seen_ids.add(uid)
        out.append(dc_replace(v, unique_id=uid))
    return out


def load_vendors_csv(path: str | Path) -> List[Vendor]:
    """加载并过滤 vendors.csv，返回有效的 Vendor 列表（保持 CSV 顺序）。

    Args:
        path: vendors.csv 路径。

    Returns:
        过滤（``enabled & user_enabled``，扣除 ``unavailable_models``）后的
        有效厂商列表。每个 Vendor 至少包含 1 个可用模型，并带 ``unique_id``。

    Raises:
        FileNotFoundError: 文件不存在。
        ValueError: 文件格式错（列数、表头、bool 字段、需要时空必填、
            (base_url, api_key) 重复、单字段混用 ``|`` 和 ``;`` 等）。
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"vendors.csv 不存在: {p}")

    # utf-8-sig 兼容带 BOM 的 CSV（vendor_checker 实测出来的格式）
    with p.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        rows = list(reader)

    if not rows:
        raise ValueError(f"vendors.csv 为空: {p}")

    header = tuple(h.strip() for h in rows[0])
    if header != _EXPECTED_HEADER:
        raise ValueError(
            f"vendors.csv 表头不符。期望 {_EXPECTED_HEADER}，实际 {header}"
        )

    seen_keys: set[Tuple[str, str]] = set()
    vendors: List[Vendor] = []
    skipped_disabled = 0
    skipped_empty_models = 0
    skipped_no_model_after_filter = 0

    for line_no, row in enumerate(rows[1:], start=2):
        # 允许注释行（以 # 开头），允许全空行
        if not row or all(not cell.strip() for cell in row):
            continue
        if row[0].lstrip().startswith("#"):
            continue
        if len(row) != len(_EXPECTED_HEADER):
            raise ValueError(
                f"vendors.csv 第 {line_no} 行列数错误：期望 "
                f"{len(_EXPECTED_HEADER)} 列，实际 {len(row)} 列。row={row!r}"
            )
        name = row[0].strip()
        base_url = row[1].strip()
        api_key = row[2].strip()
        available = _parse_models(row[3])
        unavailable = set(_parse_models(row[4]))
        enabled = _parse_bool(row[5], field_name="enabled", vendor_hint=name)
        use_proxy = _parse_bool(row[6], field_name="use_proxy", vendor_hint=name)
        user_enabled = _parse_bool(
            row[7], field_name="user_enabled", vendor_hint=name
        )

        if not name:
            raise ValueError(f"vendors.csv 第 {line_no} 行 vendor_name 为空")
        if not base_url:
            raise ValueError(
                f"vendors.csv 第 {line_no} 行 base_url 为空 (vendor={name!r})"
            )
        if not api_key:
            raise ValueError(
                f"vendors.csv 第 {line_no} 行 api_key 为空 (vendor={name!r})"
            )

        # 早期过滤：disabled 行允许 available_models 为空（不会被使用）
        if not (enabled and user_enabled):
            skipped_disabled += 1
            _logger.info(
                "vendor 跳过（enabled=%s, user_enabled=%s）: %s",
                enabled,
                user_enabled,
                name,
            )
            continue

        # 启用行才校验非空
        if not available:
            raise ValueError(
                f"vendors.csv 第 {line_no} 行 available_models 为空 "
                f"(vendor={name!r}, enabled=true, user_enabled=true)"
            )

        # (base_url, api_key) 唯一性校验（vendor_name 允许重复，但同 url+key 不允许）
        unique_key = (base_url, api_key)
        if unique_key in seen_keys:
            raise ValueError(
                f"vendors.csv 第 {line_no} 行 (base_url, api_key) 重复: "
                f"vendor={name!r}, base_url={base_url!r}"
            )
        seen_keys.add(unique_key)

        # 扣除 unavailable_models
        effective_models = tuple(m for m in available if m not in unavailable)
        if not effective_models:
            skipped_no_model_after_filter += 1
            _logger.warning(
                "vendor 跳过（扣除 unavailable_models 后无可用模型）: %s "
                "(available=%s, unavailable=%s)",
                name,
                available,
                sorted(unavailable),
            )
            continue

        vendors.append(
            Vendor(
                name=name,
                base_url=base_url,
                api_key=api_key,
                models=effective_models,
                use_proxy=use_proxy,
            )
        )

    if not vendors:
        raise ValueError(
            f"vendors.csv 加载后无任何可用厂商：原始 {len(rows) - 1} 行，"
            f"被禁用 {skipped_disabled}，空模型 {skipped_empty_models}，"
            f"无可用模型 {skipped_no_model_after_filter}"
        )

    vendors = _assign_unique_ids(vendors)

    _logger.info(
        "vendor 加载完成: %d 个有效厂商 (跳过 disabled=%d, no_model_after_filter=%d)",
        len(vendors),
        skipped_disabled,
        skipped_no_model_after_filter,
    )
    return vendors


# ---------------------------------------------------------------------------
# 轮询路由
# ---------------------------------------------------------------------------


@dataclass
class VendorRouter:
    """跨任务轮询厂商 + 构建候选 (vendor, model) 序列。

    线程安全：``next_vendor`` 与 ``build_candidate_chain`` 都用 ``RLock`` 保护。

    候选链规则（design §六 + §8.3 单次任务的熔断切换流程）：

    1. 起点：本次任务分配到的"主厂商"的首选模型
    2. 同厂商内：依次尝试 available_models 列表的所有模型
    3. 跨厂商：从主厂商在 vendors 列表中的下一个开始，依次扫完所有厂商及其模型
    4. 不重复：每个 (vendor, model) 在一次任务的链里只出现一次
    """

    vendors: Sequence[Vendor]
    _index: int = field(default=0, init=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.vendors:
            raise ValueError("VendorRouter 需要至少一个 Vendor")

    def next_vendor(self) -> Vendor:
        """返回下一个轮询厂商，全局自增线程安全。"""
        with self._lock:
            vendor = self.vendors[self._index % len(self.vendors)]
            self._index += 1
            return vendor

    def build_candidate_chain(
        self, primary_vendor: Vendor
    ) -> List[ModelChoice]:
        """以 ``primary_vendor`` 为起点构建本次任务的完整候选链。

        Args:
            primary_vendor: 由 :meth:`next_vendor` 返回的主厂商。

        Returns:
            完整候选链：先扫完主厂商所有模型，再依次扫剩余厂商。
            列表中每个 (vendor, model) 唯一。
        """
        with self._lock:
            try:
                start = self.vendors.index(primary_vendor)
            except ValueError:
                # 不在路由列表中（理论不会发生）：兜底从 0 开始
                _logger.warning(
                    "build_candidate_chain: primary_vendor %s 不在路由列表中，"
                    "兜底从 0 开始",
                    primary_vendor.name,
                )
                start = 0
            ordered = list(self.vendors[start:]) + list(self.vendors[:start])

        chain: List[ModelChoice] = []
        seen: set[str] = set()
        for v in ordered:
            for m in v.models:
                key = f"{v.name}/{m}"
                if key in seen:
                    continue
                seen.add(key)
                chain.append(ModelChoice(vendor=v, model=m))
        return chain


# ---------------------------------------------------------------------------
# 便捷工厂
# ---------------------------------------------------------------------------


def build_router_from_csv(path: str | Path) -> VendorRouter:
    """一步完成 ``load_vendors_csv`` + ``VendorRouter`` 构造。"""
    vendors = load_vendors_csv(path)
    return VendorRouter(vendors=tuple(vendors))


def all_model_keys(vendors: Iterable[Vendor]) -> List[str]:
    """返回所有 (vendor, model) 的 key 列表（用于熔断器初始化或调试）。"""
    keys: List[str] = []
    for v in vendors:
        for m in v.models:
            keys.append(f"{v.name}/{m}")
    return keys

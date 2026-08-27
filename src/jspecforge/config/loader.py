"""配置加载模块。

提供 load_config / load_blacklist / validate_config 函数。
支持 paths 字段中的 {workspace_dir} 等占位符插值。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List

import yaml

from jspecforge.config.settings import (
    AppConfig,
    BlacklistConfig,
    CleanupConfig,
    DatabaseConfig,
    EnvironmentConfig,
    ErrorLogConfig,
    ExportConfig,
    JavaConfig,
    LLMConfig,
    LoggingConfig,
    MonitorConfig,
    MTConfig,
    OpenJMLConfig,
    PathsConfig,
    PBTConfig,
    RetryConfig,
    TaskLoadingConfig,
    WorkersConfig,
)

# YAML section → dataclass 映射
_SECTION_MAP: Dict[str, type] = {
    "environment": EnvironmentConfig,
    "paths": PathsConfig,
    "database": DatabaseConfig,
    "openjml": OpenJMLConfig,
    "java": JavaConfig,
    "llm": LLMConfig,
    "pbt": PBTConfig,
    "mt": MTConfig,
    "retry": RetryConfig,
    "blacklist": BlacklistConfig,
    "workers": WorkersConfig,
    "task_loading": TaskLoadingConfig,
    "monitor": MonitorConfig,
    "logging": LoggingConfig,
    "export": ExportConfig,
    "cleanup": CleanupConfig,
    "error_log": ErrorLogConfig,
}

# paths 字段中可被引用的占位符键（按依赖顺序）
_PATH_PLACEHOLDER_KEYS = [
    "workspace_dir",
    "tmp_dir",
    "lib_dir",
    "output_dir",
    "log_dir",
    "prompts_dir",
    "template_dir",
]


class ConfigValidationError(Exception):
    """配置校验失败时抛出。"""


def load_config(config_path: str | Path) -> AppConfig:
    """加载 config.yaml 并构建 AppConfig。

    Args:
        config_path: YAML 配置文件路径。

    Returns:
        校验通过的 AppConfig 实例。

    Raises:
        FileNotFoundError: 配置文件不存在。
        ConfigValidationError: 字段缺失或值非法。
    """
    config_path = Path(config_path)
    if not config_path.is_file():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")

    raw: Dict[str, Any] = _read_yaml(config_path)

    # 提取顶层字段
    platform = raw.pop("platform", "windows")
    pipeline_mode = raw.pop("pipeline_mode", "full")
    # P13: OJ 失败后是否进入 RP 兜底（默认 True 保持旧行为）
    rp_fallback_enabled = bool(raw.pop("rp_fallback_enabled", True))

    # 构建 paths（先做占位符插值）
    paths_data = raw.get("paths", {})
    paths_data = _interpolate_paths(paths_data)
    raw["paths"] = paths_data

    # 对 blacklist.dynamic_persist_file 做 paths 占位符插值
    # （独立处理，因为 _interpolate_paths 仅作用于 paths section 内部）
    blacklist_data = raw.get("blacklist", {})
    if isinstance(blacklist_data, dict):
        persist = blacklist_data.get("dynamic_persist_file")
        if isinstance(persist, str) and "{" in persist:
            for ref_key, ref_val in paths_data.items():
                if isinstance(ref_val, str):
                    persist = persist.replace(f"{{{ref_key}}}", ref_val)
            blacklist_data["dynamic_persist_file"] = persist
            raw["blacklist"] = blacklist_data

    # 构建各子配置
    kwargs: Dict[str, Any] = {
        "platform": platform,
        "pipeline_mode": pipeline_mode,
        "rp_fallback_enabled": rp_fallback_enabled,
    }
    for section_name, dataclass_type in _SECTION_MAP.items():
        section_data = raw.get(section_name, {})
        kwargs[section_name] = _build_dataclass(dataclass_type, section_data)

    config = AppConfig(**kwargs)
    config.validate()
    return config


def load_blacklist(blacklist_path: str | Path) -> List[str]:
    """加载 JML 黑名单关键字文件。

    每行一个关键字，忽略空行和 # 开头的注释行。

    Args:
        blacklist_path: 黑名单文件路径。

    Returns:
        关键字列表。

    Raises:
        FileNotFoundError: 文件不存在。
    """
    blacklist_path = Path(blacklist_path)
    if not blacklist_path.is_file():
        raise FileNotFoundError(f"黑名单文件不存在: {blacklist_path}")

    keywords: List[str] = []
    for line in blacklist_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            keywords.append(stripped)
    return keywords


def _read_yaml(path: Path) -> Dict[str, Any]:
    """读取 YAML 文件并返回字典。"""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ConfigValidationError(
            f"配置文件格式错误: 顶层应为映射，实际为 {type(data).__name__}"
        )
    return data


def _interpolate_paths(paths_data: Dict[str, Any]) -> Dict[str, Any]:
    """对 paths 字段中的占位符进行插值替换。

    支持 {workspace_dir}、{tmp_dir} 等占位符，按依赖顺序逐字段解析。
    """
    resolved: Dict[str, str] = {}

    for key in _PATH_PLACEHOLDER_KEYS:
        val = paths_data.get(key, "")
        if isinstance(val, str) and "{" in val:
            for ref_key, ref_val in resolved.items():
                val = val.replace(f"{{{ref_key}}}", ref_val)
        if isinstance(val, str):
            resolved[key] = val
            paths_data[key] = val

    # 其余 paths 字段也可能包含占位符，用已解析的全部替换
    for key, val in paths_data.items():
        if key in resolved:
            continue
        if isinstance(val, str) and "{" in val:
            for ref_key, ref_val in resolved.items():
                val = val.replace(f"{{{ref_key}}}", ref_val)
            paths_data[key] = val

    return paths_data


def _build_dataclass(cls: type, data: Dict[str, Any]) -> Any:
    """从字典构建 dataclass 实例，只取 dataclass 中定义的字段。"""
    import dataclasses

    if not isinstance(data, dict):
        data = {}
    valid_fields = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in valid_fields})


def validate_config(config: AppConfig) -> List[str]:
    """校验 AppConfig 合法性，返回警告列表；非法时抛出 ConfigValidationError。

    Args:
        config: 待校验的 AppConfig 实例。

    Returns:
        警告消息列表（不阻塞启动）。

    Raises:
        ConfigValidationError: 必填字段缺失或值非法。
    """
    errors: List[str] = []
    warnings: List[str] = []

    # platform 枚举
    if config.platform not in ("windows", "linux"):
        errors.append(
            f"platform 必须为 'windows' 或 'linux'，当前值: {config.platform!r}"
        )

    # pipeline_mode 枚举
    if config.pipeline_mode not in ("full", "oj_only", "oj_rp"):
        errors.append(
            "pipeline_mode 必须为 'full'、'oj_only' 或 'oj_rp'，"
            f"当前值: {config.pipeline_mode!r}"
        )

    # environment.mode 枚举
    if config.environment.mode not in ("windows_wsl", "linux"):
        errors.append(
            f"environment.mode 必须为 'windows_wsl' 或 'linux'，"
            f"当前值: {config.environment.mode!r}"
        )

    # paths.workspace_dir 必填
    if not config.paths.workspace_dir:
        errors.append("paths.workspace_dir 不能为空")

    # database.password
    if not config.database.password:
        errors.append("database.password 不能为空")
    elif config.database.password == "your_password":
        errors.append("database.password 不能为占位值 'your_password'")

    # retry.*_max >= 1
    for fname in (
        "task_a_max_rounds",
        "task_a_logic_max",
        "task_a_syntax_consecutive_max",
        "task_a_syntax_total_max",
        "pbt_pass_count_max",
        "oj_pass_count_max",
        "input_gen_max_per_a_round",
        "input_gen_total_max",
        "a_round_max",
        "source_fidelity_max",
        "rac_error_pattern_max",
        "rac_oj_bug_total_max",
    ):
        val = getattr(config.retry, fname)
        if val < 1:
            errors.append(f"retry.{fname} 必须 >= 1，当前值: {val}")

    # workers.* >= 0; 每个 pipeline_mode 只强制需要的 stage worker >= 1。
    for fname in ("task_a", "openjml_esc", "rac", "input_gen", "pbt", "mt", "final"):
        val = getattr(config.workers, fname)
        if val < 0:
            errors.append(f"workers.{fname} 必须 >= 0，当前值: {val}")

    required_workers_by_mode = {
        "oj_only": ("task_a", "openjml_esc", "final"),
        "oj_rp": ("task_a", "openjml_esc", "rac", "input_gen", "pbt", "final"),
        "full": ("task_a", "openjml_esc", "rac", "input_gen", "pbt", "mt", "final"),
    }
    for fname in required_workers_by_mode.get(config.pipeline_mode, ()):
        val = getattr(config.workers, fname)
        if val < 1:
            errors.append(
                f"pipeline_mode={config.pipeline_mode!r} 时 workers.{fname} 必须 >= 1，"
                f"当前值: {val}"
            )

    # openjml.timeout > 0
    if config.openjml.timeout < 1:
        errors.append(f"openjml.timeout 必须 > 0，当前值: {config.openjml.timeout}")

    # openjml.rac_timeout > 0
    if config.openjml.rac_timeout < 1:
        errors.append(f"openjml.rac_timeout 必须 > 0，当前值: {config.openjml.rac_timeout}")

    # openjml.timeout_threshold_seconds > 0
    if config.openjml.timeout_threshold_seconds < 1:
        errors.append(
            f"openjml.timeout_threshold_seconds 必须 > 0，"
            f"当前值: {config.openjml.timeout_threshold_seconds}"
        )

    # pbt.valid_input_min_ratio in [0, 1]
    if not (0 <= config.pbt.valid_input_min_ratio <= 1):
        errors.append(
            f"pbt.valid_input_min_ratio 必须在 [0, 1]，"
            f"当前值: {config.pbt.valid_input_min_ratio}"
        )

    # pbt.test_runs >= 1
    if config.pbt.test_runs < 1:
        errors.append(f"pbt.test_runs 必须 >= 1，当前值: {config.pbt.test_runs}")

    # llm.max_concurrent_global >= llm.max_concurrent_per_vendor
    if config.llm.max_concurrent_global < config.llm.max_concurrent_per_vendor:
        errors.append(
            f"llm.max_concurrent_global({config.llm.max_concurrent_global}) "
            f"必须 >= llm.max_concurrent_per_vendor"
            f"({config.llm.max_concurrent_per_vendor})"
        )

    # llm.temperature in [0, 2]
    if not (0 <= config.llm.temperature <= 2):
        errors.append(
            f"llm.temperature 必须在 [0, 2]，当前值: {config.llm.temperature}"
        )

    # llm.max_tokens >= 1
    if config.llm.max_tokens < 1:
        errors.append(f"llm.max_tokens 必须 >= 1，当前值: {config.llm.max_tokens}")

    # blacklist.v1 / protected：必须为字符串列表
    for field_name in ("v1", "protected"):
        val = getattr(config.blacklist, field_name)
        if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
            errors.append(
                f"blacklist.{field_name} 必须为字符串列表，当前值: {val!r}"
            )

    # blacklist.protected 与 v1 的交集应为空（v1 中的关键字若也在 protected
    # 中，运行时合并后会被 protected 反向移除，等同于禁用 v1，配置必然是
    # 写错了）
    if (
        isinstance(config.blacklist.v1, list)
        and isinstance(config.blacklist.protected, list)
    ):
        overlap = set(config.blacklist.v1) & set(config.blacklist.protected)
        if overlap:
            errors.append(
                f"blacklist.v1 与 blacklist.protected 不应有交集，"
                f"重叠关键字: {sorted(overlap)}"
            )

    # mt.jdk11_javac / mt.major_home：仅 mt.enabled=true 时强制非空
    # （Major 3.0.1 仅支持 JDK 11，必须显式指定路径；gotchas §2.1）
    if config.mt.enabled:
        if not config.mt.jdk11_javac:
            errors.append(
                "mt.jdk11_javac 不能为空（mt.enabled=true 时必须显式指定 "
                "JDK 11 javac 路径，Major 3.0.1 不兼容 JDK 21）"
            )
        if not config.mt.major_home:
            errors.append(
                "mt.major_home 不能为空（mt.enabled=true 时必须指定 Major "
                "安装目录）"
            )

    # error_log.* >= 1 / >= 100
    for fname in ("openjml_max_lines", "rac_max_lines", "compile_error_max_lines"):
        val = getattr(config.error_log, fname)
        if val < 1:
            errors.append(f"error_log.{fname} 必须 >= 1，当前值: {val}")
    if config.error_log.failure_sample_max_chars < 100:
        errors.append(
            f"error_log.failure_sample_max_chars 必须 >= 100，"
            f"当前值: {config.error_log.failure_sample_max_chars}"
        )

    # database.pool_size 建议 >= workers 总和（WARNING）
    workers_total = (
        config.workers.task_a
        + config.workers.openjml_esc
        + config.workers.rac
        + config.workers.input_gen
        + config.workers.pbt
        + config.workers.mt
        + config.workers.final
    )
    if config.database.pool_size < workers_total:
        warnings.append(
            f"database.pool_size({config.database.pool_size}) "
            f"建议 >= workers 总和({workers_total})"
        )

    if errors:
        raise ConfigValidationError(
            "配置校验失败:\n" + "\n".join(f"  - {e}" for e in errors)
        )

    return warnings

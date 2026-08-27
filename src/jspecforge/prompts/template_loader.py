"""提示词模板加载器与渲染引擎。

**设计要点**：

1. 模板用 ``{variable_name}`` 占位（与顾问初稿和 design §5.2 速查表一致），
   但**不调** Python 内置 :py:meth:`str.format`。原因：变量值常含 Java 源码
   片段（``{ return 0; }`` 这种花括号），:py:meth:`str.format` 会把字面 ``{``
   误当作二次占位符并抛 ``KeyError``。
2. 替换器用 :py:func:`re.sub` 单次扫描：模式 ``\\{(\\w+)\\}`` 仅命中合法标识
   符占位符；Java 代码里的 ``{ ... }`` 因含空格/分号/字面值不匹配 ``\\w+``，
   原样保留。
3. **启动时一次性加载 + lint** 全部 10 个模板。Lint 比对模板实际占位符
   与 :data:`variables.TEMPLATE_SPECS` 声明，任意不一致 → 一次性 raise
   :class:`TemplateLintError`。
4. **UTF-8 with BOM 兼容**：用 ``encoding="utf-8-sig"`` 读模板文件，匹配
   ``vendors.csv`` 同样的兼容策略（gotchas §13.1.1）。

**对外 API**：

- 显式实例：:class:`TemplateLoader`（推荐用于测试 + 多实例场景）
- 模块级便捷：:func:`init_default_loader` + :func:`render`（推荐用于 P6
  处理器主流程）

**P6 处理器标准用法**::

    # main.py 启动时（只调一次）
    init_default_loader(Path(config.paths.prompts_dir))

    # P6 处理器
    from jspecforge.prompts import render
    user_text = render("A_U_Fail_OJ", oj_error=oj_stdout)
    sess.append_user(user_text)

**测试用法**::

    loader = TemplateLoader(tmp_path, lint_on_load=False)  # 禁 lint 测错路径
    rendered = loader.render("A_S")
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Set

from jspecforge.prompts.exceptions import (
    MissingVariableError,
    TemplateLintError,
    TemplateLoadError,
    TemplateNotFoundError,
)
from jspecforge.prompts.variables import (
    TEMPLATE_SPECS,
    TemplateSpec,
    validate_render_kwargs,
)
from jspecforge.utils.log_utils import get_logger

_logger = get_logger("prompts.template_loader")


# ---------------------------------------------------------------------------
# 占位符正则
# ---------------------------------------------------------------------------

#: 占位符模式：``{合法 Python 标识符}``。
#:
#: 关键约束：``\w+`` 在 Python 默认（不带 re.UNICODE 之外的标志）下匹配
#: ``[A-Za-z0-9_]+``，**不**匹配空格、分号、点号、运算符。所以 Java 源码里
#: 的 ``{ return 0; }`` / ``{x.y}`` / ``{a + b}`` 都不会被误命中。
#:
#: 一个潜在边界：模板里如果写了 ``{0}`` / ``{42}`` 这种纯数字，**会**被命中。
#: 但 Python 标识符不能以数字开头——variables.py 中所有变量名都是合法标识
#: 符——所以纯数字占位符在 lint 阶段会被报"未声明变量"，安全暴露。
_PLACEHOLDER_PATTERN = re.compile(r"\{(\w+)\}")


def extract_placeholders(template_text: str) -> Set[str]:
    """从模板文本中提取所有 ``{var}`` 占位符的变量名。

    Args:
        template_text: 模板正文。

    Returns:
        变量名集合（去重）。空集表示模板无占位符（如 A_S / A_U_PBT_Pass）。
    """
    return set(_PLACEHOLDER_PATTERN.findall(template_text))


# ---------------------------------------------------------------------------
# 加载器
# ---------------------------------------------------------------------------


class TemplateLoader:
    """模板加载与渲染引擎。

    构造时一次性把 :data:`TEMPLATE_SPECS` 中全部模板从 ``prompts_dir`` 加载
    到内存。``lint_on_load=True`` 时还会校验占位符与声明一致。

    线程安全：构造后只读，:meth:`render` 不修改实例状态——多线程并发渲染
    安全。
    """

    def __init__(
        self,
        prompts_dir: Path | str,
        *,
        lint_on_load: bool = True,
    ) -> None:
        """初始化并加载全部模板。

        Args:
            prompts_dir: 模板根目录（应是 ``config.paths.prompts_dir``）。
                内部按 ``{prompts_dir}/{session_type}/{template_id}.txt``
                定位每个模板。
            lint_on_load: ``True`` 时启动 lint，模板占位符与
                :data:`TEMPLATE_SPECS` 声明不一致直接报错；测试场景可设
                ``False`` 跳过。

        Raises:
            TemplateLoadError: 任一模板文件读取失败。
            TemplateLintError: ``lint_on_load=True`` 且任一模板的占位符
                与声明不一致。
        """
        self._prompts_dir: Path = Path(prompts_dir)
        self._templates: Dict[str, str] = {}
        self._load_all()
        if lint_on_load:
            self._lint_all()
        _logger.info(
            "TemplateLoader: 加载 %d 个模板自 %s (lint=%s)",
            len(self._templates),
            self._prompts_dir,
            lint_on_load,
        )

    # ------------------------------------------------------------------
    # 只读属性
    # ------------------------------------------------------------------

    @property
    def prompts_dir(self) -> Path:
        return self._prompts_dir

    def known_template_ids(self) -> Set[str]:
        """返回当前已加载的模板 ID 集合（深拷贝）。"""
        return set(self._templates.keys())

    def get_raw(self, template_id: str) -> str:
        """获取模板原文（不做任何替换）。

        Raises:
            TemplateNotFoundError: 未注册或未加载成功的模板 ID。
        """
        if template_id not in self._templates:
            raise TemplateNotFoundError(template_id, self._templates.keys())
        return self._templates[template_id]

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------

    def render(self, template_id: str, /, **kwargs: object) -> str:
        """按 ``{var}`` 占位符替换变量，返回最终 prompt 字符串。

        替换规则：

        1. 校验 ``kwargs`` 包含 :class:`TemplateSpec.required_vars` 中全部
           变量；缺失任意一个 → :class:`MissingVariableError`。
        2. 多余 ``kwargs``（``required_vars`` 外的）→ ``log.warning`` 但
           **不**抛错；这些值在最终字符串中无引用即可。
        3. 用 :py:func:`re.sub` 单次扫描替换：``{name}`` → ``str(kwargs[name])``。
           变量值含字面 ``{`` ``}`` 时**不会**被二次解析（pattern ``\\w+``
           不匹配空格/分号/字面值，原样保留）。

        Args:
            template_id: 模板 ID（必须已注册）。
            **kwargs: 变量值，键名必须与 :class:`TemplateSpec.required_vars`
                匹配（多余键允许，但会触发 warning）。值会通过 :py:func:`str`
                转字符串——调用方负责把 dataclass / list 等结构化数据预先
                格式化（例如 ``failure_samples`` 应传已格式化好的字符串）。

        Returns:
            最终 prompt 字符串，可直接交给 :meth:`LLMSession.append_user`
            或 :meth:`LLMSession.append_system`。

        Raises:
            TemplateNotFoundError: 未注册的模板 ID。
            MissingVariableError: 必填变量缺失。
        """
        # 同时校验 ID 已注册（间接通过 validate_render_kwargs）+ 已加载
        if template_id not in self._templates:
            raise TemplateNotFoundError(
                template_id, self._templates.keys()
            )
        extra = validate_render_kwargs(template_id, kwargs)
        if extra:
            _logger.warning(
                "render(%s): 忽略未声明的多余变量 %s",
                template_id,
                sorted(extra),
            )

        template_text = self._templates[template_id]
        return _PLACEHOLDER_PATTERN.sub(
            lambda m: str(kwargs[m.group(1)]),
            template_text,
        )

    # ------------------------------------------------------------------
    # 内部：加载 + lint
    # ------------------------------------------------------------------

    def _load_all(self) -> None:
        """从磁盘读取全部 10 个模板。失败立即 raise。"""
        for template_id, spec in TEMPLATE_SPECS.items():
            path = self._prompts_dir / spec.relative_path
            if not path.is_file():
                raise TemplateLoadError(
                    template_id=template_id,
                    path=str(path),
                    cause="file not found",
                )
            try:
                # utf-8-sig 兼容 BOM；普通 UTF-8 文件也能正确读取
                self._templates[template_id] = path.read_text(
                    encoding="utf-8-sig"
                )
            except OSError as exc:
                raise TemplateLoadError(
                    template_id=template_id,
                    path=str(path),
                    cause=f"OSError: {exc}",
                ) from exc
            except UnicodeDecodeError as exc:
                raise TemplateLoadError(
                    template_id=template_id,
                    path=str(path),
                    cause=f"non UTF-8 content: {exc}",
                ) from exc

    def _lint_all(self) -> None:
        """校验已加载模板的占位符与 :data:`TEMPLATE_SPECS` 声明一致。"""
        errors: List[str] = []
        for template_id, spec in TEMPLATE_SPECS.items():
            text = self._templates[template_id]
            actual = extract_placeholders(text)
            declared = set(spec.required_vars)
            extra_in_template = actual - declared
            missing_in_template = declared - actual
            if extra_in_template:
                errors.append(
                    f"{template_id}: 模板含未在 TEMPLATE_SPECS 声明的占位符 "
                    f"{sorted(extra_in_template)}"
                )
            if missing_in_template:
                errors.append(
                    f"{template_id}: TEMPLATE_SPECS 声明的变量未在模板中"
                    f"出现 {sorted(missing_in_template)}"
                )
        if errors:
            raise TemplateLintError(errors)


# ---------------------------------------------------------------------------
# 模块级默认实例（便捷接口）
# ---------------------------------------------------------------------------

_default_loader: Optional[TemplateLoader] = None
_default_loader_lock = threading.Lock()


def init_default_loader(
    prompts_dir: Path | str,
    *,
    lint_on_load: bool = True,
) -> TemplateLoader:
    """绑定模块级默认 :class:`TemplateLoader`，供 :func:`render` 使用。

    应在程序启动早期调用一次（如 ``main.py`` 加载完 :class:`AppConfig` 后），
    传入 ``config.paths.prompts_dir``。重复调用会**替换**之前的实例（线程
    安全），便于测试用例反复重置。

    Args:
        prompts_dir: 模板根目录。
        lint_on_load: 是否启动时 lint。

    Returns:
        新创建的 loader 实例。
    """
    global _default_loader
    loader = TemplateLoader(prompts_dir, lint_on_load=lint_on_load)
    with _default_loader_lock:
        _default_loader = loader
    return loader


def reset_default_loader() -> None:
    """清空模块级 loader（仅测试用）。"""
    global _default_loader
    with _default_loader_lock:
        _default_loader = None


def get_default_loader() -> TemplateLoader:
    """返回模块级 loader；未初始化时 raise。"""
    if _default_loader is None:
        raise RuntimeError(
            "Default TemplateLoader 未初始化——请在程序启动时调用 "
            "init_default_loader(prompts_dir)"
        )
    return _default_loader


def render(template_id: str, /, **kwargs: object) -> str:
    """模块级便捷渲染：委托给默认 loader。

    Raises:
        RuntimeError: 默认 loader 未初始化。
        TemplateNotFoundError / MissingVariableError: 同
            :meth:`TemplateLoader.render`。
    """
    return get_default_loader().render(template_id, **kwargs)


__all__ = [
    "TemplateLoader",
    "extract_placeholders",
    "init_default_loader",
    "reset_default_loader",
    "get_default_loader",
    "render",
]

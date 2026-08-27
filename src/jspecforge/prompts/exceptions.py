"""提示词引擎自定义异常。

按 CLAUDE.md §4.1：自定义异常放在各模块的 ``exceptions.py``。所有异常都
继承 :class:`PromptError` 顶层基类，便于上层一次性 ``except PromptError``
做兜底兼容（生产代码应优先用具体子类）。

错误信息原则：
- 始终包含模板 ID 与具体冲突点（变量名 / 文件路径），不要给出"模板出错"
  这种泛泛错误信息
- 错误消息使用英文 + 中文混合是因为日志会被人类直接看；变量名/文件名等
  机器可读字段保持英文

PromptError → TemplateNotFoundError / MissingVariableError /
              TemplateLintError / TemplateLoadError
"""

from __future__ import annotations

from typing import Iterable, List


class PromptError(Exception):
    """提示词引擎的所有自定义异常基类。

    上层 ``except PromptError`` 可一次捕获本模块全部错误。
    """


class TemplateNotFoundError(PromptError):
    """请求的模板 ID 不存在。

    场景：``render("UNKNOWN_ID", ...)`` 或 ``loader.get("XYZ")``。
    错误消息附带已注册的模板 ID 列表，方便排查拼写错误。
    """

    def __init__(self, template_id: str, known_ids: Iterable[str]) -> None:
        self.template_id = template_id
        self.known_ids: List[str] = sorted(set(known_ids))
        msg = (
            f"Template not found: {template_id!r}. "
            f"Known templates: {self.known_ids}"
        )
        super().__init__(msg)


class MissingVariableError(PromptError):
    """渲染时必需变量未提供。

    错误消息列出全部缺失变量与该模板的完整变量清单，**不要**只报第一个
    缺失项——上层往往是批量 mapping 缺多个值。
    """

    def __init__(
        self,
        template_id: str,
        missing: Iterable[str],
        required: Iterable[str],
    ) -> None:
        self.template_id = template_id
        self.missing: List[str] = sorted(set(missing))
        self.required: List[str] = sorted(set(required))
        msg = (
            f"Template {template_id!r} missing required variable(s): "
            f"{self.missing}. Required: {self.required}"
        )
        super().__init__(msg)


class TemplateLintError(PromptError):
    """启动时 lint 失败：模板文件中的占位符与 :mod:`variables` 声明不一致。

    场景：模板里写了 ``{some_var}`` 但 :data:`variables.TEMPLATE_SPECS` 没
    在 ``required_vars`` 中声明它，或反过来。Lint 在加载阶段做一次性扫描
    后立刻报错，避免运行时才暴露。

    错误消息合并所有出问题的模板，不要逐个 raise——一次给出全部 lint 错误
    更便于人类批量修。
    """

    def __init__(self, errors: Iterable[str]) -> None:
        self.errors: List[str] = list(errors)
        joined = "\n  - ".join(self.errors)
        super().__init__(
            f"Prompt template lint failed ({len(self.errors)} issue(s)):\n  - {joined}"
        )


class TemplateLoadError(PromptError):
    """模板文件 IO 错误（编码错误、磁盘读取失败等）。

    与 :class:`TemplateNotFoundError` 区别：前者是"路径下根本没这个文件"，
    本异常是"文件存在但读不出来"（权限/编码/被锁等）。
    """

    def __init__(self, template_id: str, path: str, cause: str) -> None:
        self.template_id = template_id
        self.path = path
        self.cause = cause
        super().__init__(
            f"Failed to load template {template_id!r} from {path}: {cause}"
        )

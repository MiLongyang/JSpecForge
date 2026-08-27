"""提示词引擎模块。

公开 API（按依赖顺序）：

- :mod:`exceptions`：自定义异常基类与子类
- :mod:`variables`：10 个模板的变量 schema 与校验
- :mod:`template_loader`：加载器 + ``{var}`` 单次替换 + 启动时 lint

**使用边界**：

- P5 只负责"模板 + 变量字典 → 字符串"。LLM 调用、消息持久化、JML 提取
  都是 P6/P7 的事，本模块**不依赖** :mod:`jspecforge.llm` 与
  :mod:`jspecforge.db`
- P6 处理器标准用法：``init_default_loader(config.paths.prompts_dir)``
  一次 → 后续 ``render("A_U_Fail_OJ", oj_error=...)`` → 把字符串交给
  :class:`LLMSession.append_user`

**整套依赖图**（``A → B`` 表示 A import B）::

    template_loader → variables + exceptions + utils.log_utils
    variables       → exceptions

不依赖：``llm`` / ``db`` / ``processors``。
"""

from jspecforge.prompts.exceptions import (
    MissingVariableError,
    PromptError,
    TemplateLintError,
    TemplateLoadError,
    TemplateNotFoundError,
)
from jspecforge.prompts.template_loader import (
    TemplateLoader,
    extract_placeholders,
    get_default_loader,
    init_default_loader,
    render,
    reset_default_loader,
)
from jspecforge.prompts.variables import (
    ROLE_SYSTEM,
    ROLE_USER,
    SESSION_INPUT_GEN,
    SESSION_TASK_A,
    TEMPLATE_SPECS,
    TemplateSpec,
    all_template_ids,
    get_spec,
    validate_render_kwargs,
)

__all__ = [
    # exceptions
    "PromptError",
    "TemplateNotFoundError",
    "MissingVariableError",
    "TemplateLintError",
    "TemplateLoadError",
    # variables
    "TemplateSpec",
    "TEMPLATE_SPECS",
    "ROLE_SYSTEM",
    "ROLE_USER",
    "SESSION_TASK_A",
    "SESSION_INPUT_GEN",
    "get_spec",
    "all_template_ids",
    "validate_render_kwargs",
    # template_loader
    "TemplateLoader",
    "extract_placeholders",
    "init_default_loader",
    "reset_default_loader",
    "get_default_loader",
    "render",
]

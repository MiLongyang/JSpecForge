"""提示词模板变量 schema。

定义 10 个模板的元数据（session_type、role、必填变量集合），是
:mod:`template_loader` 启动时 lint 与运行时变量校验的"事实之源"。

Schema 来源（按优先级）：

1. ``docs/design/提示词模板与LLM交互规范.md`` §一 表（变量列）+ §5.2 占位符
   速查表
2. ``docs/prompts/提示词模板初稿-10个模板合集.md``（顾问初稿，逐模板列出
   "变量"字段）

两份文档对 10 个模板的变量列表完全一致——本模块也是。

**Schema 一旦改动，必须同步**：

- 对应 ``.txt`` 模板文件
- design 文档表格 + 速查表
- 顾问初稿（``docs/prompts/...``）
- 本模块 TEMPLATE_SPECS

启动时 lint 会比对模板文件实际占位符与 ``required_vars``，不一致直接
:class:`TemplateLintError`，所以改 schema 后忘改模板（或反之）会立刻暴露。

**为什么用 ``frozenset`` 而不是 ``list``**：

- 变量名是无序无重的集合语义
- frozenset 可哈希、不可变，与 ``@dataclass(frozen=True)`` 配合自然
- 校验时做 ``provided - required`` / ``required - provided`` 集合运算更简洁
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, Mapping, Set

from jspecforge.prompts.exceptions import (
    MissingVariableError,
    TemplateNotFoundError,
)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 会话类型枚举值（与 :class:`jspecforge.db.enums.SessionType` 对齐，但本
#: 模块不直接依赖 db 层——避免 prompts → db 的反向依赖）
SESSION_TASK_A = "task_a"
SESSION_INPUT_GEN = "input_gen"

#: 角色枚举值（与 :class:`jspecforge.db.enums.MessageRole` 对齐）
ROLE_SYSTEM = "system"
ROLE_USER = "user"


# ---------------------------------------------------------------------------
# 模板规格
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TemplateSpec:
    """单个模板的元数据。

    Attributes:
        template_id: 模板 ID（如 ``"A_U_Fail_OJ"``）。
        session_type: ``"task_a"`` / ``"input_gen"``。决定模板文件落在
            ``{prompts_dir}/{session_type}/{template_id}.txt``。
        role: ``"system"`` / ``"user"``。决定 :class:`LLMSession` 应当用
            ``append_system`` 还是 ``append_user``。
        required_vars: 必填变量名集合。渲染时缺任何一个都会
            :class:`MissingVariableError`。
        description: 触发场景说明（与 design §一 表一致）。
    """

    template_id: str
    session_type: str
    role: str
    required_vars: FrozenSet[str]
    description: str

    @property
    def relative_path(self) -> str:
        """模板文件相对 ``prompts_dir`` 的路径，如 ``"task_a/A_S.txt"``。"""
        return f"{self.session_type}/{self.template_id}.txt"


# ---------------------------------------------------------------------------
# 10 个模板的注册表
# ---------------------------------------------------------------------------

#: 全部 10 个模板的规格表。键 = template_id。
#:
#: 排序：先 task_a 6 个，再 input_gen 4 个；同会话内按 design §一 表的顺序。
TEMPLATE_SPECS: Dict[str, TemplateSpec] = {
    # ---------------- Task A 会话（6 个）----------------
    "A_S": TemplateSpec(
        template_id="A_S",
        session_type=SESSION_TASK_A,
        role=ROLE_SYSTEM,
        required_vars=frozenset(),
        description=(
            "Task A 首次创建会话时，固定在 messages[0]，全程不变。"
            "包含 OpenJML/RAC 版本信息与 JML 黑名单语法警告。"
        ),
    ),
    "A_U_First": TemplateSpec(
        template_id="A_U_First",
        session_type=SESSION_TASK_A,
        role=ROLE_USER,
        required_vars=frozenset(
            # E.2.1 (2026-05-03): 删除 method_code, 改用 full_java_code 单一字段
            # (Complete Java Class 已经包含目标方法,Target Method 是冗余)
            {"nl_description", "full_java_code"}
        ),
        description="首次生成 JML（a_round=0）, 填入 NL 描述 + 完整 Java 类代码。",
    ),
    "A_U_Fail_OJ": TemplateSpec(
        template_id="A_U_Fail_OJ",
        session_type=SESSION_TASK_A,
        role=ROLE_USER,
        required_vars=frozenset({"oj_error"}),
        description=(
            "OpenJML ESC 真正逻辑错误（LOGIC_ERROR：exit=6 + 输出无 timeout/"
            "unsupported 关键词 + 耗时未触兜底）。填入 OJ 报错信息。"
        ),
    ),
    "A_U_Fail_Syntax": TemplateSpec(
        template_id="A_U_Fail_Syntax",
        session_type=SESSION_TASK_A,
        role=ROLE_USER,
        required_vars=frozenset({"syntax_error"}),
        description=(
            "OpenJML 语法错误（退出码 1）或 RAC 编译失败。填入语法错误信息"
            "（OJ 或 RAC 的报错输出，截断至对应 max_lines）。"
        ),
    ),
    "A_U_Fail_PostCondition": TemplateSpec(
        template_id="A_U_Fail_PostCondition",
        session_type=SESSION_TASK_A,
        role=ROLE_USER,
        required_vars=frozenset({"failure_samples", "oj_error_block"}),
        description=(
            "动态测试存在 postcondition 失败。填入按 JML 子句分类的失败"
            "样本（含输入值、返回值、RAC 断言报错），每类最多 "
            "pbt.failure_sample_max 个样本。"
        ),
    ),
    "A_U_PBT_Pass": TemplateSpec(
        template_id="A_U_PBT_Pass",
        session_type=SESSION_TASK_A,
        role=ROLE_USER,
        required_vars=frozenset({"oj_error_block"}),
        description=(
            "PBT 动态测试 100% 通过但 OJ/ESC 未通过。附带真实 OJ 失败原因"
            "（oj_error_block），请求重写规约以通过 ESC。"
        ),
    ),
    "A_U_Fail_InsufficientInput": TemplateSpec(
        template_id="A_U_Fail_InsufficientInput",
        session_type=SESSION_TASK_A,
        role=ROLE_USER,
        required_vars=frozenset(
            {"valid_input_stats", "precondition_samples", "oj_error_block"}
        ),
        description=(
            "Phase 4 (2026-05-13) 新增: PBT 有效输入不足回喂 task_a。"
            "填入 PBT 统计数据 + precondition 违反样本, 提示 LLM 检查前置"
            "条件是否写得过严, 或确认是题目值域自然稀疏 (可保持不变)。"
        ),
    ),
    "A_U_Fail_SourceFidelity": TemplateSpec(
        template_id="A_U_Fail_SourceFidelity",
        session_type=SESSION_TASK_A,
        role=ROLE_USER,
        required_vars=frozenset({"source_error", "full_java_code"}),
        description=(
            "Task A source fidelity failure: the model modified the original "
            "Java method source. Feed back the original Java code and ask it "
            "to regenerate JML only."
        ),
    ),
    "A_U_MT_Feedback": TemplateSpec(
        template_id="A_U_MT_Feedback",
        session_type=SESSION_TASK_A,
        role=ROLE_USER,
        required_vars=frozenset({
            "mt_path", "mt_score", "mt_killed",
            "mt_survived", "mt_total", "survived_summary",
            "oj_error_block",
        }),
        description=(
            "P11 MT 反馈: 变异测试分数未达标, 存活变异体摘要回喂 task_a, "
            "提示 LLM 加强 JML 规约以杀死存活变异体。"
        ),
    ),
    # ---------------- Input Gen 会话（4 个）----------------
    "IG_S": TemplateSpec(
        template_id="IG_S",
        session_type=SESSION_INPUT_GEN,
        role=ROLE_SYSTEM,
        required_vars=frozenset(),
        description="输入生成器首次创建会话时，固定在 messages[0]。",
    ),
    "IG_U_First": TemplateSpec(
        template_id="IG_U_First",
        session_type=SESSION_INPUT_GEN,
        role=ROLE_USER,
        required_vars=frozenset(
            {"nl_description", "jml_annotated_code", "test_driver_template"}
        ),
        description=(
            "首次生成输入生成器代码。填入带 JML 注释的完整 Java 源码 + "
            "自然语言需求描述 + 测试驱动模板的框架代码。"
        ),
    ),
    "IG_U_Fail_Compile": TemplateSpec(
        template_id="IG_U_Fail_Compile",
        session_type=SESSION_INPUT_GEN,
        role=ROLE_USER,
        required_vars=frozenset({"compile_error"}),
        description="输入生成器代码 javac 编译失败。填入 javac 编译错误信息。",
    ),
    "IG_U_Fail_ValidInput": TemplateSpec(
        template_id="IG_U_Fail_ValidInput",
        session_type=SESSION_INPUT_GEN,
        role=ROLE_USER,
        required_vars=frozenset({"valid_input_stats", "precondition_samples"}),
        description=(
            "动态测试有效输入不足（有效率 < pbt.valid_input_min_ratio 且无 "
            "postcondition 失败）。填入统计数据 + precondition 违反样本。"
        ),
    ),
}


# ---------------------------------------------------------------------------
# 查询接口
# ---------------------------------------------------------------------------


def get_spec(template_id: str) -> TemplateSpec:
    """按 ID 查规格。

    Args:
        template_id: 模板 ID。

    Returns:
        TemplateSpec 实例。

    Raises:
        TemplateNotFoundError: 未注册的模板 ID。
    """
    spec = TEMPLATE_SPECS.get(template_id)
    if spec is None:
        raise TemplateNotFoundError(template_id, TEMPLATE_SPECS.keys())
    return spec


def all_template_ids() -> Set[str]:
    """全部已注册模板 ID 集合（深拷贝，调用方可自由 mutate）。"""
    return set(TEMPLATE_SPECS.keys())


# ---------------------------------------------------------------------------
# 渲染时校验
# ---------------------------------------------------------------------------


def validate_render_kwargs(
    template_id: str,
    provided: Mapping[str, object],
) -> Set[str]:
    """渲染前校验：必填变量是否齐备。

    Args:
        template_id: 模板 ID。必须已注册，否则 :class:`TemplateNotFoundError`。
        provided: 调用方传入的变量字典。值类型不做约束（渲染时统一 ``str()``）。

    Returns:
        多余变量名集合（``provided`` 有但 ``required_vars`` 没有的）。调用方
        可据此 ``log.warning`` 但**不应 raise**——多余变量是常见的"传错了
        但不致命"情况。

    Raises:
        TemplateNotFoundError: ``template_id`` 未注册。
        MissingVariableError: 缺失必填变量。
    """
    spec = get_spec(template_id)
    provided_keys = set(provided.keys())
    missing = spec.required_vars - provided_keys
    if missing:
        raise MissingVariableError(
            template_id=template_id,
            missing=missing,
            required=spec.required_vars,
        )
    return provided_keys - spec.required_vars

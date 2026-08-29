"""TestDriver 模板注入器（P6e PBT 处理器使用）。

设计依据：

- ``docs/design/外部工具调用技术规格.md`` §5.6 / §5.8（模板注入流程）
- ``docs/plan/P6-处理器.md`` plan §3.5 / §三 P6e

**职责**：

1. 解析目标方法签名（``method_signature`` / ``method_code``）拿到方法名 +
   参数类型列表
2. 用上述信息拼出 ``PLACEHOLDER_METHOD_CALL`` 的 Java 代码（如
   ``Solution.add((Integer) input[0], (Integer) input[1]);``）
3. 读 ``templates/TestDriverTemplate.java`` 模板内容
4. 替换 3 个占位符：``__CLASS_NAME__`` / ``PLACEHOLDER_INPUT_GENERATOR``
   / ``PLACEHOLDER_METHOD_CALL``
5. 写到 ``{java_source_dir}/TestDriver_{task_id}_{a_round}.java``

**注入器是纯字符串变换**——不依赖 javac/JDK，可独立单测。

**类型 cast 策略**（plan §3.5 简化）：

- ``int`` / ``long`` / ``double`` / ``float`` / ``boolean`` / ``char`` /
  ``byte`` / ``short`` → ``(WrapperType) input[i]``
  （Java 编译器自动拆箱到 primitive，便于调原方法签名）
- 数组类型（含基本类型数组与对象数组）→ ``(int[]) input[i]`` 等直接 cast
- 引用类型（``String`` / ``List`` / 自定义类）→ ``(String) input[i]``

**方法签名解析的限制**：

- 仅支持单个方法签名（不处理重载选择）
- 不支持泛型类型参数（``List<Integer>``）—— 直接当作裸类型 ``List``
  注：测试集 method_code 通常是简化签名，不含复杂泛型；如果将来需要再扩展
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from jspecforge.utils.log_utils import get_logger

_logger = get_logger("tools.test_driver_builder")

# Java 8 个 primitive 类型 → wrapper 映射（Java 编译器自动拆箱）
_PRIMITIVE_TO_WRAPPER = {
    "int": "Integer",
    "long": "Long",
    "double": "Double",
    "float": "Float",
    "boolean": "Boolean",
    "char": "Character",
    "byte": "Byte",
    "short": "Short",
}

_PRIMITIVE_ARRAY_ADAPTERS = {
    "int[]": "__jsf_asIntArray",
    "long[]": "__jsf_asLongArray",
    "double[]": "__jsf_asDoubleArray",
    "float[]": "__jsf_asFloatArray",
    "boolean[]": "__jsf_asBooleanArray",
    "char[]": "__jsf_asCharArray",
    "byte[]": "__jsf_asByteArray",
    "short[]": "__jsf_asShortArray",
    "int[][]": "__jsf_asInt2DArray",
    "long[][]": "__jsf_asLong2DArray",
    "double[][]": "__jsf_asDouble2DArray",
    "float[][]": "__jsf_asFloat2DArray",
    "boolean[][]": "__jsf_asBoolean2DArray",
    "char[][]": "__jsf_asChar2DArray",
    "byte[][]": "__jsf_asByte2DArray",
    "short[][]": "__jsf_asShort2DArray",
}

# 占位符常量
_PH_CLASS_NAME = "__CLASS_NAME__"
_PH_INPUT_GENERATOR = "PLACEHOLDER_INPUT_GENERATOR"
_PH_METHOD_CALL = "PLACEHOLDER_METHOD_CALL"


def _strip_template_comment_header(template_text: str) -> str:
    """删掉模板开头的占位符描述注释（前几行含 ``__CLASS_NAME__`` 等名称）。

    ``str.replace()`` 是全局替换，注释中的占位符名称也会被替换掉，
    导致 LLM 方法代码被插入到注释行（类外），引发 javac 编译失败。
    删掉注释头后替换只影响真正的占位符位置。
    """
    lines = template_text.split("\n")
    cutoff = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        # 删掉 "// JSpecForge PBT TestDriver 模板" + "// 占位符" +
        # 3 行占位符名称描述 + 空行，共约 6 行
        if stripped.startswith("//") and any(
            ph in stripped for ph in (_PH_CLASS_NAME, _PH_INPUT_GENERATOR, _PH_METHOD_CALL)
        ):
            cutoff = i + 1
        elif cutoff > 0 and stripped == "":
            cutoff = i + 1
            break
        elif cutoff > 0 and not stripped.startswith("//"):
            break
    if cutoff > 0:
        return "\n".join(lines[cutoff:])
    return template_text


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MethodSignature:
    """目标方法签名解析结果。

    Attributes:
        method_name: 方法名（如 ``"add"``）。
        return_type: 返回类型（如 ``"int"``、``"void"``）。
        param_types: 参数类型列表（按顺序，如 ``["int", "int"]``、
            ``["int[]", "int"]``）。
        is_static: 是否 ``static``（决定调用方式：``ClassName.method`` vs
            ``new ClassName().method``）。
    """

    method_name: str
    return_type: str
    param_types: List[str]
    is_static: bool


@dataclass(frozen=True)
class BuildResult:
    """注入结果。"""

    java_path: Path
    class_name: str
    target_method: MethodSignature
    method_call_code: str


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def build_test_driver(
    *,
    task_id: int,
    a_round: int,
    target_class_name: str,
    method_signature_text: str,
    input_generator_code: str,
    template_path: Path,
    output_dir: Path,
) -> BuildResult:
    """生成完整 TestDriver_*.java 文件。

    Args:
        task_id: 任务 ID（用于类名）。
        a_round: Task A 轮次（0-based，用于类名）。
        target_class_name: 目标 Java 类名（如 ``"Solution"``）。
        method_signature_text: 方法签名文本，可以是完整方法体也可以是裸签名
            （如 ``"public static int add(int a, int b)"``）。
        input_generator_code: LLM 生成的 ``generateValidInput`` 方法源码。
        template_path: ``TestDriverTemplate.java`` 路径。
        output_dir: 输出目录（自动创建）。

    Returns:
        :class:`BuildResult`。

    Raises:
        FileNotFoundError: 模板文件不存在。
        ValueError: 方法签名无法解析。
    """
    if not template_path.is_file():
        raise FileNotFoundError(
            f"TestDriverTemplate.java 不存在: {template_path}"
        )

    sig = parse_method_signature(method_signature_text)
    if sig is None:
        raise ValueError(
            f"无法从签名文本中解析方法签名: {method_signature_text[:100]!r}"
        )

    method_call_code = build_method_call_code(target_class_name, sig)
    class_name = f"TestDriver_{task_id}_{a_round}"

    template_text = template_path.read_text(encoding="utf-8")
    # 删掉模板注释头（前 5 行含占位符名称，str.replace 会误替换注释中的占位符）
    template_text = _strip_template_comment_header(template_text)
    rendered = (
        template_text
        .replace(_PH_CLASS_NAME, class_name)
        .replace(_PH_INPUT_GENERATOR, input_generator_code.strip())
        .replace(_PH_METHOD_CALL, method_call_code)
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    java_path = output_dir / f"{class_name}.java"
    with java_path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(rendered)

    _logger.info(
        "build_test_driver: task=%d a_round=%d → %s（method=%s, params=%s）",
        task_id,
        a_round,
        java_path,
        sig.method_name,
        sig.param_types,
    )

    return BuildResult(
        java_path=java_path,
        class_name=class_name,
        target_method=sig,
        method_call_code=method_call_code,
    )


# ---------------------------------------------------------------------------
# 解析：方法签名 → MethodSignature
# ---------------------------------------------------------------------------


def parse_method_signature(text: str) -> Optional[MethodSignature]:
    """从 Java 方法签名文本中提取名/返回类型/参数列表。

    支持三种输入：

    1. 完整方法体（``public static int add(int a, int b) { return a + b; }``）
    2. 裸签名（``public static int add(int a, int b)``）
    3. 仅方法名 + 参数（``add(int a, int b)``）—— 此时无修饰符，
       ``is_static`` 默认 ``False``

    Args:
        text: 包含方法签名的 Java 代码文本。

    Returns:
        :class:`MethodSignature`；解析失败返回 ``None``。
    """
    if not text:
        return None

    # 匹配：[modifiers] [returnType] methodName ( params )
    # 修饰符可重复（public static final 等）；返回类型可含 [] 与点号；
    # 方法名是合法 Java 标识符。
    signature_pattern = (
        r"(?P<modifiers>(?:\s*(?:public|private|protected|static|final|abstract|"
        r"synchronized|native|strictfp)\s+)*)"
        r"(?P<return_type>[\w$.<>\[\]]+(?:\s*\[\s*\])*?)\s+"
        r"(?P<method_name>[a-zA-Z_$][\w$]*)\s*"
        r"\(\s*(?P<params>[^)]*)\s*\)"
    )

    # 完整 method_code 可能在目标方法前包含字段初始化和 lambda 调用。
    # 优先要求真正的方法声明位于行首，并在可选 throws 后紧跟方法体。
    full_method_pattern = re.compile(
        r"^[ \t]*"
        + signature_pattern
        + r"\s*(?:throws\s+[\w$.,\s]+)?\s*\{",
        re.MULTILINE,
    )
    pattern = full_method_pattern
    m = pattern.search(text)
    if m is None:
        # 保留对裸签名和“方法名 + 参数”输入的兼容。
        pattern = re.compile(signature_pattern, re.MULTILINE)
        m = pattern.search(text)
    if m is None:
        return None

    modifiers = m.group("modifiers") or ""
    return_type = m.group("return_type").strip()
    method_name = m.group("method_name")
    params_text = m.group("params").strip()

    # P13 fix: 清洗参数列表内的注释（如 "int tonic /* 0=C .. 11=B */"）
    # 移除块注释 /* ... */ 和行注释 // ... (到逗号或结尾)
    params_text = re.sub(r'/\*.*?\*/', '', params_text)  # 移除 /* ... */
    params_text = re.sub(r'//[^,]*', '', params_text)     # 移除 // ... 到逗号前

    # 跳过 Java 关键字（如 if/while/return）
    java_keywords = {"if", "while", "for", "switch", "return", "throw", "new"}
    if method_name in java_keywords:
        # 继续找下一个匹配
        for next_m in pattern.finditer(text, m.end()):
            cand = next_m.group("method_name")
            if cand not in java_keywords:
                modifiers = next_m.group("modifiers") or ""
                return_type = next_m.group("return_type").strip()
                method_name = cand
                params_text = next_m.group("params").strip()
                break
        else:
            return None

    is_static = "static" in modifiers
    param_types = _parse_param_types(params_text)
    return MethodSignature(
        method_name=method_name,
        return_type=return_type,
        param_types=param_types,
        is_static=is_static,
    )


def _split_top_level_commas(s: str) -> List[str]:
    """按顶层逗号切分, 忽略泛型 ``<>`` 内部的逗号。

    修复 PBT TestDriver 泛型强转错位: 参数类型 ``HashMap<String, Integer>``
    含内部逗号, 朴素 ``str.split(",")`` 会把它切成 ``HashMap<String`` 与
    ``Integer>`` 两段, 导致生成的调用语句括号错位、编译失败。
    """
    parts: List[str] = []
    depth = 0
    cur: List[str] = []
    for ch in s:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur))
    return parts


def _parse_param_types(params_text: str) -> List[str]:
    """解析参数列表字符串 → 类型列表。

    Examples:
        >>> _parse_param_types("int x")
        ['int']
        >>> _parse_param_types("int[] arr, int n")
        ['int[]', 'int']
        >>> _parse_param_types("")
        []
        >>> _parse_param_types("String s, Object[] arr")
        ['String', 'Object[]']
    """
    if not params_text or not params_text.strip():
        return []

    # 按顶层逗号切分（忽略泛型 Map<K, V> 内部的逗号）
    parts = [p.strip() for p in _split_top_level_commas(params_text) if p.strip()]
    types: List[str] = []
    for part in parts:
        # 取最后一个空格之前的部分作为类型；然后处理类型与变量名间的 [] 后缀
        # 如 "int[] arr" → ["int[]", "arr"]；"int arr[]" → ["int", "arr[]"]
        # 我们规范化为 "int[]"（类型后缀 []）
        tokens = part.split()
        if not tokens:
            continue
        # 最后一项是变量名（可能含 [] 后缀，如 ``int x[]``）
        var_name = tokens[-1]
        type_tokens = tokens[:-1]
        if not type_tokens:
            # 没有类型，直接整个当作类型（不规范但不抛错）
            type_str = part.strip()
        else:
            type_str = " ".join(type_tokens)
            # 把 var_name 后缀的 [] 移到 type
            arr_count = var_name.count("[]")
            if arr_count > 0:
                type_str = type_str + ("[]" * arr_count)
        types.append(_normalize_type(type_str))
    return types


def _normalize_type(t: str) -> str:
    """规范化类型字符串：去 ``final``、压缩多个空格、去掉变量名 ``[]`` 已合并的后缀。"""
    t = t.strip()
    # 去 final
    t = re.sub(r"\bfinal\b\s*", "", t).strip()
    # 把 ``int [ ]`` → ``int[]``（去内部空格）
    t = re.sub(r"\s*\[\s*\]\s*", "[]", t)
    return t


# ---------------------------------------------------------------------------
# 拼接：MethodSignature → PLACEHOLDER_METHOD_CALL 代码
# ---------------------------------------------------------------------------


def build_method_call_code(
    target_class_name: str,
    sig: MethodSignature,
) -> str:
    """生成单行 Java 调用语句（替换 ``PLACEHOLDER_METHOD_CALL``）。

    Examples:
        >>> sig = MethodSignature("add", "int", ["int", "int"], True)
        >>> build_method_call_code("Solution", sig)
        'Solution.add((Integer) input[0], (Integer) input[1]);'

        >>> sig = MethodSignature("sort", "void", ["int[]"], True)
        >>> build_method_call_code("Solution", sig)
        'Solution.sort((int[]) input[0]);'

        >>> sig = MethodSignature("toUpper", "String", ["String"], False)
        >>> build_method_call_code("Solution", sig)
        'new Solution().toUpper((String) input[0]);'

    Args:
        target_class_name: 目标 Java 类名。
        sig: 方法签名。

    Returns:
        Java 调用语句（含末尾分号；不含返回值赋值——因为 PBT 测试只关注
        是否抛 ``JmlAssertionError``，返回值丢弃）。
    """
    casts: List[str] = []
    for i, t in enumerate(sig.param_types):
        casts.append(_argument_expression(t, f"input[{i}]"))
    args = ", ".join(casts)

    if sig.is_static:
        return f"{target_class_name}.{sig.method_name}({args});"
    return f"new {target_class_name}().{sig.method_name}({args});"


def _argument_expression(java_type: str, input_expr: str) -> str:
    java_type = java_type.strip()
    adapter = _PRIMITIVE_ARRAY_ADAPTERS.get(java_type)
    if adapter is not None:
        return f"{adapter}({input_expr})"
    return f"({_cast_type(java_type)}) {input_expr}"


def _cast_type(java_type: str) -> str:
    """返回从 ``Object`` cast 到 ``java_type`` 时使用的类型名。

    Java 自动拆箱要求：从 ``Object`` cast 到基本类型时，先 cast 到包装
    类型，编译器会自动 unbox。
    """
    java_type = java_type.strip()
    # 数组类型（含基本类型数组与对象数组）：原样 cast
    if java_type.endswith("[]"):
        return java_type
    # 基本类型 → wrapper（自动拆箱）
    if java_type in _PRIMITIVE_TO_WRAPPER:
        return _PRIMITIVE_TO_WRAPPER[java_type]
    # 引用类型：原样
    return java_type

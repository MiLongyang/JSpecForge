"""JML 提取与拼接工具（P6 处理器使用）。

LLM 在 Task A 会话中返回的 ``content`` 是**自由文本**——可能含 markdown
围栏、解释段落、多个代码块、纯代码等。本模块负责把 LLM 回复中的 Java +
JML 注释提取出来，作为 ``full_java_with_jml``（带 JML 注释的完整 Java
源码），供 OpenJML / RAC / PBT 后续验证。

P5 提示词渲染只负责"模板 + 变量 → 字符串"，**不负责** LLM 输出解析。
本模块是 P6 任务书 §7.5 / P5 §7.9 中明确划给 P6 的工作。

**依赖**：仅 Python 标准库（``re``）。**不**依赖 db / llm / prompts。

**对外接口**：

- :func:`extract_jml_block`：从 LLM ``content`` 中提取一段含 JML 注释
  的 Java 代码块；提取失败返回 ``None``
- :func:`merge_jml_into_code`：把提取结果作为 ``full_java_with_jml``
  返回（V1 简化策略：直接用 LLM 版本，前提是含 ``//@`` 或 ``/*@``）
- :func:`extract_method_block`：从 LLM ``content`` 中提取一段 Java 方法
  代码（用于 input_gen 提取 ``generateValidInput`` 方法）

**V1 简化策略说明**：``merge_jml_into_code`` 不做"局部 JML 注释 diff
拼回原 Java"——LLM 通常会返回完整 Java 类（含 import + class 头 + 方法体），
我们直接用 LLM 版本作为最终源码。``original_full_code`` 仅用于校验
（提取出的代码必须包含原始的方法签名片段，否则视为提取失败）。

更精细的拼接策略（按方法签名定位、保留原 Java 注释、剥离 LLM 多余文本等）
列入 ``docs/reference/future_improvements.md`` 待后续迭代。
"""

from __future__ import annotations

import re
from typing import List, Optional

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: markdown 围栏代码块匹配。``re.DOTALL`` 让 ``.`` 跨行匹配。
#: 既匹配 ``` java\n...```（含 java 标识），也匹配 ```\n...```（无标识）。
_FENCE_PATTERN: re.Pattern[str] = re.compile(
    r"```(?:java|Java|JAVA)?\s*\n(.*?)(?:```|\Z)",
    re.DOTALL,
)

#: 无围栏时，从首个 ``import`` / ``public class`` / ``class`` 起取到末尾。
_RAW_JAVA_START_PATTERN: re.Pattern[str] = re.compile(
    r"(?:^|\n)(?P<start>(?:import\s+|public\s+(?:final\s+|abstract\s+)?class\s+|class\s+))",
)

#: JML 单行注释 ``//@`` 或多行注释起始 ``/*@``。
_JML_MARKERS: tuple[str, ...] = ("//@", "/*@")

#: input_gen 模板要求 LLM 返回方法 ``public static Object[] generateValidInput(Random random)``。
#: 提取算法用此正则定位方法起点。
_INPUT_GEN_METHOD_HEADER: re.Pattern[str] = re.compile(
    r"public\s+static\s+Object\s*\[\s*\]\s+generateValidInput\s*\(",
)


# ---------------------------------------------------------------------------
# 提取主函数
# ---------------------------------------------------------------------------


def extract_jml_block(content: str) -> Optional[str]:
    """从 LLM 回复 ``content`` 中提取一段含 JML 注释的 Java 代码块。

    算法（V1）：

    1. 优先匹配 markdown 围栏 ``` java ... ```（含 / 不含 ``java`` 标识符
       均接受）。如果有多个围栏，取**最长的**（最可能是完整代码）
    2. 没有围栏：尝试从首个 ``import`` / ``public class`` / ``class``
       起截到末尾
    3. 校验：提取出的内容必须含 ``//@`` 或 ``/*@``——否则视为 LLM 没生成
       JML，返回 ``None``

    Args:
        content: LLM 返回的 ``LLMResult.content`` 字段。

    Returns:
        提取出的 Java 代码字符串（已去首尾空白）；失败返回 ``None``。

    Examples:
        >>> extract_jml_block(
        ...     "好的：\\n```java\\n//@ requires x >= 0;\\n"
        ...     "public static int abs(int x) { return x; }\\n```"
        ... )
        '//@ requires x >= 0;\\npublic static int abs(int x) { return x; }'
    """
    if not content:
        return None

    candidate = _try_extract_from_fences(content)
    if candidate is None:
        candidate = _try_extract_from_raw(content)
    if candidate is None:
        return None

    if not _contains_jml_marker(candidate):
        return None

    return candidate.strip()


def merge_jml_into_code(
    original_full_code: str,
    jml_annotated_code: str,
) -> Optional[str]:
    """把 LLM 返回的带 JML Java 代码作为 ``full_java_with_jml`` 返回。

    V1 简化策略：

    - LLM 通常返回**完整 Java 类**（含 import + class 头 + 方法体）
    - 直接采用 LLM 版本作为最终源码
    - ``original_full_code`` 仅用于轻校验：提取出的代码若**完全不含**
      原始 ``full_code`` 中任何 ``public ... method_name(`` 形式的方法
      签名片段，视为 LLM 跑题（如返回了完全不相关的代码），返回 ``None``

    Args:
        original_full_code: 原始 ``tasks.full_code``（未含 JML 的完整 Java）。
        jml_annotated_code: :func:`extract_jml_block` 的提取结果。

    Returns:
        ``full_java_with_jml`` 字符串；失败返回 ``None``。
    """
    if not jml_annotated_code:
        return None
    if not jml_annotated_code.strip():
        return None

    # 轻校验：提取出的代码至少含原始代码中**任意**一个方法签名标识符
    # （public/private/protected/static 后跟一个标识符 + ``(``）
    method_idents = _extract_method_identifiers(original_full_code)
    if method_idents:
        if not any(
            re.search(rf"\b{re.escape(name)}\s*\(", jml_annotated_code)
            for name in method_idents
        ):
            return None

    return jml_annotated_code.strip()


def is_source_method_fidelity_preserved(
    original_method_code: str,
    jml_annotated_code: str,
) -> bool:
    """Return whether the target method source is unchanged after removing JML.

    The comparison intentionally ignores formatting and comments, but keeps Java
    tokens exact. It is not a semantic equivalence check: renames, expression
    rewrites, signature changes, or body changes fail.
    """
    if not original_method_code or not original_method_code.strip():
        return True
    if not jml_annotated_code or not jml_annotated_code.strip():
        return False

    method_names = _extract_method_identifiers(original_method_code)
    if not method_names:
        return True
    method_name = method_names[0]

    original_method = _extract_java_method_by_name(
        original_method_code, method_name
    )
    if original_method is None:
        original_method = original_method_code

    annotated_method = _extract_java_method_by_name(
        jml_annotated_code, method_name
    )
    if annotated_method is None:
        return False

    return (
        _java_token_fingerprint(original_method)
        == _java_token_fingerprint(annotated_method)
    )


def extract_method_block(
    content: str,
    method_header_pattern: re.Pattern[str] = _INPUT_GEN_METHOD_HEADER,
) -> Optional[str]:
    """从 LLM 回复中提取 input_gen 阶段的方法代码块。

    E.2.2 (2026-05-03) 重做: 不再只提取单个 ``generateValidInput`` 方法,
    而是提取整段方法定义(允许 LLM 写辅助方法如 ``generateRandomWord``)。
    所有方法会一起被注入到 TestDriver class 体内, 否则辅助方法引用会
    产生 javac "找不到符号" 错误。

    算法:

    1. 提取整段 Java 代码 (markdown 围栏优先)
    2. 校验含目标方法签名 (``generateValidInput``)
    3. 如果代码被外层 ``public class X { ... }`` 包装, **剥掉** class wrapper
    4. 移除顶部 ``import`` 语句 (TestDriver 模板自己已有 import,无需重复)
    5. 返回剩下的方法定义 (一段连续代码块, 可含多个 method)

    Args:
        content: LLM 返回的 content。
        method_header_pattern: 匹配目标方法签名的正则, 用于校验内容含
            ``generateValidInput``。

    Returns:
        方法定义字符串(可能含多个方法, 全部直接注入 TestDriver class);
        失败返回 ``None``。
    """
    if not content:
        return None

    # 1. 提取整段 Java 代码（不强制含 JML）
    candidate = _try_extract_from_fences(content)
    if candidate is None:
        candidate = _try_extract_from_raw(content)
    if candidate is None:
        # 兜底：直接用整个 content
        candidate = content

    # 2. 校验含目标方法
    if not method_header_pattern.search(candidate):
        return None

    # 3. 剥掉外层 class wrapper (如果 LLM 写了 ``public class X { ... }``)
    candidate = _strip_class_wrapper(candidate)

    # 4. 移除顶部 import 语句 (TestDriver 模板已含, 重复 import 不允许 class 体内)
    candidate = _strip_import_lines(candidate)

    # 5. 二次校验剥离后仍含目标方法
    if not method_header_pattern.search(candidate):
        return None

    return candidate.strip() or None


# ---------------------------------------------------------------------------
# E.2.2 辅助: 剥外层 class wrapper / 移除 import
# ---------------------------------------------------------------------------


def _strip_class_wrapper(code: str) -> str:
    """如果 ``code`` 整体被 ``public class X { ... }`` 或 ``class X { ... }``
    包装, 返回 class 体内的内容 (剥掉外层); 否则原样返回。

    E.2.2 (2026-05-03): LLM 偶尔会忽略提示词把 generateValidInput 包在 class
    里输出, 注入 TestDriver 时会引发"嵌套 class"或"顶层声明位置错误"。
    """
    # 匹配 (public)? class IDENT { ... }
    pattern = re.compile(
        r"^\s*(?:public\s+)?class\s+\w+\s*\{",
        re.MULTILINE,
    )
    m = pattern.search(code)
    if m is None:
        return code
    open_brace_idx = code.rfind("{", m.start(), m.end())
    if open_brace_idx < 0:
        return code
    close_brace_idx = _find_matching_brace(code, open_brace_idx)
    if close_brace_idx < 0:
        return code
    # 提取 class 体内 (open+1 到 close)
    inner = code[open_brace_idx + 1:close_brace_idx]
    return inner.strip()


def _strip_import_lines(code: str) -> str:
    """移除顶部连续的 ``import ...;`` 行 (java.util.Random 等已在 TestDriver
    模板里 import, 重复 import 在 class 体内不允许)。

    只处理顶部连续 import 行, 不动方法体内可能含的 import 字符串。
    """
    lines = code.split("\n")
    out: List[str] = []
    skipped_imports = True
    for line in lines:
        stripped = line.strip()
        if skipped_imports and (
            stripped.startswith("import ")
            or stripped.startswith("package ")
            or stripped == ""
        ):
            # 顶部空行也跳, 防止 import 段后多余空行
            if stripped == "" and not out:
                continue
            if stripped.startswith(("import ", "package ")):
                continue
        skipped_imports = False
        out.append(line)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 类名提取（OJ / RAC / PBT 共用）
# ---------------------------------------------------------------------------


def extract_java_class_name(java_code: str) -> Optional[str]:
    """从 Java 源码中提取首个 ``public class`` / ``class`` 后的标识符。

    用于 P6 处理器决定 ``.java`` / ``.class`` 文件名（如
    ``Solution_42_0.java``）。

    Args:
        java_code: 完整或部分 Java 源码。

    Returns:
        类名字符串；找不到时返回 ``None``。
    """
    if not java_code:
        return None
    m = re.search(
        r"public\s+(?:final\s+|abstract\s+)?class\s+(\w+)",
        java_code,
    )
    if m:
        return m.group(1)
    m2 = re.search(r"\bclass\s+(\w+)", java_code)
    if m2:
        return m2.group(1)
    return None


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _try_extract_from_fences(content: str) -> Optional[str]:
    """尝试从 markdown 围栏中提取 Java 代码。

    多个围栏时取**最长的**（最可能是完整代码）。
    """
    blocks: List[str] = []
    for m in _FENCE_PATTERN.finditer(content):
        block = m.group(1)
        if block:
            blocks.append(block)
    if not blocks:
        return None
    return max(blocks, key=len)


def _try_extract_from_raw(content: str) -> Optional[str]:
    """无围栏时，从首个 import/class 起取到末尾。"""
    m = _RAW_JAVA_START_PATTERN.search(content)
    if m is None:
        return None
    return content[m.start("start") :]


def _contains_jml_marker(text: str) -> bool:
    """检查文本是否含至少一个 JML 标记。"""
    return any(marker in text for marker in _JML_MARKERS)


def _extract_method_identifiers(java_code: str) -> List[str]:
    """从 Java 源码中提取方法名标识符（粗略，用于校验）。

    匹配 ``[modifier]+ [generic]? returnType methodName(``。返回方法名
    列表（去重，保留出现顺序）。
    """
    if not java_code:
        return []
    pattern = re.compile(
        r"(?:public|private|protected|static|final|abstract|synchronized)\s+"
        r"(?:[\w<>\[\],\s.]+?\s+)?"
        r"(\w+)\s*\(",
    )
    seen: set[str] = set()
    result: List[str] = []
    for m in pattern.finditer(java_code):
        name = m.group(1)
        # 过滤掉 Java 关键字 / 控制流（粗略）
        if name in {"if", "while", "for", "switch", "catch", "return", "new"}:
            continue
        if name not in seen:
            seen.add(name)
            result.append(name)
    return result


def _extract_java_method_by_name(java_code: str, method_name: str) -> Optional[str]:
    """Extract the first Java method declaration with ``method_name``."""
    if not java_code or not method_name:
        return None
    name = re.escape(method_name)
    method_pattern = re.compile(
        r"(?P<header>"
        r"(?:@\w+(?:\([^)]*\))?\s*)*"
        r"(?:(?:public|private|protected|static|final|abstract|synchronized|"
        r"native|strictfp|default)\s+)*"
        r"(?:<[^>{};]+>\s*)?"
        r"[\w$<>\[\],.?&\s]+\s+"
        rf"{name}\s*\([^;{{}}]*\)\s*"
        r"(?:throws\s+[^{;]+)?"
        r"\{"
        r")",
        re.MULTILINE,
    )
    for match in method_pattern.finditer(java_code):
        open_idx = java_code.find("{", match.start("header"), match.end("header"))
        close_idx = _find_matching_brace(java_code, open_idx)
        if close_idx >= 0:
            return java_code[match.start("header") : close_idx + 1]
    return None


def _java_token_fingerprint(java_code: str) -> List[str]:
    """Tokenize Java source after removing comments and whitespace."""
    return _tokenize_java(_strip_java_comments(java_code))


def _strip_java_comments(java_code: str) -> str:
    """Remove Java/JML comments while preserving string and char literals."""
    out: List[str] = []
    i = 0
    in_string = False
    in_char = False
    while i < len(java_code):
        ch = java_code[i]
        nxt = java_code[i + 1] if i + 1 < len(java_code) else ""

        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < len(java_code):
                out.append(java_code[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue

        if in_char:
            out.append(ch)
            if ch == "\\" and i + 1 < len(java_code):
                out.append(java_code[i + 1])
                i += 2
                continue
            if ch == "'":
                in_char = False
            i += 1
            continue

        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "'":
            in_char = True
            out.append(ch)
            i += 1
            continue

        if ch == "/" and nxt == "/":
            i += 2
            while i < len(java_code) and java_code[i] not in "\r\n":
                i += 1
            out.append("\n")
            continue
        if ch == "/" and nxt == "*":
            i += 2
            while i + 1 < len(java_code) and not (
                java_code[i] == "*" and java_code[i + 1] == "/"
            ):
                i += 1
            i += 2 if i + 1 < len(java_code) else 0
            out.append(" ")
            continue

        out.append(ch)
        i += 1
    return "".join(out)


def _tokenize_java(java_code: str) -> List[str]:
    """Small Java lexer sufficient for source-fidelity token comparison."""
    tokens: List[str] = []
    operators = (
        ">>>=", "<<=", ">>=", "...", ">>>", "==", "!=", "<=", ">=", "&&",
        "||", "++", "--", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=",
        "->", "::", "<<", ">>",
    )
    i = 0
    while i < len(java_code):
        ch = java_code[i]
        if ch.isspace():
            i += 1
            continue
        if ch == '"' or ch == "'":
            quote = ch
            start = i
            i += 1
            while i < len(java_code):
                if java_code[i] == "\\":
                    i += 2
                    continue
                if java_code[i] == quote:
                    i += 1
                    break
                i += 1
            tokens.append(java_code[start:i])
            continue
        if ch.isalpha() or ch == "_" or ch == "$":
            start = i
            i += 1
            while i < len(java_code) and (
                java_code[i].isalnum() or java_code[i] in {"_", "$"}
            ):
                i += 1
            tokens.append(java_code[start:i])
            continue
        if ch.isdigit():
            start = i
            if (
                ch == "0"
                and i + 1 < len(java_code)
                and java_code[i + 1] in {"x", "X"}
            ):
                i += 2
                while i < len(java_code) and (
                    java_code[i].isdigit()
                    or java_code[i].lower() in {"a", "b", "c", "d", "e", "f"}
                    or java_code[i] == "_"
                ):
                    i += 1
            else:
                i += 1
                while i < len(java_code) and (
                    java_code[i].isdigit() or java_code[i] == "_"
                ):
                    i += 1
                if i < len(java_code) and java_code[i] == ".":
                    i += 1
                    while i < len(java_code) and (
                        java_code[i].isdigit() or java_code[i] == "_"
                    ):
                        i += 1
                if i < len(java_code) and java_code[i] in {"e", "E"}:
                    i += 1
                    if i < len(java_code) and java_code[i] in {"+", "-"}:
                        i += 1
                    while i < len(java_code) and (
                        java_code[i].isdigit() or java_code[i] == "_"
                    ):
                        i += 1
            if i < len(java_code) and java_code[i] in {"l", "L", "f", "F", "d", "D"}:
                i += 1
            tokens.append(java_code[start:i])
            continue
        matched = False
        for op in operators:
            if java_code.startswith(op, i):
                tokens.append(op)
                i += len(op)
                matched = True
                break
        if matched:
            continue
        tokens.append(ch)
        i += 1
    return tokens


def _find_matching_brace(text: str, open_idx: int) -> int:
    """从 ``text[open_idx]`` 这个 ``{`` 起找到配对的 ``}``。

    朴素配对（不感知字符串字面值 / 注释中的 ``{`` ``}``）。对于规范的
    Java 代码足够用；如果遇到方法体内字符串字面值含未配对花括号的极端
    情况，返回 ``-1``。

    Returns:
        配对 ``}`` 在 ``text`` 中的索引；找不到返回 ``-1``。
    """
    if open_idx < 0 or open_idx >= len(text) or text[open_idx] != "{":
        return -1
    depth = 0
    in_string = False
    in_char = False
    in_line_comment = False
    in_block_comment = False
    i = open_idx
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""

        # 注释处理
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue
        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        # 字符串字面值
        if in_string:
            if ch == "\\":
                i += 2  # 跳过转义字符
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if in_char:
            if ch == "\\":
                i += 2
                continue
            if ch == "'":
                in_char = False
            i += 1
            continue
        # 注释起点
        if ch == "/" and nxt == "/":
            in_line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue
        # 字符串/字符起点
        if ch == '"':
            in_string = True
            i += 1
            continue
        if ch == "'":
            in_char = True
            i += 1
            continue
        # 花括号
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1

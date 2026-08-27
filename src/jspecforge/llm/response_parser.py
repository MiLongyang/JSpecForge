"""SSE 流式响应解析（OpenAI 兼容格式）。

依据 ``docs/design/LLM调用与厂商管理设计.md`` §三（SSE 流式响应解析）+
§四（Token 统计提取）。

**关键不变量**：

- 编码必须 UTF-8（``response.encoding = 'utf-8'``），否则中文乱码
- 必须检查 ``chunk.error``（HTTP 200 也可能含上游错误）
- ``delta.reasoning_content`` 单独累积，与 ``delta.content`` 分离
- thinking_tag 备用切割：当模型把 ``<think>...</think>`` 混进 content 时启用
- token 统计能拿就拿，拿不到留 None（不同厂商支持不一致）
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence

from jspecforge.utils.log_utils import get_logger

_logger = get_logger("llm.response_parser")


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass
class ParsedResponse:
    """SSE 解析结果。

    Attributes:
        content: 模型正文回复（已剥离 thinking_tag 内容）。
        reasoning: 推理过程（来自 ``delta.reasoning_content`` 或 thinking_tag）。
        error: 上游错误（``chunk.error`` 字段），无错误为空字符串。
        prompt_tokens: 输入 token 数（拿不到为 None）。
        completion_tokens: 输出 token 数（拿不到为 None）。
        total_tokens: 总 token 数（拿不到为 None）。
        raw_chunks: 已解析的 chunk 总数（调试用）。
    """

    content: str = ""
    reasoning: str = ""
    error: str = ""
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    raw_chunks: int = 0


# ---------------------------------------------------------------------------
# 思考模型识别
# ---------------------------------------------------------------------------


def is_thinking_model(model_name: str, keywords: Sequence[str]) -> bool:
    """模型名是否命中 thinking_model_keywords（不区分大小写模糊匹配）。"""
    if not model_name:
        return False
    lower = model_name.lower()
    return any(kw.lower() in lower for kw in keywords if kw)


# ---------------------------------------------------------------------------
# thinking_tag 备用切割
# ---------------------------------------------------------------------------


def _build_tag_re(open_tag: str, close_tag: str) -> Optional[re.Pattern[str]]:
    """构造 ``<think>(.*?)</think>`` 这类匹配器。两个 tag 都非空才返回。"""
    if not open_tag or not close_tag:
        return None
    return re.compile(
        re.escape(open_tag) + r"(.*?)" + re.escape(close_tag),
        re.DOTALL,
    )


def split_thinking_tag(
    content: str,
    open_tag: str,
    close_tag: str,
) -> tuple[str, str]:
    """从 content 中剥离 ``<open_tag>...<close_tag>`` 段。

    Args:
        content: 原始 content。
        open_tag: 开始标签（空字符串 = 禁用）。
        close_tag: 结束标签（空字符串 = 禁用）。

    Returns:
        ``(stripped_content, extracted_reasoning)``。
        - ``stripped_content``：剥掉所有标签段后的剩余正文
        - ``extracted_reasoning``：所有标签段拼接（按出现顺序）
        若标签未配置或无匹配，返回 ``(content, "")``。
    """
    pattern = _build_tag_re(open_tag, close_tag)
    if pattern is None or not content:
        return content, ""
    matches = pattern.findall(content)
    if not matches:
        return content, ""
    stripped = pattern.sub("", content)
    reasoning = "\n".join(m.strip() for m in matches if m.strip())
    return stripped, reasoning


# ---------------------------------------------------------------------------
# SSE 主解析
# ---------------------------------------------------------------------------


def _iter_sse_lines(stream_lines: Iterable[str]) -> Iterator[str]:
    """从 ``response.iter_lines(decode_unicode=True)`` 等可迭代源筛出 ``data: `` 行的负载。"""
    for line in stream_lines:
        if not line:
            continue
        # iter_lines 可能给 bytes，也可能给 str；统一兜底
        if isinstance(line, bytes):
            try:
                line = line.decode("utf-8", errors="replace")
            except Exception:
                continue
        if not line.startswith("data: "):
            continue
        yield line[6:]


def parse_sse_stream(
    response: Any,
    *,
    model_name: str,
    thinking_keywords: Sequence[str] = (),
    thinking_tag: str = "",
    thinking_end_tag: str = "",
) -> ParsedResponse:
    """解析 OpenAI 兼容的 SSE 流响应。

    Args:
        response: ``requests.Response``（必须是 ``stream=True`` 模式获取的）。
            本函数会强制设置 ``response.encoding = 'utf-8'``。
        model_name: 当前请求的模型名（用于命中 thinking_keywords 的判断）。
        thinking_keywords: 思考模型名关键字（命中时偏好 ``reasoning_content``
            字段；未命中也容错处理）。
        thinking_tag: 备用思考标签开始（如 ``<think>``）；空 = 禁用。
        thinking_end_tag: 备用思考标签结束（如 ``</think>``）；空 = 禁用。

    Returns:
        :class:`ParsedResponse`。

    Note:
        本函数**不会抛出网络相关异常**——网络异常请在调用前后由上层捕获。
        JSON 解析错误的 chunk 会被记 DEBUG 日志后跳过，不影响整体解析。
    """
    # 强制 UTF-8（gotchas: 中文乱码 ä½ å¥½）
    try:
        response.encoding = "utf-8"
    except Exception:
        # mock 对象可能没这个属性；不阻塞解析
        pass

    is_thinking = is_thinking_model(model_name, thinking_keywords)

    content_parts: List[str] = []
    reasoning_parts: List[str] = []
    error_msg = ""
    usage: Optional[Dict[str, Any]] = None
    raw_chunks = 0

    for data_str in _iter_sse_lines(response.iter_lines(decode_unicode=True)):
        if data_str.strip() == "[DONE]":
            break
        try:
            chunk = json.loads(data_str)
        except json.JSONDecodeError:
            _logger.debug("parse_sse_stream: 跳过无效 JSON chunk: %r", data_str[:120])
            continue
        raw_chunks += 1

        # 1. 错误优先（HTTP 200 也可能含错误）
        err_obj = chunk.get("error")
        if err_obj:
            if isinstance(err_obj, dict):
                error_msg = str(err_obj.get("message") or err_obj)
            else:
                error_msg = str(err_obj)
            break

        # 2. 提取 delta.content / delta.reasoning_content
        choices = chunk.get("choices") or []
        if choices:
            delta = choices[0].get("delta") or {}
            c = delta.get("content")
            if c:
                content_parts.append(c)
            r = delta.get("reasoning_content")
            if r:
                reasoning_parts.append(r)

        # 3. 提取 usage（可能在最后一个 chunk）
        if chunk.get("usage"):
            usage = chunk["usage"]

    content = "".join(content_parts)
    reasoning = "".join(reasoning_parts)

    # 4. thinking_tag 备用切割：当 reasoning 字段没值但 content 含标签时启用
    if not reasoning and content:
        stripped, extracted = split_thinking_tag(
            content, thinking_tag, thinking_end_tag
        )
        if extracted:
            content = stripped
            reasoning = extracted

    # 5. 思考模型但 reasoning 仍空 + content 也空 → 仅作日志，不强制干预
    if is_thinking and not reasoning and not content and not error_msg:
        _logger.debug(
            "parse_sse_stream: 思考模型 %s 返回空 (chunks=%d)",
            model_name,
            raw_chunks,
        )

    prompt_tokens = completion_tokens = total_tokens = None
    if isinstance(usage, dict):
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        total_tokens = usage.get("total_tokens")

    return ParsedResponse(
        content=content,
        reasoning=reasoning,
        error=error_msg,
        prompt_tokens=_safe_int(prompt_tokens),
        completion_tokens=_safe_int(completion_tokens),
        total_tokens=_safe_int(total_tokens),
        raw_chunks=raw_chunks,
    )


def _safe_int(value: Any) -> Optional[int]:
    """容错把 usage 字段转 int；非数字返回 None。"""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 成功判定（design §3.3）
# ---------------------------------------------------------------------------


@dataclass
class Verdict:
    """解析后的成功/失败判定。"""

    success: bool
    error_kind: str = ""  # "" / "upstream_error" / "reasoning_only_no_content" / "empty_response"
    error_detail: str = ""


def judge_response(parsed: ParsedResponse) -> Verdict:
    """对 :class:`ParsedResponse` 应用 design §3.3 的成功判定逻辑。

    判定规则：

    - 有 ``content`` → 成功（无论 reasoning 是否有内容）
    - ``content`` 空 + ``reasoning`` 有 → 失败（``reasoning_only_no_content``）
    - 两者都空 → 失败（``empty_response``）
    - SSE 中检测到 ``error`` 字段 → 失败（``upstream_error``，覆盖前面所有）
    """
    if parsed.error:
        return Verdict(
            success=False,
            error_kind="upstream_error",
            error_detail=parsed.error,
        )
    if parsed.content and parsed.content.strip():
        return Verdict(success=True)
    if parsed.reasoning and parsed.reasoning.strip():
        return Verdict(
            success=False,
            error_kind="reasoning_only_no_content",
            error_detail="模型只输出 reasoning_content，正文 content 为空",
        )
    return Verdict(
        success=False,
        error_kind="empty_response",
        error_detail="SSE 流结束但 content / reasoning 都为空",
    )

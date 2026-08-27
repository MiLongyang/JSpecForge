"""LLM 会话管理：``task_a`` / ``input_gen`` 隔离 + 持久化到 ``task_llm_messages``。

依据 ``docs/design/数据库表字段设计.md`` §四（task_llm_messages 字段）+ §九
（写入时序）+ ``docs/design/核心工作流程设计.md`` §三（task_a 会话）+ §5.3
（输入生成器会话隔离规则）。

**P4 已包揽 task_llm_messages 的写入与 pending 标记**——P6/P7 处理器**禁止**
直接调用 :class:`MessageDAO.insert`，必须经过本模块的 :class:`LLMSession`。
理由：

1. 让 ``seq`` / ``round`` / ``a_round`` / pending 标记规则只在一个文件里
2. 让"在内存里 append 消息" + "持久化到 DB" 永远同步发生，避免内存与 DB
   分叉
3. 让中断恢复点天然落在每条消息边界（每条消息都已 commit）

**会话隔离规则**（与 design §三 / §5.3 一致）：

- ``task_a``：单实例覆盖整个任务的全部 Task A 轮次。``a_round`` 字段固定
  传 0（不参与分组）；新 a_round 不会创建新会话，只是在同一会话内继续 append
- ``input_gen``：**每个 a_round 一个独立实例**。新 a_round 时直接构造一个
  新的 ``LLMSession``，因为 DB 按 ``(task_id, session_type, a_round)`` 分组，
  新 a_round 自然就是新组（``seq`` 从 0 开始）。**这就是"input_gen 按 a_round
  隔离，新 a_round 时清空消息历史"的实现**

**round 字段语义**（design §四 表注释）：

- system 消息：``round = 0``
- 第 i 个 (user, assistant) 对：两条都是 ``round = i``（0-based）
- 因此：append user 时 round = 当前 assistant 计数；record assistant 时
  round = 当前 assistant 计数；记录后 assistant 计数 +1

**pending 标记**：assistant 行的结果列（``is_oj_pass`` / ``compile_status``
等）在 :meth:`record_assistant` 时**保持 NULL**，由后续验证步骤通过
:func:`MessageDAO.update_task_a_result` / :func:`MessageDAO.update_input_gen_result`
回填。这是 design §九 中的 pending 标记。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Union

from jspecforge.db.enums import MessageRole, SessionType
from jspecforge.db.message_dao import MessageDAO
from jspecforge.llm.client import BaseLLMClient, LLMResult
from jspecforge.utils.log_utils import get_logger

_logger = get_logger("llm.session")


# ---------------------------------------------------------------------------
# 会话类
# ---------------------------------------------------------------------------


@dataclass
class _MessageInMem:
    """内存中保留的消息（OpenAI 兼容格式）。"""

    role: str
    content: str


class LLMSession:
    """单个 LLM 会话（task_a 或某 a_round 的 input_gen）。

    用法（生产代码，P6 处理器）::

        sess = LLMSession.for_task_a(task_id=42, dao=MessageDAO, conn=conn)
        sess.append_system(rendered_a_s)
        sess.append_user(rendered_a_u_first)
        result = sess.complete_via(client)   # 失败时不写 assistant
        if result.success:
            jml = extract_jml(result.content)   # P6 自己的提取逻辑
            ...

    用法（中断恢复，P7 编排器）::

        sess = LLMSession.from_db(
            task_id=42,
            session_type=SessionType.TASK_A,
            a_round=0,
            dao=MessageDAO,
            conn=conn,
        )
        # sess.messages 已恢复到中断前
        sess.append_user(rendered_retry_prompt)   # 继续追加
        ...

    用法（测试，无 DB）::

        sess = LLMSession.for_task_a(task_id=1, dao=None, conn=None)
        # 仅在内存里维护 messages，不持久化

    线程不安全：单个实例只能由一个线程使用（与 P6/P7 任务粒度一致）。
    """

    def __init__(
        self,
        *,
        task_id: int,
        session_type: Union[str, SessionType],
        a_round: int,
        dao: Optional[type] = None,
        conn: Optional[Any] = None,
    ) -> None:
        """通用构造器。一般使用 :meth:`for_task_a` / :meth:`for_input_gen`。

        Args:
            task_id: 任务 ID。
            session_type: ``"task_a"`` / ``"input_gen"`` 或 :class:`SessionType`。
            a_round: ``task_a`` 时固定传 0；``input_gen`` 时传当前 a_round。
            dao: :class:`MessageDAO` 类（或 mock）。``None`` 时跳过持久化（测试用）。
            conn: PyMySQL connection。``None`` 时跳过持久化（测试用）。
        """
        self._task_id = int(task_id)
        self._session_type: SessionType = (
            session_type if isinstance(session_type, SessionType)
            else SessionType(session_type)
        )
        self._a_round = int(a_round)
        if self._session_type is SessionType.TASK_A and self._a_round != 0:
            # design §四：task_a 时 a_round 固定 0
            _logger.warning(
                "LLMSession(task_a): a_round=%d != 0，强制改为 0",
                self._a_round,
            )
            self._a_round = 0
        self._dao = dao
        self._conn = conn

        self._messages: List[_MessageInMem] = []
        self._next_seq: int = 0
        self._assistant_count: int = 0
        self._last_assistant_id: Optional[int] = None

    # ------------------------------------------------------------------
    # 工厂方法
    # ------------------------------------------------------------------

    @classmethod
    def for_task_a(
        cls,
        task_id: int,
        *,
        dao: Optional[type] = None,
        conn: Optional[Any] = None,
    ) -> "LLMSession":
        """创建 task_a 会话（a_round 固定 0）。"""
        return cls(
            task_id=task_id,
            session_type=SessionType.TASK_A,
            a_round=0,
            dao=dao,
            conn=conn,
        )

    @classmethod
    def for_input_gen(
        cls,
        task_id: int,
        a_round: int,
        *,
        dao: Optional[type] = None,
        conn: Optional[Any] = None,
    ) -> "LLMSession":
        """创建某 a_round 的 input_gen 会话。

        **新 a_round = 新实例**，从 ``seq=0`` 开始，与上一 a_round 完全隔离。
        """
        return cls(
            task_id=task_id,
            session_type=SessionType.INPUT_GEN,
            a_round=a_round,
            dao=dao,
            conn=conn,
        )

    @classmethod
    def from_db(
        cls,
        *,
        task_id: int,
        session_type: Union[str, SessionType],
        a_round: int,
        dao: type,
        conn: Any,
    ) -> "LLMSession":
        """从 DB 恢复会话状态（中断恢复用，P7 编排器使用）。

        从 ``task_llm_messages`` 按 (task_id, session_type, a_round) 拉取
        所有消息，重建内存状态。最后一条 assistant 若 pending（结果列 NULL）
        会被记录在 ``last_assistant_id`` 中，供恢复逻辑识别。
        """
        sess = cls(
            task_id=task_id,
            session_type=session_type,
            a_round=a_round,
            dao=dao,
            conn=conn,
        )
        rows = dao.list_session(
            conn,
            task_id=task_id,
            session_type=sess._session_type,
            a_round=sess._a_round,
        ) if hasattr(dao, "list_session") else MessageDAO.list_session(
            conn,
            task_id=task_id,
            session_type=sess._session_type,
            a_round=sess._a_round,
        )
        for row in rows:
            sess._messages.append(
                _MessageInMem(role=row["role"], content=row["content"])
            )
            seq = int(row["seq"])
            if seq + 1 > sess._next_seq:
                sess._next_seq = seq + 1
            if row["role"] == MessageRole.ASSISTANT.value:
                sess._assistant_count += 1
                sess._last_assistant_id = int(row["id"])
        _logger.info(
            "LLMSession.from_db: task=%d session=%s a_round=%d 恢复 %d 条消息 "
            "(assistant_count=%d, last_assistant_id=%s)",
            sess._task_id,
            sess._session_type.value,
            sess._a_round,
            len(rows),
            sess._assistant_count,
            sess._last_assistant_id,
        )
        return sess

    # ------------------------------------------------------------------
    # 只读属性
    # ------------------------------------------------------------------

    @property
    def task_id(self) -> int:
        return self._task_id

    @property
    def session_type(self) -> SessionType:
        return self._session_type

    @property
    def a_round(self) -> int:
        return self._a_round

    @property
    def messages(self) -> List[Dict[str, str]]:
        """OpenAI 格式 messages 列表（深拷贝，防外部修改）。"""
        return [{"role": m.role, "content": m.content} for m in self._messages]

    @property
    def assistant_count(self) -> int:
        return self._assistant_count

    @property
    def last_assistant_id(self) -> Optional[int]:
        return self._last_assistant_id

    @property
    def is_persisted(self) -> bool:
        return self._dao is not None and self._conn is not None

    def log_context(self) -> Dict[str, Any]:
        """传给 :meth:`BaseLLMClient.chat` 的日志上下文。"""
        return {
            "task_id": self._task_id,
            "session_type": self._session_type.value,
            "a_round": self._a_round,
        }

    # ------------------------------------------------------------------
    # 追加消息（自动持久化）
    # ------------------------------------------------------------------

    def append_system(self, content: str) -> Optional[int]:
        """追加 system 消息。``round`` 固定 0。"""
        return self._append(MessageRole.SYSTEM, content, round_value=0)

    def append_user(self, content: str) -> Optional[int]:
        """追加 user 消息。``round = 当前 assistant 计数``。"""
        return self._append(
            MessageRole.USER, content, round_value=self._assistant_count
        )

    def record_assistant(
        self,
        content: str,
        *,
        call_meta: Optional[Mapping[str, Any]] = None,
    ) -> Optional[int]:
        """记录 LLM 返回的 assistant 消息。

        ``round`` 与配对的 user 相同（即记录前的 assistant 计数）。记录后
        ``assistant_count`` +1。

        **结果列保持 NULL（pending 标记）**，由后续 OJ/RAC/PBT 或编译验证步骤
        通过 :func:`MessageDAO.update_task_a_result` /
        :func:`MessageDAO.update_input_gen_result` 回填。
        """
        round_value = self._assistant_count
        msg_id = self._append(
            MessageRole.ASSISTANT,
            content,
            round_value=round_value,
            call_meta=call_meta,
        )
        self._assistant_count += 1
        if msg_id is not None:
            self._last_assistant_id = msg_id
        return msg_id

    def _append(
        self,
        role: MessageRole,
        content: str,
        *,
        round_value: int,
        call_meta: Optional[Mapping[str, Any]] = None,
    ) -> Optional[int]:
        """通用追加：内存 +1 + 持久化（如果配置了 dao/conn）+ commit。"""
        seq = self._next_seq
        self._next_seq += 1
        self._messages.append(_MessageInMem(role=role.value, content=content))

        if not self.is_persisted:
            return None

        assert self._dao is not None and self._conn is not None
        msg_id = self._dao.insert(
            self._conn,
            task_id=self._task_id,
            session_type=self._session_type,
            a_round=self._a_round,
            seq=seq,
            round=round_value,
            role=role,
            content=content,
            call_meta=dict(call_meta) if call_meta is not None else None,
        )
        try:
            self._conn.commit()
        except Exception:  # noqa: BLE001
            _logger.warning(
                "LLMSession._append: commit 失败 (task=%d, seq=%d) — "
                "消息已 INSERT 但未 commit，可能在崩溃时丢失",
                self._task_id,
                seq,
            )
        return int(msg_id)

    # ------------------------------------------------------------------
    # 高层便捷
    # ------------------------------------------------------------------

    def complete_via(
        self,
        client: BaseLLMClient,
    ) -> LLMResult:
        """一键发送：``client.chat(self.messages)`` → 成功时记录 assistant。

        失败时**不**记录 assistant（避免污染 message 历史；上层处理器会
        决定是否消耗 a_round 或重试 LLM 调用，对应 design §3.4 中
        "reasoning_only_no_content 不消耗 a_round 直接重试" 的语义）。
        """
        result = client.chat(self.messages, log_context=self.log_context())
        if result.success:
            self.record_assistant(
                result.content,
                call_meta=result.to_call_meta(),
            )
        return result

    # ------------------------------------------------------------------
    # 中断恢复辅助
    # ------------------------------------------------------------------

    def has_pending_assistant(self) -> bool:
        """最后一条 assistant 是否处于 pending 状态（结果列全 NULL）。

        仅持久化会话支持；非持久化返回 False。
        """
        if not self.is_persisted or self._last_assistant_id is None:
            return False
        assert self._dao is not None and self._conn is not None
        row = self._dao.get_latest_assistant(
            self._conn,
            task_id=self._task_id,
            session_type=self._session_type,
            a_round=self._a_round,
        )
        if row is None:
            return False
        if self._session_type is SessionType.TASK_A:
            # task_a：is_oj_pass IS NULL → pending
            return row.get("is_oj_pass") is None
        # input_gen：compile_status IS NULL → pending
        return row.get("compile_status") is None

    def has_trailing_user_without_assistant(self) -> bool:
        """末尾消息是否是 user 但没对应 assistant 跟随。

        2026-05-02 阶段 C(用户实测问题 6.1):
        中断在 LLM 调用前/中(已 INSERT user,但 LLM 还没返回 assistant)。
        重启后调用方应**跳过** ``append_user``,直接复用末尾 user 调
        :meth:`complete_via`,避免 DB 出现重复 user 行。

        Returns:
            True iff ``messages[-1].role == 'user'`` AND messages 非空。
            空会话返回 False(未开始,正常走 first 流程)。
        """
        if not self._messages:
            return False
        return self._messages[-1].role == MessageRole.USER.value

    def last_assistant_content(self) -> Optional[str]:
        """返回最后一条 assistant 的 content,无则返回 None。

        2026-05-02 阶段 C(用户实测问题 6.2):
        中断恢复时若 :meth:`has_pending_assistant` 为 True,调用方可用
        本方法直接拿 LLM 上次返回的内容,**跳过新一轮 LLM 调用**,
        改为重跑后续验证步骤(OJ / RAC / 编译等)。

        Returns:
            内存中最后一条 assistant 的 content;无 assistant 时为 None。
        """
        for msg in reversed(self._messages):
            if msg.role == MessageRole.ASSISTANT.value:
                return msg.content
        return None

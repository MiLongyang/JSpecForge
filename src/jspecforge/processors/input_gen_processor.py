"""InputGenProcessor —— 输入生成器 passthrough 处理器。

Phase 4 清理（2026-05-13）：生成器已上移到 task 级（``task.input_generator_code``），
本 processor 不再调 LLM / 编译验证，仅作为 task_a→pbt 之间的 passthrough
把 task 级生成器拷到 ``current_input_gen_code`` 并路由 PBT。

**职责**：

1. 若 ``task.input_generator_code`` 为空 → 路由 final（
   ``failure_stage="exhausted_rounds"``）
2. 否则拷到 ``task.current_input_gen_code`` → 路由 PBT

保留 :class:`InputGenInfraError` 占位以兼容 orchestrator 的 stage→infra 映射；
目前 ``process()`` 不会再抛该异常。
"""

from __future__ import annotations

from typing import Any

from jspecforge.config.settings import AppConfig
from jspecforge.core.task_entity import Task
from jspecforge.db.task_dao import TaskDAO
from jspecforge.processors.base import (
    NEXT_FINAL,
    NEXT_PBT,
    BaseProcessor,
    ProcessResult,
)
from jspecforge.utils.log_utils import get_logger

_logger = get_logger("processors.input_gen")


class InputGenInfraError(Exception):
    """InputGen 基础设施级错误（passthrough 下保留仅用于 orchestrator 映射）。"""


class InputGenProcessor(BaseProcessor):
    """输入生成器 passthrough 处理器。"""

    def __init__(
        self,
        config: AppConfig,
        *,
        conn: Any,
        task_dao: type = TaskDAO,
    ) -> None:
        super().__init__(config)
        self._conn = conn
        self._task_dao = task_dao

    def process(self, task: Task) -> ProcessResult:
        if not task.input_generator_code:
            return self._route_to_final(
                task, "exhausted_rounds", "task 级生成器缺失"
            )

        task.current_input_gen_code = task.input_generator_code
        self._task_dao.update_step(self._conn, task.id, "pbt")
        self._conn.commit()
        return ProcessResult(
            next_stage=NEXT_PBT,
            task=task,
            diagnostic="InputGen: 使用 task 级生成器 → PBT",
        )

    def _route_to_final(
        self,
        task: Task,
        failure_stage: str,
        reason: str,
    ) -> ProcessResult:
        task.pending_termination = failure_stage
        self._task_dao.update_step(self._conn, task.id, "final")
        self._conn.commit()
        _logger.info(
            "InputGen[%s] 终止: %s（%s）",
            task.id,
            failure_stage,
            reason,
        )
        return ProcessResult(
            next_stage=NEXT_FINAL,
            task=task,
            diagnostic=f"terminated: {reason}",
        )

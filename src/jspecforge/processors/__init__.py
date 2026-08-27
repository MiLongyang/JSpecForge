"""JSpecForge 处理器模块（P6）。

公开 6 个 processor + 共享基础设施：

- :class:`BaseProcessor`：处理器基类
- :class:`ProcessResult`：单步处理结果（含 ``next_stage`` + ``task`` +
  ``diagnostic``）
- ``NEXT_*`` 常量：合法的 ``next_stage`` 取值

P6 阶段会逐步填充以下处理器（首批 :mod:`jspecforge.processors.base` 已就绪）：

- :class:`TaskAProcessor`：生成 / 修复 JML 规约
- :class:`OJProcessor`：OpenJML ESC 静态验证
- :class:`RACProcessor`：RAC 编译插桩
- :class:`InputGenProcessor`：生成 / 修复 PBT 输入生成器
- :class:`PBTProcessor`:  动态测试
- :class:`FinalProcessor`:  最终裁定

**模块依赖**（A → B 表示 A import B）::

    base       → core.task_entity + config.settings
    *_processor → base + db + tools + llm + prompts + utils.jml_utils
                  + processors._format_helpers

**使用边界**（plan §4.3）：

- P7 编排器从队列取 Task → 调 ``processor.process(task)`` → 路由
- ``ProcessResult.next_stage`` 取值见 :data:`NEXT_STAGES`
- ``done_oj_pass`` 是特殊终态（OJProcessor 已 mark_success），P7 路由器
  应识别此值并直接跳出本 task 的循环
"""

from __future__ import annotations

from jspecforge.processors.base import (
    NEXT_DONE,
    NEXT_DONE_OJ_PASS,
    NEXT_FINAL,
    NEXT_INPUT_GEN,
    NEXT_OJ,
    NEXT_PBT,
    NEXT_RAC,
    NEXT_STAGES,
    NEXT_TASK_A,
    BaseProcessor,
    ProcessResult,
)
from jspecforge.processors.final_processor import FinalProcessor
from jspecforge.processors.input_gen_processor import (
    InputGenInfraError,
    InputGenProcessor,
)
from jspecforge.processors.openjml_processor import OJInfraError, OJProcessor
from jspecforge.processors.pbt_processor import PBTInfraError, PBTProcessor
from jspecforge.processors.rac_processor import RACInfraError, RACProcessor
from jspecforge.processors.task_a_processor import TaskAProcessor

__all__ = [
    "BaseProcessor",
    "ProcessResult",
    "NEXT_TASK_A",
    "NEXT_OJ",
    "NEXT_RAC",
    "NEXT_INPUT_GEN",
    "NEXT_PBT",
    "NEXT_FINAL",
    "NEXT_DONE_OJ_PASS",
    "NEXT_DONE",
    "NEXT_STAGES",
    "TaskAProcessor",
    "OJProcessor",
    "OJInfraError",
    "RACProcessor",
    "RACInfraError",
    "InputGenProcessor",
    "InputGenInfraError",
    "PBTProcessor",
    "PBTInfraError",
    "FinalProcessor",
]

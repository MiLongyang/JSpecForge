"""队列管理器（P7.0）。

设计依据:

- ``docs/design/系统架构与调度设计.md`` §三 Queue 与 Processor 对照表
- ``docs/design/系统架构与调度设计.md`` §五 队列拓扑与任务流转
- ``docs/plan/P6-处理器.md`` §6.6 P7 待做清单

**职责**:

提供 6 个内存队列(``queue.Queue``)的统一访问入口,Router 投递、Worker
取任务、Monitor 查 ``qsize()`` 都通过本类。

**6 个队列**(与 :data:`processors.base.NEXT_STAGES` 中除终态以外的 6 个值
一一对应):

==============  ==========================
队列名          对应 ``next_stage`` 值
==============  ==========================
``task_a``      :data:`NEXT_TASK_A`
``oj``          :data:`NEXT_OJ`
``rac``         :data:`NEXT_RAC`
``input_gen``   :data:`NEXT_INPUT_GEN`
``pbt``         :data:`NEXT_PBT`
``final``       :data:`NEXT_FINAL`
==============  ==========================

终态 ``done_oj_pass`` / ``done`` **不**对应队列(由 Router 直接丢弃)。

**线程安全**: ``queue.Queue`` 自带线程安全,本类只是浅封装,无额外锁。
"""

from __future__ import annotations

import queue
import threading
from typing import Dict, FrozenSet, List, Set, Tuple

from jspecforge.core.task_entity import Task
from jspecforge.processors.base import (
    NEXT_FINAL,
    NEXT_INPUT_GEN,
    NEXT_MT,
    NEXT_OJ,
    NEXT_PBT,
    NEXT_RAC,
    NEXT_TASK_A,
)
from jspecforge.utils.log_utils import get_logger

_logger = get_logger("core.queue_manager")

#: 6 个有队列的 stage 名称(与 next_stage 取值一致)
QUEUE_STAGES: FrozenSet[str] = frozenset(
    {NEXT_TASK_A, NEXT_OJ, NEXT_RAC, NEXT_INPUT_GEN, NEXT_PBT, NEXT_MT, NEXT_FINAL}
)


class QueueManager:
    """6 个队列的统一管理器。

    使用方式::

        qm = QueueManager()
        qm.put(NEXT_TASK_A, task)              # Router 投递
        task = qm.get(NEXT_OJ, timeout=1)      # Worker 取任务
        snapshot = qm.qsize_snapshot()         # Monitor 查深度

    所有方法线程安全。
    """

    def __init__(self) -> None:
        """初始化 6 个独立的 ``queue.Queue``(无界)。"""
        self._queues: Dict[str, queue.Queue[Task]] = {
            stage: queue.Queue() for stage in QUEUE_STAGES
        }
        self._in_queue_lock = threading.Lock()
        self._in_queue_ids: Set[int] = set()
        _logger.info("QueueManager initialized with %d queues", len(self._queues))

    # ------------------------------------------------------------------
    # Router / Worker 接口
    # ------------------------------------------------------------------

    def put(self, stage: str, task: Task) -> None:
        """把 task 投递到指定队列。

        Args:
            stage: 目标队列名(``QUEUE_STAGES`` 之一)。
            task: 待投递的 Task。

        Raises:
            ValueError: ``stage`` 不在 ``QUEUE_STAGES`` 中。
        """
        if stage not in self._queues:
            raise ValueError(
                f"未知 stage: {stage!r},合法值: {sorted(QUEUE_STAGES)}"
            )
        with self._in_queue_lock:
            if task.id in self._in_queue_ids:
                _logger.warning(
                    "Task[%d] 重复入队被拦截 (target=%s, current_step=%s)",
                    task.id,
                    stage,
                    task.current_step,
                )
                return
            self._in_queue_ids.add(task.id)
        self._queues[stage].put(task)

    def get(self, stage: str, timeout: float = 1.0) -> Task:
        """从指定队列取一个 task(阻塞)。

        Args:
            stage: 队列名。
            timeout: 阻塞超时秒数。超时抛 ``queue.Empty``。

        Returns:
            队列中的 Task。

        Raises:
            ValueError: 未知 stage。
            queue.Empty: 超时无 task。
        """
        if stage not in self._queues:
            raise ValueError(
                f"未知 stage: {stage!r},合法值: {sorted(QUEUE_STAGES)}"
            )
        task = self._queues[stage].get(timeout=timeout)
        with self._in_queue_lock:
            self._in_queue_ids.discard(task.id)
        return task

    def task_done(self, stage: str) -> None:
        """通知队列一个 task 处理完毕(配合 ``join()`` 用)。

        Args:
            stage: 队列名。
        """
        if stage in self._queues:
            self._queues[stage].task_done()

    # ------------------------------------------------------------------
    # Monitor 接口
    # ------------------------------------------------------------------

    def qsize(self, stage: str) -> int:
        """返回指定队列当前深度(近似值)。

        Args:
            stage: 队列名。

        Returns:
            当前 ``qsize()``。``queue.Queue.qsize()`` 是近似值,适合 monitor
            展示,不应用于业务判断。
        """
        if stage not in self._queues:
            return 0
        return self._queues[stage].qsize()

    def qsize_snapshot(self) -> Dict[str, int]:
        """返回 6 个队列的深度快照(给 monitor 用)。

        Returns:
            ``{stage_name: qsize}`` 字典。
        """
        return {stage: q.qsize() for stage, q in self._queues.items()}

    def total_pending(self) -> int:
        """所有队列深度之和(给 monitor 看总待处理任务)。"""
        return sum(q.qsize() for q in self._queues.values())

    # ------------------------------------------------------------------
    # 调试 / 优雅停机
    # ------------------------------------------------------------------

    def drain_all(self) -> List[Tuple[str, Task]]:
        """**测试用**: 清空所有队列,返回 (stage, task) 列表。

        生产代码不应调用——优雅停机时让 worker 自然处理完队列即可。
        """
        drained: List[Tuple[str, Task]] = []
        for stage, q in self._queues.items():
            while True:
                try:
                    task = q.get_nowait()
                except queue.Empty:
                    break
                drained.append((stage, task))
        return drained

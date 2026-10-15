"""A2A task 账本域（§5.1 类手术第二刀，自 ``adapters/a2a.py`` 抽出）。

负责入站 task 的登记 / 查询 / FIFO 裁剪 / 终态判定与停机清账；
``_Task``（内存里的一个入站 A2A task）随本域走，``a2a`` 顶部再进口保持
``a2a._Task`` 可解析（行为零变化）。

**状态为什么留在适配器**：``_tasks`` / ``_order`` / ``_lock`` 的纪律跨域共享 ——
留守侧的轮次计数（``_bump_turn`` / ``reset_turns``）与 ``send`` / ``stats`` /
``_rpc_list_tasks`` 都用同一把锁、同一份账；测试也直戳 ``adapter._tasks``。
状态搬走 ⇒ 留守方法全部改字节 ⇒ 违反零字节纪律。

**协作者为什么是逐调用转发的 read-only property**：第九刀（telegram 凭据域）
初版「构造时注入绑定可调用」被定向复跑证伪（构造后打桩全部失明）；本域沿用
同一定案 —— property 每次求值都回 ``self._adapter.X`` 取，与手术前动态解析
语义一致，方法体得以零字节迁移。
"""

from __future__ import annotations

import collections
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from .a2a_protocol import STATE_SUBMITTED, TERMINAL_STATES, _new_id

if TYPE_CHECKING:
    from .a2a import A2aAdapter

logger = logging.getLogger("opencode_bridge.adapters.a2a")


@dataclass
class _Task:
    """内存里的一个入站 A2A task（请求 / 回答的关联体）。

    **只在内存里**：见模块 docstring「消息与对端的对应关系」一节对重启后果的说明。
    """

    task_id: str
    context_id: str
    peer: str
    #: 等待 agent 答复的信号。**用 :class:`threading.Event` 而不是 ``Future``**：
    #: ``stop()`` 能立刻 ``set()`` 它唤醒所有阻塞中的 HTTP handler 线程，而
    #: ``Future.result(timeout=...)`` 只能靠轮询才能被停机打断。
    done: threading.Event = field(default_factory=threading.Event)
    state: str = STATE_SUBMITTED
    reply: str = ""
    created_at: float = 0.0


class A2aTaskLedger:
    """task 账本：登记 / 查询 / 裁剪 / 终态判定 / 停机清账。

    状态留在适配器本体（见模块 docstring）；本类只持方法域。
    """

    def __init__(self, adapter: "A2aAdapter") -> None:
        #: 宿主适配器；协作者经 read-only property 逐调用转发。
        self._adapter = adapter

    @property
    def _lock(self):
        """跨域共享的那把锁（账本域与留守侧共同持锁）。"""
        return self._adapter._lock

    @property
    def _tasks(self):
        """``task_id -> _Task`` 账本本体。"""
        return self._adapter._tasks

    @property
    def _order(self):
        """``peer -> deque[task_id]`` FIFO 顺序表。"""
        return self._adapter._order

    @property
    def max_tasks(self):
        """内存里保留的 task 记录上限（闸门留在适配器构造里）。"""
        return self._adapter.max_tasks

    # ------------------------------------------------------------------
    # 以下方法自 A2aAdapter 字节级迁入（顺序未动）
    # ------------------------------------------------------------------
    def _lookup(self, task_id: str, peer: str) -> Optional[_Task]:
        """按 id 取 task，且**必须属于本对端**（§13.1）。

        刻意不返回"存在但不是你"的区分信息 —— §3.3.2 明说服务器 *SHOULD NOT*
        distinguish between "does not exist" and "not authorized"。
        """
        key = str(task_id or "").strip()
        if not key:
            return None
        with self._lock:
            task = self._tasks.get(key)
        if task is None or task.peer != peer:
            return None
        return task

    def _new_task(self, context_id: str, peer: str) -> _Task:
        return _Task(
            task_id=_new_id("task"),
            context_id=context_id,
            peer=peer,
            created_at=time.time(),
        )

    def _register(self, task: _Task) -> None:
        """把 task 记进账本（**幂等**）。

        幂等是必需的：``returnImmediately`` 那条路径会先同步登记一次，好让对端
        紧接着的 ``GetTask`` 能查到；分发线程随后再登记一次 —— 不幂等就会在
        ``_order`` 里塞进**两个**相同的 id，FIFO 顺序和条数统计全乱。
        """
        with self._lock:
            if task.task_id in self._tasks:
                return
            self._tasks[task.task_id] = task
            self._order.setdefault(task.peer, collections.deque()).append(task.task_id)
            self._trim_locked()

    def _trim_locked(self) -> None:
        """超上限时按 FIFO 丢最老的**终态**记录（调用方持锁）。

        只丢终态：在飞 task 被丢会让等待中的 HTTP 请求永远醒不过来。
        """
        limit = max(8, int(self.max_tasks))
        while len(self._tasks) > limit:
            victim = next(
                (t.task_id for t in self._tasks.values()
                 if t.state in TERMINAL_STATES),
                None,
            )
            if victim is None:
                return                      # 全在飞：宁可不裁也不制造悬挂
            self._drop_locked(victim)

    def _drop_locked(self, task_id: str) -> None:
        task = self._tasks.pop(task_id, None)
        if task is None:
            return
        queue_ = self._order.get(task.peer)
        if queue_ is not None:
            try:
                queue_.remove(task_id)
            except ValueError:
                pass
            if not queue_:
                self._order.pop(task.peer, None)

    def _finalize(self, task: _Task, state: str, reply: str = "") -> bool:
        """把 task 推到终态并唤醒等待者。**幂等**（第一个终态说了算）。"""
        with self._lock:
            if task.state in TERMINAL_STATES and task.done.is_set():
                return False
            task.state = state
            task.reply = reply or ""
            task.done.set()
        return True

    def _fail_pending(self, state: str, reason: str) -> int:
        """停机 / 关服时把所有未完成 task 判终态，唤醒阻塞中的 handler 线程。"""
        with self._lock:
            pending = [t for t in self._tasks.values()
                       if t.state not in TERMINAL_STATES]
        count = 0
        for task in pending:
            if self._finalize(task, state, reason):
                count += 1
        if count:
            logger.info("a2a: %d 个等待中的任务被判为 %s（%s）", count, state, reason)
        return count

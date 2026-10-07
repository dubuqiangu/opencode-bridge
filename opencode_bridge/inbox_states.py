"""入站收件箱的行**状态词汇表**（G2）。

为什么单独成文件
----------------
:class:`DeliveryState` 是**纯字符串常量类**，而现在有两个模块要读它：
持久化层 :mod:`opencode_bridge.inbox` 与行数治理层 :mod:`opencode_bridge.inbox_row_cap`。
住在任何一边都会成环 —— 上游实测过：写在 ``inbox.py`` 里时，
``inbox_row_cap`` import 它就会拿到"partially initialized module"。

⚠️ 上一版试过"在 :mod:`opencode_bridge.inbox` 的方法体里延迟 import"，那只是把环
藏起来：import 期的错误从"立刻炸"变成"第一次收到消息时才炸"，更难查。共享的词汇表
就该有自己的模块 —— 这比绕开它便宜。
"""

from __future__ import annotations

__all__ = ["DeliveryState"]


class DeliveryState:
    """收件箱一行的状态。纯字符串常量，便于直接落进 SQLite 与日志。"""

    #: 已落盘、尚未尝试投递。**未了结** —— 复现必安全。
    PENDING = "pending"
    #: 正在投递。**落在这一行的崩溃 = 结果不可知**，只告警不重放。**未了结** ——
    #: 恢复层对它只告警、绝不重放，所以淘汰它换不来任何补偿。
    ATTEMPTING = "attempting"
    #: 结果不可知，**而且是已经记下来的那种**：请求已经交给 opencode，而失败发生在
    #: **传输层**（超时 / 连接被拒 / 流中断）⇒ 远端**是否收到这件事从未被记录**。
    #: **未了结** —— 恢复层对它与 :attr:`ATTEMPTING` 同等待遇：**只告警、绝不重放**。
    #:
    #: ⚠️ 与 :attr:`ATTEMPTING` 的差别在**谁**发现的：那一行是进程**死在里面**，
    #: 这一行是进程**活着**、并且当场就知道自己不知道（用户 2026-10-07 拍板）。
    #: ⛔ **绝不许**在「拿不到 status」时回落成 :attr:`FAILED` —— 那是把"不知道"
    #: 记成"一定没送到"，恢复层就会按退避阶梯重放它 ⇒ agent 对同一条指令跑两遍
    #: （AGENTS.md §8 第 3 条：恢复一份从来没记录过的信息 = 猜）。
    #: ⚠️ 代价**必须知情**：不重放 ⇒ 远端**真**没收到时那条指令**丢了** ⇒ 那条告警
    #: **必须**同时写「请重新发送一次」，否则等于把负担转给用户却不告诉他该做什么
    #: （而收件箱存在的理由正是"别丢这条"）。
    OUTCOME_UNKNOWN = "outcome_unknown"
    #: 投递成功。这一行同时是平台重投时的**回执**。**已了结**。
    DELIVERED = "delivered"
    #: 投递明确失败，等 ``not_before`` 到期后重放。**未了结** —— 它还会被投递。
    FAILED = "failed"
    #: 重试预算耗尽。终态 + 一次告警。**已了结**。
    ABANDONED = "abandoned"

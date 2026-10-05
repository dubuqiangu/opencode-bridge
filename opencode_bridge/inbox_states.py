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
    #: 投递成功。这一行同时是平台重投时的**回执**。**已了结**。
    DELIVERED = "delivered"
    #: 投递明确失败，等 ``not_before`` 到期后重放。**未了结** —— 它还会被投递。
    FAILED = "failed"
    #: 重试预算耗尽。终态 + 一次告警。**已了结**。
    ABANDONED = "abandoned"

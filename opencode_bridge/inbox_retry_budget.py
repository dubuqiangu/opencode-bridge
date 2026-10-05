"""入站收件箱的**重试预算**（G2）：退避阶梯、尝试次数上限，以及钉死两者关系的那条不变量。

为什么单独成文件
----------------
**两个**模块要读这份预算，所以它不能住在任何一边：

* 持久化层 :mod:`opencode_bridge.inbox` 的
  :meth:`~opencode_bridge.inbox.InboundInbox.mark_failed` —— 拿阶梯算出要写进
  ``not_before`` 的期限，拿次数上限决定"这次失败是不是最后一次"；
* 恢复层 :mod:`opencode_bridge.inbox_recovery` —— 用户可见告警里"重试 N 次"的
  那个 ``N`` 就是 :data:`MAX_ATTEMPTS`。

⚠️ 定义权**在这里**、不在任何一层的策略里。两边对"还差几次预算"必须只有**一个**
答案：任何一边自己再算一遍，就会出现"以为还有预算、其实早已耗尽"这种**只在生产里
显形**的分歧（因为单测里两边读的是同一个字面量，谁都发现不了）。

⚠️ 上一版这两个常量住在 :mod:`opencode_bridge.inbox` 里，而恢复层要 import 它们就得
**穿过收件箱这一层** —— 收件箱与策略无关，它只是恰好是定义所在地。现在两个模块各自
import 本模块，依赖方向从"恢复层 → 收件箱 → （本该是词汇表）"变成"两边 → 词汇表"。

退避阶梯为什么住在**持久化层**
----------------------------
:meth:`~opencode_bridge.inbox.InboundInbox.mark_failed` 要写出 ``not_before``，
所以"还差几次预算"这个判断必须发生在**那里** —— 否则恢复层唯一能看到的东西
:class:`~opencode_bridge.inbox.QueuedPrompt` 不带 ``attempts``，恢复层无从判断该不该
放弃。这个状态就是一行数据库记录，跨进程重启后必须还在，所以它属于持久化层而不是
策略层。

⚠️ 这段话**不**是说阶梯可以搬去策略层：搬走的是**常量与它们之间的关系**（本模块），
留在 :meth:`InboundInbox.mark_failed` 原地的是**判断**（读完 ``attempts``、自增、
判终态、排下一级）。判断留在写入侧，因为它必须和那次 UPDATE 在同一条连接上。
"""

from __future__ import annotations

__all__ = ["BACKOFF_LADDER_SECONDS", "MAX_ATTEMPTS"]

#: 重试退避阶梯（秒）。固定阶梯、**不用指数**，与 hermes-agent 的
#: ``gateway/delivery_ledger.py``（commit ``8a5edab282632443``）一致。
#:
#: ⚠️ 阶梯是**期限**（写进 ``not_before``）而不是 sleep：这才是它重启安全的原因 ——
#: 一个进程重启不会把等待重置一遍，也不会有半个 sleep 睡在内存里。
#:
#: 级数与 :data:`MAX_ATTEMPTS` **必须相等减一** —— ``MAX_ATTEMPTS`` 次尝试之间只隔
#: ``MAX_ATTEMPTS - 1`` 次等待。这条约束是照抄 hermes 的：
#:
#:     _RETRY_BACKOFF_SECONDS = (30.0, 120.0)
#:     assert len(_RETRY_BACKOFF_SECONDS) == MAX_ATTEMPTS - 1
#:
#: 之前这里写成三级（30/120/600）配 ``MAX_ATTEMPTS = 3``，是**派单规格里的算术错**
#: （抄了公式却没抄它隐含的级数）：``mark_failed`` 自增后的 ``attempts`` 从 1 起，
#: 而放弃条件是 ``attempts >= MAX_ATTEMPTS``，于是只有 ``attempts`` 为 1 和 2 时
#: 会排重试 —— 三级阶梯的第 0 级（30 秒）**永远触发不到**，首次重试被白白多等了
#: 90 秒（120s 而不是 30s）。
BACKOFF_LADDER_SECONDS: tuple[float, ...] = (30.0, 120.0)

#: 最多花掉几次投递尝试。第 ``MAX_ATTEMPTS`` 次失败就转 ``abandoned``，
#: **绝不**再排下一次重试（理由见 :meth:`InboundInbox.mark_failed`）。
MAX_ATTEMPTS: int = 3

assert len(BACKOFF_LADDER_SECONDS) == MAX_ATTEMPTS - 1, (
    "MAX_ATTEMPTS 次尝试之间只隔 MAX_ATTEMPTS - 1 次等待，"
    "所以阶梯级数必须正好等于 MAX_ATTEMPTS - 1；"
    "多给几级不会更安全——超出部分的级永远轮不到，"
    "少给则最后一次重试会复用更早的期限，阶梯就白写了"
)

"""``--status`` 里「事件流订阅」那一段的**渲染**（纯函数：不联网、不读盘、不造线程）。

这一段答哪个问题
================

**「``/api/event`` 那条 SSE 线程此刻怎么样？」** —— 与 ``--status`` 里其它几段
**都不是同一个问题**：

* 「渠道配置与能力」答「凭据齐不齐」。⚠️ 那一列来自
  :meth:`opencode_bridge.adapters.base.Adapter.capabilities`，它是**平台能力声明**、
  **刻意不看**这条线程 ⇒ 能力与健康是两件事，⛔ 不许拿它当健康指标。
* 「上次启动时的探测结论」「上次出站失败」答**过去某一刻**发生了什么。
* **这一段**答**此刻那条线程还活着没有**。

为什么它存在
============

``fix-289`` 之前，``/api/event`` 的任何非 200 都会让那条线程**永久结束**（此后
进程余下的全部答复静默丢失），而 ``--status`` 照常报平台正常 ⇒ **「线程已经死了」
在视图里一个字都看不出来**。⚠️ 那条缺陷本身已由
:mod:`opencode_bridge.subscription_supervisor` 修好（**有界退避后重新订阅**），
但「现在是什么状态」**没有任何一条通道到用户** —— 只有一行日志。
⇒ 本模块把那份**不可变**快照
:class:`~opencode_bridge.subscription_supervisor.SubscriptionStatus` 说成人话。

「正在重连」与「线程已经死了」怎么分开
======================================

⛔ **判别靠的是一对属性**，不是单看 ``phase``：两个相里
``(terminated_without_stop, recovering)`` 分别是 ``(True, False)`` 与
``(False, True)`` ⇒ 见 :func:`subscription_display_state`（它**先问这一对**）。

⚠️ 而**只有** ``terminated_without_stop`` 那一相 = 线程**结束且不会自己回来**
⇒ 那才是**需要人管**的一种（重启桥才恢复）。``reconnecting`` 则是**线程还活着**、
只是此刻**不在收事件** ⇒ 用户需要知道「正在等」，⛔ 但⛔ 不许显示成正常。

⚠️ **认不出来的 ``phase`` 一律落到** :data:`SUBSCRIPTION_DISPLAY_UNRECOGNISED`，
**绝不当成正常** —— 「读不懂」与「正常」在数据上长得一样，正是 AGENTS.md §7.1
记过的那次亏。

⚠️ ``subscription_status`` 为 ``None``（**今天的生产情形**：``--status`` 是独立进程，
拿不到桥进程内的对象）⇒ 那一段**明确说「读不到」**，而⛔ 不拿「读不到」说成正常 ——
见 :data:`NO_LIVE_SNAPSHOT_TEXT`。

⚠️ 本模块**不许**发网络请求、不许读盘、不许造线程：``--status`` 必须是「网络坏了
也能看」的那条路（与 :mod:`opencode_bridge.health` 同一纪律）。
"""

from __future__ import annotations

from .subscription_supervisor import (
    PHASE_ENDED_WITHOUT_STOP,
    PHASE_IDLE,
    PHASE_RECONNECTING,
    PHASE_STOPPED_BY_REQUEST,
    PHASE_STREAMING,
    SubscriptionStatus,
)

__all__ = [
    "EVENT_SUBSCRIPTION_SECTION_HEADER",
    "NO_LIVE_SNAPSHOT_TEXT",
    "NO_SNAPSHOT_MISSING_ERROR_TEXT",
    "RECONNECTING_CLAUSE",
    "STREAMING_CLAUSE",
    "SUBSCRIPTION_DISPLAY_NOT_STARTED",
    "SUBSCRIPTION_DISPLAY_RECOVERING",
    "SUBSCRIPTION_DISPLAY_STOPPED_BY_REQUEST",
    "SUBSCRIPTION_DISPLAY_STREAMING",
    "SUBSCRIPTION_DISPLAY_TERMINATED",
    "SUBSCRIPTION_DISPLAY_UNRECOGNISED",
    "TERMINATED_CLAUSE",
    "render_subscription_status",
    "subscription_display_state",
]

#: ``--status`` 里这一段的**段标题**（调用方用它切段，所以它是模块常量而不是字面量）。
EVENT_SUBSCRIPTION_SECTION_HEADER = "== 事件流订阅 =="

#: 六个**封闭**的显示档位。⚠️ 取值与 :class:`SubscriptionStatus` 的五个 phase 一一对应，
#: 另加一档兜底（:data:`SUBSCRIPTION_DISPLAY_UNRECOGNISED`）——
#: 「读不懂」必须有自己的档位，否则它会落进 :data:`SUBSCRIPTION_DISPLAY_STREAMING`，
#: 而那正是本视图存在的理由要消灭的那类假话。
SUBSCRIPTION_DISPLAY_STREAMING = "streaming"
SUBSCRIPTION_DISPLAY_RECOVERING = "recovering"
SUBSCRIPTION_DISPLAY_TERMINATED = "terminated_without_stop"
SUBSCRIPTION_DISPLAY_NOT_STARTED = "not_started"
SUBSCRIPTION_DISPLAY_STOPPED_BY_REQUEST = "stopped_by_request"
SUBSCRIPTION_DISPLAY_UNRECOGNISED = "unrecognised"

#: 下面三个是**关键句**：断言钉的是它们，而不是整段输出（``--status`` 是纯文本，
#: 整段相等会随任何措辞调整全红）。⚠️ 它们同时是**这份模块与它的调用方之间唯一的措辞
#: 契约** —— 改它们要一起看两侧的断言。
STREAMING_CLAUSE = "订阅正常，正在收事件"
RECONNECTING_CLAUSE = "正在重连 —— 线程还活着，此刻不在收事件"
TERMINATED_CLAUSE = "⚠️ 线程已经结束、不会自己回来（需要人管：重启桥才会恢复）"

_NOT_STARTED_CLAUSE = "这条线程还没进过订阅（桥的订阅线程没起，或已收工）"
_STOPPED_CLAUSE = "这条订阅已被正常叫停（正常关机路径，不是故障）"
_UNRECOGNISED_CLAUSE = (
    "认不出这份订阅状态的 phase（不是本模块认识的五档之一）—— ⛔ 这**不是**「事件流正常」的证据"
)

#: **拿不到活快照**时逐字显示的文案（``--status`` 是独立进程 ⇒ 今天的生产情形）。
#:
#: ⚠️ 它**既不说正常、也不说坏**：它说的是「这一段答不了这个问题」。
#: ⛔ 把它写成「未发现异常」就是本视图存在的理由本身。
NO_LIVE_SNAPSHOT_TEXT = (
    "本进程读不到桥进程内的那条订阅线程（--status 是独立进程）"
    "—— ⛔ 这一段因此**不构成**「事件流正常」的证据"
)

#: 快照压根没有 ``last_error`` 时那一行逐字显示的文案。
#: ⚠️ 它**不能**写成「无失败」（没有记录时分不出「没失败过」与「这次没记下来」）
#: —— 与 :data:`opencode_bridge.health.NO_OUTBOUND_FAILURE_TEXT` 同一个纪律。
#: ⛔ 也不许说「没成功订阅过」：那要读 ``subscriptions_started`` 才成立，
#: 而这一档**可能**只是还没有哪一次失败被记下来。
NO_SNAPSHOT_MISSING_ERROR_TEXT = "无记录（这一份快照没有记下失败原因）"

#: 「上一次失败」那一行的**前缀**。⚠️ 说成「上一次为什么坏」的档案、⛔ 不是「现在还坏着」。
_LAST_ERROR_LABEL = "上一次失败"


def subscription_display_state(status: SubscriptionStatus) -> str:
    """把一份快照归到 :data:`SUBSCRIPTION_DISPLAY_*` 里的一档（**全函数**，不抛）。

    判别顺序是**承重的**：先问那一对判别属性，再看 ``phase``：

    1. ``terminated_without_stop`` ⇒ 线程**已经结束、不会自己回来**（⚠️ 需要人管）；
    2. ``recovering`` ⇒ 正在退避后重新订阅（**线程还活着**，只是此刻不在收事件）；
    3. 否则按 ``phase`` 取它自己那一档；
    4. **认不出来 ⇒** :data:`SUBSCRIPTION_DISPLAY_UNRECOGNISED`（⛔ 绝不当成正常）。
    """
    if status.terminated_without_stop:
        return SUBSCRIPTION_DISPLAY_TERMINATED
    if status.recovering:
        return SUBSCRIPTION_DISPLAY_RECOVERING
    if status.phase == PHASE_STREAMING:
        return SUBSCRIPTION_DISPLAY_STREAMING
    if status.phase == PHASE_IDLE:
        return SUBSCRIPTION_DISPLAY_NOT_STARTED
    if status.phase == PHASE_STOPPED_BY_REQUEST:
        return SUBSCRIPTION_DISPLAY_STOPPED_BY_REQUEST
    return SUBSCRIPTION_DISPLAY_UNRECOGNISED


def _verdict_sentence(status: SubscriptionStatus | None) -> list[str]:
    """状态那一行（或那几行）的正文：一句**非空**的人话。

    ⚠️ 「正在重连」带上第几次重连 —— 用户要判断的是「等了一会儿还在等」还是
    「已经在第 7 次了」，而这两个数只差一个字段。
    """
    if status is None:
        return [NO_LIVE_SNAPSHOT_TEXT]
    display_state = subscription_display_state(status)
    if display_state == SUBSCRIPTION_DISPLAY_RECOVERING:
        return [RECONNECTING_CLAUSE + "（第 " + str(status.reconnect_attempts) + " 次）"]
    if display_state == SUBSCRIPTION_DISPLAY_TERMINATED:
        return [TERMINATED_CLAUSE]
    if display_state == SUBSCRIPTION_DISPLAY_STREAMING:
        return [STREAMING_CLAUSE]
    if display_state == SUBSCRIPTION_DISPLAY_NOT_STARTED:
        return [_NOT_STARTED_CLAUSE]
    if display_state == SUBSCRIPTION_DISPLAY_STOPPED_BY_REQUEST:
        return [_STOPPED_CLAUSE]
    return [_UNRECOGNISED_CLAUSE]


def render_subscription_status(status: SubscriptionStatus | None) -> list[str]:
    """返回 ``--status`` 里「事件流订阅」那一段的**全部**行（含段标题）。

    ⚠️ **每一行都自带缩进**，调用方只管原样 ``print`` —— 渲染与装配分开是
    ``exp-68`` 裁定的那条线（``__main__`` 只留装配），所以本函数不假设调用方会加前缀。

    :param status: :meth:`~opencode_bridge.event_stream.EventStream.subscription_status`
        给出的那份**不可变**快照；``None`` = 本进程拿不到（``--status`` 是独立进程）。
    """
    lines = [
        "",
        EVENT_SUBSCRIPTION_SECTION_HEADER,
        "  说明：这一段答「事件流（/api/event）那条线程**此刻**怎么样」，与上面几段都",
        "        **不是**同一个问题：「渠道配置与能力」答凭据齐不齐，「上次启动时的",
        "        探测结论」/「上次出站失败」答**过去某一刻**发生了什么。",
        "  ⚠️ 上面那张能力表全绿**不代表**这一段会绿 —— 能力与健康是两件事。",
    ]
    for sentence in _verdict_sentence(status):
        lines.append("  状态        : " + sentence)
    if status is None:
        # ⛔ 没有快照就没有计数器可写 —— 编一个「订阅 0 次 / 收到 0 帧」会是
        # 「伪造观测」（AGENTS.md §8），而它与「真的订阅过 0 次」在数据上分不出来。
        return lines
    lines.append(
        "  累计        : 开始订阅 "
        + str(status.subscriptions_started)
        + " 次 · 宣布重连 "
        + str(status.reconnect_attempts)
        + " 次 · 收到 "
        + str(status.frames_received)
        + " 帧"
    )
    lines.append(
        "  " + _LAST_ERROR_LABEL + "  : "
        + (status.last_error or NO_SNAPSHOT_MISSING_ERROR_TEXT)
    )
    lines.append(
        "  注：上面那一行是**上一次**为什么坏的档案，⛔ 不代表此刻还坏着；"
        "而「正在重连」也不代表它已经好了。"
    )
    return lines

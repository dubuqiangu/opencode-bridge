"""崩溃恢复策略（G2）：把收件箱里的行变回"用户真的收到了回复"。

纯策略、可注入
--------------
本模块不 import opencode 客户端，也不碰磁盘（除了通过传进来的
:class:`~opencode_bridge.inbox.InboundInbox`），所以整份逻辑可以拿一个假收件箱
单测 —— :mod:`tests.test_inbox_recovery` 就是这么干的。投递与告警都是**注入的
callable**，本模块对它们一无所知。

分档语义（用户 2026-10-03 拍板、2026-10-07 补出第三档）
------------------------------------------------------
先说为什么这里有一个**刻意的不对称**：只要「请求已经交给 opencode」而我们**没拿到
它的答复**，opencode 就可能已经处理、也可能没处理 —— 结果**真的不可知**。用户被摆上
了这个取舍，而这样的窗口有**两处**（崩溃进去的那一处，与传输层失败的那一处）：

==============================  ==========================================
行状态                          处理
==============================  ==========================================
``pending``（从未尝试）          **直接重放**，不可能重复，不告警
``failed``（明确失败）          按退避阶梯**重放**（用户已见过 ``发送失败: …``）
``attempting``（进程死在里面）  **只告警，绝不重放**
``outcome_unknown``（传输失败） **只告警，绝不重放**
``abandoned``（预算耗尽）       终态，告警一次
==============================  ==========================================

⚠️ **``attempting`` 与 ``outcome_unknown`` 绝不重放，这是用户拍板的，不是可以顺手
"改进"的地方。** 当前这个 bug 丢一条消息；改法若会罕见地让 agent 动两次，在 coding
场景下重复未必是较小的那个恶 —— 重复的文件编辑、重复的 ``git`` 操作、重复的长构建。

⚠️ 而"不重放"的**代价**必须知情，**也必须说给用户听**：远端**真**没收到时那条指令
**丢了**，而收件箱存在的理由正是"别丢这条"。⇒ :func:`_uncertain_alert_text` **必须**
同时写「请重新发送一次」；⛔ 只记一条"结果未知"等于把负担转给用户、却不告诉他该做什么。

⚠️ ``outcome_unknown`` 那一条的来历见
:attr:`~opencode_bridge.inbox.DeliveryState.OUTCOME_UNKNOWN`；它与 ``attempting``
在**恢复层**没有区别（同一批行、同一句告警、同样不重放），差别只在"谁发现的"。

⚠️ 而 :func:`_replay_one` 是这一档的**第二个**产地：启动重放时投递侧抛出的异常若
"可能 agent 已经跑过"，那一行就落 ``outcome_unknown`` 而不是 ``failed``
（:func:`_agent_may_have_run` 判据）⇒ 而它**当轮**就会被第 3 步读出来告警，
因为第 3 步跑在第 1/2 步**之后**。

告警文字的纪律
--------------
告警**只**通过 ``notify`` 发给用户，**绝不**拼进 ``prompt.text`` ——
那段文字会进入 agent 的上下文，污染它可能直接改变 agent 在重复任务上的行为。

不许抛异常
----------
:func:`recover_pending` 里每一次对外调用（读收件箱、写状态、投递、发告警）都被
单独兜住：恢复失败**绝不能**让桥起不来。这里没有 sleep —— 退避是收件箱里的期限
（``not_before``），不是内存里的等待，所以重启不会把等待重置一遍。
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

from .inbox import (
    BACKOFF_LADDER_SECONDS,
    MAX_ATTEMPTS,
    InboundInbox,
    QueuedPrompt,
)

__all__ = [
    "BACKOFF_LADDER_SECONDS",
    "MAX_ATTEMPTS",
    "RecoveryOutcome",
    "mark_prompt_never_sent",
    "recover_pending",
]

logger = logging.getLogger("opencode_bridge.inbox_recovery")

# ⚠️ ``MAX_ATTEMPTS`` 与 ``BACKOFF_LADDER_SECONDS`` 是从 :mod:`opencode_bridge.inbox`
# **原样再导出**的，定义权在那里不在这里：:meth:`InboundInbox.mark_failed` 要用阶梯
# 写 ``not_before``，而 :class:`QueuedPrompt` 不带 ``attempts``，恢复层自己算不出
# "还差几次预算"。放在恢复层会造成循环 import，所以这里只做转发 ——
# 调用方 ``from opencode_bridge import inbox_recovery`` 就能拿到全部策略常量。


@dataclass
class RecoveryOutcome:
    """一次恢复的结果。

    ⚠️ :attr:`uncertain` 与 :attr:`abandoned` **已经发过告警了**（本模块调的
    ``notify``）。它们留在这里只给你做日志 / 状态输出用，**不要**再发一遍。
    """

    #: 本次**成功**重发的。重放失败的不算 —— 它们会变成 ``failed`` 行，
    #: 下次恢复再按阶梯试。
    replayed: list[QueuedPrompt] = field(default_factory=list)
    #: 结果不可知、只告警未重放的。
    uncertain: list[QueuedPrompt] = field(default_factory=list)
    #: 终态（重试预算耗尽），只告警未重放的。
    abandoned: list[QueuedPrompt] = field(default_factory=list)


def _safe_call(getter: Callable[[], list[QueuedPrompt]], *, reading: str) -> list[QueuedPrompt]:
    """取一批条目；取不到就当空批并告警。**不许**把异常抛给调用方。"""
    try:
        return list(getter())
    except Exception:
        logger.exception(
            "inbox recovery: cannot read the %s rows; treating them as absent", reading
        )
        return []


def _safe_action(action: Callable[[], None], *, doing: str) -> bool:
    """做一次收件箱写入。**成功**返回 ``True``；失败只记日志并返回 ``False``
    （那一行会留给下次恢复再处理）。

    ⚠️ 返回值不是装饰：:func:`_replay_one` 要靠它决定**敢不敢投递** ——
    见那里「记不下 ``attempting`` 就不投」那一段。⇒ 把它改成只打印的话，
    那一段的判据就成了恒真的（§9）。
    """
    try:
        action()
        return True
    except Exception:
        logger.exception("inbox recovery: cannot %s; the row stays for the next boot", doing)
        return False


def _uncertain_alert_text(count: int) -> str:
    """结果不可知时的告警。纯用户语言，不含任何内部术语。

    ⚠️ **必须**带「请重新发送一次」，而这不是客套：不重放是用户拍板的（理由见模块
    开头），代价是远端**真**没收到时那条指令**丢了**。⛔ 只说"未知"而不告诉他该做什么，
    等于把负担转给用户却不给方法 —— 而收件箱存在的理由正是"别丢这条"。
    判据：:mod:`tests.test_inbox_recovery` 有一条断言这句话在（删掉它，那条会红）。
    """
    return (
        f"有 {count} 条消息状态未知：请求已经提交给 agent，但没能确认它是否已经处理"
        f"（可能是上次退出正好落在提交的过程中，也可能是提交时连接中断）。"
        f"为避免重复执行副作用（重复改文件、重复 git 操作、重复长时间构建），"
        f"已不自动重发。请检查该会话是否已经处理过；如果没有，请重新发送一次。"
    )


def _abandoned_alert_text(count: int) -> str:
    """重试预算耗尽时的告警。"""
    return (
        f"有 {count} 条消息重试 {MAX_ATTEMPTS} 次后仍然发送失败，已停止自动重试。"
        f"如仍需处理，请重新发送一次。"
    )


def _alert_per_conversation(
    prompts: list[QueuedPrompt],
    notify: Callable[[str, str], None],
    compose_text: Callable[[int], str],
) -> None:
    """按会话合并后告警：每个会话**一条**消息，带上该会话的条数。

    合并的理由：一条会话里压着 5 条未知消息时，发 5 条几乎一样的告警会把真正的
    通知淹掉；而"哪个会话、几条"这两个信息一条就够说清。
    """
    counts = Counter(prompt.conversation_id for prompt in prompts)
    for conversation_id in sorted(counts):
        text = compose_text(counts[conversation_id])
        try:
            notify(conversation_id, text)
        except Exception:
            logger.exception(
                "inbox recovery: cannot alert conversation %s; the user may not see it",
                conversation_id,
            )


#: 异常上标记「prompt **压根没提交**给 agent」的那个属性名。
#:
#: ⚠️ 它存在的理由是 ``status`` **表达不了**这件事。判定「这一行能不能重放」需要
#: 两个信息，而 ``status`` 只带得出其中一个：**服务端给了什么答复**。另一半是
#: 「prompt 有没有真的离开过本机」—— ``create_session`` 阶段的传输失败**也**是
#: ``status=None``，而那一刻 prompt **从没**交出去。
#: ⇒ 只看 ``status`` 就会把「没发出去」说成「不知道发没发出去」，用户因此收到
#: 一句**假话**（"请求已经提交给 agent"——那一刻它根本没提交）。
#: ⇒ 而**猜**是 AGENTS.md §8 第 3 条明令禁止的：正确做法是**把它记下来**。
#:
#: ⛔ **只有确知没提交的那一方才许设它**，而缺省一律按「可能已经跑过」处理 ——
#: 宁可丢一条消息（用户被告知重新发送），也不可让 agent 对同一条指令跑两遍。
_PROMPT_NEVER_SENT_ATTRIBUTE = "agent_may_have_run"


def mark_prompt_never_sent(exc: BaseException) -> BaseException:
    """盖上「prompt 压根没提交」的戳并**原样返回**它（供 ``raise mark_...（exc）``）。

    唯一调用点是 :meth:`~opencode_bridge.inbound_gateway.InboundGateway._dispatch_prompt`
    的恢复路径 —— 而**只有**投递侧知道那一刻 prompt 有没有真的交出去：
    :func:`_agent_may_have_run` 是恢复层的分档判据，它拿到的是一个被压成字符串的
    ``outcome``，那份信息到这里已经没了。

    ⛔ **不许**拿它标「已提交」：那一档由**缺省值**表达。改成显式会让「没设戳」与
    「显式说已提交」变成同一件事，缺省那个安全方向就消失了（§9：恒真的判据比没有
    判据更危险 —— 这里恒真的方向恰好是错的那一侧）。
    """
    setattr(exc, _PROMPT_NEVER_SENT_ATTRIBUTE, False)
    return exc


def _agent_may_have_run(exc: BaseException) -> bool:
    """agent 有没有可能**已经**跑过这条指令（⇒ 该行**绝不许**重放）。

    判据两条，**次序**就是它们的重要性：

    1. 投递侧显式盖了「压根没提交」的戳（:func:`mark_prompt_never_sent`）⇒ 一定没跑过
       —— **哪怕那次失败连 HTTP 状态都没有**（``create_session`` 阶段的传输失败正是
       如此）。只看 ``status`` 认不出这一档，而它**恰恰是明确失败**、该落 ``failed``。
    2. 否则按 ``status`` 判：**服务端给了答复**（哪怕 4xx/5xx）就是**明确失败**，
       agent 不可能已经跑过它；**没有 status** 就是**传输层没给出答复**，而请求
       **已经**交出去了 ⇒ 不知道 ⇒ 按「可能已经跑过」处理。

    ⛔ 两者都指向「可能已经跑过」时才返回 ``True`` —— **缺省方向必须保守**。
    """
    if getattr(exc, _PROMPT_NEVER_SENT_ATTRIBUTE, None) is False:
        return False
    return getattr(exc, "status", None) is None


def _replay_failure_reason(exc: BaseException) -> str:
    """重放失败时写进 ``last_error`` 的那句话。**必须带 status 线索**。

    盘上那一行只有这句话能区分「明确失败（可以重放）」与「结果未知（绝不重放）」
    ⇒ 少一个 ``status=`` 就是把判定留给下一个读盘的人**猜**，而 §8 说猜出来的方案
    必然在某些情况下错。

    判据：:mod:`tests.test_inbox` 有一条 **AST 覆盖面守门**钉住「凡落 ``failed``
    的路径，``reason`` 都含 status 线索」—— 只写注释不算，那正是那条纪律治的毛病。
    """
    status = getattr(exc, "status", None)
    return (
        f"{type(exc).__name__}(status={status}, "
        f"{_PROMPT_NEVER_SENT_ATTRIBUTE}={_agent_may_have_run(exc)}): {exc}"
    )


def _replay_one(
    prompt: QueuedPrompt,
    inbox: InboundInbox,
    dispatch: Callable[[QueuedPrompt], None],
    outcome: RecoveryOutcome,
) -> None:
    """重放一条：写 ``attempting`` → 投递 → 成功才写 ``delivered``。

    投递抛异常时**分两档**记（:func:`_agent_may_have_run` 判）：

    * 可能**已经跑过**（拿不到 status）⇒ 落 ``outcome_unknown``：**不碰**
      ``attempts``（本档没有「下次」），恢复层对它**只告警、绝不重放**；
    * 明确失败（有 status，或投递侧确知 prompt 压根没提交）⇒ 落 ``failed``，
      预算可能刚好在那里耗尽、于是转 ``abandoned``。

    两档都**不**把这条算进 :attr:`RecoveryOutcome.replayed`。

    ⚠️ ``outcome_unknown`` 那一档的**告警不由这里发**：
    :func:`recover_pending` 的第 3 步从收件箱里读 ``uncertain_prompts()``，而它跑在
    第 1/2 步**之后** ⇒ 刚刚写下的这一行立刻就会被取出来、发出一条**带「请重新
    发送一次」**的告警（:func:`_uncertain_alert_text`）。⛔ 这里若自己再发一遍就是
    每条发两次；⛔ 若只记一条日志不告诉用户怎么办，那是把负担转给用户。
    """
    if not _safe_action(
        lambda: inbox.mark_attempting(prompt.delivery_id), doing="mark the row attempting"
    ):
        # ⚠️⚠️ **记不下 ``attempting`` 就不投**。``attempting`` 是"结果不可知"的**唯一**
        # 来源（理由见 :meth:`InboundInbox.mark_attempting` 的 docstring：写在它之前，
        # 崩溃就落在"还没试"，而那一档会被重放）。⇒ 写失败还照样投的话，这一行在盘上
        # 仍然是 ``pending``/``failed``（**可重放**），而请求可能**已经**交出去
        # ⇒ 下次启动重放它 ⇒ **agent 对同一条指令跑两遍**。
        #
        # ⚠️ 真实触发条件不是"两次独立的坏运气"，而是**一次持续的写盘故障**
        # （盘满 / 库被锁 —— :meth:`InboundGateway._dispatch_prompt` 自己的注释也把
        # 收件箱那两次写入撞上 sqlite3 列为唯一现实触发条件）：实测
        # ``mark_attempting`` 与 ``mark_delivered`` 同时失败时，``dispatch`` 已经被
        # 调用过，而盘上那一行仍是 ``pending``。⇒ 唯一让那种情形不变成双跑的办法
        # 就是**现在就不投**（那一行留在盘上，下次恢复再试；那时若还不行，它仍只是
        # "没投过"，不是"投了两次"）。
        #
        # ⚠️ 实况路径早就是这个形状：``mark_attempting`` 在 ``_dispatch_prompt`` 的
        # try **之外** ⇒ 失败一路冒到 ``_drain`` 的 except，那条队列就此中止
        # （``tests/test_inbound_gateway`` 有一条用例钉着 ``prompt`` 一次都没被调用）。
        # ⇒ 恢复路径此前与它**不一致**，而那一侧正是会重放的地方。
        logger.warning(
            "inbox recovery: NOT replaying %s — its row could not be marked"
            " attempting, so a crash right now would leave a never-tried row that"
            " the next boot replays (the prompt may already have been submitted)",
            prompt.delivery_id,
        )
        return
    try:
        dispatch(prompt)
    except Exception as exc:
        reason = _replay_failure_reason(exc)
        if _agent_may_have_run(exc):
            # ⚠️⚠️ 这一支就是本次补上的那处缺口：请求**已经**交出去，而失败发生在
            # **传输层**（超时 / 连接被拒 / 流中断）⇒ 远端是否收到**没有被记录**。
            # ⇒ ⛔ 绝不能记 ``failed``：那是把"不知道"说成"一定没送到"，而本模块的
            # 第 2 步会按退避阶梯**重放** ``failed`` ⇒ **agent 对同一条指令跑两遍**
            # （AGENTS.md §8 第 3 条：恢复一份没被记录的信息 = 猜）。
            logger.warning(
                "inbox recovery: replaying %s ended with an UNKNOWN outcome (%s);"
                " recording outcome_unknown, NOT failed — the remote side may already"
                " have run it, and failed rows are replayed on the ladder",
                prompt.delivery_id, reason,
            )
            _safe_action(
                lambda: inbox.mark_outcome_unknown(prompt.delivery_id, reason),
                doing="mark the row outcome-unknown",
            )
            return
        logger.warning(
            "inbox recovery: replaying %s failed (%s)", prompt.delivery_id, reason
        )
        _safe_action(
            lambda: inbox.mark_failed(prompt.delivery_id, reason), doing="mark the row failed"
        )
        return
    _safe_action(
        lambda: inbox.mark_delivered(prompt.delivery_id), doing="mark the row delivered"
    )
    outcome.replayed.append(prompt)


def _replay_batch(
    prompts: list[QueuedPrompt],
    inbox: InboundInbox,
    dispatch: Callable[[QueuedPrompt], None],
    outcome: RecoveryOutcome,
) -> None:
    for prompt in prompts:
        _replay_one(prompt, inbox, dispatch, outcome)


def recover_pending(
    inbox: InboundInbox,
    *,
    dispatch: Callable[[QueuedPrompt], None],
    notify: Callable[[str, str], None],
) -> RecoveryOutcome:
    """启动时把收件箱里的行恢复成"用户真的收到了回复"。

    :param inbox: 收件箱（只需要 :mod:`opencode_bridge.inbox` 里那份公开 API）。
    :param dispatch: 把一条 :class:`QueuedPrompt` 真正交给 opencode。
        抛异常就算这次失败 —— 本函数负责记 ``failed``，**不**负责重试。
    :param notify: ``(conversation_id, text)``，把一条**用户可见**的告警发出去。
    :returns: 本次恢复了什么。**永不抛异常** —— 恢复失败不该让桥起不来。
    """
    outcome = RecoveryOutcome()

    # 1) 从没尝试过的 —— 直接重放。agent 不可能已经跑过，重复不可能发生，所以不告警。
    _replay_batch(
        _safe_call(inbox.pending_prompts, reading="pending"),
        inbox,
        dispatch,
        outcome,
    )

    # 2) 明确失败过、且退避期限已到 —— 按阶梯重放。
    #    期限是收件箱里的 not_before，不是内存里的等待：此刻判断，此刻就已到期。
    due_failed = _safe_call(
        lambda: inbox.due_failed_prompts(time.time()), reading="failed"
    )
    _replay_batch(due_failed, inbox, dispatch, outcome)

    # 3) 结果不可知 —— **只告警，绝不重放**。见模块开头的用户拍板说明。
    #    ``uncertain_prompts()`` 一张嘴就含两档：崩在 ``prompt()`` 里的 ``attempting``，
    #    与传输层失败记下来的 ``outcome_unknown``。⛔ 这一档**绝不许**进上面第 1/2 步
    #    的重放集合 —— 那样 agent 会对同一条指令跑两遍。
    outcome.uncertain = _safe_call(inbox.uncertain_prompts, reading="outcome-unknown")
    _alert_per_conversation(outcome.uncertain, notify, _uncertain_alert_text)

    # 4) 终态：预算耗尽。也可能正是上面第 2 步里刚刚耗尽的 —— 那一条本轮就该告警。
    outcome.abandoned = _safe_call(inbox.abandoned_prompts, reading="abandoned")
    _alert_per_conversation(outcome.abandoned, notify, _abandoned_alert_text)

    return outcome
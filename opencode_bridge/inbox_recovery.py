"""崩溃恢复策略（G2）：把收件箱里的行变回"用户真的收到了回复"。

纯策略、可注入
--------------
本模块不 import opencode 客户端，也不碰磁盘（除了通过传进来的
:class:`~opencode_bridge.inbox.InboundInbox`），所以整份逻辑可以拿一个假收件箱
单测 —— :mod:`tests.test_inbox_recovery` 就是这么干的。投递与告警都是**注入的
callable**，本模块对它们一无所知。

分档语义（用户 2026-10-03 拍板）
------------------------------
先说为什么这里有一个**刻意的不对称**：崩溃正好落在 ``client.prompt()`` 执行期间时，
opencode 可能已经处理、也可能没处理 —— 结果**真的不可知**。用户被摆上了这个取舍：

==============================  ==========================================
行状态                          处理
==============================  ==========================================
``pending``（从未尝试）          **直接重放**，不可能重复，不告警
``failed``（明确失败）          按退避阶梯**重放**（用户已见过 ``发送失败: …``）
``attempting``（结果不可知）    **只告警，绝不重放**
``abandoned``（预算耗尽）       终态，告警一次
==============================  ==========================================

⚠️ **``attempting`` 绝不重放，这是用户拍板的，不是可以顺手"改进"的地方。**
当前这个 bug 丢一条消息；改法若会罕见地让 agent 动两次，在 coding 场景下
重复未必是较小的那个恶 —— 重复的文件编辑、重复的 ``git`` 操作、重复的长构建。

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


def _safe_action(action: Callable[[], None], *, doing: str) -> None:
    """做一次收件箱写入；失败只记日志（那一行会留给下次恢复再处理）。"""
    try:
        action()
    except Exception:
        logger.exception("inbox recovery: cannot %s; the row stays for the next boot", doing)


def _uncertain_alert_text(count: int) -> str:
    """结果不可知时的告警。纯用户语言，不含任何内部术语。"""
    return (
        f"有 {count} 条消息在上次退出时状态未知：退出正好发生在提交给 agent 的过程中，"
        f"是否已被处理无法确认。为避免重复执行副作用（重复改文件、重复 git 操作、"
        f"重复长时间构建），已不自动重发，请自行检查该会话是否已经处理过。"
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


def _replay_one(
    prompt: QueuedPrompt,
    inbox: InboundInbox,
    dispatch: Callable[[QueuedPrompt], None],
    outcome: RecoveryOutcome,
) -> None:
    """重放一条：写 ``attempting`` → 投递 → 成功才写 ``delivered``。

    投递抛异常时写 ``failed``（预算可能刚好在那里耗尽、于是转 ``abandoned``），
    **不**把这条算进 :attr:`RecoveryOutcome.replayed`。
    """
    _safe_action(
        lambda: inbox.mark_attempting(prompt.delivery_id), doing="mark the row attempting"
    )
    try:
        dispatch(prompt)
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
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
    outcome.uncertain = _safe_call(inbox.uncertain_prompts, reading="attempting")
    _alert_per_conversation(outcome.uncertain, notify, _uncertain_alert_text)

    # 4) 终态：预算耗尽。也可能正是上面第 2 步里刚刚耗尽的 —— 那一条本轮就该告警。
    outcome.abandoned = _safe_call(inbox.abandoned_prompts, reading="abandoned")
    _alert_per_conversation(outcome.abandoned, notify, _abandoned_alert_text)

    return outcome
"""``/delivery retry`` —— 群内手动补投收件箱里还没送到的消息。

背景与边界
----------
收件箱（:mod:`opencode_bridge.inbox`）把"用户发过、但 agent 可能还没收到"的指令留在
盘上；启动时 :func:`~opencode_bridge.inbox_recovery.recover_pending` 会按退避阶梯
自动重放其中**可以安全重放**的那些。但有两类情况自动重放够不着：

* 还没到退避期限的 ``failed`` 行 —— 只能等；
* 用户不想等、想立刻再试一次。

本模块就是那条**手动**通道：在群里敲 ``/delivery retry`` 列出本会话的待补投项，
再 ``/delivery retry <序号>`` 补投指定的一条。

职责边界（⛔ 别越界）
--------------------
* 本模块**不自己写**收件箱状态 —— 一条都不写：投递走
  :func:`~opencode_bridge.inbox_recovery.replay_one`，它按原有契约写
  ``attempting`` / ``delivered`` / ``failed`` / ``outcome_unknown``。本模块只**读**
  :meth:`~opencode_bridge.inbox.InboundInbox.abandoned_prompts` 判"这次是不是把预算
  用完了"。
* 本模块**不自己分类**投递异常 —— 分档由 :func:`replay_one` 的返回值给出；
  分类判据（有没有 status、投递侧盖没盖"压根没提交"的戳）全在恢复层。
* 本模块**不**调 ``_safe_action``：那个 helper 把返回值吞成 ``bool``，而这里要的
  正是"到底落在哪一档"。收件箱写失败由 :func:`replay_one` 自己处理并反映在返回值里。

命令语义
--------
待补投集合 = ``pending`` 行 + **已到退避期限**的 ``failed`` 行，各自按会话过滤后
**拼接**（pending 在前）。⛔ 不列 ``attempting`` / ``outcome_unknown`` / 未到期的
``failed`` / ``abandoned`` —— 前两档"绝不重放"（请求可能已经交出去），
``abandoned`` 是终态（预算耗尽就不再自动试，手动也不给试），没有可补的东西。

⚠️ 序号在每次调用时**重算** ⇒ 两次调用之间若盘上状态变了，同一个序号可能指向另一条。
两档都可重放 ⇒ 这是**不便**，不是安全风险；因此补投回复里都带上该条正文的预览，
让错位**可见**。

⚠️ 手动补投失败会**消耗退避预算**（``replay_one`` 走的就是 ``mark_failed``），
最终可能把那一行推到 ``abandoned``。这是刻意的：手动与自动共用同一套预算，
⛔ 不另开一条"手动不算数"的路 —— 否则"重试"就成了一条绕过预算的旁路。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Optional

from .inbox import InboundInbox, QueuedPrompt
from .inbox_recovery import (
    REPLAY_DELIVERED,
    REPLAY_FAILED,
    REPLAY_NOT_ATTEMPTED,
    REPLAY_OUTCOME_UNKNOWN,
    RecoveryOutcome,
    replay_one,
)

__all__ = [
    "DeliveryReply",
    "DeliveryRetryCommand",
]

logger = logging.getLogger("opencode_bridge.delivery_retry")

_USAGE_TEXT = (
    "用法：/delivery retry 列出待补投的消息，/delivery retry <序号> 补投其中一条。"
)
_INBOX_DISABLED_TEXT = "收件箱未启用，没有可补投的消息。"
_NO_PENDING_TEXT = "本会话当前没有待补投的消息。"

#: 预览长度上限 —— 列表里每条只给一行，够认出来是哪条就行。
PREVIEW_LIMIT = 60


@dataclass(frozen=True)
class DeliveryReply:
    """``/delivery`` 的一条回复。core 那边只负责把它发出去，不看内容。

    刻意**不复用** :class:`~opencode_bridge.session_model.CommandReply`：那一个带
    ``session_id``（``/model`` 要把它指给某个会话），而这里没有任何会话可指。
    两个命令各自持有自己的回复类型，将来谁加字段都不会牵动另一个。
    """

    text: str
    kind: str = "text"


def _preview(text: str) -> str:
    """把一条正文压成一行预览：连续空白压成单空格，超长截断加省略号。"""
    collapsed = " ".join(text.split())
    if len(collapsed) <= PREVIEW_LIMIT:
        return collapsed
    return collapsed[: PREVIEW_LIMIT - 1] + "…"


class DeliveryRetryCommand:
    """``/delivery retry`` 的全部逻辑。

    ``dispatch`` 由 core 注入，且**复用网关恢复路径那条投递**
    （:meth:`~opencode_bridge.inbound_gateway.InboundGateway.dispatch_recovered`）——
    手动补投与自动重投因此走**同一条**路，本模块不依赖 ``InboundGateway`` 的
    内部状态，可以脱离它单独测试。

    ⚠️ 而知情代价也随之一并继承：走恢复路径 ⇒ 409「agent 正忙」也会落 ``failed``
    并**消耗一级预算**（恢复路径没有内存队列可以退回，理由见 ``dispatch_recovered``
    的 docstring）⇒ ⛔ 这里不为手动补投另开一套映射。

    ``clock`` 与 :class:`~opencode_bridge.session_model.SessionModelCommand` 同一套
    做法：公开可注入，测试里把时钟拨快就能验证"未到期的 ``failed`` 行不会被列出"，
    不必真的等 30 秒。

    ⚠️ 默认必须是 ``time.time``（**墙钟**）：收件箱的 ``not_before`` 就是按
    ``time.time`` 写的，换成 ``time.monotonic`` 会让退避判定永远比不出来。

    ``inbox`` 允许为 ``None``（write-ahead 收件箱没启用）—— 那种情况下本命令
    回"收件箱未启用"而不是报错：**没开收件箱不是故障**。
    """

    def __init__(
        self,
        *,
        inbox: Optional[InboundInbox],
        dispatch: Callable[[QueuedPrompt], None],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._inbox = inbox
        self._dispatch = dispatch
        self.clock = clock

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------
    def reply_for(self, conversation_id: str, args: str) -> DeliveryReply:
        """执行一次 ``/delivery``，返回要回给 IM 的那一条消息。

        ⚠️ ``args`` 已经过 :meth:`~opencode_bridge.commands.CommandHandler.handle_command`
        的 ``@bot`` 后缀剥离与 ``_clean``，这里不再重复处理。
        """
        tokens = str(args or "").split()
        if not tokens:
            return self._list(conversation_id)
        if tokens[0].lower() != "retry":
            return DeliveryReply(_USAGE_TEXT, kind="error")
        if len(tokens) == 1:
            return self._list(conversation_id)
        if len(tokens) > 2:
            return DeliveryReply(_USAGE_TEXT, kind="error")
        ordinal = _parse_ordinal(tokens[1])
        if ordinal is None:
            return DeliveryReply(_USAGE_TEXT, kind="error")
        return self._retry_ordinal(conversation_id, ordinal)

    # ------------------------------------------------------------------
    # 候选集合（列表与单条补投共用）
    # ------------------------------------------------------------------
    def _candidates(self, conversation_id: str) -> list[QueuedPrompt]:
        """本会话当前**可以补投**的那几条：``pending`` 在前、已到期的 ``failed`` 在后。

        两档都可安全重放 —— ``pending`` 从未尝试过，到期的 ``failed`` 是**明确失败**
        （请求有没有得到响应是记录下来的）。⛔ 其余档一律不进集合：
        ``attempting`` / ``outcome_unknown`` 绝不重放，未到期的 ``failed`` 还在等
        退避，``abandoned`` 是终态（预算耗尽就不再试，手动也不给试）。

        两侧各自按 ``created_at ASC, delivery_id ASC`` 有序（收件箱那边定的），
        拼接后整体有序 ⇒ 序号在本会话内**可复现**。⛔ 不在这里排序或去重：
        两档的状态互斥，本来就不会重叠。

        ⚠️ ``clock()`` 只用来问"退避到期了没"，⛔ 不写任何状态 —— 本模块一条
        收件箱状态都不写（见模块 docstring 的职责边界）。
        """
        pending = [
            prompt
            for prompt in self._inbox.pending_prompts()
            if prompt.conversation_id == conversation_id
        ]
        due_failed = [
            prompt
            for prompt in self._inbox.due_failed_prompts(self.clock())
            if prompt.conversation_id == conversation_id
        ]
        return pending + due_failed

    # ------------------------------------------------------------------
    # 列表
    # ------------------------------------------------------------------
    def _list(self, conversation_id: str) -> DeliveryReply:
        if self._inbox is None:
            return DeliveryReply(_INBOX_DISABLED_TEXT)
        candidates = self._candidates(conversation_id)
        if not candidates:
            return DeliveryReply(_NO_PENDING_TEXT)
        lines = [f"本会话有待补投的消息 {len(candidates)} 条："]
        lines.extend(
            f"{position}. {_preview(prompt.text)}"
            for position, prompt in enumerate(candidates, start=1)
        )
        lines.append("用 /delivery retry <序号> 补投其中一条。")
        return DeliveryReply("\n".join(lines))

    # ------------------------------------------------------------------
    # 单条补投
    # ------------------------------------------------------------------
    def _retry_ordinal(self, conversation_id: str, ordinal: int) -> DeliveryReply:
        if self._inbox is None:
            return DeliveryReply(_INBOX_DISABLED_TEXT)
        candidates = self._candidates(conversation_id)
        if ordinal > len(candidates):
            return DeliveryReply(
                f"序号 {ordinal} 超出范围：本会话当前有待补投的消息"
                f" {len(candidates)} 条。",
                kind="error",
            )
        prompt = candidates[ordinal - 1]
        # ⚠️ 那个 RecoveryOutcome 是要丢的：批量恢复靠它记账（replayed / uncertain /
        # abandoned），而手动补投一次只看 replay_one 的返回值，不看批量记账。
        # 传一个空的进去是行为中性的 —— replay_one 只在 delivered 那支往里 append。
        result = replay_one(prompt, self._inbox, self._dispatch, RecoveryOutcome())
        return self._reply_for_result(ordinal, prompt, result)

    def _reply_for_result(
        self, ordinal: int, prompt: QueuedPrompt, result: str
    ) -> DeliveryReply:
        """把 :func:`replay_one` 的四档返回值翻成一句给用户的话。

        ⚠️ 逐档**显式枚举**，末尾留一条 fail-safe 兜底 —— ⛔ 不要写成
        ``if result == REPLAY_FAILED: … else: 成功``：那样新增一档返回值时会落进
        "声称成功"那一支，而**没有任何既有用例会红**（§7.1 判据⑥：判定里不许有
        否定式条件，缺省要留给"不知道"那一侧）。兜底既不声称成功也不声称失败。
        """
        if result == REPLAY_DELIVERED:
            return DeliveryReply(f"已补投 #{ordinal}：{_preview(prompt.text)}")
        if result == REPLAY_FAILED:
            return self._failed_reply(ordinal, prompt)
        if result == REPLAY_OUTCOME_UNKNOWN:
            return DeliveryReply(
                f"#{ordinal} 已提交，但没能确认结果（很可能 agent 已经在跑了）。"
                "它不会再被自动重投，需要的话请自己确认一次。",
                kind="error",
            )
        if result == REPLAY_NOT_ATTEMPTED:
            return DeliveryReply(
                f"#{ordinal} 没能记下投递状态，因此这次没有投递；请稍后再试。",
                kind="error",
            )
        logger.warning(
            "delivery retry: replay_one returned an unrecognised result %r for %s;"
            " reporting neither success nor failure",
            result,
            prompt.delivery_id,
        )
        return DeliveryReply(
            f"#{ordinal} 补投结果不明（既不能确认送出，也不能确认失败）；"
            "请稍后用 /delivery retry 再看一次。",
            kind="error",
        )

    def _failed_reply(self, ordinal: int, prompt: QueuedPrompt) -> DeliveryReply:
        """明确失败那一档：先查它是不是**这一次**把重试预算用完了。

        ⚠️ 判据沿用恢复层第 4 步那条既有机制 —— 读
        :meth:`~opencode_bridge.inbox.InboundInbox.abandoned_prompts` 看这个
        ``delivery_id`` 在不在里面，⛔ 本模块**不重算**预算算术：``attempts`` 与
        退避阶梯全在 :meth:`~opencode_bridge.inbox.InboundInbox.mark_failed` 里，
        在这里重算就是第二份实现，两份迟早对不上。

        重放前该行的状态只可能是 ``pending`` 或**已到期的** ``failed``（见
        :meth:`_candidates`）⇒ 它现在若在 ``abandoned`` 里，只可能是刚刚这次失败
        造成的 ⇒ 不需要"重放前的快照"来对比。
        """
        abandoned_ids = {row.delivery_id for row in self._inbox.abandoned_prompts()}
        if prompt.delivery_id in abandoned_ids:
            return DeliveryReply(
                f"#{ordinal} 补投失败，重试预算已耗尽，不会再自动重投。",
                kind="error",
            )
        return DeliveryReply(
            f"#{ordinal} 补投失败；下一次启动会自动重试。", kind="error"
        )


def _parse_ordinal(token: str) -> Optional[int]:
    """把 ``retry`` 后面的那个 token 读成正整数；读不出就返回 ``None``。

    ⚠️ ``isascii()`` 不是多余的：``str.isdigit()`` 对全角数字（``１０``）也为真，
    而 ``int()`` 会把它们读成 10 ⇒ 用户看到的是"序号打了却报用法错误"。
    这里**只接受 ASCII 数字**，与"序号本来就是我们打印的"对齐。

    ⛔ ``0`` 与负数一律算用法错误：序号从 1 开始，收下 ``0`` 就得再解释一次
    为什么它摸不到任何一条。
    """
    if not token.isascii() or not token.isdigit():
        return None
    value = int(token)
    return value if value >= 1 else None

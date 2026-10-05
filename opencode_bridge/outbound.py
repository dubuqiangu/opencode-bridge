"""出站：桥往 IM 那边发出去的一切 —— 发信、改写进度、收尾、按钮应答。

这一整块原先是 :class:`~opencode_bridge.core.BridgeCore` 的四个方法。搬出来是因为
那个类已经远超 AGENTS.md §5.1 的阈值（方法数 ~15+ / 类自身代码行 ~250+），而出站是
里面**自成一块**的一坨：正文消毒、节流改写、收尾兜底、以及按钮应答。

⚠️ **这些方法早就是一条共享服务了**：
:class:`~opencode_bridge.event_stream.EventStream`、
:class:`~opencode_bridge.inbound_gateway.InboundGateway` 与
:class:`~opencode_bridge.commands.CommandHandler` 拿到的都是**注入进来的 callable**。
搬动的是它们的**实现**，签名一个字都没改 —— 所以那三个类的构造毫无涟漪。

依赖只有两条：``adapter_for``（一条路由协作者）与 ``max_message_chars``（构造时读一次的
配置上限）。出站这一侧不碰会话、不碰 turn、不碰事件流状态，所以搬出去就能脱离 core
单独测。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Optional

from .adapters import Adapter
from .hooks import MsgHandle, Outbound
from .normalize import _clean
from .split import split_text

__all__ = ["OutboundSender"]

logger = logging.getLogger("opencode_bridge.outbound")

NO_OUTPUT_TEXT = "（无输出）"

#: 按 conversation_id 找出该回哪个适配器（找不到返回 ``None``）。
AdapterFor = Callable[[str], Optional[Adapter]]


def one_message_budget(bridge_budget: int, adapter: Optional[Adapter]) -> int:
    """桥往 IM **一条**消息里最多能写多少字符 —— 取两条上限的**小者**。

    两条上限各管一件事：

    * ``bridge_budget``（``bridge.max_message_chars``）是**桥的**预算 —— 用户的配置。
    * ``adapter.effective_max_length`` 是**平台**真正接受的一条消息长度。它是
      **唯一**该读"我按多少切"的地方：静态下限 ``max_message_length`` 会被
      Mattermost / Nextcloud 在启动后用服务端的 ``MaxPostSize`` 细化（槽
      ``message_limit``，非 0 时胜出），所以类属性不是真值。

    ⚠️ **这个函数是唯一一处算这个数的地方**，因为**曾经有两处**算它而它们不一致：
    :meth:`OutboundSender.finalize` 取小者（正确），而
    ``event_stream.py`` 的流式闸门只拿 ``bridge_budget`` 比 —— 于是 6/13 个平台上
    一段"平台上限与 ``bridge_budget`` 之间"的正文能过闸，适配器把它切成多条、
    只交回最后一条的句柄，而 ``Turn.shown_progress_text`` 记下的是**整段**；
    收尾时那个下界 ``max(len(head), len(shown_progress_text))`` 于是把长度**还原**
    回超出平台上限的值。**两个读者必须问同一个问题，否则承诺（占位消息会被最终答复
    顶掉）就有一处兑现不了。**

    ``adapter`` 为 ``None`` 时**没有平台可问**，只按桥的预算走 —— 与
    :meth:`OutboundSender.finalize` 那条"找不到适配器就整段发出去"的路一致。
    """
    if adapter is None:
        return int(bridge_budget)
    return min(int(bridge_budget), int(adapter.effective_max_length))


class OutboundSender:
    """Everything the bridge writes back to the IM side."""

    def __init__(self, *, adapter_for: AdapterFor, max_message_chars: int) -> None:
        """``max_message_chars`` 是**构造时读一次**的配置值：它来自 ``bridge``
        配置段，运行时不会被改写。
        """
        self._adapter_for = adapter_for
        self._max_message_chars = max_message_chars

    # ------------------------------------------------------------------
    # 按钮应答
    # ------------------------------------------------------------------
    def answer(self, adapter: Optional[Adapter], query_id: str, text: str) -> None:
        if adapter is None or not query_id:
            return
        try:
            adapter.answer(query_id, text)
        except Exception:
            logger.exception("adapter.answer failed")

    # ------------------------------------------------------------------
    # 发信 / 改写 / 收尾
    # ------------------------------------------------------------------
    def send_text(
        self,
        conversation_id: str,
        text: str,
        *,
        kind: str = "text",
        adapter: Optional[Adapter] = None,
        session_id: Optional[str] = None,
    ) -> Optional[MsgHandle]:
        """发一条出站文本。``kind == "progress"`` 且平台**改不了**已发消息时**不发**。

        ⚠️ 这一段是「**不要写你以后要抛下的东西**」（AGENTS.md §8）在出站侧的落点，
        也是本类唯一的 ``kind == "progress"`` 闸门 —— 三个调用方
        （:class:`~opencode_bridge.event_stream.EventStream` 的流式首片与重试提示、
        :class:`~opencode_bridge.inbound_gateway.InboundGateway` 的 ``⏳ 处理中…``）
        都从这里过，所以**改一处就够**，不必在每个调用点各判一次。

        为什么必须在这里挡：占位消息是一张**承诺**，承诺收尾时会被最终答复顶掉。
        ``email`` / ``ntfy`` / ``a2a`` / ``homeassistant`` / ``irc`` / ``twitch`` /
        ``qqbot`` 的 :meth:`~opencode_bridge.adapters.base.Adapter.edit` 恒返回
        ``False``，于是那条气泡**永远**清不掉：每一轮都留下一个「还在处理中」的
        僵尸气泡 **加** 一条真正的答复。挡在这里之后 :meth:`finalize` 拿到
        ``handle=None``，直接发最终答复 —— **答复一条不少、不截断、不重复**，
        而僵尸气泡从来不存在。

        返回 ``None`` 与「发送失败」同形是刻意的：调用方本来就把返回值当作
        「有没有可改写的句柄」在用（``Turn.progress_handle``），所以**上游一个字都不用改**。
        """
        adapter = adapter or self._adapter_for(conversation_id)
        if adapter is None:
            logger.warning("no adapter for %s; cannot send", conversation_id)
            return None
        if kind == "progress" and not adapter.supports_message_edit:
            logger.debug(
                "%s 无法改写已发消息：不发占位消息（否则它永远清不掉）",
                adapter.name,
            )
            return None
        out = Outbound(
            conversation_id=conversation_id,
            text=_clean(text),
            kind=kind,
            session_id=session_id,
        )
        try:
            return adapter.send(out)
        except Exception:
            logger.exception("adapter.send failed for %s", conversation_id)
            return None

    def edit_progress(
        self,
        conversation_id: str,
        handle: MsgHandle,
        text: str,
        session_id: str,
    ) -> bool:
        """Throttled/streaming edit. Never falls back to send (no spam)."""
        adapter = self._adapter_for(conversation_id)
        if adapter is None:
            return False
        out = Outbound(
            conversation_id=conversation_id,
            text=_clean(text),
            kind="progress",
            session_id=session_id,
        )
        try:
            ok = adapter.edit(handle, out)
        except ValueError:
            logger.warning(
                "progress edit rejected by adapter (text too long: %d chars)",
                len(out.text),
            )
            return False
        except Exception:
            logger.exception("adapter.edit failed")
            return False
        if not ok:
            logger.debug("progress edit returned False for %s", conversation_id)
        return bool(ok)

    def finalize(
        self, conversation_id: str, handle: Optional[MsgHandle], text: str,
        session_id: str, *, kind: str = "final",
        shown_progress_text: str = "",
    ) -> None:
        """Publish the final message (LANE_C_SPEC §1.5 step 4/5).

        **占位消息是一张承诺：它会承载最终答复。** 所以答复装不下一条消息时，
        这里**不放弃**那条消息，而是把它**补完**：装得下的那一段改写进去，剩下的
        作为后续消息发出去。于是占位消息永远显示一个**完整**片段，读者连着读下来
        就是**完整**答复 —— 而不是「半截正文 + 一条完整正文」。

        ⚠️ **为什么不是「改写失败就整段重发」**：那条消息一旦改不动，内容就被
        **冻结**在最后一次写入成功的那一刻（平台没有「删除 / 撤回」这个原语）。
        冻结的是哪一段只有 ``shown_progress_text`` 知道（见
        :attr:`~opencode_bridge.event_stream.Turn.shown_progress_text`），所以改写
        失败时**只补发读者还没看到的那截**；整段重发会重复，按猜测的偏移补发会丢。
        它不是 ``text`` 的前缀时（失败文案、空答复、占位消息压根没发出去）一律整段
        发 —— 宁可多发也绝不丢。

        ``kind`` 是**收尾语义**，不是文案：成功走 ``"final"``，失败走 ``"error"``。
        ``adapters/a2a.py`` 靠 ``kind == "error"`` 把 A2A 任务判成
        ``TASK_STATE_FAILED``，所以失败那条不能落到默认值上。
        """
        adapter = self._adapter_for(conversation_id)
        if adapter is None:
            logger.warning("no adapter for %s; cannot finalise", conversation_id)
            return
        final = _clean(text) or NO_OUTPUT_TEXT
        if handle is None:
            # 没有占位消息可补完（七个平台压根不发，或占位消息发失败了）：
            # 整段交给适配器，它自己会按平台上限切分。
            self.send_text(
                conversation_id, final, kind=kind, adapter=adapter,
                session_id=session_id,
            )
            return

        # ⚠️ 两条上限取小的那个：``bridge.max_message_chars`` 是**桥的**预算，
        # ``effective_max_length`` 是**平台**真正允许的一条消息长度。只看前者
        # 会在 Discord 上翻车 —— 默认配置 4000 > Discord 的 2000，于是 2001~4000
        # 字的答复会被拿去改写、平台拒收、改写返回 False、整段重发，占位消息就此
        # 留下一截正文。那正是本方法要消灭的那种残留。
        #
        # ⚠️ 走 :func:`one_message_budget` 而不是在这里重写一遍 ``min(...)``：
        # ``event_stream.py`` 的流式闸门问的是同一个问题，两处各算一次就会漂
        # （闸门曾只按桥的预算判，于是记下超额的 ``shown_progress_text``，收尾
        # 时这个下界又把超额的长度放了回来）。**唯一真相在那个函数里。**
        budget = one_message_budget(self._max_message_chars, adapter)
        head, tail = self._split_for_the_placeholder(
            final, budget, shown_progress_text,
        )

        replaced = False
        try:
            replaced = bool(adapter.edit(handle, Outbound(
                conversation_id=conversation_id, text=head, kind=kind,
                session_id=session_id,
            )))
        except ValueError:
            # Lane B raises for texts above the platform limit.
            logger.warning(
                "final edit rejected (%d chars); sending the rest instead",
                len(head),
            )
        except Exception:
            logger.exception("adapter.edit failed; sending the rest instead")

        if replaced:
            # 补完成功：占位消息现在显示 head，剩下的接着发。
            if tail:
                self.send_text(
                    conversation_id, tail, kind=kind, adapter=adapter,
                    session_id=session_id,
                )
            return

        # 改不动：那条消息冻结在 shown_progress_text。只补发它还没显示的那截。
        already_shown = (
            shown_progress_text
            if shown_progress_text and final.startswith(shown_progress_text)
            else ""
        )
        remainder = final[len(already_shown):]
        if not remainder:
            # 冻结的那一截**已经就是整条答复**（流式那一路最后写成功的就是全文，
            # 收尾这次改写恰好失败）。读者手上已经是完整答复 —— 什么都不用补，
            # 再发一遍才是重复。
            logger.warning(
                "%s: could not rewrite the progress message, but it already "
                "shows the complete answer; sending nothing",
                adapter.name,
            )
            return
        logger.warning(
            "%s: could not complete the progress message (%d of %d chars are "
            "showing); sending the remaining %d chars as new messages",
            adapter.name, len(already_shown), len(final), len(remainder),
        )
        self.send_text(
            conversation_id, remainder, kind=kind, adapter=adapter,
            session_id=session_id,
        )

    def _split_for_the_placeholder(
        self, final: str, budget: int, shown_progress_text: str,
    ) -> tuple[str, str]:
        """把答复切成 ``(占位消息那一段, 后续消息那些段)``。

        切点用本仓库自己的 :func:`~opencode_bridge.split.split_text`
        （``prefix_fmt=""``），所以断点优先级与适配器分片时**完全一致** ——
        读者看到的分段与「整段直接发」一模一样。

        ⚠️ ``len(head)`` **不许小于**占位消息已经显示的长度：那会让读者已经读过
        的那一截凭空消失（数据丢失）。

        ⚠️ 这条下界**曾经**会把 ``head`` 顶出预算，而当时的注释写的是
        「``shown_progress_text`` 按构造不超过预算」。那句话**曾经为真**：那时
        流式闸门与本方法的预算都是 4000。后来闸门仍按 4000 判、而本方法改成了
        取两条上限的小者，两者变成两个量，于是 Discord（2000）上记下了 3973 字符
        的 ``shown_progress_text``，这里的下界把 3973 原样放了回去，一次收尾改写
        就带着 3973 字符去了一个 2000 字符的平台。
        **现在它靠构造成立**：流式闸门与本方法都问 :func:`one_message_budget`，
        而闸门只在装得下时才写、``shown_progress_text`` 只记写成功的那一份 ——
        所以它**至多**等于本方法用的那个预算。这条不变式由
        ``tests/test_event_stream.py`` 里那条直接断言它的用例钉着。
        """
        if len(final) <= budget:
            return final, ""
        head = split_text(final, budget, prefix_fmt="")[0]
        head_length = max(len(head), len(shown_progress_text))
        return final[:head_length], final[head_length:]

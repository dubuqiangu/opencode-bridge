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

__all__ = ["OutboundSender"]

logger = logging.getLogger("opencode_bridge.outbound")

NO_OUTPUT_TEXT = "（无输出）"

#: 按 conversation_id 找出该回哪个适配器（找不到返回 ``None``）。
AdapterFor = Callable[[str], Optional[Adapter]]


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
    ) -> None:
        """Publish the final message (LANE_C_SPEC §1.5 step 4/5).

        ``kind`` 是**收尾语义**，不是文案：成功走 ``"final"``，失败走 ``"error"``，
        两条路共用这一段（先把已发的那条进度消息改写成收尾内容，改不动就再发一条）。
        ``adapters/a2a.py`` 靠 ``kind == "error"`` 把任务判成 ``TASK_STATE_FAILED``，
        所以失败那条不能落到默认值上。
        """
        adapter = self._adapter_for(conversation_id)
        if adapter is None:
            logger.warning("no adapter for %s; cannot finalise", conversation_id)
            return
        final = _clean(text) or NO_OUTPUT_TEXT
        if handle is not None and len(final) <= self._max_message_chars:
            out = Outbound(
                conversation_id=conversation_id,
                text=final,
                kind=kind,
                session_id=session_id,
            )
            try:
                if adapter.edit(handle, out):
                    return
            except ValueError:
                # Lane B raises for texts above the platform limit.
                logger.warning(
                    "final edit rejected (%d chars); sending instead",
                    len(final),
                )
            except Exception:
                logger.exception("adapter.edit failed; sending instead")
        # no handle / too long / edit failed -> plain send (adapter chunks)
        self.send_text(
            conversation_id, final, kind=kind, adapter=adapter,
            session_id=session_id,
        )

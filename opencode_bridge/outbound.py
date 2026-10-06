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

⚠️ 构造器**没有第三个依赖**，那是刻意的：``tests/test_outbound.py`` 用
``inspect.signature`` 把这两个参数的名字与形态钉死了。而"这次发送到底成没成"
要落盘就得知道 ``bridge_dir`` —— 那只有运行器知道。于是那条通路走**进程级装配**
（:func:`install_outbound_failure_recorder`），由 :mod:`opencode_bridge.__main__`
在启动时装一次；没装就只是不记，发送行为一个字不变。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Optional

from .adapters import Adapter
from .health import OutboundFailureRecorder, platform_key
from .hooks import MsgHandle, Outbound, SendResult
from .normalize import _clean
from .split import split_text

__all__ = [
    "CANCELLED_TURN_KIND",
    "CANCELLED_TURN_NOTICE_TEXT",
    "CANCELLED_TURN_TEXT",
    "OutboundSender",
    "installed_outbound_failure_recorder",
    "install_outbound_failure_recorder",
]

logger = logging.getLogger("opencode_bridge.outbound")

NO_OUTPUT_TEXT = "（无输出）"

#: 某一轮被丢弃时，占位消息**被改写成**的那一句（平台改得动已发消息时走这条）。
#:
#: ⚠️ 冻结文案：用户 2026-10-07 在两个候选里明确否掉了"把半截正文当最终答复发出去"
#: —— 他既然要开新会话，那一轮的正文就是噪音 ⇒ 这里写"已取消"，不写正文。
CANCELLED_TURN_TEXT = "已取消"

#: 同一条事实的**第二种落点**：没有占位消息可改写时，另发这一句。
#:
#: 为什么必须有它：七个 ``supports_message_edit`` 为 ``False`` 的平台上压根**不发**
#: 占位消息（见 :meth:`OutboundSender.send_text` 的 ``kind == "progress"`` 闸门），
#: 所以"刚才那条被清掉了"这件事**没有任何视觉载体** —— 不另发一句，用户就是零反馈。
#: 能改写却**没发出去**占位消息的那六个平台同此理，所以判据是
#: **"手上有没有一条可改写的消息"**，不是"这个平台在不在名单里"。
CANCELLED_TURN_NOTICE_TEXT = "已取消上一条请求。"

#: 上面两句随消息带出去的 **:attr:`~opencode_bridge.hooks.Outbound.kind`**。
#:
#: 为什么必须独立于 ``"text"`` / ``"error"``：用户 2026-10-07 拍板取消**既不是失败也不是
#: 完成**。而 ``adapters/a2a.py`` 此前是 ``error -> FAILED else COMPLETED``
#: ⇒ 取消会落在"其它"那一支，被报成 ``TASK_STATE_COMPLETED``（"agent 正常答完了"），
#: 而对端刚被告知的那件事恰恰相反。A2A 规范 §4.1.3 为此列了 ``TASK_STATE_CANCELED``，
#: 于是这里给取消一条独立的 kind，由 :data:`~opencode_bridge.adapters.a2a.
#: _TASK_STATE_BY_OUTBOUND_KIND` 显式映射。
#:
#: ⚠️ **刻意不是 ``"progress"``**：``send_text`` 里那道 ``kind == "progress"`` 闸门会
#: 在七个改不动的平台上把 progress **直接丢掉**，而这句「已取消」必须真的发出去。
CANCELLED_TURN_KIND = "cancelled"

#: 按 conversation_id 找出该回哪个适配器（找不到返回 ``None``）。
AdapterFor = Callable[[str], Optional[Adapter]]

#: 进程级的出站失败记录器。``None`` = **没装**（测试、单进程工具、或运行器尚未装配）
#: ⇒ 出站失败只是不再落盘，发送路径的行为**一个字不变**。
_installed_recorder: Optional[OutboundFailureRecorder] = None


def install_outbound_failure_recorder(
    recorder: Optional[OutboundFailureRecorder],
) -> None:
    """装上（或卸下）进程级的 :class:`~opencode_bridge.health.OutboundFailureRecorder`。

    由 :mod:`opencode_bridge.__main__` 在**启动时**调用一次 —— 必须早于
    :meth:`~opencode_bridge.core.BridgeCore.start`，因为适配器一启动就可能发信
    （探针回复、排队中的消息补发）。

    ⚠️ **进程级而不是构造注入**只有一个理由：``OutboundSender.__init__`` 的参数
    列表被 ``tests/test_outbound.py`` 用 ``inspect.signature`` 钉死成两个，而第三个
    依赖（``bridge_dir``）只有运行器知道。⚛ 这条装配是**单向可加的**：没装时
    :meth:`OutboundSender.send_text` 照旧工作，所以它坏了也不会让桥发不出消息。
    """
    global _installed_recorder
    _installed_recorder = recorder


def installed_outbound_failure_recorder() -> Optional[OutboundFailureRecorder]:
    """当前装着的记录器（没装返回 ``None``）。诊断与测试用。"""
    return _installed_recorder


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

        ⚠️ **这里是全仓唯一一处「出了 ``send()`` 之后判成败」的地方** ——
        ``commands.py`` / ``event_stream.py`` / ``inbound_gateway.py`` 的每一次
        ``_send_text`` 都从这里过。⇒ :meth:`_record_outcome` 就是那条缺失的通道的
        落点：适配器算出来的结构化失败原因（``FORBIDDEN`` / ``RATE_LIMITED`` /
        ``TIMEOUT`` …）在这里被读出来、落盘，供 ``--status`` 报给用户 ——
        在此之前它们被算完就直接丢掉，用户那边什么都没有。
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
        # ⚠️ 用 :meth:`~opencode_bridge.adapters.base.Adapter.send_observed` 而不是直接
        # ``send()``：它**先清再读**，所以"这一条"的结果不会与"上一次"混淆
        # （判据的完整理由见 :meth:`_record_outcome`），而且它把抛出的异常
        # **交还**给我们 —— 栈必须留在这里（``adapter.send failed`` 这条 ERROR 是
        # 既有的、被测试钉住的），而只拿到一句 ``"send() raised: …"`` 查不出是哪
        # 一层炸的。
        result, raised = adapter.send_observed(out)
        if raised is not None:
            logger.exception("adapter.send failed for %s", conversation_id)
        self._record_outcome(adapter, result, conversation_id)
        return result.handle

    @staticmethod
    def _record_outcome(
        adapter: Adapter,
        result: SendResult,
        conversation_id: str,
    ) -> None:
        """把**这一次**发送的成败喂给出站失败记录器（没装记录器就是空操作）。

        ## 判据：句柄 **和** ``error_kind``，而不是句柄**或**它

        两者各自都会漏掉一种真实的丢消息：

        * 只看句柄 ⇒ ``telegram`` / ``irc`` / ``twitch`` 那种"分片发到第 3 片
          断了、前 2 片已送达"的**部分成功**会被记成成功 ⇒ 调用方以为答复整条到了，
          而读者少收了一截。:attr:`~opencode_bridge.hooks.SendResult.partial` 正是
          为这件事准备的信号。
        * 只看 ``error_kind`` ⇒ ``ok=True`` 时它只是 ``UNKNOWN`` 的默认值，
          把它读成"有过失败"就是误报。

        ⇒ 判据落在 :attr:`~opencode_bridge.hooks.SendResult` 上（``ok`` /
        ``partial`` / ``error_kind`` 三者一起），而那个结构是**每个适配器都已经
        在记**的东西 —— 不需要各平台再改一行。

        ⛔ **本方法绝不抛**：它服务于一条排障通道，而排障通道坏了不该让一条**本来
        能发出去的消息**发不出去（与 ``record_startup_probes`` 的不变量同源）。
        """
        recorder = None
        try:
            recorder = installed_outbound_failure_recorder()
            if recorder is None:
                return
            if result.ok and not result.partial:
                recorder.note_success(platform_key(adapter))
                return
            detail = result.error_detail or "send() returned None"
            if result.partial:
                detail = f"部分送达：{detail}"
            recorder.note_failure(
                platform_key(adapter),
                result.error_kind,
                detail,
                retry_after=result.retry_after,
            )
            logger.warning(
                "出站失败 %s conversation=%s：%s —— %s",
                platform_key(adapter), conversation_id,
                getattr(result.error_kind, "value", result.error_kind), detail,
            )
        except Exception as exc:  # noqa: BLE001 - 排障记录绝不该决定桥的生死
            logger.warning(
                "outbound-failure 记录器抛了（%s: %s）—— 不影响这次发送",
                type(exc).__name__, exc,
            )

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

    def cancel_turn(
        self, conversation_id: str, handle: Optional[MsgHandle],
        session_id: str,
    ) -> None:
        """Tell the reader the turn they were waiting on is gone — nothing else.

        本方法与 :meth:`finalize` **并列**，不是它的替代品。

        * ``finalize`` 发布**完整答复**（由 ``session.execution.succeeded`` /
          ``.failed`` 等终止事件驱动）；本方法对应那一轮被
          :meth:`~opencode_bridge.session_registry.SessionRegistry.drop_session`
          **直接丢弃** —— 没有终止事件、没有完整正文，只有一条永远停在
          「⏳ 处理中…」上的占位消息。

        ## ⛔ 绝不发半截正文

        ``turn.parts`` 里确实攒了那一轮的半截输出，**但绝不发它**：用户 2026-10-07
        在两个候选里明确否掉了这一条（他发 ``/new`` 就是要丢掉当前上下文，那轮正文
        是噪音），而发半截正文会让读者以为那就是答案。⛔ 也不复用
        :meth:`finalize` —— 它走的就是"发布完整答复"那条路，正是被否掉的行为。

        ## 两条落点，判据是**手上有没有一条可改写的消息**

        * 有（``handle is not None``）⇒ 改写成 :data:`CANCELLED_TURN_TEXT`。
          读者看到的是**同一条消息**，而它已经不再显示"处理中"。
        * 没有 ⇒ 另发一条 :data:`CANCELLED_TURN_NOTICE_TEXT`。

        ⚠️ **判据不是平台名单**：那七个平台压根不发占位消息（``send_text`` 的
        ``kind == "progress"`` 闸门），所以它们手上**永远**没有句柄 ⇒ 两种写法在
        那七个平台上等价。而能改写的六个平台里占位消息**可能发失败**（网络 / 权限），
        那时句柄同样是 ``None`` ⇒ 补一句同样是对的，因为读者手上确实什么都没有。
        ⇒ 一个判据就够，不必两处都判。

        ⚠️ 改写**可能失败**（声明了能力但部署关掉了 / 客户端不支持 / 网络）⇒ 退回补发
        那一句，理由与 :meth:`finalize` 里那段"有界残留"同源：宁可多一句，也不让读者
        盯着一个僵尸气泡猜。本方法**绝不抛** —— 收尾通道坏了不该让 ``/new`` 本身失败。

        ⚠️ **两条落点的 ``kind`` 都是 :data:`CANCELLED_TURN_KIND`，不是 ``"text"``**：
        取消既不是完成也不是失败（用户 2026-10-07 拍板），而 ``adapters/a2a.py`` 靠
        ``kind`` 决定 A2A 终态 ⇒ 落回 ``"text"`` 会让对端收到 ``TASK_STATE_COMPLETED``。
        改写那条同样要带：``adapter.edit`` 收的是同一个 ``Outbound``，平台把改写降级成
        send 时（``adapters/matrix.py`` 就是这么做的）那个 kind 会被**原样转发**。

        :param handle: 那一轮的占位消息句柄；``None`` = 没有可改写的消息。
        :param session_id: 被丢弃的那一轮（随消息带出去，供平台侧记账）。
        """
        adapter = self._adapter_for(conversation_id)
        if adapter is None:
            logger.warning(
                "no adapter for %s; the cancelled turn goes unreported",
                conversation_id,
            )
            return
        if handle is not None:
            out = Outbound(
                conversation_id=conversation_id,
                text=CANCELLED_TURN_TEXT,
                kind=CANCELLED_TURN_KIND,
                session_id=session_id,
            )
            replaced = False
            try:
                replaced = bool(adapter.edit(handle, out))
            except Exception:
                logger.exception("adapter.edit failed while cancelling a turn")
                replaced = False
            if replaced:
                return
            logger.warning(
                "%s: could not rewrite the progress message of the cancelled "
                "turn; sending a separate notice instead",
                adapter.name,
            )
        self.send_text(
            conversation_id, CANCELLED_TURN_NOTICE_TEXT,
            kind=CANCELLED_TURN_KIND,
            adapter=adapter, session_id=session_id,
        )

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
        算「哪截」的那一步在 :meth:`_spans_the_reader_has_not_seen` —— 它**不能**假设
        冻结的那一截是 ``text`` 的前缀（乱序 delta 会让那一截落在中间的偏移上）。

        **⚠️ 有界残留（已量过，无法在没有「删除」原语的前提下修掉）**：占位消息
        一条 ``edit()`` 都没成功过时，它会永远停在 ``⏳ 处理中…`` 上 —— 读者因此
        看到一个卡住的气泡**外加**一条真正的答复。七个不可改写的平台压根没有这条路
        （:meth:`send_text` 的 ``kind == "progress"`` 闸门），所以这条只落在**声明了
        ``supports_message_edit`` 却改不动**的平台上（部署关掉了能力 / 客户端不支持 /
        网络）。清掉它需要平台提供**删除消息**的原语，本仓库没有这个能力声明，
        也不打算凭空发明一个 —— 于是按 AGENTS.md §8「信息确实不可恢复且无解」记成
        残留，而不是假装解决了。**答复本身仍然恰好一次到达**（下面那条
        ``shown_progress_text`` 为空的路），丢的只是那个气泡。

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

        # 改不动：那条消息冻结在 shown_progress_text。只补发它还没显示的那几段。
        unseen_spans = self._spans_the_reader_has_not_seen(final, shown_progress_text)
        if not unseen_spans:
            # 冻结的那一截**已经就是整条答复**（流式那一路最后写成功的就是全文，
            # 收尾这次改写恰好失败）。读者手上已经是完整答复 —— 什么都不用补，
            # 再发一遍才是重复。
            logger.warning(
                "%s: could not rewrite the progress message, but it already "
                "shows the complete answer; sending nothing",
                adapter.name,
            )
            return
        already_shown = len(final) - sum(len(span) for span in unseen_spans)
        logger.warning(
            "%s: could not complete the progress message (%d of %d chars are "
            "showing); sending the remaining %d chars as new messages",
            adapter.name, already_shown, len(final), len(final) - already_shown,
        )
        for span in unseen_spans:
            self.send_text(
                conversation_id, span, kind=kind, adapter=adapter,
                session_id=session_id,
            )

    @staticmethod
    def _spans_the_reader_has_not_seen(
        final: str, shown_progress_text: str,
    ) -> list[str]:
        """读者**还没读到**的那几段 ``final`` —— 按原文顺序，不含空段。

        收尾改写失败时那条消息被**冻结**在 ``shown_progress_text``，所以要补发的是
        「``final`` 减去它已经占住的那一段」，而**不是**一条后缀。

        ## 为什么不能假设冻结的那一截是前缀

        :meth:`~opencode_bridge.event_stream.Turn.assemble` 是**按 ordinal 排序**拼
        起来的，而 delta **可能乱序到达**：ordinal 1 先到时正文已经显示成第 2 段，
        随后 ordinal 0 才补上第一段。于是最后一次成功写入的内容可能是
        ``final[300:400]`` 那样的一段 —— **落在中间的偏移上，不是前缀**。此前这里
        只认前缀，于是那一路**整段重发**：读者先把第 4 段读了一遍，再从第 1 段重读
        全文 —— 同一段话出现两次（实测 400 字的答复被读成 500 字）。

        ## 三种可能，只在能判定时才判

        * **前缀**（正常情形：delta 按序到达，或各 ``assistantMessageID`` 依次追加）
          ⇒ 补发它后面那一段。读者连着读下来**逐字节等于原文**。
        * **在 ``final`` 里恰好出现一次** ⇒ 读者读过的是
          ``final[offset:offset+len(shown)]``，没读过的是它**前后两段**。两段分别发，
          于是**每个字符恰好到达一次**，既没重复也没丢。
        * **出现多次或压根不出现** ⇒ 偏移**不可判定**，于是**整段发**。宁可多发也
          绝不丢，也绝不猜一个偏移（AGENTS.md §8：猜错必然在某些输入上错）。

        ``shown_progress_text`` 为空（占位消息压根没发出去、或一次写入都没成功过）
        落到第一种：读者什么也没读过，整段都是欠账。

        ## ⚠️ 有界残留：第二种情形下**顺序**恢复不了

        冻结的那一段已经被读者**按它自己的样子**读过了（它是流式进度的一部分），
        而它落在 ``final`` 的**中间或末尾**偏移上 —— 所以「冻结的那段 + 后面补发的
        两段」拼起来是原文的一个**轮转**。**每字恰好一次**成立，
        **按序等于原文**不成立。

        **恢复不了，也没有别的选法**：那条消息改不动（否则这里根本走不到）、平台
        没有「删除 / 撤回」这个原语（见 :meth:`finalize` 里那段有界残留），于是已经
        印出去的那一段既挪不动也抹不掉。

        ⚠️ 另一种选法是**整段重发**（也就是修好之前的行为）：最后那条消息会是完整
        的原文，代价是那一段被读两遍。两者都兑现不了同一份契约，本方法选
        **「不重复」** —— 重复正是本方法存在的理由，而顺序在这里**不可判定**。

        ⚠️ 曾经想在上游堵：让流式闸门只发布**追加式**的正文（不是当前显示内容的
        延伸就拒绝写）。**已否决**，它会让情况更糟 —— 正文在闸门那里就**追不上**了：
        乱序投递 ``(1,) (0,) (2,)`` 而节流放行了前两帧时，闸门会一直停在 ``"b"`` 上，
        而放行第二帧时它本来能追上 ``"abc"``（那是个正经前缀，收尾只需补 ``"d"``）。
        换句话说：闸门的职责是"把正文发出去"，不是"自己判断顺序对不对"。
        """
        if not shown_progress_text:
            return [final]
        if final.startswith(shown_progress_text):
            unseen = final[len(shown_progress_text):]
            return [unseen] if unseen else []
        if final.count(shown_progress_text) == 1:
            offset = final.index(shown_progress_text)
            return [
                span for span in (
                    final[:offset], final[offset + len(shown_progress_text):],
                ) if span
            ]
        return [final]

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

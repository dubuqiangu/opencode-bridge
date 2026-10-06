"""入站这一侧：适配器推来的消息、按钮回调、每会话队列、启动时的收件箱重放。

这一块原先是 :class:`~opencode_bridge.core.BridgeCore` 的七个方法。搬出来是因为
那个类已经远超 AGENTS.md §5.1 的阈值（方法数 ~15+ / 类自身代码行 ~250+），而入站
是里面**自成一块**的一坨：适配器进来的两个 hook、同一会话内消息的排队与串行投递、
写前收件箱（G2）那几次记账、以及崩溃后启动时的重放。

搬的时候**连私有状态一起搬**（§5.1 首选的那种形态）：``_queues`` / ``_draining`` /
``inbox`` 都是只有这一块才读写的东西。剩下的依赖由 :meth:`InboundGateway.__init__`
显式注入，其中两项是**共用的状态**而不是协作者：``lock``（core 与事件流还在用同一把
锁）与 ``turns``（事件流在那边读改同一个 dict）。注入的是**同一个对象**，所以互斥
关系与 dict 身份一点没变。

这一块**不碰**事件流、不碰生命周期：``BridgeCore.start()`` 只在 SSE 线程起来之后调
一次 :meth:`InboundGateway.recover_inbox`，其余全靠上面两个 hook 进来。
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections.abc import Callable, Mapping
from typing import Any

from .adapters import Adapter
# C1：发给 agent 的 prompt 前面拼一段"这条回复发出去是什么样"的说明。
# 纯函数、无状态，所以直接 import —— 本文件注入的是**有状态的协作者**
# （路由 / 收件箱 / 出站），而这一段只需要适配器的能力声明。
from .channel_profile import with_channel_hint
# ``setup:`` 按钮回调要回的那份**冻结文案**就在命令那边，所以这里 import 它，
# 而不是复制第二份 —— 改一处只碰一个地方（AGENTS.md §5.1）。
from .commands import _SETUP_ALIASES, _setup_guide
# 建 turn 用 :class:`Turn`，类型归事件流所有；这里只 import，不复制。
from .event_stream import Turn
from .hooks import Inbound, MsgHandle
from .inbound_merge import (
    BUFFERED_NOTICE,
    HELD,
    HELD_EXPIRED_NOTICE,
    IGNORED,
    ConversationMerger,
)
from .inbox import InboundInbox, QueuedPrompt
from .inbox_recovery import recover_pending
from .normalize import _clean, trim_outer_whitespace
from .opencode_client import OpenCodeClient, OpenCodeError
from .permission_ledger import (
    REPEATED_ANSWER_ACK,
    PermissionLedger,
    repeated_answer_notice,
)

__all__ = ["InboundGateway"]

logger = logging.getLogger("opencode_bridge.inbound_gateway")

PROGRESS_TEXT = "⏳ 处理中…"

#: Values accepted in ``perm:<sessionID>:<reqID>:<decision>`` callbacks.
_PERM_DECISIONS = ("once", "always", "reject")

#: 长输入回执的字数门槛（配置缺失或非法时用这个）。180 抄自 dsh 的
#: ``longInputAckChars``；含义与理由见 :meth:`InboundGateway._acknowledge_long_input`。
DEFAULT_LONG_INPUT_ACK_CHARS = 180

#: 续行缓冲的保险丝秒数（配置缺失或非法时用这个）。**不是**合并窗口 ——
#: 没有 ``..`` 的消息不会起任何计时器，所以普通消息的额外延迟可证明是 0。
DEFAULT_MERGE_CONTINUE_TIMEOUT_SECONDS = 15.0


def _positive_float(value: Any, default: float) -> float:
    """A finite, non-negative float, else ``default`` (NaN and negatives rejected)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in (float("inf"), float("-inf")):
        return default
    return number if number > 0 else default


def _positive_int(value: Any, default: int) -> int:
    """A non-negative int, else ``default``. ``0`` is legal (= 该功能关掉)."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number >= 0 else default

#: 启动时等事件流确认连上的上限（秒），见 :meth:`InboundGateway.recover_inbox`。
#: 取 2 秒是因为 opencode 通常就在本机；而真的不可达时这 2 秒只换来一行告警 ——
#: 那种情况下恢复扫描本身也多半会失败，不该在这里死等。
_EVENT_STREAM_CONFIRMED_TIMEOUT_SECONDS = 2.0


# ----------------------------------------------------------------------
# 注入的协作者：签名写在这里，core 那边的实现是什么它们不关心
# ----------------------------------------------------------------------
#: 按 conversation_id 找出该回哪个适配器（找不到返回 ``None``）。
AdapterFor = Callable[[str], Adapter | None]

#: 回一条按钮回调的应答（ack）。
AnswerCallback = Callable[[Adapter | None, str, str], None]

#: 取（或建）这条会话的 opencode session；平台以关键字 ``platform`` 传入。
EnsureSession = Callable[..., str]

#: 执行一条斜杠命令。
HandleCommand = Callable[[str, Adapter, str], None]

#: 记下"这个会话属于哪个适配器"。
RememberPlatform = Callable[[str, str], None]

#: 发一条出站文本；关键字参数与 core 的发信入口一致，返回句柄或 ``None``。
SendText = Callable[..., MsgHandle | None]


# ----------------------------------------------------------------------
# 写前收件箱：从一条入站消息到收件箱里的一行
# ----------------------------------------------------------------------
# 这一段是**模块级函数**而不是 :class:`InboundGateway` 的方法：纯逻辑的构造与记账，
# 与"入站"这个类的其余职责（排队、投递、重放）无关，也不该给它再加方法
# （AGENTS.md §5.1）。
def _queued_prompt_for(inbound: Inbound, text: str) -> QueuedPrompt:
    """Build the :class:`QueuedPrompt` row for one inbound message.

    The body is carried through **verbatim** — never prefixed, never rewritten.
    That text lands in the agent's context, so anything appended here (a
    "replayed after crash" note, say) would be read by the agent as part of
    user's request. Alerts about a replay belong in the notification path, not
    in the prompt.

    Delivery id: the platform's own ``message_id`` when it has one, else
    ``sha256(platform|conversation_id|text)``. The fallback is a real
    trade-off — two byte-identical messages from a platform that reports no
    ``message_id`` collapse into one delivery — so it is logged, never silent
    (see :func:`_record_inbound`).
    """
    platform = str(inbound.platform or "")
    conversation_id = str(inbound.conversation_id or "")
    message_id = str(inbound.message_id) if inbound.message_id else None
    if message_id:
        delivery_id = "%s:%s:%s" % (platform, conversation_id, message_id)
    else:
        delivery_id = hashlib.sha256(
            ("%s|%s|%s" % (platform, conversation_id, text)).encode("utf-8")
        ).hexdigest()
    return QueuedPrompt(
        delivery_id=delivery_id,
        conversation_id=conversation_id,
        platform=platform,
        message_id=message_id,
        text=text,
    )


def _record_inbound(
    inbox: InboundInbox | None, inbound: Inbound, text: str,
) -> QueuedPrompt | None:
    """Write-ahead one inbound prompt. Returns the row to deliver, or ``None``
    to deliver nothing.

    ``None`` means **dedup hit**: the same ``delivery_id`` is already in the
    inbox, so the platform re-delivered something we have a receipt for. Running
    the agent again on it is exactly what at-most-once is for.

    ``inbox is None`` means the inbox is switched off; delivery proceeds
    unchanged, just without a receipt.
    """
    queued = _queued_prompt_for(inbound, text)
    if inbox is None:
        return queued
    if inbox.record(queued):
        return queued
    if queued.message_id is None:
        # The hash-fallback collapse is the one dedup the user cannot predict
        # from the platform side, so it gets spelled out rather than left as a
        # mysterious "message ignored".
        logger.info(
            "inbox %s: duplicate ignored; this platform reports no message_id, "
            "so the dedup key fell back to a content hash — two byte-identical "
            "messages collapse into one delivery",
            queued.delivery_id,
        )
    else:
        logger.info(
            "inbox %s: duplicate ignored (platform re-delivered a known message)",
            queued.delivery_id,
        )
    return None


# ----------------------------------------------------------------------
# inbound gateway
# ----------------------------------------------------------------------
class InboundGateway:
    """Everything the adapters push in, and the machinery behind it."""

    def __init__(
        self,
        *,
        client: OpenCodeClient,
        lock: threading.RLock,
        turns: dict[str, Turn],
        inbox: InboundInbox | None,
        stream_confirmed: threading.Event,
        adapter_for: AdapterFor,
        answer_callback: AnswerCallback,
        ensure_session: EnsureSession,
        handle_command: HandleCommand,
        remember_platform: RememberPlatform,
        send_text: SendText,
        permission_ledger: PermissionLedger,
        bridge_config: Mapping[str, Any],
    ) -> None:
        """全部依赖由 core 注入，本类不自己去找。

        ``lock`` 与 ``turns`` 是**共用的状态**而不是协作者：事件流读改同一个
        ``turns``，core 那边七个方法还在用同一把锁（见模块 docstring）。注入的是
        同一个对象，所以互斥关系与 dict 身份都没变。

        ``inbox`` 是 ``None`` 时写前收件箱整个关掉 —— 投递照走，只是不留收据。

        ``stream_confirmed`` 是事件流那个"订阅已建立"的信号；恢复要等它（有上限），
        但**不归本类拥有**：设它的是事件流。

        ``permission_ledger`` 与命令侧、事件流侧**共用同一个对象**，所以
        ``perm:`` 按钮那一条路和 ``/approve`` 会对"这个请求答过了没有"得出
        同一个答案 —— 否则同一个请求从两条路各能答一次，去重就成了半拉子
        （见 :mod:`opencode_bridge.permission_ledger`）。

        ``bridge_config`` 是 ``config.bridge`` 那一段（构造时读一次，运行时不会
        被改写）。C3 只从里面取两个数：长输入回执的字数门槛、续行保险丝的秒数。
        刻意**不**注入整个 :class:`~opencode_bridge.config.Config` —— 本类用不到
        别的配置，而那两个数读不到就各自退回模块默认值（见
        :mod:`opencode_bridge.inbound_merge`）。
        """
        self._client = client
        self._lock = lock
        self._turns = turns
        self._inbox = inbox
        self._stream_confirmed = stream_confirmed
        self._adapter_for = adapter_for
        self._answer = answer_callback
        self._ensure_session = ensure_session
        self._handle_command = handle_command
        self._remember_platform = remember_platform
        self._send_text = send_text
        self._permission_ledger = permission_ledger
        self._long_input_ack_chars = _positive_int(
            bridge_config.get("long_input_ack_chars"), DEFAULT_LONG_INPUT_ACK_CHARS
        )
        #: C3 的续行缓冲。**本类自己建**（只有入站这一侧读它），所以不注入。
        #: 保险丝到点的回调是本类的方法 —— 缓冲因此不需要知道任何出站的东西。
        self._merger = ConversationMerger(
            hold_timeout_seconds=_positive_float(
                bridge_config.get("merge_continue_timeout_seconds"),
                DEFAULT_MERGE_CONTINUE_TIMEOUT_SECONDS,
            ),
            on_hold_expired=self._deliver_expired_hold,
        )

        #: conversation_id -> 该会话排队等发的消息。**只有本类读写**。
        self._queues: dict[str, list[QueuedPrompt]] = {}
        #: 正在排空某个会话的 conversation_id —— 同一会话同时只允许一个排空者。
        self._draining: set[str] = set()

    # ------------------------------------------------------------------
    # Hooks: inbound
    # ------------------------------------------------------------------
    def on_inbound(self, inbound: Inbound) -> None:
        try:
            conversation_id = str(inbound.conversation_id or "")
            # ⚠️ 必须**先**记映射、再处理 is_callback 早退 —— 按钮回调那条路
            # 本身会return 掉，若在这里记就漏了它，后续 :meth:`on_callback` 只能靠
            # 前缀去猜是哪个适配器。
            self._remember_platform(conversation_id, inbound.platform)
            if inbound.is_callback:
                # Lane B fires on_inbound(kind="callback") *before*
                # on_callback(); handling it here as well would double-send.
                return
            text = _clean(inbound.text)
            if not conversation_id:
                return
            # ⚠️ 这里**不能**写 ``.strip()``：无参 strip 会去掉前导空白，于是粘贴
            # 的第一行被 dedent、后面几行没有 —— agent 拿到的是 IndentationError。
            # 全空白消息交给下面那个 ``not text`` 守卫去丢（那才是 strip 当初的
            # 作用），排版交给 :func:`~opencode_bridge.normalize.trim_outer_whitespace`。
            text = trim_outer_whitespace(text)
            if not text:
                return
            adapter = self._adapter_for(conversation_id)
            if adapter is None:
                logger.warning(
                    "no adapter attached; dropping message for %s",
                    conversation_id,
                )
                return
            if text.startswith("/") or text.lstrip().startswith("/"):
                # ⚠️ 这里**判两次**：正文不再 lstrip（缩进要留着），但命令必须
                # 仍然容得下一个前导空格 —— 那是 C3 之前 ``.strip()`` 顺带给出的
                # 行为，而"只改缩进、别的都不动"要求把它原样留下。
                # ``text.lstrip()`` 与旧代码的 ``.strip()`` 在命令这一支上等价：
                # 命令一定是一行，而 :meth:`~opencode_bridge.commands.CommandHandler.
                # handle_command` 本来就按空白切词，两侧空白它都吃。
                #
                # ⚠️ 命令**绝不**写前落盘。命令就地执行、从不经过 prompt()，
                # 所以它永远不会走到 mark_delivered —— 那一行会永远留在
                # 收件箱里，于是每次启动都被重放一遍：`/new` 每次重启都重建会话、
                # `/setup` 每次都重发引导。那比要修的丢消息 bug 更糟。
                # 落盘必须留在 else 分支里（tests/test_inbox_wiring.py 锁住这条）。
                #
                # ⚠️ 命令也**绝不**进合并窗口（与 dsh 的 gateway.ts:395-428 同一条
                # 纪律）：`/approve` 这类命令必须立刻执行，缓存它等于把 C4 刚堵上的
                # 权限路径重新打开一条延迟通道。用户给命令敲 `..` 本来就没有意义。
                self._handle_command(conversation_id, adapter, text.lstrip())
                return
            self._deliver_plain_text(conversation_id, adapter, inbound, text)
        except Exception:
            logger.exception("on_inbound failed")

    def _deliver_plain_text(
        self,
        conversation_id: str,
        adapter: Adapter,
        inbound: Inbound,
        text: str,
    ) -> None:
        """One non-command line: merge-gate it, then persist and deliver.

        拆成独立方法是因为 :meth:`on_inbound` 里已经有一条命令分支，再往那个
        ``else`` 里塞合并、回执、去重三件事会让它读不下去（AGENTS.md §5.1 的
        "只能往大方法里塞 if 分支"就是该拆的信号）。

        **顺序是有意的**：合并 → 回执 → 写前落盘 → 投递。合并必须在最前（那是
        "这几行是一件事"的判断），而落盘必须在投递前（那是"别丢这条"）。两件事
        刻意由两个模块各管各的：缓冲**不**写进收件箱（见
        :mod:`opencode_bridge.inbound_merge` 的模块 docstring）。
        """
        merged = self._merger.ingest(conversation_id, text)
        if merged.kind == IGNORED:
            return
        if merged.kind == HELD:
            # 回执是**必须**的：一行以 `..` 结尾的散文会被判成续行标记，没有这句
            # 它就是静默消失（AGENTS.md §8 点名最糟的那种代价）。
            self._send_text(
                conversation_id, BUFFERED_NOTICE, kind="text", adapter=adapter
            )
            return
        self._persist_and_enqueue(conversation_id, adapter, inbound, merged.text)

    def _persist_and_enqueue(
        self,
        conversation_id: str,
        adapter: Adapter,
        inbound: Inbound,
        text: str,
    ) -> None:
        """Acknowledge if long, write the inbox receipt, then queue for delivery.

        合并**已经**做完，这里只处理"怎么把它变成一次投递"。拆成独立方法是
        因为保险丝那条路要走同样的三步，而它拿到的是**并集**而不是一行 ——
        若让它去调 :meth:`_deliver_plain_text`，那份并集会被再过一次合并，
        末尾的 ``..`` 会让它重新进缓冲，形成自己喂自己的循环。
        """
        self._acknowledge_long_input(conversation_id, adapter, text)
        queued = _record_inbound(self._inbox, inbound, text)
        if queued is not None:  # None = 去重命中，已投递过
            self._enqueue(queued)

    def _acknowledge_long_input(
        self, conversation_id: str, adapter: Adapter, text: str,
    ) -> None:
        """Tell the reader a long input arrived — **only where nothing else will**.

        判据是 :attr:`~opencode_bridge.adapters.base.Adapter.supports_message_edit`
        而不是"平台名单"：不能改写已发消息时出站那道闸门**根本不发**
        ``⏳ 处理中…``（见 :mod:`opencode_bridge.channel_profile`），于是用户粘一大段
        之后**什么迹象都没有**。能改写的那些本来就有那个占位消息，再加一句只是让
        一次提问变成三条消息。
        """
        threshold = self._long_input_ack_chars
        if threshold <= 0 or len(text) < threshold:
            return
        if getattr(adapter, "supports_message_edit", False):
            return
        self._send_text(
            conversation_id,
            "已收到 %d 字，处理中…" % len(text),
            kind="text",
            adapter=adapter,
        )

    def _deliver_expired_hold(self, conversation_id: str, held_text: str) -> None:
        """Fuse fired: deliver what was held, and say that we did.

        这一条保证缓冲的内容**任何路径都不会无声消失**：用户敲了 ``..`` 然后
        再没下文，保险丝到点替他发出去，并告诉他发出去的是什么。

        ⚠️ 合成的那条 :class:`Inbound` **没有** ``message_id`` —— 它不是平台
        交付的一条消息。给了 ``None`` 就会走 :func:`_queued_prompt_for` 的
        ``sha256(platform|conversation_id|text)`` 兜底，于是同一次超时重放两次会
        被收件箱去重挡掉（那是**要**的：同一条并集不该被 agent 跑两遍）。
        ``platform`` 取适配器自己的 ``name``，与各适配器构造 ``Inbound`` 时写的
        ``platform=self.name`` 是同一个值（见 :meth:`AdapterRouter.asking_platform`
        的说明：提问平台就是适配器自己报上来的名字）。
        """
        adapter = self._adapter_for(conversation_id)
        inbound = Inbound(
            conversation_id=conversation_id,
            text=held_text,
            kind="text",
            platform=str(getattr(adapter, "name", "") or ""),
        )
        if adapter is None:
            logger.warning(
                "inbound merge: no adapter for %s; the held text is delivered "
                "anyway but nothing will be sent to the reader",
                conversation_id,
            )
        self._send_text(
            conversation_id,
            HELD_EXPIRED_NOTICE % held_text,
            kind="text",
            adapter=adapter,
        )
        self._persist_and_enqueue(conversation_id, adapter, inbound, held_text)

    def on_callback(
        self, conversation_id: str, data: str, query_id: str
    ) -> None:
        """Handle ``setup:<platform>`` and ``perm:<sessionID>:<reqID>:<decision>``."""
        adapter = None
        try:
            adapter = self._adapter_for(conversation_id)
            data = str(data or "")
            # /setup inline-button press: reply once here (on_inbound already
            # dropped the kind="callback" copy, so no double-send) and ack.
            if data.startswith("setup:"):
                platform = _SETUP_ALIASES.get(data.split(":", 1)[1].strip().lower())
                if platform is None:
                    self._answer(adapter, query_id, "未知平台")
                    return
                self._send_text(
                    conversation_id, _setup_guide(platform), kind="text",
                    adapter=adapter,
                )
                self._answer(adapter, query_id, "已打开接入引导")
                return
            parts = data.split(":", 3)
            decision_ok = len(parts) == 4 and parts[0] == "perm"
            if decision_ok:
                _, session_id, request_id, decision = parts
                decision_ok = decision in _PERM_DECISIONS
            if not decision_ok:
                logger.warning("unsupported callback payload: %r", data)
                self._answer(adapter, query_id, "失败")
                return
            # ⚠️ 迟到的第二次回答在这里被否掉（C4）。按钮可以被连点、可以被
            # Telegram 重投，于是同一个 ``perm:`` 载荷会走两遍；不否掉就会发出
            # 两次 reply_permission，第二次还能把 once 放宽成 always。
            # 与命令侧共用同一个账本，所以两条路合起来只答一次。
            status = self._permission_ledger.status_of(session_id, request_id)
            if status.already_closed:
                logger.info(
                    "ignoring a repeated permission button press for %s of "
                    "session %s (already %s); nothing is sent to the server",
                    request_id, session_id, status.state,
                )
                self._answer(adapter, query_id, REPEATED_ANSWER_ACK)
                self._send_text(
                    conversation_id,
                    repeated_answer_notice(status),
                    kind="error",
                    adapter=adapter,
                )
                return
            try:
                self._client.reply_permission(session_id, request_id, decision)
            except Exception as exc:
                logger.warning(
                    "reply_permission(%s, %s, %s) failed: %s",
                    session_id,
                    request_id,
                    decision,
                    exc,
                )
                self._answer(adapter, query_id, "失败")
                self._send_text(
                    conversation_id,
                    f"权限回复失败: {exc}",
                    kind="error",
                    adapter=adapter,
                )
                return
            # 只在**成功**之后记账（理由见 commands._apply_permission_decision）。
            self._permission_ledger.record_answered(
                session_id, request_id, decision
            )
            self._answer(adapter, query_id, "已处理")
        except Exception:
            logger.exception("on_callback failed")
            self._answer(adapter, query_id, "失败")

    # ------------------------------------------------------------------
    # prompt queue (one conversation at a time, many in parallel)
    # ------------------------------------------------------------------
    def _enqueue(self, queued: QueuedPrompt) -> None:
        conversation_id = queued.conversation_id
        with self._lock:
            self._queues.setdefault(conversation_id, []).append(queued)
            if conversation_id in self._draining:
                return  # current drainer will pick this up
            self._draining.add(conversation_id)
        self._drain(conversation_id)

    def flush_queue(self, conversation_id: str) -> None:
        """Called after a turn finalises (or on ``session.idle``)."""
        with self._lock:
            if conversation_id in self._draining:
                return
            if not self._queues.get(conversation_id):
                return
            self._draining.add(conversation_id)
        self._drain(conversation_id)

    def _drain(self, conversation_id: str) -> None:
        try:
            while True:
                with self._lock:
                    queue = self._queues.get(conversation_id) or []
                    if not queue:
                        # empty check + draining flag flip are atomic, so a
                        # concurrent enqueue can never be lost
                        self._draining.discard(conversation_id)
                        return
                    queued = queue.pop(0)
                outcome = self._dispatch_prompt(queued)
                if outcome == "busy":
                    # 409 是"还没轮到"，不是失败：不写 failed，重试预算分文未花，
                    # 收件箱那一行已退回 pending（服务端拒收 = 从未尝试过）。
                    # 这一条退回内存队列，等 :meth:`flush_queue`。
                    with self._lock:
                        queue = self._queues.setdefault(conversation_id, [])
                        queue.insert(0, queued)
                        self._draining.discard(conversation_id)
                    return
        except Exception:
            logger.exception("queue drain failed for %s", conversation_id)
            with self._lock:
                self._draining.discard(conversation_id)

    def _dispatch_prompt(
        self, queued: QueuedPrompt, *, recording_delivery: bool = True,
    ) -> str:
        """Send one queued prompt. Returns ``ok`` / ``busy`` / ``error``.

        The four inbox writes live here and nowhere else, because only this
        method can tell *which* of the three outcomes happened — and the
        ``attempting`` write has to sit immediately against ``client.prompt()``:
        written earlier, a crash during ``create_session`` would leave a row
        that looks "outcome unknown" when in fact the agent never ran.

        ``recording_delivery=False`` skips the writes for the startup recovery
        path, where :func:`~.inbox_recovery.recover_pending` is the sole bookkeeper.
        """
        conversation_id = queued.conversation_id
        adapter = self._adapter_for(conversation_id)
        inbox = self._inbox if recording_delivery else None
        try:
            # ⚠️ ``queued.platform`` 就是当初产生这条消息的适配器报上来的，准确；
            #: 它是归属划分需要的那个"提问平台"（启动重放路径上 core 的
            #: ``_conv_adapter`` 可能还是空的，这一行就不依赖它）。
            session_id = self._ensure_session(conversation_id, platform=queued.platform)
        except Exception as exc:
            logger.exception("create_session failed for %s", conversation_id)
            self._send_text(
                conversation_id,
                f"创建会话失败: {exc}",
                kind="error",
                adapter=adapter,
            )
            if inbox is not None:
                inbox.mark_failed(queued.delivery_id, f"create_session failed: {exc}")
            return "error"

        if inbox is not None:
            inbox.mark_attempting(queued.delivery_id)
        try:
            # ⚠️ 渠道说明（C1）只在**发给 opencode 的这一刻**拼上去，而且是
            # 由 ``adapter`` 自己的能力算出来的（见 :mod:`opencode_bridge.channel_profile`
            # ——刻意没有"平台 → 上限"的对照表）。收件箱里存的、哈希算的、
            # 重放重发的都还是用户原文，那条不变量一个字没动。
            self._client.prompt(
                session_id, with_channel_hint(queued.text, adapter)
            )
        except OpenCodeError as exc:
            if exc.status == 409:
                logger.info(
                    "session %s busy; message queued for %s",
                    session_id,
                    conversation_id,
                )
                # ⚠️ 必须在这里退回 pending：409 是"服务端拒收"，agent **没跑过**，
                # 所以重放不可能重复副作用。不退回的话那一行会永远停在
                # attempting，被恢复层按"结果不可知"只告警而不重放 ——
                # 崩溃若落在 409 与进程内重投之间，这条消息就**永远送不到**。
                if inbox is not None:
                    inbox.mark_pending(queued.delivery_id)
                return "busy"
            logger.warning("prompt failed: %s", exc)
            self._send_text(
                conversation_id,
                f"发送失败: {exc}（可尝试 /new 重建会话）",
                kind="error",
                adapter=adapter,
            )
            if inbox is not None:
                inbox.mark_failed(queued.delivery_id, f"prompt failed: {exc}")
            return "error"
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("prompt failed")
            self._send_text(
                conversation_id,
                f"发送失败: {exc}",
                kind="error",
                adapter=adapter,
            )
            if inbox is not None:
                inbox.mark_failed(queued.delivery_id, f"prompt failed: {exc}")
            return "error"

        if inbox is not None:
            inbox.mark_delivered(queued.delivery_id)
        # success: create / reuse this turn's progress message
        with self._lock:
            turn = self._turns.get(session_id)
            if turn is None:
                turn = Turn(conversation_id=conversation_id)
                self._turns[session_id] = turn
            # ⚠️ 「创建这一轮的那一条进度消息」是一次**跨 send 的归属**：锁内领取、
            # 锁外发、锁内归还。这个字段与事件流那一侧**共用同一个**
            # （见 :attr:`~opencode_bridge.event_stream.Turn.progress_message_creating`
            # 的完整说明）—— 此前两条路径各自的 ``progress_handle is None`` 都在锁内
            # 读、而**发送与写回都在锁外**，于是「占位消息」与「流式首片」各发一条、
            # 其中一条的句柄被丢掉（读者把同一段正文读两遍，或看见一个永远停在
            # 「⏳ 处理中…」的僵尸气泡）。
            create_progress = (
                turn.progress_handle is None
                and not turn.progress_message_creating
            )
            if create_progress:
                turn.progress_message_creating = True
        # ⚠️ ``create_progress`` 为假（这一轮已经有句柄，或**有人正在创建**它）时这里
        # **不发**占位消息，而这**不丢任何东西**：占位文案本身没有信息量（正文一直记在
        # ``turn.parts`` 里），真正在创建的那条消息会承载它；它若发失败，
        # ``progress_handle`` 仍是 ``None``，收尾那一步照旧整段发出完整答复。
        # 反过来多发一条的代价则是实打实的：多一条用户看得见、却可能永远不会被答复
        # 改写的孤儿消息（平台没有「撤回」原语，它清不掉）。
        if create_progress:
            handle = None
            try:
                handle = self._send_text(
                    conversation_id,
                    PROGRESS_TEXT,
                    kind="progress",
                    adapter=adapter,
                    session_id=session_id,
                )
            finally:
                # 成功**和**抛异常都要把创建权还回去（见事件流那一侧的同一段注释）。
                with self._lock:
                    current = self._turns.get(session_id)
                    if current is not None:
                        current.progress_message_creating = False
                        if current.progress_handle is None:
                            current.progress_handle = handle
        return "ok"

    # ------------------------------------------------------------------
    # inbox recovery (startup)
    # ------------------------------------------------------------------
    def recover_inbox(self) -> None:
        """Replay whatever the last crash left in the inbox.

        **为什么夹在 SSE 线程与适配器之间**（两个邻居都不是随便选的）：

        * **在 SSE 线程之后** —— 重放出去的 prompt 必须有人接它的回复。
          ``session.execution.started`` 整条丢掉的话就没有 :class:`Turn`，
          收尾时既不发布结果也不刷队列，用户对这条消息什么都看不到 ——
          于是"修好丢消息"变成"重放出一条没有回复的消息"。
          为此这里等事件流确认连上（有上限）：服务端握手后发的第一帧
          （``server.connected``）到达即证明订阅已建立。
        * **在适配器之前** —— 此刻还没有实况入站，重放不会和用户的新消息
          并发打同一个会话（那种交错会把其中一条变成 409，甚至两次都成功）。
        * **告警仍然送得出去** —— 这正是看上去的矛盾点：``notify`` 需要可用
          的适配器，但扫描跑在 ``adapter.start()`` 之前。两者并不冲突：
          ``Adapter.send`` 是纯出站（``start()`` 只负责入站轮询；见
          ``adapters/telegram.py`` 的 ``send`` 与 ``start``），所以适配器
          已 attach 就能发。真的发不出去时 :meth:`BridgeCore._send_text` 记警告，这里
          再记一条 —— 这段告警只有这一条路，静默丢掉等于没告警。

        ``uncertain`` / ``abandoned`` **已经**被 :func:`recover_pending`
        告警过，这里只记日志，绝不重发。
        """
        inbox = self._inbox
        if inbox is None:
            logger.info(
                "write-ahead inbox disabled (no inbox injected); "
                "a crash during delivery loses that message silently"
            )
            return
        if not self._stream_confirmed.wait(
            timeout=_EVENT_STREAM_CONFIRMED_TIMEOUT_SECONDS
        ):
            logger.warning(
                "event stream not confirmed within %.1fs; running inbox recovery "
                "anyway (replays will most likely fail too — see the logs below)",
                _EVENT_STREAM_CONFIRMED_TIMEOUT_SECONDS,
            )

        def dispatch_recovered(queued: QueuedPrompt) -> None:
            """Hand one recovered prompt to opencode.

            Goes through the very same :meth:`_dispatch_prompt` a live message
            takes, which is also why the inbox bookkeeping is split the way it
            is: :func:`~.inbox_recovery.recover_pending` owns the writes here
            (it brackets this call with ``mark_attempting`` / ``mark_delivered``),
            so a second ``mark_failed`` from inside the dispatch would burn two
            retry-budget steps for one failure.

            Anything other than ``ok`` raises so recovery records ``failed`` and
            the next boot retries on the backoff ladder. ``busy`` included: at
            startup there is no in-memory queue to fall back into.
            """
            outcome = self._dispatch_prompt(queued, recording_delivery=False)
            if outcome != "ok":
                raise OpenCodeError(
                    f"replay of {queued.delivery_id} returned {outcome!r}"
                )

        def notify_recovered(conversation_id: str, alert_text: str) -> None:
            """Send one user-visible recovery alert; never let it vanish."""
            if self._send_text(conversation_id, alert_text, kind="text") is None:
                logger.warning(
                    "inbox recovery: could not deliver the alert for %s; "
                    "the user may never learn about it",
                    conversation_id,
                )

        logger.info("write-ahead inbox enabled; scanning for rows left by a crash")
        outcome = recover_pending(
            inbox,
            dispatch=dispatch_recovered,
            notify=notify_recovered,
        )
        logger.info(
            "inbox recovery done: %d replayed, %d uncertain (alerted, NOT replayed),"
            " %d abandoned (alerted)",
            len(outcome.replayed),
            len(outcome.uncertain),
            len(outcome.abandoned),
        )

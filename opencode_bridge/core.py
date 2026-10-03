"""Lane C — bridge core: session lifecycle, streaming, dispatch.

``BridgeCore`` implements the :class:`~opencode_bridge.hooks.Hooks` protocol:

* adapter polling threads call :meth:`on_inbound` / :meth:`on_callback`
* a dedicated daemon thread consumes ``client.subscribe()`` (SSE) and
  dispatches events to the streaming logic

The ``/xxx`` commands themselves live in :mod:`opencode_bridge.commands`
(AGENTS.md §5.1).  This module keeps the two places that decide *whether* a
command runs: the inbound entry point (:meth:`on_inbound` ->
:meth:`_handle_command`) and the ``setup:`` / ``perm:`` callback payloads
(:meth:`on_callback`).

Shared state (turns, queues, reverse session map) is guarded by one
``threading.RLock``.  Blocking I/O (HTTP calls, adapter sends) always happens
*outside* the lock so a slow network never freezes the other conversations.

Robustness rules:

* every event handler is wrapped in ``try/except Exception`` — a single bad
  event must never kill the SSE thread
* ``session.text.delta`` for an unknown ``sessionID`` is dropped silently
* every outbound text is sanitised (``\\x00`` and other control characters
  removed, undecodable surrogates replaced)
* ``adapter.edit`` is always wrapped: ``ValueError`` (text too long) falls
  back to ``adapter.send`` for finalisation
* inbound **prompt** text is written to the durable inbox (when one is
  injected) *before* it is dispatched, so a crash in the delivery window
  loses nothing silently; the durable policy lives in
  :mod:`opencode_bridge.inbox_recovery`, this module only wires it
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from typing import Any, Optional

from .adapters import Adapter
# ``/help``、``/setup`` 的冻结文案与命令实现都搬进了 :mod:`opencode_bridge.commands`
# （AGENTS.md §5.1）。这里**继续按 ``core`` 的路径再导出**其中被外部取用的名字：
# ``opencode_bridge.__main__`` 的 ``--setup`` 与测试写的都是
# ``from opencode_bridge.core import ...``，导入路径不能因为搬动而变。
# ``_setup_guide`` / ``_SETUP_ALIASES`` 不是再导出 —— :meth:`BridgeCore.on_callback`
# 处理 ``setup:`` 按钮回调时要读的**就是同一份**冻结文案，所以它 import 这里，
# 而不是复制第二份。
from .commands import (
    HELP_TEXT,
    SETUP_MENU_TEXT,
    CommandHandler,
    _SETUP_ALIASES,
    _setup_guide,
    setup_platforms,
    setup_reply,
)
from .config import Config
from .conversation_keys import ConversationState
# 事件流那一整块（订阅 SSE、归属过滤、流式渲染、turn 收尾）搬进了
# :mod:`opencode_bridge.event_stream`，连同只有它才写的状态一起（AGENTS.md §5.1）。
# ``Turn`` 也要 import：prompt 分发那侧要为新会话建一个 turn，所以它留在 core 的
# 视野里 —— 但类型本身归事件流所有（改一处只碰一个类）。
from .event_stream import EventStream, Turn
from .hooks import Inbound, MsgHandle, Outbound  # BridgeCore implements Hooks
from .identity import LEGACY_PREFIXES
from .inbox import InboundInbox, QueuedPrompt
from .inbox_recovery import recover_pending
from .normalize import _clean
from .opencode_client import OpenCodeClient, OpenCodeError
from .session_model import SessionModelCommand
from .state import StateStore

__all__ = [
    "BridgeCore",
    "HELP_TEXT",
    "NO_OUTPUT_TEXT",
    "ruleset_for",
    "SETUP_MENU_TEXT",
    "setup_reply",
    "setup_platforms",
]

logger = logging.getLogger("opencode_bridge.core")

DEFAULT_EDIT_INTERVAL = 1.5
DEFAULT_MAX_MESSAGE_CHARS = 4000
SESSION_TITLE_PREFIX = "tg-bridge:"
SESSION_TITLE_MAX = 60
PROGRESS_TEXT = "⏳ 处理中…"
NO_OUTPUT_TEXT = "（无输出）"

#: Values accepted in ``perm:<sessionID>:<reqID>:<decision>`` callbacks.
_PERM_DECISIONS = ("once", "always", "reject")

#: ``StateStore`` 里存放"消息流位置"的 meta 键。轮询型适配器（email 的 IMAP
#: UID 等）靠它跨重启续跑；见 :meth:`BridgeCore.load_stream_cursor`。
_STREAM_CURSOR_META_KEY = "stream_cursor"

#: 启动时等事件流确认连上的上限（秒），见 :meth:`BridgeCore._recover_inbox`。
#: 取 2 秒是因为 opencode 通常就在本机；而真的不可达时这 2 秒只换来一行告警 ——
#: 那种情况下恢复扫描本身也多半会失败，不该在这里死等。
_EVENT_STREAM_CONFIRMED_TIMEOUT_SECONDS = 2.0


def ruleset_for(mode: str | None) -> list[dict] | None:
    """Map ``permissions_mode`` to an OpenCode permissions rule set.

    ``"ask"`` (and anything unknown) -> ``None`` (server default = ask).
    """
    normalized = str(mode or "ask").strip().lower()
    if normalized == "allow":
        return [{"action": "*", "resource": "*", "effect": "allow"}]
    if normalized == "deny":
        return [{"action": "*", "resource": "*", "effect": "deny"}]
    if normalized != "ask":
        logger.warning(
            "unknown permissions_mode %r; falling back to 'ask'", mode
        )
    return None


# ----------------------------------------------------------------------
# 写前收件箱：从一条入站消息到收件箱里的一行
# ----------------------------------------------------------------------
# 这一段刻意是**模块级函数**而不是 :class:`BridgeCore` 的方法：那个类已经
# 51 个方法 / ~1160 行自有代码（AGENTS.md §5.1），纯逻辑的构造与记账不该再往里加。
def _queued_prompt_for(inbound: Inbound, text: str) -> QueuedPrompt:
    """Build the :class:`QueuedPrompt` row for one inbound message.

    The body is carried through **verbatim** — never prefixed, never rewritten.
    That text lands in the agent's context, so anything appended here (a
    "replayed after crash" note, say) would be read by the agent as part of the
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
# core
# ----------------------------------------------------------------------
class BridgeCore:
    """Routes IM messages to OpenCode sessions and streams events back."""

    def __init__(
        self,
        config: Config,
        client: OpenCodeClient,
        state: StateStore,
        inbox: InboundInbox | None = None,
    ) -> None:
        """``inbox`` enables the write-ahead inbox; ``None`` switches it off.

        Optional and last so that every existing construction site keeps working
        untouched — which also means a wiring bug would be invisible (nothing
        fails when the inbox is simply absent), so ``__main__`` logs which mode
        it started in and ``tests/test_inbox_wiring.py`` guards the wiring.
        """
        self.config = config
        self.client = client
        self.state = state
        self._inbox = inbox
        # ``/model`` 的全部逻辑（参数解析、模型目录缓存、回复文案）都在这个对象
        # 里，core 侧只留一个转发（AGENTS.md §5.1：这个类已经 50+ 个方法）。
        self.model_command = SessionModelCommand(
            client, ensure_session=self._ensure_session
        )

        self._lock = threading.RLock()
        self._adapters: list[Adapter] = []
        self._adapter_by_name: dict[str, Adapter] = {}
        #: 会话级状态读写（会话 ↔ opencode session 的映射）。
        #: ⚠️ **不是** ``self.state``：``channel:`` 这类**歧义**旧前缀的键该归谁，
        #: 只能由"发起查询的平台"回答 —— 而那条信息在本类手里（:attr:`Inbound.platform`
        #: 种下的 :attr:`_conv_adapter`）。:mod:`opencode_bridge.conversation_keys`
        #: 负责那条不相交划分，本类负责**显式**把平台传进去。
        #: 会话相关的读写**一律走它**；``self.state`` 只留给非会话键
        #: （邮件的流位置游标、``all_sessions`` 清单）。
        self.conversation_state = ConversationState(state, lambda: self.adapters)
        #: 斜杠命令那一整块（``/help`` ``/setup`` ``/new`` ``/stop`` ``/status``
        #: ``/cd`` ``/approve`` ``/deny`` 与 ``/model`` 的转发）。七个协作者
        #: **显式注入**（见 :class:`~opencode_bridge.commands.CommandHandler`）——
        #: 命令既不碰会话的流式状态也不碰事件流，搬出去就能脱离 core 单独测
        #: （AGENTS.md §5.1）。
        #: ⚠️ 构造点必须在 ``conversation_state`` **之后**：那是七个依赖之一。
        self.commands = CommandHandler(
            client=client,
            config=config,
            conversation_state=self.conversation_state,
            model_command=self.model_command,
            ensure_session=self._ensure_session,
            drop_session=self._drop_session,
            send_text=self._send_text,
        )
        #: conversation_id -> adapter name (learned on first inbound)
        self._conv_adapter: dict[str, str] = {}
        #: 活跃 turn：``session_id -> Turn``。
        #: ⚠️ 这个 dict 由 :class:`~opencode_bridge.event_stream.EventStream` **共有**
        #: （prompt 分发在这里建 turn，事件流在那边读改它），所以它留在本类、
        #: 作为同一个对象注入事件流 —— 不是复制一份，是同一把锁、同一个 dict。
        self._turns: dict[str, Turn] = {}
        self._queues: dict[str, list[QueuedPrompt]] = {}
        self._draining: set[str] = set()

        self._thread: threading.Thread | None = None
        self._started = False

        bridge_cfg = getattr(config, "bridge", None) or {}
        self.edit_interval = self._positive_float(
            bridge_cfg.get("edit_interval_seconds"), DEFAULT_EDIT_INTERVAL
        )
        self.max_message_chars = max(
            1, int(self._positive_float(bridge_cfg.get("max_message_chars"),
                                        DEFAULT_MAX_MESSAGE_CHARS))
        )

        #: 订阅 SSE、把执行过程渲染回 IM 的那一整块。十二个依赖**显式注入**
        #: （见 :class:`~opencode_bridge.event_stream.EventStream`）—— 其中
        #: ``lock`` / ``turns`` 是共用的状态（同上），其余是协作者。事件流不碰
        #: 收件箱、不碰入站、不碰生命周期，所以搬出去就能脱离 core 单独测
        #: （AGENTS.md §5.1）。
        self.event_stream = EventStream(
            client=client,
            state=state,
            lock=self._lock,
            turns=self._turns,
            # 可注入的单调时钟（测试冻结它来验证节流）—— 它只服务于流式节流，
            # 所以归事件流所有。
            clock=time.monotonic,
            edit_interval=self.edit_interval,
            max_message_chars=self.max_message_chars,
            adapter_for=self._adapter_for,
            send_text=self._send_text,
            edit_progress=self._edit_progress,
            finalize=self._finalize,
            flush_queue=self._flush_queue,
        )

        # 事件名以 **anomalyco/opencode v2.0.22 源码** 为准，不是文档
        # （`docs/server.mdx` 对 v2 已过时，仍列 v1 事件——那是推测的来源）。
        # 白名单见 `packages/schema/src/event-manifest.ts`；处理器表与那两个
        # frozenset（高频归属过滤 / 认识但故意不处理）一起搬进了
        # :mod:`opencode_bridge.event_stream`。
        #
        # ⚠️ 那里**故意没有** `session.idle` 与 `session.status`：
        # 源码里 `session.idle` 标注 `// deprecated`，而 `session.status`
        # 在 v2.0.22 **全代码库零处发布**。它们曾被当作"一轮结束"的信号，
        # 于是真实环境永远等不到收尾（症状：用户只看到 `⏳ 处理中…`）。
        # 结束信号只有一个：`session.execution.succeeded / .failed / .interrupted`。

    @staticmethod
    def _positive_float(value: Any, default: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        if number < 0 or number != number:  # negative or NaN
            return default
        return number

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    @property
    def adapters(self) -> tuple[Adapter, ...]:
        with self._lock:
            return tuple(self._adapters)

    def attach(self, adapter: Adapter) -> None:
        """Attach one messaging adapter (may be called multiple times)."""
        with self._lock:
            if any(a is adapter for a in self._adapters):
                return
            self._adapters.append(adapter)
            self._adapter_by_name.setdefault(adapter.name, adapter)
        logger.info("adapter attached: %s", adapter.name)

    def start(self) -> None:
        """Start the SSE reader thread, replay the inbox, then start adapters."""
        with self._lock:
            if self._started:
                return
            self._started = True
            thread = threading.Thread(
                target=self.event_stream.run, name="opencode-sse", daemon=True
            )
            self._thread = thread
        thread.start()
        logger.info("event stream thread started")
        # ⚠️ 位置有意义：SSE 线程**之后**、适配器**之前**（理由见 _recover_inbox）。
        self._recover_inbox()
        for adapter in self.adapters:
            try:
                adapter.start()
            except Exception:
                logger.exception("adapter %s failed to start", adapter.name)
        logger.info("bridge core started with %d adapter(s)", len(self.adapters))

    def stop(self) -> None:
        """Stop adapters, close the client and join the SSE thread.

        Safe to call twice; every step is exception-isolated so that a
        ``KeyboardInterrupt`` can always unwind cleanly.
        """
        logger.info("bridge core stopping ...")
        for adapter in self.adapters:
            try:
                adapter.stop()
            except Exception:
                logger.exception("adapter %s failed to stop", adapter.name)
        try:
            self.client.close()
        except Exception:
            logger.exception("client close failed")
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            try:
                thread.join(timeout=5.0)
            except Exception:  # pragma: no cover - defensive
                logger.exception("event thread join failed")
            if thread.is_alive():
                logger.warning("event thread did not exit within 5s")
        self._thread = None
        logger.info("bridge core stopped")

    # ------------------------------------------------------------------
    # adapter routing
    # ------------------------------------------------------------------
    def _route_by_prefix(
        self, conversation_id: str, adapters: list[Adapter]
    ) -> Adapter:
        """按 ``conversation_id`` 的平台段猜适配器 —— **兜底路径，不是主路径**。

        ⚠️ **主路径是 :meth:`_remember_platform`**：入站时产生这条消息的适配器
        自己就知道自己是谁（``Inbound.platform``），那里没有不确定性。这里只在
        "这个目标从未收到过入站消息"（例如让agent 主动往某个 chat 发消息）时才会被走到。

        **为什么这里必须覆盖全部前缀**：A1 迁移打掉了"按调用线程判断归属"那层保护
        —— 已迁移的适配器不再持有 ``_thread``，``_adapter_for`` 里的线程匹配恒不命中。
        而本方法此前只硬编码了 ``chat:`` 与 ``channel:`` 两种，其余一律
        ``adapters[0]``；``attach()`` 的顺序就是**配置文件里的字典顺序**（用户可控），
        于是多平台用户可能把回复发到**错误的平台** —— 且不报错。
        """
        def named(name: str) -> Adapter | None:
            for adapter in adapters:
                if adapter.name == name:
                    return adapter
            return None

        cid = str(conversation_id or "")
        head, sep, rest = cid.partition(":")

        # 旧别名以 identity 的登记表为准，避免这里变成第二份真相。
        # 值为 None 表示**歧义**前缀（``channel:`` 被 slack/discord/mattermost 共用）。
        legacy = LEGACY_PREFIXES.get(head, "missing")
        if legacy is None:
            # 歧义前缀只能启发式：Slack id 以 C/D 开头、Discord 是纯数字、
            # Mattermost 是 26 位 base32。**这仍然可能猜错** —— 真正的确定性来自
            # :meth:`_remember_platform`，这里只是没有别的办法时的兜底。
            #
            # ⚠️ 别被"我们知道各家文法不相交"带偏：那个划分只许用在
            # :mod:`opencode_bridge.conversation_keys` 的**读侧归属判定**上，
            # **绝不许**拿它来路由出站消息（那是按前缀猜目标，不是认领键）。
            if rest.isdigit():
                return named("discord") or named("slack") or adapters[0]
            return named("slack") or named("discord") or adapters[0]
        if legacy != "missing" and legacy != head:
            # 真正的别名（``chat``→telegram、``room``→matrix）
            hit = named(legacy)
            if hit is not None:
                return hit

        # 新格式 ``platform:local_id``（以及映射到自身的 irc/twitch/nextcloud）：
        # **平台段本身就是答案**，不需要任何猜测。
        if sep:
            hit = named(head)
            if hit is not None:
                return hit
        return adapters[0]

    def _remember_platform(self, conversation_id: str, platform: str) -> None:
        """记下"这个会话属于哪个适配器" —— 用的是**准确**信息。

        产生这条入站消息的适配器就是它自己（``Inbound.platform``），所以这一步
        没有不确定性。对比 :meth:`_route_by_prefix` 的前缀猜测：猜错的后果是把回复
        发到**另一个平台**，且不报错、只表现为"用户发现回复跑错了地方"。

        A1 迁移之前这里还有第二个来源 —— "调用线程是否等于某适配器的 ``_thread``"。
        迁移后该字段恒为 ``None``，那条路失效了，所以必须靠本方法兜住。
        """
        name = str(platform or "").strip()
        if not name or not conversation_id:
            return
        with self._lock:
            self._conv_adapter[conversation_id] = name

    def _asking_platform(self, conversation_id: str) -> str:
        """这条会话的**提问平台** —— 歧义旧键归属划定的唯一输入。

        来源是 :meth:`_remember_platform`：入站时由产生这条消息的适配器**自己**报上
        来的 :attr:`~opencode_bridge.hooks.Inbound.platform`。这是准确信息，
        :meth:`_adapter_for` 里那条"前缀猜出来的"兜底只会写进**旧格式** id 的条目，
        而旧格式 id 在读侧根本不会走到归属判定（它按精确键读）。

        从未收到过入站消息的会话（agent 主动外发）返回空串 —— 那时没有"提问方"，
        也就没有平台有权认领旧键，于是 :meth:`ConversationState` 不做任何回退。

        调用点大多会**显式**把平台传进来（收件箱行 / 命令的适配器），本方法是那些
        拿不到上下文的路径（:class:`~opencode_bridge.session_model.SessionModelCommand`
        通过注入的 ``ensure_session`` callable 回调进来）的兜底。
        """
        with self._lock:
            return self._conv_adapter.get(str(conversation_id or ""), "")

    def _adapter_for(self, conversation_id: str) -> Adapter | None:
        """Pick the adapter that owns ``conversation_id``.

        Order: remembered mapping -> calling polling thread -> conversation
        id prefix -> first attached adapter.
        """
        with self._lock:
            remembered = self._conv_adapter.get(conversation_id)
            if remembered:
                adapter = self._adapter_by_name.get(remembered)
                if adapter is not None:
                    return adapter
            adapters = list(self._adapters)
        if not adapters:
            return None

        chosen: Adapter | None = None
        current = threading.current_thread()
        for adapter in adapters:
            # Lane B keeps its poller thread in ``_thread``; matching the
            # thread tells us which adapter invoked the hook.
            poller = getattr(adapter, "_thread", None)
            if poller is not None and poller is current:
                chosen = adapter
                break
        if chosen is None:
            chosen = self._route_by_prefix(conversation_id, adapters)
        with self._lock:
            self._conv_adapter[conversation_id] = chosen.name
        return chosen

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
            text = _clean(inbound.text).strip()
            if not conversation_id or not text:
                return
            adapter = self._adapter_for(conversation_id)
            if adapter is None:
                logger.warning(
                    "no adapter attached; dropping message for %s",
                    conversation_id,
                )
                return
            if text.startswith("/"):
                # ⚠️ 命令**绝不**写前落盘。命令由 core 自己就地执行、从不经过
                # prompt()，所以它永远不会走到 mark_delivered —— 那一行会永远留在
                # 收件箱里，于是每次启动都被重放一遍：`/new` 每次重启都重建会话、
                # `/setup` 每次都重发引导。那比要修的丢消息 bug 更糟。
                # 落盘必须留在 else 分支里（tests/test_inbox_wiring.py 锁住这条）。
                self._handle_command(conversation_id, adapter, text)
            else:
                queued = _record_inbound(self._inbox, inbound, text)
                if queued is not None:  # None = 去重命中，已投递过
                    self._enqueue(queued)
        except Exception:
            logger.exception("on_inbound failed")

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
            try:
                self.client.reply_permission(session_id, request_id, decision)
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
            self._answer(adapter, query_id, "已处理")
        except Exception:
            logger.exception("on_callback failed")
            self._answer(adapter, query_id, "失败")

    # ------------------------------------------------------------------
    # Hooks: 消息流游标（落盘，见 hooks.py 的说明）
    # ------------------------------------------------------------------
    def load_stream_cursor(self, stream_scope: str) -> Optional[int]:
        """实现 ``Hooks.load_stream_cursor``：读回某条消息流上次的位置。

        ``stream_scope`` 不是会话 id，只是 ``state.json`` 里的一个**不透明键**，
        由适配器保证稳定且互不撞车（email 用"账号 + 邮箱"）。

        存的不是整数（被手改坏 / 旧版本写入的别的类型）时按"没有已存位置"
        处理并告警 —— 退化方向必须是"重新走首次启动语义"，不能是"拿着垃圾值
        去算 UID 区间"。
        """
        stored = self.state.get_meta(str(stream_scope), _STREAM_CURSOR_META_KEY, None)
        if stored is None:
            return None
        try:
            return int(stored)
        except (TypeError, ValueError):
            logger.warning("stream cursor %r is not an integer; ignoring it", stored)
            return None

    def save_stream_cursor(self, stream_scope: str, position: int) -> None:
        """实现 ``Hooks.save_stream_cursor``：把某条消息流的位置写进 state。

        写失败只告警、不上抛：位置丢了最坏是重启后重投一封（由写前日志兜底），
        而让异常冒到适配器的轮询线程会把整个收信循环打断。
        """
        try:
            self.state.set_meta(str(stream_scope), _STREAM_CURSOR_META_KEY, int(position))
        except Exception as exc:  # noqa: BLE001 - 落盘失败不该打断收信
            logger.warning(
                "cannot persist the stream cursor for %s (%s); a restart may "
                "re-process messages that were already handled",
                stream_scope, exc,
            )

    @staticmethod
    def _answer(adapter: Adapter | None, query_id: str, text: str) -> None:
        if adapter is None or not query_id:
            return
        try:
            adapter.answer(query_id, text)
        except Exception:
            logger.exception("adapter.answer failed")

    # ------------------------------------------------------------------
    # outbound helpers
    # ------------------------------------------------------------------
    def _send_text(
        self,
        conversation_id: str,
        text: str,
        *,
        kind: str = "text",
        adapter: Adapter | None = None,
        session_id: str | None = None,
    ) -> MsgHandle | None:
        adapter = adapter or self._adapter_for(conversation_id)
        if adapter is None:
            logger.warning("no adapter for %s; cannot send", conversation_id)
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

    def _edit_progress(
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

    def _finalize(
        self, conversation_id: str, handle: MsgHandle | None, text: str,
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
        if handle is not None and len(final) <= self.max_message_chars:
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
        self._send_text(
            conversation_id, final, kind=kind, adapter=adapter,
            session_id=session_id,
        )

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

    def _flush_queue(self, conversation_id: str) -> None:
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
                    # 409 是"还没轮到"，不是失败：不写 failed，重试预算分文未花
                    # （收件箱也没有"撤销 attempting"的转换，所以那一行停在
                    # attempting —— 进程内重投成功后就转 delivered）。
                    # 这一条退回内存队列，等 _flush_queue。
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
            #: 它是归属划分需要的那个"提问平台"（启动重放路径上 ``_conv_adapter``
            #: 可能还是空的，这一行就不依赖它）。
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
            self.client.prompt(session_id, queued.text)
        except OpenCodeError as exc:
            if exc.status == 409:
                logger.info(
                    "session %s busy; message queued for %s",
                    session_id,
                    conversation_id,
                )
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
            need_progress = turn.progress_handle is None
        if need_progress:
            handle = self._send_text(
                conversation_id,
                PROGRESS_TEXT,
                kind="progress",
                adapter=adapter,
                session_id=session_id,
            )
            with self._lock:
                current = self._turns.get(session_id)
                if current is not None and current.progress_handle is None:
                    current.progress_handle = handle
        return "ok"

    # ------------------------------------------------------------------
    # inbox recovery (startup)
    # ------------------------------------------------------------------
    def _recover_inbox(self) -> None:
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
          已 attach 就能发。真的发不出去时 :meth:`_send_text` 记警告，这里
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
        if not self.event_stream.stream_confirmed.wait(
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

    # ------------------------------------------------------------------
    # session lifecycle
    # ------------------------------------------------------------------
    def _ensure_session(self, conversation_id: str, *, platform: str = "") -> str:
        """取这条会话的 opencode session，没有就建一个。

        :param platform: **发起这次读取的平台**，显式传给
            :class:`~opencode_bridge.conversation_keys.ConversationState` 去判定
            ``channel:`` 旧键的归属。留空时退回 :meth:`_asking_platform`
            （入站时种下的准确映射）；那条路只服务"拿不到上下文"的调用方，
            :class:`~opencode_bridge.session_model.SessionModelCommand` 就是。
        """
        asking = platform or self._asking_platform(conversation_id)
        session_id = self.conversation_state.get_session(
            conversation_id, platform=asking
        )
        if session_id:
            return session_id
        directory = (
            self.conversation_state.get_meta(
                conversation_id, "directory", None, platform=asking
            )
            or self.config.opencode_directory
            or "."
        )
        # ⚠️ 必须解析成绝对路径再发。opencode 的 `POST /api/session` 对
        # `location.directory` 的**相对路径**（含默认的 "."）一律返回 **500 且响应体为空**，
        # 错误信息因此完全丢失，桥只能报"HTTP 500"这种没有信息量的错。
        # 2026-10-03 A4 真实服务端验证时实测：绝对路径 200 / 空串 200 / "." 500（5/5 稳定复现）。
        #
        # 这里做 abspath 而不是要求用户配绝对路径，有两个理由：
        #   1. `opencode_directory` 的默认值就是 "."（见 config.py），语义是"当前目录"——
        #      把"当前目录"解析成绝对路径是它本来的意思，不该让用户为默认值买单；
        #   2. 上面那个 `or "."` 兜底意味着即使配置为空也必然踩中，不解析就必然失败。
        #
        # 回环测试抓不到这个 bug：测试都传绝对路径或临时目录，只有真实默认配置会中招。
        directory = os.path.abspath(directory)
        title = f"{SESSION_TITLE_PREFIX}{conversation_id}"[:SESSION_TITLE_MAX]
        agent = self.config.opencode_agent or None
        rules = ruleset_for(self.config.permissions_mode)
        try:
            session_id = self.client.create_session(
                directory=directory,
                title=title,
                agent=agent,
                permissions=rules,
            )
        except OpenCodeError as exc:
            if exc.status != 400 or rules is None:
                raise
            logger.warning(
                "create_session rejected permissions (%s); retrying without",
                exc,
            )
            session_id = self.client.create_session(
                directory=directory,
                title=title,
                agent=agent,
                permissions=None,
            )
        self.conversation_state.set_session(conversation_id, session_id)
        logger.info(
            "created session %s for %s (dir=%s)", session_id, conversation_id,
            directory,
        )
        return session_id

    def _drop_session(self, conversation_id: str, *, platform: str = "") -> str | None:
        """Delete the current session server-side and locally (never raises).

        ``platform`` 语义同 :meth:`_ensure_session`：它决定 :meth:`drop_session`
        要不要连带删掉那个 ``channel:`` 旧键 —— 只删**本平台文法覆盖得到**的那个。
        """
        asking = platform or self._asking_platform(conversation_id)
        session_id = self.conversation_state.get_session(
            conversation_id, platform=asking
        )
        if session_id:
            try:
                self.client.delete_session(session_id)
            except Exception as exc:
                logger.warning("delete_session(%s) failed: %s", session_id, exc)
            self.conversation_state.drop_session(conversation_id, platform=asking)
            with self._lock:
                self._turns.pop(session_id, None)
        return session_id

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------
    def _handle_command(
        self, conversation_id: str, adapter: Adapter, text: str
    ) -> None:
        """命令逻辑在 :mod:`opencode_bridge.commands`，这里只转发。

        调用点留在本类（:meth:`on_inbound`），因为"命令绝不写前落盘"那条决定
        就在那儿 —— 判断该不该落盘的人必须和执行命令的人在一起看。
        """
        self.commands.handle_command(conversation_id, adapter, text)

"""Lane C — bridge core: lifecycle and the wiring hub.

``BridgeCore`` implements the :class:`~opencode_bridge.hooks.Hooks` protocol:

* adapter polling threads call :meth:`on_inbound` / :meth:`on_callback`
* a dedicated daemon thread consumes ``client.subscribe()`` (SSE) and
  dispatches events to the streaming logic

Seven clusters have been extracted (AGENTS.md §5.1), each into its own module
with its own private state, and each **injected** here rather than reached for:

* :mod:`opencode_bridge.commands` — the ``/xxx`` commands
* :mod:`opencode_bridge.event_stream` — SSE subscription, streaming, turn teardown
* :mod:`opencode_bridge.inbound_gateway` — inbound messages, button callbacks,
  the per-conversation prompt queue and the write-ahead inbox
* :mod:`opencode_bridge.adapter_router` — which adapter owns a conversation
* :mod:`opencode_bridge.session_registry` — get-or-create the opencode session
* :mod:`opencode_bridge.outbound` — every send / progress edit / finalisation
* :mod:`opencode_bridge.stream_cursor` — how far a polled message stream got

What is left here is the hub and nothing else: lifecycle, the ``Hooks`` protocol
itself, and the assembly that wires those seven together (AGENTS.md §5.1 wants
under ~15 methods / ~250 own lines; this class is now well inside both).

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
  loses nothing silently; that wiring lives in
  :mod:`opencode_bridge.inbound_gateway`, the durable policy in
  :mod:`opencode_bridge.inbox_recovery`
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

from .adapters import Adapter
from .adapter_router import AdapterRouter
# ``/help``、``/setup`` 的冻结文案与命令实现都搬进了 :mod:`opencode_bridge.commands`
# （AGENTS.md §5.1）。这里**继续按 ``core`` 的路径再导出**其中被外部取用的名字：
# ``opencode_bridge.__main__`` 的 ``--setup`` 与测试写的都是
# ``from opencode_bridge.core import ...``，导入路径不能因为搬动而变。
from .commands import (
    HELP_TEXT,
    SETUP_MENU_TEXT,
    CommandHandler,
    setup_platforms,
    setup_reply,
)
from .config import Config
from .conversation_keys import ConversationState
from .event_stream import EventStream, Turn
from .health import platform_key, probe_after_start
from .hooks import Inbound  # BridgeCore implements Hooks
# 入站那一整块（适配器进来的两个 hook、每会话队列、写前收件箱与启动重放）搬进了
# :mod:`opencode_bridge.inbound_gateway`，连同只有它才写的状态一起（AGENTS.md §5.1）。
# ``Turn`` 也要 import：turn 表留在 core 的视野里（事件流与入站那一侧共用同一个
# dict），但类型本身归事件流所有 —— 改一处只碰一个类。
from .inbound_gateway import InboundGateway
from .inbox import InboundInbox
# ``NO_OUTPUT_TEXT`` 与 ``ruleset_for`` 跟着各自的实现搬走了（出站那一侧与会话
# 映射那一侧），但它们在 ``__all__`` 里、``tests/test_core.py`` 与 ``__main__``
# 都按 ``core`` 的路径 import —— 所以这里**再导出**，导入路径不能因为搬动而变。
from .outbound import NO_OUTPUT_TEXT, OutboundSender
from .opencode_client import OpenCodeClient
from .permission_ledger import PermissionLedger
from .session_model import SessionModelCommand
from .session_registry import SessionRegistry, ruleset_for
from .state import StateStore
from .stream_cursor import StreamCursorStore

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

#: Minimum seconds between two rewrites of the same progress message while an
#: answer streams in — the throttle in
#: :meth:`~opencode_bridge.event_stream.EventStream._on_text_delta`
#: (``if (now - turn.last_edit_ts) < self._edit_interval: return``). Lower = the
#: reader watches the answer grow more smoothly; higher = fewer API calls and less
#: risk of a platform rate limit.
#:
#: **What kind of number: 出处不明 — no derivation found.** It arrived in the
#: initial commit ``5ce06e0`` (2026-10-01) with no comment and no commit-message
#: rationale, and has never been touched since (``git log -S`` finds that single
#: commit). It matches no reference project's value either: hermes uses
#: ``DEFAULT_STREAMING_EDIT_INTERVAL = 0.8`` *plus* an adaptive backoff and
#: flood-strike escalation, and dsh has no edit-interval constant at all. No
#: measurement of real IM edit cadence is recorded anywhere in this repo — so
#: **do not read 1.5 as a measured result.** It is an unexamined starting value.
#:
#: ⚠️ It is **not** the body-coalescing window §6.3 rule 2 asks for. Coalescing
#: happens by ordinal in :meth:`~opencode_bridge.event_stream.Turn.assemble`
#: (deltas are bucketed per ``(assistantMessageID, ordinal)`` and merged); there
#: is no time-window coalescing in this codebase. This constant only bounds how
#: often an already-assembled body gets rewritten.
DEFAULT_EDIT_INTERVAL = 1.5

#: The **bridge's own** budget for one message the bridge itself writes or edits
#: (the progress placeholder, and the finalising edit that completes it). It is
#: **not** a platform limit and not a safe send size.
#:
#: **What kind of number: 出处不明 — no derivation found**, same provenance as
#: :data:`DEFAULT_EDIT_INTERVAL` (initial commit ``5ce06e0``, no comment, never
#: changed). It happens to equal ``maxMessageLength: 4000``, the constant
#: ``zhuiyueya/dsh-im-gateway`` uses for the same concept across its channels —
#: and this repo's own design reference flags exactly that as dsh's anti-pattern
#: ("4000 是猜的默认值而非各平台真实上限"). A transcription is plausible and
#: **unproven**: ``5ce06e0`` shows no sign of consulting the reference projects,
#: and AGENTS.md §6's "check the reference projects first" rule was added two
#: days later.
#:
#: **Per-platform truth lives on the adapter, never here.** Read
#: :attr:`~opencode_bridge.adapters.base.Adapter.max_message_length` (the
#: declared static floor), refined at runtime through the
#: :attr:`~opencode_bridge.adapters.base.Adapter.message_limit` slot (declared
#: ``0`` = "not refined"; Mattermost / Nextcloud fill it from the server), and
#: ⚠️ **This default exceeds the real limit on 5 of the 13 platforms** (measured
#: 2026-10-05): ``discord`` 2000, ``email`` 998, ``irc`` 400, ``qqbot`` 2000,
#: ``twitch`` 400. ``mattermost``'s static floor merely *coincides* at 4000 (it
#: is refined from ``config/client`` ``MaxPostSize`` at runtime), so it is not
#: counted as "exceeded".
#:
#: Always take the resolved value from
#: :attr:`~opencode_bridge.adapters.base.Adapter.effective_max_length` — that
#: property is the single resolver (``adapters/base.py:190``).
#:
#: Both readers now narrow this value through **one** function,
#: :func:`~opencode_bridge.outbound.one_message_budget`:
#: the streaming gate (``event_stream.py``) and
#: :meth:`~opencode_bridge.outbound.OutboundSender.finalize`.
#:
#: ⚠️ **This paragraph used to document an open residual, and deliberately so.**
#: The streaming gate once compared against this value **un-narrowed** while
#: ``finalize`` narrowed it, so on a platform below 4000 a body between the two
#: passed the gate; the adapter split it and handed back the last chunk's
#: handle while ``Turn.shown_progress_text`` recorded the whole body, and
#: ``finalize``'s lower bound ``max(len(head), len(shown_progress_text))``
#: restored the full length — the edit went out over the platform limit.
#: Cosmetic rather than data loss (the reader still got the complete answer
#: exactly once), but it also falsified the invariant written at
#: ``outbound.py``'s ``_split_for_the_placeholder`` that *``shown_progress_text``
#: fits the budget by construction*.
#:
#: **Closed** by narrowing the gate to the same quantity. The two alternatives
#: were traced and both rejected: clamping ``shown_progress_text`` makes the
#: suffix too long and **re-sends** the span (duplication), and clamping inside
#: ``finalize`` makes the edit succeed at ``final[:budget]`` over a message that
#: already showed past it — the reader sees that span **twice**. Only narrowing
#: the gate keeps "the reader gets the answer exactly once" true.
DEFAULT_MAX_MESSAGE_CHARS = 4000


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
        self._lock = threading.RLock()
        #: 活跃 turn：``session_id -> Turn``。
        #: ⚠️ 这个 dict 由三个协作者**共有**：:class:`~opencode_bridge.event_stream.EventStream`
        #: 读改它、:class:`~opencode_bridge.inbound_gateway.InboundGateway` 为新会话建它、
        #: :class:`~opencode_bridge.session_registry.SessionRegistry` 删会话时弹掉它。
        #: 所以它留在本类、作为**同一个对象**注入三者 —— 不是复制一份，是同一个 dict。
        self._turns: dict[str, Turn] = {}
        self._thread: threading.Thread | None = None
        self._started = False
        #: 「上次启动」各适配器的探测结论（``{平台键: {verdict, code, detail}}``）。
        #: 在 :meth:`start` 那一轮收集完，**由调用方（运行器）落盘** ——
        #: 本类不知道 ``bridge_dir`` 在哪，而适配器更不知道（那是
        #: :mod:`opencode_bridge.health` 存在的理由）。给默认值而不是只在
        #: :meth:`start` 里凭空长出来，于是"没启动过"与"启动了但没人探测"
        #: 对消费者都是同一个可读的形状（空 dict），不是 ``AttributeError``。
        self.startup_probes: dict[str, dict] = {}
        #: 权限请求的本地账本（C4）。**必须早于** ``commands`` /
        #: ``event_stream`` / ``inbound_gateway`` 三处构造 —— 三个都要用到**同一个**
        #: 对象，否掉迟到回答才可能（见
        #: :mod:`opencode_bridge.permission_ledger`）。它是跨三块的**共有状态**，
        #: 归属在本类，与 ``_lock`` / ``_turns`` 同一性质。
        self.permission_ledger = PermissionLedger()

        bridge_cfg = getattr(config, "bridge", None) or {}
        self.edit_interval = self._positive_float(
            bridge_cfg.get("edit_interval_seconds"), DEFAULT_EDIT_INTERVAL
        )
        self.max_message_chars = max(
            1, int(self._positive_float(bridge_cfg.get("max_message_chars"),
                                        DEFAULT_MAX_MESSAGE_CHARS))
        )

        #: 适配器挂载表与归属判定（AGENTS.md §5.1）。它拥有 ``_adapters`` /
        #: ``_adapter_by_name`` / ``_conv_adapter``，并**第一个**构造 ——
        #: 下面每一个协作者的 ``adapter_for`` 都是从它这里取的。
        self.routing = AdapterRouter(lock=self._lock)
        #: 会话级状态读写（会话 ↔ opencode session 的映射）。
        #: ⚠️ **不是** ``self.state``：``channel:`` 这类**歧义**旧前缀的键该归谁，
        #: 只能由"发起查询的平台"回答 —— 而那条信息在 :class:`AdapterRouter` 手里
        #: （:attr:`~opencode_bridge.hooks.Inbound.platform` 种下的
        #: :attr:`_conv_adapter`）。:mod:`opencode_bridge.conversation_keys`
        #: 负责那条不相交划分，取那一侧负责**显式**把平台传进去。
        #: 会话相关的读写**一律走它**；``self.state`` 只留给非会话键
        #: （邮件的流位置游标、``all_sessions`` 清单）。
        self.conversation_state = ConversationState(state, lambda: self.adapters)
        #: 出站那一条路：发信、进度改写、收尾、按钮应答（AGENTS.md §5.1）。
        #: ⚠️ 这三个方法早就以注入 callable 的形式被事件流、入站那一侧与命令簇
        #: 共用了 —— 搬的是实现，签名一个字没改，所以下面三处构造毫无涟漪。
        self.outbound = OutboundSender(
            adapter_for=self.routing.adapter_for,
            max_message_chars=self.max_message_chars,
        )
        #: 轮询型适配器的消息流读位置（跨重启续跑）。与会话映射是两回事，
        #: 所以不并进 :class:`~opencode_bridge.session_registry.SessionRegistry`。
        self.stream_cursor = StreamCursorStore(state=state)
        #: 会话 ↔ opencode session 的取/建/删。``lock`` / ``turns`` 是共用的状态，
        #: ``asking_platform`` 是 :class:`AdapterRouter` 的协作者（AGENTS.md §5.1）。
        self.sessions = SessionRegistry(
            client=client,
            config=config,
            conversation_state=self.conversation_state,
            lock=self._lock,
            turns=self._turns,
            asking_platform=self.routing.asking_platform,
            # ⚠️ 删会话时弹掉在跑的那一轮要让读者知道（否则那条 `⏳ 处理中…` 变成
            # 僵尸气泡，而用户零反馈）⇒ 注入**出站那一条**，它按平台能力决定改写
            # 占位消息还是补发一句。⛔ 绝不发半截正文（用户 2026-10-07 拍板）。
            cancel_turn=self.outbound.cancel_turn,
        )
        #: ``/model`` 的全部逻辑（参数解析、模型目录缓存、回复文案）都在这个对象
        #: 里，core 侧只把它当协作者传下去。
        self.model_command = SessionModelCommand(
            client, ensure_session=self.sessions.ensure_session
        )
        #: 斜杠命令那一整块（``/help`` ``/setup`` ``/new`` ``/stop`` ``/status``
        #: ``/cd`` ``/approve`` ``/deny`` 与 ``/model`` 的转发）。八个协作者
        #: **显式注入**（见 :class:`~opencode_bridge.commands.CommandHandler`）。
        #: ⚠️ 构造点必须在 ``sessions`` / ``outbound`` **之后**：它们是依赖之一。
        self.commands = CommandHandler(
            client=client,
            config=config,
            conversation_state=self.conversation_state,
            model_command=self.model_command,
            ensure_session=self.sessions.ensure_session,
            drop_session=self.sessions.drop_session,
            send_text=self.outbound.send_text,
            permission_ledger=self.permission_ledger,
        )

        #: 订阅 SSE、把执行过程渲染回 IM 的那一整块。十三个依赖**显式注入**
        #: （见 :class:`~opencode_bridge.event_stream.EventStream`）—— 其中
        #: ``lock`` / ``turns`` 是共用的状态（同上），其余是协作者。事件流不碰
        #: 收件箱、不碰入站、不碰生命周期，所以搬出去就能脱离 core 单独测
        #: （AGENTS.md §5.1）。
        #: ⚠️ ``flush_queue`` 传的是 :meth:`_flush_queue` 这个**转发**，因为入站
        #: 那一侧要拿事件流的 ``stream_confirmed``（反方向），两边互为对方的前置
        #: —— 所以这里靠转发打破构造顺序，而不是让谁去认识对方。
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
            adapter_for=self.routing.adapter_for,
            send_text=self.outbound.send_text,
            edit_progress=self.outbound.edit_progress,
            finalize=self.outbound.finalize,
            flush_queue=self._flush_queue,
            permission_ledger=self.permission_ledger,
        )

        #: 适配器进来的两个 hook、每会话的 prompt 队列、写前收件箱与启动重放。
        #: 十二个依赖**显式注入**（见
        #: :class:`~opencode_bridge.inbound_gateway.InboundGateway`）—— 其中
        #: ``lock`` / ``turns`` 是共用的状态（同上），``inbox`` 从构造参数透传。
        #: 入站这一侧不碰事件流的状态，只等它的 ``stream_confirmed``（AGENTS.md §5.1）。
        #: ⚠️ 构造点必须在 ``event_stream`` **之后**：``stream_confirmed`` 归事件流所有。
        self.inbound_gateway = InboundGateway(
            client=client,
            lock=self._lock,
            turns=self._turns,
            inbox=inbox,
            stream_confirmed=self.event_stream.stream_confirmed,
            adapter_for=self.routing.adapter_for,
            answer_callback=self.outbound.answer,
            ensure_session=self.sessions.ensure_session,
            # 命令就地转发：判断"该不该写前落盘"的人在 InboundGateway.on_inbound，
            # 执行命令的人在 CommandHandler —— 两者之间只需要这条 callable。
            handle_command=self.commands.handle_command,
            remember_platform=self.routing.remember_platform,
            send_text=self.outbound.send_text,
            permission_ledger=self.permission_ledger,
            bridge_config=getattr(config, "bridge", None) or {},
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
        """``Hooks.adapters`` 的实现 —— 挂载表归
        :class:`~opencode_bridge.adapter_router.AdapterRouter` 所有。
        """
        return self.routing.adapters

    def attach(self, adapter: Adapter) -> None:
        """Attach one messaging adapter (may be called multiple times)."""
        self.routing.attach(adapter)

    def start(self) -> None:
        """Start the SSE reader thread, replay the inbox, then start adapters.

        这一轮顺带收集每个适配器的**启动探测结论**到 :attr:`startup_probes`，
        由**运行器**在全部适配器都试过之后统一落一次盘。⚠️ **收集必须在这里**，
        而不是让调用方在 :meth:`start` 返回后再遍历一遍 —— ``start()`` 抛出的异常
        在这里被接住并记了日志（调用方再也看不到它），而"抛异常"恰恰是最需要被
        用户看见的那种失败。
        """
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
        # ⚠️ 位置有意义：SSE 线程**之后**、适配器**之前**（理由见
        # InboundGateway.recover_inbox）。
        self.inbound_gateway.recover_inbox()
        probes: dict[str, dict] = {}
        for adapter in self.adapters:
            key = platform_key(adapter)
            try:
                start_result = adapter.start()
            except Exception as exc:
                logger.exception("adapter %s failed to start", adapter.name)
                probe = probe_after_start(adapter, start_error=exc)
                if probe is not None:
                    probes[key] = probe
                continue
            probe = probe_after_start(adapter, start_result)
            if probe is not None:
                probes[key] = probe
        self.startup_probes = probes
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
        # ⚠️ 顺序：**先叫停看护者，再关客户端**。反过来时，处于退避等待中的 SSE
        # 看护者不会被客户端关闭唤醒（它等的是自己那个 Event），于是
        # ``join(5.0)`` 有可能刚好等不满、平白打一行 "did not exit within 5s"。
        try:
            self.event_stream.request_stop()
        except Exception:
            logger.exception("event stream stop request failed")
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
    # Hooks: inbound
    #   （实现转发给 :class:`~opencode_bridge.inbound_gateway.InboundGateway`）
    # ------------------------------------------------------------------
    def on_inbound(self, inbound: Inbound) -> None:
        """每条入站消息的入口 —— 逻辑在入站那一侧，这里只转发。

        :class:`Hooks` 的实现必须留在本类（适配器是照这个协议调 core 的），
        而"要不要写前落盘"、排队、投递、收件箱记账都在入站那一侧，所以这里不重复。
        """
        self.inbound_gateway.on_inbound(inbound)

    def on_callback(
        self, conversation_id: str, data: str, query_id: str
    ) -> None:
        """按钮回调（``setup:`` / ``perm:``）—— 同样只转发，见 :meth:`on_inbound`。"""
        self.inbound_gateway.on_callback(conversation_id, data, query_id)

    # ------------------------------------------------------------------
    # Hooks: 消息流游标（落盘，见 hooks.py 的说明）
    #   （实现在 :class:`~opencode_bridge.stream_cursor.StreamCursorStore`）
    # ------------------------------------------------------------------
    def load_stream_cursor(self, stream_scope: str) -> Optional[int]:
        """实现 ``Hooks.load_stream_cursor``：读回某条消息流上次的位置。

        ``Hooks`` 的实现必须留在本类（适配器是照这个协议调 core 的）；
        位置怎么存、坏值怎么退化，都在 :mod:`opencode_bridge.stream_cursor` 里。
        """
        return self.stream_cursor.load(stream_scope)

    def save_stream_cursor(self, stream_scope: str, position: int) -> None:
        """实现 ``Hooks.save_stream_cursor``：把某条消息流的位置写进 state。

        写失败只告警不上抛（理由见 :meth:`StreamCursorStore.save`）。
        """
        self.stream_cursor.save(stream_scope, position)

    # ------------------------------------------------------------------
    # prompt queue
    #   （队列本身在 :class:`~opencode_bridge.inbound_gateway.InboundGateway`）
    # ------------------------------------------------------------------
    def _flush_queue(self, conversation_id: str) -> None:
        """事件流那边 turn 收尾后把排队的消息放出去 —— 转发给入站那一侧。

        这个转发**不是**兼容层：:meth:`EventStream` 构造时需要它，而入站那一侧
        又需要事件流的 ``stream_confirmed``（见 :meth:`__init__` 里的构造顺序说明）。
        """
        self.inbound_gateway.flush_queue(conversation_id)

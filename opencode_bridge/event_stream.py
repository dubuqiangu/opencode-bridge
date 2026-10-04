"""opencode 事件流：订阅 SSE、归属过滤、把一轮的执行过程渲染回 IM。

这一块原先是 :class:`~opencode_bridge.core.BridgeCore` 的十七个私有方法。搬出来
是因为那个类已经远超 AGENTS.md §5.1 的阈值（方法数 ~15+ / 类自身代码行 ~250+），
而事件流是里面**自成一块**的一坨：它订阅 SSE、按会话归属过滤事件、把 delta 合并成
一条流式消息、维护每个活跃 turn 的进度句柄，并在结束时发布最终答复。

搬的时候**连私有状态一起搬**（§5.1 首选的那种形态）：``_handlers`` /
``_sid_conv`` / ``_tool_names`` / ``_unhandled_event_names`` /
``_last_unhandled_log_at`` / ``stream_confirmed`` 以及 :class:`Turn` 都是只有这一块
才写的东西。剩下的依赖由 :meth:`EventStream.__init__` 显式注入，其中两项是**共用的
状态**而不是协作者：``lock``（core 那边 12 个方法还在用同一把锁）与 ``turns``
（prompt 分发与 ``_drop_session`` 也要动这个 dict）。注入的是**同一个对象**，所以
互斥关系与 dict 身份一点没变。

事件名以 **anomalyco/opencode v2.0.22 源码** 为准，不是文档（``docs/server.mdx``
对 v2 已过时，仍列 v1 事件——那是推测的来源）。白名单见
``packages/schema/src/event-manifest.ts``。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from .adapters import Adapter
from .hooks import MsgHandle
from .normalize import _as_dict, _clean
from .opencode_client import OpenCodeClient
from .permission_ledger import PermissionLedger
from .state import StateStore

__all__ = ["EventStream", "Turn"]

logger = logging.getLogger("opencode_bridge.event_stream")


def _session_id(data: dict) -> str:
    for key in ("sessionID", "session_id", "sessionId"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


@dataclass
class Turn:
    """Streaming state for one execution of one session."""

    conversation_id: str
    progress_handle: MsgHandle | None = None
    #: 进度消息**当前实际显示**的内容 —— 只在写入**成功**之后才更新。
    #:
    #: 为什么非记不可：收尾时那条消息有可能**改不动**（平台能力 / 部署配置 /
    #: 个别客户端 / 网络）。改不动就意味着它的内容被**冻结**在这里这一刻，
    #: 而桥必须知道冻结的是哪一段，才能只补发"读者还没看到的那截"，而不是
    #: 把整条答复再发一遍（重复）或从错误的偏移补发（丢失）。
    #:
    #: ⚠️ 记录的是**写成功**的那一份，不是"打算写的那一份" —— ``edit_progress``
    #: 返回 ``False`` 时平台可能根本没换成功，那一刻显示的还是上一次的内容。
    #: 空串 = 还没有任何一次写入成功过（占位消息压根没发出去，或改写从未成功）。
    shown_progress_text: str = ""
    #: assistantMessageID -> {ordinal: delta} (deltas may arrive out of order)
    parts: dict[str, dict[int, str]] = field(default_factory=dict)
    last_edit_ts: float = 0.0
    tool_trace: list[str] = field(default_factory=list)
    agent: str = ""
    model: str = ""

    def assemble(self) -> str:
        chunks: list[str] = []
        for by_ordinal in self.parts.values():  # dict order == arrival order
            for ordinal in sorted(by_ordinal):
                chunks.append(by_ordinal[ordinal])
        return "".join(chunks)


# ----------------------------------------------------------------------
# 注入的协作者：签名写在这里，core 那边的实现是什么它们不关心
# ----------------------------------------------------------------------
#: 按 conversation_id 找出该回哪个适配器（找不到返回 ``None``）。
AdapterFor = Callable[[str], Adapter | None]

#: 发一条出站文本；关键字参数与 core 的发信入口一致，返回句柄或 ``None``。
SendText = Callable[..., MsgHandle | None]

#: 改写已发出的那条流式消息（节流由 core 那边做）。
EditProgress = Callable[..., bool]

#: 发布这条会话的最终答复。
Finalize = Callable[..., None]

#: turn 收尾后把排队的消息放出去。
FlushQueue = Callable[[str], None]


# ----------------------------------------------------------------------
# event stream
# ----------------------------------------------------------------------
class EventStream:
    """订阅 opencode 的 ``/api/event``，把一轮执行的过程与结果发回 IM。

    与 :class:`~opencode_bridge.core.BridgeCore` 之间只有 :meth:`__init__` 里那些
    注入的协作者 —— **没有** core 引用、也不读 core 的任何私有状态，所以这一整块
    能脱离 core 单独测（见 ``tests/test_event_stream.py``）。

    事件名以 **anomalyco/opencode v2.0.22 源码** 为准，不是文档
    （``docs/server.mdx`` 对 v2 已过时，仍列 v1 事件——那是推测的来源）。
    白名单见 ``packages/schema/src/event-manifest.ts``。
    """

    #: 高频、按会话归属过滤的事件类型。``/api/event`` 是**全服务器广播**，同机其它
    #: agent 会话（包括开发者自己正在跑的 opencode 会话）的 delta 会以每秒数百条
    #: 的量级推过来；不过滤会把日志和 CPU 全烧在无关数据上。
    #:
    #: **刻意只包含高频事件**：``permission.asked`` 与 ``session.execution.*`` 不在其中。
    #: 它们低频，且对它们而言"未知会话"是**有意义的信息** —— 放行让原有的
    #: warning 继续暴露真问题（比如竞态导致会话没登记上）。若一律过滤，
    #: 一次竞态就会让 agent 永远等不到审批，而日志里什么都看不到。
    _HIGH_VOLUME_SESSION_EVENTS = frozenset({
        "session.text.delta",
        "session.reasoning.delta",
        "session.step.started",
        "session.step.streamed",
        "session.step.ended",
        "session.tool.input.started",
        "session.tool.input.delta",
        "session.tool.input.ended",
        "session.tool.called",
        "session.tool.progress",
        "session.tool.success",
        "session.tool.failed",
    })

    #: **认识但故意不处理**的事件：完全静默，不记账、不打日志。
    #:
    #: 这些是协议的一部分、不是我们漏实现了什么。把它们当"未知事件"记账是错的
    #: ——实测 `session.reasoning.delta` 半分钟就有 2800+ 条（思考流不该上IM），
    #: 足以把真正的错误彻底淹掉。
    #:
    #: 判断标准：**IM 里该不该出现**。思考过程、shell 生命周期、工具入参/出参、
    #: 配额与用量统计，对聊天用户都没有意义，就不该走"未知事件"那套记账。
    _KNOWN_BUT_IGNORED_EVENTS = frozenset({
        # 思考流：不上 IM
        "session.reasoning.started",
        "session.reasoning.delta",
        "session.reasoning.ended",
        # 工具细节：只有简短的"正在做什么"提示才有意义，进出参全文没有
        "session.tool.input.delta",
        "session.tool.progress",
        "session.step.streamed",
        "session.synthetic",
        "session.instructions.updated",
        # 会话元信息
        "session.viewed",
        "session.usage.updated",
        "session.metadata.updated",
        "session.permissions",
        "session.renamed",
        "session.agent.selected",
        "session.model.selected",
        "session.moved",
        "session.inbox.enqueued",
        "session.inbox.delivered",
        "session.inbox.cancelled",
        "session.inbox.delivery.changed",
        "session.created",
        "session.deleted",
        "session.forked",
        # 注意：`session.retry.scheduled` **不在**这里 —— 它有处理器
        # （`_on_retry_scheduled`），放进本集合会让人误以为它被忽略。
        # 压缩（上下文自动压缩）：值得单独提示，但当前实现里没有对应 handler
        "session.compaction.started",
        "session.compaction.delta",
        "session.compaction.ended",
        "session.compaction.failed",
        "session.revert.staged",
        "session.revert.cleared",
        "session.revert.committed",
        "session.shell.started",
        "session.shell.ended",
        "session.skill.activated",
        # 连接与全局状态：与本桥无关
        "server.connected",
        "provider.updated",
        "model.updated",
        "agent.updated",
        "command.updated",
        "config.updated",
        "skill.updated",
        "plugin.updated",
        "reference.updated",
        "project.updated",
        "filesystem.changed",
        "credential.updated",
        "credential.switched",
        "integration.updated",
        "models-dev.refreshed",
        "websearch.updated",
        "worktree.updated",
        "worktree.resolved",
        "installation.updated",
        "installation.update-available",
        "vcs.branch.updated",
        "mcp.status.changed",
        "mcp.resources.changed",
        "location.shutdown",
    })

    #: 未知事件记账日志的最小间隔（秒）。多路事件交替出现时"同一个名字连续"
    #: 这个前提**不成立**，所以不能靠事件名判断该不该打；一律按时间节流，
    #: 保证噪音有硬上限。
    _UNHANDLED_LOG_INTERVAL_SECONDS = 60.0

    def __init__(
        self,
        *,
        client: OpenCodeClient,
        state: StateStore,
        lock: threading.RLock,
        turns: dict[str, Turn],
        clock: Callable[[], float],
        edit_interval: float,
        max_message_chars: int,
        adapter_for: AdapterFor,
        send_text: SendText,
        edit_progress: EditProgress,
        finalize: Finalize,
        flush_queue: FlushQueue,
        permission_ledger: PermissionLedger,
    ) -> None:
        """全部依赖由 core 注入，本类不自己去找。

        ``lock`` 与 ``turns`` 是**共用的状态**而不是协作者：core 的 prompt 分发与
        ``_drop_session`` 也要动同一把锁、同一个 dict（见模块 docstring）。注入的是
        同一个对象，所以互斥关系与 dict 身份都没变。

        ``clock`` / ``edit_interval`` / ``max_message_chars`` 是**构造时读一次**的
        配置值：它们来自 ``bridge`` 配置段，运行时不会被改写。

        ``permission_ledger`` 与命令侧、入站按钮侧**共用同一个对象** —— 权限请求
        的身份只有一份记录，否掉第二次回答才可能（见
        :mod:`opencode_bridge.permission_ledger`）。本类只在两个时刻写它：
        ``permission.asked``（这是唯一知道 request id 的时刻）与
        ``permission.replied``。
        """
        self._client = client
        self._state = state
        self._lock = lock
        self._turns = turns
        self.clock = clock
        self._edit_interval = edit_interval
        self._max_message_chars = max_message_chars
        self._adapter_for = adapter_for
        self._send_text = send_text
        self._edit_progress = edit_progress
        self._finalize = finalize
        self._flush_queue = flush_queue
        self._permission_ledger = permission_ledger

        #: Set once the event stream has delivered its first frame, i.e. the
        #: subscription is live. Startup recovery waits for it (bounded) before
        #: replaying anything — see ``BridgeCore._recover_inbox`` for why.
        self.stream_confirmed = threading.Event()

        #: session_id -> conversation_id (rebuilt from state per event)
        self._sid_conv: dict[str, str] = {}
        #: (session_id, tool call id) -> tool name (from tool.input.started)
        self._tool_names: dict[tuple[str, str], str] = {}

        self._handlers: dict[str, Callable[[dict], None]] = {
            "session.execution.started": self._on_execution_started,
            "session.execution.succeeded": self._on_turn_finished,
            "session.execution.interrupted": self._on_execution_interrupted,
            "session.execution.failed": self._on_execution_failed,
            "session.step.started": self._on_step_started,
            "session.text.delta": self._on_text_delta,
            "session.tool.input.started": self._on_tool_input_started,
            "session.tool.called": self._on_tool_event,
            "session.tool.success": self._on_tool_event,
            "session.tool.failed": self._on_tool_event,
            "session.retry.scheduled": self._on_retry_scheduled,
            "permission.asked": self._on_permission_asked,
            "permission.replied": self._on_permission_replied,
        }
        #: 事件名出现但没有 handler 时记进这里，便于 `dispatch` 打汇总日志，
        #: 免得刷屏（一个未知事件可能每秒来几百条）。有界，不无限增长。
        self._unhandled_event_names: dict[str, int] = {}
        #: 上一次打"未处理事件"日志的时刻，用于按时间节流汇总。
        self._last_unhandled_log_at: float = 0.0

    # ------------------------------------------------------------------
    # 订阅与分发
    # ------------------------------------------------------------------
    def run(self) -> None:
        """Consume ``/api/event`` until the stream ends (the SSE thread body)."""
        try:
            for event in self._client.subscribe():
                # A first frame means the subscription is live. Startup recovery
                # blocks on this before replaying (see :meth:`_recover_inbox`).
                self.stream_confirmed.set()
                try:
                    if not isinstance(event, dict):
                        continue
                    self.dispatch(event)
                except Exception:
                    logger.exception(
                        "event dispatch failed: %s",
                        event.get("type")
                        if isinstance(event, dict)
                        else repr(event),
                    )
        except Exception:
            logger.exception("event stream terminated")
        logger.debug("event loop exited")

    def dispatch(self, event: dict) -> None:
        """Route one event frame to its handler (unknown names are accounted)."""
        event_name = str(event.get("type") or "")
        handler = self._handlers.get(event_name)
        if handler is None:
            # 区分两种"没有 handler"，这是实测踩出来的教训：
            #
            # (A) **认识但故意不处理**（例如思考流 `session.reasoning.*` 不该上IM）
            #     -> 完全静默。它们是协议的一部分，不是我们漏实现了什么。
            # (B) **不认识**（可能是 opencode 升版后新增了我们没跟上的事件）
            #     -> 记账 + 打日志，但要**严格节流**。
            #
            # 曾经这里没区分，把 `session.reasoning.delta`（实测 30 分钟 2800+ 条）
            # 当成"未知事件"反复记账；又因为多路事件交替出现，"同一名字连续"的
            # 判断永远不成立，于是每次都打一行含 30+ 项的全量汇总 ——
            # **为了避免噪音淹掉真信号，结果自己制造了噪音**，把日志冲垮。
            if event_name not in self._KNOWN_BUT_IGNORED_EVENTS:
                self._note_unhandled_event(event_name)
            return
        data = _as_dict(event.get("data"))
        with self._lock:
            # Refresh the session -> conversation reverse map every round, and
            # **before** the ownership check below -- that check reads this very
            # map, so refreshing afterwards made it judge on the *previous* round.
            # Harmless while a turn exists for every live session, but after a
            # restart the in-memory turn table is empty and the session is known
            # only from ``state.json``: the first high-volume frame of that turn
            # (a ``session.text.delta``, say) was dropped as "someone else's".
            #
            # Cost is one in-memory dict copy per frame (``all_sessions`` holds
            # ``state._data`` under its own lock; no disk I/O).
            self._sid_conv = {
                sid: conv for conv, sid in self._state.all_sessions().items()
            }
        if not self._owns_session_event(event_name, data):
            # 已知事件名，但**属于别的会话** —— 这是正常情况，不是错误。
            #
            # `/api/event` 是**全服务器广播**：同机跑的其它 agent 会话（本项目里
            # 就包括开发者自己正在跑的 opencode 会话）的事件同样会推过来。
            # 实测曾刷出 `permission request for unknown session ses_effe80...`，
            # 那是我们自己的会话，与 Telegram 毫无关系。
            #
            # 所以这里与上面的"未知事件名"必须区别对待：
            #   未知**名字** = 可能是我们漏实现了什么 -> 记账 + 打日志
            #   已知名字但**别人的会话** = 正常 -> debug 级，不惊动人
            logger.debug(
                "忽略非本桥会话的事件 %s (session=%s)", event_name, _session_id(data)
            )
            return
        handler(data)

    def _note_unhandled_event(self, event_name: str) -> None:
        """记账一个"事件名认识但没有 handler"的事件，并**按时间节流**打日志。

        为什么必须按时间节流：曾以为"同一个名字会连续出现"，于是用
        "事件名变了没有"来决定要不要打汇总。**实测证明这个前提不成立**——
        事件是多路交替的（`reasoning.delta` 一边涨一边夹着几十种其它事件），
        于是每来一个新事件名就打一行含 30+ 项的全量汇总，日志被自己的
        "降噪机制"冲垮（实测半小时内刷出上千行）。

        所以改成：**首次见到打一行，之后每 60 秒最多打一行汇总**，
        噪音有了硬上限。汇总行只取计数最多的若干项，避免一行几百字符。
        """
        previous_count = self._unhandled_event_names.get(event_name, 0)
        self._unhandled_event_names[event_name] = previous_count + 1

        # 注入的时钟，与本类其它时间读法一致（流式节流用的就是它）。此前这里直接
        # 调模块级 ``time.monotonic()``，于是这个节流**没法用注入的时钟测** ——
        # 测试只能去手改 ``_last_unhandled_log_at`` 才能把窗口"等过去"。
        now = self.clock()
        if previous_count == 0:
            logger.info(
                "收到未处理事件 %r（已记账；若你正在等某条回复却没下文，先查这里。"
                "若是 opencode 升版新增的事件，需在 _handlers 里补处理器）",
                event_name,
            )
            self._last_unhandled_log_at = now
            return

        if (now - self._last_unhandled_log_at) < self._UNHANDLED_LOG_INTERVAL_SECONDS:
            return
        self._last_unhandled_log_at = now

        top = sorted(
            self._unhandled_event_names.items(), key=lambda item: -item[1]
        )[:10]
        logger.info(
            "未处理事件累计（%d 种，仅列前 10）：%s",
            len(self._unhandled_event_names),
            ", ".join("%s x%d" % (name, count) for name, count in top),
        )

    def _owns_session_event(self, event_name: str, data: dict) -> bool:
        """这个事件是否属于本桥关心的会话。

        判据是"**state.json 里登记过**，或**当前有活跃 turn**"。
        拿不到 sessionID 时返回 True，交给 handler 自行处理
        （`session.retry.scheduled` 没有 sessionID，它靠 assistantMessageID
        反查，反查不到会安静返回并记debug 日志）。
        """
        if event_name not in self._HIGH_VOLUME_SESSION_EVENTS:
            return True  # 低频生命周期事件：放行，让"未知会话"的警告有意义
        session_id = _session_id(data)
        if not session_id:
            return True
        with self._lock:
            return session_id in self._sid_conv or session_id in self._turns

    # ------------------------------------------------------------------
    # event handlers (each is called from the SSE thread)
    # ------------------------------------------------------------------
    def _conversation_for(self, session_id: str) -> str | None:
        with self._lock:
            conv = self._sid_conv.get(session_id)
            if conv:
                return conv
            turn = self._turns.get(session_id)
            return turn.conversation_id if turn else None

    def _on_execution_started(self, data: dict) -> None:
        session_id = _session_id(data)
        if not session_id:
            return
        with self._lock:
            conv = self._sid_conv.get(session_id)
            turn = self._turns.get(session_id)
            if turn is None:
                if conv is None:
                    return
                turn = Turn(conversation_id=conv)
                self._turns[session_id] = turn
            # reset the round; keep the progress message (reused)
            turn.parts.clear()
            turn.tool_trace.clear()
            turn.last_edit_ts = 0.0
            turn.agent = ""
            turn.model = ""
            self._tool_names = {
                key: name
                for key, name in self._tool_names.items()
                if key[0] != session_id
            }

    def _on_step_started(self, data: dict) -> None:
        session_id = _session_id(data)
        if not session_id:
            return
        agent = data.get("agent")
        model = data.get("model")
        if isinstance(model, dict):
            model = "/".join(
                str(model.get(key))
                for key in ("providerID", "id")
                if model.get(key)
            )
        with self._lock:
            turn = self._turns.get(session_id)
            if turn is None:
                return
            if isinstance(agent, str) and agent:
                turn.agent = agent
            if isinstance(model, str) and model:
                turn.model = model

    def _on_text_delta(self, data: dict) -> None:
        session_id = _session_id(data)
        assistant_id = data.get("assistantMessageID")
        delta = data.get("delta")
        if not session_id or not isinstance(assistant_id, str) or not assistant_id:
            return
        if not isinstance(delta, str) or not delta:
            return
        ordinal_raw = data.get("ordinal")
        ordinal = (
            ordinal_raw
            if isinstance(ordinal_raw, int) and not isinstance(ordinal_raw, bool)
            else 0
        )
        with self._lock:
            turn = self._turns.get(session_id)
            if turn is None:
                conv = self._sid_conv.get(session_id)
                if conv is None:
                    return  # unknown session -> silent drop
                turn = Turn(conversation_id=conv)
                self._turns[session_id] = turn
            bucket = turn.parts.setdefault(assistant_id, {})
            bucket[ordinal] = bucket.get(ordinal, "") + delta
            text = turn.assemble()
            handle = turn.progress_handle
            conversation_id = turn.conversation_id
            if len(text) > self._max_message_chars:
                # 超过上限就**不发也不改**这一轮，留给收尾时整段发出去（适配器
                # 自己会切分）。这就是"已经有句柄时"一直在用的那条策略 ——
                # 进度消息本来就只发得下上限那么多，再长它也会被后续片段取代。
                #
                # ⚠️ 这个判断此前带着 ``handle is not None``，于是**第一片**正文
                # 无论多长都会被原样发出去：IM 里于是出现一串很快会被取代的碎片
                # 消息 —— 正是 delta 合并要避免的那种刷屏（实测思考流半分钟 2800+
                # 条）。判据是"长度"，不该取决于句柄在不在。
                return
            now = self.clock()
            if (now - turn.last_edit_ts) < self._edit_interval:
                return  # throttled
            turn.last_edit_ts = now

        if handle is None:
            new_handle = self._send_text(
                conversation_id,
                text,
                kind="progress",
                adapter=self._adapter_for(conversation_id),
                session_id=session_id,
            )
            with self._lock:
                current = self._turns.get(session_id)
                if current is not None and current.progress_handle is None:
                    current.progress_handle = new_handle
                    # 发成功才记：失败时那条消息并不存在（见 shown_progress_text）
                    if new_handle is not None:
                        current.shown_progress_text = text
            return
        if self._edit_progress(conversation_id, handle, text, session_id):
            with self._lock:
                current = self._turns.get(session_id)
                if current is not None:
                    current.shown_progress_text = text

    def _on_tool_input_started(self, data: dict) -> None:
        session_id = _session_id(data)
        name = data.get("name")
        call_id = data.get("id") or data.get("toolCallID")
        if not session_id or not isinstance(name, str) or not name:
            return
        if call_id is None:
            return
        with self._lock:
            self._tool_names[(session_id, str(call_id))] = name

    def _on_tool_event(self, data: dict) -> None:
        session_id = _session_id(data)
        if not session_id:
            return
        call_id = data.get("id") or data.get("toolCallID")
        with self._lock:
            name = None
            if call_id is not None:
                name = self._tool_names.get((session_id, str(call_id)))
            if not isinstance(name, str) or not name:
                name = data.get("name")
            if not isinstance(name, str) or not name:
                name = str(call_id) if call_id else "tool"
            turn = self._turns.get(session_id)
            if turn is None:
                return
            turn.tool_trace.append(f"▶ {name}")

    def _session_for_assistant(self, assistant_id: str) -> str | None:
        """按 ``assistantMessageID`` 反查它属于哪个会话。

        为什么需要反查：v2.0.22 里 `session.retry.scheduled` 的 `data` 只有
        `assistantMessageID / attempt / at / error`，**没有 `sessionID`**，
        而其余 `session.*` 事件都有。不能靠会话 id 路由，就只能按
        assistantMessageID 在活跃 turn 里找。

        turn 数量是"当前并发对话数"，很小，线性扫足够；找不到就返回 None，
        交给调用方记账而不是静默丢弃。
        """
        if not assistant_id:
            return None
        with self._lock:
            for session_id, turn in self._turns.items():
                if assistant_id in turn.parts:
                    return session_id
        return None

    def _on_retry_scheduled(self, data: dict) -> None:
        """`session.retry.scheduled` —— 模型重试时给用户一个提示，别干等着。

        取代原先挂在 `session.status{type:"retry"}` 上的实现：那个事件在
        v2.0.22 **从不发布**，所以那段代码从来没跑过；而它想提供的
        「正在重试」反馈本身是有价值的，于是改挂到真实存在的事件上。
        """
        session_id = _session_id(data)
        if not session_id:
            session_id = self._session_for_assistant(
                str(data.get("assistantMessageID") or "")
            )
        if not session_id:
            logger.debug(
                "retry.scheduled 找不到对应会话 (assistantMessageID=%r)",
                data.get("assistantMessageID"),
            )
            return
        conversation_id = self._conversation_for(session_id)
        if not conversation_id:
            return
        attempt = data.get("attempt", "?")
        error = _as_dict(data.get("error"))
        reason = _clean(error.get("message") or error.get("type") or "")
        text = f"⏳ 重试中 (attempt {attempt})" + (f": {reason}" if reason else "")
        with self._lock:
            turn = self._turns.get(session_id)
            handle = turn.progress_handle if turn else None
        if handle is None:
            return
        self._edit_progress(conversation_id, handle, text, session_id)

    def _on_execution_interrupted(self, data: dict) -> None:
        """`session.execution.interrupted` —— 但 ``reason == "shutdown"`` **不算**结束。

        opencode 服务重启时会保留 claim 并**续跑**这一轮。源码依据
        （v2.0.22 `packages/core/src/session/projector.ts` 的 `projectIdle`）：
        `reason === "shutdown"` 时直接 return，不产生 idle 投影。

        若把它当结束处理，后果是双重的：
          1. 还没写完的 turn 被提前 finalize，把半截内容当最终答复发出去；
          2. turn 已从 `_turns` 弹掉，续跑后 `session.text.delta` 会**另建一个
             turn**，于是同一条回复被发两遍。
        多个第三方消费者（openchamber / waku）都专门为这条踩过坑。
        """
        reason = str(data.get("reason") or "")
        if reason == "shutdown":
            logger.info(
                "execution interrupted by shutdown (session=%s)：这一轮会被续跑，"
                "不当作结束",
                _session_id(data),
            )
            return
        self._finalize_session(_session_id(data))

    def _on_execution_failed(self, data: dict) -> None:
        session_id = _session_id(data)
        error = _as_dict(data.get("error"))
        error_type = error.get("type") or "error"
        error_message = error.get("message") or _clean(data.get("error") or "")
        conversation_id = self._conversation_for(session_id)
        with self._lock:
            turn = self._turns.pop(session_id, None)
        if conversation_id is None:
            logger.warning(
                "execution failed for unknown session %s: %s",
                session_id,
                error_message,
            )
            return
        # 失败**走成功那条发布路径**（:meth:`_finalize_session` 用的同一个
        # ``_finalize``）：把这一轮已经发出去的那条 ``⏳ 处理中…`` 改写成失败原因。
        # 之前这里另起一条 ``kind="error"`` 消息，于是用户看到"一个卡住的进度气泡
        # + 一行不相干的报错"—— 同一件事说了两遍，还留下永远不会被收掉的气泡。
        #
        # ⚠️ ``kind="error"`` 必须留着：``adapters/a2a.py`` 靠它把 A2A 任务判成
        # ``TASK_STATE_FAILED``（``tests/test_a2a.py`` 钉着这条），改成 "final"
        # 会把一次失败汇报成"完成"。
        self._finalize(
            conversation_id,
            turn.progress_handle if turn else None,
            f"任务失败 [{error_type}]: {error_message}",
            session_id,
            kind="error",
            # ⚠️ 失败文案与本轮正文**毫无关系**，所以它几乎永远不是
            # ``shown_progress_text`` 的延伸。``finalize`` 会自己校验这一点，
            # 校验不过就整段发出去 —— 绝不拿正文的前缀去给一条报错算偏移。
            shown_progress_text=turn.shown_progress_text if turn else "",
        )
        # a failed execution leaves the session idle -> release queued messages
        self._flush_queue(conversation_id)

    def _on_permission_asked(self, data: dict) -> None:
        session_id = _session_id(data)
        conversation_id = self._conversation_for(session_id)
        if not conversation_id:
            logger.warning(
                "permission request for unknown session %s", session_id
            )
            return
        request_id = str(data.get("id") or "")
        action = str(data.get("action") or "?")
        resources = data.get("resources")
        if isinstance(resources, (list, tuple)):
            resources_text = ", ".join(str(item) for item in resources)
        else:
            resources_text = str(resources or "-")
        message = _clean(data.get("message") or "")
        # ⚠️ 记账**必须在发信之前**：这是全流程里唯一同时握着 session id 与
        # request id 的时刻，错过就再也补不回来（那正是 C4 的根因 ——
        # 之前这里把 request_id 渲染成文字就扔了，之后只能靠用户手敲的字符串
        # 去猜，见 :mod:`opencode_bridge.permission_ledger`）。
        self._permission_ledger.record_asked(session_id, request_id)
        text = (
            "🔐 权限请求\n"
            f"动作: {action}\n"
            f"资源: {resources_text}\n"
            f"说明: {message}\n"
            f"回复: /approve {request_id}  或  "
            f"/approve {request_id} always  或  /deny {request_id}"
        )
        self._send_text(
            conversation_id, text, kind="text", session_id=session_id
        )

    def _on_permission_replied(self, data: dict) -> None:
        """``permission.replied`` —— 服务端说这个权限请求**已经结束**。

        这条事件以前在 :attr:`_KNOWN_BUT_IGNORED_EVENTS` 里被整个丢掉，也就是
        唯一一个"这个请求不会再被回答"的**权威**信号被扔了。现在它只做一件事：
        把账本记上，命令侧与按钮侧据此否掉迟到的第二次回答。

        **刻意不发任何东西到 IM**：一次应答通常就是用户刚按过按钮（他那边已经收到
        确认），或者 agent 自己走了另一条路 —— 这两种情况再发一条"某请求已结束"
        对用户只是噪音。真要有话说，命令/按钮那条路上"已回复过"的那句才是用户
        需要看到的，而它在**他真的又答了一次**的时候才出现。

        别的会话的一律不记：`/api/event` 是全服务器广播，把同机其它 agent 会话的
        ``permission.replied`` 记进来只会白占账本的槽位。
        """
        session_id = _session_id(data)
        request_id = str(data.get("id") or "")
        if not session_id or not request_id:
            return
        if not self._conversation_for(session_id):
            logger.debug(
                "permission.replied for a session this bridge does not serve "
                "(session=%s); not recorded",
                session_id,
            )
            return
        self._permission_ledger.note_replied(session_id, request_id)

    def _finalize_session(self, session_id: str) -> None:
        """Publish the turn's final message, then flush the queue.

        触发源只有两个（v2.0.22 源码核实）：
          - ``session.execution.succeeded``
          - ``session.execution.interrupted``，**且** ``reason != "shutdown"``
            （shutdown 会被续跑，见 :meth:`_on_execution_interrupted`）

        ⚠️ 曾经还把 ``session.idle`` 与 ``session.status{type:"idle"}`` 当触发源，
        但前者在源码里已标 ``// deprecated``、后者全代码库零处发布 ——
        于是真实环境**永远等不到收尾**，症状是用户只看到 `⏳ 处理中…`。
        写这段注释时全仓 1409 条测试都是绿的，而它们正是拿那两个不存在的事件
        当触发源：**测试在保护一个虚构的契约**。

        幂等：turn 在锁内 pop，所以重复触发只会多刷一次（通常为空的）队列。
        """
        if not session_id:
            return
        with self._lock:
            turn = self._turns.pop(session_id, None)
        conversation_id = self._conversation_for(session_id) or (
            turn.conversation_id if turn else None
        )
        if conversation_id is None:
            logger.debug("turn end for unknown session %s", session_id)
            return
        if turn is not None:
            final = _clean(turn.assemble())
            self._finalize(
                conversation_id, turn.progress_handle, final, session_id,
                shown_progress_text=turn.shown_progress_text,
            )
        self._flush_queue(conversation_id)

    def _on_turn_finished(self, data: dict) -> None:
        self._finalize_session(_session_id(data))

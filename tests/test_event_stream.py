"""``EventStream`` 的**独立**测试 —— 全程不构造 :class:`BridgeCore`。

这一整个文件存在的理由是 AGENTS.md §5.1 说的"抽出去"那种拆法：新类必须能脱离
原类单独测。所以这里没有 ``make_env``、没有适配器挂载、没有 ``BridgeCore``；
十二个协作者要么是真对象（``StateStore`` / ``Turn``），要么是本文件现造的替身。

因此这里能断言 core 层面**看不见**的东西：事件归属过滤的高频白名单、节流用的是
哪个时钟、``turns`` 与锁是不是**共用同一个对象**、未知事件的记账与节流 —— 那些全
是协作契约。
"""

from __future__ import annotations

import inspect
import os
import tempfile
import threading
import unittest

from opencode_bridge.core import BridgeCore
from opencode_bridge.event_stream import EventStream, Turn
from opencode_bridge.hooks import MsgHandle, Outbound
from opencode_bridge.opencode_client import OpenCodeError
from opencode_bridge.state import StateStore

CONVERSATION = "chat:55"
PLATFORM = "fake"
SESSION_ID = "ses_reported_by_the_state_store"


# ----------------------------------------------------------------------
# 替身
# ----------------------------------------------------------------------
class RecordingSendText:
    """core 那条发信路径的替身：签名一致，只记录，不经过任何适配器。"""

    def __init__(self, handle: MsgHandle | None = None) -> None:
        self.outgoing: list[Outbound] = []
        self._handles = 0
        self.handle_to_return = handle
        self.fail_with: Exception | None = None

    def __call__(
        self,
        conversation_id: str,
        text: str,
        *,
        kind: str = "text",
        adapter=None,
        session_id: str | None = None,
    ) -> MsgHandle | None:
        if self.fail_with is not None:
            raise self.fail_with
        self.outgoing.append(Outbound(
            conversation_id=conversation_id,
            text=text,
            kind=kind,
            session_id=session_id,
        ))
        self._handles += 1
        if self.handle_to_return is None:
            return MsgHandle(
                conversation_id=conversation_id,
                message_id="m%d" % self._handles,
                platform=PLATFORM,
            )
        return self.handle_to_return

    @property
    def last(self) -> Outbound:
        return self.outgoing[-1]


class RecordingClient:
    """``EventStream`` 只用到 ``subscribe``（``dispatch`` 由测试直接喂）。"""

    def __init__(self, events: list | None = None) -> None:
        self.events = list(events or [])
        self.subscribe_calls = 0

    def subscribe(self):
        self.subscribe_calls += 1
        return iter(self.events)


class RecordingState:
    """``EventStream`` 只用到 ``all_sessions``；盘上的读写在 ``StateStore``。"""

    def __init__(self, store: StateStore, sessions: dict) -> None:
        self.store = store
        self.sessions = dict(sessions)
        self.reads = 0

    def all_sessions(self) -> dict:
        self.reads += 1
        return dict(self.sessions)


class FlakyState(RecordingState):
    """``all_sessions`` 第一次抛异常，用来验证 SSE 线程不会被单个事件带走。"""

    def __init__(self, store: StateStore, sessions: dict) -> None:
        super().__init__(store, sessions)
        self.fail_next_read = True

    def all_sessions(self) -> dict:
        if self.fail_next_read:
            self.fail_next_read = False
            raise RuntimeError("state read boom")
        return super().all_sessions()


def ev(event_type: str, **data) -> dict:
    return {"type": event_type, "data": data}


def text_delta(session_id: str, text: str, ordinal: int = 0,
               assistant: str = "msg_1") -> dict:
    return ev("session.text.delta", sessionID=session_id,
              assistantMessageID=assistant, ordinal=ordinal, delta=text)


# ----------------------------------------------------------------------
# 基类：造一套 EventStream（**没有** BridgeCore）
# ----------------------------------------------------------------------
class EventStreamTestCase(unittest.TestCase):
    """每个用例一套全新的十二个协作者。"""

    edit_interval = 1.5
    max_message_chars = 4000

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.store = StateStore(os.path.join(self.tempdir.name, "state.json"))
        self.sessions = {CONVERSATION: SESSION_ID}
        self.state = RecordingState(self.store, self.sessions)
        self.client = RecordingClient()
        self.lock = threading.RLock()
        self.turns: dict[str, Turn] = {}
        self.ticks = [1000.0]
        self.send_text = RecordingSendText()
        self.edit_progress_calls: list[tuple] = []
        self.finalize_calls: list[tuple] = []
        self.flush_queue_calls: list[str] = []
        self.adapter_for_calls: list[str] = []
        self.stream = EventStream(
            client=self.client,
            state=self.state,
            lock=self.lock,
            turns=self.turns,
            clock=lambda: self.ticks[0],
            edit_interval=self.edit_interval,
            max_message_chars=self.max_message_chars,
            adapter_for=self._adapter_for,
            send_text=self.send_text,
            edit_progress=self._record_edit_progress,
            finalize=self._record_finalize,
            flush_queue=self._flush_queue,
        )

    # --- 五个协作者的可调用替身 -----------------------------------------
    def _adapter_for(self, conversation_id: str):
        self.adapter_for_calls.append(conversation_id)
        return object()

    def _record_edit_progress(self, conversation_id, handle, text,
                              session_id) -> bool:
        self.edit_progress_calls.append(
            (conversation_id, handle.message_id if handle else None, text,
             session_id)
        )
        return True

    def _record_finalize(self, conversation_id, handle, text, session_id,
                         *, kind="final") -> None:
        self.finalize_calls.append((
            conversation_id,
            handle.message_id if handle else None,
            text,
            session_id,
            kind,
        ))

    def _flush_queue(self, conversation_id: str) -> None:
        self.flush_queue_calls.append(conversation_id)

    # --- 驱动与断言的小帮手 ---------------------------------------------
    def rebuild(self, **overrides) -> EventStream:
        """Rebuild the stream with different constructor values.

        ``edit_interval`` / ``max_message_chars`` are read once at construction,
        so a test that needs another value has to build another stream.
        """
        kwargs = {
            "client": self.client,
            "state": self.state,
            "lock": self.lock,
            "turns": self.turns,
            "clock": lambda: self.ticks[0],
            "edit_interval": self.edit_interval,
            "max_message_chars": self.max_message_chars,
            "adapter_for": self._adapter_for,
            "send_text": self.send_text,
            "edit_progress": self._record_edit_progress,
            "finalize": self._record_finalize,
            "flush_queue": self._flush_queue,
        }
        kwargs.update(overrides)
        self.stream = EventStream(**kwargs)
        return self.stream

    def feed(self, event: dict) -> None:
        self.stream.dispatch(event)

    def start_turn(self, session_id: str = SESSION_ID) -> str:
        self.feed(ev("session.execution.started", sessionID=session_id))
        return session_id

    def last_text(self) -> str:
        return self.send_text.last.text


# ----------------------------------------------------------------------
# 1-2: 依赖面与共享状态（AGENTS.md §5.1 的"抽出去"能不能成立）
# ----------------------------------------------------------------------
class CollaboratorSurfaceTests(EventStreamTestCase):
    def test_the_constructor_takes_the_twelve_injected_dependencies(self):
        parameters = inspect.signature(EventStream.__init__).parameters
        self.assertEqual(
            [name for name in parameters if name != "self"],
            ["client", "state", "lock", "turns", "clock", "edit_interval",
             "max_message_chars", "adapter_for", "send_text", "edit_progress",
             "finalize", "flush_queue"],
        )
        for name, parameter in parameters.items():
            if name == "self":
                continue
            self.assertEqual(
                parameter.kind, inspect.Parameter.KEYWORD_ONLY,
                "%s 必须显式按名字注入" % name,
            )
            self.assertIs(
                parameter.default, inspect.Parameter.empty,
                "%s 不许有默认值 —— 少传一个就该在构造时炸掉" % name,
            )

    def test_no_bridge_core_is_reachable_from_the_stream(self):
        """拿到了 core 就等于又耦合回大类和它的私有状态 —— 那这次拆分就白做了。"""
        for name, value in vars(self.stream).items():
            self.assertNotIsInstance(value, BridgeCore, name)

    def test_the_lock_and_the_turn_table_are_the_injected_objects(self):
        """注入的是**同一个对象**，不是复制一份：否则互斥关系和 turn 身份都会变。"""
        self.assertIs(self.stream._lock, self.lock)
        self.assertIs(self.stream._turns, self.turns)

    def test_building_a_stream_never_touches_a_bridge_core(self):
        self.assertIsInstance(self.stream, EventStream)


# ----------------------------------------------------------------------
# 3: 分发与归属过滤
# ----------------------------------------------------------------------
class DispatchTests(EventStreamTestCase):
    def test_an_unknown_event_name_is_accounted_not_dropped_silently(self):
        self.feed(ev("session.brand.new.event"))

        self.assertEqual(self.stream._unhandled_event_names,
                         {"session.brand.new.event": 1})
        self.assertEqual(self.send_text.outgoing, [])

    def test_known_but_ignored_events_are_not_accounted(self):
        """思考流半分钟 2800+ 条：记账会淹掉真正的错误，所以必须完全静默。"""
        for event_type in ("session.reasoning.delta", "session.tool.progress",
                           "session.usage.updated"):
            with self.subTest(event_type=event_type):
                self.feed(ev(event_type, sessionID=SESSION_ID, delta="x"))

        self.assertEqual(self.stream._unhandled_event_names, {})
        self.assertEqual(self.send_text.outgoing, [])

    def test_the_reverse_session_map_is_refreshed_on_every_event(self):
        self.state.sessions["chat:99"] = "ses_other"
        self.feed(ev("session.execution.started", sessionID="ses_other"))

        self.assertEqual(self.state.reads, 1)
        self.assertEqual(self.stream._sid_conv,
                         {SESSION_ID: CONVERSATION, "ses_other": "chat:99"})

    def test_high_volume_events_from_another_session_are_dropped(self):
        self.feed(ev("session.execution.started", sessionID=SESSION_ID))
        before = len(self.send_text.outgoing)

        self.feed(text_delta("ses_ghost", "noise"))
        self.feed(ev("session.reasoning.delta", sessionID="ses_ghost",
                     delta="noise"))

        self.assertEqual(len(self.send_text.outgoing), before)
        self.assertEqual(self.edit_progress_calls, [])

    # --- Fix 2: 归属判断要用**当前**那份映射 ----------------------------
    def test_the_first_high_volume_frame_of_a_restored_session_is_not_dropped(self):
        """重启后内存里的 turn 表是空的，会话只登记在 ``state.json`` 里。

        归属判断读的就是那张反向表，而它此前是在判断**之后**才刷新的 —— 于是这轮
        的第一帧（``session.text.delta`` 属于高频事件）会被当成"别人的会话"丢掉，
        用户永远看不到这一轮。
        """
        self.assertEqual(self.turns, {}, "前提：还没有任何 turn")
        self.assertEqual(self.stream._sid_conv, {}, "前提：反向表还是空的")

        self.feed(text_delta(SESSION_ID, "first frame after restart"))

        self.assertIn(SESSION_ID, self.turns)
        self.assertEqual(self.send_text.outgoing[-1].text,
                         "first frame after restart")

    def test_the_ownership_filter_still_drops_a_session_we_never_registered(self):
        """修的是"顺序"，不是"放松"：没登记过的会话照样丢。"""
        self.start_turn()
        before = len(self.send_text.outgoing)

        self.feed(text_delta("ses_never_registered", "noise"))

        self.assertEqual(len(self.send_text.outgoing), before)
        self.assertNotIn("ses_never_registered", self.turns)

    def test_the_reverse_map_is_refreshed_even_for_dropped_frames(self):
        """刷新在判断之前，所以被丢掉的那一帧也已经刷新过了。"""
        self.feed(text_delta("ses_ghost", "noise"))

        self.assertEqual(self.stream._sid_conv, {SESSION_ID: CONVERSATION})

    def test_low_frequency_events_from_another_session_still_reach_the_handler(self):
        """低频事件放行：``permission.asked`` 的"未知会话"警告是有意义的信息。"""
        with self.assertLogs("opencode_bridge.event_stream",
                             level="WARNING") as logs:
            self.feed(ev("permission.asked", sessionID="ses_ghost", id="per_1"))

        self.assertTrue(any("unknown session" in line for line in logs.output))
        self.assertEqual(self.send_text.outgoing, [])

    def test_a_non_dict_frame_is_skipped_by_the_loop(self):
        """``run`` 有 ``isinstance`` 闸门：畸形帧不许把 SSE 线程带走。"""
        self.client.events = ["not-a-dict", 42, None]

        self.stream.run()

        self.assertEqual(self.client.subscribe_calls, 1)
        self.assertTrue(self.stream.stream_confirmed.is_set())
        self.assertEqual(self.turns, {})

    def test_a_non_dict_frame_is_ignored_by_run_not_by_dispatch(self):
        """``run`` 有 ``isinstance`` 闸门；``dispatch`` 只被喂 dict。"""
        self.stream.run()

        self.assertEqual(self.client.subscribe_calls, 1)

    def test_a_failing_handler_does_not_kill_the_loop(self):
        """单个事件炸了不许带走 SSE 线程 —— 后面的事件还得继续处理。"""
        self.state = FlakyState(self.store, self.sessions)
        self.stream = self.rebuild()
        self.client.events = [
            {"type": "session.execution.started", "data": {"sessionID": SESSION_ID}},
            {"type": "session.execution.started", "data": {"sessionID": SESSION_ID}},
        ]

        with self.assertLogs("opencode_bridge.event_stream", level="ERROR") as logs:
            self.stream.run()

        self.assertEqual(len(logs.output), 1)
        self.assertIn("event dispatch failed", logs.output[0])
        self.assertIn(SESSION_ID, self.turns)


# ----------------------------------------------------------------------
# 4-5: 未知事件的记账与按时间节流
# ----------------------------------------------------------------------
class UnhandledEventAccountingTests(EventStreamTestCase):
    def test_the_first_sighting_opens_the_throttle_window(self):
        self.feed(ev("session.brand.new.event"))

        self.assertGreater(self.stream._last_unhandled_log_at, 0.0)

    def test_repeats_inside_the_window_are_counted_but_not_logged(self):
        with self.assertLogs("opencode_bridge.event_stream",
                             level="INFO") as first:
            self.feed(ev("session.brand.new.event"))
        self.assertEqual(len(first.output), 1)

        with self.assertNoLogs("opencode_bridge.event_stream", level="INFO"):
            for _ in range(5):
                self.feed(ev("session.brand.new.event"))

        self.assertEqual(self.stream._unhandled_event_names,
                         {"session.brand.new.event": 6})

    def test_a_new_name_is_never_throttled(self):
        """新名字不受节流限制：opencode 升版新增了什么，一眼就要看见。"""
        self.feed(ev("session.brand.new.event"))

        with self.assertLogs("opencode_bridge.event_stream",
                             level="INFO") as logs:
            self.feed(ev("session.other.unknown.event"))

        self.assertEqual(len(logs.output), 1)

    def test_the_window_opens_again_after_the_interval(self):
        self.feed(ev("session.brand.new.event"))

        self.stream._last_unhandled_log_at -= (
            self.stream._UNHANDLED_LOG_INTERVAL_SECONDS + 1
        )
        with self.assertLogs("opencode_bridge.event_stream",
                             level="INFO") as logs:
            self.feed(ev("session.brand.new.event"))

        self.assertEqual(len(logs.output), 1)
        self.assertTrue(any("累计" in line for line in logs.output))

    # --- Fix 3: 这个节流用的是注入的时钟 ------------------------------
    def test_the_log_throttle_is_driven_by_the_injected_clock(self):
        """节流窗口必须能被注入的时钟推过去 —— 不然只能手改私有属性。"""
        with self.assertLogs("opencode_bridge.event_stream",
                             level="INFO") as first:
            self.feed(ev("session.brand.new.event"))
        self.assertEqual(len(first.output), 1)

        # 窗口内：只记账，不打日志
        with self.assertNoLogs("opencode_bridge.event_stream", level="INFO"):
            self.ticks[0] += 1.0
            self.feed(ev("session.brand.new.event"))

        # 把注入的时钟推过窗口 -> 允许再打一行汇总
        self.ticks[0] += self.stream._UNHANDLED_LOG_INTERVAL_SECONDS
        with self.assertLogs("opencode_bridge.event_stream",
                             level="INFO") as second:
            self.feed(ev("session.brand.new.event"))

        self.assertEqual(len(second.output), 1)
        self.assertTrue(any("累计" in line for line in second.output))
        self.assertEqual(self.stream._unhandled_event_names,
                         {"session.brand.new.event": 3})


# ----------------------------------------------------------------------
# 6-8: turn 生命周期
# ----------------------------------------------------------------------
class TurnLifecycleTests(EventStreamTestCase):
    def test_a_started_round_creates_a_turn_for_a_known_session(self):
        self.start_turn()

        turn = self.turns[SESSION_ID]
        self.assertEqual(turn.conversation_id, CONVERSATION)
        self.assertEqual(turn.parts, {})
        self.assertEqual(turn.tool_trace, [])
        self.assertEqual(turn.last_edit_ts, 0.0)

    def test_a_second_round_resets_the_turn_but_keeps_the_progress_message(self):
        self.start_turn()
        self.send_text.handle_to_return = self.send_text(
            CONVERSATION, "first", kind="progress", session_id=SESSION_ID
        )
        self.turns[SESSION_ID].progress_handle = self.send_text.handle_to_return
        self.turns[SESSION_ID].parts = {"msg_1": {0: "old"}}
        self.turns[SESSION_ID].agent = "builder"

        self.start_turn()

        turn = self.turns[SESSION_ID]
        self.assertEqual(turn.parts, {})
        self.assertEqual(turn.agent, "")
        self.assertIs(turn.progress_handle, self.send_text.handle_to_return)

    def test_an_event_without_a_session_id_creates_nothing(self):
        self.feed(ev("session.execution.started"))

        self.assertEqual(self.turns, {})

    def test_a_started_round_for_an_unknown_session_creates_nothing(self):
        self.feed(ev("session.execution.started", sessionID="ses_ghost"))

        self.assertEqual(self.turns, {})

    def test_step_started_records_agent_and_model(self):
        self.start_turn()

        self.feed(ev("session.step.started", sessionID=SESSION_ID,
                     agent="builder", model={"providerID": "opencode",
                                             "id": "gpt"}))

        turn = self.turns[SESSION_ID]
        self.assertEqual(turn.agent, "builder")
        self.assertEqual(turn.model, "opencode/gpt")

    def test_step_started_ignores_a_non_string_agent(self):
        self.start_turn()

        self.feed(ev("session.step.started", sessionID=SESSION_ID, agent=7,
                     model="plain"))

        self.assertEqual(self.turns[SESSION_ID].agent, "")
        self.assertEqual(self.turns[SESSION_ID].model, "plain")

    def test_a_new_round_prunes_the_tool_names_of_that_session_only(self):
        # 让 ses_other 通过归属过滤，它的工具名才会被记下来
        self.state.sessions["chat:99"] = "ses_other"
        self.start_turn()
        self.feed(ev("session.tool.input.started", sessionID=SESSION_ID,
                     name="bash", id="call_1"))
        self.feed(ev("session.tool.input.started", sessionID="ses_other",
                     name="read", id="call_2"))
        self.feed(ev("session.tool.input.started", sessionID="ses_other",
                     name="grep", id="call_3"))

        self.start_turn()

        self.assertEqual(sorted(self.stream._tool_names),
                         [("ses_other", "call_2"), ("ses_other", "call_3")])


# ----------------------------------------------------------------------
# 9-11: 文本合并、节流与长度上限
# ----------------------------------------------------------------------
class TextDeltaTests(EventStreamTestCase):
    def test_deltas_are_merged_into_one_streaming_message(self):
        self.start_turn()

        for ordinal, text in ((0, "Hel"), (1, "lo "), (2, "world")):
            self.ticks[0] += 10.0  # 走过节流窗口
            self.feed(text_delta(SESSION_ID, text, ordinal))

        # 第一片是**新发**一条进度消息，之后的片都改写它 —— 两边都要看到
        self.assertEqual(len(self.send_text.outgoing), 1)
        self.assertEqual(self.send_text.outgoing[0].text, "Hel")
        self.assertEqual(self.send_text.outgoing[0].kind, "progress")
        self.assertEqual(self.edit_progress_calls[-1][2], "Hello world")
        self.assertEqual(self.adapter_for_calls, [CONVERSATION])

    def test_out_of_order_ordinals_are_sorted(self):
        self.start_turn()
        for ordinal, text in ((2, "c"), (0, "a"), (1, "b")):
            self.ticks[0] += 10.0
            self.feed(text_delta(SESSION_ID, text, ordinal))

        self.assertEqual(self.edit_progress_calls[-1][2], "abc")

    def test_edits_are_throttled_by_the_injected_clock(self):
        self.start_turn()
        self.feed(text_delta(SESSION_ID, "A"))
        self.assertEqual(len(self.edit_progress_calls), 0)  # 首次是发，不是改

        self.feed(text_delta(SESSION_ID, "B"))
        self.assertEqual(self.edit_progress_calls, [])

        self.ticks[0] += 10.0
        self.feed(text_delta(SESSION_ID, "C"))

        self.assertEqual(len(self.edit_progress_calls), 1)
        self.assertEqual(self.edit_progress_calls[0][2], "ABC")

    def test_a_long_body_is_not_edited_but_still_accumulates(self):
        self.rebuild(max_message_chars=5)
        self.start_turn()

        self.feed(text_delta(SESSION_ID, "A"))
        self.feed(text_delta(SESSION_ID, "0123456789"))

        self.assertEqual(self.edit_progress_calls, [])
        self.assertEqual(self.turns[SESSION_ID].assemble(), "A0123456789")

    # --- Fix 4: 上限判据与"句柄在不在"无关 --------------------------
    def test_a_first_fragment_over_the_cap_publishes_no_progress_message(self):
        """第一片就已经超上限时，不发那条进度消息。

        上限策略是"**拒绝**这一轮、留给收尾整段发"（既不截断也不在这里切分）。
        此前这个判断只在已有句柄时生效，于是第一片无论多长都会被原样发出去 ——
        IM 里出现一串很快会被取代的碎片消息。
        """
        self.rebuild(max_message_chars=5)
        self.start_turn()

        self.feed(text_delta(SESSION_ID, "0123456789"))

        self.assertEqual(self.send_text.outgoing, [])
        self.assertEqual(self.edit_progress_calls, [])
        self.assertEqual(self.turns[SESSION_ID].assemble(), "0123456789")
        # 被拒的那一帧不消耗节流窗口
        self.assertEqual(self.turns[SESSION_ID].last_edit_ts, 0.0)

    def test_the_whole_answer_is_still_published_after_a_refused_first_fragment(self):
        """拒绝只是"进度消息不发"，答案本身不能丢：收尾时整段发出去。"""
        self.rebuild(max_message_chars=5)
        self.start_turn()
        self.feed(text_delta(SESSION_ID, "0123456789"))
        self.feed(text_delta(SESSION_ID, " tail", 1))

        self.feed(ev("session.execution.succeeded", sessionID=SESSION_ID))

        self.assertEqual(self.send_text.outgoing, [])
        self.assertEqual(self.finalize_calls[-1][1], None, "没有句柄可改")
        self.assertEqual(self.finalize_calls[-1][2], "0123456789 tail")

    def test_a_first_fragment_exactly_at_the_cap_is_published(self):
        """边界：等于上限要照发（判据是 ``>``）。"""
        self.rebuild(max_message_chars=5)
        self.start_turn()

        self.feed(text_delta(SESSION_ID, "12345"))

        self.assertEqual(self.send_text.outgoing[-1].text, "12345")

    def test_delta_shapes_that_carry_nothing_are_dropped(self):
        self.start_turn()
        before = len(self.send_text.outgoing)

        self.feed(ev("session.text.delta", sessionID=SESSION_ID, delta="x"))
        self.feed(ev("session.text.delta", sessionID=SESSION_ID,
                     assistantMessageID="", delta="x"))
        self.feed(ev("session.text.delta", sessionID=SESSION_ID,
                     assistantMessageID="m", delta=""))
        self.feed(ev("session.text.delta", sessionID=SESSION_ID,
                     assistantMessageID="m", delta=7))
        self.feed(ev("session.text.delta", assistantMessageID="m", delta="x"))

        self.assertEqual(len(self.send_text.outgoing), before)

    def test_a_delta_for_an_unknown_session_is_dropped_silently(self):
        self.feed(text_delta("ses_ghost", "noise"))

        self.assertEqual(self.turns, {})
        self.assertEqual(self.send_text.outgoing, [])


# ----------------------------------------------------------------------
# 12-13: 工具轨迹
# ----------------------------------------------------------------------
class ToolTraceTests(EventStreamTestCase):
    def test_a_tool_name_learned_from_input_started_is_used(self):
        self.start_turn()

        self.feed(ev("session.tool.input.started", sessionID=SESSION_ID,
                     name="bash", id="call_1"))
        self.feed(ev("session.tool.called", sessionID=SESSION_ID, id="call_1"))

        self.assertEqual(self.turns[SESSION_ID].tool_trace, ["▶ bash"])

    def test_tool_call_ids_are_matched_per_session(self):
        # 让 ses_other 通过归属过滤，它的工具名才会被记下来
        self.state.sessions["chat:99"] = "ses_other"
        self.start_turn()
        self.feed(ev("session.tool.input.started", sessionID="ses_other",
                     name="read", id="call_1"))

        self.feed(ev("session.tool.called", sessionID=SESSION_ID, id="call_1"))

        self.assertEqual(self.turns[SESSION_ID].tool_trace, ["▶ call_1"])

    def test_fallbacks_when_the_name_is_unknown(self):
        self.start_turn()

        self.feed(ev("session.tool.success", sessionID=SESSION_ID, id="c1"))
        self.feed(ev("session.tool.failed", sessionID=SESSION_ID,
                     name="inline"))
        self.feed(ev("session.tool.failed", sessionID=SESSION_ID))

        self.assertEqual(self.turns[SESSION_ID].tool_trace,
                         ["▶ c1", "▶ inline", "▶ tool"])

    def test_input_started_without_a_usable_name_or_id_is_ignored(self):
        self.start_turn()

        self.feed(ev("session.tool.input.started", sessionID=SESSION_ID,
                     name=""))
        self.feed(ev("session.tool.input.started", sessionID=SESSION_ID,
                     name="bash"))

        self.assertEqual(self.stream._tool_names, {})

    def test_a_tool_event_for_a_session_without_a_turn_is_dropped(self):
        self.feed(ev("session.tool.called", sessionID=SESSION_ID, name="bash"))

        self.assertEqual(self.turns, {})


# ----------------------------------------------------------------------
# 14-15: 重试提示与 shutdown 续跑
# ----------------------------------------------------------------------
class RetryAndInterruptTests(EventStreamTestCase):
    def test_retry_reaches_the_conversation_by_session_id(self):
        self.start_turn()
        handle = self.send_text(
            CONVERSATION, "progress", kind="progress", session_id=SESSION_ID
        )
        self.turns[SESSION_ID].progress_handle = handle

        self.feed(ev("session.retry.scheduled", sessionID=SESSION_ID,
                     attempt=2, error={"message": "429"}))

        self.assertEqual(self.edit_progress_calls[-1][2],
                         "⏳ 重试中 (attempt 2): 429")

    def test_retry_without_a_session_id_is_routed_by_assistant_message_id(self):
        """v2.0.22 的 ``session.retry.scheduled`` 没有 sessionID，只能反查。"""
        self.start_turn()
        self.feed(text_delta(SESSION_ID, "partial"))
        handle = self.send_text(
            CONVERSATION, "progress", kind="progress", session_id=SESSION_ID
        )
        self.turns[SESSION_ID].progress_handle = handle

        self.feed(ev("session.retry.scheduled", assistantMessageID="msg_1",
                     attempt=7))

        self.assertEqual(self.edit_progress_calls[-1][2], "⏳ 重试中 (attempt 7)")

    def test_retry_for_an_unroutable_assistant_message_does_nothing(self):
        self.start_turn()

        self.feed(ev("session.retry.scheduled",
                     assistantMessageID="msg_unknown", attempt=6))

        self.assertEqual(self.edit_progress_calls, [])

    def test_retry_without_a_progress_message_does_nothing(self):
        self.start_turn()

        self.feed(ev("session.retry.scheduled", sessionID=SESSION_ID,
                     attempt=2))

        self.assertEqual(self.edit_progress_calls, [])

    def test_a_shutdown_interrupt_keeps_the_turn_for_the_resume(self):
        """shutdown 只是续跑：提前收尾会把半截内容当最终答复发出去。"""
        self.start_turn()
        self.feed(text_delta(SESSION_ID, "half written"))

        self.feed(ev("session.execution.interrupted", sessionID=SESSION_ID,
                     reason="shutdown"))

        self.assertIn(SESSION_ID, self.turns)
        self.assertEqual(self.finalize_calls, [])
        self.assertEqual(self.flush_queue_calls, [])

    def test_any_other_interrupt_reason_finalises(self):
        self.start_turn()
        self.feed(text_delta(SESSION_ID, "cut short"))

        self.feed(ev("session.execution.interrupted", sessionID=SESSION_ID,
                     reason="user"))

        self.assertNotIn(SESSION_ID, self.turns)
        self.assertEqual(len(self.finalize_calls), 1)
        self.assertEqual(self.flush_queue_calls, [CONVERSATION])


# ----------------------------------------------------------------------
# 16-18: 失败、权限提示与收尾
# ----------------------------------------------------------------------
class FinalizeTests(EventStreamTestCase):
    def test_a_successful_execution_publishes_the_merged_text(self):
        self.start_turn()
        self.feed(text_delta(SESSION_ID, "final answer"))

        self.feed(ev("session.execution.succeeded", sessionID=SESSION_ID))

        self.assertEqual(self.finalize_calls[-1][2], "final answer")
        self.assertEqual(self.finalize_calls[-1][0], CONVERSATION)
        self.assertEqual(self.finalize_calls[-1][4], "final")
        self.assertEqual(self.flush_queue_calls, [CONVERSATION])
        self.assertNotIn(SESSION_ID, self.turns)

    def test_finishing_twice_is_idempotent(self):
        self.start_turn()
        self.feed(text_delta(SESSION_ID, "once"))

        self.feed(ev("session.execution.succeeded", sessionID=SESSION_ID))
        self.feed(ev("session.execution.succeeded", sessionID=SESSION_ID))

        self.assertEqual(len(self.finalize_calls), 1)

    def test_finishing_an_unknown_session_does_nothing(self):
        self.feed(ev("session.execution.succeeded", sessionID="ses_ghost"))

        self.assertEqual(self.finalize_calls, [])
        self.assertEqual(self.flush_queue_calls, [])

    def test_an_empty_session_id_does_nothing(self):
        self.feed(ev("session.execution.succeeded"))

        self.assertEqual(self.finalize_calls, [])

    def test_a_failed_execution_reports_the_error_and_releases_the_queue(self):
        self.start_turn()

        self.feed(ev("session.execution.failed", sessionID=SESSION_ID,
                     error={"type": "ProviderError", "message": "boom"}))

        self.assertEqual(self.finalize_calls[-1][2],
                         "任务失败 [ProviderError]: boom")
        self.assertEqual(self.finalize_calls[-1][4], "error")
        self.assertEqual(self.flush_queue_calls, [CONVERSATION])
        self.assertNotIn(SESSION_ID, self.turns)

    # --- Fix 1: 失败走收尾那条路，不再另起一条消息 ---------------------
    def test_a_failure_edits_the_progress_message_instead_of_sending_a_second_one(self):
        """用户只该看到**一条**消息：那条 ``⏳ 处理中…`` 被改写成失败原因。

        之前这里另发一条 ``kind="error"``，于是 IM 里留下一个永远不会被收掉的
        进度气泡 + 一行不相干的报错 —— 同一件事说了两遍。
        """
        self.start_turn()
        self.feed(text_delta(SESSION_ID, "half an answer"))

        self.feed(ev("session.execution.failed", sessionID=SESSION_ID,
                     error={"type": "ProviderError", "message": "boom"}))

        # publish 走 _finalize，带着这一轮那条进度消息的句柄
        self.assertEqual(len(self.finalize_calls), 1)
        conversation, handle, text, session_id, kind = self.finalize_calls[-1]
        self.assertEqual(conversation, CONVERSATION)
        self.assertEqual(handle, "m1", "必须是那条已经发出去的进度消息")
        self.assertEqual(text, "任务失败 [ProviderError]: boom")
        self.assertEqual(session_id, SESSION_ID)
        self.assertEqual(kind, "error")
        # 关键：没有第二条消息被发出去
        self.assertEqual(len(self.send_text.outgoing), 1)
        self.assertEqual(self.send_text.outgoing[0].kind, "progress")

    def test_a_failure_without_a_progress_message_still_publishes_one_message(self):
        """还没有进度消息时（第一片正文都没来）照发，只是没有句柄可改。"""
        self.start_turn()

        self.feed(ev("session.execution.failed", sessionID=SESSION_ID,
                     error={"type": "ProviderError", "message": "boom"}))

        self.assertEqual(self.finalize_calls[-1][1], None)
        self.assertEqual(self.finalize_calls[-1][2],
                         "任务失败 [ProviderError]: boom")

    def test_the_failure_keeps_the_error_kind_so_a2a_marks_the_task_failed(self):
        """``adapters/a2a.py`` 用 ``kind == "error"`` 判 ``TASK_STATE_FAILED``。

        失败若落到 ``_finalize`` 的默认 "final"，一次失败会被汇报成"完成"。
        """
        self.start_turn()
        self.feed(text_delta(SESSION_ID, "half"))

        self.feed(ev("session.execution.failed", sessionID=SESSION_ID,
                     error={"type": "RateLimited", "message": "gave up"}))

        self.assertEqual(self.finalize_calls[-1][4], "error")

    def test_a_failure_for_an_unknown_session_only_warns(self):
        self.feed(ev("session.execution.failed", sessionID="ses_ghost",
                     error={"message": "nobody"}))

        self.assertEqual(self.send_text.outgoing, [])
        self.assertEqual(self.flush_queue_calls, [])

    def test_a_permission_request_is_rendered_with_its_action_and_resources(self):
        self.start_turn()

        self.feed(ev("permission.asked", sessionID=SESSION_ID, id="per_1",
                     action="bash", resources=["/etc/passwd", "/tmp"],
                     message="run rm -rf?"))

        text = self.last_text()
        self.assertIn("🔐 权限请求", text)
        self.assertIn("动作: bash", text)
        self.assertIn("资源: /etc/passwd, /tmp", text)
        self.assertIn("说明: run rm -rf?", text)
        self.assertIn("/approve per_1 always", text)
        self.assertIn("/deny per_1", text)
        self.assertEqual(self.send_text.last.kind, "text")

    def test_a_permission_request_without_resources_shows_a_dash(self):
        self.start_turn()

        self.feed(ev("permission.asked", sessionID=SESSION_ID, id="per_2",
                     action="edit"))

        self.assertIn("资源: -", self.last_text())

    def test_a_permission_request_for_an_unknown_session_only_warns(self):
        self.feed(ev("permission.asked", sessionID="ses_ghost", id="per_3"))

        self.assertEqual(self.send_text.outgoing, [])

    def test_a_send_failure_is_left_to_the_injected_callback(self):
        """事件流自己不吞异常：发不出去由 core 那边的发信路径记警告。"""
        self.start_turn()
        self.send_text.fail_with = RuntimeError("no adapter")

        with self.assertRaises(RuntimeError):
            self.feed(ev("permission.asked", sessionID=SESSION_ID, id="per_4"))


# ----------------------------------------------------------------------
# 19: core 侧那一层装配（唯一的集成断言）
# ----------------------------------------------------------------------
class BridgeCoreWiringTests(unittest.TestCase):
    def test_core_builds_an_event_stream_and_still_routes_events(self):
        from tests.test_core import ev as core_ev, inbound, make_env

        with tempfile.TemporaryDirectory() as tempdir:
            core, client, adapter, _state, _path, _config = make_env(tempdir)
            self.assertIsInstance(core.event_stream, EventStream)

            core.on_inbound(inbound(CONVERSATION, "hello"))
            session_id = client.created_ids[0]

            core.event_stream.dispatch(
                core_ev("session.execution.started", sessionID=session_id)
            )
            core.event_stream.dispatch(
                core_ev("session.text.delta", sessionID=session_id,
                        assistantMessageID="msg_1", ordinal=0, delta="hi")
            )
            core.event_stream.dispatch(
                core_ev("session.execution.succeeded", sessionID=session_id)
            )

            finals = [out for _, out in adapter.edited if out.kind == "final"]
            self.assertEqual([out.text for out in finals], ["hi"])

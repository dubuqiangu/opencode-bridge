"""T3.1 Matrix adapter tests（无网络：HTTP 层替换 ``_request``）。"""

from __future__ import annotations

import logging
import time
import unittest
import urllib.parse
import uuid

# Keep expected-failure warnings out of the test output; assertLogs still
# works because it swaps in its own handler on the target logger.
logging.getLogger("opencode_bridge").addHandler(logging.NullHandler())

from opencode_bridge.adapters import build
from opencode_bridge.adapters.matrix import (
    MESSAGE_LIMIT,
    MatrixAdapter,
    _classify_matrix_error,
    _retry_after_seconds,
)
from opencode_bridge.hooks import Inbound, MsgHandle, Outbound, SendError

ROOM = "!abc:example.org"
ROOM_CID = "room:!abc:example.org"
BOT = "@bot:example.org"


class RecordingHooks:
    """Minimal ``Hooks`` implementation that records every call."""

    def __init__(self, events=None):
        self.inbounds: list[Inbound] = []
        self.callbacks: list[tuple] = []
        self.events: list[tuple] = events if events is not None else []

    def on_inbound(self, inbound: Inbound) -> None:
        self.inbounds.append(inbound)
        self.events.append(("inbound", inbound))

    def on_callback(self, conversation_id: str, data: str, query_id: str) -> None:
        self.callbacks.append((conversation_id, data, query_id))


def make_matrix(config: dict | None = None, hooks: RecordingHooks | None = None):
    cfg = {
        "homeserver": "https://matrix.example.org",
        "access_token": "syt_fake_token",
        "user_id": BOT,
    }
    if config:
        cfg.update(config)
    adapter = MatrixAdapter(cfg, hooks or RecordingHooks())
    adapter.min_interval = 0  # no artificial sleeps in tests
    adapter.backoff_interval = 0.01
    return adapter, adapter.hooks


# --- sync payload helpers ---------------------------------------------------

def text_event(text="hi", sender="@alice:example.org", event_id="$e1", **extra):
    ev = {
        "type": "m.room.message",
        "event_id": event_id,
        "sender": sender,
        "origin_server_ts": 1700000000000,
        "content": {"msgtype": "m.text", "body": text},
    }
    ev.update(extra)
    return ev


def sync_payload(events=None, next_batch="s1", room=ROOM, **room_extra):
    room_obj = {"timeline": {"events": list(events or [])}}
    room_obj.update(room_extra)
    return {"next_batch": next_batch, "rooms": {"join": {room: room_obj}}}


class TestMatrixCapabilities(unittest.TestCase):
    """能力声明（T1.1）：调用方据此判断，不再靠 try/except 猜。"""

    def test_capabilities_truthful(self):
        adapter, _ = make_matrix()
        caps = adapter.capabilities()
        self.assertEqual(caps["name"], "matrix")
        self.assertEqual(caps["label"], "Matrix")
        self.assertEqual(caps["max_message_length"], 4096)
        self.assertEqual(adapter.max_message_length, MESSAGE_LIMIT)
        self.assertTrue(caps["supports_inbound"])
        self.assertFalse(caps["supports_inline_buttons"])
        self.assertFalse(caps["supports_media"])
        self.assertEqual(caps["typed_command_prefix"], "/")
        self.assertEqual(adapter.typed_command_prefix, "/")

    def test_registered_in_build_registry(self):
        adapter = build(
            "matrix",
            {"homeserver": "https://x.org", "access_token": "t", "user_id": BOT},
            RecordingHooks(),
        )
        self.assertIsInstance(adapter, MatrixAdapter)
        self.assertEqual(adapter.capabilities()["max_message_length"], 4096)

    def test_homeserver_trailing_slash_stripped(self):
        adapter, _ = make_matrix({"homeserver": "https://matrix.example.org/"})
        self.assertEqual(adapter.homeserver, "https://matrix.example.org")

    def test_answer_is_noop(self):
        adapter, _ = make_matrix()
        self.assertIsNone(adapter.answer("q1"))
        self.assertIsNone(adapter.answer("q1", "text"))


class TestMatrixLifecycle(unittest.TestCase):
    """缺配置 → 告警并返回，不抛异常、不起线程。"""

    def test_missing_homeserver_warns_and_does_not_start(self):
        adapter, _ = make_matrix({"homeserver": ""})
        with self.assertLogs("opencode_bridge.adapters.matrix", level="WARNING") as cm:
            adapter.start()  # must not raise
        self.assertTrue(any("homeserver" in line for line in cm.output))
        self.assertIsNone(adapter._thread)
        self.assertFalse(adapter.running)

    def test_missing_access_token_warns_and_does_not_start(self):
        adapter, _ = make_matrix({"access_token": ""})
        with self.assertLogs("opencode_bridge.adapters.matrix", level="WARNING") as cm:
            adapter.start()
        self.assertTrue(any("access_token" in line for line in cm.output))
        self.assertIsNone(adapter._thread)
        self.assertFalse(adapter.running)

    def test_start_spawns_thread_and_stop_joins(self):
        adapter, _ = make_matrix()
        calls: list[str] = []

        def fake_request(method, path, payload=None, *, timeout=None):
            calls.append(path)
            return 200, sync_payload([], next_batch="s-loop")

        adapter._request = fake_request
        adapter.start()
        try:
            self.assertTrue(adapter.running)
            deadline = time.time() + 3
            while not calls and time.time() < deadline:
                time.sleep(0.01)
        finally:
            adapter.stop()
        self.assertTrue(calls, "起线程后应至少同步一次")
        self.assertFalse(adapter.running)

    def test_send_without_credentials_is_classified(self):
        adapter, _ = make_matrix({"access_token": ""})
        result = adapter.send_result(Outbound(ROOM_CID, "hi"))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, SendError.BAD_FORMAT)


class TestMatrixInbound(unittest.TestCase):
    """``/sync`` 解析、游标推进、过滤、授权。"""

    def _capture(self, adapter, responses):
        calls: list[tuple] = []

        def fake_request(method, path, payload=None, *, timeout=None):
            calls.append((method, path, payload))
            return responses[min(len(calls) - 1, len(responses) - 1)]

        adapter._request = fake_request
        return calls

    def test_text_message_becomes_inbound(self):
        adapter, hooks = make_matrix()
        adapter._request = lambda *a, **k: (
            200,
            sync_payload([text_event(text="你好 matrix")], next_batch="s1"),
        )
        self.assertTrue(adapter._sync_once())
        self.assertEqual(len(hooks.inbounds), 1)
        ib = hooks.inbounds[0]
        self.assertIsInstance(ib, Inbound)
        self.assertEqual(ib.conversation_id, ROOM_CID)
        self.assertEqual(ib.text, "你好 matrix")
        self.assertEqual(ib.kind, "text")
        self.assertEqual(ib.user_id, "@alice:example.org")
        self.assertEqual(ib.message_id, "$e1")
        self.assertEqual(ib.platform, "matrix")
        self.assertEqual(ib.raw["event_id"], "$e1")

    def test_sync_request_shape_uses_bearer_and_cursor(self):
        adapter, _ = make_matrix()
        calls = self._capture(adapter, [(200, sync_payload([], next_batch="s9"))])
        adapter._sync_once()
        method, path, payload = calls[0]
        self.assertEqual(method, "GET")
        self.assertIsNone(payload, "GET 不应带请求体")
        self.assertTrue(path.startswith("_matrix/client/v3/sync?"), path)
        query = urllib.parse.parse_qs(path.split("?", 1)[1])
        self.assertEqual(query["timeout"], ["30000"])
        self.assertNotIn("since", query, "首次同步不带 since")

    def test_next_batch_cursor_advances_across_calls(self):
        """Matrix 增量同步的核心：第二次请求必须带上第一次返回的 next_batch。"""
        adapter, _ = make_matrix()
        calls = self._capture(
            adapter,
            [
                (200, sync_payload([text_event()], next_batch="s100")),
                (200, sync_payload([], next_batch="s200")),
            ],
        )
        self.assertTrue(adapter._sync_once())
        self.assertEqual(adapter._since, "s100")
        self.assertTrue(adapter._sync_once())
        self.assertEqual(adapter._since, "s200")

        first = urllib.parse.parse_qs(calls[0][1].split("?", 1)[1])
        second = urllib.parse.parse_qs(calls[1][1].split("?", 1)[1])
        self.assertNotIn("since", first)
        self.assertEqual(second["since"], ["s100"], "第二次请求必须带上第一次的游标")

    def test_cursor_missing_keeps_previous_value(self):
        adapter, _ = make_matrix()
        adapter._since = "s5"
        adapter._request = lambda *a, **k: (200, {"rooms": {}})
        self.assertTrue(adapter._sync_once())
        self.assertEqual(adapter._since, "s5", "响应没有 next_batch 时不能丢游标")

    def test_filtered_events_are_ignored(self):
        adapter, hooks = make_matrix()
        adapter._request = lambda *a, **k: (
            200,
            sync_payload(
                [
                    text_event(text="我的回声", sender=BOT, event_id="$self"),
                    text_event(text="图片", event_id="$img",
                               content={"msgtype": "m.image", "body": "x", "url": "u"}),
                    {"type": "m.room.member", "event_id": "$member",
                     "sender": "@alice:example.org", "content": {}},
                    {"type": "m.reaction", "event_id": "$react",
                     "sender": "@alice:example.org",
                     "content": {"m.relates_to": {"rel_type": "m.annotation"}}},
                    text_event(text="", event_id="$empty"),
                    text_event(text="edited", event_id="$edit",
                               content={"msgtype": "m.text", "body": "edited",
                                        "m.relates_to": {"rel_type": "m.replace",
                                                         "event_id": "$old"}}),
                    text_event(text="reply", event_id="$reply",
                               content={"msgtype": "m.text", "body": "reply",
                                        "m.relates_to": {"rel_type": "m.thread"}}),
                    text_event(text="ack", event_id="$ack",
                               content={"msgtype": "m.notice", "body": "ack"}),
                    "not-a-dict",
                    text_event(text="真的来了", event_id="$keep"),
                ],
                next_batch="s1",
            ),
        )
        adapter._sync_once()
        self.assertEqual([ib.text for ib in hooks.inbounds], ["真的来了"])

    def test_authorization_gate_runs_before_inbound(self):
        """授权闸门在最前：白名单外的房间不许进入上层。"""
        hooks = RecordingHooks()
        adapter, _ = make_matrix({"allowed_chat_ids": ["!ok:example.org"]}, hooks)
        adapter._request = lambda *a, **k: (
            200,
            sync_payload([text_event(text="未授权", event_id="$no")], next_batch="s1"),
        )
        adapter._sync_once()
        self.assertEqual(hooks.inbounds, [])

        adapter._request = lambda *a, **k: (
            200,
            sync_payload(
                [text_event(text="授权", event_id="$yes")],
                next_batch="s2",
                room="!ok:example.org",
            ),
        )
        adapter._sync_once()
        self.assertEqual(len(hooks.inbounds), 1)
        self.assertEqual(hooks.inbounds[0].conversation_id, "room:!ok:example.org")

    def test_empty_allowlist_admits_all(self):
        adapter, hooks = make_matrix()
        self.assertTrue(adapter.admits(ROOM))
        adapter._request = lambda *a, **k: (
            200,
            sync_payload([text_event(text="open")], next_batch="s1"),
        )
        adapter._sync_once()
        self.assertEqual(len(hooks.inbounds), 1)

    def test_non_message_rooms_shapes_are_tolerated(self):
        adapter, hooks = make_matrix()
        adapter._request = lambda *a, **k: (
            200,
            {
                "next_batch": "s1",
                "rooms": {
                    "join": {
                        ROOM: {"state": {"events": [{"type": "x"}]}},
                        "!bad:example.org": "not-a-dict",
                    },
                    "invite": {ROOM: {}},
                    "leave": {ROOM: {}},
                },
            },
        )
        self.assertTrue(adapter._sync_once())  # must not raise
        self.assertEqual(hooks.inbounds, [])
        self.assertEqual(adapter._since, "s1")

    def test_sync_http_error_keeps_cursor(self):
        adapter, _ = make_matrix()
        adapter._since = "s7"
        adapter._request = lambda *a, **k: (
            429,
            {"errcode": "M_LIMIT", "error": "slow down", "retry_after_ms": 2000},
        )
        self.assertFalse(adapter._sync_once())
        self.assertEqual(adapter._since, "s7")

    def test_loop_survives_single_sync_exception(self):
        """单次异常绝不能逃出线程：退避后继续，线程仍活着。"""
        adapter, _ = make_matrix()
        adapter._since = ""
        attempts: list[str] = []

        def fake_request(method, path, payload=None, *, timeout=None):
            attempts.append(path)
            if len(attempts) == 1:
                raise RuntimeError("boom")
            return 200, sync_payload([], next_batch="s1")

        adapter._request = fake_request
        adapter.start()
        try:
            deadline = time.time() + 3
            while len(attempts) < 2 and time.time() < deadline:
                time.sleep(0.01)
            self.assertGreaterEqual(len(attempts), 2, "异常后应退避并重试")
            self.assertTrue(adapter.running, "异常不得让线程退出")
            self.assertEqual(adapter._since, "s1")
        finally:
            adapter.stop()

    def test_inbound_loop_never_raises(self):
        adapter, _ = make_matrix()

        def boom(*a, **k):
            raise ValueError("always fails")

        adapter._sync_once = boom
        adapter._stop_event.set()  # 进来就退出
        adapter._inbound_loop()  # must not raise

    def test_hook_exception_is_contained(self):
        class ExplodingHooks(RecordingHooks):
            def on_inbound(self, inbound):  # type: ignore[override]
                raise RuntimeError("hook down")

        adapter, _ = make_matrix(hooks=ExplodingHooks())
        adapter._request = lambda *a, **k: (
            200,
            sync_payload([text_event()], next_batch="s1"),
        )
        self.assertTrue(adapter._sync_once())


class TestMatrixSend(unittest.TestCase):
    """出站：URL / payload / txnId / 句柄 / 分片 / 结构化失败。"""

    def _capture(self, adapter, responder=None):
        calls: list[tuple] = []
        counter = {"n": 0}

        def fake_request(method, path, payload=None, *, timeout=None):
            calls.append((method, path, payload))
            counter["n"] += 1
            if responder is not None:
                return responder(method, path, payload, counter["n"])
            return 200, {"event_id": f"$evt{counter['n']}"}

        adapter._request = fake_request
        return calls

    def test_send_request_shape_and_handle(self):
        adapter, _ = make_matrix()
        calls = self._capture(adapter)
        handle = adapter.send(Outbound(ROOM_CID, "你好 matrix"))
        self.assertIsInstance(handle, MsgHandle)
        self.assertEqual(handle.platform, "matrix")
        self.assertEqual(handle.conversation_id, ROOM_CID)
        self.assertEqual(handle.message_id, "$evt1")

        method, path, payload = calls[0]
        self.assertEqual(method, "PUT")
        head, _, txn = path.rpartition("/")
        self.assertEqual(
            head,
            f"_matrix/client/v3/rooms/{ROOM}/send/m.room.message",
        )
        self.assertEqual(str(uuid.UUID(txn)), txn, "txnId 必须是合法 uuid")
        self.assertEqual(payload, {"msgtype": "m.text", "body": "你好 matrix"})

    def test_send_accepts_bare_room_id(self):
        adapter, _ = make_matrix()
        calls = self._capture(adapter)
        adapter.send(Outbound(ROOM, "bare"))
        self.assertIn(f"_matrix/client/v3/rooms/{ROOM}/send/m.room.message/", calls[0][1])

    def test_send_splits_long_text(self):
        adapter, _ = make_matrix()
        calls = self._capture(adapter)
        text = ("字" * 60 + "\n") * 80  # 4880 chars > 4096
        handle = adapter.send(Outbound(ROOM_CID, text))
        self.assertGreater(len(calls), 1)
        for _, _, payload in calls:
            self.assertLessEqual(len(payload["body"]), MESSAGE_LIMIT)
        self.assertEqual("".join(p["body"] for _, _, p in calls), text)
        # 每次 PUT 都要有独立 txnId（否则会被 Matrix 当成重试吞掉）
        txns = {c[1].rpartition("/")[2] for c in calls}
        self.assertEqual(len(txns), len(calls))
        self.assertEqual(handle.message_id, f"$evt{len(calls)}")

    def test_send_bad_conversation_id_classified(self):
        adapter, _ = make_matrix()
        result = adapter.send_result(Outbound("room:", "hi"))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, SendError.BAD_FORMAT)

    def test_send_empty_text_classified(self):
        adapter, _ = make_matrix()
        result = adapter.send_result(Outbound(ROOM_CID, ""))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, SendError.BAD_FORMAT)

    def test_send_http_4xx_classified(self):
        for status, want in ((403, SendError.FORBIDDEN), (404, SendError.NOT_FOUND),
                             (400, SendError.BAD_FORMAT), (413, SendError.TOO_LONG)):
            with self.subTest(status=status):
                adapter, _ = make_matrix()
                self._capture(
                    adapter,
                    lambda m, p, pl, n, s=status: (
                        s, {"errcode": "M_X", "error": "nope"}
                    ),
                )
                result = adapter.send_result(Outbound(ROOM_CID, "hi"))
                self.assertFalse(result.ok)
                self.assertEqual(result.error_kind, want)
                self.assertNotEqual(result.error_kind, SendError.UNKNOWN)
                self.assertEqual(result.error_detail, "nope")

    def test_send_http_5xx_classified_transient(self):
        adapter, _ = make_matrix()
        self._capture(adapter, lambda m, p, pl, n: (503, {"errcode": "M_UNKNOWN",
                                                           "error": "unavailable"}))
        result = adapter.send_result(Outbound(ROOM_CID, "hi"))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, SendError.TRANSIENT)

    def test_send_transport_error_classified(self):
        adapter, _ = make_matrix()

        def boom(*a, **k):
            return 0, {"errcode": "M_TRANSPORT", "error": "transport error: down"}

        adapter._request = boom
        result = adapter.send_result(Outbound(ROOM_CID, "hi"))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, SendError.TRANSIENT)
        self.assertNotEqual(result.error_kind, SendError.UNKNOWN)

    def test_send_rate_limit_reports_retry_after(self):
        adapter, _ = make_matrix()
        self._capture(
            adapter,
            lambda m, p, pl, n: (429, {"errcode": "M_LIMIT", "error": "slow down",
                                       "retry_after_ms": 2500}),
        )
        result = adapter.send_result(Outbound(ROOM_CID, "hi"))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, SendError.RATE_LIMITED)
        self.assertEqual(result.retry_after, 2.5)

    def test_send_partial_failure_is_marked_partial(self):
        """分片中途失败：前面成功的分片不能被记成全成功。"""
        adapter, _ = make_matrix()

        def responder(method, path, payload, n, *, timeout=None):
            if n == 1:
                return 200, {"event_id": "$evt1"}
            return 500, {"errcode": "M_UNKNOWN", "error": "boom"}

        self._capture(adapter, responder)
        result = adapter.send_result(Outbound(ROOM_CID, "x" * 9000))
        self.assertTrue(result.ok)
        self.assertTrue(result.partial)
        self.assertEqual(result.error_kind, SendError.TRANSIENT)
        self.assertEqual(result.handle.message_id, "$evt1")

    def test_send_success_result_is_ok(self):
        adapter, _ = make_matrix()
        self._capture(adapter)
        result = adapter.send_result(Outbound(ROOM_CID, "hi"))
        self.assertTrue(result.ok)
        self.assertFalse(result.partial)
        self.assertEqual(result.handle.message_id, "$evt1")


class TestMatrixEdit(unittest.TestCase):
    """编辑 = 兼容近似（``"* "`` 前缀 + ``m.new_content``），不是原生 API。"""

    def _capture(self, adapter):
        calls: list[tuple] = []
        counter = {"n": 0}

        def fake_request(method, path, payload=None, *, timeout=None):
            calls.append((method, path, payload))
            counter["n"] += 1
            return 200, {"event_id": f"$new{counter['n']}"}

        adapter._request = fake_request
        return calls

    def test_edit_uses_fallback_shape(self):
        adapter, _ = make_matrix()
        calls = self._capture(adapter)
        handle = MsgHandle(ROOM_CID, "$old", "matrix")
        result = adapter.edit(handle, Outbound(ROOM_CID, "改好了"))
        method, path, payload = calls[0]
        self.assertEqual(method, "PUT")
        self.assertIn("/send/m.room.message/", path)
        self.assertEqual(payload["msgtype"], "m.text")
        self.assertEqual(payload["body"], "* 改好了")
        self.assertEqual(
            payload["m.new_content"], {"msgtype": "m.text", "body": "改好了"}
        )
        self.assertEqual(
            payload["m.relates_to"],
            {"rel_type": "m.replace", "event_id": "$old"},
        )
        # 返回 True/False（基类契约）；core.py 只判真假
        self.assertTrue(result)

    def test_edit_failure_returns_false_and_classifies(self):
        adapter, _ = make_matrix()
        adapter._request = lambda *a, **k: (403, {"errcode": "M_FORBIDDEN",
                                                  "error": "no power"})
        handle = MsgHandle(ROOM_CID, "$old", "matrix")
        self.assertFalse(adapter.edit(handle, Outbound(ROOM_CID, "x")))
        self.assertEqual(adapter.last_send_error, SendError.FORBIDDEN)

    def test_edit_bad_handle_or_empty_text(self):
        adapter, _ = make_matrix()
        self.assertFalse(adapter.edit(MsgHandle("room:", "$old", "matrix"),
                                      Outbound("room:", "x")))
        self.assertFalse(adapter.edit(MsgHandle(ROOM_CID, "$old", "matrix"),
                                      Outbound(ROOM_CID, "")))

    def test_edit_over_limit_degrades_to_plain_send(self):
        """超限 → 退化为普通新消息（无 "* " 前缀、无 m.new_content）。"""
        adapter, _ = make_matrix()
        calls = self._capture(adapter)
        handle = MsgHandle(ROOM_CID, "$old", "matrix")
        text = "字" * (MESSAGE_LIMIT + 100)
        result = adapter.edit(handle, Outbound(ROOM_CID, text))
        self.assertGreater(len(calls), 1, "超限应走分片发送")
        for _, _, payload in calls:
            self.assertNotIn("m.new_content", payload)
            self.assertFalse(payload["body"].startswith("* "))
            self.assertLessEqual(len(payload["body"]), MESSAGE_LIMIT)
        self.assertEqual("".join(p["body"] for _, _, p in calls), text)
        self.assertTrue(result)

    def test_edit_returns_bool_matching_base_contract(self):
        """core.py 用 ``if adapter.edit(...)`` 判成败，基类契约是 ``-> bool``。"""
        adapter, _ = make_matrix()
        self._capture(adapter)
        handle = MsgHandle(ROOM_CID, "$old", "matrix")
        self.assertIs(adapter.edit(handle, Outbound(ROOM_CID, "v2")), True)


class TestMatrixErrorHelpers(unittest.TestCase):
    def test_classify_matrix_error_by_errcode(self):
        self.assertEqual(_classify_matrix_error(429, {"errcode": "M_LIMIT"}),
                         SendError.RATE_LIMITED)
        self.assertEqual(_classify_matrix_error(403, {"errcode": "M_FORBIDDEN"}),
                         SendError.FORBIDDEN)
        self.assertEqual(_classify_matrix_error(404, {"errcode": "M_NOT_FOUND"}),
                         SendError.NOT_FOUND)
        self.assertEqual(_classify_matrix_error(413, {"errcode": "M_TOO_LARGE"}),
                         SendError.TOO_LONG)
        self.assertEqual(_classify_matrix_error(400, {"errcode": "M_BAD_JSON"}),
                         SendError.BAD_FORMAT)
        # 未知 errcode → 回落到通用 HTTP 分类
        self.assertEqual(_classify_matrix_error(400, {"errcode": "M_WAT",
                                                     "error": "message is too long"}),
                         SendError.TOO_LONG)
        self.assertEqual(_classify_matrix_error(500, {"errcode": "M_UNKNOWN"}),
                         SendError.TRANSIENT)
        self.assertEqual(_classify_matrix_error(0, "transport error: down"),
                         SendError.TRANSIENT)
        self.assertEqual(_classify_matrix_error(200, "not a dict"),
                         SendError.UNKNOWN)

    def test_retry_after_seconds(self):
        self.assertEqual(_retry_after_seconds({"retry_after_ms": 2500}), 2.5)
        self.assertEqual(_retry_after_seconds({"retry_after_ms": 10 ** 9}), 60.0)
        self.assertIsNone(_retry_after_seconds({"retry_after_ms": -1}))
        self.assertIsNone(_retry_after_seconds({"retry_after_ms": "x"}))
        self.assertIsNone(_retry_after_seconds({}))
        self.assertIsNone(_retry_after_seconds("nope"))

    def test_conversation_id_round_trip(self):
        self.assertEqual(MatrixAdapter._conversation_id(ROOM), ROOM_CID)
        self.assertEqual(MatrixAdapter._room_id(ROOM_CID), ROOM)
        self.assertEqual(MatrixAdapter._room_id(ROOM), ROOM)
        self.assertIsNone(MatrixAdapter._room_id("room:"))
        self.assertIsNone(MatrixAdapter._room_id(""))

    def test_request_builds_bearer_url_without_network(self):
        """真实 ``_request`` 只在 stub 掉 urlopen 时被调用：验证 URL / 头 / 方法。"""
        import opencode_bridge.adapters.matrix as matrix_mod

        adapter, _ = make_matrix()
        seen: dict = {}

        class FakeResp:
            status = 200

            def read(self):
                return b'{"event_id": "$real"}'

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            seen["method"] = req.get_method()
            seen["headers"] = req.headers
            seen["body"] = req.data
            seen["timeout"] = timeout
            return FakeResp()

        old = matrix_mod.urllib.request.urlopen
        matrix_mod.urllib.request.urlopen = fake_urlopen
        try:
            status, data = adapter._request(
                "GET", adapter._sync_path(), None, timeout=1.0
            )
        finally:
            matrix_mod.urllib.request.urlopen = old

        self.assertEqual(status, 200)
        self.assertEqual(data, {"event_id": "$real"})
        self.assertTrue(seen["url"].startswith("https://matrix.example.org/_matrix/"))
        self.assertIn("timeout=30000", seen["url"])
        self.assertEqual(seen["method"], "GET")
        self.assertIsNone(seen["body"])
        self.assertEqual(seen["headers"]["Authorization"], "Bearer syt_fake_token")
        self.assertEqual(seen["timeout"], 1.0)

    def test_request_maps_http_error_and_non_json(self):
        import opencode_bridge.adapters.matrix as matrix_mod

        adapter, _ = make_matrix()
        old = matrix_mod.urllib.request.urlopen

        def http_error(req, timeout=None):
            raise matrix_mod.urllib.error.HTTPError(
                req.full_url, 429, "Too Many Requests", {}, None
            )

        matrix_mod.urllib.request.urlopen = http_error
        try:
            status, data = adapter._request("PUT", "_matrix/x", {"a": 1})
        finally:
            matrix_mod.urllib.request.urlopen = old
        self.assertEqual(status, 429)
        self.assertEqual(data["errcode"], "M_HTTP_429")
        self.assertEqual(_classify_matrix_error(status, data), SendError.RATE_LIMITED)

        def junk(req, timeout=None):
            class R:
                status = 200

                def read(self_inner):
                    return b"<html>nope</html>"

                def __enter__(self_inner):
                    return self_inner

                def __exit__(self_inner, *exc):
                    return False

            return R()

        matrix_mod.urllib.request.urlopen = junk
        try:
            status, data = adapter._request("PUT", "_matrix/x", {"a": 1})
        finally:
            matrix_mod.urllib.request.urlopen = old
        self.assertEqual(status, 200)
        self.assertEqual(data["errcode"], "M_NOT_JSON")

    def test_request_maps_transport_exception(self):
        import opencode_bridge.adapters.matrix as matrix_mod

        adapter, _ = make_matrix()

        def boom(*a, **k):
            raise OSError("connection reset")

        prev = matrix_mod.urllib.request.urlopen
        matrix_mod.urllib.request.urlopen = boom
        try:
            status, data = adapter._request("PUT", "_matrix/x", {"a": 1})
        finally:
            matrix_mod.urllib.request.urlopen = prev
        self.assertEqual(status, 0)
        self.assertEqual(data["errcode"], "M_TRANSPORT")
        self.assertEqual(_classify_matrix_error(status, data), SendError.TRANSIENT)


if __name__ == "__main__":
    unittest.main()

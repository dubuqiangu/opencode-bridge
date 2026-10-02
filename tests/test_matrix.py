"""T3.1 Matrix adapter tests（无网络：HTTP 层替换 ``_request``）。

A1 迁移（轮询循环收敛到 :mod:`opencode_bridge.transport.PollingTransport`）之后
新增 :class:`TestMatrixMigrationInvariants`：把「行为不变」逐条钉死 —— 退避接线值
（2s **恒定**，不是指数）、``reset_after=0``、stop 快且不泄漏线程、游标先推进再分发、
失败时游标不动、事件过滤 8 条一条不少；以及一条**防呆**用例把 ``room:`` 前缀钉死
（本轮刻意不切 ``matrix:``，切换的前置条件是 ``state.py`` 的键迁移）。
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
import unittest
import urllib.parse
import uuid

# Keep expected-failure warnings out of the test output; assertLogs still
# works because it swaps in its own handler on the target logger.
logging.getLogger("opencode_bridge").addHandler(logging.NullHandler())

from opencode_bridge.adapters import build
from opencode_bridge.adapters.matrix import (
    BACKOFF_INTERVAL,
    MESSAGE_LIMIT,
    ROOM_MSG_TYPE,
    SYNC_SOCKET_TIMEOUT,
    SYNC_TIMEOUT_MS,
    MatrixAdapter,
    _classify_matrix_error,
    _retry_after_seconds,
)
from opencode_bridge.hooks import Inbound, MsgHandle, Outbound, SendError
from opencode_bridge.identity import (
    LEGACY_PREFIXES,
    InvalidConversationId,
    is_valid,
    normalize,
    parse_id,
)
from opencode_bridge.state import StateStore

ROOM = "!abc:example.org"
ROOM_CID = "room:!abc:example.org"
BOT = "@bot:example.org"


def wait_until(predicate, timeout: float = 5.0, interval: float = 0.01) -> bool:
    """轮询等条件成立（比裸 sleep 稳；失败信息由调用方的断言给出）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


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
        self.assertIsNone(adapter.transport)   # 传输层压根没建 → 没有线程
        self.assertFalse(adapter.running)

    def test_missing_access_token_warns_and_does_not_start(self):
        adapter, _ = make_matrix({"access_token": ""})
        with self.assertLogs("opencode_bridge.adapters.matrix", level="WARNING") as cm:
            adapter.start()
        self.assertTrue(any("access_token" in line for line in cm.output))
        self.assertIsNone(adapter.transport)
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

    def test_consumer_loop_never_raises_even_if_fetch_always_throws(self):
        """迁移后没有 ``_inbound_loop`` 了；这里改成**更强**的断言。

        原来只是"预置停止位后调用循环方法不抛异常"。现在验证真实的消费线程：
        fetch 每轮都抛异常时，异常不逃出线程、线程不会静默死掉、而且会**持续**
        按退避重试（而不是重试一次就退出）。
        """
        adapter, _ = make_matrix()
        adapter._since = ""
        attempts: list[str] = []

        def boom(*a, **k):
            attempts.append("x")
            raise ValueError("always fails")

        adapter._request = boom
        adapter.start()
        try:
            self.assertTrue(
                wait_until(lambda: len(attempts) >= 3), "应持续按退避重试"
            )
            self.assertTrue(adapter.running, "线程不得静默退出")
            self.assertEqual(adapter._since, "", "全程失败 → 游标一字不动")
            self.assertGreaterEqual(adapter.transport.stats()["errors"], 1)
        finally:
            adapter.stop()

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


class TestMatrixMigrationInvariants(unittest.TestCase):
    """A1 迁移的「行为不变」清单：逐条钉死。

    退避/重置这类语义没法靠本机计时证明（迁移前就是 2s 这个量级），所以退避
    部分做成**白盒接线断言**（直接读传输层的退避状态机）：迁移最容易出错的
    恰恰是"接线值对不对"，而那正是这些用例要守的东西。
    """

    # ------------------------------------------------------------------
    # 前缀：本轮**刻意不切**
    # ------------------------------------------------------------------
    def test_conversation_id_still_uses_legacy_room_prefix(self):
        """显式防呆：有人在本轮偷偷把 ``room:`` 换成 ``matrix:`` 时这里会红。

        ``room`` 在 :data:`identity.LEGACY_PREFIXES` 里是**指向别的平台的旧别名**
        （映射到 ``matrix``），所以现在的 id **不是**合法的新格式 —— 这一点本身
        就是"前缀还没切"的证据。切换的前置条件是 ``state.py`` 的键迁移，见
        ``matrix.py`` 模块 docstring。
        """
        self.assertEqual(LEGACY_PREFIXES["room"], "matrix",
                         "room 是旧别名，映射到 matrix")
        self.assertEqual(MatrixAdapter._conversation_id(ROOM), ROOM_CID)
        for room in ("!abc:example.org", "!x.y:host:8443", "+plus:example.org"):
            with self.subTest(room=room):
                self.assertEqual(MatrixAdapter._conversation_id(room), f"room:{room}")
        # 仍是旧格式：parse_id 拒绝它，normalize 才知道怎么归一
        self.assertFalse(is_valid(ROOM_CID))
        with self.assertRaises(InvalidConversationId):
            parse_id(ROOM_CID)
        self.assertEqual(normalize(ROOM_CID, platform_hint="matrix"), f"matrix:{ROOM}")
        self.assertEqual(MatrixAdapter._room_id(ROOM_CID), ROOM)

    def test_switching_prefix_now_would_orphan_stored_sessions(self):
        """把"为什么现在不能切前缀"写成**可执行**的断言（只读地借用 StateStore）。

        ``conversation_id`` 是 :class:`StateStore` 的**不透明键**：切前缀等于把
        历史键全部作废，而且不报错、只表现为"agent 突然记错上下文"。
        真要切时必须先做键迁移 —— 那时这个用例会提醒你同步更新它。
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            cid = MatrixAdapter._conversation_id(ROOM)
            store = StateStore(path)
            store.set_session(cid, "sess-1")
            reopened = StateStore(path)          # 模拟进程重启
            self.assertEqual(reopened.get_session(cid), "sess-1",
                             "旧键在重启后必须仍能读回（这就是'已落盘'）")
            future = f"matrix:{ROOM}"
            self.assertNotEqual(future, cid)
            self.assertIsNone(
                reopened.get_session(future),
                "切前缀后同一个房间是另一个不透明键 → 会话映射直接丢失",
            )

    # ------------------------------------------------------------------
    # 退避接线：2s 恒定（不是指数）
    # ------------------------------------------------------------------
    def test_backoff_wiring_matches_legacy_constant(self):
        adapter, _ = make_matrix()
        adapter.backoff_interval = BACKOFF_INTERVAL      # 2.0（make_irc 风格默认值）
        transport = adapter._make_transport()
        self.assertEqual(transport.min_backoff, BACKOFF_INTERVAL)
        self.assertEqual(transport.max_backoff, BACKOFF_INTERVAL,
                         "max == min ⇒ 退避恒定；迁移前本来就没有指数退避")
        self.assertEqual(transport._idle_delay(), BACKOFF_INTERVAL,
                         "HTTP 失败（fetch 返回 NOTHING）后的重试间隔 = 2s")
        self.assertEqual(transport.reset_after, 0.0,
                         "必须 0：fetch 成功一次就重置退避（连上即重置）")

    def test_backoff_is_constant_not_exponential(self):
        adapter, _ = make_matrix()
        adapter.backoff_interval = BACKOFF_INTERVAL
        transport = adapter._make_transport()
        waits = [transport._next_backoff(survived=False) for _ in range(4)]
        self.assertEqual(waits, [2.0, 2.0, 2.0, 2.0],
                         "连续失败也必须恒定 2s（迁移前只有一个常数 backoff）")

    def test_failed_sync_is_retried_after_the_backoff_interval(self):
        """端到端：HTTP 失败后确实等了一个 backoff_interval 才重试。"""
        adapter, _ = make_matrix()
        adapter.backoff_interval = 0.1
        at: list[float] = []

        def failing(method, path, payload=None, *, timeout=None):
            at.append(time.monotonic())
            return 500, {"errcode": "M_UNKNOWN", "error": "boom"}

        adapter._request = failing
        adapter.start()
        try:
            self.assertTrue(wait_until(lambda: len(at) >= 3))
            gaps = [b - a for a, b in zip(at, at[1:])]
            for gap in gaps:
                self.assertGreaterEqual(gap, 0.09, f"失败后没退避：{gaps}")
        finally:
            adapter.stop()

    def test_successful_sync_is_not_delayed_by_the_backoff(self):
        """成功的一轮**不等** backoff（否则入站会被 2s 拖死）。"""
        adapter, _ = make_matrix()
        adapter.backoff_interval = 5.0                 # 故意设很大：一旦生效就会超时
        at: list[float] = []

        def ok(method, path, payload=None, *, timeout=None):
            at.append(time.monotonic())
            return 200, sync_payload([], next_batch="s1")

        adapter._request = ok
        adapter.start()
        try:
            self.assertTrue(wait_until(lambda: len(at) >= 5, timeout=3.0),
                            "成功轮次之间不应有 5s 的等待")
            gaps = [b - a for a, b in zip(at, at[1:])]
            self.assertLess(max(gaps), 1.0, f"成功轮次被退避拖住了：{gaps}")
        finally:
            adapter.stop()

    # ------------------------------------------------------------------
    # stop()：快（打断退避等待）+ 不泄漏线程
    # ------------------------------------------------------------------
    def test_stop_interrupts_the_backoff_wait(self):
        """失败轮正在 2s 退避等待里时 stop() 必须立刻返回。"""
        adapter, _ = make_matrix()
        adapter.backoff_interval = 5.0
        attempts: list[float] = []

        def failing(method, path, payload=None, *, timeout=None):
            attempts.append(time.monotonic())
            return 500, {"errcode": "M_UNKNOWN", "error": "boom"}

        adapter._request = failing
        adapter.start()
        self.addCleanup(adapter.stop)
        self.assertTrue(wait_until(lambda: len(attempts) >= 1))
        began = time.monotonic()
        adapter.stop()
        self.assertLess(time.monotonic() - began, 2.0,
                        "stop() 没打断退避等待（会白等满 backoff）")
        self.assertFalse(adapter.running)
        self.assertIsNone(adapter.transport)
        adapter.stop()                                 # 幂等
        self.assertFalse(adapter.running)

    def test_stop_during_inflight_sync_does_not_leak_the_thread(self):
        """已知限制（迁移前就有）：stop() 打不断在挂起的 ``/sync``。

        至少保证不会永久泄漏线程：这一轮返回后消费线程自然退出。
        """
        adapter, _ = make_matrix()
        started = threading.Event()

        def slow(method, path, payload=None, *, timeout=None):
            started.set()
            time.sleep(0.3)
            return 200, sync_payload([], next_batch="s1")

        adapter._request = slow
        adapter.start()
        self.addCleanup(adapter.stop)
        transport = adapter.transport
        self.assertTrue(started.wait(3), "没进到 /sync")
        adapter.stop()
        self.assertTrue(wait_until(lambda: not transport.running, timeout=5),
                        "在途请求返回后线程必须退出")

    # ------------------------------------------------------------------
    # 游标语义：先推进再分发 / 失败不动
    # ------------------------------------------------------------------
    def test_cursor_advances_before_dispatch_even_if_dispatch_explodes(self):
        """铁律：分发崩了游标也必须**已经**推进（否则整页事件被无限重放）。

        注意 ``_sync_once`` 让分发异常继续往上抛（迁移前也是这样：异常由循环体
        的 try/except 接住），所以这里断言的是**游标状态**，不是返回值。
        """
        adapter, _ = make_matrix()
        adapter._since = ""
        paths: list[str] = []

        def ok(method, path, payload=None, *, timeout=None):
            paths.append(path)
            return 200, sync_payload([text_event(text="hi", event_id="$e1")],
                                     next_batch="s42")

        adapter._request = ok

        def boom(data):
            raise RuntimeError("dispatch exploded")

        adapter._dispatch_sync = boom
        with self.assertRaises(RuntimeError):
            adapter._sync_once()
        self.assertEqual(adapter._since, "s42", "游标必须先推进（分发崩了也一样）")
        # 恢复后下一次请求必须带上 s42 ⇒ 服务器不会把那页事件再发一遍
        adapter._dispatch_sync = lambda data: None
        self.assertTrue(adapter._sync_once())
        query = urllib.parse.parse_qs(paths[-1].split("?", 1)[1])
        self.assertEqual(query["since"], ["s42"], "必须带上已推进的游标（否则重放）")

    def test_cursor_advance_survives_a_dispatch_blip_in_the_running_loop(self):
        """同一件事在真实消费线程上的版本：分发抛异常不许把线程带走。"""
        adapter, _ = make_matrix()
        adapter._since = ""
        paths: list[str] = []

        def ok(method, path, payload=None, *, timeout=None):
            paths.append(path)
            return 200, sync_payload([text_event(text="hi", event_id="$e1")],
                                     next_batch="s42")

        adapter._request = ok

        def boom(data):
            raise RuntimeError("dispatch exploded")

        adapter._dispatch_sync = boom
        adapter.start()
        try:
            self.assertTrue(wait_until(lambda: len(paths) >= 2, timeout=3.0),
                            "分发异常后仍应继续轮询")
            query = urllib.parse.parse_qs(paths[-1].split("?", 1)[1])
            self.assertEqual(query["since"], ["s42"],
                             "游标已推进 ⇒ 这页事件不会被重放")
            self.assertTrue(adapter.running, "分发异常不得让线程静默退出")
        finally:
            adapter.stop()

    def test_transport_level_failure_leaves_cursor_untouched(self):
        """fetch 抛异常（传输层故障）时游标也不能动。"""
        adapter, _ = make_matrix()
        adapter._since = "s7"

        def boom(*a, **k):
            raise RuntimeError("socket died")

        adapter._request = boom
        self.assertFalse(adapter._sync_once())
        self.assertEqual(adapter._since, "s7")

    def test_cursor_never_moves_on_http_failure_through_the_transport(self):
        """整条链路（传输层线程）上验证：连续失败时游标不动、线程仍重试。"""
        adapter, _ = make_matrix()
        adapter._since = "s7"
        calls: list[str] = []

        def failing(method, path, payload=None, *, timeout=None):
            calls.append(path)
            return 503, {"errcode": "M_UNKNOWN", "error": "unavailable"}

        adapter._request = failing
        adapter.start()
        self.addCleanup(adapter.stop)
        self.assertTrue(wait_until(lambda: len(calls) >= 3, timeout=5),
                        "HTTP 失败后必须继续轮询")
        self.assertEqual(adapter._since, "s7", "失败时游标一字不动")
        for path in calls:
            query = urllib.parse.parse_qs(path.split("?", 1)[1])
            self.assertEqual(query.get("since"), ["s7"], "失败时也不能换游标")
        self.assertTrue(adapter.running)

    # ------------------------------------------------------------------
    # 长轮询超时值
    # ------------------------------------------------------------------
    def test_long_poll_timeouts_are_unchanged(self):
        """``timeout=30000`` 与 socket 超时 35s 都不能被迁移改动。

        两者的大小关系是**功能约束**：socket 超时必须大于长轮询挂起时长，
        否则每一轮都会在拿到数据前被本地掐断。
        """
        self.assertEqual(SYNC_TIMEOUT_MS, 30000)
        self.assertEqual(SYNC_SOCKET_TIMEOUT, 35.0)
        self.assertGreater(
            SYNC_SOCKET_TIMEOUT, SYNC_TIMEOUT_MS / 1000.0,
            "socket 超时必须 > 长轮询挂起时长（35s > 30s）",
        )
        adapter, _ = make_matrix()
        seen: dict = {}

        def cap(method, path, payload=None, *, timeout=None):
            seen["timeout"] = timeout
            return 200, sync_payload([], next_batch="s1")

        adapter._request = cap
        adapter._request_sync()
        self.assertEqual(seen["timeout"], SYNC_SOCKET_TIMEOUT,
                         "轮询请求必须把 35s 的 socket 超时传下去")
        query = urllib.parse.parse_qs(adapter._sync_path().split("?", 1)[1])
        self.assertEqual(query["timeout"], ["30000"])

    # ------------------------------------------------------------------
    # 事件过滤：8 条一条不少
    # ------------------------------------------------------------------
    def test_all_eight_event_filters_still_apply(self):
        """一条事件对应一个过滤条件，全塞进同一页，只有一条该被投递。

        过滤条件（与迁移前逐字一致）：① 事件不是 dict ② ``type`` 不是
        ``m.room.message`` ③ ``content`` 不是 dict ④ ``msgtype`` 不是
        ``m.text`` ⑤ 自己发的回声 ⑥ 带 ``m.relates_to``（编辑/回复/表情）
        ⑦ ``body`` 非字符串或为空 ⑧ 房间不在白名单。
        """
        hooks = RecordingHooks()
        adapter, _ = make_matrix({"allowed_chat_ids": [ROOM]}, hooks)
        good = text_event(text="真消息", event_id="$keep")
        stranger = text_event(text="别的房间", event_id="$other")
        adapter._request = lambda *a, **k: (
            200,
            {
                "next_batch": "s1",
                "rooms": {
                    "join": {
                        ROOM: {
                            "timeline": {
                                "events": [
                                    "not-a-dict",                                   # ①
                                    {"type": "m.room.member", "event_id": "$m",      # ②
                                     "content": {"msgtype": "m.text", "body": "x"}},
                                    {"type": ROOM_MSG_TYPE, "event_id": "$c",       # ③
                                     "content": "not-a-dict"},
                                    text_event(text="图", event_id="$img",          # ④
                                               content={"msgtype": "m.image",
                                                        "body": "x", "url": "u"}),
                                    text_event(text="通知", event_id="$n",          # ④
                                               content={"msgtype": "m.notice",
                                                        "body": "n"}),
                                    text_event(text="回声", sender=BOT,            # ⑤
                                               event_id="$self"),
                                    text_event(text="编辑", event_id="$ed",          # ⑥
                                               content={"msgtype": "m.text", "body": "ed",
                                                        "m.relates_to": {
                                                            "rel_type": "m.replace",
                                                            "event_id": "$old"}}),
                                    text_event(text="回复", event_id="$rp",          # ⑥
                                               content={"msgtype": "m.text", "body": "rp",
                                                        "m.relates_to": {
                                                            "rel_type": "m.in_reply_to",
                                                            "event_id": "$old"}}),
                                    {"type": "m.reaction", "event_id": "$an",      # ⑥
                                     "sender": "@alice:example.org",
                                     "content": {"m.relates_to": {
                                         "rel_type": "m.annotation"}}},
                                    text_event(text="", event_id="$empty"),         # ⑦
                                    text_event(text=None, event_id="$nonstr",       # ⑦
                                               content={"msgtype": "m.text",
                                                        "body": 123}),
                                    good,
                                ]
                            }
                        },
                        "!stranger:example.org": {                                    # ⑧
                            "timeline": {"events": [stranger]}
                        },
                    }
                },
            },
        )
        self.assertTrue(adapter._sync_once())
        self.assertEqual([ib.text for ib in hooks.inbounds], ["真消息"])
        self.assertEqual(hooks.inbounds[0].conversation_id, ROOM_CID)
        self.assertEqual(hooks.inbounds[0].message_id, "$keep")


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

"""Lane B tests — adapters (no network; HTTP layer is monkeypatched)."""

from __future__ import annotations

import json
import logging
import time
import unittest

# Keep expected-failure warnings out of the test output; assertLogs still
# works because it swaps in its own handler on the target logger.
logging.getLogger("opencode_bridge").addHandler(logging.NullHandler())

from opencode_bridge.adapters import Adapter, AdapterError, build
from opencode_bridge.adapters.discord import DiscordAdapter
from opencode_bridge.adapters.slack import SlackAdapter
from opencode_bridge.adapters.telegram import TelegramAdapter
from opencode_bridge.hooks import Button, Inbound, MsgHandle, Outbound, SendError


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
        self.events.append(("callback", conversation_id, data, query_id))


class FakeWS:
    """最小假 WebSocket：按脚本逐条吐出文本，脚本空则返回 None 表示对端关闭。

    只实现 Slack 入站用到的三个方法（recv / send / close），用于验证协议层逻辑，
    不涉及真实 socket —— 真实 RFC 6455 收发由 ``tests/test_ws.py`` 用真服务器覆盖。
    """

    def __init__(self, script=None):
        self.script = list(script or [])
        self.sent: list[str] = []
        self.closed = False

    def recv(self):
        if self.script:
            return self.script.pop(0)
        return None

    def send(self, text: str) -> None:
        self.sent.append(text)

    def close(self) -> None:
        self.closed = True


def make_telegram(config: dict | None = None, hooks: RecordingHooks | None = None):
    cfg = {"bot_token": "123:FAKE"}
    if config:
        cfg.update(config)
    hooks = hooks or RecordingHooks()
    adapter = TelegramAdapter(cfg, hooks)
    adapter.min_interval = 0  # no artificial sleeps in tests
    return adapter, hooks


def message_update(update_id=5, chat_id=55, message_id=9, text="hello", **msg_extra):
    message = {
        "message_id": message_id,
        "chat": {"id": chat_id, "type": "private"},
        "from": {"id": 7, "is_bot": False, "first_name": "u"},
        "text": text,
    }
    message.update(msg_extra)
    return {"update_id": update_id, "message": message}


class TestTelegramPolling(unittest.TestCase):
    def test_offset_advances_to_update_id_plus_one(self):
        adapter, hooks = make_telegram()
        calls = []

        def fake_post(method, payload=None, *, timeout=None):
            payload = dict(payload or {})
            calls.append((method, payload))
            if method == "getUpdates" and payload.get("offset") == -1:
                return {"ok": True, "result": [{"update_id": 41}]}
            if method == "getUpdates":
                return {"ok": True, "result": [{"update_id": 100}]}
            return {"ok": True, "result": {}}

        adapter._post = fake_post
        adapter._flush_pending()
        self.assertEqual(adapter._offset, 42)  # 41 + 1

        adapter._poll_once()  # consumes update_id 100 -> offset 101
        self.assertEqual(adapter._offset, 101)

        adapter._poll_once()
        get_updates_calls = [c for c in calls if c[0] == "getUpdates"]
        # call#1 flush (offset -1), call#2 first poll (offset 42), call#3 (offset 101)
        self.assertEqual(get_updates_calls[1][1]["offset"], 42)
        self.assertEqual(get_updates_calls[2][1]["offset"], 101)
        # long-poll payload shape from the contract
        self.assertEqual(get_updates_calls[1][1]["allowed_updates"], ["message", "callback_query"])
        self.assertEqual(get_updates_calls[1][1]["limit"], 100)
        self.assertEqual(get_updates_calls[1][1]["timeout"], 25)

    def test_message_text_becomes_inbound(self):
        adapter, hooks = make_telegram()
        adapter._dispatch_update(message_update(text="你好 world"))
        self.assertEqual(len(hooks.inbounds), 1)
        inbound = hooks.inbounds[0]
        self.assertIsInstance(inbound, Inbound)
        self.assertEqual(inbound.kind, "text")
        self.assertEqual(inbound.conversation_id, "telegram:55")
        self.assertEqual(inbound.text, "你好 world")
        self.assertEqual(inbound.platform, "telegram")
        self.assertEqual(inbound.message_id, "9")
        self.assertEqual(inbound.user_id, "7")

    def test_callback_order_on_callback_before_answer(self):
        events: list[tuple] = []
        adapter, hooks = make_telegram(hooks=RecordingHooks(events=events))

        def fake_post(method, payload=None, *, timeout=None):
            events.append(("post", method, dict(payload or {})))
            return {"ok": True, "result": {}}

        adapter._post = fake_post
        update = {
            "update_id": 6,
            "callback_query": {
                "id": "QID1",
                "data": "act:ok",
                "from": {"id": 7},
                "message": {"message_id": 9, "chat": {"id": 55, "type": "private"}},
            },
        }
        adapter._dispatch_update(update)

        # Inbound(kind="callback", callback_query_id=...) delivered to core
        self.assertEqual(len(hooks.inbounds), 1)
        inbound = hooks.inbounds[0]
        self.assertEqual(inbound.kind, "callback")
        self.assertEqual(inbound.callback_query_id, "QID1")
        self.assertEqual(inbound.text, "act:ok")
        self.assertEqual(inbound.conversation_id, "telegram:55")
        # hooks.on_callback got (conversation_id, data, query_id)
        self.assertEqual(hooks.callbacks, [("telegram:55", "act:ok", "QID1")])
        # ordering: inbound -> on_callback -> answerCallbackQuery
        kinds = [e[0] for e in events]
        self.assertEqual(kinds, ["inbound", "callback", "post"])
        self.assertEqual(events[2][1], "answerCallbackQuery")
        self.assertEqual(events[2][2]["callback_query_id"], "QID1")

    def test_ignored_updates(self):
        adapter, hooks = make_telegram()
        cases = [
            # sticker / photo without text
            {"update_id": 1, "message": {"message_id": 1, "chat": {"id": 55}, "sticker": {}}},
            # photo with caption (captions are not processed per contract)
            {
                "update_id": 2,
                "message": {
                    "message_id": 2,
                    "chat": {"id": 55},
                    "photo": [{}],
                    "caption": "pic",
                },
            },
            # edited_message
            {"update_id": 3, "edited_message": {"message_id": 3, "chat": {"id": 55}, "text": "x"}},
            # channel_post
            {"update_id": 4, "channel_post": {"message_id": 4, "chat": {"id": 55}, "text": "x"}},
        ]
        for update in cases:
            adapter._dispatch_update(update)
        self.assertEqual(hooks.inbounds, [])

    def test_allowed_chat_ids_whitelist(self):
        adapter, hooks = make_telegram({"allowed_chat_ids": [999]})
        adapter._dispatch_update(message_update(chat_id=55))
        self.assertEqual(hooks.inbounds, [])  # rejected, no crash
        adapter._dispatch_update(message_update(chat_id=999))
        self.assertEqual(len(hooks.inbounds), 1)
        self.assertEqual(hooks.inbounds[0].conversation_id, "telegram:999")

        # string ids in the config compare equal to int chat ids
        adapter2, hooks2 = make_telegram({"allowed_chat_ids": ["888"]})
        adapter2._dispatch_update(message_update(chat_id=888))
        self.assertEqual(len(hooks2.inbounds), 1)
        # callbacks are filtered too
        adapter2._dispatch_update(
            {
                "update_id": 9,
                "callback_query": {
                    "id": "Q2",
                    "data": "d",
                    "from": {"id": 1},
                    "message": {"message_id": 1, "chat": {"id": 55}},
                },
            }
        )
        self.assertEqual(hooks2.callbacks, [])


class TestTelegramSend(unittest.TestCase):
    def test_send_returns_handle_and_splits_long_text(self):
        adapter, hooks = make_telegram()
        sent = []

        def fake_post(method, payload=None, *, timeout=None):
            payload = dict(payload or {})
            if method == "sendMessage":
                sent.append(payload)
                return {
                    "ok": True,
                    "result": {"message_id": 1000 + len(sent)},
                }
            return {"ok": True, "result": {}}

        adapter._post = fake_post
        text = ("字" * 60 + "\n") * 80  # 4880 chars
        self.assertGreater(len(text), 4000)
        out = Outbound(conversation_id="chat:55", text=text)
        handle = adapter.send(out)

        self.assertIsInstance(handle, MsgHandle)
        self.assertEqual(handle.platform, "telegram")
        self.assertEqual(handle.conversation_id, "chat:55")
        self.assertGreater(len(sent), 1)  # split into multiple messages
        for payload in sent:
            self.assertLessEqual(len(payload["text"]), 4096)
            self.assertEqual(payload["chat_id"], 55)
            self.assertIs(payload["disable_web_page_preview"], True)
        self.assertEqual("".join(p["text"] for p in sent), text)
        # only the LAST chunk's handle is returned
        self.assertEqual(handle.message_id, str(1000 + len(sent)))

    def test_send_failure_returns_none(self):
        adapter, hooks = make_telegram()
        adapter._post = lambda method, payload=None, *, timeout=None: {
            "ok": False,
            "error_code": 400,
            "description": "Bad Request: chat not found",
        }
        self.assertIsNone(adapter.send(Outbound("chat:55", "hi")))

    def test_edit_not_modified_returns_false(self):
        adapter, hooks = make_telegram()
        adapter._post = lambda method, payload=None, *, timeout=None: {
            "ok": False,
            "error_code": 400,
            "description": "Bad Request: message is not modified",
        }
        handle = MsgHandle(conversation_id="chat:55", message_id="10", platform="telegram")
        out = Outbound(conversation_id="chat:55", text="same", buttons=(Button("B", "d"),))
        self.assertIs(adapter.edit(handle, out), False)

    def test_edit_other_4xx_returns_false(self):
        adapter, hooks = make_telegram()
        adapter._post = lambda method, payload=None, *, timeout=None: {
            "ok": False,
            "error_code": 404,
            "description": "Bad Request: message to edit not found",
        }
        handle = MsgHandle(conversation_id="chat:55", message_id="10", platform="telegram")
        self.assertIs(adapter.edit(handle, Outbound("chat:55", "x")), False)

    def test_edit_success_and_keyboard(self):
        adapter, hooks = make_telegram()
        captured = []

        def fake_post(method, payload=None, *, timeout=None):
            captured.append((method, dict(payload or {})))
            return {"ok": True, "result": {}}

        adapter._post = fake_post
        handle = MsgHandle(conversation_id="chat:55", message_id="10", platform="telegram")
        out = Outbound(
            conversation_id="chat:55",
            text="pick",
            buttons=(Button("Yes", "y"), Button("No", "n")),
        )
        self.assertIs(adapter.edit(handle, out), True)
        method, payload = captured[-1]
        self.assertEqual(method, "editMessageText")
        self.assertEqual(payload["chat_id"], 55)
        self.assertEqual(payload["message_id"], 10)
        self.assertEqual(payload["text"], "pick")
        self.assertEqual(
            payload["inline_keyboard"],
            [[{"text": "Yes", "callback_data": "y"}], [{"text": "No", "callback_data": "n"}]],
        )


class TestTelegramLifecycle(unittest.TestCase):
    def test_start_with_missing_token_warns_and_returns(self):
        adapter, hooks = make_telegram({"bot_token": ""})
        with self.assertLogs("opencode_bridge.adapters.telegram", level="WARNING") as cm:
            adapter.start()
        self.assertTrue(any("bot_token" in line for line in cm.output))
        self.assertIsNone(adapter._thread)

    def test_start_with_fake_token_401_warns_and_returns(self):
        adapter, hooks = make_telegram()
        adapter._post = lambda method, payload=None, *, timeout=None: {
            "ok": False,
            "error_code": 401,
            "description": "Unauthorized",
        }
        with self.assertLogs("opencode_bridge.adapters.telegram", level="WARNING") as cm:
            adapter.start()  # must not raise
        self.assertIsNone(adapter._thread)
        self.assertFalse(adapter.running)
        self.assertTrue(any("getMe" in line for line in cm.output))

    def test_start_poll_stop_thread(self):
        adapter, hooks = make_telegram()

        def fake_post(method, payload=None, *, timeout=None):
            payload = dict(payload or {})
            if method == "getMe":
                return {"ok": True, "result": {"id": 1, "username": "bot"}}
            if method == "getUpdates" and payload.get("offset") == -1:
                return {"ok": True, "result": [{"update_id": 7}]}
            if method == "getUpdates":
                return {"ok": True, "result": []}
            return {"ok": True, "result": {}}

        adapter._post = fake_post
        adapter.start()
        try:
            self.assertTrue(adapter.running)
            self.assertEqual(adapter._offset, 8)
            time.sleep(0.2)
        finally:
            adapter.stop()
        self.assertFalse(adapter.running)


class TestSlackAdapter(unittest.TestCase):
    def make(self, config=None):
        cfg = {"bot_token": "xoxb-fake"}
        if config:
            cfg.update(config)
        hooks = RecordingHooks()
        adapter = SlackAdapter(cfg, hooks)
        adapter.min_interval = 0
        return adapter, hooks

    def test_send_and_edit(self):
        adapter, hooks = self.make()
        calls = []

        def fake_request(method, path, payload, *, timeout=None):
            calls.append((method, path, dict(payload)))
            if path == "chat.postMessage":
                return 200, {"ok": True, "ts": "1700.1"}
            return 200, {"ok": True, "ts": payload.get("ts")}

        adapter._request = fake_request
        out = Outbound(conversation_id="channel:C123", text="你好 slack")
        handle = adapter.send(out)
        self.assertIsInstance(handle, MsgHandle)
        self.assertEqual(handle.platform, "slack")
        self.assertEqual(handle.conversation_id, "channel:C123")
        self.assertEqual(handle.message_id, "1700.1")
        method, path, payload = calls[-1]
        self.assertEqual(path, "chat.postMessage")
        self.assertEqual(payload["channel"], "C123")
        self.assertEqual(payload["text"], "你好 slack")

        ok = adapter.edit(handle, Outbound("channel:C123", "edited"))
        self.assertIs(ok, True)
        self.assertEqual(calls[-1][1], "chat.update")
        self.assertEqual(calls[-1][2]["ts"], "1700.1")

    def test_start_without_token_warns(self):
        adapter, hooks = self.make({"bot_token": ""})
        with self.assertLogs("opencode_bridge.adapters.slack", level="WARNING"):
            adapter.start()

    def test_start_with_token_logs_todo_and_returns(self):
        adapter, hooks = self.make()
        with self.assertLogs("opencode_bridge.adapters.slack", level="WARNING") as cm:
            adapter.start()  # must not raise / spawn anything fatal
        self.assertIsNone(adapter._thread)


class TestDiscordAdapter(unittest.TestCase):
    def make(self, config=None):
        cfg = {"bot_token": "fake-token"}
        if config:
            cfg.update(config)
        hooks = RecordingHooks()
        adapter = DiscordAdapter(cfg, hooks)
        adapter.min_interval = 0
        return adapter, hooks

    def test_send_and_edit(self):
        adapter, hooks = self.make()
        calls = []

        def fake_request(method, path, payload, *, timeout=None):
            calls.append((method, path, dict(payload)))
            if method == "POST":
                return 200, {"id": "555"}
            return 200, {"id": "555"}

        adapter._request = fake_request
        out = Outbound(conversation_id="channel:424242", text="hi discord")
        handle = adapter.send(out)
        self.assertIsInstance(handle, MsgHandle)
        self.assertEqual(handle.platform, "discord")
        self.assertEqual(handle.message_id, "555")
        self.assertEqual(calls[-1][0], "POST")
        self.assertEqual(calls[-1][1], "channels/424242/messages")
        self.assertEqual(calls[-1][2]["content"], "hi discord")

        self.assertIs(adapter.edit(handle, Outbound("channel:424242", "v2")), True)
        self.assertEqual(calls[-1][0], "PATCH")
        self.assertEqual(calls[-1][1], "channels/424242/messages/555")

    def test_send_splits_at_discord_limit(self):
        adapter, hooks = self.make()
        sent = []

        def fake_request(method, path, payload, *, timeout=None):
            if method == "POST":
                sent.append(payload["content"])
            return 200, {"id": str(len(sent))}

        adapter._request = fake_request
        text = ("x" * 100 + "\n") * 30  # 3030 chars > 2000
        handle = adapter.send(Outbound("channel:1", text))
        self.assertGreater(len(sent), 1)
        for content in sent:
            self.assertLessEqual(len(content), 2000)
        self.assertEqual(handle.message_id, str(len(sent)))

    def test_start_without_token_warns(self):
        adapter, hooks = self.make({"bot_token": ""})
        with self.assertLogs("opencode_bridge.adapters.discord", level="WARNING"):
            adapter.start()


class TestSlackInbound(unittest.TestCase):
    """T2.1 — Slack Socket Mode 入站：先 ack 再过滤、授权闸门在最前、断线重连。"""

    @staticmethod
    def _envelope(env_id="e1", event=None, etype="events_api"):
        env = {"envelope_id": env_id, "type": etype}
        if event is not None:
            env["payload"] = {"event": event}
        return json.dumps(env)

    @staticmethod
    def _msg(text="hi", channel="C1", user="U1", ts="111.222", **extra):
        ev = {"type": "message", "text": text, "channel": channel, "user": user, "ts": ts}
        ev.update(extra)
        return ev

    def _adapter(self, hooks, **cfg):
        base = {"bot_token": "xoxb-t", "app_token": "xapp-t"}
        base.update(cfg)
        adapter = build("slack", base, hooks)
        adapter.min_interval = 0
        return adapter

    def test_message_event_becomes_inbound(self):
        hooks = RecordingHooks()
        adapter = self._adapter(hooks)
        ws = FakeWS()
        adapter._handle_envelope(ws, self._envelope(event=self._msg()))
        self.assertEqual(len(hooks.inbounds), 1)
        ib = hooks.inbounds[0]
        self.assertEqual(ib.conversation_id, "channel:C1")
        self.assertEqual(ib.text, "hi")
        self.assertEqual(ib.user_id, "U1")
        self.assertEqual(ib.message_id, "111.222")
        self.assertEqual(ib.platform, "slack")

    def test_every_envelope_is_acked_before_filtering(self):
        """漏 ack 会让 Slack 无限重发；连 hello / 无关事件也必须 ack。"""
        hooks = RecordingHooks()
        adapter = self._adapter(hooks)
        ws = FakeWS()
        adapter._handle_envelope(ws, self._envelope("e-hello", etype="hello"))
        adapter._handle_envelope(ws, self._envelope("e-noise", event={"type": "reaction_added"}))
        self.assertEqual(len(ws.sent), 2, ws.sent)
        self.assertEqual(json.loads(ws.sent[0]), {"envelope_id": "e-hello"})
        self.assertEqual(json.loads(ws.sent[1]), {"envelope_id": "e-noise"})
        self.assertEqual(hooks.inbounds, [])

    def test_authorization_gate_runs_before_inbound(self):
        """未授权频道必须被丢弃 —— 授权在入站最前，命令/审批字不能绕过。"""
        hooks = RecordingHooks()
        adapter = self._adapter(hooks, allowed_chat_ids=["C_allow"])
        ws = FakeWS()
        adapter._handle_envelope(ws, self._envelope(event=self._msg(channel="C_other")))
        self.assertEqual(hooks.inbounds, [])
        self.assertEqual(len(ws.sent), 1, "被丢弃的 envelope 仍需 ack")

        adapter._handle_envelope(ws, self._envelope("e2", event=self._msg(channel="C_allow")))
        self.assertEqual(len(hooks.inbounds), 1)

    def test_empty_allowlist_admits_all(self):
        hooks = RecordingHooks()
        adapter = self._adapter(hooks)  # 未设白名单 = 全开（v1 语义）
        ws = FakeWS()
        adapter._handle_envelope(ws, self._envelope(event=self._msg(channel="C_any")))
        self.assertEqual(len(hooks.inbounds), 1)

    def test_bot_echo_and_subtype_ignored(self):
        hooks = RecordingHooks()
        adapter = self._adapter(hooks)
        ws = FakeWS()
        adapter._handle_envelope(ws, self._envelope(event=self._msg(bot_id="B1")))
        adapter._handle_envelope(ws, self._envelope("e2", event=self._msg(subtype="message_changed")))
        self.assertEqual(hooks.inbounds, [])

    def test_malformed_and_empty_payloads_ignored(self):
        hooks = RecordingHooks()
        adapter = self._adapter(hooks)
        ws = FakeWS()
        adapter._handle_envelope(ws, "not json{")
        adapter._handle_envelope(ws, "[]")
        adapter._handle_envelope(ws, self._envelope(event=self._msg(text="")))
        adapter._handle_envelope(ws, self._envelope(event=self._msg(channel="")))
        self.assertEqual(hooks.inbounds, [])

    def test_capability_now_declares_inbound(self):
        adapter = self._adapter(RecordingHooks())
        self.assertTrue(adapter.supports_inbound)
        self.assertIn("app_token", str(adapter.config))

    def test_start_without_app_token_is_outbound_only(self):
        hooks = RecordingHooks()
        adapter = build("slack", {"bot_token": "xoxb-t"}, hooks)
        adapter.start()
        self.assertFalse(adapter.running, "缺 app_token 时不应起入站线程")

    def test_handle_envelope_returns_true_for_ordinary_traffic(self):
        hooks = RecordingHooks()
        adapter = self._adapter(hooks)
        ws = FakeWS()
        self.assertTrue(adapter._handle_envelope(ws, self._envelope("e1", etype="hello")))
        self.assertTrue(
            adapter._handle_envelope(ws, self._envelope("e2", event=self._msg()))
        )
        self.assertTrue(adapter._handle_envelope(ws, "not json{"))

    def test_disconnect_envelope_requests_reconnect(self):
        """WSS URL 约 1 小时过期，Slack 会发 disconnect —— 必须据此主动重连，
        而不是被动等对端关 socket。"""
        hooks = RecordingHooks()
        adapter = self._adapter(hooks)
        ws = FakeWS()
        for reason in ("warning", "refresh_requested"):
            env = json.dumps(
                {"type": "disconnect", "reason": reason, "envelope_id": "d-" + reason}
            )
            self.assertFalse(
                adapter._handle_envelope(ws, env), f"reason={reason} 应要求重连"
            )

    def test_disconnect_drops_old_connection_and_reconnects(self):
        """端到端：disconnect 之后旧连接上的消息必须丢弃（否则等于静默丢消息），
        新连接上的消息照常处理，且确实重新取了 URL。"""
        import opencode_bridge.adapters.slack as slack_mod

        old_delay = slack_mod.RECONNECT_DELAY
        slack_mod.RECONNECT_DELAY = 0.01
        try:
            hooks = RecordingHooks()
            adapter = self._adapter(hooks)
            sockets = [
                # 旧连接：先收到 disconnect，其后的消息属于已失效的连接
                FakeWS([self._envelope("d1", etype="disconnect"),
                        self._envelope("e1", event=self._msg(channel="C_OLD"))]),
                FakeWS([self._envelope("e2", event=self._msg(channel="C_NEW",
                                                                text="重连后收到"))]),
            ]
            created: list[FakeWS] = []
            urls: list[str] = []

            def factory(url):
                s = sockets[len(created)]
                created.append(s)
                return s

            def open_url():
                urls.append("wss://example/ws")
                if len(urls) > 3:
                    raise RuntimeError("测试到此为止")
                return "wss://example/ws"

            adapter._ws_factory = factory
            adapter._open_socket_url = open_url
            adapter.start()
            deadline = time.time() + 3
            while not hooks.inbounds and time.time() < deadline:
                time.sleep(0.01)
            adapter.stop()

            self.assertEqual(len(hooks.inbounds), 1, "只应处理重连后那条")
            self.assertEqual(hooks.inbounds[0].conversation_id, "channel:C_NEW")
            self.assertGreaterEqual(len(urls), 2, "disconnect 后应重新取 URL 重连")
        finally:
            slack_mod.RECONNECT_DELAY = old_delay

    def test_loop_reconnects_and_stop_closes_socket(self):
        import opencode_bridge.adapters.slack as slack_mod

        old_delay = slack_mod.RECONNECT_DELAY
        slack_mod.RECONNECT_DELAY = 0.01  # 别让测试等 3s
        try:
            hooks = RecordingHooks()
            adapter = self._adapter(hooks)
            sockets: list[FakeWS] = []

            def factory(url):
                s = FakeWS()          # 立刻返回 None → 触发重连
                sockets.append(s)
                return s

            adapter._ws_factory = factory
            adapter._open_socket_url = lambda: "wss://example/ws"
            adapter.start()
            deadline = time.time() + 3
            while len(sockets) < 2 and time.time() < deadline:
                time.sleep(0.01)
            adapter.stop()
            self.assertGreaterEqual(len(sockets), 2, "断开后应重连")
            self.assertTrue(sockets[0].closed, "stop 应关闭活动连接")
            self.assertFalse(adapter.running)
        finally:
            slack_mod.RECONNECT_DELAY = old_delay


class TestSlackInboundRealWebSocket(unittest.TestCase):
    """T2.0↔T2.1 接缝的**真 socket 联调**。

    前面 ``TestSlackInbound`` 用假 WS 验证协议层、``test_ws.py`` 用裸服务器验证
    传输层，但"``ws.connect`` 出来的对象能不能被 ``_handle_envelope`` 正常 ack"
    这个接缝此前没有任何测试覆盖。这里用真服务器线程 + 真 WebSocket 客户端跑通
    完整链路：握手 → 收包 → 授权 → Inbound → ack 掩码帧回到服务器。
    """

    @staticmethod
    def _load_ws_test_helpers():
        # ``unittest discover -s tests`` 以顶层模块名导入，``python -m unittest
        # tests.test_x`` 则以包名导入 —— 两种跑法都得能用。
        try:
            from tests.test_ws import Server, recv_masked_text, send_text
        except ImportError:
            from test_ws import Server, recv_masked_text, send_text
        return Server, recv_masked_text, send_text

    def test_end_to_end_over_real_websocket(self):
        from opencode_bridge.ws import connect as ws_connect

        Server, recv_masked_text, send_text = self._load_ws_test_helpers()

        hooks = RecordingHooks()
        adapter = build("slack", {"bot_token": "xoxb-t", "app_token": "xapp-t"}, hooks)
        adapter.min_interval = 0

        server = Server()
        self.addCleanup(server.stop)

        calls = []

        def open_url():
            # 只允许建一次连接：否则 stop 之后重连会挂在新连接上等握手超时。
            if calls:
                raise RuntimeError("测试只允许建一次连接")
            calls.append(server.url)
            return server.url

        adapter._open_socket_url = open_url
        adapter._ws_factory = lambda url: ws_connect(url)   # 走真实实现，不用假 WS

        server.start_server()
        adapter.start()
        try:
            conn = server.wait_handshake()
            send_text(
                conn,
                json.dumps(
                    {
                        "envelope_id": "E1",
                        "type": "events_api",
                        "payload": {
                            "event": {
                                "type": "message",
                                "text": "真 socket 联调",
                                "channel": "C_REAL",
                                "user": "U_REAL",
                                "ts": "1700000000.000100",
                            }
                        },
                    }
                ),
            )

            deadline = time.time() + 5
            while not hooks.inbounds and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(hooks.inbounds), 1, "真 WS 收到消息后应产生 Inbound")
            self.assertEqual(hooks.inbounds[0].conversation_id, "channel:C_REAL")
            self.assertEqual(hooks.inbounds[0].text, "真 socket 联调")
            self.assertEqual(hooks.inbounds[0].user_id, "U_REAL")
            self.assertEqual(hooks.inbounds[0].platform, "slack")

            # ack 必须以「客户端掩码帧」回到服务器；recv_masked_text 会校验 MASK 位
            self.assertEqual(json.loads(recv_masked_text(conn)), {"envelope_id": "E1"})
        finally:
            adapter.stop()


class TestSendResult(unittest.TestCase):
    """T1.3 — 出站错误分类：失败从"静默 None"变成结构化可观测数据。"""

    def test_classify_http_mapping(self):
        from opencode_bridge.adapters.base import classify_http

        self.assertEqual(classify_http(0), SendError.TRANSIENT)       # 传输层失败
        self.assertEqual(classify_http(429), SendError.RATE_LIMITED)
        self.assertEqual(classify_http(401), SendError.FORBIDDEN)
        self.assertEqual(classify_http(403), SendError.FORBIDDEN)
        self.assertEqual(classify_http(404), SendError.NOT_FOUND)
        self.assertEqual(classify_http(413), SendError.TOO_LONG)
        self.assertEqual(classify_http(400), SendError.BAD_FORMAT)
        self.assertEqual(classify_http(400, "message is too long"), SendError.TOO_LONG)
        self.assertEqual(classify_http(500), SendError.TRANSIENT)
        self.assertEqual(classify_http(503), SendError.TRANSIENT)
        self.assertEqual(classify_http(302), SendError.UNKNOWN)

    def test_send_result_ok(self):
        adapter = build("telegram", {"bot_token": "t"}, RecordingHooks())
        adapter.send = lambda out: MsgHandle("c", "1", "telegram")
        r = adapter.send_result(Outbound(conversation_id="chat:1", text="hi"))
        self.assertTrue(r.ok)
        self.assertFalse(r.partial)
        self.assertIsNotNone(r.handle)
        self.assertEqual(r.error_kind, SendError.UNKNOWN)  # ok 时无意义

    def test_send_result_failure_uses_noted_kind(self):
        adapter = build("telegram", {"bot_token": "t"}, RecordingHooks())
        adapter.send = lambda out: None

        def _fail(out):
            adapter._note_send_failure(SendError.RATE_LIMITED, "429 too many", retry_after=7.0)
            return None

        adapter.send = _fail
        r = adapter.send_result(Outbound(conversation_id="chat:1", text="hi"))
        self.assertFalse(r.ok)
        self.assertEqual(r.error_kind, SendError.RATE_LIMITED)
        self.assertEqual(r.retry_after, 7.0)
        self.assertEqual(r.error_detail, "429 too many")

    def test_send_result_partial_when_handle_returned_after_failure(self):
        """分片"部分成功"：有句柄但过程记过失败 → 必须标 partial，不能记成全成功。"""
        adapter = build("telegram", {"bot_token": "t"}, RecordingHooks())

        def _partial(out):
            adapter._note_send_failure(SendError.TRANSIENT, "chunk 3 failed")
            return MsgHandle("c", "2", "telegram")

        adapter.send = _partial
        r = adapter.send_result(Outbound(conversation_id="chat:1", text="hi"))
        self.assertTrue(r.ok)
        self.assertTrue(r.partial)
        self.assertEqual(r.error_kind, SendError.TRANSIENT)

    def test_send_result_does_not_propagate_exception(self):
        adapter = build("telegram", {"bot_token": "t"}, RecordingHooks())

        def _boom(out):
            raise RuntimeError("boom")

        adapter.send = _boom
        r = adapter.send_result(Outbound(conversation_id="chat:1", text="hi"))
        self.assertFalse(r.ok)
        self.assertEqual(r.error_kind, SendError.TRANSIENT)

    def test_send_result_unknown_when_send_returns_none_without_note(self):
        adapter = build("telegram", {"bot_token": "t"}, RecordingHooks())
        adapter.send = lambda out: None
        r = adapter.send_result(Outbound(conversation_id="chat:1", text="hi"))
        self.assertFalse(r.ok)
        self.assertEqual(r.error_kind, SendError.UNKNOWN)

    def test_last_send_error_property_tracks(self):
        adapter = build("telegram", {"bot_token": "t"}, RecordingHooks())
        self.assertIsNone(adapter.last_send_error)
        adapter._note_send_failure(SendError.FORBIDDEN, "no rights")
        self.assertEqual(adapter.last_send_error, SendError.FORBIDDEN)
        adapter._clear_send_failure()
        self.assertIsNone(adapter.last_send_error)

    def test_slack_error_classification(self):
        from opencode_bridge.adapters.slack import _classify_slack_error

        self.assertEqual(_classify_slack_error(200, "rate_limited"), SendError.RATE_LIMITED)
        self.assertEqual(_classify_slack_error(200, "channel_not_found"), SendError.NOT_FOUND)
        self.assertEqual(_classify_slack_error(200, "invalid_auth"), SendError.FORBIDDEN)
        self.assertEqual(_classify_slack_error(200, "missing_scope"), SendError.FORBIDDEN)
        self.assertEqual(_classify_slack_error(200, "no_text"), SendError.BAD_FORMAT)
        self.assertEqual(_classify_slack_error(429, ""), SendError.RATE_LIMITED)
        self.assertEqual(_classify_slack_error(200, "weird_thing"), SendError.UNKNOWN)

    def test_bad_conversation_id_classified(self):
        adapter = build("telegram", {"bot_token": "t"}, RecordingHooks())
        r = adapter.send_result(Outbound(conversation_id="not-a-chat", text="hi"))
        self.assertFalse(r.ok)
        self.assertEqual(r.error_kind, SendError.BAD_FORMAT)


class TestBuildRegistry(unittest.TestCase):
    def test_build_known_adapters(self):
        hooks = RecordingHooks()
        tg = build("telegram", {"bot_token": "t"}, hooks)
        self.assertIsInstance(tg, TelegramAdapter)
        sl = build("slack", {"bot_token": "t"}, hooks)
        self.assertIsInstance(sl, SlackAdapter)
        dc = build("discord", {"bot_token": "t"}, hooks)
        self.assertIsInstance(dc, DiscordAdapter)
        for adapter in (tg, sl, dc):
            self.assertIsInstance(adapter, Adapter)
            adapter.stop()  # harmless with no thread

    def test_build_unknown_raises_keyerror(self):
        with self.assertRaises(KeyError):
            build("nope", {}, RecordingHooks())

    def test_build_rejects_dotted_and_non_identifier_names(self):
        """名字会进 importlib，必须挡住点号/非标识符，避免越出 adapters 包。"""
        hooks = RecordingHooks()
        for bad in ("os.path", "a.b", "", "not-an-identifier", "../base"):
            with self.subTest(name=bad):
                with self.assertRaises(KeyError):
                    build(bad, {}, hooks)

    def test_brand_new_adapter_needs_no_change_to_base(self):
        """新增平台的成本应只是新建一个模块文件 —— 核心注册表不得硬编码平台名。"""
        import importlib.machinery
        import sys
        import types

        from opencode_bridge.adapters import base as base_mod

        pkg = base_mod.__package__ or "opencode_bridge.adapters"
        mod_name = f"{pkg}.faketestplat"
        module = types.ModuleType(mod_name)
        module.__spec__ = importlib.machinery.ModuleSpec(mod_name, None)

        class FakePlatAdapter(Adapter):
            name = "faketestplat"
            label = "FakePlat"

            def start(self) -> None:
                return None

            def send(self, out):
                return None

            def edit(self, handle, out):
                return None

        module.FakePlatAdapter = FakePlatAdapter
        sys.modules[mod_name] = module
        base_mod._REGISTRY.pop("faketestplat", None)
        try:
            base_mod.register("faketestplat")(FakePlatAdapter)
            adapter = base_mod.build("faketestplat", {}, RecordingHooks())
            self.assertIsInstance(adapter, FakePlatAdapter)
            self.assertEqual(adapter.capabilities()["name"], "faketestplat")
        finally:
            sys.modules.pop(mod_name, None)
            base_mod._REGISTRY.pop("faketestplat", None)


class TestTransportBasedRunningIsReported(unittest.TestCase):
    """凡是线程归``Transport`` 所有的适配器，都**必须**覆写 ``running``。

    ⚠️ 这条来自一个真实缺陷：基类 ``Adapter.running`` 读 ``self._thread``
    （``thread is not None and thread.is_alive()``），而传输层化的适配器把线程
    交给了 ``Transport``、**从不设** ``_thread`` —— 于是继承基类的那个实现会
    **永远返回 False**，``capabilities()["running"]`` 也就永远False，
    ``--status`` / ``--setup --json`` 会把一个**正在收信**的平台报成"没在跑"。

    ``ntfy`` 与 ``email`` 都栽在这上面（它们生在新传输层上，却继承了基类实现）。
    这条测试一次性覆盖全部适配器，所以下一个"生在新传输层上"的平台不会再栽。

    判据用**类自己的``__dict__``** 而不是"能否跑起来"—— 后者需要真实网络或
    逐个平台造条件，既慢又脆。
    """

    def test_transport_based_adapters_must_override_running(self):
        import inspect
        import sys

        from opencode_bridge.adapters import adapter_class, registered_names

        checked = []
        for name in sorted(registered_names()):
            cls = adapter_class(name)
            module = sys.modules[cls.__module__]
            source = inspect.getsource(module)
            if "_transport" not in source:
                continue  # 未传输层化，仍用 _thread，继承基类是对的
            checked.append(name)
            with self.subTest(platform=name):
                self.assertIn(
                    "running",
                    cls.__dict__,
                    f"{name} 的线程归 Transport 所有，却继承了基类 running"
                    f"（基类读 self._thread，本类从不设它）→ capabilities() 里"
                    f" running 永远是 False，状态视图会把正在收信的平台报成没在跑",
                )
        # 这条断言本身要有效：至少得覆盖到几个已知传输层化的平台
        for expected in ("ntfy", "email", "irc", "telegram"):
            self.assertIn(expected, checked, f"{expected} 应被本用例覆盖")

    def test_base_running_still_works_for_thread_owning_adapters(self):
        """反面对照：仍用 ``_thread`` 的适配器继承基类实现是对的，别被误改。"""
        from opencode_bridge.adapters import adapter_class, registered_names

        inheriting = []
        for name in sorted(registered_names()):
            cls = adapter_class(name)
            if "running" in cls.__dict__:
                continue
            inheriting.append(name)
            with self.subTest(platform=name):
                self.assertFalse(
                    hasattr(cls, "_transport"),
                    f"{name} 既有 _transport 又没覆写 running —— 那才是 bug",
                )
        self.assertTrue(inheriting, "应至少有几个未迁移的平台继承基类 running")


class TestCapabilities(unittest.TestCase):
    """T1.1 — 能力显式声明：调用方据此判断，不再靠 try/except 猜。"""

    def test_capabilities_truthful_per_platform(self):
        hooks = RecordingHooks()
        expected = {
            "telegram": {"max_message_length": 4096, "supports_inbound": True,
                         "supports_inline_buttons": True, "supports_media": True},
            "slack": {"max_message_length": 40000, "supports_inbound": True,
                      "supports_inline_buttons": False, "supports_media": False},
            "discord": {"max_message_length": 2000, "supports_inbound": True,
                        "supports_inline_buttons": False, "supports_media": False},
        }
        for name, want in expected.items():
            caps = build(name, {"bot_token": "t"}, hooks).capabilities()
            self.assertEqual(caps["name"], name)
            self.assertTrue(caps["label"], f"{name} 应声明展示名")
            for key, value in want.items():
                self.assertEqual(caps[key], value, f"{name}.{key} 声明不实")

    def test_capabilities_snapshot_keys(self):
        caps = build("telegram", {"bot_token": "t"}, RecordingHooks()).capabilities()
        self.assertEqual(
            set(caps),
            {"name", "label", "max_message_length", "supports_inbound",
             "supports_inline_buttons", "supports_media", "typed_command_prefix",
             "allowed_chat_ids_count", "config_optional", "running"},
        )
        # config_optional 默认 False —— 除"无凭据且默认值安全"的平台（如 a2a）外，
        # 都必须显式配置才放行。守卫它在默认侧，防止有人顺手把它默认成 True。
        self.assertFalse(caps["config_optional"])

    def test_command_prefix_declared(self):
        for name in ("telegram", "slack", "discord"):
            adapter = build(name, {"bot_token": "t"}, RecordingHooks())
            self.assertEqual(adapter.typed_command_prefix, "/", name)


class TestAccessGate(unittest.TestCase):
    """T1.2 — 授权闸门统一到基类，三平台同一套判定。"""

    def test_empty_allowlist_admits_all(self):
        adapter = build("telegram", {"bot_token": "t"}, RecordingHooks())
        self.assertTrue(adapter.admits(55))
        self.assertTrue(adapter.admits("任意 chat"))

    def test_allowlist_filters(self):
        adapter = build(
            "telegram", {"bot_token": "t", "allowed_chat_ids": [55, "66"]},
            RecordingHooks(),
        )
        self.assertTrue(adapter.admits(55))
        self.assertTrue(adapter.admits("66"))
        self.assertFalse(adapter.admits(77))

    def test_allowlist_accepts_int_and_str_and_strips(self):
        adapter = build(
            "telegram", {"bot_token": "t", "allowed_chat_ids": [" 55 "]},
            RecordingHooks(),
        )
        self.assertEqual(adapter.allowed_chat_ids, {"55"})
        self.assertTrue(adapter.admits(55))
        self.assertTrue(adapter.admits(" 55 "))

    def test_alternative_allowlist_keys(self):
        for key in ("allowed_chats", "allowlist"):
            adapter = build(
                "telegram", {"bot_token": "t", key: [55]}, RecordingHooks()
            )
            self.assertTrue(adapter.admits(55), key)
            self.assertFalse(adapter.admits(66), key)

    def test_scalar_allowlist_value(self):
        adapter = build("telegram", {"bot_token": "t", "allowed_chat_ids": 55},
                        RecordingHooks())
        self.assertTrue(adapter.admits(55))
        self.assertFalse(adapter.admits(66))

    def test_telegram_allowed_delegates_to_base(self):
        adapter = build(
            "telegram", {"bot_token": "t", "allowed_chat_ids": [55]},
            RecordingHooks(),
        )
        self.assertEqual(adapter._allowed(55), adapter.admits(55))
        self.assertEqual(adapter._allowed(77), adapter.admits(77))

    def test_all_platforms_share_the_same_gate(self):
        hooks = RecordingHooks()
        for name in ("telegram", "slack", "discord"):
            adapter = build(name, {"bot_token": "t", "allowed_chat_ids": [55]}, hooks)
            self.assertEqual(adapter.admits(55), True, name)
            self.assertEqual(adapter.admits(77), False, name)


if __name__ == "__main__":
    unittest.main()

"""Lane B tests — adapters (no network; HTTP layer is monkeypatched)."""

from __future__ import annotations

import logging
import time
import unittest

# Keep expected-failure warnings out of the test output; assertLogs still
# works because it swaps in its own handler on the target logger.
logging.getLogger("opencode_bridge").addHandler(logging.NullHandler())

from opencode_bridge.adapters import Adapter, AdapterError, build
from opencode_bridge.adapters.discord import DiscordAdapter
from opencode_bridge.adapters.slack import SlackAdapter
from opencode_bridge.adapters.telegram import TelegramAdapter, split_text
from opencode_bridge.hooks import Button, Inbound, MsgHandle, Outbound


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


class TestSplitText(unittest.TestCase):
    def test_short_text_single_chunk(self):
        self.assertEqual(split_text("abc"), ["abc"])
        self.assertEqual(split_text("x" * 4096), ["x" * 4096])

    def test_4000_plus_split_at_newlines(self):
        text = ("L" * 50 + "\n") * 100  # 5100 chars
        self.assertGreater(len(text), 4000)
        chunks = split_text(text)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 4096)
            self.assertGreater(len(chunk), 0)
        # non-final chunks should end on a newline boundary
        for chunk in chunks[:-1]:
            self.assertTrue(chunk.endswith("\n"), "cut not on newline boundary")
        self.assertEqual("".join(chunks), text)

    def test_hard_cut_without_newlines(self):
        chunks = split_text("A" * 5000)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(len(chunks[0]), 4096)
        self.assertEqual(len(chunks[1]), 904)
        self.assertEqual("".join(chunks), "A" * 5000)


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
        self.assertEqual(inbound.conversation_id, "chat:55")
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
        self.assertEqual(inbound.conversation_id, "chat:55")
        # hooks.on_callback got (conversation_id, data, query_id)
        self.assertEqual(hooks.callbacks, [("chat:55", "act:ok", "QID1")])
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
        self.assertEqual(hooks.inbounds[0].conversation_id, "chat:999")

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


if __name__ == "__main__":
    unittest.main()

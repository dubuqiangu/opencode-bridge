"""ntfy 适配器测试（零真实网络：HTTP 层全部 monkeypatch）。

覆盖重点（本平台最容易写错的四处）：
1. **启动不重放历史缓存** —— ``since=<当前时间戳>``，否则首次启动会把整个话题
   缓存（最多 10MB）当新消息，各触发一次 agent 运行。
2. **游标在队列排空时才推进** —— 推进过早 + 进程崩溃 = 静默丢消息。
3. **防回环只用 tags，绝不用 title** —— ntfy 无用户身份，title 是发布者可控字段。
4. **4096 是字节不是字符** —— 用 ``text[:4096]`` 截会把中文切成半个字符。
"""

from __future__ import annotations

import json
import time
import unittest

from opencode_bridge.adapters.ntfy import (
    DEFAULT_POLL_INTERVAL,
    MESSAGE_LIMIT,
    NtfyAdapter,
)
from opencode_bridge.hooks import Inbound, MsgHandle, Outbound, SendError
from opencode_bridge.identity import platform_of
from opencode_bridge.transport import NOTHING


class RecordingHooks:
    def __init__(self):
        self.inbounds: list[Inbound] = []
        self.callbacks: list[tuple] = []

    def on_inbound(self, inbound: Inbound) -> None:
        self.inbounds.append(inbound)

    def on_callback(self, conversation_id: str, data: str, query_id: str) -> None:
        self.callbacks.append((conversation_id, data, query_id))


def make_ntfy(**cfg) -> tuple[NtfyAdapter, RecordingHooks]:
    base = {"topic": "mytopic", "server": "https://ntfy.example.com"}
    base.update(cfg)
    hooks = RecordingHooks()
    adapter = NtfyAdapter(base, hooks)
    adapter.min_interval = 0        # 测试里不要人为 sleep
    return adapter, hooks


def msg(msg_id="m1", text="hello", **extra) -> dict:
    out = {
        "id": msg_id,
        "time": 1700000000,
        "event": "message",
        "topic": "mytopic",
        "message": text,
    }
    out.update(extra)
    return out


class Responder:
    """可编排的假 ``_http``：记录调用，按脚本返回。"""

    def __init__(self, script=None):
        self.script = list(script or [])
        self.calls: list[dict] = []

    def __call__(self, method, url, *, data=None, headers=None):
        self.calls.append(
            {"method": method, "url": url, "data": data, "headers": dict(headers or {})}
        )
        if self.script:
            item = self.script.pop(0)
        else:
            item = (200, [], {})
        if isinstance(item, BaseException):
            raise item
        return item


class TestCapabilities(unittest.TestCase):
    def test_declarations(self):
        adapter, _ = make_ntfy()
        self.assertEqual(adapter.name, "ntfy")
        self.assertTrue(adapter.supports_inbound)
        self.assertFalse(adapter.supports_inline_buttons)
        self.assertFalse(adapter.supports_media)
        self.assertEqual(adapter.max_message_length, MESSAGE_LIMIT)

    def test_required_tokens(self):
        adapter, _ = make_ntfy()
        # 不变量：outbound_tokens ⊆ required_tokens
        self.assertLessEqual(set(adapter.outbound_tokens), set(adapter.required_tokens))
        self.assertIn("topic", adapter.required_tokens)

    def test_in_registration_table(self):
        from opencode_bridge.adapters import registered_names
        self.assertIn("ntfy", registered_names())

    def test_buildable(self):
        from opencode_bridge.adapters import build
        adapter = build("ntfy", {"topic": "t"}, object())
        self.assertIsInstance(adapter, NtfyAdapter)


class TestAuth(unittest.TestCase):
    def test_no_auth_is_allowed(self):
        adapter, _ = make_ntfy()
        self.assertIsNone(adapter._auth_header)

    def test_bearer_token(self):
        adapter, _ = make_ntfy(token="tk_secret")
        self.assertEqual(adapter._auth_header, "Bearer tk_secret")

    def test_basic_auth(self):
        adapter, _ = make_ntfy(user="alice", password="s3cret")
        self.assertTrue(adapter._auth_header.startswith("Basic "))

    def test_token_wins_over_basic(self):
        adapter, _ = make_ntfy(token="tk_x", user="alice", password="p")
        self.assertEqual(adapter._auth_header, "Bearer tk_x")

    def test_auth_header_present_in_requests(self):
        adapter, _ = make_ntfy(token="tk_x")
        self.assertEqual(adapter._headers(publish=False).get("Authorization"),
                         "Bearer tk_x")
        self.assertEqual(adapter._headers(publish=True).get("Authorization"),
                         "Bearer tk_x")

    def test_echo_tag_only_on_publish(self):
        adapter, _ = make_ntfy()
        self.assertNotIn("X-Tags", adapter._headers(publish=False))
        self.assertEqual(adapter._headers(publish=True)["X-Tags"],
                         adapter.echo_tag)


class TestSubscribeUrl(unittest.TestCase):
    def test_bootstrap_uses_now_not_whole_cache(self):
        """首次必须 since=<当前时间>，否则会重放整个话题缓存。"""
        adapter, _ = make_ntfy()
        before = int(time.time())
        url = adapter._poll_url()
        after = int(time.time())
        self.assertIn("poll=1", url)
        self.assertIn(f"/{adapter.topic}/json", url)   # topic 未被 url-encode 破坏
        since = url.split("since=")[1]
        self.assertTrue(before <= int(since) <= after,
                        f"首次游标应是当前时间戳，实际 {since}")

    def test_subsequent_polls_use_message_id_cursor(self):
        adapter, _ = make_ntfy()
        adapter._since = "abc123"
        self.assertIn("since=abc123", adapter._poll_url())

    def test_topic_is_url_encoded(self):
        adapter, _ = make_ntfy(topic="a b/c")
        self.assertNotIn(" ", adapter._poll_url())


class TestFetchAndCursor(unittest.TestCase):
    def test_no_messages_returns_nothing(self):
        adapter, _ = make_ntfy()
        adapter._http = Responder([(200, [], {})])
        self.assertIs(adapter._fetch_one(), NOTHING)

    def test_filters_out_non_message_events(self):
        """open / keepalive / poll_request 都不能当消息处理。"""
        adapter, _ = make_ntfy()
        adapter._http = Responder([(200, [
            {"event": "open", "topic": "mytopic"},
            {"event": "keepalive", "topic": "mytopic"},
            {"event": "poll_request", "topic": "mytopic"},
        ], {})])
        self.assertIs(adapter._fetch_one(), NOTHING)

    def test_returns_one_message_at_a_time(self):
        adapter, _ = make_ntfy()
        adapter._http = Responder([(200, [msg("m1"), msg("m2", "second")], {})])
        first = adapter._fetch_one()
        self.assertEqual(first["id"], "m1")
        # 第二条来自本地队列，**不应**再发 HTTP
        calls_before = len(adapter._http.calls)
        second = adapter._fetch_one()
        self.assertEqual(second["id"], "m2")
        self.assertEqual(len(adapter._http.calls), calls_before)

    def test_cursor_advances_only_after_queue_drains(self):
        """队列还有剩余时游标不能推进，否则崩溃会静默丢消息。"""
        adapter, _ = make_ntfy()
        adapter._http = Responder([(200, [msg("m1"), msg("m2"), msg("m3")], {})])
        adapter._fetch_one()               # 取走 m1，队列还剩 m2/m3
        self.assertEqual(adapter._since, "",
                         "队列未排空时游标必须不动")
        adapter._fetch_one()               # m2，队列还剩 m3
        self.assertEqual(adapter._since, "")
        adapter._fetch_one()               # m3，队列空了 → 推进
        self.assertEqual(adapter._since, "m3")

    def test_malformed_items_are_skipped(self):
        adapter, _ = make_ntfy()
        adapter._http = Responder([(200, ["not-a-dict", None, 42, msg("ok")], {})])
        self.assertEqual(adapter._fetch_one()["id"], "ok")

    def test_single_dict_payload_tolerated(self):
        """服务端若返回单个对象而非数组，不应崩。"""
        adapter, _ = make_ntfy()
        adapter._http = Responder([(200, msg("solo"), {})])
        self.assertIs(adapter._fetch_one(), NOTHING)

    def test_http_error_raises_for_retry(self):
        adapter, _ = make_ntfy()
        adapter._http = Responder([(500, None, {})])
        with self.assertRaises(RuntimeError):
            adapter._fetch_one()

    def test_rate_limit_is_not_a_retryable_error(self):
        """429 是带宽预算耗尽，重试无用 —— 应返回 NOTHING 且可观测。"""
        adapter, _ = make_ntfy()
        adapter._http = Responder([(429, {"code": "42905"}, {})])
        self.assertIs(adapter._fetch_one(), NOTHING)
        self.assertEqual(adapter.last_send_error, SendError.RATE_LIMITED)

    def test_truncation_header_is_surfaced(self):
        adapter, _ = make_ntfy()
        adapter._http = Responder([(200, [], {"x-messages-truncated": "1"})])
        with self.assertLogs("opencode_bridge.adapters.ntfy", level="WARNING") as cm:
            self.assertIs(adapter._fetch_one(), NOTHING)
        self.assertTrue(any("截断" in line for line in cm.output), cm.output)


class TestInboundDispatch(unittest.TestCase):
    def _feed(self, adapter, item):
        adapter._on_raw(item)

    def test_message_becomes_inbound(self):
        adapter, hooks = make_ntfy()
        self._feed(adapter, msg("m1", "hello there"))
        self.assertEqual(len(hooks.inbounds), 1)
        ib = hooks.inbounds[0]
        self.assertEqual(ib.text, "hello there")
        self.assertEqual(ib.platform, "ntfy")
        self.assertEqual(ib.message_id, "m1")
        self.assertEqual(platform_of(ib.conversation_id), "ntfy")

    def test_own_echo_is_dropped_by_tag(self):
        adapter, hooks = make_ntfy()
        self._feed(adapter, msg("m1", "mine", tags=["opencode-bridge", "other"]))
        self.assertEqual(hooks.inbounds, [])

    def test_echo_tag_may_be_string_not_list(self):
        """tags 偶尔是字符串而非数组，不能因此放行自己的消息。"""
        adapter, hooks = make_ntfy()
        self._feed(adapter, msg("m1", "mine", tags="opencode-bridge"))
        self.assertEqual(hooks.inbounds, [])

    def test_title_is_never_used_as_identity(self):
        """title 是发布者可控字段 —— 别人可以自称同样的 title。"""
        adapter, hooks = make_ntfy()
        self._feed(adapter, msg("m1", "spoofed", title="opencode-bridge"))
        self.assertEqual(len(hooks.inbounds), 1,
                         "title 不参与任何身份判断，应当正常投递")

    def test_empty_body_dropped(self):
        adapter, hooks = make_ntfy()
        for body in ("", "   ", None):
            self._feed(adapter, msg("m1", body))
        self.assertEqual(hooks.inbounds, [])

    def test_non_dict_ignored(self):
        adapter, hooks = make_ntfy()
        self._feed(adapter, "junk")
        self.assertEqual(hooks.inbounds, [])

    def test_authorization_gate_runs_before_inbound(self):
        adapter, hooks = make_ntfy(allowed_chat_ids=["other-topic"])
        self._feed(adapter, msg("m1", "hi"))
        self.assertEqual(hooks.inbounds, [], "白名单外的话题必须被丢弃")

    def test_hook_exception_does_not_propagate(self):
        adapter, hooks = make_ntfy()

        class Boom:
            def on_inbound(self, inbound):
                raise RuntimeError("boom")

            def on_callback(self, *a):
                pass

        adapter.hooks = Boom()
        self._feed(adapter, msg("m1"))   # 不应抛


class TestLifecycle(unittest.TestCase):
    def test_missing_topic_does_not_start(self):
        adapter = NtfyAdapter({"server": "https://ntfy.example.com"}, RecordingHooks())
        with self.assertLogs("opencode_bridge.adapters.ntfy", level="WARNING"):
            adapter.start()
        self.assertFalse(adapter.running if hasattr(adapter, "running") else True)
        self.assertIsNone(adapter._transport)

    def test_stop_without_start_is_safe(self):
        adapter, _ = make_ntfy()
        adapter.stop()

    def test_stop_is_idempotent(self):
        adapter, _ = make_ntfy()
        adapter.start()
        adapter.stop()
        adapter.stop()

    def test_server_trailing_slash_normalised(self):
        adapter, _ = make_ntfy(server="https://ntfy.example.com/")
        self.assertEqual(adapter.server, "https://ntfy.example.com")


class TestByteLimit(unittest.TestCase):
    def test_fit_bytes_respects_utf8_budget(self):
        """ntfy 上限 4096 是**字节**；中文一字 3 字节。"""
        adapter, _ = make_ntfy()
        text = "中" * 5000                       # 15000 字节
        fitted = adapter._fit_bytes(text)
        self.assertLessEqual(len(fitted.encode("utf-8")), MESSAGE_LIMIT)
        self.assertEqual(fitted, "中" * (MESSAGE_LIMIT // 3))

    def test_fit_bytes_never_splits_a_character(self):
        """用 text[:4096] 截会把中文切成半个字符，编码即报错。"""
        adapter, _ = make_ntfy()
        for text in ("中" * 5000, "🙂" * 3000, "a中🙂" * 2000):
            with self.subTest(text=text[:6]):
                fitted = adapter._fit_bytes(text)
                # 能编码 = 没有半个字符
                self.assertIsInstance(fitted.encode("utf-8"), bytes)
                self.assertLessEqual(len(fitted.encode("utf-8")), MESSAGE_LIMIT)

    def test_fit_bytes_keeps_short_text_untouched(self):
        adapter, _ = make_ntfy()
        self.assertEqual(adapter._fit_bytes("short"), "short")
        self.assertEqual(adapter._fit_bytes(""), "")

    def test_ascii_budget_is_one_to_one(self):
        adapter, _ = make_ntfy()
        self.assertEqual(adapter._fit_bytes("a" * 100), "a" * 100)


class TestSend(unittest.TestCase):
    def test_send_posts_with_echo_tag(self):
        adapter, _ = make_ntfy()
        adapter._http = Responder([(200, {"id": "p1"}, {})])
        handle = adapter.send(Outbound("ntfy:mytopic", "hi there"))
        self.assertIsInstance(handle, MsgHandle)
        self.assertEqual(handle.message_id, "p1")
        call = adapter._http.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["data"], b"hi there")
        self.assertEqual(call["headers"]["X-Tags"], adapter.echo_tag)
        self.assertEqual(call["headers"]["Content-Type"], "text/plain; charset=utf-8")

    def test_send_strips_ntfy_prefix_from_conversation_id(self):
        adapter, _ = make_ntfy()
        adapter._http = Responder([(200, {"id": "p1"}, {})])
        adapter.send(Outbound("ntfy:mytopic", "x"))
        self.assertIn("/mytopic", adapter._http.calls[0]["url"])
        self.assertNotIn("ntfy%3A", adapter._http.calls[0]["url"])

    def test_send_splits_long_text_and_keeps_each_piece_within_bytes(self):
        adapter, _ = make_ntfy()
        adapter._http = Responder([(200, {"id": f"p{i}"}, {}) for i in range(20)])
        adapter.send(Outbound("ntfy:mytopic", "中" * 20000))
        self.assertGreater(len(adapter._http.calls), 1, "超限应分多片")
        for call in adapter._http.calls:
            self.assertLessEqual(len(call["data"]), MESSAGE_LIMIT)

    def test_send_failure_is_observable(self):
        adapter, _ = make_ntfy()
        # 注意：Responder 脚本耗尽后默认返回 200，所以**两次**失败都要排进脚本 ——
        # 否则第二次 send() 会"意外成功"，把失败掩盖掉（本测试第一版就踩了这个）。
        adapter._http = Responder([
            (403, {"error": "forbidden"}, {}),
            (403, {"error": "forbidden"}, {}),
        ])
        handle = adapter.send(Outbound("ntfy:mytopic", "x"))
        self.assertIsNone(handle)
        result = adapter.send_result(Outbound("ntfy:mytopic", "x"))
        self.assertFalse(result.ok)
        self.assertIn(result.error_kind,
                      (SendError.FORBIDDEN, SendError.BAD_FORMAT))

    def test_send_rate_limit_classified(self):
        adapter, _ = make_ntfy()
        adapter._http = Responder([(429, {"code": "42905"}, {})])
        adapter.send(Outbound("ntfy:mytopic", "x"))
        self.assertEqual(adapter.last_send_error, SendError.RATE_LIMITED)

    def test_send_rejects_empty_text(self):
        adapter, _ = make_ntfy()
        handle = adapter.send(Outbound("ntfy:mytopic", ""))
        self.assertIsNone(handle)
        self.assertEqual(adapter.last_send_error, SendError.BAD_FORMAT)


class TestEditAndAnswer(unittest.TestCase):
    def test_edit_returns_false(self):
        """ntfy 没有编辑端点 —— 诚实返回 False，让 core 退化成发新消息。"""
        adapter, _ = make_ntfy()
        handle = MsgHandle("ntfy:mytopic", "m1", "ntfy")
        self.assertIs(adapter.edit(handle, Outbound("ntfy:mytopic", "updated")), False)

    def test_answer_is_noop(self):
        adapter, _ = make_ntfy()
        self.assertIsNone(adapter.answer("q1", "text"))


class TestPollIntervalCoercion(unittest.TestCase):
    """⚠️ ``poll_interval`` 原来在 ``start()`` 里是**裸** ``float(self.config.get(...) or ...)``。

    那一行的两个坏法（都已修）：

    1. **填非数字** ⇒ ``ValueError`` 打断 ``start()`` ⇒ 被 ``BridgeCore.start`` 的
       except 接住 ⇒ **ntfy 这个适配器从此不启动**，而日志只有一条栈、
       **不说「是哪个键配错了」**。
    2. **填负数更糟** ⇒ 会被传输层夹成 ``0.0`` ⇒ **每轮空转立刻重问**，
       **本地毫无异常**，故障体现在服务端。

    ⇒ 现在走共享助手 ``coerce_float``，下界是**开区间**（``0`` 不是「很短的轮询」
    是「不等待」）。
    """

    def _start_and_read_idle(self, **cfg):
        """起一次适配器并读**真拿到的**空闲间隔（⛔ 不读类属性、不读配置）。"""
        from opencode_bridge.transport import PollingTransport

        adapter, _ = make_ntfy(**cfg)
        original_start = PollingTransport.start
        PollingTransport.start = lambda self, *a, **k: None   # 不起线程 ⇒ 零网络
        try:
            adapter.start()
        finally:
            PollingTransport.start = original_start
        return adapter._transport._idle_delay()

    def test_legal_values_are_used_verbatim_and_stay_silent(self):
        for raw in (2.5, "2.5", " 2.5 ", 0.01):
            with self.subTest(raw=raw):
                with self.assertNoLogs("opencode_bridge.config_coerce", level="WARNING"):
                    self.assertEqual(self._start_and_read_idle(poll_interval=raw), float(raw))

    def test_a_non_numeric_value_falls_back_and_names_the_key(self):
        with self.assertLogs("opencode_bridge.config_coerce", level="WARNING") as caught:
            idle = self._start_and_read_idle(poll_interval="abc")
        self.assertEqual(idle, DEFAULT_POLL_INTERVAL, "非法值必须回落到默认，而不是打断 start()")
        self.assertIn("poll_interval", "\n".join(caught.output))

    def test_a_negative_value_never_becomes_an_empty_loop(self):
        """⚠️ 反向断言：退回裸 ``float()`` 就会红 —— 负数会被传输层夹成 ``0.0``。"""
        with self.assertLogs("opencode_bridge.config_coerce", level="WARNING"):
            idle = self._start_and_read_idle(poll_interval=-5)
        self.assertEqual(idle, DEFAULT_POLL_INTERVAL)
        self.assertGreater(idle, 0.0, "0.0 = 每轮空转立刻重问")

    def test_a_boolean_is_not_read_as_a_number_of_seconds(self):
        with self.assertLogs("opencode_bridge.config_coerce", level="WARNING"):
            self.assertEqual(self._start_and_read_idle(poll_interval=True), DEFAULT_POLL_INTERVAL)

    def test_zero_is_refused_rather_than_silently_accepted(self):
        """``0`` 不是「很短的轮询」是「不等待」⇒ 开区间，不是闭区间。"""
        with self.assertLogs("opencode_bridge.config_coerce", level="WARNING"):
            self.assertEqual(self._start_and_read_idle(poll_interval=0), DEFAULT_POLL_INTERVAL)

    def test_an_absent_value_is_still_silent(self):
        with self.assertNoLogs("opencode_bridge.config_coerce", level="WARNING"):
            self.assertEqual(self._start_and_read_idle(), DEFAULT_POLL_INTERVAL)


if __name__ == "__main__":
    unittest.main()

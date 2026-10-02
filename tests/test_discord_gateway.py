"""T2.2 测试——Discord 入站（Gateway v10 over WebSocket）。零真实网络。

Gateway 协议本身就是 JSON over WebSocket，所以这里用**假 WS 注入**（`_ws_factory`，
与 ``test_adapters.py`` 里 Slack 入站同一手法）驱动协议层：比真服务器快、也更容易
断言"发出去的包长什么样"。真实 RFC 6455 收发由 ``tests/test_ws.py`` 用裸服务器覆盖；
REST 出站（``send`` / ``edit``）由 ``tests/test_adapters.py`` 覆盖，这里只放一条
回归断言，确保加入站没有把出站弄坏。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import unittest

# 让"预期内的告警"别污染测试输出（assertLogs 会自己挂 handler，不受影响）
logging.getLogger("opencode_bridge").addHandler(logging.NullHandler())

import opencode_bridge.adapters.discord as discord_mod
from opencode_bridge.adapters import build
from opencode_bridge.adapters.discord import (
    DEFAULT_INTENTS,
    GATEWAY_API_VERSION,
    INTENT_DIRECT_MESSAGES,
    INTENT_GUILD_MESSAGES,
    INTENT_MESSAGE_CONTENT,
    RECONNECT_MAX,
    RECONNECT_MIN,
    DiscordAdapter,
)
from opencode_bridge.hooks import Inbound, Outbound

GATEWAY_URL = "wss://gw.example.test/gw"
RESUME_URL = "wss://resume.example.test/gw"
BOT_USER_ID = "U_BOT_ME"


# ----------------------------------------------------------------------
# 测试替身
# ----------------------------------------------------------------------
class RecordingHooks:
    """记录每次 ``on_inbound``（外加一条顺序日志，用于断言闸门顺序）。"""

    def __init__(self, calls: list | None = None) -> None:
        self.inbounds: list[Inbound] = []
        self.calls = calls if calls is not None else []

    def on_inbound(self, inbound: Inbound) -> None:
        self.inbounds.append(inbound)
        self.calls.append(("inbound", inbound.conversation_id))

    def on_callback(self, conversation_id: str, data: str, query_id: str) -> None:
        pass


class GatewayWS:
    """假网关连接：按脚本逐条吐 JSON 文本；脚本空则返回 ``None`` 表示对端关闭。

    比 ``test_adapters.FakeWS`` 多两样：``close_code`` / ``close_reason``（重连决策
    要读）与真正的 ``close(code, reason)`` 签名（我们主动断开时会带非 1000 的码）。
    """

    def __init__(self, script=None, close_code=None, close_reason="") -> None:
        self.script = list(script or [])
        self.sent: list[str] = []
        self.closed = False
        self.close_code = close_code
        self.close_reason = close_reason
        self.close_calls: list[tuple] = []

    # -- WebSocketClient 契约 ------------------------------------------
    def recv(self) -> str | None:
        if self.script:
            return self.script.pop(0)
        return None

    def send(self, text: str) -> None:
        self.sent.append(text)

    def close(self, code: int = 1000, reason: str = "") -> None:
        self.close_calls.append((code, reason))
        self.closed = True

    # -- 测试助手 --------------------------------------------------------
    def ops(self) -> list[dict]:
        return [json.loads(item) for item in self.sent]

    def ops_of(self, op: int) -> list[dict]:
        return [pkt for pkt in self.ops() if pkt.get("op") == op]


class BlockingGatewayWS(GatewayWS):
    """``recv()`` 一直阻塞直到被 ``close()`` 唤醒（模拟真实长连接）。"""

    def __init__(self, close_code=None) -> None:
        super().__init__(close_code=close_code)
        self._woken = threading.Event()

    def recv(self) -> str | None:
        self._woken.wait(10)
        return None

    def close(self, code: int = 1000, reason: str = "") -> None:
        super().close(code, reason)
        self._woken.set()


class AutoAckGatewayWS(GatewayWS):
    """``send()`` 之后立刻回一个 op 11（模拟服务端确认心跳）。

    没有它，心跳线程会因为"一个周期内没收到 ACK"而主动断开 —— 那是正确行为，
    但就无法观察连续多个周期的心跳了。
    """

    def __init__(self, adapter, **kw) -> None:
        super().__init__(**kw)
        self._adapter = adapter

    def send(self, text: str) -> None:
        super().send(text)
        self._adapter._handle_payload(self, json.dumps({"op": 11, "d": None}))


# ----------------------------------------------------------------------
# 网关包构造（结构照官方：只有 op 0 带 s / t）
# ----------------------------------------------------------------------
def hello_packet(interval_ms: int = 45000) -> str:
    return json.dumps(
        {"op": 10, "d": {"heartbeat_interval": interval_ms, "_trace": ["gateway", "hello"]}}
    )


def ready_packet(
    user_id: str = BOT_USER_ID,
    session_id: str = "sess-1",
    resume_url: str = RESUME_URL,
    seq: int = 100,
) -> str:
    return json.dumps(
        {
            "op": 0,
            "s": seq,
            "t": "READY",
            "d": {
                "v": 10,
                "user": {"id": user_id, "username": "opencode", "bot": True},
                "session_id": session_id,
                "resume_gateway_url": resume_url,
            },
        }
    )


def dispatch_packet(t: str, d: dict, seq: int | None = 100) -> str:
    packet: dict = {"op": 0, "d": d}
    if seq is not None:
        packet["s"] = seq
    packet["t"] = t
    return json.dumps(packet)


def message_packet(
    author_id: str = "U_HUMAN",
    channel: str = "C1",
    content: str = "你好",
    msg_id: str = "M1",
    msg_type=0,
    flags=None,
    webhook_id=None,
    bot=None,
    seq: int | None = 101,
) -> str:
    author: dict = {"id": author_id, "username": "u"}
    if bot is not None:  # None = 服务端没下发这个可选键
        author["bot"] = bot
    msg: dict = {
        "id": msg_id,
        "channel_id": channel,
        "author": author,
        "content": content,
        "type": msg_type,
    }
    if flags is not None:
        msg["flags"] = flags
    if webhook_id is not None:
        msg["webhook_id"] = webhook_id
    return dispatch_packet("MESSAGE_CREATE", msg, seq=seq)


class GatewayTestCase(unittest.TestCase):
    """构造一个已连上（但还没 start 线程）的适配器。"""

    def adapter(self, hooks: RecordingHooks | None = None, **cfg) -> DiscordAdapter:
        base = {"bot_token": "tok-123", "gateway_url": GATEWAY_URL}
        base.update(cfg)
        adapter = DiscordAdapter(base, hooks or RecordingHooks())
        adapter.min_interval = 0
        return adapter

    def connected(self, hooks: RecordingHooks | None = None, script=None, **cfg):
        """模拟"已连上并收到 Hello"：返回 (adapter, ws)。"""
        adapter = self.adapter(hooks, **cfg)
        ws = GatewayWS()
        action = adapter._handle_payload(ws, hello_packet())
        self.assertEqual(action, "continue")
        return adapter, ws


# ----------------------------------------------------------------------
# 1) 能力声明
# ----------------------------------------------------------------------
class TestCapabilities(GatewayTestCase):
    def test_inbound_capability_flipped_and_tokens(self):
        adapter = DiscordAdapter({"bot_token": "t"}, RecordingHooks())
        caps = adapter.capabilities()
        self.assertIs(caps["supports_inbound"], True, "T2.2 之后 Discord 应声明支持入站")
        self.assertEqual(DiscordAdapter.required_tokens, ("bot_token",))
        self.assertEqual(caps["max_message_length"], 2000)

    def test_registry_build_reports_inbound(self):
        adapter = build("discord", {"bot_token": "t"}, RecordingHooks())
        self.assertIs(adapter.capabilities()["supports_inbound"], True)

    def test_start_without_token_does_not_spawn_thread(self):
        adapter = DiscordAdapter({"bot_token": ""}, RecordingHooks())
        with self.assertLogs("opencode_bridge.adapters.discord", level="WARNING"):
            adapter.start()  # 不抛，只警告
        self.assertIsNone(adapter._thread)
        self.assertFalse(adapter.running)

    def test_default_intents_is_the_documented_bitmask(self):
        self.assertEqual(INTENT_GUILD_MESSAGES, 512)
        self.assertEqual(INTENT_DIRECT_MESSAGES, 4096)
        self.assertEqual(INTENT_MESSAGE_CONTENT, 32768)
        self.assertEqual(DEFAULT_INTENTS, 512 | 4096 | 32768)
        self.assertEqual(DEFAULT_INTENTS, 37376)
        self.assertEqual(GATEWAY_API_VERSION, 10)

    def test_intents_config_override_and_bad_value_fallback(self):
        self.assertEqual(self.adapter(intents=4096).intents, 4096)
        with self.assertLogs("opencode_bridge.adapters.discord", level="WARNING"):
            self.assertEqual(self.adapter(intents="oops").intents, DEFAULT_INTENTS)


# ----------------------------------------------------------------------
# 2) 会话建立：Hello → 心跳 → Identify
# ----------------------------------------------------------------------
class TestSessionSetup(GatewayTestCase):
    def test_hello_then_heartbeat_then_identify_order(self):
        adapter, ws = self.connected()
        ops = ws.ops()
        self.assertEqual([pkt["op"] for pkt in ops], [1, 2], "顺序必须是 HELLO → op1 → Identify")

    def test_first_heartbeat_has_d_key_even_with_no_events(self):
        adapter, ws = self.connected()
        first = ws.ops()[0]
        self.assertIn("d", first, "心跳的 d 键不能省")
        self.assertIsNone(first["d"], "一个事件都没收到时 d 就是 null")

    def test_identify_payload_shape(self):
        adapter, ws = self.connected()
        identify = ws.ops()[1]
        data = identify["d"]
        self.assertEqual(data["token"], "tok-123")
        self.assertEqual(data["intents"], 512 | 4096 | 32768)
        self.assertEqual(data["intents"], 37376)
        self.assertIsInstance(data["intents"], int, "intents 必须是整数 bitmask，不是数组")
        self.assertEqual(
            set(data["properties"]), {"os", "browser", "device"}, "properties 三个键必须齐全"
        )
        self.assertEqual(data["properties"]["os"], "python")
        self.assertNotIn("s", identify, "Identify 不带 s")

    def test_identify_is_not_resent_when_session_exists(self):
        adapter, ws = self.connected()
        adapter._handle_payload(ws, ready_packet())
        ws2 = GatewayWS()
        adapter._handle_payload(ws2, hello_packet())
        self.assertEqual(ws2.ops_of(2), [], "有 session 时走 Resume，不再 Identify")
        self.assertEqual(len(ws2.ops_of(6)), 1)


# ----------------------------------------------------------------------
# 3) 心跳
# ----------------------------------------------------------------------
class TestHeartbeat(GatewayTestCase):
    def test_heartbeat_interval_unit_is_milliseconds(self):
        adapter, ws = self.connected()  # 默认 Hello 带 45000
        self.assertEqual(adapter._heartbeat_interval, 45.0, "45000ms 必须换算成 45s")

    def test_short_interval_fires_periodically_without_waiting_seconds(self):
        """60ms 的周期必须在亚秒尺度上真的发出去（若按秒理解就一发都没有）。"""
        adapter = self.adapter()
        ws = AutoAckGatewayWS(adapter)  # 会自动回 ACK，否则心跳线程会判定连接已死
        adapter._heartbeat_jitter = lambda interval: 0.0  # 去掉抖动，纯测周期
        adapter._handle_payload(ws, hello_packet(60))
        self.assertEqual(adapter._heartbeat_interval, 0.06, "60ms 应换算成 0.06s")
        self.assertEqual(len(ws.ops_of(1)), 1, "Hello 之后立刻发一次心跳")
        self.addCleanup(adapter._stop_heartbeat)
        deadline = time.time() + 2.0
        while len(ws.ops_of(1)) < 4 and time.time() < deadline:
            time.sleep(0.005)
        self.assertGreaterEqual(
            len(ws.ops_of(1)), 4, "60ms 周期内至少应发出 4 次心跳"
        )
        for pkt in ws.ops_of(1):
            self.assertIn("d", pkt)
        self.assertFalse(ws.closed, "有 ACK 就不该被判定为死连接")

    def test_server_heartbeat_request_answered_immediately(self):
        adapter, ws = self.connected()
        before = len(ws.ops_of(1))
        adapter._handle_payload(ws, json.dumps({"op": 1, "d": None}))
        self.assertEqual(len(ws.ops_of(1)), before + 1, "收到 op 1 必须立刻回一次心跳")

    def test_heartbeat_ack_is_recorded(self):
        adapter, ws = self.connected()
        self.assertFalse(adapter.ack_received)
        adapter._handle_payload(ws, json.dumps({"op": 11, "d": None}))
        self.assertTrue(adapter.ack_received)

    def test_heartbeat_uses_last_seq_after_events(self):
        adapter, ws = self.connected()
        adapter._handle_payload(ws, message_packet(content="第一条"))
        adapter._handle_payload(ws, json.dumps({"op": 1, "d": None}))
        self.assertEqual(ws.ops_of(1)[-1]["d"], 101)

    def test_ack_timeout_closes_with_non_1000_code(self):
        """一个周期内没收到 op 11 → 用 4000 主动断开（1000 会让 session 失效）。"""
        adapter = self.adapter()
        ws = GatewayWS()
        adapter._heartbeat_jitter = lambda interval: 0.0
        adapter._start_heartbeat(ws, 0.05)
        self.addCleanup(adapter._stop_heartbeat)
        deadline = time.time() + 2.0
        while not ws.close_calls and time.time() < deadline:
            time.sleep(0.005)
        self.assertTrue(ws.close_calls, "没有 ACK 就应该断开")
        code = ws.close_calls[0][0]
        self.assertEqual(code, discord_mod.GATEWAY_CLOSE_REQUESTED)
        self.assertNotIn(code, (1000, 1001), "不能用 1000/1001 关连接")

    def test_opcode_5_is_not_used(self):
        """op 5 已废弃；未知 opcode 必须被忽略而不是当成事件。"""
        adapter, ws = self.connected()
        hooks = RecordingHooks()
        adapter.hooks = hooks
        action = adapter._handle_payload(ws, json.dumps({"op": 5, "d": {"x": 1}}))
        self.assertEqual(action, "continue")
        self.assertEqual(hooks.inbounds, [])


# ----------------------------------------------------------------------
# 4) READY
# ----------------------------------------------------------------------
class TestReady(GatewayTestCase):
    def test_ready_caches_user_session_and_resume_url(self):
        adapter, ws = self.connected()
        adapter._handle_payload(ws, ready_packet())
        self.assertEqual(adapter.my_user_id, BOT_USER_ID)
        self.assertEqual(adapter.session_id, "sess-1")
        self.assertEqual(adapter._resume_url, RESUME_URL)
        self.assertEqual(adapter.last_seq, 100)

    def test_last_seq_only_updates_on_non_null_s(self):
        adapter, ws = self.connected()
        adapter._handle_payload(ws, ready_packet(seq=500))
        self.assertEqual(adapter.last_seq, 500)
        for packet in (
            json.dumps({"op": 11, "d": None}),
            json.dumps({"op": 1, "d": None}),
            json.dumps({"op": 10, "d": {"heartbeat_interval": 1}}),
            dispatch_packet("MESSAGE_CREATE", {"id": "x", "type": 7, "channel_id": "C1"}, seq=None),
        ):
            adapter._handle_payload(ws, packet)
            self.assertEqual(adapter.last_seq, 500, "s 为 null 时不能覆盖 last_seq")


# ----------------------------------------------------------------------
# 5) MESSAGE_CREATE 过滤矩阵
# ----------------------------------------------------------------------
class TestMessageFilter(unittest.TestCase):
    def setUp(self):
        self.hooks = RecordingHooks()
        self.adapter = DiscordAdapter(
            {"bot_token": "tok", "gateway_url": GATEWAY_URL}, self.hooks
        )
        self.ws = GatewayWS()
        self.adapter._handle_payload(self.ws, hello_packet())
        self.adapter._handle_payload(self.ws, ready_packet())
        self.adapter._handle_payload(self.ws, message_packet(content="预热", seq=99))
        self.assertEqual(len(self.hooks.inbounds), 1)  # 前置：一条消息能进来

    def feed(self, packet: str) -> bool:
        return self.adapter._handle_message_create(json.loads(packet)["d"])

    def assert_dropped(self, packet: str, reason: str):
        self.hooks.inbounds.clear()
        self.assertFalse(self.feed(packet), reason)

    def test_keeps_plain_human_message(self):
        self.hooks.inbounds.clear()
        self.assertTrue(self.feed(message_packet(author_id="U_HUMAN", content="在吗")))
        self.assertEqual(len(self.hooks.inbounds), 1)
        inbound = self.hooks.inbounds[0]
        self.assertIsInstance(inbound, Inbound)
        self.assertEqual(inbound.conversation_id, "channel:C1")
        self.assertEqual(inbound.text, "在吗")
        self.assertEqual(inbound.kind, "text")
        self.assertEqual(inbound.user_id, "U_HUMAN")
        self.assertEqual(inbound.message_id, "M1")
        self.assertEqual(inbound.platform, "discord")
        self.assertEqual(inbound.raw["channel_id"], "C1")

    def test_drops_own_message(self):
        self.assert_dropped(
            message_packet(author_id=BOT_USER_ID, bot=True), "自己发的必须丢，否则无限回环"
        )

    def test_keeps_other_bots_message(self):
        """``author.bot`` **不能**当过滤判据：别的 bot 的消息要留着。"""
        self.hooks.inbounds.clear()
        self.assertTrue(self.feed(message_packet(author_id="U_OTHER_BOT", bot=True)))
        self.assertEqual(len(self.hooks.inbounds), 1)

    def test_works_when_author_bot_key_absent(self):
        """``author.bot`` 是可选字段，可能整个键不存在。"""
        packet = message_packet(author_id="U_HUMAN")
        self.assertNotIn('"bot"', packet)
        self.hooks.inbounds.clear()
        self.assertTrue(self.feed(packet))
        self.assertEqual(len(self.hooks.inbounds), 1)

    def test_drops_non_default_message_type(self):
        for msg_type in (6, 7, 18, 19):
            self.assert_dropped(message_packet(msg_type=msg_type), f"type={msg_type} 是系统消息")

    def test_drops_crosspost(self):
        self.assert_dropped(message_packet(flags=1 << 1), "crosspost 会重复触发")

    def test_keeps_message_with_other_flags(self):
        self.hooks.inbounds.clear()
        self.assertTrue(self.feed(message_packet(flags=1 << 0)))
        self.assertEqual(len(self.hooks.inbounds), 1)

    def test_drops_webhook_message(self):
        self.assert_dropped(
            message_packet(webhook_id="999"), "webhook 消息的 author.id 不是真人"
        )

    def test_drops_empty_content(self):
        self.assert_dropped(message_packet(content=""), "纯附件消息没有正文")

    def test_drops_missing_channel(self):
        self.assert_dropped(
            dispatch_packet("MESSAGE_CREATE", {"id": "M", "type": 0, "author": {"id": "U"}}),
            "没有 channel_id 无法定位会话",
        )

    def test_drops_channel_outside_allowlist(self):
        adapter = DiscordAdapter(
            {
                "bot_token": "tok",
                "gateway_url": GATEWAY_URL,
                "allowed_chat_ids": ["C_OK"],
            },
            self.hooks,
        )
        self.assertFalse(
            adapter._handle_message_create(json.loads(message_packet(channel="C_OTHER"))["d"])
        )
        self.assertTrue(
            adapter._handle_message_create(json.loads(message_packet(channel="C_OK"))["d"])
        )

    def test_authorization_gate_runs_before_inbound(self):
        """授权判定必须早于产生 Inbound（否则能拿命令字绕过闸门）。"""
        calls: list = []
        adapter = DiscordAdapter(
            {"bot_token": "tok", "allowed_chat_ids": ["C_OK"]},
            RecordingHooks(calls),
        )
        original = adapter.admits

        def spy(principal):
            calls.append(("admits", str(principal)))
            return original(principal)

        adapter.admits = spy  # type: ignore[method-assign]
        self.assertFalse(
            adapter._handle_message_create(json.loads(message_packet(channel="C_BAD"))["d"])
        )
        self.assertEqual(calls, [("admits", "C_BAD")], "被拒的消息不得产生任何 Inbound")
        self.assertTrue(
            adapter._handle_message_create(json.loads(message_packet(channel="C_OK"))["d"])
        )
        self.assertEqual(
            calls[-2:], [("admits", "C_OK"), ("inbound", "channel:C_OK")], "先闸门后 Inbound"
        )


# ----------------------------------------------------------------------
# 6) 重连决策（op 7 / op 9 / close code）
# ----------------------------------------------------------------------
class TestReconnectDecisions(GatewayTestCase):
    def test_op7_reconnects_immediately_and_closes_socket(self):
        adapter, ws = self.connected()
        adapter._handle_payload(ws, ready_packet())
        action = adapter._handle_payload(ws, json.dumps({"op": 7, "d": None}))
        self.assertEqual(action, "reconnect")
        self.assertTrue(ws.closed, "收到 op 7 应自己先关掉，别等对端")
        self.assertEqual(ws.close_calls[0][0], discord_mod.GATEWAY_CLOSE_REQUESTED)
        self.assertEqual(adapter.session_id, "sess-1", "op 7 之后 session 仍可 Resume")

    def test_invalid_session_false_switches_to_identify(self):
        adapter, ws = self.connected()
        adapter._handle_payload(ws, ready_packet())
        action = adapter._handle_payload(ws, json.dumps({"op": 9, "d": False}))
        self.assertEqual(action, "reidentify")
        self.assertIsNone(adapter.session_id, "d=false 后 session 必须丢弃")

        ws2 = GatewayWS()
        adapter._handle_payload(ws2, hello_packet())
        self.assertEqual([pkt["op"] for pkt in ws2.ops()], [1, 2], "应重新 Identify")
        self.assertEqual(ws2.ops_of(6), [], "d=false 之后不能 Resume")

    def test_invalid_session_true_uses_resume(self):
        adapter, ws = self.connected()
        adapter._handle_payload(ws, ready_packet())
        adapter._handle_payload(ws, message_packet(content="让 last_seq 前进", seq=777))
        action = adapter._handle_payload(ws, json.dumps({"op": 9, "d": True}))
        self.assertEqual(action, "reconnect")
        self.assertEqual(adapter.session_id, "sess-1", "d=true 时 session 保留")

        ws2 = GatewayWS()
        adapter._handle_payload(ws2, hello_packet())
        ops = [pkt["op"] for pkt in ws2.ops()]
        self.assertEqual(ops, [1, 6], "HELLO → op1 → Resume")
        self.assertEqual(ws2.ops_of(2), [], "Resume 路径不能再发 Identify")
        resume = ws2.ops_of(6)[0]["d"]
        self.assertEqual(resume["session_id"], "sess-1")
        self.assertEqual(resume["seq"], 777, "Resume 必须带最近收到的 s")
        self.assertEqual(resume["token"], "tok-123")

    def test_resume_uses_resume_gateway_url(self):
        """Resume 必须用 READY 的 resume_gateway_url，不是初始 URL。"""
        adapter = self.adapter()
        urls: list[str] = []

        def factory(url, **kw):
            urls.append(url)
            return GatewayWS()

        adapter._ws_factory = factory
        adapter._handle_payload(GatewayWS(), ready_packet())
        adapter._resolve_gateway_url()
        adapter._session_id = "sess-1"
        adapter._resume_url = RESUME_URL
        self.assertEqual(len(urls), 0)
        url = adapter._resolve_gateway_url()
        self.assertTrue(url.startswith(RESUME_URL), url)
        self.assertIn(f"v={GATEWAY_API_VERSION}", url)
        self.assertIn("encoding=json", url)

    def test_close_code_decision_table(self):
        adapter, _ = self.connected()
        self.assertEqual(adapter._action_for_close(None), "reconnect")
        for code in (4000, 4001, 4002, 4003, 4005, 4007, 4008, 4009):
            with self.assertLogs("opencode_bridge.adapters.discord", level="INFO"):
                self.assertEqual(adapter._action_for_close(code), "reconnect", code)
        for code in (4004, 4010, 4011, 4012, 4013, 4014):
            with self.assertLogs("opencode_bridge.adapters.discord", level="ERROR"):
                self.assertEqual(adapter._action_for_close(code), "fatal", code)
        self.assertEqual(set(discord_mod.FATAL_CLOSE_CODES), {4004, 4010, 4011, 4012, 4013, 4014})


# ----------------------------------------------------------------------
# 7) 收包循环（线程级）
# ----------------------------------------------------------------------
class LoopTestCase(GatewayTestCase):
    def setUp(self):
        super().setUp()
        old = (discord_mod.RECONNECT_MIN, discord_mod.RECONNECT_MAX)
        discord_mod.RECONNECT_MIN = 0.01
        discord_mod.RECONNECT_MAX = 0.02

        def restore():
            discord_mod.RECONNECT_MIN, discord_mod.RECONNECT_MAX = old

        self.addCleanup(restore)

    def wait_for(self, predicate, timeout: float = 5.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.005)
        return predicate()

    def run_loop(self, adapter: DiscordAdapter, sockets: list[GatewayWS]) -> list[str]:
        """让 ``_inbound_loop`` 依次使用给定的假连接，返回建连时用过的 URL。"""
        urls: list[str] = []
        created: list[GatewayWS] = []

        def factory(url, **kw):
            urls.append(url)
            ws = sockets[len(created)] if len(created) < len(sockets) else sockets[-1]
            created.append(ws)
            return ws

        adapter._ws_factory = factory
        adapter.start()
        self.addCleanup(adapter.stop)
        return urls


class TestInboundLoop(LoopTestCase):
    def test_loop_reconnects_after_peer_close(self):
        hooks = RecordingHooks()
        adapter = self.adapter(hooks)
        first = GatewayWS(script=[hello_packet(), ready_packet(), message_packet(content="第一条")])
        second = GatewayWS(script=[message_packet(content="重连后")])
        self.run_loop(adapter, [first, second])
        self.assertTrue(self.wait_for(lambda: len(hooks.inbounds) >= 2))
        self.assertEqual(
            [ib.text for ib in hooks.inbounds][:2], ["第一条", "重连后"], "重连后要继续收事件"
        )
        self.assertTrue(first.closed, "断开后的旧连接必须被关掉")

    def test_op7_triggers_new_connection(self):
        hooks = RecordingHooks()
        adapter = self.adapter(hooks)
        first = GatewayWS(script=[hello_packet(), json.dumps({"op": 7, "d": None})])
        second = GatewayWS(script=[message_packet(content="op7 之后")])
        urls = self.run_loop(adapter, [first, second])
        self.assertTrue(self.wait_for(lambda: len(hooks.inbounds) >= 1))
        self.assertEqual(hooks.inbounds[0].text, "op7 之后")
        self.assertGreaterEqual(len(urls), 2, "op 7 后必须主动重连")
        self.assertTrue(first.closed)

    def test_fatal_close_code_stops_reconnecting(self):
        for code, needle in ((4004, "认证"), (4014, "后台")):
            with self.subTest(code=code):
                hooks = RecordingHooks()
                adapter = self.adapter(hooks)
                created: list[str] = []
                ws = GatewayWS(close_code=code, close_reason="fatal")

                def factory(url, **kw):
                    created.append(url)
                    return ws

                adapter._ws_factory = factory
                with self.assertLogs("opencode_bridge.adapters.discord", level="ERROR") as cap:
                    adapter.start()
                    self.assertTrue(self.wait_for(lambda: not adapter.running, 3.0))
                    adapter.stop()
                self.assertEqual(len(created), 1, f"close {code} 属于配错，不该重连")
                joined = "\n".join(cap.output)
                self.assertIn(str(code), joined)
                self.assertIn(needle, joined, "必须给出可执行的诊断")

    def test_non_fatal_close_code_reconnects(self):
        hooks = RecordingHooks()
        adapter = self.adapter(hooks)
        created: list[GatewayWS] = []

        def factory(url, **kw):
            ws = GatewayWS(close_code=4009, close_reason="session timeout")
            created.append(ws)
            return ws

        adapter._ws_factory = factory
        adapter.start()
        self.addCleanup(adapter.stop)
        self.assertTrue(self.wait_for(lambda: len(created) >= 2, 3.0), "4009 应可 Resume 重连")

    def test_missing_bot_token_does_not_connect(self):
        adapter = DiscordAdapter({"gateway_url": GATEWAY_URL}, RecordingHooks())
        adapter._ws_factory = lambda url, **kw: self.fail("不该建连")
        adapter.start()
        self.assertFalse(adapter.running)

    def test_stop_ends_thread_promptly(self):
        adapter = self.adapter()
        adapter._ws_factory = lambda url, **kw: BlockingGatewayWS()
        started = time.time()
        adapter.start()
        self.assertTrue(self.wait_for(lambda: adapter.running, 2.0))
        adapter.stop()
        elapsed = time.time() - started
        self.assertFalse(adapter.running, "stop() 必须让线程结束")
        self.assertLess(elapsed, 3.0, f"stop 用了 {elapsed:.2f}s，说明没先关 WS")

    def test_gateway_url_comes_from_rest_not_hardcoded(self):
        adapter = DiscordAdapter({"bot_token": "tok"}, RecordingHooks())
        calls: list[tuple] = []

        def fake_request(method, path, payload, *, timeout=None):
            calls.append((method, path, dict(payload)))
            return 200, {"url": "wss://rest-provided.example.test/gw"}

        adapter._request = fake_request  # type: ignore[method-assign]
        url = adapter._resolve_gateway_url()
        self.assertEqual(calls, [("GET", "gateway/bot", {})], "URL 必须问 REST 要")
        self.assertTrue(url.startswith("wss://rest-provided.example.test/gw"), url)
        self.assertIn("?v=10&encoding=json", url)
        self.assertNotIn("compress", url, "不要加 compress")
        # 第二次走缓存，不再打 REST
        self.assertEqual(adapter._resolve_gateway_url(), url)
        self.assertEqual(len(calls), 1)

    def test_gateway_url_appends_to_existing_query(self):
        adapter = DiscordAdapter(
            {"bot_token": "tok", "gateway_url": "wss://x.test/gw?a=1"}, RecordingHooks()
        )
        self.assertEqual(adapter._resolve_gateway_url(), "wss://x.test/gw?a=1&v=10&encoding=json")

    def test_gateway_bot_failure_is_retryable(self):
        adapter = DiscordAdapter({"bot_token": "tok"}, RecordingHooks())
        adapter._request = lambda *a, **k: (401, {"message": "401: Unauthorized"})  # type: ignore[method-assign]
        with self.assertRaises(RuntimeError) as ctx:
            adapter._resolve_gateway_url()
        self.assertIn("gateway/bot", str(ctx.exception))

    def test_oversized_payload_is_refused_not_sent(self):
        adapter, ws = self.connected()
        ws.sent.clear()
        sent = adapter._send_op(ws, 0, {"blob": "x" * discord_mod.GATEWAY_MAX_PAYLOAD})
        self.assertFalse(sent)
        self.assertEqual(ws.sent, [], "超过 4096 字节的 payload 不能发")
        self.assertEqual(ws.close_calls[0][0], 4002)

    def test_exception_in_inbound_hook_does_not_kill_loop(self):
        class ExplodingHooks(RecordingHooks):
            def on_inbound(self, inbound):
                raise RuntimeError("上层炸了")

        hooks = ExplodingHooks()
        adapter = self.adapter(hooks)
        ws = GatewayWS(script=[hello_packet(), message_packet(content="炸"), None])
        adapter._ws_factory = lambda url, **kw: ws
        adapter.start()
        self.addCleanup(adapter.stop)
        self.assertTrue(self.wait_for(lambda: ws.closed, 3.0), "钩子异常不能让线程退出")


# ----------------------------------------------------------------------
# 8) 出站未被入站改动波及（回归）
# ----------------------------------------------------------------------
class TestOutboundUnaffected(unittest.TestCase):
    def test_send_and_edit_still_work(self):
        adapter = DiscordAdapter({"bot_token": "t"}, RecordingHooks())
        calls: list[tuple] = []

        def fake_request(method, path, payload, *, timeout=None):
            calls.append((method, path, dict(payload)))
            return 200, {"id": "999"}

        adapter._request = fake_request  # type: ignore[method-assign]
        handle = adapter.send(Outbound(conversation_id="channel:42", text="hi"))
        self.assertIsNotNone(handle)
        self.assertEqual(handle.message_id, "999")
        self.assertEqual(calls[-1][1], "channels/42/messages")
        self.assertIs(adapter.edit(handle, Outbound("channel:42", "v2")), True)
        self.assertEqual(calls[-1][0], "PATCH")
        self.assertEqual(calls[-1][1], "channels/42/messages/999")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
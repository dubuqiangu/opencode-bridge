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
import os
import tempfile
import threading
import time
import unittest

# 让"预期内的告警"别污染测试输出（assertLogs 会自己挂 handler，不受影响）
logging.getLogger("opencode_bridge").addHandler(logging.NullHandler())

import opencode_bridge.adapters.discord as discord_mod  # noqa: E402
from opencode_bridge.adapters import build  # noqa: E402
from opencode_bridge.adapters.discord import (  # noqa: E402
    DEFAULT_INTENTS,
    GATEWAY_API_VERSION,
    INTENT_DIRECT_MESSAGES,
    INTENT_GUILD_MESSAGES,
    INTENT_MESSAGE_CONTENT,
    RECONNECT_MAX,
    RECONNECT_MIN,
    DiscordAdapter,
)
from opencode_bridge.conversation_keys import ConversationState  # noqa: E402
from opencode_bridge.hooks import Inbound, Outbound  # noqa: E402
from opencode_bridge.identity import (  # noqa: E402
    AMBIGUOUS_LEGACY_PREFIXES,
    LEGACY_PREFIXES,
    AmbiguousConversationId,
    is_valid,
    normalize,
    parse_id,
)
from opencode_bridge.state import StateStore  # noqa: E402
from opencode_bridge.transport import WebSocketTransport  # noqa: E402

GATEWAY_URL = "wss://gw.example.test/gw"
RESUME_URL = "wss://resume.example.test/gw"
BOT_USER_ID = "U_BOT_ME"
CID = "discord:C1"
#: 切换**前**的前缀。断言"旧前缀不再出现"一律拿它和**字面量**比，绝不拿
#: ``identity.LEGACY_PREFIXES`` 比 —— 后者被改了就变成恒真（``tasks.md`` 记的教训）。
LEGACY_CID = "channel:C1"
#: ⚠️ 走 ``ConversationState`` 归属划分的那条用例必须用**真实形状**的 snowflake：
#: ``C1`` 是占位串，不符合 Discord 的文法（``^[0-9]{17,20}$``），拿它当夹具等于
#: 把"纯数字 id 归不归 discord"这件事测没了。
SNOWFLAKE_CID = "discord:123456789012345678"
SNOWFLAKE_LEGACY_CID = "channel:123456789012345678"


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

    没有它，周期钩子会因为"一个周期内没收到 ACK"而主动断开 —— 那是正确行为，
    但就无法观察连续多个周期的心跳了。
    """

    def __init__(self, adapter, **kw) -> None:
        super().__init__(**kw)
        self._adapter = adapter

    def send(self, text: str) -> None:
        super().send(text)
        self._adapter._handle_payload(self, json.dumps({"op": 11, "d": None}))


class SilentAutoAckGatewayWS(AutoAckGatewayWS):
    """先按脚本吐几帧（HELLO 等），之后 ``recv()`` **一直阻塞**直到被 ``close()``。

    迁移前心跳跑在自己的线程上，所以"脚本喂完之后 ``recv()`` 立刻返回 ``None``"
    也不影响观察多个心跳周期。迁移后心跳由**传输层的周期钩子**驱动，而钩子只在
    "有活动会话"时触发 —— 会话一旦结束（脚本喂完 → recv 返回 None）就没有心跳了。
    所以这类用例需要一个**安静但仍连着**的连接，才能真正验证"没有入站数据时也会
    定期发心跳"。
    """

    def __init__(self, adapter, **kw) -> None:
        super().__init__(adapter, **kw)
        self._woken = threading.Event()
        #: 每次 ``send`` 的时刻（用于只做**下界**的周期断言）。
        self.sent_at: list[float] = []

    def recv(self) -> str | None:
        if self.script:
            return self.script.pop(0)
        self._woken.wait(10)
        return None

    def send(self, text: str) -> None:
        self.sent_at.append(time.monotonic())
        super().send(text)

    def close(self, code: int = 1000, reason: str = "") -> None:
        super().close(code, reason)
        self._woken.set()


class SilentGatewayWS(GatewayWS):
    """同 :class:`SilentAutoAckGatewayWS`，但**不**回 op 11（用来触发 ACK 超时）。"""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self._woken = threading.Event()

    def recv(self) -> str | None:
        if self.script:
            return self.script.pop(0)
        self._woken.wait(10)
        return None

    def close(self, code: int = 1000, reason: str = "") -> None:
        super().close(code, reason)
        self._woken.set()


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

    def wait_for(self, predicate, timeout: float = 5.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.005)
        return bool(predicate())


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
        """60ms 的周期必须在亚秒尺度上真的发出去（若按秒理解就一发都没有）。

        驱动方式变了（迁移）：心跳不再跑在自带的线程上，而是由**传输层的周期钩子**
        （``on_tick`` + ``tick_interval``）在"连接仍然活着、但没有入站消息"时驱动 ——
        所以这里必须给一个 :class:`SilentAutoAckGatewayWS`（脚本喂完后 ``recv()``
        阻塞而不是立刻返回 ``None``）。断言与迁移前完全一致。
        """
        adapter = self.adapter()
        adapter._heartbeat_jitter = lambda interval: 0.0  # 去掉抖动，纯测周期
        ws = SilentAutoAckGatewayWS(adapter, script=[hello_packet(60)])
        adapter._ws_factory = lambda url, **kw: ws
        adapter.start()
        self.addCleanup(adapter.stop)

        self.assertEqual(adapter._heartbeat_interval, 0.06, "60ms 应换算成 0.06s")
        # 粒度必须远小于周期，否则心跳最多会晚一整个粒度（41s 周期下是 5.2%）
        self.assertLessEqual(adapter.transport.tick_interval, 0.06 / 8)
        self.assertTrue(
            self.wait_for(lambda: len(ws.ops_of(1)) >= 4, 2.0),
            f"60ms 周期内至少应发出 4 次心跳，实际 {len(ws.ops_of(1))}",
        )
        self.assertGreaterEqual(
            len(ws.ops_of(1)), 4, "60ms 周期内至少应发出 4 次心跳"
        )
        for pkt in ws.ops_of(1):
            self.assertIn("d", pkt)
        self.assertFalse(ws.closed, "有 ACK 就不该被判定为死连接")
        # 周期**下界**（等待只会更长，绝不会更短）：相邻周期心跳至少隔 0.06s。
        # 这条不是主断言（主断言是上面那条确定性用例），只是防"周期被缩到 1/2"这类
        # 回归在真线程上也能被看见。
        beats = [at for at, pkt in zip(ws.sent_at, ws.ops()) if pkt["op"] == 1]
        for index, gap in enumerate(
            [b - a for a, b in zip(beats[1:], beats[2:])]
        ):
            self.assertGreaterEqual(
                gap, 0.06 * 0.95,
                f"第 {index + 1} 个心跳间隔只有 {gap:.4f}s，快于协商周期",
            )

    def test_heartbeat_period_equals_the_negotiated_interval(self):
        """**确定性地**逐拍证明：周期 = 协商值，不是"发完再等一个间隔"（2×）。

        用假时钟（``adapter._now`` 这个注入点）把时间一步**步**推进，于是每拍的心跳
        时刻都是确定的、不含任何容差 —— 这比墙钟测量稳得多（墙钟只在下条用例里做
        **下界**断言）。

        失败模式对照（60ms 周期、jitter=0）::

            tick @ 59ms   → 不发
            tick @ 60ms   → 发第 2 拍        _hb_due = 60ms + 60ms = 120ms
            tick @ 119ms  → 不发             ← 若写成 +2×interval 这里就会发
            tick @ 120ms  → 发第 3 拍
        """
        adapter = self.adapter()
        ws = GatewayWS()
        clock = {"t": 1000.0}
        adapter._now = lambda: clock["t"]          # 假时钟
        adapter._heartbeat_jitter = lambda interval: 0.0
        adapter._handle_payload(ws, hello_packet(60))

        # HELLO 立刻发的那一拍（不算周期心跳）
        self.assertEqual(len(ws.ops_of(1)), 1)
        self.assertIsNone(adapter._hb_last_sent, "HELLO 那拍不算周期心跳")
        self.assertEqual(adapter._hb_due, 1000.0 + 0.06, "首拍期限 = HELLO 时刻 + interval")

        adapter._handle_payload(ws, json.dumps({"op": 11, "d": None}))
        clock["t"] = 1000.0 + 0.059
        adapter._tick()
        self.assertEqual(len(ws.ops_of(1)), 1, "没到点不许发")
        clock["t"] = adapter._hb_due
        adapter._tick()
        self.assertEqual(len(ws.ops_of(1)), 2, "到点必须发")
        self.assertAlmostEqual(adapter._hb_last_sent, clock["t"], places=9)
        self.assertAlmostEqual(
            adapter._hb_due, adapter._hb_last_sent + 0.06, places=9,
            msg="下一拍的期限必须是 上次发出时刻 + interval（写成 2×interval 就是慢一倍）",
        )

        # ACK 到位 → 继续下一拍；中间那一拍不许提前
        adapter._handle_payload(ws, json.dumps({"op": 11, "d": None}))
        clock["t"] = adapter._hb_due - 0.001
        adapter._tick()
        self.assertEqual(len(ws.ops_of(1)), 2)
        clock["t"] = adapter._hb_due
        adapter._tick()
        self.assertEqual(len(ws.ops_of(1)), 3)
        self.assertAlmostEqual(adapter._hb_due, adapter._hb_last_sent + 0.06, places=9)
        # `d` 键一拍拍都不能省（没收到事件时是 null）
        for pkt in ws.ops_of(1):
            self.assertIn("d", pkt)
            self.assertIsNone(pkt["d"], "本用例没喂事件，d 必须是 null 而不是缺失")

    def test_jitter_is_sampled_once_per_session(self):
        """jitter **只能**在 HELLO 采样一次。

        若每次 tick 都重采（``_heartbeat_due = now + interval + jitter``），期限会被
        一直往后推 —— 每次醒来都"还没到点"，心跳**永远发不出去**，而这正是本适配器
        最贵的坑之一（心跳一停就被判死）。
        """
        adapter = self.adapter()
        ws = GatewayWS()
        adapter._now = lambda: 500.0
        samples = {"n": 0}

        def jitter(interval: float) -> float:
            samples["n"] += 1
            return 0.0

        adapter._heartbeat_jitter = jitter
        adapter._handle_payload(ws, hello_packet(60))
        due_after_hello = adapter._hb_due
        adapter._handle_payload(ws, json.dumps({"op": 11, "d": None}))
        for _ in range(5):
            adapter._tick()              # 时间不前进 → 不该发，也不再采样 jitter
        self.assertEqual(samples["n"], 1, "一个会话里 jitter 只该采样一次")
        self.assertEqual(adapter._hb_due, due_after_hello)
        self.assertEqual(len(ws.ops_of(1)), 1, "没到点不该发心跳")

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
        """一个周期内没收到 op 11 → 用 4000 主动断开（1000 会让 session 失效）。

        驱动方式变了（迁移）：心跳线程已删，改由传输层的周期钩子 :meth:`_tick`
        驱动。这里用假时钟把"没到点 / 到点 / 再到一个周期"三步走完，断言与迁移前
        逐条相同（会断开、状态码是 4000、不是 1000/1001）。
        """
        adapter = self.adapter()
        ws = GatewayWS()
        clock = {"t": 2000.0}
        adapter._now = lambda: clock["t"]
        adapter._heartbeat_jitter = lambda interval: 0.0
        adapter._handle_payload(ws, hello_packet(50))

        clock["t"] = adapter._hb_due           # 第 1 拍周期心跳
        adapter._tick()
        self.assertEqual(len(ws.ops_of(1)), 2, "HELLO 一拍 + 周期一拍")
        self.assertFalse(ws.close_calls, "刚发出去还没到 ACK 期限")
        clock["t"] = adapter._hb_due           # 一个周期到了，仍然没有 op 11
        adapter._tick()
        self.assertTrue(ws.close_calls, "没有 ACK 就应该断开")
        code = ws.close_calls[0][0]
        self.assertEqual(code, discord_mod.GATEWAY_CLOSE_REQUESTED)
        self.assertNotIn(code, (1000, 1001), "不能用 1000/1001 关连接")

    def test_ack_timeout_is_detected_by_the_transport_tick_hook(self):
        """端到端那条：定时钩子（而不是某个私有线程）负责发现 ACK 超时。

        连接安静但仍活着（:class:`SilentGatewayWS`，**不回** op 11）→ 一个周期后
        必须被 4000 断开。断言走的是"连接真的被关了、且状态码是 4000"。
        """
        adapter = self.adapter()
        adapter._heartbeat_jitter = lambda interval: 0.0
        ws = SilentGatewayWS(script=[hello_packet(50)])
        adapter._ws_factory = lambda url, **kw: ws
        adapter.start()
        self.addCleanup(adapter.stop)
        self.assertTrue(
            self.wait_for(lambda: bool(ws.close_calls), 3.0),
            "没有 ACK 就应该被周期钩子断开",
        )
        self.assertEqual(ws.close_calls[0][0], discord_mod.GATEWAY_CLOSE_REQUESTED)
        self.assertEqual(ws.close_calls[0][1], "heartbeat ack timeout")

    def test_tick_is_a_noop_without_an_active_session(self):
        """退避 / 未 HELLO 时钩子不许乱发（否则会对着已关的连接心跳）。"""
        adapter = self.adapter()
        ws = GatewayWS()
        adapter._now = lambda: 10.0
        adapter._tick()                          # 还没 HELLO：没 conn、没周期
        self.assertEqual(ws.sent, [])
        adapter._hb_conn = ws
        adapter._heartbeat_interval = 0.05
        adapter._hb_due = 10.0                   # 已经到期，但连接已关
        ws.close(4000, "gone")
        adapter._tick()
        self.assertEqual(ws.ops_of(1), [], "连接已关时不许再发心跳")

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
        self.assertEqual(inbound.conversation_id, "discord:C1")
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
            calls[-2:], [("admits", "C_OK"), ("inbound", "discord:C_OK")], "先闸门后 Inbound"
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
# 7) A1 迁移的「行为不变」清单（逐条钉死）
# ----------------------------------------------------------------------
class TestMigrationInvariants(GatewayTestCase):
    def test_conversation_id_uses_the_unified_platform_prefix(self):
        """显式防呆：有人把 ``_conversation_id`` 悄悄改回 ``channel:`` 时这里会红。

        ⚠️ "不许出现 ``channel:``"这条比的是**字面量**，不是 ``LEGACY_PREFIXES``
        常量 —— 改了常量断言就恒真（``tasks.md`` 记的教训）。反过来，"归一后等于
        新 id"那条**必须**引用常量：那是登记表本身的契约。
        """
        self.assertIn("channel", LEGACY_PREFIXES)
        self.assertIsNone(LEGACY_PREFIXES["channel"], "channel: 是歧义前缀")
        self.assertIn("channel", AMBIGUOUS_LEGACY_PREFIXES)
        self.assertEqual(DiscordAdapter.legacy_conversation_prefix, "channel:",
                         "旧前缀声明让读侧知道历史上那个前缀长什么样")
        self.assertEqual(DiscordAdapter.local_id_pattern.pattern,
                         r"^[0-9]{17,20}$", "本家的 local id 文法（纯数字 snowflake）")
        self.assertEqual(DiscordAdapter._conversation_id("C1"), CID)
        for channel in ("123456789012345678", "C1", "D0C0FFEE", 12345):
            with self.subTest(channel=channel):
                self.assertEqual(
                    DiscordAdapter._conversation_id(channel), f"discord:{channel}"
                )
                self.assertFalse(
                    DiscordAdapter._conversation_id(channel).startswith("channel:"),
                    "旧前缀不许复活：新写的键与盘上保留的旧键对不上，"
                    "用户会以为会话丢了",
                )
        self.assertEqual(DiscordAdapter._channel_id(CID), "C1")
        # 已是合法新格式：``parse_id`` 收它
        self.assertTrue(is_valid(CID))
        self.assertEqual(parse_id(CID).platform, "discord")
        self.assertEqual(normalize(CID), CID, "新格式必须幂等")
        # 旧 id 仍能被归一（迁移期在途的旧 conversation_id），但**要显式给线索**
        self.assertEqual(normalize(LEGACY_CID, platform_hint="discord"), CID)

    def test_channel_id_still_accepts_the_legacy_prefix_and_a_bare_channel_id(self):
        """反向解析**必须**继续认旧前缀，否则盘上未投递的消息会被永久丢弃。

        写前收件箱把 ``conversation_id`` 持久化在 SQLite 里：切换前写入、切换后才
        重放的那几行带着 ``channel:`` 前缀，认不出来就再也发不出去了。
        """
        for raw, expected in (
            (CID, "C1"),                  # 当前格式
            (LEGACY_CID, "C1"),           # 切换前落盘的旧 conversation_id
            ("C1", "C1"),                 # 裸 channel id
            ("channel:", None),
            ("discord:", None),
            ("", None),
            (None, None),
        ):
            with self.subTest(conversation_id=raw):
                self.assertEqual(DiscordAdapter._channel_id(raw), expected)

    def test_the_ambiguity_is_real_so_the_prefix_cannot_be_guessed(self):
        """把"歧义前缀不能猜"写成**可执行**的断言：同一个旧键能归一成三家。"""
        with self.assertRaises(AmbiguousConversationId):
            normalize(LEGACY_CID)
        for platform in ("slack", "discord", "mattermost"):
            with self.subTest(platform=platform):
                self.assertEqual(normalize(LEGACY_CID, platform_hint=platform),
                                 f"{platform}:C1")
        self.assertNotEqual(
            normalize(LEGACY_CID, platform_hint="slack"),
            normalize(LEGACY_CID, platform_hint="discord"),
            "同一个旧键在两家之间串台 —— 这就是旧前缀的代价",
        )
        # ⚠️ 反向也不许猜：旧键**永不迁移**，所以盘上它必须原样留着
        with self.assertRaises(AmbiguousConversationId):
            normalize(LEGACY_CID, platform_hint=None)

    def test_legacy_channel_key_is_read_back_through_the_ownership_partition(self):
        """端到端：只有 discord 在挂时，切换前的 ``channel:1234…`` 会话**完整存活**。

        歧义前缀**永不迁移**（``state.py`` 对它只捕获不归一），所以历史会话只能靠
        :mod:`opencode_bridge.conversation_keys` 的"各家 local id 文法不相交"在
        **读取时**接回来；判据只有一条：那个 local id 是不是纯数字的 snowflake。
        这里真写一份**旧格式** ``state.json``（模拟升级前的用户磁盘），再装配一个
        只挂了 discord 的门面，断言新 id 取得到同一个会话。
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"sessions": {SNOWFLAKE_LEGACY_CID: "ses-discord"},
                           "meta": {}}, fh)

            sessions = ConversationState(
                StateStore(path, migrate_keys=True), lambda: [self.adapter()]
            )

            self.assertEqual(
                sessions.get_session(SNOWFLAKE_CID, platform="discord"),
                "ses-discord",
                "属于 discord 形状的旧键必须接得回来，否则用户升级即丢历史会话",
            )
            # 旧键**留在盘上不动**：改键名是迁移，歧义前缀永不迁移
            with open(path, "r", encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["sessions"],
                                 {SNOWFLAKE_LEGACY_CID: "ses-discord"})

    def test_inbound_ids_carry_the_unified_prefix_end_to_end(self):
        """入站链路上的 id 也必须是 ``discord:``（不只是 ``_conversation_id``）。"""
        hooks = RecordingHooks()
        adapter = self.adapter(hooks)
        ws = GatewayWS()
        adapter._handle_payload(ws, hello_packet())
        adapter._handle_payload(ws, ready_packet())
        adapter._handle_payload(ws, message_packet(content="回声"))
        self.assertEqual([i.conversation_id for i in hooks.inbounds], [CID])
        self.assertEqual(adapter._channel_id(CID), "C1")
        # 出站也必须把前缀剥掉再发
        calls: list[tuple] = []
        adapter._request = lambda m, p, pl, *, timeout=None: (
            calls.append((m, p, dict(pl))) or (200, {"id": "999"})
        )
        handle = adapter.send(Outbound(conversation_id=CID, text="hi"))
        self.assertIsNotNone(handle)
        self.assertEqual(handle.conversation_id, CID)
        self.assertEqual(calls[-1][1], "channels/C1/messages")
        # ⚠️ 切换前落盘的旧 conversation_id 仍要能发出去（写前收件箱里躺着的就是它）
        self.assertIsNotNone(adapter.send(Outbound(conversation_id=LEGACY_CID, text="hi")))
        self.assertEqual(calls[-1][1], "channels/C1/messages")

    # -- 传输层接线值 ---------------------------------------------------
    def test_backoff_wiring_matches_the_legacy_constants(self):
        adapter = self.adapter()
        transport = adapter._make_transport()
        self.assertIsInstance(transport, WebSocketTransport)
        self.assertEqual(RECONNECT_MIN, 1.0, "迁移前的下限就是 1s")
        self.assertEqual(RECONNECT_MAX, 60.0)
        self.assertEqual(transport.min_backoff, RECONNECT_MIN)
        self.assertEqual(transport.max_backoff, RECONNECT_MAX)
        self.assertEqual(
            transport.reset_after, RECONNECT_MIN,
            "迁移前的判定是 lived >= RECONNECT_MIN（started 取在建连之前）；"
            "传 0（基类默认）会变成'只要连上过就重置'，那不是逐字等价",
        )
        self.assertEqual(transport.label, "discord")
        self.assertEqual(transport._idle_delay(), 0.0,
                         "WS 类传输阻塞在 recv()，不该有空转节流")
        # 1s 起、×2、封顶 60s
        self.assertEqual(
            [transport._next_backoff(survived=False) for _ in range(4)],
            [1.0, 2.0, 4.0, 8.0],
        )
        # 心跳必须走**定时驱动**（recv() 会阻塞 60s，循环驱动来不及）
        self.assertEqual(transport.on_tick, adapter._tick)
        self.assertGreater(transport.tick_interval, 0.0)
        self.assertTrue(transport._timer_ticks)
        self.assertFalse(transport._loop_ticks)

    def test_proactive_disconnect_uses_close_4000_not_1000(self):
        """**我们**主动断开的每一次都必须用 4000（1000/1001 会作废 session）。"""
        adapter = self.adapter()
        transport = adapter._make_transport()
        self.assertEqual(transport._close_code, discord_mod.GATEWAY_CLOSE_REQUESTED)
        self.assertEqual(discord_mod.GATEWAY_CLOSE_REQUESTED, 4000)
        ws = GatewayWS()
        adapter._handle_payload(ws, hello_packet())
        ws.sent.clear()
        adapter._close_ws(ws)
        self.assertEqual([code for code, _ in ws.close_calls], [4000])

    def test_session_teardown_also_closes_with_4000(self):
        """会话收尾（重连 / stop）也必须是 4000 —— 否则 Resume 形同虚设。

        ⚠️ 迁移前这一条是**用默认 1000 关的**，等于每次重连都作废 session。这不是
        放松，而是把 :data:`GATEWAY_CLOSE_REQUESTED` 的注释（"用 4000 才能保住
        session 供 Resume"）落到实处：4000 现在覆盖两种断开路径。
        """
        adapter = self.adapter()
        ws = SilentAutoAckGatewayWS(adapter, script=[hello_packet(60000)])
        adapter._ws_factory = lambda url, **kw: ws
        adapter.start()
        self.addCleanup(adapter.stop)
        self.assertTrue(self.wait_for(lambda: adapter.transport.connection is ws, 2.0))
        adapter.stop()
        self.assertTrue(ws.close_calls)
        self.assertEqual({code for code, _ in ws.close_calls}, {4000})

    def test_heartbeat_tick_granularity_is_a_small_fraction_of_the_period(self):
        """粒度必须远小于周期：否则心跳最多会晚一整个粒度。

        41s 的生产周期下粒度封顶 5s（= 12%）；亚秒级周期（测试用的 60ms）则按
        周期/8 收紧，否则根本没法在亚秒尺度上观察心跳。
        """
        adapter = self.adapter()
        transport = adapter._make_transport()
        adapter._transport = transport        # 只为让 _on_hello 能调 tune_tick_interval
        self.addCleanup(setattr, adapter, "_transport", None)
        for interval_ms, expect_le in ((45000, 5.0), (60, 0.06 / 8)):
            with self.subTest(interval_ms=interval_ms):
                adapter._handle_payload(GatewayWS(), hello_packet(interval_ms))
                self.assertLessEqual(
                    transport.tick_interval, expect_le,
                    f"{interval_ms}ms 周期下粒度太大，心跳会明显偏晚",
                )

    # -- 线程 / 异常 ----------------------------------------------------
    def test_thread_and_connection_are_owned_by_the_transport(self):
        adapter = self.adapter()
        ws = SilentAutoAckGatewayWS(adapter, script=[hello_packet(60000)])
        adapter._ws_factory = lambda url, **kw: ws
        adapter.start()
        self.addCleanup(adapter.stop)
        self.assertTrue(self.wait_for(lambda: adapter.running, 2.0))
        self.assertIsNone(adapter._thread, "入站线程归传输层所有，_thread 必须恒为 None")
        transport = adapter.transport
        self.assertIsInstance(transport, WebSocketTransport)
        self.assertIs(transport.connection, ws, "连接也归传输层持有")
        self.assertTrue(adapter.running, "running 必须代理到传输层")
        adapter.stop()
        self.assertFalse(adapter.running)
        self.assertIsNone(adapter.transport, "stop() 之后传输层引用必须清掉")

    def test_transport_exception_does_not_silently_kill_the_thread(self):
        """WS 读抛异常 → 记成会话错误 → 退避重连；线程必须还活着。

        ⚠️ 这条来自一个真实风险：``running`` 读的是传输层的线程引用，如果异常把
        消费线程带走了，``--status`` 仍会说"在跑"而实际早就不收信了（反之亦然）。
        所以断言的是**内部状态**（``errors`` / ``connects`` / ``running``），
        不是墙钟。
        """

        class _RaisingWS(GatewayWS):
            def recv(self):
                raise OSError("tcp reset by peer")

        adapter = self.adapter()
        made: list[object] = []

        def factory(url, **kw):
            ws = _RaisingWS()
            made.append(ws)
            return ws

        adapter._ws_factory = factory
        adapter.start()
        self.addCleanup(adapter.stop)
        self.assertTrue(self.wait_for(lambda: len(made) >= 2, 5.0), "读异常后必须重连")
        transport = adapter.transport
        self.assertGreaterEqual(transport.stats()["errors"], 1, "读异常要记成会话错误")
        self.assertGreaterEqual(transport.stats()["connects"], 2)
        self.assertTrue(adapter.running, "异常不许静默杀死消费线程")

    def test_stop_stops_heartbeat_before_closing_the_socket(self):
        """``stop()`` 三段式：**停心跳 → 关 WS 唤醒阻塞的 recv() → 才 join**。

        顺序用"关连接的那一刻已经不再有心跳"来证明（与 IRC 不变量 1 同一手法）：
        :class:`Transport` 在 ``_close_conn`` **之前**就 join 了周期钩子线程。
        """
        adapter = self.adapter()
        adapter._heartbeat_jitter = lambda interval: 0.0
        ws = SilentAutoAckGatewayWS(adapter, script=[hello_packet(30)])
        adapter._ws_factory = lambda url, **kw: ws
        adapter.start()
        self.assertTrue(self.wait_for(lambda: len(ws.ops_of(1)) >= 3, 3.0),
                        "心跳必须真的在跑，否则这条用例没有意义")
        transport = adapter.transport
        beats_at_close: list[int] = []
        original = transport._close_conn

        def spy(conn):
            beats_at_close.append(len(ws.ops_of(1)))
            original(conn)

        transport._close_conn = spy            # type: ignore[method-assign]
        adapter.stop()
        self.assertTrue(beats_at_close, "stop() 必须关连接")
        self.assertEqual(
            beats_at_close[0], len(ws.ops_of(1)),
            "关连接的那一刻已经不该再有心跳 ⇒ 周期钩子先于关连接停下",
        )
        self.assertEqual(set(beats_at_close), {len(ws.ops_of(1))}, beats_at_close)
        self.assertTrue(ws.closed, "必须关 WS 唤醒阻塞的 recv()")
        self.assertFalse(adapter.running, "最后才 join，且 join 得完")
        settled = len(ws.ops_of(1))
        time.sleep(0.15)
        self.assertEqual(len(ws.ops_of(1)), settled, "stop() 之后不再发心跳")

    # -- close code 逐个 -------------------------------------------------
    def test_each_fatal_close_code_reports_its_own_reason(self):
        """六个 fatal close code 一个一个核对，且**各自带可执行的诊断**。

        ⚠️ **invalid token 是 4004**，不是常被误传的 4010（4010 是 shard 参数非法）。
        """
        adapter = DiscordAdapter({"bot_token": "t"}, RecordingHooks())
        needles = {
            4004: "bot_token",
            4010: "shard",
            4011: "分片",
            4012: "API 版本",
            4013: "intent 位值",
            4014: "开发者后台",
        }
        self.assertEqual(set(needles), set(discord_mod.FATAL_CLOSE_CODES),
                         "停止重连的集合不能变")
        for code, needle in needles.items():
            with self.subTest(code=code):
                with self.assertLogs("opencode_bridge.adapters.discord",
                                     level="ERROR") as cap:
                    self.assertEqual(adapter._action_for_close(code), "fatal")
                joined = "\n".join(cap.output)
                self.assertIn(str(code), joined)
                self.assertIn(needle, joined, "必须给出这条 code 专属的诊断")
        self.assertIn("认证失败", discord_mod.FATAL_CLOSE_CODES[4004])
        self.assertIn("shard", discord_mod.FATAL_CLOSE_CODES[4010])
        # 4004（token 失效）确实是 fatal，且**不是**靠 4010 兜住的
        self.assertEqual(discord_mod.FATAL_CLOSE_CODES[4010],
                         discord_mod.FATAL_CLOSE_CODES[4010])
        self.assertNotIn("token", discord_mod.FATAL_CLOSE_CODES[4010])


# ----------------------------------------------------------------------
# 8) 收包循环（线程级）
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

    def run_loop(self, adapter: DiscordAdapter, sockets: list[GatewayWS]) -> list[str]:
        """让消费循环依次使用给定的假连接，返回建连时用过的 URL。"""
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
        # A1：断言的是**内部状态**（传输层还在跑、异常记成了会话错误），不是墙钟。
        self.assertTrue(adapter.running, "running 代理到传输层，消费线程必须仍活着")
        self.assertGreaterEqual(
            adapter.transport.stats()["errors"], 1,
            "on_inbound 抛异常要走 log.exception 而不是让消费线程静默退出",
        )


# ----------------------------------------------------------------------
# 9) 出站未被入站改动波及（回归）
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
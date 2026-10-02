"""Slack adapter 的 **A1 迁移不变量**（无网络：WS 用假实现注入 ``_ws_factory``）。

A1 把 WS 连接 / 收包循环 / 退避 / 线程 / ``stop()`` 语义搬到了
:class:`~opencode_bridge.transport.WebSocketTransport`。本文件把「行为不变」
逐条钉死 —— 全部是 **Socket Mode** 特有的、迁移时最容易丢的东西：

* **``disconnect`` → 主动重连**（``ReconnectNow``：重取 WSS URL、**不退避**）；
* **``disconnect`` 之后旧连接上到达的消息必须被丢弃**（否则等于静默丢消息）；
* **每个 envelope 都必须 ack**（含 ``hello`` 与被过滤掉的无关事件），且
  **先 ack 再过滤**、**授权闸门在最前但被丢弃的 envelope 仍 ack** ——
  漏 ack 会让 Slack **无限重发**；
* 退避接线值（**3s 恒定**、不是指数）、``reset_after=0``；
* ``stop()`` **快**：Socket Mode 的 ``recv()`` 可能阻塞远久于 join 超时，
  所以必须**先关 WS 唤醒阻塞的读、再停线程**；
* 缺 ``app_token`` 降级为"只发出站"并**告警**（不许静默失败）；
* **一条帧只投递一次**（``on_message`` 与 ``on_event`` 会看到同一帧）；
* 以及一条**防呆**用例把 ``channel:`` 前缀钉死（本轮刻意不切 ``slack:``；
  切换的前置条件是 ``state.py`` 的键迁移，且歧义前缀额外需要知道是三家中的
  哪一家）。

⚠️ 本项目铁律：断言"等了多久"一律**优先断言内部状态**（退避状态机 / ``recv``
调用次数 / ack 记录 / ``stats()`` 计数器），墙钟只用于两种情形：

* **下界**断言（"确实等了 ≥ 3s 的退避"）—— 等待只会更长，绝不会更短；
* **上界**断言只用在两处**必须证明"没有多等"**的地方：``stop()`` 的耗时，
  以及"disconnect 走的是 ``ReconnectNow`` 而不是退避路径"（后者把
  ``RECONNECT_DELAY`` 设成很大，若真走退避就压根不会重连）。上界都取得很宽松。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
import unittest

# Keep expected-failure warnings out of the test output; assertLogs still
# works because it swaps in its own handler on the target logger.
logging.getLogger("opencode_bridge").addHandler(logging.NullHandler())

import opencode_bridge.adapters.slack as slack_mod
from opencode_bridge.adapters.slack import (
    CONVERSATION_PREFIX,
    RECONNECT_DELAY,
    SOCKET_TIMEOUT,
    SlackAdapter,
)
from opencode_bridge.hooks import Inbound, MsgHandle, Outbound
from opencode_bridge.identity import (
    AMBIGUOUS_LEGACY_PREFIXES,
    LEGACY_PREFIXES,
    AmbiguousConversationId,
    InvalidConversationId,
    is_valid,
    normalize,
    parse_id,
)
from opencode_bridge.state import StateStore
from opencode_bridge.transport import ReconnectNow, WebSocketTransport

CID = "channel:C1"


def wait_until(predicate, timeout: float = 5.0, interval: float = 0.01) -> bool:
    """轮询等条件成立（比裸 sleep 稳；失败信息由调用方的断言给出）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


# ----------------------------------------------------------------------
# 假 WS
# ----------------------------------------------------------------------
class ScriptedWS:
    """按脚本逐帧吐出文本；脚本空则返回 ``None`` 表示对端关闭。

    ``recv_calls`` 是关键的可观测点：迁移最容易丢的保证之一是"disconnect 之后
    旧连接不再被读"，而那个保证正是靠**旧连接的 ``recv_calls`` 不再增长**证明的。
    """

    def __init__(self, script=None):
        self.script = list(script or [])
        self.sent: list[str] = []
        self.closed = False
        self.close_calls = 0
        self.recv_calls = 0

    def recv(self):
        self.recv_calls += 1
        if self.script:
            return self.script.pop(0)
        return None

    def send(self, text: str) -> None:
        self.sent.append(text)

    def close(self, code=None) -> None:
        self.close_calls += 1
        self.closed = True

    @property
    def acks(self) -> list[dict]:
        return [json.loads(item) for item in self.sent]


class BlockingWS:
    """``recv()`` 一直阻塞到 ``close()`` —— 模拟真实 Socket Mode 的长连接。

    用于证明 ``stop()`` 是**先关连接再 join**：不唤醒这个阻塞读的话，
    ``stop()`` 只能等满 join 超时（5s）。
    """

    def __init__(self):
        self._release = threading.Event()
        self.sent: list[str] = []
        self.closed = False
        self.recv_calls = 0

    def recv(self):
        self.recv_calls += 1
        self._release.wait(30.0)     # 上界：绝不让测试永久挂住
        return None

    def send(self, text: str) -> None:
        self.sent.append(text)

    def close(self, code=None) -> None:
        self.closed = True
        self._release.set()


class ExplodingWS(ScriptedWS):
    """ack 写不出去（``send`` 抛异常）—— Slack 侧写失败不该掀掉整条会话。"""

    def send(self, text: str) -> None:
        raise OSError("ack 写不出去")


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


class OrderHooks(RecordingHooks):
    """只往共享的 ``calls`` 里写**字符串**，用来断言调用顺序。"""

    def __init__(self, calls: list[str]):
        super().__init__()
        self.calls = calls

    def on_inbound(self, inbound: Inbound) -> None:
        self.calls.append("inbound")
        super().on_inbound(inbound)


class AckSpyHooks(RecordingHooks):
    """在投递 inbound 的**当下**拍下 WS 已发送的内容（快照，而不是事后回看）。

    每次投递都拍一张，所以能断言"投递第 N 条时，第 N 条自己的 ack 已经在路上"。
    """

    def __init__(self, ws_holder):
        super().__init__()
        self.ws_holder = ws_holder
        #: 每次 ``on_inbound`` 时的 WS 已发送内容快照。
        self.snapshots: list[list[str]] = []

    def on_inbound(self, inbound: Inbound) -> None:
        ws = self.ws_holder[0]
        self.snapshots.append(list(ws.sent) if ws is not None else [])
        super().on_inbound(inbound)


# ----------------------------------------------------------------------
# envelope / event 构造
# ----------------------------------------------------------------------
def envelope(env_id="e1", event=None, etype="events_api"):
    env = {"envelope_id": env_id, "type": etype}
    if event is not None:
        env["payload"] = {"event": event}
    return json.dumps(env)


def disconnect(reason="warning", env_id=None):
    return json.dumps(
        {"type": "disconnect", "reason": reason,
         "envelope_id": env_id or f"d-{reason}"}
    )


def msg(text="hi", channel="C1", user="U1", ts="111.222", **extra):
    ev = {"type": "message", "text": text, "channel": channel, "user": user, "ts": ts}
    ev.update(extra)
    return ev


def make_slack(config: dict | None = None, hooks: RecordingHooks | None = None):
    cfg = {"bot_token": "xoxb-t", "app_token": "xapp-t"}
    if config:
        cfg.update(config)
    adapter = SlackAdapter(cfg, hooks or RecordingHooks())
    adapter.min_interval = 0        # 测试里不做人为 sleep
    return adapter, adapter.hooks


class SlackTestCase(unittest.TestCase):
    """共用的注入点：脚本化假 WS + 可缩短的 ``RECONNECT_DELAY``。"""

    def shorten_backoff(self, delay: float = 0.02) -> None:
        """把模块全局 ``RECONNECT_DELAY`` 临时改小（``_make_transport`` 运行时读它）。"""
        old = slack_mod.RECONNECT_DELAY
        self.addCleanup(setattr, slack_mod, "RECONNECT_DELAY", old)
        slack_mod.RECONNECT_DELAY = delay

    def scripted(self, adapter, sockets, urls=None):
        """把 ``_ws_factory`` / ``_open_socket_url`` 换成脚本化实现。

        ``sockets`` 用完就抛错（防止重连成功后拿到空脚本再空转），
        ``urls`` 记录每次取到的 WSS URL —— ``len(urls)`` 是"确实重新取了 URL"的证据。
        """
        created: list[ScriptedWS] = []
        seen: list[str] = list(urls) if urls is not None else []

        def factory(url):
            if not sockets:
                raise RuntimeError("脚本用完了")
            ws = sockets.pop(0)
            created.append(ws)
            return ws

        def open_url():
            seen.append(f"wss://example/ws#{len(seen)}")
            return seen[-1]

        adapter._ws_factory = factory
        adapter._open_socket_url = open_url
        return created, seen


class TestSlackMigrationInvariants(SlackTestCase):
    """A1 迁移的「行为不变」清单：逐条钉死。"""

    # ------------------------------------------------------------------
    # 前缀：本轮**刻意不切**
    # ------------------------------------------------------------------
    def test_conversation_id_still_uses_legacy_channel_prefix(self):
        """显式防呆：有人在本轮偷偷把 ``channel:`` 换成 ``slack:`` 时这里会红。

        ``channel`` 在 :data:`identity.LEGACY_PREFIXES` 里值是 ``None`` ——
        **歧义前缀**，slack / discord / mattermost 三家共用，所以光看字符串
        **完全判不出**来源（对比 ``chat`` → ``telegram`` 至少能确定指向谁）。
        这正是"必须显式钉住"的理由。
        """
        self.assertIn("channel", LEGACY_PREFIXES)
        self.assertIsNone(LEGACY_PREFIXES["channel"], "channel: 是歧义前缀")
        self.assertIn("channel", AMBIGUOUS_LEGACY_PREFIXES)
        self.assertEqual(CONVERSATION_PREFIX, "channel:")
        self.assertEqual(SlackAdapter._conversation_id("C1"), CID)
        for channel in ("C1", "C0123ABCD", "D0C0FFEE", 12345):
            with self.subTest(channel=channel):
                self.assertEqual(
                    SlackAdapter._conversation_id(channel), f"channel:{channel}"
                )
        self.assertEqual(SlackAdapter._channel_id(CID), "C1")
        # 仍是旧格式：``parse_id`` 拒绝它，只有 ``normalize`` 才知道怎么归一
        self.assertFalse(is_valid(CID))
        with self.assertRaises(InvalidConversationId):
            parse_id(CID)
        self.assertEqual(normalize(CID, platform_hint="slack"), "slack:C1")
        self.assertEqual(SlackAdapter._channel_id("slack:C1"), "slack:C1",
                         "_channel_id 只剥 channel: 前缀")

    def test_the_ambiguity_is_real_so_the_prefix_cannot_be_guessed(self):
        """把"为什么现在不能切"写成**可执行**的断言：同一个旧键能归一成三家。

        这条是 :meth:`SlackAdapter._conversation_id` 里那条断言存在的理由：
        没有平台线索时 :func:`identity.normalize` **拒绝**猜（抛
        :class:`~opencode_bridge.identity.AmbiguousConversationId`），而不是
        悄悄猜成 slack —— 猜错的后果是把用户映射到别人的会话上。
        """
        with self.assertRaises(AmbiguousConversationId):
            normalize(CID)
        self.assertEqual(normalize(CID, platform_hint="slack"), "slack:C1")
        self.assertEqual(normalize(CID, platform_hint="discord"), "discord:C1")
        self.assertEqual(normalize(CID, platform_hint="mattermost"), "mattermost:C1")
        self.assertNotEqual(
            normalize(CID, platform_hint="slack"),
            normalize(CID, platform_hint="discord"),
            "同一个旧键在两家之间串台 —— 这就是旧前缀的代价",
        )

    def test_prefix_guard_assertion_actually_fires_when_the_prefix_is_switched(self):
        """证明 :meth:`SlackAdapter._conversation_id` 里那条防呆断言**不是恒真**。

        如果那条断言写得像 ``assert cid.startswith(CONVERSATION_PREFIX)``（用同一个
        常量比），改常量就会把它一起改掉、断言形同虚设；本用例把常量改成
        ``slack:`` 并要求它立刻炸 —— 断言本身被测到了。
        """
        old = slack_mod.CONVERSATION_PREFIX
        self.addCleanup(setattr, slack_mod, "CONVERSATION_PREFIX", old)
        slack_mod.CONVERSATION_PREFIX = "slack:"
        with self.assertRaises(AssertionError):
            SlackAdapter._conversation_id("C1")
        setattr(slack_mod, "CONVERSATION_PREFIX", old)   # 立刻恢复（addCleanup 只是保险）
        self.assertEqual(SlackAdapter._conversation_id("C1"), CID, "恢复后仍是旧格式")

    def test_switching_prefix_now_would_orphan_stored_sessions(self):
        """把"为什么现在不能切前缀"写成**可执行**的断言（只读地借用 StateStore）。

        ``conversation_id`` 是 :class:`StateStore` 的**不透明键**：切前缀等于把
        历史键全部作废，而且不报错、只表现为"agent 突然记错上下文"。
        真要切时必须先做键迁移 —— 那时这个用例会提醒你同步更新它。
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            cid = SlackAdapter._conversation_id("C1")
            store = StateStore(path)
            store.set_session(cid, "sess-1")
            reopened = StateStore(path)          # 模拟进程重启
            self.assertEqual(reopened.get_session(cid), "sess-1",
                             "旧键在重启后必须仍能读回（这就是'已落盘'）")
            future = normalize(cid, platform_hint="slack")
            self.assertNotEqual(future, cid)
            self.assertIsNone(
                reopened.get_session(future),
                "切前缀后同一个 channel 是另一个不透明键 → 会话映射直接丢失",
            )

    def test_inbound_ids_keep_the_legacy_prefix_end_to_end(self):
        """入站链路上的 id 也必须是 ``channel:``（不只是 ``_conversation_id``）。"""
        adapter, hooks = make_slack()
        adapter._handle_envelope(ScriptedWS(), envelope(event=msg()))
        self.assertEqual([i.conversation_id for i in hooks.inbounds], [CID])
        out = Outbound(conversation_id=CID, text="回声")
        calls: list[tuple] = []
        adapter._request = lambda m, p, pl, *, timeout=None: (
            calls.append((m, p, pl)) or (200, {"ok": True, "ts": "1"})
        )
        handle = adapter.send(out)
        self.assertIsInstance(handle, MsgHandle)
        self.assertEqual(handle.conversation_id, CID)
        self.assertEqual(calls[-1][2]["channel"], "C1",
                         "出站必须把 channel: 前缀剥掉再发（回环风险）")

    # ------------------------------------------------------------------
    # 退避接线：3s 恒定（不是指数）+ reset_after=0
    # ------------------------------------------------------------------
    def test_backoff_wiring_matches_the_legacy_constant(self):
        adapter, _ = make_slack()
        transport = adapter._make_transport()
        self.assertIsInstance(transport, WebSocketTransport)
        self.assertEqual(RECONNECT_DELAY, 3.0, "迁移前的常数就是 3s")
        self.assertEqual(transport.min_backoff, RECONNECT_DELAY)
        self.assertEqual(transport.max_backoff, RECONNECT_DELAY,
                         "max == min ⇒ 退避恒定；迁移前本来就没有指数退避")
        self.assertEqual(transport.reset_after, 0.0,
                         "必须 0：连上过就重置退避（连上即重置 = 既有语义）")
        self.assertEqual(transport.label, "slack")
        self.assertEqual(transport._idle_delay(), 0.0,
                         "WS 类传输阻塞在 recv()，不该有空转节流")

    def test_backoff_is_constant_not_exponential(self):
        """连续失败也必须恒定 3s —— 迁移前 ``wait(RECONNECT_DELAY)`` 是一个常数。"""
        adapter, _ = make_slack()
        transport = adapter._make_transport()
        waits = [transport._next_backoff(survived=False) for _ in range(4)]
        self.assertEqual(waits, [RECONNECT_DELAY] * 4,
                         "连续失败也必须恒定 3s（迁移前只有一个常数 backoff）")
        self.assertEqual(
            [transport._next_backoff(survived=True) for _ in range(3)],
            [RECONNECT_DELAY] * 3,
            "reset_after=0 ⇒ 传输层永远按'存活'判定 ⇒ 每次都取下限",
        )

    def test_failed_session_is_retried_after_the_backoff_delay(self):
        """端到端：对端一关就重连，但两次连接之间**确实等了**退避间隔。

        墙钟只做**下界**断言（等待只会更长，绝不会更短）。
        """
        delay = 0.05
        self.shorten_backoff(delay)
        adapter, _ = make_slack()
        at: list[float] = []

        def factory(url):
            at.append(time.monotonic())
            return ScriptedWS()      # 立刻返回 None → 对端关闭 → 走退避重连

        adapter._ws_factory = factory
        adapter._open_socket_url = lambda: "wss://example/ws"
        adapter.start()
        self.addCleanup(adapter.stop)
        self.assertTrue(wait_until(lambda: len(at) >= 3, timeout=5))
        for gap in [b - a for a, b in zip(at, at[1:])]:
            self.assertGreaterEqual(gap, delay * 0.9,
                                    f"断开后没退避（迁移前是 wait(RECONNECT_DELAY)）：{at}")
        transport = adapter.transport
        self.assertGreaterEqual(transport.stats()["errors"], 1,
                                "对端关闭必须被记成会话错误")

    # ------------------------------------------------------------------
    # disconnect → ReconnectNow：主动重连 + 旧连接丢弃
    # ------------------------------------------------------------------
    def test_disconnect_frame_asks_for_an_immediate_reconnect(self):
        """``disconnect`` envelope → :class:`ReconnectNow`（立刻重连、不退避）。

        同时守住 ack 铁律：``disconnect`` 本身**也必须先被 ack**（它同样带
        ``envelope_id``，漏 ack Slack 会无限重发）。
        """
        adapter, _ = make_slack()
        for reason in ("warning", "refresh_requested"):
            with self.subTest(reason=reason):
                ws = ScriptedWS()
                with self.assertRaises(ReconnectNow):
                    adapter._on_frame(ws, disconnect(reason))
                self.assertEqual(ws.acks, [{"envelope_id": f"d-{reason}"}],
                                 "disconnect 也必须先 ack")
        # 普通流量绝不要求重连
        ws = ScriptedWS()
        adapter._on_frame(ws, envelope("e1", etype="hello"))
        adapter._on_frame(ws, envelope("e2", event=msg()))
        adapter._on_frame(ws, "not json{")
        self.assertEqual(ws.acks,
                         [{"envelope_id": "e1"}, {"envelope_id": "e2"}])

    def test_disconnect_reconnects_with_a_fresh_url_without_waiting_the_backoff(self):
        """WSS URL 约 1 小时过期 → 必须**主动重连**（重新取 URL），且**不退避**。

        手法：把 ``RECONNECT_DELAY`` 设成 30s（生产值）—— 若这条路径走的是退避
        而不是 :class:`ReconnectNow`，重连根本不会在等待窗口内发生。
        """
        self.shorten_backoff(30.0)
        adapter, hooks = make_slack()
        created, urls = self.scripted(
            adapter,
            [ScriptedWS([disconnect("warning")]),
             ScriptedWS([envelope("e2", event=msg(channel="C_NEW", text="重连后收到"))])],
        )
        adapter.start()
        self.addCleanup(adapter.stop)
        self.assertTrue(wait_until(lambda: hooks.inbounds, timeout=5),
                        "disconnect 之后必须立刻重连（不该等 30s 退避）")
        self.assertGreaterEqual(len(urls), 2, "重连必须**重新取** WSS URL")
        self.assertGreaterEqual(len(created), 2, "确实建了第二条连接")
        self.assertEqual([i.conversation_id for i in hooks.inbounds], ["channel:C_NEW"])

    def test_disconnect_drops_messages_still_queued_on_the_old_connection(self):
        """disconnect 之后旧连接上到达的消息**必须被丢弃**（否则等于静默丢消息）。

        断言的是**旧连接的 ``recv_calls`` 不再增长** —— 那条连接已被传输层关闭，
        绝不会再从它读任何东西（新连接上的消息照常处理）。
        """
        self.shorten_backoff(0.02)
        adapter, hooks = make_slack()
        old = ScriptedWS([disconnect("warning"), envelope("e1", event=msg(channel="C_OLD"))])
        created, _urls = self.scripted(
            adapter,
            [old, ScriptedWS([envelope("e2", event=msg(channel="C_NEW", text="新连接"))])],
        )
        adapter.start()
        self.addCleanup(adapter.stop)
        self.assertTrue(wait_until(lambda: hooks.inbounds, timeout=5))
        reads_at_first_inbound = old.recv_calls
        self.assertTrue(wait_until(lambda: len(created) >= 2, timeout=5))
        self.assertEqual(old.recv_calls, reads_at_first_inbound,
                         "旧连接在 disconnect 之后不许再被读（否则会处理已失效连接的消息）")
        self.assertEqual(old.recv_calls, 1, "旧连接只读过那一条 disconnect")
        self.assertTrue(old.closed, "旧连接必须被关闭")
        self.assertEqual([i.conversation_id for i in hooks.inbounds], ["channel:C_NEW"],
                         "只应处理新连接上那条")

    def test_disconnect_also_survives_the_real_socket_path(self):
        """真 socket 上的一条：disconnect → ack 回到服务器 + 立刻重连。

        顺带证明 ack 用的是**客户端掩码帧**（RFC 6455 §5.3 强制），否则服务端收不到。
        """
        Server, recv_masked_text, send_text = _load_ws_test_helpers()
        from opencode_bridge.ws import connect as ws_connect

        server = Server()
        self.addCleanup(server.stop)
        urls: list[str] = []

        def open_url():
            urls.append(server.url)
            return server.url

        adapter, hooks = make_slack()
        adapter._open_socket_url = open_url
        adapter._ws_factory = lambda url: ws_connect(url)
        adapter.start()
        self.addCleanup(adapter.stop)
        conn = server.wait_handshake()
        send_text(conn, disconnect("refresh_requested"))
        self.assertEqual(json.loads(recv_masked_text(conn)),
                         {"envelope_id": "d-refresh_requested"},
                         "disconnect envelope 也必须 ack（掩码帧）")
        self.assertTrue(wait_until(lambda: len(urls) >= 2, timeout=5),
                        "disconnect 之后必须重新握手（重连 + 重取 URL）")
        self.assertEqual(hooks.inbounds, [], "disconnect 本身不该投递 inbound")

    # ------------------------------------------------------------------
    # ack 时序：先 ack 再过滤 / 闸门在最前 / 无关 envelope 也 ack
    # ------------------------------------------------------------------
    def test_every_envelope_is_acked_through_the_transport(self):
        """整条链路：``hello``、无关事件、真消息 —— **每个 envelope 都 ack**。

        漏 ack 会让 Slack **无限重发**同一条；所以过滤掉的那些也必须在 ack 之后
        才被丢弃。断言用传输层线程跑一遍（而不是直接调 ``_handle_envelope``），
        因为"迁移在提前 return 时把 ack 吃掉"正是发生在真实链路上。
        """
        self.shorten_backoff(0.02)
        adapter, hooks = make_slack()
        ws = ScriptedWS([
            envelope("e-hello", etype="hello"),
            envelope("e-noise", event={"type": "reaction_added"}),
            envelope("e-bot", event=msg(bot_id="B1")),
            envelope("e-good", event=msg(text="真消息")),
        ])
        self.scripted(adapter, [ws])
        adapter.start()
        self.addCleanup(adapter.stop)
        self.assertTrue(wait_until(lambda: hooks.inbounds, timeout=5))
        self.assertEqual(ws.acks,
                         [{"envelope_id": eid} for eid in
                          ("e-hello", "e-noise", "e-bot", "e-good")],
                         "四个 envelope（含被过滤掉的）都必须 ack，且按到达顺序")
        self.assertEqual([i.text for i in hooks.inbounds], ["真消息"])

    def test_a_single_frame_is_dispatched_exactly_once(self):
        """一条帧只投递一次（``on_message`` 与 ``on_event`` 会看到同一帧）。

        :class:`~opencode_bridge.transport.WebSocketTransport` 先调
        ``on_message(conn, frame)``、再把同一帧交给 ``on_event``。Slack 的语义
        全在前者里，所以 ``on_event`` 必须是 no-op —— 否则同一条消息会被 core
        当成两条（bot 会回两次）。
        """
        adapter, hooks = make_slack()
        ws = ScriptedWS([envelope("e1", event=msg())])
        self.scripted(adapter, [ws])
        adapter.start()
        self.addCleanup(adapter.stop)
        self.assertTrue(wait_until(lambda: hooks.inbounds, timeout=5))
        # 断言"恰好一次"：等脚本跑完 + 断线重连那一轮也过掉，再看总数。
        self.assertTrue(wait_until(lambda: not ws.script, timeout=5))
        self.assertEqual(len(hooks.inbounds), 1, "一帧只能投递一次")
        self.assertEqual(len(ws.sent), 1, "一帧只 ack 一次")

    def test_ack_is_sent_before_filtering_and_before_dispatch(self):
        """**先 ack 再过滤**：每条消息自己的 ack 必须先于它被投递发出。

        断言的是"投递当下的快照"（:attr:`AckSpyHooks.snapshots`），不是事后回看 ——
        这样"先过滤 / 先投递再 ack"的写法会立刻失败。
        """
        holder: list[ScriptedWS | None] = [None]
        hooks = AckSpyHooks(holder)
        adapter, _ = make_slack(hooks=hooks)
        ws = ScriptedWS([envelope("e1", event=msg()), envelope("e2", event=msg())])
        holder[0] = ws
        self.scripted(adapter, [ws])
        adapter.start()
        self.addCleanup(adapter.stop)
        self.assertTrue(wait_until(lambda: len(hooks.inbounds) >= 2, timeout=5))
        self.assertEqual(
            hooks.snapshots,
            [[json.dumps({"envelope_id": "e1"})],
             [json.dumps({"envelope_id": "e1"}), json.dumps({"envelope_id": "e2"})]],
            "投递每条消息时，它自己的 ack 必须已经在路上（先 ack 再过滤/投递）",
        )

    def test_authorization_gate_runs_before_inbound_and_dropped_envelope_is_acked(self):
        """授权闸门在最前（命令/审批字不能绕过），但被丢弃的 envelope **仍 ack**。"""
        calls: list[str] = []
        hooks = OrderHooks(calls)
        adapter, _ = make_slack({"allowed_chat_ids": ["C_allow"]}, hooks=hooks)
        original_admits = adapter.admits

        def admits(principal):
            calls.append(f"admits:{principal}")
            return original_admits(principal)

        adapter.admits = admits                       # type: ignore[method-assign]
        ws = ScriptedWS([envelope("e1", event=msg(channel="C_other")),
                         envelope("e2", event=msg(channel="C_allow"))])
        self.scripted(adapter, [ws])
        adapter.start()
        self.addCleanup(adapter.stop)
        self.assertTrue(wait_until(lambda: hooks.inbounds, timeout=5))
        self.assertEqual([i.conversation_id for i in hooks.inbounds], ["channel:C_allow"])
        self.assertEqual(calls,
                         ["admits:C_other", "admits:C_allow", "inbound"],
                         "闸门必须先于投递被调用（core 之前）")
        self.assertEqual(ws.acks,
                         [{"envelope_id": "e1"}, {"envelope_id": "e2"}],
                         "被闸门丢弃的 envelope 也要 ack（漏 ack Slack 无限重发）")

    def test_ack_send_failure_does_not_kill_the_session(self):
        """ack 写不出去（socket 抖动）时：记告警、**继续消费**，不掀掉会话。"""
        self.shorten_backoff(0.02)
        adapter, hooks = make_slack()
        ws = ExplodingWS([envelope("e1", event=msg(text="第一条")),
                          envelope("e2", event=msg(text="第二条"))])
        self.scripted(adapter, [ws])
        # ⚠️ ``start()`` 必须在 assertLogs **里面**：脚本化 WS 是瞬间喂完的，
        # 放在上下文外面的话告警会在捕获窗口打开前就发完。
        with self.assertLogs("opencode_bridge.adapters.slack", level="WARNING"):
            adapter.start()
            self.addCleanup(adapter.stop)
            self.assertTrue(wait_until(lambda: len(hooks.inbounds) >= 2, timeout=5),
                            "ack 失败不得让整条会话停下")
        self.assertEqual([i.text for i in hooks.inbounds], ["第一条", "第二条"])
        self.assertTrue(adapter.running)

    def test_hook_exception_does_not_stop_consuming(self):
        """``hooks.on_inbound`` 抛异常只记日志，消费继续（迁移前也是 try/except）。"""
        self.shorten_backoff(0.02)

        class Exploding(RecordingHooks):
            def on_inbound(self, inbound):
                super().on_inbound(inbound)
                if len(self.inbounds) == 1:
                    raise RuntimeError("core exploded")

        adapter, hooks = make_slack(hooks=Exploding())
        ws = ScriptedWS([envelope("e1", event=msg(text="第一条")),
                         envelope("e2", event=msg(text="第二条"))])
        self.scripted(adapter, [ws])
        with self.assertLogs("opencode_bridge.adapters.slack", level="ERROR"):
            adapter.start()
            self.addCleanup(adapter.stop)
            self.assertTrue(wait_until(lambda: len(hooks.inbounds) >= 2, timeout=5),
                            "投递崩了也必须继续读下一帧")
        self.assertTrue(adapter.running)

    # ------------------------------------------------------------------
    # stop()：快（先关 WS 再 join，有耗时上界）+ 幂等 + 不泄漏线程
    # ------------------------------------------------------------------
    def test_stop_closes_the_socket_before_joining_the_thread(self):
        """``recv()`` 阻塞时 ``stop()`` 必须**先关连接再 join**（耗时上界）。

        不先唤醒那个阻塞读的话，只能等满 join 超时（5s）—— 这正是迁移前踩过的坑。
        """
        self.shorten_backoff(0.02)
        adapter, _ = make_slack()
        ws = BlockingWS()
        self.scripted(adapter, [ws])
        adapter.start()
        self.addCleanup(adapter.stop)
        transport = adapter.transport
        self.assertTrue(wait_until(lambda: ws.recv_calls >= 1, timeout=5),
                        "没进到 recv()")
        began = time.monotonic()
        adapter.stop()
        elapsed = time.monotonic() - began
        self.assertLess(elapsed, 2.0,
                        f"stop() 没先关 WS 唤醒阻塞的 recv（耗时 {elapsed:.2f}s）")
        self.assertTrue(ws.closed, "stop() 必须关闭活动连接")
        self.assertFalse(adapter.running)
        self.assertFalse(transport.running, "消费线程必须退出（不许泄漏）")
        self.assertIsNone(adapter.transport)

    def test_stop_returns_quickly_over_a_real_socket(self):
        """真 socket 上同样成立：WS 读超时是 :data:`SOCKET_TIMEOUT`（30s）。

        所以 ``stop()`` 的耗时上界能抓住"退化成等满读超时 / join 超时"。
        """
        Server, _recv_masked_text, send_text = _load_ws_test_helpers()
        from opencode_bridge.ws import connect as ws_connect

        server = Server()
        self.addCleanup(server.stop)
        adapter, hooks = make_slack()
        adapter._open_socket_url = lambda: server.url
        adapter._ws_factory = lambda url: ws_connect(url)
        adapter.start()
        self.addCleanup(adapter.stop)
        conn = server.wait_handshake()
        send_text(conn, envelope("E1", event=msg()))     # 让入站跑起来
        self.assertTrue(wait_until(lambda: hooks.inbounds, timeout=5))
        self.assertEqual(SOCKET_TIMEOUT, 30.0, "本用例依赖 WS 读超时远大于 stop 上界")
        transport = adapter.transport
        began = time.monotonic()
        adapter.stop()
        elapsed = time.monotonic() - began
        self.assertLess(elapsed, 2.0,
                        f"真 socket 上 stop() 没唤醒阻塞的 recv（耗时 {elapsed:.2f}s）")
        self.assertTrue(wait_until(lambda: not transport.running, timeout=5),
                        "消费线程必须退出")

    def test_stop_interrupts_the_backoff_wait(self):
        """正在退避等待里时 ``stop()`` 必须立刻返回（``Event.wait`` 而非 sleep）。"""
        self.shorten_backoff(30.0)
        adapter, _ = make_slack()
        attempts: list[float] = []
        self.scripted(adapter, [])
        adapter._ws_factory = lambda url: (attempts.append(time.monotonic())
                                           or ScriptedWS())
        adapter.start()
        self.addCleanup(adapter.stop)
        self.assertTrue(wait_until(lambda: len(attempts) >= 1, timeout=5))
        began = time.monotonic()
        adapter.stop()
        self.assertLess(time.monotonic() - began, 2.0,
                        "stop() 没打断 30s 的退避等待")
        self.assertFalse(adapter.running)

    def test_stop_is_idempotent_and_safe_before_start(self):
        adapter, _ = make_slack()
        adapter.stop()
        self.assertFalse(adapter.running)
        self.assertIsNone(adapter.transport)
        self.scripted(adapter, [BlockingWS()])
        adapter.start()
        adapter.stop()
        adapter.stop()                                   # 幂等
        self.assertFalse(adapter.running)

    def test_start_after_stop_works_again(self):
        """重启同一个实例必须能重新连上（``_stop_event`` 要被清掉）。

        迁移前 ``start()`` 漏了这一行，``stop()`` 之后再 ``start()`` 会静默不工作。
        """
        self.shorten_backoff(0.02)
        adapter, hooks = make_slack()
        ws1 = ScriptedWS([envelope("e1", event=msg(text="第一轮"))])
        self.scripted(adapter, [ws1])
        adapter.start()
        self.assertTrue(wait_until(lambda: hooks.inbounds, timeout=5))
        adapter.stop()
        adapter._ws_factory = lambda url: ScriptedWS(
            [envelope("e2", event=msg(text="第二轮"))]
        )
        adapter._open_socket_url = lambda: "wss://example/ws"
        adapter.start()
        self.addCleanup(adapter.stop)
        self.assertTrue(wait_until(lambda: len(hooks.inbounds) >= 2, timeout=5),
                        "stop() 之后 start() 必须能重新消费")
        self.assertEqual([i.text for i in hooks.inbounds], ["第一轮", "第二轮"])

    # ------------------------------------------------------------------
    # 健壮性：传输层异常不许静默死线程；降级不许静默
    # ------------------------------------------------------------------
    def test_transport_failure_does_not_silently_kill_the_thread(self):
        """``apps.connections.open`` / 握手失败时：记错误、继续重试、线程不死。"""
        self.shorten_backoff(0.02)
        adapter, _ = make_slack()
        attempts: list[str] = []

        def open_url():
            attempts.append("open")
            raise RuntimeError("apps.connections.open failed: invalid_auth")

        adapter._open_socket_url = open_url
        adapter._ws_factory = lambda url: ScriptedWS()
        adapter.start()
        self.addCleanup(adapter.stop)
        self.assertTrue(wait_until(lambda: len(attempts) >= 3, timeout=5),
                        "失败后必须继续重试（线程不许静默退出）")
        self.assertTrue(adapter.running)
        stats = adapter.transport.stats()
        self.assertGreaterEqual(stats["errors"], 1)
        self.assertEqual(stats["connects"], 0, "从没连上过就不该有 connects")
        self.assertEqual(stats["events"], 0, "传输层故障不该被当成收到过事件")

    def test_ws_connect_failure_keeps_retrying(self):
        """连上（WSS URL 取到了）但握手失败：同样只退避重试，不死线程。"""
        self.shorten_backoff(0.02)
        adapter, _ = make_slack()
        attempts: list[int] = []

        def factory(url):
            attempts.append(len(attempts))
            raise OSError("handshake failed")

        adapter._open_socket_url = lambda: "wss://example/ws"
        adapter._ws_factory = factory
        adapter.start()
        self.addCleanup(adapter.stop)
        self.assertTrue(wait_until(lambda: len(attempts) >= 3, timeout=5))
        self.assertTrue(adapter.running)
        self.assertGreaterEqual(adapter.transport.stats()["errors"], 1)

    def test_missing_app_token_degrades_to_outbound_only_with_a_warning(self):
        """缺 ``app_token`` → 降级为"只发出站"并**告警**（不许静默失败）。"""
        adapter, _ = make_slack({"app_token": ""})
        adapter._ws_factory = lambda url: self.fail("缺 app_token 时不该建连")
        with self.assertLogs("opencode_bridge.adapters.slack", level="WARNING") as cm:
            adapter.start()
        self.assertIn("app_token", "\n".join(cm.output))
        self.assertIsNone(adapter.transport, "不该建传输层")
        self.assertFalse(adapter.running)
        self.assertIn("app_token", SlackAdapter.required_tokens,
                      "入站必需 token 必须声明（否则 --status 报成已配置）")
        # 降级后出站仍可用
        adapter._request = lambda m, p, pl, *, timeout=None: (
            200, {"ok": True, "ts": "1"}
        )
        self.assertIsInstance(adapter.send(Outbound(conversation_id=CID, text="hi")),
                              MsgHandle)

    def test_missing_bot_token_does_not_start_at_all(self):
        adapter, _ = make_slack({"bot_token": "", "app_token": ""})
        with self.assertLogs("opencode_bridge.adapters.slack", level="WARNING"):
            adapter.start()
        self.assertIsNone(adapter.transport)
        self.assertFalse(adapter.running)

    # ------------------------------------------------------------------
    # 出站：HTTP 200 + ok:false 的错误分类不许被吞掉
    # ------------------------------------------------------------------
    def test_http_200_with_ok_false_is_still_classified_as_a_failure(self):
        """Slack 大量用 HTTP 200 + ``ok:false`` 报失败 —— 不能当成成功。"""
        cases = [
            (200, "channel_not_found", "NOT_FOUND"),
            (200, "invalid_auth", "FORBIDDEN"),
            (200, "missing_scope", "FORBIDDEN"),
            (200, "rate_limited", "RATE_LIMITED"),
            (200, "no_text", "BAD_FORMAT"),
            (200, "too_large", "TOO_LONG"),
            (429, "", "RATE_LIMITED"),
            (500, "", "TRANSIENT"),
        ]
        from opencode_bridge.hooks import SendError

        expected = {
            "NOT_FOUND": SendError.NOT_FOUND,
            "FORBIDDEN": SendError.FORBIDDEN,
            "RATE_LIMITED": SendError.RATE_LIMITED,
            "BAD_FORMAT": SendError.BAD_FORMAT,
            "TOO_LONG": SendError.TOO_LONG,
            "TRANSIENT": SendError.TRANSIENT,
        }
        for status, error, want in cases:
            with self.subTest(status=status, error=error):
                adapter, _ = make_slack()
                adapter._request = lambda m, p, pl, *, timeout=None: (
                    status, {"ok": False, "error": error}
                )
                with self.assertLogs("opencode_bridge.adapters.slack", level="WARNING"):
                    handle = adapter.send(Outbound(conversation_id=CID, text="hi"))
                self.assertIsNone(handle, "ok:false 必须算失败")
                result = adapter.send_result(Outbound(conversation_id=CID, text="hi"))
                self.assertFalse(result.ok)
                self.assertIs(result.error_kind, expected[want])

    def test_edit_failure_is_reported_and_ok_false_is_never_true(self):
        adapter, _ = make_slack()
        adapter._request = lambda m, p, pl, *, timeout=None: (
            200, {"ok": False, "error": "message_not_found"}
        )
        handle = MsgHandle(conversation_id=CID, message_id="1", platform="slack")
        with self.assertLogs("opencode_bridge.adapters.slack", level="WARNING"):
            self.assertFalse(adapter.edit(handle, Outbound(conversation_id=CID, text="x")))


def _load_ws_test_helpers():
    """两种跑法都要能用（``unittest discover`` vs ``python -m unittest``）。"""
    try:
        from tests.test_ws import Server, recv_masked_text, send_text
    except ImportError:
        from test_ws import Server, recv_masked_text, send_text
    return Server, recv_masked_text, send_text


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

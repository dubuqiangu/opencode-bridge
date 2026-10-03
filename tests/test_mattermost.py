"""T3.2 测试 —— Mattermost 入站（WebSocket 事件流）+ REST 出站。零真实网络。

协议层用**假 WS 注入**（``_ws_factory``，与 ``test_discord_gateway.py`` 里 Discord
Gateway 同一手法），REST 用 monkeypatch ``_request``。真实 RFC 6455 收发由
``tests/test_ws.py`` 用裸服务器覆盖。

对照测试清单：
能力声明 / required_tokens / 静态下限、缺 token 或 site_url 不起线程、**握手带
Authorization 头**、hello 缓存 connection_id 与 server_version、posted → Inbound
（字段名取对）、防回环、过滤矩阵、只处理 posted、两个信封不混淆、闸门顺序、
发消息 URL/body/201/失败可观测、**编辑用 /patch**、MaxPostSize 运行时细化与回落、
帧尺寸上限、重连 query、stop 及时结束线程、**ping → pong 的保证**。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import unittest

logging.getLogger("opencode_bridge").addHandler(logging.NullHandler())

import opencode_bridge.adapters.mattermost as mm_mod
from opencode_bridge.adapters import build, registered_names
from opencode_bridge.adapters.mattermost import (
    POSTS_PATH,
    PRE_HELLO_GRACE,
    WS_MAX_FRAME_BYTES,
    MattermostAdapter,
)
from opencode_bridge.hooks import Inbound, MsgHandle, Outbound, SendError
from opencode_bridge.transport import WebSocketTransport

SITE_URL = "https://mm.example.test"
TOKEN = "tok-abcdefghijklmnopqrstuvwxyz0123"
MY_USER_ID = "u_me_0123456789abcdefghijklm"
OTHER_USER_ID = "u_other_0123456789abcdefgh"
CHANNEL = "c_channel_0123456789abcdefghij"
CONNECTION_ID = "con_0123456789abcdefghijklmn"


# ----------------------------------------------------------------------
# 测试替身
# ----------------------------------------------------------------------
class RecordingHooks:
    """记录每次 ``on_inbound``。"""

    def __init__(self) -> None:
        self.inbounds: list[Inbound] = []

    def on_inbound(self, inbound: Inbound) -> None:
        self.inbounds.append(inbound)

    def on_callback(self, conversation_id: str, data: str, query_id: str) -> None:
        pass


class ExplodingHooks(RecordingHooks):
    def on_inbound(self, inbound: Inbound) -> None:
        raise RuntimeError("上层炸了")


class FakeWS:
    """最小假 WebSocket：按脚本逐条吐文本，脚本空则返回 ``None`` 表示对端关闭。"""

    def __init__(self, script=None, close_code=None, close_reason="") -> None:
        self.script: list[str] = list(script or [])
        self.sent: list[str] = []
        self.closed = False
        self.close_code = close_code
        self.close_reason = close_reason
        self.recv_calls = 0

    def recv(self) -> str | None:
        self.recv_calls += 1
        if self.script:
            return self.script.pop(0)
        return None

    def send(self, text: str) -> None:
        self.sent.append(text)

    def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True


class BlockingWS(FakeWS):
    """``recv()`` 一直阻塞直到 ``close()`` 唤醒它（模拟真实长连接）。"""

    def __init__(self) -> None:
        super().__init__()
        self._woken = threading.Event()

    def recv(self) -> str | None:
        self._woken.wait(10)
        return None

    def close(self, code: int = 1000, reason: str = "") -> None:
        super().close(code, reason)
        self._woken.set()


# ----------------------------------------------------------------------
# 信封构造（字段名必须与官方一致）
# ----------------------------------------------------------------------
def event_frame(event: str, data: dict, seq: int = 7) -> str:
    """事件信封：``{"event", "data", "broadcast", "seq"}``。"""
    return json.dumps(
        {"event": event, "data": data, "broadcast": {"channel_id": CHANNEL}, "seq": seq}
    )


def hello_frame(connection_id: str = CONNECTION_ID, server_version: str = "9.5.2") -> str:
    return event_frame("hello", {"connection_id": connection_id,
                                 "server_version": server_version})


def post_frame(
    message: str = "hello",
    *,
    user_id: str = OTHER_USER_ID,
    channel_id: str = CHANNEL,
    post_id: str = "post_1",
    type_: str = "",
    delete_at: int = 0,
    file_ids=None,
    root_id: str = "",
    seq: int = 7,
    **extra,
) -> str:
    """``posted`` 事件的 ``data``：字段名是 ``message``（不是 text/content）。"""
    data = {
        "id": post_id,
        "create_at": 1700000000000,
        "update_at": 1700000000000,
        "delete_at": delete_at,
        "user_id": user_id,
        "channel_id": channel_id,
        "root_id": root_id,
        "message": message,
        "type": type_,
        "file_ids": file_ids or [],
    }
    data.update(extra)
    return event_frame("posted", data, seq=seq)


def response_frame(status: str = "OK", seq_reply: int = 3) -> str:
    """响应信封：**没有** ``event``，只有 ``status`` / ``seq_reply``。"""
    return json.dumps({"status": status, "seq_reply": seq_reply, "data": {}})


# ----------------------------------------------------------------------
# 适配器工厂
# ----------------------------------------------------------------------
def make_adapter(
    config: dict | None = None,
    hooks: RecordingHooks | None = None,
    *,
    user_id: str = MY_USER_ID,
) -> tuple[MattermostAdapter, RecordingHooks]:
    cfg = {"site_url": SITE_URL, "token": TOKEN, "user_id": user_id}
    if config:
        cfg.update(config)
    hooks = hooks or RecordingHooks()
    adapter = MattermostAdapter(cfg, hooks)
    adapter.min_interval = 0
    return adapter, hooks


def stub_rest(adapter: MattermostAdapter, replies=None):
    """把 ``_request`` 换成记录器；``replies`` 按调用顺序返回 ``(status, body)``。"""
    calls: list[tuple] = []
    queue = list(replies or [])

    def fake(method, path, payload=None, *, timeout=None):
        calls.append((method, path, dict(payload) if payload is not None else None))
        if queue:
            return queue.pop(0)
        return 200, {}

    adapter._request = fake  # type: ignore[method-assign]
    return calls


class MattermostTestCase(unittest.TestCase):
    """子类若改了模块级重连常量，必须用 :meth:`patch_module` 登记还原。"""

    def patch_module(self, name: str, value) -> None:
        original = getattr(mm_mod, name)
        setattr(mm_mod, name, value)

        def restore() -> None:
            setattr(mm_mod, name, original)

        self.addCleanup(restore)


# ----------------------------------------------------------------------
# 1) 能力声明
# ----------------------------------------------------------------------
class TestCapabilities(MattermostTestCase):
    def test_declared_capabilities(self):
        adapter, _ = make_adapter()
        caps = adapter.capabilities()
        self.assertEqual(caps["name"], "mattermost")
        self.assertEqual(caps["label"], "Mattermost")
        self.assertTrue(caps["supports_inbound"])
        self.assertFalse(caps["supports_inline_buttons"])
        self.assertFalse(caps["supports_media"])
        self.assertEqual(caps["typed_command_prefix"], "/")

    def test_required_tokens_are_real_credential_keys(self):
        self.assertEqual(MattermostAdapter.required_tokens, ("site_url", "token"))
        adapter, _ = make_adapter()
        # 缺 site_url / token 时 --status 要能看出"没配好"
        from opencode_bridge.adapters import adapter_class

        cls = adapter_class("mattermost")
        self.assertIs(cls, MattermostAdapter)
        self.assertIn("site_url", cls.required_tokens)
        self.assertIn("token", cls.required_tokens)

    def test_max_message_length_is_a_conservative_static_floor(self):
        """类属性只是**静态下限**（4000 = PostMessageMaxRunesV1），不是精确上限。"""
        self.assertEqual(MattermostAdapter.max_message_length, 4000)
        adapter, _ = make_adapter()
        self.assertEqual(adapter.effective_max_length, 4000)
        # 运行时还没细化时，生效值 == 静态下限
        stub_rest(adapter, [(200, {"id": "p", "config": {"MaxPostSize": "8000"}})])
        self.assertEqual(adapter.effective_max_length, 4000)
        # 细化后立即改变（细化的机制在 TestMaxPostSize 里细测）
        adapter._apply_max_post_size(8000)
        self.assertEqual(adapter.effective_max_length, 8000)

    def test_registered_in_registry(self):
        self.assertIn("mattermost", registered_names())

    def test_build_from_registry_needs_no_other_file_change(self):
        """注册表动态发现：``build("mattermost")`` 不依赖改任何其它文件。"""
        adapter = build("mattermost", {"site_url": SITE_URL, "token": TOKEN},
                        RecordingHooks())
        self.assertIsInstance(adapter, MattermostAdapter)
        self.assertEqual(adapter.capabilities()["label"], "Mattermost")

    def test_build_with_empty_config_does_not_raise(self):
        """``--status`` 会对每个已注册平台调 ``build(key, entry, hooks)``，
        entry 可能整个是空的 —— 构造期绝不能抛。"""
        adapter = build("mattermost", {}, RecordingHooks())
        self.assertEqual(adapter.site_url, "")
        self.assertEqual(adapter.token, "")
        self.assertFalse(adapter.running)


# ----------------------------------------------------------------------
# 2) 缺凭据不起线程
# ----------------------------------------------------------------------
class TestMissingCredentials(MattermostTestCase):
    def test_missing_token_does_not_start(self):
        adapter = MattermostAdapter({"site_url": SITE_URL}, RecordingHooks())
        adapter._ws_factory = lambda *a, **k: self.fail("不该建连")
        adapter._request = lambda *a, **k: self.fail("不该打 REST")  # type: ignore[method-assign]
        with self.assertLogs("opencode_bridge.adapters.mattermost", level="WARNING"):
            adapter.start()
        self.assertFalse(adapter.running)
        self.assertIsNone(adapter._thread)

    def test_missing_site_url_does_not_start(self):
        adapter = MattermostAdapter({"token": TOKEN}, RecordingHooks())
        adapter._ws_factory = lambda *a, **k: self.fail("不该建连")
        adapter._request = lambda *a, **k: self.fail("不该打 REST")  # type: ignore[method-assign]
        with self.assertLogs("opencode_bridge.adapters.mattermost", level="WARNING"):
            adapter.start()
        self.assertFalse(adapter.running)

    def test_server_url_is_accepted_as_alias(self):
        adapter = MattermostAdapter({"server_url": SITE_URL + "/", "token": TOKEN},
                                    RecordingHooks())
        self.assertEqual(adapter.site_url, SITE_URL)


# ----------------------------------------------------------------------
# 3) URL 与握手
# ----------------------------------------------------------------------
class TestHandshakeUrl(MattermostTestCase):
    def test_ws_url_path_and_scheme(self):
        adapter, _ = make_adapter()
        self.assertEqual(
            adapter._ws_base_url(), "wss://mm.example.test/api/v4/websocket"
        )

    def test_scheme_derived_per_config(self):
        cases = {
            "https://x.test": "wss://x.test/api/v4/websocket",
            "http://x.test": "ws://x.test/api/v4/websocket",
            # 已写成 ws scheme 也不该拼错
            "wss://x.test": "wss://x.test/api/v4/websocket",
            "ws://x.test": "ws://x.test/api/v4/websocket",
            # 没写 scheme 按安全的 https 猜
            "x.test": "wss://x.test/api/v4/websocket",
        }
        for site, expected in cases.items():
            with self.subTest(site=site):
                adapter = MattermostAdapter({"site_url": site, "token": "t"},
                                            RecordingHooks())
                self.assertEqual(adapter._ws_base_url(), expected)

    def test_subpath_install_keeps_base_path(self):
        adapter, _ = make_adapter({"site_url": "https://host.test/mattermost/"})
        self.assertEqual(
            adapter._ws_base_url(), "wss://host.test/mattermost/api/v4/websocket"
        )
        self.assertEqual(adapter._rest_base(), "https://host.test/mattermost")

    def test_rest_base_maps_ws_scheme_back(self):
        adapter, _ = make_adapter({"site_url": "wss://host.test"})
        self.assertEqual(adapter._rest_base(), "https://host.test")


class TestAuthHeader(MattermostTestCase):
    def test_handshake_carries_bearer_authorization_header(self):
        """**最容易漏的一步**：token 走握手 header，不是 query。"""
        adapter, _ = make_adapter()
        captured: list[tuple] = []

        def factory(url, **kw):
            captured.append((url, kw))
            return FakeWS()

        adapter._ws_factory = factory
        ws = adapter._make_ws(adapter._ws_base_url())
        self.assertIsInstance(ws, FakeWS)
        self.assertEqual(len(captured), 1)
        url, kw = captured[0]
        self.assertEqual(url, "wss://mm.example.test/api/v4/websocket")
        self.assertEqual(kw["headers"]["Authorization"], f"Bearer {TOKEN}")
        self.assertNotIn(TOKEN, url, "token 绝不能出现在 URL / query 里")

    def test_ws_factory_gets_recv_timeout(self):
        adapter, _ = make_adapter()
        captured: dict = {}

        def factory(url, **kw):
            captured.update(kw)
            return FakeWS()

        adapter._ws_factory = factory
        adapter._make_ws("wss://x.test/ws")
        # 服务端 60 秒 ping / 100 秒断，75 秒读超时落在两者之间
        self.assertEqual(captured["timeout"], mm_mod.WS_RECV_TIMEOUT)
        self.assertLess(captured["timeout"], 100.0)
        self.assertGreater(captured["timeout"], 60.0)

    def test_auth_failure_close_is_diagnosable(self):
        """鉴权失败 = 服务端直接关连接、**不给任何错误 JSON**。必须与网络断开区分开。"""
        adapter, _ = make_adapter()
        level, text = adapter._diagnose_disconnect(
            got_hello=False, elapsed=0.5, close_code=1000
        )
        self.assertGreaterEqual(level, 40, "鉴权失败要打 error/warning 而不是 info")
        self.assertIn("鉴权失败", text)
        self.assertIn("hello", text)
        self.assertIn("token", text)

        # 已经握手成功过 → 普通的服务端关闭 / 网络抖动，不要误报成鉴权失败
        level, text = adapter._diagnose_disconnect(
            got_hello=True, elapsed=3600.0, close_code=1001
        )
        self.assertEqual(level, 30)
        self.assertNotIn("鉴权失败", text)

        # 还没收到 hello 但断了很久 → 更可能是网络抖动
        level, text = adapter._diagnose_disconnect(
            got_hello=False, elapsed=PRE_HELLO_GRACE + 5, close_code=None
        )
        self.assertEqual(level, 30)
        self.assertNotIn("鉴权失败", text)


# ----------------------------------------------------------------------
# 4) hello
# ----------------------------------------------------------------------
class TestHello(MattermostTestCase):
    def test_hello_caches_connection_id_and_server_version(self):
        adapter, _ = make_adapter()
        ws = FakeWS()
        self.assertTrue(adapter._handle_packet(ws, hello_frame()))
        self.assertTrue(adapter._got_hello)
        self.assertEqual(adapter._connection_id, CONNECTION_ID)
        self.assertEqual(adapter._server_version, "9.5.2")

    def test_hello_reports_is_hello_to_caller(self):
        adapter, _ = make_adapter()
        ws = FakeWS()
        self.assertTrue(adapter._handle_packet(ws, hello_frame()))
        self.assertFalse(adapter._handle_packet(ws, post_frame()))

    def test_rehello_replaces_connection_id(self):
        """可靠重连没命中队列时服务端会**重新**发 hello 并换新 connection_id。"""
        adapter, _ = make_adapter()
        adapter._handle_packet(FakeWS(), hello_frame(connection_id="old_id"))
        adapter._handle_packet(FakeWS(), hello_frame(connection_id="new_id", server_version="9.6"))
        self.assertEqual(adapter._connection_id, "new_id")
        self.assertEqual(adapter._server_version, "9.6")


# ----------------------------------------------------------------------
# 5~8) posted 事件：字段映射 / 防回环 / 过滤矩阵 / 事件白名单
# ----------------------------------------------------------------------
class TestPostedEvent(MattermostTestCase):
    def test_posted_becomes_inbound_with_correct_field_names(self):
        adapter, hooks = make_adapter()
        ws = FakeWS()
        adapter._handle_packet(
            ws,
            post_frame("你好 **world**", user_id=OTHER_USER_ID, post_id="post_abc"),
        )
        self.assertEqual(len(hooks.inbounds), 1)
        inbound = hooks.inbounds[0]
        self.assertEqual(inbound.conversation_id, f"channel:{CHANNEL}")
        self.assertEqual(inbound.text, "你好 **world**", "正文取 data['message']")
        self.assertEqual(inbound.user_id, OTHER_USER_ID, "作者取 data['user_id']")
        self.assertEqual(inbound.message_id, "post_abc", "消息 id 取 data['id']")
        self.assertEqual(inbound.kind, "text")
        self.assertEqual(inbound.platform, "mattermost")
        self.assertEqual(inbound.raw["channel_id"], CHANNEL)
        self.assertEqual(inbound.raw["message"], "你好 **world**")

    def test_self_post_is_dropped_other_users_are_kept(self):
        adapter, hooks = make_adapter()
        ws = FakeWS()
        adapter._handle_packet(ws, post_frame("自己发的", user_id=MY_USER_ID))
        self.assertEqual(hooks.inbounds, [], "自己发的必须丢（否则无限回环）")
        adapter._handle_packet(ws, post_frame("别人发的", user_id=OTHER_USER_ID))
        self.assertEqual([ib.text for ib in hooks.inbounds], ["别人发的"])

    def test_loop_guard_uses_user_id_only_not_a_bot_flag(self):
        """Post 对象上**没有**任何 bot 标志字段，所以防回环只能用 user_id 比对。"""
        adapter, hooks = make_adapter()
        # 别人的 bot（没有 bot 字段可判）照样要收
        adapter._handle_packet(
            FakeWS(),
            post_frame("bot 消息", user_id=OTHER_USER_ID, props={}, username="somebot"),
        )
        self.assertEqual(len(hooks.inbounds), 1)

    def test_inbound_paused_without_own_user_id(self):
        """拿不到自己的 user id 就无法防回环 —— 宁可停摆也不能自问自答。"""
        adapter, hooks = make_adapter(user_id="")
        adapter._handle_packet(FakeWS(), post_frame("谁说的", user_id=OTHER_USER_ID))
        self.assertEqual(hooks.inbounds, [])

    def test_filter_matrix(self):
        cases = {
            "系统消息": post_frame("join 频道", type_="system_join_channel"),
            "已软删除": post_frame("撤回", delete_at=1700000009999),
            "空正文且无附件": post_frame(""),
            "纯空白正文且无附件": post_frame("   \n\t "),
        }
        for label, frame in cases.items():
            with self.subTest(case=label):
                adapter, hooks = make_adapter()
                adapter._handle_packet(FakeWS(), frame)
                self.assertEqual(hooks.inbounds, [], f"{label} 应被丢弃")

    def test_empty_message_with_file_ids_is_kept(self):
        """纯附件消息 message 为空但有 file_ids —— 不是空消息。"""
        adapter, hooks = make_adapter()
        adapter._handle_packet(
            FakeWS(), post_frame("", file_ids=["fil_abc"])
        )
        self.assertEqual(len(hooks.inbounds), 1)

    def test_delete_at_zero_is_kept(self):
        adapter, hooks = make_adapter()
        adapter._handle_packet(FakeWS(), post_frame("正常", delete_at=0))
        self.assertEqual(len(hooks.inbounds), 1)

    def test_only_posted_is_handled(self):
        adapter, hooks = make_adapter()
        ws = FakeWS()
        for event in ("post_edited", "post_deleted", "typing", "user_typing",
                      "reaction_added", "reaction_removed", "channel_created",
                      "status_change", "direct_added"):
            adapter._handle_packet(ws, event_frame(event, post_payload_dict()))
        self.assertEqual(hooks.inbounds, [], "非 posted 事件一律忽略")

    def test_thread_reply_is_kept(self):
        """``root_id != ""`` 是线程回复，不是噪声（要求里没让过滤它）。"""
        adapter, hooks = make_adapter()
        adapter._handle_packet(FakeWS(), post_frame("线程回复", root_id="post_root"))
        self.assertEqual(len(hooks.inbounds), 1)

    def test_missing_fields_use_defaults(self):
        """官方 OpenAPI 的 schema 比运行时 JSON 略旧 —— 一律 .get() 兜底。"""
        adapter, hooks = make_adapter()
        adapter._handle_packet(FakeWS(), json.dumps({
            "event": "posted",
            "data": {"channel_id": CHANNEL, "user_id": OTHER_USER_ID,
                     "message": "字段很少"},
            "seq": 5,
        }))
        self.assertEqual(len(hooks.inbounds), 1)
        self.assertIsNone(hooks.inbounds[0].message_id)

    def test_seq_is_tracked_even_for_dropped_events(self):
        """seq 是服务端分配的：被丢弃的事件同样占用 seq，不记会让重连重复投递。"""
        adapter, _ = make_adapter()
        ws = FakeWS()
        adapter._handle_packet(ws, post_frame("自己的", user_id=MY_USER_ID, seq=11))
        self.assertEqual(adapter._last_seq, 11)
        adapter._handle_packet(ws, post_frame("", seq=12))
        self.assertEqual(adapter._last_seq, 12)

    def test_malformed_frames_are_ignored(self):
        adapter, hooks = make_adapter()
        ws = FakeWS()
        adapter._handle_packet(ws, "not json at all")
        adapter._handle_packet(ws, "[1,2,3]")
        adapter._handle_packet(ws, json.dumps({"event": "posted", "data": None}))
        self.assertEqual(hooks.inbounds, [])

    def test_on_inbound_exception_does_not_escape(self):
        adapter, _ = make_adapter(hooks=ExplodingHooks())
        with self.assertLogs("opencode_bridge.adapters.mattermost", level="ERROR"):
            self.assertFalse(adapter._handle_packet(FakeWS(), post_frame("会炸")))
        self.assertFalse(adapter.running)


def post_payload_dict() -> dict:
    """一个字段齐全的 posted data（用于喂非 posted 事件，验证事件名白名单）。"""
    return {
        "id": "post_x",
        "user_id": OTHER_USER_ID,
        "channel_id": CHANNEL,
        "message": "hi",
        "type": "",
        "delete_at": 0,
        "file_ids": [],
    }


# ----------------------------------------------------------------------
# 9) 两个信封不混淆
# ----------------------------------------------------------------------
class TestEnvelopeDiscrimination(MattermostTestCase):
    def test_response_envelope_is_not_treated_as_event(self):
        adapter, hooks = make_adapter()
        ws = FakeWS()
        adapter._handle_packet(ws, response_frame("OK"))
        adapter._handle_packet(ws, json.dumps({
            "status": "FAIL", "seq_reply": 4,
            "error": {"message": "websocket request failed", "detailed_error": "boom"},
        }))
        self.assertEqual(hooks.inbounds, [], "响应信封不能被当成 posted 事件")
        self.assertIsNone(adapter._last_seq, "响应信封的 seq_reply 不是服务端事件 seq")
        self.assertFalse(adapter._got_hello)

    def test_response_with_post_like_data_is_ignored(self):
        """即使响应里带了 post 形状的 data（有 status），也不能投递 Inbound。"""
        adapter, hooks = make_adapter()
        adapter._handle_packet(FakeWS(), json.dumps({
            "status": "OK", "seq_reply": 1, "data": post_payload_dict(),
        }))
        self.assertEqual(hooks.inbounds, [])

    def test_event_envelope_with_data_containing_status_is_still_an_event(self):
        """判别看的是**信封**有没有 status，不是 data 里有没有。"""
        adapter, hooks = make_adapter()
        payload = post_payload_dict()
        payload["status"] = "OK"  # post 里带了个 status 字段（服务端不会，但别因此崩）
        adapter._handle_packet(FakeWS(), json.dumps(
            {"event": "posted", "data": payload, "seq": 9}
        ))
        self.assertEqual(len(hooks.inbounds), 1)


# ----------------------------------------------------------------------
# 10) 授权闸门
# ----------------------------------------------------------------------
class TestAccessGate(MattermostTestCase):
    def test_non_whitelisted_channel_is_dropped_before_inbound(self):
        adapter, hooks = make_adapter({"allowed_chat_ids": [CHANNEL]})
        adapter._handle_packet(FakeWS(), post_frame("授权", channel_id=CHANNEL))
        self.assertEqual(len(hooks.inbounds), 1)
        adapter._handle_packet(FakeWS(), post_frame("未授权", channel_id="c_stranger"))
        self.assertEqual(len(hooks.inbounds), 1, "未授权频道必须在产生 Inbound 之前丢")

    def test_empty_whitelist_admits_everything(self):
        adapter, hooks = make_adapter()
        self.assertEqual(adapter.allowed_chat_ids, set())
        adapter._handle_packet(FakeWS(), post_frame("任意频道", channel_id="c_any"))
        self.assertEqual(len(hooks.inbounds), 1)

    def test_gate_runs_before_command_parsing(self):
        """未授权者的 /approve 之类命令字必须拿不到（沿用基类 admits，不自建白名单）。"""
        adapter, hooks = make_adapter({"allowed_chats": ["c_ok"]})
        adapter._handle_packet(
            FakeWS(), post_frame("/approve 1", channel_id="c_bad")
        )
        self.assertEqual(hooks.inbounds, [])

    def test_admits_is_the_base_implementation(self):
        adapter, _ = make_adapter({"allowed_chat_ids": ["c_ok"]})
        from opencode_bridge.adapters.base import Adapter as BaseAdapter

        self.assertIs(type(adapter).admits, BaseAdapter.admits)


# ----------------------------------------------------------------------
# 11) 出站：发消息
# ----------------------------------------------------------------------
class TestSend(MattermostTestCase):
    def test_send_success_is_201_with_post_id(self):
        adapter, _ = make_adapter()
        calls = stub_rest(adapter, [(201, {"id": "post_new", "message": "hi"})])
        handle = adapter.send(Outbound(conversation_id=f"channel:{CHANNEL}", text="hi"))
        self.assertIsNotNone(handle)
        self.assertEqual(handle.message_id, "post_new")
        self.assertEqual(handle.platform, "mattermost")
        self.assertEqual(len(calls), 1)
        method, path, payload = calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(path, POSTS_PATH)
        self.assertEqual(payload, {"channel_id": CHANNEL, "message": "hi"})

    def test_2xx_without_post_id_is_still_a_failure(self):
        """判定是"2xx 且有 id"，所以 2xx 但没有 id 仍然算失败。"""
        adapter, _ = make_adapter()
        stub_rest(adapter, [(201, {"status_code": 400, "message": "bad request"})])
        self.assertIsNone(adapter.send(Outbound(conversation_id=f"channel:{CHANNEL}",
                                               text="hi")))

    def test_send_failure_status_is_not_success(self):
        adapter, _ = make_adapter()
        stub_rest(adapter, [(500, {"message": "Internal Server Error"})])
        self.assertIsNone(adapter.send(Outbound(conversation_id=f"channel:{CHANNEL}",
                                               text="hi")))
        self.assertIs(adapter.last_send_error, SendError.TRANSIENT)

    def test_send_result_is_observable_on_failure(self):
        adapter, _ = make_adapter()
        stub_rest(adapter, [(403, {"message": "Forbidden", "detailed_error": "no permission"})])
        result = adapter.send_result(Outbound(conversation_id=f"channel:{CHANNEL}",
                                              text="hi"))
        self.assertFalse(result.ok)
        self.assertIs(result.error_kind, SendError.FORBIDDEN)
        self.assertIn("permission", result.error_detail)

    def test_send_result_maps_429_to_rate_limited(self):
        adapter, _ = make_adapter()
        stub_rest(adapter, [(429, {"message": "Too many requests"})])
        result = adapter.send_result(Outbound(conversation_id=f"channel:{CHANNEL}",
                                              text="hi"))
        self.assertIs(result.error_kind, SendError.RATE_LIMITED)

    def test_send_result_maps_400_too_long(self):
        adapter, _ = make_adapter()
        stub_rest(adapter, [(400, {"message": "Message is too long"})])
        result = adapter.send_result(Outbound(conversation_id=f"channel:{CHANNEL}",
                                              text="hi"))
        self.assertIs(result.error_kind, SendError.TOO_LONG)

    def test_transport_failure_maps_to_transient(self):
        adapter, _ = make_adapter()
        stub_rest(adapter, [(0, {"message": "transport error: boom"})])
        result = adapter.send_result(Outbound(conversation_id=f"channel:{CHANNEL}",
                                              text="hi"))
        self.assertIs(result.error_kind, SendError.TRANSIENT)

    def test_rate_limit_retry_after_from_header(self):
        adapter, _ = make_adapter()
        import time as _time

        adapter._last_headers = {"x-ratelimit-reset": str(int(_time.time()) + 5)}
        delay = adapter._retry_after_seconds()
        self.assertIsNotNone(delay)
        assert delay is not None
        self.assertTrue(0 < delay <= 6, delay)
        adapter._last_headers = {}
        self.assertIsNone(adapter._retry_after_seconds())

    def test_send_refuses_bad_conversation_id_and_empty_text(self):
        adapter, _ = make_adapter()
        calls = stub_rest(adapter)
        self.assertIsNone(adapter.send(Outbound(conversation_id="", text="hi")))
        self.assertIsNone(adapter.send(Outbound(conversation_id=f"channel:{CHANNEL}",
                                               text="")))
        self.assertEqual(calls, [], "参数不合法时不该打 REST")
        self.assertIs(adapter.last_send_error, SendError.BAD_FORMAT)

    def test_send_refuses_without_credentials(self):
        adapter = MattermostAdapter({"site_url": SITE_URL}, RecordingHooks())
        adapter.min_interval = 0
        adapter._request = lambda *a, **k: self.fail("不该打 REST")  # type: ignore[method-assign]
        self.assertIsNone(adapter.send(Outbound(conversation_id=f"channel:{CHANNEL}",
                                               text="hi")))

    def test_partial_send_marks_partial(self):
        adapter, _ = make_adapter()
        adapter._apply_max_post_size(10)
        stub_rest(adapter, [
            (201, {"id": "p1"}),
            (500, {"message": "Internal Server Error"}),
        ])
        result = adapter.send_result(Outbound(conversation_id=f"channel:{CHANNEL}",
                                              text="x" * 25))
        self.assertTrue(result.ok, "第一片成功，整体算部分成功")
        self.assertTrue(result.partial)
        self.assertIs(result.error_kind, SendError.TRANSIENT)
        self.assertEqual(result.handle.message_id, "p1")


# ----------------------------------------------------------------------
# 12) 编辑必须用 /patch
# ----------------------------------------------------------------------
class TestEdit(MattermostTestCase):
    def test_edit_uses_patch_endpoint_not_put_post_id(self):
        adapter, _ = make_adapter()
        calls = stub_rest(adapter, [(200, {"id": "post_1", "message": "新正文"})])
        handle = MsgHandle(conversation_id=f"channel:{CHANNEL}",
                           message_id="post_1", platform="mattermost")
        self.assertTrue(adapter.edit(handle, Outbound(conversation_id=handle.conversation_id,
                                                      text="新正文")))
        self.assertEqual(len(calls), 1)
        method, path, payload = calls[0]
        self.assertEqual(method, "PUT")
        self.assertEqual(path, "api/v4/posts/post_1/patch")
        self.assertEqual(payload, {"message": "新正文"})
        # 关键回归断言：绝不能是 PUT api/v4/posts/post_1（那是整条替换，会清空未列字段）
        self.assertNotEqual(path, "api/v4/posts/post_1")
        self.assertTrue(path.endswith("/patch"))

    def test_edit_failure_returns_false_and_notes_error(self):
        adapter, _ = make_adapter()
        stub_rest(adapter, [(404, {"message": "Not found"})])
        handle = MsgHandle(conversation_id=f"channel:{CHANNEL}",
                           message_id="gone", platform="mattermost")
        self.assertFalse(adapter.edit(handle, Outbound(conversation_id=handle.conversation_id,
                                                       text="x")))
        self.assertIs(adapter.last_send_error, SendError.NOT_FOUND)

    def test_edit_rejects_bad_handle_and_empty_text(self):
        adapter, _ = make_adapter()
        calls = stub_rest(adapter)
        self.assertFalse(adapter.edit(MsgHandle(conversation_id=f"channel:{CHANNEL}",
                                                message_id="", platform="mattermost"),
                                      Outbound(conversation_id=f"channel:{CHANNEL}",
                                               text="x")))
        handle = MsgHandle(conversation_id=f"channel:{CHANNEL}",
                           message_id="p1", platform="mattermost")
        self.assertFalse(adapter.edit(handle, Outbound(conversation_id=handle.conversation_id,
                                                       text="")))
        self.assertEqual(calls, [])

    def test_answer_is_noop(self):
        adapter, _ = make_adapter()
        self.assertIsNone(adapter.answer("qid", "text"))


# ----------------------------------------------------------------------
# 13) MaxPostSize 运行时细化
# ----------------------------------------------------------------------
class TestMaxPostSize(MattermostTestCase):
    def test_max_post_size_refines_effective_limit(self):
        adapter, _ = make_adapter()
        # 官方返回的是**字符串**，必须 int() 转换
        stub_rest(adapter, [(200, {"config": {"MaxPostSize": "500"}})])
        self.assertEqual(adapter._fetch_max_post_size(), 500)
        self.assertEqual(adapter._apply_max_post_size(500), 500)
        self.assertEqual(adapter.effective_max_length, 500)

    def test_outbound_split_uses_runtime_limit(self):
        adapter, _ = make_adapter()
        adapter._apply_max_post_size(500)
        chunks = adapter._split_outbound("字" * 1200, CHANNEL)
        self.assertEqual(len(chunks), 3)
        self.assertTrue(all(len(c) <= 500 for c in chunks))
        self.assertEqual("".join(chunks), "字" * 1200)

    def test_static_floor_used_when_config_fetch_fails(self):
        adapter, _ = make_adapter()
        for reply in (
            (500, {"message": "Internal"}),
            (200, {}),
            (200, {"config": {}}),
            (200, {"config": {"MaxPostSize": "abc"}}),
            (200, {"config": {"MaxPostSize": "0"}}),
            (0, {"message": "transport error"}),
        ):
            with self.subTest(reply=reply):
                adapter._apply_max_post_size(None)
                stub_rest(adapter, [reply])
                self.assertIsNone(adapter._fetch_max_post_size())
                self.assertEqual(adapter._apply_max_post_size(None), 4000)
                self.assertEqual(adapter.effective_max_length, 4000)

    def test_start_refines_limit_without_raising(self):
        adapter, _ = make_adapter()
        adapter.user_id = MY_USER_ID  # 跳过 users/me
        calls = stub_rest(adapter, [(200, {"config": {"MaxPostSize": "600"}})])
        adapter._ws_factory = lambda url, **kw: BlockingWS()
        adapter.start()
        try:
            self.assertEqual(calls, [("GET", "api/v4/config/client?format=old", None)])
            self.assertEqual(adapter.effective_max_length, 600)
        finally:
            adapter.stop()

    def test_start_survives_rest_probe_failure(self):
        adapter, _ = make_adapter()
        adapter.user_id = MY_USER_ID
        adapter._request = lambda *a, **k: (_ for _ in ()).throw(  # type: ignore[method-assign]
            RuntimeError("network down")
        )
        adapter._ws_factory = lambda url, **kw: BlockingWS()
        adapter.start()  # 不抛
        try:
            self.assertTrue(adapter.running)
            self.assertEqual(adapter.effective_max_length, 4000)
        finally:
            adapter.stop()

    def test_users_me_caches_own_user_id(self):
        adapter, _ = make_adapter(user_id="")
        stub_rest(adapter, [(200, {"id": MY_USER_ID, "username": "bridge"})])
        adapter._load_self_user_id()
        self.assertEqual(adapter.user_id, MY_USER_ID)

    def test_users_me_failure_keeps_user_id_empty(self):
        adapter, _ = make_adapter(user_id="")
        stub_rest(adapter, [(401, {"message": "Unauthorized"})])
        with self.assertLogs("opencode_bridge.adapters.mattermost", level="WARNING"):
            adapter._load_self_user_id()
        self.assertEqual(adapter.user_id, "")

    def test_configured_user_id_skips_users_me(self):
        adapter, _ = make_adapter()
        calls = stub_rest(adapter)
        adapter._load_self_user_id()
        self.assertEqual(calls, [], "配置已给 user_id 时不必再问 REST")


# ----------------------------------------------------------------------
# 14) 帧尺寸
# ----------------------------------------------------------------------
class TestFrameLimit(MattermostTestCase):
    def test_every_outbound_chunk_stays_under_frame_limit(self):
        adapter, _ = make_adapter()
        chunks = adapter._split_outbound("a" * 20000, CHANNEL)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            body = adapter._post_body_bytes(CHANNEL, chunk)
            self.assertLess(body, WS_MAX_FRAME_BYTES, f"单片 {body} 字节超限")

    def test_wide_characters_get_byte_based_second_pass(self):
        """4 字节码点（emoji）按字符数切也会超帧预算 → 必须按字节再切一次。"""
        adapter, _ = make_adapter()
        adapter._apply_max_post_size(4000)
        text = "😀" * 9000
        chunks = adapter._split_outbound(text, CHANNEL)
        self.assertGreater(len(chunks), 2)
        for chunk in chunks:
            body = adapter._post_body_bytes(CHANNEL, chunk)
            self.assertLess(body, WS_MAX_FRAME_BYTES)
        self.assertEqual("".join(chunks), text)

    def test_challenge_response_frame_is_under_limit(self):
        adapter, _ = make_adapter()
        ws = FakeWS()
        adapter._handle_packet(
            ws,
            event_frame("authentication_challenge", {"challenge": "chal_" + "x" * 60}),
        )
        self.assertEqual(len(ws.sent), 1)
        reply = json.loads(ws.sent[0])
        self.assertEqual(reply, {"authentication_challenge_response": "chal_" + "x" * 60})
        self.assertLess(len(ws.sent[0].encode("utf-8")), WS_MAX_FRAME_BYTES)

    def test_oversized_challenge_response_is_refused(self):
        adapter, _ = make_adapter()
        ws = FakeWS()
        adapter._handle_packet(
            ws, event_frame("authentication_challenge", {"challenge": "x" * 9000})
        )
        self.assertEqual(ws.sent, [], "超过 8192 字节的帧不能发")

    def test_challenge_without_challenge_field_is_ignored(self):
        adapter, _ = make_adapter()
        ws = FakeWS()
        adapter._handle_packet(ws, event_frame("authentication_challenge", {}))
        self.assertEqual(ws.sent, [])

    def test_default_limit_is_below_frame_budget(self):
        adapter, _ = make_adapter()
        self.assertLessEqual(adapter.max_message_length, WS_MAX_FRAME_BYTES)


# ----------------------------------------------------------------------
# 15) 重连
# ----------------------------------------------------------------------
class TestReconnect(MattermostTestCase):
    def test_url_is_fresh_when_no_replay_credentials(self):
        adapter, _ = make_adapter()
        self.assertEqual(adapter._ws_url(),
                         "wss://mm.example.test/api/v4/websocket")

    def test_url_carries_connection_id_and_sequence_number(self):
        adapter, _ = make_adapter()
        adapter._connection_id = CONNECTION_ID
        adapter._last_seq = 42
        url = adapter._ws_url()
        self.assertIn("connection_id=" + CONNECTION_ID, url)
        self.assertIn("sequence_number=42", url)
        self.assertTrue(url.startswith("wss://mm.example.test/api/v4/websocket?"))

    def test_url_needs_both_params(self):
        """只给 connection_id 不给 sequence_number 等于没给 → 走全新连接。"""
        adapter, _ = make_adapter()
        adapter._connection_id = CONNECTION_ID
        self.assertNotIn("?", adapter._ws_url())
        adapter._last_seq = 42
        adapter._connection_id = None
        self.assertNotIn("?", adapter._ws_url())

    def test_auth_header_present_on_reconnect_too(self):
        adapter, _ = make_adapter()
        adapter._connection_id = CONNECTION_ID
        adapter._last_seq = 7
        captured: dict = {}

        def factory(url, **kw):
            captured["url"] = url
            captured.update(kw)
            return FakeWS()

        adapter._ws_factory = factory
        adapter._make_ws(adapter._ws_url())
        self.assertIn("connection_id=", captured["url"])
        self.assertEqual(captured["headers"]["Authorization"], f"Bearer {TOKEN}")


# ----------------------------------------------------------------------
# 16) 收包循环与生命周期
# ----------------------------------------------------------------------
class TestInboundLoop(MattermostTestCase):
    def setUp(self) -> None:
        super().setUp()
        # 退避调小，免得测试等 1~60 秒；改完立刻登记还原，别把常量泄漏给别的测试类。
        self.patch_module("RECONNECT_MIN", 0.01)
        self.patch_module("RECONNECT_MAX", 0.02)
        self.patch_module("WS_RECV_TIMEOUT", 0.05)

    def wait_for(self, predicate, timeout: float = 5.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.005)
        return predicate()

    def test_loop_consumes_events_and_reconnects(self):
        hooks = RecordingHooks()
        adapter, _ = make_adapter(hooks=hooks)
        adapter.user_id = MY_USER_ID
        sockets = [
            FakeWS([hello_frame(), post_frame("第一条")]),
            FakeWS([post_frame("重连后")]),
        ]
        created: list[FakeWS] = []
        urls: list[str] = []

        def factory(url, **kw):
            urls.append(url)
            ws = sockets[len(created)] if len(created) < len(sockets) else sockets[-1]
            created.append(ws)
            return ws

        adapter._ws_factory = factory
        adapter._request = lambda *a, **k: (200, {"config": {"MaxPostSize": "4000"}})  # type: ignore[method-assign]
        adapter.start()
        self.addCleanup(adapter.stop)
        self.assertTrue(self.wait_for(lambda: len(hooks.inbounds) >= 2))
        self.assertEqual([ib.text for ib in hooks.inbounds][:2], ["第一条", "重连后"])
        self.assertGreaterEqual(len(urls), 2, "断开后应重连")
        self.assertTrue(created[0].closed, "旧连接必须被关掉")

    def test_reconnect_after_hello_carries_replay_params(self):
        hooks = RecordingHooks()
        adapter, _ = make_adapter(hooks=hooks)
        adapter.user_id = MY_USER_ID
        sockets = [
            FakeWS([hello_frame(), post_frame("第一条", seq=15)]),
            FakeWS(),
        ]
        created: list[FakeWS] = []
        urls: list[str] = []

        def factory(url, **kw):
            urls.append(url)
            ws = sockets[len(created)] if len(created) < len(sockets) else sockets[-1]
            created.append(ws)
            return ws

        adapter._ws_factory = factory
        adapter._request = lambda *a, **k: (200, {"config": {"MaxPostSize": "4000"}})  # type: ignore[method-assign]
        adapter.start()
        self.addCleanup(adapter.stop)
        self.assertTrue(self.wait_for(lambda: len(urls) >= 2))
        # 第一条连接是全新连接；第二条要带 connection_id + sequence_number
        self.assertNotIn("connection_id", urls[0])
        self.assertIn(f"connection_id={CONNECTION_ID}", urls[1])
        self.assertIn("sequence_number=15", urls[1])

    def test_reconnect_without_hello_is_still_retryable(self):
        """没收到 hello（命中服务端队列续上）也要能一直重连。"""
        adapter, _ = make_adapter()
        adapter.user_id = MY_USER_ID
        count = {"n": 0}

        def factory(url, **kw):
            count["n"] += 1
            return FakeWS()

        adapter._ws_factory = factory
        adapter._request = lambda *a, **k: (200, {"config": {"MaxPostSize": "4000"}})  # type: ignore[method-assign]
        adapter.start()
        self.addCleanup(adapter.stop)
        self.assertTrue(self.wait_for(lambda: count["n"] >= 3))

    def test_connect_failure_is_logged_and_retried(self):
        adapter, _ = make_adapter()
        adapter.user_id = MY_USER_ID
        attempts = {"n": 0}

        def factory(url, **kw):
            attempts["n"] += 1
            raise OSError("connection refused")

        adapter._ws_factory = factory
        adapter._request = lambda *a, **k: (200, {"config": {"MaxPostSize": "4000"}})  # type: ignore[method-assign]
        adapter.start()
        self.addCleanup(adapter.stop)
        self.assertTrue(self.wait_for(lambda: attempts["n"] >= 2, 3.0))
        with self.assertLogs("opencode_bridge.adapters.mattermost", level="WARNING"):
            adapter._log_disconnect(None)

    def test_auth_failure_close_is_logged_as_error(self):
        """刚建连就断 + 从没 hello → 日志里必须出现可执行的鉴权诊断。"""
        adapter, _ = make_adapter()
        adapter.user_id = MY_USER_ID

        def factory(url, **kw):
            return FakeWS(close_code=1000)

        adapter._ws_factory = factory
        adapter._request = lambda *a, **k: (200, {"config": {"MaxPostSize": "4000"}})  # type: ignore[method-assign]
        with self.assertLogs("opencode_bridge.adapters.mattermost", level="WARNING") as cap:
            adapter.start()
            self.assertTrue(self.wait_for(lambda: not adapter.running or True, 0.3))
            adapter.stop()
        joined = "\n".join(cap.output)
        self.assertIn("鉴权失败", joined)
        self.assertIn("token", joined)

    def test_start_without_own_user_id_logs_error_but_still_connects(self):
        adapter, _ = make_adapter(user_id="")
        adapter._ws_factory = lambda url, **kw: FakeWS()
        adapter._request = lambda *a, **k: (200, {"config": {"MaxPostSize": "4000"}})  # type: ignore[method-assign]
        adapter.start()
        try:
            self.assertTrue(adapter.running)
            # 入站被挂起：一条都不该投递（否则自问自答）
            self.assertEqual(adapter._handle_posted(post_payload_dict()), False)
        finally:
            adapter.stop()

    def test_stop_ends_thread_promptly(self):
        adapter, _ = make_adapter()
        adapter.user_id = MY_USER_ID
        adapter._ws_factory = lambda url, **kw: BlockingWS()
        adapter._request = lambda *a, **k: (200, {"config": {"MaxPostSize": "4000"}})  # type: ignore[method-assign]
        started = time.time()
        adapter.start()
        self.assertTrue(self.wait_for(lambda: adapter.running, 2.0))
        adapter.stop()
        elapsed = time.time() - started
        self.assertFalse(adapter.running, "stop() 必须让线程结束")
        self.assertLess(elapsed, 3.0, f"stop 用了 {elapsed:.2f}s，说明没先关 WS")


# ----------------------------------------------------------------------
# 17) ping → pong 的保证来自 ws.py
# ----------------------------------------------------------------------
class TestPingPongGuarantee(unittest.TestCase):
    """Mattermost 的保活**完全依赖** ws.py 自动回 pong，这里把它锁住。

    服务端每 60 秒发一个 RFC 6455 ping 控制帧、100 秒没等到 pong 就断开。本适配器
    **不自己发心跳**（官方事件表里也没有 ``ping`` 事件可等），所以"收到 ping 必须有
    pong"这条保证只能来自 :mod:`opencode_bridge.ws`。如果哪天有人删掉那个分支，这组
    测试会立刻失败 —— 那正是我们要的。
    """

    def _drive_recv_with_ping(self, frames: list[bytes]) -> list[bytes]:
        """把帧喂给真 ``WebSocketClient`` 的 ``recv()``，返回它写出去的帧。"""
        import socket as socket_mod
        import threading

        from opencode_bridge import ws as ws_mod

        client, server = socket_mod.socketpair()
        self.addCleanup(client.close)
        self.addCleanup(server.close)

        writes: list[bytes] = []
        stop = threading.Event()

        def pump():
            while not stop.is_set():
                try:
                    chunk = server.recv(4096)
                except OSError:
                    break
                if not chunk:
                    break
                writes.append(chunk)

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        self.addCleanup(stop.set)

        # 用正式的 __init__ 构造，再把已建好的 socketpair 塞进去绕过真实握手。
        #
        # 此前这里是 ``WebSocketClient.__new__(...)`` + 手工逐个赋值 13 个私有属性。
        # 那是个**地雷**：``WebSocketClient`` 每新增一个实例属性（如``_prefetch``），
        # 这个测试就会因 AttributeError 挂掉——而它挂掉的原因与被测的 ping/pong
        # 保证毫无关系。``__init__`` 本身不做连接，所以这里没有理由绕过它。
        cli = ws_mod.WebSocketClient("ws://local", timeout=2.0)
        cli._sock = client
        cli._closed = False

        for frame in frames:
            server.sendall(frame)
        out = cli.recv()
        self.addCleanup(cli._shutdown)
        time.sleep(0.15)
        stop.set()
        reader.join(timeout=1.0)
        return writes, out

    @staticmethod
    def _text_frame(text: str) -> bytes:
        payload = text.encode("utf-8")
        return bytes([0x81, len(payload)]) + payload

    @staticmethod
    def _control_frame(opcode: int, payload: bytes = b"") -> bytes:
        assert len(payload) <= 125
        return bytes([0x80 | opcode, len(payload)]) + payload

    @staticmethod
    def _find_pong(joined: bytes) -> bytes | None:
        """在客户端发出的字节流里找出 pong 帧并**解掩码**返回 payload。

        客户端 → 服务端的每一帧都强制带掩码（RFC 6455 §5.3），所以 payload 在线上是
        异或过的，必须先还原才能比对内容。找不到返回 ``None``。
        """
        i = 0
        while i + 2 <= len(joined):
            b0, b1 = joined[i], joined[i + 1]
            opcode = b0 & 0x0F
            length = b1 & 0x7F
            masked = bool(b1 & 0x80)
            off = i + 2
            if length == 126:
                length = int.from_bytes(joined[off:off + 2], "big")
                off += 2
            elif length == 127:
                length = int.from_bytes(joined[off:off + 8], "big")
                off += 8
            mask = joined[off:off + 4] if masked else b""
            off += 4 if masked else 0
            payload = joined[off:off + length]
            if len(payload) < length:
                return None  # 读到的字节不够一整帧
            if masked:
                payload = bytes(b ^ mask[j % 4] for j, b in enumerate(payload))
            if opcode == 0x0A:
                return payload
            i = off + length
        return None

    def test_ping_frame_gets_a_pong_reply(self):
        writes, out = self._drive_recv_with_ping([
            self._control_frame(0x9, b"mm-keepalive"),
            self._text_frame('{"event":"hello","data":{}}'),
        ])
        self.assertEqual(out, '{"event":"hello","data":{}}')
        joined = b"".join(writes)
        self.assertTrue(joined, "收到 ping 必须发出 pong")
        pong = self._find_pong(joined)
        self.assertIsNotNone(pong, f"没有找到 pong 帧（opcode 0x0A）：{joined!r}")
        assert pong is not None
        # RFC 6455 §5.5.3：pong 必须原样带回 ping 的 payload
        self.assertEqual(pong, b"mm-keepalive")

    def test_empty_ping_still_gets_a_pong(self):
        writes, out = self._drive_recv_with_ping([
            self._control_frame(0x9),
            self._text_frame("{}"),
        ])
        self.assertEqual(out, "{}")
        self.assertEqual(self._find_pong(b"".join(writes)), b"")

    def test_server_pong_is_ignored_not_returned_to_caller(self):
        """官方事件表里没有 ping 事件，pong 也不是业务消息，必须被吃掉。"""
        writes, out = self._drive_recv_with_ping([
            self._control_frame(0x9, b"ping"),
            self._control_frame(0xA, b"pong"),
            self._text_frame('{"event":"hello","data":{}}'),
        ])
        self.assertEqual(out, '{"event":"hello","data":{}}')
        self.assertEqual(self._find_pong(b"".join(writes)), b"ping")

    def test_adapter_does_not_send_its_own_heartbeat_frames(self):
        """本适配器不自己发 ping —— 保证完全来自 ws.py 的自动回 pong。"""
        adapter, _ = make_adapter()
        ws = FakeWS([hello_frame(), post_frame("hi")])
        adapter._handle_packet(ws, hello_frame())
        adapter._handle_packet(ws, post_frame("hi"))
        self.assertEqual(ws.sent, [], "入站路径不发任何帧")

    def test_ws_source_really_auto_pongs(self):
        """直接读 ws.py 源码锁住那条分支（防有人"顺手清理"掉它）。"""
        import inspect

        from opencode_bridge import ws as ws_mod

        source = inspect.getsource(ws_mod.WebSocketClient._handle_control_frame)
        self.assertIn("_OP_PING", source)
        self.assertIn("_OP_PONG", source)


# ----------------------------------------------------------------------
# 18) 传输层接线值（G4 迁移）
# ----------------------------------------------------------------------
class TestTransportWiring(MattermostTestCase):
    """退避 / 钩子接线：这些值决定"什么时候重连、等多久重连"。

    迁移前它们散在 ``_inbound_loop`` 末尾那几行里，没有测试守着；迁移后它们成了
    构造 :class:`~opencode_bridge.transport.WebSocketTransport` 的四个实参 ——
    **值改错不会让任何功能用例失败**（重连只是慢一点/快一点），所以必须显式钉住。
    """

    def test_backoff_wiring_matches_the_legacy_constants(self):
        adapter, _ = make_adapter()
        transport = adapter._make_transport()
        self.assertIsInstance(transport, WebSocketTransport)
        self.assertEqual(mm_mod.RECONNECT_MIN, 1.0, "迁移前的下限就是 1s")
        self.assertEqual(mm_mod.RECONNECT_MAX, 60.0)
        self.assertEqual(transport.min_backoff, mm_mod.RECONNECT_MIN)
        self.assertEqual(transport.max_backoff, mm_mod.RECONNECT_MAX)
        self.assertEqual(
            transport.reset_after, mm_mod.RECONNECT_MIN,
            "迁移前的判定是 _connected_at and (now - _connected_at) >= RECONNECT_MIN；"
            "传 0（基类默认）会把'压根没连上'也当成'连上过'，于是建连失败不再 ×2 增长",
        )
        self.assertEqual(transport.label, "mattermost")
        self.assertEqual(transport._idle_delay(), 0.0,
                         "WS 类传输阻塞在 recv()，不该有空转节流")
        # 1s 起、×2、封顶 60s
        self.assertEqual(
            [transport._next_backoff(survived=False) for _ in range(4)],
            [1.0, 2.0, 4.0, 8.0],
        )

    def test_session_hooks_are_wired_to_the_adapter(self):
        adapter, _ = make_adapter()
        transport = adapter._make_transport()
        self.assertEqual(transport._on_message, adapter._handle_packet)
        # 断开诊断必须在会话结束时跑（迁移前是 _inbound_loop 的 finally）
        self.assertEqual(transport._on_close_fn, adapter._on_close)
        self.assertIsNone(transport._on_open_fn,
                          "Mattermost 没有'连上后要发什么'，不需要 on_open")
        self.assertEqual(transport._close_code, 1000,
                         "Mattermost 没有'必须用某个 close code'的要求")

    def test_thread_and_connection_belong_to_the_transport(self):
        adapter, _ = make_adapter()
        adapter.user_id = MY_USER_ID
        self.assertIsNone(adapter._thread, "入站线程归传输层所有，_thread 必须恒为 None")
        adapter._ws_factory = lambda url, **kw: BlockingWS()
        adapter._request = lambda *a, **k: (200, {"config": {"MaxPostSize": "4000"}})  # type: ignore[method-assign]
        adapter.start()
        try:
            self.assertTrue(adapter.running, "running 必须代理到传输层")
            self.assertIsInstance(adapter.transport, WebSocketTransport)
            self.assertIsNotNone(adapter.transport.connection)
        finally:
            adapter.stop()
        self.assertFalse(adapter.running)
        self.assertIsNone(adapter.transport, "stop() 之后传输层引用必须清掉")

    def test_running_is_overridden_not_inherited(self):
        """基类 ``running`` 读 ``self._thread``（永远是 None）⇒ 继承它就永远 False。"""
        from opencode_bridge.adapters.base import Adapter as BaseAdapter

        self.assertIn("running", MattermostAdapter.__dict__)
        self.assertIsNot(MattermostAdapter.running, BaseAdapter.running)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
"""B2 测试 —— Home Assistant 适配器（``adapters/homeassistant.py``）。

**零真实外网**：每个用例在 ``127.0.0.1:0`` 上起一个用标准库 ``socket`` +
``threading`` 写的**真 WebSocket 服务器**。服务端**自己**按 RFC 6455 算一遍
``Sec-WebSocket-Accept``（本文件里的实现与 ``ws.py`` 的 ``_accept_for`` 相互独立）
—— 一条链路上两边各算一遍，握手才有意义。服务端发帧**不加掩码**，读帧则校验并还原
客户端掩码，因此测的是真实协议行为。握手请求头逐条记下来，用来证明**凭据既不在
URL / query / header 里，也不出现在任何日志里**。

被测协议事实（全部在 ``adapters/homeassistant.py`` 的 docstring 里带出处）：

* 端点 ``/api/websocket``；握手**不带凭据**；
* 服务端先发 ``auth_required`` → 客户端发 ``{"type":"auth","access_token":...}``
  → 服务端回 ``auth_ok`` 或 ``auth_invalid``（随后断连）；
* 订阅用 ``{"id": N, "type": "subscribe_events", "event_type": "state_changed"}``；
* 事件是 ``{"id": N, "type": "event", "event": {...}}``；
* **ping 由客户端发、服务端回 pong**（本文件用真服务器锁死这条：断言服务端"只"
  收到客户端发来的 ping，没有反向的）。

覆盖：
注册表可发现 / ``outbound_tokens ⊆ required_tokens`` 不变量 / ``capabilities()``
与实现一致 / **认证握手流程**（含 auth_invalid）/ 订阅命令形状 / 事件过滤（未订阅的
事件类型、无白名单、无 ``user_id``、ignore 列表）/ ``edit()`` 恒 False 且不发任何命令
/ **客户端主动 ping** 与看门狗 / ``stop()`` 干净并断言耗时上界 / 畸形帧与畸形消息
不泄漏 traceback / **凭据不进 URL、不进握手 header、不进日志**。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import socket
import struct
import threading
import time
import unittest
from typing import Any, Callable, Dict, List, Optional

from opencode_bridge import identity
from opencode_bridge.adapters.base import (
    AdapterError,
    adapter_class,
    build,
    registered_names,
)
from opencode_bridge.adapters.homeassistant import (
    DEFAULT_EVENT_TYPES,
    HomeAssistantAdapter,
    MESSAGE_LIMIT,
    WS_PATH,
)
from opencode_bridge.hooks import Inbound, MsgHandle, Outbound, SendError

# ======================================================================
# 服务端：手写 RFC 6455 字节（**不 import ws.py 的实现** —— 两边各算一遍）
# ======================================================================
_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_OP_TEXT = 0x1
_OP_BINARY = 0x2
_OP_CLOSE = 0x8

#: 通用等待预算（秒）。这些用例要起真服务器、跑真线程，等待时长取决于**调度**而不是
#: 被测代码，所以给得宽；等待**成功**时用例立刻结束，几乎不花时间。
#: ⚠️ 本文件**不断言**"等了 X 秒"（那类断言栽过三次），只断言**内部状态**；
#: 唯一的时间上界断言是 ``stop()`` 的耗时（任务明确要求，且它是个上界不是等待）。
WAIT = 20.0


def _accept_for(key: str) -> str:
    """服务端侧独立实现的 ``base64(sha1(key + GUID))``。"""
    return base64.b64encode(
        hashlib.sha1(key.encode("ascii") + _GUID).digest()
    ).decode("ascii")


def _recv_exact(sock: socket.socket, size: int, timeout: float = 5.0) -> bytes:
    sock.settimeout(timeout)
    buf = bytearray()
    while len(buf) < size:
        chunk = sock.recv(size - len(buf))
        if not chunk:
            raise ConnectionError("连接被对端关闭，收不到预期字节")
        buf += chunk
    return bytes(buf)


def _handshake(sock: socket.socket, timeout: float = 5.0) -> Dict[str, str]:
    """读客户端握手请求、回 101，返回请求头字典（小写键）+ ``__request_line__``。"""
    sock.settimeout(timeout)
    buf = bytearray()
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(1)
        if not chunk:
            raise ConnectionError("握手请求没读完就断了")
        buf += chunk
    head = bytes(buf).decode("latin-1")
    lines = head.split("\r\n")
    headers: Dict[str, str] = {}
    for line in lines[1:]:
        key, sep, value = line.partition(":")
        if sep:
            headers[key.strip().lower()] = value.strip()
    key = headers.get("sec-websocket-key")
    if not key:
        raise AssertionError(f"握手请求里没有 Sec-WebSocket-Key: {head!r}")
    sock.sendall(
        b"HTTP/1.1 101 Switching Protocols\r\n"
        b"Upgrade: websocket\r\n"
        b"Connection: Upgrade\r\n"
        b"Sec-WebSocket-Accept: " + _accept_for(key).encode("ascii") + b"\r\n\r\n"
    )
    headers["__request_line__"] = lines[0]
    return headers


def send_frame(sock: socket.socket, opcode: int, payload: bytes = b"") -> None:
    """服务端发帧（按 RFC 服务端**不加掩码**）。"""
    head = bytearray()
    head.append(0x80 | opcode)
    size = len(payload)
    if size < 126:
        head.append(size)
    elif size < 65536:
        head.append(126)
        head += size.to_bytes(2, "big")
    else:
        head.append(127)
        head += size.to_bytes(8, "big")
    sock.sendall(bytes(head) + payload)


def recv_frame(sock: socket.socket, timeout: float = 5.0) -> tuple:
    """读客户端帧，返回 ``(fin, opcode, payload, masked)``；带掩码则还原。"""
    head = _recv_exact(sock, 2, timeout)
    b0, b1 = head[0], head[1]
    fin = bool(b0 & 0x80)
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    size = b1 & 0x7F
    if size == 126:
        size = int.from_bytes(_recv_exact(sock, 2, timeout), "big")
    elif size == 127:
        size = int.from_bytes(_recv_exact(sock, 8, timeout), "big")
    mask = _recv_exact(sock, 4, timeout) if masked else b""
    payload = _recv_exact(sock, size, timeout)
    if masked:
        payload = bytes(a ^ mask[i % 4] for i, a in enumerate(payload))
    return fin, opcode, payload, masked


class HAGateway:
    """在 ``127.0.0.1:0`` 上起一个真 Home Assistant 形状的 WS 服务器。

    每条连接一个处理线程，剧本固定为官方协议流程：

    1. ``auth_required``
    2. 收客户端的 ``auth``（令牌对上 → ``auth_ok``；对不上 → ``auth_invalid`` 后断连）
    3. 之后进入命令循环：``subscribe_events`` → ``result``、``ping`` → ``pong``、
       ``call_service`` → ``result``（失败形状可配置）

    测试可以在任意时刻从**另一个线程** :meth:`push` 事件（内部有发送锁，不会与
    命令回复的帧交错）。
    """

    def __init__(
        self,
        token: str = "ha-token-abc123",
        *,
        auth_ok: bool = True,
        subscribe_result: Optional[dict] = None,
        call_service_result: Optional[dict] = None,
        answer_ping: bool = True,
    ) -> None:
        self.token = token
        self.auth_ok = auth_ok
        #: ``subscribe_events`` 的 result 负载；``None`` 表示成功。
        self.subscribe_result = subscribe_result
        #: ``call_service`` 的 result 负载；``None`` 表示成功。
        self.call_service_result = call_service_result
        self.answer_ping = answer_ping

        self.handshakes: List[Dict[str, str]] = []
        self.auth_messages: List[dict] = []
        self.commands: List[dict] = []
        self.subscribes: List[dict] = []
        self.pings: List[dict] = []
        self.call_services: List[dict] = []
        self.errors: List[BaseException] = []

        self._send_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(8)
        self.port: int = self._listener.getsockname()[1]
        self._conns: List[socket.socket] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._accept_loop, name="ha-test-gateway", daemon=True
        )

    # -- 地址 ----------------------------------------------------------
    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}{WS_PATH}"

    # -- 生命周期 ------------------------------------------------------
    def start(self) -> "HAGateway":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        try:
            self._listener.close()
        except OSError:
            pass
        with self._state_lock:
            conns = list(self._conns)
        for conn in conns:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
        self._thread.join(3)

    def __enter__(self) -> "HAGateway":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def _accept_loop(self) -> None:
        self._listener.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                return
            with self._state_lock:
                self._conns.append(conn)
            threading.Thread(
                target=self._serve, args=(conn,), name="ha-test-conn", daemon=True
            ).start()

    # -- 每条连接 ------------------------------------------------------
    def _serve(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(5.0)
            headers = _handshake(conn, 5.0)
            with self._state_lock:
                self.handshakes.append(headers)
            if not self._do_auth(conn):
                return
            self._command_loop(conn)
        except (ConnectionError, TimeoutError, socket.timeout, OSError):
            pass                       # 测试收尾时连接被拆掉，属正常
        except AssertionError as exc:
            with self._state_lock:
                self.errors.append(exc)
        except BaseException as exc:  # noqa: BLE001 - 交给断言用
            with self._state_lock:
                self.errors.append(exc)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _do_auth(self, conn: socket.socket) -> bool:
        """官方顺序：``auth_required`` → 收 ``auth`` → ``auth_ok`` / ``auth_invalid``。"""
        self.send_json(conn, {"type": "auth_required", "ha_version": "2026.6.0"})
        auth = self.recv_json(conn)
        with self._state_lock:
            self.auth_messages.append(auth)
        if not self.auth_ok or auth.get("access_token") != self.token:
            self.send_json(
                conn,
                {"type": "auth_invalid", "message": "Invalid access token or password"},
            )
            return False               # 官方在 auth_invalid 之后直接断开
        self.send_json(conn, {"type": "auth_ok", "ha_version": "2026.6.0"})
        return True

    def _command_loop(self, conn: socket.socket) -> None:
        """收命令 → 回 result / pong。读超时**不是**断开，继续等。"""
        while not self._stop.is_set():
            try:
                cmd = self.recv_json(conn, timeout=0.2)
            except (TimeoutError, socket.timeout):
                continue                # 只是这一拍没数据
            except (ConnectionError, OSError, AssertionError):
                return                 # 连接断了
            kind = str(cmd.get("type") or "")
            with self._state_lock:
                self.commands.append(cmd)
                if kind == "subscribe_events":
                    self.subscribes.append(cmd)
                elif kind == "ping":
                    self.pings.append(cmd)
                elif kind == "call_service":
                    self.call_services.append(cmd)
            if kind == "subscribe_events":
                result = self.subscribe_result
                if result is None:
                    self.send_json(
                        conn,
                        {"id": cmd.get("id"), "type": "result", "success": True,
                         "result": None},
                    )
                else:
                    self.send_json(
                        conn,
                        {"id": cmd.get("id"), "type": "result",
                         "success": result.get("success", False),
                         "error": result.get("error")},
                    )
            elif kind == "ping":
                if self.answer_ping:
                    self.send_json(
                        conn, {"id": cmd.get("id"), "type": "pong"}
                    )
                # 故意不回 pong 时：什么都不做，让客户端的看门狗判死。
            elif kind == "call_service":
                result = self.call_service_result
                if result is None:
                    self.send_json(
                        conn,
                        {"id": cmd.get("id"), "type": "result", "success": True,
                         "result": {"context": {"id": "ctx-1", "user_id": None,
                                                "parent_id": None}}},
                    )
                else:
                    self.send_json(
                        conn,
                        {"id": cmd.get("id"), "type": "result",
                         "success": result.get("success", False),
                         "error": result.get("error"),
                         "result": result.get("result")},
                    )
            else:
                self.send_json(
                    conn,
                    {"id": cmd.get("id"), "type": "result", "success": False,
                     "error": {"code": "unknown_command", "message": "nope"}},
                )

    # -- 发 ------------------------------------------------------------
    def send_json(self, conn: socket.socket, obj: dict) -> None:
        with self._send_lock:
            send_frame(conn, _OP_TEXT, json.dumps(obj).encode("utf-8"))

    def send_text(self, conn: socket.socket, text: str) -> None:
        with self._send_lock:
            send_frame(conn, _OP_TEXT, text.encode("utf-8"))

    def recv_json(self, conn: socket.socket, timeout: float = 5.0) -> dict:
        _fin, opcode, payload, masked = recv_frame(conn, timeout)
        assert opcode == _OP_TEXT, f"期望 text 帧，实际 opcode=0x{opcode:x}"
        assert masked, "客户端发出的帧必须带掩码（RFC 6455 §5.3）"
        return json.loads(payload.decode("utf-8"))

    # -- 测试侧注入 ----------------------------------------------------
    def push(self, obj: dict, conn: Optional[socket.socket] = None) -> None:
        """推一条 ``event`` 消息（默认发给最近一条连接）。"""
        with self._state_lock:
            targets = [conn] if conn is not None else list(self._conns)
        for target in targets:
            if target is None:
                continue
            self.send_json(
                target,
                {"id": 1, "type": "event", "event": obj},
            )

    def push_raw_text(self, text: str) -> None:
        """推一条**非 JSON** 文本（畸形消息测试用）。"""
        with self._state_lock:
            targets = list(self._conns)
        for target in targets:
            if target is not None:
                self.send_text(target, text)

    def push_malformed(self, payload: Any) -> None:
        """推一条 JSON 但形状不对的消息（数组 / 空对象 / event 不是对象…）。"""
        with self._state_lock:
            targets = list(self._conns)
        for target in targets:
            if target is None:
                continue
            with self._send_lock:
                send_frame(target, _OP_TEXT, json.dumps(payload).encode("utf-8"))

    def types_of(self, kind: str) -> List[dict]:
        """按 ``type`` 取已收到的命令（快照）。"""
        with self._state_lock:
            return [c for c in self.commands if c.get("type") == kind]

    @property
    def command_types(self) -> List[str]:
        with self._state_lock:
            return [str(c.get("type") or "") for c in self.commands]


# ----------------------------------------------------------------------
# 通用小工具
# ----------------------------------------------------------------------
def wait_for(predicate: Callable[[], bool], timeout: float = WAIT) -> bool:
    """轮询到 ``predicate`` 为真；超时返回 False（**不断言**它必须为真）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class RecordingHooks:
    """收集 ``on_inbound`` / ``on_callback`` 的最小 Hooks 实现。"""

    def __init__(self) -> None:
        self.inbounds: List[Inbound] = []
        self.callbacks: List[tuple] = []
        self._event = threading.Event()
        self._lock = threading.Lock()

    def on_inbound(self, inbound: Inbound) -> None:
        with self._lock:
            self.inbounds.append(inbound)
        self._event.set()

    def on_callback(self, conversation_id: str, data: str, query_id: str) -> None:
        with self._lock:
            self.callbacks.append((conversation_id, data, query_id))

    def wait_for_inbound(self, count: int = 1) -> bool:
        """等到第 ``count`` 条入站出现（按**内部状态**判定，不看时钟）。"""
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline:
            with self._lock:
                if len(self.inbounds) >= count:
                    return True
            self._event.clear()
            self._event.wait(0.05)
        with self._lock:
            return len(self.inbounds) >= count

    @property
    def count(self) -> int:
        with self._lock:
            return len(self.inbounds)

    def last(self) -> Optional[Inbound]:
        with self._lock:
            return self.inbounds[-1] if self.inbounds else None


def state_changed(
    entity_id: str = "binary_sensor.front_door",
    old: str = "off",
    new: str = "on",
    *,
    user_id: Optional[str] = "u-alice",
    context_id: str = "ctx-abc",
    friendly_name: str = "前门",
    domain: str = "",
) -> dict:
    """造一条形状与官方文档一致的 ``state_changed`` 事件。"""
    if not domain:
        domain = entity_id.partition(".")[0]

    def _state(state: Optional[str]) -> Optional[dict]:
        if state is None:
            return None
        return {
            "entity_id": entity_id,
            "state": state,
            "attributes": {"friendly_name": friendly_name},
            "last_changed": "2026-06-15T00:00:00+00:00",
            "last_updated": "2026-06-15T00:00:00+00:00",
            "context": {"id": context_id, "parent_id": None, "user_id": user_id},
        }

    return {
        "event_type": "state_changed",
        "data": {
            "entity_id": entity_id,
            "old_state": _state(old),
            "new_state": _state(new),
        },
        "origin": "LOCAL",
        "time_fired": "2026-06-15T00:00:00+00:00",
        "context": {"id": context_id, "parent_id": None, "user_id": user_id},
    }


TOKEN = "ha-token-abc123"


class AdapterCase(unittest.TestCase):
    """共用脚手架：起真服务器 → 建适配器 → 保证收尾一定 ``stop()``。"""

    def make_gateway(self, **kw: Any) -> HAGateway:
        gw = HAGateway(**kw)
        gw.start()
        self.addCleanup(gw.stop)
        return gw

    def make_adapter(self, gw: HAGateway, hooks: RecordingHooks, **cfg: Any) -> HomeAssistantAdapter:
        config = {"url": gw.base_url, "token": TOKEN}
        config.update(cfg)
        adapter = build("homeassistant", config, hooks)
        assert isinstance(adapter, HomeAssistantAdapter)
        self.addCleanup(adapter.stop)
        return adapter

    def start_and_wait_authed(self, adapter: HomeAssistantAdapter, gw: HAGateway) -> None:
        adapter.start()
        self.assertTrue(
            wait_for(lambda: adapter.authenticated and gw.subscribes),
            f"适配器没完成握手；auth={gw.auth_messages} subs={gw.subscribes}",
        )


# ======================================================================
# 1) 注册 / 凭据不变量 / 能力声明
# ======================================================================
class TestRegistrationAndDeclarations(unittest.TestCase):
    def test_discoverable_through_registry(self):
        self.assertIn("homeassistant", registered_names())
        cls = adapter_class("homeassistant")
        self.assertIs(cls, HomeAssistantAdapter)
        self.assertEqual(cls.name, "homeassistant")

    def test_build_from_registry(self):
        adapter = build("homeassistant", {"url": "http://x:8123", "token": "t"}, RecordingHooks())
        self.addCleanup(adapter.stop)
        self.assertEqual(adapter.name, "homeassistant")

    def test_outbound_tokens_subset_of_required_tokens(self):
        """仓库硬不变量：**能发出去**的前提一定是**已配置**的子集。

        历史教训：Matrix 忘声明 ``required_tokens``，配得完全正确的用户被判成没配。
        """
        cls = adapter_class("homeassistant")
        required = set(cls.required_tokens)
        outbound = set(cls.outbound_tokens)
        self.assertTrue(required, "required_tokens 不能为空（必须声明自己的配置面）")
        self.assertTrue(outbound, "outbound_tokens 不能为空")
        self.assertTrue(
            outbound <= required,
            f"outbound_tokens {sorted(outbound)} 不是 required_tokens {sorted(required)} 的子集",
        )
        # 凭据键要如实覆盖实现真正读的那两个
        self.assertEqual(sorted(required), ["token", "url"])

    def test_capabilities_are_truthful(self):
        hooks = RecordingHooks()
        adapter = build(
            "homeassistant",
            {"url": "http://127.0.0.1:1", "token": "t", "entities": ["light.x"]},
            hooks,
        )
        self.addCleanup(adapter.stop)
        caps = adapter.capabilities()
        self.assertEqual(caps["name"], "homeassistant")
        self.assertEqual(caps["label"], "Home Assistant")
        self.assertEqual(caps["max_message_length"], MESSAGE_LIMIT)
        self.assertEqual(caps["max_message_length"], adapter.max_message_length)
        # 与实现一致：收 WS 事件；没有按钮、没有媒体、没有"配置可省"。
        self.assertTrue(caps["supports_inbound"])
        self.assertFalse(caps["supports_inline_buttons"])
        self.assertFalse(caps["supports_media"])
        self.assertFalse(caps["config_optional"])
        self.assertFalse(caps["running"], "还没 start() 就该是 False")
        self.assertEqual(caps["allowed_chat_ids_count"], 0)

    def test_capabilities_running_reflects_transport(self):
        """本平台的线程归 Transport 所有 ⇒ ``running`` 必须代理（不变量）。"""
        hooks = RecordingHooks()
        adapter = build(
            "homeassistant", {"url": "http://127.0.0.1:1", "token": "t"}, hooks
        )
        self.addCleanup(adapter.stop)
        self.assertIn("running", type(adapter).__dict__)
        self.assertFalse(adapter.running)

    def test_edit_returns_false_and_capabilities_do_not_claim_more(self):
        adapter = build(
            "homeassistant", {"url": "http://127.0.0.1:1", "token": "t"}, RecordingHooks()
        )
        self.addCleanup(adapter.stop)
        handle = MsgHandle("homeassistant:light.x", "ctx-1", "homeassistant")
        self.assertIs(adapter.edit(handle, Outbound("homeassistant:light.x", "改了")), False)
        self.assertEqual(adapter.send_result(
            Outbound("homeassistant:light.x", "x")).ok, False)


class TestConversationId(unittest.TestCase):
    def test_format_id_roundtrip(self):
        cid = HomeAssistantAdapter.conversation_id_for("binary_sensor.front_door")
        self.assertEqual(cid, "homeassistant:binary_sensor.front_door")
        self.assertEqual(cid, identity.format_id("homeassistant", "binary_sensor.front_door"))
        self.assertEqual(HomeAssistantAdapter.split_conversation(cid),
                         "binary_sensor.front_door")
        # 平台段可解析，且不会与别的平台撞车
        self.assertEqual(identity.parse_id(cid).platform, "homeassistant")

    def test_rejects_foreign_and_legacy_ids(self):
        split = HomeAssistantAdapter.split_conversation
        self.assertIsNone(split("light.kitchen"))               # 没有平台前缀
        self.assertIsNone(split("channel:1"))                  # 迁移前的歧义前缀
        self.assertIsNone(split(""))                            # 空
        self.assertIsNone(split("slack:light.kitchen"))         # 别的平台
        self.assertIsNone(split(None))


# ======================================================================
# 2) 认证握手（含失败）
# ======================================================================
class TestAuthHandshake(AdapterCase):
    def test_full_handshake_command_shapes(self):
        gw = self.make_gateway()
        hooks = RecordingHooks()
        adapter = self.make_adapter(gw, hooks, entities=["binary_sensor.front_door"])
        self.start_and_wait_authed(adapter, gw)

        # a) 握手 URL 必须是 HA 的 /api/websocket，且**不带任何凭据**
        self.assertEqual(len(gw.handshakes), 1)
        request_line = gw.handshakes[0]["__request_line__"]
        self.assertTrue(request_line.startswith("GET "), request_line)
        self.assertIn(WS_PATH, request_line)
        self.assertEqual(adapter.ws_url(), gw.url)

        # b) auth 消息：只有 type + access_token，**不能带 id**（官方 schema 会拒）
        self.assertEqual(len(gw.auth_messages), 1)
        auth = gw.auth_messages[0]
        self.assertEqual(auth.get("type"), "auth")
        self.assertEqual(auth.get("access_token"), TOKEN)
        self.assertNotIn("id", auth, "auth 消息不能带 id（官方 AUTH_MESSAGE_SCHEMA）")
        self.assertEqual(set(auth), {"type", "access_token"})

        # c) 订阅：每种事件类型一条命令，id 递增
        self.assertEqual(len(gw.subscribes), 1)
        sub = gw.subscribes[0]
        self.assertEqual(sub.get("type"), "subscribe_events")
        self.assertEqual(sub.get("event_type"), "state_changed")
        self.assertIsInstance(sub.get("id"), int)
        self.assertEqual(sub["id"], auth.get("id", 1) if "id" in auth else 1)
        self.assertEqual(adapter.subscribed_event_types, {"state_changed"})
        self.assertEqual(adapter.event_types, DEFAULT_EVENT_TYPES)

        # d) 握手之后服务端才开始推事件 —— 认证没过就不该有任何入站
        self.assertEqual(hooks.count, 0)

    def test_auth_failure_is_reported_without_leaking_token(self):
        gw = self.make_gateway(auth_ok=False)
        hooks = RecordingHooks()
        adapter = self.make_adapter(gw, hooks, entities=["light.x"])

        with self.assertLogs("opencode_bridge.adapters.homeassistant", level="DEBUG") as cap:
            adapter.start()
            # 官方在 auth_invalid 之后直接断开 ⇒ 我们会退避重连（至少两次握手）
            self.assertTrue(
                wait_for(lambda: len(gw.handshakes) >= 2),
                f"鉴权失败后应当退避重连，只握手了 {len(gw.handshakes)} 次",
            )

        blob = "\n".join(r.getMessage() for r in cap.records)
        self.assertIn("鉴权被拒", blob)
        self.assertIn("auth_invalid", blob)
        # 断言失败**没有任何入站**（鉴权没过就不该有对话）
        self.assertFalse(adapter.authenticated)
        self.assertEqual(hooks.count, 0)
        self.assertEqual(adapter.subscribed_event_types, set())
        # 失败原因要留下来给排障用（且**不含令牌**）
        self.assertIn("Invalid access token", adapter.last_auth_error or "")
        # ⚠️ 令牌绝不能出现在任何一条日志里
        self.assertNotIn(TOKEN, blob)
        # 也不该有任何异常栈（auth_invalid 是预期路径，不是"意外"）
        self.assertNotIn("Traceback", blob)

    def test_missing_token_refuses_to_start(self):
        gw = self.make_gateway()
        adapter = build("homeassistant", {"url": gw.base_url}, RecordingHooks())
        self.addCleanup(adapter.stop)
        with self.assertLogs("opencode_bridge.adapters.homeassistant", level="WARNING") as cap:
            adapter.start()
        self.assertFalse(adapter.running)
        self.assertEqual(gw.handshakes, [])
        self.assertIn("token missing", "\n".join(r.getMessage() for r in cap.records))

    def test_unauthorized_subscription_is_diagnosed(self):
        """``unauthorized`` 是"订阅类型不在白名单且用户不是管理员"（通配 ``*`` 必踩）。"""
        gw = self.make_gateway(
            subscribe_result={
                "success": False,
                "error": {"code": "unauthorized", "message": "Unauthorized"},
            }
        )
        hooks = RecordingHooks()
        adapter = self.make_adapter(gw, hooks, event_types=["*"], accept_all=True)
        with self.assertLogs("opencode_bridge.adapters.homeassistant", level="ERROR") as cap:
            adapter.start()
            self.assertTrue(
                wait_for(lambda: len(gw.subscribes) >= 1),
                "适配器应该尝试过通配订阅",
            )
            # 一条都没订上 ⇒ 会话失败并退避重连（第二次握手即可观测）
            self.assertTrue(wait_for(lambda: len(gw.handshakes) >= 2))

        blob = "\n".join(r.getMessage() for r in cap.records)
        self.assertIn("unauthorized", blob)
        self.assertIn("SUBSCRIBE_ALLOWLIST", blob)
        self.assertFalse(adapter.authenticated)
        self.assertNotIn(TOKEN, blob)

    def test_first_message_must_be_auth_required(self):
        """连到的不是 HA 的 WS 端点时，诊断必须说清而不是干瞪眼。"""
        gw = self.make_gateway()

        def bad_script(conn: socket.socket) -> None:  # pragma: no cover - 见下
            pass

        # 直接替换真服务器的 _do_auth：先发一条别的消息。
        original = gw._do_auth

        def _do_auth(conn: socket.socket) -> bool:
            gw.send_json(conn, {"type": "result", "success": True, "result": None})
            return False

        gw._do_auth = _do_auth  # type: ignore[method-assign]
        self.addCleanup(setattr, gw, "_do_auth", original)

        hooks = RecordingHooks()
        adapter = self.make_adapter(gw, hooks, entities=["light.x"])
        with self.assertLogs("opencode_bridge.adapters.homeassistant", level="WARNING") as cap:
            adapter.start()
            self.assertTrue(wait_for(lambda: len(gw.handshakes) >= 2))
        blob = "\n".join(r.getMessage() for r in cap.records)
        self.assertIn("auth_required", blob)
        self.assertFalse(adapter.authenticated)


# ======================================================================
# 3) 事件过滤
# ======================================================================
class TestEventFiltering(AdapterCase):
    def test_default_drops_everything_and_warns(self):
        """**默认保守**：没配 entities / domains / accept_all ⇒ 一个事件都不收。"""
        gw = self.make_gateway()
        hooks = RecordingHooks()
        adapter = self.make_adapter(gw, hooks)
        with self.assertLogs("opencode_bridge.adapters.homeassistant", level="WARNING") as cap:
            self.start_and_wait_authed(adapter, gw)
            gw.push(state_changed("light.kitchen"))
            # 状态条件：确认服务端确实推过帧之后，再断言"没收到"（不用时钟）
            self.assertTrue(wait_for(lambda: gw._conns))
            self.assertFalse(
                wait_for(lambda: hooks.count > 0, timeout=1.0),
                "默认配置下不该产生任何入站",
            )
        blob = "\n".join(r.getMessage() for r in cap.records)
        self.assertIn("默认一个事件都不收", blob)
        self.assertEqual(hooks.count, 0)
        self.assertFalse(adapter._accepts_everything())

    def test_unrecognized_event_type_is_dropped(self):
        """没订阅的事件类型（服务端误推 / 中间有代理乱塞）必须丢。"""
        gw = self.make_gateway()
        hooks = RecordingHooks()
        adapter = self.make_adapter(gw, hooks, entities=["binary_sensor.front_door"])
        self.start_and_wait_authed(adapter, gw)

        self.assertEqual(adapter.event_types, DEFAULT_EVENT_TYPES)
        gw.push({
            "event_type": "component_loaded",
            "data": {"component": "hue"},
            "origin": "LOCAL",
            "time_fired": "2026-06-15T00:00:00+00:00",
            "context": {"id": "c1", "parent_id": None, "user_id": "u-alice"},
        })
        self.assertFalse(
            wait_for(lambda: hooks.count > 0, timeout=1.0),
            "未订阅的事件类型不该变成对话",
        )
        # 同一连接上再来一条 state_changed —— 必须仍然收得到（证明只是过滤，不是断连）
        gw.push(state_changed())
        self.assertTrue(hooks.wait_for_inbound(1))
        self.assertEqual(hooks.last().conversation_id,
                         "homeassistant:binary_sensor.front_door")

    def test_wildcard_subscription_accepts_other_event_types(self):
        """``event_types: ["*"]`` = 显式"我全收"（⚠️ 需要管理员令牌）。"""
        gw = self.make_gateway()
        hooks = RecordingHooks()
        adapter = self.make_adapter(gw, hooks, event_types=["*"], accept_all=True)
        self.start_and_wait_authed(adapter, gw)

        self.assertEqual(adapter.event_types, ("*",))
        self.assertEqual(gw.subscribes[0]["type"], "subscribe_events")
        # 通配订阅**不带** event_type 键（官方：省略即 MATCH_ALL）
        self.assertNotIn("event_type", gw.subscribes[0])
        gw.push({
            "event_type": "component_loaded",
            "data": {"entity_id": "light.kitchen", "component": "hue"},
            "origin": "LOCAL",
            "time_fired": "2026-06-15T00:00:00+00:00",
            "context": {"id": "c1", "parent_id": None, "user_id": "u-alice"},
        })
        self.assertTrue(hooks.wait_for_inbound(1))
        self.assertIn("component_loaded", hooks.last().text)

    def test_require_user_context_drops_machine_events(self):
        """``context.user_id`` 为空 = 不是某个人的操作 ⇒ 默认不当对话。"""
        gw = self.make_gateway()
        hooks = RecordingHooks()
        adapter = self.make_adapter(gw, hooks, entities=["binary_sensor.front_door"])
        self.assertTrue(adapter.require_user_context)
        self.start_and_wait_authed(adapter, gw)

        gw.push(state_changed(user_id=None))          # 定时器 / 脚本触发
        self.assertFalse(
            wait_for(lambda: hooks.count > 0, timeout=1.0),
            "user_id 为空的事件不该变成对话",
        )
        gw.push(state_changed(user_id="u-bob"))       # 真人操作
        self.assertTrue(hooks.wait_for_inbound(1))
        self.assertEqual(hooks.last().user_id, "u-bob")
        self.assertEqual(hooks.last().message_id, "ctx-abc")
        self.assertEqual(hooks.last().platform, "homeassistant")

    def test_require_user_context_can_be_disabled_explicitly(self):
        gw = self.make_gateway()
        hooks = RecordingHooks()
        adapter = self.make_adapter(
            gw, hooks, entities=["binary_sensor.front_door"],
            require_user_context=False,
        )
        self.assertFalse(adapter.require_user_context)
        self.start_and_wait_authed(adapter, gw)
        gw.push(state_changed(user_id=None))
        self.assertTrue(hooks.wait_for_inbound(1))
        self.assertIsNone(hooks.last().user_id)

    def test_entity_domain_and_ignore_filters(self):
        gw = self.make_gateway()
        hooks = RecordingHooks()
        adapter = self.make_adapter(
            gw, hooks, entities=["light.kitchen"], domains=["binary_sensor"],
            ignore_entities=["binary_sensor.garage"],
        )
        self.start_and_wait_authed(adapter, gw)

        gw.push(state_changed("switch.garage_door"))     # 既不在 entities 也不在 domains
        self.assertFalse(wait_for(lambda: hooks.count > 0, timeout=1.0))
        gw.push(state_changed("binary_sensor.garage"))   # 在 domains，但被 ignore
        self.assertFalse(wait_for(lambda: hooks.count > 0, timeout=1.0))
        gw.push(state_changed("light.kitchen"))          # 精确命中 entities
        self.assertTrue(hooks.wait_for_inbound(1))
        self.assertEqual(hooks.last().conversation_id, "homeassistant:light.kitchen")
        self.assertIn("light.kitchen", hooks.last().text)

    def test_allowlist_gates_before_inbound(self):
        """授权闸门必须在产生 Inbound **之前**（不能用命令字绕过，不变量 3）。"""
        gw = self.make_gateway()
        hooks = RecordingHooks()
        adapter = self.make_adapter(
            gw, hooks, domains=["light"], allowed_chat_ids=["light.kitchen"],
        )
        self.start_and_wait_authed(adapter, gw)
        gw.push(state_changed("light.studio", new="/approve"))
        self.assertFalse(wait_for(lambda: hooks.count > 0, timeout=1.0))
        gw.push(state_changed("light.kitchen"))
        self.assertTrue(hooks.wait_for_inbound(1))
        self.assertEqual(hooks.last().conversation_id, "homeassistant:light.kitchen")

    def test_entity_removed_and_unroutable_events_dropped(self):
        gw = self.make_gateway()
        hooks = RecordingHooks()
        adapter = self.make_adapter(gw, hooks, accept_all=True)
        self.start_and_wait_authed(adapter, gw)

        removed = state_changed("light.kitchen")
        removed["data"]["new_state"] = None            # 实体被删：没有新状态
        gw.push(removed)
        no_entity = {"event_type": "state_changed", "data": {"foo": "bar"},
                     "origin": "LOCAL", "time_fired": "x", "context": {}}
        gw.push(no_entity)
        self.assertFalse(wait_for(lambda: hooks.count > 0, timeout=1.0))
        gw.push(state_changed("light.kitchen"))         # 连接仍然可用
        self.assertTrue(hooks.wait_for_inbound(1))

    def test_non_state_changed_event_renders_and_routes(self):
        gw = self.make_gateway()
        hooks = RecordingHooks()
        adapter = self.make_adapter(
            gw, hooks, event_types=["automation_triggered"], entities=["light.kitchen"],
        )
        self.start_and_wait_authed(adapter, gw)
        gw.push({
            "event_type": "automation_triggered",
            "data": {"entity_id": "light.kitchen", "skip_condition": False},
            "origin": "LOCAL",
            "time_fired": "2026-06-15T00:00:00+00:00",
            "context": {"id": "ctx-9", "parent_id": None, "user_id": "u-carol"},
        })
        self.assertTrue(hooks.wait_for_inbound(1))
        inbound = hooks.last()
        self.assertEqual(inbound.conversation_id, "homeassistant:light.kitchen")
        self.assertIn("automation_triggered", inbound.text)
        self.assertEqual(inbound.user_id, "u-carol")


# ======================================================================
# 4) 出站 / edit
# ======================================================================
class TestOutbound(AdapterCase):
    def test_edit_returns_false_and_sends_nothing(self):
        gw = self.make_gateway()
        hooks = RecordingHooks()
        adapter = self.make_adapter(gw, hooks, entities=["light.kitchen"])
        adapter.ping_interval = 0.1
        self.start_and_wait_authed(adapter, gw)
        before = len(gw.command_types)

        handle = MsgHandle("homeassistant:light.kitchen", "ctx-1", "homeassistant")
        self.assertIs(
            adapter.edit(handle, Outbound("homeassistant:light.kitchen", "改一下")), False
        )
        # 用**状态条件**而不是时钟证明"什么都没发"：等看门狗至少发过一次 ping
        # （ping 在服务端被回 pong，所以那一刻之后的命令列表就是定论）。
        self.assertTrue(wait_for(lambda: len(gw.types_of("ping")) >= 1))
        self.assertEqual(len(gw.command_types), before + 1)
        self.assertEqual(gw.call_services, [])

    def test_send_calls_service_and_returns_handle(self):
        gw = self.make_gateway()
        hooks = RecordingHooks()
        adapter = self.make_adapter(gw, hooks, entities=["light.kitchen"])
        self.start_and_wait_authed(adapter, gw)

        handle = adapter.send(Outbound("homeassistant:light.kitchen", "厨房灯已关"))
        self.assertIsNotNone(handle)
        self.assertEqual(handle.conversation_id, "homeassistant:light.kitchen")
        self.assertEqual(handle.platform, "homeassistant")
        self.assertEqual(handle.message_id, "ctx-1")

        self.assertEqual(len(gw.call_services), 1)
        cmd = gw.call_services[0]
        self.assertEqual(cmd["type"], "call_service")
        self.assertEqual(cmd["domain"], "persistent_notification")
        self.assertEqual(cmd["service"], "create")
        self.assertEqual(cmd["service_data"]["message"], "厨房灯已关")
        self.assertIn("title", cmd["service_data"])
        self.assertEqual(cmd["target"], {"entity_id": ["light.kitchen"]})
        # id 必须严格递增（官方 ERR_ID_REUSE）
        self.assertGreater(cmd["id"], gw.subscribes[0]["id"])

    def test_send_result_maps_error_code(self):
        gw = self.make_gateway(
            call_service_result={
                "success": False,
                "error": {"code": "unauthorized", "message": "Unauthorized"},
            }
        )
        hooks = RecordingHooks()
        adapter = self.make_adapter(gw, hooks, entities=["light.kitchen"])
        self.start_and_wait_authed(adapter, gw)

        result = adapter.send_result(Outbound("homeassistant:light.kitchen", "hi"))
        self.assertFalse(result.ok)
        self.assertIs(result.handle, None)
        self.assertIs(result.error_kind, SendError.FORBIDDEN)
        self.assertEqual(result.platform, "homeassistant")

    def test_send_without_connection_is_transient_not_fake_success(self):
        gw = self.make_gateway()
        hooks = RecordingHooks()
        adapter = self.make_adapter(gw, hooks, entities=["light.kitchen"])
        # 故意不 start() ⇒ 没有连接
        result = adapter.send_result(Outbound("homeassistant:light.kitchen", "hi"))
        self.assertFalse(result.ok)
        self.assertIs(result.error_kind, SendError.TRANSIENT)
        self.assertIn("连接不可用", result.error_detail)

    def test_bad_conversation_id_is_bad_format(self):
        adapter = build(
            "homeassistant", {"url": "http://127.0.0.1:1", "token": "t"}, RecordingHooks()
        )
        self.addCleanup(adapter.stop)
        result = adapter.send_result(Outbound("light.kitchen", "hi"))
        self.assertFalse(result.ok)
        self.assertIs(result.error_kind, SendError.BAD_FORMAT)

    def test_long_text_is_split_into_multiple_service_calls(self):
        gw = self.make_gateway()
        hooks = RecordingHooks()
        adapter = self.make_adapter(gw, hooks, entities=["light.kitchen"])
        self.start_and_wait_authed(adapter, gw)
        adapter.message_limit = 10
        adapter.send(Outbound("homeassistant:light.kitchen", "0123456789abcdefghij"))
        self.assertEqual(len(gw.call_services), 2)
        joined = "".join(c["service_data"]["message"] for c in gw.call_services)
        self.assertEqual(joined, "0123456789abcdefghij")

    def test_own_service_call_echo_is_suppressed(self):
        """防回环：我们自己调服务造成的 state_changed 不能又变成对话。"""
        gw = self.make_gateway()
        hooks = RecordingHooks()
        adapter = self.make_adapter(gw, hooks, domains=["light"])
        self.start_and_wait_authed(adapter, gw)

        self.assertIsNotNone(adapter.send(Outbound("homeassistant:light.kitchen", "已关灯")))
        self.assertTrue(len(gw.call_services) >= 1)
        gw.push(state_changed("light.kitchen"))          # 我们自己造成的回声
        self.assertFalse(
            wait_for(lambda: hooks.count > 0, timeout=1.0),
            "自己刚调完服务就回声 = 自问自答回环",
        )
        gw.push(state_changed("light.study"))            # 别的实体不受影响
        self.assertTrue(hooks.wait_for_inbound(1))
        self.assertEqual(hooks.last().conversation_id, "homeassistant:light.study")


# ======================================================================
# 5) 保活：HA 是**客户端主动发 ping**
# ======================================================================
class TestKeepalive(AdapterCase):
    def test_ping_is_client_initiated(self):
        """锁死本平台与 mattermost 的关键区别：ping 由**客户端**发。

        若实现错成"等服务端 ping"，服务端一条 ping 都不会收到，本用例就会失败。
        """
        gw = self.make_gateway()
        hooks = RecordingHooks()
        adapter = self.make_adapter(gw, hooks, entities=["light.kitchen"])
        adapter.ping_interval = 0.1
        self.start_and_wait_authed(adapter, gw)

        self.assertTrue(wait_for(lambda: len(gw.types_of("ping")) >= 2),
                        "客户端必须主动周期性发 ping（HA 的 ping/pong 是客户端发起）")
        ping = gw.types_of("ping")[0]
        self.assertEqual(set(ping), {"id", "type"})
        self.assertIsInstance(ping["id"], int)
        # 服务端回的 pong 必须带同一个 id
        self.assertFalse(adapter._pong_ids == set(), "应当收到过 pong")

    def test_watchdog_reconnects_when_pong_stops(self):
        """收不到 pong（TCP 半开 / 服务端卡死）⇒ 判死并重连，而不是永远挂着。"""
        gw = self.make_gateway(answer_ping=False)
        hooks = RecordingHooks()
        adapter = self.make_adapter(gw, hooks, entities=["light.kitchen"])
        adapter.ping_interval = 0.1
        adapter.start()
        self.assertTrue(
            wait_for(lambda: len(gw.handshakes) >= 2, timeout=WAIT),
            "看门狗判死后应当重连（第二条握手）",
        )
        self.assertEqual(hooks.count, 0)


# ======================================================================
# 6) 畸形输入 / 停止 / 凭据卫生
# ======================================================================
class TestHygiene(AdapterCase):
    def test_malformed_messages_do_not_crash_and_leak_no_traceback(self):
        gw = self.make_gateway()
        hooks = RecordingHooks()
        adapter = self.make_adapter(gw, hooks, entities=["binary_sensor.front_door"])
        self.start_and_wait_authed(adapter, gw)

        with self.assertLogs("opencode_bridge.adapters.homeassistant", level="DEBUG") as cap:
            gw.push_raw_text("[--INVALID--JSON--]")
            gw.push_malformed([1, 2, 3])                     # 不是对象
            gw.push_malformed({"type": "event"})              # 缺 event 字段
            gw.push_malformed({"type": "event", "event": []})  # event 不是对象
            gw.push_malformed({"type": "result"})            # 缺 id
            gw.push_malformed({"id": "x", "type": "result"}) # id 不是整数
            gw.push_malformed({"type": "ping"})              # 缺 id：防御分支忽略
            gw.push_malformed({"type": "who_knows", "blob": "x" * 5000})
            # 连接必须还活着：再来一条正常事件仍能收到（**内部状态**判定）
            gw.push(state_changed())
            self.assertTrue(hooks.wait_for_inbound(1))

        blob = "\n".join(r.getMessage() for r in cap.records)
        self.assertNotIn("Traceback", blob)
        self.assertTrue(adapter.running)
        self.assertEqual(hooks.last().conversation_id,
                         "homeassistant:binary_sensor.front_door")

    def test_oversized_event_body_does_not_break_stream(self):
        gw = self.make_gateway()
        hooks = RecordingHooks()
        adapter = self.make_adapter(gw, hooks, accept_all=True)
        self.start_and_wait_authed(adapter, gw)
        big = state_changed("light.kitchen", new="x" * 100_000)
        gw.push(big)
        gw.push(state_changed("light.study"))
        self.assertTrue(hooks.wait_for_inbound(2))
        self.assertEqual(hooks.last().conversation_id, "homeassistant:light.study")

    def test_credentials_never_reach_url_or_handshake_headers(self):
        gw = self.make_gateway()
        hooks = RecordingHooks()
        adapter = self.make_adapter(gw, hooks, entities=["light.kitchen"])
        self.assertNotIn(TOKEN, adapter.ws_url())
        self.assertNotIn("?", adapter.ws_url())

        with self.assertLogs("opencode_bridge.adapters.homeassistant", level="DEBUG") as cap:
            self.start_and_wait_authed(adapter, gw)
            gw.push(state_changed("light.kitchen"))
            self.assertTrue(hooks.wait_for_inbound(1))

        request_line = gw.handshakes[0]["__request_line__"]
        self.assertNotIn(TOKEN, request_line)
        self.assertNotIn("?", request_line)
        for name, value in gw.handshakes[0].items():
            self.assertNotIn(TOKEN, f"{name}: {value}",
                             f"握手头 {name} 里出现了凭据")
        blob = "\n".join(r.getMessage() for r in cap.records)
        self.assertNotIn(TOKEN, blob, "令牌不得出现在任何日志里")

    def test_redact_strips_query(self):
        from opencode_bridge.adapters.homeassistant import _redact

        self.assertEqual(_redact("ws://h:8123/api/websocket"), "ws://h:8123/api/websocket")
        self.assertEqual(
            _redact("ws://h:8123/api/websocket?access_token=secret"),
            "ws://h:8123/api/websocket?…",
        )
        self.assertNotIn("secret", _redact("ws://h/x?access_token=secret"))

    def test_stop_is_clean_and_bounded(self):
        gw = self.make_gateway()
        hooks = RecordingHooks()
        adapter = self.make_adapter(gw, hooks, entities=["light.kitchen"])
        adapter.ping_interval = 0.1
        self.start_and_wait_authed(adapter, gw)

        before_threads = {t.name for t in threading.enumerate()}
        started = time.monotonic()
        adapter.stop()
        elapsed = time.monotonic() - started

        # ⬇ 唯一的时间断言：**上界**（stop 必须立刻把阻塞中的 recv 唤醒）。
        self.assertLess(elapsed, 5.0, f"stop() 花了 {elapsed:.2f}s（应立刻返回）")
        # 其余全用内部状态断言
        self.assertFalse(adapter.running)
        self.assertIsNone(adapter.transport)
        self.assertFalse(adapter.authenticated)
        self.assertEqual(adapter._pending, {})
        # 幂等：再 stop 一次不炸
        adapter.stop()

        def no_ha_threads() -> bool:
            names = {t.name for t in threading.enumerate()}
            return not (names & {"homeassistant-heartbeat"}) and not any(
                n.startswith("transport:homeassistant") for n in names
            )

        self.assertTrue(wait_for(no_ha_threads, timeout=5.0),
                        "stop() 之后不该留下心跳 / 传输层线程")
        self.assertTrue(before_threads is not None)

    def test_stop_without_start_is_safe(self):
        adapter = build(
            "homeassistant", {"url": "http://127.0.0.1:1", "token": "t"}, RecordingHooks()
        )
        adapter.stop()
        self.assertFalse(adapter.running)

    def test_gateway_saw_no_protocol_assertion_failures(self):
        gw = self.make_gateway()
        hooks = RecordingHooks()
        adapter = self.make_adapter(gw, hooks, entities=["light.kitchen"])
        self.start_and_wait_authed(adapter, gw)
        gw.push(state_changed("light.kitchen"))
        self.assertTrue(hooks.wait_for_inbound(1))
        adapter.stop()
        self.assertEqual(gw.errors, [])


class TestConfiguredButReceivesNothing(unittest.TestCase):
    """「凭据配齐了，但一个事件都收不到」必须**机器可检测**。

    ⚠️ 这条来自复核时发现的真实可用性缺陷：本平台默认「全丢」（设备状态变更不等于
    "有人跟你说话"），而用户只配齐 ``required_tokens``（``url`` + ``token``）之后，
    ``--status`` 会显示「已配置 / 入站就绪」，实际**收不到任何事件**。
    与本项目修过多次的"配得完全正确却被判成不可用"是同一类问题 ——
    **状态视图说就绪，而实际不工作**。

    启动日志里确实有WARNING，但只查 ``--status`` 的人看不到。所以
    ``capabilities()`` 必须给出机器可读的判据。
    """

    BASE = {"url": "ws://ha.local:8123/api/websocket", "token": "tok"}

    def _caps(self, **extra):
        from opencode_bridge.adapters import build

        return build("homeassistant", {**self.BASE, **extra}, None).capabilities()

    def test_only_required_tokens_means_nothing_is_accepted(self):
        caps = self._caps()
        self.assertFalse(
            caps["inbound_accepts_anything"],
            "只配 url+token 时必须如实报告'收不到任何事件'，否则 --status 的"
            "「入站就绪」就是假的",
        )
        self.assertEqual(caps["filter_entities_count"], 0)
        self.assertEqual(caps["filter_domains_count"], 0)
        self.assertFalse(caps["accept_all"])

    def test_any_filter_makes_it_accept(self):
        for label, extra in (
            ("domains", {"domains": ["light"]}),
            ("entities", {"entities": ["light.kitchen"]}),
            ("accept_all", {"accept_all": True}),
        ):
            with self.subTest(filter=label):
                self.assertTrue(
                    self._caps(**extra)["inbound_accepts_anything"],
                    f"配了 {label} 就应当开始收事件",
                )

    def test_the_snapshot_keeps_the_base_keys(self):
        """覆写 ``capabilities()`` 不许把基类原有的键弄丢（那会让状态视图缺列）。"""
        from opencode_bridge.adapters import Adapter
        from opencode_bridge.hooks import Outbound

        class _Probe(Adapter):
            """最小具体子类，只为取到**未经覆写**的基类键集。"""

            name = "probe"
            label = "probe"

            def send(self, out: Outbound):
                return None

            def edit(self, handle, out: Outbound) -> bool:
                return False

            def start(self) -> None:
                return None

        base_keys = set(_Probe({}, None).capabilities())
        caps = self._caps()
        self.assertTrue(base_keys, "探针应至少拿到若干基类键")
        self.assertTrue(
            base_keys <= set(caps),
            f"丢了基类的键：{sorted(base_keys - set(caps))}",
        )
        self.assertIn("inbound_accepts_anything", caps)
        self.assertIn("require_user_context", caps)

    def test_the_signal_matches_the_runtime_filter(self):
        """快照里的判据必须与真正跑起来的过滤器**一致**，不能是两套逻辑。"""
        from opencode_bridge.adapters import build

        for extra in ({}, {"domains": ["light"]}, {"accept_all": True}):
            with self.subTest(extra=extra):
                adapter = build("homeassistant", {**self.BASE, **extra}, None)
                self.assertEqual(
                    adapter.capabilities()["inbound_accepts_anything"],
                    adapter._accepts_everything(),
                    "capabilities 暴露的判据与 _accepts_everything() 不一致 —— "
                    "两套逻辑迟早会分叉",
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
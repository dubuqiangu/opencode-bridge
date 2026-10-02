"""T2.0 测试——最小 WebSocket 客户端（RFC 6455，纯标准库）。

**不 mock socket**：每个用例都在 ``127.0.0.1:0`` 上起一个用标准库 ``socket`` +
``threading`` 写的真服务器线程，服务端按 RFC 手工收发字节（发帧不加掩码，读帧
校验并还原客户端掩码），因此测的是真实协议行为，而不是内部实现细节。

覆盖：握手成功/被拒、握手请求头、文本收发、中文 UTF-8、16 位与 64 位长度、
客户端掩码、分片聚合、控制帧插在分片中间、ping/pong、close 语义、超时、
幂等关闭、上下文管理器、连接被拒、URL 解析。
"""

from __future__ import annotations

import base64
import hashlib
import socket
import struct
import threading
import time
import unittest

from opencode_bridge.ws import (
    WebSocketClient,
    WebSocketError,
    _parse_ws_url,
    connect,
)

# ----------------------------------------------------------------------
# 服务端辅助函数：手工按 RFC 6455 收发字节（不 mock）
# ----------------------------------------------------------------------
_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

_OP_CONT = 0x0
_OP_TEXT = 0x1
_OP_BINARY = 0x2
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA


def _recv_exact(sock: socket.socket, size: int, timeout: float = 5.0) -> bytes:
    """服务端侧读满 n 字节。"""
    sock.settimeout(timeout)
    buf = bytearray()
    while len(buf) < size:
        chunk = sock.recv(size - len(buf))
        if not chunk:
            raise AssertionError("连接被提前关闭，收不到预期字节")
        buf += chunk
    return bytes(buf)


def read_request_head(sock: socket.socket, timeout: float = 5.0) -> bytes:
    """读客户端的 HTTP 握手请求（含结尾空行）。"""
    sock.settimeout(timeout)
    buf = bytearray()
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(1)
        if not chunk:
            raise AssertionError("握手请求没读完就断了")
        buf += chunk
    return bytes(buf)


def header_of(request_head: bytes, name: str) -> str:
    """从握手请求里取某个头（大小写不敏感）。"""
    for line in request_head.decode("latin-1").split("\r\n")[1:]:
        key, sep, value = line.partition(":")
        if sep and key.strip().lower() == name.lower():
            return value.strip()
    raise AssertionError(f"请求里没有头 {name}: {request_head!r}")


def expected_accept(key: str) -> str:
    """RFC 6455 §4.2.2：base64(sha1(key + GUID))。"""
    return base64.b64encode(hashlib.sha1(key.encode("ascii") + _GUID).digest()).decode("ascii")


def make_response(
    request_head: bytes,
    *,
    status: bytes = b"101 Switching Protocols",
    upgrade: bytes | None = b"websocket",
    connection: bytes | None = b"Upgrade",
    accept: bytes | None = None,
) -> bytes:
    """按服务端规则拼一份握手响应；参数为 ``None`` 表示"故意不发这个头"。"""
    key = header_of(request_head, "Sec-WebSocket-Key")
    accept_value = expected_accept(key) if accept is None else accept.decode("latin-1")
    lines = [b"HTTP/1.1 " + status]
    if upgrade is not None:
        lines.append(b"Upgrade: " + upgrade)
    if connection is not None:
        lines.append(b"Connection: " + connection)
    if accept_value:
        lines.append(b"Sec-WebSocket-Accept: " + accept_value.encode("latin-1"))
    return b"\r\n".join(lines) + b"\r\n\r\n"


def accept_with_handshake(
    listener: socket.socket,
    *,
    responder=make_response,
    timeout: float = 5.0,
) -> tuple[socket.socket, bytes]:
    """接受连接 + 完成握手，返回 ``(连接, 请求原文)``。"""
    listener.settimeout(timeout)
    conn, _ = listener.accept()
    conn.settimeout(timeout)
    request_head = read_request_head(conn)
    conn.sendall(responder(request_head))
    return conn, request_head


def send_frame(
    sock: socket.socket,
    opcode: int,
    payload: bytes = b"",
    *,
    fin: bool = True,
    mask: bytes | None = None,
    rsv1: bool = False,
) -> None:
    """服务端发帧。**按 RFC 服务端不加掩码**；``mask`` 仅用于构造非法帧。"""
    head = bytearray()
    head.append((0x80 if fin else 0x00) | opcode | (0x40 if rsv1 else 0x00))
    size = len(payload)
    if size < 126:
        head.append(size)
    elif size < 65536:
        head.append(126)
        head += size.to_bytes(2, "big")
    else:
        head.append(127)
        head += size.to_bytes(8, "big")
    if mask is not None:
        head[1] |= 0x80
        head += mask
        payload = bytes(a ^ mask[i % 4] for i, a in enumerate(payload))
    sock.sendall(bytes(head) + payload)


def send_text(sock: socket.socket, text: str, *, fin: bool = True) -> None:
    send_frame(sock, _OP_TEXT, text.encode("utf-8"), fin=fin)


def send_ping(sock: socket.socket, payload: bytes = b"") -> None:
    send_frame(sock, _OP_PING, payload)


def send_close(sock: socket.socket, code: int = 1000, reason: str = "") -> None:
    send_frame(sock, _OP_CLOSE, struct.pack("!H", code) + reason.encode("utf-8"))


def recv_frame(sock: socket.socket, timeout: float = 5.0) -> tuple[bool, int, bytes, bool]:
    """读客户端发来的帧，返回 ``(fin, opcode, payload, 是否带掩码)``。

    带掩码时按 RFC 还原 payload——这一步同时证明了客户端确实做了掩码。
    """
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


def recv_masked_text(sock: socket.socket, timeout: float = 5.0) -> str:
    """读一条客户端文本帧，并断言它带掩码（RFC 强制客户端掩码）。"""
    fin, opcode, payload, masked = recv_frame(sock, timeout)
    assert opcode == _OP_TEXT, f"期望 text 帧，实际 opcode=0x{opcode:x}"
    assert fin, "客户端默认发完整帧（FIN=1）"
    assert masked, "客户端发出的帧必须带掩码（RFC 6455 §5.3）"
    return payload.decode("utf-8")


class Server(threading.Thread):
    """在 127.0.0.1:0 上起一个只说 RFC 字节的真服务器线程。

    两段式：``start_server()`` 只保证"线程已在监听"；握手要等客户端连上来，
    所以由 ``wait_handshake()`` 在客户端 connect 之后调用。
    """

    def __init__(self, responder=make_response) -> None:
        super().__init__(daemon=True)
        self.responder = responder  # 测试可在 start 之前覆盖成"故意握手失败"的版本
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port: int = self._listener.getsockname()[1]
        self.conn: socket.socket | None = None
        self.request_head = b""
        self.error: BaseException | None = None
        self._up = threading.Event()
        self._done = threading.Event()
        self._release = threading.Event()
        self._begin = False  # 注意：不能叫 _started，那是 Thread 内部属性

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/test?token=abc"

    def run(self) -> None:
        self._up.set()
        try:
            self.conn, self.request_head = accept_with_handshake(
                self._listener, responder=self.responder
            )
        except BaseException as exc:  # noqa: BLE001 - 交给测试断言用
            self.error = exc
        finally:
            self._done.set()
        self._release.wait(10)
        if self.conn is not None:
            self.conn.close()
        self._listener.close()

    def start_server(self) -> None:
        """启动服务器线程（可重复调用）。"""
        if not self._begin:
            self._begin = True
            self.start()
        self._up.wait(5)

    def wait_handshake(self, timeout: float = 5.0) -> socket.socket:
        """等服务器侧握手完成，返回与客户端相连的 socket。"""
        self.start_server()
        if not self._done.wait(timeout):
            raise AssertionError("服务器握手超时")
        if self.error is not None:
            raise AssertionError(f"服务器线程失败: {self.error!r}") from self.error
        assert self.conn is not None
        return self.conn

    def stop(self) -> None:
        self._release.set()
        if self._begin:
            self.join(5)
        if not self._begin:
            self._listener.close()

    def __enter__(self) -> "Server":
        self.start_server()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


class WsTestCase(unittest.TestCase):
    """带真服务器线程的测试基类。"""

    responder = staticmethod(make_response)

    def setUp(self) -> None:
        self.server = Server(self.responder)
        self.addCleanup(self.server.stop)

    def connect_client(self, **kw) -> WebSocketClient:
        """握手并返回客户端；服务器侧同步就绪后返回。"""
        self.server.start_server()
        ws = connect(self.server.url, **kw)
        self.addCleanup(ws.close)
        self.server.wait_handshake()
        return ws

    def server_conn(self) -> socket.socket:
        conn = self.server.conn
        assert conn is not None
        return conn


# ----------------------------------------------------------------------
# 握手
# ----------------------------------------------------------------------
class TestHandshake(WsTestCase):
    def test_handshake_success_and_request_headers(self) -> None:
        ws = self.connect_client(headers={"Authorization": "Bearer xoxb-1"})
        self.assertFalse(ws.closed)
        head = self.server.request_head.decode("latin-1")
        self.assertTrue(head.startswith("GET /test?token=abc HTTP/1.1\r\n"), head)
        request_head = self.server.request_head
        self.assertEqual(header_of(request_head, "Host"), f"127.0.0.1:{self.server.port}")
        self.assertEqual(header_of(request_head, "Upgrade"), "websocket")
        self.assertEqual(header_of(request_head, "Connection"), "Upgrade")
        self.assertEqual(header_of(request_head, "Sec-WebSocket-Version"), "13")
        self.assertEqual(header_of(request_head, "Authorization"), "Bearer xoxb-1")
        key = header_of(request_head, "Sec-WebSocket-Key")
        self.assertEqual(len(base64.b64decode(key)), 16, "Sec-WebSocket-Key 应是 16 字节随机数的 base64")

    def test_handshake_key_is_random_each_time(self) -> None:
        keys = []
        for _ in range(2):
            with Server() as srv:
                with connect(srv.url):
                    pass
                srv.wait_handshake()
                keys.append(header_of(srv.request_head, "Sec-WebSocket-Key"))
        self.assertNotEqual(keys[0], keys[1])

    def test_handshake_rejected_non_101(self) -> None:
        self.server.responder = lambda head: make_response(head, status=b"400 Bad Request")
        self.server.start_server()
        with self.assertRaises(WebSocketError) as ctx:
            connect(self.server.url)
        self.assertIn("101", str(ctx.exception))
        self.assertIn("400", str(ctx.exception))

    def test_handshake_rejected_missing_upgrade(self) -> None:
        self.server.responder = lambda head: make_response(head, upgrade=None)
        self.server.start_server()
        with self.assertRaises(WebSocketError) as ctx:
            connect(self.server.url)
        self.assertIn("Upgrade", str(ctx.exception))

    def test_handshake_rejected_missing_connection(self) -> None:
        self.server.responder = lambda head: make_response(head, connection=None)
        self.server.start_server()
        with self.assertRaises(WebSocketError) as ctx:
            connect(self.server.url)
        self.assertIn("Connection", str(ctx.exception))

    def test_handshake_rejected_bad_accept(self) -> None:
        self.server.responder = lambda head: make_response(
            head, accept=b"AAAAAAAAAAAAAAAAAAAAAAAAAAA="
        )
        self.server.start_server()
        with self.assertRaises(WebSocketError) as ctx:
            connect(self.server.url)
        self.assertIn("Accept", str(ctx.exception))

    def test_handshake_accepts_comma_separated_headers(self) -> None:
        """``Connection: keep-alive, Upgrade`` 属于合法写法，应通过。"""
        self.server.responder = lambda head: make_response(head, connection=b"keep-alive, Upgrade")
        ws = self.connect_client()
        self.assertFalse(ws.closed)

    def test_connect_to_refused_address_raises_websocket_error(self) -> None:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()  # 端口已释放，再连必然被拒
        with self.assertRaises(WebSocketError):
            connect(f"ws://127.0.0.1:{port}/nope", timeout=2.0)


class TestUrlParsing(unittest.TestCase):
    """URL 解析不需要真服务器（含 wss 分支）。"""

    def test_ws_default_port_and_query(self) -> None:
        target = _parse_ws_url("ws://example.com/socket?v=1&x=2")
        self.assertEqual((target.host, target.port, target.secure), ("example.com", 80, False))
        self.assertEqual(target.path, "/socket?v=1&x=2")
        self.assertEqual(target.host_header, "example.com")

    def test_wss_default_port_and_tls_flag(self) -> None:
        target = _parse_ws_url("wss://example.com/")
        self.assertEqual((target.port, target.secure), (443, True))
        self.assertEqual(target.path, "/")

    def test_explicit_port_in_host_header(self) -> None:
        target = _parse_ws_url("wss://example.com:8443/x")
        self.assertEqual((target.port, target.host_header), (8443, "example.com:8443"))

    def test_ipv6_literal(self) -> None:
        target = _parse_ws_url("ws://[::1]:9000/")
        self.assertEqual((target.host, target.host_header), ("::1", "[::1]:9000"))

    def test_empty_path_becomes_slash(self) -> None:
        self.assertEqual(_parse_ws_url("ws://example.com").path, "/")

    def test_non_ascii_path_is_quoted(self) -> None:
        target = _parse_ws_url("ws://example.com/消息")
        self.assertTrue(target.path.isascii())

    def test_bad_scheme_rejected(self) -> None:
        for bad in ("http://example.com/", "ftp://example.com/", "example.com"):
            with self.assertRaises(WebSocketError):
                _parse_ws_url(bad)


# ----------------------------------------------------------------------
# 收发文本
# ----------------------------------------------------------------------
class TestTextExchange(WsTestCase):
    def test_send_masks_and_server_can_unmask(self) -> None:
        ws = self.connect_client()
        ws.send("hello world")
        # recv_masked_text 内部断言 MASK 位为 1，且还原后内容一致
        self.assertEqual(recv_masked_text(self.server_conn()), "hello world")

    def test_roundtrip_utf8_chinese(self) -> None:
        ws = self.connect_client()
        conn = self.server_conn()
        text = "你好，世界 🌍 emoji 与中文"
        ws.send(text)
        self.assertEqual(recv_masked_text(conn), text)
        send_text(conn, text)
        self.assertEqual(ws.recv(), text)

    def test_send_empty_text(self) -> None:
        ws = self.connect_client()
        ws.send("")
        self.assertEqual(recv_frame(self.server_conn()), (True, _OP_TEXT, b"", True))

    def test_recv_empty_text_from_server(self) -> None:
        ws = self.connect_client()
        send_text(self.server_conn(), "")
        self.assertEqual(ws.recv(), "")

    def test_long_text_uses_16bit_length(self) -> None:
        """125 < 字节数 <= 65535：长度用 2 字节扩展。"""
        ws = self.connect_client()
        conn = self.server_conn()
        text = "长" * 1000  # 3000 字节
        ws.send(text)
        self.assertEqual(recv_masked_text(conn), text)
        send_text(conn, text)
        self.assertEqual(ws.recv(), text)

    def test_huge_text_uses_64bit_length(self) -> None:
        """> 65535 字节：长度用 8 字节扩展。"""
        ws = self.connect_client()
        conn = self.server_conn()
        text = "x" * 70000
        ws.send(text)
        self.assertEqual(recv_masked_text(conn), text)
        send_text(conn, text)
        self.assertEqual(ws.recv(), text)

    def test_two_messages_in_order(self) -> None:
        ws = self.connect_client()
        conn = self.server_conn()
        send_text(conn, "第一")
        send_text(conn, "第二")
        self.assertEqual(ws.recv(), "第一")
        self.assertEqual(ws.recv(), "第二")


# ----------------------------------------------------------------------
# 分片
# ----------------------------------------------------------------------
class TestFragmentation(WsTestCase):
    def test_fragmented_message_is_aggregated(self) -> None:
        ws = self.connect_client()
        conn = self.server_conn()
        send_text(conn, "Hel", fin=False)
        send_frame(conn, _OP_CONT, b"lo ", fin=False)
        send_frame(conn, _OP_CONT, b"World", fin=True)
        self.assertEqual(ws.recv(), "Hello World")

    def test_control_frames_interleaved_in_fragment(self) -> None:
        """ping/pong 可以插在分片中间：自动回 pong、忽略 pong，消息照常聚合。"""
        ws = self.connect_client()
        conn = self.server_conn()
        send_text(conn, "Hel", fin=False)
        send_ping(conn, b"p1")
        send_frame(conn, _OP_CONT, b"lo ", fin=False)
        # 服务端给 pong 加掩码属于不合规，客户端应宽容还原并忽略
        send_frame(conn, _OP_PONG, b"ignored", mask=b"\x00\x00\x00\x00")
        send_frame(conn, _OP_CONT, b"World", fin=True)
        self.assertEqual(ws.recv(), "Hello World")
        _, opcode, payload, masked = recv_frame(conn)  # 客户端自动回的那条 pong
        self.assertEqual((opcode, payload, masked), (_OP_PONG, b"p1", True))

    def test_many_fragments(self) -> None:
        ws = self.connect_client()
        conn = self.server_conn()
        send_text(conn, "a", fin=False)
        for ch in "bcdef":
            send_frame(conn, _OP_CONT, ch.encode(), fin=False)
        send_frame(conn, _OP_CONT, b"g", fin=True)
        self.assertEqual(ws.recv(), "abcdefg")

    def test_new_text_frame_during_fragment_is_protocol_error(self) -> None:
        ws = self.connect_client()
        conn = self.server_conn()
        send_text(conn, "a", fin=False)
        send_text(conn, "b", fin=True)
        with self.assertRaises(WebSocketError) as ctx:
            ws.recv()
        self.assertIn("分片", str(ctx.exception))

    def test_orphan_continuation_is_protocol_error(self) -> None:
        ws = self.connect_client()
        send_frame(self.server_conn(), _OP_CONT, b"x", fin=True)
        with self.assertRaises(WebSocketError) as ctx:
            ws.recv()
        self.assertIn("continuation", str(ctx.exception))

    def test_continuation_utf8_split_across_frames(self) -> None:
        """UTF-8 多字节序列被切在两帧之间，聚合后仍应正确解码。"""
        ws = self.connect_client()
        conn = self.server_conn()
        raw = "中文测试".encode("utf-8")
        send_frame(conn, _OP_TEXT, raw[:4], fin=False)
        send_frame(conn, _OP_CONT, raw[4:], fin=True)
        self.assertEqual(ws.recv(), "中文测试")


# ----------------------------------------------------------------------
# 控制帧
# ----------------------------------------------------------------------
class TestControlFrames(WsTestCase):
    def test_client_ping_sends_masked_ping_frame(self) -> None:
        ws = self.connect_client()
        ws.ping(b"hb")
        self.assertEqual(recv_frame(self.server_conn()), (True, _OP_PING, b"hb", True))

    def test_ping_with_empty_payload(self) -> None:
        ws = self.connect_client()
        ws.ping()
        _, opcode, payload, _ = recv_frame(self.server_conn())
        self.assertEqual((opcode, payload), (_OP_PING, b""))

    def test_ping_payload_too_long_rejected(self) -> None:
        ws = self.connect_client()
        with self.assertRaises(ValueError):
            ws.ping(b"x" * 126)

    def test_server_ping_is_answered_with_pong(self) -> None:
        """ping 的应答发生在 recv() 的读循环里（最小实现没有后台读线程）。"""
        ws = self.connect_client()
        conn = self.server_conn()
        got: list[str | None] = []
        reader = threading.Thread(target=lambda: got.append(ws.recv()), daemon=True)
        reader.start()
        send_ping(conn, b"server-hb")
        _, opcode, payload, masked = recv_frame(conn)
        self.assertEqual((opcode, payload, masked), (_OP_PONG, b"server-hb", True))
        send_text(conn, "after-ping")  # ping 不应打断数据流
        reader.join(5)
        self.assertEqual(got, ["after-ping"])

    def test_server_pong_is_ignored(self) -> None:
        ws = self.connect_client()
        conn = self.server_conn()
        send_frame(conn, _OP_PONG, b"whatever")
        send_text(conn, "payload")
        self.assertEqual(ws.recv(), "payload")

    def test_oversized_control_frame_is_error(self) -> None:
        ws = self.connect_client()
        send_frame(self.server_conn(), _OP_PING, b"y" * 126)  # 控制帧上限 125
        with self.assertRaises(WebSocketError) as ctx:
            ws.recv()
        self.assertIn("125", str(ctx.exception))

    def test_fragmented_control_frame_is_error(self) -> None:
        ws = self.connect_client()
        send_frame(self.server_conn(), _OP_PING, b"a", fin=False)
        with self.assertRaises(WebSocketError):
            ws.recv()


# ----------------------------------------------------------------------
# 关闭
# ----------------------------------------------------------------------
class TestClose(WsTestCase):
    def test_recv_returns_none_after_peer_close(self) -> None:
        ws = self.connect_client()
        conn = self.server_conn()
        send_close(conn, 1000, "bye")
        self.assertIsNone(ws.recv())
        self.assertTrue(ws.closed)
        self.assertEqual(ws.close_code, 1000)
        self.assertEqual(ws.close_reason, "bye")
        _, opcode, payload, _ = recv_frame(conn)  # 客户端应回一个 close 帧
        self.assertEqual(opcode, _OP_CLOSE)
        self.assertEqual(struct.unpack("!H", payload[:2])[0], 1000)

    def test_close_is_idempotent(self) -> None:
        ws = self.connect_client()
        ws.close(1000, "done")
        self.assertTrue(ws.closed)
        for _ in range(3):
            ws.close()  # 重复调用不抛
            ws.close(1001)  # 参数都换了也不抛
        self.assertTrue(ws.closed)
        self.assertIsNone(ws.recv())

    def test_close_on_never_connected_client(self) -> None:
        client = WebSocketClient("ws://127.0.0.1:1/")
        self.assertTrue(client.closed)
        client.close()
        client.close()
        self.assertTrue(client.closed)
        self.assertIsNone(client.recv())

    def test_close_validates_code_and_reason(self) -> None:
        ws = self.connect_client()
        with self.assertRaises(ValueError):
            ws.close(42)
        with self.assertRaises(ValueError):
            ws.close(1000, "x" * 200)
        ws.close(1000)

    def test_send_after_close_raises(self) -> None:
        ws = self.connect_client()
        ws.close()
        with self.assertRaises(WebSocketError):
            ws.send("late")
        with self.assertRaises(WebSocketError):
            ws.ping()

    def test_context_manager_closes_socket(self) -> None:
        self.server.start_server()
        with connect(self.server.url) as ws:
            self.server.wait_handshake()
            ws.send("x")
            self.assertEqual(recv_masked_text(self.server_conn()), "x")
        self.assertTrue(ws.closed)
        self.assertIsNone(ws.recv())

    def test_double_connect_raises(self) -> None:
        ws = self.connect_client()
        with self.assertRaises(WebSocketError):
            ws.connect()

    def test_unexpected_disconnect_raises_not_none(self) -> None:
        """对端裸断（没发 close）必须报错，不能伪装成"正常关闭"。"""
        ws = self.connect_client()
        self.server_conn().close()  # 直接断开，不发 close 帧
        with self.assertRaises(WebSocketError) as ctx:
            ws.recv()
        self.assertIn("意外关闭", str(ctx.exception))


# ----------------------------------------------------------------------
# 帧合法性
# ----------------------------------------------------------------------
class TestFrameValidation(WsTestCase):
    def test_binary_frame_raises_instead_of_being_dropped(self) -> None:
        ws = self.connect_client()
        send_frame(self.server_conn(), _OP_BINARY, b"\x00\x01\x02")
        with self.assertRaises(WebSocketError) as ctx:
            ws.recv()
        self.assertIn("二进制", str(ctx.exception))

    def test_rsv1_set_raises(self) -> None:
        ws = self.connect_client()
        send_frame(self.server_conn(), _OP_TEXT, b"hi", rsv1=True)  # 未协商扩展却置 RSV1
        with self.assertRaises(WebSocketError) as ctx:
            ws.recv()
        self.assertIn("RSV", str(ctx.exception))

    def test_invalid_utf8_text_raises(self) -> None:
        ws = self.connect_client()
        send_frame(self.server_conn(), _OP_TEXT, b"\xff\xfe")
        with self.assertRaises(WebSocketError) as ctx:
            ws.recv()
        self.assertIn("UTF-8", str(ctx.exception))

    def test_recv_timeout_raises_websocket_error(self) -> None:
        """超时必须报错——不能返回 None 与"连接关闭"混淆。"""
        ws = self.connect_client(timeout=1.0)
        time.sleep(1.2)  # 服务器什么都不发
        with self.assertRaises(WebSocketError) as ctx:
            ws.recv()
        self.assertIn("超时", str(ctx.exception))
        self.assertFalse(ws.closed, "超时不代表连接已关闭")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
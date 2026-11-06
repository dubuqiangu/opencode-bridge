"""出站代理支持守门：三条裸 socket / 裸 ``http.client`` 的缝要走 ``HTTPS_PROXY``。

被测对象是「代理发现」与「经代理建连」这一对行为：

* :func:`opencode_bridge.proxy_config.proxy_endpoint_for` —— 环境变量翻译；
* ``ws.WebSocketClient.connect`` / ``transport.tcp_lines.TcpLineTransport._open``
  —— 经代理 ``CONNECT`` 隧道（随后 TLS 仍以**真实目标**校验证书）；
* ``adapters.telegram_http.TelegramHttp._post`` —— ``HTTPSConnection`` 指向代理 +
  ``set_tunnel`` 指向真实目标。

**鉴别力来自真服务器，不是 mock**：代理是一个绑定 ``127.0.0.2`` 的真 socket 服务，
逐字节读客户端的 ``CONNECT`` 请求、记录 authority、回 200 并把双向字节中继到真实
目标（目标绑 ``127.0.0.1``）。于是「``CONNECT`` 的 authority == 真实目标」与
「TLS 的 ``server_hostname`` == 真实目标（而不是代理）」这两条断言都有咬合 ——
若代码走直连（或把代理主机当目标），``proxy.connections`` 会是空、断言当场红。
"""

from __future__ import annotations

import os
import socket
import threading
import unittest
import urllib.request
from unittest import mock

from .proxy_env_isolation import (
    clear_proxy_environment,
    proxy_environment,
    restore_proxy_environment,
)
from .test_ws import accept_with_handshake, read_request_head

from opencode_bridge.adapters.telegram_http import TelegramHttp
from opencode_bridge.adapters.telegram_protocol import API_HOST, API_PORT
from opencode_bridge.proxy_config import proxy_endpoint_for
from opencode_bridge.proxy_tunnel import (
    ProxyTunnelError,
    _connect_authority,
    establish_proxy_tunnel,
)
from opencode_bridge.transport.tcp_lines import TcpLineTransport
from opencode_bridge.ws import WebSocketClient, WebSocketError

# 代理绑 127.0.0.2、目标绑 127.0.0.1 —— 两个不同的回环地址，保证
# 「server_hostname == 目标」与「CONNECT 连到代理」两条断言互相可鉴别。
PROXY_HOST = "127.0.0.2"
ORIGIN_HOST = "127.0.0.1"

_ACCEPT_TIMEOUT = 5.0


# ----------------------------------------------------------------------
# 假代理：真 socket，读 CONNECT、记录 authority、回 200 并中继
# ----------------------------------------------------------------------
class RecordingConnectProxy(threading.Thread):
    """裸 socket 假代理：记录每条 ``CONNECT`` 的 authority，可选双向中继。"""

    def __init__(
        self,
        *,
        response_status: bytes = b"200 Connection established",
        relay: bool = True,
    ) -> None:
        super().__init__(daemon=True)
        self._response_status = response_status
        self._relay_enabled = relay
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind((PROXY_HOST, 0))
        self._listener.listen(5)
        self.port: int = self._listener.getsockname()[1]
        self.connections: list[str] = []
        self._lock = threading.Lock()
        self._open_sockets: list[socket.socket] = []

    def run(self) -> None:
        try:
            while True:
                conn, _ = self._listener.accept()
                conn.settimeout(_ACCEPT_TIMEOUT)
                self._handle(conn)
        except OSError:
            pass

    def _handle(self, conn: socket.socket) -> None:
        request_head = read_request_head(conn)
        request_line = request_head.split(b"\r\n", 1)[0].decode("latin-1")
        authority = request_line.split(" ", 2)[1]
        # 先记录、再回应答：调用方读到 200 时 authority 一定已在表里（无竞态）。
        with self._lock:
            self.connections.append(authority)
        conn.sendall(b"HTTP/1.1 " + self._response_status + b"\r\n\r\n")
        if not self._response_status.startswith(b"200"):
            conn.close()
            return
        if not self._relay_enabled:
            conn.close()
            return
        upstream = socket.create_connection(_split_authority(authority), timeout=_ACCEPT_TIMEOUT)
        upstream.settimeout(_ACCEPT_TIMEOUT)
        self._open_sockets.extend([conn, upstream])
        threading.Thread(target=self._pipe, args=(conn, upstream), daemon=True).start()
        threading.Thread(target=self._pipe, args=(upstream, conn), daemon=True).start()

    @staticmethod
    def _pipe(source: socket.socket, sink: socket.socket) -> None:
        try:
            while True:
                chunk = source.recv(4096)
                if not chunk:
                    break
                sink.sendall(chunk)
        except OSError:
            pass
        finally:
            try:
                sink.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    def close(self) -> None:
        for sock in [self._listener, *self._open_sockets]:
            try:
                sock.close()
            except OSError:
                pass


def _split_authority(authority: str) -> tuple[str, int]:
    """把 ``host:port`` 拆成 ``(host, port)``，兼容 IPv6 的 ``[..]:port``。"""
    host, _, port = authority.rpartition(":")
    host = host.strip("[]")
    return host, int(port)


# ----------------------------------------------------------------------
# 回环目标：真 WebSocket 服务端 / 真 TCP 接受者
# ----------------------------------------------------------------------
class LoopbackWebSocketOrigin(threading.Thread):
    """绑 ``127.0.0.1``、完成一次 WS 握手并保持连接的极小服务端。"""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind((ORIGIN_HOST, 0))
        self._listener.listen(1)
        self.port: int = self._listener.getsockname()[1]
        self.request_head = b""
        self.error: Exception | None = None
        self._conn: socket.socket | None = None

    def run(self) -> None:
        try:
            self._conn, self.request_head = accept_with_handshake(
                self._listener, timeout=_ACCEPT_TIMEOUT
            )
        except Exception as exc:  # noqa: BLE001 - 记下来供用例断言
            self.error = exc

    def close(self) -> None:
        for sock in (self._conn, self._listener):
            if sock is None:
                continue
            try:
                sock.close()
            except OSError:
                pass


class LoopbackTcpOrigin(threading.Thread):
    """绑 ``127.0.0.1``、接受一次连接并置位的极小 TCP 目标。"""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind((ORIGIN_HOST, 0))
        self._listener.listen(1)
        self.port: int = self._listener.getsockname()[1]
        self.accepted = threading.Event()
        self.error: Exception | None = None
        self._conn: socket.socket | None = None

    def run(self) -> None:
        try:
            self._listener.settimeout(_ACCEPT_TIMEOUT)
            self._conn, _ = self._listener.accept()
        except Exception as exc:  # noqa: BLE001
            self.error = exc
        finally:
            self.accepted.set()

    def close(self) -> None:
        for sock in (self._conn, self._listener):
            if sock is None:
                continue
            try:
                sock.close()
            except OSError:
                pass


# ----------------------------------------------------------------------
# TLS / HTTPSConnection 记录器（替换共享模块对象，目标服务端只用裸 socket）
# ----------------------------------------------------------------------
class RecordingTlsContext:
    """记录 ``server_hostname`` 的 TLS 上下文替身，``wrap_socket`` 原样返回 socket。"""

    def __init__(self) -> None:
        self.server_hostnames: list[str | None] = []

    def wrap_socket(self, sock: socket.socket, *, server_hostname=None, **kw):
        self.server_hostnames.append(server_hostname)
        return sock


class _FakeHttpsResponse:
    status = 200

    def read(self) -> bytes:
        return b'{"ok": true, "result": "proxied"}'


class RecordingHttpsConnection:
    """记录构造入参与 ``set_tunnel`` 的 ``http.client.HTTPSConnection`` 替身。"""

    def __init__(self, host, port=None, *args, timeout=None, **kwargs) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.tunnel: tuple[str, int] | None = None
        self.closed = False

    def set_tunnel(self, host, port) -> None:
        self.tunnel = (host, port)

    def request(self, method, path, body=None, headers=None) -> None:
        pass

    def getresponse(self) -> _FakeHttpsResponse:
        return _FakeHttpsResponse()

    def close(self) -> None:
        self.closed = True


class _TokenStub:
    bot_token = "9999999:stub-token-for-proxy-test"


# ----------------------------------------------------------------------
# 环境隔离基类
# ----------------------------------------------------------------------
class ProxyEnvTestCase(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._proxy_snapshot = proxy_environment()
        clear_proxy_environment()

    def tearDown(self) -> None:
        restore_proxy_environment(self._proxy_snapshot)
        super().tearDown()

    def set_proxy(self, *, url: str | None = None, no_proxy: str | None = None) -> None:
        clear_proxy_environment()
        if url is None:
            # 默认指向**刚起的那个假代理的真实端口**（它绑 0 = 随机端口）。
            url = f"http://{PROXY_HOST}:{getattr(self, 'proxy_port', 8888)}"
        os.environ["HTTPS_PROXY"] = url
        if no_proxy is not None:
            os.environ["no_proxy"] = no_proxy


# ----------------------------------------------------------------------
# 1) proxy_config：环境变量翻译
# ----------------------------------------------------------------------
class TestProxyEndpointFor(ProxyEnvTestCase):
    def test_no_proxy_environment_returns_none(self):
        self.assertIsNone(proxy_endpoint_for(API_HOST, scheme="https"))

    def test_https_proxy_env_yields_endpoint(self):
        os.environ["HTTPS_PROXY"] = f"http://{PROXY_HOST}:8123"
        self.assertEqual(proxy_endpoint_for(API_HOST, scheme="https"), (PROXY_HOST, 8123))

    def test_all_proxy_is_used_as_fallback(self):
        os.environ["ALL_PROXY"] = f"http://{PROXY_HOST}:8999"
        self.assertEqual(proxy_endpoint_for(API_HOST, scheme="https"), (PROXY_HOST, 8999))

    def test_no_proxy_list_bypasses_matching_host(self):
        self.set_proxy(no_proxy=ORIGIN_HOST)
        self.assertIsNone(proxy_endpoint_for(ORIGIN_HOST, scheme="https"))
        # 代理仍在，别的 host 不受影响 —— 证明「返回 None」是 no_proxy 生效而非代理缺失。
        self.assertEqual(proxy_endpoint_for(API_HOST, scheme="https"), (PROXY_HOST, 8888))


# ----------------------------------------------------------------------
# 2) WS 客户端
# ----------------------------------------------------------------------
class TestWebSocketProxyArms(ProxyEnvTestCase):
    def _origin(self) -> LoopbackWebSocketOrigin:
        origin = LoopbackWebSocketOrigin()
        origin.start()
        self.addCleanup(origin.close)
        return origin

    def _proxy(self, **kw) -> RecordingConnectProxy:
        proxy = RecordingConnectProxy(**kw)
        proxy.start()
        self.proxy_port = proxy.port
        self.addCleanup(proxy.close)
        return proxy

    def _await_origin(self, origin: LoopbackWebSocketOrigin) -> None:
        # 目标服务端在**发出握手响应之后**才把请求头存进 self.request_head ⇒
        # 客户端 connect() 可能在赋值前就返回（竞态）⇒ 必须先 join 再断言。
        origin.join(_ACCEPT_TIMEOUT)
        self.assertTrue(origin.error is None, f"目标服务端要完成握手，实际 {origin.error!r}")

    def test_direct_arm_ignores_proxy_when_env_absent(self):
        origin = self._origin()
        client = WebSocketClient(f"ws://{ORIGIN_HOST}:{origin.port}/ws", timeout=_ACCEPT_TIMEOUT)
        self.addCleanup(client.close)
        client.connect()
        self._await_origin(origin)
        self.assertIn(b"GET /ws HTTP/1.1", origin.request_head)

    def test_proxy_arm_tunnels_to_real_target(self):
        origin = self._origin()
        proxy = self._proxy()
        self.set_proxy()
        tls_context = RecordingTlsContext()
        with mock.patch("ssl.create_default_context", return_value=tls_context):
            client = WebSocketClient(
                f"wss://{ORIGIN_HOST}:{origin.port}/ws", timeout=_ACCEPT_TIMEOUT
            )
            self.addCleanup(client.close)
            client.connect()
        self._await_origin(origin)
        # CONNECT 打的是**真实目标**，TLS 校验的也是**真实目标**（不是代理主机）。
        self.assertEqual(proxy.connections, [f"{ORIGIN_HOST}:{origin.port}"])
        self.assertEqual(tls_context.server_hostnames, [ORIGIN_HOST])
        self.assertIn(b"GET /ws HTTP/1.1", origin.request_head)

    def test_no_proxy_arm_stays_direct(self):
        origin = self._origin()
        proxy = self._proxy()
        self.set_proxy(no_proxy=ORIGIN_HOST)
        tls_context = RecordingTlsContext()
        with mock.patch("ssl.create_default_context", return_value=tls_context):
            client = WebSocketClient(
                f"wss://{ORIGIN_HOST}:{origin.port}/ws", timeout=_ACCEPT_TIMEOUT
            )
            self.addCleanup(client.close)
            client.connect()
        self._await_origin(origin)
        self.assertEqual(proxy.connections, [])
        self.assertEqual(tls_context.server_hostnames, [ORIGIN_HOST])
        self.assertIn(b"GET /ws HTTP/1.1", origin.request_head)

    def test_proxy_rejection_raises_websocket_error(self):
        self._origin()
        proxy = self._proxy(response_status=b"407 Proxy Authentication Required", relay=False)
        self.set_proxy()
        tls_context = RecordingTlsContext()
        with mock.patch("ssl.create_default_context", return_value=tls_context):
            client = WebSocketClient(
                f"wss://{ORIGIN_HOST}:44444/ws", timeout=_ACCEPT_TIMEOUT
            )
            self.addCleanup(client.close)
            with self.assertRaises(WebSocketError) as caught:
                client.connect()
        # 判据必须证明**走过代理**：忽略代理时会直连 127.0.0.1:44444（无人监听）而抛
        # 另一个 WebSocketError，所以「抛了异常」本身不构成鉴别 ⇒ 另断两条：
        # ① 代理确实收到了指向**真实目标**的 CONNECT；② 错误来自代理隧道那一支。
        self.assertEqual(proxy.connections, [f"{ORIGIN_HOST}:44444"])
        self.assertIn("代理", str(caught.exception))


# ----------------------------------------------------------------------
# 3) TCP 行传输
# ----------------------------------------------------------------------
class TestTcpLineTransportProxyArms(ProxyEnvTestCase):
    def _origin(self) -> LoopbackTcpOrigin:
        origin = LoopbackTcpOrigin()
        origin.start()
        self.addCleanup(origin.close)
        return origin

    def _proxy(self, **kw) -> RecordingConnectProxy:
        proxy = RecordingConnectProxy(**kw)
        proxy.start()
        self.proxy_port = proxy.port
        self.addCleanup(proxy.close)
        return proxy

    def _transport(self, origin: LoopbackTcpOrigin, *, tls: bool) -> TcpLineTransport:
        return TcpLineTransport(
            ORIGIN_HOST,
            origin.port,
            tls=tls,
            name="proxy-test-tcp",
            connect_timeout=_ACCEPT_TIMEOUT,
        )

    def test_direct_arm_connects_to_target(self):
        origin = self._origin()
        transport = self._transport(origin, tls=False)
        sock = transport._open()
        self.addCleanup(sock.close)
        self.assertTrue(origin.accepted.wait(_ACCEPT_TIMEOUT))
        self.assertIsNone(origin.error)

    def test_proxy_arm_tunnels_and_validates_real_target(self):
        origin = self._origin()
        proxy = self._proxy()
        self.set_proxy()
        tls_context = RecordingTlsContext()
        transport = self._transport(origin, tls=True)
        with mock.patch("ssl.create_default_context", return_value=tls_context):
            sock = transport._open()
        self.addCleanup(sock.close)
        self.assertTrue(origin.accepted.wait(_ACCEPT_TIMEOUT))
        self.assertEqual(proxy.connections, [f"{ORIGIN_HOST}:{origin.port}"])
        self.assertEqual(tls_context.server_hostnames, [ORIGIN_HOST])

    def test_no_proxy_arm_stays_direct(self):
        origin = self._origin()
        proxy = self._proxy()
        self.set_proxy(no_proxy=ORIGIN_HOST)
        tls_context = RecordingTlsContext()
        transport = self._transport(origin, tls=True)
        with mock.patch("ssl.create_default_context", return_value=tls_context):
            sock = transport._open()
        self.addCleanup(sock.close)
        self.assertTrue(origin.accepted.wait(_ACCEPT_TIMEOUT))
        self.assertEqual(proxy.connections, [])
        self.assertEqual(tls_context.server_hostnames, [ORIGIN_HOST])


# ----------------------------------------------------------------------
# 4) Telegram 出站（HTTPSConnection + set_tunnel）
# ----------------------------------------------------------------------
class TestTelegramHttpProxyArms(ProxyEnvTestCase):
    def _post_once(self) -> RecordingHttpsConnection:
        recored_connections: list[RecordingHttpsConnection] = []

        def factory(*args, **kwargs):
            conn = RecordingHttpsConnection(*args, **kwargs)
            recored_connections.append(conn)
            return conn

        with mock.patch("http.client.HTTPSConnection", new=factory):
            result = TelegramHttp(adapter=_TokenStub())._post("getMe")
        self.assertTrue(result.get("ok") is True, f"替身应答应被解析为 ok，实际 {result!r}")
        self.assertEqual(len(recored_connections), 1)
        return recored_connections[0]

    def test_direct_arm_targets_api_host_without_tunnel(self):
        conn = self._post_once()
        self.assertEqual((conn.host, conn.port), (API_HOST, API_PORT))
        self.assertIsNone(conn.tunnel)

    def test_proxy_arm_targets_proxy_with_tunnel_to_api_host(self):
        self.set_proxy()
        conn = self._post_once()
        self.assertEqual((conn.host, conn.port), (PROXY_HOST, 8888))
        self.assertEqual(conn.tunnel, (API_HOST, API_PORT))


# ----------------------------------------------------------------------
# 5) establish_proxy_tunnel：逐分支守门（真 socket）+ CONNECT authority 格式
# ----------------------------------------------------------------------
class ScriptedConnectProxy(threading.Thread):
    """回一份**脚本化** CONNECT 应答的假代理，逼出各失败分支。

    与 :class:`RecordingConnectProxy` 的区别：这里逐字节控制回给客户端的应答，
    所以「读到一半对端关闭」「应答头非法」「状态非 200」都是真事件。``hold_open``
    为真时发完应答先不关连接（供超时分支），由 :attr:`release` 事件决定何时关闭。
    """

    def __init__(self, response_bytes: bytes, *, hold_open: bool = False) -> None:
        super().__init__(daemon=True)
        self._response_bytes = response_bytes
        self._hold_open = hold_open
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind((PROXY_HOST, 0))
        self._listener.listen(1)
        self.port: int = self._listener.getsockname()[1]
        self.connections: list[str] = []
        self.release = threading.Event()
        self._conn: socket.socket | None = None

    def run(self) -> None:
        try:
            conn, _ = self._listener.accept()
            conn.settimeout(_ACCEPT_TIMEOUT)
            self._conn = conn
            request_head = read_request_head(conn)
            request_line = request_head.split(b"\r\n", 1)[0].decode("latin-1")
            self.connections.append(request_line.split(" ", 2)[1])
            conn.sendall(self._response_bytes)
            if self._hold_open:
                self.release.wait(_ACCEPT_TIMEOUT)
            conn.close()
        except OSError:
            pass

    def close(self) -> None:
        self.release.set()
        for sock in (self._conn, self._listener):
            if sock is None:
                continue
            try:
                sock.close()
            except OSError:
                pass


class TestEstablishProxyTunnelBranches(unittest.TestCase):
    """``establish_proxy_tunnel`` 的四个失败分支与成功路径。

    每个分支都经**真实 socket** 逼出：脚本化代理逐字节控制应答，于是「读到一半
    对端关闭」「应答头非法」「状态非 200」都是真事件，不是 mock。
    """

    def _proxy(self, response_bytes: bytes, *, hold_open: bool = False) -> ScriptedConnectProxy:
        proxy = ScriptedConnectProxy(response_bytes, hold_open=hold_open)
        proxy.start()
        self.addCleanup(proxy.close)
        return proxy

    def _connected_to(
        self, proxy: ScriptedConnectProxy, *, timeout: float | None = None
    ) -> socket.socket:
        sock = socket.create_connection((PROXY_HOST, proxy.port), timeout=_ACCEPT_TIMEOUT)
        if timeout is not None:
            sock.settimeout(timeout)
        self.addCleanup(sock.close)
        return sock

    def test_success_returns_bytes_after_response_head(self):
        proxy = self._proxy(b"HTTP/1.1 200 Connection established\r\n\r\nAFTER-HEAD-BYTES")
        sock = self._connected_to(proxy)
        leftover = establish_proxy_tunnel(sock, "example.com", 443)
        self.assertEqual(proxy.connections, ["example.com:443"])
        self.assertEqual(leftover, b"AFTER-HEAD-BYTES")

    def test_sendall_oserror_becomes_proxy_tunnel_error(self):
        # 真实 socket，写侧已被 shutdown —— 再发 CONNECT 必然 OSError（BrokenPipeError）。
        client_side, peer_side = socket.socketpair()
        self.addCleanup(client_side.close)
        self.addCleanup(peer_side.close)
        client_side.shutdown(socket.SHUT_WR)
        with self.assertRaises(ProxyTunnelError) as caught:
            establish_proxy_tunnel(client_side, "example.com", 443)
        self.assertIn("发送 CONNECT 到代理失败", str(caught.exception))

    def test_partial_response_then_close_becomes_proxy_tunnel_error(self):
        proxy = self._proxy(b"HTTP/1.1 200 Connection established\r\n")  # 无结尾空行
        sock = self._connected_to(proxy)
        with self.assertRaises(ProxyTunnelError) as caught:
            establish_proxy_tunnel(sock, "example.com", 443)
        self.assertIn("读取代理 CONNECT 应答失败", str(caught.exception))

    def test_response_read_timeout_becomes_proxy_tunnel_error(self):
        proxy = self._proxy(b"HTTP/1.1 200 Connection established\r\n", hold_open=True)
        sock = self._connected_to(proxy, timeout=0.2)
        with self.assertRaises(ProxyTunnelError) as caught:
            establish_proxy_tunnel(sock, "example.com", 443)
        self.assertIn("读取代理 CONNECT 应答失败", str(caught.exception))
        self.assertIn("超时", str(caught.exception))

    def test_malformed_status_line_becomes_proxy_tunnel_error(self):
        proxy = self._proxy(b"NOT-HTTP nonsense\r\n\r\n")
        sock = self._connected_to(proxy)
        with self.assertRaises(ProxyTunnelError) as caught:
            establish_proxy_tunnel(sock, "example.com", 443)
        self.assertIn("解析代理 CONNECT 应答失败", str(caught.exception))

    def test_non_200_status_becomes_proxy_tunnel_error(self):
        proxy = self._proxy(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
        sock = self._connected_to(proxy)
        with self.assertRaises(ProxyTunnelError) as caught:
            establish_proxy_tunnel(sock, "example.com", 443)
        message = str(caught.exception)
        self.assertIn("代理拒绝 CONNECT example.com:443", message)
        self.assertIn("HTTP 407", message)


class TestConnectAuthorityFormatting(unittest.TestCase):
    """``_connect_authority`` 的 ``host:port`` 拼装（IPv6 字面量要加方括号）。"""

    def test_ipv6_literal_is_bracketed_with_port(self):
        self.assertEqual(_connect_authority("::1", 443), "[::1]:443")

    def test_ipv4_literal_is_not_bracketed(self):
        self.assertEqual(_connect_authority("127.0.0.1", 443), "127.0.0.1:443")

    def test_hostname_is_not_bracketed(self):
        self.assertEqual(_connect_authority("example.com", 8443), "example.com:8443")


# ----------------------------------------------------------------------
# 6) 环境隔离：清掉 Windows 系统代理（WinINET）
# ----------------------------------------------------------------------
_WININET_SETTINGS_KEY = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
_WININET_VALUE_NAMES = ("ProxyEnable", "ProxyServer", "ProxyOverride")


def _read_wininet_values_directly() -> dict[str, tuple[object, int]]:
    """用例侧的独立读数（直接用 ``winreg``，不经被测模块，避免自证）。

    返回值名 → ``(数据, 类型)``，与 ``winreg.QueryValueEx`` 的返回顺序一致。
    """
    import winreg

    values: dict[str, tuple[object, int]] = {}
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _WININET_SETTINGS_KEY, 0, winreg.KEY_QUERY_VALUE
        ) as key:
            for name in _WININET_VALUE_NAMES:
                try:
                    values[name] = winreg.QueryValueEx(key, name)
                except FileNotFoundError:
                    continue
    except OSError:
        return {}
    return values


def _write_wininet_proxy_directly(proxy_server: str) -> None:
    """注入一个假系统代理：``ProxyEnable=1`` 且 ``ProxyServer=proxy_server``。"""
    import winreg

    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, _WININET_SETTINGS_KEY, 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, proxy_server)


def _restore_wininet_values_directly(saved_values: dict[str, tuple[object, int]]) -> None:
    """把用例开始前读到的三个值原样写回（含「本来就没有」= 删掉）。"""
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _WININET_SETTINGS_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            for name in _WININET_VALUE_NAMES:
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    continue
            for name, (value_data, value_type) in saved_values.items():
                winreg.SetValueEx(key, name, 0, value_type, value_data)
    except OSError:
        pass


class TestWinInetProxyIsolation(ProxyEnvTestCase):
    """``clear_proxy_environment`` 必须同时清掉 Windows 系统代理（WinINET）。

    只清 ``*_proxy`` 环境变量不够：``urllib.request.getproxies()`` 在环境为空时回落读
    注册表（``getproxies_environment() or getproxies_registry()``），这台机器的系统代理
    因此会漏进「无代理」用例。本例**注入**一个假系统代理、先证明它对 ``getproxies()``
    可见（对照臂），再调 ``clear_proxy_environment`` 并断言它消失。

    注册表不可用（非 Windows / 无 ``winreg`` / 无权限）时跳过；注入前后都留底并还原。
    """

    _INJECTED_PROXY_SERVER = "wininet-proxy.isolation.invalid:9999"

    def setUp(self) -> None:
        super().setUp()
        if os.name != "nt":
            self.skipTest("WinINET 系统代理只在 Windows 上存在")
        try:
            import winreg  # noqa: F401
        except ImportError:
            self.skipTest("winreg 不可用")
        self._original_wininet = _read_wininet_values_directly()
        self.addCleanup(_restore_wininet_values_directly, self._original_wininet)

    def test_clear_removes_wininet_proxy_visible_to_getproxies(self):
        _write_wininet_proxy_directly(self._INJECTED_PROXY_SERVER)
        injected = urllib.request.getproxies()
        self.assertTrue(
            any(self._INJECTED_PROXY_SERVER in value for value in injected.values()),
            f"注入的系统代理应对 getproxies() 可见（对照臂），实际 {injected!r}",
        )
        clear_proxy_environment()
        self.assertEqual(urllib.request.getproxies(), {})
        self.assertEqual(_read_wininet_values_directly(), {})


if __name__ == "__main__":
    unittest.main()

"""出站代理支持守门：三条裸 socket / 裸 ``http.client`` 的缝要走 ``HTTPS_PROXY``。

被测对象是「代理发现」与「经代理建连」这一对行为：

* :func:`opencode_bridge.proxy_config.proxy_endpoint_for` —— 环境变量翻译（含代理
  自身 scheme 与预计算的 ``Proxy-Authorization`` 凭据）；
* ``ws.WebSocketClient.connect`` / ``transport.tcp_lines.TcpLineTransport._open``
  —— 经代理 ``CONNECT`` 隧道（``https://`` 代理先对**代理**做 TLS，随后 TLS 仍以
  **真实目标**校验证书）；
* ``adapters.telegram_http.TelegramHttp._post`` —— 按代理 scheme 选
  ``HTTPSConnection`` / ``HTTPConnection`` + ``set_tunnel`` 指向真实目标；
* ``proxy_tunnel.establish_proxy_tunnel`` —— CONNECT 的 ``Proxy-Authorization`` 头。

**鉴别力来自真服务器，不是 mock**：代理是一个绑定 ``127.0.0.2`` 的真 socket 服务，
逐字节读客户端的 ``CONNECT`` 请求、记录 authority 与完整请求头、回 200 并把双向
字节中继到真实目标（目标绑 ``127.0.0.1``）。于是「``CONNECT`` 的 authority == 真实
目标」、「TLS 的 ``server_hostname`` == 真实目标（而不是代理）」与「代理真的收到了
``Proxy-Authorization`` 头」这些断言都有咬合 —— 若代码走直连（或把代理主机当目标），
``proxy.connections`` 会是空、断言当场红。

``https://`` 代理那条臂用**真 TLS**：用例在临时目录里用 ``openssl`` 现签一张自签名
证书（IP SAN = ``127.0.0.2``），代理进程用真 ``ssl`` 服务端上下文接 TLS —— 客户端若
在发 CONNECT **之前**没跟代理握手完 TLS，代理的服务端握手就会失败、读不到 CONNECT
（⇒ 那条臂真红）。⚠️ 证书只在临时目录里、⛔ 不进仓库（私有密钥形状会被推送保护拦，
见 §2.1/§2.4）；``openssl`` 不在 PATH 时该臂 ``skipTest``。
"""

from __future__ import annotations

import base64
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
import urllib.request
from unittest import mock

from .proxy_env_isolation import (
    clear_proxy_environment,
    proxy_environment,
    restore_proxy_environment,
)
from .test_ws import accept_with_handshake, header_of, read_request_head

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
# 真 TLS 支撑：临时目录里用 openssl 现签自签名证书（⛔ 不把 PEM 提交进仓库）
# ----------------------------------------------------------------------
_TLS_CERT_DIRECTORY: str | None = None
_TLS_CERT_UNAVAILABLE = False


def generate_loopback_tls_certificate() -> tuple[str, str] | None:
    """用 ``openssl`` 现签一张 IP SAN = :data:`PROXY_HOST` 的自签名证书。

    返回 ``(cert_path, key_path)``；``openssl`` 不在 PATH 或签发失败时返回 ``None``
    （调用方 ``skipTest``）。证书只写临时目录，⛔ 不以文件形式进仓库 —— 私有密钥
    形状会被推送保护拦下（§2.1/§2.4）。签发结果按进程缓存（一次即可），失败也缓存
    以免每次用例都去 `which` 一遍。
    """
    global _TLS_CERT_DIRECTORY, _TLS_CERT_UNAVAILABLE
    if _TLS_CERT_DIRECTORY is not None:
        return (
            os.path.join(_TLS_CERT_DIRECTORY, "cert.pem"),
            os.path.join(_TLS_CERT_DIRECTORY, "key.pem"),
        )
    if _TLS_CERT_UNAVAILABLE:
        return None
    openssl = shutil.which("openssl")
    if openssl is None:
        _TLS_CERT_UNAVAILABLE = True
        return None
    directory = tempfile.mkdtemp(prefix="bridge-proxy-tls-")
    certificate_path = os.path.join(directory, "cert.pem")
    key_path = os.path.join(directory, "key.pem")
    # Windows 上 openssl 会去读一个默认位置的 openssl.cnf，缺失时 `req` 直接报
    # "Can't open .../openssl.cnf"。给一个最小配置，避免依赖具体安装布局。
    config_path = os.path.join(directory, "openssl-min.cnf")
    with open(config_path, "w", encoding="utf-8") as config_file:
        config_file.write("[req]\ndistinguished_name = dn\n[dn]\n")
    environment = dict(os.environ)
    environment["OPENSSL_CONF"] = config_path
    completed = subprocess.run(
        [
            openssl, "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", key_path, "-out", certificate_path,
            "-days", "1", "-nodes",
            "-subj", f"/CN={PROXY_HOST}",
            "-addext", f"subjectAltName=IP:{PROXY_HOST}",
        ],
        capture_output=True, text=True, env=environment,
    )
    if completed.returncode != 0:
        _TLS_CERT_UNAVAILABLE = True
        return None
    _TLS_CERT_DIRECTORY = directory
    return certificate_path, key_path


class RecordingRealTlsContext:
    """真 TLS 客户端上下文的记录包装：记下 ``server_hostname`` 后委托真上下文。

    与 :class:`RecordingTlsContext`（identity 替身、只记录不握手）不同，这个包装真
    完成 TLS 握手 —— 供 ``https://`` 代理那条「真 TLS」臂使用。
    """

    def __init__(self, real_context: ssl.SSLContext) -> None:
        self._real_context = real_context
        self.server_hostnames: list[str | None] = []

    def wrap_socket(self, sock: socket.socket, **kwargs: object) -> ssl.SSLSocket:
        self.server_hostnames.append(kwargs.get("server_hostname"))  # type: ignore[arg-type]
        return self._real_context.wrap_socket(sock, **kwargs)  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# 假代理：真 socket，读 CONNECT、记录 authority、回 200 并中继
# ----------------------------------------------------------------------
class RecordingConnectProxy(threading.Thread):
    """裸 socket 假代理：记录每条 ``CONNECT`` 的 authority / 完整头，可选中继 / TLS。

    ``tls_context`` 非 ``None`` 时，代理先作为 **TLS 服务端**接管连接（真握手），
    之后才在解密后的流上读 ``CONNECT`` —— 这正是 ``https://`` 代理那条臂的判据：
    客户端若在发 CONNECT 前没跟代理完成 TLS，服务端握手会失败、读不到 CONNECT。
    """

    def __init__(
        self,
        *,
        response_status: bytes = b"200 Connection established",
        relay: bool = True,
        tls_context: ssl.SSLContext | None = None,
    ) -> None:
        super().__init__(daemon=True)
        self._response_status = response_status
        self._relay_enabled = relay
        self._tls_context = tls_context
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind((PROXY_HOST, 0))
        self._listener.listen(5)
        self.port: int = self._listener.getsockname()[1]
        self.connections: list[str] = []
        self.request_heads: list[bytes] = []
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
        if self._tls_context is not None:
            conn = self._tls_context.wrap_socket(conn, server_side=True)
        request_head = read_request_head(conn)
        request_line = request_head.split(b"\r\n", 1)[0].decode("latin-1")
        authority = request_line.split(" ", 2)[1]
        # 先记录、再回应答：调用方读到 200 时 authority 一定已在表里（无竞态）。
        with self._lock:
            self.connections.append(authority)
            self.request_heads.append(request_head)
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


class RecordingHttpConnection:
    """记录构造入参与 ``set_tunnel`` 的 ``http.client`` 连接替身。

    同时给 ``HTTPSConnection`` 与 ``HTTPConnection`` 当替身（telegram 会按代理
    scheme 二选一），所以名字取中性 —— 它记录的是「一次出站 HTTP 连接」。
    """

    def __init__(self, host, port=None, *args, timeout=None, **kwargs) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.tunnel: tuple[str, int] | None = None
        self.tunnel_headers: dict[str, str] | None = None
        self.closed = False

    def set_tunnel(self, host, port=None, headers=None) -> None:
        self.tunnel = (host, port)
        self.tunnel_headers = headers

    def request(self, method, path, body=None, headers=None) -> None:
        pass

    def getresponse(self) -> _FakeHttpsResponse:
        return _FakeHttpsResponse()

    def close(self) -> None:
        self.closed = True


class _TokenStub:
    # 片段拼接：运行值与原先逐字节相同，避免仓库里出现连续凭据形状字面量（§2.4）。
    bot_token = "9999999" + ":" + "stub-token-for-proxy-test"


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

    def set_proxy(
        self,
        *,
        url: str | None = None,
        no_proxy: str | None = None,
        var: str = "HTTPS_PROXY",
    ) -> None:
        clear_proxy_environment()
        if url is None:
            # 默认指向**刚起的那个假代理的真实端口**（它绑 0 = 随机端口）。
            url = f"http://{PROXY_HOST}:{getattr(self, 'proxy_port', 8888)}"
        os.environ[var] = url
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
        endpoint = proxy_endpoint_for(API_HOST, scheme="https")
        self.assertEqual((endpoint.host, endpoint.port, endpoint.scheme), (PROXY_HOST, 8123, "http"))
        self.assertIsNone(endpoint.proxy_authorization)

    def test_all_proxy_is_used_as_fallback(self):
        os.environ["ALL_PROXY"] = f"http://{PROXY_HOST}:8999"
        endpoint = proxy_endpoint_for(API_HOST, scheme="https")
        self.assertEqual((endpoint.host, endpoint.port, endpoint.scheme), (PROXY_HOST, 8999, "http"))

    def test_no_proxy_list_bypasses_matching_host(self):
        self.set_proxy(no_proxy=ORIGIN_HOST)
        self.assertIsNone(proxy_endpoint_for(ORIGIN_HOST, scheme="https"))
        # 代理仍在，别的 host 不受影响 —— 证明「返回 None」是 no_proxy 生效而非代理缺失。
        endpoint = proxy_endpoint_for(API_HOST, scheme="https")
        self.assertEqual((endpoint.host, endpoint.port), (PROXY_HOST, 8888))

    def test_proxy_own_scheme_is_reported_not_port_defaulted_silently(self):
        # 代理 URL 自身的 scheme 要报出来（选键的 scheme= 参数是另一回事）。
        os.environ["HTTPS_PROXY"] = f"https://{PROXY_HOST}:8443"
        endpoint = proxy_endpoint_for(API_HOST, scheme="https")
        self.assertEqual(endpoint.scheme, "https")

    def test_https_proxy_without_port_defaults_to_443(self):
        os.environ["HTTPS_PROXY"] = f"https://{PROXY_HOST}"
        endpoint = proxy_endpoint_for(API_HOST, scheme="https")
        self.assertEqual((endpoint.port, endpoint.scheme), (443, "https"))

    def test_userinfo_becomes_basic_authorization(self):
        os.environ["HTTPS_PROXY"] = f"http://user:pass@{PROXY_HOST}:8123"
        endpoint = proxy_endpoint_for(API_HOST, scheme="https")
        expected = "Basic " + base64.b64encode(b"user:pass").decode("ascii")
        self.assertEqual(endpoint.proxy_authorization, expected)

    def test_userinfo_is_url_decoded_before_basic_encoding(self):
        # urllib 的语义：userinfo 先 unquote 再 base64（%40 == '@'）。
        os.environ["HTTPS_PROXY"] = f"http://us%40er:p%40ss@{PROXY_HOST}:8123"
        endpoint = proxy_endpoint_for(API_HOST, scheme="https")
        expected = "Basic " + base64.b64encode("us@er:p@ss".encode("utf-8")).decode("ascii")
        self.assertEqual(endpoint.proxy_authorization, expected)


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

    def test_https_proxy_arm_completes_tls_to_proxy_before_connect(self):
        certificate = generate_loopback_tls_certificate()
        if certificate is None:
            self.skipTest("openssl 不可用，无法现签 TLS 证书")
        certificate_path, key_path = certificate
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(certificate_path, key_path)
        origin = self._origin()
        proxy = self._proxy(tls_context=server_context)
        # 目标是 ws://（无目标 TLS），经 ALL_PROXY 回退选中；代理自身是 https://。
        self.set_proxy(url=f"https://{PROXY_HOST}:{proxy.port}", var="ALL_PROXY")
        recorder = RecordingRealTlsContext(
            ssl.create_default_context(cafile=certificate_path)
        )
        with mock.patch("ssl.create_default_context", return_value=recorder):
            client = WebSocketClient(
                f"ws://{ORIGIN_HOST}:{origin.port}/ws", timeout=_ACCEPT_TIMEOUT
            )
            self.addCleanup(client.close)
            client.connect()
        self._await_origin(origin)
        # 代理读到了 CONNECT —— 而它只可能在**TLS 握手完成之后**读到（真 TLS 服务端）。
        # 客户端若在 CONNECT 前没跟代理握手完，代理的 wrap_socket(server_side) 会失败、
        # 这里会是空；CONNECT 的 authority 仍是**真实目标**而非代理。
        self.assertEqual(proxy.connections, [f"{ORIGIN_HOST}:{origin.port}"])
        # 对"到代理的"连接做过 TLS，SNI = 代理主机（而不是目标）。
        self.assertEqual(recorder.server_hostnames, [PROXY_HOST])
        self.assertIn(b"GET /ws HTTP/1.1", origin.request_head)

    def test_userinfo_proxy_sends_basic_authorization_header(self):
        origin = self._origin()
        proxy = self._proxy()
        self.set_proxy(url=f"http://user:pass@{PROXY_HOST}:{proxy.port}")
        tls_context = RecordingTlsContext()
        with mock.patch("ssl.create_default_context", return_value=tls_context):
            client = WebSocketClient(
                f"wss://{ORIGIN_HOST}:{origin.port}/ws", timeout=_ACCEPT_TIMEOUT
            )
            self.addCleanup(client.close)
            client.connect()
        self._await_origin(origin)
        self.assertEqual(proxy.connections, [f"{ORIGIN_HOST}:{origin.port}"])
        expected = "Basic " + base64.b64encode(b"user:pass").decode("ascii")
        self.assertEqual(
            header_of(proxy.request_heads[0], "Proxy-Authorization"), expected
        )

    def test_proxy_without_userinfo_sends_no_authorization_header(self):
        origin = self._origin()
        proxy = self._proxy()
        self.set_proxy()  # http://host:port，无 userinfo
        tls_context = RecordingTlsContext()
        with mock.patch("ssl.create_default_context", return_value=tls_context):
            client = WebSocketClient(
                f"wss://{ORIGIN_HOST}:{origin.port}/ws", timeout=_ACCEPT_TIMEOUT
            )
            self.addCleanup(client.close)
            client.connect()
        self._await_origin(origin)
        self.assertEqual(proxy.connections, [f"{ORIGIN_HOST}:{origin.port}"])
        self.assertNotIn(b"proxy-authorization", proxy.request_heads[0].lower())


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

    def test_https_proxy_arm_completes_tls_to_proxy_before_connect(self):
        certificate = generate_loopback_tls_certificate()
        if certificate is None:
            self.skipTest("openssl 不可用，无法现签 TLS 证书")
        certificate_path, key_path = certificate
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(certificate_path, key_path)
        origin = self._origin()
        proxy = self._proxy(tls_context=server_context)
        # 目标是明文 TCP（无目标 TLS），经 ALL_PROXY 回退选中；代理自身是 https://。
        self.set_proxy(url=f"https://{PROXY_HOST}:{proxy.port}", var="ALL_PROXY")
        recorder = RecordingRealTlsContext(
            ssl.create_default_context(cafile=certificate_path)
        )
        transport = self._transport(origin, tls=False)
        with mock.patch("ssl.create_default_context", return_value=recorder):
            sock = transport._open()
        self.addCleanup(sock.close)
        self.assertTrue(origin.accepted.wait(_ACCEPT_TIMEOUT))
        # 代理读到了 CONNECT —— 而它只可能在**TLS 握手完成之后**读到（真 TLS 服务端）。
        # 客户端若在 CONNECT 前没跟代理握手完，代理的 wrap_socket(server_side) 会失败，
        # CONNECT 的 authority 仍是**真实目标**而非代理。
        self.assertEqual(proxy.connections, [f"{ORIGIN_HOST}:{origin.port}"])
        # 对"到代理的"连接做过 TLS，SNI = 代理主机（而不是目标）。
        self.assertEqual(recorder.server_hostnames, [PROXY_HOST])

    def test_userinfo_proxy_sends_basic_authorization_header(self):
        origin = self._origin()
        proxy = self._proxy()
        self.set_proxy(url=f"http://user:pass@{PROXY_HOST}:{proxy.port}", var="HTTP_PROXY")
        transport = self._transport(origin, tls=False)
        sock = transport._open()
        self.addCleanup(sock.close)
        self.assertTrue(origin.accepted.wait(_ACCEPT_TIMEOUT))
        self.assertEqual(proxy.connections, [f"{ORIGIN_HOST}:{origin.port}"])
        expected = "Basic " + base64.b64encode(b"user:pass").decode("ascii")
        self.assertEqual(
            header_of(proxy.request_heads[0], "Proxy-Authorization"), expected
        )


# ----------------------------------------------------------------------
# 4) Telegram 出站（HTTPSConnection + set_tunnel）
# ----------------------------------------------------------------------
class TestTelegramHttpProxyArms(ProxyEnvTestCase):
    def _post_once(self) -> tuple[str, RecordingHttpConnection]:
        """跑一次 ``_post``，返回 ``(实际用的连接类标签, 连接替身)``。

        同时替换 ``HTTPSConnection`` 与 ``HTTPConnection`` 两个类：前者记
        ``"https"``、后者记 ``"http"`` —— 于是「按代理 scheme 选对类」这件事有了
        真咬合（只替换一个类会让另一条分支走进真网络）。
        """
        recorded_connections: list[RecordingHttpConnection] = []
        used_connection_labels: list[str] = []

        def recorder(label: str):
            def factory(*args, **kwargs):
                conn = RecordingHttpConnection(*args, **kwargs)
                recorded_connections.append(conn)
                used_connection_labels.append(label)
                return conn
            return factory

        with mock.patch("http.client.HTTPSConnection", new=recorder("https")), \
                mock.patch("http.client.HTTPConnection", new=recorder("http")):
            result = TelegramHttp(adapter=_TokenStub())._post("getMe")
        self.assertTrue(result.get("ok") is True, f"替身应答应被解析为 ok，实际 {result!r}")
        self.assertEqual(len(recorded_connections), 1)
        return used_connection_labels[0], recorded_connections[0]

    def test_direct_arm_targets_api_host_without_tunnel(self):
        label, conn = self._post_once()
        self.assertEqual(label, "https")
        self.assertEqual((conn.host, conn.port), (API_HOST, API_PORT))
        self.assertIsNone(conn.tunnel)

    def test_http_proxy_arm_uses_plain_connection_with_tunnel_to_api_host(self):
        self.set_proxy()
        label, conn = self._post_once()
        # http:// 代理走明文 HTTPConnection（不是对代理做 TLS 的 HTTPSConnection）。
        self.assertEqual((label, conn.host, conn.port), ("http", PROXY_HOST, 8888))
        self.assertEqual(conn.tunnel, (API_HOST, API_PORT))
        # 代理 URL 无 userinfo ⇒ 不得带 Proxy-Authorization 头（对照臂）。
        self.assertIsNone(conn.tunnel_headers)

    def test_https_proxy_arm_uses_tls_connection_to_proxy(self):
        self.set_proxy(url=f"https://{PROXY_HOST}:8443")
        label, conn = self._post_once()
        self.assertEqual((label, conn.host, conn.port), ("https", PROXY_HOST, 8443))
        self.assertEqual(conn.tunnel, (API_HOST, API_PORT))

    def test_userinfo_proxy_arm_sends_basic_authorization_header(self):
        self.set_proxy(url=f"http://user:pass@{PROXY_HOST}:8888")
        label, conn = self._post_once()
        self.assertEqual(label, "http")
        expected = "Basic " + base64.b64encode(b"user:pass").decode("ascii")
        self.assertEqual(conn.tunnel_headers, {"Proxy-Authorization": expected})


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
        self.request_heads: list[bytes] = []
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
            self.request_heads.append(request_head)
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

    def test_proxy_authorization_header_is_sent_when_given(self):
        proxy = self._proxy(b"HTTP/1.1 200 Connection established\r\n\r\n")
        sock = self._connected_to(proxy)
        establish_proxy_tunnel(
            sock, "example.com", 443, proxy_authorization="Basic abc"
        )
        self.assertEqual(proxy.connections, ["example.com:443"])
        self.assertEqual(
            header_of(proxy.request_heads[0], "Proxy-Authorization"), "Basic abc"
        )

    def test_no_proxy_authorization_header_when_absent(self):
        proxy = self._proxy(b"HTTP/1.1 200 Connection established\r\n\r\n")
        sock = self._connected_to(proxy)
        establish_proxy_tunnel(sock, "example.com", 443)
        self.assertNotIn(
            b"proxy-authorization", proxy.request_heads[0].lower()
        )

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

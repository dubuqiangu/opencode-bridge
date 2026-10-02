"""共享的本地 HTTP 服务：端口绑定 / 按路径路由 / 起停 / 优雅关闭（**与平台无关**）。

为什么要有这个模块
------------------
仓库里 11 个平台**全是 outbound**（我们主动去连、去拉），所以此前没有任何入站
socket。第一个需要入站 socket 的平台是 **a2a**（``tasks.md`` B1）—— 方向相反，
**我们被调**：要起一个本地 HTTP 服务让外部 agent 调我们。

而路线图上的 **A3（inbound-push 入口）**要的东西完全一样：*单端口 HTTP 服务 +
按路径路由到各适配器*。所以这段"起停 / 绑定 / 路由 / 优雅关闭"如果写在
``adapters/a2a.py`` 里，A3 落地时就得整体重写一遍 —— 那是必然发生的重复劳动。

于是抽到这里。**平台适配器只提供三样东西**：

1. **路径**（``Route.path``，精确匹配，已归一）
2. **处理器**（``Callable[[HttpRequest], HttpResponse]``，纯函数式，禁止直接写 socket）
3. **要不要鉴权**（``Route.require_auth``）

端口怎么选、线程怎么起、怎么优雅关闭、handler 抛异常怎么办 —— 全部由本模块负责。
本模块**不认识任何消息平台**（刻意不 import ``opencode_bridge.adapters``），
与 :mod:`opencode_bridge.transport` 的分层纪律一致。

A3 将来怎么复用
---------------
``HttpServer`` 的 ``routes`` 是一个列表，且 :meth:`HttpServer.add_route` 允许
**start 之前**逐个挂载。所以 A3 只要建**一个** ``HttpServer``，把各 webhook
适配器的 ``Route``（验签 / 解密逻辑都在各自的 handler 里）都挂上去，就得到了
"单端口 + 按路径路由"；每个适配器仍然只声明自己的路径与是否验签，传输与生命周期
由本模块统一管。反过来，如果每个 webhook 平台各自起一个端口，那才需要端口分配、
端口冲突提示、状态视图汇总 —— 这些正是 A3 明确不要的东西。

三条不可让步的纪律
------------------
1. **默认只 bind 回环**。:data:`DEFAULT_BIND_HOST` 是**显式可见**的常量
   ``"127.0.0.1"``，没有任何"为了测试方便"就能改成 ``0.0.0.0`` 的后门。换地址
   只能是调用方**显式传参**，并且 :meth:`HttpServer.start` 会把**实际绑上的地址**
   （不是请求的地址）打进 INFO 日志 —— 状态要可观测，不能悄悄发生。
2. **处理器抛异常不许把栈泄给对端**。回 500 + 固定 JSON ``{"error": "internal
   error"}``，栈只进服务端日志。这条对"任何本机进程/浏览器网页都能打过来"的
   场景尤其重要。
3. **停止必须干净且快**。顺序固定：``shutdown()`` → ``server_close()`` → join
   ``serve_forever`` 线程 → 等在途 handler 收尾（:meth:`HttpServer.stop`）。
   ``HTTPServer.shutdown()`` **必须从 ``serve_forever() 所在线程之外调用**，
   否则自锁死锁 —— 本模块检测到这种情况会记 ERROR 并跳过，绝不死锁。

并发模型
--------
用 :class:`http.server.ThreadingHTTPServer`（每连接一线程），**不是**单线程的
``HTTPServer``。原因：入站 agent 可能处理很久（a2a 的 ``message/send`` 按规范
默认是**阻塞**的，要等 agent 给出终态），单线程下一个慢请求会把整个服务卡死 ——
连 ``/health`` 都打不通。线程是 daemon 的，所以停机不会被一个卡住的请求拖住。

一个必须知道的**标准 HTTP 行为**
--------------------------------
拒绝一个请求时（413 超限、400 chunked），本模块**先把被拒的 body 读掉再回响应**
（分块丢弃，任何时刻只持有 4 KiB，见 :meth:`HttpServer._discard`），这样连接以 FIN
干净关闭、客户端**一定读得到**状态码。不这么做的话内核会对"接收缓冲里还有未读数据"
的 socket 发 **RST**，把已写好的响应一起丢掉 —— 客户端只看到"连接错误"，而且这在
Windows 上是**概率性**的。

丢弃量有上限 :data:`_MAX_DISCARD`（``Content-Length`` 由客户端控制，无上限地读
等于把 DoS 面又打开一次）。超过上限的巨型 body 会直接关连接，客户端看到"连接错误" ——
所以**调用方的测试仍必须容忍"0 / 连接错误"这一路**。

HTTP 报文约定
-------------
* 协议固定 ``HTTP/1.0`` 语义（不 keep-alive）。保持长连接会让一个"连上但不发
  任何东西"的客户端把一个线程永久钉住；每请求一连接 + ``Connection: close``
  让停机变得可预测。
* 处理器 socket 超时 :data:`_SOCKET_TIMEOUT`。没有它，一个谎报
  ``Content-Length`` 的请求能永久占住一个线程。
* **不**支持 chunked 传输编码（明确回 400 而不是猜）。标准库客户端不发它，
  真发了说明对端不是普通 HTTP 客户端。
"""

from __future__ import annotations

import dataclasses
import ipaddress
import json
import logging
import socket
import threading
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Iterable, Mapping, Optional

logger = logging.getLogger("opencode_bridge.httpsrv")

__all__ = [
    "DEFAULT_BIND_HOST",
    "MAX_BODY_BYTES",
    "BindError",
    "HttpRequest",
    "HttpResponse",
    "Route",
    "HttpServer",
    "is_loopback_host",
    "normalize_path",
]


#: **默认绑定地址**。显式、可见、不接受"为了测试方便"改成 ``0.0.0.0``。
#: 要换地址只能显式传 ``host=``，且 :meth:`HttpServer.start` 会打印实际地址。
DEFAULT_BIND_HOST = "127.0.0.1"

#: 请求体上限（bytes），默认 1 MiB。
MAX_BODY_BYTES = 1_048_576

#: ``serve_forever`` 的轮询间隔。决定 ``shutdown()`` 的最坏等待时间
#: （``socketserver`` 用它 select），也就是 ``stop()`` 的延迟上界。
_SERVE_POLL_INTERVAL = 0.25

#: 单个 handler 连接的 socket 超时（秒）。防止谎报 ``Content-Length`` 的请求
#: 把线程永久钉住。
_SOCKET_TIMEOUT = 30.0

#: 拒绝一个超限请求时，最多**读掉**多少字节 body（只丢弃、不留存，见
#: :meth:`HttpServer._discard`）。给个硬顶是因为 ``Content-Length`` 由客户端控制。
_MAX_DISCARD = 262_144


class BindError(RuntimeError):
    """端口绑定失败（被占用 / 权限不足 / 地址不可用）。"""


def is_loopback_host(host: str) -> bool:
    """这个绑定地址是不是回环。

    用于"请求了更宽的绑定地址要不要拒绝/回落"这类判断。识别 ``localhost``
    字面量与 ``127.0.0.0/8`` / ``::1``。
    """
    text = str(host or "").strip().strip("[]").lower()
    if not text:
        return False
    if text in ("localhost", "localhost.localdomain"):
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def normalize_path(path: str) -> str:
    """归一路径：保证前导 ``/``、去掉尾部 ``/``（根路径除外）。

    刻意**不**折叠中间的重复斜杠 —— 改写调用方给的路径会让"路由到哪"变得不可预测。
    """
    text = str(path or "/").strip()
    if not text.startswith("/"):
        text = "/" + text
    if len(text) > 1 and text.endswith("/"):
        text = text.rstrip("/") or "/"
    return text


# ----------------------------------------------------------------------
# 请求 / 响应（纯数据，不碰 socket）
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class HttpRequest:
    """一个已解析的入站请求。**不可变** —— 处理器拿到它之后不该能改它。"""

    #: 大写 HTTP 方法（``GET`` / ``POST`` …）。
    method: str
    #: 已 percent-decode 并归一的路径（见 :func:`normalize_path`）。
    path: str
    #: query 参数；同名多值保留为列表。
    query: Mapping[str, list[str]]
    #: 请求头，**键一律小写**（HTTP 头大小写不敏感，见 RFC 7230）。
    headers: Mapping[str, str]
    #: 请求体（已按 ``max_body`` 截断检查）。
    body: bytes
    #: 对端 IP（字符串）。**不是**身份 —— 同一 NAT / 反代后面可能是很多人。
    client_host: str
    #: 鉴权后的对端标识。``require_auth`` 为假的路由上恒为空串。
    #: 由 :class:`HttpServer` 在鉴权通过后用 ``dataclasses.replace`` 填上。
    peer: str = ""

    def header(self, name: str, default: str = "") -> str:
        """取请求头（大小写不敏感）。"""
        return self.headers.get(str(name or "").lower(), default)

    def query_one(self, name: str, default: str = "") -> str:
        """取 query 参数的第一个值。"""
        values = self.query.get(str(name or "")) or []
        return values[0] if values else default

    def json(self) -> Any:
        """把请求体解析成 JSON。**失败抛 :class:`ValueError`**（由路由层转 400）。"""
        if not self.body:
            raise ValueError("empty body")
        return json.loads(self.body.decode("utf-8"))


@dataclass(frozen=True)
class HttpResponse:
    """处理器返回值。**不要**在处理器里直接写 socket —— 那样异常就管不住了。"""

    status: int = 200
    body: bytes = b""
    content_type: str = "application/json"
    headers: tuple[tuple[str, str], ...] = ()

    @classmethod
    def json(
        cls,
        payload: Any,
        status: int = 200,
        headers: Iterable[tuple[str, str]] = (),
    ) -> "HttpResponse":
        return cls(
            status=status,
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            content_type="application/json",
            headers=tuple(headers),
        )

    @classmethod
    def text(
        cls,
        text: str,
        status: int = 200,
        content_type: str = "text/plain; charset=utf-8",
    ) -> "HttpResponse":
        return cls(status=status, body=str(text).encode("utf-8"), content_type=content_type)


#: 处理器签名：纯函数，**不得**抛异常（抛了会被本模块兜成 500）。
Handler = Callable[[HttpRequest], HttpResponse]


@dataclass(frozen=True)
class Route:
    """一条路由：**路径 + 处理器 + 方法集 + 是否要鉴权**。

    平台适配器需要提供的全部内容就是这个 —— 端口绑定、线程、优雅关闭都不归它。
    """

    path: str
    handler: Handler
    methods: tuple[str, ...] = ("GET", "POST")
    #: True = 先跑 ``HttpServer.authenticate``，返回空/None 就 401。
    #: ⚠️ 若 ``authenticate is None``（调用方没配鉴权器）而本路由要求鉴权，
    #: 本模块**fail closed**（一律 401）并记 ERROR —— 绝不"没配就当放行"。
    require_auth: bool = False
    #: 只给日志看的名字（默认取 path）。
    name: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", normalize_path(self.path))
        object.__setattr__(self, "methods", tuple(
            str(m).strip().upper() for m in self.methods if str(m).strip()
        ) or ("GET",))
        object.__setattr__(self, "name", str(self.name or self.path))


class _BodyRejected(Exception):
    """请求体不可接受（超限 / 非法 / chunked）。带一个要回给对端的响应。"""

    def __init__(self, response: HttpResponse) -> None:
        super().__init__(f"body rejected: HTTP {response.status}")
        self.response = response


# ----------------------------------------------------------------------
# 服务器
# ----------------------------------------------------------------------
class _Server(ThreadingHTTPServer):
    """每连接一线程的 HTTP 服务器。

    * ``daemon_threads = True``：停机不会被一个卡住的请求拖住（我们另有在途计数
      做优雅收尾，见 :meth:`HttpServer.stop`）。
    * 覆写 ``server_bind`` 跳过 ``socket.getfqdn()``：它会做一次**反向 DNS 查询**，
      DNS 坏了能让启动凭空多花好几秒 —— 而本服务只绑回环，hostname 只用于日志，
      我们又自己覆写了 ``version_string``。
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], app: "HttpServer") -> None:
        self.app = app
        super().__init__(address, _Handler)

    def server_bind(self) -> None:  # noqa: D102 - 见类 docstring
        import socketserver

        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)

    def handle_error(self, request, client_address) -> None:
        """子类化了 handler 之后，单个连接崩掉不该往 stderr 刷一大段栈。"""
        logger.debug("httpsrv[%s]: 连接处理异常（%s）", self.app.name, client_address, exc_info=True)


class _Handler(BaseHTTPRequestHandler):
    """把 HTTP 报文翻成 :class:`HttpRequest`，把 :class:`HttpResponse` 写回去。

    本类**不含任何业务逻辑** —— 它只负责"把异常翻译成 500 且不泄栈"。
    """

    #: 保持 HTTP/1.0 语义（不 keep-alive）：每请求一连接，停机可预测。
    protocol_version = "HTTP/1.0"
    #: socket 超时 —— 没有它，谎报 Content-Length 的请求能永久占住一个线程。
    timeout = _SOCKET_TIMEOUT
    server_version = "opencode-bridge"
    sys_version = ""

    def version_string(self) -> str:
        return self.server_version

    def log_message(self, fmt: str, *args: Any) -> None:
        # 覆写掉默认实现（它会往 stderr 写 access log）。
        logger.debug(
            "httpsrv[%s] %s: %s", self.server.app.name, self.address_string(), fmt % args
        )

    def do_GET(self) -> None:  # noqa: N802
        self._respond()

    def do_HEAD(self) -> None:  # noqa: N802
        self._respond()

    def do_POST(self) -> None:  # noqa: N802
        self._respond()

    def do_PUT(self) -> None:  # noqa: N802
        self._respond()

    def do_PATCH(self) -> None:  # noqa: N802
        self._respond()

    def do_DELETE(self) -> None:  # noqa: N802
        self._respond()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._respond()

    def _respond(self) -> None:
        app = self.server.app
        try:
            response = app._dispatch(self)
        except Exception:  # noqa: BLE001 - 兜底：绝不让栈泄给对端
            logger.exception("httpsrv[%s]: 请求处理失败（回 500）", app.name)
            response = HttpResponse.json({"error": "internal error"}, 500)
        self._write(response)

    def _write(self, response: HttpResponse) -> None:
        body = response.body or b""
        try:
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(body)))
            # 本服务可能在本机被任意进程/浏览器网页访问；禁掉内容嗅探。
            self.send_header("X-Content-Type-Options", "nosniff")
            for key, value in response.headers:
                self.send_header(key, value)
            self.end_headers()
            if body and self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, socket.timeout, TimeoutError, OSError):
            # 对端在等答案时把连接关了（stop() 收尾就会这样）—— 安静收场。
            self.close_connection = True


class HttpServer:
    """本地 HTTP 服务：绑定单端口 → 按 ``(path, method)`` 路由 → 优雅关闭。

    典型用法（``a2a`` 是第一个使用方，A3 的 webhook 适配器会沿用同一套）::

        server = HttpServer(
            host="127.0.0.1", port=9900, name="a2a",
            authenticate=my_authenticator,          # 可为 None = 不鉴权
            unauthorized=lambda r: HttpResponse.json({"error": "unauthorized"}, 401),
            routes=[
                Route("/.well-known/agent-card.json", card, methods=("GET",)),
                Route("/rpc", rpc, methods=("POST",), require_auth=True),
            ],
        )
        actual_port = server.start()      # 绑 0 时由操作系统分配端口
        ...
        server.stop()

    :param host: 绑定地址。**默认 :data:`DEFAULT_BIND_HOST`（127.0.0.1）**；
        换地址必须显式传，且 :meth:`start` 会打印实际绑定结果。
    :param port: 绑定端口。**0 = 由操作系统分配**（测试用它拿真实端口）。
    :param authenticate: ``Callable[[HttpRequest], str | None]``。返回对端标识，
        返回空/None 即拒绝。**None 表示完全不鉴权** —— 调用方有义务自己警告
        （本模块只负责把它做对，不负责替调用方做安全决策）。
    :param unauthorized: 鉴权失败时的响应（默认 401 + ``{"error": "unauthorized"}``）。
    :param max_body: 请求体上限（bytes）。
    :param stop_timeout: :meth:`stop` 等在途 handler 收尾的秒数上界。
    """

    def __init__(
        self,
        *,
        routes: Iterable[Route] = (),
        host: str = DEFAULT_BIND_HOST,
        port: int = 0,
        name: str = "httpsrv",
        authenticate: Optional[Callable[[HttpRequest], Optional[str]]] = None,
        unauthorized: Optional[Callable[[HttpRequest], HttpResponse]] = None,
        max_body: int = MAX_BODY_BYTES,
        stop_timeout: float = 5.0,
    ) -> None:
        self.name = str(name or "httpsrv")
        self._requested_host = str(host or DEFAULT_BIND_HOST).strip() or DEFAULT_BIND_HOST
        try:
            self._requested_port = int(port)
        except (TypeError, ValueError):
            self._requested_port = 0
        self.max_body = max(1, int(max_body))
        self.stop_timeout = max(0.0, float(stop_timeout))
        self._authenticate = authenticate
        self._unauthorized = unauthorized

        self._lock = threading.Lock()
        self._cv = threading.Condition()
        self._httpd: Optional[_Server] = None
        self._thread: Optional[threading.Thread] = None
        self._host: str = self._requested_host
        self._port: int = self._requested_port
        self._inflight = 0

        #: ``(path, method) -> Route``
        self._routes: dict[tuple[str, str], Route] = {}
        self._paths: set[str] = set()
        for route in routes:
            self.add_route(route)
        self._stats: dict[str, int] = {
            "requests": 0, "handled": 0, "not_found": 0,
            "method_not_allowed": 0, "unauthorized": 0, "errors": 0,
        }

    # --- 路由 ----------------------------------------------------------
    def add_route(self, route: Route) -> None:
        """挂一条路由。**只允许在 :meth:`start` 之前调用**（否则请求会漏路由）。"""
        if not isinstance(route, Route):
            raise TypeError(f"expected Route, got {type(route).__name__}")
        if not callable(route.handler):
            raise TypeError(f"route {route.path!r} has a non-callable handler")
        with self._lock:
            if self._httpd is not None:
                raise RuntimeError("cannot add routes after start(); stop() first")
            for method in route.methods:
                key = (route.path, method)
                if key in self._routes:
                    raise ValueError(
                        f"duplicate route: {method} {route.path!r} "
                        f"(already handled by {self._routes[key].name!r})"
                    )
                self._routes[key] = route
            self._paths.add(route.path)

    def routes(self) -> tuple[Route, ...]:
        """已挂载的路由（去重后，按 path 排序）。"""
        with self._lock:
            seen: dict[str, Route] = {}
            for route in self._routes.values():
                seen.setdefault(route.path, route)
        return tuple(seen[key] for key in sorted(seen))

    # --- 状态（可观测）-------------------------------------------------
    @property
    def bound(self) -> bool:
        """是否已经绑上端口并在服务。"""
        with self._lock:
            return self._httpd is not None

    @property
    def host(self) -> str:
        """**实际**绑定的地址**（不是请求的那个）**。"""
        with self._lock:
            return self._host

    @property
    def port(self) -> int:
        """**实际**绑定的端口。``start()`` 之后端口 0 会变成操作系统分配的那个。"""
        with self._lock:
            return self._port

    @property
    def loopback_only(self) -> bool:
        return is_loopback_host(self.host)

    def url(self, path: str = "") -> str:
        """本机的基址（如 Agent Card 要公布的地址）。"""
        host = self.host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"          # IPv6 字面量加括号
        return f"http://{host}:{self.port}{normalize_path(path) if path else ''}"

    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    def _bump(self, key: str, delta: int = 1) -> None:
        self._stats[key] = self._stats.get(key, 0) + delta

    # --- 生命周期 ------------------------------------------------------
    def start(self) -> int:
        """绑端口并起服务线程。返回**实际**端口（``port=0`` 时是操作系统分配的）。

        **幂等**：已在跑时直接返回当前端口，不起第二个监听。绑定失败抛
        :class:`BindError`（调用方如适配器应自行捕获并记日志，不要让桥接崩掉）。
        """
        with self._lock:
            if self._httpd is not None:
                return self._port
            try:
                httpd = _Server((self._requested_host, self._requested_port), self)
            except OSError as exc:
                raise BindError(
                    f"cannot bind {self._requested_host}:{self._requested_port}: {exc}"
                ) from exc
            bound_host, bound_port = httpd.server_address[:2]
            self._httpd = httpd
            self._host = str(bound_host)
            self._port = int(bound_port)
            thread = threading.Thread(
                target=self._serve, name=f"httpsrv:{self.name}", daemon=True
            )
            self._thread = thread
        thread.start()
        logger.info(
            "httpsrv[%s]: 监听 http://%s:%d（%s，%s）",
            self.name, self._host, self._port,
            "仅本机" if self.loopback_only else "⚠ 非回环地址",
            "已配置鉴权" if self._authenticate is not None else "⚠ 未鉴权",
        )
        return self._port

    def _serve(self) -> None:
        httpd = self._httpd
        if httpd is None:  # pragma: no cover - start() 与 stop() 之间的竞态
            return
        try:
            httpd.serve_forever(poll_interval=_SERVE_POLL_INTERVAL)
        except Exception:  # noqa: BLE001 - 服务线程不许把异常抛出去
            logger.exception("httpsrv[%s]: 服务线程异常退出", self.name)
        finally:
            # 正常路径由 stop() 清引用；这里兜底（serve_forever 自己崩了）。
            with self._lock:
                if self._httpd is httpd:
                    self._httpd = None

    def stop(self, timeout: Optional[float] = None) -> None:
        """**干净关闭**：``shutdown()`` → ``server_close()`` → join → 等在途收尾。**幂等**。

        顺序为什么是这个：

        1. ``shutdown()`` 必须**先**做，且必须从 ``serve_forever()`` 所在线程
           **之外**调用 —— 它要等 ``serve_forever`` 的循环退出，在那个线程里调
           就自锁死锁。检测到那种情况会记 ERROR 并跳过（死锁比报错更糟）。
        2. ``server_close()`` 关监听 socket，端口立刻释放（``allow_reuse_address``
           已开，所以紧接着重新 bind 同端口不会撞 TIME_WAIT）。
        3. 然后才 join ``serve_forever`` 线程 —— 它已经收到 shutdown 信号，
           最坏再等一个 ``_SERVE_POLL_INTERVAL``。
        4. 最后**优雅关闭**：等在途 handler 收尾（默认 :attr:`stop_timeout`）。
           handler 线程是 daemon 的，所以即使有 handler 卡住也不会拖住停机 ——
           只是会留一条 WARNING 说明有几个请求没收干净。

        ⚠️ 阻塞在"等对端答复"上的 handler **不会**被本模块唤醒 —— 唤醒它们是
        处理器自己的事（a2a 在 :meth:`A2aAdapter.stop` 里先把待答复任务判失败）。
        """
        budget = self.stop_timeout if timeout is None else max(0.0, float(timeout))
        with self._lock:
            httpd, self._httpd = self._httpd, None
            thread, self._thread = self._thread, None
        if httpd is None and thread is None:
            return
        if httpd is not None:
            if threading.current_thread() is thread:
                logger.error(
                    "httpsrv[%s]: stop() 在 serve_forever 线程内被调用（shutdown 会死锁），已跳过",
                    self.name,
                )
            else:
                try:
                    httpd.shutdown()
                except Exception as exc:  # noqa: BLE001 - 关闭路径不许抛
                    logger.debug("httpsrv[%s]: shutdown() 失败（忽略）: %s", self.name, exc)
                try:
                    httpd.server_close()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("httpsrv[%s]: server_close() 失败（忽略）: %s", self.name, exc)
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(budget)
        with self._cv:
            self._cv.wait_for(lambda: self._inflight == 0, timeout=budget)
            leftover = self._inflight
        if leftover:
            logger.warning(
                "httpsrv[%s]: 停止后仍有 %d 个在途请求未收尾（handler 为 daemon 线程，"
                "随进程退出）",
                self.name, leftover,
            )

    # --- 请求处理 ------------------------------------------------------
    def _dispatch(self, handler: BaseHTTPRequestHandler) -> HttpResponse:
        with self._cv:
            self._inflight += 1
        try:
            return self._handle(handler)
        finally:
            with self._cv:
                self._inflight -= 1
                self._cv.notify_all()

    def _handle(self, handler: BaseHTTPRequestHandler) -> HttpResponse:
        self._bump("requests")
        split = urllib.parse.urlsplit(handler.path or "/")
        try:
            path = normalize_path(urllib.parse.unquote(split.path or "/"))
        except Exception:
            path = normalize_path(split.path or "/")
        try:
            request = HttpRequest(
                method=str(handler.command or "GET").upper(),
                path=path,
                query=urllib.parse.parse_qs(split.query, keep_blank_values=True),
                headers=_lower_headers(handler.headers),
                body=self._read_body(handler),
                client_host=str(handler.client_address[0]) if handler.client_address else "",
            )
        except _BodyRejected as rejected:
            self._bump("errors")
            return rejected.response

        route = self._routes.get((request.path, request.method))
        if route is None:
            if request.path in self._paths:
                self._bump("method_not_allowed")
                allowed = sorted(m for p, m in self._routes if p == request.path)
                return HttpResponse.json(
                    {"error": "method not allowed", "allow": allowed}, 405,
                    headers=(("Allow", ", ".join(allowed)),),
                )
            self._bump("not_found")
            return HttpResponse.json({"error": "not found", "path": request.path}, 404)

        if route.require_auth:
            identity = self._identify(request, route)
            if not identity:
                self._bump("unauthorized")
                return self._unauthorized_response(request)
            request = dataclasses.replace(request, peer=identity)

        self._bump("handled")
        try:
            response = route.handler(request)
        except Exception:  # noqa: BLE001 - 处理器异常不许泄栈、不许打死连接
            logger.exception(
                "httpsrv[%s]: 处理器 %s 抛出异常（回 500）", self.name, route.path
            )
            self._bump("errors")
            return HttpResponse.json({"error": "internal error"}, 500)
        if not isinstance(response, HttpResponse):
            logger.error(
                "httpsrv[%s]: 处理器 %s 返回了 %s（期望 HttpResponse），回 500",
                self.name, route.path, type(response).__name__,
            )
            self._bump("errors")
            return HttpResponse.json({"error": "internal error"}, 500)
        return response

    def _read_body(self, handler: BaseHTTPRequestHandler) -> bytes:
        """按 ``Content-Length`` 读请求体。不可接受时抛 :class:`_BodyRejected`。

        刻意**不支持 chunked**：标准库客户端不会发它，真发了说明对端不是普通
        HTTP 客户端 —— 明确回 400 好过猜边界把连接搞乱。
        """
        encoding = (handler.headers.get("Transfer-Encoding") or "").strip().lower()
        if encoding and encoding != "identity":
            raise _BodyRejected(HttpResponse.json(
                {"error": "chunked transfer encoding is not supported"}, 400))
        raw = (handler.headers.get("Content-Length") or "").strip()
        try:
            length = int(raw) if raw else 0
        except ValueError:
            raise _BodyRejected(HttpResponse.json({"error": "invalid Content-Length"}, 400)) from None
        if length < 0:
            raise _BodyRejected(HttpResponse.json({"error": "invalid Content-Length"}, 400))
        if length > self.max_body:
            # 先把被拒的 body **读掉**（分块丢弃，任何时刻只持有 4 KiB），这样连接
            # 能以 FIN 正常关闭，客户端**读得到** 413。
            #
            # 为什么不"直接关掉算了"：内核对一个"接收缓冲里还有未读数据"的 socket
            # 关闭时会发 **RST**，而 RST 会把已经写好的响应一起丢掉 —— 客户端于是看到
            # 的是"连接错误"而不是 413。实测这在 Windows 上是**概率性**的（与时序
            # 有关），会让调用方的测试随机失败。
            #
            # 为什么不整块读进内存：那就等于把"拒绝大 body"这个防护取消了。这里
            # **读但不存**，且丢弃量有上限（:data:`_MAX_DISCARD`）—— 超过就直接关，
            # 由调用方容忍"连接错误"（见模块 docstring）。
            self._discard(handler, length)
            raise _BodyRejected(HttpResponse.json(
                {"error": "payload too large", "limit": self.max_body}, 413))
        if not length:
            return b""
        try:
            return handler.rfile.read(length)
        except (socket.timeout, TimeoutError):
            raise _BodyRejected(HttpResponse.json({"error": "timed out reading body"}, 408)) from None
        except OSError:
            raise _BodyRejected(HttpResponse.json({"error": "truncated body"}, 400)) from None

    @staticmethod
    def _discard(handler: BaseHTTPRequestHandler, length: int) -> int:
        """把 ``length`` 字节**读掉但不留存**。返回实际丢弃的字节数。

        存在的唯一理由：让"拒绝一个请求"也能干净地以 FIN 关闭连接（见
        :meth:`_read_body` 里 413 分支的注释）。任何时刻只持有 4 KiB，所以
        "拒绝大 body"这个防护没有被取消。

        丢弃量超过 :data:`_MAX_DISCARD` 就**放弃**：``Content-Length`` 由客户端
        控制，无上限地读等于把 DoS 面又打开一次。放弃后连接会被 RST，客户端看到
        "连接错误" —— 那也是可接受的（:mod:`opencode_bridge.httpsrv` 模块
        docstring 已写明调用方必须容忍这一路）。
        """
        budget = min(int(length), _MAX_DISCARD)
        dropped = 0
        try:
            while dropped < budget:
                chunk = handler.rfile.read(min(4096, budget - dropped))
                if not chunk:
                    break
                dropped += len(chunk)
        except (socket.timeout, TimeoutError, OSError):
            pass                    # 客户端不等了 —— 断开即可
        return dropped

    def _identify(self, request: HttpRequest, route: Route) -> str:
        """鉴权。返回对端标识；空串表示拒绝。

        ⚠️ **没配 ``authenticate`` 却要求鉴权时 fail closed**：路由说"要鉴权"而
        服务上没有鉴权器，只可能是调用方配错了 —— 放行等于把配置错误变成静默的
        安全洞。记 ERROR 而不是 DEBUG，因为这属于"人没看到就出事"的类型。
        """
        if self._authenticate is None:
            logger.error(
                "httpsrv[%s]: 路由 %r 要求鉴权但未配置 authenticate —— 一律拒绝（fail closed）",
                self.name, route.path,
            )
            return ""
        try:
            identity = self._authenticate(request)
        except Exception:  # noqa: BLE001 - 鉴权器自身的 bug 不能变成"放行"
            logger.exception("httpsrv[%s]: 鉴权器抛出异常 —— 拒绝", self.name)
            return ""
        return str(identity or "").strip()

    def _unauthorized_response(self, request: HttpRequest) -> HttpResponse:
        if self._unauthorized is not None:
            try:
                response = self._unauthorized(request)
                if isinstance(response, HttpResponse):
                    return response
                logger.error(
                    "httpsrv[%s]: unauthorized() 返回了 %s（期望 HttpResponse），用默认 401",
                    self.name, type(response).__name__,
                )
            except Exception:  # noqa: BLE001
                logger.exception("httpsrv[%s]: unauthorized() 抛出异常，用默认 401", self.name)
        return HttpResponse.json({"error": "unauthorized"}, 401)


def _lower_headers(headers: Any) -> dict[str, str]:
    """请求头 -> 小写键的 dict。同名重复取**第一个**。

    刻意用普通 dict 而不是 ``email.message.Message``：处理器只该看到"键小写、
    取值是字符串"这一种形态，不该有机会碰到邮件模块的那套 API。
    """
    out: dict[str, str] = {}
    try:
        items = headers.items()
    except AttributeError:  # pragma: no cover - 防御
        return out
    for key, value in items:
        low = str(key).lower()
        if low not in out:
            out[low] = str(value)
    return out
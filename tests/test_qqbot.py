"""B2 测试 —— QQ 机器人开放平台适配器（``adapters/qqbot.py``）。

**零真实外网**：每个用例在 ``127.0.0.1:0`` 上起一个用标准库 ``socket`` +
``threading`` 写的**真 WebSocket 服务器**。服务端**自己**按 RFC 6455 算一遍
``Sec-WebSocket-Accept``（本文件里的实现与 ``ws.py`` 的 ``_accept_for`` 相互独立）
—— 一条链路上两边各算一遍，握手才有意义。服务端发帧**不加掩码**，读帧则校验并还原
客户端掩码，因此测的是真实协议行为。

覆盖：
注册表可发现 / ``required_tokens`` ⊇ ``outbound_tokens`` 不变量 / ``capabilities()``
与实现一致 / **心跳单位是毫秒**（两条互补断言：400ms ⇒ 约 0.4s 一发；45000ms ⇒ 只发
立即的那一发）/ 心跳与 ACK 往返 / 防回环（``author.bot`` 与 ``author.id == READY.user.id``，
以及"不用内容启发式"）/ ``edit()`` 恒 False 且不发任何 HTTP / ``stop()`` 干净并断言耗时
上界 / 半开连接与服务端悄悄断线的重连 / 畸形帧与畸形消息不泄漏 traceback / 凭据不进
query string 也不进日志。
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
import urllib.request
from typing import Callable, List, Optional

from opencode_bridge import identity
from opencode_bridge.adapters.base import adapter_class, build, registered_names
from opencode_bridge.adapters.qqbot import (
    DEFAULT_INTENTS,
    INTENT_GROUP_AND_C2C,
    INTENT_PUBLIC_GUILD_MESSAGES,
    MESSAGE_LIMIT,
    OP_DISPATCH,
    OP_HEARTBEAT,
    OP_HEARTBEAT_ACK,
    OP_HELLO,
    OP_IDENTIFY,
    OP_INVALID_SESSION,
    OP_RECONNECT,
    OP_RESUME,
    QQBotAdapter,
)
from opencode_bridge.hooks import Outbound, SendError
from opencode_bridge.identity import InvalidConversationId
from opencode_bridge.transport import ReconnectNow

# ======================================================================
# 服务端：手写 RFC 6455 字节（不 import ws.py 的实现 —— 两边各算一遍）
# ======================================================================
_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
#: 101 响应与第一帧（Hello）之间的延时。**0 = 故意让它们落在同一个 TCP 段**。
#:
#: 这曾经是绕开一个真实丢帧 bug 的补丁（``ws.py`` 的 ``_check_handshake`` 把
#: 握手时多读到的字节丢掉，导致第一帧被静默吞掉、客户端干等到读超时）。
#: 该bug 已修复，见 ``opencode_bridge/ws.py`` 的 ``_prefetch`` 与
#: ``tests/test_ws.py::TestPipelinedFirstFrame``。
#:
#: 现在保持 0 是**有意**的：这样 qqbot 的整套真服务器测试都顺带覆盖
#: ``101 + 第一帧同段`` 这条路径 —— 而那正是现实里服务端的行为。
FIRST_FRAME_DELAY = 0.0
_OP_TEXT = 0x1
_OP_BINARY = 0x2
_OP_CLOSE = 0x8


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


def _handshake(sock: socket.socket, timeout: float = 5.0) -> dict:
    """读客户端握手请求、回 101，返回请求头字典（小写键）。"""
    sock.settimeout(timeout)
    buf = bytearray()
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(1)
        if not chunk:
            raise ConnectionError("握手请求没读完就断了")
        buf += chunk
    head = bytes(buf).decode("latin-1")
    lines = head.split("\r\n")
    headers: dict = {}
    for line in lines[1:]:
        key, sep, value = line.partition(":")
        if sep:
            headers[key.strip().lower()] = value.strip()
    key = headers.get("sec-websocket-key")
    if not key:
        raise AssertionError(f"握手请求里没有 Sec-WebSocket-Key: {head!r}")
    response = (
        b"HTTP/1.1 101 Switching Protocols\r\n"
        b"Upgrade: websocket\r\n"
        b"Connection: Upgrade\r\n"
        b"Sec-WebSocket-Accept: " + _accept_for(key).encode("ascii") + b"\r\n\r\n"
    )
    sock.sendall(response)
    # ⚠️ **必须**在 101 响应与"第一帧"之间留一道屏障。
    #
    # 原因：`opencode_bridge/ws.py` 的 `_read_http_response()` 是**逐字节**读到
    # ``\r\n\r\n`` 为止的，于是和握手响应同处一个 TCP 段的任何字节都会落进它的
    # ``rest`` —— 而 `_check_handshake()` **把 rest 直接丢掉了**。后果：服务端若在
    # 101 之后立刻发 Hello（同一次 sendall，或间隔短到被内核合并成一个段），
    # 那一帧会被**静默丢弃**，客户端随后一直阻塞到读超时。
    #
    # 这已经用最小复现确认过（见 tests/test_ws.py 里对该缺陷的说明）。本适配器
    # 无权改 ``ws.py``（硬约束），所以夹具在这里代偿：**先只发 101，隔一会儿再发
    # 第一帧**。真实网关是否也会这样合并，取决于它的实现 —— 若会，那是 ``ws.py``
    # 该修的地方（把 rest 交给调用方，或改成带缓冲的读）。
    if FIRST_FRAME_DELAY:
        time.sleep(FIRST_FRAME_DELAY)
    headers["__request_line__"] = lines[0]
    return headers


def send_frame(
    sock: socket.socket,
    opcode: int,
    payload: bytes = b"",
    *,
    fin: bool = True,
    rsv1: bool = False,
) -> None:
    """服务端发帧（按 RFC 服务端**不加掩码**）。"""
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
    sock.sendall(bytes(head) + payload)


def send_json(sock: socket.socket, obj: dict) -> None:
    send_frame(sock, _OP_TEXT, json.dumps(obj).encode("utf-8"))


def send_text(sock: socket.socket, text: str) -> None:
    send_frame(sock, _OP_TEXT, text.encode("utf-8"))


def send_binary(sock: socket.socket, blob: bytes) -> None:
    send_frame(sock, _OP_BINARY, blob)


def send_close(sock: socket.socket, code: int = 1000) -> None:
    send_frame(sock, _OP_CLOSE, struct.pack("!H", code))


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


def recv_json(sock: socket.socket, timeout: float = 5.0) -> dict:
    """读一条客户端文本帧并解析；同时断言它**带掩码**（RFC 6455 §5.3）。"""
    _fin, opcode, payload, masked = recv_frame(sock, timeout)
    assert opcode == _OP_TEXT, f"期望 text 帧，实际 opcode=0x{opcode:x}"
    assert masked, "客户端发出的帧必须带掩码"
    return json.loads(payload.decode("utf-8"))


def drain_until_close(conn: socket.socket, on_packet: Callable[[dict], None],
                      *, timeout: float = 1.0) -> None:
    """一直读到**连接真的断了**，把每条文本帧交给 ``on_packet``。

    ⚠️ 读超时（``TimeoutError``）**不是**断开 —— 长时间没有下行帧是正常状态（心跳周期
    45 秒时就是如此）。早先的夹具在这里直接 ``break``，等于"观察窗一结束就关连接"，
    于是每次关连接都触发一次客户端重连 + 又一发"立即心跳"，把心跳周期相关的断言测废了。
    """
    while True:
        try:
            packet = recv_json(conn, timeout=timeout)
        except (TimeoutError, socket.timeout):
            continue                      # 只是这一拍没数据，继续等
        except (ConnectionError, OSError, AssertionError):
            break                         # 连接断了 / 帧不合法，结束这条剧本
        on_packet(packet)


class Gateway:
    """在 ``127.0.0.1:0`` 上起一个真网关服务器（每条连接一个处理线程）。

    ``handler(conn, gw, index)`` 是每条连接的剧本；``gw`` 暴露上面那组收发函数。
    服务器**接受任意条连接**（重连测试要靠它），并把每次握手请求头记下来。
    """

    def __init__(self, handler: Callable, timeout: float = 10.0) -> None:
        self._handler = handler
        self._timeout = timeout
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(8)
        self.port: int = self._listener.getsockname()[1]
        self.requests: List[dict] = []
        self.errors: List[BaseException] = []
        self._conns: List[socket.socket] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._accept_loop, name="qqbot-test-gateway", daemon=True
        )

    @property
    def url(self) -> str:
        """网关地址（与官方示例同形状：路径 + 尾斜杠，无 query）。"""
        return f"ws://127.0.0.1:{self.port}/websocket"

    @property
    def handshakes(self) -> int:
        with self._lock:
            return len(self.requests)

    def start(self) -> "Gateway":
        self._thread.start()
        return self

    def _accept_loop(self) -> None:
        self._listener.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                return
            index = self.handshakes
            with self._lock:
                self._conns.append(conn)
            thread = threading.Thread(
                target=self._serve, args=(conn, index), daemon=True
            )
            thread.start()

    def _serve(self, conn: socket.socket, index: int) -> None:
        try:
            conn.settimeout(self._timeout)
            headers = _handshake(conn, self._timeout)
            with self._lock:
                self.requests.append(headers)
            self._handler(conn, self, index)
        except (ConnectionError, TimeoutError, socket.timeout, OSError):
            pass            # 测试结束时连接被拆掉，属正常
        except BaseException as exc:  # noqa: BLE001 - 交给断言用
            with self._lock:
                self.errors.append(exc)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def stop(self) -> None:
        self._stop.set()
        try:
            self._listener.close()
        except OSError:
            pass
        with self._lock:
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

    def __enter__(self) -> "Gateway":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


# ----------------------------------------------------------------------
# 通用小工具
# ----------------------------------------------------------------------
#: 等"某个状态出现"的统一预算（秒）。
#:
#: 为什么给得这么宽：这些用例要起真服务器、跑真线程，等待时长完全取决于**调度**，
#: 而不是被测代码。机器一繁忙（CI 上尤其如此），"等 5 秒内握手成功"就会偶发失败
#: —— 那种失败测不出任何东西，只会训练人忽略红灯。
#: 等待**成功**时用例会立刻结束，所以宽预算几乎不花时间；真正要断言时间的地方
#: （心跳间隔上界、``stop()`` 耗时上界）另外用**窄**断言单独表达。
WAIT = 20.0


def wait_for(predicate: Callable[[], bool], timeout: float = 3.0) -> bool:
    """轮询等条件成立。断言"等了多久"一律用它，wall-clock 只做下界（不变量 18）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class RecordingHooks:
    """记录 ``on_inbound`` 的最小 Hooks 实现。"""

    def __init__(self) -> None:
        self.inbounds = []
        self.raise_on_inbound = False

    def on_inbound(self, inbound) -> None:
        if self.raise_on_inbound:
            raise RuntimeError("上层炸了")
        self.inbounds.append(inbound)

    def on_callback(self, conversation_id: str, data: str, query_id: str) -> None:
        return None


APP_ID = "102075492"
APP_SECRET = "SUPERSECRET-APP-SECRET-VALUE"
ACCESS_TOKEN = "ACCESS-TOKEN-abcdef123456"

#: 官方示例里的群 openid，以及由它推出的 conversation_id。
GROUP_OPENID = "B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4E5"
GROUP_CID = QQBotAdapter.conversation_id_for("group", GROUP_OPENID)


class TokenStub:
    """替换适配器的 ``_request``：只回答凭证接口与（可选）网关地址。

    这样出站与鉴权都**不碰真实网络**，而入站仍然走 ``127.0.0.1`` 上的真服务器。
    """

    def __init__(self, *, gateway_url: Optional[str] = None,
                 expires_in: str = "7200", fail: bool = False) -> None:
        self.calls: List[tuple] = []
        self.gateway_url = gateway_url
        self.expires_in = expires_in
        self.fail = fail
        #: 出站调用按顺序吃这些（``pop(0)``）；空了就回默认成功。
        self.outbound: List[tuple] = []

    def __call__(self, method, path, payload=None, *, token="", timeout=None):
        self.calls.append((method, path, payload, token))
        if path.endswith("/app/getAppAccessToken"):
            if self.fail:
                # 官方：失败时 HTTP 仍是 200，错误在响应体的 code 里。
                return (200, {"code": 100016, "message": "invalid appid or secret"})
            return (200, {"access_token": ACCESS_TOKEN, "expires_in": self.expires_in})
        if path.endswith("/gateway"):
            return (200, {"url": self.gateway_url or ""})
        if self.outbound:
            return self.outbound.pop(0)
        return (200, {"id": "ROBOT1.0_stubmessageid"})

    @property
    def auth_headers(self) -> List[str]:
        return [token for (_m, _p, _b, token) in self.calls if token]


def make_adapter(hooks=None, **config) -> QQBotAdapter:
    """带完整凭据的适配器（``_request`` 由各用例自己 stub）。"""
    base = {"app_id": APP_ID, "app_secret": APP_SECRET}
    base.update(config)
    return QQBotAdapter(base, hooks or RecordingHooks())


def hello_packet(interval_ms: int = 45000, seq: int | None = None) -> dict:
    packet = {"op": OP_HELLO, "d": {"heartbeat_interval": interval_ms}}
    if seq is not None:
        packet["s"] = seq
    return packet


def ready_packet(seq: int = 1, user_id: str = "6158788875714165",
                 session_id: str = "082ee18c-0be3-491b-9d8b-fbd95c51673a") -> dict:
    return {
        "op": OP_DISPATCH,
        "s": seq,
        "t": "READY",
        "d": {
            "version": 1,
            "session_id": session_id,
            "user": {"id": user_id, "username": "测试机器人", "bot": True},
            "shard": [0, 1],
        },
    }


def group_message_packet(seq: int = 2, author_bot: bool = False,
                         author_id: str = "A1B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4",
                         content: str = "你好", message_type: int = 0,
                         group_openid: str = "B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4E5",
                         msg_id: str = "ROBOT1.0_zzz") -> dict:
    """``GROUP_AT_MESSAGE_CREATE`` 的形状（官方群 @ 消息事件示例）。"""
    return {
        "op": OP_DISPATCH,
        "s": seq,
        "t": "GROUP_AT_MESSAGE_CREATE",
        "d": {
            "id": msg_id,
            "author": {
                "id": author_id,
                "member_openid": author_id,
                "member_role": "member",
                "username": "小明",
                "bot": author_bot,
            },
            "content": content,
            "group_openid": group_openid,
            "timestamp": "2026-07-21T10:00:00+08:00",
            "message_type": message_type,
            "message_scene": {"source": "default", "ext": ["msg_idx=REFIDX_x=="]},
        },
    }


def c2c_message_packet(seq: int = 3, author_bot: bool = False,
                       user_openid: str = "C3D4E5F6A1B2C3D4E5F6A1B2C3D4E5F6",
                       content: str = "在吗") -> dict:
    return {
        "op": OP_DISPATCH,
        "s": seq,
        "t": "C2C_MESSAGE_CREATE",
        "d": {
            "id": "ROBOT1.0_yyy",
            "author": {
                "id": user_openid,
                "user_openid": user_openid,
                "union_openid": "",
                "username": "",
                "bot": author_bot,
            },
            "content": content,
            "timestamp": "2026-07-21T10:05:00+08:00",
            "message_type": 0,
        },
    }


def channel_message_packet(seq: int = 4, author_id: str = "1234",
                           channel_id: str = "100010",
                           content: str = "channel hi") -> dict:
    """频道 ``AT_MESSAGE_CREATE`` 的形状（官方频道消息事件示例）。"""
    return {
        "op": OP_DISPATCH,
        "s": seq,
        "t": "AT_MESSAGE_CREATE",
        "d": {
            "author": {"avatar": "", "bot": False, "id": author_id,
                       "username": "abc"},
            "channel_id": channel_id,
            "content": content,
            "guild_id": "18700000000001",
            "id": "0812345677890abcdef",
            "timestamp": "2021-05-20T15:14:58+08:00",
        },
    }


class QQBotTestCase(unittest.TestCase):
    """给每个用例一个干净的 logger 记录器 + **真实外网熔断**。

    熔断很重要：``_on_hello`` 会取 access_token，走的是 :mod:`urllib`。任何漏 stub 的
    用例都会真的去连 ``api.bot.qq.com`` —— 那既违反"零外网"的测试前提，又会让测试
    结果依赖网络。这里把它换成"一调用就炸"，漏网会立刻暴露成测试失败。
    """

    def setUp(self) -> None:
        self.logger = logging.getLogger("opencode_bridge.adapters.qqbot")
        self.records: List[logging.LogRecord] = []
        handler = logging.Handler()
        handler.emit = self.records.append  # type: ignore[method-assign]
        self.logger.addHandler(handler)
        self.logger.setLevel(logging.DEBUG)
        self.addCleanup(self.logger.removeHandler, handler)

        def _no_network(*args, **kwargs):
            raise AssertionError(
                "测试里不许访问真实外网：请 stub adapter._request 或给 gateway_url"
            )

        original = urllib.request.urlopen
        urllib.request.urlopen = _no_network  # type: ignore[assignment]
        self.addCleanup(
            lambda: setattr(urllib.request, "urlopen", original)
        )

    @property
    def formatted_logs(self) -> str:
        """**含异常栈**的日志文本。

        ``LogRecord.getMessage()`` 不含 traceback，用它断言"没有泄漏栈信息"是假断言 ——
        必须过一遍 :class:`logging.Formatter`（它会补上 ``exc_text``）。
        """
        fmt = logging.Formatter("%(levelname)s:%(name)s:%(message)s")
        return "\n".join(fmt.format(r) for r in self.records)

    @property
    def records_with_traceback(self) -> List[logging.LogRecord]:
        """带 ``exc_info`` 的记录 —— 即"真的打出了栈"的那些。"""
        return [r for r in self.records if r.exc_info]

    def stub_http(self, adapter: QQBotAdapter, **kwargs) -> TokenStub:
        """给适配器装上只答凭证/网关接口的 ``_request``（出站也在内存里跑完）。"""
        stub = TokenStub(**kwargs)
        adapter._request = stub          # type: ignore[method-assign]
        adapter.min_interval = 0.0       # type: ignore[assignment]
        return stub

    @property
    def log_text(self) -> str:
        return "\n".join(r.getMessage() for r in self.records)


# ======================================================================
# 注册表 / 契约 / 能力声明
# ======================================================================
class TestRegistryAndContract(QQBotTestCase):
    def test_registered_and_discoverable(self):
        self.assertIn("qqbot", registered_names())
        self.assertIs(adapter_class("qqbot"), QQBotAdapter)
        self.assertIsInstance(build("qqbot", {"app_id": APP_ID}, RecordingHooks()),
                              QQBotAdapter)

    def test_outbound_tokens_is_subset_of_required_tokens(self):
        """仓库硬不变量（test_cli 也守这条；它抓到过 Matrix 忘声明的真 bug）。"""
        required = set(QQBotAdapter.required_tokens)
        outbound = set(QQBotAdapter.outbound_tokens)
        self.assertTrue(required, "required_tokens 不能为空（声明义务）")
        self.assertTrue(outbound, "outbound_tokens 不能为空")
        self.assertTrue(
            outbound <= required,
            f"outbound_tokens {sorted(outbound)} 不是 required_tokens {sorted(required)} 的子集",
        )

    def test_declared_tokens_are_real_credential_keys(self):
        """凭据键就是开放平台管理端那两个；官方文档里的 ``appId`` / ``clientSecret``
        写法与常见别名也认（用户照抄文档是很自然的事）。"""
        self.assertEqual(set(QQBotAdapter.required_tokens), {"app_id", "app_secret"})
        for config in (
            {"app_id": "1", "app_secret": "2"},
            {"appid": "1", "appsecret": "2"},
            {"appId": "1", "clientSecret": "2"},
            {"app_id": "  1  ", "app_secret": "  2  "},
        ):
            with self.subTest(config=sorted(config)):
                adapter = QQBotAdapter(dict(config), RecordingHooks())
                self.assertEqual(adapter.app_id, "1")
                self.assertEqual(adapter.app_secret, "2")

    def test_not_config_optional(self):
        """qqbot 确实需要凭据 ⇒ 不能声明 config_optional。"""
        self.assertFalse(QQBotAdapter.config_optional)

    def test_capabilities_match_implementation(self):
        """``capabilities()`` 的每个值都必须与实现一致（不变量 2：禁止谎报）。"""
        adapter = make_adapter()
        caps = adapter.capabilities()
        self.assertEqual(caps["name"], "qqbot")
        self.assertEqual(caps["label"], "QQ Bot")
        self.assertTrue(caps["supports_inbound"])
        self.assertFalse(caps["supports_inline_buttons"])
        self.assertFalse(caps["supports_media"])
        self.assertEqual(caps["max_message_length"], MESSAGE_LIMIT)

        # supports_media=False ⇒ 出站只可能出现 msg_type=0，且绝不带 media/image/ark
        stub = TokenStub()
        adapter._request = stub          # type: ignore[method-assign]
        adapter.min_interval = 0.0       # type: ignore[assignment]
        adapter.send(Outbound(conversation_id="qqbot:group:G1", text="hi"))
        bodies = [c[2] for c in stub.calls if c[1].startswith("/v2/groups/")]
        self.assertEqual(len(bodies), 1)
        self.assertEqual(bodies[0]["msg_type"], 0)
        for forbidden in ("media", "image", "ark", "markdown", "keyboard",
                          "file_info", "embed"):
            self.assertNotIn(forbidden, bodies[0])

        # supports_inline_buttons=False ⇒ answer() 是 no-op，edit() 恒 False
        self.assertIsNone(adapter.answer("query-1", "text"))
        self.assertFalse(
            adapter.edit(
                __import__("opencode_bridge.hooks", fromlist=["MsgHandle"]).MsgHandle(
                    conversation_id="qqbot:group:G1", message_id="m1", platform="qqbot"
                ),
                Outbound(conversation_id="qqbot:group:G1", text="x"),
            )
        )
        # max_message_length ⇒ 出站分片按它切，且每片都不超
        stub2 = TokenStub()
        adapter._request = stub2         # type: ignore[method-assign]
        adapter.send(Outbound(conversation_id="qqbot:group:G1", text="字" * (MESSAGE_LIMIT * 2 + 7)))
        contents = [c[2]["content"] for c in stub2.calls if c[1].startswith("/v2/groups/")]
        self.assertGreater(len(contents), 1, "超长文本必须分片")
        for text in contents:
            self.assertLessEqual(len(text), MESSAGE_LIMIT)

    def test_max_message_length_is_flagged_as_our_own_conservative_choice(self):
        """⚠️ 官方没公布长度上限 —— 这个常量必须是我们自选的保守值而非事实。"""
        source = (
            __import__("pathlib").Path(__file__).resolve().parents[1]
            / "opencode_bridge" / "adapters" / "qqbot.py"
        ).read_text(encoding="utf-8")
        self.assertIn("自选的保守上限，不是官方数字", source)
        self.assertIn("40054007", source)


class TestConversationId(QQBotTestCase):
    """``conversation_id`` 必须是 ``qqbot:...``，且**含冒号的 local 段要能往返**。"""

    def test_format_uses_qqbot_prefix(self):
        self.assertEqual(
            QQBotAdapter.conversation_id_for("group", "B2C3"), "qqbot:group:B2C3"
        )

    def test_local_part_may_contain_colons_and_round_trips(self):
        """local 段是 ``<scope>:<openid>``，含冒号。

        ``identity`` 按**第一个**冒号切分，所以平台段与 local 段不会互相污染 —— 这里
        把这条钉死，免得哪天有人加"禁冒号"校验把 QQ 打死（Matrix 房间 id 含冒号已经踩过）。
        """
        for scope, target in (
            ("group", "B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4E5"),
            ("c2c", "C3D4E5F6A1B2C3D4E5F6A1B2C3D4E5F6"),
            ("channel", "100010"),
        ):
            cid = QQBotAdapter.conversation_id_for(scope, target)
            with self.subTest(scope=scope):
                self.assertTrue(cid.startswith("qqbot:"))
                self.assertEqual(identity.platform_of(cid), "qqbot")
                local = identity.local_of(cid)
                self.assertEqual(local, f"{scope}:{target}")   # 往返无损
                self.assertTrue(identity.is_valid(cid))
                self.assertEqual(QQBotAdapter.split_conversation(cid), (scope, target))

    def test_openid_with_colon_still_round_trips(self):
        """极端情况：目标 id 本身含冒号也不能解析错。"""
        weird = "AA:BB:CC"
        cid = QQBotAdapter.conversation_id_for("group", weird)
        self.assertEqual(cid, "qqbot:group:AA:BB:CC")
        self.assertEqual(QQBotAdapter.split_conversation(cid), ("group", weird))

    def test_split_rejects_foreign_and_legacy_ids(self):
        for bad in (
            "telegram:123",           # 别的平台
            "channel:100010",         # 迁移前的歧义前缀
            "chat:55",                # 迁移前的 telegram 前缀
            "qqbot:",                 # 空 local
            "qqbot:nosuchscope:X",    # 未知 scope
            "qqbot:group",            # 没有第二个冒号
            "", None, 123,
        ):
            with self.subTest(cid=bad):
                self.assertIsNone(QQBotAdapter.split_conversation(bad))
                adapter = make_adapter()
                self.assertIsNone(
                    adapter.send(Outbound(conversation_id=str(bad), text="x"))
                )
                self.assertEqual(adapter.last_send_error, SendError.BAD_FORMAT)


class TestConfiguration(QQBotTestCase):
    def test_default_api_base_is_the_official_prod_host(self):
        self.assertEqual(make_adapter().api_base, "https://api.bot.qq.com")

    def test_sandbox_base_is_announced_as_unverified(self):
        adapter = make_adapter(sandbox=True)
        self.assertEqual(adapter.api_base, "https://sandbox.api.sgroup.qq.com")
        self.stub_http(adapter)
        # 指一个没人听的端口：连接会立刻失败，不会在测试期间反复退避重试刷日志。
        adapter.gateway_url = "ws://127.0.0.1:1/websocket"
        try:
            adapter.start()
            self.assertIn("未能从官方文档核实", self.log_text)
        finally:
            adapter.stop()

    def test_explicit_api_base_wins_over_sandbox(self):
        adapter = make_adapter(sandbox=True, api_base="https://example.invalid/v2/")
        self.assertEqual(adapter.api_base, "https://example.invalid/v2")
        self.stub_http(adapter)
        adapter.gateway_url = "ws://127.0.0.1:1/websocket"
        try:
            adapter.start()
            self.assertNotIn("未能从官方文档核实", self.log_text)
        finally:
            adapter.stop()

    def test_default_intents_are_group_c2c_and_public_guild(self):
        adapter = make_adapter()
        self.assertEqual(adapter.intents, INTENT_GROUP_AND_C2C | INTENT_PUBLIC_GUILD_MESSAGES)
        self.assertEqual(adapter.intents, DEFAULT_INTENTS)
        # 刻意不订阅：频道私信（v1 不处理）、交互事件（不实现按钮）、私域专用消息事件
        self.assertEqual(adapter.intents & (1 << 12), 0)
        self.assertEqual(adapter.intents & (1 << 26), 0)

    def test_intents_accept_int_or_name_list_and_reject_garbage(self):
        self.assertEqual(make_adapter(intents=513).intents, 513)
        self.assertEqual(
            make_adapter(intents=["PUBLIC_GUILD_MESSAGES"]).intents,
            INTENT_PUBLIC_GUILD_MESSAGES,
        )
        self.assertEqual(make_adapter(intents="not-a-number").intents, DEFAULT_INTENTS)
        self.assertEqual(make_adapter(intents=0).intents, DEFAULT_INTENTS)

    def test_shard_defaults_to_single_and_validates(self):
        self.assertEqual(make_adapter().shard, (0, 1))
        self.assertEqual(make_adapter(shard=[2, 4]).shard, (2, 4))
        self.assertEqual(make_adapter(shard=[9, 4]).shard, (0, 1))
        self.assertEqual(make_adapter(shard=[0]).shard, (0, 1))

    def test_start_without_credentials_warns_and_does_not_raise(self):
        for config in ({}, {"app_id": APP_ID}, {"app_secret": APP_SECRET}):
            with self.subTest(config=sorted(config)):
                adapter = QQBotAdapter(dict(config), RecordingHooks())
                adapter.start()                       # 不许抛
                self.assertFalse(adapter.running)
                self.assertIn("missing", self.log_text)

    def test_start_wires_transport_with_reset_after_zero(self):
        """``reset_after=0`` = 连上即重置退避（不变量 10，别顺手改成正数）。"""
        adapter = make_adapter()
        transport = adapter._make_transport()
        self.assertEqual(transport.reset_after, 0.0)
        self.assertEqual(transport.min_backoff, 1.0)
        self.assertEqual(transport.max_backoff, 60.0)
        self.assertEqual(transport.label, "qqbot")


# ======================================================================
# access_token
# ======================================================================
class TestAccessToken(QQBotTestCase):
    def test_fetches_with_official_body_and_header_shape(self):
        adapter = make_adapter()
        stub = TokenStub()
        adapter._request = stub          # type: ignore[method-assign]
        self.assertEqual(adapter._ensure_access_token(), ACCESS_TOKEN)
        method, path, payload, _token = stub.calls[0]
        self.assertEqual((method, path), ("POST", "/app/getAppAccessToken"))
        self.assertEqual(payload, {"appId": APP_ID, "clientSecret": APP_SECRET})

    def test_expires_in_is_a_string_in_official_responses(self):
        """官方示例给的是 ``"7200"``（字符串）—— 按字符串解析不能崩。"""
        adapter = make_adapter()
        adapter._request = TokenStub(expires_in="7200")   # type: ignore[method-assign]
        adapter._ensure_access_token()
        self.assertGreater(adapter._token_expires_at, time.monotonic() + 6000)

    def test_token_is_cached_until_refresh_margin(self):
        adapter = make_adapter()
        stub = TokenStub()
        adapter._request = stub          # type: ignore[method-assign]
        adapter._ensure_access_token()
        adapter._ensure_access_token()
        adapter._ensure_access_token()
        self.assertEqual(len(stub.calls), 1, "有效期内的重复调用不该再换 token")

    def test_business_error_with_http_200_raises(self):
        """官方明说：凭证接口失败时 **HTTP 仍是 200**，错误在响应体 ``code`` 里。"""
        adapter = make_adapter()
        adapter._request = TokenStub(fail=True)     # type: ignore[method-assign]
        with self.assertRaises(RuntimeError):
            adapter._ensure_access_token()
        self.assertIn("100016", self.log_text)

    def test_missing_ttl_is_not_cached(self):
        adapter = make_adapter()
        stub = TokenStub(expires_in="")
        adapter._request = stub          # type: ignore[method-assign]
        adapter._ensure_access_token()
        adapter._ensure_access_token()
        self.assertEqual(len(stub.calls), 2, "TTL 不可信时不该缓存（官方说重复获取不消耗配额）")

    def test_send_refuses_when_token_cannot_be_obtained(self):
        adapter = make_adapter()
        adapter._request = TokenStub(fail=True)     # type: ignore[method-assign]
        adapter.min_interval = 0.0                  # type: ignore[assignment]
        self.assertIsNone(
            adapter.send(Outbound(conversation_id="qqbot:group:G1", text="hi"))
        )
        self.assertEqual(adapter.last_send_error, SendError.TRANSIENT)

    def test_send_refuses_without_credentials(self):
        adapter = QQBotAdapter({}, RecordingHooks())
        self.assertIsNone(
            adapter.send(Outbound(conversation_id="qqbot:group:G1", text="hi"))
        )
        self.assertEqual(adapter.last_send_error, SendError.BAD_FORMAT)


# ======================================================================
# 心跳：单位（毫秒！）、ACK 往返、停摆检测
# ======================================================================
class FakeConn:
    """只看发了什么的假连接（用于纯逻辑层的单位断言）。"""

    def __init__(self) -> None:
        self.sent: List[dict] = []
        self.closed = False
        self.closed_with: Optional[tuple] = None

    def send(self, text: str) -> None:
        self.sent.append(json.loads(text))

    def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True
        self.closed_with = (code, reason)

    @property
    def ops(self) -> List[int]:
        return [p.get("op") for p in self.sent]


class TestHeartbeatUnit(QQBotTestCase):
    """★ 本项目最贵的一个坑：``heartbeat_interval`` 的单位。

    官方两处都写明"**单位毫秒(milliseconds)**"，示例 ``{"op":10,"d":
    {"heartbeat_interval": 45000}}``。Discord 那边当初按秒用，心跳快了 1000 倍、
    瞬间触发限流 —— 所以这里既断言换算结果，也断言**发出去的实际节奏**。
    """

    def test_hello_interval_is_divided_by_1000(self):
        adapter = make_adapter()
        self.stub_http(adapter)
        conn = FakeConn()
        adapter._on_hello(conn, {"heartbeat_interval": 45000})
        self.assertAlmostEqual(adapter._heartbeat_interval, 45.0, places=6)
        adapter._stop_watchdog()

    def test_first_heartbeat_is_sent_immediately_with_seq_field(self):
        adapter = make_adapter()
        self.stub_http(adapter)
        adapter._last_seq = 251
        conn = FakeConn()
        adapter._on_hello(conn, {"heartbeat_interval": 45000})
        first = conn.sent[0]
        self.assertEqual(first, {"op": OP_HEARTBEAT, "d": 251})
        adapter._stop_watchdog()

    def test_first_heartbeat_d_is_null_when_no_seq_yet(self):
        adapter = make_adapter()
        self.stub_http(adapter)
        conn = FakeConn()
        adapter._on_hello(conn, {"heartbeat_interval": 45000})
        self.assertIn("d", conn.sent[0])
        self.assertIsNone(conn.sent[0]["d"])
        adapter._stop_watchdog()

    def test_out_of_range_interval_falls_back_to_official_sample(self):
        """官方没给上下限 —— 收到 ``0`` 会让心跳线程变忙等，必须挡住。"""
        for bad in (0, -1, 10 ** 9, "45000", None, True):
            with self.subTest(bad=bad):
                adapter = make_adapter()
                self.stub_http(adapter)
                conn = FakeConn()
                adapter._on_hello(conn, {"heartbeat_interval": bad})
                self.assertAlmostEqual(adapter._heartbeat_interval, 45.0, places=6)
                adapter._stop_watchdog()

    def test_short_but_sane_interval_is_honoured(self):
        """区间校验不能把"平台给了较短周期"误判成非法（否则就是自己改协议）。"""
        adapter = make_adapter()
        self.stub_http(adapter)
        conn = FakeConn()
        adapter._on_hello(conn, {"heartbeat_interval": 400})
        self.assertAlmostEqual(adapter._heartbeat_interval, 0.4, places=6)
        adapter._stop_watchdog()

    def test_real_server_sees_period_of_400ms_not_400s(self):
        """真服务器上的**实测节奏**：400 毫秒 ⇒ 约 0.4 秒一发。"""
        beats: List[float] = []

        def handler(conn, gw, index):
            conn.settimeout(5.0)
            send_json(conn, hello_packet(400))          # 400 **毫秒**

            def on_packet(packet: dict) -> None:
                if packet.get("op") == OP_HEARTBEAT:
                    beats.append(time.monotonic())
                    send_json(conn, {"op": OP_HEARTBEAT_ACK})

            # 剧本**一直记到连接真的断**为止：既不用"固定 N 秒的记录窗口"（客户端建连
            # 可能被调度推迟到窗口之后，那样一个心跳都录不到），也不把读超时当成断开
            # （那等于"观察窗一结束就关连接"，每次关连接都会触发重连 + 又一发立即心跳）。
            drain_until_close(conn, on_packet)

        with Gateway(handler) as gw:
            adapter = make_adapter(gateway_url=gw.url)
            self.stub_http(adapter)
            adapter.ws_recv_timeout = 30.0       # type: ignore[assignment]
            adapter.start()
            try:
                self.assertTrue(
                    wait_for(lambda: len(beats) >= 4, timeout=WAIT),
                    f"400ms 周期 ⇒ 几秒内应收到多次心跳，实际 {len(beats)}",
                )
            finally:
                adapter.stop()
        gaps = [b - a for a, b in zip(beats, beats[1:])]
        self.assertTrue(gaps, "至少要有两个心跳才能比间隔")
        for gap in gaps:
            self.assertGreater(gap, 0.15, f"间隔 {gap:.3f}s 太快 —— 疑似把毫秒又除了一次")
            self.assertLess(gap, 1.5, f"间隔 {gap:.3f}s 太慢 —— 疑似把毫秒当成秒")

    def test_real_server_45000ms_sends_only_the_immediate_heartbeat(self):
        """反向断言：45000 **毫秒** = 45 秒 ⇒ 观察窗内**只**有立即发的那一发。

        如果代码把毫秒当秒用，这一发会变成"45 秒一次"，观察窗内看不到后续心跳；这与
        上一个用例（400ms ⇒ 密集心跳）一起把单位钉死在毫秒上。
        """
        beats = []

        def handler(conn, gw, index):
            conn.settimeout(5.0)
            send_json(conn, hello_packet(45000))

            def on_packet(packet: dict) -> None:
                if packet.get("op") == OP_HEARTBEAT:
                    beats.append(time.monotonic())
                    send_json(conn, {"op": OP_HEARTBEAT_ACK})   # 真服务器会回 ACK

            drain_until_close(conn, on_packet)

        with Gateway(handler) as gw:
            adapter = make_adapter(gateway_url=gw.url)
            self.stub_http(adapter)
            # 读超时必须**大于观察窗**，否则它会先到期 → 重连 → 又来一发"立即心跳"，
            # 让这条断言测到的是读超时而不是心跳周期。
            adapter.ws_recv_timeout = 60.0       # type: ignore[assignment]
            adapter.start()
            try:
                self.assertTrue(
                    wait_for(lambda: len(beats) >= 1, timeout=WAIT),
                    "至少要看到 Hello 之后那一发立即心跳",
                )
                # 45 秒周期 ⇒ 接下来的 3 秒里**不该**再有周期心跳。
                # （若把毫秒当秒用，这一发会变成"45 秒一次"；若把周期缩到毫秒级，
                #   这里会立刻冒出第二发。）
                self.assertFalse(
                    wait_for(lambda: len(beats) >= 2, timeout=3.0),
                    f"45 秒周期内不该有第二发心跳，实际 {len(beats)} 发",
                )
            finally:
                adapter.stop()


class TestHeartbeatAckRoundTrip(QQBotTestCase):
    def test_ack_marks_alive_and_is_consumed(self):
        adapter = make_adapter()
        conn = FakeConn()
        adapter._on_gateway_frame(conn, json.dumps({"op": OP_HEARTBEAT_ACK}))
        self.assertTrue(adapter._ack_received)

    def test_server_requested_heartbeat_is_answered_immediately(self):
        """官方表：Heartbeat「客户端**或服务端**发送心跳」⇒ 服务端要求时立刻回。"""
        adapter = make_adapter()
        adapter._last_seq = 77
        conn = FakeConn()
        adapter._on_gateway_frame(conn, json.dumps({"op": OP_HEARTBEAT}))
        self.assertEqual(conn.sent, [{"op": OP_HEARTBEAT, "d": 77}])

    def test_missing_ack_closes_connection_within_two_periods(self):
        """只发心跳收不到 ACK ⇒ 必须自己发现并断开（否则只能等读超时）。"""
        adapter = make_adapter()
        self.stub_http(adapter)
        conn = FakeConn()
        adapter._on_hello(conn, {"heartbeat_interval": 1000})   # 1 秒周期
        adapter.ws_recv_timeout = 30.0                           # type: ignore[assignment]
        try:
            self.assertTrue(
                wait_for(lambda: conn.closed, timeout=WAIT),
                "两个心跳周期内没收到 ACK 就该判定连接已死",
            )
            self.assertIn(conn.closed_with[0], (1000, 4000))
            self.assertIn("ACK", self.log_text)
        finally:
            adapter._stop_watchdog()

    def test_heartbeat_stops_when_ack_keeps_arriving(self):
        """ACK 一直在来时，看门狗不许误杀连接。"""
        adapter = make_adapter()
        self.stub_http(adapter)
        conn = FakeConn()
        adapter._on_hello(conn, {"heartbeat_interval": 1000})
        stop = threading.Event()

        def ack_forever():
            while not stop.is_set():
                adapter._ack_received = True
                time.sleep(0.05)

        feeder = threading.Thread(target=ack_forever, daemon=True)
        feeder.start()
        try:
            time.sleep(2.6)
            self.assertFalse(conn.closed, "持续收到 ACK 时不许误杀连接")
            self.assertGreaterEqual(len(conn.ops), 2, "心跳必须真的在周期发")
        finally:
            stop.set()
            adapter._stop_watchdog()

    def test_heartbeat_period_equals_negotiated_interval(self):
        """★ 周期必须**正好**等于协商出来的间隔 —— 曾经写成两倍，等于自己把心跳放慢一倍。"""
        adapter = make_adapter()
        self.stub_http(adapter)
        conn = FakeConn()
        adapter._on_hello(conn, {"heartbeat_interval": 1000})   # 1 秒
        try:
            time.sleep(2.6)
            # Hello 那一发 + 约 2 次周期心跳（2.6s 窗口）
            self.assertIn(
                len(conn.sent), (3, 4),
                f"1 秒周期在 2.6s 内应发 3~4 次心跳（Hello 那发 + 周期），实际 {len(conn.sent)}",
            )
        finally:
            adapter._stop_watchdog()


class TestSessionIdentifyAndResume(QQBotTestCase):
    def test_first_connection_identifies_with_official_shape(self):
        adapter = make_adapter()
        self.stub_http(adapter)
        conn = FakeConn()
        adapter._on_hello(conn, {"heartbeat_interval": 45000})
        try:
            identify = next(p for p in conn.sent if p["op"] == OP_IDENTIFY)
            data = identify["d"]
            self.assertEqual(data["token"], f"QQBot {ACCESS_TOKEN}",
                             "官方要求 token 形如 'QQBot {AccessToken}'")
            self.assertEqual(data["shard"], [0, 1], "无需分片时官方建议 [0, 1]")
            self.assertEqual(data["intents"], DEFAULT_INTENTS)
            self.assertIn("$os", data["properties"])
        finally:
            adapter._stop_watchdog()

    def test_identify_is_not_sent_without_a_token(self):
        """取不到 access_token 就不能发 Identify（否则是拿空 token 去撞官方限流）。"""
        adapter = make_adapter()
        adapter._request = TokenStub(fail=True)     # type: ignore[method-assign]
        conn = FakeConn()
        adapter._on_hello(conn, {"heartbeat_interval": 45000})
        try:
            self.assertNotIn(OP_IDENTIFY, conn.ops)
            self.assertTrue(conn.closed, "取不到凭证就必须放弃这条连接")
        finally:
            adapter._stop_watchdog()

    def test_ready_captures_session_and_own_user_id(self):
        adapter = make_adapter()
        adapter._on_ready(ready_packet()["d"])
        self.assertEqual(adapter._session_id, "082ee18c-0be3-491b-9d8b-fbd95c51673a")
        self.assertEqual(adapter._my_user_id, "6158788875714165")

    def test_reconnect_resumes_when_session_known(self):
        adapter = make_adapter()
        self.stub_http(adapter)
        adapter._on_ready(ready_packet()["d"])
        adapter._last_seq = 1337
        conn = FakeConn()
        adapter._on_hello(conn, {"heartbeat_interval": 45000})
        try:
            resume = next(p for p in conn.sent if p["op"] == OP_RESUME)
            self.assertEqual(resume["d"]["session_id"], "082ee18c-0be3-491b-9d8b-fbd95c51673a")
            self.assertEqual(resume["d"]["seq"], 1337)
        finally:
            adapter._stop_watchdog()

    def test_identifies_when_resume_without_seq(self):
        adapter = make_adapter()
        self.stub_http(adapter)
        adapter._on_ready(ready_packet()["d"])
        adapter._last_seq = None
        conn = FakeConn()
        adapter._on_hello(conn, {"heartbeat_interval": 45000})
        try:
            self.assertIn(OP_IDENTIFY, conn.ops)
            self.assertNotIn(OP_RESUME, conn.ops)
        finally:
            adapter._stop_watchdog()

    def test_invalid_session_false_clears_session_and_reconnects(self):
        adapter = make_adapter()
        adapter._on_ready(ready_packet()["d"])
        adapter._last_seq = 10
        conn = FakeConn()
        with self.assertRaises(ReconnectNow):
            adapter._on_gateway_frame(conn, json.dumps({"op": OP_INVALID_SESSION, "d": False}))
        self.assertIsNone(adapter._session_id, "d=false 必须丢弃 session 走 Identify")

    def test_invalid_session_true_keeps_session(self):
        adapter = make_adapter()
        adapter._on_ready(ready_packet()["d"])
        conn = FakeConn()
        with self.assertRaises(ReconnectNow):
            adapter._on_gateway_frame(conn, json.dumps({"op": OP_INVALID_SESSION, "d": True}))
        self.assertIsNotNone(adapter._session_id)

    def test_op7_reconnect_closes_and_reconnects_immediately(self):
        adapter = make_adapter()
        conn = FakeConn()
        with self.assertRaises(ReconnectNow):
            adapter._on_gateway_frame(conn, json.dumps({"op": OP_RECONNECT}))
        self.assertTrue(conn.closed)

    def test_resumed_dispatch_is_accepted_and_ignored(self):
        adapter = make_adapter()
        adapter._on_event(json.dumps({"op": OP_DISPATCH, "s": 2002, "t": "RESUMED", "d": ""}))

    def test_sequence_is_tracked_from_null_free_frames_only(self):
        adapter = make_adapter()
        adapter._on_gateway_frame(FakeConn(), json.dumps({"op": OP_HEARTBEAT_ACK, "s": 12}))
        self.assertEqual(adapter._last_seq, 12)
        adapter._on_gateway_frame(
            FakeConn(), json.dumps({"op": OP_HEARTBEAT_ACK, "s": None})
        )
        self.assertEqual(adapter._last_seq, 12, "s 为 null 不能覆盖序列号")

    def test_gateway_url_is_fetched_over_rest_and_cached(self):
        adapter = make_adapter()
        stub = TokenStub(gateway_url="wss://api.example.invalid/websocket/")
        adapter._request = stub          # type: ignore[method-assign]
        url = adapter._resolve_gateway_url()
        self.assertEqual(url, "wss://api.example.invalid/websocket/")
        self.assertEqual(adapter.gateway_url, url)
        adapter._resolve_gateway_url()
        self.assertEqual(
            sum(1 for c in stub.calls if c[1] == "/gateway"), 1, "网关地址只取一次"
        )

    def test_gateway_url_failure_is_diagnosable(self):
        adapter = make_adapter()
        adapter._request = TokenStub(gateway_url="")   # type: ignore[method-assign]
        with self.assertRaises(RuntimeError) as ctx:
            adapter._resolve_gateway_url()
        self.assertIn("/gateway", str(ctx.exception))


# ======================================================================
# 入站过滤 / 防回环
# ======================================================================
class TestInboundFiltering(QQBotTestCase):
    def _feed(self, adapter: QQBotAdapter, packet: dict) -> bool:
        return adapter._handle_message(packet["t"], packet["d"])

    def test_group_at_message_becomes_inbound(self):
        hooks = RecordingHooks()
        adapter = make_adapter(hooks)
        self.assertTrue(self._feed(adapter, group_message_packet()))
        self.assertEqual(len(hooks.inbounds), 1)
        inbound = hooks.inbounds[0]
        self.assertEqual(inbound.conversation_id,
                         "qqbot:group:B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4E5")
        self.assertEqual(inbound.text, "你好")
        self.assertEqual(inbound.kind, "text")
        self.assertEqual(inbound.platform, "qqbot")
        self.assertEqual(inbound.user_id, "A1B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4")
        self.assertEqual(inbound.message_id, "ROBOT1.0_zzz")

    def test_c2c_message_becomes_inbound_with_user_openid(self):
        hooks = RecordingHooks()
        adapter = make_adapter(hooks)
        self.assertTrue(self._feed(adapter, c2c_message_packet()))
        self.assertEqual(
            hooks.inbounds[0].conversation_id,
            "qqbot:c2c:C3D4E5F6A1B2C3D4E5F6A1B2C3D4E5F6",
        )

    def test_channel_message_becomes_inbound(self):
        hooks = RecordingHooks()
        adapter = make_adapter(hooks)
        self.assertTrue(self._feed(adapter, channel_message_packet()))
        self.assertEqual(hooks.inbounds[0].conversation_id, "qqbot:channel:100010")
        self.assertEqual(hooks.inbounds[0].user_id, "1234")

    def test_content_is_passed_through_verbatim(self):
        """官方说群消息的 ``content`` **已去除 @机器人前缀** ⇒ 不许我们自己再剥。"""
        hooks = RecordingHooks()
        adapter = make_adapter(hooks)
        self._feed(adapter, group_message_packet(content="<@!bot> 看看这个"))
        self.assertEqual(hooks.inbounds[0].text, "<@!bot> 看看这个")

    # ---- 防回环 -------------------------------------------------------
    def test_author_bot_true_is_dropped(self):
        """平台签发的 ``author.bot`` —— 不用任何内容启发式。"""
        hooks = RecordingHooks()
        adapter = make_adapter(hooks)
        self.assertFalse(self._feed(adapter, group_message_packet(author_bot=True)))
        self.assertEqual(hooks.inbounds, [])
        self.assertIn("author.bot=true", self.log_text)

    def test_own_user_id_from_ready_is_dropped(self):
        """``author.id == READY.d.user.id`` —— 这是频道场景能精确识别自己的字段。"""
        hooks = RecordingHooks()
        adapter = make_adapter(hooks)
        adapter._on_ready(ready_packet(user_id="1234")["d"])
        self.assertFalse(self._feed(adapter, channel_message_packet(author_id="1234")))
        self.assertEqual(hooks.inbounds, [])
        self.assertIn("READY.user.id", self.log_text)

    def test_other_bots_messages_are_dropped_too(self):
        """``author.bot`` 会连带丢掉**别的**机器人 —— 这是明确的取舍，日志写清楚。"""
        hooks = RecordingHooks()
        adapter = make_adapter(hooks)
        self.assertFalse(
            self._feed(adapter, group_message_packet(author_bot=True, author_id="OTHERBOT"))
        )

    def test_loop_guard_does_not_use_content_heuristics(self):
        """★ 回归：防回环**绝不能**退化成"看内容像不像自己发的"（不变量 14）。

        一条内容与我们典型回复一字不差、但发送者是真人 —— 必须放行。
        """
        hooks = RecordingHooks()
        adapter = make_adapter(hooks)
        adapter._on_ready(ready_packet(user_id="1234")["d"])
        self.assertTrue(
            self._feed(adapter, group_message_packet(content="opencode-bridge 已完成"))
        )
        self.assertEqual(len(hooks.inbounds), 1)

    def test_loop_guard_holds_before_ready_is_received(self):
        """READY 还没来也不能漏防回环 —— ``author.bot`` 独立于它。"""
        hooks = RecordingHooks()
        adapter = make_adapter(hooks)
        self.assertFalse(self._feed(adapter, group_message_packet(author_bot=True)))
        self.assertEqual(hooks.inbounds, [])

    # ---- 噪声过滤 -----------------------------------------------------
    def test_non_text_message_types_are_dropped(self):
        for message_type in (3, 101, 102, 103):
            with self.subTest(message_type=message_type):
                hooks = RecordingHooks()
                adapter = make_adapter(hooks)
                self.assertFalse(
                    self._feed(adapter, group_message_packet(message_type=message_type))
                )
                self.assertEqual(hooks.inbounds, [])

    def test_invalid_message_type_is_dropped(self):
        hooks = RecordingHooks()
        adapter = make_adapter(hooks)
        packet = group_message_packet()
        packet["d"]["message_type"] = "0"          # 字符串不是合法类型
        self.assertFalse(self._feed(adapter, packet))
        self.assertEqual(hooks.inbounds, [])

    def test_empty_or_blank_content_is_dropped(self):
        for content in ("", "   ", "\n\t"):
            with self.subTest(content=repr(content)):
                hooks = RecordingHooks()
                adapter = make_adapter(hooks)
                self.assertFalse(self._feed(adapter, group_message_packet(content=content)))
                self.assertEqual(hooks.inbounds, [])

    def test_missing_conversation_identifier_is_dropped(self):
        hooks = RecordingHooks()
        adapter = make_adapter(hooks)
        self.assertFalse(self._feed(adapter, group_message_packet(group_openid="")))
        self.assertEqual(hooks.inbounds, [])

    def test_malformed_payloads_are_dropped_without_raising(self):
        hooks = RecordingHooks()
        adapter = make_adapter(hooks)
        for payload in (None, [], "text", 42, {}):
            with self.subTest(payload=payload):
                self.assertFalse(adapter._handle_message("GROUP_AT_MESSAGE_CREATE", payload))
        self.assertEqual(hooks.inbounds, [])

    def test_full_group_message_event_is_handled_too(self):
        """``GROUP_MESSAGE_CREATE``（全量模式）与 @ 模式同形状，必须同样处理。"""
        hooks = RecordingHooks()
        adapter = make_adapter(hooks)
        packet = group_message_packet()
        packet["t"] = "GROUP_MESSAGE_CREATE"
        self.assertTrue(self._feed(adapter, packet))
        self.assertEqual(len(hooks.inbounds), 1)

    def test_guild_message_create_is_handled(self):
        hooks = RecordingHooks()
        adapter = make_adapter(hooks)
        packet = channel_message_packet()
        packet["t"] = "MESSAGE_CREATE"
        self.assertTrue(self._feed(adapter, packet))

    def test_unhandled_events_are_ignored(self):
        hooks = RecordingHooks()
        adapter = make_adapter(hooks)
        for name in ("GUILD_CREATE", "MESSAGE_REACTION_ADD", "AT_EVERYONE", ""):
            with self.subTest(name=name):
                adapter._handle_dispatch(name, {"whatever": 1})
        self.assertEqual(hooks.inbounds, [])

    def test_direct_message_create_warns_once_and_is_not_handled(self):
        hooks = RecordingHooks()
        adapter = make_adapter(hooks)
        adapter._handle_dispatch("DIRECT_MESSAGE_CREATE", {"guild_id": "G"})
        adapter._handle_dispatch("DIRECT_MESSAGE_CREATE", {"guild_id": "G"})
        self.assertEqual(hooks.inbounds, [])
        self.assertEqual(self.log_text.count("DIRECT_MESSAGE_CREATE"), 1)


class TestAccessGate(QQBotTestCase):
    def test_non_whitelisted_conversation_is_dropped_before_inbound(self):
        hooks = RecordingHooks()
        adapter = make_adapter(hooks, allowed_chat_ids=["OTHER-GROUP"])
        self.assertFalse(
            adapter._handle_message("GROUP_AT_MESSAGE_CREATE",
                                    group_message_packet()["d"])
        )
        self.assertEqual(hooks.inbounds, [], "闸门必须早于 Inbound，否则命令字可绕过")

    def test_whitelisted_conversation_is_admitted(self):
        hooks = RecordingHooks()
        target = "B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4E5"
        adapter = make_adapter(hooks, allowed_chat_ids=[target])
        self.assertTrue(
            adapter._handle_message("GROUP_AT_MESSAGE_CREATE",
                                    group_message_packet(group_openid=target)["d"])
        )

    def test_empty_allowlist_admits_everything_without_config_version(self):
        """无 ``config_version`` ⇒ 沿用旧的「空 = 全开」（本夹具不传该键）。"""
        hooks = RecordingHooks()
        adapter = make_adapter(hooks)
        self.assertTrue(
            adapter._handle_message("GROUP_AT_MESSAGE_CREATE",
                                    group_message_packet()["d"])
        )

    def test_empty_allowlist_rejects_everything_once_config_version_flips(self):
        """``config_version >= 2`` ⇒ 空 = 全拒。

        ⚠️ 顺带钉住 qqbot 用**基类默认** ``pairing_supported = False``：principal
        虽然是稳定会话 id，但它与"谁能驱动"不是一回事（同群任何成员共享它），
        拿它当配对锚点等于把整个群一起授权。
        """
        hooks = RecordingHooks()
        adapter = make_adapter(hooks, config_version=2)
        self.assertFalse(
            adapter._handle_message("GROUP_AT_MESSAGE_CREATE",
                                    group_message_packet()["d"])
        )
        self.assertEqual(len(hooks.inbounds), 0)
        self.assertFalse(adapter.pairing_supported)

    def test_on_inbound_exception_does_not_escape(self):
        hooks = RecordingHooks()
        hooks.raise_on_inbound = True
        adapter = make_adapter(hooks)
        self.assertFalse(
            adapter._handle_message("GROUP_AT_MESSAGE_CREATE",
                                    group_message_packet()["d"])
        )


# ======================================================================
# 出站
# ======================================================================
class TestSend(QQBotTestCase):
    def _adapter(self, *, min_interval: float = 0.0, **config) -> tuple:
        adapter = make_adapter(**config)
        stub = TokenStub()
        adapter._request = stub          # type: ignore[method-assign]
        adapter.min_interval = min_interval   # type: ignore[assignment]
        return adapter, stub

    def _bodies(self, stub: TokenStub, prefix: str) -> List[dict]:
        return [c[2] for c in stub.calls if c[1].startswith(prefix)]

    def test_group_send_uses_official_path_and_payload(self):
        adapter, stub = self._adapter()
        handle = adapter.send(Outbound(conversation_id="qqbot:group:G1", text="hi"))
        method, path, _payload, token = stub.calls[-1]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/v2/groups/G1/messages")
        self.assertEqual(token, ACCESS_TOKEN, "凭据走 header 的 Authorization")
        self.assertIsNotNone(handle)
        self.assertEqual(handle.platform, "qqbot")
        self.assertEqual(handle.message_id, "ROBOT1.0_stubmessageid")

    def test_c2c_send_uses_v2_users_path(self):
        adapter, stub = self._adapter()
        adapter.send(Outbound(conversation_id="qqbot:c2c:U1", text="hi"))
        self.assertEqual(stub.calls[-1][1], "/v2/users/U1/messages")

    def test_channel_send_uses_channels_path_without_msg_type(self):
        adapter, stub = self._adapter()
        adapter.send(Outbound(conversation_id="qqbot:channel:100010", text="hi"))
        self.assertEqual(stub.calls[-1][1], "/channels/100010/messages")
        self.assertNotIn("msg_type", stub.calls[-1][2])

    def test_inbound_msg_id_is_used_for_passive_reply(self):
        """★ 带 ``msg_id`` 才是被动消息：不受"主动消息频控"约束，群里没权限时主动发会
        直接 40034105 失败。所以这条路径是能不能回上话的关键。"""
        adapter, stub = self._adapter()
        self.assertTrue(
            adapter._handle_message("GROUP_AT_MESSAGE_CREATE", group_message_packet()["d"])
        )
        adapter.send(Outbound(conversation_id=GROUP_CID, text="hi"))
        payload = self._bodies(stub, "/v2/groups/")[0]
        self.assertEqual(payload["msg_id"], "ROBOT1.0_zzz")
        self.assertEqual(payload["msg_seq"], 1)

    def test_msg_seq_increments_per_attempt(self):
        """官方：相同 msg_id + msg_seq 重复发送会失败（40054005）。"""
        adapter, stub = self._adapter()
        adapter._handle_message("GROUP_AT_MESSAGE_CREATE", group_message_packet()["d"])
        for _ in range(3):
            adapter.send(Outbound(conversation_id=GROUP_CID, text="hi"))
        seqs = [b["msg_seq"] for b in self._bodies(stub, "/v2/groups/")]
        self.assertEqual(seqs, [1, 2, 3])

    def test_passive_reply_stops_after_official_cap(self):
        """官方：群 5 次、单聊 4 次被动回复上限。"""
        adapter, stub = self._adapter()
        adapter._handle_message("GROUP_AT_MESSAGE_CREATE", group_message_packet()["d"])
        for _ in range(7):
            adapter.send(Outbound(conversation_id=GROUP_CID, text="hi"))
        with_seq = [b for b in self._bodies(stub, "/v2/groups/") if "msg_id" in b]
        self.assertEqual([b["msg_seq"] for b in with_seq], [1, 2, 3, 4, 5])

    def test_passive_reply_cap_for_c2c_is_four(self):
        adapter, stub = self._adapter()
        adapter._handle_message("C2C_MESSAGE_CREATE", c2c_message_packet()["d"])
        cid = QQBotAdapter.conversation_id_for(
            "c2c", "C3D4E5F6A1B2C3D4E5F6A1B2C3D4E5F6")
        for _ in range(6):
            adapter.send(Outbound(conversation_id=cid, text="hi"))
        with_seq = [b for b in self._bodies(stub, "/v2/users/") if "msg_id" in b]
        self.assertEqual([b["msg_seq"] for b in with_seq], [1, 2, 3, 4])

    def test_expired_passive_reply_is_not_reused(self):
        adapter, stub = self._adapter()
        adapter._handle_message("GROUP_AT_MESSAGE_CREATE", group_message_packet()["d"])
        self.assertIn(GROUP_CID, adapter._reply_ctx)
        adapter._reply_ctx[GROUP_CID] = ("ROBOT1.0_zzz", time.monotonic() - 1.0, 1)
        adapter.send(Outbound(conversation_id=GROUP_CID, text="hi"))
        self.assertNotIn("msg_id", self._bodies(stub, "/v2/groups/")[0])

    def test_channel_never_sends_msg_id(self):
        adapter, stub = self._adapter()
        adapter._handle_message("AT_MESSAGE_CREATE", channel_message_packet()["d"])
        adapter.send(Outbound(conversation_id="qqbot:channel:100010", text="hi"))
        self.assertNotIn("msg_id", stub.calls[-1][2])

    def test_empty_text_is_refused(self):
        adapter, _stub = self._adapter()
        self.assertIsNone(adapter.send(Outbound(conversation_id="qqbot:group:G1", text="")))
        self.assertEqual(adapter.last_send_error, SendError.BAD_FORMAT)

    def test_error_code_with_http_200_is_a_failure(self):
        """官方凭证接口明说失败也是 HTTP 200；OpenAPI 侧同样要查 ``err_code``。"""
        adapter, _stub = self._adapter()
        stub = TokenStub()
        stub.outbound = [(200, {"err_code": 40034105,
                                "message": "主动消息发送失败，无权限"})]
        adapter._request = stub          # type: ignore[method-assign]
        self.assertIsNone(adapter.send(Outbound(conversation_id="qqbot:group:G1", text="hi")))
        self.assertEqual(adapter.last_send_error, SendError.FORBIDDEN)

    def test_2xx_without_id_is_a_failure(self):
        adapter, _stub = self._adapter()
        adapter._request = lambda *a, **k: (200, {"message": "ok"})
        adapter.min_interval = 0.0       # type: ignore[assignment]
        self.assertIsNone(adapter.send(Outbound(conversation_id="qqbot:group:G1", text="hi")))

    def test_error_code_classification(self):
        from opencode_bridge.adapters.qqbot import _classify_qq_error
        cases = {
            40054007: SendError.TOO_LONG,       # 消息长度超限
            40054018: SendError.TOO_LONG,       # 消息过长或异常
            40034100: SendError.RATE_LIMITED,   # 主动消息发送超过频控限制
            40034105: SendError.FORBIDDEN,      # 主动消息发送失败，无权限
            11253: SendError.FORBIDDEN,         # 应用无接口访问权限
            40034101: SendError.NOT_FOUND,      # 机器人非群成员
            22006: SendError.BAD_FORMAT,        # 消息类型与内容不匹配
        }
        for code, expected in cases.items():
            with self.subTest(code=code):
                self.assertEqual(_classify_qq_error(400, code, ""), expected)

    def test_http_status_classification_fallback(self):
        from opencode_bridge.adapters.qqbot import _classify_qq_error
        self.assertEqual(_classify_qq_error(429, None, ""), SendError.RATE_LIMITED)
        self.assertEqual(_classify_qq_error(500, None, ""), SendError.TRANSIENT)
        self.assertEqual(_classify_qq_error(0, None, "boom"), SendError.TRANSIENT)

    def test_partial_send_is_marked_partial(self):
        adapter, stub = self._adapter()
        stub.outbound = [
            (200, {"id": "ROBOT1.0_first"}),
            (400, {"err_code": 40034100, "message": "频控"}),
        ]
        result = adapter.send_result(
            Outbound(conversation_id="qqbot:group:G1",
                     text="字" * (MESSAGE_LIMIT + 5))
        )
        # 基类约定：至少有一片送达 ⇒ ok=True，但必须带 partial=True 与失败分类，
        # 否则调用方会把"只到了一半"当成完整送达。
        self.assertTrue(result.ok)
        self.assertTrue(result.partial, "分片中部分成功必须标 partial")
        self.assertEqual(result.error_kind, SendError.RATE_LIMITED)
        self.assertIsNotNone(result.handle, "partial 时 handle 指向最后一条成功的消息")

    def test_throttle_enforces_min_interval(self):
        adapter, _stub = self._adapter(min_interval=0.3)
        started = time.monotonic()
        adapter.send(Outbound(conversation_id="qqbot:group:G1", text="a"))
        adapter.send(Outbound(conversation_id="qqbot:group:G1", text="b"))
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.25, "节流必须真的等（wall-clock 只做下界）")

    def test_throttle_is_per_conversation(self):
        adapter, _stub = self._adapter(min_interval=0.3)
        started = time.monotonic()
        adapter.send(Outbound(conversation_id="qqbot:group:G1", text="a"))
        adapter.send(Outbound(conversation_id="qqbot:group:G2", text="b"))
        self.assertLess(time.monotonic() - started, 0.25, "不同会话之间不该互相阻塞")


class TestEdit(QQBotTestCase):
    """``edit()`` 必须诚实返回 ``False``。"""

    def test_edit_returns_false_and_makes_no_http_call(self):
        from opencode_bridge.hooks import MsgHandle
        adapter = make_adapter()
        stub = TokenStub()
        adapter._request = stub          # type: ignore[method-assign]
        handle = MsgHandle(conversation_id="qqbot:group:G1", message_id="m1",
                           platform="qqbot")
        for cid in ("qqbot:group:G1", "qqbot:c2c:U1", "qqbot:channel:100010"):
            with self.subTest(cid=cid):
                self.assertIs(adapter.edit(handle, Outbound(conversation_id=cid,
                                                            text="x")), False)
        self.assertEqual(stub.calls, [], "edit 不许发任何 HTTP 请求")

    def test_edit_logs_the_reason_once(self):
        from opencode_bridge.hooks import MsgHandle
        adapter = make_adapter()
        handle = MsgHandle(conversation_id="qqbot:group:G1", message_id="m1",
                           platform="qqbot")
        for _ in range(3):
            adapter.edit(handle, Outbound(conversation_id="qqbot:group:G1", text="x"))
        self.assertEqual(self.log_text.count("edit() 恒返回 False"), 1,
                         "core 会对每次进度更新都调 edit，只告警一次")

    def test_no_edit_endpoint_is_documented_for_group_or_c2c(self):
        """复核用：源码注释必须写清"查不到/不存在"的依据。"""
        import pathlib
        source = (pathlib.Path(__file__).resolve().parents[1]
                  / "opencode_bridge" / "adapters" / "qqbot.py").read_text("utf-8")
        self.assertIn("群聊 / 单聊场景官方**只有撤回**", source)
        self.assertIn("50049", source)


# ======================================================================
# 生命周期：重连 / 半开 / stop()
# ======================================================================
class TestReconnect(QQBotTestCase):
    def test_server_dropping_connection_triggers_reconnect(self):
        """连接被服务端悄悄关掉 —— 不能假设"读不到就是断了"，要真的重连。"""
        def handler(conn, gw, index):
            conn.settimeout(5.0)
            if index == 0:
                send_json(conn, hello_packet(45000))
                recv_json(conn, timeout=3.0)          # Identify
                time.sleep(0.05)
                send_close(conn, 1001)                 # 服务端关闭
            else:
                send_json(conn, hello_packet(45000))

        with Gateway(handler) as gw:
            adapter = make_adapter(gateway_url=gw.url)
            adapter._request = TokenStub()       # type: ignore[method-assign]
            adapter.ws_recv_timeout = 5.0        # type: ignore[assignment]
            adapter.start()
            try:
                self.assertTrue(
                    wait_for(lambda: gw.handshakes >= 2, timeout=WAIT),
                    "服务端关闭后必须重连（handshakes=%d）" % gw.handshakes,
                )
            finally:
                adapter.stop()
        self.assertEqual(gw.errors, [])

    def test_op7_reconnect_opens_a_new_connection(self):
        def handler(conn, gw, index):
            conn.settimeout(5.0)
            send_json(conn, hello_packet(45000))
            recv_json(conn, timeout=3.0)
            if index == 0:
                send_json(conn, {"op": OP_RECONNECT})

        with Gateway(handler) as gw:
            adapter = make_adapter(gateway_url=gw.url)
            adapter._request = TokenStub()       # type: ignore[method-assign]
            adapter.ws_recv_timeout = 5.0        # type: ignore[assignment]
            adapter.start()
            try:
                self.assertTrue(wait_for(lambda: gw.handshakes >= 2, timeout=WAIT))
            finally:
                adapter.stop()

    def test_silent_server_is_detected_by_read_timeout(self):
        """半开连接：TCP 静默、服务端一个字节都不发 ⇒ 读超时兜底并重连。"""
        def handler(conn, gw, index):
            time.sleep(2.0)                     # 什么都不发，也不关

        with Gateway(handler) as gw:
            adapter = make_adapter(gateway_url=gw.url)
            adapter._request = TokenStub()       # type: ignore[method-assign]
            adapter.ws_recv_timeout = 0.4        # type: ignore[assignment]
            adapter.start()
            try:
                self.assertTrue(
                    wait_for(lambda: gw.handshakes >= 2, timeout=WAIT),
                    "静默连接必须被读超时发现并重连（handshakes=%d）" % gw.handshakes,
                )
            finally:
                adapter.stop()

    def test_watchdog_does_not_accumulate_zombie_threads(self):
        def handler(conn, gw, index):
            conn.settimeout(5.0)
            send_json(conn, hello_packet(45000))
            recv_json(conn, timeout=3.0)
            time.sleep(0.02)
            send_close(conn, 1001)

        with Gateway(handler) as gw:
            adapter = make_adapter(gateway_url=gw.url)
            adapter._request = TokenStub()       # type: ignore[method-assign]
            adapter.ws_recv_timeout = 5.0        # type: ignore[assignment]
            adapter.start()
            try:
                self.assertTrue(wait_for(lambda: gw.handshakes >= 3, timeout=WAIT))
                time.sleep(0.3)
                alive = [t for t in threading.enumerate()
                         if t.name.startswith("qqbot-heartbeat") and t.is_alive()]
                self.assertLessEqual(
                    len(alive), 1,
                    f"每条会话结束时必须停掉上一条会话的看门狗，实际存活 {len(alive)} 条",
                )
            finally:
                adapter.stop()

    def test_identify_failure_before_ready_is_diagnosed(self):
        """官方对 intents 越权 / 鉴权失败**不给错误负载**，只关连接 —— 必须从状态推出结论。"""
        def handler(conn, gw, index):
            conn.settimeout(5.0)
            send_json(conn, hello_packet(45000))
            try:
                recv_json(conn, timeout=3.0)     # Identify
            except (ConnectionError, TimeoutError, socket.timeout):
                return
            time.sleep(0.02)
            send_close(conn, 4004)                # 鉴权失败式关闭

        with Gateway(handler) as gw:
            adapter = make_adapter(gateway_url=gw.url)
            self.stub_http(adapter)
            adapter.ws_recv_timeout = 5.0        # type: ignore[assignment]
            adapter.start()
            try:
                # 等"诊断日志真的写出来"这个内部状态，别睡固定时间（架构不变量 18）
                self.assertTrue(
                    wait_for(lambda: "没收到 READY/RESUMED" in self.log_text, timeout=WAIT),
                    f"发了 Identify 却没有 READY 就被关掉，必须给出可执行诊断；日志：{self.log_text!r}",
                )
                self.assertIn("无权限的 intents", self.log_text)
            finally:
                adapter.stop()

    def test_handshake_before_hello_is_diagnosed(self):
        def handler(conn, gw, index):
            conn.settimeout(5.0)
            time.sleep(0.02)
            send_close(conn, 1002)

        with Gateway(handler) as gw:
            adapter = make_adapter(gateway_url=gw.url)
            self.stub_http(adapter)
            adapter.ws_recv_timeout = 5.0        # type: ignore[assignment]
            adapter.start()
            try:
                self.assertTrue(
                    wait_for(lambda: "收到 Hello(op 10) 之前" in self.log_text, timeout=WAIT),
                    f"连 Hello 都没收到就断，必须诊断为握手/网络层问题；日志：{self.log_text!r}",
                )
            finally:
                adapter.stop()


class TestStop(QQBotTestCase):
    def test_stop_is_clean_and_fast_even_when_recv_is_blocked(self):
        """★ stop() 必须**先关 WS** 唤醒阻塞中的 recv()，否则每次都白等一个读超时。

        服务器在握手后彻底静默 ⇒ 客户端 recv() 会一直阻塞（读超时 120s）。
        """
        def handler(conn, gw, index):
            conn.settimeout(10.0)
            time.sleep(5.0)                     # 静默：不发 Hello，也不关

        with Gateway(handler) as gw:
            adapter = make_adapter(gateway_url=gw.url)
            adapter._request = TokenStub()       # type: ignore[method-assign]
            adapter.ws_recv_timeout = 120.0      # type: ignore[assignment]
            adapter.start()
            self.assertTrue(wait_for(lambda: gw.handshakes >= 1, timeout=WAIT))
            started = time.monotonic()
            adapter.stop()
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 2.0, f"stop() 耗时 {elapsed:.2f}s —— 读超时没被绕过")
            self.assertFalse(adapter.running)

    def test_stop_is_idempotent(self):
        def handler(conn, gw, index):
            send_json(conn, hello_packet(45000))

        with Gateway(handler) as gw:
            adapter = make_adapter(gateway_url=gw.url)
            adapter._request = TokenStub()       # type: ignore[method-assign]
            adapter.ws_recv_timeout = 5.0        # type: ignore[assignment]
            adapter.start()
            self.assertTrue(wait_for(lambda: gw.handshakes >= 1, timeout=WAIT))
            adapter.stop()
            adapter.stop()                       # 不许二次崩
            self.assertFalse(adapter.running)

    def test_stop_without_start_is_safe(self):
        make_adapter().stop()

    def test_transport_stats_are_observable(self):
        def handler(conn, gw, index):
            send_json(conn, hello_packet(45000))
            try:
                recv_json(conn, timeout=3.0)
                recv_json(conn, timeout=3.0)
            except (ConnectionError, TimeoutError, socket.timeout):
                return
            time.sleep(0.02)
            send_close(conn, 1000)

        with Gateway(handler) as gw:
            adapter = make_adapter(gateway_url=gw.url)
            adapter._request = TokenStub()       # type: ignore[method-assign]
            adapter.ws_recv_timeout = 5.0        # type: ignore[assignment]
            adapter.start()
            transport = adapter.transport
            self.assertIsNotNone(transport)
            try:
                # sessions 只在"会话**结束**后"才 +1，而最后一条会话是被 stop() 打断的
                # （基类 break 之前不计数），所以直接等 sessions>=1，别等 connects>=2
                # 再去看 sessions —— 那是个竞态。
                self.assertTrue(
                    wait_for(lambda: transport.stats()["sessions"] >= 1, timeout=WAIT),
                    f"连接结束后 sessions 应至少 +1，实际 {transport.stats()}",
                )
                self.assertTrue(
                    wait_for(lambda: transport.stats()["connects"] >= 2, timeout=WAIT),
                    f"断开后应重连（connects>=2），实际 {transport.stats()}",
                )
                self.assertGreaterEqual(transport.stats()["events"], 1)
            finally:
                adapter.stop()


# ======================================================================
# 真服务器上的端到端入站 + 畸形输入 + 凭据卫生
# ======================================================================
class TestEndToEndOverRealServer(QQBotTestCase):
    def test_handshake_barrier_against_ws_pysilently_dropped_first_frame(self):
        """★ 反转过一次：这条断言原本要求 ``FIRST_FRAME_DELAY > 0``。
    
        那是在**绕过** ``ws.py`` 的一个真实丢帧 bug —— 当时服务端把 101 响应与
        第一帧放进同一个 TCP 段时，``_check_handshake`` 会把多读到的字节丢掉，
        于是第一帧被静默吞掉、客户端一直等到读超时。该 bug **已修复**
        （``connect()`` 存 ``_prefetch``，``_read_frame`` 优先消费）。
    
        所以现在断言反过来：**必须能在同一个 TCP 段里收到第一帧**。保留这个严苛
        布局是有意的 —— 它让 qqbot 的整套真服务器测试顺带覆盖那条路径，而那
        正是现实里服务端的行为（Discord 网关就这么干）。回归测试见
        ``tests/test_ws.py::TestPipelinedFirstFrame``。
        """
        self.assertEqual(
            FIRST_FRAME_DELAY, 0.0,
            "第一帧必须与握手同段发出：留延时等于把这个平台的入站测试废掉了",
        )

    def test_real_server_produces_inbound(self):
        hooks = RecordingHooks()

        def handler(conn, gw, index):
            conn.settimeout(5.0)
            send_json(conn, hello_packet(45000))
            recv_json(conn, timeout=3.0)                    # Identify
            send_json(conn, {"op": OP_HEARTBEAT_ACK})
            send_json(conn, ready_packet())
            send_json(conn, group_message_packet(seq=2))
            time.sleep(0.3)

        with Gateway(handler) as gw:
            adapter = make_adapter(hooks, gateway_url=gw.url)
            adapter._request = TokenStub()       # type: ignore[method-assign]
            adapter.ws_recv_timeout = 5.0        # type: ignore[assignment]
            adapter.start()
            try:
                self.assertTrue(
                    wait_for(lambda: len(hooks.inbounds) >= 1, timeout=WAIT),
                    f"真服务器推的群消息必须变成 Inbound；握手次数 {gw.handshakes}",
                )
            finally:
                adapter.stop()
        inbound = hooks.inbounds[0]
        self.assertEqual(inbound.platform, "qqbot")
        self.assertEqual(inbound.conversation_id, GROUP_CID)
        self.assertEqual(gw.errors, [])

    def test_handshake_carries_no_credentials(self):
        """官方握手**不需要**任何凭据（鉴权在 op 2 Identify）—— 不许塞 query 或 header。"""
        def handler(conn, gw, index):
            conn.settimeout(5.0)
            send_json(conn, hello_packet(45000))
            try:
                recv_json(conn, timeout=3.0)
            except (ConnectionError, TimeoutError, socket.timeout):
                return
            time.sleep(0.1)

        with Gateway(handler) as gw:
            adapter = make_adapter(gateway_url=gw.url)
            adapter._request = TokenStub()       # type: ignore[method-assign]
            adapter.ws_recv_timeout = 5.0        # type: ignore[assignment]
            adapter.start()
            try:
                self.assertTrue(wait_for(lambda: gw.handshakes >= 1, timeout=WAIT))
            finally:
                adapter.stop()
        request_line = gw.requests[0]["__request_line__"]
        self.assertNotIn("?", request_line, "网关地址绝不能带 query string")
        self.assertNotIn("token", request_line.lower())
        for header in ("authorization", "access_token", "appid", "appsecret"):
            self.assertNotIn(header, gw.requests[0],
                             f"握手请求里不该有 {header} 头")

    def test_identify_carries_token_in_body_not_in_url(self):
        received = []

        def handler(conn, gw, index):
            conn.settimeout(5.0)
            send_json(conn, hello_packet(45000))
            drain_until_close(conn, received.append)

        with Gateway(handler) as gw:
            adapter = make_adapter(gateway_url=gw.url)
            self.stub_http(adapter)
            adapter.ws_recv_timeout = 30.0       # type: ignore[assignment]
            adapter.start()
            try:
                self.assertTrue(
                    wait_for(
                        lambda: any(p.get("op") == OP_IDENTIFY for p in received),
                        timeout=WAIT,
                    ),
                    f"必须在观察窗内收到 Identify；实际收到 {received!r}",
                )
            finally:
                adapter.stop()
        identify = next(p for p in received if p["op"] == OP_IDENTIFY)
        self.assertEqual(identify["d"]["token"], f"QQBot {ACCESS_TOKEN}")
        self.assertNotIn(ACCESS_TOKEN, gw.url, "网关地址里绝不能带 token")


class TestMalformedInput(QQBotTestCase):
    """畸形帧 / 畸形消息：**不许**把栈信息泄漏出去，也不许把网关线程带走。"""

    def _drive(self, blobs: List) -> Gateway:
        def handler(conn, gw, index):
            conn.settimeout(5.0)
            send_json(conn, hello_packet(45000))
            try:
                recv_json(conn, timeout=3.0)          # Identify
            except (ConnectionError, TimeoutError, socket.timeout):
                return
            send_json(conn, {"op": OP_HEARTBEAT_ACK})
            send_json(conn, ready_packet())
            for blob in blobs:
                blob(conn)
                time.sleep(0.02)
            time.sleep(0.3)

        gw = Gateway(handler)
        gw.start()
        return gw

    def test_malformed_frames_do_not_crash_and_do_not_leak_traceback(self):
        hooks = RecordingHooks()
        blobs = [
            lambda c: send_text(c, "this is not json"),
            lambda c: send_text(c, "[1, 2, 3]"),
            lambda c: send_text(c, "null"),
            lambda c: send_text(c, '{"s": 3}'),                       # 没有 op
            lambda c: send_text(c, '{"op": "not-an-int", "d": {}}'),
            lambda c: send_text(c, '{"op": 99, "d": {"x": 1}}'),    # 未知 opcode
            lambda c: send_text(c, '{"op": 0, "t": 123, "d": "x"}'), # t 不是字符串
            lambda c: send_frame(c, _OP_TEXT, b"", rsv1=True),       # RSV 位非零
        ]
        gw = self._drive(blobs)
        try:
            adapter = make_adapter(hooks, gateway_url=gw.url)
            self.stub_http(adapter)
            adapter.ws_recv_timeout = 5.0        # type: ignore[assignment]
            adapter.start()
            # 等"畸形帧被处理过"这个**内部状态**，而不是睡固定时间 —— 后者在 CI 上会 flaky
            self.assertTrue(
                wait_for(lambda: "非 JSON 帧" in self.log_text, timeout=WAIT),
                "非 JSON 帧应被记日志后忽略",
            )
            self.assertTrue(wait_for(lambda: gw.handshakes >= 2, timeout=WAIT),
                            "非法帧导致会话结束后应重连")
            self.assertTrue(adapter.running, "消费线程必须还活着")
            self.assertEqual(hooks.inbounds, [], "畸形帧不该产生 Inbound")
        finally:
            adapter.stop()
            gw.stop()
        self.assertNotIn("Traceback", self.formatted_logs,
                         "畸形输入不许把 traceback 泄进日志")
        self.assertEqual(self.records_with_traceback, [],
                         "适配器自己的防御路径不该 logger.exception")

    def test_malformed_dispatch_payload_does_not_reach_hooks(self):
        hooks = RecordingHooks()

        def handler(conn, gw, index):
            conn.settimeout(5.0)
            send_json(conn, hello_packet(45000))
            try:
                recv_json(conn, timeout=3.0)
            except (ConnectionError, TimeoutError, socket.timeout):
                return
            send_json(conn, {"op": OP_HEARTBEAT_ACK})
            send_json(conn, ready_packet())
            for payload in (None, [], "x", 42, {}, {"id": None}, {"content": 5}):
                send_json(conn, {"op": OP_DISPATCH, "s": 9,
                                 "t": "GROUP_AT_MESSAGE_CREATE", "d": payload})
            time.sleep(0.3)

        with Gateway(handler) as gw:
            adapter = make_adapter(hooks, gateway_url=gw.url)
            self.stub_http(adapter)
            adapter.ws_recv_timeout = 5.0        # type: ignore[assignment]
            adapter.start()
            try:
                self.assertTrue(
                    wait_for(lambda: gw.handshakes >= 1, timeout=WAIT)
                )
                wait_for(lambda: gw.handshakes >= 2, timeout=WAIT)
                self.assertEqual(hooks.inbounds, [])
                self.assertTrue(adapter.running)
            finally:
                adapter.stop()
        self.assertNotIn("Traceback", self.formatted_logs)
        self.assertEqual(self.records_with_traceback, [])

    def test_binary_frame_is_not_silently_accepted(self):
        """``ws.py`` 收到二进制帧会抛错（宁可崩在可见的协议错误上）—— 适配器要能重连。"""
        def handler(conn, gw, index):
            conn.settimeout(5.0)
            send_json(conn, hello_packet(45000))
            try:
                recv_json(conn, timeout=3.0)
            except (ConnectionError, TimeoutError, socket.timeout):
                return
            send_binary(conn, b"\x00\x01\x02")
            time.sleep(0.2)

        with Gateway(handler) as gw:
            adapter = make_adapter(gateway_url=gw.url)
            adapter._request = TokenStub()       # type: ignore[method-assign]
            adapter.ws_recv_timeout = 5.0        # type: ignore[assignment]
            adapter.start()
            try:
                self.assertTrue(
                    wait_for(lambda: gw.handshakes >= 2, timeout=WAIT),
                    "收到非法二进制帧后应重连而不是静默丢消息",
                )
            finally:
                adapter.stop()


class TestCredentialHygiene(QQBotTestCase):
    def test_secrets_never_reach_the_log(self):
        hooks = RecordingHooks()

        def handler(conn, gw, index):
            conn.settimeout(5.0)
            send_json(conn, hello_packet(45000))
            try:
                packet = recv_json(conn, timeout=3.0)
            except (ConnectionError, TimeoutError, socket.timeout):
                return
            send_json(conn, {"op": OP_HEARTBEAT_ACK})
            send_json(conn, ready_packet())
            send_json(conn, group_message_packet(seq=2, author_bot=True))
            send_json(conn, group_message_packet(seq=3))
            time.sleep(0.3)

        with Gateway(handler) as gw:
            adapter = make_adapter(hooks, gateway_url=gw.url)
            adapter._request = TokenStub()       # type: ignore[method-assign]
            adapter.ws_recv_timeout = 5.0        # type: ignore[assignment]
            adapter.start()
            try:
                self.assertTrue(wait_for(lambda: len(hooks.inbounds) >= 1, timeout=WAIT))
                # 顺带把出站也跑一遍（带 Authorization 头的请求不许把 token 写进日志）
                adapter.min_interval = 0.0       # type: ignore[assignment]
                adapter.send(Outbound(conversation_id="qqbot:group:G1", text="hi"))
                adapter.stop()
            finally:
                adapter.stop()

        text = self.log_text
        self.assertNotIn(APP_SECRET, text, "AppSecret 绝不能进日志")
        self.assertNotIn(ACCESS_TOKEN, text, "access_token 绝不能进日志")
        self.assertNotIn(APP_ID, text, "AppID 必须脱敏后出现")
        # 脱敏后的样子应该出现
        self.assertIn("*" * (len(APP_ID) - 4) + APP_ID[-4:], text)

    def test_gateway_url_query_is_never_logged(self):
        """防御"有人把凭据配到 URL 里"：日志只留 scheme://host/path。"""
        adapter = make_adapter()
        adapter._request = TokenStub(
            gateway_url="wss://api.example.invalid/websocket/?access_token=SHOULD-NOT-LOG"
        )               # type: ignore[method-assign]
        url = adapter._resolve_gateway_url()
        self.assertIn("access_token=SHOULD-NOT-LOG", url, "URL 本身仍照用")
        self.assertNotIn("SHOULD-NOT-LOG", self.log_text, "但绝不能进日志")

    def test_authorisation_header_shape(self):
        adapter = make_adapter()
        stub = TokenStub()
        adapter._request = stub          # type: ignore[method-assign]
        adapter.min_interval = 0.0       # type: ignore[assignment]
        adapter.send(Outbound(conversation_id="qqbot:group:G1", text="hi"))
        self.assertEqual(stub.auth_headers, [ACCESS_TOKEN])


class TestIdentityIntegration(QQBotTestCase):
    def test_conversation_ids_validate_under_identity_module(self):
        cid = QQBotAdapter.conversation_id_for("group", "B2C3")
        self.assertEqual(identity.parse_id(cid).platform, "qqbot")
        self.assertEqual(identity.parse_id(cid).local_id, "group:B2C3")
        self.assertEqual(identity.normalize(cid), cid, "已是新格式必须幂等")

    def test_foreign_ids_are_rejected_not_guessed(self):
        with self.assertRaises(InvalidConversationId):
            identity.parse_id("channel:100010")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
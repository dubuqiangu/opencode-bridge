"""最小 WebSocket 客户端（RFC 6455，纯 Python 标准库）。

为什么不用第三方库
------------------
本项目对运行时依赖的承诺是"空的 ``requirements.txt``"：只允许 Python 3.10+
标准库。而入站侧的 Slack Socket Mode（T2.1）和 Discord Gateway（T2.2）都必须先有
一个 WebSocket 客户端——Node 侧 dsh-im-gateway 可以直接用内置的全局 ``WebSocket``，
Python 侧没有等价物，装 ``websockets`` / ``websocket-client`` 都会破坏零依赖承诺。
因此这里按 RFC 6455 手写一个"够用即止"的客户端，代码量小、可审计、不进依赖树。

覆盖范围（刻意从简）
--------------------
* 仅支持 ``ws://`` / ``wss://``，路径与 query 原样透传；``wss`` 用
  ``ssl.create_default_context()`` 包装（即默认校验证书与主机名）。
* 仅支持文本帧（opcode ``0x1``）的收发；收到二进制帧（``0x2``）**直接抛错**，
  绝不静默丢弃——适配器宁可崩在可见的协议错误上，也不要悄悄少收消息。
* 不协商任何扩展（没有 permessage-deflate 等），所以 RSV1-3 非零即协议错误。
* 支持分片（continuation）聚合与控制帧（ping / pong / close），控制帧允许插在
  分片消息中间。
* 收方向是单线程的（一次只能有一个线程在 ``recv()``）；发方向内部有锁，允许
  心跳线程与业务线程并发 ``send`` / ``ping`` / ``close``。

非目标：WebSocket 服务端、子协议协商、扩展、压缩、客户端分片发送、多路复用。
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import socket
import ssl
import struct
import threading
import urllib.parse
from dataclasses import dataclass

__all__ = ["WebSocketClient", "WebSocketError", "connect"]

logger = logging.getLogger("opencode_bridge.ws")

#: RFC 6455 §1.3 固定的 GUID，用于计算 Sec-WebSocket-Accept
_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

#: 本客户端只发 / 只收文本帧
_OP_CONT = 0x0
_OP_TEXT = 0x1
_OP_BINARY = 0x2
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA

#: 控制帧 payload 上限（RFC 6455 §5.5）
_MAX_CONTROL_PAYLOAD = 125

#: HTTP 握手响应头上限（正常只有几百字节；防御性上限）
_MAX_HANDSHAKE_HEAD = 64 * 1024


class WebSocketError(Exception):
    """WebSocket 协议错误或传输层错误。

    握手失败、响应非法、RSV 位非零、收到不支持的帧类型、socket 超时、
    连接被意外重置等，一律包成本异常，便于适配器统一 ``except``。
    """


# ----------------------------------------------------------------------
# URL 解析
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class _Target:
    """握手前的连接目标。"""

    host: str
    port: int
    secure: bool
    path: str  # 形如 /socket?v=1
    host_header: str


def _parse_ws_url(url: str) -> _Target:
    """把 ``ws://host:port/path?query`` 解析成握手目标。"""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError as exc:  # pragma: no cover - urlsplit 极少抛错
        raise WebSocketError(f"URL 无法解析: {url!r}") from exc

    scheme = (parts.scheme or "").lower()
    if scheme not in ("ws", "wss"):
        raise WebSocketError(f"只支持 ws:// 与 wss://，收到: {scheme or '(空)'!r}")
    secure = scheme == "wss"

    host = parts.hostname
    if not host:
        raise WebSocketError(f"URL 缺少主机名: {url!r}")
    try:
        port = parts.port or (443 if secure else 80)
    except ValueError as exc:
        raise WebSocketError(f"URL 端口非法: {url!r}") from exc

    # 主机名里的 IPv6 字面量在 Host 头中要带方括号
    literal = f"[{host}]" if ":" in host else host
    host_header = literal if port == (443 if secure else 80) else f"{literal}:{port}"

    path = parts.path or "/"
    if not path.isascii():  # URL 里出现非 ASCII（例如中文路径）时补转义
        path = urllib.parse.quote(path, safe="/%:@&=+$,~!*'()")
    if parts.query:
        path = f"{path}?{parts.query}"
    return _Target(host=host, port=port, secure=secure, path=path, host_header=host_header)


def _accept_for(key: str) -> str:
    """``base64(sha1(key + GUID))``——服务端必须回这个值，否则不是合法握手。"""
    digest = hashlib.sha1(key.encode("ascii") + _GUID).digest()
    return base64.b64encode(digest).decode("ascii")


def _tokens(value: str) -> list[str]:
    """把 ``a, b`` 形式的逗号列表拆成小写 token。"""
    return [item.strip().lower() for item in value.split(",") if item.strip()]


# ----------------------------------------------------------------------
# 底层收发
# ----------------------------------------------------------------------
def _mask(payload: bytes, mask: bytes) -> bytes:
    """掩码变换（RFC 6455 §5.3）：与 4 字节掩码逐字节 XOR，异或两次即还原。"""
    if not payload:
        return b""
    key = mask * (len(payload) // 4 + 1)
    return bytes(a ^ b for a, b in zip(payload, key))


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    """读满 ``size`` 字节。超时 / 读失败 / 对端裸断都转成 ``WebSocketError``。

    注意：这里**不能**把超时当成"连接关闭"返回空串——上层 ``recv()`` 用 ``None``
    表示"对端发了 close 帧"，超时与关闭必须区分开。
    """
    if size <= 0:
        return b""
    buf = bytearray()
    while len(buf) < size:
        try:
            chunk = sock.recv(size - len(buf))
        except TimeoutError as exc:  # 3.10+ socket.timeout 就是 TimeoutError
            raise WebSocketError(f"读取超时（{size - len(buf)} 字节未收全）") from exc
        except OSError as exc:  # 含 ssl.SSLError
            raise WebSocketError(f"socket 读取失败: {exc}") from exc
        if not chunk:
            raise WebSocketError("连接被对端意外关闭（未收到 close 帧）")
        buf += chunk
    return bytes(buf)


def _read_http_response(sock: socket.socket) -> tuple[bytes, bytes]:
    """读 HTTP 握手响应，返回 ``(head, body_start)``（head 含 ``\\r\\n\\r\\n``）。"""
    buf = bytearray()
    while b"\r\n\r\n" not in buf:
        if len(buf) > _MAX_HANDSHAKE_HEAD:
            raise WebSocketError("握手响应头过大")
        try:
            chunk = sock.recv(4096)
        except TimeoutError as exc:
            raise WebSocketError("握手响应读取超时") from exc
        except OSError as exc:
            raise WebSocketError(f"握手响应读取失败: {exc}") from exc
        if not chunk:
            raise WebSocketError("握手阶段连接被对端关闭（未收到 101 响应）")
        buf += chunk
    head, _, rest = bytes(buf).partition(b"\r\n\r\n")
    return head + b"\r\n\r\n", rest


def _parse_response_head(head: bytes) -> tuple[int, str, dict[str, str]]:
    """解析 ``HTTP/1.1 101 Switching Protocols\\r\\nKey: Value\\r\\n...``。"""
    text = head.decode("latin-1")
    lines = text.split("\r\n")
    first = lines[0].strip().split(" ", 2)
    if len(first) < 2 or not first[0].upper().startswith("HTTP/"):
        raise WebSocketError(f"握手响应状态行非法: {lines[0]!r}")
    try:
        status = int(first[1])
    except ValueError as exc:
        raise WebSocketError(f"握手响应状态码非法: {first[1]!r}") from exc
    reason = first[2] if len(first) > 2 else ""

    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line.strip():
            continue
        name, sep, value = line.partition(":")
        if not sep:
            continue
        key = name.strip().lower()
        value = value.strip()
        # 同名头合并（Connection/Upgrade 可能是逗号列表）
        headers[key] = f"{headers[key]}, {value}" if key in headers else value
    return status, reason, headers


def _hard_close(sock: socket.socket | None) -> None:
    """直接关掉底层 socket，忽略一切错误（用于握手失败的清理路径）。"""
    if sock is None:
        return
    try:
        sock.close()
    except OSError:  # pragma: no cover - close 基本不抛
        pass


# ----------------------------------------------------------------------
# 客户端
# ----------------------------------------------------------------------
class WebSocketClient:
    """最小 WebSocket 客户端：文本消息 + 分片 + 控制帧。

    典型用法::

        with connect("wss://example.com/socket") as ws:
            ws.send("hello")
            print(ws.recv())
    """

    def __init__(
        self,
        url: str,
        *,
        timeout: float = 30.0,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._url = url
        self._timeout = timeout
        self._extra_headers = dict(headers or {})

        self._sock: socket.socket | None = None
        self._closed = True  # connect() 之前视为不可用
        self._close_sent = False
        self._close_received = False
        self._close_code: int | None = None
        self._close_reason = ""

        # 分片聚合状态：_frag_opcode 非 None 表示一条消息正在接收中
        self._frag_opcode: int | None = None
        self._frag_buf = bytearray()

        self._send_lock = threading.Lock()

    # -- 属性 ----------------------------------------------------------
    @property
    def url(self) -> str:
        return self._url

    @property
    def closed(self) -> bool:
        """连接是否已关闭（未连接也算关闭）。"""
        return self._closed

    @property
    def close_code(self) -> int | None:
        """收到对端 close 帧里的状态码（没有则 ``None``）。"""
        return self._close_code

    @property
    def close_reason(self) -> str:
        """收到对端 close 帧里的原因文本。"""
        return self._close_reason

    # -- 握手 ----------------------------------------------------------
    def connect(self) -> None:
        """阻塞到握手完成；失败一律抛 ``WebSocketError``。"""
        if self._sock is not None and not self._closed:
            raise WebSocketError("连接已建立，无需重复 connect()")

        target = _parse_ws_url(self._url)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = self._build_request(target, key)

        raw: socket.socket | None = None
        try:
            try:
                raw = socket.create_connection((target.host, target.port), timeout=self._timeout)
                raw.settimeout(self._timeout)
                sock = raw
                if target.secure:
                    # wss：默认校验证书链与主机名，不给关校验的开关
                    sock = ssl.create_default_context().wrap_socket(
                        raw, server_hostname=target.host
                    )
                sock.sendall(request)
                head, body = _read_http_response(sock)
                self._check_handshake(head, body, key)
            except WebSocketError:
                raise
            except OSError as exc:
                raise WebSocketError(f"连接 {target.host}:{target.port} 失败: {exc}") from exc
        except BaseException:
            _hard_close(raw)  # 握手没成，底层 socket 不能泄漏
            raise

        self._sock = sock
        self._closed = False
        logger.debug("websocket 握手完成 %s", target.host_header)

    def _build_request(self, target: _Target, key: str) -> bytes:
        """拼 GET 请求；协议头在先，调用方传入的 header 可覆盖（会破坏校验）。"""
        headers = {
            "Host": target.host_header,
            "Upgrade": "websocket",
            "Connection": "Upgrade",
            "Sec-WebSocket-Key": key,
            "Sec-WebSocket-Version": "13",
            "User-Agent": "opencode-bridge-ws/1",
        }
        headers.update(self._extra_headers)
        lines = [f"GET {target.path} HTTP/1.1"]
        lines += [f"{name}: {value}" for name, value in headers.items()]
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")

    def _check_handshake(self, head: bytes, body: bytes, key: str) -> None:
        """校验 101 + Upgrade + Connection + Sec-WebSocket-Accept（RFC 6455 §4.1）。"""
        status, reason, headers = _parse_response_head(head)
        if status != 101:
            snippet = body[:200].decode("utf-8", "replace").strip()
            raise WebSocketError(
                f"握手被拒：期望 HTTP 101，实际 HTTP {status} {reason}"
                + (f"（响应体: {snippet}）" if snippet else "")
            )
        if "websocket" not in _tokens(headers.get("upgrade", "")):
            raise WebSocketError(f"握手响应缺少 Upgrade: websocket（{headers.get('upgrade', '')!r}）")
        if "upgrade" not in _tokens(headers.get("connection", "")):
            raise WebSocketError(f"握手响应缺少 Connection: Upgrade（{headers.get('connection', '')!r}）")
        accept = headers.get("sec-websocket-accept", "").strip()
        expected = _accept_for(key)
        if accept != expected:
            raise WebSocketError(f"Sec-WebSocket-Accept 不匹配：期望 {expected}，收到 {accept!r}")

    # -- 发 ------------------------------------------------------------
    def send(self, text: str) -> None:
        """发送一条文本帧（客户端必须掩码，空字符串也照发）。"""
        if not isinstance(text, str):
            raise TypeError("send() 只接受 str，请自行编码二进制帧")
        self._send_frame(_OP_TEXT, text.encode("utf-8"))

    def ping(self, payload: bytes = b"") -> None:
        """主动发 ping（心跳用）。payload 最长 125 字节。"""
        if len(payload) > _MAX_CONTROL_PAYLOAD:
            raise ValueError(f"ping payload 最长 {_MAX_CONTROL_PAYLOAD} 字节")
        self._send_frame(_OP_PING, payload)

    def _send_frame(self, opcode: int, payload: bytes, *, fin: bool = True) -> None:
        """组帧并发出。客户端发出的每一帧都必须带掩码（RFC 6455 §5.3 强制）。"""
        sock = self._require_sock()
        size = len(payload)
        head = bytearray()
        head.append((0x80 if fin else 0x00) | opcode)
        flag = 0x80  # MASK 位：客户端恒为 1
        if size < 126:
            head.append(flag | size)
        elif size < 65536:
            head.append(flag | 126)
            head += size.to_bytes(2, "big")
        else:
            head.append(flag | 127)
            head += size.to_bytes(8, "big")
        mask = os.urandom(4)
        head += mask
        frame = bytes(head) + _mask(payload, mask)
        with self._send_lock:
            try:
                sock.sendall(frame)
            except OSError as exc:
                raise WebSocketError(f"发送失败: {exc}") from exc

    def _require_sock(self) -> socket.socket:
        sock = self._sock
        if sock is None or self._closed:
            raise WebSocketError("连接已关闭，无法发送")
        return sock

    # -- 收 ------------------------------------------------------------
    def recv(self) -> str | None:
        """阻塞读一条**完整**文本消息；连接关闭返回 ``None``，超时报错。

        分片会被聚合成一条消息后才返回；插在分片之间的控制帧由本方法就地处理
        （ping 自动回 pong，pong 丢弃，close 标记关闭）。
        """
        if self._sock is None or self._closed or self._close_received:
            return None
        while True:
            fin, opcode, payload = self._read_frame()
            if opcode == _OP_CLOSE:
                # close 可以出现在分片消息中间：丢弃未完成的分片，本次 recv 结束
                self._handle_close_frame(payload)
                return None
            if opcode & 0x08:  # ping / pong 插在分片中间：处理完继续读下一帧
                self._handle_control_frame(opcode, payload)
                continue
            if opcode == _OP_BINARY:
                raise WebSocketError("收到二进制帧（0x2），本客户端只支持文本消息")
            if opcode == _OP_TEXT:
                if self._frag_opcode is not None:
                    raise WebSocketError("上一条分片消息尚未结束，又收到新的 text 帧")
                if fin:
                    return _decode_text(payload)
                self._frag_opcode = _OP_TEXT
                self._frag_buf = bytearray(payload)
                continue
            if opcode == _OP_CONT:
                if self._frag_opcode is None:
                    raise WebSocketError("收到孤立的 continuation 帧（前面没有分片首帧）")
                self._frag_buf += payload
                if not fin:
                    continue
                data = bytes(self._frag_buf)
                self._frag_opcode = None
                self._frag_buf = bytearray()
                return _decode_text(data)
            raise WebSocketError(f"未知 opcode: 0x{opcode:x}")

    def _read_frame(self) -> tuple[bool, int, bytes]:
        """读一帧，返回 ``(fin, opcode, payload)``（payload 已解掩码）。"""
        sock = self._require_sock()
        head = _recv_exact(sock, 2)
        b0, b1 = head[0], head[1]
        fin = bool(b0 & 0x80)
        if b0 & 0x70:
            raise WebSocketError("RSV1-3 非零，但本客户端未协商任何扩展")
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        size = b1 & 0x7F
        if size == 126:
            size = int.from_bytes(_recv_exact(sock, 2), "big")
        elif size == 127:
            size = int.from_bytes(_recv_exact(sock, 8), "big")
            if size >= 1 << 63:
                raise WebSocketError("64 位长度最高位必须为 0（RFC 6455 §5.2）")
        if opcode & 0x08:
            if not fin:
                raise WebSocketError("控制帧不允许分片（FIN 必须为 1）")
            if size > _MAX_CONTROL_PAYLOAD:
                raise WebSocketError(f"控制帧 payload 超过 {_MAX_CONTROL_PAYLOAD} 字节")
        mask = _recv_exact(sock, 4) if masked else b""
        payload = _recv_exact(sock, size)
        if masked:
            # 服务端本不该掩码；真出现了也按 RFC 还原（宽容处理，不静默出错）
            payload = _mask(payload, mask)
        return fin, opcode, payload

    def _handle_control_frame(self, opcode: int, payload: bytes) -> None:
        """处理 ping / pong：ping 自动回 pong（可带原 payload），pong 忽略。"""
        if opcode == _OP_PING:
            self._send_frame(_OP_PONG, payload)
            return
        if opcode == _OP_PONG:
            return  # 心跳应答，标准上允许带数据；最小实现不使用
        raise WebSocketError(f"未知的控制 opcode: 0x{opcode:x}")

    def _handle_close_frame(self, payload: bytes) -> None:
        """处理对端 close：记录状态码/原因、回一个 close、把连接标记为关闭。"""
        if len(payload) == 1:
            raise WebSocketError("close 帧 payload 长度非法（恰好 1 字节）")
        if len(payload) >= 2:
            self._close_code = int.from_bytes(payload[:2], "big")
            self._close_reason = payload[2:].decode("utf-8", "replace")
        self._close_received = True
        logger.debug("收到 close 帧 code=%s reason=%r", self._close_code, self._close_reason)
        if not self._close_sent:
            # 按 RFC 回一个 close；只用状态码，长度超限就退化成空 close
            echo = payload[:2] if 0 < len(payload) <= _MAX_CONTROL_PAYLOAD else b""
            try:
                self._send_frame(_OP_CLOSE, echo)
            except WebSocketError:  # 对端可能已经半关连接，回不回都无所谓
                pass
            self._close_sent = True
        self._shutdown()

    # -- 关 ------------------------------------------------------------
    def close(self, code: int = 1000, reason: str = "") -> None:
        """发送 close 帧并关闭连接。幂等：重复调用不抛异常。"""
        if not isinstance(code, int) or not (1000 <= code <= 4999):
            raise ValueError(f"close code 非法: {code!r}（应用 1000-4999）")
        reason_bytes = reason.encode("utf-8")
        if len(reason_bytes) + 2 > _MAX_CONTROL_PAYLOAD:
            raise ValueError("close reason 过长（连同状态码不得超过 125 字节）")

        if self._sock is None:
            self._closed = True
            return
        if not self._close_sent and not self._closed:
            try:
                self._send_frame(_OP_CLOSE, struct.pack("!H", code) + reason_bytes)
            except WebSocketError as exc:  # 对端已消失就不必纠缠
                logger.debug("发送 close 帧失败（忽略）: %s", exc)
            self._close_sent = True
        self._shutdown()

    def _shutdown(self) -> None:
        """关闭底层 socket 并清空状态。"""
        sock, self._sock = self._sock, None
        self._closed = True
        self._frag_opcode = None
        self._frag_buf = bytearray()
        if sock is None:
            return
        with self._send_lock:
            try:
                sock.close()
            except OSError:  # pragma: no cover
                pass

    # -- 上下文管理 -----------------------------------------------------
    def __enter__(self) -> "WebSocketClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _decode_text(payload: bytes) -> str:
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WebSocketError(f"文本帧不是合法 UTF-8: {exc}") from exc


def connect(url: str, **kw: object) -> WebSocketClient:
    """便捷函数：构造 + 立即握手，返回已连接的 ``WebSocketClient``。"""
    client = WebSocketClient(url, **kw)  # type: ignore[arg-type]
    client.connect()
    return client
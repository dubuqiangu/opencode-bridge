"""RFC 6455 wire primitives for the minimal WebSocket client (pure stdlib).

Provenance
----------
Moved verbatim from ``ws.py`` (the opcode/GUID constants, ``WebSocketError``,
its ``URL 解析`` and ``底层收发`` sections, and ``_decode_text``).  The split
is structural only — §5: ``WebSocketClient`` / ``connect`` stay in ``ws.py``,
the stateless protocol helpers live here.  Every moved name is re-imported
by ``ws.py`` so no import path changes.

Nothing here owns a socket's lifetime and nothing logs: these are pure
helpers called by :class:`~opencode_bridge.ws.WebSocketClient`.  This module
has **no runtime import** of ``opencode_bridge.ws`` — the dependency stays
one-directional (``ws`` → ``ws_protocol``).
"""

from __future__ import annotations

import base64
import hashlib
import socket
import urllib.parse
from dataclasses import dataclass

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


def _decode_text(payload: bytes) -> str:
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WebSocketError(f"文本帧不是合法 UTF-8: {exc}") from exc

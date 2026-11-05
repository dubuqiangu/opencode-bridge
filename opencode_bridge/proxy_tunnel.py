"""经 HTTP 代理建立 CONNECT 隧道（纯标准库）。

裸 socket 的出站缝（:mod:`opencode_bridge.ws` 的 WS 客户端、
:mod:`opencode_bridge.transport.tcp_lines`）拿到的是「到目标的透明字节流」，
而 HTTP 代理要求先发一条 ``CONNECT host:port`` 并把代理的 ``200`` 应答读掉，
之后双向字节才直通。本模块把这一段收敛成一个函数，供两条缝共用。

⚠️ HTTP 响应的读取与解析**复用** :mod:`opencode_bridge.ws_protocol` 里既有的
``_read_http_response`` / ``_parse_response_head``（它们本就是通用 HTTP 原语），
⛔ 不另写一份解析（两处解析会分叉）。依赖方向是单向的：``proxy_tunnel`` →
``ws_protocol``，``ws_protocol`` 不 import 本模块。
"""

from __future__ import annotations

import socket

from .ws_protocol import WebSocketError, _parse_response_head, _read_http_response

__all__ = ["ProxyTunnelError", "establish_proxy_tunnel"]


class ProxyTunnelError(Exception):
    """CONNECT 隧道建立失败（代理拒绝、应答非法、收发失败）。

    单独一个异常类型，是为了让上层按各自协议的契约包装它（WS 层包成
    :class:`~opencode_bridge.ws_protocol.WebSocketError`，传输层直接交给重连退避），
    而不把 WS 专属的异常类型泄漏进通用传输层。
    """


def establish_proxy_tunnel(sock: socket.socket, host: str, port: int) -> bytes:
    """在已连到代理的 ``sock`` 上对 ``host:port`` 发 CONNECT，校验 200。

    成功返回**代理应答头之后多读到的字节**（正常情况下为空；调用方自行决定是否
    保留）。失败一律抛 :class:`ProxyTunnelError`。
    """
    authority = _connect_authority(host, port)
    request = (
        f"CONNECT {authority} HTTP/1.1\r\n"
        f"Host: {authority}\r\n"
        "\r\n"
    ).encode("ascii")
    try:
        sock.sendall(request)
    except OSError as exc:
        raise ProxyTunnelError(f"发送 CONNECT 到代理失败: {exc}") from exc
    try:
        head, leftover = _read_http_response(sock)
    except WebSocketError as exc:
        raise ProxyTunnelError(f"读取代理 CONNECT 应答失败: {exc}") from exc
    try:
        status, reason, _headers = _parse_response_head(head)
    except WebSocketError as exc:
        raise ProxyTunnelError(f"解析代理 CONNECT 应答失败: {exc}") from exc
    if status != 200:
        raise ProxyTunnelError(
            f"代理拒绝 CONNECT {authority}: HTTP {status} {reason}".rstrip()
        )
    return leftover


def _connect_authority(host: str, port: int) -> str:
    """CONNECT 目标的 ``host:port`` 形式；IPv6 字面量按 RFC 3986 加方括号。"""
    literal = f"[{host}]" if ":" in host else host
    return f"{literal}:{port}"

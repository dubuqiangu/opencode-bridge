"""Wire contract of the local OpenCode HTTP API.

``OpenCodeError`` / ``Endpoint`` 两个契约类型，加上 ``_safe_read_body`` /
``_force_close_stream`` 两个低层响应体/流拆除 helper —— 由客户端类与
endpoint 发现域共用。

Provenance: 2026-10-13 §5 第六刀（``opencode_client.py`` 文件刀）：从
``opencode_client.py`` 逐字移植，行为零变化。
"""

from __future__ import annotations

import base64
import socket
from dataclasses import dataclass
from typing import Any


class OpenCodeError(Exception):
    """一次 HTTP 调用失败。``status is None`` 时是**传输层**失败。

    ``request_never_sent`` = 「**可证明**请求压根没离开本机」——由
    :func:`~opencode_bridge.request_phase.request_definitely_never_sent` 从原始
    传输异常的形状判定，缺省 ``False``（保守）。

    ⚠️ 它**只**在 ``status is None`` 时有意义：有 status 就说明服务端答复过，
    那时候这个字段不再提供任何额外信息。⛔ 也别拿它当「已提交」的证据 ——
    「可能已经跑过」那一档由**缺省值**表达（与
    :func:`~opencode_bridge.inbox_recovery.mark_prompt_never_sent` 同一条纪律：
    显式标「已提交」会让缺省那个安全方向消失）。
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        body: str | None = None,
        request_never_sent: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.body = body
        self.request_never_sent = request_never_sent


@dataclass(frozen=True)
class Endpoint:
    url: str  # no trailing slash
    password: str
    username: str = "opencode"

    def __post_init__(self) -> None:
        object.__setattr__(self, "url", (self.url or "").rstrip("/"))

    def auth_header(self) -> dict[str, str]:
        raw = f"{self.username}:{self.password}".encode("utf-8")
        token = base64.b64encode(raw).decode("ascii")
        return {"Authorization": f"Basic {token}"}


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def _safe_read_body(exc: Any, limit: int = 8192) -> str:
    try:
        raw = exc.read()
    except Exception:  # pragma: no cover - best effort
        return ""
    if isinstance(raw, bytes):
        return raw[:limit].decode("utf-8", "replace")
    return str(raw)[:limit]


def _force_close_stream(resp: Any) -> None:
    """Best-effort: unblock a thread sitting in ``resp.readline()``."""
    try:
        fp = getattr(resp, "fp", None)
        raw = getattr(fp, "raw", None)
        sock = getattr(raw, "_sock", None)
        if sock is None:
            sock = getattr(resp, "sock", None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
    except Exception:
        pass
    try:
        resp.close()
    except Exception:
        pass

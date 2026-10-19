"""HTTP + SSE client for a local OpenCode service (Lane A).

Standard library only: ``urllib.request`` / ``http.client``.  All requests
carry HTTP Basic auth (username ``opencode`` by default).  The event stream
(``GET /api/event``) is consumed line-by-line — never buffered whole.

Provenance: 2026-10-13 §5 第六刀（文件刀）：endpoint 发现域与 wire 契约域逐字
移植 ``opencode_endpoint_discovery.py`` / ``opencode_wire.py``；顶部再进口
块保既有 import 路径，行为零变化。

2026-10-19 §5.1 第三刀（类手术）：SSE 事件订阅域（``subscribe``/``_iter_events``/
``_register_stream``/``_unregister_stream``/``close`` 本体）连 ``_closed``/
``_stream_lock``/``_streams`` 状态逐字迁入 ``opencode_event_stream.py`` 的
``OpenCodeEventStream``（协作者 property 逐调用转发回 ``self._client.endpoint``）；
``_STREAM_TIMEOUT``/``_RECONNECT_MIN``/``_RECONNECT_MAX`` 常量随迁（测试 patch
目标已重指向新模块）；本类只留转发。
"""

from __future__ import annotations

import http.client
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterator

from .opencode_endpoint_discovery import DEFAULT_SERVICE_URL, discover_endpoint
from .opencode_event_stream import OpenCodeEventStream
from .opencode_wire import (
    Endpoint,
    OpenCodeError,
    _safe_read_body,
)
from .request_phase import request_definitely_never_sent

__all__ = [
    "DEFAULT_SERVICE_URL",
    "Endpoint",
    "OpenCodeClient",
    "OpenCodeError",
    "discover_endpoint",
]

logger = logging.getLogger("opencode_bridge.opencode_client")

_PERMISSION_DECISIONS = frozenset({"once", "always", "reject"})

class OpenCodeClient:
    def __init__(self, endpoint: Endpoint, *, timeout: float = 30.0) -> None:
        self._endpoint = endpoint
        self._timeout = timeout
        self._event_stream = OpenCodeEventStream(self)

    @property
    def endpoint(self) -> Endpoint:
        return self._endpoint

    @property
    def closed(self) -> bool:
        return self._event_stream.closed

    # ------------------------------------------------------------------
    # basics
    # ------------------------------------------------------------------
    def info(self) -> dict:
        """GET /api/info — returns the unwrapped payload (version, pid, ...)."""
        response = self._request("GET", "/api/info")
        inner = response.get("data")
        return inner if isinstance(inner, dict) else response

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        params: dict[str, Any] | None = None,
    ) -> dict:
        url = self._endpoint.url + path
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                sep = "&" if "?" in url else "?"
                url = url + sep + urllib.parse.urlencode(clean)

        headers = dict(self._endpoint.auth_header())
        body_bytes: bytes | None
        if isinstance(body, (bytes, bytearray)):
            body_bytes = bytes(body)
        elif body is not None:
            body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        else:
            body_bytes = None

        req = urllib.request.Request(url, data=body_bytes, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            err_body = _safe_read_body(exc)
            raise OpenCodeError(
                f"{method} {path} -> HTTP {exc.code}",
                status=exc.code,
                body=err_body,
            ) from None
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            # ⚠️ 这一支把两种**相反**的处境压成了同一个异常：连接压根没建起来
            # （可证明「从未提交」⇒ 可安全重放）与请求已发出但读响应中断
            # （⇒ 结果未知、绝不能重放）。分档判据在 request_phase，而**只有这一层**
            # 拿得到原始传输异常的**形状** ⇒ 那件事必须在这里被记下来（§8 第 3 条）。
            raise OpenCodeError(
                f"{method} {path} failed: {exc}",
                request_never_sent=request_definitely_never_sent(exc),
            ) from exc

        if not raw:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise OpenCodeError(
                f"{method} {path} returned invalid JSON: {exc}",
                body=raw[:2048].decode("utf-8", "replace"),
            ) from exc
        if isinstance(parsed, dict):
            return parsed
        return {"data": parsed}

    @staticmethod
    def _unwrap_dict(response: dict) -> dict:
        inner = response.get("data")
        return inner if isinstance(inner, dict) else response

    @staticmethod
    def _unwrap_list(response: dict) -> list:
        inner = response.get("data")
        return inner if isinstance(inner, list) else []

    @staticmethod
    def _data_id(response: dict, what: str) -> str:
        inner = response.get("data")
        if isinstance(inner, dict):
            value = inner.get("id")
            if isinstance(value, str) and value:
                return value
        raise OpenCodeError(f"{what}: response has no data.id", body=json.dumps(response))

    # ------------------------------------------------------------------
    # sessions
    # ------------------------------------------------------------------
    def create_session(
        self,
        *,
        directory: str,
        title: str | None = None,
        agent: str | None = None,
        permissions: list[dict] | None = None,
    ) -> str:
        """POST /api/session -> ``data.id`` (``ses_...``)."""
        payload = {
            "title": title,
            "agent": agent,
            "location": {"directory": directory},
            "permissions": permissions if permissions is not None else [],
        }
        response = self._request("POST", "/api/session", body=payload)
        return self._data_id(response, "create_session")

    def get_session(self, session_id: str) -> dict:
        response = self._request("GET", f"/api/session/{urllib.parse.quote(session_id)}")
        return self._unwrap_dict(response)

    def list_sessions(
        self, *, limit: int = 50, directory: str | None = None
    ) -> list[dict]:
        response = self._request(
            "GET",
            "/api/session",
            params={"limit": limit, "directory": directory},
        )
        return self._unwrap_list(response)

    def delete_session(self, session_id: str) -> None:
        self._request("DELETE", f"/api/session/{urllib.parse.quote(session_id)}")
        return None

    # ------------------------------------------------------------------
    # models
    # ------------------------------------------------------------------
    def set_session_model(
        self, session_id: str, provider_id: str, model_id: str
    ) -> None:
        """POST /api/session/{id}/model -> **204 No Content**（没有可解析的响应体）。

        请求体是 ``Model.Ref`` **对象**而不是字符串：只给 ``providerID`` + ``id``
        即可，``variant`` 由服务端自己补成 ``default``（真实 2.0.22 实测 204）。
        ``_request`` 对空响应体返回 ``{}``，所以这里和 ``delete_session`` /
        ``interrupt`` 一样只发不等。
        """
        self._request(
            "POST",
            f"/api/session/{urllib.parse.quote(session_id)}/model",
            body={"model": {"providerID": provider_id, "id": model_id}},
        )
        return None

    def list_models(self) -> list[dict]:
        """GET /api/model -> the whole provider model catalog (unwrapped).

        不是分页接口：2.0.22 实测一次返回全部模型（本机 819 条）。调用方
        （:mod:`opencode_bridge.session_model`）负责缓存，别每次都用。
        """
        response = self._request("GET", "/api/model")
        return self._unwrap_list(response)

    # ------------------------------------------------------------------
    # conversation
    # ------------------------------------------------------------------
    def prompt(self, session_id: str, text: str, *, resume: bool = True) -> str:
        """POST /api/session/{id}/prompt -> ``data.id`` (``msg_...``).

        409 (session busy) raises :class:`OpenCodeError` with ``status=409``.
        """
        response = self._request(
            "POST",
            f"/api/session/{urllib.parse.quote(session_id)}/prompt",
            body={"text": text, "resume": resume},
        )
        return self._data_id(response, "prompt")

    def interrupt(self, session_id: str) -> None:
        """POST /api/session/{id}/interrupt (no body)."""
        self._request(
            "POST",
            f"/api/session/{urllib.parse.quote(session_id)}/interrupt",
            body=b"",
        )
        return None

    def reply_permission(
        self,
        session_id: str,
        request_id: str,
        decision: str,
        *,
        message: str | None = None,
    ) -> None:
        """POST /api/session/{id}/permission/{requestID}/reply (204 -> None)."""
        if decision not in _PERMISSION_DECISIONS:
            raise ValueError(
                f"invalid decision {decision!r}; expected one of "
                f"{sorted(_PERMISSION_DECISIONS)}"
            )
        self._request(
            "POST",
            f"/api/session/{urllib.parse.quote(session_id)}"
            f"/permission/{urllib.parse.quote(request_id)}/reply",
            body={"decision": decision, "message": message},
        )
        return None

    def messages(self, session_id: str, *, limit: int = 100) -> list[dict]:
        response = self._request(
            "GET",
            f"/api/session/{urllib.parse.quote(session_id)}/message",
            params={"limit": limit},
        )
        return self._unwrap_list(response)

    # ------------------------------------------------------------------
    # events
    # ------------------------------------------------------------------
    def subscribe(self, *, restart: bool = True) -> Iterator[dict]:
        """SSE event stream; delegated to :class:`OpenCodeEventStream`."""
        return self._event_stream.subscribe(restart=restart)

    def close(self) -> None:
        """Thread-safe: stop :meth:`subscribe` and abort in-flight streams."""
        self._event_stream.close()
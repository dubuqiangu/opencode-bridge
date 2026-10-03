"""HTTP + SSE client for a local OpenCode service (Lane A).

Standard library only: ``urllib.request`` / ``http.client``.  All requests
carry HTTP Basic auth (username ``opencode`` by default).  The event stream
(``GET /api/event``) is consumed line-by-line — never buffered whole.
"""

from __future__ import annotations

import base64
import http.client
import json
import logging
import os
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterator

__all__ = [
    "DEFAULT_SERVICE_URL",
    "Endpoint",
    "OpenCodeClient",
    "OpenCodeError",
    "discover_endpoint",
]

logger = logging.getLogger("opencode_bridge.opencode_client")

DEFAULT_SERVICE_URL = "http://127.0.0.1:4096"

#: socket timeout used while streaming ``GET /api/event``
_STREAM_TIMEOUT = 300.0
_RECONNECT_MIN = 0.5
_RECONNECT_MAX = 30.0

_PERMISSION_DECISIONS = frozenset({"once", "always", "reject"})


class OpenCodeError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


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
# endpoint discovery
# ----------------------------------------------------------------------
def _service_file_candidates() -> list[str]:
    candidates: list[str] = []
    env = os.environ.get("OPENCODE_SERVICE_FILE")
    if env:
        candidates.append(env)
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        candidates.append(os.path.join(xdg, "opencode", "service.json"))
    candidates.append(
        os.path.join(os.path.expanduser("~"), ".local", "state", "opencode", "service.json")
    )
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        candidates.append(os.path.join(local_appdata, "opencode", "service.json"))
    return candidates


def _read_service_file(path: str) -> dict[str, Any] | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError) as exc:
        logger.warning("cannot read service file %s: %s", path, exc)
        return None
    if not isinstance(raw, dict):
        logger.warning("service file %s is not a JSON object", path)
        return None
    return raw


def discover_endpoint(
    url: str = "", password: str = "", *, service_file: str | None = None
) -> Endpoint:
    """Resolve the OpenCode service address.

    Priority:

    1. explicit ``url`` + ``password`` (both non-empty)
    2. each of ``url`` / ``password`` independently, non-empty value wins
    3. environment ``OPENCODE_URL`` / ``OPENCODE_PASSWORD``
    4. service registration file JSON ``{"url": ..., "password": ...}``
       searched at ``$OPENCODE_SERVICE_FILE``,
       ``$XDG_STATE_HOME/opencode/service.json``,
       ``~/.local/state/opencode/service.json``,
       ``%LOCALAPPDATA%/opencode/service.json``

    Raises :class:`OpenCodeError` when nothing resolves.
    """
    final_url = (url or "").strip() or os.environ.get("OPENCODE_URL", "").strip()
    final_password = (password or "").strip() or os.environ.get(
        "OPENCODE_PASSWORD", ""
    ).strip()

    if not (final_url and final_password):
        service_data: dict[str, Any] | None = None
        if service_file is not None:
            service_data = _read_service_file(service_file)
        else:
            for candidate in _service_file_candidates():
                if os.path.isfile(candidate):
                    service_data = _read_service_file(candidate)
                    if service_data is not None:
                        break
        if service_data is not None:
            if not final_url:
                final_url = str(service_data.get("url") or "").strip()
            if not final_password:
                final_password = str(service_data.get("password") or "").strip()

    if not final_url and final_password:
        final_url = DEFAULT_SERVICE_URL

    if not final_url or not final_password:
        raise OpenCodeError("cannot resolve opencode endpoint")

    return Endpoint(final_url, final_password)


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


class OpenCodeClient:
    def __init__(self, endpoint: Endpoint, *, timeout: float = 30.0) -> None:
        self._endpoint = endpoint
        self._timeout = timeout
        self._closed = threading.Event()
        self._stream_lock = threading.Lock()
        self._streams: list[Any] = []

    @property
    def endpoint(self) -> Endpoint:
        return self._endpoint

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

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
            raise OpenCodeError(f"{method} {path} failed: {exc}") from exc

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
        """Infinite event generator backed by ``GET /api/event`` (SSE).

        - parses ``data: {json}`` lines and yields the dicts
        - ignores ``: heartbeat`` comments, blank and non-data lines
        - connection loss with ``restart=True`` -> exponential backoff
          (0.5s .. 30s, unlimited retries); with ``restart=False`` ->
          ``StopIteration``
        - HTTP non-200 -> raises :class:`OpenCodeError`
        - :meth:`close` ends the generator within ~2 seconds
        """
        delay = _RECONNECT_MIN
        while not self._closed.is_set():
            stream_error: BaseException | None = None
            events = self._iter_events()
            try:
                for event in events:
                    delay = _RECONNECT_MIN
                    yield event
            except OpenCodeError:
                raise
            except (OSError, ValueError, http.client.HTTPException) as exc:
                stream_error = exc
            finally:
                try:
                    events.close()
                except Exception:  # pragma: no cover - defensive
                    pass

            if self._closed.is_set():
                return
            if not restart:
                if stream_error is not None:
                    logger.debug("event stream ended: %s", stream_error)
                return
            if stream_error is None:
                logger.debug("event stream closed by server; reconnecting")
            else:
                logger.warning("event stream error: %s; reconnecting", stream_error)
            if self._closed.wait(delay):
                return
            delay = min(delay * 2, _RECONNECT_MAX)

    def _iter_events(self) -> Iterator[dict]:
        url = self._endpoint.url + "/api/event"
        req = urllib.request.Request(
            url, headers=self._endpoint.auth_header(), method="GET"
        )
        try:
            resp = urllib.request.urlopen(req, timeout=_STREAM_TIMEOUT)
        except urllib.error.HTTPError as exc:
            err_body = _safe_read_body(exc)
            raise OpenCodeError(
                f"GET /api/event -> HTTP {exc.code}",
                status=exc.code,
                body=err_body,
            ) from None

        if not self._register_stream(resp):
            _force_close_stream(resp)
            return

        try:
            status = getattr(resp, "status", None) or getattr(resp, "code", None)
            if status is not None and int(status) != 200:
                raise OpenCodeError(
                    f"GET /api/event -> HTTP {status}",
                    status=int(status),
                    body="",
                )
            while not self._closed.is_set():
                line = resp.readline()  # streaming: one SSE line at a time
                if not line:
                    return  # EOF
                text = line.decode("utf-8", "replace").rstrip("\r\n")
                if not text or text.startswith(":"):
                    continue  # blank line / heartbeat comment
                if not text.startswith("data:"):
                    continue  # event:/id:/retry: fields are not payloads
                payload = text[5:].lstrip()
                if not payload:
                    continue
                try:
                    event = json.loads(payload)
                except ValueError:
                    logger.warning("skipping malformed SSE frame: %.200s", payload)
                    continue
                if isinstance(event, dict):
                    yield event
                else:
                    logger.warning("skipping non-object SSE frame: %.200r", event)
        finally:
            self._unregister_stream(resp)
            try:
                resp.close()
            except Exception:  # pragma: no cover - defensive
                pass

    def _register_stream(self, resp: Any) -> bool:
        with self._stream_lock:
            if self._closed.is_set():
                return False
            self._streams.append(resp)
            return True

    def _unregister_stream(self, resp: Any) -> None:
        with self._stream_lock:
            try:
                self._streams.remove(resp)
            except ValueError:
                pass

    def close(self) -> None:
        """Thread-safe: stop :meth:`subscribe` and abort in-flight streams."""
        self._closed.set()
        with self._stream_lock:
            streams, self._streams = self._streams, []
        for resp in streams:
            _force_close_stream(resp)
        logger.debug("client closed")

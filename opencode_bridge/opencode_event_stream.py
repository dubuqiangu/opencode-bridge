"""SSE event-subscription domain for the OpenCode HTTP client (Lane A).

Holds the streaming state (``_closed``/``_stream_lock``/``_streams``) and the
``GET /api/event`` generator machinery.  Extracted from ``OpenCodeClient``
(2026-10-19 §5.1 第三刀 类手术): the client keeps thin forwarders; this class
owns the SSE subscription domain and its private state.

协作者形态（第九刀定案）：构造时注入绑定可调用被定向复跑证伪，故用 read-only
property 逐调用转发回 ``self._client.X`` —— 与手术前动态解析语义一致、方法体
零字节改动。  logger 同通道名 ``opencode_bridge.opencode_client``，日志逐字节不变。
"""

from __future__ import annotations

import http.client
import json
import logging
import threading
import urllib.error
import urllib.request
from typing import Any, Iterator

from .opencode_wire import (
    Endpoint,
    OpenCodeError,
    _force_close_stream,
    _safe_read_body,
)

logger = logging.getLogger("opencode_bridge.opencode_client")

#: socket timeout used while streaming ``GET /api/event``
_STREAM_TIMEOUT = 300.0
_RECONNECT_MIN = 0.5
_RECONNECT_MAX = 30.0


class OpenCodeEventStream:
    def __init__(self, client) -> None:
        self._client = client
        self._closed = threading.Event()
        self._stream_lock = threading.Lock()
        self._streams: list[Any] = []

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    @property
    def _endpoint(self) -> Endpoint:
        return self._client.endpoint

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

"""Lane B — Discord adapter (CONTRACT.md §2.3). Standard library only.

v1 scope: outbound messages (``POST /channels/{id}/messages`` and
``PATCH .../{message_id}``) fully work; inbound polling is a documented TODO
(``start`` only logs a warning instead of spawning a poller).
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Any, List, Optional, Tuple

from ..hooks import Hooks, MsgHandle, Outbound, SendError
from .base import Adapter, classify_http, register
from .telegram import split_text

logger = logging.getLogger("opencode_bridge.adapters.discord")

__all__ = ["DiscordAdapter"]

API_BASE = "https://discord.com/api/v10"
MESSAGE_LIMIT = 2000         # Discord message content limit
MIN_SEND_INTERVAL = 1.2      # per-conversation send/edit throttle (seconds)
SOCKET_TIMEOUT = 30.0
MAX_RETRY_AFTER = 60.0


@register("discord")
class DiscordAdapter(Adapter):
    """Discord REST adapter (outbound-first skeleton, runnable)."""

    name = "discord"
    label = "Discord"
    max_message_length = MESSAGE_LIMIT          # 消息内容上限 2000 字符
    supports_inbound = False                   # v1 仅出站（入站见 tasks.md T2.2）
    supports_inline_buttons = False            # components 未实现
    supports_media = False

    message_limit = MESSAGE_LIMIT
    min_interval = MIN_SEND_INTERVAL

    def __init__(self, config: dict, hooks: Hooks) -> None:
        super().__init__(config, hooks)
        self.bot_token: str = str(self.config.get("bot_token") or "").strip()
        self._throttle_lock = threading.Lock()
        self._last_send: dict[str, float] = {}

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        payload: dict,
        *,
        timeout: float = SOCKET_TIMEOUT,
    ) -> Tuple[int, dict]:
        """Call ``{API_BASE}/{path}``. Never raises; returns (status, body)."""
        url = f"{API_BASE}/{path.lstrip('/')}"
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bot {self.bot_token}",
                "User-Agent": "opencode-bridge (discord, 1.0)",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = getattr(resp, "status", 200) or 200
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                raw = exc.read()
            except Exception:
                raw = b""
        except Exception as exc:
            logger.warning("discord: transport error on %s: %s", path, exc)
            return 0, {"ok": False, "message": f"transport error: {exc}"}
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            return status, {"ok": False, "message": f"non-JSON response (HTTP {status})"}
        if not isinstance(data, dict):
            return status, {"ok": False, "message": "unexpected payload"}
        if status >= 400 and "code" not in data:
            data.setdefault("ok", False)
        return status, data

    def _throttle(self, conversation_id: str) -> None:
        interval = getattr(self, "min_interval", MIN_SEND_INTERVAL)
        while True:
            with self._throttle_lock:
                last = self._last_send.get(conversation_id)
                now = time.monotonic()
                if last is None or (now - last) >= interval:
                    self._last_send[conversation_id] = now
                    return
                wait = interval - (now - last)
            if self._stop_event.wait(wait):
                return

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        if not self.bot_token:
            logger.warning("discord: bot_token missing; adapter not started")
            return
        # v1: inbound polling (GET /channels/{id}/messages incremental
        # replay) is intentionally not implemented yet.
        logger.warning(
            "discord: inbound polling not implemented in v1 (TODO); "
            "outbound send/edit only"
        )

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------
    @staticmethod
    def _channel_id(conversation_id: str) -> Optional[str]:
        raw = conversation_id
        if raw.startswith("channel:"):
            raw = raw[len("channel:"):]
        return raw or None

    @staticmethod
    def _conversation_id(channel_id: Any) -> str:
        return f"channel:{channel_id}"

    def _send_chunk(
        self, channel: str, content: str, conversation_id: str
    ) -> Tuple[int, dict]:
        """POST one message chunk, retrying once on 429 (retry_after)."""
        self._throttle(conversation_id)
        status, data = self._request(
            "POST", f"channels/{channel}/messages", {"content": content}
        )
        if status == 429:
            delay = data.get("retry_after")
            if isinstance(delay, (int, float)) and not isinstance(delay, bool):
                delay = min(float(delay), MAX_RETRY_AFTER)
                logger.warning(
                    "discord: rate limited, sleeping %.1fs and retrying once", delay
                )
                time.sleep(delay)
                self._throttle(conversation_id)
                status, data = self._request(
                    "POST", f"channels/{channel}/messages", {"content": content}
                )
        return status, data

    def send(self, out: Outbound) -> MsgHandle | None:
        channel = self._channel_id(out.conversation_id)
        if not channel:
            logger.warning("discord: bad conversation_id %r", out.conversation_id)
            self._note_send_failure(SendError.BAD_FORMAT, "bad conversation_id")
            return None
        if not out.text:
            logger.warning("discord: refusing to send empty text")
            self._note_send_failure(SendError.BAD_FORMAT, "empty text")
            return None
        chunks: List[str] = split_text(out.text, self.message_limit)
        if len(chunks) > 1:
            logger.info(
                "discord: splitting outbound message into %d chunks", len(chunks)
            )
        handle: MsgHandle | None = None
        for chunk in chunks:
            status, data = self._send_chunk(channel, chunk, out.conversation_id)
            if status < 200 or status >= 300 or "id" not in data:
                detail = str(data.get("message") or data.get("code") or "")
                logger.warning(
                    "discord: send failed (HTTP %s): %s",
                    status,
                    data.get("message") or data.get("code"),
                )
                # Discord 在 429 的响应体里给 retry_after（秒，float）
                retry_after = data.get("retry_after")
                self._note_send_failure(
                    classify_http(status, detail),
                    detail,
                    retry_after=float(retry_after) if isinstance(retry_after, (int, float)) else None,
                )
                return handle if handle is not None else None
            handle = MsgHandle(
                conversation_id=out.conversation_id,
                message_id=str(data.get("id")),
                platform=self.name,
            )
        return handle

    def edit(self, handle: MsgHandle, out: Outbound) -> bool:
        channel = self._channel_id(handle.conversation_id)
        if not channel or not handle.message_id:
            logger.warning("discord: bad handle %r", handle)
            return False
        if not out.text:
            logger.warning("discord: refusing to edit with empty text")
            return False
        self._throttle(handle.conversation_id)
        status, data = self._request(
            "PATCH",
            f"channels/{channel}/messages/{handle.message_id}",
            {"content": out.text},
        )
        if status == 429:
            delay = data.get("retry_after")
            if isinstance(delay, (int, float)) and not isinstance(delay, bool):
                time.sleep(min(float(delay), MAX_RETRY_AFTER))
                self._throttle(handle.conversation_id)
                status, data = self._request(
                    "PATCH",
                    f"channels/{channel}/messages/{handle.message_id}",
                    {"content": out.text},
                )
        if status < 200 or status >= 300:
            logger.warning(
                "discord: edit failed (HTTP %s): %s",
                status,
                data.get("message") or data.get("code"),
            )
            return False
        return True

    def answer(self, query_id: str, text: str = "") -> None:
        return None  # Discord components: deferred via interactions (TODO v2)

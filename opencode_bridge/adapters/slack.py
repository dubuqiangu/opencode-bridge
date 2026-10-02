"""Lane B — Slack adapter (CONTRACT.md §2.3). Standard library only.

v1 scope: outbound messages (``chat.postMessage`` / ``chat.update``) fully
work; inbound polling is a documented TODO (``start`` only logs a warning
about it instead of spawning a poller).
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Any, List, Optional, Tuple

from ..hooks import Hooks, MsgHandle, Outbound
from .base import Adapter, register
from .telegram import split_text

logger = logging.getLogger("opencode_bridge.adapters.slack")

__all__ = ["SlackAdapter"]

API_BASE = "https://slack.com/api"
MESSAGE_LIMIT = 40000        # Slack hard-truncates beyond ~40k characters
MIN_SEND_INTERVAL = 1.2      # per-conversation send/edit throttle (seconds)
SOCKET_TIMEOUT = 30.0
MAX_RETRY_AFTER = 60.0


@register("slack")
class SlackAdapter(Adapter):
    """Slack Web API adapter (outbound-first skeleton, runnable)."""

    name = "slack"
    label = "Slack"
    max_message_length = MESSAGE_LIMIT          # chat.postMessage 文本上限 40000
    supports_inbound = False                   # v1 仅出站（入站见 tasks.md T2.1）
    supports_inline_buttons = False            # blocks 未实现
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
        self, method: str, path: str, payload: dict, *, timeout: float = SOCKET_TIMEOUT
    ) -> Tuple[int, dict]:
        """POST ``{API_BASE}/{path}`` with a Bearer token. Never raises."""
        url = f"{API_BASE}/{path.lstrip('/')}"
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {self.bot_token}",
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
            logger.warning("slack: transport error on %s: %s", path, exc)
            return 0, {"ok": False, "error": f"transport error: {exc}"}
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return status, {"ok": False, "error": f"non-JSON response (HTTP {status})"}
        if not isinstance(data, dict):
            return status, {"ok": False, "error": "unexpected payload"}
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
            logger.warning("slack: bot_token missing; adapter not started")
            return
        # v1: inbound polling (conversations.list + conversations.history
        # incremental replay) is intentionally not implemented yet.
        logger.warning(
            "slack: inbound polling not implemented in v1 (TODO); "
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

    def send(self, out: Outbound) -> MsgHandle | None:
        channel = self._channel_id(out.conversation_id)
        if not channel:
            logger.warning("slack: bad conversation_id %r", out.conversation_id)
            return None
        if not out.text:
            logger.warning("slack: refusing to send empty text")
            return None
        chunks: List[str] = split_text(out.text, self.message_limit)
        if len(chunks) > 1:
            logger.info("slack: splitting outbound message into %d chunks", len(chunks))
        handle: MsgHandle | None = None
        for chunk in chunks:
            self._throttle(out.conversation_id)
            status, data = self._request(
                "POST", "chat.postMessage", {"channel": channel, "text": chunk}
            )
            if not data.get("ok"):
                logger.warning(
                    "slack: chat.postMessage failed (HTTP %s): %s",
                    status,
                    data.get("error"),
                )
                return handle if handle is not None else None
            handle = MsgHandle(
                conversation_id=out.conversation_id,
                message_id=str(data.get("ts", "")),
                platform=self.name,
            )
        return handle

    def edit(self, handle: MsgHandle, out: Outbound) -> bool:
        channel = self._channel_id(handle.conversation_id)
        if not channel or not handle.message_id:
            logger.warning("slack: bad handle %r", handle)
            return False
        if not out.text:
            logger.warning("slack: refusing to edit with empty text")
            return False
        self._throttle(handle.conversation_id)
        status, data = self._request(
            "POST",
            "chat.update",
            {"channel": channel, "ts": handle.message_id, "text": out.text},
        )
        if not data.get("ok"):
            logger.warning(
                "slack: chat.update failed (HTTP %s): %s", status, data.get("error")
            )
            return False
        return True

    def answer(self, query_id: str, text: str = "") -> None:
        return None  # Slack has no callback-query ack equivalent in v1

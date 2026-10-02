"""Lane B — Telegram Bot API adapter (CONTRACT.md §2.2). Standard library only.

Transport is plain ``http.client.HTTPSConnection`` with JSON request bodies,
which keeps every call behind a single overridable method ``_post`` so tests
can monkeypatch it without touching the network.
"""

from __future__ import annotations

import http.client
import json
import logging
import threading
import time
from typing import Any, List, Optional

from ..hooks import Button, Hooks, Inbound, MsgHandle, Outbound, SendError
from ..split import split_text  # 统一分片实现（T1.4b），此处再导出保持向后兼容
from .base import Adapter, classify_http, register

logger = logging.getLogger("opencode_bridge.adapters.telegram")

__all__ = ["TelegramAdapter", "split_text"]

API_HOST = "api.telegram.org"
API_PORT = 443
MESSAGE_LIMIT = 4096          # Telegram max message length in characters
CALLBACK_DATA_LIMIT = 64      # inline callback_data limit in bytes
MIN_SEND_INTERVAL = 1.2       # per-conversation send/edit throttle (seconds)
BACKOFF_INTERVAL = 2.0        # interval used after a 429 (seconds)
POLL_LONG_TIMEOUT = 25        # getUpdates long-poll seconds
POLL_SOCKET_TIMEOUT = 40.0    # socket timeout must be > POLL_LONG_TIMEOUT
DEFAULT_SOCKET_TIMEOUT = 30.0
MAX_RETRY_AFTER = 60.0        # cap for Telegram 429 retry_after sleeps


def _retry_after(data: Any) -> Optional[float]:
    """Extract a 429 ``retry_after`` delay from a Telegram response body."""
    if not isinstance(data, dict):
        return None
    params = data.get("parameters")
    delay: Any = None
    if isinstance(params, dict):
        delay = params.get("retry_after")
    if delay is None and data.get("error_code") == 429:
        delay = 1
    if isinstance(delay, bool) or not isinstance(delay, (int, float)):
        return None
    if delay < 0:
        return None
    return min(float(delay), MAX_RETRY_AFTER)


@register("telegram")
class TelegramAdapter(Adapter):
    """Long-polling ``getUpdates`` Telegram adapter."""

    name = "telegram"
    label = "Telegram"
    max_message_length = MESSAGE_LIMIT          # Bot API: 4096 字符
    supports_inbound = True
    supports_inline_buttons = True
    supports_media = True

    # Class-level knobs (tests may override them on the instance).
    message_limit = MESSAGE_LIMIT
    min_interval = MIN_SEND_INTERVAL
    backoff_interval = BACKOFF_INTERVAL

    def __init__(self, config: dict, hooks: Hooks) -> None:
        super().__init__(config, hooks)
        self.bot_token: str = str(self.config.get("bot_token") or "").strip()
        # allowed_chat_ids 已由基类 _init_access() 统一解析（T1.2）
        self.poll_long_timeout = int(
            self.config.get("poll_timeout") or POLL_LONG_TIMEOUT
        )
        self._offset = 0
        self._api_lock = threading.Lock()          # serializes _post calls
        self._throttle_lock = threading.Lock()     # guards _last_send
        self._last_send: dict[str, float] = {}

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------
    def _post(
        self, method: str, payload: Optional[dict] = None, *, timeout: Optional[float] = None
    ) -> dict:
        """POST ``https://api.telegram.org/bot<token>/<method>`` (JSON body).

        Always returns a dict; network / decode failures are mapped to
        ``{"ok": False, "error_code": ..., "description": ...}`` so callers
        never see an exception from this layer unless they want to.
        """
        body = json.dumps(payload if payload is not None else {}).encode("utf-8")
        path = f"/bot{self.bot_token}/{method}"
        sock_timeout = timeout if timeout is not None else DEFAULT_SOCKET_TIMEOUT
        status = 0
        raw = b""
        conn = http.client.HTTPSConnection(API_HOST, API_PORT, timeout=sock_timeout)
        try:
            conn.request(
                "POST",
                path,
                body=body,
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Accept": "application/json",
                },
            )
            resp = conn.getresponse()
            status = resp.status
            raw = resp.read()
        except Exception as exc:
            return {
                "ok": False,
                "error_code": status or 0,
                "description": f"transport error: {exc}",
            }
        finally:
            try:
                conn.close()
            except Exception:
                pass
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return {
                "ok": False,
                "error_code": status,
                "description": f"non-JSON response (HTTP {status}): {raw[:200]!r}",
            }
        if not isinstance(data, dict):
            return {"ok": False, "error_code": status, "description": "unexpected payload"}
        if "error_code" not in data and status >= 400:
            data["error_code"] = status
        return data

    def _api(
        self, conversation_id: str, method: str, payload: dict, *, timeout: Optional[float] = None
    ) -> dict:
        """Throttled + 429-retried wrapper around ``_post``.

        Returns the final response dict; on transport failure returns
        ``{"ok": False}``. Never raises.
        """
        self._throttle(conversation_id)
        try:
            with self._api_lock:
                data = self._post(method, payload, timeout=timeout)
        except Exception:
            logger.exception("telegram: %s request failed", method)
            return {"ok": False, "error_code": 0, "description": "transport error"}
        if isinstance(data, dict) and data.get("ok") is False:
            delay = _retry_after(data)
            if delay is not None:
                logger.warning(
                    "telegram: %s rate-limited, sleeping %.1fs and retrying once",
                    method,
                    delay,
                )
                self._mark_backoff(conversation_id)
                time.sleep(delay)
                try:
                    with self._api_lock:
                        data = self._post(method, payload, timeout=timeout)
                except Exception:
                    logger.exception("telegram: %s retry failed", method)
                    return {"ok": False, "error_code": 0, "description": "transport error"}
        # No generic failure logging here: callers decide which errors are
        # silent (e.g. editMessageText "message is not modified").
        return data if isinstance(data, dict) else {"ok": False}

    def _throttle(self, conversation_id: str) -> None:
        """Enforce a minimum interval between send/edit per conversation."""
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

    def _mark_backoff(self, conversation_id: str) -> None:
        """After a 429, push the next allowed send further out (2s)."""
        backoff = getattr(self, "backoff_interval", BACKOFF_INTERVAL)
        with self._throttle_lock:
            self._last_send[conversation_id] = time.monotonic() + backoff - self.min_interval

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Verify the token, flush pending updates, spawn the poller.

        Missing / invalid token: log a warning and return (never raises).
        """
        if not self.bot_token:
            logger.warning("telegram: bot_token missing; adapter not started")
            return
        try:
            me = self._post("getMe", {}, timeout=8.0)
        except Exception as exc:
            logger.warning("telegram: getMe failed (%s); adapter not started", exc)
            return
        if not isinstance(me, dict) or me.get("ok") is not True:
            if isinstance(me, dict):
                code = me.get("error_code")
                desc = me.get("description")
            else:
                code = "?"
                desc = repr(me)
            logger.warning(
                "telegram: getMe failed (code=%s): %s; adapter not started",
                code,
                desc,
            )
            return
        try:
            self._flush_pending()
        except Exception:
            logger.exception("telegram: failed to flush pending updates")
        self._stop_event.clear()
        thread = threading.Thread(
            target=self._poll_loop, name="telegram-poll", daemon=True
        )
        self._thread = thread
        thread.start()
        logger.info("telegram: polling started (offset=%s)", self._offset)

    def _flush_pending(self) -> None:
        """Drop history: read the last update_id and skip everything before it."""
        data = self._post(
            "getUpdates", {"limit": 1, "offset": -1}, timeout=DEFAULT_SOCKET_TIMEOUT
        )
        if not isinstance(data, dict) or data.get("ok") is not True:
            logger.warning(
                "telegram: could not flush pending updates: %s",
                (data or {}).get("description"),
            )
            return
        results = data.get("result") or []
        if results:
            try:
                self._offset = int(results[-1].get("update_id")) + 1
            except (TypeError, ValueError):
                logger.warning("telegram: unexpected flush payload: %r", results[-1])

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------
    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                ok = self._poll_once()
            except Exception:
                logger.exception("telegram: poll cycle failed")
                ok = False
            if not ok:
                self._stop_event.wait(2.0)

    def _poll_once(self) -> bool:
        """One ``getUpdates`` round. Returns True when the API call succeeded."""
        payload = {
            "timeout": self.poll_long_timeout,
            "offset": self._offset,
            "allowed_updates": ["message", "callback_query"],
            "limit": 100,
        }
        data = self._post(
            "getUpdates", payload, timeout=self.poll_long_timeout + 15.0
        )
        if not isinstance(data, dict) or data.get("ok") is not True:
            logger.warning(
                "telegram: getUpdates failed (code=%s): %s",
                (data or {}).get("error_code"),
                (data or {}).get("description"),
            )
            return False
        updates = data.get("result") or []
        for update in updates:
            if not isinstance(update, dict):
                continue
            try:
                self._advance_offset(update)
                self._dispatch_update(update)
            except Exception:
                logger.exception("telegram: failed to handle update %r", update)
        if not updates and not self._stop_event.is_set():
            # Defensive pacing when a patched/short-poll response returns fast.
            self._stop_event.wait(0.05)
        return True

    def _advance_offset(self, update: dict) -> None:
        try:
            update_id = int(update.get("update_id"))
        except (TypeError, ValueError):
            return
        if update_id + 1 > self._offset:
            self._offset = update_id + 1

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------
    def _dispatch_update(self, update: dict) -> None:
        cq = update.get("callback_query")
        if isinstance(cq, dict):
            self._handle_callback(cq)
            return
        # edited_message / channel_post / edited_channel_post have no
        # "message" key here -> they simply fall through and are ignored.
        message = update.get("message")
        if not isinstance(message, dict):
            return
        text = message.get("text")
        if not isinstance(text, str) or text == "":
            # Stickers, photos, voice notes, caption-only media: ignored.
            return
        chat = message.get("chat")
        if not isinstance(chat, dict) or chat.get("id") is None:
            return
        conversation_id = self._conversation_id(chat.get("id"))
        if not self._allowed(chat.get("id")):
            logger.info("telegram: dropped message from non-whitelisted chat %s", chat.get("id"))
            return
        from_user = message.get("from") or {}
        user_id = str(from_user.get("id")) if isinstance(from_user, dict) else None
        inbound = Inbound(
            conversation_id=conversation_id,
            text=text,
            kind="text",
            user_id=user_id,
            message_id=str(message.get("message_id"))
            if message.get("message_id") is not None
            else None,
            platform=self.name,
            raw=message,
        )
        self.hooks.on_inbound(inbound)

    def _handle_callback(self, cq: dict) -> None:
        data = cq.get("data")
        query_id = cq.get("id")
        chat_id = None
        message = cq.get("message")
        if isinstance(message, dict):
            chat = message.get("chat")
            if isinstance(chat, dict):
                chat_id = chat.get("id")
        if chat_id is None:
            from_user = cq.get("from")
            if isinstance(from_user, dict):
                chat_id = from_user.get("id")
        if chat_id is None or not self._allowed(chat_id):
            if chat_id is not None:
                logger.info(
                    "telegram: dropped callback from non-whitelisted chat %s", chat_id
                )
            else:
                logger.warning("telegram: callback without chat context: %r", cq)
            return
        conversation_id = self._conversation_id(chat_id)
        payload = data if isinstance(data, str) else ""
        # 1) push the event into the core, 2) notify the core, 3) ack Telegram.
        try:
            from_user = cq.get("from")
            user_id = (
                str(from_user.get("id")) if isinstance(from_user, dict) else None
            )
            inbound = Inbound(
                conversation_id=conversation_id,
                text=payload,
                kind="callback",
                user_id=user_id,
                message_id=str(message.get("message_id"))
                if isinstance(message, dict) and message.get("message_id") is not None
                else None,
                callback_query_id=str(query_id) if query_id is not None else None,
                platform=self.name,
                raw=cq,
            )
            try:
                self.hooks.on_inbound(inbound)
            except Exception:
                logger.exception("telegram: on_inbound hook failed for callback")
        finally:
            try:
                if query_id is not None:
                    self.hooks.on_callback(conversation_id, payload, str(query_id))
            except Exception:
                logger.exception("telegram: on_callback hook failed")
            try:
                if query_id is not None:
                    self.answer(str(query_id))
            except Exception:
                logger.exception("telegram: answer failed for query %s", query_id)

    def answer(self, query_id: str, text: str = "") -> None:
        """Acknowledge a callback query (``answerCallbackQuery``). Never raises."""
        if not query_id:
            return
        payload: dict = {"callback_query_id": query_id}
        if text:
            payload["text"] = text
        try:
            data = self._post("answerCallbackQuery", payload, timeout=DEFAULT_SOCKET_TIMEOUT)
        except Exception:
            logger.exception("telegram: answerCallbackQuery failed")
            return
        if isinstance(data, dict) and data.get("ok") is not True:
            logger.debug(
                "telegram: answerCallbackQuery not ok: %s", data.get("description")
            )

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------
    @staticmethod
    def _conversation_id(chat_id: Any) -> str:
        return f"chat:{chat_id}"

    @staticmethod
    def _chat_id(conversation_id: str) -> Optional[int]:
        raw = conversation_id
        if raw.startswith("chat:"):
            raw = raw[len("chat:"):]
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def _allowed(self, chat_id: Any) -> bool:
        """入站闸门：委托基类统一判定（T1.2，语义与旧实现一致）。"""
        return self.admits(chat_id)

    def send(self, out: Outbound) -> MsgHandle | None:
        """Send text (split at 4096); returns the handle of the LAST chunk."""
        chat_id = self._chat_id(out.conversation_id)
        if chat_id is None:
            logger.warning("telegram: bad conversation_id %r", out.conversation_id)
            self._note_send_failure(SendError.BAD_FORMAT, "bad conversation_id")
            return None
        if not out.text:
            logger.warning("telegram: refusing to send empty text")
            self._note_send_failure(SendError.BAD_FORMAT, "empty text")
            return None
        # prefix_fmt="" 保持既有出站行为（分段不加「（i/n）」前缀，且
        # "".join(chunks) == text）。前缀编号是 split.py 的可选能力，
        # 是否默认开启见 tasks.md T1.4 的后续决策。
        chunks = split_text(out.text, self.message_limit, prefix_fmt="")
        if len(chunks) > 1:
            logger.info(
                "telegram: splitting outbound message into %d chunks", len(chunks)
            )
        handle: MsgHandle | None = None
        for chunk in chunks:
            payload = {
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": True,
            }
            data = self._api(out.conversation_id, "sendMessage", payload)
            if not isinstance(data, dict) or data.get("ok") is not True:
                code = data.get("error_code") if isinstance(data, dict) else 0
                desc = (data.get("description") if isinstance(data, dict) else "") or ""
                logger.warning(
                    "telegram: sendMessage failed (code=%s): %s",
                    code if code is not None else "?",
                    desc or data,
                )
                params = data.get("parameters") if isinstance(data, dict) else None
                retry_after = None
                if isinstance(params, dict):
                    retry_after = params.get("retry_after")
                try:
                    self._note_send_failure(
                        classify_http(int(code or 0), str(desc)),
                        str(desc),
                        retry_after=float(retry_after) if retry_after else None,
                    )
                except (TypeError, ValueError):
                    self._note_send_failure(SendError.UNKNOWN, str(desc))
                if handle is None:
                    return None
                return handle  # partial send: keep the last good handle
            result = data.get("result") or {}
            message_id = result.get("message_id")
            if message_id is None:
                return handle
            handle = MsgHandle(
                conversation_id=out.conversation_id,
                message_id=str(message_id),
                platform=self.name,
            )
        return handle

    def edit(self, handle: MsgHandle, out: Outbound) -> bool:
        """Edit a message (with optional single-column inline keyboard).

        Raises ``ValueError`` when the text exceeds 4096 characters — per
        CONTRACT.md §2.2 the caller is responsible for truncating.
        """
        if len(out.text) > self.message_limit:
            raise ValueError(
                f"telegram edit text too long: {len(out.text)} > {self.message_limit}"
            )
        if not out.text:
            logger.warning("telegram: refusing to edit with empty text")
            return False
        chat_id = self._chat_id(handle.conversation_id)
        if chat_id is None:
            logger.warning(
                "telegram: bad conversation_id %r in handle", handle.conversation_id
            )
            return False
        try:
            message_id = int(handle.message_id)
        except (TypeError, ValueError):
            logger.warning("telegram: bad message_id %r in handle", handle.message_id)
            return False
        payload: dict = {"chat_id": chat_id, "message_id": message_id, "text": out.text}
        if out.buttons:
            # Single column: one button per row (CONTRACT.md §2.2).
            payload["inline_keyboard"] = [[_button_row(b)] for b in out.buttons]
        data = self._api(handle.conversation_id, "editMessageText", payload)
        if not isinstance(data, dict):
            return False
        if data.get("ok") is True:
            return True
        description = str(data.get("description") or "")
        if "message is not modified" in description:
            return False  # silent: identical content is not an error
        code = data.get("error_code")
        if isinstance(code, int) and 400 <= code < 500:
            logger.warning(
                "telegram: editMessageText rejected (HTTP %s): %s", code, description
            )
        else:
            logger.warning(
                "telegram: editMessageText failed (code=%s): %s", code, description
            )
        return False


def _button_row(button: Button) -> dict:
    """Serialize a Button to a Telegram inline keyboard button (<= 64 bytes)."""
    data = button.data.encode("utf-8")[:CALLBACK_DATA_LIMIT].decode(
        "utf-8", errors="ignore"
    )
    return {"text": button.label, "callback_data": data}

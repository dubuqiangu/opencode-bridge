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

from ..hooks import Hooks, Inbound, MsgHandle, Outbound, SendError
from ..split import split_text
from .base import Adapter, classify_http, register

logger = logging.getLogger("opencode_bridge.adapters.slack")

__all__ = ["SlackAdapter"]

API_BASE = "https://slack.com/api"
MESSAGE_LIMIT = 40000        # Slack hard-truncates beyond ~40k characters
MIN_SEND_INTERVAL = 1.2      # per-conversation send/edit throttle (seconds)
SOCKET_TIMEOUT = 30.0
MAX_RETRY_AFTER = 60.0
RECONNECT_DELAY = 3.0        # Socket Mode 断开后的重连间隔（秒）


def _classify_slack_error(status: int, error: str) -> SendError:
    """Slack 用 ``ok:false`` + 字符串 ``error`` 报失败（多为 HTTP 200），
    所以先按 Slack 官方错误串判定，再回落到通用 HTTP 分类（T1.3）。"""
    low = (error or "").lower()
    if status == 429 or "rate_limited" in low or "ratelimited" in low:
        return SendError.RATE_LIMITED
    if "not_found" in low or "is_archived" in low:
        return SendError.NOT_FOUND
    if (
        "invalid_auth" in low
        or "not_authed" in low
        or "token_revoked" in low
        or "missing_scope" in low
        or "no_permission" in low
        or "forbidden" in low
    ):
        return SendError.FORBIDDEN
    if "no_text" in low or "invalid_arg" in low or "too_large" in low or "too_long" in low:
        return SendError.TOO_LONG if ("too_large" in low or "too_long" in low) else SendError.BAD_FORMAT
    return classify_http(status, error)


@register("slack")
class SlackAdapter(Adapter):
    """Slack Web API adapter (outbound-first skeleton, runnable)."""

    name = "slack"
    label = "Slack"
    max_message_length = MESSAGE_LIMIT          # chat.postMessage 文本上限 40000
    supports_inbound = True                    # Socket Mode 入站（需另配 app_token）
    supports_inline_buttons = False            # blocks 未实现
    supports_media = False
    # 入站必需 app_token：缺它会静默降级为"只发出站"，所以必须声明出来，
    # 让 --status / --setup --json 不会把这种情况报成"已配置"。
    required_tokens = ("bot_token", "app_token")

    message_limit = MESSAGE_LIMIT
    min_interval = MIN_SEND_INTERVAL

    def __init__(self, config: dict, hooks: Hooks) -> None:
        super().__init__(config, hooks)
        self.bot_token: str = str(self.config.get("bot_token") or "").strip()
        # Socket Mode 入站需要 app-level token（xapp-…）；缺它时仍可只发出站。
        self.app_token: str = str(self.config.get("app_token") or "").strip()
        self._ws_factory = None      # 测试注入点：Callable[[str], WebSocketClient]
        self._ws = None              # 当前连接，stop() 时关掉
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
        """启动入站（Socket Mode）。缺 app_token 时降级为"只发出站"。"""
        if not self.bot_token:
            logger.warning("slack: bot_token missing; adapter not started")
            return
        if not self.app_token:
            logger.warning(
                "slack: app_token (xapp-) missing; inbound disabled, outbound only"
            )
            return
        thread = threading.Thread(
            target=self._inbound_loop, name="slack-inbound", daemon=True
        )
        self._thread = thread
        thread.start()

    # ------------------------------------------------------------------
    # Inbound (Socket Mode)
    # ------------------------------------------------------------------
    def _open_socket_url(self) -> str:
        """``apps.connections.open`` → 本次连接用的 WSS URL。"""
        status, data = self._request("POST", "apps.connections.open", {})
        if not data.get("ok") or not data.get("url"):
            err = str(data.get("error") or "no url")
            raise RuntimeError(f"apps.connections.open failed: {err}")
        return str(data["url"])

    def _make_ws(self, url: str):
        """建 WS 连接；``_ws_factory`` 为测试注入点，生产走标准库实现。"""
        factory = self._ws_factory
        if factory is None:
            from ..ws import connect as factory  # 延迟导入：未装 WebSocket 时也能只发
        return factory(url)

    def _inbound_loop(self) -> None:
        """连接 → 收包 → 断开重连，直到 stop。"""
        while not self._stop_event.is_set():
            ws = None
            try:
                url = self._open_socket_url()
                ws = self._make_ws(url)
                self._ws = ws
                logger.info("slack: Socket Mode connected")
                while not self._stop_event.is_set():
                    raw = ws.recv()
                    if raw is None:
                        break  # 对端关闭 → 走重连
                    if not self._handle_envelope(ws, raw):
                        break  # 服务端要求重连（WSS URL 过期）→ 走重连
            except Exception as exc:
                logger.warning("slack: inbound error: %s", exc)
            finally:
                self._ws = None
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
            if self._stop_event.wait(RECONNECT_DELAY):
                break

    def _handle_envelope(self, ws, raw: str) -> bool:
        """处理一个 envelope，返回 ``False`` 表示应立即重连。

        **先 ack 再过滤**（漏 ack 会让 Slack 无限重发）。

        Socket Mode 的 WSS URL 约 1 小时过期。Slack 会在过期前发
        ``{"type": "disconnect", "reason": "warning"}``，也可能不给预警直接发
        ``reason: "refresh_requested"``。所以这里**主动**返回重连信号，而不是
        被动等对端关闭 socket —— 后者依赖对端行为，不够稳。
        """
        try:
            env = json.loads(raw)
        except Exception:
            return True
        if not isinstance(env, dict):
            return True
        env_type = str(env.get("type") or "")
        envelope_id = env.get("envelope_id")
        if envelope_id:
            try:
                ws.send(json.dumps({"envelope_id": envelope_id}))
            except Exception as exc:
                logger.warning("slack: ack failed for %s: %s", envelope_id, exc)
        if env_type == "disconnect":
            # 不重连就会在 URL 过期后收不到任何事件；重连时重新取 URL 即可。
            logger.info(
                "slack: server requested reconnect (reason=%s)",
                env.get("reason") or "?",
            )
            return False
        if env_type == "hello":
            return True
        payload = env.get("payload")
        event = payload.get("event") if isinstance(payload, dict) else None
        if not isinstance(event, dict) or event.get("type") != "message":
            return True
        # bot 自己的消息也会以事件形式回到我们（否则会无限回环）；官方指定的
        # 判别字段是 bot_id / bot_profile，不是 subtype。
        if event.get("bot_id") or event.get("bot_profile"):
            return True
        if event.get("subtype"):
            return True  # message_changed / channel_join / thread_broadcast 等噪声
        text = str(event.get("text") or "")
        channel = str(event.get("channel") or "")
        if not text or not channel:
            return True
        # 授权闸门必须在最前：未授权者的消息不许进入上层（否则能用命令/审批字绕过）
        if not self.admits(channel):
            logger.info("slack: dropping message from non-whitelisted channel %s", channel)
            return True
        try:
            self.hooks.on_inbound(
                Inbound(
                    conversation_id=self._conversation_id(channel),
                    text=text,
                    kind="text",
                    user_id=str(event.get("user") or "") or None,
                    message_id=str(event.get("ts") or "") or None,
                    platform=self.name,
                    raw=event,
                )
            )
        except Exception as exc:
            logger.exception("slack: on_inbound failed: %s", exc)
        return True

    # ------------------------------------------------------------------
    # Lifecycle teardown
    # ------------------------------------------------------------------
    def stop(self) -> None:
        """先关 WS 让 ``recv()`` 立刻返回，再停线程。

        顺序不能反：基类 ``stop()`` 会 join 线程（5s 超时），而 ``recv()``
        最多阻塞 SOCKET_TIMEOUT(30s)，不先关连接就会每次 stop 都等满超时。
        """
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                ws.close()
            except Exception as exc:
                logger.debug("slack: ws close during stop failed: %s", exc)
        super().stop()

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
            self._note_send_failure(SendError.BAD_FORMAT, "bad conversation_id")
            return None
        if not out.text:
            logger.warning("slack: refusing to send empty text")
            self._note_send_failure(SendError.BAD_FORMAT, "empty text")
            return None
        chunks: List[str] = split_text(out.text, self.message_limit, prefix_fmt="")
        if len(chunks) > 1:
            logger.info("slack: splitting outbound message into %d chunks", len(chunks))
        handle: MsgHandle | None = None
        for chunk in chunks:
            self._throttle(out.conversation_id)
            status, data = self._request(
                "POST", "chat.postMessage", {"channel": channel, "text": chunk}
            )
            if not data.get("ok"):
                err = str(data.get("error") or "")
                logger.warning(
                    "slack: chat.postMessage failed (HTTP %s): %s",
                    status,
                    data.get("error"),
                )
                self._note_send_failure(_classify_slack_error(status, err), err)
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

"""Lane C — bridge core: session lifecycle, commands, streaming, dispatch.

``BridgeCore`` implements the :class:`~opencode_bridge.hooks.Hooks` protocol:

* adapter polling threads call :meth:`on_inbound` / :meth:`on_callback`
* a dedicated daemon thread consumes ``client.subscribe()`` (SSE) and
  dispatches events to the streaming logic

Shared state (turns, queues, reverse session map) is guarded by one
``threading.RLock``.  Blocking I/O (HTTP calls, adapter sends) always happens
*outside* the lock so a slow network never freezes the other conversations.

Robustness rules:

* every event handler is wrapped in ``try/except Exception`` — a single bad
  event must never kill the SSE thread
* ``session.text.delta`` for an unknown ``sessionID`` is dropped silently
* every outbound text is sanitised (``\\x00`` and other control characters
  removed, undecodable surrogates replaced)
* ``adapter.edit`` is always wrapped: ``ValueError`` (text too long) falls
  back to ``adapter.send`` for finalisation
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .adapters import Adapter
from .config import DEFAULT_CONFIG_NAME, Config
from .hooks import Button, Inbound, MsgHandle, Outbound  # BridgeCore implements Hooks
from .opencode_client import OpenCodeClient, OpenCodeError
from .state import StateStore

__all__ = [
    "BridgeCore",
    "HELP_TEXT",
    "NO_OUTPUT_TEXT",
    "ruleset_for",
    "SETUP_MENU_TEXT",
    "setup_reply",
    "setup_platforms",
]

logger = logging.getLogger("opencode_bridge.core")

DEFAULT_EDIT_INTERVAL = 1.5
DEFAULT_MAX_MESSAGE_CHARS = 4000
SESSION_TITLE_PREFIX = "tg-bridge:"
SESSION_TITLE_MAX = 60
PROGRESS_TEXT = "⏳ 处理中…"
NO_OUTPUT_TEXT = "（无输出）"

#: Values accepted in ``perm:<sessionID>:<reqID>:<decision>`` callbacks.
_PERM_DECISIONS = ("once", "always", "reject")

HELP_TEXT = """\
可用命令：
/help                       显示本帮助
/setup [平台]               三个平台接入引导（Telegram / Slack / Discord）
/new  /reset                新建会话（丢弃当前上下文）
/stop                       中断当前正在执行的任务
/status                     查看当前会话状态
/cd <目录>                  切换工作目录并新建会话
/approve <请求ID> [always]  允许权限请求（always = 总是允许）
/deny <请求ID>              拒绝权限请求
直接发送文本即可与 agent 对话。
安全提示：桥接进程拥有与你相同的本地权限，请仅在可信环境运行，
并务必为适配器配置 allowed_chat_ids 白名单。"""

#: ``/setup`` with no argument: the platform chooser (frozen copy).
SETUP_MENU_TEXT = """\
选择要接入的平台：
1) Telegram —— 支持双向对话
2) Slack —— 支持双向对话
3) Discord —— 支持双向对话

回复 /setup 1、/setup 2 或 /setup 3 也可直接查看。"""

#: ``/setup <bad>``: same list, framed as a usage hint (frozen copy).
SETUP_INVALID_TEXT = """\
无法识别的平台。可接入的平台有：
1) Telegram —— 支持双向对话
2) Slack —— 支持双向对话
3) Discord —— 支持双向对话

用法: /setup 1|2|3 或 /setup telegram|slack|discord"""

#: Accepted ``/setup`` arguments -> canonical platform key (lower-cased first).
_SETUP_ALIASES = {
    "1": "telegram",
    "telegram": "telegram",
    "2": "slack",
    "slack": "slack",
    "3": "discord",
    "discord": "discord",
}

#: Inline buttons offered on the menu (adapter may ignore them).
_SETUP_BUTTON_LABELS = (("telegram", "Telegram"), ("slack", "Slack"),
                         ("discord", "Discord"))


def _config_path_hint() -> str:
    """Absolute config-file path for the ``/setup`` guides (runtime only).

    Mirrors :meth:`Config.load`'s search chain: honour ``OPENCODE_BRIDGE_CONFIG``
    when it points at a real file, else ``<cwd>/config.json`` when present,
    else fall back to ``<package root>/config.json`` with an explanatory note.
    Never raises and never hardcodes a machine-specific path.
    """
    def bridge_root() -> str:
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    env_path = (os.environ.get("OPENCODE_BRIDGE_CONFIG") or "").strip()
    if env_path and os.path.isfile(env_path):
        return os.path.abspath(env_path)
    cwd_path = os.path.join(os.getcwd(), DEFAULT_CONFIG_NAME)
    if os.path.isfile(cwd_path):
        return os.path.abspath(cwd_path)
    return (
        os.path.join(bridge_root(), DEFAULT_CONFIG_NAME)
        + "（未找到已生效的配置文件，以上为桥接目录默认位置）"
    )


#: Frozen per-platform onboarding guides (factual copy; do not reword).
_SETUP_GUIDES = {
    "telegram": """\
1. 打开 Telegram，找 @BotFather → 发送 /newbot
2. 依次设置显示名、用户名（必须以 bot 结尾），复制返回的 token（形如 123456789:AA...)
3. 找 @userinfobot → 发送任意一句话 → 复制返回的纯数字 chat id
4. 编辑配置文件：
     "adapters": {
       "telegram": { "bot_token": "123456789:AA...", "allowed_chat_ids": [123456789] }
     }
   注意 allowed_chat_ids 是数组，数字不要加引号
5. 执行 opencode service restart
6. 在 Telegram 给你的 bot 发一句 hi，收到回复即成功""",
    "slack": """\
1. 打开 https://api.slack.com/apps → Create New App → From scratch → 选 workspace
2. 左侧 Socket Mode → 打开 Enable Socket Mode
3. 左侧 Basic Information → App-Level Tokens → Generate Token and Scopes
   → 命名 → 勾选 connections:write → 复制（xapp- 开头，入站必需）
4. 左侧 OAuth & Permissions → Scopes → Bot Token Scopes → Add an OAuth Scope，添加
   chat:write、channels:history、im:history
   （要 @ 才响应加 app_mentions:read；用私有频道加 groups:history）
5. 同页顶部 Install to Workspace → Allow → 复制 Bot User OAuth Token（xoxb- 开头）
   注意：之后每改一次 scope，都要回来点一次 Reinstall to Workspace
6. 左侧 Event Subscriptions → 打开 Enable Events
   → Subscribe to bot events → Add Bot User Event → 添加 message.channels、message.im
7. 编辑配置文件：
     "adapters": { "slack": { "bot_token": "xoxb-...", "app_token": "xapp-..." } }
8. 在目标频道输入 /invite @你的bot（私有频道同样用 /invite；私聊可直接发消息）
9. 执行 opencode service restart
10. 在频道里发一句普通文字，收到回复即成功

两个常见坑：
· Event Subscriptions 没打开、或事件没加在 bot events 下，会「静默收不到」且不报错
· 只填 bot_token 也能启动，但那只发不收（入站必须有 app_token）""",
    "discord": """\
1. 打开 https://discord.com/developers/applications → New Application → 左侧 Bot
2. Reset Token → 复制 token
3. 同一页把 Privileged Gateway Intents 下的 Message Content Intent 打开（必需）
4. 左侧 OAuth2 → URL Generator → 勾选 scope: bot → Permissions: Send Messages
5. 用生成的 URL 把 bot 邀请进你的服务器
6. 编辑配置文件：
     "adapters": { "discord": { "bot_token": "..." } }
7. 执行 opencode service restart
8. 在频道里发一句普通文字，收到回复即成功

两个常见坑：
· 第 3 步的开关不开，网关会直接拒绝连接（close 4014），日志里会写明原因
· bot 必须已被邀请进频道，否则发消息报 not_in_channel""",
}


def _setup_guide(platform: str) -> str:
    """Compose the ``/setup <platform>`` reply: path hint + frozen guide."""
    return (
        f"配置文件： {_config_path_hint()}\n"
        "改完后执行： opencode service restart\n\n"
        + _SETUP_GUIDES[platform]
    )


def setup_reply(platform: str | None = None) -> str:
    """Public entry for the ``/setup`` onboarding copy.

    Reused by :mod:`opencode_bridge.__main__` (``--setup``) so the CLI and the
    in-bot command share **one** source of truth. ``platform`` may be ``None``
    (menu), a canonical key or any alias in :data:`_SETUP_ALIASES`.
    """
    token = (platform or "").strip()
    if not token:
        return SETUP_MENU_TEXT
    key = _SETUP_ALIASES.get(token.lower())
    return _setup_guide(key) if key is not None else SETUP_INVALID_TEXT


def setup_platforms() -> tuple[tuple[str, str], ...]:
    """``(key, label)`` pairs for the platform chooser, in menu order."""
    return _SETUP_BUTTON_LABELS


def _clean(text: Any) -> str:
    """Make ``text`` safe for any messaging platform.

    Drops ``\\x00`` and other C0 control characters (kept: ``\\n`` ``\\r``
    ``\\t``) and replaces undecodable byte sequences / lone surrogates.
    No markdown processing happens here — adapters send plain text.
    """
    if not isinstance(text, str):
        text = str(text)
    try:
        text = text.encode("utf-8", "replace").decode("utf-8")
    except Exception:  # pragma: no cover - extremely defensive
        text = text.encode("utf-8", "ignore").decode("utf-8", "ignore")
    return "".join(ch for ch in text if ch in "\n\r\t" or ch >= " ")


def ruleset_for(mode: str | None) -> list[dict] | None:
    """Map ``permissions_mode`` to an OpenCode permissions rule set.

    ``"ask"`` (and anything unknown) -> ``None`` (server default = ask).
    """
    normalized = str(mode or "ask").strip().lower()
    if normalized == "allow":
        return [{"action": "*", "resource": "*", "effect": "allow"}]
    if normalized == "deny":
        return [{"action": "*", "resource": "*", "effect": "deny"}]
    if normalized != "ask":
        logger.warning(
            "unknown permissions_mode %r; falling back to 'ask'", mode
        )
    return None


def _session_id(data: dict) -> str:
    for key in ("sessionID", "session_id", "sessionId"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


# ----------------------------------------------------------------------
# turn state
# ----------------------------------------------------------------------
@dataclass
class Turn:
    """Streaming state for one execution of one session."""

    conversation_id: str
    progress_handle: MsgHandle | None = None
    #: assistantMessageID -> {ordinal: delta} (deltas may arrive out of order)
    parts: dict[str, dict[int, str]] = field(default_factory=dict)
    last_edit_ts: float = 0.0
    tool_trace: list[str] = field(default_factory=list)
    agent: str = ""
    model: str = ""

    def assemble(self) -> str:
        chunks: list[str] = []
        for by_ordinal in self.parts.values():  # dict order == arrival order
            for ordinal in sorted(by_ordinal):
                chunks.append(by_ordinal[ordinal])
        return "".join(chunks)


# ----------------------------------------------------------------------
# core
# ----------------------------------------------------------------------
class BridgeCore:
    """Routes IM messages to OpenCode sessions and streams events back."""

    def __init__(
        self, config: Config, client: OpenCodeClient, state: StateStore
    ) -> None:
        self.config = config
        self.client = client
        self.state = state

        self._lock = threading.RLock()
        self._adapters: list[Adapter] = []
        self._adapter_by_name: dict[str, Adapter] = {}
        #: conversation_id -> adapter name (learned on first inbound)
        self._conv_adapter: dict[str, str] = {}
        #: session_id -> conversation_id (rebuilt from state per event)
        self._sid_conv: dict[str, str] = {}
        self._turns: dict[str, Turn] = {}
        self._queues: dict[str, list[str]] = {}
        self._draining: set[str] = set()
        #: (session_id, tool call id) -> tool name (from tool.input.started)
        self._tool_names: dict[tuple[str, str], str] = {}

        self._thread: threading.Thread | None = None
        self._started = False

        bridge_cfg = getattr(config, "bridge", None) or {}
        self.edit_interval = self._positive_float(
            bridge_cfg.get("edit_interval_seconds"), DEFAULT_EDIT_INTERVAL
        )
        self.max_message_chars = max(
            1, int(self._positive_float(bridge_cfg.get("max_message_chars"),
                                        DEFAULT_MAX_MESSAGE_CHARS))
        )
        #: injectable monotonic clock (tests freeze it to check throttling)
        self.clock: Callable[[], float] = time.monotonic

        self._handlers: dict[str, Callable[[dict], None]] = {
            "session.execution.started": self._on_execution_started,
            "session.execution.succeeded": self._on_turn_finished,
            "session.execution.interrupted": self._on_turn_finished,
            "session.step.started": self._on_step_started,
            "session.text.delta": self._on_text_delta,
            "session.tool.input.started": self._on_tool_input_started,
            "session.tool.called": self._on_tool_event,
            "session.tool.success": self._on_tool_event,
            "session.tool.failed": self._on_tool_event,
            "session.status": self._on_status,
            "session.idle": self._on_turn_finished,
            "session.execution.failed": self._on_execution_failed,
            "permission.asked": self._on_permission_asked,
        }

    @staticmethod
    def _positive_float(value: Any, default: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        if number < 0 or number != number:  # negative or NaN
            return default
        return number

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    @property
    def adapters(self) -> tuple[Adapter, ...]:
        with self._lock:
            return tuple(self._adapters)

    def attach(self, adapter: Adapter) -> None:
        """Attach one messaging adapter (may be called multiple times)."""
        with self._lock:
            if any(a is adapter for a in self._adapters):
                return
            self._adapters.append(adapter)
            self._adapter_by_name.setdefault(adapter.name, adapter)
        logger.info("adapter attached: %s", adapter.name)

    def start(self) -> None:
        """Start the SSE reader thread and every attached adapter."""
        with self._lock:
            if self._started:
                return
            self._started = True
            thread = threading.Thread(
                target=self._event_loop, name="opencode-sse", daemon=True
            )
            self._thread = thread
        thread.start()
        logger.info("event stream thread started")
        for adapter in self.adapters:
            try:
                adapter.start()
            except Exception:
                logger.exception("adapter %s failed to start", adapter.name)
        logger.info("bridge core started with %d adapter(s)", len(self.adapters))

    def stop(self) -> None:
        """Stop adapters, close the client and join the SSE thread.

        Safe to call twice; every step is exception-isolated so that a
        ``KeyboardInterrupt`` can always unwind cleanly.
        """
        logger.info("bridge core stopping ...")
        for adapter in self.adapters:
            try:
                adapter.stop()
            except Exception:
                logger.exception("adapter %s failed to stop", adapter.name)
        try:
            self.client.close()
        except Exception:
            logger.exception("client close failed")
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            try:
                thread.join(timeout=5.0)
            except Exception:  # pragma: no cover - defensive
                logger.exception("event thread join failed")
            if thread.is_alive():
                logger.warning("event thread did not exit within 5s")
        self._thread = None
        logger.info("bridge core stopped")

    # ------------------------------------------------------------------
    # adapter routing
    # ------------------------------------------------------------------
    def _route_by_prefix(
        self, conversation_id: str, adapters: list[Adapter]
    ) -> Adapter:
        def named(name: str) -> Adapter | None:
            for adapter in adapters:
                if adapter.name == name:
                    return adapter
            return None

        if conversation_id.startswith("chat:"):
            return named("telegram") or adapters[0]
        if conversation_id.startswith("channel:"):
            rest = conversation_id[len("channel:"):]
            # Discord channel ids are numeric, Slack ids start with "C..."
            if rest.isdigit():
                return named("discord") or named("slack") or adapters[0]
            return named("slack") or named("discord") or adapters[0]
        return adapters[0]

    def _adapter_for(self, conversation_id: str) -> Adapter | None:
        """Pick the adapter that owns ``conversation_id``.

        Order: remembered mapping -> calling polling thread -> conversation
        id prefix -> first attached adapter.
        """
        with self._lock:
            remembered = self._conv_adapter.get(conversation_id)
            if remembered:
                adapter = self._adapter_by_name.get(remembered)
                if adapter is not None:
                    return adapter
            adapters = list(self._adapters)
        if not adapters:
            return None

        chosen: Adapter | None = None
        current = threading.current_thread()
        for adapter in adapters:
            # Lane B keeps its poller thread in ``_thread``; matching the
            # thread tells us which adapter invoked the hook.
            poller = getattr(adapter, "_thread", None)
            if poller is not None and poller is current:
                chosen = adapter
                break
        if chosen is None:
            chosen = self._route_by_prefix(conversation_id, adapters)
        with self._lock:
            self._conv_adapter[conversation_id] = chosen.name
        return chosen

    # ------------------------------------------------------------------
    # Hooks: inbound
    # ------------------------------------------------------------------
    def on_inbound(self, inbound: Inbound) -> None:
        try:
            if inbound.is_callback:
                # Lane B fires on_inbound(kind="callback") *before*
                # on_callback(); handling it here as well would double-send.
                return
            conversation_id = str(inbound.conversation_id or "")
            text = _clean(inbound.text).strip()
            if not conversation_id or not text:
                return
            adapter = self._adapter_for(conversation_id)
            if adapter is None:
                logger.warning(
                    "no adapter attached; dropping message for %s",
                    conversation_id,
                )
                return
            if text.startswith("/"):
                self._handle_command(conversation_id, adapter, text)
            else:
                self._enqueue(conversation_id, text)
        except Exception:
            logger.exception("on_inbound failed")

    def on_callback(
        self, conversation_id: str, data: str, query_id: str
    ) -> None:
        """Handle ``setup:<platform>`` and ``perm:<sessionID>:<reqID>:<decision>``."""
        adapter = None
        try:
            adapter = self._adapter_for(conversation_id)
            data = str(data or "")
            # /setup inline-button press: reply once here (on_inbound already
            # dropped the kind="callback" copy, so no double-send) and ack.
            if data.startswith("setup:"):
                platform = _SETUP_ALIASES.get(data.split(":", 1)[1].strip().lower())
                if platform is None:
                    self._answer(adapter, query_id, "未知平台")
                    return
                self._send_text(
                    conversation_id, _setup_guide(platform), kind="text",
                    adapter=adapter,
                )
                self._answer(adapter, query_id, "已打开接入引导")
                return
            parts = data.split(":", 3)
            decision_ok = len(parts) == 4 and parts[0] == "perm"
            if decision_ok:
                _, session_id, request_id, decision = parts
                decision_ok = decision in _PERM_DECISIONS
            if not decision_ok:
                logger.warning("unsupported callback payload: %r", data)
                self._answer(adapter, query_id, "失败")
                return
            try:
                self.client.reply_permission(session_id, request_id, decision)
            except Exception as exc:
                logger.warning(
                    "reply_permission(%s, %s, %s) failed: %s",
                    session_id,
                    request_id,
                    decision,
                    exc,
                )
                self._answer(adapter, query_id, "失败")
                self._send_text(
                    conversation_id,
                    f"权限回复失败: {exc}",
                    kind="error",
                    adapter=adapter,
                )
                return
            self._answer(adapter, query_id, "已处理")
        except Exception:
            logger.exception("on_callback failed")
            self._answer(adapter, query_id, "失败")

    @staticmethod
    def _answer(adapter: Adapter | None, query_id: str, text: str) -> None:
        if adapter is None or not query_id:
            return
        try:
            adapter.answer(query_id, text)
        except Exception:
            logger.exception("adapter.answer failed")

    # ------------------------------------------------------------------
    # outbound helpers
    # ------------------------------------------------------------------
    def _send_text(
        self,
        conversation_id: str,
        text: str,
        *,
        kind: str = "text",
        adapter: Adapter | None = None,
        session_id: str | None = None,
    ) -> MsgHandle | None:
        adapter = adapter or self._adapter_for(conversation_id)
        if adapter is None:
            logger.warning("no adapter for %s; cannot send", conversation_id)
            return None
        out = Outbound(
            conversation_id=conversation_id,
            text=_clean(text),
            kind=kind,
            session_id=session_id,
        )
        try:
            return adapter.send(out)
        except Exception:
            logger.exception("adapter.send failed for %s", conversation_id)
            return None

    def _edit_progress(
        self,
        conversation_id: str,
        handle: MsgHandle,
        text: str,
        session_id: str,
    ) -> bool:
        """Throttled/streaming edit. Never falls back to send (no spam)."""
        adapter = self._adapter_for(conversation_id)
        if adapter is None:
            return False
        out = Outbound(
            conversation_id=conversation_id,
            text=_clean(text),
            kind="progress",
            session_id=session_id,
        )
        try:
            ok = adapter.edit(handle, out)
        except ValueError:
            logger.warning(
                "progress edit rejected by adapter (text too long: %d chars)",
                len(out.text),
            )
            return False
        except Exception:
            logger.exception("adapter.edit failed")
            return False
        if not ok:
            logger.debug("progress edit returned False for %s", conversation_id)
        return bool(ok)

    def _finalize(
        self, conversation_id: str, handle: MsgHandle | None, text: str,
        session_id: str,
    ) -> None:
        """Publish the final message (LANE_C_SPEC §1.5 step 4/5)."""
        adapter = self._adapter_for(conversation_id)
        if adapter is None:
            logger.warning("no adapter for %s; cannot finalise", conversation_id)
            return
        final = _clean(text) or NO_OUTPUT_TEXT
        if handle is not None and len(final) <= self.max_message_chars:
            out = Outbound(
                conversation_id=conversation_id,
                text=final,
                kind="final",
                session_id=session_id,
            )
            try:
                if adapter.edit(handle, out):
                    return
            except ValueError:
                # Lane B raises for texts above the platform limit.
                logger.warning(
                    "final edit rejected (%d chars); sending instead",
                    len(final),
                )
            except Exception:
                logger.exception("adapter.edit failed; sending instead")
        # no handle / too long / edit failed -> plain send (adapter chunks)
        self._send_text(
            conversation_id, final, kind="final",
            adapter=adapter, session_id=session_id,
        )

    # ------------------------------------------------------------------
    # prompt queue (one conversation at a time, many in parallel)
    # ------------------------------------------------------------------
    def _enqueue(self, conversation_id: str, text: str) -> None:
        with self._lock:
            self._queues.setdefault(conversation_id, []).append(text)
            if conversation_id in self._draining:
                return  # current drainer will pick this up
            self._draining.add(conversation_id)
        self._drain(conversation_id)

    def _flush_queue(self, conversation_id: str) -> None:
        """Called after a turn finalises (or on ``session.idle``)."""
        with self._lock:
            if conversation_id in self._draining:
                return
            if not self._queues.get(conversation_id):
                return
            self._draining.add(conversation_id)
        self._drain(conversation_id)

    def _drain(self, conversation_id: str) -> None:
        try:
            while True:
                with self._lock:
                    queue = self._queues.get(conversation_id) or []
                    if not queue:
                        # empty check + draining flag flip are atomic, so a
                        # concurrent enqueue can never be lost
                        self._draining.discard(conversation_id)
                        return
                    text = queue.pop(0)
                outcome = self._dispatch_prompt(conversation_id, text)
                if outcome == "busy":
                    with self._lock:
                        queue = self._queues.setdefault(conversation_id, [])
                        queue.insert(0, text)
                        self._draining.discard(conversation_id)
                    return
        except Exception:
            logger.exception("queue drain failed for %s", conversation_id)
            with self._lock:
                self._draining.discard(conversation_id)

    def _dispatch_prompt(self, conversation_id: str, text: str) -> str:
        """Send one queued prompt. Returns ``ok`` / ``busy`` / ``error``."""
        adapter = self._adapter_for(conversation_id)
        try:
            session_id = self._ensure_session(conversation_id)
        except Exception as exc:
            logger.exception("create_session failed for %s", conversation_id)
            self._send_text(
                conversation_id,
                f"创建会话失败: {exc}",
                kind="error",
                adapter=adapter,
            )
            return "error"

        try:
            self.client.prompt(session_id, text)
        except OpenCodeError as exc:
            if exc.status == 409:
                logger.info(
                    "session %s busy; message queued for %s",
                    session_id,
                    conversation_id,
                )
                return "busy"
            logger.warning("prompt failed: %s", exc)
            self._send_text(
                conversation_id,
                f"发送失败: {exc}（可尝试 /new 重建会话）",
                kind="error",
                adapter=adapter,
            )
            return "error"
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("prompt failed")
            self._send_text(
                conversation_id,
                f"发送失败: {exc}",
                kind="error",
                adapter=adapter,
            )
            return "error"

        # success: create / reuse this turn's progress message
        with self._lock:
            turn = self._turns.get(session_id)
            if turn is None:
                turn = Turn(conversation_id=conversation_id)
                self._turns[session_id] = turn
            need_progress = turn.progress_handle is None
        if need_progress:
            handle = self._send_text(
                conversation_id,
                PROGRESS_TEXT,
                kind="progress",
                adapter=adapter,
                session_id=session_id,
            )
            with self._lock:
                current = self._turns.get(session_id)
                if current is not None and current.progress_handle is None:
                    current.progress_handle = handle
        return "ok"

    # ------------------------------------------------------------------
    # session lifecycle
    # ------------------------------------------------------------------
    def _ensure_session(self, conversation_id: str) -> str:
        session_id = self.state.get_session(conversation_id)
        if session_id:
            return session_id
        directory = (
            self.state.get_meta(conversation_id, "directory", None)
            or self.config.opencode_directory
            or "."
        )
        title = f"{SESSION_TITLE_PREFIX}{conversation_id}"[:SESSION_TITLE_MAX]
        agent = self.config.opencode_agent or None
        rules = ruleset_for(self.config.permissions_mode)
        try:
            session_id = self.client.create_session(
                directory=directory,
                title=title,
                agent=agent,
                permissions=rules,
            )
        except OpenCodeError as exc:
            if exc.status != 400 or rules is None:
                raise
            logger.warning(
                "create_session rejected permissions (%s); retrying without",
                exc,
            )
            session_id = self.client.create_session(
                directory=directory,
                title=title,
                agent=agent,
                permissions=None,
            )
        self.state.set_session(conversation_id, session_id)
        logger.info(
            "created session %s for %s (dir=%s)", session_id, conversation_id,
            directory,
        )
        return session_id

    def _drop_session(self, conversation_id: str) -> str | None:
        """Delete the current session server-side and locally (never raises)."""
        session_id = self.state.get_session(conversation_id)
        if session_id:
            try:
                self.client.delete_session(session_id)
            except Exception as exc:
                logger.warning("delete_session(%s) failed: %s", session_id, exc)
            self.state.drop_session(conversation_id)
            with self._lock:
                self._turns.pop(session_id, None)
        return session_id

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------
    def _handle_command(
        self, conversation_id: str, adapter: Adapter, text: str
    ) -> None:
        tokens = text.split(None, 1)
        raw_name = tokens[0][1:]  # strip "/"
        args = tokens[1].strip() if len(tokens) > 1 else ""
        # Telegram group commands may carry a bot suffix: /new@your_bot
        name = raw_name.split("@", 1)[0].lower()
        args = _clean(args)

        handler = {
            "help": self._cmd_help,
            "setup": self._cmd_setup,
            "new": self._cmd_new,
            "reset": self._cmd_new,
            "stop": self._cmd_stop,
            "status": self._cmd_status,
            "cd": self._cmd_cd,
            "approve": self._cmd_approve,
            "allow": self._cmd_approve,
            "deny": self._cmd_deny,
        }.get(name)
        if handler is None:
            self._send_text(
                conversation_id,
                f"未知命令 {raw_name}，发送 /help 查看用法。",
                kind="error",
                adapter=adapter,
            )
            return
        try:
            handler(conversation_id, adapter, args)
        except OpenCodeError as exc:
            logger.warning("command /%s failed: %s", name, exc)
            self._send_text(
                conversation_id,
                f"命令执行失败: {exc}",
                kind="error",
                adapter=adapter,
            )
        except Exception as exc:
            logger.exception("command /%s failed", name)
            self._send_text(
                conversation_id,
                f"命令执行失败: {exc}",
                kind="error",
                adapter=adapter,
            )

    def _cmd_help(self, conversation_id: str, adapter: Adapter, args: str) -> None:
        self._send_text(conversation_id, HELP_TEXT, adapter=adapter)

    def _cmd_setup(
        self, conversation_id: str, adapter: Adapter, args: str
    ) -> None:
        """``/setup`` menu / ``/setup <telegram|slack|discord|1|2|3>`` guide."""
        token = args.strip().split(None, 1)[0] if args.strip() else ""
        if not token:
            self._send_setup_menu(conversation_id, adapter)
            return
        platform = _SETUP_ALIASES.get(token.lower())
        if platform is None:
            self._send_text(
                conversation_id, SETUP_INVALID_TEXT, kind="error",
                adapter=adapter,
            )
            return
        self._send_text(
            conversation_id, _setup_guide(platform), kind="text",
            adapter=adapter,
        )

    def _send_setup_menu(self, conversation_id: str, adapter: Adapter) -> None:
        """Plain-text chooser + inline buttons (best effort, never raises).

        ``send`` only carries text; Telegram attaches the keyboard through
        ``edit``, so the menu is sent first and then re-edited with buttons.
        A failed button edit simply leaves the (already usable) text menu.
        """
        handle = self._send_text(
            conversation_id, SETUP_MENU_TEXT, kind="text", adapter=adapter
        )
        if handle is None:
            return
        out = Outbound(
            conversation_id=conversation_id,
            text=SETUP_MENU_TEXT,
            kind="text",
            buttons=tuple(
                Button(label=label, data=f"setup:{key}")
                for key, label in _SETUP_BUTTON_LABELS
            ),
        )
        try:
            adapter.edit(handle, out)
        except Exception:
            logger.exception("attaching /setup inline buttons failed")

    def _cmd_new(self, conversation_id: str, adapter: Adapter, args: str) -> None:
        self._drop_session(conversation_id)
        session_id = self._ensure_session(conversation_id)
        self._send_text(
            conversation_id,
            f"已新建会话 {session_id[:12]}",
            adapter=adapter,
            session_id=session_id,
        )

    def _cmd_stop(self, conversation_id: str, adapter: Adapter, args: str) -> None:
        session_id = self.state.get_session(conversation_id)
        if not session_id:
            self._send_text(conversation_id, "当前没有会话。", adapter=adapter)
            return
        self.client.interrupt(session_id)  # OpenCodeError -> caught by caller
        self._send_text(
            conversation_id,
            "已请求中断当前任务。",
            adapter=adapter,
            session_id=session_id,
        )

    def _cmd_status(self, conversation_id: str, adapter: Adapter, args: str) -> None:
        session_id = self.state.get_session(conversation_id)
        if not session_id:
            self._send_text(conversation_id, "当前没有会话。", adapter=adapter)
            return
        session = self.client.get_session(session_id)
        model = session.get("model")
        if isinstance(model, dict):
            model_text = "/".join(
                str(model.get(key))
                for key in ("providerID", "id")
                if model.get(key)
            ) or "?"
        else:
            model_text = str(model or "?")
        tokens = _as_dict(session.get("tokens"))
        directory = _as_dict(session.get("location")).get(
            "directory", self.config.opencode_directory
        )
        lines = [
            "会话状态",
            f"session_id: {session.get('id') or session_id}",
            f"agent: {session.get('agent') or '-'}",
            f"model: {model_text}",
            f"cost: {session.get('cost', 0)}",
            f"tokens: input={tokens.get('input', 0)} "
            f"output={tokens.get('output', 0)}",
            f"directory: {directory or '-'}",
        ]
        self._send_text(
            conversation_id,
            "\n".join(lines),
            adapter=adapter,
            session_id=session_id,
        )

    def _cmd_cd(self, conversation_id: str, adapter: Adapter, args: str) -> None:
        if not args:
            self._send_text(
                conversation_id,
                "用法: /cd <目录>   例如 /cd /data/work",
                kind="error",
                adapter=adapter,
            )
            return
        directory = args
        self.state.set_meta(conversation_id, "directory", directory)
        self._drop_session(conversation_id)
        self._ensure_session(conversation_id)
        self._send_text(conversation_id, f"已切换到 {directory}", adapter=adapter)

    def _cmd_approve(
        self, conversation_id: str, adapter: Adapter, args: str
    ) -> None:
        self._reply_permission(conversation_id, adapter, args, "once")

    def _cmd_deny(
        self, conversation_id: str, adapter: Adapter, args: str
    ) -> None:
        self._reply_permission(conversation_id, adapter, args, "reject")

    def _reply_permission(
        self,
        conversation_id: str,
        adapter: Adapter,
        args: str,
        default_decision: str,
    ) -> None:
        tokens = args.split()
        usage = "用法: /approve <请求ID> [always]  或  /deny <请求ID>"
        if not tokens or len(tokens) > 2:
            self._send_text(conversation_id, usage, kind="error", adapter=adapter)
            return
        request_id = tokens[0]
        decision = default_decision
        if len(tokens) == 2:
            choice = tokens[1].lower()
            if choice not in ("once", "always"):
                self._send_text(
                    conversation_id, usage, kind="error", adapter=adapter
                )
                return
            decision = choice
        session_id = self.state.get_session(conversation_id)
        if not session_id:
            self._send_text(
                conversation_id, "当前没有会话，无法回复权限请求。",
                kind="error", adapter=adapter,
            )
            return
        self.client.reply_permission(session_id, request_id, decision)
        self._send_text(
            conversation_id,
            f"已回复权限请求 {request_id}: {decision}",
            adapter=adapter,
            session_id=session_id,
        )

    # ------------------------------------------------------------------
    # SSE
    # ------------------------------------------------------------------
    def _event_loop(self) -> None:
        try:
            for event in self.client.subscribe():
                try:
                    if not isinstance(event, dict):
                        continue
                    self._dispatch(event)
                except Exception:
                    logger.exception(
                        "event dispatch failed: %s",
                        event.get("type")
                        if isinstance(event, dict)
                        else repr(event),
                    )
        except Exception:
            logger.exception("event stream terminated")
        logger.debug("event loop exited")

    def _dispatch(self, event: dict) -> None:
        handler = self._handlers.get(str(event.get("type") or ""))
        if handler is None:
            return  # unknown / irrelevant event
        data = _as_dict(event.get("data"))
        with self._lock:
            # refresh the session -> conversation reverse map every round
            self._sid_conv = {
                sid: conv for conv, sid in self.state.all_sessions().items()
            }
        handler(data)

    # ------------------------------------------------------------------
    # event handlers (each is called from the SSE thread)
    # ------------------------------------------------------------------
    def _conversation_for(self, session_id: str) -> str | None:
        with self._lock:
            conv = self._sid_conv.get(session_id)
            if conv:
                return conv
            turn = self._turns.get(session_id)
            return turn.conversation_id if turn else None

    def _on_execution_started(self, data: dict) -> None:
        session_id = _session_id(data)
        if not session_id:
            return
        with self._lock:
            conv = self._sid_conv.get(session_id)
            turn = self._turns.get(session_id)
            if turn is None:
                if conv is None:
                    return
                turn = Turn(conversation_id=conv)
                self._turns[session_id] = turn
            # reset the round; keep the progress message (reused)
            turn.parts.clear()
            turn.tool_trace.clear()
            turn.last_edit_ts = 0.0
            turn.agent = ""
            turn.model = ""
            self._tool_names = {
                key: name
                for key, name in self._tool_names.items()
                if key[0] != session_id
            }

    def _on_step_started(self, data: dict) -> None:
        session_id = _session_id(data)
        if not session_id:
            return
        agent = data.get("agent")
        model = data.get("model")
        if isinstance(model, dict):
            model = "/".join(
                str(model.get(key))
                for key in ("providerID", "id")
                if model.get(key)
            )
        with self._lock:
            turn = self._turns.get(session_id)
            if turn is None:
                return
            if isinstance(agent, str) and agent:
                turn.agent = agent
            if isinstance(model, str) and model:
                turn.model = model

    def _on_text_delta(self, data: dict) -> None:
        session_id = _session_id(data)
        assistant_id = data.get("assistantMessageID")
        delta = data.get("delta")
        if not session_id or not isinstance(assistant_id, str) or not assistant_id:
            return
        if not isinstance(delta, str) or not delta:
            return
        ordinal_raw = data.get("ordinal")
        ordinal = (
            ordinal_raw
            if isinstance(ordinal_raw, int) and not isinstance(ordinal_raw, bool)
            else 0
        )
        with self._lock:
            turn = self._turns.get(session_id)
            if turn is None:
                conv = self._sid_conv.get(session_id)
                if conv is None:
                    return  # unknown session -> silent drop
                turn = Turn(conversation_id=conv)
                self._turns[session_id] = turn
            bucket = turn.parts.setdefault(assistant_id, {})
            bucket[ordinal] = bucket.get(ordinal, "") + delta
            text = turn.assemble()
            handle = turn.progress_handle
            conversation_id = turn.conversation_id
            if handle is not None and len(text) > self.max_message_chars:
                return  # too long to edit; idle finalisation will send it
            now = self.clock()
            if (now - turn.last_edit_ts) < self.edit_interval:
                return  # throttled
            turn.last_edit_ts = now

        if handle is None:
            new_handle = self._send_text(
                conversation_id,
                text,
                kind="progress",
                adapter=self._adapter_for(conversation_id),
                session_id=session_id,
            )
            with self._lock:
                current = self._turns.get(session_id)
                if current is not None and current.progress_handle is None:
                    current.progress_handle = new_handle
            return
        self._edit_progress(conversation_id, handle, text, session_id)

    def _on_tool_input_started(self, data: dict) -> None:
        session_id = _session_id(data)
        name = data.get("name")
        call_id = data.get("id") or data.get("toolCallID")
        if not session_id or not isinstance(name, str) or not name:
            return
        if call_id is None:
            return
        with self._lock:
            self._tool_names[(session_id, str(call_id))] = name

    def _on_tool_event(self, data: dict) -> None:
        session_id = _session_id(data)
        if not session_id:
            return
        call_id = data.get("id") or data.get("toolCallID")
        with self._lock:
            name = None
            if call_id is not None:
                name = self._tool_names.get((session_id, str(call_id)))
            if not isinstance(name, str) or not name:
                name = data.get("name")
            if not isinstance(name, str) or not name:
                name = str(call_id) if call_id else "tool"
            turn = self._turns.get(session_id)
            if turn is None:
                return
            turn.tool_trace.append(f"▶ {name}")

    def _on_status(self, data: dict) -> None:
        session_id = _session_id(data)
        status = _as_dict(data.get("status"))
        status_type = status.get("type")
        if status_type == "idle":
            # legacy / alternate idle signal — same finalisation as
            # session.execution.succeeded (idempotent: the turn is popped)
            self._finalize_session(session_id)
            return
        if status_type != "retry":
            return  # "busy" -> no operation
        conversation_id = self._conversation_for(session_id)
        if not conversation_id:
            return
        attempt = status.get("attempt", "?")
        message = _clean(status.get("message") or "")
        text = f"⏳ 重试中 (attempt {attempt}): {message}"
        with self._lock:
            turn = self._turns.get(session_id)
            handle = turn.progress_handle if turn else None
        if handle is None:
            return
        self._edit_progress(conversation_id, handle, text, session_id)

    def _on_execution_failed(self, data: dict) -> None:
        session_id = _session_id(data)
        error = _as_dict(data.get("error"))
        error_type = error.get("type") or "error"
        error_message = error.get("message") or _clean(data.get("error") or "")
        conversation_id = self._conversation_for(session_id)
        with self._lock:
            turn = self._turns.pop(session_id, None)
        if conversation_id is None:
            logger.warning(
                "execution failed for unknown session %s: %s",
                session_id,
                error_message,
            )
            return
        self._send_text(
            conversation_id,
            f"任务失败 [{error_type}]: {error_message}",
            kind="error",
            session_id=session_id,
        )
        # a failed execution leaves the session idle -> release queued messages
        self._flush_queue(conversation_id)

    def _on_permission_asked(self, data: dict) -> None:
        session_id = _session_id(data)
        conversation_id = self._conversation_for(session_id)
        if not conversation_id:
            logger.warning(
                "permission request for unknown session %s", session_id
            )
            return
        request_id = str(data.get("id") or "")
        action = str(data.get("action") or "?")
        resources = data.get("resources")
        if isinstance(resources, (list, tuple)):
            resources_text = ", ".join(str(item) for item in resources)
        else:
            resources_text = str(resources or "-")
        message = _clean(data.get("message") or "")
        text = (
            "🔐 权限请求\n"
            f"动作: {action}\n"
            f"资源: {resources_text}\n"
            f"说明: {message}\n"
            f"回复: /approve {request_id}  或  "
            f"/approve {request_id} always  或  /deny {request_id}"
        )
        self._send_text(
            conversation_id, text, kind="text", session_id=session_id
        )

    def _finalize_session(self, session_id: str) -> None:
        """Publish the turn's final message, then flush the queue.

        Triggered by ``session.execution.succeeded`` /
        ``session.execution.interrupted`` / ``session.idle`` /
        ``session.status{type:"idle"}``.  Safe to run more than once: the
        turn is popped under the lock, so a duplicate trigger only re-flushes
        an (usually empty) queue.
        """
        if not session_id:
            return
        with self._lock:
            turn = self._turns.pop(session_id, None)
        conversation_id = self._conversation_for(session_id) or (
            turn.conversation_id if turn else None
        )
        if conversation_id is None:
            logger.debug("turn end for unknown session %s", session_id)
            return
        if turn is not None:
            final = _clean(turn.assemble())
            self._finalize(
                conversation_id, turn.progress_handle, final, session_id
            )
        self._flush_queue(conversation_id)

    def _on_turn_finished(self, data: dict) -> None:
        self._finalize_session(_session_id(data))

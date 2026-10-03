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
* inbound **prompt** text is written to the durable inbox (when one is
  injected) *before* it is dispatched, so a crash in the delivery window
  loses nothing silently; the durable policy lives in
  :mod:`opencode_bridge.inbox_recovery`, this module only wires it
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .adapters import Adapter
from .config import DEFAULT_CONFIG_NAME, Config
from .hooks import Button, Inbound, MsgHandle, Outbound  # BridgeCore implements Hooks
from .identity import LEGACY_PREFIXES
from .inbox import InboundInbox, QueuedPrompt
from .inbox_recovery import recover_pending
from .opencode_client import OpenCodeClient, OpenCodeError
from .session_model import SessionModelCommand
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

#: ``StateStore`` 里存放"消息流位置"的 meta 键。轮询型适配器（email 的 IMAP
#: UID 等）靠它跨重启续跑；见 :meth:`BridgeCore.load_stream_cursor`。
_STREAM_CURSOR_META_KEY = "stream_cursor"

#: 启动时等事件流确认连上的上限（秒），见 :meth:`BridgeCore._recover_inbox`。
#: 取 2 秒是因为 opencode 通常就在本机；而真的不可达时这 2 秒只换来一行告警 ——
#: 那种情况下恢复扫描本身也多半会失败，不该在这里死等。
_EVENT_STREAM_CONFIRMED_TIMEOUT_SECONDS = 2.0

HELP_TEXT = """\
可用命令：
/help                       显示本帮助
/setup [平台]               三个平台接入引导（Telegram / Slack / Discord）
/new  /reset                新建会话（丢弃当前上下文）
/stop                       中断当前正在执行的任务
/status                     查看当前会话状态
/model [provider/id]         查看或切换当前会话使用的模型
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
# 写前收件箱：从一条入站消息到收件箱里的一行
# ----------------------------------------------------------------------
# 这一段刻意是**模块级函数**而不是 :class:`BridgeCore` 的方法：那个类已经
# 51 个方法 / ~1160 行自有代码（AGENTS.md §5.1），纯逻辑的构造与记账不该再往里加。
def _queued_prompt_for(inbound: Inbound, text: str) -> QueuedPrompt:
    """Build the :class:`QueuedPrompt` row for one inbound message.

    The body is carried through **verbatim** — never prefixed, never rewritten.
    That text lands in the agent's context, so anything appended here (a
    "replayed after crash" note, say) would be read by the agent as part of the
    user's request. Alerts about a replay belong in the notification path, not
    in the prompt.

    Delivery id: the platform's own ``message_id`` when it has one, else
    ``sha256(platform|conversation_id|text)``. The fallback is a real
    trade-off — two byte-identical messages from a platform that reports no
    ``message_id`` collapse into one delivery — so it is logged, never silent
    (see :func:`_record_inbound`).
    """
    platform = str(inbound.platform or "")
    conversation_id = str(inbound.conversation_id or "")
    message_id = str(inbound.message_id) if inbound.message_id else None
    if message_id:
        delivery_id = "%s:%s:%s" % (platform, conversation_id, message_id)
    else:
        delivery_id = hashlib.sha256(
            ("%s|%s|%s" % (platform, conversation_id, text)).encode("utf-8")
        ).hexdigest()
    return QueuedPrompt(
        delivery_id=delivery_id,
        conversation_id=conversation_id,
        platform=platform,
        message_id=message_id,
        text=text,
    )


def _record_inbound(
    inbox: InboundInbox | None, inbound: Inbound, text: str,
) -> QueuedPrompt | None:
    """Write-ahead one inbound prompt. Returns the row to deliver, or ``None``
    to deliver nothing.

    ``None`` means **dedup hit**: the same ``delivery_id`` is already in the
    inbox, so the platform re-delivered something we have a receipt for. Running
    the agent again on it is exactly what at-most-once is for.

    ``inbox is None`` means the inbox is switched off; delivery proceeds
    unchanged, just without a receipt.
    """
    queued = _queued_prompt_for(inbound, text)
    if inbox is None:
        return queued
    if inbox.record(queued):
        return queued
    if queued.message_id is None:
        # The hash-fallback collapse is the one dedup the user cannot predict
        # from the platform side, so it gets spelled out rather than left as a
        # mysterious "message ignored".
        logger.info(
            "inbox %s: duplicate ignored; this platform reports no message_id, "
            "so the dedup key fell back to a content hash — two byte-identical "
            "messages collapse into one delivery",
            queued.delivery_id,
        )
    else:
        logger.info(
            "inbox %s: duplicate ignored (platform re-delivered a known message)",
            queued.delivery_id,
        )
    return None


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
        self,
        config: Config,
        client: OpenCodeClient,
        state: StateStore,
        inbox: InboundInbox | None = None,
    ) -> None:
        """``inbox`` enables the write-ahead inbox; ``None`` switches it off.

        Optional and last so that every existing construction site keeps working
        untouched — which also means a wiring bug would be invisible (nothing
        fails when the inbox is simply absent), so ``__main__`` logs which mode
        it started in and ``tests/test_inbox_wiring.py`` guards the wiring.
        """
        self.config = config
        self.client = client
        self.state = state
        self._inbox = inbox
        # ``/model`` 的全部逻辑（参数解析、模型目录缓存、回复文案）都在这个对象
        # 里，core 侧只留一个转发（AGENTS.md §5.1：这个类已经 50+ 个方法）。
        self.model_command = SessionModelCommand(
            client, ensure_session=self._ensure_session
        )

        self._lock = threading.RLock()
        self._adapters: list[Adapter] = []
        self._adapter_by_name: dict[str, Adapter] = {}
        #: conversation_id -> adapter name (learned on first inbound)
        self._conv_adapter: dict[str, str] = {}
        #: session_id -> conversation_id (rebuilt from state per event)
        self._sid_conv: dict[str, str] = {}
        self._turns: dict[str, Turn] = {}
        self._queues: dict[str, list[QueuedPrompt]] = {}
        self._draining: set[str] = set()
        #: (session_id, tool call id) -> tool name (from tool.input.started)
        self._tool_names: dict[tuple[str, str], str] = {}

        self._thread: threading.Thread | None = None
        self._started = False
        #: Set once the event stream has delivered its first frame, i.e. the
        #: subscription is live. Startup recovery waits for it (bounded) before
        #: replaying anything — see :meth:`_recover_inbox` for why.
        self._event_stream_confirmed = threading.Event()

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

        # 事件名以 **anomalyco/opencode v2.0.22 源码** 为准，不是文档
        # （`docs/server.mdx` 对 v2 已过时，仍列 v1 事件——那是推测的来源）。
        # 白名单见 `packages/schema/src/event-manifest.ts`。
        #
        # ⚠️ 下面**故意没有** `session.idle` 与 `session.status`：
        # 源码里 `session.idle` 标注 `// deprecated`，而 `session.status`
        # 在 v2.0.22 **全代码库零处发布**。它们曾被当作"一轮结束"的信号，
        # 于是真实环境永远等不到收尾（症状：用户只看到 `⏳ 处理中…`）。
        # 结束信号只有一个：`session.execution.succeeded / .failed / .interrupted`。
        self._handlers: dict[str, Callable[[dict], None]] = {
            "session.execution.started": self._on_execution_started,
            "session.execution.succeeded": self._on_turn_finished,
            "session.execution.interrupted": self._on_execution_interrupted,
            "session.execution.failed": self._on_execution_failed,
            "session.step.started": self._on_step_started,
            "session.text.delta": self._on_text_delta,
            "session.tool.input.started": self._on_tool_input_started,
            "session.tool.called": self._on_tool_event,
            "session.tool.success": self._on_tool_event,
            "session.tool.failed": self._on_tool_event,
            "session.retry.scheduled": self._on_retry_scheduled,
            "permission.asked": self._on_permission_asked,
        }
        #: 事件名出现但没有 handler 时记进这里，便于 `_dispatch` 打汇总日志，
        #: 免得刷屏（一个未知事件可能每秒来几百条）。有界，不无限增长。
        self._unhandled_event_names: dict[str, int] = {}
        #: 上一次打"未处理事件"日志的时刻，用于按时间节流汇总。
        self._last_unhandled_log_at: float = 0.0

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
        """Start the SSE reader thread, replay the inbox, then start adapters."""
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
        # ⚠️ 位置有意义：SSE 线程**之后**、适配器**之前**（理由见 _recover_inbox）。
        self._recover_inbox()
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
        """按 ``conversation_id`` 的平台段猜适配器 —— **兜底路径，不是主路径**。

        ⚠️ **主路径是 :meth:`_remember_platform`**：入站时产生这条消息的适配器
        自己就知道自己是谁（``Inbound.platform``），那里没有不确定性。这里只在
        "这个目标从未收到过入站消息"（例如让agent 主动往某个 chat 发消息）时才会被走到。

        **为什么这里必须覆盖全部前缀**：A1 迁移打掉了"按调用线程判断归属"那层保护
        —— 已迁移的适配器不再持有 ``_thread``，``_adapter_for`` 里的线程匹配恒不命中。
        而本方法此前只硬编码了 ``chat:`` 与 ``channel:`` 两种，其余一律
        ``adapters[0]``；``attach()`` 的顺序就是**配置文件里的字典顺序**（用户可控），
        于是多平台用户可能把回复发到**错误的平台** —— 且不报错。
        """
        def named(name: str) -> Adapter | None:
            for adapter in adapters:
                if adapter.name == name:
                    return adapter
            return None

        cid = str(conversation_id or "")
        head, sep, rest = cid.partition(":")

        # 旧别名以 identity 的登记表为准，避免这里变成第二份真相。
        # 值为 None 表示**歧义**前缀（``channel:`` 被 slack/discord/mattermost 共用）。
        legacy = LEGACY_PREFIXES.get(head, "missing")
        if legacy is None:
            # 歧义前缀只能启发式：Slack id 以 C/D 开头、Discord 是纯数字、
            # Mattermost 是 26 位 base32。**这仍然可能猜错** —— 真正的确定性来自
            # :meth:`_remember_platform`，这里只是没有别的办法时的兜底。
            if rest.isdigit():
                return named("discord") or named("slack") or adapters[0]
            return named("slack") or named("discord") or adapters[0]
        if legacy != "missing" and legacy != head:
            # 真正的别名（``chat``→telegram、``room``→matrix）
            hit = named(legacy)
            if hit is not None:
                return hit

        # 新格式 ``platform:local_id``（以及映射到自身的 irc/twitch/nextcloud）：
        # **平台段本身就是答案**，不需要任何猜测。
        if sep:
            hit = named(head)
            if hit is not None:
                return hit
        return adapters[0]

    def _remember_platform(self, conversation_id: str, platform: str) -> None:
        """记下"这个会话属于哪个适配器" —— 用的是**准确**信息。

        产生这条入站消息的适配器就是它自己（``Inbound.platform``），所以这一步
        没有不确定性。对比 :meth:`_route_by_prefix` 的前缀猜测：猜错的后果是把回复
        发到**另一个平台**，且不报错、只表现为"用户发现回复跑错了地方"。

        A1 迁移之前这里还有第二个来源 —— "调用线程是否等于某适配器的 ``_thread``"。
        迁移后该字段恒为 ``None``，那条路失效了，所以必须靠本方法兜住。
        """
        name = str(platform or "").strip()
        if not name or not conversation_id:
            return
        with self._lock:
            self._conv_adapter[conversation_id] = name

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
            conversation_id = str(inbound.conversation_id or "")
            # ⚠️ 必须**先**记映射、再处理 is_callback 早退 —— 按钮回调那条路
            # 本身会return 掉，若在这里记就漏了它，后续 :meth:`on_callback` 只能靠
            # 前缀去猜是哪个适配器。
            self._remember_platform(conversation_id, inbound.platform)
            if inbound.is_callback:
                # Lane B fires on_inbound(kind="callback") *before*
                # on_callback(); handling it here as well would double-send.
                return
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
                # ⚠️ 命令**绝不**写前落盘。命令由 core 自己就地执行、从不经过
                # prompt()，所以它永远不会走到 mark_delivered —— 那一行会永远留在
                # 收件箱里，于是每次启动都被重放一遍：`/new` 每次重启都重建会话、
                # `/setup` 每次都重发引导。那比要修的丢消息 bug 更糟。
                # 落盘必须留在 else 分支里（tests/test_inbox_wiring.py 锁住这条）。
                self._handle_command(conversation_id, adapter, text)
            else:
                queued = _record_inbound(self._inbox, inbound, text)
                if queued is not None:  # None = 去重命中，已投递过
                    self._enqueue(queued)
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

    # ------------------------------------------------------------------
    # Hooks: 消息流游标（落盘，见 hooks.py 的说明）
    # ------------------------------------------------------------------
    def load_stream_cursor(self, stream_scope: str) -> Optional[int]:
        """实现 ``Hooks.load_stream_cursor``：读回某条消息流上次的位置。

        ``stream_scope`` 不是会话 id，只是 ``state.json`` 里的一个**不透明键**，
        由适配器保证稳定且互不撞车（email 用"账号 + 邮箱"）。

        存的不是整数（被手改坏 / 旧版本写入的别的类型）时按"没有已存位置"
        处理并告警 —— 退化方向必须是"重新走首次启动语义"，不能是"拿着垃圾值
        去算 UID 区间"。
        """
        stored = self.state.get_meta(str(stream_scope), _STREAM_CURSOR_META_KEY, None)
        if stored is None:
            return None
        try:
            return int(stored)
        except (TypeError, ValueError):
            logger.warning("stream cursor %r is not an integer; ignoring it", stored)
            return None

    def save_stream_cursor(self, stream_scope: str, position: int) -> None:
        """实现 ``Hooks.save_stream_cursor``：把某条消息流的位置写进 state。

        写失败只告警、不上抛：位置丢了最坏是重启后重投一封（由写前日志兜底），
        而让异常冒到适配器的轮询线程会把整个收信循环打断。
        """
        try:
            self.state.set_meta(str(stream_scope), _STREAM_CURSOR_META_KEY, int(position))
        except Exception as exc:  # noqa: BLE001 - 落盘失败不该打断收信
            logger.warning(
                "cannot persist the stream cursor for %s (%s); a restart may "
                "re-process messages that were already handled",
                stream_scope, exc,
            )

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
    def _enqueue(self, queued: QueuedPrompt) -> None:
        conversation_id = queued.conversation_id
        with self._lock:
            self._queues.setdefault(conversation_id, []).append(queued)
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
                    queued = queue.pop(0)
                outcome = self._dispatch_prompt(queued)
                if outcome == "busy":
                    # 409 是"还没轮到"，不是失败：不写 failed，重试预算分文未花
                    # （收件箱也没有"撤销 attempting"的转换，所以那一行停在
                    # attempting —— 进程内重投成功后就转 delivered）。
                    # 这一条退回内存队列，等 _flush_queue。
                    with self._lock:
                        queue = self._queues.setdefault(conversation_id, [])
                        queue.insert(0, queued)
                        self._draining.discard(conversation_id)
                    return
        except Exception:
            logger.exception("queue drain failed for %s", conversation_id)
            with self._lock:
                self._draining.discard(conversation_id)

    def _dispatch_prompt(
        self, queued: QueuedPrompt, *, recording_delivery: bool = True,
    ) -> str:
        """Send one queued prompt. Returns ``ok`` / ``busy`` / ``error``.

        The four inbox writes live here and nowhere else, because only this
        method can tell *which* of the three outcomes happened — and the
        ``attempting`` write has to sit immediately against ``client.prompt()``:
        written earlier, a crash during ``create_session`` would leave a row
        that looks "outcome unknown" when in fact the agent never ran.

        ``recording_delivery=False`` skips the writes for the startup recovery
        path, where :func:`~.inbox_recovery.recover_pending` is the sole bookkeeper.
        """
        conversation_id = queued.conversation_id
        adapter = self._adapter_for(conversation_id)
        inbox = self._inbox if recording_delivery else None
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
            if inbox is not None:
                inbox.mark_failed(queued.delivery_id, f"create_session failed: {exc}")
            return "error"

        if inbox is not None:
            inbox.mark_attempting(queued.delivery_id)
        try:
            self.client.prompt(session_id, queued.text)
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
            if inbox is not None:
                inbox.mark_failed(queued.delivery_id, f"prompt failed: {exc}")
            return "error"
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("prompt failed")
            self._send_text(
                conversation_id,
                f"发送失败: {exc}",
                kind="error",
                adapter=adapter,
            )
            if inbox is not None:
                inbox.mark_failed(queued.delivery_id, f"prompt failed: {exc}")
            return "error"

        if inbox is not None:
            inbox.mark_delivered(queued.delivery_id)
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
    # inbox recovery (startup)
    # ------------------------------------------------------------------
    def _recover_inbox(self) -> None:
        """Replay whatever the last crash left in the inbox.

        **为什么夹在 SSE 线程与适配器之间**（两个邻居都不是随便选的）：

        * **在 SSE 线程之后** —— 重放出去的 prompt 必须有人接它的回复。
          ``session.execution.started`` 整条丢掉的话就没有 :class:`Turn`，
          收尾时既不发布结果也不刷队列，用户对这条消息什么都看不到 ——
          于是"修好丢消息"变成"重放出一条没有回复的消息"。
          为此这里等事件流确认连上（有上限）：服务端握手后发的第一帧
          （``server.connected``）到达即证明订阅已建立。
        * **在适配器之前** —— 此刻还没有实况入站，重放不会和用户的新消息
          并发打同一个会话（那种交错会把其中一条变成 409，甚至两次都成功）。
        * **告警仍然送得出去** —— 这正是看上去的矛盾点：``notify`` 需要可用
          的适配器，但扫描跑在 ``adapter.start()`` 之前。两者并不冲突：
          ``Adapter.send`` 是纯出站（``start()`` 只负责入站轮询；见
          ``adapters/telegram.py`` 的 ``send`` 与 ``start``），所以适配器
          已 attach 就能发。真的发不出去时 :meth:`_send_text` 记警告，这里
          再记一条 —— 这段告警只有这一条路，静默丢掉等于没告警。

        ``uncertain`` / ``abandoned`` **已经**被 :func:`recover_pending`
        告警过，这里只记日志，绝不重发。
        """
        inbox = self._inbox
        if inbox is None:
            logger.info(
                "write-ahead inbox disabled (no inbox injected); "
                "a crash during delivery loses that message silently"
            )
            return
        if not self._event_stream_confirmed.wait(
            timeout=_EVENT_STREAM_CONFIRMED_TIMEOUT_SECONDS
        ):
            logger.warning(
                "event stream not confirmed within %.1fs; running inbox recovery "
                "anyway (replays will most likely fail too — see the logs below)",
                _EVENT_STREAM_CONFIRMED_TIMEOUT_SECONDS,
            )

        def dispatch_recovered(queued: QueuedPrompt) -> None:
            """Hand one recovered prompt to opencode.

            Goes through the very same :meth:`_dispatch_prompt` a live message
            takes, which is also why the inbox bookkeeping is split the way it
            is: :func:`~.inbox_recovery.recover_pending` owns the writes here
            (it brackets this call with ``mark_attempting`` / ``mark_delivered``),
            so a second ``mark_failed`` from inside the dispatch would burn two
            retry-budget steps for one failure.

            Anything other than ``ok`` raises so recovery records ``failed`` and
            the next boot retries on the backoff ladder. ``busy`` included: at
            startup there is no in-memory queue to fall back into.
            """
            outcome = self._dispatch_prompt(queued, recording_delivery=False)
            if outcome != "ok":
                raise OpenCodeError(
                    f"replay of {queued.delivery_id} returned {outcome!r}"
                )

        def notify_recovered(conversation_id: str, alert_text: str) -> None:
            """Send one user-visible recovery alert; never let it vanish."""
            if self._send_text(conversation_id, alert_text, kind="text") is None:
                logger.warning(
                    "inbox recovery: could not deliver the alert for %s; "
                    "the user may never learn about it",
                    conversation_id,
                )

        logger.info("write-ahead inbox enabled; scanning for rows left by a crash")
        outcome = recover_pending(
            inbox,
            dispatch=dispatch_recovered,
            notify=notify_recovered,
        )
        logger.info(
            "inbox recovery done: %d replayed, %d uncertain (alerted, NOT replayed),"
            " %d abandoned (alerted)",
            len(outcome.replayed),
            len(outcome.uncertain),
            len(outcome.abandoned),
        )

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
        # ⚠️ 必须解析成绝对路径再发。opencode 的 `POST /api/session` 对
        # `location.directory` 的**相对路径**（含默认的 "."）一律返回 **500 且响应体为空**，
        # 错误信息因此完全丢失，桥只能报"HTTP 500"这种没有信息量的错。
        # 2026-10-03 A4 真实服务端验证时实测：绝对路径 200 / 空串 200 / "." 500（5/5 稳定复现）。
        #
        # 这里做 abspath 而不是要求用户配绝对路径，有两个理由：
        #   1. `opencode_directory` 的默认值就是 "."（见 config.py），语义是"当前目录"——
        #      把"当前目录"解析成绝对路径是它本来的意思，不该让用户为默认值买单；
        #   2. 上面那个 `or "."` 兜底意味着即使配置为空也必然踩中，不解析就必然失败。
        #
        # 回环测试抓不到这个 bug：测试都传绝对路径或临时目录，只有真实默认配置会中招。
        directory = os.path.abspath(directory)
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
            "model": self._cmd_model,
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

    def _cmd_model(self, conversation_id: str, adapter: Adapter, args: str) -> None:
        """``/model`` —— 逻辑在 :mod:`opencode_bridge.session_model`，这里只转发。"""
        reply = self.model_command.reply_for(conversation_id, args)
        self._send_text(
            conversation_id, reply.text, kind=reply.kind, adapter=adapter,
            session_id=reply.session_id,
        )

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
                # A first frame means the subscription is live. Startup recovery
                # blocks on this before replaying (see :meth:`_recover_inbox`).
                self._event_stream_confirmed.set()
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

    def _note_unhandled_event(self, event_name: str) -> None:
        """记账一个"事件名认识但没有 handler"的事件，并**按时间节流**打日志。

        为什么必须按时间节流：曾以为"同一个名字会连续出现"，于是用
        "事件名变了没有"来决定要不要打汇总。**实测证明这个前提不成立**——
        事件是多路交替的（`reasoning.delta` 一边涨一边夹着几十种其它事件），
        于是每来一个新事件名就打一行含 30+ 项的全量汇总，日志被自己的
        "降噪机制"冲垮（实测半小时内刷出上千行）。

        所以改成：**首次见到打一行，之后每 60 秒最多打一行汇总**，
        噪音有了硬上限。汇总行只取计数最多的若干项，避免一行几百字符。
        """
        previous_count = self._unhandled_event_names.get(event_name, 0)
        self._unhandled_event_names[event_name] = previous_count + 1

        now = time.monotonic()
        if previous_count == 0:
            logger.info(
                "收到未处理事件 %r（已记账；若你正在等某条回复却没下文，先查这里。"
                "若是 opencode 升版新增的事件，需在 _handlers 里补处理器）",
                event_name,
            )
            self._last_unhandled_log_at = now
            return

        if (now - self._last_unhandled_log_at) < self._UNHANDLED_LOG_INTERVAL_SECONDS:
            return
        self._last_unhandled_log_at = now

        top = sorted(
            self._unhandled_event_names.items(), key=lambda item: -item[1]
        )[:10]
        logger.info(
            "未处理事件累计（%d 种，仅列前 10）：%s",
            len(self._unhandled_event_names),
            ", ".join("%s x%d" % (name, count) for name, count in top),
        )

    def _dispatch(self, event: dict) -> None:
        event_name = str(event.get("type") or "")
        handler = self._handlers.get(event_name)
        if handler is None:
            # 区分两种"没有 handler"，这是实测踩出来的教训：
            #
            # (A) **认识但故意不处理**（例如思考流 `session.reasoning.*` 不该上IM）
            #     -> 完全静默。它们是协议的一部分，不是我们漏实现了什么。
            # (B) **不认识**（可能是 opencode 升版后新增了我们没跟上的事件）
            #     -> 记账 + 打日志，但要**严格节流**。
            #
            # 曾经这里没区分，把 `session.reasoning.delta`（实测 30 分钟 2800+ 条）
            # 当成"未知事件"反复记账；又因为多路事件交替出现，"同一名字连续"的
            # 判断永远不成立，于是每次都打一行含 30+ 项的全量汇总 ——
            # **为了避免噪音淹掉真信号，结果自己制造了噪音**，把日志冲垮。
            if event_name not in self._KNOWN_BUT_IGNORED_EVENTS:
                self._note_unhandled_event(event_name)
            return
        data = _as_dict(event.get("data"))
        if not self._owns_session_event(event_name, data):
            # 已知事件名，但**属于别的会话** —— 这是正常情况，不是错误。
            #
            # `/api/event` 是**全服务器广播**：同机跑的其它 agent 会话（本项目里
            # 就包括开发者自己正在跑的 opencode 会话）的事件同样会推过来。
            # 实测曾刷出 `permission request for unknown session ses_effe80...`，
            # 那是我们自己的会话，与 Telegram 毫无关系。
            #
            # 所以这里与上面的"未知事件名"必须区别对待：
            #   未知**名字** = 可能是我们漏实现了什么 -> 记账 + 打日志
            #   已知名字但**别人的会话** = 正常 -> debug 级，不惊动人
            logger.debug(
                "忽略非本桥会话的事件 %s (session=%s)", event_name, _session_id(data)
            )
            return
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

    #: 高频、按会话归属过滤的事件类型。`/api/event` 是**全服务器广播**，同机其它
    #: agent 会话（包括开发者自己正在跑的 opencode 会话）的 delta 会以每秒数百条
    #: 的量级推过来；不过滤会把日志和 CPU 全烧在无关数据上。
    #:
    #: **刻意只包含高频事件**：`permission.asked` 与 `session.execution.*` 不在其中。
    #: 它们低频，且对它们而言"未知会话"是**有意义的信息** —— 放行让原有的
    #: warning 继续暴露真问题（比如竞态导致会话没登记上）。若一律过滤，
    #: 一次竞态就会让 agent 永远等不到审批，而日志里什么都看不到。
    _HIGH_VOLUME_SESSION_EVENTS = frozenset({
        "session.text.delta",
        "session.reasoning.delta",
        "session.step.started",
        "session.step.streamed",
        "session.step.ended",
        "session.tool.input.started",
        "session.tool.input.delta",
        "session.tool.input.ended",
        "session.tool.called",
        "session.tool.progress",
        "session.tool.success",
        "session.tool.failed",
    })

    #: **认识但故意不处理**的事件：完全静默，不记账、不打日志。
    #:
    #: 这些是协议的一部分、不是我们漏实现了什么。把它们当"未知事件"记账是错的
    #: ——实测 `session.reasoning.delta` 半分钟就有 2800+ 条（思考流不该上IM），
    #: 足以把真正的错误彻底淹掉。
    #:
    #: 判断标准：**IM 里该不该出现**。思考过程、shell 生命周期、工具入参/出参、
    #: 配额与用量统计，对聊天用户都没有意义，就不该走"未知事件"那套记账。
    _KNOWN_BUT_IGNORED_EVENTS = frozenset({
        # 思考流：不上 IM
        "session.reasoning.started",
        "session.reasoning.delta",
        "session.reasoning.ended",
        # 工具细节：只有简短的"正在做什么"提示才有意义，进出参全文没有
        "session.tool.input.delta",
        "session.tool.progress",
        "session.step.streamed",
        "session.synthetic",
        "session.instructions.updated",
        # 会话元信息
        "session.viewed",
        "session.usage.updated",
        "session.metadata.updated",
        "session.permissions",
        "session.renamed",
        "session.agent.selected",
        "session.model.selected",
        "session.moved",
        "session.inbox.enqueued",
        "session.inbox.delivered",
        "session.inbox.cancelled",
        "session.inbox.delivery.changed",
        "session.created",
        "session.deleted",
        "session.forked",
        # 注意：`session.retry.scheduled` **不在**这里 —— 它有处理器
        # （`_on_retry_scheduled`），放进本集合会让人误以为它被忽略。
        # 压缩（上下文自动压缩）：值得单独提示，但当前实现里没有对应 handler
        "session.compaction.started",
        "session.compaction.delta",
        "session.compaction.ended",
        "session.compaction.failed",
        "session.revert.staged",
        "session.revert.cleared",
        "session.revert.committed",
        "session.shell.started",
        "session.shell.ended",
        "session.skill.activated",
        # 连接与全局状态：与本桥无关
        "server.connected",
        "provider.updated",
        "model.updated",
        "agent.updated",
        "command.updated",
        "config.updated",
        "skill.updated",
        "plugin.updated",
        "reference.updated",
        "project.updated",
        "filesystem.changed",
        "credential.updated",
        "credential.switched",
        "integration.updated",
        "models-dev.refreshed",
        "websearch.updated",
        "worktree.updated",
        "worktree.resolved",
        "installation.updated",
        "installation.update-available",
        "vcs.branch.updated",
        "mcp.status.changed",
        "mcp.resources.changed",
        "location.shutdown",
        "permission.replied",
    })

    #: 未知事件记账日志的最小间隔（秒）。多路事件交替出现时"同一个名字连续"
    #: 这个前提**不成立**，所以不能靠事件名判断该不该打；一律按时间节流，
    #: 保证噪音有硬上限。
    _UNHANDLED_LOG_INTERVAL_SECONDS = 60.0

    def _owns_session_event(self, event_name: str, data: dict) -> bool:
        """这个事件是否属于本桥关心的会话。

        判据是"**state.json 里登记过**，或**当前有活跃 turn**"。
        拿不到 sessionID 时返回 True，交给 handler 自行处理
        （`session.retry.scheduled` 没有 sessionID，它靠 assistantMessageID
        反查，反查不到会安静返回并记debug 日志）。
        """
        if event_name not in self._HIGH_VOLUME_SESSION_EVENTS:
            return True  # 低频生命周期事件：放行，让"未知会话"的警告有意义
        session_id = _session_id(data)
        if not session_id:
            return True
        with self._lock:
            return session_id in self._sid_conv or session_id in self._turns

    def _session_for_assistant(self, assistant_id: str) -> str | None:
        """按 ``assistantMessageID`` 反查它属于哪个会话。

        为什么需要反查：v2.0.22 里 `session.retry.scheduled` 的 `data` 只有
        `assistantMessageID / attempt / at / error`，**没有 `sessionID`**，
        而其余 `session.*` 事件都有。不能靠会话 id 路由，就只能按
        assistantMessageID 在活跃 turn 里找。

        turn 数量是"当前并发对话数"，很小，线性扫足够；找不到就返回 None，
        交给调用方记账而不是静默丢弃。
        """
        if not assistant_id:
            return None
        with self._lock:
            for session_id, turn in self._turns.items():
                if assistant_id in turn.parts:
                    return session_id
        return None

    def _on_retry_scheduled(self, data: dict) -> None:
        """`session.retry.scheduled` —— 模型重试时给用户一个提示，别干等着。

        取代原先挂在 `session.status{type:"retry"}` 上的实现：那个事件在
        v2.0.22 **从不发布**，所以那段代码从来没跑过；而它想提供的
        「正在重试」反馈本身是有价值的，于是改挂到真实存在的事件上。
        """
        session_id = _session_id(data)
        if not session_id:
            session_id = self._session_for_assistant(
                str(data.get("assistantMessageID") or "")
            )
        if not session_id:
            logger.debug(
                "retry.scheduled 找不到对应会话 (assistantMessageID=%r)",
                data.get("assistantMessageID"),
            )
            return
        conversation_id = self._conversation_for(session_id)
        if not conversation_id:
            return
        attempt = data.get("attempt", "?")
        error = _as_dict(data.get("error"))
        reason = _clean(error.get("message") or error.get("type") or "")
        text = f"⏳ 重试中 (attempt {attempt})" + (f": {reason}" if reason else "")
        with self._lock:
            turn = self._turns.get(session_id)
            handle = turn.progress_handle if turn else None
        if handle is None:
            return
        self._edit_progress(conversation_id, handle, text, session_id)

    def _on_execution_interrupted(self, data: dict) -> None:
        """`session.execution.interrupted` —— 但 ``reason == "shutdown"`` **不算**结束。

        opencode 服务重启时会保留 claim 并**续跑**这一轮。源码依据
        （v2.0.22 `packages/core/src/session/projector.ts` 的 `projectIdle`）：
        `reason === "shutdown"` 时直接 return，不产生 idle 投影。

        若把它当结束处理，后果是双重的：
          1. 还没写完的 turn 被提前 finalize，把半截内容当最终答复发出去；
          2. turn 已从 `_turns` 弹掉，续跑后 `session.text.delta` 会**另建一个
             turn**，于是同一条回复被发两遍。
        多个第三方消费者（openchamber / waku）都专门为这条踩过坑。
        """
        reason = str(data.get("reason") or "")
        if reason == "shutdown":
            logger.info(
                "execution interrupted by shutdown (session=%s)：这一轮会被续跑，"
                "不当作结束",
                _session_id(data),
            )
            return
        self._finalize_session(_session_id(data))

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

        触发源只有两个（v2.0.22 源码核实）：
          - ``session.execution.succeeded``
          - ``session.execution.interrupted``，**且** ``reason != "shutdown"``
            （shutdown 会被续跑，见 :meth:`_on_execution_interrupted`）

        ⚠️ 曾经还把 ``session.idle`` 与 ``session.status{type:"idle"}`` 当触发源，
        但前者在源码里已标 ``// deprecated``、后者全代码库零处发布 ——
        于是真实环境**永远等不到收尾**，症状是用户只看到 `⏳ 处理中…`。
        写这段注释时全仓 1409 条测试都是绿的，而它们正是拿那两个不存在的事件
        当触发源：**测试在保护一个虚构的契约**。

        幂等：turn 在锁内 pop，所以重复触发只会多刷一次（通常为空的）队列。
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

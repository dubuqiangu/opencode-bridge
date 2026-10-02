"""Lane B — Slack adapter (CONTRACT.md §2.3). Standard library only.

v1 scope: outbound messages (``chat.postMessage`` / ``chat.update``) fully
work; inbound is Slack **Socket Mode**（WSS 长连接，需另配 app_token）。

A1：WS 连接 / 收包循环 / 退避 / 线程 / ``stop()`` 语义已迁到
:class:`~opencode_bridge.transport.WebSocketTransport`。本文件只留 Slack 语义：
envelope 分发、ack 时机、事件过滤、授权闸门、出站 HTTP、以及 Slack 特有的
``ok:false`` 错误分类。

⚠️ ``conversation_id`` 前缀**本轮刻意仍是 ``channel:``**，不要"顺手改好"
------------------------------------------------------------------------
``channel`` 在 :data:`identity.LEGACY_PREFIXES` 里的值是 ``None`` ——
**歧义前缀**，被 slack / discord / mattermost **三家共用**，光看字符串**判不出**
来源（对比 ``chat`` → ``telegram`` 至少能确定指向谁）。

把 :meth:`SlackAdapter._conversation_id` 改成 ``identity.format_id("slack", ...)``
会改变 ``conversation_id`` 的字符串格式，而 :class:`~opencode_bridge.state.StateStore`
拿它当**不透明键**存 ``conversation_id ↔ session_id`` 映射 —— 于是**已落盘
``state.json`` 里的所有 ``channel:`` 键会一次性变成孤儿**，用户会在迁移后一次性
"忘记"所有历史会话映射。这种故障**不报错**，只表现为"agent 突然记错上下文"，
比直接失败难查得多。

前缀切换必须是一个**单独的变更**，前置条件有两个：

1. 必须与 ``state.py`` 的键迁移（旧键 → 新键的显式重写 + 版本门控）**一起**发；
2. 歧义前缀还**额外**需要"是三家中的哪一家"这条线索 ——
   :func:`identity.normalize` 对 ``channel:`` 不给 ``platform_hint`` 就抛
   :class:`~opencode_bridge.identity.AmbiguousConversationId`，无法静默猜。

在那之前 ``_conversation_id`` 必须逐字节保持 ``channel:`` 前缀 ——
:func:`SlackAdapter._conversation_id` 里有一条**显式断言**把它钉死，
``tests/test_slack.py`` 里另有一条独立用例（防有人把断言删掉）。

Socket Mode 四个不能省的点
--------------------------
1. **WSS URL 约 1 小时过期**：Slack 会在过期前发 ``{"type":"disconnect"}``
   （``reason=warning`` / ``refresh_requested``），也可能不给预警。所以收到它
   必须**主动重连并重新取 URL**，而不是被动等对端关 socket ——
   见 :meth:`SlackAdapter._on_frame`（抛
   :class:`~opencode_bridge.transport.ReconnectNow`，传输层立刻重连且**不退避**）。
   这也顺带保证了第二件事：**disconnect 之后旧连接上到达的消息被丢弃** ——
   传输层会关掉那条连接、绝不再从它读任何东西（否则等于静默丢消息）。
2. **每个 envelope 都必须 ack**（含 ``hello`` 与被过滤掉的无关事件），漏 ack
   Slack 会**无限重发**同一条。**顺序铁律：先 ack 再过滤**，见
   :meth:`SlackAdapter._handle_envelope`。这是迁移最容易踩的坑：一旦把 ack
   挪到"通过过滤之后"，提前 ``return`` 的分支就会把 ack 一起吃掉。
3. **授权闸门在最前**，但被闸门丢弃的 envelope **仍要 ack**（同一条 ack 铁律
   在过滤之前就已完成，所以两件事不冲突）。
4. **出站错误大量是 HTTP 200 + ``ok:false``**：Slack 用 ``ok:false`` + 字符串
   ``error`` 报失败，"HTTP 没报错"不等于成功。见 :func:`_classify_slack_error`
   ——它不能被"HTTP 成功就没事"的直觉吞掉。
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
from ..transport import ReconnectNow, WebSocketTransport
from .base import Adapter, classify_http, register

logger = logging.getLogger("opencode_bridge.adapters.slack")

__all__ = ["SlackAdapter"]

API_BASE = "https://slack.com/api"
MESSAGE_LIMIT = 40000        # Slack hard-truncates beyond ~40k characters
MIN_SEND_INTERVAL = 1.2      # per-conversation send/edit throttle (seconds)
SOCKET_TIMEOUT = 30.0
MAX_RETRY_AFTER = 60.0
RECONNECT_DELAY = 3.0        # Socket Mode 断开后的重连间隔（秒）

#: 旧的 ``conversation_id`` 前缀（A2 之后**仍是**它，见模块 docstring）。
#: 单独提成常量，是为了 :meth:`SlackAdapter._conversation_id` 里能断言"产物就是这个"。
CONVERSATION_PREFIX = "channel:"


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
    """Slack Web API adapter（Socket Mode 入站 + ``chat.postMessage`` 出站）。

    线程与连接归 :class:`~opencode_bridge.transport.WebSocketTransport`；
    :attr:`running` / :meth:`stop` 是它的代理。

    ⚠️ ``_conversation_id`` 刻意仍产出 ``channel:`` 前缀 —— 见模块 docstring
    「``conversation_id`` 前缀本轮刻意仍是 ``channel:``」一节（切换的前置条件是
    ``state.py`` 的键迁移，且歧义前缀额外需要知道是三家中的哪一家）。
    """

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
        self._transport: Optional[WebSocketTransport] = None
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
    # 传输层
    # ------------------------------------------------------------------
    @property
    def transport(self) -> Optional[WebSocketTransport]:
        """当前传输层（``start()`` 之后才有；测试与 :meth:`_connection` 看它）。"""
        return self._transport

    @property
    def running(self) -> bool:
        """消费线程是否活着（代理到传输层）。

        ⚠️ 基类的 :attr:`Adapter._thread` 现在**恒为 None**（入站线程由传输层持有，
        名字是 ``transport:slack``）。``core.py`` 的 ``_adapter_for`` 已经能靠前缀/
        映射找到本适配器（见 ``tests/test_routing.py`` 的
        ``test_migrated_adapter_without_thread_still_routes``），不依赖线程匹配。
        """
        transport = self._transport
        return transport is not None and transport.running

    def _connection(self) -> Any:
        """当前 WS 连接（未连接时 ``None``；由传输层持有）。"""
        transport = self._transport
        return transport.connection if transport is not None else None

    def _make_transport(self) -> WebSocketTransport:
        """构造本次运行用的传输层（测试注入点：退避接线值）。

        退避**逐字对齐迁移前**的 ``_inbound_loop`` 末尾那次
        ``self._stop_event.wait(RECONNECT_DELAY)``：

        * ``min_backoff = max_backoff = RECONNECT_DELAY`` ⇒ 退避**恒定 3s**。
          迁移前只有一个常数、**没有指数增长**，所以 max 必须与 min 同值，
          否则会静默变成 3s → 6s → 12s（= 行为变更）。
        * ``reset_after=0`` ⇒ 只要连上过就重置退避（连上即重置 = 既有语义）；
          传正数会变成"稳定存活 N 秒才重置"，同样是行为变更。
        * 退避等待用 ``Event.wait`` 而不是 ``sleep``，所以 :meth:`stop` 能立刻打断。
        """
        # ⚠️ 刻意读**模块全局**而不是类属性：迁移前就是运行时读全局
        # ``RECONNECT_DELAY``，既有测试（tests/test_adapters.py）靠 monkeypatch
        # 那个全局来缩短等待。改成类属性会让这些用例重新等满 3s。
        return WebSocketTransport(
            self._open_socket,
            on_message=self._on_frame,
            name="slack",
            min_backoff=RECONNECT_DELAY,
            max_backoff=RECONNECT_DELAY,
            reset_after=0.0,
        )

    def _open_socket(self) -> Any:
        """建一次会话：取 WSS URL → 建连。异常一律抛给传输层退避重试。

        对应迁移前 ``_inbound_loop`` 开头的三行（``_open_socket_url`` →
        ``_make_ws`` → 记 "Socket Mode connected"），逐字保留顺序与日志。
        """
        url = self._open_socket_url()
        ws = self._make_ws(url)
        logger.info("slack: Socket Mode connected")
        return ws

    def _on_frame(self, conn: Any, frame: Any) -> None:
        """传输层的 ``on_message`` 钩子：一条**原始帧** → ack → 过滤 → 投递。

        Slack 的全部入站语义都在 :meth:`_handle_envelope` 里（**先 ack 再过滤**），
        本方法只多一件事：把"服务端要求换连接"翻译成
        :class:`~opencode_bridge.transport.ReconnectNow`。

        抛它 = **立刻重连、不退避**，且传输层会**关掉这条旧连接**（于是旧连接上
        后续到达的消息被丢弃，而不是被处理一半）。这正是 Socket Mode 需要主动
        重连的原因：WSS URL 约 1 小时过期（见模块 docstring）。
        """
        if self._handle_envelope(conn, frame):
            return
        raise ReconnectNow("slack: server requested reconnect (disconnect envelope)")

    def _on_event(self, item: Any) -> None:
        """传输层的 ``on_event`` 回调：**刻意是 no-op**。

        :class:`~opencode_bridge.transport.WebSocketTransport` 在每条原始帧到达时
        先调 ``on_message(conn, frame)``、**再**把同一帧交给 ``on_event``。Slack 的
        ack 与分发已经在 :meth:`_on_frame` 里做完，所以这里必须什么都不做 ——
        否则同一条消息会被投递两次（core 会当成两条消息，bot 也会回两次）。
        保留这个空实现（而不是给 ``start()`` 传 ``lambda _: None``）是为了让
        "同一帧会被两个钩子看到"这件事在代码里是**显式可见**的。
        """

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """启动入站（Socket Mode）。缺 app_token 时降级为"只发出站"。

        缺 ``app_token`` 时**只告警并返回**（不许变成静默失败）：
        ``required_tokens`` 把它列进入站必需项，状态视图据此把这种情况报成
        "未配置 / 只能发不能收"（见 ``tests/test_cli.py``）。
        """
        if not self.bot_token:
            logger.warning("slack: bot_token missing; adapter not started")
            return
        if not self.app_token:
            logger.warning(
                "slack: app_token (xapp-) missing; inbound disabled, outbound only"
            )
            return
        # 迁移前漏了这一行：stop() 置位后同一个实例再 start()，入站循环会立刻
        # 退出（静默不工作）。matrix / telegram / irc 迁到传输层时都补上了。
        # 传输层的 start() 只清它自己的停止位，不碰适配器这个（_throttle 依赖它）。
        self._stop_event.clear()
        transport = self._make_transport()
        self._transport = transport
        transport.start(self._on_event)

    def stop(self) -> None:
        """置停止位 → 关 WS 唤醒阻塞的 ``recv()`` → join 线程（**幂等**）。

        顺序不能反：Socket Mode 的 ``recv()`` 可能阻塞远久于基类 ``join`` 的 5s
        （WS 读超时 :data:`SOCKET_TIMEOUT` = 30s），不先把连接关掉把那个阻塞唤醒，
        每次 stop 都会白等满超时。迁移前这段是手写的（先 ``self._ws`` 取出来
        ``close()``、再 ``super().stop()``），现在它在
        :meth:`~opencode_bridge.transport.WebSocketTransport._close_conn` 里
        （先 ``shutdown`` 唤醒读、再 ``close``），但**语义没变**。
        """
        super().stop()                      # 置停止位（_throttle 依赖它）
        transport, self._transport = self._transport, None
        if transport is not None:
            transport.stop()

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

    def _handle_envelope(self, ws, raw: str) -> bool:
        """处理一个 envelope，返回 ``False`` 表示应立即重连。

        本方法是 Slack 入站的**唯一**语义入口：迁移后它仍由传输层的
        ``on_message`` 钩子（:meth:`_on_frame`）逐帧调用，**没有**第二份实现。

        **先 ack 再过滤**（漏 ack 会让 Slack 无限重发）：ack 在下面第一次 ``return``
        之前发出，所以 ``hello``、被事件类型过滤掉的、被授权闸门丢弃的 envelope
        **都**被 ack 过了。迁移时最容易丢的就是这一条 —— 把 ack 挪到过滤之后，
        每个提前 ``return`` 的分支都会变成一次漏 ack（Slack 随后无限重发）。

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
    # Outbound
    # ------------------------------------------------------------------
    @staticmethod
    def _channel_id(conversation_id: str) -> Optional[str]:
        raw = conversation_id
        if raw.startswith(CONVERSATION_PREFIX):
            raw = raw[len(CONVERSATION_PREFIX):]
        return raw or None

    @staticmethod
    def _conversation_id(channel_id: Any) -> str:
        """``channel_id`` → ``channel:...``。

        ⚠️ **不要**改成 ``identity.format_id("slack", channel_id)``。

        ① ``channel:`` 是**歧义前缀**：:data:`identity.LEGACY_PREFIXES["channel"]``
        的值是 ``None``（slack / discord / mattermost 三家共用），归一时**必须**
        额外给 ``platform_hint``，否则 :func:`identity.normalize` 抛
        :class:`~opencode_bridge.identity.AmbiguousConversationId`。
        ② 切换会改变 ``conversation_id`` 的字符串格式，而
        :class:`~opencode_bridge.state.StateStore` 拿它当**不透明键**存
        ``conversation_id ↔ session_id`` —— 已落盘 ``state.json`` 里的键会全部
        变孤儿，用户一次性丢失会话映射，且**不报错**。

        前置条件：必须与 ``state.py`` 的键迁移（旧键 → 新键的显式重写 + 版本门控）
        **一起**发。见模块 docstring「``conversation_id`` 前缀本轮刻意仍是
        ``channel:``」。下面的断言就是**防呆**：后续迁移里若有人顺手把它切成
        ``slack:``，这里会立刻炸（而不是等到用户报"agent 记错上下文"）。
        """
        cid = f"{CONVERSATION_PREFIX}{channel_id}"
        # ⚠️ 这里比的是**字面量** ``channel:``，不是 ``CONVERSATION_PREFIX`` ——
        # 用常量比会变成恒真（改了常量就一起改了），等于没钉。
        # （``tests/test_slack.py`` 里有一条用例专门证明这条断言不是恒真。）
        assert cid.startswith("channel:"), (
            f"conversation_id 前缀被改动了: {cid!r}（本轮必须保持 channel:；"
            "见模块 docstring「conversation_id 前缀本轮刻意仍是 channel:」）"
        )
        return cid

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
            # Slack 大量用 HTTP 200 + ok:false 报失败 —— 必须查 ok，不能只看状态码。
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
        # 同上：ok:false 才是判据（HTTP 200 不代表成功）。
        if not data.get("ok"):
            logger.warning(
                "slack: chat.update failed (HTTP %s): %s", status, data.get("error")
            )
            return False
        return True

    def answer(self, query_id: str, text: str = "") -> None:
        """Ack callback path —— Socket Mode 下 ack 已在 :meth:`_handle_envelope` 里发出。

        Slack 的交互确认走的是**信封 ack**（``{"envelope_id": ...}``，见
        :meth:`_handle_envelope`），不是 Telegram 那种单独的 ``answerCallbackQuery``。
        所以这里是 no-op，**不是**"漏实现"：按钮（``supports_inline_buttons``）
        本来就没实现，``blocks`` 一旦落地，接缝是同一个 ``on_message`` 钩子。
        """
        return None

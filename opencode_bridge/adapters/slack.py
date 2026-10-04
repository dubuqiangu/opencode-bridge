"""Lane B — Slack adapter (CONTRACT.md §2.3). Standard library only.

v1 scope: outbound messages (``chat.postMessage`` / ``chat.update``) fully
work; inbound is Slack **Socket Mode**（WSS 长连接，需另配 app_token）。

A1：WS 连接 / 收包循环 / 退避 / 线程 / ``stop()`` 语义已迁到
:class:`~opencode_bridge.transport.WebSocketTransport`。本文件只留 Slack 语义：
envelope 分发、ack 时机、事件过滤、授权闸门、出站 HTTP、以及 Slack 特有的
``ok:false`` 错误分类。

``conversation_id`` 前缀：已从 ``channel:`` 切到 ``slack:``（歧义前缀，靠划分回读）
------------------------------------------------------------------------
``channel`` 在 :data:`identity.LEGACY_PREFIXES` 里的值是 ``None`` ——
**歧义前缀**，被 slack / discord / mattermost **三家共用**，光看字符串**判不出**
来源（对比 ``chat`` → ``telegram`` 至少能确定指向谁）。所以切前缀不像
telegram / matrix 那样只要配一次键迁移就完事：**旧键永远迁不了**
（:mod:`opencode_bridge.state` 对歧义前缀只捕获、不归一），
必须靠"读取时回退"把历史会话接上。

于是本适配器做三件事：

1. :meth:`SlackAdapter._conversation_id` 产出统一的 ``slack:<channel_id>``；
2. 声明 :attr:`SlackAdapter.legacy_conversation_prefix` = ``channel:``（只为让读侧
   知道历史上那个前缀长什么样），并让 :meth:`SlackAdapter._channel_id`
   **继续认**它；
3. 声明 :attr:`SlackAdapter.local_id_pattern` = :data:`SLACK_LOCAL_ID_PATTERN`
   —— **本平台自己的** local id 文法。

回退读发生在哪一步：:mod:`opencode_bridge.conversation_keys` 只在**发起查询的平台
就是 slack**、且那个 local id 符合**本文件**声明的文法时，才去试一次
``channel:<local>``。Slack / discord / mattermost 三家的文法两两不相交，于是
"这个 ``channel:`` 键归谁"是**能确定的判断**而不是猜 —— 三家同时挂载时三份历史
会话全部存活、互不串台；而曾经同时跑过 slack + discord、后来删掉 discord 的用户，
也不会把 discord 的键认领回来（"曾经是否共存过"这件事盘上根本没记过，任何按
挂载情况仲裁的方案都判不了它）。文法推导见 :data:`SLACK_LOCAL_ID_PATTERN`，
判定入口见 :meth:`~opencode_bridge.adapters.base.Adapter.owns_local_id`。

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
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Any, List, Optional, Tuple

from ..hooks import Hooks, Inbound, MsgHandle, Outbound, SendError
from ..identity import format_id
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

#: Slack local id 的文法：**首字符是大写字母**，其余大写字母或数字。
#:
#: Slack 的实体 id 一律大写，且用首字母区分类型（``C`` 公频 / ``D`` 私聊 /
#: ``G`` 私频 / ``U`` 用户 / ``W`` workspace），真实长度 9~11 位。
#:
#: ⚠️ 下限取 **6** 位（= 首字母 + 5）而不是更紧：不相交性**只**取决于「首字符是
#: 大写字母」这一条，下限松紧都改不了它。松一点最多多认一个根本不属于别家的形状，
#: 紧一点却可能让真实会话**悄悄接不回来**（症状是"用户莫名其妙丢了历史会话"）。
#: :mod:`opencode_bridge.state` 的对照表把这一格写成 ``{7,}``，而同节举的例子
#: ``C01ABC`` 只有 6 位 —— 两处对不上；这里按那个例子取 6。
SLACK_LOCAL_ID_PATTERN = re.compile(r"^[A-Z][A-Z0-9]{5,}$")


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

    ``_conversation_id`` 产出统一格式 ``slack:<channel_id>``（已从 ``channel:`` 切过来），
    ``_channel_id`` 仍认旧前缀 —— 见模块 docstring「``conversation_id`` 前缀」一节。
    """

    name = "slack"
    label = "Slack"
    max_message_length = MESSAGE_LIMIT          # chat.postMessage 文本上限 40000
    supports_inbound = True                    # Socket Mode 入站（需另配 app_token）
    supports_inline_buttons = False            # blocks 未实现
    supports_media = False
    #: ``chat.update`` 真能改写已发消息（见 :meth:`edit`），所以占位气泡发得。
    supports_message_edit = True
    # 入站必需 app_token：缺它会静默降级为"只发出站"，所以必须声明出来，
    # 让 --status / --setup --json 不会把这种情况报成"已配置"。
    required_tokens = ("bot_token", "app_token")

    min_interval = MIN_SEND_INTERVAL

    #: 迁移前 Slack 用的 ``conversation_id`` 前缀（**歧义**：三家共用）。
    #: 值刻意写**字面量**，不用任何常量拼 —— 拼了就跟"防走偏断言"一样会恒真。
    legacy_conversation_prefix = "channel:"
    #: 本平台的 local id 文法（Slack 自己的 API 属性，见
    #: :data:`SLACK_LOCAL_ID_PATTERN` 的推导）。判定入口是基类的
    #: :meth:`~opencode_bridge.adapters.base.Adapter.owns_local_id`。
    local_id_pattern = SLACK_LOCAL_ID_PATTERN
    #: ``conversation_id`` 的合法前缀：当前格式 + 旧别名。
    #: :meth:`_channel_id` 按这个列表剥前缀，所以**旧前缀必须继续认** ——
    #: 写前收件箱把 ``conversation_id`` 持久化在盘上，切换前写入、切换后才重放的
    #: 未投递消息带着 ``channel:``，认不出来就等于把那些回复永久丢弃。
    _CONVERSATION_PREFIXES = ("slack:", legacy_conversation_prefix)

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
        """``conversation_id`` → Slack channel id；空的一律返回 ``None``。

        裸 channel id（``"C1"``）也认，切换前后的两种前缀都认 ——
        理由见 :attr:`SlackAdapter._CONVERSATION_PREFIXES`。
        """
        raw = str(conversation_id or "")
        for prefix in SlackAdapter._CONVERSATION_PREFIXES:
            if raw.startswith(prefix):
                raw = raw[len(prefix):]
                break
        return raw or None

    @staticmethod
    def _conversation_id(channel_id: Any) -> str:
        """``channel_id`` → ``slack:...``（统一 ``platform:local_id`` 格式）。

        ① 歧义前缀 **永不迁移**：:mod:`opencode_bridge.state` 对 ``channel:`` 只捕获、
        不归一（归属从未被持久化，推不出来），所以历史会话靠
        :mod:`opencode_bridge.conversation_keys` 的"各家 local id 文法不相交"
        在**读取时**接回来 —— 判据是 :attr:`SlackAdapter.local_id_pattern`。
        ② 反向解析（:meth:`_channel_id`）必须继续认 ``channel:``：写前收件箱把
        ``conversation_id`` 持久化在盘上，切换前写入、切换后才重放的未投递消息带着
        旧前缀，认不出来就等于把那些回复永久丢弃。
        """
        return format_id("slack", channel_id)

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
        chunks: List[str] = split_text(out.text, self.effective_max_length, prefix_fmt="")
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

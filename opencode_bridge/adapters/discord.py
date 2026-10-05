"""Lane B — Discord adapter (CONTRACT.md §2.3). Standard library only.

Outbound (``POST /channels/{id}/messages`` / ``PATCH .../{message_id}``) 与
**入站 Gateway v10 WebSocket**（tasks.md T2.2）都可用。

A1：WS 连接 / 收包循环 / 退避 / 线程 / ``stop()`` 语义已迁到
:class:`~opencode_bridge.transport.WebSocketTransport`。本文件只留 Discord 语义：
HELLO 协商、心跳（周期 + ACK 监测）、opcode 分发、close code 决策、事件过滤、
授权闸门、出站 REST。生产连的是 :mod:`opencode_bridge.ws`（T2.0，纯标准库自研的
最小 RFC 6455 客户端），因此本文件不引入任何第三方依赖。

``conversation_id`` 前缀：已从 ``channel:`` 切到 ``discord:``（歧义前缀，靠划分回读）
------------------------------------------------------------------------
``channel`` 在 :data:`identity.LEGACY_PREFIXES` 里的值是 ``None`` ——
**歧义前缀**，被 slack / discord / mattermost **三家共用**，光看字符串**判不出**
来源（对比 ``chat`` → ``telegram`` 至少能确定指向谁）。所以切前缀不像
telegram / matrix 那样只要配一次键迁移就完事：**旧键永远迁不了**
（:mod:`opencode_bridge.state` 对歧义前缀只捕获、不归一），
必须靠"读取时回退"把历史会话接上。

于是本适配器做三件事：

1. :meth:`DiscordAdapter._conversation_id` 产出统一的 ``discord:<channel_id>``；
2. 声明 :attr:`DiscordAdapter.legacy_conversation_prefix` = ``channel:``（只为让读侧
   知道历史上那个前缀长什么样），并让 :meth:`DiscordAdapter._channel_id`
   **继续认**它；
3. 声明 :attr:`DiscordAdapter.local_id_pattern` = :data:`DISCORD_LOCAL_ID_PATTERN`
   —— **本平台自己的** local id 文法（snowflake）。

回退读发生在哪一步：:mod:`opencode_bridge.conversation_keys` 只在**发起查询的平台
就是 discord**、且那个 local id 符合**本文件**声明的文法时，才去试一次
``channel:<local>``。Slack / discord / mattermost 三家的文法两两不相交，于是
"这个 ``channel:`` 键归谁"是**能确定的判断**而不是猜 —— 纯数字的 id 不可能是
slack 的（大写字母开头）也不可能是 mattermost 的（恰好 26 位）。文法推导见
:data:`DISCORD_LOCAL_ID_PATTERN`，判定入口见
:meth:`~opencode_bridge.adapters.base.Adapter.owns_local_id`。

心跳（本项目最贵的坑之一）
--------------------------
1. **``heartbeat_interval`` 的单位是毫秒**（按秒用会让心跳快 1000 倍，瞬间触发
   4008 限流被踢）。换算在 :meth:`DiscordAdapter._on_hello` 里，**一个字都别改**。
2. **心跳的 ``d`` 键不能省**：一个事件都没收到时也必须发 ``{"op":1,"d":null}``。
   :meth:`DiscordAdapter._send_op` 无条件写 ``"d"``。
3. **周期由传输层的周期钩子驱动**（``on_tick`` + ``tick_interval``），**不是**自带
   心跳线程 —— 见下一节。

为什么心跳要用"定时驱动"的周期钩子而不是循环驱动
--------------------------------------------------
Gateway 的 WS 读会阻塞 :data:`GATEWAY_RECV_TIMEOUT`（60s）才返回，而心跳周期是
41~45s。靠"每次取下一条数据之前 tick"的话，心跳会被拖到 60s 一次 —— 超过 Discord
容忍的 ``interval × 1.25``，连接会被直接判死。

也**不能**靠调小 WS 读超时来解决：:mod:`opencode_bridge.ws` 的 ``_recv_exact`` 在
读超时时会抛异常并**丢掉已读到的半帧**，把 socket 超时调小等于让字节流损坏。所以
本适配器用 ``tick_interval > 0``（另起一个 daemon 定时线程）的模式，粒度取
心跳周期的 1/8（:data:`HEARTBEAT_TICK_DIVISOR`），见 :meth:`_make_transport`。

Gateway 上七个不能省的点
------------------------
1. **网关主机名必须问 REST**：``GET /gateway/bot`` 返回的 ``url``（官方会换域名），
   不许硬编码 ``gateway.discord.gg``。见 :meth:`_resolve_gateway_url`。
2. **Resume 必须用 READY 给的 ``resume_gateway_url``**，用错会显著提高断线率。
3. **停止重连的 close code** 是 ``{4004, 4010, 4011, 4012, 4013, 4014}`` ——
   配错/缺权限，重连一万次也不会好，必须带**具体原因**停下而不是无脑
   ``while True``。⚠️ **invalid token 是 4004，不是常被误传的 4010**（4010 是
   shard 参数非法）。见 :data:`FATAL_CLOSE_CODES`。
4. **防回环只看 ``author.id`` 是否等于自己**（READY 里缓存的 user id），
   **绝不能用 ``author.bot``** —— 那会把别的 bot 发的消息全丢掉。而且
   ``author.bot`` 是**可选**字段，可能整个键不存在。
5. **七个事件过滤器一条都不能少**：自己发的 / ``type != 0`` 系统消息 /
   IS_CROSSPOST / webhook / 空正文 / 缺 channel_id / 授权闸门。见
   :meth:`_handle_message_create`。
6. **主动断开用 4000 而不是 1000**：1000/1001 会让 session 失效、bot 在开发者后台
   显示离线，也会让 Resume 失去意义。见 :data:`GATEWAY_CLOSE_REQUESTED`。
7. **intents 用位值表达式**（``512|4096|32768`` = 37376），不是裸数字。
   ⚠️ MESSAGE_CONTENT（``1 << 15``）**必须在开发者后台勾选**，否则服务端 close
   4014（"Disallowed intents"）—— 见 :data:`INTENT_MESSAGE_CONTENT`。
"""

from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, List, Optional, Tuple

from ..hooks import Hooks, Inbound, MsgHandle, Outbound, SendError
from ..identity import format_id
from ..split import split_text
from ..transport import NOTHING, ReconnectNow, WebSocketTransport
from .base import Adapter, classify_http, register

logger = logging.getLogger("opencode_bridge.adapters.discord")

__all__ = ["DiscordAdapter"]

API_BASE = "https://discord.com/api/v10"
MESSAGE_LIMIT = 2000         # Discord message content limit
MIN_SEND_INTERVAL = 1.2      # per-conversation send/edit throttle (seconds)
SOCKET_TIMEOUT = 30.0
MAX_RETRY_AFTER = 60.0

# ----------------------------------------------------------------------
# Gateway v10 入站常量（T2.2）
# ----------------------------------------------------------------------
GATEWAY_API_VERSION = 10           # 连接串固定 ?v=10（REST 侧已由 API_BASE 固定）
GATEWAY_ENCODING = "json"          # 不加 compress=（那需要 zlib-stream 协商）
GATEWAY_MAX_PAYLOAD = 4096         # 单个网关 payload 上限，超了服务端 close 4002
#: recv 超时（秒）。心跳周期实测 41~45s 且每次心跳都有 op 11 往返，正常不会触发；
#: 它只是"Linux 上 close() 不保证唤醒阻塞 recv"的兜底（见 _open_socket 注释）。
#: ⚠️ **不要为了让心跳更准而调小它** —— ws.py 在读超时会丢掉半帧（见模块 docstring）。
GATEWAY_RECV_TIMEOUT = 60.0
RECONNECT_MIN = 1.0                # 重连退避下限（秒）
RECONNECT_MAX = 60.0               # 重连退避上限（秒）
HEARTBEAT_DEFAULT = 45.0           # Hello 没给 heartbeat_interval 时的兜底（秒）
#: 心跳到期判定的时间粒度 = 心跳周期 / 这个除数（再封顶在 5s）。心跳的实际发送
#: 时刻最多比协商值晚这么多 —— 41s 周期下是 ~5.2%，远小于服务端容忍的 25%。
#: 取周期的分数（而不是常数）是为了让亚秒级的协商值也能测（周期 60ms 时粒度 7.5ms）。
HEARTBEAT_TICK_DIVISOR = 8
HEARTBEAT_TICK_MAX = 5.0
HEARTBEAT_TICK_MIN = 0.001

#: intents 是**整数 bitmask**（按位或），不是数组。本项目只要消息类 intent：
INTENT_GUILD_MESSAGES = 1 << 9     # 512   服务器内频道消息
INTENT_DIRECT_MESSAGES = 1 << 12   # 4096  私聊
INTENT_MESSAGE_CONTENT = 1 << 15   # 32768 消息正文（不开就收不到 content）
#: ⚠️ MESSAGE_CONTENT 必须在**开发者后台 / Portal** 勾选，否则网关会 close 4014
#: （"Disallowed intents"）。403/401 之类都跟这个无关 —— 只改配置、不改代码。
DEFAULT_INTENTS = (
    INTENT_GUILD_MESSAGES | INTENT_DIRECT_MESSAGES | INTENT_MESSAGE_CONTENT
)  # = 37376

#: 我们主动断开时使用的 close code。**不能用 1000/1001**：那样会让 session 失效、
#: bot 在开发者后台显示离线；用 4000 才能保住 session 供 Resume。
#: 迁移前这条只用在"我们自己主动断"（op 7 / ACK 超时）上，会话收尾的 ``ws.close()``
#: 用的是默认 1000 —— 那等于每次重连都作废 session、Resume 形同虚设。现在由
#: :class:`~opencode_bridge.transport.WebSocketTransport` 的 ``close_code`` 统一
#: 覆盖**两种**断开路径。
GATEWAY_CLOSE_REQUESTED = 4000

#: IS_CROSSPOST：转发消息会在源频道与每个目标频道各触发一次 MESSAGE_CREATE
FLAG_IS_CROSSPOST = 1 << 1

# 网关 opcode（5 号已废弃，不存在）
OP_DISPATCH = 0            # 服务器 → 客户端事件（唯一带 s / t 的 opcode）
OP_HEARTBEAT = 1           # 双向：客户端周期发，服务端可要求立即发
OP_IDENTIFY = 2            # 客户端 → 服务器
OP_RESUME = 6              # 客户端 → 服务器（续用 session，不重新 Identify）
OP_RECONNECT = 7           # 服务器要求重连
OP_INVALID_SESSION = 9     # d=true 可 Resume / d=false 必须重新 Identify
OP_HELLO = 10              # 服务器开场包（带 heartbeat_interval）
OP_HEARTBEAT_ACK = 11      # 心跳确认

#: 这些 close code 是**配置/权限层面的错**，重连一万次也不会好，必须停下并给出
#: 可执行的诊断（官方 Gateway 文档的 close code 表）。
#:
#: ⚠️ **invalid token 是 4004**（"Authentication failed"），不是常被误传的 4010。
#: 4010 是 "Invalid Shard"，本适配器固定 ``[0]`` 单分片，正常不该出现。
FATAL_CLOSE_CODES = {
    4004: "认证失败：bot_token 无效或已重置",
    4010: "shard 参数非法（本适配器固定 [0] 单分片，正常不该出现）",
    4011: "该连接需要分片：intent 太多，请减少 intent 位",
    4012: "API 版本非法：v10 不被支持",
    4013: "intent 位值非法：intents 算错了（检查 config.intents）",
    4014: "intent 未在开发者后台开启：去 Portal 勾选对应 intent 后重连",
}

#: Discord local id 的文法：**纯十进制数字**，17~20 位。
#:
#: Discord 的 channel / user / guild id 都是 snowflake —— ``(unix_ms << 22) | 杂项``，
#: 所以必然是十进制数字串，而位数由毫秒时间戳决定：Discord 2015 年上线，那之后的
#: 时间戳左移 22 位约等于 5.9e18 起（17 位），到 uint64 的天花板是 20 位。
#:
#: ⚠️ **上限 20 是承重的那一头**：Mattermost 的 id 恰好 26 位（它用小写字母，
#: 但 base32 字母表里含数字），所以"26 位纯数字"这种形状必须落到 mattermost 而不是
#: discord —— 上限放到 26 就会和它撞。下限 17 则是刻意放松：没有任何别的平台的文法
#: 会匹配 17~20 位纯数字（slack 要大写字母开头），而收紧下限只会让真实会话接不回来。
DISCORD_LOCAL_ID_PATTERN = re.compile(r"^[0-9]{17,20}$")

# _handle_payload / 收包循环的返回值（动作）
_ACT_CONTINUE = "continue"      # 继续收下一包
_ACT_RECONNECT = "reconnect"    # 重连（尽量 Resume）
_ACT_REIDENTIFY = "reidentify"  # 重连 + 丢弃 session 走 Identify
_ACT_FATAL = "fatal"            # 停止重连


# ---------------------------------------------------------------------------
# 传输层接缝
# ---------------------------------------------------------------------------
class _DiscordTransport(WebSocketTransport):
    """Discord 用的 :class:`~opencode_bridge.transport.WebSocketTransport`。

    只覆写一样东西，**都留在适配器这一侧**（传输层不该知道 Discord 存在）：

    **对端关闭时把 ``close_code`` 交回适配器判断。** 基类
    :meth:`~opencode_bridge.transport.WebSocketTransport._next` 在 ``recv()``
    返回 ``None`` 时把它统一变成 ``ConnectionError``（消息里带 close code 字符串），
    而**哪个 close code 该停止重连**是平台语义（``4004`` 是 token 失效、
    ``4014`` 是 intent 没在后台勾选）—— 基类不该知道。所以这里接回来，让适配器
    决定；判定为"配错"时直接 :meth:`Transport.stop` 停掉整个循环（而不是
    重连一万次）。

    ⚠️ 周期钩子**不是**在这里实现的 —— 那是传输层的一等能力（``on_tick`` +
    ``tick_interval``），见 :class:`DiscordAdapter._make_transport`。本子类与
    ``_IrcTransport`` 一样只承担"平台语义接缝"，不重写任何连接管理机制。
    """

    def __init__(self, connect: Callable[[], Any], *, on_disconnect: Callable[[Any], bool],
                 **kw: Any) -> None:
        self._on_disconnect = on_disconnect
        super().__init__(connect, **kw)

    def _next(self, conn: Any) -> Any:
        try:
            return super()._next(conn)
        except ConnectionError:
            # 对端关闭（或读失败）。判定是不是"配错"必须看 close code。
            if self._on_disconnect(conn):
                self.stop()            # ① 停钩子 → ② 关连接 → （不自 join）
                return NOTHING          # 消费循环下一轮就会看到停止位并退出
            raise


@register("discord")
class DiscordAdapter(Adapter):
    """Discord adapter: REST 出站 + Gateway v10 入站。

    线程与连接归 :class:`_DiscordTransport`；:attr:`running` / :meth:`stop` 是它的
    代理。

    ``_conversation_id`` 产出统一格式 ``discord:<channel_id>``（已从 ``channel:`` 切过来），
    ``_channel_id`` 仍认旧前缀 —— 见模块 docstring「``conversation_id`` 前缀」一节。
    """

    name = "discord"
    label = "Discord"
    max_message_length = MESSAGE_LIMIT          # 消息内容上限 2000 字符
    supports_inbound = True                    # Gateway v10 入站（T2.2）
    #: principal = channel id：(a) 会话唯一且稳定，(b) 用户能直接看到它，
    #: (c) Discord 对发件人做过认证。三条都成立。
    pairing_supported = True
    supports_inline_buttons = False            # components 未实现
    supports_media = False
    #: ``PATCH /channels/{id}/messages/{id}`` 真能改写已发消息，所以占位气泡发得。
    supports_message_edit = True
    required_tokens = ("bot_token",)           # 入站只多要一个 bot_token（默认值）

    min_interval = MIN_SEND_INTERVAL

    #: 迁移前 Discord 用的 ``conversation_id`` 前缀（**歧义**：三家共用）。
    #: 值刻意写**字面量**，不用任何常量拼 —— 拼了就跟"防走偏断言"一样会恒真。
    legacy_conversation_prefix = "channel:"
    #: 本平台的 local id 文法（snowflake 纯数字，见
    #: :data:`DISCORD_LOCAL_ID_PATTERN` 的推导）。判定入口是基类的
    #: :meth:`~opencode_bridge.adapters.base.Adapter.owns_local_id`。
    local_id_pattern = DISCORD_LOCAL_ID_PATTERN
    #: ``conversation_id`` 的合法前缀：当前格式 + 旧别名。
    #: :meth:`_channel_id` 按这个列表剥前缀，所以**旧前缀必须继续认** ——
    #: 写前收件箱把 ``conversation_id`` 持久化在盘上，切换前写入、切换后才重放的
    #: 未投递消息带着 ``channel:``，认不出来就等于把那些回复永久丢弃。
    _CONVERSATION_PREFIXES = ("discord:", legacy_conversation_prefix)

    def __init__(self, config: dict, hooks: Hooks) -> None:
        super().__init__(config, hooks)
        self.bot_token: str = str(self.config.get("bot_token") or "").strip()
        self._throttle_lock = threading.Lock()
        self._last_send: dict[str, float] = {}

        # --- Gateway 入站状态（T2.2）----------------------------------
        # intents 是整数 bitmask：允许配置覆盖（例如只想收私聊），默认三项全开。
        self.intents: int = self._config_intents()
        # gateway_url 允许配置覆盖（自建网关 / 测试）；留空则启动时问 REST 要。
        self._gateway_url: Optional[str] = (
            str(self.config.get("gateway_url") or "").strip() or None
        )
        self._resume_url: Optional[str] = None      # READY 给的 resume_gateway_url
        self._session_id: Optional[str] = None
        self._last_seq: Optional[int] = None        # 最近一次非 null 的 s
        self._my_user_id: Optional[str] = None     # READY.d.user.id（过滤自己）
        self._ack_received: bool = False            # 最近一次心跳是否被 ACK
        self._heartbeat_interval: float = 0.0       # 秒（Hello 给的是毫秒）
        self._ws_factory = None                      # 测试注入点
        self._transport: Optional[_DiscordTransport] = None
        # --- 心跳状态机（迁移前在心跳线程的局部变量里，这里显式化以便断言）----
        #: 当前会话的 WS（HELLO 时记下，供周期钩子发心跳）。
        self._hb_conn: Any = None
        #: 最近一次**周期**心跳的发出时刻（None = 本会话还没发过）。
        self._hb_last_sent: Optional[float] = None
        #: 下一次周期心跳到期的时间戳（见 :meth:`_tick`）。
        self._hb_due: float = 0.0

    def _config_intents(self) -> int:
        """读 ``intents`` 配置；非法值退回默认 37376 而不是静默发 0。"""
        raw = self.config.get("intents")
        if raw in (None, ""):
            return DEFAULT_INTENTS
        try:
            value = int(raw)
        except (TypeError, ValueError):
            logger.warning("discord: intents 配置非法 %r，改用默认 %d", raw, DEFAULT_INTENTS)
            return DEFAULT_INTENTS
        return value if value > 0 else DEFAULT_INTENTS

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
    # 传输层接缝
    # ------------------------------------------------------------------
    @property
    def transport(self) -> Optional[_DiscordTransport]:
        """当前传输层（``start()`` 之后才有；测试与 :meth:`_connection` 看它）。"""
        return self._transport

    @property
    def running(self) -> bool:
        """消费线程是否活着（代理到传输层）。

        ⚠️ 基类的 :attr:`Adapter._thread` 现在**恒为 None**（入站线程由传输层持有，
        名字是 ``transport:discord``）。``core.py`` 的 ``_adapter_for`` 已经能靠前缀/
        映射找到本适配器，不依赖线程匹配。
        """
        transport = self._transport
        return transport is not None and transport.running

    def _connection(self) -> Any:
        """当前 WS 连接（未连接时 ``None``；由传输层持有）。"""
        transport = self._transport
        return transport.connection if transport is not None else None

    def _make_transport(self) -> _DiscordTransport:
        """构造本次运行用的传输层（测试注入点：退避 / 关闭码 / 粒度接线值）。

        退避**逐字对齐迁移前** ``_inbound_loop`` 末尾那三行：

        * ``min_backoff=RECONNECT_MIN`` / ``max_backoff=RECONNECT_MAX`` ——
          1s 起、×2、封顶 60s，与迁移前 ``delay = min(delay*2, RECONNECT_MAX)`` 一致。
        * ``reset_after=RECONNECT_MIN`` —— 迁移前的判定是
          ``time.monotonic() - started >= RECONNECT_MIN``（``started`` 取在**建连之前**）。
          传 0（基类默认）会变成"只要连上过就重置"，与那一行**不是**逐字等价，所以
          这里显式传 1.0。

        其它两处接线值：

        * ``close_code=GATEWAY_CLOSE_REQUESTED``（4000）—— 迁移前只有"主动断开"
          （op 7 / ACK 超时）用 4000，会话收尾走的是默认 1000，那等于每次重连都作废
          session、Resume 形同虚设。现在**两种**断开都用 4000（见模块 docstring 第 6 条）。
        * ``tick_interval`` 初始值只是一档粗粒度；HELLO 协商到真实周期之后
          :meth:`_on_hello` 会把它收紧成"周期 / 8"（见 :data:`HEARTBEAT_TICK_DIVISOR`）。

        ⚠️ 刻意读**模块全局**而不是类属性：迁移前就是运行时读全局
        ``RECONNECT_MIN`` / ``RECONNECT_MAX``，既有测试
        （``tests/test_discord_gateway.py`` 的 ``LoopTestCase``）靠 monkeypatch
        那个全局来缩短等待。
        """
        return _DiscordTransport(
            self._open_socket,
            on_message=self._on_frame,
            on_disconnect=self._on_disconnect,
            on_tick=self._tick,
            tick_interval=HEARTBEAT_TICK_MAX,
            name="discord",
            min_backoff=RECONNECT_MIN,
            max_backoff=RECONNECT_MAX,
            reset_after=RECONNECT_MIN,
            close_code=GATEWAY_CLOSE_REQUESTED,
        )

    def _open_socket(self) -> Any:
        """建一次会话：定 URL → 建 WS → 记日志。

        对应迁移前 ``_inbound_loop`` 开头的两行，逐字保留顺序与日志文本。
        """
        ws = self._make_ws(self._resolve_gateway_url())
        logger.info(
            "discord: gateway 已连接（%s 路径）",
            "Resume" if self._should_resume() else "Identify",
        )
        return ws

    def _make_ws(self, url: str):
        """建 WS 连接；``_ws_factory`` 为测试注入点，生产走标准库实现（T2.0）。

        ``GATEWAY_RECV_TIMEOUT`` 是"Linux 上 ``close()`` 不保证唤醒阻塞 ``recv()``"
        的兜底 —— 心跳每 ~41s 就有一次 ACK 往返，正常连接不会因为它被误判。
        """
        factory = self._ws_factory
        if factory is None:
            from ..ws import connect as factory  # 延迟导入：没开入站也不加载它
        return factory(url, timeout=GATEWAY_RECV_TIMEOUT)

    def _on_frame(self, conn: Any, frame: Any) -> None:
        """传输层的 ``on_message`` 钩子：一条**原始帧** → 分发 → 可能要求换连接。

        迁移前是内层 ``while`` 循环里"``recv()`` → ``_handle_payload`` → 动作 !=
        continue 就 ``break``"。现在 break 由抛
        :class:`~opencode_bridge.transport.ReconnectNow` 表达。

        ⚠️ 一处**刻意**的行为差异：op 7 / op 9 会抛 ``ReconnectNow``（**立刻**重连、
        不走退避），而迁移前它们走的是"break 出内层循环后 ``wait(RECONNECT_MIN)``"
        —— 即 1s 后才重连。理由与 :mod:`opencode_bridge.adapters.slack` 完全一致：
        这两类包都是**服务端主动要求换连接**（"不是故障"），不该被当失败指数退避；
        官方对 op 7 的说法也是"别在那儿干等"。顺带还避免了一次假的"会话出错"告警。
        若将来要求逐字保留那 1s，改成让基类走正常退避即可（去掉 ``ReconnectNow``、
        直接 :meth:`_close_ws` 让下一次 ``recv()`` 返回 ``None``）。
        """
        action = self._handle_payload(conn, frame)
        if action == _ACT_CONTINUE:
            return
        if action == _ACT_FATAL:
            self._stop_gateway("配置/权限类 close code")
            return
        if action == _ACT_REIDENTIFY:
            # 迁移前在会话收尾的 ``finally`` 里清；现在就地清 —— 这两步之间没有任何
            # 代码读 session，语义等价（见模块 docstring）。
            self._clear_session()
        raise ReconnectNow(f"discord: gateway 要求重连（{action}）")

    def _on_disconnect(self, conn: Any) -> bool:
        """对端关闭：按 close code 判定"要不要停"。返回 ``True`` = 停掉整个循环。

        只对"拿到 close code 且属于 :data:`FATAL_CLOSE_CODES`"返回 ``True``；
        ``None``（对端没给 close code）按可 Resume 处理，与迁移前一致。
        """
        return self._action_for_close(getattr(conn, "close_code", None)) == _ACT_FATAL

    def _stop_gateway(self, why: str) -> None:
        """配置/权限错 → 打带原因的 error 并**停止重连**（不再 ``while True``）。"""
        logger.error("discord: %s，停止重连", why)
        self._stop_event.set()          # 顺带让 _throttle 之类立刻放开
        transport = self._transport
        if transport is not None:
            transport.stop()            # 停钩子 → 关连接 → join（不自 join）

    def _on_event(self, item: Any) -> None:
        """传输层的 ``on_event`` 回调：**刻意是 no-op**。

        :class:`~opencode_bridge.transport.WebSocketTransport` 在每条原始帧到达时
        先调 ``on_message(conn, frame)``、**再**把同一帧交给 ``on_event``。Discord 的
        解析全在 :meth:`_on_frame` 里做完了，这里必须什么都不做 ——
        否则同一条消息会被投递两次（core 会当成两条消息，bot 也会回两次）。
        保留这个空实现（而不是给 ``start()`` 传 ``lambda _: None``）是为了让
        "同一帧会被两个钩子看到"这件事在代码里是**显式可见**的。
        """

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """启动入站（Gateway v10 WebSocket）。缺 bot_token 时只警告不起线程。"""
        if not self.bot_token:
            logger.warning("discord: bot_token missing; adapter not started")
            return
        # 迁移前漏了这一行：stop() 置位后同一个实例再 start()，入站循环会立刻
        # 退出（静默不工作）。matrix / telegram / irc / slack 迁到传输层时都补上了。
        # 传输层的 start() 只清它自己的停止位，不碰适配器这个（_throttle 依赖它）。
        self._stop_event.clear()
        transport = self._make_transport()
        self._transport = transport
        transport.start(self._on_event)

    def stop(self) -> None:
        """置停止位 → **停心跳 → 关 WS 唤醒阻塞的 recv() → join 线程**（**幂等**）。

        顺序不能反：传输层的 ``join`` 只等 5s，而 ``recv()`` 在
        :data:`GATEWAY_RECV_TIMEOUT`（60s）内可能一直阻塞；不先把连接关掉就会每次
        stop 都等满超时。三段式（停钩子 → 关连接 → join）现在由
        :meth:`~opencode_bridge.transport.Transport.stop` 统一保证，迁移前是在本方法
        里手写的（``_stop_heartbeat()`` → ``_close_ws(ws)`` → ``super().stop()``）。
        """
        super().stop()                      # 置停止位（_throttle 依赖它）
        transport, self._transport = self._transport, None
        if transport is not None:
            transport.stop()

    # ------------------------------------------------------------------
    # Inbound (Gateway v10)
    # ------------------------------------------------------------------
    @property
    def my_user_id(self) -> Optional[str]:
        """READY 里拿到的 bot 自身 user id（过滤自己消息的唯一可靠判据）。"""
        return self._my_user_id

    @property
    def session_id(self) -> Optional[str]:
        """当前会话 id（Resume 用）。"""
        return self._session_id

    @property
    def last_seq(self) -> Optional[int]:
        """最近一次收到的非 null ``s``（心跳与 Resume 都复用它）。"""
        return self._last_seq

    @property
    def ack_received(self) -> bool:
        """最近一次心跳是否已收到 op 11 ACK。"""
        return self._ack_received

    @property
    def gateway_url(self) -> Optional[str]:
        """缓存的网关 URL（``GET /gateway/bot`` 返回的原始值，未取到则 ``None``）。"""
        return self._gateway_url

    def _should_resume(self) -> bool:
        """有 session 且有 resume URL 才 Resume，否则走 Identify。"""
        return bool(self._session_id and self._resume_url)

    def _clear_session(self) -> None:
        """丢弃会话：下一次连接必须 Identify（op 9 d=false / 4011 等）。"""
        self._session_id = None
        self._last_seq = None

    def _resolve_gateway_url(self) -> str:
        """本次连接用的 WSS URL（带 ``?v=10&encoding=json``）。

        Resume 用 READY 给的 ``resume_gateway_url``；否则用缓存的初始 URL。
        初始 URL 来自 ``GET /gateway/bot``（**不硬编码主机名**：官方会换域名），
        也允许 ``gateway_url`` 配置覆盖（自建网关 / 测试）。
        """
        base = self._resume_url if self._should_resume() else self._gateway_url
        if not base:
            status, data = self._request("GET", "gateway/bot", {})
            url = str(data.get("url") or "")
            if status != 200 or not url:
                raise RuntimeError(
                    f"GET /gateway/bot failed: HTTP {status} "
                    f"{data.get('message') or data.get('code') or ''}".strip()
                )
            self._gateway_url = url
            base = url
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}v={GATEWAY_API_VERSION}&encoding={GATEWAY_ENCODING}"

    # -- 收包 -------------------------------------------------------------
    def _handle_payload(self, ws, raw: str) -> str:
        """处理一个网关包，返回下一步动作（continue/reconnect/reidentify/fatal）。

        只有 ``op == 0`` 的包才带 ``s`` / ``t``；``s`` 为 null 时**不能**覆盖
        ``_last_seq``（否则 Resume 会跳事件）。
        """
        try:
            packet = json.loads(raw)
        except Exception:
            logger.debug("discord: 非 JSON 帧，忽略")
            return _ACT_CONTINUE
        if not isinstance(packet, dict):
            return _ACT_CONTINUE
        try:
            op = int(packet.get("op"))
        except (TypeError, ValueError):
            logger.debug("discord: 包里没有合法 op，忽略")
            return _ACT_CONTINUE

        seq = packet.get("s")
        if isinstance(seq, int) and not isinstance(seq, bool):
            self._last_seq = seq
        data = packet.get("d")

        if op == OP_DISPATCH:
            self._handle_dispatch(str(packet.get("t") or ""), data)
            return _ACT_CONTINUE
        if op == OP_HEARTBEAT:
            # 服务端要求**立即**心跳，不能等下一个周期
            self._send_op(ws, OP_HEARTBEAT, self._last_seq)
            return _ACT_CONTINUE
        if op == OP_HELLO:
            self._on_hello(ws, data)
            return _ACT_CONTINUE
        if op == OP_HEARTBEAT_ACK:
            self._ack_received = True
            return _ACT_CONTINUE
        if op == OP_RECONNECT:
            # 官方：几秒后服务端也会关掉，别在那儿干等
            logger.info("discord: op 7 Reconnect —— 立即断开并重连")
            self._close_ws(ws)
            return _ACT_RECONNECT
        if op == OP_INVALID_SESSION:
            if bool(data):
                logger.info("discord: op 9 Invalid Session(d=true) —— 可 Resume")
                return _ACT_RECONNECT
            logger.warning("discord: op 9 Invalid Session(d=false) —— session 失效，改走 Identify")
            self._clear_session()
            return _ACT_REIDENTIFY
        logger.debug("discord: 忽略未处理的 op=%s", op)
        return _ACT_CONTINUE

    def _action_for_close(self, code: object) -> str:
        """按对端 close code 决定是否重连（4004/4010-4014 属于配错，重连无用）。"""
        try:
            code_int = int(code) if code is not None else None
        except (TypeError, ValueError):
            code_int = None
        if code_int is None:
            logger.info("discord: 对端关闭但没给 close code，按可 Resume 处理")
            return _ACT_RECONNECT
        if code_int in FATAL_CLOSE_CODES:
            logger.error(
                "discord: close %s —— %s", code_int, FATAL_CLOSE_CODES[code_int]
            )
            return _ACT_FATAL
        logger.info("discord: close %s —— 可 Resume 重连", code_int)
        return _ACT_RECONNECT

    # -- 会话建立 ---------------------------------------------------------
    def _on_hello(self, ws, data: object) -> None:
        """HELLO → 定心跳周期 → 立刻发一次心跳 → Identify / Resume → 摆好心跳排程。

        官方推荐顺序是 HELLO → op1 → Identify（Identify 24h 内全局限 1000 次，
        所以有 session 就优先 Resume）。第一次心跳**立即**发（这样 Identify 前一定
        有心跳），jitter 加在第一次**周期**心跳前。

        迁移前最后一行是 ``_start_heartbeat(ws, interval)``（起一个心跳线程）；现在
        只**摆好状态机**（:attr:`_hb_due`），实际的"到点了没有"由传输层的周期钩子
        :meth:`_tick` 判定。心跳线程被删掉是因为同一份机制不该在三家适配器里各写一遍。
        """
        raw = data.get("heartbeat_interval") if isinstance(data, dict) else None
        if (
            isinstance(raw, (int, float))
            and not isinstance(raw, bool)
            and raw > 0
        ):
            interval = float(raw) / 1000.0  # ⚠️ 官方文档的单位是**毫秒**
        else:
            interval = HEARTBEAT_DEFAULT
            logger.warning(
                "discord: Hello 未给合法 heartbeat_interval（%r），按 %.1fs 处理",
                raw,
                interval,
            )
        self._heartbeat_interval = interval
        self._ack_received = False

        # 心跳状态机复位（周期钩子据此发心跳）。jitter 在**这里**采样一次 ——
        # 迁移前的 `_heartbeat_loop` 也是在第一次等待前采一次，若每次 tick 都重采，
        # 期限会被一直往后推，心跳永远发不出去。
        self._hb_conn = ws
        self._hb_last_sent = None
        self._hb_due = self._now() + interval + self._heartbeat_jitter(interval)
        self._tighten_tick(interval)

        # 第一次心跳：d 键必须存在，一个事件都没收到时就是 null
        self._send_op(ws, OP_HEARTBEAT, self._last_seq)
        if self._should_resume():
            self._send_op(
                ws,
                OP_RESUME,
                {
                    "token": self.bot_token,
                    "session_id": self._session_id,
                    "seq": self._last_seq,
                },
            )
        else:
            self._send_op(
                ws,
                OP_IDENTIFY,
                {
                    "token": self.bot_token,
                    "intents": self.intents,
                    "properties": {
                        "os": "python",
                        "browser": "opencode-bridge",
                        "device": "opencode-bridge",
                    },
                },
            )

    def _send_op(self, ws, op: int, data: Any = None, seq: Any = None) -> bool:
        """发一个网关 payload。

        * 心跳的 ``d`` **恒存在**（没有事件时是 ``null``），不能省掉这个键；
        * ``s`` 只在明确要带时才出现（Identify / Resume 不带）；
        * payload 超 4096 字节服务端会 close 4002，所以本地先拦一道。
        """
        packet: dict = {"op": op, "d": data}
        if seq is not None:
            packet["s"] = seq
        text = json.dumps(packet)
        if len(text.encode("utf-8")) > GATEWAY_MAX_PAYLOAD:
            logger.error(
                "discord: 网关 payload %d 字节超过 %d，拒发并断开",
                len(text.encode("utf-8")),
                GATEWAY_MAX_PAYLOAD,
            )
            self._close_ws(ws, 4002, "payload too large")
            return False
        try:
            ws.send(text)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("discord: 发送 op=%s 失败: %s", op, exc)
            return False

    def _close_ws(self, ws, code: int = GATEWAY_CLOSE_REQUESTED, reason: str = "") -> None:
        """主动断开：默认用非 1000 的 code，保住 session 以便 Resume。"""
        try:
            ws.close(code, reason)
        except Exception as exc:  # noqa: BLE001
            logger.debug("discord: ws.close 失败（忽略）: %s", exc)

    # -- 周期心跳（由传输层的周期钩子驱动，不是自带的线程）----------------
    @staticmethod
    def _now() -> float:
        """单调时钟。单独抽成方法是给测试一个**假时钟**注入点。

        心跳周期是本项目最贵的坑之一（毫秒单位 / 不能慢一倍），必须能逐拍断言；
        直接调 ``time.monotonic()`` 就只能靠墙钟测，那是 flaky 的老路。
        """
        return time.monotonic()

    def _heartbeat_jitter(self, interval: float) -> float:
        """第一次**周期**心跳前的抖动（官方：``interval * random(0, 1)``）。

        单独抽成方法是给测试一个注入点（避免测试依赖随机值）。
        """
        return random.uniform(0.0, interval)

    def _tick(self) -> None:
        """**传输层周期钩子**：到点就发心跳，同时监测 ACK。返回即结束。

        迁移前这段逻辑在**心跳线程** :meth:`_heartbeat_loop` 里：
        「先 ``wait(interval + jitter)`` → 发一次 → 再 ``wait(interval)`` 等 ACK →
        没 ACK 就判定连接已死并用 4000 断开」。现在把它写成**基于到期时刻的状态机**，
        由传输层的定时线程周期调用（见 :meth:`_make_transport` 为什么必须用定时驱动）。

        与迁移前逐项对应：

        * "还没发过周期心跳" → 等 ``interval + jitter``（:meth:`_on_hello` 里已采样好
          jitter 存进 :attr:`_hb_due`）。jitter **只采样一次**，否则每次 tick 都重采
          会把期限一直往后推、心跳永远发不出去。
        * "已发过" → 等 ``interval``；**这一个 interval 就是 ACK 的等待窗口**，
          到期还没收到 op 11 → 判定连接已死 → 用 :data:`GATEWAY_CLOSE_REQUESTED`
          （4000，**非 1000**）断开，随后 Resume 重连。

        ⚠️ 周期 = :attr:`_hb_due - _hb_last_sent`，**恰好等于**协商来的 interval。
        写成"发完再等一个 interval"就会变成 2× interval（慢一倍）—— 那是本项目
        明确点名的失败模式，用例 ``test_heartbeat_period_equals_the_negotiated_interval``
        用假时钟逐拍钉死。

        钩子抛异常由传输层兜住（记 ``errors`` 后继续），所以这里不必自己包 try。
        """
        conn = self._hb_conn
        if conn is None or getattr(conn, "closed", False):
            return                       # 没有活动会话（退避中）/ 连接已关
        interval = self._heartbeat_interval
        if interval <= 0:
            return                       # 还没收到 HELLO，心跳周期未知
        now = self._now()
        if now < self._hb_due:
            return                       # 还没到点
        if self._hb_last_sent is not None and not self._ack_received:
            # 一个周期内没收到 op 11 ⇒ 连接已死（官方要求主动断开，1000 会作废 session）
            logger.warning(
                "discord: %.1fs 内没收到 op 11 ACK，判定连接已死，断开重连", interval
            )
            self._close_ws(conn, GATEWAY_CLOSE_REQUESTED, "heartbeat ack timeout")
            return
        # ⚠️ ``_ack_received`` 必须在**发之前**清零：测试替身（以及任何同步回 ACK 的
        # 实现）会在 ``send()`` 里立刻投递 op 11，发完再清会把刚到的 ACK 抹掉，
        # 于是下一次到期时被误判成"连接已死"。
        self._ack_received = False      # 从现在起等这一拍的 ACK
        self._send_op(conn, OP_HEARTBEAT, self._last_seq)
        self._hb_last_sent = now
        self._hb_due = now + interval   # ⚠️ 必须是 now+interval，不是 now+2×interval

    def _tighten_tick(self, interval: float) -> None:
        """HELLO 协商到真实周期后，把周期钩子的粒度收紧成"周期 / 8"。

        粒度 = 心跳实际发送时刻**最多**晚多少。取周期的分数而不是常数，是因为
        亚秒级周期（测试里的 60ms）也必须能测；同时封顶 5s，免得为 41s 的心跳
        白白起一个每 5ms 醒一次的线程。
        """
        transport = self._transport
        if transport is None:
            return                       # 单元测试直接调 _on_hello（没起传输层）
        transport.tune_tick_interval(
            min(
                HEARTBEAT_TICK_MAX,
                max(HEARTBEAT_TICK_MIN, interval / HEARTBEAT_TICK_DIVISOR),
            )
        )

    # -- dispatch / 事件过滤 ---------------------------------------------
    def _handle_dispatch(self, name: str, data: object) -> None:
        if name == "READY":
            self._handle_ready(data)
        elif name == "MESSAGE_CREATE":
            self._handle_message_create(data)
        else:
            logger.debug("discord: 忽略 dispatch t=%s", name)

    def _handle_ready(self, data: object) -> None:
        if not isinstance(data, dict):
            return
        user = data.get("user")
        user = user if isinstance(user, dict) else {}
        self._my_user_id = str(user.get("id") or "") or None
        self._session_id = str(data.get("session_id") or "") or None
        self._resume_url = str(data.get("resume_gateway_url") or "") or None
        logger.info(
            "discord: READY（user=%s session=%s）", self._my_user_id, self._session_id
        )

    def _drop_inbound(self, reason: str, channel: str, author: str, is_bot: bool) -> None:
        logger.info(
            "discord: 丢弃消息（%s）channel=%s author=%s author_is_bot=%s",
            reason,
            channel or "?",
            author or "?",
            is_bot,
        )

    def _handle_message_create(self, data: object) -> bool:
        """``MESSAGE_CREATE`` → 过滤 → Inbound。返回是否真的放行了一条。"""
        if not isinstance(data, dict):
            return False
        author = data.get("author")
        author = author if isinstance(author, dict) else {}
        author_id = str(author.get("id") or "")
        # ``author.bot`` 是**可选**字段（可能整个键不存在），而且**不能**拿它当过滤
        # 判据 —— 那会把别的 bot 发的消息也全丢掉；这里只用于日志。
        is_bot = bool(author.get("bot", False))
        channel_id = str(data.get("channel_id") or "")
        content = str(data.get("content") or "")

        # 1) 自己发的（否则发出去的消息会回到我们这里，无限回环）
        if self._my_user_id and author_id == self._my_user_id:
            self._drop_inbound("bot 自己发的", channel_id, author_id, is_bot)
            return False
        # 2) 只收普通消息（6=频道置顶、7=有人加入、18=新线程… 全是系统消息）
        if data.get("type", 0) != 0:
            self._drop_inbound(f"非普通消息 type={data.get('type')!r}", channel_id, author_id, is_bot)
            return False
        # 3) crosspost 转发会在每个频道各触发一次
        flags = data.get("flags") or 0
        if isinstance(flags, int) and not isinstance(flags, bool) and flags & FLAG_IS_CROSSPOST:
            self._drop_inbound("crosspost 转发", channel_id, author_id, is_bot)
            return False
        # 4) webhook 消息的 author.id 是 webhook id，不是真人
        if data.get("webhook_id"):
            self._drop_inbound("webhook 消息", channel_id, author_id, is_bot)
            return False
        # 5) 空正文（附件消息 content 为空）
        if not content:
            self._drop_inbound("空正文", channel_id, author_id, is_bot)
            return False
        if not channel_id:
            self._drop_inbound("缺 channel_id", channel_id, author_id, is_bot)
            return False
        # 6) 授权闸门必须在产生 Inbound **之前**（否则能用命令/审批字绕过）。
        #    ⚠️ /pair 在未授权时也要能进来，所以 conversation_id 提到闸门之前算。
        conversation_id = self._conversation_id(channel_id)
        if not self.admits(channel_id) and not self.answer_pairing_request(
            channel_id, conversation_id, content
        ):
            self._drop_inbound("未在白名单", channel_id, author_id, is_bot)
            return False
        try:
            self.hooks.on_inbound(
                Inbound(
                    conversation_id=conversation_id,
                    text=content,
                    kind="text",
                    user_id=author_id or None,
                    message_id=str(data.get("id") or "") or None,
                    platform=self.name,
                    raw=data,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("discord: on_inbound 失败: %s", exc)
            return False
        return True

    # ------------------------------------------------------------------
    # Lifecycle teardown
    # ------------------------------------------------------------------
    # ⚠️ ``stop()`` 与 ``running`` 已随传输层迁到上面「传输层接缝」一节。

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------
    @staticmethod
    def _channel_id(conversation_id: str) -> Optional[str]:
        """``conversation_id`` → Discord channel id；空的一律返回 ``None``。

        裸 channel id（``"1234"``）也认，切换前后的两种前缀都认 ——
        理由见 :attr:`DiscordAdapter._CONVERSATION_PREFIXES`。
        """
        raw = str(conversation_id or "")
        for prefix in DiscordAdapter._CONVERSATION_PREFIXES:
            if raw.startswith(prefix):
                raw = raw[len(prefix):]
                break
        return raw or None

    @staticmethod
    def _conversation_id(channel_id: Any) -> str:
        """``channel_id`` → ``discord:...``（统一 ``platform:local_id`` 格式）。

        ① 歧义前缀 **永不迁移**：:mod:`opencode_bridge.state` 对 ``channel:`` 只捕获、
        不归一（归属从未被持久化，推不出来），所以历史会话靠
        :mod:`opencode_bridge.conversation_keys` 的"各家 local id 文法不相交"
        在**读取时**接回来 —— 判据是 :attr:`DiscordAdapter.local_id_pattern`。
        ② 反向解析（:meth:`_channel_id`）必须继续认 ``channel:``：写前收件箱把
        ``conversation_id`` 持久化在盘上，切换前写入、切换后才重放的未投递消息带着
        旧前缀，认不出来就等于把那些回复永久丢弃。
        """
        return format_id("discord", channel_id)

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
        chunks: List[str] = split_text(out.text, self.effective_max_length, prefix_fmt="")
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
        """``PATCH /channels/{id}/messages/{id}`` —— 真的原地改写。

        ⚠️ **超限就地拒收，抛** ``ValueError`` —— 与
        :meth:`TelegramAdapter.edit` **同一件事、同一句话术**（只差平台名）：
        ``outbound.py`` 的两条改写路
        （:meth:`~opencode_bridge.outbound.OutboundSender.edit_progress` 与
        ``finalize``）都把 ``ValueError`` 认成「**本地**判定的正文超限」并各自
        兜底（进度那条宁可不改；收尾那条补发读者没读到的那几段）。基类
        ``edit`` 的契约是「失败 -> 记日志、返回 ``False``」，而同一件事在两个平台上
        表达得不一样本身就是缺陷：调用方只能靠**猜**哪条路是哪条。

        **为什么不能靠平台回 400**：它确实会回 400，但那时请求已经发出去了 ——
        一次注定失败的往返，外加 :meth:`_throttle` 的那一次等待，而且调用方拿到的
        只是一个 ``False``，与"网络失败""权限不足"完全分不开。

        **什么时候会走到**：流式闸门与收尾都按
        :func:`~opencode_bridge.outbound.one_message_budget` 判，正常路径下**不该**
        超限（不变式由 ``tests/test_event_stream.py`` 与
        ``tests/test_progress_placeholder.py`` 钉着）。这一道是**纵深防御**：闸门与收尾
        读的是两次 ``adapter_for``，而 Mattermost / Nextcloud 的
        :attr:`~opencode_bridge.adapters.base.Adapter.message_limit` 会被服务端的
        ``MaxPostSize`` 之类**启动后细化** —— 若细化发生在一次写入与一次收尾之间，
        预算会当场变小，而 ``finalize`` 那个 ``max(len(head), len(shown))`` 下界会把
        已显示的长度原样放回去。于是本地这道闸就是那唯一一处能在**请求发出之前**说
        「装不下」的地方。
        """
        if len(out.text) > self.effective_max_length:
            raise ValueError(
                f"discord edit text too long: {len(out.text)} > {self.effective_max_length}"
            )
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

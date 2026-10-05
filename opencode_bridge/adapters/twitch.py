"""Lane B — Twitch adapter (tasks.md T3.4). Standard library only.

Twitch 的聊天就是 **IRC over WebSocket**：底层是 ``wss://irc-ws.chat.twitch.tv``
（本仓库自研的 :mod:`opencode_bridge.ws`，TLS + 自动 ping/pong），线上跑的是 IRC 行
协议。所以本适配器的骨架与 :mod:`opencode_bridge.adapters.irc` 同源 —— 行解析、
控制码清理、提及识别、字节预算、PING/PONG、注册时序都直接 **import** 自
:mod:`.irc`（那里是这些纯函数的唯一实现），Twitch 特有的部分只有：

1. **IRCv3 消息标签**（``@display-name=…;user-id=… PRIVMSG #chan :hi``）：

   * 必须先发 ``CAP REQ :twitch.tv/tags twitch.tv/commands`` 才有标签。没有标签
     就拿不到 ``user-id`` / ``room-id`` / 消息 ``id``，``twitch.tv/commands`` 不协商
     连 ``PRIVMSG`` 都识别不了（只会收到裸文本）。
   * 标签值里的空格**合法**（``display-name=Some One``），所以切分必须"按第一个
     未转义的 ``;`` 切标签、按标签段后第一个空格切前缀、按第一个 `` :`` 切末位
     参数"，不能 ``split()`` 了事。转义（``\\:`` ``\\s`` ``\\\\`` ``\\r`` ``\\n``）
     也要还原，否则 ``display-name=A\\:B`` 会解析错。
   * 标签**不保证齐全、顺序任意、值可为空**（Twitch 自己就这么发），任何缺失都
     必须安全兜底成 ``None`` / 空串。

2. **帧 ≠ 行**：IRC 行在 WebSocket **文本帧**里传输，但不能假设一帧一行。Twitch
   一帧里塞多行很常见，WebSocket 分片被 :mod:`ws` 聚合后也可能只带半行。这里
   维护自己的行缓冲（:attr:`TwitchAdapter._rbuf`），按 ``\\n`` 切、按字节设上限，
   连接断开时**丢弃残留**（半行说明连接已经错位，重连后重新开始才对）。
   另注：Twitch 会给客户端发**远长于 512 字节**的行（标签 + 正文），所以入站不能
   套用 IRC 那个 512 字节上限（:data:`INBOUND_LINE_LIMIT`）。

3. **刻意只响应提及**：Twitch 频道一秒钟能刷几十条，全量转成 Inbound 会把模型
   上下文冲垮、也会把别人的闲聊当成本对话。这是**产品取舍**，不是技术限制。
   私聊（target == 我们的 nick）不受此限。

4. **限流**：未认证（unverified）应用在 Twitch IRC 上约 **20 条 / 30 秒**
   （社区经验值，非官方文档常量），超了会被断开。:meth:`TwitchAdapter._throttle`
   用 1.5s 最小间隔折算这个上限。

G4：连接 / 收帧 / 退避 / 线程 / ``stop()`` 语义已迁到
:class:`~opencode_bridge.transport.WebSocketTransport`。本文件只留 Twitch 语义：
IRCv3 标签解析、**帧≠行的重组**（:meth:`TwitchAdapter._on_frame`）、注册时序、
JOIN 时机、去重窗口、提及识别、出站分片。

两处**必须逐字保留**的时序（动了就会丢消息）
--------------------------------------------
1. **JOIN 必须在收到 001 之后、下一行被处理之前发出**。迁移前是"读循环读到 001 就
   返回 → :meth:`TwitchAdapter._join`"；现在读循环归传输层，所以 JOIN 在
   :meth:`_handle_numeric` 里就地发出 —— 两者对服务端是**同一顺序**。
2. **注册超时靠"关掉连接"作废会话**，不靠抛异常：传输层的 ``on_message`` 会把普通
   异常吞掉并继续消费这条连接（抛了等于没抛），而 ``ReconnectNow`` 会跳过退避 ——
   两者都会改变迁移前"结束会话 → 等 ``reconnect_delay`` → 重连"的时序。
   详见 :meth:`TwitchAdapter._void_session`。
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, List, Optional, Tuple

from ..hooks import Hooks, Inbound, MsgHandle, Outbound, SendError
from ..identity import format_id
from ..split import split_text
from ..transport import WebSocketTransport
from .base import Adapter, classify_http, register
# 纯解析/文本工具复用 IRC 适配器的唯一实现（不重复实现，也不修改它）
from .irc import (
    _byte_safe_split,
    _fit_utf8,           # noqa: F401  (保留给下沉的兜底路径使用)
    _make_mention_re,
    _prefix_nick,
    strip_control_codes,
)

logger = logging.getLogger("opencode_bridge.adapters.twitch")

__all__ = ["TwitchAdapter", "MESSAGE_LIMIT"]

#: Twitch 官方 IRC 网关（**必须 TLS**；明文 6667 早已停用）。
TWITCH_ENDPOINT = "wss://irc-ws.chat.twitch.tv:443/"
TMI_HOST = "tmi.twitch.tv"
#: Twitch 用户查询 API（拿自己的 user id / login）。
HELIX_USERS_URL = "https://api.twitch.tv/helix/users"

#: 单条消息的**字符**上限。
#: ⚠️ 社区经验值：Twitch 聊天消息上限通常被引用为 500 字符，但 Twitch 官方文档
#: **没有把这个常量公开**。这里取 400 的保守值（留 20% 余量），宁可多切一条，
#: 也不赌"刚好等于上限"会被静默截断或直接断线。
MESSAGE_LIMIT = 400
#: 单条 IRC 行的**字节**预算。
#: ⚠️ 同样是推断值：IRC RFC 2812 的 512 字节行上限对 Twitch **入站不成立**
#: （Twitch 自己就发 512 字节以上的行），但出站仍按协议上限保守处理。这里取
#: 2048 而不是 512，是为了让 400 个中文字（1200 字节）也能**单条**发出 ——
#: 若按 512 收，每条中文消息都会被切成 3 段。
LINE_BUDGET = 2048
#: 入站单行字节上限（只防"服务端或中间人塞爆内存"，远超正常行）。
INBOUND_LINE_LIMIT = 16 * 1024

WS_TIMEOUT = 30.0            # WebSocket 读超时（空闲由 Twitch 的 PING 兜底）
REGISTER_TIMEOUT = 30.0      # 等 001 (RPL_WELCOME) 的上限
RECONNECT_DELAY = 5.0        # 首次重连延迟（秒）
MAX_RECONNECT_DELAY = 60.0   # 重连退避上限（秒）
MIN_SEND_INTERVAL = 1.5      # ≈ 20 条 / 30 秒（社区经验值）
API_TIMEOUT = 10.0           # Helix 查询自己的超时
SEEN_IDS_MAX = 1024          # 入站消息 id 去重窗口（防重连后重复投递）

#: 传给传输层的 ``reset_after``：**刻意取一个极小的正数**，而不是基类默认的 0。
#:
#: 迁移前的 ``_session_loop`` 规则是「**建连成功**就把退避重置回 ``reconnect_delay``；
#: **建连失败**才 ×2 增长」，也就是说"连不上"与"连上了但很快断"是**两种**退避。
#: 而传输层把「``_open()`` 直接失败」也算成 ``lived = 0.0``，于是 ``reset_after=0``
#: 会让两种情况都永远只等下限 —— Twitch 挂掉时会每 5s 猛重连一次、退避形同虚设。
#: 取这个极小正数就精确复刻那条规则：建连失败 ⇒ 不算稳定 ⇒ 走 ×2 增长；
#: 建连成功（哪怕下一毫秒就断）⇒ 算稳定 ⇒ 重置回下限。
RECONNECT_STABLE_AFTER = 0.001

CAP_TAGS = "twitch.tv/tags"
CAP_COMMANDS = "twitch.tv/commands"
CAP_MEMBERSHIP = "twitch.tv/membership"

RPL_WELCOME = "001"


# ---------------------------------------------------------------------------
# IRCv3 标签解析
# ---------------------------------------------------------------------------
def _unescape_tag_value(value: str) -> str:
    """还原 IRCv3 的标签转义：``\\:`` ``\\s`` ``\\\\`` ``\\r`` ``\\n``。

    末尾落单的 ``\\`` 按 RFC 视为字面反斜杠。逐字符扫描，不做正则替换 ——
    否则 ``\\\\s`` 会被替换成 ``\\s`` 再被当成空格。
    """
    if "\\" not in value:
        return value
    out: List[str] = []
    i, n = 0, len(value)
    while i < n:
        ch = value[i]
        if ch != "\\" or i + 1 >= n:
            out.append(ch)
            i += 1
            continue
        nxt = value[i + 1]
        if nxt == ":":
            out.append(";")
        elif nxt == "s":
            out.append(" ")
        elif nxt == "\\":
            out.append("\\")
        elif nxt == "r":
            out.append("\r")
        elif nxt == "n":
            out.append("\n")
        else:
            out.append(nxt)      # 未知转义：只丢掉反斜杠
        i += 2
    return "".join(out)


def _parse_tags(section: str) -> dict[str, str]:
    """把标签段解析成 dict。

    必须逐字符扫描：标签值里可以有**未转义的含义为分隔符**的字符（``\\:`` /
    ``\\s``），用 ``split(";")`` 会把 ``display-name=A\\;B`` 切错。
    值缺失（``@mod`` 没有 ``=``）按"空值"处理，值本身为空（``@mod=``）保留空串。
    """
    tags: dict[str, str] = {}
    key: List[str] = []
    value: List[str] = []
    in_value = False
    pending_escape = False

    def flush() -> None:
        if key:
            tags["".join(key)] = _unescape_tag_value("".join(value))
        key.clear()
        value.clear()

    for ch in section:
        if pending_escape:
            pending_escape = False
            if in_value:
                value.append("\\")
            value.append(ch)
            continue
        if ch == "\\":
            pending_escape = True
            continue
        if not in_value and ch == "=":
            in_value = True
            continue
        if ch == ";":
            flush()
            in_value = False
            continue
        if in_value:
            value.append(ch)
        else:
            key.append(ch)
    if pending_escape:          # 落单的反斜杠
        if in_value:
            value.append("\\")
    flush()
    return tags


def _parse_twitch_line(raw: str) -> Tuple[dict[str, str], str, str, List[str]]:
    """一行 Twitch IRC → ``(tags, prefix, COMMAND, params)``。

    切分顺序（每一步都只认"第一个"标记，因为后面的内容可以合法地含这些字符）：

    1. 行首 ``@`` 的标签段，按**第一个空格**结束；
    2. 行首 ``:`` 的 prefix，按**第一个空格**结束；
    3. 参数里按**第一个 ``" :"``** 切出末位参数（它可以含空格与冒号）。
    """
    rest = raw.rstrip("\r\n")
    tags: dict[str, str] = {}
    if rest.startswith("@"):
        section, sep, rest = rest[1:].partition(" ")
        if sep:
            tags = _parse_tags(section)
        else:
            rest = ""            # 只有标签没有命令：畸形，丢弃
    prefix = ""
    if rest.startswith(":"):
        prefix, _, rest = rest[1:].partition(" ")
    head, sep, trailing = rest.partition(" :")
    params = head.split()
    if sep:
        params.append(trailing)
    command = params.pop(0).upper() if params else ""
    return tags, prefix, command, params


# ---------------------------------------------------------------------------
# 适配器
# ---------------------------------------------------------------------------
@register("twitch")
class TwitchAdapter(Adapter):
    """Twitch 聊天适配器（IRC over WSS）。"""

    name = "twitch"
    label = "Twitch"
    max_message_length = MESSAGE_LIMIT          # 保守字符上限（见常量注释）
    supports_inbound = True
    #: ⛔ **显式 False** —— 判据 (c) 不成立：Twitch 的 IRC 通道**不认证**任何用户，
    #: 任何客户端都能声称任何 nick。配对码发出去也证明不了发件人是谁。
    pairing_supported = False
    supports_inline_buttons = False             # IRC 协议里没有按钮
    supports_media = False

    #: Twitch 社区命令习惯是 ``!command``，不是 ``/command``。
    typed_command_prefix = "!"

    #: 入站必需 = token + channel（缺任一都连不上/收不到）。
    #: ``nick`` **不在**这里：可以用 ``client_id`` + Helix API 查出来
    #: （见 :meth:`_resolve_identity`），因此它是可选增强而不是硬前置。
    required_tokens = ("token", "channel")
    #: 出站同样需要 token（鉴权）与 channel（知道往哪发），是 required 的子集。
    outbound_tokens = ("token", "channel")

    # 类级旋钮（测试可在实例上覆盖）。
    line_budget = LINE_BUDGET
    inbound_line_limit = INBOUND_LINE_LIMIT
    min_interval = MIN_SEND_INTERVAL
    ws_timeout = WS_TIMEOUT
    register_timeout = REGISTER_TIMEOUT
    reconnect_delay = RECONNECT_DELAY
    max_reconnect_delay = MAX_RECONNECT_DELAY

    def __init__(self, config: dict, hooks: Hooks) -> None:
        super().__init__(config, hooks)
        self.token: str = str(self.config.get("token") or "").strip()
        #: Twitch API 需要 ``Client-Id`` + ``Authorization: Bearer`` 两个头，
        #: 缺 Client-Id 就查不了 user id（防回环会降级，见类 docstring）。
        self.client_id: str = str(self.config.get("client_id") or "").strip()
        self.nick: str = str(self.config.get("nick") or "").strip()
        self.display_name: str = str(self.config.get("display_name") or "").strip()
        self.membership: bool = bool(self.config.get("membership"))
        self.raw_channel: str = str(self.config.get("channel") or "").strip()
        #: 官方端点固定为上面的 wss 地址；配置可覆盖（自建网关 / 测试用回环 ws）。
        self.endpoint: str = str(
            self.config.get("endpoint") or TWITCH_ENDPOINT
        ).strip()
        # allowed_chat_ids 已由基类 _init_access() 统一解析（T1.2）
        self.channel: str = self._normalize_channel(self.raw_channel)
        self.user_id: str = str(self.config.get("user_id") or "").strip()
        self._identity: dict[str, str] = {}
        # 线程与连接都归传输层所有（``start()`` 之后才有；见「传输层接缝」一节）。
        self._transport: Optional[WebSocketTransport] = None
        self._send_lock = threading.Lock()
        self._throttle_lock = threading.Lock()
        self._last_send: dict[str, float] = {}
        self._seq_lock = threading.Lock()
        self._seq = 0
        self._rbuf = ""
        self._registered = False
        #: 本次会话是否已被适配器主动作废（见 :meth:`_void_session`）。置位后，
        #: 同一帧里剩下的 IRC 行不再处理（迁移前是抛异常跳出读循环）。
        self._session_voided = False
        #: 注册超时定时器（收到 001 或会话结束时取消，见 :meth:`_on_open` / :meth:`_on_close`）。
        self._register_timer: Optional[threading.Timer] = None
        self._room_users = 0               # membership 粗略计数（近似值）
        self._seen_lock = threading.Lock()
        self._seen_ids: List[str] = []
        self._seen_id_set: set[str] = set()
        self._nick_res: List[Optional[Any]] = []
        self._rebuild_mentions()

    # ------------------------------------------------------------------
    # 配置辅助
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_channel(raw: str) -> str:
        """频道名补 ``#``；Twitch 的 JOIN/PRIVMSG 都用 ``#channel``。"""
        name = str(raw or "").strip()
        if not name:
            return ""
        return name if name.startswith("#") else f"#{name}"

    @property
    def raw_token(self) -> str:
        """去掉 ``oauth:`` 前缀的裸 token。

        用户常把控制台里的 ``oauth:xxxx`` 整段粘进来；``PASS`` 又必须补前缀，
        不去重就会发出 ``oauth:oauth:xxxx`` 而鉴权失败。
        """
        token = self.token
        if token.lower().startswith("oauth:"):
            token = token[len("oauth:"):]
        return token

    def _rebuild_mentions(self) -> None:
        """按当前 nick / display_name 重建提及正则（昵称可能来自 API，稍后才确定）。"""
        seen: set[str] = set()
        res: List[Optional[Any]] = []
        for candidate in (self.nick, self.display_name):
            key = candidate.lower()
            if not candidate or key in seen:
                continue
            seen.add(key)
            res.append(_make_mention_re(candidate))
        self._nick_res = res or [None]

    def _caps(self) -> str:
        """要协商的 capability 列表。"""
        caps = [CAP_TAGS, CAP_COMMANDS]
        if self.membership:
            caps.append(CAP_MEMBERSHIP)
        return " ".join(caps)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        """缺 ``token`` / ``channel`` 时只告警并返回（不抛异常、不起线程）。"""
        if not self.token:
            logger.warning("twitch: token missing; adapter not started")
            return
        if not self.raw_channel:
            logger.warning("twitch: channel missing; adapter not started")
            return
        self._stop_event.clear()
        transport = self._make_transport()
        self._transport = transport
        transport.start(self._on_event)
        logger.info(
            "twitch: connecting to %s as %s, channel %s (client_id=%s, membership=%s)",
            self.endpoint,
            self.nick or "(待 Helix 查询)",
            self.channel,
            "yes" if self.client_id else "no",
            self.membership,
        )

    def stop(self) -> None:
        """置停止位 → 关 WS 让阻塞中的 ``recv()`` 立刻返回 → join 线程（**幂等**）。

        顺序不能反：传输层的 ``join`` 只等 5s，而读循环阻塞在 ``ws.recv()`` 上
        最长 ``ws_timeout``（默认 30s）。迁移前这段是手写的（``_ws_lock`` 里取出连接
        → :meth:`_close_ws` → ``super().stop()``），现在由
        :meth:`~opencode_bridge.transport.Transport.stop` 统一保证
        （关连接那步还多了 ``shutdown`` 唤醒阻塞中的读），语义没变。
        """
        super().stop()                      # 置停止位（_throttle 依赖它）
        transport, self._transport = self._transport, None
        if transport is not None:
            transport.stop()

    @staticmethod
    def _close_ws(ws: Any) -> None:
        """关连接：先 ``shutdown`` 唤醒阻塞中的 ``recv``，再 ``close``。

        :mod:`opencode_bridge.ws` 没有暴露底层 socket，这里做一次**防御性**
        ``getattr``：取不到就只调 ``close()``（功能退化，但不会崩）。
        """
        sock = getattr(ws, "_sock", None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:  # noqa: BLE001 - 关闭路径不能抛
                pass
        try:
            ws.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug("twitch: ws close during stop failed: %s", exc)

    def _make_ws(self):
        """建 WS 连接（生产走仓库自研的 :mod:`opencode_bridge.ws`）。"""
        from ..ws import connect as factory  # 延迟导入
        return factory(self.endpoint, timeout=self.ws_timeout)

    # ------------------------------------------------------------------
    # 传输层接缝（会话：连接 → 注册 → JOIN → 读帧 → 退避重连）
    # ------------------------------------------------------------------
    @property
    def transport(self) -> Optional[WebSocketTransport]:
        """当前传输层（``start()`` 之后才有；测试与 :meth:`_current_ws` 看它）。"""
        return self._transport

    @property
    def running(self) -> bool:
        """消费线程是否活着（代理到传输层）。

        ⚠️ 基类的 :attr:`Adapter._thread` 现在**恒为 None**（入站线程由传输层持有，
        名字是 ``transport:twitch``）。``core.py`` 的 ``_adapter_for`` 靠前缀 / 映射
        找适配器，不依赖线程匹配。
        """
        transport = self._transport
        return transport is not None and transport.running

    def _make_transport(self) -> WebSocketTransport:
        """构造本次运行用的传输层（测试注入点：退避接线值）。

        退避**逐字对齐迁移前** ``_session_loop`` 那两条路径：

        * ``min_backoff=self.reconnect_delay`` / ``max_backoff=self.max_reconnect_delay``
          —— 迁移前"连上过之后"每次都等固定的 ``reconnect_delay``（原代码那一支是
          ``wait(self.reconnect_delay)``，**没有** ×2 增长），所以传输层的
          ``min_backoff`` 必须正好等于它；
        * ``reset_after=RECONNECT_STABLE_AFTER``（极小的正数，见常量注释）——
          传输层把「``_open()`` 直接失败」也算 ``lived = 0.0``，只有这个值才能让
          **建连失败**继续走 ×2 增长（迁移前那支是
          ``wait(min(delay, max)); delay = min(delay*2, max)``），而**建连成功**
          立刻把退避重置回下限。

        ⚠️ 刻意读**实例属性** ``self.reconnect_delay``（不是模块全局）：迁移前
        ``_session_loop`` 也是运行时读实例属性，既有测试靠在实例上覆盖它来缩短等待。
        """
        return WebSocketTransport(
            self._open_socket,
            on_open=self._on_open,
            on_message=self._on_frame,
            on_close=self._on_close,
            name="twitch",
            min_backoff=self.reconnect_delay,
            max_backoff=self.max_reconnect_delay,
            reset_after=RECONNECT_STABLE_AFTER,
        )

    def _open_socket(self) -> Any:
        """建一次连接；失败按原样抛给传输层退避重试。

        对应迁移前 ``_session_loop`` 开头那两行（``try: ws = self._make_ws()`` +
        ``logger.warning("twitch: connect failed: %s", exc)``），两者都逐字保留。
        """
        try:
            return self._make_ws()
        except Exception as exc:  # noqa: BLE001 - 交给传输层退避重试
            logger.warning("twitch: connect failed: %s", exc)
            raise

    def _on_open(self, conn: Any) -> None:
        """传输层的 ``on_open`` 钩子：每次连上（每次重连）都跑一遍。

        PASS → NICK → USER → CAP REQ，并起一个注册超时定时器。对应迁移前
        ``_register`` 的前半段，逐字保留顺序与命令内容。Twitch 明确要求：``NICK``
        只填**用户名**（小写），不是老式的 ``NICK user justin``；``USER`` 虽被 Twitch
        忽略，但协议要求必须有。

        ⚠️ **这里不再读数据**。迁移前 ``_register`` 自己调 :meth:`_read_loop` 读到
        001 为止（读不到就 ``raise TimeoutError``）；现在读一律由传输层的消费循环做
        （本适配器的 ``on_message`` 钩子），于是"等 001"变成一个**超时判定**：
        定时器到点就 :meth:`_void_session` 关掉连接，阻塞中的 ``recv()`` 立刻返回，
        会话结束 → 退避 → 重连。归宿与迁移前那条 ``raise TimeoutError`` 完全一样。

        抛异常 = 本次会话作废（拿不到 nick 时就是：既没配 nick 也没 client_id）——
        基类关连接、退避重试，**不会**卡在"连上但没注册"。
        """
        self._resolve_identity()
        if not self.nick:
            raise RuntimeError(
                "无法确定 Twitch 用户名：既没有配置 nick，也没有 client_id 可查 Helix"
            )
        self._rbuf = ""
        self._registered = False
        self._session_voided = False
        self._send_line(conn, f"PASS oauth:{self.raw_token}")
        self._send_line(conn, f"NICK {self.nick.lower()}")
        self._send_line(conn, f"USER {self.nick.lower()} 0 * :{self.display_name or self.nick}")
        self._send_line(conn, f"CAP REQ :{self._caps()}")
        # 读循环是**阻塞**在 ``ws.recv()`` 上的（socket 超时 ``ws_timeout`` 默认 30s），
        # 所以注册超时不能靠"消费循环里检查 deadline"—— 那样要等 30s 才发现。改为起一个
        # 定时器，到点直接关掉 WS，把阻塞中的 recv 唤醒，让传输层正常走退避重连。
        timer = threading.Timer(self.register_timeout, self._force_close, args=(conn,))
        timer.daemon = True
        self._register_timer = timer
        timer.start()

    def _on_close(self, conn: Any) -> None:
        """传输层的 ``on_close`` 钩子：会话收尾。对应迁移前 ``_session_loop`` 的 ``finally``。

        三件事，顺序与迁移前一致：取消注册定时器 → 清 ``_registered`` → 丢弃残留
        半行缓冲（半行说明连接已错位，重连后必须重新开始）。关连接那步由基类在本钩子
        **之后**做（迁移前是适配器自己关）；顺序反过来无差异 —— :meth:`_close_ws`
        不读也不写上面任何一个状态。
        """
        timer, self._register_timer = self._register_timer, None
        if timer is not None:
            timer.cancel()
        self._registered = False
        self._rbuf = ""

    def _on_frame(self, conn: Any, frame: Any) -> None:
        """传输层的 ``on_message`` 钩子：一条**原始帧** → 按 ``\\n`` 切行 → 逐行派发。

        **一帧不等于一行**：Twitch 常把多行塞进同一帧，:mod:`ws` 聚合分片后也可能
        只给出半行。因此缓冲必须跨帧存活（:attr:`_rbuf` 是唯一读入口的实例状态）。
        迁移前这段是 :meth:`_read_loop` 的循环体，逐行保留。

        注意缓冲是 **str**（不可变），所以每次改动都必须写回 :attr:`_rbuf` ——
        用局部变量 ``buf += frame`` 只会重绑局部名，残留半行会跟着丢掉。
        """
        if not isinstance(frame, str):
            logger.warning("twitch: 非文本帧 %r，已忽略", type(frame))
            return
        self._rbuf += frame
        while "\n" in self._rbuf:
            line, _, _rest = self._rbuf.partition("\n")
            self._rbuf = self._rbuf[len(line) + 1:]
            self._handle_line(conn, line.rstrip("\r"))
            if self._session_voided:
                # 本次会话已被作废（RECONNECT / 注册超时 → 连接已关）：
                # 同一帧里剩下的行不再处理 —— 迁移前是抛异常跳出这个循环。
                return
        if len(self._rbuf.encode("utf-8")) > self.inbound_line_limit:
            logger.warning(
                "twitch: 入站行超长（%d 字节），丢弃",
                len(self._rbuf.encode("utf-8")),
            )
            self._rbuf = ""

    def _on_event(self, item: Any) -> None:
        """传输层的 ``on_event`` 回调：**刻意是 no-op**。

        :class:`~opencode_bridge.transport.WebSocketTransport` 在每条原始帧到达时
        先调 ``on_message(conn, frame)``（本适配器接的是 :meth:`_on_frame`，切行与派发
        都在那里做完了）、**再**把同一帧交给 ``on_event``。这里必须什么都不做，
        否则同一条消息会被投递两次（core 会当成两条消息，bot 也会回两次）。
        保留这个空实现（而不是给 ``start()`` 传 ``lambda _: None``）是为了让
        「同一帧会被两个钩子看到」这件事在代码里是**显式可见**的。
        """

    def _void_session(self, conn: Any) -> None:
        """主动结束本次会话：置标记 + 关连接，让下一次 ``recv()`` 返回 ``None``。

        为什么**关连接**而不是抛异常：传输层的 ``on_message`` 钩子会把普通异常
        **吞掉并继续消费这条连接**（见 :mod:`opencode_bridge.transport.websocket` 的
        :meth:`~opencode_bridge.transport.WebSocketTransport._next`），抛了等于没抛；
        能立刻重连的只有 :class:`~opencode_bridge.transport.ReconnectNow`，但它会
        **跳过退避**。而迁移前这两条路径（服务端 ``RECONNECT``、注册超时）都是
        「抛异常 → 结束会话 → 等 ``reconnect_delay`` → 重连」。关连接走的正是基类
        正常的"会话结束 → 退避 → 重连"路径，时序逐字不变。
        """
        self._session_voided = True
        self._close_ws(conn)

    def _force_close(self, ws: Any) -> None:
        """注册超时：作废本次会话（关掉 WS，唤醒阻塞中的 ``recv()``）。"""
        logger.warning("twitch: registration timed out; closing connection")
        self._void_session(ws)

    def _join(self, ws: Any) -> None:
        """发 ``JOIN``。**只在收到 001 之后调一次**（见 :meth:`_handle_numeric`）。"""
        self._send_line(ws, f"JOIN {self.channel}")
        logger.info("twitch: joined %s as %s", self.channel, self.nick)

    # ------------------------------------------------------------------
    # 行派发
    # ------------------------------------------------------------------
    def _handle_line(self, ws: Any, line: str) -> None:
        if not line:
            return
        tags, prefix, command, params = _parse_twitch_line(line)
        if not command:
            return
        if command == "PING":
            # Twitch 固定发 ``PING :tmi.twitch.tv``，原样回即可
            self._send_line(ws, f"PONG :{params[0] if params else TMI_HOST}")
            return
        if command == "PRIVMSG":
            self._handle_privmsg(line, tags, prefix, params)
            return
        if command == "CAP":
            if params and params[0].upper() == "ACK":
                logger.info("twitch: CAP ACK %s", params[-1])
            return
        if command == "RECONNECT":
            # Twitch 让客户端重连（做服务端侧重平衡）；作废本次会话即可触发重连。
            # ⚠️ 迁移前这里是 ``raise ConnectionError(...)``，见 :meth:`_void_session`
            # 说明为什么改成"关连接"（抛异常会被传输层的 on_message 吞掉）。
            logger.info("twitch: server requested RECONNECT")
            self._void_session(ws)
            return
        if command == "NOTICE":
            logger.info("twitch: notice: %s", params[-1] if params else "")
            return
        if command.isdigit():
            self._handle_numeric(command, params)
            return
        if command in ("JOIN", "PART"):
            self._handle_membership(command, prefix, params)

    def _handle_numeric(self, code: str, params: List[str]) -> None:
        if code == RPL_WELCOME:
            self._registered = True
            logger.info("twitch: registered as %s (001)", self.nick)
            # 迁移前这里是"注册用的读循环读到 001 就返回 → 上层调 :meth:`_join`"；
            # 现在读循环归传输层，所以 JOIN 就地发出 —— **仍在下一行被处理之前**，
            # 对服务端是同一个顺序（Twitch 也是 JOIN 之后才开始推成员事件）。
            self._join(self._current_ws())
        # 375/372/376（MOTD）与 353/366（names）都不需要处理，忽略即可。

    def _handle_membership(self, command: str, prefix: str, params: List[str]) -> None:
        """维护一个**近似**的成员计数（仅在协商了 membership 时才有事件）。"""
        if not self.membership:
            return
        if command == "JOIN":
            self._room_users += 1
        else:
            self._room_users = max(0, self._room_users - 1)
        logger.debug("twitch: %s %s (users≈%d)", command, _prefix_nick(prefix),
                     self._room_users)

    def room_user_count(self) -> int:
        """近似成员数；未协商 ``twitch.tv/membership`` 时恒为 0。"""
        return self._room_users

    def _current_ws(self) -> Any:
        """当前 WS 连接（**未连接时为 ``None``**；由传输层持有）。

        出站 :meth:`send` 与 :meth:`_handle_numeric` 都要用它。
        """
        transport = self._transport
        return transport.connection if transport is not None else None

    def _handle_privmsg(
        self, raw: str, tags: dict[str, str], prefix: str, params: List[str]
    ) -> None:
        if len(params) < 2:
            return
        target, body = params[0], params[-1]
        nick = _prefix_nick(prefix)
        user_id = (tags.get("user-id") or "").strip()
        display_name = (tags.get("display-name") or "").strip() or nick
        if self._is_echo(user_id, nick, display_name):
            return
        is_private = bool(self.nick) and target.lower() == self.nick.lower()
        if not is_private:
            # 频道消息只响应提及（刻意取舍：Twitch 频道太吵，见模块 docstring）
            if not self._mentioned(body):
                return
            text = self._strip_any_mention(body)
        else:
            text = body
        text = strip_control_codes(text)
        if not text:
            return
        message_id = (tags.get("id") or "").strip()
        if message_id and not self._remember_id(message_id):
            return          # 重连后 Twitch 可能重发，丢弃重复
        # 授权闸门在最前：未授权频道的消息不许进入上层（否则能用命令/审批字绕过）
        if not self.admits(target) and not self.answer_pairing_request(
            target, self._conversation_id(target), text
        ):
            logger.info("twitch: dropping message from non-whitelisted channel %s", target)
            return
        try:
            self.hooks.on_inbound(
                Inbound(
                    conversation_id=self._conversation_id(target),
                    text=text,
                    kind="text",
                    user_id=user_id or nick or None,
                    message_id=message_id or None,
                    platform=self.name,
                    raw=raw,
                )
            )
        except Exception as exc:
            logger.exception("twitch: on_inbound failed: %s", exc)

    # ------------------------------------------------------------------
    # 防回环 / 提及 / 去重
    # ------------------------------------------------------------------
    def _is_echo(self, user_id: str, nick: str, display_name: str) -> bool:
        """判"是不是我自己发的"。

        主判据是**身份**而不是权限：``user-id``（需 Helix 查到，已缓存）→ 自身
        nick（大小写不敏感）→ 自身 display-name。刻意**不用** ``user-type`` / ``mod``
        当判据：Twitch 对自己发的消息同样可能带这些 tag，用它们判会把别人的发言
        误丢。

        拿不到 user id 时（没配 ``client_id``，或 Helix 不可用）自动降级为 nick /
        display-name 比较 —— 这是**降级但仍然有效**的方案（nick 在 Twitch 频道里
        全局唯一），只是"改了 display-name 的人"这一种绕过方式挡不住。
        """
        if self.user_id and user_id and user_id == self.user_id:
            return True
        if self.nick and nick and nick.lower() == self.nick.lower():
            return True
        if self.display_name and display_name and (
            display_name.lower() == self.display_name.lower()
        ):
            return True
        return False

    def _mentioned(self, text: str) -> bool:
        return any(r.search(text or "") for r in self._nick_res if r is not None)

    def _strip_any_mention(self, text: str) -> str:
        """剥掉第一个命中的提及及其 ``:`` / ``,`` / ``!user@host`` 分隔形式。"""
        for regex in self._nick_res:
            if regex is None:
                continue
            match = regex.search(text or "")
            if match is None:
                continue
            end = match.end()
            if end < len(text) and text[end] == "!":
                end += 1
                while end < len(text) and not text[end].isspace():
                    end += 1
            while end < len(text) and text[end] in ":,":
                end += 1
            return text[end:].lstrip()
        return text

    def _remember_id(self, message_id: str) -> bool:
        """记住消息 id；``False`` 表示这是重复投递，应丢弃。"""
        with self._seen_lock:
            if message_id in self._seen_id_set:
                return False
            self._seen_id_set.add(message_id)
            self._seen_ids.append(message_id)
            while len(self._seen_ids) > SEEN_IDS_MAX:
                self._seen_id_set.discard(self._seen_ids.pop(0))
            return True

    # ------------------------------------------------------------------
    # Helix：查自己的 user id / login（可选增强）
    # ------------------------------------------------------------------
    def _resolve_identity(self) -> dict[str, str]:
        """查自己的 ``user id`` / ``login`` / ``display_name``（尽力而为，失败降级）。

        需要 ``client_id`` + ``token`` 两个配置项（Twitch API 要求
        ``Client-Id`` 与 ``Authorization: Bearer`` **两个头**）。查不到就用配置里的
        ``nick`` 继续跑，只是防回环少一道判据。
        """
        if self.user_id and self.nick:
            return self._identity
        if not (self.raw_token and self.client_id):
            return self._identity
        status, data = self._http_get(HELIX_USERS_URL)
        if status < 200 or status >= 300:
            detail = str((data or {}).get("message") or f"HTTP {status}")
            logger.warning("twitch: Helix 用户查询失败（HTTP %s）: %s", status, detail)
            # 记录到可观测通道：身份查不到会让防回环降级，用户需要知道为什么。
            self._note_send_failure(classify_http(status, detail),
                                    f"identity lookup failed: {detail}")
            return self._identity
        entries = data.get("data") if isinstance(data, dict) else None
        if not isinstance(entries, list) or not entries:
            return self._identity
        first = entries[0] if isinstance(entries[0], dict) else {}
        self.user_id = str(first.get("id") or "").strip() or self.user_id
        login = str(first.get("login") or "").strip()
        self.display_name = (
            str(first.get("display_name") or "").strip() or self.display_name
        )
        if login and not self.nick:
            self.nick = login
        self._identity = {"id": self.user_id, "login": login,
                          "display_name": self.display_name}
        self._rebuild_mentions()
        logger.info("twitch: identity resolved (user_id=%s nick=%s)", self.user_id, self.nick)
        return self._identity

    def _http_get(self, url: str) -> Tuple[int, dict]:
        """``GET`` JSON（只用 Helix）。绝不抛；返回 ``(status, data)``。"""
        req = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Client-Id": self.client_id,
                "Authorization": f"Bearer {self.raw_token}",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
                status = getattr(resp, "status", 200) or 200
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                raw = exc.read()
            except Exception:
                raw = b""
        except Exception as exc:
            logger.warning("twitch: Helix transport error: %s", exc)
            return 0, {"message": f"transport error: {exc}"}
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            return status, {"message": "non-JSON response"}
        return status, data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------
    # 出站
    # ------------------------------------------------------------------
    @staticmethod
    def _conversation_id(target: Any) -> str:
        """``twitch`` 在 ``identity.LEGACY_PREFIXES`` 里**映射到自身**，所以这里改用
        ``format_id`` 的产物与原来的 ``f"twitch:{target}"`` **逐字节相同** ——
        已落盘的 ``state.json`` 不受影响（这三家irc / twitch / nextcloud 都享有
        这个性质；``chat:`` / ``room:`` / ``channel:`` 不享有）。

        :meth:`_target` **刻意保留手写剥前缀**：它必须容忍无前缀的裸 target，
        而 ``identity.local_of()`` 对那种输入会抛错 —— 换成它就是行为变更。
        """
        return format_id("twitch", target)

    @staticmethod
    def _target(conversation_id: Any) -> Optional[str]:
        raw = str(conversation_id or "")
        if raw.startswith("twitch:"):
            raw = raw[len("twitch:"):]
        return raw or None

    def _body_budget(self, target: str) -> int:
        """正文可用字节 = ``line_budget - "PRIVMSG " - 目标 - " :"``。"""
        overhead = len("PRIVMSG ") + len(target.encode("utf-8")) + 1 + 1
        return max(0, self.line_budget - overhead)

    def _outbound_pieces(self, text: str, target: str) -> List[str]:
        """先按字符上限切（与其余平台一致），再按字节兜底（不切出乱码）。"""
        budget = self._body_budget(target)
        pieces: List[str] = []
        for chunk in split_text(text, self.effective_max_length, prefix_fmt=""):
            pieces.extend(_byte_safe_split(chunk, budget))
        return pieces

    def _throttle(self, conversation_id: str) -> None:
        """最小发送间隔 ≈ 20 条 / 30 秒（Twitch 对未认证应用的社区经验上限）。

        真超了 Twitch 会直接断开连接，所以这里**等满**而不是"少等一点"；
        ``stop()`` 置位后立刻返回。
        """
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

    def _next_seq(self) -> str:
        with self._seq_lock:
            self._seq += 1
            return str(self._seq)

    def _send_line(self, ws: Any, line: str) -> bool:
        """发一条 IRC 行（WebSocket 文本帧）。**行内不得出现 CR/LF**。

        兜底：整行超预算时按字符边界截断（宁可少发也不让对端丢连接）。
        """
        line = line.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
        payload = line
        encoded = payload.encode("utf-8")
        if len(encoded) > self.line_budget:
            payload = _fit_utf8(payload, self.line_budget)
            logger.warning(
                "twitch: truncating outbound line to %d bytes (budget %d)",
                len(payload.encode("utf-8")), self.line_budget,
            )
        try:
            with self._send_lock:
                ws.send(payload)
            return True
        except Exception as exc:
            logger.warning("twitch: send failed: %s", exc)
            return False

    def send(self, out: Outbound) -> MsgHandle | None:
        """``PRIVMSG #channel :<text>``（超长自动分片，返回最后一片的句柄）。"""
        target = self._target(out.conversation_id)
        if not target:
            logger.warning("twitch: bad conversation_id %r", out.conversation_id)
            self._note_send_failure(SendError.BAD_FORMAT, "bad conversation_id")
            return None
        if not out.text:
            logger.warning("twitch: refusing to send empty text")
            self._note_send_failure(SendError.BAD_FORMAT, "empty text")
            return None
        if not self.token:
            logger.warning("twitch: token missing; send refused")
            self._note_send_failure(SendError.BAD_FORMAT, "token missing")
            return None
        target = self._normalize_channel(target)
        ws = self._current_ws()
        if ws is None:
            logger.warning("twitch: not connected; dropping outbound message")
            self._note_send_failure(SendError.TRANSIENT, "not connected")
            return None
        pieces = self._outbound_pieces(out.text, target)
        if len(pieces) > 1:
            logger.info("twitch: splitting outbound message into %d lines", len(pieces))
        handle: MsgHandle | None = None
        for piece in pieces:
            self._throttle(out.conversation_id)
            if not self._send_line(ws, f"PRIVMSG {target} :{piece}"):
                self._note_send_failure(SendError.TRANSIENT, "websocket send failed")
                return handle if handle is not None else None
            # Twitch IRC 不回消息 id（PRIVMSG 发出即无回执），用本地序号占位；
            # edit() 恒 False，所以这个句柄只用于日志定位。
            handle = MsgHandle(
                conversation_id=out.conversation_id,
                message_id=self._next_seq(),
                platform=self.name,
            )
        return handle

    def edit(self, handle: MsgHandle, out: Outbound) -> bool:
        """Twitch IRC 没有编辑消息 —— **恒返回 False**。

        ``core.py`` 拿到 False 会退化成发一条新消息，这正是 IRC/Twitch 该有的
        行为（``edit`` 原消息做不到）。
        """
        logger.debug("twitch: edit unsupported; caller should send a new message")
        return False

    def answer(self, query_id: str, text: str = "") -> None:
        """Twitch IRC 没有 callback query —— 永远 no-op。"""
        return None

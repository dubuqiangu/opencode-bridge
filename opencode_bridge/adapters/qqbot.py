"""B2 —— QQ 机器人开放平台适配器（腾讯 QQ 官方机器人）。标准库 only。

传输层用 :class:`~opencode_bridge.transport.WebSocketTransport`（A1）+ 自研
:mod:`opencode_bridge.ws`（T2.0），**零第三方依赖**；出站走官方 OpenAPI REST。

⚠️ 本文件的每一条协议事实都写明了出处。凡是**官方文档里查不到**的，一律在注释里
标成"查不到 / 推断"，绝不把推断写成事实（见 ``docs/architecture.md`` 不变量 19）。

已核实的协议事实（出处见 ``docs`` 段与各常量注释）
----------------------------------------------------

1. **网关地址不是硬编码**：先 ``GET /gateway``（或 ``/gateway/bot``）拿地址，返回形如
   ``{"url": "wss://api.bot.qq.com/websocket/"}``。
   `<https://bot.q.qq.com/wiki/develop/api-v2/openapi/wss/url_get.html>` 与
   `<.../wss/shard_url_get.html>`。**不硬编码主机名**（官方会换域名），启动时问一次
   REST 并缓存。
2. **握手不带任何凭据**。QQ 网关的 WebSocket 握手是**匿名**的：鉴权发生在连上之后的
   **op 2 Identify** 里，``d.token`` 格式为 ``"QQBot {AccessToken}"``。
   `<https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/event-emit.html>`
   → 注意官方两页对 token 格式有出入：较新的 event-emit 页写 ``QQBot {AccessToken}``，
   较旧的 reference.html 页写已废弃的 ``Bot {appid}.{app_token}``。本适配器只用
   **access_token**（``启动接入`` 页已把 bot Token 标为"已弃用"）。
   **因此本文件既不把凭据放进 query string，也不放进握手 header** —— 官方没有这个要求，
   放了反而多一处泄漏面。
3. **心跳 op 1，``heartbeat_interval`` 单位是毫秒**。官方原文："一旦连接成功，就会返回
   OpCode 10 Hello 消息。这个消息主要的内容是心跳周期，**单位毫秒(milliseconds)**"
   示例 ``{"op": 10, "d": {"heartbeat_interval": 45000}}``（同 event-emit.html 与
   reference.html，两处一致）。**这与 Discord 相同（毫秒）**，但本项目已经踩过一次"按秒
   用导致心跳快 1000 倍"的坑，所以 :meth:`_on_hello` 里除以 1000 之后会把这个换算写进
   日志，测试也按"毫秒 → 秒"来断言。
4. **心跳与 ACK 的往返**：``d`` 带**最近收到的下行序列号 ``s``**（首次连接传 ``null``），
   服务端回 ``{"op": 11}``。**不带 ack token / 不带自增心跳序号**。
   （`d` 键必须存在，这是官方示例的字面形状。）
5. **有 resume**：op 6 ``{token, session_id, seq}``，成功后补发遗漏事件并下发
   ``{"op":0,"t":"RESUMED"}``。有 session 就优先 Resume，避免重跑 Identify 的频率配额。
   官方**没有**给 close code 语义（不像 Discord 有 4000/4008 那套），所以这里不套用
   Discord 的"非 1000 close code 才能保 session"经验 —— 那是查不到的，别假装知道。
6. **intents 是 bitmask，且带权限闸门**：官方原文"如果在鉴权的时候传递了无权限的
   ``intents``，``websocket`` 会报错，并直接关闭连接"。默认事件（``GUILDS`` /
   ``PUBLIC_GUILD_MESSAGES`` / ``GUILD_MEMBERS``）无需申请，其余需申请。
7. **防回环**：官方把 ``GROUP_AT_MESSAGE_CREATE`` 定义为"用户在群里 @机器人发送消息"、
   ``C2C_MESSAGE_CREATE`` 定义为"用户给机器人发送单聊消息" —— 即这两个事件在语义上
   就是**用户 → 机器人**。但 ``GROUP_MESSAGE_CREATE``（同属 intent ``1<<25``，需在
   开放平台单独打开"接收所有消息"）定义为"群里的**每一条**消息"，**没有**承诺排除机器人
   自己。于是 ``tasks.md`` 里"防回环天然（bot 消息不推回给自己）"这个**推断不成立**：
   它对默认的两个事件成立，对全量消息模式不成立。本适配器用**平台签发**的字段兜底
   （``author.bot`` / ``author.id`` 对比 READY 的 ``user.id``），**绝不用内容启发式**
   （``docs/architecture.md`` 不变量 14）。

查不到 / 推断的部分（**不要**当事实引用）
----------------------------------------

* **沙箱域名**：当前官方 ``API 调用指南`` 只给了统一请求地址 ``https://api.bot.qq.com``；
  沙箱域名在官方页面里以图片/代码块呈现，检索不到文本，第三方一致写
  ``https://sandbox.api.sgroup.qq.com``。本适配器把它当**默认值**并在 ``sandbox: true``
  时打一条告警说明"该域名未经官方文档核实"，同时允许 ``api_base`` 覆盖 —— 不把推断
  硬编码成事实。
* **消息长度上限**：官方**没给数字**，只给了错误码 ``40054007 消息长度超限`` /
  ``40054018 消息过长或异常``（且**没说单位是字节还是字符**）。所以
  :data:`MESSAGE_LIMIT` 是一个**自选的保守值**并显式标注；真的超限时靠上面两个错误码
  归到 ``SendError.TOO_LONG``，是**可观测失败**而不是静默截断。
* **编辑消息接口**：``群聊/单聊`` 场景**只有撤回（DELETE）**，没有编辑（消息收发概述页
  的"撤回消息"一节列出了 DELETE 端点，全篇没有编辑端点）。频道场景的错误码表里有
  ``3000000~3999999 编辑消息错误`` / ``50049 只能修改含有 keyboard 元素的消息`` /
  ``50050 修改消息时，keyboard 元素不能为空``，说明频道那个 PATCH **改的是 keyboard
  而不是正文**，且**我没能找到它的接口文档页**。因此 :meth:`edit` 诚实返回 ``False``，
  让 core 退化成发新消息。
* **heartbeat_interval 的合法区间**：官方只给了 45000 这个示例值，没给上下限。因此
  收到的值必须做区间校验（否则一个 ``0`` 会让心跳线程变成忙等）。

``docs``（本文件所有断言的出处）
--------------------------------

* 事件订阅与通知（payload / opcode / Hello / Identify / 心跳 / Resume / intents）
  <https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/event-emit.html>
* 使用 Websocket 接入（同上内容的第二份，含 token 格式的旧版表述）
  <https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/reference.html>
* 获取通用 / 带分片 WSS 接入点
  <https://bot.q.qq.com/wiki/develop/api-v2/openapi/wss/url_get.html>、
  <https://bot.q.qq.com/wiki/develop/api-v2/openapi/wss/shard_url_get.html>
* 获取访问凭证（``POST /app/getAppAccessToken``，``expires_in`` 是**字符串**）
  <https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/access-token.html>
* API 调用指南（``Authorization: QQBot {ACCESS_TOKEN}``、错误码全表）
  <https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/api-call-guide.html>
* 群 @机器人消息事件（``GROUP_AT_MESSAGE_CREATE`` + ``User``/``MessageScene`` 结构）
  <https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_at_message_create.html>
* 单聊消息事件（``C2C_MESSAGE_CREATE``）
  <https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/c2c_message_create.html>
* 发送群聊消息 / 发送单聊消息（``msg_id`` / ``msg_seq`` 被动回复、频控、错误码）
  <https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_messages.post.html>、
  <https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_users_user_openid_messages.post.html>
* 发送子频道消息（``POST /channels/{channel_id}/messages``）
  <https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/message/send.html>
* 频道消息事件（``AT_MESSAGE_CREATE`` / ``MESSAGE_CREATE``）
  <https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/message/event.html>
* 消息收发概述（被动消息有效期与次数、消息去重、撤回、**没有**编辑）
  <https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html>
* 启动接入（bot Token 已弃用）
  <https://bot.q.qq.com/wiki/develop/api-v2/>
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple

from .. import identity
from ..config_coerce import coerce_int
from ..hooks import Hooks, Inbound, MsgHandle, Outbound, SendError
from ..split import split_text
from ..transport import ReconnectNow, WebSocketTransport
from ._redactable_ids import redactable_id
from .base import Adapter, classify_http, register

logger = logging.getLogger("opencode_bridge.adapters.qqbot")

__all__ = ["QQBotAdapter", "MESSAGE_LIMIT"]


# ----------------------------------------------------------------------
# 端点 / 常量（出处见模块 docstring 的 docs 段）
# ----------------------------------------------------------------------
#: 官方 ``API 调用指南`` 的"统一请求地址"。沙箱域名见 :data:`SANDBOX_API_BASE`。
API_BASE = "https://api.bot.qq.com"
#: ⚠️ **查不到官方出处** —— 官方页面上沙箱域名是图片/代码块，文本检索不到；第三方
#: （Qwen Code 文档、MaiBot 文档）一致写这个主机名。``sandbox: true`` 时用它，并打告警。
SANDBOX_API_BASE = "https://sandbox.api.sgroup.qq.com"

ACCESS_TOKEN_PATH = "/app/getAppAccessToken"
GATEWAY_PATH = "/gateway"

#: 官方 ``expires_in`` 是**字符串**（示例 ``"7200"``），且 HTTP 200 也可能带业务错误
#: ``{"code": 100007}``。提前这么多秒刷新，避开"接近过期 60 秒内才会换新 token"的窗口。
TOKEN_REFRESH_MARGIN = 300.0
TOKEN_MIN_TTL = 0.0

#: **自选的保守上限，不是官方数字。** 官方对 ``content`` 长度只给了错误码
#: ``40054007 消息长度超限`` / ``40054018 消息过长或异常``，既没给数字也没说单位。
#: 真超限时这两个错误码会被归到 :attr:`SendError.TOO_LONG`（可观测），不会静默截断。
MESSAGE_LIMIT = 2000

#: 官方频控最紧的一档是"单关系维度 20/qpm"（群聊与单聊都是）⇒ 每条至少隔 3s。
MIN_SEND_INTERVAL = 3.0
SOCKET_TIMEOUT = 30.0

#: 被动回复（带 ``msg_id``）的有效期与次数，来自"消息收发概述"。频道场景官方没给
#: 次数，v1 **不使用**被动回复（见 :meth:`_passive_reply_fields`）。
PASSIVE_WINDOW = {"group": 300.0, "c2c": 3600.0}     # 秒
PASSIVE_MAX_REPLIES = {"group": 5, "c2c": 4}

#: 重连退避（交给传输层）。``reset_after=0`` = 连上即重置（不变量 10）。
RECONNECT_MIN = 1.0
RECONNECT_MAX = 60.0

#: 读超时只是"Linux 上 close() 不保证唤醒阻塞 recv"的兜底，**不参与健康判定**
#: —— 健康判定由应用层心跳 + op 11 ACK 负责。正常连接每收到一个 ACK 就会让 recv
#: 返回，所以这个值只要大于心跳周期就不会误伤；它同时兜住 TCP 半开（静默）连接。
WS_RECV_TIMEOUT = 120.0

#: Hello 没给合法 ``heartbeat_interval`` 时的兜底周期，取**官方示例的 45000ms**。
HEARTBEAT_DEFAULT = 45.0
#: 收到的 ``heartbeat_interval``（毫秒）必须落在这个区间内。官方只给了 45000 这个
#: 示例值、没给上下限，所以区间是**自加的防御**：``0`` / 负数 / 非数字会让心跳线程变成忙等
#: （几毫秒一发，直接把自己打挂）。下限取 100ms：远高于忙等，又不至于把"平台给了个较短
#: 的周期"误判成非法。
HEARTBEAT_MIN_MS = 100.0
HEARTBEAT_MAX_MS = 600_000.0

# 网关 opcode（官方"通用数据结构 Payload"表）
OP_DISPATCH = 0           # Dispatch：事件推送（唯一带 s / t 的 opcode）
OP_HEARTBEAT = 1          # Heartbeat：客户端发 / 服务端可要求立即发
OP_IDENTIFY = 2           # Identify：客户端鉴权
OP_RESUME = 6             # Resume：客户端恢复连接
OP_RECONNECT = 7          # Reconnect：服务端要求立刻重连
OP_INVALID_SESSION = 9    # Invalid Session：identify / resume 参数有错
OP_HELLO = 10             # Hello：连上后的第一条
OP_HEARTBEAT_ACK = 11     # Heartbeat ACK

# intents 位（官方"事件订阅 Intents"一节）
INTENT_GUILDS = 1 << 0
INTENT_GUILD_MEMBERS = 1 << 1
INTENT_GUILD_MESSAGES = 1 << 9          # 仅**私域**机器人可设
INTENT_DIRECT_MESSAGE = 1 << 12
INTENT_GROUP_AND_C2C = 1 << 25          # 群聊 + 单聊（本适配器的默认订阅）
INTENT_INTERACTION = 1 << 26
INTENT_PUBLIC_GUILD_MESSAGES = 1 << 30  # 频道 @机器人（基础事件，默认有权限）
#: 默认订阅 ``1<<25``（群 @ + 单聊）与 ``1<<30``（频道 @）。**刻意不加**：
#: ``1<<0`` / ``1<<1``（频道管理噪声）、``1<<9``（私域限定）、``1<<12``（频道私信，
#: v1 不处理）、``1<<26``（交互事件，本适配器不实现按钮）。
#: 官方明确："传递了无权限的 ``intents``，``websocket`` 会报错，并直接关闭连接" ——
#: 所以少订阅是对的方向，且可用配置 ``intents`` 覆盖。
DEFAULT_INTENTS = INTENT_GROUP_AND_C2C | INTENT_PUBLIC_GUILD_MESSAGES
#: 官方示例 ``shard: [0, 4]`` 是 4 分片；单实例用 ``[0, 1]``（原文："若无需分片，使用
#: ``[0, 1]`` 即可"）。
DEFAULT_SHARD: Tuple[int, int] = (0, 1)

#: ``shard`` 两个元素各自的"被判非法"哨兵，交给共享助手作回落值用。
#:
#: ⚠️ 刻意落在各自合法区间**之外**（``shard_id`` 要 ``>= 0``、``num_shards`` 要
#: ``>= 1``）—— 于是"拿到哨兵"与"用户真的配了这个数"不可能混淆，
#: :meth:`QQBotAdapter._config_shard` 才能用它判断"这条是不是助手已经告警过了"，
#: 从而**同一个问题只打一条告警**。
_UNUSABLE_SHARD_ID = -1
_UNUSABLE_NUM_SHARDS = 0
#: ``shard`` 两个元素在告警里的键名。写成带下标的路径，是为了让告警直接指向用户写的
#: 那一项（``shard[0]`` / ``shard[1]``），而不是笼统的 ``shard``。
_SHARD_ID_KEY = "shard[0]"
_SHARD_NUM_SHARDS_KEY = "shard[1]"

# 会话 scope → (事件名集合, 出站路径模板)
SCOPE_GROUP = "group"
SCOPE_C2C = "c2c"
SCOPE_CHANNEL = "channel"
_SCOPE_PATHS: Dict[str, str] = {
    SCOPE_GROUP: "/v2/groups/{target}/messages",
    SCOPE_C2C: "/v2/users/{target}/messages",
    SCOPE_CHANNEL: "/channels/{target}/messages",
}
#: 会话 scope → 触发它的事件名。
_SCOPE_EVENTS: Dict[str, str] = {
    SCOPE_GROUP: "GROUP_AT_MESSAGE_CREATE",
    SCOPE_C2C: "C2C_MESSAGE_CREATE",
    SCOPE_CHANNEL: "AT_MESSAGE_CREATE",
}

#: 只收纯文本：``message_type`` 只有 0 是普通文本（3=结构化卡片 / 103=引用消息，
#: 后两者的 ``content`` 是空的或只含占位空格）。收了也渲染不出东西，直接丢。
MSG_TYPE_TEXT = 0

#: 官方错误码 → 平台中立的发送失败分类（``API 调用指南`` 错误码全表）。
_ERR_TOO_LONG = frozenset({40054007, 40054018})
_ERR_RATE_LIMITED = frozenset({40034100, 1100100, 304045, 504000})
_ERR_FORBIDDEN = frozenset({
    40034105, 11251, 11252, 11253, 11254, 11262, 11265, 50045,
})
_ERR_NOT_FOUND = frozenset({40034101, 40054003, 10003, 10004})
_ERR_BAD_FORMAT = frozenset({304061, 304103, 22006, 340069, 50006, 304011})


class _QQBotTransport(WebSocketTransport):
    """QQ 网关用的传输层：每条会话结束时把适配器的**心跳看门狗**停掉。

    为什么必须是个子类：``_on_close`` 是 :class:`~opencode_bridge.transport.Transport`
    的钩子，而基类不会反过来去调适配器的方法（分层规矩：传输层不认识消息平台）。
    不停看门狗的后果是每次重连都留下一条线程守着一具已经关掉的连接 —— 到第三次重连
    就有三条僵尸线程互相抢心跳。
    """

    def __init__(self, *args: Any, on_session_end: Any = None, **kw: Any) -> None:
        self._on_session_end = on_session_end
        super().__init__(*args, **kw)

    def _on_close(self, conn: Any) -> None:
        if self._on_session_end is not None:
            self._on_session_end(conn)


def _first(config: dict, *keys: str) -> str:
    """按顺序取第一个非空字符串键（``None`` / 空白都算"没配"）。"""
    for key in keys:
        value = config.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _mask(value: str, keep: int = 4) -> str:
    """日志里只留尾 ``keep`` 位。AppID / AppSecret 官方都提示"不要对外传播"。"""
    text = str(value or "")
    if not text:
        return "(空)"
    if len(text) <= keep:
        return "*" * len(text)
    return "*" * (len(text) - keep) + text[-keep:]


def _classify_qq_error(status: int, code: Optional[int], detail: str) -> SendError:
    """官方 ``err_code`` / ``code`` + HTTP 状态码 → 平台中立分类（T1.3）。"""
    if code is not None:
        if code in _ERR_TOO_LONG:
            return SendError.TOO_LONG
        if code in _ERR_RATE_LIMITED:
            return SendError.RATE_LIMITED
        if code in _ERR_FORBIDDEN:
            return SendError.FORBIDDEN
        if code in _ERR_NOT_FOUND:
            return SendError.NOT_FOUND
        if code in _ERR_BAD_FORMAT:
            return SendError.BAD_FORMAT
    return classify_http(status, detail)


@register("qqbot")
class QQBotAdapter(Adapter):
    """QQ 官方机器人适配器：WebSocket 网关入站 + OpenAPI REST 出站。

    能力声明（**与实现严格一致**，不许谎报 —— 调用点据 ``capabilities()`` 决策）：
    只发纯文本、只做被动回复式发送、没有编辑、没有富媒体、没有 inline 按钮。
    """

    name = "qqbot"
    label = "QQ Bot"
    #: **自选的保守值**，不是官方数字（见 :data:`MESSAGE_LIMIT`）。
    max_message_length = MESSAGE_LIMIT
    supports_inbound = True             # WebSocket 网关事件流
    #: ⛔ **显式 False（已裁决，不再重开）** —— 判据 (a)/(b) 都成立（``group_openid`` 是
    #: 稳定会话 id），但 principal 与「**谁能驱动**」不是一回事：同一个群里的**任何**
    #: 成员都共享它。拿它当配对锚点，等于把整个群一起授权进白名单 —— 而那正是
    #: 配对回信里那句范围声明要防的后果。
    #:
    #: 复核已确认本判据成立。要改之前先回答：授权一个 ``group_openid`` 之后，
    #: 群里任何一个 @ 机器人的人是不是都因此获得了执行权限？
    pairing_supported = False
    supports_inline_buttons = False     # API 有 keyboard 字段，但本适配器不构造它
    supports_media = False              # 只发纯文本（msg_type=0）
    typed_command_prefix = "/"
    #: 真实凭据键就是这两个：``app_id`` + ``app_secret`` 换 access_token，出站（发消息）
    #: 与入站（Identify / Resume）都依赖它们，所以两者都进 ``outbound_tokens``。
    required_tokens = ("app_id", "app_secret")
    outbound_tokens = ("app_id", "app_secret")

    # -- 类级旋钮（测试可在实例上覆盖）----------------------------------
    min_interval = MIN_SEND_INTERVAL
    #: 读超时兜底；测试可以调小以验证"半开连接会被发现并重连"。
    ws_recv_timeout = WS_RECV_TIMEOUT

    def __init__(self, config: dict, hooks: Hooks) -> None:
        super().__init__(config, hooks)
        # 官方文档里这两个字段叫 ``appId`` / ``clientSecret``，配置文件里按仓库惯例用
        # 蛇形 ``app_id`` / ``app_secret``；几个常见别名一并认，省得用户自己踩。
        self.app_id: str = _first(self.config, "app_id", "appid", "appId")
        self.app_secret: str = _first(
            self.config, "app_secret", "appsecret", "appSecret",
            "client_secret", "clientSecret",
        )

        # --- 环境 / 端点 --------------------------------------------------
        #: 沙箱模式只影响**出站** REST 主机；网关地址一律由 ``GET /gateway`` 返回
        #: （官方没有"沙箱网关地址"这个说法，网关地址由平台按环境返回）。
        self.sandbox: bool = bool(self.config.get("sandbox"))
        configured_base = str(self.config.get("api_base") or "").strip().rstrip("/")
        if configured_base:
            self.api_base: str = configured_base
        else:
            self.api_base = SANDBOX_API_BASE if self.sandbox else API_BASE
        #: 允许配置覆盖网关地址（自建网关 / 测试注入）；留空则启动时问 REST 要。
        self.gateway_url: Optional[str] = (
            str(self.config.get("gateway_url") or "").strip() or None
        )
        self.intents: int = self._config_intents()
        self.shard: Tuple[int, int] = self._config_shard()

        # --- 传输层 / 网关状态 ---------------------------------------------
        self._transport: Optional[WebSocketTransport] = None
        self._ws_factory = None                # 测试注入点（默认走 ws.connect）
        self._session_id: Optional[str] = None
        self._last_seq: Optional[int] = None    # 最近一次下行序列号（心跳 + Resume 都用）
        self._my_user_id: Optional[str] = None  # READY.d.user.id（防回环判据之一）
        self._ack_received: bool = False        # 最近一次心跳是否已收到 op 11
        self._heartbeat_interval: float = 0.0   # **秒**（Hello 给的是毫秒，见 _on_hello）
        self._identify_token: str = ""          # "QQBot {access_token}"，绝不落日志
        self._identified: bool = False          # 本次连接是否已发过 Identify / Resume
        self._hello_seen: bool = False          # 本次连接是否收到过 Hello
        self._ready_seen: bool = False          # 本次连接是否收到过 READY / RESUMED
        self._pending: Optional[Tuple[str, dict]] = None  # (原始帧, 解析结果) 复用

        # --- access_token --------------------------------------------------
        self._token: str = ""
        self._token_expires_at: float = 0.0
        self._token_lock = threading.Lock()

        # --- 心跳看门狗 ---------------------------------------------------
        self._hb_thread: Optional[threading.Thread] = None
        self._hb_stop = threading.Event()

        # --- 被动回复上下文 -----------------------------------------------
        #: conversation_id → (msg_id, 失效时刻, 下一个 msg_seq)
        self._reply_ctx: Dict[str, Tuple[str, float, int]] = {}

        # --- 出站节流 -----------------------------------------------------
        self._throttle_lock = threading.Lock()
        self._last_send: Dict[str, float] = {}

        # --- 一次性告警 ---------------------------------------------------
        self._warned_edit = False
        self._warned_dm = False
        self._warned_loop_guard = False

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    def _config_intents(self) -> int:
        """读 ``intents`` 配置；非法值退回默认而不是静默发 0（0 = 一个事件都不收）。

        ⚠️ **名字列表那一支刻意留在这里**（"字符串集合 → bitmask"压不成 ``coerce_int``，
        且它有自己的容错语义：认不出的名字按 0 算、全都认不出才落到整数那条路）。
        整数那一支已迁到共享助手 :func:`~opencode_bridge.config_coerce.coerce_int`，
        下界**闭区间 1** —— 改前那行逐字是 ``value if value > 0``。

        ⇒ 迁移带来的**行为变化**（逐条有测试钉住改前/改后，
        ``tests/test_qqbot.py::TestIntentsConfigCoercion``）：

        1. ``intents=0`` / 负数：改前**静默**回落，改后**告警**回落（``0`` 同样值得
           点名：它是一个事件都不收）。
        2. ``intents=true``：改前 ``int(True) == 1`` **不抛异常** ⇒ 静默拿到 1，而 1
           不是本适配器用的任何一个 intent 位（:data:`INTENT_GROUP_AND_C2C` 是
           ``1 << 25``）⇒ 官方会因 intents 越权直接关连接，用户只看到"刚鉴权就断"。
           改后助手在**共享层**拒 bool。
        3. ``intents=33554433.0``（JSON 没有 int/float 之分）：改前 ``int()`` **截断**，
           改后告警回落。
        4. ``intents="   "``（纯空白）：改前告警（``int()`` 抛 ``ValueError``），改后
           按纪律 1 当"没配" ⇒ 静默用默认。
        """
        raw = self.config.get("intents")
        if isinstance(raw, (list, tuple)):          # 也接受名字列表（容错）
            mask = 0
            for item in raw:
                mask |= _INTENT_NAMES.get(str(item).strip().upper(), 0)
            if mask:
                return mask
        return coerce_int(
            self.config, "intents", DEFAULT_INTENTS,
            minimum=1, platform=self.name,
        )

    def _config_shard(self) -> Tuple[int, int]:
        """读 ``shard`` 配置（``[shard_id, num_shards]``）；非法值退回 ``(0, 1)``。

        三个元素各走各的判据（两个元素已迁到共享助手 ``coerce_int``）：

        1. **形状不对**（不是长度 2 的 ``list`` / ``tuple``）⇒ 静默回默认。⚠️ 这条
           **刻意保持静默**：那多半是"这个键压根没配"（纪律 1），不是配置错误。
        2. **元素不是整数 / 越界** ⇒ 助手告警并回落到哨兵（见 :data:`_UNUSABLE_SHARD_ID`
           与 :data:`_UNUSABLE_NUM_SHARDS`）⇒ 本方法看到哨兵就直接回默认。
        3. **交叉关系非法**（``shard_id >= num_shards``）⇒ 仍由本方法告警：这一条
           **单个元素都合法**（两个元素各自在区间内），只有"放在一起看"才非法，
           助手拿不到这个信息。

        ⚠️ ``coerce_int`` 的入参是「整份 config + 键名」（纪律 1 与纪律 2 的分界必须
        由**同一个**判据划，见 ``config_coerce`` 的模块 docstring），所以每个元素包成
        一份单键映射；键名写成 ``shard[0]`` / ``shard[1]`` 让告警指向用户写的那一项。

        ⇒ 迁移带来的**行为变化**（逐条有测试钉住改前/改后，
        ``tests/test_qqbot.py::TestShardConfigCoercion``）：

        1. ⚠️ **改前那条"静默回落且无告警"就是第 2 步的解析失败分支**
           （``except (TypeError, ValueError): return DEFAULT_SHARD``，**没有**日志）——
           复核确认：告警只挂在**交叉关系**那一条（``if num < 1 or not (0 <= …)``）上。
           ``shard=["x", 4]`` 改前静默回落，改后告警回落。
        2. ``shard=[1.0, 4]`` / ``[True, 4]``：改前**静默采纳**（``int()`` 截断 /
           ``int(True) == 1``），改后告警回落 ``(0, 1)``。
        3. ``shard=[0, 0]``：改前告警（``num < 1`` 那条），改后**也**告警，但告警出自
           助手（下界 ``minimum=1``）—— 条数不变，文案变。
        """
        raw = self.config.get("shard")
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            return DEFAULT_SHARD
        shard_id = coerce_int(
            {_SHARD_ID_KEY: raw[0]}, _SHARD_ID_KEY, _UNUSABLE_SHARD_ID,
            minimum=0, platform=self.name,
        )
        num_shards = coerce_int(
            {_SHARD_NUM_SHARDS_KEY: raw[1]}, _SHARD_NUM_SHARDS_KEY, _UNUSABLE_NUM_SHARDS,
            minimum=1, platform=self.name,
        )
        if shard_id == _UNUSABLE_SHARD_ID or num_shards == _UNUSABLE_NUM_SHARDS:
            return DEFAULT_SHARD       # 助手已经点名告警过了，不再说第二遍
        if not 0 <= shard_id < num_shards:
            logger.warning("qqbot: shard 配置非法 %r，改用 %s", raw, DEFAULT_SHARD)
            return DEFAULT_SHARD
        return (shard_id, num_shards)

    # ------------------------------------------------------------------
    # 会话标识（``platform:local_id``）
    # ------------------------------------------------------------------
    @staticmethod
    def conversation_id_for(scope: str, target: str) -> str:
        """``qqbot:<scope>:<target>``。

        ⚠️ local 段**含冒号**（``group:B2C3...``）。这与 ``identity`` 的设计一致：
        ``identity._split`` 按**第一个**冒号切分，所以 ``qqbot`` 是平台段、
        ``group:B2C3...`` 是完整的 local 段，**往返无损**（``tests/test_qqbot.py``
        用含冒号的 local 段锁住了这条）。
        """
        return identity.format_id("qqbot", f"{scope}:{target}")

    @staticmethod
    def split_conversation(conversation_id: object) -> Optional[Tuple[str, str]]:
        """``qqbot:<scope>:<target>`` → ``(scope, target)``；不认识则 ``None``。

        旧格式（``channel:xxx`` / ``chat:xxx``）会被 :func:`identity.local_of` 拒掉，
        不会静默当成 qqbot 会话。
        """
        local = identity.local_of(conversation_id)
        if not local:
            return None
        scope, sep, target = local.partition(":")
        if not sep or not target or scope not in _SCOPE_PATHS:
            return None
        return scope, target

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[dict] = None,
        *,
        token: str = "",
        timeout: Optional[float] = None,
    ) -> Tuple[int, dict]:
        """``{api_base}/{path}`` + ``Authorization: QQBot {token}``。**Never raises**。

        ``payload is None`` 时不发 body（GET）。**凭据只进 header** —— 绝不进 query
        string（会进日志 / ``Referer`` / 代理历史）。
        """
        url = f"{self.api_base}/{path.lstrip('/')}"
        body = (
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if payload is not None
            else None
        )
        headers = {
            "Accept": "application/json",
            "User-Agent": "opencode-bridge (qqbot, 1.0)",
        }
        if token:
            headers["Authorization"] = f"QQBot {token}"
        if body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(
                req, timeout=timeout if timeout is not None else SOCKET_TIMEOUT
            ) as resp:
                status = getattr(resp, "status", 200) or 200
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                raw = exc.read()
            except Exception:  # noqa: BLE001
                raw = b""
        except Exception as exc:  # noqa: BLE001 - 传输失败映射成状态码 0
            logger.warning("qqbot: transport error on %s: %s", path, exc)
            return 0, {"message": f"transport error: {exc}"}
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:  # noqa: BLE001
            return status, {"message": f"non-JSON response (HTTP {status})"}
        if not isinstance(data, dict):
            return status, {"message": "unexpected payload"}
        return status, data

    @staticmethod
    def error_code(data: Any) -> Optional[int]:
        """官方失败响应带 ``err_code``（OpenAPI）或 ``code``（凭证接口），取其一。"""
        if not isinstance(data, dict):
            return None
        for key in ("err_code", "code"):
            value = data.get(key)
            if isinstance(value, bool) or value is None:
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def error_detail(data: Any) -> str:
        if not isinstance(data, dict):
            return ""
        for key in ("message", "msg", "error"):
            value = data.get(key)
            if value:
                return str(value)
        return ""

    # ------------------------------------------------------------------
    # access_token（官方：POST /app/getAppAccessToken，expires_in 是字符串）
    # ------------------------------------------------------------------
    def _ensure_access_token(self) -> str:
        """取一个可用的 access_token（带缓存与提前刷新）。

        官方明确：``expires_in`` 是**字符串**；而且"业务错误通过响应体的 ``code``
        返回，即使调用失败，HTTP 返回码仍为 ``200``" —— 所以必须查 ``access_token``
        键存在与否，**不能只看 HTTP 码**。
        """
        with self._token_lock:
            now = time.monotonic()
            if self._token and self._token_expires_at - now > TOKEN_REFRESH_MARGIN:
                return self._token
            status, data = self._request(
                "POST",
                ACCESS_TOKEN_PATH,
                {"appId": self.app_id, "clientSecret": self.app_secret},
            )
            token = str(data.get("access_token") or "").strip()
            if status < 200 or status >= 300 or not token:
                code = self.error_code(data)
                logger.error(
                    "qqbot: 获取 access_token 失败 (HTTP %s, code=%s): %s",
                    status,
                    code,
                    self.error_detail(data) or "响应里没有 access_token",
                )
                raise RuntimeError(f"access_token 获取失败: HTTP {status} code={code}")
            try:
                ttl = float(data.get("expires_in") or 0)   # 官方示例给的是字符串 "7200"
            except (TypeError, ValueError):
                ttl = 0.0
            if ttl <= TOKEN_MIN_TTL:
                # 没有可信的 TTL：不缓存（每次重新换，官方说重复获取返回同一个值，
                # 不会多消耗配额），比按错误的 TTL 缓存到失效之后安全。
                ttl = 0.0
                self._token_expires_at = 0.0
            else:
                self._token_expires_at = now + ttl
            self._token = token
            logger.info(
                "qqbot: access_token 已更新 (app_id=%s, ttl=%.0fs)",
                _mask(self.app_id),
                ttl,
            )
            return token

    def _identify_payload(self, token: str) -> str:
        return f"QQBot {token}"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    @property
    def transport(self) -> Optional[WebSocketTransport]:
        """当前传输层（测试 / 诊断用）。"""
        return self._transport

    @property
    def running(self) -> bool:
        """消费线程是否活着（代理到传输层）。"""
        transport = self._transport
        return transport is not None and transport.running

    def _make_transport(self) -> WebSocketTransport:
        """构造传输层。

        * ``on_message`` 只处理**控制类** opcode（Hello / 心跳 / ACK / Reconnect /
          Invalid Session）—— 只有从这里能抛 :class:`ReconnectNow` 拿到"立即重连、
          不退避"的语义（服务端要求换连接不是故障）。
        * ``start(on_event)`` 只处理 **dispatch 事件**；基类保证这里的异常不许杀死
          消费循环（不变量 4），所以畸形消息不可能把网关线程带走。
        * ``reset_after=0`` = 连上即重置退避（不变量 10）。
        """
        return _QQBotTransport(
            self._open_gateway,
            on_message=self._on_gateway_frame,
            on_session_end=self._on_session_end,
            name="qqbot",
            min_backoff=RECONNECT_MIN,
            max_backoff=RECONNECT_MAX,
            reset_after=0.0,
        )

    def start(self) -> None:
        """启动入站。缺 ``app_id`` / ``app_secret`` 时只告警不起线程（不抛）。"""
        if not self.app_id:
            logger.warning("qqbot: app_id missing; adapter not started")
            return
        if not self.app_secret:
            logger.warning("qqbot: app_secret missing; adapter not started")
            return
        if self.sandbox and not str(self.config.get("api_base") or "").strip():
            logger.warning(
                "qqbot: sandbox=true 使用 %s —— **该沙箱域名未能从官方文档核实**"
                "（官方《API 调用指南》只给了正式环境 %s）。请用 api_base 显式指定。",
                SANDBOX_API_BASE,
                API_BASE,
            )
        self._stop_event.clear()
        transport = self._make_transport()
        self._transport = transport
        transport.start(self._on_event)
        logger.info(
            "qqbot: gateway loop started (app_id=%s, intents=%d, shard=%s, api=%s)",
            _mask(self.app_id),
            self.intents,
            self.shard,
            self.api_base,
        )

    def stop(self) -> None:
        """**先停心跳看门狗，再关传输层（它内部先关连接再 join），最后 ``super().stop()``**。

        顺序不能反：``recv()`` 最多会阻塞 :data:`WS_RECV_TIMEOUT`（120s）；先把连接
        关掉才能立刻唤醒它，否则每次 ``stop()`` 都白等 —— 这个坑本项目八个适配器都踩过
        （见 :meth:`WebSocketTransport._close_conn` 的注释）。幂等。
        """
        self._stop_watchdog()
        transport, self._transport = self._transport, None
        if transport is not None:
            transport.stop()
        super().stop()

    # ------------------------------------------------------------------
    # 入站：连接 / 控制类 opcode
    # ------------------------------------------------------------------
    def _make_ws(self, url: str):
        """建 WS 连接。**握手不带任何凭据**（官方：鉴权在 op 2 Identify 里做）。"""
        factory = self._ws_factory
        if factory is None:
            from ..ws import connect as factory  # 延迟导入：没开入站就不加载它
        return factory(url, timeout=float(self.ws_recv_timeout))

    def _resolve_gateway_url(self) -> str:
        """本次连接用的网关地址。

        **不硬编码主机名**（官方会换域名）：留空则 ``GET /gateway`` 要一次并缓存，
        允许 ``gateway_url`` 配置覆盖（自建网关 / 测试）。
        """
        if self.gateway_url:
            return self.gateway_url
        status, data = self._request(
            "GET", GATEWAY_PATH, None, token=self._ensure_access_token()
        )
        url = str(data.get("url") or "").strip()
        if status < 200 or status >= 300 or not url:
            raise RuntimeError(
                f"GET {GATEWAY_PATH} 失败: HTTP {status} "
                f"{self.error_detail(data) or '响应里没有 url'}"
            )
        self.gateway_url = url
        logger.info("qqbot: gateway 地址 %s", _redact(url))
        return url

    def _open_gateway(self):
        """``Transport._open``：建 WS 连接（**握手不带凭据**，鉴权在 op 2 Identify）。

        这里先换一次 access_token：``GET /gateway`` 这个 REST 调用本身就需要
        ``Authorization: QQBot {token}``（官方《API 调用指南》）。
        """
        self._ensure_access_token()
        self._hello_seen = False
        self._ready_seen = False
        self._identified = False
        self._ack_received = False
        return self._make_ws(self._resolve_gateway_url())

    def _on_session_end(self, conn: Any) -> None:
        """每条会话结束时停掉心跳看门狗 + **诊断这条会话为什么没活成**。

        官方对两种关键失败**不给任何错误负载**，只是"直接关闭连接"：

        * "如果在鉴权的时候传递了无权限的 ``intents``，``websocket`` 会报错，并直接关闭
          连接"；
        * 鉴权失败同样不返回可读信息。

        所以必须能从**状态**推出结论，否则用户只能对着"连接断开"发呆。连 Hello 都没收到
        就断 = 握手/网关地址/网络层问题；发了 Identify 却**没收到 READY/RESUMED** 就断
        = intents 越权或 token 无效（官方对这两种失败都不给错误负载）—— 给出可执行的
        排查建议。
        """
        self._stop_watchdog()
        if self._stop_event.is_set():
            return                       # 我们自己要停的，不算故障
        code = getattr(conn, "close_code", None)
        if not self._hello_seen:
            logger.warning(
                "qqbot: 会话在收到 Hello(op 10) 之前就结束了（close_code=%s）—— "
                "先查网关地址 / 网络 / TLS；还没到鉴权阶段。", code,
            )
        elif not self._ready_seen:
            logger.error(
                "qqbot: 会话发了 Identify/Resume 但**没收到 READY/RESUMED** 就被服务端"
                "关掉（close_code=%s）。官方明确「传递了无权限的 intents 会报错并直接"
                "关闭连接」，鉴权失败同样不返回可读错误。请依次检查：1) intents=%d 是否"
                "超出机器人已申请的权限（去开放平台核对）；2) app_id / app_secret 是否"
                "正确、机器人是否被封禁或已下线。", code, self.intents,
            )
        else:
            logger.info("qqbot: 会话结束（close_code=%s），将按退避重连", code)

    # ---- 帧处理（控制类）----------------------------------------------
    def _on_gateway_frame(self, conn: Any, frame: Any) -> None:
        """``WebSocketTransport.on_message``：控制类 opcode。

        ⚠️ **畸形帧绝不能外抛**：传输层会兜住并记 log，但那样连接仍在、状态可能半坏。
        这里自己吞掉并 ``debug`` 一行（帧内容不进日志，避免把凭据或超长 payload 抄进
        日志文件）。
        """
        raw = frame if isinstance(frame, str) else ""
        packet: dict = {}
        if raw:
            try:
                parsed = json.loads(raw)
            except Exception:  # noqa: BLE001
                logger.debug("qqbot: 非 JSON 帧，忽略（%d 字节）", len(raw))
                parsed = None
            if isinstance(parsed, dict):
                packet = parsed
                # 复用解析结果给紧随其后的 on_event（同一线程，省一次 json.loads）。
                self._pending = (raw, packet)
            elif parsed is not None:
                logger.debug("qqbot: 帧不是 JSON 对象，忽略")
        try:
            op = int(packet.get("op"))
        except (TypeError, ValueError):
            logger.debug("qqbot: 包里没有合法 op，忽略")
            return

        seq = packet.get("s")
        if isinstance(seq, int) and not isinstance(seq, bool):
            self._last_seq = seq
        data = packet.get("d")

        if op == OP_HELLO:
            self._on_hello(conn, data)
        elif op == OP_HEARTBEAT:
            # 官方表：Heartbeat「客户端**或服务端**发送心跳」⇒ 服务端要求时立刻回。
            self._send_op(conn, OP_HEARTBEAT, self._last_seq)
        elif op == OP_HEARTBEAT_ACK:
            self._ack_received = True
        elif op == OP_DISPATCH:
            pass                       # 事件由紧随其后的 on_event 处理（见它的 docstring）
        elif op == OP_RECONNECT:
            logger.info("qqbot: op 7 Reconnect —— 立即断开并重连")
            self._hard_close(conn)
            raise ReconnectNow("server asked to reconnect (op 7)")
        elif op == OP_INVALID_SESSION:
            if bool(data):
                logger.info("qqbot: op 9 Invalid Session(d=true) —— 可 Resume")
            else:
                logger.warning(
                    "qqbot: op 9 Invalid Session(d=false) —— session 失效，改走 Identify"
                )
                self._clear_session()
            self._hard_close(conn)
            raise ReconnectNow("invalid session (op 9)")
        else:
            logger.debug("qqbot: 忽略未处理的 op=%s", op)

    def _on_event(self, frame: Any) -> None:
        """``Transport.start(on_event)``：只处理 dispatch 事件。

        基类兜住这里的异常，所以一次畸形消息不可能把消费线程带走。
        """
        raw = frame if isinstance(frame, str) else ""
        pending, self._pending = self._pending, None
        if pending is not None and pending[0] == raw:
            packet = pending[1]
        else:
            try:
                parsed = json.loads(raw) if raw else None
            except Exception:  # noqa: BLE001
                logger.debug("qqbot: 非 JSON 帧，忽略")
                return
            if not isinstance(parsed, dict):
                return
            packet = parsed
        try:
            op = int(packet.get("op"))
        except (TypeError, ValueError):
            return
        if op != OP_DISPATCH:
            return                       # 控制类已在 on_message 里处理过
        try:
            self._handle_dispatch(str(packet.get("t") or ""), packet.get("d"))
        except Exception:  # noqa: BLE001 - 用户回调出错不该断连
            logger.exception("qqbot: 处理 dispatch 事件失败（已忽略）")

    def _on_hello(self, conn: Any, data: object) -> None:
        """op 10 Hello → 定心跳周期 → 立刻发一次心跳 → Identify / Resume → 起看门狗。

        ⚠️⚠️ **``heartbeat_interval`` 的单位是毫秒**（官方原文："单位毫秒(milliseconds)"，
        示例 45000）。这是本项目踩过一次的坑（Discord 那边先按秒用过，心跳快了 1000
        倍、瞬间触发限流），所以换算后**连同原始值一起写进日志**，便于事后核对。
        """
        self._hello_seen = True
        raw = data.get("heartbeat_interval") if isinstance(data, dict) else None
        interval = HEARTBEAT_DEFAULT
        if (
            isinstance(raw, (int, float))
            and not isinstance(raw, bool)
            and HEARTBEAT_MIN_MS <= float(raw) <= HEARTBEAT_MAX_MS
        ):
            interval = float(raw) / 1000.0   # ⚠️ 官方单位：毫秒
        else:
            logger.warning(
                "qqbot: Hello 的 heartbeat_interval=%r 不在 [%.0f, %.0f] ms 内，"
                "按官方示例值 %.1fs 处理",
                raw, HEARTBEAT_MIN_MS, HEARTBEAT_MAX_MS, HEARTBEAT_DEFAULT,
            )
        self._heartbeat_interval = interval
        self._ack_received = False
        logger.info(
            "qqbot: Hello —— heartbeat_interval=%r **毫秒** ⇒ 折算 %.3f 秒",
            raw, interval,
        )

        # 官方示例的第一发就是心跳，且 `d` 是最近收到的 `s`（没有则为 null）。
        self._send_op(conn, OP_HEARTBEAT, self._last_seq)
        # Identify 必须带 token。这里再取一次（而不是复用 ``_open_gateway`` 缓存的那个）：
        # 凭证 2 小时过期，长连接期间可能已经刷新过，这里取到的永远是当下有效的那个。
        try:
            self._identify_token = self._identify_payload(self._ensure_access_token())
        except Exception as exc:  # noqa: BLE001 - 取不到凭证就让这次会话失败并重试
            logger.error("qqbot: Identify 前取 access_token 失败: %s", exc)
            self._hard_close(conn, 4000, "no access token")
            return
        if self._should_resume():
            logger.info("qqbot: 发送 Resume（session=%s seq=%s）", self._session_id, self._last_seq)
            self._send_op(
                conn,
                OP_RESUME,
                {
                    "token": self._identify_token,
                    "session_id": self._session_id,
                    "seq": self._last_seq,
                },
            )
        else:
            logger.info("qqbot: 发送 Identify（intents=%d shard=%s）", self.intents, self.shard)
            self._send_op(
                conn,
                OP_IDENTIFY,
                {
                    "token": self._identify_token,
                    "intents": self.intents,
                    "shard": [self.shard[0], self.shard[1]],
                    "properties": {
                        "$os": "python",
                        "$browser": "opencode-bridge",
                        "$device": "opencode-bridge",
                    },
                },
            )
        self._identified = True
        self._start_watchdog(conn, interval)

    def _should_resume(self) -> bool:
        """有 session 且有序列号才 Resume，否则走 Identify。"""
        return bool(self._session_id and self._last_seq is not None)

    def _clear_session(self) -> None:
        """丢弃会话：下一次连接必须 Identify。"""
        self._session_id = None

    def _send_op(self, conn: Any, op: int, data: Any = None) -> bool:
        """发一个网关 payload。

        ``d`` 键**恒存在**（心跳没有序列号时是 ``null``）—— 官方示例的字面形状就是
        ``{"op": 1, "d": 251}``；省略 ``d`` 属于协议违规。
        """
        try:
            conn.send(json.dumps({"op": int(op), "d": data}))
            return True
        except Exception as exc:  # noqa: BLE001 - 发不出去就交给上层重连
            logger.debug("qqbot: 发送 op=%s 失败: %s", op, exc)
            return False

    @staticmethod
    def _hard_close(conn: Any, code: int = 4000, reason: str = "") -> None:
        """主动断开（用来触发"服务端要求重连"与"心跳停摆"）。

        code 用 **4000** 而不是 1000：4000 是应用私有区间，语义上表示"我们主动放弃这条
        连接"，也不会和"正常关闭"混淆。⚠️ 官方**没有**给 close code 语义表，所以这里
        **不**沿用 Discord 的"必须非 1000 才能保 session"经验 —— 那是查不到的。
        """
        try:
            conn.close(code, reason)
        except TypeError:      # 测试替身 / 其他实现的 close() 可能不收参数
            try:
                conn.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("qqbot: ws close 失败（忽略）: %s", exc)
        except Exception as exc:  # noqa: BLE001
            logger.debug("qqbot: ws close 失败（忽略）: %s", exc)

    # ---- 心跳看门狗（只发心跳 + 监测 ACK）-----------------------------
    def _start_watchdog(self, conn: Any, interval: float) -> None:
        self._stop_watchdog()
        stop = threading.Event()
        self._hb_stop = stop
        thread = threading.Thread(
            target=self._watchdog_loop,
            args=(conn, interval, stop),
            name="qqbot-heartbeat",
            daemon=True,
        )
        self._hb_thread = thread
        thread.start()

    def _watchdog_loop(self, conn: Any, interval: float, stop: threading.Event) -> None:
        """周期心跳 + ACK 监测。**周期恰好等于 ``interval``**（不是两倍）。

        结构上是"每个周期做两件事"：先检查**上一发**心跳的 ACK 到没到，没到就判死；
        再发新的一发。这样

        * 判死延迟 = 一个周期（官方语义："一个心跳周期内没收到 op 11 就判死"）；
        * 发送周期 = ``interval``。⚠️ 如果写成"发完再等一个周期"，实际周期会变成
          **两倍** —— 那是自己把心跳放慢一倍，超时断开反而会被拖长。

        **必须有它**（这是与 mattermost 的关键区别：那边靠 ``ws.py`` 自动回 pong 就够了，
        因为保活由服务端 ping 驱动；QQ 的心跳是**应用层**的 op 1/op 11，不发就断）：

        * 只发心跳、**收不到 op 11** → 一个周期后判定连接已死 → 主动 close →
          ``recv()`` 返回 ``None`` → 传输层重连。
        * TCP 半开（静默）：即使心跳发得出去（写缓冲还活着）也收不到 ACK，同一条路径
          能发现；另有 :data:`WS_RECV_TIMEOUT` 兜底。
        """
        outstanding = False           # 上一发心跳还在等 ACK？
        while not stop.is_set() and not self._stop_event.is_set():
            if stop.wait(interval):
                return
            if getattr(conn, "closed", False):
                return
            if outstanding and not self._ack_received:
                logger.warning(
                    "qqbot: %.3fs 内没收到 op 11 ACK，判定连接已死，断开重连", interval
                )
                self._hard_close(conn, 4000, "heartbeat ack timeout")
                return
            if self._stop_event.is_set():
                return
            outstanding = True
            self._ack_received = False      # 期待这一发对应的一个新 ACK
            self._send_op(conn, OP_HEARTBEAT, self._last_seq)

    def _stop_watchdog(self) -> None:
        self._hb_stop.set()
        thread = self._hb_thread
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=2.0)
        self._hb_thread = None

    # ------------------------------------------------------------------
    # 入站：事件 → Inbound
    # ------------------------------------------------------------------
    def _handle_dispatch(self, name: str, data: object) -> None:
        if name == "READY":
            self._on_ready(data)
            return
        if name == "RESUMED":
            self._ready_seen = True
            logger.info("qqbot: RESUMED —— 补发完成")
            return
        if name == "DIRECT_MESSAGE_CREATE":
            if not self._warned_dm:
                self._warned_dm = True
                logger.warning(
                    "qqbot: 收到 DIRECT_MESSAGE_CREATE（频道私信），本适配器 v1 不处理。"
                    "如需支持请去掉默认 intents 并实现 /dms/{guild_id}/messages 出站。"
                )
            return
        if name in _SCOPE_EVENTS.values() or name in ("GROUP_MESSAGE_CREATE", "MESSAGE_CREATE"):
            self._handle_message(name, data)
            return
        logger.debug("qqbot: 忽略 dispatch t=%s", name)

    def _on_ready(self, data: object) -> None:
        """READY → 记 session_id 与**自己的 user id**（防回环判据之一）。"""
        if not isinstance(data, dict):
            return
        self._ready_seen = True
        user = data.get("user")
        user = user if isinstance(user, dict) else {}
        self._my_user_id = str(user.get("id") or "").strip() or None
        self._session_id = str(data.get("session_id") or "").strip() or None
        logger.info(
            "qqbot: READY（user=%s session=%s）",
            self._my_user_id or "?", self._session_id or "?",
        )
        if self._my_user_id and not self._warned_loop_guard:
            self._warned_loop_guard = True
            logger.info(
                "qqbot: 防回环启用（平台签发字段：author.bot / author.id==%s）；"
                "不使用任何内容启发式", self._my_user_id,
            )

    def _scope_for(self, event: str, data: dict) -> Tuple[str, str]:
        """事件 → ``(scope, target)``；取不到 target 时 ``target`` 为空串。"""
        if event.startswith("GROUP_"):
            return SCOPE_GROUP, str(data.get("group_openid") or "")
        if event == "C2C_MESSAGE_CREATE":
            author = data.get("author")
            author = author if isinstance(author, dict) else {}
            target = str(
                author.get("user_openid") or author.get("id") or ""
            )
            return SCOPE_C2C, target
        return SCOPE_CHANNEL, str(data.get("channel_id") or "")

    def _drop_inbound(self, reason: str, cid: str, author: str) -> None:
        """记一行"为什么丢"。

        ⚠️ **本平台只有 ``author`` 需要处理**：``cid`` 进来时**已经**是
        ``qqbot:<scope>:<target>``（见 :meth:`conversation_id_for`），也就是
        :func:`~opencode_bridge.adapters._redactable_ids.redactable_id` 唯一认得的
        形式 —— 所以它原样打出去即可（:func:`redactable_id` 对已带前缀的入参
        **幂等**，拼两次不会变成 ``qqbot:qqbot:...``）。原先的 ``cid or "?"``
        换成了同一个函数：它对空串也返回 ``?``，于是"缺会话标识"那一支仍然
        **说得清是"没有"而不是"有但被洗掉了"**。

        ``author`` 是 ``member_openid`` / ``user_openid``，裸值明文；补前缀后被
        洗成 ``qqbot:conv#<摘要>``，**同一个人跨行仍可关联**。

        ⚠️ 本方法只改**记什么**：8 个调用点与每一个 ``return False`` 都原样未动。
        """
        logger.info(
            "qqbot: 丢弃消息（%s）conversation=%s author=%s",
            reason,
            redactable_id(self.name, cid),
            redactable_id(self.name, author),
        )

    def _handle_message(self, event: str, data: object) -> bool:
        """消息事件 → 过滤 → Inbound。返回是否真的放行了一条。

        过滤顺序刻意与 mattermost 一致：**先防回环、再过滤噪声、最后才是授权闸门**，
        且闸门必须在产生 ``Inbound`` **之前**（否则能用命令 / 审批字绕过）。
        """
        if not isinstance(data, dict):
            return False
        scope, target = self._scope_for(event, data)
        author = data.get("author")
        author = author if isinstance(author, dict) else {}
        author_id = str(
            author.get("member_openid") or author.get("user_openid") or author.get("id") or ""
        )
        cid = self.conversation_id_for(scope, target) if target else ""

        # 1) 防回环 —— **只用平台签发的字段**（不变量 14）。
        #    * ``author.bot``：官方 ``User`` 结构里明写"是否为机器人"，平台签发、
        #      发布者不可控。
        #    * ``author.id == READY.d.user.id``：频道场景 id 同源，可精确识别自己。
        #    官方**没有承诺**"机器人自己的消息不会推回来"（``GROUP_MESSAGE_CREATE``
        #    的定义是"群里的每一条消息"），所以不能只依赖事件语义。
        if author.get("bot") is True:
            self._drop_inbound("author.bot=true（平台签发：发送者是机器人）", cid, author_id)
            return False
        if self._my_user_id and author_id and author_id == self._my_user_id:
            self._drop_inbound("发送者是自己（author.id == READY.user.id）", cid, author_id)
            return False
        # 2) 只收纯文本；卡片 / 引用消息的 content 是空的或占位空格。
        message_type = data.get("message_type")
        if isinstance(message_type, int) and not isinstance(message_type, bool):
            if message_type != MSG_TYPE_TEXT:
                self._drop_inbound(
                    f"非纯文本 message_type={message_type}", cid, author_id
                )
                return False
        elif message_type is not None:
            self._drop_inbound(f"message_type 非法 {message_type!r}", cid, author_id)
            return False
        content = data.get("content")
        content = content if isinstance(content, str) else ""
        if not content.strip():
            self._drop_inbound("空正文", cid, author_id)
            return False
        # 3) 拼不出会话标识就没法路由
        if not cid:
            self._drop_inbound("缺会话标识（group_openid / user_openid / channel_id）", "", author_id)
            return False
        # 4) 授权闸门：principal = **会话目标**（group_openid / user_openid /
        #    channel_id），与 ``allowed_chat_ids`` 的语义一致。
        #    ⚠️ 本平台 :attr:`pairing_supported` 用基类默认 False —— principal 虽
        #    是稳定会话 id，但 principal 与"谁能驱动"并不是一回事（同群任何成员
        #    都共享它），拿它当配对锚点会把整个群一起授权。失败关闭。
        if not self.admits(target) and not self.answer_pairing_request(
            target, cid, content
        ):
            self._drop_inbound("未在白名单", cid, author_id)
            return False

        message_id = str(data.get("id") or "").strip()
        if message_id:
            self._remember_inbound(cid, scope, message_id)
        try:
            self.hooks.on_inbound(
                Inbound(
                    conversation_id=cid,
                    text=content,
                    kind="text",
                    user_id=author_id or None,
                    message_id=message_id or None,
                    platform=self.name,
                    raw=data,
                )
            )
        except Exception as exc:  # noqa: BLE001 - 上层炸了也不能让网关线程退出
            logger.exception("qqbot: on_inbound 失败: %s", exc)
            return False
        return True

    def _remember_inbound(self, cid: str, scope: str, message_id: str) -> None:
        """记住刚收到的 ``msg_id``，出站时用它做**被动回复**。

        官方：被动消息（带 ``msg_id``）在群聊 5 分钟 / 5 次、单聊 60 分钟 / 4 次内有效，
        且**不受**"主动消息频控"约束 —— 群里主动消息没申请权限时会直接
        ``40034105 主动消息发送失败，无权限``，所以这条路径是能不能回上话的关键。
        """
        window = PASSIVE_WINDOW.get(scope)
        if not window:
            return
        self._reply_ctx[cid] = (message_id, time.monotonic() + window, 1)

    # ------------------------------------------------------------------
    # 出站（REST）
    # ------------------------------------------------------------------
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

    def _passive_reply_fields(self, scope: str, cid: str) -> dict:
        """被动回复字段（``msg_id`` + ``msg_seq``）；不适用时返回 ``{}``。

        ``msg_seq`` 随每次**尝试**递增（官方："相同的 msg_id + msg_seq 重复发送会失败"，
        错误码 40054005），所以失败也不复用同一个序号。
        """
        window = PASSIVE_WINDOW.get(scope)
        cap = PASSIVE_MAX_REPLIES.get(scope)
        if not window or not cap:
            return {}
        entry = self._reply_ctx.get(cid)
        if not entry:
            return {}
        msg_id, expires_at, seq = entry
        now = time.monotonic()
        if now >= expires_at or seq > cap:
            self._reply_ctx.pop(cid, None)
            return {}
        self._reply_ctx[cid] = (msg_id, expires_at, seq + 1)
        return {"msg_id": msg_id, "msg_seq": seq}

    def _send_path(self, scope: str, target: str) -> str:
        return _SCOPE_PATHS[scope].format(target=urllib.parse.quote(target, safe=""))

    def send(self, out: Outbound) -> MsgHandle | None:
        """按 scope 走对应的官方发消息接口，超长自动分片。

        判定用"2xx **且**响应里有 ``id``"：官方成功响应形如
        ``{"id": "ROBOT1.0_xxx", "timestamp": ...}``，而失败时 HTTP 也可能是 200
        带 ``err_code``（凭证接口官方就明说了），所以两层都要查。
        """
        parsed = self.split_conversation(out.conversation_id)
        if not parsed:
            logger.warning("qqbot: bad conversation_id %r", out.conversation_id)
            self._note_send_failure(SendError.BAD_FORMAT, "bad conversation_id")
            return None
        scope, target = parsed
        if not out.text:
            logger.warning("qqbot: refusing to send empty text")
            self._note_send_failure(SendError.BAD_FORMAT, "empty text")
            return None
        if not self.app_id or not self.app_secret:
            logger.warning("qqbot: app_id/app_secret missing; send refused")
            self._note_send_failure(SendError.BAD_FORMAT, "app_id/app_secret missing")
            return None
        try:
            token = self._ensure_access_token()
        except Exception as exc:  # noqa: BLE001
            self._note_send_failure(SendError.TRANSIENT, f"access_token: {exc}")
            return None

        limit = self.effective_max_length
        chunks = split_text(out.text, limit, prefix_fmt="")
        if len(chunks) > 1:
            logger.info(
                "qqbot: splitting outbound message into %d chunks (limit=%d，"
                "⚠️ 上限是自选保守值，官方未公布)", len(chunks), limit,
            )
        handle: MsgHandle | None = None
        for chunk in chunks:
            self._throttle(out.conversation_id)
            payload: dict = {"content": chunk}
            if scope == SCOPE_CHANNEL:
                pass          # 频道接口的正文键就是 content，无 msg_type
            else:
                payload["msg_type"] = 0
                payload.update(self._passive_reply_fields(scope, out.conversation_id))
            status, data = self._request(
                "POST", self._send_path(scope, target), payload, token=token
            )
            message_id = str(data.get("id") or "").strip()
            code = self.error_code(data)
            if status < 200 or status >= 300 or code is not None or not message_id:
                detail = self.error_detail(data)
                logger.warning(
                    "qqbot: send failed (HTTP %s, code=%s): %s", status, code,
                    detail or "响应里没有 id",
                )
                self._note_send_failure(
                    _classify_qq_error(status, code, detail), detail or f"HTTP {status}"
                )
                return handle if handle is not None else None
            handle = MsgHandle(
                conversation_id=out.conversation_id,
                message_id=message_id,
                platform=self.name,
            )
        return handle

    def edit(self, handle: MsgHandle, out: Outbound) -> bool:
        """**恒返回 ``False``** —— 本平台没有可用的"编辑正文"接口。

        依据（详见模块 docstring 与下面这条一次性告警）：

        * 群聊 / 单聊场景官方**只有撤回**（``DELETE /v2/{groups,users}/{openid}/messages/{id}``），
          消息收发概述页通篇**没有**编辑端点。
        * 频道场景错误码表里有 ``3000000~3999999 编辑消息错误`` /
          ``50049 只能修改含有 keyboard 元素的消息`` /
          ``50050 修改消息时，keyboard 元素不能为空`` —— 说明那个 PATCH 改的是
          **keyboard**（按钮）而不是正文，而且我**没能找到它的接口文档页**。
        * 因此返回 ``False``：core 会退化成"再发一条新消息"（架构文档不变量 4 要求
          诚实降级，不许假装成功）。
        """
        if not self._warned_edit:
            self._warned_edit = True
            logger.info(
                "qqbot: edit() 恒返回 False —— QQ 开放平台的群/单聊场景只有撤回没有编辑，"
                "频道那个 PATCH 改的是 keyboard 不是正文（错误码 50049/50050）。"
                "core 会退化成发新消息。"
            )
        return False

    def answer(self, query_id: str, text: str = "") -> None:
        """本适配器不构造 keyboard 事件，没有需要应答的 callback query。"""
        return None


#: 名字 → intent 位（``intents`` 配置允许写名字列表）。
_INTENT_NAMES: Dict[str, int] = {
    "GUILDS": INTENT_GUILDS,
    "GUILD_MEMBERS": INTENT_GUILD_MEMBERS,
    "GUILD_MESSAGES": INTENT_GUILD_MESSAGES,
    "DIRECT_MESSAGE": INTENT_DIRECT_MESSAGE,
    "GROUP_AND_C2C_EVENT": INTENT_GROUP_AND_C2C,
    "GROUP_AND_C2C": INTENT_GROUP_AND_C2C,
    "INTERACTION": INTENT_INTERACTION,
    "PUBLIC_GUILD_MESSAGES": INTENT_PUBLIC_GUILD_MESSAGES,
}


def _redact(url: str) -> str:
    """日志里不打印 query（防御"有人把凭据配到 URL 里"这种情况）。"""
    parts = urllib.parse.urlsplit(url)
    base = f"{parts.scheme}://{parts.netloc}{parts.path}"
    return f"{base}?…" if parts.query else base
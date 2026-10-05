"""B2 —— Home Assistant 平台适配器（``/api/websocket`` 事件总线）。标准库 only。

传输层用 :class:`~opencode_bridge.transport.WebSocketTransport` + 自研
:mod:`opencode_bridge.ws`（零第三方依赖）。入站 = WS 事件总线的订阅事件；出站 =
**同一条 WS 连接**上的 ``call_service`` 命令（默认 ``persistent_notification.create``，
即 HA 前端铃铛里的一条持久通知）。

已核实的协议事实（出处见模块末尾 ``docs`` 段）
----------------------------------------------------

1. **端点是 ``/api/websocket``**，握手**不带任何鉴权信息**（无 header、无 query）。
   官方 const 源码：``URL: Final = "/api/websocket"``。鉴权是**连上之后的带内消息**。
2. **鉴权是服务端先说、客户端后答的两步**（官方文档 "Authentication phase"）::

       服务端 → {"type": "auth_required", "ha_version": "..."}
       客户端 → {"type": "auth", "access_token": "<长期访问令牌>"}
       服务端 → {"type": "auth_ok", "ha_version": "..."}        # 成功
       服务端 → {"type": "auth_invalid", "message": "..."}     # 失败，随后**断开连接**

   服务端只在 ``AUTH_MESSAGE_TIMEOUT = 10`` 秒内等这条 ``auth``（``websocket_api/http.py``），
   超时就断开 —— 所以认证必须**连上立刻**做（本适配器在 ``_on_open`` 里同步做完）。
   ``auth`` 消息**不能带 ``id``**（``AUTH_MESSAGE_SCHEMA`` 只认 ``type`` + 二选一的
   ``api_password`` / ``access_token``，多余键会被 voluptuous 判非法）。
3. **``subscribe_events`` 的参数形状**是 ``{"id": N, "type": "subscribe_events",
   "event_type": "<类型>"}``，其中 ``event_type`` **可选**，缺省即 ``MATCH_ALL``（``*``，
   订阅全部）。官方原文："If you want to listen to multiple event types, you will
   have to send multiple ``subscribe_events`` commands."
4. ⚠️ **通配 ``*`` 需要管理员**。``handle_subscribe_events`` 里：
   ``if event_type not in SUBSCRIBE_ALLOWLIST and not connection.user.is_admin: raise Unauthorized``
   —— ``*`` 不在 ``SUBSCRIBE_ALLOWLIST`` 里，所以**只有管理员用户能订阅全部事件**；
   ``state_changed`` 在白名单里，任何用户都能订。本适配器因此默认只订
   ``state_changed``，并在 ``unauthorized`` 时明确报出"要管理员"。
5. **事件消息形状**（``messages.event_message``）::

       {"id": <订阅用的 id>, "type": "event",
        "event": {"event_type": "...", "data": {...},
                  "origin": "LOCAL", "time_fired": "2016-11-26T01:37:24+00:00",
                  "context": {"id": "...", "parent_id": null, "user_id": "..."}}}

   ``state_changed`` 的 ``data`` 是 ``{"entity_id", "old_state", "new_state"}``，
   状态对象里有 ``state`` / ``attributes.friendly_name`` / ``context.user_id``。
6. ⚠️ **保活是"客户端发 ping / 服务端回 pong"**（**不是** Mattermost 那种"服务端 ping"）::

       客户端 → {"id": 19, "type": "ping"}
       服务端 → {"id": 19, "type": "pong"}     # commands.py::handle_ping

   官方原文："The API supports **receiving a ping from the client** and returning a pong."
   —— 与 Mattermost（RFC 6455 ping 控制帧由服务端驱动、靠 ``ws.py`` 自动回 pong 就够）
   **方向相反**，与 Twitch 同类。所以本适配器有应用层 ping 看门狗
   （见 :meth:`HomeAssistantAdapter._watchdog_loop`）。
   ⚠️ **但还有第二层**：``websocket_api/http.py`` 里服务端建的是
   ``web.WebSocketResponse(heartbeat=55)``，aiohttp 会**每 55 秒发一个 RFC 6455 ping
   控制帧**并在下一轮没等到 pong 就断连（``_pong_not_received``）。这一层由 ``ws.py``
   的 ``_handle_control_frame`` **自动回 pong**，不需要本适配器写任何代码。
   结论：**传输层保活由服务端驱动（零代码），应用层保活必须客户端主动发。** 两层都要。
7. **消息长度上限：官方没有公布任何出站/入站消息长度限制**（查过官方 WebSocket API
   文档与 ``websocket_api`` 全部模块，未见任何长度常量或校验）。唯一存在的上限是
   **aiohttp 的默认 ``max_msg_size = 4 * 1024 * 1024``（4 MiB）**，而 HA 建
   ``WebSocketResponse`` 时**没有覆盖**这个默认值 ⇒ 超过 4 MiB 的客户端消息会被断连。
   因此 :data:`MESSAGE_LIMIT` 是**自选的保守值**，不是官方数字；真超限时不会静默
   截断，只会按 ``persistent_notification`` 的 schema 正常发出去（该组件对
   ``message`` 只做 ``cv.string``，无长度校验 —— 已核实）。
8. **错误码**：命令失败回 ``{"id", "type": "result", "success": false,
   "error": {"code", "message"}}``，``code`` 取自 ``websocket_api/const.py``
   （``unauthorized`` / ``not_found`` / ``invalid_format`` / ``id_reuse`` /
   ``unknown_command`` / ``home_assistant_error`` / ``service_validation_error`` /
   ``timeout`` / ``template_error`` / ``not_supported`` / ``not_allowed`` /
   ``unknown_error``）。**鉴权失败不走这套** —— 它走 ``auth_invalid`` + 断连。
9. **命令 id 必须严格递增**（``connection.py``：``if cur_id <= self.last_id:  ERR_ID_REUSE``），
   所以本适配器用一把锁保护的自增计数器，**每条连接**从 1 重新开始。

这个平台到底算不算"对话"（``tasks.md`` 里那句"它是设备事件管道不是 IM"）
------------------------------------------------------------------------

**判断：默认不成立，且必须显式化。** HA 推的是**设备状态变更**（"有人进了客厅"、
"灯开了"），不是"某人给你发了一条消息"。把它当对话喂给 agent，语义上只在**极窄**的
条件下成立：事件必须能追溯到**某个真人用户的操作**，而且必须能说清"是谁、在哪、干了
什么"。一个由定时器 / 脚本 / 传感器自己触发的事件（``context.user_id`` 为 ``null``）
根本不是"有人在跟你说话"，拿它去起一段对话只会污染 agent 的上下文。

所以这个约束被**显式化**成三条**默认保守**的规则（都是配置项，默认值即保守值）：

1. **默认一个事件都不收**：必须显式给 ``entities``（实体白名单）或 ``domains``
   （域白名单），或显式 ``accept_all: true``。空配置 = 订阅照做、事件全丢，
   并在启动时打一条一次性告警说明"没配过滤器"。
2. **默认要求可归因到真人**：``require_user_context`` 默认 ``True`` —— 事件的
   ``context.user_id`` 为空就丢。这条正是"设备事件 ≠ 对话"的判据：只有当某次操作
   能追到某个 HA 用户时，才把它当作"这个人做了一件事"。
3. **防回环用自己的动作记录**：出站 ``call_service`` 会改状态 → 又变成
   ``state_changed`` 事件 → 再喂 agent → 再调服务，无限循环。HA 的
   ``ActiveConnection.context()`` **不接受客户端自带的 context id**
   （源码固定返回 ``Context(user_id=self.user.id)``），所以没法在事件里打平台签发的
   标记；只能用**我们自己刚调过哪些实体**这个动作记录做窗口内抑制
   （:attr:`echo_suppress_seconds`，默认 10 秒）。默认出站服务
   ``persistent_notification.create`` 本身不改任何实体状态，天然不成环。

⚠️ **没有为了"像 IM"而发明平台没有的能力**：没有编辑、没有按钮、没有媒体、
没有群聊/私聊、没有消息已读。HA 的 WS API 里根本不存在这些概念。

⚠️ **连接方向与凭据**：这是**出站**连接（我们主动连 HA），**不是**我们开监听端口，
所以"bind 127.0.0.1"那套约束在这里不适用 —— HA 通常就跑在局域网另一台机器上。
但代价是：**凭据会被发到配置里写的那个地址**。因此本文件：

* 令牌**只**出现在 ``auth`` 消息体里，**不进 URL、不进 query、不进握手 header**
  （官方也没要求，WS 端点 ``requires_auth = False``，鉴权纯带内）；
* 任何日志都不打印令牌（连接失败、订阅失败、鉴权失败的文案都不带它），
  URL 日志走 :func:`_redact`（去掉 query）。

出站能力说明（为什么是 ``call_service``）
------------------------------------------

HA 的 WS API **没有"发消息"这种原语**。唯一能把内容送到人眼前的官方途径是调用一个
service，所以本适配器出站 = ``{"id": N, "type": "call_service", "domain", "service",
"service_data", "target"?}``，默认 ``persistent_notification.create``
（``message`` + ``title``，该组件源码核实：只要求 ``message`` 是字符串，无长度校验）。
想要别的效果就配 ``service_domain`` / ``service_name``（例如 ``notify`` /
``tts``）—— 但请读上面那段防回环的说明：改状态的服务会成环。
这条出站需要**已建立的 WS 连接**，没连上就返回 ``SendError.TRANSIENT``（不假装成功）。

``edit()`` 为什么恒为 ``False``
------------------------------

已核实：HA 的 WebSocket 命令表里**没有任何"编辑一条已有消息/通知"的命令**；
``persistent_notification`` 组件只注册了 ``create`` / ``dismiss`` / ``dismiss_all``
三个 service —— ``dismiss`` 是**删掉**，不是改内容。所以 :meth:`edit` 诚实返回
``False``，core 会退化成发新消息（架构文档不变量 4）。

``docs``（本文件所有断言的出处）
--------------------------------

* WebSocket API 总览（端点、鉴权流程、``subscribe_events``、事件形状、ping/pong）
  <https://developers.home-assistant/docs/websocket_api/>、
  <https://developers.home-assistant.io/docs/api/websocket/>
* 鉴权 API（长期访问令牌怎么拿、401 的含义）
  <https://developers.home-assistant.io/docs/auth_api/>
* ``websocket_api/const.py``（``URL = "/api/websocket"``、错误码全表）
  <https://github.com/home-assistant/core/blob/dev/homeassistant/components/websocket_api/const.py>
* ``websocket_api/auth.py``（``auth_required`` / ``auth`` / ``auth_ok`` /
  ``auth_invalid`` 与 ``AUTH_MESSAGE_SCHEMA``）
  <https://github.com/home-assistant/core/blob/dev/homeassistant/components/websocket_api/auth.py>
* ``websocket_api/commands.py``（``subscribe_events`` / ``SUBSCRIBE_ALLOWLIST`` 权限闸门 /
  ``handle_ping`` → ``pong_message`` / ``call_service`` 的 schema）
  <https://github.com/home-assistant/core/blob/dev/homeassistant/components/websocket_api/commands.py>
* ``websocket_api/http.py``（``AUTH_MESSAGE_TIMEOUT = 10``、
  ``web.WebSocketResponse(heartbeat=55)``、鉴权阶段流程）
  <https://github.com/home-assistant/core/blob/dev/homeassistant/components/websocket_api/http.py>
* ``websocket_api/connection.py``（``id`` 必须递增、``context()`` 固定用连接用户）
  <https://github.com/home-assistant/core/blob/dev/homeassistant/components/websocket_api/connection.py>
* ``websocket_api/messages.py``（``result_message`` / ``event_message`` 的字段形状）
  <https://github.com/home-assistant/core/blob/dev/homeassistant/components/websocket_api/messages.py>
* ``auth/permissions/events.py``（``SUBSCRIBE_ALLOWLIST`` 全表）
  <https://github.com/home-assistant/core/blob/dev/homeassistant/auth/permissions/events.py>
* ``components/persistent_notification/__init__.py``（``create`` 的 schema：``message``
  无长度校验；只有 create/dismiss/dismiss_all，没有编辑）
  <https://github.com/home-assistant/core/blob/dev/homeassistant/components/persistent_notification/__init__.py>
* aiohttp ``web_ws.py``（``max_msg_size`` 默认 4 MiB；``heartbeat`` 用 RFC 6455 ping
  驱动、``_pong_not_received`` 断连）
  <https://github.com/aio-libs/aiohttp/blob/master/aiohttp/web_ws.py>
* 第三方参考实现（Hermes 的 HA 平台，``tasks.md`` 提到的那份；本仓库的
  ``url`` / ``token`` 配置键名与默认地址沿用它的惯例）
  <https://github.com/NousResearch/hermes-agent/blob/main/plugins/platforms/homeassistant/adapter.py>

查不到 / 属于推断的部分（**不要**当官方事实引用）
--------------------------------------------------

* ⚠️ **默认地址 ``http://homeassistant.local:8123`` 不是官方文档里的规定**，是 mDNS
  主机名惯例（与 Hermes 的默认值一致）。它只作为"没配 ``url`` 时的兜底"，且必须
  显式告警。
* ⚠️ **消息长度上限 4096 是自选的**（官方没公布任何数字，见第 7 条）。真实上限只
  知道"不超过 aiohttp 的 4 MiB"。
* ⚠️ **"事件数据里的 ``context.user_id`` 能可靠归因到真人"是推断**：官方文档只描述
  了字段含义（``context`` 记录"是什么触发了这个事件"），没有承诺它在所有场景下都非空
  （定时器 / 脚本 / 集成自己触发的事件实测就是 ``null``）。这正是默认
  ``require_user_context=True`` 的理由 —— 宁可漏，不要把机器事件当人话。
* ⚠️ **aiohttp 每 55 秒发一次 RFC 6455 ping** 这条是从 ``heartbeat=55`` 参数 +
  aiohttp ``web_ws.py`` 的 ``_pong_not_received`` 回调推出来的，HA 自己没写文档说明
  它的保活节奏。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Set, Tuple

from .. import identity
from ..hooks import Hooks, Inbound, MsgHandle, Outbound, SendError
from ..split import split_text
from ..transport import WebSocketTransport
from .base import Adapter, register

logger = logging.getLogger("opencode_bridge.adapters.homeassistant")

__all__ = ["HomeAssistantAdapter", "MESSAGE_LIMIT", "WS_PATH", "EVENT_STATE_CHANGED"]


# ----------------------------------------------------------------------
# 常量（全部来自官方文档 / 源码；推断项见各自注释）
# ----------------------------------------------------------------------
#: 官方 ``websocket_api/const.py`` 的 ``URL``。
WS_PATH = "/api/websocket"

#: ⚠️ **不是官方规定**：mDNS 主机名惯例（同 Hermes 的默认值）。仅作兜底，且会告警。
DEFAULT_BASE_URL = "http://homeassistant.local:8123"

#: 官方常量 ``homeassistant.const.EVENT_STATE_CHANGED``。它在 ``SUBSCRIBE_ALLOWLIST``
#: 里 ⇒ 非管理员也能订阅，所以拿它当默认订阅类型。
EVENT_STATE_CHANGED = "state_changed"
#: HA 的通配订阅值（``MATCH_ALL``）。⚠️ **需要管理员**（见模块 docstring 第 4 条）。
MATCH_ALL = "*"
#: 默认订阅的事件类型（保守：只订白名单里的那一个）。
DEFAULT_EVENT_TYPES: Tuple[str, ...] = (EVENT_STATE_CHANGED,)

#: ⚠️ **自选的保守上限，不是官方数字**（官方没公布任何长度限制，见模块 docstring 第 7 条）。
#: 唯一已知的硬上限是 aiohttp 的 ``max_msg_size`` 默认 4 MiB。
MESSAGE_LIMIT = 4096

#: 服务端只在 ``AUTH_MESSAGE_TIMEOUT = 10`` 秒内等 ``auth`` 消息（官方
#: ``websocket_api/http.py``）。认证必须连上立刻做，所以整段握手都在
#: :meth:`HomeAssistantAdapter._handshake` 里同步完成 —— 我们**不**自己实现这个
#: 计时器（:mod:`opencode_bridge.ws` 没有"带超时的中断读"这个能力），服务端会替我们计时。
#: 同步等一条命令的 ``result`` 的上限（``call_service`` 是本地动作，10 秒足够）。
COMMAND_TIMEOUT = 10.0
#: ``ws.py`` 的 socket 读超时。⚠️ **不是**保活机制：控制帧在 ``recv()`` 内部就地处理，
#: 不会让它返回。健康判定靠应用层 ping 看门狗；这个值只兜 TCP 半开（静默）的连接。
WS_RECV_TIMEOUT = 90.0

#: 应用层 ping 周期。⚠️ HA 的 ping/pong 是**客户端发起**（见模块 docstring 第 6 条），
#: 不主动发就拿不到"服务端事件循环还活着"的证据。30 秒明显短于服务端 55 秒的
#: RFC 6455 心跳周期，两层不会互相盖住。
PING_INTERVAL = 30.0
#: 多久没等到 pong 就判死连接（一个周期）。
PING_TIMEOUT = PING_INTERVAL * 2.0

RECONNECT_MIN = 1.0
RECONNECT_MAX = 60.0

#: 出站默认走 ``persistent_notification.create``（HA 前端的持久通知）。
DEFAULT_SERVICE_DOMAIN = "persistent_notification"
DEFAULT_SERVICE_NAME = "create"
DEFAULT_NOTIFICATION_TITLE = "opencode-bridge"

#: 出站调了服务之后，多久之内忽略**同一批实体**上的 ``state_changed``（防自问自答回环）。
#: HA 的 ``ActiveConnection.context()`` 不接受客户端自带的 context id（见模块 docstring），
#: 只能用我们自己的动作记录做窗口抑制。
ECHO_SUPPRESS_SECONDS = 10.0

#: HA 的命令错误码 → 平台中立的发送失败分类。**全集取自官方
#: ``websocket_api/const.py``**（已逐个抄在下面，没有臆造成员）：
#: ``id_reuse`` / ``invalid_format`` / ``not_allowed`` / ``not_found`` /
#: ``not_supported`` / ``home_assistant_error`` / ``service_validation_error`` /
#: ``unknown_command`` / ``unknown_error`` / ``unauthorized`` / ``timeout`` /
#: ``template_error``。
#: ⚠️ 官方**没有** ``too_long``，也**没有**任何限流码，所以那两个集合**故意留空**
#: （而不是塞一个看起来像的字符串）—— 将来官方真加了码，只改这一处即可。
_ERR_FORBIDDEN = frozenset({"unauthorized", "not_allowed"})
_ERR_NOT_FOUND = frozenset({"not_found"})
_ERR_BAD_FORMAT = frozenset({"invalid_format", "unknown_command", "not_supported"})
_ERR_TRANSIENT = frozenset({"timeout", "home_assistant_error", "unknown_error",
                            "service_validation_error", "template_error"})
_ERR_TOO_LONG: frozenset = frozenset()
_ERR_RATE_LIMITED: frozenset = frozenset()

_ERR_UNAUTHORIZED = "unauthorized"


def _classify_ha_error(code: object, detail: str = "") -> SendError:
    """HA 的 ``error.code`` → :class:`SendError`（T1.3 可观测失败，绝不静默吞）。"""
    key = str(code or "").strip().lower()
    if key in _ERR_FORBIDDEN:
        return SendError.FORBIDDEN
    if key in _ERR_NOT_FOUND:
        return SendError.NOT_FOUND
    if key in _ERR_BAD_FORMAT:
        return SendError.BAD_FORMAT
    if key in _ERR_TOO_LONG:
        return SendError.TOO_LONG
    if key in _ERR_RATE_LIMITED:
        return SendError.RATE_LIMITED
    if key in _ERR_TRANSIENT:
        return SendError.TRANSIENT
    return SendError.UNKNOWN


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


def _string_set(value: object) -> Set[str]:
    """把配置里的字符串集合统一成 ``set[str]``（接受 list / tuple / set / 单值）。"""
    if value in (None, ""):
        return set()
    if isinstance(value, (list, tuple, set)):
        items: List[object] = list(value)
    else:
        items = [value]
    return {str(x).strip() for x in items if str(x).strip()}


def _truthy(value: object, default: bool = True) -> bool:
    """宽松读布尔；没配或非法时返回 ``default``（**不**静默当成 False）。"""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    low = str(value).strip().lower()
    if low in ("1", "true", "yes", "on"):
        return True
    if low in ("0", "false", "no", "off"):
        return False
    logger.warning("homeassistant: 布尔配置非法 %r，按 %s 处理", value, default)
    return default


def _redact(url: str) -> str:
    """日志里不打印 query（防御"有人把令牌配到 URL 里"这种情况）。"""
    parts = urllib.parse.urlsplit(url)
    base = f"{parts.scheme}://{parts.netloc}{parts.path}"
    return f"{base}?…" if parts.query else base


class _HomeAssistantTransport(WebSocketTransport):
    """HA 用的传输层：把**会话开始 / 结束**两个时机转交给适配器。

    为什么必须是子类：``_on_open`` / ``_on_close`` 是
    :class:`~opencode_bridge.transport.Transport` 在**自己身上**调的钩子（那个 ``self``
    是传输层、不是适配器），而基类不会反过来去调适配器的方法（分层规矩：传输层不认识
    消息平台）。本平台必须借这两个时机做事：

    * ``on_session_open`` —— 同步完成 ``auth`` + ``subscribe_events``。HA 的鉴权是
      **带内**的（``auth`` 是一条协议消息），且服务端只给 ``AUTH_MESSAGE_TIMEOUT = 10``
      秒，必须连上立刻做完，不能丢给消费循环。
    * ``on_session_end`` —— 停掉 ping 看门狗 + 诊断这次会话为什么没活成。不停看门狗
      的后果是每次重连都留下一条线程守着一具已经关掉的连接 —— 到第三次重连就有三条
      僵尸线程互相抢心跳（与 qqbot 的同名子类同一个坑）。

    在 ``_on_open`` 里抛异常 = 这次会话失败（基类会关连接并退避重连）。
    """

    def __init__(
        self,
        *args: Any,
        on_session_open: Any = None,
        on_session_end: Any = None,
        **kw: Any,
    ) -> None:
        self._on_session_open = on_session_open
        self._on_session_end = on_session_end
        super().__init__(*args, **kw)

    def _on_open(self, conn: Any) -> None:
        if self._on_session_open is not None:
            self._on_session_open(conn)

    def _on_close(self, conn: Any) -> None:
        if self._on_session_end is not None:
            self._on_session_end(conn)


@register("homeassistant")
class HomeAssistantAdapter(Adapter):
    """Home Assistant 适配器：WS 事件总线入站 + 同连接 ``call_service`` 出站。

    能力声明（**与实现严格一致**，不许谎报 —— 调用点据 ``capabilities()`` 决策）：
    收事件（但**默认全丢**，见类属性 :attr:`require_user_context` 与
    :attr:`accept_all` 的说明）、只发纯文本、**没有编辑**、没有富媒体、没有 inline 按钮。
    """

    name = "homeassistant"
    label = "Home Assistant"
    #: ⚠️ 自选的保守上限（官方未公布任何数字，见 :data:`MESSAGE_LIMIT`）。
    max_message_length = MESSAGE_LIMIT
    supports_inbound = True              # WS 事件总线（订阅命令拿到的 event 消息）
    #: ⛔ **显式 False** —— principal 是 ``entity_id``，判据 (b) 不成立：
    #: 那是实体标识，不是"谁能跟我说话"的会话，用户也无法据此判断授权对不对。
    pairing_supported = False
    supports_inline_buttons = False      # WS API 里根本没有按钮这个概念
    supports_media = False               # 出站只发 service 的文本参数
    typed_command_prefix = "/"
    #: 真实凭据键就是这两个：``url``（HA 实例地址）+ ``token``（长期访问令牌）。
    #: 入站（``auth``）与出站（``call_service`` 走同一条 WS 连接）**都**依赖它们，
    #: 所以两者都在 ``outbound_tokens`` 里（不变量：outbound_tokens ⊆ required_tokens）。
    required_tokens = ("url", "token")
    outbound_tokens = ("url", "token")

    # -- 类级旋钮（测试可在实例上覆盖）----------------------------------
    min_interval = 0.0                    # HA 没有文档化的出站频率限制
    ping_interval = PING_INTERVAL
    command_timeout = COMMAND_TIMEOUT
    ws_recv_timeout = WS_RECV_TIMEOUT
    echo_suppress_seconds = ECHO_SUPPRESS_SECONDS

    def __init__(self, config: dict, hooks: Hooks) -> None:
        super().__init__(config, hooks)
        raw_url = _first(
            self.config, "url", "site_url", "base_url", "hass_url", "server_url",
        )
        #: HA 实例地址（**出站**目标）。留空则回落 :data:`DEFAULT_BASE_URL` 并告警。
        self.base_url: str = (raw_url or DEFAULT_BASE_URL).rstrip("/")
        self._url_configured: bool = bool(raw_url)
        #: 长期访问令牌。⚠️ **绝不进日志 / URL / 握手 header**（见模块 docstring）。
        self.token: str = _first(
            self.config, "token", "access_token", "hass_token",
            "long_lived_access_token",
        )

        # --- 入站过滤（**默认保守**，见模块 docstring 那一节）------------
        #: 订阅哪些事件类型（每种一条 ``subscribe_events``）。
        self.event_types: Tuple[str, ...] = self._config_event_types()
        #: 实体白名单（精确 entity_id）与域白名单（``light`` / ``binary_sensor`` …）。
        self.entities: Set[str] = _string_set(
            self.config.get("entities") or self.config.get("watch_entities")
        )
        self.domains: Set[str] = _string_set(
            self.config.get("domains") or self.config.get("watch_domains")
        )
        #: 显式"我要全收"。**默认 False**：两个白名单都空 ⇒ 事件全丢。
        self.accept_all: bool = _truthy(self.config.get("accept_all"), False)
        self.ignore_entities: Set[str] = _string_set(
            self.config.get("ignore_entities")
        )
        #: ⚠️ 默认 ``True``：只把 ``context.user_id`` 非空的事件当"某人做了一件事"。
        #: 这是"设备事件管道 ≠ 对话"这条判断被显式化的地方。
        self.require_user_context: bool = _truthy(
            self.config.get("require_user_context"), True
        )

        # --- 出站 -------------------------------------------------------
        self.service_domain: str = (
            _first(self.config, "service_domain", "domain") or DEFAULT_SERVICE_DOMAIN
        )
        self.service_name: str = (
            _first(self.config, "service_name", "service") or DEFAULT_SERVICE_NAME
        )
        self.notification_title: str = (
            _first(self.config, "notification_title", "title")
            or DEFAULT_NOTIFICATION_TITLE
        )
        self._throttle_lock = threading.Lock()
        self._last_send: Dict[str, float] = {}

        # --- 会话状态 ---------------------------------------------------
        self._transport: Optional[WebSocketTransport] = None
        self._ws_factory = None              # 测试注入点（默认走 ws.connect）
        self._consumer_thread: Optional[threading.Thread] = None
        self._id_lock = threading.Lock()
        self._msg_id = 0
        self._pending_lock = threading.Lock()
        self._pending: Dict[int, Dict[str, Any]] = {}
        self._authed = False
        self._subscribed: Set[str] = set()
        self._subscribed_ids: Set[int] = set()
        self._pong_ids: Set[int] = set()
        self._pong_lock = threading.Lock()
        self._last_ping_id: Optional[int] = None
        self._pong_received = False
        #: 最近一次鉴权被拒的原因（``auth_invalid`` 的 message）。给排障用。
        self.last_auth_error: Optional[str] = None

        # --- 心跳看门狗 ---------------------------------------------------
        self._hb_thread: Optional[threading.Thread] = None
        self._hb_stop = threading.Event()

        # --- 防回环：自己刚动过哪些实体 ------------------------------------
        self._echo_lock = threading.Lock()
        self._recent_targets: Dict[str, float] = {}

        # --- 一次性告警 ---------------------------------------------------
        self._warned_no_filter = False
        self._warned_default_url = False
        self._warned_edit = False
        self._warned_wildcard = False

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    def _config_event_types(self) -> Tuple[str, ...]:
        """读 ``event_types``；非法 / 空 → :data:`DEFAULT_EVENT_TYPES`（只订 state_changed）。

        刻意**不**把默认设成 ``*``（订阅全部）：``*`` 不在 ``SUBSCRIBE_ALLOWLIST`` 里，
        只有管理员能用，非管理员会被 ``Unauthorized`` 拒掉（见模块 docstring 第 4 条）。
        """
        raw = self.config.get("event_types")
        items = _string_set(raw) if raw not in (None, "") else set()
        if not items:
            return DEFAULT_EVENT_TYPES
        return tuple(sorted(items))

    def _uses_wildcard_subscription(self) -> bool:
        """是否要发"订阅全部事件"（``*``）——⚠️ 这需要**管理员**用户。"""
        return MATCH_ALL in self.event_types

    def _accepts_everything(self) -> bool:
        """没有配置任何实体 / 域过滤 ⇒ 默认**全丢**（保守）。"""
        return self.accept_all or bool(self.entities) or bool(self.domains)

    def capabilities(self) -> dict:
        """在基类快照之上，**额外暴露事件过滤状态**。

        ⚠️ **为什么必须暴露**：本平台默认「一个事件都不收」——只有显式给``entities`` /
        ``domains`` / ``accept_all`` 才会放行。这本是刻意的保守设计（设备状态变更
        不等于"有人跟你说话"，见模块 docstring），但它带来一个危险后果：

        用户只配齐了 ``required_tokens``（``url`` + ``token``）后，``--status`` 会显示
        「已配置 / 入站就绪」，而实际上**一个事件都收不到**。这与本项目修过多次的
        "配得完全正确却被判成不可用 / 可用但其实不行"是同一类问题 ——
        **状态视图说就绪，而实际不工作，且不报错**。

        所以这里给出机器可读的判据：``inbound_accepts_anything=False`` 就是一个
        明确的"配好了但收不到"信号，``--setup --json`` / ``--status`` 的 JSON 输出
        能直接读到，不必去翻日志。
        """
        caps = dict(super().capabilities())
        accepts = self._accepts_everything()
        caps.update(
            {
                # False = 配齐了凭据但**收不到任何事件**（除非再配 entities/domains/accept_all）
                "inbound_accepts_anything": accepts,
                "accept_all": bool(self.accept_all),
                "filter_entities_count": len(self.entities),
                "filter_domains_count": len(self.domains),
                "event_types": tuple(self.event_types),
                # True 时 context.user_id 为空的事件会被丢（机器/定时器触发的事件）
                "require_user_context": bool(self.require_user_context),
            }
        )
        return caps

    # ------------------------------------------------------------------
    # URL / 连接
    # ------------------------------------------------------------------
    def _scheme(self, ws: bool) -> str:
        """http/https ↔ ws/wss。"""
        scheme = (urllib.parse.urlsplit(self.base_url).scheme or "").lower()
        if ws:
            if scheme in ("http", "ws"):
                return "ws"
            return "wss"                      # 没写 scheme / https 都按 wss
        if scheme in ("http", "ws"):
            return "http"
        return "https"

    def ws_url(self) -> str:
        """本次连接用的 URL。

        ⚠️ **凭据绝不进 URL**（官方也不要求：``WebsocketAPIView.requires_auth = False``，
        鉴权是连上之后的 ``auth`` 消息）。
        """
        parts = urllib.parse.urlsplit(self.base_url)
        base_path = parts.path.rstrip("/")     # 保留子路径部署
        return f"{self._scheme(True)}://{parts.netloc}{base_path}{WS_PATH}"

    def _make_ws(self, url: str) -> Any:
        """建 WS 连接。**握手不带任何 header**（``headers=None``）。

        官方要求凭据放在 ``auth`` 消息里；多塞一个 ``Authorization`` 头只会多一处
        泄漏面（会进中间代理的日志），所以这里刻意不传。
        """
        factory = self._ws_factory
        if factory is None:
            from ..ws import connect as factory   # 延迟导入：没开入站就不加载它
        return factory(url, timeout=float(self.ws_recv_timeout))

    # ------------------------------------------------------------------
    # 会话状态（测试与状态视图消费）
    # ------------------------------------------------------------------
    @property
    def transport(self) -> Optional[WebSocketTransport]:
        """当前传输层（测试 / 诊断用）。"""
        return self._transport

    @property
    def authenticated(self) -> bool:
        """**本次会话已通过鉴权**（收到过 ``auth_ok``）。"""
        return self._authed

    @property
    def subscribed_event_types(self) -> Set[str]:
        """本次会话**订阅成功**的事件类型集合。"""
        return set(self._subscribed)

    @property
    def running(self) -> bool:
        """消费线程是否活着（代理到传输层 —— 本平台线程归 Transport 所有）。"""
        transport = self._transport
        return transport is not None and transport.running

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def _make_transport(self) -> WebSocketTransport:
        """构造传输层。

        * **不用** ``on_message``：本平台没有"服务端要求立刻重连"这类控制消息，
          全部消息都在 ``on_event`` 里处理（``result`` / ``event`` / ``pong``）。
        * ``reset_after=0`` = 连上即重置退避（不变量 10）。
        """
        return _HomeAssistantTransport(
            self._open_session,
            on_session_open=self._handshake,
            on_session_end=self._on_session_end,
            name="homeassistant",
            min_backoff=RECONNECT_MIN,
            max_backoff=RECONNECT_MAX,
            reset_after=0.0,
        )

    def start(self) -> None:
        """启动入站。缺 ``token`` 时只告警不起线程（**不抛**）。"""
        if not self.token:
            logger.warning(
                "homeassistant: token missing; adapter not started（需要 HA 用户的"
                "长期访问令牌，在 HA 的个人资料页创建）"
            )
            return
        if not self._url_configured and not self._warned_default_url:
            self._warned_default_url = True
            logger.warning(
                "homeassistant: 未配置 url，回落到 %s —— **该地址不是官方文档规定的**"
                "（mDNS 主机名惯例），请显式写成你 HA 实例的真实地址",
                DEFAULT_BASE_URL,
            )
        if not self._accepts_everything() and not self._warned_no_filter:
            self._warned_no_filter = True
            logger.warning(
                "homeassistant: 没有配置 entities / domains / accept_all —— "
                "**默认一个事件都不收**。本平台推的是设备状态变更而不是人的消息"
                "（见 adapters/homeassistant.py 的模块 docstring），请显式声明"
                "你关心的实体 / 域，或设 accept_all: true 并考虑把 "
                "require_user_context 保持 true（只把能追到某个 HA 用户的事件当对话）"
            )
        if self._uses_wildcard_subscription() and not self._warned_wildcard:
            self._warned_wildcard = True
            logger.warning(
                "homeassistant: event_types 含 '*'（订阅**全部**事件）—— 官方闸门是"
                "「event_type 不在 SUBSCRIBE_ALLOWLIST 且用户不是管理员」，而 '*' 不在"
                "白名单里，所以**必须用管理员用户的令牌**，否则订阅会被拒"
                "（诊断文案里会写 unauthorized）。"
            )
        self._stop_event.clear()
        transport = self._make_transport()
        self._transport = transport
        transport.start(self._on_event)
        logger.info(
            "homeassistant: event loop started (url=%s, event_types=%s, "
            "entities=%d, domains=%d, accept_all=%s, require_user_context=%s)",
            _redact(self.ws_url()),
            ",".join(self.event_types),
            len(self.entities),
            len(self.domains),
            self.accept_all,
            self.require_user_context,
        )

    def stop(self) -> None:
        """**先停看门狗，再关传输层（它内部先关连接再 join），最后 ``super().stop()``**。

        顺序不能反：``recv()`` 最多会阻塞 :data:`WS_RECV_TIMEOUT`；先把连接关掉才能立刻
        唤醒它，否则每次 ``stop()`` 都白等 —— 这个坑本项目每个 WS 适配器都踩过。
        幂等。
        """
        self._stop_watchdog()
        transport, self._transport = self._transport, None
        if transport is not None:
            transport.stop()
        with self._pending_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for slot in pending:      # 别让任何还在等 result 的线程永远挂着
            slot["event"].set()
        super().stop()

    # ------------------------------------------------------------------
    # 传输层回调
    # ------------------------------------------------------------------
    def _open_session(self) -> Any:
        """``Transport._open``：建 WS 连接，并重置**每连接**状态。"""
        with self._id_lock:
            self._msg_id = 0                 # id 必须每连接从 0 递增（见模块 docstring）
        with self._pending_lock:
            self._pending.clear()
        self._authed = False
        self._subscribed = set()
        self._subscribed_ids = set()
        with self._pong_lock:
            self._pong_ids = set()
        self._pong_received = False
        self._last_ping_id = None
        return self._make_ws(self.ws_url())

    def _handshake(self, conn: Any) -> None:
        """握手 → ``auth`` → ``subscribe_events``。**抛异常 = 这次会话失败**（退避重连）。

        由 :class:`_HomeAssistantTransport` 在 ``Transport._open`` 之后、消费循环开始
        之前调用（那一刻还没有别人读这条连接，所以这里可以放心同步读）。

        HA 的鉴权是**带内**的，而且服务端只给 ``AUTH_MESSAGE_TIMEOUT = 10`` 秒：
        连上必须立刻发 ``auth``，所以整段握手都在这里同步做完，而不是丢给消费循环。
        """
        self._consumer_thread = threading.current_thread()
        url = _redact(getattr(conn, "url", "") or self.ws_url())
        logger.info("homeassistant: websocket 已连接 %s", url)

        # 1) 等 auth_required（官方：连上第一条一定是它）
        first = self._recv_json(conn, "auth_required")
        kind = str(first.get("type") or "")
        if kind != "auth_required":
            # 诊断放在适配器这里而不是只抛异常：传输层只会把它记成"会话出错"，
            # 用户看不出到底是"地址不对"还是"HA 版本不认"。
            logger.warning(
                "homeassistant: 鉴权阶段收到的第一条消息 type=%r，而官方协议要求是"
                " 'auth_required'。请确认 url 指向的确实是 Home Assistant 的 %s"
                "（多半是路径写错、或前面挂了个不转发 WebSocket 的反向代理）。",
                kind or "?", WS_PATH,
            )
            raise RuntimeError(
                f"鉴权阶段：第一条消息的 type={kind!r}，官方协议要求是 'auth_required'"
                "（这个地址可能不是 Home Assistant 的 /api/websocket）"
            )
        ha_version = str(first.get("ha_version") or "").strip()
        logger.info("homeassistant: auth_required（HA 版本 %s）", ha_version or "?")

        # 2) 发 auth —— ⚠️ 这条消息**不能带 id**，也不能带别的键。
        #    凭据只在这里出现一次，绝不进 URL / header / 日志。
        self._send_json(conn, {"type": "auth", "access_token": self.token})

        # 3) 等 auth_ok / auth_invalid
        second = self._recv_json(conn, "auth_ok")
        verdict = str(second.get("type") or "")
        if verdict != "auth_ok":
            detail = str(second.get("message") or "").strip()
            self.last_auth_error = detail or "auth_invalid"
            # ⚠️ 文案里**不带令牌**：这里只说"被拒了"与该查什么。
            logger.error(
                "homeassistant: 鉴权被拒（auth_invalid：%s）。请依次检查：1) 令牌是否是"
                " HA 个人资料页生成的**长期访问令牌**；2) 该用户是否被停用；"
                "3) 这个 HA 是否要求本机之外的连接额外放行。官方在这一步之后会直接"
                "断开连接，我们按退避重试。",
                detail or "（服务端没给原因）",
            )
            raise RuntimeError("homeassistant 拒绝了凭据（auth_invalid）")

        self._authed = True
        logger.info("homeassistant: auth_ok（HA 版本 %s）", ha_version or "?")

        # 4) 订阅（每种事件类型一条命令 —— 官方明确要求）
        self._subscribe(conn)

        # 5) 起应用层 ping 看门狗（HA 的 ping/pong 是客户端发起，见模块 docstring）
        self._start_watchdog(conn)

    def _subscribe(self, conn: Any) -> None:
        """按 :attr:`event_types` 逐个订阅；一条都没成功 ⇒ 抛异常让这次会话失败。

        失败的诊断要区分两种：``unauthorized``（订阅的类型不在 ``SUBSCRIBE_ALLOWLIST``
        且用户不是管理员 —— 通配 ``*`` 必踩）与其他（多半是地址 / 版本不匹配）。
        """
        failures: List[str] = []
        for event_type in self.event_types:
            payload: Dict[str, Any] = {"type": "subscribe_events"}
            if event_type != MATCH_ALL:
                payload["event_type"] = event_type
            msg_id = self._alloc_id()
            payload["id"] = msg_id
            self._send_json(conn, payload)
            result = self._recv_json(conn, f"subscribe_events({event_type}) 的结果")
            if result.get("type") != "result":
                failures.append(f"{event_type}: 期望 result，收到 {result.get('type')!r}")
                continue
            if result.get("success") is True:
                self._subscribed.add(event_type)
                self._subscribed_ids.add(msg_id)
                continue
            error = result.get("error")
            code = str((error or {}).get("code") or "") if isinstance(error, dict) else ""
            message = str((error or {}).get("message") or "") if isinstance(error, dict) else ""
            failures.append(f"{event_type}: code={code or '?'} {message}".strip())
            if code == _ERR_UNAUTHORIZED:
                logger.error(
                    "homeassistant: 订阅 %s 被拒（unauthorized）。官方闸门是"
                    "「event_type 不在 SUBSCRIBE_ALLOWLIST 且用户不是管理员」——"
                    "白名单里只有 state_changed 与若干注册表事件，通配 '*' 必被拒。"
                    "请改用 state_changed，或换管理员令牌。",
                    event_type,
                )
        if not self._subscribed:
            raise RuntimeError(
                "homeassistant: 所有事件订阅都失败 —— " + "；".join(failures or ["（无详情）"])
            )
        for item in failures:
            logger.warning("homeassistant: 订阅失败，已忽略该类型：%s", item)
        logger.info(
            "homeassistant: 已订阅事件类型 %s（id=%s）",
            ",".join(sorted(self._subscribed)),
            ",".join(str(i) for i in sorted(self._subscribed_ids)),
        )

    def _on_session_end(self, conn: Any) -> None:
        """每条会话结束时停看门狗 + 诊断**为什么**没活成。"""
        self._stop_watchdog()
        # 会话已经结束 ⇒ "本次会话已鉴权"这个状态不再成立（否则状态视图会把一条
        # 已经断掉的连接报成"鉴权正常"）。
        self._authed = False
        if self._stop_event.is_set():
            return                      # 我们自己要停的，不算故障
        code = getattr(conn, "close_code", None)
        if not self._authed:
            logger.warning(
                "homeassistant: 会话在鉴权成功之前就结束了（close_code=%s）—— "
                "先查 url 是否指向 HA 的 %s、网络 / TLS 是否通；鉴权失败时官方会"
                "发完 auth_invalid 直接断开。", code, WS_PATH,
            )
        else:
            logger.info("homeassistant: 会话结束（close_code=%s），将按退避重连", code)

    # -- 帧处理 ---------------------------------------------------------
    def _on_event(self, frame: Any) -> None:
        """``Transport.start(on_event)``：处理每一条下行消息。

        基类兜住这里的异常，所以一次畸形消息不可能把消费线程带走（不变量 4）。
        """
        try:
            packet = self._parse(frame)
            if packet is None:
                return
            kind = str(packet.get("type") or "")
            if kind == "event":
                self._handle_event(packet)
            elif kind == "result":
                self._resolve_pending(packet)
            elif kind == "pong":
                self._on_pong(packet)
            elif kind == "ping":
                # ⚠️ **防御性分支**：官方协议里 ping 是**客户端发**、服务端回 pong，
                # 服务端不会主动发 JSON ping（``handle_ping`` 是命令处理器）。
                # 万一将来 HA 改成服务端发起，这里按同一条命令的格式回 pong 即可。
                self._reply_pong(packet)
            else:
                logger.debug("homeassistant: 忽略 type=%s 的消息", kind or "?")
        except Exception:  # noqa: BLE001 - 用户回调出错不该断连
            logger.exception("homeassistant: 处理下行消息失败（已忽略）")

    def _parse(self, frame: Any) -> Optional[dict]:
        """把一帧解析成 dict；**任何**畸形输入都只 debug 一行，绝不外抛、绝不打印内容。

        帧内容可能含超长 payload，也可能含用户数据，所以**不把它抄进日志**。
        """
        raw = frame if isinstance(frame, str) else ""
        if not raw:
            logger.debug("homeassistant: 空帧，忽略")
            return None
        try:
            parsed = json.loads(raw)
        except Exception:  # noqa: BLE001
            logger.debug("homeassistant: 非 JSON 帧，忽略（%d 字节）", len(raw))
            return None
        if not isinstance(parsed, dict):
            logger.debug("homeassistant: 帧不是 JSON 对象（%s），忽略", type(parsed).__name__)
            return None
        return parsed

    def _recv_json(self, conn: Any, expect: str) -> dict:
        """握手期同步读一条消息。返回 dict；对端关闭 / 超时 / 非 JSON 都抛。

        ``expect`` 只进错误文案，便于排障时看清卡在哪一步。
        """
        frame = conn.recv()
        if frame is None:
            code = getattr(conn, "close_code", None)
            raise RuntimeError(
                f"在等 {expect} 时服务端关闭了连接（close_code={code}）"
            )
        parsed = self._parse(frame)
        if parsed is None:
            raise RuntimeError(f"在等 {expect} 时收到无法解析的帧（{len(str(frame))} 字节）")
        return parsed

    def _send_json(self, conn: Any, payload: dict) -> None:
        """发一条 JSON 命令。失败向上抛（让这次会话失败并退避重连）。"""
        conn.send(json.dumps(payload, ensure_ascii=False))

    def _alloc_id(self) -> int:
        """分配命令 id。**必须严格递增**（官方 ``ERR_ID_REUSE``）。"""
        with self._id_lock:
            self._msg_id += 1
            return self._msg_id

    def _reply_pong(self, packet: dict) -> None:
        """回 ``{"id": .., "type": "pong"}``（只用于上面那个防御性分支）。"""
        conn = self._connection()
        if conn is None:
            return
        msg_id = packet.get("id")
        if not isinstance(msg_id, int) or isinstance(msg_id, bool):
            return
        try:
            self._send_json(conn, {"id": msg_id, "type": "pong"})
        except Exception as exc:  # noqa: BLE001
            logger.debug("homeassistant: 回 pong 失败: %s", exc)

    def _connection(self) -> Any:
        transport = self._transport
        return None if transport is None else transport.connection

    # -- result / pong --------------------------------------------------
    def _resolve_pending(self, packet: dict) -> None:
        """``{"id", "type":"result", "success", "result"?|"error"?}`` → 唤醒等待者。"""
        msg_id = packet.get("id")
        if isinstance(msg_id, bool) or not isinstance(msg_id, int):
            logger.debug("homeassistant: result 缺少合法 id，忽略")
            return
        with self._pending_lock:
            slot = self._pending.pop(msg_id, None)
        if slot is None:
            # 订阅命令的 result 在握手期同步读掉了；到这里的只可能是"来晚了"的回包。
            error = packet.get("error")
            logger.debug(
                "homeassistant: 收到无主的 result id=%s success=%s%s",
                msg_id, packet.get("success"),
                f" code={(error or {}).get('code')}" if isinstance(error, dict) else "",
            )
            return
        slot["success"] = packet.get("success") is True
        slot["result"] = packet.get("result")
        error = packet.get("error")
        if isinstance(error, dict):
            slot["code"] = str(error.get("code") or "")
            slot["message"] = str(error.get("message") or "")
        slot["event"].set()

    def _on_pong(self, packet: dict) -> None:
        """收到 ``pong``：给看门狗一个"连接还活着"的证据。"""
        msg_id = packet.get("id")
        with self._pong_lock:
            if isinstance(msg_id, int) and not isinstance(msg_id, bool):
                self._pong_ids.add(msg_id)
        self._pong_received = True
        with self._pending_lock:
            slot = self._pending.get(msg_id) if isinstance(msg_id, int) else None
        if slot is not None:
            slot["success"] = True
            slot["result"] = None
            slot["event"].set()

    # ------------------------------------------------------------------
    # 应用层心跳（HA 的 ping/pong 是**客户端发起**）
    # ------------------------------------------------------------------
    def _start_watchdog(self, conn: Any) -> None:
        self._stop_watchdog()
        interval = float(getattr(self, "ping_interval", PING_INTERVAL) or PING_INTERVAL)
        stop = threading.Event()
        self._hb_stop = stop
        thread = threading.Thread(
            target=self._watchdog_loop,
            args=(conn, interval, stop),
            name="homeassistant-heartbeat",
            daemon=True,
        )
        self._hb_thread = thread
        thread.start()

    def _watchdog_loop(self, conn: Any, interval: float, stop: threading.Event) -> None:
        """周期发 ``{"id": N, "type": "ping"}``，一个周期内没收到 pong 就判死并断开。

        ⚠️ **为什么必须有它**（与 mattermost 的关键区别）：Mattermost 的保活由服务端
        RFC 6455 ping 驱动，靠 ``ws.py`` 自动回 pong 就够；**HA 的应用层 ping/pong
        方向是反的** —— 官方文档原文是"receiving a ping from the client"，
        ``handle_ping`` 也是客户端命令。不主动发，就永远拿不到"HA 事件循环还活着"
        的证据，而 TCP 半开（写缓冲还在、读已经没人回）时 ``recv()`` 会一直挂着。

        ⚠️ **周期恰好等于 ``interval``**（不是两倍）：每个周期先检查**上一发**的 pong
        到没到、没到就判死，再发新的一发。写成"发完再等一个周期"会把周期变成两倍。
        """
        outstanding = False
        while not stop.is_set() and not self._stop_event.is_set():
            if stop.wait(interval):
                return
            if getattr(conn, "closed", False):
                return
            if outstanding and not self._pong_received:
                logger.warning(
                    "homeassistant: %.1fs 内没收到 pong，判定连接已死，断开重连",
                    interval,
                )
                self._hard_close(conn)
                return
            outstanding = True
            self._pong_received = False
            try:
                msg_id = self._alloc_id()
                self._last_ping_id = msg_id
                self._send_json(conn, {"id": msg_id, "type": "ping"})
            except Exception as exc:  # noqa: BLE001 - 发不出去就交给上层重连
                logger.debug("homeassistant: 发 ping 失败: %s", exc)
                return

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

    @staticmethod
    def _hard_close(conn: Any, code: int = 4000, reason: str = "") -> None:
        """主动断开（用来让传输层立刻重连）。4000 = 应用私有区间，语义是"我们放弃它"。"""
        try:
            conn.close(code, reason)
        except TypeError:      # 测试替身 / 其他实现的 close() 可能不收参数
            try:
                conn.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("homeassistant: ws close 失败（忽略）: %s", exc)
        except Exception as exc:  # noqa: BLE001
            logger.debug("homeassistant: ws close 失败（忽略）: %s", exc)

    # ------------------------------------------------------------------
    # 入站：事件 → Inbound
    # ------------------------------------------------------------------
    def _drop_inbound(self, reason: str, entity_id: str = "") -> None:
        logger.info(
            "homeassistant: 丢弃事件（%s）entity=%s", reason, entity_id or "?",
        )

    def _handle_event(self, packet: dict) -> bool:
        """``type == "event"`` → 过滤 → Inbound。返回是否真的放行了一条。"""
        event = packet.get("event")
        if not isinstance(event, dict):
            self._drop_inbound("event 字段不是对象")
            return False
        event_type = str(event.get("event_type") or "")
        # 订阅了通配 `*` 时什么都会推过来；`event_types` 里含 `*` = 显式"我全收"。
        if MATCH_ALL not in self.event_types and event_type not in self.event_types:
            self._drop_inbound(f"未订阅的事件类型 event_type={event_type!r}")
            return False
        data = event.get("data")
        if not isinstance(data, dict):
            # ⚠️ 只说"不是对象"，**不把 data 抄进日志**（可能是设备属性 / 用户数据）。
            self._drop_inbound(f"{event_type} 的 data 不是对象")
            return False
        entity_id = str(data.get("entity_id") or "").strip()
        if not entity_id:
            # 没有 entity_id 就拼不出会话标识 → 无处路由，诚实丢弃而不是硬编一个。
            self._drop_inbound(f"{event_type} 里没有 entity_id，无法路由")
            return False
        if not self._entity_allowed(entity_id):
            self._drop_inbound("不在 entities / domains 白名单内（默认全丢）", entity_id)
            return False
        if entity_id in self.ignore_entities:
            self._drop_inbound("在 ignore_entities 里", entity_id)
            return False
        if self._is_own_echo(entity_id):
            self._drop_inbound("是我们自己刚调服务造成的回声（防自问自答回环）", entity_id)
            return False

        context = event.get("context")
        context = context if isinstance(context, dict) else {}
        user_id = str(context.get("user_id") or "").strip()
        if self.require_user_context and not user_id:
            # ⚠️ 这就是"设备事件 ≠ 对话"的判据：追不到真人用户的事件不当地成对话。
            self._drop_inbound(
                "context.user_id 为空（不是某个用户的操作；"
                "定时器/脚本/集成自己触发的事件就是这样）",
                entity_id,
            )
            return False

        text = self._render(event_type, entity_id, data)
        if not text.strip():
            self._drop_inbound("渲染不出正文", entity_id)
            return False

        cid = self.conversation_id_for(entity_id)
        # 授权闸门必须在产生 Inbound **之前**（否则能用命令 / 审批字绕过，不变量 3）。
        #    ⚠️ 本平台 :attr:`pairing_supported` = False（principal 是 entity_id），
        #    配对分支恒不成立；保留那半行是为了与其它平台同一形态。
        if not self.admits(entity_id) and not self.answer_pairing_request(
            entity_id, cid, text
        ):
            self._drop_inbound("未在白名单", entity_id)
            return False
        message_id = str(context.get("id") or "").strip()
        try:
            self.hooks.on_inbound(
                Inbound(
                    conversation_id=cid,
                    text=text,
                    kind="text",
                    user_id=user_id or None,
                    message_id=message_id or None,
                    platform=self.name,
                    raw=event,
                )
            )
        except Exception as exc:  # noqa: BLE001 - 上层炸了也不能让消费线程退出
            logger.exception("homeassistant: on_inbound 失败: %s", exc)
            return False
        return True

    def _entity_allowed(self, entity_id: str) -> bool:
        """实体是否落在配置的白名单里。**两个白名单都空且没 accept_all ⇒ 拒**。"""
        if self.accept_all:
            return True
        if entity_id in self.entities:
            return True
        domain = entity_id.partition(".")[0]
        return bool(domain) and domain in self.domains

    def _render(self, event_type: str, entity_id: str, data: dict) -> str:
        """把事件渲染成一行可读文本（不做任何"像人话"的加工）。

        刻意保守：只陈述事实（谁 / 哪个实体 / 从什么变成什么），不猜测意图。
        """
        if event_type == EVENT_STATE_CHANGED:
            old_state = data.get("old_state")
            new_state = data.get("new_state")
            if not isinstance(new_state, dict):
                # 实体被删除时 new_state 为 null —— 没有"新状态"可报告。
                return ""
            label = self._state_label(new_state, entity_id)
            return (
                f"[Home Assistant] {label}: "
                f"{self._state_value(old_state)} -> {self._state_value(new_state)}"
            )
        # 非 state_changed：只列 event_type 与几个标量字段，不猜语义。
        extras = [
            f"{key}={value}"
            for key, value in list(data.items())[:4]
            if not isinstance(value, (dict, list))
        ]
        tail = (" " + " ".join(extras)) if extras else ""
        return f"[Home Assistant] 事件 {event_type}{tail}"

    @staticmethod
    def _state_value(state: object) -> str:
        if not isinstance(state, dict):
            return "(无)"
        return str(state.get("state") or "?")

    @staticmethod
    def _state_label(state: dict, entity_id: str) -> str:
        """``friendly_name (entity_id)``；没有 friendly_name 就只给 entity_id。"""
        attributes = state.get("attributes")
        name = ""
        if isinstance(attributes, dict):
            name = str(attributes.get("friendly_name") or "").strip()
        return f"{name} ({entity_id})" if name else entity_id

    # ------------------------------------------------------------------
    # 防回环：我们自己刚动过哪些实体
    # ------------------------------------------------------------------
    def _remember_targets(self, entity_ids: Set[str]) -> None:
        """记下"我们刚对这些实体调过服务"（防自问自答回环，理由见模块 docstring）。"""
        if not entity_ids:
            return
        window = float(getattr(self, "echo_suppress_seconds", ECHO_SUPPRESS_SECONDS) or 0.0)
        now = time.monotonic()
        with self._echo_lock:
            for entity_id in entity_ids:
                self._recent_targets[entity_id] = now + window
            # 顺手清掉过期条目（dict 很小，但长期运行不该无限长）
            if len(self._recent_targets) > 64:
                self._recent_targets = {
                    key: until for key, until in self._recent_targets.items() if until > now
                }

    def _is_own_echo(self, entity_id: str) -> bool:
        with self._echo_lock:
            until = self._recent_targets.get(entity_id)
        return until is not None and until > time.monotonic()

    # ------------------------------------------------------------------
    # 会话标识（``platform:local_id``）
    # ------------------------------------------------------------------
    @staticmethod
    def conversation_id_for(entity_id: object) -> str:
        """``homeassistant:<entity_id>``。

        用 entity_id 作 local 段：它自带域（``light.x`` / ``binary_sensor.y``），
        全局唯一且可读，``--status`` / 日志里一眼能看出是哪个实体。
        ⚠️ local 段**不含冒号**，所以 :func:`identity.format_id` 的往返是无损的。
        """
        return identity.format_id("homeassistant", entity_id)

    @staticmethod
    def split_conversation(conversation_id: object) -> Optional[str]:
        """``homeassistant:<entity_id>`` → ``<entity_id>``；不认识 / 旧格式则 ``None``。

        旧格式（``channel:`` / ``chat:`` 这类歧义前缀）会被 :func:`identity.local_of`
        拒掉，不会静默当成 homeassistant 会话。
        """
        local = identity.local_of(conversation_id)
        if not local:
            return None
        parsed = identity.parse_id(conversation_id)
        return local if parsed.platform == "homeassistant" else None

    # ------------------------------------------------------------------
    # 出站：同一条 WS 连接上的 ``call_service``
    # ------------------------------------------------------------------
    def _throttle(self, conversation_id: str) -> None:
        interval = float(getattr(self, "min_interval", 0.0) or 0.0)
        if interval <= 0:
            return
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

    def _service_payload(self, msg_id: int, text: str, entity_id: str) -> dict:
        """构造 ``call_service`` 命令。

        * ``service_data`` 用 ``message`` + ``title`` —— 这是
          ``persistent_notification.create`` 的**官方 schema**
          （``vol.Required("message")`` / ``vol.Optional("title")``）。
        * local 段像个 entity_id（含 ``.``）时**额外**带上 ``target``，这样把出站换成
          ``notify`` / ``tts`` 之类的服务时目标才明确；对
          ``persistent_notification.create`` 是无害的（它的 schema 不读 target）。
        """
        service_data: Dict[str, Any] = {"message": text}
        if self.notification_title:
            service_data["title"] = self.notification_title
        payload: Dict[str, Any] = {
            "id": msg_id,
            "type": "call_service",
            "domain": self.service_domain,
            "service": self.service_name,
            "service_data": service_data,
        }
        if entity_id and "." in entity_id:
            payload["target"] = {"entity_id": [entity_id]}
        return payload

    def _command(
        self, payload_factory: Any, *, timeout: float, what: str
    ) -> Tuple[bool, Any, Dict[str, Any]]:
        """发一条命令并等它的 ``result``。返回 ``(success, result, error)``。

        * 没连接 → ``(False, None, {"code": "", "message": "..."})``。
        * **重入保护**：在消费线程里调用会与自己死锁（等自己读 result）⇒ 直接失败，
          而不是假装成功。
        * 超时 → 取消等待（迟到的 result 会被记成"无主的 result"而丢弃）。
        """
        conn = self._connection()
        if conn is None:
            return False, None, {
                "code": "", "message": "Home Assistant 连接不可用（未连接或正在重连）",
            }
        if threading.current_thread() is self._consumer_thread:
            return False, None, {
                "code": "", "message": "不能在消费线程里同步等待 command 结果（会死锁）",
            }
        msg_id = self._alloc_id()
        slot: Dict[str, Any] = {
            "event": threading.Event(), "success": False,
            "result": None, "code": "", "message": "",
        }
        with self._pending_lock:
            self._pending[msg_id] = slot
        try:
            self._send_json(conn, payload_factory(msg_id))
        except Exception as exc:  # noqa: BLE001 - 发不出去 = 连接已死
            with self._pending_lock:
                self._pending.pop(msg_id, None)
            return False, None, {"code": "", "message": f"{what} 发送失败: {exc}"}
        if not slot["event"].wait(timeout=float(timeout or COMMAND_TIMEOUT)):
            with self._pending_lock:
                self._pending.pop(msg_id, None)
            return False, None, {"code": "timeout", "message": f"{what} 等待结果超时"}
        return (
            bool(slot["success"]),
            slot["result"],
            {"code": slot["code"], "message": slot["message"]},
        )

    def send(self, out: Outbound) -> MsgHandle | None:
        """调一次 service（默认 ``persistent_notification.create``），超长自动分片。

        HA 没有"发消息"原语，所以出站就是调 service（见模块 docstring）。**没有连接
        就返回 None 并记 ``TRANSIENT``** —— 绝不假装送达（不变量 15）。
        """
        entity_id = self.split_conversation(out.conversation_id)
        if not entity_id:
            logger.warning("homeassistant: bad conversation_id %r", out.conversation_id)
            self._note_send_failure(SendError.BAD_FORMAT, "bad conversation_id")
            return None
        if not out.text:
            logger.warning("homeassistant: refusing to send empty text")
            self._note_send_failure(SendError.BAD_FORMAT, "empty text")
            return None
        if not self.token:
            logger.warning("homeassistant: token missing; send refused")
            self._note_send_failure(SendError.BAD_FORMAT, "token missing")
            return None

        chunks = split_text(out.text, self.effective_max_length, prefix_fmt="")
        if len(chunks) > 1:
            logger.info(
                "homeassistant: splitting outbound message into %d chunks (limit=%d，"
                "⚠️ 上限是自选保守值，官方未公布)", len(chunks), self.effective_max_length,
            )
        handle: Optional[MsgHandle] = None
        for chunk in chunks:
            self._throttle(out.conversation_id)
            ok, result, error = self._command(
                lambda msg_id: self._service_payload(msg_id, chunk, entity_id),
                timeout=float(getattr(self, "command_timeout", COMMAND_TIMEOUT)),
                what=f"{self.service_domain}.{self.service_name}",
            )
            if not ok:
                code = str(error.get("code") or "")
                detail = str(error.get("message") or "") or code or "未知失败"
                logger.warning(
                    "homeassistant: %s.%s 失败（code=%s）: %s",
                    self.service_domain, self.service_name, code or "?", detail,
                )
                # ⚠️ **没有 HA 错误码 = 失败发生在本地**（没连接 / 发送出错 / 等结果
                # 超时），那是传输层问题 ⇒ ``TRANSIENT``。有错误码才按平台码分类。
                kind = (
                    _classify_ha_error(code, detail) if code else SendError.TRANSIENT
                )
                self._note_send_failure(kind, detail)
                return handle if handle is not None else None
            self._remember_targets({entity_id} if "." in entity_id else set())
            context = result.get("context") if isinstance(result, dict) else None
            message_id = ""
            if isinstance(context, dict):
                message_id = str(context.get("id") or "").strip()
            handle = MsgHandle(
                conversation_id=out.conversation_id,
                # HA 的 result 里没有"消息 id"；能拿到的唯一标识是这次调用产生的
                # context id。如实用它（edit() 本来就恒 False，它只用于日志 / 去重）。
                message_id=message_id or f"{self.service_domain}.{self.service_name}",
                platform=self.name,
            )
        return handle

    def edit(self, handle: MsgHandle, out: Outbound) -> bool:
        """**恒返回 ``False``** —— HA 没有"编辑"这个概念。

        依据（已核实，见模块 docstring）：

        * WebSocket API 的命令表里**没有**任何"改一条已有内容"的命令；
        * ``persistent_notification`` 组件只注册了 ``create`` / ``dismiss`` /
          ``dismiss_all`` 三个 service —— ``dismiss`` 是**删掉**，不是改内容。

        返回 ``False`` 让 core 退化成"再发一条新消息"（不变量 4）。
        """
        if not self._warned_edit:
            self._warned_edit = True
            logger.info(
                "homeassistant: edit() 恒返回 False —— HA 的 WebSocket API 没有编辑命令，"
                "persistent_notification 只有 create / dismiss / dismiss_all"
                "（dismiss 是删不是改）。core 会退化成发新消息。"
            )
        return False

    def answer(self, query_id: str, text: str = "") -> None:
        """本适配器不构造任何按钮事件，没有需要应答的 callback query。"""
        return None
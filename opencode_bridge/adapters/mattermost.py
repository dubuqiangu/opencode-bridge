"""Lane B — Mattermost adapter (tasks.md T3.2). Standard library only.

传输层与 Discord 入站同构：入站是 ``{site_url}/api/v4/websocket`` 上的 JSON over
WebSocket，复用 :mod:`opencode_bridge.ws`（T2.0，纯标准库自研的最小 RFC 6455 客户端）；
出站走 REST（``POST /api/v4/posts`` / ``PUT /api/v4/posts/{id}/patch``）。零第三方依赖。

G4：WS 连接 / 收包循环 / 退避 / 线程 / ``stop()`` 语义已迁到
:class:`~opencode_bridge.transport.WebSocketTransport`。本文件只留 Mattermost 语义：
两个信封的判别、**seq 记账**、可靠重连的 ``connection_id``、challenge 回应、
断开诊断（鉴权失败 vs 网络抖动）、事件过滤、出站 REST。

⚠️ 迁移里有两处**刻意保留**的细节，动了就会造成重复 / 丢失消息
----------------------------------------------------------------------
1. **先记 seq、再过滤**（:meth:`MattermostAdapter._handle_packet`）：``seq`` 由
   **服务端分配**，被过滤掉的事件（自己发的、系统消息、已删除、未授权频道…）
   **同样占用了一个 seq**。不记它，可靠重连就会把那条事件再投一次。
   同理，**响应信封（带 ``status`` 的）绝不能推进 ``_last_seq``** —— 它的
   ``seq_reply`` 回应的是我们自己的请求，不是事件流的位置。
2. **可靠重连的两个参数必须同时给**（:meth:`MattermostAdapter._ws_url`）：只有
   ``connection_id`` + ``sequence_number`` **都在**时服务端才会去查事件队列补发；
   只给一个等于没给，下次重连会从头开始（于是重放窗口内的消息重复投递）。
   ``_connection_id`` 必须在 ``hello`` 里**覆盖**成新的 —— 服务端没命中队列时会
   重新发 ``hello`` 并换新的 id。

三处**刻意**的日志差异（不是行为变更）
------------------------------------
入站侧原本会区分「建连失败（地址 / TLS / 网络）」与「入站异常」两条 warning。迁移后：

* 「建连失败」仍由 :meth:`_open_socket` 用**同样的文案**打出来（它就发生在建连那一步）；
* 「入站异常」与「对端正常关闭」改由共享传输层统一打
  ``transport[mattermost]: 会话出错: …``（WARNING），随后照旧由
  :meth:`_on_close` → :meth:`_log_disconnect` 打出**平台级**诊断。

级别、异常文本、诊断信息都还在，变的只是前缀与归属的 logger。

协议细节均按官方 OpenAPI 与 ``mattermost/mathermost@master`` 源码核实，几处**反直觉**
的点写在下面，实现处都有对应注释：

1. **鉴权走握手 header**：``Authorization: Bearer <token>``（官方 Go 客户端
   ``model.NewWebSocketClient4`` 就是这么做的），**不是** query 参数。
2. **鉴权失败没有任何错误信息**：token 无效 / 权限不足时，服务端**直接关闭连接**，
   不返回错误 JSON。所以必须能区分"鉴权失败"与"网络断了"，否则用户只能对着一个
   无信息的异常发呆 —— 见 :meth:`MattermostAdapter._diagnose_disconnect`。
3. **``hello`` 是鉴权成功之后才发的**（``data.server_version`` + 26 字符
   ``data.connection_id``），不是"连上就有"。拿不到 ``hello`` 就等于没鉴权成功。
4. **保活由服务端 ping 驱动**：服务器每 60 秒发一个 RFC 6455 **ping 控制帧**，
   100 秒内没收到 pong 就主动断开。**读超时只由 pong 续期**，收普通数据消息不算。
   我们不自己发心跳，只依赖 :mod:`opencode_bridge.ws` 在收到 ping 时自动回 pong
   （``ws.py`` 的 ``_handle_control_frame``）；官方事件表里**没有** ``ping`` 事件，
   所以也**不能**去等它。读超时取 75 秒（落在 60 与 100 之间）：既能判死连接，
   又不会被正常的 60 秒 ping 周期误伤。
5. **两个信封字段不同，不能混**::

       事件   {"event": "...", "data": {...}, "broadcast": {...}, "seq": N}
       响应   {"status": "OK"|"FAIL", "seq_reply": N, "data"?: ..., "error"?: {...}}

   ``seq`` 由**服务端分配**（可靠重连要用）。判别方式：**有 ``status`` 的是响应**，
   否则才是事件。
6. **消息长度上限不能硬编码**：官方文档没给这个数，真实上限由服务端运行时从数据库
   列宽算出（``max(DB列字节数/4, 262144)``），随部署而异。网上流传的 "16383" 与
   "4000" 都不是稳定契约。因此类属性 :attr:`MattermostAdapter.max_message_length`
   只声明一个**保守的静态下限**（官方常量 ``PostMessageMaxRunesV1`` = 4000），
   启动后再用 ``GET /api/v4/config/client?format=old`` 的 ``MaxPostSize`` **细化**。

``conversation_id`` 前缀：已从 ``channel:`` 切到 ``mattermost:``（歧义前缀，靠划分回读）
------------------------------------------------------------------------
``channel`` 在 :data:`identity.LEGACY_PREFIXES` 里的值是 ``None`` ——
**歧义前缀**，被 slack / discord / mattermost **三家共用**，光看字符串**判不出**来源。
所以切前缀不像 telegram / matrix 那样只要配一次键迁移就完事：**旧键永远迁不了**
（:mod:`opencode_bridge.state` 对歧义前缀只捕获、不归一），必须靠"读取时回退"把历史
会话接上。

于是本适配器做三件事：产出统一的 ``mattermost:<channel_id>``
（:meth:`MattermostAdapter._conversation_id`）；声明
:attr:`MattermostAdapter.legacy_conversation_prefix` = ``channel:`` 并让
:meth:`MattermostAdapter._channel_id` 继续认旧前缀（只为让读侧知道那个旧前缀长什么样）；
声明 :attr:`MattermostAdapter.local_id_pattern` = :data:`MATTERMOST_LOCAL_ID_PATTERN`
—— **本平台自己的** local id 文法（26 位小写字母/数字）。

回退读发生在哪一步：:mod:`opencode_bridge.conversation_keys` 只在**发起查询的平台
就是 mattermost**、且那个 local id 符合**本文件**声明的文法时，才去试一次
``channel:<local>``。三家文法两两不相交，于是"这个 ``channel:`` 键归谁"是**能确定的
判断**而不是猜；推导见 :data:`MATTERMOST_LOCAL_ID_PATTERN`，判定入口见
:meth:`~opencode_bridge.adapters.base.Adapter.owns_local_id`。
"""

from __future__ import annotations

import json
import logging
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, List, Optional, Tuple

from ..config_coerce import coerce_bool
from ..hooks import Hooks, Inbound, MsgHandle, Outbound, SendError
from ..identity import format_id
from ..split import split_text
from ..transport import WebSocketTransport
from ._redactable_ids import redactable_id
from .base import Adapter, classify_http, register

logger = logging.getLogger("opencode_bridge.adapters.mattermost")

__all__ = ["MattermostAdapter", "MESSAGE_LIMIT"]

# ----------------------------------------------------------------------
# 常量（全部来自官方文档 / 源码，不要凭记忆改）
# ----------------------------------------------------------------------
#: 静态**下限**：官方常量 ``PostMessageMaxRunesV1``。真实上限见类属性
#: :attr:`MattermostAdapter.max_message_length` 的 docstring —— 启动后会用
#: ``MaxPostSize`` 细化，出站分片按细化后的值切。
MESSAGE_LIMIT = 4000

WS_PATH = "/api/v4/websocket"       # 末尾斜杠有无都行
USERS_ME_PATH = "api/v4/users/me"
CLIENT_CONFIG_PATH = "api/v4/config/client?format=old"
POSTS_PATH = "api/v4/posts"

#: REST 限流是 10 req/s per user，取 ~6.7/s 留出余量（超了会吃 429 + X-RateLimit-Reset）。
MIN_SEND_INTERVAL = 0.15
DEFAULT_SOCKET_TIMEOUT = 30.0
MAX_RETRY_AFTER = 60.0

#: 服务器每 60 秒 ping 一次、100 秒没等到 pong 就主动断开。读超时取中间值 75 秒：
#: 比 ping 周期长（不会误伤），比服务端的 100 秒短（我们先于服务端判死连接）。
WS_RECV_TIMEOUT = 75.0

RECONNECT_MIN = 1.0                # 重连退避下限（秒）
RECONNECT_MAX = 60.0               # 重连退避上限（秒）

#: 服务端 ``SetReadLimit(8192)`` —— 客户端 → 服务端的每一帧都必须**小于**这个字节数。
WS_MAX_FRAME_BYTES = 8192
#: ``{"channel_id": ..., "message": ...}`` 这个 JSON 包装层的预留字节（留足余量）。
_BODY_OVERHEAD = 64
#: 一个码点在 UTF-8 里最多 4 字节（emoji / 增补平面字符）。JSON 序列化用
#: ``ensure_ascii=False``（配合已声明的 ``charset=utf-8``），所以 4 就是准确上界 ——
#: 若改成默认的 ``ensure_ascii=True``，一个 emoji 会变成 12 个 ASCII 字节（\uXXXX
#: 代理对），这里就得按 12 算。
_MAX_UTF8_BYTES_PER_CHAR = 4

#: 刚建连就断掉时，多快之内算"疑似鉴权失败"。Mattermost 鉴权失败是**立刻**关连接，
#: 而正常网络抖动不会这么快，所以这个窗口足够区分两者。
PRE_HELLO_GRACE = 10.0

#: 只处理 ``posted``：这一条就天然排除 ``post_edited`` / ``post_deleted`` /
#: ``typing`` / ``reaction_*`` / ``channel_*`` 等一切噪声事件。
POSTED_EVENT = "posted"

#: Mattermost local id 的文法：**恰好 26 位**的小写字母 / 数字。
#:
#: Mattermost 的所有实体 id 都由服务端 ``model.NewId()`` 生成 —— 一个固定 **26 位**
#: 的 base32 串（字母表 ``ybndrfg8ejkmcpqxot1uwisza345h769``，所以是小写 + 数字）。
#: 同一份生成本也用在校验 WebSocket ``hello`` 的连接 id 上（本文件按 26 字符读它）。
#:
#: ⚠️ **长度是承重的那一头**，必须恰好 26：26 位**纯数字**也要算 mattermost 的 ——
#: base32 字母表里本来就有数字，而 discord 的上限是 20 位（见
#: :data:`~opencode_bridge.adapters.discord.DISCORD_LOCAL_ID_PATTERN`），所以这一格
#: 不会和 discord 抢；slack 那格要求大写字母开头，也抢不到。
MATTERMOST_LOCAL_ID_PATTERN = re.compile(r"^[a-z0-9]{26}$")


def _classify_mm_error(status: int, data: Any) -> SendError:
    """把 HTTP 状态码 + Mattermost 的 ``{"message", "detailed_error"}`` 收敛成 7 类。

    Mattermost 的失败原因主要在**响应体**里（``status_code`` + ``message`` /
    ``detailed_error``），状态码只到"哪一类"；所以先看正文里的关键词，再用基类的
    :func:`~opencode_bridge.adapters.base.classify_http` 兜底（T1.3）。
    """
    detail = _error_detail(data)
    low = detail.lower()
    if status == 429 or "rate limit" in low:
        return SendError.RATE_LIMITED
    if "too long" in low or "too large" in low or "maximummessagesize" in low:
        return SendError.TOO_LONG
    if status in (401, 403):
        return SendError.FORBIDDEN
    if status == 404:
        return SendError.NOT_FOUND
    return classify_http(status, detail)


def _error_detail(data: Any) -> str:
    """从 Mattermost 的错误响应体里取一句可读的描述。"""
    if not isinstance(data, dict):
        return ""
    for key in ("detailed_error", "message", "error"):
        value = data.get(key)
        if value:
            return str(value)
    return ""


def _as_int(value: Any, default: int = 0) -> int:
    """宽松地取整数（Mattermost 部分字段在 JSON 里可能是字符串）。"""
    if isinstance(value, bool) or value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@register("mattermost")
class MattermostAdapter(Adapter):
    """Mattermost adapter：WebSocket 入站（``posted`` 事件）+ REST 出站。

    能力声明（T1.1）里的 ``max_message_length`` 是**静态下限**，不是精确上限 ——
    真实上限随部署而异（服务端运行时从 DB 列宽算出），启动后由
    :meth:`_apply_max_post_size` 用 ``GET /config/client`` 的 ``MaxPostSize`` 细化，
    出站分片按**运行时的有效值**切（见 :attr:`effective_max_length`）。

    ``_conversation_id`` 产出统一格式 ``mattermost:<channel_id>``（已从 ``channel:``
    切过来），``_channel_id`` 仍认旧前缀；读取时的归属划分见
    :mod:`opencode_bridge.conversation_keys`。
    """

    name = "mattermost"
    label = "Mattermost"
    #: **静态下限**（官方常量 ``PostMessageMaxRunesV1``）。运行时会被
    #: ``GET /api/v4/config/client?format=old`` 的 ``config.MaxPostSize`` 细化 ——
    #: 官方文档没公布这个上限，服务端是运行时算出来的，所以不能硬编码当成精确值。
    max_message_length = MESSAGE_LIMIT
    supports_inbound = True                    # WebSocket 事件流（T3.2）
    #: principal = channel id：(a) 会话唯一且稳定，(b) 用户能直接看到它，
    #: (c) Mattermost 对发件人做过认证。三条都成立。
    pairing_supported = True
    supports_inline_buttons = False            # v1 不发 attachments / actions
    supports_media = False                     # v1 只发纯文本
    #: ``PUT /posts/{id}/patch`` 真能改写已发消息（见 :meth:`edit`），占位气泡发得。
    supports_message_edit = True
    typed_command_prefix = "/"
    #: ``site_url`` 与 ``token`` 缺一不可（缺了 ``start()`` 只告警不起线程）。
    required_tokens = ("site_url", "token")
    # REST 出站同样要这两个键（没有 bot_token 概念）
    outbound_tokens = ("site_url", "token")

    # -- 类级旋钮（测试可在实例上覆盖）----------------------------------
    # 分片阈值不在这里声明：基类的 ``message_limit`` 槽默认 ``0``（= 静态下限），
    # ``start()`` 后由 :meth:`_apply_max_post_size` 写入服务端 ``MaxPostSize``。
    min_interval = MIN_SEND_INTERVAL
    #: 迁移前 Mattermost 用的 ``conversation_id`` 前缀（**歧义**：三家共用）。
    #: 值刻意写**字面量**，不用任何常量拼 —— 拼了就跟"防走偏断言"一样会恒真。
    legacy_conversation_prefix = "channel:"
    #: 本平台的 local id 文法（26 位小写字母/数字，见
    #: :data:`MATTERMOST_LOCAL_ID_PATTERN` 的推导）。判定入口是基类的
    #: :meth:`~opencode_bridge.adapters.base.Adapter.owns_local_id`。
    local_id_pattern = MATTERMOST_LOCAL_ID_PATTERN
    #: ``conversation_id`` 的合法前缀：当前格式 + 旧别名。
    #: :meth:`_channel_id` 按这个列表剥前缀，所以**旧前缀必须继续认** ——
    #: 写前收件箱把 ``conversation_id`` 持久化在盘上，切换前写入、切换后才重放的
    #: 未投递消息带着 ``channel:``，认不出来就等于把那些回复永久丢弃。
    _CONVERSATION_PREFIXES = ("mattermost:", legacy_conversation_prefix)

    def __init__(self, config: dict, hooks: Hooks) -> None:
        super().__init__(config, hooks)
        # site_url 与 server_url 是同一个意思，两个键都认（后者的名字更常见）。
        raw_site = self.config.get("site_url") or self.config.get("server_url") or ""
        self.site_url: str = str(raw_site).strip().rstrip("/")
        self.token: str = str(self.config.get("token") or "").strip()
        #: 可选：限定团队。v1 的收发都以 channel id 为准，team_id 只用于日志与将来
        #: 的团队级过滤，不参与路由（不在适配器里自建白名单，白名单走基类 admits()）。
        self.team_id: str = str(self.config.get("team_id") or "").strip()
        #: 自建服务器常用自签证书；默认校验证书链与主机名。
        self.verify_tls: bool = self._config_verify_tls()
        #: 自己的 user id（``GET /api/v4/users/me``），防回环的**唯一**判据。
        #: 也允许配置直接给（配置优先，避免每次启动多一次 REST 调用）。
        self.user_id: str = str(self.config.get("user_id") or "").strip()

        # --- WebSocket 入站状态 ----------------------------------------
        # 线程与连接都归传输层所有（``start()`` 之后才有；见「传输层接缝」一节）。
        self._transport: Optional[WebSocketTransport] = None
        self._ws_factory = None          # 测试注入点
        self._connection_id: Optional[str] = None   # hello 给的 26 字符连接 id
        self._server_version: Optional[str] = None
        self._last_seq: Optional[int] = None         # 最近一次事件的服务端 seq
        self._connected_at: float = 0.0              # 本次连接建立时刻（诊断用）
        self._got_hello: bool = False                # 本次连接是否收到过 hello

        # --- REST / 出站状态 --------------------------------------------
        self._last_headers: dict[str, str] = {}       # 最近一次响应头（取 X-RateLimit-Reset）
        self._throttle_lock = threading.Lock()
        self._last_send: dict[str, float] = {}

    def _config_verify_tls(self) -> bool:
        """读 ``verify_tls``（默认 ``True``）；非法值按 ``True`` 处理而不是静默降级。

        词表与"认不出来 ⇒ 回落 + 告警"这条纪律由
        :func:`~opencode_bridge.config_coerce.coerce_bool` 统一提供（它的认词表本来就是
        照本文件抄的，两边逐个取值同答）。⚠️ 与改前唯一的差别是**只含空白的串**：
        改前会告警，改后按"没配"静默处理 —— 取值两种情况都是 ``True``。

        ⚠️ **这个开关只管 REST 出站**（:meth:`_request` 里据此决定要不要
        ``ssl._create_unverified_context()``）。WebSocket 侧固定用
        ``ssl.create_default_context()``（不提供关校验的开关），所以**它关不掉 WS 的
        证书校验** —— 自签证书的部署仍然只能配受信任的证书。这条边界与迁移无关，
        迁移后依然成立。
        """
        return coerce_bool(self.config, "verify_tls", True, platform=self.name)

    # ------------------------------------------------------------------
    # URL 推导
    # ------------------------------------------------------------------
    def _scheme(self, ws: bool) -> str:
        """把配置里的 http/https（或 ws/wss）映射成对应 scheme。

        * WS 端点：``wss://`` ↔ ``https``、``ws://`` ↔ ``http``；
        * REST 端点：反过来（``ws://site`` 也能当 ``http://site`` 用）。
        """
        scheme = (urllib.parse.urlsplit(self.site_url).scheme or "").lower()
        if ws:
            if scheme in ("http", "ws"):
                return "ws"
            if scheme in ("https", "wss"):
                return "wss"
            return "wss"  # 没写 scheme 时按安全的 https/wss 猜
        if scheme in ("http", "ws"):
            return "http"
        return "https"

    def _rest_base(self) -> str:
        """REST 根地址（保留子路径部署的 base path，去掉尾斜杠）。"""
        parts = urllib.parse.urlsplit(self.site_url)
        return f"{self._scheme(False)}://{parts.netloc}{parts.path}".rstrip("/")

    def _ws_base_url(self) -> str:
        """WebSocket 端点（不带 query）。

        子路径部署（``https://host/mattermost``）要保留 base path，所以不能只取 netloc。
        """
        parts = urllib.parse.urlsplit(self.site_url)
        base_path = parts.path.rstrip("/")
        return f"{self._scheme(True)}://{parts.netloc}{base_path}{WS_PATH}"

    def _ws_url(self) -> str:
        """本次连接用的 URL —— 有可靠重连凭据时带上 ``connection_id`` + ``sequence_number``。

        Mattermost 端只有**两个参数都在**时才会去查服务端的事件队列补发；只给
        ``connection_id`` 不给 ``sequence_number`` 等于没给，所以这里要么两个都给，
        要么给一个全新连接（服务端会重新发 ``hello`` 并换新的 ``connection_id``）。
        """
        url = self._ws_base_url()
        if not self._connection_id or self._last_seq is None:
            return url
        query = urllib.parse.urlencode(
            {
                "connection_id": self._connection_id,
                "sequence_number": self._last_seq,
            }
        )
        return f"{url}?{query}"

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[dict] = None,
        *,
        timeout: Optional[float] = None,
    ) -> Tuple[int, dict]:
        """``{site_url}/{path}`` + ``Authorization: Bearer``。Never raises。

        ``payload is None`` 时不发 body（GET）。响应头存在 :attr:`_last_headers`
        里（429 的 ``X-RateLimit-Reset`` 在那里，不在响应体）。
        """
        url = f"{self._rest_base()}/{path.lstrip('/')}" if path else self._rest_base()
        body = (
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if payload is not None
            else None
        )
        req = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "opencode-bridge (mattermost, 1.0)",
            },
        )
        self._last_headers = {}
        kwargs: dict[str, Any] = {}
        if not self.verify_tls:
            # 自建服务器常见自签证书。⚠️ 只影响 REST —— ``ws.py`` 内部固定用
            # ``ssl.create_default_context()``（不提供关校验的开关），所以 WS 侧
            # 仍然校验证书；这里刻意不假装能覆盖 WS。
            kwargs["context"] = ssl._create_unverified_context()
        try:
            with urllib.request.urlopen(
                req,
                timeout=timeout if timeout is not None else DEFAULT_SOCKET_TIMEOUT,
                **kwargs,
            ) as resp:
                status = getattr(resp, "status", 200) or 200
                raw = resp.read()
                try:
                    self._last_headers = {k.lower(): v for k, v in resp.headers.items()}
                except Exception:  # noqa: BLE001 - 头拿不到不影响主流程
                    self._last_headers = {}
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                raw = exc.read()
            except Exception:  # noqa: BLE001
                raw = b""
            try:
                self._last_headers = {k.lower(): v for k, v in exc.headers.items()}
            except Exception:  # noqa: BLE001
                self._last_headers = {}
        except Exception as exc:  # noqa: BLE001 - 传输层失败映射为状态码 0
            logger.warning("mattermost: transport error on %s: %s", path, exc)
            return 0, {"message": f"transport error: {exc}"}
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:  # noqa: BLE001
            return status, {"message": f"non-JSON response (HTTP {status})"}
        if not isinstance(data, dict):
            return status, {"message": "unexpected payload"}
        return status, data

    def _retry_after_seconds(self) -> Optional[float]:
        """从 ``X-RateLimit-Reset`` 头算建议等待秒数（Mattermost 限流 10 req/s per user）。

        官方给的是 **Unix epoch 秒**；这里也容忍"剩余秒数"这种小值写法。
        """
        raw = self._last_headers.get("x-ratelimit-reset")
        try:
            reset = float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        if reset <= 0:
            return None
        delay = reset - time.time() if reset > 1_000_000_000 else reset
        return max(0.0, min(delay, MAX_RETRY_AFTER))

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
    # 启动期的两次 REST 探测（都在 try/except 里，失败只降级不抛）
    # ------------------------------------------------------------------
    def _load_self_user_id(self) -> None:
        """``GET /api/v4/users/me`` → ``["id"]``，缓存自己的 user id（防回环用）。"""
        if self.user_id:
            return  # 配置已给，不必再问
        status, data = self._request("GET", USERS_ME_PATH, None)
        if status < 200 or status >= 300:
            logger.warning(
                "mattermost: GET /users/me 失败 (HTTP %s)，无法确定自己的 user id", status
            )
            return
        found = str(data.get("id") or "").strip()
        if found:
            self.user_id = found
            logger.info("mattermost: 自己的 user id = %s", found)
        else:
            logger.warning("mattermost: GET /users/me 没返回 id")

    def _fetch_max_post_size(self) -> Optional[int]:
        """``GET /api/v4/config/client?format=old`` → ``config.MaxPostSize``。

        注意它返回的是**字符串**（必须 ``int()``）。取不到返回 ``None``（调用方回落到
        静态下限），不猜、不硬编码。
        """
        status, data = self._request("GET", CLIENT_CONFIG_PATH, None)
        if status < 200 or status >= 300:
            logger.info(
                "mattermost: 读取 MaxPostSize 失败 (HTTP %s)，沿用静态上限 %d",
                status,
                self.max_message_length,
            )
            return None
        config = data.get("config")
        raw = config.get("MaxPostSize") if isinstance(config, dict) else None
        try:
            value = int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            logger.info(
                "mattermost: MaxPostSize 不是合法整数（%r），沿用静态上限 %d",
                raw,
                self.max_message_length,
            )
            return None
        if value <= 0:
            return None
        return value

    def _apply_max_post_size(self, value: Optional[int]) -> int:
        """用运行时 ``MaxPostSize`` 细化分片阈值，返回生效值（取不到则保持静态下限）。

        比较对象是 :attr:`effective_max_length` 而不是裸槽：槽的初值是 ``0``（"未细化"），
        拿它比会在"服务端恰好等于静态下限"时多打一条没发生过的细化日志。
        """
        if value is None or value <= 0:
            self.message_limit = int(self.max_message_length)
            return self.effective_max_length
        if value != self.effective_max_length:
            logger.info(
                "mattermost: 消息上限由静态下限 %d 细化为服务端 MaxPostSize=%d",
                self.max_message_length,
                value,
            )
        self.message_limit = value
        return self.effective_max_length

    def _refresh_runtime_limits(self) -> None:
        """启动时补齐两样运行期事实。**任何失败都只降级，不抛**（``start()`` 不许抛）。"""
        try:
            self._load_self_user_id()
        except Exception as exc:  # noqa: BLE001
            logger.warning("mattermost: 取自己的 user id 失败: %s", exc)
        try:
            self._apply_max_post_size(self._fetch_max_post_size())
        except Exception as exc:  # noqa: BLE001
            logger.warning("mattermost: 细化消息上限失败，沿用静态下限: %s", exc)
            self._apply_max_post_size(None)

    # ------------------------------------------------------------------
    # 传输层接缝
    # ------------------------------------------------------------------
    @property
    def transport(self) -> Optional[WebSocketTransport]:
        """当前传输层（``start()`` 之后才有；连接由它持有，见 ``transport.connection``）。"""
        return self._transport

    @property
    def running(self) -> bool:
        """消费线程是否活着（代理到传输层）。

        ⚠️ 基类的 :attr:`Adapter._thread` 现在**恒为 None**（入站线程由传输层持有，
        名字是 ``transport:mattermost``）。``core.py`` 的 ``_adapter_for`` 靠前缀 /
        映射找适配器，不依赖线程匹配。
        """
        transport = self._transport
        return transport is not None and transport.running

    def _make_transport(self) -> WebSocketTransport:
        """构造本次运行用的传输层（测试注入点：退避接线值）。

        退避**逐字对齐迁移前** ``_inbound_loop`` 末尾那几行
        （``delay = RECONNECT_MIN`` 起、×2、封顶 ``RECONNECT_MAX``、连接稳定后重置）：

        * ``min_backoff=RECONNECT_MIN`` / ``max_backoff=RECONNECT_MAX`` —— 1s 起、
          ×2、封顶 60s，与迁移前一致；
        * ``reset_after=RECONNECT_MIN`` —— 迁移前的判定是
          ``self._connected_at and (monotonic() - _connected_at) >= RECONNECT_MIN``，
          而传输层把「``_open()`` 直接失败」也算成 ``lived = 0.0``；**只有传这个正数**
          才能让「压根没连上」与「连上了但没活够 1s」都走 ×2 增长，与迁移前逐项一致
          （传基类默认的 0 会让这两种情况永远只等下限）。

        ⚠️ 刻意读**模块全局**而不是类属性：迁移前就是运行时读全局
        ``RECONNECT_MIN`` / ``RECONNECT_MAX``，既有测试
        （``tests/test_mattermost.py`` 的 ``TestInboundLoop``）靠 monkeypatch
        那两个全局来缩短等待。
        """
        return WebSocketTransport(
            self._open_socket,
            on_message=self._handle_packet,
            on_close=self._on_close,
            name="mattermost",
            min_backoff=RECONNECT_MIN,
            max_backoff=RECONNECT_MAX,
            reset_after=RECONNECT_MIN,
        )

    def _open_socket(self) -> Any:
        """建一次会话：重置诊断状态 → 定 URL（可靠重连参数）→ 建 WS → 记日志。

        对应迁移前 ``_inbound_loop`` 开头那几行，逐字保留顺序、日志文本与那句
        「建连失败（地址 / TLS / 网络）」的 warning（迁移后入站侧异常改由共享
        传输层记 ``transport[...] 会话出错``，见模块 docstring）。
        """
        self._connected_at = 0.0
        self._got_hello = False
        try:
            url = self._ws_url()
            ws = self._make_ws(url)
        except Exception as exc:  # noqa: BLE001 - 交给传输层退避重试
            logger.warning("mattermost: 建连失败（地址 / TLS / 网络）: %s", exc)
            raise
        self._connected_at = time.monotonic()
        logger.info("mattermost: websocket 已连接 %s", self._redact(url))
        return ws

    def _on_event(self, item: Any) -> None:
        """传输层的 ``on_event`` 回调：**刻意是 no-op**。

        :class:`~opencode_bridge.transport.WebSocketTransport` 在每条原始帧到达时
        先调 ``on_message(conn, frame)``（本适配器接的是
        :meth:`_handle_packet`，信封判别 / seq 记账都在那里做完了）、**再**把同一帧
        交给 ``on_event``。这里必须什么都不做，否则同一条消息会被投递两次
        （core 会当成两条消息，bot 也会回两次）。
        保留这个空实现（而不是给 ``start()`` 传 ``lambda _: None``）是为了让
        「同一帧会被两个钩子看到」这件事在代码里是**显式可见**的。
        """

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """启动入站。缺 ``token`` / ``site_url`` 时只告警并返回（不抛异常）。"""
        if not self.token:
            logger.warning("mattermost: token missing; adapter not started")
            return
        if not self.site_url:
            logger.warning("mattermost: site_url missing; adapter not started")
            return
        self._stop_event.clear()
        self._refresh_runtime_limits()
        if self.team_id:
            logger.info("mattermost: team_id=%s", self.team_id)
        if not self.user_id:
            # 没有自己的 user id 就无法过滤自己的回声 —— 每发一条就会被当成新输入再回
            # 一条，无限回环。这种情况下宁可**不处理入站**，也不让它打转。
            logger.error(
                "mattermost: 拿不到自己的 user id（GET /users/me 失败），已暂停入站处理"
                "以避免自问自答回环；可在配置里显式给 user_id"
            )
        transport = self._make_transport()
        self._transport = transport
        transport.start(self._on_event)

    # ------------------------------------------------------------------
    # Inbound（WebSocket 事件流）
    # ------------------------------------------------------------------
    def _auth_headers(self) -> dict[str, str]:
        """握手 header —— Mattermost 的 WS 鉴权**只**走这里，不要塞进 query。"""
        return {"Authorization": f"Bearer {self.token}"}

    def _make_ws(self, url: str):
        """建 WS 连接；``_ws_factory`` 为测试注入点，生产走标准库实现（T2.0）。"""
        factory = self._ws_factory
        if factory is None:
            from ..ws import connect as factory  # 延迟导入：没开入站也不加载它
        return factory(url, timeout=WS_RECV_TIMEOUT, headers=self._auth_headers())

    def _on_close(self, conn: Any) -> None:
        """传输层的 ``on_close`` 钩子：会话结束 → 打**平台级**断开诊断。

        对应迁移前 ``_inbound_loop`` 的 ``finally`` 末尾那句 ``self._log_disconnect(ws)``。
        迁移前它紧跟在 ``ws.close()`` 之后，现在由基类保证在 ``_close_conn`` **之前**
        调用 —— 顺序不构成差异：:meth:`opencode_bridge.ws.WebSocketClient.close` 只发
        close 帧并关 socket，**不写** ``close_code``（那个字段只在**收到**对端 close 帧时
        才被填），所以关连接前后读到的 ``close_code`` 完全一样。

        ⚠️ 这里只在**确实建上过连接**时才会被调用（``_open()`` 直接失败时基类不调）。
        迁移前那次「压根没连上」的 ``_log_disconnect(None)`` 已被 ``_open_socket`` 里
        更准确的「建连失败」取代 —— 迁移前那两句在连不上时会同时打出来，其中
        「尚未收到 hello 就断开（可能是网络抖动）」对建连失败其实是**误报**。
        """
        self._log_disconnect(conn)

    # -- 断开诊断 ---------------------------------------------------------
    @staticmethod
    def _diagnose_disconnect(
        got_hello: bool, elapsed: float, close_code: object
    ) -> Tuple[int, str]:
        """区分"鉴权失败"与"网络断了"，返回 ``(日志级别, 文案)``。**纯函数，便于测试。**

        Mattermost 鉴权失败的表现是**服务端直接关闭连接、一个错误 JSON 都不给**，
        且 ``hello`` 只在鉴权成功之后才发。所以"刚建连就断、且从没收到 hello"就是
        鉴权失败的强信号，必须给出可执行的诊断而不是干巴巴一句"连接断开"。
        """
        code = f"（close code={close_code}）" if close_code is not None else ""
        if got_hello:
            return 30, f"服务端关闭了连接 {code}，将重连"
        if elapsed <= PRE_HELLO_GRACE:
            return (
                40,
                "连接在收到 hello 之前就断了，几乎可以确定是**鉴权失败**："
                "Mattermost 在 token 无效 / 权限不足 / WebSocket 未授权时不返回任何错误"
                f"JSON，直接关连接（close code={close_code}）。"
                "请检查 token 是否有效、是否有该服务器的访问权限，"
                "以及反向代理是否透传 Authorization 头",
            )
        return 30, f"尚未收到 hello 就断开（可能是网络抖动）{code}"

    def _log_disconnect(self, ws: object) -> None:
        elapsed = (
            time.monotonic() - self._connected_at if self._connected_at else float("inf")
        )
        level, text = self._diagnose_disconnect(
            self._got_hello, elapsed, getattr(ws, "close_code", None)
        )
        logger.log(level, "mattermost: %s", text)

    @staticmethod
    def _redact(url: str) -> str:
        """日志里不打印完整 URL（可靠重连时 query 里带 connection_id），只留主机 + 路径。

        附带覆盖掉"有人把 token 配到 URL 里"这种情况：query 一律不落日志。
        """
        parts = urllib.parse.urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}{parts.path}"
        return f"{base}?…" if parts.query else base

    # -- 收包 -------------------------------------------------------------
    def _handle_packet(self, ws, raw: str) -> bool:
        """处理一个 WebSocket 信封，返回"这条是不是 hello"。

        本方法是入站的**唯一**语义入口：迁移后它由传输层的 ``on_message`` 钩子
        （签名正好是 ``(conn, frame)``）逐帧调用，**没有**第二份实现。
        返回值只给测试与调试看 —— 传输层不解释它（更早的框架版本没有"要求重连"
        这类返回值，重连一律由"关连接 / 对端关闭"驱动）。

        **两个信封要分清**：事件是 ``{"event", "data", "broadcast", "seq"}``，
        响应是 ``{"status", "seq_reply", ...}``。判别方式就是**有没有 ``status`` 键**
        —— 响应不会有 ``event``，而 ``status`` 键只有响应才有。
        """
        try:
            packet = json.loads(raw)
        except Exception:  # noqa: BLE001
            logger.debug("mattermost: 非 JSON 帧，忽略")
            return False
        if not isinstance(packet, dict):
            return False
        if "status" in packet:
            self._handle_response(packet)
            return False

        seq = packet.get("seq")
        if isinstance(seq, int) and not isinstance(seq, bool):
            # seq 由**服务端分配**，可靠重连要拿它做 sequence_number —— 事件要先记
            # 再过滤：被丢弃的事件同样占用了 seq，不记会让重连重复投递。
            self._last_seq = seq

        event = str(packet.get("event") or "")
        if event == "hello":
            self._on_hello(packet)
            return True
        if event == "authentication_challenge":
            self._on_auth_challenge(ws, packet)
            return False
        if event != POSTED_EVENT:
            # post_edited / post_deleted / typing / reaction_* / channel_* 全在这里
            # 被排除，不需要再逐个列白名单。
            logger.debug("mattermost: 忽略 event=%s", event)
            return False
        self._handle_posted(packet.get("data"))
        return False

    def _handle_response(self, packet: dict) -> None:
        """响应信封：只是对我们请求的回复（v1 不经 WS 发业务请求），记一笔日志。"""
        data = packet.get("data")
        detail = ""
        if isinstance(data, dict):
            detail = _error_detail(data)
        error = packet.get("error")
        if isinstance(error, dict):
            detail = detail or _error_detail(error)
        status = packet.get("status")
        logger.debug(
            "mattermost: 收到响应信封 status=%s seq_reply=%s %s",
            status,
            packet.get("seq_reply"),
            detail,
        )

    def _on_hello(self, packet: dict) -> None:
        """``hello`` = **鉴权成功**的标志（不是"连上就有"）。缓存 connection_id 供可靠重连。"""
        self._got_hello = True
        data = packet.get("data")
        data = data if isinstance(data, dict) else {}
        connection_id = str(data.get("connection_id") or "").strip()
        server_version = str(data.get("server_version") or "").strip()
        if connection_id:
            # 可靠重连没命中服务端队列时，服务端会**重新**发 hello 并换新的
            # connection_id —— 这里必须覆盖成新的，否则下次重连拿着失效的 id。
            self._connection_id = connection_id
        self._server_version = server_version or None
        logger.info(
            "mattermost: hello（鉴权成功）connection_id=%s server_version=%s",
            connection_id or "?",
            server_version or "?",
        )

    def _on_auth_challenge(self, ws, packet: dict) -> None:
        """回 ``authentication_challenge``。

        正常走握手 header 鉴权时**不会**收到这个事件（它只在 cookie / query 鉴权的连接
        上出现）。这里保留一条防御性分支：真收到了就按官方格式回一个 challenge 响应，
        但绝不因此把 token 放进 query。
        """
        data = packet.get("data")
        challenge = str(data.get("challenge") or "") if isinstance(data, dict) else ""
        if not challenge:
            logger.warning("mattermost: authentication_challenge 没有 challenge 字段")
            return
        reply = json.dumps({"authentication_challenge_response": challenge})
        if len(reply.encode("utf-8")) >= WS_MAX_FRAME_BYTES:
            logger.error("mattermost: challenge 响应帧过大，拒发")
            return
        try:
            ws.send(reply)
            logger.info("mattermost: 已回应 authentication_challenge")
        except Exception as exc:  # noqa: BLE001
            logger.warning("mattermost: 回应 authentication_challenge 失败: %s", exc)

    # -- 事件过滤 ---------------------------------------------------------
    def _drop_inbound(self, reason: str, channel_id: str, author: str) -> None:
        """记一行"为什么丢"，两个 id 一律走 :func:`redactable_id`。

        ⚠️ mattermost 的 id 恰好 26 位小写字母数字，**与哈希片段无法区分**
        （C2 明写），所以按形状脱敏不可能 —— 补上前缀是唯一的路。脱敏后是
        ``mattermost:conv#<摘要>``，**同一条频道 / 同一个作者跨行仍可关联**。

        ⚠️ 本方法只改**记什么**：9 个调用点与每一个 ``return False`` 都原样未动。
        """
        logger.info(
            "mattermost: 丢弃消息（%s）channel=%s user=%s",
            reason,
            redactable_id(self.name, channel_id),
            redactable_id(self.name, author),
        )

    def _handle_posted(self, data: object) -> bool:
        """``posted`` → 过滤 → Inbound。返回是否真的放行了一条。

        ``data`` 里真正的字段名是 ``message``（正文，Markdown）/ ``id`` /
        ``channel_id`` / ``user_id`` / ``type`` / ``delete_at`` / ``root_id`` /
        ``create_at``。官方 OpenAPI 的 schema 比运行时 JSON 略旧，所以一律用
        ``.get()`` 兜底，不要假设键一定存在。
        """
        if not isinstance(data, dict):
            return False
        channel_id = str(data.get("channel_id") or "")
        author = str(data.get("user_id") or "")
        post_id = str(data.get("id") or "")
        text = str(data.get("message") or "")
        post_type = str(data.get("type") or "")

        # 0) 拿不到自己的 user id 就无法防回环 —— 整个入站停摆（见 start() 的告警）。
        if not self.user_id:
            self._drop_inbound("不知道自己的 user_id，无法防回环", channel_id, author)
            return False
        # 1) 防回环：**只用** user_id 比对。Post 对象上没有任何 bot 标志字段（已核实），
        #    所以不要去找"是不是 bot"—— 那会把别的 bot 的消息也全丢掉。
        if author == self.user_id:
            self._drop_inbound("自己发的", channel_id, author)
            return False
        # 2) 系统消息（type == "" 才是普通消息；system_* 全是系统消息）
        if post_type != "":
            self._drop_inbound(f"系统消息 type={post_type!r}", channel_id, author)
            return False
        # 3) 已软删除（delete_at != 0）
        if _as_int(data.get("delete_at")) != 0:
            self._drop_inbound("已软删除 delete_at!=0", channel_id, author)
            return False
        # 4) 空正文且无附件（纯附件消息的 message 是空串）
        if not text.strip() and not data.get("file_ids"):
            self._drop_inbound("空正文且无附件", channel_id, author)
            return False
        # 5) 缺 channel_id：拼不出 conversation_id
        if not channel_id:
            self._drop_inbound("缺 channel_id", channel_id, author)
            return False
        # 6) 授权闸门必须在产生 Inbound **之前**（否则能用命令 / 审批字绕过）。
        #    ⚠️ /pair 在未授权时也要能进来，所以 conversation_id 提到闸门之前算。
        conversation_id = self._conversation_id(channel_id)
        if not self.admits(channel_id) and not self.answer_pairing_request(
            channel_id, conversation_id, text
        ):
            self._drop_inbound("未在白名单", channel_id, author)
            return False
        try:
            self.hooks.on_inbound(
                Inbound(
                    conversation_id=conversation_id,
                    text=text,
                    kind="text",
                    user_id=author or None,
                    message_id=post_id or None,
                    platform=self.name,
                    raw=data,
                )
            )
        except Exception as exc:  # noqa: BLE001 - 上层炸了也不能让网关线程退出
            logger.exception("mattermost: on_inbound 失败: %s", exc)
            return False
        return True

    # ------------------------------------------------------------------
    # Lifecycle teardown
    # ------------------------------------------------------------------
    def stop(self) -> None:
        """置停止位 → **关 WS 唤醒阻塞的 ``recv()`` → join 线程**（**幂等**）。

        顺序不能反：传输层的 ``join`` 只等 5s，而 ``recv()`` 在
        :data:`WS_RECV_TIMEOUT`（75s）内可能一直阻塞；不先把连接关掉就会每次
        stop 都等满超时。迁移前这段是手写的（取 ``self._ws`` → ``close()`` →
        ``super().stop()``），现在由
        :meth:`~opencode_bridge.transport.Transport.stop` 统一保证
        （关连接那步还多了 ``shutdown`` 唤醒阻塞中的读），语义没变。
        """
        super().stop()                      # 置停止位（_throttle 依赖它）
        transport, self._transport = self._transport, None
        if transport is not None:
            transport.stop()

    # ------------------------------------------------------------------
    # Outbound（REST）
    # ------------------------------------------------------------------
    @staticmethod
    def _conversation_id(channel_id: Any) -> str:
        """``channel_id`` → ``mattermost:...``（统一 ``platform:local_id`` 格式）。

        ① 歧义前缀 **永不迁移**：:mod:`opencode_bridge.state` 对 ``channel:`` 只捕获、
        不归一（归属从未被持久化，推不出来），所以历史会话靠
        :mod:`opencode_bridge.conversation_keys` 的"各家 local id 文法不相交"
        在**读取时**接回来 —— 判据是 :attr:`MattermostAdapter.local_id_pattern`。
        ② 反向解析（:meth:`_channel_id`）必须继续认 ``channel:``：写前收件箱把
        ``conversation_id`` 持久化在盘上，切换前写入、切换后才重放的未投递消息带着
        旧前缀，认不出来就等于把那些回复永久丢弃。
        """
        return format_id("mattermost", channel_id)

    @staticmethod
    def _channel_id(conversation_id: Any) -> Optional[str]:
        """``conversation_id`` → Mattermost channel id；空的一律返回 ``None``。

        裸 channel id 也认，切换前后的两种前缀都认 ——
        理由见 :attr:`MattermostAdapter._CONVERSATION_PREFIXES`。
        """
        raw = str(conversation_id or "")
        for prefix in MattermostAdapter._CONVERSATION_PREFIXES:
            if raw.startswith(prefix):
                raw = raw[len(prefix):]
                break
        return raw or None

    def _post_body_bytes(self, channel_id: str, text: str) -> int:
        """一条出站消息序列化成 JSON 后的字节数（用来守住 8192 字节帧上限）。

        必须与 :meth:`_request` 用**完全相同**的序列化参数（``ensure_ascii=False``），
        否则量出来的不是真正上线的字节数。
        """
        return len(
            json.dumps(
                {"channel_id": channel_id, "message": text}, ensure_ascii=False
            ).encode("utf-8")
        )

    def _split_outbound(self, text: str, channel_id: str) -> List[str]:
        """按**运行时的有效上限**分片，并按 UTF-8 字节做一道帧尺寸兜底。

        ``prefix_fmt=""`` 与 telegram/slack/discord/matrix 保持一致：分段不额外加
        「（i/n）」，且 ``"".join(chunks) == 原文``。

        字节兜底的动机：分片阈值是**码点数**，而帧上限是**字节数** —— 一条 4000 字的
        CJK / emoji 正文序列化后可以是阈值的 3~4 倍。这里按最坏 4 字节/码点折一个更小
        的阈值，把超限的那一片再切一次。⚠️ 这是**自加的保守护栏**（REST 请求体本身
        没有 8192 的限制），代价是非 ASCII 正文会多分几片；换来的是任何情况下单次请求
        都不会大到把服务端读缓冲顶爆。
        """
        limit = self.effective_max_length
        chunks = split_text(text, limit, prefix_fmt="")
        if not chunks:
            return []
        budget = max(1, (WS_MAX_FRAME_BYTES - _BODY_OVERHEAD) // _MAX_UTF8_BYTES_PER_CHAR)
        out: List[str] = []
        for chunk in chunks:
            body_bytes = self._post_body_bytes(channel_id, chunk)
            if body_bytes < WS_MAX_FRAME_BYTES:
                out.append(chunk)
            else:
                logger.info(
                    "mattermost: 单片 %d 字节超过帧上限 %d，按字节预算再切一次",
                    body_bytes,
                    WS_MAX_FRAME_BYTES,
                )
                out.extend(split_text(chunk, budget, prefix_fmt=""))
        return out

    def _note_failure(self, status: int, data: Any, what: str) -> None:
        """把一次出站失败喂给基类的可观测通道（T1.3）。

        ``_note_send_failure`` 记下的分类会被基类 ``Adapter.send_result()`` 读出，
        产出结构化 ``SendResult``（``ok=False`` + ``error_kind`` / ``retry_after``）；
        分片发送中途失败且已有成功分片时，``send_result`` 会额外标 ``partial=True``。
        """
        detail = _error_detail(data) or f"HTTP {status}"
        logger.warning("mattermost: %s failed (HTTP %s): %s", what, status, detail)
        self._note_send_failure(
            _classify_mm_error(status, data),
            detail,
            retry_after=self._retry_after_seconds(),
        )

    def send(self, out: Outbound) -> MsgHandle | None:
        """``POST /api/v4/posts``（官方成功码是 **201**），超长自动分片。

        判定用"2xx **且**响应里有 ``id``"而不是死卡 201：有些部署前面挂的代理会把 201
        改写成 200，而响应体仍然是那条 post，硬卡状态码只会造成假失败。
        """
        channel = self._channel_id(out.conversation_id)
        if not channel:
            logger.warning("mattermost: bad conversation_id %r", out.conversation_id)
            self._note_send_failure(SendError.BAD_FORMAT, "bad conversation_id")
            return None
        if not out.text:
            logger.warning("mattermost: refusing to send empty text")
            self._note_send_failure(SendError.BAD_FORMAT, "empty text")
            return None
        if not self.token or not self.site_url:
            logger.warning("mattermost: token/site_url missing; send refused")
            self._note_send_failure(SendError.BAD_FORMAT, "token/site_url missing")
            return None
        chunks = self._split_outbound(out.text, channel)
        if len(chunks) > 1:
            logger.info(
                "mattermost: splitting outbound message into %d chunks (limit=%d)",
                len(chunks),
                self.effective_max_length,
            )
        handle: MsgHandle | None = None
        for chunk in chunks:
            self._throttle(out.conversation_id)
            status, data = self._request(
                "POST",
                POSTS_PATH,
                # channel_id 与 message **两个字段都必填**
                {"channel_id": channel, "message": chunk},
            )
            if status < 200 or status >= 300 or not data.get("id"):
                self._note_failure(status, data, "POST /posts")
                return handle if handle is not None else None
            handle = MsgHandle(
                conversation_id=out.conversation_id,
                message_id=str(data.get("id")),
                platform=self.name,
            )
        return handle

    def edit(self, handle: MsgHandle, out: Outbound) -> bool:
        """``PUT /api/v4/posts/{post_id}/patch`` —— **必须用 /patch**。

        ⚠️ **不要用** ``PUT /posts/{id}``：那是"整条替换"，会把请求里没列出的字段
        （props、file_ids 等）**清空**。编辑只用 ``/patch``（部分更新）。

        ⚠️ **超限就地拒收，抛** ``ValueError`` —— 与
        :meth:`~opencode_bridge.adapters.telegram.TelegramAdapter.edit` 与
        :meth:`~opencode_bridge.adapters.discord.DiscordAdapter.edit` 同一件事、
        同一句话术。Mattermost 在 ``server/channels/api4/post.go`` 里对
        ``updatePost`` 与 ``patchPost`` **都**调 :func:`rejectOversizedMessage`
        （``utf8.RuneCountInString(message) > MaxPostSize`` ⇒ HTTP **400**，
        ``model.post.is_valid.message_length.app_error``），而 ``model.Post.IsValid``
        里还有同一道检查 —— 所以它**拒收，不截断**。本地先判一道，省掉一次注定
        失败的往返与一次 :meth:`_throttle` 等待，也让日志说出真正的原因。

        **为什么不能退化成"发一条新消息"**：占位消息是**另外发出去的**那条消息，
        上面已经显示着一截正文；把整段再发一遍，读者就把那一截读了两遍
        （实测 4000 字读成 5500 字，见 ``tests/test_edit_length_guard.py``）。

        **这一条尤其该有**：:meth:`_apply_max_post_size` 会在 ``start()`` 之后用
        服务端的 ``MaxPostSize`` 改小 :attr:`message_limit`，而那个槽是**运行期**
        的 —— 若它落在"一次流式写入"与"一次收尾"之间，收尾用的预算当场变小，
        ``finalize`` 那个 ``max(len(head), len(shown_progress_text))`` 下界又把已显示
        的长度原样放回去，于是**一条超限的改写真的会被构造出来**。
        """
        if len(out.text) > self.effective_max_length:
            raise ValueError(
                f"mattermost edit text too long: {len(out.text)} > {self.effective_max_length}"
            )
        post_id = str(handle.message_id or "").strip()
        if not post_id or not self.token or not self.site_url:
            logger.warning("mattermost: bad handle %r", handle)
            self._note_send_failure(SendError.BAD_FORMAT, "bad handle")
            return False
        if not out.text:
            logger.warning("mattermost: refusing to edit with empty text")
            self._note_send_failure(SendError.BAD_FORMAT, "empty text")
            return False
        self._throttle(handle.conversation_id)
        status, data = self._request(
            "PUT", f"{POSTS_PATH}/{post_id}/patch", {"message": out.text}
        )
        if status < 200 or status >= 300:
            self._note_failure(status, data, "PUT /posts/{id}/patch")
            return False
        return True

    def answer(self, query_id: str, text: str = "") -> None:
        """Mattermost v1 没有需要应答的 callback query（按钮走 attachments/TODO）。"""
        return None

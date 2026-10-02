"""Lane B — Nextcloud Talk adapter (tasks.md T3.5). Standard library only.

传输层是纯 ``urllib.request``（零第三方依赖），全部调用收敛在单个可覆写的
:meth:`NextcloudAdapter._request` 后面，测试只需替换它即可离线验证协议层逻辑。
入站是 **HTTP 长轮询**（``lookIntoFuture=1`` 挂住最多 30 秒），出站是 REST ——
不是 WebSocket，所以本文件不碰 :mod:`opencode_bridge.ws`。

三个最容易踩、也最容易写错的点（下面每条都对应一处实现注释）
-----------------------------------------------------------

1. **必须带 ``OCS-APIRequest: true``，且必须是字面量小写 ``true````。服务端源码
   （``OCSController`` 的 CSRF 校验）是**严格字符串比较** ``=== 'true'`` ——
   ``True`` / ``TRUE`` / ``1`` / ``yes`` 全部被判成 CSRF 攻击并返回 **403**。
   同事必须带 ``Accept: application/json`` **和** ``?format=json``（两个都加最稳），
   否则返回 XML，而 XML 里的 ``reactions`` 结构不是良构 XML，极难解析。

2. **一律用 ``ocs/v2.php``，不要用 ``ocs/v1.php``。** v1 入口的 HTTP 状态码**恒为
   200**（连失败也是），失败信息全在响应体的 ``ocs.meta.statuscode`` 里，必须额外
   解析；v2 才返回真实状态码，可以直接按状态码分类。

3. **``urllib`` 对 304 会抛 ``HTTPError``。** ``urllib.request.HTTPErrorProcessor``
   只把 2xx 当成功，所以长轮询"无新消息"时服务端回的 **304** 是以**异常**形式到达的。
   不显式接住就会让整条入站循环反复报错。接住之后按"无新消息"处理，游标不变。

另外：本地 socket 超时必须**大于**服务端的 ``timeout`` 参数（服务端 30 → 本地 40），
否则本地先断，长轮询就白挂了。

已知限制（v1 刻意不做）
----------------------
* **不做 ``@提及`` 渲染**：``message`` 字段是含 ``{mention-call1}`` 之类占位符的
  **模板串**，不是纯文本。要渲染得先调 mentions 端点把 ``@昵称`` 换成像
  ``@‹user-id›`` 的 mentionId 串，属另一套协议，超出 v1 范围。因此命令需要用户
  发全名或直接发原始占位符。
* **不支持媒体**：v1 只发纯文本。
* **只发出站纯文本，不发 reactions / 分享文件 / 投票。**
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..hooks import Hooks, Inbound, MsgHandle, Outbound, SendError
from ..split import split_text
from .base import Adapter, classify_http, register

logger = logging.getLogger("opencode_bridge.adapters.nextcloud")

__all__ = ["NextcloudAdapter", "MESSAGE_LIMIT", "PERM_CHAT", "OCS_APIREQUEST_VALUE"]

# ----------------------------------------------------------------------
# 常量（全部来自官方文档 / nextcloud-server + spreed 源码，不要凭记忆改）
# ----------------------------------------------------------------------
#: **源码硬编码常量** ``ChatManager::MAX_CHAT_LENGTH`` —— 不是配置项、不可改。
#: 运行时可以用 ``/cloud/capabilities`` 的 ``spreed.config.chat.max-length`` 交叉校验
#: （见 ``_apply_max_chat_length``），读不到就回落本值。
MESSAGE_LIMIT = 32000

#: OCS 前端控制器。**只**用 v2：v1 的状态码恒为 200，失败只能从响应体里挖。
OCS_V2 = "ocs/v2.php"
#: 会话列表走 app API **v4**（v1/v2/v3 该端点 404）；聊天记录走 **v1**。
ROOMS_PATH = "apps/spreed/api/v4/room"
CHAT_PATH = "apps/spreed/api/v1/chat"
USER_PATH = "cloud/user"
CAPABILITIES_PATH = "cloud/capabilities"

#: ``OCS-APIRequest`` 头的值。**必须是小写字面量 ``"true"``**（服务端 ``=== 'true'``）。
OCS_APIREQUEST_VALUE = "true"

#: Room ``permissions`` 位掩码里"可以发聊天消息"的那一位（``PERM_CHAT``）。
PERM_CHAT = 128
#: ``permissions & 128`` 为真时才能发言；否则 ``readOnly`` → 403、
#: ``lobbyState`` → 412（前提是本会话还不是 lobby 的 moderator）。
PERM_DEFAULT = 384

CHAT_PAGE_LIMIT = 100            # 长轮询每轮最多带回多少条
BOOTSTRAP_LIMIT = 1              # bootstrap 只要"当前最新那条"当游标

#: 服务端长轮询上限 30 秒，配置**不许超过**它。
DEFAULT_POLL_TIMEOUT = 30
MAX_POLL_TIMEOUT = 30
#: 本地 socket 超时要比服务端 timeout **大**，否则本地先断、长轮询白挂。
LOCAL_SOCKET_SLACK = 10.0
DEFAULT_SOCKET_TIMEOUT = 30.0

#: 同时最多在飞几个长轮询请求（= worker 数）。每个请求会占住一个 worker 30 秒，
#: 会话多时必须设上限；绝不能"每个会话一个线程"。
DEFAULT_MAX_CONCURRENT_POLLS = 5
MAX_CONCURRENT_POLLS_CEILING = 32

#: 官方要求**每 5 分钟做一次不带 modifiedSince 的全量刷新**（原因见
#: :meth:`NextcloudAdapter._refresh_rooms` 的 docstring）。
DEFAULT_FULL_REFRESH = 300.0

#: 一个轮次结束到下一轮开始之间的歇脚（秒）。长轮询本身会挂 30 秒，这里只是兜底。
POLL_IDLE_WAIT = 0.5
BACKOFF_MIN = 1.0                # 出站失败后的退避下限
BACKOFF_MAX = 30.0

#: ``messageType`` 里"普通用户发言"的取值。其余是 system / command / comment_deleted …
MESSAGE_TYPE_COMMENT = "comment"
#: ``actorType`` 里代表真人用户的取值（其它有 guests / federated_users / bots …）。
ACTOR_TYPE_USER = "users"
#: ``messageParameters`` 里代表"分享了一份东西"的参数名。
#:
#: ⚠️ 这里有个**源码级的反直觉点**：``file_shared`` / ``object_shared`` 这类系统消息
#: 会被服务端**改写成** ``messageType="comment"`` **且** ``systemMessage=""`` —— 所以
#: "只看 messageType / systemMessage"是**过滤不干净**的，必须额外查 messageParameters。
SHARED_PARAM_NAMES = frozenset({"file", "object"})


def _as_int(value: Any, default: int = 0) -> int:
    """宽松地取整数（OCS 里部分字段在 JSON 中可能是字符串）。"""
    if isinstance(value, bool) or value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _header_int(headers: Dict[str, str], name: str, default: int = 0) -> int:
    """从响应头里取整数（``X-Chat-Last-Given``）。取不到返回 ``default``。"""
    return _as_int((headers or {}).get(name), default)


def _headers_of(raw: Any) -> Dict[str, str]:
    """把 ``email.message`` / dict 统一成 ``{小写名: 值}``。"""
    out: Dict[str, str] = {}
    if not raw:
        return out
    try:
        items = raw.items()
    except AttributeError:
        return out
    for key, value in items:
        out[str(key).lower()] = str(value)
    return out


def _ocs_data(body: Any) -> Tuple[int, Any]:
    """拆 OCS 信封，返回 ``(statuscode, data)``。

    v2 的 HTTP 状态码通常已经够用，但 body 里仍然有 ``ocs.meta.statuscode``，
    两者**都**看一眼更稳（代理改写状态码时不会把我们带沟里）。
    """
    if not isinstance(body, dict):
        return 0, None
    ocs = body.get("ocs")
    if not isinstance(ocs, dict):
        return 0, None
    meta = ocs.get("meta")
    meta = meta if isinstance(meta, dict) else {}
    return _as_int(meta.get("statuscode")), ocs.get("data")


def _error_detail(body: Any) -> str:
    """从 OCS 错误响应里取一句可读的描述（Nextcloud 常见的几种形态都兜住）。"""
    if not isinstance(body, dict):
        return ""
    ocs = body.get("ocs")
    data = ocs.get("data") if isinstance(ocs, dict) else None
    if isinstance(data, dict):
        for key in ("error", "message"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
            # 形如 {"error": {"type": "x", "message": "..."}} 或 {"message": {...}}
            if isinstance(value, dict):
                inner = value.get("message")
                if inner:
                    return str(inner)
    meta = ocs.get("meta") if isinstance(ocs, dict) else None
    if isinstance(meta, dict) and meta.get("message"):
        return str(meta.get("message"))
    return ""


def _classify_nc_error(status: int, body: Any) -> SendError:
    """HTTP 状态码 + OCS 响应体 → 7 类平台中立分类（T1.3）。

    Nextcloud 的失败原因主要在**响应体**里（``ocs.data.error`` 可以是字符串也可以是
    对象），所以先看正文里的关键词；同时**以 ``ocs.meta.statuscode`` 为准**，兜住
    "HTTP 2xx 但业务失败"那种形态（代理改写状态码时不会把我们带沟里）。
    """
    detail = _error_detail(body)
    low = detail.lower()
    ocs_code, _ = _ocs_data(body)
    # 业务状态码比 HTTP 状态码更可信：它才真的表达 OCS 层的成败。
    effective = (
        ocs_code if isinstance(ocs_code, int) and 400 <= ocs_code <= 599 else status
    )
    if effective == 429 or "too many requests" in low:
        return SendError.RATE_LIMITED
    if effective == 413 or "too long" in low or "too large" in low:
        return SendError.TOO_LONG
    if low == "age" or "too old" in low:
        # Talk：400 {"error": "age"} = 超过 24 小时不能编辑。
        # 刻意归到 BAD_FORMAT 而不是 TOO_LONG —— 是"这条消息太老"，不是"内容太长"；
        # 混淆这两者会让调用方误以为"再切短一点就能发"。
        return SendError.BAD_FORMAT
    if effective in (401, 403) or "forbidden" in low or "not allowed" in low:
        return SendError.FORBIDDEN
    if effective == 404 or "not found" in low:
        return SendError.NOT_FOUND
    return classify_http(status, detail)


@dataclass
class _Room:
    """一个 Talk 会话的本地状态。"""

    token: str
    #: 长轮询游标（``lastKnownMessageId``）。bootstrap 之后 = 当前最新那条消息 id。
    cursor: int = 0
    #: ``readOnly == 1``
    read_only: bool = False
    #: ``lobbyState == 1``
    lobby: bool = False
    #: 能不能发言（由 ``permissions & 128`` / readOnly / lobby 三者共同决定）。
    can_send: bool = False
    #: 游标是否已经 bootstrap 过（否则先 bootstrap 再进长轮询）。
    bootstrapped: bool = False


@dataclass(frozen=True)
class _Resp:
    """一次 HTTP 调用的结果：``(status, body, headers)``。

    ``status == 0`` 表示传输层失败（没拿到状态码）。``status == 304`` 是长轮询
    "无新消息"的正常结果，**不是**失败。
    """

    status: int
    data: Dict[str, Any] = field(default_factory=dict)
    headers: Dict[str, str] = field(default_factory=dict)


@register("nextcloud")
class NextcloudAdapter(Adapter):
    """Nextcloud Talk adapter：长轮询入站 + REST 出站。"""

    name = "nextcloud"
    label = "Nextcloud Talk"
    #: **源码硬编码常量** ``ChatManager::MAX_CHAT_LENGTH``，**不是配置项**。
    #: 启动后由 ``GET /cloud/capabilities`` 的 ``spreed.config.chat.max-length``
    #: 交叉校验 / 细化（读不到就沿用本值），出站分片按运行时的有效值切。
    max_message_length = MESSAGE_LIMIT
    supports_inbound = True                     # lookIntoFuture=1 长轮询
    supports_inline_buttons = False             # v1 不发 reactions / 卡片
    supports_media = False                      # v1 只发纯文本
    typed_command_prefix = "/"
    required_tokens = ("base_url", "username", "password")
    outbound_tokens = ("base_url", "username", "password")

    # -- 类级旋钮（测试可在实例上覆盖）----------------------------------
    #: 分片阈值（运行时的有效上限）：初值 = 源码常量，``start()`` 后可能被 capabilities 细化。
    message_limit = MESSAGE_LIMIT
    min_interval = 0.0                         # Talk 没有 Matrix/Slack 那种会话级节流

    def __init__(self, config: dict, hooks: Hooks) -> None:
        super().__init__(config, hooks)
        # base_url **可能含子路径前缀**（``https://host/nextcloud``），原样用。
        self.base_url: str = str(self.config.get("base_url") or "").strip().rstrip("/")
        self.username: str = str(self.config.get("username") or "").strip()
        #: Nextcloud「设置 → 安全 → 设备专属密码」生成的 app password（可单独吊销）。
        self.password: str = str(self.config.get("password") or "").strip()
        #: 自己的 uid。配置给了就跳过 ``/cloud/user``（省一次 REST）。
        self.user_id: str = str(self.config.get("user_id") or "").strip()
        self.max_concurrent_polls: int = self._config_int_value(
            "max_concurrent_polls", DEFAULT_MAX_CONCURRENT_POLLS,
            1, MAX_CONCURRENT_POLLS_CEILING,
        )
        self.poll_timeout: int = self._config_int_value(
            "poll_timeout", DEFAULT_POLL_TIMEOUT, 1, MAX_POLL_TIMEOUT
        )
        self.full_refresh_seconds: float = self._config_float(
            "full_refresh_seconds", DEFAULT_FULL_REFRESH, 30.0, 86400.0
        )

        # --- 入站状态 ---------------------------------------------------
        self.rooms: dict[str, _Room] = {}
        self._poll_order: List[str] = []
        self._poll_lock = threading.Lock()
        self._identity_ready = False
        self._last_modified_since: int = 0
        self._workers: List[threading.Thread] = []
        self._active_responses: List[Any] = []
        self._resp_lock = threading.Lock()
        self._local_timeout = float(self.poll_timeout) + LOCAL_SOCKET_SLACK
        #: 单调时钟注入点（测试用它推进时间，不必真等 5 分钟）。
        self._clock = time.monotonic

    # ------------------------------------------------------------------
    # 配置解析
    # ------------------------------------------------------------------
    def _config_int_value(self, key: str, default: int, low: int, high: int) -> int:
        raw = self.config.get(key)
        if raw in (None, ""):
            return default
        try:
            value = int(raw)
        except (TypeError, ValueError):
            logger.warning("nextcloud: %s 配置非法 %r，按 %s 处理", key, raw, default)
            return default
        if value < low or value > high:
            # 越界一律回落到默认，**不静默采纳**（例如 poll_timeout > 30 服务端会直接拒）
            logger.warning(
                "nextcloud: %s=%s 超出允许区间 [%s, %s]，按 %s 处理",
                key, value, low, high, default,
            )
            return default
        return value

    def _config_float(self, key: str, default: float, low: float, high: float) -> float:
        raw = self.config.get(key)
        if raw in (None, ""):
            return default
        try:
            value = float(raw)
        except (TypeError, ValueError):
            logger.warning(
                "nextcloud: %s 配置非法 %r，按 %s 处理", key, raw, default
            )
            return default
        if value < low or value > high:
            logger.warning(
                "nextcloud: %s=%s 超出合理区间 [%s, %s]，按 %s 处理",
                key, value, low, high, default,
            )
            return default
        return value

    # ------------------------------------------------------------------
    # URL / 鉴权
    # ------------------------------------------------------------------
    def _auth_header(self) -> str:
        """HTTP Basic：``Authorization: Basic base64(user:app_password)``。"""
        raw = f"{self.username}:{self.password}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")

    def _ocs_headers(self, *, form: Optional[dict] = None) -> Dict[str, str]:
        """每个请求都必须带的头。

        ⚠️ ``OCS-APIRequest`` 的值是**小写字面量** ``"true"``（服务端 ``=== 'true'``），
        写成 Python 的 ``True`` 会被判成 CSRF 攻击 → 403。
        """
        headers = {
            "Authorization": self._auth_header(),
            "OCS-APIRequest": OCS_APIREQUEST_VALUE,
            "Accept": "application/json",
            "User-Agent": "opencode-bridge (nextcloud, 1.0)",
        }
        if form is not None:
            # 发消息的 body 服务端是**显式按 urlencoded 解析**的
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        return headers

    def _url(self, path: str, params: Optional[dict] = None) -> str:
        """``{base_url}/ocs/v2.php/{path}?format=json&…``。

        ``format=json`` 与 ``Accept: application/json`` **两个都加**：前者是 OCS 的
        显式请求，后者兜底，少任何一个都可能拿到 XML。
        """
        base = f"{self.base_url}/{OCS_V2}/{str(path).lstrip('/')}"
        query: Dict[str, Any] = {"format": "json"}
        if params:
            for key, value in params.items():
                if value is None:
                    continue
                query[key] = value
        return f"{base}?{urllib.parse.urlencode(query)}"

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------
    def _build_request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        form: Optional[dict] = None,
    ) -> urllib.request.Request:
        """组装 ``Request``（**不发送**）。抽出来是为了让"请求头对不对"可离线断言。"""
        url = self._url(path, params)
        body = None
        if form is not None:
            # 服务端**显式按 urlencoded 解析** body，所以这里不能用 JSON。
            body = urllib.parse.urlencode(form).encode("utf-8")
        return urllib.request.Request(
            url, data=body, method=method, headers=self._ocs_headers(form=form)
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        form: Optional[dict] = None,
        timeout: Optional[float] = None,
    ) -> _Resp:
        """一次 OCS 调用。**Never raises**，结果一律包成 :class:`_Resp`。

        ``timeout`` 缺省用 :attr:`_local_timeout`（= 服务端 poll_timeout + 10s）——
        本地 socket 超时必须大于服务端 timeout，否则本地先断、长轮询白挂。
        """
        req = self._build_request(method, path, params=params, form=form)
        sock_timeout = timeout if timeout is not None else self._local_timeout
        try:
            with urllib.request.urlopen(req, timeout=sock_timeout) as resp:
                self._track_response(resp)
                try:
                    status = getattr(resp, "status", 200) or 200
                    raw = resp.read()
                    headers = _headers_of(getattr(resp, "headers", None))
                finally:
                    self._untrack_response(resp)
        except urllib.error.HTTPError as exc:
            status = getattr(exc, "code", 0)
            headers = _headers_of(getattr(exc, "headers", None))
            if status == 304:
                # ⚠️ urllib 只把 2xx 当成功（``HTTPErrorProcessor``），所以长轮询
                # "无新消息"的 304 是**以异常形式**到达的。必须接住并当成正常结果，
                # 否则整条入站循环会一直报错。
                logger.debug("nextcloud: 长轮询无新消息（HTTP 304）")
                return _Resp(304, {}, headers)
            try:
                raw = exc.read()
            except Exception:  # noqa: BLE001
                raw = b""
        except Exception as exc:  # noqa: BLE001 - 传输层失败映射为状态码 0
            logger.warning("nextcloud: transport error on %s %s: %s", method, path, exc)
            return _Resp(0, {"message": f"transport error: {exc}"}, {})

        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:  # noqa: BLE001
            return _Resp(status, {"message": f"non-JSON response (HTTP {status})"}, headers)
        if not isinstance(data, dict):
            return _Resp(status, {"message": "unexpected payload"}, headers)
        return _Resp(status, data, headers)

    # -- 供 stop() 尽力打断阻塞中的长轮询 --------------------------------
    def _track_response(self, resp: Any) -> None:
        with self._resp_lock:
            self._active_responses.append(resp)

    def _untrack_response(self, resp: Any) -> None:
        with self._resp_lock:
            try:
                self._active_responses.remove(resp)
            except ValueError:
                pass

    def _close_active_responses(self) -> None:
        """把在飞的长轮询响应尽量关掉，好让 worker 尽快从阻塞里出来。

        ⚠️ **尽力而为**：关一个正卡在 ``read()`` 上的 socket 不保证在所有平台上都能
        立刻唤醒那次读（Linux 上尤其如此）。所以真正保证及时退出的是"本地 socket
        超时 = 服务端 timeout + 10s"这条兜底，而不是这里。
        """
        with self._resp_lock:
            active = list(self._active_responses)
            self._active_responses.clear()
        for resp in active:
            try:
                resp.close()
            except Exception:  # noqa: BLE001 - 关不掉就算了
                pass

    # ------------------------------------------------------------------
    # 启动期探测（都在 try/except 里，失败只降级不抛）
    # ------------------------------------------------------------------
    def _load_self_uid(self) -> None:
        """``GET /cloud/user`` → ``ocs.data.id``（**大小写敏感，绝不 lower()**）。

        uid 是防回环的**唯一**依据；取不到就整个入站停摆（见 :meth:`_handle_message`）。
        """
        if self.user_id:
            self._identity_ready = True
            return
        resp = self._request("GET", USER_PATH, timeout=DEFAULT_SOCKET_TIMEOUT)
        code, data = _ocs_data(resp.data)
        ok = resp.status in (200, 201) and (code in (0, 200))
        uid = str(data.get("id") or "").strip() if isinstance(data, dict) else ""
        if ok and uid:
            self.user_id = uid
            self._identity_ready = True
            logger.info("nextcloud: 自己的 uid = %s", uid)
            return
        logger.warning(
            "nextcloud: 取自己的 uid 失败 (HTTP %s / ocs %s)：%s",
            resp.status, code, _error_detail(resp.data) or "无 id 字段",
        )

    def _fetch_max_chat_length(self) -> Optional[int]:
        """``GET /cloud/capabilities`` → ``spreed.config.chat.max-length``（交叉校验）。

        32000 是服务端**源码常量**、不是配置项；这里只是拿部署侧的配置值校验一遍，
        读不到返回 ``None`` 由调用方回落到常量。
        """
        resp = self._request("GET", CAPABILITIES_PATH, timeout=DEFAULT_SOCKET_TIMEOUT)
        if resp.status < 200 or resp.status >= 300:
            logger.info(
                "nextcloud: 读 capabilities 失败 (HTTP %s)，沿用源码常量 %d",
                resp.status, MESSAGE_LIMIT,
            )
            return None
        _, data = _ocs_data(resp.data)
        chat = (((data or {}).get("capabilities") or {}).get("spreed") or {})
        chat = (chat.get("config") or {}).get("chat") if isinstance(chat, dict) else None
        raw = chat.get("max-length") if isinstance(chat, dict) else None
        value = _as_int(raw, 0)
        if value <= 0:
            logger.info(
                "nextcloud: capabilities 没给可用的 chat.max-length（%r），沿用常量 %d",
                raw, MESSAGE_LIMIT,
            )
            return None
        return value

    def _apply_max_chat_length(self, value: Optional[int]) -> int:
        """用部署侧的 ``max-length`` 细化分片阈值；取不到就沿用源码常量。"""
        if value is None or value <= 0:
            self.message_limit = int(self.max_message_length)
            return self.message_limit
        if value != self.message_limit:
            logger.info(
                "nextcloud: 消息上限由源码常量 %d 细化为部署值 %d",
                self.max_message_length, value,
            )
        self.message_limit = value
        return self.message_limit

    @property
    def effective_max_length(self) -> int:
        """**运行时生效**的出站分片阈值。"""
        limit = _as_int(getattr(self, "message_limit", 0), int(self.max_message_length))
        return limit if limit > 0 else int(self.max_message_length)

    def _refresh_runtime_facts(self) -> None:
        """启动时补齐运行期事实。**任何失败都只降级，不抛**（``start()`` 不许抛）。"""
        try:
            self._load_self_uid()
        except Exception as exc:  # noqa: BLE001
            logger.warning("nextcloud: 取自己的 uid 失败: %s", exc)
        try:
            self._apply_max_chat_length(self._fetch_max_chat_length())
        except Exception as exc:  # noqa: BLE001
            logger.warning("nextcloud: 校验消息上限失败，沿用源码常量: %s", exc)
            self._apply_max_chat_length(None)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        """启动入站。缺 ``base_url`` / ``username`` / ``password`` 时只告警并返回。"""
        if not self.base_url:
            logger.warning("nextcloud: base_url missing; adapter not started")
            return
        if not self.username:
            logger.warning("nextcloud: username missing; adapter not started")
            return
        if not self.password:
            logger.warning(
                "nextcloud: password missing; adapter not started"
                "（应填「设置 → 安全 → 设备专属密码」生成的 app password）"
            )
            return
        self._stop_event.clear()
        self._refresh_runtime_facts()
        if not self.user_id:
            # 拿不到 uid 就没法过滤自己的回声 —— 每发一条回复都会被当成新输入再触发一条。
            # 这种情况下宁可**不处理入站**，也不让它打转。
            logger.error(
                "nextcloud: 拿不到自己的 uid（GET /cloud/user 失败），已暂停入站处理"
                "以避免自问自答回环；可在配置里显式给 user_id"
            )
        thread = threading.Thread(
            target=self._coordinator_loop, name="nextcloud-coordinator", daemon=True
        )
        self._thread = thread
        thread.start()
        logger.info(
            "nextcloud: coordinator started (rooms=%d, max_concurrent_polls=%d, "
            "poll_timeout=%ds, full_refresh=%.0fs)",
            len(self.rooms), self.max_concurrent_polls,
            self.poll_timeout, self.full_refresh_seconds,
        )

    def _coordinator_loop(self) -> None:
        """协调线程：定期刷新会话表 + 按需拉起 worker。**任何异常都不许逃出线程。"""
        self._bootstrap_workers()
        last_full: Optional[float] = None
        while not self._stop_event.is_set():
            try:
                now = self._clock()
                if last_full is None or (now - last_full) >= self.full_refresh_seconds:
                    self._refresh_rooms(full=True)
                    last_full = now
                else:
                    # 增量刷新增量更新；发现不了"会话被删 / 我被移出"（官方明确说明），
                    # 所以那条路必须靠上面的周期性全量刷新。
                    self._refresh_rooms(full=False)
            except Exception:  # noqa: BLE001
                logger.exception("nextcloud: 刷新会话列表失败")
            if self._stop_event.wait(POLL_IDLE_WAIT):
                break
        self._stop_workers()

    def _bootstrap_workers(self) -> None:
        """按 :attr:`max_concurrent_polls` 拉起固定数量的 worker（**不是每会话一个**）。"""
        count = max(1, int(self.max_concurrent_polls))
        for index in range(count):
            thread = threading.Thread(
                target=self._worker_loop, name=f"nextcloud-poll-{index}", daemon=True
            )
            self._workers.append(thread)
            thread.start()
        logger.info("nextcloud: %d 个轮询 worker 已启动", count)

    def _stop_workers(self) -> None:
        current = threading.current_thread()
        for thread in self._workers:
            if thread.is_alive() and thread is not current:
                thread.join(timeout=2.0)
        self._workers = []

    # ------------------------------------------------------------------
    # 会话表
    # ------------------------------------------------------------------
    def _fetch_rooms(self, modified_since: Optional[int]) -> Optional[List[dict]]:
        """``GET apps/spreed/api/v4/room``（**api/v4**，v1/v2/v3 该端点 404）。"""
        params = {"modifiedSince": modified_since} if modified_since else None
        resp = self._request("GET", ROOMS_PATH, params=params,
                             timeout=DEFAULT_SOCKET_TIMEOUT)
        code, data = _ocs_data(resp.data)
        if resp.status < 200 or resp.status >= 300 or not isinstance(data, list):
            logger.warning(
                "nextcloud: 列会话失败 (HTTP %s / ocs %s): %s",
                resp.status, code, _error_detail(resp.data) or "data 不是数组",
            )
            return None
        return [item for item in data if isinstance(item, dict)]

    def _refresh_rooms(self, *, full: bool) -> None:
        """刷新会话表。

        **为什么必须周期性全量刷新**：带 ``modifiedSince`` 的增量拉取**检测不到**
        "会话被删除 / 我被移出群" —— 被移出后该会话根本不会再出现在增量响应里，
        于是我们会拿着一个已经不存在的 token 一直轮询、一直 404。官方要求每 5 分钟
        跑一次不带 ``modifiedSince`` 的全量刷新来对账，本实现照办。
        """
        rooms = self._fetch_rooms(None if full else (self._last_modified_since or None))
        if rooms is None:
            return
        seen: set[str] = set()
        for raw in rooms:
            token = str(raw.get("token") or "").strip()
            if not token:
                continue
            seen.add(token)
            room = self.rooms.get(token)
            if room is None:
                room = _Room(token=token)
                self.rooms[token] = room
            permissions = _as_int(raw.get("permissions"), PERM_DEFAULT)
            room.read_only = _as_int(raw.get("readOnly")) == 1
            room.lobby = _as_int(raw.get("lobbyState")) == 1
            # PERM_CHAT(128) 是"可以发聊天消息"；readOnly → 403、lobby → 412，
            # 所以三者任一不满足就不该尝试发消息。
            room.can_send = bool(permissions & PERM_CHAT) and not room.read_only \
                and not room.lobby
        if full:
            # 全量刷新才对账：把已经消失（被删 / 被移出）的会话踢掉。
            for token in [t for t in self.rooms if t not in seen]:
                logger.info("nextcloud: 会话 %s 已消失（被删或我被移出），停止轮询", token)
                del self.rooms[token]
        with self._poll_lock:
            # 轮转顺序保持稳定（按 token 排序），新增的追加到末尾。
            self._poll_order = sorted(self.rooms)
        self._last_modified_since = int(time.time())

    def _bootstrap_cursor(self, token: str) -> int:
        """新会话的游标：``GET chat/{token}?lookIntoFuture=0&limit=1`` + 响应头。

        ⚠️ 这里**必须** ``limit=1`` + 拿 ``X-Chat-Last-Given``：若用
        ``lastKnownMessageId=0`` + ``lookIntoFuture=0``，服务端返回的是**最新 N 条
        （降序）**，不是最早 N 条 —— 拿它的末条当游标会把整段历史当成新消息重放一遍。
        """
        quoted = urllib.parse.quote(token, safe="")
        resp = self._request(
            "GET",
            f"{CHAT_PATH}/{quoted}",
            params={"lookIntoFuture": 0, "limit": BOOTSTRAP_LIMIT},
            timeout=DEFAULT_SOCKET_TIMEOUT,
        )
        if resp.status < 200 or resp.status >= 300:
            logger.info(
                "nextcloud: 会话 %s 游标 bootstrap 失败 (HTTP %s)，游标保持 0",
                token, resp.status,
            )
            return 0
        cursor = _header_int(resp.headers, "x-chat-last-given")
        if cursor <= 0:
            logger.info("nextcloud: 会话 %s 的 X-Chat-Last-Given 缺失或非法，游标保持 0",
                        token)
        return max(0, cursor)

    def _next_room(self) -> Optional[str]:
        """取下一个要轮询的会话 token（**轮转**：取出来的放回队尾）。"""
        with self._poll_lock:
            if not self._poll_order:
                return None
            token = self._poll_order.pop(0)
            self._poll_order.append(token)
            return token

    def _worker_loop(self) -> None:
        """worker：不停地取下一个会话做一次长轮询。**任何异常都不许逃出线程。"""
        while not self._stop_event.is_set():
            token = self._next_room()
            if token is None:
                if self._stop_event.wait(POLL_IDLE_WAIT):
                    return
                continue
            try:
                self._poll_once(token)
            except Exception:  # noqa: BLE001
                logger.exception("nextcloud: 轮询会话 %s 失败", token)

    # ------------------------------------------------------------------
    # 长轮询
    # ------------------------------------------------------------------
    def _poll_once(self, token: str) -> None:
        """一次长轮询（``lookIntoFuture=1``，服务端最多挂 ``timeout`` 秒）。

        游标**先更新再处理消息**：单条消息处理抛异常时游标已经推进，不会被重放 ——
        与 telegram 推进 offset、matrix 推进 next_batch 同理。
        """
        room = self.rooms.get(token)
        if room is None:
            return
        if not room.bootstrapped:
            room.cursor = self._bootstrap_cursor(token)
            room.bootstrapped = True
        quoted = urllib.parse.quote(token, safe="")
        params = {
            "lookIntoFuture": 1,
            "limit": CHAT_PAGE_LIMIT,
            "timeout": self.poll_timeout,
            "lastKnownMessageId": room.cursor,
            # 只读不打扰：不要替用户改已读标记 / 状态 / 通知。
            "setReadMarker": 0,
            "noStatusUpdate": 1,
            "markNotificationsAsRead": 0,
        }
        # 本地 socket 超时必须 > 服务端 timeout，否则本地先断、长轮询白挂。
        resp = self._request(
            "GET", f"{CHAT_PATH}/{quoted}", params=params,
            timeout=self._local_timeout,
        )
        if resp.status == 304:
            return  # 无新消息：游标不变
        if resp.status < 200 or resp.status >= 300:
            logger.warning(
                "nextcloud: 长轮询 %s 失败 (HTTP %s): %s",
                token, resp.status, _error_detail(resp.data),
            )
            return
        code, data = _ocs_data(resp.data)
        if code and code >= 400:
            logger.warning("nextcloud: 长轮询 %s 的 ocs 状态码 %s", token, code)
            return

        # ---- 先推进游标，再逐条处理 --------------------------------------
        new_cursor = _header_int(resp.headers, "x-chat-last-given")
        if new_cursor > 0:
            # 它可能指向一条对你不可见的消息（权限不够），但仍应照用 ——
            # 忽略它会导致下一次重复投递同一批。
            room.cursor = new_cursor
        elif data:
            logger.info(
                "nextcloud: 会话 %s 的 200 响应缺 X-Chat-Last-Given，游标保持 %d"
                "（可能重复投递）", token, room.cursor,
            )
        for message in (data or []):
            if self._stop_event.is_set():
                return
            if not isinstance(message, dict):
                continue
            try:
                self._handle_message(token, message)
            except Exception:  # noqa: BLE001 - 单条异常不许拖垮整轮
                logger.exception("nextcloud: 处理会话 %s 的消息失败", token)

    # ------------------------------------------------------------------
    # 消息过滤
    # ------------------------------------------------------------------
    def _drop_inbound(self, reason: str, token: str, actor: str) -> None:
        logger.info(
            "nextcloud: 丢弃消息（%s）room=%s actor=%s", reason, token or "?", actor or "?"
        )

    @staticmethod
    def _shares_attachment(params: Any) -> bool:
        """``messageParameters`` 里是否含"分享了文件 / 位置 / 投票"。

        ⚠️ **这是本适配器最容易漏的一条反向判断**：``file_shared`` / ``object_shared``
        这类系统消息会被服务端**改写成** ``messageType="comment"`` **且**
        ``systemMessage=""``，所以"只看 messageType / systemMessage"过滤不干净，
        必须额外查 ``messageParameters`` 里有没有 ``file`` / ``object``。
        """
        if not isinstance(params, dict) or not params:
            return False
        for name, spec in params.items():
            if str(name).strip().lower() in SHARED_PARAM_NAMES:
                return True
            # 防御：个别版本把类型放在 spec["type"] 里而不是参数名上
            if isinstance(spec, dict):
                inner = str(spec.get("type") or "").strip().lower()
                if inner in SHARED_PARAM_NAMES:
                    return True
        return False

    def _handle_message(self, token: str, message: dict) -> bool:
        """过滤 → Inbound。返回是否真的放行了一条。

        字段名权威来源是 ``lib/Model/Message::toArray()``：``id`` / ``token`` /
        ``actorId`` / ``actorType`` / ``actorDisplayName`` / ``timestamp``（**秒**级）/
        ``message`` / ``messageParameters`` / ``messageType`` / ``systemMessage``。
        """
        actor_id = str(message.get("actorId") or "")
        actor_type = str(message.get("actorType") or "")
        message_type = str(message.get("messageType") or "")
        system_message = str(message.get("systemMessage") or "")
        text = str(message.get("message") or "")
        params = message.get("messageParameters")

        # 0) 拿不到自己的 uid 就无法防回环 —— 整个入站停摆（见 start() 的告警）
        if not self.user_id:
            self._drop_inbound("不知道自己的 uid，无法防回环", token, actor_id)
            return False
        # 1) 防回环：**只看** actorType=="users" 且 actorId==自己的 uid。
        #    Talk 里还有 guests / federated_users / bots 等 actor 类型，它们发的话
        #    我们要正常处理，所以必须带上 actorType 判定，不能只看 actorId。
        if actor_type == ACTOR_TYPE_USER and actor_id == self.user_id:
            self._drop_inbound("自己发的", token, actor_id)
            return False
        # 2) 只收普通发言：system / command / comment_deleted 一律不要
        if message_type != MESSAGE_TYPE_COMMENT:
            self._drop_inbound(f"非普通发言 messageType={message_type!r}", token, actor_id)
            return False
        # 3) 系统消息（正常情况它同时带 systemMessage，但这里独立判一次更保险）
        if system_message:
            self._drop_inbound(f"系统消息 systemMessage={system_message!r}", token, actor_id)
            return False
        # 4) ⚠️ 反直觉分支：file_shared / object_shared 会被改写成 comment + 空
        #    systemMessage，光看上面两条过滤不干净，必须查 messageParameters。
        if self._shares_attachment(params):
            self._drop_inbound("分享了文件/位置/投票（messageParameters 含 file/object）",
                               token, actor_id)
            return False
        # 5) 空正文
        if not text.strip():
            self._drop_inbound("空正文", token, actor_id)
            return False
        # 6) 授权闸门必须在产生 Inbound **之前**（否则能用命令 / 审批字绕过）。
        #    allowed_chat_ids 填的是**会话 token**，不是 user id。
        if not self.admits(token):
            self._drop_inbound("未在白名单", token, actor_id)
            return False
        try:
            self.hooks.on_inbound(
                Inbound(
                    conversation_id=self._conversation_id(token),
                    # ⚠️ 这是含 ``{mention-call1}`` 占位符的模板串，v1 不做提及渲染
                    text=text,
                    kind="text",
                    user_id=actor_id or None,
                    message_id=str(message.get("id") or "") or None,
                    platform=self.name,
                    raw=message,
                )
            )
        except Exception as exc:  # noqa: BLE001 - 上层炸了也不能让 worker 退出
            logger.exception("nextcloud: on_inbound 失败: %s", exc)
            return False
        return True

    # ------------------------------------------------------------------
    # Lifecycle teardown
    # ------------------------------------------------------------------
    def stop(self) -> None:
        """先停循环 + 尽力关掉在飞的长轮询，再 join。

        顺序不能反：基类 ``stop()`` 会 join 协调线程（5s 超时），而 worker 可能正卡在
        一次 30 秒的长轮询里。先置 stop 事件（worker 下一次取活就会退出）+ 关响应，
        才能让 join 尽快返回。关 socket 只是尽力而为，最终兜底是本地 socket 超时
        （= 服务端 timeout + 10s）。
        """
        self._stop_event.set()
        self._close_active_responses()
        super().stop()
        self._stop_workers()

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------
    @staticmethod
    def _conversation_id(token: Any) -> str:
        return f"nextcloud:{token}"

    @staticmethod
    def _token(conversation_id: Any) -> Optional[str]:
        raw = str(conversation_id or "")
        if raw.startswith("nextcloud:"):
            raw = raw[len("nextcloud:"):]
        return raw or None

    def _can_send_to(self, token: str) -> bool:
        """能不能对这个会话发消息。

        已知该会话的 ``readOnly==1`` / ``lobbyState==1`` / 缺 ``PERM_CHAT`` 时**直接拒绝**，
        不去打注定 403 / 412 的请求 —— 一个只读会话不该把出站搞崩。未知会话（还没被
        发现）一律放行，交给服务端裁决。
        """
        room = self.rooms.get(token)
        if room is None:
            return True
        return room.can_send

    def _note_failure(self, resp: _Resp, what: str) -> None:
        """把一次出站失败喂给基类的可观测通道（T1.3）。

        ``_note_send_failure`` 记下的分类会被基类 ``Adapter.send_result()`` 读出，
        产出结构化 ``SendResult``；分片发送中途失败且已有成功分片时还会标
        ``partial=True``。
        """
        detail = _error_detail(resp.data) or f"HTTP {resp.status}"
        logger.warning("nextcloud: %s failed (HTTP %s): %s", what, resp.status, detail)
        self._note_send_failure(_classify_nc_error(resp.status, resp.data), detail)

    @staticmethod
    def _succeeded(resp: _Resp) -> bool:
        """出站调用是否成功：2xx（另含 Nextcloud 可能给的 202）且 OCS 层没报错。"""
        if resp.status < 200 or resp.status >= 300:
            return False
        code, _ = _ocs_data(resp.data)
        return not code or code < 400

    def send(self, out: Outbound) -> MsgHandle | None:
        """``POST chat/{token}``（**成功 201**），超长自动分片。

        body 是 ``urlencode({"message": text})`` —— 服务端**显式按 urlencoded 解析**，
        字段名是 ``message``（不是 ``text`` / ``body``）。
        """
        token = self._token(out.conversation_id)
        if not token:
            logger.warning("nextcloud: bad conversation_id %r", out.conversation_id)
            self._note_send_failure(SendError.BAD_FORMAT, "bad conversation_id")
            return None
        if not out.text:
            # 服务端对空消息直接 400；本地先拦一道，省一次往返
            logger.warning("nextcloud: refusing to send empty text")
            self._note_send_failure(SendError.BAD_FORMAT, "empty text")
            return None
        if not self.base_url or not self.username or not self.password:
            logger.warning("nextcloud: base_url/username/password missing; send refused")
            self._note_send_failure(
                SendError.BAD_FORMAT, "base_url/username/password missing"
            )
            return None
        if not self._can_send_to(token):
            logger.warning("nextcloud: 会话 %s 只读 / 无发言权限，不发", token)
            self._note_send_failure(SendError.FORBIDDEN, "room is read-only / no PERM_CHAT")
            return None
        chunks = split_text(out.text, self.effective_max_length, prefix_fmt="")
        if len(chunks) > 1:
            logger.info(
                "nextcloud: splitting outbound message into %d chunks (limit=%d)",
                len(chunks), self.effective_max_length,
            )
        quoted = urllib.parse.quote(token, safe="")
        handle: MsgHandle | None = None
        for chunk in chunks:
            resp = self._request("POST", f"{CHAT_PATH}/{quoted}", form={"message": chunk})
            if not self._succeeded(resp):
                self._note_failure(resp, "POST /chat/{token}")
                return handle if handle is not None else None
            _, data = _ocs_data(resp.data)
            message_id = str(data.get("id") or "") if isinstance(data, dict) else ""
            handle = MsgHandle(
                conversation_id=out.conversation_id,
                message_id=message_id,
                platform=self.name,
            )
        return handle

    def edit(self, handle: MsgHandle, out: Outbound) -> bool:
        """``PUT chat/{token}/{messageId}`` —— **本平台支持编辑**。

        已知限制（失败时如实归因，不吞）：

        * **超过 24 小时不能编辑** → 400 ``{"error": "age"}``；
        * 非本人且非 moderator → 403；
        * 会话只读 → 403；
        * 超长 → 413。
        需要服务端开启 ``edit-messages`` capability（Talk 19+ 默认开）。

        返回 ``True``/``False``（基类契约），成功码是 **200 或 202**。
        """
        token = self._token(handle.conversation_id)
        message_id = str(handle.message_id or "").strip()
        if not token or not message_id:
            logger.warning("nextcloud: bad handle %r", handle)
            self._note_send_failure(SendError.BAD_FORMAT, "bad handle")
            return False
        if not out.text:
            logger.warning("nextcloud: refusing to edit with empty text")
            self._note_send_failure(SendError.BAD_FORMAT, "empty text")
            return False
        if not self.base_url or not self.username or not self.password:
            logger.warning("nextcloud: base_url/username/password missing; edit refused")
            self._note_send_failure(
                SendError.BAD_FORMAT, "base_url/username/password missing"
            )
            return False
        quoted_token = urllib.parse.quote(token, safe="")
        quoted_id = urllib.parse.quote(message_id, safe="")
        resp = self._request(
            "PUT", f"{CHAT_PATH}/{quoted_token}/{quoted_id}", form={"message": out.text}
        )
        if not self._succeeded(resp):
            self._note_failure(resp, "PUT /chat/{token}/{messageId}")
            return False
        return True

    def answer(self, query_id: str, text: str = "") -> None:
        """Talk v1 没有需要应答的 callback query。"""
        return None
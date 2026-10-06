"""Lane B — Matrix adapter (tasks.md T3.1). Standard library only.

传输层用 ``urllib.request``（零第三方依赖），全部调用收敛在单个可覆写的
``_request`` 方法后面，测试只需替换它即可离线验证协议层逻辑。

A1：轮询循环 / 退避 / 线程 / ``stop()`` 语义已迁到
:class:`~opencode_bridge.transport.PollingTransport`。本文件只留 Matrix 语义：
增量同步游标、事件过滤、授权闸门、编辑的 MSC2676 近似、出站分片。

``conversation_id`` 前缀：已从 ``room:`` 切到 ``matrix:``
-----------------------------------------------------
:meth:`MatrixAdapter._conversation_id` 现在走
``identity.format_id("matrix", ...)``，产出统一格式 ``platform:local_id``。

切换的前置条件（旧前缀期间一直挂着的那条警告）已经满足：
:class:`~opencode_bridge.state.StateStore` 的键迁移（旧键 → 新键的显式重写 +
版本门控）已上线，且 ``__main__`` **打开了** ``migrate_keys=True``。这两件事
**必须同一个变更**：只切前缀而不开迁移，已落盘 ``state.json`` 里的所有 ``room:``
键会一次性变成孤儿，用户会在升级后一次性"忘记"所有历史会话映射 —— 不报错，
只表现为"agent 突然记错上下文"，比直接失败难查得多。

⚠️ **反向解析仍认旧前缀**（见 :attr:`MatrixAdapter._CONVERSATION_PREFIXES`）：
写前收件箱把 ``conversation_id`` **持久化**在 SQLite 里，升级前写入、升级后才
重放的未投递消息带着 ``room:`` 前缀。认不出来就等于把那些回复永久丢弃。
``state.py`` 的"精确键 → 无歧义别名"回退是同一类问题的另一半。

两个平台特有的点：

1. **增量同步游标**：``/_matrix/client/v3/sync`` 返回的 ``next_batch`` 是下一次
   增量同步的**唯一**凭据（不存在"拿最近 N 条"这种回放接口）。它保存在实例字段
   :attr:`MatrixAdapter._since` 上，跨多次调用存活；每次请求都带上，所以既不会
   漏消息也不会无限重放。**顺序铁律：先推进游标，再分发事件** —— 否则分发里
   一条事件抛异常就会让整页事件被无限重放。

2. **"编辑"消息**：Matrix 基础协议里**没有**编辑消息的 API。本适配器走业界兼容
   近似写法（见 :meth:`MatrixAdapter.edit`）：再发一条 ``m.room.message``，
   ``body`` 以 ``"* "`` 开头，且 ``content`` 里带 ``m.new_content`` +
   ``m.relates_to``（``rel_type="m.replace"``）。支持这套写法的客户端会把它渲染
   成对原事件的编辑；不支持的客户端只会当成一条普通消息 —— 即"看起来多了一条"，
   不会丢内容。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, List, Optional, Tuple

from ..config_coerce import coerce_int
from ..hooks import Hooks, Inbound, MsgHandle, Outbound, SendError
from ..identity import format_id
from ..split import split_text
from ..transport import NOTHING, PollingTransport
from ._redactable_ids import redactable_id
from .base import Adapter, classify_http, register

logger = logging.getLogger("opencode_bridge.adapters.matrix")

__all__ = ["MatrixAdapter", "MESSAGE_LIMIT"]

#: 事件体（JSON）整体有 64KB 上限，正文留足富余取保守值。
MESSAGE_LIMIT = 4096
SYNC_TIMEOUT_MS = 30000          # 长轮询挂起时长（客户端 /sync 参数）
SYNC_SOCKET_TIMEOUT = 35.0       # socket 超时必须 > 长轮询时长，否则会被提前掐断
DEFAULT_SOCKET_TIMEOUT = 30.0
BACKOFF_INTERVAL = 2.0           # 单次同步失败后的退避间隔（秒）
MIN_SEND_INTERVAL = 1.2          # 每个会话的发送/编辑节流（秒）
MAX_RETRY_AFTER = 60.0           # M_LIMIT 的 retry_after_ms 上限（秒）

ROOM_MSG_TYPE = "m.room.message"
TEXT_MSGTYPE = "m.text"
SYNC_PATH = "_matrix/client/v3/sync"


def _classify_matrix_error(status: int, data: Any) -> SendError:
    """Matrix 报失败是 ``{"errcode": "M_XXX", "error": ...}`` + HTTP 状态码。

    先按官方 ``errcode`` 判定（语义比状态码精确），再回落到基类的
    :func:`~opencode_bridge.adapters.base.classify_http`（T1.3）。
    """
    if not isinstance(data, dict):
        return classify_http(status)
    errcode = str(data.get("errcode") or "").strip().upper()
    detail = str(data.get("error") or "")
    if errcode == "M_LIMIT" or status == 429:
        return SendError.RATE_LIMITED
    if errcode == "M_FORBIDDEN":
        return SendError.FORBIDDEN
    if errcode == "M_NOT_FOUND":
        return SendError.NOT_FOUND
    if errcode == "M_TOO_LARGE" or status == 413:
        return SendError.TOO_LONG
    if errcode in ("M_BAD_JSON", "M_INVALID_PARAM", "M_UNRECOGNIZED", "M_BAD_STATE"):
        return SendError.BAD_FORMAT
    return classify_http(status, f"{errcode} {detail}".strip())


def _retry_after_seconds(data: Any) -> Optional[float]:
    """从 ``M_LIMIT`` 响应里取 ``retry_after_ms``（毫秒 → 秒，封顶）。"""
    if not isinstance(data, dict):
        return None
    ms = data.get("retry_after_ms")
    if isinstance(ms, bool) or not isinstance(ms, (int, float)):
        return None
    if ms < 0:
        return None
    return min(float(ms) / 1000.0, MAX_RETRY_AFTER)


def _error_detail(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    return str(data.get("error") or data.get("errcode") or "")


@register("matrix")
class MatrixAdapter(Adapter):
    """Matrix Client-Server API adapter（``/sync`` 入站 + ``send`` 出站）。

    线程与退避归 :class:`~opencode_bridge.transport.PollingTransport`；
    :attr:`running` / :meth:`stop` 是它的代理。

    ``_conversation_id`` 产出统一格式 ``matrix:<room_id>``（已从 ``room:`` 切过来），
    ``_room_id`` 仍认旧前缀 —— 见模块 docstring「``conversation_id`` 前缀」一节。
    """

    name = "matrix"
    label = "Matrix"
    max_message_length = MESSAGE_LIMIT          # 取保守值（事件体整体上限 64KB）
    supports_inbound = True                     # /sync 长轮询
    #: principal = room id：(a) 会话唯一且稳定，(b) 用户能直接看到它，
    #: (c) Matrix 对发件人做过认证（access token）。三条都成立。
    pairing_supported = True
    supports_inline_buttons = False             # v1 不把 reactions 当交互
    supports_media = False                      # v1 只发 m.text
    #: ``m.relates_to`` + ``rel_type="m.replace"`` 是真的替换（见 :meth:`edit`），
    #: 所以占位气泡发得。⚠️ 个别客户端不支持替换，:meth:`edit` 会返回 ``False``，
    #: 那时收尾退化成"再发一条"—— 能力是**平台级**的，个别客户端的缺口不在这里判。
    supports_message_edit = True

    # Matrix 没有 bot_token 的概念，凭据是 homeserver + access_token。
    # user_id 也列入必需：它是**过滤自己回声**的唯一依据，缺了会无限回环
    # （桥接把自己发出的消息再当成入站消息收回来）。
    required_tokens = ("homeserver", "access_token", "user_id")
    outbound_tokens = ("homeserver", "access_token")

    # 类级旋钮（测试可在实例上覆盖）。
    min_interval = MIN_SEND_INTERVAL
    backoff_interval = BACKOFF_INTERVAL

    #: ``conversation_id`` 的合法前缀：当前格式 + 切换前的旧别名（``room:``）。
    #: :meth:`_room_id` 按这个列表剥前缀，所以**旧前缀必须继续认** ——
    #: 写前收件箱把 ``conversation_id`` 持久化在盘上，切换前写入、切换后才重放的
    #: 未投递消息带着 ``room:``，认不出来就等于把那些回复永久丢弃。
    #: 与 :data:`identity.LEGACY_PREFIXES` 同源（``room`` → ``matrix``）。
    _CONVERSATION_PREFIXES = ("matrix:", "room:")

    def __init__(self, config: dict, hooks: Hooks) -> None:
        super().__init__(config, hooks)
        # ``homeserver`` 允许带尾斜杠，这里统一去掉，避免拼出 "//_matrix/..."。
        self.homeserver: str = str(self.config.get("homeserver") or "").strip().rstrip("/")
        self.access_token: str = str(self.config.get("access_token") or "").strip()
        #: 自己的 MXID，用于过滤自己发出的回声（不配置就不做该过滤）。
        self.user_id: str = str(self.config.get("user_id") or "").strip()
        # allowed_chat_ids 已由基类 _init_access() 统一解析（T1.2）
        #: ``/sync`` 长轮询挂起时长（毫秒，进 ``_sync_path`` 的 ``timeout`` 参数）。
        #: ⚠️ **刻意不设区间**：Matrix 协议对 ``timeout`` 本身不设上界（服务端会自己
        #: 掐断），而 ``SYNC_SOCKET_TIMEOUT`` 是本适配器自己的常量、不由配置决定 ——
        #: 加一个自造的上界只会把"用户故意调大长轮询"变成一次静默回落。
        self.sync_timeout_ms: int = coerce_int(
            self.config, "sync_timeout_ms", SYNC_TIMEOUT_MS, platform=self.name
        )
        #: ``next_batch`` 游标 —— Matrix 增量同步的核心，跨调用保存在实例上。
        self._since: str = str(self.config.get("since") or "").strip()
        self._transport: Optional[PollingTransport] = None
        self._throttle_lock = threading.Lock()
        self._last_send: dict[str, float] = {}

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
        """Call ``{homeserver}/{path}`` with a Bearer token. Never raises.

        Returns ``(http_status, body)``; 传输层失败映射为 ``(0, {"errcode":
        "M_TRANSPORT", ...})``，调用方按状态码走失败分类。
        """
        url = f"{self.homeserver}/{path.lstrip('/')}"
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json",
                "Authorization": f"Bearer {self.access_token}",
            },
        )
        sock_timeout = timeout if timeout is not None else DEFAULT_SOCKET_TIMEOUT
        try:
            with urllib.request.urlopen(req, timeout=sock_timeout) as resp:
                status = getattr(resp, "status", 200) or 200
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                raw = exc.read()
            except Exception:
                raw = b""
        except Exception as exc:
            logger.warning("matrix: transport error on %s: %s", path, exc)
            return 0, {"errcode": "M_TRANSPORT", "error": f"transport error: {exc}"}
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            return status, {
                "errcode": "M_NOT_JSON",
                "error": f"non-JSON response (HTTP {status})",
            }
        if not isinstance(data, dict):
            return status, {"errcode": "M_UNEXPECTED", "error": "unexpected payload"}
        # 代理 / 网关常把 4xx/5xx 的响应体吃掉；补一个 errcode 标记，
        # 好让调用方仍然按状态码走失败分类（而不是当成"成功但没有 event_id"）。
        if status >= 400 and not data.get("errcode"):
            data["errcode"] = f"M_HTTP_{status}"
        return status, data

    def _throttle(self, conversation_id: str) -> None:
        """每个会话的最小发送间隔（homeserver 普遍对 M_LIMIT 敏感）。"""
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
    # Lifecycle
    # ------------------------------------------------------------------
    @property
    def transport(self) -> Optional[PollingTransport]:
        """当前传输层（``start()`` 之后才有）。"""
        return self._transport

    @property
    def running(self) -> bool:
        """轮询线程是否活着（代理到传输层）。"""
        transport = self._transport
        return transport is not None and transport.running

    def _make_transport(self) -> PollingTransport:
        """构造本次运行用的传输层（测试注入点：退避接线值）。"""
        # 三处间隔都取 ``backoff_interval``，因为迁移前就是**一个**常数：
        #   * idle_sleep     —— HTTP 失败（fetch 返回 NOTHING）后的重试间隔；
        #   * min_backoff    —— fetch 抛异常（传输层错误）后的重试间隔；
        #   * max_backoff    —— 与 min 相同 ⇒ 退避**恒定**、不会指数增长
        #                        （迁移前没有指数退避，这里不能顺手"优化"）。
        # reset_after=0 ⇒ 只要 fetch 成功过一次就重置退避（连上即重置）。
        backoff = float(self.backoff_interval)
        return PollingTransport(
            self._request_sync,
            idle_sleep=backoff,
            name="matrix",
            min_backoff=backoff,
            max_backoff=backoff,
            reset_after=0.0,
        )

    def start(self) -> None:
        """缺 ``homeserver`` / ``access_token`` 时只告警并返回（不抛异常）。

        ``_since`` 不在这里重置 —— 游标属于实例状态，重启同一个实例应继续增量拉取。
        """
        if not self.homeserver:
            logger.warning("matrix: homeserver missing; adapter not started")
            return
        if not self.access_token:
            logger.warning("matrix: access_token missing; adapter not started")
            return
        # ⚠️ `user_id` 在 :attr:`required_tokens` 里、却**不在**上面那两道闸门里，
        # 而它是 :meth:`_handle_event` 里过滤自己回声的**唯一**依据
        # （`if self.user_id and sender == self.user_id`）⇒ 空值时整个条件短路，
        # **回声一条都挡不住**，桥会无限自问自答。这里点名它，别让这个陷阱只在
        # 症状里出现。
        # ⛔ 只告警、**不改行为**：``user_id` 缺失时是否改成「失败关闭」
        # （缺了就拒收）**尚未拍板** —— 那是行为变更，不是告警能顺带做的事。
        if not self.user_id:
            logger.warning(
                "matrix: user_id missing; the adapter cannot filter its own "
                "echoes — the bridge will treat its own messages as inbound "
                "and keep talking to itself (fill in user_id = this bot's MXID)"
            )
        self._stop_event.clear()
        transport = self._make_transport()
        self._transport = transport
        transport.start(self._on_sync)
        logger.info("matrix: sync loop started (since=%r)", self._since)

    def stop(self) -> None:
        """置停止位 → 关传输层 → join（**幂等**）。

        迁移前是 ``Adapter.stop()`` 置位 + join 5s；退避等待用
        ``_stop_event.wait(backoff)``，所以能被立刻打断。现在这两段都在
        :class:`~opencode_bridge.transport.PollingTransport` 里，语义不变。

        ⚠️ 已知限制（迁移前就有，不是本次引入的）：``stop()`` **打不断**正在挂起
        的 ``/sync`` HTTP 请求 —— 最长要等 ``SYNC_SOCKET_TIMEOUT``（35s）那一轮
        自己返回，join 会在 5s 处超时返回，线程（daemon）随后自然退出。
        """
        super().stop()                      # 置停止位（_throttle 依赖它）
        transport, self._transport = self._transport, None
        if transport is not None:
            transport.stop()

    # ------------------------------------------------------------------
    # Inbound (/sync long polling)
    # ------------------------------------------------------------------
    def _sync_path(self) -> str:
        """本次同步的请求路径（带上 :attr:`_since` 游标）。"""
        query = {"timeout": str(self.sync_timeout_ms)}
        if self._since:
            query["since"] = self._since
        return f"{SYNC_PATH}?{urllib.parse.urlencode(query)}"

    def _request_sync(self) -> Any:
        """一轮 ``/sync`` 的**原始结果**：成功返回响应 dict，失败返回 :data:`NOTHING`。

        HTTP 失败（4xx/5xx/带 errcode）**不抛异常**，而是返回 :data:`NOTHING` ——
        轮询语义下"这一轮没拿到"就是"没有"，由传输层按 ``idle_sleep`` 退避后重试。
        抛异常在传输层里表示"传输层故障"（会走另一条退避路径 + 记一条告警），
        而 Matrix 的 429/403 是**正常的业务响应**，不该被当成故障。
        """
        status, data = self._request(
            "GET",
            self._sync_path(),
            None,
            timeout=SYNC_SOCKET_TIMEOUT,
        )
        if status < 200 or status >= 300 or data.get("errcode"):
            logger.warning(
                "matrix: /sync failed (HTTP %s): %s",
                status,
                _error_detail(data) or data.get("errcode"),
            )
            return NOTHING
        return data

    def _on_sync(self, data: Any) -> None:
        """传输层交给我们的**一份成功响应**。

        **顺序铁律：先推进游标，再分发事件。** 这样分发里单条事件抛异常也不会让
        这一页事件被无限重放（下次 ``/sync`` 已经带着新的 ``next_batch``）。
        游标推进在最前，也意味着即使 :meth:`_dispatch_sync` 整体崩了，
        游标也不会倒退或丢失。
        """
        next_batch = str(data.get("next_batch") or "").strip()
        if next_batch:
            self._since = next_batch
        self._dispatch_sync(data)

    def _sync_once(self) -> bool:
        """跑完一轮同步（请求 → 推进游标 → 分发）。返回 True 表示成功。

        这是 :meth:`_request_sync` + :meth:`_on_sync` 的"永不抛异常"组合形态，
        供测试与手动诊断（拉一次看看游标对不对）使用；常驻轮询走传输层。
        """
        try:
            data = self._request_sync()
        except Exception:
            logger.exception("matrix: sync cycle failed")
            return False
        if data is NOTHING:
            return False                      # 失败：游标一字不动
        self._on_sync(data)
        return True

    def _dispatch_sync(self, data: dict) -> None:
        """遍历 ``rooms.join.{roomId}.timeline.events``。"""
        rooms = data.get("rooms")
        joined = rooms.get("join") if isinstance(rooms, dict) else None
        if not isinstance(joined, dict):
            return
        for room_id, room in joined.items():
            if not isinstance(room, dict):
                continue
            timeline = room.get("timeline")
            events = timeline.get("events") if isinstance(timeline, dict) else None
            if not isinstance(events, list):
                continue
            for event in events:
                try:
                    self._handle_event(str(room_id), event)
                except Exception:
                    logger.exception(
                        "matrix: failed to handle event %r",
                        event.get("event_id") if isinstance(event, dict) else event,
                    )

    def _handle_event(self, room_id: str, event: Any) -> None:
        """单条 timeline 事件的过滤 + 授权 + 投递。"""
        if not isinstance(event, dict):
            return
        if event.get("type") != ROOM_MSG_TYPE:
            return  # m.room.member / m.room.redaction / 其它事件类型
        content = event.get("content")
        if not isinstance(content, dict):
            return
        if content.get("msgtype") != TEXT_MSGTYPE:
            return  # m.notice / m.image / m.emote ...
        sender = str(event.get("sender") or "")
        if self.user_id and sender == self.user_id:
            return  # 自己的回声（含本适配器自己发出的编辑消息）
        if "m.relates_to" in content:
            # 编辑（m.replace）/ 回复（m.in_reply_to）/ 表情回应（m.annotation）
            return
        body = content.get("body")
        if not isinstance(body, str) or not body:
            return
        event_id = event.get("event_id")
        # 授权闸门必须在最前：未授权房间的消息不许进入上层（否则能用命令/审批字绕过）。
        # ⚠️ /pair 在未授权时也要能进来，所以 conversation_id 提到闸门之前算。
        conversation_id = self._conversation_id(room_id)
        if not self.admits(room_id) and not self.answer_pairing_request(
            room_id, conversation_id, body
        ):
            logger.info(
                "matrix: dropping message from non-whitelisted room %s",
                redactable_id(self.name, room_id),
            )
            return
        try:
            self.hooks.on_inbound(
                Inbound(
                    conversation_id=conversation_id,
                    text=body,
                    kind="text",
                    user_id=sender or None,
                    message_id=str(event_id) if event_id is not None else None,
                    platform=self.name,
                    raw=event,
                )
            )
        except Exception as exc:
            logger.exception("matrix: on_inbound failed: %s", exc)

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------
    @staticmethod
    def _conversation_id(room_id: Any) -> str:
        """``room_id`` → ``matrix:...``（统一 ``platform:local_id`` 格式）。"""
        return format_id("matrix", room_id)

    @staticmethod
    def _room_id(conversation_id: Any) -> Optional[str]:
        """``conversation_id`` → 房间 id；空的一律返回 ``None``。

        裸房间 id（``"!abc:example.org"``）也认，切换前后的两种前缀都认 ——
        理由见 :attr:`MatrixAdapter._CONVERSATION_PREFIXES`。注意房间 id 本身
        **含冒号**，所以这里只剥**已知前缀**、不做按冒号切分。
        """
        raw = str(conversation_id or "")
        for prefix in MatrixAdapter._CONVERSATION_PREFIXES:
            if raw.startswith(prefix):
                raw = raw[len(prefix):]
                break
        return raw or None

    def _send_path(self, room_id: str, txn_id: str) -> str:
        """``rooms/{roomId}/send/m.room.message/{txnId}``。

        ``txnId`` 由调用方给（``uuid4``）—— Matrix 用它做幂等键：同一个 txnId 重复
        PUT 不会产生第二条事件，网络重试因此是安全的。
        """
        room = urllib.parse.quote(room_id, safe="!:$")
        return f"_matrix/client/v3/rooms/{room}/send/{ROOM_MSG_TYPE}/{txn_id}"

    def _put_message(
        self, room_id: str, content: dict, conversation_id: str
    ) -> Tuple[int, dict]:
        self._throttle(conversation_id)
        return self._request(
            "PUT",
            self._send_path(room_id, str(uuid.uuid4())),
            content,
            timeout=DEFAULT_SOCKET_TIMEOUT,
        )

    @staticmethod
    def _handle_from(
        room_id: str, conversation_id: str, event_id: Any
    ) -> Optional[MsgHandle]:
        if event_id is None:
            return None
        return MsgHandle(
            conversation_id=conversation_id,
            message_id=str(event_id),
            platform="matrix",
        )

    def _note_failure(self, status: int, data: Any, what: str) -> None:
        """把一次出站失败喂给基类的可观测通道（T1.3）。

        ``_note_send_failure`` 记下的分类会被基类 ``Adapter.send_result()`` 读出，
        产出结构化 ``SendResult``（``ok=False`` + ``error_kind`` / ``retry_after``）；
        分片发送中途失败且已有成功分片时，``send_result`` 会额外标 ``partial=True``。
        """
        detail = _error_detail(data) or f"HTTP {status}"
        logger.warning("matrix: %s failed (HTTP %s): %s", what, status, detail)
        self._note_send_failure(
            _classify_matrix_error(status, data),
            detail,
            retry_after=_retry_after_seconds(data),
        )

    def send(self, out: Outbound) -> MsgHandle | None:
        """发文本（超长自动分片）；返回**最后一片**的句柄，失败返回 None。

        每片都是独立的 ``PUT .../send/m.room.message/{uuid4}``，事务 ID 兼作幂等键。
        """
        room_id = self._room_id(out.conversation_id)
        if not room_id:
            logger.warning("matrix: bad conversation_id %r", out.conversation_id)
            self._note_send_failure(SendError.BAD_FORMAT, "bad conversation_id")
            return None
        if not out.text:
            logger.warning("matrix: refusing to send empty text")
            self._note_send_failure(SendError.BAD_FORMAT, "empty text")
            return None
        if not self.homeserver or not self.access_token:
            logger.warning("matrix: homeserver/access_token missing; send refused")
            self._note_send_failure(
                SendError.BAD_FORMAT, "homeserver/access_token missing"
            )
            return None
        # prefix_fmt="" 与 telegram/slack/discord 保持一致：分段不额外加「（i/n）」，
        # 且 "".join(chunks) == 原文。
        chunks: List[str] = split_text(out.text, self.effective_max_length, prefix_fmt="")
        if len(chunks) > 1:
            logger.info("matrix: splitting outbound message into %d chunks", len(chunks))
        handle: MsgHandle | None = None
        for chunk in chunks:
            status, data = self._put_message(
                room_id,
                {"msgtype": TEXT_MSGTYPE, "body": chunk},
                out.conversation_id,
            )
            if status < 200 or status >= 300 or not data.get("event_id"):
                self._note_failure(status, data, "send")
                return handle if handle is not None else None
            handle = self._handle_from(
                room_id, out.conversation_id, data.get("event_id")
            )
            if handle is None:
                return None
        return handle

    def edit(self, handle: MsgHandle, out: Outbound) -> bool:
        """**兼容近似**：Matrix 没有标准的"编辑消息"API。

        本方法不发"真编辑"，而是再发一条 ``m.room.message``：

        * ``body`` 以 ``"* "`` 开头（不支持编辑的客户端会把它渲染成引用体）；
        * ``content["m.new_content"]`` 带新的 ``msgtype`` / ``body``；
        * ``content["m.relates_to"]`` 带 ``rel_type="m.replace"`` +
          ``event_id=<原事件>``（这是客户端真正把它认成"编辑"的关键）。

        已知局限：
        * 并非所有客户端都支持 MSC2676 回落写法；不支持的只会当成一条普通消息 ——
          内容不丢，但会多出一条。**调用方不应把它当成"原地改写"来做幂等判断。**
        * 新正文超过 :attr:`effective_max_length` 时**退化为发一条新消息**（普通
          ``send``，会自行分片），不再带编辑语义。
          **⚠️ 这里刻意与 telegram / discord / slack / mattermost / nextcloud 不一样，
          而且那五家才是"该抛"的那一类** —— 理由是本适配器根本**不在同一个族里**：
          ① Matrix 没有标准的编辑 API，本方法**自己就是那条 ``send``**，所以"退化
          成 send"在这里根本不是退化，它就是本方法平时的做法；② :data:`MESSAGE_LIMIT`
          是**我们自己选的保守值**（按事件体 64KB 上限反推），不是 Matrix 会拒收的
          阈值 —— 抛 ``ValueError`` 等于**拒绝投递一条 Matrix 乐意收下的消息**，
          那才是真把正文弄丢。
          反过来，那五家是真的会被**拒收**（discord 400/50035、telegram 400
          "message is too long"、slack ``msg_too_long``、mattermost 400
          ``model.post.is_valid.message_length.app_error``、nextcloud 413），所以它们
          在本地就抛、零请求。**别把这一家"改成一致"** —— 代价是矩阵上会丢正文，
          而且那五家改成这一家会把占位消息上已经显示的那一截**读两遍**
          （实测 4000 字读成 5500 字，见 ``tests/test_edit_length_guard.py``）。
          两族的分工写在 :meth:`~opencode_bridge.adapters.base.Adapter.edit`。
        * 因为带 ``m.relates_to``，本条消息会被自己的入站过滤跳过
          （见 :meth:`_handle_event`），不会形成回声。

        返回 ``True``/``False``，与基类契约及 ``core.py`` 的真值判定一致
        （``core.py`` 只判真假，不消费句柄）。注意本方法产生的是**新事件**，
        所以再想编辑同一处内容必须重新取句柄 —— 而调用方拿不到它，这是
        Matrix 兼容写法的固有限制，不要据此做"原地改写"的幂等假设。
        """
        room_id = self._room_id(handle.conversation_id)
        if not room_id or not handle.message_id:
            logger.warning("matrix: bad handle %r", handle)
            self._note_send_failure(SendError.BAD_FORMAT, "bad handle")
            return False
        if not out.text:
            logger.warning("matrix: refusing to edit with empty text")
            self._note_send_failure(SendError.BAD_FORMAT, "empty text")
            return False
        if len(out.text) > self.effective_max_length:
            logger.info(
                "matrix: edit body too long (%d > %d); degrading to plain send",
                len(out.text),
                self.effective_max_length,
            )
            return self.send(
                Outbound(
                    conversation_id=handle.conversation_id,
                    text=out.text,
                    kind=out.kind,
                    buttons=out.buttons,
                    session_id=out.session_id,
                )
            ) is not None
        new_content = {"msgtype": TEXT_MSGTYPE, "body": out.text}
        content = {
            "msgtype": TEXT_MSGTYPE,
            "body": f"* {out.text}",
            "m.new_content": dict(new_content),
            "m.relates_to": {
                "rel_type": "m.replace",
                "event_id": str(handle.message_id),
            },
        }
        status, data = self._put_message(room_id, content, handle.conversation_id)
        if status < 200 or status >= 300 or not data.get("event_id"):
            self._note_failure(status, data, "edit")
            return False
        self._handle_from(room_id, handle.conversation_id, data.get("event_id"))
        return True

    def answer(self, query_id: str, text: str = "") -> None:
        """Matrix 没有 callback query 概念 —— no-op（reactions 见 T3.x）。"""
        return None

"""Lane B — Matrix adapter (tasks.md T3.1). Standard library only.

传输层用 ``urllib.request``（零第三方依赖），全部调用收敛在单个可覆写的
``_request`` 方法后面，测试只需替换它即可离线验证协议层逻辑。

两个平台特有的点：

1. **增量同步游标**：``/_matrix/client/v3/sync`` 返回的 ``next_batch`` 是下一次
   增量同步的**唯一**凭据（不存在"拿最近 N 条"这种回放接口）。它保存在实例字段
   :attr:`MatrixAdapter._since` 上，跨多次调用存活；每次请求都带上，所以既不会
   漏消息也不会无限重放。

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

from ..hooks import Hooks, Inbound, MsgHandle, Outbound, SendError
from ..split import split_text
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
    """Matrix Client-Server API adapter（``/sync`` 入站 + ``send`` 出站）。"""

    name = "matrix"
    label = "Matrix"
    max_message_length = MESSAGE_LIMIT          # 取保守值（事件体整体上限 64KB）
    supports_inbound = True                     # /sync 长轮询
    supports_inline_buttons = False             # v1 不把 reactions 当交互
    supports_media = False                      # v1 只发 m.text

    # Matrix 没有 bot_token 的概念，凭据是 homeserver + access_token。
    # user_id 也列入必需：它是**过滤自己回声**的唯一依据，缺了会无限回环
    # （桥接把自己发出的消息再当成入站消息收回来）。
    required_tokens = ("homeserver", "access_token", "user_id")
    outbound_tokens = ("homeserver", "access_token")

    # 类级旋钮（测试可在实例上覆盖）。
    message_limit = MESSAGE_LIMIT
    min_interval = MIN_SEND_INTERVAL
    backoff_interval = BACKOFF_INTERVAL

    def __init__(self, config: dict, hooks: Hooks) -> None:
        super().__init__(config, hooks)
        # ``homeserver`` 允许带尾斜杠，这里统一去掉，避免拼出 "//_matrix/..."。
        self.homeserver: str = str(self.config.get("homeserver") or "").strip().rstrip("/")
        self.access_token: str = str(self.config.get("access_token") or "").strip()
        #: 自己的 MXID，用于过滤自己发出的回声（不配置就不做该过滤）。
        self.user_id: str = str(self.config.get("user_id") or "").strip()
        # allowed_chat_ids 已由基类 _init_access() 统一解析（T1.2）
        self.sync_timeout_ms = int(
            self.config.get("sync_timeout_ms") or SYNC_TIMEOUT_MS
        )
        #: ``next_batch`` 游标 —— Matrix 增量同步的核心，跨调用保存在实例上。
        self._since: str = str(self.config.get("since") or "").strip()
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
        self._stop_event.clear()
        thread = threading.Thread(
            target=self._inbound_loop, name="matrix-inbound", daemon=True
        )
        self._thread = thread
        thread.start()
        logger.info("matrix: sync loop started (since=%r)", self._since)

    # ------------------------------------------------------------------
    # Inbound (/sync long polling)
    # ------------------------------------------------------------------
    def _sync_path(self) -> str:
        """本次同步的请求路径（带上 :attr:`_since` 游标）。"""
        query = {"timeout": str(self.sync_timeout_ms)}
        if self._since:
            query["since"] = self._since
        return f"{SYNC_PATH}?{urllib.parse.urlencode(query)}"

    def _inbound_loop(self) -> None:
        """长轮询循环。**任何**单次异常都在此被捕获 —— 绝不让线程静默死掉。"""
        backoff = getattr(self, "backoff_interval", BACKOFF_INTERVAL)
        while not self._stop_event.is_set():
            try:
                ok = self._sync_once()
            except Exception:
                logger.exception("matrix: sync cycle failed")
                ok = False
            if not ok:
                if self._stop_event.wait(backoff):
                    break
            elif self._stop_event.wait(0.01):
                # 打桩 / 短响应时兜底限速，同时让 stop() 能立刻退出。
                break

    def _sync_once(self) -> bool:
        """一次 ``/sync``。返回 True 表示成功（游标已推进）。"""
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
            return False
        # 先推进游标再分发：分发里的单条异常不会让这条消息被无限重放。
        next_batch = str(data.get("next_batch") or "").strip()
        if next_batch:
            self._since = next_batch
        self._dispatch_sync(data)
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
        # 授权闸门必须在最前：未授权房间的消息不许进入上层（否则能用命令/审批字绕过）
        if not self.admits(room_id):
            logger.info("matrix: dropping message from non-whitelisted room %s", room_id)
            return
        try:
            self.hooks.on_inbound(
                Inbound(
                    conversation_id=self._conversation_id(room_id),
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
        return f"room:{room_id}"

    @staticmethod
    def _room_id(conversation_id: Any) -> Optional[str]:
        raw = str(conversation_id or "")
        if raw.startswith("room:"):
            raw = raw[len("room:"):]
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
        chunks: List[str] = split_text(out.text, self.message_limit, prefix_fmt="")
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
        * 新正文超过 :attr:`message_limit` 时**退化为发一条新消息**（普通
          ``send``，会自行分片），不再带编辑语义。
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
        if len(out.text) > self.message_limit:
            logger.info(
                "matrix: edit body too long (%d > %d); degrading to plain send",
                len(out.text),
                self.message_limit,
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

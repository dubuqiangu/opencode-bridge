"""Lane B — Telegram Bot API adapter (CONTRACT.md §2.2). Standard library only.

HTTP 是裸 ``http.client.HTTPSConnection`` + JSON 请求体，所有调用都收敛在单个
可覆写方法 ``_post`` 后面，测试只需替换它即可离线验证协议层逻辑。

A1：轮询循环 / 退避 / 线程 / ``stop()`` 语义已迁到
:class:`~opencode_bridge.transport.PollingTransport`。本文件只留 Telegram 语义：
``getUpdates`` 的 offset 游标、事件过滤、授权闸门、callback query 与按钮、
429 ``retry_after``、出站分片。

⚠️ ``conversation_id`` 前缀**本轮刻意仍是 ``chat:``**，不要"顺手改好"
------------------------------------------------------------------------
``identity.LEGACY_PREFIXES`` 里 ``chat`` → ``telegram`` 就是 telegram 自己的
旧别名（光看字符串完全合法，判不出来）。

把 :meth:`TelegramAdapter._conversation_id` 改成 ``identity.format_id("telegram", ...)``
会改变 ``conversation_id`` 的字符串格式，而 :class:`~opencode_bridge.state.StateStore`
拿它当**不透明键**存 ``conversation_id ↔ session_id`` 映射 —— 于是**已落盘
``state.json`` 里的所有 ``chat:`` 键会一次性变成孤儿**，用户会在迁移后一次性
"忘记"所有历史会话映射。这种故障**不报错**，只表现为"agent 突然记错上下文"，
比直接失败难查得多。

所以前缀切换必须是一个**单独的变更**，且必须与 ``state.py`` 的键迁移
（旧键 → 新键的显式重写 + 版本门控）**一起**发。在那之前
``_conversation_id`` 必须逐字节保持 ``chat:`` 前缀 ——
``tests/test_telegram.py`` 里有专门的用例把这一点钉死。

Telegram 特有的四点
------------------
1. **offset 游标**：``getUpdates`` 的 ``offset`` 是"确认到此为止"的凭据，服务端
   **只返回 ``update_id >= offset`` 的 update**（服务端侧去重，重复确认不会重复发）。
   它保存在 :attr:`TelegramAdapter._offset` 上，跨多次调用存活。
   **顺序铁律：先推进 offset，再分发** —— 否则分发里单条 update 抛异常就会让这批
   update 被无限重放（这是 Telegram 迁移里最容易搞错的一条）。

2. **两套过滤，别搞混**：``getUpdates`` 请求里的 ``allowed_updates=["message",
   "callback_query"]`` 是**服务端侧**订阅范围（"我不想收别的"）；
   :meth:`TelegramAdapter._dispatch_update` 里的 ``edited_message`` /
   ``channel_post`` / 非文本 / 白名单判定是**客户端侧**入站过滤
   （"收到了也要丢"）。两者是**不同机制**，迁移不许混为一谈 ——
   服务端不推 ≠ 客户端不需要过滤（白名单与"非文本"必须在客户端拦）。

3. **按钮是本平台独有的能力**（``supports_inline_buttons = True``，仓库里唯一）：
   callback query 的接收 → :meth:`TelegramAdapter.answer` 应答是一整套顺序敏感的
   动作（见 :meth:`TelegramAdapter._handle_callback`：投递 inbound → ``on_callback``
   → ``answerCallbackQuery``，且后两步在 ``finally`` 里，**投递崩了也要应答**）。
   这些**全部留在适配器**，传输层只搬走"怎么持续拿到 update"。

4. **长轮询的两层超时**：``getUpdates`` 的 ``timeout`` 是**服务端挂起时长**
   （Bot API 上限 50s），HTTP socket 超时必须**大于**它，否则每轮都会在服务端返回前
   被本地掐断。迁移前取 ``poll_timeout + 15``（默认 25 + 15 = 40s，
   :data:`POLL_SOCKET_TIMEOUT`），本轮逐字保留 —— 有专门的用例锁住这个大小关系。
"""

from __future__ import annotations

import http.client
import json
import logging
import threading
import time
from typing import Any, List, Optional

from ..hooks import Button, Hooks, Inbound, MsgHandle, Outbound, SendError
from ..split import split_text  # 统一分片实现（T1.4b），此处再导出保持向后兼容
from ..transport import NOTHING, PollingTransport
from .base import Adapter, classify_http, register

logger = logging.getLogger("opencode_bridge.adapters.telegram")

__all__ = ["TelegramAdapter", "split_text"]

API_HOST = "api.telegram.org"
API_PORT = 443
MESSAGE_LIMIT = 4096          # Telegram max message length in characters
CALLBACK_DATA_LIMIT = 64      # inline callback_data limit in bytes
MIN_SEND_INTERVAL = 1.2       # per-conversation send/edit throttle (seconds)
BACKOFF_INTERVAL = 2.0        # 一次 getUpdates 失败后的重试间隔（秒，恒定）
POLL_LONG_TIMEOUT = 25        # getUpdates long-poll seconds
POLL_SOCKET_TIMEOUT = 40.0    # socket timeout must be > POLL_LONG_TIMEOUT
#: 空轮（``ok`` 但 ``result`` 为空）后的防御性节流（秒）。
#: 迁移前是 :meth:`TelegramAdapter._poll_once` 末尾的 ``_stop_event.wait(0.05)``：
#: 服务端立刻返回空批时不做任何等待会打爆 API。走传输层后它就是
#: ``PollingTransport`` 的 ``idle_sleep``（仍然是**可被 stop 打断**的
#: ``Event.wait``，不是 ``time.sleep``）。
EMPTY_ROUND_INTERVAL = 0.05
DEFAULT_SOCKET_TIMEOUT = 30.0
MAX_RETRY_AFTER = 60.0        # cap for Telegram 429 retry_after sleeps


def _retry_after(data: Any) -> Optional[float]:
    """Extract a 429 ``retry_after`` delay from a Telegram response body."""
    if not isinstance(data, dict):
        return None
    params = data.get("parameters")
    delay: Any = None
    if isinstance(params, dict):
        delay = params.get("retry_after")
    if delay is None and data.get("error_code") == 429:
        delay = 1
    if isinstance(delay, bool) or not isinstance(delay, (int, float)):
        return None
    if delay < 0:
        return None
    return min(float(delay), MAX_RETRY_AFTER)


@register("telegram")
class TelegramAdapter(Adapter):
    """Long-polling ``getUpdates`` Telegram adapter。

    线程与退避归 :class:`~opencode_bridge.transport.PollingTransport`；
    :attr:`running` / :meth:`stop` 是它的代理。

    ⚠️ ``_conversation_id`` 刻意仍产出 ``chat:`` 前缀 —— 见模块 docstring
    「``conversation_id`` 前缀本轮刻意仍是 ``chat:``」一节（切换的前置条件是
    ``state.py`` 的键迁移，本轮不做）。
    """

    name = "telegram"
    label = "Telegram"
    max_message_length = MESSAGE_LIMIT          # Bot API: 4096 字符
    supports_inbound = True
    supports_inline_buttons = True              # 仓库里唯一支持 inline 按钮的平台
    supports_media = True

    # Class-level knobs (tests may override them on the instance).
    message_limit = MESSAGE_LIMIT
    min_interval = MIN_SEND_INTERVAL
    backoff_interval = BACKOFF_INTERVAL

    def __init__(self, config: dict, hooks: Hooks) -> None:
        super().__init__(config, hooks)
        self.bot_token: str = str(self.config.get("bot_token") or "").strip()
        # allowed_chat_ids 已由基类 _init_access() 统一解析（T1.2）
        self.poll_long_timeout = int(
            self.config.get("poll_timeout") or POLL_LONG_TIMEOUT
        )
        self._offset = 0
        self._api_lock = threading.Lock()          # serializes _post calls
        self._throttle_lock = threading.Lock()     # guards _last_send
        self._last_send: dict[str, float] = {}
        self._transport: Optional[PollingTransport] = None
        #: 已取到、还没分发的 update 批次（getUpdates 一次最多回 100 条，
        #: 而传输层的 fetch 一次只交**一条**，所以批量挂在这里逐条取）。
        #: 只被消费线程读写（测试里由 :meth:`_poll_once` 单线程读写）。
        self._pending: list[dict] = []

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------
    def _post(
        self, method: str, payload: Optional[dict] = None, *, timeout: Optional[float] = None
    ) -> dict:
        """POST ``https://api.telegram.org/bot<token>/<method>`` (JSON body).

        Always returns a dict; network / decode failures are mapped to
        ``{"ok": False, "error_code": ..., "description": ...}`` so callers
        never see an exception from this layer unless they want to.
        """
        body = json.dumps(payload if payload is not None else {}).encode("utf-8")
        path = f"/bot{self.bot_token}/{method}"
        sock_timeout = timeout if timeout is not None else DEFAULT_SOCKET_TIMEOUT
        status = 0
        raw = b""
        conn = http.client.HTTPSConnection(API_HOST, API_PORT, timeout=sock_timeout)
        try:
            conn.request(
                "POST",
                path,
                body=body,
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Accept": "application/json",
                },
            )
            resp = conn.getresponse()
            status = resp.status
            raw = resp.read()
        except Exception as exc:
            return {
                "ok": False,
                "error_code": status or 0,
                "description": f"transport error: {exc}",
            }
        finally:
            try:
                conn.close()
            except Exception:
                pass
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return {
                "ok": False,
                "error_code": status,
                "description": f"non-JSON response (HTTP {status}): {raw[:200]!r}",
            }
        if not isinstance(data, dict):
            return {"ok": False, "error_code": status, "description": "unexpected payload"}
        if "error_code" not in data and status >= 400:
            data["error_code"] = status
        return data

    def _api(
        self, conversation_id: str, method: str, payload: dict, *, timeout: Optional[float] = None
    ) -> dict:
        """Throttled + 429-retried wrapper around ``_post``.

        Returns the final response dict; on transport failure returns
        ``{"ok": False}``. Never raises.
        """
        self._throttle(conversation_id)
        try:
            with self._api_lock:
                data = self._post(method, payload, timeout=timeout)
        except Exception:
            logger.exception("telegram: %s request failed", method)
            return {"ok": False, "error_code": 0, "description": "transport error"}
        if isinstance(data, dict) and data.get("ok") is False:
            delay = _retry_after(data)
            if delay is not None:
                logger.warning(
                    "telegram: %s rate-limited, sleeping %.1fs and retrying once",
                    method,
                    delay,
                )
                self._mark_backoff(conversation_id)
                time.sleep(delay)
                try:
                    with self._api_lock:
                        data = self._post(method, payload, timeout=timeout)
                except Exception:
                    logger.exception("telegram: %s retry failed", method)
                    return {"ok": False, "error_code": 0, "description": "transport error"}
        # No generic failure logging here: callers decide which errors are
        # silent (e.g. editMessageText "message is not modified").
        return data if isinstance(data, dict) else {"ok": False}

    def _throttle(self, conversation_id: str) -> None:
        """Enforce a minimum interval between send/edit per conversation."""
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

    def _mark_backoff(self, conversation_id: str) -> None:
        """After a 429, push the next allowed send further out (2s)."""
        backoff = getattr(self, "backoff_interval", BACKOFF_INTERVAL)
        with self._throttle_lock:
            self._last_send[conversation_id] = time.monotonic() + backoff - self.min_interval

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
        # 三处间隔**逐字对齐迁移前**的 ``_poll_loop`` / ``_poll_once``：
        #   * min_backoff —— getUpdates 调用**失败**（``ok`` 不为 True）后的重试间隔。
        #     迁移前是 ``self._stop_event.wait(2.0)``：一个字面量 2s，
        #     **没有指数增长**。所以 max_backoff 必须与 min 同值，
        #     否则会静默变成"2s → 4s → 8s…"的指数退避（= 行为变更）。
        #   * max_backoff —— 与 min 相同 ⇒ 退避**恒定**。
        #   * idle_sleep —— **空轮**（成功但 result 为空）后的 0.05s 防御性节流，
        #     迁移前在 ``_poll_once`` 末尾（同样是用 Event.wait，可被 stop 打断）。
        # reset_after=0 ⇒ 只要 fetch 成功过一次就重置退避（连上即重置 = 既有语义）。
        backoff = float(self.backoff_interval)
        return PollingTransport(
            self._fetch_update,
            idle_sleep=EMPTY_ROUND_INTERVAL,
            name="telegram",
            min_backoff=backoff,
            max_backoff=backoff,
            reset_after=0.0,
        )

    def start(self) -> None:
        """Verify the token, flush pending updates, spawn the poller.

        Missing / invalid token: log a warning and return (never raises).
        """
        if not self.bot_token:
            logger.warning("telegram: bot_token missing; adapter not started")
            return
        try:
            me = self._post("getMe", {}, timeout=8.0)
        except Exception as exc:
            logger.warning("telegram: getMe failed (%s); adapter not started", exc)
            return
        if not isinstance(me, dict) or me.get("ok") is not True:
            if isinstance(me, dict):
                code = me.get("error_code")
                desc = me.get("description")
            else:
                code = "?"
                desc = repr(me)
            logger.warning(
                "telegram: getMe failed (code=%s): %s; adapter not started",
                code,
                desc,
            )
            return
        try:
            self._flush_pending()
        except Exception:
            logger.exception("telegram: failed to flush pending updates")
        self._stop_event.clear()
        # _flush_pending 刚把历史全丢了 ⇒ 手上没分发的旧 update 也不能再投。
        self._pending.clear()
        transport = self._make_transport()
        self._transport = transport
        transport.start(self._on_update)
        logger.info("telegram: polling started (offset=%s)", self._offset)

    def stop(self) -> None:
        """置停止位 → 关传输层 → join（**幂等**）。

        迁移前是 ``Adapter.stop()`` 置位 + join 5s；退避等待与空轮节流都用
        ``_stop_event.wait(...)``，所以都能被立刻打断。现在这两段都在
        :class:`~opencode_bridge.transport.PollingTransport` 里，语义不变。

        ⚠️ 已知限制（迁移前就有，不是本次引入的）：``stop()`` **打不断**正在挂起的
        ``getUpdates`` HTTP 请求（``http.client`` 没有可关的句柄）—— 最长要等
        ``poll_timeout + 15``（默认 40s）那一轮自己返回，join 会在 5s 处超时返回，
        线程（daemon）随后自然退出。至少不会泄漏线程。
        """
        super().stop()                      # 置停止位（_throttle 依赖它）
        transport, self._transport = self._transport, None
        if transport is not None:
            transport.stop()

    def _flush_pending(self) -> None:
        """Drop history: read the last update_id and skip everything before it."""
        data = self._post(
            "getUpdates", {"limit": 1, "offset": -1}, timeout=DEFAULT_SOCKET_TIMEOUT
        )
        if not isinstance(data, dict) or data.get("ok") is not True:
            logger.warning(
                "telegram: could not flush pending updates: %s",
                (data or {}).get("description"),
            )
            return
        results = data.get("result") or []
        if results:
            try:
                self._offset = int(results[-1].get("update_id")) + 1
            except (TypeError, ValueError):
                logger.warning("telegram: unexpected flush payload: %r", results[-1])

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------
    def _fetch_update(self) -> Any:
        """传输层的 fetch：**一条** update；这轮没有就返回 :data:`NOTHING`。

        ``getUpdates`` 一次最多回 100 条，而传输层的 fetch 一次只交**一条**，
        所以批量先挂进 :attr:`_pending` 再逐条取（与迁移前"一批处理完再发下一轮"
        顺序一致）。``_pending`` 空了才发下一轮请求。

        两条退出路径对应迁移前 ``_poll_loop`` 的两种等待，**不能混**：

        * 空轮（``ok`` 但没有 update）→ :data:`NOTHING` → 传输层按
          ``idle_sleep``（:data:`EMPTY_ROUND_INTERVAL` = 0.05s）节流后重问；
        * 请求失败 → :meth:`_poll_round` 抛异常 → 传输层按
          ``min_backoff``（:data:`BACKOFF_INTERVAL` = 2s）退避后重连。
        """
        if not self._pending:
            self._poll_round()
        if not self._pending:
            return NOTHING
        return self._pending.pop(0)

    def _poll_round(self) -> None:
        """一轮 ``getUpdates``：把 ``result`` 装进 :attr:`_pending`。失败抛异常。

        **抛异常 = 传输层故障**（基类会关连接、退避、从头再来），这与迁移前
        ``_poll_loop`` 里 ``_poll_once()`` 返回 False 后 ``wait(2.0)`` 是同一件事，
        只是把"等多久"交给传输层的退避状态机。

        offset 一字不动：失败轮不会推进任何游标（**铁律**）。
        """
        payload = {
            "timeout": self.poll_long_timeout,
            "offset": self._offset,
            # 服务端侧订阅范围（不是入站过滤）：只要 message + callback_query。
            "allowed_updates": ["message", "callback_query"],
            "limit": 100,
        }
        # socket 超时必须 > 服务端挂起时长（poll_timeout），否则会在服务端
        # 返回前先被本地掐断。迁移前是 ``self.poll_long_timeout + 15.0``（默认
        # 25 + 15 = 40s = POLL_SOCKET_TIMEOUT），逐字保留。
        data = self._post(
            "getUpdates", payload, timeout=self.poll_long_timeout + 15.0
        )
        if not isinstance(data, dict) or data.get("ok") is not True:
            code = (data or {}).get("error_code") if isinstance(data, dict) else None
            desc = (data or {}).get("description") if isinstance(data, dict) else None
            logger.warning(
                "telegram: getUpdates failed (code=%s): %s", code, desc
            )
            raise RuntimeError(f"getUpdates failed (code={code}): {desc}")
        # 非 dict 元素直接丢掉（迁移前循环里的 ``continue``）。
        self._pending = [u for u in (data.get("result") or []) if isinstance(u, dict)]

    def _on_update(self, update: Any) -> None:
        """传输层交给我们的**一条** update。

        **顺序铁律：先推进 offset，再分发** —— 这样分发里单条处理抛异常也不会让
        这一批 update 被无限重放（下一轮 ``getUpdates`` 已经带着新的 offset，
        服务端不会把那批再发一遍）。这段 try/except 与迁移前的循环体逐字一致：
        单条失败只记日志、继续处理同批的下一条。
        """
        if not isinstance(update, dict):
            return
        try:
            self._advance_offset(update)
            self._dispatch_update(update)
        except Exception:
            logger.exception("telegram: failed to handle update %r", update)

    def _poll_once(self) -> bool:
        """跑完一轮（请求一批 → 逐条「先推进 offset 再分发」）。返回 True 表示成功。

        这是 :meth:`_fetch_update` 的"永不抛异常"组合形态，供测试与手动诊断
        （拉一次看看 offset 对不对）使用；常驻轮询走传输层。
        """
        try:
            self._poll_round()
        except Exception:
            logger.exception("telegram: poll cycle failed")
            return False                      # 失败：offset 一字不动
        while self._pending:
            self._on_update(self._pending.pop(0))
        return True

    def _advance_offset(self, update: dict) -> None:
        try:
            update_id = int(update.get("update_id"))
        except (TypeError, ValueError):
            return
        if update_id + 1 > self._offset:
            self._offset = update_id + 1

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------
    def _dispatch_update(self, update: dict) -> None:
        cq = update.get("callback_query")
        if isinstance(cq, dict):
            self._handle_callback(cq)
            return
        # edited_message / channel_post / edited_channel_post have no
        # "message" key here -> they simply fall through and are ignored.
        message = update.get("message")
        if not isinstance(message, dict):
            return
        text = message.get("text")
        if not isinstance(text, str) or text == "":
            # Stickers, photos, voice notes, caption-only media: ignored.
            return
        chat = message.get("chat")
        if not isinstance(chat, dict) or chat.get("id") is None:
            return
        conversation_id = self._conversation_id(chat.get("id"))
        if not self._allowed(chat.get("id")):
            logger.info("telegram: dropped message from non-whitelisted chat %s", chat.get("id"))
            return
        from_user = message.get("from") or {}
        user_id = str(from_user.get("id")) if isinstance(from_user, dict) else None
        inbound = Inbound(
            conversation_id=conversation_id,
            text=text,
            kind="text",
            user_id=user_id,
            message_id=str(message.get("message_id"))
            if message.get("message_id") is not None
            else None,
            platform=self.name,
            raw=message,
        )
        self.hooks.on_inbound(inbound)

    def _handle_callback(self, cq: dict) -> None:
        """按钮点击（``callback_query``）—— 本平台**独有**的能力，整体留在适配器。

        顺序敏感，且是**行为契约**：① 投递 inbound → ② ``on_callback`` →
        ③ ``answerCallbackQuery``。后两步在 ``finally`` 里：即使用户回调抛异常、
        即使 ``on_callback`` 抛异常，**也必须**给 Telegram 应答，否则那个转圈圈会
        一直挂在用户界面上（Bot API 认为 query 未被 ack）。
        """
        data = cq.get("data")
        query_id = cq.get("id")
        chat_id = None
        message = cq.get("message")
        if isinstance(message, dict):
            chat = message.get("chat")
            if isinstance(chat, dict):
                chat_id = chat.get("id")
        if chat_id is None:
            from_user = cq.get("from")
            if isinstance(from_user, dict):
                chat_id = from_user.get("id")
        if chat_id is None or not self._allowed(chat_id):
            if chat_id is not None:
                logger.info(
                    "telegram: dropped callback from non-whitelisted chat %s", chat_id
                )
            else:
                logger.warning("telegram: callback without chat context: %r", cq)
            return
        conversation_id = self._conversation_id(chat_id)
        payload = data if isinstance(data, str) else ""
        # 1) push the event into the core, 2) notify the core, 3) ack Telegram.
        try:
            from_user = cq.get("from")
            user_id = (
                str(from_user.get("id")) if isinstance(from_user, dict) else None
            )
            inbound = Inbound(
                conversation_id=conversation_id,
                text=payload,
                kind="callback",
                user_id=user_id,
                message_id=str(message.get("message_id"))
                if isinstance(message, dict) and message.get("message_id") is not None
                else None,
                callback_query_id=str(query_id) if query_id is not None else None,
                platform=self.name,
                raw=cq,
            )
            try:
                self.hooks.on_inbound(inbound)
            except Exception:
                logger.exception("telegram: on_inbound hook failed for callback")
        finally:
            try:
                if query_id is not None:
                    self.hooks.on_callback(conversation_id, payload, str(query_id))
            except Exception:
                logger.exception("telegram: on_callback hook failed")
            try:
                if query_id is not None:
                    self.answer(str(query_id))
            except Exception:
                logger.exception("telegram: answer failed for query %s", query_id)

    def answer(self, query_id: str, text: str = "") -> None:
        """Acknowledge a callback query (``answerCallbackQuery``). Never raises."""
        if not query_id:
            return
        payload: dict = {"callback_query_id": query_id}
        if text:
            payload["text"] = text
        try:
            data = self._post("answerCallbackQuery", payload, timeout=DEFAULT_SOCKET_TIMEOUT)
        except Exception:
            logger.exception("telegram: answerCallbackQuery failed")
            return
        if isinstance(data, dict) and data.get("ok") is not True:
            logger.debug(
                "telegram: answerCallbackQuery not ok: %s", data.get("description")
            )

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------
    @staticmethod
    def _conversation_id(chat_id: Any) -> str:
        """``chat_id`` → ``chat:...``。

        ⚠️ **不要**改成 ``identity.format_id("telegram", chat_id)``。``chat`` 在
        :data:`identity.LEGACY_PREFIXES` 里是 telegram 自己的旧别名，切换会让已落盘
        ``state.json`` 的会话映射键全部失效 —— 前置条件是 ``state.py`` 的键迁移，
        本轮不做。见模块 docstring「``conversation_id`` 前缀本轮刻意仍是 ``chat:``」。
        """
        return f"chat:{chat_id}"

    @staticmethod
    def _chat_id(conversation_id: str) -> Optional[int]:
        raw = conversation_id
        if raw.startswith("chat:"):
            raw = raw[len("chat:"):]
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def _allowed(self, chat_id: Any) -> bool:
        """入站闸门：委托基类统一判定（T1.2，语义与旧实现一致）。"""
        return self.admits(chat_id)

    def send(self, out: Outbound) -> MsgHandle | None:
        """Send text (split at 4096); returns the handle of the LAST chunk."""
        chat_id = self._chat_id(out.conversation_id)
        if chat_id is None:
            logger.warning("telegram: bad conversation_id %r", out.conversation_id)
            self._note_send_failure(SendError.BAD_FORMAT, "bad conversation_id")
            return None
        if not out.text:
            logger.warning("telegram: refusing to send empty text")
            self._note_send_failure(SendError.BAD_FORMAT, "empty text")
            return None
        # prefix_fmt="" 保持既有出站行为（分段不加「（i/n）」前缀，且
        # "".join(chunks) == text）。前缀编号是 split.py 的可选能力，
        # 是否默认开启见 tasks.md T1.4 的后续决策。
        chunks = split_text(out.text, self.message_limit, prefix_fmt="")
        if len(chunks) > 1:
            logger.info(
                "telegram: splitting outbound message into %d chunks", len(chunks)
            )
        handle: MsgHandle | None = None
        for chunk in chunks:
            payload = {
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": True,
            }
            data = self._api(out.conversation_id, "sendMessage", payload)
            if not isinstance(data, dict) or data.get("ok") is not True:
                code = data.get("error_code") if isinstance(data, dict) else 0
                desc = (data.get("description") if isinstance(data, dict) else "") or ""
                logger.warning(
                    "telegram: sendMessage failed (code=%s): %s",
                    code if code is not None else "?",
                    desc or data,
                )
                params = data.get("parameters") if isinstance(data, dict) else None
                retry_after = None
                if isinstance(params, dict):
                    retry_after = params.get("retry_after")
                try:
                    self._note_send_failure(
                        classify_http(int(code or 0), str(desc)),
                        str(desc),
                        retry_after=float(retry_after) if retry_after else None,
                    )
                except (TypeError, ValueError):
                    self._note_send_failure(SendError.UNKNOWN, str(desc))
                if handle is None:
                    return None
                return handle  # partial send: keep the last good handle
            result = data.get("result") or {}
            message_id = result.get("message_id")
            if message_id is None:
                return handle
            handle = MsgHandle(
                conversation_id=out.conversation_id,
                message_id=str(message_id),
                platform=self.name,
            )
        return handle

    def edit(self, handle: MsgHandle, out: Outbound) -> bool:
        """Edit a message (with optional single-column inline keyboard).

        Raises ``ValueError`` when the text exceeds 4096 characters — per
        CONTRACT.md §2.2 the caller is responsible for truncating.
        """
        if len(out.text) > self.message_limit:
            raise ValueError(
                f"telegram edit text too long: {len(out.text)} > {self.message_limit}"
            )
        if not out.text:
            logger.warning("telegram: refusing to edit with empty text")
            return False
        chat_id = self._chat_id(handle.conversation_id)
        if chat_id is None:
            logger.warning(
                "telegram: bad conversation_id %r in handle", handle.conversation_id
            )
            return False
        try:
            message_id = int(handle.message_id)
        except (TypeError, ValueError):
            logger.warning("telegram: bad message_id %r in handle", handle.message_id)
            return False
        payload: dict = {"chat_id": chat_id, "message_id": message_id, "text": out.text}
        if out.buttons:
            # Single column: one button per row (CONTRACT.md §2.2).
            payload["inline_keyboard"] = [[_button_row(b)] for b in out.buttons]
        data = self._api(handle.conversation_id, "editMessageText", payload)
        if not isinstance(data, dict):
            return False
        if data.get("ok") is True:
            return True
        description = str(data.get("description") or "")
        if "message is not modified" in description:
            return False  # silent: identical content is not an error
        code = data.get("error_code")
        if isinstance(code, int) and 400 <= code < 500:
            logger.warning(
                "telegram: editMessageText rejected (HTTP %s): %s", code, description
            )
        else:
            logger.warning(
                "telegram: editMessageText failed (code=%s): %s", code, description
            )
        return False


def _button_row(button: Button) -> dict:
    """Serialize a Button to a Telegram inline keyboard button (<= 64 bytes)."""
    data = button.data.encode("utf-8")[:CALLBACK_DATA_LIMIT].decode(
        "utf-8", errors="ignore"
    )
    return {"text": button.label, "callback_data": data}

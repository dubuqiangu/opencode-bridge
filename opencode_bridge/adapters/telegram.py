"""Lane B — Telegram Bot API adapter (CONTRACT.md §2.2). Standard library only.

HTTP 是裸 ``http.client.HTTPSConnection`` + JSON 请求体，所有调用都收敛在单个
可覆写方法 ``_post`` 后面，测试只需替换它即可离线验证协议层逻辑。

A1：轮询循环 / 退避 / 线程 / ``stop()`` 语义已迁到
:class:`~opencode_bridge.transport.PollingTransport`。本文件只留 Telegram 语义：
``getUpdates`` 的 offset 游标、事件过滤、授权闸门、callback query 与按钮、
429 ``retry_after``、出站分片。

``conversation_id`` 前缀：已从 ``chat:`` 切到 ``telegram:``
-------------------------------------------------------
:meth:`TelegramAdapter._conversation_id` 现在走
``identity.format_id("telegram", ...)``，产出统一格式 ``platform:local_id``。

切换的前置条件（旧前缀期间一直挂着的那条警告）已经满足：
:class:`~opencode_bridge.state.StateStore` 的键迁移
（旧键 → 新键的显式重写 + 版本门控）已上线，且 ``__main__`` **打开了**
``migrate_keys=True``。这两件事**必须同一个变更**：只切前缀而不开迁移，
``state.json`` 里所有 ``chat:`` 键会一次性变成孤儿，用户会在升级后一次性
"忘记"所有历史会话映射 —— 不报错，只表现为"agent 突然记错上下文"。

⚠️ **反向解析仍认旧前缀**（见 :attr:`TelegramAdapter._CONVERSATION_PREFIXES`）：
写前收件箱把 ``conversation_id`` **持久化**在 SQLite 里，升级前写入、
升级后才重放的未投递消息带着 ``chat:`` 前缀。认不出来就等于把那些回复永久丢弃。
``state.py`` 的"精确键 → 无歧义别名"回退是同一类问题的另一半，两者都要留到
slack / discord / mattermost 也切完之后才谈得上删。

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

from .. import health
from ..hooks import Button, Hooks, Inbound, MsgHandle, Outbound, SendError
from ..identity import format_id
from ..split import split_text  # 统一分片实现（T1.4b），此处再导出保持向后兼容
from ..transport import NOTHING, PollingTransport
from ._redactable_ids import redactable_id
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

    ``_conversation_id`` 产出统一格式 ``telegram:<chat_id>``（已从 ``chat:`` 切过来），
    ``_chat_id`` 仍认旧前缀 —— 见模块 docstring「``conversation_id`` 前缀」一节。
    """

    name = "telegram"
    label = "Telegram"
    max_message_length = MESSAGE_LIMIT          # Bot API: 4096 字符
    supports_inbound = True
    #: principal = chat id：(a) 会话唯一且稳定，(b) 用户在 Telegram 里能直接看到它，
    #: (c) 平台对发件人做过认证 —— 三条判据都成立。
    pairing_supported = True
    supports_inline_buttons = True              # 仓库里唯一支持 inline 按钮的平台
    supports_media = True
    #: ``editMessageText`` 真能把占位气泡顶成最终答复，所以 ``⏳ 处理中…`` 发得。
    supports_message_edit = True

    # Class-level knobs (tests may override them on the instance).
    min_interval = MIN_SEND_INTERVAL
    backoff_interval = BACKOFF_INTERVAL

    #: ``conversation_id`` 的合法前缀：当前格式 + 切换前的旧别名（``chat:``）。
    #: :meth:`_chat_id` 按这个列表剥前缀，所以**旧前缀必须继续认** ——
    #: 写前收件箱把 ``conversation_id`` 持久化在盘上，切换前写入、切换后才重放的
    #: 未投递消息带着 ``chat:``，认不出来就等于把那些回复永久丢弃。
    #: 与 :data:`identity.LEGACY_PREFIXES` 同源（``chat`` → ``telegram``）。
    _CONVERSATION_PREFIXES = ("telegram:", "chat:")

    def __init__(self, config: dict, hooks: Hooks) -> None:
        super().__init__(config, hooks)
        self.bot_token: str = str(self.config.get("bot_token") or "").strip()
        # allowed_chat_ids 已由基类 _init_access() 统一解析（T1.2）
        self.poll_long_timeout = self._coerce_poll_timeout()
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
    # 配置解析
    # ------------------------------------------------------------------
    def _coerce_poll_timeout(self) -> int:
        """``poll_timeout``（getUpdates 长轮询秒数）的取值。

        **没配** -> :data:`POLL_LONG_TIMEOUT`（静默）；**配了但非法** ->
        :data:`POLL_LONG_TIMEOUT` 并**告警**。纪律照抄本仓库既有的两处同款：
        :func:`opencode_bridge.adapters.a2a._coerce_positive`（"说清是哪个键、
        收到了什么、回落成多少"）与
        :meth:`opencode_bridge.adapters.nextcloud.NextcloudAdapter._config_int_value`
        （"配置非法 %r，按 %s 处理"）。⚠️ 不替用户决定成别的值：非正数也回落，
        而**不**静默采纳 —— 一个写错的长轮询时长会让 socket 超时的大小关系
        （:data:`POLL_SOCKET_TIMEOUT` 必须大于它）失效。

        ⚠️ **这里绝对不许让 ``int()`` 的异常逃出去**：``base.build`` 会把它包成
        :class:`~opencode_bridge.adapters.base.AdapterError`，``__main`` 那个循环
        ``continue`` 掉这个适配器 ⇒ 整个桥 ``usable == 0``，而用户看到的报错是
        「没有任何可用适配器」—— **一个字都不提 ``poll_timeout`` 非法**。
        也就是说，一个旋钮写错会打死这条零容错关键路径，而真正的死因不在错误
        信息里、只在日志的一行 ``failed to build adapter`` 里。
        """
        raw = self.config.get("poll_timeout")
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return POLL_LONG_TIMEOUT
        try:
            seconds = int(raw)
        except (TypeError, ValueError):
            logger.warning(
                "telegram: 配置项 poll_timeout=%r 不是整数（%s），已回落为 %d",
                raw, type(raw).__name__, POLL_LONG_TIMEOUT,
            )
            return POLL_LONG_TIMEOUT
        if seconds <= 0:
            logger.warning(
                "telegram: 配置项 poll_timeout=%r 非法（非正数），已回落为 %d",
                raw, POLL_LONG_TIMEOUT,
            )
            return POLL_LONG_TIMEOUT
        return seconds

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

        ⚠️ **每个失败分支都调 :meth:`~opencode_bridge.adapters.base.Adapter.report_startup_probe`**
        —— ``getMe`` 失败曾经只留一行日志、适配器直接 ``return``，而
        ``--status`` / ``--setup --json`` 只看 token 字符串非空就报"已配置 / 入站就绪"
        ⇒ **token 打错、被吊销、或网络被墙时，桥完全静默而状态视图说一切正常**。
        现在结论被 :mod:`opencode_bridge.health` 落盘并由那两个视图读出来。

        ⚠️ 本方法里那两行 ``telegram: getMe failed ...`` **原文保留**：用户与文档
        （``docs/install.md`` / ``plugin/README.md``）都按这行字排障，改了会让
        已写的排障指引失效。所以基类那条规范化日志**不取代**它，两条并存。
        """
        if not self.bot_token:
            logger.warning("telegram: bot_token missing; adapter not started")
            self.report_startup_probe(
                health.VERDICT_SKIPPED, detail="bot_token 没填，连 token 都没得验"
            )
            return
        try:
            me = self._post("getMe", {}, timeout=8.0)
        except Exception as exc:
            logger.warning("telegram: getMe failed (%s); adapter not started", exc)
            # ``_post`` 自己会把传输失败包成 ``{"ok": False, "error_code": 0, …}``
            # —— 所以这里的 ``code=0`` 与那条路径**同一个含义**：没拿到 HTTP 状态码
            # （同 ``classify_http`` 对 ``status <= 0`` 的判法）。不是"没有码"。
            self.report_startup_probe(
                health.VERDICT_FAILED,
                code=0,
                detail=f"getMe 抛出异常 {type(exc).__name__}: {exc}",
            )
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
            # 这一条分支同时覆盖两种失败：平台明确回的 API 错误
            # （``ok:false`` + error_code/description），以及 ``_post`` 把传输层
            # 异常包成的 ``error_code: 0`` —— 两者都要落盘，否则"网络被墙"
            # 这种最常见的失败恰恰不在状态视图里。
            self.report_startup_probe(
                health.VERDICT_FAILED,
                code=code,
                detail=f"getMe: {desc or '（平台没给描述）'}",
            )
            return
        self.report_startup_probe(health.VERDICT_OK, detail="getMe 通过")
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
        if not self._allowed(chat.get("id")) and not self.answer_pairing_request(
            chat.get("id"), conversation_id, text
        ):
            logger.info(
                "telegram: dropped message from non-whitelisted chat %s",
                redactable_id(self.name, chat.get("id")),
            )
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
        if chat_id is None:
            # 只记**载荷的形状**，绝不记**载荷的内容**。
            # `keys=` 是排序后的顶层键名 —— 排障真正要的就是"有没有 message／
            # 有没有 from"，它不含任何用户数据；`from=` 取不到时由 redactable_id
            # 自己落到 MISSING_ID（见 _redactable_ids 的行为 1）。
            # ⛔ 绝不记 data 的值、from.username、first_name/last_name、
            # message.text 或 message 里任何 chat 字段：那是 PII **加上**
            # 用户自己那段正文，而这一行的用途只是解释"为什么没找到 chat"。
            from_user = cq.get("from")
            logger.warning(
                "telegram: callback without chat context: from=%s keys=%s",
                redactable_id(
                    self.name, from_user.get("id") if isinstance(from_user, dict) else None
                ),
                tuple(sorted(cq)) if isinstance(cq, dict) else (),
            )
            return
        conversation_id = self._conversation_id(chat_id)
        if not self._allowed(chat_id) and not self.answer_pairing_request(
            chat_id, conversation_id, None
        ):
            logger.info(
                "telegram: dropped callback from non-whitelisted chat %s",
                redactable_id(self.name, chat_id),
            )
            return
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
        """``chat_id`` → ``telegram:...``（统一 ``platform:local_id`` 格式）。"""
        return format_id("telegram", chat_id)

    @staticmethod
    def _chat_id(conversation_id: str) -> Optional[int]:
        """``conversation_id`` → 数字 ``chat_id``；不认得就返回 ``None``。

        裸 ``chat_id``（``"55"``）也认，切换前后的两种前缀都认 ——
        理由见 :attr:`TelegramAdapter._CONVERSATION_PREFIXES`。
        """
        raw = str(conversation_id or "")
        for prefix in TelegramAdapter._CONVERSATION_PREFIXES:
            if raw.startswith(prefix):
                raw = raw[len(prefix):]
                break
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
        chunks = split_text(out.text, self.effective_max_length, prefix_fmt="")
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
        if len(out.text) > self.effective_max_length:
            raise ValueError(
                f"telegram edit text too long: {len(out.text)} > {self.effective_max_length}"
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

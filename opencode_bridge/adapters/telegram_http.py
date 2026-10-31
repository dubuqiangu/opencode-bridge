"""Telegram 出站 HTTP / 节流 / 发送预算域（§5.1 类手术：自 ``telegram.py`` 的
``TelegramAdapter`` 抽出）。

本模块持有 ``TelegramAdapter`` 三条出站闸门的完整实现：

- :meth:`TelegramHttp._post` —— 裸 ``http.client`` POST（可被测试替换的那道缝）；
- :meth:`TelegramHttp._api` —— 节流 + 429 重试的包装；
- :meth:`TelegramHttp._throttle` / :meth:`TelegramHttp._mark_backoff` /
  :meth:`TelegramHttp._await_send_budget` —— 每聊天最小间隔、429 退避与每聊天
  每分钟新建消息预算。

5 个方法自 ``TelegramAdapter`` **逐字节移植**（AST / 文本同一性由手术脚本逐函数
断言），除 ``self`` → ``self._adapter`` 的重写外一个字符未动。

**协作解析**：适配器留守的成员（``bot_token`` / ``_api_lock`` / ``_throttle_lock`` /
``_last_send`` / ``_send_history`` / ``send_limit_per_minute`` / ``min_interval`` /
``backoff_interval`` / ``_stop_event`` / ``_BILLABLE_METHODS``）经 ``self._adapter.X``
**逐调用**解析 —— 与手术前 ``self.X`` 在适配器实例上动态解析的语义完全一致。

⭐ **组内互相调用也必须经适配器**：``_api`` 里的 ``_throttle`` /
``_await_send_budget`` / ``_post`` / ``_mark_backoff`` 一律走
``self._adapter.<名>``，⛔ 不是本类自己的 ``self.<名>``。理由：约 13 个测试文件在
构造后 ``adapter._post = <stub>``，另有若干 ``adapter._api = <stub>`` /
``adapter._await_send_budget = <stub>``；若本类调用自己的方法，那些打桩会**静默
失明**（第九刀初版「构造时冻结可调用」正是被这条证伪的）。

``_throttle`` / ``_mark_backoff`` / ``_await_send_budget`` 对
``self._adapter._last_send[...]`` / ``self._adapter._send_history[...]`` 是**原地变更**
（保住 dict 身份），⛔ 不 rebind 局部副本。

logger 通道名保持 ``opencode_bridge.adapters.telegram``（日志逐字节不变）。

⚠️ 随方法迁来的 docstring 里对**留守**成员的 ``:meth:`` / ``:attr:`` 裸引用
（如 ``_coerce_send_limit``、``_throttle``）指向它们在 ``telegram.py`` 里的原主 ——
语义未变，只是跨模块了。
"""

from __future__ import annotations

import http.client
import json
import logging
import time
from typing import Any, Optional

from .telegram_protocol import (
    API_HOST, API_PORT, BACKOFF_INTERVAL, DEFAULT_SOCKET_TIMEOUT,
    MIN_SEND_INTERVAL, _retry_after,
)
from .telegram_send_budget import (
    DEFAULT_SEND_LIMIT_PER_MINUTE, MAX_RATE_LIMIT_WAIT_SECONDS,
    MAX_SEND_ATTEMPTS, rate_limit_wait_seconds, record_send,
    seconds_until_send_slot,
)

logger = logging.getLogger("opencode_bridge.adapters.telegram")


class TelegramHttp:
    """Telegram 出站 HTTP / 节流 / 发送预算：POST + 429 重试 + 每聊天预算。

    由 ``TelegramAdapter`` 构造并以适配器引用注入；适配器侧保留 5 个**同名同签名**
    的转发方法，保住既有测试与留守调用点（消费面零改动）。
    """

    def __init__(self, *, adapter: Any) -> None:
        # 协作解析经 ``_adapter`` **逐调用动态解析**（见各方法体内的
        # ``self._adapter.X``）—— 与手术前 ``self.X`` 在适配器实例上的解析语义
        # 完全一致：测试在构造后替换 ``adapter._post`` / ``adapter._api`` 等
        # 打桩继续生效。构造时只存适配器引用，不冻结任何成员。
        self._adapter = adapter

    def _post(
        self, method: str, payload: Optional[dict] = None, *, timeout: Optional[float] = None
    ) -> dict:
        """POST ``https://api.telegram.org/bot<token>/<method>`` (JSON body).

        Always returns a dict; network / decode failures are mapped to
        ``{"ok": False, "error_code": ..., "description": ...}`` so callers
        never see an exception from this layer unless they want to.
        """
        body = json.dumps(payload if payload is not None else {}).encode("utf-8")
        path = f"/bot{self._adapter.bot_token}/{method}"
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

        三道闸，作用域**故意不同**：

        1. :meth:`_throttle` —— 每聊天最小间隔，**所有**方法都守（编辑也算
           一次「对平台的请求」）；
        2. :meth:`_await_send_budget` —— 每聊天每分钟**新建消息**预算，只对
           :attr:`_BILLABLE_METHODS` 生效（编辑 / typing / reaction / 回调
           应答不计费，否则一次流式改写就能把预算耗光）；
        3. 429 重试 —— 最多 :data:`MAX_SEND_ATTEMPTS` 次尝试、累计等待不超过
           :data:`MAX_RATE_LIMIT_WAIT_SECONDS`；**等待用 ``_stop_event.wait``**
           而不是 ``time.sleep``，所以 ``stop()`` 能立刻打断（此前最坏被拖 60s）。

        Returns the final response dict; on transport failure returns
        ``{"ok": False}``. Never raises.
        """
        self._adapter._throttle(conversation_id)
        if method in self._adapter._BILLABLE_METHODS:
            self._adapter._await_send_budget(conversation_id)
        attempts_done = 0
        already_waited = 0.0
        while True:
            attempts_done += 1
            try:
                with self._adapter._api_lock:
                    data = self._adapter._post(method, payload, timeout=timeout)
            except Exception:
                logger.exception("telegram: %s request failed", method)
                return {"ok": False, "error_code": 0, "description": "transport error"}
            if not (isinstance(data, dict) and data.get("ok") is False):
                break
            delay = _retry_after(data)
            if delay is None:
                break
            if method not in self._adapter._BILLABLE_METHODS:
                # 上游机制：编辑 / 草稿撞 429 不重试、直接返回失败（下一帧
                # 会再试，且 _mark_backoff 让下一次至少隔 2s）。套上重试会把
                # 一次流式改写的等待拖到 60s，比丢掉这一帧更坏。
                logger.warning(
                    "telegram: %s rate-limited（非计费方法，不重试）", method
                )
                self._adapter._mark_backoff(conversation_id)
                break
            wait = rate_limit_wait_seconds(
                delay, already_waited=already_waited, attempts_done=attempts_done
            )
            if wait is None:
                logger.warning(
                    "telegram: %s 仍被限流，不再重试"
                    "（已尝试 %d 次、累计等待 %.1fs，上限 %d 次 / %.0fs）",
                    method, attempts_done, already_waited,
                    MAX_SEND_ATTEMPTS, MAX_RATE_LIMIT_WAIT_SECONDS,
                )
                break
            logger.warning(
                "telegram: %s rate-limited, waiting %.1fs (attempt %d/%d)",
                method, wait, attempts_done, MAX_SEND_ATTEMPTS,
            )
            self._adapter._mark_backoff(conversation_id)
            if self._adapter._stop_event.wait(wait):
                logger.info(
                    "telegram: %s 的 429 等待被关停打断，不再重试", method
                )
                break
            already_waited += wait
        # No generic failure logging here: callers decide which errors are
        # silent (e.g. editMessageText "message is not modified").
        return data if isinstance(data, dict) else {"ok": False}

    def _throttle(self, conversation_id: str) -> None:
        """Enforce a minimum interval between send/edit per conversation."""
        interval = getattr(self._adapter, "min_interval", MIN_SEND_INTERVAL)
        while True:
            with self._adapter._throttle_lock:
                last = self._adapter._last_send.get(conversation_id)
                now = time.monotonic()
                if last is None or (now - last) >= interval:
                    self._adapter._last_send[conversation_id] = now
                    return
                wait = interval - (now - last)
            if self._adapter._stop_event.wait(wait):
                return

    def _mark_backoff(self, conversation_id: str) -> None:
        """After a 429, push the next allowed send further out (2s)."""
        backoff = getattr(self._adapter, "backoff_interval", BACKOFF_INTERVAL)
        with self._adapter._throttle_lock:
            self._adapter._last_send[conversation_id] = time.monotonic() + backoff - self._adapter.min_interval

    def _await_send_budget(self, conversation_id: str) -> None:
        """等到这个聊天有「新建消息」额度，并把这一刻记进窗口。

        ⚠️ **只给 :attr:`_BILLABLE_METHODS` 调用**：编辑 / typing / reaction /
        回调应答不消耗 Telegram 的每聊天预算，套上它会白白拖慢流式改写。

        等待用 ``_stop_event.wait`` ⇒ ``stop()`` 立刻打断，⛔ 不是
        ``time.sleep``（那正是 429 那条曾被拖 60s 的同型缺陷）。
        ⚠️ 被打断时**不记账就返回**，调用方仍会发出这条消息 —— 与
        :meth:`_throttle` 的既有语义一致（stop = 不再等，不是「放弃发送」）。

        ``limit <= 0`` 是纯函数层的防御分支（配置面到不了：``_coerce_send_limit``
        把合法值夹到 1..30），这里直接放行。
        """
        limit = getattr(
            self._adapter, "send_limit_per_minute", DEFAULT_SEND_LIMIT_PER_MINUTE
        )
        if limit <= 0:
            return
        while True:
            now = time.monotonic()
            with self._adapter._throttle_lock:
                history = self._adapter._send_history.get(conversation_id, ())
                wait = seconds_until_send_slot(history, now=now, limit=limit)
                if wait <= 0.0:
                    self._adapter._send_history[conversation_id] = record_send(
                        history, now=now
                    )
                    return
            logger.info(
                "telegram: 每聊天发送预算已满（%d 条/分钟），等待 %.1fs 后再发",
                limit, wait,
            )
            if self._adapter._stop_event.wait(wait):
                return

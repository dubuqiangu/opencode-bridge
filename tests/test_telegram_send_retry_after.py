"""``TelegramAdapter.send`` 记录 ``retry_after`` 的方式必须与 ``_api`` 同源。

## 缺陷

``send()`` 的 sendMessage 失败分支曾**自己**内联解析 ``parameters.retry_after``：

* 不门控 ``error_code == 429`` ⇒ ``{"error_code": 400, "parameters":
  {"retry_after": 7}}`` 会记成 ``retry_after=7.0``；
* 不夹到 :data:`MAX_RETRY_AFTER` ⇒ ``9999`` 原样记；
* ``float(...)`` 抛 ``TypeError``/``ValueError`` 时**顺手把分类降成
  ``SendError.UNKNOWN``**，丢掉 ``classify_http`` 的正确限流分类。

## 修复

改用 :func:`opencode_bridge.adapters.telegram_protocol._retry_after`（已导入
``telegram.py``）：只认 429、夹到上限、**从不抛**（畸形输入返回 ``None``）；
分类永远取 ``classify_http`` 的结果。

## 为什么这个值要紧

:attr:`SendResult.retry_after` 是**诊断值**：``_note_send_failure`` →
``SendResult`` → ``outbound.py`` → ``outbound-failures.json`` → ``--status``。
它**不驱动重试**（重试在 ``_api`` 里）。所以本修复只改「记录下来的那个数」，
让它与 ``_api`` 的门控/夹逼口径一致。

## 判据

每条都写明「什么情况下它会红」。⛔ 不真网、不 sleep：``_api`` 被替身短路。
"""

from __future__ import annotations

import logging
import unittest

logging.getLogger("opencode_bridge").addHandler(logging.NullHandler())

from opencode_bridge.adapters.base import SendError
from opencode_bridge.adapters.telegram import (
    MAX_RETRY_AFTER,
    TelegramAdapter,
    _retry_after,
)
from opencode_bridge.hooks import Outbound, SendResult


def make_adapter() -> TelegramAdapter:
    adapter = TelegramAdapter({"bot_token": "123:FAKE"}, None)
    adapter.min_interval = 0
    return adapter


def observe_send_failure(body: dict) -> SendResult:
    """把 ``body`` 当作一次 sendMessage 的失败体喂给 ``send_observed``，回读结果。"""
    adapter = make_adapter()
    adapter._api = lambda conversation_id, method, payload, *, timeout=None: body
    result, exception = adapter.send_observed(
        Outbound(conversation_id="telegram:1", text="hi")
    )
    assert exception is None, exception
    return result


class TheRecordedRetryAfterMatchesTheGateTests(unittest.TestCase):
    """(a)(b) 429 的 ``retry_after`` 照记、照夹。"""

    def test_a_429_records_the_retry_after_value(self):
        """429 + ``retry_after=5`` ⇒ 记录的 ``retry_after`` 是 5.0。

        红的情形：内联解析被换掉却漏了这个值（记成 None）。
        """
        body = {
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests",
            "parameters": {"retry_after": 5},
        }
        result = observe_send_failure(body)
        self.assertIs(result.error_kind, SendError.RATE_LIMITED)
        self.assertEqual(result.retry_after, 5.0)
        # 与 _api 同源：记录值 == _retry_after(body)。
        self.assertEqual(result.retry_after, _retry_after(body))

    def test_an_absurd_429_retry_after_is_clamped_to_the_cap(self):
        """429 + ``retry_after=9999`` ⇒ 记成 :data:`MAX_RETRY_AFTER`（夹逼）。

        红的情形：退回内联解析（不夹）⇒ 记成 9999.0。
        """
        body = {
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests",
            "parameters": {"retry_after": 9999},
        }
        result = observe_send_failure(body)
        self.assertEqual(result.retry_after, MAX_RETRY_AFTER)
        self.assertEqual(result.retry_after, _retry_after(body))

    def test_the_clamp_constant_itself_is_sixty_seconds(self):
        """夹逼断言的另一半：常量本身必须被钉住，否则上面那条可能恒真。"""
        self.assertEqual(MAX_RETRY_AFTER, 60.0)


class TheErrorCodeGateTests(unittest.TestCase):
    """(c) 只有 429 才记 ``retry_after``；别的码一律 None。"""

    def test_a_non_429_carrying_retry_after_records_none(self):
        """``error_code=400`` + ``parameters.retry_after=7`` ⇒ 记录 None。

        红的情形：退回内联解析（不门控）⇒ 400 也记成 7.0，而那条调用根本没被限流。
        """
        body = {
            "ok": False,
            "error_code": 400,
            "description": "Bad Request",
            "parameters": {"retry_after": 7},
        }
        result = observe_send_failure(body)
        self.assertIsNone(result.retry_after)
        self.assertIs(result.error_kind, SendError.BAD_FORMAT)

    def test_a_403_carrying_retry_after_records_none(self):
        """``error_code=403`` + retry_after ⇒ None，分类仍是 FORBIDDEN。"""
        body = {
            "ok": False,
            "error_code": 403,
            "description": "Forbidden",
            "parameters": {"retry_after": 3},
        }
        result = observe_send_failure(body)
        self.assertIsNone(result.retry_after)
        self.assertIs(result.error_kind, SendError.FORBIDDEN)


class TheMalformedRetryAfterTests(unittest.TestCase):
    """(d) 畸形 ``retry_after`` 不改分类、不抛、记 None。"""

    def test_a_malformed_retry_after_keeps_the_classified_kind(self):
        """429 + ``retry_after="soon"`` ⇒ 分类仍是 RATE_LIMITED（⛔ 不是 UNKNOWN）。

        红的情形：退回内联解析 ⇒ ``float("soon")`` 抛 ``ValueError`` ⇒ 走
        ``except`` 分支记 ``SendError.UNKNOWN``，把「被限流」这个正确分类丢掉。
        """
        body = {
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests",
            "parameters": {"retry_after": "soon"},
        }
        result = observe_send_failure(body)
        self.assertIs(result.error_kind, SendError.RATE_LIMITED)
        self.assertIsNot(result.error_kind, SendError.UNKNOWN)
        self.assertIsNone(result.retry_after)

    def test_a_boolean_retry_after_keeps_the_classified_kind(self):
        """429 + ``retry_after=True`` ⇒ 分类 RATE_LIMITED、记 None（``_retry_after`` 拒布尔）。"""
        body = {
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests",
            "parameters": {"retry_after": True},
        }
        result = observe_send_failure(body)
        self.assertIs(result.error_kind, SendError.RATE_LIMITED)
        self.assertIsNone(result.retry_after)

    def test_a_429_without_parameters_still_records_one_second(self):
        """429 无 ``parameters`` ⇒ 记 1.0（``_retry_after`` 的既有契约，回归护栏）。"""
        body = {"ok": False, "error_code": 429, "description": "Too Many Requests"}
        result = observe_send_failure(body)
        self.assertEqual(result.retry_after, 1.0)


if __name__ == "__main__":
    unittest.main()

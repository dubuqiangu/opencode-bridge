"""``TelegramHttp._mark_backoff`` 必须容忍缺失的 ``min_interval``。

## 缺陷

``_mark_backoff`` 的上一行已经用 ``getattr(..., "backoff_interval", BACKOFF_INTERVAL)``，
而最后一行**裸读** ``self._adapter.min_interval`` ⇒ 一个没有该属性的适配器抛
``AttributeError``。而 ``_mark_backoff`` 由 ``_api`` 在**自己的 ``try`` 之外**调用、
``_api`` 文档写明「Never raises」⇒ 一次 429 会变成异常冒出。

## 修复

与 ``backoff_interval`` 同规：``getattr(self._adapter, "min_interval",
MIN_SEND_INTERVAL)``（``MIN_SEND_INTERVAL`` 已在 ``telegram_http.py`` 导入）。

## 判据

用**没有** ``min_interval`` 的假适配器分别走 ``_mark_backoff`` 直调与 ``_api`` 的
429 路径；两条都必须不抛，且排期值用 ``MIN_SEND_INTERVAL``。⛔ 不真网、不 sleep。
"""

from __future__ import annotations

import threading
import unittest
from unittest import mock

from opencode_bridge.adapters import telegram_http as telegram_http_module
from opencode_bridge.adapters.telegram_http import TelegramHttp
from opencode_bridge.adapters.telegram_protocol import (
    BACKOFF_INTERVAL,
    MIN_SEND_INTERVAL,
)


class FakeAdapter:
    """**故意没有** ``min_interval`` 的适配器替身（其余协作面齐备）。"""

    def __init__(self) -> None:
        self._throttle_lock = threading.Lock()
        self._last_send: dict = {}
        self.backoff_interval = BACKOFF_INTERVAL
        self._BILLABLE_METHODS = frozenset({"sendMessage"})
        self._api_lock = threading.Lock()
        self._stop_event = threading.Event()

    def _throttle(self, conversation_id: str) -> None:
        return None

    def _await_send_budget(self, conversation_id: str) -> None:
        return None

    def _post(self, method, payload=None, *, timeout=None):
        return {
            "ok": False,
            "error_code": 429,
            "parameters": {"retry_after": 0},
        }


class TheMarkBackoffToleranceTests(unittest.TestCase):
    """``min_interval`` 缺失时不抛，并回落 ``MIN_SEND_INTERVAL``。"""

    def test_mark_backoff_uses_the_default_interval_when_missing(self):
        """直调 ``_mark_backoff``：不抛，且排期 = now + backoff - MIN_SEND_INTERVAL。

        红的情形：裸读 ``min_interval`` ⇒ ``AttributeError``（本用例直接报错）。
        """
        adapter = FakeAdapter()
        http = TelegramHttp(adapter=adapter)
        with mock.patch.object(
            telegram_http_module.time, "monotonic", return_value=1000.0
        ):
            http._mark_backoff("telegram:1")   # ⛔ 不许抛
        self.assertEqual(
            adapter._last_send["telegram:1"],
            1000.0 + BACKOFF_INTERVAL - MIN_SEND_INTERVAL,
        )

    def test_the_api_429_path_does_not_raise_without_min_interval(self):
        """``_api`` 的 429 路径同样不抛（``_mark_backoff`` 在 ``_api`` 的 try 之外）。

        红的情形：裸读 ⇒ ``_api`` 违反「Never raises」，异常冒到调用方。
        """
        adapter = FakeAdapter()
        http = TelegramHttp(adapter=adapter)
        # ``_api`` 经适配器转发面调 ``_mark_backoff``（手术后的既有形态）⇒ 替身也接上。
        adapter._mark_backoff = http._mark_backoff
        with mock.patch.object(
            telegram_http_module.time, "monotonic", return_value=1000.0
        ):
            data = http._api("telegram:1", "sendMessage", {})   # ⛔ 不许抛
        self.assertEqual(data["error_code"], 429)
        self.assertEqual(
            adapter._last_send["telegram:1"],
            1000.0 + BACKOFF_INTERVAL - MIN_SEND_INTERVAL,
        )

    def test_a_present_min_interval_still_wins(self):
        """有 ``min_interval`` 时用它（回归护栏：别把 getattr 做成恒用默认）。"""
        adapter = FakeAdapter()
        adapter.min_interval = 5.0
        http = TelegramHttp(adapter=adapter)
        with mock.patch.object(
            telegram_http_module.time, "monotonic", return_value=1000.0
        ):
            http._mark_backoff("telegram:1")
        self.assertEqual(
            adapter._last_send["telegram:1"], 1000.0 + BACKOFF_INTERVAL - 5.0
        )


class TheIntervalConstantsTests(unittest.TestCase):
    """回落断言的另一半：被回落到的那两个常量本身必须被钉住，否则上面可能恒真。"""

    def test_the_default_intervals_are_pinned(self):
        self.assertEqual(MIN_SEND_INTERVAL, 1.2)
        self.assertEqual(BACKOFF_INTERVAL, 2.0)


if __name__ == "__main__":
    unittest.main()

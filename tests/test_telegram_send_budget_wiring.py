"""telegram 发送预算的接线守门（``telegram.py`` 的 ``_api`` / ``_await_send_budget``）。

判据全部是**离散计数与固定时刻**：``time.monotonic`` 被替换、``_stop_event.wait``
被替换、``time.sleep`` 一旦被调用就让测试炸掉。⛔ 不读墙钟、不真睡。

⚠️ 每条用例都写明「什么情况下它会红」—— 说不出红法的断言就是恒真的。
"""

from __future__ import annotations

import unittest
from unittest import mock

from opencode_bridge.adapters import telegram as telegram_module
from opencode_bridge.adapters.telegram import TelegramAdapter
from opencode_bridge.hooks import Outbound

LOGGER_NAME = "opencode_bridge.adapters.telegram"


def make_adapter(*, send_limit=None) -> TelegramAdapter:
    config = {"bot_token": "123:FAKE"}
    if send_limit is not None:
        config["send_limit_per_minute"] = send_limit
    adapter = TelegramAdapter(config, None)
    adapter.min_interval = 0          # 测试里不做最小间隔等待
    return adapter


def ok_post(method, payload=None, *, timeout=None):
    return {"ok": True, "result": {"message_id": 1}}


class TheBillableMethodSplitTests(unittest.TestCase):
    """只有「新建消息」消耗预算；编辑 / typing / 回调应答一律不计费。"""

    def test_send_message_consumes_budget_and_the_others_do_not(self):
        """``sendMessage`` 走预算，其余三种不走。

        红的情形：``_BILLABLE_METHODS`` 把 ``editMessageText`` 也算进去
        ⇒ 一次流式改写（几十次 edit）就能把预算耗光，随后真正的答复要等
        一个窗口；或反过来漏了 ``sendMessage`` ⇒ 预算压根没生效。
        """
        adapter = make_adapter()
        seen = []
        adapter._await_send_budget = lambda conversation_id: seen.append(
            conversation_id
        )
        adapter._post = ok_post
        for method in (
            "sendMessage",
            "editMessageText",
            "sendChatAction",
            "answerCallbackQuery",
        ):
            adapter._api("telegram:1", method, {})
        self.assertEqual(seen, ["telegram:1"])

    def test_every_chunk_of_a_split_message_goes_through_the_budget(self):
        """``send()`` 逐片调用 ``_api`` ⇒ 每一片都占一个预算额度。

        红的情形：预算只在 ``send()`` 外面套一次 ⇒ 一条 3 片的长回复只记 1 条，
        平台侧却收到 3 条 ⇒ 下一轮连发必然撞 429。
        """
        adapter = make_adapter()
        seen = []
        adapter._await_send_budget = lambda conversation_id: seen.append(
            conversation_id
        )
        adapter._post = ok_post
        text = "x" * (adapter.effective_max_length * 2 + 10)
        handle = adapter.send(Outbound(conversation_id="telegram:1", text=text))
        self.assertEqual(len(seen), 3)
        self.assertIsNotNone(handle)


class TheBudgetWaitTests(unittest.TestCase):
    """窗口满时等到最早一条滑出；关停能立刻打断。"""

    def test_a_full_window_waits_exactly_until_the_oldest_expires(self):
        """2 条预算、t=100/101 各发一条、now=110 ⇒ 等 50s，再在 163 记账。

        红的情形：等待算式写成 ``window - now``（得 50 纯属巧合？不，会得 -50
        ⇒ 0.0，直接放行）；或等待后不重新裁剪就记账 ⇒ 历史上出现第 3 条；
        或等完后不记账 ⇒ 下一次仍然满预算。
        """
        adapter = make_adapter(send_limit=2)
        adapter._send_history["telegram:1"] = [100.0, 101.0]
        waits = []
        adapter._stop_event.wait = lambda seconds: waits.append(seconds) or False
        with mock.patch.object(
            telegram_module.time, "monotonic", side_effect=[110.0, 163.0]
        ):
            adapter._await_send_budget("telegram:1")
        self.assertEqual(waits, [50.0])
        self.assertEqual(adapter._send_history["telegram:1"], [163.0])

    def test_stop_interrupts_the_budget_wait_without_recording(self):
        """等待中 ``stop()`` ⇒ 只等一次、不记账就返回。

        红的情形：把 ``_stop_event.wait`` 的返回值丢掉、继续循环 ⇒ 关停还要
        再等一个窗口（最坏 60s）；或在被打断后仍然记账 ⇒ 一条没发出的消息
        占掉了额度。
        """
        adapter = make_adapter(send_limit=1)
        adapter._send_history["telegram:1"] = [100.0]
        calls = []

        def fake_wait(seconds):
            calls.append(seconds)
            return len(calls) == 1      # 第一次就报告「正在关停」

        adapter._stop_event.wait = fake_wait
        with mock.patch.object(
            telegram_module.time, "monotonic", return_value=110.0
        ):
            adapter._await_send_budget("telegram:1")
        self.assertEqual(calls, [50.0])
        self.assertEqual(adapter._send_history["telegram:1"], [100.0])

    def test_the_budget_is_per_chat(self):
        """一个聊天满额不影响另一个聊天。

        红的情形：历史只有一份全局列表 ⇒ 任何群发都会互相拖慢。
        """
        adapter = make_adapter(send_limit=1)
        adapter._send_history["telegram:1"] = [100.0]
        waits = []
        adapter._stop_event.wait = lambda seconds: waits.append(seconds) or False
        with mock.patch.object(
            telegram_module.time, "monotonic", return_value=110.0
        ):
            adapter._await_send_budget("telegram:2")
        self.assertEqual(waits, [])
        self.assertEqual(adapter._send_history["telegram:2"], [110.0])


class The429RetryTests(unittest.TestCase):
    """429：最多 4 次尝试、累计等待 <= 60s、等待可被 stop 打断。"""

    def test_retry_waits_through_the_stop_event_not_time_sleep(self):
        """429 的等待必须走 ``_stop_event.wait``。

        红的情形：退回 ``time.sleep(delay)`` ⇒ 本用例里 sleep 被替换成抛异常，
        测试直接炸；这正是 2026-10-25 审计前那版「关停最坏被拖 60s」的缺陷。
        """
        adapter = make_adapter()
        adapter._post = lambda method, payload=None, *, timeout=None: {
            "ok": False, "error_code": 429, "parameters": {"retry_after": 5},
        }
        waits = []
        adapter._stop_event.wait = lambda seconds: waits.append(seconds) or False
        with mock.patch.object(
            telegram_module.time,
            "sleep",
            side_effect=AssertionError("time.sleep 会拖住 stop()"),
        ):
            adapter._api("telegram:1", "sendMessage", {})
        self.assertEqual(waits, [5.0, 5.0, 5.0])

    def test_retry_stops_after_four_attempts(self):
        """一直 429（retry_after=0）⇒ 恰好 4 次 ``_post``、3 次等待。

        红的情形：退回到只重试一次（2 次调用）；或上界写成 ``> 4``（5 次）。
        """
        adapter = make_adapter()
        posts = []

        def always_429(method, payload=None, *, timeout=None):
            posts.append(method)
            return {"ok": False, "error_code": 429,
                    "parameters": {"retry_after": 0}}

        adapter._post = always_429
        waits = []
        adapter._stop_event.wait = lambda seconds: waits.append(seconds) or False
        data = adapter._api("telegram:1", "sendMessage", {})
        self.assertEqual(len(posts), 4)
        self.assertEqual(waits, [0.0, 0.0, 0.0])
        self.assertIs(data["error_code"], 429)

    def test_the_cumulative_wait_cap_stops_before_the_attempt_cap(self):
        """每次要求 40s ⇒ 第 2 次就停（40+40 > 60），不是发满 4 次。

        红的情形：只按单次 ``retry_after`` 判 ⇒ 累计等 120s，把一条回复或
        一次关停拖到两分钟。
        """
        adapter = make_adapter()
        posts = []

        def always_429(method, payload=None, *, timeout=None):
            posts.append(method)
            return {"ok": False, "error_code": 429,
                    "parameters": {"retry_after": 40}}

        adapter._post = always_429
        waits = []
        adapter._stop_event.wait = lambda seconds: waits.append(seconds) or False
        adapter._api("telegram:1", "sendMessage", {})
        self.assertEqual(len(posts), 2)
        self.assertEqual(waits, [40.0])

    def test_a_rate_limited_edit_is_not_retried(self):
        """编辑撞 429 ⇒ 只发 1 次、零等待（返回失败，让下一帧再试）。

        红的情形：把编辑也套进 4 次重试 ⇒ 一次流式改写最坏被拖 60s，
        而这期间后续帧与最终答复都排在它后面。
        """
        adapter = make_adapter()
        posts = []

        def always_429(method, payload=None, *, timeout=None):
            posts.append(method)
            return {"ok": False, "error_code": 429,
                    "parameters": {"retry_after": 5}}

        adapter._post = always_429
        waits = []
        adapter._stop_event.wait = lambda seconds: waits.append(seconds) or False
        data = adapter._api("telegram:1", "editMessageText", {})
        self.assertEqual(len(posts), 1)
        self.assertEqual(waits, [])
        self.assertIs(data["ok"], False)

    def test_stop_interrupts_the_429_wait_immediately(self):
        """等待中 ``stop()`` ⇒ 只发 1 次、返回最后的 429。

        红的情形：忽略 ``wait`` 的返回值 ⇒ 关停后还要再发 3 次（每次仍可能
        被限流），而线程退出被拖在重试循环里。
        """
        adapter = make_adapter()
        posts = []

        def always_429(method, payload=None, *, timeout=None):
            posts.append(method)
            return {"ok": False, "error_code": 429,
                    "parameters": {"retry_after": 60}}

        adapter._post = always_429
        waits = []
        adapter._stop_event.wait = lambda seconds: waits.append(seconds) or True
        data = adapter._api("telegram:1", "sendMessage", {})
        self.assertEqual(len(posts), 1)
        self.assertEqual(waits, [60.0])
        self.assertIs(data["ok"], False)


class TheSendLimitConfigTests(unittest.TestCase):
    """配置面：默认 15、非整数回落、越界夹逼、合法值不告警。"""

    def test_the_default_is_fifteen(self):
        """没配 ⇒ 15，且零告警。

        红的情形：类属性被改成别的数；或默认路径也打一条告警（噪音）。
        """
        with self.assertNoLogs(LOGGER_NAME, level="WARNING"):
            adapter = make_adapter()
        self.assertEqual(adapter.send_limit_per_minute, 15)

    def test_a_non_integer_falls_back_with_a_warning(self):
        """``"20"`` / ``True`` 不是整数 ⇒ 15 + 点名该键的 WARNING。

        红的情形：``int("20")`` 被顺手接受 ⇒ 与 ``poll_timeout`` 的纪律不一致；
        或 ``True`` 被当成 1 ⇒ 悄悄把预算砍到 1 条/分钟。
        """
        for configured in ("20", True):
            with self.subTest(configured=configured):
                with self.assertLogs(LOGGER_NAME, level="WARNING") as captured:
                    adapter = make_adapter(send_limit=configured)
                self.assertEqual(adapter.send_limit_per_minute, 15)
                self.assertIn("send_limit_per_minute", "\n".join(captured.output))
                self.assertIn("不是整数", "\n".join(captured.output))

    def test_an_out_of_range_integer_is_clamped_with_a_warning(self):
        """0 ⇒ 1、99 ⇒ 30，且各自点名「已夹到」。

        红的情形：越界回落默认 15（0 变成 15、99 也变成 15）⇒ 配置意图被
        静默改写；或只夹下界 ⇒ 99 原样生效 = 关掉预算。
        """
        for configured, expected in ((0, 1), (99, 30)):
            with self.subTest(configured=configured):
                with self.assertLogs(LOGGER_NAME, level="WARNING") as captured:
                    adapter = make_adapter(send_limit=configured)
                self.assertEqual(adapter.send_limit_per_minute, expected)
                self.assertIn("已夹到", "\n".join(captured.output))

    def test_an_in_range_integer_is_accepted_silently(self):
        """合法值原样生效、零告警。

        红的情形：合法值也打告警；或把 20 改成别的数。
        """
        with self.assertNoLogs(LOGGER_NAME, level="WARNING"):
            adapter = make_adapter(send_limit=20)
        self.assertEqual(adapter.send_limit_per_minute, 20)


if __name__ == "__main__":
    unittest.main()

"""``editMessageText`` 撞 HTTP 429 必须重试（P0-B）。

## 缺陷

``TelegramHttp._api`` 的重试路径曾用 ``method not in _BILLABLE_METHODS`` 这道闸
**兼当**「值得重试吗」。而 ``_BILLABLE_METHODS`` 的语义是「消耗每聊天新建消息
**预算**」—— 与「429 该不该重试」是两件事。``editMessageText`` 不在那个集合里
⇒ ``edit()`` 撞 429 时**零重试**、当帧白丢。

## 修复意图

**把「重试资格」与「计费」解耦**：429 = 请求被平台**拒绝**、没有副作用 ⇒ 重试
安全。计费只保留它自己那道闸（``_await_send_budget``，行为不变）。重试仍受既有
双上限（``MAX_SEND_ATTEMPTS`` 次尝试 / 累计 ``MAX_RATE_LIMIT_WAIT_SECONDS``）与
可被 ``_stop_event.wait`` 打断的等待约束 —— ⛔ 不新增任何旋钮。

## 本文件的判据

全部是离散计数 / 返回值，⛔ 不读墙钟、不 ``time.sleep``；并发用
``threading.Event`` 同步点。

* 「重试并最终成功」—— ``_post`` 第一次 429、第二次 ok，断言最终 ``ok=True``
  且恰好两次尝试、一次等待；同步点 ``Event`` 保证「第二次确实发生」。
* 「``sendMessage`` 仍重试」—— 回归护栏，证明解耦没有顺手弄坏既有行为。
* 「``_await_send_budget`` 只对计费方法调用」—— 计费行为逐字不变。
* 「非 429 不重试」—— 对照臂：证明重试是 429 专属，不是「任何失败都重试」。

## 反向证明（在验证阶段注入，不在本文件里）

* 恢复「按 ``_BILLABLE_METHODS`` 门控重试」⇒ 编辑重试用例真红。
* 破坏等待上限（拿掉 ``wait is None`` 那道 break）⇒ 上限用例真红。
* 让 ``_await_send_budget`` 对所有方法生效 ⇒ 计费用例真红。
"""

from __future__ import annotations

import threading
import unittest

from opencode_bridge.adapters.telegram import TelegramAdapter
from opencode_bridge.hooks import MsgHandle, Outbound


def make_adapter() -> TelegramAdapter:
    """零间隔适配器：测试里不做每聊天最小间隔等待。"""
    adapter = TelegramAdapter({"bot_token": "123:FAKE"}, None)
    adapter.min_interval = 0
    return adapter


def rate_limited_then_ok(attempts: list, *, retry_after: float = 0.0):
    """``_post`` 替身：第一次回 429，之后成功。把方法名追加进 ``attempts``。"""

    def post(method, payload=None, *, timeout=None):
        attempts.append(method)
        if len(attempts) == 1:
            return {
                "ok": False,
                "error_code": 429,
                "parameters": {"retry_after": retry_after},
            }
        return {"ok": True, "result": {"message_id": 7}}

    return post


class TheEditRetryTests(unittest.TestCase):
    """429 重试资格与计费解耦后：编辑与发送同等重试。"""

    def test_a_rate_limited_edit_retries_and_then_succeeds(self):
        """编辑撞 429（小 ``retry_after``）⇒ 重试一次后成功。

        红的情形：退回「按 ``_BILLABLE_METHODS`` 门控重试」⇒ 编辑只试 1 次、
        零等待就返回 429（这正是被修的缺陷）。
        """
        adapter = make_adapter()
        attempts: list = []
        retry_reached = threading.Event()
        real_wait = adapter._stop_event.wait
        seen_waits: list = []

        def post(method, payload=None, *, timeout=None):
            attempts.append(method)
            if len(attempts) == 1:
                return {
                    "ok": False,
                    "error_code": 429,
                    "parameters": {"retry_after": 0},
                }
            retry_reached.set()
            return {"ok": True, "result": {"message_id": 7}}

        def recording_wait(seconds):
            seen_waits.append(seconds)
            # 真实 threading.Event；0.0 ⇒ 立刻返回，⛔ 不真睡。
            return real_wait(seconds)

        adapter._post = post
        adapter._stop_event.wait = recording_wait
        outcome: dict = {}

        def run_api():
            outcome["data"] = adapter._api("telegram:1", "editMessageText", {})

        worker = threading.Thread(target=run_api)
        worker.start()
        # 同步点：等第二次尝试真的发生（超时只作死锁守卫，不作时序断言）。
        self.assertTrue(
            retry_reached.wait(timeout=5.0),
            "editMessageText 的 429 没有被重试（第二次尝试从未发生）",
        )
        worker.join(timeout=5.0)
        self.assertFalse(worker.is_alive(), "重试路径没有在时限内返回")
        self.assertEqual(attempts, ["editMessageText", "editMessageText"])
        self.assertEqual(seen_waits, [0.0])
        self.assertIs(outcome["data"].get("ok"), True)

    def test_a_rate_limited_send_message_still_retries(self):
        """``sendMessage`` 撞 429 ⇒ 仍重试（回归护栏）。

        红的情形：解耦时顺手弄坏了发送路径的重试（例如把重试整段挪进计费分支）。
        """
        adapter = make_adapter()
        attempts: list = []
        adapter._post = rate_limited_then_ok(attempts)
        adapter._await_send_budget = lambda conversation_id: None
        data = adapter._api("telegram:1", "sendMessage", {})
        self.assertEqual(attempts, ["sendMessage", "sendMessage"])
        self.assertIs(data.get("ok"), True)


class TheEditMethodEndToEndTests(unittest.TestCase):
    """走公共 ``edit()`` 的端到端臂：缺陷就在它下游那一层。"""

    def test_edit_survives_a_cold_429_through_the_public_method(self):
        """``edit()`` 第一次被 429 拒绝、重试后成功返回 ``True``。

        红的情形：``_api`` 恢复按计费门控 ⇒ ``edit()`` 第一次 429 就直接
        ``return False``，按钮/占位气泡永远顶不上去。
        """
        adapter = make_adapter()
        attempts: list = []
        adapter._post = rate_limited_then_ok(attempts)
        handle = MsgHandle(
            conversation_id="telegram:1", message_id="7", platform="telegram"
        )
        edited = adapter.edit(
            handle, Outbound(conversation_id="telegram:1", text="最终答复")
        )
        self.assertIs(edited, True)
        self.assertEqual(attempts, ["editMessageText", "editMessageText"])


class TheBillingGateUnchangedTests(unittest.TestCase):
    """计费行为零变化：只有计费方法碰 ``_await_send_budget``。"""

    def test_only_billable_methods_touch_the_send_budget(self):
        """``sendMessage`` 记预算；编辑 / typing / 回调应答一律不记。

        红的情形：解耦时把 ``_await_send_budget`` 的闸门也一起拆了（对所有方法
        调用）⇒ 一次流式改写（几十次 edit）就能把预算耗光。
        """
        adapter = make_adapter()
        budget_calls: list = []
        adapter._await_send_budget = lambda conversation_id: budget_calls.append(
            conversation_id
        )
        adapter._post = lambda method, payload=None, *, timeout=None: {
            "ok": True,
            "result": {"message_id": 1},
        }
        for method in (
            "sendMessage",
            "editMessageText",
            "sendChatAction",
            "answerCallbackQuery",
        ):
            adapter._api("telegram:1", method, {})
        self.assertEqual(budget_calls, ["telegram:1"])


class TheNonRateLimitControlTests(unittest.TestCase):
    """对照臂：非 429 的失败**不**重试（重试是 429 专属，不是「任何失败都重试」）。"""

    def test_a_non_429_error_is_not_retried(self):
        """``error_code=400`` ⇒ 恰好 1 次 ``_post``、零等待。

        红的情形：把重试条件放宽成「只要 ``ok`` 为 false」⇒ 400（请求本身
        不合法）也会被原样重试，纯浪费，且告警会指向一个没被限流的调用。
        """
        adapter = make_adapter()
        attempts: list = []
        waits: list = []

        def post(method, payload=None, *, timeout=None):
            attempts.append(method)
            return {"ok": False, "error_code": 400, "description": "Bad Request"}

        adapter._post = post
        adapter._stop_event.wait = lambda seconds: waits.append(seconds) or False
        data = adapter._api("telegram:1", "editMessageText", {})
        self.assertEqual(attempts, ["editMessageText"])
        self.assertEqual(waits, [])
        self.assertIs(data.get("ok"), False)


if __name__ == "__main__":
    unittest.main()

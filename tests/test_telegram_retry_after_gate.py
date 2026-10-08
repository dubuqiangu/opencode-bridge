"""telegram：``_retry_after`` 的门控与畸形输入行为（2026-10-08 修复的钉子）。

改之前的门控是「只要带得出 ``parameters.retry_after`` 就照等」——telegram 审计
实测 ``{"error_code": 400, "parameters": {"retry_after": 7}}`` 返回 **7.0** ⇒
400 是**请求本身不合法**，睡 7 秒再原样重试一次 100% 还是 400，纯浪费一轮墙钟，
而那条 ``rate-limited`` 告警会指向一个根本没被限流的调用。

修复后的语义：**门控答的是「这个码是不是限流（429）」，而不是「来者不拒」**。

判据分两组，缺一组这个文件就是恒真的：

* **门控组** —— 429 以外的码**一概不等**（400 带 retry_after、无 error_code 带
  retry_after、``ok: True`` 带 retry_after）；
* **保留组** —— 修复**不许**顺手弄坏既有行为：429 照常等、夹逼照常夹、
  畸形输入照常返回 ``None``（⛔ 不许把 ``None`` 改成 ``0`` —— 那会变成
  立刻重试的紧循环）。

⚠️ 断言「钳到 ``MAX_RETRY_AFTER``」的用例必须**同时**断言常量本身的值
（AGENTS.md §7.1：一整组拿导入常量当期望值的断言曾集体恒真）。
"""

from __future__ import annotations

import logging
import unittest

logging.getLogger("opencode_bridge").addHandler(logging.NullHandler())

from opencode_bridge.adapters.telegram import MAX_RETRY_AFTER, _retry_after


class RetryAfterGateTest(unittest.TestCase):
    """门控组：429 以外的码一概不等。"""

    def test_code_400_with_retry_after_is_ignored_entirely(self):
        """改之前这条返回 7.0（白等 7 秒）⇒ 本条就是那次修复的判别臂。"""
        self.assertIsNone(
            _retry_after({"ok": False, "error_code": 400,
                          "parameters": {"retry_after": 7}}),
            "400 不是限流，它的 retry_after 必须整个被忽略",
        )

    def test_missing_error_code_with_retry_after_is_ignored(self):
        """没有 error_code 的错误体同样不是限流（旧门控按「ok 为 false」会等）。"""
        self.assertIsNone(
            _retry_after({"ok": False, "parameters": {"retry_after": 7}}),
        )

    def test_ok_true_body_is_ignored(self):
        """成功体即使带着 retry_after 形状的字段也一概不等。"""
        self.assertIsNone(
            _retry_after({"ok": True, "parameters": {"retry_after": 7}}),
        )

    def test_code_403_with_retry_after_is_ignored(self):
        self.assertIsNone(
            _retry_after({"ok": False, "error_code": 403,
                          "parameters": {"retry_after": 3}}),
        )


class RetryAfterPreservedBehaviorTest(unittest.TestCase):
    """保留组：429 照常等、夹逼照常夹、畸形输入照常拒绝。"""

    def test_code_429_with_retry_after_waits_it(self):
        self.assertEqual(
            _retry_after({"ok": False, "error_code": 429,
                          "parameters": {"retry_after": 5}}),
            5.0,
        )

    def test_code_429_with_fractional_retry_after_waits_it(self):
        self.assertEqual(
            _retry_after({"ok": False, "error_code": 429,
                          "parameters": {"retry_after": 2.5}}),
            2.5,
        )

    def test_code_429_without_parameters_defaults_to_one_second(self):
        self.assertEqual(
            _retry_after({"ok": False, "error_code": 429}),
            1.0,
        )

    def test_code_429_with_non_dict_parameters_defaults_to_one_second(self):
        self.assertEqual(
            _retry_after({"ok": False, "error_code": 429,
                          "parameters": "not-a-dict"}),
            1.0,
        )

    def test_absurd_retry_after_is_clamped_to_the_module_constant(self):
        clamped = _retry_after({"ok": False, "error_code": 429,
                                "parameters": {"retry_after": 9999}})
        self.assertEqual(clamped, MAX_RETRY_AFTER)

    def test_the_clamp_constant_itself_is_sixty_seconds(self):
        """钳位断言的另一半：常量本身的值必须被钉住，否则上面整组恒真。"""
        self.assertEqual(MAX_RETRY_AFTER, 60.0)


class RetryAfterMalformedInputTest(unittest.TestCase):
    """畸形输入照常拒绝（⛔ None 不是 0：调用方不许把 None 当成「立刻重试」）。"""

    def test_negative_retry_after_is_rejected(self):
        self.assertIsNone(
            _retry_after({"ok": False, "error_code": 429,
                          "parameters": {"retry_after": -3}}),
        )

    def test_boolean_retry_after_is_rejected(self):
        self.assertIsNone(
            _retry_after({"ok": False, "error_code": 429,
                          "parameters": {"retry_after": True}}),
        )

    def test_string_retry_after_is_rejected(self):
        self.assertIsNone(
            _retry_after({"ok": False, "error_code": 429,
                          "parameters": {"retry_after": "5"}}),
        )

    def test_list_retry_after_is_rejected(self):
        self.assertIsNone(
            _retry_after({"ok": False, "error_code": 429,
                          "parameters": {"retry_after": [5]}}),
        )

    def test_non_dict_body_is_rejected(self):
        self.assertIsNone(_retry_after(None))
        self.assertIsNone(_retry_after("rate limited"))
        self.assertIsNone(_retry_after(429))


if __name__ == "__main__":
    unittest.main()

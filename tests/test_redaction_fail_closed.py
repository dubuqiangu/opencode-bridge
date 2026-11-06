"""``Redactor.scrub`` 的失败关闭（fail-closed）行为守门。

为什么单列一个文件
==================

``scrub`` 原先在某条规则抛异常时 ``return text`` —— 那是**失败开放**：
脱敏层在最该兜底的时刻，把**原文**（含它本该遮蔽的秘密）原样交回调用方。
本文件把这个行为翻成**失败关闭**：四条规则各自包在 try 里，任何一条抛异常时
输出都是不可用标记 ``[REDACTED:#?]`` —— 既不是原文，也不是半脱敏（可能仍含
秘密）的中间态。

三组，分别钉三件事：

1. :class:`RuleFailureIsFailClosedTests` —— 四条规则**逐条**注入异常，
   输出都是不可用标记、且原文里的秘密**一个都不出现**；
2. :class:`FilterStillPassesRecordsTests` —— ``RedactingFilter`` 在这条路径上
   仍然返回 ``True``（脱敏层只许改内容，不许丢记录），而记录内容已被替换成标记；
3. :class:`HealthyPathIsDistinguishedTests` —— **对照臂**：没有规则抛异常时，
   输出**不是**那个不可用标记，且与「逐条直接调用」**逐字节相同**（证明失败关闭
   没有改变正常路径的可观测输出）。

**反向证明**：把 ``scrub`` 改回 ``return text``（旧失败开放），第 1、3 组会变红。
"""

from __future__ import annotations

import logging
import unittest
from unittest import mock

from opencode_bridge.redaction import Redactor, RedactingFilter
from tests.test_redaction import TELEGRAM_TOKEN, make_redactor

#: 失败关闭时输出的字面量。**按字面断言**而不是用 ``_MASK_TEMPLATE %`` 重算 ——
#: 用同一对常量重算等于「拿被审对象算它自己」，那条断言会恒真（§7.1）。
UNAVAILABLE_MASK = "[REDACTED:#?]"

#: 四条规则，各配一个**该规则本来会处理**的秘密。逐条注入异常时用。
#: 用 ``(方法名, 该条会命中的秘密)`` 二元组，测试据此既能 patch 又能断言不泄漏。
RULE_FAILURE_CASES = (
    ("scrub_credentials", TELEGRAM_TOKEN),
    ("scrub_conversation_ids", "telegram:123456789"),
    ("scrub_phones", "13800138000"),
    ("scrub_emails", "bob@example.com"),
)


class RuleFailureIsFailClosedTests(unittest.TestCase):
    """某条规则抛异常 ⇒ 失败关闭，绝不放行原文或半脱敏文本。"""

    def setUp(self) -> None:
        self.redactor = make_redactor()

    def _scrub_while_rule_raises(self, rule_name: str, text: str) -> str:
        """在 ``rule_name`` 一直抛异常的窗口内跑一次 ``scrub``。"""
        with mock.patch.object(
            Redactor, rule_name, side_effect=RuntimeError("redaction rule exploded")
        ):
            return self.redactor.scrub(text)

    def test_each_rule_failure_returns_the_unavailable_mask(self):
        for rule_name, secret in RULE_FAILURE_CASES:
            with self.subTest(rule=rule_name):
                scrubbed = self._scrub_while_rule_raises(
                    rule_name, "prefix %s suffix" % secret
                )

                self.assertEqual(scrubbed, UNAVAILABLE_MASK)
                self.assertNotEqual(scrubbed, "prefix %s suffix" % secret)

    def test_each_rule_failure_never_leaks_its_secret(self):
        for rule_name, secret in RULE_FAILURE_CASES:
            with self.subTest(rule=rule_name):
                original = "prefix %s suffix" % secret

                scrubbed = self._scrub_while_rule_raises(rule_name, original)

                self.assertNotIn(secret, scrubbed)

    def test_a_failure_after_partial_scrubbing_does_not_leak_the_remainder(self):
        """第三条规则抛异常时，前两条已改过文本 —— 那些「半成品」也不许逃出去。

        这里只把手机号放进原文、注入 ``scrub_phones``：失败开放会让**整段原文**
        （含手机号）回去；失败关闭则连半脱敏的中间态都不放行。
        """

        scrubbed = self._scrub_while_rule_raises(
            "scrub_phones", "call 13800138000 mail bob@example.com"
        )

        self.assertEqual(scrubbed, UNAVAILABLE_MASK)
        self.assertNotIn("13800138000", scrubbed)
        self.assertNotIn("bob@example.com", scrubbed)


class FilterStillPassesRecordsTests(unittest.TestCase):
    """失败关闭路径上，过滤器仍然**不丢记录**，只是内容换成标记。"""

    def test_the_filter_does_not_drop_a_record_when_a_rule_raises(self):
        record = logging.LogRecord(
            "opencode_bridge.test", logging.INFO, __file__, 1,
            "conv telegram:%s", ("123456789",), None,
        )

        with mock.patch.object(
            Redactor, "scrub_conversation_ids",
            side_effect=RuntimeError("redaction rule exploded"),
        ):
            returned = RedactingFilter(make_redactor()).filter(record)

        self.assertTrue(returned)
        self.assertEqual(record.getMessage(), UNAVAILABLE_MASK)
        self.assertNotIn("123456789", record.getMessage())


class HealthyPathIsDistinguishedTests(unittest.TestCase):
    """对照臂：**没有规则抛异常**时，失败关闭这条分支不该被走到。

    没有它，上面那些断言有可能只是「无论输入都返回标记」的恒真产物 ——
    这一组证明守门分得开「正常脱敏」与「失败关闭」两种情形。
    """

    def setUp(self) -> None:
        self.redactor = make_redactor()

    def test_a_healthy_scrub_returns_real_digests_not_the_unavailable_mask(self):
        scrubbed = self.redactor.scrub(
            "conv telegram:123456789 phone 13800138000 mail bob@example.com"
        )

        self.assertNotEqual(scrubbed, UNAVAILABLE_MASK)
        self.assertIn("conv#", scrubbed)
        self.assertNotIn("123456789", scrubbed)
        self.assertNotIn("13800138000", scrubbed)
        self.assertNotIn("bob@example.com", scrubbed)

    def test_the_four_rules_still_run_in_the_same_order(self):
        """正常路径的输出 == 「按同一顺序逐条直接调用」的输出（零行为变化）。"""
        text = (
            "token=0123456789abcdefghij conv telegram:123456789 "
            "phone 13800138000 mail bob@example.com"
        )

        via_scrub = self.redactor.scrub(text)
        manual_chain = self.redactor.scrub_emails(
            self.redactor.scrub_phones(
                self.redactor.scrub_conversation_ids(
                    self.redactor.scrub_credentials(text)
                )
            )
        )

        self.assertEqual(via_scrub, manual_chain)


if __name__ == "__main__":
    unittest.main()

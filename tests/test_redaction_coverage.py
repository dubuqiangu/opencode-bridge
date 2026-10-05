"""脱敏覆盖的**顺序无关**那一层（:mod:`opencode_bridge.redaction_coverage`）。

## 这份文件在守什么
==================

``redaction.py`` 把过滤器挂在 **handler** 上，而 ``Logger.callHandlers`` 从**发出记录
的那个 logger** 往上走 —— 于是"覆盖"依赖 **handler 在走位里的位置**：

* 排在带过滤器 handler **之后**的 → 盖住了（过滤器就地改了那条**共享**的
  ``LogRecord``，后面的 handler 拿到的是同一个已经脱敏的对象）；
* 排在**之前**的 → **漏**。典型就是挂在 ``opencode_bridge`` 或更深的 logger 上的
  本地 handler：它在走位里比 root 近，先把明文写出去了。

:func:`~opencode_bridge.redaction_coverage.install_order_independent_coverage` 把脱敏
挪到 ``logging.setLogRecordFactory`` —— 记录**创建**那一刻 —— 于是 handler 的数量、
挂上去的时刻、走位顺序**全都无关**。

## 判据
======

* **决定性**：在装好之后才挂上 handler，发一条含凭据的记录，断言那个 handler 的
  **输出**是脱敏的。用真的 :class:`logging.StreamHandler` 与
  :class:`io.StringIO`，不拿脱敏引擎的替身。
* **幂等**：两层会对同一条记录各跑一遍，所以第二遍必须**逐字节是恒等变换** ——
  断言 ``msg`` / ``args`` / ``exc_text`` 全部一模一样，而不只是"看起来一样"。
* **不许静默降级**：顺序无关那层被别人换掉时，:func:`coverage_report` 与
  :func:`warn_about_uncovered_records` 必须**说得出来**。
* **不越界**：别的 logger（``opencode_bridge_extra`` 这种）与宿主自带的 record
  factory 一条都不许被改。

## 夹具的形状
============

凭据按 AGENTS.md §2.4 **拼接** —— 仓库里不出现任何连续的完整密钥形状，而运行值
逐字节不变。
"""

from __future__ import annotations

import io
import logging
import unittest

from opencode_bridge import redaction, redaction_coverage
from opencode_bridge.redaction import RedactingFilter, Redactor

#: 拼接出来的 telegram bot token：形状完整、但源码里没有可匹配的连续字面量。
TELEGRAM_TOKEN = "123456789" + ":" + "A" * 35
#: 另一个形状（``secret-assignment``），用来证明不是只对一种形状生效。
LONG_PASSWORD = "correct-horse-battery-staple"

PROBE_LOGGER_NAME = "opencode_bridge.redaction_probe"
#: 故意用 ``opencode_bridge_extra``：它在命名空间**之外**，必须一条都不被改。
FOREIGN_LOGGER_NAME = "opencode_bridge_extra"


def make_redactor() -> Redactor:
    """固定密钥的脱敏器 —— 摘要于是可断言。"""
    return Redactor(key=b"redaction-coverage-test-key")


class StreamCollector(logging.StreamHandler):
    """真的 ``StreamHandler``，只是流指向内存里的字符串。"""

    def __init__(self, level: int = logging.NOTSET) -> None:
        self.stream = io.StringIO()
        super().__init__(self.stream)
        self.setLevel(level)
        self.setFormatter(logging.Formatter("%(name)s: %(message)s"))

    @property
    def text(self) -> str:
        return self.stream.getvalue()


class CoverageTestCase(unittest.TestCase):
    """把 root 与命名空间 logger 都恢复原样，并且**卸掉**全局那一层。

    ⚠️ 卸载是**必须**的：record factory 是进程全局的，不撤就会活过这个用例 ——
    于是后面每一个用例的前提都被这个用例改了，而且没人会想到去查日志。
    """

    def setUp(self) -> None:
        root = logging.getLogger()
        bridge = logging.getLogger(redaction.LOGGER_NAMESPACE)
        for logger in (root, bridge, logging.getLogger(FOREIGN_LOGGER_NAME)):
            self.addCleanup(setattr, logger, "handlers", list(logger.handlers))
            self.addCleanup(setattr, logger, "level", logger.level)
            logger.handlers = []
            logger.setLevel(logging.DEBUG)
        self.addCleanup(redaction.remove_redaction_filter)

    def install(self, redactor: Redactor | None = None):
        """装上两层，返回挂在 root 上的那个 handler（用来断言"老路没坏"）。"""
        on_root = StreamCollector()
        logging.getLogger().addHandler(on_root)
        covered = redaction.install_redaction_filter(
            redactor=redactor or make_redactor())
        self.assertIn(on_root, covered,
                      "前提不成立：root handler 上没有过滤器")
        return on_root


# ----------------------------------------------------------------------
# 1. 决定性：装好之后才挂上的 handler 也必须脱敏
# ----------------------------------------------------------------------
class AHandlerAttachedLaterIsStillCoveredTests(CoverageTestCase):
    def test_a_handler_attached_after_the_install_still_gets_redacted_output(self):
        """**这就是那条决定性用例。**

        先装过滤器，再挂 handler，再发一条含凭据的记录 —— 断言 handler 拿到的
        **输出**里有遮蔽标记、**没有**明文。
        """
        self.install()
        attached_later = StreamCollector()
        logging.getLogger().addHandler(attached_later)

        logging.getLogger(PROBE_LOGGER_NAME).warning("token=%s", TELEGRAM_TOKEN)

        self.assertIn("[REDACTED:telegram-bot-token]", attached_later.text)
        self.assertNotIn(TELEGRAM_TOKEN, attached_later.text)

    def test_the_same_holds_for_a_traceback(self):
        """``logger.exception`` 的 ``exc_text`` 也要脱敏 —— 栈里可能有请求头。"""
        self.install()
        attached_later = StreamCollector()
        logging.getLogger().addHandler(attached_later)

        try:
            raise RuntimeError("POST /a2a?token=%s failed" % TELEGRAM_TOKEN)
        except RuntimeError:
            logging.getLogger(PROBE_LOGGER_NAME).exception("call failed")

        self.assertIn("Traceback", attached_later.text)
        self.assertNotIn(TELEGRAM_TOKEN, attached_later.text)

    def test_the_handler_that_existed_at_install_time_still_works(self):
        """**老路没坏**：这一条本来就是对的，改动不许把它悄悄关掉。"""
        on_root = self.install()

        logging.getLogger(PROBE_LOGGER_NAME).warning("token=%s", TELEGRAM_TOKEN)

        self.assertIn("[REDACTED:telegram-bot-token]", on_root.text)
        self.assertNotIn(TELEGRAM_TOKEN, on_root.text)

    def test_a_handler_closer_to_the_source_logger_is_covered_too(self):
        """**修复前真正会漏的那一个形状**：挂在命名空间 logger 上的 handler。

        ``callHandlers`` 从发出记录的 logger 往上走，它比 root **更靠前** —— 过滤器
        在 root 上时它已经拿到明文了。
        """
        self.install()
        on_namespace = StreamCollector()
        logging.getLogger(redaction.LOGGER_NAMESPACE).addHandler(on_namespace)

        logging.getLogger(PROBE_LOGGER_NAME).warning("token=%s", TELEGRAM_TOKEN)

        self.assertNotIn(TELEGRAM_TOKEN, on_namespace.text,
                         "挂在命名空间 logger 上的 handler 拿到了明文 —— "
                         "覆盖仍然依赖 handler 在走位里的位置")

    def test_the_traceback_is_covered_on_that_handler_too(self):
        """同上，但走 ``logger.exception`` —— 栈由 formatter 渲染，所以它测的是
        ``exc_text`` **在记录创建那一刻**就已经被填好并脱敏了。

        ⚠️ 只测 handler 那一层不够：handler 层的过滤器也会填 ``exc_text``，所以
        那个形状下"谁填的"分不出来。这里 handler 在走位里**更靠前**，root 那层根本
        来不及，于是只有"创建时就填好"这一条路能救它。
        """
        self.install()
        on_namespace = StreamCollector()
        logging.getLogger(redaction.LOGGER_NAMESPACE).addHandler(on_namespace)

        try:
            raise RuntimeError("POST /a2a?token=%s failed" % TELEGRAM_TOKEN)
        except RuntimeError:
            logging.getLogger(PROBE_LOGGER_NAME).exception("call failed")

        self.assertIn("Traceback", on_namespace.text, "防空跑：这条根本没渲染出栈")
        self.assertNotIn(TELEGRAM_TOKEN, on_namespace.text,
                         "栈里的凭据漏出去了 —— exc_text 不是在创建记录时就脱敏的")


# ----------------------------------------------------------------------
# 2. 幂等：两层对同一条记录各跑一遍，第二遍必须是恒等变换
# ----------------------------------------------------------------------
class TwoLayersStayIdempotentTests(CoverageTestCase):
    def test_running_the_second_layer_over_an_already_scrubbed_record_changes_nothing(self):
        """handler 那一层在顺序无关那层**之后**又跑一次 —— 必须一个字都不改。"""
        self.install()
        record = logging.LogRecord(
            PROBE_LOGGER_NAME, logging.WARNING, __file__, 1,
            "token=%s conv telegram:123456789 phone 13800138000",
            (TELEGRAM_TOKEN,), None,
        )
        RedactingFilter(make_redactor()).filter(record)      # 第一遍

        before = (record.msg, record.args, record.exc_text)
        RedactingFilter(make_redactor()).filter(record)      # 第二遍：必须是恒等

        self.assertEqual((record.msg, record.args, record.exc_text), before)
        self.assertIsNone(record.args, "第二遍把 args 留了下来 —— 两次之后对不上号")

    def test_the_traceback_is_also_idempotent(self):
        self.install()
        record = logging.LogRecord(
            PROBE_LOGGER_NAME, logging.ERROR, __file__, 1, "boom", None,
            (RuntimeError, RuntimeError("token=%s" % TELEGRAM_TOKEN), None),
        )
        RedactingFilter(make_redactor()).filter(record)
        first_exc_text = record.exc_text
        self.assertIsNotNone(first_exc_text)
        self.assertNotIn(TELEGRAM_TOKEN, first_exc_text)

        RedactingFilter(make_redactor()).filter(record)

        self.assertEqual(record.exc_text, first_exc_text,
                         "第二遍把已经脱敏过的栈又脱敏了一次")

    def test_a_handler_in_the_walk_sees_exactly_one_redaction(self):
        """端到端：一条记录经过两层之后，输出里的遮蔽标记**只出现一次**。"""
        self.install()
        collector = StreamCollector()
        logging.getLogger().addHandler(collector)

        logging.getLogger(PROBE_LOGGER_NAME).warning("password='%s'", LONG_PASSWORD)

        self.assertEqual(collector.text.count("[REDACTED:secret-assignment]"), 1,
                         "遮蔽标记出现了 %d 次 —— 有一层没识别出'已经脱敏过'"
                         % collector.text.count("[REDACTED:secret-assignment]"))
        self.assertNotIn(LONG_PASSWORD, collector.text)


# ----------------------------------------------------------------------
# 3. 不许静默降级
# ----------------------------------------------------------------------
class CoverageMustBeReportableTests(CoverageTestCase):
    def test_the_report_says_the_order_independent_layer_is_in_place(self):
        self.install()

        report = redaction_coverage.coverage_report()

        self.assertTrue(report["record_factory_is_ours"])
        self.assertTrue(report["fully_covered"])
        self.assertIsNone(redaction_coverage.warn_about_uncovered_records(),
                          "覆盖完好时不该报「不覆盖」")

    def test_replacing_the_record_factory_is_reported_not_silently_accepted(self):
        """**有人在我们之后又换了 record factory** ⇒ 脱敏失效，必须**说得出来**。"""
        self.install()
        logging.setLogRecordFactory(logging.LogRecord)     # 模拟库/宿主覆盖

        report = redaction_coverage.coverage_report()

        self.assertFalse(report["record_factory_is_ours"])
        self.assertFalse(report["fully_covered"])
        with self.assertLogs("opencode_bridge.redaction_coverage",
                             level="WARNING") as captured:
            message = redaction_coverage.warn_about_uncovered_records()
        self.assertIsNotNone(message)
        self.assertIn("record factory", "\n".join(captured.output))

    def test_a_handler_is_never_dropped_even_when_scrubbing_explodes(self):
        """脱敏层绝不许丢记录，也绝不许让日志路径抛。"""
        class ExplodingRedactor(Redactor):
            def scrub(self, text):  # type: ignore[override]
                raise RuntimeError("scrub blew up")

        self.install(redactor=ExplodingRedactor(key=b"boom"))
        collector = StreamCollector()
        logging.getLogger().addHandler(collector)

        logging.getLogger(PROBE_LOGGER_NAME).warning("still logged")

        self.assertIn("still logged", collector.text,
                      "脱敏自己炸了就把记录吞了 —— 吞日志比漏值更难查")


# ----------------------------------------------------------------------
# 4. 不越界：只碰本命名空间，也不动别人已有的 factory
# ----------------------------------------------------------------------
class ScopingTests(CoverageTestCase):
    def test_a_foreign_logger_is_left_untouched(self):
        """``opencode_bridge_extra`` 不在命名空间之下 —— 一条都不许被改。

        ⚠️ 记录器必须在**发日志之前**挂上：晚挂的记录器什么也收不到，于是这条
        断言会因为"输出为空"而恒真 —— 而它要证明的恰恰是"输出里有明文"。
        """
        self.install()
        on_foreign = StreamCollector()
        logging.getLogger(FOREIGN_LOGGER_NAME).addHandler(on_foreign)

        logging.getLogger(FOREIGN_LOGGER_NAME).warning("token=%s", TELEGRAM_TOKEN)

        self.assertIn(TELEGRAM_TOKEN, on_foreign.text,
                      "记录器没收到任何东西 —— 这条断言是空的（防空跑）")
        self.assertNotIn("[REDACTED:", on_foreign.text,
                         "越界改了别的命名空间的记录")

    def test_a_pre_existing_record_factory_is_still_the_one_that_runs(self):
        """我们**链**在别人那一层之后，不是把它顶掉 —— 它加的字段必须还在。

        判据落在**记录本身**：宿主的 factory 给每条记录盖了个自己的标记，而标记能
        出现在最终走出来的记录上，就说明它确实被调用过（没被顶掉）。
        """
        host_marker = object()

        def host_factory(name, level, pathname, lineno, msg, args, exc_info,
                         func=None, sinfo=None):
            record = logging.LogRecord(name, level, pathname, lineno, msg, args,
                                       exc_info, func, sinfo)
            record.host_marker = host_marker
            return record

        logging.setLogRecordFactory(host_factory)
        self.install()
        seen: list[logging.LogRecord] = []

        class Peeking(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                seen.append(record)

        logging.getLogger().addHandler(Peeking())
        logging.getLogger(PROBE_LOGGER_NAME).warning("token=%s", TELEGRAM_TOKEN)

        self.assertEqual(len(seen), 1)
        self.assertIs(getattr(seen[0], "host_marker", None), host_marker,
                      "宿主的 record factory 没被调用 —— 我们把它顶掉了")
        self.assertNotIn(TELEGRAM_TOKEN, seen[0].getMessage(),
                         "顺序无关那层没脱敏 —— 说明它绕过了宿主那一层")

    def test_removing_the_coverage_puts_the_previous_factory_back(self):
        """卸载必须**原样**放回原来那个 —— 别的代码不该被我们顶掉。"""
        def host_factory(name, level, pathname, lineno, msg, args, exc_info,
                         func=None, sinfo=None):
            return logging.LogRecord(name, level, pathname, lineno, msg, args,
                                     exc_info, func, sinfo)

        logging.setLogRecordFactory(host_factory)
        self.install()
        self.assertIsNot(logging.getLogRecordFactory(), host_factory)

        self.assertTrue(redaction.remove_redaction_filter())

        self.assertIs(logging.getLogRecordFactory(), host_factory,
                      "卸载没有把原来那个 factory 放回去")

    def _foreign_text_removed(self) -> None:
        """留着是为了让"删掉了那个惰性记录器"这件事在 git 历史里说得清。"""


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

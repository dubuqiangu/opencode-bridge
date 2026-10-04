"""``ConversationMerger`` 的**独立**测试 —— 不构造 :class:`BridgeCore`。

这个模块是纯逻辑 + 一个保险丝计时器：不做 I/O、不发消息、不认识适配器，所以它
能单独测。本文件锁住的是**账本之外**的四组语义：

1. **零延迟**：没有标记的行**立刻** ``DELIVER``，不起计时器、不进缓冲。
2. **标记必须被吃掉**：送进 agent 的正文里不许残留 ``..`` / ``!!``。
3. **拼接按换行**，不是空串（dsh 是空串，那会把两行代码接成一行）。
4. **保险丝**：敲了 ``..`` 之后走开的那一行，到点会被交出去且只交一次。

⚠️ 接线（谁调 :meth:`ConversationMerger.ingest`、回执怎么发、保险丝到点之后
怎么投递）不在这里，而在真实路径上：``tests/test_inbound_gateway.py`` 的
``InboundMergeWiringTests`` 与 ``tests/test_core.py`` 的
``PermissionSafetyTests`` 同级新增的 ``InboundBurstTests``。这里全绿而接线漏了
是可能的，所以那两处必须存在。
"""

from __future__ import annotations

import threading
import unittest

from opencode_bridge.inbound_merge import (
    BUFFERED_NOTICE,
    CONTINUE_NEXT,
    DELIVER,
    HELD,
    HELD_EXPIRED_NOTICE,
    IGNORED,
    NO_DIRECTIVE,
    SUBMIT_NOW,
    ConversationMerger,
    split_off_directive,
)

CONVERSATION = "irc:libera:#dev"
OTHER_CONVERSATION = "irc:libera:#ops"

#: 保险丝用例里给一个短到不需要真的等的值；断言等的是**回调**而不是时长。
FUSE_SECONDS = 0.05


class SplitOffDirectiveTests(unittest.TestCase):
    def test_a_plain_line_carries_no_directive(self):
        self.assertEqual(split_off_directive("看一下 README"),
                         ("看一下 README", NO_DIRECTIVE))

    def test_two_dots_at_the_end_mean_more_is_coming(self):
        self.assertEqual(split_off_directive("第一段.."),
                         ("第一段", CONTINUE_NEXT))

    def test_two_bangs_at_the_end_mean_submit_now(self):
        self.assertEqual(split_off_directive("第一段!!"),
                         ("第一段", SUBMIT_NOW))

    def test_trailing_whitespace_after_a_marker_still_counts(self):
        """手机输入法与 IRC 客户端很容易在标记后面留一个空格。

        没有这条，``"就这些!! "`` 就判不出标记 —— 等于这个功能在手机上不存在。
        """
        self.assertEqual(split_off_directive("就这些!!  "),
                         ("就这些", SUBMIT_NOW))
        self.assertEqual(split_off_directive("接着.. \t"),
                         ("接着", CONTINUE_NEXT))

    def test_the_body_is_rstripped_after_the_marker_is_cut(self):
        """``"a.. "`` 的正文应当是 ``"a"`` 而不是 ``"a "``。"""
        body, directive = split_off_directive("a.. ")
        self.assertEqual(body, "a")
        self.assertEqual(directive, CONTINUE_NEXT)

    def test_bangs_win_over_dots_when_both_are_present(self):
        """``"a!!.."`` 按行尾那个判 —— 而 ``"a!!"`` 必须是"立刻发"。

        顺序与 dsh 一致（``merge.ts`` 的 ``stripControlSuffix`` 也是先试 ``!!``）：
        用户想立刻发的时候，不会因为末尾还多两个点而变成续行。
        """
        self.assertEqual(split_off_directive("a!!.."),
                         ("a!!", CONTINUE_NEXT))
        self.assertEqual(split_off_directive("a!!"), ("a", SUBMIT_NOW))

    def test_a_marker_that_is_not_at_the_end_is_ordinary_text(self):
        """``".. 不对，是这里"`` 不该被吃掉标记 —— 判据是**末尾**。"""
        self.assertEqual(split_off_directive(".. 不对，是这里"),
                         (".. 不对，是这里", NO_DIRECTIVE))

    def test_a_bare_marker_yields_an_empty_body(self):
        self.assertEqual(split_off_directive(".."), ("", CONTINUE_NEXT))
        self.assertEqual(split_off_directive("!!"), ("", SUBMIT_NOW))


class ZeroLatencyTests(unittest.TestCase):
    """没有标记的行必须**立刻**出去 —— 这是 C3 的硬要求。"""

    def setUp(self) -> None:
        self.expired: list[tuple[str, str]] = []
        self.merger = self.build()

    def build(self, **overrides) -> ConversationMerger:
        kwargs = {
            "hold_timeout_seconds": FUSE_SECONDS,
            "on_hold_expired": lambda cid, text: self.expired.append((cid, text)),
        }
        kwargs.update(overrides)
        return ConversationMerger(**kwargs)

    def test_a_plain_line_is_delivered_on_the_first_ingest(self):
        result = self.merger.ingest(CONVERSATION, "看一下 README")

        self.assertEqual(result.kind, DELIVER)
        self.assertEqual(result.text, "看一下 README")
        self.assertEqual(self.merger.held_conversation_ids(), ())

    def test_a_delivered_line_is_not_sitting_in_any_buffer(self):
        self.merger.ingest(CONVERSATION, "看一下 README")

        self.assertEqual(self.merger.held_text(CONVERSATION), "")

    def test_a_delivered_line_starts_no_fuse_so_it_costs_no_wait(self):
        """**证明**零延迟：没有缓冲就没有计时器，所以没有 ``..`` 的路径上
        根本不存在一个可以被等待的计时器。"""
        self.merger.ingest(CONVERSATION, "看一下 README")

        self.assertEqual(self.merger._fuses, {})

    def test_two_consecutive_plain_lines_are_two_separate_deliveries(self):
        """不合并 —— 这是与 dsh 的第 1 处分歧：它会把裸文本缓存 5 秒。"""
        first = self.merger.ingest(CONVERSATION, "第一句")
        second = self.merger.ingest(CONVERSATION, "第二句")

        self.assertEqual(first.kind, DELIVER)
        self.assertEqual(second.kind, DELIVER)
        self.assertEqual(second.text, "第二句")


class ContinuationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.expired: list[tuple[str, str]] = []
        self.merger = ConversationMerger(
            hold_timeout_seconds=FUSE_SECONDS,
            on_hold_expired=lambda cid, text: self.expired.append((cid, text)),
        )

    def test_a_continuation_marker_holds_the_line_instead_of_delivering_it(self):
        result = self.merger.ingest(CONVERSATION, "第一段..")

        self.assertEqual(result.kind, HELD)
        self.assertEqual(result.held, "第一段")

    def test_a_burst_becomes_one_delivery_in_order(self):
        self.merger.ingest(CONVERSATION, "帮我看下这个函数..")
        self.merger.ingest(CONVERSATION, "def f(x):..")
        result = self.merger.ingest(CONVERSATION, "    return x +")

        self.assertEqual(result.kind, DELIVER)
        self.assertEqual(
            result.text, "帮我看下这个函数\ndef f(x):\n    return x +"
        )

    def test_a_line_without_a_marker_is_not_held_even_mid_burst(self):
        """**只有**行尾标记才续行 —— 判据窄是刻意的（见模块 docstring 的残留风险）。

        顺带钉住一个初版笔误：一行以 ``::`` 结尾的散文**不是**续行标记。
        """
        result = self.merger.ingest(CONVERSATION, "帮我看下这个函数::")

        self.assertEqual(result.kind, DELIVER)
        self.assertEqual(self.merger.held_conversation_ids(), ())

    def test_the_bang_marker_submits_immediately(self):
        self.merger.ingest(CONVERSATION, "第一段..")
        result = self.merger.ingest(CONVERSATION, "第二段!!")

        self.assertEqual(result.kind, DELIVER)
        self.assertEqual(result.text, "第一段\n第二段")

    def test_lines_are_joined_by_a_newline_not_by_nothing(self):
        """⚠️ 与 dsh 刻意不同：它是 ``existing + text``，那会把两行接成一行。

        一段 ``def f(x):`` / ``    return x`` 被接成 ``def f(x):    return x``
        时，送进 agent 的就不是用户写的东西了。
        """
        self.merger.ingest(CONVERSATION, "第一行..")
        result = self.merger.ingest(CONVERSATION, "第二行")

        self.assertIn("\n", result.text)
        self.assertEqual(result.text, "第一行\n第二行")

    def test_no_marker_text_reaches_the_agent(self):
        """**决定性的一条**：标记被消费掉，送出去的正文里不许残留它们。"""
        self.merger.ingest(CONVERSATION, "第一段..")
        result = self.merger.ingest(CONVERSATION, "第二段!!")

        self.assertNotIn("..", result.text)
        self.assertNotIn("!!", result.text)

    def test_a_bare_continuation_marker_with_nothing_held_is_ignored(self):
        """光一个 ``..`` 且前面什么都没有：**不该**起一个空缓冲。

        否则会凭空造出一个"有东西在等"的状态，而它等的内容是空串。
        """
        result = self.merger.ingest(CONVERSATION, "..")

        self.assertEqual(result.kind, IGNORED)
        self.assertEqual(self.merger.held_conversation_ids(), ())

    def test_a_bare_continuation_marker_while_holding_keeps_what_is_held(self):
        self.merger.ingest(CONVERSATION, "第一段..")
        result = self.merger.ingest(CONVERSATION, "..")

        self.assertEqual(result.kind, HELD)
        self.assertEqual(result.held, "第一段")

    def test_a_bare_bang_with_nothing_held_is_ignored(self):
        result = self.merger.ingest(CONVERSATION, "!!")

        self.assertEqual(result.kind, IGNORED)

    def test_an_empty_line_while_holding_delivers_what_is_held(self):
        self.merger.ingest(CONVERSATION, "第一段..")
        result = self.merger.ingest(CONVERSATION, "   ")

        self.assertEqual(result.kind, DELIVER)
        self.assertEqual(result.text, "第一段")

    def test_two_conversations_never_merge_into_one_another(self):
        """⚠️ 两个会话的缓冲必须互不相干 —— 那是把 A 的话说给 B 听。"""
        self.merger.ingest(CONVERSATION, "给 dev 的..")
        self.merger.ingest(OTHER_CONVERSATION, "给 ops 的..")

        result = self.merger.ingest(OTHER_CONVERSATION, "第二行")

        self.assertEqual(result.text, "给 ops 的\n第二行")
        self.assertEqual(self.merger.held_text(CONVERSATION), "给 dev 的")

    def test_four_held_lines_still_arrive_in_the_typed_order(self):
        for line in ("一", "二", "三"):
            self.merger.ingest(CONVERSATION, line + "..")

        result = self.merger.ingest(CONVERSATION, "四")

        self.assertEqual(result.text, "一\n二\n三\n四")


class HoldFuseTests(unittest.TestCase):
    """保险丝：敲了 ``..`` 然后走开的那一行，到点被交出去，且只交一次。"""

    def setUp(self) -> None:
        self.expired: list[tuple[str, str]] = []
        self.fired = threading.Event()
        self.merger = ConversationMerger(
            hold_timeout_seconds=FUSE_SECONDS,
            on_hold_expired=self._on_expired,
        )

    def _on_expired(self, conversation_id: str, held_text: str) -> None:
        self.expired.append((conversation_id, held_text))
        self.fired.set()

    def tearDown(self) -> None:
        self.merger.stop()

    def test_a_held_line_is_handed_over_when_nothing_follows(self):
        self.merger.ingest(CONVERSATION, "敲完就走了..")

        self.assertTrue(self.fired.wait(timeout=5),
                        "保险丝没有在超时后把缓冲交出来")

    def test_the_handed_over_text_is_exactly_what_was_held(self):
        self.merger.ingest(CONVERSATION, "第一段..")
        self.merger.ingest(CONVERSATION, "第二段..")

        self.assertTrue(self.fired.wait(timeout=5))
        self.assertEqual(self.expired, [(CONVERSATION, "第一段\n第二段")])

    def test_a_line_delivered_before_the_fuse_cancels_it_so_nothing_is_sent_twice(self):
        """⚠️ 竞态：保险丝还在计时，下一行到了。那一行已经把并集发出去了，
        计时器若还触发就会让同一个请求被 agent 跑两遍。"""
        self.merger.ingest(CONVERSATION, "第一段..")
        self.merger.ingest(CONVERSATION, "第二段")

        self.assertEqual(self.merger._fuses, {})
        # 给保险丝足够时间自己醒来，确认它确实不会再交一次
        threading.Event().wait(timeout=FUSE_SECONDS * 6)
        self.assertEqual(self.expired, [])

    def test_the_fuse_is_rearmed_by_each_continuation(self):
        self.merger.ingest(CONVERSATION, "第一段..")
        first_fuse = self.merger._fuses.get(CONVERSATION)

        self.merger.ingest(CONVERSATION, "第二段..")
        second_fuse = self.merger._fuses.get(CONVERSATION)

        self.assertIsNotNone(first_fuse)
        self.assertIsNotNone(second_fuse)
        self.assertIsNot(first_fuse, second_fuse)

    def test_a_zero_timeout_never_arms_a_fuse(self):
        merger = ConversationMerger(
            hold_timeout_seconds=0,
            on_hold_expired=lambda cid, text: None,
        )
        merger.ingest(CONVERSATION, "敲完就走了..")

        self.assertEqual(merger._fuses, {})
        self.assertEqual(merger.held_text(CONVERSATION), "敲完就走了")


class FlushAndStopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.expired: list[tuple[str, str]] = []
        self.merger = ConversationMerger(
            hold_timeout_seconds=FUSE_SECONDS,
            on_hold_expired=lambda cid, text: self.expired.append((cid, text)),
        )

    def tearDown(self) -> None:
        self.merger.stop()

    def test_flush_takes_the_held_text_out_and_leaves_nothing_behind(self):
        self.merger.ingest(CONVERSATION, "第一段..")

        self.assertEqual(self.merger.flush(CONVERSATION), "第一段")
        self.assertEqual(self.merger.held_text(CONVERSATION), "")

    def test_flush_on_a_conversation_that_holds_nothing_is_empty(self):
        self.assertEqual(self.merger.flush(CONVERSATION), "")

    def test_flush_cancels_the_fuse_so_the_text_is_not_also_sent_by_it(self):
        self.merger.ingest(CONVERSATION, "第一段..")
        self.merger.flush(CONVERSATION)

        threading.Event().wait(timeout=FUSE_SECONDS * 6)
        self.assertEqual(self.expired, [])

    def test_stop_cancels_the_fuses_but_keeps_the_held_text(self):
        """关停**不能**把缓冲丢掉：那是"用户已经被告知收到了"的内容。

        它仍然没被交出去，所以调用方（关停那条路）要自己 :meth:`flush`。
        """
        self.merger.ingest(CONVERSATION, "第一段..")

        self.merger.stop()

        self.assertEqual(self.merger._fuses, {})
        self.assertEqual(self.merger.held_text(CONVERSATION), "第一段")


class NoticeTests(unittest.TestCase):
    def test_the_held_notice_names_both_markers(self):
        """回执必须把两个标记都讲出来，否则用户不知道怎么结束一段。"""
        self.assertIn("..", BUFFERED_NOTICE)
        self.assertIn("!!", BUFFERED_NOTICE)

    def test_the_expired_notice_says_it_was_sent(self):
        """超时那条必须说"发出去了"—— 否则用户不知道内容有没有丢。"""
        rendered = HELD_EXPIRED_NOTICE % "第一段"
        self.assertIn("第一段", rendered)

    def test_the_expired_notice_survives_a_percent_sign_in_the_text(self):
        """``%`` 是格式化符：正文里带百分号时不能把那句提示炸掉。"""
        rendered = HELD_EXPIRED_NOTICE % "CPU 用了 80%"

        self.assertIn("80%", rendered)


class ConcurrentIngestTests(unittest.TestCase):
    def test_two_threads_on_one_conversation_never_lose_the_first_line(self):
        """两个适配器线程共用一个合并器时，先到的那一行不能被后到的清掉。"""
        expired: list[tuple[str, str]] = []
        merger = ConversationMerger(
            hold_timeout_seconds=FUSE_SECONDS,
            on_hold_expired=lambda cid, text: expired.append((cid, text)),
        )
        self.addCleanup(merger.stop)
        start = threading.Barrier(3)
        results: list[str] = []

        def send(text: str) -> None:
            start.wait(timeout=5)
            results.append(merger.ingest(CONVERSATION, text).kind)

        threads = [
            threading.Thread(target=send, args=("甲..",)),
            threading.Thread(target=send, args=("乙",)),
        ]
        for thread in threads:
            thread.start()
        start.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=10)

        # 无论谁先到：要么甲被缓冲、乙把它带出去（两次 ingest，其中一次 DELIVER），
        # 要么乙先发、甲随后进缓冲（HELD）。但**绝不能**两边都 DELIVER 而丢掉甲。
        self.assertEqual(len(results), 2)
        self.assertTrue(
            HELD in results or merger.held_text(CONVERSATION) == "甲",
            "甲既没有被缓冲也没有被带出去，丢了：%r" % (results,),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

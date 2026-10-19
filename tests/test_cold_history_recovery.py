"""``cold_history_recovery.py`` 测试：B 冷历史恢复答复的纯函数多臂 + 恢复路径接线。

这一层分两半：

1. **纯函数域**（:func:`~opencode_bridge.cold_history_recovery.find_reply_for_prompt`）
   按 ``queued.text`` + 时间序，从消息历史里定位最终 assistant 正文。**不碰模型、
   不重放工具** —— 它只是一次**只读回查**，把"那一轮最终正文"找出来还给调用方。

2. **恢复路径接线**（:func:`~opencode_bridge.inbox_recovery.recover_pending` 注入
   ``recover_cold``）：结果不可知那一档，先试冷历史恢复 —— 命中就把那行 settle 成
   ``delivered`` + 记进 :attr:`RecoveryOutcome.recovered`、**不再告警**；不命中或抛异常
   才退回既有告警。``recover_cold`` 缺省为 ``None`` 时行为逐字节不变（只告警）。

纯函数测试拿**手摆的消息列表**（``{type:"user", text}`` / ``{type:"assistant",
content:[{type:"text", text}]}``）跑 —— 零磁盘、零 I/O。恢复路径测试复用
:mod:`tests.test_inbox_recovery` 里的 :class:`FakeInbox` 与两个 Recording 替身。
"""

from __future__ import annotations

import unittest

from opencode_bridge.cold_history_recovery import find_reply_for_prompt
from opencode_bridge.inbox import DeliveryState
from opencode_bridge.inbox_recovery import RecoveryOutcome, recover_pending

# 复用 test_inbox_recovery 的假收件箱与替身 —— 同一份纯策略，同一批桩。
from tests.test_inbox_recovery import (
    FakeInbox,
    RecordingDispatcher,
    RecordingNotifier,
    make_prompt,
)


def user(text: str) -> dict:
    """一条 ``{type: "user", text}`` 消息。"""
    return {"type": "user", "text": text}


def assistant(*text_parts: str) -> dict:
    """一条 ``{type: "assistant", content: [...]}`` 消息。

    传入的每个段都作为一块 ``type == "text"`` 的正文；不传段 = 纯工具调用（无正文）。
    """
    return {"type": "assistant", "content": [{"type": "text", "text": part} for part in text_parts]}


def tool_only_assistant() -> dict:
    """一条只带工具调用、**没有** ``type == "text"`` 正文块的 assistant 消息。"""
    return {"type": "assistant", "content": [{"type": "tool_call", "name": "read_file"}]}


class TestFindReplyForPrompt(unittest.TestCase):
    """:func:`find_reply_for_prompt` 的纯函数多臂。"""

    def test_a_normal_user_then_assistant_returns_the_assistant_body(self):
        messages = [user("看一下 README"), assistant("README 有 12 节。")]
        self.assertEqual(find_reply_for_prompt(messages, "看一下 README"),
                         "README 有 12 节。")

    def test_a_multi_block_assistant_body_is_joined_in_order(self):
        messages = [user("总结"), assistant("第一段。", "第二段。", "第三段。")]
        self.assertEqual(find_reply_for_prompt(messages, "总结"),
                         "第一段。第二段。第三段。")

    def test_same_text_resend_takes_the_first_match_followed_by_a_real_body(self):
        """同文本重发：取**时间序内首个、且之后确有 assistant 正文**的 user 消息。

        第一个匹配后面只有纯工具调用（无正文 ⇒ 被抑制），再被一条别的指令切断，
        第二个匹配后面才有真正文 ⇒ 命中第二个。首个"有正文"的匹配才作数。
        """
        messages = [
            user("重发这句话"),                      # 第一个匹配 —— 但后面只有纯工具调用
            tool_only_assistant(),                  # 无正文 ⇒ 该候选被抑制
            user("别的指令"),                       # 切断上一段候选
            user("重发这句话"),                      # 第二个匹配 —— 后面有真正文
            assistant("这一轮的最终答复"),
        ]
        self.assertEqual(find_reply_for_prompt(messages, "重发这句话"),
                         "这一轮的最终答复")

    def test_a_user_followed_only_by_tool_calls_is_suppressed(self):
        """interim 抑制：匹配的 user 后面只有纯工具调用的 assistant ⇒ 返回 ``None``。"""
        messages = [user("跑一下工具"), tool_only_assistant()]
        self.assertIsNone(find_reply_for_prompt(messages, "跑一下工具"))

    def test_an_out_of_order_assistant_before_the_user_is_ignored(self):
        """乱序 / 消息交错：assistant 出现在匹配的 user **之前** ⇒ 不算它的答复。"""
        messages = [
            assistant("先出现的答复"),
            user("请回答问题"),
            assistant("真正的答复"),
        ]
        self.assertEqual(find_reply_for_prompt(messages, "请回答问题"), "真正的答复")

    def test_an_empty_message_list_returns_none(self):
        self.assertIsNone(find_reply_for_prompt([], "任何提示"))

    def test_a_user_message_without_a_text_field_is_skipped(self):
        """形状不符的 user（无 ``text``）被跳过 —— 不会误当成候选取答复。"""
        messages = [
            {"type": "user", "text": "真问题"},
            assistant("真答复"),
            {"type": "user"},                        # 无 text 字段
            assistant("被误配的答复"),
        ]
        self.assertEqual(find_reply_for_prompt(messages, "真问题"), "真答复")

    def test_no_matching_prompt_returns_none(self):
        messages = [user("另一个问题"), assistant("答复")]
        self.assertIsNone(find_reply_for_prompt(messages, "不存在的提示"))

    def test_a_matching_user_with_no_following_assistant_returns_none(self):
        """最后一个匹配的 user 后面**没有**任何 assistant ⇒ 找不到最终正文。"""
        messages = [user("唯一问题")]
        self.assertIsNone(find_reply_for_prompt(messages, "唯一问题"))

    def test_a_tool_only_then_a_text_assistant_still_returns_the_text_body(self):
        """中间夹纯工具、后续确有正文 ⇒ 正文照样返回（抑制只针对「那一段答复」）。"""
        messages = [
            user("跑工具再看"),
            tool_only_assistant(),
            assistant("基于工具输出的正文答复"),
        ]
        self.assertEqual(find_reply_for_prompt(messages, "跑工具再看"),
                         "基于工具输出的正文答复")

    def test_a_wrong_shaped_message_is_skipped_not_crashed(self):
        """消息形状不对（如缺 ``type`` / ``content`` 非列表）⇒ 跳过，不抛异常。"""
        messages = [
            {"text": "没有 type 的 user"},
            user("真问题"),
            {"type": "assistant", "content": "不是列表"},
            assistant("真答复"),
        ]
        self.assertEqual(find_reply_for_prompt(messages, "真问题"), "真答复")

    def test_an_assistant_with_no_text_blocks_returns_none_for_that_candidate(self):
        """``_assistant_text`` 对无文本块的消息返回 ``None`` —— 不该被当成答复。"""
        messages = [user("q"), assistant(), assistant("有正文的")]
        self.assertEqual(find_reply_for_prompt(messages, "q"), "有正文的")


class TestRecoverPendingWithRecoverCold(unittest.TestCase):
    """``recover_pending`` 注入 ``recover_cold`` 后的三臂 + 缺省对照。"""

    _DELIVERY_ID = "telegram:100200:u99"

    def _one_uncertain_inbox(self) -> FakeInbox:
        inbox = FakeInbox()
        inbox.add(make_prompt(self._DELIVERY_ID), DeliveryState.OUTCOME_UNKNOWN)
        return inbox

    def test_a_hit_marks_the_row_delivered_and_recovered_not_uncertain(self):
        """命中：行 settle 成 ``delivered``、进 ``recovered``、**不**进 ``uncertain``。"""
        inbox = self._one_uncertain_inbox()
        notify = RecordingNotifier()

        def recover_cold(prompt):
            return "从冷历史找回的正文"

        outcome = recover_pending(
            inbox,
            dispatch=RecordingDispatcher(),
            notify=notify,
            recover_cold=recover_cold,
        )

        self.assertEqual([prompt.delivery_id for prompt in outcome.recovered],
                         [self._DELIVERY_ID])
        self.assertEqual(outcome.uncertain, [],
                         "命中即 settle —— 该行不能再走「只告警」那一档")
        self.assertEqual(outcome.replayed, [])
        self.assertEqual([(delivery_id, state) for delivery_id, state in inbox.settled_changes],
                         [(self._DELIVERY_ID, DeliveryState.DELIVERED)])
        self.assertEqual(notify.alerts, [], "命中即不告警")

    def test_a_recover_cold_returning_none_keeps_the_row_uncertain(self):
        """不命中（返回 ``None``）：那行照旧 ``uncertain`` + 告警，不动。"""
        inbox = self._one_uncertain_inbox()
        notify = RecordingNotifier()

        def recover_cold(prompt):
            return None

        outcome = recover_pending(
            inbox,
            dispatch=RecordingDispatcher(),
            notify=notify,
            recover_cold=recover_cold,
        )

        self.assertEqual(outcome.recovered, [])
        self.assertEqual([prompt.delivery_id for prompt in outcome.uncertain],
                         [self._DELIVERY_ID])
        self.assertEqual(inbox.settled_changes, [], "不命中不动那行")
        self.assertIn("请重新发送一次", notify.alerts[0][1])

    def test_a_raising_recover_cold_keeps_the_row_uncertain(self):
        """恢复回调抛异常 ⇒ 吞掉、照旧 ``uncertain`` + 告警，不抛出（恢复不许让桥起不来）。"""
        inbox = self._one_uncertain_inbox()
        notify = RecordingNotifier()

        def recover_cold(prompt):
            raise OSError("取历史失败")

        outcome = recover_pending(
            inbox,
            dispatch=RecordingDispatcher(),
            notify=notify,
            recover_cold=recover_cold,
        )

        self.assertEqual(outcome.recovered, [])
        self.assertEqual([prompt.delivery_id for prompt in outcome.uncertain],
                         [self._DELIVERY_ID])
        self.assertIn("请重新发送一次", notify.alerts[0][1])

    def test_uncertain_row_is_never_dispatched_even_when_recover_cold_is_present(self):
        """命中走的是**冷历史正文**，不是把 prompt 重放给 agent —— 重放集合必须仍是空。"""
        inbox = self._one_uncertain_inbox()
        dispatch = RecordingDispatcher()

        def recover_cold(prompt):
            return "冷历史正文"

        outcome = recover_pending(
            inbox,
            dispatch=dispatch,
            notify=RecordingNotifier(),
            recover_cold=recover_cold,
        )

        self.assertEqual(dispatch.dispatched, [],
                         "冷历史恢复**不属于重放** —— agent 绝不对同一条指令跑两遍")
        self.assertEqual(outcome.replayed, [])
        self.assertEqual([prompt.delivery_id for prompt in outcome.recovered],
                         [self._DELIVERY_ID])

    def test_default_recover_cold_is_none_so_behaviour_is_unchanged(self):
        """缺省对照：不传 ``recover_cold`` 时行为与 B 之前逐字节一致（只告警）。"""
        inbox = self._one_uncertain_inbox()
        notify = RecordingNotifier()

        outcome = recover_pending(
            inbox, dispatch=RecordingDispatcher(), notify=notify
        )

        self.assertEqual(outcome.recovered, [])
        self.assertEqual([prompt.delivery_id for prompt in outcome.uncertain],
                         [self._DELIVERY_ID])
        self.assertEqual(inbox.settled_changes, [], "缺省时那行不被 settle")
        self.assertIn("请重新发送一次", notify.alerts[0][1])


class TestRecoveryOutcomeHasRecoveredField(unittest.TestCase):
    """``RecoveryOutcome`` 带 ``recovered`` 字段 —— 且默认空，不破坏既有消费方。"""

    def test_the_recovered_field_defaults_to_empty(self):
        outcome = RecoveryOutcome()
        self.assertEqual(outcome.recovered, [])

    def test_the_recovered_field_is_list_typed_and_independent(self):
        first = RecoveryOutcome()
        first.recovered.append("x")
        second = RecoveryOutcome()
        self.assertEqual(second.recovered, [],
                         "default_factory ⇒ 每个实例独立，互不串")
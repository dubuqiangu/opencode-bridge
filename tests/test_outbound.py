"""``OutboundSender`` 的**独立**测试 —— 全程不构造 :class:`BridgeCore`。

这一整个文件存在的理由是 AGENTS.md §5.1 说的"抽出去"那种拆法：新类必须能脱离
原类单独测。所以这里没有 ``BridgeCore``、没有真适配器；两个依赖（``adapter_for``
与 ``max_message_chars``）一个是本文件现造的路由替身，一个是构造时读一次的数。

因此这里能断言 core 层面**看不见**的东西：出站正文**一律**过一遍消毒、
进度改写**永不**退化成新发一条（会刷屏）、收尾改不动时**才**退化成新发一条、
以及 ``kind`` 是收尾语义（``adapters/a2a.py`` 靠 ``"error"`` 判失败）。
"""

from __future__ import annotations

import inspect
import unittest

from opencode_bridge.adapters import Adapter
from opencode_bridge.core import BridgeCore
from opencode_bridge.hooks import MsgHandle, Outbound
from opencode_bridge.outbound import NO_OUTPUT_TEXT, OutboundSender

CONVERSATION = "chat:55"
PLATFORM = "telegram"


class ScriptedAdapter(Adapter):
    """每个方法的结果与异常都可编程，用来走完收尾那四条分支。"""

    def __init__(self, name: str = PLATFORM) -> None:
        super().__init__({}, hooks=None)  # type: ignore[arg-type]
        self.name = name
        self.label = name
        self.sent: list[Outbound] = []
        self.edited: list[tuple[MsgHandle, Outbound]] = []
        self.answers: list[tuple[str, str]] = []
        self.send_error: Exception | None = None
        self.answer_error: Exception | None = None
        #: "ok" | "false" | "too_long" | "boom"
        self.edit_result = "ok"
        self._handles = 0

    def start(self) -> None:
        return None

    def stop(self, timeout: float = 5.0) -> None:
        return None

    def send(self, out: Outbound) -> MsgHandle | None:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(out)
        self._handles += 1
        return MsgHandle(out.conversation_id, "m%d" % self._handles, self.name)

    def edit(self, handle: MsgHandle, out: Outbound) -> bool:
        self.edited.append((handle, out))
        if self.edit_result == "too_long":
            raise ValueError("message too long")
        if self.edit_result == "boom":
            raise RuntimeError("edit exploded")
        return self.edit_result != "false"

    def answer(self, query_id: str, text: str = "") -> None:
        if self.answer_error is not None:
            raise self.answer_error
        self.answers.append((query_id, text))


# ----------------------------------------------------------------------
# 1: 依赖面
# ----------------------------------------------------------------------
class CollaboratorSurfaceTests(unittest.TestCase):
    def test_the_constructor_takes_the_two_injected_dependencies(self):
        parameters = inspect.signature(OutboundSender.__init__).parameters
        self.assertEqual([name for name in parameters if name != "self"],
                         ["adapter_for", "max_message_chars"])
        for name, parameter in parameters.items():
            if name == "self":
                continue
            self.assertEqual(parameter.kind, inspect.Parameter.KEYWORD_ONLY, name)
            self.assertIs(parameter.default, inspect.Parameter.empty, name)

    def test_no_bridge_core_is_reachable_from_the_sender(self):
        sender = OutboundSender(adapter_for=lambda conversation_id: None,
                                max_message_chars=4000)

        for name, value in vars(sender).items():
            self.assertNotIsInstance(value, BridgeCore, name)


# ----------------------------------------------------------------------
# 基类
# ----------------------------------------------------------------------
class OutboundSenderTestCase(unittest.TestCase):
    max_message_chars = 4000

    def setUp(self) -> None:
        self.adapter = ScriptedAdapter()
        self.asked: list[str] = []
        self.sender = OutboundSender(
            adapter_for=self._adapter_for,
            max_message_chars=self.max_message_chars,
        )
        self.handle = MsgHandle(CONVERSATION, "m1", PLATFORM)

    def _adapter_for(self, conversation_id: str):
        self.asked.append(conversation_id)
        return self.adapter

    def texts_of(self) -> list[str]:
        return [out.text for out in self.adapter.sent]

    def kinds_of(self) -> list[str]:
        return [out.kind for out in self.adapter.sent]


# ----------------------------------------------------------------------
# 2: 按钮应答
# ----------------------------------------------------------------------
class AnswerTests(OutboundSenderTestCase):
    def test_the_ack_reaches_the_adapter(self):
        self.sender.answer(self.adapter, "Q1", "已处理")

        self.assertEqual(self.adapter.answers, [("Q1", "已处理")])

    def test_no_adapter_or_no_query_id_answers_nothing(self):
        self.sender.answer(None, "Q1", "已处理")
        self.sender.answer(self.adapter, "", "已处理")

        self.assertEqual(self.adapter.answers, [])

    def test_a_failing_adapter_answer_is_logged_not_raised(self):
        """按钮应答失败不该把 on_callback 整条路带走。"""
        self.adapter.answer_error = RuntimeError("adapter gone")

        with self.assertLogs("opencode_bridge.outbound", level="ERROR"):
            self.sender.answer(self.adapter, "Q1", "已处理")


# ----------------------------------------------------------------------
# 3: 发信
# ----------------------------------------------------------------------
class SendTextTests(OutboundSenderTestCase):
    def test_the_handle_comes_back_and_the_text_is_sanitised(self):
        handle = self.sender.send_text(CONVERSATION, "带着\x00控制符\x07的正文")

        self.assertIsNotNone(handle)
        self.assertEqual(self.texts_of(), ["带着控制符的正文"])
        self.assertEqual(self.kinds_of(), ["text"])

    def test_a_missing_adapter_warns_and_sends_nothing(self):
        sender = OutboundSender(adapter_for=lambda conversation_id: None,
                                max_message_chars=4000)
        with self.assertLogs("opencode_bridge.outbound", level="WARNING"):
            self.assertIsNone(sender.send_text(CONVERSATION, "没人接"))

    def test_a_failing_send_is_logged_and_reported_as_no_handle(self):
        self.adapter.send_error = RuntimeError("socket closed")

        with self.assertLogs("opencode_bridge.outbound", level="ERROR"):
            self.assertIsNone(self.sender.send_text(CONVERSATION, "发不出去"))

    def test_an_explicit_adapter_skips_the_lookup(self):
        self.sender.send_text(CONVERSATION, "正文", adapter=self.adapter)

        self.assertEqual(self.asked, [])


# ----------------------------------------------------------------------
# 4: 进度改写 —— 永不退化成新发一条
# ----------------------------------------------------------------------
class EditProgressTests(OutboundSenderTestCase):
    def test_a_successful_edit_returns_true(self):
        self.assertTrue(
            self.sender.edit_progress(CONVERSATION, self.handle, "进度", "ses_1"))

        self.assertEqual(self.adapter.sent, [])
        self.assertEqual(self.adapter.edited[0][1].kind, "progress")

    def test_a_refused_edit_never_becomes_a_new_message(self):
        """退化成 send 就是刷屏 —— 进度消息宁可停在那里。

        ⚠️ 三条失败路的日志级别不一样：被拒（``ValueError``）与异常是 WARNING，
        ``edit`` 返回 ``False`` 只是 DEBUG —— 所以这里从 DEBUG 起抓。
        """
        for outcome in ("too_long", "boom", "false"):
            with self.subTest(outcome=outcome):
                self.adapter = ScriptedAdapter()
                self.adapter.edit_result = outcome
                sender = OutboundSender(
                    adapter_for=lambda conversation_id: self.adapter,
                    max_message_chars=4000)

                with self.assertLogs("opencode_bridge.outbound", level="DEBUG"):
                    self.assertFalse(sender.edit_progress(
                        CONVERSATION, self.handle, "进度", "ses_1"))

                self.assertEqual(self.adapter.sent, [])

    def test_no_adapter_means_no_edit(self):
        sender = OutboundSender(adapter_for=lambda conversation_id: None,
                                max_message_chars=4000)

        self.assertFalse(sender.edit_progress(CONVERSATION, self.handle,
                                              "进度", "ses_1"))


# ----------------------------------------------------------------------
# 5: 收尾 —— 改不动才退化成新发一条
# ----------------------------------------------------------------------
class FinalizeTests(OutboundSenderTestCase):
    def finalize(self, *, handle=..., text="最终答复", kind="final") -> None:
        target = self.handle if handle is ... else handle
        self.sender.finalize(CONVERSATION, target, text, "ses_1", kind=kind)

    def test_an_editable_progress_message_is_rewritten_in_place(self):
        self.finalize()

        self.assertEqual(self.adapter.sent, [])
        self.assertEqual(self.adapter.edited[0][1].text, "最终答复")
        self.assertEqual(self.adapter.edited[0][1].kind, "final")

    def test_a_refused_edit_falls_back_to_a_plain_send(self):
        """三种"改不动"都要另发一条完整消息。

        ⚠️ ``edit`` 返回 ``False`` 那条是**静默**的（只有被拒与异常才记日志），
        所以这里断言的是可观察的结果 —— 那条消息真的发出去了。
        """
        for outcome in ("too_long", "boom", "false"):
            with self.subTest(outcome=outcome):
                self.adapter = ScriptedAdapter()
                self.adapter.edit_result = outcome
                sender = OutboundSender(
                    adapter_for=lambda conversation_id: self.adapter,
                    max_message_chars=4000)
                sender.finalize(CONVERSATION, self.handle, "最终答复", "ses_1")

                self.assertEqual(self.texts_of(), ["最终答复"])
                self.assertEqual(self.adapter.edited[0][1].kind, "final")

    def test_the_two_logged_edit_failures_say_why(self):
        for outcome in ("too_long", "boom"):
            with self.subTest(outcome=outcome):
                self.adapter = ScriptedAdapter()
                self.adapter.edit_result = outcome
                sender = OutboundSender(
                    adapter_for=lambda conversation_id: self.adapter,
                    max_message_chars=4000)
                with self.assertLogs("opencode_bridge.outbound",
                                     level="WARNING"):
                    sender.finalize(CONVERSATION, self.handle, "最终答复", "ses_1")

    def test_without_a_handle_it_just_sends(self):
        self.finalize(handle=None)

        self.assertEqual(self.texts_of(), ["最终答复"])
        self.assertEqual(self.adapter.edited, [])

    def test_text_over_the_cap_is_sent_rather_than_edited(self):
        """装不下就别改 —— 适配器自己会切分。"""
        sender = OutboundSender(adapter_for=self._adapter_for,
                                max_message_chars=10)
        sender.finalize(CONVERSATION, self.handle, "很长很长很长的一段答复", "ses_1")

        self.assertEqual(self.adapter.edited, [])
        self.assertEqual(self.texts_of(), ["很长很长很长的一段答复"])

    def test_blank_text_falls_back_to_the_placeholder(self):
        self.finalize(text="\x00\x07")

        self.assertEqual(self.adapter.edited[0][1].text, NO_OUTPUT_TEXT)

    def test_the_error_kind_is_carried_through(self):
        """``adapters/a2a.py`` 靠 ``kind == "error"`` 把任务判成失败，所以这一条
        不能落到默认值上。"""
        self.finalize(kind="error")

        self.assertEqual(self.adapter.edited[0][1].kind, "error")

    def test_no_adapter_means_neither_edit_nor_send(self):
        sender = OutboundSender(adapter_for=lambda conversation_id: None,
                                max_message_chars=4000)
        with self.assertLogs("opencode_bridge.outbound", level="WARNING"):
            sender.finalize(CONVERSATION, self.handle, "最终答复", "ses_1")


if __name__ == "__main__":
    unittest.main()

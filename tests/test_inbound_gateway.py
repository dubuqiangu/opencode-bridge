"""``InboundGateway`` 的**独立**测试 —— 全程不构造 :class:`BridgeCore`。

这一整个文件存在的理由是 AGENTS.md §5.1 说的"抽出去"那种拆法：新类必须能脱离
原类单独测。所以这里没有 ``make_env``、没有适配器挂载、没有 ``BridgeCore``；
十一个协作者要么是真对象（``threading`` 的锁、真 ``InboundInbox``）、要么是本文件
现造的替身。

因此这里能断言 core 层面**看不见**的东西：命令绝不写前落盘、去重命中不投递、
409 退回队首而不是算失败、``attempting`` 必须紧贴 ``prompt()``、恢复必须等事件流
确认、恢复路径**不写**收件箱（那是 ``inbox_recovery`` 的账）—— 那些全是协作契约。

⚠️ 收件箱的**状态语义**由 ``test_inbox.py`` / ``test_inbox_recovery.py`` /
``test_inbox_wiring.py`` 锁住；这里只测接线（哪个方法写了哪一次账）。
"""

from __future__ import annotations

import inspect
import os
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from opencode_bridge.adapters import Adapter
from opencode_bridge.channel_profile import _HINT_SEPARATOR
from opencode_bridge.commands import _setup_guide
from opencode_bridge.core import BridgeCore
from opencode_bridge.event_stream import Turn
from opencode_bridge.hooks import Inbound, MsgHandle, Outbound
from opencode_bridge.inbound_gateway import (
    PROGRESS_TEXT,
    InboundGateway,
    _queued_prompt_for,
    _record_inbound,
)
from opencode_bridge.inbox import DeliveryState, InboundInbox, QueuedPrompt
from opencode_bridge.opencode_client import OpenCodeError

CONVERSATION = "chat:55"
OTHER_CONVERSATION = "chat:66"
PLATFORM = "fake"
SESSION_ID = "ses_fake0001"

#: 仓库内的临时目录：即便 TEMP/TMP 指向机器别处，测试也不可能写到仓库之外。
_REPOSITORY_TEMP = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".tmp"
)


# ----------------------------------------------------------------------
# 替身
# ----------------------------------------------------------------------
class RecordingClient:
    """``InboundGateway`` 只用到 ``prompt`` 与 ``reply_permission``。"""

    def __init__(self) -> None:
        self.prompts: list[tuple[str, str]] = []
        self.permission_replies: list[tuple[str, str, str]] = []
        self.prompt_errors: list[Exception] = []
        self.permission_errors: list[Exception] = []

    def prompt(self, session_id: str, text: str) -> None:
        self.prompts.append((session_id, text))
        if self.prompt_errors:
            raise self.prompt_errors.pop(0)

    def reply_permission(self, session_id, request_id, decision) -> None:
        self.permission_replies.append((session_id, request_id, decision))
        if self.permission_errors:
            raise self.permission_errors.pop(0)


class RecordingAnswer:
    """core 那条按钮应答路径的替身：签名一致，只记录。"""

    def __init__(self) -> None:
        self.acks: list[tuple] = []

    def __call__(self, adapter, query_id: str, text: str) -> None:
        self.acks.append((adapter, query_id, text))


class RecordingSendText:
    """core 那条发信路径的替身：签名一致，只记录，不经过任何适配器。"""

    def __init__(self) -> None:
        self.outgoing: list[Outbound] = []
        self.fail_with: Exception | None = None
        self.returns_none = False
        self._handles = 0

    def __call__(
        self,
        conversation_id: str,
        text: str,
        *,
        kind: str = "text",
        adapter=None,
        session_id: str | None = None,
    ) -> MsgHandle | None:
        if self.fail_with is not None:
            raise self.fail_with
        self.outgoing.append(Outbound(
            conversation_id=conversation_id, text=text, kind=kind,
            session_id=session_id,
        ))
        self._handles += 1
        if self.returns_none:
            return None
        return MsgHandle(
            conversation_id=conversation_id,
            message_id="m%d" % self._handles,
            platform=PLATFORM,
        )

    @property
    def last(self) -> Outbound:
        return self.outgoing[-1]


class StandInAdapter(Adapter):
    """``adapter_for`` 返回的那个"已挂载适配器"。

    ⚠️ 它**必须是真的 :class:`Adapter` 子类**，不能是裸 ``object()``：
    :meth:`InboundGateway._dispatch_prompt` 会按 C1 的要求问适配器要渠道能力
    （见 :mod:`opencode_bridge.channel_profile`），裸对象会在那里抛
    ``AttributeError``，于是每个投递用例都会莫名走进"发送失败"分支。
    """

    name = "fake"
    label = "Fake Chat"
    max_message_length = 640

    def start(self) -> None:
        return None

    def send(self, out: Outbound) -> MsgHandle | None:
        return MsgHandle(
            conversation_id=out.conversation_id,
            message_id="m-stand-in",
            platform=self.name,
        )

    def edit(self, handle: MsgHandle, out: Outbound) -> bool:
        return False


class RecordingStreamConfirmed:
    """事件流那个"订阅已建立"的信号；记录它被等了多久、确认没有。"""

    def __init__(self, *, confirmed: bool = True) -> None:
        self.waits: list[float | None] = []
        self._confirmed = confirmed

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(timeout)
        return self._confirmed


class RecordingInbox:
    """只记账的假收件箱：够 ``InboundGateway`` 与 ``inbox_recovery`` 用。

    ⚠️ 它**不**实现重试预算算术 —— 那些由真收件箱与 ``tests/test_inbox.py``
    负责（见模块开头的分工说明）。它只实现 :mod:`inbox_recovery` 用到的那一件事：
    ``failed`` 行**默认不到期**（真收件箱会写一个未来的 ``not_before``），要重放
    得显式说它到期了。没有这条，``mark_failed`` 的那一行会在同一轮恢复里被立刻
    重放两次，测试就会断言出一个生产里不存在的行为。
    """

    def __init__(self) -> None:
        self.rows: dict[str, DeliveryState] = {}
        self.failures: dict[str, str] = {}
        self.recorded: list[QueuedPrompt] = []
        self.duplicate_of: str | None = None
        self.due_failures: set[str] = set()
        #: 每次状态写入的 ``(delivery_id, 写成了什么)``，按发生顺序。
        self.writes: list[tuple[str, str]] = []
        #: 每行被写了多少次 ``attempting``（真收件箱据此算重试预算）。
        self.attempts: dict[str, int] = {}

    def record(self, queued: QueuedPrompt) -> bool:
        if self.duplicate_of is not None and queued.delivery_id == self.duplicate_of:
            return False
        self.rows[queued.delivery_id] = DeliveryState.PENDING
        self.attempts[queued.delivery_id] = 0
        self.recorded.append(queued)
        self.writes.append((queued.delivery_id, "recorded"))
        return True

    def mark_attempting(self, delivery_id: str) -> None:
        self.rows[delivery_id] = DeliveryState.ATTEMPTING
        self.attempts[delivery_id] = self.attempts.get(delivery_id, 0) + 1
        self.writes.append((delivery_id, "attempting"))

    def mark_delivered(self, delivery_id: str) -> None:
        self.rows[delivery_id] = DeliveryState.DELIVERED
        self.writes.append((delivery_id, "delivered"))

    def mark_failed(self, delivery_id: str, reason: str) -> None:
        self.rows[delivery_id] = DeliveryState.FAILED
        self.failures[delivery_id] = reason
        self.due_failures.discard(delivery_id)   # 退避期限还没到
        self.writes.append((delivery_id, "failed"))

    def make_failure_due(self, delivery_id: str) -> None:
        """把一行 ``failed`` 的退避期限调到已过（``not_before`` 已到）。"""
        self.due_failures.add(delivery_id)

    def _prompts_now_in(self, state: DeliveryState) -> list[QueuedPrompt]:
        return [
            queued for queued in self.recorded
            if self.rows.get(queued.delivery_id) == state
        ]

    def pending_prompts(self) -> list[QueuedPrompt]:
        return self._prompts_now_in(DeliveryState.PENDING)

    def uncertain_prompts(self) -> list[QueuedPrompt]:
        return self._prompts_now_in(DeliveryState.ATTEMPTING)

    def due_failed_prompts(self, now: float) -> list[QueuedPrompt]:
        return [
            queued for queued in self._prompts_now_in(DeliveryState.FAILED)
            if queued.delivery_id in self.due_failures
        ]

    def abandoned_prompts(self) -> list[QueuedPrompt]:
        return []


def message(text: str, *, conversation_id: str = CONVERSATION,
            message_id: str | None = "1", kind: str = "text",
            callback_query_id: str | None = None) -> Inbound:
    return Inbound(
        conversation_id=conversation_id,
        text=text,
        kind=kind,
        platform=PLATFORM,
        message_id=message_id,
        callback_query_id=callback_query_id,
    )


def user_text_of(prompt: str) -> str:
    """``prompt`` 里的**用户正文** —— 剥掉 C1 拼在前面的渠道说明。

    本文件断言的是**协作契约**（写前落盘、去重、排队、409 退回、恢复不写账），
    不是渠道说明本身；说明由 ``tests/test_channel_profile.py`` 单独断言。
    所以这里只取分隔标记之后的那一段，而那一段仍然必须**逐字节**等于用户敲的字。
    """
    return prompt.split(_HINT_SEPARATOR + "\n", 1)[-1]


def queued(delivery_id: str, text: str,
           conversation_id: str = CONVERSATION) -> QueuedPrompt:
    return QueuedPrompt(
        delivery_id=delivery_id,
        conversation_id=conversation_id,
        platform=PLATFORM,
        message_id=delivery_id.rsplit(":", 1)[-1],
        text=text,
    )


# ----------------------------------------------------------------------
# 基类：造一套 InboundGateway（**没有** BridgeCore）
# ----------------------------------------------------------------------
class InboundGatewayTestCase(unittest.TestCase):
    """每个用例一套全新的十一个协作者。"""

    def setUp(self) -> None:
        os.makedirs(_REPOSITORY_TEMP, exist_ok=True)
        self.tempdir = tempfile.TemporaryDirectory(dir=_REPOSITORY_TEMP)
        self.addCleanup(self.tempdir.cleanup)
        self.client = RecordingClient()
        self.inbox = RecordingInbox()
        self.lock = threading.RLock()
        self.turns: dict[str, Turn] = {}
        self.stream_confirmed = RecordingStreamConfirmed()
        self.send_text = RecordingSendText()
        self.answer = RecordingAnswer()
        self.gateway = self.build_gateway()
        #: ``_adapter_for`` 返回什么（``None`` = 没有挂适配器）。
        #: ⚠️ 默认值是**真的适配器**而不是裸 ``object()`` —— C1 让投递路径去问
        #: 适配器要渠道能力，裸对象会在那里炸（理由见 :class:`StandInAdapter`）。
        self.adapter_result: object | None = StandInAdapter({}, hooks=None)  # type: ignore[arg-type]
        self.adapter_for_error: Exception | None = None
        self.remember_platform_error: Exception | None = None
        self.adapter_for_calls: list[str] = []
        self.remembered_platforms: list[tuple[str, str]] = []
        self.commands: list[tuple[str, object, str]] = []
        self.ensure_session_calls: list[tuple[str, str]] = []
        self.ensure_session_errors: list[Exception] = []
        self.session_id = SESSION_ID

    def build_gateway(self, **overrides) -> InboundGateway:
        kwargs = {
            "client": self.client,
            "lock": self.lock,
            "turns": self.turns,
            "inbox": self.inbox,
            "stream_confirmed": self.stream_confirmed,
            "adapter_for": self._adapter_for,
            "answer_callback": self.answer,
            "ensure_session": self._ensure_session,
            "handle_command": self._handle_command,
            "remember_platform": self._remember_platform,
            "send_text": self.send_text,
        }
        kwargs.update(overrides)
        self.gateway = InboundGateway(**kwargs)
        return self.gateway

    # --- 四个可调用替身 -------------------------------------------------
    def _adapter_for(self, conversation_id: str):
        self.adapter_for_calls.append(conversation_id)
        if self.adapter_for_error is not None:
            raise self.adapter_for_error
        return self.adapter_result

    def _ensure_session(self, conversation_id: str, *, platform: str = "") -> str:
        self.ensure_session_calls.append((conversation_id, platform))
        if self.ensure_session_errors:
            raise self.ensure_session_errors.pop(0)
        return self.session_id

    def _handle_command(self, conversation_id, adapter, text) -> None:
        self.commands.append((conversation_id, adapter, text))

    def _remember_platform(self, conversation_id: str, platform: str) -> None:
        if self.remember_platform_error is not None:
            raise self.remember_platform_error
        self.remembered_platforms.append((conversation_id, platform))

    # --- 断言用的小帮手 -------------------------------------------------
    @property
    def prompt_bodies(self) -> list[tuple[str, str]]:
        """``(session_id, 用户正文)``，渠道说明已剥掉（理由见 :func:`user_text_of`）。"""
        return [
            (session_id, user_text_of(text))
            for session_id, text in self.client.prompts
        ]

    def texts_of(self) -> list[tuple[str, str]]:
        return [(out.conversation_id, out.text) for out in self.send_text.outgoing]

    def kinds_of(self) -> list[str]:
        return [out.kind for out in self.send_text.outgoing]

    def states_of(self) -> dict[str, DeliveryState]:
        return dict(self.inbox.rows)

    def deliver(self, text: str = "看一下 README",
                delivery_id: str = "d1") -> None:
        """直接入队一条，绕过 ``on_inbound`` 的去重与命令分支。"""
        self.gateway._enqueue(queued(delivery_id, text))


# ----------------------------------------------------------------------
# 1: 依赖面与共有状态（AGENTS.md §5.1 的"抽出去"能不能成立）
# ----------------------------------------------------------------------
class CollaboratorSurfaceTests(InboundGatewayTestCase):
    def test_the_constructor_takes_the_eleven_injected_dependencies(self):
        parameters = inspect.signature(InboundGateway.__init__).parameters
        self.assertEqual(
            [name for name in parameters if name != "self"],
            ["client", "lock", "turns", "inbox", "stream_confirmed",
             "adapter_for", "answer_callback", "ensure_session",
             "handle_command", "remember_platform", "send_text"],
        )
        for name, parameter in parameters.items():
            if name == "self":
                continue
            self.assertEqual(
                parameter.kind, inspect.Parameter.KEYWORD_ONLY,
                "%s 必须显式按名字注入" % name,
            )
            self.assertIs(
                parameter.default, inspect.Parameter.empty,
                "%s 不许有默认值 —— 少传一个就该在构造时炸掉" % name,
            )

    def test_no_bridge_core_is_reachable_from_the_gateway(self):
        """拿到了 core 就等于又耦合回大类和它的私有状态 —— 那这次拆分就白做了。"""
        for name, value in vars(self.gateway).items():
            self.assertNotIsInstance(value, BridgeCore, name)

    def test_the_lock_and_the_turn_table_are_the_injected_objects(self):
        """注入的是**同一个对象**，不是复制一份：否则互斥关系和 turn 身份都会变。"""
        self.assertIs(self.gateway._lock, self.lock)
        self.assertIs(self.gateway._turns, self.turns)

    def test_the_stream_confirmed_signal_is_the_injected_one(self):
        self.assertIs(self.gateway._stream_confirmed, self.stream_confirmed)

    def test_the_queue_state_is_the_gateway_own(self):
        """``_queues`` / ``_draining`` 只有这一块写，所以它们**不注入**、自己建。"""
        first = self.gateway
        other = self.build_gateway(inbox=None)

        self.assertIsNot(first._queues, other._queues)
        self.assertIsNot(first._draining, other._draining)
        self.assertEqual(first._queues, {})
        self.assertEqual(first._draining, set())


# ----------------------------------------------------------------------
# 2: on_inbound —— 命令不落盘、去重不投递
# ----------------------------------------------------------------------
class OnInboundTests(InboundGatewayTestCase):
    def test_the_platform_is_remembered_before_the_callback_early_return(self):
        """按钮回调那条路本身会 return；先记映射，它才不用靠前缀去猜适配器。"""
        self.gateway.on_inbound(
            message("setup:telegram", kind="callback", callback_query_id="Q1")
        )

        self.assertEqual(self.remembered_platforms, [(CONVERSATION, PLATFORM)])
        self.assertEqual(self.send_text.outgoing, [])
        self.assertEqual(self.inbox.recorded, [])

    def test_a_prompt_is_written_ahead_before_it_is_queued(self):
        self.gateway.on_inbound(message("把 README 翻译成英文"))

        self.assertEqual([queued.text for queued in self.inbox.recorded],
                         ["把 README 翻译成英文"])
        self.assertEqual(self.prompt_bodies, [(SESSION_ID, "把 README 翻译成英文")])
        self.assertEqual(self.states_of(),
                         {"fake:chat:55:1": DeliveryState.DELIVERED})

    def test_a_command_is_never_written_ahead(self):
        """命令不经过 prompt()，永远不会 mark_delivered —— 落盘会留下一行永远
        停在 pending 的记录，于是每次启动都重放一遍。"""
        for text in ("/help", "/new", "/status"):
            with self.subTest(text=text):
                self.gateway.on_inbound(message(text, message_id=text))

        self.assertEqual([text for _, _, text in self.commands],
                         ["/help", "/new", "/status"])
        self.assertEqual(self.inbox.recorded, [])
        self.assertEqual(self.client.prompts, [])

    def test_a_duplicate_delivery_is_dropped_and_says_so(self):
        self.inbox.duplicate_of = "fake:chat:55:9"
        with self.assertLogs("opencode_bridge.inbound_gateway", level="INFO"):
            self.gateway.on_inbound(message("跑一下测试", message_id="9"))

        self.assertEqual(self.client.prompts, [])
        self.assertEqual(self.inbox.rows, {})
        self.assertEqual(self.gateway._queues, {})

    def test_a_message_without_an_adapter_is_dropped_with_a_warning(self):
        self.adapter_result = None

        with self.assertLogs("opencode_bridge.inbound_gateway", level="WARNING"):
            self.gateway.on_inbound(message("没人接"))

        self.assertEqual(self.inbox.recorded, [])
        self.assertEqual(self.client.prompts, [])

    def test_blank_text_and_a_blank_conversation_id_are_dropped(self):
        self.gateway.on_inbound(message("", message_id="1"))
        self.gateway.on_inbound(message("   ", message_id="2"))
        self.gateway.on_inbound(message("有正文", conversation_id="", message_id="3"))

        self.assertEqual(self.inbox.recorded, [])
        self.assertEqual(self.client.prompts, [])

    def test_a_failing_inbound_is_logged_and_never_escapes(self):
        """一条坏消息不许打断适配器的轮询循环。"""
        self.remember_platform_error = RuntimeError("platform lookup blew up")

        with self.assertLogs("opencode_bridge.inbound_gateway", level="ERROR"):
            self.gateway.on_inbound(message("坏消息"))

        self.assertEqual(self.inbox.recorded, [])
        self.assertEqual(self.client.prompts, [])


# ----------------------------------------------------------------------
# 3: on_callback —— setup: 与 perm: 两类按钮
# ----------------------------------------------------------------------
class OnCallbackTests(InboundGatewayTestCase):
    def test_setup_button_sends_the_frozen_guide_and_acks(self):
        self.gateway.on_callback(CONVERSATION, "setup:telegram", "Q1")

        self.assertEqual(self.texts_of(), [(CONVERSATION, _setup_guide("telegram"))])
        self.assertEqual(self.answer.acks,
                         [(self.adapter_result, "Q1", "已打开接入引导")])

    def test_an_unknown_platform_acks_without_sending_anything(self):
        self.gateway.on_callback(CONVERSATION, "setup:nope", "Q3")

        self.assertEqual(self.send_text.outgoing, [])
        self.assertEqual(self.answer.acks, [(self.adapter_result, "Q3", "未知平台")])

    def test_each_accepted_permission_decision_is_forwarded(self):
        for decision in ("once", "always", "reject"):
            with self.subTest(decision=decision):
                self.client.permission_replies.clear()
                self.answer.acks.clear()
                self.gateway.on_callback(
                    CONVERSATION, "perm:ses_x:per_9:%s" % decision, "Q4"
                )

                self.assertEqual(self.client.permission_replies,
                                 [("ses_x", "per_9", decision)])
                self.assertEqual(self.answer.acks,
                                 [(self.adapter_result, "Q4", "已处理")])

    def test_an_unsupported_payload_acks_failure_and_never_touches_the_client(self):
        for payload in ("perm:ses_x:per_9", "perm:ses_x:per_9:bogus",
                        "garbage", ""):
            with self.subTest(payload=payload):
                self.client.permission_replies.clear()
                self.answer.acks.clear()
                with self.assertLogs("opencode_bridge.inbound_gateway",
                                     level="WARNING"):
                    self.gateway.on_callback(CONVERSATION, payload, "Q9")

                self.assertEqual(self.client.permission_replies, [])
                self.assertEqual(self.answer.acks,
                                 [(self.adapter_result, "Q9", "失败")])

    def test_a_failing_reply_acks_failure_and_explains_why(self):
        self.client.permission_errors.append(OpenCodeError("nope", status=500))

        self.gateway.on_callback(CONVERSATION, "perm:ses_x:per_9:once", "Q1")

        self.assertEqual(self.answer.acks, [(self.adapter_result, "Q1", "失败")])
        self.assertEqual(self.kinds_of(), ["error"])
        self.assertIn("权限回复失败", self.send_text.last.text)

    def test_an_exception_still_acks_so_the_button_is_not_left_spinning(self):
        self.adapter_for_error = RuntimeError("adapter lookup blew up")

        with self.assertLogs("opencode_bridge.inbound_gateway", level="ERROR"):
            self.gateway.on_callback(CONVERSATION, "setup:telegram", "Q1")

        self.assertEqual(self.answer.acks, [(None, "Q1", "失败")])


# ----------------------------------------------------------------------
# 4: 队列 —— 同一会话串行、409 退回队首
# ----------------------------------------------------------------------
class QueueTests(InboundGatewayTestCase):
    def queued_texts(self) -> list[str]:
        return [queued.text for queued in self.gateway._queues.get(CONVERSATION, [])]

    def test_the_first_message_is_delivered_right_away(self):
        self.deliver()

        self.assertEqual(self.prompt_bodies, [(SESSION_ID, "看一下 README")])
        self.assertEqual(self.queued_texts(), [])
        self.assertEqual(self.gateway._draining, set())

    def test_a_message_queued_behind_a_drainer_waits_for_the_flush(self):
        # 另一个线程的排空者正在处理这条会话，所以新到的消息只能排队。
        self.gateway._draining.add(CONVERSATION)
        self.deliver("第二条")

        self.assertEqual(self.client.prompts, [])
        self.assertEqual(self.queued_texts(), ["第二条"])

        # 那个排空者排空了自己的队列、退出（``_drain`` 收尾会清掉这个标记）。
        self.gateway._draining.clear()
        self.gateway.flush_queue(CONVERSATION)

        self.assertEqual(self.prompt_bodies, [(SESSION_ID, "第二条")])

    def test_flushing_an_empty_queue_does_nothing(self):
        self.gateway.flush_queue(CONVERSATION)

        self.assertEqual(self.client.prompts, [])
        self.assertEqual(self.gateway._draining, set())

    def test_flushing_while_another_drainer_owns_the_conversation_does_nothing(self):
        self.gateway._draining.add(CONVERSATION)
        self.deliver("排队中")

        self.gateway.flush_queue(CONVERSATION)

        self.assertEqual(self.client.prompts, [])
        self.assertEqual(self.queued_texts(), ["排队中"])

    def test_a_busy_session_is_not_a_failure_it_goes_back_to_the_front(self):
        self.client.prompt_errors.append(OpenCodeError("busy", status=409))
        self.deliver("第一条", delivery_id="d1")

        self.assertEqual(self.prompt_bodies, [(SESSION_ID, "第一条")])
        self.assertEqual([queued.text for queued in
                          self.gateway._queues[CONVERSATION]], ["第一条"])
        self.assertEqual(self.gateway._draining, set())
        # 409 没有花掉重试预算：收件箱停在 attempting，而不是 failed。
        self.assertEqual(self.states_of(), {"d1": DeliveryState.ATTEMPTING})
        self.assertEqual(self.inbox.failures, {})

        self.gateway.flush_queue(CONVERSATION)

        self.assertEqual(self.states_of(), {"d1": DeliveryState.DELIVERED})

    def test_a_busy_message_keeps_its_place_at_the_front_of_the_queue(self):
        """409 退回**队首**：先到的那条还在等，重试不能被后来的挤到后面。"""
        self.gateway._draining.add(CONVERSATION)
        self.deliver("第一条", delivery_id="d1")
        self.deliver("第二条", delivery_id="d2")
        self.gateway._draining.clear()
        self.client.prompt_errors.append(OpenCodeError("busy", status=409))

        self.gateway.flush_queue(CONVERSATION)          # 第一条 409，退回队首

        self.assertEqual([text for _, text in self.prompt_bodies], ["第一条"])
        self.assertEqual(self.queued_texts(), ["第一条", "第二条"])

        self.gateway.flush_queue(CONVERSATION)          # 这次轮到它成功

        self.assertEqual([text for _, text in self.prompt_bodies],
                         ["第一条", "第一条", "第二条"])

    def test_a_failed_drain_is_logged_and_releases_the_conversation(self):
        self.gateway._draining.add(CONVERSATION)
        self.gateway._queues[CONVERSATION] = [queued("d1", "坏消息")]

        with mock.patch.object(self.gateway, "_dispatch_prompt",
                               side_effect=RuntimeError("boom")):
            with self.assertLogs("opencode_bridge.inbound_gateway", level="ERROR"):
                self.gateway._drain(CONVERSATION)

        self.assertEqual(self.gateway._draining, set())

    def test_two_conversations_are_drained_independently(self):
        self.deliver("甲", delivery_id="a1")
        self.gateway._enqueue(queued("b1", "乙", conversation_id=OTHER_CONVERSATION))

        self.assertEqual([text for _, text in self.prompt_bodies], ["甲", "乙"])
        self.assertEqual(self.gateway._draining, set())


# ----------------------------------------------------------------------
# 5: _dispatch_prompt —— 四次收件箱记账的位置就是它的全部意义
# ----------------------------------------------------------------------
class DispatchPromptTests(InboundGatewayTestCase):
    def test_the_attempting_write_sits_immediately_before_the_prompt_call(self):
        """早写一个字，``create_session`` 期间的崩溃就会被误判成"结果不可知"。"""
        seen: list[tuple[str, dict]] = []
        original = self.client.prompt

        def watching_prompt(session_id: str, text: str) -> None:
            seen.append(("at prompt", dict(self.inbox.rows)))
            original(session_id, text)
            seen.append(("after prompt", dict(self.inbox.rows)))

        self.client.prompt = watching_prompt
        self.gateway._enqueue(queued("d1", "看一下 README"))

        self.assertEqual(seen[0], ("at prompt", {"d1": DeliveryState.ATTEMPTING}))
        # prompt() 刚返回时**仍然**是 attempting —— 落 delivered 只能发生在它之后。
        self.assertEqual(seen[1], ("after prompt", {"d1": DeliveryState.ATTEMPTING}))
        self.assertEqual(self.states_of(), {"d1": DeliveryState.DELIVERED})

    def test_a_session_is_created_with_the_platform_the_message_came_from(self):
        self.deliver()

        self.assertEqual(self.ensure_session_calls, [(CONVERSATION, PLATFORM)])

    def test_a_create_session_failure_marks_the_row_failed_and_explains(self):
        self.ensure_session_errors.append(OpenCodeError("no capacity", status=500))
        self.deliver()

        self.assertEqual(self.kinds_of(), ["error"])
        self.assertIn("创建会话失败", self.send_text.last.text)
        self.assertEqual(self.states_of(), {"d1": DeliveryState.FAILED})
        self.assertIn("create_session failed", self.inbox.failures["d1"])

    def test_a_prompt_failure_marks_the_row_failed_and_points_at_new(self):
        self.client.prompt_errors.append(OpenCodeError("server said no", status=500))
        self.deliver()

        self.assertIn("发送失败", self.send_text.last.text)
        self.assertIn("/new", self.send_text.last.text)
        self.assertEqual(self.states_of(), {"d1": DeliveryState.FAILED})

    def test_success_creates_the_turn_and_sends_one_progress_message(self):
        self.deliver()

        self.assertEqual(list(self.turns), [SESSION_ID])
        self.assertEqual(self.turns[SESSION_ID].conversation_id, CONVERSATION)
        self.assertEqual(self.texts_of(), [(CONVERSATION, PROGRESS_TEXT)])
        self.assertEqual(self.kinds_of(), ["progress"])
        self.assertIsNotNone(self.turns[SESSION_ID].progress_handle)

    def test_a_second_message_reuses_the_turn_and_sends_no_second_progress(self):
        self.deliver("第一条", delivery_id="d1")
        self.gateway.flush_queue(CONVERSATION)
        self.deliver("第二条", delivery_id="d2")

        self.assertEqual(len(self.turns), 1)
        self.assertEqual(self.kinds_of(), ["progress"])

    def test_the_recovery_path_writes_no_inbox_state_at_all(self):
        """恢复时 ``inbox_recovery`` 才是账本 —— 这里再写一次会烧掉两级预算。"""
        self.gateway._dispatch_prompt(queued("d1", "重放"), recording_delivery=False)

        self.assertEqual(self.prompt_bodies, [(SESSION_ID, "重放")])
        self.assertEqual(self.inbox.rows, {})
        self.assertEqual(self.inbox.failures, {})


# ----------------------------------------------------------------------
# 6: recover_inbox —— 等事件流确认、只重放该重放的
# ----------------------------------------------------------------------
class RecoverInboxTests(InboundGatewayTestCase):
    def test_without_an_inbox_it_says_so_and_does_nothing_else(self):
        gateway = self.build_gateway(inbox=None)

        with self.assertLogs("opencode_bridge.inbound_gateway", level="INFO") as cap:
            gateway.recover_inbox()

        self.assertIn("write-ahead inbox disabled", "\n".join(cap.output))
        self.assertEqual(self.stream_confirmed.waits, [])
        self.assertEqual(self.client.prompts, [])

    def test_recovery_waits_for_the_stream_and_still_replays_after_a_timeout(self):
        self.inbox.record(queued("d1", "上次没发出去的"))
        self.stream_confirmed._confirmed = False

        with self.assertLogs("opencode_bridge.inbound_gateway", level="WARNING"):
            self.gateway.recover_inbox()

        self.assertEqual(len(self.stream_confirmed.waits), 1)
        self.assertGreater(self.stream_confirmed.waits[0], 0.0)
        self.assertEqual(self.prompt_bodies, [(SESSION_ID, "上次没发出去的")])

    def test_a_pending_row_is_replayed_and_marked_delivered(self):
        self.inbox.record(queued("d1", "上次没发出去的"))

        self.gateway.recover_inbox()

        self.assertEqual(self.prompt_bodies, [(SESSION_ID, "上次没发出去的")])
        self.assertEqual(self.states_of(), {"d1": DeliveryState.DELIVERED})

    def test_an_uncertain_row_is_alerted_and_never_replayed(self):
        self.inbox.record(queued("d1", "上次发到一半"))
        self.inbox.mark_attempting("d1")

        self.gateway.recover_inbox()

        self.assertEqual(self.client.prompts, [])
        self.assertEqual(len(self.send_text.outgoing), 1)
        self.assertEqual(self.send_text.last.conversation_id, CONVERSATION)
        self.assertIn("状态未知", self.send_text.last.text)
        self.assertEqual(self.states_of(), {"d1": DeliveryState.ATTEMPTING})

    def test_a_replay_that_fails_is_recorded_by_the_recovery_layer(self):
        self.inbox.record(queued("d1", "重放会失败"))
        self.ensure_session_errors.append(OpenCodeError("still busy", status=500))

        self.gateway.recover_inbox()

        self.assertEqual(self.states_of(), {"d1": DeliveryState.FAILED})
        self.assertIn("returned 'error'", self.inbox.failures["d1"])
        # 一次失败只该花掉**一级**预算：写两次 failed 会让重试阶梯直接见底。
        self.assertEqual(
            [write for write in self.inbox.writes if write[1] == "failed"],
            [("d1", "failed")],
        )

    def test_the_recovery_path_writes_no_inbox_state_of_its_own(self):
        """恢复时 :mod:`inbox_recovery` 是唯一的账本。

        ``_dispatch_prompt`` 若自己再写一遍，一次失败就烧两级预算，一次成功也把
        ``attempting`` 数了两遍 —— 而重试预算正是按这个数字算的。
        """
        self.inbox.record(queued("d1", "重放"))

        self.gateway.recover_inbox()

        self.assertEqual(self.inbox.writes, [
            ("d1", "recorded"),
            ("d1", "attempting"),
            ("d1", "delivered"),
        ])
        self.assertEqual(self.inbox.attempts, {"d1": 1})

    def test_a_failed_row_is_replayed_only_once_its_backoff_is_up(self):
        self.inbox.record(queued("d1", "上次明确失败"))
        self.inbox.mark_failed("d1", "previous boot")

        self.gateway.recover_inbox()          # 退避期限还没到

        self.assertEqual(self.client.prompts, [])
        self.assertEqual(self.states_of(), {"d1": DeliveryState.FAILED})

        self.inbox.make_failure_due("d1")
        self.gateway.recover_inbox()

        self.assertEqual(self.prompt_bodies, [(SESSION_ID, "上次明确失败")])
        self.assertEqual(self.states_of(), {"d1": DeliveryState.DELIVERED})

    def test_an_alert_that_cannot_be_delivered_is_logged_not_swallowed(self):
        self.inbox.record(queued("d1", "上次发到一半"))
        self.inbox.mark_attempting("d1")
        self.send_text.returns_none = True

        with self.assertLogs("opencode_bridge.inbound_gateway",
                             level="WARNING") as captured:
            self.gateway.recover_inbox()

        self.assertIn("could not deliver the alert", "\n".join(captured.output))
        self.assertIn(CONVERSATION, "\n".join(captured.output))

    def test_recovery_never_raises(self):
        self.inbox.record(queued("d1", "x"))
        self.client.prompt_errors.append(RuntimeError("socket died"))

        self.gateway.recover_inbox()      # 不抛就是这一条的全部断言

        self.assertEqual(self.states_of(), {"d1": DeliveryState.FAILED})


# ----------------------------------------------------------------------
# 7: 收件箱那一段真的落盘（接线，不是状态语义）
# ----------------------------------------------------------------------
class RealInboxWiringTests(unittest.TestCase):
    """``_record_inbound`` 用的是**真**收件箱：写前这一笔必须真的落盘。"""

    def setUp(self) -> None:
        os.makedirs(_REPOSITORY_TEMP, exist_ok=True)
        self.tempdir = tempfile.TemporaryDirectory(dir=_REPOSITORY_TEMP)
        self.addCleanup(self.tempdir.cleanup)
        self.database_path = os.path.join(self.tempdir.name, "inbox.db")
        self.inbox = InboundInbox(self.database_path)
        self.addCleanup(self.inbox.close)

    def read_rows(self) -> list[tuple[str, str]]:
        """另开一条只读连接读盘上的原始行（同 ``tests/test_inbox.py`` 的做法）。"""
        connection = sqlite3.connect(self.database_path)
        try:
            return [
                (row[0], row[1])
                for row in connection.execute(
                    "SELECT delivery_id, state FROM inbox ORDER BY delivery_id ASC"
                ).fetchall()
            ]
        finally:
            connection.close()

    def test_a_recorded_row_is_pending_on_disk(self):
        built = _record_inbound(self.inbox, message("写前落盘", message_id="7"),
                                "写前落盘")

        self.assertIsNotNone(built)
        self.assertEqual(self.read_rows(), [("fake:chat:55:7", "pending")])

    def test_recording_the_same_delivery_twice_yields_the_row_once(self):
        first = _record_inbound(self.inbox, message("重复", message_id="8"), "重复")
        second = _record_inbound(self.inbox, message("重复", message_id="8"), "重复")

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(self.read_rows(), [("fake:chat:55:8", "pending")])

    def test_without_an_inbox_the_row_is_built_but_nothing_is_written(self):
        built = _record_inbound(None, message("没开收件箱"), "没开收件箱")

        self.assertIsNotNone(built)
        self.assertEqual(self.read_rows(), [])

    def test_the_delivery_id_falls_back_to_a_content_hash_without_a_message_id(self):
        built = _queued_prompt_for(message("没有 message_id", message_id=None),
                                   "没有 message_id")
        same_again = _queued_prompt_for(
            message("没有 message_id", message_id=None), "没有 message_id")

        self.assertEqual(built.delivery_id, same_again.delivery_id)
        self.assertNotIn(":", built.delivery_id)


if __name__ == "__main__":
    unittest.main()

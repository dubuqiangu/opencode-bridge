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

import ast
import hashlib
import inspect
import io
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
    DEFAULT_MERGE_CONTINUE_TIMEOUT_SECONDS,
    PROGRESS_TEXT,
    InboundGateway,
    _positive_float,
    _positive_int,
    _queued_prompt_for,
    _record_inbound,
)
from opencode_bridge.channel_profile import with_channel_hint
from opencode_bridge.inbound_gateway import HELD_NOT_DELIVERED_NOTICE
from opencode_bridge.inbound_merge import BUFFERED_NOTICE, HELD_EXPIRED_NOTICE
from opencode_bridge.inbox import DeliveryState, InboundInbox, QueuedPrompt
from opencode_bridge.opencode_client import OpenCodeError
from opencode_bridge.permission_ledger import REPEATED_ANSWER_ACK, PermissionLedger

CONVERSATION = "chat:55"
OTHER_CONVERSATION = "chat:66"
PLATFORM = "fake"
SESSION_ID = "ses_fake0001"
#: 本模块断言「配 0 要点名该键」时要看的那条 logger。逐字对应生产侧
#: ``inbound_gateway.py`` 的 ``logger = logging.getLogger(...)``。
INBOUND_GATEWAY_LOGGER = "opencode_bridge.inbound_gateway"

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
    #: 本替身**能**改写已发消息（与 :meth:`edit` 的返回值一致），所以本文件那些
    #: 断言"这一轮建/复用了一条进度占位消息"的用例仍然测的是**占位消息那条路**。
    #: ⚠️ 出站闸门读的是**这个声明**而不是 ``edit()`` 的返回值 ——
    #: 见 :class:`opencode_bridge.adapters.base.Adapter` 的 ``supports_message_edit``。
    supports_message_edit = True

    def start(self) -> None:
        return None

    def send(self, out: Outbound) -> MsgHandle | None:
        return MsgHandle(
            conversation_id=out.conversation_id,
            message_id="m-stand-in",
            platform=self.name,
        )

    def edit(self, handle: MsgHandle, out: Outbound) -> bool:
        return True


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

    def mark_pending(self, delivery_id: str) -> None:
        """409：服务端拒收 —— 退回可重放，**不**算一次尝试（``attempts`` 不动）。"""
        self.rows[delivery_id] = DeliveryState.PENDING
        self.writes.append((delivery_id, "pending"))

    def mark_outcome_unknown(self, delivery_id: str, reason: str) -> None:
        """传输层失败、拿不到 status ⇒ 结果不可知。**不是**失败，也不烧预算。"""
        self.rows[delivery_id] = DeliveryState.OUTCOME_UNKNOWN
        self.failures[delivery_id] = reason
        self.writes.append((delivery_id, "outcome_unknown"))

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

    def _prompts_now_in(self, *states: DeliveryState) -> list[QueuedPrompt]:
        return [
            queued for queued in self.recorded
            if self.rows.get(queued.delivery_id) in states
        ]

    def pending_prompts(self) -> list[QueuedPrompt]:
        return self._prompts_now_in(DeliveryState.PENDING)

    def uncertain_prompts(self) -> list[QueuedPrompt]:
        # ⚠️ 与真收件箱一致：**两档**都算"结果不可知"，恢复层只告警、不重放它们。
        return self._prompts_now_in(DeliveryState.ATTEMPTING, DeliveryState.OUTCOME_UNKNOWN)

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
    """每个用例一套全新的十三个协作者。"""

    #: C3 的两个配置项在这里要显式给 0 / 极短，否则那些用例会去抢真实的计时器
    #: 与真实的门槛值（测试不该依赖默认值）。需要它们的用例自己覆盖。
    bridge_config: dict = {}

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
        #: 真对象：本文件要断言的正是按钮那条路上有没有接上账本。
        self.permission_ledger = PermissionLedger()
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
        #: conversation_id -> session id 的**覆盖**表。默认空，于是绝大多数用例
        #: 仍然一律拿到 :data:`SESSION_ID`；只有"两个会话不许互相合并"那类用例
        #: 需要两个不同 session 才能看出串了。
        self.session_id_by_conversation: dict[str, str] = {}
        #: 保险丝到点的信号（见 :meth:`build_gateway` 里的包装）。
        self.hold_expired = threading.Event()

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
            "permission_ledger": self.permission_ledger,
            "bridge_config": self.bridge_config,
        }
        kwargs.update(overrides)
        self.gateway = InboundGateway(**kwargs)
        self._watch_the_hold_fuse()
        return self.gateway

    def _watch_the_hold_fuse(self) -> None:
        """把保险丝的回调包一层，好让用例能等它**而不必等时长**。

        生产代码里这个回调是 :meth:`InboundGateway._deliver_expired_hold`，由本类
        自己构造合并器时绑上；这里只在它外面套一个置信号的壳，投递行为一个字没改。
        """
        merger = self.gateway._merger
        deliver_then_signal = merger._on_hold_expired

        def signal_then_deliver(conversation_id: str, held_text: str) -> None:
            self.hold_expired.set()
            deliver_then_signal(conversation_id, held_text)

        merger._on_hold_expired = signal_then_deliver

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
        return self.session_id_by_conversation.get(conversation_id, self.session_id)

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
    def test_the_constructor_takes_the_thirteen_injected_dependencies(self):
        parameters = inspect.signature(InboundGateway.__init__).parameters
        self.assertEqual(
            [name for name in parameters if name != "self"],
            ["client", "lock", "turns", "inbox", "stream_confirmed",
             "adapter_for", "answer_callback", "ensure_session",
             "handle_command", "remember_platform", "send_text",
             "permission_ledger", "bridge_config"],
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


# ----------------------------------------------------------------------
# 3c: 入站正文的缩进 —— 逐字节保留（去空白 ≠ 去缩进）
# ----------------------------------------------------------------------
# 这一类盯的是**agent 实际收到的正文**（``prompt_bodies`` 剥掉渠道说明后的那段），
# 不是被清洗前的入参。⚠️ 每一条的第一行都**带缩进** —— 一个首行无缩进的样本会让
# 断言恒真，那正是本项目已经踩过一次（先写了一个首行没缩进的样本，"验证"通过，
# 然后样本被扔掉、断言什么也没证明）。
class InboundIndentationTests(InboundGatewayTestCase):
    """把"空白是噪声、缩进有意义"这条不变量钉在真实路径上。"""

    #: 只调保险丝，不动长输入回执的门槛（这一类与那条无关）。
    bridge_config = {"merge_continue_timeout_seconds": 0.05}

    def delivered(self) -> list[str]:
        """每条 prompt 里的**用户正文**（渠道说明已剥掉）。"""
        return [user_text_of(text) for _, text in self.client.prompts]

    # --- 决定性的一条 -----------------------------------------------------
    def test_a_pasted_python_block_reaches_the_agent_byte_for_byte(self):
        pasted = "    def f():\n        return 1"

        self.gateway.on_inbound(message(pasted, message_id="m1"))

        self.assertEqual(self.delivered(), [pasted])

    def test_the_indentation_error_is_not_manufactured_any_more(self):
        """⚠️ 改之前：第一行 dedent、第二行不 dedent → ``def f():`` 后面跟着一个
        8 空格函数体 = ``IndentationError``。这条断言的是**不存在**这种形状。"""
        self.gateway.on_inbound(
            message("    def f():\n        return 1", message_id="m1")
        )

        first, second = self.delivered()[0].split("\n")
        self.assertTrue(first.startswith("    "), "首行缩进被吃掉了：%r" % first)
        self.assertGreater(len(second) - len(second.lstrip()), len(first) - len(
            first.lstrip()), "函数体必须比 def 更深")

    def test_a_nested_yaml_block_reaches_the_agent_byte_for_byte(self):
        pasted = "build:\n  steps:\n    - name: test\n      run: pytest"

        self.gateway.on_inbound(message(pasted, message_id="m1"))

        self.assertEqual(self.delivered(), [pasted])

    def test_a_deeply_nested_block_keeps_every_level(self):
        pasted = "  a:\n    b:\n      c:\n        - 1\n        - 2"

        self.gateway.on_inbound(message(pasted, message_id="m1"))

        self.assertEqual(self.delivered(), [pasted])

    # --- 开头的空行仍然要丢 -----------------------------------------------
    def test_leading_blank_lines_are_dropped_and_indentation_kept(self):
        self.gateway.on_inbound(
            message("\n\n    def g():\n        return 2\n\n", message_id="m1")
        )

        self.assertEqual(self.delivered(), ["    def g():\n        return 2"])

    def test_interior_blank_lines_are_kept(self):
        """内部空行是代码结构的一部分，不是排版噪声。"""
        pasted = "def a():\n    pass\n\n\ndef b():\n    pass"

        self.gateway.on_inbound(message(pasted, message_id="m1"))

        self.assertEqual(self.delivered(), [pasted])

    # --- 全空白 / 无空白 --------------------------------------------------
    def test_an_all_whitespace_message_is_dropped_not_turned_into_something(self):
        """全空白 → 空串 → 守卫丢掉。**不是**变成一串空格发给 agent。"""
        for blank in ("", "   ", "\n\n", "  \n\t\n  "):
            with self.subTest(blank=blank):
                self.client.prompts.clear()
                self.gateway.on_inbound(message(blank, message_id="m1"))

                self.assertEqual(self.client.prompts, [])
                self.assertEqual(self.inbox.recorded, [])

    def test_a_single_line_without_whitespace_is_untouched(self):
        self.gateway.on_inbound(message("hi", message_id="m1"))

        self.assertEqual(self.delivered(), ["hi"])

    def test_a_single_indented_line_keeps_its_indentation(self):
        self.gateway.on_inbound(message("    indented", message_id="m1"))

        self.assertEqual(self.delivered(), ["    indented"])

    # --- 尾巴 --------------------------------------------------------------
    def test_trailing_newlines_are_dropped(self):
        self.gateway.on_inbound(
            message("    x = 1\n\n\n", message_id="m1")
        )

        self.assertEqual(self.delivered(), ["    x = 1"])

    def test_trailing_spaces_on_the_last_line_are_dropped(self):
        self.gateway.on_inbound(message("    x = 1   ", message_id="m1"))

        self.assertEqual(self.delivered(), ["    x = 1"])

    def test_interior_trailing_spaces_are_not_rewritten(self):
        """⚠️ 只动**结尾**。行尾那圈空格在正文里，删它同样是改写用户写的代码。"""
        pasted = "def m():\n    return 1   \n\ndef n():\n    pass"

        self.gateway.on_inbound(message(pasted, message_id="m1"))

        self.assertEqual(self.delivered(), [pasted])

    # --- 收件箱里存的也必须是缩进完好的那份 --------------------------------
    def test_the_inbox_row_keeps_the_indentation_too(self):
        """⚠️ 去重键与重放读的都是收件箱那一行。它若存的是 dedent 过的版本，
        那么崩溃重放出来的 prompt 仍然是坏的 —— 而那条路没有真人看着。"""
        pasted = "    def f():\n        return 1"

        self.gateway.on_inbound(message(pasted, message_id="m1"))

        self.assertEqual([row.text for row in self.inbox.recorded], [pasted])

    # --- 与 C3 合并的交界 -------------------------------------------------
    def test_a_held_line_keeps_its_indentation_all_the_way_through(self):
        """⚠️ **C3 与这条的交界**：strip 在**合并之前**逐行跑，所以合并前被缓冲的
        那一行的缩进也会被吃掉 —— 不只第一条消息的第一行。"""
        self.gateway.on_inbound(message("    def f():..", message_id="m1"))
        self.gateway.on_inbound(message("        return 1", message_id="m2"))

        self.assertEqual(self.delivered(), ["    def f():\n        return 1"])

    def test_a_merged_burst_keeps_the_indentation_of_every_line(self):
        self.gateway.on_inbound(message("  a:..", message_id="m1"))
        self.gateway.on_inbound(message("    b:..", message_id="m2"))
        self.gateway.on_inbound(message("      c: 1", message_id="m3"))

        self.assertEqual(self.delivered(), ["  a:\n    b:\n      c: 1"])

    def test_the_expired_hold_also_keeps_the_indentation(self):
        """保险丝那条路**不**重新走清洗（它直接调 :meth:`_persist_and_enqueue`），
        所以缓冲里攒下的缩进原样送达。"""
        self.gateway.on_inbound(message("    def f():..", message_id="m1"))

        self.assertTrue(self.hold_expired.wait(timeout=5),
                        "保险丝没有把缓冲交出来")
        self.assertEqual(self.delivered(), ["    def f():"])

    # --- 命令不受影响 ------------------------------------------------------
    def test_a_command_with_a_leading_space_is_still_a_command(self):
        """⚠️ **刻意保留的旧行为**：修缩进时若连带把 lstrip 去掉，
        ``"  /help"`` 就会从一条命令变成一条发给 agent 的消息。命令那一支显式
        lstrip，所以这里与改动前逐字节一致。"""
        self.gateway.on_inbound(message("  /help", message_id="m1"))

        self.assertEqual(self.commands,
                         [(CONVERSATION, self.adapter_result, "/help")])

    def test_a_command_with_a_trailing_space_is_still_a_command(self):
        self.gateway.on_inbound(message("/help  ", message_id="m1"))

        self.assertEqual(self.commands,
                         [(CONVERSATION, self.adapter_result, "/help")])

    def test_a_command_surrounded_by_blank_lines_is_still_a_command(self):
        """旧代码是 ``.strip()``，所以前导空行也一样容得下。"""
        self.gateway.on_inbound(message("\n\n  /help\n", message_id="m1"))

        self.assertEqual(self.commands,
                         [(CONVERSATION, self.adapter_result, "/help")])

    def test_an_indented_line_starting_with_a_slash_is_still_routed_as_a_command(self):
        """⚠️ **已知残留，本次刻意不改**：``"    /usr/bin/env"`` 这种缩进的
        ``/`` 开头行仍然进命令表（于是回一句"未知命令"）。

        旧代码是 ``.strip()``，**行为完全一样** —— 所以这不是本次引入的回归，
        改它就属于"缩进之外的改动"了。真正的分界（缩进的 ``/`` 开头应当当正文）
        要动的是命令路由，那是另一次取舍。
        """
        self.gateway.on_inbound(message("    /usr/bin/env", message_id="m1"))

        self.assertEqual(self.commands,
                         [(CONVERSATION, self.adapter_result, "/usr/bin/env")])

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
        """三种决策各自**换一个请求 id** —— 一个请求只允许被回答一次（C4）。

        ⚠️ 原来这里三个 subTest 共用同一个 ``per_9``，也就是断言"同一个请求可以被
        连答三次"。那正是 C4 要治的：连点按钮会走两遍，第二次还能把 once 放宽成
        always。重复那条现在由 :meth:`test_a_repeated_button_press_is_refused` 单独
        断言，所以三种决策的**可接受集合**一条没少。
        """
        for decision in ("once", "always", "reject"):
            with self.subTest(decision=decision):
                self.client.permission_replies.clear()
                self.answer.acks.clear()
                self.gateway.on_callback(
                    CONVERSATION, "perm:ses_x:per_for_%s:%s" % (decision, decision),
                    "Q4",
                )

                self.assertEqual(
                    self.client.permission_replies,
                    [("ses_x", "per_for_%s" % decision, decision)],
                )
                self.assertEqual(self.answer.acks,
                                 [(self.adapter_result, "Q4", "已处理")])

    def test_a_repeated_button_press_is_refused_and_says_so(self):
        """连点 / 平台重投：第二次一个字节都不发，并且两处都告诉用户。

        按钮那条路上"沉默"特别糟 —— Telegram 的转圈圈停了，用户只会以为已经答过，
        而实际上第一次可能失败了他不知道。所以 ack 与正文都必须说话。
        """
        self.gateway.on_callback(CONVERSATION, "perm:ses_x:per_tap:once", "Q1")

        self.gateway.on_callback(CONVERSATION, "perm:ses_x:per_tap:once", "Q2")

        self.assertEqual(self.client.permission_replies,
                         [("ses_x", "per_tap", "once")])
        self.assertEqual(
            self.answer.acks,
            [(self.adapter_result, "Q1", "已处理"),
             (self.adapter_result, "Q2", REPEATED_ANSWER_ACK)],
        )
        self.assertIn("per_tap", self.send_text.last.text)
        self.assertIn("忽略", self.send_text.last.text)
        self.assertEqual(self.send_text.last.kind, "error")

    def test_a_second_button_press_cannot_widen_once_into_always(self):
        """"once 之后再按 always" 是按钮版的 ``/deny <id> always``：必须被否掉。"""
        self.gateway.on_callback(CONVERSATION, "perm:ses_x:per_widen:once", "Q1")
        self.gateway.on_callback(CONVERSATION, "perm:ses_x:per_widen:always", "Q2")

        self.assertEqual(self.client.permission_replies,
                         [("ses_x", "per_widen", "once")])

    def test_a_request_resolved_by_the_server_is_not_answerable_by_a_late_press(self):
        """``permission.replied`` 之后的那一次按压同样不算数（账本由事件流写）。"""
        self.permission_ledger.note_replied("ses_x", "per_late")

        self.gateway.on_callback(CONVERSATION, "perm:ses_x:per_late:always", "Q1")

        self.assertEqual(self.client.permission_replies, [])
        self.assertEqual(self.answer.acks,
                         [(self.adapter_result, "Q1", REPEATED_ANSWER_ACK)])

    def test_a_failed_reply_leaves_the_request_answerable(self):
        """⚠️ 一次 5xx **不能**把请求锁死成"已回复过"。

        那个请求在服务端还挂着，用户唯一能做的事就是再答一次；把它记成已答过就等于
        把那个请求永久卡死，且用户只会看到"已回复过"这种误导性的说法。
        """
        self.client.permission_errors.append(OpenCodeError("nope", status=500))
        self.gateway.on_callback(CONVERSATION, "perm:ses_x:per_retry:once", "Q1")

        self.gateway.on_callback(CONVERSATION, "perm:ses_x:per_retry:once", "Q2")

        # ⚠️ 替身是"先记账再抛"，所以**两次**尝试都在列表里 —— 这正是要断言的：
        # 第一次失败没有把请求锁死，第二次仍然到达了客户端。
        self.assertEqual(self.client.permission_replies,
                         [("ses_x", "per_retry", "once"),
                          ("ses_x", "per_retry", "once")])
        self.assertEqual(self.answer.acks,
                         [(self.adapter_result, "Q1", "失败"),
                          (self.adapter_result, "Q2", "已处理")])

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
# 3b: C3 —— 续行合并与长输入回执的**接线**（真 ``on_inbound``，不打桩）
# ----------------------------------------------------------------------
# ``tests/test_inbound_merge.py`` 锁的是合并器自己；这里锁的是"它有没有被接上"
# 以及"回执发到了哪里"。两处分开是因为合并器全绿而接线漏掉是可能的 ——
# 而漏掉的症状恰好是 C3 整个功能不存在。
class InboundMergeWiringTests(InboundGatewayTestCase):
    # 门槛给 1 字，于是任何非空消息都会触发回执；保险丝给 0.05 秒，
    # 那些要等它到点的用例等的是**回调**而不是时长。
    bridge_config = {
        "long_input_ack_chars": 1,
        "merge_continue_timeout_seconds": 0.05,
    }

    # --- 决定性的那条：一次并集 = 一次投递 ---------------------------------
    def test_a_marker_burst_becomes_exactly_one_prompt_in_order(self):
        self.gateway.on_inbound(message("帮我看下这个函数..", message_id="m1"))
        self.gateway.on_inbound(message("def f(x):..", message_id="m2"))
        self.gateway.on_inbound(message("return x +", message_id="m3"))

        self.assertEqual(self.client.prompts, [
            (SESSION_ID, with_channel_hint(
                "帮我看下这个函数\ndef f(x):\nreturn x +",
                self.adapter_result,
            )),
        ])

    def test_a_marker_burst_lands_in_the_inbox_as_one_receipt(self):
        self.gateway.on_inbound(message("甲..", message_id="m1"))
        self.gateway.on_inbound(message("乙..", message_id="m2"))

        self.assertEqual(self.client.prompts, [])
        self.assertEqual(self.inbox.recorded, [])

        self.gateway.on_inbound(message("丙", message_id="m3"))

        # 3 条并成 1 行 —— 收件箱里是**一**行，不是三行。
        self.assertEqual(len(self.inbox.recorded), 1)
        self.assertEqual(self.inbox.recorded[0].text, "甲\n乙\n丙")

    def test_no_marker_text_reaches_the_agent(self):
        """**无标记泄漏**：送进 agent 的正文里不许残留 ``..`` / ``!!``。"""
        self.gateway.on_inbound(message("第一段..", message_id="m1"))
        self.gateway.on_inbound(message("第二段!!", message_id="m2"))

        self.assertEqual(
            user_text_of(self.client.prompts[0][1]), "第一段\n第二段"
        )

    # --- 零延迟 -----------------------------------------------------------
    def test_an_ordinary_message_is_prompted_immediately_with_nothing_buffered(self):
        self.gateway.on_inbound(message("看一下 README", message_id="m1"))

        self.assertEqual(self.prompt_bodies, [(SESSION_ID, "看一下 README")])
        self.assertEqual(self.gateway._merger.held_conversation_ids(), ())

    def test_an_ordinary_message_starts_no_hold_fuse(self):
        """证明"额外延迟 = 0"：没有缓冲就没有计时器可等。"""
        self.gateway.on_inbound(message("看一下 README", message_id="m1"))

        self.assertEqual(self.gateway._merger._fuses, {})

    def test_two_ordinary_messages_stay_two_separate_prompts(self):
        self.gateway.on_inbound(message("第一句", message_id="m1"))
        self.gateway.on_inbound(message("第二句", message_id="m2"))

        self.assertEqual(self.prompt_bodies,
                         [(SESSION_ID, "第一句"), (SESSION_ID, "第二句")])

    # --- 回执 -------------------------------------------------------------
    def test_a_held_line_is_acknowledged_immediately(self):
        self.gateway.on_inbound(message("第一段..", message_id="m1"))

        self.assertEqual(self.client.prompts, [])
        self.assertIn("..", self.send_text.last.text)
        self.assertIn("!!", self.send_text.last.text)

    def test_the_held_acknowledgement_is_never_left_silent(self):
        """⚠️ 一句正好以 ``..`` 结尾的散文会被判成续行标记。没有这句回执，
        它就是静默消失 —— AGENTS.md §8 点名最糟的代价。"""
        self.gateway.on_inbound(message("等等..", message_id="m1"))

        self.assertEqual(self.client.prompts, [])
        self.assertEqual(self.send_text.outgoing[-1].text,
                         BUFFERED_NOTICE)

    def test_the_expired_hold_is_delivered_and_reported(self):
        self.gateway.on_inbound(message("敲完就走了..", message_id="m1"))

        self.assertTrue(self.hold_expired.wait(timeout=5),
                        "保险丝没有把缓冲交出来")
        self.assertEqual(self.prompt_bodies, [(SESSION_ID, "敲完就走了")])
        self.assertIn(
            "敲完就走了",
            "".join(out.text for out in self.send_text.outgoing),
        )

    def test_the_expired_hold_is_delivered_exactly_once(self):
        """竞态：保险丝到点的那一下不能和"下一行到了"各发一次。"""
        self.gateway.on_inbound(message("第一段..", message_id="m1"))
        self.gateway.on_inbound(message("第二段", message_id="m2"))

        threading.Event().wait(timeout=0.4)   # 让保险丝有机会（不该）再醒一次

        self.assertEqual(self.prompt_bodies,
                         [(SESSION_ID, "第一段\n第二段")])

    # --- 命令必须绕开 -----------------------------------------------------
    def test_a_command_never_waits_for_the_next_line(self):
        """⚠️ 命令**不进**合并窗口 —— 缓存它等于给 C4 刚堵上的权限路径
        重新开一条延迟通道（dsh 也在 ``gateway.ts:395-428`` 做了同一个排除）。"""
        self.gateway.on_inbound(message("/approve per_9", message_id="m1"))

        self.assertEqual(self.commands,
                         [(CONVERSATION, self.adapter_result, "/approve per_9")])
        self.assertEqual(self.gateway._merger.held_conversation_ids(), ())

    def test_a_command_after_a_held_line_does_not_swallow_the_held_line(self):
        """敲了 ``..`` 之后敲命令：命令立刻执行，而缓冲**不动**。

        刻意不为它加"命令顺手把缓冲冲掉"的行为 —— 那样一条命令会静默改掉
        agent 接下来看到的上下文。缓冲由自己的保险丝负责交出去。
        """
        self.gateway.on_inbound(message("第一段..", message_id="m1"))
        self.gateway.on_inbound(message("/help", message_id="m2"))

        self.assertEqual(self.commands, [(CONVERSATION, self.adapter_result, "/help")])
        self.assertEqual(self.gateway._merger.held_text(CONVERSATION), "第一段")

    # --- 两个会话不许互相合并 ---------------------------------------------
    def test_two_conversations_never_merge_into_one_prompt(self):
        self.session_id_by_conversation[CONVERSATION] = SESSION_ID
        self.other_session_id = "ses_fake0002"
        self.session_id_by_conversation[OTHER_CONVERSATION] = self.other_session_id
        self.gateway.on_inbound(message("给 dev 的..", conversation_id=CONVERSATION,
                                        message_id="d1"))
        self.gateway.on_inbound(message("给 ops 的..", conversation_id=OTHER_CONVERSATION,
                                        message_id="o1"))
        self.gateway.on_inbound(message("dev 第二行", conversation_id=CONVERSATION,
                                        message_id="d2"))
        self.gateway.on_inbound(message("ops 第二行", conversation_id=OTHER_CONVERSATION,
                                        message_id="o2"))

        self.assertEqual(self.prompt_bodies, [
            (SESSION_ID, "给 dev 的\ndev 第二行"),
            (self.other_session_id, "给 ops 的\nops 第二行"),
        ])

    # --- 长输入回执 --------------------------------------------------------
    def test_a_long_input_is_acknowledged_on_a_platform_that_cannot_edit(self):
        self.adapter_result = StandInAdapter({}, hooks=None)
        self.adapter_result.supports_message_edit = False
        long_text = "改一下 README 的安装步骤" * 20

        self.gateway.on_inbound(message(long_text, message_id="m1"))

        self.assertIn("已收到", self.send_text.outgoing[0].text)
        self.assertIn(str(len(long_text)), self.send_text.outgoing[0].text)
        # 回执**不能**取代占位消息，也不能多发一条：正文仍然只投递一次。
        self.assertEqual(self.prompt_bodies, [(SESSION_ID, long_text)])

    def test_a_long_input_is_not_acknowledged_where_the_placeholder_already_shows(self):
        """能改写已发消息的平台本来就有 ``⏳ 处理中…``，再加一句只是让一次提问
        变成三条消息。判据读的是能力声明，不是平台名单。"""
        self.adapter_result = StandInAdapter({}, hooks=None)
        self.adapter_result.supports_message_edit = True

        self.gateway.on_inbound(message("长输入" * 60, message_id="m1"))

        self.assertNotIn("已收到", "".join(
            out.text for out in self.send_text.outgoing
        ))

    def test_a_short_input_is_not_acknowledged_even_where_nothing_else_shows(self):
        """门槛之上的那一句之外的输入不该收到回执 —— 这里把门槛调高再验一次。"""
        gateway = self.build_gateway(
            bridge_config={"long_input_ack_chars": 500,
                           "merge_continue_timeout_seconds": 0.05},
        )
        self.adapter_result = StandInAdapter({}, hooks=None)
        self.adapter_result.supports_message_edit = False

        gateway.on_inbound(message("hi", message_id="m1"))

        self.assertNotIn("已收到", "".join(
            out.text for out in self.send_text.outgoing
        ))

    def test_the_acknowledgement_threshold_is_configurable(self):
        gateway = self.build_gateway(
            bridge_config={"long_input_ack_chars": 500,
                           "merge_continue_timeout_seconds": 0.05},
        )
        self.adapter_result = StandInAdapter({}, hooks=None)
        self.adapter_result.supports_message_edit = False

        gateway.on_inbound(message("短于门槛的输入", message_id="m1"))

        self.assertNotIn("已收到", "".join(
            out.text for out in self.send_text.outgoing
        ))


# ----------------------------------------------------------------------
# 3c-续: 保险丝到点时那句「已把等到的内容原样发出」必须**说真话**
# ----------------------------------------------------------------------
# ⚠️ 缺陷在**生产侧**::_deliver_expired_hold 无条件先发那句回执，而
# :meth:`_persist_and_enqueue` 有三条正当的「没有投递」的路（去重命中 / 收件箱
# 已关 / 收件箱写失败）⇒ 用户被明确告知「原样发出」，然后**永远等不到答复**，
# 而那一条也不会被重放。与 ``c41c3ae``（收件箱已关被报成去重命中）是同一个
# 形状的谎报，只是这一次的受害者是读者本人而不是日志的读者。
#
# ⚠️ 而「同一条并集超时两次」**不是假想**：合成的那条 ``Inbound`` 没有
# ``message_id`` ⇒ 去重键落到 ``sha256(platform|conversation_id|text)`` 兜底 ⇒
# 同一会话里第二次超时**逐字**撞上，而「把同一段话重发一遍」是极常见的动作。
#
# ⚠️ 全类**不出现计时**：同步点是 ``Event``（等保险丝回调**跑完**），不是等时长。
class ExpiredHoldNoticeHonestyTests(InboundGatewayTestCase):
    """用户只在真的投递出去时才被告知「已发出」。"""

    bridge_config = {"merge_continue_timeout_seconds": 0.05}

    def setUp(self) -> None:
        super().setUp()
        # ⚠️ 基类那个壳是「先置信号、后投递」，照它断言会读到投递**之前**的快照 ⇒
        # 这里再包一层：投递**跑完**之后才置信号，于是 Event 是同步点而不是计时判据。
        deliver_then_signal = self.gateway._merger._on_hold_expired

        def deliver_before_signalling(conversation_id: str, held_text: str) -> None:
            try:
                deliver_then_signal(conversation_id, held_text)
            finally:
                self.hold_expired.set()

        self.gateway._merger._on_hold_expired = deliver_before_signalling

    def expire_a_hold(self, text: str, *, message_id: str) -> None:
        """送一行带 ``..`` 的入站，然后等到保险丝那一趟**跑完**。"""
        self.hold_expired.clear()
        self.gateway.on_inbound(message(text, message_id=message_id))
        self.assertTrue(
            self.hold_expired.wait(10.0),
            "the hold fuse never fired for %r (deadlock guard, not a timing "
            "assertion)" % text,
        )

    def what_the_reader_was_told(self) -> list[str]:
        return [out.text for out in self.send_text.outgoing]

    def test_the_first_expiry_does_say_that_the_held_text_was_sent(self):
        self.expire_a_hold("帮我看下 README..", message_id="m1")

        self.assertEqual(self.prompt_bodies, [(SESSION_ID, "帮我看下 README")])
        self.assertIn(
            HELD_EXPIRED_NOTICE % "帮我看下 README", self.what_the_reader_was_told()
        )

    def test_the_same_union_expiring_twice_is_never_reported_as_sent(self):
        """同一会话里第二次超时发出的同一段并集：没说发出，且说清了没发出去。

        ⚠️ 为什么会撞上：保险丝合成的那条 ``Inbound`` **没有** ``message_id``
        （它不是平台交付的一条消息）⇒ 去重键落到 ``sha256(platform|conversation_id|
        text)`` 兜底 ⇒ 同一会话里同一段并集的第二次超时**逐字**同一个键。
        这里把那个键**独立**算一遍（不调生产函数算），顺带把这条兜底钉住。
        """
        self.expire_a_hold("帮我看下 README..", message_id="m1")
        self.assertEqual(self.prompt_bodies, [(SESSION_ID, "帮我看下 README")])
        self.send_text.outgoing.clear()

        content_hash_key = hashlib.sha256(
            ("%s|%s|%s" % (PLATFORM, CONVERSATION, "帮我看下 README")).encode("utf-8")
        ).hexdigest()
        # 第一次那一趟落下来的就是这一行（替身只对点名的 delivery_id 报去重）。
        self.assertEqual(self.inbox.recorded[-1].delivery_id, content_hash_key)
        self.assertIsNone(self.inbox.recorded[-1].message_id)
        self.inbox.duplicate_of = content_hash_key

        self.expire_a_hold("帮我看下 README..", message_id="m2")

        self.assertEqual(self.prompt_bodies, [(SESSION_ID, "帮我看下 README")],
                         "第二次不该再让 agent 跑一遍同一段并集")
        told = self.what_the_reader_was_told()
        self.assertNotIn(
            HELD_EXPIRED_NOTICE % "帮我看下 README", told,
            "⛔ 绝不能对一条没有投递的并集说「已把等到的内容原样发出」",
        )
        self.assertIn(HELD_NOT_DELIVERED_NOTICE % "帮我看下 README", told)
        # 缓冲里的内容必须回到读者手上 —— 否则他既没发出去、也看不到自己敲了什么。
        self.assertTrue(any("帮我看下 README" in text for text in told))

    def test_an_inbox_that_cannot_be_written_says_the_message_was_not_delivered(self):
        """写前落盘失败 ⇒ 盘上没回执、也发不出去 ⇒ 用户必须知道要自己重发。"""
        self.inbox.record = mock.Mock(
            side_effect=sqlite3.OperationalError("database or disk is full")
        )

        with self.assertLogs("opencode_bridge.inbound_gateway", level="ERROR"):
            self.gateway.on_inbound(message("写不下来的那条", message_id="m1"))

        self.assertEqual(self.client.prompts, [], "写前义务没履行就不许投递")
        self.assertEqual(self.inbox.writes, [])
        told = "".join(self.what_the_reader_was_told())
        self.assertIn("没有发出去", told)
        self.assertIn("请重新发送一次", told)


# ----------------------------------------------------------------------
# 3b-续: ``merge_continue_timeout_seconds`` 的判据 —— 配 0 就是配 0
# ----------------------------------------------------------------------
# ⚠️ 这一类测的是**上一个环节**，缺陷就住在那儿：
# ``tests/test_inbound_merge.py`` 锁的是合并器在 ``hold_timeout_seconds=0``
# 时的行为（那条本来就绿），而 ``inbound_gateway._positive_float`` 曾把配置里的
# ``0`` 换成默认值 ⇒ 合并器**永远收不到 0** ⇒ 那个分支是死代码。
# ⇒ 只测合并器，这个缺陷会**整条漏掉**。
#
# 全类**不出现任何计时**（AGENTS.md §7.1：本机 ``monotonic`` 只有 16 ms 分辨率）：
# 读的都是离散事实 —— 保险丝装没装上、缓冲还在不在。
class MergeFuseTimeoutConfigurationTests(InboundGatewayTestCase):
    """``merge_continue_timeout_seconds: 0`` 必须**真的**是 0。

    ⚠️ 这个配置项是 G2 崩溃窗口的**唯一**旋钮：被扣在 ``ConversationMerger``
    内存 dict 里的整条消息（落盘在它之后），暴露窗口的上界就是它。
    判成 ``> 0`` 的话，「配 0 关掉窗口」会悄悄变成「配 0 仍是 15 秒窗口」。
    """

    def build_with_timeout(self, timeout_seconds) -> InboundGateway:
        """每条用例都自己建网关 —— 判据各自不同，不吃类级默认值。"""
        gateway = self.build_gateway(
            bridge_config={"merge_continue_timeout_seconds": timeout_seconds},
        )
        # 别让装上去的保险丝在用例返回之后才到点（那会去碰替身）。
        self.addCleanup(gateway._merger.stop)
        return gateway

    # --- 0 得真的到得了合并器 ---------------------------------------------
    def test_a_configured_zero_reaches_the_merger_as_zero(self):
        gateway = self.build_with_timeout(0)

        self.assertEqual(gateway._merger._hold_timeout_seconds, 0.0)

    def test_a_configured_zero_arms_no_fuse(self):
        """0 = 关掉保险丝：缓冲还在，但**没有**计时器。

        这才是那个分支的判据 —— 缺陷形态下这里会被装上一个 15 秒的保险丝。
        """
        gateway = self.build_with_timeout(0)

        gateway.on_inbound(message("敲完就走了..", message_id="m1"))

        self.assertEqual(gateway._merger._fuses, {})
        self.assertEqual(gateway._merger.held_text(CONVERSATION), "敲完就走了")

    def test_a_positive_timeout_still_arms_exactly_one_fuse(self):
        """反向对照：不是"配什么都关掉保险丝" —— 否则上面两条就是恒真的。"""
        gateway = self.build_with_timeout(0.05)

        gateway.on_inbound(message("第一段..", message_id="m1"))

        self.assertEqual(list(gateway._merger._fuses), [CONVERSATION])

    # --- 0 的语义是"关掉保险丝"，不是"静默丢消息" --------------------------
    def test_a_fuse_free_hold_is_delivered_together_with_the_next_line(self):
        """关掉保险丝之后缓冲要**一直留着**，等下一条非 ``..`` 行一起发出去。

        这条盯的是「有没有被静默丢掉」：``_rearm_fuse`` 那个 ``<= 0`` 早退只该
        **不装计时器**，不该动缓冲本身。
        """
        gateway = self.build_with_timeout(0)

        gateway.on_inbound(message("第一段..", message_id="m1"))
        gateway.on_inbound(message("第二段!!", message_id="m2"))

        self.assertEqual(self.prompt_bodies, [(SESSION_ID, "第一段\n第二段")])

    # --- 判据没被顺手放宽 ---------------------------------------------------
    def test_a_negative_timeout_still_falls_back_to_the_default(self):
        """改成 ``>= 0`` 不等于把判据删了：负数仍然要退回默认值。"""
        gateway = self.build_with_timeout(-1)

        self.assertEqual(
            gateway._merger._hold_timeout_seconds,
            DEFAULT_MERGE_CONTINUE_TIMEOUT_SECONDS,
        )

    def test_a_non_finite_timeout_still_falls_back_to_the_default(self):
        """NaN / ±inf 走的是同一个函数里的**另外两个** return，别碰坏它们。"""
        for illegal_timeout in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(timeout=illegal_timeout):
                gateway = self.build_with_timeout(illegal_timeout)

                self.assertEqual(
                    gateway._merger._hold_timeout_seconds,
                    DEFAULT_MERGE_CONTINUE_TIMEOUT_SECONDS,
                )

    def test_the_two_positive_parsers_agree_that_zero_is_legal(self):
        """同族两个解析器对 ``0`` 必须**同一个答案** —— 一侧性是本条的判据。

        ⚠️ ``core.py`` 里那个**同名**的 ``_positive_float`` 不在此列：它服务
        ``edit_interval`` / ``max_message_chars``，那两个 ``0`` 无意义。
        """
        self.assertEqual(_positive_float(0, 15.0), 0.0)
        self.assertEqual(_positive_int(0, 180), 0)

    # --- 显式配 0 必须有一条点名该键的 WARNING -----------------------------
    # 判据的来源是 AGENTS.md §4.1 那条**既有**纪律：键被 ``start()`` 接受却不按
    # 字面直觉生效 ⇒ 必须有一条点名该键的 WARNING。``0`` 正是那种「**会被接受、
    # 但语义反直觉**」的值（它把 G2 的丢消息窗口从有界变成无界）。
    # ⚠️ 下面四条**各测一件不同的事** —— 任何一条单独存在都会被别的形态绕过：
    # 只测「有没有告警」时，一条什么都不说的告警照样绿；只测「点名了键」时，
    # 一条只写好处、把代价留给文档的告警照样绿。
    def test_a_configured_zero_warns_and_names_the_key(self):
        """判据①：告警存在，且**点名该键**。

        缺陷形态（告警被删）⇒ 这里红，②③④ 全绿 ⇒ 覆盖是分开的。
        """
        with self.assertLogs(INBOUND_GATEWAY_LOGGER, level="WARNING") as caught:
            self.build_with_timeout(0)

        self.assertIn("merge_continue_timeout_seconds", "\n".join(caught.output))

    def test_the_warning_also_states_that_zero_is_not_the_safer_setting(self):
        """判据②：⛔ **代价必须与好处在同一条告警里** —— 只写好处会被读反。

        缺陷形态（保留键名、删掉代价那半句）⇒ 只有本条红。
        """
        with self.assertLogs(INBOUND_GATEWAY_LOGGER, level="WARNING") as caught:
            self.build_with_timeout(0)

        warning = "\n".join(caught.output)
        self.assertIn("配 0 并不比默认更安全", warning)
        self.assertIn("无界", warning)

    def test_the_warning_names_the_three_drain_paths_it_has_to_name(self):
        """判据③：代价的**凭据**也得点名 —— 正是那三个出口零调用点，关停才不排空。

        少点名一个，读者就得自己去查"那到底排不排空" ⇒ 三样都要在。
        """
        with self.assertLogs(INBOUND_GATEWAY_LOGGER, level="WARNING") as caught:
            self.build_with_timeout(0)

        warning = "\n".join(caught.output)
        for drain_path in ("flush", "stop", "held_conversation_ids"):
            with self.subTest(drain_path=drain_path):
                self.assertIn(drain_path, warning)

    def test_the_configured_default_is_silent(self):
        """判据④：⛔ 用默认值（15）时**不许**发 —— 否则每次启动都有一条噪音。

        缺陷形态（把判据写成「键在场就发」）⇒ 这里红。⚠️ 而那个形态在生产上
        **恒真**：`Config._merge_bridge` 总把默认值补进 ``bridge`` 段，所以这个键
        永远在场 ⇒ 只看"显式配 0 那条"根本抓不住这个缺陷。
        """
        with self.assertNoLogs(INBOUND_GATEWAY_LOGGER, level="WARNING"):
            self.build_with_timeout(DEFAULT_MERGE_CONTINUE_TIMEOUT_SECONDS)

    def test_an_absent_key_falls_back_and_is_silent(self):
        """「没配」与「配 0」必须分开：没配走的是**回落**那条路。

        ⚠️ 这是判据④的另一半：网关这一侧拿到的可能是 ``None``（键缺），
        也可能已经是合并过默认值的 ``15.0``（生产常态）—— 两条都不许发告警。
        """
        with self.assertNoLogs(INBOUND_GATEWAY_LOGGER, level="WARNING"):
            gateway = self.build_gateway(bridge_config={})
        self.addCleanup(gateway._merger.stop)

        self.assertEqual(
            gateway._merger._hold_timeout_seconds,
            DEFAULT_MERGE_CONTINUE_TIMEOUT_SECONDS,
        )

    def test_a_rejected_timeout_does_not_claim_it_was_a_configured_zero(self):
        """配错（非数字 / 负数 / NaN）走的也是**回落** ⇒ 不许发那条 0 的告警。

        ⚠️ 生产上这些值先被 ``Config._merge_bridge`` 当场丢掉并各自告警；这里直接
        喂给网关，测的是**网关这一侧**的判据：回落出来的值不是 0 ⇒ 不落进告警分支。
        """
        for rejected_timeout in ("不是数字", -1, float("nan")):
            with self.subTest(timeout=rejected_timeout):
                with self.assertNoLogs(INBOUND_GATEWAY_LOGGER, level="WARNING"):
                    self.build_with_timeout(rejected_timeout)


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
        """⚠️ 本条仍成立：``flush_queue`` **不许**起第二个排空者。

        但它**要**把那次唤醒记下来 —— 见
        :meth:`test_a_flush_landing_while_a_prompt_is_in_flight_is_not_lost`。
        「不并发」与「不丢唤醒」是两件事，只有后者是新行为。
        """
        self.gateway._draining.add(CONVERSATION)
        self.deliver("排队中")

        self.gateway.flush_queue(CONVERSATION)

        self.assertEqual(self.client.prompts, [])
        self.assertEqual(self.queued_texts(), ["排队中"])
        self.assertEqual(self.gateway._flush_requested, {CONVERSATION},
                         "唤醒必须被记下来，否则它会被吞掉")

    # ------------------------------------------------------------------
    # 丢唤醒（台账第 12 行 / 第 19 行那条时序）
    # ------------------------------------------------------------------
    #: 死锁守卫用的超时（本文件不用等时长做时序判定，只用它防挂死）。
    JOIN_GUARD_SECONDS = 10.0

    def _stage_a_prompt_in_flight(self, *, busy_calls: int = 1):
        """把第一条消息的 ``prompt()`` 停在半路，并把第二条排到它后面。

        返回 ``(drain_thread, release_prompt, prompt_entered)``；两个 Event 是
        **同步点**（⛔ 不用 ``time.sleep``）。
        """
        prompt_entered = threading.Event()
        release_prompt = threading.Event()
        calls = {"count": 0}
        original_prompt = self.client.prompt

        def blocking_prompt(session_id, text):
            calls["count"] += 1
            original_prompt(session_id, text)          # 记账走真替身
            prompt_entered.set()
            release_prompt.wait(self.JOIN_GUARD_SECONDS)   # 死锁守卫
            if calls["count"] <= busy_calls:
                raise OpenCodeError("busy", status=409)

        self.client.prompt = blocking_prompt
        self.addCleanup(setattr, self.client, "prompt", original_prompt)

        drain_thread = threading.Thread(
            target=self.gateway._enqueue, args=(queued("d1", "第一条"),), daemon=True)
        drain_thread.start()
        self.assertTrue(prompt_entered.wait(self.JOIN_GUARD_SECONDS),
                        "第一条的 prompt() 从没被调用 —— 交错没搭起来")
        self.assertIn(CONVERSATION, self.gateway._draining,
                      "prompt 在途时排空权必须在位，否则测的不是这条时序")
        # 第二条排在它后面（_enqueue 看到 _draining 就直接 return）
        self.gateway._enqueue(queued("d2", "第二条"))
        return drain_thread, release_prompt, calls

    def _join_drain(self, drain_thread) -> None:
        drain_thread.join(self.JOIN_GUARD_SECONDS)
        self.assertFalse(drain_thread.is_alive(), "排空线程没退出")

    def test_a_flush_landing_while_a_prompt_is_in_flight_is_not_lost(self):
        """⭐ 台账第 12 行：**丢唤醒 ⇒ 消息滞留 RAM**。

        丢在哪一步：适配器线程正卡在 ``client.prompt()``（``_draining`` 仍含该会话）
        ⇒ turn 收尾事件到达、``flush_queue`` 命中 ``_draining`` 直接返回、**唤醒被丢弃**
        ⇒ 随后那次 dispatch 拿到 **409**、``_drain`` 把消息塞回队首并 ``discard(_draining)``
        ⇒ 此后**没有任何东西会再排它**。

        判据 = **那条交错下消息确实回来了**（队列排空 + 两条都送出去）。
        ⛔ 「第二次 flush 发生在第一次之后」这种断言在**没丢唤醒时也成立** ⇒ 不算判据。
        ⛔ 不用 ``time.sleep``：交错由两个 Event 钉死。
        """
        drain_thread, release_prompt, calls = self._stage_a_prompt_in_flight()
        # ⭐ 台账那条时序：终止事件在 prompt 在途时到达
        self.gateway.flush_queue(CONVERSATION)
        release_prompt.set()                 # prompt 现在返回 409
        self._join_drain(drain_thread)

        self.assertEqual(calls["count"], 3,
                         "409 之后必须兑现那次被丢下的唤醒：第一条重试一次 + 第二条一次")
        self.assertEqual(
            self.prompt_bodies,
            [(SESSION_ID, "第一条"), (SESSION_ID, "第一条"), (SESSION_ID, "第二条")],
            "被丢唤醒的那条消息**必须回到队列并被送出** —— 滞留 RAM 就是这条缺陷。"
            "⚠️ 「第一条」出现两次是对的：第一次被 409 拒收（服务端压根没跑它），"
            "收件箱那行也退回 pending，所以重发它是既有正确行为，不是重复投递。"
            "（用 prompt_bodies 而不是 client.prompts：后者带渠道说明前缀。）")
        self.assertEqual(self.queued_texts(), [])
        self.assertEqual(self.gateway._draining, set())
        self.assertEqual(self.gateway._flush_requested, set(),
                         "唤醒兑现后必须清掉标记，否则下一次 409 会无限重试")

    def test_the_control_interleaving_still_drains(self):
        """⭐ 反面对照：flush 落在**排空权释放之后** ⇒ 照旧一次排空。

        ⛔ 这条是为了证明上面那条不是「反正总能排空」：两组只差 flush 的落点。
        """
        drain_thread, release_prompt, calls = self._stage_a_prompt_in_flight()
        release_prompt.set()
        self._join_drain(drain_thread)
        self.assertEqual(self.queued_texts(), ["第一条", "第二条"])   # 都退回队首了

        self.gateway.flush_queue(CONVERSATION)

        self.assertEqual(calls["count"], 3)
        self.assertEqual(self.queued_texts(), [])
        self.assertEqual(self.gateway._draining, set())

    def test_a_second_busy_hands_over_instead_of_looping_forever(self):
        """⭐ 兑现唤醒只多试**一次**：服务端持续 409 时必须交出排空权。

        ⛔ 没有这条，「一次兑现就够不成热循环」是**没人看守**的 —— 而反复 409 的
        服务端会被这个重试喂成打服务器的忙循环。
        ⚠️ 必须有**真的排空者**去兑现：手工往 ``_draining`` 里塞一个会话 id
        是**没有排空者**的，那样没人兑现、消息只会一直排着（那是上面那条用例的
        场景，不是这条）。所以这里复用 :meth:`_stage_a_prompt_in_flight`。
        """
        drain_thread, release_prompt, calls = self._stage_a_prompt_in_flight(
            busy_calls=2)                    # ⭐ 第一次与重试都 409
        self.gateway.flush_queue(CONVERSATION)   # 在途 ⇒ 记下一次唤醒
        release_prompt.set()
        self._join_drain(drain_thread)

        self.assertEqual(calls["count"], 2, "兑现 = 恰好多试一次；再多就是热循环")
        self.assertEqual(self.queued_texts(), ["第一条", "第二条"],
                         "两次都 409 ⇒ 第一条退回队首；第二条从未被尝试，仍排着")
        self.assertEqual(self.gateway._draining, set(), "第二次 409 必须交出排空权")
        self.assertEqual(self.gateway._flush_requested, set(),
                         "标记已清 ⇒ 不会再因这次唤醒重试")

    def test_a_busy_session_is_not_a_failure_it_goes_back_to_the_front(self):
        self.client.prompt_errors.append(OpenCodeError("busy", status=409))
        self.deliver("第一条", delivery_id="d1")

        self.assertEqual(self.prompt_bodies, [(SESSION_ID, "第一条")])
        self.assertEqual([queued.text for queued in
                          self.gateway._queues[CONVERSATION]], ["第一条"])
        self.assertEqual(self.gateway._draining, set())
        # 409 没有花掉重试预算：收件箱退回 pending —— 服务端拒收 = 从未尝试过。
        self.assertEqual(self.states_of(), {"d1": DeliveryState.PENDING})
        self.assertEqual(self.inbox.failures, {})
        self.assertEqual(
            self.inbox.writes, [("d1", "attempting"), ("d1", "pending")],
            "409 只多这一次写入：不写 failed、不重置任何别的账",
        )

        self.gateway.flush_queue(CONVERSATION)

        self.assertEqual(self.states_of(), {"d1": DeliveryState.DELIVERED})

    def test_a_busy_row_on_the_recovery_path_is_a_failure_not_a_pending_rewind(self):
        """恢复路径上的 409 **不**退回 pending —— 那里没有内存队列可退。

        ``recording_delivery=False`` 时 :meth:`_dispatch_prompt` 手上没有收件箱，
        所以这条 ``mark_pending`` 压根不该被调用：重放失败要照常冒泡给
        :func:`~opencode_bridge.inbox_recovery.recover_pending`，由它记 ``failed``。
        少了那个 ``if inbox is not None`` 守卫，这一行会被悄悄退回 pending ——
        于是每次启动都重放一次，烧不掉任何预算，正是"无限重试"的形状。
        """
        self.client.prompt_errors.append(OpenCodeError("busy", status=409))

        outcome = self.gateway._dispatch_prompt(
            queued("d1", "重放"), recording_delivery=False
        )

        self.assertEqual(outcome, "busy")
        self.assertEqual(self.inbox.rows, {}, "恢复路径上这里必须一行都不写")
        self.assertEqual(self.inbox.writes, [])

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

    def test_a_message_lost_to_a_drain_failure_is_told_it_was_never_sent(self):
        """⛔ 排空途中抛异常时，那一条**已经不在队列里**了 ⇒ 不许静默。

        ⚠️ 触发点是真的，而且很窄：``_dispatch_prompt`` 抛得出来的唯一现实成因是
        收件箱那两次写入（``adapter_for`` 抛不出来；``send_text`` 永远不抛 ——
        :meth:`~opencode_bridge.adapters.base.Adapter.send_observed` 把适配器的
        异常交还而不是上抛）。这里**不** mock 被测方法：替身让真收件箱替身的那次
        UPDATE 真的抛出来，走的是生产里同一条路。
        """
        self.inbox.mark_attempting = mock.Mock(
            side_effect=sqlite3.OperationalError("database or disk is full")
        )

        with self.assertLogs("opencode_bridge.inbound_gateway", level="ERROR"):
            self.gateway.on_inbound(message("坏消息", message_id="m1"))

        self.assertEqual(self.gateway._draining, set())
        self.assertEqual(self.client.prompts, [])
        told = "".join(text for _, text in self.texts_of())
        self.assertIn("没能提交给 agent", told)
        self.assertIn("请重新发送一次", told)
        self.assertEqual(
            self.inbox.writes,
            [(self.inbox.recorded[0].delivery_id, "recorded")],
            "⛔ 不许替恢复层改账：抛在哪一步我们不知道，而把一个可能已经 delivered 的行"
            "改成 failed 会让下次启动重放它 ⇒ agent 对同一条指令跑两遍",
        )

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

    def test_a_prompt_failure_without_a_status_is_an_unknown_outcome_not_a_failure(self):
        """⭐ 这就是本次分出第三态的那条判据：**有 status ⇒ 明确失败；没 status ⇒ 不知道**。

        :meth:`OpenCodeClient._request` 在超时 / 连接被拒 / 流中断时抛的
        :class:`OpenCodeError` **不带** ``status``（它压根没拿到 HTTP 答复），而那一刻
        请求**已经**发出去了 ⇒ 远端是否收到**没有被记录**。

        ⛔ 记 ``failed`` 就是把"不知道"说成"一定没送到"，恢复层会按退避阶梯重放它
        ⇒ **agent 对同一条指令跑两遍**（AGENTS.md §8 第 3 条）。
        """
        self.client.prompt_errors.append(OpenCodeError("POST /session/x -> timed out"))

        self.deliver()

        self.assertEqual(
            self.inbox.writes,
            [("d1", "attempting"), ("d1", "outcome_unknown")],
            "第三态写在这一行之后 —— 绝不能是 failed（``recorded`` 不在其中："
            "``deliver`` 直接入队，绕过了 ``on_inbound`` 的写前落盘）",
        )
        self.assertEqual(self.states_of(), {"d1": DeliveryState.OUTCOME_UNKNOWN})
        self.assertIn("结果未知", self.send_text.last.text)
        self.assertIn("请重新发送一次", self.send_text.last.text,
                      "不重放的代价 = 远端真没收到时那条指令丢了 ⇒ 必须告诉用户怎么办")
        self.assertNotIn("发送失败", self.send_text.last.text,
                         "「发送失败」是一句谎话 —— 用户会照它去 /new 重建会话，"
                         "把一次可能成功的提交变成丢会话")
        self.assertNotIn("/new", self.send_text.last.text)

    def test_a_failure_that_carries_a_status_is_still_a_definite_failure(self):
        """反向对照：有 status ⇒ 服务端**明确**给了答复 ⇒ 那一档仍然可重放。

        没有这一条，上面那条可能是"因为什么都记成了未知才绿的"。
        """
        self.client.prompt_errors.append(OpenCodeError("server said no", status=503))

        self.deliver()

        self.assertEqual(self.states_of(), {"d1": DeliveryState.FAILED})
        self.assertNotIn("结果未知", self.send_text.last.text)

    def test_a_non_opencode_error_from_prompt_is_also_an_unknown_outcome(self):
        """连 status 都没有的异常：同样**不知道**请求发出没有 ⇒ 同一个第三态。

        这一条也是生产侧那句 ``pragma: no cover`` 被删掉的理由 —— 它真的会被走到。
        """
        self.client.prompt_errors.append(RuntimeError("socket died"))

        self.deliver()

        self.assertEqual(self.states_of(), {"d1": DeliveryState.OUTCOME_UNKNOWN})
        self.assertIn("请重新发送一次", self.send_text.last.text)

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

    def test_a_live_transport_failure_is_alerted_on_the_next_boot_and_never_replayed(self):
        """⭐ 端到端：实况投递时传输层失败 ⇒ 落第三态 ⇒ 下次启动**只告警、绝不重放**。

        这是本次修复的**全链路**那一格：单看任一端都成立（投递端落对了状态 / 恢复端
        不重放），而缺陷恰恰在两者的接缝上 —— 状态记错一档，整条链就重新变成
        "agent 对同一条指令跑两遍"。
        """
        self.inbox.record(queued("d1", "改一下 README"))
        self.client.prompt_errors.append(OpenCodeError("POST /session/x -> timed out"))

        self.gateway._enqueue(queued("d1", "改一下 README"))

        self.assertEqual(self.states_of(), {"d1": DeliveryState.OUTCOME_UNKNOWN})
        # 换一轮：接下来的断言全部属于"下一次启动"，所以先把实况那一轮清干净。
        self.inbox.writes.clear()
        self.send_text.outgoing.clear()
        self.client.prompts.clear()

        self.gateway.recover_inbox()

        self.assertEqual(self.client.prompts, [],
                         "结果未知的一行绝不许重放 —— 它可能**已经**跑过了")
        self.assertEqual(self.inbox.writes, [],
                         "只告警那一档不许被挪走，否则用户可能还没看见的那次告警就没了")
        self.assertEqual(self.states_of(), {"d1": DeliveryState.OUTCOME_UNKNOWN})
        self.assertEqual(len(self.send_text.outgoing), 1)
        self.assertIn("请重新发送一次", self.send_text.last.text)

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
        """恢复层**绝不**把投递侧的异常抛出去 —— 不抛就是这一条的全部断言。

        ⚠️ 落哪一档是第二件事，而这一条顺手把它钉住了：``prompt`` 抛的是裸
        ``RuntimeError``（连 ``status`` 都没有）⇒ **不知道**请求出去没有
        ⇒ 落 ``outcome_unknown``、**只告警、绝不重放**，而不是 ``failed``
        （记成 ``failed`` 就是猜，而重放它 = agent 对同一条指令跑两遍）。
        """
        self.inbox.record(queued("d1", "x"))
        self.client.prompt_errors.append(RuntimeError("socket died"))

        self.gateway.recover_inbox()      # 不抛就是这一条的全部断言

        self.assertEqual(self.states_of(), {"d1": DeliveryState.OUTCOME_UNKNOWN})

    def test_a_replay_that_hits_the_transport_layer_is_never_dispatched_again(self):
        """⭐ **缺陷的端到端那一格**：重放时传输层失败 ⇒ 绝不重放，agent 不跑两遍。

        缺陷活在这条链的**接缝**上：投递侧已经判出「结果未知」（落第三档），而恢复
        侧原先把 ``dispatch_recovered`` 抛出来的任何异常一律记成 ``failed`` ⇒ 那一行
        下次启动按退避阶梯再投一遍 ⇒ **同一条指令跑两遍**。

        ⇒ 判据落在**第二轮**的 ``client.prompts`` 上：那是缺陷真正发作的地方。
        """
        self.inbox.record(queued("d1", "改一下 README"))
        self.client.prompt_errors.append(OpenCodeError("POST /session/x -> timed out"))

        self.gateway.recover_inbox()

        self.assertEqual(self.states_of(), {"d1": DeliveryState.OUTCOME_UNKNOWN})
        self.assertTrue(
            any("请重新发送一次" in sent.text for sent in self.send_text.outgoing),
            "⛔ 只告警不给方法 = 把负担转给用户却不告诉他该做什么"
            "（这里可能有两条：投递侧那句与恢复层按会话合并的那句，两条都必须给方法）",
        )
        # 换一轮：下面那些断言都属于"下一次启动"。
        self.client.prompts.clear()
        self.send_text.outgoing.clear()

        self.gateway.recover_inbox()

        self.assertEqual(self.client.prompts, [],
                         "结果未知的一行绝不许重放 —— 它可能**已经**跑过了")
        self.assertEqual(self.states_of(), {"d1": DeliveryState.OUTCOME_UNKNOWN})

    def test_a_replay_refused_with_an_http_status_is_retried_on_the_next_boot(self):
        """反向对照：服务端**明确**给了答复 ⇒ 明确失败 ⇒ 仍按退避阶梯重试。

        没有这一条，上面那条可能是「因为什么都记成了未知才绿的」—— 而那会让整条
        退避阶梯在恢复路径上**彻底失效**（每一次明确失败都变成要用户手动重发）。
        """
        self.inbox.record(queued("d1", "改一下 README"))
        self.client.prompt_errors.append(OpenCodeError("server said no", status=503))

        self.gateway.recover_inbox()

        self.assertEqual(self.states_of(), {"d1": DeliveryState.FAILED})
        # ⚠️ 这里断言的是**合成**异常的 status（``None``），而**不是**服务端的 503 ——
        # 那一个 :meth:`InboundGateway._dispatch_prompt` 知道却没有带出来（见
        # ``dispatch_recovered`` 那段 docstring：⛔ 不许在这里编一个 status）。
        # 盘上真正分开两类的是 ``agent_may_have_run=False``，而它确实在。
        self.assertIn("status=None", self.inbox.failures["d1"])
        self.assertIn("agent_may_have_run=False", self.inbox.failures["d1"],
                      "「prompt 压根没提交」必须落在盘上 —— 那是分档的依据，"
                      "而 status 表达不了它")
        self.assertEqual(self.inbox.attempts, {"d1": 1},
                         "明确失败要**恰好**烧掉一级预算")

        self.inbox.make_failure_due("d1")

        self.gateway.recover_inbox()

        # 两轮各投一次：第一轮被服务端 503 拒掉，第二轮（期限已到）重试并送达。
        self.assertEqual(self.prompt_bodies,
                         [("ses_fake0001", "改一下 README")] * 2,
                         "明确失败的那一档必须在下一轮被重试 —— 否则退避阶梯成了摆设")
        self.assertEqual(self.states_of(), {"d1": DeliveryState.DELIVERED})

    def test_a_create_session_failure_during_a_replay_is_a_definite_failure(self):
        """⭐ ``create_session`` 阶段的传输失败也是「没有 status」，但它**不是**未知。

        那一刻 prompt **压根没提交**给 agent ⇒ 重放**安全** ⇒ 该落 ``failed`` 并按
        退避阶梯重试。只看 ``status`` 会把它判成「结果未知」⇒ 用户收到一句**假话**
        （"请求已经提交给 agent" —— 那一刻它根本没提交），而本该自动送达的消息变成
        要他手动重发。

        ⚠️ 这条现实触发条件很常见：启动时 opencode 还没起来（见
        :meth:`InboundGateway.recover_inbox` 里那句 "replays will most likely fail too"）。
        """
        self.inbox.record(queued("d1", "改一下 README"))
        self.ensure_session_errors.append(OpenCodeError("POST /session -> 拒绝连接"))

        self.gateway.recover_inbox()

        self.assertEqual(self.states_of(), {"d1": DeliveryState.FAILED},
                         "prompt 没提交 ⇒ 明确失败，绝不是「结果未知」")
        self.assertIn("status=None", self.inbox.failures["d1"])
        self.assertEqual(self.inbox.attempts, {"d1": 1})
        self.assertEqual(len(self.send_text.outgoing), 1)
        self.assertIn("创建会话失败", self.send_text.outgoing[0].text,
                      "恢复路径上照样告诉用户「建会话没成」—— 只记档不吭声等于"
                      "把失败说成没有发生")

        self.inbox.make_failure_due("d1")

        self.gateway.recover_inbox()

        self.assertEqual(self.prompt_bodies, [(SESSION_ID, "改一下 README")],
                         "这一档必须在下一轮被重试 —— 否则退避阶梯成了摆设")
        self.assertEqual(self.states_of(), {"d1": DeliveryState.DELIVERED})


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


# ----------------------------------------------------------------------
# 守门：``_dispatch_prompt`` 的返回值必须被 ``dispatch_recovered`` 逐个点名
# ----------------------------------------------------------------------
_GATEWAY_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "opencode_bridge", "inbound_gateway.py",
)

#: ``_dispatch_prompt`` 允许返回的**全集**。新增一档必须先在这里登记 ——
#: 登记的动作本身就是提醒「去把 ``dispatch_recovered`` 也改了」。
_DISPATCH_OUTCOMES = frozenset({"ok", "busy", "error", "outcome_unknown"})


def _returned_literals_in_source(source: str, function_name: str) -> set:
    """这个源文件里那个函数 ``return`` 出去的**字符串字面量**集合。

    ⚠️ 用 AST 而不是行级正则：``return`` 与那个字面量可以隔着任意多行，
    而本仓库为「只认同一行」栽过（AGENTS.md §7.1 那张表）。
    """
    tree = ast.parse(source)
    literals: set = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == function_name):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Return)
                and isinstance(inner.value, ast.Constant)
                and isinstance(inner.value.value, str)
            ):
                literals.add(inner.value.value)
    return literals


def _string_literals_in_function(source: str, function_name: str) -> set:
    """那个函数体里出现的**全部**字符串字面量。"""
    tree = ast.parse(source)
    literals: set = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == function_name):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                literals.add(inner.value)
    return literals


def _stamp_guard_is_a_catch_all(source: str) -> bool:
    """盖「压根没提交」那个戳的分支，它的条件是不是**兜住一切**的形状。

    ⭐ 这是**形状**判据，不是「某个词还在不在」：查的是「那个 ``if`` 的判定里
    有没有否定/不等/不在」，因为**否定式**条件天然是兜住一切的
    （``!= "ok"`` / ``not in (...)`` / ``not x``）。

    :return: ``True`` = 兜住了不该兜的（缺陷形状）；``False`` = 是逐个枚举。
    :raises AssertionError: 找不到那个分支 —— 那说明 :func:`mark_prompt_never_sent`
        的调用点变了（挪了位置或不止一处），判据必须先跟着改，不能默默放过。
    """
    tree = ast.parse(source)
    recovered = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "dispatch_recovered"
    )
    guards: list = []
    for node in ast.walk(recovered):
        if not isinstance(node, ast.If):
            continue
        stamps = [
            inner for inner in ast.walk(node)
            if isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Name)
            and inner.func.id == "mark_prompt_never_sent"
        ]
        if stamps:
            guards.append(node.test)
    if len(guards) != 1:
        raise AssertionError(
            "dispatch_recovered 里盖戳的 if 分支恰好 %d 个（要求恰好 1 个）—— "
            "调用点挪了位置或不止一处，判据得先改" % len(guards)
        )
    return any(
        isinstance(inner, (ast.UnaryOp, ast.NotEq, ast.NotIn))
        for inner in ast.walk(guards[0])
    )


class TestEveryDispatchOutcomeIsClassified(unittest.TestCase):
    """⭐ **覆盖面守门**：盖「可重放」那个戳的分支必须**逐个枚举**，不能兜住一切。

    为什么必须是形状而不是「某几个词还在不在」：恢复层判「能不能重放」的唯一
    凭据是 :func:`~opencode_bridge.inbox_recovery.mark_prompt_never_sent` 盖的那个戳
    ⇒ **谁落进盖戳的分支，谁就被判成可重放**。而上一版 ``dispatch_recovered`` 用
    的是 ``if outcome != "ok":`` ⇒ 任何**新增**的、含义为「结果未知」的返回值都会
    被悄悄当成可重放 ⇒ 双跑 ⇒ 而**没有任何行为用例会红**（既有用例在那条路径一条都
    不经过 —— 本仓库实测过的形状）。

    ⚠️ 判据问的是「什么算好」（盖戳那一支必须枚举），不是「我以为会看到什么」。
    """

    def source(self) -> str:
        with io.open(_GATEWAY_MODULE_PATH, encoding="utf-8") as source_file:
            return source_file.read()

    def test_the_criteria_flags_a_catch_all_guard(self):
        """判据的**辨别力**自证（正反两面）：⛔ 只证明「它现在是绿的」是不够的（§9）。"""
        self.assertTrue(
            _stamp_guard_is_a_catch_all(
                "def dispatch_recovered(q):\n"
                "    outcome = q()\n"
                "    if outcome != 'ok':\n"
                "        raise mark_prompt_never_sent(RuntimeError('x'))\n"
            ),
            "否定式条件天然兜住一切 ⇒ 它必须被判成缺陷形状",
        )
        self.assertFalse(
            _stamp_guard_is_a_catch_all(
                "def dispatch_recovered(q):\n"
                "    outcome = q()\n"
                "    if outcome in ('busy', 'error'):\n"
                "        raise mark_prompt_never_sent(RuntimeError('x'))\n"
            ),
            "逐个枚举的形状不该被误报 —— 误报制造无用工作（§7.1 第 4 条）",
        )

    def test_the_stamping_branch_is_not_a_catch_all(self):
        self.assertFalse(
            _stamp_guard_is_a_catch_all(self.source()),
            "盖「压根没提交」那个戳的分支**兜住了一切** —— 那正是本条缺陷的形状："
            "一个 dispatch_recovered 认不出来的返回值会被判成「明确失败、可以重放」"
            "⇒ agent 对同一条指令跑两遍。逐个枚举，缺省留给「不知道」那一侧。",
        )

    def test_the_stamping_branch_still_names_the_definite_failure_outcomes(self):
        """反向对照：枚举的那一档**必须真的被枚举**（空分支会把明确失败变成不重放）。"""
        named = _string_literals_in_function(self.source(), "dispatch_recovered")

        for outcome in ("busy", "error"):
            self.assertIn(outcome, named,
                          "%r 那一档是「明确失败、可以重放」⇒ 必须在盖戳的分支里"
                          "被点名" % outcome)
        self.assertIn("ok", named)

    def test_every_returned_outcome_is_inside_the_known_set(self):
        returned = _returned_literals_in_source(self.source(), "_dispatch_prompt")

        self.assertTrue(returned, "一个字面量都没量到 ⇒ 判据坏了，不是目标不在（§7.1）")
        self.assertEqual(
            returned - _DISPATCH_OUTCOMES, set(),
            "_dispatch_prompt 新增了返回值 %s 而没登记 ⇒ 那一档的语义没人确认过，"
            "而 dispatch_recovered 的缺省分支会替它做决定"
            % sorted(returned - _DISPATCH_OUTCOMES),
        )

    def test_the_fall_through_branch_still_raises(self):
        """⭐ 缺省那一支**必须真的抛** —— 否则「枚举之外」就等于「静默丢弃」。

        枚举之外的那一档要么被当成「不知道」（抛、记第三态、只告警），要么就
        **什么都没做**：那一行既不重放也不告警，而恢复层随后把它当成已处理完 ⇢
        静默丢弃 —— 正是这个模块存在的理由所反对的那件事。
        """
        source = self.source()
        tree = ast.parse(source)
        recovered = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "dispatch_recovered"
        )
        raises_outside = [
            node for node in ast.walk(recovered)
            if isinstance(node, ast.Raise)
        ]

        self.assertTrue(
            len(raises_outside) >= 2,
            "dispatch_recovered 只有 %d 个 raise ⇒ 「枚举之外」那一档没有被表达出来"
            % len(raises_outside),
        )
        stamped = [node for node in raises_outside
                   if any(isinstance(inner, ast.Call)
                          and isinstance(inner.func, ast.Name)
                          and inner.func.id == "mark_prompt_never_sent"
                          for inner in ast.walk(node))]
        self.assertEqual(len(stamped), 1,
                         "盖戳的那个 raise 必须恰好一个（多处 = 有分支被判成可重放）")
        self.assertEqual(len(raises_outside) - len(stamped), 1,
                         "必须有**另一个**不带戳的 raise = 缺省那一支「不知道」")


if __name__ == "__main__":
    unittest.main()

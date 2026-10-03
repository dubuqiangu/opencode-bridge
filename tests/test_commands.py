"""``CommandHandler`` 的**独立**测试 —— 全程不构造 :class:`BridgeCore`。

这一整个文件存在的理由是 AGENTS.md §5.1 说的"抽出去"那种拆法：新类必须能脱离
原类单独测。所以这里没有 ``make_env``、没有适配器挂载、没有 ``BridgeCore``；
七个协作者要么是真实的小对象（``Config`` / ``ConversationState`` / ``StateStore``），
要么是本文件里现造的替身。

因此这里能断言 core 层面**看不见**的东西：``platform`` 透传、``drop`` 与
``ensure`` 的先后、缺会话时**不许**去碰服务端 —— 那些全是协作契约。
"""

from __future__ import annotations

import inspect
import os
import tempfile
import unittest
from dataclasses import dataclass

from opencode_bridge.adapters.base import Adapter
from opencode_bridge.commands import (
    SETUP_MENU_TEXT,
    CommandHandler,
    HELP_TEXT,
)
from opencode_bridge.config import Config
from opencode_bridge.conversation_keys import ConversationState
from opencode_bridge.core import BridgeCore
from opencode_bridge.hooks import MsgHandle, Outbound
from opencode_bridge.opencode_client import OpenCodeError
from opencode_bridge.session_model import CommandReply
from opencode_bridge.state import StateStore

from tests.test_core import inbound, make_env

CONVERSATION = "chat:55"
PLATFORM = "fake"
SESSION_ID = "ses_injected_by_the_fake_client"
#: 净化器（``_clean``）要在参数解析前吃掉的那个字符
NULL_BYTE = "\x00"


# ----------------------------------------------------------------------
# 替身：七个协作者里只有 client / 发信 / 建会话 / 删会话需要替身
# ----------------------------------------------------------------------
class RecordingOpenCodeClient:
    """``CommandHandler`` 会用到的三个接口（就这三个：interrupt / get_session /
    reply_permission），外加可注入的错误。"""

    def __init__(self) -> None:
        self.session_info: dict = {
            "agent": "builder",
            "model": {"providerID": "prov", "id": "model-x"},
            "cost": 0.25,
            "tokens": {"input": 10, "output": 20},
            "location": {"directory": "/srv/work"},
        }
        self.interrupted: list[str] = []
        self.permission_replies: list[tuple[str, str, str]] = []
        self.get_session_calls: list[str] = []
        self.interrupt_error: Exception | None = None
        self.permission_error: Exception | None = None
        self.get_session_error: Exception | None = None

    def interrupt(self, session_id: str) -> None:
        if self.interrupt_error is not None:
            raise self.interrupt_error
        self.interrupted.append(session_id)

    def get_session(self, session_id: str) -> dict:
        self.get_session_calls.append(session_id)
        if self.get_session_error is not None:
            raise self.get_session_error
        return dict(self.session_info)

    def reply_permission(
        self, session_id: str, request_id: str, decision: str
    ) -> None:
        if self.permission_error is not None:
            raise self.permission_error
        self.permission_replies.append((session_id, request_id, decision))


@dataclass(frozen=True)
class OutgoingReply:
    """一次出站发信的四个参数。"""

    conversation_id: str
    text: str
    kind: str
    session_id: str | None


class RecordingAdapter(Adapter):
    """只需要 ``name`` / ``send`` / ``edit`` —— 命令只用到这三个。"""

    name = PLATFORM

    def __init__(self) -> None:
        super().__init__({}, hooks=None)  # type: ignore[arg-type]
        self.sent: list[Outbound] = []
        self.edited: list[tuple[MsgHandle, Outbound]] = []
        self.edit_error: Exception | None = None
        self._handles = 0

    def send(self, out: Outbound) -> MsgHandle | None:
        self.sent.append(out)
        self._handles += 1
        return MsgHandle(
            conversation_id=out.conversation_id,
            message_id="m%d" % self._handles,
            platform=self.name,
        )

    def start(self) -> None:
        """``Adapter`` 是抽象类，这条是它要求的最小实现。命令不碰它。"""

    def edit(self, handle: MsgHandle, out: Outbound) -> bool:
        self.edited.append((handle, out))
        if self.edit_error is not None:
            raise self.edit_error
        return True


class RecordingSendText:
    """core 那条发信路径的替身：签名一致，只记录，不带任何 core 的行为。"""

    def __init__(self, adapter: Adapter) -> None:
        self.default_adapter = adapter
        self.outgoing: list[OutgoingReply] = []

    def __call__(
        self,
        conversation_id: str,
        text: str,
        *,
        kind: str = "text",
        adapter: Adapter | None = None,
        session_id: str | None = None,
    ) -> MsgHandle | None:
        target = adapter if adapter is not None else self.default_adapter
        handle = target.send(Outbound(
            conversation_id=conversation_id,
            text=text,
            kind=kind,
            session_id=session_id,
        ))
        self.outgoing.append(OutgoingReply(conversation_id, text, kind, session_id))
        return handle

    @property
    def last(self) -> OutgoingReply:
        return self.outgoing[-1]


class RecordingEnsureSession:
    """core 的 ``_ensure_session`` 的替身。"""

    def __init__(self, session_id: str = SESSION_ID) -> None:
        self.session_id = session_id
        self.calls: list[tuple[str, str]] = []

    def __call__(self, conversation_id: str, *, platform: str = "") -> str:
        self.calls.append((conversation_id, platform))
        return self.session_id


class RecordingDropSession:
    """core 的 ``_drop_session`` 的替身。"""

    def __init__(self, existing_session_id: str | None = SESSION_ID) -> None:
        self.existing_session_id = existing_session_id
        self.calls: list[tuple[str, str]] = []

    def __call__(self, conversation_id: str, *, platform: str = "") -> str | None:
        self.calls.append((conversation_id, platform))
        return self.existing_session_id


class StubModelCommand:
    """只回放一条固定回复。

    ``/model`` 自己的参数解析与目录缓存在 ``tests/test_model_command.py`` 里测；
    这里只关心本类有没有把 ``text`` / ``kind`` / ``session_id`` 原样交出去。
    """

    def __init__(self, reply: CommandReply) -> None:
        self.reply = reply
        self.calls: list[tuple[str, str]] = []

    def reply_for(self, conversation_id: str, args: str) -> CommandReply:
        self.calls.append((conversation_id, args))
        return self.reply


# ----------------------------------------------------------------------
# 基类：造一套 CommandHandler（**没有** BridgeCore）
# ----------------------------------------------------------------------
class HandlerTestCase(unittest.TestCase):
    """每个用例一套全新的七件协作者。"""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.config = Config()
        self.state = StateStore(os.path.join(self.tempdir.name, "state.json"))
        self.conversation_state = ConversationState(self.state, lambda: ())
        self.client = RecordingOpenCodeClient()
        self.adapter = RecordingAdapter()
        self.send_text = RecordingSendText(self.adapter)
        self.ensure_session = RecordingEnsureSession()
        self.drop_session = RecordingDropSession()
        self.model_command = StubModelCommand(
            CommandReply("当前模型: prov/model-x", kind="text",
                         session_id=SESSION_ID)
        )
        self.handler = CommandHandler(
            client=self.client,
            config=self.config,
            conversation_state=self.conversation_state,
            model_command=self.model_command,
            ensure_session=self.ensure_session,
            drop_session=self.drop_session,
            send_text=self.send_text,
        )

    # --- 驱动与断言的小帮手 ---------------------------------------------
    def run_command(self, command: str) -> None:
        self.handler.handle_command(CONVERSATION, self.adapter, command)

    @property
    def last_reply(self) -> OutgoingReply:
        return self.send_text.last

    def attach_session(self, session_id: str = SESSION_ID) -> str:
        """在 ``ConversationState`` 里放一条会话（真对象，不打桩）。"""
        self.conversation_state.set_session(CONVERSATION, session_id)
        return session_id


# ----------------------------------------------------------------------
# 1-2: 依赖面（AGENTS.md §5.1 的"抽出去"能不能成立）
# ----------------------------------------------------------------------
class CollaboratorSurfaceTests(HandlerTestCase):
    def test_the_constructor_takes_exactly_the_seven_collaborators(self):
        parameters = inspect.signature(CommandHandler.__init__).parameters
        self.assertEqual(
            [name for name in parameters if name != "self"],
            ["client", "config", "conversation_state", "model_command",
             "ensure_session", "drop_session", "send_text"],
        )
        for name, parameter in parameters.items():
            if name == "self":
                continue
            self.assertEqual(
                parameter.kind, inspect.Parameter.KEYWORD_ONLY,
                "%s 必须显式按名字注入" % name,
            )

    def test_no_bridge_core_is_reachable_from_the_handler(self):
        """拿到了 core 就等于又耦合回大类和它的私有状态 —— 那这次拆分就白做了。"""
        for name, value in vars(self.handler).items():
            self.assertNotIsInstance(value, BridgeCore, name)

    def test_building_a_handler_never_touches_a_bridge_core(self):
        self.assertIsInstance(self.handler, CommandHandler)


# ----------------------------------------------------------------------
# 3-4: help / setup
# ----------------------------------------------------------------------
class HelpAndSetupTests(HandlerTestCase):
    def test_help_sends_the_frozen_text_and_nothing_else(self):
        self.run_command("/help")

        self.assertEqual(self.last_reply.text, HELP_TEXT)
        self.assertEqual(self.last_reply.kind, "text")
        self.assertEqual(self.client.get_session_calls, [])
        self.assertEqual(self.client.interrupted, [])
        self.assertEqual(self.client.permission_replies, [])

    def test_help_accepts_the_telegram_bot_suffix(self):
        self.run_command("/help@my_bridge_bot")

        self.assertEqual(self.last_reply.text, HELP_TEXT)

    def test_setup_menu_ships_the_buttons_in_a_follow_up_edit(self):
        self.run_command("/setup")

        self.assertEqual(self.send_text.outgoing[0].text, SETUP_MENU_TEXT)
        self.assertEqual(len(self.adapter.sent), 1)
        self.assertEqual(
            [button.data for button in self.adapter.edited[-1][1].buttons],
            ["setup:telegram", "setup:slack", "setup:discord"],
        )

    def test_a_failing_button_edit_still_leaves_the_usable_text_menu(self):
        self.adapter.edit_error = RuntimeError("telegram rejected the keyboard")

        self.run_command("/setup")  # 不得抛出去

        self.assertEqual(len(self.adapter.sent), 1)
        self.assertIn("/setup 1", self.adapter.sent[0].text)

    def test_setup_guide_is_sent_for_each_platform_alias(self):
        for argument, needle in (("telegram", "@BotFather"),
                                 ("1", "@BotFather"),
                                 ("TELEGRAM", "@BotFather"),
                                 ("slack", "xoxb-"),
                                 ("2", "xapp-"),
                                 ("discord", "Message Content Intent")):
            with self.subTest(argument=argument):
                self.send_text.outgoing.clear()
                self.run_command("/setup " + argument)

                self.assertIn(needle, self.last_reply.text)
                self.assertEqual(self.last_reply.kind, "text")

    def test_an_unknown_setup_argument_reports_the_valid_ones(self):
        self.run_command("/setup bogus")

        self.assertEqual(self.last_reply.kind, "error")
        self.assertIn("/setup 1|2|3", self.last_reply.text)
        self.assertEqual(self.adapter.edited, [])

    def test_the_setup_guide_never_carries_a_machine_specific_body(self):
        """第一行是运行期算出的配置路径（按设计如此），其余是冻结文案。"""
        self.run_command("/setup telegram")

        body = "\n".join(self.last_reply.text.splitlines()[1:])
        self.assertNotIn("C:\\Users", body)
        self.assertNotIn("/home/", body)


# ----------------------------------------------------------------------
# 5-6: 会话生命周期（/new /reset、/stop、/cd）
# ----------------------------------------------------------------------
class SessionCommandTests(HandlerTestCase):
    def test_new_drops_before_it_ensures(self):
        """顺序反了就会把**新建的**会话删掉 —— 这是本类最该被锁住的不变式。"""
        self.attach_session("ses_old")

        self.run_command("/new")

        self.assertEqual(self.drop_session.calls, [(CONVERSATION, PLATFORM)])
        self.assertEqual(self.ensure_session.calls, [(CONVERSATION, PLATFORM)])
        self.assertEqual(self.last_reply.text,
                         "已新建会话 %s" % SESSION_ID[:12])
        self.assertEqual(self.last_reply.session_id, SESSION_ID)

    def test_reset_is_the_same_command_as_new(self):
        self.run_command("/reset")

        self.assertEqual(self.drop_session.calls, [(CONVERSATION, PLATFORM)])
        self.assertEqual(self.ensure_session.calls, [(CONVERSATION, PLATFORM)])

    def test_the_platform_argument_is_the_adapters_own_name(self):
        """``channel:`` 旧键的归属只能由"发起查询的平台"回答，漏传就成了猜。"""
        self.run_command("/new")

        self.assertEqual(self.drop_session.calls[0][1], PLATFORM)
        self.assertEqual(self.ensure_session.calls[0][1], PLATFORM)

    def test_stop_interrupts_the_attached_session(self):
        session_id = self.attach_session()

        self.run_command("/stop")

        self.assertEqual(self.client.interrupted, [session_id])
        self.assertEqual(self.last_reply.text, "已请求中断当前任务。")
        self.assertEqual(self.last_reply.session_id, session_id)

    def test_stop_without_a_session_never_calls_the_server(self):
        self.run_command("/stop")

        self.assertEqual(self.client.interrupted, [])
        self.assertEqual(self.last_reply.text, "当前没有会话。")
        self.assertEqual(self.last_reply.session_id, None)

    def test_an_interrupted_call_failure_becomes_one_error_reply(self):
        self.attach_session()
        self.client.interrupt_error = OpenCodeError("busy", status=409)

        self.run_command("/stop")

        self.assertEqual(self.last_reply.kind, "error")
        self.assertIn("命令执行失败", self.last_reply.text)

    def test_cd_records_the_directory_verbatim_and_recreates_the_session(self):
        """state 里存**原样**（"相对解析"与回显才有意义），绝对路径只发生在
        发请求的那一刻 —— 那是 ``_ensure_session`` 的事。"""
        self.attach_session("ses_old")

        self.run_command("/cd docs")

        self.assertEqual(
            self.conversation_state.get_meta(
                CONVERSATION, "directory", None, platform=PLATFORM
            ),
            "docs",
        )
        self.assertEqual(self.drop_session.calls, [(CONVERSATION, PLATFORM)])
        self.assertEqual(self.ensure_session.calls, [(CONVERSATION, PLATFORM)])
        self.assertEqual(self.last_reply.text, "已切换到 docs")

    def test_cd_without_an_argument_prints_the_usage(self):
        self.run_command("/cd")

        self.assertEqual(self.last_reply.kind, "error")
        self.assertIn("用法: /cd <目录>", self.last_reply.text)
        self.assertEqual(self.drop_session.calls, [])
        self.assertEqual(self.ensure_session.calls, [])


# ----------------------------------------------------------------------
# 7-8: /status 与 /model
# ----------------------------------------------------------------------
class StatusCommandTests(HandlerTestCase):
    def test_status_reports_the_session_fields(self):
        session_id = self.attach_session()

        self.run_command("/status")

        self.assertEqual(self.client.get_session_calls, [session_id])
        text = self.last_reply.text
        self.assertIn("session_id: %s" % session_id, text)
        self.assertIn("agent: builder", text)
        self.assertIn("model: prov/model-x", text)
        self.assertIn("cost: 0.25", text)
        self.assertIn("tokens: input=10 output=20", text)
        self.assertIn("directory: /srv/work", text)
        self.assertEqual(self.last_reply.kind, "text")

    def test_status_without_a_session_never_calls_the_server(self):
        self.run_command("/status")

        self.assertEqual(self.client.get_session_calls, [])
        self.assertEqual(self.last_reply.text, "当前没有会话。")

    def test_a_session_payload_with_missing_fields_does_not_crash(self):
        """``tokens`` / ``location`` 缺失是常态（老服务端），不许抛。"""
        self.attach_session()
        self.client.session_info = {}

        self.run_command("/status")

        self.assertIn("model: ?", self.last_reply.text)
        self.assertIn("tokens: input=0 output=0", self.last_reply.text)
        self.assertEqual(self.last_reply.kind, "text")

    def test_a_get_session_failure_becomes_one_error_reply(self):
        self.attach_session()
        self.client.get_session_error = OpenCodeError("down", status=500)

        self.run_command("/status")

        self.assertEqual(self.client.get_session_calls, [SESSION_ID])
        self.assertEqual(self.last_reply.kind, "error")
        self.assertIn("命令执行失败", self.last_reply.text)


class ModelCommandForwardingTests(HandlerTestCase):
    def test_model_forwards_the_reply_verbatim(self):
        self.model_command.reply = CommandReply(
            "当前模型: opencode/space-bunny", kind="text", session_id="ses_x",
        )

        self.run_command("/model")

        self.assertEqual(self.model_command.calls, [(CONVERSATION, "")])
        self.assertEqual(self.last_reply.text, "当前模型: opencode/space-bunny")
        self.assertEqual(self.last_reply.session_id, "ses_x")

    def test_model_keeps_an_error_reply_an_error(self):
        self.model_command.reply = CommandReply(
            "用法: /model …", kind="error", session_id=None,
        )

        self.run_command("/model opencode/")

        self.assertEqual(self.model_command.calls,
                         [(CONVERSATION, "opencode/")])
        self.assertEqual(self.last_reply.kind, "error")

    def test_the_bot_suffix_is_stripped_before_forwarding(self):
        self.run_command("/model@my_bridge_bot")

        self.assertEqual(self.model_command.calls, [(CONVERSATION, "")])


# ----------------------------------------------------------------------
# 9-10: /approve 与 /deny（会改动下游 agent 的授权状态，语义不许漂）
# ----------------------------------------------------------------------
class PermissionCommandTests(HandlerTestCase):
    def test_approve_defaults_to_once_and_always_is_opt_in(self):
        session_id = self.attach_session()

        self.run_command("/approve per_1")
        self.run_command("/approve per_2 always")
        self.run_command("/allow per_3 ONCE")

        self.assertEqual(self.client.permission_replies, [
            (session_id, "per_1", "once"),
            (session_id, "per_2", "always"),
            (session_id, "per_3", "once"),
        ])
        self.assertEqual(self.last_reply.text, "已回复权限请求 per_3: once")
        self.assertEqual(self.last_reply.session_id, session_id)

    def test_deny_defaults_to_reject(self):
        session_id = self.attach_session()

        self.run_command("/deny per_4")
        self.run_command("/deny per_5 always")

        self.assertEqual(self.client.permission_replies, [
            (session_id, "per_4", "reject"),
            (session_id, "per_5", "always"),
        ])

    def test_malformed_permission_arguments_never_reach_the_server(self):
        self.attach_session()

        for argument in ("", "a b c", "per_6 bogus", "per_6 ALWAYS extra"):
            with self.subTest(argument=argument):
                self.run_command("/approve " + argument)

                self.assertEqual(self.last_reply.kind, "error")
                self.assertIn("用法: /approve", self.last_reply.text)

        self.assertEqual(self.client.permission_replies, [])

    def test_replying_without_a_session_says_so_and_sends_nothing(self):
        self.run_command("/approve per_1")

        self.assertEqual(self.client.permission_replies, [])
        self.assertEqual(self.last_reply.kind, "error")
        self.assertIn("当前没有会话", self.last_reply.text)

    def test_control_characters_are_stripped_from_the_argument(self):
        """参数先过 ``_clean`` 再解析，否则带控制字符的请求 id 会被原样发给
        opencode —— 那是下游 agent 的授权凭据，不该由 IM 文本决定。"""
        session_id = self.attach_session()

        self.run_command("/approve per%s7" % NULL_BYTE)

        self.assertEqual(self.client.permission_replies,
                         [(session_id, "per7", "once")])


# ----------------------------------------------------------------------
# 11: 未知命令与失败兜底
# ----------------------------------------------------------------------
class DispatchTests(HandlerTestCase):
    def test_an_unknown_command_reports_the_name_and_the_help_hint(self):
        self.run_command("/xyz foo bar")

        self.assertEqual(self.last_reply.kind, "error")
        self.assertEqual(self.last_reply.text,
                         "未知命令 xyz，发送 /help 查看用法。")
        self.assertEqual(self.client.get_session_calls, [])
        self.assertEqual(self.ensure_session.calls, [])

    def test_argument_underscores_are_stripped_only_from_the_command_name(self):
        """``/help@bot`` 要认得出，参数里的 ``@`` 必须留着。"""
        self.run_command("/approve per_7 a@b")

        self.assertEqual(self.last_reply.kind, "error")
        self.assertIn("用法: /approve", self.last_reply.text)

    def test_an_unexpected_error_inside_a_command_still_replies(self):
        """命令抛异常绝不能冒到入站线程 —— 用户要看到一句话，不是沉默。"""
        self.model_command.reply_for = _explode  # type: ignore[assignment]

        self.run_command("/model")

        self.assertEqual(self.last_reply.kind, "error")
        self.assertIn("命令执行失败", self.last_reply.text)


def _explode(conversation_id: str, args: str) -> CommandReply:
    raise RuntimeError("目录服务炸了")


# ----------------------------------------------------------------------
# 12: core 侧那一层转发（唯一的集成断言）
# ----------------------------------------------------------------------
class BridgeCoreWiringTests(unittest.TestCase):
    def test_core_builds_a_command_handler_and_still_routes_commands(self):
        with tempfile.TemporaryDirectory() as tempdir:
            core, _client, adapter, _state, _path, _config = make_env(tempdir)

            self.assertIsInstance(core.commands, CommandHandler)
            core.on_inbound(inbound(CONVERSATION, "/help"))

            self.assertEqual(adapter.sent[-1].text, HELP_TEXT)

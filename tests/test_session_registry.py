"""``SessionRegistry`` 的**独立**测试 —— 全程不构造 :class:`BridgeCore`。

这一整个文件存在的理由是 AGENTS.md §5.1 说的"抽出去"那种拆法：新类必须能脱离
原类单独测。所以这里没有 ``BridgeCore``；六个依赖里三个是真的（``Config``、
``StateStore``、``ConversationState``），两个是共用的状态（锁与 turn 表），一个是
路由协作者（本文件现造的替身）。

因此这里能断言 core 层面**看不见**的东西：工作目录**必须**解析成绝对路径才发给
opencode（相对路径一律 500）、400 时退回不带权限重试一次、标题截断、以及删会话
时连带把那个 turn 从共用的表里弹掉。
"""

from __future__ import annotations

import inspect
import os
import tempfile
import threading
import unittest

from opencode_bridge.config import Config
from opencode_bridge.conversation_keys import ConversationState
from opencode_bridge.core import BridgeCore
from opencode_bridge.event_stream import Turn
from opencode_bridge.hooks import MsgHandle
from opencode_bridge.opencode_client import OpenCodeError
from opencode_bridge.session_registry import (
    SESSION_TITLE_MAX,
    SESSION_TITLE_PREFIX,
    SessionRegistry,
    ruleset_for,
)
from opencode_bridge.state import StateStore

CONVERSATION = "chat:55"
PLATFORM = "telegram"


class RecordingClient:
    """只记下 ``create_session`` 的参数 —— 那几个参数就是这一层的全部契约。"""

    def __init__(self) -> None:
        self.created: list[dict] = []
        self.deleted: list[str] = []
        self.create_errors: list[Exception] = []
        self.delete_errors: list[Exception] = []

    def create_session(self, *, directory, title, agent, permissions):
        self.created.append({"directory": directory, "title": title,
                             "agent": agent, "permissions": permissions})
        if self.create_errors:
            raise self.create_errors.pop(0)
        return "ses_%04d" % len(self.created)

    def delete_session(self, session_id: str) -> None:
        self.deleted.append(session_id)
        if self.delete_errors:
            raise self.delete_errors.pop(0)

    @property
    def last(self) -> dict:
        return self.created[-1]


# ----------------------------------------------------------------------
# 基类
# ----------------------------------------------------------------------
class SessionRegistryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.store = StateStore(os.path.join(self.tempdir.name, "state.json"))
        self.conversation_state = ConversationState(self.store, lambda: ())
        self.client = RecordingClient()
        self.config = Config(opencode_directory=os.path.join(
            self.tempdir.name, "work"))
        self.lock = threading.RLock()
        self.turns: dict[str, Turn] = {}
        self.asked: list[str] = []
        #: 「那一轮被丢弃了」的记录：``(conversation_id, handle, session_id)``。
        self.cancellations: list[tuple[str, MsgHandle | None, str]] = []
        self.cancel_error: Exception | None = None
        self.registry = self.build()

    def _record_cancelled_turn(
        self, conversation_id: str, handle: MsgHandle | None, session_id: str,
    ) -> None:
        if self.cancel_error is not None:
            raise self.cancel_error
        self.cancellations.append((conversation_id, handle, session_id))

    def build(self, **overrides) -> SessionRegistry:
        kwargs = {
            "client": self.client,
            "config": self.config,
            "conversation_state": self.conversation_state,
            "lock": self.lock,
            "turns": self.turns,
            "asking_platform": self._asking_platform,
            "cancel_turn": self._record_cancelled_turn,
        }
        kwargs.update(overrides)
        self.registry = SessionRegistry(**kwargs)
        return self.registry

    def _asking_platform(self, conversation_id: str) -> str:
        self.asked.append(conversation_id)
        return PLATFORM

    def remember(self, conversation_id: str, platform: str) -> None:
        """种下一条准确映射（等价于入站时 ``remember_platform`` 做过的那一步）。"""
        self.store.set_meta("%s|platform" % conversation_id, "", platform)


# ----------------------------------------------------------------------
# 1: 依赖面与共有状态
# ----------------------------------------------------------------------
class CollaboratorSurfaceTests(SessionRegistryTestCase):
    def test_the_constructor_takes_the_seven_injected_dependencies(self):
        parameters = inspect.signature(SessionRegistry.__init__).parameters
        self.assertEqual(
            [name for name in parameters if name != "self"],
            ["client", "config", "conversation_state", "lock", "turns",
             "asking_platform", "cancel_turn"],
        )
        for name, parameter in parameters.items():
            if name == "self":
                continue
            self.assertEqual(parameter.kind, inspect.Parameter.KEYWORD_ONLY, name)
            self.assertIs(parameter.default, inspect.Parameter.empty, name)

    def test_no_bridge_core_is_reachable_from_the_registry(self):
        for name, value in vars(self.registry).items():
            self.assertNotIsInstance(value, BridgeCore, name)

    def test_the_shared_state_is_the_injected_object(self):
        """锁与 turn 表注入的是**同一个对象** —— 复制一份就等于把互斥关系弄丢了。"""
        self.assertIs(self.registry._lock, self.lock)
        self.assertIs(self.registry._turns, self.turns)


# ----------------------------------------------------------------------
# 2: ruleset_for（跟着唯一调用者一起搬过来的纯函数）
# ----------------------------------------------------------------------
class RulesetTests(unittest.TestCase):
    def test_each_mode_maps_to_its_own_rule_set(self):
        self.assertEqual(ruleset_for("allow"),
                         [{"action": "*", "resource": "*", "effect": "allow"}])
        self.assertEqual(ruleset_for("deny"),
                         [{"action": "*", "resource": "*", "effect": "deny"}])

    def test_ask_and_anything_unknown_mean_the_server_default(self):
        for mode in ("ask", "whatever", None, ""):
            with self.subTest(mode=mode):
                self.assertIsNone(ruleset_for(mode))

    def test_an_unknown_mode_is_logged_rather_than_silently_defaulted(self):
        with self.assertLogs("opencode_bridge.session_registry", level="WARNING"):
            self.assertIsNone(ruleset_for("whatever"))


# ----------------------------------------------------------------------
# 3: ensure_session
# ----------------------------------------------------------------------
class EnsureSessionTests(SessionRegistryTestCase):
    def test_a_known_conversation_is_returned_without_creating(self):
        self.conversation_state.set_session(CONVERSATION, "ses_existing")

        self.assertEqual(self.registry.ensure_session(CONVERSATION),
                         "ses_existing")
        self.assertEqual(self.client.created, [])

    def test_a_new_conversation_creates_one_and_records_it(self):
        session_id = self.registry.ensure_session(CONVERSATION)

        self.assertEqual(session_id, "ses_0001")
        self.assertEqual(
            self.conversation_state.get_session(CONVERSATION, platform=PLATFORM),
            "ses_0001")

    def test_the_directory_is_sent_as_an_absolute_path(self):
        """⚠️ opencode 对相对 ``directory`` 一律 500 且响应体为空 —— 所以这里必须
        解析绝对路径，而不是要求用户自己配。"""
        self.config.opencode_directory = "."

        self.registry.ensure_session(CONVERSATION)

        self.assertEqual(self.client.last["directory"],
                         os.path.abspath("."))

    def test_the_conversation_directory_wins_over_the_configured_one(self):
        self.conversation_state.set_meta(CONVERSATION, "directory",
                                         os.path.join(self.tempdir.name, "docs"))

        self.registry.ensure_session(CONVERSATION)

        self.assertEqual(self.client.last["directory"],
                         os.path.join(self.tempdir.name, "docs"))

    def test_the_title_is_prefixed_and_truncated(self):
        long_conversation = "chat:" + ("9" * 120)

        self.registry.ensure_session(long_conversation)

        title = self.client.last["title"]
        self.assertTrue(title.startswith(SESSION_TITLE_PREFIX))
        self.assertEqual(len(title), SESSION_TITLE_MAX)

    def test_an_unset_agent_is_sent_as_none(self):
        self.config.opencode_agent = ""

        self.registry.ensure_session(CONVERSATION)

        self.assertIsNone(self.client.last["agent"])

    def test_a_configured_agent_is_sent_through(self):
        self.config.opencode_agent = "build"

        self.registry.ensure_session(CONVERSATION)

        self.assertEqual(self.client.last["agent"], "build")

    def test_an_explicit_platform_beats_the_asking_platform_fallback(self):
        self.registry.ensure_session(CONVERSATION, platform="matrix")

        self.assertEqual(self.asked, [], "显式给了平台就不该再问路由")

    def test_the_fallback_platform_is_asked_for_when_none_is_given(self):
        self.registry.ensure_session(CONVERSATION)

        self.assertEqual(self.asked, [CONVERSATION])

    def test_a_400_about_permissions_is_retried_once_without_them(self):
        """老服务端不认 ``permissions`` 字段 —— 退回不带权限再试一次，而不是
        把"建会话失败"报给用户。"""
        self.config.permissions_mode = "allow"
        self.client.create_errors.append(
            OpenCodeError("unknown field", status=400))

        self.registry.ensure_session(CONVERSATION)

        self.assertEqual(len(self.client.created), 2)
        self.assertEqual(self.client.created[0]["permissions"],
                         [{"action": "*", "resource": "*", "effect": "allow"}])
        self.assertIsNone(self.client.created[1]["permissions"])

    def test_a_400_under_ask_is_not_retried_because_there_were_no_rules(self):
        """``ask`` 根本没发规则 —— 再试一次是白发第二次请求。"""
        self.client.create_errors.append(OpenCodeError("bad request", status=400))

        with self.assertRaises(OpenCodeError):
            self.registry.ensure_session(CONVERSATION)

        self.assertEqual(len(self.client.created), 1)

    def test_a_500_is_raised_to_the_caller(self):
        """建会话失败要变成"创建会话失败: …"给用户，所以这里只负责上抛。"""
        self.client.create_errors.append(OpenCodeError("no capacity", status=500))

        with self.assertRaises(OpenCodeError):
            self.registry.ensure_session(CONVERSATION)


# ----------------------------------------------------------------------
# 4: platform 参数转发（歧义旧键的归属只能靠它判定）
# ----------------------------------------------------------------------
class RecordingConversationState:
    """只记下每次调用收到的 ``platform`` —— 那正是这个参数的**全部**作用。

    用真 :class:`ConversationState` 测不出转发：单会话、且没有 ``channel:``
    旧键时，两条路径读到的键完全一样。
    """

    def __init__(self, session_id: str | None = None) -> None:
        self.session_id = session_id
        self.platforms_seen: list[tuple[str, str]] = []

    def get_session(self, conversation_id: str, *, platform: str):
        self.platforms_seen.append(("get_session", platform))
        return self.session_id

    def get_meta(self, conversation_id, key, default=None, *, platform):
        self.platforms_seen.append(("get_meta", platform))
        return default

    def set_session(self, conversation_id: str, session_id: str) -> None:
        self.sessions_written = session_id

    def drop_session(self, conversation_id: str, *, platform: str) -> None:
        self.platforms_seen.append(("drop_session", platform))


class PlatformForwardingTests(SessionRegistryTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.recorder = RecordingConversationState(session_id="ses_known")
        self.registry = self.build(conversation_state=self.recorder)

    def test_ensure_session_forwards_an_explicit_platform(self):
        self.registry.ensure_session(CONVERSATION, platform="matrix")

        self.assertEqual(self.recorder.platforms_seen,
                         [("get_session", "matrix")])
        self.assertEqual(self.asked, [], "显式给了平台就不该再问路由")

    def test_ensure_session_falls_back_to_the_asking_platform(self):
        self.registry.ensure_session(CONVERSATION)

        self.assertEqual(self.recorder.platforms_seen, [("get_session", PLATFORM)])
        self.assertEqual(self.asked, [CONVERSATION])

    def test_drop_session_forwards_an_explicit_platform(self):
        self.registry.drop_session(CONVERSATION, platform="matrix")

        self.assertIn(("get_session", "matrix"), self.recorder.platforms_seen)
        self.assertIn(("drop_session", "matrix"), self.recorder.platforms_seen)

    def test_drop_session_falls_back_to_the_asking_platform(self):
        self.registry.drop_session(CONVERSATION)

        self.assertIn(("drop_session", PLATFORM), self.recorder.platforms_seen)
        self.assertEqual(self.asked, [CONVERSATION])


# ----------------------------------------------------------------------
# 5: drop_session
# ----------------------------------------------------------------------
class DropSessionTests(SessionRegistryTestCase):
    def seed_session(self, session_id: str = "ses_0001") -> str:
        self.conversation_state.set_session(CONVERSATION, session_id)
        return session_id

    def test_dropping_deletes_on_both_sides(self):
        session_id = self.seed_session()

        self.assertEqual(self.registry.drop_session(CONVERSATION), session_id)

        self.assertEqual(self.client.deleted, [session_id])
        self.assertIsNone(self.conversation_state.get_session(
            CONVERSATION, platform=PLATFORM))

    def test_dropping_also_evicts_the_turn_from_the_shared_table(self):
        """turn 表是三个协作者共用的：会话没了还留着 turn，下一轮会认错归属。"""
        session_id = self.seed_session()
        self.turns[session_id] = Turn(conversation_id=CONVERSATION)

        self.registry.drop_session(CONVERSATION)

        self.assertNotIn(session_id, self.turns)

    def test_a_server_failure_still_drops_it_locally(self):
        session_id = self.seed_session()
        self.client.delete_errors.append(OpenCodeError("nope", status=500))

        with self.assertLogs("opencode_bridge.session_registry", level="WARNING"):
            self.registry.drop_session(CONVERSATION)

        self.assertIsNone(self.conversation_state.get_session(
            CONVERSATION, platform=PLATFORM))
        self.assertNotIn(session_id, self.turns)

    def test_a_conversation_never_seen_costs_nothing(self):
        self.assertIsNone(self.registry.drop_session("brand:new"))

        self.assertEqual(self.client.deleted, [])
        self.assertEqual(self.asked, ["brand:new"])


# ----------------------------------------------------------------------
# 6: 被丢弃的那一轮必须告诉读者（``ora-15`` 确诊的缺陷）
#
# ⛔ 这些用例**不**碰文案：文案的判据是"平台那两类各钉一条"，那是
# ``tests/test_outbound.py`` 的职责。这里钉的是**契约**：有没有在跑的东西被丢掉，
# 决定要不要说那句话，以及**说什么**（那个占位消息的句柄）。
# ----------------------------------------------------------------------
class CancelledTurnAnnouncementTests(SessionRegistryTestCase):
    def seed_running_turn(
        self, session_id: str = "ses_0001",
        handle: MsgHandle | None = None,
    ) -> str:
        """种下一条「模型正在回答」的状态：会话已登记、turn 在跑、占位消息已发。"""
        self.conversation_state.set_session(CONVERSATION, session_id)
        self.turns[session_id] = Turn(
            conversation_id=CONVERSATION, progress_handle=handle,
        )
        return session_id

    def test_a_running_turn_is_reported_with_its_placeholder_handle(self):
        """⛔ 缺陷形态下这一条是红的：弹掉 turn 时一句话都不说，读者只看到
        **一个永远卡在「⏳ 处理中…」的气泡** 加一句「已新建会话 xxx」。"""
        handle = MsgHandle(CONVERSATION, "m42", PLATFORM)
        session_id = self.seed_running_turn(handle=handle)

        self.registry.drop_session(CONVERSATION)

        self.assertEqual(
            self.cancellations, [(CONVERSATION, handle, session_id)],
            "丢掉在跑的一轮却没有告诉读者 —— 占位消息会变成僵尸气泡",
        )

    def test_a_run_with_no_placeholder_message_is_still_reported(self):
        """占位消息**发失败**时句柄是 ``None``，而那一轮照样在跑 ⇒ 照样要说。"""
        session_id = self.seed_running_turn(handle=None)

        self.registry.drop_session(CONVERSATION)

        self.assertEqual(self.cancellations, [(CONVERSATION, None, session_id)])

    def test_nothing_is_said_when_the_user_was_running_nothing(self):
        """⚠️ 用户没在跑任何东西时发 ``/new`` —— 多出来的任何一句都是噪音。"""
        self.seed_session_for_nothing_running()

        self.registry.drop_session(CONVERSATION)

        self.assertEqual(self.cancellations, [])

    def seed_session_for_nothing_running(self) -> str:
        """只有一条登记好的会话，**没有** turn。"""
        self.conversation_state.set_session(CONVERSATION, "ses_idle")
        return "ses_idle"

    def test_a_conversation_with_no_session_says_nothing_either(self):
        """连会话都没有 ⇒ 没有 turn 可丢 ⇒ 不该有任何一句话（也省掉那次 ``ask``）。"""
        self.registry.drop_session("brand:new")

        self.assertEqual(self.cancellations, [])

    def test_the_report_is_not_raised_as_a_failure_of_new_itself(self):
        """⛔ 收尾通道坏了不该让 ``/new`` 本身失败 —— 调用方拿到的仍是那个 id。"""
        session_id = self.seed_running_turn()
        self.cancel_error = RuntimeError("the IM platform is on fire")

        with self.assertLogs("opencode_bridge.session_registry", level="ERROR"):
            returned = self.registry.drop_session(CONVERSATION)

        self.assertEqual(returned, session_id)
        self.assertNotIn(session_id, self.turns,
                         "收尾失败也不该把 turn 留在表里 —— 会话已经没了")

    def test_the_report_happens_after_the_turn_leaves_the_shared_table(self):
        """⚠️ 顺序：先弹掉、后说话。反过来会让收尾那一侧以为轮还在跑，
        而它读的是**同一个** dict。"""
        handle = MsgHandle(CONVERSATION, "m7", PLATFORM)
        session_id = self.seed_running_turn(handle=handle)
        seen_turn_present_during_report: list[bool] = []

        def watching_report(
            conversation_id: str, handle_arg: MsgHandle | None, dropped: str,
        ) -> None:
            seen_turn_present_during_report.append(dropped in self.turns)
            self.cancellations.append((conversation_id, handle_arg, dropped))

        self.registry = self.build(cancel_turn=watching_report)

        self.registry.drop_session(CONVERSATION)

        self.assertEqual(seen_turn_present_during_report, [False],
                         "报告时那一轮仍在共享表里")


if __name__ == "__main__":
    unittest.main()

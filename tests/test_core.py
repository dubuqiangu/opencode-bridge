"""Lane C tests — ``BridgeCore`` + CLI (fully offline, no network).

``FakeClient`` mirrors the ``OpenCodeClient`` surface with recorded calls and
controllable errors; ``FakeAdapter`` records ``send`` / ``edit`` / ``answer``.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from opencode_bridge.adapters.base import Adapter
from opencode_bridge import __main__ as cli
from opencode_bridge.channel_profile import _HINT_SEPARATOR
from opencode_bridge.config import Config
from opencode_bridge.adapters.base import build, registered_names
from opencode_bridge.core import (
    DEFAULT_EDIT_INTERVAL,
    DEFAULT_MAX_MESSAGE_CHARS,
    HELP_TEXT,
    NO_OUTPUT_TEXT,
    BridgeCore,
    ruleset_for,
)
from opencode_bridge.hooks import Inbound, MsgHandle, Outbound
from opencode_bridge.opencode_client import Endpoint, OpenCodeError
from opencode_bridge.outbound import OutboundSender, one_message_budget
from opencode_bridge.state import StateStore
from tests.test_outbound import CONVERSATION, ScriptedAdapter

#: ``config.bridge`` 的完整默认值。**逐个键列出**，而不是从生产代码 import ——
#: 那道断言的全部意义就是"新增一个键必须有人在这里看见并决定它的默认值"。
DEFAULT_BRIDGE = {
    "edit_interval_seconds": 1.5,
    "max_message_chars": 4000,
    # C3：长输入回执门槛（180，抄自 dsh 的 longInputAckChars）
    "long_input_ack_chars": 180,
    # C3：续行保险丝秒数（15）—— 不是合并窗口，普通消息不碰它
    "merge_continue_timeout_seconds": 15.0,
}


# ----------------------------------------------------------------------
# fakes
# ----------------------------------------------------------------------
class FakeClient:
    """Offline stand-in for :class:`OpenCodeClient`."""

    def __init__(self) -> None:
        self.create_attempts: list[dict] = []
        self.created_ids: list[str] = []
        self.deleted: list[str] = []
        self.interrupted: list[str] = []
        self.get_session_calls: list[str] = []
        self.permission_replies: list[tuple] = []
        self.prompts: list[tuple[str, str]] = []
        self.create_errors: list[Exception] = []
        self.prompt_errors: list[Exception] = []
        self.permission_errors: list[Exception] = []
        self.delete_errors: list[Exception] = []
        self.interrupt_errors: list[Exception] = []
        self.get_session_error: Exception | None = None
        self.session_info: dict = {
            "agent": "builder",
            "model": {"providerID": "prov", "id": "model-x"},
            "cost": 0.25,
            "tokens": {"input": 10, "output": 20},
            "location": {"directory": "D:\\work"},
        }
        #: ``GET /api/model`` 的返回。``/model`` 命令的搜索与校验都靠它。
        self.model_catalog: list[dict] = [
            {"providerID": "prov", "id": "model-x", "name": "Model X"},
            {"providerID": "opencode", "id": "space-bunny-free",
             "name": "Space Bunny Free"},
            {"providerID": "opencode", "id": "space-bunny", "name": "Space Bunny"},
            {"providerID": "anthropic", "id": "claude-sonnet-4-5",
             "name": "Claude Sonnet 4.5"},
        ]
        self.list_models_calls: list[int] = []
        self.model_switches: list[tuple[str, str, str]] = []
        self.closed = False
        self._seq = 0
        self._events: list[dict] = []
        self._wake = threading.Event()

    # --- basics -------------------------------------------------------
    def info(self) -> dict:
        return {"version": "test", "pid": 1}

    # --- session ------------------------------------------------------
    def create_session(
        self,
        *,
        directory: str,
        title: str | None = None,
        agent: str | None = None,
        permissions: list[dict] | None = None,
    ) -> str:
        attempt = {
            "directory": directory,
            "title": title,
            "agent": agent,
            "permissions": permissions,
        }
        self.create_attempts.append(attempt)
        if self.create_errors:
            raise self.create_errors.pop(0)
        self._seq += 1
        session_id = f"ses_fake{self._seq:04d}"
        self.created_ids.append(session_id)
        return session_id

    def get_session(self, session_id: str) -> dict:
        self.get_session_calls.append(session_id)
        if self.get_session_error is not None:
            raise self.get_session_error
        info = dict(self.session_info)
        info.setdefault("id", session_id)
        return info

    def delete_session(self, session_id: str) -> None:
        if self.delete_errors:
            raise self.delete_errors.pop(0)
        self.deleted.append(session_id)

    # --- models -------------------------------------------------------
    def list_models(self) -> list[dict]:
        self.list_models_calls.append(1)
        return [dict(entry) for entry in self.model_catalog]

    def set_session_model(
        self, session_id: str, provider_id: str, model_id: str
    ) -> None:
        # 先记后抛：测试要断言的是"这个端点有没有被调用过"。
        self.model_switches.append((session_id, provider_id, model_id))

    # --- conversation -------------------------------------------------
    def prompt(self, session_id: str, text: str, *, resume: bool = True) -> str:
        self.prompts.append((session_id, text))
        if self.prompt_errors:
            raise self.prompt_errors.pop(0)
        return "msg_fake"

    def interrupt(self, session_id: str) -> None:
        if self.interrupt_errors:
            raise self.interrupt_errors.pop(0)
        self.interrupted.append(session_id)

    def reply_permission(
        self,
        session_id: str,
        request_id: str,
        decision: str,
        *,
        message: str | None = None,
    ) -> None:
        if decision not in ("once", "always", "reject"):
            raise ValueError(f"invalid decision {decision!r}")
        if self.permission_errors:
            raise self.permission_errors.pop(0)
        self.permission_replies.append((session_id, request_id, decision))

    # --- events -------------------------------------------------------
    def subscribe(self, *, restart: bool = True):
        while not self.closed:
            self._wake.wait(0.05)
            self._wake.clear()
            while self._events:
                yield self._events.pop(0)

    def push(self, event: dict) -> None:
        self._events.append(event)
        self._wake.set()

    def close(self) -> None:
        self.closed = True
        self._wake.set()


class FakeAdapter(Adapter):
    """Offline stand-in for a messaging adapter.

    ⚠️ 声明 ``supports_message_edit = True``：它**就是**一个能改写已发消息的
    平台（:meth:`edit` 默认返回 ``True``，与 Telegram / Slack 同形）。出站那道
    「平台清不掉就不发占位消息」的闸门读的是**这个声明**，不读 ``edit()`` 的返回值，
    所以这里必须声明 —— 否则整套流式/收尾用例会误以为在测一个 IRC。
    """

    name = "fake"
    supports_message_edit = True

    def __init__(self) -> None:
        super().__init__({}, hooks=None)  # type: ignore[arg-type]
        self.sent: list[Outbound] = []
        self.edited: list[tuple[MsgHandle, Outbound]] = []
        self.answers: list[tuple[str, str]] = []
        self.edit_results: list = []  # bool | Exception, consumed in order
        self.started = False
        self.stopped = False
        self._handles = 0

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        super().stop()
        self.stopped = True

    def send(self, out: Outbound) -> MsgHandle | None:
        self.sent.append(out)
        self._handles += 1
        return MsgHandle(
            conversation_id=out.conversation_id,
            message_id=f"m{self._handles}",
            platform=self.name,
        )

    def edit(self, handle: MsgHandle, out: Outbound) -> bool:
        self.edited.append((handle, out))
        if self.edit_results:
            result = self.edit_results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return bool(result)
        return True

    def answer(self, query_id: str, text: str = "") -> None:
        self.answers.append((query_id, text))


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def make_env(td: str, *, mode: str = "ask", bridge: dict | None = None):
    cfg = Config(permissions_mode=mode)
    if bridge is not None:
        cfg.bridge = dict(bridge)
    path = os.path.join(td, "state.json")
    state = StateStore(path)
    client = FakeClient()
    adapter = FakeAdapter()
    core = BridgeCore(cfg, client, state)
    core.attach(adapter)
    return core, client, adapter, state, path, cfg


def prompt_bodies(prompts: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """``(session_id, 用户正文)`` —— 剥掉 C1 拼在正文前面的渠道说明。

    本文件断言的是**路由与会话生命周期**，不是渠道说明；说明由
    ``tests/test_channel_profile.py`` 单独断言。剥掉之后剩下的那段仍然必须
    **逐字节**等于用户敲的字，所以"用户原文没有被改写"这条不变量照样被守住。
    """
    separator = _HINT_SEPARATOR + "\n"
    return [
        (session_id, text.split(separator, 1)[-1])
        for session_id, text in prompts
    ]


def inbound(cid: str, text: str, kind: str = "text") -> Inbound:
    return Inbound(conversation_id=cid, text=text, kind=kind, platform="telegram")


def ev(etype: str, **data) -> dict:
    return {"type": etype, "data": data}


# ----------------------------------------------------------------------
# 1-2: session lifecycle
# ----------------------------------------------------------------------
class SessionLifecycleTests(unittest.TestCase):
    def test_first_message_creates_session_and_persists_across_restart(self):
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, state, path, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "hello"))

            self.assertEqual(len(client.create_attempts), 1)
            attempt = client.create_attempts[0]
            # A4 实测：opencode 对相对路径（含默认的 "."）一律 500 且响应体为空，
            # 所以 core 必须先解析成绝对路径。见 SessionDirectoryIsAbsoluteTests。
            self.assertTrue(
                os.path.isabs(attempt["directory"]),
                "会话目录必须是绝对路径，否则真实服务端返回 500：%r"
                % (attempt["directory"],),
            )
            self.assertTrue(attempt["title"].startswith("tg-bridge:chat:55"))
            self.assertLessEqual(len(attempt["title"]), 60)
            self.assertIsNone(attempt["permissions"])  # permissions_mode=ask
            sid = client.created_ids[0]
            self.assertEqual(state.get_session("chat:55"), sid)
            self.assertEqual(prompt_bodies(client.prompts), [(sid, "hello")])

            # "restart": fresh state/client must reuse the stored session
            state2 = StateStore(path)
            client2 = FakeClient()
            adapter2 = FakeAdapter()
            core2 = BridgeCore(Config(), client2, state2)
            core2.attach(adapter2)
            core2.on_inbound(inbound("chat:55", "again"))

            self.assertEqual(client2.create_attempts, [])
            self.assertEqual(prompt_bodies(client2.prompts), [(sid, "again")])

    def test_new_command_deletes_and_recreates_session(self):
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, state, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "hello"))
            old_sid = client.created_ids[0]
            core.on_inbound(inbound("chat:55", "/new"))

            self.assertEqual(client.deleted, [old_sid])
            self.assertEqual(len(client.created_ids), 2)
            new_sid = client.created_ids[1]
            self.assertNotEqual(old_sid, new_sid)
            self.assertEqual(state.get_session("chat:55"), new_sid)
            self.assertIn(f"已新建会话 {new_sid[:12]}", adapter.sent[-1].text)

            core.on_inbound(inbound("chat:55", "next"))
            self.assertEqual(client.prompts[-1][0], new_sid)

    def test_create_session_400_falls_back_to_no_permissions(self):
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, state, _, _ = make_env(td, mode="allow")
            client.create_errors.append(OpenCodeError("bad", status=400))
            core.on_inbound(inbound("chat:55", "hi"))

            self.assertEqual(len(client.create_attempts), 2)
            self.assertEqual(
                client.create_attempts[0]["permissions"],
                [{"action": "*", "resource": "*", "effect": "allow"}],
            )
            self.assertIsNone(client.create_attempts[1]["permissions"])
            self.assertEqual(len(client.prompts), 1)

    def test_create_session_failure_sends_error_not_prompt(self):
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, state, _, _ = make_env(td)
            client.create_errors.append(OpenCodeError("boom", status=500))
            core.on_inbound(inbound("chat:55", "hi"))

            self.assertEqual(client.prompts, [])
            self.assertEqual(adapter.sent[-1].kind, "error")
            self.assertIn("创建会话失败", adapter.sent[-1].text)


# ----------------------------------------------------------------------
# 3-4: commands
# ----------------------------------------------------------------------
class CommandTests(unittest.TestCase):
    def test_help_routes_to_usage_without_prompt(self):
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "/help"))
            self.assertEqual(adapter.sent[-1].text, HELP_TEXT)
            self.assertEqual(client.prompts, [])
            self.assertEqual(client.create_attempts, [])

    def test_status_routes_to_get_session_without_prompt(self):
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "hello"))
            sid = client.created_ids[0]
            prompts_before = len(client.prompts)

            core.on_inbound(inbound("chat:55", "/status"))
            text = adapter.sent[-1].text
            self.assertEqual(client.get_session_calls, [sid])
            self.assertIn(f"session_id: {sid}", text)
            self.assertIn("agent: builder", text)
            self.assertIn("model: prov/model-x", text)
            self.assertIn("cost: 0.25", text)
            self.assertIn("tokens: input=10 output=20", text)
            self.assertIn("directory: D:\\work", text)
            self.assertEqual(len(client.prompts), prompts_before)

    def test_cd_switches_directory_and_recreates_session(self):
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, state, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "hello"))
            old_sid = client.created_ids[0]
            prompts_before = len(client.prompts)

            core.on_inbound(inbound("chat:55", "/cd docs"))
            self.assertEqual(state.get_meta("chat:55", "directory"), "docs")
            self.assertEqual(client.deleted, [old_sid])
            self.assertEqual(len(client.created_ids), 2)
            # A4 实测：opencode 对相对路径一律 500，所以这里发出去的必须是解析后的绝对路径。
            # 注意与上一行对照 —— state 里存的仍是用户输入的原样 "docs"，
            # 解析只发生在发请求的那一刻。这个分离是有意的：
            # state 保留原样，`/cd` 的回显和后续相对解析才有意义。
            switched_directory = client.create_attempts[1]["directory"]
            self.assertTrue(
                os.path.isabs(switched_directory),
                "/cd 后的会话目录也必须是绝对路径，否则真实服务端返回 500：%r"
                % (switched_directory,),
            )
            self.assertEqual(os.path.basename(switched_directory), "docs")
            self.assertEqual(os.path.dirname(switched_directory), os.getcwd())
            self.assertEqual(adapter.sent[-1].text, "已切换到 docs")
            self.assertEqual(len(client.prompts), prompts_before)

            core.on_inbound(inbound("chat:55", "/cd"))
            self.assertIn("用法: /cd", adapter.sent[-1].text)

    def test_stop_routes_to_interrupt_without_prompt(self):
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "hello"))
            sid = client.created_ids[0]
            prompts_before = len(client.prompts)

            core.on_inbound(inbound("chat:55", "/stop"))
            self.assertEqual(client.interrupted, [sid])
            self.assertEqual(len(client.prompts), prompts_before)

    def test_unknown_command_never_prompts(self):
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "/xyz foo"))
            self.assertEqual(client.prompts, [])
            self.assertEqual(client.create_attempts, [])
            self.assertEqual(adapter.sent[-1].kind, "error")
            self.assertIn("/help", adapter.sent[-1].text)

    def test_command_with_bot_suffix_is_recognised(self):
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "/help@your_bot"))
            self.assertEqual(adapter.sent[-1].text, HELP_TEXT)
            self.assertEqual(client.prompts, [])

    def test_approve_and_deny_route_to_reply_permission(self):
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "hello"))
            sid = client.created_ids[0]

            core.on_inbound(inbound("chat:55", "/approve per_1"))
            self.assertEqual(
                client.permission_replies[-1], (sid, "per_1", "once")
            )
            core.on_inbound(inbound("chat:55", "/approve per_2 always"))
            self.assertEqual(
                client.permission_replies[-1], (sid, "per_2", "always")
            )
            core.on_inbound(inbound("chat:55", "/deny per_3"))
            self.assertEqual(
                client.permission_replies[-1], (sid, "per_3", "reject")
            )
            core.on_inbound(inbound("chat:55", "/approve"))
            self.assertIn("用法: /approve", adapter.sent[-1].text)
            self.assertEqual(prompt_bodies(client.prompts), [(sid, "hello")])


# ----------------------------------------------------------------------
# C4: 审批路径的端到端不变量（真装配，真事件流，真命令分发）
# ----------------------------------------------------------------------
# 全部经 ``core.on_inbound`` / ``core.event_stream.dispatch`` /
# ``core.on_callback`` —— 也就是适配器真正会走的那三条路，而不是直接调某个
# 内部辅助函数。只测内部辅助函数的话，"这一块根本没接线"照样全绿。
class PermissionSafetyTests(unittest.TestCase):
    """C4 的断言：随口一句永远不是批准；一个请求只被回答一次。"""

    def ask_permission(self, core, session_id, request_id, action="bash"):
        """把一个 ``permission.asked`` 真事件推进事件流（渲染 + 记账）。"""
        core.event_stream.dispatch(ev(
            "permission.asked", sessionID=session_id, id=request_id,
            action=action, resources=["/etc/shadow"],
        ))

    def test_an_ordinary_message_while_a_request_is_pending_is_never_an_approval(self):
        """**决定性的反面用例**：请求挂着的时候随口一句，不许变成批准。

        这一条是 C4 前提的正面回答。实测结论是：**今天已经成立** —— 一条普通消息
        走的是 :meth:`~opencode_bridge.inbound_gateway.InboundGateway.on_inbound`
        的 else 分支，被当成 prompt 发给 agent，压根到不了命令表。所以这里不是
        "加了个保护"，而是把这条性质**钉住**：哪天有人加个"用户似乎同意了"之类的
        启发式，这条会立刻红。

        断言的是调用列表为空，而不只是"用户没报错" —— 恒真的断言比没有断言更危险。
        """
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "看一下 README"))
            session_id = client.created_ids[0]
            self.ask_permission(core, session_id, "per_pending")

            casual_replies = [
                "是", "好", "行", "可以", "ok", "yes", "y", "sure",
                "go ahead", "do it", "你看着办", "没问题",
                "确认", "同意", "批准", "allow", "approve",
                "per_pending", "once",
            ]
            for text in casual_replies:
                with self.subTest(text=text):
                    core.on_inbound(inbound("chat:55", text, ))
                    self.assertEqual(
                        client.permission_replies, [],
                        "%r 被当成了批准" % text,
                    )

    def test_a_casual_reply_is_delivered_to_the_agent_instead_of_swallowed(self):
        """与上面配对：随口一句**必须**到达 agent（当普通对话）。

        只断言"没变成批准"不够 —— 万一实现是把它丢了，那也满足前一条而用户什么
        都看不到。所以这里断言它进了 ``prompt()``。
        """
        with tempfile.TemporaryDirectory() as td:
            core, client, _, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "好"))
            session_id = client.created_ids[0]
            self.ask_permission(core, session_id, "per_pending")

            core.on_inbound(inbound("chat:55", "好"))

            self.assertEqual(client.permission_replies, [])
            self.assertEqual(prompt_bodies(client.prompts),
                             [(session_id, "好"), (session_id, "好")])

    def test_the_decision_word_without_the_slash_is_not_a_command(self):
        """``approve per_1``（没有斜杠）不是命令 —— 斜杠是唯一入口。"""
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "hello"))
            session_id = client.created_ids[0]
            self.ask_permission(core, session_id, "per_1")

            core.on_inbound(inbound("chat:55", "approve per_1"))

            self.assertEqual(client.permission_replies, [])
            self.assertEqual(prompt_bodies(client.prompts),
                             [(session_id, "hello"),
                              (session_id, "approve per_1")])

    def test_a_second_answer_to_one_request_is_refused_end_to_end(self):
        """真事件流 + 真命令：第二次一个字节都不发，且用户被告知。"""
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "跑一下部署脚本"))
            session_id = client.created_ids[0]
            self.ask_permission(core, session_id, "per_deploy")

            core.on_inbound(inbound("chat:55", "/approve per_deploy"))
            core.on_inbound(inbound("chat:55", "/approve per_deploy always"))

            self.assertEqual(client.permission_replies,
                             [(session_id, "per_deploy", "once")])
            self.assertIn("忽略", adapter.sent[-1].text)

    def test_a_late_answer_after_permission_replied_is_refused_end_to_end(self):
        """服务端说结束了之后的那次回答不算数（这条事件以前被整个丢掉）。"""
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "跑一下部署脚本"))
            session_id = client.created_ids[0]
            self.ask_permission(core, session_id, "per_deploy")

            core.event_stream.dispatch(ev(
                "permission.replied", sessionID=session_id, id="per_deploy",
            ))
            core.on_inbound(inbound("chat:55", "/approve per_deploy always"))

            self.assertEqual(client.permission_replies, [])
            self.assertIn("已经结束", adapter.sent[-1].text)

    def test_the_command_and_the_button_cannot_both_answer_one_request(self):
        """两条回答路径共用**同一个账本** —— 否则去重是半拉子。

        命令先答，按钮后按（或反过来）：第二次都必须被否掉。这条断的是"两边各有
        一份记录"这种最省事也最没用的修法。
        """
        with tempfile.TemporaryDirectory() as td:
            core, client, _, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "跑一下部署脚本"))
            session_id = client.created_ids[0]
            self.ask_permission(core, session_id, "per_deploy")

            core.on_inbound(inbound("chat:55", "/approve per_deploy"))
            core.on_callback("chat:55",
                             "perm:%s:per_deploy:always" % session_id, "Q1")

            self.assertEqual(client.permission_replies,
                             [(session_id, "per_deploy", "once")])

    def test_a_failed_answer_leaves_the_request_answerable_end_to_end(self):
        """一次 5xx 不能把请求锁死 —— 用户重试必须还能发出去。"""
        with tempfile.TemporaryDirectory() as td:
            core, client, _, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "跑一下部署脚本"))
            session_id = client.created_ids[0]
            self.ask_permission(core, session_id, "per_deploy")

            client.permission_errors.append(OpenCodeError("down", status=500))
            core.on_inbound(inbound("chat:55", "/approve per_deploy"))
            core.on_inbound(inbound("chat:55", "/approve per_deploy"))

            self.assertEqual(client.permission_replies,
                             [(session_id, "per_deploy", "once")])

    def test_the_paths_that_already_worked_are_untouched(self):
        """C4 一条都不许改动的行为：三种决策仍然照发，且**每个请求**各一次。

        这条是"没阉掉"的配对半边：去重要是"一个请求一次"，不是"一个会话一次"。
        """
        with tempfile.TemporaryDirectory() as td:
            core, client, _, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "hello"))
            session_id = client.created_ids[0]
            for request_id in ("per_a", "per_b", "per_c"):
                self.ask_permission(core, session_id, request_id)

            core.on_inbound(inbound("chat:55", "/approve per_a"))
            core.on_inbound(inbound("chat:55", "/approve per_b always"))
            core.on_inbound(inbound("chat:55", "/deny per_c"))

            self.assertEqual(client.permission_replies, [
                (session_id, "per_a", "once"),
                (session_id, "per_b", "always"),
                (session_id, "per_c", "reject"),
            ])


# ----------------------------------------------------------------------
# C3: 一次并集 = 一次 agent 运行（真装配、真适配器、真分发）
# ----------------------------------------------------------------------
# ``tests/test_inbound_merge.py`` 测合并器自己，``tests/test_inbound_gateway.py``
# 测接线；这里测的是**三段合起来**之后 agent 到底被驱动了几次 —— 而那才是 C3 要
# 解决的问题本身（一次 4 行的粘贴原本会变成 4 次 ``prompt()``）。
class InboundBurstTests(unittest.TestCase):
    CONVERSATION = "irc:libera:#dev"

    def send(self, core, text: str, message_id: str) -> None:
        core.on_inbound(Inbound(
            conversation_id=self.CONVERSATION,
            text=text,
            kind="text",
            platform="fake",
            message_id=message_id,
        ))

    def test_a_four_line_paste_is_one_agent_run_not_four(self):
        """**决定性的一条**：4 行一次到达 → agent 只跑一次。

        改动之前这里实测是 4 次 ``prompt()``（同一个 opencode session），也就是
        LLM 轮次 ×4、上下文碎裂 —— 那正是 C3 要治的代价。
        """
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            lines = ("第一段..", "第二段..", "第三段..", "第四段")
            for index, line in enumerate(lines, start=1):
                self.send(core, line, "m%d" % index)

            self.assertEqual(len(client.prompts), 1)
            self.assertEqual(len(client.create_attempts), 1,
                             "并集只该建一次会话")
            self.assertEqual(prompt_bodies(client.prompts),
                             [(client.created_ids[0],
                               "第一段\n第二段\n第三段\n第四段")])

    def test_an_ordinary_single_message_still_runs_immediately_and_alone(self):
        """**延迟那条**：一条普通消息的额外延迟必须是 0。

        这里是"可证明的 0"而不是"权衡后接受"：没有标记就不会有任何缓冲，也就不
        会有任何计时器 —— 而计时器是唯一能给一条消息加等待的东西。
        """
        with tempfile.TemporaryDirectory() as td:
            core, client, _, _, _, _ = make_env(td)
            self.send(core, "看一下 README", "m1")

            self.assertEqual(len(client.prompts), 1)
            self.assertEqual(
                core.inbound_gateway._merger.held_conversation_ids(), (),
                "一条没有标记的消息绝不该被缓冲 —— 那就是延迟的来源",
            )
            self.assertEqual(core.inbound_gateway._merger._fuses, {})

    def test_two_ordinary_messages_stay_two_runs(self):
        """不合并：合并只能由用户自己的标记开启，不能由"看起来像一段话"推断。"""
        with tempfile.TemporaryDirectory() as td:
            core, client, _, _, _, _ = make_env(td)
            self.send(core, "第一句", "m1")
            self.send(core, "第二句", "m2")

            self.assertEqual(prompt_bodies(client.prompts), [
                (client.created_ids[0], "第一句"),
                (client.created_ids[0], "第二句"),
            ])

    def test_the_reconstructed_text_carries_no_marker(self):
        """**无标记泄漏**：送进 agent 的正文里不许出现 ``..`` 或 ``!!``。"""
        with tempfile.TemporaryDirectory() as td:
            core, client, _, _, _, _ = make_env(td)
            self.send(core, "先看这个..", "m1")
            self.send(core, "再看那个!!", "m2")

            delivered = prompt_bodies(client.prompts)[0][1]
            self.assertEqual(delivered, "先看这个\n再看那个")
            self.assertNotIn("..", delivered)
            self.assertNotIn("!!", delivered)

    def test_a_slash_command_is_never_held_for_a_continuation(self):
        """⚠️ 命令不进合并窗口（dsh 同一条纪律，``gateway.ts:395-428``）。

        缓存命令等于给 C4 刚堵上的权限路径重新开一条延迟通道。
        """
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            self.send(core, "hello", "m1")
            session_id = client.created_ids[0]

            self.send(core, "/approve per_9", "m2")

            self.assertEqual(client.permission_replies,
                             [(session_id, "per_9", "once")])
            self.assertEqual(
                core.inbound_gateway._merger.held_conversation_ids(), (),
                "命令绝不能留在缓冲里",
            )

    def test_a_held_line_is_acknowledged_so_it_is_never_silently_swallowed(self):
        """⚠️ 一句正好以 ``..`` 结尾的散文会被判成续行标记。这条断言的是
        它**不是静默的** —— 用户立刻看到一句提示。"""
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            self.send(core, "等等..", "m1")

            self.assertEqual(client.prompts, [])
            self.assertIn("..", adapter.sent[-1].text)
            self.assertIn("!!", adapter.sent[-1].text)

    def test_a_long_input_gets_a_receipt_where_nothing_else_would_show(self):
        """不能改写已发消息的平台（IRC / Twitch / ntfy / email / a2a / QQ / HA）
        **根本不发** ``⏳ 处理中…``，所以长输入必须自己回一句。"""
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td, bridge={
                "long_input_ack_chars": 10,
                "merge_continue_timeout_seconds": 15.0,
            })
            adapter.supports_message_edit = False

            self.send(core, "改一下 README 的安装步骤并重新跑一遍测试", "m1")

            self.assertEqual(len(client.prompts), 1)
            receipts = [out.text for out in adapter.sent if "已收到" in out.text]
            self.assertEqual(len(receipts), 1)
            self.assertIn("24", receipts[0])

    def test_a_long_input_gets_no_extra_receipt_where_the_placeholder_shows(self):
        """能改写的那些本来就有占位消息 —— 再加一句只是让一次提问变成三条消息。"""
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td, bridge={
                "long_input_ack_chars": 10,
                "merge_continue_timeout_seconds": 15.0,
            })
            adapter.supports_message_edit = True

            self.send(core, "改一下 README 的安装步骤并重新跑一遍测试", "m1")

            self.assertEqual(len(client.prompts), 1)
            self.assertEqual(
                [out.text for out in adapter.sent if "已收到" in out.text], []
            )


# ----------------------------------------------------------------------
# 入站缩进：粘贴的代码块必须**逐字节**到达 agent
# ----------------------------------------------------------------------
# 这一类只认一件事：**agent 实际收到的那段正文**（``prompt_bodies`` 已经把渠道说明
# 剥掉，剩下的那段仍须逐字节等于用户敲的）。⚠️ 每个样本的**第一行都带缩进** ——
# 首行无缩进的样本会让断言恒真，那正是本项目已经踩过一次的坑。
class InboundIndentationTests(unittest.TestCase):
    CONVERSATION = "chat:55"

    def send(self, core, text: str) -> None:
        core.on_inbound(Inbound(
            conversation_id=self.CONVERSATION,
            text=text,
            kind="text",
            platform="telegram",
            message_id="m1",
        ))

    def delivered(self, core, client) -> list[str]:
        return [body for _, body in prompt_bodies(client.prompts)]

    # --- 缺陷报告里的那两张表，逐字重建 -----------------------------------
    def test_the_reported_python_block_arrives_byte_identical(self):
        sent = "    def f():\n        return 1"
        with tempfile.TemporaryDirectory() as td:
            core, client, _, _, _, _ = make_env(td)
            self.send(core, sent)

            self.assertEqual(self.delivered(core, client), [sent])

    def test_the_reported_block_after_blank_lines_keeps_its_indentation(self):
        """⚠️ 改之前这里丢的是**前导空行 + 首行缩进**两样，而首行缩进才是损坏。"""
        sent = "\n\n    def g():\n        return 2\n\n"
        expected = "    def g():\n        return 2"
        with tempfile.TemporaryDirectory() as td:
            core, client, _, _, _, _ = make_env(td)
            self.send(core, sent)

            self.assertEqual(self.delivered(core, client), [expected])

    def test_a_yaml_block_arrives_byte_identical(self):
        sent = "version: 2\njobs:\n  build:\n    steps:\n      - run: make\n"
        expected = sent.rstrip()
        with tempfile.TemporaryDirectory() as td:
            core, client, _, _, _, _ = make_env(td)
            self.send(core, sent)

            self.assertEqual(self.delivered(core, client), [expected])

    def test_a_nested_list_block_arrives_byte_identical(self):
        sent = "items:\n  - name: a\n    tags: [1, 2]\n  - name: b\n    tags: []"
        with tempfile.TemporaryDirectory() as td:
            core, client, _, _, _, _ = make_env(td)
            self.send(core, sent)

            self.assertEqual(self.delivered(core, client), [sent])

    # --- 损坏的形状本身 ---------------------------------------------------
    def test_no_indentation_error_is_manufactured(self):
        """⚠️ 核心断言：改之前首行被 dedent 而次行没有，于是
        ``def f():`` 后面跟着一个更深的函数体 = ``IndentationError``。"""
        with tempfile.TemporaryDirectory() as td:
            core, client, _, _, _, _ = make_env(td)
            self.send(core, "    def f():\n        return 1")

            first, second = self.delivered(core, client)[0].split("\n")
            self.assertTrue(first.startswith("    "),
                            "首行缩进被吃掉：%r" % first)
            self.assertEqual(len(first) - len(first.lstrip()), 4)
            self.assertEqual(len(second) - len(second.lstrip()), 8)

    def test_the_indent_stack_never_decreases_on_an_indented_first_line(self):
        """逐行检查缩进阶梯：首行有缩进时，函数体必须更深而不是更浅。"""
        sent = "  def outer():\n      def inner():\n          return 1\n\n  return outer"
        with tempfile.TemporaryDirectory() as td:
            core, client, _, _, _, _ = make_env(td)
            self.send(core, sent)

            body = self.delivered(core, client)[0]
            indents = [
                len(line) - len(line.lstrip())
                for line in body.split("\n")
                if line.strip()
            ]
            self.assertEqual(indents, [2, 6, 10, 2])

    # --- 与 C3 合并的交界 -------------------------------------------------
    def test_a_merged_burst_keeps_every_lines_indentation(self):
        """⚠️ strip 在**合并之前**逐行跑，所以合并前每一行的缩进都会被吃掉 ——
        损坏面比单条消息更大（不止第一条消息的第一行）。"""
        with tempfile.TemporaryDirectory() as td:
            core, client, _, _, _, _ = make_env(td)
            for index, line in enumerate(
                ("  def f():..", "      x = 1..", "      return x"), start=1
            ):
                core.on_inbound(Inbound(
                    conversation_id=self.CONVERSATION, text=line, kind="text",
                    platform="telegram", message_id="m%d" % index,
                ))

            self.assertEqual(len(client.prompts), 1,
                             "并集只该跑一次 agent")
            self.assertEqual(
                self.delivered(core, client),
                ["  def f():\n      x = 1\n      return x"],
            )

    def test_failed_command_replies_with_error(self):
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "hello"))
            client.get_session_error = OpenCodeError("down", status=500)
            core.on_inbound(inbound("chat:55", "/status"))
            self.assertEqual(adapter.sent[-1].kind, "error")
            self.assertIn("命令执行失败", adapter.sent[-1].text)


# ----------------------------------------------------------------------
# /setup onboarding command (Lane R)
# ----------------------------------------------------------------------
class SetupCommandTests(unittest.TestCase):
    def test_setup_menu_lists_three_platforms(self):
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "/setup"))

            self.assertEqual(client.prompts, [])
            self.assertEqual(client.create_attempts, [])
            text = adapter.sent[-1].text
            for needle in ("Telegram", "Slack", "Discord", "1)", "2)", "3)"):
                self.assertIn(needle, text)
            self.assertIn("/setup 1", text)  # pure-text numbered fallback

            # buttons ride a follow-up edit (adapter.send has no buttons)
            self.assertEqual(len(adapter.sent), 1)
            self.assertEqual(len(adapter.edited), 1)
            buttons = adapter.edited[-1][1].buttons
            self.assertEqual(
                [b.data for b in buttons],
                ["setup:telegram", "setup:slack", "setup:discord"],
            )

    def test_setup_telegram_guide_contains_botfather_steps(self):
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "/setup telegram"))
            text = adapter.sent[-1].text
            self.assertIn("@BotFather", text)
            self.assertIn("allowed_chat_ids", text)
            self.assertIn("123456789:AA", text)
            self.assertEqual(client.prompts, [])

    def test_setup_slack_guide_covers_both_tokens_and_socket_mode(self):
        """Slack 引导必须同时讲清两枚 token 的分工（已核实 Slack 官方文档）。"""
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "/setup slack"))
            text = adapter.sent[-1].text
            # xoxb- 出站必需、xapp- 入站必需
            self.assertIn("xoxb-", text)
            self.assertIn("xapp-", text)
            # Socket Mode 的关键步骤与 scope
            self.assertIn("Enable Socket Mode", text)
            self.assertIn("connections:write", text)
            # 官方按钮名是 Add Bot User Event（不是 Add Bot Token Event）
            self.assertIn("Add Bot User Event", text)
            # 收发双向的前提：bot 必须被邀请进频道（它无法自己加群）
            self.assertIn("/invite", text)
            # 「入站尚未接入」这个说法已失效，不得复活成假话
            self.assertNotIn("conversations.history", text)
            self.assertNotIn("双向对话", text.replace("无法在 Slack 里与 bot 双向对话", ""))
            self.assertEqual(client.prompts, [])

    def test_setup_discord_guide_mentions_intent_and_dev_portal(self):
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "/setup discord"))
            text = adapter.sent[-1].text
            # 后台开关的现行名称是 "Message Content Intent"（旧的
            # "MESSAGE CONTENT INTENT" 是历史标签）
            self.assertIn("Message Content Intent", text)
            self.assertIn("discord.com/developers", text)
            # T2.2 落地后 Discord 已支持双向，"仅能主动发送"不得复活
            self.assertNotIn("仅实现主动", text)
            self.assertNotIn("无法在 Discord", text)
            self.assertEqual(client.prompts, [])

    def test_setup_is_case_insensitive_and_accepts_numbers(self):
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "/setup TELEGRAM"))
            self.assertIn("@BotFather", adapter.sent[-1].text)
            core.on_inbound(inbound("chat:55", "/Setup 1"))
            self.assertIn("@BotFather", adapter.sent[-1].text)
            core.on_inbound(inbound("chat:55", "/setup 2"))
            self.assertIn("xoxb-", adapter.sent[-1].text)
            self.assertEqual(client.prompts, [])

    def test_setup_invalid_argument_lists_platforms_without_raising(self):
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "/setup bogus"))
            out = adapter.sent[-1]
            self.assertEqual(out.kind, "error")
            for needle in ("Telegram", "Slack", "Discord", "/setup 1",
                           "telegram"):
                self.assertIn(needle, out.text)
            self.assertEqual(client.prompts, [])
            # the event loop survives a bad argument: next command still works
            core.on_inbound(inbound("chat:55", "/help"))
            self.assertEqual(adapter.sent[-1].text, HELP_TEXT)

    def test_setup_replies_do_not_contain_machine_specific_paths(self):
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            for cmd in ("/setup", "/setup telegram", "/setup slack",
                        "/setup discord", "/setup bogus"):
                core.on_inbound(inbound("chat:55", cmd))
            for out in adapter.sent:
                lines = out.text.splitlines()
                # line 1 is the runtime-computed config path (machine-specific
                # by design); the frozen copy around it must never hardcode one
                if lines and lines[0].startswith("配置文件："):
                    body = "\n".join(lines[1:])
                else:
                    body = out.text
                self.assertNotIn("D:\\workSpace", body)
                # 用**通用占位符**而不是本机真实用户名 —— 这条断言要防的是
                # "冻结文案硬编码了机器路径"，而把真实用户名写进公开仓库等于
                # 自己泄漏它，且与被测行为无关。
                self.assertNotIn("C:\\Users\\example-user", body)
                self.assertNotIn("C:\\Users\\%s" % os.environ.get("USERNAME", ""), body)

    def test_setup_config_path_is_derived_at_runtime(self):
        with tempfile.TemporaryDirectory() as td:
            cfg_path = os.path.join(td, "config.json")
            with open(cfg_path, "w", encoding="utf-8") as fh:
                fh.write("{}")
            core, client, adapter, _, _, _ = make_env(td)
            with mock.patch.dict(
                os.environ, {"OPENCODE_BRIDGE_CONFIG": cfg_path}
            ):
                core.on_inbound(inbound("chat:55", "/setup telegram"))
            first = adapter.sent[-1].text.splitlines()[0]
            self.assertEqual(first, f"配置文件： {os.path.abspath(cfg_path)}")

    def test_setup_button_callback_replies_exactly_once(self):
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            # Lane B fires on_inbound(kind="callback") *and* on_callback;
            # on_inbound must drop its copy or the reply would double-send.
            core.on_inbound(
                Inbound(
                    conversation_id="chat:55",
                    text="setup:telegram",
                    kind="callback",
                    callback_query_id="Q7",
                )
            )
            self.assertEqual(adapter.sent, [])
            core.on_callback("chat:55", "setup:telegram", "Q7")
            self.assertEqual(len(adapter.sent), 1)
            self.assertIn("@BotFather", adapter.sent[0].text)
            self.assertEqual(adapter.answers, [("Q7", "已打开接入引导")])
            self.assertEqual(client.prompts, [])

    def test_help_lists_setup_command(self):
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "/help"))
            self.assertIn("/setup", adapter.sent[-1].text)


# ----------------------------------------------------------------------
# 5-6: prompt routing / queueing
# ----------------------------------------------------------------------
class PromptRoutingTests(unittest.TestCase):
    def test_plain_text_prompts_exactly_once(self):
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "hello world"))
            self.assertEqual(len(client.prompts), 1)
            self.assertEqual(
                prompt_bodies(client.prompts)[0][1], "hello world"
            )
            kinds = [out.kind for out in adapter.sent]
            self.assertEqual(kinds, ["progress"])

    def test_prompt_409_queues_message_and_succeeded_flushes_it(self):
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            client.prompt_errors.append(OpenCodeError("busy", status=409))

            core.on_inbound(inbound("chat:55", "queued text"))
            sid = client.created_ids[0]
            self.assertEqual(prompt_bodies(client.prompts), [(sid, "queued text")])
            self.assertEqual(adapter.sent, [])  # nothing sent while busy

            core.event_stream.dispatch(ev("session.execution.succeeded", sessionID=sid))
            self.assertEqual(
                prompt_bodies(client.prompts),
                [(sid, "queued text"), (sid, "queued text")],
            )
            # queue drained: a second idle must not prompt again
            core.event_stream.dispatch(ev("session.execution.succeeded", sessionID=sid))
            self.assertEqual(len(client.prompts), 2)

    def test_prompt_error_sends_error_message(self):
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            client.prompt_errors.append(OpenCodeError("nope", status=500))
            core.on_inbound(inbound("chat:55", "hello"))
            self.assertEqual(adapter.sent[-1].kind, "error")
            self.assertIn("发送失败", adapter.sent[-1].text)


# ----------------------------------------------------------------------
# 7-9: streaming
# ----------------------------------------------------------------------
class StreamingTests(unittest.TestCase):
    def test_out_of_order_deltas_are_sorted_by_ordinal(self):
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "go"))
            sid = client.created_ids[0]

            core.event_stream.dispatch(ev("session.execution.started", sessionID=sid))
            core.event_stream.dispatch(
                ev(
                    "session.text.delta",
                    sessionID=sid,
                    assistantMessageID="msg_1",
                    ordinal=3,
                    delta="c",
                )
            )
            core.event_stream.dispatch(
                ev(
                    "session.text.delta",
                    sessionID=sid,
                    assistantMessageID="msg_1",
                    ordinal=1,
                    delta="a",
                )
            )
            core.event_stream.dispatch(
                ev(
                    "session.text.delta",
                    sessionID=sid,
                    assistantMessageID="msg_1",
                    ordinal=2,
                    delta="b",
                )
            )
            core.event_stream.dispatch(ev("session.execution.succeeded", sessionID=sid))

            finals = [out for _, out in adapter.edited if out.kind == "final"]
            self.assertEqual(len(finals), 1)
            self.assertEqual(finals[0].text, "abc")

    def test_edits_are_throttled_by_edit_interval(self):
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(
                td, bridge={"edit_interval_seconds": 1.5}
            )
            ticks = [1000.0]
            core.event_stream.clock = lambda: ticks[0]

            core.on_inbound(inbound("chat:55", "go"))
            sid = client.created_ids[0]
            self.assertEqual(adapter.sent[0].kind, "progress")
            core.event_stream.dispatch(ev("session.execution.started", sessionID=sid))

            def delta(text: str) -> None:
                core.event_stream.dispatch(
                    ev(
                        "session.text.delta",
                        sessionID=sid,
                        assistantMessageID="msg_1",
                        ordinal=0,
                        delta=text,
                    )
                )

            delta("A")
            self.assertEqual(len(adapter.edited), 1)
            ticks[0] = 1000.5
            delta("B")
            ticks[0] = 1001.0
            delta("C")
            self.assertEqual(len(adapter.edited), 1)  # still throttled
            ticks[0] = 1002.0
            delta("D")
            self.assertEqual(len(adapter.edited), 2)
            self.assertEqual(adapter.edited[1][1].text, "ABCD")

    def test_execution_succeeded_edits_progress_into_final(self):
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "go"))
            sid = client.created_ids[0]
            core.event_stream.dispatch(ev("session.execution.started", sessionID=sid))
            core.event_stream.dispatch(
                ev(
                    "session.text.delta",
                    sessionID=sid,
                    assistantMessageID="msg_1",
                    ordinal=0,
                    delta="结果如下",
                )
            )
            sends_before = len(adapter.sent)
            core.event_stream.dispatch(ev("session.execution.succeeded", sessionID=sid))

            finals = [out for _, out in adapter.edited if out.kind == "final"]
            self.assertEqual([f.text for f in finals], ["结果如下"])
            self.assertEqual(len(adapter.sent), sends_before)  # no extra send

    def test_execution_succeeded_without_output_sends_placeholder(self):
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "go"))
            sid = client.created_ids[0]
            core.event_stream.dispatch(ev("session.execution.started", sessionID=sid))
            core.event_stream.dispatch(ev("session.execution.succeeded", sessionID=sid))
            finals = [out for _, out in adapter.edited if out.kind == "final"]
            self.assertEqual([f.text for f in finals], [NO_OUTPUT_TEXT])

    def test_execution_succeeded_finalises_and_is_idempotent(self):
        # Live opencode 2.0.21 emits session.execution.succeeded but never
        # session.execution.succeeded / .interrupted — finalisation must work for both.
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "go"))
            sid = client.created_ids[0]
            core.event_stream.dispatch(ev("session.execution.started", sessionID=sid))
            core.event_stream.dispatch(
                ev(
                    "session.text.delta",
                    sessionID=sid,
                    assistantMessageID="msg_1",
                    ordinal=0,
                    delta="done",
                )
            )
            core.event_stream.dispatch(
                ev("session.execution.succeeded", sessionID=sid)
            )
            finals = [out for _, out in adapter.edited if out.kind == "final"]
            self.assertEqual([f.text for f in finals], ["done"])
            self.assertNotIn(sid, core._turns)

            # a late duplicate trigger must not publish a second final
            core.event_stream.dispatch(ev("session.execution.succeeded", sessionID=sid))
            finals = [out for _, out in adapter.edited if out.kind == "final"]
            self.assertEqual(len(finals), 1)

    def test_execution_interrupted_finalises_partial_output(self):
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "go"))
            sid = client.created_ids[0]
            core.event_stream.dispatch(ev("session.execution.started", sessionID=sid))
            core.event_stream.dispatch(
                ev(
                    "session.text.delta",
                    sessionID=sid,
                    assistantMessageID="msg_1",
                    ordinal=0,
                    delta="部分结果",
                )
            )
            core.event_stream.dispatch(
                ev("session.execution.interrupted", sessionID=sid,
                   reason="user")
            )
            finals = [out for _, out in adapter.edited if out.kind == "final"]
            self.assertEqual([f.text for f in finals], ["部分结果"])

    def test_unpublished_status_event_does_not_finalise_turn(self):
        """`session.status` 在 v2.0.22 **从不发布**，所以它不能、也不该触发收尾。

        这条测试以前断言的是相反的事（"status idle 会 finalize"），
        于是 1409 条测试全绿而真实环境永远收不到尾——**测试在保护虚构的契约**。
        现在改成断言真实契约：这个事件被忽略，且被记账成"未处理事件"。
        """
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "go"))
            sid = client.created_ids[0]
            core.event_stream.dispatch(ev("session.execution.started", sessionID=sid))
            core.event_stream.dispatch(
                ev(
                    "session.text.delta",
                    sessionID=sid,
                    assistantMessageID="msg_1",
                    ordinal=0,
                    delta="ok",
                )
            )
            core.event_stream.dispatch(
                ev("session.status", sessionID=sid, status={"type": "idle"})
            )
            # 关键断言：没有final，只有进行中的那条
            finals = [out for _, out in adapter.edited if out.kind == "final"]
            self.assertEqual(finals, [])
            # 且它被记账了，不再是"静默丢弃"
            self.assertIn("session.status", core.event_stream._unhandled_event_names)

            # 真正的收尾事件仍然要能收尾（证明上面不是"整条链路都不工作"）
            core.event_stream.dispatch(ev("session.execution.succeeded", sessionID=sid))
            finals = [out for _, out in adapter.edited if out.kind == "final"]
            self.assertEqual([f.text for f in finals], ["ok"])

    def test_overlong_final_completes_the_placeholder_and_sends_the_rest(self):
        """答复超过一条消息时，占位消息被**补完**成装得下的那一段，剩下的另发。

        ⚠️ 这条断言**改过**：它原来叫 ``test_overlong_final_is_sent_instead_of_edited``，
        钉的是"装不下就压根不改写、整段另发一条" —— 而那正是本任务要消灭的行为：
        占位消息会永远停在半截正文上，读者先读到半句、再读到全文，同一段话出现两次。
        """
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(
                td, bridge={"max_message_chars": 20}
            )
            core.on_inbound(inbound("chat:55", "go"))
            sid = client.created_ids[0]
            core.event_stream.dispatch(ev("session.execution.started", sessionID=sid))
            core.event_stream.dispatch(
                ev(
                    "session.text.delta",
                    sessionID=sid,
                    assistantMessageID="msg_1",
                    ordinal=0,
                    delta="x" * 30,
                )
            )
            self.assertEqual(adapter.edited, [])  # long delta never streamed
            core.event_stream.dispatch(ev("session.execution.succeeded", sessionID=sid))

            # 占位消息被补成 20 字符（= 一条消息的预算），不再是半截正文
            self.assertEqual(adapter.edited[-1][1].text, "x" * 20)
            self.assertEqual(adapter.sent[-1].kind, "final")
            self.assertEqual(adapter.sent[-1].text, "x" * 10)

    def test_final_edit_valueerror_falls_back_to_send(self):
        """改写抛 ValueError，而占位消息**已经显示着全文** ⇒ 一个字都不用补。

        读者手上已经是完整答复；再发一遍才是重复。这条断言以前写的是"另发一条
        完整正文"，那正好是本任务要消灭的重复。
        """
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "go"))
            sid = client.created_ids[0]
            core.event_stream.dispatch(ev("session.execution.started", sessionID=sid))
            core.event_stream.dispatch(
                ev(
                    "session.text.delta",
                    sessionID=sid,
                    assistantMessageID="msg_1",
                    ordinal=0,
                    delta="最终结果",
                )
            )
            adapter.edit_results.append(ValueError("edit text too long"))
            core.event_stream.dispatch(ev("session.execution.succeeded", sessionID=sid))

            self.assertEqual(
                [out.kind for out in adapter.sent], ["progress"],
                "占位消息已经显示全文，不该再发第二条",
            )

    def test_retry_scheduled_edits_progress_message(self):
        """重试提示改挂到 `session.retry.scheduled`——v2.0.22 里真实存在的那个事件。

        两处与旧实现不同，都是核实源码后的结论：

        1. 旧实现挂在 `session.status{type:"retry"}` 上，而 `session.status`
           在 v2.0.22 全代码库零处发布，所以那段代码**从来没跑过**。
        2. 新事件的 `data` 里**没有 `sessionID`**，只有 `assistantMessageID /
           attempt / at / error`，所以要靠 assistantMessageID 反查会话。
        """
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "go"))
            sid = client.created_ids[0]
            core.event_stream.dispatch(ev("session.execution.started", sessionID=sid))
            # 先产生一段正文，让 turn 里记下这个 assistantMessageID，
            # 否则反查不到会话（这正是新事件没有 sessionID 带来的约束）
            core.event_stream.dispatch(
                ev(
                    "session.text.delta",
                    sessionID=sid,
                    assistantMessageID="msg_retry_1",
                    ordinal=0,
                    delta="思考中",
                )
            )
            core.event_stream.dispatch(
                ev(
                    "session.retry.scheduled",
                    assistantMessageID="msg_retry_1",
                    attempt=2,
                    at=1234567890,
                    error={"message": "provider transport"},
                )
            )
            self.assertEqual(
                adapter.edited[-1][1].text,
                "⏳ 重试中 (attempt 2): provider transport",
            )

    def test_retry_scheduled_without_session_hint_is_ignored(self):
        """反查不到会话时不能崩、也不能乱发——什么都不发，只返回。"""
        with tempfile.TemporaryDirectory() as td:
            core, _client, adapter, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "go"))
            core.event_stream.dispatch(
                ev(
                    "session.retry.scheduled",
                    assistantMessageID="msg_never_seen",
                    attempt=1,
                )
            )
            # 没有匹配的 turn -> 不该发出任何编辑
            self.assertEqual(adapter.edited, [])

    def test_unrelated_session_events_are_filtered_by_event_volume(self):
        """`/api/event` 是全服务器广播，必须按会话归属过滤，但**只过滤高频事件**。

        实测曾刷出 `permission request for unknown session ses_effe80...`，
        那是开发者自己正在跑的 opencode 会话，与本桥毫无关系——却每次都打一条
        WARNING。同机多会话时这会把日志淹掉。

        但**不能一律过滤**：`permission.asked` 若因竞态丢失，agent 会永远等不到
        审批，而日志里什么都看不到。所以低频生命周期事件必须放行。
        """
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "go"))
            owned_session = client.created_ids[0]

            # 高频事件 + 别人的会话 -> 被归属过滤，handler 不跑、不产生任何编辑
            # （会打一行 DEBUG 说明被忽略了——降噪但可查，正是设计意图）
            core.event_stream.dispatch(
                ev("session.text.delta", sessionID="ses_someone_else",
                   assistantMessageID="msg_x", ordinal=0, delta="别人的内容")
            )
            self.assertEqual(adapter.edited, [], "别人的会话不该被渲染")

            # 高频事件 + 自己的会话 -> 正常处理
            core.event_stream.dispatch(
                ev("session.text.delta", sessionID=owned_session,
                   assistantMessageID="msg_y", ordinal=0, delta="我的内容")
            )
            self.assertTrue(adapter.edited, "自己的会话应被处理")
            self.assertEqual(adapter.edited[-1][1].text, "我的内容")

            # 低频事件 + 未知会话 -> **必须放行**，让原有的警告继续暴露问题
            with self.assertLogs("opencode_bridge.event_stream", level="WARNING") as logs:
                core.event_stream.dispatch(ev("permission.asked", sessionID="ses_ghost",
                                  id="per_1", action="bash"))
            self.assertTrue(
                any("unknown session" in line for line in logs.output),
                "低频事件不应被归属过滤吞掉，否则审批竞态会静默失败，实际: %r"
                % (logs.output,),
            )

    def test_unrelated_session_events_are_not_recorded_as_unhandled(self):
        """别人的会话事件**不算**"未处理事件名"——否则记账会被噪音撑爆。"""
        with tempfile.TemporaryDirectory() as td:
            core, client, _adapter, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "go"))
            core.event_stream.dispatch(
                ev("session.text.delta", sessionID="ses_someone_else",
                   assistantMessageID="msg_x", ordinal=0, delta="别人的")
            )
            self.assertEqual(core.event_stream._unhandled_event_names, {})

    def test_shutdown_interruption_does_not_finalise_turn(self):
        """`execution.interrupted{reason:"shutdown"}` **不是**结束——这一轮会被续跑。

        这是本次修掉的真实 bug。若误当结束处理，后果是双重的：
        半截内容被当最终答复发出去，且 turn 已弹掉；续跑后 `session.text.delta`
        会另建 turn，于是**同一条回复被发两遍**。

        源码依据：v2.0.22 `packages/core/src/session/projector.ts` 的 `projectIdle`
        对 `reason === "shutdown"` 直接 return，不产生 idle 投影。
        """
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "go"))
            sid = client.created_ids[0]
            core.event_stream.dispatch(ev("session.execution.started", sessionID=sid))
            core.event_stream.dispatch(
                ev("session.text.delta", sessionID=sid,
                   assistantMessageID="msg_1", ordinal=0, delta="写了一半")
            )
            core.event_stream.dispatch(
                ev("session.execution.interrupted",
                   sessionID=sid, reason="shutdown")
            )

            # 1) 不能收尾
            finals = [out for _, out in adapter.edited if out.kind == "final"]
            self.assertEqual(finals, [], "shutdown 不该触发收尾")

            # 2) turn 必须还在，否则续跑时会另建 turn 导致重复发送
            self.assertIn(sid, core._turns, "shutdown 之后 turn 不该被弹掉")

            # 3) 续跑：同一turn 继续累积，最终由真正的终止事件收尾
            core.event_stream.dispatch(
                ev("session.text.delta", sessionID=sid,
                   assistantMessageID="msg_1", ordinal=0, delta="，然后写完了")
            )
            core.event_stream.dispatch(ev("session.execution.succeeded", sessionID=sid))
            finals = [out for _, out in adapter.edited if out.kind == "final"]
            self.assertEqual([f.text for f in finals], ["写了一半，然后写完了"])

    def test_non_shutdown_interruption_does_finalise_turn(self):
        """对照组：`reason` 是别的值（user/inactivity）时**就是**结束，必须收尾。"""
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "go"))
            sid = client.created_ids[0]
            core.event_stream.dispatch(ev("session.execution.started", sessionID=sid))
            core.event_stream.dispatch(
                ev("session.text.delta", sessionID=sid,
                   assistantMessageID="msg_1", ordinal=0, delta="说到一半被打断")
            )
            core.event_stream.dispatch(
                ev("session.execution.interrupted",
                   sessionID=sid, reason="user")
            )
            finals = [out for _, out in adapter.edited if out.kind == "final"]
            self.assertEqual([f.text for f in finals], ["说到一半被打断"])

    def test_unhandled_event_is_recorded_and_logged_with_time_throttle(self):
        """未知事件必须**可观测**：记账 + 打日志，且日志有**硬上限**。

        两版语义都踩过坑，这两条断言就是它们的边界：

        -曾按"事件名切换"决定要不要打汇总。**前提不成立**——事件是多路交替的，
          于是每来一个新名字就打一行含 30+ 项的全量汇总，日志被自己的"降噪
          机制"冲垮（实测半小时上千行）。
        - 现在一律**按时间节流**：首次见到打一行，之后每 60 秒最多一行汇总。

        时钟是注入的（``event_stream.clock``），所以"等过窗口"靠**拨时钟**，
        不再手改 ``_last_unhandled_log_at`` 那个私有属性。
        """
        with tempfile.TemporaryDirectory() as td:
            core, _client, _adapter, _, _, _ = make_env(td)
            ticks = [1000.0]
            core.event_stream.clock = lambda: ticks[0]

            # 首次见到未知事件名 -> 打一行
            with self.assertLogs("opencode_bridge.event_stream", level="INFO") as logs:
                core.event_stream.dispatch(ev("session.brand.new.event", sessionID="ses_x"))
            self.assertIn("session.brand.new.event", core.event_stream._unhandled_event_names)
            self.assertEqual(len(logs.output), 1)

            # 节流窗口内：反复来**同一个**名字也不许打日志（但要记账）
            with self.assertNoLogs("opencode_bridge.event_stream", level="INFO"):
                for _ in range(5):
                    core.event_stream.dispatch(ev("session.brand.new.event", sessionID="ses_x"))
            self.assertEqual(core.event_stream._unhandled_event_names["session.brand.new.event"], 6)

            # 注意：**新出现的名字**不受节流限制，每个都打一行——
            # 这正是"看得见的静默"的意义（opencode 升版新增了什么，一眼就知道）。
            # 上界是"不同名字的个数"，不是事件条数，所以是安全的。
            with self.assertLogs("opencode_bridge.event_stream", level="INFO") as logs:
                core.event_stream.dispatch(ev("session.other.unknown.event", sessionID="ses_x"))
            self.assertEqual(len(logs.output), 1)
            self.assertEqual(core.event_stream._unhandled_event_names["session.other.unknown.event"], 1)

            # 把注入的时钟推过节流窗口 -> 才允许再打一行汇总
            ticks[0] += (
                core.event_stream._UNHANDLED_LOG_INTERVAL_SECONDS + 1
            )
            with self.assertLogs("opencode_bridge.event_stream", level="INFO") as logs:
                core.event_stream.dispatch(ev("session.brand.new.event", sessionID="ses_x"))
            self.assertEqual(len(logs.output), 1, "过窗口后应打一行汇总")
            self.assertTrue(any("累计" in line for line in logs.output))

    def test_known_but_ignored_events_are_not_recorded(self):
        """**认识但故意不处理**的事件（如思考流）必须完全静默，不许记账。

        实测 `session.reasoning.delta` 半分钟 2800+ 条。若把它当"未知事件"
        记账并打汇总，会把真正的错误彻底淹掉——而它只是"思考过程不该上IM"，
        不是我们漏实现了什么。
        """
        with tempfile.TemporaryDirectory() as td:
            core, _client, _adapter, _, _, _ = make_env(td)
            with self.assertNoLogs("opencode_bridge.core", level="INFO"):
                for name in sorted(core.event_stream._KNOWN_BUT_IGNORED_EVENTS)[:12]:
                    core.event_stream.dispatch(
                        ev(name, sessionID="ses_x", assistantMessageID="msg_a",
                           ordinal=0, delta="思考中")
                    )
            self.assertEqual(
                core.event_stream._unhandled_event_names, {},
                "故意忽略的事件不该进未处理记账：%r" % core.event_stream._unhandled_event_names,
            )

    def test_thinking_stream_never_reaches_the_im_bridge(self):
        """思考流既不上IM、也不进未处理记账——它是协议的一部分，不是错误。"""
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "go"))
            session_id = client.created_ids[0]
            core.event_stream.dispatch(ev("session.execution.started", sessionID=session_id))
            core.event_stream.dispatch(
                ev("session.reasoning.delta", sessionID=session_id,
                   assistantMessageID="msg_r", ordinal=0, delta="让我想想")
            )
            self.assertEqual(adapter.edited, [], "思考过程不该被渲染给用户")
            self.assertEqual(core.event_stream._unhandled_event_names, {})


# ----------------------------------------------------------------------
# 10-12: failures / permissions / unknown sessions
# ----------------------------------------------------------------------
class EventFailureTests(unittest.TestCase):
    def test_execution_failed_edits_the_progress_message_into_one_bubble(self):
        """失败走收尾那条路：那条 ``⏳ 处理中…`` 被改写成失败原因。

        之前是**另发一条** ``kind="error"`` 消息，于是用户看到两个气泡：一个卡住的
        进度、一个不相干的报错 —— 同一件事说了两遍，且进度那条永远不会被收掉。
        """
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "go"))
            sid = client.created_ids[0]
            core.event_stream.dispatch(
                ev(
                    "session.text.delta",
                    sessionID=sid,
                    assistantMessageID="msg_1",
                    ordinal=0,
                    delta="写到一半",
                )
            )
            self.assertEqual(adapter.sent[-1].kind, "progress")
            sent_before = len(adapter.sent)

            core.event_stream.dispatch(
                ev(
                    "session.execution.failed",
                    sessionID=sid,
                    error={"type": "APIError", "message": "boom"},
                )
            )

            # 只多了一条 edit，没有新消息
            self.assertEqual(len(adapter.sent), sent_before)
            self.assertEqual(adapter.edited[-1][1].kind, "error")
            self.assertIn("APIError", adapter.edited[-1][1].text)
            self.assertIn("boom", adapter.edited[-1][1].text)

    def test_permission_asked_then_callback_reply(self):
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "go"))
            sid = client.created_ids[0]

            core.event_stream.dispatch(
                ev(
                    "permission.asked",
                    sessionID=sid,
                    id="per_9",
                    action="edit",
                    resources=["a.txt", "b.txt"],
                    message="修改文件？",
                )
            )
            text = adapter.sent[-1].text
            self.assertIn("🔐 权限请求", text)
            self.assertIn("动作: edit", text)
            self.assertIn("资源: a.txt, b.txt", text)
            self.assertIn("/approve per_9", text)
            self.assertIn("/approve per_9 always", text)
            self.assertIn("/deny per_9", text)

            core.on_callback("chat:55", f"perm:{sid}:per_9:once", "Q1")
            self.assertEqual(
                client.permission_replies, [(sid, "per_9", "once")]
            )
            self.assertEqual(adapter.answers, [("Q1", "已处理")])

            # ⚠️ 这条断言的是**服务端报错**那条路，所以必须换一个请求 id：
            # 同一个 ``per_9`` 已经被答过了，按 C4 现在会被账本先一步挡掉，
            # 压根到不了服务端，也就走不到失败分支（那条由
            # ``PermissionSafetyTests`` 里另外两个用例覆盖）。
            core.event_stream.dispatch(
                ev("permission.asked", sessionID=sid, id="per_10", action="edit")
            )
            client.permission_errors.append(OpenCodeError("nope", status=400))
            core.on_callback("chat:55", f"perm:{sid}:per_10:reject", "Q2")
            self.assertEqual(adapter.answers[-1], ("Q2", "失败"))
            self.assertEqual(adapter.sent[-1].kind, "error")

    def test_malformed_callback_is_answered_with_failure(self):
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            core.on_callback("chat:55", "nonsense", "Q9")
            self.assertEqual(adapter.answers, [("Q9", "失败")])
            self.assertEqual(client.permission_replies, [])

    def test_callback_inbound_is_ignored_to_avoid_double_dispatch(self):
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            # Lane B fires on_inbound(kind="callback") *and* on_callback
            core.on_inbound(
                Inbound(
                    conversation_id="chat:55",
                    text="perm:ses_x:per_1:once",
                    kind="callback",
                    callback_query_id="Q1",
                )
            )
            self.assertEqual(adapter.sent, [])
            self.assertEqual(adapter.answers, [])
            self.assertEqual(client.permission_replies, [])

    def test_unknown_session_delta_is_dropped_silently(self):
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "go"))
            sends_before = len(adapter.sent)

            core.event_stream.dispatch(
                ev(
                    "session.text.delta",
                    sessionID="ses_ghost",
                    assistantMessageID="msg_1",
                    ordinal=0,
                    delta="x",
                )
            )
            core.event_stream.dispatch(ev("session.execution.started", sessionID="ses_ghost"))
            core.event_stream.dispatch(ev("session.execution.succeeded", sessionID="ses_ghost"))

            self.assertEqual(len(adapter.sent), sends_before)
            self.assertNotIn("ses_ghost", core._turns)

    def test_delta_carries_control_characters_cleaned_before_send(self):
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "go"))
            sid = client.created_ids[0]
            core.event_stream.dispatch(ev("session.execution.started", sessionID=sid))
            core.event_stream.dispatch(
                ev(
                    "session.text.delta",
                    sessionID=sid,
                    assistantMessageID="msg_1",
                    ordinal=0,
                    delta="bad\x00text",
                )
            )
            core.event_stream.dispatch(ev("session.execution.succeeded", sessionID=sid))
            finals = [out for _, out in adapter.edited if out.kind == "final"]
            self.assertEqual(len(finals), 1)
            self.assertNotIn("\x00", finals[0].text)
            self.assertEqual(finals[0].text, "badtext")


# ----------------------------------------------------------------------
# threading / lifecycle
# ----------------------------------------------------------------------
class CoreLifecycleTests(unittest.TestCase):
    def test_start_stop_closes_client_and_joins_thread(self):
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            core.start()
            self.assertTrue(adapter.started)
            thread = core._thread
            self.assertIsNotNone(thread)
            self.assertTrue(thread.is_alive())

            core.stop()
            core.stop()  # idempotent
            self.assertTrue(client.closed)
            self.assertTrue(adapter.stopped)
            self.assertFalse(thread.is_alive())

    def test_event_loop_survives_a_failing_dispatch(self):
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            core.start()
            thread = core._thread

            original = core.event_stream.dispatch
            calls = {"n": 0}

            def flaky(event: dict) -> None:
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("boom")
                original(event)

            core.event_stream.dispatch = flaky  # type: ignore[method-assign]
            # 事件流搬进了 opencode_bridge.event_stream（AGENTS.md §5.1），所以这行
            # 日志的 logger 也跟着换了名字；断言的仍然是同一条消息。
            with self.assertLogs("opencode_bridge.event_stream",
                                 level="ERROR") as logs:
                client.push(ev("session.execution.started", sessionID="ses_x"))
                client.push(ev("session.execution.started", sessionID="ses_x"))
                deadline = time.time() + 5.0
                while calls["n"] < 2 and time.time() < deadline:
                    time.sleep(0.01)
            self.assertEqual(calls["n"], 2)
            self.assertTrue(
                any("event dispatch failed" in line for line in logs.output)
            )
            self.assertTrue(thread.is_alive(), "SSE thread died on bad event")
            core.stop()
            self.assertFalse(thread.is_alive())

    def test_stop_before_start_is_safe(self):
        core = BridgeCore(Config(), FakeClient(), StateStore(
            os.path.join(tempfile.gettempdir(), "opencode-bridge-never.json")
        ))
        core.stop()  # must not raise


# ----------------------------------------------------------------------
# config / ruleset helpers
# ----------------------------------------------------------------------
# ----------------------------------------------------------------------
# 两个默认常量的**文档字符串是真的** —— 一次审计把它们标成「无出处的圆整数」
# ----------------------------------------------------------------------
# ⚠️ 这一类的价值全在**非空洞**上：一条注释如果只是"存在"而没人核，那它就是装饰；
# 而一条**写错**的注释比没有注释更糟，因为下一个人会信它。所以下面每一条断言都
# 在核**注释里的具体主张**，而不是核常量还在不在。
# 顺带把两件容易被下一个读代码的人搞错的事钉住：
# ① 4000 **超过** 6/13 个平台的真实上限，所以它不是"安全发送长度"；
# ② 它必须**每一处**都与平台上限取小者 —— 这一条以前只有 finalize 做到，
#    流式闸门没做，于是两处读者漂了（见下面那条"反过来写"的用例）。
#
# ⛔ ``core.py`` 里 ``DEFAULT_MAX_MESSAGE_CHARS`` 的文档字符串**还有一段已经过期**
# （它把"闸门未收窄"记成"已知残留，刻意不改"）。那个文件不在本轮范围内，所以
# 这里只记着：谁改那段注释时，请连同 ``outbound.one_message_budget`` 与
# ``event_stream._on_text_delta`` 的现状一起看。
class DocumentedDefaultConstantTests(unittest.TestCase):
    def test_the_two_constants_still_hold_their_documented_values(self):
        """``1.5`` 与 ``4000`` 必须原样 —— 这次改动是纯注释。"""
        self.assertEqual(DEFAULT_EDIT_INTERVAL, 1.5)
        self.assertEqual(DEFAULT_MAX_MESSAGE_CHARS, 4000)

    def test_the_config_defaults_agree_with_the_module_constants(self):
        """``config.bridge`` 的默认值与这两个常量是同一组数，两边不许漂移。"""
        self.assertEqual(DEFAULT_BRIDGE["edit_interval_seconds"],
                         DEFAULT_EDIT_INTERVAL)
        self.assertEqual(DEFAULT_BRIDGE["max_message_chars"],
                         DEFAULT_MAX_MESSAGE_CHARS)

    def test_the_max_chars_default_exceeds_six_of_the_platform_limits(self):
        """钉住注释里那句「**超过** 6/13 个平台的真实上限」。

        反向价值更大：哪天有人**调小** ``DEFAULT_MAX_MESSAGE_CHARS`` 让这句话不再
        成立，或者某个平台的上限变了，这条会红 —— 那正是该回来改注释的时候。
        """
        below, coinciding = self._platforms_relative_to_the_default()

        self.assertEqual(
            sorted(below),
            ["discord", "email", "irc", "qqbot", "twitch"],
            "低于 4000 的平台清单变了，core.py 里的注释要跟着改",
        )
        self.assertEqual(
            sorted(coinciding), ["mattermost"],
            "与 4000 相等的平台清单变了，core.py 里的注释要跟着改",
        )
        self.assertEqual(len(below) + len(coinciding), 6)

    def test_the_finalize_budget_is_narrowed_to_the_platform_limit(self):
        """钉住「收尾那条路被 min() 收窄」—— 4000 **不是**编辑预算的真值。

        走真实 :class:`OutboundSender`，而不是只读那一行代码。
        """
        for platform_limit in (2000, 400, 998):
            with self.subTest(platform_limit=platform_limit):
                adapter = ScriptedAdapter()
                adapter.max_message_length = platform_limit
                sender = OutboundSender(
                    adapter_for=lambda conversation_id, a=adapter: a,
                    max_message_chars=DEFAULT_MAX_MESSAGE_CHARS,
                )
                answer = "字" * (platform_limit + 800)

                sender.finalize(
                    CONVERSATION, _handle_for(adapter), answer, "ses_probe",
                    shown_progress_text="",
                )

                self.assertTrue(adapter.edited, "收尾必须尝试改写占位消息")
                self.assertLessEqual(
                    len(adapter.edited[-1][1].text), platform_limit,
                    "改写正文超过了平台上限 —— min() 那一步不见了",
                )

    def test_a_body_over_the_platform_limit_reaches_the_edit_via_shown_progress(self):
        """⚠️ **这条用例已经反过来写** —— 它原来断言的是缺陷本身。

        原来：给 ``finalize`` 一个比收窄后预算更长的 ``shown_progress_text``，
        断言编辑长度**超过**平台上限。这条路当时真的走得通（流式闸门只按桥的
        预算判，于是会记下超额的量），而 ``core.py`` 的 ``DEFAULT_MAX_MESSAGE_CHARS``
        文档字符串里那段「已知残留，刻意不改」描述的就是它。

        现在那条路**走不通**了：流式闸门与 ``finalize`` 都问
        ``one_message_budget``，所以 ``shown_progress_text`` 至多等于收尾用的预算。
        下面是"给定一个超额的 ``shown_progress_text``"这个**假设前提**下的断言 ——
        它证明不了不变式（不变式说的是**流式那一侧记不下**超额的值，而那在
        ``tests/test_event_stream.py`` 里直接断言），只证明收尾的下界确实还是那个
        下界。**留着的意义**：哪天有人删掉那个下界，这里会红；而下界一旦没了，
        不变式就没有第二道防线了。

        要真正的不变式与"读者恰好一次"，见
        ``tests/test_event_stream.py::ShownProgressTextFitsTheFinalizeBudgetTests``
        与 ``tests/test_progress_placeholder.py::TheTwoReadersAgreeOnOneBudgetTests``。
        """
        platform_limit = 2000
        adapter = ScriptedAdapter()
        adapter.max_message_length = platform_limit
        sender = OutboundSender(
            adapter_for=lambda conversation_id, a=adapter: a,
            max_message_chars=DEFAULT_MAX_MESSAGE_CHARS,
        )
        answer = "字" * 6000
        shown_beyond_budget = "字" * 2500

        sender.finalize(
            CONVERSATION, _handle_for(adapter), answer, "ses_probe",
            shown_progress_text=shown_beyond_budget,
        )

        self.assertGreater(
            len(adapter.edited[-1][1].text), platform_limit,
            "收尾那个 max(len(head), len(shown_progress_text)) 下界不见了 —— "
            "它是不变式的第二道防线（第一道是流式闸门按同一个预算判）",
        )

    def test_the_streaming_gate_and_finalize_ask_for_the_same_budget(self):
        """两个读者问的**必须是同一个数** —— 这是这个缺陷的根，不是它的表现。

        之前两侧各写一遍 ``min(...)``（闸门那遍还漏了平台上限），于是它们漂了。
        现在两侧都调 ``one_message_budget``；这条断言它给出的数确实是"两条上限的
        小者"，并且在 6/13 个平台上**确实小于**桥的预算 —— 也就是漂了就会出事的那
        些平台上，它不是一个恒等于 4000 的装饰函数。
        """
        for platform_limit in (2000, 400, 998, 4096, 40000):
            with self.subTest(platform_limit=platform_limit):
                adapter = ScriptedAdapter()
                adapter.max_message_length = platform_limit

                self.assertEqual(
                    one_message_budget(DEFAULT_MAX_MESSAGE_CHARS, adapter),
                    min(DEFAULT_MAX_MESSAGE_CHARS, platform_limit),
                )

        below = self._platforms_relative_to_the_default()[0]
        self.assertTrue(below, "没有任何平台低于 4000 —— 那这个预算就没有约束力")
        for name in below:
            with self.subTest(platform=name):
                adapter = build(name, {}, hooks=_NoOpHooks())
                self.assertLess(
                    one_message_budget(DEFAULT_MAX_MESSAGE_CHARS, adapter),
                    DEFAULT_MAX_MESSAGE_CHARS,
                    "%s 的上限低于 4000，而这个函数没有把预算收窄下来" % name,
                )

    @staticmethod
    def _platforms_relative_to_the_default() -> tuple[list[str], list[str]]:
        """Names of adapters whose effective limit is below / equal to the default.

        真适配器（``build`` 真类），不是替身 —— 断言的是**各平台自己声明的上限**，
        而那正是注释让读者去看的地方。
        """
        below, coinciding = [], []
        for name in registered_names():
            adapter = build(name, {}, hooks=_NoOpHooks())
            effective = int(adapter.effective_max_length)
            if effective < DEFAULT_MAX_MESSAGE_CHARS:
                below.append(name)
            elif effective == DEFAULT_MAX_MESSAGE_CHARS:
                coinciding.append(name)
        return below, coinciding


class _NoOpHooks:
    """``Adapter`` 只要一个 ``hooks`` 对象；这些用例不发消息也不收消息。"""

    def on_inbound(self, inbound: Inbound) -> None:
        return None

    def on_callback(self, conversation_id: str, data: str, query_id: str) -> None:
        return None


def _handle_for(adapter: ScriptedAdapter) -> MsgHandle:
    """A placeholder message handle, produced by the adapter's own ``send``."""
    return adapter.send(Outbound(conversation_id=CONVERSATION, text=""))


class ConfigBridgeTests(unittest.TestCase):
    def test_default_bridge_section(self):
        self.assertEqual(Config().bridge, DEFAULT_BRIDGE)

    def test_partial_bridge_section_is_merged_with_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "config.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"bridge": {"edit_interval_seconds": 0.2}}, fh)
            cfg = Config.load(path)
        self.assertEqual(
            cfg.bridge,
            dict(DEFAULT_BRIDGE, edit_interval_seconds=0.2),
        )

    def test_invalid_bridge_values_fall_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "config.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "bridge": {
                            "edit_interval_seconds": "fast",
                            "max_message_chars": -5,
                        }
                    },
                    fh,
                )
            with self.assertLogs("opencode_bridge.config", level="WARNING"):
                cfg = Config.load(path)
        self.assertEqual(cfg.bridge, DEFAULT_BRIDGE)

    def test_missing_config_file_keeps_bridge_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Config.load(os.path.join(td, "nope.json"))
        self.assertEqual(cfg.bridge, DEFAULT_BRIDGE)

    def test_core_reads_bridge_settings(self):
        with tempfile.TemporaryDirectory() as td:
            core, *_ = make_env(
                td,
                bridge={
                    "edit_interval_seconds": 0.4,
                    "max_message_chars": 123,
                },
            )
            self.assertEqual(core.edit_interval, 0.4)
            self.assertEqual(core.max_message_chars, 123)


class RulesetTests(unittest.TestCase):
    def test_ruleset_for_known_modes(self):
        self.assertIsNone(ruleset_for("ask"))
        self.assertEqual(
            ruleset_for("allow"),
            [{"action": "*", "resource": "*", "effect": "allow"}],
        )
        self.assertEqual(
            ruleset_for("deny"),
            [{"action": "*", "resource": "*", "effect": "deny"}],
        )

    def test_ruleset_for_unknown_mode_falls_back_to_ask(self):
        with self.assertLogs("opencode_bridge.session_registry", level="WARNING"):
            self.assertIsNone(ruleset_for("whatever"))


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
class CliTests(unittest.TestCase):
    @staticmethod
    def _write_config(td: str, data: dict) -> str:
        path = os.path.join(td, "config.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        return path

    def test_empty_adapter_tokens_exit_0_without_traceback(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write_config(
                td,
                {
                    "state_path": os.path.join(td, "state.json"),
                    "adapters": {
                        "telegram": {"bot_token": ""},
                        "slack": {"bot_token": ""},
                        "discord": {"bot_token": ""},
                    },
                },
            )
            stderr = io.StringIO()
            with mock.patch.dict(
                os.environ,
                {
                    "OPENCODE_URL": "http://127.0.0.1:9",
                    "OPENCODE_PASSWORD": "pw",
                },
            ):
                with redirect_stderr(stderr):
                    rc = cli.main(["--config", path])
        out = stderr.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("没有任何可用适配器", out)
        self.assertIn("/setup", out)
        self.assertNotIn("Traceback", out)

    def test_no_adapters_configured_exits_0_gracefully(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write_config(
                td, {"state_path": os.path.join(td, "state.json")}
            )
            stderr = io.StringIO()
            with mock.patch.dict(
                os.environ,
                {
                    "OPENCODE_URL": "http://127.0.0.1:9",
                    "OPENCODE_PASSWORD": "pw",
                },
            ):
                with redirect_stderr(stderr):
                    rc = cli.main(["--config", path])
        out = stderr.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("没有任何可用适配器", out)
        self.assertIn("/setup", out)
        self.assertNotIn("Traceback", out)

    @staticmethod
    def _cfg_with(adapters) -> Config:
        return Config(
            opencode_url="",
            opencode_password="",
            opencode_directory=".",
            opencode_agent="",
            permissions_mode="ask",
            log_level="INFO",
            state_path="state.json",
            bridge=dict(DEFAULT_BRIDGE),
            adapters=adapters,
        )

    def test_has_configured_adapter_three_states(self):
        # bot_token key missing -> False
        self.assertFalse(
            cli._has_configured_adapter(self._cfg_with({"telegram": {}}))
        )
        # empty string -> False
        self.assertFalse(
            cli._has_configured_adapter(
                self._cfg_with({"telegram": {"bot_token": ""}})
            )
        )
        # non-empty token -> True
        self.assertTrue(
            cli._has_configured_adapter(
                self._cfg_with({"telegram": {"bot_token": "x"}})
            )
        )

    def test_has_configured_adapter_empty_or_nondict_adapters(self):
        self.assertFalse(cli._has_configured_adapter(self._cfg_with({})))
        self.assertFalse(cli._has_configured_adapter(self._cfg_with("nope")))
        self.assertFalse(cli._has_configured_adapter(self._cfg_with(None)))

    def test_check_prints_version_pid_url_and_exits_0(self):
        seen: dict = {}

        class FakeInfoClient:
            def __init__(self, endpoint, **kwargs):
                seen["endpoint"] = endpoint
                self.closed = False

            def info(self):
                return {"version": "2.0.21", "pid": 4242}

            def close(self):
                self.closed = True

        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as td:
            missing = os.path.join(td, "nope.json")
            with mock.patch.object(
                cli,
                "discover_endpoint",
                return_value=Endpoint("http://127.0.0.1:4097", "pw"),
            ), mock.patch.object(cli, "OpenCodeClient", FakeInfoClient):
                with redirect_stdout(stdout):
                    rc = cli.main(["--check", "--config", missing])
        self.assertEqual(rc, 0)
        out = stdout.getvalue()
        self.assertIn("2.0.21", out)
        self.assertIn("4242", out)
        self.assertIn("http://127.0.0.1:4097", out)
        self.assertTrue(seen["endpoint"].url.startswith("http"))
        self.assertTrue(seen["endpoint"].auth_header()["Authorization"])

    def test_check_failure_exits_1(self):
        with tempfile.TemporaryDirectory() as td:
            missing = os.path.join(td, "nope.json")
            with mock.patch.object(
                cli,
                "discover_endpoint",
                side_effect=OpenCodeError("cannot resolve opencode endpoint"),
            ):
                with redirect_stderr(io.StringIO()):
                    rc = cli.main(["--check", "--config", missing])
        self.assertEqual(rc, 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


# ----------------------------------------------------------------------
# A4 真实服务端验证发现的回归：会话目录必须是绝对路径
# ----------------------------------------------------------------------
class SessionDirectoryIsAbsoluteTests(unittest.TestCase):
    """opencode `POST /api/session` 对相对路径（含 "."）一律 500 且响应体为空。

    2026-10-03 对着真实 opencode 2.0.22 实测：
      directory="."            -> 500（5/5 稳定复现，body 为空）
      directory=""             -> 200
      directory="<绝对路径>"    -> 200

    回环测试原本抓不到，因为它们都传绝对路径或临时目录；
只有**默认配置**会中招，而默认配置恰好就是 "."。
    """

    def test_default_directory_is_resolved_to_absolute(self):
        # Config 的默认值就是 "."，这是最容易中招的路径
        with tempfile.TemporaryDirectory() as temp_dir:
            core, client, _adapter, _state, _, config = make_env(temp_dir)
            self.assertEqual(
                config.opencode_directory, ".",
                "本测试的前提：默认值仍是相对路径。若默认值改了，这里会提醒你复核。",
            )
            core.on_inbound(inbound("chat:55", "hello"))
            self.assertTrue(os.path.isabs(client.create_attempts[0]["directory"]))

    def test_empty_directory_falls_back_to_absolute_not_bare_dot(self):
        # 配置为空时走 `or "."` 兜底 —— 那个兜底本身也必须被解析
        with tempfile.TemporaryDirectory() as temp_dir:
            core, client, _adapter, _state, _, config = make_env(temp_dir)
            config.opencode_directory = ""
            core.on_inbound(inbound("chat:56", "hello"))
            directory = client.create_attempts[0]["directory"]
            self.assertTrue(os.path.isabs(directory))
            self.assertNotEqual(directory, ".")

    def test_per_conversation_directory_override_is_also_resolved(self):
        # 每个会话 meta 里存的 directory 走的是另一条分支，同样不能放过
        with tempfile.TemporaryDirectory() as temp_dir:
            core, client, _adapter, state, _, _config = make_env(temp_dir)
            state.set_meta("chat:57", "directory", ".")
            core.on_inbound(inbound("chat:57", "hello"))
            self.assertTrue(os.path.isabs(client.create_attempts[0]["directory"]))

    def test_absolute_directory_is_passed_through_unchanged(self):
        # 反向保护：已经是绝对路径时不能被二次改写（否则会静默改变用户指定的目录）
        with tempfile.TemporaryDirectory() as temp_dir:
            core, client, _adapter, _state, _, config = make_env(temp_dir)
            chosen = os.path.join(temp_dir, "some-project")
            config.opencode_directory = chosen
            core.on_inbound(inbound("chat:58", "hello"))
            self.assertEqual(client.create_attempts[0]["directory"], chosen)

    def test_every_create_attempt_is_absolute_across_retries(self):
        # 权限回退会重试 create_session，两次都得是绝对路径
        with tempfile.TemporaryDirectory() as temp_dir:
            core, client, _adapter, _state, _, _config = make_env(temp_dir, mode="allow")
            core.on_inbound(inbound("chat:59", "hello"))
            self.assertGreaterEqual(len(client.create_attempts), 1)
            for attempt in client.create_attempts:
                self.assertTrue(os.path.isabs(attempt["directory"]))

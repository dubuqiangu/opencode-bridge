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
from opencode_bridge.config import Config
from opencode_bridge.core import (
    HELP_TEXT,
    NO_OUTPUT_TEXT,
    BridgeCore,
    ruleset_for,
)
from opencode_bridge.hooks import Inbound, MsgHandle, Outbound
from opencode_bridge.opencode_client import Endpoint, OpenCodeError
from opencode_bridge.state import StateStore

DEFAULT_BRIDGE = {"edit_interval_seconds": 1.5, "max_message_chars": 4000}


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
    """Offline stand-in for a messaging adapter."""

    name = "fake"

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
            self.assertEqual(client.prompts, [(sid, "hello")])

            # "restart": fresh state/client must reuse the stored session
            state2 = StateStore(path)
            client2 = FakeClient()
            adapter2 = FakeAdapter()
            core2 = BridgeCore(Config(), client2, state2)
            core2.attach(adapter2)
            core2.on_inbound(inbound("chat:55", "again"))

            self.assertEqual(client2.create_attempts, [])
            self.assertEqual(client2.prompts, [(sid, "again")])

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
            self.assertEqual(client.prompts, [(sid, "hello")])

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
                client.prompts[0][1], "hello world"
            )
            kinds = [out.kind for out in adapter.sent]
            self.assertEqual(kinds, ["progress"])

    def test_prompt_409_queues_message_and_succeeded_flushes_it(self):
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            client.prompt_errors.append(OpenCodeError("busy", status=409))

            core.on_inbound(inbound("chat:55", "queued text"))
            sid = client.created_ids[0]
            self.assertEqual(client.prompts, [(sid, "queued text")])
            self.assertEqual(adapter.sent, [])  # nothing sent while busy

            core._dispatch(ev("session.execution.succeeded", sessionID=sid))
            self.assertEqual(
                client.prompts,
                [(sid, "queued text"), (sid, "queued text")],
            )
            # queue drained: a second idle must not prompt again
            core._dispatch(ev("session.execution.succeeded", sessionID=sid))
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

            core._dispatch(ev("session.execution.started", sessionID=sid))
            core._dispatch(
                ev(
                    "session.text.delta",
                    sessionID=sid,
                    assistantMessageID="msg_1",
                    ordinal=3,
                    delta="c",
                )
            )
            core._dispatch(
                ev(
                    "session.text.delta",
                    sessionID=sid,
                    assistantMessageID="msg_1",
                    ordinal=1,
                    delta="a",
                )
            )
            core._dispatch(
                ev(
                    "session.text.delta",
                    sessionID=sid,
                    assistantMessageID="msg_1",
                    ordinal=2,
                    delta="b",
                )
            )
            core._dispatch(ev("session.execution.succeeded", sessionID=sid))

            finals = [out for _, out in adapter.edited if out.kind == "final"]
            self.assertEqual(len(finals), 1)
            self.assertEqual(finals[0].text, "abc")

    def test_edits_are_throttled_by_edit_interval(self):
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(
                td, bridge={"edit_interval_seconds": 1.5}
            )
            ticks = [1000.0]
            core.clock = lambda: ticks[0]

            core.on_inbound(inbound("chat:55", "go"))
            sid = client.created_ids[0]
            self.assertEqual(adapter.sent[0].kind, "progress")
            core._dispatch(ev("session.execution.started", sessionID=sid))

            def delta(text: str) -> None:
                core._dispatch(
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
            core._dispatch(ev("session.execution.started", sessionID=sid))
            core._dispatch(
                ev(
                    "session.text.delta",
                    sessionID=sid,
                    assistantMessageID="msg_1",
                    ordinal=0,
                    delta="结果如下",
                )
            )
            sends_before = len(adapter.sent)
            core._dispatch(ev("session.execution.succeeded", sessionID=sid))

            finals = [out for _, out in adapter.edited if out.kind == "final"]
            self.assertEqual([f.text for f in finals], ["结果如下"])
            self.assertEqual(len(adapter.sent), sends_before)  # no extra send

    def test_execution_succeeded_without_output_sends_placeholder(self):
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "go"))
            sid = client.created_ids[0]
            core._dispatch(ev("session.execution.started", sessionID=sid))
            core._dispatch(ev("session.execution.succeeded", sessionID=sid))
            finals = [out for _, out in adapter.edited if out.kind == "final"]
            self.assertEqual([f.text for f in finals], [NO_OUTPUT_TEXT])

    def test_execution_succeeded_finalises_and_is_idempotent(self):
        # Live opencode 2.0.21 emits session.execution.succeeded but never
        # session.execution.succeeded / .interrupted — finalisation must work for both.
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "go"))
            sid = client.created_ids[0]
            core._dispatch(ev("session.execution.started", sessionID=sid))
            core._dispatch(
                ev(
                    "session.text.delta",
                    sessionID=sid,
                    assistantMessageID="msg_1",
                    ordinal=0,
                    delta="done",
                )
            )
            core._dispatch(
                ev("session.execution.succeeded", sessionID=sid)
            )
            finals = [out for _, out in adapter.edited if out.kind == "final"]
            self.assertEqual([f.text for f in finals], ["done"])
            self.assertNotIn(sid, core._turns)

            # a late duplicate trigger must not publish a second final
            core._dispatch(ev("session.execution.succeeded", sessionID=sid))
            finals = [out for _, out in adapter.edited if out.kind == "final"]
            self.assertEqual(len(finals), 1)

    def test_execution_interrupted_finalises_partial_output(self):
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "go"))
            sid = client.created_ids[0]
            core._dispatch(ev("session.execution.started", sessionID=sid))
            core._dispatch(
                ev(
                    "session.text.delta",
                    sessionID=sid,
                    assistantMessageID="msg_1",
                    ordinal=0,
                    delta="部分结果",
                )
            )
            core._dispatch(
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
            core._dispatch(ev("session.execution.started", sessionID=sid))
            core._dispatch(
                ev(
                    "session.text.delta",
                    sessionID=sid,
                    assistantMessageID="msg_1",
                    ordinal=0,
                    delta="ok",
                )
            )
            core._dispatch(
                ev("session.status", sessionID=sid, status={"type": "idle"})
            )
            # 关键断言：没有final，只有进行中的那条
            finals = [out for _, out in adapter.edited if out.kind == "final"]
            self.assertEqual(finals, [])
            # 且它被记账了，不再是"静默丢弃"
            self.assertIn("session.status", core._unhandled_event_names)

            # 真正的收尾事件仍然要能收尾（证明上面不是"整条链路都不工作"）
            core._dispatch(ev("session.execution.succeeded", sessionID=sid))
            finals = [out for _, out in adapter.edited if out.kind == "final"]
            self.assertEqual([f.text for f in finals], ["ok"])

    def test_overlong_final_is_sent_instead_of_edited(self):
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(
                td, bridge={"max_message_chars": 20}
            )
            core.on_inbound(inbound("chat:55", "go"))
            sid = client.created_ids[0]
            core._dispatch(ev("session.execution.started", sessionID=sid))
            core._dispatch(
                ev(
                    "session.text.delta",
                    sessionID=sid,
                    assistantMessageID="msg_1",
                    ordinal=0,
                    delta="x" * 30,
                )
            )
            self.assertEqual(adapter.edited, [])  # long delta never edited
            core._dispatch(ev("session.execution.succeeded", sessionID=sid))

            self.assertEqual(adapter.edited, [])  # still no edit call
            self.assertEqual(adapter.sent[-1].kind, "final")
            self.assertEqual(adapter.sent[-1].text, "x" * 30)

    def test_final_edit_valueerror_falls_back_to_send(self):
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "go"))
            sid = client.created_ids[0]
            core._dispatch(ev("session.execution.started", sessionID=sid))
            core._dispatch(
                ev(
                    "session.text.delta",
                    sessionID=sid,
                    assistantMessageID="msg_1",
                    ordinal=0,
                    delta="最终结果",
                )
            )
            adapter.edit_results.append(ValueError("edit text too long"))
            core._dispatch(ev("session.execution.succeeded", sessionID=sid))

            self.assertEqual(adapter.sent[-1].kind, "final")
            self.assertEqual(adapter.sent[-1].text, "最终结果")

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
            core._dispatch(ev("session.execution.started", sessionID=sid))
            # 先产生一段正文，让 turn 里记下这个 assistantMessageID，
            # 否则反查不到会话（这正是新事件没有 sessionID 带来的约束）
            core._dispatch(
                ev(
                    "session.text.delta",
                    sessionID=sid,
                    assistantMessageID="msg_retry_1",
                    ordinal=0,
                    delta="思考中",
                )
            )
            core._dispatch(
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
            core._dispatch(
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
            core._dispatch(
                ev("session.text.delta", sessionID="ses_someone_else",
                   assistantMessageID="msg_x", ordinal=0, delta="别人的内容")
            )
            self.assertEqual(adapter.edited, [], "别人的会话不该被渲染")

            # 高频事件 + 自己的会话 -> 正常处理
            core._dispatch(
                ev("session.text.delta", sessionID=owned_session,
                   assistantMessageID="msg_y", ordinal=0, delta="我的内容")
            )
            self.assertTrue(adapter.edited, "自己的会话应被处理")
            self.assertEqual(adapter.edited[-1][1].text, "我的内容")

            # 低频事件 + 未知会话 -> **必须放行**，让原有的警告继续暴露问题
            with self.assertLogs("opencode_bridge.core", level="WARNING") as logs:
                core._dispatch(ev("permission.asked", sessionID="ses_ghost",
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
            core._dispatch(
                ev("session.text.delta", sessionID="ses_someone_else",
                   assistantMessageID="msg_x", ordinal=0, delta="别人的")
            )
            self.assertEqual(core._unhandled_event_names, {})

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
            core._dispatch(ev("session.execution.started", sessionID=sid))
            core._dispatch(
                ev("session.text.delta", sessionID=sid,
                   assistantMessageID="msg_1", ordinal=0, delta="写了一半")
            )
            core._dispatch(
                ev("session.execution.interrupted",
                   sessionID=sid, reason="shutdown")
            )

            # 1) 不能收尾
            finals = [out for _, out in adapter.edited if out.kind == "final"]
            self.assertEqual(finals, [], "shutdown 不该触发收尾")

            # 2) turn 必须还在，否则续跑时会另建 turn 导致重复发送
            self.assertIn(sid, core._turns, "shutdown 之后 turn 不该被弹掉")

            # 3) 续跑：同一turn 继续累积，最终由真正的终止事件收尾
            core._dispatch(
                ev("session.text.delta", sessionID=sid,
                   assistantMessageID="msg_1", ordinal=0, delta="，然后写完了")
            )
            core._dispatch(ev("session.execution.succeeded", sessionID=sid))
            finals = [out for _, out in adapter.edited if out.kind == "final"]
            self.assertEqual([f.text for f in finals], ["写了一半，然后写完了"])

    def test_non_shutdown_interruption_does_finalise_turn(self):
        """对照组：`reason` 是别的值（user/inactivity）时**就是**结束，必须收尾。"""
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "go"))
            sid = client.created_ids[0]
            core._dispatch(ev("session.execution.started", sessionID=sid))
            core._dispatch(
                ev("session.text.delta", sessionID=sid,
                   assistantMessageID="msg_1", ordinal=0, delta="说到一半被打断")
            )
            core._dispatch(
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
        """
        with tempfile.TemporaryDirectory() as td:
            core, _client, _adapter, _, _, _ = make_env(td)

            # 首次见到未知事件名 -> 打一行
            with self.assertLogs("opencode_bridge.core", level="INFO") as logs:
                core._dispatch(ev("session.brand.new.event", sessionID="ses_x"))
            self.assertIn("session.brand.new.event", core._unhandled_event_names)
            self.assertEqual(len(logs.output), 1)

            # 节流窗口内：反复来**同一个**名字也不许打日志（但要记账）
            with self.assertNoLogs("opencode_bridge.core", level="INFO"):
                for _ in range(5):
                    core._dispatch(ev("session.brand.new.event", sessionID="ses_x"))
            self.assertEqual(core._unhandled_event_names["session.brand.new.event"], 6)

            # 注意：**新出现的名字**不受节流限制，每个都打一行——
            # 这正是"看得见的静默"的意义（opencode 升版新增了什么，一眼就知道）。
            # 上界是"不同名字的个数"，不是事件条数，所以是安全的。
            with self.assertLogs("opencode_bridge.core", level="INFO") as logs:
                core._dispatch(ev("session.other.unknown.event", sessionID="ses_x"))
            self.assertEqual(len(logs.output), 1)
            self.assertEqual(core._unhandled_event_names["session.other.unknown.event"], 1)

            # 把时钟往前推过节流窗口 -> 才允许再打一行汇总
            core._last_unhandled_log_at -= (
                core._UNHANDLED_LOG_INTERVAL_SECONDS + 1
            )
            with self.assertLogs("opencode_bridge.core", level="INFO") as logs:
                core._dispatch(ev("session.brand.new.event", sessionID="ses_x"))
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
                for name in sorted(core._KNOWN_BUT_IGNORED_EVENTS)[:12]:
                    core._dispatch(
                        ev(name, sessionID="ses_x", assistantMessageID="msg_a",
                           ordinal=0, delta="思考中")
                    )
            self.assertEqual(
                core._unhandled_event_names, {},
                "故意忽略的事件不该进未处理记账：%r" % core._unhandled_event_names,
            )

    def test_thinking_stream_never_reaches_the_im_bridge(self):
        """思考流既不上IM、也不进未处理记账——它是协议的一部分，不是错误。"""
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "go"))
            session_id = client.created_ids[0]
            core._dispatch(ev("session.execution.started", sessionID=session_id))
            core._dispatch(
                ev("session.reasoning.delta", sessionID=session_id,
                   assistantMessageID="msg_r", ordinal=0, delta="让我想想")
            )
            self.assertEqual(adapter.edited, [], "思考过程不该被渲染给用户")
            self.assertEqual(core._unhandled_event_names, {})


# ----------------------------------------------------------------------
# 10-12: failures / permissions / unknown sessions
# ----------------------------------------------------------------------
class EventFailureTests(unittest.TestCase):
    def test_execution_failed_sends_error_message(self):
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "go"))
            sid = client.created_ids[0]
            core._dispatch(
                ev(
                    "session.execution.failed",
                    sessionID=sid,
                    error={"type": "APIError", "message": "boom"},
                )
            )
            self.assertEqual(adapter.sent[-1].kind, "error")
            self.assertIn("APIError", adapter.sent[-1].text)
            self.assertIn("boom", adapter.sent[-1].text)

    def test_permission_asked_then_callback_reply(self):
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "go"))
            sid = client.created_ids[0]

            core._dispatch(
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

            client.permission_errors.append(OpenCodeError("nope", status=400))
            core.on_callback("chat:55", f"perm:{sid}:per_9:reject", "Q2")
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

            core._dispatch(
                ev(
                    "session.text.delta",
                    sessionID="ses_ghost",
                    assistantMessageID="msg_1",
                    ordinal=0,
                    delta="x",
                )
            )
            core._dispatch(ev("session.execution.started", sessionID="ses_ghost"))
            core._dispatch(ev("session.execution.succeeded", sessionID="ses_ghost"))

            self.assertEqual(len(adapter.sent), sends_before)
            self.assertNotIn("ses_ghost", core._turns)

    def test_delta_carries_control_characters_cleaned_before_send(self):
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "go"))
            sid = client.created_ids[0]
            core._dispatch(ev("session.execution.started", sessionID=sid))
            core._dispatch(
                ev(
                    "session.text.delta",
                    sessionID=sid,
                    assistantMessageID="msg_1",
                    ordinal=0,
                    delta="bad\x00text",
                )
            )
            core._dispatch(ev("session.execution.succeeded", sessionID=sid))
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

            original = core._dispatch
            calls = {"n": 0}

            def flaky(event: dict) -> None:
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("boom")
                original(event)

            core._dispatch = flaky  # type: ignore[method-assign]
            with self.assertLogs("opencode_bridge.core", level="ERROR") as logs:
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
            {"edit_interval_seconds": 0.2, "max_message_chars": 4000},
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
        with self.assertLogs("opencode_bridge.core", level="WARNING"):
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

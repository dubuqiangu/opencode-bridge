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
            self.assertEqual(attempt["directory"], ".")
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
            self.assertEqual(client.create_attempts[1]["directory"], "docs")
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
                self.assertNotIn("C:\\Users\\33204", body)

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

    def test_prompt_409_queues_message_and_idle_flushes_it(self):
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            client.prompt_errors.append(OpenCodeError("busy", status=409))

            core.on_inbound(inbound("chat:55", "queued text"))
            sid = client.created_ids[0]
            self.assertEqual(client.prompts, [(sid, "queued text")])
            self.assertEqual(adapter.sent, [])  # nothing sent while busy

            core._dispatch(ev("session.idle", sessionID=sid))
            self.assertEqual(
                client.prompts,
                [(sid, "queued text"), (sid, "queued text")],
            )
            # queue drained: a second idle must not prompt again
            core._dispatch(ev("session.idle", sessionID=sid))
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
            core._dispatch(ev("session.idle", sessionID=sid))

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

    def test_idle_edits_progress_into_final(self):
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
            core._dispatch(ev("session.idle", sessionID=sid))

            finals = [out for _, out in adapter.edited if out.kind == "final"]
            self.assertEqual([f.text for f in finals], ["结果如下"])
            self.assertEqual(len(adapter.sent), sends_before)  # no extra send

    def test_idle_without_output_sends_placeholder(self):
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "go"))
            sid = client.created_ids[0]
            core._dispatch(ev("session.execution.started", sessionID=sid))
            core._dispatch(ev("session.idle", sessionID=sid))
            finals = [out for _, out in adapter.edited if out.kind == "final"]
            self.assertEqual([f.text for f in finals], [NO_OUTPUT_TEXT])

    def test_execution_succeeded_finalises_and_is_idempotent(self):
        # Live opencode 2.0.21 emits session.execution.succeeded but never
        # session.idle — finalisation must work for both.
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
            core._dispatch(ev("session.idle", sessionID=sid))
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

    def test_status_idle_finalises_turn(self):
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
            core._dispatch(ev("session.idle", sessionID=sid))

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
            core._dispatch(ev("session.idle", sessionID=sid))

            self.assertEqual(adapter.sent[-1].kind, "final")
            self.assertEqual(adapter.sent[-1].text, "最终结果")

    def test_retry_status_edits_progress_message(self):
        with tempfile.TemporaryDirectory() as td:
            core, client, adapter, _, _, _ = make_env(td)
            core.on_inbound(inbound("chat:55", "go"))
            sid = client.created_ids[0]
            core._dispatch(ev("session.execution.started", sessionID=sid))
            core._dispatch(
                ev(
                    "session.status",
                    sessionID=sid,
                    status={
                        "type": "retry",
                        "attempt": 2,
                        "message": "provider transport",
                    },
                )
            )
            self.assertEqual(adapter.edited[-1][1].text, "⏳ 重试中 (attempt 2): provider transport")


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
            core._dispatch(ev("session.idle", sessionID="ses_ghost"))

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
            core._dispatch(ev("session.idle", sessionID=sid))
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
                client.push(ev("session.status", sessionID="ses_x",
                               status={"type": "busy"}))
                client.push(ev("session.status", sessionID="ses_x",
                               status={"type": "busy"}))
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

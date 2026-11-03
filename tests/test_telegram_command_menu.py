"""telegram：``start()`` 注册命令菜单（``setMyCommands``）—— 单一真源 + 失败只告警。

背景：桥此前**没有任何** ``setMyCommands`` 调用，用户只能靠 ``/help`` 发现命令。
现在 ``TelegramAdapter.start()`` 在凭据探测之后**发一次** ``setMyCommands``，命令集与
说明文案全部来自 :mod:`opencode_bridge.command_menu` 的单一真源（命令表 = 分发与菜单
共同的真源）。

守门三件事（⛔ 只改行为不加守门等于没改）：

* 启动时 ``setMyCommands`` **恰好发一次**，且命令集 = 命令表派生的规范命令；
* ``_post`` 抛异常 ⇒ ``start()`` **照常成功**（传输层起来）+ **恰好一条** WARNING；
* **漂移守卫**：菜单里的每个命令名都可分发、都是命令表的键，且说明文案逐字来自
  ``HELP_TEXT``（⛔ 不新造文案）—— 全部对着**真命令表**断言，不用手抄清单。

⚠️ 本文件不联网：全部用替换 ``_post`` 的办法（与 ``test_telegram_startup_probe.py``
一致）。
"""

from __future__ import annotations

import logging
import re
import unittest

# 期望的 warning 不刷屏；``assertLogs`` 自己换 handler，不受影响。
logging.getLogger("opencode_bridge").addHandler(logging.NullHandler())

from opencode_bridge.adapters.telegram import TelegramAdapter
from opencode_bridge.command_menu import (
    CANONICAL_COMMAND_NAMES,
    COMMAND_HANDLERS,
    COMMAND_MENU_DESCRIPTIONS,
    command_menu_entries,
)
from opencode_bridge.command_text import HELP_TEXT
from opencode_bridge.commands import CommandHandler

#: 一个**形状上就不是**真 token 的占位值（真形状的夹具只在要验脱敏时才需要）。
FAKE_BOT_TOKEN = "123456789:not-a-real-token-abcdefghij"

#: Telegram Bot API 对命令名 / 说明 / 条数的限额（超了平台会整批拒绝）。
TELEGRAM_COMMAND_NAME_PATTERN = re.compile(r"^[a-z0-9_]{1,32}$")
TELEGRAM_DESCRIPTION_MAX_LENGTH = 256
TELEGRAM_MAX_COMMANDS = 100


class _RecordingHooks:
    def on_inbound(self, inbound) -> None:
        return None

    def on_callback(self, conversation_id: str, data: str, query_id: str) -> None:
        return None


def make_adapter() -> TelegramAdapter:
    adapter = TelegramAdapter({"bot_token": FAKE_BOT_TOKEN}, _RecordingHooks())
    adapter.min_interval = 0
    return adapter


def stub_startup_post(adapter: TelegramAdapter, on_set_my_commands) -> list[dict]:
    """替换 ``_post``：``getMe`` 通过、``setMyCommands`` 交给回调、其余回空批。

    返回**捕获到的** ``setMyCommands`` payload 列表（闭包式捕获，见
    ``tests/test_telegram_callback_data_reject.py``）。回调的返回值决定 ``_post``
    返回什么 —— 因此"抛异常"那一臂只需让回调 ``raise``。
    """
    captured_payloads: list[dict] = []

    def fake_post(method, payload=None, *, timeout=None):
        if method == "getMe":
            return {"ok": True, "result": {"id": 1}}
        if method == "setMyCommands":
            captured_payloads.append(dict(payload or {}))
            return on_set_my_commands(dict(payload or {}))
        return {"ok": True, "result": []}

    adapter._post = fake_post
    return captured_payloads


class CommandMenuRegistrationTests(unittest.TestCase):
    """① 菜单发出去了、发的是什么；② 发失败也不许拦住 ``start()``。"""

    def test_start_sends_the_command_menu_exactly_once(self):
        adapter = make_adapter()
        calls = stub_startup_post(adapter, lambda payload: {"ok": True, "result": True})

        adapter.start()
        self.addCleanup(adapter.stop)

        self.assertEqual(len(calls), 1, f"该恰好注册一次。实际：{calls!r}")
        # 出站集合对着**命令表的派生结果**断言（不是对着自己、也不是手抄清单）：
        # 这样"菜单被清空"这类变异会在这里真红。
        self.assertEqual(
            [entry["command"] for entry in calls[0]["commands"]],
            list(CANONICAL_COMMAND_NAMES),
        )

    def test_failed_registration_only_warns_and_start_still_succeeds(self):
        """``setMyCommands`` 抛异常 ⇒ 传输层照起、恰好一条 WARNING、零异常上抛。"""

        def explode(_payload):
            raise RuntimeError("429 too many requests")

        adapter = make_adapter()
        stub_startup_post(adapter, explode)

        with self.assertLogs(
            "opencode_bridge.adapters.telegram", level="WARNING"
        ) as captured:
            adapter.start()
        self.addCleanup(adapter.stop)

        self.assertIsNotNone(adapter.transport, "注册失败也不许拦住传输层")
        self.assertEqual(
            len(captured.output), 1, f"该恰好一条告警。实际：{captured.output!r}"
        )
        warning_line = captured.output[0]
        self.assertIn("setMyCommands", warning_line)
        self.assertIn("注册失败", warning_line)
        self.assertIn("RuntimeError", warning_line)

    def test_registration_never_calls_post_twice_on_success(self):
        """成功的 ``setMyCommands`` 不许在别处被**再调一次**（比如重试）。"""
        adapter = make_adapter()
        calls = stub_startup_post(adapter, lambda payload: {"ok": True, "result": True})

        adapter.start()
        self.addCleanup(adapter.stop)
        adapter.stop()

        self.assertEqual(len(calls), 1)


class CommandMenuMatchesTheCommandTableTests(unittest.TestCase):
    """漂移守卫：菜单全部对着**真命令表**断言，⛔ 不用手抄清单。"""

    def test_every_menu_command_is_a_command_table_key(self):
        menu_names = [entry["command"] for entry in command_menu_entries()]
        self.assertTrue(menu_names, "菜单不能为空")
        for name in menu_names:
            self.assertIn(
                name, COMMAND_HANDLERS, f"{name} 不在命令表里 ⇒ 菜单会露出死命令"
            )

    def test_every_menu_command_resolves_to_a_real_handler_method(self):
        for name in [entry["command"] for entry in command_menu_entries()]:
            method_name = COMMAND_HANDLERS[name]
            self.assertTrue(
                callable(getattr(CommandHandler, method_name, None)),
                f"{name} 指向 {method_name}，但 CommandHandler 上没有这个方法",
            )

    def test_menu_lists_exactly_the_canonical_handler_keys(self):
        self.assertEqual(
            set(CANONICAL_COMMAND_NAMES), set(COMMAND_MENU_DESCRIPTIONS),
            "每个规范命令都必须有说明、且说明不许指向不存在的命令",
        )
        self.assertEqual(
            [entry["command"] for entry in command_menu_entries()],
            list(CANONICAL_COMMAND_NAMES),
        )

    def test_aliases_stay_dispatchable_but_are_not_listed_twice(self):
        """``reset`` / ``allow`` 是别名：仍可分发，但菜单里不重复出现。"""
        self.assertEqual(COMMAND_HANDLERS["reset"], COMMAND_HANDLERS["new"])
        self.assertEqual(COMMAND_HANDLERS["allow"], COMMAND_HANDLERS["approve"])
        menu_names = [entry["command"] for entry in command_menu_entries()]
        self.assertNotIn("reset", menu_names)
        self.assertNotIn("allow", menu_names)

    def test_descriptions_are_grounded_in_help_text(self):
        """⛔ 说明文案逐字来自 ``HELP_TEXT`` —— 防"另写一套"文案。"""
        for name, description in COMMAND_MENU_DESCRIPTIONS.items():
            self.assertIn(
                description, HELP_TEXT,
                f"{name} 的说明必须逐字来自 HELP_TEXT，不许新造",
            )

    def test_menu_respects_telegram_limits(self):
        entries = command_menu_entries()
        self.assertLessEqual(len(entries), TELEGRAM_MAX_COMMANDS)
        for entry in entries:
            self.assertRegex(entry["command"], TELEGRAM_COMMAND_NAME_PATTERN)
            self.assertLessEqual(
                len(entry["description"]), TELEGRAM_DESCRIPTION_MAX_LENGTH
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

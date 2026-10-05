"""授权白名单的**可见性**：三件事必须都对用户说真话。

这三组测试守的是同一个形状的缺陷 —— **"谁能驱动我的 bot" 曾经只有闸门知道，
所有给人看的出口都报"配置好了"**：

1. ``/setup`` 的 Slack / Discord 引导**只写 token、不写白名单**。照着配完 = 全开。
   → :class:`TestSetupGuidesDeclareAnAllowlist`：把引导里的配置片段**真的
   ``json.loads`` 一遍**再断言白名单非空。刻意不写成子串匹配 —— 那样引导写错了
   测试照样绿（这正是本仓库反复踩的"恒真的测试"）。
2. ``--setup --json`` 只看 token，于是 :meth:`Adapter.admits` 的真实语义
   （空 = 全开）被报成"配好了"，而 ``bridge_setup`` 工具会照着这句话转述给用户。
   → :class:`TestSetupJsonReportsExposure`
3. 三个授权键名**先出现者胜**，于是
   ``{"allowed_chat_ids": [], "allowed_chats": [42]}`` 让一个写了非空白名单的用户
   拿到**全开**且没有任何提示。→ :class:`TestConflictingAllowlistKeys`

⚠️ **这些测试不许断言"改动前的样子"**。守住的是**行为与诚实**，不是某句文案：
文案可以再改，而"空 = 全开不许被说成配好了"是承重的。
"""

from __future__ import annotations

import io
import json
import logging
import unittest
from typing import Any

from opencode_bridge import __main__ as cli
from opencode_bridge.adapters import build
from opencode_bridge.allowlist import resolve_allowlist
from opencode_bridge.commands import _SETUP_GUIDES, setup_reply
from opencode_bridge.config import Config


def extract_adapters_block(text: str, anchor: str = '"adapters"') -> dict[str, Any]:
    """从引导文案里抠出 ``"adapters"`` **那个键的值**并**解析**它。

    返回形如 ``{"telegram": {"bot_token": ..., "allowed_chat_ids": [...]}}``。

    按大括号配平（跳过字符串字面量与转义），所以单行与多行片段都能抠出来 ——
    用户是照着复制粘贴的，**能不能被解析本身就是功能的一部分**，所以测试必须
    真的解析，而不是 ``assertIn('"adapters"', text)``。

    ⚠️ 起点是 ``anchor`` **之后**的第一个 ``{``：那是 anchor 的**值**的开括号，
    不是它所在对象的开括号（那个在 anchor 之前）。本函数第一版从 anchor 本身起算，
    于是 ``"adapters"`` 被当成一个完整的 JSON 字符串先吃掉，然后报 "Extra data"。
    """
    anchor_at = text.index(anchor)
    start = text.index("{", anchor_at + len(anchor))
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:index + 1])  # 解析失败 = 测试红，正是要守的
    raise AssertionError(f"从 {anchor!r} 起的括号没有配平：{text[start:start + 200]!r}")


def status_row(cfg: Config, key: str) -> dict[str, Any]:
    for row in cli._platform_status(cfg):
        if row["key"] == key:
            return row
    raise AssertionError(f"平台 {key!r} 不在 _platform_status 输出里")


class _RecordingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class _CaptureLogs:
    """抓某个 logger 的记录。上下文管理器，退出时恢复原 level。"""

    def __init__(self, logger_name: str) -> None:
        self._logger = logging.getLogger(logger_name)
        self._handler = _RecordingHandler()

    def __enter__(self) -> "_CaptureLogs":
        self._previous_level = self._logger.level
        self._logger.setLevel(logging.DEBUG)
        self._logger.addHandler(self._handler)
        return self

    def __exit__(self, *_exc: object) -> None:
        self._logger.removeHandler(self._handler)
        self._logger.setLevel(self._previous_level)

    @property
    def messages(self) -> list[str]:
        return [record.getMessage() for record in self._handler.records]


class TestSetupGuidesDeclareAnAllowlist(unittest.TestCase):
    """Part 1a —— 引导里必须有**非空**白名单，而且那段 JSON 必须能解析。"""

    def test_every_guide_emits_parseable_config_with_non_empty_allowlist(self):
        for platform, guide in _SETUP_GUIDES.items():
            with self.subTest(platform=platform):
                adapters = extract_adapters_block(guide)
                entry = adapters[platform]
                self.assertIn(
                    "allowed_chat_ids", entry,
                    f"{platform} 引导的配置片段里没有 allowed_chat_ids —— "
                    f"照着配完的桥接是**完全开放**的",
                )
                self.assertTrue(
                    entry["allowed_chat_ids"],
                    f"{platform} 引导给出的 allowed_chat_ids 是空的 —— "
                    f"那等于教用户配一个全开的桥",
                )

    def test_guide_warns_that_empty_means_allow_all(self):
        """三份引导都要说清"留空 = 全开"，否则那行非空样例救不了没留心的用户。"""
        for platform, guide in _SETUP_GUIDES.items():
            with self.subTest(platform=platform):
                self.assertIn(
                    "全开", guide,
                    f"{platform} 引导必须说出「留空 = 全开」这个后果",
                )

    def test_allowlist_ids_are_strings_where_the_platform_uses_string_ids(self):
        """Slack / Discord 的入站主体是频道 id（字符串），样例不能写成数字。

        写成数字会让 ``_entries_of`` 转成 ``"12345"`` —— 恰好与真实 id 同形，所以
        **跑起来不会坏**，但样例本身在教一个错的类型；而 telegram 的 chat id 确实是
        数字，引导里那句「数字不要加引号」是承重的。
        """
        for platform in ("slack", "discord"):
            with self.subTest(platform=platform):
                entry = extract_adapters_block(_SETUP_GUIDES[platform])[platform]
                for allowed_id in entry["allowed_chat_ids"]:
                    self.assertIsInstance(
                        allowed_id, str,
                        f"{platform} 的样例 id 应写成字符串（该平台的 id 是字符串）",
                    )

    def test_public_setup_reply_carries_the_allowlist_too(self):
        """``/setup <platform>`` 与 ``--setup <platform>`` 共用同一份文案。

        守的是"公开入口"而不是私有常量：引导若哪天只在 ``setup_reply`` 里拼，
        这个测试会立刻发现它拼丢了白名单。
        """
        for platform in _SETUP_GUIDES:
            with self.subTest(platform=platform):
                reply = setup_reply(platform)
                entry = extract_adapters_block(reply)[platform]
                self.assertTrue(entry["allowed_chat_ids"])


class TestSetupJsonReportsExposure(unittest.TestCase):
    """Part 1b —— ``--setup --json`` 必须报出授权暴露面。"""

    def test_wide_open_platform_is_not_reported_as_ready_for_the_agent(self):
        cfg = Config(adapters={"slack": {"bot_token": "xoxb-t", "app_token": "xapp-t"}})
        row = status_row(cfg, "slack")
        self.assertTrue(row["configured"], "凭据齐备这件事本身不变")
        self.assertTrue(row["accepts_any_sender"])
        self.assertFalse(row["allowlist_configured"])
        self.assertEqual(row["allowed_chat_ids_count"], 0)
        self.assertIn(cli._NOT_READY_NO_ALLOWLIST, row["not_ready_reasons"])
        self.assertFalse(
            row["ready_for_agent"],
            "无白名单（空 = 全开）时不得报成可以告诉用户「配好了」",
        )

    def test_restricted_platform_is_reported_ready(self):
        cfg = Config(
            adapters={"slack": {
                "bot_token": "xoxb-t", "app_token": "xapp-t",
                "allowed_chat_ids": ["C_allow"],
            }}
        )
        row = status_row(cfg, "slack")
        self.assertTrue(row["allowlist_configured"])
        self.assertFalse(row["accepts_any_sender"])
        self.assertEqual(row["allowed_chat_ids_count"], 1)
        self.assertEqual(row["not_ready_reasons"], [])
        self.assertTrue(row["ready_for_agent"])

    def test_unconfigured_platform_reports_only_missing_credentials(self):
        """没配凭据的平台**不报** no_allowlist —— 它收不到消息，说"谁都能驱动"是假话。"""
        row = status_row(Config(adapters={}), "telegram")
        self.assertEqual(row["not_ready_reasons"], [cli._NOT_READY_MISSING_CREDENTIALS])
        self.assertFalse(row["ready_for_agent"])

    def test_configured_keeps_its_original_meaning(self):
        """既有字段一个字没改 —— 否则每个旧消费者都要重新学这套输出。

        这条是**兼容性守卫**：``configured`` 曾经只表示"凭据齐备"，若被顺手改成
        "一切就绪"，Slack 缺 ``app_token`` 之类的既有语义就塌了。
        """
        cfg = Config(adapters={"slack": {"bot_token": "xoxb-t"}})
        row = status_row(cfg, "slack")
        self.assertFalse(row["configured"], "缺 app_token 仍须报未配置")
        self.assertFalse(row["ready_for_agent"])
        self.assertIn(cli._NOT_READY_MISSING_CREDENTIALS, row["not_ready_reasons"])

    def test_empty_allowlist_key_is_reported_as_present_but_empty(self):
        """键写了 ``[]`` 与键没写，结果都是全开；但**"用户写过意图"**这件事要能区分。"""
        absent = status_row(Config(adapters={"telegram": {"bot_token": "t"}}), "telegram")
        written = status_row(
            Config(adapters={"telegram": {"bot_token": "t", "allowed_chat_ids": []}}),
            "telegram",
        )
        self.assertEqual(absent["allowlist_keys_present"], [])
        self.assertEqual(written["allowlist_keys_present"], ["allowed_chat_ids"])
        self.assertTrue(absent["accepts_any_sender"])
        self.assertTrue(written["accepts_any_sender"])
        self.assertFalse(written["ready_for_agent"], "写了空数组同样不许报就绪")

    def test_json_output_is_serialisable_and_carries_the_new_fields(self):
        """整个 payload 必须能 json.dumps —— ``bridge_setup`` 工具要读它。"""
        cfg = Config(adapters={"discord": {"bot_token": "t"}})
        payload = {"config_path": "/tmp/config.json", "platforms": cli._platform_status(cfg)}
        encoded = json.loads(json.dumps(payload, ensure_ascii=False))
        discord = next(r for r in encoded["platforms"] if r["key"] == "discord")
        for field in (
            "allowed_chat_ids_count", "allowlist_configured", "accepts_any_sender",
            "allowlist_keys_present", "allowlist_conflict",
            "not_ready_reasons", "ready_for_agent",
        ):
            self.assertIn(field, discord, f"--setup --json 缺字段 {field}")


class TestConflictingAllowlistKeys(unittest.TestCase):
    """Part 3 —— 键冲突必须**说出来**，且**不改变**哪个键赢。"""

    #: 派单点名的那个洞：非空的意图，拿到的是全开。
    HOLE_CONFIG = {"bot_token": "t", "allowed_chat_ids": [], "allowed_chats": [42]}

    def test_gate_still_admits_everyone(self):
        """⚠️ 这条锁的是**行为不变**：先出现者胜没有被偷偷改掉。

        若将来有人"顺手改成非空优先"，这里会红 —— 而那正是 §8 说的"悄悄换掉一个
        已被依赖的行为"。要改必须先改这里、写明理由与迁移期。
        """
        adapter = build("telegram", dict(self.HOLE_CONFIG), None)
        self.assertEqual(adapter.allowed_chat_ids, set())
        self.assertTrue(adapter.admits("任何人"), "空 = 全开是既有语义，未被改动")
        self.assertTrue(adapter.admits("42"))

    def test_conflict_is_reported_and_states_the_resolved_value(self):
        adapter = build("telegram", dict(self.HOLE_CONFIG), None)
        conflict = adapter.allowlist_resolution.conflict
        self.assertIsNotNone(conflict, "两个键解析结果不同却没报冲突")
        self.assertEqual(conflict.resolved_key, "allowed_chat_ids")
        self.assertEqual(conflict.resolved_count, 0, "必须报出实际生效的是 0 项")
        self.assertEqual(conflict.shadowed_counts, (("allowed_chats", 1),))
        # 说的是"哪个键赢了 + 赢了几项 + 后果"，不是泛泛的"配置有误"
        detail = conflict.describe()
        self.assertIn("allowed_chat_ids", detail)
        self.assertIn("allowed_chats", detail)
        self.assertIn("0 项", detail)
        self.assertIn("全开", detail, "必须说出后果，否则用户不知道要急")

    def test_startup_logs_a_warning_for_the_conflict(self):
        with _CaptureLogs("opencode_bridge.allowlist") as captured:
            build("telegram", dict(self.HOLE_CONFIG), None)
        joined = "\n".join(captured.messages)
        self.assertTrue(
            any("allowed_chats" in message for message in captured.messages),
            f"键冲突必须喊出来。实际日志：{joined!r}",
        )
        self.assertIn(
            "全开", joined,
            "警告必须说出后果（=全开），只说「键冲突」用户仍然不知道严重性",
        )

    def test_single_key_config_is_not_a_conflict(self):
        resolution = resolve_allowlist({"allowed_chat_ids": [1]})
        self.assertIsNone(resolution.conflict)
        self.assertEqual(resolution.entries, frozenset({"1"}))

    def test_two_keys_with_identical_values_are_not_a_conflict(self):
        """值相同 ⇒ 结果毫无歧义，报冲突只是制造噪音。

        但两个键名仍然出现在 ``present_keys`` 里（"你写了两个同义键"对排障有用）。
        """
        resolution = resolve_allowlist(
            {"allowed_chat_ids": ["55"], "allowed_chats": ["55"]}
        )
        self.assertIsNone(resolution.conflict)
        self.assertEqual(resolution.present_keys, ("allowed_chat_ids", "allowed_chats"))
        self.assertEqual(resolution.entries, frozenset({"55"}))

    def test_no_warning_for_a_single_key_config(self):
        with _CaptureLogs("opencode_bridge.allowlist") as captured:
            build("telegram", {"bot_token": "t", "allowed_chat_ids": [55]}, None)
        self.assertEqual(
            [m for m in captured.messages if "allowed_chats" in m], [],
            "只有一个键时不许喊键冲突",
        )

    def test_conflict_dict_is_serialisable_and_complete(self):
        conflict = resolve_allowlist(self.HOLE_CONFIG).conflict
        payload = json.loads(json.dumps(conflict.as_dict(), ensure_ascii=False))
        self.assertEqual(payload["resolved_key"], "allowed_chat_ids")
        self.assertEqual(payload["resolved_count"], 0)
        self.assertEqual(payload["keys"], ["allowed_chat_ids", "allowed_chats"])
        self.assertEqual(
            payload["shadowed"], [{"key": "allowed_chats", "entry_count": 1}]
        )
        self.assertTrue(payload["detail"])

    def test_setup_json_surfaces_the_conflict(self):
        cfg = Config(adapters={"telegram": dict(self.HOLE_CONFIG)})
        row = status_row(cfg, "telegram")
        self.assertIsNotNone(row["allowlist_conflict"], "--setup --json 必须报键冲突")
        self.assertEqual(row["allowlist_conflict"]["resolved_count"], 0)
        self.assertEqual(
            row["allowlist_keys_present"], ["allowed_chat_ids", "allowed_chats"]
        )
        self.assertFalse(row["ready_for_agent"])


class TestWideOpenIsLoud(unittest.TestCase):
    """Part 2a —— 已配置却空=全开 ⇒ 喊出来。"""

    def test_configured_adapter_with_empty_allowlist_warns(self):
        with _CaptureLogs("opencode_bridge.allowlist") as captured:
            build("telegram", {"bot_token": "t"}, None)
        joined = "\n".join(captured.messages)
        self.assertIn("全开", joined, "空白名单必须被说成是全开")
        self.assertIn(
            "allowed_chat_ids", joined,
            "警告必须指出要填哪个键，否则用户不知道动哪里",
        )

    def test_unconfigured_adapter_does_not_warn_about_being_open(self):
        """没凭据的适配器收不到消息 —— 对它喊"谁都能驱动"是假话。"""
        with _CaptureLogs("opencode_bridge.allowlist") as captured:
            build("telegram", {}, None)
        self.assertEqual(
            [m for m in captured.messages if "全开" in m], [],
            "未配置凭据时不许喊全开",
        )

    def test_restricted_adapter_does_not_warn(self):
        with _CaptureLogs("opencode_bridge.allowlist") as captured:
            build("telegram", {"bot_token": "t", "allowed_chat_ids": [55]}, None)
        self.assertEqual(captured.messages, [], "有限白名单时不该有任何告警")

    def test_warning_names_the_actual_platform(self):
        with _CaptureLogs("opencode_bridge.allowlist") as captured:
            build("slack", {"bot_token": "xoxb-t", "app_token": "xapp-t"}, None)
        self.assertTrue(
            any(m.startswith("slack:") for m in captured.messages),
            f"告警必须指明是哪个平台。实际：{captured.messages!r}",
        )


class TestStatusTableShowsTheExposure(unittest.TestCase):
    """Part 2b —— ``--status`` 那列不许再轻描淡写。"""

    def _render(self, cfg: Config) -> str:
        import contextlib

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            cli.run_status(cfg)
        return buffer.getvalue()

    @staticmethod
    def _row_for(rendered: str, label: str) -> str:
        """只取某个平台那一**行**。

        刻意不看整张输出：表头下方那行图例里也有「未设=全开」三个字，整段断言
        会把图例和表格行混为一谈 —— 而这里要守的恰恰是**那一行**。
        """
        for line in rendered.splitlines():
            if line.strip().startswith(label):
                return line
        raise AssertionError(f"--status 输出里找不到 {label} 那一行")

    def test_empty_allowlist_is_rendered_as_an_exposure_not_a_blank(self):
        rendered = self._render(Config(adapters={"telegram": {"bot_token": "t"}}))
        row = self._row_for(rendered, "Telegram")
        self.assertIn("未设=全开", row)
        self.assertNotIn(
            "未设(全开)", row,
            "旧串「未设(全开)」不显眼也没说清后果，已被替换",
        )

    def test_populated_allowlist_still_renders_a_plain_count(self):
        rendered = self._render(
            Config(adapters={"telegram": {"bot_token": "t", "allowed_chat_ids": [1, 2]}})
        )
        row = self._row_for(rendered, "Telegram")
        self.assertIn("2 项", row)
        self.assertNotIn(
            "全开", row,
            "有限白名单的平台不该在那一行被标成全开",
        )

    def test_status_legend_explains_the_column(self):
        rendered = self._render(Config(adapters={}))
        self.assertIn("全开", rendered.split("== 渠道配置与能力 ==")[1][:400])

    def test_conflict_is_listed_below_the_table_with_the_resolved_value(self):
        rendered = self._render(
            Config(adapters={"telegram": {
                "bot_token": "t", "allowed_chat_ids": [], "allowed_chats": [42],
            }})
        )
        self.assertIn("⚠键冲突", rendered)
        self.assertIn("授权键冲突", rendered)
        self.assertIn("allowed_chats", rendered)
        self.assertIn("0 项", rendered)


class TestConfigExampleShipsTheAllowlistKey(unittest.TestCase):
    """``config.example.json`` 被安装脚本原样落盘 ⇒ 它就是每个用户的起点。"""

    EXAMPLE = "config.example.json"

    def test_every_example_adapter_declares_the_allowlist_key(self):
        import os

        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), self.EXAMPLE)
        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
        for platform, entry in document["adapters"].items():
            with self.subTest(platform=platform):
                self.assertIn(
                    "allowed_chat_ids", entry,
                    f"示例配置的 {platform} 段没有 allowed_chat_ids —— "
                    f"用户看不到这个键，也就无从知道要填",
                )

    def test_example_slack_entry_keeps_both_tokens(self):
        """补白名单时**不许**把 ``app_token`` 挤掉（入站必需）。"""
        import os

        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), self.EXAMPLE)
        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
        slack = document["adapters"]["slack"]
        self.assertIn("bot_token", slack)
        self.assertIn("app_token", slack)


if __name__ == "__main__":
    unittest.main()

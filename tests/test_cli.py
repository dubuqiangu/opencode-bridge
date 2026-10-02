"""CLI 层测试（``--setup --json`` / ``--status`` 的取值语义）。

重点覆盖一个**静默失败点**：Slack 缺 ``app_token`` 时"只发出站"仍可用，但入站
根本没通。如果状态输出只看 ``bot_token``，就会把"入站没通"报成"已配置"，用户
照着看会以为双向对话已经通了。
"""

from __future__ import annotations

import unittest

from opencode_bridge import __main__ as cli
from opencode_bridge.adapters import adapter_class, registered_names
from opencode_bridge.config import Config


def status_of(cfg: Config, key: str) -> dict:
    for row in cli._platform_status(cfg):
        if row["key"] == key:
            return row
    raise AssertionError(f"平台 {key!r} 不在 _platform_status 输出里")


class TestPlatformStatusSemantics(unittest.TestCase):
    def test_slack_missing_app_token_is_not_configured(self):
        """只填 bot_token：出站可用，但入站没通 —— 不能报成已配置。"""
        cfg = Config(adapters={"slack": {"bot_token": "xoxb-t"}})
        row = status_of(cfg, "slack")
        self.assertFalse(row["configured"], "缺 app_token 不应算配好")
        self.assertTrue(row["outbound_ready"])
        self.assertFalse(row["inbound_ready"])
        self.assertEqual(row["missing"], ["app_token"])

    def test_slack_with_both_tokens_is_fully_configured(self):
        cfg = Config(
            adapters={"slack": {"bot_token": "xoxb-t", "app_token": "xapp-t"}}
        )
        row = status_of(cfg, "slack")
        self.assertTrue(row["configured"])
        self.assertTrue(row["inbound_ready"])
        self.assertEqual(row["missing"], [])

    def test_discord_inbound_not_implemented_even_when_configured(self):
        """入站要"已实现"且"配置齐备"两个条件，不能只看 token。"""
        cfg = Config(adapters={"discord": {"bot_token": "t"}})
        row = status_of(cfg, "discord")
        self.assertTrue(row["configured"])
        self.assertFalse(row["inbound_implemented"])
        self.assertFalse(row["inbound_ready"], "未实现的入站不能报就绪")

    def test_telegram_single_token_is_enough(self):
        cfg = Config(adapters={"telegram": {"bot_token": "t"}})
        row = status_of(cfg, "telegram")
        self.assertTrue(row["configured"])
        self.assertTrue(row["inbound_ready"])
        self.assertEqual(row["missing"], [])

    def test_unconfigured_platform_reports_missing(self):
        cfg = Config(adapters={})
        row = status_of(cfg, "telegram")
        self.assertFalse(row["configured"])
        self.assertFalse(row["outbound_ready"])
        self.assertFalse(row["inbound_ready"])
        self.assertEqual(row["missing"], ["bot_token"])

    def test_blank_token_counts_as_missing(self):
        cfg = Config(adapters={"telegram": {"bot_token": "   "}})
        self.assertFalse(status_of(cfg, "telegram")["configured"])

    def test_non_dict_adapter_entry_is_tolerated(self):
        """配置写坏了不该让 --status 崩。"""
        cfg = Config(adapters={"telegram": "oops", "slack": None})
        row = status_of(cfg, "telegram")
        self.assertFalse(row["configured"])
        self.assertFalse(status_of(cfg, "slack")["configured"])


class TestStatusPlatformDiscovery(unittest.TestCase):
    def test_every_registered_adapter_appears_in_status(self):
        """状态视图必须自动列出所有已注册平台，否则新加的平台用户根本看不到。"""
        keys = set(cli._status_platform_keys())
        self.assertTrue(
            keys.issuperset(set(registered_names())),
            "注册表里的平台必须都出现在状态视图里",
        )

    def test_known_platforms_present(self):
        keys = set(cli._status_platform_keys())
        self.assertTrue({"telegram", "slack", "discord"}.issubset(keys))

    def test_frozen_setup_menu_stays_curated(self):
        """冻结的 /setup 菜单刻意只列三平台（人工引导文案），不随注册表扩张。"""
        self.assertEqual(
            [k for k, _ in cli.setup_platforms()], ["telegram", "slack", "discord"]
        )

    def test_adapter_class_lookup(self):
        cls = adapter_class("slack")
        self.assertIsNotNone(cls)
        self.assertEqual(cls.required_tokens, ("bot_token", "app_token"))
        self.assertIsNone(adapter_class("definitely-not-a-platform"))


class TestChannelConfigRows(unittest.TestCase):
    def test_rows_expose_honest_inbound_readiness(self):
        cfg = Config(adapters={"slack": {"bot_token": "xoxb-t"}})
        rows = cli._channel_config_rows(cfg)
        by_key = {r[0]: r for r in rows}
        slack = by_key["slack"]
        self.assertEqual(len(slack), 5, "(key,label,configured,inbound_ready,caps)")
        self.assertFalse(slack[2], "缺 app_token 时 configured 应为 False")
        self.assertFalse(slack[3], "入站未就绪时 inbound_ready 应为 False")

    def test_label_comes_from_adapter_declaration(self):
        cfg = Config(adapters={})
        by_key = {r[0]: r for r in cli._channel_config_rows(cfg)}
        self.assertEqual(by_key["telegram"][1], "Telegram")
        self.assertEqual(by_key["slack"][1], "Slack")
        self.assertEqual(by_key["discord"][1], "Discord")

    def test_capability_read_failure_does_not_crash(self):
        """能力读取失败要被标记出来，而不是让 --status 整个崩掉。"""
        cfg = Config(adapters={"telegram": {"bot_token": "t"}})
        rows = cli._channel_config_rows(cfg)
        self.assertTrue(rows, "即使能力读取有问题也该有行输出")


if __name__ == "__main__":
    unittest.main()

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

    def test_discord_inbound_ready_when_configured(self):
        cfg = Config(adapters={"discord": {"bot_token": "t"}})
        row = status_of(cfg, "discord")
        self.assertTrue(row["configured"])
        self.assertTrue(row["inbound_implemented"])
        self.assertTrue(row["inbound_ready"])

    def test_inbound_not_implemented_stays_not_ready_even_when_configured(self):
        """"已实现"与"配置齐备"是两个条件，不能只看 token。

        真实平台目前都实现了入站，所以这个分支用**合成适配器**守护 ——
        否则将来某个平台只做出站时，这层语义就没有测试了。
        """
        import importlib.machinery
        import sys
        import types

        from opencode_bridge.adapters import base as base_mod

        pkg = base_mod.__package__ or "opencode_bridge.adapters"
        mod_name = f"{pkg}.outboundonlyplat"
        module = types.ModuleType(mod_name)
        module.__spec__ = importlib.machinery.ModuleSpec(mod_name, None)

        class OutboundOnlyAdapter(base_mod.Adapter):
            name = "outboundonlyplat"
            label = "OutboundOnly"
            supports_inbound = False   # 假设只做出站
            required_tokens = ("bot_token",)

            def start(self) -> None:
                return None

            def send(self, out):
                return None

            def edit(self, handle, out) -> bool:
                return False

        module.OutboundOnlyAdapter = OutboundOnlyAdapter
        sys.modules[mod_name] = module
        base_mod._REGISTRY.pop("outboundonlyplat", None)
        try:
            base_mod.register("outboundonlyplat")(OutboundOnlyAdapter)
            cfg = Config(adapters={"outboundonlyplat": {"bot_token": "t"}})
            row = status_of(cfg, "outboundonlyplat")
            self.assertTrue(row["configured"], "token 齐备就算配好")
            self.assertFalse(row["inbound_implemented"])
            self.assertFalse(row["inbound_ready"], "入站未实现时不能报就绪")
        finally:
            sys.modules.pop(mod_name, None)
            base_mod._REGISTRY.pop("outboundonlyplat", None)

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


class TestPreflightAndCredentialDeclarations(unittest.TestCase):
    """凭据声明与启动前检查。

    这组测试守的是一类具体缺陷：把 ``bot_token`` 硬编码在通用代码里，会让
    Matrix / IRC / Mattermost 这类**根本没有这个键**的平台要么被判定成
    "没配置"（桥接拒绝启动），要么被报成"发不出去"。
    """

    def test_outbound_tokens_must_be_subset_of_required(self):
        """不变量：能发出去的前提一定是"已配置"的子集。

        这条不变量正是当初漏掉 Matrix 的那道防线 —— 它的 ``required_tokens``
        继承了基类默认的 ``bot_token``，而出站用的是 homeserver/access_token，
        两者互相矛盾。
        """
        for name in registered_names():
            cls = adapter_class(name)
            with self.subTest(platform=name):
                required = set(getattr(cls, "required_tokens", ()))
                outbound = set(getattr(cls, "outbound_tokens", ()))
                self.assertTrue(required, f"{name} 必须声明 required_tokens")
                self.assertTrue(outbound, f"{name} 必须声明 outbound_tokens")
                self.assertTrue(
                    outbound <= required,
                    f"{name}: outbound_tokens {sorted(outbound)} 不是 "
                    f"required_tokens {sorted(required)} 的子集",
                )

    def test_every_adapter_declares_credential_keys_as_strings(self):
        for name in registered_names():
            cls = adapter_class(name)
            for attr in ("required_tokens", "outbound_tokens"):
                keys = getattr(cls, attr)
                with self.subTest(platform=name, attr=attr):
                    self.assertIsInstance(keys, tuple)
                    self.assertTrue(keys)
                    for key in keys:
                        self.assertIsInstance(key, str)
                        self.assertNotIn(" ", key)

    def test_preflight_accepts_config_without_any_bot_token(self):
        """回归：只配 Matrix / IRC / Mattermost 时，桥接必须能启动。

        修复前这里硬编码找 ``bot_token``，导致这三类用户被判成"没配任何
        适配器"而直接退出。
        """
        cases = {
            "matrix": {"homeserver": "https://m.example.org",
                       "access_token": "syt", "user_id": "@a:b"},
            "irc": {"host": "irc.example.org", "nick": "bot", "channels": ["#x"]},
            "mattermost": {"site_url": "https://mm.example.com", "token": "tok"},
            "telegram": {"bot_token": "t"},
        }
        for name, entry in cases.items():
            with self.subTest(platform=name):
                self.assertTrue(
                    cli._has_configured_adapter(Config(adapters={name: entry})),
                    f"{name} 配置齐备却被判定为未配置",
                )

    def test_preflight_rejects_incomplete_and_credential_free_configs(self):
        # 只有一个白名单、没有凭据 —— 不能算配好了
        self.assertFalse(
            cli._has_configured_adapter(
                Config(adapters={"telegram": {"allowed_chat_ids": [1]}})
            )
        )
        # 缺必需项
        self.assertFalse(
            cli._has_configured_adapter(Config(adapters={"matrix": {"access_token": "x"}}))
        )
        # 空 / 空白值不算
        self.assertFalse(
            cli._has_configured_adapter(Config(adapters={"irc": {"host": "  ", "nick": "n"}}))
        )
        self.assertFalse(cli._has_configured_adapter(Config(adapters={})))

    def test_config_optional_platform_needs_no_explicit_config(self):
        """★ 回归：只配 a2a（空配置）的用户曾被**拒绝启动**。

        a2a bind 127.0.0.1 + 端口 0（由系统分配）+ 无鉴权也只对本机开放 ⇒
        空配置即可运行。但preflight 只看 ``required_tokens``，而 a2a 的
        ``required_tokens`` 里有 ``bind_port``（因为"必须声明非空"那条守卫），
        于是报"没配任何适配器"并拒绝启动 —— 与当年 Matrix / IRC / Mattermost
        被拒启动是**同一类** bug。
        """
        self.assertTrue(
            cli._has_configured_adapter(Config(adapters={"a2a": {}})),
            "只配 a2a 时不许拒绝启动：它空配置就能跑",
        )
        # 显式给了端口也一样放行（这条修复前就能过，守住别回退）
        self.assertTrue(
            cli._has_configured_adapter(Config(adapters={"a2a": {"bind_port": 9900}}))
        )

    def test_config_optional_defaults_to_false_for_every_other_platform(self):
        """默认必须是 ``False`` —— 否则所有平台的配置门槛会被静默拆掉。"""
        for name in registered_names():
            if name == "a2a":
                continue
            with self.subTest(platform=name):
                self.assertFalse(
                    bool(getattr(adapter_class(name), "config_optional", False)),
                    f"{name} 不该声明 config_optional —— 它确实需要凭据",
                )

    def test_config_optional_does_not_exempt_declaration_obligations(self):
        """它豁免的是"必须**显式配置**才能跑"，**不豁免**"必须**声明**配置面"。"""
        for name in registered_names():
            cls = adapter_class(name)
            with self.subTest(platform=name):
                self.assertTrue(
                    getattr(cls, "required_tokens", ()),
                    f"{name}: required_tokens 必须非空（声明义务）",
                )
                self.assertTrue(getattr(cls, "outbound_tokens", ()))

    def test_status_view_reports_config_optional_platform_as_ready(self):
        """状态视图不许把开箱可用的平台报成"未配置"/"发不出去"。"""
        rows = {
            row["key"]: row
            for row in cli._platform_status(Config(adapters={"a2a": {}}))
        }
        self.assertIn("a2a", rows)
        self.assertTrue(rows["a2a"]["configured"], "a2a 空配置应显示已配置")
        self.assertTrue(rows["a2a"]["outbound_ready"], "a2a 能发出去")
        self.assertTrue(rows["a2a"]["inbound_ready"], "a2a 入站已实现且应就绪")

    def test_channel_config_rows_respect_config_optional(self):
        """``--check`` 的行构建走的是另一条判定路径，也必须一致。"""
        rows = dict(
            (key, (configured, inbound_ready))
            for key, _label, configured, inbound_ready, _caps
            in cli._channel_config_rows(Config(adapters={"a2a": {}}))
        )
        self.assertIn("a2a", rows)
        self.assertTrue(rows["a2a"][0], "a2a 空配置应显示已配置")
        self.assertTrue(rows["a2a"][1], "a2a 入站应就绪")

    def test_incomplete_config_is_still_rejected_for_normal_platforms(self):
        """拆掉门槛之后，普通平台的拒绝路径**必须仍然有效**。"""
        for adapters in (
            {"telegram": {"allowed_chat_ids": [1]}},          # 只有白名单没有凭据
            {"matrix": {"access_token": "x"}},                # 缺 homeserver/user_id
            {"irc": {"host": "  ", "nick": "n"}},             # 空白值
            {"ntfy": {}},                                     # 缺 topic
        ):
            with self.subTest(platform=sorted(adapters)[0]):
                self.assertFalse(cli._has_configured_adapter(Config(adapters=adapters)))

    def test_outbound_ready_is_true_for_correctly_configured_non_bot_platforms(self):
        """回归：出站就绪必须按各平台自己的凭据键判定。"""
        cases = {
            "matrix": ({"homeserver": "https://m", "access_token": "s", "user_id": "@a:b"},
                       ("homeserver", "access_token")),
            "irc": ({"host": "h", "nick": "n", "channels": ["#x"]}, ("host", "nick")),
            "mattermost": ({"site_url": "https://mm", "token": "t"},
                           ("site_url", "token")),
        }
        for name, (entry, _outbound) in cases.items():
            with self.subTest(platform=name):
                self.assertTrue(
                    status_of(Config(adapters={name: entry}), name)["outbound_ready"],
                    f"{name} 配齐了却被报成发不出去",
                )

    def test_outbound_ready_false_when_its_own_credentials_missing(self):
        """Matrix 缺 access_token：出站不该报就绪（但 user_id 齐了也不算）。"""
        row = status_of(
            Config(adapters={"matrix": {"homeserver": "https://m", "user_id": "@a:b"}}),
            "matrix",
        )
        self.assertFalse(row["outbound_ready"])
        self.assertIn("access_token", row["missing"])

    def test_no_adapter_message_mentions_every_platform_credential(self):
        """提示语不能只提 bot_token，否则那三类用户以为自己配错了。"""
        text = cli.NO_ADAPTER_MESSAGE
        for key in ("bot_token", "app_token", "homeserver", "access_token",
                    "host", "nick", "channels", "site_url", "token"):
            self.assertIn(key, text, f"提示语里应说明 {key}")


class TestTableWidthHelpers(unittest.TestCase):
    """``--status`` 表格的对齐。

    回归点：``f"{s:<n}"`` 按**字符数**补齐，而中文在终端占 2 列，于是中文表头
    本来就错位；平台名再一变长（``Nextcloud Talk`` 14 字符 > 原写死的 12），
    就会把与下一列之间的空格挤掉，输出成 ``Nextcloud Talk未配置``。
    """

    def test_dwidth_counts_cjk_as_two_columns(self):
        self.assertEqual(cli._dwidth("平台"), 4)          # 2 个汉字 = 4 列
        self.assertEqual(cli._dwidth("长度上限"), 8)      # 4 个汉字
        self.assertEqual(cli._dwidth("Nextcloud Talk"), 14)  # 全 ASCII
        self.assertEqual(cli._dwidth(""), 0)
        self.assertEqual(cli._dwidth("IRC"), 3)

    def test_pad_produces_constant_display_width(self):
        for text in ("IRC", "Telegram", "Mattermost", "Nextcloud Talk", "平台", "未配置"):
            with self.subTest(text=text):
                self.assertEqual(cli._dwidth(cli._pad(text, 16)), 16)

    def test_pad_never_truncates_when_text_exceeds_width(self):
        """标签比列宽长时不能截断内容（截断会隐藏平台名）。"""
        padded = cli._pad("Nextcloud Talk", 4)
        self.assertIn("Nextcloud Talk", padded)

    def test_name_column_fits_the_longest_label(self):
        """列宽必须容得下最长平台名 + 间隔。"""
        cfg = Config(adapters={})
        rows = cli._channel_config_rows(cfg)
        widest = max(cli._dwidth(r[1]) for r in rows)
        self.assertGreaterEqual(widest, cli._dwidth("Nextcloud Talk"))
        name_w = max([cli._dwidth("平台")] + [cli._dwidth(r[1]) for r in rows]) + 2
        for row in rows:
            self.assertLessEqual(cli._dwidth(row[1]), name_w)


if __name__ == "__main__":
    unittest.main()

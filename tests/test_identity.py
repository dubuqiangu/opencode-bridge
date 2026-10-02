"""``identity`` 模块测试：统一会话标识 ``platform:local_id``。

重点覆盖两类真实风险：
1. **含冒号的 local 段**（Matrix 房间 id ``!abcDEF:example.org``）——
   解析必须按第一个冒号切，否则会把 local 切碎。
2. **歧义前缀 ``channel:``**（slack/discord/mattermost 共用）——
   缺平台线索时必须抛错，**绝不能猜**。猜错会把用户映射到别人的会话上。
"""

from __future__ import annotations

import unittest

from opencode_bridge.identity import (
    AMBIGUOUS_LEGACY_PREFIXES,
    KNOWN_PLATFORMS,
    AmbiguousConversationId,
    InvalidConversationId,
    format_id,
    is_valid,
    local_of,
    normalize,
    parse_id,
    platform_of,
)


class TestFormatId(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(format_id("slack", "C123"), "slack:C123")
        self.assertEqual(format_id("matrix", "!abc:example.org"),
                         "matrix:!abc:example.org")

    def test_local_may_contain_colon(self):
        """Matrix 房间 id 自带冒号，不能因此报错或被切开。"""
        cid = format_id("matrix", "!abcDEF:example.org")
        self.assertEqual(cid, "matrix:!abcDEF:example.org")
        self.assertEqual(parse_id(cid).local_id, "!abcDEF:example.org")
        self.assertEqual(parse_id(cid).platform, "matrix")

    def test_irc_channel_target(self):
        self.assertEqual(format_id("irc", "#chan"), "irc:#chan")

    def test_rejects_bad_platform(self):
        for bad in ("", "  ", "Slack", "1abc", "a" * 33, "slack-x", "slack:x"):
            with self.subTest(platform=bad):
                with self.assertRaises(InvalidConversationId):
                    format_id(bad, "C1")

    def test_rejects_bad_local(self):
        for bad in ("", "   ", None, True):
            with self.subTest(local=bad):
                with self.assertRaises(InvalidConversationId):
                    format_id("slack", bad)


class TestParseId(unittest.TestCase):
    def test_round_trip(self):
        for cid in ("slack:C1", "telegram:55", "irc:#c", "matrix:!a:b.org",
                    "nextcloud:tok123", "email:me@x.org"):
            with self.subTest(cid=cid):
                parsed = parse_id(cid)
                self.assertEqual(format_id(parsed.platform, parsed.local_id), cid)
                self.assertFalse(parsed.legacy)

    def test_local_keeps_everything_after_first_colon(self):
        parsed = parse_id("matrix:!abc:example.org:8448")
        self.assertEqual(parsed.platform, "matrix")
        self.assertEqual(parsed.local_id, "!abc:example.org:8448")

    def test_accessors(self):
        self.assertEqual(platform_of("slack:C1"), "slack")
        self.assertEqual(local_of("slack:C1"), "C1")

    def test_accessors_return_none_on_legacy_or_broken(self):
        """便捷函数不抛 —— 这是"看一眼"的用法。"""
        for bad in ("chat:55", "channel:C1", "no-colon", "", None, 123):
            with self.subTest(cid=bad):
                self.assertIsNone(platform_of(bad))
                self.assertIsNone(local_of(bad))
                self.assertFalse(is_valid(bad))

    def test_parse_rejects_missing_platform_prefix(self):
        for bad in ("C1", "", "   ", ":C1", None, 123, b"slack:C1"):
            with self.subTest(cid=bad):
                with self.assertRaises(InvalidConversationId):
                    parse_id(bad)


class TestIsValid(unittest.TestCase):
    def test_new_format_is_valid_even_for_unimplemented_platform(self):
        """未知平台只要语法合法就该放行 —— 支持"先换 id、再补适配器"的
        分步迁移，否则中间态无法存在。"""
        self.assertTrue(is_valid("brandnew:C1"))
        self.assertEqual(parse_id("brandnew:C1").platform, "brandnew")

    def test_legacy_prefixes_are_not_new_format(self):
        for cid in ("chat:55", "room:!a:b", "channel:C1"):
            with self.subTest(cid=cid):
                self.assertFalse(is_valid(cid))

    def test_known_platforms_all_satisfy_the_syntax_rule(self):
        import re
        for name in KNOWN_PLATFORMS:
            with self.subTest(platform=name):
                self.assertTrue(is_valid(f"{name}:x"), name)
                self.assertRegex(name, re.compile(r"^[a-z][a-z0-9_]*$"))


class TestNormalize(unittest.TestCase):
    def test_unique_legacy_prefixes(self):
        self.assertEqual(normalize("chat:55"), "telegram:55")
        self.assertEqual(normalize("room:!abc:example.org"),
                         "matrix:!abc:example.org")
        # 这三个本来就是 platform: 形式，应幂等
        for cid in ("irc:#chan", "twitch:foo", "nextcloud:tok"):
            with self.subTest(cid=cid):
                self.assertEqual(normalize(cid), cid)

    def test_idempotent(self):
        for cid in ("slack:C1", "telegram:55", "chat:55", "irc:#c"):
            with self.subTest(cid=cid):
                once = normalize(cid)
                self.assertEqual(normalize(once), once)

    def test_ambiguous_prefix_requires_hint(self):
        """``channel:`` 三家共用 —— 缺线索必须抛错，不能猜。"""
        self.assertIn("channel", AMBIGUOUS_LEGACY_PREFIXES)
        with self.assertRaises(AmbiguousConversationId):
            normalize("channel:C123")

    def test_ambiguous_prefix_with_hint(self):
        for hint, want in (("slack", "slack:C123"),
                           ("discord", "discord:C123"),
                           ("mattermost", "mattermost:C123")):
            with self.subTest(hint=hint):
                self.assertEqual(normalize("channel:C123", platform_hint=hint), want)

    def test_ambiguous_error_is_a_kind_of_invalid(self):
        """上层用 except InvalidConversationId 一把兜住即可。"""
        self.assertTrue(issubclass(AmbiguousConversationId, InvalidConversationId))

    def test_hint_ignored_when_not_needed(self):
        self.assertEqual(normalize("chat:55", platform_hint="slack"), "telegram:55")
        self.assertEqual(normalize("slack:C1", platform_hint="discord"), "slack:C1")

    def test_hint_is_validated(self):
        with self.assertRaises(InvalidConversationId):
            normalize("channel:C1", platform_hint="Bad Platform")

    def test_rejects_unknown_prefix(self):
        """既不是已知平台、也不是已知 legacy 前缀 → 明确报错，不静默放过。"""
        with self.assertRaises(InvalidConversationId):
            normalize("bogus-prefix-thing:xyz")

    def test_all_eight_shipped_adapters_have_a_resolvable_id(self):
        """回归：八个已实现平台用新方案拼出的 id 必须能原样 parse 回来。

        这条是迁移的安全网 —— 适配器改用 :func:`format_id` 后，若某个平台的
        local_id 含冒号（Matrix）或以数字开头（Telegram chat id），这里会抓到。
        """
        shipped = {
            "telegram:55",
            "slack:C0123ABCD",
            "discord:1234567890123456789",
            "matrix:!AbcDEF:example.org",
            "mattermost:abc123def456",
            "irc:#chan",
            "twitch:someuser",
            "nextcloud:tok3n4bl3",
        }
        for cid in shipped:
            with self.subTest(cid=cid):
                self.assertTrue(is_valid(cid))
                self.assertEqual(normalize(cid), cid)

    def test_cross_platform_ids_can_never_collide(self):
        """本次重构的核心保证：不同平台的同名 local id 不再共用一个会话键。"""
        telegram = format_id("telegram", "55")
        slack = format_id("slack", "55")
        self.assertNotEqual(telegram, slack)
        self.assertEqual(platform_of(telegram), "telegram")
        self.assertEqual(platform_of(slack), "slack")

    def test_migrating_channel_prefix_removes_the_collision(self):
        """迁移前 slack 与 discord 的 ``channel:C1`` 是同一个键（= 会话串台）。"""
        before_slack = "channel:C1"
        before_discord = "channel:C1"
        self.assertEqual(before_slack, before_discord)  # 迁移前：撞车
        after_slack = normalize(before_slack, platform_hint="slack")
        after_discord = normalize(before_discord, platform_hint="discord")
        self.assertNotEqual(after_slack, after_discord)  # 迁移后：不再撞车


if __name__ == "__main__":
    unittest.main()

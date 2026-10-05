"""审计**其余 11 个平台**：私聊 principal 是不是 :mod:`irc / twitch` 那种同型陷阱。

拆出来的理由：本类的判据是**从适配器源码 AST 里重扫**得出的，与「起真适配器跑闸门」
（:mod:`tests.test_nick_in_allowlist`）、「真起子进程看桥起不来」
（:mod:`tests.test_bridge_startup_refusal`）是三种不同的证据形态，共享状态为零。

机制说明（为什么这是洞、为什么是拒绝而不是 warning）见
:mod:`tests.test_nick_in_allowlist` 的模块 docstring。本文件不含任何用例改动。
"""

from __future__ import annotations

import ast
import unittest

from opencode_bridge.adapters import build
from tests.inbound_log_support import ALL_PLATFORMS, RecordingHooks
from tests.nick_trap_support import (
    GATE_PRINCIPAL_BY_PLATFORM,
    SELF_IDENTITY_PRINCIPAL_PLATFORMS,
    SELF_IDENTITY_SYMBOLS,
    _module_source,
    _scan_admits_calls,
)


class GatePrincipalAuditTests(unittest.TestCase):
    """审计**其余 11 个平台**：私聊 principal 是不是同型陷阱。**判据列在这里**。

    ## 判据（不是「已确认」，是这四条可复跑的机械判据）

    构成同型陷阱必须**同时**满足：

    1. 闸门 principal **可能等于「bot 自己的身份」** ⇒ ⛔ principal 不能是
       :data:`~tests.nick_trap_support.SELF_IDENTITY_SYMBOLS` 里的任何一个符号；
    2. principal **不能是适配器自己的配置字段** ⇒ ⛔ ``self.<principal>`` 在整个模块里
       **不得**有任何赋值。**实测形态**：其余 11 家的 principal **全部**是从入站报文解析
       出来的**局部变量**或**函数形参**，没有一个是 ``self.`` 属性 —— 这就是「平台权威
       分配」的可查形态；
    3. 判「这是私聊」与跑闸门**不能**用同一个字段 ⇒ 只有 irc / twitch 满足；
    4. 白名单里的「bot 自己身份」最多只授权**一个恰好等于它的会话** ⇒
       :meth:`test_each_other_platform_rejects_an_inbound_from_a_different_conversation`
       **逐平台实测**：白名单里放该平台自己的身份时，来自**别的**会话的入站仍被拒。

    判据 1、2、3 由本类的断言从源码重扫得出；判据 4 由上面那条逐平台跑出来。
    """

    #: 逐平台「principal 从哪来」的人话版，与上面的机械判据一一对应。
    PROVENANCE = {
        "a2a": "A2A 对端 agent id（JSON-RPC 形参 `peer`）",
        "discord": "Discord 消息载荷里的 channel 雪花号",
        "email": "邮件的 `From`（发件人地址）；收件人才是 bot 自己",
        "homeassistant": "HA 事件里的实体 id",
        "matrix": "矩阵服务器分配的 room id（形参 `room_id`）",
        "mattermost": "Mattermost 载荷里的 channel_id",
        "nextcloud": "Nextcloud Talk 服务器分配的 conversation token（形参 `token`）",
        "ntfy": "ntfy 主题名（发布者选的名字，不是 bot 的身份）",
        "qqbot": "group_openid / user_openid / channel_id（**按用户**各不相同）",
        "slack": "Slack 的 `C…`/`D…` 频道 id（服务器分配）",
        "telegram": "Telegram 的 chat id（**每个人的私聊各不相同**）",
    }

    def test_the_audit_covers_every_registered_platform(self):
        """:data:`GATE_PRINCIPAL_BY_PLATFORM` 必须**恰好**覆盖 13 个平台。

        空集 ≠ 不存在（AGENTS.md §7.1）：少一家 = 审计漏了一家，而不是"没问题"。
        """
        self.assertEqual(len(ALL_PLATFORMS), 13)
        self.assertEqual(sorted(GATE_PRINCIPAL_BY_PLATFORM), sorted(ALL_PLATFORMS))

    def test_each_platform_has_exactly_one_gate_principal_symbol(self):
        for platform, expected in sorted(GATE_PRINCIPAL_BY_PLATFORM.items()):
            with self.subTest(platform=platform):
                pairs = _scan_admits_calls(ast.parse(_module_source(platform)))
                self.assertTrue(
                    pairs,
                    f"{platform} 扫到 0 处 self.admits(...) —— 先怀疑判据坏了，"
                    f"不是目标不存在",
                )
                observed = {ast.unparse(call.args[0]) for _fn, call in pairs}
                self.assertEqual(
                    observed, {expected},
                    f"{platform} 的闸门 principal 变了（实测 {sorted(observed)}，"
                    f"表里写的是 {expected!r}）⇒「是否仍是同型陷阱」需要重新回答",
                )

    def test_the_other_eleven_never_gate_on_a_self_identity_symbol(self):
        """判据 1：principal 不是「自己是谁」。"""
        offenders = [
            f"{platform}: 闸门 principal 就是 {principal!r}"
            for platform, principal in sorted(GATE_PRINCIPAL_BY_PLATFORM.items())
            if platform not in SELF_IDENTITY_PRINCIPAL_PLATFORMS
            and principal in SELF_IDENTITY_SYMBOLS
        ]
        self.assertEqual(offenders, [], f"判据 1 被破了：{offenders}")

    def test_the_other_eleven_principal_is_never_a_config_derived_attribute(self):
        """判据 2：``self.<principal>`` 在模块里**一次都不许被赋值**。"""
        offenders: list[str] = []
        for platform, principal in sorted(GATE_PRINCIPAL_BY_PLATFORM.items()):
            if platform in SELF_IDENTITY_PRINCIPAL_PLATFORMS:
                continue
            tree = ast.parse(_module_source(platform))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Assign):
                    continue
                for target in node.targets:
                    if (isinstance(target, ast.Attribute) and target.attr == principal
                            and isinstance(target.value, ast.Name)
                            and target.value.id == "self"):
                        offenders.append(
                            f"{platform}: self.{principal} = "
                            f"{ast.unparse(node.value)[:60]}"
                        )
        self.assertEqual(
            offenders, [],
            "判据 2 被破了：这些平台的 principal 变成了适配器自己的字段 ⇒ "
            "「平台权威分配」不再成立，必须重审：\n  " + "\n  ".join(offenders),
        )

    def test_each_other_platform_rejects_an_inbound_from_a_different_conversation(self):
        """判据 4：**逐平台实测**闸门真的按那个 principal 拒。

        白名单里放**该平台自己的身份字符串**，再拿一个**不同的**会话标识去过闸门 ——
        必须被拒。这证明「把 bot 自己的身份写进白名单」在这 11 家里**不会**顺带授权
        别人的私聊，因为它们的私聊 principal 不是同一个字符串。
        """
        own_identity = {
            "telegram": "bot-own-user-id",
            "slack": "B0OWNBOTUSERID",
            "discord": "0",
            "matrix": "@thebot:example.org",
            "email": "thebot@example.org",
            "nextcloud": "thebot",
            "qqbot": "thebot-openid",
            "a2a": "peer-self",
            "homeassistant": "automation.thebot",
            "ntfy": "thebot",
            "mattermost": "thebot-user-id",
        }
        cases = {
            "a2a": ({"host": "127.0.0.1", "port": 0}, "peer-from-the-wire"),
            "discord": ({"bot_token": "token"}, "999999999999999999"),
            "email": ({"imap_host": "imap.example.org", "username": "u"},
                      "someone@example.org"),
            "homeassistant": ({"url": "http://h", "token": "t"}, "light.elsewhere"),
            "matrix": ({"homeserver": "http://h", "access_token": "t",
                        "user_id": "@thebot:example.org"}, "!room:example.org"),
            "mattermost": ({"site_url": "http://h", "token": "t"}, "channel-from-wire"),
            "nextcloud": ({"base_url": "http://h", "user": "u", "password": "p"},
                          "tok-from-wire"),
            "ntfy": ({"server": "http://h", "topic": "own-topic"}, "other-topic"),
            "qqbot": ({"app_id": "a", "secret": "s", "token": "t"},
                      "group-openid-from-wire"),
            "slack": ({"bot_token": "xoxb-x", "app_token": "xapp-x"}, "C0FROMWIRE"),
            "telegram": ({"bot_token": "123:ABC"}, "-1009999999999"),
        }
        self.assertEqual(sorted(cases), sorted(self.PROVENANCE),
                         "判据 4 的用例与审计表对不上：加平台时两处都要补")
        for platform, (credentials, other_principal) in sorted(cases.items()):
            with self.subTest(platform=platform):
                config = dict(credentials)
                config["allowed_chat_ids"] = [own_identity[platform]]
                config["config_version"] = 2
                adapter = build(platform, config, RecordingHooks())
                self.assertFalse(
                    adapter.admits(other_principal),
                    f"{platform}: 白名单里只有它自己的身份 "
                    f"({own_identity[platform]!r})，来自别的会话的入站 "
                    f"({other_principal!r}) 却被放行了 ⇒ 这就是同型陷阱",
                )

    def test_only_irc_and_twitch_compare_the_principal_with_their_own_nick(self):
        """判据 3：只有这两家拿 principal 与 ``self.nick`` 比过（两个方向都要成立）。"""
        offenders: list[str] = []
        for platform, principal in sorted(GATE_PRINCIPAL_BY_PLATFORM.items()):
            source = _module_source(platform)
            compares = (
                f"{principal}.lower() == self.nick.lower()" in source
                or f"self.nick.lower() == {principal}.lower()" in source
            )
            if compares and platform not in SELF_IDENTITY_PRINCIPAL_PLATFORMS:
                offenders.append(platform)
        self.assertEqual(offenders, [], f"判据 3 被破了：{offenders}")
        for platform in sorted(SELF_IDENTITY_PRINCIPAL_PLATFORMS):
            with self.subTest(platform=platform):
                self.assertIn(
                    f"{GATE_PRINCIPAL_BY_PLATFORM[platform]}.lower() == "
                    f"self.nick.lower()",
                    _module_source(platform),
                    f"{platform} 的 is_private 判定写法变了 ⇒ "
                    f"「principal 与自己身份同形」这个前提要重新核实",
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

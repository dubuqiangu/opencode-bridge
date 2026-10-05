"""私聊被拒时不能打「not-whitelisted」那句通用文案（对私聊是假话）。

拆出来的理由：这一组断言的是**三处逐字一致**（两处适配器源码 + README 排障表）
外加脱敏层之后那一层，依赖 :data:`~opencode_bridge.allowlist.NO_SENDER_IDENTITY_REASON`
与 :data:`~tests.nick_trap_support.README_PATH`；它**只读文件**，不构造闸门跑授权，
也不起进程。

⚠️ 其中 :meth:`PrivateMessageRejectionWordingTests.test_the_space_before_the_reason_is_load_bearing`
钉的是「判据前面那个空格不许被整理掉」—— 那条断言与脱敏层的会话 id 形状是一对，
动它等于改动被测行为，本文件只做搬家。

机制说明见 :mod:`tests.test_nick_in_allowlist` 的模块 docstring。
本文件不含任何用例改动。
"""

from __future__ import annotations

import unittest

from opencode_bridge.adapters.irc import IRCAdapter
from opencode_bridge.adapters.twitch import TwitchAdapter
from opencode_bridge.allowlist import NO_SENDER_IDENTITY_REASON
from tests.inbound_log_support import CapturedLogs, RecordingHooks
from tests.nick_trap_support import (
    README_PATH,
    SELF_IDENTITY_PRINCIPAL_PLATFORMS,
    _module_source,
    _read_text,
)


class PrivateMessageRejectionWordingTests(unittest.TestCase):
    """私聊被拒时不能打「not-whitelisted」那句通用文案（对私聊是假话）。"""

    def test_the_private_drop_line_says_no_sender_identity(self):
        with CapturedLogs("irc") as handler:
            adapter = IRCAdapter(
                {"host": "127.0.0.1", "nick": "MyBot", "channels": "#chan",
                 "allowed_chat_ids": ["#chan"], "config_version": 2},
                RecordingHooks(),
            )
            adapter._handle_privmsg(":alice!a@h", ["MyBot", "hello bot"])
        joined = "\n".join(handler.lines)
        self.assertTrue(handler.lines, "私聊被拒却一行日志都没记 —— 这条在量空集")
        self.assertIn(NO_SENDER_IDENTITY_REASON, joined)
        self.assertNotIn(
            "non-whitelisted target", joined,
            "私聊路径不该再打那句通用文案：真相是「没有可用的发件人身份」，"
            "不是「你不在名单里」—— 照着假话查白名单会把人引到错的方向",
        )

    def test_the_channel_drop_line_is_unchanged(self):
        """频道那一句**逐字不动**：它是 grep 锚点，不能顺手改。"""
        with CapturedLogs("twitch") as handler:
            adapter = TwitchAdapter(
                {"token": "oauth:abcdefgh", "channel": "chan", "nick": "MyBot",
                 "allowed_chat_ids": ["#allowed"], "config_version": 2},
                RecordingHooks(),
            )
            adapter._handle_privmsg(
                ":alice!a@h PRIVMSG #other :MyBot: hello",
                {"id": "m-1"}, ":alice!a@h", ["#other", "MyBot: hello"],
            )
        joined = "\n".join(handler.lines)
        self.assertTrue(handler.lines, "频道被拒却一行日志都没记 —— 这条在量空集")
        self.assertIn("dropping message from non-whitelisted channel", joined)
        self.assertNotIn(
            NO_SENDER_IDENTITY_REASON, joined,
            "频道路径不该说「没有可用的发件人身份」—— 频道名是真会话标识",
        )

    def test_the_private_reason_string_is_pinned_in_both_sources_and_the_readme(self):
        """代码里的两处与 README 排障表**钉在一起**（fix-118 的形状）。

        改任何一处而不同步另两处 ⇒ 这里红。三处都做**逐字**包含、不做规范化 ——
        否则"改得几乎一样"会蒙过去，而用户是照着日志搜的。
        """
        readme = _read_text(README_PATH)
        for platform in sorted(SELF_IDENTITY_PRINCIPAL_PLATFORMS):
            with self.subTest(platform=platform):
                self.assertIn(
                    NO_SENDER_IDENTITY_REASON, _module_source(platform),
                    f"{platform} 的私聊丢弃文案里那句判据不见了",
                )
        self.assertIn(
            NO_SENDER_IDENTITY_REASON, readme,
            "README 排障表必须引用这句判据（用户照着它搜日志）",
        )
        self.assertIn(
            "dropping private message from non-whitelisted nick", readme,
            "README 排障表必须给出 irc / twitch 私聊被拒的那条日志前缀",
        )

    def test_the_refusal_consequence_is_pinned_in_the_readme_too(self):
        self.assertIn(
            "这一条会把所有人的私聊一起放行", _read_text(README_PATH),
            "「拒绝启动」那条排障说明必须逐字复用后果那句话",
        )

    def test_the_space_before_the_reason_is_load_bearing(self):
        """判据前面那个**空格不许被"整理掉"**。

        脱敏层的会话 id 形状是 ``<平台>:<后面所有非空白字符>``
        （:data:`opencode_bridge.redaction._CONVERSATION_ID_PATTERN`）——
        它会**连中文一起吞掉**。所以 ``%s（判据）`` 渲染再脱敏之后，判据
        **根本不会落盘**（实测落盘只剩前半句），而这正是本条断言存在的原因：
        它把"那句话真的进了日志"钉在**渲染 + 脱敏之后**那一层，而不是源码里。

        断言用的是 :class:`~opencode_bridge.redaction.Redactor` 本尊（脱敏层就是它）。
        """
        from opencode_bridge.redaction import Redactor

        redactor = Redactor(key=b"nick-in-allowlist-test-key-32b!")
        reason = NO_SENDER_IDENTITY_REASON
        with_space = redactor.scrub(
            "irc: dropping private message from non-whitelisted nick "
            "irc:SomeNick " + reason
        )
        without_space = redactor.scrub(
            "irc: dropping private message from non-whitelisted nick "
            "irc:SomeNick" + reason
        )
        self.assertIn(
            reason, with_space,
            "判据与 id 之间有空格时判据应当落盘",
        )
        self.assertNotIn(
            reason, without_space,
            "这条断言的前提变了：脱敏层不再吞掉紧跟 id 的中文 ⇒ "
            "那两个 %s 后的空格可以（但不必）去掉了",
        )
        # 而真实落盘的那一行必须真的有判据（与上面第二条互为对照）。
        with CapturedLogs("irc") as handler:
            adapter = IRCAdapter(
                {"host": "127.0.0.1", "nick": "MyBot", "channels": "#chan",
                 "allowed_chat_ids": ["#chan"], "config_version": 2},
                RecordingHooks(),
            )
            adapter._handle_privmsg(":alice!a@h", ["MyBot", "hello bot"])
        self.assertIn(reason, "\n".join(handler.lines))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

""":func:`opencode_bridge.adapters._redactable_ids.redactable_id` 自身的契约。

**这一份只管那个函数**：空 local 段不抛、失败关闭、已带前缀幂等、13 个平台的
``name`` 全是合法平台键，以及它与各适配器自己的 ``conversation_id`` 产出同一个
字符串。

另两份：
:mod:`tests.test_inbound_log_structure`（结构断言）、
:mod:`tests.test_inbound_log_per_platform`（逐平台跑真实拒绝路径）。
共享夹具在 :mod:`tests.inbound_log_support`。
"""

from __future__ import annotations

import unittest

from opencode_bridge import identity
from opencode_bridge.adapters import registered_names
from opencode_bridge.adapters._redactable_ids import MISSING_ID, redactable_id
from opencode_bridge.adapters.a2a import A2aAdapter
from opencode_bridge.adapters.discord import DiscordAdapter
from opencode_bridge.adapters.email import EmailAdapter
from opencode_bridge.adapters.homeassistant import HomeAssistantAdapter
from opencode_bridge.adapters.irc import IRCAdapter
from opencode_bridge.adapters.matrix import MatrixAdapter
from opencode_bridge.adapters.mattermost import MattermostAdapter
from opencode_bridge.adapters.nextcloud import NextcloudAdapter
from opencode_bridge.adapters.qqbot import QQBotAdapter
from opencode_bridge.adapters.slack import SlackAdapter
from opencode_bridge.adapters.telegram import TelegramAdapter
from opencode_bridge.adapters.twitch import TwitchAdapter
from opencode_bridge.redaction import Redactor
from tests.inbound_log_support import ALL_PLATFORMS, FIXED_REDACTION_KEY


# ======================================================================
# 1. redactable_id 自身的契约
# ======================================================================
class RedactableIdTests(unittest.TestCase):
    def test_a_real_local_id_becomes_the_platform_prefixed_form(self):
        self.assertEqual(redactable_id("discord", "123456789012345678"),
                         "discord:123456789012345678")

    def test_a_missing_local_id_never_raises_and_never_leaks(self):
        """``_conversation_id`` 对空 local **抛**是对的；日志路径不能抛 ——
        "缺 channel_id"那一支恰恰要把"没有"这件事说出来。"""
        for empty in ("", "   ", None, 0):
            with self.subTest(empty=empty):
                self.assertEqual(redactable_id("discord", empty), MISSING_ID)

    def test_the_missing_placeholder_itself_is_not_scrubbed(self):
        """``platform:?`` 会被洗成 ``conv#<摘要>("?")`` —— 那就把"这里本来就没有
        id"洗成了一个看起来很像真摘要的东西。裸 ``?`` 不匹配任何规则，原样留下。"""
        self.assertEqual(Redactor(key=FIXED_REDACTION_KEY).scrub("channel=%s" % MISSING_ID),
                         "channel=%s" % MISSING_ID)

    def test_an_invalid_platform_key_fails_closed(self):
        """平台键非法时**绝不**退回打原值。"""
        self.assertEqual(redactable_id("Not A Platform", "secret-id"), MISSING_ID)
        self.assertEqual(redactable_id("", "secret-id"), MISSING_ID)

    def test_an_already_prefixed_value_is_returned_unchanged(self):
        """``qqbot`` 的 conversation 段进来时**已经**是 ``qqbot:<scope>:<target>``；
        拼两次不能变成 ``qqbot:qqbot:group:...``。"""
        already = "qqbot:group:B2C3D4E5"
        self.assertEqual(redactable_id("qqbot", already), already)
        self.assertEqual(redactable_id("qqbot", redactable_id("qqbot", "group:B2C3D4E5")),
                         already)

    def test_every_registered_platform_name_is_a_valid_platform_key(self):
        """把 ``redactable_id`` 那个"失败关闭"分支**结构上**堵死。

        :data:`MISSING_ID` 只有一种成因（平台侧没给 id）才讲得通；平台键非法是
        它的第二个成因，而一个符号两种含义就是歧义。所以 13 个 ``name`` 必须是
        合法平台键 —— 这条断言就是那个前提。
        """
        names = list(registered_names())
        self.assertEqual(sorted(names), sorted(ALL_PLATFORMS),
                         "已注册的平台变了：要么新增了平台（逐平台测试要补一条），"
                         "要么少了一个（下面的断言会漏掉它）")
        for name in names:
            with self.subTest(platform=name):
                self.assertTrue(identity.is_valid(redactable_id(name, "probe-local-id")))

    def test_it_agrees_with_each_adapter_own_conversation_id(self):
        """日志里那一段必须与**真正会进** ``Inbound.conversation_id`` 的值一致。

        不一致的后果是"日志与会话表对不上"成了排障时的假线索 —— 而那比明文更难查，
        因为它看起来像真话。
        """
        probe = "PROBE-LOCAL-ID"
        #: ``staticmethod`` 那一批：可以直接按未绑定函数调。
        for platform, conversation_id_of in (
            ("telegram", TelegramAdapter._conversation_id),
            ("slack", SlackAdapter._conversation_id),
            ("discord", DiscordAdapter._conversation_id),
            ("matrix", MatrixAdapter._conversation_id),
            ("mattermost", MattermostAdapter._conversation_id),
            ("irc", IRCAdapter._conversation_id),
            ("twitch", TwitchAdapter._conversation_id),
            ("nextcloud", NextcloudAdapter._conversation_id),
            ("homeassistant", HomeAssistantAdapter.conversation_id_for),
        ):
            with self.subTest(platform=platform):
                self.assertEqual(redactable_id(platform, probe),
                                 conversation_id_of(probe))
        #: ``email`` / ``a2a`` 的 ``_conversation_id`` 是**实例方法**但只用类属性
        #: ``name``，所以一个 ``__new__`` 空壳就够（不去跑 ``__init__``）。
        for platform, cls in (("email", EmailAdapter), ("a2a", A2aAdapter)):
            with self.subTest(platform=platform):
                self.assertEqual(redactable_id(platform, probe),
                                 cls._conversation_id(cls.__new__(cls), probe))
        #: qqbot 的 conversation 段**由 scope + target 拼成**，且 local 段**含冒号**
        #: （``group:openid``）—— 单列一条把它钉住。
        self.assertEqual(redactable_id("qqbot", "group:" + probe),
                         QQBotAdapter.conversation_id_for("group", probe))
        # ntfy 没有 ``_conversation_id`` 方法：它就地 ``format_id(self.name, topic)``。
        self.assertEqual(redactable_id("ntfy", probe), identity.format_id("ntfy", probe))

    def test_the_output_is_actually_scrubbed_by_the_redaction_engine(self):
        """**阴性对照**：前缀形式被 C2 洗掉，而裸值原样留着。

        没有这一条，上面那些"结构对"的断言可能整体量错了对象。
        """
        redactor = Redactor(key=FIXED_REDACTION_KEY)
        prefixed = redactor.scrub("channel=%s" % redactable_id("discord", "123456789012345678"))
        self.assertNotIn("123456789012345678", prefixed)
        self.assertRegex(prefixed, r"channel=discord:conv#[0-9a-f]{6}-[0-9a-f]{6}")
        bare = redactor.scrub("channel=%s" % "123456789012345678")
        self.assertIn("123456789012345678", bare)

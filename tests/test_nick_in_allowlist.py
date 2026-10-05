"""**「把自己的 nick 写进白名单」= 给所有人的私聊开门** —— 这条洞被拒绝，不是被警告。

## 陷阱的机制（不是推测，是这两行的直接后果）

irc / twitch 的私聊报文是 ``PRIVMSG <target> :<body>``，而 ``<target>`` 是
**「发给谁」= bot 自己**。两个适配器判「这是私聊」与跑授权闸门**用的是同一个
``target``** ⇒ 私聊的 principal 是 bot 自己的 nick ⇒ **所有私聊共用同一个
principal**。于是用户为了「让私聊能用」把 ``nick`` 填进 ``allowed_chat_ids``，
闸门就把**任何人**的私聊当成已授权放进来。

仓库自己的测试把这个事实写成了期望值：
``tests/test_irc.py::TestIRCInbound::test_private_message_becomes_inbound``
断言 ``conversation_id == f"irc:{NICK}"`` —— 私聊会话就是「bot 的 nick」。

## 为什么是**拒绝**而不是 warning

这修的是**用户主动把陷阱踩下去**的那条路（不是有人在攻击）。用户以为自己授权了
"自己一个人"，实际授权的是全世界的私聊 —— **静默**才是它危险的地方。所以
``IRCAdapter.nick`` 的 setter 抛
:class:`~opencode_bridge.adapters.base.AdapterError`：适配器**构造不出来** ⇒
私聊那条路压根不存在。

⚠️ **定性**：修完之后 irc / twitch 的私聊**依然不可用** —— IRC 协议层没有发送者
身份，做凭据认证（NickServ / SASL）**依赖服务器是否强制**，我们没有那个信息，
猜一个身份等于开门。所以这不是「功能修复」，是「把静默的洞变成明确的拒绝」。

## 「nick 变成已知值的每一个时刻」怎么被同一个检查点覆盖

因为 ``nick`` 是 **property**，写入只有一个入口（setter）。覆盖范围因此不是
「我数出来的几个赋值点」，而是**将来任何人新加的任何一行 ``self.nick = ...``**。
见 :class:`~tests.test_nick_checkpoint.NickCheckpointTests`。

## 变异实验

``.tmp/nick_trap/mutation_experiment.py``：3 个真变异（只警告 / 只查 ``__init__`` /
去掉大小写折叠）+ 2 个对照（改注释 / 改无关文案）。每处都校验：文件 SHA-256 应用后
变化、还原后逐字节相同、锚点在文件里的命中数**恰好 1**（≠1 即中止报错）。

## 这一组测试按职责分成了六个文件（纯结构调整，用例逐字未变）

| 文件 | 承载的类 / 内容 |
|---|---|
| ``tests/test_nick_in_allowlist.py``（本文件） | :class:`NickInAllowlistTests` |
| ``tests/test_nick_checkpoint.py`` | :class:`~tests.test_nick_checkpoint.NickCheckpointTests` |
| ``tests/test_gate_principal_audit.py`` | :class:`~tests.test_gate_principal_audit.GatePrincipalAuditTests` |
| ``tests/test_private_rejection_wording.py`` | :class:`~tests.test_private_rejection_wording.PrivateMessageRejectionWordingTests` |
| ``tests/test_bridge_startup_refusal.py`` | :class:`~tests.test_bridge_startup_refusal.BridgeStartupRefusalTests`（**真起子进程**） |
| ``tests/nick_trap_support.py`` | 上述文件共用的常量与源码扫描工具（**无用例**，不被 discover 收集） |

依赖方向只有一条：``nick_trap_support`` ← 其余五个，其余五个彼此无依赖。
"""

from __future__ import annotations

import unittest

from opencode_bridge.adapters import build
from opencode_bridge.adapters.base import AdapterError, adapter_class
from opencode_bridge.adapters.irc import IRCAdapter
from opencode_bridge.adapters.twitch import TwitchAdapter
from opencode_bridge.allowlist import (
    describe_nick_in_allowlist,
    nick_in_allowlist,
)
from tests.inbound_log_support import ALL_PLATFORMS, RecordingHooks
from tests.nick_trap_support import SELF_IDENTITY_PRINCIPAL_PLATFORMS


class NickInAllowlistTests(unittest.TestCase):
    """拒启动、拒运行期赋值、拒大小写变体。"""

    def _irc(self, config: dict) -> IRCAdapter:
        base = {"host": "127.0.0.1", "nick": "MyBot", "channels": "#chan"}
        base.update(config)
        return IRCAdapter(base, RecordingHooks())

    def _twitch(self, config: dict) -> TwitchAdapter:
        base = {"token": "oauth:abcdefgh", "channel": "chan", "nick": "MyBot"}
        base.update(config)
        return TwitchAdapter(base, RecordingHooks())

    # -- 验收条件 1：触发即拒绝 ---------------------------------------------
    def test_whitelisting_own_nick_refuses_to_build_the_adapter(self):
        """白名单里有 bot 自己的 nick ⇒ **适配器构造不出来**。

        走 :func:`~opencode_bridge.adapters.build`（**真实工厂**，与
        ``__main__._run_bridge_locked`` 调的是同一个），而不是直接 new 一个类 ——
        否则测的是"类会抛"，不是"启动路径上真的会撞上"。
        """
        for platform, config in (
            ("irc", {"host": "127.0.0.1", "nick": "MyBot", "channels": "#chan",
                     "allowed_chat_ids": ["#chan", "mybot"]}),
            ("twitch", {"token": "oauth:abcdefgh", "channel": "chan", "nick": "MyBot",
                        "allowed_chat_ids": ["#chan", "mybot"]}),
        ):
            with self.subTest(platform=platform):
                with self.assertRaises(AdapterError) as caught:
                    build(platform, config, RecordingHooks())
                self.assertIn(
                    "会把所有人的私聊一起放行", str(caught.exception),
                    "错误文案必须说清后果：用户只删一行配置就能修好，"
                    "但他得先知道那一行的后果是什么才会去删",
                )

    def test_the_trap_is_real_so_the_check_is_not_vacuous(self):
        """证明这个洞**本来是真的**，所以拒绝它不是空转。

        把 nick 加进 ``allowed_chat_ids`` 之后，闸门对「私聊的那个 target」返回
        ``True`` —— 而私聊的 target 按定义就是 bot 自己的 nick
        （irc / twitch 的 ``is_private`` 那一行），于是**所有**私聊都被放行。
        """
        adapter = self._irc({"allowed_chat_ids": ["#chan"]})
        self.assertFalse(adapter.admits(adapter.nick), "前置条件：默认应当被拒")
        adapter.allowed_chat_ids = {"#chan", adapter.nick}
        self.assertTrue(
            adapter.admits(adapter.nick),
            "闸门确实会把 bot 自己的 nick 当已授权 —— 这就是被封掉的那扇门",
        )

    # -- 验收条件 2：casefold 比 lower 更严 ---------------------------------
    def test_case_variants_of_own_nick_are_all_refused(self):
        for nick in ("MyBot", "mybot", "MYBOT", "mYbOt"):
            for entry in ("MyBot", "mybot", "MYBOT", "mYbOt"):
                with self.subTest(nick=nick, allowlist_entry=entry):
                    with self.assertRaises(AdapterError):
                        self._irc({"allowed_chat_ids": ["#chan", entry]})
                    with self.assertRaises(AdapterError):
                        self._twitch({"allowed_chat_ids": ["#chan", entry]})

    def test_casefold_catches_what_lower_would_miss(self):
        """判定式确实用 ``casefold()``，不是 ``lower()``。

        ``"ß".casefold() == "ss"`` 而 ``"ß".lower() == "ß"``：这是一对
        ``lower()`` 判不出来、``casefold()`` 判得出来的输入。断言的是**判定式本身**
        （:func:`nick_in_allowlist`），所以将来有人把 ``casefold`` 换成 ``lower``
        这里就红 —— 那正是"去掉大小写折叠"那个真变异。
        """
        self.assertEqual("ß".casefold(), "ss")
        self.assertNotEqual("ß".lower(), "ss")
        self.assertEqual(nick_in_allowlist("ß", {"ss"}), "ss")
        self.assertIsNone(nick_in_allowlist("ß", {"ss-not-the-same"}))
        with self.assertRaises(AdapterError):
            self._irc({"nick": "ß", "allowed_chat_ids": ["#chan", "ss"]})

    # -- 验收条件 3：比的是闸门真正用的那一份 -------------------------------
    def test_the_checkpoint_reads_the_gate_set_not_a_reparse(self):
        """改 ``allowed_chat_ids`` 之后，下一次 nick 赋值按**改后的**那份判。

        这就是「比的是闸门真正用的那个集合」的可复跑证据：若实现改成重新解析
        ``self.config``，构造后改 ``allowed_chat_ids`` 就不影响判定 ⇒ 这里会红。
        """
        adapter = self._irc({"allowed_chat_ids": ["#chan"]})
        self.assertEqual(adapter.nick, "MyBot")
        adapter.allowed_chat_ids = {"#chan", "MyBot"}
        with self.assertRaises(AdapterError):
            adapter.nick = "MyBot"

    def test_a_nick_that_is_not_whitelisted_is_accepted(self):
        """反向：不是自己身份的 nick 照常能写（不许把检查写成"永远拒"）。"""
        adapter = self._irc({"allowed_chat_ids": ["#chan", "someone"]})
        self.assertEqual(adapter.nick, "MyBot")
        adapter.nick = "OtherBot"
        self.assertEqual(adapter.nick, "OtherBot")
        self.assertEqual(adapter.nick, adapter._nick)   # property 真的接上了

    # -- 验收条件 4：twitch 的运行期赋值 ------------------------------------
    #: Helix 下发的那份身份（``login`` / ``user_id`` / ``display_name``）。
    HELIX_IDENTITY = [{"id": "99999", "login": "helixbot", "display_name": "HelixBot"}]

    def _twitch_with_stubbed_helix(self, config: dict) -> TwitchAdapter:
        adapter = self._twitch(config)
        adapter._http_get = lambda url: (200, {"data": list(self.HELIX_IDENTITY)})
        return adapter

    def test_twitch_runtime_helix_nick_is_refused_even_without_configured_nick(self):
        """**配置里没有 nick**，运行期 Helix 下发的 nick 命中白名单 ⇒ 同样被拒。

        这是「不能在 ``__init__`` 查一次」的判据：
        :meth:`TwitchAdapter._resolve_identity` 里的 ``self.nick = login`` 是
        **运行期**赋值，只查 ``__init__`` 的实现过不了这条。
        """
        adapter = self._twitch_with_stubbed_helix(
            {"nick": "", "client_id": "cid", "user_id": "",
             "allowed_chat_ids": ["#chan", "helixbot"]}
        )
        self.assertEqual(adapter.nick, "", "前置条件：配置里确实没有 nick")
        with self.assertRaises(AdapterError) as caught:
            adapter._resolve_identity()
        self.assertIn("会把所有人的私聊一起放行", str(caught.exception))
        self.assertEqual(adapter.nick, "", "被拒之后 nick 必须仍是空，不是半写进去")

    def test_twitch_runtime_helix_nick_not_whitelisted_is_still_accepted(self):
        """反向：Helix 给的 nick 不在白名单 ⇒ 运行期赋值照常成功。"""
        adapter = self._twitch_with_stubbed_helix(
            {"nick": "", "client_id": "cid", "user_id": "", "allowed_chat_ids": ["#chan"]}
        )
        adapter._resolve_identity()
        self.assertEqual(adapter.nick, "helixbot")

    def test_irc_has_no_runtime_nick_source_and_still_checks_at_construction(self):
        """irc 的 nick 只来自配置；构造那一次就拒。

        （对照 twitch：那边多一条运行期来源，所以那条路径也必须有断言。）
        """
        with self.assertRaises(AdapterError):
            self._irc({"allowed_chat_ids": ["#chan", "MyBot"]})

    # -- 验收条件 5：只有 irc / twitch --------------------------------------
    def test_only_irc_and_twitch_carry_the_nick_checkpoint(self):
        """13 个平台里，**只有** irc / twitch 的 ``nick`` 是带检查的 property。

        这条同时是给下一个平台的**提示**：哪天有平台把「自己是谁」当成私聊
        principal，它必须像这两家一样主动挂上检查 —— 而这条断言会提醒有人补。
        """
        carrying = [
            platform for platform in ALL_PLATFORMS
            if isinstance(getattr(adapter_class(platform), "nick", None), property)
            and getattr(adapter_class(platform), "nick").fset is not None
        ]
        self.assertEqual(sorted(carrying), sorted(SELF_IDENTITY_PRINCIPAL_PLATFORMS))

    # -- 验收条件 6：文案说后果 ---------------------------------------------
    def test_the_refusal_names_the_written_entry_and_the_current_nick(self):
        message = describe_nick_in_allowlist("irc", "MyBot", "mybot")
        self.assertIn("'mybot'", message, "要说清是哪一条白名单项出的问题")
        self.assertIn("'MyBot'", message, "要说清当前 nick 是什么（大小写也要看得见）")
        self.assertIn("#channel", message, "要给出唯一可行的改法：授权频道名")
        self.assertIn("私聊", message, "要说清为什么私聊会共用同一个 principal")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
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
见 :class:`NickCheckpointTests`。

## 变异实验

``.tmp/nick_trap/mutation_experiment.py``：3 个真变异（只警告 / 只查 ``__init__`` /
去掉大小写折叠）+ 2 个对照（改注释 / 改无关文案）。每处都校验：文件 SHA-256 应用后
变化、还原后逐字节相同、锚点在文件里的命中数**恰好 1**（≠1 即中止报错）。
"""

from __future__ import annotations

import ast
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from opencode_bridge.adapters import build
from opencode_bridge.adapters.base import AdapterError, adapter_class
from opencode_bridge.adapters.irc import IRCAdapter
from opencode_bridge.adapters.twitch import TwitchAdapter
from opencode_bridge.allowlist import (
    NO_SENDER_IDENTITY_REASON,
    describe_nick_in_allowlist,
    nick_in_allowlist,
)
from tests.inbound_log_support import ALL_PLATFORMS, CapturedLogs, RecordingHooks

ADAPTERS_DIR = Path(__file__).resolve().parent.parent / "opencode_bridge" / "adapters"
README_PATH = Path(__file__).resolve().parent.parent / "README.md"

#: 闸门真正用的那个 principal，逐平台。这里写的是**期望值**；
#: :meth:`GatePrincipalAuditTests.test_each_platform_has_exactly_one_gate_principal_symbol`
#: 从源码 AST 里重新扫出来比对 —— 谁改了 principal 的来源，那条断言就红，
#: 于是"是否仍是同型陷阱"会被**重新提问**，而不是靠人记得复查。
GATE_PRINCIPAL_BY_PLATFORM = {
    "a2a": "peer",
    "discord": "channel_id",
    "email": "sender",
    "homeassistant": "entity_id",
    "irc": "target",
    "matrix": "room_id",
    "mattermost": "channel_id",
    "nextcloud": "token",
    "ntfy": "topic",
    "qqbot": "target",
    "slack": "channel",
    "telegram": "chat_id",
    "twitch": "target",
}

#: **只有**这两家：私聊的 principal 与「自己是谁」是同一个字符串。
#:
#: 判据（可从源码复核）：``is_private`` 拿 ``PRIVMSG`` 的 ``params[0]`` 与
#: ``self.nick`` 比，而**同一个** ``params[0]`` 就是喂给 ``self.admits(...)`` 的那个。
SELF_IDENTITY_PRINCIPAL_PLATFORMS = frozenset({"irc", "twitch"})

#: 「自己是谁」这一类符号名。闸门 principal **不允许**是其中任何一个。
SELF_IDENTITY_SYMBOLS = frozenset({
    "nick", "login", "display_name", "bot_token", "bot_user",
    "user_id", "my_user_id", "_my_user_id", "self_id", "bot_name",
})


def _module_source(platform: str) -> str:
    return _read_text(ADAPTERS_DIR / f"{platform}.py")


def _read_text(path: Path) -> str:
    with io.open(path, encoding="utf-8") as handle:
        return handle.read()


def _walk_without_nested_functions(function: ast.FunctionDef):
    """函数体子树，但**不进**嵌套的 ``def`` / ``lambda``（那些归它们自己那一轮）。"""
    stack = list(function.body)
    while stack:
        current = stack.pop()
        yield current
        for child in ast.iter_child_nodes(current):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            stack.append(child)


def _is_self_admits_call(node: ast.AST) -> bool:
    """是不是 ``self.admits(...)`` —— 闸门对外**只有**这一个入口。

    （:meth:`Adapter.answer_pairing_request` 内部也是调它，所以没有第二个入口。）
    """
    if not (isinstance(node, ast.Call) and node.args):
        return False
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "admits"
        and isinstance(func.value, ast.Name)
        and func.value.id == "self"
    )


def _scan_admits_calls(tree: ast.Module) -> list[tuple[ast.FunctionDef, ast.Call]]:
    """``(所在函数, self.admits(<arg>) 调用)`` 的全部配对。"""
    pairs: list[tuple[ast.FunctionDef, ast.Call]] = []
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef):
            continue
        for inner in _walk_without_nested_functions(function):
            if _is_self_admits_call(inner):
                pairs.append((function, inner))  # type: ignore[arg-type]
    return pairs


class GatePrincipalAuditTests(unittest.TestCase):
    """审计**其余 11 个平台**：私聊 principal 是不是同型陷阱。**判据列在这里**。

    ## 判据（不是「已确认」，是这四条可复跑的机械判据）

    构成同型陷阱必须**同时**满足：

    1. 闸门 principal **可能等于「bot 自己的身份」** ⇒ ⛔ principal 不能是
       :data:`SELF_IDENTITY_SYMBOLS` 里的任何一个符号；
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


class NickCheckpointTests(unittest.TestCase):
    """「nick 变成已知值的每一个时刻」怎么被**同一个**检查点覆盖。"""

    def test_the_two_adapters_share_one_kind_of_checkpoint(self):
        """两家的 ``nick`` 都是本类自己定义的 property 且带 setter。"""
        for adapter in (IRCAdapter, TwitchAdapter):
            with self.subTest(adapter=adapter.__name__):
                attribute = adapter.__dict__.get("nick")
                self.assertIsInstance(
                    attribute, property,
                    "nick 必须是本类**自己**定义的 property；继承来的不算"
                    "（否则给基类加检查会牵连 11 个不需要它的平台）",
                )
                self.assertIsNotNone(attribute.fset, "property 必须有 setter")

    def test_every_nick_assignment_site_goes_through_that_one_setter(self):
        """把两个文件里每一处 ``self.nick = ...`` 列出来，并核对它们**绕不开** setter。

        ⛔ 这条**不是**在断言"赋值点只有这几处"（那是数出来的、明天就可能过期）。
        它断言的是**为什么**这些赋值点全都安全：因为 ``nick`` 是 data descriptor，
        ``self.nick = X`` 走 setter，而 setter 是唯一做检查的地方 ⇒ 将来**新增**任何
        一处赋值，它自动被覆盖，不需要在这里补断言。

        但本条仍然有作用：它把"赋值点确实存在、且全都落在 ``self.`` 上"钉住，
        防止有人改成 ``self.__dict__[...] = ...`` 或 ``object.__setattr__`` 绕过
        descriptor —— 那两种写法会**绕开**检查，而它们恰好长得像"普通的性能优化"。
        """
        for platform in sorted(SELF_IDENTITY_PRINCIPAL_PLATFORMS):
            with self.subTest(platform=platform):
                source = _module_source(platform)
                tree = ast.parse(source)
                assignments = [
                    node for node in ast.walk(tree)
                    if isinstance(node, ast.Assign)
                    for target in node.targets
                    if isinstance(target, ast.Attribute) and target.attr == "nick"
                    and isinstance(target.value, ast.Name) and target.value.id == "self"
                ]
                self.assertGreaterEqual(
                    len(assignments), 1,
                    f"{platform} 一个 self.nick = ... 都没有 ⇒ "
                    f"要么 nick 从别处来（本条的前提变了），要么判据坏了",
                )
                bypasses = [
                    ast.unparse(node) for node in ast.walk(tree)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "__setattr__"
                ]
                self.assertEqual(
                    [text for text in bypasses if "nick" in text], [],
                    f"{platform} 用 __setattr__ 直写会绕开 property 的检查",
                )
                self.assertNotIn(
                    'self.__dict__["nick"]', source,
                    f"{platform} 用 __dict__ 直写会绕开 property 的检查",
                )

    def test_the_constructor_assignment_itself_is_the_checked_path(self):
        """构造路径上那一次赋值也走 setter —— 不是 ``__init__`` 之后另做一次检查。"""
        adapter = IRCAdapter(
            {"host": "127.0.0.1", "nick": "CheckedBot", "channels": "#chan"},
            RecordingHooks(),
        )
        self.assertEqual(adapter.nick, "CheckedBot")
        self.assertEqual(adapter._nick, "CheckedBot",
                         "property 的后备字段必须与读到的值一致")


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


class BridgeStartupRefusalTests(unittest.TestCase):
    """验收条件 1 的**进程级**证据：桥**起不来**，而不是打个 warning 继续跑。

    ⚠️ 必须带对照（:meth:`test_the_control_run_without_the_trap_reaches_running_state`）：
    只断言"退出码非 0"是无法区分的 —— 配置写错、连不上 opencode、没配适配器，
    都会给出同一个退出码。对照跑通同一条路径，才能把退出码 1 归因到**陷阱**。
    """

    #: 桥启动成功的标志（``__main__._run_bridge_locked`` 在 ``core.start()`` 之后打它）。
    RUNNING_MARKER = "running against"

    def _run_bridge(self, allowed_chat_ids: list[str], timeout: float = 40.0):
        """在独立目录里真跑一次 ``python -m opencode_bridge``。

        ``opencode_url`` / ``opencode_password`` 显式给全 ⇒
        :func:`~opencode_bridge.opencode_client.discover_endpoint` 直接返回，
        **不发任何网络请求**（本机不该有、也不需要有 opencode 服务）。

        :return: ``(退出码 或 None, 全部输出)``。``None`` = 检查期间进程还活着
            （对照组：桥起来了，所以我们把它杀掉）。
        """
        with tempfile.TemporaryDirectory() as workdir:
            config_path = os.path.join(workdir, "config.json")
            log_path = os.path.join(workdir, "bridge.log")
            io.open(config_path, "w", encoding="utf-8").write(json.dumps({
                "opencode_url": "http://127.0.0.1:65533",
                "opencode_password": "not-a-real-password",
                "state_path": os.path.join(workdir, "state.json"),
                "permissions_mode": "deny",
                "adapters": {"irc": {
                    "host": "127.0.0.1", "port": 6667, "nick": "MyBot",
                    "channels": ["#chan"], "allowed_chat_ids": allowed_chat_ids,
                }},
            }, ensure_ascii=False))
            env = dict(os.environ)
            env["OPENCODE_BRIDGE_CONFIG"] = config_path
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONPATH"] = (
                str(Path(__file__).resolve().parent.parent)
                + os.pathsep + env.get("PYTHONPATH", "")
            )
            with io.open(log_path, "w", encoding="utf-8") as log_file:
                process = subprocess.Popen(
                    [sys.executable, "-m", "opencode_bridge", "--config", config_path],
                    cwd=workdir, env=env, stdout=log_file, stderr=subprocess.STDOUT,
                )
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    output = io.open(log_path, encoding="utf-8", errors="replace").read()
                    if self.RUNNING_MARKER in output or process.poll() is not None:
                        break
                    time.sleep(0.15)
                if process.poll() is None:
                    exit_code: int | None = None
                    process.kill()
                else:
                    exit_code = process.returncode
                process.wait(timeout=15)
            output = io.open(log_path, encoding="utf-8", errors="replace").read()
        return exit_code, output

    def test_the_bridge_refuses_to_start_when_own_nick_is_whitelisted(self):
        exit_code, output = self._run_bridge(["#chan", "mybot"])
        self.assertEqual(exit_code, 1, f"期望因陷阱而拒绝启动，实际:\n{output}")
        self.assertIn("拒绝启动", output)
        self.assertIn("会把所有人的私聊一起放行", output)
        self.assertNotIn(
            self.RUNNING_MARKER, output,
            "桥不该在这条配置下跑起来 —— 跑起来就意味着私聊那条路存在",
        )

    def test_the_control_run_without_the_trap_reaches_running_state(self):
        """**对照**：白名单里只有频道名 ⇒ 桥**照常启动**。

        没有这一条，"退出码 1" 无法归因 —— 它可能只是这个测试环境压根起不来桥。
        """
        exit_code, output = self._run_bridge(["#chan"])
        self.assertIn(
            self.RUNNING_MARKER, output,
            f"对照组也起不来 ⇒ 本测试环境本身有问题，上面那条的退出码 1 "
            f"就不能归因到陷阱：\n{output}",
        )
        self.assertIsNone(exit_code, f"对照组不该自己退出：\n{output}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

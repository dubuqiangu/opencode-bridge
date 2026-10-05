"""13 个平台各跑一次**真实拒绝路径**，断言**落盘**的那一行。

每一类测试都在"闸门翻转后的场景"里站：``config_version >= 2`` + **空**清单 ⇒
谁都不放行 ⇒ 一个陌生人的消息被丢弃 ⇒ 检查那一行日志。

## 为什么必须装上脱敏过滤器才断言

生产里真正落地的那一行是 root handler 上 :class:`RedactingFilter` 洗过之后的
结果。没装过滤器时 ``channel=discord:123456789012345678`` 里那个 id **仍然在**，
所以"行里有前缀"不等于"行里没有明文"。因此这里断言的是**装上过滤器之后**的那
一行 —— 也就是用户 ``grep`` 日志时真正会看到的东西（见
:class:`~tests.inbound_log_support.CollectingHandler`）。

## 为什么"可关联"必须一起断言

C2 对会话 id 用**带进程内随机密钥的 HMAC 摘要**（而不是遮蔽），为的就是
"同一个人横跨多行日志仍然对得上"。所以这里断言 ``<platform>:conv#<摘要>``
**在**行里 —— 把裸 id 换成遮蔽会让这一条红，那才是把可运维性换成了隐私。

⚠️ **诚实边界**：C2 的会话 id 规则**只有一个标签** ``conv#``，所以**作者 /
发件人**这类不是会话的 id 也显示成 ``conv#``；区分靠日志行里的字段名
（``channel=`` vs ``author=``）。这条代价由 discord / mattermost / nextcloud /
qqbot 四条里的 :meth:`PerPlatformRejectionLogTests._assert_fields_survive`
钉住：字段名不许消失。

另两份：:mod:`tests.test_redactable_ids`、
:mod:`tests.test_inbound_log_structure`。共享夹具在
:mod:`tests.inbound_log_support`。
"""

from __future__ import annotations

import json
import unittest

from opencode_bridge import identity
from opencode_bridge.adapters.a2a import A2aAdapter
from opencode_bridge.adapters.discord import DiscordAdapter
from opencode_bridge.adapters.email import EmailAdapter
from opencode_bridge.adapters.homeassistant import HomeAssistantAdapter
from opencode_bridge.adapters.irc import IRCAdapter
from opencode_bridge.adapters.matrix import MatrixAdapter
from opencode_bridge.adapters.mattermost import MattermostAdapter
from opencode_bridge.adapters.nextcloud import NextcloudAdapter
from opencode_bridge.adapters.ntfy import NtfyAdapter
from opencode_bridge.adapters.qqbot import QQBotAdapter
from opencode_bridge.adapters.slack import SlackAdapter
from opencode_bridge.adapters.telegram import TelegramAdapter
from opencode_bridge.adapters.twitch import TwitchAdapter
from opencode_bridge.redaction import Redactor
from tests.inbound_log_support import (
    FLIPPED_GATE_CONFIG,
    FIXED_REDACTION_KEY,
    CapturedLogs,
    RecordingHooks,
    SlackFakeWebSocket,
    make_raw_mail,
)

# ======================================================================
# 3. 逐平台：跑真实拒绝路径，断言落盘的那一行
# ======================================================================
class PerPlatformRejectionLogTests(unittest.TestCase):
    """13 个平台各一条：**空清单 + ``config_version>=2``** 下丢弃一条陌生人的消息。

    每条都断言三件事：

    1. 那一行**不含**那个裸 id（装上了 :class:`RedactingFilter` 之后 —— 即
       用户 ``grep`` 到的样子）；
    2. 那一行**含**那个 ``conversation_id`` 的摘要，即**跨行仍可关联**；
    3. 消息**确实**被丢了（``hooks.inbounds`` 为空）—— 顺带证明"只改记什么、
       不改丢不丢"这条硬约束没被破坏。
    """

    maxDiff = None

    # -- 共享断言 ----------------------------------------------------
    def _assert_drop_line(
        self,
        platform: str,
        raw_local_id: str,
        lines: list[str],
        expected_conversation_id: str | None = None,
        fingerprinted_local: str | None = None,
    ) -> None:
        """断言"落盘的那一行"：无裸 id、有可关联摘要。

        期望值**从 :class:`Redactor` 直接算**，不调用被测的
        :func:`redactable_id` —— 否则"实现算出来的"与"断言算出来的"是同一个
        函数，实现错了断言也会跟着错。

        :param expected_conversation_id: 额外断言"落盘那一行里的摘要**就是**这个
            会话标识的脱敏形态"。给了它，就顺带钉住"日志说的会话 == 真会进
            ``Inbound.conversation_id`` 的那个会话"。
        :param fingerprinted_local: **被摘要的那个值**。默认就是
            ``raw_local_id``；只有 qqbot 的会话字段需要改 —— 它的 conversation
            段进来时**已经**是 ``qqbot:<scope>:<target>``，所以被摘要的是
            ``group:<openid>`` 而**不是**裸 openid。这两者的区别正是"裸 id 明明
            还在、却已经不再是日志里那个值"的典型形态，所以要显式分开写。
        """
        redactor = Redactor(key=FIXED_REDACTION_KEY)
        # ``fingerprint`` 自带 ``conv#`` 标签，所以平台段拼在**外面**。
        expected_fingerprint = "%s:%s" % (
            platform,
            redactor.fingerprint("conv",
                                 raw_local_id if fingerprinted_local is None
                                 else fingerprinted_local),
        )
        self.assertTrue(lines, "拒绝路径上一行日志都没记 —— 这条测试量的是空集")
        for line in lines:
            self.assertNotIn(
                raw_local_id, line,
                "落盘的日志行里仍有**裸 id**（平台=%s）：%s" % (platform, line),
            )
        joined = "\n".join(lines)
        self.assertIn(
            expected_fingerprint, joined,
            "落盘的日志行里没有可关联的 conversation_id 摘要（平台=%s，"
            "期望 %s）：\n%s" % (platform, expected_fingerprint, joined),
        )
        # 实参必须**经过** redactable_id：``=<local id>`` 这个形状一旦出现，
        # 说明有人把裸值直接拼进去了（哪怕它恰好也是别的字段的一部分）。
        self.assertNotIn("=%s" % raw_local_id, joined,
                         "实参没有经过 redactable_id（平台=%s）" % platform)
        if expected_conversation_id is not None:
            self.assertIn(
                redactor.scrub(expected_conversation_id), joined,
                "落盘那一行里的摘要不是本适配器真会用的 conversation_id"
                "（平台=%s，期望 %s）：\n%s"
                % (platform, expected_conversation_id, joined),
            )

    def _rejected(self, platform: str) -> None:
        """每条逐平台测试收尾都用它：消息一条都不许被放进来。"""
        self.assertEqual(
            getattr(self, "hooks").inbounds, [],
            "这条消息本该被丢弃 —— 若它进来了，说明「丢不丢」的逻辑变了"
            "（平台=%s）" % platform,
        )

    # -- telegram ---------------------------------------------------
    def test_telegram(self) -> None:
        chat_id = -1001234567890
        self.hooks = hooks = RecordingHooks()
        adapter = TelegramAdapter({"bot_token": "123:FAKE", **FLIPPED_GATE_CONFIG}, hooks)
        update = {"update_id": 5, "message": {
            "message_id": 9, "chat": {"id": chat_id, "type": "supergroup"},
            "from": {"id": 7, "is_bot": False, "first_name": "stranger"},
            "text": "hello there",
        }}
        with CapturedLogs("telegram") as handler:
            adapter._dispatch_update(update)
        self._assert_drop_line("telegram", str(chat_id), handler.lines,
                               adapter._conversation_id(chat_id))
        self._rejected("telegram")

    def test_telegram_callback_path_also_goes_through_the_prefixed_form(self) -> None:
        """按钮点击是**另一条**闸门分支（``_handle_callback``），同样打裸 chat_id。"""
        chat_id = -1009876543210
        self.hooks = hooks = RecordingHooks()
        adapter = TelegramAdapter({"bot_token": "123:FAKE", **FLIPPED_GATE_CONFIG}, hooks)
        callback = {"id": "cb-1", "data": "approve:x", "from": {"id": 7},
                    "message": {"message_id": 9, "chat": {"id": chat_id, "type": "group"}}}
        with CapturedLogs("telegram") as handler:
            adapter._handle_callback(callback)
        self._assert_drop_line("telegram", str(chat_id), handler.lines,
                               adapter._conversation_id(chat_id))
        self._rejected("telegram")

    # -- slack ------------------------------------------------------
    def test_slack(self) -> None:
        channel = "C0123ABCDEFG"
        self.hooks = hooks = RecordingHooks()
        adapter = SlackAdapter({"bot_token": "xoxb-t", "app_token": "xapp-t",
                                **FLIPPED_GATE_CONFIG}, hooks)
        envelope = json.dumps({
            "envelope_id": "e1", "type": "events_api",
            "payload": {"event": {"type": "message", "text": "hello there",
                                  "channel": channel, "user": "U0STRANGER",
                                  "ts": "111.222"}},
        })
        with CapturedLogs("slack") as handler:
            self.assertTrue(adapter._handle_envelope(SlackFakeWebSocket(), envelope))
        self._assert_drop_line("slack", channel, handler.lines,
                               adapter._conversation_id(channel))
        self._rejected("slack")

    # -- discord ----------------------------------------------------
    def test_discord(self) -> None:
        channel_id = "123456789012345678"
        author_id = "987654321098765432"
        self.hooks = hooks = RecordingHooks()
        adapter = DiscordAdapter({"bot_token": "d-token", **FLIPPED_GATE_CONFIG}, hooks)
        payload = {"id": "m1", "type": 0, "channel_id": channel_id, "content": "hello there",
                   "author": {"id": author_id, "bot": False}}
        with CapturedLogs("discord") as handler:
            self.assertFalse(adapter._handle_message_create(payload))
        self._assert_drop_line("discord", channel_id, handler.lines,
                               adapter._conversation_id(channel_id))
        self._assert_drop_line("discord", author_id, handler.lines,
                               adapter._conversation_id(author_id))
        self._assert_fields_survive(handler.lines, ("channel=", "author="))
        self._rejected("discord")

    # -- matrix -----------------------------------------------------
    def test_matrix(self) -> None:
        room_id = "!stranger:example.org"
        self.hooks = hooks = RecordingHooks()
        adapter = MatrixAdapter({"homeserver": "https://matrix.example.org",
                                 "access_token": "syt_fake_token",
                                 "user_id": "@bridgebot:example.org",
                                 **FLIPPED_GATE_CONFIG}, hooks)
        event = {"type": "m.room.message", "sender": "@stranger:example.org",
                 "event_id": "$evt-1",
                 "content": {"msgtype": "m.text", "body": "hello there"}}
        with CapturedLogs("matrix") as handler:
            adapter._handle_event(room_id, event)
        self._assert_drop_line("matrix", room_id, handler.lines,
                               adapter._conversation_id(room_id))
        self._rejected("matrix")

    # -- mattermost -------------------------------------------------
    def test_mattermost(self) -> None:
        channel_id = "chan0000000000000000000001a"
        author_id = "user0000000000000000000000b"
        self.hooks = hooks = RecordingHooks()
        adapter = MattermostAdapter({"site_url": "https://mm.example.com", "token": "mm-t",
                                     "user_id": "user000000000000000000009z9",
                                     **FLIPPED_GATE_CONFIG}, hooks)
        post = {"id": "post-1", "user_id": author_id, "channel_id": channel_id,
                "message": "hello there", "type": "", "delete_at": 0, "file_ids": []}
        with CapturedLogs("mattermost") as handler:
            self.assertFalse(adapter._handle_posted(post))
        self._assert_drop_line("mattermost", channel_id, handler.lines,
                               adapter._conversation_id(channel_id))
        self._assert_drop_line("mattermost", author_id, handler.lines,
                               adapter._conversation_id(author_id))
        self._assert_fields_survive(handler.lines, ("channel=", "user="))
        self._rejected("mattermost")

    # -- irc --------------------------------------------------------
    def test_irc(self) -> None:
        target = "#stranger-room"
        self.hooks = hooks = RecordingHooks()
        adapter = IRCAdapter({"host": "127.0.0.1", "nick": "opencodebot",
                              "channels": ["#stranger-room"],
                              **FLIPPED_GATE_CONFIG}, hooks)
        with CapturedLogs("irc") as handler:
            adapter._handle_privmsg(
                ":stranger!user@example.org PRIVMSG",
                [target, "opencodebot: hello there"],
            )
        self._assert_drop_line("irc", target, handler.lines,
                               adapter._conversation_id(target))
        self._rejected("irc")

    # -- twitch -----------------------------------------------------
    def test_twitch(self) -> None:
        target = "#stranger_channel"
        self.hooks = hooks = RecordingHooks()
        adapter = TwitchAdapter({"token": "t-token", "channel": "stranger_channel",
                                 "nick": "opencodebot", **FLIPPED_GATE_CONFIG}, hooks)
        prefix = ":stranger!stranger@stranger.tmi.twitch.tv"
        with CapturedLogs("twitch") as handler:
            adapter._handle_privmsg(
                "%s PRIVMSG %s :opencodebot: hello there" % (prefix, target),
                {"id": "twitch-msg-1"}, prefix, [target, "opencodebot: hello there"],
            )
        self._assert_drop_line("twitch", target, handler.lines,
                               adapter._conversation_id(target))
        self._rejected("twitch")

    # -- ntfy -------------------------------------------------------
    def test_ntfy(self) -> None:
        topic = "stranger-topic"
        self.hooks = hooks = RecordingHooks()
        adapter = NtfyAdapter({"topic": "my-topic", "server": "https://ntfy.example.com",
                               **FLIPPED_GATE_CONFIG}, hooks)
        item = {"id": "n1", "time": 1700000000, "event": "message",
                "topic": topic, "message": "hello there"}
        with CapturedLogs("ntfy") as handler:
            adapter._on_raw(item)
        self._assert_drop_line("ntfy", topic, handler.lines,
                               identity.format_id("ntfy", topic))
        self._rejected("ntfy")

    # -- email ------------------------------------------------------
    def test_email(self) -> None:
        sender = "stranger@example.net"
        self.hooks = hooks = RecordingHooks()
        adapter = EmailAdapter({"address": "bot@example.com", "password": "app-password",
                                "imap_host": "imap.example.com",
                                "smtp_host": "smtp.example.com",
                                **FLIPPED_GATE_CONFIG}, hooks)
        raw = make_raw_mail(sender=sender)
        with CapturedLogs("email") as handler:
            adapter._on_raw(raw)
        self._assert_drop_line("email", sender, handler.lines,
                               identity.format_id("email", sender))
        self._rejected("email")

    # -- nextcloud --------------------------------------------------
    def test_nextcloud(self) -> None:
        """**最高危的一处**：room token 是 OCS 的**会话 token**，
        进 URL（``/call/<token>``），而本平台 :attr:`pairing_supported` = False
        的理由之一就是"用户不该知道它"。
        """
        room_token = "r4om3t0k3n"
        actor_id = "stranger-actor"
        self.hooks = hooks = RecordingHooks()
        adapter = NextcloudAdapter({"base_url": "https://cloud.example.com",
                                    "username": "bridge", "password": "app-password",
                                    "user_id": "BridgeBot", **FLIPPED_GATE_CONFIG}, hooks)
        message = {"id": "1", "token": room_token, "actorId": actor_id,
                   "actorType": "users", "actorDisplayName": "Stranger",
                   "timestamp": 1700000000, "message": "hello there",
                   "messageParameters": {}, "messageType": "comment", "systemMessage": ""}
        with CapturedLogs("nextcloud") as handler:
            self.assertFalse(adapter._handle_message(room_token, message))
        self._assert_drop_line("nextcloud", room_token, handler.lines,
                               adapter._conversation_id(room_token))
        self._assert_drop_line("nextcloud", actor_id, handler.lines,
                               adapter._conversation_id(actor_id))
        self._assert_fields_survive(handler.lines, ("room=", "actor="))
        self._rejected("nextcloud")

    # -- qqbot ------------------------------------------------------
    def test_qqbot(self) -> None:
        """``cid`` 进来时**已经**是 ``qqbot:<scope>:<target>``；作者是裸 openid。"""
        group_openid = "B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4E5"
        author_openid = "A1B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4"
        self.hooks = hooks = RecordingHooks()
        adapter = QQBotAdapter({"app_id": "102000000", "app_secret": "app-secret",
                                **FLIPPED_GATE_CONFIG}, hooks)
        event = "GROUP_AT_MESSAGE_CREATE"
        data = {"id": "ROBOT1.0_msg-1",
                "author": {"id": author_openid, "member_openid": author_openid,
                           "member_role": "member", "username": "stranger", "bot": False},
                "content": "hello there", "message_type": 0, "group_openid": group_openid}
        with CapturedLogs("qqbot") as handler:
            self.assertFalse(adapter._handle_message(event, data))
        conversation_id = QQBotAdapter.conversation_id_for("group", group_openid)
        self._assert_drop_line("qqbot", group_openid, handler.lines, conversation_id,
                               "group:" + group_openid)
        self._assert_drop_line("qqbot", author_openid, handler.lines)
        self._assert_fields_survive(handler.lines, ("conversation=", "author="))
        self._rejected("qqbot")

    # -- a2a --------------------------------------------------------
    def test_a2a(self) -> None:
        peer = "stranger-agent"
        self.hooks = hooks = RecordingHooks()
        adapter = A2aAdapter(dict(FLIPPED_GATE_CONFIG), hooks)
        params = {"message": {"role": "user",
                              "parts": [{"kind": "text", "text": "hello there"}]}}
        with CapturedLogs("a2a") as handler:
            response = adapter._rpc_send_message("req-1", params, peer)
        self.assertTrue(response, "a2a 应当回一条 JSON-RPC 错误而不是静默")
        self._assert_drop_line("a2a", peer, handler.lines,
                               adapter._conversation_id(peer))
        self._rejected("a2a")

    # -- homeassistant ----------------------------------------------
    def test_homeassistant(self) -> None:
        entity_id = "light.kitchen"
        self.hooks = hooks = RecordingHooks()
        adapter = HomeAssistantAdapter({"url": "ws://ha.example.com", "token": "ha-token",
                                        "entities": [entity_id],
                                        **FLIPPED_GATE_CONFIG}, hooks)
        packet = {"id": 1, "type": "event", "event": {
            "event_type": "state_changed",
            "data": {"entity_id": entity_id,
                     "new_state": {"state": "on",
                                   "attributes": {"friendly_name": "厨房灯"}}},
            "context": {"id": "ctx-1", "parent_id": None, "user_id": "u-stranger"},
        }}
        with CapturedLogs("homeassistant") as handler:
            self.assertFalse(adapter._handle_event(packet))
        self._assert_drop_line("homeassistant", entity_id, handler.lines,
                               HomeAssistantAdapter.conversation_id_for(entity_id))
        self._rejected("homeassistant")

    # -- 共享辅助 ---------------------------------------------------
    def _assert_fields_survive(self, lines: list[str], fields: tuple[str, ...]) -> None:
        """字段名必须还在 —— C2 只洗**值**，洗不掉 ``channel=`` / ``author=``。

        这一条是那笔"代价"的守门：作者 / 发件人这类**不是会话**的 id 在日志里
        也显示成 ``conv#``（C2 只有一个标签），所以区分"哪个字段是会话、哪个
        字段是人"**只**剩字段名。字段名一丢，"谁在刷屏"就再也答不上了。
        """
        joined = "\n".join(lines)
        for field in fields:
            with self.subTest(field=field):
                self.assertIn(field, joined,
                              "字段名 %r 消失了 —— 脱敏后值一律显示 conv#，"
                              "区分会话 / 人的最后线索就没了：\n%s" % (field, joined))

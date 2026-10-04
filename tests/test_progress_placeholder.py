"""占位消息的**存活性**：发出去的 ``⏳ 处理中…`` 必须被顶掉，或者根本不该发。

## 这个 bug 长什么样

桥在模型开始产出时发一条 ``⏳ 处理中…``，收尾时试图把它**原地改写**成最终答复。
七个平台的 :meth:`~opencode_bridge.adapters.base.Adapter.edit` **恒返回 ``False``**
（email / ntfy / a2a / homeassistant / irc / twitch / qqbot —— 各自的 docstring
都写明了为什么）。于是 ``finalize`` 退化成"再发一条"，而那条气泡**永远**清不掉：
**每一轮**都留下一个僵尸"处理中" + 一条真正的答复。

## 这里的判据

不测"有没有调用过 send"，只测**最终状态**：

* **没有僵尸** —— 每一条被发出去的消息，它最后承载的文本都**不是**占位文案；
* **答复恰好一次、逐字节相同** —— 承载答复的**消息身份**恰好一个，那条消息的
  末尾文本与答复**逐字节相等**（不丢、不截断、不重复）。

## 为什么两个身份而不是"文本出现几次"

能改写的平台上，同一条消息会被改写很多次（流式 delta + 收尾），
所以"答复文本出现了几次"根本不是用户看到的东西 —— 用户看到的是**一条消息**。
于是判据落在**消息身份**（:class:`~opencode_bridge.hooks.MsgHandle` 的 ``message_id``）上。

## 夹具怎么搭

每个平台都跑**真的**适配器实例、真的传输层，只把最底下那一层 HTTP / WebSocket /
SMTP 换成一个记录器（见 :func:`record_wire`）。桥那一侧
（``BridgeCore`` → ``EventStream`` → ``InboundGateway`` → ``OutboundSender``）
**一行都没换**，所以走的就是生产路径本身。
"""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from collections.abc import Callable
from dataclasses import dataclass, field

from opencode_bridge.adapters.base import build, registered_names
from opencode_bridge.config import Config
from opencode_bridge.core import BridgeCore
from opencode_bridge.hooks import Inbound, MsgHandle, Outbound
from opencode_bridge.inbound_gateway import PROGRESS_TEXT
from opencode_bridge.state import StateStore

#: 短答复：远低于**每一个**平台的分片阈值，所以"线上文本"就是答复本身，
#: 逐字节断言成立（长答复另有一例，见 :class:`LongAnswerIsNotTruncatedTests`）。
ANSWER = "构建失败是因为 config.py 里少了 default 分支。"

#: 长答复：5000 个 ASCII 字符 —— 超过 IRC / Twitch 的 400，超过 Discord / QQ 的 2000，
#: 超过 email 的 998 行长预算，但远小于 Nextcloud / Slack。
LONG_ANSWER = "0123456789" * 500

#: 仓库内的临时目录：即便 TEMP/TMP 指向机器别处，测试也不可能写到仓库之外。
_REPOSITORY_TEMP = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".tmp"
)

#: 恒返回 ``False`` 的 :meth:`edit` —— 也就是**不发**占位消息的那七个。
PLATFORMS_WITHOUT_MESSAGE_EDIT = (
    "a2a", "email", "homeassistant", "irc", "ntfy", "qqbot", "twitch",
)

#: 真能原地改写的六个 —— 占位消息照发，而且会被顶掉。
PLATFORMS_WITH_MESSAGE_EDIT = (
    "discord", "matrix", "mattermost", "nextcloud", "slack", "telegram",
)


@dataclass
class Delivery:
    """一次投递：写到了**哪条消息**上、写了什么、走的是哪条路。"""

    identity: str
    text: str
    via: str  # "send" | "edit"
    kind: str
    edited: bool = True


@dataclass
class PlatformUnderTest:
    """一个平台 + 它这一轮的全部投递记录。"""

    name: str
    adapter: object
    conversation_id: str
    deliveries: list[Delivery] = field(default_factory=list)
    #: 真正发到线路上的文本（分片后的每一段，按顺序）。
    wire: list[str] = field(default_factory=list)
    #: 线路文本在比对前要做的归一化。默认原样；只有邮件需要（见 :func:`_email`）——
    #: MIME 的 quoted-printable 会按 76 字符硬折行，那是传输层的事，不是内容变了。
    normalise_wire: Callable[[str], str] = lambda text: text

    # -- 派生视图 ------------------------------------------------------
    def identities(self) -> dict[str, list[str]]:
        """``消息身份 -> 依次写上去的文本``（按发生顺序）。"""
        written: dict[str, list[str]] = {}
        for delivery in self.deliveries:
            written.setdefault(delivery.identity, []).append(delivery.text)
        return written

    def left_showing_the_placeholder(self) -> list[str]:
        """**最后**一条文本仍是占位文案的那些消息身份。"""
        return [
            identity
            for identity, texts in self.identities().items()
            if texts[-1] == PROGRESS_TEXT
        ]

    def identities_whose_last_text_is(self, text: str) -> list[str]:
        return [
            identity
            for identity, texts in self.identities().items()
            if texts[-1] == text
        ]

    def progress_sends(self) -> list[Delivery]:
        return [
            delivery
            for delivery in self.deliveries
            if delivery.via == "send" and delivery.kind == "progress"
        ]

    def wire_text(self) -> list[str]:
        """线路上的每一段，已按该平台的传输层规则归一化。"""
        return [self.normalise_wire(text) for text in self.wire if text]


# ----------------------------------------------------------------------
# 最底下那一层：线路记录器（每个平台只换这一处）
# ----------------------------------------------------------------------
def _privmsg_body(line: str) -> str:
    """``PRIVMSG <target> :<body>`` → ``<body>``。"""
    _prefix, _sep, body = line.partition(" :")
    return body


def _build_a2a_task(adapter, peer: str):
    """给 a2a 造一个"正在等回复"的 task —— 那才是它出站的合法前提。"""
    from opencode_bridge.adapters import a2a as a2a_module

    task = a2a_module._Task(
        task_id="task-1", context_id="ctx-1", peer=peer,
    )
    with adapter._lock:
        adapter._tasks[task.task_id] = task
        adapter._order.setdefault(peer, []).append(task.task_id)
    return task


def build_platform(name: str) -> PlatformUnderTest:
    """造一个**真适配器**，只把传输层换成记录器。

    每家的 ``conversation_id`` 都用该平台自己的文法（``format_id`` 的现行格式），
    所以走的确实是它自己的解析 / 分片 / 发信代码。
    """
    under_test = _PLATFORM_SETUP[name]()
    _instrument_adapter_boundary(under_test)
    return under_test


def _instrument_adapter_boundary(under_test: PlatformUnderTest) -> None:
    """在 ``adapter.send`` / ``adapter.edit`` 上包一层记录，然后**照常调用真实现**。"""
    adapter = under_test.adapter
    real_send, real_edit = adapter.send, adapter.edit

    def recording_send(out: Outbound) -> MsgHandle | None:
        handle = real_send(out)
        under_test.deliveries.append(Delivery(
            identity=handle.message_id if handle is not None else "no-handle",
            text=out.text, via="send", kind=out.kind,
        ))
        return handle

    def recording_edit(handle: MsgHandle, out: Outbound) -> bool:
        edited = real_edit(handle, out)
        under_test.deliveries.append(Delivery(
            identity=handle.message_id, text=out.text, via="edit",
            kind=out.kind, edited=edited,
        ))
        return edited

    adapter.send = recording_send
    adapter.edit = recording_edit


def _telegram() -> PlatformUnderTest:
    from opencode_bridge.adapters.telegram import TelegramAdapter

    under_test = PlatformUnderTest(
        "telegram", TelegramAdapter({"bot_token": "1:t"}, _InertHooks()),
        "telegram:55",
    )
    counter = {"n": 100}

    def fake_api(conversation_id, method, payload, *, timeout=None):
        counter["n"] += 1
        under_test.wire.append(payload["text"])
        return {"ok": True, "result": {"message_id": counter["n"]}}

    under_test.adapter._api = fake_api
    return under_test


def _slack() -> PlatformUnderTest:
    from opencode_bridge.adapters.slack import SlackAdapter

    under_test = PlatformUnderTest(
        "slack",
        SlackAdapter({"bot_token": "xoxb-t", "app_token": "xapp-a"}, _InertHooks()),
        "slack:C1",
    )
    counter = {"n": 0}

    def fake_request(method, path, payload, *, timeout=None):
        if path == "chat.postMessage":
            counter["n"] += 1
            under_test.wire.append(payload["text"])
            return 200, {"ok": True, "ts": "1700000000.000%d" % counter["n"]}
        under_test.wire.append(payload.get("text", ""))
        return 200, {"ok": True, "ts": "1700000000.000%d" % counter["n"]}

    under_test.adapter._request = fake_request
    return under_test


def _discord() -> PlatformUnderTest:
    from opencode_bridge.adapters.discord import DiscordAdapter

    under_test = PlatformUnderTest(
        "discord", DiscordAdapter({"bot_token": "d"}, _InertHooks()), "discord:123",
    )
    counter = {"n": 0}

    def fake_request(method, path, payload, *, timeout=None):
        counter["n"] += 1
        under_test.wire.append(payload["content"])
        return 200, {"id": "100%d" % counter["n"], "channel_id": "123"}

    under_test.adapter._request = fake_request
    return under_test


def _matrix() -> PlatformUnderTest:
    from opencode_bridge.adapters.matrix import MatrixAdapter

    under_test = PlatformUnderTest(
        "matrix",
        MatrixAdapter({
            "homeserver": "https://matrix.example.com",
            "access_token": "m", "user_id": "@bot:example.com",
        }, _InertHooks()),
        "matrix:!room:example.com",
    )
    counter = {"n": 0}

    def fake_put(room_id, content, conversation_id):
        counter["n"] += 1
        # 编辑走 ``m.replace``：用户看到的是 ``m.new_content``，回退路径才用 ``body``
        replacement = content.get("m.new_content") or {}
        under_test.wire.append(str(replacement.get("body", content.get("body", ""))))
        return 200, {"event_id": "$evt%d" % counter["n"]}

    under_test.adapter._put_message = fake_put
    return under_test


def _mattermost() -> PlatformUnderTest:
    from opencode_bridge.adapters.mattermost import MattermostAdapter

    under_test = PlatformUnderTest(
        "mattermost",
        MattermostAdapter({"site_url": "https://mm.example.com", "token": "t"},
                          _InertHooks()),
        "mattermost:team1",
    )
    counter = {"n": 0}

    def fake_request(method, path, payload=None, *, timeout=None):
        counter["n"] += 1
        under_test.wire.append(str((payload or {}).get("message", "")))
        return 200, {"id": "post%d" % counter["n"]}

    under_test.adapter._request = fake_request
    return under_test


def _nextcloud() -> PlatformUnderTest:
    from opencode_bridge.adapters import nextcloud as nextcloud_module
    from opencode_bridge.adapters.nextcloud import NextcloudAdapter

    under_test = PlatformUnderTest(
        "nextcloud",
        NextcloudAdapter({
            "base_url": "https://nc.example.com",
            "username": "u", "password": "p",
        }, _InertHooks()),
        "nextcloud:tok3n",
    )
    counter = {"n": 0}

    def fake_request(method, path, *, params=None, form=None, timeout=None):
        counter["n"] += 1
        if form:
            under_test.wire.append(str(form.get("message", "")))
            return nextcloud_module._Resp(
                status=201,
                data={"ocs": {"meta": {"statuscode": 201},
                              "data": {"id": counter["n"]}}},
            )
        under_test.wire.append("")
        return nextcloud_module._Resp(
            status=200, data={"ocs": {"meta": {"statuscode": 200}, "data": {}}},
        )

    under_test.adapter._request = fake_request
    return under_test


def _email() -> PlatformUnderTest:
    import email as email_module

    from opencode_bridge.adapters.email import EmailAdapter

    under_test = PlatformUnderTest(
        "email",
        EmailAdapter({
            "address": "bot@example.com", "password": "app-pw",
            "imap_host": "imap.example.com", "smtp_host": "smtp.example.com",
        }, _InertHooks()),
        "email:ops@example.com",
    )

    def fake_smtp_send(recipient: str, raw: bytes) -> None:
        # 走真正的 MIME 解析：邮件头每次都不一样（Date / Message-ID），比正文没意义；
        # 正文是 quoted-printable，必须 ``decode=True`` 解码，不能直接读字符串。
        parsed = email_module.message_from_bytes(raw)
        payload = parsed.get_payload(decode=True) or b""
        under_test.wire.append(payload.decode("utf-8", "replace"))

    under_test.adapter._smtp_send = fake_smtp_send
    # quoted-printable 会按 76 字符硬折行，那是 RFC 5322 的**传输**要求（也正是邮件
    # 适配器"硬折行而不拆信"的那件事），不是内容变了。逐字节比对前只去掉**换行**
    # —— 空格是正文的一部分，不能一起吃掉。
    under_test.normalise_wire = lambda text: "".join(text.splitlines())
    return under_test


def _ntfy() -> PlatformUnderTest:
    from opencode_bridge.adapters.ntfy import NtfyAdapter

    under_test = PlatformUnderTest(
        "ntfy", NtfyAdapter({"topic": "bridge"}, _InertHooks()), "ntfy:bridge",
    )
    counter = {"n": 0}

    def fake_http(method, url, data=None, headers=None, timeout=None):
        counter["n"] += 1
        under_test.wire.append(
            data.decode("utf-8", "replace") if isinstance(data, bytes) else str(data)
        )
        return 200, {"id": "n%d" % counter["n"]}, None

    under_test.adapter._http = fake_http
    return under_test


def _irc() -> PlatformUnderTest:
    from opencode_bridge.adapters.irc import IRCAdapter

    under_test = PlatformUnderTest(
        "irc", IRCAdapter({"host": "irc.example.com", "nick": "bridge",
                           "channels": ["#ops"]}, _InertHooks()),
        "irc:#ops",
    )

    # ``send`` 先要一个"当前连接"，没有就当传输失败拒发 —— 这里给一个占位对象。
    under_test.adapter._connection = lambda: object()

    def fake_write_line(line: str) -> bool:
        under_test.wire.append(_privmsg_body(line))
        return True

    under_test.adapter._write_line = fake_write_line
    return under_test


def _twitch() -> PlatformUnderTest:
    from opencode_bridge.adapters.twitch import TwitchAdapter

    under_test = PlatformUnderTest(
        "twitch", TwitchAdapter({"token": "oauth:t", "channel": "ops"},
                                _InertHooks()),
        "twitch:#ops",
    )
    # ``send`` 先要一个"当前连接"，没有就当传输失败拒发 —— 这里给一个占位对象。
    under_test.adapter._current_ws = lambda: object()
    under_test.adapter._send_line = lambda ws, line: (
        under_test.wire.append(_privmsg_body(line)) or True
    )
    return under_test


def _qqbot() -> PlatformUnderTest:
    from opencode_bridge.adapters.qqbot import QQBotAdapter

    under_test = PlatformUnderTest(
        "qqbot", QQBotAdapter({"app_id": "1", "app_secret": "s"}, _InertHooks()),
        "qqbot:group:openid-1",
    )
    under_test.adapter._ensure_access_token = lambda: "token-1"
    counter = {"n": 0}

    def fake_request(method, path, payload=None, *, token="", timeout=None):
        counter["n"] += 1
        under_test.wire.append(str((payload or {}).get("content", "")))
        return 200, {"id": "ROBOT1.0_msg%d" % counter["n"], "timestamp": counter["n"]}

    under_test.adapter._request = fake_request
    return under_test


def _a2a() -> PlatformUnderTest:
    from opencode_bridge.adapters.a2a import A2aAdapter

    under_test = PlatformUnderTest("a2a", A2aAdapter({}, _InertHooks()), "a2a:peer-1")
    _build_a2a_task(under_test.adapter, "peer-1")

    real_finalize = under_test.adapter._finalize

    def recording_finalize(target, state, reply=""):
        result = real_finalize(target, state, reply)
        # a2a 的"线路"不是一串消息，而是**那个 task 的最终 reply**。
        under_test.wire.append(target.reply)
        return result

    under_test.adapter._finalize = recording_finalize
    return under_test


def _homeassistant() -> PlatformUnderTest:
    from opencode_bridge.adapters.homeassistant import HomeAssistantAdapter

    under_test = PlatformUnderTest(
        "homeassistant",
        HomeAssistantAdapter({"url": "ws://ha.example.com", "token": "t"},
                             _InertHooks()),
        "homeassistant:light.kitchen",
    )
    counter = {"n": 0}

    def fake_command(payload_factory, *, timeout, what):
        counter["n"] += 1
        payload = payload_factory(counter["n"])
        under_test.wire.append(
            str((payload.get("service_data") or {}).get("message", ""))
        )
        return True, {"context": {"id": "ctx%d" % counter["n"]}}, {}

    under_test.adapter._command = fake_command
    return under_test


class _InertHooks:
    """``Adapter.__init__`` 只要一个 hooks 对象；这里什么都不实现。"""

    def on_inbound(self, inbound: Inbound) -> None:
        return None

    def on_callback(self, conversation_id: str, data: str, query_id: str) -> None:
        return None

    def load_stream_cursor(self, stream_scope: str):
        return None

    def save_stream_cursor(self, stream_scope: str, position: int) -> None:
        return None


class _RecordingOpenCodeClient:
    """``BridgeCore`` + ``SessionRegistry`` + ``InboundGateway`` 用到的四个方法。"""

    def __init__(self) -> None:
        self.created_ids: list[str] = []
        self.prompts: list[tuple[str, str]] = []

    def create_session(self, *, directory, title=None, agent=None, permissions=None):
        self.created_ids.append("ses_test%04d" % (len(self.created_ids) + 1))
        return self.created_ids[-1]

    def get_session(self, session_id: str) -> dict:
        return {"id": session_id}

    def delete_session(self, session_id: str) -> None:
        return None

    def prompt(self, session_id: str, text: str, *, resume: bool = True) -> str:
        self.prompts.append((session_id, text))
        return "msg_test"

    def reply_permission(self, session_id, request_id, decision, *, message=None):
        return None

    def close(self) -> None:
        return None


_PLATFORM_SETUP = {
    "a2a": _a2a,
    "discord": _discord,
    "email": _email,
    "homeassistant": _homeassistant,
    "irc": _irc,
    "matrix": _matrix,
    "mattermost": _mattermost,
    "nextcloud": _nextcloud,
    "ntfy": _ntfy,
    "qqbot": _qqbot,
    "slack": _slack,
    "telegram": _telegram,
    "twitch": _twitch,
}


def event(name: str, **data) -> dict:
    return {"type": name, "data": data}


def run_one_turn(under_test: PlatformUnderTest, answer: str) -> None:
    """在**真桥**上跑完整的一轮：入站 → prompt → 流式 delta → 收尾。

    桥那一侧全部是真的（``BridgeCore`` / ``EventStream`` / ``InboundGateway`` /
    ``OutboundSender``），只有最底下的线路是记录器 —— 所以走的就是生产路径。
    ``edit_interval_seconds=0`` 是为了让流式改写不被节流挡掉，那与本用例无关。
    """
    os.makedirs(_REPOSITORY_TEMP, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=_REPOSITORY_TEMP) as tempdir:
        config = Config()
        config.bridge = {"edit_interval_seconds": 0, "max_message_chars": 4000}
        core = BridgeCore(
            config, _RecordingOpenCodeClient(), StateStore(
                os.path.join(tempdir, "state.json")
            ),
        )
        core.attach(under_test.adapter)
        try:
            core.on_inbound(Inbound(
                conversation_id=under_test.conversation_id,
                text="跑一下",
                platform=under_test.name,
            ))
            session_id = core.client.created_ids[0]
            stream = core.event_stream
            stream.dispatch(event("session.execution.started", sessionID=session_id))
            # 故意**乱序**投递两片：顺便证明收尾时拼回的是原文，不是到达顺序。
            stream.dispatch(event(
                "session.text.delta", sessionID=session_id,
                assistantMessageID="msg_a", ordinal=1, delta=answer[len(answer) // 2:],
            ))
            stream.dispatch(event(
                "session.text.delta", sessionID=session_id,
                assistantMessageID="msg_a", ordinal=0, delta=answer[:len(answer) // 2],
            ))
            stream.dispatch(event(
                "session.execution.succeeded", sessionID=session_id,
            ))
        finally:
            for adapter in core.adapters:
                adapter.stop()
            core.client.close()


class TurnTestCase(unittest.TestCase):
    def setUp(self) -> None:
        os.makedirs(_REPOSITORY_TEMP, exist_ok=True)
        self.tempdir = tempfile.TemporaryDirectory(dir=_REPOSITORY_TEMP)
        self.addCleanup(self.tempdir.cleanup)


# ----------------------------------------------------------------------
# 1. 七个清不掉的平台：占位消息**根本不发**
# ----------------------------------------------------------------------
class NoStalePlaceholderTests(TurnTestCase):
    def test_every_platform_without_message_edit_declares_it(self):
        for name in PLATFORMS_WITHOUT_MESSAGE_EDIT:
            with self.subTest(platform=name):
                self.assertFalse(build_platform(name).adapter.supports_message_edit)

    def test_no_progress_message_is_sent_at_all_on_those_seven(self):
        """这就是修好的形态：**零**条进度消息，所以没有什么可变成僵尸。"""
        for name in PLATFORMS_WITHOUT_MESSAGE_EDIT:
            with self.subTest(platform=name):
                under_test = build_platform(name)

                run_one_turn(under_test, ANSWER)

                self.assertEqual(
                    under_test.progress_sends(), [],
                    "%s 仍然发了进度占位消息，而它永远清不掉" % name,
                )
                self.assertEqual(under_test.left_showing_the_placeholder(), [])

    def test_the_answer_reaches_those_seven_exactly_once_byte_for_byte(self):
        for name in PLATFORMS_WITHOUT_MESSAGE_EDIT:
            with self.subTest(platform=name):
                under_test = build_platform(name)

                run_one_turn(under_test, ANSWER)

                self.assertEqual(
                    under_test.identities_whose_last_text_is(ANSWER),
                    sorted(set(under_test.identities())),
                    "%s 的答复必须落在唯一一条消息上，且末尾逐字节相等" % name,
                )
                self.assertEqual(
                    under_test.wire_text(), [ANSWER],
                    "%s 的线路上只应有答复本身，一条不多一条不少" % name,
                )


# ----------------------------------------------------------------------
# 2. 六个能改写的平台：占位消息照发，**并被顶掉**
# ----------------------------------------------------------------------
class EditingPlatformsAreUnregressedTests(TurnTestCase):
    def test_the_placeholder_is_still_sent_on_those_six(self):
        """这条好路不能被顺手砍掉：占位消息**仍然要发**，它就是流式进度。"""
        for name in PLATFORMS_WITH_MESSAGE_EDIT:
            with self.subTest(platform=name):
                under_test = build_platform(name)

                run_one_turn(under_test, ANSWER)

                sent = under_test.progress_sends()
                self.assertEqual(len(sent), 1, "%s 应当仍然发一条占位消息" % name)
                self.assertEqual(sent[0].text, PROGRESS_TEXT)

    def test_the_placeholder_is_edited_in_place_and_leaves_nothing_behind(self):
        for name in PLATFORMS_WITH_MESSAGE_EDIT:
            with self.subTest(platform=name):
                under_test = build_platform(name)

                run_one_turn(under_test, ANSWER)

                self.assertEqual(
                    under_test.left_showing_the_placeholder(), [],
                    "%s 的占位气泡没有被顶掉" % name,
                )
                placeholder_identity = under_test.progress_sends()[0].identity
                written = under_test.identities()[placeholder_identity]
                self.assertGreater(
                    len(written), 1,
                    "%s 的占位气泡只被发过一次、一次也没被改写过" % name,
                )
                self.assertEqual(written[-1], ANSWER)
                self.assertTrue(
                    all(d.edited for d in under_test.deliveries if d.via == "edit"),
                    "%s 有一次原地改写失败了" % name,
                )

    def test_only_one_message_ever_carries_the_answer_on_those_six(self):
        for name in PLATFORMS_WITH_MESSAGE_EDIT:
            with self.subTest(platform=name):
                under_test = build_platform(name)

                run_one_turn(under_test, ANSWER)

                self.assertEqual(len(under_test.identities()), 1)
                self.assertEqual(
                    under_test.identities_whose_last_text_is(ANSWER),
                    [under_test.progress_sends()[0].identity],
                )


# ----------------------------------------------------------------------
# 3. 十三个平台：答复恰好一次、逐字节相同
# ----------------------------------------------------------------------
class TheAnswerArrivesExactlyOnceTests(TurnTestCase):
    def test_every_registered_platform_delivers_the_answer_exactly_once(self):
        """⚠️ 覆盖面守门：13 个已注册平台**一个不漏**地跑完整一轮。"""
        self.assertEqual(
            sorted(registered_names()), sorted(_PLATFORM_SETUP),
            "新增平台必须在这里登记一份夹具，否则它不受任何断言保护",
        )
        for name in registered_names():
            with self.subTest(platform=name):
                under_test = build_platform(name)

                run_one_turn(under_test, ANSWER)

                self.assertEqual(
                    under_test.left_showing_the_placeholder(), [],
                    "%s 留下了僵尸占位气泡" % name,
                )
                self.assertEqual(
                    len(under_test.identities_whose_last_text_is(ANSWER)), 1,
                    "%s 的答复没有落在唯一一条消息上" % name,
                )
                self.assertEqual(
                    under_test.identities()[
                        under_test.identities_whose_last_text_is(ANSWER)[0]
                    ][-1],
                    ANSWER,
                    "%s 的答复不是逐字节到达的" % name,
                )

    def test_a_long_answer_is_not_truncated_and_not_duplicated(self):
        """长答复会被切分；**原样**到达，且只落在一条消息上。

        ⚠️ 能改写的六个平台上，超过 ``bridge.max_message_chars`` 的答复会让
        :meth:`~opencode_bridge.outbound.OutboundSender.finalize` 放弃改写、
        改发新消息（那是既有行为，见该方法的注释）。所以本例只断言
        「不丢、不截断、不重复」，**不**断言"只有一条消息"—— 那是上面短答复那几条
        已经钉住的不变量。
        """
        for name in registered_names():
            with self.subTest(platform=name):
                under_test = build_platform(name)

                run_one_turn(under_test, LONG_ANSWER)

                self.assertEqual(
                    under_test.left_showing_the_placeholder(), [],
                    "%s 长答复这一轮留下了僵尸占位气泡" % name,
                )
                carrying = under_test.identities_whose_last_text_is(LONG_ANSWER)
                self.assertEqual(
                    len(carrying), 1,
                    "%s 的长答复落在了 %d 条消息上（丢了或重复了）"
                    % (name, len(carrying)),
                )


# ----------------------------------------------------------------------
# 4. 声明与事实相符 —— 声明不是许愿
# ----------------------------------------------------------------------
class TheDeclarationMatchesTheAdapterTests(TurnTestCase):
    def test_edit_returns_true_exactly_where_the_declaration_claims_it(self):
        """对着**每个平台**真调一次 ``edit()``，返回值必须与声明一致。

        这是"声明不许撒谎"的那道闸：有人给一个恒返回 ``False`` 的适配器写上
        ``supports_message_edit = True``（或者反过来藏起一个真能改写的平台），
        这里立刻红。
        """
        for name in registered_names():
            with self.subTest(platform=name):
                under_test = build_platform(name)
                adapter = under_test.adapter
                handle = MsgHandle(
                    conversation_id=under_test.conversation_id,
                    # 纯数字：Telegram 的 ``editMessageText`` 要 ``int(message_id)``，
                    # 用 "probe-1" 会让它在解析 id 那一步就返回 False —— 那是
                    # "句柄不合法"，不是"平台不能改写"，不能拿来当能力判据。
                    message_id="4242", platform=name,
                )

                edited = adapter.edit(handle, Outbound(
                    conversation_id=under_test.conversation_id, text="probe",
                ))


                self.assertEqual(
                    edited, adapter.supports_message_edit,
                    "%s 的 supports_message_edit=%r 与 edit() 的实际返回 %r 不符"
                    % (name, adapter.supports_message_edit, edited),
                )

    def test_the_outbound_gate_is_the_only_place_progress_is_filtered(self):
        """闸门在出站咽喉上，所以调用方一个 if 都不用加 —— 用源码守住这一点。"""
        import inspect

        from opencode_bridge.outbound import OutboundSender

        source = inspect.getsource(OutboundSender.send_text)
        self.assertIn("supports_message_edit", source)
        self.assertIn('kind == "progress"', source)

    def test_a_missing_adapter_never_sends_a_placeholder(self):
        """路由已经坏了的场合：不发，也不多报一个故障。"""
        from opencode_bridge.outbound import OutboundSender

        sender = OutboundSender(adapter_for=lambda conversation_id: None,
                                max_message_chars=4000)

        self.assertIsNone(
            sender.send_text("nowhere:1", PROGRESS_TEXT, kind="progress")
        )
        self.assertIsNone(
            sender.send_text("nowhere:1", "hello", kind="text")
        )


if __name__ == "__main__":
    unittest.main()

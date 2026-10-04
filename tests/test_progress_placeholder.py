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
from opencode_bridge.outbound import OutboundSender
from opencode_bridge.state import StateStore

#: 短答复：远低于**每一个**平台的分片阈值，所以"线上文本"就是答复本身，
#: 逐字节断言成立（长答复另有一例，见 :class:`LongAnswerIsNotTruncatedTests`）。
ANSWER = "构建失败是因为 config.py 里少了 default 分支。"

#: 长答复：5000 个 ASCII 字符 —— 超过 IRC / Twitch 的 400，超过 Discord / QQ 的 2000，
#: 超过 email 的 998 行长预算，但远小于 Nextcloud / Slack。
LONG_ANSWER = "0123456789" * 500

#: ``run_one_turn`` 给 ``bridge.max_message_chars`` 配的值；与
#: :func:`one_message_budget` 共用同一份，免得两处漂移。
BRIDGE_MAX_MESSAGE_CHARS = 4000

#: 「按顺序一小片一小片投递」时每片多长。比任何一个平台的单条上限都小得多，
#: 所以**每一种长度**下流式改写都真的发生过 —— 否则超长答复一次都不会被改写，
#: "收尾那一步改不动"的前提根本无从复现。
STREAMING_CHUNK = 137

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
    #: 第几次 ``edit()`` 起开始**假装失败**（``None`` = 不装）。用来复现
    #: "改写这一步真的失败"—— 平台能力 / 部署配置 / 个别客户端 / 网络都可能。
    fail_edits_from: int | None = None
    #: 这一轮一共发生过几次 ``edit()``（含失败的那些）。
    edit_calls: int = 0

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
        under_test.edit_calls += 1
        should_fail = (
            under_test.fail_edits_from is not None
            and under_test.edit_calls >= under_test.fail_edits_from
        )
        if should_fail:
            # 平台真的改不动那条消息（部署关掉了能力 / 客户端不支持 / 网络抖动）。
            # 底下**不**调真实现：真实现会成功，那样就复现不了"改不动"。
            under_test.deliveries.append(Delivery(
                identity=handle.message_id, text=out.text, via="edit",
                kind=out.kind, edited=False,
            ))
            return False
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


def one_message_budget(adapter) -> int:
    """这个平台"一条消息"到底装得下多少 —— 与
    :meth:`OutboundSender.finalize` 算的是同一个数（取两条上限的小者）。

    测试从**实现同一处**取这个预算，而不是把 4000 抄一遍：抄的那份迟早会与
    实现漂移，而漂移的方向恰好是"测试还以为装得下"。
    """
    return min(BRIDGE_MAX_MESSAGE_CHARS, int(adapter.effective_max_length))


def answer_of_length(total: int) -> str:
    """一条**恰好** ``total`` 个字符的 ASCII 答复，每 60 字符一个换行。

    纯 ASCII 是有意的：ntfy 的上限是**字节**、IRC 的兜底也按字节再切一遍，
    中文会让"多少字符算一条消息"在测试里变得不可预测。换行是为了给
    :func:`~opencode_bridge.split.split_text` 一个自然断点，分段才可复现。
    """
    filler = "answer text that must arrive whole, in order, exactly once."
    characters: list[str] = []
    index = 0
    while len(characters) < total:
        if characters and len(characters) % 60 == 0:
            characters.append("\n")
        characters.append(filler[index % len(filler)])
        index += 1
    return "".join(characters)[:total]


def length_boundaries(budget: int) -> tuple[tuple[str, str], ...]:
    """四个边界，每个都返回 ``(名字, 恰好那个长度的答复)``。"""
    return (
        ("short", answer_of_length(max(1, budget // 2))),
        ("exactly_at_budget", answer_of_length(budget)),
        ("just_over", answer_of_length(budget + 51)),
        ("far_over", answer_of_length(budget * 3 + 137)),
    )


def reader_view(under_test: "PlatformUnderTest") -> str:
    """读者**最终**看到的东西：每条消息最后写入的文本，按消息出现顺序拼起来。

    只取"最后一次写入"是因为同一条消息会被改写很多次（流式 + 收尾），而读者
    看到的是最后那一版；只取"每条消息一次"是因为那正是"一条消息 = 一段内容"
    这个模型。两者合起来，拼接结果与原文不等，就一定是丢了 / 重复了 / 乱序了 /
    留了半截。
    """
    order: list[str] = []
    for delivery in under_test.deliveries:
        if delivery.identity not in order:
            order.append(delivery.identity)
    written = under_test.identities()
    return "".join(written[identity][-1] for identity in order)


def normalise(text: str) -> str:
    """占位消息文案的归一化版本（与 :attr:`PlatformUnderTest.normalise_wire`
    同一套规则，邮件那边折行、其余原样）。"""
    return "".join(text.splitlines())


def uncontinued_fragments(
    under_test: "PlatformUnderTest", answer: str,
) -> list[tuple[str, str]]:
    """那些"最终停在答复真前缀上、而续段没跟上"的残留。

    这就是旧契约留下的东西：占位消息永远显示半截正文，读者先读到半句、再在
    别的消息里读到全文 —— 同一段话出现两次，而且那半句看上去像完整的一句话。
    """
    order: list[str] = []
    for delivery in under_test.deliveries:
        if delivery.identity not in order:
            order.append(delivery.identity)
    written = under_test.identities()
    leftovers: list[tuple[str, str]] = []
    for index, identity in enumerate(order):
        final_text = written[identity][-1]
        is_partial = 0 < len(final_text) < len(answer) and answer.startswith(final_text)
        if not is_partial:
            continue
        # 续段必须出现在**它之后**：把它之后所有消息的最终文本拼起来看
        # 是否真的包含剩下的那一截。顺序错了也算残留 —— 读者读到的是乱的。
        after = "".join(written[later][-1] for later in order[index + 1:])
        if not after.startswith(answer[len(final_text):]):
            leftovers.append((identity, final_text))
    return leftovers


def run_one_turn(
    under_test: PlatformUnderTest, answer: str, *, stream_progress: bool = True,
    chunk_size: int | None = None,
) -> None:
    """在**真桥**上跑完整的一轮：入站 → prompt → 流式 delta → 收尾。

    桥那一侧全部是真的（``BridgeCore`` / ``EventStream`` / ``InboundGateway`` /
    ``OutboundSender``），只有最底下的线路是记录器 —— 所以走的就是生产路径。

    ``edit_interval_seconds=0`` 让流式改写不被节流挡掉（那与本用例无关）；
    ``stream_progress=False`` 把节流窗口设成极大，让流式改写一次都不发生 ——
    线路记录于是恰好等于「占位消息 + 最终答复」，逐字节比对不含歧义。

    ``chunk_size=None`` 时按"两半、**先到后半**"投递，那是**乱序**投递，
    用来钉住 :meth:`Turn.assemble` 会按 ordinal 排序。给了 ``chunk_size`` 就按顺序
    一小片一小片投递 —— 更贴近真实模型输出，也让**每一种长度**下占位消息都真的被
    流式写过（否则超长答复一次流式改写都不会发生，"改不动"那条路就无从复现）。
    """
    os.makedirs(_REPOSITORY_TEMP, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=_REPOSITORY_TEMP) as tempdir:
        config = Config()
        config.bridge = {
            "edit_interval_seconds": 0 if stream_progress else 10 ** 9,
            "max_message_chars": BRIDGE_MAX_MESSAGE_CHARS,
        }
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
            if chunk_size is None:
                # 两半、故意先投后半：证明收尾时拼回的是按 ordinal 排的原文。
                halves = [
                    (1, answer[len(answer) // 2:]),
                    (0, answer[:len(answer) // 2]),
                ]
            else:
                halves = [
                    (index, answer[start:start + chunk_size])
                    for index, start in enumerate(range(0, len(answer), chunk_size))
                ]
            for ordinal, delta in halves:
                stream.dispatch(event(
                    "session.text.delta", sessionID=session_id,
                    assistantMessageID="msg_a", ordinal=ordinal, delta=delta,
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
        """长答复：**按顺序**读下来逐字节等于原文，且没有一条消息停在半截上。

        ⚠️ 这条断言**改过**：它原来要求"整条长答复落在**一条**消息上"，那是
        旧契约（超长就放弃占位消息、整段另发）。新契约下超长答复会**跨**多条消息
        —— 占位消息承载开头那一段、其余作为后续消息 —— 所以判据必须是**拼接**，
        而不是"某一条消息等于全文"。
        """
        for name in registered_names():
            with self.subTest(platform=name):
                under_test = build_platform(name)

                run_one_turn(under_test, LONG_ANSWER)

                self.assertEqual(
                    under_test.left_showing_the_placeholder(), [],
                    "%s 长答复这一轮留下了僵尸占位气泡" % name,
                )
                self.assertEqual(
                    reader_view(under_test), LONG_ANSWER,
                    "%s 的长答复按顺序读下来不等于原文" % name,
                )

    # ------------------------------------------------------------------
    # 整条长度轴：四个边界 × 十三个平台
    # ------------------------------------------------------------------
    def test_every_length_boundary_delivers_the_whole_answer_in_order(self):
        """短 / 正好装满 / 刚超 / 远超 —— 四个边界，十三个平台，逐字节。

        判据是**读者最终看到的东西**：每条消息**最后**写入的文本，按消息出现的
        顺序拼起来。这一条同时否掉四种坏结果：

        * **丢** —— 拼接短于原文；
        * **重复** —— 拼接长于原文；
        * **乱序** —— 拼接不等于原文；
        * **留下半截** —— 拼接里混进了 ``⏳ 处理中…`` 或任何没被续上的片段。
        """
        for name in registered_names():
            budget = one_message_budget(build_platform(name).adapter)
            for boundary, answer in length_boundaries(budget):
                with self.subTest(platform=name, boundary=boundary):
                    under_test = build_platform(name)

                    run_one_turn(under_test, answer)

                    self.assertEqual(
                        reader_view(under_test), answer,
                        "%s 在 %s 边界上：读者读到的不是完整原文"
                        % (name, boundary),
                    )
                    self.assertEqual(
                        under_test.left_showing_the_placeholder(), [],
                        "%s 在 %s 边界上留下了僵尸占位气泡" % (name, boundary),
                    )

    def test_a_runtime_edit_failure_still_delivers_the_whole_answer(self):
        """**收尾那一步改不动**时（部署关掉能力 / 客户端不支持 / 网络抖动），
        读者仍然拿到完整答复，而且没有半截没人接。

        复现方式：先跑一遍量出这一轮共有几次 ``edit()``，再跑一遍让**最后一次**
        （也就是收尾那一次）失败 —— 流式那些改写照常成功，所以占位消息在那一刻
        显示着一段正文，而桥改不动它。

        ⚠️ 这一条是补上的**漏洞**，不是锦上添花：没有它的时候，
        ``Turn.shown_progress_text`` 到底记没记上**没有任何断言**，
        所以把记录条件反过来（记失败的那次）整个测试集照样全绿。
        """
        for name in PLATFORMS_WITH_MESSAGE_EDIT:
            boundaries = dict(length_boundaries(
                one_message_budget(build_platform(name).adapter)
            ))
            for boundary in ("short", "just_over", "far_over"):
                answer = boundaries[boundary]
                with self.subTest(platform=name, boundary=boundary):
                    probe = build_platform(name)
                    run_one_turn(probe, answer, chunk_size=STREAMING_CHUNK)
                    finalize_edit_is_the_last = probe.edit_calls
                    self.assertGreater(
                        finalize_edit_is_the_last, 1,
                        "%s 在 %s 边界上流式改写一次都没成功过 —— "
                        "那这条用例就测不到'占位消息已经显示着一段正文'这个前提"
                        % (name, boundary),
                    )

                    under_test = build_platform(name)
                    under_test.fail_edits_from = finalize_edit_is_the_last
                    run_one_turn(under_test, answer, chunk_size=STREAMING_CHUNK)

                    self.assertEqual(
                        reader_view(under_test), answer,
                        "%s 在 %s 边界上收尾改写失败：读者读到的不是完整原文"
                        % (name, boundary),
                    )
                    self.assertEqual(
                        under_test.left_showing_the_placeholder(), [],
                        "%s 在 %s 边界上收尾改写失败后留下了僵尸占位气泡"
                        % (name, boundary),
                    )
                    self.assertEqual(
                        uncontinued_fragments(under_test, answer), [],
                        "%s 在 %s 边界上收尾改写失败后留下了没人接下去的半截"
                        % (name, boundary),
                    )

    def test_the_wire_carries_the_whole_answer_exactly_once_at_every_boundary(self):
        """同一条判据，这次落在**线路**上（适配器分片之后）。

        与 :func:`reader_view` 分工：那条量的是"桥交给适配器的东西 + 每条消息的
        最终版本"，这条量的是**真正发出去的每一段** —— 所以它连"适配器把答复切错 /
        切重 / 切漏"也能抓到。

        这里把流式改写关掉，于是线路记录恰好等于「占位消息（若平台支持）+ 最终
        答复」，比对不含任何歧义；流式那一路由 :func:`reader_view` 那条覆盖。
        """
        for name in registered_names():
            budget = one_message_budget(build_platform(name).adapter)
            for boundary, answer in length_boundaries(budget):
                with self.subTest(platform=name, boundary=boundary):
                    under_test = build_platform(name)

                    run_one_turn(under_test, answer, stream_progress=False)

                    transcript = "".join(under_test.wire_text())
                    expected = under_test.normalise_wire(answer)
                    if under_test.progress_sends():
                        self.assertTrue(
                            transcript.startswith(normalise(PROGRESS_TEXT)),
                            "%s 的线路第一段应当是占位消息" % name,
                        )
                        transcript = transcript[len(normalise(PROGRESS_TEXT)):]
                    self.assertEqual(
                        transcript, expected,
                        "%s 在 %s 边界上：线路上的文本拼不回原文"
                        % (name, boundary),
                    )

    def test_no_message_is_left_holding_a_fragment(self):
        """**显式**断言：没有哪条消息最后停在一段"没人接下去"的正文上。

        与 :meth:`reader_view` 是同一件事的正面说法：这里把"半截"定义出来 ——
        一条消息的最终文本是答复的**真前缀**（不是全部），而它的续段**没有**在它
        之后出现。那正是旧契约留下的残留（占位消息永远停在半截正文上）。
        """
        for name in registered_names():
            budget = one_message_budget(build_platform(name).adapter)
            for boundary, answer in length_boundaries(budget):
                if boundary == "short":
                    continue  # 装得下 ⇒ 不会有前缀型残留
                with self.subTest(platform=name, boundary=boundary):
                    under_test = build_platform(name)

                    run_one_turn(under_test, answer)

                    fragments = uncontinued_fragments(under_test, answer)
                    self.assertEqual(
                        fragments, [],
                        "%s 在 %s 边界上留下了没人接下去的半截：%r"
                        % (name, boundary, fragments),
                    )

    def test_a_prefix_holding_message_really_does_occur_at_the_long_boundaries(self):
        """上一条不能是空断言：得先证明「占位消息承载开头那一段」确实发生。"""
        holders = 0
        for name in PLATFORMS_WITH_MESSAGE_EDIT:
            under_test = build_platform(name)
            boundaries = dict(length_boundaries(
                one_message_budget(under_test.adapter)
            ))
            answer = boundaries["far_over"]

            run_one_turn(under_test, answer)

            placeholder_identity = under_test.progress_sends()[0].identity
            final_text = under_test.identities()[placeholder_identity][-1]
            if 0 < len(final_text) < len(answer) and answer.startswith(final_text):
                holders += 1
        self.assertEqual(
            holders, len(PLATFORMS_WITH_MESSAGE_EDIT),
            "有能改写的平台并没有让占位消息承载开头那一段 —— "
            "说明这条路径根本没被走到，后面的断言就是空的",
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


        source = inspect.getsource(OutboundSender.send_text)
        self.assertIn("supports_message_edit", source)
        self.assertIn('kind == "progress"', source)

    def test_a_missing_adapter_never_sends_a_placeholder(self):
        """路由已经坏了的场合：不发，也不多报一个故障。"""

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

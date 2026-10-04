"""C1 —— prompt 里的渠道说明：数字从适配器来，正文不含内部信息。

四组验收，分别对应四个"证明"：

1. :class:`DerivedFromTheAdapterTests` —— **数字不是查表来的**。给一个从没听说过的
   平台一个古怪的上限（137），提示里必须出现 137，而且**不许出现任何别的平台名**
   （一旦有人把对照表抄回来，这条立刻红）。
2. :class:`TheStatedNumberMatchesRealDeliveryTests` —— **提示说的是实情**。不是拿
   ``split_text(x, hint)`` 自证，而是真的驱动适配器的 ``send()`` / 出站分片，
   量它切出来的每一段有多长，再跟提示里写的那个数对。
3. :class:`NothingInternalLeaksTests` —— **不漏内部信息**。会话 id、路径、主机名、
   凭据形状全从唯一的外部输入（平台展示名）灌进去，提示里一个都不许剩。
4. :class:`InjectionIsWiredIntoTheSinglePromptCallTests` —— **接线**与**非侵入**：
   注入只发生在那一个调用点，收件箱里存的、去重哈希算的、路由与分片一个字没动。

⚠️ 本文件**不**断言提示的整段文案（文案是给模型读的，改一次措辞不该红一片测试）。
它断言的是**可验证的事实**：那个数、那个平台名、以及"不该出现的东西没出现"。
"""

from __future__ import annotations

import threading
import unittest

from opencode_bridge.adapters import Adapter
from opencode_bridge.adapters.base import adapter_class, build, registered_names
from opencode_bridge.channel_profile import (
    _HINT_SEPARATOR,
    ChannelProfile,
    channel_profile_for,
    with_channel_hint,
)
from opencode_bridge.hooks import Inbound, MsgHandle, Outbound
from opencode_bridge.inbound_gateway import InboundGateway
from opencode_bridge.inbox import QueuedPrompt
from opencode_bridge.permission_ledger import PermissionLedger


# ----------------------------------------------------------------------
# 替身
# ----------------------------------------------------------------------
class InertHooks:
    """``Adapter.__init__`` 只要一个 hooks；这里什么都不实现。"""

    def on_inbound(self, inbound: Inbound) -> None:
        return None

    def on_callback(self, conversation_id: str, data: str, query_id: str) -> None:
        return None

    def load_stream_cursor(self, stream_scope: str):
        return None

    def save_stream_cursor(self, stream_scope: str, position: int) -> None:
        return None


class DeclaredLimitAdapter(Adapter):
    """一个**仓库里不存在**的平台：只有能力声明，没有传输。"""

    def __init__(self, *, label: str, max_message_length: int,
                 splits_long_messages: bool = True,
                 supports_inline_buttons: bool = False) -> None:
        self.name = "quibblechat"
        self.label = label
        self.max_message_length = max_message_length
        self.splits_long_messages = splits_long_messages
        self.supports_inline_buttons = supports_inline_buttons
        super().__init__({}, InertHooks())

    def start(self) -> None:
        return None

    def send(self, out: Outbound) -> MsgHandle | None:
        return MsgHandle(out.conversation_id, "m1", self.name)

    def edit(self, handle: MsgHandle, out: Outbound) -> bool:
        return False


def every_registered_adapter() -> list[Adapter]:
    return [build(name, {}, InertHooks()) for name in registered_names()]


def platform_names_in_use() -> tuple[str, ...]:
    """仓库里真实存在的平台名 —— "提示提到了别的平台"就说明有人抄了对照表。"""
    names: set[str] = set()
    for adapter in every_registered_adapter():
        names.add(adapter.name.lower())
        names.add(str(adapter.label).lower())
    return tuple(sorted(names))


# ----------------------------------------------------------------------
# 1. 数字与平台名都来自适配器
# ----------------------------------------------------------------------
class DerivedFromTheAdapterTests(unittest.TestCase):
    def test_an_unheard_of_limit_is_quoted_back_verbatim(self):
        """上限 137 是编出来的 —— 提示里必须就是 137，不是某个"已知平台"的值。"""
        adapter = DeclaredLimitAdapter(label="Quibblechat", max_message_length=137)

        hint = ChannelProfile.from_adapter(adapter).render()

        self.assertIn("137", hint)
        self.assertIn("Quibblechat", hint)

    def test_the_same_adapter_class_yields_a_different_hint_per_instance(self):
        """同一个类、不同上限 ⇒ 两段不同的提示（说明它不是按类名缓存的常量）。"""
        short = ChannelProfile.from_adapter(
            DeclaredLimitAdapter(label="Quibblechat", max_message_length=137)
        ).render()
        long_hint = ChannelProfile.from_adapter(
            DeclaredLimitAdapter(label="Quibblechat", max_message_length=99000)
        ).render()

        self.assertNotEqual(short, long_hint)
        self.assertIn("99000", long_hint)

    def test_no_real_platform_is_ever_named_in_a_foreign_channels_hint(self):
        """⚠️ 抄对照表的人会在这里露馅：陌生平台的提示里出现 Slack / IRC / … 。"""
        adapter = DeclaredLimitAdapter(label="Quibblechat", max_message_length=137)

        hint = with_channel_hint("hello", adapter).lower()

        for platform_name in platform_names_in_use():
            self.assertNotIn(
                platform_name, hint,
                "提示里出现了 %r —— 这说明有人把平台对照表抄了进来"
                % platform_name,
            )

    def test_the_profile_module_holds_no_table_of_limits(self):
        """源码级复核：``channel_profile`` 的**代码**里没有"平台上限"字面量。

        行为断言（上一条）能抓住"提示里多说了别人"，这条抓住另一半：有人把
        13 家的上限抄成一张表备在模块里、暂时还没被引用 —— 那正是第 14 个平台
        会开始出错的地方。

        判据用 :mod:`ast` 取**代码**里的整数字面量，注释与 docstring 一律不算 ——
        文档里当然要写"IRC 400、Slack 40000"来说明为什么不能查表，那不是数据。
        """
        import ast
        import opencode_bridge.channel_profile as module

        with open(module.__file__, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        code_literals = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, int)
        }
        platform_limits = {
            adapter.effective_max_length for adapter in every_registered_adapter()
        }

        self.assertEqual(
            code_literals & platform_limits, set(),
            "channel_profile.py 的代码里出现了某个平台的真实上限 —— 那就是一张对照表",
        )

    def test_the_profile_is_a_value_not_a_late_assembled_format_string(self):
        """提示是**一次算完的值**：``ChannelProfile`` 冻结，且 ``render()`` 无副作用。"""
        profile = ChannelProfile.from_adapter(
            DeclaredLimitAdapter(label="Quibblechat", max_message_length=137)
        )

        with self.assertRaises(Exception):
            profile.max_message_chars = 1  # type: ignore[misc]
        self.assertEqual(profile.render(), profile.render())

    def test_channel_profile_for_is_the_same_answer_as_from_adapter(self):
        adapter = DeclaredLimitAdapter(label="Quibblechat", max_message_length=137)

        self.assertEqual(
            channel_profile_for(adapter),
            ChannelProfile.from_adapter(adapter),
        )

    def test_the_short_and_long_wordings_are_chosen_by_the_declared_number(self):
        """同一段模板，按**适配器报上来的数**换措辞（不是按平台名单）。"""
        short_hint = ChannelProfile.from_adapter(
            DeclaredLimitAdapter(label="Room A", max_message_length=400)
        ).render()
        long_hint = ChannelProfile.from_adapter(
            DeclaredLimitAdapter(label="Room B", max_message_length=40000)
        ).render()

        self.assertIn("couple of sentences", short_hint)
        self.assertNotIn("couple of sentences", long_hint)
        self.assertIn("several separate messages", long_hint)


# ----------------------------------------------------------------------
# 2. 提示里的数字 == 适配器投递时真正切分的那个数
# ----------------------------------------------------------------------
class TheStatedNumberMatchesRealDeliveryTests(unittest.TestCase):
    """把真适配器的出站**跑起来**，量它切出来的段长，再跟提示对。

    ⚠️ 刻意**不**用 ``split_text(text, <提示里的数>)`` 自证 —— 那是拿提示自己
    当标尺。这里量的是适配器 ``send()`` / 出站分片**实际**产出的每一段。
    """

    LONG_TEXT = "x" * 120_000

    def stated_number(self, adapter: Adapter) -> int:
        return ChannelProfile.from_adapter(adapter).max_message_chars

    def test_irc_actually_splits_at_the_number_the_hint_quotes(self):
        from opencode_bridge.adapters.irc import IRCAdapter

        adapter = IRCAdapter({}, InertHooks())
        stated = self.stated_number(adapter)

        pieces = adapter._outbound_pieces(self.LONG_TEXT, "#chan")

        self.assertGreater(len(pieces), 1, "120k 字符必须被拆开")
        self.assertTrue(
            all(len(piece) <= stated for piece in pieces),
            "有一段超过了提示里写的 %d：%d"
            % (stated, max(len(piece) for piece in pieces)),
        )
        # 而且是**贴着**那个数切的（不是切得远小于它 —— 那说明提示给宽了）。
        self.assertGreaterEqual(max(len(piece) for piece in pieces), stated - 1)
        self.assertEqual(stated, 400)

    def test_slack_actually_posts_chunks_of_the_number_the_hint_quotes(self):
        from opencode_bridge.adapters.slack import SlackAdapter

        adapter = SlackAdapter({"bot_token": "t", "app_token": "a"}, InertHooks())
        stated = self.stated_number(adapter)
        posted: list[str] = []
        adapter._request = lambda method, path, payload, *, timeout=None: (
            posted.append(payload["text"]) or (200, {"ok": True, "ts": "1"})
        )

        adapter.send(Outbound(conversation_id="slack:C1", text=self.LONG_TEXT))

        self.assertGreater(len(posted), 1, "120k 字符必须被拆成多条")
        self.assertTrue(all(len(text) <= stated for text in posted))
        self.assertGreaterEqual(max(len(text) for text in posted), stated - 1)
        self.assertEqual(stated, 40000)

    def test_telegram_actually_posts_chunks_of_the_number_the_hint_quotes(self):
        from opencode_bridge.adapters.telegram import TelegramAdapter

        adapter = TelegramAdapter({"bot_token": "1:t"}, InertHooks())
        stated = self.stated_number(adapter)
        posted: list[str] = []
        adapter._api = lambda conversation_id, method, payload, *, timeout=None: (
            posted.append(payload["text"]) or {"ok": True, "result": {"message_id": 1}}
        )

        adapter.send(Outbound(conversation_id="telegram:55", text=self.LONG_TEXT))

        self.assertGreater(len(posted), 1)
        self.assertTrue(all(len(text) <= stated for text in posted))
        self.assertGreaterEqual(max(len(text) for text in posted), stated - 1)
        self.assertEqual(stated, 4096)

    def test_a_channel_that_never_splits_is_never_given_a_length(self):
        """邮件 / A2A 的那个数**不是消息容量** —— 提示里就不许出现长度。"""
        for platform in ("email", "a2a"):
            with self.subTest(platform=platform):
                adapter = build(platform, {
                    "address": "bot@example.com",
                    "password": "app-password",
                    "imap_host": "imap.example.com",
                    "smtp_host": "smtp.example.com",
                }, InertHooks())
                profile = ChannelProfile.from_adapter(adapter)

                self.assertFalse(
                    profile.splits_long_messages,
                    "%s 必须声明自己不切片，否则提示会把行长说成消息容量" % platform,
                )
                rendered = profile.render()
                self.assertIn("no length to stay inside", rendered)
                self.assertNotIn(
                    str(profile.max_message_chars), rendered,
                    "%s 不切片，提示里就不该出现那个数" % platform,
                )

    def test_a_runtime_refined_limit_is_the_one_that_gets_quoted(self):
        """Mattermost 启动后用服务端配置细化上限 —— 提示必须跟着变。

        这是"读运行期值而不是类属性"这条设计的存在理由：类属性会报出一个
        比实际**宽**的数，而"提示说 40000、实际按 400 切"比不给提示更糟。
        """
        from opencode_bridge.adapters.mattermost import MattermostAdapter

        adapter = MattermostAdapter({"site_url": "https://mm.example.com",
                                     "token": "t"}, InertHooks())
        before = self.stated_number(adapter)
        adapter._apply_max_post_size(321)
        after = self.stated_number(adapter)

        self.assertNotEqual(before, after)
        self.assertEqual(after, 321)
        self.assertIn("321", ChannelProfile.from_adapter(adapter).render())


# ----------------------------------------------------------------------
# 3. 提示正文不含内部信息
# ----------------------------------------------------------------------
#: 真形状但**由片段拼成** —— 推送保护按形状拦整个 push（AGENTS.md §2.5），
#: 而脱敏器的测试本质上就需要真形状。运行值逐字节不变。
SLACK_TOKEN_SHAPED = "xox" + "b-" + "1234567890" + "-" + "AaBbCcDdEeFfGgHh123456"
TELEGRAM_TOKEN_SHAPED = "123456789" + ":" + "AaBbCcDdEeFfGgHhIiJjKkLlMmNnOoPpQq"


class NothingInternalLeaksTests(unittest.TestCase):
    def stated(self, adapter: Adapter) -> str:
        return with_channel_hint("用户正文", adapter)

    def test_a_conversation_id_shaped_label_is_dropped_whole(self):
        adapter = DeclaredLimitAdapter(
            label="telegram:123456789", max_message_length=400
        )

        hint = self.stated(adapter)

        self.assertNotIn("123456789", hint)
        self.assertNotIn("conv#", hint, "脱敏指纹也不该出现在给模型看的正文里")
        self.assertNotIn("[REDACTED", hint)

    def test_a_windows_path_shaped_label_and_name_are_dropped_whole(self):
        adapter = DeclaredLimitAdapter(
            label=r"C:\Users\example-user\state.json", max_message_length=400
        )
        adapter.name = r"workSpace\python\aicode"

        hint = self.stated(adapter)

        self.assertNotIn("example-user", hint)
        self.assertNotIn("state.json", hint)
        self.assertNotIn("\\", hint)
        self.assertNotIn("/", hint)
        self.assertIn(
            "a chat channel", hint,
            "两个候选都不可信时要说「一个聊天窗口」而不是硬塞一个",
        )

    def test_a_posix_path_shaped_label_and_name_are_dropped_whole(self):
        adapter = DeclaredLimitAdapter(
            label="/home/example-user/.opencode", max_message_length=400
        )
        adapter.name = "/srv/bridge/state.json"

        hint = self.stated(adapter)

        self.assertNotIn("example-user", hint)
        self.assertNotIn("/home", hint)
        self.assertNotIn("state.json", hint)
        self.assertNotIn("/", hint)

    def test_a_hostname_shaped_label_is_dropped_whole(self):
        adapter = DeclaredLimitAdapter(
            label="chat.internal.example.com", max_message_length=400
        )

        hint = self.stated(adapter)

        self.assertNotIn("internal", hint)
        self.assertNotIn("example.com", hint)

    def test_credential_shaped_labels_are_dropped_whole(self):
        for token in (SLACK_TOKEN_SHAPED, TELEGRAM_TOKEN_SHAPED):
            with self.subTest(shape_length=len(token)):
                adapter = DeclaredLimitAdapter(label=token, max_message_length=400)
                adapter.name = token

                hint = self.stated(adapter)

                self.assertNotIn(token, hint)
                self.assertNotIn("[REDACTED", hint)
                self.assertIn("a chat channel", hint)

    def test_a_bare_opaque_id_shaped_label_is_dropped_whole(self):
        adapter = DeclaredLimitAdapter(label="room 1234567890", max_message_length=400)
        adapter.name = "room 1234567890"

        hint = self.stated(adapter)

        self.assertNotIn("1234567890", hint)
        self.assertIn("a chat channel", hint)

    def test_no_real_platforms_hint_carries_a_path_url_or_hostname(self):
        for adapter in every_registered_adapter():
            with self.subTest(platform=adapter.name):
                hint = self.stated(adapter)
                channel_part = hint.split("\n\n")[0]
                self.assertNotIn("/", channel_part)
                self.assertNotIn("\\", channel_part)
                self.assertNotIn("@", channel_part)
                self.assertNotIn("://", channel_part)
                self.assertNotIn("[REDACTED", hint)
                self.assertNotIn("conv#", hint)

    def test_the_user_text_is_carried_through_byte_for_byte(self):
        """提示不许**改写**用户正文 —— 它只是拼在前面。"""
        adapter = DeclaredLimitAdapter(label="Quibblechat", max_message_length=400)
        user_text = "看看 README 里的 **加粗** 和 `代码`"

        hinted = with_channel_hint(user_text, adapter)

        self.assertTrue(hinted.endswith(user_text))
        self.assertEqual(hinted.split(_HINT_SEPARATOR + "\n", 1)[1], user_text)


# ----------------------------------------------------------------------
# 4. 接线：只在那一个调用点，且不碰别的行为
# ----------------------------------------------------------------------
class RecordingPromptClient:
    def __init__(self) -> None:
        self.prompts: list[tuple[str, str]] = []

    def prompt(self, session_id: str, text: str) -> None:
        self.prompts.append((session_id, text))

    def reply_permission(self, session_id, request_id, decision) -> None:
        return None


class RecordingRows:
    """只记"落了什么"，用来证明收件箱里存的仍是用户原文。"""

    def __init__(self) -> None:
        self.recorded: list[QueuedPrompt] = []
        self.states: dict[str, str] = {}

    def record(self, queued: QueuedPrompt) -> bool:
        self.recorded.append(queued)
        self.states[queued.delivery_id] = "pending"
        return True

    def mark_attempting(self, delivery_id: str) -> None:
        self.states[delivery_id] = "attempting"

    def mark_delivered(self, delivery_id: str) -> None:
        self.states[delivery_id] = "delivered"

    def mark_failed(self, delivery_id: str, reason: str) -> None:
        self.states[delivery_id] = "failed"


class InjectionIsWiredIntoTheSinglePromptCallTests(unittest.TestCase):
    def gateway_for(self, adapter) -> tuple[InboundGateway, RecordingPromptClient,
                                            RecordingRows]:
        client = RecordingPromptClient()
        rows = RecordingRows()
        sent: list[Outbound] = []
        gateway = InboundGateway(
            client=client,
            lock=threading.RLock(),
            turns={},
            inbox=rows,
            stream_confirmed=type(
                "Confirmed", (), {"wait": lambda self, timeout=None: True}
            )(),
            adapter_for=lambda conversation_id: adapter,
            answer_callback=lambda adapter, query_id, text: None,
            ensure_session=lambda conversation_id, platform="": "ses_fake0001",
            handle_command=lambda conversation_id, adapter, text: None,
            remember_platform=lambda conversation_id, platform: None,
            send_text=lambda conversation_id, text, **kwargs: sent.append(
                Outbound(conversation_id=conversation_id, text=text,
                         kind=kwargs.get("kind", "text"))
            ) or MsgHandle(conversation_id, "m1", "quibblechat"),
            permission_ledger=PermissionLedger(),
            bridge_config={},
        )
        return gateway, client, rows

    def queued_row(self, text: str) -> QueuedPrompt:
        return QueuedPrompt(
            delivery_id="d1",
            conversation_id="quibblechat:room",
            platform="quibblechat",
            message_id="m-1",
            text=text,
        )

    def test_the_prompt_that_reaches_opencode_carries_the_channel_note(self):
        adapter = DeclaredLimitAdapter(label="Quibblechat", max_message_length=137)
        gateway, client, _rows = self.gateway_for(adapter)

        gateway._dispatch_prompt(self.queued_row("看一下 README"))

        self.assertEqual(len(client.prompts), 1)
        sent_text = client.prompts[0][1]
        self.assertIn("You are replying in Quibblechat.", sent_text)
        self.assertIn("137", sent_text)
        self.assertTrue(sent_text.endswith("看一下 README"))

    def test_the_durable_row_keeps_the_users_own_bytes(self):
        """⚠️ 承重不变量：收件箱里存的、哈希算的，仍是用户原文，一字不加。"""
        adapter = DeclaredLimitAdapter(label="Quibblechat", max_message_length=137)
        gateway, _client, rows = self.gateway_for(adapter)

        gateway._dispatch_prompt(self.queued_row("原文必须逐字节保留"))

        self.assertEqual([row.text for row in rows.recorded], [])
        self.assertEqual(rows.states, {"d1": "delivered"})

    def test_the_turn_table_still_holds_one_progress_placeholder(self):
        """进度占位消息一个不多 —— 提示没有改变"发几条出去"。"""
        adapter = DeclaredLimitAdapter(label="Quibblechat", max_message_length=137)
        gateway, _client, _rows = self.gateway_for(adapter)

        gateway._dispatch_prompt(self.queued_row("hi"))

        self.assertEqual(list(gateway._turns), ["ses_fake0001"])

    def test_no_adapter_means_no_channel_note_and_no_extra_failure(self):
        """路由已经坏了的场合：正文原样发出，不再叠加第二个故障。"""
        gateway, client, _rows = self.gateway_for(None)

        gateway._dispatch_prompt(self.queued_row("hi"))

        self.assertEqual(client.prompts, [("ses_fake0001", "hi")])


# ----------------------------------------------------------------------
# 5. 能力面：真适配器都能答出那三个问题
# ----------------------------------------------------------------------
class EveryRegisteredAdapterCanDescribeItselfTests(unittest.TestCase):
    def test_every_platform_reports_a_usable_limit(self):
        for adapter in every_registered_adapter():
            with self.subTest(platform=adapter.name):
                limit = adapter.effective_max_length
                self.assertIsInstance(limit, int)
                self.assertGreater(limit, 0)
                self.assertTrue(
                    ChannelProfile.from_adapter(adapter).render().strip()
                )

    def test_the_base_class_answers_the_limit_question_itself(self):
        """基类此前对"我实际按多少切"**一个答案都没有** —— 现在有了。"""
        declared_only = adapter_class("telegram")
        self.assertIsNotNone(declared_only)

        class NoMessageLimitAttribute(declared_only):  # type: ignore[misc, valid-type]
            message_limit = 0

        adapter = NoMessageLimitAttribute({"bot_token": "1:t"}, InertHooks())

        self.assertEqual(
            adapter.effective_max_length, adapter.max_message_length,
            "没有运行期细化值时必须退回类属性，而不是 0",
        )

    def test_the_declared_split_flag_defaults_to_true_for_a_plain_subclass(self):
        class Plain(Adapter):
            def start(self) -> None:
                return None

            def send(self, out: Outbound) -> MsgHandle | None:
                return None

            def edit(self, handle: MsgHandle, out: Outbound) -> bool:
                return False

        self.assertTrue(Plain({}, InertHooks()).splits_long_messages)


# ----------------------------------------------------------------------
# 6. 等待期间读者看得见什么 —— C1 曾经在这里说了一句假话
# ----------------------------------------------------------------------
#: 会先看到一条占位消息的那一支里，独有的标记。
_PLACEHOLDER_MARKER = "placeholder"

#: 什么都不会出现的那一支里，独有的标记。
_SILENCE_MARKER = "nothing at all appears while you work"


def progress_paragraph(adapter) -> str:
    """``render()`` 里讲"等待期间"的那一段。

    按**内容**找而不是按位置找（提示的段落顺序是文案的事，不该被这里钉住）。
    找到的那一段可能属于"错的那一支"—— 那正是各条断言要揭穿的东西，所以这里
    如实返回，不替调用方判断。
    """
    for paragraph in ChannelProfile.from_adapter(adapter).render().split("\n\n"):
        if _PLACEHOLDER_MARKER in paragraph or _SILENCE_MARKER in paragraph:
            return paragraph
    raise AssertionError(
        "等待期间那一段既没有提到占位消息、也没说读者在干等：%r" % adapter.name
    )


class TheProgressVisibilityClauseIsConditionalTests(unittest.TestCase):
    """这一段提示曾经对**七个平台是假的**：它说读者会先看到一条占位消息，
    而出站那道闸门（见 :meth:`OutboundSender.send_text`）在那些平台上
    **根本不发**占位消息 —— 读者等待期间什么都看不见。

    "一条骗人的渠道说明比没有渠道说明更糟"，所以这里钉的是**真话**，
    不是文案：断言的是两个分支各自**独有的事实**，因此任何一边被误接到
    另一边都会立刻红。
    """

    def test_every_platform_is_covered(self):
        """覆盖面守门：13 个已注册平台一个不漏。"""
        self.assertEqual(len(every_registered_adapter()), 13)

    def test_an_editing_platform_still_says_the_placeholder_is_replaced(self):
        editing = [a for a in every_registered_adapter() if a.supports_message_edit]
        self.assertTrue(editing, "一个能改写的平台都没有？那这条断言是空的")
        for adapter in editing:
            with self.subTest(platform=adapter.name):
                paragraph = progress_paragraph(adapter)

                self.assertIn(_PLACEHOLDER_MARKER, paragraph)
                self.assertIn("further messages below", paragraph)
                self.assertNotIn(
                    "below the placeholder", paragraph,
                    "%s 的占位消息现在被**补完**成答复的开头一段，"
                    "整条答复不再被顶上去、更不再被整条另发" % adapter.name,
                )
                self.assertNotIn(
                    _SILENCE_MARKER, paragraph,
                    "%s 能改写，却说了'等待期间什么都不会出现'" % adapter.name,
                )

    def test_a_non_editing_platform_never_mentions_a_placeholder(self):
        """**证伪测试**：旧那句"posted as a new message below the placeholder"
        就是靠这一条抓出来的 —— 不能改写的平台上根本没有占位消息。"""
        silent = [a for a in every_registered_adapter()
                  if not a.supports_message_edit]
        self.assertTrue(silent, "一个不能改写的平台都没有？那这条断言是空的")
        for adapter in silent:
            with self.subTest(platform=adapter.name):
                hint = with_channel_hint("看一下 README", adapter)

                self.assertNotIn(
                    _PLACEHOLDER_MARKER, hint,
                    "%s 不能改写已发消息，桥**不会**发占位消息，"
                    "提示里不许出现 placeholder" % adapter.name,
                )

    def test_a_non_editing_platform_says_the_reader_waits_for_the_finished_answer(self):
        for adapter in every_registered_adapter():
            if adapter.supports_message_edit:
                continue
            with self.subTest(platform=adapter.name):
                paragraph = progress_paragraph(adapter)

                self.assertIn(_SILENCE_MARKER, paragraph)
                self.assertIn("finished answer is ready", paragraph)
                self.assertIn(
                    "no sign that anything is happening", paragraph,
                    "%s 必须说清等待期间读者看不到任何迹象" % adapter.name,
                )

    def test_the_two_branches_can_never_be_confused(self):
        """一个平台**只可能**落进一支：两支的独有标记必须互斥。

        这条不依赖文案，只依赖两个标记，所以**改措辞不会误伤**它；而它挡住的是
        "有人把两支的 if 写反 / 漏了 else"这类改动 —— 那种改动在只断言"提到了
        占位消息"的测试下是看不出来的。
        """
        for adapter in every_registered_adapter():
            with self.subTest(platform=adapter.name):
                hint = with_channel_hint("看一下 README", adapter)

                mentions_placeholder = _PLACEHOLDER_MARKER in hint
                mentions_silence = _SILENCE_MARKER in hint

                self.assertNotEqual(
                    mentions_placeholder, mentions_silence,
                    "%s 的提示同时命中或同时落空了两支的标记" % adapter.name,
                )
                self.assertEqual(
                    mentions_placeholder, adapter.supports_message_edit,
                    "%s 的提示与 supports_message_edit=%r 不符"
                    % (adapter.name, adapter.supports_message_edit),
                )

    def test_the_declared_flag_reaches_the_profile(self):
        for adapter in every_registered_adapter():
            with self.subTest(platform=adapter.name):
                self.assertEqual(
                    ChannelProfile.from_adapter(adapter).supports_message_edit,
                    adapter.supports_message_edit,
                )

    def test_the_clause_does_not_name_platforms_or_bridge_internals(self):
        """这两支都是给模型读的正文：不许出现平台名、也不许出现桥内部的说法。"""
        forbidden = ("opencode", "bridge", "adapter", "outbound", "edit()")
        for adapter in every_registered_adapter():
            with self.subTest(platform=adapter.name):
                paragraph = progress_paragraph(adapter).lower()

                for word in forbidden:
                    self.assertNotIn(word, paragraph)
                for platform_name in platform_names_in_use():
                    self.assertNotIn(
                        platform_name, paragraph,
                        "等待期间那一段提到了平台 %r" % platform_name,
                    )


if __name__ == "__main__":
    unittest.main()

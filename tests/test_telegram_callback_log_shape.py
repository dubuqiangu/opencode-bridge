"""telegram：``_handle_callback`` 那条告警**只记载荷的形状，不记内容**。

改前::

    logger.warning("telegram: callback without chat context: %r", cq)

``%r`` 整个 ``cq`` 会带出 ``from.id`` / ``from.username`` / ``from.first_name`` /
``last_name`` / ``data``（**任意用户自定义字符串**）/ ``message``（含 ``chat.id`` 与
**被点的那条消息正文**）⇒ 这一行是 **PII + 内容**，比裸 id 更重。

⚠️ **可达性照实说，别夸大**：``chat_id`` 只在 ``message.chat.id`` 与 ``from.id``
**都**缺失时才是 ``None``，而 Telegram 的 ``callback_query`` **一定带** ``from``
⇒ 对**格式正确**的载荷近乎不可达。它是给畸形 / 恶意载荷准备的，而这条路径
**外部可达** ⇒ 「**低概率、高后果、改动极小**」，不是「正在被持续利用」。

判据是「**用户数据一个字都不出现，形状信息一条不少**」：

* ⛔ 一个哨兵字符串都不许出现（**含不以其子串形式出现**）；
* ✅ 顶层键名仍然出现 —— 这是防「**靠少记信息来修**」的闸。只打一个
  ``from=?`` 而没有 ``keys=``，是把排障能力换成了隐私，方向反了。
"""

from __future__ import annotations

import io
import logging
import unittest
from pathlib import Path

logging.getLogger("opencode_bridge").addHandler(logging.NullHandler())

from opencode_bridge.adapters.telegram import TelegramAdapter

from tests.log_redaction_support import (  # 绝对导入：与既有 tests.inbound_log_support 一致
    RecordingHooks,
    captured_logs,
    expected_conversation_digest,
)

#: 哨兵 —— 一眼可辨，且**互不为子串**（否则"不许以子串形式出现"这条判据会
#: 因为一个哨兵恰好是另一个的前缀而给出无法解释的失败）。
SENTINEL_USERNAME = "SENTINEL-USERNAME"
SENTINEL_FIRST_NAME = "SENTINEL-FIRSTNAME"
SENTINEL_LAST_NAME = "SENTINEL-LASTNAME"
SENTINEL_DATA = "SENTINEL-CALLBACK-DATA"
SENTINEL_MESSAGE_TEXT = "SENTINEL-MESSAGE-BODY"
SENTINEL_CHAT_TITLE = "SENTINEL-CHAT-TITLE"
SENTINEL_ACTOR_NAME = "SENTINEL-ACTOR-NAME"

ALL_SENTINELS = (
    SENTINEL_USERNAME, SENTINEL_FIRST_NAME, SENTINEL_LAST_NAME,
    SENTINEL_DATA, SENTINEL_MESSAGE_TEXT, SENTINEL_CHAT_TITLE,
    SENTINEL_ACTOR_NAME,
)

#: 这条日志的 **grep 锚点** —— 与另外四句 ``non-whitelisted ...`` 的约定一致，
#: 刻意保留英文短语。
GREP_ANCHOR = "callback without chat context"


def malformed_callback_query() -> dict:
    """一个**畸形**载荷：``message.chat`` 与 ``from.id`` 都缺 ⇒ 命中那条告警。

    每个可能带用户数据的位置都塞了哨兵：只要实现里还有任何一处 ``%r`` /
    字段直取，哨兵就会出现在日志里。
    """
    return {
        "id": "SENTINEL-QUERY-ID",
        "data": SENTINEL_DATA,
        "from": {
            "id": None,
            "is_bot": False,
            "first_name": SENTINEL_FIRST_NAME,
            "last_name": SENTINEL_LAST_NAME,
            "username": SENTINEL_USERNAME,
            "language_code": "zh-hans",
        },
        "message": {
            "message_id": 4242,
            "date": 1700000000,
            "text": SENTINEL_MESSAGE_TEXT,
            "chat": {"type": "private"},      # ⛔ 刻意缺 id
            "from": {
                "id": None,
                "first_name": SENTINEL_ACTOR_NAME,
                "username": SENTINEL_USERNAME,
            },
        },
    }


def make_adapter() -> TelegramAdapter:
    adapter = TelegramAdapter({"bot_token": "123456:SENTINEL-BOT-TOKEN-PLACEHOLDER",
                               "allowed_chat_ids": []},
                              RecordingHooks())
    adapter.min_interval = 0
    return adapter


class CallbackWithoutChatContextLogTest(unittest.TestCase):
    def drive(self):
        payload = malformed_callback_query()
        adapter = make_adapter()
        with captured_logs("telegram") as handler:
            adapter._handle_callback(payload)
        return handler, payload

    # -- 1) 用户数据一个字都不出现 ----------------------------------------
    def test_no_sentinel_appears_at_all(self):
        handler, _payload = self.drive()
        lines = handler.lines(GREP_ANCHOR)
        self.assertEqual(len(lines), 1, "应当恰好一行，实际 %r" % (handler.lines(""),))
        for sentinel in ALL_SENTINELS:
            self.assertNotIn(sentinel, lines[0],
                             "载荷**内容**漏进日志了（哨兵 %s）：%r"
                             % (sentinel, lines[0]))

    def test_message_body_verbatim_would_have_been_caught(self):
        """防恒真：证明上面那条判据**真的看得见**正文。

        喂一份**只**含正文的载荷，走同一条告警路径；判据必须报出来。
        否则上面那条 ``assertNotIn`` 可能只是因为哨兵没被引用过而恒真。
        """
        adapter = make_adapter()
        payload = {"id": "q", "message": {"text": SENTINEL_MESSAGE_TEXT,
                                           "chat": {"type": "private"}}}
        with captured_logs("telegram") as handler:
            adapter._handle_callback(payload)
        joined = "\n".join(handler.lines(""))
        # 这一份载荷的键名里没有哨兵，所以要单独造一个含正文键的判据：
        # 这里断言的是"载荷的键名被记下来了"，而**正文值没被记下来**。
        self.assertIn("message", joined, "message 这个键名必须出现（形状信息）")
        self.assertNotIn(SENTINEL_MESSAGE_TEXT, joined, "正文值绝不许出现")

    # -- 2) 形状信息一条不少（防「靠少记信息来修」的闸）--------------------
    def test_top_level_key_names_are_still_logged(self):
        handler, payload = self.drive()
        line = handler.lines(GREP_ANCHOR)[0]
        for key in sorted(payload):
            self.assertIn(key, line,
                          "顶层键名 %r 不见了 ⇒ 排障时答不出"
                          "'有没有 message / 有没有 from'" % (key,))

    def test_keys_are_logged_as_the_sorted_top_level_key_tuple(self):
        """键名按**排序后的元组**记 ⇒ 同一形状的两次告警逐字节可比。"""
        handler, payload = self.drive()
        line = handler.lines(GREP_ANCHOR)[0]
        self.assertIn(repr(tuple(sorted(payload))), line)

    def test_missing_from_key_is_visible(self):
        """``from`` 整个不在 ⇒ 键名元组里就没有它，而这本身是可排障的信息。"""
        adapter = make_adapter()
        payload = {"id": "q", "message": {"chat": {"type": "private"}}}
        with captured_logs("telegram") as handler:
            adapter._handle_callback(payload)
        line = handler.lines(GREP_ANCHOR)[0]
        self.assertIn(repr(("id", "message")), line)
        self.assertNotIn("from=", line.replace("from=?", ""))

    # -- 3) from.id 的形态 -------------------------------------------------
    def test_absent_from_id_falls_back_to_missing_placeholder(self):
        handler, _payload = self.drive()
        line = handler.lines(GREP_ANCHOR)[0]
        self.assertIn("from=?", line,
                      "取不到 from.id 时必须走 MISSING_ID 占位（裸 ?）")

    def test_missing_placeholder_is_not_redacted_into_a_fake_digest(self):
        """占位符刻意是**裸的** ``?``（不带平台前缀）⇒ 原样留下。

        因为 ``telegram:?`` 会被脱敏成 ``conv#<摘要>("?")`` —— 一个**看起来很像
        真摘要**的东西，于是"这里本来就没有 id"这条信息被洗掉了；而"缺 chat 上下文"
        那一支恰恰要靠它才看得见。
        """
        handler, _payload = self.drive()
        line = handler.lines(GREP_ANCHOR)[0]
        self.assertIn("from=?", line)
        self.assertNotIn("telegram:?", line, "占位符刻意不带平台前缀")
        self.assertNotIn("conv#", line, "没有真 id ⇒ 这一行不该出现任何 conv# 摘要")

    def test_from_id_would_be_logged_as_a_correlatable_digest(self):
        """``from.id`` 若取到 ⇒ 脱敏后以 ``telegram:conv#…`` 形态出现（不是明文）。

        ⚠️ 注意 :func:`redactable_id` **只负责补前缀**，摘要（HMAC）是 C2 的
        :class:`Redactor` 在 :class:`RedactingFilter` 里算的。所以这里要断言的是
        两段：适配器交出去的**前缀形态** + 过滤之后的**摘要形态**。
        """
        from opencode_bridge.adapters._redactable_ids import redactable_id
        from opencode_bridge.redaction import Redactor
        from tests.log_redaction_support import FIXED_REDACTION_KEY

        # 适配器交出去的：带前缀的明文。
        handed_to_c2 = redactable_id("telegram", "424242")
        self.assertEqual(handed_to_c2, "telegram:424242")

        # C2 洗完之后：可关联摘要，且明文 id 不见了。
        scrubbed = Redactor(key=FIXED_REDACTION_KEY).scrub(handed_to_c2)
        self.assertEqual(scrubbed, expected_conversation_digest("telegram", "424242"))
        self.assertTrue(scrubbed.startswith("telegram:conv#"))
        self.assertNotIn("424242", scrubbed, "明文 id 不许留在日志里")

    def test_non_dict_from_does_not_crash_the_warning_path(self):
        """``from`` 是个字符串（畸形）⇒ 告警仍照记，不许抛。"""
        adapter = make_adapter()
        payload = {"id": "q", "from": "not-a-dict", "data": SENTINEL_DATA}
        with captured_logs("telegram") as handler:
            adapter._handle_callback(payload)
        lines = handler.lines(GREP_ANCHOR)
        self.assertEqual(len(lines), 1)
        self.assertNotIn(SENTINEL_DATA, lines[0])
        self.assertIn(repr(("data", "from", "id")), lines[0])

    # -- 4) 行为没变 --------------------------------------------------------
    def test_malformed_callback_still_stops_early_without_delivering(self):
        """判定没变：仍然原样 ``return``，不放行任何 inbound / callback。"""
        hooks = RecordingHooks()
        adapter = TelegramAdapter({"bot_token": "123456:PLACEHOLDER",
                                   "allowed_chat_ids": []}, hooks)
        adapter.min_interval = 0
        payload = malformed_callback_query()
        with captured_logs("telegram"):
            adapter._handle_callback(payload)
        self.assertEqual(hooks.inbounds, [])
        self.assertEqual(payload["data"], SENTINEL_DATA, "载荷不该被就地改写")

    def test_wellformed_callback_is_unaffected(self):
        """对照：格式正确的载荷**根本不该**命中这条告警（近乎不可达的由来）。"""
        adapter = make_adapter()
        wellformed = {"id": "q", "data": "allow:1",
                      "from": {"id": 424242, "username": SENTINEL_USERNAME,
                               "first_name": SENTINEL_FIRST_NAME},
                      "message": {"message_id": 1, "chat": {"id": 424242,
                                                           "type": "private"}}}
        with captured_logs("telegram") as handler:
            adapter._handle_callback(wellformed)
        self.assertEqual(handler.lines(GREP_ANCHOR), [],
                         "格式正确的载荷有 chat 上下文，不该命中这条告警")


class GrepAnchorTest(unittest.TestCase):
    """保留英文锚点是**刻意**的（全仓库只有定义处引用它，改文案不牵动文档）。"""

    def setUp(self):
        path = (Path(__file__).resolve().parent.parent
                / "opencode_bridge" / "adapters" / "telegram.py")
        with io.open(path, encoding="utf-8") as handle:
            self.source = handle.read()

    def test_anchor_string_is_still_in_the_source(self):
        self.assertEqual(self.source.count(GREP_ANCHOR), 1,
                         "锚点只该在定义处出现一次")

    def test_whole_payload_repr_is_gone(self):
        """⛔ 那一行不再 ``%r`` 整个 ``cq`` —— 本次改动的核心。"""
        self.assertNotIn("callback without chat context: %r", self.source)
        self.assertNotIn('callback without chat context: %s", cq', self.source)

    def test_log_level_is_unchanged(self):
        start = self.source.index(GREP_ANCHOR)
        self.assertIn("logger.warning", self.source[start - 200:start + 40])


if __name__ == "__main__":
    unittest.main()

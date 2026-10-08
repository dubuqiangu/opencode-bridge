"""telegram：``_on_update`` 异常那行**只记定位字段，不记载荷**（2026-10-08 修复）。

改之前::

    logger.exception("telegram: failed to handle update %r", update)

``%r`` 整个 update ⇒ 用户私聊正文（``message.text``）/ ``username`` /
``first_name`` / ``chat.title`` 逐字进 **ERROR 级**日志，而这条路径**不是冷路径**
（``_advance_offset`` + ``_dispatch_update`` 任何一处抛异常都走这里，且每次都
带一条 update）。§2.2 把「用户消息正文」列为敏感项。

判据与 ``tests/test_telegram_callback_log_shape.py`` 同构
（「用户数据一个字都不出现（含子串），形状信息一条不少」）：

* ⛔ 一个哨兵字符串都不许出现（含不以其子串形式出现）；
* ✅ 定位字段一条不少：``update_id=`` · ``chat=``（脱敏后可关联）· ``keys=``
  （排序后的顶层键名元组）—— 这是防「靠少记信息来修」的闸；
* ✅ 级别仍是 ERROR、traceback 仍在 —— 只改「记什么」，不许把排障信息弄掉。

触发方式照抄 ``tests/test_telegram.py`` 的既有先例（``_dispatch_update = boom``）：
被测的是 ``_on_update`` + ``_update_locator``，``_dispatch_update`` 是协作者，
把它换成「必抛」的替身是进入 except 分支的最短路径。
"""

from __future__ import annotations

import logging
import unittest

logging.getLogger("opencode_bridge").addHandler(logging.NullHandler())

from opencode_bridge.adapters.telegram import TelegramAdapter

from tests.log_redaction_support import (  # 绝对导入：与 callback_log_shape 一致
    RecordingHooks,
    captured_logs,
    expected_conversation_digest,
)

#: 哨兵 —— 互不为子串（与 callback_log_shape 同一条纪律）。
SENTINEL_MESSAGE_TEXT = "SENTINEL-MESSAGE-BODY"
SENTINEL_USERNAME = "SENTINEL-USERNAME"
SENTINEL_FIRST_NAME = "SENTINEL-FIRSTNAME"
SENTINEL_CHAT_TITLE = "SENTINEL-CHAT-TITLE"

ALL_SENTINELS = (
    SENTINEL_MESSAGE_TEXT, SENTINEL_USERNAME,
    SENTINEL_FIRST_NAME, SENTINEL_CHAT_TITLE,
)

#: 这条日志的 grep 锚点 —— 与 callback 那条 ``callback without chat context``
#: 的约定一致，刻意保留英文短语。
GREP_ANCHOR = "failed to handle update"

#: 与哨兵、update_id 都不冲突的 chat id —— 「明文 id 不许留在日志里」
#: 这条断言不能被别的数字误触发。
UPDATE_ID = 9001
CHAT_ID = 4242


def message_update_with_sentinels() -> dict:
    """一个**每个可能带用户数据的位置都塞了哨兵**的正常形状 update。"""
    return {
        "update_id": UPDATE_ID,
        "message": {
            "message_id": 77,
            "text": SENTINEL_MESSAGE_TEXT,
            "chat": {"id": CHAT_ID, "type": "private",
                     "title": SENTINEL_CHAT_TITLE},
            "from": {"id": 7, "is_bot": False,
                     "first_name": SENTINEL_FIRST_NAME,
                     "username": SENTINEL_USERNAME},
        },
    }


def make_adapter() -> TelegramAdapter:
    adapter = TelegramAdapter({"bot_token": "123:FAKE",
                               "allowed_chat_ids": []},
                              RecordingHooks())
    adapter.min_interval = 0
    return adapter


def dispatch_that_always_raises(update: dict) -> None:
    """协作者替身：必抛，从而进入 ``_on_update`` 的 except 分支。"""
    raise RuntimeError("dispatch exploded")


def drive(update: dict):
    adapter = make_adapter()
    adapter._dispatch_update = dispatch_that_always_raises
    with captured_logs("telegram") as handler:
        adapter._on_update(update)
    matching = [r for r in handler.records if GREP_ANCHOR in r.message]
    return handler, matching


class UpdateLogShapeTest(unittest.TestCase):
    # -- 1) 用户数据一个字都不出现 ----------------------------------------
    def test_no_sentinel_appears_at_all(self):
        _handler, matching = drive(message_update_with_sentinels())
        self.assertEqual(len(matching), 1,
                         "应当恰好一行，实际 %r" % ([r.message for r in matching],))
        for sentinel in ALL_SENTINELS:
            self.assertNotIn(sentinel, matching[0].message,
                             "载荷**内容**漏进日志了（哨兵 %s）：%.300r"
                             % (sentinel, matching[0].message))

    # -- 2) 形状信息一条不少（防「靠少记信息来修」的闸）--------------------
    def test_update_id_is_logged(self):
        _handler, matching = drive(message_update_with_sentinels())
        self.assertIn("update_id=%s" % UPDATE_ID, matching[0].message)

    def test_chat_id_is_logged_as_a_correlatable_digest_not_plaintext(self):
        """``chat=`` 交出去的是带前缀的 id，C2 洗完是可关联摘要；明文不许留。"""
        _handler, matching = drive(message_update_with_sentinels())
        self.assertIn("chat=" + expected_conversation_digest("telegram",
                                                             str(CHAT_ID)),
                      matching[0].message)
        self.assertNotIn(str(CHAT_ID), matching[0].message.replace(
            "update_id=%s" % UPDATE_ID, ""), "明文 chat id 不许留在日志里")

    def test_keys_are_logged_as_the_sorted_top_level_key_tuple(self):
        update = message_update_with_sentinels()
        _handler, matching = drive(update)
        self.assertIn(repr(tuple(sorted(update))), matching[0].message,
                      "顶层键名元组不见了 ⇒ 排障时答不出'有没有 message'")

    # -- 3) 级别与 traceback 没被这次改动碰掉 ------------------------------
    def test_level_is_still_error_and_traceback_survives(self):
        _handler, matching = drive(message_update_with_sentinels())
        self.assertEqual(matching[0].levelname, "ERROR")
        self.assertIn("dispatch exploded", matching[0].traceback,
                      "traceback 是排障要的东西，只改「记什么」不许弄掉它")

    # -- 4) locator 的两条备选取值路径 -------------------------------------
    def test_callback_update_locates_chat_through_the_nested_message(self):
        """无顶层 ``message``、有 ``callback_query.message.chat`` ⇒ 仍能定位。"""
        update = {"update_id": UPDATE_ID,
                  "callback_query": {"id": "Q9", "data": "act:x",
                                     "message": {"chat": {"id": CHAT_ID}}}}
        _handler, matching = drive(update)
        self.assertEqual(len(matching), 1)
        self.assertIn("chat=" + expected_conversation_digest("telegram",
                                                             str(CHAT_ID)),
                      matching[0].message)

    def test_update_without_any_chat_falls_back_to_the_missing_placeholder(self):
        """连 ``callback_query`` 都没有 ⇒ ``chat=?``（裸占位，⛔ 不许被脱敏成假摘要）。"""
        update = {"update_id": UPDATE_ID, "edited_message": {"text": "x"}}
        _handler, matching = drive(update)
        self.assertEqual(len(matching), 1)
        self.assertIn("chat=?", matching[0].message)
        self.assertNotIn("conv#", matching[0].message,
                         "没有真 id ⇒ 这一行不该出现任何 conv# 摘要")


if __name__ == "__main__":
    unittest.main()

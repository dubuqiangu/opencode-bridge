"""telegram：无 ``message`` 键的 update 必须留一条 INFO 说明忽略了哪一类（2026-10-09 修复）。

改之前 ``_dispatch_update`` 对 ``channel_post`` / ``edited_message`` /
``edited_channel_post`` 这类**没有 ``message`` 键**的形状是纯 fall-through：
投递 **0** 条 + 日志 **0** 条 ⇒ 与「没收到」在日志上**完全同形**（2026-10-08
telegram 审计，第 1 类已确认缺陷：**零投递是设计、零解释不是**）。

修复 = fall-through 那里补一条 ``logger.info``，**只记形状**（排序后的顶层键名，
``update_id`` 除外）—— 与 ``_update_locator`` 同一条纪律，⛔ 绝不记载荷。

本文件是那条台账行点名要的守门，同时钉住两个方向：

* ⛔ 不许静默：``channel_post`` / ``edited_message`` 进来 ⇒ 至少一条 INFO
  说明忽略了哪一类，且**投递仍然 0 条**（不接入站 = 频道每条发言都触发
  agent，台账已否决接入）；
* ⛔ 不许记载荷：正文一个字都不许进日志（哨兵断言）；
* ⛔ 不许误伤：正常 ``message`` 形状 ⇒ 投递 1 条 + **零**条该 INFO（对照臂）；
  带贴纸/照片但无文本的 ``message``（走的是后面的 text 检查，不是这条
  fall-through）⇒ 也**零**条该 INFO。

三组臂各只差「update 里的键名 / 有无 text」一个变量 ⇒ 判据有辨别力。
"""

from __future__ import annotations

import logging
import unittest

logging.getLogger("opencode_bridge").addHandler(logging.NullHandler())

from opencode_bridge.adapters.telegram import TelegramAdapter

from tests.log_redaction_support import (  # 绝对导入：与 update_log_shape 一致
    RecordingHooks,
    captured_logs,
)

#: 这条 INFO 的 grep 锚点 —— 刻意保留英文短语，与 ``_update_locator`` 的
#: ``keys=`` 记法同族。
GREP_ANCHOR = "ignoring update without message key"

#: 哨兵正文 —— 塞进载荷的每个可能位置，断言它**一个字都不进日志**。
SENTINEL_BODY = "SENTINEL-CHANNEL-BODY"


def make_adapter() -> TelegramAdapter:
    return TelegramAdapter({"bot_token": "123:FAKE"}, RecordingHooks())


def dispatch_and_collect(update: dict):
    """跑一次 ``_dispatch_update``，返回 ``(投递列表, 含锚点的日志记录)``。"""
    adapter = make_adapter()
    with captured_logs("telegram") as handler:
        adapter._dispatch_update(update)
    matching = [r for r in handler.records if GREP_ANCHOR in r.message]
    return adapter.hooks.inbounds, matching


def channel_post_update() -> dict:
    return {
        "update_id": 41,
        "channel_post": {
            "message_id": 4,
            "chat": {"id": 55},
            "text": SENTINEL_BODY,
        },
    }


def edited_message_update() -> dict:
    return {
        "update_id": 42,
        "edited_message": {
            "message_id": 3,
            "chat": {"id": 55},
            "text": SENTINEL_BODY,
        },
    }


def plain_message_update() -> dict:
    return {
        "update_id": 43,
        "message": {
            "message_id": 9,
            "chat": {"id": 55, "type": "private"},
            "from": {"id": 7, "is_bot": False, "first_name": "u"},
            "text": SENTINEL_BODY,
        },
    }


def sticker_message_update() -> dict:
    """带贴纸、无文本的**正常** ``message`` 形状 —— 它走的是后面的 text 检查，
    不是这条 fall-through ⇒ 不该出现这条 INFO。"""
    return {
        "update_id": 44,
        "message": {
            "message_id": 10,
            "chat": {"id": 55},
            "sticker": {},
        },
    }


class NonMessageShapeInfoLogGuardTest(unittest.TestCase):
    def test_channel_post_is_dropped_with_exactly_one_info_naming_the_shape(self):
        """channel_post ⇒ 投递 0 条 + 恰好 1 条 INFO 点名 ``channel_post``。"""
        inbounds, matching = dispatch_and_collect(channel_post_update())
        self.assertEqual(inbounds, [], "零投递是设计 —— 不许把它们接进入站")
        self.assertEqual(len(matching), 1,
                         "应当恰好一行，实际 %r" % [r.message for r in matching])
        self.assertEqual(matching[0].levelname, "INFO")
        self.assertIn("channel_post", matching[0].message,
                      "必须说明忽略了哪一类，否则排障时对不上形状")

    def test_edited_message_is_dropped_with_exactly_one_info_naming_the_shape(self):
        """edited_message ⇒ 投递 0 条 + 恰好 1 条 INFO 点名 ``edited_message``。"""
        inbounds, matching = dispatch_and_collect(edited_message_update())
        self.assertEqual(inbounds, [])
        self.assertEqual(len(matching), 1,
                         "应当恰好一行，实际 %r" % [r.message for r in matching])
        self.assertIn("edited_message", matching[0].message)

    def test_payload_never_leaks_into_the_info_line(self):
        """⛔ 哨兵正文一个字都不许进这条 INFO —— 只记形状，不记载荷。"""
        _inbounds, matching = dispatch_and_collect(channel_post_update())
        self.assertEqual(len(matching), 1)
        self.assertNotIn(SENTINEL_BODY, matching[0].message,
                         "载荷**内容**漏进日志了（哨兵 %s）：%.300r"
                         % (SENTINEL_BODY, matching[0].message))

    def test_plain_message_is_delivered_and_does_not_log_the_ignore_line(self):
        """对照臂：正常 ``message`` ⇒ 投递 1 条 + 零条该 INFO（不许误伤主路径）。"""
        inbounds, matching = dispatch_and_collect(plain_message_update())
        self.assertEqual(len(inbounds), 1)
        self.assertEqual(matching, [])

    def test_sticker_message_does_not_log_the_ignore_line_either(self):
        """贴纸形状带 ``message`` 键 ⇒ 走 text 检查，不是这条 fall-through
        ⇒ 不该出现这条 INFO（钉住锚点落在**正确的那条分支**上）。"""
        inbounds, matching = dispatch_and_collect(sticker_message_update())
        self.assertEqual(inbounds, [])
        self.assertEqual(matching, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

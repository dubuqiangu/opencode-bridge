"""telegram：``answerCallbackQuery`` 被拒收（``ok:false``）必须留下 WARNING（2026-10-09 修复）。

改之前 ``TelegramAdapter.answer`` 抓到 ``ok is not True`` 只打一条 ``logger.debug``
（``telegram: answerCallbackQuery not ok: %s``）⇒ 默认档位下**零日志**；而转圈
**只有** ``answerCallbackQuery`` 能停掉 ⇒ 用户看到的是「点了没反应、也永远转下去」
（2026-10-08 telegram 审计，第 1 类已确认缺陷）。

本文件是那条台账行点名要的**行为型守门**（⛔ 只改档位不加守门等于没改）：

* ok:false ⇒ **WARNING 及以上 ≥ 1 条**，且点名 ``query_id`` 与服务端 ``description``；
* ok:true（对照臂）⇒ 0 条 —— 钉住「不是每条应答都刷告警」；
* 传输层抛异常（对照臂）⇒ 1 条 ERROR —— 既有行为，不许被这次改动碰掉。

三臂只差 ``_post`` 的应答一个变量 ⇒ 判据有辨别力（2026-10-08 审计已实测两臂：
ok:false ⇒ WARNING 及以上 0 条、抛异常 ⇒ 1 条；本文件把修复后的两臂钉进树里）。
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

#: 这条 WARNING 的 grep 锚点 —— 与既有 ``answerCallbackQuery failed``（异常路）
#: 的约定一致，刻意保留英文短语。
GREP_ANCHOR = "answerCallbackQuery not ok"

#: 被点名的 query id —— 走的是与 ``answer failed for query %s`` 同一条记法
#: （Telegram 派生的不透明 id，不是用户内容）。
QUERY_ID = "Q-7031"

#: 服务端拒收时给的 description —— 断言它逐字进日志（排障要的就是它）。
REJECTION_DESCRIPTION = "query is too old and response timeout expired"


def make_adapter() -> TelegramAdapter:
    return TelegramAdapter({"bot_token": "123:FAKE"}, RecordingHooks())


def answer_with_post_returning(post_result) -> list:
    """把 ``_post`` 换成固定应答，跑一次 ``answer``，返回脱敏后的日志记录。"""
    adapter = make_adapter()

    def fake_post(method, payload=None, *, timeout=None):
        return post_result

    adapter._post = fake_post
    with captured_logs("telegram") as handler:
        adapter.answer(QUERY_ID)
    return [r for r in handler.records if GREP_ANCHOR in r.message]


def answer_with_post_raising() -> list:
    """对照臂：传输层抛异常 ⇒ 走 ``logger.exception`` 那条既有路径。"""
    adapter = make_adapter()

    def fake_post(method, payload=None, *, timeout=None):
        raise RuntimeError("transport exploded")

    adapter._post = fake_post
    with captured_logs("telegram") as handler:
        adapter.answer(QUERY_ID)  # 契约：never raises
    return [r for r in handler.records if "answerCallbackQuery failed" in r.message]


class AnswerNotOkWarningGuardTest(unittest.TestCase):
    def test_not_ok_answer_logs_exactly_one_warning_naming_query_and_description(self):
        """ok:false ⇒ 恰好 1 条 WARNING 及以上，点名 ``query_id`` 与 ``description``。"""
        matching = answer_with_post_returning(
            {"ok": False, "description": REJECTION_DESCRIPTION}
        )
        self.assertEqual(len(matching), 1,
                         "应当恰好一行，实际 %r" % [r.message for r in matching])
        record = matching[0]
        self.assertIn(
            record.levelname, ("WARNING", "ERROR", "CRITICAL"),
            "改之前这里是 debug（默认档位零日志）；修复后必须 WARNING 及以上，"
            "实际 %s" % record.levelname)
        self.assertIn(QUERY_ID, record.message,
                      "必须点名 query_id —— 否则排障时对不上是哪个按钮")
        self.assertIn(REJECTION_DESCRIPTION, record.message,
                      "服务端的拒收原因必须逐字进日志")

    def test_ok_answer_stays_silent(self):
        """对照臂：ok:true ⇒ 0 条 —— 不是每条应答都刷告警。"""
        matching = answer_with_post_returning({"ok": True, "result": {}})
        self.assertEqual(matching, [])


    def test_transport_exception_still_logs_error_and_never_raises(self):
        """对照臂：抛异常 ⇒ 1 条 ERROR（既有行为不许被碰掉），且 ``answer`` 不抛。"""
        matching = answer_with_post_raising()
        self.assertEqual(len(matching), 1,
                         "异常臂应恰好一条 ERROR，实际 %r" % [r.message for r in matching])
        self.assertEqual(matching[0].levelname, "ERROR")
        self.assertIn("answerCallbackQuery failed", matching[0].message)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

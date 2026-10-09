"""telegram：部分发送必须给读者**缺口标记**（2026-10-09 修复）。

改之前 ``send()`` 多段发送中途失败：前段留着不回滚、后段失败后返回**最后一个
成功段的 handle**（2026-10-08 telegram 审计：「部分失败无回滚、无中止、无重发 ⇒
读者拿到的是静默截断的答复」）—— 读者**没有任何办法知道**答复不完整，
只有日志与 ``--status`` 知道。

处置（用户 2026-10-09 拍板「加缺口标记」）：部分失败（前段已上线、后段失败）
时追加一条缺口标记消息（:data:`SEND_GAP_MARKER_TEXT`），让读者知道后续缺失；
标记本身失败只留 WARNING —— 缺口可见性退回由日志与 ``--status`` 承担。

本文件是那条台账行点名要的**行为型守门**（⛔ 只改行为不加守门等于没改）：

* 第 2 段失败 ⇒ 读者收到 [前段, 缺口标记]，标记文本与常量**逐字相同**；
* 全成功（对照臂）⇒ 只有正文段、零标记 —— 钉住「不是每次发送都追加标记」；
* 标记也失败 ⇒ 仍返回最后成功段的 handle + 恰好 1 条点名「缺口标记」的告警；
* 第 1 段就失败 ⇒ 返回 ``None``、零标记（一段都没上线 ⇒ 不存在缺口）。

四臂只差「哪一段失败 / 标记是否成功」⇒ 判据有辨别力（审计实测两臂只差
「第 2 段」那一次失败：读者只收到 ``["HEAD"]``、对照臂是 ``["HEAD","TAIL"]``）。

⚠️ 本文件不联网：全部用替换 ``_post`` 的办法（与 ``test_adapters.py`` 一致）。
"""

from __future__ import annotations

import logging
import unittest

logging.getLogger("opencode_bridge").addHandler(logging.NullHandler())

from opencode_bridge.adapters.telegram import (
    SEND_GAP_MARKER_TEXT,
    TelegramAdapter,
)
from opencode_bridge.hooks import Outbound

from tests.log_redaction_support import (
    RecordingHooks,
    captured_logs,
)


def make_adapter() -> TelegramAdapter:
    adapter = TelegramAdapter({"bot_token": "123:FAKE"}, RecordingHooks())
    adapter.min_interval = 0  # 测试里不做发送间隔等待
    # 运行期细化槽（``effective_max_length`` 的真源，base.py：非 0 即胜出）：
    # 5 码点一段 ⇒ "AAAAABBBB"（9 码点）恰好切成 ["AAAAA", "BBBB"] 两段。
    adapter.message_limit = 5
    return adapter


def send_with_script(adapter: TelegramAdapter, responses: list) -> tuple:
    """按脚本消费应答，返回 (返回值, 实际出站的 payload 文本列表)。"""
    sent_texts: list[str] = []
    remaining = list(responses)

    def fake_post(method, payload=None, *, timeout=None):
        sent_texts.append((payload or {}).get("text"))
        if not remaining:
            # 脚本耗尽：⛔ 不用 assert（会被 ``_api`` 的 ``except Exception``
            # 吞成一次普通传输失败）—— 多发的请求由各臂的 sent_texts
            # **精确列表断言**抓住，那里会真正变红。
            return {"ok": False, "error_code": 0, "description": "script exhausted"}
        return remaining.pop(0)

    adapter._post = fake_post
    result = adapter.send(Outbound("chat:55", "AAAAABBBB"))
    return result, sent_texts


def gap_marker_warnings(handler) -> list[str]:
    """点名「缺口标记」的 WARNING 及以上（按文案取，不按顺序）。"""
    return [
        record.message for record in handler.records
        if record.levelname in ("WARNING", "ERROR", "CRITICAL")
        and "缺口标记" in record.message
    ]


class TestPartialSendGapMarker(unittest.TestCase):
    def test_second_chunk_failure_appends_gap_marker(self):
        """⭐ 缺陷本体：第 2 段失败 ⇒ 读者看到 [前段, 缺口标记]。

        改之前这里 sent_texts 是 ["AAAAA"]——后段失败后静默结束，读者不知道
        答复不完整。
        """
        ok_first = {"ok": True, "result": {"message_id": 1}}
        fail = {"ok": False, "error_code": 400, "description": "Bad Request: no"}
        ok_marker = {"ok": True, "result": {"message_id": 2}}
        with captured_logs("telegram") as logs:
            result, sent_texts = send_with_script(
                make_adapter(), [ok_first, fail, ok_marker]
            )
        self.assertEqual(result.message_id, "1")  # 仍返回最后成功段（契约未动）
        self.assertEqual(sent_texts, ["AAAAA", "BBBB", SEND_GAP_MARKER_TEXT])
        # 标记成功 ⇒ 不许有「标记也失败」的告警
        self.assertEqual(gap_marker_warnings(logs), [])

    def test_full_success_sends_no_marker(self):
        """对照臂：全成功 ⇒ 只有正文段、零标记（不是每次发送都追加）。"""
        ok_first = {"ok": True, "result": {"message_id": 1}}
        ok_second = {"ok": True, "result": {"message_id": 2}}
        with captured_logs("telegram") as logs:
            result, sent_texts = send_with_script(
                make_adapter(), [ok_first, ok_second]
            )
        self.assertEqual(result.message_id, "2")
        self.assertEqual(sent_texts, ["AAAAA", "BBBB"])
        self.assertEqual(gap_marker_warnings(logs), [])

    def test_marker_failure_keeps_handle_and_warns(self):
        """标记也失败 ⇒ 仍返回最后成功段的 handle + 恰好 1 条点名告警。"""
        ok_first = {"ok": True, "result": {"message_id": 1}}
        fail = {"ok": False, "error_code": 400, "description": "Bad Request: no"}
        with captured_logs("telegram") as logs:
            result, sent_texts = send_with_script(
                make_adapter(), [ok_first, fail, fail]
            )
        self.assertEqual(result.message_id, "1")
        self.assertEqual(sent_texts, ["AAAAA", "BBBB", SEND_GAP_MARKER_TEXT])
        warnings = gap_marker_warnings(logs)
        self.assertEqual(len(warnings), 1, f"该恰好 1 条。实际：{warnings!r}")

    def test_first_chunk_failure_returns_none_without_marker(self):
        """第 1 段就失败 ⇒ 返回 None、零标记（一段都没上线 ⇒ 没有缺口）。"""
        fail = {"ok": False, "error_code": 400, "description": "Bad Request: no"}
        with captured_logs("telegram") as logs:
            result, sent_texts = send_with_script(make_adapter(), [fail])
        self.assertIsNone(result)
        self.assertNotIn(SEND_GAP_MARKER_TEXT, sent_texts)
        self.assertEqual(gap_marker_warnings(logs), [])


if __name__ == "__main__":
    unittest.main()

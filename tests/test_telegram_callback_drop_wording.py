"""telegram：按钮点击被白名单丢弃的那句日志文案是 **grep 锚点**，逐字钉住。

台账「``dropped callback from non-whitelisted chat`` 零钉」行（覆盖面缺口，
**不是行为缺陷** —— 行为已由 :mod:`tests.test_inbound_log_per_platform`
的 callback 分支钉住）：

* message 版文案有 needle 钉着（源码 + README 排障表，
  :mod:`tests.test_inbound_log_structure`）；
* 而 callback 版一句都没钉 ⇒ 它可**无声漂移**（有人"顺手改得更好读"、
  或把两句"统一措辞"时，用户照旧文档搜不到）。

本文件补上三层：

1. **渲染层逐字** —— 闸门翻转臂下，落盘那一行真的含锚点（钉在渲染 +
   脱敏之后，⛔ 不是只钉源码）；
2. **对照臂**（防恒真）—— 白名单内的 chat **不**打这句；且 inbound
   真的放行了 1 条，证明对照臂走的是"过闸"那条路，不是某个早退；
3. **源码锚点恰一次** —— 有人统一 / 改写文案时这里先红。
"""

from __future__ import annotations

import io
import unittest
from pathlib import Path

from opencode_bridge.adapters.telegram import TelegramAdapter

from tests.inbound_log_support import (
    FLIPPED_GATE_CONFIG,
    CapturedLogs,
    RecordingHooks,
)

#: 这条日志的 **grep 锚点** —— 与 message 版
#: ``dropped message from non-whitelisted chat`` 同一条约定：用户照它搜日志。
#: 刻意保留英文短语、不含 ``telegram:`` 前缀（前缀在 message 版 needle 里也没钉）。
GREP_ANCHOR = "dropped callback from non-whitelisted chat"

TELEGRAM_SOURCE_PATH = (
    Path(__file__).resolve().parent.parent
    / "opencode_bridge" / "adapters" / "telegram.py"
)

WELLFORMED_CALLBACK = {
    "id": "cb-wording-1",
    "data": "approve:x",
    "from": {"id": 7, "is_bot": False},
    "message": {"message_id": 9,
                "chat": {"id": 424242, "type": "private"}},
}


def make_adapter(allowed_chat_ids: list) -> tuple[TelegramAdapter, RecordingHooks]:
    hooks = RecordingHooks()
    adapter = TelegramAdapter(
        {"bot_token": "123456:SENTINEL-PLACEHOLDER",
         "allowed_chat_ids": allowed_chat_ids,
         "config_version": 2},
        hooks,
    )
    return adapter, hooks


class DroppedCallbackWordingTests(unittest.TestCase):
    """非白名单 chat 的按钮点击 ⇒ 落盘那一行逐字含锚点。"""

    def test_drop_line_is_logged_verbatim_after_redaction(self):
        adapter, hooks = make_adapter([])          # config_version 2 ⇒ 空 = 全拒
        with CapturedLogs("telegram") as handler:
            adapter._handle_callback(dict(WELLFORMED_CALLBACK))
        self.assertTrue(handler.lines, "丢弃路径一行日志都没记 —— 这条在量空集")
        matching = [line for line in handler.lines if GREP_ANCHOR in line]
        self.assertEqual(
            len(matching), 1,
            "应恰一行含锚点，实际 %r" % (handler.lines,),
        )
        self.assertEqual(
            hooks.inbounds, [],
            "非白名单的 callback 不该被放行 —— 放行说明丢的是「行为」不是「文案」",
        )

    def test_whitelisted_chat_does_not_log_the_drop_line(self):
        """对照臂：过闸的 callback **不**打这句 ⇒ 上面那条不是无条件记录。

        ``answer`` 走 ``_post`` 出网，这里按仓内先例
        (:mod:`tests.test_telegram_answer_not_ok_warning`) 把 ``_post``
        换成固定应答 —— 本文件不测应答，只为走完过闸那条路。
        """
        adapter, hooks = make_adapter(["424242"])

        def stub_post(method, payload=None, *, timeout=None):
            return {"ok": True, "result": {}}

        adapter._post = stub_post
        with CapturedLogs("telegram") as handler:
            adapter._handle_callback(dict(WELLFORMED_CALLBACK))
        matching = [line for line in handler.lines if GREP_ANCHOR in line]
        self.assertEqual(
            matching, [],
            "白名单内的 chat 不该打丢弃文案 —— 若打了，上面那条逐字断言恒真",
        )
        self.assertEqual(
            len(hooks.inbounds), 1,
            "对照臂必须真的走到放行那条路（恰 1 条 kind=callback 的 inbound），"
            "否则它没资格当对照 —— 实际 %r" % (hooks.inbounds,),
        )


class SourceAnchorTests(unittest.TestCase):
    """锚点在源码里恰出现一次 —— 统一 / 改写文案时这里先红。"""

    def setUp(self):
        with io.open(TELEGRAM_SOURCE_PATH, encoding="utf-8") as handle:
            self.source = handle.read()

    def test_anchor_appears_exactly_once_in_the_source(self):
        self.assertEqual(
            self.source.count(GREP_ANCHOR), 1,
            "锚点只该在丢弃分支出现一次 —— 出现 %d 次"
            % (self.source.count(GREP_ANCHOR),),
        )

    def test_the_two_drop_wordings_stay_distinct(self):
        """callback 版与 message 版是**两句不同的话**，不许被"统一"。

        统一成 message 版 ⇒ 上面 ``count == 1`` 红（锚点消失）；
        统一成 callback 版 ⇒ :mod:`tests.test_inbound_log_structure`
        的 message 版 needle 红。两侧都有人守，哪边改了都响。
        """
        self.assertIn("dropped message from non-whitelisted chat", self.source)
        self.assertIn(GREP_ANCHOR, self.source)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

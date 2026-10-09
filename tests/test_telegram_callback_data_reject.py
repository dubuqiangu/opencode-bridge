"""telegram：超限按钮【报错拒绝】—— 绝不带截断的 callback_data 上线（2026-10-09 修复）。

改之前 ``_button_row`` 把 ``button.data`` 按字节静默截断到 64 字节、再把切碎的
半个字符 ``ignore`` 掉（2026-10-08 telegram 审计，第 1 类已确认缺陷）：
Telegram 对 callback_data **不校验、不报错** ⇒ 点击回来的 data 与按钮绑定的
**不是同一个值** ⇒ 用户点了却触发另一个动作。

处置（用户 2026-10-09 拍板「报错拒绝」）：

* ``_button_row`` 超限即抛 ``ValueError``（与 ``edit()`` 对超长正文的闸同形）；
* ``edit()`` 逐按钮接住：**放弃该按钮** + WARNING 点名序号 / label / 字节数 / 上限，
  其余按钮照常渲染；全部被拒则不下发空的 ``inline_keyboard``。

本文件是那条台账行点名要的**行为型守门**（⛔ 只改行为不加守门等于没改）：

* ASCII 70 字节 ⇒ 拒绝 + 恰好 1 条告警，**且截断产物（前 64 字节）绝不出现**在
  出站 payload 里 —— 那正是缺陷本体；
* 多字节 ``配``×40（120 字节）⇒ 拒绝（审计实测：截断会得到 63 字节、
  切在多字节中间）；
* 恰好 64 字节（边界）⇒ **原样上线、零告警** —— 防把合法边界也拒掉的过宽闸；
* 两个全超限 ⇒ ``inline_keyboard`` 键整个不出现 + 每按钮各 1 条告警。

⚠️ 本文件不联网：全部用替换 ``_post`` 的办法（与 ``test_adapters.py`` 一致）。
"""

from __future__ import annotations

import logging
import unittest

logging.getLogger("opencode_bridge").addHandler(logging.NullHandler())

from opencode_bridge.adapters.telegram import (
    CALLBACK_DATA_LIMIT,
    TelegramAdapter,
)
from opencode_bridge.hooks import Button, MsgHandle, Outbound

from tests.log_redaction_support import (
    RecordingHooks,
    captured_logs,
)


def make_adapter() -> TelegramAdapter:
    adapter = TelegramAdapter({"bot_token": "123:FAKE"}, RecordingHooks())
    adapter.min_interval = 0  # 测试里不做发送间隔等待
    return adapter


def edit_with_buttons(adapter: TelegramAdapter, buttons) -> dict:
    """跑一次 edit 并把实际出站的 payload 交回给断言。"""
    captured_payloads = []

    def fake_post(method, payload=None, *, timeout=None):
        captured_payloads.append(dict(payload or {}))
        return {"ok": True, "result": {}}

    adapter._post = fake_post
    handle = MsgHandle(conversation_id="chat:55", message_id="10", platform="telegram")
    adapter.edit(handle, Outbound("chat:55", "pick", buttons=buttons))
    assert len(captured_payloads) == 1, "应当恰好发出一次 editMessageText"
    return captured_payloads[0]


def callback_warnings(handler) -> list[str]:
    """WARNING 及以上、且点名 ``callback_data`` 的那些行（按文案取，不按顺序）。"""
    return [
        record.message for record in handler.records
        if record.levelname in ("WARNING", "ERROR", "CRITICAL")
        and "callback_data" in record.message
    ]


class TestOversizedButtonsAreRejected(unittest.TestCase):
    """四臂只差「按钮 data 的字节数」一个变量 ⇒ 判据有辨别力。"""

    def test_oversized_ascii_button_is_rejected_not_truncated(self):
        """⭐ 审计实测组：ASCII 70 字节。改之前 ⇒ 上线 64 字节截断产物。

        断言两件事：① 恰好 1 条点名告警（label / 字节数 / 上限都在里面）；
        ② **截断产物 ``"x" * 64`` 绝不出现在 payload 里** —— 缺陷本体是它。
        """
        with captured_logs("telegram") as logs:
            payload = edit_with_buttons(
                make_adapter(),
                (Button("Keep", "ok"), Button("Bad", "x" * 70)),
            )
        warnings = callback_warnings(logs)
        self.assertEqual(len(warnings), 1, f"该恰好 1 条告警。实际：{warnings!r}")
        self.assertIn("Bad", warnings[0])                    # 点名哪个按钮
        self.assertIn("70", warnings[0])                     # 收到多少字节
        self.assertIn(str(CALLBACK_DATA_LIMIT), warnings[0])  # 上限
        self.assertEqual(
            payload["inline_keyboard"],
            [[{"text": "Keep", "callback_data": "ok"}]],
        )
        # ⭐ 缺陷本体：截断产物绝不许上线（改之前这里就是 "x" * 64）。
        self.assertNotIn("x" * CALLBACK_DATA_LIMIT, str(payload))

    def test_oversized_multibyte_button_is_rejected(self):
        """审计实测组：``配``×40（120 字节）—— 旧实现会切成 63 字节
        （切在多字节中间再 ignore 掉半个字符）。唯一按钮被拒 ⇒ 不下发空 keyboard。"""
        with captured_logs("telegram") as logs:
            payload = edit_with_buttons(
                make_adapter(), (Button("配", "配" * 40),)
            )
        warnings = callback_warnings(logs)
        self.assertEqual(len(warnings), 1)
        self.assertIn("120", warnings[0])
        self.assertNotIn("inline_keyboard", payload)

    def test_exactly_at_limit_is_kept_verbatim_and_silent(self):
        """边界本身（64 字节）合法：原样上线、零告警 —— 防过宽闸。"""
        boundary_data = "配" * 21 + "T"  # 21×3 + 1 = 64 字节
        self.assertEqual(len(boundary_data.encode("utf-8")), CALLBACK_DATA_LIMIT)
        with captured_logs("telegram") as logs:
            payload = edit_with_buttons(
                make_adapter(), (Button("OK", boundary_data),)
            )
        noisy = [
            record.message for record in logs.records
            if record.levelname in ("WARNING", "ERROR", "CRITICAL")
        ]
        self.assertEqual(noisy, [])
        self.assertEqual(
            payload["inline_keyboard"],
            [[{"text": "OK", "callback_data": boundary_data}]],
        )

    def test_all_buttons_rejected_omits_keyboard_key_entirely(self):
        """两个全超限 ⇒ 每按钮各 1 条告警 + ``inline_keyboard`` 键整个不出现。"""
        with captured_logs("telegram") as logs:
            payload = edit_with_buttons(
                make_adapter(),
                (Button("One", "y" * 65), Button("Two", "配" * 22)),
            )
        warnings = callback_warnings(logs)
        self.assertEqual(len(warnings), 2)
        self.assertNotIn("inline_keyboard", payload)
        self.assertEqual(payload["text"], "pick")  # 正文照常编辑


if __name__ == "__main__":
    unittest.main()

"""会话 id 规则：**紧跟 id 的中文尾注不再被一起摘要掉**。

缺陷与修法见 :mod:`tests.redaction_cjk_tail_support` 的模块 docstring。
本文件只管**修好的那批形态**；真 id 的完整性与排除集的形状在
:mod:`tests.test_redaction_cjk_id_integrity` /
:mod:`tests.test_redaction_cjk_exclusion_set`。

⚠️ **所有输入都是「裸形态」** ``platform:local_id``：被吞的是
:func:`~opencode_bridge.adapters._redactable_ids.redactable_id` 打进日志的那一层，
而规则对已脱敏的 ``conv#<hex>`` 形态**不匹配** ⇒ 喂 ``conv#`` 形态的探针会拿到
0 命中，从而「否证」一个真缺陷。
"""

from __future__ import annotations

import re
import unittest

from opencode_bridge.redaction import _CONVERSATION_ID_PATTERN
from tests.redaction_cjk_tail_support import (
    ASCII_TAIL_NOTE_CASES,
    TAIL_NOTE_CASES,
    assert_digest_shape,
    expected_tail_note_output,
    make_redactor,
)

#: 半吞那条的输入与本地段 —— 单列出来是因为它是**唯一**一条本地段自带冒号的。
HALF_SWALLOWED_TEXT = "a2a:local:127.0.0.1（来源 peer）"
HALF_SWALLOWED_LOCAL_ID = "local:127.0.0.1"
HALF_SWALLOWED_TAIL_NOTE = "（来源 peer）"


class TailNoteSurvivesTests(unittest.TestCase):
    """紧跟 id 的全角尾注必须**逐字节**留下，且 id 本身被完整摘要。"""

    def setUp(self) -> None:
        self.redactor = make_redactor()

    def test_the_tail_note_survives_verbatim(self):
        for text, platform, local_id, tail_note in TAIL_NOTE_CASES:
            with self.subTest(text=text):
                assert_digest_shape(self, self.redactor.fingerprint("conv", local_id))
                self.assertEqual(
                    self.redactor.scrub(text),
                    expected_tail_note_output(self.redactor, platform, local_id, tail_note),
                )


class HalfSwallowedTests(unittest.TestCase):
    """``a2a:local:127.0.0.1（来源 peer）`` —— 修好前是 ``a2a:conv#xxxx peer）``。

    **半吞**（一半被摘要、一半明文留在日志里）比整条尾注消失更难查：日志读起来
    像乱码，而排障的人不会怀疑那一行本身是坏的。所以这一类单独立一组。
    """

    def setUp(self) -> None:
        self.redactor = make_redactor()

    def test_the_whole_parenthetical_survives(self):
        self.assertEqual(
            self.redactor.scrub(HALF_SWALLOWED_TEXT),
            expected_tail_note_output(
                self.redactor, "a2a", HALF_SWALLOWED_LOCAL_ID, HALF_SWALLOWED_TAIL_NOTE,
            ),
        )

    def test_the_digest_is_followed_by_a_full_width_opening_bracket(self):
        """盯**摘要之后**紧跟的那几个字符。

        修好前那里是 `` peer）``（裸 ASCII 词 + 落单的右括号）；
        修好后必须是 ``（来源 peer）`` 的开头 —— 全角左括号。

        ⛔ 判据不能只写「尾注在」—— 修好前那条输出里 ``peer）`` **也在**。
        ⛔ 也别写死 ``digest_and_tail[7:]``：那是「跳过一个块」的长度，
        而摘要是 ``<6 位>-<6 位>``（两个块）。
        """
        digest_and_tail = self.redactor.scrub(HALF_SWALLOWED_TEXT).split("conv#", 1)[1]
        after_digest = re.sub(r"^[0-9a-f]{6}-[0-9a-f]{6}", "", digest_and_tail)

        self.assertTrue(
            after_digest.startswith(HALF_SWALLOWED_TAIL_NOTE), after_digest,
        )


class AsciiTailNoteRegressionTests(unittest.TestCase):
    """半角形态本来就被排除集挡住了 ⇒ 这次改动**不许**动它们。

    这组是**回归闸**：改动前就全绿，改动后必须仍然全绿。
    """

    def setUp(self) -> None:
        self.redactor = make_redactor()

    def test_the_ascii_forms_behave_exactly_as_before(self):
        for text, local_id, tail_note in ASCII_TAIL_NOTE_CASES:
            with self.subTest(text=text):
                self.assertEqual(
                    self.redactor.scrub(text),
                    expected_tail_note_output(self.redactor, "telegram", local_id, tail_note),
                )


class IdempotenceTests(unittest.TestCase):
    """``(?![a-z]+#)`` 是幂等性的承重项 ⇒ 加了排除集之后它必须**仍然**承重。

    修好后的输出长成 ``telegram:conv#xxxx（判据）`` —— 一个**带尾巴**的已脱敏形态。
    若 ``(?![a-z]+#)`` 因为这次改动失效，``conv#xxxx`` 的 ``xxxx`` 会被再摘要一次，
    日志里出现两个不同的假摘要 —— 那比不脱敏更难查。

    ⚠️ 所以这一组不是「顺手加的」：排除集是这次唯一改动的东西，而它紧挨着
    那个前瞻断言所在的同一条正则。
    """

    #: 修好后仍然必须幂等的输入（含**带尾巴**的那些）。
    SAMPLES = (
        "telegram:12345（判据）",
        "telegram:12345，判据",
        "a2a:local:127.0.0.1（来源 peer）",
        "ntfy:我的话题（备注）",
        "ntfy:我的话题",
        "irc:#中文频道（入站）",
        "homeassistant:light.厨房灯（不在清单）",
    )

    def setUp(self) -> None:
        self.redactor = make_redactor()

    def test_scrubbing_twice_equals_scrubbing_once_for_every_case(self):
        for sample in self.SAMPLES:
            with self.subTest(sample=sample):
                once = self.redactor.scrub(sample)

                self.assertEqual(once, self.redactor.scrub(once))
                self.assertEqual(once, self.redactor.scrub(self.redactor.scrub(once)))

    def test_an_already_redacted_form_is_not_matched_at_all(self):
        """已脱敏形态必须**零命中** —— 连「带全角尾巴」的也必须是。

        这条同时是「喂错形态的探针会否证真缺陷」那份教训的机械证据：
        :data:`~opencode_bridge.redaction._CONVERSATION_ID_PATTERN` 对
        ``telegram:conv#<hex>`` 返回 ``[]``，所以拿它当探针只会得到「缺陷不存在」。
        """
        self.assertEqual(
            _CONVERSATION_ID_PATTERN.findall("telegram:conv#75aed5-296a87"), [],
        )
        self.assertEqual(
            _CONVERSATION_ID_PATTERN.findall("telegram:conv#75aed5-296a87（判据）"), [],
        )


if __name__ == "__main__":
    unittest.main()

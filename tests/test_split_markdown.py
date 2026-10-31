"""C 项守门 —— 结构感知切分（围栏 / 表格）与「纯文字沿用 legacy」的兼容承诺。

来源 = ``MichengAI/dsh-im-connect`` 的 ``src/engine/split.ts``（Apache-2.0，锁
``e271ec1``）；移植件 = :mod:`opencode_bridge.markdown_blocks`；接线点 =
:func:`opencode_bridge.split.split_text`。借鉴点全目录见
``docs/borrowing-catalog.md`` §1。

⚠️ **判据一律是可复核的不变量**，不是抄一份「我以为的输出」：
每片 ≤ ``max_len`` · 每片自带围栏首尾标记 · 每片仍被认作表格（表头重复） ·
序号只加在纯文字片 · 正文重组无损（AGENTS.md §7.1：先打实际值再写断言）。

为什么单独一个文件：``tests/test_split.py``（470 行）全是散文 / emoji，
是 **legacy 路径的守门**；本文件守**结构化路径**与**两路的边界**。
"""

from __future__ import annotations

import unittest
from unittest import mock

import opencode_bridge.split as split_module
from opencode_bridge.markdown_blocks import (
    Segment,
    merge_segments,
    render_chunks,
    table_cells,
    table_end,
    to_blocks,
)
from opencode_bridge.split import split_text

#: 围栏开始标记。三反引号，恰好 3 个字符。
FENCE_OPENING = "```"

#: 测试用编号模板。刻意用**字面量**而不是 ``SEGMENT_NUMBERING_FMT``：
#: 断言的期望值必须能被读者独立复算，而不是与实现共用一个常量（§7.1「恒真断言」）。
TEST_PREFIX_FMT = "[{i}/{n}]"


def _strip_test_prefix(piece: str) -> str:
    """剥掉本文件用的 ``[i/n]`` 前缀；没有前缀就原样返回。"""
    if piece.startswith("[") and "]" in piece:
        head, _, tail = piece.partition("]")
        if head[1:].replace("/", "").isdigit():
            return tail
    return piece


class FenceTests(unittest.TestCase):
    """围栏：不许拦腰切；未闭合的必须补出结束标记。"""

    def test_unclosed_fence_is_completed_and_every_piece_is_self_contained(self):
        body = "a" * 100
        text = FENCE_OPENING + "\n" + body
        # 前提自检：输入确实**没有**结束标记，否则本用例测的是另一件事。
        self.assertEqual(text.count(FENCE_OPENING), 1)

        pieces = split_text(text, 40)

        self.assertGreaterEqual(len(pieces), 2, pieces)
        for piece in pieces:
            self.assertTrue(piece.startswith(FENCE_OPENING + "\n"), repr(piece))
            self.assertTrue(piece.endswith("\n" + FENCE_OPENING), repr(piece))
            self.assertLessEqual(len(piece), 40, repr(piece))
        # 每片恰好两个标记 = 自己的首 + 自己的尾 ⇒ 结束标记是被补出来的。
        marker_total = sum(piece.count(FENCE_OPENING) for piece in pieces)
        self.assertEqual(marker_total, 2 * len(pieces))
        # 正文无损：剥掉每片的首尾标记后拼回去应当逐字等于原正文。
        rebuilt = "".join(
            piece[len(FENCE_OPENING) + 1 : -(len(FENCE_OPENING) + 1)] for piece in pieces
        )
        self.assertEqual(rebuilt, body)

    def test_a_short_fence_is_never_cut_in_half(self):
        fence = "```python\nprint(1)\n```"
        prose_head = "很长的一段开头。" * 5
        text = prose_head + "\n\n" + fence + "\n\n结尾。"

        pieces = split_text(text, 40)

        self.assertGreater(len(pieces), 1, pieces)
        # 判据：某**一个**片里含完整围栏。⛔ 不能改判 ``"\n".join(pieces)``——
        # 那样即使拦腰切成两片，拼回去也照样包含该子串（假绿）。
        self.assertTrue(
            any(fence in piece for piece in pieces),
            "围栏被拦腰切开了：\n" + "\n---+---\n".join(pieces),
        )
        for piece in pieces:
            self.assertLessEqual(len(piece), 40, repr(piece))


class TableTests(unittest.TestCase):
    """表格：按行拆，每片重复表头与分隔行。"""

    HEADER = "| a | b |"
    SEPARATOR = "| --- | --- |"
    ROWS = ["| one | 1 |", "| two | 2 |", "| three | 3 |"]

    def test_oversized_table_repeats_the_header_and_keeps_every_row(self):
        text = "\n".join([self.HEADER, self.SEPARATOR, *self.ROWS])

        pieces = split_text(text, 40)

        self.assertGreaterEqual(len(pieces), 2, pieces)
        for piece in pieces:
            self.assertTrue(
                piece.startswith(self.HEADER + "\n" + self.SEPARATOR), repr(piece)
            )
            self.assertLessEqual(len(piece), 40, repr(piece))
            # 每片仍是**合法表格**（表头 + 分隔行格数一致）——不是只剩半张表。
            self.assertGreater(table_end(piece.split("\n"), 0), 0, repr(piece))
        # 数据行按原顺序、无丢失：每片跳过它自己的表头两行。
        rows = [line for piece in pieces for line in piece.split("\n")[2:]]
        self.assertEqual(rows, self.ROWS)


class NumberingTests(unittest.TestCase):
    """序号只加在纯文字片：结构片加序号会破坏结构。"""

    def test_only_plain_prose_pieces_take_a_prefix(self):
        prose = "甲" * 100
        fence = FENCE_OPENING + "\n" + "b" * 50 + "\n" + FENCE_OPENING
        text = prose + "\n\n" + fence

        pieces = split_text(text, 60, prefix_fmt=TEST_PREFIX_FMT)
        total = len(pieces)

        self.assertGreaterEqual(total, 3, pieces)
        self.assertTrue(pieces[0].startswith(f"[1/{total}]"), repr(pieces[0]))
        self.assertTrue(pieces[1].startswith(f"[2/{total}]"), repr(pieces[1]))
        # 末片是围栏：原样，⛔ 一个前缀字符都不许有。
        self.assertEqual(pieces[-1], fence)
        for piece in pieces:
            self.assertLessEqual(len(piece), 60, repr(piece))
        # 纯文字片拼回去应当逐字等于**该纯文字块**（前缀只加在片首，正文无损）。
        # ⚠️ 块文本 = ``prose + "\n"``：源里 ``prose`` 与围栏之间是一个空行，其中第一个
        # 换行被这个纯文字块吸走（``to_blocks`` 把连续纯文字行并成一个块）⇒ 无损重建必须
        # 保留它。⛔ 断言 ``== prose`` 正是被 C-1 修掉的「丢掉那个换行」行为。
        rebuilt_prose = "".join(
            _strip_test_prefix(piece) for piece in pieces if not piece.startswith(FENCE_OPENING)
        )
        self.assertEqual(rebuilt_prose, prose + "\n")


class StructuredPathIsLosslessTests(unittest.TestCase):
    """C-1 守门：结构化路径的**同一片内**正文逐字无损（⛔ 不注入空行、⛔ 不丢字符）。

    缺陷本体：纯文字切分器切出的片**自带分隔符**（片尾保留 ``"\\n"``）；参考实现
    那种「一律 ``"\\n"`` 连接 + strip 首尾」会**再加一个** ``"\\n"``（两条表格数据行
    之间多出空行 ⇒ GFM 把表格截断）并**删掉**片尾那个 ``"\\n"``（开围栏后的换行丢失）。
    ⇒ 本类的判据是**可复核的不变量**（具体子串在 / 不在 + 重建逐字相等），
    ⛔ 不是抄一份输出。为什么是子串而不是整体相等：跨**消息**（chunk）边界的块间
    分隔符本就不在同一片里（消息是分开发送的），故只有单块样本能整体相等。
    """

    def test_a_split_table_keeps_its_rows_adjacent_without_a_blank_line(self):
        # 一张超过预算的表会被切成多片；相邻两片之间**不得**多出空行，
        # 否则 GFM 会在空行处结束表格、把后半截渲染成普通段落。
        text = "intro\n\n| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\nend"

        pieces = split_text(text, 20)
        joined = "".join(pieces)

        self.assertNotIn("| 1 | 2 |\n\n| 3 | 4 |", joined, pieces)
        self.assertIn("| 1 | 2 |\n| 3 | 4 |", joined, pieces)
        self.assertNotIn("\n\n", joined, pieces)
        for piece in pieces:
            self.assertLessEqual(len(piece), 20, repr(piece))

    def test_a_split_fence_rebuilds_the_original_text_byte_for_byte(self):
        # 单块围栏：整段正文必须**逐字**重建（含开围栏后那个换行，以及闭围栏前的换行）。
        text = FENCE_OPENING + "\nabcdefghijklmnop\n" + FENCE_OPENING

        pieces = split_text(text, 12)

        self.assertGreater(len(pieces), 1, pieces)  # 前提自检：确实被切过
        self.assertEqual("".join(pieces), text, pieces)
        self.assertIn("\nabcdefghijklmnop\n", "".join(pieces), pieces)
        self.assertNotIn("\n\n", "".join(pieces), pieces)
        for piece in pieces:
            self.assertLessEqual(len(piece), 12, repr(piece))

    def test_a_long_prose_block_beside_a_table_rebuilds_with_its_absorbed_newline(self):
        # 源里 ``prose`` 与表格之间是一个空行；其中第一个换行被这个纯文字块吸走
        # （``to_blocks`` 把连续纯文字行并成一个块）⇒ 重建必须保留它，⛔ 不能丢。
        prose = "乙" * 90
        table = "| a | b |\n|---|---|\n| 1 | 2 |"
        text = prose + "\n\n" + table

        pieces = split_text(text, 40)
        prose_pieces = [piece for piece in pieces if not piece.startswith("|")]

        self.assertGreater(len(prose_pieces), 1, pieces)  # 前提自检：纯文字块确实被切过
        self.assertEqual("".join(prose_pieces), prose + "\n", pieces)
        for piece in pieces:
            self.assertLessEqual(len(piece), 40, repr(piece))

    def test_a_continuation_piece_joins_without_injecting_a_blank_line(self):
        # 连接规则本身：续片（``needs_separator_before=False``）直连，其余片之间补一个 ``"\n"``。
        # 第一片片尾自带 ``"\n"``（逐字划分的产物）⇒ 续片直连后恰好是一个换行，⛔ 不是两个。
        pieces = [
            Segment("甲\n", True, True),
            Segment("乙", True, False),
            Segment("丙", True, True),
        ]
        rendered = [
            part.text for part in render_chunks(merge_segments(pieces, 100, 100))
        ]
        self.assertEqual(rendered, ["甲\n乙\n丙"])

        # 对照臂：把那片标成「需要分隔符」⇒ 立刻多出一个空行 ⇒ 证明上面那条不是恒真。
        doubled = [
            Segment("甲\n", True, True),
            Segment("乙", True, True),
            Segment("丙", True, True),
        ]
        rendered_doubled = [
            part.text for part in render_chunks(merge_segments(doubled, 100, 100))
        ]
        self.assertEqual(rendered_doubled, ["甲\n\n乙\n丙"])


class RoutingTests(unittest.TestCase):
    """两路分岔的边界：纯文字单块 ⇒ legacy；有结构 ⇒ 结构路径。"""

    def test_prose_only_text_never_enters_the_structured_path(self):
        with mock.patch.object(split_module, "_split_markdown") as structured:
            structured.side_effect = AssertionError("纯文字单块不得进入结构路径")
            pieces = split_module.split_text("甲" * 200, 50)
        structured.assert_not_called()
        self.assertGreater(len(pieces), 1, pieces)

    def test_structured_text_does_enter_the_structured_path(self):
        # 正对照：证明上一条不是恒真（结构输入确实会走到那条路）。
        sentinel = ["SENTINEL-FROM-STRUCTURED-PATH"]
        text = "甲" * 100 + "\n\n" + FENCE_OPENING + "\ncode\n" + FENCE_OPENING
        with mock.patch.object(
            split_module, "_split_markdown", return_value=sentinel
        ) as structured:
            result = split_module.split_text(text, 30)
        structured.assert_called_once()
        self.assertEqual(result, sentinel)


class ModuleTests(unittest.TestCase):
    """``markdown_blocks`` 两个最容易写错的解析器（转义 / 行内代码 / CR 归一）。"""

    def test_table_cells_ignores_escaped_and_inline_code_pipes(self):
        line = "| a \\| b | `x|y` |"
        self.assertEqual(table_cells(line), [" a | b ", " `x|y` "])
        self.assertIsNone(table_cells("no pipes at all"))

    def test_to_blocks_rebuilds_the_text_modulo_carriage_returns(self):
        text = (
            "段落一\n\n"
            + FENCE_OPENING + "\ncode\n" + FENCE_OPENING + "\n\n"
            "题 | 值\n--- | ---\n1 | 2\n"
        )
        blocks = to_blocks(text)
        self.assertEqual(
            "\n".join(block.text for block in blocks), text.replace("\r\n", "\n")
        )
        # 前提自检：样本里确实各有一个围栏块与表格块，否则断言恒真。
        kinds = [block.kind for block in blocks]
        self.assertEqual(kinds.count("fence"), 1, kinds)
        self.assertEqual(kinds.count("table"), 1, kinds)


if __name__ == "__main__":
    unittest.main()

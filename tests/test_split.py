"""Lane D tests — 分片算法 ``split_text``（纯函数，无 IO / 无网络）。

覆盖 T1.4：码点切分、断点优先级、``（i/n）`` 前缀两遍法重编号、组合序列
（ZWJ / 肤色 / 国旗 / keycap）原子化，以及各种退化路径。
"""

from __future__ import annotations

import re
import unittest

from opencode_bridge.split import _grapheme_atoms, split_text

# -- 组合序列样本 ------------------------------------------------------
FAMILY = "\U0001f468\u200d\U0001f469\u200d\U0001f467"  # 👨‍👩‍👧
FLAG_CN = "\U0001f1e8\U0001f1f3"  # 🇨🇳
FLAG_US = "\U0001f1fa\U0001f1f8"  # 🇺🇸
KEYCAP = "1\ufe0f\u20e3"  # 1️⃣
THUMB_SKIN = "\U0001f44d\U0001f3fd"  # 👍🏽
DEFAULT_FMT = "（{i}/{n}）"


def prefix_regex(prefix_fmt: str = DEFAULT_FMT) -> re.Pattern[str]:
    """由 ``prefix_fmt`` 推出前缀正则（``{}`` 占位处为 ``(\\d+)``）。"""
    pattern = re.escape(prefix_fmt)
    for field in re.findall(r"\{[^{}]*\}", prefix_fmt):
        pattern = pattern.replace(re.escape(field), r"(\d+)")
    return re.compile(r"^" + pattern)


def strip_prefix(chunks: list[str], prefix_fmt: str = DEFAULT_FMT) -> list[str]:
    """去掉每段的分段前缀（无前缀的段原样返回）。"""
    regex = prefix_regex(prefix_fmt)
    return [regex.sub("", chunk, count=1) for chunk in chunks]


def atoms_of(chunks: list[str], prefix_fmt: str = DEFAULT_FMT) -> list[str]:
    """按段重新原子化；若某段被从中间切开，这里会暴露错位的原子。"""
    out: list[str] = []
    for chunk in strip_prefix(chunks, prefix_fmt):
        out.extend(_grapheme_atoms(chunk))
    return out


# ----------------------------------------------------------------------
class ShortTextTests(unittest.TestCase):
    def test_short_text_returned_as_single_chunk(self):
        self.assertEqual(split_text("hello", 100), ["hello"])
        self.assertEqual(split_text("中文短句。", 100), ["中文短句。"])
        # 恰好等于上限：不切、不加前缀
        self.assertEqual(split_text("x" * 100, 100), ["x" * 100])

    def test_empty_text_returns_empty_list(self):
        self.assertEqual(split_text("", 100), [])
        self.assertEqual(split_text("", 0), [])

    def test_non_positive_max_len_returns_whole_text(self):
        text = "很长的一段文本，" * 20
        for max_len in (0, -1, -4096):
            self.assertEqual(split_text(text, max_len), [text])

    def test_single_char_text(self):
        self.assertEqual(split_text("A", 1), ["A"])


# ----------------------------------------------------------------------
class ChunkLimitTests(unittest.TestCase):
    def test_every_chunk_within_max_len(self):
        text = "A" * 5000
        for max_len in (10, 32, 100, 4000):
            chunks = split_text(text, max_len)
            self.assertGreater(len(chunks), 1, f"max_len={max_len}")
            for chunk in chunks:
                self.assertGreater(len(chunk), 0, "空段")
                self.assertLessEqual(len(chunk), max_len, f"max_len={max_len}")

    def test_hard_cut_when_no_break_available(self):
        chunks = split_text("A" * 2500, 100)
        for chunk in chunks[:-1]:
            self.assertEqual(len(chunk), 100)
        self.assertLessEqual(len(chunks[-1]), 100)
        self.assertEqual("".join(strip_prefix(chunks)), "A" * 2500)

    def test_content_is_preserved(self):
        text = "A" * 3000
        chunks = split_text(text, 100)
        self.assertEqual("".join(strip_prefix(chunks)), text)


# ----------------------------------------------------------------------
class BreakPriorityTests(unittest.TestCase):
    def test_chinese_breaks_at_sentence_end(self):
        text = "甲乙丙丁戊己庚辛。" * 4
        chunks = split_text(text, 20)
        self.assertEqual(len(chunks), 4)
        for chunk in chunks:
            self.assertTrue(chunk.endswith("。"), f"未在句号处断行: {chunk!r}")
            self.assertLessEqual(len(chunk), 20)

    def test_newline_break_preferred(self):
        text = ("L" * 50 + "\n") * 100  # 5100 码点
        chunks = split_text(text, 100)
        self.assertEqual(len(chunks), 100)
        for chunk in chunks[:-1]:
            self.assertTrue(chunk.endswith("\n"), f"cut not on newline: {chunk!r}")
        self.assertEqual("".join(strip_prefix(chunks)), text)

    def test_english_period_break(self):
        text = "Hello world. " * 6
        chunks = split_text(text, 20)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks[:-1]:
            self.assertTrue(chunk.endswith(". "), f"未在英文句点后断行: {chunk!r}")
        self.assertEqual("".join(strip_prefix(chunks)), text)

    def test_english_comma_break(self):
        text = "alpha beta, " * 8
        chunks = split_text(text, 24)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks[:-1]:
            self.assertTrue(chunk.endswith(", "), f"未在逗号后断行: {chunk!r}")
        self.assertEqual("".join(strip_prefix(chunks)), text)

    def test_newline_wins_over_sentence_end(self):
        # 窗口内同时有句号和换行：换行优先级更高，段尾应为换行
        chunks = split_text("第一行有句号。\n第二行没有。\n" * 6, 20)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks[:-1]:
            self.assertTrue(chunk.endswith("\n"), f"未优先在换行处断行: {chunk!r}")
        self.assertEqual("".join(strip_prefix(chunks)), "第一行有句号。\n第二行没有。\n" * 6)


# ----------------------------------------------------------------------
class PrefixTests(unittest.TestCase):
    def test_prefix_numbering_is_sequential(self):
        chunks = split_text("A" * 2500, 100)
        self.assertGreater(len(chunks), 20)
        for index, chunk in enumerate(chunks, 1):
            match = prefix_regex().match(chunk)
            self.assertIsNotNone(match, f"缺前缀: {chunk!r}")
            self.assertEqual(int(match.group(1)), index, f"编号跳号: {chunk!r}")
            self.assertEqual(int(match.group(2)), len(chunks), f"n 与段数不符: {chunk!r}")
            self.assertLessEqual(len(chunk), 100)

    def test_prefix_index_never_exceeds_total(self):
        """跨 (max_len, 文本长度, 前缀模板) 扫描，不允许出现"第 3/2 段"。"""
        for prefix_fmt in ("（{i}/{n}）", "[{i}/{n}]", "#{i} of {n}#", "{} / {}"):
            regex = prefix_regex(prefix_fmt)
            for max_len in (16, 24, 64, 128, 512, 4000):
                for length in range(1, 400, 7):
                    text = "甲" * length
                    chunks = split_text(text, max_len, prefix_fmt=prefix_fmt)
                    total = len(chunks)
                    self.assertTrue(chunks)
                    for index, chunk in enumerate(chunks, 1):
                        self.assertLessEqual(len(chunk), max_len)
                        if total <= 1:
                            continue
                        match = regex.match(chunk)
                        self.assertIsNotNone(match, f"缺前缀: {chunk!r}")
                        got_i, got_n = int(match.group(1)), int(match.group(2))
                        self.assertEqual(got_i, index, f"{prefix_fmt} {max_len=} {length=}")
                        self.assertEqual(got_n, total, f"{prefix_fmt} {max_len=} {length=}")

    def test_custom_prefix_fmt(self):
        prefix_fmt = "[{i}/{n}]"
        chunks = split_text("A" * 500, 50, prefix_fmt=prefix_fmt)
        self.assertGreater(len(chunks), 5)
        for index, chunk in enumerate(chunks, 1):
            self.assertTrue(
                chunk.startswith(f"[{index}/{len(chunks)}]"), f"编号不一致: {chunk!r}"
            )
            self.assertLessEqual(len(chunk), 50)
        self.assertEqual("".join(strip_prefix(chunks, prefix_fmt)), "A" * 500)

    def test_long_prefix_uses_wider_digits(self):
        """n 跨过两位数后前缀变长，编号仍须自洽。"""
        chunks = split_text("A" * 5000, 100)
        self.assertGreater(len(chunks), 9)
        for index, chunk in enumerate(chunks, 1):
            match = prefix_regex().match(chunk)
            self.assertIsNotNone(match, chunk)
            self.assertEqual(int(match.group(1)), index)
            self.assertEqual(int(match.group(2)), len(chunks))
            self.assertLessEqual(len(chunk), 100)

    def test_tiny_max_len_falls_back_to_hard_cut_without_prefix(self):
        chunks = split_text("A" * 10, 3)  # 前缀 "（1/2）" 需 5 码点 > 3
        self.assertEqual(chunks, ["AAA", "AAA", "AAA", "A"])
        for chunk in chunks:
            self.assertNotIn("（", chunk)
            self.assertLessEqual(len(chunk), 3)

    def test_prefix_budget_too_small_drops_prefix(self):
        """前缀让预算连一个原子都放不下时，该段放弃前缀而不是超限。"""
        text = "甲乙丙丁戊己庚辛。" * 20
        chunks = split_text(text, 8)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 8, chunk)
        self.assertEqual("".join(strip_prefix(chunks)), text)


# ----------------------------------------------------------------------
class GraphemeTests(unittest.TestCase):
    def test_atomization_groups_combining_sequences(self):
        self.assertEqual(_grapheme_atoms(FAMILY), [FAMILY])
        self.assertEqual(_grapheme_atoms(FLAG_CN + FLAG_US), [FLAG_CN, FLAG_US])
        self.assertEqual(_grapheme_atoms(KEYCAP), [KEYCAP])
        self.assertEqual(_grapheme_atoms(THUMB_SKIN), [THUMB_SKIN])
        self.assertEqual(_grapheme_atoms("1️⃣2️⃣"), [KEYCAP, "2️⃣"])
        # 变音符（Mn）与 keycap 的 U+20E3（Me）都并入基字符
        self.assertEqual(_grapheme_atoms("éx"), ["é", "x"])

    def test_flags_split_on_pair_boundary(self):
        text = "🇨🇳🇺🇸" * 3
        chunks = split_text(text, 8, prefix_fmt="")
        self.assertEqual(atoms_of(chunks, ""), _grapheme_atoms(text))
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 8)
        self.assertEqual("".join(chunks), text)

    def test_family_emoji_never_split(self):
        text = FAMILY * 10 + "尾巴"
        chunks = split_text(text, 20)
        self.assertEqual(atoms_of(chunks), _grapheme_atoms(text))
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 20)
        joined = "".join(strip_prefix(chunks))
        self.assertEqual(joined, text)
        self.assertEqual(joined.count(FAMILY), 10)

    def test_keycap_and_skin_tone_never_split(self):
        text = KEYCAP * 6 + THUMB_SKIN * 6
        chunks = split_text(text, 6, prefix_fmt="")
        self.assertEqual(atoms_of(chunks, ""), _grapheme_atoms(text))
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 6)

    def test_mixed_sequences_with_prefix(self):
        text = FAMILY + FAMILY + FLAG_CN + FLAG_US + KEYCAP + THUMB_SKIN + "abc"
        chunks = split_text(text, 12)
        self.assertEqual(atoms_of(chunks), _grapheme_atoms(text))
        self.assertEqual("".join(strip_prefix(chunks)), text)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 12)

    def test_no_dangling_zwj_at_chunk_boundary(self):
        text = (FAMILY + KEYCAP + FLAG_CN) * 12
        chunks = split_text(text, 14)
        for chunk in strip_prefix(chunks):
            self.assertFalse(chunk.endswith("\u200d"), f"段尾留下孤立 ZWJ: {chunk!r}")
            self.assertFalse(chunk.startswith("\u20e3"), f"段首留下孤立 keycap: {chunk!r}")
        self.assertEqual(atoms_of(chunks), _grapheme_atoms(text))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
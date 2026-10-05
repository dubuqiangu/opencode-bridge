"""Lane D tests — 分片算法 ``split_text``（纯函数，无 IO / 无网络）。

覆盖 T1.4：码点切分、断点优先级、``（i/n）`` 前缀两遍法重编号、组合序列
（ZWJ / 肤色 / 国旗 / keycap）原子化，以及各种退化路径。

⚠️ **默认不加编号**：``prefix_fmt`` 默认是空串，与 13 个生产调用点一致。
所以测编号的用例必须显式传 :data:`SEGMENT_NUMBERING_FMT`（走 :func:`numbered`），
测切点 / 原子化的用例也**显式**传 —— 它们当初是在编号默认开启时写的断点期望，
不显式传就等于悄悄换了基线。默认路径本身由
:class:`OmittedPrefixArgTests` 单独钉住。
"""

from __future__ import annotations

import re
import unittest

from opencode_bridge.split import (
    NO_PREFIX_FMT,
    SEGMENT_NUMBERING_FMT,
    _grapheme_atoms,
    split_text,
)

# -- 组合序列样本 ------------------------------------------------------
FAMILY = "\U0001f468\u200d\U0001f469\u200d\U0001f467"  # 👨‍👩‍👧
FLAG_CN = "\U0001f1e8\U0001f1f3"  # 🇨🇳
FLAG_US = "\U0001f1fa\U0001f1f8"  # 🇺🇸
KEYCAP = "1\ufe0f\u20e3"  # 1️⃣
THUMB_SKIN = "\U0001f44d\U0001f3fd"  # 👍🏽

#: 生产调用点传进来的就是**这个值**（``prefix_fmt=""``）。留成具名常量而不是在断言里
#: 写裸 ``""``：这样测的是"与生产同一个值"，而不是"看起来一样的另一个值"。
PRODUCTION_PREFIX_FMT = NO_PREFIX_FMT


def prefix_regex(prefix_fmt: str = SEGMENT_NUMBERING_FMT) -> re.Pattern[str]:
    """由 ``prefix_fmt`` 推出前缀正则（``{}`` 占位处为 ``(\\d+)``）。"""
    pattern = re.escape(prefix_fmt)
    for field in re.findall(r"\{[^{}]*\}", prefix_fmt):
        pattern = pattern.replace(re.escape(field), r"(\d+)")
    return re.compile(r"^" + pattern)


def strip_prefix(
    chunks: list[str], prefix_fmt: str = SEGMENT_NUMBERING_FMT,
) -> list[str]:
    """去掉每段的分段前缀（无前缀的段原样返回）。"""
    regex = prefix_regex(prefix_fmt)
    return [regex.sub("", chunk, count=1) for chunk in chunks]


def atoms_of(
    chunks: list[str], prefix_fmt: str = SEGMENT_NUMBERING_FMT,
) -> list[str]:
    """按段重新原子化；若某段被从中间切开，这里会暴露错位的原子。"""
    out: list[str] = []
    for chunk in strip_prefix(chunks, prefix_fmt):
        out.extend(_grapheme_atoms(chunk))
    return out


def numbered(text: str, max_len: int) -> list[str]:
    """**显式**要编号前缀的分片。

    编号早已不是默认行为，但本模块绝大多数用例测的是切点 / 原子化这类**与前缀无关**
    的性质，而它们当初是在"编号默认开启"时写的。集中到这里显式传模板，翻默认值时
    这些断点期望就**一个数字都不用改** —— 否则等于悄悄把断点基线换掉，而断点必须
    与各适配器自己的分片保持一致，不能随手重排。
    """
    return split_text(text, max_len, prefix_fmt=SEGMENT_NUMBERING_FMT)


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
            chunks = numbered(text, max_len)
            self.assertGreater(len(chunks), 1, f"max_len={max_len}")
            for chunk in chunks:
                self.assertGreater(len(chunk), 0, "空段")
                self.assertLessEqual(len(chunk), max_len, f"max_len={max_len}")

    def test_hard_cut_when_no_break_available(self):
        chunks = numbered("A" * 2500, 100)
        for chunk in chunks[:-1]:
            self.assertEqual(len(chunk), 100)
        self.assertLessEqual(len(chunks[-1]), 100)
        self.assertEqual("".join(strip_prefix(chunks)), "A" * 2500)

    def test_content_is_preserved(self):
        text = "A" * 3000
        chunks = numbered(text, 100)
        self.assertEqual("".join(strip_prefix(chunks)), text)


# ----------------------------------------------------------------------
class BreakPriorityTests(unittest.TestCase):
    def test_chinese_breaks_at_sentence_end(self):
        text = "甲乙丙丁戊己庚辛。" * 4
        chunks = numbered(text, 20)
        self.assertEqual(len(chunks), 4)
        for chunk in chunks:
            self.assertTrue(chunk.endswith("。"), f"未在句号处断行: {chunk!r}")
            self.assertLessEqual(len(chunk), 20)

    def test_newline_break_preferred(self):
        text = ("L" * 50 + "\n") * 100  # 5100 码点
        chunks = numbered(text, 100)
        self.assertEqual(len(chunks), 100)
        for chunk in chunks[:-1]:
            self.assertTrue(chunk.endswith("\n"), f"cut not on newline: {chunk!r}")
        self.assertEqual("".join(strip_prefix(chunks)), text)

    def test_english_period_break(self):
        text = "Hello world. " * 6
        chunks = numbered(text, 20)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks[:-1]:
            self.assertTrue(chunk.endswith(". "), f"未在英文句点后断行: {chunk!r}")
        self.assertEqual("".join(strip_prefix(chunks)), text)

    def test_english_comma_break(self):
        text = "alpha beta, " * 8
        chunks = numbered(text, 24)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks[:-1]:
            self.assertTrue(chunk.endswith(", "), f"未在逗号后断行: {chunk!r}")
        self.assertEqual("".join(strip_prefix(chunks)), text)

    def test_newline_wins_over_sentence_end(self):
        # 窗口内同时有句号和换行：换行优先级更高，段尾应为换行
        chunks = numbered("第一行有句号。\n第二行没有。\n" * 6, 20)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks[:-1]:
            self.assertTrue(chunk.endswith("\n"), f"未优先在换行处断行: {chunk!r}")
        self.assertEqual("".join(strip_prefix(chunks)), "第一行有句号。\n第二行没有。\n" * 6)


# ----------------------------------------------------------------------
class PrefixTests(unittest.TestCase):
    def test_prefix_numbering_is_sequential(self):
        chunks = numbered("A" * 2500, 100)
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
        chunks = numbered("A" * 5000, 100)
        self.assertGreater(len(chunks), 9)
        for index, chunk in enumerate(chunks, 1):
            match = prefix_regex().match(chunk)
            self.assertIsNotNone(match, chunk)
            self.assertEqual(int(match.group(1)), index)
            self.assertEqual(int(match.group(2)), len(chunks))
            self.assertLessEqual(len(chunk), 100)

    def test_tiny_max_len_falls_back_to_hard_cut_without_prefix(self):
        chunks = numbered("A" * 10, 3)  # 前缀 "（1/2）" 需 5 码点 > 3
        self.assertEqual(chunks, ["AAA", "AAA", "AAA", "A"])
        for chunk in chunks:
            self.assertNotIn("（", chunk)
            self.assertLessEqual(len(chunk), 3)

    def test_prefix_budget_too_small_drops_prefix(self):
        """前缀让预算连一个原子都放不下时，该段放弃前缀而不是超限。"""
        text = "甲乙丙丁戊己庚辛。" * 20
        chunks = numbered(text, 8)
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
        chunks = split_text(text, 8, prefix_fmt=PRODUCTION_PREFIX_FMT)
        self.assertEqual(atoms_of(chunks, ""), _grapheme_atoms(text))
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 8)
        self.assertEqual("".join(chunks), text)

    def test_family_emoji_never_split(self):
        text = FAMILY * 10 + "尾巴"
        chunks = numbered(text, 20)
        self.assertEqual(atoms_of(chunks), _grapheme_atoms(text))
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 20)
        joined = "".join(strip_prefix(chunks))
        self.assertEqual(joined, text)
        self.assertEqual(joined.count(FAMILY), 10)

    def test_keycap_and_skin_tone_never_split(self):
        text = KEYCAP * 6 + THUMB_SKIN * 6
        chunks = split_text(text, 6, prefix_fmt=PRODUCTION_PREFIX_FMT)
        self.assertEqual(atoms_of(chunks, ""), _grapheme_atoms(text))
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 6)

    def test_mixed_sequences_with_prefix(self):
        text = FAMILY + FAMILY + FLAG_CN + FLAG_US + KEYCAP + THUMB_SKIN + "abc"
        chunks = numbered(text, 12)
        self.assertEqual(atoms_of(chunks), _grapheme_atoms(text))
        self.assertEqual("".join(strip_prefix(chunks)), text)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 12)

    def test_no_dangling_zwj_at_chunk_boundary(self):
        text = (FAMILY + KEYCAP + FLAG_CN) * 12
        chunks = numbered(text, 14)
        for chunk in strip_prefix(chunks):
            self.assertFalse(chunk.endswith("\u200d"), f"段尾留下孤立 ZWJ: {chunk!r}")
            self.assertFalse(chunk.startswith("\u20e3"), f"段首留下孤立 keycap: {chunk!r}")
        self.assertEqual(atoms_of(chunks), _grapheme_atoms(text))


# ----------------------------------------------------------------------
class OmittedPrefixArgTests(unittest.TestCase):
    """**漏传** ``prefix_fmt``（就是那个陷阱）必须与 13 个生产调用点逐字节一致。

    13 个生产调用点（``outbound.py`` + 12 处适配器出站）全都显式传
    ``prefix_fmt=""``，而**没有一个**依赖默认值。所以判定标准只有一条：
    **不传参数 == 传 ``""``**，且这个等式要落在真实的段文本上，不是"看起来差不多"。
    """

    #: 三个 18 码点的句子（每句以 ``。`` 收尾）；上限 24 装得下一整句但装不下两句，
    #: 于是每个断点都落在句号上 —— 段文本因此是**可手算的**。
    SENTENCE = "甲乙丙丁戊己庚辛。壬癸子丑寅卯辰巳。"
    THREE_CHUNK_TEXT = SENTENCE * 3
    THREE_CHUNK_LIMIT = 24

    def test_omitting_prefix_fmt_equals_production_call_argument(self):
        """决定性断言：不传参数与生产传的那个值**产出同一批段文本**。"""
        chunks = split_text(self.THREE_CHUNK_TEXT, self.THREE_CHUNK_LIMIT)
        as_production_calls_it = split_text(
            self.THREE_CHUNK_TEXT, self.THREE_CHUNK_LIMIT,
            prefix_fmt=PRODUCTION_PREFIX_FMT,
        )
        self.assertGreater(len(chunks), 1, "样本没切成多段，断言就是空的")
        self.assertEqual(chunks, as_production_calls_it)

    def test_omitted_prefix_fmt_yields_exact_chunk_texts(self):
        """逐段断言正文本身：编号的任何一个字符都不该出现。"""
        chunks = split_text(self.THREE_CHUNK_TEXT, self.THREE_CHUNK_LIMIT)
        self.assertEqual(chunks, [self.SENTENCE] * 3)
        for index, chunk in enumerate(chunks, 1):
            self.assertNotIn("（", chunk, f"第 {index} 段混进了编号起始符")
            self.assertNotIn("）", chunk, f"第 {index} 段混进了编号结束符")
            self.assertNotIn("/", chunk, f"第 {index} 段混进了编号分隔符")
            self.assertNotIn("{", chunk, f"第 {index} 段混进了未渲染的模板占位")
        self.assertEqual("".join(chunks), self.THREE_CHUNK_TEXT)

    #: 已知原子的**字面清单**（家庭 emoji / keycap / 国旗 / 肤色 / 国旗）。这是
    #: 承重断言的依据 —— ⛔ 不能拿 :func:`_grapheme_atoms` 自己当判据：把原子化
    #: 规则改坏时，被测函数和判据会**一起**变错，于是断言照样通过（实测：国旗
    #: 成对规则被去掉后，"逐段原子化 == 整篇原子化"在所有 max_len 上都通过）。
    KNOWN_ATOMS = (FAMILY, KEYCAP, FLAG_CN, THUMB_SKIN, FLAG_US)

    def split_chunks_into_known_atoms(self, chunks: list[str]) -> list[str] | None:
        """把每段按 :data:`KNOWN_ATOMS` 贪心拆开；拆不干净就返回 ``None``。

        返回值是原子的**名字**，这样失败时能直接看出是哪一段、哪一块坏了，
        而不是甩一串看不出所以然的码点。
        """
        atom_names = {FAMILY: "FAMILY", KEYCAP: "KEYCAP", FLAG_CN: "FLAG_CN",
                      THUMB_SKIN: "THUMB_SKIN", FLAG_US: "FLAG_US"}
        names: list[str] = []
        for chunk in chunks:
            rest = chunk
            while rest:
                for atom, name in atom_names.items():
                    if rest.startswith(atom):
                        names.append(name)
                        rest = rest[len(atom):]
                        break
                else:
                    return None
        return names

    #: 一组正好覆盖全部原子化规则的码点序列：家庭 emoji（ZWJ）、keycap（Me）、
    #: 国旗（Regional_Indicator 成对）、肤色修饰符。它是 **14 个码点 / 5 个原子**。
    ASTRAL_GROUP = "".join(KNOWN_ATOMS)

    def test_omitted_prefix_fmt_keeps_join_invariant_on_astral_text(self):
        """星平面 + 组合序列：``"".join(chunks) == text`` 在默认路径上仍成立。

        ⚠️ 两处取值都必须说清，否则断言会变成摆设：

        1. ``max_len`` 要扫**非原子对齐**的值。这一组正好 14 码点，而
           ``max_len == 14`` 恰好让"按码点硬切"与"按原子切"重合 —— 那种取值下
           原子化有没有生效**看不出来**（实测：纯码点变异在 14 上不被抓住，
           在 13 / 11 / 9 上立刻被抓）。
        2. ``max_len`` 不能小于最大的原子（家庭 emoji 是 7 码点）。小于它时
           :func:`_count_atoms` 按规格仍返回 1 个原子，于是那一段**必然超限**
           —— 这不是缺陷，所以不能拿它来断言 ``len(chunk) <= max_len``。
        """
        text = self.ASTRAL_GROUP * 5
        for max_len in (7, 9, 11, 13, 15, 17, 20):
            with self.subTest(max_len=max_len):
                chunks = split_text(text, max_len)
                self.assertGreater(len(chunks), 1, "样本没切成多段，断言就是空的")
                self.assertEqual("".join(chunks), text)
                for chunk in chunks:
                    self.assertLessEqual(len(chunk), max_len, repr(chunk))
                    self.assertFalse(chunk.startswith("\u200d"), f"段首孤立 ZWJ: {chunk!r}")
                    self.assertFalse(chunk.endswith("\u200d"), f"段尾孤立 ZWJ: {chunk!r}")
                    self.assertFalse(chunk.startswith("\u20e3"), f"段首孤立 keycap: {chunk!r}")
                    self.assertFalse(chunk.endswith("\ufe0f"), f"段尾孤立 VS16: {chunk!r}")
                    self.assertNotIn("（", chunk, f"混进了编号起始符: {chunk!r}")
                # ⛔ 承重判据：**每段都能按字面原子清单拆干净**，且拼出来的原子
                # 序列与原文一致。任何被从中间切开的 emoji / 组合序列都拆不干净。
                #
                # ⚠️ 这里**不能**拿 ``_grapheme_atoms`` 当判据（无论逐段还是先拼后
                # 拆）：把原子化规则改坏时，被测函数与判据会**一起**变错，断言照样
                # 通过 —— 实测：国旗成对规则被去掉后，两种写法在所有 max_len 上都
                # 通过。所以判据必须是与被测实现无关的**字面清单**。
                split_names = self.split_chunks_into_known_atoms(chunks)
                self.assertIsNotNone(
                    split_names,
                    f"max_len={max_len} 下有段拆不成完整原子："
                    f"{[repr(c) for c in chunks]}",
                )
                self.assertEqual(
                    split_names, ["FAMILY", "KEYCAP", "FLAG_CN", "THUMB_SKIN",
                                   "FLAG_US"] * 5,
                    f"max_len={max_len} 下原子被切开或次序错乱：{split_names}",
                )
                self.assertEqual("".join(chunks).count(FAMILY), 5)

    def test_omitted_prefix_fmt_keeps_newline_break_priority(self):
        """默认路径上的断点优先级：换行仍然最高（纯码点硬切会在此处露馅）。"""
        line = "L" * 17 + "\n"  # 18 码点
        text = line * 8
        chunks = split_text(text, 40)
        self.assertGreater(len(chunks), 1)
        # 40 装得下两行整（36 码点）但装不下三行（54），故每段在第二个换行处断开。
        for chunk in chunks[:-1]:
            self.assertTrue(chunk.endswith("\n"), f"未在换行处断行: {chunk!r}")
        self.assertEqual("".join(chunks), text)
        # 段数 = 8 行 / 2 行每段。若换行断点被去掉，会退化成 40 码点的硬切。
        self.assertEqual(len(chunks), 4, f"断点被重排: {[len(c) for c in chunks]}")

    def test_omitted_prefix_fmt_keeps_english_punctuation_break_priority(self):
        """默认路径上英文 ``". "`` / ``", "`` 之后仍是断点（否则段数会变）。

        ``"Hello world. "`` 是 13 码点：``max_len=17`` 时按句点断行得到 **8 段 ×13**
        （段尾带着那个空格 —— 断点在 ``". "`` **之后**，故空格归上一段），
        而 17 的纯硬切是 6 段（17×5 + 2）—— 段数差得很开，一眼可辨。
        """
        text = "Hello world. " * 8
        chunks = split_text(text, 17)
        self.assertEqual(
            chunks, ["Hello world. "] * 8,
            f"英文句点断点失效: {[len(c) for c in chunks]}",
        )
        self.assertEqual("".join(chunks), text)

    def test_omitted_prefix_fmt_on_short_text_is_single_chunk(self):
        """默认路径下短文本仍原样返回单元素（不可能加编号）。"""
        self.assertEqual(split_text("短句。", 100), ["短句。"])

    def test_new_adapter_omitting_the_argument_ships_no_numbering(self):
        """钉住"陷阱已关闭"：一个**照着现有适配器写**、但漏传参数的新适配器。

        现有 13 个调用点全都显式写 ``prefix_fmt=""``，所以"照抄现有写法"的人不会
        中招 —— 会中招的是**照着 :func:`split_text` 的签名**写、觉得前缀是默认行为
        的那个人。这个用例就替那个人跑一遍：他拿到的东西必须是 13 个现役平台
        同样的正文，不带任何编号。
        """
        class AdapterThatForgetsTheArgument:
            """出站分片的最小形状：照 13 个现役调用点的样子写，只是漏了参数。"""

            max_message_length = 24

            def chunks_for(self, text: str) -> list[str]:
                return split_text(text, self.max_message_length)  # 漏传 prefix_fmt

        chunks = AdapterThatForgetsTheArgument().chunks_for(self.THREE_CHUNK_TEXT)
        self.assertEqual(chunks, [self.SENTENCE] * 3)
        self.assertEqual("".join(chunks), self.THREE_CHUNK_TEXT)
        self.assertNotIn("（", "".join(chunks))

    def test_omitting_argument_matches_explicitly_written_new_adapter(self):
        """两个"新适配器"必须给出同一结果：一个漏传、一个照现役写法显式传空串。

        这条把上面那条从"看起来对"升级成"与现役平台一致" —— 判据是**同一个
        Adapter 基类形状下两种写法的产出相等**，不是断言里重复一遍期望值。
        """
        class ExplicitAdapter:
            max_message_length = 24

            def chunks_for(self, text: str) -> list[str]:
                return split_text(text, self.max_message_length, prefix_fmt="")

        text = self.THREE_CHUNK_TEXT
        omitted = split_text(text, 24)
        self.assertEqual(omitted, ExplicitAdapter().chunks_for(text))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
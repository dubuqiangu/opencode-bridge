"""排除集的**形状**（逐码位清单）+ 仓库日志调用点的**爆炸半径**复核。

缺陷与修法见 :mod:`tests.redaction_cjk_tail_support` 的模块 docstring。

为什么这两件事要放一份文件
==========================

它们问的是**同一个问题的两面**：排除集多收一个字符 ⇒ 切碎一类真 id；
少收一个字符 ⇒ 漏掉一类尾注。所以它们共用同一份码位清单与同一个
:func:`~tests.redaction_cjk_tail_support.is_excluded_by` 探针。

两块的分工
==========

1. :class:`ExclusionSetShapeTests` —— 排除集**逐码位**钉死：多一个、少一个、
   以及「一个 CJK 汉字都不许收」的反向闸；
2. :class:`RepositoryLogCallSiteTests` —— 用 AST 把仓库里**实参是平台前缀 id**
   的日志调用点全找出来，渲染后断言**模板里的每个字符都原数守恒**。
"""

from __future__ import annotations

import unittest

from tests.redaction_cjk_tail_support import (
    BASELINE_LOCAL_ID_PATTERN,
    CODE_POINT_SCAN_RANGE,
    EXPECTED_EXCLUDED_RANGES,
    EXPECTED_KEPT_RANGES,
    PROBE_LOCAL_ID,
    excluded_code_points,
    find_logger_calls_with_prefixed_id_arguments,
    is_excluded_by,
    is_excluded_by_rule,
    make_redactor,
)


class ExclusionSetShapeTests(unittest.TestCase):
    """排除集是**承重**的 ⇒ 从两端各钉一次：该收的都收了，不该收的一个没收。"""

    def test_the_exclusion_probe_is_not_vacuous(self) -> None:
        """先证明这个探针**会说出「不排除」**。

        ⚠️ 这条是 :func:`is_excluded_by` 的非空洞性闸。它**真的**坏过一次：
        取了 ``group(1)``（那是**平台段**）而 local-id 在 ``group(2)`` ⇒
        「汉字不在 ``telegram`` 里」被读成「汉字被排除」⇒ 下面两条都恒真，
        而「全角标点都被排除」那条**照样绿**（它只断言 ``True``）。
        ⇒ **一个恒真的探针会让这一整类断言失去意义**，所以先钉住它两个方向。
        """
        self.assertTrue(is_excluded_by_rule("（"), "全角左括号必须被判为分隔符")
        self.assertFalse(is_excluded_by_rule("我"), "汉字必须被判为 id 的一部分")
        self.assertFalse(is_excluded_by_rule("0"), "数字必须被判为 id 的一部分")
        self.assertFalse(
            is_excluded_by(BASELINE_LOCAL_ID_PATTERN, "我"),
            "对照用的旧模式也必须能区分汉字",
        )

    def test_every_full_width_punctuation_mark_terminates_the_local_id(self) -> None:
        """清单里的每一个码位都真的终止 local-id 段（不是「大概覆盖了」）。"""
        for code_point in sorted(excluded_code_points()):
            with self.subTest(code_point="U+%04X" % code_point):
                self.assertTrue(
                    is_excluded_by_rule(chr(code_point)),
                    "U+%04X 没被排除 —— 对应尾注会被一起吞掉" % code_point,
                )

    def test_not_one_han_character_or_full_width_letter_is_excluded(self) -> None:
        """反向钉一次：CJK 汉字、假名、全角字母与数字**必须留在** id 里。

        这是「不许切碎真 id」那条主闸的**结构形式** —— 逐码位扫过整个 CJK 基本区
        （约 2 万字）与全角字母数字区，而不是挑几个字举例。
        """
        offenders = [
            "U+%04X" % code_point
            for low, high in EXPECTED_KEPT_RANGES
            for code_point in range(low, high + 1)
            if is_excluded_by_rule(chr(code_point))
        ]
        self.assertEqual(offenders, [], "这些**文字**被当成分隔符排除了")

    def test_the_excluded_set_is_exactly_the_documented_ranges(self) -> None:
        """这次改动**新**排除的码位，必须恰好是清单上那些，且一个都没漏。

        判据问的是**增量**（相对 :data:`BASELINE_LOCAL_ID_PATTERN`），不是绝对值：
        绝对值会把改动前就被 ``\\s`` 排除的空白字符也算进来，那条断言在改动前
        就会红（§7.1：判据必须先在改动前跑过）。
        """
        expected = set()
        for low, high in EXPECTED_EXCLUDED_RANGES:
            expected.update(range(low, high + 1))

        newly_excluded = {
            code_point
            for code_point in CODE_POINT_SCAN_RANGE
            if is_excluded_by_rule(chr(code_point))
            and not is_excluded_by(BASELINE_LOCAL_ID_PATTERN, chr(code_point))
        }
        no_longer_excluded = {
            code_point
            for code_point in CODE_POINT_SCAN_RANGE
            if is_excluded_by(BASELINE_LOCAL_ID_PATTERN, chr(code_point))
            and not is_excluded_by_rule(chr(code_point))
        }

        self.assertEqual(
            sorted(newly_excluded - expected), [],
            "多排除了这些码位（每一个都会切碎一类真 local-id）",
        )
        self.assertEqual(
            sorted(expected - newly_excluded), [],
            "少排除了这些码位（每一个都会漏掉一类尾注）",
        )
        self.assertEqual(
            sorted(no_longer_excluded), [],
            "这些码位改动前被排除、改动后不再被排除（⇒ 有 id 的脱敏变松了）",
        )


class RepositoryLogCallSiteTests(unittest.TestCase):
    """仓库里**实参是平台前缀 id** 的那些日志调用点：模板字符必须逐个守恒。

    判据是**字符计数**而不是「尾注在不在」：探针 id 是纯 ASCII，于是渲染结果里
    模板的每一个字符都必须**原样、原数**地留在输出中 —— 少一个就是被吞了。

    ⛔ **不改这些格式串**（比如插个空格绕过）—— 那是权宜不是修复，
    ``fix-136`` 已经被记过一次「绕过」。
    """

    #: 探针实参表：模板里除 id 之外的占位符都填这个字符串。
    #: 刻意**不含任何数字/邮箱/凭据形状**，免得别的规则先动手、掩盖这一条要验的东西。
    FILLER = "x"

    @classmethod
    def setUpClass(cls) -> None:
        cls.call_sites = find_logger_calls_with_prefixed_id_arguments()
        cls.redactor = make_redactor()

    def _render(self, template: str, slot_index: int) -> "str | None":
        """把模板渲染出来（id 槽放探针），渲染不了就返回 ``None``。"""
        probe = [self.FILLER] * 9
        if slot_index >= len(probe):
            return None
        probe[slot_index] = PROBE_LOCAL_ID
        try:
            return template % tuple(probe)
        except (TypeError, ValueError):
            return None            # 占位符比探针表多/少 ⇒ 这条跳过

    def test_the_audit_actually_found_the_prefixed_id_call_sites(self) -> None:
        """防空跑：判据在改动之前跑过吗？这里就是「它跑到了东西」的证据。"""
        self.assertGreaterEqual(len(self.call_sites), 20)

    def test_every_template_character_survives_scrubbing(self) -> None:
        offenders = []
        for relative, line, template, slot_index, _ in self.call_sites:
            rendered = self._render(template, slot_index)
            if rendered is None:
                continue
            scrubbed = self.redactor.scrub(rendered)
            for character in set(rendered) - set(PROBE_LOCAL_ID):
                if rendered.count(character) != scrubbed.count(character):
                    offenders.append(
                        "%s:%d 模板=%r 字符=%r" % (relative, line, template, character)
                    )
                    break
        self.assertEqual(offenders, [], "这些调用点的模板字符被脱敏器改动了")

    def test_the_probe_itself_is_redacted_in_every_one_of_them(self) -> None:
        """非空洞性：上面那条守的是「模板字符守恒」，这条守「id 真的被脱敏」。

        没有这条，一个「什么都不匹配」的正则也能让上一条永远绿。
        """
        not_redacted = [
            "%s:%d %r" % (relative, line, template)
            for relative, line, template, slot_index, _ in self.call_sites
            if (rendered := self._render(template, slot_index)) is not None
            and PROBE_LOCAL_ID in self.redactor.scrub(rendered)
        ]
        self.assertEqual(not_redacted, [])


if __name__ == "__main__":
    unittest.main()

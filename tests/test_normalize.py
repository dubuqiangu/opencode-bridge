"""``normalize`` 的纯函数测试 —— 不构造 :class:`BridgeCore`。

``normalize`` 是入站/命令/事件流三条路径共用的叶子模块：不做 I/O、不发消息，
所以它能单独测。

本文件锁的是 :func:`~opencode_bridge.normalize.trim_outer_whitespace` 的**全部**
边界 —— 它顶替的是入站路径上那个无参 ``.strip()``，而那个 ``.strip()`` 去掉
**前导**空白会把粘贴的第一行 dedent。所以每一条用例都刻意**在第一行放缩进**：

    ⚠️ 一个第一行没有缩进的样本会让这些断言**恒真**（dedent 它等于什么都没做），
    那就是一条骗人的测试。这里没有一条。

⚠️ 接线不在这里：入站路径有没有真的用它，由 ``tests/test_inbound_gateway.py``
与 ``tests/test_core.py`` 断言（它们看的是 **agent 实际收到什么**）。
"""

from __future__ import annotations

import unittest

from opencode_bridge.normalize import trim_outer_whitespace

#: 一段带缩进的 Python。第一行**有**缩进，所以 dedent 它一定看得出来。
INDENTED_PYTHON = "    def f():\n        return 1"

#: 同上但前面有空行。空行要丢，缩进要留。
PYTHON_AFTER_BLANK_LINES = "\n\n    def g():\n        return 2\n\n"

#: 嵌套列表：缩进**只在第一行之外**出现的那种最容易漏。
INDENTED_YAML = "  build:\n    steps:\n      - name: test\n        run: pytest"


class PreservesIndentationTests(unittest.TestCase):
    """**决定性的一节**：缩进逐字节保留。"""

    def test_an_indented_first_line_keeps_its_indentation(self):
        self.assertEqual(trim_outer_whitespace(INDENTED_PYTHON), INDENTED_PYTHON)

    def test_deeper_indentation_on_later_lines_is_untouched(self):
        trimmed = trim_outer_whitespace(INDENTED_PYTHON)

        self.assertEqual(trimmed.split("\n")[1], "        return 1")

    def test_nested_structures_keep_every_line(self):
        self.assertEqual(trim_outer_whitespace(INDENTED_YAML), INDENTED_YAML)

    def test_tab_indentation_survives_too(self):
        tabbed = "\tdef f():\n\t\treturn 1"

        self.assertEqual(trim_outer_whitespace(tabbed), tabbed)

    def test_mixed_indentation_widths_are_left_alone(self):
        mixed = "   a\n\t b\n     c"

        self.assertEqual(trim_outer_whitespace(mixed), mixed)


class LeadingBlankLineTests(unittest.TestCase):
    """开头的空行照丢（那是 :meth:`on_inbound` 一直以来的行为），缩进不丢。"""

    def test_leading_blank_lines_are_dropped_and_indentation_kept(self):
        self.assertEqual(
            trim_outer_whitespace(PYTHON_AFTER_BLANK_LINES),
            "    def g():\n        return 2",
        )

    def test_leading_blank_lines_that_are_themselves_indented_are_dropped(self):
        """⚠️ 这条是"第一行空但带缩进"那个边界：整行空白才算空行，
        而**第一行有内容的行**的缩进一个字都不许动。"""
        trimmed = trim_outer_whitespace("\n    \n\t\n    def h():\n        return 3")

        self.assertEqual(trimmed, "    def h():\n        return 3")

    def test_a_single_leading_newline_is_dropped(self):
        self.assertEqual(
            trim_outer_whitespace("\n    x = 1"), "    x = 1"
        )

    def test_leading_blank_lines_containing_carriage_returns_are_dropped(self):
        trimmed = trim_outer_whitespace("\r\n\r\n    def i():\n        return 4")

        self.assertTrue(trimmed.startswith("    def i():"),
                        "CRLF 开头的空行要丢，且第一行缩进要留：%r" % trimmed)

    def test_blank_lines_inside_the_body_are_kept(self):
        """内部空行是代码结构的一部分（PEP 8 的函数间空行），一个字都不能少。"""
        body = "def a():\n    pass\n\n\ndef b():\n    pass"

        self.assertEqual(trim_outer_whitespace(body), body)


class TrailingWhitespaceTests(unittest.TestCase):
    """尾巴上的空白一律去掉 —— 那是 :meth:`on_inbound` 一直以来的行为。"""

    def test_trailing_newlines_are_dropped(self):
        self.assertEqual(
            trim_outer_whitespace("    def j():\n        return 5\n\n\n"),
            "    def j():\n        return 5",
        )

    def test_trailing_spaces_on_the_last_line_are_dropped(self):
        self.assertEqual(trim_outer_whitespace("    x = 1   "), "    x = 1")

    def test_trailing_indentation_only_line_is_dropped_whole(self):
        self.assertEqual(
            trim_outer_whitespace("def k():\n    pass\n    "),
            "def k():\n    pass",
        )

    def test_trailing_whitespace_on_interior_lines_is_kept(self):
        """⚠️ 只动**结尾**。行尾那圈空格在正文里，删掉它同样是改写用户写的代码
        （有些格式化的 lint 规则对它有意见，但那是 lint 的事，不是桥的事）。"""
        body = "def m():\n    return 1   \n\ndef n():\n    pass"

        self.assertEqual(trim_outer_whitespace(body), body)

    def test_a_body_that_is_only_whitespace_becomes_empty(self):
        """全空白 → 空串。调用方的 ``if not text`` 守卫据此丢掉它。

        这是那个无参 ``.strip()`` **真正**被依赖的行为，也是它当初存在的理由。
        """
        for blank in ("", "   ", "\n", "\n\n", "  \n\t\n  ", "\r\n"):
            with self.subTest(blank=blank):
                self.assertEqual(trim_outer_whitespace(blank), "")

    def test_whitespace_only_input_never_raises(self):
        """⚠️ 开头那道 ``if not text.strip()`` 不是装饰 —— 少了它，下面那个
        "跳过开头空行"的 ``while`` 会一路走出列表末尾，抛 ``IndexError``。

        而 :meth:`~opencode_bridge.inbound_gateway.InboundGateway.on_inbound` 是
        ``except Exception: logger.exception(...)`` 兜着的，于是那条消息会变成
        **日志里一条看不懂的栈、IM 上什么也没有** —— 正是 AGENTS.md §8 说的
        "用户静默地遇到错的东西"。所以这条断言的是"不抛"，不是"抛什么"。
        """
        for blank in (" ", "  ", "\n", "\n\n\n", " \n \n ", "\t", "\r",
                      "\r\n", "\t\n \r\n\t", " " * 50):
            with self.subTest(blank=blank):
                try:
                    self.assertEqual(trim_outer_whitespace(blank), "")
                except IndexError:
                    self.fail(
                        "全空白输入 %r 抛了 IndexError —— 开头那道守卫没了"
                        % (blank,)
                    )


class PassthroughTests(unittest.TestCase):
    def test_a_single_line_without_any_whitespace_is_unchanged(self):
        self.assertEqual(trim_outer_whitespace("hi"), "hi")

    def test_a_single_indented_line_keeps_its_indentation(self):
        """一行、首行带缩进 —— 合成器的"只去掉头两行"会毁掉它。"""
        self.assertEqual(trim_outer_whitespace("    indented"), "    indented")

    def test_text_that_is_only_leading_space_is_emptied(self):
        self.assertEqual(trim_outer_whitespace("     "), "")

    def test_non_string_input_is_coerced_like_clean_does(self):
        self.assertEqual(trim_outer_whitespace(None), "")
        self.assertEqual(trim_outer_whitespace(42), "42")

    def test_carriage_returns_inside_the_body_are_left_alone(self):
        """``_clean`` 保留 ``\\r``，所以 CRLF 的中间部分与改动前一样原样送达。

        这里刻意只锁"不改"，不锁"该不该改" —— 那是一次独立的取舍。
        """
        crlf = "    def o():\r\n        return 6"

        self.assertEqual(trim_outer_whitespace(crlf), crlf)


class IdempotenceTests(unittest.TestCase):
    def test_trimming_twice_changes_nothing(self):
        for body in (INDENTED_PYTHON, PYTHON_AFTER_BLANK_LINES, INDENTED_YAML,
                     "  ", "\n\n  x  \n"):
            with self.subTest(body=body):
                once = trim_outer_whitespace(body)
                self.assertEqual(trim_outer_whitespace(once), once)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

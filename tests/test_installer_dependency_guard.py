"""安装脚本的「缺 Python」闸门：Unix 硬失败、Windows 必须与之对齐。

审计 C16：``install.sh`` 在缺 ``python3`` 时 ``exit 1`` 硬失败，而
``install.ps1`` 只 ``Write-Warn`` 一句就继续 ⇒ 之后照样打印「安装完成」，
可 bridge 根本跑不起来。

本测试把两个脚本的**缺 Python 分支**分别钉住，且**只**钉那一段：

* ``install.ps1`` —— 先锚在 ``Test-HasCommand "python"``，再取它那个**配平花括号**
  的 ``{ ... }`` 体 ⇒ 断言这段含 ``Write-Bad`` 与 ``return 1``、**不含** ``Write-Warn``。
  ⛔ 全文件级 ``"Write-Bad" in text`` 恒真（该文件里到处是 ``Write-Bad``），
  所以必须把断言缩进那一段，否则它什么也证明不了。
* ``install.sh`` —— 锚在 ``command -v python3``，取到第一个 ``fi`` 的 ``if`` 块
  ⇒ 断言这段含 ``exit 1``。

⛔ 全部锚在符号 / 文本上，**不含行号**（行号一插入就过期）。
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_PS1_PATH = REPO_ROOT / "install.ps1"
INSTALL_SH_PATH = REPO_ROOT / "install.sh"


def extract_braced_block(text: str, anchor: str) -> str:
    """取锚点之后第一个 ``{`` 到与之配平的 ``}`` 之间的子串（含两端花括号）。

    锚点必须存在，否则 :class:`AssertionError` 会说清是哪个锚点没找到 ——
    「空集被当成不存在」是本仓库栽过的坑（§7.1），这里让它响亮失败而不是静默返回空串。
    """
    if anchor not in text:
        raise AssertionError("锚点不存在: %r" % anchor)
    anchor_index = text.index(anchor)
    open_index = text.index("{", anchor_index)
    depth = 0
    for position in range(open_index, len(text)):
        character = text[position]
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return text[open_index:position + 1]
    raise AssertionError("锚点 %r 的花括号体没有配平" % anchor)


def extract_shell_if_block(text: str, anchor: str) -> str:
    """取含锚点的那一行 ``if`` 到其后第一个 ``fi`` 之间的子串（含两端）。

    只在「锚点所在块内没有嵌套 ``if``」时成立 —— 缺 Python 闸门就是这种形状，
    与本测试的用途一致。锚点缺失 / 没有 ``fi`` 都会响亮失败。
    """
    lines = text.splitlines()
    anchor_line = next(
        (line_number for line_number, line in enumerate(lines) if anchor in line),
        None,
    )
    if anchor_line is None:
        raise AssertionError("锚点不存在: %r" % anchor)
    for line_number in range(anchor_line, len(lines)):
        if lines[line_number].strip() == "fi":
            return "\n".join(lines[anchor_line:line_number + 1])
    raise AssertionError("锚点 %r 的 if 块没有结尾 fi" % anchor)


class InstallPs1MissingPythonGuardTests(unittest.TestCase):
    """``install.ps1`` 的缺 Python 分支必须硬失败，而不是 warning 后继续。"""

    def setUp(self):
        self.source_text = INSTALL_PS1_PATH.read_text(encoding="utf-8")
        self.missing_python_block = extract_braced_block(
            self.source_text, 'Test-HasCommand "python"'
        )

    def test_block_isolates_the_python_branch_not_another_write_bad_block(self):
        """取到的必须是缺 Python 那一段，不是别处的 ``Write-Bad`` 块。

        紧邻的缺 git 分支同样含 ``Write-Bad`` 与 ``return 1`` ⇒ 没有这一条，
        另外两条断言换到任何一个 ``Write-Bad`` 分支上也会绿（恒真的守门）。
        """
        self.assertIn("python", self.missing_python_block)

    def test_missing_python_branch_reports_error_and_returns_failure(self):
        self.assertIn("Write-Bad", self.missing_python_block)
        self.assertIn("return 1", self.missing_python_block)

    def test_missing_python_branch_does_not_warn_and_continue(self):
        """回归到旧的 ``Write-Warn`` 后继续 ⇒ 这里红。"""
        self.assertNotIn("Write-Warn", self.missing_python_block)


class InstallShMissingPythonGuardTests(unittest.TestCase):
    """``install.sh`` 的缺 ``python3`` 分支必须 ``exit 1`` —— ps1 与它对齐的基准。"""

    def setUp(self):
        self.source_text = INSTALL_SH_PATH.read_text(encoding="utf-8")
        self.missing_python_block = extract_shell_if_block(
            self.source_text, "command -v python3"
        )

    def test_missing_python3_branch_exits_with_failure(self):
        self.assertIn("exit 1", self.missing_python_block)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

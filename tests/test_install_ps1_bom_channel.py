"""Windows 一键安装的两条约束必须同时成立 —— 它们互相踩，所以只能钉在【文档形态】上。

背景（2026-11-21 真机实测）
--------------------------
``install.ps1`` 含大量中文字面量 ⇒ 按本仓库自述，它**必须**以 UTF-8 with BOM 保存，
否则 PowerShell 5.1 按系统 ANSI 代码页解读，``Invoke-Uninstall`` 一带直接
``字符串缺少终止符``。⇒ **BOM 不能删。**

而 ``iwr <url> | iex`` 这条路把响应正文（**含** U+FEFF）送进 ``iex`` 的解析输入
⇒ 解析器认不出 ``<#`` 块注释起点 ⇒ 整段注释被当代码解析 ⇒ 报 4 处语法错
⇒ **一件事都装不上**，而退出码仍是 0（5.1 抛 ParserError；pwsh 7 连错误都不抛）。
⇒ A/B 只差一个码点：``RAW len=14055 first_cp=65279 parse_errors=4``
　　vs ``TRIM len=14054 first_cp=60 parse_errors=0``；trim 之后两个 shell 都装成。

⇒ 文档里的 Windows 取法**只有一种**成立：``-OutFile`` 按原始字节落盘 ⇒ ``-File`` 执行。
　（``-OutFile`` 不重编码，BOM 保住；``(iwr).Content | Out-File`` 那种写法反而会丢。）

本文件钉住三件事
----------------
1. ``install.ps1`` / ``plugin/install.ps1`` 的**字节**仍以 UTF-8 BOM 开头；
2. 这几份文档里**没有任何一行**把 raw 的 install.ps1 通过管道喂给 ``iex``；
3. 凡是**从 raw 取 install.ps1** 的行，一律用 ``-OutFile`` 落盘（不是别的写文件 cmdlet），
   且该文档里确有一条 ``-File`` 执行 —— 免得只下不跑。

⚠️ 第 3 条是第 2 条的**补集**：第 2 条只禁 ``| iex`` 这一种写法，换成
　　``| Set-Content`` / ``| Out-File`` 同样会毁掉 BOM，而第 2 条对它们静默放行。
　　（第一版判据只查 ``| iex``，反向证明的 M4 变异「改成 | Set-Content」直接漏过。）

⚠️ 判据锚在**符号/形状**上（``-OutFile``、``-File``、``U+FEFF``），⛔ 不含行号。

⚠️ 有意**不**扫 ``docs/audit-2026-11-05-docs-and-installability.md``：那份审计文档
　　必须能**引用**坏形态（它正是记录这次推翻的地方，表格里那份写法带 ``\`` 转义）。
　　判据比被审对象窄，是为了不制造「把证据改掉」这种更坏的改动。
"""

from __future__ import annotations

import io
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

INSTALL_PS1_PATH = REPO_ROOT / "install.ps1"
PLUGIN_INSTALL_PS1_PATH = REPO_ROOT / "plugin" / "install.ps1"

UTF8_BOM = b"\xef\xbb\xbf"

DOCUMENTED_INSTALL_FILES = (
    REPO_ROOT / "README.md",
    REPO_ROOT / "docs" / "install.md",
    REPO_ROOT / "docs" / "update.md",
    REPO_ROOT / "plugin" / "README.md",
    INSTALL_PS1_PATH,
)

PIPE_TO_IEX_LINE = re.compile(
    r"raw\.githubusercontent\.com.*\|\s*(?:iex|Invoke-Expression)\b",
    re.IGNORECASE,
)

FETCH_RAW_INSTALLER_LINE = re.compile(
    r"raw\.githubusercontent\.com.*/install\.ps1",
    re.IGNORECASE,
)

RUN_FILE_MODE = re.compile(r"\s-File\s+\S+", re.IGNORECASE)


def read_text(path: Path) -> str:
    with io.open(str(path), encoding="utf-8") as handle:
        return handle.read()


class InstallerScriptsKeepUtf8BomTests(unittest.TestCase):
    """BOM 是 PowerShell 5.1 读中文的承重件 —— 删了它，``-File`` 那条路当场崩。"""

    def test_install_ps1_begins_with_utf8_bom(self):
        self.assertEqual(
            INSTALL_PS1_PATH.read_bytes()[:3],
            UTF8_BOM,
            "install.ps1 必须以 UTF-8 with BOM 保存：PowerShell 5.1 对无 BOM 的中文 "
            ".ps1 退回系统 ANSI 代码页解析 ⇒ Invoke-Uninstall 一带报「字符串缺少终止符」。"
            "⛔ 这条与「文档不许 iwr|iex」是同一结构的两个条件，别为了后者删前者。",
        )

    def test_plugin_install_ps1_begins_with_utf8_bom(self):
        self.assertEqual(
            PLUGIN_INSTALL_PS1_PATH.read_bytes()[:3],
            UTF8_BOM,
            "plugin/install.ps1 同样以 UTF-8 with BOM 保存（见 .gitattributes 的说明）。",
        )


class DocumentedWindowsOneLinerIsInvocableTests(unittest.TestCase):
    """文档里 Windows 那条命令必须「按原字节落盘 → ``-File`` 执行」。

    2026-11-21 实测：``iwr ... | iex`` 在 PS 5.1 与 pwsh 7 上都装不上，
    而两边退出码都是 0 —— 静默失败。删 BOM 又会让 5.1 的 ``-File`` 崩，
    所以唯一可变的是**文档里的调用形态**，它必须被钉住。
    """

    def test_no_documented_line_pipes_the_installer_payload_into_iex(self):
        offenders = []
        for path in DOCUMENTED_INSTALL_FILES:
            for line_number, line in enumerate(read_text(path).splitlines(), start=1):
                if PIPE_TO_IEX_LINE.search(line):
                    offenders.append("%s:%d :: %s"
                                     % (path.name, line_number, line.strip()))
        self.assertEqual(
            offenders,
            [],
            "下列文档行把 install.ps1 的响应正文直接管道给了 iex —— 正文首字节是 "
            "U+FEFF，进 iex 的解析输入会让 <# 块注释失效（PS 5.1 报 ParserError、"
            "pwsh 7 静默什么都没装，两边退出码都是 0）。改成 -OutFile 落盘 + -File 执行：\n  "
            + "\n  ".join(offenders),
        )

    def test_every_raw_installer_fetch_saves_the_original_bytes(self):
        """⚠️ 这条是上一条的补集：``| Set-Content`` 之类**同样**毁 BOM，但管道目标不是 iex。

        「只禁 ``| iex``」的判据对它们静默放行（2026-11-21 反向证明的 M4 实测）。
        所以判据要问**取数那一步有没有保住字节**，而不是问「后面接的是不是 iex」。
        """
        offenders = []
        for path in DOCUMENTED_INSTALL_FILES:
            for line_number, line in enumerate(read_text(path).splitlines(), start=1):
                if not FETCH_RAW_INSTALLER_LINE.search(line):
                    continue
                if "-OutFile" in line:
                    continue
                offenders.append("%s:%d :: %s"
                                 % (path.name, line_number, line.strip()))
        self.assertEqual(
            offenders,
            [],
            "下列文档行从 raw.githubusercontent.com 取 install.ps1，却没有用 -OutFile 落盘。"
            "该文件首字节是 U+FEFF（PowerShell 5.1 读中文的承重件），任何把正文当字符串"
            "再写盘的写法都会改变/丢失它。请改成 -OutFile：\n  " + "\n  ".join(offenders),
        )

    def test_each_documented_windows_install_runs_a_downloaded_file(self):
        """只落盘不执行 = 装不上；所以给落盘命令的文档必须同时给 ``-File <路径>``。"""
        problems = []
        for path in DOCUMENTED_INSTALL_FILES:
            text = read_text(path)
            download_lines = [line for line in text.splitlines()
                               if FETCH_RAW_INSTALLER_LINE.search(line)]
            if not download_lines:
                continue
            if not any("-OutFile" in line for line in download_lines):
                problems.append("%s :: 取 install.ps1 那一行没有 -OutFile" % path.name)
            if not RUN_FILE_MODE.search(text):
                problems.append("%s :: 有落盘命令，却没有 ``powershell … -File <路径>`` 执行那一行"
                                % path.name)
        self.assertEqual(problems, [], "\n  ".join(problems))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

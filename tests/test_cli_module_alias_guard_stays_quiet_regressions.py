"""「bridge 目录必须被隔离」的**缺陷回归层 · 别名护栏的反向样本那一半**。

从 :mod:`tests.test_artifact_isolation_defect_regressions` 里拆出来的**别名那半截**
（纯结构调整，判据与用例逐字未变）。

**本文件只装一个关注点**：判据
:func:`~tests.bridge_dir_isolation_scan.cli_module_import_alias_violations`
**在该安静的地方必须安静**。

⚠️ **一个恒报的判据等于没有判据** —— 误报一出现就有人会去加豁免，于是真洞回来
（AGENTS.md §9）。这里钉的四类里前两类是**本仓库的真源码**形状
（``importlib.import_module('opencode_bridge.adapters.%s' % platform)``、
裸 ``import opencode_bridge``），第三类是**同形**的无关星号导入。

⚠️ 与它**互为反向样本**的那一半（「必须报得出来」：机械强制扫真仓库 + 三条逃逸口）
在 :mod:`tests.test_cli_module_alias_guard_reporting_regressions`
—— 那边喂「必须报」的样本，这边喂「必须不报」的真源码 ⇒ 判据既不是恒不报，也不是恒报。
⚠️ 末一条单独调
:func:`~tests.bridge_dir_isolation_scan.cli_module_binding_escape_violations`
而不是总入口，所以有人把那三条从总入口里摘掉时**这里不会**被牵连变红 ——
那正是要靠那边那些用例去抓的（否则这一条会把它们的红盖住）。

## ⛔ 拆分这道护栏的头号风险：**新文件必须以 ``test_`` 开头**

共享件 :mod:`tests.bridge_dir_isolation_scan` **不以 ``test_`` 开头** ⇒ unittest 的
discover **不收集**它 ⇒ 把用例挪进去等于**悄悄关掉护栏**，而 ``git status`` 与测试
计数都看不出异常：用例这样丢掉时，测试数只会「少了几条」，而没人知道少的是护栏。"""

from __future__ import annotations

import unittest

from tests.bridge_dir_isolation_scan import (
    CLI_MODULE_ALIAS,
    TESTS_DIR,
    cli_module_binding_escape_violations,
    cli_module_import_alias_violations,
)


class TestTheCliModuleAliasGuardStaysQuietOnWhatItMustNot(unittest.TestCase):
    """⭐⭐⭐ 反「恒红」：收紧这条护栏最容易误报的四类写法，一个都不许报。

    ⚠️ **一个恒报的判据等于没有判据** —— 误报一出现就有人会去加豁免，于是真洞
    回来（AGENTS.md §9）。这四类里前两类是**本仓库的真源码**形状。
    """

    def test_the_real_adapter_import_shape_is_not_reported(self):
        """⭐⭐⭐ ``tests/test_edit_length_guard.py`` 的**真实**写法不许报。

        ⚠️ 它是 ``importlib.import_module('opencode_bridge.adapters.%s' % platform)``
        —— 离逃逸口①**只差一个模块名**，而模块名来自变量 ⇒ 静态拼不出来 ⇒ 必须
        闭嘴。判据若把「提到了 ``opencode_bridge``」就报，这条护栏今天就已经红了
        ⇒ 而一旦有人为了让它变绿而放宽，它对真洞也就跟着瞎了。
        """
        real_shape = (
            "import importlib\n"
            "\n"
            "adapter = importlib.import_module('opencode_bridge.adapters.%s' % platform)\n"
        )
        self.assertEqual(cli_module_import_alias_violations(real_shape, "sample.py"), [])

        #: 同类的其余两个方向：与本仓库无关的模块名、只提到包名而不提 CLI 模块。
        for unrelated in (
            "import importlib\n\nm = importlib.import_module('email.message')\n",
            "import importlib\n\nm = importlib.import_module('opencode_bridge')\n",
            "import importlib\n\nm = importlib.import_module('opencode_bridge.health')\n",
        ):
            with self.subTest(unrelated_module=unrelated.splitlines()[-1]):
                self.assertEqual(
                    cli_module_import_alias_violations(unrelated, "sample.py"), [],
                    "判据把一个提到包名、但根本没导入 CLI 模块的动态导入报成了缺陷。",
                )

    def test_a_bare_package_import_that_never_touches_the_cli_module_stays_quiet(self):
        """⭐⭐⭐ 「裸 ``import opencode_bridge``」本身**不是**缺陷，碰 ``__main__`` 才是。

        ⚠️ 本仓库今天就有**三个**文件这么写（判据共享件自己、
        ``redaction_cjk_tail_support.py``、``test_redaction.py``），它们都**没有**去取
        ``.__main__`` ⇒ 判据若只看「有没有裸导入」就报，这三个文件当场变红。
        （这条要求是逐个核实过的，不是照抄结论。）

        ⚠️ 而那两个文件里**确实**出现过 ``opencode_bridge.__main__`` 这行字 ——
        **在 docstring 里**。⇒ 判据必须走 AST：扫全文会把它们全报一遍（与本模块
        「不许扫全文」那条纪律同型）。
        """
        for sample in (
            "import opencode_bridge\n\nhp = opencode_bridge.health\n",
            "import opencode_bridge as pkg\n\nhp = pkg.health\n",
            '"""这里提到 opencode_bridge.__main__，但只在文档里。"""\n'
            "import opencode_bridge\n\nhp = opencode_bridge.health\n",
            "from opencode_bridge import health\n\nhp = health\n",
            #: ⚠️ 关键的一条：``.__main__`` **属性访问本身不是缺陷** —— 判据要求那个
            #: 属性挂在「本仓库这个包的绑定名」上（:func:`~tests.bridge_dir_isolation_scan.
            #: _package_attribute_cli_module_violations` 里的 ``in package_binding_lines``）。
            #: 把那一条去掉，判据就会开始对着**任何** ``x.__main__`` 报 —— 而那是
            #: 过敏感（实测：去掉后本条立刻变红，所以它是被钉住的）。
            "import runpy\n\nmodule = runpy.__main__\n",
        ):
            with self.subTest(sample=sample.splitlines()[0][:40]):
                self.assertEqual(
                    cli_module_import_alias_violations(sample, "sample.py"), [],
                    "判据把一个根本没碰 __main__ 的裸包导入报成了缺陷 —— "
                    "误报一出现就会有人去加豁免。",
                )

    def test_a_star_import_of_another_module_stays_quiet(self):
        """⭐⭐⭐ ``from <别的模块> import *`` 与逃逸口③**同形**，但一个都不许报。

        ⚠️ 它与 ``from opencode_bridge import *`` 在 AST 上只差 ``node.module`` 那一层
        ⇒ 判据若不认这一层，仓库里任何一个 ``import *`` 都会变成缺陷（实测：把那条
        判断去掉后**零个用例变红** —— 也就是说判据当时**过敏感**，而没有任何反向用例
        拦着它）。

        ⚠️ 同理 ``from opencode_bridge import __main__ as cli, health as hp``
        （同一个包、多个名字）必须安静：它有 ``as cli`` 那个合规绑定，而 ``*`` 那条
        判据只认**字面上的星号**。
        """
        for sample in (
            "from email.parser import *\n",
            "from opencode_bridge.adapters import *\n",
            "from tests.bridge_dir_isolation_scan import *\n",
            "from opencode_bridge import __main__ as cli, health as hp\n",
        ):
            with self.subTest(sample=sample.strip()):
                self.assertEqual(
                    cli_module_import_alias_violations(sample, "sample.py"), [],
                    "判据把一个与 CLI 模块无关的星号导入报成了缺陷 —— "
                    "过敏感的判据会让人去加豁免，于是真洞回来。",
                )

    def test_the_repository_uses_no_secondary_binding_path_at_all(self):
        """⭐⭐⭐ 真仓库里那三条逃逸路**一处都没有**（判据②的现场事实，反向钉住）。

        ⚠️ 与上面那些用例是**互为反向**的一对：那边喂「必须报」的样本，这边喂
        「必须不报」的真源码 ⇒ 判据既不是恒不报，也不是恒报。

        ⚠️ 它**单独**调 :func:`~tests.bridge_dir_isolation_scan.cli_module_binding_escape_violations`
        而不是总入口，所以有人把那三条从总入口里摘掉时**这里不会**被牵连变红 ——
        那正是要靠上面那些用例去抓的（否则这一条会把它们的红盖住）。
        """
        offenders: list[str] = []
        for path in sorted(TESTS_DIR.glob("*.py")):
            offenders.extend(
                cli_module_binding_escape_violations(
                    path.read_text(encoding="utf-8"), path.name
                )
            )
        self.assertEqual(
            offenders, [],
            "这些文件把 CLI 模块绑到了判据看不见的名字上 ⇒ 判据③对它们整个失效：\n  %s\n"
            "改法只有一个：用 `from opencode_bridge import __main__ as %s`，"
            "或者压根别用 importlib 动态导它。"
            % ("\n  ".join(offenders), CLI_MODULE_ALIAS),
        )


if __name__ == "__main__":  # pragma: no cover - 手动跑这一个文件用
    unittest.main()

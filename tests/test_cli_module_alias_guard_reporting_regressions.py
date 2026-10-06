"""「bridge 目录必须被隔离」的**缺陷回归层 · 别名护栏的「报得出来」那一侧**。

从 :mod:`tests.test_artifact_isolation_defect_regressions` 里拆出来的**别名那半截**
（纯结构调整，判据与用例逐字未变）—— 那个文件原本有 698 物理行、超 §5 的 400 行线，
而它内部聚着**两拨互不相关的关注点**。

**本文件只装一个关注点**：判据
:func:`~tests.bridge_dir_isolation_scan.cli_module_import_alias_violations`
**必须报得出来** —— 分两个方向：

① **机械强制**（:class:`TestTheCliModuleAliasIsMechanicallyForced`）：扫真实仓库
   ``tests/*.py``，凡导入 ``__main__`` 的别名**恒为** ``cli``。
   ⚠️ 判据③（真源码推出来的入口名单）**整个挂在 ``cli.`` 这个前缀**上 ⇒ 换成 ``as m``
   之后 ``m._run_bridge_locked`` 就不再被认出来，而那两份产物落在 ``.gitignore`` 里
   ⇒ **没有任何测试变红**。

② **判据看得见**（:class:`TestTheCliModuleAliasGuardSeesEverySecondaryPath`）：①那道护栏
   只看 ``ast.Import`` 与 ``ast.ImportFrom``，而它的 docstring 却宣称「机械强制」
   ⇒ ``importlib.import_module`` 那条路、裸 ``import opencode_bridge`` 之后再取
   ``.__main__``、``from opencode_bridge import *`` 三条**实测各 0 violation**
   ⇒ 判据③对**整个文件**失效。
   ⇒ 方向相反的自守：钉的是「判据**看得见**」，不是钉「判据报得多」。

⚠️ **同一判据的反向样本**（该安静的地方安静：真源码形状、无关的星号导入、docstring 里
提到 ``opencode_bridge.__main__``）在
:mod:`tests.test_cli_module_alias_guard_stays_quiet_regressions`
—— ⛔ 一个恒报的判据等于没有判据，漏了那一半就等于把误报当绿灯。

⛔ 覆盖**不到**的形状（``getattr`` / ``exec`` / ``sys.modules[...]`` / 拼不出的模块名 /
跨文件传递）逐条列在
:func:`~tests.bridge_dir_isolation_scan.cli_module_binding_escape_violations`
的 docstring 里 —— ⛔ 本文件的用例**只**证明列在那里的形状被覆盖，不证明没列的那些。

## ⛔ 拆分这道护栏的头号风险：**新文件必须以 ``test_`` 开头**

共享件 :mod:`tests.bridge_dir_isolation_scan` **不以 ``test_`` 开头** ⇒ unittest 的
discover **不收集**它 ⇒ 把用例挪进去等于**悄悄关掉护栏**，而 ``git status`` 与测试
计数都看不出异常：用例这样丢掉时，测试数只会「少了几条」，而没人知道少的是护栏。"""

from __future__ import annotations

import ast
import unittest

from tests.bridge_dir_isolation_scan import (
    CLI_MODULE_ALIAS,
    CLI_MODULE_BINDING_ESCAPE_CHECKS,
    TESTS_DIR,
    cli_module_binding_escape_violations,
    cli_module_import_alias_violations,
    cli_module_import_bindings,
)


class TestTheCliModuleAliasIsMechanicallyForced(unittest.TestCase):
    """⭐⭐⭐ 绕过 B：「``__main__`` 一律以 ``cli`` 导入」不许只活在注释里。

    ⚠️ 判据③（真源码推出来的入口名单）**整个挂在 ``cli.`` 这个前缀**上 ⇒ 换成
    ``as m`` 之后 ``m._run_bridge_locked`` 就不再被认出来。本项目反复栽在
    「**注释里的约定不算约定**」上 ⇒ :func:`cli_module_import_alias_violations`
    把它变成机械强制（理由与实测见那个函数的 docstring）。
    """

    CONFORMING_MODULE = (
        "from opencode_bridge import __main__ as cli\n"
        "\n"
        "def starts_the_bridge(cfg):\n"
        "    return cli.run_bridge(cfg)\n"
    )

    def test_no_test_module_imports_the_cli_module_under_another_alias(self):
        """判据本身：扫 ``tests/*.py``，凡导入 ``__main__`` 的别名**恒为** ``cli``。"""
        offenders: list[str] = []
        for path in sorted(TESTS_DIR.glob("*.py")):
            offenders.extend(
                cli_module_import_alias_violations(
                    path.read_text(encoding="utf-8"), path.name
                )
            )
        self.assertEqual(
            offenders, [],
            "这些导入把 CLI 模块绑到了 %r 以外的名字上 ⇒ 判据③整个挂在 %r 这个前缀上，"
            "换个别名它就静悄悄失效：\n  %s\n改法只有一个："
            "from opencode_bridge import __main__ as %r。"
            % (CLI_MODULE_ALIAS, CLI_MODULE_ALIAS, "\n  ".join(offenders), CLI_MODULE_ALIAS),
        )

    def test_the_scan_really_sees_those_imports(self):
        """⭐⭐⭐ 反「恒空」：上面那条必须**看见了**导入才可信（AGENTS.md §7.1）。

        ⚠️ glob 模式写错一个字符也会让上面那条恒绿 ⇒ 这里把「扫到的条数」也钉住。
        """
        seen = 0
        for path in sorted(TESTS_DIR.glob("*.py")):
            seen += len(
                cli_module_import_bindings(path.read_text(encoding="utf-8"), path.name)
            )
        self.assertGreaterEqual(
            seen, 10,
            "tests/ 下只认出 %d 处「导入 __main__」—— 上面那条护栏恒绿（空集 ≠ 不存在）"
            % seen,
        )

    def test_an_import_under_another_alias_is_reported(self):
        """⭐⭐⭐ 自守（喂反向样本）：换个别名、或者干脆不写 ``as`` ⇒ 必须报。

        ⚠️ **不写 ``as``** 那种（绑定名变成 ``__main__`` 或包名 ``opencode_bridge``）
        同样是错 —— 只测换别名的话，删掉 ``as cli`` 就绕过了。
        """
        for source in (
            "from opencode_bridge import __main__ as m\n",
            "from opencode_bridge import __main__\n",
            "import opencode_bridge.__main__ as m\n",
            "import opencode_bridge.__main__\n",
        ):
            violations = cli_module_import_alias_violations(source, "sample.py")
            self.assertEqual(len(violations), 1, (source, violations))
            self.assertIn("恒为", violations[0])

    def test_the_conforming_import_is_not_reported(self):
        """⭐⭐ 反「恒红」：现在真仓库的写法必须**不**报（没有它，「一律都报」也绿）。"""
        self.assertEqual(
            cli_module_import_alias_violations(self.CONFORMING_MODULE, "sample.py"), [],
        )

    def test_every_binding_the_repository_makes_is_the_conforming_alias(self):
        """⭐⭐⭐ 反「恒红」**逐个**说：真仓库里认出来的每一个绑定名都必须是 ``cli``。

        ⚠️ 与 :meth:`test_no_test_module_imports_the_cli_module_under_another_alias`
        是**同一件事的两个方向**：那条钉「总体 0 违规」，这条钉「**每一个**绑定名都
        是 ``cli``」⇒ 只报违规数的话，一个「压根没认出任何绑定」的判据也能过。

        ⚠️ 这里**刻意不把 14 写成常量**：新写一个 ``as cli`` 的测试文件就会让那个
        数过期，而过期的数字会被人拿「改一下」应付。真正要钉的是「**全部**是
        ``cli``」，个数由上面那条 ``>= 10`` 的下界守住。
        """
        bound_names: list[str] = []
        for path in sorted(TESTS_DIR.glob("*.py")):
            bound_names.extend(
                name for _, name in cli_module_import_bindings(
                    path.read_text(encoding="utf-8"), path.name
                )
            )
        self.assertEqual(
            [name for name in bound_names if name != CLI_MODULE_ALIAS], [],
            "真仓库里 %d 处「导入 CLI 模块」中有 %d 处绑到了 %r 以外的名字：%r"
            % (len(bound_names),
               len([n for n in bound_names if n != CLI_MODULE_ALIAS]),
               CLI_MODULE_ALIAS,
               sorted(set(bound_names))),
        )


class TestTheCliModuleAliasGuardSeesEverySecondaryPath(unittest.TestCase):
    """⭐⭐⭐ 绕过 B 自己的洞：别名护栏**只**看两条 import 语句时的三条逃逸路。

    ⚠️ **方向相反的自守**：上面那道护栏一直是「报得出来」那一侧，而它自己的
    覆盖面从没被钉过 —— 判据③整个挂在 ``cli.`` 前缀上，而
    :func:`~tests.bridge_dir_isolation_scan.is_cli_entry_point` 第一行就把非
    ``cli.`` 前缀全部 ``return False``。⇒ 只要一个文件把 CLI 模块绑到别的名字上，
    那个文件里的**任何**间接落盘点都看不见 ⇒ 那两份产物落进**仓库根**，而它们
    的名字都在 ``.gitignore`` 里 ⇒ **没有任何测试变红**。

    **实测**：修之前这三条各 **0 violation**（不是「大概漏了」，是喂进去数出来的
    0）。⇒ 这里逐条钉「判据**看得见**它」，不是钉「判据报得多」—— 一个恒报的判据
    和一个恒不报的判据一样没用，而**恒不报的那一侧当时正是绿的**。
    """

    #: 三条逃逸路，各自带一句**源码形态**说明（用例名与这里的键一一对应）。
    ESCAPE_SAMPLES = {
        "dynamic_import_of_the_cli_module": (
            "import importlib\n"
            "\n"
            "m = importlib.import_module('opencode_bridge.__main__')\n"
        ),
        "bare_package_import_then_attribute_access": (
            "import opencode_bridge\n"
            "\n"
            "m = opencode_bridge.__main__\n"
        ),
        "star_import_of_the_package": (
            "from opencode_bridge import *\n"
        ),
    }

    #: 逃逸口① 的**其余**几种写法：模块名是拼出来 / 格式化出来的，而那条
    #: ``'opencode_bridge.adapters.%s' % platform`` 证明「静态拼不出来」时必须闭嘴。
    #:
    #: ⚠️ 刻意**不放进** :attr:`ESCAPE_SAMPLES`：那张表要满足「每条判据只对自己的
    #: 那一条路有反应」，多塞几个同属逃逸口①的变体会把那个不变量撑破。
    DYNAMIC_MODULE_NAME_VARIANTS = (
        #: 裸字面量（最现实的那一种）。
        "import importlib\n\nm = importlib.import_module('opencode_bridge.__main__')\n",
        #: ``from importlib import import_module`` ⇒ 被调方的全名只剩末段。
        "from importlib import import_module\n\nm = import_module('opencode_bridge.__main__')\n",
        #: ``+`` 拼接。
        "import importlib\n\nm = importlib.import_module('opencode_bridge.' + '__main__')\n",
        #: f-string（**没有**占位符时，CPython 也把它解析成 ``JoinedStr``）。
        "import importlib\n\nm = importlib.import_module(f'opencode_bridge.__main__')\n",
        #: ``%`` 格式化，且**右操作数静态已知**。
        #: ⚠️ 这条钉的是一个踩过的坑：``%s`` 的尾巴会贴住 ``__main__``，于是按**词
        #: 边界**匹配的判据把它当成同一个词而漏掉（把转换说明符换成占位符才修好）。
        "import importlib\n\nm = importlib.import_module('opencode_bridge.%s' % '__main__')\n",
    )

    def test_each_secondary_binding_path_is_reported(self):
        """⭐⭐⭐ 三条逃逸路**各自**都必须是「报得出来」的那一侧。"""
        for label, sample in self.ESCAPE_SAMPLES.items():
            with self.subTest(escape_path=label):
                violations = cli_module_import_alias_violations(sample, "sample.py")
                self.assertEqual(
                    len(violations), 1,
                    "这条逃逸路没被报出来（%s）—— 判据③于是对整个文件失效，"
                    "而产物名在 .gitignore 里 ⇒ 污染全程无声。源码：\n%s"
                    % (label, sample),
                )

    def test_every_static_shape_of_a_dynamic_module_name_is_reported(self):
        """⭐⭐⭐ 「模块名是**拼出来**的」那几种写法，**每一种**都得报。

        ⚠️ 这是上面那条**补上的一处漏**：那个变体表刚加上时，实测有一个变异让
        ``RED=0`` —— ``statically_known_module_name_text`` 里的 ``%`` 分支被短路
        掉之后**没有任何用例变红** ⇒ 也就是说，那条分支是「写出来就算数」的，
        没人钉着它。⇒ 本条把 ``Constant`` / ``JoinedStr`` / ``+`` 拼接 / ``%``
        四个分支各自钉住。

        ⚠️ 「静态不可知」的那些写法**不在**这张表里，而且**必须不报**（另一半见
        :meth:`TestTheCliModuleAliasGuardStaysQuietOnWhatItMustNot.test_the_real_adapter_import_shape_is_not_reported`）
        —— 一个恒报的判据等于没有判据。
        """
        for sample in self.DYNAMIC_MODULE_NAME_VARIANTS:
            with self.subTest(sample=sample.splitlines()[-1].strip()):
                violations = cli_module_import_alias_violations(sample, "sample.py")
                self.assertEqual(
                    len(violations), 1,
                    "这种「拼出来的模块名」没被报出来 —— 而它就是逃逸口①本身。"
                    "源码：\n%s" % sample,
                )

    def test_each_criterion_is_pinned_on_its_own_path_alone(self):
        """⭐⭐⭐ **每一条判据**必须只对自己的那条路有反应（结构性自守）。

        ⚠️ 为什么必须**逐条**钉，而不是只钉「三条合起来报出来」：把
        :data:`~tests.bridge_dir_isolation_scan.CLI_MODULE_BINDING_ESCAPE_CHECKS`
        里某一条换成另一个（或者让三条判据其实**是同一个函数的三份拷贝**）时，
        「合起来能报」照样绿 ⇒ 三条逃逸路里少了两条也没人知道。

        ⚠️ 方向也是反的：这里断言的是「**该响的响、不该响的不响**」，所以它同时
        挡住「一条判据顺手把所有形状都报了」这种**过度敏感**的写法。
        """
        parsed = {
            label: ast.parse(sample, filename="sample.py")
            for label, sample in self.ESCAPE_SAMPLES.items()
        }
        self.assertEqual(
            len(CLI_MODULE_BINDING_ESCAPE_CHECKS), len(self.ESCAPE_SAMPLES),
            "逃逸口判据与用例对不上（判据 %d 条、用例 %d 条）—— 少一条就是少堵一个洞，"
            "多一条则说明这里该有对应的反向用例。"
            % (len(CLI_MODULE_BINDING_ESCAPE_CHECKS), len(self.ESCAPE_SAMPLES)),
        )
        for criterion in CLI_MODULE_BINDING_ESCAPE_CHECKS:
            fired_on = sorted(
                label for label, tree in parsed.items()
                if criterion(tree, "sample.py")
            )
            self.assertEqual(
                len(fired_on), 1,
                "%s 对 %d 条逃逸路有反应（%r）—— 每条判据必须只管自己那一条。"
                % (criterion.__name__, len(fired_on), fired_on),
            )

    def test_all_three_paths_in_one_file_are_reported_separately(self):
        """⭐⭐ 三条路**同时**出现在一个文件里 ⇒ 三条都要报，且**互不吞掉**。

        ⚠️ 它钉的是**并集**：判据要么只报第一条命中的（``return`` 写错位置），
        要么把三条并成一条重复报 ⇒ 两种都跑不掉。
        """
        all_three_at_once = (
            "import importlib\n"
            "import opencode_bridge\n"
            "from opencode_bridge import *\n"
            "\n"
            "first = importlib.import_module('opencode_bridge.__main__')\n"
            "second = opencode_bridge.__main__\n"
        )
        violations = cli_module_binding_escape_violations(all_three_at_once, "sample.py")
        self.assertEqual(len(violations), 3, violations)
        #: 三个行号各自对应一件事：``import *`` 那**一句**、``import_module`` 那个
        #: **调用点**、``.__main__`` 那个**属性访问**。⚠️ 行号是刻意钉的：判据哪天
        #: 改用「报在 import 语句那一行」或「报在文件头上」，这里会红。
        self.assertEqual(
            [violation.split(":")[1] for violation in violations],
            ["3", "5", "6"],
            "报出来的行号与三条逃逸路各自的位置对不上 —— 有一条报错了行或被并掉了。",
        )


if __name__ == "__main__":  # pragma: no cover - 手动跑这一个文件用
    unittest.main()

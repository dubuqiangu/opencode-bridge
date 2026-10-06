"""「bridge 目录必须被隔离」的**缺陷回归层**：已经实测出来的那几个洞，逐个钉住。

从 :mod:`tests.test_artifact_isolation_contract` 里拆出来的**后半截**（纯结构调整，
判据与用例逐字未变）—— 那个文件连同它的用例原本会超 §5 的 400 物理行。

**已实测的缺陷**：入口判定（缺陷 3，真写产物的那层曾经整个漏在名字表外）与环境变量
pin（缺陷 4，只看「设了没有」时对六种写法恒不报）。**本轮新堵的两条绕过**：判据自己
被绕过的两个逃逸口 —— 一个的结论曾由**源码顺序**决定（绕过 A），另一个挂在一条**只
活在注释里**的别名约定上（绕过 B）。

**再往下一层的洞（也堵上了）**：绕过 B 那道护栏自己**只**看 ``ast.Import`` 与
``ast.ImportFrom``，而它的 docstring 却宣称「机械强制」⇒ ``importlib.import_module``
那条路、裸 ``import opencode_bridge`` 之后再取 ``.__main__``、``from opencode_bridge
import *`` 三条**实测各 0 violation** ⇒ 判据③对**整个文件**失效。⇒ 见
:class:`TestTheCliModuleAliasGuardSeesEverySecondaryPath`（**方向相反**的自守：钉的是
「判据**看得见**」）与 :class:`TestTheCliModuleAliasGuardStaysQuietOnWhatItMustNot`。
⛔ 覆盖**不到**的形状（``getattr`` / ``exec`` / ``sys.modules[...]`` / 拼不出的模块名 /
跨文件传递）逐条列在 :func:`~tests.bridge_dir_isolation_scan.cli_module_binding_escape_violations`
的 docstring 里 —— ⛔ 本文件的用例**只**证明列在那里的形状被覆盖，不证明没列的那些。

⚠️ 每个判据都配了「必须报 / 必须不报」**互为反向样本**的一对：只钉「会报错」的判据
早晚会被人拿 :func:`mock.patch.dict` 之类把范围放宽成整个文件（真发生过一次，见
:func:`~tests.bridge_dir_isolation_scan.patched_bridge_dir`）。

## ⛔ 拆分这道护栏的头号风险：**新文件必须以 ``test_`` 开头**

共享件 :mod:`tests.bridge_dir_isolation_scan` **不以 ``test_`` 开头** ⇒ unittest 的
discover **不收集**它 ⇒ 把用例挪进去等于**悄悄关掉护栏**，而 ``git status`` 与测试
计数都看不出异常：用例这样丢掉时，测试数只会「少了几条」，而没人知道少的是护栏。
"""

from __future__ import annotations

import ast
import unittest

from tests.bridge_dir_isolation_scan import (
    CLI_ENTRY_POINT_EXACT_NAMES,
    CLI_ENTRY_POINT_NAME_FRAGMENT,
    CLI_MODULE_ALIAS,
    CLI_MODULE_BINDING_ESCAPE_CHECKS,
    CLI_TOP_LEVEL_FUNCTION,
    TESTS_DIR,
    cli_module_binding_escape_violations,
    cli_module_import_alias_violations,
    cli_module_import_bindings,
    is_cli_entry_point,
    production_artifact_writing_entry_points,
    unisolated_call_sites,
)


class TestTheEntryPointRuleRecognizesEveryWritingLayer(unittest.TestCase):
    """⭐⭐⭐ 缺陷 3：真的写产物的那一层，规则必须认得出来 —— 且认靠**规则**不靠名字表。"""

    def test_the_check_sees_the_delegated_startup_entry_point(self):
        """⭐⭐ 缺陷 3：直接调 ``cli._run_bridge_locked(cfg)`` 且没隔离 ⇒ **必须**报。

        ⚠️ 它才是真正写那两份文件的那层（``run_bridge`` 只把活委托下去；装配记录器
        与落盘探测结论都在它里面），而 :mod:`tests.test_outbound_failure_channel`
        里那个 ``BRIDGE_STARTUP_FUNCTION`` **已经**把它当「真实启动路径」在钉
        ⇒ 一条测「真实启动路径」的用例直接调它是相当自然的一步。
        **实测过**：只认 ``{run_bridge, main}`` 的时候这一条**恒不报**。
        """
        sample = (
            "class Sample:\n"
            "    def starts_the_bridge_for_real(self, cfg) -> int:\n"
            "        return cli._run_bridge_locked(cfg)\n"
        )
        offenders = unisolated_call_sites(sample, "sample.py")
        self.assertEqual(len(offenders), 1, offenders)
        self.assertIn("cli._run_bridge_locked", offenders[0])
        self.assertIn("Sample.starts_the_bridge_for_real", offenders[0])

        #: ⭐ 反「恒红」：同样这一条，钉在临时目录上以后必须**不**报 ——
        #: 一个恒报的判据和一个恒不报的判据一样没用。
        isolated = (
            "class Sample:\n"
            "    def starts_the_bridge_for_real(self, cfg) -> int:\n"
            "        with mock.patch.object(cli, '_bridge_dir', lambda: self.bridge_dir):\n"
            "            return cli._run_bridge_locked(cfg)\n"
        )
        self.assertEqual(unisolated_call_sites(isolated, "sample.py"), [])

    def test_the_entry_point_rule_is_not_a_name_table(self):
        """⭐⭐⭐ 自守：判据认入口靠的是**规则**，不是一张写死的名字表。

        ⚠️ 三个方向各钉一个：
        ① ``run_bridge`` 这个**词**出现在名字里 ⇒ 同一族里将来新增的委托层
           （``run_bridge_in_pool`` 那种）不用改判据也照样被认出来；
        ② 别的 ``.main()``（``threading.main``）与本护栏无关，不许报；
        ③ ``__main__.py`` 里压根没有的那个名字也不算 —— 否则护栏会开始对着
           一堆查不到的文件名报「没隔离」，误报一出现就有人会去加豁免。
        """
        invented = (
            "class Sample:\n"
            "    def run(self, cfg) -> int:\n"
            "        return cli.run_bridge_in_pool(cfg)\n"
        )
        offenders = unisolated_call_sites(invented, "sample.py")
        self.assertEqual(len(offenders), 1, offenders)
        self.assertIn("cli.run_bridge_in_pool", offenders[0])

        unrelated = (
            "class Sample:\n"
            "    def run(self) -> int:\n"
            "        threading.main()\n"
            "        app.main(argv)\n"
            "        cli.maintenance_loop()\n"
        )
        self.assertEqual(
            unisolated_call_sites(unrelated, "sample.py"), [],
            "判据把跟本仓库 CLI 无关的调用也当成了落盘入口。",
        )

    def test_the_entry_point_rule_covers_every_writing_layer_in_the_real_source(self):
        """⭐⭐⭐ 自守：**真源码**里每一层会写产物的入口，规则都必须认出来。

        ⚠️ 这条是「名字规则认不出来时还有真源码兜底」那半边的保险：
        :func:`~tests.bridge_dir_isolation_scan.production_artifact_writing_entry_points`
        一旦退化成空集（源码读不到 / 顶层函数改名），这里会**红**而不是让判据悄悄瞎掉
        （AGENTS.md §7.1：空集 ≠ 不存在）。
        """
        writing_layers = production_artifact_writing_entry_points()
        self.assertGreaterEqual(
            len(writing_layers), 2,
            "从真源码推出的落盘层只有 %d 个 —— 生产源码读不到，或 %s 被改名了。"
            "那一刻判据已经瞎了，却会一直绿。" % (len(writing_layers), CLI_TOP_LEVEL_FUNCTION),
        )
        for name in sorted(writing_layers):
            self.assertTrue(
                is_cli_entry_point(CLI_MODULE_ALIAS + "." + name),
                "%s 会写运行期产物，判据却认不出 %s.%s ⇒ 新增一层委托就会漏。"
                % (name, CLI_MODULE_ALIAS, name),
            )
        #: ⛔ 反「规则悄悄退回成名字表」：必须至少有一层是**名字规则认不出**的 ——
        #: 全都认得出的话，上面那段可能只是名字表的回光返照。
        self.assertTrue(
            [
                name for name in sorted(writing_layers)
                if name not in CLI_ENTRY_POINT_EXACT_NAMES
                and CLI_ENTRY_POINT_NAME_FRAGMENT not in name
            ],
            "真源码里那些「名字规则认不出」的落盘层一个都没有 —— 判据可能已经退回成"
            "一张写死的名字表（它最初就是那样，_run_bridge_locked 曾整个漏在外面）。",
        )


class TestAnEnvPinIsJudgedByItsValue(unittest.TestCase):
    """⭐⭐⭐ 缺陷 4：``OPENCODE_BRIDGE_CONFIG`` 的**值**才是判据，不是「设了没有」。"""

    def test_an_env_pin_pointing_back_at_the_repository_root_is_not_isolation(self):
        """⭐⭐⭐ 缺陷 4：``OPENCODE_BRIDGE_CONFIG`` 的**值**指向仓库根 ⇒ 必须报出来。

        ⚠️ 与「补丁替换值指回 ``os.getcwd()``」**完全同型**，而那条路早修好了：只看
        「设了没有」的话，下游 :func:`opencode_bridge.__main__._bridge_dir` 在那个变量
        指向一个**存在的文件**时返回的正是那个文件所在的目录 ⇒ 指向仓库根就是仓库根。
        **实测过**：旧判据对下面六种写法**恒不报**。
        """

        def offenders_for(setting: str) -> list[str]:
            return unisolated_call_sites(
                "class Sample:\n"
                "    def setUp(self) -> None:\n"
                "%s"
                "\n"
                "    def starts_the_bridge(self, cfg) -> int:\n"
                "        return cli.run_bridge(cfg)\n" % setting,
                "sample.py",
            )

        for setting, blames_the_value in (
            (
                "        os.environ['OPENCODE_BRIDGE_CONFIG'] = os.path.join(\n"
                "            repo, 'config.json')\n", True,
            ),
            (
                "        os.environ['OPENCODE_BRIDGE_CONFIG'] = os.path.join(\n"
                "            _REPOSITORY_ROOT, 'config.json')\n", True,
            ),
            ("        os.environ['OPENCODE_BRIDGE_CONFIG'] = os.getcwd()\n", True),
            (
                "        os.environ.setdefault(\n"
                "            'OPENCODE_BRIDGE_CONFIG', os.path.join(repo, 'config.json'))\n", True,
            ),
            #: 只给键不给值 ⇒ 保留原值，而原值指向哪里与这层隔离无关 ⇒ 不算隔离
            #: （与「patch 了但没给替换值」同一条纪律）。
            ("        os.environ.setdefault('OPENCODE_BRIDGE_CONFIG')\n", False),
            (
                "        with mock.patch.dict(\n"
                "            os.environ, {'OPENCODE_BRIDGE_CONFIG': os.path.join(repo, 'c.json')}\n"
                "        ):\n"
                "            pass\n", True,
            ),
        ):
            offenders = offenders_for(setting)
            self.assertEqual(len(offenders), 1, (setting, offenders))
            self.assertIn("OPENCODE_BRIDGE_CONFIG", offenders[0])
            if blames_the_value:
                self.assertIn("设了", offenders[0])

    def test_an_env_pin_pointing_at_a_temporary_directory_stays_quiet(self):
        """⭐⭐ 反「恒红」：值指向临时目录的环境变量**仍然是有效隔离**，不许报。

        ⚠️ 本仓库三个隔离基类里两个用的就是这条路（``config_path`` 那个写法）⇒
        收紧判据时最容易在这里误报，一误报就有人会把真洞那条判据又放宽回去。
        """
        for setting in (
            "        os.environ['OPENCODE_BRIDGE_CONFIG'] = os.path.join(\n"
            "            self.bridge_dir, 'config.json')\n",
            "        os.environ['OPENCODE_BRIDGE_CONFIG'] = config_path\n",
            "        os.environ['OPENCODE_BRIDGE_CONFIG'] = str(\n"
            "            pathlib.Path(directory) / 'config.json')\n",
            "        os.environ.setdefault('OPENCODE_BRIDGE_CONFIG', self.temp_config_path)\n",
            "        with mock.patch.dict(\n"
            "            os.environ, {'OPENCODE_BRIDGE_CONFIG': str(self.config_path)}\n"
            "        ):\n"
            "            pass\n",
        ):
            sample = (
                "class Sample:\n"
                "    def setUp(self) -> None:\n"
                "%s"
                "\n"
                "    def starts_the_bridge(self, cfg) -> int:\n"
                "        return cli.run_bridge(cfg)\n" % setting
            )
            self.assertEqual(unisolated_call_sites(sample, "sample.py"), [], setting)

        #: ⭐ **patch 优先于环境变量**：``_bridge_dir`` 被 patch 住时它根本不读那个
        #: 环境变量 ⇒ 那时再看环境变量就是误报（类里 patch 到临时目录、而进程里本来
        #: 就带着仓库根的环境变量，是本仓库常见的形状）。
        patch_wins = (
            "class Sample:\n"
            "    def setUp(self) -> None:\n"
            "        os.environ['OPENCODE_BRIDGE_CONFIG'] = os.path.join(repo, 'config.json')\n"
            "        self.patcher = mock.patch.object(\n"
            "            cli, '_bridge_dir', lambda: self.bridge_dir)\n"
            "        self.patcher.start()\n"
            "\n"
            "    def starts_the_bridge(self, cfg) -> int:\n"
            "        return cli.run_bridge(cfg)\n"
        )
        self.assertEqual(
            unisolated_call_sites(patch_wins, "sample.py"), [],
            "_bridge_dir 已经 patch 到临时目录了，环境变量那条不许再报。",
        )


class TestEveryBridgeDirPinInTheScopeIsJudged(unittest.TestCase):
    """⭐⭐⭐ 绕过 A：一个作用域里**若干条** pin 的结论必须合起来算。

    ⚠️ **这是「打败这道护栏最自然的形状」**：:func:`mock.patch` 的样板天生是「先在
    ``setUp`` 钉好、后来某处再改回去」。实测判据原先在
    :func:`~tests.bridge_dir_isolation_scan.patched_bridge_dir` 里**命中第一个 pin
    就返回** ⇒ 结论**由源码顺序决定**：先 ``lambda: self.bridge_dir``、后
    ``lambda: os.getcwd()`` **不报**（洞），把两个顺序对调才报。

    ⇒ 两个方向各钉一条，且「只有一条好 pin」那条钉住反恒红。
    ⛔ 只堵住一个方向，另一个立刻变成同样的洞 —— 所以两个方向都必须有用例。
    """

    SAMPLE_WITH_TWO_PATCHES = (
        "class Sample:\n"
        "    def setUp(self) -> None:\n"
        "%s"
        "\n"
        "    def starts_the_bridge(self, cfg) -> int:\n"
        "        return cli.run_bridge(cfg)\n"
    )

    #: 钉到临时目录（**有效**隔离）。
    PIN_ONTO_THE_TEMPORARY_DIRECTORY = (
        "        self.patcher = mock.patch.object(\n"
        "            cli, '_bridge_dir', lambda: self.bridge_dir)\n"
        "        self.patcher.start()\n"
    )
    #: 钉回 cwd（**看着钉了、实际没钉住**）。
    PIN_BACK_ONTO_THE_CWD = (
        "        self.patcher = mock.patch.object(\n"
        "            cli, '_bridge_dir', lambda: os.getcwd())\n"
        "        self.patcher.start()\n"
    )
    #: patch 了但**不给替换值**（同样没钉住：原函数还在）。
    PIN_WITHOUT_A_REPLACEMENT = (
        "        self.patcher = mock.patch.object(cli, '_bridge_dir')\n"
        "        self.patcher.start()\n"
    )

    def _offenders_for(self, set_up_body: str) -> list[str]:
        return unisolated_call_sites(self.SAMPLE_WITH_TWO_PATCHES % set_up_body, "sample.py")

    def test_pinning_first_and_reverting_to_the_cwd_later_is_still_a_defect(self):
        """⭐⭐⭐ 绕过 A 的正方向：先钉好、后改回 cwd ⇒ 必须报。"""
        offenders = self._offenders_for(
            self.PIN_ONTO_THE_TEMPORARY_DIRECTORY + self.PIN_BACK_ONTO_THE_CWD
        )
        self.assertEqual(len(offenders), 1, offenders)
        self.assertIn("os.getcwd()", offenders[0])

    def test_reverting_to_the_cwd_first_and_pinning_later_is_also_a_defect(self):
        """⭐⭐⭐ 绕过 A 的反方向：先改回 cwd、后钉好 ⇒ **同样**必须报（旧判据下也报）。

        ⚠️ 它钉的是「两个方向**等价**」，防止将来只修好一个方向、另一个又变成洞。
        """
        offenders = self._offenders_for(
            self.PIN_BACK_ONTO_THE_CWD + self.PIN_ONTO_THE_TEMPORARY_DIRECTORY
        )
        self.assertEqual(len(offenders), 1, offenders)
        self.assertIn("os.getcwd()", offenders[0])

    def test_pinning_first_then_patching_without_a_replacement_is_still_a_defect(self):
        """⭐⭐⭐ 绕过 A 的另一个形状：后一条**没给替换值** ⇒ 同样必须报。

        ⚠️ 这个形状比「后一条改成 ``os.getcwd()``」更隐蔽：后者一眼就看见仓库根。
        """
        offenders = self._offenders_for(
            self.PIN_ONTO_THE_TEMPORARY_DIRECTORY + self.PIN_WITHOUT_A_REPLACEMENT
        )
        self.assertEqual(len(offenders), 1, offenders)
        self.assertIn("没给替换值", offenders[0])

    def test_a_lone_pin_onto_the_temporary_directory_still_silences_the_scan(self):
        """⭐⭐ 反「恒红」＋反「恒空」：只有一条**有效** pin 时必须不报（它是上面几条的对照）。"""
        self.assertEqual(
            self._offenders_for(self.PIN_ONTO_THE_TEMPORARY_DIRECTORY), [],
            "只有一条钉到临时目录的 pin —— 判据必须闭嘴（补丁样板是这么写的）。",
        )

    def test_a_lone_pin_without_a_replacement_value_is_still_a_defect(self):
        """⭐⭐ 对照：只有一条**没钉住**的 pin 时必须报（旧判据下也报，钉住不退化）。"""
        offenders = self._offenders_for(self.PIN_WITHOUT_A_REPLACEMENT)
        self.assertEqual(len(offenders), 1, offenders)
        self.assertIn("没给替换值", offenders[0])


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
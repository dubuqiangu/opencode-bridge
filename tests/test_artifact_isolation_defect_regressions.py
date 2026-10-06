"""「bridge 目录必须被隔离」的**缺陷回归层 · 落盘点那一半**：已经实测出来的那几个洞，逐个钉住。

从 :mod:`tests.test_artifact_isolation_contract` 里拆出来的**后半截**（纯结构调整，
判据与用例逐字未变）—— 那个文件连同它的用例原本会超 §5 的 400 物理行。

**本文件只装一个判据族**：:func:`~tests.bridge_dir_isolation_scan.unisolated_call_sites`
（以及它认入口用的 :func:`~tests.bridge_dir_isolation_scan.is_cli_entry_point`）——
即「这一处落盘调用点所在的函数/类里到底有没有被隔离」。

**已实测的缺陷**：入口判定（缺陷 3，真写产物的那层曾经整个漏在名字表外）与环境变量
pin（缺陷 4，只看「设了没有」时对六种写法恒不报）。**本轮新堵的一条绕过**：判据自己
被绕过的那个逃逸口 —— 一个作用域里**若干条** pin 时，结论曾由**源码顺序**决定
（绕过 A）。

⚠️ 另一个判据族（「``__main__`` 一律以 ``cli`` 导入」不许只活在注释里，绕过 B）
在 :mod:`tests.test_cli_module_alias_guard_reporting_regressions` 与
:mod:`tests.test_cli_module_alias_guard_stays_quiet_regressions`
—— 按「测哪个判据」拆开，⛔ 不按类拆、不按行数对半。

⚠️ 每个判据都配了「必须报 / 必须不报」**互为反向样本**的一对：只钉「会报错」的判据
早晚会被人拿 :func:`mock.patch.dict` 之类把范围放宽成整个文件（真发生过一次，见
:func:`~tests.bridge_dir_isolation_scan.patched_bridge_dir`）。

## ⛔ 拆分这道护栏的头号风险：**新文件必须以 ``test_`` 开头**

共享件 :mod:`tests.bridge_dir_isolation_scan` **不以 ``test_`` 开头** ⇒ unittest 的
discover **不收集**它 ⇒ 把用例挪进去等于**悄悄关掉护栏**，而 ``git status`` 与测试
计数都看不出异常：用例这样丢掉时，测试数只会「少了几条」，而没人知道少的是护栏。"""

from __future__ import annotations

import unittest

from tests.bridge_dir_isolation_scan import (
    CLI_ENTRY_POINT_EXACT_NAMES,
    CLI_ENTRY_POINT_NAME_FRAGMENT,
    CLI_MODULE_ALIAS,
    CLI_TOP_LEVEL_FUNCTION,
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


if __name__ == "__main__":  # pragma: no cover - 手动跑这一个文件用
    unittest.main()

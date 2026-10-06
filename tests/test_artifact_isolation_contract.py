"""「bridge 目录必须被隔离」的**源头层**结构断言（扫 ``tests/*.py``）。

钉住的缺陷（实测，不是推断）
============================

``platform-health.json`` 与 ``outbound-failures.json`` 是
:mod:`opencode_bridge.health` 的两份**运行期**记录，落在
``__main__._bridge_dir()`` 推导出来的那个目录里。而那个推导在没有
``OPENCODE_BRIDGE_CONFIG`` 时**退回 cwd** —— 跑测试时 cwd 就是仓库根。

⚠️ **它们在 ``.gitignore`` 里** ⇒ 提交时被挡住、``git status`` 干净 ⇒ 缺陷可以
长期存活而不被任何人看见（**空集 ≠ 不存在**：看不见不等于没发生）。

已修的历史现场（本任务之前）：``tests/test_conversation_id_cutover.py`` 的
``CliOpensKeyMigration._run_bridge``、``tests/test_inbox_wiring.py`` 的
``CliWiresTheInbox``、``tests/test_core.py::CliTests`` 的两条 ``cli.main`` 用例。

**为什么症状层（a）不够**：摘掉 :class:`~tests.test_inbox_wiring.CliWiresTheInbox`
那处隔离时，仓库根**仍然干净** —— 那条路只**装配**记录器，从不记录失败 ⇒
症状层抓不到它。本层抓得到。症状层在 :mod:`tests.test_repository_root_artifacts`。

扫描逻辑与契约常量在 :mod:`tests.bridge_dir_isolation_scan`（共享件，不被 discover 收集）。
"""

from __future__ import annotations

import ast
import pathlib
import unittest

from opencode_bridge import health
from tests.bridge_dir_isolation_scan import (
    ARTIFACT_WRITING_CALLS,
    CLI_ENTRY_POINT_EXACT_NAMES,
    CLI_ENTRY_POINT_NAME_FRAGMENT,
    CLI_TOP_LEVEL_FUNCTION,
    RUNTIME_ARTIFACT_NAMES,
    TESTS_DIR,
    artifact_call_sites,
    enclosing_function_and_class,
    is_cli_entry_point,
    parent_map,
    production_artifact_writing_entry_points,
    unisolated_call_sites,
)


class TestEveryArtifactWritingCallSiteIsIsolated(unittest.TestCase):
    """每个落盘调用点**所在的函数或类里**都有隔离措施。

    这是**结构**断言：它不点名某个用例，所以下次有人新写一处不隔离的落盘点
    （跨行、包在 ``with`` 里都算）会被当场抓住，而不是等它把仓库根弄脏。

    ⚠️ **AST，不是行级正则**（:mod:`tests.test_config_coerce` 那条纪律）：
    ``mock.patch.object(\n    cli, "_bridge_dir", lambda: directory\n)`` 本来就跨行。

    ⚠️ **豁免名单刻意是空的**：见 :attr:`KNOWN_UNEXEMPTED` 的说明。
    """

    def _offenders(self) -> list[str]:
        found: list[str] = []
        for path in sorted(TESTS_DIR.glob("test_*.py")):
            found.extend(
                unisolated_call_sites(path.read_text(encoding="utf-8"), path.name)
            )
        return found

    #: 已知的、**豁免**的落盘点。⛔ **刻意是空的**。
    #:
    #: 豁免本身是合理的 —— 专门测「不隔离会怎样」的用例（那条路必须真的写到
    #: ``_bridge_dir()`` 才对）**必须**豁免。但那种用例本仓库目前**一个都没有**，
    #: 而一张有理由的表很容易被当成垃圾桶：新写的用例嫌麻烦就往里加一条，
    #: 于是「豁免」变成「没人再看的黑名单」（这正是
    #: :attr:`tests.test_config_coerce.TestNoBareNumericCoercionInAdapterLifecycles.KNOWN_OUT_OF_SCOPE`
    #: 记的那种退化 —— 它当初也是从「一张表」变成「必须为空」的）。
    #:
    #: ⇒ 下一个真正需要豁免的用例进来时，请把 ``(文件名, 行号)`` 与
    #: **「它为什么必须真的写到未隔离的目录」** 一起写进来。
    KNOWN_UNEXEMPTED: tuple = ()

    def test_no_test_module_writes_a_runtime_artifact_into_the_repository_root(self):
        """⚠️ 判据本身（外加一张**刻意为空**的已知表 —— 见 :attr:`KNOWN_UNEXEMPTED`）。"""
        offenders = self._offenders()
        self.assertEqual(
            sorted(offenders), sorted(self.KNOWN_UNEXEMPTED),
            "这些调用点会把 %s 写进 cwd（跑测试时就是仓库根）：\n  %s\n"
            "修法只有一个：把 bridge 目录钉在临时目录上 —— "
            "mock.patch.object(cli, '_bridge_dir', lambda: <临时目录>) 或设 "
            "OPENCODE_BRIDGE_CONFIG（手法抄 tests/test_platform_health.py 的 "
            "_BridgeDirIsolated）。⛔ 不许用 os.chdir：它会影响同进程里后续所有用例。\n"
            "若确有「专门测不隔离会怎样」的用例，请把它连同理由写进 KNOWN_UNEXEMPTED。"
            % (list(RUNTIME_ARTIFACT_NAMES), "\n  ".join(offenders)),
        )

    def test_the_check_sees_a_direct_call_with_an_unpinned_bridge_dir(self):
        """⭐ 反「恒空」：喂一段**不隔离**的直调样本，要求判据抓到它。

        ⚠️ 没有这条，一个「只认 ``mock.patch``」的判据会恒空地通过 —— 而那正是
        「把 ``_bridge_dir`` 补丁删掉」这种变异的逃逸口。
        """
        sample = (
            "class Sample:\n"
            "    def writes_without_isolation(self) -> None:\n"
            "        health.record_startup_probes(\n"
            "            cli._bridge_dir(),\n"
            "            {'telegram': {'verdict': 'ok'}},\n"
            "        )\n"
        )
        offenders = unisolated_call_sites(sample, "sample.py")
        self.assertEqual(len(offenders), 1, offenders)
        self.assertIn("Sample.writes_without_isolation", offenders[0])
        #: 行号取**调用表达式**自己的 ``lineno``（Python 3.8+ 起就是 ``func`` 那一行，
        #: 不是实参那一行）—— 把它写死是刻意的：判据哪天改成取别的位置，这里会红。
        self.assertIn("sample.py:3", offenders[0])

    def test_a_patch_that_still_points_at_the_cwd_is_not_isolation(self):
        """⭐⭐ 这条是**实测出来的洞**：补丁在，缺陷照样在。

        ⚠️ ``mock.patch.object(cli, "_bridge_dir", lambda: os.getcwd())`` 结构上
        「钉了 ``_bridge_dir``」，而它把目录**指回仓库根** ⇒ 判据若只看「补丁在不
        在」，这个变异会**恒绿**（实测过：那时只有症状层抓到它）。所以
        :func:`~tests.bridge_dir_isolation_scan.patched_bridge_dir` 必须读替换值。
        """
        sample = (
            "class Sample:\n"
            "    def run(self) -> int:\n"
            "        with mock.patch.object(\n"
            "            cli, '_bridge_dir', lambda: os.getcwd()\n"
            "        ):\n"
            "            return cli.run_bridge(cfg)\n"
        )
        offenders = unisolated_call_sites(sample, "sample.py")
        self.assertEqual(len(offenders), 1, offenders)
        self.assertIn("os.getcwd()", offenders[0])

        #: 同一个洞的另一面：``patch.object`` **不带替换值** ⇒ 原函数没被换掉。
        bare_patch = (
            "class Sample:\n"
            "    def run(self) -> int:\n"
            "        with mock.patch.object(cli, '_bridge_dir'):\n"
            "            return cli.run_bridge(cfg)\n"
        )
        self.assertEqual(
            len(unisolated_call_sites(bare_patch, "sample.py")), 1,
            "不带替换值的 patch 等于没隔离 —— 判据必须报它。",
        )

    def test_the_check_sees_a_cli_entry_point_across_several_lines(self):
        """⭐ 反退化：判据必须看得见**跨行**的 ``cli.main([...])``，且没隔离就报。

        ⚠️ 本仓库已经因为「只认同一行」误判过一次（AGENTS.md §7.1 的行级正则那条）。
        """
        sample = (
            "class Sample:\n"
            "    def run(self) -> int:\n"
            "        return cli.main(\n"
            "            ['--config',\n"
            "             path]\n"
            "        )\n"
        )
        offenders = unisolated_call_sites(sample, "sample.py")
        self.assertEqual(len(offenders), 1, offenders)
        self.assertIn("Sample.run", offenders[0])

    def test_a_patched_bridge_dir_silences_the_same_sample(self):
        """⭐ 反「恒红」：同一段样本加上隔离措施后，判据必须**不再**报它。

        ⚠️ 只测「会报错」不测「不误报」的判据，早晚会被人拿 :func:`mock.patch.dict`
        之类把范围放宽到整个文件 —— 那就等于没有判据。
        """
        isolated = (
            "class Sample:\n"
            "    def run(self) -> int:\n"
            "        with mock.patch.object(\n"
            "            cli, '_bridge_dir', lambda: directory\n"
            "        ):\n"
            "            return cli.main(['--config', path])\n"
        )
        self.assertEqual(unisolated_call_sites(isolated, "sample.py"), [])

        injected_dir = (
            "class Sample:\n"
            "    def writes_with_an_injected_dir(self) -> None:\n"
            "        health.record_startup_probes(self.bridge_dir, probes)\n"
        )
        self.assertEqual(unisolated_call_sites(injected_dir, "sample.py"), [])

    def test_isolation_is_inherited_from_a_base_class_in_the_same_file(self):
        """⭐ 判据必须**沿基类**往上找隔离措施。

        ⚠️ 本仓库三处隔离基类（``_BridgeDirIsolated`` / ``_RefusalHarness`` /
        ``RecorderInstalled``）都是「基类 ``setUp`` 钉、子类用」的形状 ⇒ 只看
        最近的类就会把正确的写法全报成缺陷，然后有人去删掉基类。
        """
        sample = (
            "class Base(unittest.TestCase):\n"
            "    def setUp(self) -> None:\n"
            "        with mock.patch.object(\n"
            "            cli, '_bridge_dir', lambda: self.bridge_dir\n"
            "        ):\n"
            "            pass\n"
            "\n"
            "class Child(Base):\n"
            "    def test_it(self) -> int:\n"
            "        return cli.run_bridge(cfg)\n"
        )
        self.assertEqual(unisolated_call_sites(sample, "sample.py"), [])

    def test_the_scan_actually_covers_every_test_module(self):
        """⭐ 反「扫了个空目录」：判据必须真的逐个读了 ``tests/*.py``，且看得见调用点。

        ⚠️ 两个独立的恒空出口，各钉一个：
        ① :meth:`pathlib.Path.glob` 的模式写错一个字符，:meth:`_offenders` 就恒空；
        ② 遍历本身坏了（``ast.walk`` 用错、名字比对写错），offenders 也恒空。
        ⇒ ① 用「文件数 + 抽查具体文件」钉，② 用「**全仓库**调用点总数 > 0」钉。
        """
        scanned = sorted(path.name for path in TESTS_DIR.glob("test_*.py"))
        self.assertGreater(len(scanned), 10, "tests/ 下只扫到 %d 个文件 —— 判据恒空" % len(scanned))
        self.assertIn("test_platform_health.py", scanned)
        self.assertIn("test_core.py", scanned)

        every_site = []
        for path in sorted(TESTS_DIR.glob("test_*.py")):
            every_site.extend(
                (path.name, lineno, callee)
                for lineno, callee, _ in artifact_call_sites(
                    path.read_text(encoding="utf-8"), path.name
                )
            )
        self.assertGreater(
            len(every_site), 20,
            "全仓库只认出 %d 个落盘调用点 —— 判据恒空（AGENTS.md §7.1：空集 ≠ 不存在）"
            % len(every_site),
        )
        #: 直接钉住「间接入口」也看得见：本仓库的污染全部来自 ``cli.run_bridge`` /
        #: ``cli.main``，判据漏掉它们的话 (b) 层对真实缺陷完全无效。
        entry_sites = [site for site in every_site if site[2].startswith("cli.")]
        self.assertGreaterEqual(
            len(entry_sites), 5,
            "只认出 %d 个 cli.* 入口 —— 真正写进仓库的就是它们，判据漏了这一类"
            % len(entry_sites),
        )

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
                is_cli_entry_point("cli." + name),
                "%s 会写运行期产物，判据却认不出 cli.%s ⇒ 新增一层委托就会漏。"
                % (name, name),
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


class TestTheArtifactNamesAreNotHardCodedAnywhere(unittest.TestCase):
    """兜底：判据里除了**反退化那一条**，不许把那两个文件名写死成字面量。

    ⚠️ 这条比看起来重要：判据自己用 :data:`health` 的常量，而
    :mod:`tests.test_platform_health` 那种**刻意的**写死是有意义的（它断言的是
    盘上那个字面量名）。但**判据**写死就等于「改名悄悄绕过护栏」。

    ⚠️ 扫的是**判据那两个文件**（本文件与共享件），不是整个 ``tests/`` ——
    :mod:`tests.test_platform_health` 刻意写着那个字面量名（它断言的就是盘上
    那个名字），扫全目录会把它误报成缺陷。

    ⚠️ 用 **AST** 找字面量而不是正则扫全文：正则会被**注释与文档字符串**里的
    同一批字面量触发（这两个文件的模块 docstring 就在介绍这两个文件名），而那些是
    给人读的、不是判据用的 ⇒ 扫全文必然误报，误报一出现就有人会去加豁免。
    """

    #: 唯一允许写死字面量的那一条：它断言的就是「判据用的名字与生产代码一致」。
    ALLOWED_HARDCODED_TEST = "test_the_two_names_are_really_the_ones_health_writes"

    #: 被扫的那两个文件（判据自己 + 它用的共享件）。
    SCANNED_FILES = (
        pathlib.Path(__file__).resolve(),
        TESTS_DIR / "bridge_dir_isolation_scan.py",
        TESTS_DIR / "test_repository_root_artifacts.py",
    )

    def test_no_judgment_code_hard_codes_the_runtime_artifact_names(self):
        artifacts = set(RUNTIME_ARTIFACT_NAMES)
        offenders: list[str] = []
        for path in self.SCANNED_FILES:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            parents = parent_map(tree)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                    continue
                if node.value not in artifacts:
                    continue
                function, class_node = enclosing_function_and_class(node, parents)
                owner = (
                    function.name if function is not None
                    else class_node.name if class_node is not None else "<模块层>"
                )
                if owner == self.ALLOWED_HARDCODED_TEST:
                    continue
                offenders.append("%s 的 %s 处的字面量 %r" % (path.name, owner, node.value))
        self.assertEqual(
            offenders, [],
            "判据里出现了写死的运行期文件名 —— 生产代码那边改名的话，"
            "护栏会去查一个没人写的文件（恒真）。请改用 "
            "health.PLATFORM_HEALTH_FILE_NAME / health.OUTBOUND_FAILURES_FILE_NAME；"
            "唯一允许写死的是 %s。" % self.ALLOWED_HARDCODED_TEST,
        )


class TestTheScannedNamesAreRealHealthEntrypoints(unittest.TestCase):
    """⭐ 反「判据扫的是没人调用的名字」：被扫的那两个必须真是 health 的落盘入口。

    ⚠️ 这不是多此一举：判据写错了名字就会**恒真**（扫一个没人调用的符号）
    —— 本项目因此踩过「空集被当成不存在」（AGENTS.md §7.1）。
    """

    def test_the_two_scanned_names_are_health_s_exported_entry_points(self):
        self.assertEqual(
            sorted(set(health.__all__) & ARTIFACT_WRITING_CALLS),
            ["OutboundFailureRecorder", "record_startup_probes"],
            "ARTIFACT_WRITING_CALLS 里有一个不是 health 真正导出的落盘入口 —— "
            "判据扫了一个没人调用的名字（恒真）。",
        )
"""测试不许把桥的**运行期产物**写进仓库根目录 —— **症状层**（a）。

钉住的缺陷（实测，不是推断）
============================

``platform-health.json`` 与 ``outbound-failures.json`` 是
:mod:`opencode_bridge.health` 的两份**运行期**记录，落在
``__main__._bridge_dir()`` 推导出来的那个目录里。而那个推导在没有
``OPENCODE_BRIDGE_CONFIG`` 时**退回 cwd** —— 跑测试时 cwd 就是仓库根 ⇒

    只要有一条用例走了真 ``__main__.main`` / ``__main__.run_bridge``（也就是走
    「拒绝启动 → 落一条结论」或「装上出站失败记录器」那两条路），仓库根就会多出
    这两个文件。

⚠️ **它们在 ``.gitignore`` 里** ⇒ 提交时被挡住、``git status`` 干净 ⇒ 缺陷可以
长期存活而不被任何人看见（**空集 ≠ 不存在**：看不见不等于没发生）。

已修的历史现场（本任务之前）：``tests/test_conversation_id_cutover.py`` 的
``CliOpensKeyMigration._run_bridge``、``tests/test_inbox_wiring.py`` 的
``CliWiresTheInbox``、``tests/test_core.py::CliTests`` 的两条 ``cli.main`` 用例。

本文件负责**症状层（a）**，源头层（b）在
:mod:`tests.test_artifact_isolation_contract`。
"""

from __future__ import annotations

import os
import unittest

from opencode_bridge import health
from tests.bridge_dir_isolation_scan import RUNTIME_ARTIFACT_NAMES

REPOSITORY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _artifacts_in_repository_root() -> list[str]:
    return [
        name
        for name in RUNTIME_ARTIFACT_NAMES
        if os.path.isfile(os.path.join(REPOSITORY_ROOT, name))
    ]


class TestTheRepositoryRootHoldsNoRuntimeArtifact(unittest.TestCase):
    """(a) 症状层：仓库根**此刻**没有那两个运行期文件。

    ⚠️ 这条断言本身**抓不住将来的新缺口**（它只看此刻），真正抓得住的是
    :class:`TestAReproducedPollutionIsCaught`。
    """

    def test_the_repository_root_holds_no_runtime_artifact(self):
        found = _artifacts_in_repository_root()
        self.assertEqual(
            found, [],
            "仓库根出现了桥的运行期产物：%s\n"
            "它们的名字在 .gitignore 里 ⇒ 提交时被挡住、git status 干净，"
            "所以缺陷可以长期存活而没人看见。跑测试的用例必须把 bridge 目录"
            "钉在临时目录上（mock.patch(cli, '_bridge_dir', ...) 或设 "
            "OPENCODE_BRIDGE_CONFIG）。" % found,
        )

    def test_the_two_names_are_really_the_ones_health_writes(self):
        """⭐ 反「判据本身坏了」：断言用的文件名必须**就是**生产代码那两个。

        ⚠️ 这不是多此一举：判据写错了名字就会**恒真**（去查一个没人写的文件）
        —— 本项目因此踩过「空集被当成不存在」（AGENTS.md §7.1）。
        """
        self.assertEqual(
            RUNTIME_ARTIFACT_NAMES,
            ("platform-health.json", "outbound-failures.json"),
            "这两个文件名变了的话，本文件的判据就在查没人写的文件 —— "
            "恒真。请连同 :mod:`opencode_bridge.health` 一起改。",
        )
        self.assertEqual(
            health.PLATFORM_HEALTH_FILE_NAME, RUNTIME_ARTIFACT_NAMES[0],
            "判据用的名字与 health 的常量脱钩了 —— 护栏会去查一个没人写的文件。",
        )
        self.assertEqual(
            health.OUTBOUND_FAILURES_FILE_NAME, RUNTIME_ARTIFACT_NAMES[1],
            "判据用的名字与 health 的常量脱钩了 —— 护栏会去查一个没人写的文件。",
        )


#: 会被 :class:`TestAReproducedPollutionIsCaught` 在同进程里再跑一遍的模块 ——
#: 就是本任务修过的那三个（它们各自含一处会写到 cwd 的 CLI 入口）。
REPLAYED_MODULES = (
    "tests.test_conversation_id_cutover",
    "tests.test_core",
    "tests.test_inbox_wiring",
)


class TestAReproducedPollutionIsCaught(unittest.TestCase):
    """(a) 症状层的**可复现**那一半：把那三个模块再跑一遍，仓库根必须仍然干净。

    ⚠️ 为什么必须真的跑一遍：只断言「此刻仓库根是干净的」，那么一条**将来**新写的
    不隔离用例永远抓不住它 —— 它跑的时候还没污染，而断言在它跑之前就已经通过了。
    ⇒ 这里在同进程里重放，之后复检 ⇒ **隔离被摘掉的那一处必然让本条变红**。

    ⚠️ 重放的是**真的**测试，所以内层若有失败必须一起报出来，否则「内层红了但
    护栏只看文件」会把它悄悄咽掉。

    ⚠️ 这条护栏**不是**判据的全部：它只能抓到**真的写出文件**的那些变异。
    实测过 —— 把 :class:`~tests.test_inbox_wiring.CliWiresTheInbox` 的隔离摘掉时
    仓库根仍然干净（那条路只**装配**记录器，从不记录失败）⇒ 那种变异由源头层
    （b）抓，见 :mod:`tests.test_artifact_isolation_contract`。两层缺一不可。
    """

    def test_replaying_the_three_modules_leaves_the_repository_root_clean(self):
        polluted_before = _artifacts_in_repository_root()
        self.assertEqual(
            polluted_before, [],
            "重放之前仓库根就已经脏了（%s）—— 先清掉，否则下面那条判据恒假。"
            % polluted_before,
        )

        failures: list[str] = []
        for module_name in REPLAYED_MODULES:
            suite = unittest.TestLoader().loadTestsFromName(module_name)
            result = unittest.TestResult()
            suite.run(result)
            if not result.wasSuccessful():
                failures.append("%s: %s" % (module_name, result.errors + result.failures))

        polluted_after = _artifacts_in_repository_root()
        try:
            self.assertEqual(
                failures, [],
                "重放 %s 时有用例失败了 —— 本条护栏只检查落盘文件，"
                "会把它们咽掉：\n%s" % (list(REPLAYED_MODULES), "\n".join(failures)),
            )
            self.assertEqual(
                polluted_after, [],
                "重放 %s 之后仓库根出现了 %s —— 有一处 CLI 入口没把 bridge 目录"
                "钉在临时目录上。" % (list(REPLAYED_MODULES), polluted_after),
            )
        finally:
            #: ⚠️ 重放本身**不许**留下痕迹：这条护栏是在缺陷复发时才该变红的，
            #: 而不是自己每次跑都往仓库根丢两个文件。
            for name in polluted_after:
                os.remove(os.path.join(REPOSITORY_ROOT, name))


if __name__ == "__main__":  # pragma: no cover - 手动跑这一个文件用
    unittest.main()
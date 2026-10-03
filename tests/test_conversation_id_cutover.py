"""``conversation_id`` 前缀切换（telegram / matrix）+ ``state.json`` 键迁移的端到端不变量。

## 这是 G5 的第 1 步

统一 ``platform:local_id`` 不能一次切完，因为旧前缀分两类：

* ``chat:`` → ``telegram``、``room:`` → ``matrix``：**无歧义**，
  :func:`identity.normalize` 闭着眼睛也能归一 ⇒ 本文件覆盖的两个平台。
* ``channel:`` → slack / discord / mattermost **三家共用**：**歧义**，
  缺 ``platform_hint`` 时 ``normalize()`` 直接抛
  :class:`~opencode_bridge.identity.AmbiguousConversationId`，而
  :class:`~opencode_bridge.state.StateStore` 拿到的只是不透明字符串、无从判断。
  那三家留到第 2 步（需要一次设计决策），本文件只保证它们**现在没被弄坏**。

## 唯一真正要命的不变量

``conversation_id`` 是 :class:`StateStore` 的**不透明键**。切前缀如果不同步打开
``migrate_keys=True``，已落盘的 ``chat:`` / ``room:`` 键会一次性变成孤儿 ——
用户升级后一次性"忘记"所有历史会话，而且**不报错**，只表现为
"agent 突然记错上下文"。所以"适配器产出新前缀"与"加载期键迁移"必须同一个变更上线，
``__main__`` 也必须真的把开关打开 —— 后者由 :class:`CliOpensKeyMigration` 守着。

## 断言纪律

"旧前缀不许再出现"一律比**字面量**（``"chat:"`` / ``"room:"``），绝不比
:data:`identity.LEGACY_PREFIXES` 常量 —— 改了常量，断言就变成恒真
（``tasks.md`` 记的教训）。反过来，"旧 id 能被归一成新 id"那条**必须**引用常量：
那是登记表本身的契约。

零真实网络、零真实 sleep。临时目录钉在仓库内的 ``.tmp/``，绝不写到仓库之外。
"""

from __future__ import annotations

import json
import os
import tempfile
import types
import unittest
from unittest import mock

from opencode_bridge import __main__ as cli
from opencode_bridge.adapters.matrix import MatrixAdapter
from opencode_bridge.adapters.telegram import TelegramAdapter
from opencode_bridge.config import Config
from opencode_bridge.core import BridgeCore
from opencode_bridge.opencode_client import Endpoint
from opencode_bridge.state import StateStore
from tests.test_core import FakeClient
from tests.test_routing import NamedAdapter

_REPOSITORY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: 仓库内的临时目录：即便 TEMP/TMP 指向机器别处，测试也不可能写到仓库之外。
_REPOSITORY_TEMP = os.path.join(_REPOSITORY_ROOT, ".tmp")

TELEGRAM_CHAT = 55
MATRIX_ROOM = "!abc:example.org"
#: 旧前缀一律写**字面量**，不用 ``identity.LEGACY_PREFIXES`` 拼 —— 拼了就恒真。
LEGACY_TELEGRAM_KEY = "chat:55"
LEGACY_MATRIX_KEY = "room:!abc:example.org"
#: 三家共用的歧义前缀；本步**不许**碰它。
AMBIGUOUS_CHANNEL_KEY = "channel:C1"


def write_legacy_state(directory: str, document: dict, name: str = "state.json") -> str:
    """真写一份**切换前**的 ``state.json``（模拟升级前用户的磁盘）。"""
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(document, handle, ensure_ascii=False)
    return path


def read_state_bytes(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def backup_files(directory: str, name: str = "state.json") -> list[str]:
    import glob

    return sorted(glob.glob(os.path.join(directory, name + ".bak.*")))


class LegacyStateFileTestCase(unittest.TestCase):
    """每个用例一份仓库内的临时目录。"""

    def setUp(self) -> None:
        os.makedirs(_REPOSITORY_TEMP, exist_ok=True)
        self._directory = tempfile.TemporaryDirectory(dir=_REPOSITORY_TEMP)
        self.addCleanup(self._directory.cleanup)
        self.directory = self._directory.name


class A2bCompletionCriterion(LegacyStateFileTestCase):
    """台账 A2b 的完成判据：两个旧键都要在新格式下、带着原来的会话回来。"""

    def test_both_legacy_keys_come_back_under_the_new_format(self):
        path = write_legacy_state(self.directory, {
            "sessions": {LEGACY_TELEGRAM_KEY: "ses-telegram",
                         LEGACY_MATRIX_KEY: "ses-matrix"},
            "meta": {LEGACY_TELEGRAM_KEY: {"directory": "docs"},
                     LEGACY_MATRIX_KEY: {"directory": "."}},
        })

        store = StateStore(path, migrate_keys=True)

        report = store.last_migration
        self.assertEqual(report.scanned, 2)
        self.assertEqual(report.migrated, 2)
        self.assertEqual(report.collisions, 0)
        self.assertEqual(report.ambiguous_kept, 0)
        # 适配器现在**问的**必须就是迁移后的键。
        # ⚠️ 这两条不许省：少了它们，下面那次 get_session 会靠别名回退"蒙对"，
        # 于是"适配器还在发旧前缀"这种半迁移态照样全绿 —— 而那正是要防的状态。
        self.assertEqual(TelegramAdapter._conversation_id(TELEGRAM_CHAT), "telegram:55")
        self.assertEqual(MatrixAdapter._conversation_id(MATRIX_ROOM),
                         "matrix:!abc:example.org")
        # 用**适配器自己产出的 id** 去查 —— 这才是"用户下一次发消息时会发生什么"
        self.assertEqual(
            store.get_session(TelegramAdapter._conversation_id(TELEGRAM_CHAT)),
            "ses-telegram",
        )
        self.assertEqual(
            store.get_session(MatrixAdapter._conversation_id(MATRIX_ROOM)),
            "ses-matrix",
        )
        self.assertEqual(store.get_meta(TelegramAdapter._conversation_id(TELEGRAM_CHAT),
                                        "directory"), "docs")
        with open(path, "r", encoding="utf-8") as handle:
            on_disk = json.load(handle)
        self.assertEqual(
            on_disk["sessions"],
            {"telegram:55": "ses-telegram", "matrix:!abc:example.org": "ses-matrix"},
        )
        # meta 必须用**同一张**映射表，否则 get_meta 与 get_session 会对不上
        self.assertEqual(
            on_disk["meta"],
            {"telegram:55": {"directory": "docs"},
             "matrix:!abc:example.org": {"directory": "."}},
        )
        # 旧键不许留在盘上：比**字面量**，比常量就恒真了
        self.assertNotIn("chat:", json.dumps(on_disk, ensure_ascii=False))
        self.assertNotIn("room:", json.dumps(on_disk, ensure_ascii=False))


class AmbiguousChannelKeysSurvive(LegacyStateFileTestCase):
    """⚠️ 本步的承重安全属性：三家共用的 ``channel:`` 键必须**原封不动**。

    它无法无歧义归一（``normalize()`` 会抛 :class:`AmbiguousConversationId`），
    所以迁移期一律原样保留。**三个适配器还在产出这种前缀** —— 弄坏它就是弄坏
    还在用的 slack / discord / mattermost。
    """

    def test_channel_key_is_left_verbatim_and_still_resolvable(self):
        path = write_legacy_state(self.directory, {
            "sessions": {AMBIGUOUS_CHANNEL_KEY: "ses-slack",
                         LEGACY_TELEGRAM_KEY: "ses-telegram"},
            "meta": {AMBIGUOUS_CHANNEL_KEY: {"nick": "bob"}},
        })

        store = StateStore(path, migrate_keys=True)

        report = store.last_migration
        self.assertEqual(report.migrated, 1, "只有 telegram 那个键该被改写")
        self.assertEqual(report.ambiguous_kept, 1, "channel: 必须被认成歧义前缀、而不是不认识")
        self.assertEqual(store.get_session(AMBIGUOUS_CHANNEL_KEY), "ses-slack",
                         "原样保留的键必须仍查得到")
        self.assertEqual(store.get_meta(AMBIGUOUS_CHANNEL_KEY, "nick"), "bob")
        with open(path, "r", encoding="utf-8") as handle:
            on_disk = json.load(handle)
        self.assertIn(AMBIGUOUS_CHANNEL_KEY, on_disk["sessions"])
        self.assertEqual(on_disk["sessions"][AMBIGUOUS_CHANNEL_KEY], "ses-slack")
        self.assertEqual(on_disk["meta"][AMBIGUOUS_CHANNEL_KEY], {"nick": "bob"})
        self.assertEqual(on_disk["sessions"]["telegram:55"], "ses-telegram")

    def test_a_channel_only_state_file_is_not_rewritten_at_all(self):
        """整份文件只有歧义键 ⇒ 零迁移 ⇒ 连字节都不许动（也不许产生备份）。"""
        document = {"sessions": {AMBIGUOUS_CHANNEL_KEY: "ses-slack"}, "meta": {}}
        path = write_legacy_state(self.directory, document)
        before = read_state_bytes(path)

        store = StateStore(path, migrate_keys=True)

        self.assertEqual(store.last_migration.migrated, 0)
        self.assertFalse(store.last_migration.changed)
        self.assertEqual(read_state_bytes(path), before)
        self.assertEqual(backup_files(self.directory), [])

    def test_slack_discord_mattermost_routing_is_unaffected(self):
        """同一个问题分别问"迁移过的 core"和"没迁移过的 core"，答案必须一致。

        路由是 :meth:`BridgeCore._route_by_prefix` 的启发式（纯数字→discord、
        C 开头→slack）。本步绝不允许改动它 —— 所以这里不比某个写死的答案，而是比
        "迁移前后是否一致"：一致即证明这步没顺手碰路由。
        """
        path = write_legacy_state(self.directory, {
            "sessions": {AMBIGUOUS_CHANNEL_KEY: "ses-slack"}, "meta": {},
        })
        migrated_core = self._core_with(StateStore(path, migrate_keys=True))
        fresh_core = self._core_with(StateStore(os.path.join(self.directory, "empty.json")))

        for conversation_id in ("channel:C1", "channel:1234567890",
                                "channel:abcdefghijklmnopqrstuvwxyz"):
            with self.subTest(conversation_id=conversation_id):
                self.assertEqual(self._route(migrated_core, conversation_id),
                                 self._route(fresh_core, conversation_id),
                                 "本步只许改键格式，不许改路由")
        # 顺带钉住：新格式的 telegram / matrix id 仍各自路由回自己的适配器
        self.assertEqual(self._route(migrated_core, "telegram:55"), "telegram")
        self.assertEqual(self._route(migrated_core, "matrix:!abc:example.org"), "matrix")
        self.assertEqual(self._route(migrated_core, AMBIGUOUS_CHANNEL_KEY), "slack")

    @staticmethod
    def _core_with(store: StateStore) -> BridgeCore:
        core = BridgeCore(Config(), FakeClient(), store)
        for name in ("slack", "discord", "mattermost", "telegram", "matrix"):
            core.attach(NamedAdapter(name))
        return core

    @staticmethod
    def _route(core: BridgeCore, conversation_id: str) -> str | None:
        adapter = core._adapter_for(conversation_id)
        return adapter.name if adapter is not None else None


class MigrationIsIdempotent(LegacyStateFileTestCase):
    """重复加载必须逐字节收敛，且只备份一次。"""

    def test_repeated_loads_change_nothing_and_back_up_once(self):
        path = write_legacy_state(self.directory, {
            "sessions": {LEGACY_TELEGRAM_KEY: "ses-telegram",
                         AMBIGUOUS_CHANNEL_KEY: "ses-slack"},
            "meta": {LEGACY_MATRIX_KEY: {"nick": "bob"}},
        })

        StateStore(path, migrate_keys=True)
        after_first_load = read_state_bytes(path)
        backups_after_first = backup_files(self.directory)
        self.assertEqual(len(backups_after_first), 1, "第一次迁移必须留一份可回滚的备份")
        # 备份里是**迁移前**的内容，用户要能自己回滚
        with open(backups_after_first[0], "r", encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["sessions"][LEGACY_TELEGRAM_KEY],
                             "ses-telegram")

        for _ in range(3):
            again = StateStore(path, migrate_keys=True)
            self.assertEqual(again.last_migration.migrated, 0)
            self.assertFalse(again.last_migration.changed)
            self.assertEqual(read_state_bytes(path), after_first_load,
                             "重复加载不许再改文件（逐字节）")
            self.assertEqual(backup_files(self.directory), backups_after_first,
                             "不许二次备份")
        self.assertEqual(len(backup_files(self.directory)), 1)


class CollidingKeysAreBothKept(LegacyStateFileTestCase):
    """新旧两条同指一个会话时：两条都留、谁也不覆盖，并**告警**。

    覆盖任一条都可能把用户映射到**别人的会话**上，且不报错。所以策略是放弃迁移，
    把那条旧键留在原地 —— 用户需要知道，所以必须打 WARNING。
    """

    def test_both_sessions_survive_and_the_collision_is_reported(self):
        path = write_legacy_state(self.directory, {
            "sessions": {LEGACY_TELEGRAM_KEY: "ses-legacy", "telegram:55": "ses-modern"},
            "meta": {},
        })
        before = read_state_bytes(path)

        with self.assertLogs("opencode_bridge.state", level="WARNING") as logs:
            store = StateStore(path, migrate_keys=True)

        self.assertEqual(store.last_migration.collisions, 1)
        self.assertEqual(store.last_migration.migrated, 0)
        self.assertFalse(store.last_migration.changed)
        self.assertEqual(store.get_session(LEGACY_TELEGRAM_KEY), "ses-legacy")
        self.assertEqual(store.get_session("telegram:55"), "ses-modern")
        self.assertEqual(read_state_bytes(path), before, "零迁移就不许碰用户的文件")
        self.assertEqual(backup_files(self.directory), [])
        self.assertTrue(any("legacy form" in line for line in logs.output), logs.output)


class CorruptStateFileIsNeverOverwritten(LegacyStateFileTestCase):
    """打开开关**不许**改变"损坏文件保持现场"这条已有行为。"""

    def test_corrupt_file_stays_byte_identical_and_is_reported_as_error(self):
        path = os.path.join(self.directory, "state.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('{"sessions": {"chat:55": "ses-a"')   # 截断的 JSON
        before = read_state_bytes(path)

        with self.assertLogs("opencode_bridge.state", level="ERROR") as logs:
            store = StateStore(path, migrate_keys=True)

        self.assertEqual(read_state_bytes(path), before,
                         "损坏文件必须保持原样，用户才能自己抢救")
        self.assertEqual(store.last_migration.migrated, 0)
        self.assertEqual(store.all_sessions(), {}, "读不出来就不许凭空造映射")
        self.assertEqual(backup_files(self.directory), [])
        self.assertTrue(any("not valid JSON" in line for line in logs.output), logs.output)


# ----------------------------------------------------------------------
# __main__ 接线守卫
# ----------------------------------------------------------------------
class _RecordingCore:
    """只记下构造函数收到的参数 —— 本文件不关心桥跑起来做什么。"""

    def __init__(self, config, client, state, inbox=None) -> None:
        pass

    def attach(self, adapter) -> None:
        return None

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None


class _SilentDiagnostics:
    def __init__(self, directory) -> None:
        pass

    def install(self) -> None:
        return None

    def record(self, exit_reason, detail="") -> None:
        return None

    def dump_stacks(self, exit_reason) -> None:
        return None

    def close(self) -> None:
        return None


class _PermissiveInstanceLock:
    def __init__(self, directory) -> None:
        pass

    def acquire(self) -> tuple[bool, int]:
        return True, 0

    def release(self) -> None:
        return None


class _AlreadyStopping:
    """让 ``stop_event.wait(1.0)`` 立刻为真，测试不必真等一秒。"""

    def wait(self, timeout=None) -> bool:
        return True

    def set(self) -> None:
        return None


class CliOpensKeyMigration(LegacyStateFileTestCase):
    """⚠️ 守卫：``migrate_keys`` 是**可选参数**，漏打开不会有任何测试变红。

    ``StateStore`` 的默认值是关（迁移期刻意如此），而 ``__main__`` 不显式打开的话，
    telegram / matrix 一旦改用新前缀，磁盘上所有旧键就全部作废 —— 且**不报错**。
    同样的道理：``test_inbox_wiring`` 守着"``__main__`` 必须注入收件箱"。
    """

    def _run_bridge(self, state_path: str) -> int:
        """走真实的 ``__main__.run_bridge``，只把网络与线程换成替身。"""
        config = Config(
            state_path=state_path,
            adapters={"telegram": {"bot_token": "token-not-real"}},
        )
        with mock.patch.object(
            cli, "discover_endpoint",
            return_value=Endpoint("http://127.0.0.1:4096", "pw"),
        ), mock.patch.object(
            cli, "OpenCodeClient",
            lambda endpoint: types.SimpleNamespace(close=lambda: None),
        ), mock.patch.object(
            cli, "build",
            lambda name, entry, hooks: types.SimpleNamespace(bot_token="token-not-real"),
        ), mock.patch.object(
            cli, "BridgeCore", _RecordingCore
        ), mock.patch.object(
            cli, "ProcessDiagnostics", _SilentDiagnostics
        ), mock.patch.object(
            cli, "InstanceLock", _PermissiveInstanceLock
        ), mock.patch.object(
            cli, "threading", types.SimpleNamespace(Event=lambda: _AlreadyStopping())
        ):
            return cli.run_bridge(config)

    def test_bridge_opens_the_state_store_with_key_migration_enabled(self):
        """真跑一次启动接线：既断言关键字传下去了，也断言**盘上真的迁移了**。"""
        path = write_legacy_state(self.directory, {
            "sessions": {LEGACY_TELEGRAM_KEY: "ses-telegram",
                         AMBIGUOUS_CHANNEL_KEY: "ses-slack"},
            "meta": {},
        })
        recorded: dict = {}
        real_store = cli.StateStore

        def recording_store(store_path, **kwargs):
            recorded.update(kwargs)
            return real_store(store_path, **kwargs)

        with mock.patch.object(cli, "StateStore", recording_store):
            exit_code = self._run_bridge(path)

        self.assertEqual(exit_code, 0)
        self.assertIs(
            recorded.get("migrate_keys"), True,
            "__main__ 必须显式打开 migrate_keys：不打开的话 telegram/matrix 切前缀"
            "就会让所有已落盘的 chat:/room: 键成孤儿，用户一次性丢失会话映射且不报错",
        )
        # 关键字那条断言够不够？不够 —— 它只证明参数传到了；下面这条断言**行为**：
        # 不开开关时盘上的键会停在 "chat:55"，查询新 id 直接 KeyError。
        with open(path, "r", encoding="utf-8") as handle:
            sessions = json.load(handle)["sessions"]
        self.assertEqual(sessions["telegram:55"], "ses-telegram")
        self.assertIn(AMBIGUOUS_CHANNEL_KEY, sessions, "歧义键不许被牵动")

    def test_bridge_leaves_a_corrupt_state_file_untouched(self):
        """开关打开后，"损坏文件保持现场"这条老规矩不能被打破。"""
        path = os.path.join(self.directory, "state.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('{"sessions": {"chat:55": "ses-a"')
        before = read_state_bytes(path)

        with self.assertLogs("opencode_bridge.state", level="ERROR") as logs:
            exit_code = self._run_bridge(path)

        self.assertEqual(exit_code, 0)
        self.assertEqual(read_state_bytes(path), before)
        self.assertTrue(any("not valid JSON" in line for line in logs.output), logs.output)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
"""``state.py`` 测试：``state.json`` 会话键迁移（A2b）。

重点覆盖八类真实风险：
1. **正常迁移** —— ``chat:`` → ``telegram:``、``room:`` → ``matrix:``；
2. **歧义 ``channel:`` 原样保留** —— slack / discord / mattermost 三家共用，
   :func:`identity.normalize` 没有 ``platform_hint`` 就抛错，本模块**绝不许猜**；
3. **幂等** —— 跑两次结果相同、不重复备份；
4. **原子性** —— 落盘失败时原文件完好（临时文件 + ``os.replace``）；
5. **损坏文件不被覆盖** —— 保持现场让用户自己抢救；
6. **条目数守恒** —— 键变了但条目数不变；
7. **未知键原样保留** —— 不许因为"看不懂"就丢；
8. **备份确实生成** —— 用户要能自己回滚。

零真实网络、零真实 sleep。涉及"等了多久"的地方一律断言**内部状态**
（``store.last_migration`` / 落盘内容 / 备份文件集合），不断言具体时间值 ——
备份文件名里的时间戳只断言前缀与内容。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import unittest
from unittest import mock

from opencode_bridge.state import (
    STATE_SCHEMA_VERSION,
    DormantSessionReport,
    MigrationReport,
    StateStore,
    _classify_key,
    migrate_document,
    log_dormant_session_reconciliation,
    reconcile_dormant_sessions,
)


def write_state(directory: str, document: dict, name: str = "state.json") -> str:
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(document, fh, ensure_ascii=False)
    return path


def read_state(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def backups(directory: str, name: str = "state.json") -> list:
    import glob

    return sorted(glob.glob(os.path.join(directory, name + ".bak.*")))


class TestKeyClassification(unittest.TestCase):
    """钉死"哪些键能迁、哪些不能"这条策略本身。"""

    def test_unambiguous_legacy_aliases_are_migratable(self):
        self.assertEqual(_classify_key("chat:55"), ("migrate", "telegram:55"))
        self.assertEqual(
            _classify_key("room:!abcDEF:example.org"),
            ("migrate", "matrix:!abcDEF:example.org"),
        )

    def test_ambiguous_channel_prefix_is_never_resolved(self):
        """``channel:`` 没有别名 —— 绝不返回任何平台键，哪怕 local 段形状可辨。

        下面三种 local 段形状**明显可分**（Slack ``C``/``D`` 开头、Discord 纯数字、
        Mattermost 26 位 base32）。本模块依然全部判为歧义：形状启发式就是
        ``identity`` 明确拒绝的那种猜测，而猜错会把用户映射到别人的会话上。
        """
        shapes = ("C123ABC", "123456789012345678", "abcdefghijklmnopqrstuvwxyz")
        for local in shapes:
            with self.subTest(local=local):
                verdict, new_key = _classify_key(f"channel:{local}")
                self.assertEqual(verdict, "ambiguous")
                self.assertIsNone(new_key)

    def test_empty_local_segment_is_invalid_not_ambiguous(self):
        """``channel:``（local 段为空）：``normalize`` 连切分都过不去。

        归为 ``invalid`` 而非 ``ambiguous`` 只是一个分类选择 —— 两种都不迁移，
        所以对"数据一条不丢"没有任何区别。
        """
        self.assertEqual(_classify_key("channel:")[0], "invalid")

    def test_already_new_format_is_left_alone(self):
        for key in ("telegram:55", "matrix:!a:b.org", "slack:C1", "irc:#chan",
                    "twitch:target", "nextcloud:tok"):
            with self.subTest(key=key):
                self.assertEqual(_classify_key(key), ("new", None))

    def test_self_mapped_legacy_prefixes_are_already_new_format(self):
        """``irc`` / ``twitch`` / ``nextcloud`` 映射到自身 ⇒ 逐字节不变。"""
        for key in ("irc:#chan", "twitch:abc", "nextcloud:tok123"):
            with self.subTest(key=key):
                self.assertEqual(_classify_key(key), ("new", None))

    def test_unparsable_keys_are_invalid_not_ambiguous(self):
        for key in ("no-colon", "", "   ", "Chat:55", "1abc:x", "a" * 33 + ":x",
                    123, None):
            with self.subTest(key=key):
                self.assertEqual(_classify_key(key)[0], "invalid")

    def test_matrix_room_id_with_colon_survives(self):
        """⚠️ Matrix 房间 id 自带冒号：``room:!abcDEF:example.org``。

        :func:`identity.normalize` 会校验平台名，所以这里必须证明"含冒号的 local
        段"不会被当成非法平台段而抛异常。
        """
        verdict, new_key = _classify_key("room:!abcDEF:example.org")
        self.assertEqual(verdict, "migrate")
        self.assertEqual(new_key, "matrix:!abcDEF:example.org")


class TestMigrateDocumentPure(unittest.TestCase):
    """``migrate_document`` 是纯函数：不碰磁盘、不改入参。"""

    def test_rewrites_sessions_and_meta_consistently(self):
        raw = {
            "sessions": {"chat:55": "ses_a", "channel:C1": "ses_b"},
            "meta": {"chat:55": {"nick": "bob"}},
        }
        snapshot = json.loads(json.dumps(raw))
        document, report = migrate_document(raw)

        self.assertEqual(document["sessions"]["telegram:55"], "ses_a")
        self.assertEqual(document["sessions"]["channel:C1"], "ses_b")
        # meta 用**同一张**映射表，否则 get_meta 会与 get_session 对不上
        self.assertEqual(document["meta"], {"telegram:55": {"nick": "bob"}})
        self.assertEqual(report.migrated, 1)
        self.assertEqual(report.ambiguous_kept, 1)
        self.assertTrue(raw == snapshot, "入参文档不许被就地修改")

    def test_entry_counts_are_conserved(self):
        raw = {
            "sessions": {
                "chat:1": "s1", "chat:2": "s2", "room:r": "s3",
                "channel:C1": "s4", "channel:99": "s5",
                "telegram:9": "s6", "weird-key": "s7", "no-colon": "s8",
            },
            "meta": {"chat:1": {"a": 1}, "channel:C1": {"b": 2}, "room:r": {}},
        }
        document, report = migrate_document(raw)
        self.assertEqual(len(document["sessions"]), len(raw["sessions"]))
        self.assertEqual(len(document["meta"]), len(raw["meta"]))
        # 值一条不少、不少一条（键名变了，映射本身必须完整）
        self.assertEqual(
            sorted(document["sessions"].values()), sorted(raw["sessions"].values())
        )
        self.assertEqual(report.migrated, 3)          # chat:1 chat:2 room:r
        self.assertEqual(report.ambiguous_kept, 2)    # channel:C1 channel:99
        self.assertEqual(report.invalid_kept, 2)      # weird-key / no-colon
        self.assertEqual(report.collisions, 0)

    def test_unknown_keys_are_preserved_verbatim(self):
        raw = {"sessions": {"weird-key": "s", "no-colon": "t", "9bad:x": "u"},
               "meta": {}}
        document, _ = migrate_document(raw)
        self.assertEqual(set(document["sessions"]), {"weird-key", "no-colon", "9bad:x"})

    def test_collision_keeps_both_and_never_overwrites(self):
        """目标键已存在时不许合并 —— 两条都留着，绝不覆盖任何一条。"""
        raw = {"sessions": {"chat:55": "legacy", "telegram:55": "modern"},
               "meta": {}}
        document, report = migrate_document(raw)
        self.assertEqual(document["sessions"]["chat:55"], "legacy")
        self.assertEqual(document["sessions"]["telegram:55"], "modern")
        self.assertEqual(report.migrated, 0)
        self.assertEqual(report.collisions, 1)
        self.assertFalse(report.changed)

    def test_two_legacy_keys_onto_one_target_keep_both(self):
        raw = {"sessions": {"chat:55": "a", "room:55": "b"}, "meta": {}}
        document, report = migrate_document(raw)
        # 两个不同前缀归一后不会撞（telegram:55 / matrix:55），此例只是固定行为：
        # 无论撞不撞，条目数都必须守恒。
        self.assertEqual(len(document["sessions"]), 2)
        self.assertEqual(report.collisions, 0)

    def test_empty_document(self):
        document, report = migrate_document({})
        self.assertEqual(document["sessions"], {})
        self.assertEqual(document["meta"], {})
        self.assertFalse(report.changed)
        self.assertEqual(report.scanned, 0)

    def test_non_dict_sections_are_tolerated(self):
        document, report = migrate_document({"sessions": "junk", "meta": 5})
        self.assertEqual(document["sessions"], {})
        self.assertEqual(document["meta"], {})
        self.assertFalse(report.changed)

    def test_schema_version_written_only_when_something_changed(self):
        untouched, report = migrate_document(
            {"sessions": {"telegram:1": "s"}, "meta": {}}
        )
        self.assertNotIn("schema_version", untouched)
        self.assertEqual(report.schema_version, 0)

        document, report = migrate_document({"sessions": {"chat:1": "s"}, "meta": {}})
        self.assertEqual(document["schema_version"], STATE_SCHEMA_VERSION)
        self.assertEqual(report.schema_version, STATE_SCHEMA_VERSION)


class TestMigrationOnLoad(unittest.TestCase):
    """``StateStore(..., migrate_keys=True)`` 的落盘行为。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.td = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def test_migration_rewrites_keys_on_disk_and_logs_backup_path(self):
        path = write_state(self.td, {
            "sessions": {"chat:55": "ses_a", "room:!r:example.org": "ses_b"},
            "meta": {"chat:55": {"nick": "bob"}},
        })
        with self.assertLogs("opencode_bridge.state", level="INFO") as logs:
            store = StateStore(path, migrate_keys=True)

        report = store.last_migration
        self.assertEqual(report.migrated, 2)
        self.assertTrue(report.changed)

        on_disk = read_state(path)
        self.assertEqual(on_disk["sessions"], {
            "telegram:55": "ses_a",
            "matrix:!r:example.org": "ses_b",
        })
        self.assertEqual(on_disk["meta"], {"telegram:55": {"nick": "bob"}})
        self.assertEqual(on_disk["schema_version"], STATE_SCHEMA_VERSION)

        # 备份确实存在、内容等于迁移前的原文件（用户要能自己回滚）
        found = backups(self.td)
        self.assertEqual(len(found), 1)
        self.assertEqual(report.backup_path, found[0])
        self.assertEqual(read_state(found[0])["sessions"],
                         {"chat:55": "ses_a", "room:!r:example.org": "ses_b"})
        # 备份路径必须出现在日志里（不许静默）
        self.assertTrue(any(found[0] in line for line in logs.output), logs.output)

    def test_ambiguous_channel_keys_are_preserved_verbatim(self):
        """歧义键三家共用 ⇒ 原样保留，一个都不许猜。"""
        slack, discord, mattermost = (
            "channel:C0123ABC", "channel:123456789012345678",
            "channel:abcdefghijklmnopqrstuvwxyz",
        )
        path = write_state(self.td, {
            "sessions": {slack: "ses_s", discord: "ses_d", mattermost: "ses_m",
                         "chat:55": "ses_t"},
            "meta": {discord: {"n": 1}},
        })
        store = StateStore(path, migrate_keys=True)

        self.assertEqual(store.last_migration.migrated, 1)
        self.assertEqual(store.last_migration.ambiguous_kept, 3)
        sessions = read_state(path)["sessions"]
        for key in (slack, discord, mattermost):
            with self.subTest(key=key):
                self.assertIn(key, sessions, "歧义键必须原样保留")
        self.assertEqual(sessions[slack], "ses_s")
        self.assertEqual(sessions[discord], "ses_d")
        self.assertEqual(sessions[mattermost], "ses_m")
        self.assertEqual(read_state(path)["meta"], {discord: {"n": 1}})

    def test_idempotent_across_repeated_loads(self):
        path = write_state(self.td, {
            "sessions": {"chat:55": "ses_a", "channel:C1": "ses_b"},
            "meta": {},
        })
        first = StateStore(path, migrate_keys=True)
        self.assertEqual(first.last_migration.migrated, 1)
        after_first = read_state(path)
        backup_first = backups(self.td)
        self.assertEqual(len(backup_first), 1)

        for _ in range(3):
            again = StateStore(path, migrate_keys=True)
            self.assertEqual(again.last_migration.migrated, 0)
            self.assertFalse(again.last_migration.changed)
            self.assertEqual(read_state(path), after_first, "重复加载不许再改文件")
            self.assertEqual(backups(self.td), backup_first, "不许二次备份")

    def test_disabled_by_default_keeps_legacy_keys_untouched(self):
        path = write_state(self.td, {"sessions": {"chat:55": "s"}, "meta": {}})
        before = read_state(path)
        with self.assertLogs("opencode_bridge.state", level="INFO") as logs:
            store = StateStore(path)
        self.assertEqual(store.all_sessions(), {"chat:55": "s"})
        self.assertEqual(read_state(path), before)
        self.assertEqual(backups(self.td), [])
        self.assertEqual(store.last_migration.migrated, 0)
        self.assertTrue(any("disabled" in line for line in logs.output), logs.output)

    def test_nothing_to_migrate_is_still_explainable(self):
        path = write_state(self.td, {"sessions": {"telegram:55": "s"}, "meta": {}})
        before = read_state(path)
        with self.assertLogs("opencode_bridge.state", level="INFO") as logs:
            store = StateStore(path, migrate_keys=True)
        report = store.last_migration
        self.assertEqual(report.scanned, 1)
        self.assertEqual(report.migrated, 0)
        self.assertFalse(report.changed)
        self.assertEqual(read_state(path), before, "零迁移不许碰文件")
        self.assertEqual(backups(self.td), [])
        self.assertTrue(any("nothing to migrate" in line for line in logs.output),
                        logs.output)

    def test_missing_file_is_not_an_error(self):
        path = os.path.join(self.td, "state.json")
        store = StateStore(path, migrate_keys=True)
        self.assertEqual(store.all_sessions(), {})
        self.assertEqual(store.last_migration.migrated, 0)
        self.assertFalse(os.path.exists(path))

    def test_collision_is_logged_and_both_copies_survive(self):
        path = write_state(self.td, {
            "sessions": {"chat:55": "legacy", "telegram:55": "modern"}, "meta": {},
        })
        with self.assertLogs("opencode_bridge.state", level="WARNING") as logs:
            store = StateStore(path, migrate_keys=True)
        self.assertEqual(store.last_migration.collisions, 1)
        self.assertEqual(store.last_migration.migrated, 0)
        self.assertEqual(read_state(path)["sessions"],
                         {"chat:55": "legacy", "telegram:55": "modern"})
        self.assertEqual(backups(self.td), [], "零迁移就不该有备份")
        self.assertTrue(any("legacy form" in line for line in logs.output), logs.output)


class TestDurability(unittest.TestCase):
    """原子性 / 损坏文件 / 备份失败 —— "不许破坏用户的文件"。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.td = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def _legacy_doc(self):
        return {"sessions": {"chat:55": "ses_a"}, "meta": {"chat:55": {"k": "v"}}}

    def test_write_failure_leaves_original_file_intact(self):
        """落盘失败（``os.replace`` 抛错）⇒ 原文件逐字节不变、临时文件被清掉。"""
        path = write_state(self.td, self._legacy_doc())
        before = read_state(path)
        with mock.patch("opencode_bridge.state.os.replace",
                        side_effect=OSError("disk full")):
            with self.assertLogs("opencode_bridge.state", level="ERROR") as logs:
                store = StateStore(path, migrate_keys=True)

        self.assertEqual(read_state(path), before, "原文件不许被破坏")
        self.assertEqual(store.all_sessions(), {"telegram:55": "ses_a"})
        self.assertIsNotNone(store.last_migration.backup_path)
        self.assertEqual(read_state(store.last_migration.backup_path), before)
        self.assertTrue(any("intact" in line for line in logs.output), logs.output)
        # 临时文件必须被清理干净（否则目录越用越脏）
        leftovers = [n for n in os.listdir(self.td)
                     if n.startswith(".state-") and n.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_backup_failure_skips_the_rewrite_entirely(self):
        """备份失败 ⇒ 干脆不写。没有回滚路径就不该动用户的文件。"""
        path = write_state(self.td, self._legacy_doc())
        before = read_state(path)
        with mock.patch("opencode_bridge.state.shutil.copy2",
                        side_effect=OSError("read-only fs")):
            with self.assertLogs("opencode_bridge.state", level="ERROR") as logs:
                store = StateStore(path, migrate_keys=True)

        self.assertEqual(read_state(path), before)
        self.assertEqual(backups(self.td), [])
        self.assertIsNone(store.last_migration.backup_path)
        self.assertEqual(store.last_migration.migrated, 1)
        self.assertTrue(any("rollback" in line for line in logs.output), logs.output)

    def test_corrupt_file_is_neither_migrated_nor_overwritten(self):
        path = os.path.join(self.td, "state.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"sessions": {"chat:55": "ses_a"')  # 截断的 JSON
        with open(path, "rb") as fh:
            broken = fh.read()

        with self.assertLogs("opencode_bridge.state", level="ERROR") as logs:
            store = StateStore(path, migrate_keys=True)

        with open(path, "rb") as fh:  # 现场保持原样，用户能自己抢救
            self.assertEqual(fh.read(), broken)
        self.assertEqual(backups(self.td), [])
        self.assertEqual(store.all_sessions(), {})
        self.assertEqual(store.last_migration.migrated, 0)
        self.assertTrue(any("untouched" in line for line in logs.output),
                        logs.output)

    def test_non_object_document_is_left_alone(self):
        path = write_state(self.td, {})
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("[1, 2, 3]")
        with self.assertLogs("opencode_bridge.state", level="WARNING"):
            store = StateStore(path, migrate_keys=True)
        self.assertEqual(store.all_sessions(), {})
        self.assertEqual(backups(self.td), [])


class TestMigratedStoreBehaviour(unittest.TestCase):
    """迁移之后 store 本身照常工作 —— 尤其是适配器还没切前缀的那段窗口。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.td = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def test_legacy_key_still_resolves_after_migration(self):
        """键已迁到 ``telegram:``，而适配器还在用 ``chat:`` 查 —— 必须查得到。

        没有这条，迁移窗口内每个会话都会"查不到映射"→ 建新会话 → 用户看到的就是
        "agent 突然忘了一部分对话"，正是 A2b 要消灭的症状。
        """
        path = write_state(self.td, {"sessions": {"chat:55": "ses_a"}, "meta": {}})
        store = StateStore(path, migrate_keys=True)
        self.assertEqual(store.get_session("telegram:55"), "ses_a")
        self.assertEqual(store.get_session("chat:55"), "ses_a")
        self.assertEqual(store.all_sessions(), {"telegram:55": "ses_a"})

    def test_legacy_meta_still_resolves_after_migration(self):
        path = write_state(self.td, {
            "sessions": {}, "meta": {"room:!r:o": {"nick": "bob"}},
        })
        store = StateStore(path, migrate_keys=True)
        self.assertEqual(store.get_meta("matrix:!r:o", "nick"), "bob")
        self.assertEqual(store.get_meta("room:!r:o", "nick"), "bob")
        self.assertEqual(store.get_meta("matrix:!r:o", "absent", 7), 7)

    def test_ambiguous_key_gets_no_fallback(self):
        """``channel:`` 没有别名可回退 —— 回退本身绝不许变成一种猜测。"""
        path = write_state(self.td, {
            "sessions": {"slack:C1": "ses_s", "channel:C1": "ses_legacy"},
            "meta": {},
        })
        store = StateStore(path, migrate_keys=True)
        # 精确键优先：channel:C1 与 slack:C1 是两条独立记录
        self.assertEqual(store.get_session("channel:C1"), "ses_legacy")
        self.assertEqual(store.get_session("slack:C1"), "ses_s")

    def test_exact_key_wins_over_the_alias(self):
        path = write_state(self.td, {
            "sessions": {"chat:55": "legacy", "telegram:55": "modern"}, "meta": {},
        })
        store = StateStore(path, migrate_keys=True)
        self.assertEqual(store.get_session("chat:55"), "legacy")
        self.assertEqual(store.get_session("telegram:55"), "modern")

    def test_drop_session_removes_the_alias_too(self):
        path = write_state(self.td, {"sessions": {"chat:55": "ses_a"}, "meta": {}})
        store = StateStore(path, migrate_keys=True)
        store.drop_session("chat:55")          # 调用方仍传旧键
        self.assertEqual(store.all_sessions(), {})
        self.assertEqual(read_state(path)["sessions"], {})

    def test_writes_after_migration_round_trip(self):
        path = write_state(self.td, {"sessions": {"chat:55": "ses_a"}, "meta": {}})
        store = StateStore(path, migrate_keys=True)
        store.set_session("telegram:77", "ses_new")
        store.set_meta("telegram:77", "n", 1)
        reopened = StateStore(path, migrate_keys=True)
        self.assertEqual(reopened.get_session("telegram:77"), "ses_new")
        self.assertEqual(reopened.get_meta("telegram:77", "n"), 1)
        self.assertEqual(reopened.get_session("chat:55"), "ses_a")


class DormantSessionReconciliationTests(unittest.TestCase):
    """``state.json`` 里"登记过但没人服务"的 session 绑定：只读对账。

    ⚠️ 这些用例构造的孤儿必须**真的是孤儿** —— 经 :meth:`StateStore.set_session`
    写进映射、而活跃集合里没有对应 id。手搓一个字典再拿它和自己比，
    测的是 ``set`` 的字典序，不是这条路径。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.td = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self.td, "state.json")

    def store_with(self, bindings: dict) -> StateStore:
        """``bindings`` = ``{conversation_id: session_id}``，全部经 store 自己的写路径落盘。"""
        store = StateStore(self.path)
        for conversation_id, session_id in bindings.items():
            store.set_session(conversation_id, session_id)
        return store

    def all_registered_session_ids(self, store: StateStore) -> tuple:
        """``state.json`` 里登记过的全部 session id —— ⚠️ 是 ``all_sessions()`` 的**值**。

        ``all_sessions()`` 返回 ``{session_id: conversation_id}``（把盘上
        ``{conversation_id: session_id}`` 翻过来的**反向表**）⇒ 键是会话键、
        值是 session id。取错了方向，活跃集合与它永不相交 ⇒ 孤儿数恰好等于 dormant 数，
        看起来"有产出"，其实一条都没对上。
        """
        return tuple(store.all_sessions().values())

    def disk_snapshot(self) -> tuple:
        with open(self.path, "rb") as handle:
            payload = handle.read()
        return os.stat(self.path).st_mtime_ns, payload

    def test_no_orphans_when_every_dormant_id_is_active(self):
        """dormant 全部出现在活跃集合里 ⇒ 孤儿为空集（不是"少几个"，是**零**）。

        这是"零告警"的判据：一条永远报不出零的对账，读者会学会忽略它。
        """
        store = self.store_with({
            "telegram:55": "ses_active_one",
            "telegram:56": "ses_active_two",
        })
        dormant = self.all_registered_session_ids(store)
        report = reconcile_dormant_sessions(dormant, dormant)
        self.assertEqual(report.dormant, tuple(sorted(dormant)))
        self.assertEqual(report.active, len(set(dormant)))
        self.assertEqual(set(report.orphans), set())
        self.assertEqual(report.orphan_count, 0)

    def test_a_single_orphan_is_named_in_the_report(self):
        """一条孤儿 ⇒ 报告点名它，且 ``active`` 数的是**调用方给的那批**。"""
        store = self.store_with({
            "telegram:55": "ses_active_one",
            "telegram:56": "ses_dropped_one",
        })
        report = reconcile_dormant_sessions(
            self.all_registered_session_ids(store), ["ses_active_one"])
        self.assertEqual(report.orphans, ("ses_dropped_one",))
        self.assertEqual(report.dormant_count, 2)
        self.assertEqual(report.active, 1)
        self.assertIn("ses_dropped_one", report.summary())
        self.assertEqual(report.orphans, tuple(sorted(report.orphans)))

    def test_empty_active_set_makes_every_dormant_id_an_orphan(self):
        """重启之后、什么都还没重新登记 ⇒ 活跃集合为空 ⇒ **一条都不许漏**。

        这个形状最容易"伪干净"：调用方没有活跃 id 可传，若实现把
        ``dormant - {}`` 悄悄当成空集，日志会报告零孤儿而映射里全是死的。
        """
        store = self.store_with({
            "telegram:55": "ses_hero_from_before",
            "telegram:56": "ses_hero_from_before",
            "matrix:!room:example.org": "ses_survivor",
        })
        dormant = self.all_registered_session_ids(store)
        report = reconcile_dormant_sessions(dormant, [])
        self.assertEqual(report.active, 0)
        self.assertEqual(set(report.orphans), set(dormant))
        self.assertEqual(report.orphan_count, report.dormant_count)
        self.assertEqual(len(dormant), 3)

    def test_reconciliation_never_writes_state_json(self):
        """⚠️ 这条守的是「无写副作用」本身用它自己的形状，不是靠代码读起来没写。

        判据：磁盘上那份 ``state.json`` 的 **mtime 与字节**在调用前后完全一致，
        且调用之后 :meth:`StateStore.all_sessions` 读回的映射一条都没少 ——
        如果实现真的顺手 ``drop_session``，字节会变（或至少映射会变），这里会红。
        """
        store = self.store_with({
            "telegram:55": "ses_keep_one",
            "telegram:56": "ses_orphan_one",
        })
        before_mtime, before_bytes = self.disk_snapshot()
        before_map = store.all_sessions()

        report = reconcile_dormant_sessions(
            self.all_registered_session_ids(store), ["ses_keep_one"])

        after_mtime, after_bytes = self.disk_snapshot()
        self.assertEqual(report.orphans, ("ses_orphan_one",))
        self.assertEqual(after_bytes, before_bytes, "state.json 的字节必须逐字节不变")
        self.assertEqual(after_mtime, before_mtime, "state.json 的 mtime 不许变")
        self.assertEqual(store.all_sessions(), before_map, "映射里一条都不许少")

    def test_reconciliation_report_is_logged_as_exactly_one_info_record(self):
        """每轮对账**恰好一行 INFO**、零 WARNING/ERROR —— 这是本功能的全部产出。

        ``reconcile_dormant_sessions`` 是纯函数、**自己不写日志**（它没有 logger，
        也不该有：那样一次调用会在不知道 cadence 的地方冒出一行）。
        所以这里钉的是**设计上唯一的那条打点语句**的形状：
        ``logger.info("%s", report.summary())`` —— 一个 %s、一行、零告警。
        """
        store = self.store_with({
            "telegram:55": "ses_keep_one",
            "telegram:56": "ses_orphan_one",
            "telegram:57": "ses_orphan_two",
        })
        report = reconcile_dormant_sessions(
            self.all_registered_session_ids(store), ["ses_keep_one"])

        with self.assertNoLogs("opencode_bridge.state", level="WARNING"):
            with self.assertLogs("opencode_bridge.state", level="INFO") as captured:
                log_dormant_session_reconciliation(
                    report, logging.getLogger("opencode_bridge.state"))

        self.assertEqual(len(captured.records), 1)
        record = captured.records[0]
        self.assertEqual(record.levelno, logging.INFO)
        self.assertEqual(record.name, "opencode_bridge.state")
        self.assertIn("dormant=3", record.getMessage())
        self.assertIn("active=1", record.getMessage())
        self.assertIn("orphans=2", record.getMessage())
        orphan_part = record.getMessage().split("orphan_ids=")[1]
        self.assertIn("ses_orphan_one", orphan_part)
        self.assertIn("ses_orphan_two", orphan_part)
        self.assertNotIn("ses_keep_one", orphan_part)
        self.assertNotIn("\n", record.getMessage())

    def test_multiple_orphans_match_exactly_and_are_order_stable(self):
        """多孤儿 ⇒ 集合**恰好相等**（不多不少），且与入参顺序无关。"""
        store = self.store_with({
            "telegram:55": "ses_keep_one",
            "telegram:56": "ses_orphan_beta",
            "telegram:57": "ses_orphan_alpha",
            "telegram:58": "ses_orphan_gamma",
        })
        expected = {"ses_orphan_alpha", "ses_orphan_beta", "ses_orphan_gamma"}
        dormant = self.all_registered_session_ids(store)
        first = reconcile_dormant_sessions(dormant, ["ses_keep_one"])
        reversed_dormant = list(reversed(dormant))
        second = reconcile_dormant_sessions(reversed_dormant, ["ses_keep_one"])
        self.assertEqual(set(first.orphans), expected)
        self.assertEqual(set(second.orphans), expected)
        self.assertEqual(len(first.orphans), len(expected))
        self.assertEqual(len(second.orphans), len(expected))
        self.assertEqual(first.orphans, second.orphans)
        self.assertEqual(first.orphans, tuple(sorted(first.orphans)))


class TestMigrationReportDefaults(unittest.TestCase):
    def test_default_report_is_inert(self):
        report = MigrationReport()
        self.assertFalse(report.changed)
        self.assertIsNone(report.backup_path)
        self.assertEqual(report.migrated, 0)
        self.assertIn("migrated=0", report.summary())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
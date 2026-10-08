"""运行期凭据失效记录器（``credential_health.CredentialFailureRecorder``）的回归护栏。

它钉的契约（每一条都有「什么情况下它会红」）：

* **状态变化时才写盘** —— 同一次失效连击里第二次观测**不重写**（否则凭据失效
  每轮都观测到一次，``--status`` 的时间戳会跟着轮询节拍刷）。
* **写盘失败不造记录** —— 那次观测只进内存（:attr:`_unwritten`），恢复时
  **不**给一条盘上不存在的失效盖 ``recovered_at``（那是编造）。
* **读路径不捏造时刻** —— 盘上条目缺 ``at`` 就读出 ``None``，⛔ 不是 ``time.time()``
  （写路径补“现在”是对的，读路径补就是编造）。
* **``code`` 恒在、``int`` 原样** —— ``401`` 要能让 ``--status`` 显示 ``code=401``
  那个数字，缺信息用 ``"?"`` 占位而不是缺键。
* **坏文件不崩 ``--status``** —— 记 warning、当它没有记录。

⚠️ 判据纪律与 :mod:`tests.test_outbound_failure_channel` 同源：没有记录 ≠ 正常，
「无记录」的措辞由 :data:`credential_health.NO_CREDENTIAL_FAILURE_TEXT` 承担。
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import time
import unittest

from opencode_bridge import credential_health


class RecorderTestCase(unittest.TestCase):
    """共用一间临时 ``bridge_dir`` 的基类（每条用例各自新建记录器）。"""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.bridge_dir = self._directory.name
        self.failures_path = os.path.join(
            self.bridge_dir, credential_health.CREDENTIAL_FAILURES_FILE_NAME
        )

    def read_entry(self, platform: str):
        record = credential_health.read_credential_failures(self.bridge_dir)
        return credential_health.credential_failure_from_record(record, platform)


class FirstFailureIsPersisted(RecorderTestCase):
    def test_first_failure_is_written_with_int_code_preserved(self):
        recorder = credential_health.CredentialFailureRecorder(self.bridge_dir)
        self.assertTrue(recorder.note_failure("telegram", 401, "Unauthorized"))
        entry = self.read_entry("telegram")
        self.assertIsNotNone(entry)
        # ⛔ int 原样保留：`--status` 要显示 code=401 那个数字，str 会丢掉这条判据
        self.assertEqual(entry["code"], 401)
        self.assertNotIsInstance(entry["code"], str)
        self.assertIsInstance(entry["at"], float)
        self.assertNotIn("recovered_at", entry)

    def test_empty_platform_key_is_rejected(self):
        recorder = credential_health.CredentialFailureRecorder(self.bridge_dir)
        self.assertFalse(recorder.note_failure("", 401))
        self.assertFalse(recorder.note_recovery(""))
        self.assertFalse(os.path.exists(self.failures_path))


class FailureStreakWritesOncePerStreak(RecorderTestCase):
    def test_repeated_failure_in_same_streak_does_not_rewrite(self):
        recorder = credential_health.CredentialFailureRecorder(self.bridge_dir)
        self.assertTrue(recorder.note_failure("telegram", 401, "第一次"))
        first_at = self.read_entry("telegram")["at"]
        # 第二次观测（detail 换了也不该触发重写：记录已经写着"在失效"）
        self.assertFalse(recorder.note_failure("telegram", 401, "第二次"))
        self.assertEqual(self.read_entry("telegram")["at"], first_at)
        self.assertEqual(recorder.failing_platforms(), ("telegram",))


class RecoveryStampsOnlyARealFailure(RecorderTestCase):
    def test_recovery_without_failure_writes_nothing(self):
        recorder = credential_health.CredentialFailureRecorder(self.bridge_dir)
        self.assertFalse(recorder.note_recovery("telegram"))
        self.assertFalse(os.path.exists(self.failures_path))

    def test_recovery_after_failure_stamps_recovered_at(self):
        recorder = credential_health.CredentialFailureRecorder(self.bridge_dir)
        self.assertTrue(recorder.note_failure("telegram", 401, "被吊销"))
        self.assertTrue(recorder.note_recovery("telegram"))
        entry = self.read_entry("telegram")
        self.assertIsInstance(entry["recovered_at"], float)
        self.assertGreaterEqual(entry["recovered_at"], entry["at"])

    def test_recovery_does_not_fabricate_when_disk_entry_is_gone(self):
        recorder = credential_health.CredentialFailureRecorder(self.bridge_dir)
        self.assertTrue(recorder.note_failure("telegram", 401))
        os.remove(self.failures_path)
        # 盘上没有 = 没有可盖戳的观测：⛔ 不造，而且连击状态一并让出
        self.assertFalse(recorder.note_recovery("telegram"))
        self.assertFalse(os.path.exists(self.failures_path))
        # 让出之后下一次失效会重新写（新连击）
        self.assertTrue(recorder.note_failure("telegram", 401))


class WriteFailureStillObserves(RecorderTestCase):
    """写盘失败注入：``bridge_dir`` 里那层子目录先不存在（落盘必失败）。"""

    def setUp(self) -> None:
        super().setUp()
        self.unwritable_dir = os.path.join(self.bridge_dir, "并不存在的目录")
        self.unwritable_failures_path = os.path.join(
            self.unwritable_dir, credential_health.CREDENTIAL_FAILURES_FILE_NAME
        )

    def test_unwritable_directory_keeps_observation_in_memory_not_on_disk(self):
        recorder = credential_health.CredentialFailureRecorder(self.unwritable_dir)
        with self.assertLogs("opencode_bridge.credential_health", level="WARNING"):
            self.assertFalse(recorder.note_failure("telegram", 401))
        # failing_platforms 并上 _unwritten：刚失效过但写不下去，不许说成没失效
        self.assertEqual(recorder.failing_platforms(), ("telegram",))
        self.assertFalse(os.path.exists(self.unwritable_failures_path))

    def test_same_streak_does_not_retry_the_write(self):
        recorder = credential_health.CredentialFailureRecorder(self.unwritable_dir)
        self.assertFalse(recorder.note_failure("telegram", 401))
        # 同一次连击里的重复观测不再付一遍写盘的钱
        self.assertFalse(recorder.note_failure("telegram", 401))
        self.assertFalse(os.path.exists(self.unwritable_failures_path))

    def test_recovery_clears_unwritten_mark_without_writing(self):
        recorder = credential_health.CredentialFailureRecorder(self.unwritable_dir)
        self.assertFalse(recorder.note_failure("telegram", 401))
        self.assertFalse(recorder.note_recovery("telegram"))
        self.assertEqual(recorder.failing_platforms(), ())
        self.assertFalse(os.path.exists(self.unwritable_failures_path))

    def test_next_streak_retries_write_once_directory_exists(self):
        recorder = credential_health.CredentialFailureRecorder(self.unwritable_dir)
        self.assertFalse(recorder.note_failure("telegram", 401))
        os.makedirs(self.unwritable_dir)
        # ⚠️ 连击的边界是「恢复观测」（_unwritten 只由 note_recovery 清）：
        # 不先观测到恢复，下一次 note_failure 仍属同一次连击、不重试写
        self.assertFalse(recorder.note_recovery("telegram"))
        self.assertTrue(recorder.note_failure("telegram", 401))
        self.assertTrue(os.path.exists(self.unwritable_failures_path))


class ReadModifyWriteKeepsOtherPlatforms(RecorderTestCase):
    def test_other_platform_entries_survive_and_stale_recovery_is_dropped(self):
        recorder = credential_health.CredentialFailureRecorder(self.bridge_dir)
        self.assertTrue(recorder.note_failure("telegram", 401))
        self.assertTrue(recorder.note_failure("discord", 403))
        self.assertTrue(recorder.note_recovery("telegram"))
        # 别的平台那条没被 telegram 的读改写覆盖掉
        discord_entry = self.read_entry("discord")
        self.assertEqual(discord_entry["code"], 403)
        self.assertNotIn("recovered_at", discord_entry)
        # telegram 又失效 ⇒ 新条目不许背着上一连击的 recovered_at
        self.assertTrue(recorder.note_failure("telegram", 401))
        self.assertNotIn("recovered_at", self.read_entry("telegram"))
        # discord 那条在 telegram 的两次读改写之后仍然在盘上
        self.assertEqual(self.read_entry("discord")["code"], 403)


class ReadPathDoesNotFabricate(RecorderTestCase):
    def test_missing_at_is_read_back_as_none_not_as_now(self):
        hand_written = {
            "recorded_at": time.time(),
            "platforms": {"telegram": {"code": 401}},
        }
        with io.open(self.failures_path, "w", encoding="utf-8") as handle:
            json.dump(hand_written, handle)
        entry = self.read_entry("telegram")
        self.assertIsNotNone(entry)
        # ⛔ 读路径不补 time.time()：缺 at 就是 None（补了就是编造失效时刻）
        self.assertIsNone(entry["at"])

    def test_bad_json_reads_back_none_with_warning(self):
        with io.open(self.failures_path, "w", encoding="utf-8") as handle:
            handle.write("这不是 JSON")
        with self.assertLogs("opencode_bridge.credential_health", level="WARNING"):
            record = credential_health.read_credential_failures(self.bridge_dir)
        self.assertIsNone(record)

    def test_non_dict_document_reads_back_none(self):
        with io.open(self.failures_path, "w", encoding="utf-8") as handle:
            json.dump([1, 2, 3], handle)
        self.assertIsNone(credential_health.read_credential_failures(self.bridge_dir))
        self.assertEqual(
            credential_health.credential_failures_in_record(None), ()
        )
        self.assertIsNone(credential_health.credential_failure_recorded_at(None))


class NormalizationShapes(unittest.TestCase):
    def test_code_none_becomes_placeholder_not_missing_key(self):
        entry = credential_health.normalize_credential_failure(None, "x")
        # ⛔ 缺信息要说出来（"?"），不靠缺键蒙混 —— 消费方按 entry["code"] 取
        self.assertEqual(entry["code"], "?")

    def test_code_bool_is_stringified_not_treated_as_int(self):
        self.assertEqual(credential_health.normalize_credential_failure(True)["code"], "True")

    def test_code_int_is_preserved_as_int(self):
        self.assertEqual(credential_health.normalize_credential_failure(401)["code"], 401)

    def test_explicit_at_is_honored_and_unparseable_at_becomes_none(self):
        self.assertEqual(
            credential_health.normalize_credential_failure(401, at=123.5)["at"], 123.5
        )
        self.assertIsNone(
            credential_health.normalize_credential_failure(401, at="不是时刻")["at"]
        )

    def test_multiline_detail_is_flattened_to_one_line(self):
        entry = credential_health.normalize_credential_failure(401, "  头一行\n第二行 \n 第三行 ")
        self.assertEqual(entry["detail"], "头一行 第二行 第三行")

    def test_overlong_detail_is_truncated_at_300_chars(self):
        entry = credential_health.normalize_credential_failure(401, "配" * 400)
        self.assertEqual(len(entry["detail"]), 301)
        self.assertTrue(entry["detail"].endswith("…"))


class DescribeWording(unittest.TestCase):
    def test_401_renders_as_forbidden(self):
        self.assertEqual(
            credential_health.describe_credential_failure({"code": 401}), "forbidden"
        )
        self.assertEqual(
            credential_health.describe_credential_failure({"code": 401, "detail": "被吊销"}),
            "forbidden —— 被吊销",
        )

    def test_non_401_code_renders_as_code_equals(self):
        self.assertEqual(
            credential_health.describe_credential_failure({"code": "?"}), "code=?"
        )
        self.assertEqual(
            credential_health.describe_credential_failure({"code": 403, "detail": "x"}),
            "code=403 —— x",
        )

    def test_missing_entry_renders_no_record_text(self):
        self.assertEqual(
            credential_health.describe_credential_failure(None),
            credential_health.NO_CREDENTIAL_FAILURE_TEXT,
        )


class InstallRoundtrip(unittest.TestCase):
    def test_installed_recorder_is_returned_until_uninstalled(self):
        # 进程级槽位：先注册清理，保证这条用例不把记录器泄漏给同进程的其他用例
        self.addCleanup(credential_health.install_credential_failure_recorder, None)
        recorder = credential_health.CredentialFailureRecorder(".")
        credential_health.install_credential_failure_recorder(recorder)
        self.assertIs(credential_health.installed_credential_failure_recorder(), recorder)
        credential_health.install_credential_failure_recorder(None)
        self.assertIsNone(credential_health.installed_credential_failure_recorder())


if __name__ == "__main__":
    unittest.main()

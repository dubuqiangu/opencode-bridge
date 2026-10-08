"""``--status`` 的「上次凭据失效（运行期记录）」那一段的回归护栏。

它钉的是**拍板一**的用户可见面：``--status`` 必须把「启动失败」（platform-health.json，
``start()`` 返回那一刻定稿）与「**运行期**凭据失效」（credential-failures.json，轮询线程
观测到 401 的那一刻写）**分开答** —— 启动时是好的、运行中被吊销，两段要分别说，
谁也不许盖住谁。

## 判据纪律（与 :class:`tests.test_outbound_failure_channel.StatusSectionWording` 同源）

* ⚠️ **必须逐段取**：``--status`` 其余部分本来就会出现「失败」「正常」等字样
  （渠道配置、上次启动探测），整页断言会让「无记录不许说正常」恒真。
* ⚠️ 全程不联网、纯读盘 —— ``--status`` 必须是"网络坏了也能看"的那条路。
* ⚠️ 无记录 ⇒ :data:`credential_health.NO_CREDENTIAL_FAILURE_TEXT`，
  ⛔ 既不说「正常」也不说「失效」。
* ⚠️ 盘上**任何**键都要能成行（拼错的 ``telegramm`` 也要露脸 —— 它是唯一能解释
  「为什么没看到失效」的线索）。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest

from opencode_bridge import __main__ as cli
from opencode_bridge import credential_health, health
from opencode_bridge.config import Config


class StatusCredentialFailureSectionTestCase(unittest.TestCase):
    """照抄出站失败段测试的 harness：临时目录 + ``OPENCODE_BRIDGE_CONFIG``。"""

    SECTION_HEADER = cli._CREDENTIAL_FAILURE_SECTION_HEADER
    STARTUP_SECTION_HEADER = "== 上次启动时的探测结论 =="

    def setUp(self) -> None:
        self._previous = os.environ.get("OPENCODE_BRIDGE_CONFIG")
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.bridge_dir = self._directory.name
        config_path = os.path.join(self.bridge_dir, "config.json")
        with io.open(config_path, "w", encoding="utf-8") as handle:
            json.dump({"adapters": {}}, handle)
        os.environ["OPENCODE_BRIDGE_CONFIG"] = config_path
        self.addCleanup(self._restore_config_env)

    def _restore_config_env(self) -> None:
        if self._previous is None:
            os.environ.pop("OPENCODE_BRIDGE_CONFIG", None)
        else:
            os.environ["OPENCODE_BRIDGE_CONFIG"] = self._previous

    def render(self) -> str:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            cli.run_status(Config(adapters={"telegram": {"bot_token": "t"}}))
        return buffer.getvalue()

    def section(self, header: str) -> str:
        """只取指定那一段（到下一个 ``== `` 段头为止）。

        ⚠️ ``--status`` 的其余部分（渠道配置、上次启动探测）本来就会出现
        「失败」「正常」等字样，整页断言会把「这一段不许说什么」变成恒真。
        """
        lines = self.render().splitlines()
        start = next(i for i, line in enumerate(lines) if header in line)
        tail = lines[start + 1:]
        end = next((i for i, line in enumerate(tail) if line.startswith("== ")), len(tail))
        return "\n".join(tail[:end])

    def credential_section(self) -> str:
        return self.section(self.SECTION_HEADER)

    def startup_section(self) -> str:
        return self.section(self.STARTUP_SECTION_HEADER)

    def row_for(self, platform_label: str, body: str) -> str:
        for line in body.splitlines():
            if line.strip().startswith(platform_label):
                return line
        raise AssertionError(
            "那段输出里找不到 %s 那一行：\n%s" % (platform_label, body)
        )

    def note_failure_on_disk(self, platform: str, code, detail: str = "") -> None:
        """用生产写入方把一条运行期失效真落到盘上（``--status`` 只读盘）。"""
        recorder = credential_health.CredentialFailureRecorder(self.bridge_dir)
        if not recorder.note_failure(platform, code, detail):
            raise AssertionError("写盘失败：%s 那条失效没落下去" % platform)


class NoRecordSaysNoRecord(StatusCredentialFailureSectionTestCase):
    def test_no_record_says_no_record_and_never_says_normal(self):
        body = self.credential_section()
        # 已配置的 telegram 那一行落的是「无记录」逐字文案
        self.assertIn(credential_health.NO_CREDENTIAL_FAILURE_TEXT, body)
        self.assertNotIn("正常", body)
        self.assertNotIn("运行期失效", body)
        self.assertNotIn("forbidden", body)
        self.assertNotIn("记录时间", body)


class UnrecoveredFailureIsNamed(StatusCredentialFailureSectionTestCase):
    def test_unrecovered_failure_names_code_and_claims_no_recovery(self):
        self.note_failure_on_disk("telegram", 401, "被吊销")
        body = self.credential_section()
        row = self.row_for("Telegram", body)
        self.assertIn("运行期失效", row)
        self.assertIn("forbidden", row)
        self.assertIn("此后没有观测到 getMe 通过", row)
        self.assertNotIn(credential_health.NO_CREDENTIAL_FAILURE_TEXT, row)

    def test_recorded_at_line_names_the_write_moment(self):
        self.note_failure_on_disk("telegram", 401)
        body = self.credential_section()
        self.assertIn("记录时间", body)
        self.assertIn("这份文件最后一次被写入的时刻，不是失效发生的时刻", body)


class RecoveredFailureSaysRecoveredAt(StatusCredentialFailureSectionTestCase):
    def test_recovered_failure_says_recovered_at(self):
        recorder = credential_health.CredentialFailureRecorder(self.bridge_dir)
        self.assertTrue(recorder.note_failure("telegram", 401, "被吊销"))
        self.assertTrue(recorder.note_recovery("telegram"))
        row = self.row_for("Telegram", self.credential_section())
        self.assertIn("运行期失效", row)
        self.assertIn("已恢复于", row)
        self.assertNotIn("此后没有观测到 getMe 通过", row)


class StartupAndRuntimeAreAnsweredSeparately(StatusCredentialFailureSectionTestCase):
    """**拍板一的主测**：启动失败与运行期失效是两份记录，各答各的。"""

    def test_startup_ok_plus_runtime_failure_shows_both_in_own_sections(self):
        # 启动那一刻是好的（platform-health.json）……
        health.record_startup_probes(
            self.bridge_dir, {"telegram": {"verdict": health.VERDICT_OK}}
        )
        # ……启动之后凭据被吊销（credential-failures.json）
        self.note_failure_on_disk("telegram", 401, "运行中被吊销")

        startup = self.startup_section()
        runtime = self.credential_section()

        # 启动段照实说「上次启动那一刻 ok」，⛔ 不替运行期失效说话
        self.assertIn("正常", startup)
        self.assertNotIn("运行期失效", startup)
        self.assertNotIn("forbidden", startup)
        # 运行期段照实说被吊销，⛔ 不被「上次启动正常」盖住
        self.assertIn("运行期失效", runtime)
        self.assertIn("forbidden", runtime)
        self.assertNotIn("正常", runtime)


class UnregisteredKeyStillGetsARow(StatusCredentialFailureSectionTestCase):
    def test_unregistered_key_still_gets_a_row(self):
        # 用户把 telegram 拼错了：那条记录是唯一能解释「为什么没看到失效」的线索
        self.note_failure_on_disk("telegramm", 401)
        body = self.credential_section()
        unregistered_row = self.row_for("telegramm", body)
        self.assertIn("forbidden", unregistered_row)
        self.assertIn("运行期失效", unregistered_row)


class MissingAtIsSaidNotHidden(StatusCredentialFailureSectionTestCase):
    def test_entry_without_at_says_unrecorded_moment(self):
        hand_written = {
            "recorded_at": 1_700_000_000.0,
            "platforms": {"telegram": {"code": 401}},
        }
        failures_path = os.path.join(
            self.bridge_dir, credential_health.CREDENTIAL_FAILURES_FILE_NAME
        )
        with io.open(failures_path, "w", encoding="utf-8") as handle:
            json.dump(hand_written, handle)
        row = self.row_for("Telegram", self.credential_section())
        # ⛔ 缺信息要出声，不靠不显示蒙混；⛔ 读路径也不许补一个编造的时刻
        self.assertIn("未记录失效时刻", row)
        self.assertIn("运行期失效", row)


if __name__ == "__main__":
    unittest.main()

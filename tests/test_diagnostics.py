"""`diagnostics` 模块的测试（G1 取证）。

重点锁一条**很容易写错、后果很严重**的行为：注册信号处理器时必须
`chain=True`。若用 `chain=False`，dump 完就不再走默认处理器，
SIGTERM / SIGINT 之后**进程不会退出** —— 插件将杀不掉这个桥、Ctrl+C 也会失效。
取证绝不能改变被观察对象的行为，所以这条用**真实子进程发信号**来验证。
"""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opencode_bridge.diagnostics import (  # noqa: E402
    LIFECYCLE_FILENAME,
    MAX_LEDGER_BYTES,
    STACK_DUMP_FILENAME,
    ProcessDiagnostics,
    describe_environment,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read_ledger(directory):
    path = os.path.join(directory, LIFECYCLE_FILENAME)
    if not os.path.isfile(path):
        return []
    rows = []
    with io.open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


class LedgerTests(unittest.TestCase):
    def test_record_writes_parseable_json_with_expected_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            diagnostics = ProcessDiagnostics(temp_dir)
            diagnostics.record("stopped", detail="unit-test")
            rows = read_ledger(temp_dir)
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["reason"], "stopped")
            self.assertEqual(row["pid"], os.getpid())
            self.assertEqual(row["detail"], "unit-test")
            self.assertGreaterEqual(row["durationSeconds"], 0)
            self.assertGreaterEqual(row["threadCount"], 1)
            self.assertIn("startedAt", row)
            self.assertIn("endedAt", row)

    def test_each_record_is_one_jsonl_line(self):
        """多行追加后仍能逐行解析——否则事后无法机器分析。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            diagnostics = ProcessDiagnostics(temp_dir)
            for index in range(5):
                diagnostics.record("cycle-%d" % index)
            rows = read_ledger(temp_dir)
            self.assertEqual([r["reason"] for r in rows],
                             ["cycle-%d" % i for i in range(5)])

    def test_ledger_rolls_over_instead_of_growing_forever(self):
        """诊断文件不该把磁盘吃满。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, LIFECYCLE_FILENAME)
            with io.open(path, "w", encoding="utf-8") as handle:
                handle.write("x" * (MAX_LEDGER_BYTES + 10))
            ProcessDiagnostics(temp_dir).record("after-cap")
            self.assertTrue(
                os.path.exists(path + ".1"),
                "超过上限应滚动出 .1，而不是继续追加",
            )

    def test_dump_stacks_writes_a_trace_containing_our_own_frame(self):
        """栈转储必须真的抓到线程栈——否则取证形同虚设。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            diagnostics = ProcessDiagnostics(temp_dir)
            diagnostics.install()
            try:
                diagnostics.dump_stacks("unit-test")
            finally:
                # 必须在 with 块**内**关闭：`faulthandler` 持有该文件的句柄，
                # 而 TemporaryDirectory 在 with 块结束就删目录——那时句柄还开着，
                # Windows 上会 `WinError32另一个程序正在使用此文件`。
                # （早先用 addCleanup 是错的：它的回调在 with 块之后才跑。）
                diagnostics.close()

            stack_path = os.path.join(temp_dir, STACK_DUMP_FILENAME)
            self.assertTrue(os.path.isfile(stack_path))
            with io.open(stack_path, encoding="utf-8", errors="replace") as handle:
                content = handle.read()
            self.assertIn("unit-test", content)
            self.assertIn("Current thread", content)

    def test_describe_environment_mentions_python_and_cwd(self):
        text = describe_environment()
        self.assertIn("python=", text)
        self.assertIn("cwd=", text)


class NeverBreaksTheMainFlowTests(unittest.TestCase):
    """诊断自身出错**绝不能**变成新的故障源。"""

    def test_record_survives_unwritable_directory(self):
        # 指向一个一定不可写的路径（把文件名放在一个"文件"下面）
        with tempfile.TemporaryDirectory() as temp_dir:
            blocker = os.path.join(temp_dir, "blocker")
            with io.open(blocker, "w", encoding="utf-8") as handle:
                handle.write("not a directory")
            diagnostics = ProcessDiagnostics(os.path.join(blocker, "sub"))
            diagnostics.record("should-not-raise")   # 不抛异常即通过
            diagnostics.dump_stacks("should-not-raise")

    def test_close_is_idempotent_and_safe_without_install(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            diagnostics = ProcessDiagnostics(temp_dir)
            diagnostics.close()
            diagnostics.close()   # 再来一次不能抛


class SignalHandlingTests(unittest.TestCase):
    """用**真实子进程发信号**验证：装了诊断之后，进程仍然会被信号正常终止。

    这是 `chain=True` 与 `chain=False` 的唯一可观察差别，而 `chain=False`
    会让插件杀不掉桥、Ctrl+C 失效——必须用行为测试锁住，不能只读代码。
    """

    def _run_child(self, body, timeout=30):
        script = (
            "import sys, os, signal, time\n"
            "sys.path.insert(0, %r)\n"
            "from opencode_bridge.diagnostics import ProcessDiagnostics\n"
            "d = ProcessDiagnostics(%r)\n"
            "d.install()\n"
            "%s\n"
        ) % (REPO_ROOT, body["diagnostics_dir"], body["code"])
        return subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )

    @unittest.skipIf(os.name != "nt", "这条针对 Windows 的信号行为")
    def test_sigterm_still_terminates_the_process(self):
        """装了诊断之后，SIGTERM 必须仍然能终止进程（chain=True 的保证）。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            script = (
                "import os, signal, sys\n"
                "sys.stdout.write('ready\\n'); sys.stdout.flush()\n"
                "os.kill(os.getpid(), signal.SIGTERM)\n"
                "time.sleep(30)\n"
            )
            result = self._run_child({"diagnostics_dir": temp_dir, "code": script})
            # 被 SIGTERM 终止 => 非零返回码（Windows 上是 status 1/0xC000013A 等）
            self.assertNotEqual(
                result.returncode, 0,
                "SIGTERM 之后进程竟然正常退出了，说明用了 chain=False —— "
                "插件将杀不掉这个桥",
            )
            # 而且栈应该已经落盘
            self.assertTrue(
                os.path.isfile(os.path.join(temp_dir, STACK_DUMP_FILENAME)),
                "SIGTERM 应触发栈转储（chain=True 才会 dump 后再走默认处理器）",
            )

    @unittest.skipIf(os.name != "nt", "这条针对 Windows 的信号行为")
    def test_ctrl_c_style_interrupt_still_works(self):
        """SIGINT（Ctrl+C）也必须仍然生效。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            script = (
                "import os, signal, sys, time\n"
                "os.kill(os.getpid(), signal.SIGINT)\n"
                "time.sleep(30)\n"
            )
            result = self._run_child({"diagnostics_dir": temp_dir, "code": script})
            self.assertNotEqual(
                result.returncode, 0,
                "SIGINT 之后进程竟然正常退出了，说明用了 chain=False",
            )


if __name__ == "__main__":
    unittest.main()
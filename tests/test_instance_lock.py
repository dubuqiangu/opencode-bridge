"""``instance_lock`` 的行为测试。

重点覆盖那些"想当然会写错"的分支：
- 失效锁（持有者已死）必须能被接管，否则一次强杀就留下永久死锁
- 锁文件损坏 / 为空必须能被接管
- ``release`` 只删自己那份，不能删掉后来接管者的
- **持有者真的活着时**必须拒绝 —— 这条用一个**真实子进程**的 pid，
  因为用假 pid 测的是 mock 不是行为
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opencode_bridge.instance_lock import (  # noqa: E402
    MAX_LOCK_AGE_SECONDS,
    InstanceLock,
    pid_is_alive,
)


class PidIsAliveTests(unittest.TestCase):
    def test_own_process_is_alive(self):
        self.assertTrue(pid_is_alive(os.getpid()))

    def test_non_positive_pid_is_not_alive(self):
        self.assertFalse(pid_is_alive(0))
        self.assertFalse(pid_is_alive(-1))

    def test_actually_running_subprocess_is_detected(self):
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            # 给它一点时间真正起来，否则可能还没 spawn 完成
            time.sleep(0.3)
            self.assertTrue(pid_is_alive(child.pid))
        finally:
            child.terminate()
            child.wait(timeout=10)

    def test_detects_process_we_do_not_own(self):
        """必须能判活**不是自己子进程**的进程 —— 生产里失败的正是这一种。

        这条测试是补上一个真实漏网的 bug：原来的实现用 `os.kill(pid, 0)`，
        它对"当前进程派生的子/孙进程"恰好能探活，所以单测一直是绿的；
        但对**由 PowerShell Start-Process 拉起、经 venv launcher 两层启动的桥进程**，
        实测抛 `WinError 87 参数错误` -> "活着"被误判成"死了" ->
        单实例守卫永远放行 -> 用户收到两条一模一样的回复。

        原来的测试用 `subprocess.Popen`，那恰好是**能工作**的那种进程关系，
        于是测试全绿而生产失败。这里刻意换成"非自己子进程"来复现。

        实现注意：被启动的代码**写进临时 .py 文件**再执行。
        早先一版把代码塞进 `Start-Process -ArgumentList '-c','...'`，被 PowerShell
        的引号规则拆坏，进程**根本没起来**（tasklist 查不到），
        于是两种实现都报"死了"——测试看着通过，其实什么也没验证到。
        """
        if os.name != "nt":
            self.skipTest("这条针对 Windows 的 OpenProcess 路径")

        with tempfile.TemporaryDirectory() as temp_dir:
            sleeper = os.path.join(temp_dir, "sleeper.py")
            with io.open(sleeper, "w", encoding="utf-8") as handle:
                handle.write("import time\ntime.sleep(60)\n")

            launcher = subprocess.Popen(
                [
                    "powershell", "-NoProfile", "-Command",
                    "$p = Start-Process -FilePath '%s' -ArgumentList '%s' "
                    "-PassThru -WindowStyle Hidden; $p.Id"
                    % (sys.executable, sleeper),
                ],
                stdout=subprocess.PIPE, text=True,
            )
            try:
                detached_pid = int(launcher.stdout.readline().strip())
                time.sleep(1.2)

                # 先确认它真的活着，否则本测试什么也没验证到
                listing = subprocess.run(
                    ["tasklist", "/FI", "PID eq %d" % detached_pid],
                    capture_output=True, text=True,
                ).stdout
                self.assertIn(
                    str(detached_pid), listing,
                    "前置条件失败：分离进程没起来（pid=%d），本测试将毫无意义"
                    % detached_pid,
                )

                self.assertTrue(
                    pid_is_alive(detached_pid),
                    "对非自己子进程的进程（pid=%d）也必须判活 —— 生产里失败的正是这一种"
                    % detached_pid,
                )
            finally:
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(detached_pid)],
                    capture_output=True,
                )
                launcher.wait(timeout=15)

    # 刻意**不测**"已退出的子进程应被判为不存活"：Windows 的 pid 回收很积极，
    # 子进程 wait() 之后那个 pid 可能立刻被别的进程占用，于是断言随机失败——
    # 那是环境的性质，不是本模块的契约。实测本机 pid 回收确实会触发。
    # 真正要保证的行为是"持有者不活跃时锁能被接管"，由下面两个
    # `test_takes_over_*` 用**明确不存在的 pid** 覆盖。


class InstanceLockTests(unittest.TestCase):
    def test_first_acquire_succeeds_and_writes_own_pid(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            lock = InstanceLock(temp_dir)
            acquired, holder = lock.acquire()
            self.assertTrue(acquired)
            self.assertTrue(lock.held)
            self.assertEqual(holder, os.getpid())
            with io.open(lock.path, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["pid"], os.getpid())
            lock.release()
            self.assertFalse(os.path.exists(lock.path))

    def test_refuses_when_a_live_process_holds_the_lock(self):
        """这条是整个模块存在的理由：活着就不能并存。"""
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            time.sleep(0.3)
            with tempfile.TemporaryDirectory() as temp_dir:
                # 直接写一个"被真实子进程占用"的锁
                probe = InstanceLock(temp_dir)
                with io.open(probe.path, "w", encoding="utf-8") as handle:
                    json.dump({"pid": child.pid, "startedAt": int(time.time())}, handle)

                lock = InstanceLock(temp_dir)
                acquired, holder = lock.acquire()
                self.assertFalse(acquired, "有活着的持有者时必须拒绝加锁")
                self.assertEqual(holder, child.pid, "应报出占用者的 pid")
                self.assertFalse(lock.held)
        finally:
            child.terminate()
            child.wait(timeout=10)

    def test_takes_over_lock_whose_holder_is_dead(self):
        """一次强杀不能留下永久死锁。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            probe = InstanceLock(temp_dir)
            with io.open(probe.path, "w", encoding="utf-8") as handle:
                json.dump({"pid": 999999, "startedAt": 0}, handle)  # 不存在的 pid

            lock = InstanceLock(temp_dir)
            acquired, holder = lock.acquire()
            self.assertTrue(acquired, "持有者已死时应接管")
            self.assertEqual(holder, os.getpid())

    def test_takes_over_own_stale_lock(self):
        """上次异常退出留下的自己的锁，应能接管而不是永久卡死。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            probe = InstanceLock(temp_dir)
            with io.open(probe.path, "w", encoding="utf-8") as handle:
                json.dump({"pid": os.getpid(), "startedAt": 0}, handle)

            lock = InstanceLock(temp_dir)
            self.assertTrue(lock.acquire()[0])

    def test_takes_over_corrupt_lock_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            probe = InstanceLock(temp_dir)
            with io.open(probe.path, "w", encoding="utf-8") as handle:
                handle.write("{ 这不是合法 JSON")

            lock = InstanceLock(temp_dir)
            self.assertTrue(lock.acquire()[0], "损坏的锁必须能被接管")

    def test_takes_over_empty_lock_file(self):
        """O_EXCL 创建与写 pid 之间有一个瞬间读到空文件——不能因此死锁。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            probe = InstanceLock(temp_dir)
            with io.open(probe.path, "w", encoding="utf-8") as handle:
                handle.write("")

            lock = InstanceLock(temp_dir)
            self.assertTrue(lock.acquire()[0], "空锁必须能被接管")

    def test_release_does_not_delete_a_lock_taken_over_by_someone_else(self):
        """不能删掉后来接管者的锁——那会让第三个人也进来。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            lock = InstanceLock(temp_dir)
            lock.acquire()
            # 模拟"别人接管了"：把文件里的 pid 换掉
            with io.open(lock.path, "w", encoding="utf-8") as handle:
                json.dump({"pid": 999999, "startedAt": 0}, handle)
            lock.release()
            self.assertTrue(
                os.path.exists(lock.path),
                "锁的持有者已经不是我们，不该删掉它",
            )

    def test_takes_over_lock_that_is_too_old(self):
        """超过最大年龄的锁必须被接管 —— 否则 pid 回收会让桥永远起不来。

        这条不是洁癖：Windows 的 pid 回收很积极，持有者早已退出、pid 却恰好
        被无关进程占用时，只靠存活判定会把陈旧锁当成"还活着"，症状是
        「桥怎么都启动不了，必须人工去删那个 lock 文件」。
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            probe = InstanceLock(temp_dir)
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                time.sleep(0.3)
                with io.open(probe.path, "w", encoding="utf-8") as handle:
                    json.dump(
                        {
                            "pid": child.pid,  # 确实活着
                            "startedAt": int(time.time())
                            - MAX_LOCK_AGE_SECONDS - 60,  # 但锁太老了
                        },
                        handle,
                    )
                lock = InstanceLock(temp_dir)
                self.assertTrue(
                    lock.acquire()[0],
                    "超过 %d 秒的锁应被接管，不能让它把桥永久挡住"
                    % MAX_LOCK_AGE_SECONDS,
                )
            finally:
                child.terminate()
                child.wait(timeout=10)

    def test_fresh_lock_from_a_live_process_is_still_respected(self):
        """对照组：锁很新且持有者活着 -> 必须仍然拒绝（自愈不能变成"永远接管"）。"""
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            time.sleep(0.3)
            with tempfile.TemporaryDirectory() as temp_dir:
                probe = InstanceLock(temp_dir)
                with io.open(probe.path, "w", encoding="utf-8") as handle:
                    json.dump({"pid": child.pid, "startedAt": int(time.time())}, handle)
                self.assertFalse(
                    InstanceLock(temp_dir).acquire()[0],
                    "新鲜的、持有者活着的锁必须继续生效",
                )
        finally:
            child.terminate()
            child.wait(timeout=10)

    def test_release_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            lock = InstanceLock(temp_dir)
            lock.acquire()
            lock.release()
            lock.release()  # 再来一次不能抛
            self.assertFalse(lock.held)

    def test_two_locks_in_same_process_are_not_serialised(self):
        """同进程内两个锁对象应视为同一个持有者（第二个直接放行）。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            first = InstanceLock(temp_dir)
            self.assertTrue(first.acquire()[0])
            second = InstanceLock(temp_dir)
            acquired, _ = second.acquire()
            self.assertTrue(
                acquired,
                "自己的残留锁应被接管而不是拒绝（否则重启会起不来）",
            )


if __name__ == "__main__":
    unittest.main()
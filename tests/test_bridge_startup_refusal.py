"""验收条件 1 的**进程级**证据：桥**起不来**，而不是打个 warning 继续跑。

拆出来的理由（本组是这一组里唯一**必须独立**的）：它起**真子进程**跑
``python -m opencode_bridge``，且依赖 ``opencode_url`` 与 ``password`` 齐备 ——
**否则 ``main()`` 也会返回 1**，那就不是证明（自带对照组正是为此）。
它与其它四组零共享状态：不 import :mod:`tests.nick_trap_support`，
也不构造任何适配器。混在一起会让「跑哪几个用例」变成一个不确定的开关。

⚠️ 代价是它是全仓最慢的一组（每条最多等 40 秒）—— 所以它是**单独一个文件**，
而不是「跑不跑得看心情」。

机制说明见 :mod:`tests.test_nick_in_allowlist` 的模块 docstring。
本文件不含任何用例改动。
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
from pathlib import Path


class BridgeStartupRefusalTests(unittest.TestCase):
    """验收条件 1 的**进程级**证据：桥**起不来**，而不是打个 warning 继续跑。

    ⚠️ 必须带对照（:meth:`test_the_control_run_without_the_trap_reaches_running_state`）：
    只断言"退出码非 0"是无法区分的 —— 配置写错、连不上 opencode、没配适配器，
    都会给出同一个退出码。对照跑通同一条路径，才能把退出码 1 归因到**陷阱**。
    """

    #: 桥启动成功的标志（``__main__._run_bridge_locked`` 在 ``core.start()`` 之后打它）。
    RUNNING_MARKER = "running against"

    def _run_bridge(self, allowed_chat_ids: list[str], timeout: float = 40.0):
        """在独立目录里真跑一次 ``python -m opencode_bridge``。

        ``opencode_url`` / ``opencode_password`` 显式给全 ⇒
        :func:`~opencode_bridge.opencode_client.discover_endpoint` 直接返回，
        **不发任何网络请求**（本机不该有、也不需要有 opencode 服务）。

        :return: ``(退出码 或 None, 全部输出)``。``None`` = 检查期间进程还活着
            （对照组：桥起来了，所以我们把它杀掉）。
        """
        with tempfile.TemporaryDirectory() as workdir:
            config_path = os.path.join(workdir, "config.json")
            log_path = os.path.join(workdir, "bridge.log")
            io.open(config_path, "w", encoding="utf-8").write(json.dumps({
                "opencode_url": "http://127.0.0.1:65533",
                "opencode_password": "not-a-real-password",
                "state_path": os.path.join(workdir, "state.json"),
                "permissions_mode": "deny",
                "adapters": {"irc": {
                    "host": "127.0.0.1", "port": 6667, "nick": "MyBot",
                    "channels": ["#chan"], "allowed_chat_ids": allowed_chat_ids,
                }},
            }, ensure_ascii=False))
            env = dict(os.environ)
            env["OPENCODE_BRIDGE_CONFIG"] = config_path
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONPATH"] = (
                str(Path(__file__).resolve().parent.parent)
                + os.pathsep + env.get("PYTHONPATH", "")
            )
            with io.open(log_path, "w", encoding="utf-8") as log_file:
                process = subprocess.Popen(
                    [sys.executable, "-m", "opencode_bridge", "--config", config_path],
                    cwd=workdir, env=env, stdout=log_file, stderr=subprocess.STDOUT,
                )
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    output = io.open(log_path, encoding="utf-8", errors="replace").read()
                    if self.RUNNING_MARKER in output or process.poll() is not None:
                        break
                    time.sleep(0.15)
                if process.poll() is None:
                    exit_code: int | None = None
                    process.kill()
                else:
                    exit_code = process.returncode
                process.wait(timeout=15)
            output = io.open(log_path, encoding="utf-8", errors="replace").read()
        return exit_code, output

    def test_the_bridge_refuses_to_start_when_own_nick_is_whitelisted(self):
        exit_code, output = self._run_bridge(["#chan", "mybot"])
        self.assertEqual(exit_code, 1, f"期望因陷阱而拒绝启动，实际:\n{output}")
        self.assertIn("拒绝启动", output)
        self.assertIn("会把所有人的私聊一起放行", output)
        self.assertNotIn(
            self.RUNNING_MARKER, output,
            "桥不该在这条配置下跑起来 —— 跑起来就意味着私聊那条路存在",
        )

    def test_the_control_run_without_the_trap_reaches_running_state(self):
        """**对照**：白名单里只有频道名 ⇒ 桥**照常启动**。

        没有这一条，"退出码 1" 无法归因 —— 它可能只是这个测试环境压根起不来桥。
        """
        exit_code, output = self._run_bridge(["#chan"])
        self.assertIn(
            self.RUNNING_MARKER, output,
            f"对照组也起不来 ⇒ 本测试环境本身有问题，上面那条的退出码 1 "
            f"就不能归因到陷阱：\n{output}",
        )
        self.assertIsNone(exit_code, f"对照组不该自己退出：\n{output}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

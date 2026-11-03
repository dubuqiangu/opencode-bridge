"""启动期故障隔离：子系统降级 + 「桥拒绝启动」落盘（lane A）。

它钉的**三处行为变更**（用户已签字；此处只做守门，不重新论证）：

1. ``BridgeCore.start()`` 里的 ``recover_inbox()`` —— 重放上一轮崩溃留下的收件箱
   失败时**降级**（记日志 + 桥继续服务新消息），⛔ 不再让一个正在启动的桥崩掉。
2. ``_run_bridge_locked`` 里的 ``ProcessDiagnostics.install()`` —— 取证装不上时
   降级（记 warning + 本轮不取崩溃栈），⛔ 不再让桥起不来、也不再让 ``finally``
   里的 ``exit_reason`` 变成未赋值（UnboundLocalError）。
3. **装配必需构件在构造期崩掉时落一条「桥未启动」** —— ``BridgeCore(...)`` 构造失败
   与 ``core.start()`` 失败此前都是裸堆栈退出，盘上留下上一次成功启动的 ``ok``
   （正是 :func:`bridge_runner._record_bridge_refusal` 的 docstring 描述的那个谎）。
   ⛔ **只修记录，不改 abort**：记完照抛 ⇒ 退出码仍是 1。

⚠️ 另一个被**钉住**的现存行为（不是本 lane 的变更）：``usable >= 1`` 的**构造循环
部分失败**里，被跳过的那个适配器的原因**只进日志、不进 platform-health 记录**。
见 :meth:`TestBuildLoopPartialFailureIsolation.test_the_skipped_reason_stays_log_only`。

⚠️ 本文件**全部不联网**：``discover_endpoint`` / ``OpenCodeClient`` / ``build`` /
``BridgeCore`` / ``ProcessDiagnostics`` / ``InstanceLock`` / ``threading.Event`` 全部
换成替身；只有第 4 组（A1）用**真的** :class:`BridgeCore`，但把它的 SSE 线程体与
``recover_inbox`` 换成受控替身。
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import tempfile
import types
import unittest
from unittest import mock

from opencode_bridge import __main__ as cli
from opencode_bridge import bridge_runner
from opencode_bridge import health
from opencode_bridge.adapters.base import Adapter
from opencode_bridge.config import Config
from opencode_bridge.core import BridgeCore
from opencode_bridge.diagnostics import ProcessDiagnostics

logging.getLogger("opencode_bridge").addHandler(logging.NullHandler())

#: 抓 import 期的**真**类，免得被下面 ``mock.patch.object(bridge_runner, "ProcessDiagnostics", …)``
#: 换成替身后拿不到它（A2 的用例要验的正是**真** ``record`` / ``dump_stacks`` / ``close``
#: 在 ``install()`` 被跳过时安全）。
_REAL_PROCESS_DIAGNOSTICS = ProcessDiagnostics


class _QuietDiagnostics:
    """``ProcessDiagnostics`` 的**最小**替身：什么都不写、不抛。"""

    def __init__(self, *args, **kwargs) -> None:
        return None

    def install(self) -> None:
        return None

    def record(self, *args, **kwargs) -> None:
        return None

    def dump_stacks(self, *args, **kwargs) -> None:
        return None

    def close(self) -> None:
        return None


def _diagnostics_whose_install_raises(bridge_dir: str):
    """真的 :class:`ProcessDiagnostics`，但 ``install()`` 改成抛异常。

    ⚠️ 刻意用**真**对象：这样 ``finally`` 里那三下（``record`` / ``dump_stacks`` /
    ``close``）走的是生产实现 —— 而本 lane 的核心声明正是**它们在 install 被跳过时
    安全**（``_stack_file`` 为 ``None``、``_registered`` 为空）。用替身会把这条声明
    验成空转。
    """
    diagnostics = _REAL_PROCESS_DIAGNOSTICS(bridge_dir)

    def broken_install() -> None:
        raise RuntimeError("取证装不上")

    diagnostics.install = broken_install  # type: ignore[method-assign]
    return diagnostics


class _PermissiveLock:
    """``InstanceLock`` 的替身：永远拿得到。"""

    def __init__(self, *args, **kwargs) -> None:
        return None

    def acquire(self):
        return True, 0

    def release(self) -> None:
        return None


class _StopImmediately:
    """``threading.Event`` 的替身：``wait`` 立刻返回 ``True`` ⇒ 主循环当轮退出。"""

    def wait(self, timeout=None) -> bool:
        return True

    def set(self) -> None:
        return None


class _RunningAdapter(Adapter):
    """一个不联网、``start()`` 立刻返回的适配器（够走完 attach / 启动循环）。"""

    supports_inbound = True

    def __init__(self, platform: str) -> None:
        self.name = platform
        self.label = platform
        #: 构造循环据此判「token 非空」⇒ 这个平台才 attach 得上。
        self.bot_token = "token-not-real"
        super().__init__({}, hooks=None)  # type: ignore[arg-type]

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def send(self, out) -> None:  # type: ignore[override]
        return None

    def edit(self, handle, out) -> bool:  # type: ignore[override]
        return False


class _RecordingCore:
    """``BridgeCore`` 替身：记下 attach 过谁，``start()`` / ``stop()`` 都不做事。"""

    def __init__(self, *args, **kwargs) -> None:
        self.attached: list[str] = []
        self.startup_probes: dict = {}

    def attach(self, adapter) -> None:
        self.attached.append(getattr(adapter, "name", repr(adapter)))

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None


class _BridgeHarness(unittest.TestCase):
    """跑**真的** ``cli.run_bridge``，只把网络 / 构造 / 线程 / 落盘目录换成替身。"""

    def setUp(self) -> None:
        # ⚠️ ``ignore_cleanup_errors``：``BridgeCore(...)`` 构造失败时异常直接上抛，
        # 而 ``_run_bridge_locked`` 里那一步之后的 ``inbox.close()``（唯一关 SQLite
        # 连接的地方）在 ``finally`` 里、也就是**到不了** ⇒ ``inbox.db`` 仍被占着，
        # Windows 上删临时目录会 ``PermissionError``。那是生产 abort 语义的既有后果
        # （本 lane 只加记录、不改 abort），测试只需容忍清理失败，不掩盖它。
        self._directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.bridge_dir = self._directory.name
        self.addCleanup(self._directory.cleanup)

    def config_with(self, adapters: dict) -> Config:
        """⚠️ ``state_path`` 指进临时目录 ⇒ ``inbox.db`` 也落在那里，⛔ 不污染仓库根。"""
        return Config(
            adapters=adapters,
            state_path=os.path.join(self.bridge_dir, "state.json"),
        )

    def run_bridge(
        self,
        cfg: Config,
        *,
        build,
        core_replacement,
        diagnostics=_QuietDiagnostics,
    ) -> tuple[int, list]:
        """跑一遍 ``cli.run_bridge``；返回 ``(退出码, 构造出来的 core 列表)``。

        :param build: 替身 ``bridge_runner.build(name, entry, hooks)``。
        :param core_replacement: 替身 ``BridgeCore``（可调用；构造成功才被记录，
            构造就抛的用例拿到的是空列表、异常照抛）。
        :param diagnostics: 替身 ``ProcessDiagnostics``（可调用，收 ``bridge_dir``）。
        """
        from opencode_bridge.opencode_client import Endpoint

        created_cores: list = []

        def core_factory(*args, **kwargs):
            core = core_replacement(*args, **kwargs)
            created_cores.append(core)
            return core

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                bridge_runner, "discover_endpoint",
                return_value=Endpoint("http://127.0.0.1:4096", "pw"),
            ))
            stack.enter_context(mock.patch.object(
                bridge_runner, "OpenCodeClient",
                lambda endpoint: types.SimpleNamespace(close=lambda: None),
            ))
            stack.enter_context(mock.patch.object(bridge_runner, "build", build))
            stack.enter_context(mock.patch.object(
                bridge_runner, "BridgeCore", core_factory,
            ))
            stack.enter_context(mock.patch.object(
                bridge_runner, "ProcessDiagnostics", diagnostics,
            ))
            stack.enter_context(mock.patch.object(
                bridge_runner, "InstanceLock", _PermissiveLock,
            ))
            stack.enter_context(mock.patch.object(
                bridge_runner, "_bridge_dir", lambda: self.bridge_dir,
            ))
            stack.enter_context(mock.patch.object(
                bridge_runner, "threading",
                types.SimpleNamespace(Event=lambda: _StopImmediately()),
            ))
            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = cli.run_bridge(cfg)
        return exit_code, created_cores

    def verdicts_on_disk(self) -> dict[str, str]:
        record = health.read_platform_health(self.bridge_dir)
        return {
            key: str(entry.get("verdict"))
            for key, entry in (record or {}).get("platforms", {}).items()
        }

    def assert_the_refusal_names(self, expected_one_liner: str) -> None:
        """盘上必须有一条 ``telegram`` 的 ``not_started``，``detail`` 含给定那句话。"""
        record = health.read_platform_health(self.bridge_dir)
        self.assertIsNotNone(record, "桥拒绝启动，却没有任何记录")
        entry = (record.get("platforms") or {}).get("telegram")
        self.assertIsNotNone(entry, "拒绝启动的记录里没有那唯一的平台")
        self.assertEqual(entry["verdict"], health.VERDICT_NOT_STARTED)
        self.assertIn(expected_one_liner, entry["detail"])


# ======================================================================
# A4-1/2/3：构造循环的**部分**失败被隔离
# ======================================================================
class TestBuildLoopPartialFailureIsolation(_BridgeHarness):
    """⭐ ``usable >= 1`` 且其中一个适配器 ``build()`` 失败 ⇒ 桥照跑。

    Recon 结论：``core.start()`` 那一层的**适配器启动**部分失败已被
    ``tests/test_platform_health.py`` 覆盖（``test_one_exploding_adapter_does_not_stop_the_others``），
    而**构造循环**这一层的部分失败此前没有被端到端钉过。
    """

    def two_adapter_config(self) -> Config:
        # telegram 先出现 ⇒ 预检在它身上就返回 True（凭据是齐的）。
        return self.config_with({
            "telegram": {"bot_token": "token-not-real"},
            "irc": {"host": "irc.example.org", "nick": "bot", "channels": ["#x"]},
        })

    def test_one_failing_build_does_not_stop_the_other_adapter(self):
        """① 退出码 0；② 成功的那个被 attach 上、桥跑到 running。"""
        def build(name, entry, hooks):
            if name == "irc":
                raise RuntimeError("irc 构造时炸了")
            return _RunningAdapter(name)

        with self.assertLogs("opencode_bridge", level="INFO") as caught:
            exit_code, created_cores = self.run_bridge(
                self.two_adapter_config(), build=build,
                core_replacement=_RecordingCore,
            )

        self.assertEqual(exit_code, 0, "一个适配器构造失败不许把整座桥带走")
        self.assertEqual(created_cores[0].attached, ["telegram"])
        self.assertIn(
            "bridge running against", "\n".join(caught.output),
            "桥没有走到 running —— 部分失败没有被隔离",
        )

    def test_the_skipped_reason_stays_log_only(self):
        """③ ⚠️ 被跳过的适配器原因目前**只进日志、不进记录**（钉住现存诚实行为）。

        ⚠️ **这个断言在「部分失败的原因将来也进 platform-health」那天应当翻转**：
        届时被跳过的平台会有自己的一条记录、``detail`` 里带着「构造失败」。今天
        ``usable == 0`` 才会走 :func:`_record_bridge_refusal`，而 ``usable >= 1`` 时
        那份 ``unusable_reasons`` 无人消费 —— 记录里只有 attach 上的平台。
        """
        def build(name, entry, hooks):
            if name == "irc":
                raise RuntimeError("irc 构造时炸了")
            return _RunningAdapter(name)

        with self.assertLogs("opencode_bridge", level="INFO") as caught:
            self.run_bridge(
                self.two_adapter_config(), build=build,
                core_replacement=_RecordingCore,
            )

        # 日志里**有**那条原因（说明它确实被算出来了，只是没落盘）。
        self.assertIn("failed to build adapter", "\n".join(caught.output))

        record = health.read_platform_health(self.bridge_dir)
        self.assertNotIn("irc", (record or {}).get("platforms", {}))
        self.assertNotIn(
            "构造失败", json.dumps(record, ensure_ascii=False),
            "被跳过平台的原因进记录了 —— 若这是有意变更，请翻转本断言",
        )

    def test_all_builds_failing_is_still_an_abort(self):
        """对照臂：两个都失败 ⇒ ``usable == 0`` ⇒ 退出码 1 + 每个平台一条记录。"""
        def build(name, entry, hooks):
            raise RuntimeError("都炸了")

        with contextlib.redirect_stderr(io.StringIO()):
            exit_code, _created = self.run_bridge(
                self.two_adapter_config(), build=build,
                core_replacement=_RecordingCore,
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(
            set(self.verdicts_on_disk()), {"telegram", "irc"},
            "usable == 0 必须为配置里的每个平台各留一条记录",
        )
        self.assertEqual(set(self.verdicts_on_disk().values()),
                         {health.VERDICT_NOT_STARTED})


# ======================================================================
# A1：recover_inbox() 失败被降级
# ======================================================================
class TestRecoverInboxFailureIsIsolated(_BridgeHarness):
    """重放上一轮崩溃留下的收件箱失败 ⇒ 桥继续服务新消息。"""

    class _RecoverInboxExplodingCore(BridgeCore):
        """真的 :class:`BridgeCore`，只把 SSE 线程体与 ``recover_inbox`` 换成受控替身。"""

        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.event_stream.run = lambda: None  # type: ignore[method-assign]
            self.inbound_gateway.recover_inbox = self._boom  # type: ignore[method-assign]

        @staticmethod
        def _boom() -> None:
            raise RuntimeError("重放上一轮收件箱时炸了")

    def test_a_raising_recover_inbox_does_not_kill_the_bridge(self):
        def build(name, entry, hooks):
            return _RunningAdapter(name)

        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertLogs("opencode_bridge", level="ERROR") as caught:
                exit_code, created_cores = self.run_bridge(
                    self.config_with({"telegram": {"bot_token": "token-not-real"}}),
                    build=build,
                    core_replacement=self._RecoverInboxExplodingCore,
                )

        self.assertEqual(exit_code, 0, "收件箱重放失败不许杀掉一个正在启动的桥")
        self.assertIn("收件箱重放失败", "\n".join(caught.output))
        # ⚠️ 这一组用的是**真** :class:`BridgeCore`（不是 ``_RecordingCore``），
        # 所以挂载表要问它自己的 :attr:`BridgeCore.adapters`，没有 ``attached`` 那个替身字段。
        self.assertEqual(
            [getattr(adapter, "name", None) for adapter in created_cores[0].adapters],
            ["telegram"],
            "降级之后适配器仍必须被 attach / 启动",
        )


# ======================================================================
# A2：ProcessDiagnostics.install() 失败被降级
# ======================================================================
class TestDiagnosticsInstallFailureIsIsolated(_BridgeHarness):
    """取证装不上 ⇒ 记 warning + 桥照跑（且 ``finally`` 不炸 UnboundLocalError）。"""

    def test_a_raising_install_does_not_kill_the_bridge(self):
        def build(name, entry, hooks):
            return _RunningAdapter(name)

        with contextlib.redirect_stderr(io.StringIO()):
            # ⚠️ 收到 INFO 才拿得到那句 "bridge running against"（降到 WARNING 只收
            # WARNING 及以上，会把「桥确实跑到 running」这条证据漏掉）。
            with self.assertLogs("opencode_bridge", level="INFO") as caught:
                exit_code, _created = self.run_bridge(
                    self.config_with({"telegram": {"bot_token": "token-not-real"}}),
                    build=build,
                    core_replacement=_RecordingCore,
                    diagnostics=_diagnostics_whose_install_raises,
                )

        self.assertEqual(exit_code, 0, "取证装不上不许杀掉桥")
        self.assertIn("崩溃栈取证未装配", "\n".join(caught.output))
        # ⚠️ install 被跳过后，finally 里那三下（record/dump_stacks/close）走的是
        # **真**对象 —— 走到这里没抛，正是「它们在无 install 时安全」的证据。
        self.assertIn(
            "bridge running against", "\n".join(caught.output),
        )


# ======================================================================
# A3：装配必需构件崩掉 ⇒ 落一条「桥未启动」，再照抛
# ======================================================================
class TestAssemblyFailureRecordsARefusal(_BridgeHarness):
    """⛔ 修的是**记录**，不是 abort：记完照抛 ⇒ 退出码 1。"""

    class _CoreThatFailsToConstruct:
        def __init__(self, *args, **kwargs) -> None:
            raise RuntimeError("构造 core 时炸了")

    class _CoreThatFailsToStart(_RecordingCore):
        def start(self) -> None:
            raise RuntimeError("事件流线程起不来")

    def configured(self) -> Config:
        return self.config_with({"telegram": {"bot_token": "token-not-real"}})

    def test_a_failing_core_construction_records_a_refusal_and_aborts(self):
        """⛔ 盘上留下的必须是一句「桥未启动」，不是上一次成功启动的 ``ok``。

        ⚠️ 阶段名 ``core_construction_failed`` 由 ``_REFUSAL_REASON_BY_STAGE``
        映射成下面这句**中文一句话**，而**只有后者**进得了 ``detail``（见
        :func:`bridge_runner._record_bridge_refusal`）—— 所以断言的是那句话。
        """
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(RuntimeError):
                self.run_bridge(
                    self.configured(), build=lambda n, e, h: _RunningAdapter(n),
                    core_replacement=self._CoreThatFailsToConstruct,
                )
        self.assert_the_refusal_names("桥核心构造失败，桥因此没起来")

    def test_a_failing_core_start_records_a_refusal_and_aborts(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(RuntimeError):
                self.run_bridge(
                    self.configured(), build=lambda n, e, h: _RunningAdapter(n),
                    core_replacement=self._CoreThatFailsToStart,
                )
        self.assert_the_refusal_names(
            "事件流线程启动失败，桥无法收到 agent 回复，因此没起来"
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

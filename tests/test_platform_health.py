"""「上次启动时的探测结论」这条通路的回归护栏（落盘 + 两个视图）。

它钉的是本任务最初的缺陷：**token 打错 / 被吊销 / 网络被墙时，桥完全静默，
而 ``--status`` 与 ``--setup --json`` 仍然显示「已配置 / 入站就绪」** —— 因为那两个
视图只检查 token 字符串非空，而 ``getMe`` 的结论只写进了一行日志、没有任何通道
到用户。

⚠️ 这些用例**全部不联网**：``--status`` / ``--setup --json`` 必须是"网络坏了也能看"
的那条路（``run_check`` 的 docstring 明写「no sessions, **no adapters**」），而
落盘侧只需要一个临时目录。所以这里没有一处 mock HTTP。

⚠️ 关于"没有记录"：**无记录 ≠ 正常**。每一条断言都按这个前提写 —— 把「没验过」
显示成「验过了、没问题」正是本任务要消灭的那类假话。
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import re
import tempfile
import types
import unittest
from unittest import mock

from opencode_bridge import __main__ as cli
from opencode_bridge import health
from opencode_bridge.adapters.base import Adapter
from opencode_bridge.config import Config
from opencode_bridge.core import BridgeCore
from opencode_bridge.hooks import MsgHandle, Outbound
from opencode_bridge.state import StateStore

# 期望的 warning 不刷屏；``assertLogs`` 自己换 handler，不受影响。
logging.getLogger("opencode_bridge").addHandler(logging.NullHandler())

#: **形状**正确的 Telegram bot token。⛔ 按 AGENTS.md §2.4 **拼接片段**：
#: 本仓库武装了推送保护，完整形状的字面量会让整个 push 被拦（而这不是"测试夹具
#: 例外"的情况）。运行值逐字节不变，脱敏器只认形状。
_TOKEN_BODY = ("Ab" * 17) + "Z"          # 35 个 [A-Za-z0-9_-]
SHAPE_TOKEN = "123456789" + ":" + _TOKEN_BODY

#: 同一个形状规则的 ``re``，同样**拼接**而成（AGENTS.md §2.4）。
_TOKEN_SHAPE = re.compile(r"\d{8,10}:" + r"[A-Za-z0-9_-]{35}")

#: ``_platform_status`` 改动**之前**的 key 集合 —— 硬编码成字面量（不是对着
#: 实现算一遍）：它是一道**防"顺手清理"**的护栏，而对着实现算就恒真了。
#: 仓库外的消费者（``bridge_setup``）按**值**断言这些字段，我们读不到它的源码，
#: 所以删一个 / 改一个名字都可能打掉别人的判据，而本仓库不会有任何测试变红。
PLATFORM_STATUS_KEYS_BEFORE_PROBE_FIELD = {
    "key",
    "label",
    "configured",
    "outbound_ready",
    "inbound_ready",
    "inbound_implemented",
    "missing",
    "allowed_chat_ids_count",
    "allowlist_configured",
    "admits_nobody",
    "admits_any_sender",
    "allowlist_keys_present",
    "allowlist_conflict",
    "not_ready_reasons",
    "ready_for_agent",
    "capabilities",
}


class _StubClient:
    """只够 :meth:`BridgeCore.start` / :meth:`BridgeCore.stop` 用的空客户端。"""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _ProbeAdapter(Adapter):
    """按构造参数决定 ``start()`` 的三种结局：抛异常 / 上报结论 / 什么都不说。

    ⛔ 它**不注册**进适配器注册表（没挂 ``@register``），所以不会让
    ``--status`` 的平台表多出一行 —— 这些用例要验的是"收集与落盘"，不是平台清单。
    """

    supports_inbound = True

    def __init__(
        self,
        platform: str,
        *,
        start_error: BaseException | None = None,
        verdict: str | None = None,
        code: object = None,
        detail: str = "",
    ) -> None:
        self.name = platform
        self.label = platform
        self._start_error = start_error
        self._verdict = verdict
        self._code = code
        self._detail = detail
        super().__init__({}, hooks=None)  # type: ignore[arg-type]

    def start(self) -> None:
        if self._start_error is not None:
            raise self._start_error
        if self._verdict is not None:
            self.report_startup_probe(
                self._verdict, code=self._code, detail=self._detail
            )

    def stop(self) -> None:
        return None

    def send(self, out: Outbound) -> MsgHandle | None:
        return None

    def edit(self, handle: MsgHandle, out: Outbound) -> bool:
        return False


def make_core(workdir: str, adapters: list[Adapter]) -> BridgeCore:
    """造一个**不会起线程、不会碰网络**的 core。

    SSE 线程的 target 是 ``event_stream.run``、启动重放是
    ``inbound_gateway.recover_inbox``，两处都换成空实现 —— 这里要验的是
    ``start()`` 里**收集结论的那一段**，不是事件流。
    """
    core = BridgeCore(Config(), _StubClient(), StateStore(
        os.path.join(workdir, "state.json")
    ))
    core.event_stream.run = lambda: None          # type: ignore[method-assign]
    core.inbound_gateway.recover_inbox = lambda: None  # type: ignore[method-assign]
    for adapter in adapters:
        core.attach(adapter)
    return core


class _BridgeDirIsolated(unittest.TestCase):
    """把 :func:`__main__._bridge_dir` 的推导结果钉在临时目录上。

    必须在**没有** ``OPENCODE_BRIDGE_CONFIG`` 时也可靠 —— 那条推导会退回 cwd，
    于是"没隔离"的用例会把 ``platform-health.json`` 写进仓库根目录。
    """

    def setUp(self) -> None:
        self._previous = os.environ.get("OPENCODE_BRIDGE_CONFIG")
        self._directory = tempfile.TemporaryDirectory()
        self.bridge_dir = self._directory.name
        config_path = os.path.join(self.bridge_dir, "config.json")
        with io.open(config_path, "w", encoding="utf-8") as handle:
            json.dump({"adapters": {}}, handle)
        os.environ["OPENCODE_BRIDGE_CONFIG"] = config_path
        self.addCleanup(self._directory.cleanup)

        def restore() -> None:
            if self._previous is None:
                os.environ.pop("OPENCODE_BRIDGE_CONFIG", None)
            else:
                os.environ["OPENCODE_BRIDGE_CONFIG"] = self._previous

        self.addCleanup(restore)
        self.assertEqual(
            cli._bridge_dir(), os.path.abspath(self.bridge_dir),
            "用例没把 bridge 目录隔离干净 —— 它会把落盘文件写进仓库",
        )

    def read_health_file(self) -> str:
        with io.open(
            os.path.join(self.bridge_dir, health.PLATFORM_HEALTH_FILE_NAME),
            encoding="utf-8",
        ) as handle:
            return handle.read()


class TestPersistRoundTrip(_BridgeDirIsolated):
    """落盘 ↔ 读取：写进去再读出来必须**逐字段相等**，且盘上不出现凭据形状。"""

    def test_written_record_is_read_back_identically(self):
        """往返一致。

        ⚠️ 用的是**中性**的 ``detail``：读取侧会再过一次规范化（脱敏 + 单行化），
        而脱敏对 ``email#...`` 这类摘要是**进程内密钥**的 —— 同一个进程里往返必然
        相等，跨进程则不必（本模块的契约只保证"读到的形态一致"）。
        """
        probes = {
            "telegram": {"verdict": "failed", "code": 401, "detail": "getMe: Unauthorized"},
            "slack": {"verdict": "ok", "detail": "auth.test 通过"},
            "irc": {"verdict": "skipped", "detail": "没有凭据可验"},
        }
        written = health.record_startup_probes(self.bridge_dir, probes)
        self.assertIsNotNone(written)
        record = health.read_platform_health(self.bridge_dir)
        self.assertIsNotNone(record)
        self.assertGreater(health.recorded_at(record), 0.0)
        for platform, expected in probes.items():
            with self.subTest(platform=platform):
                self.assertEqual(
                    health.probe_from_record(record, platform), expected
                )

    def test_persisted_file_never_contains_a_token_shape(self):
        """⛔ 安全红线：落盘内容里不许出现 token 形状。

        两条断言都要：夹具本身**必须真的被脱敏引擎认出来**（否则第二条是恒真的
        —— AGENTS.md §7.1「空集 ≠ 不存在」），而盘上的文件里不许有它的明文。
        """
        from opencode_bridge import redaction

        self.assertIsNotNone(
            _TOKEN_SHAPE.search(SHAPE_TOKEN),
            "夹具必须与 Telegram token 形状完全吻合，否则下面那条断言恒真",
        )
        scrubbed = redaction.default_redactor().scrub("getMe: " + SHAPE_TOKEN)
        self.assertNotIn(SHAPE_TOKEN, scrubbed)
        self.assertIn("[REDACTED:telegram-bot-token]", scrubbed)

        probes = {
            "telegram": {
                "verdict": "failed",
                "code": 401,
                "detail": "getMe: " + SHAPE_TOKEN,
            },
            "slack": {
                "verdict": "failed",
                # 错误码位置也要洗：它是平台回的自由文本，形状上完全可能是凭据。
                "code": SHAPE_TOKEN,
                "detail": "token=" + SHAPE_TOKEN,
            },
        }
        health.record_startup_probes(self.bridge_dir, probes)
        on_disk = self.read_health_file()
        self.assertNotIn(SHAPE_TOKEN, on_disk)
        self.assertEqual(
            _TOKEN_SHAPE.search(on_disk), None,
            "落盘文件里出现了 token 形状的字面量 —— 这是不可逆的泄漏",
        )
        record = health.read_platform_health(self.bridge_dir)
        telegram = health.probe_from_record(record, "telegram")
        self.assertEqual(telegram["verdict"], "failed")
        self.assertNotIn(SHAPE_TOKEN, telegram["detail"])

    def test_a_broken_entry_does_not_cost_us_the_others(self):
        """一个平台坏掉不许吃掉其余平台的结论。

        坏的那条给的是**类型错**的条目（``"不是字典"``）—— 这正是"用户手改坏文件"
        与"某个上游塞了个怪东西"两种现实会落到的那条路上。
        """
        path = health.record_startup_probes(self.bridge_dir, {
            "telegram": {"verdict": "ok", "detail": "getMe 通过"},
            "坏掉的平台": "不是字典",
            "slack": {"verdict": "failed", "code": 403, "detail": "invalid_auth"},
        })
        self.assertIsNotNone(path)
        record = health.read_platform_health(self.bridge_dir)
        self.assertEqual(
            sorted(health.platforms_in_record(record)), ["slack", "telegram"]
        )
        self.assertEqual(
            health.probe_from_record(record, "slack")["code"], 403
        )

    def test_unwritable_directory_never_breaks_the_caller(self):
        """写不进去时返回 ``None`` 且**不抛**：这份记录是排障辅助，不是运行前提。"""
        missing = os.path.join(self.bridge_dir, "并不存在的目录")
        self.assertIsNone(health.record_startup_probes(missing, {
            "telegram": {"verdict": "ok", "detail": "getMe 通过"},
        }))

    def test_missing_record_reads_as_none_not_as_ok(self):
        """⛔ 没有记录 = ``None``，而 ``None`` **不代表成功**。"""
        self.assertIsNone(health.read_platform_health(self.bridge_dir))
        self.assertIsNone(
            health.probe_from_record(health.read_platform_health(self.bridge_dir),
                                     "telegram")
        )

    def test_unreadable_verdict_never_becomes_ok(self):
        """盘上被手改成 ``"probably-fine"`` 的 verdict 归一化成 ``failed``。

        理由与写入侧同一条：读不懂的话**绝不**当成好。
        """
        path = os.path.join(self.bridge_dir, health.PLATFORM_HEALTH_FILE_NAME)
        with io.open(path, "w", encoding="utf-8") as handle:
            json.dump(
                {"recorded_at": 1.0,
                 "platforms": {"telegram": {"verdict": "probably-fine"}}},
                handle,
            )
        probe = health.probe_from_record(health.read_platform_health(self.bridge_dir),
                                         "telegram")
        self.assertEqual(probe["verdict"], health.VERDICT_FAILED)


class TestCoreCollectsEveryAdapter(_BridgeDirIsolated):
    """③ 收集发生在**唯一**调 ``adapter.start()`` 的地方，且逐个隔离。"""

    def test_one_exploding_adapter_does_not_stop_the_others(self):
        """⭐ 核心缺陷：一个适配器 ``start()`` 抛异常，其余适配器**照记不误**。

        抛异常的适配器自己**没有机会**上报（所以它必须由收集侧补一条
        ``failed`` —— 否则"抛了异常"这种最该被看见的失败反而没人记）。
        """
        core = make_core(self.bridge_dir, [
            _ProbeAdapter("boomplatform", start_error=RuntimeError("探测时炸了")),
            _ProbeAdapter("goodplatform", verdict="ok", detail="探测通过"),
            _ProbeAdapter("badplatform", verdict="failed", code=401,
                          detail="Unauthorized"),
        ])
        core.start()
        self.addCleanup(core.stop)

        self.assertEqual(sorted(core.startup_probes),
                         ["badplatform", "boomplatform", "goodplatform"])
        self.assertEqual(core.startup_probes["boomplatform"]["verdict"], "failed")
        self.assertIn("探测时炸了", core.startup_probes["boomplatform"]["detail"])
        self.assertEqual(core.startup_probes["goodplatform"]["verdict"], "ok")

        health.record_startup_probes(self.bridge_dir, core.startup_probes)
        record = health.read_platform_health(self.bridge_dir)
        for platform in ("boomplatform", "goodplatform", "badplatform"):
            with self.subTest(platform=platform):
                self.assertEqual(
                    health.probe_from_record(record, platform),
                    core.startup_probes[platform],
                    "收集到的三条结论必须全部落盘 —— 断一条就是那条用户永远看不见",
                )

    def test_an_adapter_that_says_nothing_is_not_recorded_as_ok(self):
        """没上报就是没上报：既不进盘，也不许被当成"正常"。"""
        core = make_core(self.bridge_dir, [_ProbeAdapter("quietplatform")])
        core.start()
        self.addCleanup(core.stop)
        self.assertEqual(core.startup_probes, {})
        self.assertIsNone(
            health.probe_from_record(core.startup_probes, "quietplatform")
        )


class TestSetupJsonCarriesTheProbe(_BridgeDirIsolated):
    """④ ``--setup --json`` 那一列。"""

    def _row(self, cfg: Config, platform: str) -> dict:
        return next(row for row in cli._platform_status(cfg) if row["key"] == platform)

    def test_last_start_probe_is_none_when_there_is_no_record(self):
        cfg = Config(adapters={"telegram": {"bot_token": "t"}})
        self.assertIsNone(self._row(cfg, "telegram")["last_start_probe"])

    def test_last_start_probe_carries_the_recorded_failure(self):
        cfg = Config(adapters={"telegram": {"bot_token": "t"}})
        health.record_startup_probes(self.bridge_dir, {
            "telegram": {"verdict": "failed", "code": 401, "detail": "getMe: Unauthorized"},
        })
        probe = self._row(cfg, "telegram")["last_start_probe"]
        self.assertEqual(probe["verdict"], "failed")
        self.assertEqual(probe["code"], 401)

    def test_no_existing_key_of_platform_status_was_removed_or_renamed(self):
        """⛔ 外部契约护栏：现有 key 一个都没少、名字没变。

        ⛔ **不许**对着实现算一遍期望集合 —— 那恒真。必须是上面那份硬编码的
        ``PLATFORM_STATUS_KEYS_BEFORE_PROBE_FIELD``。
        """
        cfg = Config(adapters={"telegram": {"bot_token": "t"}})
        for row in cli._platform_status(cfg):
            with self.subTest(platform=row["key"]):
                missing = PLATFORM_STATUS_KEYS_BEFORE_PROBE_FIELD - set(row)
                self.assertEqual(
                    missing, set(),
                    "既有 key 少了 %s —— 仓库外的消费者按值断言它们" % sorted(missing),
                )
        sample = cli._platform_status(cfg)[0]
        self.assertEqual(
            set(sample) - PLATFORM_STATUS_KEYS_BEFORE_PROBE_FIELD,
            {"last_start_probe"},
            "本次只许新增 last_start_probe 这一个键",
        )


class TestStatusTableShowsTheProbe(_BridgeDirIsolated):
    """④ ``--status`` 的人读那一段。"""

    SECTION_HEADER = "== 上次启动时的探测结论 =="

    def render(self, cfg: Config) -> str:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            cli.run_status(cfg)
        return buffer.getvalue()

    def section(self, rendered: str) -> list[str]:
        """只取新那一段（到下一个 ``==`` 段为止）。

        刻意不整段断言：``--status`` 的其余部分（含运行态）本来就会出现"失败"字样
        （锁文件里的 ``failedAt``、能力读取失败），整段断言会把那些混进来，于是
        "无记录不许说失败"这条断言就恒真了。
        """
        lines = rendered.splitlines()
        start = next(
            index for index, line in enumerate(lines) if self.SECTION_HEADER in line
        )
        tail = lines[start + 1:]
        end = next(
            (index for index, line in enumerate(tail) if line.startswith("== ")), len(tail)
        )
        return tail[:end]

    def test_without_a_record_it_neither_says_ok_nor_says_failed(self):
        """⭐ 无记录时**既不许显示成「正常」，也不许显示成「失败」**。"""
        rendered = self.render(Config(adapters={"telegram": {"bot_token": "t"}}))
        body = "\n".join(self.section(rendered))
        self.assertIn(cli.NO_START_PROBE_TEXT, body)
        self.assertNotIn("正常", body)
        self.assertNotIn("失败", body)

    @staticmethod
    def row_for(body: str, label: str) -> str:
        """只取某个平台在那一段里的**那一行**。

        必须逐行取：未配置的平台（含 ``bind_port`` 没填的 a2a）只显示「无记录」，
        而配置齐备的平台可能显示一条**别的**结论 —— 整段断言会把这些无关行混进来，
        "不许说无记录"那条也就恒真了。
        """
        for line in body.splitlines():
            if line.strip().startswith(label):
                return line
        raise AssertionError(f"那段输出里找不到 {label} 那一行：\n{body}")

    def test_the_recorded_failure_is_shown_with_its_platform_error_code(self):
        health.record_startup_probes(self.bridge_dir, {
            "telegram": {"verdict": "failed", "code": 401, "detail": "getMe: Unauthorized"},
        })
        rendered = self.render(Config(adapters={"telegram": {"bot_token": "t"}}))
        body = "\n".join(self.section(rendered))
        row = self.row_for(body, "Telegram")
        self.assertIn("上次启动", row)
        self.assertIn("code=401", row)
        self.assertIn("Unauthorized", row)
        self.assertNotIn(cli.NO_START_PROBE_TEXT, row)
        self.assertNotIn("正常", row)

    def test_a_recorded_ok_is_labelled_as_the_last_start_and_carries_its_time(self):
        health.record_startup_probes(self.bridge_dir, {
            "telegram": {"verdict": "ok", "detail": "getMe 通过"},
        })
        rendered = self.render(Config(adapters={"telegram": {"bot_token": "t"}}))
        body = "\n".join(self.section(rendered))
        row = self.row_for(body, "Telegram")
        self.assertIn("上次启动 正常（", row)
        self.assertNotIn(cli.NO_START_PROBE_TEXT, row)

    def test_the_section_says_the_conclusion_is_from_the_last_start(self):
        """⚠️ 时效性：这一段必须说清那是「上次**启动尝试**」的结论，不是实时探测。

        ⚠️ **措辞必须含「尝试」**：拒绝启动的两条路现在也落记录了（见
        ``tests/test_bridge_refusal_probe.py``），所以这一段覆盖的是**每一次启动尝试**
        —— 若措辞退回成「上次启动」（读起来像"上次成功启动"），读者就会把
        「桥没起来」那条记录误当成一次成功启动的结论。
        """
        rendered = self.render(Config(adapters={"telegram": {"bot_token": "t"}}))
        body = "\n".join(self.section(rendered))
        self.assertIn("上一次启动尝试", body)
        self.assertIn("不是实时探测", body)

    def test_the_section_says_a_refusal_is_recorded_too(self):
        """这一段必须说清「桥拒绝启动时也会记一条」。

        不说的话，"上一次启动尝试"会与"盘上那条 ``ok`` 来自上一次成功启动"混成一句
        —— 而这正是本任务要消灭的那个缺陷的**措辞**那一半。
        """
        rendered = self.render(Config(adapters={"telegram": {"bot_token": "t"}}))
        body = "\n".join(self.section(rendered))
        self.assertIn("拒绝启动", body)
        self.assertIn("桥未启动", body)
        # 唯一的例外必须说出来：「已有另一个实例在运行」那次不写记录。
        self.assertIn("已有另一个实例在运行", body)


class StartupSurvivesProbeRecordingFailure(unittest.TestCase):
    """⛔ 不变量：**「上次启动探测结论」这条排障通路，绝不该决定桥的生死。**

    ⚠️ 这是实测回归（2026-10-06，全量三轮一致 5 个 error）的护栏：
    ``record_startup_probes`` **内部**兜住了写盘失败，但 ``core.startup_probes``
    这个**实参**是在它**外面**求值的 —— core 是鸭子类型替身时（测试里就有两个
    刻意最小的 ``patch`` 替身）``AttributeError`` 会在 ``core.start()`` **成功之后**
    把桥打死。⇒ 一个排障辅助功能有权杀掉正在运行的桥，
    直接违反 ``health.record_startup_probes`` docstring 自己写的
    「写盘失败绝不打断桥的启动」。

    ⇒ 下面两种坏法都必须退化成「记warning + 本轮不写记录」。
    """

    #: 刻意**最小**的替身：只有 ``run_bridge`` 会用到的那几个方法，
    #: ⛔ **不给** ``startup_probes`` —— 那正是被测的坏法之一。
    class _CoreWithoutProbeAttribute:
        def __init__(self, *args, **kwargs) -> None:
            return None

        def attach(self, adapter) -> None:
            return None

        def start(self) -> None:
            return None

        def stop(self) -> None:
            return None

    class _QuietDiagnostics:
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

    class _PermissiveLock:
        def __init__(self, *args, **kwargs) -> None:
            return None

        def acquire(self):
            return True, 0

        def release(self) -> None:
            return None

    class _StopImmediately:
        def wait(self, timeout=None) -> bool:
            return True

        def set(self) -> None:
            return None

    def _run_bridge_over(self, core_class, record_patch=None):
        """跑一遍真的 ``run_bridge``，返回 ``(exit_code, 捕获到的 warning 文本)``。"""
        from opencode_bridge.opencode_client import Endpoint

        os.makedirs(".tmp", exist_ok=True)
        with tempfile.TemporaryDirectory(dir=".tmp") as directory:
            config = Config(
                state_path=os.path.join(directory, "state.json"),
                adapters={"telegram": {"bot_token": "token-not-real"}},
            )
            patches = [
                mock.patch.object(cli, "discover_endpoint",
                                  return_value=Endpoint("http://127.0.0.1:4096", "pw")),
                mock.patch.object(cli, "OpenCodeClient",
                                  lambda endpoint: types.SimpleNamespace(close=lambda: None)),
                mock.patch.object(cli, "build",
                                  lambda name, entry, hooks: types.SimpleNamespace(
                                      bot_token="token-not-real")),
                mock.patch.object(cli, "BridgeCore", core_class),
                mock.patch.object(cli, "ProcessDiagnostics", self._QuietDiagnostics),
                mock.patch.object(cli, "InstanceLock", self._PermissiveLock),
                mock.patch.object(cli, "_bridge_dir", lambda: directory),
                mock.patch.object(cli, "threading",
                                  types.SimpleNamespace(Event=lambda: self._StopImmediately())),
            ]
            if record_patch is not None:
                patches.append(mock.patch.object(cli.health, "record_startup_probes",
                                                record_patch))
            buffer = io.StringIO()
            with contextlib.ExitStack() as stack:
                for item in patches:
                    stack.enter_context(item)
                with self.assertLogs("opencode_bridge", level="WARNING") as caught:
                    with contextlib.redirect_stdout(buffer):
                        exit_code = cli.run_bridge(config)
            return exit_code, "\n".join(caught.output)

    def test_a_core_without_the_probe_attribute_cannot_kill_the_bridge(self):
        exit_code, warnings = self._run_bridge_over(self._CoreWithoutProbeAttribute)
        self.assertEqual(
            exit_code, 0,
            "core 没有 startup_probes 时桥必须照常跑完并正常退出 —— "
            "它在 core.start() 成功之后才求值，一崩就把已经跑起来的桥杀了",
        )
        self.assertIn("不影响桥的运行", warnings)

    def test_a_raising_probe_recorder_cannot_kill_the_bridge(self):
        """``record_startup_probes`` 自己抛（不是返回 None）也必须被兜住。"""

        def exploding_recorder(*args, **kwargs):
            raise RuntimeError("落盘时炸了")

        exit_code, warnings = self._run_bridge_over(
            self._CoreWithoutProbeAttribute, record_patch=exploding_recorder
        )
        # 替身本身也没有那个属性 ⇒ 先撞 AttributeError；无论撞哪个，
        # 判据都是**桥仍然正常退出**、且有一条说明「不影响桥的运行」的 warning。
        self.assertEqual(
            exit_code, 0,
            "探测记录这条路无论怎么坏，都不许把已经 start() 成功的桥带走",
        )
        self.assertIn("不影响桥的运行", warnings)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
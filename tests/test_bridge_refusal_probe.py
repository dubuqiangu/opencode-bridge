"""「桥拒绝启动」这两条路**必须**落一条不是 ok 的结论。

它钉的缺陷（实测，不是推断）
===========================

上一批加了「上次启动时各适配器的探测结论」这条通道（:mod:`opencode_bridge.health`
⇒ ``<bridge_dir>/platform-health.json`` ⇒ ``--setup --json`` 的 ``last_start_probe``
与 ``--status`` 的一段），**但桥有两条「拒绝启动」的路径完全不写记录**：

1. ``run_bridge`` 的预检：``_has_configured_adapter`` 为否时提前返回
   （在 ``discover_endpoint`` **之前**）；
2. ``_run_bridge_locked`` 里 ``usable == 0`` 时 ``return 1``
   （在 ``discover_endpoint`` **之后**、``core.start()`` **之前**）。

⇒ **用户刚把配置改坏、桥拒绝启动时，盘上仍是上一条旧的 ``ok``**（内容与 mtime 都没变），
而 ``--status`` 会把它与「Telegram 未配置」并排显示 ——
**状态视图在用户最需要它的时候显示上一轮的好消息。**

⚠️ 本文件**全部不联网**：这些用例要验的是「桥没起来时盘上写了什么」，
而两条拒绝路径的判断都不依赖网络（预检那条尤其如此 —— 它在 ``discover_endpoint``
**之前**）。所以 ``discover_endpoint`` 在预检那条路上是被**断言没被调用**的替身。

⚠️ 关于「有记录」与「没记录」：**两者在 ``--status`` 上必须能分开说** ——
「没验过」「验了但失败」「桥压根没起来」是三种不同的处境，
把后两者显示成「正常」或把前者显示成「失败」都是这一类缺陷。
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
from opencode_bridge import health
from opencode_bridge.config import Config

logging.getLogger("opencode_bridge").addHandler(logging.NullHandler())

#: 仓库根目录（本文件在 ``<root>/tests/`` 下）—— 用来读 ``config.example.json``。
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: ``health.VERDICTS`` **加第四档之前**的取值集合。⛔ 硬编码成字面量，
#: **不许**对着实现算一遍（那恒真）。仓库外的 ``bridge_setup`` 按**值**断言现有取值，
#: 而我们读不到它的源码 ⇒ 删一个 / 改一个都可能打掉别人的判据，而本仓库不会变红。
VERDICTS_BEFORE_REFUSAL_VERDICT = {"ok", "failed", "skipped"}

#: 加了 :data:`~opencode_bridge.health.VERDICT_NOT_STARTED` **之后**的取值集合。
#: 同样硬编码：这一条同时挡住「顺手又加一档」与「悄悄改掉某个字面量」。
VERDICTS_AFTER_REFUSAL_VERDICT = {"ok", "failed", "skipped", "not_started"}

#: 又加了 :data:`~opencode_bridge.health.VERDICT_DOES_NOT_PROBE`（「本平台压根没有
#: 启动期凭据探测这个动作」）**之后**的取值集合。⚠️ 硬编码，⛔ 不许对着实现算。
#: ⚠️ 「加第四档」那条的历史快照（上面那份）**一个字没动** —— 它记的是**那一批**做过的
#: 事，删掉它等于把"当时为什么只有四档"的证据擦掉。
VERDICTS_AFTER_UNPROBED_VERDICT = {
    "ok", "failed", "skipped", "not_started", "does_not_probe",
}

#: ``health.VERDICTS`` 的**有序**序列。⚠️ 顺序也是契约的一部分：外部护栏
#: ``tests/test_outbound_failure_channel.py`` 的 ``EXPECTED_VERDICTS`` 按**序**断言它
#: ⇒ 把新档插在中间会让那条护栏的 diff 变成"重排"，把一次纯新增看成一串改写。
VERDICTS_IN_ORDER = ("ok", "failed", "skipped", "not_started", "does_not_probe")

#: 既有四档各自渲染出来的**逐字**文案，以及喂给
#: :func:`~opencode_bridge.health.describe_verdict` 的那条结论的**实参**。
#: ⛔ 硬编码成字面量，⛔ **不许**对着实现算：措辞也是既有对外表现的一部分，
#: 而 ``--status`` 逐行拼的就是它 —— 改一个都可能打掉别人的判据。
WORDING_OF_EACH_EXISTING_VERDICT = {
    "ok": ({"verdict": "ok", "detail": "getMe 通过"}, "正常"),
    "skipped": ({"verdict": "skipped", "detail": "没东西可验"}, "未探测 —— 没东西可验"),
    "not_started": (
        {"verdict": "not_started", "detail": "预检未通过"}, "桥未启动 —— 预检未通过",
    ),
    "failed": (
        {"verdict": "failed", "code": 401, "detail": "Unauthorized"},
        "失败（code=401）—— Unauthorized",
    ),
}


class _RefusalHarness(unittest.TestCase):
    """把 ``_bridge_dir`` 钉在 :meth:`tempfile.TemporaryDirectory` 上（默认在 ``.tmp/``）。

    ⚠️ 不隔离的话，这条路的落盘文件会被写进仓库根目录（``_bridge_dir`` 退回 cwd），
    而**断言读的是同一个目录** ⇒ 不隔离的用例会读到上一条用例留下的记录，
    于是"上一条 ``ok`` 被覆盖了没有"这条判据恒真。
    """

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.bridge_dir = self._directory.name
        self.addCleanup(self._directory.cleanup)
        # ⚠️ 必须 ``start()`` 之后再 ``addCleanup(patcher.stop)``：光把
        # ``mock.patch.object(...)`` 传给 addCleanup 是**不会**启用补丁的 ——
        # 于是落盘会去真的 ``_bridge_dir()``（本仓库的 cwd），用例读到的是
        # 别人留下的记录，而"上一条 ok 被覆盖了没有"那条判据恒真。
        patcher = mock.patch.object(cli, "_bridge_dir", lambda: self.bridge_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def seed_a_previous_ok_record(self) -> None:
        """先伪造一条「上一次**成功启动**」的 ``ok`` —— 缺陷的现场就是这个状态。"""
        health.record_startup_probes(self.bridge_dir, {
            "telegram": {"verdict": "ok", "detail": "getMe 通过"},
        })

    def use_fresh_bridge_dir(self) -> None:
        """换一个**全新**的落盘目录（``subTest`` 之间用）。

        ⚠️ 复用同一个目录的话，"上一条记录还在不在"这类判据会读到上一个人留下的
        文件 —— 于是"拒绝启动把上一条 ``ok`` 覆盖了没有"那条断言恒真。
        """
        self._directory.cleanup()
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.bridge_dir = self._directory.name

    def record_for(self, platform: str) -> dict | None:
        return health.probe_from_record(
            health.read_platform_health(self.bridge_dir), platform
        )

    def verdicts_on_disk(self) -> dict[str, str]:
        record = health.read_platform_health(self.bridge_dir)
        return {
            key: str(entry.get("verdict"))
            for key, entry in (record or {}).get("platforms", {}).items()
        }

    def refusal_line(self, section: str) -> str | None:
        """那一段里说「桥没起来」的 ⚠ 行；没有则 ``None``。

        ⚠️ **不能**直接 ``assertNotIn("桥未启动", 整段)``：段首那段**说明**里本来就
        写着「桥拒绝启动就写『桥未启动』」⇒ 拿整段去判会让"没记录 ≠ 桥未启动"
        这条断言恒假（AGENTS.md §7.1：判据量了一个空集，结论看起来干净、方向却是反的）。
        """
        for line in section.splitlines():
            if line.strip().startswith("⚠"):
                return line
        return None

    def status_section(self, cfg: Config) -> str:
        """``--status`` 里「上次启动时的探测结论」那一段（到下一个 ``==`` 为止）。"""
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            cli.run_status(cfg)
        lines = buffer.getvalue().splitlines()
        header = "== 上次启动时的探测结论 =="
        start = next(
            (index for index, line in enumerate(lines) if header in line), None
        )
        if start is None:
            raise AssertionError(f"--status 输出里没有 {header} 那一段")
        tail = lines[start + 1:]
        end = next(
            (index for index, line in enumerate(tail) if line.startswith("== ")),
            len(tail),
        )
        return "\n".join(tail[:end])


class TestPreflightRefusalIsRecorded(_RefusalHarness):
    """① 预检那条路（在 ``discover_endpoint`` **之前**返回）。"""

    def refusal_config(self) -> Config:
        return Config(adapters={
            "telegram": {"bot_token": ""},        # 缺凭据
            "a2a": {"bind_port": ""},             # 缺端口（平台自己作答的那一类）
        })

    def run_refused_bridge(self, cfg: Config) -> tuple[int, str]:
        """跑真的 ``cli.run_bridge``，并断言 ``discover_endpoint`` **没被调用**。

        ⚠️ 断言"没调用"是**本用例的一半**：预检那条路在 ``discover_endpoint``
        **之前**返回，而它的判断**不依赖网络** ⇒ 若哪天有人把落盘挪到
        ``discover_endpoint`` 之后，"opencode 不可达"就会开始吞掉这条记录
        （而那正是用户最需要它的时刻：服务与配置同时坏了）。
        """
        def explode_on_discovery(*args, **kwargs):
            raise AssertionError(
                "预检那条路不许碰 discover_endpoint —— 它的判断不依赖网络，"
                "而落盘必须在那之前发生"
            )

        with mock.patch.object(cli, "discover_endpoint", explode_on_discovery):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = cli.run_bridge(cfg)
        return exit_code, stderr.getvalue()

    def test_refusing_to_start_records_a_conclusion_that_is_not_ok(self):
        """⭐ 盘上必须多一条**不是 ok** 的记录，且 ``detail`` 说清为什么。"""
        self.seed_a_previous_ok_record()
        cfg = self.refusal_config()

        exit_code, _stderr = self.run_refused_bridge(cfg)
        self.assertEqual(exit_code, 0, "预检那条路是 exit 0 + 提示（docs/install.md）")

        for platform in ("telegram", "a2a"):
            with self.subTest(platform=platform):
                probe = self.record_for(platform)
                self.assertIsNotNone(probe, f"{platform} 的记录不见了")
                self.assertEqual(
                    probe["verdict"], health.VERDICT_NOT_STARTED,
                    "拒绝启动必须记成「桥未启动」这一档，绝不能留成 ok / 正常",
                )
                self.assertIn("预检未通过", probe["detail"])
                self.assertIn("没有任何适配器", probe["detail"])

    def test_the_detail_names_the_platform_and_what_it_is_missing(self):
        """⚠️ 「具体缺什么」必须落在**该平台自己那一行**上。

        ⚠️ 为什么不把整个清单抄进每一条 ``detail``：:data:`health.MAX_DETAIL_CHARS`
        会截断，而清单一旦超过它，**靠后的平台整条消失** —— 实测里 ``a2a`` 的
        ``bind_port`` 就是这么不见的，而用户恰恰照着自己那一行去找该填哪个键。
        """
        cfg = self.refusal_config()
        self.run_refused_bridge(cfg)
        telegram = self.record_for("telegram")
        self.assertIn("telegram", telegram["detail"])
        self.assertIn("bot_token", telegram["detail"])
        a2a = self.record_for("a2a")
        self.assertIn("a2a", a2a["detail"])
        self.assertIn("bind_port", a2a["detail"])
        for probe in (telegram, a2a):
            self.assertIn("预检未通过", probe["detail"])

    def test_the_previous_ok_record_is_replaced_not_kept(self):
        """⭐⭐ 本缺陷的正脸：拒绝启动时，盘上**不许**还留着上一轮的 ``ok``。

        旧行为是「两条拒绝路径都不写」⇒ 上一条 ``ok`` 连同它的 mtime 原封不动地留着，
        而 ``--status`` 把它与「未配置」并排显示。
        """
        self.seed_a_previous_ok_record()
        self.assertEqual(self.verdicts_on_disk(), {"telegram": "ok"}, "前提：先有一条 ok")
        self.run_refused_bridge(self.refusal_config())
        self.assertEqual(self.verdicts_on_disk(), {"telegram": "not_started", "a2a": "not_started"})
        self.assertNotIn("ok", self.verdicts_on_disk().values())

    def test_the_fresh_template_is_still_refused_and_is_now_recorded(self):
        """任务③⑥：``config.example.json`` 的空模板仍必须被预检拒绝，**并且**被记下来。

        ⚠️ 后半句是本任务的判据之一：上一批刚把 ``config_optional`` 谓词化
        （``tests/test_config_runnable_verdict.py`` 钉着它），而这里要求的是
        「判定不变 + 记录新增」两件事**同时**成立。
        """
        with io.open(os.path.join(REPO_ROOT, "config.example.json"), encoding="utf-8") as fh:
            template = json.load(fh)
        cfg = Config(adapters=template["adapters"])
        self.assertFalse(
            cli._has_configured_adapter(cfg),
            "空模板曾被判成已配置 —— 桥会跳过 NO_ADAPTER_MESSAGE 那条提前退出",
        )

        self.run_refused_bridge(cfg)
        a2a = self.record_for("a2a")
        self.assertIsNotNone(a2a, "空模板被拒绝启动，却没有留下任何记录")
        self.assertEqual(a2a["verdict"], health.VERDICT_NOT_STARTED)
        self.assertIn("bind_port", a2a["detail"])
        # 模板里 13 个平台全都没配 ⇒ 每一条都必须有记录，且**没有一条是 ok**
        self.assertEqual(len(self.verdicts_on_disk()), len(template["adapters"]))
        self.assertNotIn("ok", self.verdicts_on_disk().values())

    def test_no_refusal_entry_is_ever_recorded_as_does_not_probe(self):
        """⚠️ 「不做探测」那一档 ⛔ 不许出现在拒绝启动的那份记录里。

        那条路上**一个适配器都没 attach 上去** ⇒ "本平台压根没有启动期凭据探测
        这个动作"这个说法在这里是**编造**的：真实原因是"桥压根没起来"。
        ⇒ 每一条都必须是 :data:`~opencode_bridge.health.VERDICT_NOT_STARTED`。
        """
        cfg = self.refusal_config()
        self.run_refused_bridge(cfg)
        verdicts = self.verdicts_on_disk()
        self.assertNotIn(
            health.VERDICT_DOES_NOT_PROBE, verdicts.values(),
            "拒绝启动的那份记录里混进了「不做探测」—— 它把「桥没起来」说成了"
            "「这个平台没有这个动作」，用户会去改一个压根没坏的配置",
        )
        self.assertEqual(set(verdicts.values()), {health.VERDICT_NOT_STARTED})


class TestNoUsableAdapterRefusalIsRecorded(_RefusalHarness):
    """② ``usable == 0`` 那条路（在 ``discover_endpoint`` 之后、``core.start()`` 之前）。

    ⚠️ 这条路的**关键差异**：预检**已经过了** ⇒ 凭据是齐的，只是构造不出来。
    ⇒ 结论若写成 ``skipped``（"未探测 —— 没东西可验"），会把用户引到
    **"你没填 token"这个错的方向**上去 —— 这就是必须新增一档而不是复用 ``skipped`` 的理由。
    """

    class _StubCore:
        def __init__(self, *args, **kwargs) -> None:
            return None

        def attach(self, adapter) -> None:
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

    def run_with_unbuildable(self, cfg: Config, build_side_effect):
        """走真的 ``cli.run_bridge``，只把构造与线程换成替身。"""
        from opencode_bridge.opencode_client import Endpoint

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                cli, "discover_endpoint",
                return_value=Endpoint("http://127.0.0.1:4096", "pw"),
            ))
            stack.enter_context(mock.patch.object(
                cli, "OpenCodeClient",
                lambda endpoint: types.SimpleNamespace(close=lambda: None),
            ))
            stack.enter_context(mock.patch.object(cli, "build", build_side_effect))
            stack.enter_context(
                mock.patch.object(cli, "BridgeCore", self._StubCore)
            )
            stack.enter_context(
                mock.patch.object(cli, "ProcessDiagnostics", self._QuietDiagnostics)
            )
            stack.enter_context(
                mock.patch.object(cli, "InstanceLock", self._PermissiveLock)
            )
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                return cli.run_bridge(cfg)

    def test_no_usable_adapter_records_why_it_refused(self):
        """``build()`` 抛错 ⇒ 记一条 not_started，detail 说清**哪个平台、为什么**。"""
        cfg = Config(adapters={"telegram": {"bot_token": "token-not-real"}})

        def exploding_build(name, entry, hooks):
            raise RuntimeError("构造时炸了")

        self.seed_a_previous_ok_record()
        exit_code = self.run_with_unbuildable(cfg, exploding_build)

        self.assertEqual(exit_code, 1, "usable == 0 那条路是 return 1")
        probe = self.record_for("telegram")
        self.assertIsNotNone(probe, "usable == 0 时没有留下任何记录")
        self.assertEqual(probe["verdict"], health.VERDICT_NOT_STARTED)
        self.assertIn("没有任何适配器构造成功", probe["detail"])
        self.assertIn("telegram", probe["detail"])
        self.assertIn("构造失败", probe["detail"])
        self.assertIn("RuntimeError", probe["detail"])
        self.assertNotIn("ok", self.verdicts_on_disk().values())

    def test_every_reason_that_drops_an_adapter_reaches_the_record(self):
        """⚠️ 构造循环里**三条** ``continue`` 分支的理由都得出得来。

        少一条的话，那种平台就会在记录里只剩一句"没起来"，用户得自己回去猜。
        """
        def build_raising(error: BaseException):
            def build(name, entry, hooks):
                raise error

            return build

        cases = {
            "不是已注册的适配器键": build_raising(KeyError("irc")),
            "构造失败": build_raising(ValueError("端口不合法")),
            "bot_token 为空": lambda name, entry, hooks: types.SimpleNamespace(
                bot_token="  "
            ),
        }
        for expected_fragment, build_side_effect in cases.items():
            with self.subTest(reason=expected_fragment):
                self.use_fresh_bridge_dir()
                # irc 的 required_tokens 不是 bot_token，所以预检**过得去**
                # —— 这正是「凭据是齐的、只是构造不出来」那种现实。
                cfg = Config(adapters={
                    "irc": {"host": "irc.example.org", "nick": "bot", "channels": ["#x"]}
                })
                exit_code = self.run_with_unbuildable(cfg, build_side_effect)
                self.assertEqual(exit_code, 1)
                probe = self.record_for("irc")
                self.assertIsNotNone(probe, f"{expected_fragment}: 记录不见了")
                self.assertEqual(probe["verdict"], health.VERDICT_NOT_STARTED)
                self.assertIn(expected_fragment, probe["detail"])

    def test_it_never_gets_recorded_as_skipped(self):
        """⭐ 反向钉法：**不许**用 ``skipped`` 冒充「桥没起来」。

        ``skipped`` 在 ``--status`` 上读成「未探测」，在 ``usable == 0`` 这条路上
        **是错的**（凭据是齐的，只是构造不出来）⇒ 它会把用户引到"你没填 token"
        这个错的方向上去。钉它，免得哪天"省一档"把它改回去。
        """
        cfg = Config(adapters={"telegram": {"bot_token": "token-not-real"}})

        def exploding_build(name, entry, hooks):
            raise RuntimeError("构造时炸了")

        self.run_with_unbuildable(cfg, exploding_build)
        recorded = self.record_for("telegram")
        self.assertNotEqual(recorded["verdict"], health.VERDICT_SKIPPED)
        # 两档**渲染出来的话**也必须不同，否则界面上根本分不开这两件事
        self.assertNotEqual(
            health.describe_verdict(recorded),
            health.describe_verdict({"verdict": "skipped", "detail": recorded["detail"]}),
        )
        self.assertIn("桥", health.describe_verdict(recorded))


class TestTheRefusalChannelNeverDecidesTheBridgesFate(_RefusalHarness):
    """⛔ 不变量：这条排障通路**绝不该**把「拒绝启动」变成「带堆栈崩掉」。

    ⚠️ 与 ``_run_bridge_locked`` 里那次调用是**同一条**纪律：
    ``record_startup_probes`` 内部兜住了写盘失败，但 ``platform_reasons``
    与「哪些键缺了」那些**实参**是在它**外面**求值的 ——
    :func:`~opencode_bridge.__main__._missing_required_keys` 会 ``import`` 适配器注册表
    并逐个问判定。⇒ 保护必须包住**求值**，否则一个排障辅助功能有权把
    「拒绝启动 + 一条明明白白的提示」变成「拒绝启动 + 一个堆栈」。
    """

    def test_a_raising_missing_keys_check_still_exits_cleanly(self):
        cfg = Config(adapters={"telegram": {"bot_token": ""}})
        with mock.patch.object(
            cli, "_missing_required_keys",
            side_effect=RuntimeError("判定炸了"),
        ), self.assertLogs("opencode_bridge", level="WARNING") as caught:
            with mock.patch.object(cli, "discover_endpoint") as discovery:
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    exit_code = cli.run_bridge(cfg)
            discovery.assert_not_called()

        self.assertEqual(
            exit_code, 0,
            "这条排障通路抛了不许改掉桥的退出码 —— 用户拿到的应该是那句提示",
        )
        self.assertIn(cli.NO_ADAPTER_MESSAGE, stderr.getvalue())
        warnings = "\n".join(caught.output)
        self.assertIn("未落盘", warnings)
        self.assertIn("不影响桥的退出", warnings)

    def test_an_unwritable_bridge_dir_is_never_reported_as_recorded(self):
        """⚠️ 写不进去时那条 warning **不许**说"已记" —— 那是把失败报成成功。"""
        cfg = Config(adapters={"telegram": {"bot_token": ""}})
        unwritable = os.path.join(self.bridge_dir, "并不存在的目录")
        with mock.patch.object(cli, "_bridge_dir", lambda: unwritable), \
                self.assertLogs("opencode_bridge", level="WARNING") as caught:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = cli.run_bridge(cfg)
        self.assertEqual(exit_code, 0)
        self.assertIn(cli.NO_ADAPTER_MESSAGE, stderr.getvalue())
        warnings = "\n".join(caught.output)
        self.assertNotIn("已记", warnings)
        self.assertIn("未落盘", warnings)


class TestStatusWordingTellsTheThreeStatesApart(_RefusalHarness):
    """③ 「没记录」「有记录但不是 ok」「有记录且是 ok」必须**各说各的**。"""

    def section_without_any_record(self, cfg: Config) -> str:
        self.assertIsNone(health.read_platform_health(self.bridge_dir))
        return self.status_section(cfg)

    def section_with_a_refusal(self, cfg: Config) -> str:
        with mock.patch.object(cli, "discover_endpoint", side_effect=OSError("不联网")):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                cli.run_bridge(cfg)
        return self.status_section(cfg)

    def test_a_refusal_reads_as_bridge_not_started_with_its_reason(self):
        """⛔ **不许**把「桥没起来」显示成「正常」或「就绪」。"""
        cfg = Config(adapters={"telegram": {"bot_token": ""}})
        body = self.section_with_a_refusal(cfg)
        refusal = self.refusal_line(body)
        self.assertIsNotNone(refusal, "拒绝启动了，那一段却没有说「桥未启动」")
        self.assertIn("桥未启动", refusal)
        self.assertIn("预检未通过", refusal)
        self.assertIn("bot_token", refusal)
        self.assertNotIn("正常", body)
        self.assertNotIn("就绪", body)

    def test_no_record_and_a_recorded_refusal_read_differently(self):
        """⭐ 「无记录」与「有记录但不是 ok」在措辞上**必须可区分**。

        「没验过」与「验了、结论不是好」指向完全不同的下一步；把两者显示成同一句话
        就等于让用户在一个假象上排障。

        ⚠️ 两个场景的 ``cfg`` 不同（同一个平台键）：「无记录」那一侧必须有一个
        **已配置**的平台才会被列出行（未配置的平台压根不该被启动过，见
        :func:`opencode_bridge.__main__._print_last_start_probes`），而拒绝启动要求
        那个平台**没配齐** —— 两个前提互斥。
        """
        without_record = self.section_without_any_record(
            Config(adapters={"telegram": {"bot_token": "t"}})
        )
        self.assertIn(cli.NO_START_PROBE_TEXT, without_record)
        self.assertIsNone(
            self.refusal_line(without_record),
            "压根没有记录时，那一段不许出现「桥未启动」—— 无记录与「没起来」是两件事",
        )

        with_record = self.section_with_a_refusal(
            Config(adapters={"telegram": {"bot_token": ""}})
        )
        self.assertIsNotNone(self.refusal_line(with_record))
        self.assertNotIn(
            cli.NO_START_PROBE_TEXT, with_record,
            "有记录（且不是 ok）时绝不许还显示成「无记录」—— 那会让用户以为盘上是空的",
        )
        self.assertNotEqual(with_record, without_record)

    def test_the_time_still_says_it_is_not_a_live_probe(self):
        """⛔ 时效性声明不许被这次改动删掉 —— 那是「三天前的成功」唯一的护栏。"""
        cfg = Config(adapters={"telegram": {"bot_token": ""}})
        for body in (self.section_without_any_record(cfg), self.section_with_a_refusal(cfg)):
            with self.subTest(section="有记录" if "桥未启动" in body else "无记录"):
                self.assertIn("不是实时探测", body)
                self.assertIn("上一次启动尝试", body)


class TestSetupJsonCarriesTheRefusalVerdict(_RefusalHarness):
    """③ ``--setup --json`` 那一列同样不许把「桥没起来」说成平台级结论。"""

    def test_last_start_probe_is_not_started_with_a_reason(self):
        cfg = Config(adapters={"telegram": {"bot_token": ""}})
        with mock.patch.object(cli, "discover_endpoint", side_effect=OSError("不联网")):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                cli.run_bridge(cfg)
        row = next(
            row for row in cli._platform_status(cfg) if row["key"] == "telegram"
        )
        probe = row["last_start_probe"]
        self.assertIsNotNone(probe, "拒绝启动了却报成「没有记录」")
        self.assertEqual(probe["verdict"], health.VERDICT_NOT_STARTED)
        self.assertIn("预检未通过", probe["detail"])
        # 载荷仍必须能 dumps（这是外部消费的机器可读出口）
        json.dumps(cli._platform_status(cfg), ensure_ascii=False)


class TestTheVerdictVocabularyOnlyGrew(unittest.TestCase):
    """③⑤ 新增一档 ⛔ 不许删改任何既有取值 —— 仓库外的消费者按**值**断言它们。"""

    def test_the_three_existing_values_are_still_present(self):
        self.assertTrue(
            VERDICTS_BEFORE_REFUSAL_VERDICT <= set(health.VERDICTS),
            "既有三档少了 %s —— 那是删改了既有取值"
            % sorted(VERDICTS_BEFORE_REFUSAL_VERDICT - set(health.VERDICTS)),
        )

    def test_the_value_set_is_exactly_these_five_named_verdicts(self):
        """⛔ 硬编码断言：**不许**对着实现算（那恒真）。

        ⚠️ 这一条同时挡住「顺手又加一档」与「悄悄改掉某个字面量」——
        所以后来加 :data:`~opencode_bridge.health.VERDICT_DOES_NOT_PROBE` 时，
        它是**照着这份硬编码扩到五档**的，而上面那份「加第四档之前」的快照没动。
        """
        self.assertEqual(set(health.VERDICTS), VERDICTS_AFTER_UNPROBED_VERDICT)

    def test_the_new_value_is_appended_rather_than_inserted_in_the_middle(self):
        """⚠️ **顺序**也是外部契约：``tests/test_outbound_failure_channel.py`` 的
        ``EXPECTED_VERDICTS`` 按**序**断言 ``health.VERDICTS``。

        ⇒ 新档必须**追加在末尾**：插在中间会把那边的 diff 变成"重排"，
        把一次纯新增看成一串改写（而那份护栏本仓库改不动 —— 见交付报告里的欠账）。
        """
        self.assertEqual(health.VERDICTS, VERDICTS_IN_ORDER)

    def test_the_four_existing_values_all_survived(self):
        """⛔ 「四档一个都没被改」的那一半：取值**都还在**。"""
        self.assertTrue(
            VERDICTS_AFTER_REFUSAL_VERDICT <= set(health.VERDICTS),
            "既有四档少了 %s" % sorted(VERDICTS_AFTER_REFUSAL_VERDICT - set(health.VERDICTS)),
        )

    def test_the_three_existing_values_survive_normalisation(self):
        for verdict in sorted(VERDICTS_BEFORE_REFUSAL_VERDICT):
            with self.subTest(verdict=verdict):
                entry = health.normalize_verdict(verdict, detail="原文")
                self.assertEqual(entry["verdict"], verdict)
                self.assertEqual(entry["detail"], "原文")

    def test_no_existing_verdict_wording_was_touched(self):
        """⛔ **四档**的措辞一条都没变（硬编码，逐档比）。

        ⚠️ 这一条**替掉了**原来的 :meth:`test_the_three_existing_values_keep_their_wording`：
        同样的三条逐字断言一个字没少，而 :data:`WORDING_OF_EACH_EXISTING_VERDICT` 那张
        硬编码表还多钉了一条 —— ``not_started`` 的措辞。那一档是上一批加的，
        而"再加一档时别把上一批的措辞蹭掉"这件事从来没被断言过：
        只盯最初三档的话，第四档可以被无声改掉。
        ⛔ 期望值**不许**对着实现算。
        """
        for verdict, (entry, expected) in sorted(WORDING_OF_EACH_EXISTING_VERDICT.items()):
            with self.subTest(verdict=verdict):
                self.assertEqual(health.describe_verdict(entry), expected)

    def test_the_new_value_names_the_bridge_not_the_platform(self):
        """新档的措辞必须**自带**「桥没起来」这个主语，不能靠 detail 解释。"""
        rendered = health.describe_verdict({
            "verdict": health.VERDICT_NOT_STARTED, "detail": "预检未通过",
        })
        self.assertEqual(rendered, "桥未启动 —— 预检未通过")
        self.assertEqual(health.describe_verdict({"verdict": health.VERDICT_NOT_STARTED}),
                         "桥未启动")

    def test_the_unprobed_verdict_never_borrows_another_verdicts_wording(self):
        """⚠️ 「不做探测」⛔ 不许说成 ``skipped``，也不许说成 ``not_started``。

        两者都答"这个平台能不能用"，但排查方向**相反**：``skipped`` 说的是
        「没有可探测的凭据」（去补配置），这一档说的是「压根没有这个动作」
        （去看这条通道支持了哪些平台）。混用会把用户引到并不存在的缺失上去。
        """
        rendered = health.describe_verdict(health.normalize_verdict(
            health.VERDICT_DOES_NOT_PROBE, detail=health.DOES_NOT_PROBE_DETAIL,
        ))
        for misleading in ("没有可探测的凭据", "未探测", "桥未启动"):
            with self.subTest(word=misleading):
                self.assertNotIn(misleading, rendered)
        for other in (health.VERDICT_SKIPPED, health.VERDICT_NOT_STARTED):
            with self.subTest(other=other):
                self.assertNotEqual(
                    rendered,
                    health.describe_verdict(
                        {"verdict": other, "detail": health.DOES_NOT_PROBE_DETAIL}
                    ),
                )

    def test_bridge_refusal_probes_shapes_one_entry_per_platform(self):
        """落盘形态：每个平台一条 not_started，各带**自己**那条原因。"""
        probes = health.bridge_refusal_probes(
            "预检未通过：配置里没有任何适配器此刻够跑",
            {"telegram": "缺 bot_token", "a2a": "缺 bind_port"},
        )
        self.assertEqual(sorted(probes), ["a2a", "telegram"])
        self.assertEqual(probes["telegram"]["verdict"], "not_started")
        self.assertEqual(
            probes["telegram"]["detail"],
            "预检未通过：配置里没有任何适配器此刻够跑（telegram：缺 bot_token）",
        )
        self.assertIn("bind_port", probes["a2a"]["detail"])
        self.assertLess(
            len(probes["a2a"]["detail"]), health.MAX_DETAIL_CHARS,
            "单条 detail 必须在截断线以内 —— 超了就会把「该填哪个键」截掉",
        )
        # 落盘走**唯一**那个入口，形状必须被它接住
        self.assertIsNotNone(health.record_startup_probes(self._tmp_dir(), probes))

    @staticmethod
    def _tmp_dir() -> str:
        import atexit

        directory = tempfile.TemporaryDirectory()
        atexit.register(directory.cleanup)
        return directory.name


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
"""出站失败**终于有人读**这条通路的回归护栏。

它钉的缺陷：**一套建好的结构化失败分类，从来没有任何人读**。各适配器失败时调
:meth:`~opencode_bridge.adapters.base.Adapter._note_send_failure`，把原因收敛成
:class:`~opencode_bridge.hooks.SendError` 的分类（``FORBIDDEN`` /
``RATE_LIMITED`` / ``TRANSIENT`` …）存进 ``_last_send_error`` ——
而 :meth:`~opencode_bridge.adapters.base.Adapter.send_result` 与
:attr:`~opencode_bridge.adapters.base.Adapter.last_send_error` 在生产代码里
**零个调用方**，于是分类被算完就直接丢掉。

⇒ **症状**：agent 回消息时若发送失败，用户那边什么都没有，而"为什么"算出来了
却到不了用户。**同型还有一处更坏**：``email`` 的出站认证失败**连一行日志都没有**
（``send()`` 里只有 ``_note_send_failure; return None``）—— 用户改了 SMTP 密码，
收不到任何回信，日志里也查不到。

## 这些用例的三条纪律

* ⛔ **全程不联网**。``--status`` 必须是"网络坏了也能看"的那条路。
* ⚠️ **判据不许恒真**：每条断言都配一条**反向对照**（把被测的那一步短路掉，
  断言必须翻脸）—— 见 :class:`PersistStepIsActuallyWired`。
* ⚠️ **没有记录 ≠ 正常**。「无记录」既不许显示成「正常」，也不许显示成「失败」。
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import logging
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from opencode_bridge import __main__ as cli
from opencode_bridge import health, outbound
from opencode_bridge.adapters.base import Adapter
from opencode_bridge.config import Config
from opencode_bridge.hooks import MsgHandle, Outbound, SendError
from opencode_bridge.outbound import OutboundSender

# 期望的 warning 不刷屏；``assertLogs`` 自己换 handler，不受影响。
logging.getLogger("opencode_bridge").addHandler(logging.NullHandler())

CONVERSATION = "telegram:55"

#: ``_platform_status`` 在**本次新增出站失败字段之前**的 key 集合。
#: ⛔ **硬编码成字面量**，⛔ **不许**对着实现算一遍期望集合（那恒真）。
#: 理由与 ``tests/test_platform_health.py`` 里那份同名常量完全相同：仓库外的
#: ``bridge_setup`` 按**值**断言这些字段，我们读不到它的源码，所以删一个 / 改一个
#: 名字都可能打掉别人的判据，而本仓库不会有任何测试变红。
#: 刻意**另抄一份**而不是复用那个文件里的常量：那个文件的第二段断言是"本次只许新增
#: ``last_start_probe``"，本任务是**另一条**新增，两者不该互相改写对方的期望集。
PLATFORM_STATUS_KEYS_BEFORE_OUTBOUND_FIELD = {
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
    "last_start_probe",
}

#: 五档 verdict —— 硬编码。⛔ 不要对着 ``health.VERDICTS`` 算期望值（那恒真）。
#: ⚠️ **顺序也是契约**：``bridge_setup`` 按**值**断言，而本仓库这条按**序**断言 ——
#: 所以新增一档必须**追加在末尾**，插在中间会把一次纯新增看成一串改写。
#: 第五档 ``does_not_probe`` 是 2026-10-06 加的（「本平台压根没有启动期凭据探测这个动作」），
#: 与 ``skipped`` 的区别是承重的：``skipped`` 说的是「你没配东西」，它说的是「这里没这个动作」。
EXPECTED_VERDICTS = ("ok", "failed", "skipped", "not_started", "does_not_probe")


class ScriptedAdapter(Adapter):
    """可编程的 ``send()`` 结果，用来走完成功 / 失败 / 部分送达三条路。"""

    def __init__(self, name: str = "telegram") -> None:
        super().__init__({}, hooks=None)  # type: ignore[arg-type]
        self.name = name
        self.label = name
        #: ``None`` = 干净的成功；``(kind, detail)`` = 记下失败；``"raise"`` = 抛。
        self.outcome: object = None
        #: 失败时是否仍交出一个句柄（= 分片发到一半断了的"部分送达"）。
        self.hands_back_a_handle_on_failure = False
        self._handles = 0

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def send(self, out: Outbound) -> MsgHandle | None:
        if self.outcome == "raise":
            raise RuntimeError("socket closed")
        if isinstance(self.outcome, tuple):
            kind, detail = self.outcome
            self._note_send_failure(kind, detail)
        if self.outcome is None or self.hands_back_a_handle_on_failure:
            self._handles += 1
            return MsgHandle(out.conversation_id, "m%d" % self._handles, self.name)
        return None

    def edit(self, handle: MsgHandle, out: Outbound) -> bool:
        return False


class RecorderInstalled(unittest.TestCase):
    """把 :func:`health.OutboundFailureRecorder` 装到一个临时 bridge 目录上。

    ⚠️ 记录器是**进程级装配**（构造器签名被 ``tests/test_outbound.py`` 用
    ``inspect.signature`` 钉死成两个参数，装不了第三个），所以**必须**在
    ``addCleanup`` 里卸掉 —— 否则一条用例装的记录器会漏进后面的用例，而漏进去的
    后果是"本该无记录的盘上有了记录"，那种失败极难归因。
    """

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.bridge_dir = self._directory.name
        # ⚠️ 必须把 :func:`opencode_bridge.__main__._bridge_dir` 的推导**钉**在这个
        # 临时目录上：``--setup --json`` 与 ``--status`` 读的是**那个**推导的结果，
        # 而没有 ``OPENCODE_BRIDGE_CONFIG`` 时它会退回 cwd ⇒ 没隔离的用例会读到
        # 仓库里的盘上记录（或者把记录写进仓库）。
        previous = os.environ.get("OPENCODE_BRIDGE_CONFIG")
        config_path = os.path.join(self.bridge_dir, "config.json")
        with io.open(config_path, "w", encoding="utf-8") as handle:
            json.dump({"adapters": {}}, handle)
        os.environ["OPENCODE_BRIDGE_CONFIG"] = config_path

        def restore_env() -> None:
            if previous is None:
                os.environ.pop("OPENCODE_BRIDGE_CONFIG", None)
            else:
                os.environ["OPENCODE_BRIDGE_CONFIG"] = previous

        self.addCleanup(restore_env)
        self.assertEqual(
            cli._bridge_dir(), os.path.abspath(self.bridge_dir),
            "用例没把 bridge 目录隔离干净 —— 它会把落盘文件写进仓库",
        )
        self.recorder = health.OutboundFailureRecorder(self.bridge_dir)
        outbound.install_outbound_failure_recorder(self.recorder)
        self.addCleanup(
            outbound.install_outbound_failure_recorder, None
        )

    # --- 盘上那份记录 -------------------------------------------------
    def failure_file_path(self) -> str:
        return os.path.join(self.bridge_dir, health.OUTBOUND_FAILURES_FILE_NAME)

    def on_disk(self) -> str:
        """盘上那份记录的**原文**；文件不存在时返回 ``""``。

        ⛔ 刻意返回原文而不是解析后的结构：**"落盘这一步有没有被调用"是本文件
        最重要的判据**，而解析后的结构在"文件被写过但内容为空"时与"没写过"
        同形。原文让两者可区分。
        """
        try:
            with io.open(self.failure_file_path(), encoding="utf-8") as handle:
                return handle.read()
        except FileNotFoundError:
            return ""

    def recorded_entry(self, platform: str = "telegram") -> dict | None:
        return health.outbound_failure_from_record(
            health.read_outbound_failures(self.bridge_dir), platform
        )

    def sender_for(self, adapter: Adapter) -> OutboundSender:
        return OutboundSender(
            adapter_for=lambda conversation_id: adapter,
            max_message_chars=4000,
        )


# ======================================================================
# ① 落盘这一步**真的**被调用了（判据反退化）
# ======================================================================
class PersistStepIsActuallyWired(RecorderInstalled):
    """⭐ 判据反退化：把落盘短路掉，**同一个探针必须翻脸**。

    ⚠️ 这一组是整个文件里承重的部分。新加的落盘通路最典型的死法是"接线接错了 /
    记录器压根没被调用"，而那种情况下**其余全部用例照样全绿** —— 因为它们断言的
    都是 :class:`health.OutboundFailureRecorder` 自己的行为，而那条路径压根不经过
    ``send_text``。⇒ 于是每条"盘上有记录"的断言都配一条**同形**的反向对照：
    把 :func:`health.write_config_atomically` 换成一个什么都不做的计数桩，
    探针的输出必须**从"有内容"翻成"空"**。翻不了 = 判据恒真 = 护栏不存在。
    """

    WRITE_STUB_TARGET = "opencode_bridge.health.write_config_atomically"

    def probe_after_a_failing_send(self, *, short_circuit_persist: bool) -> str:
        """跑一次失败的出站发送，返回 ``outbound-failures.json`` 的原文。"""
        adapter = ScriptedAdapter()
        adapter.outcome = (SendError.FORBIDDEN, "bot was blocked by the user")
        sender = self.sender_for(adapter)
        calls: list = []

        def counting_stub(path, document):
            calls.append(path)
            if short_circuit_persist:
                return None            # 短路：调用发生了，但一个字都没落盘
            return real_stub(path, document)

        from opencode_bridge.pairing_cli import write_config_atomically as real_stub

        with mock.patch.object(
            health, "write_config_atomically", counting_stub
        ):
            with self.assertLogs("opencode_bridge.outbound", level="WARNING"):
                self.assertIsNone(
                    sender.send_text(CONVERSATION, "答复发不出去")
                )
        self.assertEqual(
            len(calls), 1,
            "落盘这一步一次都没被调用 ⇒ 出站失败仍然到不了用户那里",
        )
        return self.on_disk()

    def test_a_failing_send_leaves_the_reason_on_disk(self):
        """一次失败 ⇒ 分类与原因**真的**落到盘上（这条是正面判据）。"""
        on_disk = self.probe_after_a_failing_send(short_circuit_persist=False)

        self.assertNotEqual(on_disk, "", "发送失败了，盘上却什么都没有")
        entry = self.recorded_entry()
        self.assertIsNotNone(entry, "落盘的记录读不回来：" + on_disk)
        self.assertEqual(entry["kind"], SendError.FORBIDDEN.value)
        self.assertIn("blocked by the user", entry["detail"])

    def test_short_circuiting_the_persist_step_makes_that_signal_vanish(self):
        """⭐ **反向对照**：落盘被短路 ⇒ 上面那条判据必须翻脸。

        没有这一条，上一条就可能恒真（"盘上有东西"也许只是某个别的副作用写的）。
        ⚠️ 这里刻意断言的是**同一个探针的输出**，不是另一个独立断言 —— 独立断言
        会在两条断言各自被"顺手改坏"时一起失守，而它们要守的是**同一个**信号。
        """
        on_disk = self.probe_after_a_failing_send(short_circuit_persist=True)

        self.assertEqual(
            on_disk, "",
            "落盘被短路了盘上却有内容 ⇒ 上那条判据量的不是落盘，护栏恒真",
        )

    def test_the_status_view_reads_exactly_what_the_sender_wrote(self):
        """写与读必须是**同一份东西**（``--status`` 是独立进程，它只认盘）。"""
        adapter = ScriptedAdapter()
        adapter.outcome = (SendError.RATE_LIMITED, "429 retry_after=30")
        self.sender_for(adapter).send_text(CONVERSATION, "答复")

        row = next(
            row for row in cli._platform_status(
                Config(adapters={"telegram": {"bot_token": "t"}})
            )
            if row["key"] == "telegram"
        )
        self.assertIsNotNone(row["last_outbound_failure"])
        self.assertEqual(row["last_outbound_failure"]["kind"], "rate_limited")
        self.assertIn("retry_after=30", row["last_outbound_failure"]["detail"])


# ======================================================================
# ② 连续失败不会无限写盘
# ======================================================================
class WritesOnlyOnStateChange(RecorderInstalled):
    """边界 ②：出站失败可能**非常频繁**，而写盘必须是有节制的。"""

    def count_writes(self, action) -> int:
        """跑 ``action``，返回它真的写了几次盘（``write_config_atomically`` 的次数）。"""
        calls: list = []
        from opencode_bridge.pairing_cli import write_config_atomically as real_stub

        def counting_stub(path, document):
            calls.append(path)
            return real_stub(path, document)

        with mock.patch.object(health, "write_config_atomically", counting_stub):
            action()
        return len(calls)

    def test_a_hundred_consecutive_failures_cost_exactly_one_write(self):
        """⭐ 连续失败**不**反复写盘 —— 这是这条判据的核心。

        对端限流 / 用户批量操作时，失败可以每秒几十次；每次都写一次
        ``mkstemp`` + ``fsync`` + ``os.replace`` 就是在**排障功能自己的位置上**
        制造压力，而写出来的那份记录与第一次写的那份**逐字节等价**。
        """
        writes = self.count_writes(
            lambda: [self.recorder.note_failure(
                "telegram", SendError.RATE_LIMITED, "429") for _ in range(100)]
        )

        self.assertEqual(writes, 1, "连续失败不该反复写盘")
        self.assertEqual(self.recorder.failing_platforms(), ("telegram",))

    def test_a_repeated_send_failure_also_writes_only_once(self):
        """同一条断言要走**真实发送路径**：接线的错在这里也能被抓住。"""
        adapter = ScriptedAdapter()
        adapter.outcome = (SendError.TRANSIENT, "connection reset")
        sender = self.sender_for(adapter)

        writes = self.count_writes(
            lambda: [sender.send_text(CONVERSATION, "答复 %d" % index)
                     for index in range(25)]
        )

        self.assertEqual(writes, 1)

    def test_recovery_is_recorded_exactly_once_too(self):
        """「失败 → 恢复」要写（否则永远显示"正在失败"），但也**只**写一次。"""
        self.recorder.note_failure("telegram", SendError.TRANSIENT, "boom")

        writes = self.count_writes(
            lambda: [self.recorder.note_success("telegram") for _ in range(25)]
        )

        self.assertEqual(writes, 1)
        self.assertEqual(self.recorder.failing_platforms(), ())
        self.assertIn("recovered_at", self.recorded_entry())

    def test_success_without_a_failure_streak_never_writes(self):
        """「健康 → 健康」压根没有记录要改 —— 不许因为成功而去写盘。"""
        self.assertEqual(self.count_writes(lambda: self.recorder.note_success("irc")), 0)
        self.assertEqual(self.on_disk(), "", "没有任何失败就不该创建这份记录")

    def test_a_second_failure_streak_is_recorded_again(self):
        """节制的代价：恢复之后**再来**一次失败必须重新被看见。

        钉住它是"状态机"而不是"每进程只写一次"—— 后者会让第二次故障彻底静默，
        而那正是本任务要消灭的那类静默。
        """
        self.recorder.note_failure("telegram", SendError.TRANSIENT, "第一次")
        self.recorder.note_success("telegram")

        self.recorder.note_failure("telegram", SendError.TRANSIENT, "第二次")

        entry = self.recorded_entry()
        self.assertIn("第二次", entry["detail"])
        self.assertNotIn(
            "recovered_at", entry,
            "残留的 recovered_at 会挂在一次全新的失败上，说它「已恢复」",
        )


# ======================================================================
# ③ 写盘失败绝不影响发送
# ======================================================================
class PersistFailureNeverBlocksSending(RecorderInstalled):
    """边界 ②的第二半：排障记录**绝不该决定桥的生死**。

    这与 :func:`health.record_startup_probes` 的不变量同源 —— 今天刚为此加过一条
    护栏（``startup_probes`` 的实参求值曾把 ``core.start()`` 之后的桥打死）。
    """

    def run_with_a_broken_disk(self, action, *, expect_a_warning: bool = True):
        """把落盘换成「一写就抛 OSError」，跑 ``action`` 并把它的返回值交出来。

        ⚠️ ``expect_a_warning`` 默认 True —— 只在**压根不会去写盘**的那条
        （成功发送，见下）才关掉，否则 ``assertLogs`` 会因为"没日志"而失败，
        而那条失败的含义与本组要守的不变量无关。
        """
        with mock.patch.object(
            health, "write_config_atomically",
            mock.Mock(side_effect=OSError("磁盘只读")),
        ):
            if not expect_a_warning:
                return action()
            with self.assertLogs("opencode_bridge.health", level="WARNING"):
                return action()

    def test_a_successful_send_still_goes_out_when_the_disk_is_read_only(self):
        """⚠️ 注意这条的形状：成功发送**压根不会去写盘**（没有记录要改），
        所以它顺带证明"happy path 不碰排障文件"—— 那是"排障功能绝不该影响正常
        发送"最强的一条断言：不是"失败了也写进去了"，而是"成功时它根本不存在"。
        """
        adapter = ScriptedAdapter()
        sender = self.sender_for(adapter)

        handle = self.run_with_a_broken_disk(
            lambda: sender.send_text(CONVERSATION, "答复"),
            expect_a_warning=False,
        )

        self.assertIsNotNone(handle, "排障记录写不进去不许把消息一起搭进去")
        self.assertEqual(self.on_disk(), "")

    def test_a_failed_send_still_reports_no_handle_when_the_disk_is_read_only(self):
        """失败时**确实会**去写盘 ⇒ 这一条才是"写盘失败不许打断发送"。

        ⚠️ 必须断言那条 warning 也发了：只断言"没抛"的话，把整段 ``try`` 删掉
        也能过 —— 而那样排障通道坏掉就是完全静默的，正是本任务要消灭的那类静默。
        """
        adapter = ScriptedAdapter()
        adapter.outcome = (SendError.FORBIDDEN, "no permission")
        sender = self.sender_for(adapter)

        with self.assertLogs("opencode_bridge.outbound", level="WARNING"):
            handle = self.run_with_a_broken_disk(
                lambda: sender.send_text(CONVERSATION, "答复")
            )

        self.assertIsNone(handle)
        self.assertEqual(self.on_disk(), "")

    def test_a_broken_disk_while_recording_recovery_never_raises(self):
        """「恢复」那一次写盘失败同样不许抛（它在发送路径上被调用）。"""
        self.recorder.note_failure("telegram", SendError.TRANSIENT, "boom")
        adapter = ScriptedAdapter()

        with mock.patch.object(
            health, "write_config_atomically",
            mock.Mock(side_effect=OSError("磁盘只读")),
        ):
            with self.assertLogs("opencode_bridge.health", level="WARNING"):
                handle = self.sender_for(adapter).send_text(CONVERSATION, "答复")

        self.assertIsNotNone(handle)

    def test_a_raising_recorder_never_reaches_the_sender(self):
        """记录器自己抛异常也一样：用户该收到的消息照收。"""
        adapter = ScriptedAdapter()
        sender = self.sender_for(adapter)

        with mock.patch.object(
            outbound, "installed_outbound_failure_recorder",
            mock.Mock(side_effect=RuntimeError("记录器炸了")),
        ):
            with self.assertLogs("opencode_bridge.outbound", level="WARNING"):
                handle = sender.send_text(CONVERSATION, "答复")

        self.assertIsNotNone(handle)

    def test_an_unwritable_directory_is_reported_as_not_written(self):
        recorder = health.OutboundFailureRecorder(
            os.path.join(self.bridge_dir, "并不存在的目录")
        )
        with self.assertLogs("opencode_bridge.health", level="WARNING"):
            self.assertFalse(recorder.note_failure("telegram", SendError.TRANSIENT, "x"))


# ======================================================================
# ④ `--status` 那一段：有记录 / 无记录**双向**可区分
# ======================================================================
class StatusSectionWording(unittest.TestCase):
    """④ ``--status`` 的人读那一段（**另一个进程**，所以只能读盘）。"""

    SECTION_HEADER = cli._OUTBOUND_SECTION_HEADER

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

    def section(self) -> str:
        """只取出站失败那一段（到下一个 ``== `` 段为止）。

        ⚠️ 必须逐段取：``--status`` 的其余部分（含运行态、授权键冲突）本来就会
        出现"失败"字样，整段断言会把那些混进来，于是"无记录不许说失败"就恒真了。
        """
        lines = self.render().splitlines()
        start = next(i for i, line in enumerate(lines) if self.SECTION_HEADER in line)
        tail = lines[start + 1:]
        end = next((i for i, line in enumerate(tail) if line.startswith("== ")), len(tail))
        return "\n".join(tail[:end])

    def row_for_telegram(self, body: str) -> str:
        for line in body.splitlines():
            if line.strip().startswith("Telegram"):
                return line
        raise AssertionError("那段输出里找不到 Telegram 那一行：\n" + body)

    def record_a_failure(self, *, recovered: bool) -> None:
        recorder = health.OutboundFailureRecorder(self.bridge_dir)
        recorder.note_failure("telegram", SendError.FORBIDDEN, "bot 被移出群聊")
        if recovered:
            recorder.note_success("telegram")

    # --- 双向：两个方向的措辞必须互相不含 --------------------------
    def test_without_a_record_it_neither_says_ok_nor_says_failed(self):
        """⭐ 方向一：**无记录**时既不许显示成「正常」，也不许显示成「失败」。

        ``无记录`` 真的推不出"现在可达" —— 桥可能压根没发过消息。把"没观测到"
        说成"正常"就是本任务要消灭的那类假话。
        """
        body = self.section()

        self.assertIn(health.NO_OUTBOUND_FAILURE_TEXT, body)
        self.assertNotIn("正常", body)
        self.assertNotIn("上次出站失败", body)

    def test_with_a_record_it_says_the_reason_and_never_says_no_record(self):
        """⭐ 方向二：**有记录**时必须说出原因，且不许说「无记录」。"""
        self.record_a_failure(recovered=False)

        body = self.section()
        row = self.row_for_telegram(body)

        self.assertIn("上次出站失败", row)
        self.assertIn("forbidden", row)
        self.assertIn("bot 被移出群聊", row)
        self.assertNotIn(health.NO_OUTBOUND_FAILURE_TEXT, row)

    def test_an_unrecovered_failure_is_never_shown_as_broken_right_now(self):
        """⛔ 「没有观测到成功」**不许**被读成「现在还坏着」。

        盘上分不出"还在失败"与"没人再发消息"（桥空闲），而猜一个就是编造
        （AGENTS.md §8：靠猜的方案必然在某些输入上错）。
        """
        self.record_a_failure(recovered=False)

        row = self.row_for_telegram(self.section())

        self.assertIn("此后没有观测到成功", row)
        self.assertNotIn("仍在失败", row)
        self.assertNotIn("现在还坏", row)

    def test_a_recovered_failure_is_labelled_recovered_and_says_it_is_not_redelivered(self):
        """恢复要说清是"发送又成功了"，**并且**说明那次答复不会补发。

        不说后半句，用户会以为恢复之后那条消息补上了 —— 而平台没有"重投"原语。
        """
        self.record_a_failure(recovered=True)

        row = self.row_for_telegram(self.section())

        self.assertIn("已恢复于", row)
        self.assertNotIn("此后没有观测到成功", row)
        self.assertIn("不会补发", self.section())

    def test_the_section_says_the_timestamp_is_not_the_current_state(self):
        """时效性：这一段必须说清那是「上一次失败」，且不是实时状态。"""
        self.record_a_failure(recovered=False)

        body = self.section()

        self.assertIn("不是", body)
        self.assertIn("现在的连接状态", body)

    def test_the_section_says_the_two_records_answer_different_questions(self):
        """⚠️ 两个 ``--status`` 段答的**不是同一个问题**，措辞必须自己交代清楚。"""
        body = self.section()

        self.assertIn("platform-health.json", body)
        self.assertIn("outbound-failures.json", body)
        self.assertIn("各答各的", body)


# ======================================================================
# ⑤ 外部契约：``--setup --json`` 的既有 key 一个都没少
# ======================================================================
class SetupJsonContract(RecorderInstalled):
    def test_no_existing_key_of_platform_status_was_removed_or_renamed(self):
        """⛔ 护栏：既有 key 一个都没少、名字没变。

        ⛔ 期望集合是**上面那份硬编码字面量**，⛔ 不许对着 ``_platform_status``
        算一遍（那恒真 —— AGENTS.md §9「恒真的断言比没有断言更危险」）。
        """
        for row in cli._platform_status(
            Config(adapters={"telegram": {"bot_token": "t"}})
        ):
            with self.subTest(platform=row["key"]):
                missing = PLATFORM_STATUS_KEYS_BEFORE_OUTBOUND_FIELD - set(row)
                self.assertEqual(
                    missing, set(),
                    "既有 key 少了 %s —— 仓库外的消费者按值断言它们"
                    % sorted(missing),
                )
        sample = cli._platform_status(
            Config(adapters={"telegram": {"bot_token": "t"}})
        )[0]
        self.assertEqual(
            set(sample) - PLATFORM_STATUS_KEYS_BEFORE_OUTBOUND_FIELD,
            {"last_outbound_failure"},
            "本次只许新增 last_outbound_failure 这一个键",
        )

    def test_outbound_ready_still_means_credentials_and_not_deliverability(self):
        """⛔ ``outbound_ready`` 的含义**一个字没改**：它答「出站凭据齐备」。

        「凭据齐」与「真发得出去」是两件事 —— SMTP 密码改了、Telegram 把 bot
        踢了，凭据照样齐备而一条都发不出去。⚠️ 所以那个**新**字段才是回答后者的；
        改 ``outbound_ready`` 的含义会让每个既有消费者重新学一遍这套输出。
        """
        cfg = Config(adapters={"telegram": {"bot_token": "t"}})
        recorder = health.OutboundFailureRecorder(self.bridge_dir)
        recorder.note_failure("telegram", SendError.FORBIDDEN, "bot 被移出群聊")

        row = next(
            row for row in cli._platform_status(cfg) if row["key"] == "telegram"
        )

        self.assertTrue(
            row["outbound_ready"],
            "outbound_ready 答的是凭据齐不齐，发送失败不该把它翻成 False",
        )
        self.assertEqual(row["last_outbound_failure"]["kind"], "forbidden")


# ======================================================================
# ⑥ 既有四档 verdict 一个都没被改
# ======================================================================
class ExistingVerdictsUntouched(unittest.TestCase):
    def test_the_verdict_values_are_exactly_these_five_strings(self):
        """⚠️ 硬编码期望，⛔ 不要对着 ``health.VERDICTS`` 算（那恒真）。

        ``bridge_setup`` 按**值**断言 ``--setup --json`` 的现有取值，所以改一个
        字符串（哪怕只是大小写）都可能打掉别人的判据，而本仓库不会有任何测试变红。

        ⚠️ 第五档 ``does_not_probe``（2026-10-06）：它是**新增**的取值，
        既有四档的字符串一个都没动 —— 下面那五条断言就是这句话的机械保证。
        """
        self.assertEqual(health.VERDICTS, EXPECTED_VERDICTS)
        self.assertEqual(health.VERDICT_OK, "ok")
        self.assertEqual(health.VERDICT_FAILED, "failed")
        self.assertEqual(health.VERDICT_SKIPPED, "skipped")
        self.assertEqual(health.VERDICT_NOT_STARTED, "not_started")
        self.assertEqual(health.VERDICT_DOES_NOT_PROBE, "does_not_probe")

    def test_each_verdict_still_normalises_to_itself(self):
        for verdict in EXPECTED_VERDICTS:
            with self.subTest(verdict=verdict):
                self.assertEqual(
                    health.normalize_verdict(verdict, detail="d")["verdict"], verdict
                )

    def test_an_unreadable_verdict_still_becomes_failed_never_ok(self):
        """「读不懂的话绝不当成好」这条不变量不许被这次改动碰坏。"""
        with self.assertLogs("opencode_bridge.health", level="WARNING"):
            entry = health.normalize_verdict("probably-fine")
        self.assertEqual(entry["verdict"], "failed")

    def test_the_two_records_live_in_two_separate_files(self):
        """⭐ 边界 ① 的落点：启动探测与运行期失败**各有一份文件**，互不覆盖。

        ⚠️ 这不是风格问题：:func:`health.record_startup_probes` 每次启动都**整份
        替换** ``platform-health.json`` —— 同键共存的话，下次启动会顺手把上一轮
        运行期的失败记录删掉，而那恰恰是用户最需要它的时候（用户是在"收不到回信"
        之后才去查的，此刻桥已经重启过一次）。
        """
        self.assertNotEqual(
            health.PLATFORM_HEALTH_FILE_NAME, health.OUTBOUND_FAILURES_FILE_NAME
        )
        record = health.record_startup_probes.__doc__
        self.assertIsNotNone(record)
        with tempfile.TemporaryDirectory() as directory:
            recorder = health.OutboundFailureRecorder(directory)
            recorder.note_failure("telegram", SendError.TRANSIENT, "boom")
            health.record_startup_probes(directory, {
                "telegram": {"verdict": "ok", "detail": "getMe 通过"},
            })
            self.assertIsNotNone(
                health.outbound_failure_from_record(
                    health.read_outbound_failures(directory), "telegram"
                ),
                "启动探测那一轮把运行期的出站失败记录抹掉了",
            )
            self.assertEqual(
                health.probe_from_record(
                    health.read_platform_health(directory), "telegram"
                )["verdict"],
                "ok",
            )


# ======================================================================
# ⑦ 判据本身：部分送达 / 陈旧记录 / 抛异常
# ======================================================================
class OutcomeJudgement(RecorderInstalled):
    """「刚才那一次到底成没成」的判据 —— 三条都会漏掉一种真实的丢消息。"""

    def test_a_partial_send_is_recorded_rather_than_called_a_success(self):
        """分片发到一半断了：**前几片已送达、后几片没发**。

        只看句柄会把它记成成功 ⇒ 调用方以为答复整条到了，而读者少收了一截。
        """
        adapter = ScriptedAdapter()
        adapter.outcome = (SendError.TRANSIENT, "第 3 片连接断了")
        adapter.hands_back_a_handle_on_failure = True

        handle = self.sender_for(adapter).send_text(CONVERSATION, "很长的答复")

        self.assertIsNotNone(handle)
        entry = self.recorded_entry()
        self.assertIsNotNone(entry, "部分送达被当成了干净的成功")
        self.assertIn("部分送达", entry["detail"])

    def test_a_clean_success_after_a_failure_is_not_reported_as_a_failure(self):
        """⚠️ :meth:`_note_send_failure` **只写不清** —— 不清就会把成功误报成失败。

        这条曾经是真实风险：上一次失败留下的记录会被一次干净的成功原样带着，
        于是「失败 → 成功」被显示成"仍在失败"。
        """
        adapter = ScriptedAdapter()
        sender = self.sender_for(adapter)
        adapter.outcome = (SendError.FORBIDDEN, "第一次失败")
        sender.send_text(CONVERSATION, "答复")

        adapter.outcome = None                 # 干净的成功
        sender.send_text(CONVERSATION, "答复")

        entry = self.recorded_entry()
        self.assertIn("recovered_at", entry, "恢复没被记下来")
        self.assertEqual(
            adapter.last_send_error, None,
            "成功之后适配器上仍留着上一轮的失败记录 —— 判据会把它读成失败",
        )

    def test_a_send_that_raised_is_recorded_with_a_reason(self):
        """``send()`` 真抛了：适配器没机会自己记，**分类必须由这条路径补上**。

        不补的话这条会以"没有原因"进记录，而它恰恰是最该说清原因的（栈里有）。
        """
        adapter = ScriptedAdapter()
        adapter.outcome = "raise"

        with self.assertLogs("opencode_bridge.outbound", level="ERROR"):
            handle = self.sender_for(adapter).send_text(CONVERSATION, "答复")

        self.assertIsNone(handle)
        entry = self.recorded_entry()
        self.assertEqual(entry["kind"], SendError.TRANSIENT.value)
        self.assertIn("send() raised", entry["detail"])

    def test_a_send_that_returned_nothing_is_recorded_as_unknown_not_silently_ok(self):
        """``send()`` 返回 ``None`` 却什么都没记 ⇒ 分类落 ``unknown``，不是"好"。"""
        adapter = ScriptedAdapter()          # outcome 默认 None → 交句柄
        adapter.outcome = "returns-nothing"

        self.assertIsNone(self.sender_for(adapter).send_text(CONVERSATION, "答复"))

        entry = self.recorded_entry()
        self.assertEqual(entry["kind"], SendError.UNKNOWN.value)
        self.assertIn("returned None", entry["detail"])


# ======================================================================
# ⑧ 落盘形态与脱敏
# ======================================================================
class PersistedShape(RecorderInstalled):
    def test_an_unreadable_failure_kind_never_becomes_a_specific_category(self):
        """分类读不懂就归 ``unknown`` —— 归成某个具体类别才是编造。"""
        with self.assertLogs("opencode_bridge.health", level="WARNING"):
            self.recorder.note_failure("irc", "限流了大概", "429")

        self.assertEqual(self.recorded_entry("irc")["kind"], "unknown")

    def test_the_detail_is_never_larger_than_one_line(self):
        self.recorder.note_failure(
            "telegram", SendError.TRANSIENT, "第一行\n第二行\n" + "x" * 900
        )

        detail = self.recorded_entry()["detail"]
        self.assertNotIn("\n", detail)
        self.assertLessEqual(len(detail), health.MAX_DETAIL_CHARS + 1)

    def test_the_persisted_detail_never_contains_a_token_shape(self):
        """⛔ 安全红线：这份文件与 ``config.json`` 同层，用户贴日志时**整目录打包**。

        ⚠️ 夹具按 AGENTS.md §2.4 **拼接**而成（推送保护按完整形状拦整个 push），
        且先证明它**真的**被脱敏引擎认出来 —— 否则下面那条断言恒真
        （AGENTS.md §7.1「空集 ≠ 不存在」）。
        """
        import re

        token_body = ("Ab" * 17) + "Z"
        shape_token = "123456789" + ":" + token_body
        token_shape = re.compile(r"\d{8,10}:" + r"[A-Za-z0-9_-]{35}")

        from opencode_bridge import redaction

        self.assertIsNotNone(
            token_shape.search(shape_token),
            "夹具必须与 Telegram token 形状完全吻合，否则下面那条断言恒真",
        )
        self.assertNotIn(
            shape_token, redaction.default_redactor().scrub("getMe: " + shape_token)
        )

        self.recorder.note_failure(
            "telegram", SendError.FORBIDDEN, "token=" + shape_token
        )

        on_disk = self.on_disk()
        self.assertNotIn(shape_token, on_disk, "落盘文件里出现了 token 明文")
        self.assertEqual(token_shape.search(on_disk), None)
        self.assertIn("[REDACTED:telegram-bot-token]", self.recorded_entry()["detail"])

    def test_an_unreadable_file_reads_as_no_record_rather_than_crash(self):
        """排障通道自己坏了已经够糟 —— 不许让 ``--status`` 崩。"""
        with io.open(self.failure_file_path(), "w", encoding="utf-8") as handle:
            handle.write("{ 这不是 JSON")

        with self.assertLogs("opencode_bridge.health", level="WARNING"):
            self.assertIsNone(health.read_outbound_failures(self.bridge_dir))
        self.assertIsNone(health.outbound_failure_from_record(None, "telegram"))

    def test_a_platform_is_never_given_the_others_answer(self):
        """每个平台一条、彼此独立 —— 一个平台的记录不许覆盖另一个的。"""
        self.recorder.note_failure("telegram", SendError.FORBIDDEN, "telegram 那条")
        self.recorder.note_failure("irc", SendError.TRANSIENT, "irc 那条")

        record = health.read_outbound_failures(self.bridge_dir)
        self.assertEqual(
            sorted(health.outbound_failures_in_record(record)), ["irc", "telegram"]
        )
        self.assertIn("telegram 那条", self.recorded_entry("telegram")["detail"])
        self.assertIn("irc 那条", self.recorded_entry("irc")["detail"])

    def test_describe_outbound_failure_says_no_record_rather_than_ok(self):
        """⛔ 没有记录时的措辞**不是**"正常"。"""
        self.assertEqual(
            health.describe_outbound_failure(None), health.NO_OUTBOUND_FAILURE_TEXT
        )
        self.assertIn("forbidden", health.describe_outbound_failure(
            {"kind": "forbidden", "detail": "bot 被移出群聊"}
        ))


# ======================================================================
# ⑨ email 出站失败此前**一条日志都没有**
# ======================================================================
class EmailOutboundFailureIsLogged(unittest.TestCase):
    """⚠️ 全仓最坏的一处：SMTP 认证失败算出了分类，却**连日志都没有一行**。

    用户改了 SMTP 密码 ⇒ 收不到任何回信，而日志里也查不到。
    """

    def adapter(self, smtp_error: BaseException | None):
        from opencode_bridge.adapters import email as email_module

        adapter = email_module.EmailAdapter(
            {
                "address": "bot@example.com",
                "password": "hunter2",
                "smtp_host": "smtp.example.com",
                "allowed_chat_ids": ["user@example.com"],
            },
            hooks=None,  # type: ignore[arg-type]
        )
        if smtp_error is not None:
            adapter._smtp_send = lambda recipient, raw: (_ for _ in ()).throw(
                smtp_error
            )
        return adapter

    def test_an_smtp_authentication_failure_logs_the_classified_kind(self):
        """⛔ 分类（``FORBIDDEN``）必须**出现在日志里**，而不只是被算出来丢掉。"""
        import smtplib

        adapter = self.adapter(smtplib.SMTPAuthenticationError(535, b"bad creds"))

        with self.assertLogs("opencode_bridge.adapters.email", level="WARNING") as caught:
            self.assertIsNone(
                adapter.send(Outbound("email:user@example.com", "答复"))
            )

        body = "\n".join(caught.output)
        self.assertIn("forbidden", body, "日志里说不出是哪一类失败")
        self.assertIn("SMTP 认证失败", body)
        self.assertEqual(adapter.last_send_error, SendError.FORBIDDEN)

    def test_a_missing_smtp_host_also_logs(self):
        """这条同样此前**一条日志都没有**（只有 ``_note_send_failure``）。"""
        from opencode_bridge.adapters import email as email_module

        adapter = email_module.EmailAdapter(
            {"address": "bot@example.com", "password": "x"},
            hooks=None,  # type: ignore[arg-type]
        )

        with self.assertLogs("opencode_bridge.adapters.email", level="WARNING") as caught:
            self.assertIsNone(
                adapter.send(Outbound("email:user@example.com", "答复"))
            )

        self.assertIn("smtp_host", "\n".join(caught.output))


# ======================================================================
# ⑩ 结构断言：**真实启动路径**上确实装了记录器（AST，不是行级正则）
# ======================================================================
# ⚠️ **这一组存在的理由是一个被实测抓到的空护栏。**
#
# 本文件前面的 :class:`RecorderInstalled` 在 ``setUp`` 里**自己**调了一次
# ``install_outbound_failure_recorder`` ⇒ 于是**没有任何用例走进**
# ``opencode_bridge.__main__._run_bridge_locked`` ⇒ 真实运行路径上那唯一一处
# 安装被删掉时，**红了 0 条**。
#
# ⇒ 这就是「**检查独立于被检查的东西**」的通用形态：每一条用例都在验证自己搭的
# 舞台，而没人验证舞台与生产代码之间的**接线**。
# ⚠️ 也因此 :class:`PersistStepIsActuallyWired` 的名字虽像在钉这件事，实际钉的是
# **别的一层**（记录器自身），它绿着却漏掉了接线。
#
# ⛔ 所以下面两条**不走运行时**、只走**结构**：判据直接在
# ``opencode_bridge/__main__.py`` 的源码上找那次调用。形式照
# ``tests/test_config_coerce.py`` 的 ``TestNoBareNumericCoercionInAdapterLifecycles``
# （纯函数吃 ``source`` + 文件版包装 + 合成样本自守），⛔ 不自创第四种风格。

MAIN_MODULE_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "opencode_bridge" / "__main__.py"
)

#: 装记录器的那一步必须落在**这个函数**里 —— 真实启动路径。
#: ⚠️ **点名**而不是「某个函数里有就行」：点名之后，改名会**报错**（而不是让
#: 判据悄悄去看别的函数然后恒真）。这是自守测试之外的那一半保险。
BRIDGE_STARTUP_FUNCTION = "_run_bridge_locked"

#: 记录器装配的唯一公开入口（``opencode_bridge.outbound`` 里那个）。
RECORDER_INSTALL_FUNCTION = "install_outbound_failure_recorder"

#: 启动适配器的那一步 —— 「安装必须早于它」是本组第二条断言的锚点。
CORE_START_CALL = "core.start"

#: 「一定会执行」的容器语句：从调用点往上走，穿过它们仍算**无条件路径**。
#: ⚠️ 只收 ``try`` / ``with`` 是刻意的：它们的 body 一定会跑（``try`` 的
#: ``except`` 才是分支，而分支本身也在同一条路径上）。
ALWAYS_EXECUTED_CONTAINERS = (
    (ast.Try, ast.With)
    + ((ast.TryStar,) if hasattr(ast, "TryStar") else ())
)

#: 「**不一定**执行」的容器语句。
#:
#: ⚠️ **这里不试图去求值那个条件** —— 那正是「看起来像活路径、其实是死分支」这类
#: 假活路的来源（``if TYPE_CHECKING:`` / ``if __debug__ and X:`` / ``if 某开关:``）。
#: 一律判成「被挡」比逐个黑名单更严，且黑名单永远列不全。
#: ⇒ 后果是明确的：**装记录器那一步必须直接写在函数体里（或 ``try`` / ``with``
#: 内），不许藏在任何条件后面。**
CONDITIONAL_CONTAINERS = (ast.If, ast.IfExp, ast.For, ast.While)


def _parent_map(tree: ast.AST) -> dict:
    """``{子节点: 父节点}``，用来从调用点往上爬。

    ⚠️ ``ast`` **不**提供父指针，而这类判据全都需要它。
    """
    return {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }


def _enclosing_function(node: ast.AST, parents: dict):
    """``node`` 所在的最**内层**函数（没有则 ``None``）。

    ⚠️ 取「最内层」是承重的：嵌套 ``def`` 里的调用**不**随外层函数执行，
    把它算成外层的执行路径就是一次假活路。
    """
    current = parents.get(node)
    while current is not None and not isinstance(
        current, (ast.FunctionDef, ast.AsyncFunctionDef)
    ):
        current = parents.get(current)
    return current


def _blocking_reason(call: ast.Call, parents: dict, owner) -> str:
    """从 ``call`` 爬到 ``owner``，返回「它不一定执行」的理由；无条件则返回 ``""``。"""
    current = call
    while current is not owner:
        parent = parents.get(current)
        if parent is None:
            return "爬不到所属函数（AST 结构异常）"
        if parent is owner:
            break                     # 抵达**所属**那个函数 —— 不是嵌套函数
        if isinstance(parent, CONDITIONAL_CONTAINERS):
            return "被条件语句 %s 包着" % type(parent).__name__
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return "在嵌套函数 %r 里" % parent.name
        current = parent
    return ""


def recorder_install_sites_in_source(source: str) -> list[tuple[str, int, str]]:
    """源码里每一次 ``install_outbound_failure_recorder(...)`` 调用。

    :return: ``[(所属函数名, 行号, 被挡住的理由)]``；**第三项为空串 = 无条件路径**。

    ⚠️ 纯函数、吃 ``source``：这样自守测试能喂合成样本（见
    :class:`WiringCriteriaRejectBadSources`），而判据本身不需要磁盘。
    """
    tree = ast.parse(source)
    parents = _parent_map(tree)
    sites: list[tuple[str, int, str]] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == RECORDER_INSTALL_FUNCTION
        ):
            continue
        owner = _enclosing_function(node, parents)
        owner_name = getattr(owner, "name", "<模块层>")
        sites.append((owner_name, node.lineno, _blocking_reason(node, parents, owner)))
    return sorted(sites, key=lambda site: site[1])


def recorder_install_sites_on_disk() -> list[tuple[str, int, str]]:
    """:func:`recorder_install_sites_in_source` 读真实 ``__main__.py`` 的版本。"""
    return recorder_install_sites_in_source(
        MAIN_MODULE_PATH.read_text(encoding="utf-8")
    )


def core_start_lines_in_source(source: str) -> list[int]:
    """``core.start()`` 的行号（列表；0 个或多个都要如实给出）。

    ⚠️ 认的是 ``<名字>.start()`` 这种**属性调用**而不是字面量 ``"core.start()"``：
    行级正则认不出跨行写法，而这类判据最典型的失效方式就是「只认同一行」。
    """
    tree = ast.parse(source)
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "start"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "core"
    )


def install_precedes_core_start_in_source(source: str) -> tuple[bool, str]:
    """「安装语句在 ``core.start()`` 之前」这条排序是否成立；不成立时给出原因。

    ⚠️ **按源码顺序（``lineno``）判，不做数据流分析** —— 理由写在这里：
    这条通道的语义是「**启动那一刻**也必须能记」，而启动那一刻就是 ``core.start()``
    那一行被执行的瞬间；同一个函数体内、两次调用之间的先后关系在 Python 里**就是**
    源码顺序（没有 goto、没有声明前使用）。⇒ 求值顺序 = ``lineno`` 大小，
    任何更弱的判据（"都在这个函数里"）都会漏掉「挪到后面去」这个真实缺陷。
    """
    installs = recorder_install_sites_in_source(source)
    starts = core_start_lines_in_source(source)
    if not installs:
        return False, "源码里根本没有 %s(...) 调用" % RECORDER_INSTALL_FUNCTION
    if not starts:
        return False, "源码里找不到 core.start()（锚点没了，判据无从判起）"
    blocking = [site for site in installs if site[2]]
    if blocking:
        return False, "安装语句不是无条件路径上的：%s" % (blocking,)
    first_install = min(site[1] for site in installs)
    first_start = min(starts)
    if first_install >= first_start:
        return False, (
            "安装语句在第 %d 行，而 core.start() 在第 %d 行 —— "
            "安装晚于启动 ⇒ 启动那一刻发出去的消息不会被记"
            % (first_install, first_start)
        )
    return True, ""


class BridgeStartupWiresTheRecorder(unittest.TestCase):
    """①② 两条**结构**断言：真实启动路径确实装了，且装在 ``core.start()`` 之前。

    ⛔ **不许**改成「测试自己装一遍再断言装了」那种间接证明 —— 那正是这个空护栏
    最初的样子（前 37 条用例全是那种形状，所以删掉生产代码里的安装它们照样全绿）。
    """

    def sites(self) -> list[tuple[str, int, str]]:
        return recorder_install_sites_on_disk()

    def test_the_real_startup_path_really_calls_the_installer(self):
        """⭐ ① 删掉 ``__main__`` 里那次调用 ⇒ **必须变红**。

        判据点名 :data:`BRIDGE_STARTUP_FUNCTION`（真实启动路径）**且**要求它是
        **无条件路径**（第三项为空串）—— 只判「函数体内有调用」的话，把它挪进
        ``if False:`` / ``if TYPE_CHECKING:`` 就会恒真，而那正是「死分支冒充活路」。
        """
        sites = self.sites()

        self.assertTrue(
            sites,
            "__main__.py 里没有 %s(...) 调用 —— 真实启动路径上没人装配记录器，"
            "于是出站失败永远到不了 --status（删掉那行会让这个文件红）"
            % RECORDER_INSTALL_FUNCTION,
        )
        wrong_function = [site for site in sites if site[0] != BRIDGE_STARTUP_FUNCTION]
        self.assertEqual(
            wrong_function, [],
            "装配调用必须落在 %s 里（真实启动路径），现在落在：%s"
            % (BRIDGE_STARTUP_FUNCTION, wrong_function),
        )
        unconditional = [site for site in sites if not site[2]]
        self.assertTrue(
            unconditional,
            "装配调用不在无条件路径上：%s —— 藏在条件分支里就等于没有"
            % [site for site in sites if site[2]],
        )

    def test_the_install_precedes_core_start(self):
        """⭐ ② 把安装语句挪到 ``core.start()`` 之后 ⇒ **必须变红**。

        ⚠️ 这条的理由是 :meth:`Adapter.start` 之后适配器立刻可能发信（探针回复、
        启动时排队消息的补发），而那一刻之前没有记录器 ⇒ 那几条失败在
        ``outbound-failures.json`` 里会缺一段，用户排障时看到的是**不完整**的记录。

        ⛔ 锚点**只有** ``core.start()``，不含 ``BridgeCore(...)`` 构造：装配现在
        就在构造**之后**（构造不发消息），所以断言「早于构造」会断言一件今天
        并不成立的事 —— 那样的断言第一次跑就会红，红久了就被"修"成恒真。
        """
        ok, reason = install_precedes_core_start_in_source(
            MAIN_MODULE_PATH.read_text(encoding="utf-8")
        )

        self.assertTrue(ok, reason)

    def test_the_ordering_anchor_itself_is_still_found(self):
        """反"判据恒空"：``core.start()`` 这个锚点必须真的被找到了。

        ⚠️ 没有这条，一个认不出锚点的判据会**因为找不到而绿**（实现里写
        ``if not starts: return True`` 之类）—— 空集 ≠ 不存在（AGENTS.md §7.1）。
        """
        starts = core_start_lines_in_source(
            MAIN_MODULE_PATH.read_text(encoding="utf-8")
        )

        self.assertEqual(
            len(starts), 1,
            "应当恰好找到一次 core.start()，实际 %d 次（%s）—— 锚点判据坏了"
            % (len(starts), starts),
        )


class WiringCriteriaRejectBadSources(unittest.TestCase):
    """③ **自守**测试：判据被喂故意不合格的源码时必须**拒绝**。

    ⚠️ 没有自守测试的结构断言，最典型的失效方式是「函数名一改、判据静默地恒真」——
    它看起来还是绿的，而它已经什么都不检查了。所以每条判据都要有一条
    「喂它一份坏源码，要求它说不」的用例。
    """

    #: 一个**合格**的合成启动路径（判据必须接受它 —— 否则下面的"拒绝"毫无意义，
    #: 因为一个永远说"不"的判据也能"正确地"拒绝）。
    GOOD_SOURCE = (
        "def _run_bridge_locked(cfg):\n"
        "    core = BridgeCore(cfg, client, state, inbox)\n"
        "    try:\n"
        "        install_outbound_failure_recorder(recorder)\n"
        "    except Exception:\n"
        "        pass\n"
        "    core.start()\n"
    )

    def test_a_source_without_the_install_is_rejected(self):
        """判据 ①：把调用整段删掉 ⇒ 必须返回**空**。

        ⚠️ 样本是**整块**删掉 ``try``/``except``，不是只删那一行：只删一行会留下
        一个空的 ``try:``（``IndentationError``），而**语法错误的样本证明不了
        任何事** —— 它测的是 ``ast.parse`` 会不会抛，不是判据会不会认。
        """
        without_install = (
            "def _run_bridge_locked(cfg):\n"
            "    core = BridgeCore(cfg, client, state, inbox)\n"
            "    core.start()\n"
        )
        self.assertNotEqual(self.GOOD_SOURCE, without_install, "样本没改成")

        self.assertEqual(
            recorder_install_sites_in_source(without_install), [],
            "判据在源码没有那次调用时仍报了有 —— 它认的不是这次调用",
        )

    def test_a_source_with_the_install_is_accepted(self):
        """⭐ 反向对照：同一个判据在**合格**源码上必须**报出来**。

        ⚠️ 没有这一条，「拒绝」可能是判据恒空的副产品（一个永远返回 ``[]`` 的
        函数也会"正确地"拒绝上面那条）。两个方向都要卡。
        """
        sites = recorder_install_sites_in_source(self.GOOD_SOURCE)

        self.assertEqual(len(sites), 1, "判据没认出那次调用")
        self.assertEqual(sites[0][0], BRIDGE_STARTUP_FUNCTION)
        self.assertEqual(sites[0][2], "", "try/except 里的调用是无条件路径")

    def test_an_install_hidden_in_a_dead_branch_is_reported_as_blocked(self):
        """⭐ 判据能不能分辨「活路径 vs 死分支」—— 这条就是那个分辨能力的证据。

        ⚠️ 三种死法都喂：``if False`` / ``if TYPE_CHECKING`` / ``for`` 循环体。
        ⚛ **不求值那个条件**：判据一律把它们判成「被挡」，因为逐个黑名单永远列不全
        （下一个人会写 ``if os.environ.get('X'):``）。
        """
        for label, guard in (
            ("if False", "    if False:\n"),
            ("if TYPE_CHECKING", "    if TYPE_CHECKING:\n"),
            ("for", "    for _ in ():\n"),
        ):
            with self.subTest(guard=label):
                sample = (
                    "import typing\n"
                    "def _run_bridge_locked(cfg):\n"
                    + guard
                    + "        install_outbound_failure_recorder(recorder)\n"
                )
                sites = recorder_install_sites_in_source(sample)

                self.assertEqual(len(sites), 1, "%s 里的调用没被找到" % label)
                self.assertTrue(
                    sites[0][2],
                    "%s 里的调用被判成了无条件路径 —— 死分支冒充活路" % label,
                )

    def test_the_ordering_criterion_rejects_an_install_after_core_start(self):
        """判据 ②：安装挪到 ``core.start()`` **之后** ⇒ 必须拒绝。"""
        bad_order = (
            "def _run_bridge_locked(cfg):\n"
            "    core = BridgeCore(cfg, client, state, inbox)\n"
            "    core.start()\n"
            "    install_outbound_failure_recorder(recorder)\n"
        )

        ok, reason = install_precedes_core_start_in_source(bad_order)

        self.assertFalse(ok, "安装晚于 core.start() 竟然被判成成立")
        self.assertIn("core.start()", reason, "拒绝时必须说清是排序问题")

    def test_the_ordering_criterion_accepts_the_good_source(self):
        """⭐ 反向对照：合格顺序必须被接受（否则上面那条也是恒空副产品）。"""
        ok, reason = install_precedes_core_start_in_source(self.GOOD_SOURCE)

        self.assertTrue(ok, "合格源码被误拒：" + reason)

    def test_the_ordering_criterion_says_so_when_the_anchor_is_gone(self):
        """⚠️ 锚点消失时必须**明确报"无从判起"**，而不是当成通过。

        这是"空集 ≠ 不存在"在结构断言上的形态：找不到 ``core.start()`` 说明
        这个函数被大改了，此时正确的反应是**报错让人来看**，不是继续绿。
        """
        no_anchor = (
            "def _run_bridge_locked(cfg):\n"
            "    install_outbound_failure_recorder(recorder)\n"
            "    logger.info('x')\n"
        )

        ok, reason = install_precedes_core_start_in_source(no_anchor)

        self.assertFalse(ok, "锚点没了竟然判成通过")
        self.assertIn("core.start()", reason)


if __name__ == "__main__":
    unittest.main()
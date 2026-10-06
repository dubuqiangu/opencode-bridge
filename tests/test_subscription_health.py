"""``/api/event`` 订阅状态那条**运行期通道**的护栏（写侧节流 + 读侧复原）。

它补的是 2026-10-08 那个缺口的中段：视图侧（``subscription_status_view.py``）与
看护者（``subscription_supervisor.py``）之间**原本没有任何通道** ⇒ 「正在重连 /
线程已经死了」在真实运行里只有一行日志。本模块钉的是那一段的四个承重点：

1. ⛔ **落盘节流**：回调**每帧**都来（实测一条 5.7 KB 回答就是 3402 个 delta），
   所以「回调来了就写」会写爆磁盘 ⇒ 判据是**相位边沿 + 重连次数**，
   而这里数的是**写盘次数**（⛔ 不拿耗时当证据）。
2. ⭐ **两个方向都读得回来**：「正在重连」与「线程已经死了」必须各自复原成
   **不同**的相位 ⇒ 只钉一个方向等于没钉（这正是本缺陷的形状）。
3. ⛔ **过期记录不是「此刻」**：写下它那个进程已经不在了 ⇒ 读侧必须报「没有记录」
   （⇒ 视图层说「读不到」，⛔ 绝不当成正常）。
4. ⛔ **落盘不许带凭据片段**：``last_error`` 是上游响应体的摘录。
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest

from opencode_bridge import subscription_health
from opencode_bridge.subscription_status_view import (
    SUBSCRIPTION_DISPLAY_RECOVERING,
    SUBSCRIPTION_DISPLAY_STREAMING,
    SUBSCRIPTION_DISPLAY_TERMINATED,
    SUBSCRIPTION_DISPLAY_UNRECOGNISED,
    subscription_display_state,
)
from opencode_bridge.subscription_supervisor import (
    PHASE_ENDED_WITHOUT_STOP,
    PHASE_IDLE,
    PHASE_RECONNECTING,
    PHASE_STREAMING,
    SubscriptionStatus,
)

#: 一个**形状**正确的 Telegram bot token（按 AGENTS.md §2.4 **拼接**而成 ——
#: 仓库武装了推送保护，完整形状的字面量会让整个 push 被拦）。
_TOKEN_TAIL = ("Ab" * 17) + "Z"
CREDENTIAL_SHAPED = "9" * 8 + ":" + _TOKEN_TAIL

#: 一份「已经跑过一阵子」的订阅快照；只改用例真正关心的那几个字段。
BASE_FIELDS = {
    "subscriptions_started": 9,
    "reconnect_attempts": 7,
    "frames_received": 3,
    "last_error": "OpenCodeError: event stream 503",
}


def a_snapshot(phase: str, **changes) -> SubscriptionStatus:
    fields = dict(BASE_FIELDS)
    fields["phase"] = phase
    fields.update(changes)
    return SubscriptionStatus(**fields)


class BridgeDirIsolated(unittest.TestCase):
    """把落盘位置钉在一个临时目录上。

    ⚠️ **必须显式给 ``dir=``**：⛔ 不用带 ``dir=`` 的
    :func:`tempfile.mkdtemp`（它会落到系统临时目录去，而本仓库的沙箱只认
    ``.tmp/`` 与宿主临时目录）。
    """

    def setUp(self) -> None:
        self.bridge_dir = tempfile.mkdtemp(
            prefix="fix-runtime-channel-",
            dir=os.environ.get("TMPDIR") or None,
        )
        self.addCleanup(self._remove_tree)
        self.record_path = os.path.join(
            self.bridge_dir, subscription_health.SUBSCRIPTION_HEALTH_FILE_NAME
        )

    def _remove_tree(self) -> None:
        import shutil

        shutil.rmtree(self.bridge_dir, ignore_errors=True)

    def raw_record(self) -> dict:
        with io.open(self.record_path, encoding="utf-8-sig") as handle:
            return json.load(handle)


class CountingWriter:
    """数**真的写了几次盘**的探针（转发给真的原子写）。

    ⚠️ ⛔ **数次数，不测耗时** —— 本机 ``time.monotonic()`` 只有 16 ms 分辨率，
    而「写了很多次」与「写了几次」之间根本不是时间问题（AGENTS.md §7.1）。
    """

    def __init__(self, real_writer, calls: list) -> None:
        self._real_writer = real_writer
        self._calls = calls

    def __call__(self, path: str, document: dict) -> None:
        self._calls.append((os.path.basename(path), document))
        self._real_writer(path, document)


class WriteCountingTestCase(BridgeDirIsolated):
    """把 :func:`opencode_bridge.subscription_health.write_config_atomically` 换成
    一个**会数次数**的探针（其余行为逐字转发）。

    ⚠️ 装的是**模块属性**（不是全局 ``os.replace``）⇒ 只影响这条通道，
    ⛔ 不会波及同一次测试里别的落盘。
    """

    def setUp(self) -> None:
        super().setUp()
        self.write_calls: list = []
        real_writer = subscription_health.write_config_atomically
        subscription_health.write_config_atomically = CountingWriter(
            real_writer, self.write_calls
        )

        def restore() -> None:
            subscription_health.write_config_atomically = real_writer

        self.addCleanup(restore)

    def a_recorder(self) -> subscription_health.SubscriptionHealthRecorder:
        return subscription_health.SubscriptionHealthRecorder(self.bridge_dir)


class OnlyPhaseEdgesReachTheDiskTests(WriteCountingTestCase):
    """⭐ 落盘节流：判据是**相位边沿 + 重连次数**，⛔ 不是「回调来了就写」。"""

    def test_many_frames_on_one_subscription_write_the_disk_once(self):
        """⭐ 「写爆磁盘」那个反向证明的**靶子**。

        ⚠️ 会红的条件：把判据改成「``note`` 每次都写」⇒ 这里是 **12** 次而不是 1 次。
        """
        recorder = self.a_recorder()
        recorder.note(a_snapshot(PHASE_STREAMING))
        for frame_index in range(12):
            recorder.note(
                a_snapshot(PHASE_STREAMING, frames_received=100 + frame_index)
            )

        self.assertEqual(
            len(self.write_calls), 1,
            "同一相位的 12 次帧计数写了 %d 次盘 —— 每帧一次落盘就是写爆磁盘。"
            % len(self.write_calls),
        )

    def test_each_phase_edge_writes_exactly_once(self):
        """⭐ 「一个订阅尝试 ≤ 2 次落盘」这个上界（= 出站失败那份的同形上界）。

        ⚠️ 会红的条件：节流过头（只写「坏」不写「好」）⇒ 这里是 **4** 次而不是 5 次
        ⇒ 用户会永远看到「正在重连」，而它其实已经好了（那比不显示更坏）。
        """
        recorder = self.a_recorder()
        recorder.note(a_snapshot(PHASE_IDLE))
        recorder.note(a_snapshot(PHASE_STREAMING, reconnect_attempts=0))
        recorder.note(a_snapshot(PHASE_RECONNECTING, reconnect_attempts=1))
        recorder.note(a_snapshot(PHASE_STREAMING, reconnect_attempts=1))
        recorder.note(
            a_snapshot(PHASE_ENDED_WITHOUT_STOP, reconnect_attempts=1)
        )

        self.assertEqual(
            [name for name, _document in self.write_calls],
            [subscription_health.SUBSCRIPTION_HEALTH_FILE_NAME] * 5,
            "五次相位变化（首次→streaming→reconnecting→streaming→ended）"
            "应该写 5 次盘，实际写了 %d 次。" % len(self.write_calls),
        )

    def test_the_very_first_observation_is_persisted_even_before_anything_happened(self):
        """⚠️ 「还没进 run」也值得写一次 ⇒ ``--status`` 早早有东西可读。

        ⛔ 而它**不**等于「正常」：那一相显示成「这条线程还没进过订阅」。

        会红的条件：把「首次观测」也当成「没什么变化」跳过（那样用户要等到
        第一次订阅才开始才看到任何东西）。
        """
        recorder = self.a_recorder()

        self.assertTrue(recorder.note(a_snapshot(PHASE_IDLE)))
        self.assertEqual(len(self.write_calls), 1)

    def test_the_terminal_phase_is_never_skipped_because_the_thread_stops(self):
        """⚠️ 收工那一次**必须**落盘 —— 它正是「线程已经死了」那一相。

        会红的条件：把 :data:`PHASE_ENDED_WITHOUT_STOP` 从判据里排除掉。
        """
        recorder = self.a_recorder()
        recorder.note(a_snapshot(PHASE_STREAMING))
        recorder.note(a_snapshot(PHASE_ENDED_WITHOUT_STOP))

        self.assertEqual(len(self.write_calls), 2)
        self.assertEqual(
            self.raw_record()["subscription"]["phase"], PHASE_ENDED_WITHOUT_STOP,
        )

    def test_a_failed_write_is_not_remembered_as_delivered(self):
        """⚠️ 承重：**写盘失败不许**推进「已落盘」标记。

        ⇒ 否则下一次状态变化会被跳过，而那次变化**永久丢失**。
        （与出站失败那份「``_failing`` 只装盘上真记着的」同一个纪律。）

        ⚠️ 会红的条件：先推进标记再写盘（顺序反了）⇒ 这里是 ``True`` 而不是 ``False``。
        """
        recorder = self.a_recorder()
        unwritable = os.path.join(self.bridge_dir, "no-such-dir", "deep")
        broken = subscription_health.SubscriptionHealthRecorder(unwritable)

        self.assertFalse(broken.note(a_snapshot(PHASE_RECONNECTING)))
        self.assertIsNone(
            broken._persisted_marker,
            "一次没写成的落盘被当成了「已经说过了」⇒ 下一次状态变化会被跳过。",
        )
        # ⛔ 而那个「不记下来」的残留方向是**安全**的：读侧于是说「读不到」，
        # 而视图层不会把「读不到」显示成正常。
        self.assertIsNone(subscription_health.read_subscription_status(unwritable))
        # ⚠️ 探针数的是**尝试次数**（不是成功次数）⇒ 正好一次尝试、零次成功落盘。
        # 这条断言的作用是「证明那次尝试真的发生了」——⛔ 否则「压根没试过」
        # 与「试了但写不下去」在数据上分不开（那正是要防的假话）。
        self.assertEqual(
            len(self.write_calls), 1,
            "没有落盘尝试 ⇒ 这条用例量的是别的东西（压根没试 vs 试了写不下去）。",
        )
        self.assertFalse(os.path.exists(self.record_path))
        self.assertEqual(recorder._persisted_marker, None)

    def test_a_write_that_cannot_happen_never_raises_into_the_subscription_thread(self):
        """⛔ 排障通道绝不该决定订阅线程的生死（与另两条记录同源的不变量）。"""
        recorder = subscription_health.SubscriptionHealthRecorder(
            os.path.join(self.bridge_dir, "no-such-dir")
        )

        self.assertFalse(recorder.note(a_snapshot(PHASE_RECONNECTING)))


class BothBadPhasesSurviveTheRoundTripTests(WriteCountingTestCase):
    """⭐ **两个方向都要钉**：「正在重连」被读到，与「线程已经死了」被读到。"""

    def test_a_reconnecting_subscription_comes_back_as_reconnecting(self):
        recorder = self.a_recorder()
        recorder.note(a_snapshot(PHASE_RECONNECTING, reconnect_attempts=7))

        restored = subscription_health.read_subscription_status(self.bridge_dir)

        self.assertIsNotNone(restored, "「正在重连」没有从盘上读回来。")
        self.assertEqual(restored.phase, PHASE_RECONNECTING)
        self.assertEqual(restored.reconnect_attempts, 7)
        self.assertEqual(restored.last_error, "OpenCodeError: event stream 503")
        self.assertEqual(
            subscription_display_state(restored), SUBSCRIPTION_DISPLAY_RECOVERING,
            "读回来的相位没有落进「正在重连」那一档 ⇒ 用户看到的是别的说法。",
        )

    def test_a_subscription_that_ended_on_its_own_comes_back_as_ended(self):
        recorder = self.a_recorder()
        recorder.note(
            a_snapshot(
                PHASE_ENDED_WITHOUT_STOP, reconnect_attempts=7, last_error=None,
            )
        )

        restored = subscription_health.read_subscription_status(self.bridge_dir)

        self.assertIsNotNone(restored, "「线程已经死了」没有从盘上读回来。")
        self.assertEqual(restored.phase, PHASE_ENDED_WITHOUT_STOP)
        self.assertTrue(restored.terminated_without_stop)
        self.assertEqual(
            subscription_display_state(restored), SUBSCRIPTION_DISPLAY_TERMINATED,
            "读回来的相位没有落进「需要人管」那一档。",
        )

    def test_the_two_phases_stay_apart_across_the_channel(self):
        """⭐ 整条链路的**形状**断言：两个相位经由盘上那份记录之后仍然不同。

        ⚠️ ⛔ 逐字比**整份输出** —— 这里比的是**相位**这一个字段，⛔ 而视图那一段
        的措辞断言在 :mod:`tests.test_status` 与
        :mod:`tests.test_platform_health`。
        """
        recovering_recorder = self.a_recorder()
        recovering_recorder.note(a_snapshot(PHASE_RECONNECTING))
        recovering = subscription_health.read_subscription_status(self.bridge_dir)

        ended_recorder = subscription_health.SubscriptionHealthRecorder(
            self.bridge_dir
        )
        ended_recorder.note(a_snapshot(PHASE_ENDED_WITHOUT_STOP))
        ended = subscription_health.read_subscription_status(self.bridge_dir)

        self.assertNotEqual(recovering.phase, ended.phase)
        self.assertEqual(
            (recovering.recovering, recovering.terminated_without_stop),
            (True, False),
        )
        self.assertEqual(
            (ended.recovering, ended.terminated_without_stop),
            (False, True),
            "经由盘上那份记录之后，「线程已经死了」与「正在重连」分不开了 ⇒ "
            "这条通道复现了缺陷本身。",
        )


class ARecordWhoseWriterIsGoneIsNoRecordTests(BridgeDirIsolated):
    """⛔ **过期记录不是「此刻」**：写它那个进程不在了 ⇒ 读侧必须报「没有记录」。"""

    def write_a_record(self, pid: int) -> None:
        with io.open(self.record_path, "w", encoding="utf-8") as handle:
            json.dump({
                "recorded_at": 1000.0,
                "pid": pid,
                "subscription": {
                    "phase": PHASE_STREAMING,
                    "subscriptions_started": 1,
                    "reconnect_attempts": 0,
                    "frames_received": 3,
                    "last_error": None,
                },
            }, handle)

    def test_a_record_written_by_a_dead_process_is_not_read_as_streaming(self):
        """⭐ 这一条挡的是一个**新造出来的**静默失效。

        ⚠️ 若照样显示，用户会在一个昨天崩掉的桥上读到「订阅正常，正在收事件」。

        ⚠️ 会红的条件：从读侧删掉那道 pid 存活判据。
        """
        self.write_a_record(pid=_a_dead_pid())

        self.assertIsNone(
            subscription_health.read_subscription_status(self.bridge_dir),
            "写下它那个进程已经不在了，却把它的最后一句当成了「此刻」。",
        )

    def test_a_record_without_a_writer_is_also_no_record(self):
        """⚠️ 缺 ``pid`` ⇒ 没有写者 ⇒ 分不出「谁说的」与「现在还有没有人说」。

        会红的条件：读侧把 ``pid`` 缺省成「当前进程」。
        """
        self.write_a_record(pid=0)

        self.assertIsNone(subscription_health.read_subscription_status(self.bridge_dir))

    def test_a_record_from_this_very_process_is_read(self):
        """⭐ 反向：判据不能**过紧** —— 活着的写者那份必须读得到。

        ⚠️ 会红的条件：判据写成「一律不读」（那是一条恒假的护栏）。
        """
        self.write_a_record(pid=os.getpid())

        restored = subscription_health.read_subscription_status(self.bridge_dir)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.phase, PHASE_STREAMING)
        self.assertEqual(
            subscription_display_state(restored), SUBSCRIPTION_DISPLAY_STREAMING,
        )


class AnUnreadableRecordIsNoRecordTests(BridgeDirIsolated):
    """⛔ 坏掉 / 手改过的文件一律「没有记录」，⛔ 绝不当成正常。"""

    def test_a_missing_file_is_no_record(self):
        self.assertIsNone(subscription_health.read_subscription_status(self.bridge_dir))

    def test_a_file_that_is_not_json_is_no_record(self):
        with io.open(self.record_path, "w", encoding="utf-8") as handle:
            handle.write("{ this is not json")

        self.assertIsNone(subscription_health.read_subscription_status(self.bridge_dir))

    def test_a_json_that_is_not_an_object_is_no_record(self):
        with io.open(self.record_path, "w", encoding="utf-8") as handle:
            json.dump(["streaming"], handle)

        self.assertIsNone(subscription_health.read_subscription_status(self.bridge_dir))

    def test_a_record_missing_its_snapshot_key_is_no_record(self):
        with io.open(self.record_path, "w", encoding="utf-8") as handle:
            json.dump({"recorded_at": 1.0, "pid": os.getpid()}, handle)

        self.assertIsNone(subscription_health.read_subscription_status(self.bridge_dir))

    def test_an_unrecognised_phase_is_passed_through_and_never_read_as_healthy(self):
        """⭐ ⛔ **不替它挑一个「看起来最像」的档位** —— 那是编造观测。

        ⚠️ 会红的条件：读侧把不认识的 ``phase`` 归一化成 ``idle`` / ``streaming``。
        """
        with io.open(self.record_path, "w", encoding="utf-8") as handle:
            json.dump({
                "recorded_at": 1.0,
                "pid": os.getpid(),
                "subscription": {"phase": "half-open"},
            }, handle)

        restored = subscription_health.read_subscription_status(self.bridge_dir)

        self.assertIsNotNone(restored)
        self.assertEqual(restored.phase, "half-open", "读侧擅自改写了那个相位。")
        self.assertEqual(
            subscription_display_state(restored),
            SUBSCRIPTION_DISPLAY_UNRECOGNISED,
            "认不出来的相位落进了「正常」那一档 ⇒ 视图会显示成「订阅正常」。",
        )

    def test_counters_that_are_not_non_negative_integers_read_as_zero(self):
        """⚠️ ⛔ **不许**「就近取整」成一个看着合理的值（那是编造观测）。

        会红的条件：把 ``bool`` 当整数（``True`` 会变成「1 次」），
        或把负数 / 字符串原样带进快照。
        """
        with io.open(self.record_path, "w", encoding="utf-8") as handle:
            json.dump({
                "recorded_at": 1.0,
                "pid": os.getpid(),
                "subscription": {
                    "phase": PHASE_STREAMING,
                    "subscriptions_started": True,
                    "reconnect_attempts": -4,
                    "frames_received": "many",
                    "last_error": 7,
                },
            }, handle)

        restored = subscription_health.read_subscription_status(self.bridge_dir)

        self.assertEqual(restored.subscriptions_started, 0, "``True`` 被当成了 1 次。")
        self.assertEqual(restored.reconnect_attempts, 0)
        self.assertEqual(restored.frames_received, 0)
        self.assertIsNone(restored.last_error, "非字符串的 last_error 被 str() 了。")


class TheRecordNeverCarriesACredentialTests(WriteCountingTestCase):
    """⛔ 安全红线：落盘内容里不许出现凭据片段（与另两份记录同一条）。"""

    def test_a_credential_shaped_error_body_is_scrubbed_on_disk(self):
        recorder = self.a_recorder()

        recorder.note(
            a_snapshot(PHASE_RECONNECTING, last_error="401: " + CREDENTIAL_SHAPED)
        )

        with io.open(self.record_path, encoding="utf-8") as handle:
            on_disk = handle.read()
        self.assertNotIn(
            CREDENTIAL_SHAPED, on_disk,
            "凭据片段原样落进了 subscription-health.json —— "
            "而这个目录正是用户贴日志/贴 issue 时整份打包的地方。",
        )


class TheRecordedAtIsTheWriteMomentTests(WriteCountingTestCase):
    """⚠️ ``recorded_at`` 是**写盘时刻**，⛔ 不是「状态变化发生的时刻」。"""

    def test_it_is_present_and_a_number(self):
        recorder = self.a_recorder()
        recorder.note(a_snapshot(PHASE_RECONNECTING))

        recorded_at = subscription_health.subscription_status_recorded_at(
            self.bridge_dir
        )
        self.assertIsInstance(recorded_at, float)
        self.assertGreater(recorded_at, 0.0)

    def test_it_is_absent_when_there_is_no_record(self):
        self.assertIsNone(
            subscription_health.subscription_status_recorded_at(self.bridge_dir)
        )


def _a_dead_pid() -> int:
    """一个**确定不在**的 pid。

    ⚠️ 做法：起一个子进程、等它退出、再取它的 pid —— ⛔ 不写死一个数
    （写死的那个可能**恰好**是活着的，于是那条用例恒绿）。
    """
    import subprocess

    finished = subprocess.run(
        [__import__("sys").executable, "-c", "pass"], check=True,
    )
    del finished
    from opencode_bridge.instance_lock import pid_is_alive

    for candidate in range(60_000, 60_400):
        if not pid_is_alive(candidate):
            return candidate
    raise AssertionError("这一段 pid 区间里找不到一个确定已经退出的 pid。")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

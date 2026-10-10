"""G2 守门：续行缓冲（``..`` / ``!!``）的崩溃快照与启动重灌。

根因（见 ``tasks.md`` / ``held_buffer_store.py`` 模块 docstring）：
``_deliver_plain_text`` 的 HELD 早退发生在写前收件箱之前 ⇒ ``..`` 续行缓冲只活在
内存 dict，进程崩溃即静默丢失——写前收件箱救不了它（早退在落盘之前），恢复层也
找不到它（盘上没有行）。修复 = 整份快照落盘 + 启动重灌，本文件三层各钉一层：

* :class:`HeldBufferStoreContractTests` —— 磁盘契约（roundtrip / 缺文件首启 /
  损坏不清档 / 形状非法 / 空快照照写 / 写失败上抛 / 无 ``.staging`` 残留）；
* :class:`MergerHeldSnapshotCallbackTests` —— 四个变异点都必须把整份快照交给
  回调（锁内拷贝），外加 ``restore`` 的 set 语义与重臂保险丝；
* :class:`GatewayHeldBufferWiringTests` —— 接线守卫（可选注入意味着漏接线不会有
  任何测试变红，所以必须显式断言）：入站落盘、重启重灌 + 找回告知
  ``kind="buffered"``（a2a 非终态档，⛔ 不许回退 ``"text"``）、``store=None``
  纯内存旧行为、写盘失败只 WARNING。

⛔ 同步一律用 ``threading.Event``，不用 sleep（计时判据纪律）。
"""

from __future__ import annotations

import os
import tempfile
import threading
import unittest

from opencode_bridge.held_buffer_store import HELD_BUFFER_FILE_NAME, HeldBufferStore
from opencode_bridge.hooks import Inbound
from opencode_bridge.inbound_gateway import InboundGateway
from opencode_bridge.inbound_merge import (
    BUFFERED_NOTICE,
    BUFFERED_RESTORED_NOTICE,
    CONTINUE_SUFFIX,
    DELIVER,
    IGNORED,
    ConversationMerger,
)
from opencode_bridge.permission_ledger import PermissionLedger

#: 仓库内的临时目录：即便 TEMP/TMP 指向机器别处，测试也不可能写到仓库之外。
_REPOSITORY_TEMP = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".tmp"
)

PLATFORM = "telegram"
CONVERSATION = "quibblechat:room"

#: 保险丝测试用的短超时；其余 merger 用例用长超时，确保断言跑在保险丝到点之前
#:（否则快照会在断言中途被 fuse 线程清成空档——那不是被测行为）。
FUSE_SECONDS = 0.05
LONG_HOLD_SECONDS = 30.0

#: 死锁守卫上限：被 ``Event.set()`` 唤醒才是被测路径，超时只许响亮失败。
_EVENT_WAIT_DEADLINE_SECONDS = 5.0


def _message(text: str, message_id: str) -> Inbound:
    """一条普通入站文本（形状镜像 tests/test_inbound_gateway 的 ``message``）。"""
    return Inbound(
        conversation_id=CONVERSATION,
        platform=PLATFORM,
        message_id=message_id,
        text=text,
    )


class _RecordingSendText:
    """记下每一次出站发送 ``(conversation_id, text, kind)``——回执 kind 钉靠它。"""

    def __init__(self) -> None:
        self.outgoing: list[tuple[str, str, str]] = []

    def __call__(self, conversation_id, text, kind="text", adapter=None):
        self.outgoing.append((conversation_id, text, kind))


class _RecordingPromptClient:
    """守「重灌绝不投递」：任何 prompt 调用都被记录，测试断言它恒为 0。"""

    def __init__(self) -> None:
        self.prompts: list[tuple] = []

    def prompt(self, *args, **kwargs):
        self.prompts.append((args, kwargs))


class _ConfirmedEventStream:
    """``stream_confirmed`` 替身：立即确认（恢复路径等事件流的语义不在此处测）。"""

    def wait(self, timeout=None):
        return True


class _StandInAdapter:
    """挂上身的替身适配器。

    ``on_inbound`` 对 ``adapter_for`` 返回 ``None`` 的会话**直接丢消息**
    （只留一条 warning，进不了合并器）—— 所以哪怕只测缓冲/恢复路径，
    适配器也必须非 ``None``。本文件不触碰适配器能力查询，空壳即可。
    """


class _ExplodingHeldBufferStore:
    """写必炸的假快照层：守「写盘失败只许 WARNING、⛔ 不许打断入站」。"""

    path = "not-a-real-path"

    def read(self) -> dict[str, str]:
        return {}

    def write(self, snapshot) -> None:
        raise OSError("disk on fire")


class HeldBufferStoreContractTests(unittest.TestCase):
    """``held-buffer.json`` 的读写契约（自身无状态，每次调用都走一次磁盘）。"""

    def setUp(self) -> None:
        os.makedirs(_REPOSITORY_TEMP, exist_ok=True)
        temp_dir = tempfile.TemporaryDirectory(dir=_REPOSITORY_TEMP)
        self.addCleanup(temp_dir.cleanup)
        self.temp_dir_name = temp_dir.name
        self.snapshot_path = os.path.join(
            self.temp_dir_name, HELD_BUFFER_FILE_NAME
        )
        self.store = HeldBufferStore(self.snapshot_path)

    def test_a_snapshot_roundtrips_through_disk(self) -> None:
        self.store.write({CONVERSATION: "崩溃前的半行"})
        self.assertEqual(self.store.read(), {CONVERSATION: "崩溃前的半行"})

    def test_a_missing_file_is_a_silent_first_start(self) -> None:
        """缺文件 = 首次启动，静默空档（这一路不许有任何日志噪音）。"""
        self.assertEqual(self.store.read(), {})

    def test_invalid_json_yields_empty_but_keeps_the_file(self) -> None:
        """损坏档不清掉：清掉等于把一次可挽救的丢失变成确定丢失。"""
        with open(self.snapshot_path, "w", encoding="utf-8") as broken_file:
            broken_file.write("{写了一半就被杀")
        with self.assertLogs(
            "opencode_bridge.held_buffer_store", level="WARNING"
        ):
            self.assertEqual(self.store.read(), {})
        self.assertTrue(os.path.isfile(self.snapshot_path))

    def test_a_wrong_shape_yields_empty_but_keeps_the_file(self) -> None:
        """值不是 str = 形状不对：同损坏处置——告警、空档、盘上原样保留。"""
        with open(self.snapshot_path, "w", encoding="utf-8") as wrong_shape_file:
            wrong_shape_file.write('{"conv": 17}')
        with self.assertLogs(
            "opencode_bridge.held_buffer_store", level="WARNING"
        ):
            self.assertEqual(self.store.read(), {})
        self.assertTrue(os.path.isfile(self.snapshot_path))

    def test_an_empty_snapshot_is_written_not_skipped(self) -> None:
        """空快照也照写 = 显式清档；不写的话旧档会在下次启动被重灌一遍。"""
        self.store.write({CONVERSATION: "旧的一行"})
        self.store.write({})
        self.assertEqual(self.store.read(), {})
        self.assertTrue(os.path.isfile(self.snapshot_path))

    def test_a_write_into_a_missing_directory_raises(self) -> None:
        """写失败上抛——「要不要打断入站」只有调用方知道，本模块不替它定。"""
        lost_store = HeldBufferStore(
            os.path.join(
                self.temp_dir_name, "no-such-dir", HELD_BUFFER_FILE_NAME
            )
        )
        with self.assertRaises(OSError):
            lost_store.write({CONVERSATION: "一行"})

    def test_a_successful_write_leaves_no_staging_file_behind(self) -> None:
        """原子写：``.staging`` 只许在飞行中存在，``os.replace`` 后零残留。"""
        self.store.write({CONVERSATION: "一行"})
        leftovers = [
            name
            for name in os.listdir(self.temp_dir_name)
            if ".staging" in name
        ]
        self.assertEqual(leftovers, [])


class MergerHeldSnapshotCallbackTests(unittest.TestCase):
    """四个变异点（进缓冲 / 投递 / flush / 保险丝）+ ``restore`` 的快照契约。"""

    def setUp(self) -> None:
        self.expired: list[tuple[str, str]] = []
        self.expired_signal = threading.Event()
        self.snapshots: list[dict[str, str]] = []

    def _build_merger(self, hold_timeout_seconds: float) -> ConversationMerger:
        merger = ConversationMerger(
            hold_timeout_seconds=hold_timeout_seconds,
            on_hold_expired=self._on_hold_expired,
            on_held_state_changed=self.snapshots.append,
        )
        self.addCleanup(merger.stop)
        return merger

    def _on_hold_expired(self, conversation_id: str, held_text: str) -> None:
        self.expired.append((conversation_id, held_text))
        self.expired_signal.set()

    def test_holding_a_line_publishes_the_whole_snapshot(self) -> None:
        merger = self._build_merger(LONG_HOLD_SECONDS)
        result = merger.ingest(CONVERSATION, "第一段" + CONTINUE_SUFFIX)
        self.assertEqual(result.kind, "held")
        self.assertEqual(self.snapshots, [{CONVERSATION: "第一段"}])

    def test_a_bare_continue_marker_publishes_nothing(self) -> None:
        """光一个 ``..``：没有可缓冲的内容 ⇒ 不是状态变异 ⇒ 不许出快照。"""
        merger = self._build_merger(LONG_HOLD_SECONDS)
        result = merger.ingest(CONVERSATION, CONTINUE_SUFFIX)
        self.assertEqual(result.kind, IGNORED)
        self.assertEqual(self.snapshots, [])

    def test_delivering_the_next_line_publishes_an_empty_snapshot(self) -> None:
        merger = self._build_merger(LONG_HOLD_SECONDS)
        merger.ingest(CONVERSATION, "第一段" + CONTINUE_SUFFIX)
        result = merger.ingest(CONVERSATION, "第二行")
        self.assertEqual(result.kind, DELIVER)
        self.assertEqual(self.snapshots[-1], {})

    def test_flushing_publishes_an_empty_snapshot(self) -> None:
        merger = self._build_merger(LONG_HOLD_SECONDS)
        merger.ingest(CONVERSATION, "第一段" + CONTINUE_SUFFIX)
        flushed = merger.flush(CONVERSATION)
        self.assertEqual(flushed, "第一段")
        self.assertEqual(self.snapshots[-1], {})

    def test_the_fired_fuse_publishes_an_empty_snapshot(self) -> None:
        merger = self._build_merger(FUSE_SECONDS)
        merger.ingest(CONVERSATION, "敲完就走了" + CONTINUE_SUFFIX)
        # 保险丝线程先在锁内清档（快照先出）、再在锁外回调 expired —— 事件置位
        # 时快照必然已经出版，无需计时。
        self.assertTrue(
            self.expired_signal.wait(timeout=_EVENT_WAIT_DEADLINE_SECONDS)
        )
        self.assertEqual(self.expired, [(CONVERSATION, "敲完就走了")])
        self.assertEqual(self.snapshots[-1], {})

    def test_restore_pours_the_line_back_and_rearms_the_fuse(self) -> None:
        merger = self._build_merger(FUSE_SECONDS)
        merger.restore(CONVERSATION, "崩溃前的半行")
        self.assertEqual(merger.held_text(CONVERSATION), "崩溃前的半行")
        self.assertEqual(self.snapshots, [{CONVERSATION: "崩溃前的半行"}])
        # 重臂保险丝：它等的从来是「下一行」，不是「从盘上回来的那一刻」——
        # 超时同样把重灌的内容交出去（锁内清档 ⇒ 快照先于 expired 事件出版）。
        self.assertTrue(
            self.expired_signal.wait(timeout=_EVENT_WAIT_DEADLINE_SECONDS)
        )
        self.assertEqual(self.expired, [(CONVERSATION, "崩溃前的半行")])
        self.assertEqual(self.snapshots[-1], {})

    def test_restore_overwrites_what_is_already_held(self) -> None:
        """set 语义（照抄 dsh ``restore(key, buffer)``）：该会话已有缓冲则覆盖。"""
        merger = self._build_merger(LONG_HOLD_SECONDS)
        merger.ingest(CONVERSATION, "第一段" + CONTINUE_SUFFIX)
        merger.restore(CONVERSATION, "覆盖的一行")
        self.assertEqual(merger.held_text(CONVERSATION), "覆盖的一行")
        self.assertEqual(self.snapshots[-1], {CONVERSATION: "覆盖的一行"})

    def test_restore_ignores_an_empty_key_or_empty_text(self) -> None:
        merger = self._build_merger(LONG_HOLD_SECONDS)
        merger.restore("", "有正文")
        merger.restore(CONVERSATION, "")
        self.assertEqual(self.snapshots, [])
        self.assertEqual(merger.held_conversation_ids(), ())

    def test_two_argument_construction_stays_pure_memory(self) -> None:
        """两参直构（既有测试的形状）必须照旧可用：``None`` 回调不出锁、不落盘。"""
        merger = ConversationMerger(
            hold_timeout_seconds=LONG_HOLD_SECONDS,
            on_hold_expired=self._on_hold_expired,
        )
        self.addCleanup(merger.stop)
        result = merger.ingest(CONVERSATION, "第一段" + CONTINUE_SUFFIX)
        self.assertEqual(result.kind, "held")
        self.assertEqual(merger.held_text(CONVERSATION), "第一段")
        self.assertEqual(self.snapshots, [])


class GatewayHeldBufferWiringTests(unittest.TestCase):
    """接线守卫：``held_buffer_store`` 是可选注入，漏接线不会让任何测试变红，
    所以「真的接上了」必须在这里显式断言。"""

    def setUp(self) -> None:
        os.makedirs(_REPOSITORY_TEMP, exist_ok=True)
        temp_dir = tempfile.TemporaryDirectory(dir=_REPOSITORY_TEMP)
        self.addCleanup(temp_dir.cleanup)
        self.snapshot_path = os.path.join(
            temp_dir.name, HELD_BUFFER_FILE_NAME
        )
        self.send_text = _RecordingSendText()
        self.client = _RecordingPromptClient()

    def build_gateway(self, held_buffer_store) -> InboundGateway:
        gateway = InboundGateway(
            client=self.client,
            lock=threading.RLock(),
            turns={},
            inbox=None,
            held_buffer_store=held_buffer_store,
            stream_confirmed=_ConfirmedEventStream(),
            adapter_for=lambda conversation_id: _StandInAdapter(),
            answer_callback=lambda adapter, query_id, text: None,
            ensure_session=lambda conversation_id, *, platform="": "ses_fake0001",
            handle_command=lambda conversation_id, adapter, text: None,
            remember_platform=lambda conversation_id, platform: None,
            send_text=self.send_text,
            permission_ledger=PermissionLedger(),
            bridge_config={},
        )
        self.addCleanup(gateway._merger.stop)
        return gateway

    def test_a_held_line_reaches_the_disk_snapshot(self) -> None:
        """``..`` 行入站 ⇒ 盘上有快照：崩溃后恢复层找得到的那份踪迹。"""
        store = HeldBufferStore(self.snapshot_path)
        gateway = self.build_gateway(store)
        gateway.on_inbound(_message("第一段" + CONTINUE_SUFFIX, "m1"))
        self.assertEqual(store.read(), {CONVERSATION: "第一段"})
        # 进缓冲回执仍然送达（kind="buffered"，a2a 非终态档）。
        self.assertEqual(
            self.send_text.outgoing[-1],
            (CONVERSATION, BUFFERED_NOTICE, "buffered"),
        )

    def test_popping_the_buffer_clears_the_disk_snapshot(self) -> None:
        """投递后必须显式清档——否则下次启动会把已投递的内容重灌一遍。"""
        store = HeldBufferStore(self.snapshot_path)
        gateway = self.build_gateway(store)
        gateway.on_inbound(_message("第一段" + CONTINUE_SUFFIX, "m1"))
        merged = gateway._merger.ingest(CONVERSATION, "第二行")
        self.assertEqual(merged.kind, DELIVER)
        self.assertEqual(merged.text, "第一段\n第二行")
        self.assertEqual(store.read(), {})

    def _gateway_with_crashed_snapshot(self) -> tuple[InboundGateway, HeldBufferStore]:
        """预置一份「崩溃前」的快照档，再启动（构造 + recover）一个网关。"""
        store = HeldBufferStore(self.snapshot_path)
        store.write({CONVERSATION: "崩溃前的半行"})
        gateway = self.build_gateway(store)
        return gateway, store

    def test_recovery_pours_the_snapshot_back_and_says_so(self) -> None:
        gateway, _store = self._gateway_with_crashed_snapshot()
        gateway.recover_inbox()
        # 语义 = 重灌，⛔ 绝不立即投递：半句话不许当完整消息发给 agent。
        self.assertEqual(self.client.prompts, [])
        self.assertEqual(
            gateway._merger.held_text(CONVERSATION), "崩溃前的半行"
        )
        # 找回告知：kind="buffered"（与进缓冲回执同属非终态，a2a 穷举表零改动）。
        # ⛔ 回退 kind="text" 会让 a2a 对端在 agent 跑之前收到 task completed。
        self.assertEqual(
            self.send_text.outgoing,
            [(CONVERSATION, BUFFERED_RESTORED_NOTICE % "崩溃前的半行", "buffered")],
        )

    def test_a_recovered_line_joins_the_next_line_sent(self) -> None:
        """重灌的内容必须与下一行正确拼接——找回的不是死数据。"""
        gateway, store = self._gateway_with_crashed_snapshot()
        gateway.recover_inbox()
        merged = gateway._merger.ingest(CONVERSATION, "补上的下一行")
        self.assertEqual(merged.kind, DELIVER)
        self.assertEqual(merged.text, "崩溃前的半行\n补上的下一行")
        # 拼走之后照常清档。
        self.assertEqual(store.read(), {})

    def test_store_none_keeps_the_old_pure_memory_behavior(self) -> None:
        """``None`` = 纯内存旧行为：无档可恢复、无话可说、也不许落任何文件。"""
        gateway = self.build_gateway(None)
        gateway.recover_inbox()
        self.assertEqual(self.send_text.outgoing, [])
        self.assertEqual(self.client.prompts, [])
        self.assertFalse(os.path.exists(self.snapshot_path))
        # 入站缓冲功能本身照旧工作。
        gateway.on_inbound(_message("第一段" + CONTINUE_SUFFIX, "m1"))
        self.assertEqual(self.send_text.outgoing[-1][2], "buffered")
        self.assertEqual(
            gateway._merger.held_text(CONVERSATION), "第一段"
        )

    def test_a_snapshot_write_failure_only_warns(self) -> None:
        """快照写盘失败只许 WARNING、⛔ 不许把一次普通入站变成投递失败。"""
        gateway = self.build_gateway(_ExplodingHeldBufferStore())
        with self.assertLogs(
            "opencode_bridge.inbound_gateway", level="WARNING"
        ):
            gateway.on_inbound(_message("第一段" + CONTINUE_SUFFIX, "m1"))
        # 回执仍然送达、缓冲仍然在 —— 快照丢的是「崩溃保险」，不是这条消息。
        self.assertEqual(self.send_text.outgoing[-1][2], "buffered")
        self.assertEqual(
            gateway._merger.held_text(CONVERSATION), "第一段"
        )
        self.assertEqual(self.client.prompts, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

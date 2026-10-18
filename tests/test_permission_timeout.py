"""``PermissionTimeoutWatcher`` 的**独立**测试 —— 不构造 :class:`BridgeCore`。

锁住的是桥侧权限请求的本地超时语义（dsh-im-connect 借鉴面 D，用户拍板"到期按取消
（非拒绝）"）：

- 到点且请求仍 :data:`ANSWER_PENDING` ⇒ 账本标成 :data:`ANSWER_CANCELLED`，且
  ``notify`` 恰好回调一次；
- 到点但一条真回复抢先落地（已关闭）⇒ 账本**不被**覆盖、``notify`` **不**发假超时；
- ``cancel()`` / ``stop()`` 撤掉计时器 ⇒ 之后不发任何超时。

⚠️ 同步纪律照搬本仓库既有计时器测试（``tests/test_inbound_merge.py`` 的保险丝）：
一律用 :class:`threading.Event` 当会合点，**不用 ``time.sleep`` 做断言**；用极短的
``timeout_seconds``（如 0.05）走**真** ``threading.Timer``，不用假时钟。断言"它没再
醒"时，沿用既有"给它足够时间自己醒来确认不会动"的等待姿势。
"""

from __future__ import annotations

import threading
import unittest

from opencode_bridge.permission_ledger import (
    ANSWER_ALREADY_REPLIED,
    ANSWER_CANCELLED,
    ANSWER_PENDING,
    PermissionLedger,
)
from opencode_bridge.permission_timeout import (
    PERMISSION_TIMEOUT_MESSAGE,
    PermissionTimeoutWatcher,
)

SESSION = "ses_timeout01"
OTHER_SESSION = "ses_timeout02"

#: 一次计时器线程会合点的最大等待（死锁守卫：真卡住就响亮 fail，绝不静默）。
RENDEZVOUS_TIMEOUT_SECONDS = 5.0
#: 被测的极短超时（走真 Timer，不用假时钟）。
SHORT_TIMEOUT_SECONDS = 0.05


class TimeoutWatcherTestCase(unittest.TestCase):
    """每个用例一套全新的账本 + 看护 + 会合点。"""

    def setUp(self) -> None:
        self.ledger = PermissionLedger()
        self.notified: list[tuple[str, str, str]] = []
        self.notify_fired = threading.Event()
        self.watcher = PermissionTimeoutWatcher(
            timeout_seconds=SHORT_TIMEOUT_SECONDS,
            ledger=self.ledger,
            notify=self._record_notify,
        )

    def tearDown(self) -> None:
        self.watcher.stop()

    def _record_notify(self, session_id: str, request_id: str,
                       message: str) -> None:
        self.notified.append((session_id, request_id, message))
        self.notify_fired.set()

    # ------------------------------------------------------------------
    # 到点取消
    # ------------------------------------------------------------------
    def test_expiry_of_a_pending_request_cancels_it_and_notifies_exactly_once(self):
        self.ledger.record_asked(SESSION, "per_1")
        self.watcher.watch(SESSION, "per_1")

        self.assertTrue(
            self.notify_fired.wait(RENDEZVOUS_TIMEOUT_SECONDS),
            "计时器没有在到点后触发超时通知",
        )
        status = self.ledger.status_of(SESSION, "per_1")
        self.assertEqual(status.state, ANSWER_CANCELLED)
        self.assertTrue(status.already_closed)
        # 让计时器线程有机会醒来再看它会不会再发一次 —— 不该有第二次。
        threading.Event().wait(RENDEZVOUS_TIMEOUT_SECONDS)
        self.assertEqual(len(self.notified), 1)
        self.assertEqual(self.notified[0][0], SESSION)
        self.assertEqual(self.notified[0][1], "per_1")
        self.assertIn("per_1", self.notified[0][2])
        self.assertIn("已取消", self.notified[0][2])

    def test_the_timeout_message_does_not_claim_a_server_side_rejection(self):
        """措辞契约：只能说是本地取消，绝不能声称被拒绝/服务端已取消。"""
        self.assertNotIn("拒绝", PERMISSION_TIMEOUT_MESSAGE)
        self.assertNotIn("reject", PERMISSION_TIMEOUT_MESSAGE)

    # ------------------------------------------------------------------
    # 到点但已被真回复关闭 ⇒ 不覆盖、不通知
    # ------------------------------------------------------------------
    def test_expiry_after_an_answer_does_not_clobber_and_does_not_notify(self):
        self.ledger.record_asked(SESSION, "per_2")
        self.ledger.record_answered(SESSION, "per_2", "once")
        self.watcher.watch(SESSION, "per_2")

        # 给计时器足够时间到点，确认它不会发假超时、也不会把"已回复"抹成"已取消"。
        threading.Event().wait(SHORT_TIMEOUT_SECONDS * 6)
        status = self.ledger.status_of(SESSION, "per_2")
        self.assertEqual(status.state, ANSWER_ALREADY_REPLIED)
        self.assertEqual(status.decision, "once")
        self.assertEqual(self.notified, [])

    # ------------------------------------------------------------------
    # cancel()：真回复落地撤计时器
    # ------------------------------------------------------------------
    def test_cancel_before_expiry_prevents_notify_and_ledger_change(self):
        self.ledger.record_asked(SESSION, "per_3")
        self.watcher.watch(SESSION, "per_3")
        self.watcher.cancel(SESSION, "per_3")

        threading.Event().wait(SHORT_TIMEOUT_SECONDS * 6)
        status = self.ledger.status_of(SESSION, "per_3")
        self.assertEqual(status.state, ANSWER_PENDING)
        self.assertEqual(self.notified, [])

    def test_cancel_is_idempotent_and_ignores_unknown_keys(self):
        # 从不 watch 的 key 也 cancel —— 不该炸。
        self.watcher.cancel(SESSION, "per_never_watched")
        self.watcher.cancel(OTHER_SESSION, "per_never_watched")

    # ------------------------------------------------------------------
    # watch 在已关闭的请求上：不该造一个会误发的计时器
    # ------------------------------------------------------------------
    def test_watch_on_an_already_closed_request_does_not_fire_later(self):
        self.ledger.record_asked(SESSION, "per_4")
        self.ledger.record_answered(SESSION, "per_4", "always")
        self.watcher.watch(SESSION, "per_4")

        threading.Event().wait(SHORT_TIMEOUT_SECONDS * 6)
        # 已关闭的请求绝不被超时改写。
        self.assertEqual(
            self.ledger.status_of(SESSION, "per_4").state,
            ANSWER_ALREADY_REPLIED,
        )
        self.assertEqual(self.notified, [])

    def test_watch_rearms_an_existing_timer_instead_of_duplicating_it(self):
        """重排 = 旧计时器被撤，只有一个计时器存活 ⇒ 到点只发一次通知。"""
        self.ledger.record_asked(SESSION, "per_5")
        self.watcher.watch(SESSION, "per_5")
        self.watcher.watch(SESSION, "per_5")

        self.assertTrue(self.notify_fired.wait(RENDEZVOUS_TIMEOUT_SECONDS))
        threading.Event().wait(RENDEZVOUS_TIMEOUT_SECONDS)
        self.assertEqual(len(self.notified), 1)

    # ------------------------------------------------------------------
    # stop()：生命周期收尾，撤掉所有待处理计时器
    # ------------------------------------------------------------------
    def test_stop_cancels_all_pending_timers(self):
        self.ledger.record_asked(SESSION, "per_a")
        self.ledger.record_asked(OTHER_SESSION, "per_b")
        self.watcher.watch(SESSION, "per_a")
        self.watcher.watch(OTHER_SESSION, "per_b")

        self.watcher.stop()

        threading.Event().wait(SHORT_TIMEOUT_SECONDS * 6)
        self.assertEqual(self.notified, [])
        # 两个请求都仍是 pending（停掉后没人标它们取消）。
        self.assertEqual(self.ledger.status_of(SESSION, "per_a").state,
                         ANSWER_PENDING)
        self.assertEqual(self.ledger.status_of(OTHER_SESSION, "per_b").state,
                         ANSWER_PENDING)

    def test_stop_is_idempotent(self):
        self.watcher.stop()
        self.watcher.stop()

    # ------------------------------------------------------------------
    # timeout_seconds <= 0 = 关掉特性
    # ------------------------------------------------------------------
    def test_non_positive_timeout_disables_the_feature(self):
        disabled = PermissionTimeoutWatcher(
            timeout_seconds=0,
            ledger=self.ledger,
            notify=self._record_notify,
        )
        self.ledger.record_asked(SESSION, "per_off")
        disabled.watch(SESSION, "per_off")

        threading.Event().wait(SHORT_TIMEOUT_SECONDS * 6)
        self.assertEqual(self.notified, [])
        self.assertEqual(self.ledger.status_of(SESSION, "per_off").state,
                         ANSWER_PENDING)
        disabled.stop()

    # ------------------------------------------------------------------
    # record_cancelled 的守卫
    # ------------------------------------------------------------------
    def test_record_cancelled_does_not_reopen_an_already_closed_request(self):
        self.ledger.record_asked(SESSION, "per_6")
        self.ledger.record_answered(SESSION, "per_6", "once")

        # 计时器到点、但真回复已抢先落地 —— record_cancelled 必须让位。
        self.watcher._timeout_fired(SESSION, "per_6")

        status = self.ledger.status_of(SESSION, "per_6")
        self.assertEqual(status.state, ANSWER_ALREADY_REPLIED)
        self.assertEqual(status.decision, "once")
        self.assertEqual(self.notified, [])

    def test_record_cancelled_guard_holds_on_an_already_closed_request_directly(self):
        """直接咬合 :meth:`record_cancelled` 自己的"不重开已关闭"闸。

        ⚠️ 上面那个用例走 :meth:`_timeout_fired`，而它在调用 ``record_cancelled``
        **之前**已经用 ``status.state == ANSWER_PENDING`` 挡了一次 —— 那条路径到
        **不了** :meth:`record_cancelled` 内部的闸。真正的竞态是：``_timeout_fired``
        读到 ``ANSWER_PENDING`` 之后、``record_cancelled`` 落盘之前，一条真回复
        （走账本自己的锁）抢先落地 —— 此时只有 :meth:`record_cancelled` 内那道
        ``already_closed`` 闸能挡住晚到的计时器把真回答抹成"已取消"。这里**直接**
        调 :meth:`record_cancelled` 复现那一半。
        """
        self.ledger.record_asked(SESSION, "per_7")
        self.ledger.record_answered(SESSION, "per_7", "once")

        # 模拟"读到 pending 后、落盘前"真回复抢先落地：直接对已关闭的请求取消。
        self.ledger.record_cancelled(SESSION, "per_7")

        status = self.ledger.status_of(SESSION, "per_7")
        self.assertEqual(status.state, ANSWER_ALREADY_REPLIED)
        self.assertEqual(status.decision, "once")


if __name__ == "__main__":
    unittest.main()
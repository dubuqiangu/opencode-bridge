"""``PermissionLedger`` 的**独立**测试 —— 不构造 :class:`BridgeCore`。

这个模块是纯内存状态：不做 I/O、不发消息、不认识适配器，所以它能单独测。
本文件锁住的是**账本自己**的四条语义（哪些状态算"已经关闭"、重播会不会把它重新
打开、失败有没有被记成已答过、容量挤掉之后往哪边失败）。

⚠️ 三处**消费方**的接线契约不在这里，而在各自真实路径上：
``tests/test_core.py``（``/approve`` / ``/deny``）、``tests/test_inbound_gateway.py``
（``perm:`` 按钮）与 ``tests/test_event_stream.py``（``permission.asked`` /
``permission.replied`` 两个事件）。这里若只测账本，一份"接线漏了"的改动照样全绿。
"""

from __future__ import annotations

import threading
import unittest

from opencode_bridge.permission_ledger import (
    ANSWER_ALREADY_REPLIED,
    ANSWER_PENDING,
    ANSWER_RESOLVED_UPSTREAM,
    ANSWER_UNSEEN,
    REPEATED_ANSWER_ACK,
    PermissionLedger,
    repeated_answer_notice,
)

SESSION = "ses_ledger01"
OTHER_SESSION = "ses_ledger02"


class LedgerStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = PermissionLedger()

    def test_a_request_this_process_never_saw_is_unseen_and_not_closed(self):
        status = self.ledger.status_of(SESSION, "per_never_asked")

        self.assertEqual(status.state, ANSWER_UNSEEN)
        self.assertFalse(status.already_closed)

    def test_a_surfaced_request_is_pending_until_this_bridge_answers_it(self):
        self.ledger.record_asked(SESSION, "per_1")

        status = self.ledger.status_of(SESSION, "per_1")
        self.assertEqual(status.state, ANSWER_PENDING)
        self.assertFalse(status.already_closed)

    def test_one_answer_closes_the_request_and_remembers_the_decision(self):
        self.ledger.record_asked(SESSION, "per_1")
        self.ledger.record_answered(SESSION, "per_1", "once")

        status = self.ledger.status_of(SESSION, "per_1")
        self.assertEqual(status.state, ANSWER_ALREADY_REPLIED)
        self.assertEqual(status.decision, "once")
        self.assertTrue(status.already_closed)

    def test_permission_replied_closes_a_request_this_process_never_asked(self):
        """重启前的请求正是靠这个信号被认成"已经结束" —— 所以不能要求先有记录。"""
        self.ledger.note_replied(SESSION, "per_before_restart")

        status = self.ledger.status_of(SESSION, "per_before_restart")
        self.assertEqual(status.state, ANSWER_RESOLVED_UPSTREAM)
        self.assertTrue(status.already_closed)

    def test_a_request_id_is_scoped_to_its_session(self):
        """``/api/event`` 是全服务器广播：同一个 id 在别的会话是另一件事。"""
        self.ledger.record_answered(SESSION, "per_shared_id", "once")

        self.assertTrue(self.ledger.status_of(SESSION, "per_shared_id").already_closed)
        self.assertFalse(
            self.ledger.status_of(OTHER_SESSION, "per_shared_id").already_closed
        )

    def test_an_empty_request_id_is_never_tracked(self):
        """没有 id 的那一帧不能进账本：否则所有"没有 id"的请求会挤同一个槽位。"""
        self.ledger.record_asked(SESSION, "")
        self.ledger.record_answered(SESSION, "", "always")
        self.ledger.note_replied(SESSION, "")

        self.assertEqual(len(self.ledger), 0)
        self.assertEqual(self.ledger.status_of(SESSION, "").state, ANSWER_UNSEEN)

    def test_record_answered_works_without_a_prior_permission_asked(self):
        """账本自己**不**设第二道门槛：调用方本来就先问过 :meth:`status_of`。

        这一条把"本模块只提供事实、不替调用方做判断"钉住。
        """
        self.ledger.record_answered(SESSION, "per_typed_by_hand", "always")

        self.assertTrue(
            self.ledger.status_of(SESSION, "per_typed_by_hand").already_closed
        )


class LedgerReopeningTests(unittest.TestCase):
    """已经关闭的请求**不许**被重新打开 —— 否则重复回答会重新变成合法。"""

    def setUp(self) -> None:
        self.ledger = PermissionLedger()

    def test_a_rebroadcast_permission_asked_does_not_reopen_an_answered_request(self):
        self.ledger.record_asked(SESSION, "per_1")
        self.ledger.record_answered(SESSION, "per_1", "once")
        self.ledger.record_asked(SESSION, "per_1")

        status = self.ledger.status_of(SESSION, "per_1")
        self.assertEqual(status.state, ANSWER_ALREADY_REPLIED)
        self.assertEqual(status.decision, "once")

    def test_a_rebroadcast_permission_asked_does_not_reopen_a_resolved_request(self):
        self.ledger.note_replied(SESSION, "per_1")
        self.ledger.record_asked(SESSION, "per_1")

        self.assertEqual(
            self.ledger.status_of(SESSION, "per_1").state,
            ANSWER_RESOLVED_UPSTREAM,
        )

    def test_permission_replied_keeps_the_decision_this_bridge_already_sent(self):
        """先本地答过、再收到 ``permission.replied``：仍然算"我答过"。

        那样 :func:`repeated_answer_notice` 才说得清上次答的是什么。
        """
        self.ledger.record_answered(SESSION, "per_1", "always")
        self.ledger.note_replied(SESSION, "per_1")

        status = self.ledger.status_of(SESSION, "per_1")
        self.assertEqual(status.state, ANSWER_ALREADY_REPLIED)
        self.assertEqual(status.decision, "always")


class LedgerCapacityTests(unittest.TestCase):
    def test_the_ledger_forgets_the_oldest_request_once_it_is_full(self):
        ledger = PermissionLedger(capacity=2)
        ledger.record_answered(SESSION, "per_oldest", "once")
        ledger.record_asked(SESSION, "per_middle")
        ledger.record_asked(SESSION, "per_newest")

        self.assertEqual(len(ledger), 2)
        self.assertEqual(ledger.status_of(SESSION, "per_oldest").state, ANSWER_UNSEEN)
        self.assertEqual(
            ledger.status_of(SESSION, "per_middle").state, ANSWER_PENDING
        )

    def test_a_forgotten_request_fails_open_towards_the_server(self):
        """挤掉一条 = 退回"没见过" = 照旧转给服务端。

        这是有意的方向：多问一次（服务端会拒）远好过凭空造出"已答过"，
        后者会把一个还活着的请求静默锁死。
        """
        ledger = PermissionLedger(capacity=1)
        ledger.record_answered(SESSION, "per_oldest", "once")
        ledger.record_answered(SESSION, "per_newest", "once")

        self.assertFalse(ledger.status_of(SESSION, "per_oldest").already_closed)
        self.assertTrue(ledger.status_of(SESSION, "per_newest").already_closed)

    def test_a_capacity_below_one_still_keeps_the_book_usable(self):
        ledger = PermissionLedger(capacity=0)
        ledger.record_answered(SESSION, "per_1", "once")

        self.assertEqual(len(ledger), 1)


class LedgerConcurrencyTests(unittest.TestCase):
    def test_concurrent_writers_never_lose_or_double_count_a_request(self):
        """事件流线程与入站线程会同时写；一次回答必须仍然只被认成一次。"""
        ledger = PermissionLedger()
        start = threading.Barrier(4)
        errors: list[BaseException] = []

        def record_answers(decision: str) -> None:
            try:
                start.wait(timeout=5)
                for _ in range(200):
                    ledger.record_asked(SESSION, "per_race")
                    ledger.record_answered(SESSION, "per_race", decision)
            except BaseException as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [
            threading.Thread(target=record_answers, args=(decision,))
            for decision in ("once", "always", "reject", "once")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(errors, [])
        self.assertEqual(len(ledger), 1)
        self.assertTrue(ledger.status_of(SESSION, "per_race").already_closed)


class RepeatedAnswerNoticeTests(unittest.TestCase):
    def test_the_notice_names_the_request_and_the_decision_already_sent(self):
        ledger = PermissionLedger()
        ledger.record_asked(SESSION, "per_7")
        ledger.record_answered(SESSION, "per_7", "once")

        notice = repeated_answer_notice(ledger.status_of(SESSION, "per_7"))
        self.assertIn("per_7", notice)
        self.assertIn("once", notice)
        self.assertIn("忽略", notice)

    def test_the_notice_says_ended_rather_than_answered_for_an_upstream_reply(self):
        ledger = PermissionLedger()
        ledger.note_replied(SESSION, "per_8")

        notice = repeated_answer_notice(ledger.status_of(SESSION, "per_8"))
        self.assertIn("per_8", notice)
        self.assertIn("已经结束", notice)

    def test_the_button_ack_is_short_and_distinct_from_a_failure(self):
        """ack 走平台 toast，所以必须短；失败那条是 ``失败``，两者不能混。"""
        self.assertNotEqual(REPEATED_ANSWER_ACK, "失败")
        self.assertTrue(REPEATED_ANSWER_ACK)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

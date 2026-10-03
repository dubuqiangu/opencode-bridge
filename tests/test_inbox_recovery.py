"""``inbox_recovery.py`` 测试：崩溃之后，哪些行能重放、哪些只能告警。

这一层是**纯策略**，所以大部分用例拿一个内存假收件箱 :class:`FakeInbox` 就能跑完
—— 零磁盘、零 SQLite、零 sleep。

覆盖的六条语义（每条都对应一次真实的事故）：

1. ``pending`` **一定**被重放，且**不**告警 —— 从没试过，重复不可能发生；
2. ``attempting`` **绝不**被重放，只按会话合并告警（用户 2026-10-03 拍板：
   下游是有副作用的 coding agent，重复执行副作用未必比丢一条消息轻）；
3. ``failed`` 尊重退避期限 —— 期限没到就是不放（这就是"退避是期限不是 sleep"）；
4. 预算耗尽转 ``abandoned`` 且**不再重试**（这一条用**真**收件箱跑，因为"耗尽"
   是 ``mark_failed`` 的算术，用假收件箱断言等于在断言假货）；
5. ``dispatch`` / ``notify`` / 收件箱自己抛异常时，:func:`recover_pending`
   **绝不**把异常抛出去 —— 恢复失败不该让桥起不来；
6. ``prompt.text`` 原样交给 ``dispatch`` —— 告警文字绝不进 agent 的上下文。

⚠️ 用真收件箱的用例同样**先关连接再删临时目录**（Windows 的 ``WinError 32``）。
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from opencode_bridge.inbox import DeliveryState, InboundInbox, QueuedPrompt
from opencode_bridge.inbox_recovery import MAX_ATTEMPTS, RecoveryOutcome, recover_pending

_REPOSITORY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: 仓库内的临时目录：即便 TEMP/TMP 指向机器别处，测试也不可能写到仓库之外。
_REPOSITORY_TEMP = os.path.join(_REPOSITORY_ROOT, ".tmp")

_ALWAYS_UNREACHABLE = "opencode 不可达"


def make_prompt(
    delivery_id: str,
    *,
    conversation_id: str = "telegram:100200",
    platform: str = "telegram",
    text: str = "看一下 README",
) -> QueuedPrompt:
    """构造一条入站提示词。默认值指向一个假会话，不含任何真实身份信息。"""
    return QueuedPrompt(
        delivery_id=delivery_id,
        conversation_id=conversation_id,
        platform=platform,
        message_id=None,
        text=text,
    )


def states_of(inbox: FakeInbox, delivery_id: str) -> list[str]:
    """某一行的完整状态变迁序列（排除摆状态时用的那次 ``add``）。"""
    return [
        state
        for changed_delivery_id, state in inbox.settled_changes
        if changed_delivery_id == delivery_id
    ]


class FakeInbox:
    """内存假收件箱：实现 :class:`~opencode_bridge.inbox.InboundInbox` 的公开契约。

    只做"把行从一个状态挪到另一个状态"，**不含**任何重试预算算术 —— 那套算术在真
    收件箱里，需要真收件箱来测。

    行有两个来源：用例用 :meth:`add` 直接摆好状态（记在 ``seeded_changes``），
    以及恢复层自己写下来的状态（记在 ``settled_changes``）。两者分开，用例才能
    断言"恢复层只写了这几次、而且顺序是这样"。
    """

    def __init__(self) -> None:
        self._prompts: dict[str, QueuedPrompt] = {}
        self._current_state: dict[str, str] = {}
        #: 用例摆状态时留下的记录。
        self.seeded_changes: list[tuple[str, str]] = []
        #: 恢复层写下的状态变迁，用例拿它断言"先 attempting 再 delivered"。
        self.settled_changes: list[tuple[str, str]] = []
        self.last_errors: dict[str, str] = {}
        self.closed = False

    def add(self, prompt: QueuedPrompt, state: str) -> None:
        """摆一行已存在于收件箱、处于 ``state`` 的提示词。"""
        self._prompts[prompt.delivery_id] = prompt
        self._current_state[prompt.delivery_id] = state
        self.seeded_changes.append((prompt.delivery_id, state))

    def _move(self, delivery_id: str, state: str) -> None:
        if delivery_id in self._prompts:
            self._current_state[delivery_id] = state
            self.settled_changes.append((delivery_id, state))

    def _prompts_now_in(self, *states: str) -> list[QueuedPrompt]:
        return [
            prompt
            for delivery_id, prompt in self._prompts.items()
            if self._current_state.get(delivery_id) in states
        ]

    # ---- 真实契约 ----
    def record(self, prompt: QueuedPrompt) -> bool:
        if prompt.delivery_id in self._prompts:
            return False
        self.add(prompt, DeliveryState.PENDING)
        return True

    def mark_attempting(self, delivery_id: str) -> None:
        self._move(delivery_id, DeliveryState.ATTEMPTING)

    def mark_delivered(self, delivery_id: str) -> None:
        self._move(delivery_id, DeliveryState.DELIVERED)

    def mark_failed(self, delivery_id: str, error: str) -> None:
        self.last_errors[delivery_id] = error
        self._move(delivery_id, DeliveryState.FAILED)

    def mark_abandoned(self, delivery_id: str, error: str) -> None:
        self.last_errors[delivery_id] = error
        self._move(delivery_id, DeliveryState.ABANDONED)

    def pending_prompts(self) -> list[QueuedPrompt]:
        return self._prompts_now_in(DeliveryState.PENDING)

    def uncertain_prompts(self) -> list[QueuedPrompt]:
        return self._prompts_now_in(DeliveryState.ATTEMPTING)

    def due_failed_prompts(self, now: float) -> list[QueuedPrompt]:
        return self._prompts_now_in(DeliveryState.FAILED)

    def abandoned_prompts(self) -> list[QueuedPrompt]:
        return self._prompts_now_in(DeliveryState.ABANDONED)

    def close(self) -> None:
        self.closed = True


class RecordingDispatcher:
    """记录被真正投递出去的提示词；``error`` 不为空时每次都抛。"""

    def __init__(self, error: Exception | None = None) -> None:
        self.dispatched: list[QueuedPrompt] = []
        self.error = error

    def __call__(self, prompt: QueuedPrompt) -> None:
        self.dispatched.append(prompt)
        if self.error is not None:
            raise self.error


class RecordingNotifier:
    """记录 ``(conversation_id, 告警文字)``；``error`` 不为空时每次都抛。"""

    def __init__(self, error: Exception | None = None) -> None:
        self.alerts: list[tuple[str, str]] = []
        self.error = error

    def __call__(self, conversation_id: str, text: str) -> None:
        self.alerts.append((conversation_id, text))
        if self.error is not None:
            raise self.error


class TestPendingIsAlwaysReplayed(unittest.TestCase):
    """``pending`` 从没试过 —— 重放不可能造成重复，所以放肆重放且不打扰用户。"""

    def test_pending_prompts_are_replayed_with_a_single_trying_succeeded_pair(self):
        inbox = FakeInbox()
        inbox.add(make_prompt("telegram:100200:1"), DeliveryState.PENDING)
        inbox.add(make_prompt("telegram:100200:2"), DeliveryState.PENDING)
        dispatch = RecordingDispatcher()

        outcome = recover_pending(inbox, dispatch=dispatch, notify=RecordingNotifier())

        self.assertEqual([prompt.delivery_id for prompt in dispatch.dispatched],
                         ["telegram:100200:1", "telegram:100200:2"])
        self.assertEqual(outcome.replayed, dispatch.dispatched)
        self.assertEqual(
            inbox.settled_changes,
            [
                ("telegram:100200:1", DeliveryState.ATTEMPTING),
                ("telegram:100200:1", DeliveryState.DELIVERED),
                ("telegram:100200:2", DeliveryState.ATTEMPTING),
                ("telegram:100200:2", DeliveryState.DELIVERED),
            ],
            msg="每次投递都必须是 attempting→delivered 一对，且 delivered 写在成功之后",
        )

    def test_replaying_a_pending_prompt_never_raises_an_alert(self):
        """不需要告警：重复不可能发生，提醒用户只会制造噪音。"""
        inbox = FakeInbox()
        inbox.add(make_prompt("telegram:100200:1"), DeliveryState.PENDING)
        notify = RecordingNotifier()

        recover_pending(inbox, dispatch=RecordingDispatcher(), notify=notify)

        self.assertEqual(notify.alerts, [])

    def test_the_prompt_text_reaches_dispatch_verbatim(self):
        """告警文字绝不进 agent 上下文 —— 那会改变 agent 的行为。

        这条把"恢复期不许改写 ``prompt.text``"钉成断言。
        """
        inbox = FakeInbox()
        original = make_prompt("matrix:!room:5:t1", conversation_id="matrix:!room:5",
                               platform="matrix", text="请把 README 的第 3 段改短")
        inbox.add(original, DeliveryState.PENDING)
        dispatch = RecordingDispatcher()

        recover_pending(inbox, dispatch=dispatch, notify=RecordingNotifier())

        self.assertEqual(dispatch.dispatched[0].text, "请把 README 的第 3 段改短")


class TestUncertainIsNeverReplayed(unittest.TestCase):
    """结果不可知的一行：只告警。**这是用户拍板的语义，不是可以顺手改进的地方。**"""

    def test_uncertain_prompts_are_not_dispatched(self):
        inbox = FakeInbox()
        inbox.add(make_prompt("telegram:100200:3"), DeliveryState.ATTEMPTING)
        dispatch = RecordingDispatcher()

        outcome = recover_pending(inbox, dispatch=dispatch, notify=RecordingNotifier())

        self.assertEqual(dispatch.dispatched, [])
        self.assertEqual(outcome.replayed, [])
        self.assertEqual([prompt.delivery_id for prompt in outcome.uncertain],
                         ["telegram:100200:3"])

    def test_one_alert_per_affected_conversation_not_per_message(self):
        """3 条未知消息发 3 条几乎一样的告警，会把真正的通知淹掉。"""
        inbox = FakeInbox()
        for index in range(3):
            inbox.add(make_prompt(f"telegram:100200:u{index}"), DeliveryState.ATTEMPTING)
        notify = RecordingNotifier()

        recover_pending(inbox, dispatch=RecordingDispatcher(), notify=notify)

        self.assertEqual(len(notify.alerts), 1)
        conversation_id, text = notify.alerts[0]
        self.assertEqual(conversation_id, "telegram:100200")
        self.assertIn("3", text)
        self.assertIn("状态未知", text)

    def test_each_affected_conversation_gets_its_own_alert(self):
        inbox = FakeInbox()
        inbox.add(make_prompt("telegram:100200:u1"), DeliveryState.ATTEMPTING)
        inbox.add(make_prompt("telegram:100200:u2"), DeliveryState.ATTEMPTING)
        inbox.add(make_prompt("slack:C0ABCDEF:u3", conversation_id="slack:C0ABCDEF",
                              platform="slack"), DeliveryState.ATTEMPTING)
        notify = RecordingNotifier()

        recover_pending(inbox, dispatch=RecordingDispatcher(), notify=notify)

        self.assertEqual([conversation_id for conversation_id, _text in notify.alerts],
                         ["slack:C0ABCDEF", "telegram:100200"])

    def test_an_alerted_uncertain_row_is_left_untouched_for_the_next_boot(self):
        """告警之后**不许**把它挪走 —— 下次启动还要能再告警一次（用户可能还没看见）。"""
        inbox = FakeInbox()
        inbox.add(make_prompt("telegram:100200:4"), DeliveryState.ATTEMPTING)

        recover_pending(inbox, dispatch=RecordingDispatcher(), notify=RecordingNotifier())

        self.assertEqual(inbox.settled_changes, [])


class TestFailedRowsRespectTheBackoffDeadline(unittest.TestCase):
    """明确失败过的行按阶梯重放，但**期限没到就是不放**。"""

    def setUp(self) -> None:
        self.database_path = _temporary_database_path(self)
        self.inbox = InboundInbox(self.database_path)
        self.addCleanup(self.inbox.close)

    def test_a_failed_row_is_replayed_once_its_deadline_arrives(self):
        self.inbox.record(make_prompt("telegram:100200:5"))
        self.inbox.mark_failed("telegram:100200:5", "boom")
        expire_backoff_deadlines(self.database_path)  # 期限到了
        dispatch = RecordingDispatcher()

        outcome = recover_pending(self.inbox, dispatch=dispatch, notify=RecordingNotifier())

        self.assertEqual([prompt.delivery_id for prompt in dispatch.dispatched],
                         ["telegram:100200:5"])
        self.assertEqual(outcome.replayed, dispatch.dispatched)
        self.assertEqual(_read_state(self.database_path), DeliveryState.DELIVERED)

    def test_the_backoff_deadline_is_what_decides_not_a_sleep(self):
        """真收件箱：期限写在盘上，没到期就问不出来 —— 不靠进程内计时器。

        两次恢复紧挨着跑，中间**没有**任何等待：第一次因为期限未到而什么都不发，
        第二次因为期限被抹平而立刻发。这正是"退避是期限、不是 sleep"的形状。
        """
        self.inbox.record(make_prompt("telegram:100200:6"))
        self.inbox.mark_failed("telegram:100200:6", "boom")
        self.assertGreater(_read_column(self.database_path, "not_before"), 0.0)
        dispatch = RecordingDispatcher()

        recover_pending(self.inbox, dispatch=dispatch, notify=RecordingNotifier())
        self.assertEqual(dispatch.dispatched, [], "期限未到就放行 = 没有退避")

        expire_backoff_deadlines(self.database_path)
        recover_pending(self.inbox, dispatch=dispatch, notify=RecordingNotifier())
        self.assertEqual([prompt.delivery_id for prompt in dispatch.dispatched],
                         ["telegram:100200:6"])

    def test_a_replay_that_fails_again_is_not_counted_as_replayed(self):
        inbox = FakeInbox()
        inbox.add(make_prompt("telegram:100200:7"), DeliveryState.FAILED)
        dispatch = RecordingDispatcher(error=RuntimeError("prompt 超时"))

        outcome = recover_pending(inbox, dispatch=dispatch, notify=RecordingNotifier())

        self.assertEqual(outcome.replayed, [])
        self.assertEqual([prompt.delivery_id for prompt in dispatch.dispatched],
                         ["telegram:100200:7"])
        self.assertEqual(states_of(inbox, "telegram:100200:7"),
                         [DeliveryState.ATTEMPTING, DeliveryState.FAILED])
        self.assertIn("prompt 超时", inbox.last_errors["telegram:100200:7"])


class TestRetryBudgetIsExhaustedAgainstARealInbox(unittest.TestCase):
    """预算耗尽 → ``abandoned`` → **不再重试**。

    这里用**真**收件箱：重试预算是 ``mark_failed`` 的算术，用假收件箱断言等于在断言
    假货的算术。
    """

    def setUp(self) -> None:
        self.database_path = _temporary_database_path(self)
        self.inbox = InboundInbox(self.database_path)
        self.addCleanup(self.inbox.close)
        self.inbox.record(make_prompt("telegram:100200:8"))

    def _recover_with_failing_dispatch(self, notify=None) -> RecoveryOutcome:
        """跑一轮恢复（投递必失败），并把退避期限抹平以备下一轮。"""
        outcome = recover_pending(
            self.inbox,
            dispatch=RecordingDispatcher(error=RuntimeError(_ALWAYS_UNREACHABLE)),
            notify=notify or RecordingNotifier(),
        )
        expire_backoff_deadlines(self.database_path)
        return outcome

    def test_the_last_budgeted_attempt_is_spent_and_then_the_prompt_is_abandoned(self):
        """**绝不**花掉最后一次预算重试：第 ``MAX_ATTEMPTS`` 次失败直接转终态。"""
        for round_number in range(1, MAX_ATTEMPTS):
            outcome = self._recover_with_failing_dispatch()
            self.assertEqual(outcome.abandoned, [],
                             f"第 {round_number} 次失败时预算还没耗尽，不该出现终态行")

        outcome = self._recover_with_failing_dispatch()

        self.assertEqual(_read_state(self.database_path), DeliveryState.ABANDONED)
        self.assertEqual([prompt.delivery_id for prompt in outcome.abandoned],
                         ["telegram:100200:8"])

    def test_an_abandoned_prompt_is_never_dispatched_again(self):
        """转终态之后连一次都不再试 —— 哪怕这一次投递会成功。"""
        for _round_number in range(MAX_ATTEMPTS - 1):
            self._recover_with_failing_dispatch()
        self._recover_with_failing_dispatch()

        dispatch = RecordingDispatcher()
        outcome = recover_pending(self.inbox, dispatch=dispatch, notify=RecordingNotifier())

        self.assertEqual(dispatch.dispatched, [])
        self.assertEqual(outcome.replayed, [])
        self.assertEqual([prompt.delivery_id for prompt in outcome.abandoned],
                         ["telegram:100200:8"])

    def test_becoming_abandoned_raises_exactly_one_alert(self):
        notifier = RecordingNotifier()
        for _round_number in range(MAX_ATTEMPTS - 1):
            self._recover_with_failing_dispatch(notifier)
        self.assertEqual(notifier.alerts, [], "还没耗尽预算时不该打扰用户")

        self._recover_with_failing_dispatch(notifier)

        self.assertEqual(len(notifier.alerts), 1)
        self.assertEqual(notifier.alerts[0][0], "telegram:100200")
        self.assertIn("停止自动重试", notifier.alerts[0][1])

    def test_a_pending_prompt_replayed_successfully_ends_up_delivered(self):
        """正向闭环：重放成功 → 盘上转 delivered、收件箱不再报它。"""
        dispatch = RecordingDispatcher()

        outcome = recover_pending(self.inbox, dispatch=dispatch, notify=RecordingNotifier())

        self.assertEqual([prompt.delivery_id for prompt in outcome.replayed],
                         ["telegram:100200:8"])
        self.assertEqual(_read_state(self.database_path), DeliveryState.DELIVERED)
        self.assertEqual(self.inbox.pending_prompts(), [])
        self.assertEqual(self.inbox.uncertain_prompts(), [])


class TestRecoveryNeverRaises(unittest.TestCase):
    """恢复失败**绝不能**让桥起不来 —— 这是它被允许存在的唯一前提。"""

    def test_a_raising_dispatch_is_swallowed_and_recorded_as_a_failure(self):
        inbox = FakeInbox()
        inbox.add(make_prompt("telegram:100200:9"), DeliveryState.PENDING)

        outcome = recover_pending(
            inbox,
            dispatch=RecordingDispatcher(error=OSError("连接被重置")),
            notify=RecordingNotifier(),
        )

        self.assertEqual(outcome.replayed, [])
        self.assertIn("OSError", inbox.last_errors["telegram:100200:9"])

    def test_a_raising_notify_does_not_stop_the_replay(self):
        inbox = FakeInbox()
        inbox.add(make_prompt("telegram:100200:10"), DeliveryState.PENDING)
        inbox.add(make_prompt("telegram:100200:11"), DeliveryState.ATTEMPTING)
        dispatch = RecordingDispatcher()

        outcome = recover_pending(
            inbox, dispatch=dispatch, notify=RecordingNotifier(error=OSError("平台限流"))
        )

        self.assertEqual([prompt.delivery_id for prompt in outcome.replayed],
                         ["telegram:100200:10"])
        self.assertEqual([prompt.delivery_id for prompt in outcome.uncertain],
                         ["telegram:100200:11"])

    def test_a_raising_inbox_read_yields_an_empty_outcome_instead_of_an_exception(self):
        class ExplodingInbox(FakeInbox):
            def pending_prompts(self):
                raise sqlite3.OperationalError("database is locked")

        outcome = recover_pending(
            ExplodingInbox(), dispatch=RecordingDispatcher(), notify=RecordingNotifier()
        )

        self.assertEqual(outcome.replayed, [])
        self.assertEqual(outcome.uncertain, [])
        self.assertEqual(outcome.abandoned, [])

    def test_an_empty_inbox_reports_nothing_and_stays_silent(self):
        notify = RecordingNotifier()

        outcome = recover_pending(FakeInbox(), dispatch=RecordingDispatcher(), notify=notify)

        self.assertEqual((outcome.replayed, outcome.uncertain, outcome.abandoned), ([], [], []))
        self.assertEqual(notify.alerts, [])


# ----------------------------------------------------------------------
# 真收件箱的辅助工具
# ----------------------------------------------------------------------
def _temporary_database_path(test_case: unittest.TestCase) -> str:
    """在**仓库内**的临时目录里备一个数据库路径。

    先注册目录清理、后注册收件箱 ``close()``（LIFO ⇒ 先关连接再删目录）——
    Windows 上还开着的 SQLite 句柄会让 ``rmtree`` 报 ``WinError 32``。
    """
    os.makedirs(_REPOSITORY_TEMP, exist_ok=True)
    temporary_directory = tempfile.TemporaryDirectory(dir=_REPOSITORY_TEMP)
    test_case.addCleanup(temporary_directory.cleanup)
    return os.path.join(temporary_directory.name, "inbox.sqlite3")


def expire_backoff_deadlines(database_path: str) -> int:
    """把所有 ``failed`` 行的退避期限抹成过去 —— 等价于"时间过去了"，但不用真等 120 秒。"""
    connection = sqlite3.connect(database_path)
    try:
        cursor = connection.execute(
            "UPDATE inbox SET not_before = 0 WHERE state = ?", (DeliveryState.FAILED,)
        )
        connection.commit()
        return cursor.rowcount
    finally:
        connection.close()


def _read_column(database_path: str, column: str) -> float:
    connection = sqlite3.connect(database_path)
    try:
        return connection.execute(f"SELECT {column} FROM inbox").fetchone()[0]
    finally:
        connection.close()


def _read_state(database_path: str) -> str:
    return _read_column(database_path, "state")
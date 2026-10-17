"""``delivery_retry.py`` 的守门：``/delivery retry`` 给用户看的那一面。

本文件钉住六组语义：

1. **候选集合** —— 本会话里只有 ``pending`` 与**已到期**的 ``failed`` 会被列出 /
   被补投（``attempting`` / ``outcome_unknown`` / 未到期的 ``failed`` / ``abandoned`` /
   ``delivered`` 一律不进集合）。两臂**只差时钟**，用来证明「到期」这个条件是活的。
2. **列表文案** —— 空表 / 编号从 1 / 空白折叠成一行 / 超长截断到
   :data:`~opencode_bridge.delivery_retry.PREVIEW_LIMIT` / 跨会话隔离。
3. **用法错误** —— 未知动词、token 过多、``0`` / 负数 / 非 ASCII 数字一律
   ``kind="error"`` 且逐字回用法。
4. **单条补投的四档回复** —— ``delivered`` / 明确失败 / ``outcome_unknown`` /
   ``not_attempted``，外加**预算耗尽**那一支（同属明确失败，但文案不同）。
5. **收件箱未启用** —— 列表与补投都回「收件箱未启用」，⛔ 不是报错、零投递。
6. **形状守门 + 对照臂** —— ``delivery_retry.py`` 里一条收件箱状态都不许自己写，
   补投必须经 :func:`~opencode_bridge.inbox_recovery.replay_one` 走；且同一匹配器
   在 ``inbox_recovery.py`` 上必须**真命中**（否则守门只是空转，§9）。

⚠️ 第 6 组存在的理由：把 ``mark_failed`` 从恢复层抄一份进 ``delivery_retry``
**不会让任何一条行为用例变红** —— 判定照样通过，只是多了一处实现 ⇒ 只有形状守门能抓。
"""

from __future__ import annotations

import ast
import io
import os
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from opencode_bridge.delivery_retry import (
    PREVIEW_LIMIT,
    DeliveryReply,
    DeliveryRetryCommand,
)
from opencode_bridge.inbox import InboundInbox, QueuedPrompt, RecordOutcome
from opencode_bridge.inbox_recovery import mark_prompt_never_sent
from opencode_bridge.inbox_retry_budget import MAX_ATTEMPTS
from opencode_bridge.inbox_states import DeliveryState

_REPOSITORY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: 仓库内的临时目录：即便 TEMP/TMP 指向机器别处，测试也不可能写到仓库之外。
_REPOSITORY_TEMP = os.path.join(_REPOSITORY_ROOT, ".tmp")

#: 本文件用的两个假会话 —— 一个是被补投的那个，一个是**对照**（它那条必须毫发无伤）。
_SESSION = "telegram:100200"
_OTHER_SESSION = "telegram:999999"

#: 收件箱表里允许被直读的那几列。⛔ 不许把它放开成任意字符串：列名会被拼进 SQL。
_READABLE_COLUMNS = frozenset({"state", "attempts"})


class _ServerRefusal(Exception):
    """服务端**明确**给了答复（``status`` 非 None）⇒ 明确失败，agent 没跑过。

    ⚠️ 下面量**明确失败**与**重试预算**的用例必须带上 ``status``：恢复层判「这一行
    还能不能重放」靠两条信息 —— 「服务端给了什么答复」与「prompt 有没有真的交出去」
    —— 而**裸异常两条都没有** ⇒ 它落 ``outcome_unknown``（只告警、绝不重放），
    预算一级都不动 ⇒ 那些用例会量到**另一件事**上（§7.1：「测了 A、没测 B」）。
    """

    def __init__(self, message: str, status: int = 503) -> None:
        super().__init__(message)
        self.status = status


#: ``DeliveryState`` 的全集 —— **从类上读出来的**，⛔ 不是手维护的清单。
_EVERY_DELIVERY_STATE = tuple(
    value
    for name, value in vars(DeliveryState).items()
    if not name.startswith("_") and isinstance(value, str)
)

#: 每一档状态在盘上的**摆法**（键就是状态字面量）。⛔ 生产侧加了新状态而忘了往这里
#: 加一条时，下面那条前提守卫会红 —— 它是整个候选集合守门的**前提**。
_MOVE_INTO_THE_INBOX = {
    DeliveryState.PENDING: lambda inbox, delivery_id: None,
    DeliveryState.ATTEMPTING: lambda inbox, delivery_id: inbox.mark_attempting(
        delivery_id
    ),
    DeliveryState.OUTCOME_UNKNOWN: lambda inbox, delivery_id: inbox.mark_outcome_unknown(
        delivery_id, "URLError: 连接被拒"
    ),
    DeliveryState.DELIVERED: lambda inbox, delivery_id: inbox.mark_delivered(
        delivery_id
    ),
    DeliveryState.FAILED: lambda inbox, delivery_id: inbox.mark_failed(
        delivery_id, "boom"
    ),
    DeliveryState.ABANDONED: lambda inbox, delivery_id: inbox.mark_abandoned(
        delivery_id, "boom"
    ),
}


def make_prompt(
    delivery_id: str,
    *,
    conversation_id: str = _SESSION,
    text: str | None = None,
) -> QueuedPrompt:
    """构造一条入站提示词。

    ⚠️ 默认正文**由 ``delivery_id`` 派生** —— 平台不给 ``message_id`` 时去重键退回
    ``sha256(platform|conversation_id|text)``，两条正文相同的行会被静默吞掉，
    于是「待补投集合」那几条用例会量到一个空集合（§7.1：空集 ≠ 不存在）。
    """
    return QueuedPrompt(
        delivery_id=delivery_id,
        conversation_id=conversation_id,
        platform=conversation_id.split(":", 1)[0],
        message_id=None,
        text=f"消息 {delivery_id}" if text is None else text,
    )


def _temporary_database_path(test_case: unittest.TestCase) -> str:
    """在**仓库内**的临时目录里备一个数据库路径。

    先注册目录清理、后注册收件箱 ``close()``（LIFO ⇒ 先关连接再删目录）——
    Windows 上还开着的 SQLite 句柄会让 ``rmtree`` 报 ``WinError 32``。
    """
    os.makedirs(_REPOSITORY_TEMP, exist_ok=True)
    temporary_directory = tempfile.TemporaryDirectory(dir=_REPOSITORY_TEMP)
    test_case.addCleanup(temporary_directory.cleanup)
    return os.path.join(temporary_directory.name, "inbox.sqlite3")


def _read_column(database_path: str, delivery_id: str, column: str) -> object:
    """**另开一条连接**直读盘上那一行的那一列。

    ⚠️ 不走收件箱自己的读取面：那些方法按状态过滤，拿它来断言「这一行现在是什么
    状态」等于用被测量的东西当尺子。收件箱的连接是 autocommit 的
    （``open_inbox_connection`` 的 ``isolation_level=None``）⇒ 新连接立刻看得见。
    """
    if column not in _READABLE_COLUMNS:
        raise AssertionError(f"column {column!r} is not on the readable list")
    connection = sqlite3.connect(database_path)
    try:
        row = connection.execute(
            f"SELECT {column} FROM inbox WHERE delivery_id = ?", (delivery_id,)
        ).fetchone()
    finally:
        connection.close()
    return None if row is None else row[0]


class RecordingDispatch:
    """假的投递：记下投过谁，可以注入一次异常。"""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.dispatched: list[QueuedPrompt] = []

    def __call__(self, prompt: QueuedPrompt) -> None:
        self.dispatched.append(prompt)
        if self.error is not None:
            raise self.error


class _InboxThatCannotMarkAttempting:
    """除 ``mark_attempting`` 外全部转发给真收件箱。

    ⚠️ 它模拟的是**一次持续的写盘故障**（盘满 / 库被锁），而不是「两次独立的坏
    运气」—— ``replay_one`` 的 docstring 明写那是这条路唯一的现实触发条件。
    """

    def __init__(self, inbox: InboundInbox) -> None:
        self._inbox = inbox

    def __getattr__(self, name: str):
        return getattr(self._inbox, name)

    def mark_attempting(self, delivery_id: str) -> None:
        raise sqlite3.OperationalError("database is locked")


#: 哨兵：让 ``inbox=None``（收件箱未启用）与「没传、用 harness 那个」可分辨。
_USE_THE_HARNESS_INBOX = object()


class DeliveryRetryTestCase(unittest.TestCase):
    """真收件箱 + 假投递 + 可拨的时钟。"""

    def setUp(self) -> None:
        # 时钟默认落在「任何退避期限都已经过去」的位置 —— 这样失败行是**到期**的，
        # 想看「没到期」那一档的用例自己传 now=0.0。
        self.now = time.time() + 3600.0
        self.database_path = _temporary_database_path(self)
        self.inbox = InboundInbox(self.database_path)
        self.addCleanup(self.inbox.close)
        self.dispatch = RecordingDispatch()

    def command(
        self,
        *,
        now: float | None = None,
        inbox: object = _USE_THE_HARNESS_INBOX,
        dispatch: RecordingDispatch | None = None,
    ) -> DeliveryRetryCommand:
        if inbox is _USE_THE_HARNESS_INBOX:
            inbox = self.inbox
        if dispatch is None:
            dispatch = self.dispatch
        moment = self.now if now is None else now
        return DeliveryRetryCommand(
            inbox=inbox, dispatch=dispatch, clock=lambda: moment
        )

    def record(self, prompt: QueuedPrompt) -> QueuedPrompt:
        """落一行并**当场断言它真的落盘了**。

        ⚠️ 去重命中会静默返回 ``DUPLICATE``（没写盘）⇒ 下面那些集合型断言会变成
        空集断言，而空集断言看起来永远是绿的（§7.1）。
        """
        self.assertEqual(self.inbox.record(prompt), RecordOutcome.RECORDED)
        return prompt

    def state_of(self, delivery_id: str) -> object:
        return _read_column(self.database_path, delivery_id, "state")

    def attempts_of(self, delivery_id: str) -> object:
        return _read_column(self.database_path, delivery_id, "attempts")

    def offered_delivery_ids(
        self, conversation_id: str = _SESSION, *, now: float | None = None
    ) -> list[str]:
        return [
            prompt.delivery_id
            for prompt in self.command(now=now)._candidates(conversation_id)
        ]

    def seed_every_state(self, conversation_id: str = _SESSION) -> dict[str, str]:
        """把 ``DeliveryState`` 的每一档都真摆到盘上，返回 ``delivery_id → 状态``。"""
        state_by_delivery_id: dict[str, str] = {}
        for position, (state, move_into_the_inbox) in enumerate(
            _MOVE_INTO_THE_INBOX.items(), start=1
        ):
            delivery_id = f"{position}-{state}"
            self.record(make_prompt(delivery_id, conversation_id=conversation_id))
            move_into_the_inbox(self.inbox, delivery_id)
            state_by_delivery_id[delivery_id] = state
        return state_by_delivery_id


class TestWhichRowsAreOfferedForManualRetry(DeliveryRetryTestCase):
    def test_the_seeding_table_covers_every_delivery_state(self):
        """前提守卫：摆法表必须覆盖全集，否则下面两条会**漏掉**新加的那一档。"""
        self.assertEqual(set(_MOVE_INTO_THE_INBOX), set(_EVERY_DELIVERY_STATE))

    def test_only_pending_and_due_failed_rows_are_offered(self):
        state_by_delivery_id = self.seed_every_state()
        offered = self.offered_delivery_ids()
        self.assertEqual(
            {state_by_delivery_id[delivery_id] for delivery_id in offered},
            {DeliveryState.PENDING, DeliveryState.FAILED},
        )

    def test_a_failed_row_still_waiting_out_its_backoff_is_not_offered(self):
        """两臂**只差时钟**：把时间拨回 0，那条 ``failed`` 就还没到期。"""
        state_by_delivery_id = self.seed_every_state()
        offered = self.offered_delivery_ids(now=0.0)
        self.assertEqual(
            [state_by_delivery_id[delivery_id] for delivery_id in offered],
            [DeliveryState.PENDING],
        )


class TestListingTheRows(DeliveryRetryTestCase):
    def test_an_empty_session_says_so(self):
        reply = self.command().reply_for(_SESSION, "")
        self.assertIsInstance(reply, DeliveryReply)
        self.assertEqual(reply.kind, "text")
        self.assertEqual(reply.text, "本会话当前没有待补投的消息。")

    def test_blank_arguments_list_the_rows(self):
        self.record(make_prompt("a-row"))
        for blank in ("", "   ", "\t\n"):
            with self.subTest(blank=blank):
                reply = self.command().reply_for(_SESSION, blank)
                self.assertEqual(reply.kind, "text")
                self.assertIn("本会话有待补投的消息 1 条：", reply.text)

    def test_the_rows_are_numbered_from_one(self):
        self.record(make_prompt("aa-first", text="第一条"))
        self.record(make_prompt("bb-second", text="第二条"))
        reply = self.command().reply_for(_SESSION, "retry")
        self.assertEqual(
            reply.text.splitlines(),
            [
                "本会话有待补投的消息 2 条：",
                "1. 第一条",
                "2. 第二条",
                "用 /delivery retry <序号> 补投其中一条。",
            ],
        )

    def test_whitespace_in_a_row_collapses_to_one_line(self):
        self.record(make_prompt("a-row", text="  第一行\n\n第二行\t尾巴  "))
        reply = self.command().reply_for(_SESSION, "")
        self.assertIn("\n1. 第一行 第二行 尾巴\n", reply.text)

    def test_a_long_row_is_truncated_to_the_preview_limit(self):
        # 先钉住常量本身：下面那条断言既用 PREVIEW_LIMIT 又用字面量 59/60，
        # 没有这一条就只是「两条同源的断言互相印证」（§9 恒真断言）。
        self.assertEqual(PREVIEW_LIMIT, 60)
        self.record(make_prompt("a-row", text="x" * 200))
        reply = self.command().reply_for(_SESSION, "")
        self.assertIn("\n1. " + "x" * (PREVIEW_LIMIT - 1) + "…\n", reply.text)

    def test_another_sessions_rows_are_not_listed(self):
        self.record(make_prompt("mine", text="我的"))
        self.record(make_prompt("theirs", conversation_id=_OTHER_SESSION, text="别人的"))
        reply = self.command().reply_for(_SESSION, "")
        self.assertIn("本会话有待补投的消息 1 条：", reply.text)
        self.assertIn("1. 我的", reply.text)
        self.assertNotIn("别人的", reply.text)


class TestCommandUsage(DeliveryRetryTestCase):
    def test_unknown_verbs_are_usage_errors(self):
        for args in ("halt", "retryx 1"):
            with self.subTest(args=args):
                reply = self.command().reply_for(_SESSION, args)
                self.assertEqual(reply.kind, "error")
                self.assertIn("用法：/delivery retry", reply.text)

    def test_too_many_tokens_are_a_usage_error(self):
        reply = self.command().reply_for(_SESSION, "retry 1 2")
        self.assertEqual(reply.kind, "error")
        self.assertIn("用法：/delivery retry", reply.text)

    def test_ordinals_that_are_not_positive_ascii_digits_are_usage_errors(self):
        # ``一`` / 全角 ``１`` 走的是 ``isascii()`` 那道闸：``isdigit()`` 对全角数字
        # 也为真，只判它就会让用户「打了序号却被报用法错误」。
        for token in ("0", "-1", "一", "１", "1.5"):
            with self.subTest(token=token):
                reply = self.command().reply_for(_SESSION, f"retry {token}")
                self.assertEqual(reply.kind, "error")
                self.assertIn("用法：/delivery retry", reply.text)


class TestPickingByOrdinal(DeliveryRetryTestCase):
    def test_an_out_of_range_ordinal_names_the_current_count(self):
        self.record(make_prompt("a-row"))
        reply = self.command().reply_for(_SESSION, "retry 3")
        self.assertEqual(reply.kind, "error")
        self.assertEqual(reply.text, "序号 3 超出范围：本会话当前有待补投的消息 1 条。")

    def test_retrying_ordinal_one_only_sends_that_sessions_first_row(self):
        self.record(make_prompt("a-row", text="我的第一条"))
        self.record(make_prompt("theirs", conversation_id=_OTHER_SESSION, text="别人的"))
        reply = self.command().reply_for(_SESSION, "retry 1")
        self.assertEqual(reply.kind, "text")
        self.assertEqual(reply.text, "已补投 #1：我的第一条")
        self.assertEqual(
            [prompt.delivery_id for prompt in self.dispatch.dispatched], ["a-row"]
        )
        self.assertEqual(self.state_of("a-row"), DeliveryState.DELIVERED)
        self.assertEqual(self.state_of("theirs"), DeliveryState.PENDING)


class TestResendingOneRow(DeliveryRetryTestCase):
    def test_a_row_that_goes_through_is_reported_as_delivered(self):
        self.record(make_prompt("a-row", text="看一下 README"))
        reply = self.command().reply_for(_SESSION, "retry 1")
        self.assertEqual(reply.text, "已补投 #1：看一下 README")
        self.assertEqual(reply.kind, "text")
        self.assertEqual(self.state_of("a-row"), DeliveryState.DELIVERED)
        self.assertEqual(self.attempts_of("a-row"), 0)

    def test_a_definite_failure_is_reported_and_spends_one_attempt(self):
        self.record(make_prompt("a-row", text="看一下 README"))
        self.dispatch.error = _ServerRefusal("服务端明确拒收")
        reply = self.command().reply_for(_SESSION, "retry 1")
        self.assertEqual(reply.kind, "error")
        self.assertEqual(reply.text, "#1 补投失败；下一次启动会自动重试。")
        self.assertEqual(self.state_of("a-row"), DeliveryState.FAILED)
        self.assertEqual(self.attempts_of("a-row"), 1)

    def test_an_unknown_outcome_is_never_reported_as_a_failure(self):
        self.record(make_prompt("a-row", text="看一下 README"))
        self.dispatch.error = RuntimeError("连接在读到响应前中断")
        reply = self.command().reply_for(_SESSION, "retry 1")
        self.assertEqual(reply.kind, "error")
        self.assertEqual(
            reply.text,
            "#1 已提交，但没能确认结果（很可能 agent 已经在跑了）。"
            "它不会再被自动重投，需要的话请自己确认一次。",
        )
        self.assertEqual(self.state_of("a-row"), DeliveryState.OUTCOME_UNKNOWN)
        # ⚠️ 本档**不碰**重试预算（它没有「下一次」）⇒ 拿 attempts 量预算的用例
        # 会量到另一件事上，所以这一条必须在这里单独钉住。
        self.assertEqual(self.attempts_of("a-row"), 0)

    def test_a_never_sent_stamp_turns_a_status_less_failure_into_a_definite_one(self):
        self.record(make_prompt("a-row", text="看一下 README"))
        self.dispatch.error = mark_prompt_never_sent(
            RuntimeError("连接未建立（prompt 压根没交出去）")
        )
        reply = self.command().reply_for(_SESSION, "retry 1")
        self.assertEqual(reply.text, "#1 补投失败；下一次启动会自动重试。")
        self.assertEqual(self.state_of("a-row"), DeliveryState.FAILED)
        self.assertEqual(self.attempts_of("a-row"), 1)

    def test_a_row_whose_attempt_could_not_be_recorded_is_not_sent_at_all(self):
        self.record(make_prompt("a-row", text="看一下 README"))
        unable_to_record = _InboxThatCannotMarkAttempting(self.inbox)
        with self.assertLogs("opencode_bridge.inbox_recovery", level="ERROR"):
            reply = self.command(inbox=unable_to_record).reply_for(_SESSION, "retry 1")
        self.assertEqual(reply.kind, "error")
        self.assertEqual(
            reply.text, "#1 没能记下投递状态，因此这次没有投递；请稍后再试。"
        )
        # 「记不下进行中就不投」是这条用例真正在量的东西。
        self.assertEqual(self.dispatch.dispatched, [])
        self.assertEqual(self.state_of("a-row"), DeliveryState.PENDING)

    def test_the_last_attempt_of_the_budget_is_reported_as_exhausted(self):
        self.record(make_prompt("a-row", text="看一下 README"))
        # 把预算耗到只剩一级：mark_failed 正是在那里给 attempts 加一。
        self.inbox.mark_failed("a-row", "第一次明确失败")
        self.inbox.mark_failed("a-row", "第二次明确失败")
        self.assertEqual(self.attempts_of("a-row"), MAX_ATTEMPTS - 1)
        self.assertEqual(self.state_of("a-row"), DeliveryState.FAILED)
        self.dispatch.error = _ServerRefusal("服务端明确拒收")
        reply = self.command().reply_for(_SESSION, "retry 1")
        self.assertEqual(reply.kind, "error")
        self.assertEqual(reply.text, "#1 补投失败，重试预算已耗尽，不会再自动重投。")
        self.assertEqual(self.state_of("a-row"), DeliveryState.ABANDONED)
        self.assertEqual(self.attempts_of("a-row"), MAX_ATTEMPTS)

    def test_the_numbers_are_recomputed_between_two_commands(self):
        """⚠️ 知情代价：列表在**每次**调用时重算 ⇒ 期间状态变了，序号就指向另一条。"""
        self.record(make_prompt("aa-first", text="第一条"))
        self.record(make_prompt("bb-second", text="第二条"))
        before = self.command().reply_for(_SESSION, "").text.splitlines()
        self.assertEqual(before[0], "本会话有待补投的消息 2 条：")
        self.command().reply_for(_SESSION, "retry 1")
        after = self.command().reply_for(_SESSION, "").text.splitlines()
        self.assertEqual(after[0], "本会话有待补投的消息 1 条：")
        self.assertEqual(after[1], "1. 第二条")


class TestWhenTheInboxIsDisabled(DeliveryRetryTestCase):
    def test_a_disabled_inbox_lists_nothing_and_says_why(self):
        reply = self.command(inbox=None).reply_for(_SESSION, "")
        # ⚠️ 是「说明」，不是「报错」—— 没开收件箱不是故障。
        self.assertEqual(reply.kind, "text")
        self.assertEqual(reply.text, "收件箱未启用，没有可补投的消息。")

    def test_a_disabled_inbox_refuses_to_resend(self):
        reply = self.command(inbox=None).reply_for(_SESSION, "retry 1")
        self.assertEqual(reply.kind, "text")
        self.assertEqual(reply.text, "收件箱未启用，没有可补投的消息。")
        self.assertEqual(self.dispatch.dispatched, [])


class TestAnUnknownReplayResultIsNeverFaked(DeliveryRetryTestCase):
    def test_an_unrecognised_replay_result_is_reported_as_unknown(self):
        """新增一档返回值而作者忘了改判定时，**不许**落进「声称成功」那一支。

        ⚠️ 既有行为用例一条都不会红（它们在那条路径上一程都不经过）⇒ 只有这里能抓。
        """
        self.record(make_prompt("a-row", text="看一下 README"))
        command = self.command()
        with mock.patch(
            "opencode_bridge.delivery_retry.replay_one",
            return_value="some_future_result",
        ), self.assertLogs(
            "opencode_bridge.delivery_retry", level="WARNING"
        ) as captured:
            reply = command.reply_for(_SESSION, "retry 1")
        self.assertEqual(reply.kind, "error")
        self.assertIn("补投结果不明", reply.text)
        self.assertNotIn("已补投", reply.text)
        self.assertNotIn("投递失败", reply.text)
        self.assertIn("some_future_result", "\n".join(captured.output))


# ----------------------------------------------------------------------
# 形状守门
# ----------------------------------------------------------------------
#: 收件箱的状态写入面 —— 它们只该在恢复层里出现。
_INBOX_STATE_MUTATORS = (
    "mark_attempting",
    "mark_pending",
    "mark_delivered",
    "mark_failed",
    "mark_outcome_unknown",
    "mark_abandoned",
)


def _module_source(module_name: str) -> str:
    path = os.path.join(_REPOSITORY_ROOT, "opencode_bridge", f"{module_name}.py")
    with io.open(path, encoding="utf-8") as handle:
        return handle.read()


def _attribute_calls(source: str, attribute_names: tuple[str, ...]) -> set[str]:
    """源码里 ``<某物>.<名字>(…)`` 形态出现过的名字集合。"""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in attribute_names:
            found.add(func.attr)
    return found


def _replay_one_calls(source: str) -> int:
    """源码里裸名调用 ``replay_one(…)`` 的处数（⛔ 不含某个对象的同名方法）。"""
    return sum(
        1
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "replay_one"
    )


def _kind_literals(source: str) -> set[str]:
    """源码里写出的每一个 ``kind="…"`` 字面量（关键字实参形态）。"""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if (
                keyword.arg == "kind"
                and isinstance(keyword.value, ast.Constant)
                and isinstance(keyword.value.value, str)
            ):
                found.add(keyword.value.value)
    return found


class TestTheRetryModuleForwardsInsteadOfWritingInboxState(unittest.TestCase):
    """``delivery_retry.py`` 一条收件箱状态都不许自己写。

    理由见模块 docstring 第 6 组：抄一份实现**不会让任何行为用例变红**。
    """

    def setUp(self) -> None:
        self.source = _module_source("delivery_retry")
        # 前提守卫：读到的**必须**是那个真模块 —— 路径写错时 ast 会静默解析一个空文件，
        # 于是每一条形状断言都会在空集上通过。
        self.assertIn("class DeliveryRetryCommand", self.source)

    def test_the_retry_module_never_calls_an_inbox_state_mutator(self):
        self.assertEqual(_attribute_calls(self.source, _INBOX_STATE_MUTATORS), set())

    def test_the_same_matcher_does_catch_the_mutators_in_the_recovery_module(self):
        """**对照臂** —— 没有它，上一条在「匹配器坏了」时照样绿。"""
        recovery_source = _module_source("inbox_recovery")
        self.assertIn("def replay_one(", recovery_source)
        self.assertTrue(
            {
                "mark_attempting",
                "mark_outcome_unknown",
                "mark_failed",
                "mark_delivered",
            }
            <= _attribute_calls(recovery_source, _INBOX_STATE_MUTATORS)
        )

    def test_the_retry_module_reaches_the_row_through_replay_one(self):
        self.assertGreaterEqual(_replay_one_calls(self.source), 1)

    def test_every_reply_kind_is_one_of_the_two_known_kinds(self):
        kinds = _kind_literals(self.source)
        # ⛔ 少了这一条，下面那条断言在「探针什么都没找到」时恒真。
        self.assertTrue(kinds)
        self.assertLessEqual(kinds, {"text", "error"})


if __name__ == "__main__":
    unittest.main()

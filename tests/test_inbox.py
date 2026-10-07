"""``inbox.py`` 测试：入站写前收件箱的状态机、去重与行数治理。

重点覆盖八类真实风险：

1. **写前落盘** —— 接受一条消息时它**已经**在盘上，不依赖分发成功；
2. **状态转换** —— 每个写入点各自落到对应状态（断言**盘上的行**，不只看返回值）；
3. **``INSERT OR IGNORE`` 去重** —— 同一个 ``delivery_id`` 记两次，第二次返回
   ``False`` 且**不新增行**；已送达的回执不会被重投覆盖掉；
4. **退避是期限不是 sleep** —— ``not_before`` 写进盘上，且**跨重启还在**；
5. **重试预算耗尽 → ``abandoned``** —— 绝不再排下一次重试；
6. **回执保留期** —— ``delivered`` 行过期即删，短保留期内必须留着当回执；
7. **行数上限的淘汰顺序** —— 先 ``delivered`` 再 ``abandoned``，``pending`` /
   ``attempting`` / ``outcome_unknown`` / ``failed`` 永不淘汰；
8. **关闭之后仍然安全** —— ``close()`` 可重复调用，之后所有公开方法是空操作。

零真实网络、零真实 sleep。涉及"等了多久"的地方一律断言**盘上的期限**而不是真的
等：把 ``not_before`` 读出来与阶梯值比较，或直接问"在某个时刻它到期了吗"。

⚠️ 每个用例都**先关连接再删临时目录**：Windows 上一个还开着的 SQLite 句柄会让
``rmtree`` 直接失败（``WinError 32``）。这里靠 ``addCleanup`` 的 LIFO 顺序保证
（先注册临时目录、后注册 ``close``，于是 ``close`` 先跑）。
"""

from __future__ import annotations

import ast
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

from opencode_bridge.inbox import (
    BACKOFF_LADDER_SECONDS,
    DEFAULT_DELIVERED_RETENTION_SECONDS,
    DeliveryState,
    InboundInbox,
    MAX_ATTEMPTS,
    QueuedPrompt,
)
from opencode_bridge.inbox_row_cap import _EVICTION_ORDER, UNSETTLED_STATES
from opencode_bridge import inbox as inbox_module
from opencode_bridge import inbox_recovery as inbox_recovery_module
from opencode_bridge import inbox_retry_budget as inbox_retry_budget_module
from opencode_bridge import inbox_row_cap as inbox_row_cap_module
from opencode_bridge import inbox_sqlite as inbox_sqlite_module
from opencode_bridge.inbox_sqlite import open_inbox_connection

_REPOSITORY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: 仓库内的临时目录。存在就用它 —— 这样即使 TEMP/TMP 指向机器别处，
#: 测试也**不可能**把文件写到仓库之外。
_REPOSITORY_TEMP = os.path.join(_REPOSITORY_ROOT, ".tmp")

#: 收件箱家族的源文件路径（供下面那几条结构性断言用）。
_INBOX_SOURCES = {
    "inbox": os.path.join(_REPOSITORY_ROOT, "opencode_bridge", "inbox.py"),
    "inbox_retry_budget": os.path.join(
        _REPOSITORY_ROOT, "opencode_bridge", "inbox_retry_budget.py"),
    "inbox_row_cap": os.path.join(_REPOSITORY_ROOT, "opencode_bridge", "inbox_row_cap.py"),
    "inbox_sqlite": os.path.join(_REPOSITORY_ROOT, "opencode_bridge", "inbox_sqlite.py"),
}


def module_level_assignments(path: str) -> set[str]:
    """这个源文件在**模块顶层**被赋值（而不是 import）过的名字。

    只看顶层：函数体里的局部赋值不算 —— 我们要问的是"这个定义住在哪个模块"。
    """
    with open(path, encoding="utf-8") as source_file:
        tree = ast.parse(source_file.read(), path)
    assigned = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assigned.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            assigned.add(node.target.id)
        elif isinstance(node, ast.ClassDef):
            assigned.add(node.name)
    return assigned


def make_prompt(
    delivery_id: str,
    *,
    conversation_id: str = "telegram:100200",
    platform: str = "telegram",
    message_id: str | None = None,
    text: str = "看一下 README",
) -> QueuedPrompt:
    """构造一条入站提示词。默认值指向一个假会话，不含任何真实身份信息。"""
    return QueuedPrompt(
        delivery_id=delivery_id,
        conversation_id=conversation_id,
        platform=platform,
        message_id=message_id,
        text=text,
    )


def read_inbox_rows(database_path: str) -> list[dict]:
    """另开一条只读连接读盘上的原始行。

    状态转换是这一层最要紧的不变量，而公开 API **故意**不暴露 ``state``（它只
    暴露按状态分好的四个读取方法）。要断言"确实落成了 delivered、attempts 到底是
    几、not_before 是多少"，就得直接读盘 —— 断言内部状态，而不是解析返回值。
    """
    connection = sqlite3.connect(database_path)
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT delivery_id, state, attempts, not_before, last_error, text, updated_at"
            " FROM inbox ORDER BY delivery_id ASC"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def _only_row(database_path: str) -> dict:
    """断言只剩一行并返回它 —— 避免用例里到处写 ``rows[0]``（读起来像漏了断言）。"""
    rows = read_inbox_rows(database_path)
    if len(rows) != 1:
        raise AssertionError(f"expected exactly one inbox row, got {len(rows)}: {rows}")
    return rows[0]
class InboxTestCase(unittest.TestCase):
    """给每个用例一份仓库内的临时目录，以及一个自动关闭的收件箱工厂。"""

    def setUp(self) -> None:
        os.makedirs(_REPOSITORY_TEMP, exist_ok=True)
        # 先注册目录清理，后注册 close() —— LIFO 保证连接先关、目录后删。
        self._temporary_directory = tempfile.TemporaryDirectory(dir=_REPOSITORY_TEMP)
        self.addCleanup(self._temporary_directory.cleanup)

    def database_path(self, name: str = "inbox.sqlite3") -> str:
        return os.path.join(self._temporary_directory.name, name)

    def open_inbox(self, name: str = "inbox.sqlite3", **options) -> tuple[InboundInbox, str]:
        """开一个收件箱，返回 ``(收件箱, 数据库路径)``；用例结束前自动关闭。"""
        database_path = self.database_path(name)
        inbox = InboundInbox(database_path, **options)
        self.addCleanup(inbox.close)
        return inbox, database_path


class TestWriteAheadRecord(InboxTestCase):
    """接受一条消息时，它必须**已经**在盘上。"""

    def test_recorded_prompt_is_pending_and_readable(self):
        inbox, _database_path = self.open_inbox()
        prompt = make_prompt("telegram:100200:7", message_id="7")

        self.assertTrue(inbox.record(prompt))

        self.assertEqual(inbox.pending_prompts(), [prompt])

    def test_record_survives_a_reopen_because_it_never_depended_on_dispatch(self):
        """这一条就是整个模块存在的理由：进程死掉，义务还在盘上。"""
        database_path = self.database_path()
        first_run = InboundInbox(database_path)
        first_run.record(make_prompt("matrix:!room:9", conversation_id="matrix:!room:9",
                                     platform="matrix"))
        first_run.close()  # 模拟进程在这里被杀掉，没有 close() 之外任何收尾

        with InboundInbox(database_path) as second_run:
            self.assertEqual(
                [prompt.delivery_id for prompt in second_run.pending_prompts()],
                ["matrix:!room:9"],
            )

    def test_second_record_of_the_same_delivery_id_is_deduped(self):
        inbox, database_path = self.open_inbox()
        inbox.record(make_prompt("slack:C0ABCDEF:1", conversation_id="slack:C0ABCDEF",
                                 platform="slack", message_id="1"))

        self.assertFalse(inbox.record(make_prompt("slack:C0ABCDEF:1",
                                                 conversation_id="slack:C0ABCDEF",
                                                 platform="slack", message_id="1")))

        rows = read_inbox_rows(database_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], DeliveryState.PENDING)

    def test_platform_redelivery_does_not_overwrite_the_delivered_receipt(self):
        """已送达的那行**就是回执**。重投既不能新增行，也不能把它打回 pending。

        这是 ``INSERT OR IGNORE`` 而不是 ``INSERT OR REPLACE`` 的全部理由 ——
        写成 REPLACE 会让回执变成 pending，于是同一条消息被 agent 再跑一遍。
        """
        inbox, database_path = self.open_inbox()
        prompt = make_prompt("discord:42:abc", conversation_id="discord:42",
                             platform="discord", message_id="abc")
        inbox.record(prompt)
        inbox.mark_attempting(prompt.delivery_id)
        inbox.mark_delivered(prompt.delivery_id)

        self.assertFalse(inbox.record(prompt))

        rows = read_inbox_rows(database_path)
        self.assertEqual([(row["state"], row["attempts"]) for row in rows],
                         [(DeliveryState.DELIVERED, 0)])
        # 也不会在重投时把状态推回"待投递"
        self.assertEqual(inbox.pending_prompts(), [])


class TestStateTransitions(InboxTestCase):
    """写入点 ↔ 状态，一一对应。"""

    def test_attempting_moves_the_row_out_of_pending_into_uncertain(self):
        inbox, database_path = self.open_inbox()
        prompt = make_prompt("ntfy:topic1:3", conversation_id="ntfy:topic1",
                             platform="ntfy", message_id="3")
        inbox.record(prompt)

        inbox.mark_attempting(prompt.delivery_id)

        self.assertEqual(inbox.pending_prompts(), [])
        self.assertEqual(inbox.uncertain_prompts(), [prompt])
        self.assertEqual(
            read_inbox_rows(database_path)[0]["state"], DeliveryState.ATTEMPTING
        )

    def test_mark_pending_puts_a_busy_row_back_into_the_replayable_read(self):
        """409 的落点：退回 ``pending`` —— 从"结果不可知"变回"从未尝试过"。

        断言的是**两个读取方法同时**变了，而不只是盘上那个字符串：留在
        ``attempting`` 里，恢复层就会走"只告警、绝不重放"那条分支
        （见 :mod:`tests.test_inbox_recovery`），那一行就成了永远送不到的死行。
        """
        inbox, database_path = self.open_inbox()
        prompt = make_prompt("slack:C0ABCDEF:7", conversation_id="slack:C0ABCDEF",
                             platform="slack", message_id="7")
        inbox.record(prompt)
        inbox.mark_attempting(prompt.delivery_id)

        inbox.mark_pending(prompt.delivery_id)

        self.assertEqual(inbox.pending_prompts(), [prompt])
        self.assertEqual(
            inbox.uncertain_prompts(), [],
            "这一行必须离开'结果不可知'那档，否则恢复层不会重放它",
        )
        self.assertEqual(_only_row(database_path)["state"], DeliveryState.PENDING)

    def test_mark_pending_spends_no_budget_and_keeps_the_recorded_failure(self):
        """409 不是失败：``attempts`` 不动、``not_before`` 归零、上次失败原因留着。

        刻意**从一行 ``failed`` 直接退回**（不先走 ``mark_attempting``）：那才是这条
        转移真正要保证的性质 —— 退回待投递意味着**立刻**可重放，而不是继续被上一次
        失败留下的退避期限挡住。只测"attempting → pending"会漏掉这一点，因为
        ``mark_attempting`` 自己已经把 ``not_before`` 清零了。
        """
        inbox, database_path = self.open_inbox()
        prompt = make_prompt("irc:#ops:12", conversation_id="irc:#ops", platform="irc",
                             message_id="12")
        inbox.record(prompt)
        inbox.mark_failed(prompt.delivery_id, "ConnectionResetError: boom")
        self.assertGreater(
            _only_row(database_path)["not_before"], 0.0,
            "前提：真失败确实排了一个未来的期限",
        )

        inbox.mark_pending(prompt.delivery_id)

        row = _only_row(database_path)
        self.assertEqual(row["state"], DeliveryState.PENDING)
        self.assertEqual(row["attempts"], 1, "退回 pending 不得凭空多出一次尝试预算")
        self.assertEqual(row["not_before"], 0.0,
                         "退回 pending 必须立刻可重放，不能继续被旧期限挡住")
        self.assertEqual(row["last_error"], "ConnectionResetError: boom",
                         "409 本身不是失败，不该覆盖上一次真正失败的原因")
        self.assertEqual(
            [queued.delivery_id for queued in inbox.pending_prompts()],
            [prompt.delivery_id],
        )

    def test_a_row_put_back_to_pending_is_still_bounded_by_the_retry_budget(self):
        """退回 pending **不是**预算的旁路：重放再失败照样一级一级烧到终态。

        把 409 插在每一次失败之前，也就是最宽松的读法 —— 每次 409 都"免费"，
        若这样都能拿到无限次机会，这条转移就是在削弱 at-most-once 的另一半。
        实际次数由 ``mark_failed`` 的算术决定：``MAX_ATTEMPTS`` 次失败即终态。
        """
        inbox, database_path = self.open_inbox()
        prompt = make_prompt("matrix:!room:9:4", conversation_id="matrix:!room:9",
                             platform="matrix", message_id="4")
        inbox.record(prompt)

        for attempt_number in range(1, MAX_ATTEMPTS + 1):
            inbox.mark_attempting(prompt.delivery_id)
            inbox.mark_pending(prompt.delivery_id)

            self.assertEqual(
                _only_row(database_path)["attempts"], attempt_number - 1,
                "第 %d 次被拒时花掉的仍只是前 %d 次失败的预算" % (
                    attempt_number, attempt_number - 1),
            )
            inbox.mark_failed(prompt.delivery_id, f"第 {attempt_number} 次真失败")

        self.assertEqual(_only_row(database_path)["state"], DeliveryState.ABANDONED)
        self.assertEqual(_only_row(database_path)["attempts"], MAX_ATTEMPTS)
        self.assertEqual(inbox.pending_prompts(), [])
        self.assertEqual(inbox.due_failed_prompts(1e12), [])

    def test_delivered_clears_the_row_out_of_every_replayable_read(self):
        inbox, database_path = self.open_inbox()
        prompt = make_prompt("email:uid-1", conversation_id="email:someone@example.invalid",
                             platform="email", message_id="uid-1")
        inbox.record(prompt)
        inbox.mark_attempting(prompt.delivery_id)

        inbox.mark_delivered(prompt.delivery_id)

        self.assertEqual(inbox.pending_prompts(), [])
        self.assertEqual(inbox.uncertain_prompts(), [])
        self.assertEqual(inbox.due_failed_prompts(1e12), [])
        self.assertEqual(inbox.abandoned_prompts(), [])
        self.assertEqual(
            read_inbox_rows(database_path)[0]["state"], DeliveryState.DELIVERED
        )

    def test_delivered_also_clears_the_previous_failure_reason(self):
        inbox, database_path = self.open_inbox()
        prompt = make_prompt("irc:#ops:11", conversation_id="irc:#ops", platform="irc",
                             message_id="11")
        inbox.record(prompt)
        inbox.mark_failed(prompt.delivery_id, "ConnectionResetError: boom")

        inbox.mark_delivered(prompt.delivery_id)

        self.assertIsNone(read_inbox_rows(database_path)[0]["last_error"])

    def test_outcome_unknown_moves_the_row_out_of_every_replayable_read(self):
        """「结果不可知」那一档的落点：它必须离开**每一个**可重放的读取入口。

        ⭐ 承重的是 ``pending_prompts()`` / ``due_failed_prompts()`` 两个空列表：
        恢复层只从它们两个取要重放的行，所以它从那儿消失就等于**重放不到** ——
        而它**必须**重放不到，因为请求可能**已经**被 agent 处理过了。

        ``attempts`` 不动是同一件事的另一半：这一档没有"下次再试"，
        白花一级预算只会把 :meth:`~opencode_bridge.inbox.InboundInbox.mark_failed`
        的算术搅浑。
        """
        inbox, database_path = self.open_inbox()
        prompt = make_prompt("matrix:!room:9:15", conversation_id="matrix:!room:9",
                             platform="matrix", message_id="15")
        inbox.record(prompt)
        inbox.mark_failed(prompt.delivery_id, "ConnectionResetError: 上一次真失败")
        spent_before = _only_row(database_path)["attempts"]
        inbox.mark_attempting(prompt.delivery_id)

        inbox.mark_outcome_unknown(prompt.delivery_id, "URLError: 连接被拒")

        self.assertEqual(inbox.pending_prompts(), [])
        self.assertEqual(inbox.due_failed_prompts(1e12), [],
                         "结果不可知的一行绝不许出现在可重放的那一档")
        self.assertEqual(inbox.uncertain_prompts(), [prompt],
                         "它必须落在只告警那一档，否则恢复层连告警都发不出来")
        row = _only_row(database_path)
        self.assertEqual(row["state"], DeliveryState.OUTCOME_UNKNOWN)
        self.assertEqual(row["attempts"], spent_before,
                         "这一档没有下一次，白烧一级预算只会把 mark_failed 的算术搅浑")
        self.assertEqual(row["not_before"], 0.0, "本档没有退避期限可排")
        self.assertEqual(row["last_error"], "URLError: 连接被拒",
                         "那句原因是告警与排查的唯一凭据，必须留下来")

    def test_unknown_delivery_id_marks_are_silent_no_ops(self):
        """已被淘汰的行不该让清理路径炸掉。"""
        inbox, _database_path = self.open_inbox()

        inbox.mark_attempting("never-existed")
        inbox.mark_pending("never-existed")
        inbox.mark_outcome_unknown("never-existed", "boom")
        inbox.mark_delivered("never-existed")
        inbox.mark_failed("never-existed", "boom")
        inbox.mark_abandoned("never-existed", "boom")


class TestBackoffAndRetryBudget(InboxTestCase):
    """退避是写进盘里的**期限**，而且重启不会把它重置。"""

    def test_first_failure_schedules_a_deadline_in_the_future(self):
        inbox, database_path = self.open_inbox()
        prompt = make_prompt("mattermost:town:5", conversation_id="mattermost:town",
                             platform="mattermost", message_id="5")
        inbox.record(prompt)
        started_at = time.time()

        inbox.mark_failed(prompt.delivery_id, "TimeoutError: no answer")

        row = _only_row(database_path)
        self.assertEqual(row["state"], DeliveryState.FAILED)
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["last_error"], "TimeoutError: no answer")
        self.assertAlmostEqual(
            row["not_before"] - started_at, BACKOFF_LADDER_SECONDS[0], delta=5.0,
            msg="第一次失败应当按阶梯排下一次重试的期限，而不是立刻重放",
        )

    def test_failed_row_is_invisible_until_its_deadline_passes(self):
        inbox, database_path = self.open_inbox()
        prompt = make_prompt("twitch:chan:8", conversation_id="twitch:chan", platform="twitch",
                             message_id="8")
        inbox.record(prompt)
        inbox.mark_failed(prompt.delivery_id, "boom")
        deadline = _only_row(database_path)["not_before"]

        self.assertEqual(inbox.due_failed_prompts(deadline - 1.0), [])
        self.assertEqual(inbox.due_failed_prompts(deadline), [prompt])

    def test_backoff_deadline_survives_a_reopen(self):
        """期限必须在盘上：睡着的不是内存，是这一行。"""
        database_path = self.database_path()
        first_run = InboundInbox(database_path)
        prompt = make_prompt("qqbot:group:2", conversation_id="qqbot:group", platform="qqbot",
                             message_id="2")
        first_run.record(prompt)
        first_run.mark_failed(prompt.delivery_id, "boom")
        deadline = _only_row(database_path)["not_before"]
        first_run.close()

        with InboundInbox(database_path) as second_run:
            self.assertEqual(second_run.due_failed_prompts(deadline - 1.0), [])
            self.assertEqual(
                [row.delivery_id for row in second_run.due_failed_prompts(deadline)],
                ["qqbot:group:2"],
            )

    def test_each_failure_escalates_the_scheduled_delay(self):
        inbox, database_path = self.open_inbox()
        prompt = make_prompt("homeassistant:zha:4", conversation_id="homeassistant:zha",
                             platform="homeassistant", message_id="4")
        inbox.record(prompt)

        inbox.mark_failed(prompt.delivery_id, "first")
        first_row = _only_row(database_path)
        inbox.mark_failed(prompt.delivery_id, "second")
        second_row = _only_row(database_path)

        self.assertEqual(first_row["attempts"], 1)
        self.assertEqual(second_row["attempts"], 2)
        # 下标是「第几次失败」减一：首次失败就该走第 0 级。
        # 之前这里断言的是 LADDER[1] / LADDER[2]（120s / 600s），那把
        # 「配了三级阶梯却只有两级会触发」这个缺陷一并钉住了——阶梯第 0 级
        # 永远触发不到，首次重试被白白多等了 90 秒。
        self.assertAlmostEqual(
            first_row["not_before"] - first_row["updated_at"],
            BACKOFF_LADDER_SECONDS[0], delta=1.0,
            msg="首次失败应当走阶梯第 0 级",
        )
        self.assertAlmostEqual(
            second_row["not_before"] - second_row["updated_at"],
            BACKOFF_LADDER_SECONDS[1], delta=1.0,
            msg="第二次失败应当排到更远一级，而不是原地重试",
        )

    def test_exhausting_the_retry_budget_abandons_instead_of_scheduling_again(self):
        """**绝不**花掉最后一次预算重试：到 ``MAX_ATTEMPTS`` 就转终态。

        理由（与 hermes 同一条约束）：任何时长的事故都可能比任何定时器活得久，
        而一条被定时器放弃的行就永远没了 —— 转终态 + 一次告警更好发现。
        """
        inbox, database_path = self.open_inbox()
        prompt = make_prompt("telegram:100200:9", message_id="9")
        inbox.record(prompt)
        for _attempt in range(MAX_ATTEMPTS - 1):
            inbox.mark_failed(prompt.delivery_id, "still failing")

        inbox.mark_failed(prompt.delivery_id, "the last failure")

        row = _only_row(database_path)
        self.assertEqual(row["state"], DeliveryState.ABANDONED)
        self.assertEqual(row["attempts"], MAX_ATTEMPTS)
        self.assertEqual(row["not_before"], 0.0, "终态行不该再挂着任何重试期限")
        self.assertEqual([queued.delivery_id for queued in inbox.abandoned_prompts()],
                         ["telegram:100200:9"])
        # 终态行永远不进重放队列，即便把时钟推到天荒地老
        self.assertEqual(inbox.due_failed_prompts(1e12), [])

    def test_mark_abandoned_forces_the_terminal_state(self):
        inbox, database_path = self.open_inbox()
        prompt = make_prompt("nextcloud:talk:1", conversation_id="nextcloud:talk",
                             platform="nextcloud", message_id="1")
        inbox.record(prompt)
        inbox.mark_failed(prompt.delivery_id, "boom")

        inbox.mark_abandoned(prompt.delivery_id, "手动放弃：会话已被 /new 重置")

        row = _only_row(database_path)
        self.assertEqual(row["state"], DeliveryState.ABANDONED)
        self.assertEqual(row["not_before"], 0.0)


class TestRetentionAndRowCap(InboxTestCase):
    """行数治理：回执按期清理，兜底上限按状态优先级淘汰。"""

    def test_delivered_receipt_is_kept_within_the_retention_window(self):
        inbox, database_path = self.open_inbox(
            delivered_retention_seconds=DEFAULT_DELIVERED_RETENTION_SECONDS
        )
        prompt = make_prompt("telegram:100200:11", message_id="11")
        inbox.record(prompt)
        inbox.mark_attempting(prompt.delivery_id)
        inbox.mark_delivered(prompt.delivery_id)

        # 再记一条新消息，触发一次清理
        inbox.record(make_prompt("telegram:100200:12", message_id="12"))

        delivery_ids = [row["delivery_id"] for row in read_inbox_rows(database_path)]
        self.assertIn(prompt.delivery_id, delivery_ids,
                      "24 小时内的已送达行就是回执，不许在保留期内删掉")

    def test_delivered_rows_are_pruned_once_past_the_retention_window(self):
        inbox, database_path = self.open_inbox(delivered_retention_seconds=0.0)
        prompt = make_prompt("telegram:100200:13", message_id="13")
        inbox.record(prompt)
        inbox.mark_attempting(prompt.delivery_id)
        inbox.mark_delivered(prompt.delivery_id)

        inbox.record(make_prompt("telegram:100200:14", message_id="14"))

        rows = read_inbox_rows(database_path)
        self.assertEqual([row["delivery_id"] for row in rows], ["telegram:100200:14"])

    def test_expired_receipts_are_also_pruned_at_open(self):
        """上一次运行的回执可能早就过期，不必等下一条消息才清。"""
        database_path = self.database_path()
        first_run = InboundInbox(database_path, delivered_retention_seconds=0.0)
        prompt = make_prompt("telegram:100200:15", message_id="15")
        first_run.record(prompt)
        first_run.mark_attempting(prompt.delivery_id)
        first_run.mark_delivered(prompt.delivery_id)
        first_run.close()

        with InboundInbox(database_path, delivered_retention_seconds=0.0):
            self.assertEqual(read_inbox_rows(database_path), [])

    def test_row_cap_evicts_delivered_before_abandoned(self):
        """4 行（2 终态 + 2 回执）压到 2 行 → 必须牺牲回执，保住 ``abandoned``。

        刻意走"重开一次"这条路：``record()`` 落下的行当下就是 ``pending``，而
        ``pending`` 永不被淘汰 —— 边写边压上限，淘汰顺序会被"这一行还没标成
        delivered"搅乱，测不出优先级。
        """
        database_path = self.database_path()
        with InboundInbox(database_path) as builder:  # 默认上限，足够放下 4 行
            for index in range(2):
                prompt = make_prompt(f"telegram:100200:d{index}", message_id=f"d{index}")
                builder.record(prompt)
                builder.mark_abandoned(prompt.delivery_id, "预算耗尽")
            for index in range(2):
                prompt = make_prompt(f"telegram:100200:v{index}", message_id=f"v{index}")
                builder.record(prompt)
                builder.mark_delivered(prompt.delivery_id)

        with InboundInbox(database_path, max_rows=2):
            rows = read_inbox_rows(database_path)
            self.assertEqual([row["delivery_id"] for row in rows],
                             ["telegram:100200:d0", "telegram:100200:d1"])
            self.assertEqual([row["state"] for row in rows],
                             [DeliveryState.ABANDONED, DeliveryState.ABANDONED])

    def test_row_cap_never_evicts_an_unfulfilled_obligation(self):
        """``pending`` 是一笔还没兑现的处理义务 —— 上限无权把它丢掉。

        宁可超上限并告警，也不静默丢一条用户消息：超限的代价是磁盘，
        丢行的代价是消息永久消失。
        """
        inbox, database_path = self.open_inbox(max_rows=1)
        for index in range(3):
            inbox.record(make_prompt(f"telegram:100200:p{index}", message_id=f"p{index}"))

        with self.assertLogs("opencode_bridge.inbox", level="WARNING") as captured:
            inbox.record(make_prompt("telegram:100200:p9", message_id="p9"))

        rows = read_inbox_rows(database_path)
        self.assertEqual(len(rows), 4)
        self.assertTrue(any("cap" in line for line in captured.output),
                        f"超上限必须留下痕迹，实际日志：{captured.output}")

    def test_row_cap_is_also_enforced_at_open(self):
        database_path = self.database_path()
        first_run = InboundInbox(database_path)
        for index in range(4):
            prompt = make_prompt(f"telegram:100200:c{index}", message_id=f"c{index}")
            first_run.record(prompt)
            first_run.mark_attempting(prompt.delivery_id)
            first_run.mark_delivered(prompt.delivery_id)
        first_run.close()

        with InboundInbox(database_path, max_rows=2):
            rows = read_inbox_rows(database_path)
            self.assertEqual(len(rows), 2)


class TestUnsettledRowsAreNeverSilentlyEvicted(InboxTestCase):
    """不可重放的状态**不会**被行数上限静默淘汰（这条性质不许退化）。

    为什么这一整类都要钉住：``inbox_recovery`` 对这几档的处置各不相同 ——
    ``pending``/``failed`` 会被重放（丢了就丢一条本来送得到的），而
    ``attempting`` 是**只告警、绝不重放**：淘汰它换不来任何补偿，只会让一次
    本可以发现的"结果不可知"变成彻底的静默丢失 —— 连告警都不会有，因为
    ``uncertain_prompts()`` 已经看不到它了。
    """

    def _crash_leaving_attempting(self, database_path: str, count: int) -> None:
        """摆 ``count`` 行 ``attempting``：模拟进程在 ``prompt()`` 中途被杀。"""
        crashed = InboundInbox(database_path)
        for index in range(count):
            prompt = make_prompt(f"telegram:100200:x{index}", message_id=f"x{index}")
            crashed.record(prompt)
            crashed.mark_attempting(prompt.delivery_id)
        crashed.close()

    def test_the_row_cap_keeps_every_attempting_row(self):
        """5 行 ``attempting`` + 上限 5 + 新来一条 → 一行都不许少。

        改动之前这里会淘汰 1 行，恢复层随后只看到 4 条、告警也跟着少算一条 ——
        消息静默消失，无失败记录、无告警。
        """
        database_path = self.database_path()
        self._crash_leaving_attempting(database_path, count=5)

        capped = InboundInbox(database_path, max_rows=5)
        self.addCleanup(capped.close)
        with self.assertLogs("opencode_bridge.inbox", level="WARNING") as captured:
            capped.record(make_prompt("telegram:100200:new", message_id="new"))

        self.assertEqual(
            [row["delivery_id"] for row in read_inbox_rows(database_path)
             if row["state"] == DeliveryState.ATTEMPTING],
            [f"telegram:100200:x{index}" for index in range(5)],
            "attempting 是恢复层唯一'只告警不重放'的一档，淘汰它 = 静默丢消息",
        )
        self.assertIn("attempting=5", "\n".join(captured.output),
                      "超限必须按状态说清留下了什么，否则读者无从判断是不是漏了消息")

    def test_the_row_cap_keeps_every_outcome_unknown_row_too(self):
        """``outcome_unknown`` 与 ``attempting`` 同属"只告警不重放" ⇒ 一行都丢不得。

        淘汰它换不来任何补偿（它**不会**被重放），只会让那条「请重新发送一次」的
        告警**永远发不出去** —— 而用户看着"我明明发了"却什么都不知道，
        正是收件箱要防的那件事。
        """
        database_path = self.database_path()
        interrupted = InboundInbox(database_path)
        for index in range(5):
            prompt = make_prompt(f"telegram:100200:y{index}", message_id=f"y{index}")
            interrupted.record(prompt)
            interrupted.mark_outcome_unknown(prompt.delivery_id, "URLError: 连接被拒")
        interrupted.close()

        capped = InboundInbox(database_path, max_rows=5)
        self.addCleanup(capped.close)
        with self.assertLogs("opencode_bridge.inbox", level="WARNING") as captured:
            capped.record(make_prompt("telegram:100200:new", message_id="new"))

        self.assertEqual(
            [row["delivery_id"] for row in read_inbox_rows(database_path)
             if row["state"] == DeliveryState.OUTCOME_UNKNOWN],
            [f"telegram:100200:y{index}" for index in range(5)],
            "outcome_unknown 只告警不重放，淘汰它 = 静默丢消息",
        )
        self.assertIn("outcome_unknown=5", "\n".join(captured.output),
                      "超限必须按状态说清留下了什么")

    def test_failed_rows_are_also_kept_because_they_will_really_be_delivered(self):
        """``failed`` 到期就会**真的**被投递，淘汰它等于丢掉一条送得到的消息。"""
        database_path = self.database_path()
        builder = InboundInbox(database_path)
        for index in range(3):
            prompt = make_prompt(f"slack:C0ABCDEF:f{index}",
                                 conversation_id="slack:C0ABCDEF", platform="slack",
                                 message_id=f"f{index}")
            builder.record(prompt)
            builder.mark_failed(prompt.delivery_id, "boom")
        builder.close()

        capped = InboundInbox(database_path, max_rows=1)
        self.addCleanup(capped.close)
        capped.record(make_prompt("slack:C0ABCDEF:new", conversation_id="slack:C0ABCDEF",
                                  platform="slack", message_id="new"))

        self.assertEqual(
            [row["delivery_id"] for row in read_inbox_rows(database_path)
             if row["state"] == DeliveryState.FAILED],
            [f"slack:C0ABCDEF:f{index}" for index in range(3)],
        )

    def test_settled_rows_are_still_evicted_so_the_cap_is_not_dead(self):
        """白名单不是"什么都不删"：已了结的 ``delivered``/``abandoned`` 照旧淘汰。"""
        database_path = self.database_path()
        builder = InboundInbox(database_path)
        for index in range(2):
            prompt = make_prompt(f"irc:#ops:s{index}", conversation_id="irc:#ops",
                                 platform="irc", message_id=f"s{index}")
            builder.record(prompt)
            builder.mark_delivered(prompt.delivery_id)
        builder.close()

        with InboundInbox(database_path, max_rows=1):
            self.assertEqual(len(read_inbox_rows(database_path)), 1)

    def test_every_delivery_state_is_either_settled_or_unsettled(self):
        """每个状态都必须**主动**归类：忘了归类的将来会被静默淘汰。

        这条是白名单（fail-closed）该有的守卫。加上新状态而没往
        :data:`~opencode_bridge.inbox_row_cap.UNSETTLED_STATES` 里放，那一行既不在
        白名单里也不在未了结清单里 —— 上限会当它是可淘汰的，而没有任何测试会红。
        """
        every_state = {
            value for name, value in vars(DeliveryState).items()
            if not name.startswith("_") and isinstance(value, str)
        }
        self.assertEqual(
            every_state,
            set(_EVICTION_ORDER) | set(UNSETTLED_STATES),
            "DeliveryState 的全集必须被 _EVICTION_ORDER 与 UNSETTLED_STATES 精确瓜分",
        )


class TestLifecycleSafety(InboxTestCase):
    """关闭路径必须无害：重复关、关完再调、都不许抛。"""

    def test_creates_a_missing_parent_directory(self):
        nested = os.path.join(self._temporary_directory.name, "state", "nested")
        database_path = os.path.join(nested, "inbox.sqlite3")
        self.assertFalse(os.path.isdir(nested))

        with InboundInbox(database_path) as inbox:
            self.assertTrue(inbox.record(make_prompt("telegram:100200:16", message_id="16")))

        self.assertTrue(os.path.isfile(database_path))

    def test_close_is_idempotent(self):
        inbox, _database_path = self.open_inbox()
        inbox.record(make_prompt("telegram:100200:17", message_id="17"))

        inbox.close()
        inbox.close()

        self.assertEqual(inbox.pending_prompts(), [])

    def test_every_public_method_is_a_safe_no_op_after_close(self):
        """清理顺序出错时，退出路径不该被收件箱炸掉。"""
        inbox, _database_path = self.open_inbox()
        prompt = make_prompt("telegram:100200:18", message_id="18")
        inbox.record(prompt)
        inbox.close()

        self.assertFalse(inbox.record(prompt))
        inbox.mark_attempting(prompt.delivery_id)
        inbox.mark_pending(prompt.delivery_id)
        inbox.mark_outcome_unknown(prompt.delivery_id, "boom")
        inbox.mark_delivered(prompt.delivery_id)
        inbox.mark_failed(prompt.delivery_id, "boom")
        inbox.mark_abandoned(prompt.delivery_id, "boom")
        self.assertEqual(inbox.pending_prompts(), [])
        self.assertEqual(inbox.uncertain_prompts(), [])
        self.assertEqual(inbox.due_failed_prompts(1e12), [])
        self.assertEqual(inbox.abandoned_prompts(), [])

    def test_context_manager_closes_on_exit(self):
        database_path = self.database_path()
        with InboundInbox(database_path) as inbox:
            inbox.record(make_prompt("telegram:100200:19", message_id="19"))

        # 句柄已关，临时目录才可以被删掉（Windows 上这是 WinError 32 的来源）
        self.assertEqual(len(read_inbox_rows(database_path)), 1)

    def test_reopening_does_not_reset_a_recorded_failure(self):
        database_path = self.database_path()
        with InboundInbox(database_path) as first_run:
            first_run.record(make_prompt("telegram:100200:20", message_id="20"))
            first_run.mark_failed("telegram:100200:20", "boom")

        with InboundInbox(database_path) as second_run:
            row = _only_row(database_path)
            self.assertEqual(row["state"], DeliveryState.FAILED)
            self.assertEqual(row["attempts"], 1)
            self.assertEqual(second_run.due_failed_prompts(0.0), [])


def _only_row(database_path: str) -> dict:
    """断言只剩一行并返回它 —— 避免用例里到处写 ``rows[0]``（读起来像漏了断言）。"""
    rows = read_inbox_rows(database_path)
    if len(rows) != 1:
        raise AssertionError(f"expected exactly one inbox row, got {len(rows)}: {rows}")
    return rows[0]


class TestSplitModuleBoundaries(unittest.TestCase):
    """收件箱各模块的**边界**必须钉住 —— 搬错了地方不会让任何行为测试变红。

    为什么这些断言不是文字游戏
    --------------------------
    每一次搬移都有一个具体的失败模式，而它们**全都不会**让上面那些行为用例变红：

    * 常量被"顺手"复制回 :mod:`opencode_bridge.inbox` —— 行为全绿，而
      :mod:`opencode_bridge.inbox_recovery` 开始对着**另一份**阶梯算"还差几次预算"，
      而两份不一致只有在真的重试到分歧的那一级时才会显形；
    * 建表 / PRAGMA 在搬移时漏掉一行 —— 收件箱照样能跑，只是悄悄丢掉了耐久性或索引；
    * 阶梯与次数上限那条不变量被孤立 —— 于是没人再校验它。

    所以这里断言的是**同一份定义**与**搬移后的落点**，而不是某个具体数字。
    """

    def test_each_moved_name_has_exactly_one_definition(self):
        """每一份搬走的常量必须是**同一个对象**，不是一份拷贝。"""
        for name, owner in (
            ("BACKOFF_LADDER_SECONDS", inbox_retry_budget_module),
            ("MAX_ATTEMPTS", inbox_retry_budget_module),
            ("DEFAULT_DELIVERED_RETENTION_SECONDS", inbox_row_cap_module),
            ("DEFAULT_MAX_ROWS", inbox_row_cap_module),
        ):
            with self.subTest(name=name):
                self.assertIs(
                    getattr(inbox_module, name), getattr(owner, name),
                    f"{name} 住在 {owner.__name__}，inbox 只能转发，不能另抄一份",
                )

    def test_inbox_defines_none_of_the_moved_constants_itself(self):
        """``inbox.py`` 里不许出现这些名字的**赋值**（import 与转发不算）。"""
        defined_there = module_level_assignments(_INBOX_SOURCES["inbox"])
        for name in ("BACKOFF_LADDER_SECONDS", "MAX_ATTEMPTS",
                     "DEFAULT_DELIVERED_RETENTION_SECONDS", "DEFAULT_MAX_ROWS"):
            with self.subTest(name=name):
                self.assertNotIn(
                    name, defined_there,
                    f"{name} 的定义权已搬走；inbox.py 里重新赋值一份 = 两边各算各的预算",
                )

    def test_the_recovery_layer_reads_the_same_budget_as_the_store(self):
        """恢复层的告警文案里的 N 必须与 ``mark_failed`` 用的是同一个。"""
        self.assertIs(inbox_recovery_module.MAX_ATTEMPTS,
                      inbox_retry_budget_module.MAX_ATTEMPTS)
        self.assertIs(inbox_recovery_module.BACKOFF_LADDER_SECONDS,
                      inbox_retry_budget_module.BACKOFF_LADDER_SECONDS)

    def test_the_ladder_invariant_is_checked_at_import_time(self):
        """阶梯级数与次数上限的关系，必须有一条**会在 import 期执行**的断言守着。

        断言本身在 :mod:`opencode_bridge.inbox_retry_budget` 里；这里既验它真的存在
        （引用了那两个名字），也验它**跑得到** —— :mod:`opencode_bridge.inbox` import
        了那个模块，所以任何 import 收件箱的进程都会执行到它。
        """
        self.assertEqual(len(inbox_retry_budget_module.BACKOFF_LADDER_SECONDS),
                         inbox_retry_budget_module.MAX_ATTEMPTS - 1)
        with open(_INBOX_SOURCES["inbox_retry_budget"], encoding="utf-8") as source_file:
            tree = ast.parse(source_file.read())
        checked_by_module_level_assert = set()
        for node in tree.body:
            if isinstance(node, ast.Assert):
                checked_by_module_level_assert |= {
                    child.id for child in ast.walk(node.test)
                    if isinstance(child, ast.Name)
                }
        self.assertIn("BACKOFF_LADDER_SECONDS", checked_by_module_level_assert,
                      "那条算术不变量（阶梯级数 == 次数上限 - 1）必须有一条顶层断言守着")
        self.assertIn("MAX_ATTEMPTS", checked_by_module_level_assert)
        self.assertIn("opencode_bridge.inbox_retry_budget", sys.modules,
                      "import 收件箱必须连带 import 预算模块，否则那条断言不会执行")

    def test_the_sql_dialect_lives_in_the_sqlite_module_only(self):
        """``sqlite3.connect`` 与建表 DDL 只许出现在 :mod:`opencode_bridge.inbox_sqlite`。

        这是"搬走了什么"的机器可查版本：收件箱那一层只管把提示词变成一行，而连接的
        形状（WAL / ``synchronous`` / ``row_factory``）归基座模块。
        """
        for source_key in ("inbox", "inbox_retry_budget", "inbox_row_cap"):
            with open(_INBOX_SOURCES[source_key], encoding="utf-8") as source_file:
                text = source_file.read()
            with self.subTest(module=source_key):
                self.assertNotIn("sqlite3.connect", text,
                                 "开连接是 SQLite 基座的职责")
                self.assertNotIn("CREATE TABLE", text,
                                 "建表 DDL 是 SQLite 基座的职责")

        with open(_INBOX_SOURCES["inbox_sqlite"], encoding="utf-8") as source_file:
            substrate = source_file.read()
        self.assertIn("sqlite3.connect", substrate, "基座模块必须真的开连接")
        self.assertIn("CREATE TABLE IF NOT EXISTS inbox", substrate)
        self.assertIn("CREATE INDEX IF NOT EXISTS inbox_due", substrate)

    def test_both_pragmas_are_issued_explicitly(self):
        """两条 PRAGMA 必须**显式发出**，不能靠 SQLite 的编译默认值。

        ⚠️ 这条断言是**必须**的，且是实测出来的：只断言
        ``PRAGMA synchronous`` 的**读回值**是不够的 —— 本机这个 SQLite 的默认值
        恰好就是 FULL(2)，所以把那行 ``execute`` 删掉之后读回值**一模一样**，
        没有任何行为用例会红。可"本机默认值恰好对"不是性质：换一台默认 NORMAL 的
        机器，耐久性就悄悄降级了，而崩溃窗口那一档（``attempting``）正是靠它才查得到。

        所以这里查**源码里发出去了哪几条 PRAGMA**，而不是查生效后的值 ——
        生效值那条另有 :meth:`TestSqliteSubstrateContract.test_opened_connection_is_usable_as_documented`
        在钉，两条合起来才既"显式"又"确实生效"。
        """
        with open(_INBOX_SOURCES["inbox_sqlite"], encoding="utf-8") as source_file:
            tree = ast.parse(source_file.read())
        issued = {
            node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and node.value.strip().upper().startswith("PRAGMA")
        }
        self.assertEqual(
            issued, {"PRAGMA journal_mode=WAL", "PRAGMA synchronous=FULL"},
            "两条 PRAGMA 都必须显式发出；少了任何一条，收件箱的耐久性就依赖 SQLite "
            "的编译默认值 —— 那不是本模块能假定的性质",
        )


class TestSqliteSubstrateContract(InboxTestCase):
    """:func:`~opencode_bridge.inbox_sqlite.open_inbox_connection` 的**承重属性**。

    为什么必须钉住：这四条属性没有任何**行为**用例能观察到 ——
    ``row_factory`` 错了只是让按列名取值崩在一个离病因很远的地方，``synchronous``
    掉了会让"崩溃落在投递窗口里"这件事变得不可查，而两条路径都**照样跑绿**。
    """

    #: ``PRAGMA synchronous`` 的取值：0=OFF 1=NORMAL 2=FULL 3=EXTRA。
    _SYNCHRONOUS_FULL = 2

    def test_opened_connection_is_usable_as_documented(self):
        connection = open_inbox_connection(self.database_path("substrate.sqlite3"))
        self.addCleanup(connection.close)

        self.assertIs(connection.row_factory, sqlite3.Row,
                      "按列名取值的地方遍布收件箱与行数治理层")
        self.assertEqual(
            connection.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal",
            "崩溃时留在盘上的那行就是真相，依赖 WAL",
        )
        self.assertEqual(
            connection.execute("PRAGMA synchronous").fetchone()[0],
            self._SYNCHRONOUS_FULL,
            "synchronous 必须仍是 FULL：FULL 的开销换的是可查性",
        )
        self.assertEqual(connection.isolation_level, None,
                         "autocommit：每条语句自成一个事务，落盘与返回之间没有待 commit 的窗口")

    def test_schema_and_index_are_created(self):
        connection = open_inbox_connection(self.database_path("schema.sqlite3"))
        self.addCleanup(connection.close)
        table_names = {
            row["name"] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        index_names = {
            row["name"] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        self.assertIn("inbox", table_names)
        self.assertIn("inbox_due", index_names,
                      "按 (state, not_before) 查到期行，没索引就退化成全表扫")

    def test_the_inbox_actually_goes_through_the_substrate(self):
        """``InboundInbox`` 必须**转发**给基座模块，而不是自己再开一次连接。

        这是"原处只留调用"那条边界的机器可查版本：一旦有人把连接配置复制回
        ``inbox.py``，这条会红，而上面所有行为用例都不会。
        """
        database_path = self.database_path("delegated.sqlite3")
        with mock.patch.object(
            inbox_module, "open_inbox_connection",
            wraps=inbox_sqlite_module.open_inbox_connection,
        ) as recorded:
            with InboundInbox(database_path) as inbox:
                # 必须在 with 里面断言：出了块 close() 已把 _connection 置成 None。
                self.assertIs(inbox._connection.row_factory, sqlite3.Row,
                              "转发来的连接必须仍是配置好的那一条")
        recorded.assert_called_once_with(database_path)

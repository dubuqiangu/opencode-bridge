"""``inbox.py`` 测试：入站写前收件箱的状态机、去重与行数治理。

重点覆盖八类真实风险：

1. **写前落盘** —— 接受一条消息时它**已经**在盘上，不依赖分发成功；
2. **状态转换** —— 四个写入点各自落到对应状态（断言**盘上的行**，不只看返回值）；
3. **``INSERT OR IGNORE`` 去重** —— 同一个 ``delivery_id`` 记两次，第二次返回
   ``False`` 且**不新增行**；已送达的回执不会被重投覆盖掉；
4. **退避是期限不是 sleep** —— ``not_before`` 写进盘上，且**跨重启还在**；
5. **重试预算耗尽 → ``abandoned``** —— 绝不再排下一次重试；
6. **回执保留期** —— ``delivered`` 行过期即删，短保留期内必须留着当回执；
7. **行数上限的淘汰顺序** —— 先 ``delivered`` 再 ``abandoned``，``pending`` 永不淘汰；
8. **关闭之后仍然安全** —— ``close()`` 可重复调用，之后所有公开方法是空操作。

零真实网络、零真实 sleep。涉及"等了多久"的地方一律断言**盘上的期限**而不是真的
等：把 ``not_before`` 读出来与阶梯值比较，或直接问"在某个时刻它到期了吗"。

⚠️ 每个用例都**先关连接再删临时目录**：Windows 上一个还开着的 SQLite 句柄会让
``rmtree`` 直接失败（``WinError 32``）。这里靠 ``addCleanup`` 的 LIFO 顺序保证
（先注册临时目录、后注册 ``close``，于是 ``close`` 先跑）。
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
import unittest

from opencode_bridge.inbox import (
    BACKOFF_LADDER_SECONDS,
    DEFAULT_DELIVERED_RETENTION_SECONDS,
    DeliveryState,
    InboundInbox,
    MAX_ATTEMPTS,
    QueuedPrompt,
)
from opencode_bridge.inbox_row_cap import _EVICTION_ORDER, UNSETTLED_STATES

_REPOSITORY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: 仓库内的临时目录。存在就用它 —— 这样即使 TEMP/TMP 指向机器别处，
#: 测试也**不可能**把文件写到仓库之外。
_REPOSITORY_TEMP = os.path.join(_REPOSITORY_ROOT, ".tmp")


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

    def test_unknown_delivery_id_marks_are_silent_no_ops(self):
        """已被淘汰的行不该让清理路径炸掉。"""
        inbox, _database_path = self.open_inbox()

        inbox.mark_attempting("never-existed")
        inbox.mark_pending("never-existed")
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

    def test_the_row_cap_keeps_failed_rows_too(self):
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
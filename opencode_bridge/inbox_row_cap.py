"""入站收件箱的**行数治理**（G2）：保留期清理与行数上限。

为什么单独成文件
----------------
:class:`~opencode_bridge.inbox.InboundInbox` 负责"把一行提示词可靠地落盘"，而本模块
负责一件**完全不同**的事：盘上装了太多行时**丢哪一行**。前者是持久化，后者是策略，
两者的失效方式也不一样（前者坏了= 消息乱掉，后者坏了 = 消息消失）。混在一起时
``inbox.py`` 的物理行数会越推越高，而"行数上限该保谁"这个决定恰恰是最该被单独读懂
的一段（AGENTS.md §5）。

一条铁律：**上限只许丢已经"了结"的行
------------------------------------
"了结"= 投递结果**已定且**已记录**：

* ``delivered`` —— 成功已记录（而且它本来就是**回执**，留着只服务保留期内的重投去重）；
* ``abandoned`` —— 放弃已记录，而且 :mod:`opencode_bridge.inbox_recovery` 在启动时
  **已经**为此告警过一次。

其余三种状态都是**未了结**，每一行都还欠着用户点什么：

======================  ====================================================
状态                    丢掉它意味着什么
======================  ====================================================
``pending``             一笔还没兑现的处理义务（复现：崩溃在写前落盘之后、投递之前）
``attempting``          结果不可知的那一条**唯一**证据。它被丢掉，用户连"有 N 条状态
                        未知"这条告警都收不到 —— 消息静默消失，无失败记录、无告警
``failed``              退避期限一到就**会**被重放投递；丢掉等于丢掉一条本来能送达的
                        消息
======================  ====================================================

⚠️ ``attempting`` 那一格是本模块存在的理由：它是 :mod:`inbox_recovery` 唯一会
**告警但绝不重放**的状态，所以淘汰它不会换来任何"补偿"，只会把一次本可以发现的
不确定变成彻底的静默。

超出上限就**超着**，并且必须留下痕迹
------------------------------------
无可淘汰的行时本模块**不会**硬凑一行删掉（那正是 :data:`UNSETTLED_STATES` 存在的
理由）。它改为**如实报告**：保留了多少行、超了多少、按状态分别是多少。

⚠️ 这里**刻意不抛异常**：本模块由 :meth:`InboundInbox.record` 在入站热路径上调用，
而 ``record`` 位于 ``on_inbound`` 的写前义务里 —— 让"磁盘满了/行数超了"这种运维状况
去掀翻入站路径，代价（丢一整批正在进来的消息）远大于超限本身。既有先例也是这么定的：
``pending`` 超限一直是"告警并保留"，本模块只是把那条规则推广到**全部**未了结状态。

先淘汰回执，再淘汰终态
----------------------
同为已了结，回执比终态更该先走：``delivered`` 只服务保留期内的重投去重（超过保留期
本来就由 :func:`prune_expired_delivered` 清掉），而 ``abandoned`` 是用户在状态输出里
唯一能看见"它试过、放弃了"的凭据。
"""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from dataclasses import dataclass, field

from .inbox_states import DeliveryState

logger = logging.getLogger("opencode_bridge.inbox")

__all__ = [
    "DEFAULT_DELIVERED_RETENTION_SECONDS",
    "DEFAULT_MAX_ROWS",
    "EvictionReport",
    "UNSETTLED_STATES",
    "evict_beyond_row_cap",
    "prune_expired_delivered",
]

#: ``delivered`` 行的保留时长（秒）。取 24 小时是为了**当回执用** ——
#: 平台在保留期内重投，靠的就是这行去重（和 Telegram 自己那份未确认更新的保留期一致）。
#:
#: ⚠️ 从 :mod:`opencode_bridge.inbox` 搬来：它就是下面 :func:`prune_expired_delivered`
#: 的那个 keyword 参数的默认值，放在**用它的函数**旁边，而不是放在收件箱的构造签名里
#: 让人从三层之外找过来。
DEFAULT_DELIVERED_RETENTION_SECONDS: float = 86400.0

#: 总行数上限。入站量是人的量级（每分钟几条），500 行足够覆盖很长的故障期；
#: 它是**兜底**而不是常规路径，真触发时优先牺牲终态行。
DEFAULT_MAX_ROWS: int = 500

#: **已了结**的状态，按淘汰优先级排列（数字小的先走）。
#:
#: ⚠️ 这是一份**白名单**，不是黑名单 —— 这就是 :func:`evict_beyond_row_cap` 只删
#: 列表内状态的原因。写成"排除掉不能删的"会让**任何新加的状态默认可淘汰**（fail-open）：
#: 上游已经踩过一次了，那次是 ``ELSE 4`` 分支把一个刚被移出优先级的状态顺手兜住并
#: 淘汰掉，于是"把它从优先级表里拿掉"这个看起来正确的修法其实**一点用都没有**。
#: 白名单是 fail-closed：将来加状态的人必须**主动**决定它能不能被丢。
_EVICTION_ORDER: tuple[str, ...] = (
    DeliveryState.DELIVERED,
    DeliveryState.ABANDONED,
)

#: **未了结**的状态：盘上每一行都还欠着用户点什么，上限无权丢掉它们。
#:
#: 与 :data:`_EVICTION_ORDER` 互为补集 —— 两者相并是 :class:`~opencode_bridge.inbox.DeliveryState`
#: 的全集。这条不变量由 ``tests/test_inbox.py`` 的
#: ``test_every_delivery_state_is_either_settled_or_unsettled`` 钉住：新增状态忘了归类，
#: 那条测试会红。
UNSETTLED_STATES: tuple[str, ...] = (
    DeliveryState.PENDING,
    DeliveryState.ATTEMPTING,
    DeliveryState.FAILED,
)


@dataclass
class EvictionReport:
    """一次上限治理的**如实报告**（不抛异常，见模块开头）。

    存在的理由是"什么都不做"与"悄悄删掉"必须可区分：调用方要能一眼看出这次到底
    丢了什么、留下了什么。

    ⚠️ 可变（不是 ``frozen``）：它是**执行后回填**的记录，:func:`evict_beyond_row_cap`
    先拿到上限与初值、再逐步填 ``rows_after`` / ``kept``。写成 frozen 会让那条路径
    直接抛 :class:`dataclasses.FrozenInstanceError` —— 它只是个值对象，不该被当成
    需要不可变保证的东西。
    """

    #: 上限是多少行。由 :func:`evict_beyond_row_cap` 填。
    cap: int = 0
    #: 真的被删掉的行，按状态计数（只可能出现 :data:`_EVICTION_ORDER` 里的状态）。
    evicted: Counter = field(default_factory=Counter)
    #: 压回上限之后仍在盘上的总行数。
    rows_after: int = 0
    #: 压回上限之后仍留在盘上的，按状态计数。
    kept: Counter = field(default_factory=Counter)

    @property
    def evicted_rows(self) -> int:
        return sum(self.evicted.values())

    @property
    def over_cap_by(self) -> int:
        """仍然超出上限多少行。``0`` 表示上限已满足。"""
        return max(0, self.rows_after - self.cap)

    @property
    def kept_unsettled(self) -> Counter:
        """保留下来的**未了结**行 —— 超限时告警要报的就是它们。"""
        return Counter(
            {state: number for state, number in self.kept.items()
             if state in UNSETTLED_STATES}
        )


def prune_expired_delivered(
    connection: sqlite3.Connection, *, delivered_retention_seconds: float, moment: float,
) -> int:
    """删掉超过保留期的 ``delivered`` 回执，返回删掉几行。

    ``abandoned`` **不在这里删** —— 它是终态，要留在状态输出里看得见；它由行数上限
    兜底（见 :func:`evict_beyond_row_cap`）。
    """
    cursor = connection.execute(
        "DELETE FROM inbox WHERE state = ? AND updated_at <= ?",
        (DeliveryState.DELIVERED, moment - delivered_retention_seconds),
    )
    return int(cursor.rowcount)


def evict_beyond_row_cap(connection: sqlite3.Connection, *, max_rows: int) -> EvictionReport:
    """把总行数尽量压回 ``max_rows``，**只许丢已了结的行**（见模块开头）。

    无可淘汰的行时**不硬凑**、也不抛异常：如实返回 :class:`EvictionReport`，由调用方
    告警。宁可超限（代价是磁盘）也不静默丢一条用户消息。
    """
    report = EvictionReport(cap=max_rows)
    report.rows_after = _row_count(connection)
    report.kept = _count_states(connection)
    surplus = report.rows_after - max_rows
    if surplus <= 0:
        return report

    # ⛔ 用**具名**占位符而不是一串位置 ``?``：这条语句里同一批状态值出现**两次**
    # （``WHERE state IN (...)`` 与 ``CASE state WHEN ...``），位置占位符要传两遍、
    # 顺序错了也不报错。上一版就是这么写的，实测报
    # ``Incorrect number of bindings supplied``。具名参数让"同一批值填两处"变成
    # 一行、且 sqlite3 自己会检查名字对不对。
    binding_names = [_binding_name(position) for position in range(len(_EVICTION_ORDER))]
    priority_clause = " ".join(
        "WHEN :%s THEN %d" % (binding_name, priority)
        for priority, binding_name in enumerate(binding_names)
    )
    parameters: dict[str, object] = {
        binding_name: state
        for binding_name, state in zip(binding_names, _EVICTION_ORDER)
    }
    parameters["surplus"] = surplus
    victims = connection.execute(
        "SELECT delivery_id, state FROM inbox WHERE state IN (%s)"
        " ORDER BY CASE state %s ELSE 99 END, updated_at ASC, delivery_id ASC"
        " LIMIT :surplus"
        % (",".join(":" + binding_name for binding_name in binding_names),
           priority_clause),
        parameters,
    ).fetchall()
    if victims:
        connection.executemany(
            "DELETE FROM inbox WHERE delivery_id = ?",
            [(victim["delivery_id"],) for victim in victims],
        )
    report.evicted = Counter(victim["state"] for victim in victims)
    report.rows_after = _row_count(connection)
    report.kept = _count_states(connection)
    return report


def _row_count(connection: sqlite3.Connection) -> int:
    return int(connection.execute("SELECT COUNT(*) FROM inbox").fetchone()[0])


def _binding_name(position: int) -> str:
    """淘汰语句里第 ``position`` 个状态的具名占位符名。

    ⚠️ 刻意**不**用状态字符串本身当占位符名（``:delivered``）：状态名是这一层的
    **数据**，而 sqlite3 的具名参数会原样拼进 SQL 文本 —— 将来若状态名带一个奇怪
    字符，那是要去改 SQL 的 bug。序号 + 一个固定前缀与数据无关。
    """
    return "evictable_%d" % position


def _count_states(connection: sqlite3.Connection) -> Counter:
    """盘上每一行按状态的计数 —— 上限触发时要**按状态**说清楚留下了什么。"""
    return Counter(
        row["state"]
        for row in connection.execute("SELECT state FROM inbox").fetchall()
    )


def report_eviction(report: EvictionReport, *, inbox_path: str) -> None:
    """把一次治理结果写进日志：删了什么，以及**为什么没删更多**。

    第二句是承重的：超限时若只有一句"已淘汰 N 行"，读者无从判断是不是漏了消息；
    明确写出"保留了哪些未了结的行、超了多少"，超限才是**可诊断**的。
    """
    if report.evicted_rows:
        logger.debug(
            "inbox %s: evicted %d settled row(s) to stay under the %d-row cap (%s)",
            inbox_path, report.evicted_rows, report.cap, _describe(report.evicted),
        )
    if report.over_cap_by <= 0:
        return
    logger.warning(
        "inbox %s: %d row(s) still exceed the %d-row cap and were kept (%s);"
        " the cap will not drop unsettled rows",
        inbox_path, report.over_cap_by, report.cap,
        _describe(report.kept_unsettled) if report.kept_unsettled
        else _describe(report.kept),
    )


def _describe(counts: Counter) -> str:
    """把按状态的计数写成人能读的一串（计数为 0 的状态不出现）。"""
    if not counts:
        return "no rows left"
    return ", ".join("%s=%d" % (state, number) for state, number in sorted(counts.items()))

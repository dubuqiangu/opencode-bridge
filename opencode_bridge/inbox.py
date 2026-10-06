"""入站写前收件箱（G2）：崩溃落在投递窗口里时，消息既不丢、副作用也不重复。

要修的静默丢失
--------------
本桥刻意选 at-most-once：适配器**先推进内存游标、再分发**。这条铁律本身是对的
（避免同一条消息被处理两次），代价只有一个::

    进程死在「游标已推进」与「成功 prompt 到 opencode」之间
      -> 那条消息永久消失，连一行日志都没有

本模块把那条消息在**分发之前**落盘，于是平台的红投 / 丢弃策略变得**无关** ——
我们手上已经有自己那份副本。崩溃后由 :mod:`opencode_bridge.inbox_recovery`
从这份副本重放。

为什么是「写前」而不是「失败队列」
--------------------------------
失败队列只在 ``prompt()`` **抛异常**时留下痕迹，而真正静默的恰恰是崩溃：
没有任何代码路径跑得到「记一笔」。写前落盘把两类失败统一成同一件事 ——
磁盘上有一行 ``pending``。

五次写入，一一对应四个状态
--------------------------
=========================  ====================  ==========================
调用                       落盘的状态             为什么在这个位置
=========================  ====================  ==========================
:meth:`InboundInbox.record`  ``pending``            分发之前 —— 写前义务
:meth:`InboundInbox.mark_attempting`  ``attempting``  紧贴 ``prompt()`` 之前
:meth:`InboundInbox.mark_delivered`  ``delivered``    **仅在成功之后**
:meth:`InboundInbox.mark_failed`     ``failed``       明确失败（可能自动转终态）
:meth:`InboundInbox.mark_abandoned`  ``abandoned``    调用方自己判定预算耗尽
=========================  ====================  ==========================

落在 ``attempting`` 与 ``delivered`` 之间的崩溃是**唯一结果不可知**的窗口 ——
opencode 可能已经处理了，也可能没有。这类行只告警、绝不重放（用户 2026-10-03 拍板，
理由：下游是有副作用的 coding agent，重复执行副作用未必比丢一条消息轻）。

"丢了算谁的" —— 行数上限的规矩在 :mod:`opencode_bridge.inbox_row_cap`
------------------------------------------------------------------
盘上装不下时丢哪一行，是一件**独立于持久化**的决定，所以它住在自己的模块里。
那里有一条铁律：**上限只许丢已经"了结"的行**（``delivered`` / ``abandoned``），
其余三种状态每一行都还欠着用户点什么 —— 其中 ``attempting`` 最要紧：
:mod:`~opencode_bridge.inbox_recovery` 对它是**只告警、绝不重放**，所以淘汰它
换不来任何补偿，只会把一次本可以发现的"不确定"变成彻底的静默丢失。

``attempting`` 的**第五个**出口：409（会话忙）
------------------------------------------
409 只有在 ``prompt()`` **返回之后**才认得，而 ``attempting`` 必须紧贴那个调用
**之前**写 —— 两个都不可协商。于是"服务端拒收、agent 压根没跑过"这个**已知**的
结果一度没有任何可落盘的状态：那一行会永远停在 ``attempting``，被恢复层按"结果
不可知"只告警、绝不重放，而**平台重投也被去重挡掉**（``INSERT OR IGNORE`` 让那一行
成了一份"已处理"的回执，尽管消息从未送达）。:meth:`InboundInbox.mark_pending`
就是这条缺失的转移，它把已知的拒收退回"从未尝试过"。

幂等靠 ``INSERT OR IGNORE``
--------------------------
平台把一条**已送达**的消息重投回来时，第二次 :meth:`InboundInbox.record` 必须是
**静默的** no-op —— 留下的那行**就是回执**。所以这里是 ``INSERT OR IGNORE``，
不是 ``INSERT OR REPLACE``：后者会抹掉回执，让重投再跑一遍 agent。

三种结局，不是"成功 / 失败"
---------------------------
:meth:`InboundInbox.record` 返回 :class:`RecordOutcome`：**新行** / **去重命中** /
**收件箱已关**。

⚠️ 第三个曾经与第二个**共用一个返回值**（都是 ``False``）⇒ 关停期间掉下来的消息被报成
「平台重投了一条我们已有的消息」，**与事实相反**：当时唯一生产调用方
(:func:`opencode_bridge.inbound_gateway._record_inbound`) 只按真假分流、没有第三个分支。
⇒ 两侧现在都对得上：收件箱这一侧**可分辨**（:class:`RecordOutcome`），真话由"已关"那一档
**自己**记一条 warning（理由与级别判据见 :meth:`record`），而调用方**按 CLOSED 单独分流**、
不再把原因说反（它那句 info 说的是「没投递」这个投递侧的结论，级别仍归收件箱那一档）。

⚠️ 两个"没落盘"的结局都仍是**假值**：调用方那一侧除"已关"之外仍按真假分流，而让"已关"为真
会让它在**盘上没有任何回执**的情况下把消息投出去 —— 那正是本模块要防的事。

退避阶梯与"还差几次预算"住在 :mod:`opencode_bridge.inbox_retry_budget`
-------------------------------------------------------------------
阶梯与次数上限在那个模块里，而 :meth:`InboundInbox.mark_failed` 仍在本文件里**算**它
—— 因为 ``not_before`` 是要写进盘上的一列，判断必须与那次 UPDATE 在同一条连接上。
拆分的是"常量与它们之间的关系"，不是那个判断。

本模块**只剩一件事**：把一条提示词写成一行、再把一行读回去 —— 写前落盘的状态机、
恢复层要的四个读取入口、以及连接与锁的生命周期。另外四块各有归属，各自带一份
"从哪儿搬来的、为什么搬"的说明：:mod:`opencode_bridge.inbox_states`（状态词汇表）、
:mod:`opencode_bridge.inbox_retry_budget`（阶梯 / 次数上限）、
:mod:`opencode_bridge.inbox_sqlite`（建表 DDL 与连接配置）、
:mod:`opencode_bridge.inbox_row_cap`（保留期、行数上限，以及"盘上装不下时丢哪一行"）。
"""

from __future__ import annotations

import enum
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Optional

from .inbox_row_cap import (
    DEFAULT_DELIVERED_RETENTION_SECONDS as DEFAULT_DELIVERED_RETENTION_SECONDS,
    DEFAULT_MAX_ROWS as DEFAULT_MAX_ROWS,
    evict_beyond_row_cap,
    prune_expired_delivered,
    report_eviction,
)

# ⚠️ ``DeliveryState`` 的定义在 :mod:`opencode_bridge.inbox_states`，**不在这里** ——
# 行数治理那一层也要读它（判"这个状态算不算了结"），住在任一边都会成环（实测过）。
# 这里 import 之后**再导出**，是为了不动既有的那一百多处
# ``from opencode_bridge.inbox import DeliveryState``。
from .inbox_states import DeliveryState as DeliveryState

# ⚠️ 阶梯与次数上限的定义在 :mod:`opencode_bridge.inbox_retry_budget`：恢复层
# :mod:`opencode_bridge.inbox_recovery` 的告警文案也要读 :data:`MAX_ATTEMPTS`，两边
# 必须共用**同一个**答案。转发而不是各自定义，是为了不动既有的
# ``from opencode_bridge.inbox import BACKOFF_LADDER_SECONDS``（``inbox_recovery``
# 至今仍从本模块 import 这两个名字）。
#
# ⚠️ 别把下面这两行改成"去模块上取"（``inbox_retry_budget.BACKOFF_...``）：
# ``tests/test_inbox_wiring.py`` 用 ``mock.patch.object(inbox_module,
# "BACKOFF_LADDER_SECONDS", ...)`` 把本模块的全局量旁路成 0，而
# :meth:`InboundInbox.mark_failed` 读的正是这里绑定的这个名字。
from .inbox_retry_budget import BACKOFF_LADDER_SECONDS, MAX_ATTEMPTS

from .inbox_sqlite import open_inbox_connection

__all__ = [
    "BACKOFF_LADDER_SECONDS",
    "DEFAULT_DELIVERED_RETENTION_SECONDS",
    "DEFAULT_MAX_ROWS",
    "DeliveryState",
    "InboundInbox",
    "MAX_ATTEMPTS",
    "QueuedPrompt",
    "RecordOutcome",
]

logger = logging.getLogger("opencode_bridge.inbox")


@dataclass(frozen=True)
class QueuedPrompt:
    """一条已落盘、待（或正在）交给 opencode 的入站提示词。

    刻意**只有**投递所需字段：没有 ``attempts``、没有时间戳。恢复层要判断
    "还能不能重试"时用 :meth:`InboundInbox.mark_failed` 里的同一套预算规则，
    不靠它自己数。
    """

    #: 去重键。平台有 message id 就用它，没有就用适配器自己拼的稳定键。
    delivery_id: str
    conversation_id: str
    platform: str
    #: 平台侧消息 id；拿不到时为 ``None``。
    message_id: Optional[str]
    #: **原样**交给 agent 的正文。恢复层绝不许往这里塞任何前缀 ——
    #: 那段文字会进入 agent 的上下文，污染它可能改变 agent 的行为。
    text: str


def _prompt_from_row(row: sqlite3.Row) -> QueuedPrompt:
    """把一行数据库记录还原成 :class:`QueuedPrompt`（``message_id`` 可能是 NULL）。"""
    return QueuedPrompt(
        delivery_id=row["delivery_id"],
        conversation_id=row["conversation_id"],
        platform=row["platform"],
        message_id=row["message_id"],
        text=row["text"],
    )


class RecordOutcome(enum.Enum):
    """:meth:`InboundInbox.record` 的三个结局 —— 写前义务履行到哪一步。

    ⚠️ **为什么是三个值而不是一个 ``bool``**：旧实现里「收件箱已关」与「去重命中」
    **共用**同一个返回值 ``False``，而唯一生产调用方
    (:func:`opencode_bridge.inbound_gateway._record_inbound`) 只按真假分流
    ⇒ 关停期间掉下来的消息被报成「平台重投了一条我们已有的消息」，
    **与事实相反**。让两个"没落盘"的结局可分辨，调用方就不必靠猜 ——
    它现在**先按 :attr:`CLOSED` 单独分流**（说真话、且仍然不投递），其余才按真假。

    :meth:`InboundInbox.record` 是**唯一**返回它的方法 —— 其余写入口都返回 ``None``，
    没有返回值可误报。
    """

    #: 新行已落盘。**真值** —— 这条消息带上了回执，可以去投递。
    RECORDED = "recorded"
    #: 去重命中：同一个 ``delivery_id`` 已经在收件箱里。**假值**，而**不是**错误。
    DUPLICATE = "duplicate"
    #: ``close()`` 已经跑过，**什么都没写**。**假值**（关停侧行为不变），
    #: 但与 :attr:`DUPLICATE` **可分辨**：这一条没有回执，**永远不会被重放**。
    CLOSED = "closed"

    def __bool__(self) -> bool:
        """只有「已落盘」为真 —— 两个「没落盘」的结局都让按真假分流的调用方不投递。

        ⚠️ 这是**向后兼容**那一半，不是"真值即成功"的漂亮说法：
        :attr:`CLOSED` 必须为假，否则调用方会在**盘上没有任何回执**的情况下把消息
        投出去，而本模块存在的理由恰恰是"回执先于投递"。
        """
        return self is RecordOutcome.RECORDED


class InboundInbox:
    """落盘的入站收件箱。**只做持久化，不含投递策略。**

    线程模型：所有公开方法都在一把 ``RLock`` 下操作**同一条** SQLite 连接
    （``check_same_thread=False``），因为轮询线程与主线程都会碰它。

    耐久性：``journal_mode=WAL`` + ``synchronous=FULL``，且连接开在 autocommit
    模式（``isolation_level=None``）—— 每条语句自己就是一次事务，于是
    "落盘成功" 与 "方法返回" 之间不存在一个需要 commit 的窗口。

    ``close()`` 之后所有公开方法都是**安全的空操作**（不抛、不崩）：
    清理顺序出错不该让桥在退出路径上炸掉。

    ⚠️ 但"空操作"**不等于"沉默"**：:meth:`record` 仍会记一条 ``warning``，因为它有一个
    **返回值** —— 「没落盘因为收件箱已关」与「没落盘因为去重」必须能被分辨，
    而关停窗口里的消息丢了就该有人听见（判据见 :meth:`record` 与 :class:`RecordOutcome`）。
    """

    def __init__(
        self,
        path: str,
        *,
        delivered_retention_seconds: float = DEFAULT_DELIVERED_RETENTION_SECONDS,
        max_rows: int = DEFAULT_MAX_ROWS,
    ) -> None:
        self._path = os.fspath(path)
        self._delivered_retention_seconds = float(delivered_retention_seconds)
        self._max_rows = max(1, int(max_rows))
        self._lock = threading.RLock()
        self._connection: Optional[sqlite3.Connection] = open_inbox_connection(self._path)
        # 启动时清一次：上一次运行的 delivered 回执可能早就过期了，
        # 上一次运行留下的行也可能已经超了上限。
        self._prune_expired_delivered(time.time())
        self._enforce_row_cap()

    # ------------------------------------------------------------------
    # 写前义务
    # ------------------------------------------------------------------
    def record(self, prompt: QueuedPrompt) -> RecordOutcome:
        """把一条入站消息**在分发之前**落盘。返回 :class:`RecordOutcome`。

        * :attr:`~RecordOutcome.RECORDED` —— 新行（**真值**，既有契约里的 ``True``）；
        * :attr:`~RecordOutcome.DUPLICATE` —— 去重命中：同一个 ``delivery_id`` 已经在收件箱里
          （可能已经 ``delivered``），这条重投不该再跑一遍 agent。它**不是错误**
          （既有契约里的 ``False``）。注意这里保留**原来那一行**，包括它的状态与正文 ——
          已送达的回执不能被重投覆盖掉；
        * :attr:`~RecordOutcome.CLOSED` —— ``close()`` 已经跑过，**什么都没写**。

        ⚠️ 后两者都是**假值**：``_record_inbound`` 先按 :attr:`~RecordOutcome.CLOSED`
        分流，剩下那一侧仍按真假；而 ``CLOSED`` 若为真，它会在**盘上没有任何回执**的
        情况下把消息投出去 —— 绕过写前义务正是本模块要防的。
        「关掉之后不再投递」是**设计**（``close()`` 可重复调用，之后所有公开方法都是空操作），
        本方法与调用方都只把**误报的方向**纠正过来，不改这一侧的行为。

        ⚠️ 而两个假值**必须可分辨**（旧实现两者都是 ``False``）：``CLOSED`` 这一档现在
        **自己**记一条 ``warning``，说清真话（连接已关、这条不在盘上、永远不会被重放）。
        级别判据：不是 ``info``（无失败记录、无告警的一次丢失，且本模块对同型事件
        ——不可重放的状态被淘汰——用的就是 warning，见 :func:`.inbox_row_cap.report_eviction`）；
        不是 ``error``（关停是**预期**事件，打 error 会把真错误淹掉）；不是 ``debug``
        （关停窗口很窄，默认级别下没人会看见）。
        """
        with self._lock:
            if self._connection is None:
                logger.warning(
                    "inbox %s: refused to record %s because the connection is already"
                    " closed (shutdown in progress); it is NOT on disk, so"
                    " opencode_bridge.inbox_recovery can never replay it — a message"
                    " dropped here is lost, not deduplicated",
                    self._path, prompt.delivery_id,
                )
                return RecordOutcome.CLOSED
            moment = time.time()
            cursor = self._connection.execute(
                "INSERT OR IGNORE INTO inbox (delivery_id, conversation_id, platform,"
                " message_id, text, state, attempts, not_before, last_error,"
                " created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 0, 0, NULL, ?, ?)",
                (
                    prompt.delivery_id,
                    prompt.conversation_id,
                    prompt.platform,
                    prompt.message_id,
                    prompt.text,
                    DeliveryState.PENDING,
                    moment,
                    moment,
                ),
            )
            if cursor.rowcount != 1:
                return RecordOutcome.DUPLICATE
            self._prune_expired_delivered(moment)
            self._enforce_row_cap()
            return RecordOutcome.RECORDED

    def mark_attempting(self, delivery_id: str) -> None:
        """标记"投递已经开始"，紧贴 ``prompt()`` 调用之前。

        这一行是**结果不可知**的唯一来源，所以它必须尽可能贴近真正发出去的那一刻：
        写在它之前，崩溃就落在"还没试"（可以放心重放）；写在它之后，崩溃就落在
        "可能已经跑过 agent"（只能告警）。
        """
        with self._lock:
            self._write(
                "UPDATE inbox SET state = ?, not_before = 0, updated_at = ?"
                " WHERE delivery_id = ?",
                (DeliveryState.ATTEMPTING, time.time(), delivery_id),
            )

    def mark_pending(self, delivery_id: str) -> None:
        """退回 ``pending``：**服务端明确拒收**，agent 没跑过，可以放心重放。

        唯一调用点是 :meth:`~opencode_bridge.inbound_gateway.InboundGateway._dispatch_prompt`
        的 409 分支，而那里紧跟在 :meth:`mark_attempting` 之后 —— 调用方必须保证
        这一行当前是 ``attempting``（本方法与既有转移一样**不**带状态守卫，
        守卫只会让顺序写错的调用方静默什么都没做）。

        规则与既有转移同源，不新造一套：

        * **不碰** ``attempts``：409 不是失败，不烧重试预算。预算只由
          :meth:`mark_failed` 一处消耗，所以"退回待投递"不会让它凭空多出机会；
        * ``not_before`` 清零（与 :meth:`mark_attempting` / :meth:`mark_delivered`
          一样），让这一行立刻可重放；
        * **不碰** ``last_error``：409 本身不是失败，所以它既不该覆盖上一次真正
          的失败原因，也不该把它抹掉（同 :meth:`mark_attempting`）。

        ⚠️ at-most-once 不受影响，这是**放宽**不是**削弱**：409 意味着服务端拒收，
        副作用一次都没发生，重放不可能重复。改之前那条消息**永远送不到**，
        改之后它在下次启动送达 —— 两边都不存在重复投递。
        """
        with self._lock:
            self._write(
                "UPDATE inbox SET state = ?, not_before = 0, updated_at = ?"
                " WHERE delivery_id = ?",
                (DeliveryState.PENDING, time.time(), delivery_id),
            )

    def mark_delivered(self, delivery_id: str) -> None:
        """标记投递成功。**只有** ``prompt()`` 真的成功才该调用。"""
        with self._lock:
            self._write(
                "UPDATE inbox SET state = ?, not_before = 0, last_error = NULL,"
                " updated_at = ? WHERE delivery_id = ?",
                (DeliveryState.DELIVERED, time.time(), delivery_id),
            )

    def mark_failed(self, delivery_id: str, error: str) -> None:
        """标记一次明确的投递失败，并安排下一次重试（或直接放弃）。

        ``attempts`` 在**这里** +1 而不是 :meth:`mark_attempting`：它记的是
        "已花掉的重试预算"。这样即便调用方漏掉 ``mark_attempting``，一行也**不可能
        无限重试下去** —— 每次失败都严格消耗一级预算。

        放弃的条件是 ``attempts >= MAX_ATTEMPTS``，也就是**绝不花掉最后一次预算重试**
        （与 hermes 同一条约束，理由也一样）：任何时长的事故都可能比任何定时器活得久，
        而一条被定时器放弃的行就永远没了。转成终态 + 一次告警，比再排一次重试更容易
        让人发现。
        """
        with self._lock:
            spent = self._read_attempts(delivery_id)
            if spent is None:
                return  # 未知 delivery_id：已经被淘汰了，静默忽略
            attempts = spent + 1
            moment = time.time()
            if attempts >= MAX_ATTEMPTS:
                self._write(
                    "UPDATE inbox SET state = ?, attempts = ?, not_before = 0,"
                    " last_error = ?, updated_at = ? WHERE delivery_id = ?",
                    (DeliveryState.ABANDONED, attempts, error, moment, delivery_id),
                )
                logger.warning(
                    "inbox %s: giving up after %d attempt(s); last error: %s",
                    delivery_id, attempts, error,
                )
                return
            # 下标是「第几次失败」减一：attempts=1 -> 第 0 级（30s），
            # attempts=2 -> 第 1 级（120s）。attempts >= MAX_ATTEMPTS 的情况
            # 已在上面转成 abandoned，所以这里一定落在阶梯范围内。
            ladder_index = attempts - 1
            not_before = moment + BACKOFF_LADDER_SECONDS[ladder_index]
            self._write(
                "UPDATE inbox SET state = ?, attempts = ?, not_before = ?,"
                " last_error = ?, updated_at = ? WHERE delivery_id = ?",
                (DeliveryState.FAILED, attempts, not_before, error, moment, delivery_id),
            )

    def mark_abandoned(self, delivery_id: str, error: str) -> None:
        """强制转终态 ``abandoned``（调用方自己判定预算耗尽时用）。"""
        with self._lock:
            self._write(
                "UPDATE inbox SET state = ?, not_before = 0, last_error = ?,"
                " updated_at = ? WHERE delivery_id = ?",
                (DeliveryState.ABANDONED, error, time.time(), delivery_id),
            )

    # ------------------------------------------------------------------
    # 读取（恢复层与状态输出用）
    # ------------------------------------------------------------------
    def pending_prompts(self) -> list[QueuedPrompt]:
        """从未尝试过的行。重启后可以**放心重放**（不可能重复）。"""
        return self._prompts_in_state(DeliveryState.PENDING)

    def uncertain_prompts(self) -> list[QueuedPrompt]:
        """结果不可知的行（崩溃落在投递过程中）。**只告警，绝不重放。**"""
        return self._prompts_in_state(DeliveryState.ATTEMPTING)

    def due_failed_prompts(self, now: float) -> list[QueuedPrompt]:
        """明确失败过、且退避期限已经到期的行。"""
        with self._lock:
            if self._connection is None:
                return []
            rows = self._connection.execute(
                "SELECT delivery_id, conversation_id, platform, message_id, text"
                " FROM inbox WHERE state = ? AND not_before <= ?"
                " ORDER BY created_at ASC, delivery_id ASC",
                (DeliveryState.FAILED, float(now)),
            ).fetchall()
        return [_prompt_from_row(row) for row in rows]

    def abandoned_prompts(self) -> list[QueuedPrompt]:
        """重试预算耗尽的行。终态，留着是为了状态输出里还看得见它们。"""
        return self._prompts_in_state(DeliveryState.ABANDONED)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def close(self) -> None:
        """关掉连接。可重复调用；关掉之后所有公开方法都是空操作。"""
        with self._lock:
            connection, self._connection = self._connection, None
            if connection is None:
                return
            try:
                connection.close()
            except sqlite3.Error as exc:
                logger.warning("inbox %s: closing the connection failed: %s", self._path, exc)

    def __enter__(self) -> "InboundInbox":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 内部：写入口、行数治理
    # ------------------------------------------------------------------
    def _write(self, statement: str, parameters: tuple) -> None:
        """所有写操作的唯一入口：先判"是否已关闭"，再执行。"""
        if self._connection is None:
            return
        self._connection.execute(statement, parameters)

    def _read_attempts(self, delivery_id: str) -> Optional[int]:
        """读一行已花掉的预算；行不存在时返回 ``None``（区分"0 次"与"没有这行"）。"""
        if self._connection is None:
            return None
        row = self._connection.execute(
            "SELECT attempts FROM inbox WHERE delivery_id = ?", (delivery_id,)
        ).fetchone()
        return None if row is None else int(row[0])

    def _prompts_in_state(self, state: str) -> list[QueuedPrompt]:
        with self._lock:
            if self._connection is None:
                return []
            rows = self._connection.execute(
                "SELECT delivery_id, conversation_id, platform, message_id, text"
                " FROM inbox WHERE state = ?"
                " ORDER BY created_at ASC, delivery_id ASC",
                (state,),
            ).fetchall()
        return [_prompt_from_row(row) for row in rows]

    def _prune_expired_delivered(self, moment: float) -> None:
        """删掉超过保留期的 ``delivered`` 回执（策略在 :mod:`.inbox_row_cap`）。"""
        with self._lock:
            if self._connection is None:
                return
            prune_expired_delivered(
                self._connection,
                delivered_retention_seconds=self._delivered_retention_seconds,
                moment=moment,
            )

    def _enforce_row_cap(self) -> None:
        """把总行数压回上限，**只许丢已了结的行**（策略见 :mod:`.inbox_row_cap`）。

        无可淘汰的行时**不硬凑**：超限就超着并如实告警，把"保住了哪些行"写进日志。
        不可重放的状态（``pending`` / ``attempting`` / ``failed``）被静默淘汰，
        就是一次无失败记录、无告警的消息丢失。
        """
        with self._lock:
            if self._connection is None:
                return
            report = evict_beyond_row_cap(self._connection, max_rows=self._max_rows)
            report_eviction(report, inbox_path=self._path)
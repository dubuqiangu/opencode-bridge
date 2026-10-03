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

四次写入，一一对应四个状态
--------------------------
=========================  ====================  ==========================
调用                       落盘的状态             为什么在这个位置
=========================  ====================  ==========================
:meth:`InboundInbox.record`  ``pending``            分发之前 —— 写前义务
:meth:`InboundInbox.mark_attempting`  ``attempting``  紧贴 ``prompt()`` 之前
:meth:`InboundInbox.mark_delivered`  ``delivered``    **仅在成功之后**
:meth:`InboundInbox.mark_failed`     ``failed``       明确失败（可能自动转终态）
=========================  ====================  ==========================

落在 ``attempting`` 与 ``delivered`` 之间的崩溃是**唯一结果不可知**的窗口 ——
opencode 可能已经处理了，也可能没有。这类行只告警、绝不重放（用户 2026-10-03 拍板，
理由：下游是有副作用的 coding agent，重复执行副作用未必比丢一条消息轻）。

幂等靠 ``INSERT OR IGNORE``
--------------------------
平台把一条**已送达**的消息重投回来时，第二次 :meth:`InboundInbox.record` 必须是
**静默的** no-op —— 留下的那行**就是回执**。所以这里是 ``INSERT OR IGNORE``，
不是 ``INSERT OR REPLACE``：后者会抹掉回执，让重投再跑一遍 agent。

退避阶梯为什么住在这一层
------------------------
:meth:`InboundInbox.mark_failed` 要写出 ``not_before``，所以"还差几次预算"这个判断
必须发生在这里 —— 否则恢复层唯一能看到的东西 :class:`QueuedPrompt` 不带 ``attempts``，
恢复层无从判断该不该放弃。这个状态就是一行数据库记录，跨进程重启后必须还在，
所以它属于持久化层而不是策略层。
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Optional

__all__ = [
    "BACKOFF_LADDER_SECONDS",
    "DEFAULT_DELIVERED_RETENTION_SECONDS",
    "DEFAULT_MAX_ROWS",
    "DeliveryState",
    "InboundInbox",
    "MAX_ATTEMPTS",
    "QueuedPrompt",
]

logger = logging.getLogger("opencode_bridge.inbox")

#: 重试退避阶梯（秒）。固定阶梯、**不用指数**，与 hermes-agent 的
#: ``gateway/delivery_ledger.py``（commit ``8a5edab282632443``）一致。
#:
#: ⚠️ 阶梯是**期限**（写进 ``not_before``）而不是 sleep：这才是它重启安全的原因 ——
#: 一个进程重启不会把等待重置一遍，也不会有半个 sleep 睡在内存里。
#:
#: 级数与 :data:`MAX_ATTEMPTS` **必须相等减一** —— ``MAX_ATTEMPTS`` 次尝试之间只隔
#: ``MAX_ATTEMPTS - 1`` 次等待。这条约束是照抄 hermes 的：
#:
#:     _RETRY_BACKOFF_SECONDS = (30.0, 120.0)
#:     assert len(_RETRY_BACKOFF_SECONDS) == MAX_ATTEMPTS - 1
#:
#: 之前这里写成三级（30/120/600）配 ``MAX_ATTEMPTS = 3``，是**派单规格里的算术错**
#: （抄了公式却没抄它隐含的级数）：``mark_failed`` 自增后的 ``attempts`` 从 1 起，
#: 而放弃条件是 ``attempts >= MAX_ATTEMPTS``，于是只有 ``attempts`` 为 1 和 2 时
#: 会排重试 —— 三级阶梯的第 0 级（30 秒）**永远触发不到**，首次重试被白白多等了
#: 90 秒（120s 而不是 30s）。
BACKOFF_LADDER_SECONDS: tuple[float, ...] = (30.0, 120.0)

#: 最多花掉几次投递尝试。第 ``MAX_ATTEMPTS`` 次失败就转 ``abandoned``，
#: **绝不**再排下一次重试（理由见 :meth:`InboundInbox.mark_failed`）。
MAX_ATTEMPTS: int = 3

assert len(BACKOFF_LADDER_SECONDS) == MAX_ATTEMPTS - 1, (
    "MAX_ATTEMPTS 次尝试之间只隔 MAX_ATTEMPTS - 1 次等待，"
    "所以阶梯级数必须正好等于 MAX_ATTEMPTS - 1；"
    "多给几级不会更安全——超出部分的级永远轮不到，"
    "少给则最后一次重试会复用更早的期限，阶梯就白写了"
)

#: ``delivered`` 行的保留时长（秒）。取 24 小时是为了**当回执用** ——
#: 平台在保留期内重投，靠的就是这行去重（和 Telegram 自己那份未确认更新的保留期一致）。
DEFAULT_DELIVERED_RETENTION_SECONDS: float = 86400.0

#: 总行数上限。入站量是人的量级（每分钟几条），500 行足够覆盖很长的故障期；
#: 它是**兜底**而不是常规路径，真触发时优先牺牲终态行。
DEFAULT_MAX_ROWS: int = 500


class DeliveryState:
    """收件箱一行的状态。纯字符串常量，便于直接落进 SQLite 与日志。"""

    #: 已落盘、尚未尝试投递。
    PENDING = "pending"
    #: 正在投递。**落在这一行的崩溃 = 结果不可知**，只告警不重放。
    ATTEMPTING = "attempting"
    #: 投递成功。这一行同时是平台重投时的**回执**。
    DELIVERED = "delivered"
    #: 投递明确失败，等 ``not_before`` 到期后重放。
    FAILED = "failed"
    #: 重试预算耗尽。终态 + 一次告警。
    ABANDONED = "abandoned"


#: 行数上限的淘汰优先级：数字小的先走。``pending`` 故意不出现 ——
#: 那是一笔**还没兑现的处理义务**，行数上限不该有权把它丢掉。
#: 同档之内最旧的先走。
_EVICTION_PRIORITY = (
    ("WHEN ? THEN 0", DeliveryState.DELIVERED),
    ("WHEN ? THEN 1", DeliveryState.ABANDONED),
    ("WHEN ? THEN 2", DeliveryState.ATTEMPTING),
    ("WHEN ? THEN 3", DeliveryState.FAILED),
)


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


class InboundInbox:
    """落盘的入站收件箱。**只做持久化，不含投递策略。**

    线程模型：所有公开方法都在一把 ``RLock`` 下操作**同一条** SQLite 连接
    （``check_same_thread=False``），因为轮询线程与主线程都会碰它。

    耐久性：``journal_mode=WAL`` + ``synchronous=FULL``，且连接开在 autocommit
    模式（``isolation_level=None``）—— 每条语句自己就是一次事务，于是
    "落盘成功" 与 "方法返回" 之间不存在一个需要 commit 的窗口。

    ``close()`` 之后所有公开方法都是**安全的空操作**（不抛、不崩）：
    清理顺序出错不该让桥在退出路径上炸掉。
    """

    _SCHEMA = """
        CREATE TABLE IF NOT EXISTS inbox (
            delivery_id     TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            platform        TEXT NOT NULL,
            message_id      TEXT,
            text            TEXT NOT NULL,
            state           TEXT NOT NULL,
            attempts        INTEGER NOT NULL DEFAULT 0,
            not_before      REAL NOT NULL DEFAULT 0,
            last_error      TEXT,
            created_at      REAL NOT NULL,
            updated_at      REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS inbox_due ON inbox(state, not_before);
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
        self._connection: Optional[sqlite3.Connection] = None
        self._open_connection()
        # 启动时清一次：上一次运行的 delivered 回执可能早就过期了，
        # 上一次运行留下的行也可能已经超了上限。
        self._prune_expired_delivered(time.time())
        self._enforce_row_cap()

    # ------------------------------------------------------------------
    # 写前义务
    # ------------------------------------------------------------------
    def record(self, prompt: QueuedPrompt) -> bool:
        """把一条入站消息**在分发之前**落盘。返回 ``True``=新行，``False``=已存在。

        ``False`` 不是错误，而是**去重命中**：同一个 ``delivery_id`` 已经在收件箱里
        （可能已经 ``delivered``），这条重投不该再跑一遍 agent。注意这里保留
        **原来那一行**，包括它的状态与正文 —— 已送达的回执不能被重投覆盖掉。
        """
        with self._lock:
            if self._connection is None:
                return False
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
            inserted = cursor.rowcount == 1
            if inserted:
                self._prune_expired_delivered(moment)
                self._enforce_row_cap()
            return inserted

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
    # 内部：连接、写入口、行数治理
    # ------------------------------------------------------------------
    def _open_connection(self) -> None:
        parent = os.path.dirname(os.path.abspath(self._path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        connection = sqlite3.connect(
            self._path,
            check_same_thread=False,
            isolation_level=None,  # autocommit：每条语句自成一个事务
            timeout=10.0,
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            # 入站量是每分钟几条，FULL 的开销不值得拿耐久性去换。
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(self._SCHEMA)
        except sqlite3.Error:
            connection.close()
            raise
        self._connection = connection

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
        """删掉超过保留期的 ``delivered`` 行。

        ``abandoned`` **不在这里删** —— 它是终态，要留在状态输出里看得见，
        由下面那道行数上限兜底。
        """
        with self._lock:
            self._write(
                "DELETE FROM inbox WHERE state = ? AND updated_at <= ?",
                (DeliveryState.DELIVERED, moment - self._delivered_retention_seconds),
            )

    def _enforce_row_cap(self) -> None:
        """把总行数压回上限：先淘汰 ``delivered``，再 ``abandoned``，同档内最旧的先走。"""
        with self._lock:
            if self._connection is None:
                return
            total = self._connection.execute("SELECT COUNT(*) FROM inbox").fetchone()[0]
            surplus = int(total) - self._max_rows
            if surplus <= 0:
                return
            priority_clause = " ".join(fragment for fragment, _state in _EVICTION_PRIORITY)
            # 内层 SELECT 显式排除 pending：那是一笔还没兑现的义务，
            # 行数上限无权丢它（宁可超上限并告警，见下面那段 warning）。
            self._write(
                "DELETE FROM inbox WHERE delivery_id IN ("
                " SELECT delivery_id FROM inbox WHERE state != ?"
                f" ORDER BY CASE state {priority_clause} ELSE 4 END,"
                " updated_at ASC, delivery_id ASC LIMIT ?)",
                (DeliveryState.PENDING,)
                + tuple(state for _fragment, state in _EVICTION_PRIORITY)
                + (surplus,),
            )
            remaining = self._connection.execute(
                "SELECT COUNT(*) FROM inbox"
            ).fetchone()[0]
            logger.debug(
                "inbox %s: evicted %d row(s) to stay under the %d-row cap",
                self._path, surplus, self._max_rows,
            )
            if int(remaining) > self._max_rows:
                # 只可能是 pending 行顶上来了 —— 那是还没兑现的义务，行数上限无权丢它。
                logger.warning(
                    "inbox %s: %d pending row(s) exceed the %d-row cap and were kept;"
                    " the cap will not drop unfulfilled obligations",
                    self._path, int(remaining) - self._max_rows, self._max_rows,
                )
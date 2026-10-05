"""入站收件箱的 **SQLite 基座**（G2）：建表 DDL，以及"把数据库文件开成一条可用连接"
的那一段配置。

从 :mod:`opencode_bridge.inbox` 搬走了什么
-----------------------------------------
本模块拥有 :data:`INBOX_SCHEMA`（原 ``InboundInbox._SCHEMA``）与
:func:`open_inbox_connection`（原 ``InboundInbox._open_connection`` 的全部语句）。
:class:`~opencode_bridge.inbox.InboundInbox` 现在只在 ``__init__`` 里调一次
:func:`open_inbox_connection` 并把返回值存进 ``self._connection``。

为什么这么切
------------
收件箱的职责是"把一行提示词可靠地落盘"，而"怎么把这个 SQLite 文件打开成一条
**够用**的连接"是**另一件事**：它有自己的一套失败方式（父目录建不出来、磁盘满、
DDL 失败）与自己的一套耐久性取舍。改耐久性档位或加一列时，不该在状态机那一段里
翻找 —— 那是两处互不相干的改动动因，而这正是 AGENTS.md §5 说的"改一处功能只碰
一个文件"。

开出来的连接有四条**承重**属性，少一条都是行为变化：

* ``check_same_thread=False`` —— 轮询线程与主线程都会碰它；
* ``isolation_level=None``（autocommit）—— 每条语句自己就是一次事务，于是"落盘成功"
  与"方法返回"之间不存在一个需要 commit 的窗口；
* ``journal_mode=WAL`` + ``synchronous=FULL`` —— 崩溃时留在盘上的那行就是真相；
* ``row_factory = sqlite3.Row`` —— 按**列名**取值的地方遍布收件箱（读回
  :class:`~opencode_bridge.inbox.QueuedPrompt` 的行映射、行数治理层的分组计数）。

⚠️ 建表失败时的 ``close()`` + ``raise`` 不是防御性代码而是**必需**的：DDL 失败后若把
半开的连接挂上 ``self._connection``，收件箱会以降级状态继续服务，而之后每一次
写入的失败原因都与真正的病因无关。
"""

from __future__ import annotations

import os
import sqlite3

__all__ = ["INBOX_SCHEMA", "open_inbox_connection"]

#: ``inbox`` 表的建表 DDL。幂等（``IF NOT EXISTS``），每次开连接都跑一遍。
INBOX_SCHEMA = """
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


def open_inbox_connection(path: str) -> sqlite3.Connection:
    """打开（或建出）收件箱数据库并配置好，返回**已经可用**的连接。

    :param path: 数据库文件路径。父目录不存在时会被创建。
    :returns: autocommit、按列名取值、WAL + ``synchronous=FULL`` 的连接，
        ``inbox`` 表已就位。
    :raises sqlite3.Error: 连不上或建表失败。**不会**返回半配置状态的连接 ——
        失败时先 ``close()`` 再抛，让调用方拿到的是一个干净的失败而不是一个
        "能用但会陆续出错"的连接。
    """
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    connection = sqlite3.connect(
        path,
        check_same_thread=False,
        isolation_level=None,  # autocommit：每条语句自成一个事务
        timeout=10.0,
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        # 入站量是每分钟几条，FULL 的开销不值得拿耐久性去换。
        connection.execute("PRAGMA synchronous=FULL")
        connection.executescript(INBOX_SCHEMA)
    except sqlite3.Error:
        connection.close()
        raise
    return connection

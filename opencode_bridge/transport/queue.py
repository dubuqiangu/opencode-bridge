"""把「一轮返回一批」的 fetch 适配成 :meth:`Transport._next` 的「一次交付一条」。

## 为什么需要它

``Transport._next`` 的契约是"返回**下一条**原始事件"。但真实世界的轮询接口一轮
往往返回**一批**：Telegram ``getUpdates``、Matrix ``/sync``、Nextcloud
``lookIntoFuture=1`` 都是如此。让每个适配器自己写闭包队列 + 排空逻辑，就是
N 份几乎一样的样板，且极易写出"丢事件"或"死循环"的 bug。

所以这里提供共用件：把批量结果 :meth:`push_many` 进来，逐条 :meth:`pop` 吐出，
吐空后返回 :data:`~opencode_bridge.transport.base.NOTHING`（"这轮没有"）。
``_next`` 的契约因此保持简单 —— **什么时候排空的责任仍在适配器手里**，没有把
时序责任推给基类。

用法::

    q = EventQueue()

    def fetch():
        batch = http_get("/getUpdates")          # 一轮一批
        if not batch:
            return NOTHING
        q.push_many(batch)
        return q.pop()                            # 或者让 transport 拉

    # 短轮询（无事件就 NOTHING + idle_sleep）
    PollingTransport(lambda: q.pop() or _refill(fetch),
                     idle_sleep=1.0).start(dispatch)

更常见的写法是让 ``fetch`` 本身填队列并返回哨兵::

    def fetch():
        batch = http_get("/getUpdates")
        if not batch:
            return NOTHING
        q.push_many(batch)
        return q.pop()
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Any, Iterable

from .base import NOTHING

__all__ = ["EventQueue"]


class EventQueue:
    """线程安全的 FIFO 批量事件缓冲（无上限，由调用方控制 drain 速度）。"""

    def __init__(self, maxsize: int = 0) -> None:
        #: ``maxsize > 0`` 时 :meth:`push_many` 会丢弃**最旧**的溢出项，
        #: 避免一个失控的长轮询把内存吃光。``0`` = 不限。
        self.maxsize = max(0, int(maxsize))
        self._items: deque[Any] = deque()
        self._lock = threading.Lock()
        self.dropped = 0

    def push_many(self, items: Iterable[Any]) -> int:
        """塞入一批事件，返回当前累计待处理条数。

        ``items`` 为 ``None`` 或空 → 不改变队列。
        """
        if items is None:
            return len(self)
        with self._lock:
            for item in items:
                if self.maxsize and len(self._items) >= self.maxsize:
                    self._items.popleft()
                    self.dropped += 1
                self._items.append(item)
            return len(self._items)

    def pop(self) -> Any:
        """取最早一条；**空队列返回** :data:`NOTHING`（不是 ``None``）。

        用 ``NOTHING`` 而非 ``None``：``None`` 在本包语义里被"暂时没有"占用过，
        哨兵能让调用方一眼区分"没有"与"意外的空值"。
        """
        with self._lock:
            if not self._items:
                return NOTHING
            return self._items.popleft()

    def drain(self, limit: int | None = None) -> list[Any]:
        """一次取走最多 ``limit`` 条（``None`` = 全部），便于批处理场景。"""
        with self._lock:
            if limit is None:
                out = list(self._items)
                self._items.clear()
                return out
            out = []
            while self._items and len(out) < limit:
                out.append(self._items.popleft())
            return out

    def clear(self) -> int:
        """丢弃全部待处理事件，返回丢弃条数（连接重建时用来避免处理旧连接的残事件）。"""
        with self._lock:
            n = len(self._items)
            self._items.clear()
            return n

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def __bool__(self) -> bool:
        return len(self) > 0

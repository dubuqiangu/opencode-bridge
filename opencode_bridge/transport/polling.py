"""HTTP 轮询传输（短轮询 + 长轮询，用同一个类）。

覆盖的平台
----------
* **Telegram** ``getUpdates``：短轮询 —— 服务端立刻返回，本类靠
  ``idle_sleep`` 降频（否则会打爆 API）。
* **Matrix** ``/sync``：长轮询 —— 服务端挂起约 30s，有数据才返回，
  ``idle_sleep=0``（自己会挂起，无需再睡）。
* **Nextcloud Talk** ``lookIntoFuture=1``：同上，也是长轮询。

为什么用哨兵而不是 ``None``
---------------------------
``fetch()`` 返回 :data:`NOTHING` 表示"这轮没有"；``None`` 在 :meth:`_next`
的约定里已经是"连接结束了"。轮询语义下"没有"是**常态**，用哨兵才能把
"暂时没有"与"该重连"分开。写法::

    def fetch():
        resp = http_get("/sync", timeout=40)
        for ev in resp["next_batch"]:      # 这里用 NOTHING 表示"这轮空"
            queue.append(ev)
        return NOTHING
        # 注意：一轮里有多条时不能直接返回一条 —— 返回值是"一条"。
        # 需要批量时把 queue 挂在闭包上，逐条取；取空时返回 NOTHING。

    PollingTransport(fetch, idle_sleep=0.0, name="matrix")
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from .base import NOTHING, Transport

__all__ = ["PollingTransport", "NOTHING"]

logger = logging.getLogger("opencode_bridge.transport.polling")


class PollingTransport(Transport):
    """HTTP 轮询：``fetch()`` 返回一条原始事件，无新数据时返回 ``NOTHING``。

    长轮询型（``fetch`` 自带服务端挂起，如 Matrix ``/sync``、Nextcloud
    ``lookIntoFuture=1``）与短轮询型（Telegram ``getUpdates``）都用它，差别只在
    ``fetch`` 内部与 ``idle_sleep``：

    :param fetch: 无参可调用，返回**一条**原始事件，或 :data:`NOTHING`
        表示"这轮没有"。抛异常 = 瞬时失败（HTTP 5xx / 连接被重置），基类会
        退避后重试 —— 所以把 4xx 这类"重试也没用"的判定留在 ``fetch`` 内部
        自愈（跳过该轮并返回 NOTHING），别让它变成无意义的指数退避。
    :param idle_sleep: 空转时的休眠秒数。**短轮询必须给一个非零值**
        （Telegram 官方就是靠调用间隔限流），长轮询传 ``0``。
    :param on_open: 可选，每次"开始一轮会话"调用一次（长轮询里适合发
        ``/sync`` 的首次请求、重置 since token；不会因每次 fetch 重复调用）。
    """

    def __init__(
        self,
        fetch: Callable[[], Any],
        *,
        idle_sleep: float = 0.0,
        on_open: Callable[[], None] | None = None,
        **kw: Any,
    ) -> None:
        if not callable(fetch):
            raise TypeError("fetch 必须可调用")
        if on_open is not None and not callable(on_open):
            raise TypeError("on_open 必须可调用")
        self._fetch = fetch
        self._idle_sleep = max(0.0, float(idle_sleep))
        self._on_open_cb = on_open
        super().__init__(**kw)

    @property
    def fetch(self) -> Callable[[], Any]:
        """底层拉取函数（适配器偶尔要直接调，例如主动催一次）。"""
        return self._fetch

    def _idle_delay(self) -> float:
        return self._idle_sleep

    def _open(self) -> Any:
        """没有长连接，所以"连接对象"就是 ``fetch`` 本身。

        顺带在这里调 ``on_open``：它在基类里算作"本次会话开始"，抛异常会被
        当成本轮连接失败（退避重试），正好符合"登录握手失败要重试"的语义。
        """
        if self._on_open_cb is not None:
            self._on_open_cb()
        return self._fetch

    def _next(self, conn: Any) -> Any:
        """调一次 ``fetch``；返回值原样交出去（含 ``NOTHING``）。"""
        return self._fetch()
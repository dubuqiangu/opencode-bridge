"""SSE 订阅的**看护者**：订阅崩了 ⇒ 有界退避后重新订阅，而不是让线程就此结束。

这个模块为什么存在
==================

``ora-15`` 复查确诊的那条缺陷（本仓库优先级最高的一条，因为 ``/api/event`` 是
**服务端事件流**、telegram 也依赖它）::

    /api/event 的任何非 200 ⇒ OpenCodeClient.subscribe 把 OpenCodeError 原样抛出
                             ⇒ EventStream.run 记一行日志后 **返回**
                             ⇒ SSE 线程永久结束、**不自愈**
                             ⇒ 进程余下**全部答复静默丢失**，而 --status 照常报正常

这里补的是那一段：异常**不许**逃出线程，逃出来了就退避、重新订阅。

为什么拆成独立模块（而不是塞进 ``event_stream.py``）
====================================================

1. :mod:`opencode_bridge.event_stream` 已经 800 多行、在 AGENTS.md §5.0 的待拆名单上；
   重连状态机**不是**「事件流」这个职责，硬塞进去只会让那条债更长（§5.1「顺带判据」）。
2. 它**不认识 IM、也不认识 turn**：输入只有「一个返回事件迭代器的 callable」与
   「一帧怎么处理」，输出只有「线程别死 + 现在是什么状态」⇒ 能脱离
   :class:`~opencode_bridge.core.BridgeCore` 单独测。
3. 退避被做成**纯状态机**（:class:`ReconnectBackoff`，不睡眠、不碰线程）⇒ 它的数列
   可以逐项断言，而不必拿 ``sleep`` 去观测（本机 ``time.monotonic()`` 只有 16 ms
   分辨率，§7.1 那条）。

退避的数是怎么定的
==================

* **首次 0.5 s** —— 与 :data:`opencode_bridge.opencode_client._RECONNECT_MIN` 同值。
  真实断线常常是**瞬时**的（opencode 重启一次一两秒），第一次重试必须快，否则用户要为
  一次已经自愈的抖动白等。
* **上限 5 s** —— 这条上限换的是**恢复时间**，不是「别打扰服务端」：服务端在 loopback
  上，封顶 5 s 意味着最坏 0.2 次请求/秒，比任何号称「怕打扰」的数都更频繁，而它买到
  的是「opencode 一重启完，最多 5 s 内答复就恢复」。⚠️ 刻意**不**照抄客户端内部那套
  0.5→30 s 的上限：那一层的退避只在**一次连接之内**生效（``subscribe`` 自己会重连），
  而这一层面对的是「连都连不上」—— 30 s 的代价是整整 30 s 的静默，对 telegram 不可接受。
* **这一轮收到过帧就重置回下限** —— 一次真正跑起来的订阅又断了，那按瞬时抖动处理，
  而不是继承之前已经涨到封顶的那串等待。与
  :meth:`opencode_bridge.transport.base.Transport._next_backoff` 的 ``survived``
  同一个纪律：判据由调用点给，本模块不看时钟。

「线程死了」与「正在重连」怎么分开
================================

:meth:`SubscriptionSupervisor.status` 给出一份**不可变**的 :class:`SubscriptionStatus`，
``phase`` 有五个取值，而**「正在重连」与「线程已经死了」在字面上就不同**：

=========================  ==================================================
``phase``                  含义
=========================  ==================================================
``idle``                   还没进 :meth:`SubscriptionSupervisor.run`
``streaming``              订阅活着，正在收帧
``reconnecting``           上一次订阅坏了，正在退避 —— **线程还活着**
``stopped_by_request``     :meth:`SubscriptionSupervisor.request_stop` 叫停的，正常退出
``ended_without_stop``     线程**已经结束**且不会自己回来 ⚠️ 需要人管
=========================  ==================================================

⚠️ **状态先写、日志后发**（见 :meth:`SubscriptionSupervisor._back_off_and_announce`）
⇒ 「读到那行日志」蕴含「状态已经是 reconnecting」，读者不必猜，反过来也不会出现
「日志说正在重连、状态却说线程死了」这种自相矛盾。

⚠️ ``ended_without_stop`` 只在**订阅自己走完、且没人叫停**时出现。生产里
:meth:`opencode_bridge.opencode_client.OpenCodeClient.subscribe` 的生成器**只在客户端
关闭时**才正常走完（它内部自己带重连），而 :meth:`BridgeCore.stop` 现在先叫停看护者
再关客户端 ⇒ 正常关机走的是 ``stopped_by_request``，这条留给真正的意外。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from typing import Any

__all__ = [
    "FIRST_RECONNECT_DELAY_SECONDS",
    "MAX_RECONNECT_DELAY_SECONDS",
    "PHASE_ENDED_WITHOUT_STOP",
    "PHASE_IDLE",
    "PHASE_RECONNECTING",
    "PHASE_STOPPED_BY_REQUEST",
    "PHASE_STREAMING",
    "ReconnectBackoff",
    "SubscriptionStatus",
    "SubscriptionSupervisor",
]

logger = logging.getLogger("opencode_bridge.subscription_supervisor")

#: 第一次重试要等多久（也是「这一轮收到过帧」之后重置回的下限）。见模块 docstring。
FIRST_RECONNECT_DELAY_SECONDS = 0.5

#: 退避的**上限**：封顶之后不再增长。见模块 docstring「退避的数是怎么定的」。
MAX_RECONNECT_DELAY_SECONDS = 5.0

#: 还没进 :meth:`SubscriptionSupervisor.run`。
PHASE_IDLE = "idle"
#: 订阅活着，正在收帧。
PHASE_STREAMING = "streaming"
#: 上一次订阅坏了，正在退避 —— **线程还活着**，别去重启它。
PHASE_RECONNECTING = "reconnecting"
#: :meth:`SubscriptionSupervisor.request_stop` 叫停的，正常退出路径。
PHASE_STOPPED_BY_REQUEST = "stopped_by_request"
#: 线程**已经结束**且不会自己回来 —— ⚠️ 这才是「需要人管」的那一种。
PHASE_ENDED_WITHOUT_STOP = "ended_without_stop"


class ReconnectBackoff:
    """指数退避的**纯状态机**（不睡眠、不碰线程），可逐项断言。

    * ``take(delivered_frames=0)``：本次等当前值，随后 ``×2`` 封顶 →
      ``first, 2first, 4first, …, cap, cap…``
    * ``take(delivered_frames>0)``：**本次就**只等下限，状态也重置回下限

    ``delivered_frames`` 由**调用点**判定（见
    :meth:`SubscriptionSupervisor._back_off_and_announce` 里那句
    ``self._frames_this_subscription``）—— 本类只看这个数，所以它不依赖任何时钟。
    """

    def __init__(self, first_delay: float, max_delay: float) -> None:
        # 夹逼一下，免得 min > max 这种手滑配置让第一次就「封顶」在错误值上
        # （与 Transport.__init__ 同一道夹逼）。
        self.first_delay = max(0.0, float(first_delay))
        self.max_delay = max(self.first_delay, float(max_delay))
        self._next_wait = self.first_delay

    def take(self, *, delivered_frames: int) -> float:
        """返回"这次要等几秒"，并推进状态机。"""
        if delivered_frames > 0:
            self._next_wait = self.first_delay
            return self.first_delay
        wait = self._next_wait
        self._next_wait = min(self._next_wait * 2.0, self.max_delay)
        return wait


@dataclass(frozen=True)
class SubscriptionStatus:
    """订阅状态的一份**不可变**快照（读它不需要拿锁）。

    刻意不缓存成"最后一次失败的时刻"之类的东西：那类字段能骗人 —— 一个**活着**
    的看护者也可能几小时没出过错。真正能分开"线程死了"与"正在重连"的只有
    ``phase``，所以它才是这一份记录的主角。
    """

    phase: str = PHASE_IDLE
    #: 一共**开始过**几次订阅（第一次是 1）。重连 ⇒ 这个数会继续涨。
    subscriptions_started: int = 0
    #: 一共**宣布过**几次重连。
    reconnect_attempts: int = 0
    #: 一共收到过几帧。
    frames_received: int = 0
    #: 最近一次失败的类型与正文；成功收到帧之后不清（它是"上一次为什么坏"的档案）。
    last_error: str | None = None

    @property
    def recovering(self) -> bool:
        """「正在重连」—— **线程还活着**，不需要人管，但也不在收事件。"""
        return self.phase == PHASE_RECONNECTING

    @property
    def terminated_without_stop(self) -> bool:
        """线程**已经结束**且不会自己回来 ⚠️ —— 这是需要人管的那一种。"""
        return self.phase == PHASE_ENDED_WITHOUT_STOP


class SubscriptionSupervisor:
    """/api/event 订阅的看护者：**订阅线程的主体**。

    它管三件事，其余一律不碰：别让线程死、别紧密重试、把"现在是什么状态"说出来。

    :param subscribe: 调它得到一个事件迭代器（**每次调用都是一次全新的订阅**）。
    :param on_frame: 一帧来了怎么处理（异常由本类关进笼子，不许逃出线程）。
    :param first_delay: 第一次重试等几秒；``None`` 用
        :data:`FIRST_RECONNECT_DELAY_SECONDS`。
    :param max_delay: 退避上限；``None`` 用 :data:`MAX_RECONNECT_DELAY_SECONDS`。
    """

    def __init__(
        self,
        *,
        subscribe: Callable[[], Iterator[dict]],
        on_frame: Callable[[Any], None],
        first_delay: float | None = None,
        max_delay: float | None = None,
    ) -> None:
        self._subscribe = subscribe
        self._on_frame = on_frame
        self._backoff = ReconnectBackoff(
            FIRST_RECONNECT_DELAY_SECONDS if first_delay is None else first_delay,
            MAX_RECONNECT_DELAY_SECONDS if max_delay is None else max_delay,
        )
        #: 退避等待与 :meth:`request_stop` 共用这一个事件 —— 于是"退避中"也能被立刻
        #: 打断（``Event.wait``，不是 ``sleep``）。
        self._stop = threading.Event()
        self._status = SubscriptionStatus()
        self._frames_this_subscription = 0

    # ------------------------------------------------------------------
    # 状态与停止
    # ------------------------------------------------------------------
    def status(self) -> SubscriptionStatus:
        """当前状态的快照（读它不需要拿锁）。"""
        return self._status

    def request_stop(self) -> None:
        """请看护者收工。**任何线程可调，可重复调**。

        它做两件事：置位那个 :class:`threading.Event`（于是**正处于退避等待**的线程
        立刻醒来，不必睡满这一拍），以及让 :meth:`run` 的下一轮循环退出。
        """
        self._stop.set()

    def _update(self, **changes: Any) -> None:
        """写一份**新**快照。

        整份替换而不是就地改字段 ⇒ 读侧永远看到自洽的一份记录，且读它不需要拿锁
        （新对象造好之后才做一次属性赋值）。
        """
        self._status = replace(self._status, **changes)

    # ------------------------------------------------------------------
    # 线程主体
    # ------------------------------------------------------------------
    def run(self) -> None:
        """订阅 → 收帧 → 失败则退避 → 再订阅，直到 :meth:`request_stop`。

        ⛔ 除了 :meth:`request_stop` 之外**没有任何东西**能让本方法返回。
        「订阅自己走完了就返回」正是本模块要修的那个缺陷，所以它被明确记成
        :data:`PHASE_ENDED_WITHOUT_STOP` 并打一行 warning，而不是安静地退出。
        """
        while not self._stop.is_set():
            try:
                self._drain_one_subscription()
            except Exception as error:  # noqa: BLE001 - 订阅坏掉不许带走线程
                # ⛔⛔ 收尾通道【自己】也必须被兜住 —— 本模块存在的唯一理由就是
                # 「线程不许死」，而 `_back_off_and_announce` 里有四个可能抛的东西
                # （``take`` / ``_update`` / ``logger.warning`` / ``_stop.wait``）
                # ⇒ 它们任何一个抛出，异常就会逃出 ``run``、**永久带走线程**，
                # 那正是本模块要修的那个缺陷本身。
                #
                # ⚠️ **这不是「把真错误藏起来」**：订阅为什么坏已经由
                # ``_back_off_and_announce`` 的那一行 warning 记过了；这里兜的只是
                # 「记完账之后的那几步又失败」⇒ 而那种情况下**仍然必须重试**
                # （不重试就是静默停摆，比抛出去更坏）。
                try:
                    self._back_off_and_announce(error)
                except Exception:  # noqa: BLE001 - 连记账失败也不许带走线程
                    logger.exception(
                        "reconnect bookkeeping failed after %s; retrying anyway "
                        "in %.1fs",
                        error,
                        FIRST_RECONNECT_DELAY_SECONDS,
                    )
                    self._stop.wait(FIRST_RECONNECT_DELAY_SECONDS)
                continue
            self._update(
                phase=PHASE_ENDED_WITHOUT_STOP,
                frames_received=(
                    self._status.frames_received + self._frames_this_subscription
                ),
            )
            logger.warning(
                "event subscription ended on its own (%d frame(s) in it); "
                "the SSE thread is now gone and will not come back",
                self._frames_this_subscription,
            )
            return
        self._update(phase=PHASE_STOPPED_BY_REQUEST)

    def _drain_one_subscription(self) -> None:
        """订阅一次并把每一帧交给回调；订阅坏了就把异常抛给 :meth:`run`。

        ⚠️ **订阅是串行的**：上一个迭代器在 ``finally`` 里被关掉之后才会订阅下一个
        ⇒ 任何时刻至多一条订阅活着。两条同时活着就意味着同一条事件会被处理两次
        （两个 SSE 连接收同一份全服务器广播），所以这个 ``finally`` 是承重的。

        正常走完（生成器耗尽）**不算失败**：生产里 ``subscribe`` 只在客户端关闭时才
        正常走完（它内部自带重连），所以这里返回 ⇒ :meth:`run` 收工。
        """
        self._frames_this_subscription = 0
        self._update(
            phase=PHASE_STREAMING,
            subscriptions_started=self._status.subscriptions_started + 1,
        )
        subscription: Any = None
        try:
            subscription = self._subscribe()
            for event in subscription:
                if self._stop.is_set():
                    return
                self._frames_this_subscription += 1
                self._update(
                    frames_received=self._status.frames_received + 1
                )
                self._on_frame(event)
        finally:
            self._close_quietly(subscription)

    def _back_off_and_announce(self, error: Exception) -> None:
        """一次订阅失败之后：先退避（**有界**），再重新订阅。

        ⚠️ **状态先写、日志后发** —— 于是「读到这行日志」蕴含「状态已经是
        reconnecting」；顺序反了就会出现「日志说正在重连、状态却说线程死了」。
        """
        if self._stop.is_set():
            return  # 关机已经叫停了：别再宣布一个不会发生的重连
        wait = self._backoff.take(delivered_frames=self._frames_this_subscription)
        self._update(
            phase=PHASE_RECONNECTING,
            reconnect_attempts=self._status.reconnect_attempts + 1,
            last_error="%s: %s" % (type(error).__name__, error),
        )
        logger.warning(
            "event stream terminated (%s); re-subscribing in %.1fs (attempt %d)",
            error,
            wait,
            self._status.reconnect_attempts,
        )
        # ⛔ 用 Event.wait 而不是 sleep —— 否则 request_stop 叫不醒它，
        # 而「退避中被关机打断」正是这一层必须做到的事。
        self._stop.wait(wait)

    @staticmethod
    def _close_quietly(subscription: Any) -> None:
        """把上一次订阅关掉。

        ⛔ 这里抛出去会**盖掉真正的失败原因**（比如那个 503），所以只记一行。
        ⚠️ ``close`` 是可选的：测试替身常常只给一个 ``list_iterator``。
        """
        closer = getattr(subscription, "close", None)
        if closer is None:
            return
        try:
            closer()
        except Exception:  # noqa: BLE001 - 关闭路径不许崩
            logger.debug("closing the event subscription failed (ignored)",
                         exc_info=True)

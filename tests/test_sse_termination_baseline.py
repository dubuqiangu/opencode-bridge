"""``/api/event`` 订阅的**看护者**（``ora-15`` 确诊的那条缺陷，已修）。

## 修的是什么

``/api/event`` 的**任何非 200** 都会被 :meth:`OpenCodeClient.subscribe` 原样抛出
（实测 ``urlopen`` 只被调一次、没有任何一次重连尝试）；而
:meth:`EventStream.run` 把那个异常记一行 ``event stream terminated`` 之后
**返回** ⇒ SSE 线程就此结束 ⇒ **进程余下全部答复静默丢失**、**不自愈**，
而 ``--status`` 照常报正常。

现在 :meth:`EventStream.run` 把线程主体交给
:class:`~opencode_bridge.subscription_supervisor.SubscriptionSupervisor`：
异常**不许**逃出线程，退避（有界）之后重新订阅。

## 这个文件里装的是什么

三条纪律，各自有它的理由：

1. **没被改的那些基线**（:class:`SubscribePropagatesTheNon200Tests` 与
   :class:`SupervisingTheStreamThreadTests`）**一个字都没动**，因为它们钉的契约
   **刻意没有变**：
   - ``subscribe`` 仍然只尝试一次就把非 200 抛出去（重连归看护者，不归它 ——
     它在连接内已经有一套自己的退避）；
   - 全仓仍然只有**一处**引用 ``event_stream.run``，而 ``core.py`` 里唯一的
     ``is_alive()`` 仍然只在 ``stop()`` 里告警。
2. **原来钉着缺陷形态的那几条，已改成它们的反面**
   （:class:`TheStreamThreadEndsTests` → :class:`TheStreamThreadRecoversTests`、
   :class:`NothingRelaunchesTheStreamTests` → :class:`TheBridgeRecoversTests`）。
   ⚠️ 那些改动**不是**放松断言 —— 每一条新断言都钉在**重连之后发生的行为**上
   （第几次订阅、那一轮收尾发出去的内容、关机后线程还在不在），而"线程还活着"
   单独一条是**恒真**的，钉不住任何东西。
3. ⛔ **不许拿 sleep 观测时序**：本机 ``time.monotonic()`` 只有 16 ms 分辨率
   （AGENTS.md §7.1）⇒ 本文件只用 ``threading.Event`` / 锁内计数 / **离散顺序日志**
   当同步点，``Event.wait`` 的超时只当**死锁守卫**（要 fail 得响亮）。

⛔ 任何新断言都要先问「它会不会在任何情况下都成立」（AGENTS.md §9）——
恒真的断言比没有断言更危险。
"""

from __future__ import annotations

import ast
import contextlib
import io
import logging
import os
import pathlib
import tempfile
import threading
import unittest
import urllib.error
from unittest import mock

import opencode_bridge
from opencode_bridge.config import Config
from opencode_bridge.core import BridgeCore
from opencode_bridge.event_stream import EventStream
from opencode_bridge.opencode_client import Endpoint, OpenCodeClient, OpenCodeError
from opencode_bridge.permission_ledger import PermissionLedger
from opencode_bridge.state import StateStore
from opencode_bridge.subscription_supervisor import (
    FIRST_RECONNECT_DELAY_SECONDS,
    MAX_RECONNECT_DELAY_SECONDS,
    PHASE_ENDED_WITHOUT_STOP,
    PHASE_IDLE,
    PHASE_RECONNECTING,
    PHASE_STOPPED_BY_REQUEST,
    PHASE_STREAMING,
    ReconnectBackoff,
    SubscriptionStatus,
    SubscriptionSupervisor,
)
from tests.bridge_dir_isolation_scan import (
    dotted_name,
    enclosing_function_and_class,
    parent_map,
)
from tests.test_core import FakeClient

#: 服务不可达时 opencode 会回的那一类状态码。用 5xx 而不是 401：两者在当前实现里
#: 走同一条路（``except OpenCodeError: raise``），而 5xx 才是「一会儿会自己好」的那种
#: —— 正是**最该重连、却最不重连**的形态。
SERVICE_UNAVAILABLE = 503
EVENT_ENDPOINT = "http://127.0.0.1:4097/api/event"
RENDEZVOUS_TIMEOUT_SECONDS = 10.0

#: 本 lane 自己的 scratch 目录。⛔ 只在 ``.tmp/`` 底下开这一个子目录。
_LANE_TEMP = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".tmp", "sse-termination-baseline",
)

CONVERSATION_ID = "chat:55"
SESSION_ID = "ses_reconnect_under_test"
ASSISTANT_MESSAGE_ID = "msg_answer_under_test"


def http_error(code: int, body: bytes = b"") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        EVENT_ENDPOINT, code, "HTTP %d" % code, {}, io.BytesIO(body),
    )


class _NoOpState:
    """``EventStream`` 只读 ``all_sessions()``；这里让它读到一个空清单。"""

    def all_sessions(self) -> dict:
        return {}


class StateThatKnowsOneSession(_NoOpState):
    """认得**一个**会话。

    为什么需要它：``dispatch`` 每一帧都从 ``all_sessions()`` 重建
    ``session_id -> conversation_id`` 反查表，而 ``_on_execution_started`` 与
    ``_on_text_delta`` 的「这是不是本桥的会话」判定都读它 ⇒ 空清单的话那一轮根本
    不会被建起来，「在途 turn 能被收尾」这件事就无从谈起。
    """

    def __init__(self, conversation_id: str, session_id: str) -> None:
        self._sessions = {conversation_id: session_id}

    def all_sessions(self) -> dict:
        return dict(self._sessions)


def text_delta(session_id: str, delta: str, ordinal: int) -> dict:
    """一帧 ``session.text.delta``（ordinal 单调 ⇒ :meth:`Turn.assemble` 按序拼接）。"""
    return {
        "type": "session.text.delta",
        "data": {
            "sessionID": session_id,
            "assistantMessageID": ASSISTANT_MESSAGE_ID,
            "delta": delta,
            "ordinal": ordinal,
        },
    }


class StreamThatRefuses(FakeClient):
    """:meth:`OpenCodeClient.subscribe` 的替身：像真的非 200 那样把异常抛出去。

    ⚠️ **刻意做成生成器**（末尾那个 ``yield``）：真客户端的 ``subscribe`` 就是生成器，
    异常在第一次 ``next()`` 时才抛，所以这个替身与它同形 —— 写成普通函数会让
    「异常发生在调用处还是迭代处」这件事变得不一样，而那正是
    :meth:`EventStream.run` 里 ``for`` 语句要看清的东西。
    """

    def __init__(self, *, recovered_at_call: int = 2) -> None:
        super().__init__()
        self.subscribe_calls = 0
        self.subscribe_entered = threading.Event()
        #: 第 ``recovered_at_call`` 次进入时置位 —— 「已经重新订阅过了」的**同步点**
        self.reconnected = threading.Event()
        self._recovered_at_call = recovered_at_call

    def subscribe(self, *, restart: bool = True):
        self.subscribe_calls += 1
        self.subscribe_entered.set()
        if self.subscribe_calls >= self._recovered_at_call:
            self.reconnected.set()
        raise OpenCodeError(
            "GET /api/event -> HTTP %d" % SERVICE_UNAVAILABLE,
            status=SERVICE_UNAVAILABLE,
        )
        yield  # pragma: no cover - 只为让它成为生成器


class StreamThatDropsMidTurn:
    """第一次订阅发两帧后**在那一轮进行中**断开；第二次补完并给出终止事件。

    为什么要有它：「不许丢在途 turn 的收尾」那条约束**只有在这一轮进行中重连**
    才测得到 —— 而那恰恰是最容易丢答复的时刻（那一轮既没有收尾事件已经到达、
    又有一半正文已经发出去）。
    """

    def __init__(self) -> None:
        self.subscribe_calls = 0
        self.resubscribed = threading.Event()

    def subscribe(self, *, restart: bool = True):
        self.subscribe_calls += 1
        if self.subscribe_calls == 1:
            yield {"type": "session.execution.started",
                   "data": {"sessionID": SESSION_ID}}
            yield text_delta(SESSION_ID, "Hello ", 0)
            raise OpenCodeError("connection reset while a turn was running")
        self.resubscribed.set()
        yield text_delta(SESSION_ID, "world", 1)
        yield {"type": "session.execution.succeeded",
               "data": {"sessionID": SESSION_ID}}


class StreamThatDeliversAFrameThenBlocks:
    """发一帧之后**停住**。

    ⛔ 停住的唯一用途是给「订阅还活着、正在收帧」造一个**确定性**的观测窗口：
    看护者此刻正卡在生成器里，不可能已经往前走 ⇒ 那一刻读到的状态只有一个答案。
    （不这么做的话，那 0.5 s 的退避窗口里读到的是什么全看调度。）
    """

    def __init__(self) -> None:
        self.subscribe_calls = 0
        self.release = threading.Event()

    def subscribe(self, *, restart: bool = True):
        self.subscribe_calls += 1
        yield text_delta(SESSION_ID, "hi", 0)
        self.release.wait(RENDEZVOUS_TIMEOUT_SECONDS)  # 死锁守卫，不是时序断言


class SubscriptionScript:
    """按次序给看护者一批「订阅剧本」，并把发生过的事记成一条**离散顺序日志**。

    剧本每一项是：``("frames", [事件…])``（发完就**正常**结束）、
    ``("fail", 异常)``（一帧都没发就坏）、或
    ``("frames_then_fail", [事件…], 异常)``（发了几帧再坏）。
    **用尽之后重复最后一项** —— 所以最后一项写成 ``("frames", [])`` 就能让
    :meth:`SubscriptionSupervisor.run` 自然收工，测试于是既不需要 sleep、
    也不需要"跑到第几次就停"的猜测。

    ⚠️ 这条顺序日志是本文件唯一能证明「**没有**重复订阅」的判据：只数
    ``subscribe_calls`` 的话，两条订阅重叠也照样数得出来。
    ⚠️ **只有最后一项可以是 ``("frames", …)``** —— 中途任何一次正常结束都会被看护者
    记成「线程结束」（它与"端点一直拒绝服务"是两件事，见 ``run()`` 的 docstring）。
    """

    def __init__(self, scripts: list[tuple]) -> None:
        self._scripts = list(scripts)
        self._lock = threading.Lock()
        self.subscriptions_started = 0
        self.max_concurrent = 0
        self.order: list[str] = []
        self._concurrent = 0

    def subscribe(self):
        with self._lock:
            self.subscriptions_started += 1
            index = self.subscriptions_started
            self._concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self._concurrent)
            self.order.append("subscribed#%d" % index)
        script = self._scripts[min(index - 1, len(self._scripts) - 1)]
        return self._one_subscription(index, script)

    def _one_subscription(self, index: int, script: tuple):
        kind, payload = script[0], script[1]
        trailing_failure = script[2] if len(script) > 2 else None
        try:
            if kind == "fail":
                raise payload
            for event in payload:
                yield event
            if trailing_failure is not None:
                raise trailing_failure
        finally:
            with self._lock:
                self._concurrent -= 1
                self.order.append("finished#%d" % index)


class RefusesBeforeYielding:
    """``subscribe()`` 在**被调用时**就抛（不是等第一次 ``next()``）。

    ⚠️ 真客户端恰好不是这样（它是生成器函数），但看护者**不许**依赖这一点 ——
    「异常发生在调用处还是迭代处」是这个缺陷的正面战场，两种都得接住。
    """

    def __init__(self) -> None:
        self.subscribe_calls = 0
        self.reconnected = threading.Event()

    def subscribe(self):
        self.subscribe_calls += 1
        if self.subscribe_calls >= 2:
            self.reconnected.set()
        raise OpenCodeError("no subscription for you")


class _SignallingLogHandler(logging.Handler):
    """记下命中的那几行日志，并在命中 ``needle`` 时置位一个 Event。

    为什么日志能当**同步点**：看护者**先写状态、后发日志**（见
    :mod:`opencode_bridge.subscription_supervisor` 的模块 docstring）⇒
    「收到这行日志」蕴含「状态已经是 reconnecting」，而那正是要断言的东西。
    """

    def __init__(self, needle: str, fired: threading.Event) -> None:
        super().__init__(level=logging.WARNING)
        self.needle = needle
        self.fired = fired
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        self.messages.append(message)
        if self.needle in message:
            self.fired.set()


@contextlib.contextmanager
def watch_subscription_supervisor(needle: str):
    """盯住看护者那个 logger；交出 ``(handler, fired_event)``。"""
    fired = threading.Event()
    handler = _SignallingLogHandler(needle, fired)
    target = logging.getLogger("opencode_bridge.subscription_supervisor")
    target.addHandler(handler)
    try:
        yield handler, fired
    finally:
        target.removeHandler(handler)


def build_event_stream(
    client,
    *,
    state=None,
    send_text=None,
    finalize=None,
) -> EventStream:
    """造一套 ``EventStream``（十三个协作者全是替身或空实现）。"""
    return EventStream(
        client=client,
        state=_NoOpState() if state is None else state,
        lock=threading.RLock(),
        turns={},
        clock=lambda: 1000.0,
        edit_interval=1.5,
        max_message_chars=4000,
        adapter_for=lambda conversation_id: None,
        send_text=(lambda *args, **kwargs: None) if send_text is None else send_text,
        edit_progress=lambda *args, **kwargs: False,
        finalize=(lambda *args, **kwargs: None) if finalize is None else finalize,
        flush_queue=lambda conversation_id: None,
        permission_ledger=PermissionLedger(),
    )


class SubscribePropagatesTheNon200Tests(unittest.TestCase):
    """① ``subscribe`` 把非 200 抛出去，**且抛出前没有重连**。

    ⚠️ **这一整类在修复前后逐字未改**，而它仍然全绿 ⇒ 重连**刻意没有**下沉进
    ``subscribe``。理由：``subscribe`` 在**一次连接之内**已经有一套自己的退避
    （0.5→30 s），把「连都连不上」也塞进去会让那套上限决定恢复时间。
    """

    def test_a_non_200_on_the_event_endpoint_escapes_subscribe(self):
        client = OpenCodeClient(Endpoint("http://127.0.0.1:4097", "pw"))

        with mock.patch(
            "urllib.request.urlopen",
            side_effect=http_error(SERVICE_UNAVAILABLE, b"upstream is restarting"),
        ) as urlopen:
            with self.assertRaises(OpenCodeError) as raised:
                next(client.subscribe(restart=True))

        self.assertEqual(raised.exception.status, SERVICE_UNAVAILABLE)

    def test_subscribe_makes_exactly_one_attempt_before_it_gives_up(self):
        """⭐ 这条是「``subscribe`` 里没有重连分支」的可观察证据。"""
        client = OpenCodeClient(Endpoint("http://127.0.0.1:4097", "pw"))

        with mock.patch(
            "urllib.request.urlopen",
            side_effect=http_error(SERVICE_UNAVAILABLE),
        ) as urlopen:
            with self.assertRaises(OpenCodeError):
                list(client.subscribe(restart=True))

        self.assertEqual(
            urlopen.call_count, 1,
            "抛出前 urlopen 被调了 %d 次 —— 重连被下沉进 subscribe 了。"
            % urlopen.call_count,
        )


class ReconnectBookkeepingFailureTests(unittest.TestCase):
    """⛔ **收尾通道自己抛**时，线程仍然不许死。

    ⚠️ **这条路径此前完全没有测试**：``SubscriptionSupervisor.run`` 里
    ``except Exception as error: self._back_off_and_announce(error)``
    的那一句**自己没有被保护** ⇒ 而那个方法里有四个可能抛的东西
    （``take`` / ``_update`` / ``logger.warning`` / ``_stop.wait``）
    ⇒ 它们任何一个抛出，异常就逃出 ``run``、**永久带走线程** ——
    而「线程永久死亡」正是本模块存在的理由。

    ⇒ 所以这一条断言的是**模块自己的不变量**，不是某个函数的行为。
    """

    def test_bookkeeping_that_raises_does_not_take_the_thread_down(self):
        """"记完账之后那几步又失败" ⇒ 仍然必须重试，而不是把线程带走。"""
        subscribe_calls: list[int] = []
        escaped: list[BaseException] = []
        #: 同步点（⛔ 不用 sleep：本机计时分辨率 16 ms，时序断言不可信）
        saw_three_attempts = threading.Event()

        def always_refuses() -> Iterator[dict]:
            subscribe_calls.append(1)
            if len(subscribe_calls) >= 3:
                saw_three_attempts.set()
            raise RuntimeError("503 from the event endpoint")
            yield {}  # 让它是个生成器；⛔ 这一行永不执行

        supervisor = SubscriptionSupervisor(
            subscribe=always_refuses, on_frame=lambda event: None,
            # 退避缩到 10ms：本测试要的是【重试次数】而不是【等了很久】⇒
            # ⛔ 不许拿「等了 0.5 秒」当证据（那是计时断言）
            first_delay=0.01, max_delay=0.01,
        )

        def broken_bookkeeping(error: Exception) -> None:
            raise OSError("the bookkeeping channel is broken too")

        # 换掉「记账」这一步 —— 它是这条路径上唯一能被外力弄坏的东西
        supervisor._back_off_and_announce = broken_bookkeeping  # type: ignore[method-assign]

        def run_it() -> None:
            try:
                supervisor.run()
            except BaseException as error:  # noqa: BLE001 - 这里就是要看它有没有逃出来
                escaped.append(error)

        worker = threading.Thread(target=run_it, name="supervise-escaping-bookkeeping")
        worker.start()
        try:
            self.assertTrue(
                saw_three_attempts.wait(timeout=5.0),
                "记账失败之后仍必须继续重试；实际重试次数："
                f"{len(subscribe_calls)} 逃出来的异常：{escaped!r}",
            )
        finally:
            supervisor.request_stop()
            worker.join(timeout=5.0)

        self.assertEqual(
            escaped, [],
            "记账失败【不许】把异常逃出 run —— 逃出去就是线程永久死亡，"
            "而那正是本模块要修的缺陷",
        )
        self.assertFalse(worker.is_alive(), "run 必须能正常返回")
        self.assertGreaterEqual(
            len(subscribe_calls), 3,
            "至少重试到第三次 —— 少于三次说明「记账失败」把它打断了",
        )


class ReconnectBackoffTests(unittest.TestCase):
    """退避的数列 —— 「不许空转」与「第一次要快」的那份**可逐项断言**的证据。

    ⛔ 不拿 ``sleep`` 观测时间：:class:`ReconnectBackoff` 是纯状态机，不睡眠、
    不碰线程，所以整条数列可以**精确**断言。
    """

    def test_the_first_wait_is_half_a_second_and_each_failure_doubles_it(self):
        """「第一次重试要快」（真实断线常常是瞬时的）+ 指数退避。"""
        self.assertEqual(
            FIRST_RECONNECT_DELAY_SECONDS, 0.5,
            "首次退避被改了 —— 它的唯一作用是让一次已经自愈的瞬时抖动"
            "不至于被放大成几十秒的静默（见 subscription_supervisor 的 docstring）。",
        )
        backoff = ReconnectBackoff(FIRST_RECONNECT_DELAY_SECONDS,
                                   MAX_RECONNECT_DELAY_SECONDS)

        waits = [backoff.take(delivered_frames=0) for _ in range(4)]

        self.assertEqual(waits, [0.5, 1.0, 2.0, 4.0])

    def test_the_wait_is_capped_so_a_long_outage_cannot_busy_loop(self):
        """⛔ 「不许空转」的那一半：封顶之后**不再增长**，重试频率于是有下限。

        ⚠️ 上限取 5 s ⇒ 最坏 0.2 次订阅尝试/秒。这条数换的是**恢复时间**
        （opencode 一重启完，最多 5 s 内答复就恢复），不是「别打扰服务端」。
        """
        self.assertEqual(
            MAX_RECONNECT_DELAY_SECONDS, 5.0,
            "退避上限被改了 —— 它是「进程最坏静默多久」的那个数。",
        )
        backoff = ReconnectBackoff(FIRST_RECONNECT_DELAY_SECONDS,
                                   MAX_RECONNECT_DELAY_SECONDS)

        waits = [backoff.take(delivered_frames=0) for _ in range(30)]

        self.assertEqual(waits[:5], [0.5, 1.0, 2.0, 4.0, 5.0])
        self.assertEqual(waits[-1], MAX_RECONNECT_DELAY_SECONDS)
        self.assertEqual(
            sum(waits), 0.5 + 1.0 + 2.0 + 4.0 + 26 * 5.0,
            "30 次失败的总等待偏离了「涨到封顶然后一直是封顶」这条数列：%r" % (waits,),
        )

    def test_a_subscription_that_really_delivered_frames_resets_to_the_floor(self):
        """一次真的跑起来的订阅又断了 ⇒ 按瞬时抖动处理，不继承涨到封顶的等待。

        与 ``Transport._next_backoff`` 的 ``survived`` 同一个纪律：判据由调用点给。
        """
        backoff = ReconnectBackoff(FIRST_RECONNECT_DELAY_SECONDS,
                                   MAX_RECONNECT_DELAY_SECONDS)
        for _ in range(10):
            backoff.take(delivered_frames=0)

        after_reset = backoff.take(delivered_frames=3)
        again = backoff.take(delivered_frames=1)

        self.assertEqual(
            [after_reset, again], [FIRST_RECONNECT_DELAY_SECONDS] * 2,
        )

    def test_a_hand_slipped_cap_below_the_floor_is_clamped_to_the_floor(self):
        """与 ``Transport.__init__`` 同一道夹逼：``min > max`` 时不许第一次就「封顶」。"""
        backoff = ReconnectBackoff(5.0, 1.0)

        self.assertEqual(backoff.max_delay, 5.0)
        self.assertEqual(backoff.take(delivered_frames=0), 5.0)


class TheSupervisorKeepsOneSubscriptionTests(unittest.TestCase):
    """⛔ 「不许重复订阅」：两次订阅**不许**重叠，同一条事件也不许被处理两次。

    ⚠️ 只数 ``subscribe_calls`` **抓不住**这件事 —— 两条订阅重叠时那个数照样好看。
    真正的判据是**离散顺序日志**：每个 ``subscribed#N`` 后面必须紧跟
    ``finished#N``，才轮到 ``subscribed#N+1``。
    """

    def test_each_subscription_is_finished_before_the_next_one_starts(self):
        script = SubscriptionScript([
            ("frames_then_fail", [{"frame": 1}, {"frame": 2}],
             OpenCodeError("connection reset")),
            ("fail", OpenCodeError("connection reset")),
            ("frames_then_fail", [{"frame": 3}], OpenCodeError("again")),
            ("frames", []),
        ])
        supervisor = SubscriptionSupervisor(
            subscribe=script.subscribe,
            on_frame=lambda event: None,
            first_delay=0.0,
            max_delay=0.0,
        )

        supervisor.run()

        self.assertEqual(
            script.order, [
                "subscribed#1", "finished#1",
                "subscribed#2", "finished#2",
                "subscribed#3", "finished#3",
                "subscribed#4", "finished#4",
            ],
            "两次订阅重叠了 —— 同一份全服务器广播会被处理两遍。",
        )
        self.assertEqual(
            script.max_concurrent, 1,
            "同一时刻有 %d 条订阅活着。" % script.max_concurrent,
        )

    def test_no_frame_is_delivered_twice_across_a_reconnect(self):
        """⭐ 钉住**重连之后的行为**，而不是只钉住「线程还活着」。"""
        script = SubscriptionScript([
            ("frames_then_fail", [{"frame": 1}, {"frame": 2}],
             OpenCodeError("connection reset")),
            ("fail", OpenCodeError("connection reset")),
            ("frames_then_fail", [{"frame": 3}], OpenCodeError("again")),
            ("frames", []),
        ])
        delivered: list = []

        SubscriptionSupervisor(
            subscribe=script.subscribe,
            on_frame=delivered.append,
            first_delay=0.0,
            max_delay=0.0,
        ).run()

        self.assertEqual(
            delivered, [{"frame": 1}, {"frame": 2}, {"frame": 3}],
            "重连之后重放的帧被处理了两遍。",
        )

    def test_one_failed_subscription_leads_to_exactly_one_new_subscription(self):
        """每次失败恰好换来一次重新订阅 —— 既不漏（不自愈）也不多（紧密重试）。"""
        script = SubscriptionScript([
            ("fail", OpenCodeError("HTTP 503")),
            ("fail", OpenCodeError("HTTP 503")),
            ("fail", OpenCodeError("HTTP 503")),
            ("frames", []),
        ])
        supervisor = SubscriptionSupervisor(
            subscribe=script.subscribe,
            on_frame=lambda event: None,
            first_delay=0.0,
            max_delay=0.0,
        )

        supervisor.run()

        self.assertEqual(script.subscriptions_started, 4)
        self.assertEqual(supervisor.status().reconnect_attempts, 3)

    def test_a_subscribe_that_raises_before_yielding_is_still_caught(self):
        """「异常发生在调用处还是迭代处」两种都得接住。"""
        client = RefusesBeforeYielding()
        supervisor = SubscriptionSupervisor(
            subscribe=client.subscribe,
            on_frame=lambda event: None,
            # ⛔ 刻意不为 0：退避 0 会让这个替身在测试主线程被调度到之前**紧密空转**。
            first_delay=0.005,
            max_delay=0.005,
        )

        worker = threading.Thread(target=supervisor.run, daemon=True)
        worker.start()
        self.addCleanup(worker.join, RENDEZVOUS_TIMEOUT_SECONDS)
        self.addCleanup(supervisor.request_stop)

        self.assertTrue(
            client.reconnected.wait(timeout=RENDEZVOUS_TIMEOUT_SECONDS),
            "看护者没有重新订阅 —— 它在调用处抛的异常上死了。",
        )


class SubscriptionStateIsReadableTests(unittest.TestCase):
    """⭐ 「正在重连」与「线程已经死了」必须**读得出来**。

    ⚠️ 缺陷形态下这两者**无法区分**，而这正是它静默的原因：线程不在了，而所有视图
    都报正常。所以这里断言的不是某个字段的值，而是**一份快照本身能不能分开这两者**。
    """

    def _status_of_a_run_waiting_out_its_backoff(self):
        """一个正在退避等待中的看护者（退避 30 s ⇒ 读状态时它一定还在等）。"""
        script = SubscriptionScript([("fail", OpenCodeError("HTTP 503"))])
        supervisor = SubscriptionSupervisor(
            subscribe=script.subscribe,
            on_frame=lambda event: None,
            first_delay=30.0,
            max_delay=30.0,
        )
        with watch_subscription_supervisor("re-subscribing") as (handler, fired):
            worker = threading.Thread(target=supervisor.run, daemon=True)
            worker.start()
            self.addCleanup(worker.join, RENDEZVOUS_TIMEOUT_SECONDS)
            self.addCleanup(supervisor.request_stop)
            self.assertTrue(
                fired.wait(timeout=RENDEZVOUS_TIMEOUT_SECONDS),
                "看护者没有宣布重连：%r" % (handler.messages,),
            )
        return supervisor.status()

    def _status_of_a_run_that_was_never_stopped(self):
        """一个订阅自己走完、没人叫停 ⇒ 线程结束且不会自己回来。"""
        script = SubscriptionScript([("frames", [])])
        supervisor = SubscriptionSupervisor(
            subscribe=script.subscribe,
            on_frame=lambda event: None,
            first_delay=0.0,
            max_delay=0.0,
        )

        supervisor.run()

        return supervisor.status()

    def test_a_snapshot_alone_tells_reconnecting_apart_from_a_dead_thread(self):
        recovering = self._status_of_a_run_waiting_out_its_backoff()
        dead = self._status_of_a_run_that_was_never_stopped()

        self.assertEqual(
            (recovering.recovering, recovering.terminated_without_stop),
            (True, False),
            "正在退避的看护者被报成了别的状态：phase=%r" % recovering,
        )
        self.assertEqual(
            (dead.recovering, dead.terminated_without_stop),
            (False, True),
            "线程已经没了却没被报出来：phase=%r" % dead,
        )

    def test_a_reconnecting_run_reports_what_it_says_and_how_long_it_will_wait(self):
        """那行日志是 ``--status`` 接不上之前**唯一的用户可见面** ⇒ 它必须自带计划。"""
        recovering = self._status_of_a_run_waiting_out_its_backoff()

        self.assertEqual(recovering.phase, PHASE_RECONNECTING)
        self.assertEqual(recovering.reconnect_attempts, 1)
        self.assertEqual(recovering.subscriptions_started, 1)
        self.assertEqual(recovering.frames_received, 0)
        self.assertIn("503", recovering.last_error or "")

    def test_a_run_that_ended_by_itself_says_so_loudly(self):
        """线程结束必须留得下痕迹 —— 缺陷形态下它只留一行 ERROR 然后就静默了。"""
        script = SubscriptionScript([("frames", [])])
        supervisor = SubscriptionSupervisor(
            subscribe=script.subscribe,
            on_frame=lambda event: None,
            first_delay=0.0,
            max_delay=0.0,
        )

        with watch_subscription_supervisor("will not come back") as (handler, _fired):
            supervisor.run()

        self.assertEqual(supervisor.status().phase, PHASE_ENDED_WITHOUT_STOP)
        self.assertTrue(handler.messages, "结束这件事一个字都没说。")


class RecordingStatusObserver:
    """一个 ``on_status_change`` 观察者：把每次收到的**相位**记成一条离散顺序日志。

    ⚠️ **只记相位的变化、不记次数** —— 因为这份日志是给「相位走过哪几格」用的，
    而「回调被调了几次」是**另一件事**（它每帧一次，见下面那条用例）。

    ⚠️ 它天然线程安全：``list.append`` 在 CPython 里是原子的，而本文件其它地方
    记顺序日志时也是靠这一条 + :class:`threading.Event` 当同步点。
    """

    def __init__(self) -> None:
        self.phases_seen: list[str] = []

    def __call__(self, status: SubscriptionStatus) -> None:
        if not self.phases_seen or self.phases_seen[-1] != status.phase:
            self.phases_seen.append(status.phase)

    def distinct_phase_edges(self) -> list[str]:
        return list(self.phases_seen)


class TheStatusObserverSeesEveryPhaseChange(unittest.TestCase):
    """⭐ ``on_status_change`` 是「运行期通道」的唯一入口 ⇒ 它必须看得见相位翻转。

    ⚠️ **判据是离散顺序日志 + 同步点**，⛔ 没有一处计时：本机
    ``time.monotonic()`` 只有 16 ms 分辨率（AGENTS.md §7.1）。
    """

    def test_a_subscription_that_keeps_failing_walks_through_both_bad_phases(self):
        """⭐ 两个方向都在这一条里：**正在重连**与**线程已经死了**都被观察到。"""
        # ⚠️ 剧本**最后一项必须是 ``("frames", …)``**：用尽之后它会重复最后一项，
        # 而 ``SubscriptionScript`` 的 docstring 明写「中途任何一次正常结束都会被
        # 看护者记成线程结束」⇒ 若最后一项还在失败，这条会**永远转下去**。
        # 这里要的是「先坏一次（⇒ reconnecting），再一次自己走完（⇒ ended）」。
        script = SubscriptionScript([
            ("frames_then_fail", [{"type": "noise"}], OpenCodeError("HTTP 503")),
            ("frames", []),
        ])
        observer = RecordingStatusObserver()
        supervisor = SubscriptionSupervisor(
            subscribe=script.subscribe,
            on_frame=lambda event: None,
            first_delay=0.0,
            max_delay=0.0,
            on_status_change=observer,
        )

        supervisor.run()

        self.assertEqual(
            observer.distinct_phase_edges(),
            [PHASE_STREAMING, PHASE_RECONNECTING, PHASE_STREAMING,
             PHASE_ENDED_WITHOUT_STOP],
            "观察者没看到相位的完整轨迹 ⇒ 运行期通道只能读到其中一部分，"
            "而「正在重连」与「已死」正是这个缺陷的两个方向。",
        )
        self.assertEqual(supervisor.status().phase, PHASE_ENDED_WITHOUT_STOP)

    def test_the_observer_is_told_a_snapshot_that_already_reflects_the_change(self):
        """⚠️ 承重：观察者读到的必须是**已经生效**的那一份，不是半路的状态。

        ⇒ 判据是「回调里读回来的那份 == 回调实参那份」，⛔ 不是「下一瞬间的 status()」
        （那在并发下可能已经变了）。
        会红的条件：把 ``_announce_status_change`` 挪到 ``replace`` **之前**。
        """
        seen: list[tuple] = []

        def observer(status: SubscriptionStatus) -> None:
            seen.append((status, status.phase))

        script = SubscriptionScript([("frames", [])])
        supervisor = SubscriptionSupervisor(
            subscribe=script.subscribe,
            on_frame=lambda event: None,
            first_delay=0.0,
            max_delay=0.0,
            on_status_change=observer,
        )

        supervisor.run()

        self.assertEqual([phase for _status, phase in seen][-1],
                         PHASE_ENDED_WITHOUT_STOP)
        self.assertEqual(
            seen[-1][0], supervisor.status(),
            "回调拿到的实参与回调内部读到的自洽记录对不上 ⇒ 通知与状态不原子",
        )

    def test_the_observer_is_called_once_per_frame_and_that_is_the_callers_problem(self):
        """⭐ 钉住「回调**每帧**都会来」这条事实 ⇔ 所以节流**必须**在调用方。

        ⚠️ 它是「写爆磁盘」那个反向证明的**前提**：若这条不成立，落盘节流就是多余的。

        ⚠️ ⛔ **不用计时**：这里数的是**回调被调了几次**，一个确定值，与时钟无关。
        会红的条件：把 ``frames_received`` 从每次 ``_update`` 里去掉（那会同时让
        :attr:`SubscriptionStatus.frames_received` 失去意义）。

        ⚠️ 那个 ``+2`` 里的两项是「订阅开始」与「线程收工」两次**相位**写；
        每帧只贡献一次。
        """
        frames = [{"type": "noise", "n": index} for index in range(12)]
        script = SubscriptionScript([("frames", frames)])
        frame_counts_seen: list[int] = []

        def counting_observer(status: SubscriptionStatus) -> None:
            frame_counts_seen.append(status.frames_received)

        supervisor = SubscriptionSupervisor(
            subscribe=script.subscribe,
            on_frame=lambda event: None,
            first_delay=0.0,
            max_delay=0.0,
            on_status_change=counting_observer,
        )

        supervisor.run()

        self.assertEqual(
            len(frame_counts_seen), len(frames) + 2,
            "观察者没有每帧都被叫一次 ⇒ 「回调每帧来」这条前提没了，"
            "而落盘节流就是照着它设计的。",
        )
        self.assertEqual(supervisor.status().frames_received, len(frames))

    def test_a_raising_observer_cannot_kill_the_subscription_thread(self):
        """⛔⛔ 承重：一个**观察者**不许推翻「线程不许死」这条不变量。

        ⇒ 那是本模块存在的唯一理由；观察者抛异常就把它推翻的话，
        「落盘这条路坏了」会变成「桥收不到任何事件」—— 那比不落盘坏得多。

        会红的条件：从 :meth:`SubscriptionSupervisor._announce_status_change` 里
        去掉那个 ``except``。
        """
        script = SubscriptionScript([
            ("frames_then_fail", [{"type": "noise"}], OpenCodeError("HTTP 503")),
            ("frames", []),
        ])

        def exploding_observer(status: SubscriptionStatus) -> None:
            raise RuntimeError("落盘这条路自己坏了")

        supervisor = SubscriptionSupervisor(
            subscribe=script.subscribe,
            on_frame=lambda event: None,
            first_delay=0.0,
            max_delay=0.0,
            on_status_change=exploding_observer,
        )

        supervisor.run()

        self.assertEqual(
            supervisor.status().phase, PHASE_ENDED_WITHOUT_STOP,
            "观察者抛异常把线程带走了 ⇒ 落盘故障升级成了静默停摆。",
        )

    def test_a_raising_observer_does_not_stop_the_reconnect_loop(self):
        """⚠️ 上一条的另一半：⛔ 观察者坏了不许**连退避重试一起**停掉。

        会红的条件：把 ``_announce_status_change`` 的兜底放在 ``_back_off_and_announce``
        **之前**（于是重试循环被一次抛异常打断）。
        """
        script = SubscriptionScript([("fail", OpenCodeError("HTTP 503"))])

        def exploding_observer(status: SubscriptionStatus) -> None:
            raise RuntimeError("观察者坏了")

        supervisor = SubscriptionSupervisor(
            subscribe=script.subscribe,
            on_frame=lambda event: None,
            first_delay=0.005,
            max_delay=0.005,
            on_status_change=exploding_observer,
        )
        worker = threading.Thread(target=supervisor.run, daemon=True)
        worker.start()
        self.addCleanup(worker.join, RENDEZVOUS_TIMEOUT_SECONDS)
        self.addCleanup(supervisor.request_stop)

        # 同步点：等它**至少**重新订阅过一次（⛔ 不用 sleep —— 见 _wait_until 的
        # docstring；谓词是「一个整数变大」，与时钟无关）。
        self.assertTrue(
            _wait_until(lambda: script.subscriptions_started >= 2,
                        timeout=RENDEZVOUS_TIMEOUT_SECONDS),
            "观察者抛异常把重连循环也带走了 ⇒ 只订阅了一次就再也不试了。"
            "（本条跑到时只订阅了 %d 次）" % script.subscriptions_started,
        )

    def test_no_observer_is_the_default_and_changes_nothing(self):
        """⚠️ 单向可加：没给回调时行为与给回调前**逐字相同**。

        会红的条件：让 ``on_status_change=None`` 走成「记日志」或「记一条默认记录」。

        ⚠️ 顺带钉住 :attr:`SubscriptionStatus.frames_received` **收工后不再被加一次**
        （它逐帧加过；此前 ``run()`` 在收工那次又加了一遍 ⇒ 一次收到 1 帧的订阅
        会报成 2 帧，而那个数现在**显示在 ``--status`` 里**）。
        会红的条件：把收工那次 ``_update`` 的 ``frames_received=...`` 加回去。
        """
        script = SubscriptionScript([("frames", [{"type": "noise"}])])
        supervisor = SubscriptionSupervisor(
            subscribe=script.subscribe,
            on_frame=lambda event: None,
            first_delay=0.0,
            max_delay=0.0,
        )

        with watch_subscription_supervisor("will not come back") as (_handler, _fired):
            supervisor.run()

        self.assertEqual(supervisor.status().phase, PHASE_ENDED_WITHOUT_STOP)
        self.assertEqual(
            supervisor.status().frames_received, 1,
            "收工那一刻又把这个订阅收到的帧加了一遍 ⇒ 「--status」上那个"
            "「收到 N 帧」是双重计数。",
        )


def _wait_until(predicate, *, timeout: float) -> bool:
    """轮询一个**离散**谓词，带死锁守卫超时。

    ⚠️ 这是**唯一**一处带轮询的等待，而它等的是「一个整数变大」而不是「过了多久」
    ⇒ ⛔ 不构成时序断言（本机 16 ms 分辨率的坑在这里碰不到：谓词本身不依赖时钟）。
    ⛔ 超时只当**死锁守卫**，且失败时会响亮地报出来。
    """
    waiter = threading.Event()
    while not predicate():
        if waiter.wait(timeout=min(0.05, timeout)):
            return bool(predicate())
        timeout -= 0.05
        if timeout <= 0:
            return False
    return True


class TheStreamThreadRecoversTests(unittest.TestCase):
    """② :meth:`EventStream.run` **重新订阅**，而不是返回。"""

    def setUp(self) -> None:
        self.client = StreamThatRefuses()
        self.stream = build_event_stream(self.client)

    def test_run_resubscribes_after_a_refusing_event_endpoint(self):
        worker = threading.Thread(target=self.stream.run,
                                  name="sse-under-test", daemon=True)
        worker.start()
        self.addCleanup(worker.join, RENDEZVOUS_TIMEOUT_SECONDS)
        self.addCleanup(self.stream.request_stop)

        self.assertTrue(
            self.client.reconnected.wait(timeout=RENDEZVOUS_TIMEOUT_SECONDS),
            "看护者没有重新订阅 —— 它又回到缺陷形态了。",
        )
        self.assertGreaterEqual(self.client.subscribe_calls, 2)
        self.assertTrue(worker.is_alive(), "线程没有活过第一次失败。")
        self.assertFalse(
            self.stream.subscription_status().terminated_without_stop,
            "线程已经没了却被报成还在重连：phase=%r"
            % self.stream.subscription_status().phase,
        )

    def test_a_live_subscription_is_reported_as_streaming_not_as_reconnecting(self):
        """⭐ 状态必须**逐个**对得上，而不只是「能重连」。

        ⚠️ 判定点选在 :attr:`EventStream.stream_confirmed` 上是刻意的：它由
        :meth:`EventStream._consume_frame` 在**第一帧**置位，而那一刻看护者正卡在
        生成器里（:class:`StreamThatDeliversAFrameThenBlocks`）⇒ 读到的状态只有一个
        答案。⛔ 若换成"等订阅第二次被进入"，那 0.5 s 的退避窗口里读到的是什么全看
        调度 —— 那是一条会在别的机器上偶发变红的断言。
        """
        client = StreamThatDeliversAFrameThenBlocks()
        stream = build_event_stream(
            client,
            state=StateThatKnowsOneSession(CONVERSATION_ID, SESSION_ID),
        )
        worker = threading.Thread(target=stream.run, daemon=True)
        worker.start()
        try:
            self.assertTrue(
                stream.stream_confirmed.wait(timeout=RENDEZVOUS_TIMEOUT_SECONDS),
                "一帧都没等到。",
            )
            status = stream.subscription_status()

            self.assertEqual(status.phase, PHASE_STREAMING)
            self.assertFalse(status.recovering)
            self.assertEqual(status.frames_received, 1)
            self.assertEqual(client.subscribe_calls, 1)
        finally:
            stream.request_stop()
            client.release.set()
            worker.join(timeout=RENDEZVOUS_TIMEOUT_SECONDS)

    def test_run_never_marks_the_stream_live_without_a_frame(self):
        """恢复会「有订阅」这件事**不等于**「事件流通了」。

        ⛔ 所以 ``stream_confirmed`` 仍然不许在只重连、一帧没收到时被置上 ——
        收件箱恢复拿它当门闩，置早了就会在一条不通的流上重放。
        """
        worker = threading.Thread(target=self.stream.run, daemon=True)
        worker.start()
        self.addCleanup(worker.join, RENDEZVOUS_TIMEOUT_SECONDS)
        self.addCleanup(self.stream.request_stop)
        self.assertTrue(
            self.client.reconnected.wait(timeout=RENDEZVOUS_TIMEOUT_SECONDS),
            "看护者没有重新订阅 —— 下面那句「不许把流当通」就无从验证。",
        )

        self.assertFalse(
            self.stream.stream_confirmed.is_set(),
            "一帧都没收到，而信号却置上了 —— 收件箱恢复会以为事件流通了。",
        )

    def test_run_says_the_stream_terminated_and_names_the_retry(self):
        """⚠️ ``event stream terminated`` 这个子串**留着**：它是运维已经在抓的那句话。

        变的只是它后面多跟了「多久之后重连、第几次」—— 缺陷形态下这句话之后
        **什么也不发生**，所以光有它是不够的。
        """
        stream = build_event_stream(StreamThatRefuses())

        with watch_subscription_supervisor("event stream terminated") as (handler, fired):
            worker = threading.Thread(target=stream.run, daemon=True)
            worker.start()
            self.addCleanup(worker.join, RENDEZVOUS_TIMEOUT_SECONDS)
            self.addCleanup(stream.request_stop)
            self.assertTrue(
                fired.wait(timeout=RENDEZVOUS_TIMEOUT_SECONDS),
                "没有留下那行日志：%r" % (handler.messages,),
            )

        self.assertTrue(
            any("re-subscribing in" in message for message in handler.messages),
            "那行日志没说要重连、等多久：%r" % (handler.messages,),
        )
        self.assertEqual(stream.subscription_status().reconnect_attempts, 1)


class AnInFlightTurnSurvivesTheReconnectTests(unittest.TestCase):
    """⛔ 「不许丢在途 turn 的收尾」—— 重连发生在**那一轮进行中**时也要收得掉。

    ⚠️ 这条**刻意不**在断线时就把那一轮 finalize 掉：那一轮在 opencode 那边还在跑，
    提前收尾会把半截正文当最终答复发出去、再让续跑的 delta **另建一个 turn**
    ⇒ 同一条回复被发两遍（这正是 ``_on_execution_interrupted`` 对
    ``reason == "shutdown"`` 已经踩过并注释下来的那件事）。⇒ 修法是**保住那一轮**，
    让重连之后的终止事件去收它。
    """

    def test_the_turn_running_across_the_reconnect_is_finalized_once_with_all_its_text(self):
        finalized = threading.Event()
        published: list = []

        def record_finalize(conversation_id, handle, text, session_id, **kwargs):
            published.append({
                "conversation_id": conversation_id,
                "handle": handle,
                "text": text,
                "session_id": session_id,
            })
            finalized.set()

        client = StreamThatDropsMidTurn()
        stream = build_event_stream(
            client,
            state=StateThatKnowsOneSession(CONVERSATION_ID, SESSION_ID),
            finalize=record_finalize,
        )
        worker = threading.Thread(target=stream.run, name="sse-under-test", daemon=True)
        worker.start()
        try:
            self.assertTrue(
                client.resubscribed.wait(timeout=RENDEZVOUS_TIMEOUT_SECONDS),
                "看护者没有重新订阅。",
            )
            self.assertTrue(
                finalized.wait(timeout=RENDEZVOUS_TIMEOUT_SECONDS),
                "重连之后那一轮始终没有被收尾 —— 答复就这么丢了。",
            )
        finally:
            stream.request_stop()
            worker.join(timeout=RENDEZVOUS_TIMEOUT_SECONDS)

        self.assertEqual(len(published), 1, "收尾了 %d 次。" % len(published))
        self.assertEqual(published[0]["text"], "Hello world",
                         "收尾发出去的不是跨断线攒下的整段正文。")
        self.assertEqual(published[0]["conversation_id"], CONVERSATION_ID)
        self.assertEqual(published[0]["session_id"], SESSION_ID)
        self.assertIsNone(
            published[0]["handle"],
            "占位消息压根没发出去（send_text 返回 None），句柄理应是 None。",
        )
        self.assertEqual(
            stream._turns, {},
            "那一轮还留在 turn 表里 —— 收尾没有把它弹掉。",
        )


class ShutdownEndsTheReconnectLoopTests(unittest.TestCase):
    """⛔ 「不许线程泄漏」：退出路径必须能停掉重连循环。

    ⚠️ 退避被设成 **30 s**，而 ``join`` 的守卫只有 10 s ⇒ 这一条同时钉住了
    「等待可被立刻打断」：若退避用的是 :func:`time.sleep`，``join`` 会超时、
    线程会活过这条用例。
    """

    def test_request_stop_ends_a_run_that_is_waiting_out_its_backoff(self):
        script = SubscriptionScript([("fail", OpenCodeError("HTTP 503"))])
        supervisor = SubscriptionSupervisor(
            subscribe=script.subscribe,
            on_frame=lambda event: None,
            first_delay=30.0,
            max_delay=30.0,
        )
        worker = threading.Thread(target=supervisor.run,
                                  name="sse-shutdown-under-test", daemon=True)

        with watch_subscription_supervisor("re-subscribing") as (_handler, fired):
            worker.start()
            self.addCleanup(worker.join, RENDEZVOUS_TIMEOUT_SECONDS)
            self.addCleanup(supervisor.request_stop)
            self.assertTrue(
                fired.wait(timeout=RENDEZVOUS_TIMEOUT_SECONDS),
                "看护者没有宣布重连 —— 它根本没走到退避那一步。",
            )

            supervisor.request_stop()
            worker.join(timeout=RENDEZVOUS_TIMEOUT_SECONDS)

        self.assertFalse(worker.is_alive(),
                         "叫停之后线程还活着 —— 重连循环没有被停掉（退避是 30 s）。")
        self.assertEqual(supervisor.status().phase, PHASE_STOPPED_BY_REQUEST)
        self.assertEqual(
            script.subscriptions_started, 1,
            "叫停之后还去订阅了 %d 次 —— 退避循环没停。"
            % script.subscriptions_started,
        )


class TheBridgeRecoversTests(unittest.TestCase):
    """③ 全链：``BridgeCore`` 的 SSE 线程在端点拒绝服务时活下来并自愈。"""

    def setUp(self) -> None:
        os.makedirs(_LANE_TEMP, exist_ok=True)
        self.tempdir = tempfile.TemporaryDirectory(dir=_LANE_TEMP)
        self.addCleanup(self.tempdir.cleanup)
        self.client = StreamThatRefuses()
        self.core = BridgeCore(
            Config(), self.client,
            StateStore(os.path.join(self.tempdir.name, "state.json")),
        )
        self.addCleanup(self.core.stop)

    def test_the_stream_thread_survives_a_refusing_event_endpoint(self):
        self.core.start()
        thread = self.core._thread
        self.addCleanup(thread.join, RENDEZVOUS_TIMEOUT_SECONDS)
        self.assertIsNotNone(thread)
        self.assertTrue(
            self.client.reconnected.wait(timeout=RENDEZVOUS_TIMEOUT_SECONDS),
            "SSE 线程根本没有重新订阅。",
        )

        self.assertTrue(thread.is_alive(), "SSE 线程没有活过第一次失败。")
        self.assertGreaterEqual(self.client.subscribe_calls, 2)
        self.assertFalse(self.core.event_stream.stream_confirmed.is_set())

    def test_the_bridge_needs_no_resurrection_now_and_still_starts_one_stream(self):
        """⚠️ 上一条取代的是「再调一次 :meth:`BridgeCore.start` 救不活死线程」那条基线 ——
        **它失效的原因不是我们改了 ``start()``，而是那一轮不再会死。**

        ``start()`` 的 ``_started`` 闩一个字没动，所以这条断言守住的是另一半：
        **修复没有引入第二条拉起路径** ⇒ 仍然只有一个 :class:`EventStream`、
        只有一个看护者、一个重连循环。
        """
        self.core.start()
        thread = self.core._thread
        self.addCleanup(thread.join, RENDEZVOUS_TIMEOUT_SECONDS)
        self.assertTrue(self.client.reconnected.wait(RENDEZVOUS_TIMEOUT_SECONDS))
        stream = self.core.event_stream

        self.core.start()

        self.assertIs(self.core._thread, thread, "start() 换了另一条线程。")
        self.assertTrue(thread.is_alive(), "第二次 start() 把这条线程换掉了。")
        self.assertIs(
            self.core.event_stream, stream,
            "第二次 start() 造出了第二个 EventStream ⇒ 会有第二个重连循环。",
        )

    def test_asking_the_core_to_stop_ends_the_reconnect_loop(self):
        """⛔ 不许线程泄漏：``stop()`` 之后那条线程必须真的结束，且状态是「被叫停」。"""
        self.core.start()
        thread = self.core._thread
        self.addCleanup(thread.join, RENDEZVOUS_TIMEOUT_SECONDS)
        self.assertTrue(
            self.client.reconnected.wait(timeout=RENDEZVOUS_TIMEOUT_SECONDS),
            "SSE 线程根本没有重新订阅，谈不上停它。",
        )

        self.core.stop()

        self.assertFalse(
            thread.is_alive(),
            "shutdown 之后 SSE 线程还活着 —— 重连循环没有被停掉。",
        )
        self.assertEqual(
            self.core.event_stream.subscription_status().phase,
            PHASE_STOPPED_BY_REQUEST,
        )


def production_sources() -> list[tuple[str, ast.AST]]:
    """生产包里每个 ``.py`` 的 ``(文件名, 语法树)``。

    ⚠️ 扫描范围是「``opencode_bridge`` 包目录」，不是仓库根 —— 后者会把 ``tests/``
    与 ``plugin/`` 也算进来，于是「这条线程有几处被看护」的答案会取决于测试自己。
    """
    package_root = pathlib.Path(opencode_bridge.__file__).resolve().parent
    return [
        (path.name, ast.parse(path.read_text(encoding="utf-8")))
        for path in sorted(package_root.rglob("*.py"))
    ]


def stream_run_reference_sites() -> list[tuple[str, int, str, str]]:
    """``<…>.event_stream.run`` 的每一处引用 —— ⛔ **AST**，不是行级正则。

    ⚠️ 行级正则会漏：``target=self.event_stream.`` 换行接 ``run`` 就跨行了
    （AGENTS.md §7.1 那条「行级正则必然漏」的教训）。本函数**只**取
    ``ast.Attribute`` 上 ``attr == "run"`` 且点号链里带 ``event_stream`` 的节点 ——
    所以 ``self._supervisor.run()``（新看护者的线程主体）**不算**一处拉起。
    """
    sites = []
    for filename, tree in production_sources():
        parents = parent_map(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or node.attr != "run":
                continue
            if "event_stream" not in dotted_name(node):
                continue
            function, class_node = enclosing_function_and_class(node, parents)
            owner = "%s.%s" % (
                class_node.name if class_node else "<module>",
                function.name if function else "<module>",
            )
            sites.append((filename, node.lineno, dotted_name(node), owner))
    return sites


class SupervisingTheStreamThreadTests(unittest.TestCase):
    """③ 的**结构面**：只有一处拉起它，且没有任何人拿它的存活状态做判断。

    ⚠️ **这一整类在修复前后逐字未改**，而它仍然全绿 ⇒ 新看护者**没有**引入第二条
    拉起路径。看护不是「再起一个线程看着」，而是**同一个线程里**的一层循环。
    """

    def test_the_check_can_see_a_stream_run_reference_at_all(self):
        """⭐ 反「恒空」：先证明判据**看得见**这种引用，再拿它去数。

        ⚠️ 没有这条，一个「什么都看不见」的判据会恒空地通过 —— 而那正是
        「把扫描范围写错」这种变异的逃逸口（AGENTS.md §9）。
        """
        sample = ast.parse(
            "class Holder:\n"
            "    def start(self):\n"
            "        return self.event_stream.run\n"
        )
        parents = parent_map(sample)
        seen = [
            dotted_name(node) for node in ast.walk(sample)
            if isinstance(node, ast.Attribute) and node.attr == "run"
            and "event_stream" in dotted_name(node)
        ]
        self.assertEqual(seen, ["self.event_stream.run"])
        function, class_node = enclosing_function_and_class(
            next(node for node in ast.walk(sample)
                 if isinstance(node, ast.Attribute) and node.attr == "run"),
            parents,
        )
        self.assertEqual(class_node.name, "Holder")
        self.assertEqual(function.name, "start")

    def test_the_scan_also_sees_the_supervisor_and_the_supervisor_is_not_one(self):
        """⭐ 反「漏掉新模块」：先证明扫描**看得见** ``subscription_supervisor.py``。

        ⚠️ 没有这条，一个「新文件不在扫描范围里」的变异会**恒空地**通过下面那条
        「恰好 1 处」的断言 —— 那是 AGENTS.md §7.1「空集 ≠ 不存在」的同一个坑。
        """
        filenames = [name for name, _tree in production_sources()]

        self.assertIn("subscription_supervisor.py", filenames)
        self.assertIn("event_stream.py", filenames)

    def test_exactly_one_place_launches_the_stream_thread(self):
        sites = stream_run_reference_sites()

        self.assertEqual(
            len(sites), 1,
            "全仓有 %d 处引用 event_stream.run（%r）—— 每多一处就多一个可能"
            "重新拉起它的地方。" % (len(sites), sites),
        )
        self.assertEqual(sites[0][3], "BridgeCore.start")

    def test_no_place_consults_the_stream_threads_liveness(self):
        """``BridgeCore`` 里唯一的 ``is_alive()`` 在 ``stop()`` 里，且只用来告警。

        ⚠️ 口径先说清：**全包** ``is_alive()`` 散落在适配器轮询线程、进程 pid、
        传输层等处，所以「0 命中」那句话**对包不成立**；本条只问 ``core.py`` ——
        那条线程唯一可能被看护的地方。那里**恰好 1** 处，位于
        :meth:`BridgeCore.stop` 的 ``join`` 之后，只打一行 warning。

        ⚠️ **看护刻意没有落在这里**：重连由 SSE 线程**自己**那一层循环做，
        所以 core 既不需要也不该去问「那条线程还活着吗」—— 问了就是第二条策略。
        """
        core_sources = [
            (filename, tree) for filename, tree in production_sources()
            if filename == "core.py"
        ]
        self.assertEqual(
            [name for name, _ in core_sources], ["core.py"],
            "core.py 没被扫到 —— 判据恒空（AGENTS.md §7.1：空集 ≠ 不存在）。",
        )

        sites = []
        for filename, tree in core_sources:
            parents = parent_map(tree)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Attribute) or node.attr != "is_alive":
                    continue
                function, class_node = enclosing_function_and_class(node, parents)
                sites.append("%s.%s" % (
                    class_node.name if class_node else "<module>",
                    function.name if function else "<module>",
                ))

        self.assertEqual(
            sites, ["BridgeCore.stop"],
            "core.py 里的 is_alive() 落点变成了 %r —— 有人开始拿这条线程的存活"
            "状态做判断了。" % (sites,),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
"""Telegram 凭据闸门：``getMe`` 探测是「会话建立」的一部分，失败**带退避重试**。

被测的缺陷（已裁定）
--------------------
``telegram`` 是本项目**零容错通道**（硬约束：不允许出任何错），而改之前的
:meth:`~opencode_bridge.adapters.telegram.TelegramAdapter.start` 在 ``getMe``
失败后 ``report_startup_probe(VERDICT_FAILED, …)`` 然后 ``return``，
``self._transport`` 恒为 ``None`` ⇒ **入站 100% 死掉**。而生产代码里
``adapter.start()`` **只有 1 处调用点**（``core.BridgeCore.start``，被
``_started`` 守着）⇒ **没有任何重试入口** ⇒ 一次超时 / 一次 ``code=0``
（笔记本睡眠唤醒、代理刚起、DNS 未就绪）把入站**永久**掐到进程重启。

根因不是"忘了加重试"，而是 telegram 是 13 个适配器里**唯一**在 ``start()`` 里
加了一道**同步的、终局性**凭据闸门的；其余 12 个的鉴权在 ``Transport._open``
之后的握手里做，**抛异常 = 这次会话失败 → 退避重连**。

本文件钉住的就是那三条**不许回退**的约束：

1. **新增线程数 = 0** —— 重试循环跑在 ``Transport.start`` 建的**那条已经存在**
   的 daemon 线程里，且 ``stop()`` 能立刻打断它（见 :class:`TestNoNewThread`）。
2. **退避阶梯不落在 telegram 那个 ``PollingTransport`` 实例上** —— 它的
   ``min_backoff == max_backoff == 2.0`` 是迁移时逐字保留的语义，
   ``tests/test_telegram.py`` 有 3 条用例钉死；且 ``_next_backoff`` 的
   ``survived`` 由调用点判、轮询类 ``_open()`` 永远成功 ⇒ min == max 时
   **任何配置都升级不了退避** ⇒ 阶梯必须由适配器自己算。
3. **:meth:`~opencode_bridge.adapters.telegram.TelegramAdapter._flush_pending`
   每进程只跑一次** —— 它把 ``_offset`` 推到最后一个 ``update_id + 1``，
   语义是**永久丢弃历史**；跟着"探测变成会话建立的一部分"顺手搬进会话，
   网络抖一下就会吃掉断线期间的消息**而且不报错**。

⚠️ 本文件**不联网**：全部用替换 ``_post`` 的办法。
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import threading
import time
import unittest
from collections.abc import Callable

# 期望的 warning 不刷屏；``assertLogs`` 自己换 handler，不受影响。
logging.getLogger("opencode_bridge").addHandler(logging.NullHandler())

from opencode_bridge import health
from opencode_bridge.adapters.telegram import (
    BACKOFF_INTERVAL,
    CREDENTIAL_PROBE_INITIAL_BACKOFF,
    CREDENTIAL_PROBE_MAX_BACKOFF,
    TelegramAdapter,
)
from opencode_bridge.hooks import Inbound

#: 一个**形状上就不是**真 token 的占位值（真形状的夹具按 AGENTS.md §2.4 拼接，
#: 见 ``tests/test_platform_health.py``；这里不需要脱敏，故不需要真形状）。
FAKE_BOT_TOKEN = "123456789:not-a-real-token-abcdefghij"
CHAT_ID = 55

#: 这些重试行只在真出过故障时才有意义 —— 正常启动一条都不许有。
RETRY_LOG_MARKERS = ("将按退避重试", "将继续按退避重试", "探测恢复")


def wait_until(predicate, timeout: float = 5.0, interval: float = 0.005) -> bool:
    """轮询等条件成立（比裸 sleep 稳；失败信息由调用方的断言给出）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


class RecordingHooks:
    """只记 inbound 的最小 ``Hooks`` 实现（够本文件断言"入站有没有真的通"）。"""

    def __init__(self) -> None:
        self.inbounds: list[Inbound] = []

    def on_inbound(self, inbound: Inbound) -> None:
        self.inbounds.append(inbound)

    def on_callback(self, conversation_id: str, data: str, query_id: str) -> None:
        return None


class FakePlatform:
    """把 ``_post`` 换成可编排的假平台，并记下**每一次**请求的时刻与方法。

    ⛔ **不许**把这三张表合成一张：``getMe`` 的次数/时刻是阶梯的判据，
    ``getUpdates`` 的 offset 是"历史被丢了几次"的判据，flush 调用次数又是
    第三件事 —— 合在一起就分不出是谁的问题了（AGENTS.md §7.1）。
    """

    def __init__(self, adapter, *, get_me=None, get_updates=None, flush=None) -> None:
        #: ``get_me(attempt) -> 响应``；``attempt`` 从 **1** 起（``start()`` 里
        #: 那次同步探测就是第 1 次）。
        self._get_me = get_me or (lambda attempt: {"ok": True, "result": {"id": 1}})
        self._get_updates = get_updates or (lambda offset: {"ok": True, "result": []})
        self._flush = flush or (lambda: {"ok": True, "result": [{"update_id": 100}]})
        #: 每次 ``getMe`` 的时刻（monotonic）—— 阶梯判据的原始数据。
        self.get_me_at: list[float] = []
        #: 每次**常规** ``getUpdates`` 带过的 offset（flush 的 ``-1`` 不算）。
        self.get_updates_offsets: list[object] = []
        #: flush（``offset == -1``）被调用的时刻；**长度就是"历史被丢了几次"**。
        self.flush_at: list[float] = []
        adapter._post = self.post

    def post(self, method, payload=None, *, timeout=None):
        payload = dict(payload or {})
        if method == "getMe":
            self.get_me_at.append(time.monotonic())
            return self._get_me(len(self.get_me_at))
        if method == "getUpdates":
            if payload.get("offset") == -1:
                self.flush_at.append(time.monotonic())
                return self._flush()
            self.get_updates_offsets.append(payload.get("offset"))
            return self._get_updates(payload.get("offset"))
        return {"ok": True, "result": {}}


def make_adapter(config: dict | None = None, hooks: RecordingHooks | None = None):
    settings = {"bot_token": FAKE_BOT_TOKEN}
    if config:
        settings.update(config)
    adapter = TelegramAdapter(settings, hooks or RecordingHooks())
    adapter.min_interval = 0            # 测试里不要人为 sleep
    return adapter


class RecordingStopEvent(threading.Event):
    """一个**记账版**的 ``_stop_event``：记下 ``wait()`` 每次**被请求**的秒数。

    ⛔ **为什么记「请求值」而不是「实测量」**：``Event.wait(x)`` 的 ``x`` 是
    :meth:`~opencode_bridge.adapters.telegram.TelegramAdapter._credential_probe_backoff_for`
    这个**纯函数**的输出（就是生产侧那处等待的入参），而**实测量**在机器有负载时
    会被调度放大 —— 本机实测一次 ``wait(0.2)`` 在全量并发下回来是 **1.25s**
    （差 6 倍，远超 16ms 定时器分辨率能解释的量）⇒ 任何「观察到的间隔要落在某个
    区间里」的判据在负载下必然时红时绿（全量连跑 3 遍：第一遍红、第二三遍绿）。
    **逐项等值断言对机器负载免疫**（AGENTS.md §7.1：时序判据只能用同步点，
    ⛔ 不要拿计时当时序断言）。

    ⚠️ 它是 ``threading.Event`` 的**子类**而不是代理：``set()`` / ``clear()`` /
    ``is_set()`` 全部照原样可用 ⇒ **``stop()`` 仍然立刻能打断阶梯等待**
    （:meth:`TestNoNewThread.test_stop_interrupts_the_gate_wait_promptly` 钉的就是
    那条；换成只实现 ``wait`` 的代理就会把它悄悄弄坏）。

    ⚠️ 第二列是**离散顺序日志**、不是计时：它读 :class:`FakePlatform` 那张**次数**
    账，而 ``get_me`` 回调收到的 ``attempt`` 本来就是 ``len(get_me_at)``
    ⇒ 这一列对机器负载免疫，而「交棒那一跳没有先白等一档」这条判据就落在它身上。
    """

    def __init__(self, attempts_so_far: Callable[[], int]) -> None:
        super().__init__()
        #: ``(被请求的秒数, 请求时已经发生过几次 getMe)``，按请求先后排列。
        self.requested_waits: list[tuple[float, int]] = []
        self._attempts_so_far = attempts_so_far

    def wait(self, timeout: float | None = None) -> bool:
        """先记账，再**原样**转交真正的等待（返回值语义一个字没变）。"""
        self.requested_waits.append((timeout, self._attempts_so_far()))
        return super().wait(timeout)


def record_requested_waits(
    adapter: TelegramAdapter, platform: FakePlatform
) -> RecordingStopEvent:
    """把适配器的 ``_stop_event`` 换成记账版（**必须在** ``start()`` **之前**调）。

    ⚠️ 换掉的是「谁在看等待」，不是「谁能打断它」—— ``RecordingStopEvent`` 仍是
    真正的 ``threading.Event``，``start()`` 里的 ``clear()`` 与 ``stop()`` 里的
    ``set()`` 照常作用在它身上。
    """
    ledger = RecordingStopEvent(lambda: len(platform.get_me_at))
    adapter._stop_event = ledger
    return ledger


def fast_ladder(adapter: TelegramAdapter, initial: float, ceiling: float) -> None:
    """把凭据阶梯缩到测试能等得起的尺度（**只**缩这一个旋钮）。"""
    adapter.credential_probe_initial_backoff = initial
    adapter.credential_probe_max_backoff = ceiling


def message_update(update_id: int, text: str = "hi") -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "chat": {"id": CHAT_ID, "type": "private"},
            "from": {"id": 7, "is_bot": False, "first_name": "u"},
            "text": text,
        },
    }


def transient_getme_response() -> dict:
    """``_post`` 在 socket 超时时**真的**造出来的那一份（见 ``_post``）。"""
    return {
        "ok": False,
        "error_code": 0,
        "description": "transport error: <urlopen error timed out>",
    }


def rejected_getme_response() -> dict:
    return {"ok": False, "error_code": 401, "description": "Unauthorized"}


def count_transport_threads() -> int:
    return len([t for t in threading.enumerate() if t.name == "transport:telegram"])


class TestTransientFailureRecoversWithoutRestart(unittest.TestCase):
    """① 一次瞬时 ``getMe`` 失败后，**不重启进程**也能恢复入站（核心完成判据）。"""

    def test_one_timed_out_getme_still_lets_inbound_through(self):
        """注入**一次**超时（``error_code: 0``）⇒ 同一个进程内入站自己起来。

        改之前这一步**做不到**：``start()`` 直接 ``return``、``self._transport``
        恒 ``None``，而生产里没有任何重试入口 ⇒ 入站死到进程重启。
        """
        hooks = RecordingHooks()
        adapter = make_adapter(hooks=hooks)
        fast_ladder(adapter, 0.02, 0.05)
        platform = FakePlatform(
            adapter,
            get_me=lambda attempt: (
                transient_getme_response() if attempt == 1
                else {"ok": True, "result": {"id": 1}}
            ),
            get_updates=lambda offset: (
                {"ok": True, "result": [message_update(101)]}
                if offset in (0, 101) and not hooks.inbounds else
                {"ok": True, "result": []}
            ),
        )
        adapter.start()
        self.addCleanup(adapter.stop)

        self.assertTrue(
            wait_until(lambda: len(hooks.inbounds) >= 1, timeout=5.0),
            "瞬时失败之后入站必须自己恢复 —— 不重启进程、不碰 BridgeCore",
        )
        self.assertEqual(hooks.inbounds[0].text, "hi")
        self.assertTrue(adapter.running, "凭据闸门过了之后 running 必须为真")
        self.assertGreaterEqual(
            len(platform.get_me_at), 2,
            "必须有第二次 getMe（第一次失败不能是最后一次）",
        )

    def test_recovery_does_not_rewrite_the_startup_verdict_snapshot(self):
        """⚠️ **线程不许改** ``startup_verdict`` —— 运行器在 ``start()`` 返回那一刻
        同步读走它；让线程去改，落盘的值就取决于线程调度。

        顺带钉住那条**反转**：「上次启动时的探测结论」这个标签今天**只**因为
        "永不重试"才为真（文件启动那刻写完就永不改写，而那时"启动时失败"
        恰好等价于"永远收不到消息"）⇒ **有了重试，它才第一次真的只回答启动那一刻
        的问题**，恢复与否改由日志承担。
        """
        adapter = make_adapter()
        fast_ladder(adapter, 0.01, 0.02)
        FakePlatform(
            adapter,
            get_me=lambda attempt: (
                transient_getme_response() if attempt == 1
                else {"ok": True, "result": {"id": 1}}
            ),
        )
        adapter.start()
        self.addCleanup(adapter.stop)

        snapshot = dict(adapter.startup_verdict)
        self.assertEqual(snapshot["verdict"], health.VERDICT_FAILED)
        self.assertEqual(snapshot["code"], 0)
        self.assertTrue(wait_until(lambda: adapter.running, timeout=5.0))
        self.assertEqual(
            dict(adapter.startup_verdict), snapshot,
            "传输线程不许改写 start() 那一轮上报的结论（落盘值会变成竞态）",
        )


class TestNoNewThread(unittest.TestCase):
    """约束①：新增线程数 = 0，重试循环跑在**已经存在**的那条 transport 线程里。"""

    def test_one_start_creates_exactly_one_thread_while_the_gate_is_retrying(self):
        before = count_transport_threads()
        adapter = make_adapter()
        fast_ladder(adapter, 0.05, 0.2)
        platform = FakePlatform(
            adapter, get_me=lambda attempt: transient_getme_response()
        )
        adapter.start()
        self.addCleanup(adapter.stop)
        self.assertTrue(
            wait_until(lambda: len(platform.get_me_at) >= 3, timeout=5.0),
            "闸门必须真的在反复重试（否则这条用例什么都没证明）",
        )
        self.assertEqual(
            count_transport_threads(), before + 1,
            "重试循环不许自己起线程 —— 桥关闭时就不会有悬挂的探测线程",
        )
        self.assertIsNone(
            adapter._thread,
            "⛔ 线程归 Transport 所有，适配器不许另存一条（那会让 stop() 管不到它）",
        )

    def test_stop_interrupts_the_gate_wait_promptly(self):
        """阶梯 park 在 ``_stop_event.wait()`` 上 ⇒ ``stop()`` 立刻可打断。

        这是"根本没有新线程"的**另一半**：不 park 的实现（``time.sleep``）会让
        ``stop()`` 白等满一整档，生产里那就是关桥时多等一分钟。
        """
        adapter = make_adapter()
        adapter.credential_probe_initial_backoff = 30.0   # 故意设很大
        adapter.credential_probe_max_backoff = 30.0
        platform = FakePlatform(
            adapter, get_me=lambda attempt: transient_getme_response()
        )
        adapter.start()
        self.addCleanup(adapter.stop)
        self.assertTrue(wait_until(lambda: len(platform.get_me_at) >= 2, timeout=5.0))
        began = time.monotonic()
        adapter.stop()
        self.assertLess(
            time.monotonic() - began, 2.0,
            "stop() 必须立刻打断阶梯等待（不许退化成 time.sleep）",
        )
        self.assertFalse(adapter.running)


class TestProbeLadder(unittest.TestCase):
    """② / ③：阶梯是初值 2s → ``×2`` → 封顶 60s、**永不放弃**。"""

    def test_ladder_starts_at_two_doubles_and_caps_at_sixty(self):
        """阶梯**本身**逐项钉死（不是"重试了三次"这种弱判据）。"""
        adapter = make_adapter()
        self.assertEqual(adapter.credential_probe_initial_backoff, 2.0)
        self.assertEqual(adapter.credential_probe_max_backoff, 60.0)
        # 「初值是复用仓库里已有的 2s」这句话是**被钉住的事实**，不是注释里的说法。
        self.assertEqual(
            adapter.credential_probe_initial_backoff, BACKOFF_INTERVAL,
            "初值复用仓库既有的 2s（transport/base.py 的基类不变式也是 ×2）",
        )
        self.assertEqual(
            [adapter._credential_probe_backoff_for(n) for n in range(1, 9)],
            [2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0],
            "初值 → ×2 → 封顶 60s；封顶之后不再涨、也不回落",
        )

    def test_ladder_survives_an_absurd_attempt_count(self):
        """⚠️ "永不放弃" ⇒ 跑几个月就有几百万次 ⇒ ``2.0 ** n`` 会 ``OverflowError``。

        那会让"永不放弃"这条设计**自己**引入一个新的失败模式。
        """
        adapter = make_adapter()
        self.assertEqual(adapter._credential_probe_backoff_for(10 ** 7), 60.0)

    def test_retries_land_on_the_laddered_moments_not_merely_repeated(self):
        """端到端：**次数与阶梯**都要对上（钉住"钉住次数"这个要求本身）。

        ⛔ 判据是「**被请求的**延迟」，⛔ **不是「观察到的间隔」**：``Event.wait(x)``
        的 ``x`` 是 :meth:`~opencode_bridge.adapters.telegram.TelegramAdapter._credential_probe_backoff_for`
        这个**纯函数**的输出，而**实测量**在机器有负载时会被调度放大（本机实测
        ``wait(0.2)`` 回来是 **1.25s**，差 6 倍，远超 16ms 定时器分辨率能解释的量）
        ⇒ 「间隔要落在某个区间里」那种判据在负载下必然时红时绿（全量连跑 3 遍：
        第一遍红、第二三遍绿），而**逐项等值断言对机器负载免疫**。
        AGENTS.md §7.1：时序判据只能用同步点（``Event`` / ``Barrier`` / 离散顺序
        日志），⛔ 不要拿计时当时序断言。
        """
        adapter = make_adapter()
        fast_ladder(adapter, 0.05, 0.2)
        platform = FakePlatform(
            adapter, get_me=lambda attempt: transient_getme_response()
        )
        ladder_ledger = record_requested_waits(adapter, platform)
        adapter.start()
        self.addCleanup(adapter.stop)

        expected_attempts = 6          # 1 次同步 + 1 次立刻接手 + 4 次阶梯等待之后
        # ⛔ 下面这两条都是**死线守卫**（死锁时要 fail 得响亮），⛔ **不是时刻判据**：
        # 它们只回答「阶梯真的重试过这么多吗」，「是哪几档」由后面那条等值断言回答。
        self.assertTrue(
            wait_until(lambda: len(platform.get_me_at) >= expected_attempts, timeout=5.0),
            f"必须重试到第 {expected_attempts} 次。实际：{len(platform.get_me_at)}",
        )
        self.assertTrue(
            wait_until(lambda: len(ladder_ledger.requested_waits) >= 4, timeout=5.0),
            "阶梯必须真的 park 过 4 次，否则下面断言的是一个空列表（恒真）。"
            f"实际：{ladder_ledger.requested_waits!r}",
        )
        # 阶梯被缩到 0.05 / 0.2 ⇒ **被请求的**延迟必须是 [0.05, 0.1, 0.2, 0.2]
        # （初值、×2、封顶、封顶）—— 逐项**等值**，不是区间。
        # 第二列（请求时已经发生过几次 ``getMe``）把「交棒那一跳」也钉住了：
        # 「``start()`` 里那次同步失败」是第 1 次，**交棒**给
        # :meth:`~opencode_bridge.adapters.telegram.TelegramAdapter._probe_until_credentials_verified`
        # 里那次（= 它自己的第 1 次尝试，全局第 2 次）之前**一次等待都没有**
        # ⇒ 第一项的第二列必须是 2；先等一档再交棒会写成 1。
        self.assertEqual(
            ladder_ledger.requested_waits[:4],
            [(0.05, 2), (0.1, 3), (0.2, 4), (0.2, 5)],
            "阶梯必须逐项等于 初值 → ×2 → 封顶 → 封顶（0.05 / 0.1 / 0.2 / 0.2），"
            "且每一档都在**它自己那次失败之后**才被请求。实际请求序列"
            "（被请求的秒数, 请求时已发生的 getMe 次数）："
            f"{ladder_ledger.requested_waits[:6]!r}\n"
            "读法：① 第一项是**初值**那一档 ⇒「阶梯整体平移了一档 / 4s 起步」"
            "（初值被跳过）在这里变红；② 第二列连号 2,3,4,5 ⇒ 少一档（阶梯被跳过）"
            "时第 4 项会落在 6 而不是 5，退化成恒定 0.05 时第一列全是 0.05；"
            "③ 第一项第二列是 2 ⇒ 交棒那一跳没有先白等一档。",
        )

    def test_giving_up_is_not_an_option_even_after_many_failures(self):
        """③ **永不放弃**：连续失败 9 次之后仍在重试。

        为什么不给放弃点：改 ``config.json`` **不会**重启桥 ⇒ 设放弃点等于把
        用户唯一的自助修法（改完 token 等它生效）变成"必须重启进程"。
        """
        adapter = make_adapter()
        fast_ladder(adapter, 0.005, 0.01)
        platform = FakePlatform(
            adapter, get_me=lambda attempt: rejected_getme_response()
        )
        adapter.start()
        self.addCleanup(adapter.stop)

        self.assertTrue(
            wait_until(lambda: len(platform.get_me_at) >= 9, timeout=10.0),
            "连续失败 8 次之后必须仍在重试（401 也不例外）。实际："
            f"{len(platform.get_me_at)}",
        )


class TestHistoryIsDroppedOncePerProcess(unittest.TestCase):
    """约束③：``_flush_pending()`` **每进程只跑一次**，不是每会话一次。"""

    def test_a_second_session_does_not_drop_history_again(self):
        """⭐ 判据：第二次会话建立之后，**历史消息仍只被丢弃一次**。

        ⚠️ 这条正是"很容易顺手做错"的那一处：探测变成"会话建立的一部分"之后，
        丢弃历史看起来就该也搬进会话 ⇒ 网络抖一下就吃掉断线期间到达的消息，
        **而且不报错**。

        搭法：① 第 1 次 ``getMe`` 失败、第 2 次成功（此时丢历史一次，游标推到 101）；
        ② 轮询投递 update 101（游标 102）；③ ``getUpdates`` 开始失败 → **会话断开**，
        断线期间"平台侧"攒下 update 105；④ 重连。
        ⇒ 判据有两个：flush 只被调**一次**；且重连后第一轮带的是 ``offset=102``
        （而不是被第二次 flush 推到 106 ⇒ 105 被吃掉）。
        """
        hooks = RecordingHooks()
        adapter = make_adapter(hooks=hooks)
        adapter.backoff_interval = 0.02          # 只缩重连间隔（min 仍 == max）
        fast_ladder(adapter, 0.02, 0.05)
        outage_rounds = {"count": 0}

        def get_updates(offset):
            if offset == 101:                    # 首次连通：投一条，游标变 102
                return {"ok": True, "result": [message_update(101)]}
            if offset == 102:                    # 会话断开那一轮（**只**这一轮）
                outage_rounds["count"] += 1
                if outage_rounds["count"] == 1:
                    return {"ok": False, "error_code": 500, "description": "boom"}
                # 重连后的第一轮：断线期间到达的 update 105 必须被收到。
                # ⛔ 若历史被丢了第二次，游标会被推到 106 ⇒ 收不到 105 —— 而
                # **一条消息被静默丢弃时不会有任何报错**，那正是要防的形态。
                return {"ok": True, "result": [message_update(105)]}
            return {"ok": True, "result": []}

        platform = FakePlatform(
            adapter,
            get_me=lambda attempt: (
                transient_getme_response() if attempt == 1
                else {"ok": True, "result": {"id": 1}}
            ),
            get_updates=get_updates,
            flush=lambda: {"ok": True, "result": [{"update_id": 100}]},
        )
        adapter.start()
        self.addCleanup(adapter.stop)

        self.assertTrue(
            wait_until(lambda: any(i.text == "hi" for i in hooks.inbounds), timeout=5.0),
            "先要真的连通（第一次 flush 把游标推到 101）",
        )
        self.assertTrue(
            wait_until(lambda: adapter._offset >= 106, timeout=5.0),
            "重连之后必须收到断线期间到达的 update 105。实际游标："
            f"{adapter._offset!r}，收过的 offset：{platform.get_updates_offsets!r}",
        )
        self.assertEqual(
            len(platform.flush_at), 1,
            "⛔ 丢弃历史是**永久**语义（游标被推到最后一个 update_id + 1），"
            "每进程只许发生一次。实际 flush 次数："
            f"{len(platform.flush_at)}；会话经历：{platform.get_updates_offsets!r}",
        )
        self.assertTrue(
            wait_until(lambda: len(platform.get_updates_offsets) >= 3, timeout=5.0),
            "判据需要**第二次会话真的建立过**，否则「只 flush 一次」是恒真的",
        )
        self.assertEqual(
            len(platform.flush_at), 1,
            "第二次会话建立之后仍不许再丢一次历史",
        )
        self.assertIn(
            102, platform.get_updates_offsets,
            f"重连那一轮必须带游标 102（= 101 + 1），实际："
            f"{platform.get_updates_offsets!r}",
        )


class TestProbeLogShape(unittest.TestCase):
    """⑤ / ⑥：恢复行要响亮；日志分两层（首次 ERROR + 修复指引，重复 WARNING + 计数）。"""

    def test_recovery_line_reports_the_attempt_count_and_the_elapsed_time(self):
        """⑤ 一次失败之后第一次成功时，打一行带「第 N 次尝试 / 历时 T 秒」的行。

        ⚠️ 这一行同时是**标定阶梯初值**的数据源（需要的数写在
        ``CREDENTIAL_PROBE_INITIAL_BACKOFF`` 的注释里），所以它必须带**次数**，
        而不只是"好了"。
        """
        adapter = make_adapter()
        fast_ladder(adapter, 0.05, 0.2)
        FakePlatform(
            adapter,
            get_me=lambda attempt: (
                transient_getme_response() if attempt < 4
                else {"ok": True, "result": {"id": 1}}
            ),
        )
        adapter.start()
        self.addCleanup(adapter.stop)

        with self.assertLogs(
            "opencode_bridge.adapters.telegram", level="WARNING"
        ) as captured:
            self.assertTrue(
                wait_until(lambda: adapter.running, timeout=5.0),
                "必须真的恢复过（否则下面断言的是空的）",
            )
        recovery = [r for r in captured.records if "探测恢复" in r.getMessage()]
        self.assertEqual(
            len(recovery), 1, f"恢复行必须恰好一条。实际：{captured.output!r}"
        )
        message = recovery[0].getMessage()
        self.assertIn("getMe", message, "用户搜的是 getMe 这条既有排障路径")
        self.assertIn("第 4 次尝试", message)
        self.assertIn("历时", message)
        self.assertRegex(message, r"历时 \d+(\.\d+)? 秒")
        self.assertGreaterEqual(
            recovery[0].levelno, logging.WARNING,
            "恢复行必须**响亮**：事故的两端（失败 / 恢复）要落在同一档位上，"
            "用户在故障期间最常做的动作是把档位提到 WARNING 来抓现场",
        )

    def test_first_failure_is_error_with_a_fix_hint_and_repeats_are_warning(self):
        """⑥ 首次失败 ERROR + 修复指引；重复失败降到 WARNING 且带尝试计数。

        ⚛️ 分类（:func:`~opencode_bridge.adapters.base.classify_http`）**只**用来选
        档位与文案，**绝不**用来门控重试 —— 两类都重试。
        """
        adapter = make_adapter()
        fast_ladder(adapter, 0.02, 0.05)
        platform = FakePlatform(
            adapter, get_me=lambda attempt: rejected_getme_response()
        )
        with self.assertLogs(
            "opencode_bridge.adapters.telegram", level="WARNING"
        ) as captured:
            adapter.start()
            self.assertTrue(
                wait_until(lambda: len(platform.get_me_at) >= 3, timeout=5.0),
                "判据需要至少两次重复失败。实际：%d" % len(platform.get_me_at),
            )
        self.addCleanup(adapter.stop)

        first_failures = [
            r for r in captured.records
            if "探测未通过" in r.getMessage()
        ]
        self.assertEqual(
            len(first_failures), 1,
            f"首次失败只许有一条。实际：{[r.getMessage() for r in first_failures]!r}",
        )
        self.assertEqual(first_failures[0].levelno, logging.ERROR)
        hint = first_failures[0].getMessage()
        self.assertIn("bot_token", hint, "FORBIDDEN/NOT_FOUND 必须给「检查/更新 bot_token」")
        self.assertIn("检查", hint)

        repeats = [
            r for r in captured.records
            if "仍失败" in r.getMessage()
        ]
        self.assertGreaterEqual(
            len(repeats), 2,
            f"重复失败必须降档并带计数。实际：{[r.getMessage() for r in repeats]!r}",
        )
        for record in repeats:
            self.assertEqual(
                record.levelno, logging.WARNING,
                f"重复失败不该继续打 ERROR：{record.getMessage()}",
            )
        self.assertIn(
            "第 2 次尝试", repeats[0].getMessage(),
            "重复失败必须带尝试计数",
        )

    def test_transient_failure_still_asks_the_user_to_check_the_network(self):
        """``error_code: 0``（= 没拿到状态码）⇒ 瞬时 ⇒ 文案说网络，不说 token。"""
        adapter = make_adapter()
        fast_ladder(adapter, 0.02, 0.05)
        FakePlatform(
            adapter, get_me=lambda attempt: transient_getme_response()
        )
        with self.assertLogs(
            "opencode_bridge.adapters.telegram", level="WARNING"
        ) as captured:
            adapter.start()
        self.addCleanup(adapter.stop)

        first_failure = [
            r for r in captured.records if "探测未通过" in r.getMessage()
        ]
        self.assertEqual(len(first_failure), 1)
        message = first_failure[0].getMessage()
        self.assertEqual(first_failure[0].levelno, logging.ERROR)
        self.assertIn("网络", message)
        self.assertNotIn("检查/更新", message)


class TestReverseGuardrails(unittest.TestCase):
    """⛔ 两条反向护栏：没有故障时**不许**出现重试行与阶梯等待。"""

    def test_a_probe_that_passes_first_try_waits_nothing_and_logs_no_retry(self):
        """键已注册 / 探测一次就通过 ⇒ 零重试、零阶梯等待、零重试行。

        阶梯初值是 **2s** ⇒ 这条判据的分辨率足够：只要误等了一档，
        ``getUpdates`` 就会晚 2s 以上才被调用。
        """
        adapter = make_adapter()
        platform = FakePlatform(
            adapter,
            get_me=lambda attempt: {"ok": True, "result": {"id": 1}},
            get_updates=lambda offset: {"ok": True, "result": []},
        )
        with self.assertLogs(
            "opencode_bridge.adapters.telegram", level="INFO"
        ) as captured:
            adapter.start()
            self.assertTrue(
                wait_until(lambda: len(platform.get_updates_offsets) >= 1, timeout=5.0),
                "探测通过后必须立刻开始轮询",
            )
        self.addCleanup(adapter.stop)

        self.assertEqual(
            len(platform.get_me_at), 1,
            "探测一次就通过 ⇒ 只该打一次 getMe（不许每次重连都重探）",
        )
        self.assertEqual(
            len(platform.flush_at), 1,
            "正常路径下历史也只丢一次",
        )
        self.assertLess(
            platform.flush_at[0] - platform.get_me_at[0], 1.0,
            "⛔ 探测通过 ⇒ 不许等任何一档阶梯（初值就是 2s）",
        )
        offenders = [
            line for line in captured.output
            if any(marker in line for marker in RETRY_LOG_MARKERS)
        ]
        self.assertEqual(
            offenders, [],
            "没有故障就不许出现任何重试/恢复行。实际：\n" + "\n".join(offenders),
        )

    def test_reconnects_after_a_polled_session_do_not_reprobe(self):
        """``getUpdates`` 失败引起的重连**不许**重新打 ``getMe``。

        否则一次 5xx 就会把 ``getMe`` 的调用量放大成"每次重连一次"，
        而限流按 bot token 计。
        """
        adapter = make_adapter()
        adapter.backoff_interval = 0.01
        platform = FakePlatform(
            adapter,
            get_me=lambda attempt: {"ok": True, "result": {"id": 1}},
            get_updates=lambda offset: {"ok": False, "error_code": 500,
                                        "description": "boom"},
        )
        adapter.start()
        self.addCleanup(adapter.stop)
        self.assertTrue(
            wait_until(lambda: len(platform.get_updates_offsets) >= 3, timeout=5.0),
            f"判据需要至少三次轮询。实际：{platform.get_updates_offsets!r}",
        )
        self.assertEqual(
            len(platform.get_me_at), 1,
            "⛔ 启动之后的每次重连都重新 getMe ⇒ 一次抖动就把探测量放大成无限",
        )
        self.assertEqual(len(platform.flush_at), 1)


class TestPlatformHealthContractIsUnchanged(unittest.TestCase):
    """⛔ 反向护栏：``platform-health.json`` 的形态与 ``VERDICTS`` 取值域**一个字没变**。"""

    #: 与 ``tests/test_outbound_failure_channel.py::EXPECTED_VERDICTS`` 同源，
    #: 且**按序** —— ``VERDICTS`` 是有序序列，外部护栏按序断言它。
    EXPECTED_VERDICTS = ("ok", "failed", "skipped", "not_started", "does_not_probe")

    def test_the_verdict_domain_has_not_grown_a_retrying_tier(self):
        """⛔ 重试**不允许**长成新的一档（例如"正在重试"）。

        理由：``VERDICTS`` 是有序序列且有仓库外的护栏按序断言，
        中间插一档会让那份 diff 看起来像"重排"，把一次纯新增说成一串改写。
        """
        self.assertEqual(tuple(health.VERDICTS), self.EXPECTED_VERDICTS)
        self.assertEqual(len(health.VERDICTS), 5)

    def test_every_verdict_the_gate_produces_is_inside_the_existing_domain(self):
        """跑一遍"失败 → 恢复"，本适配器产出的结论必须全都落在既有取值域内。"""
        adapter = make_adapter()
        fast_ladder(adapter, 0.02, 0.05)
        FakePlatform(
            adapter,
            get_me=lambda attempt: (
                transient_getme_response() if attempt == 1
                else {"ok": True, "result": {"id": 1}}
            ),
        )
        adapter.start()
        self.addCleanup(adapter.stop)
        self.assertTrue(wait_until(lambda: adapter.running, timeout=5.0))

        entry = health.probe_after_start(adapter)
        self.assertIsNotNone(entry, "telegram 上报了结论（不许变成 does_not_probe）")
        self.assertIn(entry["verdict"], health.VERDICTS)
        self.assertLessEqual(
            set(entry) - {"code"}, {"verdict", "detail"},
            "落盘条目的键集合不许多出新的一层（改一个功能只碰一个文件）",
        )

    def test_the_persisted_document_keeps_exactly_its_two_top_level_keys(self):
        """落盘形态逐键不变（文件名取自 :mod:`opencode_bridge.health`，不写死）。"""
        adapter = make_adapter()
        adapter.report_startup_probe(
            health.VERDICT_FAILED, code=0, detail="getMe: transport error"
        )
        scratch = tempfile.mkdtemp(
            prefix="telegram-credential-gate-",
            # ⛔ 永远显式给 dir= —— 裸 tempfile.mkdtemp() 会落到系统临时目录，
            # 而那是本项目明确禁用的一种"通用临时目录"。
            dir=os.environ.get("TEMP") or tempfile.gettempdir(),
        )
        self.addCleanup(
            lambda: shutil.rmtree(scratch, ignore_errors=True)
        )

        import json

        written = health.record_startup_probes(
            scratch, {"telegram": adapter.startup_verdict}
        )
        self.assertIsNotNone(written)
        with open(written, "r", encoding="utf-8-sig") as handle:
            document = json.load(handle)
        self.assertEqual(set(document), {"recorded_at", "platforms"})
        entry = health.probe_from_record(document, "telegram")
        self.assertEqual(entry["verdict"], health.VERDICT_FAILED)
        self.assertEqual(entry["code"], 0)
        self.assertLessEqual(set(entry) - {"code"}, {"verdict", "detail"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

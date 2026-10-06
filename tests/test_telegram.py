"""Telegram adapter 的 **A1 迁移不变量**（无网络：HTTP 层替换 ``_post``）。

A1 把轮询循环 / 退避 / 线程 / ``stop()`` 语义搬到了
:class:`~opencode_bridge.transport.PollingTransport`。本文件把「行为不变」
逐条钉死：

* 退避接线值（**2s 恒定**，不是指数）、``reset_after=0``、空轮节流 0.05s；
* ``stop()`` 快（有耗时上界）且在途请求返回后不泄漏线程；
* **offset 先推进再分发**（单条处理抛异常也不重放）、失败时 offset 一字不动；
* 长轮询两层超时的数值与大小关系（socket 超时 > 服务端挂起时长）；
* 入站过滤逐条（含 ``allowed_updates`` 与客户端过滤是**两处不同机制**）；
* 按钮能力（``callback_query`` → ``answer()``）一条不少；
* 以及一条**端到端**用例：切换前落盘的 ``chat:`` 键在 ``state.json`` 的键迁移
  之后仍取回同一个会话（切前缀必须与 ``migrate_keys=True`` 同一个变更上线）。

⚠️ 本项目铁律：断言"等了多久"一律**优先断言内部状态**（退避状态机 / 请求载荷 /
计数器），墙钟只允许做**下界**断言（等待只会更长，绝不会更短）或 ``stop()``
耗时这种**上界**断言（且要给得宽松）。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
import unittest

# Keep expected-failure warnings out of the test output; assertLogs still
# works because it swaps in its own handler on the target logger.
logging.getLogger("opencode_bridge").addHandler(logging.NullHandler())

from opencode_bridge.adapters.telegram import (
    BACKOFF_INTERVAL,
    EMPTY_ROUND_INTERVAL,
    POLL_LONG_TIMEOUT,
    POLL_SOCKET_TIMEOUT,
    TelegramAdapter,
)
from opencode_bridge.hooks import Inbound, MsgHandle, Outbound
from opencode_bridge.identity import (
    LEGACY_PREFIXES,
    is_valid,
    normalize,
    parse_id,
)
from opencode_bridge.state import StateStore
from opencode_bridge.transport import NOTHING

CHAT = 55
CHAT_CID = "telegram:55"
#: 切换**前**的前缀。断言一律拿它和**字面量**比，绝不拿
#: ``identity.LEGACY_PREFIXES`` 比 —— 后者被改了就变成恒真（tasks.md 记的教训）。
LEGACY_CHAT_CID = "chat:55"


def wait_until(predicate, timeout: float = 5.0, interval: float = 0.01) -> bool:
    """轮询等条件成立（比裸 sleep 稳；失败信息由调用方的断言给出）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


#: 记账用例里把节流档位缩放到这个值。**只**缩这一个旋钮，且理由是
#: 「可打断性与档位大小无关，而离散判据要能区分『等满』与『被唤醒』，
#: 档位就必须远大于本机 16 ms 的时钟分辨率」（见
#: :meth:`TestTelegramMigrationInvariants.test_stop_interrupts_the_empty_round_pacing`）。
PACING_LEDGER_SECONDS = 30.0


class RecordingWaitEvent(threading.Event):
    """一个**记账版**的 ``_stop_event``：记下每次 ``wait()`` 的请求与结局。

    ⛔ **为什么记「被请求的秒数 + 是否被置位唤醒」，而不是「实测量」**：
    ``Event.wait(x)`` 的 ``x`` 是生产侧那个等待的**入参**（纯函数输出），
    而**实测量**在机器有负载时会被调度放大（本机实测一次 ``wait(0.2)``
    在全量并发下回来是 1.25s，差 6 倍）⇒ 任何「观察到的间隔要落在某个区间里」
    的判据在负载下必然时红时绿。**逐项等值 / 离散布尔断言对机器负载免疫**
    （AGENTS.md §7.1：时序判据只能用同步点，⛔ 不要拿计时当时序断言）。

    ⚠️ **必须先记账、再转交真正的等待**（与
    ``tests/test_telegram_credential_gate.py::RecordingStopEvent`` 同一条纪律）：
    在 ``super().wait()`` **返回之后**才记账的话，**正在 park 的那一次看不见**
    ⇒ 而「park 正在发生」正是调用方要等的同步点 ⇒ 那样写会让同步点永远等不到。

    ⚠️ 它是 ``threading.Event`` 的**子类**而不是代理：``set()`` / ``clear()`` /
    ``is_set()`` 全部照原样可用 ⇒ **``stop()`` 仍然立刻能打断节流等待**；
    换成只实现 ``wait`` 的代理就会把它悄悄弄坏。
    """

    def __init__(self) -> None:
        super().__init__()
        #: ``(被请求的秒数, 返回时是否是被 set() 唤醒的)``，按请求先后排列。
        #: 第二列在等待**进行中**时是 ``None``（已记账、结局未写）。
        self.requested_waits: list[tuple[float, bool | None]] = []

    def wait(self, timeout: float | None = None) -> bool:
        """先记账，再**原样**转交真正的等待（返回值语义一个字没变）。"""
        self.requested_waits.append((timeout, None))       # 先记「请求了」
        released = super().wait(timeout)
        self.requested_waits[-1] = (timeout, released)     # 再补「是否被唤醒」
        return released


class RecordingHooks:
    """Minimal ``Hooks`` implementation that records every call."""

    def __init__(self, events=None):
        self.inbounds: list[Inbound] = []
        self.callbacks: list[tuple] = []
        self.events: list[tuple] = events if events is not None else []

    def on_inbound(self, inbound: Inbound) -> None:
        self.inbounds.append(inbound)
        self.events.append(("inbound", inbound))

    def on_callback(self, conversation_id: str, data: str, query_id: str) -> None:
        self.callbacks.append((conversation_id, data, query_id))
        self.events.append(("callback", conversation_id, data, query_id))


def make_telegram(config: dict | None = None, hooks: RecordingHooks | None = None):
    cfg = {"bot_token": "123:FAKE"}
    if config:
        cfg.update(config)
    adapter = TelegramAdapter(cfg, hooks or RecordingHooks())
    adapter.min_interval = 0  # no artificial sleeps in tests
    return adapter, adapter.hooks


def message_update(update_id=5, chat_id=CHAT, message_id=9, text="hello", **msg_extra):
    message = {
        "message_id": message_id,
        "chat": {"id": chat_id, "type": "private"},
        "from": {"id": 7, "is_bot": False, "first_name": "u"},
        "text": text,
    }
    message.update(msg_extra)
    return {"update_id": update_id, "message": message}


def callback_update(update_id=6, query_id="QID1", data="act:ok", chat_id=CHAT):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": query_id,
            "data": data,
            "from": {"id": 7},
            "message": {"message_id": 9, "chat": {"id": chat_id, "type": "private"}},
        },
    }


def script_transport(adapter, *, get_updates, get_me=None, flush=None, events=None):
    """把 ``_post`` 换成脚本化实现，返回 ``(calls, offsets, methods)`` 三个记录表。

    ``get_updates(offset)`` 返回一份 ``{"ok": ..., "result": ...}``。

    ⚠️ ``_flush_pending``（``start()`` 里的 ``offset=-1``）与常规轮询走的是**同一个**
    ``getUpdates`` 方法，所以这里把 flush 单独拆出去（``flush`` 回调）且**不**记进
    ``offsets`` —— 否则混进来的 ``-1`` 会让"轮询 offset"的断言失真。

    ``events`` 传入 hooks 的共享事件表时，每次 ``_post`` 还会往里追一条
    ``("post", method, payload)``（用来断言按钮三步的顺序）。
    """
    calls: list[tuple] = []
    offsets: list[object] = []
    methods: list[str] = []

    def fake_post(method, payload=None, *, timeout=None):
        payload = dict(payload or {})
        if method == "getUpdates" and payload.get("offset") == -1:
            return (flush() if flush else {"ok": True, "result": []})
        calls.append((method, payload, timeout))
        methods.append(method)
        if events is not None:
            events.append(("post", method, dict(payload)))
        if method == "getMe":
            return get_me() if get_me else {"ok": True, "result": {"id": 1}}
        if method == "getUpdates":
            offsets.append(payload.get("offset"))
            return get_updates(payload.get("offset"))
        return {"ok": True, "result": {}}

    adapter._post = fake_post
    return calls, offsets, methods


class TestTelegramMigrationInvariants(unittest.TestCase):
    """A1 迁移的「行为不变」清单：逐条钉死。"""

    # ------------------------------------------------------------------
    # 前缀：已从 ``chat:`` 切到 ``telegram:``
    # ------------------------------------------------------------------
    def test_conversation_id_uses_the_unified_platform_prefix(self):
        """显式防呆：有人把 ``_conversation_id`` 悄悄改回 ``chat:`` 时这里会红。

        ⚠️ "不许出现 ``chat:``"这条比的是**字面量**，不是 ``LEGACY_PREFIXES`` 常量 ——
        改了常量断言就恒真（tasks.md 记的教训）。反过来，"归一后等于新 id"那条**必须**
        引用常量：那是 :data:`identity.LEGACY_PREFIXES` 的契约本身。
        """
        self.assertEqual(LEGACY_PREFIXES["chat"], "telegram",
                         "chat 是 telegram 的旧别名")
        self.assertEqual(TelegramAdapter._conversation_id(CHAT), CHAT_CID)
        for chat in (55, "55", -1001234567890):
            with self.subTest(chat=chat):
                self.assertEqual(TelegramAdapter._conversation_id(chat),
                                 f"telegram:{chat}")
                self.assertFalse(
                    TelegramAdapter._conversation_id(chat).startswith("chat:"),
                    "旧前缀不许复活：它已从 identity 的登记表里退出，"
                    "新写的键会与迁移后的键对不上",
                )
        # 已是合法新格式：parse_id 收它，且往返无损
        self.assertTrue(is_valid(CHAT_CID))
        self.assertEqual(parse_id(CHAT_CID).platform, "telegram")
        self.assertEqual(normalize(CHAT_CID), CHAT_CID, "新格式必须幂等")
        # 旧 id 仍能归一（迁移期在途的旧 conversation_id）
        self.assertEqual(normalize(LEGACY_CHAT_CID, platform_hint="telegram"),
                         CHAT_CID)
        self.assertEqual(TelegramAdapter._chat_id(CHAT_CID), 55)

    def test_chat_id_still_accepts_the_legacy_prefix_and_bare_chat_id(self):
        """反向解析**必须**继续认旧前缀，否则盘上未投递的消息会被永久丢弃。

        写前收件箱把 ``conversation_id`` 持久化在 SQLite 里：切换前写入、切换后才
        重放的那几行带着 ``chat:`` 前缀，认不出来就再也发不出去了。
        """
        for raw, expected in (
            ("telegram:55", 55),
            ("chat:55", 55),              # 切换前落盘的旧 conversation_id
            ("55", 55),                   # 裸 chat_id
            ("chat:-1001234567890", -1001234567890),
        ):
            with self.subTest(conversation_id=raw):
                self.assertEqual(TelegramAdapter._chat_id(raw), expected)
        for bad in ("telegram:", "chat:", "", "telegram:abc", None):
            with self.subTest(conversation_id=bad):
                self.assertIsNone(TelegramAdapter._chat_id(bad))

    def test_inbound_and_callback_ids_carry_the_unified_prefix(self):
        """入站链路上的 id 也必须是 ``telegram:``（不只是 ``_conversation_id`` 本身）。"""
        adapter, hooks = make_telegram()
        script_transport(
            adapter,
            get_updates=lambda off: (
                {"ok": True, "result": [message_update(), callback_update()]}
                if off == 0 else {"ok": True, "result": []}),
        )
        adapter._flush_pending()               # 空批 ⇒ offset 不变（仍 0）
        self.assertEqual(adapter._offset, 0)
        self.assertTrue(adapter._poll_once())
        self.assertEqual([i.conversation_id for i in hooks.inbounds],
                         [CHAT_CID, CHAT_CID])
        self.assertEqual(hooks.callbacks, [(CHAT_CID, "act:ok", "QID1")])

    def test_legacy_chat_key_survives_the_prefix_cutover_through_state_migration(self):
        """端到端：切换前落盘的 ``chat:55`` 键，升级后仍能取回同一个会话。

        ``conversation_id`` 是 :class:`StateStore` 的**不透明键**，所以切前缀必须与
        :class:`StateStore` 的键迁移（``migrate_keys=True``）**同一个变更**上线 ——
        否则已落盘的键全部作废，且不报错、只表现为"agent 突然记错上下文"。

        这里真写一份**旧格式** ``state.json``（模拟升级前的用户磁盘），再用开启迁移
        的 store 重开，断言新 id 取得到、且落盘键已改写成新格式。
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"sessions": {LEGACY_CHAT_CID: "sess-telegram"},
                           "meta": {}}, fh)

            store = StateStore(path, migrate_keys=True)

            report = store.last_migration
            self.assertEqual(report.migrated, 1)
            self.assertEqual(report.collisions, 0)
            # ⚠️ 用**字面量**断言旧键已消失，而不是拿 LEGACY_PREFIXES 常量比。
            self.assertEqual(store.all_sessions(), {CHAT_CID: "sess-telegram"})
            self.assertNotIn("chat:", json.dumps(store.all_sessions()))
            with open(path, "r", encoding="utf-8") as fh:
                on_disk = json.load(fh)
            self.assertEqual(on_disk["sessions"], {CHAT_CID: "sess-telegram"})

            # 模拟进程重启：新 id 查得到，旧 id 也仍查得到（别名回退是活代码，
            # slack/discord/mattermost 的 channel: 键还在盘上，它不能被删）
            reopened = StateStore(path, migrate_keys=True)
            self.assertEqual(reopened.get_session(TelegramAdapter._conversation_id(CHAT)),
                             "sess-telegram")
            self.assertEqual(reopened.get_session(LEGACY_CHAT_CID), "sess-telegram")
            self.assertEqual(reopened.get_session(LEGACY_CHAT_CID),
                             reopened.get_session(CHAT_CID))

    # ------------------------------------------------------------------
    # 退避接线：2s 恒定（不是指数）+ reset_after=0
    # ------------------------------------------------------------------
    def test_backoff_wiring_matches_the_legacy_constant(self):
        adapter, _ = make_telegram()
        transport = adapter._make_transport()
        self.assertEqual(transport.min_backoff, BACKOFF_INTERVAL)
        self.assertEqual(transport.max_backoff, BACKOFF_INTERVAL,
                         "max == min ⇒ 退避恒定；迁移前本来就没有指数退避")
        self.assertEqual(transport._idle_delay(), EMPTY_ROUND_INTERVAL,
                         "空轮（ok 但 result 为空）的防御性节流 = 0.05s")
        self.assertEqual(transport.reset_after, 0.0,
                         "必须 0：fetch 成功一次就重置退避（连上即重置）")
        self.assertEqual(transport.label, "telegram")

    def test_backoff_is_constant_not_exponential(self):
        """连续失败也必须恒定 2s —— 迁移前 ``wait(2.0)`` 是一个字面量。"""
        adapter, _ = make_telegram()
        transport = adapter._make_transport()
        waits = [transport._next_backoff(survived=False) for _ in range(4)]
        self.assertEqual(waits, [BACKOFF_INTERVAL] * 4,
                         "连续失败也必须恒定 2s（迁移前只有一个常数 backoff）")
        # reset_after=0 ⇒ 传输层永远按"存活"判定 ⇒ 每次都取下限
        adapter2, _ = make_telegram()
        adapter2.backoff_interval = 5.0
        transport2 = adapter2._make_transport()
        self.assertEqual(
            [transport2._next_backoff(survived=True) for _ in range(3)],
            [5.0, 5.0, 5.0],
        )

    def test_failed_get_updates_is_retried_after_the_backoff_interval(self):
        """端到端：API 失败后确实等了一个 backoff_interval 才重试。"""
        adapter, _ = make_telegram()
        adapter.backoff_interval = 0.1
        at: list[float] = []
        calls, _offsets, _methods = script_transport(
            adapter,
            get_me=lambda: {"ok": True, "result": {"id": 1}},
            get_updates=lambda off: (at.append(time.monotonic()) or
                                     {"ok": False, "error_code": 500,
                                      "description": "boom"}),
        )
        adapter.start()
        try:
            self.assertTrue(wait_until(lambda: len(at) >= 3))
            gaps = [b - a for a, b in zip(at, at[1:])]
            for gap in gaps:
                self.assertGreaterEqual(gap, 0.09, f"失败后没退避：{gaps}")
        finally:
            adapter.stop()

    def test_successful_rounds_are_not_delayed_by_the_backoff(self):
        """成功的一轮**不等** backoff（否则入站会被 2s 拖死）。"""
        adapter, _ = make_telegram()
        adapter.backoff_interval = 5.0                 # 故意设很大：一旦生效就会超时
        at: list[float] = []

        def get_updates(off):
            at.append(time.monotonic())
            return {"ok": True, "result": []}

        script_transport(adapter,
                         get_me=lambda: {"ok": True, "result": {"id": 1}},
                         get_updates=get_updates)
        adapter.start()
        try:
            self.assertTrue(wait_until(lambda: len(at) >= 5, timeout=3.0),
                            "成功轮次之间不应有 5s 的等待")
            gaps = [b - a for a, b in zip(at, at[1:])]
            self.assertLess(max(gaps), 1.0, f"成功轮次被退避拖住了：{gaps}")
        finally:
            adapter.stop()

    def test_empty_round_pacing_matches_the_legacy_wait(self):
        """空轮的下界：0.05s 的防御性节流真的还在（迁移前是 ``wait(0.05)``）。"""
        adapter, _ = make_telegram()
        at: list[float] = []

        def get_updates(off):
            at.append(time.monotonic())
            return {"ok": True, "result": []}

        script_transport(adapter,
                         get_me=lambda: {"ok": True, "result": {"id": 1}},
                         get_updates=get_updates)
        adapter.start()
        try:
            self.assertTrue(wait_until(lambda: len(at) >= 4, timeout=3.0))
            gaps = [b - a for a, b in zip(at, at[1:])]
            for gap in gaps:
                self.assertGreaterEqual(gap, EMPTY_ROUND_INTERVAL * 0.9,
                                        f"空轮没做防御性节流：{gaps}")
        finally:
            adapter.stop()

    # ------------------------------------------------------------------
    # fetch 的两条退出路径：NOTHING（空轮）vs 抛异常（失败轮）
    # ------------------------------------------------------------------
    def test_fetch_returns_nothing_for_an_empty_round(self):
        adapter, _ = make_telegram()
        script_transport(adapter, get_updates=lambda off: {"ok": True, "result": []})
        self.assertIs(adapter._fetch_update(), NOTHING)

    def test_fetch_raises_on_api_failure_so_the_transport_backs_off(self):
        adapter, _ = make_telegram()
        script_transport(
            adapter,
            get_updates=lambda off: {"ok": False, "error_code": 500,
                                     "description": "boom"},
        )
        with self.assertLogs("opencode_bridge.adapters.telegram", level="WARNING"):
            with self.assertRaises(Exception):
                adapter._fetch_update()

    def test_fetch_returns_the_updates_of_one_batch_one_at_a_time(self):
        """一轮最多 100 条，fetch 一次只交一条 —— 批量挂在 ``_pending`` 上。

        迁移前是"一批全处理完再发下一轮"，这里保持同样的顺序（否则会在同一批
        处理到一半时又发一次 getUpdates）。
        """
        adapter, _ = make_telegram()
        batches = [
            {"ok": True, "result": [message_update(update_id=1),
                                    message_update(update_id=2),
                                    message_update(update_id=3)]},
            {"ok": True, "result": []},
        ]

        def get_updates(off):
            return batches.pop(0) if batches else {"ok": True, "result": []}

        _calls, offsets, _methods = script_transport(adapter, get_updates=get_updates)
        got = [adapter._fetch_update()["update_id"] for _ in range(3)]
        self.assertEqual(got, [1, 2, 3])
        self.assertEqual(adapter._pending, [])
        self.assertEqual(offsets, [0], "一批处理完之前不许发第二次请求")
        # 第 4 次才发新请求（此时 offset 已被推进过 —— 这里直接手推）
        adapter._offset = 4
        self.assertIs(adapter._fetch_update(), NOTHING)
        self.assertEqual(offsets, [0, 4])

    # ------------------------------------------------------------------
    # stop()：快（打断等待，有耗时上界）+ 不泄漏线程 + 幂等
    # ------------------------------------------------------------------
    def test_stop_interrupts_the_backoff_wait(self):
        """失败轮正在 2s 退避等待里时 stop() 必须立刻返回。"""
        adapter, _ = make_telegram()
        adapter.backoff_interval = 5.0
        attempts: list[float] = []
        script_transport(
            adapter,
            get_me=lambda: {"ok": True, "result": {"id": 1}},
            get_updates=lambda off: (attempts.append(time.monotonic()) or
                                     {"ok": False, "error_code": 500,
                                      "description": "boom"}),
        )
        adapter.start()
        self.addCleanup(adapter.stop)
        self.assertTrue(wait_until(lambda: len(attempts) >= 1))
        self.assertTrue(adapter.transport.stats()["errors"] >= 1,
                        "失败轮必须被记成传输层故障（不是'空轮'）")
        began = time.monotonic()
        adapter.stop()
        self.assertLess(time.monotonic() - began, 2.0,
                        "stop() 没打断退避等待（会白等满 backoff）")
        self.assertFalse(adapter.running)
        self.assertIsNone(adapter.transport)
        adapter.stop()                                 # 幂等
        self.assertFalse(adapter.running)

    def test_stop_interrupts_the_empty_round_pacing(self):
        """空轮的节流也必须可被打断（迁移前是 ``Event.wait``，不是 sleep）。

        ⛔ **判据是「park 真的发生过、且是被 ``stop()`` 释放的」这个离散事实**，
        ⛔ **不是**「``stop()`` 耗时 < 某个数」：

        ⚠️ **原形态是恒真的**（实测：把 park 换成 ``time.sleep`` 它照样绿）。
        原因是算术：被 park 的那一档是 :data:`EMPTY_ROUND_INTERVAL` = **0.05s**，
        而原断言带宽是 **2.0s** —— **40 倍**。park 就算**整个等满**也只花 0.05s
        ⇒ 「等满」与「被立刻唤醒」在墙钟上根本分不开。而本机
        ``time.monotonic()`` 的分辨率是 16 ms（连采 20 万次只有 2 个不同值），
        与 0.05s 同量级 ⇒ 任何 0.05s 附近的时刻判据都是掷硬币。
        ⇒ 修法是**换同步点 + 换离散记账**（AGENTS.md §8：治根因，不治症状），
        ⛔ **不是**把带宽调大 —— 调到 30s 只能抓住「join 超时」，
        而抓不住「park 退化成 sleep」这个本用例点名要防的缺陷。

        ⇒ 因此把节流档位缩放到 :data:`PACING_LEDGER_SECONDS`：可打断性与档位大小
        **无关**，而记账判据要能区分「等满」与「被唤醒」，档位就必须远大于分辨率。
        """
        adapter, _ = make_telegram()
        ledger = RecordingWaitEvent()
        inner_make_transport = adapter._make_transport

        def make_transport_with_recorded_pacing():
            transport = inner_make_transport()
            transport._idle_sleep = PACING_LEDGER_SECONDS
            transport._stop_event = ledger      # 换掉的是「谁在看等待」，不是「谁能打断它」
            return transport

        adapter._make_transport = make_transport_with_recorded_pacing
        script_transport(
            adapter,
            get_me=lambda: {"ok": True, "result": {"id": 1}},
            get_updates=lambda off: {"ok": True, "result": []},   # 永远空轮 ⇒ 一直走节流
        )
        adapter.start()
        self.addCleanup(adapter.stop)

        # 下面这条**只是死锁守卫**（park 从不发生时要 fail 得响亮），⛔ 不是时刻判据：
        # 它回答「节流真的 park 过吗」，「是不是被 stop() 打断的」由后面那条回答。
        self.assertTrue(
            wait_until(lambda: len(ledger.requested_waits) >= 1, timeout=5.0),
            f"空轮节流必须真的 park 过。实际记账：{ledger.requested_waits!r}",
        )
        adapter.stop()
        self.assertEqual(
            [seconds for seconds, _released in ledger.requested_waits],
            [PACING_LEDGER_SECONDS],
            f"节流必须逐项等于 :data:`PACING_LEDGER_SECONDS`（实际记账："
            f"{ledger.requested_waits!r}）",
        )
        self.assertEqual(
            [released for _seconds, released in ledger.requested_waits],
            [True],
            "⛔ park 必须是被 stop() 唤醒的（``wait`` 返回 True），不是自己等满的档位 —— "
            "退化成 ``time.sleep`` 时这里会是 False 或干脆没有这次记账。"
            f"实际记账：{ledger.requested_waits!r}",
        )
        self.assertFalse(adapter.running)

    def test_stop_during_inflight_get_updates_does_not_leak_the_thread(self):
        """已知限制（迁移前就有）：stop() 打不断在途的 getUpdates。

        至少保证不会永久泄漏线程：这一轮返回后消费线程自然退出。
        """
        adapter, _ = make_telegram()
        entered = threading.Event()
        script_transport(
            adapter,
            get_me=lambda: {"ok": True, "result": {"id": 1}},
            get_updates=lambda off: (entered.set() or _slow_ok()),
        )
        adapter.start()
        self.addCleanup(adapter.stop)
        transport = adapter.transport
        self.assertTrue(entered.wait(3), "没进到 getUpdates")
        adapter.stop()
        self.assertTrue(wait_until(lambda: not transport.running, timeout=5),
                        "在途请求返回后线程必须退出")

    def test_stop_without_start_is_a_noop(self):
        adapter, _ = make_telegram()
        adapter.stop()
        self.assertFalse(adapter.running)
        self.assertIsNone(adapter.transport)

    def test_transport_exception_does_not_silently_kill_the_thread(self):
        """传输层故障（``_post`` 抛异常）时消费线程不许静默死掉。"""
        adapter, _ = make_telegram()
        adapter.backoff_interval = 0.01
        attempts: list[float] = []

        def boom(method, payload=None, *, timeout=None):
            if method == "getMe":
                return {"ok": True, "result": {"id": 1}}
            attempts.append(time.monotonic())
            raise RuntimeError("socket died")

        adapter._post = boom
        adapter.start()
        self.addCleanup(adapter.stop)
        self.assertTrue(wait_until(lambda: len(attempts) >= 3, timeout=5),
                        "异常后必须继续重试（线程不许静默退出）")
        self.assertTrue(adapter.running)
        stats = adapter.transport.stats()
        self.assertGreaterEqual(stats["errors"], 1)
        self.assertEqual(stats["events"], 0, "传输层故障不该被当成收到过事件")

    def test_start_creates_exactly_one_poller_thread(self):
        """一次 ``start()`` 起**一个**消费线程（线程名来自传输层，不是适配器）。"""
        adapter, _ = make_telegram()
        script_transport(adapter, get_updates=lambda off: {"ok": True, "result": []})
        adapter.start()
        try:
            transport = adapter.transport
            self.assertIsNotNone(transport)
            self.assertEqual(transport.thread_name, "transport:telegram")
            self.assertTrue(adapter.running)
            self.assertEqual(
                [t.name for t in threading.enumerate()
                 if t.name == "transport:telegram"],
                ["transport:telegram"], "不许起第二个消费线程",
            )
        finally:
            adapter.stop()
        self.assertFalse(adapter.running)

    # ------------------------------------------------------------------
    # offset 语义：先推进再分发 / 失败不动
    # ------------------------------------------------------------------
    def test_offset_advances_before_dispatch_even_if_dispatch_explodes(self):
        """铁律：分发崩了 offset 也必须**已经**推进（否则这批 update 被无限重放）。

        ``_poll_once`` 吞掉单条异常（迁移前也是循环体里 try/except），
        所以这里断言的是**offset 状态与后续请求载荷**，不是返回值。
        """
        adapter, _ = make_telegram()
        batch = {"ok": True, "result": [message_update(update_id=100),
                                        message_update(update_id=101)]}
        _calls, offsets, _methods = script_transport(adapter,
                                                     get_updates=lambda off: batch)
        seen: list[int] = []

        def boom(update):
            seen.append(update["update_id"])
            raise RuntimeError("dispatch exploded")

        adapter._dispatch_update = boom
        self.assertTrue(adapter._poll_once())
        self.assertEqual(adapter._offset, 102, "offset 必须先推进（分发崩了也一样）")
        self.assertEqual(seen, [100, 101],
                         "同批里单条崩了仍要继续处理下一条（迁移前同语义）")
        # 恢复后下一轮必须带 offset=102 ⇒ 服务端不会把那批再发一遍
        adapter._dispatch_update = lambda update: None
        self.assertTrue(adapter._poll_once())
        self.assertEqual(offsets[-1], 102, "必须带上已推进的 offset（否则重放）")

    def test_offset_advance_survives_a_dispatch_blip_in_the_running_loop(self):
        """同一件事在真实消费线程上的版本：分发抛异常不许把线程带走。"""
        adapter, _ = make_telegram()
        _calls, offsets, _methods = script_transport(
            adapter,
            get_me=lambda: {"ok": True, "result": {"id": 1}},
            get_updates=lambda off: {"ok": True,
                                     "result": [message_update(update_id=100)]},
        )

        def boom(update):
            raise RuntimeError("dispatch exploded")

        adapter._dispatch_update = boom
        adapter.start()
        try:
            self.assertTrue(wait_until(lambda: len(offsets) >= 2, timeout=3.0),
                            "分发异常后仍应继续轮询")
            self.assertEqual(adapter._offset, 101, "游标已推进 ⇒ 这批不会被重放")
            self.assertTrue(adapter.running, "分发异常不得让线程静默退出")
            self.assertEqual(offsets[0], 0, "第一轮用初始游标")
            for off in offsets[1:]:
                self.assertEqual(off, 101, "后续请求必须带新 offset（不重放）")
        finally:
            adapter.stop()

    def test_failed_round_leaves_the_offset_untouched(self):
        """API 失败时 offset 一字不动（也不许换游标）。"""
        adapter, _ = make_telegram()
        adapter._offset = 7
        script_transport(
            adapter,
            get_updates=lambda off: {"ok": False, "error_code": 500,
                                     "description": "boom"},
        )
        with self.assertLogs("opencode_bridge.adapters.telegram", level="WARNING"):
            self.assertFalse(adapter._poll_once())
        self.assertEqual(adapter._offset, "7" and 7, "失败时 offset 一字不动")

    def test_offset_never_moves_on_failure_through_the_transport(self):
        """整条链路（传输层线程）上验证：连续失败时 offset 不动、线程仍重试。"""
        adapter, _ = make_telegram()
        adapter.backoff_interval = 0.01
        adapter._offset = 7
        _calls, offsets, _methods = script_transport(
            adapter,
            get_me=lambda: {"ok": True, "result": {"id": 1}},
            get_updates=lambda off: {"ok": False, "error_code": 500,
                                     "description": "boom"},
        )
        adapter.start()
        self.addCleanup(adapter.stop)
        self.assertTrue(wait_until(lambda: len(offsets) >= 3, timeout=5),
                        "失败后必须继续轮询")
        self.assertEqual(adapter._offset, 7, "失败时 offset 一字不动")
        for off in offsets:
            self.assertEqual(off, 7, "失败时也不能换游标")
        self.assertTrue(adapter.running)

    def test_transport_level_failure_leaves_the_offset_untouched(self):
        """``_post`` 直接抛异常（传输层故障）时 offset 也不能动。"""
        adapter, _ = make_telegram()
        adapter._offset = 7

        def boom(*a, **k):
            raise RuntimeError("socket died")

        adapter._post = boom
        self.assertFalse(adapter._poll_once())
        self.assertEqual(adapter._offset, 7)

    def test_offset_only_moves_forward(self):
        """乱序到达的 update 不许把游标拉回去（否则会被反复重放）。"""
        adapter, _ = make_telegram()
        adapter._offset = 50
        batch = {"ok": True, "result": [message_update(update_id=10),
                                        message_update(update_id=60),
                                        message_update(update_id=20)]}
        script_transport(adapter, get_updates=lambda off: batch)
        self.assertTrue(adapter._poll_once())
        self.assertEqual(adapter._offset, 61)

    def test_offset_advances_even_for_updates_we_filter_out(self):
        """被过滤掉的 update **也要**推进 offset —— 否则下一轮还会收到它。"""
        adapter, hooks = make_telegram({"allowed_chat_ids": [999]})
        batch = {"ok": True, "result": [message_update(update_id=77, chat_id=55),
                                        {"update_id": 78, "message": {"chat": {"id": 55},
                                                                      "sticker": {}}}]}
        _calls, _offsets, _methods = script_transport(adapter,
                                                      get_updates=lambda off: batch)
        self.assertTrue(adapter._poll_once())
        self.assertEqual(hooks.inbounds, [])
        self.assertEqual(adapter._offset, 79,
                         "被过滤的 update 也必须确认掉（否则无限重放）")

    # ------------------------------------------------------------------
    # 长轮询两层超时
    # ------------------------------------------------------------------
    def test_long_poll_timeouts_are_unchanged(self):
        """``timeout=25`` 与 socket 超时 ``poll_timeout + 15`` 都不能被迁移改动。

        两者的大小关系是**功能约束**：socket 超时必须大于服务端挂起时长，
        否则每一轮都会在拿到数据前被本地掐断。
        """
        self.assertEqual(POLL_LONG_TIMEOUT, 25)
        self.assertEqual(POLL_SOCKET_TIMEOUT, 40.0)
        self.assertGreater(
            POLL_SOCKET_TIMEOUT, POLL_LONG_TIMEOUT,
            "socket 超时必须 > 服务端挂起时长（40s > 25s）",
        )
        adapter, _ = make_telegram()
        calls, _offsets, _methods = script_transport(
            adapter, get_updates=lambda off: {"ok": True, "result": []}
        )
        adapter._poll_once()
        method, payload, timeout = calls[-1]
        self.assertEqual(method, "getUpdates")
        self.assertEqual(payload["timeout"], POLL_LONG_TIMEOUT)
        self.assertEqual(timeout, POLL_LONG_TIMEOUT + 15.0)
        self.assertGreater(
            timeout, POLL_LONG_TIMEOUT,
            "socket 超时必须 > 服务端挂起时长（否则本地先掐断）",
        )

    def test_socket_timeout_tracks_a_custom_poll_timeout(self):
        """配了 ``poll_timeout`` 时大小关系仍要成立（不能改成常数 40s）。"""
        adapter, _ = make_telegram({"poll_timeout": 8})
        calls, _offsets, _methods = script_transport(
            adapter, get_updates=lambda off: {"ok": True, "result": []}
        )
        self.assertEqual(adapter.poll_long_timeout, 8)
        adapter._poll_once()
        _method, payload, timeout = calls[-1]
        self.assertEqual(payload["timeout"], 8)
        self.assertEqual(timeout, 23.0)
        self.assertGreater(timeout, 8)

    def test_poll_request_shape_is_unchanged(self):
        """``limit`` / ``allowed_updates`` / ``offset`` 载荷逐字不变。"""
        adapter, _ = make_telegram()
        calls, offsets, _methods = script_transport(
            adapter, get_updates=lambda off: {"ok": True, "result": []}
        )
        adapter._poll_once()
        method, payload, _timeout = calls[-1]
        self.assertEqual(method, "getUpdates")
        self.assertEqual(payload["limit"], 100)
        # allowed_updates 是**服务端侧订阅范围**，与客户端过滤是两处机制
        self.assertEqual(payload["allowed_updates"], ["message", "callback_query"])
        self.assertEqual(payload["offset"], adapter._offset)
        self.assertEqual(offsets, [0])

    # ------------------------------------------------------------------
    # 入站过滤：逐条
    # ------------------------------------------------------------------
    def test_all_inbound_filters_still_apply(self):
        """一条 update 对应一个过滤条件，全塞进同一批，只有一条该被投递。

        过滤条件（与迁移前逐字一致）：① update 不是 dict ② ``message`` 不是 dict
        （``edited_message`` / ``channel_post`` / ``edited_channel_post`` 都落这里）
        ③ ``text`` 不是字符串或为空（贴纸 / 图片 / 语音 / 只有 caption 的媒体）
        ④ ``chat`` 不是 dict 或没有 id ⑤ chat 不在白名单。
        """
        hooks = RecordingHooks()
        adapter, _ = make_telegram({"allowed_chat_ids": [CHAT]}, hooks)
        batch = {
            "ok": True,
            "result": [
                "not-a-dict",                                   # ①
                {"update_id": 11, "edited_message": {"message_id": 1,
                                                     "chat": {"id": CHAT},
                                                     "text": "改过的"}},        # ②
                {"update_id": 12, "channel_post": {"message_id": 2,
                                                    "chat": {"id": CHAT},
                                                    "text": "频道"}},            # ②
                {"update_id": 13, "message": {"message_id": 3,
                                              "chat": {"id": CHAT},
                                              "sticker": {}}},                # ③
                {"update_id": 14, "message": {"message_id": 4, "chat": {"id": CHAT},
                                              "photo": [{}], "caption": "pic"}},  # ③
                {"update_id": 15, "message": {"message_id": 5,
                                              "chat": {"id": CHAT}, "text": ""}},  # ③
                {"update_id": 16, "message": {"message_id": 6, "text": "无 chat"}},  # ④
                {"update_id": 17, "message": {"message_id": 7, "chat": {},
                                              "text": "空 chat"}},            # ④
                message_update(update_id=18, chat_id=888),      # ⑤ 白名单外
                message_update(update_id=19, chat_id=CHAT, text="真消息"),  # 唯一该投递的
            ],
        }
        script_transport(adapter, get_updates=lambda off: batch)
        self.assertTrue(adapter._poll_once())
        self.assertEqual([i.text for i in hooks.inbounds], ["真消息"])
        self.assertEqual(hooks.inbounds[0].conversation_id, CHAT_CID)
        # 整批（含被过滤的）都要确认掉
        self.assertEqual(adapter._offset, 20)

    def test_echo_of_our_own_send_is_not_reprocessed(self):
        """自己发出去的消息不会再被当成入站（``allowed_updates`` 只订 message/callback，
        而 chat 侧的回声由 Telegram 服务端按 bot 身份区分，这里守住"一条 inbound
        只投一次"这个契约）。"""
        adapter, hooks = make_telegram()
        batch = {"ok": True, "result": [message_update(update_id=1),
                                        message_update(update_id=2, text="second")]}
        script_transport(adapter, get_updates=lambda off: batch)
        self.assertTrue(adapter._poll_once())
        self.assertEqual([i.text for i in hooks.inbounds], ["hello", "second"])

    # ------------------------------------------------------------------
    # 按钮能力：callback query → answer
    # ------------------------------------------------------------------
    def test_inline_buttons_capability_survived(self):
        adapter, _ = make_telegram()
        self.assertTrue(adapter.supports_inline_buttons)
        self.assertTrue(adapter.capabilities()["supports_inline_buttons"])

    def test_callback_order_is_inbound_then_callback_then_answer(self):
        """按钮三步顺序是行为契约：投递 inbound → ``on_callback`` → 应答。"""
        events: list[tuple] = []
        adapter, hooks = make_telegram(hooks=RecordingHooks(events=events))
        calls, _offsets, methods = script_transport(
            adapter,
            get_updates=lambda off: {"ok": True, "result": []},
            events=events,
        )
        adapter._dispatch_update(callback_update())
        self.assertEqual([e[0] for e in events],
                         ["inbound", "callback", "post"])
        self.assertEqual(methods[-1], "answerCallbackQuery")
        self.assertEqual(calls[-1][1]["callback_query_id"], "QID1")
        self.assertEqual(hooks.callbacks, [(CHAT_CID, "act:ok", "QID1")])

    def test_callback_is_still_answered_when_the_hook_explodes(self):
        """用户回调抛异常也**必须**应答（``answer`` 在 ``finally`` 里）。

        不应答的话那个转圈圈会一直挂在用户界面上（Bot API 认为 query 未 ack）。
        """
        class Exploding(RecordingHooks):
            def on_inbound(self, inbound):
                raise RuntimeError("core exploded")

            def on_callback(self, conversation_id, data, query_id):
                raise RuntimeError("core exploded")

        adapter, _ = make_telegram(hooks=Exploding())
        calls, _offsets, methods = script_transport(
            adapter, get_updates=lambda off: {"ok": True, "result": []}
        )
        with self.assertLogs("opencode_bridge.adapters.telegram", level="ERROR"):
            adapter._dispatch_update(callback_update(query_id="Q9"))
        self.assertIn("answerCallbackQuery", methods,
                      "回调崩了也必须应答（answer 在 finally 里）")
        self.assertEqual(calls[-1][1]["callback_query_id"], "Q9")

    def test_callback_arrives_through_the_transport_and_answers(self):
        """整条链路：callback query 从传输层进来，照样走完三步。"""
        adapter, hooks = make_telegram()
        _calls, _offsets, methods = script_transport(
            adapter,
            get_me=lambda: {"ok": True, "result": {"id": 1}},
            get_updates=lambda off: ({"ok": True, "result": [callback_update()]}
                                     if off == 0 else {"ok": True, "result": []}),
        )
        adapter.start()
        try:
            self.assertTrue(wait_until(lambda: methods.count("answerCallbackQuery") >= 1))
        finally:
            adapter.stop()
        self.assertEqual(hooks.callbacks, [(CHAT_CID, "act:ok", "QID1")])
        self.assertEqual(len(hooks.inbounds), 1)
        self.assertEqual(hooks.inbounds[0].kind, "callback")

    def test_callback_from_a_non_whitelisted_chat_is_dropped_untouched(self):
        """白名单外的 callback：不投递、也**不应答**（不该替别人 ack）。"""
        adapter, hooks = make_telegram({"allowed_chat_ids": [CHAT]})
        _calls, _offsets, methods = script_transport(
            adapter, get_updates=lambda off: {"ok": True, "result": []}
        )
        adapter._dispatch_update(callback_update(query_id="QX", chat_id=888))
        self.assertEqual(hooks.callbacks, [])
        self.assertNotIn("answerCallbackQuery", methods)


def _slow_ok():
    """模拟一个在途的长轮询（睡 0.3s 再回一个空批）。"""
    time.sleep(0.3)
    return {"ok": True, "result": []}


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
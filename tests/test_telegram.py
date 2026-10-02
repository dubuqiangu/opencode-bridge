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
* 以及一条**防呆**用例把 ``chat:`` 前缀钉死（本轮刻意不切 ``telegram:``，
  切换的前置条件是 ``state.py`` 的键迁移）。

⚠️ 本项目铁律：断言"等了多久"一律**优先断言内部状态**（退避状态机 / 请求载荷 /
计数器），墙钟只允许做**下界**断言（等待只会更长，绝不会更短）或 ``stop()``
耗时这种**上界**断言（且要给得宽松）。
"""

from __future__ import annotations

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
    InvalidConversationId,
    is_valid,
    normalize,
    parse_id,
)
from opencode_bridge.state import StateStore
from opencode_bridge.transport import NOTHING

CHAT = 55
CHAT_CID = "chat:55"


def wait_until(predicate, timeout: float = 5.0, interval: float = 0.01) -> bool:
    """轮询等条件成立（比裸 sleep 稳；失败信息由调用方的断言给出）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


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
    # 前缀：本轮**刻意不切**
    # ------------------------------------------------------------------
    def test_conversation_id_still_uses_legacy_chat_prefix(self):
        """显式防呆：有人在本轮偷偷把 ``chat:`` 换成 ``telegram:`` 时这里会红。

        ``chat`` 在 :data:`identity.LEGACY_PREFIXES` 里**指向 telegram 自己**，
        所以光看字符串判不出来（对比 ``room`` → ``matrix`` 是别的平台）——
        这正是"必须显式钉住"的理由。
        """
        self.assertEqual(LEGACY_PREFIXES["chat"], "telegram",
                         "chat 是 telegram 的旧别名")
        self.assertEqual(TelegramAdapter._conversation_id(CHAT), CHAT_CID)
        for chat in (55, "55", -1001234567890):
            with self.subTest(chat=chat):
                self.assertEqual(TelegramAdapter._conversation_id(chat), f"chat:{chat}")
        # 仍是旧格式：parse_id 拒绝它，只有 normalize 才知道怎么归一
        self.assertFalse(is_valid(CHAT_CID))
        with self.assertRaises(InvalidConversationId):
            parse_id(CHAT_CID)
        self.assertEqual(normalize(CHAT_CID, platform_hint="telegram"),
                         "telegram:55")
        self.assertEqual(TelegramAdapter._chat_id(CHAT_CID), 55)

    def test_inbound_and_callback_ids_keep_the_legacy_prefix(self):
        """入站链路上的 id 也必须是 ``chat:``（不只是 ``_conversation_id`` 本身）。"""
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

    def test_switching_prefix_now_would_orphan_stored_sessions(self):
        """把"为什么现在不能切前缀"写成**可执行**的断言（只读地借用 StateStore）。

        ``conversation_id`` 是 :class:`StateStore` 的**不透明键**：切前缀等于把
        历史键全部作废，而且不报错、只表现为"agent 突然记错上下文"。
        真要切时必须先做键迁移 —— 那时这个用例会提醒你同步更新它。
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            cid = TelegramAdapter._conversation_id(CHAT)
            store = StateStore(path)
            store.set_session(cid, "sess-1")
            reopened = StateStore(path)          # 模拟进程重启
            self.assertEqual(reopened.get_session(cid), "sess-1",
                             "旧键在重启后必须仍能读回（这就是'已落盘'）")
            future = "telegram:55"
            self.assertNotEqual(future, cid)
            self.assertIsNone(
                reopened.get_session(future),
                "切前缀后同一个 chat 是另一个不透明键 → 会话映射直接丢失",
            )

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
        """空轮的 0.05s 节流也必须可被打断（迁移前是 ``Event.wait``，不是 sleep）。"""
        adapter, _ = make_telegram()
        seen: list[float] = []
        script_transport(
            adapter,
            get_me=lambda: {"ok": True, "result": {"id": 1}},
            get_updates=lambda off: (seen.append(time.monotonic()) or
                                     {"ok": True, "result": []}),
        )
        adapter.start()
        self.addCleanup(adapter.stop)
        self.assertTrue(wait_until(lambda: len(seen) >= 2))
        began = time.monotonic()
        adapter.stop()
        # 上界给得宽松（Windows 定时器分辨率差），只为抓住"退化成 sleep / join 超时"
        self.assertLess(time.monotonic() - began, 2.0)
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
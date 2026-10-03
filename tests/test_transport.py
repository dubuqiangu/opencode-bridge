"""A1 传输层测试（标准库 ``unittest``；**零第三方依赖、零真外网**）。

覆盖
----
* :class:`PollingTransport` —— 假 ``fetch`` 脚本驱动：哨兵语义、``idle_sleep``、
  异常退避。
* :class:`WebSocketTransport` —— **注入假 WS**（绝不联网）：``recv()`` 返回
  ``None`` 重连、收集中抛异常重连、``on_message`` 被调用、
  :class:`ReconnectNow` 立即重连（不等退避）。
* :class:`TcpLineTransport` —— ``socket.socketpair()`` + 一个**本机回环监听器**
  （``127.0.0.1:0``，仍不算外网）：半行/多行重组、坏字节 replace、
  ``on_connect``、``send_line``。
* :class:`Transport` 基类 6 条不变量**逐条**锁住。
* :class:`TestPeriodicHook` —— ``on_tick`` / ``tick_interval``：循环驱动（IRC 等价
  语义）与定时驱动（WS 这类 ``recv()`` 会长时间阻塞的长连接）两条触发时机、异常
  隔离、"只在有活动会话时调"、"未配置时零开销"。

线程用例最容易 flaky，所以：一律用 :func:`wait_until` 等条件而不是裸 sleep，
每个用例 ``addCleanup(stop)``，超时给得比真实需求宽裕得多。
"""

from __future__ import annotations

import itertools
import logging
import socket
import threading
import time
import unittest
from typing import Any

logging.getLogger("opencode_bridge").addHandler(logging.NullHandler())

from opencode_bridge.transport import (  # noqa: E402  (先装 NullHandler 再 import)
    NOTHING,
    PollingTransport,
    ReconnectNow,
    TcpLineTransport,
    Transport,
    WebSocketTransport,
)
from opencode_bridge.transport.base import _NothingSentinel  # noqa: E402

_NAME_SEQ = itertools.count()


def uniq_name(prefix: str = "t") -> str:
    """每个用例独立的 transport 名 → 线程名唯一，统计线程数不受邻居影响。"""
    return f"{prefix}-{next(_NAME_SEQ)}"


def wait_until(predicate, timeout: float = 3.0, interval: float = 0.004) -> bool:
    """轮询等条件成立（比裸 sleep 稳，且失败信息由断言给出）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


#: 脚本里的"等一下"占位（让消费线程有机会先跑一轮）。
PAUSE = object()


class _Hang:
    """脚本占位：``_next`` 阻塞 ``secs`` 秒后抛错（模拟"连上了但很快又断"）。"""

    def __init__(self, secs: float, error: BaseException | None = None) -> None:
        self.secs = secs
        self.error = error or ConnectionError("hang ended")


class _ScriptedBase(Transport):
    """可控的基类替身：``_open`` / ``_next`` 各带一份脚本。"""

    def __init__(self, *, open_script=(), next_script=(), idle_delay=0.01, **kw) -> None:
        self._open_script = list(open_script)
        self._next_script = list(next_script)
        self._idle_gap = idle_delay
        self.opens = 0
        self.opened_at: list[float] = []
        #: 每次 ``_open`` **发生时刻**的退避状态快照。
        #: 比 ``opened_at`` 的 wall-clock 差分**更可靠**：等待只会比标称值更长
        #: （机器忙时线程调度只会加延迟），所以墙钟差分需要容差、容差不够就 flaky；
        #: 而"连上过一次之后退避确实回到下限"这件事本身就是确定性的状态转移，
        #: 直接快照下来既不含容差、又能抓住"退避不再重置"这个真bug。
        self.backoff_at_open: list[float] = []
        self.on_open_calls: list[Any] = []
        self.on_close_calls: list[Any] = []
        super().__init__(**kw)

    def _open(self):
        self.opens += 1
        self.opened_at.append(time.monotonic())
        self.backoff_at_open.append(self._backoff)
        if self._open_script:
            item = self._open_script.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        raise ConnectionError("open script exhausted")

    def _next(self, conn):
        if self._next_script:
            item = self._next_script.pop(0)
            if isinstance(item, _Hang):
                time.sleep(item.secs)
                raise item.error
            if isinstance(item, BaseException):
                raise item
            return item
        return NOTHING

    def _on_open(self, conn) -> None:
        self.on_open_calls.append(conn)

    def _on_close(self, conn) -> None:
        self.on_close_calls.append(conn)

    def _idle_delay(self) -> float:
        return self._idle_gap


class _BadConn:
    """``close()`` 会抛异常的连接对象（关连接路径不许崩）。"""

    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        raise OSError("close exploded")


class _GateConn:
    """``close()`` 会唤醒 ``_next`` 里阻塞的读 —— 用来验证"先关连接再 join"。

    ``order`` 是共享的**事件顺序日志**（跨对象）。时刻（``closed_at`` /
    ``thread_ended_at``）在 Windows 上只有 ~15.6ms 分辨率，唤醒与关闭常落在
    同一个 tick 里，所以顺序用日志证明，时刻只作为辅助信息。
    """

    def __init__(self, order: list[str] | None = None) -> None:
        self._gate = threading.Event()
        self.order: list[str] = order if order is not None else []
        self.closed_at: float | None = None

    def wait(self, timeout: float) -> bool:
        return self._gate.wait(timeout)

    def close(self) -> None:
        if self.closed_at is None:      # 幂等：只记第一次关闭时刻
            self.closed_at = time.monotonic()
            self.order.append("close")
        self._gate.set()


class _BlockingTransport(Transport):
    """``_next`` 一直阻塞在 ``conn.wait()`` 上，直到连接被关掉。"""

    def __init__(self, conn, *, block: float = 30.0, **kw) -> None:
        self.conn = conn
        self.block = block
        self.thread_ended_at: float | None = None
        self.opens = 0
        super().__init__(**kw)

    def _open(self):
        self.opens += 1
        return self.conn

    def _next(self, conn):
        conn.wait(self.block)           # 没有 close() 就会阻塞满 block 秒
        self.thread_ended_at = time.monotonic()
        conn.order.append("thread_end")
        return NOTHING


class TransportTestCase(unittest.TestCase):
    """公共脚手架：起线程 + 注册清理 + 收集事件。"""

    def start_transport(self, transport, on_event=None):
        events: list[Any] = []
        transport.start(on_event if on_event is not None else events.append)
        self.addCleanup(transport.stop)
        return events


# ----------------------------------------------------------------------
# 哨兵
# ----------------------------------------------------------------------
class TestNothingSentinel(unittest.TestCase):
    def test_nothing_is_a_singleton(self):
        self.assertIs(type(NOTHING)(), NOTHING)
        self.assertIs(_NothingSentinel(), NOTHING)

    def test_nothing_repr_and_falsy(self):
        self.assertEqual(repr(NOTHING), "<NOTHING>")
        self.assertFalse(NOTHING)


# ----------------------------------------------------------------------
# PollingTransport
# ----------------------------------------------------------------------
class _Fetcher:
    """脚本驱动的假 ``fetch``：元素是返回值或要抛的异常；喂完返回 NOTHING。"""

    def __init__(self, script=()) -> None:
        self.script = list(script)
        self.calls = 0
        self.at: list[float] = []

    def __call__(self):
        self.calls += 1
        self.at.append(time.monotonic())
        if not self.script:
            return NOTHING
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class TestPollingTransport(TransportTestCase):
    def test_events_are_passed_through_untouched(self):
        fetch = _Fetcher(["a", {"raw": 1}, 3])
        poll_transport = PollingTransport(fetch, idle_sleep=0.01, name=uniq_name("poll"))
        events = self.start_transport(poll_transport)
        self.assertTrue(wait_until(lambda: len(events) >= 3))
        # 原样交付：不解释、不过滤、不包装
        self.assertEqual(events, ["a", {"raw": 1}, 3])

    def test_nothing_is_never_dispatched(self):
        fetch = _Fetcher([NOTHING, NOTHING])
        poll_transport = PollingTransport(fetch, idle_sleep=0.01, name=uniq_name("poll"))
        events = self.start_transport(poll_transport)
        self.assertTrue(wait_until(lambda: fetch.calls >= 2))
        self.assertEqual(events, [])
        self.assertGreaterEqual(poll_transport.stats()["idle"], 2)
        self.assertEqual(poll_transport.stats()["events"], 0)

    def test_idle_sleep_is_applied_between_empty_rounds(self):
        fetch = _Fetcher([NOTHING, NOTHING, "x"])
        poll_transport = PollingTransport(
            fetch, idle_sleep=0.05, min_backoff=0.01, name=uniq_name("poll")
        )
        events = self.start_transport(poll_transport)
        self.assertTrue(wait_until(lambda: len(events) == 1))
        gaps = [b - a for a, b in zip(fetch.at, fetch.at[1:])]
        self.assertGreaterEqual(len(gaps), 2)
        for gap in gaps[:2]:
            self.assertGreaterEqual(gap, 0.035, f"空转没睡够: {gaps}")
        self.assertLess(gaps[0], 1.0, "idle_sleep 过大")

    def test_long_poll_does_not_sleep(self):
        """长轮询传 idle_sleep=0：空转要立刻再问（fetch 自己会挂起）。"""
        fetch = _Fetcher([])
        poll_transport = PollingTransport(fetch, idle_sleep=0.0, name=uniq_name("poll"))
        self.start_transport(poll_transport)
        self.assertTrue(wait_until(lambda: fetch.calls >= 40, timeout=2.0))

    def test_negative_idle_sleep_is_clamped(self):
        poll_transport = PollingTransport(_Fetcher([]), idle_sleep=-1.0, name=uniq_name("poll"))
        self.assertEqual(poll_transport._idle_delay(), 0.0)

    def test_fetch_exception_backs_off_and_retries(self):
        fetch = _Fetcher([OSError("net down"), OSError("net down"), "late"])
        poll_transport = PollingTransport(
            fetch, idle_sleep=0.0, min_backoff=0.01, max_backoff=0.05,
            name=uniq_name("poll"),
        )
        events = self.start_transport(poll_transport)
        self.assertTrue(wait_until(lambda: events == ["late"]))
        self.assertGreaterEqual(fetch.calls, 3)
        self.assertTrue(poll_transport.running)          # 失败不该杀死线程

    def test_on_open_called_once_per_session(self):
        fetch = _Fetcher([OSError("x"), OSError("x"), "v"])
        seen: list[int] = []
        poll_transport = PollingTransport(
            fetch, idle_sleep=0.0, min_backoff=0.01, on_open=lambda: seen.append(1),
            name=uniq_name("poll"),
        )
        self.start_transport(poll_transport)
        self.assertTrue(wait_until(lambda: fetch.calls >= 3))
        self.assertEqual(len(seen), 3)

    def test_on_open_exception_prevents_fetch_and_backs_off(self):
        fetch = _Fetcher([])
        attempts = {"n": 0}

        def boom() -> None:
            attempts["n"] += 1
            raise RuntimeError("handshake failed")

        poll_transport = PollingTransport(
            fetch, idle_sleep=0.0, min_backoff=0.01, on_open=boom,
            name=uniq_name("poll"),
        )
        self.start_transport(poll_transport)
        self.assertTrue(wait_until(lambda: attempts["n"] >= 2))
        self.assertEqual(fetch.calls, 0)      # 握手没过就不该发请求
        self.assertTrue(poll_transport.running)

    def test_connection_exposes_fetch_callable(self):
        fetch = _Fetcher([])
        poll_transport = PollingTransport(fetch, idle_sleep=0.05, name=uniq_name("poll"))
        self.start_transport(poll_transport)
        self.assertTrue(wait_until(lambda: poll_transport.connection is not None))
        self.assertIs(poll_transport.connection, fetch)
        poll_transport.stop()
        self.assertIsNone(poll_transport.connection)       # stop 后应已清空

    def test_non_callable_fetch_rejected(self):
        with self.assertRaises(TypeError):
            PollingTransport("not callable")  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# WebSocketTransport
# ----------------------------------------------------------------------
class FakeWs:
    """假 WebSocket 客户端（接口对齐 ``opencode_bridge.ws.WebSocketClient``）。"""

    def __init__(self, script=(), *, close_code=None, close_reason="",
                 block_timeout=5.0) -> None:
        self.script = list(script)
        self._close_code = close_code
        self._close_reason = close_reason
        self._gate = threading.Event()
        self._block_timeout = block_timeout
        self.closed = True
        self.close_calls: list[int] = []
        self.sent: list[str] = []

    @property
    def close_code(self):
        return self._close_code

    @property
    def close_reason(self):
        return self._close_reason

    def open(self) -> "FakeWs":
        self.closed = False
        return self

    def recv(self):
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        if self._gate.wait(self._block_timeout):
            raise ConnectionError("fake ws: 已被 close() 关闭")
        raise ConnectionError("fake ws: 脚本喂完还没被 close()（测试问题）")

    def send(self, text: str) -> None:
        self.sent.append(text)

    def close(self, code: int = 1000, reason: str = "") -> None:
        self.close_calls.append(code)
        self.closed = True
        self._gate.set()


class TestWebSocketTransport(TransportTestCase):
    def test_frames_reach_on_event(self):
        ws = FakeWs(["one", "two"])
        ws_transport = WebSocketTransport(lambda: ws, name=uniq_name("ws"))
        events = self.start_transport(ws_transport)
        self.assertTrue(wait_until(lambda: events == ["one", "two"]))
        self.assertEqual(ws_transport.stats()["connects"], 1)

    def test_recv_none_reconnects(self):
        sockets = [FakeWs([None]), FakeWs(["after"])]
        ws_transport = WebSocketTransport(
            lambda: sockets.pop(0).open(), min_backoff=0.01, name=uniq_name("ws")
        )
        events = self.start_transport(ws_transport)
        self.assertTrue(wait_until(lambda: events == ["after"]))
        self.assertGreaterEqual(ws_transport.stats()["connects"], 2)

    def test_recv_exception_reconnects(self):
        sockets = [FakeWs([OSError("tcp reset")]), FakeWs(["ok"])]
        ws_transport = WebSocketTransport(
            lambda: sockets.pop(0).open(), min_backoff=0.01, name=uniq_name("ws")
        )
        events = self.start_transport(ws_transport)
        self.assertTrue(wait_until(lambda: events == ["ok"]))

    def test_on_message_called_with_conn_and_frame(self):
        seen: list[tuple[Any, Any]] = []
        ws = FakeWs(["a", "b"])
        ws_transport = WebSocketTransport(
            lambda: ws, on_message=lambda conn, frame: seen.append((conn, frame)),
            name=uniq_name("ws"),
        )
        events = self.start_transport(ws_transport)
        self.assertTrue(wait_until(lambda: len(seen) >= 2))
        self.assertEqual(seen[0], (ws, "a"))
        self.assertEqual(seen[1], (ws, "b"))
        self.assertEqual(events, ["a", "b"])   # on_message 不吞事件

    def test_on_message_exception_does_not_drop_connection(self):
        def boom(conn, frame):
            raise ValueError("bad ack")

        ws = FakeWs(["a", "b"])
        ws_transport = WebSocketTransport(
            lambda: ws, on_message=boom, name=uniq_name("ws")
        )
        # ⚠️ start() 必须在 assertLogs **里面**：假 WS 几微秒就能把两帧喂完，
        # 放在外面的话日志会在捕获开始前就打完了（这正是本用例第一次跑失败的原因）。
        with self.assertLogs("opencode_bridge.transport.websocket", level="WARNING"):
            events = self.start_transport(ws_transport)
            self.assertTrue(wait_until(lambda: events == ["a", "b"]))
        self.assertEqual(ws_transport.stats()["connects"], 1)   # 连接没被换掉

    def test_reconnect_now_skips_backoff(self):
        """on_message 抛 ReconnectNow → 立刻重连（min_backoff=5s 也无所谓）。"""
        made: list[FakeWs] = []

        def connect():
            ws = FakeWs(["ping"]).open()
            made.append(ws)
            return ws

        def disconnect(conn, frame):
            raise ReconnectNow(f"server asked: {frame}")

        ws_transport = WebSocketTransport(
            connect, on_message=disconnect, min_backoff=5.0, max_backoff=60.0,
            name=uniq_name("ws"),
        )
        self.start_transport(ws_transport)
        started = time.monotonic()
        self.assertTrue(wait_until(lambda: len(made) >= 5, timeout=3.0))
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertGreaterEqual(ws_transport.stats()["sessions"], 4)

    def test_close_code_from_peer_is_reported(self):
        ws = FakeWs([None], close_code=4002, close_reason="ratelimited")
        ws_transport = WebSocketTransport(
            lambda: ws, min_backoff=0.01, name=uniq_name("ws")
        )
        with self.assertLogs("opencode_bridge.transport.base", level="WARNING") as cap:
            self.start_transport(ws_transport)      # 同上：必须放在捕获窗口内
            self.assertTrue(wait_until(lambda: ws_transport.stats()["errors"] >= 1))
        blob = "\n".join(cap.output)
        self.assertIn("close_code=4002", blob)
        self.assertIn("ratelimited", blob)

    def test_local_close_code_is_used_on_shutdown(self):
        ws = FakeWs([])
        ws_transport = WebSocketTransport(
            lambda: ws, close_code=4001, name=uniq_name("ws")
        )
        self.start_transport(ws_transport)
        self.assertTrue(wait_until(lambda: ws_transport.connection is not None))
        ws_transport.stop()
        # stop() 与会话结束的 finally 各关一次 —— 幂等，只要求都用同一状态码
        self.assertTrue(ws.close_calls)
        self.assertEqual(set(ws.close_calls), {4001})

    def test_close_without_code_argument_supported(self):
        class NoArgWs(FakeWs):
            def close(self):            # type: ignore[override]
                self.closed = True
                self._gate.set()

        ws = NoArgWs([])
        ws_transport = WebSocketTransport(lambda: ws, name=uniq_name("ws"))
        self.start_transport(ws_transport)
        self.assertTrue(wait_until(lambda: ws_transport.connection is not None))
        ws_transport.stop()                        # TypeError 回退路径不许崩
        self.assertFalse(ws_transport.running)

    def test_connect_failure_is_retried(self):
        attempts = {"n": 0}

        def connect():
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise ConnectionError("handshake refused")
            return FakeWs(["ready"]).open()

        ws_transport = WebSocketTransport(
            connect, min_backoff=0.01, max_backoff=0.02, name=uniq_name("ws")
        )
        events = self.start_transport(ws_transport)
        self.assertTrue(wait_until(lambda: events == ["ready"]))


# ----------------------------------------------------------------------
# TcpLineTransport
# ----------------------------------------------------------------------
class _Peer(threading.Thread):
    """``socketpair()`` 的对端：按脚本往连接里写字节。

    默认脚本跑完**不**关连接（用来观察"空闲不是掉线"）；``close_after=True``
    时跑完立刻关（制造 FIN，验证重连）。
    """

    def __init__(self, sock, script, *, gap: float = 0.0,
                 close_after: bool = False) -> None:
        super().__init__(daemon=True, name="transport-test-peer")
        self._sock = sock
        self._script = list(script)
        self._gap = gap
        self._close_after = close_after
        self.received = bytearray()
        self.done = threading.Event()

    def run(self) -> None:
        try:
            for item in self._script:
                if item is PAUSE:
                    time.sleep(0.05)
                    continue
                self._sock.sendall(item)
                if self._gap:
                    time.sleep(self._gap)
            if self._close_after:
                self._sock.close()
        except OSError:
            pass
        finally:
            self.done.set()

    def read(self, size: int = 4096) -> bytes:
        self._sock.settimeout(3.0)
        return self._sock.recv(size)

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


class _PairedTcp(TcpLineTransport):
    """把 ``_open`` 换成"从预置 socket 列表里取"（socketpair 注入点）。"""

    def __init__(self, socks, **kw) -> None:
        self._socks = list(socks)
        super().__init__("127.0.0.1", 9, **kw)

    def _open(self):
        if not self._socks:
            raise OSError("no more paired sockets")
        sock = self._socks.pop(0)
        sock.settimeout(self._io_timeout)
        return sock


def pair_sockets(count: int = 1):
    """建 ``count`` 组 socketpair，返回 ``(transport 端, 对端列表)``。"""
    ours, theirs = [], []
    for _ in range(count):
        a, b = socket.socketpair()
        ours.append(a)
        theirs.append(b)
    return ours, theirs


class _LineServer(threading.Thread):
    """本机回环监听器（``127.0.0.1:0``）：接受一次连接、发脚本、记录收到的字节。"""

    def __init__(self, script) -> None:
        super().__init__(daemon=True, name="transport-test-lineserver")
        self._script = list(script)
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(4)
        self.port: int = self._srv.getsockname()[1]
        self.received = bytearray()

    def run(self) -> None:
        for payload in self._script:
            try:
                conn, _addr = self._srv.accept()
            except OSError:
                return
            try:
                conn.sendall(payload)
                conn.settimeout(1.0)
                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    self.received += chunk
            except OSError:
                pass
            finally:
                conn.close()

    def close(self) -> None:
        try:
            self._srv.close()
        except OSError:
            pass


class TestTcpLineTransport(TransportTestCase):
    def _transport(self, ours, **kw):
        kw.setdefault("io_timeout", 0.2)
        kw.setdefault("min_backoff", 0.01)
        kw.setdefault("name", uniq_name("tcp"))
        return _PairedTcp(ours, **kw)

    def test_line_split_across_recvs_is_reassembled(self):
        ours, theirs = pair_sockets()
        peer = _Peer(theirs[0], [b"PIN", PAUSE, b"G :srv\r\n"])
        peer.start()
        self.addCleanup(peer.close)
        tcp_transport = self._transport(ours)
        events = self.start_transport(tcp_transport)
        self.assertTrue(wait_until(lambda: events == ["PING :srv"]))

    def test_multiple_lines_in_one_recv(self):
        ours, theirs = pair_sockets()
        peer = _Peer(theirs[0], [b"001 hi\r\n002 there\r\n003 x\r\n"])
        peer.start()
        self.addCleanup(peer.close)
        tcp_transport = self._transport(ours)
        events = self.start_transport(tcp_transport)
        self.assertTrue(wait_until(lambda: len(events) >= 3))
        self.assertEqual(events[:3], ["001 hi", "002 there", "003 x"])

    def test_lone_lf_without_cr_is_accepted(self):
        ours, theirs = pair_sockets()
        peer = _Peer(theirs[0], [b"PING\n"])
        peer.start()
        self.addCleanup(peer.close)
        events = self.start_transport(self._transport(ours))
        self.assertTrue(wait_until(lambda: events == ["PING"]))

    def test_bad_utf8_is_replaced_not_fatal(self):
        ours, theirs = pair_sockets()
        peer = _Peer(theirs[0], [b"PRIVMSG \xff\xfe caf\xc3\r\n"])
        peer.start()
        self.addCleanup(peer.close)
        tcp_transport = self._transport(ours)
        events = self.start_transport(tcp_transport)
        self.assertTrue(wait_until(lambda: events == ["PRIVMSG \ufffd\ufffd caf\ufffd"]))
        self.assertTrue(tcp_transport.running)      # 坏字节不该把连接干掉

    def test_on_connect_is_called_with_socket(self):
        ours, theirs = pair_sockets()
        peer = _Peer(theirs[0], [b"ok\r\n"])
        peer.start()
        self.addCleanup(peer.close)
        seen: list[Any] = []
        tcp_transport = self._transport(ours, on_connect=seen.append)
        events = self.start_transport(tcp_transport)
        self.assertTrue(wait_until(lambda: events == ["ok"]))
        self.assertEqual(seen, [ours[0]])       # 回调拿到的就是 socket

    def test_on_connect_exception_triggers_reconnect(self):
        ours, theirs = pair_sockets(2)
        for sock in theirs:
            peer = _Peer(sock, [b"hi\r\n"])
            peer.start()
            self.addCleanup(peer.close)

        calls = {"n": 0}

        def flaky(sock):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("registration rejected")

        tcp_transport = self._transport(ours, on_connect=flaky)
        events = self.start_transport(tcp_transport)
        self.assertTrue(wait_until(lambda: events == ["hi"]))
        self.assertEqual(calls["n"], 2)

    def test_send_line_writes_crlf(self):
        ours, theirs = pair_sockets()
        peer = _Peer(theirs[0], [])
        peer.start()
        self.addCleanup(peer.close)
        tcp_transport = self._transport(ours)
        self.start_transport(tcp_transport)
        self.assertTrue(wait_until(lambda: tcp_transport.connection is not None))
        self.assertTrue(tcp_transport.send_line("NICK opencodebot"))
        self.assertEqual(peer.read(), b"NICK opencodebot\r\n")

    def test_send_line_neutralizes_embedded_crlf(self):
        ours, theirs = pair_sockets()
        peer = _Peer(theirs[0], [])
        peer.start()
        self.addCleanup(peer.close)
        tcp_transport = self._transport(ours)
        self.start_transport(tcp_transport)
        self.assertTrue(wait_until(lambda: tcp_transport.connection is not None))
        self.assertTrue(tcp_transport.send_line("PRIVMSG #a :hi\r\nQUIT sneaky"))
        self.assertEqual(peer.read(), b"PRIVMSG #a :hi QUIT sneaky\r\n")

    def test_send_line_without_connection_returns_false(self):
        tcp_transport = TcpLineTransport("127.0.0.1", 9, name=uniq_name("tcp"))
        self.assertFalse(tcp_transport.send_line("NICK x"))   # 没起线程就没连接

    def test_peer_close_triggers_reconnect(self):
        ours, theirs = pair_sockets(2)
        for sock in ours:
            self.addCleanup(sock.close)
        # 第一组：发一行然后关（FIN）→ 应触发重连到第二组
        first = _Peer(theirs[0], [b"bye\r\n"], close_after=True)
        first.start()
        self.addCleanup(first.close)
        second = _Peer(theirs[1], [b"again\r\n"])
        second.start()
        self.addCleanup(second.close)
        tcp_transport = self._transport(ours)
        events = self.start_transport(tcp_transport)
        self.assertTrue(wait_until(lambda: events == ["bye", "again"]))
        self.assertEqual(tcp_transport.stats()["connects"], 2)
        self.assertGreaterEqual(tcp_transport.stats()["sessions"], 1)

    def test_idle_timeout_is_not_a_disconnect(self):
        ours, theirs = pair_sockets()
        peer = _Peer(theirs[0], [b"only\r\n"])
        peer.start()
        self.addCleanup(peer.close)
        tcp_transport = self._transport(ours)
        events = self.start_transport(tcp_transport)
        self.assertTrue(wait_until(lambda: events == ["only"]))
        # 让对端彻底安静 > io_timeout：只应增加 NOTHING 计数，不该重连
        idle_before = tcp_transport.stats()["idle"]
        self.assertTrue(wait_until(lambda: tcp_transport.stats()["idle"] > idle_before + 2))
        self.assertEqual(tcp_transport.stats()["connects"], 1)
        self.assertEqual(tcp_transport.stats()["errors"], 0)

    def test_overlong_line_drops_buffer_without_dying(self):
        ours, theirs = pair_sockets()
        peer = _Peer(theirs[0], [b"x" * 200])
        peer.start()
        self.addCleanup(peer.close)
        tcp_transport = self._transport(ours)
        tcp_transport.max_line_bytes = 32              # 类级旋钮，测试里收紧
        events = self.start_transport(tcp_transport)
        self.assertTrue(wait_until(lambda: tcp_transport.stats()["idle"] >= 3))
        self.assertEqual(events, [])       # 没换行 → 不产出事件
        self.assertTrue(tcp_transport.running)

    def test_real_loopback_socket_path(self):
        """走真实 ``_open``（create_connection）+ on_connect + send_line。"""
        server = _LineServer([b"001 welcome\r\n"])
        server.start()
        self.addCleanup(server.close)
        sent: list[Any] = []
        tcp_transport = TcpLineTransport(
            "127.0.0.1",
            server.port,
            tls=False,
            on_connect=sent.append,
            io_timeout=0.2,
            connect_timeout=5.0,
            min_backoff=0.05,
            name=uniq_name("tcp"),
        )
        events = self.start_transport(tcp_transport)
        self.assertTrue(wait_until(lambda: events == ["001 welcome"]))
        self.assertEqual(len(sent), 1)
        self.assertTrue(tcp_transport.send_line("NICK bot"))
        self.assertTrue(wait_until(lambda: b"NICK bot\r\n" in bytes(server.received)))
        tcp_transport.stop()
        self.assertEqual(tcp_transport.connection, None)


# ----------------------------------------------------------------------
# 基类不变量（逐条）
# ----------------------------------------------------------------------
class TestBaseInvariants(TransportTestCase):
    def test_invariant1_stop_closes_connection_before_joining(self):
        """先关连接，再 join：**用时刻/顺序证明**，不只看 stop() 返不返回。"""
        order: list[str] = []
        conn = _GateConn(order)
        fake_transport = _BlockingTransport(conn, block=30.0, name=uniq_name("block"))
        events = self.start_transport(fake_transport)
        self.assertTrue(wait_until(lambda: fake_transport.running))
        time.sleep(0.05)                    # 确保消费线程已阻塞在 conn.wait 上

        began = time.monotonic()
        fake_transport.stop(timeout=5.0)
        elapsed = time.monotonic() - began

        self.assertEqual(events, [])
        self.assertIsNotNone(conn.closed_at, "stop() 没关连接")
        self.assertIsNotNone(fake_transport.thread_ended_at, "消费线程没跑起来")
        # 关闭连接 → 才唤醒读 → 消费线程才结束。反过来（先 join）就会白等 30s。
        self.assertEqual(order, ["close", "thread_end"], "顺序错了：join 在关连接之前")
        self.assertLessEqual(conn.closed_at, fake_transport.thread_ended_at)
        self.assertLess(elapsed, 3.0, f"stop() 耗了 {elapsed:.2f}s（应被 close 唤醒）")
        self.assertFalse(fake_transport.running)
        self.addCleanup(fake_transport.stop)

    def test_invariant2_backoff_doubles_and_caps(self):
        fake_transport = _ScriptedBase(min_backoff=1.0, max_backoff=4.0, name=uniq_name("bo"))
        waits = [fake_transport._next_backoff(survived=False) for _ in range(6)]
        self.assertEqual(waits, [1.0, 2.0, 4.0, 4.0, 4.0, 4.0])

    def test_invariant2_backoff_resets_after_stable_connection(self):
        fake_transport = _ScriptedBase(min_backoff=1.0, max_backoff=8.0, name=uniq_name("bo"))
        self.assertEqual(fake_transport._next_backoff(survived=False), 1.0)
        self.assertEqual(fake_transport._next_backoff(survived=False), 2.0)
        # 这次连接稳定存活过 → 本次就只等下限，且状态归零
        self.assertEqual(fake_transport._next_backoff(survived=True), 1.0)
        self.assertEqual(fake_transport._next_backoff(survived=False), 1.0)
        self.assertEqual(fake_transport._next_backoff(survived=False), 2.0)

    def test_reset_after_defaults_to_zero(self):
        """默认必须是 0 —— "只要连上过就重置"才是迁移前 8 个适配器的既有语义。

        默认若为正数，迁移就构成行为变更：网络闪断时退避会一路涨到上限，
        而现状每次都从下限重来。这条断言是那个承诺的锁。
        """
        fake_transport = _ScriptedBase()
        self.assertEqual(fake_transport.reset_after, 0.0)

    def test_reset_after_positive_defers_reset_until_connection_survives(self):
        """``reset_after > 0`` 时，短命连接**不**重置退避（闸门在调用点，不在
        ``_next_backoff`` 内）—— 保守模式确实生效，而不是死参数。"""
        conn = _GateConn()
        fake_transport = _ScriptedBase(
            open_script=[OSError("boom"), OSError("boom"), conn, OSError("gone")],
            # 每次只活 0.005s，远小于 reset_after=10s
            next_script=[_Hang(0.005)],
            min_backoff=0.1,
            max_backoff=0.8,
            reset_after=10.0,
            idle_delay=0.005,
            name=uniq_name("noreset"),
        )
        self.start_transport(fake_transport)
        # 断言**内部退避状态**而不是 wall-clock 间隔：机器忙时线程调度会让间隔
        # 测量剧烈抖动（实测本用例单独跑通过、全量跑失败），那是测试写法的问题，
        # 不是实现的问题。
        self.assertTrue(wait_until(lambda: fake_transport._backoff >= 0.4, timeout=5.0))
        # 闸门没开 → 短命连接不会把退避打回下限
        self.assertGreaterEqual(fake_transport._backoff, 0.4, "短命连接不应触发退避重置")

    def test_reset_after_zero_resets_even_a_brief_connection(self):
        """``reset_after=0``（默认）时，**哪怕只活了几毫秒**的连接也必须重置退避。

        这条补的是一个**真实缺口**：此前只断言了「默认值是 0」这个属性，以及
        「``reset_after>0`` 时不重置」的反面 —— 却**没有任何用例证明默认语义在线程里
        真的生效**。而这正是 IRC 等适配器的既有行为（``irc.py``「连上过一次就重置」）；
        更麻烦的是那些适配器自己的用例是**白盒读 ``_backoff``**，一旦传输层改了判定
        方式，它们会集体失效而没人察觉。

        本用例是上条用例的**正面对照**（同一套脚本，只把 ``reset_after`` 从 10 改成 0）：

        ============================  ===========================================
        步骤                          ``reset_after=0`` 下的退避
        ============================  ===========================================
        open 失败                      等 0.1 → 退避 0.2
        open 失败                      等 0.2 → 退避 0.4
        open 成功、仅活 0.005s         **重置** → 等 0.1，退避回 0.1
        open 失败                      等 0.1 → 退避 0.2（不再往上爬）
        ============================  ===========================================

        判据取「退避不再越过 0.25」：一旦重置生效，该值此后恒定在 0.1/0.2 之间，
        所以这个谓词是**稳定**的，不受机器负载影响（与上条用例同理，
        不用 wall-clock 间隔断言 —— 那正是本项目踩过的 flaky 坑）。
        """
        conn = _GateConn()
        fake_transport = _ScriptedBase(
            open_script=[OSError("boom"), OSError("boom"), conn, OSError("gone")],
            # 每次只活 0.005s，远小于任何正数 reset_after —— 正是"短暂连接"的极端
            next_script=[_Hang(0.005)],
            min_backoff=0.1,
            max_backoff=0.8,
            reset_after=0.0,
            idle_delay=0.005,
            name=uniq_name("zeroreset"),
        )
        self.start_transport(fake_transport)
        self.assertTrue(wait_until(lambda: len(fake_transport.opened_at) >= 4, timeout=5.0))
        self.assertLessEqual(
            fake_transport._backoff, 0.25,
            "reset_after=0 时，短暂成功连接也必须把退避打回下限",
        )

    def test_invariant2_backoff_reset_threaded(self):
        """线程里也验证一次：两次失败把退避推到 0.4s，稳定连接后退回下限0.1s。

        ⚠️ **这个用例以前是 flaky 的**（实测高负载下 15 次里失败 1 次：墙钟差分
        测到 0.172 而标称 0.1，旧断言是 ``assertAlmostEqual(0.1, delta=0.06)``）。
        根因不是实现问题（等待只会**比标称更长**，机器忙时线程调度只会加延迟），
        而是**断言形状错了**：用带容差的墙钟差分，既会在机器慢时误报，又**抓不到**
        "等待比标称更短"这类真bug。

        改为断言 ``backoff_at_open`` 这个**确定性状态序列**，不含任何容差：

        ==================  ==========  ==========================================
        第几次 open          退避快照     含义
        ==================  ==========  ==========================================
        1                   0.1         初始下限
        2                   0.2         失败一次 →×2
        3                   0.4         失败两次 → ×2
        4                   **0.1**     **连上过一次（活了 0.05s ≥ reset_after）→ 重置**
        5                   0.2         重置后重新开始涨（不是永远钉在下限）
        ==================  ==========  ==========================================

        第4 项是本用例的核心断言；第 5 项防止"用永远不重置来让第 4 项成立"。
        若重置逻辑坏掉，序列会变成 ``0.1, 0.2, 0.4, 0.8, 0.8`` 而立刻失败。
        """
        conn = _GateConn()
        fake_transport = _ScriptedBase(
            open_script=[OSError("boom"), OSError("boom"), conn, OSError("gone")],
            next_script=[_Hang(0.05)],
            min_backoff=0.1,
            max_backoff=0.8,
            reset_after=0.03,
            idle_delay=0.01,
            name=uniq_name("bo"),
        )
        self.start_transport(fake_transport)
        self.assertTrue(wait_until(lambda: len(fake_transport.opened_at) >= 5, timeout=5.0))
        seen = [round(v, 6) for v in fake_transport.backoff_at_open[:5]]
        for got, want in zip(seen, [0.1, 0.2, 0.4, 0.1, 0.2]):
            self.assertAlmostEqual(got, want, places=6)
        # 附带：确实真的等了（墙钟只做**下界**断言 —— 等待只会更长，绝不会更短）
        gaps = [b - a for a, b in zip(fake_transport.opened_at, fake_transport.opened_at[1:5])]
        for index, gap in enumerate(gaps):
            self.assertGreaterEqual(
                gap, 0.09,
                f"第 {index + 1} 段间隔 {gap:.3f}s 低于下限，说明退避压根没等",
            )

    def test_invariant3_open_exception_never_escapes(self):
        boom = ConnectionError("always down")
        fake_transport = _ScriptedBase(
            open_script=[boom, boom, boom, boom], min_backoff=0.01,
            max_backoff=0.02, name=uniq_name("bo"),
        )
        self.start_transport(fake_transport)
        self.assertTrue(wait_until(lambda: fake_transport.opens >= 3))
        self.assertTrue(fake_transport.running, "线程被异常带走了")

    def test_invariant3_next_exception_reconnects_and_survives(self):
        conn1, conn2 = _GateConn(), _GateConn()
        fake_transport = _ScriptedBase(
            open_script=[conn1, conn2],
            next_script=[OSError("stream broke"), NOTHING],
            min_backoff=0.01, name=uniq_name("bo"),
        )
        self.start_transport(fake_transport)
        self.assertTrue(wait_until(lambda: fake_transport.opens >= 2))
        self.assertTrue(fake_transport.running)
        self.assertIn(conn1, fake_transport.on_close_calls)     # 旧连接被清理了

    def test_invariant3_on_open_exception_reconnects(self):
        class _Flaky(_ScriptedBase):
            def _on_open(self, conn) -> None:
                super()._on_open(conn)
                if len(self.on_open_calls) == 1:
                    raise RuntimeError("identify rejected")

        fake_transport = _Flaky(
            open_script=[_GateConn(), _GateConn(), _GateConn()],
            min_backoff=0.01, name=uniq_name("bo"),
        )
        self.start_transport(fake_transport)
        self.assertTrue(wait_until(lambda: fake_transport.opens >= 2))
        self.assertTrue(fake_transport.running)

    def test_invariant3_on_close_exception_never_escapes(self):
        class _Angry(_ScriptedBase):
            def _on_close(self, conn) -> None:
                super()._on_close(conn)
                raise RuntimeError("cleanup blew up")

        fake_transport = _Angry(
            open_script=[_GateConn(), _GateConn()],
            next_script=[OSError("drop")],
            min_backoff=0.01, name=uniq_name("bo"),
        )
        self.start_transport(fake_transport)
        self.assertTrue(wait_until(lambda: fake_transport.opens >= 2))
        self.assertTrue(fake_transport.running)

    def test_invariant3_close_conn_exception_never_escapes(self):
        bad = _BadConn()
        fake_transport = _ScriptedBase(
            open_script=[bad, _GateConn()], next_script=[OSError("drop")],
            min_backoff=0.01, name=uniq_name("bo"),
        )
        self.start_transport(fake_transport)
        self.assertTrue(wait_until(lambda: fake_transport.opens >= 2))
        self.assertGreaterEqual(bad.close_calls, 1)
        self.assertTrue(fake_transport.running)

    def test_invariant4_on_event_exception_does_not_kill_loop(self):
        seen: list[int] = []

        def bad_callback(item):
            seen.append(item)
            raise RuntimeError("user code is broken")

        fake_transport = _ScriptedBase(
            open_script=[_GateConn()], next_script=list(range(6)),
            min_backoff=0.01, name=uniq_name("bo"),
        )
        with self.assertLogs("opencode_bridge.transport.base", level="WARNING"):
            self.start_transport(fake_transport, on_event=bad_callback)   # 同上：在捕获窗口内起线程
            self.assertTrue(wait_until(lambda: len(seen) >= 6))
        self.assertTrue(fake_transport.running)

    def test_invariant5_start_is_idempotent(self):
        fake_transport = _ScriptedBase(
            open_script=[_GateConn()], next_script=list(range(50)),
            min_backoff=0.01, name=uniq_name("bo"),
        )
        events = self.start_transport(fake_transport)
        self.assertTrue(wait_until(lambda: fake_transport.running))
        fake_transport.start(events.append)
        fake_transport.start(events.append)
        time.sleep(0.05)
        same_name = [th for th in threading.enumerate() if th.name == fake_transport.thread_name]
        self.assertEqual(len(same_name), 1, "start() 起了第二个线程")

    def test_invariant5_stop_is_idempotent(self):
        fake_transport = _ScriptedBase(
            open_script=[_GateConn()], min_backoff=0.01, name=uniq_name("bo")
        )
        self.start_transport(fake_transport)
        self.assertTrue(wait_until(lambda: fake_transport.running))
        fake_transport.stop()
        began = time.monotonic()
        fake_transport.stop()
        fake_transport.stop()
        self.assertLess(time.monotonic() - began, 1.0)
        self.assertFalse(fake_transport.running)

    def test_invariant5_stop_before_start_is_noop(self):
        fake_transport = _ScriptedBase(name=uniq_name("bo"))
        fake_transport.stop()
        self.assertFalse(fake_transport.running)

    def test_invariant5_restart_after_stop(self):
        fake_transport = _ScriptedBase(
            open_script=[_GateConn(), _GateConn()],
            next_script=[1, NOTHING, 2, NOTHING],
            min_backoff=0.01, name=uniq_name("bo"),
        )
        events = self.start_transport(fake_transport)
        self.assertTrue(wait_until(lambda: events[:1] == [1]))
        fake_transport.stop()
        self.assertFalse(fake_transport.running)
        fake_transport.start(events.append)
        self.assertTrue(wait_until(lambda: events[:2] == [1, 2]))
        self.assertTrue(fake_transport.running)

    def test_invariant6_stop_interrupts_backoff_wait(self):
        fake_transport = _ScriptedBase(
            open_script=[OSError("down")] * 3,
            min_backoff=30.0, max_backoff=60.0, name=uniq_name("bo"),
        )
        self.start_transport(fake_transport)
        self.assertTrue(wait_until(lambda: fake_transport.opens >= 1))
        began = time.monotonic()
        fake_transport.stop(timeout=5.0)
        self.assertLess(time.monotonic() - began, 2.0, "退避等待没被 stop 打断")
        self.assertFalse(fake_transport.running)

    def test_stop_from_inside_callback_does_not_deadlock(self):
        """适配器常在事件处理里关自己 —— 不能 join 自己（否则死等）。"""
        fake_transport = _ScriptedBase(
            open_script=[_GateConn()], next_script=[1, 2, 3],
            min_backoff=0.01, name=uniq_name("bo"),
        )
        fired: list[int] = []

        def callback(item):
            fired.append(item)
            fake_transport.stop()                      # 在消费线程里调 stop()

        self.start_transport(fake_transport, on_event=callback)
        self.assertTrue(wait_until(lambda: fired == [1]))
        self.assertTrue(wait_until(lambda: not fake_transport.running, timeout=3.0))
        fake_transport.stop()

    def test_running_flag_and_stats(self):
        fake_transport = _ScriptedBase(
            open_script=[_GateConn()], next_script=["a", "b"],
            min_backoff=0.01, name=uniq_name("bo"),
        )
        self.assertFalse(fake_transport.running)      # 还没 start
        events = self.start_transport(fake_transport)
        self.assertTrue(wait_until(lambda: events == ["a", "b"]))
        stats = fake_transport.stats()
        self.assertEqual(stats["connects"], 1)
        self.assertEqual(stats["events"], 2)
        self.assertEqual(stats["errors"], 0)
        self.assertGreater(stats["idle"], 0)
        fake_transport.stop()

    def test_start_requires_callable(self):
        fake_transport = _ScriptedBase(name=uniq_name("bo"))
        with self.assertRaises(TypeError):
            fake_transport.start(None)                # type: ignore[arg-type]


# ----------------------------------------------------------------------
# 周期钩子（on_tick / tick_interval）
# ----------------------------------------------------------------------
class _OrderTicked(_ScriptedBase):
    """把 ``tick`` 与 ``_next`` 的调用顺序记进一条共享日志。"""

    def __init__(self, order: list[str], tick, **kw) -> None:
        self.order = order
        super().__init__(on_tick=tick, **kw)

    def _next(self, conn):
        self.order.append("next")
        return super()._next(conn)


class TestPeriodicHook(TransportTestCase):
    """``on_tick`` / ``tick_interval`` 的两条触发时机 + 三条铁律。

    两种模式（见 ``transport/base.py`` 模块 docstring「周期钩子」）：

    * ``tick_interval <= 0`` —— **循环驱动**：每次取下一条数据之前调一次。IO 超时
      也算一轮，所以与 IRC 迁移前 ``_IrcTransport._next`` 里的 tick 逐项等价。
    * ``tick_interval > 0`` —— **定时驱动**：另起 daemon 线程，给"``_next`` 会
      长时间阻塞"的长连接用（WS ``recv()``；Discord 心跳就靠它）。
    """

    # -- 循环驱动 -------------------------------------------------------
    def test_loop_tick_runs_before_every_fetch(self):
        order: list[str] = []
        fake_transport = _OrderTicked(
            order, lambda: order.append("tick"),
            open_script=[_GateConn()], next_script=["a", "b"],
            min_backoff=0.01, idle_delay=0.01, name=uniq_name("tick"),
        )
        events = self.start_transport(fake_transport)
        self.assertTrue(wait_until(lambda: events[:2] == ["a", "b"]))
        # 严格交替：tick 必须在对应那次 _next **之前**（不是攒到后面一起发）
        self.assertGreaterEqual(order.count("tick"), 2, order)
        for index, item in enumerate(order[:4]):
            self.assertEqual(
                item, "tick" if index % 2 == 0 else "next",
                f"tick 与 _next 的顺序不对: {order[:6]}",
            )

    def test_io_timeout_round_still_ticks(self):
        """IO 超时（:data:`NOTHING`）也是一轮 —— 这正是 IRC 迁移前的语义。"""
        fake_transport = _ScriptedBase(
            open_script=[_GateConn()], next_script=[NOTHING],
            min_backoff=0.01, idle_delay=0.005,
            on_tick=lambda: None, name=uniq_name("tick"),
        )
        self.start_transport(fake_transport)
        self.assertTrue(wait_until(lambda: fake_transport.stats()["idle"] >= 3))
        self.assertGreaterEqual(
            fake_transport.stats()["ticks"], fake_transport.stats()["idle"],
            f"每一轮空转都要 tick 一次: {fake_transport.stats()}",
        )
        self.assertEqual(fake_transport.stats()["connects"], 1, "IO 超时不是掉线，不该重连")
        self.assertEqual(fake_transport.stats()["errors"], 0)

    # -- 定时驱动 -------------------------------------------------------
    def test_timer_tick_fires_while_the_read_is_blocked(self):
        """``_next`` 一直阻塞时也必须按 ``tick_interval`` 触发。

        这条是 Discord 心跳能成立的前提：WS 的 ``recv()`` 会阻塞整整一个读超时，
        而心跳周期比它短 —— 靠循环驱动的话心跳会被拖到读超时之后（超过服务端容忍
        的 1.25 倍）→ 连接被判死。
        """
        ticks: list[int] = []
        conn = _GateConn()
        fake_transport = _BlockingTransport(
            conn, block=30.0, on_tick=lambda: ticks.append(1),
            tick_interval=0.01, name=uniq_name("tick"),
        )
        self.start_transport(fake_transport)
        self.assertTrue(
            wait_until(lambda: len(ticks) >= 5, timeout=3.0),
            f"阻塞中的读也要触发周期钩子，实际 {len(ticks)} 次",
        )
        self.assertEqual(fake_transport.stats()["connects"], 1)

    def test_timer_tick_only_fires_while_a_session_is_active(self):
        """退避（没有活动会话）期间**不许**触发 —— 否则适配器会对着已关的连接保活。"""
        ticks: list[int] = []
        fake_transport = _ScriptedBase(
            open_script=[ConnectionError("down")] * 999,
            on_tick=lambda: ticks.append(1), tick_interval=0.01,
            min_backoff=0.01, max_backoff=0.02, name=uniq_name("tick"),
        )
        self.start_transport(fake_transport)
        self.assertTrue(wait_until(lambda: fake_transport.stats()["sessions"] >= 2))
        time.sleep(0.08)                       # 足够它"想" tick 十几次
        self.assertEqual(ticks, [], "没有活动会话时不该触发周期钩子")
        self.assertEqual(fake_transport.stats()["ticks"], 0)

    def test_timer_tick_interval_can_be_tightened_at_runtime(self):
        """适配器在协商到周期之后才收紧粒度（Discord 在 HELLO 之后才拿到 41s）。

        手法：先按 0.2s 粒度跑（计数**上界**断言 —— 只会更少），再在运行期改成
        0.01s，计数必须**涨**（下界断言 —— 等待只会更长）。两条断言都不含容差，
        所以不会因机器负载而 flaky。
        """
        ticks: list[int] = []
        conn = _GateConn()
        fake_transport = _BlockingTransport(
            conn, block=30.0, on_tick=lambda: ticks.append(1),
            tick_interval=0.2, name=uniq_name("tick"),
        )
        self.start_transport(fake_transport)
        self.assertTrue(wait_until(lambda: len(ticks) >= 1))
        time.sleep(0.25)
        coarse = len(ticks)
        self.assertLessEqual(coarse, 3, f"tick_interval=0.2 不该这么密: {coarse}")
        # 用 tune_tick_interval：光改属性的话定时线程还睡在当前那一拍（0.2s）里，
        # 第一次心跳会被拖满 —— 那正是"周期看起来慢一倍"的错觉来源。
        fake_transport.tune_tick_interval(0.01)
        self.assertEqual(fake_transport.tick_interval, 0.01)
        self.assertTrue(
            wait_until(lambda: len(ticks) >= coarse + 5, timeout=3.0),
            "tune_tick_interval 必须立刻生效（唤醒定时线程）",
        )

    # -- 铁律：异常隔离 -------------------------------------------------
    def test_loop_tick_exception_does_not_end_the_session(self):
        """循环驱动：钩子抛异常不许换连接、不许停消费 —— 继续派发后续事件。"""
        calls = {"n": 0}

        def boom() -> None:
            calls["n"] += 1
            raise RuntimeError("heartbeat blew up")

        fake_transport = _ScriptedBase(
            open_script=[_GateConn()], next_script=list(range(6)),
            min_backoff=0.01, idle_delay=0.005,
            on_tick=boom, name=uniq_name("tick"),
        )
        with self.assertLogs("opencode_bridge.transport.base", level="WARNING"):
            events = self.start_transport(fake_transport)
            self.assertTrue(wait_until(lambda: len(events) >= 6))
        self.assertGreaterEqual(calls["n"], 6, "钩子每一轮都要试一次")
        self.assertEqual(fake_transport.stats()["connects"], 1, "钩子出错不是连接出错，不该重连")
        self.assertGreaterEqual(fake_transport.stats()["errors"], 6, f"异常要记进 errors: {fake_transport.stats()}")
        self.assertTrue(fake_transport.running)

    def test_timer_tick_exception_does_not_end_the_session(self):
        """定时驱动同样隔离 —— 而且**不许**杀掉定时线程自己。"""
        calls = {"n": 0}

        def boom() -> None:
            calls["n"] += 1
            raise RuntimeError("ack check exploded")

        conn = _GateConn()
        fake_transport = _BlockingTransport(
            conn, block=30.0, on_tick=boom, tick_interval=0.01,
            name=uniq_name("tick"),
        )
        with self.assertLogs("opencode_bridge.transport.base", level="WARNING"):
            self.start_transport(fake_transport)
            self.assertTrue(wait_until(lambda: calls["n"] >= 3, timeout=3.0))
        self.assertEqual(fake_transport.stats()["connects"], 1)
        self.assertGreaterEqual(fake_transport.stats()["errors"], 3, f"{fake_transport.stats()}")
        self.assertTrue(fake_transport.running, "钩子异常不许杀死消费线程")

    # -- 铁律：未配置时零开销 -------------------------------------------
    def test_no_hook_means_no_thread_and_no_ticks(self):
        fake_transport = _ScriptedBase(
            open_script=[_GateConn()], next_script=["a"], min_backoff=0.01,
            name=uniq_name("tick"),
        )
        self.start_transport(fake_transport)
        self.assertTrue(wait_until(lambda: fake_transport.stats()["events"] >= 1))
        self.assertIsNone(fake_transport.on_tick)
        self.assertFalse(fake_transport._loop_ticks, "未配钩子时循环里不该有 tick 分支")
        self.assertFalse(fake_transport._timer_ticks)
        self.assertIsNone(fake_transport._tick_thread, "不该起定时线程")
        self.assertEqual(fake_transport.stats()["ticks"], 0)
        names = [th.name for th in threading.enumerate()]
        self.assertNotIn(fake_transport.tick_thread_name, names)
        fake_transport.stop()                # stop() 不该去 join 一个不存在的线程
        self.assertFalse(fake_transport.running)

    def test_non_callable_hook_rejected(self):
        with self.assertRaises(TypeError):
            _ScriptedBase(on_tick="not callable")  # type: ignore[arg-type]

    def test_stop_interrupts_a_coarse_tick_wait(self):
        """``stop()`` 必须**立刻**唤醒定时线程，哪怕粒度是 30s。

        这条来自一个真 bug：定时线程阻塞在 ``_tick_wake.wait(粒度)`` 上，只置
        ``_tick_stop`` 唤不醒它 → ``join`` 白等满超时，而那个线程会在关连接之后
        继续往一条正在关的连接上写（生产里粒度是 5s，所以表现为"stop() 之后还冒出
        一拍心跳"）。

        断言用**线程是否消失**（内部状态）而不是墙钟。
        """
        ticks: list[int] = []
        conn = _GateConn()
        fake_transport = _BlockingTransport(
            conn, block=30.0, on_tick=lambda: ticks.append(1),
            tick_interval=30.0, name=uniq_name("tick"),
        )
        self.start_transport(fake_transport)
        self.assertTrue(wait_until(lambda: fake_transport.running))
        self.assertTrue(
            any(th.name == fake_transport.tick_thread_name for th in threading.enumerate()),
            "定时线程应该已经起来",
        )
        fake_transport.stop(timeout=5.0)
        self.assertFalse(
            any(th.name == fake_transport.tick_thread_name for th in threading.enumerate()),
            "粒度 30s 时 stop() 也必须立刻收掉定时线程（不许睡满那一拍）",
        )
        self.assertIsNone(fake_transport._tick_thread)
        self.assertFalse(fake_transport.running)
        settled = len(ticks)
        time.sleep(0.05)
        self.assertEqual(len(ticks), settled, "stop() 之后不该再有任何 tick")

    def test_stop_halts_the_hook_before_closing_the_connection(self):
        """``stop()`` 的三段式：**停钩子 → 关连接 → join**。

        顺序用事件日志证明（与不变量 1 同一手法）：``Transport.stop()`` 会 join
        定时线程之后才调 ``_close_conn``，所以**任何** tick 都必须排在 ``close``
        之前；消费线程的 ``thread_end`` 又必须排在 ``close`` 之后。
        """
        order: list[str] = []
        conn = _GateConn(order)
        fake_transport = _BlockingTransport(
            conn, block=30.0,
            on_tick=lambda: order.append("tick"),
            tick_interval=0.01, name=uniq_name("tick"),
        )
        self.start_transport(fake_transport)
        self.assertTrue(wait_until(lambda: order.count("tick") >= 2))
        fake_transport.stop()
        self.assertEqual(order.count("close"), 1)
        last_tick = max(i for i, x in enumerate(order) if x == "tick")
        self.assertLess(
            last_tick, order.index("close"),
            f"周期钩子必须先于关连接停下: {order}",
        )
        self.assertLess(
            order.index("close"), len(order) - 1,
            f"关连接必须先于消费线程结束: {order}",
        )
        self.assertEqual(order[-1], "thread_end")
        self.assertFalse(
            any(th.name == fake_transport.tick_thread_name for th in threading.enumerate()),
            "stop() 之后定时线程必须已退出",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
"""缺陷 B 的**基线**：``/api/event`` 非 200 ⇒ SSE 线程结束，而**没有东西**把它拉回来。

⚠️⚠️ **本文件在当前实现上是绿的 —— 这是刻意的，请不要把它读成「通过 = 没问题」。**

这三条用例钉的是**现状**，不是一份契约。它们的唯一用途是给未来的实现留一份
**「改之前长什么样」的基线**：一旦有人给 ``subscribe`` / :meth:`EventStream.run`
加上重连与看护，**这几条必须一起改**，而改动的那一刻就是「行为变了」的证据。

⛔ 这里**不替未来的实现写断言**（「应当重连 N 次」之类）—— 那是实现决策，本轮
不归这些用例定。

## 被钉住的三件事

1. :meth:`OpenCodeClient.subscribe` 遇到 ``/api/event`` 非 200 时**直接把异常抛出去**，
   抛出之前**没有**任何一次重连尝试（``urlopen`` 只被调一次）。
2. :meth:`EventStream.run` 把那个异常记一行 ``event stream terminated`` 之后
   **返回** —— 于是 SSE 线程结束，终态事件一个都收不到，每轮只留一个
   「⏳ 处理中…」，答复静默丢失且**不自愈**。
3. 线程结束后**没有任何东西**会重新拉起它：全仓只有一处引用
   ``event_stream.run``（:meth:`BridgeCore.start` 里的那个 ``Thread`` target），
   而 :class:`BridgeCore` 里没有任何一处拿这条线程的存活状态去决定「要不要重启」。
"""

from __future__ import annotations

import ast
import io
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


def http_error(code: int, body: bytes = b"") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        EVENT_ENDPOINT, code, "HTTP %d" % code, {}, io.BytesIO(body),
    )


class _NoOpState:
    """``EventStream`` 只读 ``all_sessions()``；这里让它读到一个空清单。"""

    def all_sessions(self) -> dict:
        return {}


class StreamThatRefuses(FakeClient):
    """:meth:`OpenCodeClient.subscribe` 的替身：像真的非 200 那样把异常抛出去。

    ⚠️ **刻意做成生成器**（末尾那个 ``yield``）：真客户端的 ``subscribe`` 就是生成器，
    异常在第一次 ``next()`` 时才抛，所以这个替身与它同形 —— 写成普通函数会让
    「异常发生在调用处还是迭代处」这件事变得不一样，而那正是
    :meth:`EventStream.run` 里 ``for`` 语句要看清的东西。
    """

    def __init__(self) -> None:
        super().__init__()
        self.subscribe_calls = 0
        self.subscribe_entered = threading.Event()

    def subscribe(self, *, restart: bool = True):
        self.subscribe_calls += 1
        self.subscribe_entered.set()
        raise OpenCodeError(
            "GET /api/event -> HTTP %d" % SERVICE_UNAVAILABLE,
            status=SERVICE_UNAVAILABLE,
        )
        yield  # pragma: no cover - 只为让它成为生成器


def build_event_stream(client) -> EventStream:
    """造一套 ``EventStream``（十三个协作者全是替身或空实现）。"""
    return EventStream(
        client=client,
        state=_NoOpState(),
        lock=threading.RLock(),
        turns={},
        clock=lambda: 1000.0,
        edit_interval=1.5,
        max_message_chars=4000,
        adapter_for=lambda conversation_id: None,
        send_text=lambda *args, **kwargs: None,
        edit_progress=lambda *args, **kwargs: False,
        finalize=lambda *args, **kwargs: None,
        flush_queue=lambda conversation_id: None,
        permission_ledger=PermissionLedger(),
    )


class SubscribePropagatesTheNon200Tests(unittest.TestCase):
    """① ``subscribe`` 把非 200 抛出去，**且抛出前没有重连**。"""

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
        """⭐ 这条是「没有重连分支」的可观察证据。

        ⚠️ **基线**：将来若给 ``OpenCodeError`` 加上重连，``urlopen`` 就会被调第二次，
        这条随之变红 —— **那正是应该变红**：它是「行为变了」的证据，不是回归。
        """
        client = OpenCodeClient(Endpoint("http://127.0.0.1:4097", "pw"))

        with mock.patch(
            "urllib.request.urlopen",
            side_effect=http_error(SERVICE_UNAVAILABLE),
        ) as urlopen:
            with self.assertRaises(OpenCodeError):
                list(client.subscribe(restart=True))

        self.assertEqual(
            urlopen.call_count, 1,
            "抛出前 urlopen 被调了 %d 次 —— 非 200 已经在尝试重连了。"
            % urlopen.call_count,
        )


class TheStreamThreadEndsTests(unittest.TestCase):
    """② :meth:`EventStream.run` **返回**（而不是阻塞 / 重试）⇒ 线程结束。"""

    def test_run_returns_instead_of_blocking_when_the_stream_refuses(self):
        client = StreamThatRefuses()
        stream = build_event_stream(client)

        worker = threading.Thread(target=stream.run, name="sse-under-test", daemon=True)
        worker.start()
        worker.join(timeout=RENDEZVOUS_TIMEOUT_SECONDS)

        self.assertFalse(
            worker.is_alive(),
            "run() 还在阻塞 —— 它没有在那个异常之后返回。",
        )
        self.assertEqual(client.subscribe_calls, 1)
        self.assertFalse(
            stream.stream_confirmed.is_set(),
            "一帧都没收到，而信号却置上了 —— 收件箱恢复会以为事件流通了。",
        )

    def test_run_says_the_stream_terminated(self):
        client = StreamThatRefuses()
        stream = build_event_stream(client)

        with self.assertLogs("opencode_bridge.event_stream", level="ERROR") as logs:
            stream.run()

        self.assertTrue(
            any("event stream terminated" in line for line in logs.output),
            "没有留下那行日志：%r" % (logs.output,),
        )


class NothingRelaunchesTheStreamTests(unittest.TestCase):
    """③ 线程结束后**没有任何东西**会把它拉回来。"""

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

    def test_the_stream_thread_stays_dead_after_it_returns(self):
        self.core.start()
        self.assertTrue(
            self.client.subscribe_entered.wait(timeout=RENDEZVOUS_TIMEOUT_SECONDS),
            "SSE 线程根本没有进入 subscribe()。",
        )
        thread = self.core._thread
        self.assertIsNotNone(thread)
        thread.join(timeout=RENDEZVOUS_TIMEOUT_SECONDS)

        self.assertFalse(thread.is_alive(), "SSE 线程还活着 —— 它没有结束。")
        self.assertEqual(
            self.client.subscribe_calls, 1,
            "订阅被进入 %d 次 —— 有人重试了。" % self.client.subscribe_calls,
        )
        self.assertFalse(self.core.event_stream.stream_confirmed.is_set())

    def test_asking_the_core_to_start_again_does_not_revive_a_dead_stream(self):
        """⛔ 「再调一次 :meth:`BridgeCore.start`」是最自然的自愈尝试，而它**无效**。

        ``start()`` 的 ``_started`` 闩让第二次调用直接返回 ⇒ 一条死掉的 SSE 线程
        就此永远留在死掉的状态里，而进程余下所有答复静默丢失。
        """
        self.core.start()
        thread = self.core._thread
        thread.join(timeout=RENDEZVOUS_TIMEOUT_SECONDS)
        self.assertFalse(thread.is_alive())

        self.core.start()

        self.assertIs(self.core._thread, thread, "start() 换了另一条线程。")
        self.assertFalse(thread.is_alive(), "第二次 start() 把死线程复活了。")
        self.assertEqual(self.client.subscribe_calls, 1)


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
    ``ast.Attribute`` 上 ``attr == "run"`` 且点号链里带 ``event_stream`` 的节点。
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
    """③ 的**结构面**：源码里根本不存在「看着线程死了就重启」的那一处。

    ⚠️ **基线**：这些数是**量出来的**，不是抄来的（口径写在每条断言的注释里）。
    有人加了看护线程，它们就会变红 —— 那时该做的是**连同上面那几条一起**更新基线。
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

        ⚠️ 口径先说清：**全包** ``is_alive()`` 共 **13** 处（适配器轮询线程、
        进程 pid、传输层……），所以「0 命中」那句话**对包不成立**；本条只问
        ``core.py`` —— 那条线程唯一可能被看护的地方。实测那里**恰好 1** 处，
        位于 :meth:`BridgeCore.stop` 的 ``join`` 之后，只打一行 warning。
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
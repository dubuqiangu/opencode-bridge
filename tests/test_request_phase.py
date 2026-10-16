"""A2：传输层失败的两档 —— 「可证明从未提交」的判据与它的接线。

判据 = 异常链里出现 ``ConnectionRefusedError`` 或 ``socket.gaierror``。二者只可能
产生在**建连 / DNS 阶段**（那一刻 HTTP 请求一个字节都没发出去），所以「从未提交」
是**可证的**。其余一律按「可能已经跑过」处理（fail-safe）。

⚠️ 下面每条臂的**形状**都是本机**实测值**（§7.1：先打实际值 → 从那个值里抄 →
才写断言），⛔ 不是推测出来的：

======================  ==================================================
连接被拒（回环口已关）  ``urllib.error.URLError``，且 ``reason`` 是
                        ``ConnectionRefusedError``（WinError 10061）
accept 后立刻 close     ``ConnectionResetError``（WinError 10054）——**直接抛**，
                        不裹在 ``URLError`` 里
accept 后永不答复       ``TimeoutError``——同样**直接抛**
======================  ==================================================

⛔ **DNS 臂在本机测不到那个形状**：``no-such-host.invalid`` 被中间解析器答成了
``HTTPError 400``（那会被 ``_request`` 的第一支 `except` 接走、进 status 档）。
⇒ 故 ``socket.gaierror`` 那一条用**构造**的异常覆盖，且用例名与注释都写明它是构造的。

⚠️ 后两条实测形状（reset / timeout）**必须**回 ``False``：那时候请求字节**已经
发出去**了，服务端可能已经跑过 —— 把它们判成「从未提交」就是把「不知道」说成
「一定没送到」（AGENTS.md §8 第 3 条）。
"""

from __future__ import annotations

import http.client
import socket
import threading
import unittest
import urllib.error
import urllib.request

from opencode_bridge.opencode_client import OpenCodeClient
from opencode_bridge.opencode_wire import Endpoint, OpenCodeError
from opencode_bridge.request_phase import (
    _NEVER_LEFT_THE_HOST,
    _the_exception_chain,
    request_definitely_never_sent,
)


def _a_loopback_port_that_refuses_connections() -> int:
    """绑一个回环端口**再关掉**它 —— 之后连它必被立刻拒绝。"""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def _a_listener_that_accepts_then_closes_immediately() -> int:
    """绑一个回环监听口，accept 一个连接后**立刻 close**（⛔ 一个字节都不写）。"""
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def accept_then_close() -> None:
        try:
            connection, _ = listener.accept()
            connection.close()
        except OSError:
            pass
        finally:
            listener.close()

    threading.Thread(target=accept_then_close, daemon=True).start()
    return port


def _a_listener_that_accepts_then_never_answers() -> tuple[int, list]:
    """绑一个回环监听口，accept 后**不写任何响应** ⇒ 客户端必然读到超时。

    返回的第二项是该臂**打开的全部 socket**（监听口 + 已接受的连接），
    调用方负责关掉它们：
    ⛔ 已接受的连接不能被回收，否则对端会看到一次 close —— 那这条臂就变成了
    「accept 后立刻 close」，测的是**另一个**形状。
    """
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    open_sockets_to_close: list = [listener]

    def accept_and_hold() -> None:
        try:
            connection, _ = listener.accept()
            open_sockets_to_close.append(connection)
        except OSError:
            pass

    threading.Thread(target=accept_and_hold, daemon=True).start()
    return port, open_sockets_to_close


# ----------------------------------------------------------------------
# 「可证明没离开本机」那一档
# ----------------------------------------------------------------------
class TestWhatCountsAsProofTheRequestNeverLeftTheHost(unittest.TestCase):
    def test_a_live_refused_loopback_connection_is_proof(self):
        """实测形状：``URLError`` 且 ``reason`` 是 ``ConnectionRefusedError``。"""
        port = _a_loopback_port_that_refuses_connections()
        with self.assertRaises(urllib.error.URLError) as caught:
            urllib.request.urlopen("http://127.0.0.1:%d/x" % port, timeout=5)

        self.assertIsInstance(
            caught.exception.reason, ConnectionRefusedError,
            "实测（WinError 10061）就藏在 URLError.reason 里 —— 这正是判据必须"
            "往下追 reason 而不只看异常类型的原因",
        )
        self.assertTrue(request_definitely_never_sent(caught.exception))

    def test_a_constructed_dns_failure_is_proof(self):
        """⚠️ **构造**的异常，⛔ 不是实测 —— 理由见模块 docstring（本机 DNS 臂
        被中间解析器答成 HTTP 400，``socket.gaierror`` 观测不到）。"""
        dns_failure = urllib.error.URLError(socket.gaierror(11001, "no address"))
        self.assertTrue(request_definitely_never_sent(dns_failure))

    def test_the_judgment_follows_raise_from_like_the_client_does(self):
        """``_request`` 用 ``raise ... from exc`` —— 判定必须沿着 ``__cause__`` 走。"""
        try:
            try:
                raise ConnectionRefusedError(10061, "refused")
            except ConnectionRefusedError as transport_failure:
                raise OpenCodeError("GET /x failed: refused") from transport_failure
        except OpenCodeError as wrapped:
            self.assertTrue(request_definitely_never_sent(wrapped))
        else:
            self.fail("上面那段必然抛出 OpenCodeError")

    def test_the_judgment_follows_an_implicit_context_chain(self):
        """隐式链（没有 ``from``）同样要走 —— 实测的 ``URLError`` 就是这种。"""
        try:
            try:
                raise ConnectionRefusedError(10061, "refused")
            except ConnectionRefusedError:
                raise OpenCodeError("GET /x failed: refused")
        except OpenCodeError as wrapped:
            self.assertIsNone(wrapped.__cause__, "这一臂**刻意**不写 from")
            self.assertTrue(request_definitely_never_sent(wrapped))
        else:
            self.fail("上面那段必然抛出 OpenCodeError")


# ----------------------------------------------------------------------
# 「可能已经跑过」那一档（fail-safe 的方向）
# ----------------------------------------------------------------------
class TestWhatTheJudgmentRefusesToCallProof(unittest.TestCase):
    def test_a_connection_reset_is_not_proof_because_bytes_had_been_sent(self):
        """⛔ 判据本体：实测 accept 后立刻 close 就是这个形状 ⇒ 必须回 ``False``。

        放宽成「凡是 ``OSError`` 就算没提交」会把这一档吞进「从未提交」
        ⇒ 那正是 §8 第 3 条禁止的「恢复一份没记录过的信息」。
        """
        self.assertFalse(
            request_definitely_never_sent(ConnectionResetError(10054, "reset by peer"))
        )
        self.assertNotIn(ConnectionResetError, _NEVER_LEFT_THE_HOST)
        self.assertNotIn(TimeoutError, _NEVER_LEFT_THE_HOST)

    def test_a_live_accepted_then_closed_connection_is_not_proof(self):
        port = _a_listener_that_accepts_then_closes_immediately()
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/x" % port, timeout=5)
        except Exception as caught:
            self.assertIsInstance(
                caught, OSError, "实测是 ConnectionResetError（WinError 10054）"
            )
            self.assertFalse(request_definitely_never_sent(caught))
        else:
            self.fail("一个 accept 后立刻 close 的服务端不该让 urlopen 成功")

    def test_a_live_silent_server_times_out_and_is_not_proof(self):
        """超时**不能**证明没提交：它可能发生在读过响应之后。"""
        port, open_sockets_to_close = _a_listener_that_accepts_then_never_answers()
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/x" % port, timeout=0.5)
        except Exception as caught:
            self.assertIsInstance(caught, TimeoutError, "实测是 TimeoutError")
            self.assertFalse(request_definitely_never_sent(caught))
        else:
            self.fail("一个永不答复的服务端不该让 urlopen 成功")
        finally:
            for opened_socket in open_sockets_to_close:
                opened_socket.close()

    def test_a_remote_disconnect_is_not_proof(self):
        """⛔ ``RemoteDisconnected`` 是 ``ConnectionResetError`` 的子类 ⇒ 必须回 False。"""
        self.assertTrue(issubclass(http.client.RemoteDisconnected, ConnectionResetError),
                        "这条断言的**前提**：它确实是 ConnectionResetError 的子类")
        self.assertFalse(
            request_definitely_never_sent(http.client.RemoteDisconnected("remote closed"))
        )

    def test_a_plain_oserror_and_a_bare_exception_are_not_proof(self):
        self.assertFalse(request_definitely_never_sent(OSError("something else")))
        self.assertFalse(request_definitely_never_sent(ValueError("not a transport failure")))

    def test_a_urlerror_whose_reason_is_not_an_exception_is_not_proof(self):
        """``URLError.reason`` 不保证是异常（也可以是字符串）⇒ 追链不能炸。"""
        self.assertFalse(
            request_definitely_never_sent(urllib.error.URLError("plain string reason"))
        )


# ----------------------------------------------------------------------
# 追链本身
# ----------------------------------------------------------------------
class TestTheChainWalkItself(unittest.TestCase):
    def test_a_self_referencing_context_terminates(self):
        """异常链可以自我回环 ⇒ ⛔ 无限递归在这里是**崩溃**，不是报错。"""
        looping = ValueError("loop")
        looping.__context__ = looping

        self.assertEqual(list(_the_exception_chain(looping)), [looping],
                         "同一个对象只许出现一次")
        self.assertFalse(request_definitely_never_sent(looping))

    def test_a_cause_and_context_pair_yields_the_outer_one_first(self):
        outer = ValueError("outer")
        outer.__context__ = KeyError("inner")
        self.assertEqual([type(node) for node in _the_exception_chain(outer)],
                         [ValueError, KeyError])


# ----------------------------------------------------------------------
# 接线：形状只在 ``_request`` 那一层可见，所以判定必须在那里做出来
# ----------------------------------------------------------------------
class TestTheClientStampsTheJudgmentOnTheErrorItRaises(unittest.TestCase):
    def test_a_refused_endpoint_raises_a_transport_error_flagged_never_sent(self):
        port = _a_loopback_port_that_refuses_connections()
        client = OpenCodeClient(
            Endpoint(url="http://127.0.0.1:%d" % port, password="pw"), timeout=5
        )

        with self.assertRaises(OpenCodeError) as caught:
            client._request("GET", "/x")

        self.assertIsNone(caught.exception.status, "传输层失败没有 HTTP status")
        self.assertTrue(caught.exception.request_never_sent)

    def test_an_accepted_then_closed_endpoint_raises_a_transport_error_not_flagged(self):
        port = _a_listener_that_accepts_then_closes_immediately()
        client = OpenCodeClient(
            Endpoint(url="http://127.0.0.1:%d" % port, password="pw"), timeout=5
        )

        with self.assertRaises(OpenCodeError) as caught:
            client._request("GET", "/x")

        self.assertIsNone(caught.exception.status)
        self.assertFalse(caught.exception.request_never_sent)

    def test_the_flag_defaults_to_false_so_existing_callers_are_unchanged(self):
        """⛔ 缺省必须是保守档：既有全部 ``OpenCodeError(...)`` 调用点逐字节不变。"""
        legacy = OpenCodeError("POST /session/x -> timed out")
        self.assertFalse(legacy.request_never_sent)
        self.assertFalse(getattr(legacy, "request_never_sent", False))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

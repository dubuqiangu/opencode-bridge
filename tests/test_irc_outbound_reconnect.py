"""IRC 出站分片「跨重连」的行为锁定（**纯加测试**，不改任何生产行为）。

背景
----
IRC 适配器迁到 :mod:`opencode_bridge.transport` 之后，出站分片的行为变了一处。
``tasks.md``（A1/A2 迁移第 1 家：IRC）把它记成两条真实差异之一，并判定**新行为更好**：

* **迁移前**：``send()`` 一次抓一个 socket，所有分片写进它；连接在**分片中途**死掉时
  剩余分片写失败 ⇒ 返回 ``TRANSIENT`` + 一个部分句柄。
* **迁移后**：每一片都重新读 ``transport.connection``，所以连接换了的话，**后半片会
  发到新连接上**，整条消息仍然发完。

判定理由是「IRC 不关心 TCP 边界，每片本来就是独立的 ``PRIVMSG``，能发完比报错更符合
预期」。⚠️ 但台账同时写明这条差异**没有测试覆盖**，理由是「稳定复现需要在分片之间掐
连接」。本模块把它钉死。

怎么做到稳定（不用 sleep、不看墙钟）
------------------------------------
用**真服务器 + 后台重连线程**掐连接是**做不到确定**的：第 2 片落在死连接上还是新连接
上，取决于发送线程与消费线程谁先跑到 —— 那正是本项目已经踩过多次的墙钟 flaky 坑
（transport reset 用例、IRC 退避、heartbeat 三处都因此返工过）。所以这里换一种做法：

1. 用 :func:`socket.socketpair` 造两条**真实**连接。字节是真的、行尾是真的、顺序是真的，
   只把「当前连接是谁」与「连接何时被换掉」脚本化；
2. 换连接的动作发生在**第 N 次 ``sendall`` 的内部** —— 也就是分片循环当中、在**同一个
   线程里同步**完成。于是「第 1 片之后连接被替换」是一个**确定的状态转移**：不需要
   sleep、不需要轮询、不依赖线程调度；
3. 断言全部落在**行为**上：哪条连接收到了**逐字节相同**的哪些行、``SendResult`` 是什么
   形状。不读 ``send()`` / ``send_line()`` 的任何私有实现细节（既有的退避用例是白盒读
   ``_backoff`` 的，传输层改判定方式就集体失效 —— 这里刻意不重复那个毛病）。

被脚本化的只有「连接对象是谁」和「连接何时被换掉」。
:mod:`opencode_bridge.adapters.irc` 的 :meth:`IRCAdapter.send` /
:meth:`IRCAdapter._write_line` 与 :meth:`TcpLineTransport.send_line`
**全是生产实现**；换连接用的也是生产 ``Transport._run`` 里那把锁与那个槽位。

⛔ 刻意**不**走 ``Transport._run`` 的重连循环：它带退避与后台线程，而这条用例需要的
恰恰是「掐连接的时刻」是确定的。退避与重连机制本身由 ``TestIRCLifecycle`` /
``TestIRCMigrationInvariants`` 覆盖，本模块不重复。
"""

from __future__ import annotations

import socket
import time
import unittest

from opencode_bridge.adapters.irc import LINE_LIMIT, MESSAGE_LIMIT
from opencode_bridge.hooks import Outbound, SendError

from tests.test_irc import CHAN, make_irc

#: 本模块所有用例共用的正文：``CHUNK_COUNT`` 片、每片正好 ``MESSAGE_LIMIT`` 个字符。
#:
#: 全是 ASCII，所以 ``split_text`` 的字符切（400）与 ``_byte_safe_split`` 的字节兜底
#: （预算 495 字节）都不会二次切割 —— 分片数就是 :data:`CHUNK_COUNT`，可精确推算。
CHUNK_COUNT = 3
CHUNK_BODY = "x" * MESSAGE_LIMIT
CHUNK_TEXT = CHUNK_BODY * CHUNK_COUNT


def _expected_chunk_bytes(index: int) -> bytes:
    """第 ``index`` 片在真实线路上应当出现的**逐字节**内容（含 ``CRLF``）。

    ``index`` 目前不参与计算（三片正文相同），但保留参数是为了让调用点自解释：
    读的人一眼看到「这一坨字节是第几片的」。
    """
    return f"PRIVMSG {CHAN} :{CHUNK_BODY}\r\n".encode("utf-8")


def _install_connection(transport, connection) -> None:
    """把传输层的当前连接换掉 —— 与生产 ``Transport._run`` 逐字一致：
    ``with self._conn_lock: self._conn = conn``（同一把锁、同一个槽位）。
    """
    with transport._conn_lock:
        transport._conn = connection


def _read_exactly(sock: socket.socket, size: int, timeout: float = 5.0) -> bytes:
    """从对端读**恰好** ``size`` 个字节；不够就抛 ``AssertionError``（绝不把
    「少读到」当成功返回）。

    ``timeout`` 只用来**兜住失败**（生产行为变了才会走到这里），不参与判据：
    成功路径上这些字节在 :meth:`IRCAdapter.send` 返回前就已经进了内核缓冲区，
    :meth:`recv` 立刻拿到 —— 因此本模块**没有任何一条墙钟断言**，整组用例
    （含建立 12 条 socketpair）实测在 0.1 秒内跑完。
    """
    collected = bytearray()
    deadline = time.monotonic() + timeout
    while len(collected) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(
                f"{timeout}s 内只从这条连接读到 {len(collected)} 字节，"
                f"期望 {size} 字节 —— 剩下的分片没有落到这条连接上"
            )
        sock.settimeout(remaining)
        chunk = sock.recv(size - len(collected))
        if not chunk:
            raise AssertionError(
                f"期望 {size} 字节却只读到 {len(collected)} 字节（对端已关闭）"
            )
        collected += chunk
    return bytes(collected)


def _chunk_lines(raw: bytes) -> list[str]:
    """把收到的字节按 CRLF 拆成 IRC 行（不含行尾），顺序原样保留。"""
    return [line for line in raw.decode("utf-8").split("\r\n") if line]


class _SwappableConnection:
    """一条**真实** socket 的连接替身：第 ``swap_after`` 次写出之后换成另一条连接。

    * :meth:`sendall` **转发给真实 socket** —— 对端收到的字节、行尾、顺序与生产一致；
    * 写出计数到 ``swap_after`` 时调 ``switch_to()``（把传输层的当前连接换成另一条），
      模拟读循环在分片中途重连；
    * :attr:`writes` / :attr:`swaps` 是**反空转**判据：断言靠它们确认「换连接这件事
      真的发生了」，否则用例会因为「没换」而空转变绿。

    传输层关连接的路径只用 :meth:`shutdown` 与 :meth:`close`（见
    ``TcpLineTransport._close_conn``），所以替身只需要这四个方法。
    """

    def __init__(self, sock: socket.socket, *, switch_to, swap_after: int) -> None:
        self._sock = sock
        self._switch_to = switch_to
        self._swap_after = swap_after
        self.writes = 0
        self.swaps = 0

    def sendall(self, payload: bytes) -> None:
        self._sock.sendall(payload)
        self.writes += 1
        if self.writes == self._swap_after:
            self._switch_to()
            self.swaps += 1

    def shutdown(self, how: int) -> None:
        self._sock.shutdown(how)

    def close(self) -> None:
        self._sock.close()


class _FailingWriteConnection:
    """写必失败的连接（``sendall`` 抛错），用来观察「换连接后仍然写不出去」的边界。"""

    def __init__(self) -> None:
        self.writes = 0

    def sendall(self, payload: bytes) -> None:
        self.writes += 1
        raise ConnectionError("replacement connection is already dead")

    def shutdown(self, how: int) -> None:
        pass

    def close(self) -> None:
        pass


class TestIRCOutboundChunksSpanReconnect(unittest.TestCase):
    """分片中段换连接：**剩余分片发到新连接**，整条消息仍报成功。

    判据全是确定性状态（哪条连接收到哪些字节、``SendResult`` 的形状），
    没有一条依赖墙钟间隔。
    """

    # -- 夹具 ----------------------------------------------------------
    def _socket_pairs(self, count: int):
        """``count`` 组 socketpair，返回 (传输层端, 对端) 两个列表，两侧都登记清理。"""
        transport_ends: list[socket.socket] = []
        peer_ends: list[socket.socket] = []
        for _ in range(count):
            ours, theirs = socket.socketpair()
            self.addCleanup(ours.close)
            self.addCleanup(theirs.close)
            transport_ends.append(ours)
            peer_ends.append(theirs)
        return transport_ends, peer_ends

    def _adapter_with_replacement_connection(self, *, swap_after: int,
                                             replacement=None):
        """接好适配器，让第 ``swap_after`` 片写出后把传输层切到另一条连接。

        返回 ``(adapter, first_connection, transport_ends, peer_ends)``：
        ``peer_ends[i]`` 是第 ``i`` 条连接的对端，本模块把它当 IRC 服务器读行。
        """
        adapter, _ = make_irc()
        transport = adapter._make_transport()   # 生产 _IrcTransport（含真 send_line）
        adapter._transport = transport
        self.addCleanup(adapter.stop)          # 幂等；没有线程起过，也不会漏句柄
        transport_ends, peer_ends = self._socket_pairs(2)
        successor = transport_ends[1] if replacement is None else replacement
        first = _SwappableConnection(
            transport_ends[0],
            switch_to=lambda: _install_connection(transport, successor),
            swap_after=swap_after,
        )
        _install_connection(transport, first)
        return adapter, first, transport_ends, peer_ends

    def _assert_splits_into_several_chunks(self, adapter) -> None:
        """前置条件：:data:`CHUNK_TEXT` 确实会被切成 :data:`CHUNK_COUNT` 片（且 ≥ 2 片）。

        「跨重连」至少要两片，否则这个前提本身就没了 —— 所以显式断言，不靠
        「读字节超时」那种 5 秒之后的报错来间接暴露。

        判据只读**公开**属性
        :attr:`~opencode_bridge.adapters.base.Adapter.effective_max_length`
        （基类注释写明「这是唯一该读『我按多少切』的地方」），不反推
        ``_outbound_pieces`` 的实现形状。
        """
        self.assertGreaterEqual(CHUNK_COUNT, 2, "「跨重连」至少要两片")
        self.assertEqual(
            len(CHUNK_TEXT) // adapter.effective_max_length, CHUNK_COUNT,
            "正文长度与 effective_max_length 已不再切出 CHUNK_COUNT 片 —— "
            "先改 CHUNK_COUNT，否则下面的逐字节判据测的不是同一件事",
        )

    # -- 1：后半片真的落到新连接 -----------------------------------------
    def test_chunks_after_the_swap_reach_the_replacement_connection(self):
        """连接在**第 1 片之后**被替换 ⇒ 第 2、3 片发到**新连接**上。"""
        adapter, first, _ends, peers = self._adapter_with_replacement_connection(
            swap_after=1
        )
        self._assert_splits_into_several_chunks(adapter)
        adapter.send(Outbound(f"irc:{CHAN}", CHUNK_TEXT))

        # 反空转：换连接确实发生了，而且只发生在第 1 片之后。
        self.assertEqual(first.swaps, 1, "第 1 片写出后应把传输层切到新连接")
        self.assertEqual(first.writes, 1, "旧连接只应收到第 1 片")

        # 逐字节判据：第 1 片在旧连接上，第 2、3 片在新连接上，顺序原样。
        self.assertEqual(_read_exactly(peers[0], len(_expected_chunk_bytes(0))),
                         _expected_chunk_bytes(0), "第 1 片必须走旧连接")
        on_new = _read_exactly(
            peers[1],
            len(_expected_chunk_bytes(1)) + len(_expected_chunk_bytes(2)),
        )
        self.assertEqual(
            _chunk_lines(on_new),
            [f"PRIVMSG {CHAN} :{CHUNK_BODY}"] * 2,
            "剩余分片必须按原顺序发到新连接上",
        )

    # -- 2：结果是成功（句柄指向最后一片），不是 TRANSIENT -----------------
    def test_mid_chunk_reconnect_is_reported_as_success_not_transient(self):
        """迁移后的行为：分片中段换连接**不**算失败 —— 整条消息算发完。"""
        adapter, _first, _ends, peers = self._adapter_with_replacement_connection(
            swap_after=1
        )
        self._assert_splits_into_several_chunks(adapter)
        result = adapter.send_result(Outbound(f"irc:{CHAN}", CHUNK_TEXT))

        # 反空转：把两边的字节都读走，确认不是「少发了所以没报错」。
        _read_exactly(peers[0], len(_expected_chunk_bytes(0)))
        _read_exactly(
            peers[1],
            len(_expected_chunk_bytes(1)) + len(_expected_chunk_bytes(2)),
        )

        self.assertTrue(result.ok, "换连接后仍能发完 ⇒ 不该记成失败")
        self.assertFalse(result.partial, "没有分片丢失 ⇒ 不该是 partial")
        self.assertIsNone(adapter.last_send_error,
                          "分片中段换连接不该留下任何发送失败记录")
        self.assertIsNot(result.error_kind, SendError.TRANSIENT,
                         "TRANSIENT 是迁移前的行为：剩余分片写进死连接")
        self.assertIsNotNone(result.handle)
        self.assertEqual(result.handle.message_id, str(CHUNK_COUNT),
                         "句柄必须指向最后一片")
        self.assertEqual(result.handle.conversation_id, f"irc:{CHAN}")

    # -- 3：每一片仍是一个完整独立的 PRIVMSG -----------------------------
    def test_every_chunk_stays_a_standalone_privmsg_across_the_reconnect(self):
        """跨重连不能破坏分段语义：每片一条完整 ``PRIVMSG``，正文一字不丢。"""
        adapter, _first, _ends, peers = self._adapter_with_replacement_connection(
            swap_after=1
        )
        self._assert_splits_into_several_chunks(adapter)
        adapter.send(Outbound(f"irc:{CHAN}", CHUNK_TEXT))
        raw = _read_exactly(peers[0], len(_expected_chunk_bytes(0))) + _read_exactly(
            peers[1],
            len(_expected_chunk_bytes(1)) + len(_expected_chunk_bytes(2)),
        )

        lines = _chunk_lines(raw)
        self.assertEqual(len(lines), CHUNK_COUNT, "分片数不能被换连接改变")
        for index, line in enumerate(lines):
            with self.subTest(chunk=index):
                self.assertTrue(line.startswith(f"PRIVMSG {CHAN} :"),
                                f"第 {index} 片不是一条独立 PRIVMSG：{line[:40]!r}")
                self.assertNotIn("\r", line)
                self.assertNotIn("\n", line)
                self.assertLessEqual(len(line.encode("utf-8")) + 2, LINE_LIMIT,
                                     f"第 {index} 片超出行长上限")
                self.assertEqual(line.split(" :", 1)[1], CHUNK_BODY)
        # 逐字节拼回去必须正好等于原文（既不丢也不重）
        self.assertEqual("".join(x.split(" :", 1)[1] for x in lines), CHUNK_TEXT)

    # -- 4：换连接发生在第 2 片之后也一样（第 1 片不是特例）---------------
    def test_swap_after_the_second_chunk_sends_only_the_last_chunk_over(self):
        adapter, first, _ends, peers = self._adapter_with_replacement_connection(
            swap_after=2
        )
        self._assert_splits_into_several_chunks(adapter)
        adapter.send(Outbound(f"irc:{CHAN}", CHUNK_TEXT))

        self.assertEqual(first.swaps, 1)
        self.assertEqual(first.writes, 2, "旧连接应收到前两片")
        on_new = _read_exactly(peers[1], len(_expected_chunk_bytes(2)))
        self.assertEqual(_chunk_lines(on_new), [f"PRIVMSG {CHAN} :{CHUNK_BODY}"])
        self.assertEqual(
            _read_exactly(peers[0], 2 * len(_expected_chunk_bytes(0))),
            _expected_chunk_bytes(0) + _expected_chunk_bytes(1),
            "前两片必须都留在旧连接上",
        )

    # -- 5：对照 —— 不换连接时全部留在同一条连接 --------------------------
    def test_without_a_reconnect_every_chunk_stays_on_the_first_connection(self):
        """同一条连接走完全部分片（``swap_after`` 永不命中）。

        这条同时是上面几条的**对照**：它证明「两条连接确实被分别记账」，
        所以 #1 里「新连接收到了后半片」不是因为两条连接收到的是同一份字节。
        """
        unreachable = CHUNK_COUNT + 1
        adapter, first, transport_ends, peers = (
            self._adapter_with_replacement_connection(swap_after=unreachable)
        )
        self._assert_splits_into_several_chunks(adapter)
        adapter.send(Outbound(f"irc:{CHAN}", CHUNK_TEXT))

        self.assertEqual(first.swaps, 0, "不该换连接")
        self.assertEqual(first.writes, CHUNK_COUNT)
        self.assertIs(adapter.transport.connection, first,
                      "当前连接应仍是第一条")
        chunk_size = len(_expected_chunk_bytes(0))
        self.assertEqual(
            _read_exactly(peers[0], chunk_size * CHUNK_COUNT),
            _expected_chunk_bytes(0) * CHUNK_COUNT,
            "全部分片都该落在同一条连接上，且逐字节有序",
        )
        self.assertIsNot(adapter.transport.connection, transport_ends[1])

    # -- 6：边界 —— 换到一条写不出去的新连接，仍然如实报 partial ----------
    def test_replacement_connection_that_cannot_write_reports_partial_failure(self):
        """「能发完」的前提是**新连接真的能写**。

        新连接也写不出去时，行为必须回到「TRANSIENT + 部分句柄」，且句柄指向
        **最后一条真正送出去的分片** —— 迁移前后在这一点上本就一致，差异只在
        分片被发到了哪条连接上。
        """
        dead = _FailingWriteConnection()
        adapter, first, _ends, _peers = self._adapter_with_replacement_connection(
            swap_after=1, replacement=dead
        )
        self._assert_splits_into_several_chunks(adapter)
        result = adapter.send_result(Outbound(f"irc:{CHAN}", CHUNK_TEXT))

        self.assertEqual(first.swaps, 1, "第 1 片之后仍然换了连接")
        self.assertEqual(dead.writes, 1, "只该试写一次（不重试、不排队）")
        self.assertTrue(result.partial, "剩余分片没送出去 ⇒ 必须是 partial")
        self.assertEqual(result.error_kind, SendError.TRANSIENT)
        self.assertIsNotNone(result.handle, "部分句柄必须还在")
        self.assertEqual(result.handle.message_id, "1",
                         "句柄指向最后一条**真正送出**的分片")
        self.assertEqual(adapter.last_send_error, SendError.TRANSIENT)


if __name__ == "__main__":
    unittest.main()

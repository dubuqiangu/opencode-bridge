"""WebSocket 长连接传输（基于 :mod:`opencode_bridge.ws` 的客户端）。

覆盖的平台：Slack Socket Mode / Discord Gateway / Mattermost / Twitch（IRC over WSS）。
⚠️ Twitch 是**行**协议（IRC over WS）：``ws.recv()`` 同样可能**一帧多行**、也可能只给
半行，所以它用本类收帧、在**适配器侧**自己按 ``\n`` 切行并维护跨帧缓冲
（见 :meth:`opencode_bridge.adapters.twitch.TwitchAdapter._on_frame`）。
⚠️ 也就是说本类**不做**行缓冲 —— 需要行的协议要么像 Twitch 那样在 ``on_message``
钩子里自己切，要么改用 :mod:`.tcp_lines`（它内建行缓冲）。

注入点
------
``connect`` 是一个**返回已连接对象**的可调用，所以测试可以塞假 WS，不必真联网::

    transport = WebSocketTransport(lambda: ws_connect("wss://…"), name="slack")
    transport.start(on_frame)

生产里通常写 ``lambda: ws_connect(url, timeout=30)``；本模块刻意不 import
``opencode_bridge.ws``（传输层不该挑具体的客户端实现，测试也不该为它装真 TLS）。

协议细节的处理
--------------
* ``recv()`` 返回 ``None`` = 对端关闭（:mod:`opencode_bridge.ws` 的约定）
  → 抛异常让基类按"会话结束"处理并重连。**状态码直接读** ``conn.close_code``
  / ``close_reason``（该客户端已经记录了），不要自己猜。
* ``on_message`` 在**每条原始帧**到达时被调用（发 ack、回心跳）；它抛
  ``ReconnectNow`` → **立即重连且不退避**（Slack 服务端下发 ``disconnect``
  要求换连接的正解）；抛别的异常只记 log，连接继续。
* ``_close_conn`` 做成"先 ``shutdown`` 再 ``close()"：阻塞在 ``recv()`` 里时
  只有 ``shutdown`` 能立刻唤醒它，否则 ``stop()`` 每次都要等满 WS 读超时。

⚠️ 周期钩子在这里只能走**定时驱动**
----------------------------------------
``recv()`` 会**阻塞到真有帧到达或连接关闭**，所以"每次取下一条之前调钩子"（循环
驱动）在 WS 上等于"每次有帧时调"—— 完全无法做保活/心跳。

而且**不能靠调小 WS 读超时来换周期**：:mod:`opencode_bridge.ws` 的
``_recv_exact`` 在读超时时会抛 ``WebSocketError`` 并**丢掉已读到的半帧字节**，
把 socket 超时调小等于让流损坏。所以 Discord 的心跳必须用
``tick_interval > 0`` 的**定时驱动**模式（另起一个 daemon 线程），与
:mod:`.base` 模块 docstring「周期钩子」一节一致。
"""

from __future__ import annotations

import logging
import socket
from typing import Any, Callable

from .base import NOTHING, ReconnectNow, Transport

__all__ = ["WebSocketTransport", "ReconnectNow", "NOTHING"]

logger = logging.getLogger("opencode_bridge.transport.websocket")


class WebSocketTransport(Transport):
    """基于 :mod:`opencode_bridge.ws` 的长连接传输。

    :param connect: 返回**已连接**的 WS 客户端的可调用（测试注入点）。
    :param on_open: 可选，``on_open(conn)`` 在连上之后、消费第一帧之前被调一次
        （发登录 / Identify / CAP 协商 / IRC 注册）。抛异常 = 本次会话作废
        （"连上但注册不通过"就该重连，而不是卡在那里）—— 基类会关连接、退避重试。
        与 :class:`~opencode_bridge.transport.tcp_lines.TcpLineTransport` 的
        ``on_connect`` 是同一件事，只是本类按 WS 适配器的习惯叫 ``on_open``。
    :param on_message: 可选，``on_message(conn, frame)`` 在每条原始帧到达时
        调用（ack / 应答心跳 / 解析前置动作）。
    :param on_close: 可选，``on_close(conn)`` 在**每次会话结束时**调一次（掉线、
        注册失败、以及 ``stop()`` 关掉连接之后都会调；``_open()`` 直接失败、
        从未建上过连接时**不**调）。适配器用它做"会话收尾"：Mattermost 打断开
        诊断、Twitch 取消注册定时器并清掉半行缓冲。
        在这里抛 :class:`~opencode_bridge.transport.ReconnectNow` 可以要求**立刻**
        重连、不退避（基类 :meth:`Transport._safe_on_close` 会识别）。
    :param close_code: **本端主动关闭**时使用的状态码（默认 1000）。收到对端
        的 close 帧时用客户端自带的 ``close_code`` 属性，不受此参数影响。
        ⚠️ 平台对"我们主动断开"有要求的（Discord 必须用 4000，否则 session 失效），
        那就在这里传过去 —— 见 :mod:`opencode_bridge.adapters.discord`。
    """

    def __init__(
        self,
        connect: Callable[[], Any],
        *,
        on_open: Callable[[Any], None] | None = None,
        on_message: Callable[[Any, Any], None] | None = None,
        on_close: Callable[[Any], None] | None = None,
        close_code: int = 1000,
        **kw: Any,
    ) -> None:
        if not callable(connect):
            raise TypeError("connect 必须可调用")
        if on_open is not None and not callable(on_open):
            raise TypeError("on_open 必须可调用")
        if on_message is not None and not callable(on_message):
            raise TypeError("on_message 必须可调用")
        if on_close is not None and not callable(on_close):
            raise TypeError("on_close 必须可调用")
        self._connect = connect
        self._on_open_fn = on_open
        self._on_message = on_message
        self._on_close_fn = on_close
        self._close_code = int(close_code)
        super().__init__(**kw)

    def _open(self) -> Any:
        return self._connect()

    def _on_open(self, conn: Any) -> None:
        """转接 ``on_open`` 钩子（默认 no-op，与基类契约一致：抛异常 = 会话作废）。"""
        if self._on_open_fn is not None:
            self._on_open_fn(conn)

    def _on_close(self, conn: Any) -> None:
        """转接 ``on_close`` 钩子（默认 no-op，与基类契约一致：不许抛）。"""
        if self._on_close_fn is not None:
            self._on_close_fn(conn)

    def _next(self, conn: Any) -> Any:
        """收一帧。``None``（对端关闭）→ 视为会话结束，交给基类重连。"""
        frame = conn.recv()
        if frame is None:
            raise ConnectionError(self._closed_detail(conn))
        if self._on_message is not None:
            try:
                self._on_message(conn, frame)
            except ReconnectNow:
                raise                 # 立即重连（不等退避）
            except Exception as exc:  # noqa: BLE001 - 钩子出错不该断连接
                logger.warning(
                    "transport[%s]: on_message 抛出异常（已忽略）: %s",
                    self.label, exc,
                )
        return frame

    def _closed_detail(self, conn: Any) -> str:
        """用客户端**自带**的 ``close_code`` / ``close_reason`` 描述断开原因。"""
        code = getattr(conn, "close_code", None)
        reason = getattr(conn, "close_reason", "") or ""
        if code is None:
            return "对端关闭了 WebSocket（无 close 帧 / close_code=None）"
        return f"对端关闭了 WebSocket（close_code={code} reason={reason!r}）"

    def _close_conn(self, conn: Any) -> None:
        """先 ``shutdown`` 唤醒阻塞中的 ``recv()``，再 ``close()``。

        :mod:`opencode_bridge.ws` 没有暴露"带超时的中断读"，所以这里做一次
        **防御性** ``getattr`` 取底层 socket：取不到就只调 ``close()``
        （功能退化，但不会崩）。整个方法**不抛异常**且**幂等**。
        """
        sock = getattr(conn, "_sock", None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:  # noqa: BLE001 - 关闭路径不许抛
                pass
        try:
            conn.close(self._close_code)
        except TypeError:      # 测试替身 / 其他实现的 close() 可能不收参数
            try:
                conn.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("transport[%s]: ws close 失败（忽略）: %s",
                             self.label, exc)
        except Exception as exc:  # noqa: BLE001
            logger.debug("transport[%s]: ws close 失败（忽略）: %s", self.label, exc)
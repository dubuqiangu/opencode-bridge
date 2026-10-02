"""TCP 行协议传输（IRC / 任何 ``\\r\\n`` 分行的 TCP 服务，可选 TLS）。

覆盖的平台：**IRC**（:mod:`opencode_bridge.adapters.irc`）；Twitch 的 IRC
over WSS 若要迁过来，只需把 :meth:`TcpLineTransport._open` 换成
``ws_connect(...)`` 并沿用本文件的行缓冲逻辑（见类 docstring 的说明）。

本类负责的"难但无聊"的部分
--------------------------
1. **行缓冲跨 ``recv()`` 存活**：一次 ``recv`` 可能带来半行，也可能一次带来
   七行（IRC 服务器注册时会一口气发 001/375/372/376/422/005…）。按 ``\\n``
   切、切完把剩下的写回缓冲，**只返回第一条完整行**，剩下的留给下一次
   ``_next``（基类会立刻再调一次）。没凑出整行时返回 :data:`NOTHING`。
2. **坏字节不能崩**：解码一律 ``errors="replace"``，对端（或中间人）发来非法
   UTF-8 时替换成 U+FFFD 而不是抛异常把连接干掉。
3. **超长行防护**：缓冲超过类属性 :attr:`max_line_bytes` 就丢弃并告警，
   防止对端（或恶意客户端）不发换行把内存吃光。
4. **读超时不等于掉线**：``recv`` 抛 ``TimeoutError`` 只是"这
   ``io_timeout`` 内没数据"，返回 ``NOTHING`` 让循环再去读。**断线靠
   ``recv`` 返回空字节（对端 FIN）判定**。
5. **关闭要唤醒读**：:meth:`_close_conn` 先 ``shutdown`` 再 ``close``，
   否则阻塞中的 ``recv`` 要等满 ``io_timeout``，``stop()`` 就会白等。

适配器要做的：``on_connect`` 里发 ``NICK`` / ``USER`` / ``CAP`` / ``PASS``，
收到每条**完整行**（不含行尾）后做平台解析。
"""

from __future__ import annotations

import logging
import socket
import ssl
import threading
from typing import Any, Callable

from .base import NOTHING, Transport

__all__ = ["TcpLineTransport"]

logger = logging.getLogger("opencode_bridge.transport.tcp_lines")


class TcpLineTransport(Transport):
    """TCP 行协议传输（``\\r\\n`` 分行，可选 TLS）。

    :param host: 主机名或 IP。
    :param port: 端口。
    :param tls: 是否用 :func:`ssl.create_default_context` 升级（默认校验证书
        与主机名；不给"跳过校验"的开关）。
    :param on_connect: 可选，``on_connect(sock)`` 在每次连上后被调用一次，
        用于发 ``NICK`` / ``USER`` / ``CAP`` / ``PASS``。抛异常 = 本次连接
        作废，基类退避重连（"连上但注册不通过"就该重连，而不是卡住）。
    :param connect_timeout: 建连超时（秒）。
    :param io_timeout: 单次 ``recv`` 超时（秒）。它是循环的唯一"心跳"：到期
        返回 :data:`NOTHING` 重新判断停止位，所以别设太大（``stop()`` 的响应
        速度受它影响 —— 其实 :meth:`_close_conn` 的 ``shutdown`` 能唤醒读，
        这里只是兜底），也别设太小（白烧 CPU）。
    """

    #: 入站单行字节上限（类级旋钮，测试/适配器可覆盖）。只防"对端不发换行把
    #: 内存吃光"，远超任何正常行（IRC 上限 512，Twitch 给的也只有几 KB）。
    max_line_bytes: int = 64 * 1024

    def __init__(
        self,
        host: str,
        port: int,
        *,
        tls: bool = False,
        on_connect: Callable[[Any], None] | None = None,
        connect_timeout: float = 15.0,
        io_timeout: float = 1.0,
        **kw: Any,
    ) -> None:
        if on_connect is not None and not callable(on_connect):
            raise TypeError("on_connect 必须可调用")
        self.host = str(host)
        self.port = int(port)
        self._tls = bool(tls)
        self._on_connect = on_connect
        self._connect_timeout = float(connect_timeout)
        self._io_timeout = max(0.01, float(io_timeout))
        self._recv_size = 4096
        #: 行缓冲：跨 ``_next`` 存活（半行说明上次 recv 没读完）。
        self._rbuf = bytearray()
        self._send_lock = threading.Lock()
        super().__init__(**kw)

    # --- 连接 ----------------------------------------------------------
    def _open(self) -> Any:
        """建连（+ 可选 TLS）。异常一律抛给基类退避重试。"""
        sock = socket.create_connection(
            (self.host, self.port), timeout=self._connect_timeout
        )
        try:
            if self._tls:
                sock = self._wrap_tls(sock)
            sock.settimeout(self._io_timeout)
        except Exception:
            try:
                sock.close()
            except Exception:  # noqa: BLE001
                pass
            raise
        # 重连后残留的半行必须丢弃：它属于**上一条**连接，拼接会错位。
        self._rbuf.clear()
        return sock

    def _wrap_tls(self, sock: socket.socket) -> Any:
        """默认 TLS 包装（校验证书链与主机名）。

        单独拆成方法是为了给测试/自建网关留注入口；真 TLS 需要证书，测试里
        覆盖成 no-op 即可。
        """
        context = ssl.create_default_context()
        return context.wrap_socket(sock, server_hostname=self.host)

    def _on_open(self, conn: Any) -> None:
        if self._on_connect is not None:
            self._on_connect(conn)

    def _close_conn(self, conn: Any) -> None:
        """先 ``shutdown`` 唤醒阻塞中的 ``recv``，再 ``close``（幂等、不抛）。"""
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except Exception:  # noqa: BLE001 - 关闭路径不许抛
            pass
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    # --- 读 ------------------------------------------------------------
    def _next(self, conn: Any) -> Any:
        """取出**第一条完整行**；暂时没有就返回 :data:`NOTHING`。

        顺序很重要：**先看缓冲、再 recv**。反过来的话，一次 ``recv`` 带来的
        后几行会被压在缓冲里，而消费循环还在等下一块网络数据（IRC 注册时
        服务器一口气发 001/375/372/376/005… 就会这样被拖住）。

        - ``recv`` 超时 / 只有半行 → ``NOTHING``（不是掉线）
        - ``recv`` 返回 ``b""`` → 对端 FIN，抛异常让基类重连
        """
        line = self._take_line()
        if line is not None:
            return line
        try:
            chunk = conn.recv(self._recv_size)
        except TimeoutError:
            return NOTHING
        except ssl.SSLWantReadError:  # 非阻塞 TLS 的"暂时没数据"
            return NOTHING
        if not chunk:
            raise ConnectionError("对端关闭了 TCP 连接")
        self._rbuf += chunk
        if len(self._rbuf) > self.max_line_bytes and b"\n" not in self._rbuf:
            logger.warning(
                "transport[%s]: 入站行超长（%d 字节且没有换行），已丢弃缓冲",
                self.label, len(self._rbuf),
            )
            self._rbuf.clear()
            return NOTHING
        line = self._take_line()
        return line if line is not None else NOTHING

    def _take_line(self) -> str | None:
        """从缓冲切出第一条完整行（去掉行尾 ``\\r``）；没有就 ``None``。"""
        index = self._rbuf.find(b"\n")
        if index < 0:
            return None
        raw = bytes(self._rbuf[:index])
        del self._rbuf[: index + 1]
        # ``\r\n`` / ``\n`` 都吃；解码用 replace：坏字节不能崩掉连接。
        return raw.rstrip(b"\r").decode("utf-8", "replace")

    # --- 发 ------------------------------------------------------------
    def send_line(self, line: str) -> bool:
        """发一行命令（自动补 ``\\r\\n``，返回是否成功）。

        行内**不允许**出现 CR/LF —— 会话类协议（IRC / SMTP / IMAP）里那是命令
        注入：一条恶意消息正文能顺手发出一条新命令。这里直接替换成空格
        （宁可发出一条语义不对的消息，也不要让对端多收一条命令）。

        语义是"**尽力**"：连不上时返回 ``False`` 并记 warning，不排队重发
        （重发是业务决定 —— 队列、去重、合并都由适配器负责）。
        """
        conn = self.connection
        if conn is None:
            logger.warning(
                "transport[%s]: 未连接，丢弃命令 %r", self.label, str(line)[:60]
            )
            return False
        text = str(line).replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
        payload = (text + "\r\n").encode("utf-8")
        try:
            with self._send_lock:
                conn.sendall(payload)
        except Exception as exc:  # noqa: BLE001 - 发送失败不该崩调用方
            logger.warning("transport[%s]: 发送失败: %s", self.label, exc)
            return False
        return True
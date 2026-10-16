"""Which transport failures **prove** the request never reached the server.

``OpenCodeClient._request`` 把三种传输异常（``URLError`` / ``OSError`` /
``HTTPException``）压成同一个 ``OpenCodeError(status=None)`` —— 而
:mod:`opencode_bridge.inbound_gateway` 要靠**这一档里的差异**决定盘上落哪个状态：

* **可证明从未提交** ⇒ 可安全重放（agent 一定没跑过）；
* **已发出但响应读中断** ⇒ 结果未知、绝不重放
  （AGENTS.md §8 第 3 条：恢复一份没记录过的信息 = 猜）。

本模块只回答一个问题：**这次传输失败能不能证明请求压根没离开本机？**

判据 = 异常链里**出现** ``ConnectionRefusedError`` 或 ``socket.gaierror``。
二者只可能产生在**建连 / DNS 阶段** —— 那一刻 TCP 还没握上手、HTTP 请求字节
一个都没发出去 ⇒ 「从未提交」是**可证的**，不是猜的。

⛔ 其余一律回 ``False``（fail-safe）：``TimeoutError`` / ``socket.timeout`` 可能
发生在**读过响应之后**；``ConnectionResetError`` /
``http.client.RemoteDisconnected`` / ``IncompleteRead`` / ``ssl.SSLError``
都意味着**字节已经发出去了** ⇒ 服务端可能已经跑过 ⇒ 按「可能已经跑过」处理。
**缺省方向必须保守。**

Provenance: 2026-10-16 dsh-im-connect 借鉴面 A2（投递三档语义）。判据形状由回环
真 socket 实测取证（:mod:`tests.test_request_phase`），⛔ 不是推测出来的。
"""

from __future__ import annotations

import socket
import urllib.error
from typing import Iterator

__all__ = ["request_definitely_never_sent"]

#: 只有这两种异常形状能**证明**请求没离开本机 —— 见模块 docstring 的判据段。
#: ⚠️ 精确类型即可，⛔ 别改成 ``OSError``：那会把 ``ConnectionResetError``
#: （「已发出」那一侧最常见的一种）一并吞进「从未提交」。
_NEVER_LEFT_THE_HOST = (ConnectionRefusedError, socket.gaierror)


def _the_exception_chain(exception: BaseException) -> Iterator[BaseException]:
    """这条异常**整条链**：``__cause__`` / ``__context__``，外加 ``URLError.reason``。

    三个来源缺一不可：``_request`` 用 ``raise ... from exc`` 挂 ``__cause__``；
    ``urllib`` 把 ``socket`` 的原始异常塞进 ``URLError.reason``（DNS 失败与
    连接被拒**都**藏在这里，而 ``URLError`` 自己的类型说明不了是哪一种）；
    ``__context__`` 兜住隐式串起来的那些。

    递归用显式栈 + ``id`` 去重：异常链可以自我回环（``__context__`` 与
    ``__cause__`` 指向同一个对象），而无限递归在这里是**崩溃**，不是报错。
    """
    pending: list[BaseException] = [exception]
    visited: set[int] = set()
    while pending:
        node = pending.pop()
        if id(node) in visited:
            continue
        visited.add(id(node))
        yield node
        if isinstance(node, urllib.error.URLError):
            raw_reason = node.reason
            if isinstance(raw_reason, BaseException):
                pending.append(raw_reason)
        if node.__cause__ is not None:
            pending.append(node.__cause__)
        if node.__context__ is not None:
            pending.append(node.__context__)


def request_definitely_never_sent(exception: BaseException) -> bool:
    """这次传输失败能不能**证明**请求压根没离开本机？

    ``True`` ⇒ 可安全重放（投递侧因此落 ``failed``，退避阶梯照常管它）；
    ``False`` ⇒ 缺省保守档，按「可能已经跑过」处理。
    """
    for node in _the_exception_chain(exception):
        if isinstance(node, _NEVER_LEFT_THE_HOST):
            return True
    return False

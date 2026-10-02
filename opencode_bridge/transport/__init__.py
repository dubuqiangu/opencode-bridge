"""传输层：连接 / 退避重连 / 线程 / 停止（**零业务语义、零第三方依赖**）。

八个平台适配器当初各写了一遍"连接 → 收包 → 退避重连 → 线程 → 停止"的循环，
并且都踩过同一批坑（尤其"``stop()`` 必须先关连接再 join，否则每次都白等满
超时"）。本包把这段与业务无关的样板收敛掉：

============================  ==========================================
:class:`~.polling.PollingTransport`   HTTP 轮询（短轮询 / 长轮询）
                                 —— Telegram ``getUpdates``、Matrix ``/sync``、
                                 Nextcloud 长轮询
:class:`~.websocket.WebSocketTransport`  WS 长连接（Slack / Discord / Mattermost）
:class:`~.tcp_lines.TcpLineTransport`    TCP 行协议（IRC）
============================  ==========================================

分层规矩：**传输层不认识消息平台**。授权闸门、防回环、系统消息过滤、字段映射
全部归适配器 —— 所以本包不 import ``opencode_bridge.adapters``（既避免循环依赖，
也让传输层可以独立测试）。

最小例子::

    from opencode_bridge.transport import PollingTransport, NOTHING

    def fetch():
        result = http_get("/api/updates")
        return result if result else NOTHING

    transport = PollingTransport(fetch, idle_sleep=1.0, name="telegram")
    transport.start(on_event=handle_raw_update)   # handle_raw_update 只做解析
    ...
    transport.stop()                              # 先关连接，再 join 线程
"""

from __future__ import annotations

from .base import NOTHING, ReconnectNow, Transport
from .polling import PollingTransport
from .queue import EventQueue
from .tcp_lines import TcpLineTransport
from .websocket import WebSocketTransport

__all__ = [
    "Transport",
    "PollingTransport",
    "WebSocketTransport",
    "TcpLineTransport",
    "EventQueue",
    "NOTHING",
    "ReconnectNow",
]
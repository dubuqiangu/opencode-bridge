"""A3 单端口共享入站 server 的归属者（拓扑拍板：2026-10-10）。

为什么要有这个模块
------------------
生产侧此前唯一自持 HTTP server 的是 a2a 适配器 —— 但那是**被否掉的模板**，
不是迁移对象：webhook 类平台（Telegram webhook、Twilio、Slack Events …）的
入站拓扑按用户拍板走**单端口共享 server**，路由按路径区分
（参考 hermes 的 ``shared_ingress.py``，快照
``nousresearch-hermes-agent-8a5edab282632443.txt``）。

职责切分（与 :mod:`opencode_bridge.httpsrv` 的既定分工一致）：

* **本模块**：唯一持有 :class:`~opencode_bridge.httpsrv.HttpServer` 生命周期的
  地方 —— 收集路由、防冲突、绑端口、优雅关闭。它不认识任何消息平台
  （刻意不在运行期 import 任何适配器模块）。
* **适配器**（契约见 :meth:`opencode_bridge.adapters.base.Adapter.webhook_routes`）：
  **只交 routes、不持 server**；验签在各自 handler 内做
  （``require_auth=False`` + handler 自验，纪律对齐 hermes sms 的 fail-closed，
  见 ``docs/platform-design-reference.md`` Part 11 结论 3）。

三条不可让步的纪律
------------------
1. **零路由 ⇒ 不绑端口**。没有任何适配器贡献 routes 时不起 server，
   一个端口都不占 —— 现有 13 个适配器的部署行为零变化。
2. **路由冲突 ⇒ 整体拒绝（all-or-nothing）**。两个适配器抢同一个
   ``(path, method)`` 是配置/程序错误，本模块记 ERROR 并拒绝启动整个 hub，
   绝不"挂一半"—— 部分可用比全不可用更难排障（缺的那条路径表现为 404，
   与"没配"同形）。
3. **start() 不抛异常**（对齐适配器约定：启动失败只记日志）。绑定失败
   :class:`~opencode_bridge.httpsrv.BindError` 记 ERROR 并返回 ``None``。

配置（``config.json`` 的 ``bridge`` 段，键由 :mod:`opencode_bridge.config`
载入时校验）::

    {
      "webhook_port": 0,             // 0 = 操作系统分配（本地/测试够用）
      "webhook_host": "127.0.0.1"    // 默认回环；换地址是显式配置行为
    }

⚠️ ``webhook_port: 0`` 对真平台 webhook **不够用**（端口每次重启都变，
对端找不到我们 —— 与 a2a 的 ``bind_port`` 必填同一理由）：接真平台时
要配一个稳定端口。⚠️ 绑非回环地址时本模块会另发一条 WARNING —— hub 层
没有统一的平台验签（各 handler 自验），放宽可达性是部署者自己的决定，
本模块只负责把这件事**说出来**。

生命周期接线在 :class:`opencode_bridge.core.BridgeCore`：start 里 hub 先于
适配器（webhook 适配器可能要在自己的 ``start()`` 里把实际端口告诉平台）、
stop 里适配器先停、hub 后停（先唤醒阻塞中的 handler，再等在途请求收尾）。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Optional

from .httpsrv import DEFAULT_BIND_HOST, BindError, HttpServer, Route

if TYPE_CHECKING:  # pragma: no cover - 仅为类型标注；运行期不 import 适配器
    from .adapters.base import Adapter

logger = logging.getLogger("opencode_bridge.webhook_hub")

__all__ = ["WebhookHub"]

#: ``bridge.webhook_port`` 的默认值：``0`` = 由操作系统分配。
#: ⚠️ 只对本地/测试成立，见模块 docstring。
DEFAULT_WEBHOOK_PORT = 0

#: TCP 端口合法上限。
MAX_WEBHOOK_PORT = 65535

WEBHOOK_PORT_KEY = "webhook_port"
WEBHOOK_HOST_KEY = "webhook_host"


class WebhookHub:
    """共享 webhook server 的归属者。职责切分见模块 docstring。"""

    def __init__(self, bridge_config: Mapping[str, Any] | None = None) -> None:
        bridge = dict(bridge_config or {})
        self._host = self._coerce_host(bridge.get(WEBHOOK_HOST_KEY))
        self._port = self._coerce_port(bridge.get(WEBHOOK_PORT_KEY))
        self._server: Optional[HttpServer] = None
        self._route_count = 0

    # --- 配置读取（告警点名键名：键名只有这里知道）------------------------
    @staticmethod
    def _coerce_host(raw_host: Any) -> str:
        """``bridge.webhook_host`` → 绑定地址；非法值回落回环默认并点名告警。"""
        if raw_host is None:
            return DEFAULT_BIND_HOST
        host = str(raw_host).strip()
        if not host:
            logger.warning(
                "bridge.%s 为空，回落为 %s（回环默认）",
                WEBHOOK_HOST_KEY, DEFAULT_BIND_HOST,
            )
            return DEFAULT_BIND_HOST
        return host

    @staticmethod
    def _coerce_port(raw_port: Any) -> int:
        """``bridge.webhook_port`` → 绑定端口；非法值回落 0（OS 分配）并点名告警。"""
        if raw_port is None:
            return DEFAULT_WEBHOOK_PORT
        try:
            port = int(raw_port)
        except (TypeError, ValueError):
            logger.warning(
                "bridge.%s=%r 不是整数，回落为 %d（操作系统分配）",
                WEBHOOK_PORT_KEY, raw_port, DEFAULT_WEBHOOK_PORT,
            )
            return DEFAULT_WEBHOOK_PORT
        if port < 0 or port > MAX_WEBHOOK_PORT:
            logger.warning(
                "bridge.%s=%r 超出 0~%d，回落为 %d（操作系统分配）",
                WEBHOOK_PORT_KEY, raw_port, MAX_WEBHOOK_PORT, DEFAULT_WEBHOOK_PORT,
            )
            return DEFAULT_WEBHOOK_PORT
        return port

    # --- 生命周期 ------------------------------------------------------
    def start(self, adapters: "Iterable[Adapter]") -> Optional[int]:
        """收集路由并起 server；返回实际端口，未绑定（含各失败路）返回 ``None``。

        幂等（已在跑则直接返回当前端口）；不抛异常（模块 docstring 纪律 3）。
        """
        if self._server is not None:
            return self._server.port
        collected: list[tuple[str, Route]] = []
        for adapter in adapters:
            owner_name = adapter.name or type(adapter).__name__
            for route in tuple(adapter.webhook_routes()):
                collected.append((owner_name, route))
        if not collected:
            # 纪律 1：零路由 ⇒ 不绑端口（现有 13 个适配器一个不受影响）。
            logger.info("webhook hub: 没有适配器贡献路由，不绑定端口")
            return None
        # 纪律 2：路由冲突 ⇒ 整体拒绝。先自检（能点名双方），再挂载。
        claimed: dict[tuple[str, str], str] = {}
        for owner_name, route in collected:
            for method in route.methods:
                route_key = (route.path, method)
                if route_key in claimed:
                    logger.error(
                        "webhook hub: 拒绝启动 —— 路由冲突 %s %s"
                        "（适配器 %r 与 %r 都声明了它）",
                        method, route.path, claimed[route_key], owner_name,
                    )
                    return None
                claimed[route_key] = owner_name
        server = HttpServer(host=self._host, port=self._port, name="webhook")
        for _, route in collected:
            server.add_route(route)
        try:
            port = server.start()
        except BindError as exc:
            logger.error("webhook hub: %s", exc)
            return None
        self._server = server
        self._route_count = len(collected)
        if not server.loopback_only:
            # ⚠️ hub 层没有统一验签（各 handler 自验）—— 放宽可达性是部署者
            # 的显式决定，本模块只负责把它说出来。
            logger.warning(
                "webhook hub 绑定在非回环地址 %s:%d —— 每条路由的 handler"
                "必须自己验平台签名（hub 层无统一鉴权）",
                server.host, port,
            )
        logger.info(
            "webhook hub: %d 条路由监听 http://%s:%d",
            len(collected), server.host, port,
        )
        return port

    def stop(self) -> None:
        """关停 server（幂等）。等在途 handler 收尾由 :meth:`HttpServer.stop` 负责。"""
        server = self._server
        if server is None:
            return
        self._server = None
        try:
            server.stop()
        except Exception:  # noqa: BLE001 - 停止不许把异常抛给关停序列
            logger.exception("webhook hub: 停止失败")

    # --- 状态（可观测）--------------------------------------------------
    @property
    def bound(self) -> bool:
        server = self._server
        return server is not None and server.bound

    @property
    def host(self) -> str:
        """实际绑定的地址（未绑定时是请求的那个）。"""
        server = self._server
        return server.host if server is not None else self._host

    @property
    def port(self) -> int:
        """实际绑定的端口（未绑定时是请求的那个）。"""
        server = self._server
        return server.port if server is not None else self._port

    @property
    def route_count(self) -> int:
        return self._route_count

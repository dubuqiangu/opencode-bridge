"""出站代理发现（纯标准库）：把环境变量里的代理配置翻译成 ``(host, port)``。

本模块是一个**叶子** —— 只 import ``urllib.request`` 与 ``urllib.parse``，
既不碰 socket / ``http.client``，也不 import 本仓库任何其它模块。三条走裸
socket / 裸 ``http.client`` 的出站缝（:mod:`opencode_bridge.adapters.telegram_http`、
:mod:`opencode_bridge.ws`、:mod:`opencode_bridge.transport.tcp_lines`）都从这里
取代理端点，语义与仓库里那批走 ``urllib`` 的适配器保持一致。

为什么用 ``urllib`` 而不是自己读 ``os.environ``
------------------------------------------------
``HTTP_PROXY`` / ``HTTPS_PROXY`` / ``ALL_PROXY`` / ``no_proxy`` 的匹配规则很绕
（大小写、逗号列表、前导点、通配符、CIDR、端口……），``urllib`` 已经实现并维护它。
自己再写一份必然与标准库分叉 —— 仓库纪律明确要求 ⛔ 不手搓 ``no_proxy``。
"""

from __future__ import annotations

import urllib.parse
import urllib.request

__all__ = ["proxy_endpoint_for"]


def proxy_endpoint_for(host: str, *, scheme: str = "https") -> tuple[str, int] | None:
    """返回访问 ``host`` 时应经的代理 ``(proxy_host, proxy_port)``；无需代理则 ``None``。

    ``None`` 的两种情形：

    * 环境里没有为该 ``scheme`` 配置代理（``getproxies()`` 里取不到）；
    * 该 ``host`` 命中 ``no_proxy``（交给 :func:`urllib.request.proxy_bypass` 判定）。

    ``scheme`` 用**小写协议名**（``"https"`` / ``"http"``）。代理端点从
    ``getproxies()`` 里先取 ``<scheme>`` 键、再退回 ``all`` 键（即 ``ALL_PROXY``）——
    ``ALL_PROXY`` 的语义就是「所有协议共用的代理」，取不到具体协议的键时用它。
    ⛔ 不手搓 ``no_proxy``：命中判定一律委托 :func:`urllib.request.proxy_bypass`。

    ⚠️ 代理 URL 里若带 ``user:password@`` 用户信息，本函数**只取主机与端口**、
    不返回凭据（调用方按各自协议自行决定是否发 ``Proxy-Authorization``）。
    """
    proxies = urllib.request.getproxies()
    proxy_url = _proxy_url_for_scheme(proxies, scheme)
    if not proxy_url:
        return None
    if urllib.request.proxy_bypass(host):
        return None
    endpoint = _parse_proxy_url(proxy_url)
    return endpoint


def _proxy_url_for_scheme(proxies: dict[str, str], scheme: str) -> str | None:
    """按协议名从 ``getproxies()`` 的字典里取代理 URL（退回到 ``all``）。"""
    for key in (scheme.lower(), "all"):
        value = proxies.get(key)
        if value:
            return value
    return None


def _parse_proxy_url(proxy_url: str) -> tuple[str, int] | None:
    """把 ``http://host:port`` / ``host:port`` 解析成 ``(host, port)``。

    缺少主机名或端口非法时返回 ``None``（当成「没有可用代理」，让调用方走直连，
    而不是拿一个半截端点去建连）。端口缺省按代理 URL 自身的协议补
    （``https`` 代理补 443，其余补 80）。
    """
    candidate = proxy_url if "://" in proxy_url else f"http://{proxy_url}"
    try:
        parsed = urllib.parse.urlsplit(candidate)
        proxy_host = parsed.hostname
        proxy_port = parsed.port
    except ValueError:
        return None
    if not proxy_host:
        return None
    if proxy_port is None:
        proxy_port = 443 if parsed.scheme == "https" else 80
    return proxy_host, proxy_port

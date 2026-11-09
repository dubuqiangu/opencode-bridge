"""出站代理发现（纯标准库）：把环境变量里的代理配置翻译成一个**富端点**。

本模块是一个**叶子** —— 只 import ``base64`` / ``urllib.request`` / ``urllib.parse``，
既不碰 socket / ``http.client``，也不 import 本仓库任何其它模块。三条走裸
socket / 裸 ``http.client`` 的出站缝（:mod:`opencode_bridge.adapters.telegram_http`、
:mod:`opencode_bridge.ws`、:mod:`opencode_bridge.transport.tcp_lines`）都从这里
取代理端点，语义与仓库里那批走 ``urllib`` 的适配器保持一致。

为什么用 ``urllib`` 而不是自己读 ``os.environ``
------------------------------------------------
``HTTP_PROXY`` / ``HTTPS_PROXY`` / ``ALL_PROXY`` / ``no_proxy`` 的匹配规则很绕
（大小写、逗号列表、前导点、通配符、CIDR、端口……），``urllib`` 已经实现并维护它。
自己再写一份必然与标准库分叉 —— 仓库纪律明确要求 ⛔ 不手搓 ``no_proxy``。

返回的不再只是 ``(host, port)``
--------------------------------
调用方必须知道**代理 URL 自身的 scheme**（``https://`` 代理要先对代理本身做 TLS、
再发 ``CONNECT``），以及代理要求鉴权时的 ``Proxy-Authorization`` 头值。这两样原先
被本模块丢弃、由调用方「各自决定」，结果是三条缝对 ``https://`` 代理全线错误、对
需鉴权的代理全线失败（而走 ``urllib`` 的适配器读同一环境变量却能工作）。现在统一
由本模块算出，见 :class:`ProxyEndpoint`。
"""

from __future__ import annotations

import base64
import urllib.parse
import urllib.request
from typing import NamedTuple

__all__ = ["ProxyEndpoint", "proxy_endpoint_for"]


class ProxyEndpoint(NamedTuple):
    """一条出站缝应使用的代理端点（代理 URL 自身 scheme + 预计算的凭据）。

    :param host: 代理主机名或 IP。
    :param port: 代理端口（URL 缺省时按代理自身 scheme 补 443 / 80）。
    :param scheme: **代理 URL 自身**的小写 scheme（``"http"`` / ``"https"`` …）。
        ⚠️ 与 :func:`proxy_endpoint_for` 的 ``scheme`` 参数**不是一回事**：那个参数
        决定**挑哪个代理键**（``<scheme>`` / ``all``），本字段决定**是否对代理本身
        说 TLS**。别把两者混为一谈。
    :param proxy_authorization: 代理 URL 带 ``user:password@`` 时预计算的
        ``Proxy-Authorization`` 头值（``"Basic " + base64(user:password)``，userinfo
        先经 URL-decode）；没有 userinfo 时为 ``None``。
    """

    host: str
    port: int
    scheme: str
    proxy_authorization: str | None


def proxy_endpoint_for(host: str, *, scheme: str = "https") -> ProxyEndpoint | None:
    """返回访问 ``host`` 时应经的代理端点；无需代理则 ``None``。

    ``None`` 的两种情形：

    * 环境里没有为该 ``scheme`` 配置代理（``getproxies()`` 里取不到）；
    * 该 ``host`` 命中 ``no_proxy``（交给 :func:`urllib.request.proxy_bypass` 判定）。

    ``scheme`` 用**小写协议名**（``"https"`` / ``"http"``），它只用来**挑代理键**：
    先取 ``getproxies()`` 里的 ``<scheme>`` 键、再退回 ``all`` 键（即 ``ALL_PROXY``）
    —— ``ALL_PROXY`` 的语义就是「所有协议共用的代理」。⛔ 不手搓 ``no_proxy``：
    命中判定一律委托 :func:`urllib.request.proxy_bypass`。

    ⚠️ 代理 URL 里若带 ``user:password@`` 用户信息，凭据由本模块**预计算**成
    :attr:`ProxyEndpoint.proxy_authorization`（与 ``urllib`` 的 ``Basic`` 编码一致：
    userinfo 先 URL-decode 再 base64）。调用方只负责把它写进各自的
    ``Proxy-Authorization`` 头 / ``CONNECT`` 请求。
    """
    proxies = urllib.request.getproxies()
    proxy_url = _proxy_url_for_scheme(proxies, scheme)
    if not proxy_url:
        return None
    if urllib.request.proxy_bypass(host):
        return None
    return _parse_proxy_url(proxy_url)


def _proxy_url_for_scheme(proxies: dict[str, str], scheme: str) -> str | None:
    """按协议名从 ``getproxies()`` 的字典里取代理 URL（退回到 ``all``）。"""
    for key in (scheme.lower(), "all"):
        value = proxies.get(key)
        if value:
            return value
    return None


def _parse_proxy_url(proxy_url: str) -> ProxyEndpoint | None:
    """把 ``http://[user:pass@]host:port`` / ``host:port`` 解析成 :class:`ProxyEndpoint`。

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
    proxy_scheme = parsed.scheme.lower()
    if proxy_port is None:
        proxy_port = 443 if proxy_scheme == "https" else 80
    return ProxyEndpoint(
        host=proxy_host,
        port=proxy_port,
        scheme=proxy_scheme,
        proxy_authorization=_basic_authorization(parsed),
    )


def _basic_authorization(parsed: urllib.parse.SplitResult) -> str | None:
    """把代理 URL 的 userinfo 预计算成 ``Basic`` 头值；没有 userinfo 则 ``None``。

    与 ``urllib`` 一致：userinfo 先 :func:`urllib.parse.unquote`（它可能含 ``%40``
    这类转义），再 ``base64("user:password")``。只给用户名、没给密码时密码按空串处理。
    """
    username = parsed.username
    if username is None:
        return None
    password = parsed.password or ""
    user_pass = f"{urllib.parse.unquote(username)}:{urllib.parse.unquote(password)}"
    encoded = base64.b64encode(user_pass.encode("utf-8")).decode("ascii")
    return f"Basic {encoded}"

"""测试用的代理环境隔离：让测试套件与开发机的 ``*_PROXY`` 设置解耦。

背景
----
生产代码现在会读 ``HTTP_PROXY`` / ``HTTPS_PROXY`` / ``ALL_PROXY``（见
:mod:`opencode_bridge.proxy_config`）：三条走裸 socket / 裸 ``http.client`` 的
出站缝在检测到代理时会改走代理 CONNECT 隧道。

若测试进程**继承**开发机的代理设置，那么所有依赖真实 socket 的用例
（``127.0.0.1`` 上的 loopback IRC / WS 服务器、以及 ``TestIRCTLS`` 里把
``create_connection`` 打桩的直连断言）都会**改走代理**，于是它们量到的是
「开发机的环境」而不是被测行为 —— 这正是把测试变成**非封闭**的。

做法
----
:mod:`tests` 在导入时调用 :func:`clear_proxy_environment`，把整套测试的起点固定成
「无代理」。需要验证代理路径的用例（``tests/test_proxy_support.py``）在 ``setUp``
里自己保存并设置环境变量、``tearDown`` 里还原 —— 于是「无代理」与「有代理」两个
分支都由用例**显式**决定，不再由运行环境决定。
"""

from __future__ import annotations

import os

#: 代理环境变量统一以 ``_proxy`` 结尾（``http_proxy`` / ``HTTPS_PROXY`` /
#: ``ALL_PROXY`` / ``no_proxy`` / ``ftp_proxy`` …），大小写都要认。
_PROXY_SUFFIX = "_proxy"


def proxy_environment() -> dict[str, str]:
    """返回当前进程里所有代理环境变量的快照（名字 → 值）。"""
    return {
        name: value
        for name, value in os.environ.items()
        if name.lower().endswith(_PROXY_SUFFIX)
    }


def clear_proxy_environment() -> None:
    """移除进程里所有 ``*_proxy`` 环境变量。

    供测试套件起点与用例 ``setUp`` 调用，确保起点是确定的「无代理」。
    """
    for name in proxy_environment():
        os.environ.pop(name, None)


def restore_proxy_environment(snapshot: dict[str, str]) -> None:
    """先清空所有 ``*_proxy``，再写回 ``snapshot``（供 ``tearDown`` 用）。

    先清后写，是为了让快照里**没有**的变量也回到缺失状态，避免上一次用例设置的
    值泄漏到下一次。
    """
    clear_proxy_environment()
    os.environ.update(snapshot)

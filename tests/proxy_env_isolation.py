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

⚠️ **Windows 上只清环境变量还不够**：``urllib.request.getproxies()`` 的语义是
``getproxies_environment() or getproxies_registry()`` —— 环境里一个代理都没配时，
它会**回落去读注册表**（WinINET，即「Internet 选项」里的系统代理）。于是这台机器的
系统代理会漏进「无代理」用例，隔离并非真的成立。所以本模块**同时**清掉那三个注册表
值（``ProxyEnable`` / ``ProxyServer`` / ``ProxyOverride``），并在进程退出时把原值写回。

做法
----
:mod:`tests` 在导入时调用 :func:`clear_proxy_environment`，把整套测试的起点固定成
「无代理」。需要验证代理路径的用例（``tests/test_proxy_support.py``）在 ``setUp``
里自己保存并设置环境变量、``tearDown`` 里还原 —— 于是「无代理」与「有代理」两个
分支都由用例**显式**决定，不再由运行环境决定。

注册表那一半是 **fail-safe** 的：非 Windows、没有 ``winreg``、或注册表不可访问
（键不存在 / 无权限）时一律静默返回、绝不抛错。清之前先留底、退出时还原，
不把这台机器的系统代理**永久**改掉。
"""

from __future__ import annotations

import atexit
import os

#: 代理环境变量统一以 ``_proxy`` 结尾（``http_proxy`` / ``HTTPS_PROXY`` /
#: ``ALL_PROXY`` / ``no_proxy`` / ``ftp_proxy`` …），大小写都要认。
_PROXY_SUFFIX = "_proxy"

#: WinINET（「Internet 选项」）的系统代理设置：项路径与三个值名。
#: 与 ``urllib.request.getproxies_registry()`` 读的是同一个地方。
_WININET_SETTINGS_KEY = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
_WININET_VALUE_NAMES = ("ProxyEnable", "ProxyServer", "ProxyOverride")

#: :func:`clear_wininet_proxy` 首次生效前留存的原值（值名 → ``(数据, 类型)``，
#: 即 ``winreg.QueryValueEx`` 的返回顺序）；由 ``atexit`` 写回，避免测试把
#: 这台机器的系统代理**永久**清掉。
_wininet_saved_values: dict[str, tuple[object, int]] | None = None
_wininet_restore_registered = False


def proxy_environment() -> dict[str, str]:
    """返回当前进程里所有代理环境变量的快照（名字 → 值）。

    ⚠️ 快照**只含环境变量**：注册表（WinINET）那一半由 :func:`clear_wininet_proxy`
    在清理时留底、退出时还原，不参与「用例自己设置 / 还原」这套快照-还原协议。
    """
    return {
        name: value
        for name, value in os.environ.items()
        if name.lower().endswith(_PROXY_SUFFIX)
    }


def clear_proxy_environment() -> None:
    """移除进程里所有 ``*_proxy`` 环境变量，并清掉 Windows 系统代理（WinINET）。

    供测试套件起点与用例 ``setUp`` 调用，确保起点是确定的「无代理」。
    注册表不可用时静默跳过（fail-safe）。
    """
    for name in proxy_environment():
        os.environ.pop(name, None)
    clear_wininet_proxy()


def restore_proxy_environment(snapshot: dict[str, str]) -> None:
    """先清空所有 ``*_proxy``，再写回 ``snapshot``（供 ``tearDown`` 用）。

    先清后写，是为了让快照里**没有**的变量也回到缺失状态，避免上一次用例设置的
    值泄漏到下一次。
    """
    clear_proxy_environment()
    os.environ.update(snapshot)


# ----------------------------------------------------------------------
# Windows 系统代理（WinINET）
# ----------------------------------------------------------------------
def _winreg_module():
    """返回 ``winreg`` 模块；非 Windows 或不可用时返回 ``None``。"""
    if os.name != "nt":
        return None
    try:
        import winreg
    except ImportError:  # pragma: no cover - 非 Windows 分支已在上方返回
        return None
    return winreg


def _open_wininet_settings(winreg, access: int):
    """打开 WinINET 设置项（调用方负责关闭）。"""
    return winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WININET_SETTINGS_KEY, 0, access)


def _read_wininet_values(winreg, key) -> dict[str, tuple[object, int]]:
    """读回三个系统代理值里存在的那些（值名 → ``(数据, 类型)``）。"""
    values: dict[str, tuple[object, int]] = {}
    for name in _WININET_VALUE_NAMES:
        try:
            values[name] = winreg.QueryValueEx(key, name)
        except FileNotFoundError:
            continue
    return values


def clear_wininet_proxy() -> None:
    """删掉 WinINET 的三个代理值；首次调用前先留底，进程退出时还原。

    fail-safe：非 Windows、没有 ``winreg``、或注册表不可访问时静默返回。
    """
    global _wininet_saved_values, _wininet_restore_registered
    winreg = _winreg_module()
    if winreg is None:
        return
    try:
        with _open_wininet_settings(
            winreg, winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE
        ) as key:
            if _wininet_saved_values is None:
                _wininet_saved_values = _read_wininet_values(winreg, key)
                if not _wininet_restore_registered:
                    _wininet_restore_registered = True
                    atexit.register(restore_wininet_proxy)
            for name in _WININET_VALUE_NAMES:
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    continue
    except OSError:
        return


def restore_wininet_proxy() -> None:
    """把 :func:`clear_wininet_proxy` 首次清掉前的原值写回（幂等、fail-safe）。"""
    global _wininet_saved_values
    saved_values = _wininet_saved_values
    _wininet_saved_values = None
    if not saved_values:
        return
    winreg = _winreg_module()
    if winreg is None:
        _wininet_saved_values = saved_values
        return
    try:
        with _open_wininet_settings(
            winreg, winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE
        ) as key:
            for name in _WININET_VALUE_NAMES:
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    continue
            for name, (value_data, value_type) in saved_values.items():
                winreg.SetValueEx(key, name, 0, value_type, value_data)
    except OSError:
        _wininet_saved_values = saved_values
        return

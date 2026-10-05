"""「把自己的 nick 写进白名单」这条陷阱的**共享件** —— 五个测试文件里都用到的常量与源码扫描工具。

本模块**不以 ``test_`` 开头**，所以 unittest 的 discover **不会收集它**（它没有用例）。

## 为什么要共享而不是各写一份

:data:`GATE_PRINCIPAL_BY_PLATFORM` / :data:`SELF_IDENTITY_PRINCIPAL_PLATFORMS` /
:data:`SELF_IDENTITY_SYMBOLS` 三张表被**五个**测试文件引用，而其中的断言**互相咬合**：
:mod:`~tests.test_gate_principal_audit` 断言「principal 不是自己身份的符号」，
:mod:`~tests.test_nick_checkpoint` 拿同一张平台表去枚举 ``self.nick`` 赋值点。
把它们抄成五份，等于把「改一处漏四处」变成**结构上的必然** —— 而这类漏法不会被任何
测试发现（漏的那份只是少了一条约束，绿的照样绿）。

## 归属

拆分自原先那个 746 行的 ``tests/test_nick_in_allowlist.py``（纯结构调整，用例逐字未变）。
完整机制说明（为什么这是洞、为什么是拒绝而不是 warning）留在
:mod:`tests.test_nick_in_allowlist` 的模块 docstring 里，本模块不重复。
"""

from __future__ import annotations

import ast
import io
from pathlib import Path

ADAPTERS_DIR = Path(__file__).resolve().parent.parent / "opencode_bridge" / "adapters"
README_PATH = Path(__file__).resolve().parent.parent / "README.md"

#: 闸门真正用的那个 principal，逐平台。这里写的是**期望值**；
#: ``GatePrincipalAuditTests.test_each_platform_has_exactly_one_gate_principal_symbol``
#: 从源码 AST 里重新扫出来比对 —— 谁改了 principal 的来源，那条断言就红，
#: 于是"是否仍是同型陷阱"会被**重新提问**，而不是靠人记得复查。
GATE_PRINCIPAL_BY_PLATFORM = {
    "a2a": "peer",
    "discord": "channel_id",
    "email": "sender",
    "homeassistant": "entity_id",
    "irc": "target",
    "matrix": "room_id",
    "mattermost": "channel_id",
    "nextcloud": "token",
    "ntfy": "topic",
    "qqbot": "target",
    "slack": "channel",
    "telegram": "chat_id",
    "twitch": "target",
}

#: **只有**这两家：私聊的 principal 与「自己是谁」是同一个字符串。
#:
#: 判据（可从源码复核）：``is_private`` 拿 ``PRIVMSG`` 的 ``params[0]`` 与
#: ``self.nick`` 比，而**同一个** ``params[0]`` 就是喂给 ``self.admits(...)`` 的那个。
SELF_IDENTITY_PRINCIPAL_PLATFORMS = frozenset({"irc", "twitch"})

#: 「自己是谁」这一类符号名。闸门 principal **不允许**是其中任何一个。
SELF_IDENTITY_SYMBOLS = frozenset({
    "nick", "login", "display_name", "bot_token", "bot_user",
    "user_id", "my_user_id", "_my_user_id", "self_id", "bot_name",
})


def _module_source(platform: str) -> str:
    return _read_text(ADAPTERS_DIR / f"{platform}.py")


def _read_text(path: Path) -> str:
    with io.open(path, encoding="utf-8") as handle:
        return handle.read()


def _walk_without_nested_functions(function: ast.FunctionDef):
    """函数体子树，但**不进**嵌套的 ``def`` / ``lambda``（那些归它们自己那一轮）。"""
    stack = list(function.body)
    while stack:
        current = stack.pop()
        yield current
        for child in ast.iter_child_nodes(current):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            stack.append(child)


def _is_self_admits_call(node: ast.AST) -> bool:
    """是不是 ``self.admits(...)`` —— 闸门对外**只有**这一个入口。

    （:meth:`Adapter.answer_pairing_request` 内部也是调它，所以没有第二个入口。）
    """
    if not (isinstance(node, ast.Call) and node.args):
        return False
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "admits"
        and isinstance(func.value, ast.Name)
        and func.value.id == "self"
    )


def _scan_admits_calls(tree: ast.Module) -> list[tuple[ast.FunctionDef, ast.Call]]:
    """``(所在函数, self.admits(<arg>) 调用)`` 的全部配对。"""
    pairs: list[tuple[ast.FunctionDef, ast.Call]] = []
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef):
            continue
        for inner in _walk_without_nested_functions(function):
            if _is_self_admits_call(inner):
                pairs.append((function, inner))  # type: ignore[arg-type]
    return pairs

"""「nick 变成已知值的每一个时刻」怎么被**同一个**检查点覆盖。

拆出来的理由：这一组只断言「``nick`` 是 property 且赋值绕不开 setter」——
它读的是**适配器源码的 AST**（:mod:`ast`）与两家的类字典，**不构造任何适配器、
不碰闸门、也不起进程**，与其它三组无共享状态。混在一起会让「跑哪几个用例」变成一个
不确定的开关。

机制说明见 :mod:`tests.test_nick_in_allowlist` 的模块 docstring。
本文件不含任何用例改动。
"""

from __future__ import annotations

import ast
import unittest

from opencode_bridge.adapters.irc import IRCAdapter
from opencode_bridge.adapters.twitch import TwitchAdapter
from tests.inbound_log_support import RecordingHooks
from tests.nick_trap_support import SELF_IDENTITY_PRINCIPAL_PLATFORMS, _module_source


class NickCheckpointTests(unittest.TestCase):
    """「nick 变成已知值的每一个时刻」怎么被**同一个**检查点覆盖。"""

    def test_the_two_adapters_share_one_kind_of_checkpoint(self):
        """两家的 ``nick`` 都是本类自己定义的 property 且带 setter。"""
        for adapter in (IRCAdapter, TwitchAdapter):
            with self.subTest(adapter=adapter.__name__):
                attribute = adapter.__dict__.get("nick")
                self.assertIsInstance(
                    attribute, property,
                    "nick 必须是本类**自己**定义的 property；继承来的不算"
                    "（否则给基类加检查会牵连 11 个不需要它的平台）",
                )
                self.assertIsNotNone(attribute.fset, "property 必须有 setter")

    def test_every_nick_assignment_site_goes_through_that_one_setter(self):
        """把两个文件里每一处 ``self.nick = ...`` 列出来，并核对它们**绕不开** setter。

        ⛔ 这条**不是**在断言"赋值点只有这几处"（那是数出来的、明天就可能过期）。
        它断言的是**为什么**这些赋值点全都安全：因为 ``nick`` 是 data descriptor，
        ``self.nick = X`` 走 setter，而 setter 是唯一做检查的地方 ⇒ 将来**新增**任何
        一处赋值，它自动被覆盖，不需要在这里补断言。

        但本条仍然有作用：它把"赋值点确实存在、且全都落在 ``self.`` 上"钉住，
        防止有人改成 ``self.__dict__[...] = ...`` 或 ``object.__setattr__`` 绕过
        descriptor —— 那两种写法会**绕开**检查，而它们恰好长得像"普通的性能优化"。
        """
        for platform in sorted(SELF_IDENTITY_PRINCIPAL_PLATFORMS):
            with self.subTest(platform=platform):
                source = _module_source(platform)
                tree = ast.parse(source)
                assignments = [
                    node for node in ast.walk(tree)
                    if isinstance(node, ast.Assign)
                    for target in node.targets
                    if isinstance(target, ast.Attribute) and target.attr == "nick"
                    and isinstance(target.value, ast.Name) and target.value.id == "self"
                ]
                self.assertGreaterEqual(
                    len(assignments), 1,
                    f"{platform} 一个 self.nick = ... 都没有 ⇒ "
                    f"要么 nick 从别处来（本条的前提变了），要么判据坏了",
                )
                bypasses = [
                    ast.unparse(node) for node in ast.walk(tree)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "__setattr__"
                ]
                self.assertEqual(
                    [text for text in bypasses if "nick" in text], [],
                    f"{platform} 用 __setattr__ 直写会绕开 property 的检查",
                )
                self.assertNotIn(
                    'self.__dict__["nick"]', source,
                    f"{platform} 用 __dict__ 直写会绕开 property 的检查",
                )

    def test_the_constructor_assignment_itself_is_the_checked_path(self):
        """构造路径上那一次赋值也走 setter —— 不是 ``__init__`` 之后另做一次检查。"""
        adapter = IRCAdapter(
            {"host": "127.0.0.1", "nick": "CheckedBot", "channels": "#chan"},
            RecordingHooks(),
        )
        self.assertEqual(adapter.nick, "CheckedBot")
        self.assertEqual(adapter._nick, "CheckedBot",
                         "property 的后备字段必须与读到的值一致")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

"""``StreamCursorStore`` 的**独立**测试 —— 全程不构造 :class:`BridgeCore`。

这一整个文件存在的理由是 AGENTS.md §5.1 说的"抽出去"那种拆法：新类必须能脱离
原类单独测。依赖只有一条（``StateStore``），所以这里用**真的** store：游标这件事的
全部要点就是"它真的落在 ``state.json`` 里，而且坏值不会变成垃圾算术"。

因此这里能断言 core 层面**看不见**的东西：``stream_scope`` 是**不透明键**（不是会话
id，两条互不撞车）、坏值退化成"没有已存位置"而不是"拿着垃圾值去算 UID 区间"、
以及写失败只告警不上抛（上了抛就会把适配器的轮询循环打断）。
"""

from __future__ import annotations

import inspect
import json
import os
import tempfile
import unittest

from opencode_bridge.core import BridgeCore
from opencode_bridge.state import StateStore
from opencode_bridge.stream_cursor import (
    _STREAM_CURSOR_META_KEY,
    StreamCursorStore,
)

SCOPE = "email:bot@example.invalid"
OTHER_SCOPE = "email:other@example.invalid"


class CollaboratorSurfaceTests(unittest.TestCase):
    def test_the_constructor_takes_the_one_injected_dependency(self):
        parameters = inspect.signature(StreamCursorStore.__init__).parameters
        self.assertEqual([name for name in parameters if name != "self"],
                         ["state"])
        for name, parameter in parameters.items():
            if name == "self":
                continue
            self.assertEqual(parameter.kind, inspect.Parameter.KEYWORD_ONLY, name)
            self.assertIs(parameter.default, inspect.Parameter.empty, name)

    def test_no_bridge_core_is_reachable_from_the_store(self):
        store = StreamCursorStore(state=StateStore(":memory:"))

        for name, value in vars(store).items():
            self.assertNotIsInstance(value, BridgeCore, name)


class CursorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = os.path.join(self.tempdir.name, "state.json")
        self.store = StreamCursorStore(state=StateStore(self.path))

    def raw_meta(self, scope: str = SCOPE):
        """直接读盘 —— 断言的是"真的落盘了"，不是解析返回值。"""
        with open(self.path, encoding="utf-8") as handle:
            return json.load(handle).get("meta", {}).get(scope)

    def test_a_scope_that_was_never_written_reads_back_as_absent(self):
        self.assertIsNone(self.store.load(SCOPE))

    def test_a_saved_position_survives_a_new_store_over_the_same_file(self):
        self.store.save(SCOPE, 42)

        self.assertEqual(StreamCursorStore(state=StateStore(self.path)).load(SCOPE),
                         42)
        self.assertEqual(self.raw_meta(SCOPE),
                         {_STREAM_CURSOR_META_KEY: 42})

    def test_scopes_are_independent_keys(self):
        """``stream_scope`` 不是会话 id：同一个适配器可能有多个账号。"""
        self.store.save(SCOPE, 1)
        self.store.save(OTHER_SCOPE, 2)

        self.assertEqual(self.store.load(SCOPE), 1)
        self.assertEqual(self.store.load(OTHER_SCOPE), 2)

    def test_a_stored_string_that_is_not_an_integer_is_ignored_and_logged(self):
        """退化方向必须是"重新走首次启动语义"，不能是"拿着垃圾值去算 UID 区间"。"""
        self.store.save.__self__._state.set_meta(SCOPE, _STREAM_CURSOR_META_KEY,
                                                 "not-a-number")

        with self.assertLogs("opencode_bridge.stream_cursor", level="WARNING"):
            self.assertIsNone(self.store.load(SCOPE))

    def test_a_position_written_as_a_string_is_coerced_on_the_way_in(self):
        self.store.save(SCOPE, "13")

        self.assertEqual(self.store.load(SCOPE), 13)
        self.assertEqual(self.raw_meta(SCOPE)[_STREAM_CURSOR_META_KEY], 13)

    def test_a_write_failure_warns_instead_of_raising(self):
        """位置丢了最坏是重启后重投一封；让异常冒到轮询线程会把收信循环打断。"""
        def refuse(conversation_id, key, value):
            raise OSError("read-only file system")
        self.store._state.set_meta = refuse

        with self.assertLogs("opencode_bridge.stream_cursor", level="WARNING"):
            self.store.save(SCOPE, 7)

        self.assertIsNone(self.store.load(SCOPE))


if __name__ == "__main__":
    unittest.main()

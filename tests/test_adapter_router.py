"""``AdapterRouter`` 的**独立**测试 —— 全程不构造 :class:`BridgeCore`。

这一整个文件存在的理由是 AGENTS.md §5.1 说的"抽出去"那种拆法：新类必须能脱离
原类单独测。所以这里没有 ``BridgeCore``、没有真适配器、没有 state store；挂载表与
归属缓存都由本类自己建，唯一注入的是那把共用的锁。

因此这里能断言 core 层面**看不见**的东西：挂载表是本类**拥有**的（不是从 core 借的
容器）、挂载顺序就是 ``adapters[0]`` 的含义、猜出来的名字会**写回缓存**（所以第一次
猜错会粘住后续查找）、以及空 platform 不许冲掉一条准确映射。
"""

from __future__ import annotations

import inspect
import threading
import unittest

from opencode_bridge.adapters import Adapter
from opencode_bridge.adapter_router import AdapterRouter
from opencode_bridge.core import BridgeCore
from opencode_bridge.hooks import MsgHandle, Outbound


class NamedAdapter(Adapter):
    """名字可控的离线替身，用来模拟"挂载了多个平台"。"""

    def __init__(self, name: str) -> None:
        super().__init__({}, hooks=None)  # type: ignore[arg-type]
        self.name = name
        self.label = name

    def start(self) -> None:
        return None

    def stop(self, timeout: float = 5.0) -> None:
        return None

    def send(self, out: Outbound) -> MsgHandle | None:
        return MsgHandle(out.conversation_id, "m1", self.name)

    def edit(self, handle: MsgHandle, out: Outbound) -> bool:
        return True

    def answer(self, query_id: str, text: str = "") -> None:
        return None


# ----------------------------------------------------------------------
# 1: 依赖面与状态归属（AGENTS.md §5.1 的"抽出去"能不能成立）
# ----------------------------------------------------------------------
class CollaboratorSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lock = threading.RLock()
        self.router = AdapterRouter(lock=self.lock)

    def test_the_constructor_takes_the_one_injected_dependency(self):
        parameters = inspect.signature(AdapterRouter.__init__).parameters
        self.assertEqual([name for name in parameters if name != "self"],
                         ["lock"])
        for name, parameter in parameters.items():
            if name == "self":
                continue
            self.assertEqual(parameter.kind, inspect.Parameter.KEYWORD_ONLY, name)
            self.assertIs(parameter.default, inspect.Parameter.empty, name)

    def test_no_bridge_core_is_reachable_from_the_router(self):
        """拿到了 core 就等于又耦合回大类和它的私有状态 —— 那这次拆分就白做了。"""
        self.router.attach(NamedAdapter("telegram"))
        for name, value in vars(self.router).items():
            self.assertNotIsInstance(value, BridgeCore, name)

    def test_the_lock_is_the_injected_object(self):
        """注入的是**同一个对象**，不是复制一份：否则互斥关系就变了。"""
        self.assertIs(self.router._lock, self.lock)

    def test_the_registry_and_the_cache_are_the_router_own(self):
        """挂载表与归属缓存只有这一块碰，所以它们**不注入**、自己建。"""
        other = AdapterRouter(lock=self.lock)
        for attribute in ("_adapters", "_adapter_by_name", "_conv_adapter"):
            self.assertIsNot(getattr(self.router, attribute),
                             getattr(other, attribute),
                             attribute)
        self.assertEqual(self.router._adapters, [])
        self.assertEqual(self.router._conv_adapter, {})


# ----------------------------------------------------------------------
# 2: 挂载表
# ----------------------------------------------------------------------
class AttachTests(unittest.TestCase):
    def setUp(self) -> None:
        self.router = AdapterRouter(lock=threading.RLock())

    def test_attach_registers_by_name_and_in_the_given_order(self):
        first = NamedAdapter("matrix")
        second = NamedAdapter("irc")
        self.router.attach(first)
        self.router.attach(second)

        self.assertEqual(self.router.adapters, (first, second))
        self.assertIs(self.router._adapter_by_name["matrix"], first)

    def test_attaching_the_same_object_twice_is_a_no_op(self):
        adapter = NamedAdapter("telegram")
        self.router.attach(adapter)
        self.router.attach(adapter)

        self.assertEqual(len(self.router.adapters), 1)

    def test_the_first_adapter_of_a_name_wins(self):
        """重名挂载不许把 ``_adapter_by_name`` 指向另一个实例 —— 那会让同一平台
        的两条会话发到两个适配器上去。"""
        first = NamedAdapter("telegram")
        second = NamedAdapter("telegram")
        self.router.attach(first)
        self.router.attach(second)

        self.assertIs(self.router._adapter_by_name["telegram"], first)
        self.assertEqual(len(self.router.adapters), 2)

    def test_the_adapters_property_hands_out_a_snapshot(self):
        adapter = NamedAdapter("telegram")
        self.router.attach(adapter)

        snapshot = self.router.adapters
        self.router.attach(NamedAdapter("irc"))

        self.assertEqual(snapshot, (adapter,))


# ----------------------------------------------------------------------
# 3: 准确映射（入站种下的那条）
# ----------------------------------------------------------------------
class RememberedPlatformTests(unittest.TestCase):
    def setUp(self) -> None:
        self.router = AdapterRouter(lock=threading.RLock())
        self.router.attach(NamedAdapter("matrix"))
        self.router.attach(NamedAdapter("irc"))

    def test_the_remembered_name_is_what_adapter_for_returns(self):
        self.router.remember_platform("irc:#chan", "irc")

        self.assertEqual(self.router.adapter_for("irc:#chan").name, "irc")

    def test_an_empty_platform_never_overwrites_a_known_mapping(self):
        self.router.remember_platform("irc:#chan", "irc")

        self.router.remember_platform("irc:#chan", "")

        self.assertEqual(self.router.asking_platform("irc:#chan"), "irc")

    def test_blank_conversation_ids_are_ignored(self):
        self.router.remember_platform("", "irc")

        self.assertEqual(self.router._conv_adapter, {})

    def test_the_platform_is_stripped(self):
        self.router.remember_platform("chat:1", "  telegram  ")

        self.assertEqual(self.router.asking_platform("chat:1"), "telegram")

    def test_asking_platform_is_empty_for_a_conversation_never_seen(self):
        self.assertEqual(self.router.asking_platform("brand:new"), "")


# ----------------------------------------------------------------------
# 4: 归属查找的顺序
# ----------------------------------------------------------------------
class AdapterForTests(unittest.TestCase):
    def setUp(self) -> None:
        self.router = AdapterRouter(lock=threading.RLock())

    def test_no_adapter_at_all_returns_none(self):
        self.assertIsNone(self.router.adapter_for("irc:#c"))

    def test_the_remembered_mapping_beats_the_prefix(self):
        self.router.attach(NamedAdapter("slack"))
        self.router.remember_platform("channel:C1", "slack")

        self.assertEqual(self.router.adapter_for("channel:C1").name, "slack")

    def test_a_remembered_platform_overrides_a_prefix_that_would_guess_elsewhere(self):
        """⚠️ 这一条必须让"准确映射"与"前缀猜测"**指向不同的适配器**，否则断言
        两种实现都能过 —— 那就等于没测。

        这里的入站来自 ``matrix``，而 ``irc:`` 这个前缀会把回复猜到 irc 去。
        """
        self.router.attach(NamedAdapter("irc"))
        self.router.attach(NamedAdapter("matrix"))
        self.router.remember_platform("irc:#chan", "matrix")

        self.assertEqual(self.router.adapter_for("irc:#chan").name, "matrix")

    def test_a_remembered_platform_that_is_not_attached_falls_through(self):
        """映射指向一个没挂载的平台 —— 不许返回 ``None``，要继续往下猜。"""
        self.router.attach(NamedAdapter("matrix"))
        self.router.remember_platform("irc:#chan", "irc")

        self.assertEqual(self.router.adapter_for("irc:#chan").name, "matrix")

    def test_the_polling_thread_beats_a_fallback_that_would_pick_another(self):
        """⚠️ 同理：那条线程快捷路径必须赢过一个**会挑另一个适配器**的兜底，
        否则断言在实现里删掉线程匹配之后照样通过。

        ``irc:`` 没挂载对应适配器，兜底会落到 ``adapters[0]``（``other``）。
        """
        other = NamedAdapter("other")
        slack = NamedAdapter("slack")
        self.router.attach(other)
        self.router.attach(slack)
        slack._thread = threading.current_thread()

        self.assertIs(self.router.adapter_for("irc:#chan"), slack)

    def test_a_guess_is_written_back_into_the_cache(self):
        """⚠️ 这就是"第一次猜错会粘住后续所有查找"的放大效应 —— 必须看得见。"""
        self.router.attach(NamedAdapter("slack"))
        self.router.attach(NamedAdapter("discord"))

        self.router.adapter_for("channel:C0123ABCD")

        self.assertEqual(self.router._conv_adapter, {"channel:C0123ABCD": "slack"})

    def test_a_migrated_adapter_without_a_thread_still_resolves(self):
        """A1 之后 ``_thread`` 恒为 ``None`` —— 前缀那条路必须仍然管用。"""
        self.router.attach(NamedAdapter("slack"))

        self.assertEqual(self.router.adapter_for("channel:C1").name, "slack")


# ----------------------------------------------------------------------
# 5: 前缀兜底（刻意保留的启发式；与读侧的归属文法**不是**同一件事）
# ----------------------------------------------------------------------
class PrefixRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.router = AdapterRouter(lock=threading.RLock())

    def attach(self, *names: str) -> list[NamedAdapter]:
        out = []
        for name in names:
            adapter = NamedAdapter(name)
            self.router.attach(adapter)
            out.append(adapter)
        return out

    def route(self, conversation_id: str, adapters: list) -> str:
        return self.router._route_by_prefix(conversation_id,
                                            list(self.router.adapters)).name

    def test_legacy_aliases_resolve_through_the_identity_registry(self):
        self.attach("matrix", "telegram", "irc")

        self.assertEqual(self.route("chat:55", []), "telegram")
        self.assertEqual(self.route("room:!abc:example.org", []), "matrix")

    def test_self_mapping_prefixes_resolve(self):
        self.attach("matrix", "irc", "twitch", "nextcloud")

        self.assertEqual(self.route("irc:#chan", []), "irc")
        self.assertEqual(self.route("twitch:someone", []), "twitch")
        self.assertEqual(self.route("nextcloud:tok", []), "nextcloud")

    def test_the_new_format_needs_no_guessing(self):
        self.attach("matrix", "ntfy", "email", "a2a")

        self.assertEqual(self.route("ntfy:mytopic", []), "ntfy")
        self.assertEqual(self.route("email:bot@x.invalid", []), "email")
        self.assertEqual(self.route("a2a:agent-1", []), "a2a")

    def test_the_ambiguous_channel_prefix_keeps_its_shape_heuristic(self):
        """纯数字 → discord、其余 → slack。**这仍然可能猜错**，所以它只是兜底。"""
        self.attach("matrix", "discord", "slack")

        self.assertEqual(self.route("channel:1234567890", []), "discord")
        self.assertEqual(self.route("channel:C0123ABCD", []), "slack")

    def test_the_heuristic_falls_back_when_its_candidate_is_absent(self):
        adapters = self.attach("matrix")

        self.assertEqual(self.route("channel:1234", adapters), "matrix")

    def test_an_unknown_or_bare_id_falls_back_to_the_first_adapter(self):
        adapters = self.attach("matrix", "irc")

        self.assertEqual(self.route("nosuch:x", adapters), "matrix")
        self.assertEqual(self.route("garbage", adapters), "matrix")


if __name__ == "__main__":
    unittest.main()

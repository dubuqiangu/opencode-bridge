"""适配器归属判定（``AdapterRouter.adapter_for`` 及其两条兜底）的测试。

## 为什么这块需要独立覆盖

A1 传输层迁移之前，归属判定有**两条**依据：

1. **调用线程匹配** —— ``getattr(adapter, "_thread")`` 等于当前线程就是它
2. conversation_id 前缀猜测

迁移之后，已迁移的适配器（IRC / Matrix / Telegram / …）**不再持有 ``_thread``**
（线程归传输层所有），第1 条**恒不命中**，于是全部落到第 2 条。

而迁移前的第 2 条只硬编码了 ``chat:`` 与 ``channel:`` 两种前缀，其余一律
``return adapters[0]`` —— "第一个挂载的适配器"，而 ``attach()`` 的顺序就是
**配置文件里的字典顺序**（用户可控）。于是多平台用户可能把回复发到**错误的平台**，
且**不报错**。

更糟的是归属判定会把**猜出来的**名字写进 ``_conv_adapter`` 缓存，
所以第一次猜错会**粘住**后续所有查找。

**这三层此前零测试覆盖** —— 这就是回归能活下来的原因。本文件补上。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from opencode_bridge.adapters import Adapter
from opencode_bridge.config import Config
from opencode_bridge.core import BridgeCore
from opencode_bridge.hooks import Inbound, MsgHandle, Outbound
from opencode_bridge.state import StateStore


class NamedAdapter(Adapter):
    """名字可控的离线替身，用来模拟"挂载了多个平台"。"""

    def __init__(self, name: str) -> None:
        super().__init__({}, hooks=None)  # type: ignore[arg-type]
        self.name = name
        self.label = name
        self.sent: list[Outbound] = []
        self._handles = 0

    def start(self) -> None:
        return None

    def stop(self, timeout: float = 5.0) -> None:
        return None

    def send(self, out: Outbound) -> MsgHandle | None:
        self.sent.append(out)
        self._handles += 1
        return MsgHandle(out.conversation_id, f"m{self._handles}", self.name)

    def edit(self, handle: MsgHandle, out: Outbound) -> bool:
        return True

    def answer(self, query_id: str, text: str = "") -> None:
        return None


class RoutingTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.mkdtemp(prefix="routing")
        self.addCleanup(shutil.rmtree, self.td, True)
        self.cfg = Config()
        self.client = _SilentClient()
        self.state = StateStore(os.path.join(self.td, "state.json"))
        self.core = BridgeCore(self.cfg, self.client, self.state)
        self.adapters: dict[str, NamedAdapter] = {}

    def attach(self, *names: str) -> list[NamedAdapter]:
        """按给定顺序挂载 —— 顺序刻意可控，因为那正是 ``adapters[0]`` 的含义。"""
        out = []
        for name in names:
            adapter = NamedAdapter(name)
            self.core.attach(adapter)
            self.adapters[name] = adapter
            out.append(adapter)
        return out

    def routed_to(self, conversation_id: str) -> str | None:
        adapter = self.core.routing.adapter_for(conversation_id)
        return adapter.name if adapter is not None else None


class _SilentClient:
    """``FakeClient`` 的最小替身：本文件只关心路由，不关心会话。"""

    def create_session(self, *a, **k):
        return "session-x"

    def prompt(self, *a, **k):
        return None

    def send_prompt(self, *a, **k):
        return None

    def abort_session(self, *a, **k) -> None:
        return None


# ----------------------------------------------------------------------
# 回归本体
# ----------------------------------------------------------------------
class InboundSeedsMappingTests(RoutingTestCase):
    def test_inbound_seeds_mapping_from_platform(self):
        """入站时必须用``Inbound.platform``（**准确**信息）种下映射。"""
        self.attach("matrix", "irc")
        self.core.on_inbound(
            Inbound(conversation_id="irc:#chan", text="hi", platform="irc")
        )
        self.assertEqual(self.core.routing._conv_adapter.get("irc:#chan"), "irc")

    def test_routes_to_correct_adapter_regardless_of_attach_order(self):
        """★ 回归本体：挂载顺序**故意**与前缀相反。

        迁移前IRC 持有 ``_thread``，线程匹配能确定归属；迁移后那条路失效，
        若前缀路由不认 ``irc:`` 就会落到 ``adapters[0]``（此处是 matrix），
        把 IRC 的回复发到 Matrix。
        """
        self.attach("matrix", "irc")            # adapters[0] 是 matrix
        self.core.on_inbound(
            Inbound(conversation_id="irc:#chan", text="hi", platform="irc")
        )
        self.assertEqual(self.routed_to("irc:#chan"), "irc")

    def test_wrong_first_guess_does_not_stick(self):
        """**先**用一条裸入站污染缓存，再来一条带platform 的 —— 后者必须纠正它。

        覆盖"第一次猜错会粘住后续所有查找"这个放大效应。
        """
        self.attach("matrix", "irc")
        # 模拟旧行为：platform 为空的入站（合成替身不填platform）
        self.core.on_inbound(Inbound(conversation_id="irc:#c", text="a", platform=""))
        # 此时按前缀路由应仍能落到 irc；下面这条必须种上准确映射
        self.core.on_inbound(
            Inbound(conversation_id="irc:#c", text="b", platform="irc")
        )
        self.assertEqual(self.core.routing._conv_adapter.get("irc:#c"), "irc")

    def test_callback_inbound_also_seeds_before_early_return(self):
        """按钮回调那条路会早退 —— 若在早退**之后**才记映射就漏了它。"""
        self.attach("matrix", "telegram")
        self.core.on_inbound(
            Inbound(
                conversation_id="chat:55",
                text="btn",
                kind="callback",
                callback_query_id="q1",
                platform="telegram",
            )
        )
        self.assertEqual(
            self.core.routing._conv_adapter.get("chat:55"), "telegram",
            "callback 早退前必须已经记下映射",
        )

    def test_empty_platform_does_not_clobber_a_known_mapping(self):
        """``Inbound.platform`` 为空时**不许**覆盖已有的准确映射。

        注意这里刻意**不**断言"缓存里没有这个键" —— 归属判定本来就会把
        解析结果写回缓存（单适配器场景下这是正确且无害的）。真正要守的不变量是
        方向性的：**已知的准确映射不能被一条信息量为零的入站冲掉**。
        """
        self.attach("matrix", "irc")
        self.core.on_inbound(
            Inbound(conversation_id="irc:#c", text="a", platform="irc")
        )
        self.assertEqual(self.core.routing._conv_adapter.get("irc:#c"), "irc")
        self.core.on_inbound(
            Inbound(conversation_id="irc:#c", text="b", platform="")
        )
        self.assertEqual(
            self.core.routing._conv_adapter.get("irc:#c"), "irc",
            "platform 为空不许覆盖已有的准确映射",
        )

    def test_empty_conversation_id_is_ignored(self):
        self.attach("matrix")
        self.core.on_inbound(Inbound(conversation_id="", text="hi", platform="irc"))
        self.assertNotIn("", self.core.routing._conv_adapter)


# ----------------------------------------------------------------------
# 兜底：前缀路由必须覆盖**全部**平台
# ----------------------------------------------------------------------
class PrefixRoutingTests(RoutingTestCase):
    def test_legacy_aliases_resolve(self):
        """旧别名靠identity 的登记表解析，不许在这里硬编码成第二份真相。"""
        self.attach("matrix", "telegram", "irc")
        self.assertEqual(self.routed_to("chat:55"), "telegram")
        self.assertEqual(self.routed_to("room:!abc:example.org"), "matrix")

    def test_self_mapping_legacy_prefixes_resolve(self):
        """irc / twitch / nextcloud 的前缀映射到自身 —— 也必须认。"""
        self.attach("matrix", "irc", "twitch", "nextcloud")
        self.assertEqual(self.routed_to("irc:#chan"), "irc")
        self.assertEqual(self.routed_to("twitch:someone"), "twitch")
        self.assertEqual(self.routed_to("nextcloud:tok"), "nextcloud")

    def test_new_format_prefix_resolves_directly(self):
        """新格式 ``platform:local_id``：平台段**本身就是答案**，不需要猜。"""
        self.attach("matrix", "ntfy", "email", "a2a")
        self.assertEqual(self.routed_to("ntfy:mytopic"), "ntfy")
        self.assertEqual(self.routed_to("email:bot@x.com"), "email")
        self.assertEqual(self.routed_to("a2a:agent-1"), "a2a")

    def test_bare_platform_name_without_colon_falls_back(self):
        """没有冒号就没有平台段可依据 —— 老实落到兜底，不假装能猜。"""
        adapters = self.attach("matrix", "irc")
        self.assertIs(self.core.routing.adapter_for("garbage"), adapters[0])

    def test_unknown_platform_falls_back_to_first(self):
        """不认识的前缀（比如用户接了个还没适配的平台）→ 兜底，不抛错。"""
        adapters = self.attach("matrix", "irc")
        self.assertIs(self.core.routing.adapter_for("nosuch:x"), adapters[0])

    def test_ambiguous_channel_prefix_keeps_its_heuristic(self):
        """``channel:`` 被 slack/discord/mattermost 共用，无法从字符串判定。

        这里保留既有启发式（纯数字→discord、C 开头→slack），但它**只是兜底** ——
        真正确定性来自入站时种下的映射。
        """
        self.attach("matrix", "discord", "slack")
        self.assertEqual(self.routed_to("channel:1234567890"), "discord")
        self.assertEqual(self.routed_to("channel:C0123ABCD"), "slack")

    def test_ambiguous_prefix_without_candidates_falls_back(self):
        """启发式指向的适配器没挂载时，退到首适配器而不是崩。"""
        adapters = self.attach("matrix")
        self.assertIs(self.core.routing.adapter_for("channel:1234"), adapters[0])

    def test_alias_whose_platform_is_not_attached_falls_back(self):
        """``room:`` 存在但 Matrix 没挂载 → 兜底，不返回 None。"""
        adapters = self.attach("irc")
        self.assertIs(self.core.routing.adapter_for("room:!a:b"), adapters[0])

    def test_no_adapters_at_all_returns_none(self):
        self.assertIsNone(self.core.routing.adapter_for("irc:#c"))


class ThreadIdentityTests(RoutingTestCase):
    def test_thread_identity_still_wins_when_present(self):
        """尚未迁移的适配器仍持有 ``_thread`` 时，线程匹配这条快捷路径保持有效
        （它比任何猜测都准），不要因为新增的映射逻辑把它废掉。"""
        adapter = self.attach("slack")[0]
        self.core.routing._conv_adapter.clear()
        self.core.routing._adapters.insert(0, NamedAdapter("other"))  # 抢走 adapters[0]

        import threading

        marker = threading.current_thread()
        adapter._thread = marker
        self.assertIs(self.core.routing.adapter_for("channel:C1"), adapter)

    def test_migrated_adapter_without_thread_still_routes(self):
        """迁移后的形态：``_thread`` 恒为 None —— 靠前缀/映射也要能找到它。"""
        adapter = self.attach("slack")[0]
        adapter._thread = None
        self.assertIs(self.core.routing.adapter_for("channel:C1"), adapter)


if __name__ == "__main__":
    unittest.main()
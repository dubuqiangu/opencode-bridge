"""出站长度上限的收敛：基类**声明**槽、**解析**一次、**只读**一个名字。

这份文件守的是一个**结构性**不变量，不是某个平台的数值。此前同一件事有**四个**
名字 —— ``message_limit``（类属性）、``max_message_length``（能力声明）、
``effective_max_length``（mattermost / nextcloud 各自的 property）、
``_effective_limit``（homeassistant / qqbot 各自的私有方法）—— 而**基类一个答案
都没有**：槽没在基类上声明，于是九个适配器各自声明一份，一个漏掉就在**投递时**
（真发一条用户消息时，而不是跑测试时）抛 ``AttributeError``。

四组验收：

1. :class:`ForgettingAdapterIsHarmlessTests` —— **漏声明不再崩**。一个什么都不声明的
   ``Adapter`` 子类跑完整条投递路径。
2. :class:`RuntimeRefinementStillWinsTests` —— **服务端细化没有被冻死**。Mattermost /
   Nextcloud 收窄上限后，真正投递出去的每一段都必须按收窄后的数切。
3. :class:`OnlyOneSpellingRemainsTests` —— **不再有第二个名字**：裸槽不被任何子类读、
   ``_effective_limit`` 消失、``effective_max_length`` 只在基类定义一次。
4. :class:`EveryRegisteredAdapterAgreesTests` —— **平台之间没有隐式差异**。

⚠️ 第 3 组是 AST 检查而不是字符串搜索：``self.message_limit = value`` 是**写**，
``self.message_limit`` 是**读**，只有后者才是"有人绕过 accessor"。用 ctx 区分二者，
所以这一组不会被"把裸槽换了个名字继续读"骗过。
"""

from __future__ import annotations

import ast
import pathlib
import unittest

from opencode_bridge.adapters import Adapter
from opencode_bridge.adapters.base import adapter_class, registered_names
from opencode_bridge.hooks import Inbound, MsgHandle, Outbound, SendError
from opencode_bridge.split import split_text

from tests.test_mattermost import CHANNEL as MATTERMOST_CHANNEL
from tests.test_mattermost import make_adapter as make_mattermost_adapter
from tests.test_nextcloud import CID as NEXTCLOUD_CONVERSATION
from tests.test_nextcloud import attach as attach_nextcloud_stub
from tests.test_nextcloud import make_adapter as make_nextcloud_adapter
from tests.test_nextcloud import ok_chat as nextcloud_ok_chat

PACKAGE_ROOT = pathlib.Path(__file__).resolve().parent.parent / "opencode_bridge"
ADAPTER_DIR = PACKAGE_ROOT / "adapters"

#: 基类上"运行期细化槽"的名字。它是**唯一**允许读裸槽的地方。
LIMIT_SLOT = "message_limit"
#: 唯一允许读出站上限的 accessor。
LIMIT_ACCESSOR = "effective_max_length"
#: 收敛前 homeassistant / qqbot 各自的私有方法名，必须**彻底消失**。
RETIRED_SPELLING = "_effective_limit"


class InertHooks:
    """``Adapter.__init__`` 只要一个 hooks；这里什么都不实现。"""

    def on_inbound(self, inbound: Inbound) -> None:
        return None

    def on_callback(self, conversation_id: str, data: str, query_id: str) -> None:
        return None

    def load_stream_cursor(self, stream_scope: str):
        return None

    def save_stream_cursor(self, stream_scope: str, position: str) -> None:
        return None


class DeclaresNothingAdapter(Adapter):
    """一个仓库里不存在、且**什么都不声明**的平台。

    它刻意不声明 ``max_message_length``、不声明 ``message_limit``，只声明 ``name`` /
    ``label`` 这两个每家都必须有的。``send()`` 照抄同族适配器的形状：按
    :attr:`effective_max_length` 切、逐段投递、返回最后一段的句柄。
    """

    name = "quibblechat"
    label = "Quibblechat"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config or {}, InertHooks())
        self.delivered: list[str] = []

    def start(self) -> None:
        return None

    def send(self, out: Outbound) -> MsgHandle | None:
        if not out.text:
            self._note_send_failure(SendError.BAD_FORMAT, "empty text")
            return None
        chunks = split_text(out.text, self.effective_max_length, prefix_fmt="")
        handle: MsgHandle | None = None
        for index, chunk in enumerate(chunks, start=1):
            self.delivered.append(chunk)
            handle = MsgHandle(
                conversation_id=out.conversation_id,
                message_id=str(index),
                platform=self.name,
            )
        return handle

    def edit(self, handle: MsgHandle, out: Outbound) -> bool:
        return False


# ----------------------------------------------------------------------
# 1) 漏声明不再崩
# ----------------------------------------------------------------------
class ForgettingAdapterIsHarmlessTests(unittest.TestCase):
    def test_reading_the_limit_slot_no_longer_raises(self):
        """**这就是修复前那个投递期崩溃。**

        修复前基类不声明 ``message_limit``，而这一行在九个适配器的 ``send()`` 里
        出现；新写的适配器若忘了声明声明它，用户消息真的发出去时才炸
        ``AttributeError`` —— 测试全绿、线上才崩。
        """
        adapter = DeclaresNothingAdapter()

        slot_value = adapter.message_limit          # 修复前：AttributeError
        self.assertEqual(slot_value, 0, "槽的默认必须是 0（= 未细化），不是平台上限")
        self.assertEqual(
            adapter.effective_max_length, adapter.max_message_length,
            "没有细化值时必须退回能力声明的静态下限",
        )

    def test_the_base_class_itself_declares_the_slot(self):
        """声明**只在**基类：子类继承得到，谁都漏不掉（也谁都不必再声明一遍）。"""
        self.assertIn(LIMIT_SLOT, vars(Adapter))
        for platform in registered_names():
            with self.subTest(platform=platform):
                cls = adapter_class(platform)
                self.assertIsNotNone(cls)
                self.assertTrue(
                    hasattr(cls, LIMIT_SLOT),
                    f"{platform} 解析不到槽 —— 基类的声明没生效",
                )
                self.assertNotIn(
                    LIMIT_SLOT, vars(cls),
                    f"{platform} 自己又声明了一遍槽 —— 那正是根因，收敛白做了",
                )

    def test_a_subclass_declaring_nothing_delivers_a_real_send(self):
        """什么都不声明也要能把消息**真的投出去**，且一个字符不少。"""
        adapter = DeclaresNothingAdapter()
        body = "字" * (adapter.max_message_length * 2 + 17)

        handle = adapter.send(Outbound("quibblechat:room", body))

        self.assertIsNotNone(handle)
        self.assertEqual(handle.message_id, "3")
        self.assertEqual("".join(adapter.delivered), body)
        self.assertTrue(
            all(len(piece) <= adapter.max_message_length for piece in adapter.delivered),
            f"每一片都必须在上限内：{[len(p) for p in adapter.delivered]}",
        )
        self.assertTrue(adapter.send_result(Outbound("quibblechat:room", "hi")).ok)


# ----------------------------------------------------------------------
# 2) 服务端细化没有被冻死
# ----------------------------------------------------------------------
class RuntimeRefinementStillWinsTests(unittest.TestCase):
    def test_mattermost_max_post_size_lowers_the_delivered_pieces(self):
        """服务端说 500 就必须按 500 切，且真发出去的每条 post 都得 ≤ 500。"""
        adapter, _ = make_mattermost_adapter()
        self.assertEqual(adapter.effective_max_length, 4000, "默认是静态下限")

        posted: list[str] = []

        def fake(method, path, payload=None, *, timeout=None):
            if method == "POST":
                posted.append(payload["message"])
                return 201, {"id": f"post{len(posted)}"}
            return 200, {}

        adapter._request = fake  # type: ignore[method-assign]
        body = "字" * 1200

        self.assertEqual(adapter._apply_max_post_size(500), 500)
        self.assertEqual(adapter.effective_max_length, 500)

        handle = adapter.send(Outbound(f"mattermost:{MATTERMOST_CHANNEL}", body))

        self.assertIsNotNone(handle, "细化后仍要能投递")
        self.assertEqual(len(posted), 3)
        self.assertTrue(all(len(piece) <= 500 for piece in posted), posted)
        self.assertEqual("".join(posted), body)

    def test_nextcloud_max_length_lowers_the_delivered_pieces(self):
        """Nextcloud 同理：部署侧的 ``max-length`` 收窄后必须照办。"""
        adapter, _ = make_nextcloud_adapter()
        self.assertEqual(adapter.effective_max_length, 32000, "默认是源码常量")

        sent: list[str] = []

        def capture(method, path, *, params=None, form=None, timeout=None):
            if method == "POST":
                sent.append(form["message"])
                return nextcloud_ok_chat(len(sent))
            return nextcloud_ok_chat()

        attach_nextcloud_stub(adapter, capture)  # type: ignore[arg-type]
        body = "字" * 1000

        self.assertEqual(adapter._apply_max_chat_length(400), 400)
        self.assertEqual(adapter.effective_max_length, 400)

        handle = adapter.send(Outbound(NEXTCLOUD_CONVERSATION, body))

        self.assertIsNotNone(handle, "细化后仍要能投递")
        self.assertEqual(len(sent), 3)
        self.assertTrue(all(len(piece) <= 400 for piece in sent), sent)
        self.assertEqual("".join(sent), body)

    def test_dropping_back_to_the_static_floor_still_works(self):
        """细化过一次之后读不到服务端配置了，必须干净地退回静态下限。"""
        for build, apply_refinement, static_floor in (
            (make_mattermost_adapter, "_apply_max_post_size", 4000),
            (make_nextcloud_adapter, "_apply_max_chat_length", 32000),
        ):
            with self.subTest(platform=build.__module__):
                adapter, _ = build()
                refine = getattr(adapter, apply_refinement)
                self.assertEqual(refine(500), 500)
                self.assertEqual(adapter.effective_max_length, 500)
                self.assertEqual(refine(None), static_floor)
                self.assertEqual(adapter.effective_max_length, static_floor)


# ----------------------------------------------------------------------
# 3) 只剩一个名字
# ----------------------------------------------------------------------
def _class_attribute_targets(class_node: ast.ClassDef) -> list[str]:
    """类体里被赋值的名字（``x = ...`` / ``x: int = ...``）。"""
    found: list[str] = []
    for stmt in class_node.body:
        if isinstance(stmt, ast.Assign):
            found.extend(t.id for t in stmt.targets if isinstance(t, ast.Name))
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            found.append(stmt.target.id)
    return found


def _load_attr_reads(path: pathlib.Path) -> list[tuple[str, int]]:
    """文件里所有**读取**裸槽的位置（``self.message_limit`` 之类），不含写入。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    return [
        (node.attr, node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr == LIMIT_SLOT
        and isinstance(node.ctx, ast.Load)
    ]


class OnlyOneSpellingRemainsTests(unittest.TestCase):
    def test_no_adapter_module_reads_the_raw_slot(self):
        """裸槽只在基类的 accessor 里被读一次；任何别的读取点都是绕过 accessor。"""
        for path in sorted(PACKAGE_ROOT.rglob("*.py")):
            if path.name == "base.py":
                continue
            with self.subTest(module=path.name):
                self.assertEqual(
                    _load_attr_reads(path), [],
                    f"{path.name} 绕过了 {LIMIT_ACCESSOR} 直接读裸槽",
                )

    def test_the_slot_is_read_exactly_once_in_the_base_and_only_by_the_accessor(self):
        source = (ADAPTER_DIR / "base.py").read_text(encoding="utf-8")
        tree = ast.parse(source, "base.py")
        adapter_class = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "Adapter"
        )
        accessor = next(
            node for node in adapter_class.body
            if isinstance(node, ast.FunctionDef) and node.name == LIMIT_ACCESSOR
        )
        accessor_reads = [
            node.lineno for node in ast.walk(accessor)
            if isinstance(node, ast.Attribute)
            and node.attr == LIMIT_SLOT
            and isinstance(node.ctx, ast.Load)
        ]
        self.assertEqual(len(accessor_reads), 1, "accessor 里应恰好读一次裸槽")

        everything_else = [
            (node.lineno) for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and node.attr == LIMIT_SLOT
            and isinstance(node.ctx, ast.Load)
            and node.lineno not in accessor_reads
        ]
        self.assertEqual(everything_else, [], "基类里还有别的裸槽读取点")

    def test_the_accessor_is_defined_exactly_once_in_the_whole_package(self):
        definitions: list[str] = []
        for path in sorted(PACKAGE_ROOT.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                for stmt in node.body:
                    if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    if stmt.name in (LIMIT_ACCESSOR, RETIRED_SPELLING):
                        definitions.append(f"{path.name}:{node.name}.{stmt.name}")
        self.assertEqual(
            definitions, [f"base.py:Adapter.{LIMIT_ACCESSOR}"],
            "解析逻辑必须只存在于基类一处（收敛前有四份）",
        )

    def test_no_adapter_module_declares_its_own_slot(self):
        """十二份重复声明是"根因"的另一半：两个同义不同名的数并存，消费者只能猜。"""
        offenders: list[str] = []
        for path in sorted(ADAPTER_DIR.glob("*.py")):
            if path.name == "base.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and LIMIT_SLOT in _class_attribute_targets(node):
                    offenders.append(f"{path.name}:{node.name}")
        self.assertEqual(offenders, [], "适配器不应再自带 message_limit 声明")

    def test_the_retired_private_helper_is_gone(self):
        for platform in registered_names():
            with self.subTest(platform=platform):
                cls = adapter_class(platform)
                self.assertFalse(
                    hasattr(cls, RETIRED_SPELLING),
                    f"{platform} 还留着 {RETIRED_SPELLING}",
                )


# ----------------------------------------------------------------------
# 4) 各平台之间没有隐式差异
# ----------------------------------------------------------------------
class EveryRegisteredAdapterAgreesTests(unittest.TestCase):
    def test_the_accessor_matches_the_declared_floor_before_any_refinement(self):
        for platform in registered_names():
            with self.subTest(platform=platform):
                adapter = adapter_class(platform)({}, InertHooks())
                self.assertGreater(adapter.effective_max_length, 0)
                self.assertEqual(
                    adapter.effective_max_length, int(adapter.max_message_length),
                    "没有任何细化值时，生效值必须**等于**声明的静态下限",
                )

    def test_a_runtime_refinement_is_picked_up_by_every_platform(self):
        """任何平台（含没细化过的那两家）都能吃下细化值 —— 槽是全平台通用的。"""
        for platform in registered_names():
            with self.subTest(platform=platform):
                adapter = adapter_class(platform)({}, InertHooks())
                adapter.message_limit = 137
                self.assertEqual(adapter.effective_max_length, 137)

    def test_a_nonsense_slot_value_falls_back_instead_of_propagating(self):
        for junk in (None, 0, -1, "", "abc", True):
            with self.subTest(slot=junk):
                adapter = DeclaresNothingAdapter()
                adapter.message_limit = junk  # type: ignore[assignment]
                self.assertEqual(
                    adapter.effective_max_length, adapter.max_message_length,
                    f"{junk!r} 不是长度，必须退回静态下限",
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

"""``edit()`` 收到超限正文时的契约：**五个平台本地拒收，一个平台不拒收**。

## 为什么这件事需要一份"不一致"的契约

``Adapter.supports_message_edit`` 有六家为 ``True``，而"正文超过平台上限"这件事
**五家会被平台拒收、一家不会**：

| adapter | 平台对超限 ``edit`` 的真实回答 | 本仓库的做法 |
|---|---|---|
| ``discord`` | HTTP 400 ``code 50035`` | 本地抛 ``ValueError`` |
| ``telegram`` | HTTP 400 "message is too long" | 本地抛 ``ValueError`` |
| ``slack`` | ``ok:false`` + ``msg_too_long`` | 本地抛 ``ValueError`` |
| ``mattermost`` | HTTP 400 ``model.post.is_valid.message_length.app_error`` | 本地抛 ``ValueError`` |
| ``nextcloud`` | HTTP 413 | 本地抛 ``ValueError`` |
| ``matrix`` | **没有"编辑"这个 API** —— 它的 ``edit()`` 自己就是一条 ``send``，而上限是**我们自选**的保守值 | 退化成普通 ``send`` |

把六家改成一致才是错的：

* 对那五家改成"退化成 send" ⇒ 占位消息上已经显示的那一截被**读两遍**
  （:class:`TheDegradeAlternativeWouldDuplicateTheReaderTests` 实测 4000 字读成
  5500 字 —— 那是 ``a0f877e`` 说的"读者拿到完整答复恰好一次"被打破）。
* 对 matrix 改成"抛" ⇒ **拒绝投递一条 Matrix 乐意收下的消息**（它的 4096 是按
  事件体 64KB 上限自选的，不是平台会拒收的阈值）。

## 抛出去会不会中断这一轮？

**不会**，而且这是"抛"这条契约能成立的**前提**，所以这里有一条源码结构断言钉住它：
包里每一个 ``adapter.edit(`` 调用点都必须在 ``try`` 里，且必须真的接住异常
（:class:`ARaisingEditCannotEscapeItsCallerTests`）。动态那一半跑**真桥**：让一个
真适配器在收尾那一步抛，然后断言**什么都没到达** ``EventStream.dispatch`` 的调用方，
而读者仍然拿到完整答复。

## 判据：零请求

"抛了异常"本身不说明什么 —— 一个先发请求再抛的适配器也抛。所以每一条都断言
**线路记录器里一条都没有**：把适配器最底下那一层（``_request`` / ``_api``）换成
计数器，走的是**真适配器对象**（真解析、真节流、真分片），只有传输层被换掉。
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pathlib
import unittest
from dataclasses import dataclass, field

from opencode_bridge.adapters.base import adapter_class, registered_names
from opencode_bridge.hooks import MsgHandle, Outbound

from tests.test_progress_placeholder import (
    _InertHooks,
    answer_of_length,
    build_platform,
    reader_view,
    run_one_turn,
)
from tests.test_mattermost import CHANNEL as MATTERMOST_CHANNEL
from tests.test_mattermost import make_adapter as make_mattermost_adapter
from tests.test_nextcloud import CID as NEXTCLOUD_CONVERSATION
from tests.test_nextcloud import Stub as NextcloudStub
from tests.test_nextcloud import attach as attach_nextcloud_stub
from tests.test_nextcloud import make_adapter as make_nextcloud_adapter
from tests.test_nextcloud import ocs as nextcloud_ocs
from tests.test_slack import make_slack

PACKAGE_ROOT = pathlib.Path(__file__).resolve().parent.parent / "opencode_bridge"

#: 本地拒收的那五个 —— 平台真的会拒收，所以本地就别发。
PLATFORMS_THAT_REFUSE = ("discord", "mattermost", "nextcloud", "slack", "telegram")

#: 唯一不拒收的那一个：它没有"编辑"这个 API，``edit()`` 自己就是一条 ``send``。
PLATFORM_THAT_DEGRADES = "matrix"


@dataclass
class Wired:
    """一个**真适配器** + 一个记录"发出去过什么"的线路替身。"""

    name: str
    adapter: object
    conversation_id: str
    message_id: str
    #: 换掉传输层之后记下的每一次调用（``(method, path)``）。
    calls: list[tuple] = field(default_factory=list)

    def handle(self) -> MsgHandle:
        return MsgHandle(self.conversation_id, self.message_id, self.name)

    def edit(self, text: str) -> bool:
        return self.adapter.edit(self.handle(), Outbound(
            conversation_id=self.conversation_id, text=text))


def _discord_wired() -> Wired:
    from opencode_bridge.adapters.discord import DiscordAdapter

    adapter = DiscordAdapter({"bot_token": "d"}, _InertHooks())
    adapter.min_interval = 0
    wired = Wired("discord", adapter, "discord:123456789012345678", "1001")

    def recording_request(method, path, payload, *, timeout=None):
        wired.calls.append((method, path))
        return 200, {"id": wired.message_id, "channel_id": "123456789012345678"}

    wired.adapter._request = recording_request
    return wired


def _telegram_wired() -> Wired:
    from opencode_bridge.adapters.telegram import TelegramAdapter

    adapter = TelegramAdapter({"bot_token": "1:t"}, _InertHooks())
    adapter.min_interval = 0
    wired = Wired("telegram", adapter, "telegram:55", "4242")

    def recording_api(conversation_id, method, payload, *, timeout=None):
        wired.calls.append((method, conversation_id))
        return {"ok": True, "result": {"message_id": 4242}}

    wired.adapter._api = recording_api
    return wired


def _slack_wired() -> Wired:
    adapter, _ = make_slack()
    wired = Wired("slack", adapter, "slack:C1", "1700000000.000100")

    def recording_request(method, path, payload, *, timeout=None):
        wired.calls.append((method, path))
        # Slack 的成功判据是 ``ok``，不是 HTTP 状态码（见 slack.py 的注释）。
        return 200, {"ok": True, "ts": wired.message_id}

    wired.adapter._request = recording_request
    return wired


def _mattermost_wired() -> Wired:
    adapter, _ = make_mattermost_adapter()
    wired = Wired("mattermost", adapter, MATTERMOST_CHANNEL, "post1")

    def recording_request(method, path, payload=None, *, timeout=None):
        wired.calls.append((method, path))
        return 200, {"id": wired.message_id}

    wired.adapter._request = recording_request
    return wired


def _nextcloud_wired() -> Wired:
    from opencode_bridge.adapters import nextcloud as nextcloud_module

    adapter, _ = make_nextcloud_adapter()
    wired = Wired("nextcloud", adapter, NEXTCLOUD_CONVERSATION, "100")
    attach_nextcloud_stub(adapter, NextcloudStub(
        default=nextcloud_module._Resp(status=200, data=nextcloud_ocs({"id": 100}))))
    inner = adapter._request

    def recording_request(method, path, *, params=None, form=None, timeout=None):
        wired.calls.append((method, path))
        return inner(method, path, params=params, form=form, timeout=timeout)

    wired.adapter._request = recording_request
    return wired


def _matrix_wired() -> Wired:
    from opencode_bridge.adapters.matrix import MatrixAdapter

    adapter = MatrixAdapter({
        "homeserver": "https://matrix.example.com",
        "access_token": "m", "user_id": "@bot:example.com",
    }, _InertHooks())
    adapter.min_interval = 0
    wired = Wired("matrix", adapter, "matrix:!room:example.com", "$evt1")

    def recording_put(room_id, content, conversation_id):
        wired.calls.append(("replace", room_id))
        return 200, {"event_id": "$evt1"}

    def recording_send(out):
        wired.calls.append(("send", out.conversation_id))
        return MsgHandle(out.conversation_id, "$evt2", "matrix")

    adapter._put_message = recording_put
    adapter.send = recording_send
    return wired


BUILDERS = {
    "discord": _discord_wired,
    "matrix": _matrix_wired,
    "mattermost": _mattermost_wired,
    "nextcloud": _nextcloud_wired,
    "slack": _slack_wired,
    "telegram": _telegram_wired,
}


def wired(platform: str) -> Wired:
    """造一个**真适配器**，只把最底下那一层换成记录器。"""
    if platform not in BUILDERS:
        raise AssertionError(
            "%s 没有登记夹具 —— 新增可改写的平台必须在这里加一条，"
            "否则它的超限行为不受任何断言保护" % platform
        )
    return BUILDERS[platform]()


# ----------------------------------------------------------------------
# 1. 五个平台：本地拒收，**零请求**
# ----------------------------------------------------------------------
class TheFiveRealEditPlatformsRefuseLocallyTests(unittest.TestCase):
    def test_every_editable_platform_is_registered_here(self):
        """覆盖面守门：声明了 ``supports_message_edit`` 的**一个都不能漏**。"""
        editable = {
            platform for platform in registered_names()
            if adapter_class(platform)({}, _InertHooks()).supports_message_edit
        }
        self.assertEqual(
            sorted(editable),
            sorted(PLATFORMS_THAT_REFUSE + (PLATFORM_THAT_DEGRADES,)),
            "有平台声明了 supports_message_edit 却没有登记它的超限行为",
        )

    def test_an_over_limit_edit_raises_and_issues_no_request(self):
        for platform in PLATFORMS_THAT_REFUSE:
            with self.subTest(platform=platform):
                under_test = wired(platform)
                over_limit = "x" * (int(under_test.adapter.effective_max_length) + 1)

                with self.assertRaises(ValueError) as caught:
                    under_test.edit(over_limit)

                self.assertEqual(
                    under_test.calls, [],
                    "%s：超限的改写仍然上了线路：%r" % (platform, under_test.calls),
                )
                self.assertIn(platform, str(caught.exception))
                self.assertIn(str(under_test.adapter.effective_max_length),
                              str(caught.exception))

    def test_text_exactly_at_the_platform_limit_is_still_edited(self):
        """闸门是**闭区间**：恰好等于上限过得去（与流式闸门的 ``>`` 一致）。"""
        for platform in PLATFORMS_THAT_REFUSE:
            with self.subTest(platform=platform):
                under_test = wired(platform)
                at_the_limit = "x" * int(under_test.adapter.effective_max_length)

                edited = under_test.edit(at_the_limit)

                self.assertTrue(edited, "%s：恰好等于上限竟然被拒了" % platform)
                self.assertEqual(len(under_test.calls), 1,
                                 "%s：没有真的发出改写请求" % platform)

    def test_the_threshold_is_the_runtime_refined_limit_not_the_declared_one(self):
        """**唯一那个数**是 ``Adapter.effective_max_length``，而它会被服务端细化。

        Mattermost / Nextcloud 在 ``start()`` 之后把 ``message_limit`` 换成服务端
        的 ``MaxPostSize`` / ``max-length``。若闸门读的是类属性那份静态下限，收窄
        就**完全失效** —— 而收窄恰恰是超限改写真的会被构造出来的那个原因。
        """
        for platform in PLATFORMS_THAT_REFUSE:
            with self.subTest(platform=platform):
                under_test = wired(platform)
                adapter = under_test.adapter
                declared = int(adapter.max_message_length)
                refined = max(1, declared // 8)
                adapter.message_limit = refined
                self.assertEqual(int(adapter.effective_max_length), refined,
                                 "前提不成立：运行期细化没有生效")

                with self.assertRaises(ValueError):
                    under_test.edit("x" * (refined + 1))

                self.assertEqual(
                    under_test.calls, [],
                    "%s：按静态下限 %d 判，于是 %d 字符的改写被放行了"
                    % (platform, declared, refined + 1),
                )

    def test_the_declared_limit_is_never_hardcoded_in_the_guard(self):
        """闸门读那个属性，而不是抄一份数字 —— 抄的那份迟早与实现漂移。

        用源码检查：五家的 ``edit`` 里那条长度比较必须读
        ``self.effective_max_length``，而且**那个平台自己的上限数字不许出现在
        那一行上**（抄一份 2000 / 4000 就是"第二个真相源"）。
        """
        for platform in PLATFORMS_THAT_REFUSE:
            with self.subTest(platform=platform):
                module = importlib.import_module(
                    "opencode_bridge.adapters.%s" % platform)
                guard_lines = [
                    line for line in inspect.getsource(module).splitlines()
                    if "edit text too long" in line or "self.effective_max_length" in line
                ]
                self.assertTrue(
                    guard_lines,
                    "%s：edit() 里既没有闸门也没有读那个属性" % platform,
                )
                self.assertTrue(
                    any("self.effective_max_length" in line for line in guard_lines),
                    "%s：闸门没有读 effective_max_length" % platform,
                )
                self.assertNotIn(
                    str(module.MESSAGE_LIMIT),
                    "".join(guard_lines),
                    "%s：闸门里抄了平台常量 %r 而不是读那个属性"
                    % (platform, module.MESSAGE_LIMIT),
                )


# ----------------------------------------------------------------------
# 2. matrix：唯一不拒收的那一个，**并且要说得出为什么**
# ----------------------------------------------------------------------
class MatrixDegradesInsteadOfRefusingTests(unittest.TestCase):
    def test_an_over_limit_edit_is_delivered_as_a_plain_send(self):
        """它不抛 —— 而它**投递成功了**（这才是这一族该有的样子）。

        矩阵没有编辑 API，``edit()`` 自己就是一条 ``send``；它那个 4096 是我们按
        事件体 64KB 上限**自选**的保守值，所以抛异常等于拒绝投递一条 Matrix 乐意
        收下的消息 —— 那才是真的把正文弄丢。
        """
        under_test = wired(PLATFORM_THAT_DEGRADES)
        over_limit = "x" * (int(under_test.adapter.effective_max_length) + 1)

        edited = under_test.edit(over_limit)

        self.assertTrue(edited, "matrix 退化之后应当仍然算投递成功")
        self.assertEqual(
            [call[0] for call in under_test.calls], ["send"],
            "matrix 应当走一条普通 send（而不是 replace、也不是拒收）",
        )

    def test_text_within_the_limit_keeps_the_replace_fallback(self):
        """没超限时它仍然带 ``m.replace`` 的编辑语义 —— 退化不是常态。"""
        under_test = wired(PLATFORM_THAT_DEGRADES)

        edited = under_test.edit("短正文")

        self.assertTrue(edited)
        self.assertEqual([call[0] for call in under_test.calls], ["replace"])


# ----------------------------------------------------------------------
# 3. 抛出去会不会中断这一轮（这是"抛"这条契约的**前提**）
# ----------------------------------------------------------------------
class ARaisingEditCannotEscapeItsCallerTests(unittest.TestCase):
    """``ValueError`` 从 ``edit()`` 抛出来之后，**每个调用点都必须接住**。

    动态那一半（读者拿到完整答复、什么都没到达 ``dispatch`` 的调用方）在
    :class:`TheReaderStillGetsTheWholeAnswerWhenTheEditRaises` 里。
    """

    def _edit_call_sites(self) -> list[tuple[str, int, str]]:
        sites = []
        for path in sorted(PACKAGE_ROOT.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "edit"):
                    continue
                relative = path.relative_to(PACKAGE_ROOT.parent).as_posix()
                sites.append((relative, node.lineno, ast.unparse(node.func.value)))
        return sites

    def test_every_edit_call_site_exists_somewhere(self):
        """前提：这条断言盯着的调用点**一个都没漏掉**。

        少盯一个就等于这条断言在"没有调用点"时也绿 —— 与其在解析失败时报错，
        不如先确认它确实找到了四处（``commands`` 的按钮、``finalize``、
        ``edit_progress``、``cancel_turn``）。

        ⚠️ ``cancel_turn`` 是 2026-10-07 修 ``/new`` 吞掉在跑那一轮时新增的：
        它改写占位消息成「已取消」，与 :meth:`~opencode_bridge.outbound.
        OutboundSender.finalize` 一样会在 ``adapter.edit`` 抛 ``ValueError``
        （正文超限）时炸，所以它同样必须被下面那条"每个调用点都在 try 里"盯着。
        """
        self.assertEqual(
            sorted({site[0] for site in self._edit_call_sites()}),
            ["opencode_bridge/commands.py", "opencode_bridge/outbound.py"],
            "``adapter.edit(`` 的调用点变了：要么新增了一处（要给它加断言），"
            "要么少了一处（这条断言盯错地方了）",
        )
        self.assertEqual(len(self._edit_call_sites()), 4)

    def test_every_call_site_is_inside_a_try_that_catches(self):
        sites = {}
        for path in sorted(PACKAGE_ROOT.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "edit"):
                    continue
                sites[(str(path), node.lineno)] = _handler_names(tree, node)

        for (path, lineno), handlers in sorted(sites.items()):
            with self.subTest(call_site="%s:%d" % (path, lineno)):
                self.assertTrue(
                    handlers,
                    "这个 ``adapter.edit(`` 不在任何 ``try`` 里 —— 适配器抛出的 "
                    "ValueError 会一路穿出去，整轮收尾被中断",
                )
                self.assertTrue(
                    "ValueError" in handlers or "Exception" in handlers,
                    "这个 ``try`` 只接 %r，接不住适配器为「正文超限」抛的 ValueError"
                    % (handlers,),
                )


def _handler_names(tree: ast.AST, call: ast.Call) -> list[str]:
    """`call` 所在的那个 ``try`` 接了哪些类型（没有则空表）。"""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for statement in node.body:
            if not any(inner is call for inner in ast.walk(statement)):
                continue
            return [
                handler.type.id if isinstance(handler.type, ast.Name)
                else ast.unparse(handler.type) if handler.type else "bare"
                for handler in node.handlers
            ]
    return []


# ----------------------------------------------------------------------
# 4. 抛出去之后读者仍然拿到完整答复（真桥 + 真适配器）
# ----------------------------------------------------------------------
class TheReaderStillGetsTheWholeAnswerWhenTheEditRaises(unittest.TestCase):
    """跑**真桥**：一个真适配器在收尾那一步抛 ``ValueError``。

    判据是读者拿到的东西，不是"有没有抛异常" ——
    收尾那条路接住 ``ValueError`` 之后会补发读者还没读到的那几段。
    """

    #: 占位消息显示到哪为止（真前缀），以及服务端随后把上限收窄到多少。
    SHOWN_CHARS = 1500
    NARROWED_LIMIT = 1000

    def test_the_answer_arrives_exactly_once_on_every_platform_that_refuses(self):
        """**这就是传播那一半**：抛出去的 ``ValueError`` 被收尾接住，读者不受影响。

        判据是那行 WARNING —— 它证明"抛了、并且被接住了"；而读者视角证明接住之后
        补发的那几段恰好是读者没读到的那几段。

        ⚠️ 刻意**不**断言"有一次失败的改写"：闸门在请求发出之前就抛了，所以那条改写
        压根没变成一次投递 —— 在投递记录里找它是找不到的（那正是本文件要的"零请求"）。
        """
        for platform in PLATFORMS_THAT_REFUSE:
            with self.subTest(platform=platform):
                answer = answer_of_length(4000)
                under_test = build_platform(platform)
                _make_the_wire_refuse_over_limit(under_test)

                with self.assertLogs("opencode_bridge.outbound",
                                     level="WARNING") as captured:
                    run_one_turn(
                        under_test, answer,
                        delivered_deltas=[(0, answer[:self.SHOWN_CHARS]),
                                          (1, answer[self.SHOWN_CHARS:])],
                        # 只放行第一帧 ⇒ 占位消息停在 1500 字符的**前缀**上。
                        edit_interval_seconds=100.0,
                        # 服务端在两拍之间收窄上限 ⇒ 收尾的下界把 1500 放回去 ⇒ 超限。
                        between_deltas_and_finalize=lambda core: setattr(
                            core.adapters[0], "message_limit", self.NARROWED_LIMIT),
                    )

                self.assertIn(
                    "final edit rejected (%d chars)" % self.SHOWN_CHARS,
                    "\n".join(captured.output),
                    "%s：收尾那次超限改写没有抛、也没有被记下来 —— "
                    "要么闸门没生效，要么没人接住它" % platform,
                )
                self.assertEqual(
                    reader_view(under_test), answer,
                    "%s：读者读到的不是完整原文（丢了 / 重复了 / 乱序了）" % platform,
                )
                self.assertEqual(
                    under_test.left_showing_the_placeholder(), [],
                    "%s：留下了僵尸占位气泡" % platform,
                )


def _make_the_wire_refuse_over_limit(under_test) -> None:
    """把最底下那层换成"真的按平台上限拒收"，判据才是真的。

    传输层接缝有**两种**形状：``_request``（REST）与 telegram 的 ``_api``；而
    payload 里的正文键名又分三种（``content`` / ``message`` / ``text``）。所以按
    键名依次取，取到哪个算哪个 —— 断言要的是"这条请求有没有发出去"。
    """
    adapter = under_test.adapter

    def body_of(payload) -> str:
        payload = payload or {}
        return next(
            (str(payload[key]) for key in ("content", "message", "text")
             if payload.get(key)), "")

    if hasattr(adapter, "_api"):        # telegram
        inner_api = adapter._api

        def refusing_api(conversation_id, method, payload, *, timeout=None):
            if len(body_of(payload)) > int(adapter.effective_max_length):
                return {"ok": False, "error_code": 400,
                        "description": "Bad Request: message is too long"}
            return inner_api(conversation_id, method, payload, timeout=timeout)

        adapter._api = refusing_api
        return

    inner_request = adapter._request

    def refusing_request(method, path, *args, **kwargs):
        # 正文可能按位置传（discord / slack / mattermost）也可能按 ``form=`` 传
        # （nextcloud），所以两种都看一眼。
        body = body_of(kwargs.get("form") or kwargs.get("payload")
                       or (args[0] if args else None))
        if len(body) > int(adapter.effective_max_length):
            return 400, {"code": 50035, "message": "Invalid Form Body"}
        return inner_request(method, path, *args, **kwargs)

    adapter._request = refusing_request


# ----------------------------------------------------------------------
# 5. 被否掉的另一种契约：把"退化成 send"推广到那五家的**代价**
# ----------------------------------------------------------------------
class TheDegradeAlternativeWouldDuplicateTheReaderTests(unittest.TestCase):
    """**决定的可证伪记录**：把 matrix 的做法推广到真改写平台，读者会把那段读两遍。

    这条不是"期望出错"，而是把**被否掉的方案**的代价**量出来**放在测试里 ——
    免得下一个读代码的人只看到"六家不一致"，就顺手把它抹平。
    """

    SHOWN_CHARS = 1500
    NARROWED_LIMIT = 1000

    def _graft_matrix_style_degrade(self, adapter) -> None:
        real_edit = adapter.edit

        def degrading_edit(handle, out):
            if len(out.text) > adapter.effective_max_length:
                return adapter.send(Outbound(
                    conversation_id=handle.conversation_id, text=out.text,
                    kind=out.kind, session_id=out.session_id,
                )) is not None
            return real_edit(handle, out)

        adapter.edit = degrading_edit

    def test_degrading_duplicates_what_the_placeholder_already_shows(self):
        answer = answer_of_length(4000)
        shipped = build_platform("mattermost")
        grafted = build_platform("mattermost")
        self._graft_matrix_style_degrade(grafted.adapter)
        _make_the_wire_refuse_over_limit(grafted)

        for under_test in (shipped, grafted):
            run_one_turn(
                under_test, answer,
                delivered_deltas=[(0, answer[:self.SHOWN_CHARS]),
                                  (1, answer[self.SHOWN_CHARS:])],
                edit_interval_seconds=100.0,
                between_deltas_and_finalize=lambda core: setattr(
                    core.adapters[0], "message_limit", self.NARROWED_LIMIT),
            )

        self.assertEqual(
            reader_view(shipped), answer,
            "前提不成立：本地拒收超限改写的那条路没有把完整原文交给读者",
        )
        grafted_view = reader_view(grafted)
        self.assertEqual(
            len(grafted_view) - len(answer), self.SHOWN_CHARS,
            "退化之后读者应当**多读到**占位消息上已经显示的那 %d 个字符；"
            "实际多读了 %d 个（多半是占位消息本身被拒了，那更要紧）"
            % (self.SHOWN_CHARS, len(grafted_view) - len(answer)),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

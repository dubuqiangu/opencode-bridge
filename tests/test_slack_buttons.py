"""D3 — Slack 交互按钮（Block Kit actions）：出站 blocks / 入站 block_actions / answer。

行为契约镜像 telegram（本仓库唯一的先例实现）：

- **出站**：按钮只骑 :meth:`SlackAdapter.edit`（与 telegram「菜单先发文本、再
  edit 挂键盘」同形）；无按钮的 edit **wire 形状一个字节都不动**（进度改写全部
  走这条路，这里回归 = telegram 那句「一定要保证能用」在 slack 上的同型风险）；
  超限按钮**逐个放弃 + WARNING、绝不截断**（拍板纪律照抄 telegram
  ``callback_data`` 超限 ``912dbb1``：静默截断 = 点了却触发另一个动作）；
  全部被拒 ⇒ 不下发空 ``actions`` block。
- **入站**：``block_actions`` ⇒ ① ``Inbound(kind="callback")`` → ② ``on_callback``
  （finally 契约：投递抛异常也必须触发回调）；授权闸门最前、被丢弃的信封**仍先
  ack**（ack 铁律，迁移时最易丢的那条）；畸形载荷只记形状、不记载荷。
- **反馈**：:meth:`SlackAdapter.answer` = ``chat.postEphemeral``（Slack 版的
  ``answerCallbackQuery(text)``）——``ok:false`` 必须留 WARNING（纪律照抄 telegram
  answer 拍板 ``63d7434``：反馈通道静默失效 = 用户点了按钮却永远不知道结果）。
"""

import json
import unittest

from opencode_bridge.adapters import slack as slack_mod
from opencode_bridge.adapters.slack import SlackAdapter
from opencode_bridge.hooks import Button, MsgHandle, Outbound
from tests.test_slack import RecordingHooks, ScriptedWS, make_slack

CLICK_TEXT = "setup:slack"
CONVERSATION = "slack:C1"


# ----------------------------------------------------------------------
# 夹具
# ----------------------------------------------------------------------
class RequestRecorder:
    """替换 ``_request``：记录每次调用的 (method, path, body)，返回可配置的 ok。"""

    def __init__(self, ok=True, error="fail", raises=None):
        self.calls: list[tuple[str, str, dict]] = []
        self.ok = ok
        self.error = error
        self.raises = raises

    def __call__(self, method, path, body, *, timeout=None):
        self.calls.append((method, path, body))
        if self.raises is not None:
            raise self.raises
        return (200, {"ok": True} if self.ok
                else {"ok": False, "error": self.error})


class AckSnapshotHooks(RecordingHooks):
    """投递 inbound 的**当下**拍下 WS 已发送内容 —— 断言「ack 先于分发」。"""

    def __init__(self, ws):
        super().__init__()
        self.ws = ws
        self.snapshots: list[list[str]] = []

    def on_inbound(self, inbound) -> None:
        self.snapshots.append(list(self.ws.sent))
        super().on_inbound(inbound)


def block_actions_envelope(env_id="b1", data=CLICK_TEXT, channel="C1",
                           user="U1", actions=None, with_channel=True):
    """构造一条 Socket Mode 的 ``block_actions`` 信封（payload 即事件体）。"""
    if actions is None:
        actions = [{"action_id": data, "value": data, "block_id": "bk1"}]
    payload = {
        "type": "block_actions",
        "actions": actions,
        "user": {"id": user},
        "message": {"ts": "111.222"},
        "trigger_id": "trigger-1",
    }
    if with_channel:
        payload["channel"] = {"id": channel}
    return json.dumps(
        {"envelope_id": env_id, "type": "interactive", "payload": payload}
    )


def menu_outbound(buttons):
    return Outbound(
        conversation_id=CONVERSATION,
        text="选择平台",
        kind="text",
        buttons=tuple(buttons),
    )


# ----------------------------------------------------------------------
# 出站：edit 附 blocks；无按钮 wire 形状零变化；超限报错拒绝
# ----------------------------------------------------------------------
class TestOutboundButtons(unittest.TestCase):

    def setUp(self):
        self.hooks = RecordingHooks()
        self.adapter, _ = make_slack(hooks=self.hooks)
        self.recorder = RequestRecorder()
        self.adapter._request = self.recorder
        self.handle = MsgHandle(
            conversation_id=CONVERSATION, message_id="111.222", platform="slack"
        )

    def test_edit_attaches_buttons_as_blocks(self):
        """/setup 的第二个动作：edit 挂按钮 ⇒ chat.update 带 section+actions。"""
        ok = self.adapter.edit(
            self.handle, menu_outbound([Button("Slack", CLICK_TEXT)])
        )
        self.assertTrue(ok)
        method, path, body = self.recorder.calls[0]
        self.assertEqual((method, path), ("POST", "chat.update"))
        # text 必须保留：blocks 的 fallback 与通知都靠它。
        self.assertEqual(body["text"], "选择平台")
        self.assertEqual(body["ts"], "111.222")
        self.assertEqual(body["channel"], "C1")
        section_block, actions_block = body["blocks"]
        self.assertEqual(section_block["type"], "section")
        self.assertEqual(actions_block["type"], "actions")
        button_element, = actions_block["elements"]
        self.assertEqual(button_element["action_id"], CLICK_TEXT)
        self.assertEqual(button_element["value"], CLICK_TEXT)
        self.assertEqual(button_element["text"],
                         {"type": "plain_text", "text": "Slack"})

    def test_edit_without_buttons_keeps_the_legacy_wire_shape(self):
        """进度改写（无按钮）的请求体必须与实现前逐字节同形 —— 没有 blocks 键。

        这条红的条件：有人把 blocks 无条件下发（或 fallback text 被动过）——
        那会改变全部进度消息的渲染路径，是本文件最重要的回归守卫。
        """
        ok = self.adapter.edit(self.handle, menu_outbound([]))
        self.assertTrue(ok)
        method, path, body = self.recorder.calls[0]
        self.assertEqual((method, path), ("POST", "chat.update"))
        self.assertEqual(body, {"channel": "C1", "ts": "111.222",
                                "text": "选择平台"})

    def test_send_never_attaches_blocks(self):
        """按钮只骑 edit（与 telegram「send 只带文本」同一契约）。"""
        self.adapter.send(menu_outbound([Button("Slack", CLICK_TEXT)]))
        for method, path, body in self.recorder.calls:
            self.assertNotIn("blocks", body,
                             f"send 路径 ({path}) 不许带 blocks")

    def test_over_limit_data_button_is_rejected_not_truncated(self):
        """data 超 action_id 上限（255）⇒ 逐个放弃 + WARNING，绝不截断。

        红的条件：出现任何截断形状（前缀进了 payload）或超限按钮静默下发 ——
        正是 telegram ``callback_data`` 那条缺陷的本体（点击回来的 data 与
        按钮绑定的不是同一个值）。
        """
        over_limit_data = "x" * (slack_mod.SLACK_ACTION_ID_LIMIT + 1)
        with self.assertLogs("opencode_bridge.adapters.slack",
                             level="WARNING") as caught:
            ok = self.adapter.edit(self.handle, menu_outbound([
                Button("坏按钮", over_limit_data),
                Button("Slack", CLICK_TEXT),
            ]))
        self.assertTrue(ok)
        self.assertTrue(any("exceeds limit" in line for line in caught.output),
                        f"WARNING 必须点名超限：{caught.output}")
        actions_block = self.recorder.calls[0][2]["blocks"][1]
        self.assertEqual([el["action_id"] for el in actions_block["elements"]],
                         [CLICK_TEXT],
                         "超限按钮必须被整个放弃，合法按钮照常下发")
        self.assertNotIn(over_limit_data[:slack_mod.SLACK_ACTION_ID_LIMIT],
                         json.dumps(self.recorder.calls[0][2]),
                         "截断前缀绝不许进 payload")

    def test_over_limit_label_button_is_rejected(self):
        """label 超按钮文本上限（75）⇒ 放弃 + WARNING。"""
        over_limit_label = "L" * (slack_mod.SLACK_BUTTON_LABEL_LIMIT + 1)
        with self.assertLogs("opencode_bridge.adapters.slack",
                             level="WARNING"):
            ok = self.adapter.edit(self.handle, menu_outbound([
                Button(over_limit_label, CLICK_TEXT),
            ]))
        self.assertTrue(ok)
        self.assertNotIn("blocks", self.recorder.calls[0][2],
                         "唯一的按钮被拒 ⇒ 不许下发空 actions block")

    def test_empty_data_button_is_dropped(self):
        """空 data 按钮放弃（action_id 是必填字段，空串会被 Slack 拒收）。"""
        with self.assertLogs("opencode_bridge.adapters.slack",
                             level="WARNING"):
            self.adapter.edit(self.handle, menu_outbound([
                Button("空按钮", ""),
            ]))
        self.assertNotIn("blocks", self.recorder.calls[0][2])


# ----------------------------------------------------------------------
# 入站：block_actions → ① Inbound(kind=callback) → ② on_callback
# ----------------------------------------------------------------------
class TestInboundBlockActions(unittest.TestCase):

    def setUp(self):
        self.hooks = RecordingHooks()
        self.adapter, _ = make_slack({"allowed_chat_ids": ["C1"]},
                                     hooks=self.hooks)

    def test_click_delivers_callback_inbound_then_on_callback(self):
        """顺序契约：先投递（记录/去重侧）再回调（处理侧），data 逐字节透传。"""
        ws = ScriptedWS([])
        self.assertTrue(self.adapter._handle_envelope(
            ws, block_actions_envelope()))
        self.assertEqual(len(self.hooks.inbounds), 1)
        inbound = self.hooks.inbounds[0]
        self.assertEqual(inbound.kind, "callback")
        self.assertEqual(inbound.text, CLICK_TEXT)
        self.assertEqual(inbound.conversation_id, CONVERSATION)
        self.assertEqual(inbound.user_id, "U1")
        self.assertEqual(inbound.message_id, "111.222")
        self.assertEqual(inbound.callback_query_id, "U1:C1")
        self.assertEqual(self.hooks.callbacks,
                         [(CONVERSATION, CLICK_TEXT, "U1:C1")])
        self.assertEqual([e[0] for e in self.hooks.events],
                         ["inbound", "callback"],
                         "必须先投递、后回调（finally 契约的顺序一半）")

    def test_click_envelope_is_acked_before_dispatch(self):
        """ack 铁律延伸到交互载荷：投递发生时 ack 已在 WS 发送缓冲里。"""
        ws = ScriptedWS([])
        hooks = AckSnapshotHooks(ws)
        adapter, _ = make_slack({"allowed_chat_ids": ["C1"]}, hooks=hooks)
        self.assertTrue(adapter._handle_envelope(ws, block_actions_envelope()))
        self.assertEqual(hooks.snapshots,
                         [[json.dumps({"envelope_id": "b1"})]],
                         "投递的当下必须已经 ack（漏 ack = Slack 无限重发）")

    def test_click_from_non_whitelisted_channel_is_dropped_but_acked(self):
        """授权闸门对按钮同样最前：未授权频道的点击零投递零回调，但信封已 ack。"""
        adapter, _ = make_slack({"allowed_chat_ids": ["C_allow"]},
                               hooks=self.hooks)
        ws = ScriptedWS([])
        with self.assertLogs("opencode_bridge.adapters.slack",
                             level="INFO") as caught:
            self.assertTrue(adapter._handle_envelope(
                ws, block_actions_envelope(channel="C_other")))
        self.assertEqual(self.hooks.inbounds, [])
        self.assertEqual(self.hooks.callbacks, [])
        self.assertEqual(ws.acks, [{"envelope_id": "b1"}])
        self.assertTrue(any("non-whitelisted" in line for line in caught.output))

    def test_malformed_payload_logs_shape_and_is_ignored(self):
        """无 actions / 无 channel 的载荷：只记键名形状、零投递、零崩溃、已 ack。"""
        for raw in (
            block_actions_envelope(actions=[]),
            block_actions_envelope(with_channel=False),
            block_actions_envelope(actions=[{"block_id": "bk"}]),
        ):
            with self.subTest(raw=raw[:60]):
                ws = ScriptedWS([])
                with self.assertLogs("opencode_bridge.adapters.slack",
                                     level="INFO"):
                    self.assertTrue(self.adapter._handle_envelope(ws, raw))
                self.assertEqual(ws.acks, [{"envelope_id": "b1"}])
        self.assertEqual(self.hooks.inbounds, [])
        self.assertEqual(self.hooks.callbacks, [])

    def test_on_inbound_failure_still_triggers_on_callback(self):
        """finally 契约：投递抛异常，回调也必须被触发且异常不外溢。"""

        class ExplodingInboundHooks(RecordingHooks):
            def on_inbound(self, inbound) -> None:
                raise RuntimeError("投递侧炸了")

        hooks = ExplodingInboundHooks()
        adapter, _ = make_slack({"allowed_chat_ids": ["C1"]}, hooks=hooks)
        ws = ScriptedWS([])
        self.assertTrue(adapter._handle_envelope(ws, block_actions_envelope()))
        self.assertEqual(hooks.callbacks,
                         [(CONVERSATION, CLICK_TEXT, "U1:C1")],
                         "on_callback 在 finally 里，投递炸了也要触发")

    def test_value_field_carries_data_when_action_id_differs(self):
        """data 从 value 优先读取（兼容只在 value 携带载荷的合法 Block Kit 形状）。"""
        ws = ScriptedWS([])
        raw = block_actions_envelope(
            actions=[{"action_id": "ignored", "value": CLICK_TEXT}])
        self.assertTrue(self.adapter._handle_envelope(ws, raw))
        self.assertEqual(self.hooks.inbounds[0].text, CLICK_TEXT)


# ----------------------------------------------------------------------
# 反馈：answer = chat.postEphemeral
# ----------------------------------------------------------------------
class TestAnswerFeedback(unittest.TestCase):

    def setUp(self):
        self.hooks = RecordingHooks()
        self.adapter, _ = make_slack(hooks=self.hooks)
        self.recorder = RequestRecorder()
        self.adapter._request = self.recorder

    def test_answer_posts_ephemeral_to_the_clicker(self):
        """query_id 的 user:channel 编码 ⇒ 只对点击者可见的一条反馈。"""
        self.adapter.answer("U1:C1", "已打开接入引导")
        method, path, body = self.recorder.calls[0]
        self.assertEqual((method, path), ("POST", "chat.postEphemeral"))
        self.assertEqual(body, {"channel": "C1", "user": "U1",
                                "text": "已打开接入引导"})

    def test_answer_ok_false_leaves_a_warning_never_raises(self):
        """ok:false 必须 WARNING 点名 query_id（telegram ``63d7434`` 同型拍板）。"""
        self.adapter._request = RequestRecorder(ok=False, error="not_allowed")
        with self.assertLogs("opencode_bridge.adapters.slack",
                             level="WARNING") as caught:
            self.adapter.answer("U1:C1", "已处理")  # 不许抛
        self.assertTrue(any("U1:C1" in line and "not_allowed" in line
                            for line in caught.output),
                        f"WARNING 必须点名 query_id 与错误：{caught.output}")

    def test_answer_transport_failure_is_a_warning_never_raises(self):
        self.adapter._request = RequestRecorder(raises=OSError("网络没了"))
        with self.assertLogs("opencode_bridge.adapters.slack",
                             level="WARNING"):
            self.adapter.answer("U1:C1", "已处理")  # 不许抛

    def test_answer_with_empty_text_is_a_no_op(self):
        """信封 ack 已停交互指示；空反馈没有可展示内容，不该发空 ephemeral。"""
        self.adapter.answer("U1:C1", "")
        self.adapter.answer("U1:C1")
        self.assertEqual(self.recorder.calls, [])

    def test_answer_with_malformed_query_id_is_skipped_safely(self):
        """解析不出 user:channel 的 query_id（外来调用/旧值）安静放弃，不算错误。"""
        with self.assertLogs("opencode_bridge.adapters.slack", level="INFO"):
            self.adapter.answer("garbage", "已处理")
            self.adapter.answer(":C1", "已处理")
            self.adapter.answer("U1:", "已处理")
        self.assertEqual(self.recorder.calls, [])


# ----------------------------------------------------------------------
# 能力声明
# ----------------------------------------------------------------------
class TestCapabilityFlag(unittest.TestCase):

    def test_supports_inline_buttons_is_true(self):
        """能力位必须为 True —— 它驱动 /setup 菜单与 channel_profile 的
        「此频道可显示按钮」提示；翻回 False = slack 用户的 /setup 悄悄降级
        成纯文本菜单，而没有任何测试会红（test_adapters 只查 capabilities
        字典的那条除外）。
        """
        self.assertTrue(SlackAdapter.supports_inline_buttons)
        adapter, _ = make_slack()
        self.assertTrue(adapter.capabilities()["supports_inline_buttons"])


if __name__ == "__main__":
    unittest.main()

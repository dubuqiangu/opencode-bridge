"""占位消息**改不动**时的三个残留 —— 每一条都跑**真桥 + 真适配器**。

:mod:`tests.test_progress_placeholder` 已覆盖"改写失败但占位消息已经显示着一段正文"
这一路。剩下三件事它没有覆盖，而它们各自有各自的机制，所以这里各钉一条。

## 一、乱序 delta 让"冻结的那一段"不再是前缀

:meth:`~opencode_bridge.event_stream.Turn.assemble` 按 ``ordinal`` 排序拼接，
而 delta **可能乱序到达**（``parts`` 的注释就是这么写的）。于是流式闸门某一次
发布出去的正文可能是 ``final[300:400]`` —— 落在中间的偏移上。而
:meth:`~opencode_bridge.outbound.OutboundSender.finalize` 此前只认"前缀"，
判不过就**整段重发**：读者先把第 4 段读了一遍，再从第 1 段重读全文。
**实测（修好之前）：400 字的答复被读成 500 字。**

判据落在**读者真正拿到的东西**上：``reader_view`` 是每条消息**最后一次**写入的
文本按顺序拼起来。它必须**逐字节等于原文**（这一条**做不到**，见下）或者至少
**每个字符恰好出现一次**（这一条做得到）。

⚠️ **能保住哪一条、保不住哪一条，是量出来的，不是选的**：

* **每字恰好一次** —— 保住了。冻结的那一段在 ``final`` 里**恰好出现一次**时，
  偏移是**判定出来的**（不是猜的），于是只补发它前后两段。
* **按序等于原文** —— **保不住**，而且**不可判定**：冻结的那一段已经被读者按它
  自己的样子读过了，而那条消息改不动、平台也没有「删除 / 撤回」这个原语，
  于是已经印出去的字既挪不动也抹不掉。把这一条钉死成"必须等于原文"就是把一条
  **不可能**的契约写进测试。

## 二、一次 ``edit()`` 都没成功过 ⇒ ``⏳ 处理中…`` 永远留在那儿

**有界残留，不修**。清掉它需要平台提供**删除消息**的原语，本仓库没有这个能力
声明（见 ``Adapter.supports_message_edit`` 的说明），也不打算凭空发明一个。
本文件把它**钉成现状**：答复**恰好一次**到达（这一条是承诺，必须守），而那个气泡
留在那里（这一条是残留，必须记）。七个不可改写的平台压根没有这条路 ——
:meth:`~opencode_bridge.outbound.OutboundSender.send_text` 的
``kind == "progress"`` 闸门不让他们发占位消息。

## 三、Discord 的 ``edit`` 以前没有长度闸

同一种失败（正文超平台上限），两个平台表达得不一样：Telegram **本地**抛
``ValueError``（一个请求都不发），Discord 把注定失败的 ``PATCH`` **发出去**、
收 400、再返回 ``False``。调用方只能靠**猜**哪条是哪条。

## 覆盖漏洞（已先修）

:func:`tests.test_progress_placeholder._discord` 的假 ``_request`` 曾经对**任何
长度**的 PATCH 都回 200，于是"改写失败"这条路径**从来没被真正走到过** —— 那份
文件里所有关于"改不动"的断言都靠人工制造失败，而不是靠平台真的拒收。本文件用
自己的 ``_request`` 替身（真的会回 400）驱动真实路径，不依赖那个漏洞。
"""

from __future__ import annotations

import unittest

from opencode_bridge.adapters.discord import MESSAGE_LIMIT as DISCORD_MESSAGE_LIMIT
from opencode_bridge.adapters.discord import DiscordAdapter
from opencode_bridge.hooks import MsgHandle, Outbound

#: 桥往 IM 一条消息里能写多少 —— 取桥的预算与平台上限的**小者**，与
#: :func:`~opencode_bridge.outbound.one_message_budget` 同一个数。
#: 从**实现**里 import 而不是抄一遍 2000：抄的那份迟早与实现漂移。
from opencode_bridge.outbound import one_message_budget

from tests.test_progress_placeholder import (
    BRIDGE_MAX_MESSAGE_CHARS,
    PLATFORMS_WITH_MESSAGE_EDIT,
    PlatformUnderTest,
    _InertHooks,
    build_platform,
    reader_view,
    run_one_turn,
)


def answer_whose_every_part_is_unique(total: int) -> str:
    """``total`` 个字符，且**任何子串在它里面最多只出现一次**。

    为什么不能用 :func:`tests.test_progress_placeholder.answer_of_length`：那一条
    按固定填充串循环生成，同一段文字会在答复里**重复出现**若干次，于是"这一段在
    原文的哪个偏移"**不唯一** —— 而"不唯一就不猜"正是被测规则之一，用它当输入
    会让这条用例退化成测第三种情形。
    """
    return "".join("%05d|" % index for index in range(total // 6 + 2))[:total]


def quarters(answer: str, count: int) -> list[tuple[int, str]]:
    """把答复等分成 ``count`` 段，返回 ``[(ordinal, 那一段), ...]``。"""
    width = len(answer) // count
    return [
        (index, answer[index * width:(index + 1) * width])
        for index in range(count)
    ]


# ----------------------------------------------------------------------
# 一、乱序 delta：冻结的那一段不再是前缀
# ----------------------------------------------------------------------
class OutOfOrderDeltasTests(unittest.TestCase):
    """只有**第一帧**发布得出去（节流窗口 100 秒），随后收尾那次改写失败。

    为什么用节流而不是多注入一次失败：生产的默认窗口是 1.5 秒，一次乱序就足以
    让闸门只发布一帧 —— 而**这一帧发布的是第 4 段**，因为 ordinal 3 先到。
    所以"冻结的那一段落在末尾偏移上"这个前提**不需要任何故障**就成立。
    """

    #: 节流窗口：第一帧之后的所有帧都被挡下。它**不会**真的等 100 秒 —— 节流比的是
    #: 挂钟，而这一轮在几毫秒内就跑完了。
    THROTTLE_SECONDS = 100.0
    PART_COUNT = 4

    def run_turn_with(
        self, delivery_plan: list[tuple[int, str]], answer: str,
    ) -> PlatformUnderTest:
        under_test = build_platform("discord")
        # fail_edits_from=2 ⇒ 第 1 次改写（流式那一帧）**成功**、第 2 次（收尾）失败。
        # 不这样钉住前提的话，"占位消息已经显示着一段正文"就不成立。
        under_test.fail_edits_from = 2
        run_one_turn(
            under_test, answer,
            delivered_deltas=delivery_plan,
            edit_interval_seconds=self.THROTTLE_SECONDS,
        )
        return under_test

    def test_the_premise_really_holds_the_prefix_is_broken(self):
        """**前提**：唯一发出去的那一帧落在一个**非零偏移**上，且该偏移可判定。

        没有这条，下面那条断言就可能是"因为别的理由"绿的。
        """
        answer = answer_whose_every_part_is_unique(400)
        plan = list(reversed(quarters(answer, self.PART_COUNT)))

        under_test = self.run_turn_with(plan, answer)

        shown = self.frozen_text(under_test)
        self.assertEqual(shown, answer[300:400],
                         "前提不成立：唯一发布的那一帧不是第 4 段")
        self.assertFalse(answer.startswith(shown),
                         "前提不成立：冻结的那一段仍然是前缀 —— 那条路没被测到")
        self.assertEqual(answer.count(shown), 1,
                         "前提不成立：这一段在原文里出现多次，偏移不可判定")

    def test_every_character_reaches_the_reader_exactly_once(self):
        """读者拿到的**每一个字符**恰好一次 —— 没有重复，也没有丢。

        修好之前这里是 ``reader_view`` 500 字对 400 字原文：冻结的第 4 段被读了两遍
        （一遍在占位气泡上、一遍在重发的整段里）。
        """
        answer = answer_whose_every_part_is_unique(400)
        plan = list(reversed(quarters(answer, self.PART_COUNT)))

        under_test = self.run_turn_with(plan, answer)

        view = reader_view(under_test)
        self.assertEqual(
            sorted(view), sorted(answer),
            "读者拿到的字符集合与原文不同：%d 字对 %d 字"
            % (len(view), len(answer)),
        )
        self.assertEqual(
            len(view), len(answer),
            "读者读到的长度与原文不同（重复或丢失）：%d 对 %d" % (len(view), len(answer)),
        )

    def test_the_messages_the_reader_receives_are_named_exactly(self):
        """**逐条**说出读者拿到什么 —— 只断言"每字一次"会放过乱序（见下）。"""
        answer = answer_whose_every_part_is_unique(400)
        plan = list(reversed(quarters(answer, self.PART_COUNT)))

        under_test = self.run_turn_with(plan, answer)

        self.assertEqual(
            self.messages_the_reader_reads(under_test),
            [answer[300:400], answer[:300]],
            "读者应当依次读到：冻结在气泡上的第 4 段，然后是它前面那三段",
        )

    def test_the_same_turn_with_in_order_deltas_still_reads_in_order(self):
        """**对照组**：投递顺序正常时，读者连着读下来**逐字节等于原文**。

        这条与上一条成对：它证明"顺序"那条契约在可达的时候**确实兑现**，所以
        "乱序时顺序不可判定"是那条路径的性质，而不是本方法把顺序也弄坏了。
        """
        answer = answer_whose_every_part_is_unique(400)

        under_test = self.run_turn_with(quarters(answer, self.PART_COUNT), answer)

        self.assertEqual(reader_view(under_test), answer,
                         "按序投递时读者读到的不是完整原文")
        self.assertEqual(under_test.left_showing_the_placeholder(), [],
                         "按序投递时留下了僵尸占位气泡")

    def frozen_text(self, under_test) -> str:
        """占位气泡**最后**显示的那一截。"""
        identity = under_test.progress_sends()[0].identity
        return under_test.identities()[identity][-1]

    def messages_the_reader_reads(self, under_test) -> list[str]:
        """读者逐条读到的内容（每条消息最后一次写入的文本，按出现顺序）。"""
        order: list[str] = []
        for delivery in under_test.deliveries:
            if delivery.identity not in order:
                order.append(delivery.identity)
        written = under_test.identities()
        return [written[identity][-1] for identity in order]


# ----------------------------------------------------------------------
# 二、一次 edit() 都没成功过 → ⏳ 处理中… 留在那里（有界残留，钉成现状）
# ----------------------------------------------------------------------
class NoWriteEverLandedTests(unittest.TestCase):
    """**有界残留**：占位消息一条 ``edit()`` 都没成功过时的现状。

    为什么它只在"**声明了** ``supports_message_edit`` 却改不动"的平台上出现：
    七个恒返回 ``False`` 的平台压根**不发**占位消息
    （``OutboundSender.send_text`` 的 ``kind == "progress"`` 闸门），所以那条路上
    什么气泡都不存在。
    """

    def test_the_answer_arrives_exactly_once_even_though_the_bubble_is_stuck(self):
        """**承诺那一条**：读者仍然拿到完整答复，逐字节，一个字符都不多不少。

        残留只影响那个气泡 —— :meth:`OutboundSender.finalize` 在
        ``shown_progress_text`` 为空时知道"读者什么也没读过"，于是整段发出去。
        """
        for name in PLATFORMS_WITH_MESSAGE_EDIT:
            with self.subTest(platform=name):
                under_test = build_platform(name)
                answer = answer_whose_every_part_is_unique(400)
                # 流式一帧都不发得出去（窗口极大），收尾那次改写失败。
                under_test.fail_edits_from = 1
                run_one_turn(
                    under_test, answer,
                    delivered_deltas=[(0, answer)],
                    edit_interval_seconds=10 ** 9,
                )

                self.assertEqual(
                    len(under_test.identities()), 2,
                    "%s：应当恰好两条消息（一个卡住的气泡 + 一条答复），实际 %r"
                    % (name, under_test.identities()),
                )
                self.assertEqual(
                    under_test.identities()[list(under_test.identities())[-1]][-1],
                    answer,
                    "%s：答复必须逐字节落在那一条新消息上" % name,
                )
                self.assertEqual(
                    under_test.wire_text()[-1], under_test.normalise_wire(answer),
                    "%s：线路上最后一段必须就是完整原文" % name,
                )

    def test_the_bubble_is_left_showing_the_placeholder_and_nothing_can_clear_it(self):
        """**残留那一条**：气泡停在 ``⏳ 处理中…``，而本仓库**没有**清掉它的手段。

        判据是**两半**：① 读者真的读到了这个僵尸（不然整条用例是空的）；
        ② 桥确实没有可用的清除原语（不然它就该被修掉而不是被记成残留）。
        """
        from opencode_bridge.adapters.base import Adapter

        for name in PLATFORMS_WITH_MESSAGE_EDIT:
            with self.subTest(platform=name):
                under_test = build_platform(name)
                under_test.fail_edits_from = 1
                run_one_turn(
                    under_test, answer_whose_every_part_is_unique(400),
                    delivered_deltas=[(0, "x" * 400)],
                    edit_interval_seconds=10 ** 9,
                )

                stuck = under_test.left_showing_the_placeholder()
                self.assertEqual(
                    len(stuck), 1,
                    "%s：应当恰好留下一个僵尸占位气泡（0 个说明前提没成立，"
                    "2 个以上说明闸门漏了）" % name,
                )
                self.assertEqual(
                    under_test.identities()[stuck[0]][-1],
                    "⏳ 处理中…",
                    "%s：留下的气泡应当停在占位文案上" % name,
                )
                # ② 没有可用的清除原语：`Adapter` 没有 delete 之类的契约，
                # 而这一个已经失败的 `edit` 就是唯一的写入手段。
                self.assertFalse(
                    any("delete" in name_of.lower() for name_of in dir(Adapter)),
                    "%s：基类上出现了删除消息的契约 —— 那这条残留就该被修掉，"
                    "而不是继续记着" % name,
                )


# ----------------------------------------------------------------------
# 三、Discord 的 edit 缺长度闸（已补）
# ----------------------------------------------------------------------
class DiscordEditRefusesOverLimitTextLocallyTests(unittest.TestCase):
    """超限的 ``edit``：**本地**拒收（一个请求都不发），与 Telegram 同一表达。

    为什么这一条不是"洁癖"：修好之前 Discord 会把注定失败的 ``PATCH`` **发出去**
    （实测 5000 字符进了 ``PATCH .../messages/{id}``），收 400 再返回 ``False``。
    那既浪费一次往返与一次节流等待，也让调用方分不清"平台拒收"与"网络失败"。
    """

    def make_adapter_recording_requests(self) -> tuple[DiscordAdapter, list[str]]:
        adapter = DiscordAdapter({"bot_token": "d"}, _InertHooks())
        adapter.min_interval = 0
        methods: list[str] = []

        def recording_request(method, path, payload, *, timeout=None):
            methods.append("%s %d chars" % (method, len(payload.get("content", ""))))
            return 200, {"id": "1", "channel_id": "123"}

        adapter._request = recording_request
        return adapter, methods

    def test_an_over_limit_edit_raises_before_any_request_is_made(self):
        adapter, methods = self.make_adapter_recording_requests()

        with self.assertRaises(ValueError) as caught:
            adapter.edit(
                MsgHandle("discord:123", "1001", "discord"),
                Outbound(conversation_id="discord:123", text="x" * 5000),
            )

        self.assertEqual(methods, [], "超限的改写仍然发出了请求：%r" % (methods,))
        self.assertIn("5000", str(caught.exception))
        self.assertIn(str(DISCORD_MESSAGE_LIMIT), str(caught.exception))

    def test_text_exactly_at_the_platform_limit_is_not_refused(self):
        """闸门是闭区间：恰好等于上限过得去（与流式闸门的 ``>`` 一致）。"""
        adapter, methods = self.make_adapter_recording_requests()

        edited = adapter.edit(
            MsgHandle("discord:123", "1001", "discord"),
            Outbound(conversation_id="discord:123", text="x" * DISCORD_MESSAGE_LIMIT),
        )

        self.assertTrue(edited)
        self.assertEqual(methods, ["PATCH %d chars" % DISCORD_MESSAGE_LIMIT])

    def test_both_editable_platforms_express_the_same_refusal_the_same_way(self):
        """同一件事、同一句话术 —— 这正是"两个平台表达得不一样"要消灭的东西。"""
        from opencode_bridge.adapters.telegram import TelegramAdapter

        refusals = []
        for build_adapter, conversation_id, message_id, platform_limit in (
            (lambda: DiscordAdapter({"bot_token": "d"}, _InertHooks()),
             "discord:123", "1001", DISCORD_MESSAGE_LIMIT),
            (lambda: TelegramAdapter({"bot_token": "1:t"}, _InertHooks()),
             "telegram:55", "4242", 4096),
        ):
            adapter = build_adapter()
            adapter.min_interval = 0
            methods: list[str] = []
            adapter._request = (
                lambda method, path, payload, *, timeout=None: (
                    methods.append(method) or (200, {"id": "1"})
                )
            )
            adapter._api = (
                lambda conversation_id, method, payload, *, timeout=None: (
                    methods.append(method) or {"ok": True, "result": {"message_id": 1}}
                )
            )
            refusals.append((
                platform_limit, type(self.refusal(adapter, conversation_id, message_id)),
                methods,
            ))

        self.assertEqual(
            refusals,
            [(DISCORD_MESSAGE_LIMIT, ValueError, []), (4096, ValueError, [])],
            "两个平台的超限改写必须表达成同一件事：一个请求都不发的本地拒收",
        )

    def refusal(self, adapter, conversation_id: str, message_id: str) -> BaseException:
        """调一次超限 ``edit``，把它抛出的（或返回的）东西拿回来。"""
        try:
            adapter.edit(
                MsgHandle(conversation_id, message_id, adapter.name),
                Outbound(conversation_id=conversation_id,
                         text="x" * (adapter.effective_max_length + 1)),
            )
        except BaseException as exc:      # noqa: BLE001 - 就是要看它抛什么
            return exc
        return RuntimeError("edit returned instead of refusing")

    def test_the_sender_survives_it_and_the_reader_still_gets_everything(self):
        """**这条路径确实可达**，而且闸门生效时读者拿到的东西一个字都不少。

        可达性：``finalize`` 用 :func:`one_message_budget` 算预算，而 ``Adapter`` 的
        ``message_limit`` 会被**服务端在启动后细化**（Mattermost 的 ``MaxPostSize``、
        Nextcloud 的 ``max-length``）。若细化落在"一次流式写入"与"一次收尾"**之间**，
        收尾用的预算当场变小，而 ``_split_for_the_placeholder`` 那个
        ``max(len(head), len(shown_progress_text))`` 下界会把**已显示的长度**原样放
        回去 —— 一次注定超限的改写就是这么来的。

        所以这里在两拍之间把 Discord 的上限从 2000 收到 100，然后断言两件事：
        ① 线路上**没有**任何超限的改写（这一条对闸门敏感）；② 读者连着读下来
        逐字节等于原文（这一条对兜底敏感）。
        """
        under_test = build_platform("discord")
        adapter = under_test.adapter
        issued: list[tuple[str, int]] = []
        request_issued_by_discord = adapter._request

        def recording_request(method, path, payload, *, timeout=None):
            issued.append((method, len(payload.get("content", ""))))
            return request_issued_by_discord(method, path, payload, timeout=timeout)

        adapter._request = recording_request

        answer = answer_whose_every_part_is_unique(400)

        def narrow_the_limit(core) -> None:
            core.adapters[0].message_limit = 100
            issued.clear()   # 只看收尾那一步发了什么

        run_one_turn(
            under_test, answer,
            delivered_deltas=[(0, answer)],
            edit_interval_seconds=0,
            between_deltas_and_finalize=narrow_the_limit,
        )

        # 前提：收尾那一步的预算确实变小了，而下界确实把"已显示的全文"放了回去。
        self.assertEqual(int(adapter.effective_max_length), 100,
                         "前提不成立：上限没有被收窄")
        self.assertEqual(
            under_test.identities()[under_test.progress_sends()[0].identity][-1],
            answer,
            "前提不成立：占位消息在收窄之前并没有显示出全文，"
            "于是收尾那次改写装得下新上限",
        )
        self.assertEqual(
            [text for method, text in issued
             if method == "PATCH" and text > 100],
            [],
            "超限的改写仍然上了线路：%r" % (issued,),
        )
        self.assertEqual(reader_view(under_test), answer,
                         "读者读到的不是完整原文（丢了 / 重复了 / 乱序了）")
        self.assertEqual(under_test.left_showing_the_placeholder(), [],
                         "留下了僵尸占位气泡")


# ----------------------------------------------------------------------
# 四、`one_message_budget` 是唯一那个数（缺陷三的前提，不是缺陷本身）
# ----------------------------------------------------------------------
class TheBudgetIsStillTheOnlyNumberTests(unittest.TestCase):
    def test_the_refusal_threshold_is_the_platform_limit_and_not_the_bridge_budget(self):
        """闸门读的是**平台**上限（``Adapter.effective_max_length``），不是桥的预算。

        桥的预算（``BRIDGE_MAX_MESSAGE_CHARS`` = 4000）比 Discord 的 2000 大，
        两者只有读同一个数才谈得上"一致"。
        """
        adapter = DiscordAdapter({"bot_token": "d"}, _InertHooks())

        self.assertEqual(
            one_message_budget(BRIDGE_MAX_MESSAGE_CHARS, adapter),
            DISCORD_MESSAGE_LIMIT,
            "桥的预算与平台上限不再收敛到同一个数",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

"""``core.py`` 的写前收件箱**接线**测试：崩溃落在投递窗口里时，消息还在。

存储与策略的语义各自由 ``test_inbox.py`` / ``test_inbox_recovery.py`` 锁住；这里只测
接线 —— 也就是最容易出错的地方：

1. **写前落盘只在非命令分支** —— 放在 ``startswith("/")`` 之前会给每条命令留一行
   永不送达的 ``pending``，于是 ``/new`` 每次启动都重建会话、``/setup`` 每次都重发
   引导：比要修的丢消息 bug 更糟（见 :class:`CommandsAreNeverRecorded`）。
2. **崩溃窗口稳定可复现** —— 假客户端抛 ``BaseException`` 子类而不是 ``Exception``：
   core 里那些 ``except Exception``（"一条坏消息不许拖垮 SSE 线程"）会抓住
   ``Exception``，用普通异常根本走不到崩溃那条路。真崩溃是进程直接没了，
   **没有任何 handler**。两端都测：落在投递**之前**留下 ``pending`` -> 下次启动
   重放且不告警（:class:`CrashBeforeDispatchReplaysThePendingRow`）；落在
   ``prompt()`` **之中**留下 ``attempting`` -> 只告警、绝不重放
   （:class:`CrashInsidePrompt`）。
3. **明确失败会退避后重放** —— 盘上留着 ``failed`` 行、正文**原样**，下一次启动
   真的把它交到了 ``prompt()``（见 :class:`FailedPromptIsReplayedOnNextBoot`）。
4. **去重** —— 同一个 ``delivery_id`` 到达两次只投一次；平台不给 ``message_id``
   时去重键退回内容哈希，这个合并**必须记日志**，不能悄悄发生。
5. **409 不是失败** —— 不写 ``failed``、不花重试预算，写前义务原样留在盘上
   （状态细节见 :class:`BusyIsNotAFailure` 的说明）。
6. **接线本身** —— ``__main__`` 真的注入了一个非 ``None`` 的收件箱。可选注入的设计
   让其余所有测试都跑在"关闭"模式下，漏接线不会有任何一条测试变红。

零真实网络、零真实 sleep：退避阶梯在需要"立刻到期"的用例里旁路成 0，断言一律打在
**盘上的行**上（公开 API 故意不暴露 ``state``，理由见 ``test_inbox.read_inbox_rows``）。
"""

from __future__ import annotations

import os
import tempfile
import time
import types
import unittest
from unittest import mock

from opencode_bridge import __main__ as cli
from opencode_bridge import inbox as inbox_module
from opencode_bridge.channel_profile import _HINT_SEPARATOR
from opencode_bridge.config import Config
from opencode_bridge.core import BridgeCore
from opencode_bridge.hooks import Inbound
from opencode_bridge.inbox import BACKOFF_LADDER_SECONDS, InboundInbox
from opencode_bridge.opencode_client import Endpoint, OpenCodeError
from opencode_bridge.state import StateStore
from tests.test_core import FakeAdapter, FakeClient
from tests.test_inbox import read_inbox_rows

_REPOSITORY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: 仓库内的临时目录：即便 TEMP/TMP 指向机器别处，测试也不可能写到仓库之外。
_REPOSITORY_TEMP = os.path.join(_REPOSITORY_ROOT, ".tmp")


class SimulatedCrash(BaseException):
    """进程在 ``client.prompt()`` 执行期间被杀掉。

    ⚠️ 必须是 ``BaseException`` 的子类而不是 ``Exception``：``core`` 里所有
    ``except Exception`` 都会抓住 ``Exception``，那样就永远走不到"崩溃窗口"，
    测出来的是一条不存在的路。真崩溃没有任何 handler 能接住它。
    """


class OrderRecordingAdapter(FakeAdapter):
    """记录 ``start()`` 那一刻客户端已经收到哪些 prompt —— 用来锁扫描的顺序。"""

    def __init__(self, client: FakeClient) -> None:
        super().__init__()
        self._client = client
        self.prompts_at_start: list[tuple[str, str]] = []

    def start(self) -> None:
        super().start()
        self.prompts_at_start = list(self._client.prompts)


def inbound_message(
    text: str,
    *,
    conversation_id: str = "chat:55",
    message_id: str | None = None,
    platform: str = "telegram",
) -> Inbound:
    """一条普通（非回调）入站消息。``message_id=None`` 模拟不给 id 的平台。"""
    return Inbound(
        conversation_id=conversation_id,
        text=text,
        platform=platform,
        message_id=message_id,
    )


def texts_of(prompts: list[tuple[str, str]]) -> list[str]:
    """``FakeClient.prompts`` 里的**用户正文**，顺序不变。

    ⚠️ 剥掉的是 C1 拼在正文前面的渠道说明（见
    :mod:`opencode_bridge.channel_profile`）。本文件断言的是**写前收件箱**的语义
    ——落盘时机、去重、重放、失败预算——而不是渠道说明；说明由
    ``tests/test_channel_profile.py`` 单独断言。剥掉之后剩下的那段仍然必须
    **逐字节**等于用户敲的字，所以"用户原文没有被改写"这条不变量照样被守住。
    """
    return [
        text.split(_HINT_SEPARATOR + "\n", 1)[-1] for _session_id, text in prompts
    ]


def turn_finished(session_id: str) -> dict:
    return {"type": "session.execution.succeeded", "data": {"sessionID": session_id}}


class InboxWiringTestCase(unittest.TestCase):
    """每个用例一份仓库内的临时目录；连接与 core 都自动清理。"""

    def setUp(self) -> None:
        os.makedirs(_REPOSITORY_TEMP, exist_ok=True)
        # 先注册目录清理、后注册 close() —— LIFO 保证连接先关、目录后删（WinError 32）。
        self._temporary_directory = tempfile.TemporaryDirectory(dir=_REPOSITORY_TEMP)
        self.addCleanup(self._temporary_directory.cleanup)
        self.inbox_path = os.path.join(self._temporary_directory.name, "inbox.db")
        self.state_path = os.path.join(self._temporary_directory.name, "state.json")

    def make_core(
        self,
        *,
        client: FakeClient | None = None,
        adapter: FakeAdapter | None = None,
        with_inbox: bool = True,
        inbox_options: dict | None = None,
    ) -> tuple[BridgeCore, FakeClient, FakeAdapter, InboundInbox | None]:
        """搭一个 core。``with_inbox=False`` 就是"收件箱关闭"的部署形态。

        ``inbox_options`` 透传给 :class:`InboundInbox`（行数上限用例用
        ``{"max_rows": 3}`` 把上限压到几条，让 :mod:`inbox_row_cap` 真的触发）。
        """
        client = client or FakeClient()
        adapter = adapter if adapter is not None else FakeAdapter()
        inbox = None
        if with_inbox:
            inbox = InboundInbox(self.inbox_path, **(inbox_options or {}))
            self.addCleanup(inbox.close)
        core = BridgeCore(Config(), client, StateStore(self.state_path), inbox=inbox)
        core.attach(adapter)
        self.addCleanup(core.stop)
        return core, client, adapter, inbox

    def start_bridge(self, core: BridgeCore, client: FakeClient) -> None:
        """走真实的 ``start()``，并让事件流立刻确认连上。

        那条 ``server.connected`` 是服务端握手后发的第一帧（见
        ``test_opencode_client``），正是 :meth:`InboundGateway.recover_inbox` 等的信号 ——
        不推它的话每个用例会白等 2 秒上限。
        """
        client.push({"type": "server.connected", "data": {}})
        core.start()


class CommandsAreNeverRecorded(InboxWiringTestCase):
    """⚠️ ``/new``、``/status``、``/setup`` **绝不**写前落盘。"""

    def test_new_command_leaves_no_row_and_is_not_replayed_after_restart(self):
        core, client, _adapter, _inbox = self.make_core()
        core.on_inbound(inbound_message("先说句话", message_id="1"))
        first_session = client.created_ids[0]

        core.on_inbound(inbound_message("/new", message_id="2"))

        self.assertEqual(client.deleted, [first_session], "/new 本次确实干了破坏性的事")
        self.assertEqual(len(client.created_ids), 2)
        self.assertEqual(
            [(row["delivery_id"], row["state"]) for row in read_inbox_rows(self.inbox_path)],
            [("telegram:chat:55:1", "delivered")],
            "命令不经过 prompt()，落盘的那一行永远不会被标成 delivered，"
            "于是每次启动都被重放一遍 —— /new 每次重启都重建会话",
        )

        # 模拟重启：全新的 core、同一份 state.json 与同一份收件箱
        core.stop()
        core2, client2, _adapter2, _inbox2 = self.make_core()
        self.start_bridge(core2, client2)

        self.assertEqual(client2.deleted, [], "/new 不得在重启后被重放")
        self.assertEqual(client2.created_ids, [], "/new 不得在重启后被重放")
        self.assertEqual(client2.prompts, [], "命令消息从不进 prompt()")

    def test_status_command_leaves_no_row_either(self):
        core, _client, _adapter, _inbox = self.make_core()
        core.on_inbound(inbound_message("先说句话", message_id="1"))
        core.on_inbound(inbound_message("/status", message_id="2"))

        rows = read_inbox_rows(self.inbox_path)
        self.assertEqual(
            [(row["delivery_id"], row["state"]) for row in rows],
            [("telegram:chat:55:1", "delivered")],
            "只有那条真实提示词该在盘上",
        )


class FailedPromptIsReplayedOnNextBoot(InboxWiringTestCase):
    """prompt 明确失败：留 ``failed`` 行、正文原样、退避到期后由下次启动重放。"""

    def test_failed_row_keeps_the_text_verbatim_and_waits_for_the_backoff(self):
        core, client, _adapter, inbox = self.make_core()
        client.prompt_errors.append(OpenCodeError("server said no", status=500))

        core.on_inbound(inbound_message("改一下 README", message_id="42"))

        rows = read_inbox_rows(self.inbox_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "failed")
        self.assertEqual(rows[0]["attempts"], 1)
        self.assertEqual(
            rows[0]["text"], "改一下 README",
            "正文必须原样：告警文字绝不进 agent 的上下文，重试时也绝不加前缀",
        )
        # 退避是**期限**：没到期就不放行，这才是重启安全的理由。
        assert inbox is not None
        self.assertEqual(inbox.due_failed_prompts(time.time()), [])
        after_first_step = time.time() + BACKOFF_LADDER_SECONDS[0] + 1.0
        self.assertEqual(len(inbox.due_failed_prompts(after_first_step)), 1)

    def test_next_boot_replays_it_into_prompt_before_adapters_start(self):
        # 阶梯旁路成 0：这里要测的是"下次启动真的重放"，不是"等了 30 秒之后"。
        with mock.patch.object(inbox_module, "BACKOFF_LADDER_SECONDS", (0.0, 0.0)):
            core, client, _adapter, _inbox = self.make_core()
            client.prompt_errors.append(OpenCodeError("server said no", status=500))
            core.on_inbound(inbound_message("改一下 README", message_id="42"))
            self.assertEqual(
                [row["state"] for row in read_inbox_rows(self.inbox_path)], ["failed"]
            )
            core.stop()

            client2 = FakeClient()
            adapter2 = OrderRecordingAdapter(client2)
            core2, _client2, _adapter2, _inbox2 = self.make_core(
                client=client2, adapter=adapter2
            )
            self.start_bridge(core2, client2)

        self.assertEqual(
            texts_of(client2.prompts), ["改一下 README"],
            "下一次启动的扫描必须把它真的交给 prompt()",
        )
        self.assertEqual(
            client2.created_ids, [], "重放沿用已存的会话，不该新建"
        )
        self.assertEqual(
            texts_of(adapter2.prompts_at_start), ["改一下 README"],
            "扫描必须在 adapter.start() 之前完成，否则实时消息会与重放并发"
            "打同一个会话",
        )
        self.assertEqual(
            [row["state"] for row in read_inbox_rows(self.inbox_path)], ["delivered"]
        )


class CrashBeforeDispatchReplaysThePendingRow(InboxWiringTestCase):
    """The ledger's headline case: death *after* the write-ahead, *before* dispatch.

    ``prompt()`` was never called, so a replay cannot duplicate any side effect --
    which is exactly why ``pending`` and ``attempting`` are recorded separately.
    """

    def test_pending_row_is_replayed_by_the_next_boot(self):
        core, client, _adapter, _inbox = self.make_core()
        # Die between the write-ahead and the enqueue: _enqueue never runs at all.
        # （入队搬进了 ``InboundGateway``，所以打的是那边的 ``_enqueue``；同一个窗口。）
        with mock.patch.object(
            core.inbound_gateway, "_enqueue",
            side_effect=SimulatedCrash("died right after record"),
        ):
            with self.assertRaises(SimulatedCrash):
                core.on_inbound(inbound_message("把 README 翻译成英文", message_id="3"))

        self.assertEqual(
            [row["state"] for row in read_inbox_rows(self.inbox_path)], ["pending"]
        )
        self.assertEqual(client.prompts, [], "一次都没投出去过")
        core.stop()

        core2, client2, adapter2, _inbox2 = self.make_core()
        self.start_bridge(core2, client2)

        self.assertEqual(
            texts_of(client2.prompts), ["把 README 翻译成英文"],
            "从未尝试过的行必须真的被重放 —— 这就是要修的那个丢消息 bug",
        )
        self.assertEqual(
            [row["state"] for row in read_inbox_rows(self.inbox_path)], ["delivered"]
        )
        self.assertFalse(
            any("状态未知" in out.text for out in adapter2.sent),
            "pending 行是直接重放，不必惊动用户；只有 attempting 才告警",
        )


class CrashInsidePrompt(InboxWiringTestCase):
    """崩溃正好落在 prompt 调用里：结果不可知 -> 只告警，**绝不重放**（用户拍板）。"""

    def test_row_stays_attempting_and_next_boot_alerts_without_replaying(self):
        core, client, _adapter, _inbox = self.make_core()
        client.prompt_errors.append(SimulatedCrash("killed mid-prompt"))

        with self.assertRaises(SimulatedCrash):
            core.on_inbound(inbound_message("把 tests 全跑一遍", message_id="7"))

        rows = read_inbox_rows(self.inbox_path)
        self.assertEqual(
            [row["state"] for row in rows], ["attempting"],
            "崩溃落在 mark_attempting 与 mark_delivered 之间，这一行必须停在 attempting",
        )
        self.assertEqual(rows[0]["text"], "把 tests 全跑一遍")
        self.assertEqual(
            texts_of(client.prompts), ["把 tests 全跑一遍"],
            "消息确实已经交给了客户端 —— 这正是不能重放的原因",
        )
        core.stop()

        core2, client2, adapter2, _inbox2 = self.make_core()
        self.start_bridge(core2, client2)

        self.assertEqual(client2.prompts, [], "attempting 行绝不重放")
        alerts = [out.text for out in adapter2.sent if "状态未知" in out.text]
        self.assertEqual(
            len(alerts), 1,
            "告警只发一次：recover_pending 已经发过，core 不得重发",
        )
        self.assertEqual(
            [row["state"] for row in read_inbox_rows(self.inbox_path)], ["attempting"]
        )


class CrashInsidePromptIsNotEvictedByTheRowCap(InboxWiringTestCase):
    """真崩溃留下的 ``attempting`` 行不会被行数上限吃掉 —— 于是告警不会少算。

    这条把 :class:`CrashInsidePrompt` 的语义与 :mod:`inbox_row_cap` 的白名单接在一起：
    ``attempting`` 是恢复层唯一**只告警、绝不重放**的一档，所以它一旦被淘汰，
    ``uncertain_prompts()`` 就再也看不到它 —— 消息静默消失、无失败记录、无告警。
    走的是真 :class:`BridgeCore` + 真收件箱 + 真 ``start()``（含真恢复）。
    """

    def test_the_alert_still_counts_every_crashed_message_after_the_cap_fires(self):
        core, client, _adapter, _inbox = self.make_core(
            inbox_options={"max_rows": 3}
        )
        # ⚠️ 必须**一条一个会话**：崩溃是从 ``_drain`` 里逃出来的 ``BaseException``，
        # 逃得掉也意味着 ``_draining`` 没被清掉 —— 同一会话的下一条会以为"已经有人在
        # 排空我了"而直接返回，于是压根不会走到 prompt()（实测：第二轮 assertRaises
        # 失败，SimulatedCrash 没被抛出）。换会话才是五次独立的崩溃。
        crashed_conversations = [f"chat:crash{index}" for index in range(5)]
        for index, conversation_id in enumerate(crashed_conversations):
            client.prompt_errors.append(SimulatedCrash("killed mid-prompt"))
            with self.assertRaises(SimulatedCrash):
                core.on_inbound(inbound_message(
                    f"第 {index} 条", conversation_id=conversation_id,
                    message_id=str(20 + index),
                ))

        rows = read_inbox_rows(self.inbox_path)
        self.assertEqual(
            [row["state"] for row in rows], ["attempting"] * 5,
            "上限 3 而 attempting 有 5 条时，一条都不许被淘汰",
        )
        core.stop()

        core2, client2, adapter2, _inbox2 = self.make_core(
            inbox_options={"max_rows": 3}
        )
        self.start_bridge(core2, client2)

        self.assertEqual(client2.prompts, [], "attempting 仍绝不重放")
        alerts = [out.text for out in adapter2.sent if "状态未知" in out.text]
        # 一个会话一条告警，所以这里断言的是"每个会话都还有它那一行"，
        # 而不是"5 条消息合成一条" —— 崩溃行分属不同会话。
        self.assertEqual(len(alerts), len(crashed_conversations))
        for alert in alerts:
            self.assertIn("1", alert,
                          "每条告警必须说真的 1 条 —— 少算就意味着有条被静默丢了")
        self.assertEqual(
            sorted(row["delivery_id"] for row in read_inbox_rows(self.inbox_path)),
            sorted(f"telegram:{conversation_id}:{20 + index}"
                   for index, conversation_id in enumerate(crashed_conversations)),
        )


class RedeliveryIsDeduplicated(InboxWiringTestCase):
    """平台重投 = at-most-once 的最后一道闸门。"""

    def test_same_message_id_arriving_twice_is_delivered_once(self):
        core, client, _adapter, _inbox = self.make_core()

        core.on_inbound(inbound_message("跑一下测试", message_id="99"))
        core.on_inbound(inbound_message("跑一下测试", message_id="99"))

        self.assertEqual(texts_of(client.prompts), ["跑一下测试"])
        self.assertEqual(
            [(row["state"], row["attempts"]) for row in read_inbox_rows(self.inbox_path)],
            [("delivered", 0)],
        )

    def test_identical_messages_without_message_id_collapse_and_say_so(self):
        """没有 ``message_id`` 的平台只能按内容哈希去重 —— 合并必须**看得见**。"""
        core, client, _adapter, _inbox = self.make_core()
        core.on_inbound(inbound_message("跑一下测试", message_id=None))

        with self.assertLogs("opencode_bridge.inbound_gateway", level="INFO") as captured:
            core.on_inbound(inbound_message("跑一下测试", message_id=None))

        self.assertEqual(texts_of(client.prompts), ["跑一下测试"])
        self.assertEqual(len(read_inbox_rows(self.inbox_path)), 1)
        self.assertTrue(
            any("content hash" in line for line in captured.output),
            "去重键退回内容哈希必须记 info 日志：\n%s" % "\n".join(captured.output),
        )


class BusyIsNotAFailure(InboxWiringTestCase):
    """409 只是"还没轮到"，不能算失败、更不能花掉重试预算。

    ⚠️ 状态：409 之后那一行退回 ``pending``，**不是**停在 ``attempting``。
    409 只有在 ``prompt()`` **之后**才认得，而 ``mark_attempting`` 必须紧贴那个
    调用之前写（早写一个字，``create_session`` 期间的崩溃就会被误判成"结果不可知"）
    —— 所以"服务端明确拒收、agent 没跑过"这个**已知**结果唯一的表达方式就是退回。
    :meth:`~opencode_bridge.inbox.InboundInbox.mark_pending` 就是这条转移。

    退回 pending 也**不会**削弱 at-most-once：409 意味着服务端拒收，副作用一次都
    没发生，重放不可能重复。改之前那行会永远停在 ``attempting``，被恢复层按"结果
    不可知"只告警、绝不重放，**平台重投也被 ``INSERT OR IGNORE`` 挡掉** ——
    消息永远送不到。代价方向是"从前丢消息"变成"下次启动送达"，不是反过来。
    """

    def test_409_keeps_the_write_ahead_obligation_and_retries_after_the_turn(self):
        core, client, _adapter, inbox = self.make_core()
        client.prompt_errors.append(OpenCodeError("session busy", status=409))

        core.on_inbound(inbound_message("再等等", message_id="5"))

        rows = read_inbox_rows(self.inbox_path)
        self.assertEqual(len(rows), 1, "写前义务必须还在盘上，消息不能凭空消失")
        self.assertNotEqual(rows[0]["state"], "delivered", "409 绝不是已送达")
        self.assertEqual(rows[0]["attempts"], 0, "409 不是失败，不得花掉重试预算")
        assert inbox is not None
        self.assertEqual(inbox.due_failed_prompts(time.time() + 3600), [])
        self.assertEqual(inbox.abandoned_prompts(), [])
        self.assertEqual(texts_of(client.prompts), ["再等等"])
        session_id = client.created_ids[0]

        core.event_stream.dispatch(turn_finished(session_id))

        self.assertEqual(
            texts_of(client.prompts), ["再等等", "再等等"],
            "这一轮结束后同一行必须被重新投出去",
        )
        self.assertEqual(
            [(row["state"], row["attempts"]) for row in read_inbox_rows(self.inbox_path)],
            [("delivered", 0)],
        )

    def test_409_leaves_the_row_pending_so_a_crash_still_delivers_it(self):
        """409 与重投之间崩溃：这一行必须能被重放，而不是永远搁在 attempting。

        走**真**的 :class:`BridgeCore` + :class:`InboundGateway` + **真**收件箱，
        断言打的是**盘上的行**。没有 ``mark_pending`` 时那行停在 ``attempting``，
        下次启动按"结果不可知"只告警不重放 —— 消息永远送不到。
        """
        core, client, _adapter, _inbox = self.make_core()
        client.prompt_errors.append(OpenCodeError("session busy", status=409))

        core.on_inbound(inbound_message("先记着", message_id="6"))

        self.assertEqual(
            [(row["state"], row["attempts"]) for row in read_inbox_rows(self.inbox_path)],
            [("pending", 0)],
            "409 之后必须退回 pending：attempting 只告警不重放，消息就永远送不到",
        )
        core.stop()   # 死在 409 与进程内重投之间

        core2, client2, _adapter2, _inbox2 = self.make_core()
        self.start_bridge(core2, client2)

        self.assertEqual(
            texts_of(client2.prompts), ["先记着"],
            "下次启动必须真的把它重放出去 —— 搁死的那一行等于丢消息",
        )
        self.assertEqual(
            [(row["state"], row["attempts"]) for row in read_inbox_rows(self.inbox_path)],
            [("delivered", 0)],
        )

        # 再启动一次：已送达的行必须挡住重复投递（at-most-once 的另一半）。
        core2.stop()
        core3, client3, _adapter3, _inbox3 = self.make_core()
        self.start_bridge(core3, client3)

        self.assertEqual(
            texts_of(client3.prompts), [],
            "409 退回 pending 只放宽了'送不到'，绝不能顺带制造第二次投递",
        )
        self.assertEqual(
            [(row["state"], row["attempts"]) for row in read_inbox_rows(self.inbox_path)],
            [("delivered", 0)],
        )

    def test_409_then_a_real_failure_spends_exactly_one_budget_step(self):
        """409 不烧预算、之后的真失败烧一级 —— 两次尝试有界，不是无限重试。

        这条是 at-most-once 的另一半：退回 pending 必须**不是**预算的旁路。
        """
        with mock.patch.object(inbox_module, "BACKOFF_LADDER_SECONDS", (0.0, 0.0)):
            core, client, _adapter, _inbox = self.make_core()
            # 三次投递的结果依次是：409、409、500 —— 队列按顺序取走这些异常。
            for error in (OpenCodeError("session busy", status=409),
                          OpenCodeError("session busy", status=409),
                          OpenCodeError("server said no", status=500)):
                client.prompt_errors.append(error)
            core.on_inbound(inbound_message("慢慢来", message_id="7"))
            self.assertEqual(
                [row["attempts"] for row in read_inbox_rows(self.inbox_path)], [0],
                "409 不是失败：一级预算都还没花",
            )

            # 每一轮结束都刷一次队列：第二次仍被拒，第三次才真的失败
            core.event_stream.dispatch(turn_finished(client.created_ids[0]))
            self.assertEqual(
                [row["attempts"] for row in read_inbox_rows(self.inbox_path)], [0],
                "又一次 409 仍然不烧预算",
            )
            core.event_stream.dispatch(turn_finished(client.created_ids[0]))

        self.assertEqual(
            [(row["state"], row["attempts"]) for row in read_inbox_rows(self.inbox_path)],
            [("failed", 1)],
            "409 之后的那次真失败必须恰好烧掉一级预算，而不是两级",
        )
        self.assertEqual(
            texts_of(client.prompts), ["慢慢来", "慢慢来", "慢慢来"],
            "三次投递：第一次 409、第二次 409、第三次真失败",
        )


class InboxDisabledStillDelivers(unittest.TestCase):
    """``inbox=None``（旧形态）必须逐字保持原行为，而不是变成"什么都不做"。"""

    def test_plain_message_is_prompted_without_any_inbox(self):
        os.makedirs(_REPOSITORY_TEMP, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=_REPOSITORY_TEMP) as directory:
            client = FakeClient()
            adapter = FakeAdapter()
            core = BridgeCore(
                Config(),
                client,
                StateStore(os.path.join(directory, "state.json")),
                inbox=None,
            )
            core.attach(adapter)
            self.addCleanup(core.stop)

            core.on_inbound(inbound_message("照旧投递", message_id="1"))

            self.assertEqual(texts_of(client.prompts), ["照旧投递"])


class CliWiresTheInbox(unittest.TestCase):
    """⚠️ 守卫：可选注入意味着漏接线不会有任何测试变红，所以必须显式断言。"""

    def test_bridge_is_constructed_with_a_real_inbox_next_to_the_state_file(self):
        recorded: dict = {}

        class RecordingCore:
            def __init__(self, config, client, state, inbox=None) -> None:
                recorded["inbox"] = inbox

            def attach(self, adapter) -> None:
                return None

            def start(self) -> None:
                return None

            def stop(self) -> None:
                return None

        class SilentDiagnostics:
            def __init__(self, directory) -> None:
                pass

            def install(self) -> None:
                return None

            def record(self, exit_reason, detail="") -> None:
                return None

            def dump_stacks(self, exit_reason) -> None:
                return None

            def close(self) -> None:
                return None

        class AlwaysStopping:
            """让 ``stop_event.wait(1.0)`` 立刻为真，测试不必真等一秒。"""

            def wait(self, timeout=None) -> bool:
                return True

            def set(self) -> None:
                return None

        class PermissiveInstanceLock:
            def __init__(self, directory) -> None:
                pass

            def acquire(self) -> tuple[bool, int]:
                return True, 0

            def release(self) -> None:
                return None

        os.makedirs(_REPOSITORY_TEMP, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=_REPOSITORY_TEMP) as directory:
            config = Config(
                state_path=os.path.join(directory, "state.json"),
                adapters={"telegram": {"bot_token": "token-not-real"}},
            )
            with mock.patch.object(
                cli, "discover_endpoint",
                return_value=Endpoint("http://127.0.0.1:4096", "pw"),
            ), mock.patch.object(
                cli, "OpenCodeClient",
                lambda endpoint: types.SimpleNamespace(close=lambda: None),
            ), mock.patch.object(
                cli, "build",
                lambda name, entry, hooks: types.SimpleNamespace(bot_token="token-not-real"),
            ), mock.patch.object(
                cli, "BridgeCore", RecordingCore
            ), mock.patch.object(
                cli, "ProcessDiagnostics", SilentDiagnostics
            ), mock.patch.object(
                cli, "InstanceLock", PermissiveInstanceLock
            ), mock.patch.object(
                cli, "threading", types.SimpleNamespace(Event=lambda: AlwaysStopping())
            ):
                exit_code = cli.run_bridge(config)

            inbox_path = os.path.join(directory, "inbox.db")
            self.assertTrue(
                os.path.isfile(inbox_path),
                "收件箱必须与 state.json 同目录（%s）" % inbox_path,
            )
            inbox = recorded.get("inbox")
            self.assertIsNotNone(
                inbox,
                "__main__ 必须注入收件箱：它不注入的话，这次要修的丢消息 bug 原样存在，"
                "而其它所有测试都跑在 inbox=None 上、照样全绿",
            )
            self.assertIsInstance(inbox, InboundInbox)
            inbox.close()
        self.assertEqual(exit_code, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
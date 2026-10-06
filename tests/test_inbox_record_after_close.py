"""``close()`` 之后调 :meth:`InboundInbox.record`：**误报方向**的回归测试。

确诊的缺陷
----------
:func:`opencode_bridge.inbox.InboundInbox.record` 头一行是
``if self._connection is None: return False``，而 ``False`` 在这个类的契约里是
**去重命中**（该方法的 docstring 逐字写着「``False`` 不是错误，而是去重命中」）
⇒ 关停期间掉下来的消息被日志说成「平台重投了一条我们已有的消息」，
**与事实相反**，而级别只有 ``info``。

前置状态**可达**，不是理论：``__main__._run_bridge_locked`` 的 ``finally`` 在
``core.stop()`` **之后**才 ``inbox.close()``，而 :meth:`BridgeCore.stop` 只给适配器
线程 5 秒 join、超时**只记一条 warning 就继续** ⇒ 适配器线程还活着时收件箱已关。

窗口很窄（只在 ``close()`` 之后），所以这**不是丢数据**；它是缺陷的理由是
**误报的方向**：关停期间掉的消息，日志会把排查主动引到「平台重投」上去 ——
而本仓库反复吃过「日志指错方向」的亏。

三项分别钉住
------------
⛔ 「关完之后有一条 warning」是**恒真**断言 —— 关停期间别的告警一样会触发。
所以下面三个类各钉一项：返回值（:class:`TestTheReturnValueSaysWhatActuallyHappened`）、
文案（:class:`TestTheWarningSaysWhatActuallyHappened`）、
级别（:class:`TestTheWarningLevelIsRight`）。三项互不代替。

反向护栏
--------
:class:`TestTheDedupPathIsUntouched` 钉「真去重」那条既有路径**逐字照旧** ——
没有它，把两条路径合成一条的改法（返回同一个值、打同一条日志）也会让上面全绿。

⚠️ 本文件断言**两个** logger：收件箱自己那档（``opencode_bridge.inbox``）与唯一生产调用点
:func:`opencode_bridge.inbound_gateway._record_inbound`（``opencode_bridge.inbound_gateway``）。
缺陷的**两半**都在里面 —— 收件箱这一侧记的是「没落盘因为已关」，调用方那一侧曾经把同一件事
报成「duplicate ignored（平台重投了一条我们已有的消息）」。两半都钉住，缺一半的话
:mod:`tests.test_inbox_wiring` 里那条走真接线的用例也照样会绿。
⚠️ 局部 import :class:`RecordOutcome`：顶层 import 会让**整个模块**在没有它的旧实现上
报 ImportError，于是「日志方向」那条用例的失败原因被掩盖掉 —— 而那条才是本缺陷本体。
"""

from __future__ import annotations

import logging
import os
import tempfile
import unittest

from opencode_bridge.inbox import InboundInbox, QueuedPrompt
from tests.test_inbox import read_inbox_rows

_REPOSITORY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: 仓库内的临时目录：即便 TEMP/TMP 指向机器别处，测试也不可能写到仓库之外。
_REPOSITORY_TEMP = os.path.join(_REPOSITORY_ROOT, ".tmp")

#: 收件箱这一族的 logger。⚠️ :mod:`opencode_bridge.inbox_row_cap` **故意**用同一个名字
#: （行数治理的告警也在这一档）⇒ 断言"只有一条"时行数必须远小于默认上限 500，
#: 否则治理日志会混进来，那条断言就成了别的意思。
_INBOX_LOGGER = "opencode_bridge.inbox"

#: 唯一生产调用点那个 logger（它那两句去重 info 在这里）。
_CALLER_LOGGER = "opencode_bridge.inbound_gateway"

#: 走真调用方那几条用例造的那条入站消息。delivery_id 由
#: :func:`opencode_bridge.inbound_gateway._queued_prompt_for` 拼成
#: ``platform:conversation_id:message_id`` ⇒ 日志里的点名要能逐字对上这一串。
_PLATFORM = "telegram"
_CONVERSATION = "chat:100200"
_MESSAGE_ID = "41"
_DELIVERY_ID = "telegram:chat:100200:41"


def make_prompt(
    delivery_id: str,
    *,
    message_id: str | None = None,
    text: str = "看一下 README",
) -> QueuedPrompt:
    """构造一条入站提示词。默认会话是假的，不含任何真实身份信息。"""
    return QueuedPrompt(
        delivery_id=delivery_id,
        conversation_id="telegram:chat:100200",
        platform="telegram",
        message_id=message_id,
        text=text,
    )


class RecordAfterCloseTestCase(unittest.TestCase):
    """每条用例一份仓库内的临时目录 + 一个自动关闭的收件箱。"""

    def setUp(self) -> None:
        os.makedirs(_REPOSITORY_TEMP, exist_ok=True)
        # 先注册目录清理、后注册 close() —— LIFO 保证连接先关、目录后删（WinError 32）。
        self._temporary_directory = tempfile.TemporaryDirectory(dir=_REPOSITORY_TEMP)
        self.addCleanup(self._temporary_directory.cleanup)
        self.database_path = os.path.join(self._temporary_directory.name, "inbox.sqlite3")

    def open_inbox(self) -> InboundInbox:
        inbox = InboundInbox(self.database_path)
        self.addCleanup(inbox.close)  # 可重复调用 ⇒ 用例自己先关过也不炸
        return inbox


class TestTheReturnValueSaysWhatActuallyHappened(RecordAfterCloseTestCase):
    """返回值必须能把「没落盘因为**去重**」与「没落盘因为**收件箱已关**」分开。

    旧实现两条都是 ``False`` ⇒ 调用方只能靠猜，而唯一生产调用方猜的**方向是反的**。
    """

    def test_a_dedup_hit_and_a_write_refused_because_closed_are_different_outcomes(self):
        from opencode_bridge.inbox import RecordOutcome

        inbox = self.open_inbox()
        recorded_prompt = make_prompt("telegram:telegram:chat:100200:31", message_id="31")
        self.assertIs(inbox.record(recorded_prompt), RecordOutcome.RECORDED)
        self.assertIs(inbox.record(recorded_prompt), RecordOutcome.DUPLICATE)

        inbox.close()

        # ⚠️ 换一条 delivery_id：它**没有被去重挡掉**，所以"没落盘"只剩一个理由。
        # 沿用同一条的话，去重与"已关"两件事会同时成立，这条用例就分不出谁是谁。
        self.assertIs(
            inbox.record(make_prompt("telegram:telegram:chat:100200:32", message_id="32")),
            RecordOutcome.CLOSED,
        )
        self.assertIsNot(
            RecordOutcome.CLOSED, RecordOutcome.DUPLICATE,
            "「已关」与「去重」必须是两个值 —— 合成一个就退回本缺陷本身了",
        )

    def test_both_not_recorded_outcomes_stay_falsy_for_the_one_truthiness_caller(self):
        """⚠️ 唯一生产调用点按**真假**分流，所以两个"没落盘"的结局都必须仍是假值。

        特别地 ``CLOSED`` 必须是假值：让它为真就会让 ``_record_inbound`` 在**盘上没有任何
        回执**的情况下把消息投出去 —— 绕过写前义务正是本模块存在的理由被推翻。
        ⛔ 而"关掉之后不再投递"是**设计**（``close()`` 可重复调用，之后所有公开方法是空操作），
        本缺陷只修**误报的方向**，不改这一侧的行为。
        """
        inbox = self.open_inbox()
        recorded_prompt = make_prompt("telegram:telegram:chat:100200:33", message_id="33")
        inbox.record(recorded_prompt)

        self.assertFalse(inbox.record(recorded_prompt), "去重命中仍是假值（既有语义不许变）")

        inbox.close()

        self.assertFalse(
            inbox.record(make_prompt("telegram:telegram:chat:100200:34", message_id="34")),
            "收件箱已关时仍不投递 —— 关停侧行为不变，只修日志",
        )


class TestTheWarningSaysWhatActuallyHappened(RecordAfterCloseTestCase):
    """关掉之后写不进去，**必须被说成"收件箱已关"**。"""

    def _record_after_close(self, delivery_id: str) -> str:
        """关掉之后记一条，返回收件箱自己打出来的那条日志正文。"""
        inbox = self.open_inbox()
        inbox.close()

        with self.assertLogs(_INBOX_LOGGER, level="DEBUG") as captured:
            inbox.record(make_prompt(delivery_id, message_id=delivery_id.rsplit(":", 1)[-1]))

        self.assertEqual(
            len(captured.records), 1,
            "这一段里收件箱只该说一句话；多出来的说明又混进了别的告警，"
            "下面的断言就不知道在钉谁了：%r"
            % [record.getMessage() for record in captured.records],
        )
        return captured.records[0].getMessage()

    def test_the_line_names_the_closed_connection_not_a_platform_redelivery(self):
        message = self._record_after_close("telegram:telegram:chat:100200:35")

        self.assertIn("closed", message, "必须说真话：连接已关")
        self.assertIn(
            "telegram:telegram:chat:100200:35", message,
            "必须点名是哪一条 —— 否则关停窗口里掉了好几条时无从对账",
        )
        for reversed_wording in ("duplicate ignored", "re-delivered", "已知消息"):
            self.assertNotIn(
                reversed_wording, message,
                "把原因说反的文案必须不出现：读者会照着「平台重投」去查，"
                "而事实是收件箱已经关了",
            )

    def test_it_says_the_message_cannot_be_replayed_because_it_never_reached_the_disk(self):
        """告警里承诺的**后果**必须是真的，而且要说出来。

        ⚠️ 方向对但内容假，比方向错更难查（本仓库刚在 ``health.py`` 上吃过一次：
        「没尝试写盘却说写入失败」）。所以下面单独钉住"它真的不在盘上"。
        """
        message = self._record_after_close("telegram:telegram:chat:100200:36")

        self.assertIn(
            "replay", message,
            "关停窗口里掉的消息**永远不会被重放**（盘上没它）—— 这正是它该被听见的原因",
        )
        self.assertEqual(
            read_inbox_rows(self.database_path), [],
            "告警宣称没落盘，那就真的没有这行；宣称反了就是又一条方向错的日志",
        )


class TestTheWarningLevelIsRight(RecordAfterCloseTestCase):
    """级别：``warning``。判据写在这里，改级别的人得先推翻它。"""

    def test_it_is_a_warning_not_an_info(self):
        """为什么是 ``warning``：

        * ⛔ **不是 info** —— 这条消息没有回执、**永远不会被重放**，即一次
          「无失败记录、无告警的丢失」。本模块对同型事件（不可重放的状态被静默淘汰）
          用的正是 warning，见 :func:`opencode_bridge.inbox_row_cap.report_eviction`。
          原来的 info 让它在默认日志级别下与「去重」混为一谈。
        * ⛔ **不是 error** —— 关停是**预期事件**，它不会自愈也不会让进程退出；
          打 error 会把真的错误淹掉（关停窗口里每个还活着的适配器线程都会来一条）。
        * ⛔ **不是 debug** —— 关停窗口很窄，人几乎不可能在默认级别下看到 debug。
        """
        inbox = self.open_inbox()
        inbox.close()

        with self.assertLogs(_INBOX_LOGGER, level="DEBUG") as captured:
            inbox.record(make_prompt("telegram:telegram:chat:100200:37", message_id="37"))

        self.assertEqual(len(captured.records), 1)
        self.assertEqual(
            captured.records[0].levelno, logging.WARNING,
            "级别是判据的一部分：文案对而级别错，关停期间照样没人看见"
            "（实际打出来的是 %s）" % captured.records[0].levelname,
        )


class TestTheDedupPathIsUntouched(RecordAfterCloseTestCase):
    """反向护栏：「真去重」那条既有路径必须**逐字照旧**。"""

    def test_a_real_dedup_hit_is_silent_in_the_inbox_logger(self):
        """去重时收件箱**自己不吵** —— 那两句 info 是调用方打的（见下面那条）。

        ⛔ 这条挡住"把两条路径合成一条"：那种改法会让上面三个类照样全绿。
        """
        inbox = self.open_inbox()
        recorded_prompt = make_prompt("telegram:telegram:chat:100200:38", message_id="38")
        inbox.record(recorded_prompt)

        with self.assertNoLogs(_INBOX_LOGGER, level="DEBUG"):
            self.assertFalse(inbox.record(recorded_prompt))

    def test_a_real_dedup_hit_still_makes_the_caller_log_its_original_line(self):
        """逐字钉住调用方那句 info —— 「去重路径照旧」的可见证据。

        ⚠️ 走**真**的 :func:`opencode_bridge.inbound_gateway._record_inbound`：
        只测 :meth:`InboundInbox.record` 的话，"调用方照旧"这件事没有任何断言。
        ⛔ 本文件**不修改**那个模块，只观察它。
        """
        from opencode_bridge.hooks import Inbound
        from opencode_bridge.inbound_gateway import _record_inbound

        inbox = self.open_inbox()
        inbound = Inbound(
            conversation_id="chat:100200",
            text="跑一下测试",
            platform="telegram",
            message_id="39",
        )

        self.assertIsNotNone(_record_inbound(inbox, inbound, "跑一下测试"))

        with self.assertLogs(_CALLER_LOGGER, level="INFO") as captured:
            self.assertIsNone(_record_inbound(inbox, inbound, "跑一下测试"))

        self.assertEqual(len(captured.records), 1)
        self.assertEqual(captured.records[0].levelno, logging.INFO)
        self.assertEqual(
            captured.records[0].getMessage(),
            "inbox telegram:chat:100200:39: duplicate ignored "
            "(platform re-delivered a known message)",
            "真去重时这句 info 必须逐字不变 —— 它是本缺陷**不该**改的那条路径",
        )


class TestTheCallerSaysWhatActuallyHappened(RecordAfterCloseTestCase):
    """读侧那条分支：调用方必须说「收件箱已关」，⛔ 不得说「平台重投」。

    这是本缺陷**残留**的另一半 —— :class:`RecordOutcome` 早就给了三态，而唯一生产调用点
    :func:`opencode_bridge.inbound_gateway._record_inbound` 只按真假分流
    ⇒ ``CLOSED`` 落进了那两句 ``duplicate ignored`` 的 info。

    ⚠️ 下面每一条都断言**两头**：那句假话**不再出现**，且对应的那句真话**出现**。
    只断言「日志里出现了新那句」是 ``AGENTS.md`` §9 讲的半真断言 ——
    把整个分支连同它的日志一起删掉，它照样过。

    ⛔ 投递侧一个字都没改：``CLOSED`` 仍是假值，所以关掉之后**仍然不投递**
    （``assertIsNone`` 钉住这一半）。
    """

    def setUp(self) -> None:
        super().setUp()
        #: 这次调用**决定要投递**的那一行；``None`` = 什么都不投递。
        #: 预置成 ``None``（而不是等 helper 里赋值）⇒ 忘了调 helper 的用例会红在
        #: ``assertIsNone`` 上（一句能读的失败），而不是红在 AttributeError 上。
        self.row_to_deliver = None

    def _closed_inbox_caller_view(self) -> str:
        """走**真**的调用方（收件箱已关），返回它那个 logger 抓到的一条正文。

        结果写进 :attr:`row_to_deliver`。
        """
        from opencode_bridge.inbound_gateway import _record_inbound
        from opencode_bridge.hooks import Inbound

        inbox = self.open_inbox()
        inbox.close()
        inbound = Inbound(
            conversation_id=_CONVERSATION,
            text="关停期间掉下来的消息",
            platform=_PLATFORM,
            message_id=_MESSAGE_ID,
        )

        with self.assertLogs(_CALLER_LOGGER, level="INFO") as captured:
            self.row_to_deliver = _record_inbound(inbox, inbound, inbound.text)

        self.assertEqual(
            len(captured.records), 1,
            "这一段里调用方只该说一句话；多出来的说明又混进了别的 info，"
            "下面的断言就不知道在钉谁了：%r"
            % [record.getMessage() for record in captured.records],
        )
        return captured.records[0].getMessage()

    def test_a_write_refused_because_closed_is_never_reported_as_a_redelivery(self):
        """⚠️ 会红的情形：读侧退回「只按 ``False`` 分流」。

        症状就是本缺陷本体 —— 日志说「平台重投了一条我们已有的消息」，而事实是
        连接已经关了、这条压根没落盘。同一处改动若把 ``CLOSED`` 改成真值，
        :meth:`~unittest.TestCase.assertIsNone` 那一条会红（投递行为被改坏）。
        """
        message = self._closed_inbox_caller_view()

        for reversed_wording in ("duplicate ignored", "re-delivered", "content hash", "已知消息"):
            self.assertNotIn(
                reversed_wording, message,
                "把原因说反的文案必须不出现：读者会照着「平台重投」去查，"
                "而事实是收件箱已经关了。实际打出来的是：%r" % message,
            )
        self.assertIsNone(
            self.row_to_deliver,
            "关掉之后仍然不投递 —— 关停侧行为是设计，本缺陷只修误报的方向",
        )

    def test_the_truthful_line_names_the_delivery_and_says_why_it_is_not_delivered(self):
        """⚠️ 会红的情形：真话那句被改写、漏掉点名，或 ``already closed`` 被换成别的理由。

        点名是必需的：关停窗口里掉了好几条时，没有 delivery_id 就无从对账
        （而它由 :func:`_queued_prompt_for` 拼出，值在这一行里钉死）。
        """
        message = self._closed_inbox_caller_view()

        self.assertIn(
            "already closed", message,
            "必须说真话：连接已关。实际打出来的是：%r" % message,
        )
        self.assertIn(_DELIVERY_ID, message, "必须点名是哪一条，否则无从对账")
        self.assertIn(
            "not delivered", message,
            "调用方唯一比收件箱多知道的一件事就是「因此没有投递」——"
            "不说它，读者会以为这条已经交给 agent 了",
        )

    def test_the_truthful_line_promises_a_consequence_that_is_actually_true(self):
        """⚠️ 会红的情形：文案宣称的后果变成假的（例如改成「下次启动会重放」）。

        方向对而内容假，比方向错更难查（本仓库在 ``health.py`` 上刚吃过一次：
        「没尝试写盘却说写入失败」）⇒ 后果与盘上的事实一起钉。
        """
        message = self._closed_inbox_caller_view()

        self.assertIn(
            "replay", message,
            "关停窗口里掉的消息**永远不会被重放**（盘上没它）—— 这正是它该被听见的原因",
        )
        self.assertEqual(
            read_inbox_rows(self.database_path), [],
            "文案宣称没落盘，那就真的没有这行；宣称反了就是又一条方向错的日志",
        )

    def test_the_caller_stays_at_info_because_the_inbox_keeps_the_warning(self):
        """⚠️ 会红的情形：调用方升到 warning（一次丢失被数两遍），或收件箱不再 warning
        （关停窗口里的丢失彻底无声）。

        级别判据：严重性归收件箱那一档 —— 它**每拒一条**都 warning（它掌握路径与
        「不可重放」这个后果），而关停期间每个还活着的适配器线程都会走到这里，
        调用方再 warning 一次只是把同一次丢失数两遍。调用方补的是**投递侧**那句结论，
        info 刚好；而它把读者指到那档 warning 上，所以默认级别下这件事不会被漏掉。
        """
        from opencode_bridge.inbound_gateway import _record_inbound
        from opencode_bridge.hooks import Inbound

        inbox = self.open_inbox()
        inbox.close()
        inbound = Inbound(
            conversation_id=_CONVERSATION,
            text="关停期间掉下来的消息",
            platform=_PLATFORM,
            message_id=_MESSAGE_ID,
        )

        with self.assertLogs(_CALLER_LOGGER, level="INFO") as caller_view:
            with self.assertLogs(_INBOX_LOGGER, level="WARNING") as inbox_view:
                _record_inbound(inbox, inbound, inbound.text)

        self.assertEqual(len(caller_view.records), 1)
        self.assertEqual(
            caller_view.records[0].levelno, logging.INFO,
            "调用方是投递侧的补充说明，严重性归收件箱那一档（实际 %s）"
            % caller_view.records[0].levelname,
        )
        self.assertEqual(
            len(inbox_view.records), 1,
            "这条丢失在默认级别下必须仍然响亮：收件箱那条 warning 才是它的严重性来源",
        )
        self.assertEqual(inbox_view.records[0].levelno, logging.WARNING)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
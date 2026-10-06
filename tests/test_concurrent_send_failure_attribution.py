"""并发 ``send_observed`` 的**结果归属**：一次发送的结果不会被另一次抹掉。

它钉的缺陷：:attr:`~opencode_bridge.adapters.base.Adapter._last_send_error`
曾经是**适配器实例上的一个普通属性**（``0`` 处加锁），而
:meth:`~opencode_bridge.adapters.base.Adapter.send_observed` 的形态是
**「先清 → 再发 → 再读」** ⇒ 同一个实例上两次并发发送会互相抹掉结果。

## 两种后果（两个方向都红，缺一个就有一半恒真）

* **方向一 —— A 的失败被 B 的「先清」抹掉**：``A`` 的 ``send()`` 内部记了失败，
  ``B`` 的 :meth:`send_observed` 入口处那次「清」发生在 ``A`` 读之前 ⇒
  ``A`` 交出 ``ok=True, partial=False`` —— **部分送达被报成全成功**，
  ``outbound-failures.json`` 里什么都不留，而 ``--status``（用户唯一的自检入口）
  照常报正常。
* **方向二 —— B 的失败被记到 A 头上**：``B`` 记了失败，``A`` 在那之后读到它 ⇒
  一次**完全成功**的发送被写进失败记录。⚠️ **这一向更宽**：slack / mattermost 的
  分片之间会 sleep，一次 ``send()`` 能横跨几百毫秒。

⇒ 伤的是**这条通道自己的目的**：``outbound-failures.json`` 存在的意义就是消灭
「假的否定观测」，而共享槽让**假的肯定观测**出现 —— 完全成功的发送被记成失败、
部分失败的发送被记成成功。

## 这些用例的三条纪律

* ⛔ **全程不用 ``sleep``** —— 交错由 :class:`threading.Event` 钉死，每个会合点都有
  ``assertTrue(...)`` 兜底：会合没发生就是**编排坏了**，不能让用例悄悄退化成
  "没有并发"的情形（那正是缺陷修复前后都能过的情形）。
* ⚠️ **必须断言并发真的发生了**（两次 ``send()`` 的执行区间有重叠）——
  判据之一是 :attr:`CoordinatedSendAdapter.peak_concurrent_sends`，
  判据之二是两次执行区间的交集非空。⛔ 没有它们，下面的断言有可能只是在测
  「没有并发」。
* ⚠️ **判据钉的是返回值与槽的内容**（``ok`` / ``partial`` / ``error_kind`` /
  ``error_detail`` 与 ``_last_send_error``），⛔ 不是「没抛异常」。
"""

from __future__ import annotations

import threading
import unittest
from typing import Callable

from opencode_bridge.adapters.base import Adapter
from opencode_bridge.hooks import MsgHandle, Outbound, SendError

#: 线程会合的**上界**，只作为死锁保护（正常一次会合是微秒级）。
#: ⚠️ 必须有它：假如有人把整个 ``send_observed`` 用一把锁串行化，下面那些会合点就
#: 永远等不到 ⇒ 没有上界就是**挂死**，而不是一条红灯。
THREAD_TIMEOUT_SECONDS = 10.0

#: 两次并发发送各自用的会话键 —— 不同键 ⇒ 两个 ``send()`` 互不串话。
FAILING_CONVERSATION = "telegram:failing"
CLEAN_CONVERSATION = "telegram:clean"

#: 方向一里失败方记下的那句 —— 断言用的是**它自己的**分类与正文。
FAILURE_DETAIL = "chunk 2 failed"


class CoordinatedSendAdapter(Adapter):
    """``send()`` 行为可编程，且能**证明两次 send 真的一度重叠**。

    :attr:`behaviour` 把 ``conversation_id`` 映射到一个可调用对象，于是同一次测试里
    两个会话走两条不同的路（记失败 / 干净成功），而它们**共用一个适配器实例** ——
    那正是缺陷成立的前提。

    ## 为什么「重叠」用**进出台账**判，不用时间戳

    ⚠️ **时间戳在本机量不了这件事**（实测：本机 ``time.monotonic()`` 连续 **200000**
    次调用只取到 **1** 个不同的值 —— 它的分辨率约 15ms，而两次 ``send()`` 的重叠窗口
    是**微秒**级；``time.perf_counter()`` 的分辨率是 1e-07 但它也量的是墙上时间）。
    ⇒ 于是本类记录的是 :attr:`concurrency_ledger`：**进出 ``send()`` 的事件序列**
    （一条锁保护的单调序列）。"两个 ``send()`` 重叠"在这条序列上是一个**离散**事实
    —— 存在一个位置，B 的 ``enter`` 落在 A 的 ``enter`` 与 A 的 ``exit`` **之间** ——
    与时钟分辨率**无关**，因此不会在慢机器上假红，也不会在快机器上假绿。

    ⚠️ 这是**一次真实的踩坑**：第一版判据用 ``time.monotonic()`` 的进出时刻比区间
    交集，两边取到的是**同一个值**，于是 ``assertLess(a, b)`` 报 ``434569.687 not less
    than 434569.687``。⇒ 症状看着像"没重叠"，真因是**探针坏了**（§7.1）。
    """

    def __init__(self) -> None:
        super().__init__({}, hooks=None)  # type: ignore[arg-type]
        self.name = "telegram"
        self.label = "telegram"
        #: ``conversation_id -> send() 该做什么``（含等待与会合）。
        self.behaviour: dict[str, Callable[[Outbound], MsgHandle | None]] = {}
        #: 会合超时了的标签（正常必须是空的）—— 用它把「编排坏了」与「判据红了」分开。
        self.missed_rendezvous: list[str] = []
        #: 两次 ``send()`` 一度同时在跑的**最大**并发数（活跃计数）。
        self.peak_concurrent_sends = 0
        #: ``("enter"|"exit", conversation_id)`` 的事件序列，锁保护 ⇒ 全序。
        self.concurrency_ledger: list[tuple[str, str]] = []
        self._sends_in_flight = 0
        self._counter_lock = threading.Lock()
        self._handles_handed_out = 0

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def edit(self, handle: MsgHandle, out: Outbound) -> bool:
        return False

    def send(self, out: Outbound) -> MsgHandle | None:
        with self._counter_lock:
            self._sends_in_flight += 1
            self.peak_concurrent_sends = max(
                self.peak_concurrent_sends, self._sends_in_flight
            )
            self.concurrency_ledger.append(("enter", out.conversation_id))
        try:
            return self.behaviour[out.conversation_id](out)
        finally:
            with self._counter_lock:
                self._sends_in_flight -= 1
                self.concurrency_ledger.append(("exit", out.conversation_id))

    def a_handle_for(self, out: Outbound) -> MsgHandle:
        self._handles_handed_out += 1
        return MsgHandle(
            out.conversation_id, "m%d" % self._handles_handed_out, self.name
        )

    def wait_for_rendezvous(self, event: threading.Event, label: str) -> None:
        """等一个会合点；等不到就**记下来**而不是抛出去。

        ⚠️ 记下来而不是抛，是因为 :meth:`send` 里的异常会被
        :meth:`~opencode_bridge.adapters.base.Adapter.send_observed` 接住并变成一条
        ``ok=False`` 的结果 ⇒ 抛出去会让「编排坏了」显示成「发送失败」，把两件事
        混成同一个红灯。:attr:`missed_rendezvous` 让用例能分开说这两句话。
        """
        if not event.wait(THREAD_TIMEOUT_SECONDS):
            self.missed_rendezvous.append(label)


class ConcurrentSendOutcomeCase(unittest.TestCase):
    """共用：适配器实例、**重叠**判据、会合判据。"""

    def setUp(self) -> None:
        self.adapter = CoordinatedSendAdapter()

    def assert_both_rendezvous_points_were_reached(self) -> None:
        self.assertEqual(
            self.adapter.missed_rendezvous, [],
            "有会合点没到达 ⇒ 下面的交错压根没发生，钉的不是这条缺陷"
            "（也可能是实现把 send_observed 串行化了 —— 那同样会让这里空）",
        )

    @staticmethod
    def entry_and_exit_positions(
        ledger: list[tuple[str, str]], conversation: str
    ) -> tuple[int, int]:
        """某个会话的 ``(enter, exit)`` 在台账里的下标；缺一个就 ``AssertionError``。"""
        entered = [i for i, event in enumerate(ledger) if event == ("enter", conversation)]
        exited = [i for i, event in enumerate(ledger) if event == ("exit", conversation)]
        if len(entered) != 1 or len(exited) != 1:
            raise AssertionError(
                "会话 %s 在台账里的 enter/exit 不是恰好各一次"
                "（enter=%r exit=%r）⇒ 台账=%r" % (conversation, entered, exited, ledger)
            )
        return entered[0], exited[0]

    def assert_the_two_sends_really_overlapped(self) -> None:
        """⚠️ **反向护栏**：并发必须真的发生过，否则下面那些断言可能在测「没有并发」。

        三个判据，量的东西各不相同 —— 缺任何一条，另两条都可能在"其实没交错"的情形下
        通过（而那正是缺陷修复前后都能过的情形）：

        1. :attr:`peak_concurrent_sends` —— 活跃计数，量的是"**同时**在跑"；
        2. :attr:`concurrency_ledger` 里两段区间**相交** —— 量的是全序里
           "一段的开始落在另一段的区间内部"；
        3. 两个会话都真的进出过 —— 少一个就可能是"那一侧压根没跑"。

        ⚠️ 第 2 条是**离散**判据（⛔ 不看时钟）：见 :class:`CoordinatedSendAdapter`
        的 docstring —— 本机 ``time.monotonic()`` 的分辨率约 15ms，量不了微秒级重叠。
        ⚠️ 它**不假设谁先进入**：两个方向的用例里先进入的那一侧不同，而"相交"与
        顺序无关 —— 早先把顺序写死过一次，两条判据于是把其中一条用例判成"没重叠"，
        而台账明明白白写着两次 send 重叠（§7.1：先怀疑判据的宽度）。
        """
        self.assertGreaterEqual(
            self.adapter.peak_concurrent_sends, 2,
            "两次 send() 从没有同时在跑 ⇒ 用例没造成并发，"
            "下面钉的既可能是归属修对了、也可能是压根没交错",
        )
        ledger = self.adapter.concurrency_ledger
        entered = sorted(
            conversation for action, conversation in ledger if action == "enter"
        )
        self.assertEqual(
            entered, sorted([CLEAN_CONVERSATION, FAILING_CONVERSATION]),
            "有哪个 send() 没走到进入（或是进了两次）⇒ 编排坏了（台账=%r）" % (ledger,),
        )
        first_enter, first_exit = self.entry_and_exit_positions(
            ledger, FAILING_CONVERSATION
        )
        second_enter, second_exit = self.entry_and_exit_positions(
            ledger, CLEAN_CONVERSATION
        )
        self.assertLess(
            max(first_enter, second_enter), min(first_exit, second_exit),
            "两段 send() 的执行区间不相交 ⇒ 没重叠（台账=%r）" % (ledger,),
        )

    def assert_this_thread_saw(self, view: dict, kind: SendError | None) -> None:
        """断言**在发送方自己的线程上**读到的槽内容。

        ⚠️ 判据必须是「发送方那个线程读到的」：归属是**按线程**成立的，
        在主线程上读同一个槽量不到它（主线程压根没参与那次发送）。
        """
        slot = view["slot"]
        self.assertEqual(
            slot[0] if slot else None, kind,
            "发送方自己的线程读到的槽内容不对 —— 槽的归属仍然是错的",
        )


class OneSendsFailureSurvivesAnothersClear(ConcurrentSendOutcomeCase):
    """⭐⭐ **方向一**：A 的失败不许被 B 在入口那次「清」抹掉。

    **交错怎么钉的**（全程 Event，⛔ 不用 ``sleep``）::

        线程 A（失败方）                          线程 B（干净成功方）
        send_observed 入口：清 latest             send_observed 入口：清 latest  ← 抹掉 A 的
        send(): 记 TRANSIENT                       send(): set(clean_inside_send)
                 set(failing_noted)                        wait(release_clean)
        主线程等 failing_noted ⇒ 起 B              wait(clean_inside_send)
        主线程等 clean_inside_send                   ⇒ 此刻 A、B 同时在 send() 里（重叠已证）
        主线程 set(release_failing)                 ⇒ A 退出 send()，在 B 那次「清」之后才读
        A 退出 send() → 读 ⇒ 该看到 TRANSIENT / partial=True
    """

    def setUp(self) -> None:
        super().setUp()
        self.failing_noted = threading.Event()
        self.clean_inside_send = threading.Event()
        self.release_failing = threading.Event()
        self.release_clean = threading.Event()

        def failing_send(out: Outbound) -> MsgHandle | None:
            self.adapter._note_send_failure(SendError.TRANSIENT, FAILURE_DETAIL)
            self.failing_noted.set()
            self.adapter.wait_for_rendezvous(self.release_failing, "放行失败方")
            return self.adapter.a_handle_for(out)

        def clean_send(out: Outbound) -> MsgHandle | None:
            self.clean_inside_send.set()
            self.adapter.wait_for_rendezvous(self.release_clean, "放行成功方")
            return self.adapter.a_handle_for(out)

        self.adapter.behaviour = {
            FAILING_CONVERSATION: failing_send,
            CLEAN_CONVERSATION: clean_send,
        }
        self.failing_view: dict = {}
        self.clean_view: dict = {}

    def send_from_failing_side(self) -> None:
        result, raised = self.adapter.send_observed(
            Outbound(conversation_id=FAILING_CONVERSATION, text="hi")
        )
        self.failing_view.update(
            result=result, raised=raised, slot=self.adapter._last_send_error
        )

    def send_from_clean_side(self) -> None:
        result, raised = self.adapter.send_observed(
            Outbound(conversation_id=CLEAN_CONVERSATION, text="hi")
        )
        self.clean_view.update(
            result=result, raised=raised, slot=self.adapter._last_send_error
        )

    def run_the_pinned_interleaving(self) -> None:
        failing_thread = threading.Thread(
            target=self.send_from_failing_side, name="failing-sender"
        )
        clean_thread = threading.Thread(
            target=self.send_from_clean_side, name="clean-sender"
        )
        failing_thread.start()
        self.assertTrue(
            self.failing_noted.wait(THREAD_TIMEOUT_SECONDS),
            "失败方压根没走到「记下失败」那一步 ⇒ 交错没搭起来",
        )
        clean_thread.start()
        self.assertTrue(
            self.clean_inside_send.wait(THREAD_TIMEOUT_SECONDS),
            "成功方压根没进到 send() ⇒ 交错没搭起来",
        )
        # 此刻两个 send() 同时在跑 —— 上面那条重叠判据量到的就是这一刻。
        self.release_failing.set()
        failing_thread.join(THREAD_TIMEOUT_SECONDS)
        self.release_clean.set()
        clean_thread.join(THREAD_TIMEOUT_SECONDS)
        for thread in (failing_thread, clean_thread):
            self.assertFalse(
                thread.is_alive(),
                "线程 %s 没能在 %.0f 秒内结束 ⇒ 编排坏了（可能死锁）"
                % (thread.name, THREAD_TIMEOUT_SECONDS),
            )

    def test_the_failing_send_still_reports_its_own_partial_outcome(self):
        """A 的部分送达必须仍然报成 ``partial=True`` + **它自己的**分类与正文。"""
        self.run_the_pinned_interleaving()

        self.assert_both_rendezvous_points_were_reached()
        self.assert_the_two_sends_really_overlapped()

        result = self.failing_view["result"]
        self.assertIsNone(self.failing_view["raised"])
        self.assertTrue(result.ok, "A 交出了句柄 ⇒ ok 必须为真")
        self.assertIsNotNone(result.handle)
        self.assertTrue(
            result.partial,
            "A 的 send() 内部记过失败（%s）⇒ 结果必须是 partial=True。"
            "它报了 partial=False ⇒ 它的失败被并发的 B 在入口那次「清」抹掉了，"
            "而那条失败**根本没有落进 outbound-failures.json**" % FAILURE_DETAIL,
        )
        self.assertEqual(result.error_kind, SendError.TRANSIENT)
        self.assertIn(FAILURE_DETAIL, result.error_detail)
        self.assert_this_thread_saw(self.failing_view, SendError.TRANSIENT)

    def test_the_clean_send_is_not_charged_with_the_failing_sends_failure(self):
        """反向半边：B 干净成功，就不许带着 A 的失败。

        ⚠️ 这条与 :class:`SequentialSendsOnOneThreadDoNotLeakIntoEachOther` 是一对，
        缺一不可（这是实测出来的，见那个类的 docstring）：

        * **这条**钉的是"**别的线程**记下的失败不许被捡走"；
        * 那条钉的是"**上一次**（同线程）留下的失败不许被误读成这一次的"
          —— 它只靠入口那次「清」保证，而**并发交错里那次「清」永远看得见**
          （B 的清发生在 A 读之前）⇒ **只靠交错测不到它**。
        """
        self.run_the_pinned_interleaving()

        result = self.clean_view["result"]
        self.assertIsNone(self.clean_view["raised"])
        self.assertTrue(result.ok)
        self.assertIsNotNone(result.handle)
        self.assertFalse(
            result.partial,
            "B 干净成功却被判成部分送达（error_kind=%s detail=%s）⇒ 它捡到了"
            "**别的**发送记下的失败" % (result.error_kind, result.error_detail),
        )
        self.assertEqual(
            result.error_kind, SendError.UNKNOWN,
            "B 的失败槽必须是空的（UNKNOWN 是 ok 时的默认值）",
        )
        self.assert_this_thread_saw(self.clean_view, None)


class OneSendsCleanSendIsNotChargedWithOthersFailure(ConcurrentSendOutcomeCase):
    """⭐⭐ **方向二**：A 干净成功，不许被 B 的失败记到头上。

    **交错怎么钉的**::

        线程 A（干净成功方）                       线程 B（失败方）
        send_observed 入口：清 latest              send_observed 入口：清 latest
        send(): set(clean_inside_send)             send(): 记 TRANSIENT
                 wait(failing_noted)                        set(failing_noted)
        主线程等 clean_inside_send ⇒ 起 B           wait(failing_noted)
        主线程等 failing_noted ⇒ A 已在 B 记下之后退出 send()
        ⇒ A 在 B 的失败**仍然记着**的时候读 ⇒ 它必须仍然读到"什么都没有"
    """

    def setUp(self) -> None:
        super().setUp()
        self.clean_inside_send = threading.Event()
        self.failing_noted = threading.Event()
        self.release_failing = threading.Event()

        def clean_send(out: Outbound) -> MsgHandle | None:
            self.clean_inside_send.set()
            self.adapter.wait_for_rendezvous(self.failing_noted, "等失败方记下")
            return self.adapter.a_handle_for(out)

        def failing_send(out: Outbound) -> MsgHandle | None:
            self.adapter._note_send_failure(SendError.TRANSIENT, FAILURE_DETAIL)
            self.failing_noted.set()
            self.adapter.wait_for_rendezvous(self.release_failing, "放行失败方")
            return self.adapter.a_handle_for(out)

        self.adapter.behaviour = {
            CLEAN_CONVERSATION: clean_send,
            FAILING_CONVERSATION: failing_send,
        }
        self.clean_view: dict = {}
        self.failing_view: dict = {}

    def send_from_clean_side(self) -> None:
        result, raised = self.adapter.send_observed(
            Outbound(conversation_id=CLEAN_CONVERSATION, text="hi")
        )
        self.clean_view.update(
            result=result, raised=raised, slot=self.adapter._last_send_error
        )

    def send_from_failing_side(self) -> None:
        result, raised = self.adapter.send_observed(
            Outbound(conversation_id=FAILING_CONVERSATION, text="hi")
        )
        self.failing_view.update(
            result=result, raised=raised, slot=self.adapter._last_send_error
        )

    def run_the_pinned_interleaving(self) -> None:
        clean_thread = threading.Thread(
            target=self.send_from_clean_side, name="clean-sender"
        )
        failing_thread = threading.Thread(
            target=self.send_from_failing_side, name="failing-sender"
        )
        clean_thread.start()
        self.assertTrue(
            self.clean_inside_send.wait(THREAD_TIMEOUT_SECONDS),
            "干净方压根没进到 send() ⇒ 交错没搭起来",
        )
        failing_thread.start()
        self.assertTrue(
            self.failing_noted.wait(THREAD_TIMEOUT_SECONDS),
            "失败方压根没走到「记下失败」那一步 ⇒ 交错没搭起来",
        )
        clean_thread.join(THREAD_TIMEOUT_SECONDS)
        self.release_failing.set()
        failing_thread.join(THREAD_TIMEOUT_SECONDS)
        for thread in (clean_thread, failing_thread):
            self.assertFalse(
                thread.is_alive(),
                "线程 %s 没能在 %.0f 秒内结束 ⇒ 编排坏了（可能死锁）"
                % (thread.name, THREAD_TIMEOUT_SECONDS),
            )

    def test_the_fully_successful_send_is_not_written_into_the_failure_record(self):
        """A 完全成功 ⇒ 必须是 ``partial=False``，**一次成功的发送不许被记成失败**。

        ⚠️ 这一向是缺陷里**更宽**的一半：一次假的**肯定**失败会被写进
        ``outbound-failures.json``，而它是排障通道自己的数据 ⇒ 排障通道本身不可靠。
        """
        self.run_the_pinned_interleaving()

        self.assert_both_rendezvous_points_were_reached()
        self.assert_the_two_sends_really_overlapped()

        result = self.clean_view["result"]
        self.assertIsNone(self.clean_view["raised"])
        self.assertTrue(result.ok)
        self.assertFalse(
            result.partial,
            "A 完全成功却被判成部分送达（error_kind=%s detail=%s）⇒ 它的失败"
            "**来自另一次并发发送**。OutboundSender._record_outcome 读的就是 "
            "partial ⇒ 一次成功的发送会被写进 outbound-failures.json"
            % (result.error_kind, result.error_detail),
        )
        # ``error_detail`` 的默认值是 ``""``（``hooks.SendResult`` 的 dataclass 默认），
        # ⛔ 不是 ``None`` —— 判据钉的是"**空的**"，不是某个具体字面量之外的形态。
        self.assertEqual(result.error_detail, "")
        self.assertIsNone(result.retry_after)
        self.assert_this_thread_saw(self.clean_view, None)

        # ⛔ 下面这条是**反退化**判据：失败方自己那次必须仍然是 partial ——
        # 否则「干净方干净」有可能是因为**失败方那次整个没跑**。
        failing_result = self.failing_view["result"]
        self.assertTrue(failing_result.ok)
        self.assertTrue(
            failing_result.partial,
            "失败方那次自己没被判成部分送达 ⇒ 上面那条可能是恒真的",
        )
        self.assertEqual(failing_result.error_kind, SendError.TRANSIENT)
        self.assert_this_thread_saw(self.failing_view, SendError.TRANSIENT)


class SequentialSendsOnOneThreadDoNotLeakIntoEachOther(unittest.TestCase):
    """⭐ 入口那次「**清**」本身的护栏 —— ⛔ 它**测不到并发交错**。

    ## 为什么必须单独一组（这是实测出来的，不是推演）

    把实现换成「**按线程分槽、但忘了入口那次清、结论读共享槽**」——一个完全合理、
    极可能发生的改法——之后，:class:`OneSendsFailureSurvivesAnothersClear` 与
    :class:`OneSendsCleanSendIsNotChargedWithOthersFailure` **两个类全绿**
    ⇒ 并发那几条**测不到**那次「清」。

    真因（已定位，⛔ 不是探针坏了）：并发交错里 **B 的入口「清」总是发生在 A 读之前**
    ⇒ 无论清不清，A 都读不到 B 的失败 ⇒ 「清」在交错下**不可观测**。

    而「清」真正承重的地方是**同线程先后两次发送**：上一次失败、下一次干净时，
    不清就会把上一次的失败读成这一次的 ⇒ 一次**完全成功**的发送被记成失败。

    ⚠️ 这个"残留"是**真实存在**的，不是假想：:attr:`last_send_error` 的 docstring
    明写「:meth:`_note_send_failure` **只写不清**」（直接调 ``send()`` / ``edit()``
    记下的失败会一直留在 :attr:`_ThreadOwnedSendFailures.latest` 上）
    ⇒ 今天挡住它的**只有** ``send_observed`` 入口那一行。⇒ 那一行必须有人钉。
    """

    CONVERSATION = "telegram:sequential"

    def build_adapter_that_alternates(self):
        """返回一个适配器：``should_fail["now"]`` 为真时记一次失败，否则干净成功。"""
        adapter = CoordinatedSendAdapter()
        should_fail = {"now": True}

        def send(out: Outbound) -> MsgHandle | None:
            if should_fail["now"]:
                adapter._note_send_failure(SendError.TRANSIENT, FAILURE_DETAIL)
            return adapter.a_handle_for(out)

        adapter.behaviour = {self.CONVERSATION: send}
        return adapter, should_fail

    def test_the_second_clean_send_on_this_thread_is_not_charged_either(self):
        adapter, should_fail = self.build_adapter_that_alternates()
        out = Outbound(conversation_id=self.CONVERSATION, text="hi")

        first = adapter.send_result(out)
        self.assertTrue(first.partial)
        self.assertEqual(first.error_kind, SendError.TRANSIENT)

        should_fail["now"] = False
        second = adapter.send_result(out)

        self.assertTrue(second.ok)
        self.assertIsNotNone(second.handle)
        self.assertFalse(
            second.partial,
            "同线程第二次发送干净成功，却被报成部分送达"
            "（error_kind=%s detail=%s）⇒ 入口那次「清」没了，上一次留下的失败"
            "被读成了这一次的 ⇒ 一次成功的发送会被写进 outbound-failures.json"
            % (second.error_kind, second.error_detail),
        )
        self.assertEqual(second.error_kind, SendError.UNKNOWN)
        self.assertEqual(second.error_detail, "")
        self.assertIsNone(adapter._last_send_error)

    def test_the_pattern_holds_across_alternating_sends(self):
        """⛔ 不止第二次 —— 残留会累积，"只清第一次"修不好"每次都清"。"""
        adapter, should_fail = self.build_adapter_that_alternates()
        out = Outbound(conversation_id=self.CONVERSATION, text="hi")

        for attempt in range(1, 5):
            with self.subTest(send_number=attempt):
                result = adapter.send_result(out)
                if should_fail["now"]:
                    self.assertTrue(result.partial)
                    self.assertEqual(result.error_kind, SendError.TRANSIENT)
                else:
                    self.assertFalse(
                        result.partial,
                        "第 %d 次干净发送被上一次残留判成部分送达（%s / %s）"
                        % (attempt, result.error_kind, result.error_detail),
                    )
                    self.assertEqual(result.error_detail, "")
                should_fail["now"] = not should_fail["now"]


class NestedSendKeepsEachObservationsOwnOutcome(ConcurrentSendOutcomeCase):
    """同线程**嵌套**的 ``send_observed``：内层那次不许抹掉外层那次的失败。

    ⚠️ 仓库里当前没有这种调用（各适配器在 ``send()`` 内部只调 ``send()``，不调
    ``send_observed``）。它之所以要钉，是因为归属必须由**结构**保证而不是靠"目前
    没人这么调"：归属槽只有一份的话，一个在 ``send()`` 里调 ``send_result()`` 的
    适配器（基类文档明确鼓励子类覆写它）会让外层的结论被内层改写，而那与并发那条
    是同一类假观测。

    ## 顺序是**承重的**（这条判据量的就是它）

    ⚠️ **必须「外层先记、再调内层」**，反过来的话判据恒真：外层在**内层之后**又自己
    记了一次，于是无论归属槽是栈还是单槽，外层读到的都是它自己那条 ⇒ 两种实现在
    这里给出**相同**的输出，测不出任何东西。

    实测过这一版反序：它在本修复（栈）与"退化成一个共享槽"两种实现下**都绿**
    ⇒ 那是一条装饰性用例。这条修正由"把栈退化成单槽必须变红"这个判据反退化检查
    逼出来（⚠️ 顺序错了，探针会给出"退化后仍全绿"的结论，而真因是判据量不到差异，
    不是退化无效 —— §7.1）。
    """

    def test_the_outer_observation_is_not_rewritten_by_the_inner_one(self):
        adapter = self.adapter
        self.inner_result = None

        def inner_send(out: Outbound) -> MsgHandle | None:
            adapter._note_send_failure(SendError.TRANSIENT, FAILURE_DETAIL)
            return adapter.a_handle_for(out)

        def outer_send(out: Outbound) -> MsgHandle | None:
            # ① 外层**先**记自己那条（分片 1 失败）；
            adapter._note_send_failure(SendError.RATE_LIMITED, "outer chunk 1 failed")
            # ② 再嵌套发剩下那条（"把余下的重发一遍"是这个形态的正当动机）。
            self.inner_result = adapter.send_result(
                Outbound(conversation_id=FAILING_CONVERSATION, text="inner")
            )
            return adapter.a_handle_for(out)

        adapter.behaviour = {CLEAN_CONVERSATION: outer_send,
                             FAILING_CONVERSATION: inner_send}

        outer_result = adapter.send_result(
            Outbound(conversation_id=CLEAN_CONVERSATION, text="outer")
        )

        # 内层那次判的是它自己记下的那条 —— 两个方向都钉，免得只钉一半恒真。
        self.assertIsNotNone(self.inner_result)
        self.assertTrue(self.inner_result.partial)
        self.assertEqual(self.inner_result.error_kind, SendError.TRANSIENT)
        self.assertIn(FAILURE_DETAIL, self.inner_result.error_detail)

        self.assertTrue(outer_result.partial)
        self.assertEqual(
            outer_result.error_kind, SendError.RATE_LIMITED,
            "外层交出了它**自己**记下的 RATE_LIMITED，却读到了内层的 TRANSIENT ⇒ "
            "归属被内层改写了（两个 send_observed 共用了一个槽）",
        )
        self.assertIn("outer chunk 1 failed", outer_result.error_detail)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

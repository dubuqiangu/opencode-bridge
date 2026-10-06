"""缺陷 A 的**确定性交错**：入站侧与事件流侧在同一个 turn 上争抢
``Turn.progress_handle``。

⚠️ **这 13 条现在是全绿的** —— 缺陷已按「每轮一个创建权」修好，下面 :class:`NoClaimHeldCreatesAsUsualTests`
那两条反向护栏就是「不是恒真」的证据。本文件仍是那个缺陷的守门人：谁把「创建权」
去掉，这 6 条行为断言会立刻变红（症状见下面两段里那两个 transcript）。

## 被钉住的机制（缺陷当年已复现，这里只把它变成可重复的交错）

:meth:`~opencode_bridge.inbound_gateway.InboundGateway._dispatch_prompt` 在
「读到 ``turn.progress_handle is None``」与「把句柄写回 ``_turns``」之间夹着
**一次完整的 :meth:`~opencode_bridge.adapters.base.Adapter.send`**，而那一步
**在锁外**::

    with self._lock:
        need_progress = turn.progress_handle is None       # ← 锁内读到
    if need_progress:
        handle = self._send_text(..., PROGRESS_TEXT, ...)  # ← 锁外：一次完整 send
        with self._lock:
            if current.progress_handle is None:            # ← 条件式写回
                current.progress_handle = handle

:func:`~opencode_bridge.event_stream.EventStream._on_text_delta` 是同一副骨架
（锁内读、锁外发、条件式写回）。⇒ 第一个 ``session.text.delta`` 落进那个窗口时，
两处各自的 ``is None`` **都会通过**，于是**各发一条消息**，而**其中一个句柄被丢掉**。

## 修法：每轮一个**创建权**（``Turn.progress_message_creating``）

那个窗口不能靠「发完再回填」修好 —— 判据本身（``progress_handle is None``）在
锁外那段时间里必然为假。所以创建权被**单独**领出来：锁内置 ``True``、**锁外**发、
``finally`` 里归还。**没领到的那一路让出，不另发一条**（正文早已记在
``turn.parts`` 里，让出的是**消息**不是**正文**）。

⇒ 两个竞态形态断言的正是这件事：「**别人持创建权且它的 send 仍在飞行** ⇒ 本路
**不创建**第二条进度消息」。⇒ 那是**带前提**的断言，所以
:class:`NoClaimHeldCreatesAsUsualTests` 用同一个谓词取反前提、断言相反结论。

## 为什么「节流」挡不住（:class:`ThrottleWindowTests` 把它断言下来）

:meth:`~opencode_bridge.event_stream.EventStream._on_execution_started` 把
``turn.last_edit_ts`` 置 ``0.0``，而 ``clock`` 是 :func:`time.monotonic`
⇒ ``now - 0.0`` **恒大于** ``edit_interval``。:class:`~opencode_bridge.event_stream.Turn`
的字段默认值也是 ``0.0``，所以有没有 ``session.execution.started`` 结论一样。

## 为什么现有那 5 条 ``progress_handle`` 断言全都测不到

它们**全是单线程**的：要么先跑完 ``_dispatch_prompt`` 再喂 delta，要么先手工把
``turn.progress_handle`` 赋好再喂 delta。两条路都**没有第二个线程**去挤那个窗口
⇒ 缺陷活在那条缝里，不在任何一条断言的射程内。

## 交错怎么被钉成确定性的（⛔ 没有 sleep）

:meth:`InterleavingAdapter.send` 在**指定的那段正文**上 ``Event.set()`` 然后
``Event.wait()``。⇒ 「某一次 send 还在飞行中」从一个概率变成一个**可判定的状态**。
每个用例还带一条 :meth:`InterleavingAdapter.markers` 的**逐字**比对
（:meth:`assert_interleaving`）⇒ 「同步点其实没落在窗口里」会当场报错，
而不是悄悄变绿。

⛔ **只注入替身**（适配器）；``event_stream.py`` 与 ``inbound_gateway.py``
一行未改。
"""

from __future__ import annotations

import os
import tempfile
import threading
import unittest

from opencode_bridge.adapters import Adapter
from opencode_bridge.config import Config
from opencode_bridge.core import BridgeCore
from opencode_bridge.event_stream import Turn
from opencode_bridge.hooks import Inbound, MsgHandle, Outbound
from opencode_bridge.inbound_gateway import PROGRESS_TEXT
from opencode_bridge.state import StateStore
from tests.test_core import FakeClient

CONVERSATION = "chat:55"
PLATFORM = "telegram"
ASSISTANT_ID = "msg_1"
USER_TEXT = "看一下 README"

#: 这一轮的第一片。它同时是「读者读到的第一截正文」与「孤儿消息的内容」。
FIRST_FRAGMENT = "Hello"
#: 剩下两片。它们只是让这一轮像个真实的流式回答；**是否被节流挡掉不影响任何
#: 一条断言** —— 收尾那一步会把整条答复写进 turn 认领的那条消息。
MIDDLE_FRAGMENT = " world"
LAST_FRAGMENT = " today"
#: 读者最终**应该**读到的全部正文。它短到装得进一条消息（预算见
#: :data:`~opencode_bridge.core.DEFAULT_MAX_MESSAGE_CHARS`），所以「正文重复」
#: 只可能是同一轮里多发了一条消息造成的，不可能是分片造成的。
ANSWER = FIRST_FRAGMENT + MIDDLE_FRAGMENT + LAST_FRAGMENT

#: 交用的超时。它只是**死锁保护**（拿不到就报错，绝不静默放行），不是用来撞
#: 概率的时长：交错顺序全部由 :class:`threading.Event` 决定。
RENDEZVOUS_TIMEOUT_SECONDS = 10.0

#: 本 lane 自己的 scratch 目录。⛔ 只在 ``.tmp/`` 底下开这一个子目录，绝不碰
#: ``.tmp/`` 本身（那是共享目录，见 AGENTS.md §9）。
_LANE_TEMP = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".tmp", "progress-handle-handoff-race",
)

#: 事件日志里的记号。「哪个记号在前」就是交错的顺序本身。
SEND_ENTERED = "send-entered"
SEND_RETURNED = "send-returned"
FRAME_DISPATCHED = "frame-dispatched"


def frame(event_type: str, **data) -> dict:
    return {"type": event_type, "data": data}


def first_frame(session_id: str) -> dict:
    return frame(
        "session.text.delta", sessionID=session_id,
        assistantMessageID=ASSISTANT_ID, ordinal=0, delta=FIRST_FRAGMENT,
    )


def follow_up_frame(session_id: str, ordinal: int, delta: str) -> dict:
    return frame(
        "session.text.delta", sessionID=session_id,
        assistantMessageID=ASSISTANT_ID, ordinal=ordinal, delta=delta,
    )


class InterleavingAdapter(Adapter):
    """真 :class:`Adapter` 子类；``send()`` 可被钉在指定的那段正文上。

    **它必须是真的 ``Adapter``**：:func:`~opencode_bridge.channel_profile.with_channel_hint`
    按适配器的能力声明拼渠道说明，裸 ``object()`` 会在那里抛
    ``AttributeError``（理由抄 ``tests/test_inbound_gateway.py`` 的
    :class:`StandInAdapter`）。

    ## 记下的三样东西各自回答一个判据问题

    * :attr:`markers` —— 交错**按发生顺序**的逐字日志（证明同步点落在窗口里）；
    * :attr:`delivered` —— 读者**看得见**的消息，按送达顺序（回答「发了几条」）；
    * :attr:`displayed` —— 每条消息**当前显示**的正文（回答「读者最后读到什么」）。
    """

    name = PLATFORM
    label = "Telegram"
    max_message_length = 4000
    supports_message_edit = True
    supports_inbound = True

    def __init__(self, *, park_on_text: str | None) -> None:
        super().__init__({}, hooks=None)  # type: ignore[arg-type]
        self.park_on_text = park_on_text
        #: 「某次 send 已经进入、还没返回」。主线程等它，再放行 —— 唯一的同步点。
        self.park_entered = threading.Event()
        self.park_release = threading.Event()
        self.markers: list[tuple[str, str]] = []
        self.delivered: list[tuple[str, Outbound]] = []
        self.displayed: dict[str, str] = {}
        self.handles: dict[str, MsgHandle] = {}
        self._message_sequence = 0

    # --- lifecycle -----------------------------------------------------
    def start(self) -> None:
        return None

    # --- 交错的那一点 --------------------------------------------------
    def send(self, out: Outbound) -> MsgHandle | None:
        self.markers.append((SEND_ENTERED, out.text))
        if out.text == self.park_on_text:
            self.park_entered.set()
            #: 阻塞而不是 sleep：这一步制造的是「send 还在飞行中」这个**状态**，
            #: 顺序由 :class:`~threading.Event` 决定，与机器快慢无关。
            self.park_release.wait(timeout=RENDEZVOUS_TIMEOUT_SECONDS)
        self._message_sequence += 1
        message_id = "m%d" % self._message_sequence
        #: 先记账再放行 —— 忠实于「消息已经在平台上，只是 API 调用还没返回」。
        self.delivered.append((message_id, out))
        self.displayed[message_id] = out.text
        self.markers.append((SEND_RETURNED, message_id))
        handle = MsgHandle(
            conversation_id=out.conversation_id, message_id=message_id,
            platform=self.name,
        )
        self.handles[message_id] = handle
        return handle

    def edit(self, handle: MsgHandle, out: Outbound) -> bool:
        self.displayed[handle.message_id] = out.text
        return True

    # --- 判据用的小帮手 ------------------------------------------------
    def progress_messages(self) -> list[str]:
        """读者收到的 ``kind="progress"`` 消息 id，按送达顺序。"""
        return [
            message_id for message_id, out in self.delivered
            if out.kind == "progress"
        ]

    def reader_transcript(self) -> list[str]:
        """读者连着读下去看到的东西：每条消息**当前**显示的正文，按送达顺序。"""
        return [self.displayed[message_id] for message_id, _ in self.delivered]


class ProgressHandleHandoffTestCase(unittest.TestCase):
    """一套**真装配**（:class:`BridgeCore`）—— 缺陷就住在 core 注入的那条缝里。

    ⚠️ 这里用 :class:`BridgeCore` 而不是把两个协作者各搭一套：那个缺陷的前提正是
    「事件流与入站那一侧**共用同一个** ``turns`` dict 与同一把锁」。
    手工搭一套等价环境等于把那个前提**假设**掉了 —— 前提哪天消失，测试会静悄悄
    失效。:meth:`test_the_two_collaborators_really_share_one_turn_table` 把前提本身
    钉住。
    """

    def setUp(self) -> None:
        os.makedirs(_LANE_TEMP, exist_ok=True)
        self.tempdir = tempfile.TemporaryDirectory(dir=_LANE_TEMP)
        self.addCleanup(self.tempdir.cleanup)
        self.client = FakeClient()
        self.core = BridgeCore(
            Config(), self.client,
            StateStore(os.path.join(self.tempdir.name, "state.json")),
        )
        self.addCleanup(self.core.stop)
        self.turns = self.core._turns
        self.lock = self.core._lock
        self.workers: list[threading.Thread] = []
        self.addCleanup(self.release_every_park)

    # --- 环境搭建 ------------------------------------------------------
    def build_race(self, *, park_on_text: str | None = None) -> InterleavingAdapter:
        """装一个**会卡住某一次 send** 的适配器，并把出站那条路换成真的。

        出站（``send_text`` / ``edit_progress`` / ``finalize``）用的是真
        :class:`~opencode_bridge.outbound.OutboundSender` ⇒ 进度闸门、长度闸门、
        收尾补发全都是生产代码；替身只站在平台那一侧。
        """
        self.adapter = InterleavingAdapter(park_on_text=park_on_text)
        self.core.attach(self.adapter)
        self.core.event_stream._adapter_for = lambda conversation_id: self.adapter
        self.core.outbound._adapter_for = lambda conversation_id: self.adapter
        #: 「还没有 turn」就不可能有这场竞态。
        self.assertEqual(self.turns, {})
        return self.adapter

    def release_every_park(self) -> None:
        """⛔ 绝不让被钉住的线程留在飞行中（否则整轮跑测会挂住）。"""
        if not hasattr(self, "adapter"):
            return
        self.adapter.park_release.set()
        for worker in self.workers:
            worker.join(timeout=RENDEZVOUS_TIMEOUT_SECONDS)

    # --- 驱动 ----------------------------------------------------------
    def start_worker(self, target, label: str) -> threading.Thread:
        failures: list[BaseException] = []

        def runner() -> None:
            try:
                target()
            except BaseException as exc:  # noqa: BLE001 - 原样交回主线程判定
                failures.append(exc)

        worker = threading.Thread(target=runner, name=label, daemon=True)
        worker.failures = failures  # type: ignore[attr-defined]
        self.workers.append(worker)
        worker.start()
        return worker

    def join_worker(self, worker: threading.Thread) -> None:
        """等**这一条** worker 结束。

        ⛔ 形态一在放开 park **之前**就需要它：那一侧只断言「这一帧没有发出第二条
        消息」，而这条断言**必须在帧真的被处理过之后**才成立 —— 否则它会因为
        「worker 还没跑到」而**恒真**（这正是恒真那一侧的逃逸口）。
        """
        worker.join(timeout=RENDEZVOUS_TIMEOUT_SECONDS)
        self.assertFalse(
            worker.is_alive(),
            "worker %r 没有结束 —— 交错被死锁了，而不是被钉住了" % worker.name,
        )
        self.assertEqual(
            getattr(worker, "failures", []), [], "worker %r 抛了异常" % worker.name,
        )

    def join_workers(self) -> None:
        for worker in self.workers:
            self.join_worker(worker)

    def wait_for_park(self) -> None:
        self.assertTrue(
            self.adapter.park_entered.wait(timeout=RENDEZVOUS_TIMEOUT_SECONDS),
            "适配器的 send() 从未进入被钉住的那一段 —— 交错没有开始。markers=%r"
            % (self.adapter.markers,),
        )

    def inbound(self) -> Inbound:
        return Inbound(
            conversation_id=CONVERSATION, text=USER_TEXT, kind="text",
            platform=PLATFORM, message_id="m-inbound-1",
        )

    def dispatch(self, event: dict) -> None:
        self.core.event_stream.dispatch(event)

    def dispatch_first_frame_on_a_worker(self, session_id: str) -> threading.Thread:
        def deliver() -> None:
            self.adapter.markers.append((FRAME_DISPATCHED, FIRST_FRAGMENT))
            self.dispatch(first_frame(session_id))

        return self.start_worker(deliver, "sse-first-frame")

    def finish_the_turn(self, session_id: str) -> None:
        """把这一轮**剩下**的事件喂完，让收尾那一步真的执行。

        ⚠️ 首片那一帧由调用方自己投 —— 两种形态的差别**恰恰是首片落在哪一刻**，
        所以它不能藏在这个帮手里面。
        """
        self.dispatch(follow_up_frame(session_id, 1, MIDDLE_FRAGMENT))
        self.dispatch(follow_up_frame(session_id, 2, LAST_FRAGMENT))
        self.dispatch(frame("session.execution.succeeded", sessionID=session_id))

    # --- 判据用的断言 --------------------------------------------------
    def assert_interleaving(self, expected: list[tuple[str, str]]) -> None:
        """逐字比对交错顺序 —— 「同步点没落在窗口里」会在这里当场报错。"""
        self.assertEqual(self.adapter.markers, expected)

    def assert_the_turn_was_really_dispatched(self, session_id: str) -> None:
        """前提：会话真建了、prompt 真发出去了、turn 真建了。

        ⛔ 不是装饰 —— ``_dispatch_prompt`` 的上游把异常全吞掉（``on_inbound`` 有
        ``except Exception``），少了这几条，一个**炸了**的投递会安静地让本文件
        的其余断言变成「测了一条没跑过的代码路径」。
        """
        self.assertEqual(self.client.created_ids, [session_id])
        self.assertEqual(len(self.client.prompts), 1)
        self.assertIn(session_id, self.turns)


class SharedTurnTableTests(ProgressHandleHandoffTestCase):
    """缺陷的**前提**本身：事件流与入站那一侧拿到的**是同一个对象**。

    ⛔ 这条是绿的，它钉的是「那条缝真的存在」。少了它，手工接线与生产接线的
    一处差异就会让本文件的所有用例**静悄悄**失去意义（症状是：全都绿）。
    """

    def test_the_two_collaborators_really_share_one_turn_table(self):
        self.build_race()

        self.assertIs(self.core.event_stream._turns, self.turns)
        self.assertIs(self.core.inbound_gateway._turns, self.turns)
        self.assertIs(self.core.event_stream._lock, self.lock)
        self.assertIs(self.core.inbound_gateway._lock, self.lock)

    def test_one_turn_table_means_one_turn_object_for_both_sides(self):
        """不是「两份等价的表」，是**同一个 dict 里的同一个 Turn**。"""
        self.build_race()
        self.core.on_inbound(self.inbound())
        session_id = self.client.created_ids[0]

        self.assertIs(
            self.core.inbound_gateway._turns[session_id],
            self.core.event_stream._turns[session_id],
        )


class SequentialDeliveryControlTests(ProgressHandleHandoffTestCase):
    """⭐ 反退化对照组：把同步点**挪到窗口之外** ⇒ 三条判据全绿。

    帧落在占位句柄写回**之后**时，那场竞态根本不成立，于是同一批断言必须全过
    ⇒ 证明 :class:`FragmentWinsTheHandleTests` 与
    :class:`PlaceholderWinsTheHandleTests` 里红的那几条，量的确实是**那个窗口**，
    而不是某个恒真的东西。

    ⚠️ 顺带这也是「交错真的发生了」的反面证明：这里
    :attr:`InterleavingAdapter.markers` 只有**一条** ``send-entered``。
    """

    def test_the_pins_hold_when_the_frame_arrives_after_the_placeholder_is_adopted(self):
        self.build_race()

        self.core.on_inbound(self.inbound())
        session_id = self.client.created_ids[0]
        turn = self.turns[session_id]
        self.assert_the_turn_was_really_dispatched(session_id)
        #: ⛔ 帧序列与 :class:`FragmentWinsTheHandleTests` **完全一样**，只是投递时机
        #: 从「占位 send 还在飞行中」挪到了「占位句柄已经写回之后」。
        self.adapter.markers.append((FRAME_DISPATCHED, FIRST_FRAGMENT))
        self.dispatch(first_frame(session_id))
        self.finish_the_turn(session_id)
        #: ⛔ 逐字比对给出的是**最强的反面证明**：占位消息的 send 已经**返回**
        #: （句柄已写回），首片帧才被投递 —— 两件事根本没有交错。
        self.assertEqual(
            self.adapter.markers,
            [
                (SEND_ENTERED, PROGRESS_TEXT),
                (SEND_RETURNED, "m1"),
                (FRAME_DISPATCHED, FIRST_FRAGMENT),
            ],
        )

        # ① 正文只被读到一次
        self.assertEqual(self.adapter.reader_transcript(), [ANSWER])
        # ② turn 认领的句柄涵盖这一轮发出去的每一条进度消息
        self.assertEqual(
            set(self.adapter.progress_messages()), {turn.progress_handle.message_id},
        )
        # ③ 没有第二条进度消息，也没有停在占位文案上的僵尸气泡
        self.assertEqual(len(self.adapter.progress_messages()), 1)
        self.assertNotIn(PROGRESS_TEXT, self.adapter.reader_transcript())


class FragmentWinsTheHandleTests(ProgressHandleHandoffTestCase):
    """形态一：**占位那一路持有创建权** ⇒ 首片让出，本轮只有一条进度消息。

    窗口被钉在「``_dispatch_prompt`` 已领到创建权、正在 ``send(PROGRESS_TEXT)``
    里、句柄尚未写回」。此时 SSE 线程投递第一片 ``session.text.delta``。

    **修复后**（每轮一个创建权）：事件流这一帧看到「有人正在创建这一轮的进度消息
    且它的 send 还在飞行」⇒ **让出**，不另发一条（正文早已记在 ``turn.parts`` 里，
    不丢）。放行之后占位消息承载整条答复 ⇒ 读者只读到一份。

    ⚠️ 这一形态当年红的是「占位消息成了僵尸气泡 + 多出一条首片消息」。
    """

    def setUp(self) -> None:
        super().setUp()
        self.build_race(park_on_text=PROGRESS_TEXT)
        worker = self.start_worker(
            lambda: self.core.on_inbound(self.inbound()), "inbound-dispatch",
        )
        self.wait_for_park()
        self.session_id = self.client.created_ids[0]
        self.turn = self.turns[self.session_id]
        #: 窗口是开着的：句柄还没写回。事件流读到的 ``progress_handle`` 只能也是
        #: ``None``（它读得更早）—— 这两条把「同步点落在窗口里」钉成事实。
        self.assertIn(self.session_id, self.turns)
        self.assertIsNone(self.turns[self.session_id].progress_handle)
        self.assertEqual(self.adapter.delivered, [])
        #: ⭐ 「本路让出」**只在别人持创建权时**成立 ⇒ 先把那个前提钉住。
        #: 反向护栏见 :class:`NoClaimHeldCreatesAsUsualTests`（那里同一个谓词在
        #: ``progress_message_creating`` 为假时给出**相反**的结论）。
        self.assertTrue(
            self.turn.progress_message_creating,
            "入站那一路没有持有创建权 —— 「让出」就没有前提，这条会变成恒真。",
        )
        frame_worker = self.dispatch_first_frame_on_a_worker(self.session_id)
        #: ⛔ 必须先**等这一帧真的被处理过**，否则「没有发出第二条」会因为
        #: 「worker 还没跑到」而恒真。
        self.join_worker(frame_worker)
        #: ⭐ 修复后的行为：占位那一路的 send 仍在飞行 ⇒ 首片这一帧**不另发一条**。
        self.assertEqual(
            self.adapter.delivered, [],
            "占位消息的 send 还在飞行中，首片这一帧却另外发了一条：%r"
            % ([out.text for _, out in self.adapter.delivered],),
        )
        #: 让出的是**消息**，不是**正文**：这一帧的内容仍在 turn 里。
        self.assertEqual(self.turn.assemble(), FIRST_FRAGMENT)
        self.adapter.park_release.set()
        self.join_workers()
        self.assert_the_turn_was_really_dispatched(self.session_id)
        #: ⭐ 只有**一对** send —— 首片那条**根本没有发出去**。
        #: 交错证据没有被削弱：``frame-dispatched`` 落在占位 send 的
        #: ``send-entered`` 与 ``send-returned`` **之间**（帧在 send 飞行中到达）。
        self.assert_interleaving([
            (SEND_ENTERED, PROGRESS_TEXT),
            (FRAME_DISPATCHED, FIRST_FRAGMENT),
            (SEND_RETURNED, "m1"),
        ])

    # --- ① 用户最终读到的正文不能重复 ----------------------------------
    def test_the_answer_body_reaches_the_reader_exactly_once(self):
        """读者读到的必须是**一条**消息上的那份正文。"""
        self.finish_the_turn(self.session_id)

        self.assertEqual(
            self.adapter.reader_transcript(), [ANSWER],
            "读者读到的是 %r —— 同一轮的正文出现了不止一次。"
            % (self.adapter.reader_transcript(),),
        )
        #: 让出不能以「丢正文」为代价：那一帧虽然没发出去，内容仍在 turn 里，
        #: 而收尾那一步把它写进了 turn 认领的那条消息。
        self.assertEqual(self.turn.assemble(), ANSWER)

    # --- ② 同一轮只能有一个 progress_handle ----------------------------
    def test_one_turn_owns_exactly_one_progress_handle(self):
        """turn 认领的句柄必须**涵盖**这一轮发出去的每一条进度消息。

        ⛔ 刻意不是「条数 == 1」（那是 ③）：这条钉的是**归属** —— 有一条消息拿到
        了句柄却没人认领它，于是它成了读者看得见、却永远不会被改写的孤儿。
        """
        progress_messages = self.adapter.progress_messages()

        self.assertEqual(
            set(progress_messages), {self.turn.progress_handle.message_id},
            "这一轮发出了 %d 条进度消息（%r），而 turn 只认领了 %r —— 多出来的那条"
            "成了孤儿：读者看得见它，而它永远不会被答复改写。"
            % (len(progress_messages), progress_messages,
               self.turn.progress_handle.message_id),
        )

    # --- ③ 首片与占位消息不会各发一条 ----------------------------------
    def test_the_placeholder_and_the_first_fragment_do_not_both_send_a_message(self):
        """僵尸气泡那个形态。"""
        self.finish_the_turn(self.session_id)

        self.assertEqual(
            len(self.adapter.progress_messages()), 1,
            "占位消息与首片各发了一条：%r" % (self.adapter.progress_messages(),),
        )
        #: 这个形态最刺眼的症状：一条永远停在占位文案上的气泡。平台没有
        #: 「撤回」原语，它清不掉。
        self.assertNotIn(
            PROGRESS_TEXT, self.adapter.reader_transcript(),
            "有一条消息永远停在占位文案上。",
        )


class PlaceholderWinsTheHandleTests(ProgressHandleHandoffTestCase):
    """形态二：**首片那一路持有创建权** ⇒ 入站侧让出，本轮只有一条进度消息。

    窗口被钉在「``_on_text_delta`` 已领到创建权、正在 ``send(FIRST_FRAGMENT)`` 里、
    句柄尚未写回」。此时入站侧在**同一把锁上畅通无阻**地跑完 ``_dispatch_prompt``。

    **修复后**：入站侧看到「有人正在创建这一轮的进度消息」⇒ **不发**占位消息
    （占位文案本身没有信息量，正文一直记在 ``turn.parts`` 里）。放行之后首片那条
    承载整条答复 ⇒ 读者只读到一份。

    ⚠️ 这一形态当年红的是「读者把同一段正文读了两遍」
    （``['Hello world today', 'Hello']``）。
    """

    def setUp(self) -> None:
        super().setUp()
        #: 先登记会话（生产路径：:meth:`SessionRegistry.ensure_session`）。
        #: 事件流要认领这一帧，靠的就是 ``state.json`` 里的这张表。
        self.session_id = self.core.sessions.ensure_session(
            CONVERSATION, platform=PLATFORM,
        )
        self.build_race(park_on_text=FIRST_FRAGMENT)
        #: 服务端一收到 prompt 就发「这一轮开始了」—— 它建 turn，并把
        #: ``last_edit_ts`` 置 0.0（节流因此挡不住首片，见 ThrottleWindowTests）。
        self.dispatch(frame("session.execution.started", sessionID=self.session_id))
        self.assertEqual(self.turns[self.session_id].last_edit_ts, 0.0)
        self.turn = self.turns[self.session_id]
        worker = self.dispatch_first_frame_on_a_worker(self.session_id)
        self.wait_for_park()
        #: 首片已经**决定要发**（``send-entered`` 已出现），却还没发出去 ——
        #: 事件流读到的 ``handle`` 只能是 ``None``，而 turn 也确实还没有句柄。
        #: ⚠️ 这两条**逐字未动**：它们是「交错真的落在窗口里」的证据。
        self.assertIsNone(self.turns[self.session_id].progress_handle)
        self.assertEqual(self.adapter.delivered, [])
        #: ⭐ 「入站侧让出」**只在别人持创建权时**成立 ⇒ 先把那个前提钉住。
        self.assertTrue(
            self.turn.progress_message_creating,
            "事件流那一路没有持有创建权 —— 「让出」就没有前提，这条会变成恒真。",
        )
        #: 入站侧跑完（同步跑完，所以下面两条不是「还没跑到」的恒真）。
        self.core.on_inbound(self.inbound())
        #: ⭐ 修复后的行为：首片那一路的 send 仍在飞行 ⇒ 本路**不创建**第二条。
        self.assertEqual(
            self.adapter.progress_messages(), [],
            "首片那一路的 send 还在飞行中，入站侧却另外发了一条占位消息：%r"
            % (self.adapter.progress_messages(),),
        )
        #: 持创建权的那一路还没送完 ⇒ 句柄**仍然是 None**。
        self.assertIsNone(
            self.turns[self.session_id].progress_handle,
            "持有创建权的那一路还没送完，句柄却已经写回来了。",
        )
        self.adapter.park_release.set()
        self.join_workers()
        self.assert_the_turn_was_really_dispatched(self.session_id)
        #: ⭐ 只有**一对** send —— 占位消息**根本没有发出去**。
        #: 交错证据没有被削弱：``send-entered``（首片）落在 ``send-returned``
        #: **之前**，而它出现的那一刻 ``progress_handle`` 仍是 ``None``（上面）。
        self.assert_interleaving([
            (FRAME_DISPATCHED, FIRST_FRAGMENT),
            (SEND_ENTERED, FIRST_FRAGMENT),
            (SEND_RETURNED, "m1"),
        ])

    # --- ① ------------------------------------------------------------
    def test_the_answer_body_reaches_the_reader_exactly_once(self):
        """这一条钉的是**最严重**的形态：读者把同一段正文读了两遍。"""
        self.finish_the_turn(self.session_id)

        self.assertEqual(
            self.adapter.reader_transcript(), [ANSWER],
            "读者读到的是 %r —— 答复与首片分处两条消息，同一句话被读了两遍。"
            % (self.adapter.reader_transcript(),),
        )

    # --- ② ------------------------------------------------------------
    def test_one_turn_owns_exactly_one_progress_handle(self):
        progress_messages = self.adapter.progress_messages()

        self.assertEqual(
            set(progress_messages), {self.turn.progress_handle.message_id},
            "这一轮发出了 %d 条进度消息（%r），turn 只认领了 %r —— 首片那条成了孤儿。"
            % (len(progress_messages), progress_messages,
               self.turn.progress_handle.message_id),
        )

    # --- ③ ------------------------------------------------------------
    def test_the_placeholder_and_the_first_fragment_do_not_both_send_a_message(self):
        self.finish_the_turn(self.session_id)

        self.assertEqual(
            len(self.adapter.progress_messages()), 1,
            "占位消息与首片各发了一条：%r" % (self.adapter.progress_messages(),),
        )


class NoClaimHeldCreatesAsUsualTests(ProgressHandleHandoffTestCase):
    """⭐⭐ **反向护栏**：没有人持创建权时，两条路**照常**创建进度消息。

    **为什么必须有这一组。** 两个竞态形态里各有一条断言说的是
    「**别人持创建权且它的 send 还在飞行** ⇒ 本路**不创建**第二条进度消息」。
    那是个**带前提**的断言 —— 前提一旦消失（本该创建时不创建）它也照样成立
    ⇒ 就会变成 §9 说的那种恒真。**唯一**能拆掉恒真的办法，是在**同一个谓词**
    上把前提取反，再断言相反的结论。

    ⇒ 判别力就是这张对照表：

    | ``progress_message_creating`` | 谁是本路 | 本路创建了吗 |
    |---|---|---|
    | ``True``（别人持，被钉在飞行中） | 事件流 / 入站 | **不**创建 |
    | ``False``（没人持） | 事件流 / 入站 | **创建**（就是下面两条） |

    两行只有创建权标志一个变量不同，结论相反 ⇒ 它抓的是**创建权**，
    不是「碰巧这一帧没发」。

    ⚠️ :class:`SequentialDeliveryControlTests` **不能替代本组**：它测的是
    「占位那一路已经把句柄写回**之后**首片才到」⇒ 那一帧走的是
    ``handle is not None`` 分支（改写已有消息），**根本没经过**创建权判断
    ⇒ 把本组这两条删掉、把两个 guard 全删掉，它仍然全绿。
    """

    def test_the_event_stream_creates_the_progress_message_when_nobody_holds_the_claim(self):
        """事件流这一侧：``progress_message_creating`` 为假时，首片**照常**创建。"""
        self.build_race()
        session_id = self.core.sessions.ensure_session(CONVERSATION, platform=PLATFORM)
        self.dispatch(frame("session.execution.started", sessionID=session_id))
        turn: Turn = self.turns[session_id]
        #: 前提是假的：此刻**没有任何人**在创建这一轮的进度消息。
        self.assertFalse(
            turn.progress_message_creating,
            "这一轮本该没有任何人持创建权 —— 那下面那句「照常创建」就不成立了。",
        )

        self.adapter.markers.append((FRAME_DISPATCHED, FIRST_FRAGMENT))
        self.dispatch(first_frame(session_id))

        self.assertEqual(self.adapter.progress_messages(), ["m1"])
        self.assertEqual(turn.progress_handle.message_id, "m1")
        #: 创建权已被**归还** —— 否则下一次调用会被同一个标志永久挡住，
        #: 而「归还」这件事没有任何其它测试钉着。
        self.assertFalse(turn.progress_message_creating)

    def test_the_prompt_dispatch_creates_the_placeholder_when_nobody_holds_the_claim(self):
        """入站这一侧：``progress_message_creating`` 为假时，占位**照常**创建。

        ⚠️ 这条与 :class:`SequentialDeliveryControlTests` 的前半段送的是**同一个**
        ``Inbound``，区别只在它还多钉了一件那个对照组没钉的事：句柄写回后
        创建权**回到了假**。
        """
        self.build_race()

        self.core.on_inbound(self.inbound())
        session_id = self.client.created_ids[0]
        turn: Turn = self.turns[session_id]
        self.assert_the_turn_was_really_dispatched(session_id)
        #: 前提是假的：入站这一路进来时**没人**在创建它。
        self.assertFalse(turn.progress_message_creating)

        self.assertEqual(self.adapter.progress_messages(), ["m1"])
        self.assertEqual(turn.progress_handle.message_id, "m1")
        self.assertFalse(turn.progress_message_creating)


class ThrottleWindowTests(ProgressHandleHandoffTestCase):
    """把「节流挡不住」这件事本身钉下来 —— 它是缺陷 A 能自然发生的前提。

    ⚠️ 这两条是**绿的**：它们钉的是**前提事实**，不是缺陷。少一条它们，缺陷 A 的
    用例就可能在某次改动后**因为另一个原因**变红，而报告里读起来仍像是「那个
    窗口」的问题。
    """

    def test_a_started_round_leaves_the_throttle_window_permanently_open(self):
        self.build_race()
        session_id = self.core.sessions.ensure_session(CONVERSATION, platform=PLATFORM)

        self.dispatch(frame("session.execution.started", sessionID=session_id))
        turn: Turn = self.turns[session_id]

        self.assertEqual(turn.last_edit_ts, 0.0)
        self.assertGreater(
            self.core.event_stream.clock() - turn.last_edit_ts,
            self.core.event_stream._edit_interval,
            "首片会被节流挡掉 —— 那缺陷 A 的用例红的就不再是那个窗口了。",
        )

    def test_a_round_without_the_started_frame_also_leaves_it_open(self):
        """没有 ``session.execution.started`` 时结论一样（字段默认值就是 ``0.0``）。"""
        self.build_race()
        session_id = self.core.sessions.ensure_session(CONVERSATION, platform=PLATFORM)
        self.core.on_inbound(self.inbound())
        turn: Turn = self.turns[session_id]

        self.assert_the_turn_was_really_dispatched(session_id)
        self.assertEqual(turn.last_edit_ts, 0.0)
        self.assertGreater(
            self.core.event_stream.clock() - turn.last_edit_ts,
            self.core.event_stream._edit_interval,
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
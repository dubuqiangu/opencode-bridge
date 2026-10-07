"""入站这一侧：适配器推来的消息、按钮回调、每会话队列、启动时的收件箱重放。

这一块原先是 :class:`~opencode_bridge.core.BridgeCore` 的七个方法。搬出来是因为
那个类已经远超 AGENTS.md §5.1 的阈值（方法数 ~15+ / 类自身代码行 ~250+），而入站
是里面**自成一块**的一坨：适配器进来的两个 hook、同一会话内消息的排队与串行投递、
写前收件箱（G2）那几次记账、以及崩溃后启动时的重放。

搬的时候**连私有状态一起搬**（§5.1 首选的那种形态）：``_queues`` / ``_draining`` /
``inbox`` 都是只有这一块才读写的东西。剩下的依赖由 :meth:`InboundGateway.__init__`
显式注入，其中两项是**共用的状态**而不是协作者：``lock``（core 与事件流还在用同一把
锁）与 ``turns``（事件流在那边读改同一个 dict）。注入的是**同一个对象**，所以互斥
关系与 dict 身份一点没变。

这一块**不碰**事件流、不碰生命周期：``BridgeCore.start()`` 只在 SSE 线程起来之后调
一次 :meth:`InboundGateway.recover_inbox`，其余全靠上面两个 hook 进来。
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections.abc import Callable, Mapping
from typing import Any

from .adapters import Adapter
# C1：发给 agent 的 prompt 前面拼一段"这条回复发出去是什么样"的说明。
# 纯函数、无状态，所以直接 import —— 本文件注入的是**有状态的协作者**
# （路由 / 收件箱 / 出站），而这一段只需要适配器的能力声明。
from .channel_profile import with_channel_hint
# ``setup:`` 按钮回调要回的那份**冻结文案**就在命令那边，所以这里 import 它，
# 而不是复制第二份 —— 改一处只碰一个地方（AGENTS.md §5.1）。
from .commands import _SETUP_ALIASES, _setup_guide
# 建 turn 用 :class:`Turn`，类型归事件流所有；这里只 import，不复制。
from .event_stream import Turn
from .hooks import Inbound, MsgHandle
from .inbound_merge import (
    BUFFERED_NOTICE,
    HELD,
    HELD_EXPIRED_NOTICE,
    IGNORED,
    ConversationMerger,
)
from .inbox import InboundInbox, QueuedPrompt, RecordOutcome
from .inbox_recovery import recover_pending
from .normalize import _clean, trim_outer_whitespace
from .opencode_client import OpenCodeClient, OpenCodeError
from .permission_ledger import (
    REPEATED_ANSWER_ACK,
    PermissionLedger,
    repeated_answer_notice,
)

__all__ = ["InboundGateway"]

logger = logging.getLogger("opencode_bridge.inbound_gateway")

PROGRESS_TEXT = "⏳ 处理中…"

#: 保险丝到点、而那一行**根本没有被投递**时说的话。
#:
#: ⛔ 绝不说「已发出」：那一行若没进队列，用户会一直等一个**永远不来的答复**，
#: 而他刚刚被明确告知「原样发出」—— 与 `c41c3ae` 修掉的「收件箱已关被报成去重
#: 命中」是同一个形状的谎报，只是这一次的受害者是读者而不是日志的读者。
#: ⚠️ 这里**不**说清是去重还是收件箱已关：这一帧拿不到
#: :class:`~opencode_bridge.inbox.RecordOutcome`，而**猜**哪一个比说实话更糟
#: （AGENTS.md §8「不要要求代码区分它唯一的输入无法区分的两种情况」）
#: ⇒ 两个可能都写出来，两个都是真的；哪一条由 :func:`_record_inbound` 自己那行
#: 日志（点名 delivery_id）说清。
HELD_NOT_DELIVERED_NOTICE = (
    "等待下一行超时，但这一行**没有发出去**"
    "（收件箱里已经有同一行，或收件箱已关）—— "
    "你敲的内容原样附在下面，需要的话请重发一次：\n%s"
)

#: 请求**已经**提交给 agent、而我们**没拿到答复**时告诉用户的话。
#:
#: ⚠️ 它**必须**带「请重新发送一次」，而这不是客套：不重放是用户拍板的（理由见
#: :mod:`opencode_bridge.inbox_recovery` 模块开头），代价是远端**真**没收到时那条指令
#: **丢了**。⛔ 只说"未知"而不告诉他该做什么，等于把负担转给用户却不给方法 ——
#: 而收件箱存在的理由正是"别丢这条"。
#:
#: ⛔ 这里**不许**说「发送失败」：那是一句**谎话**（我们并不知道失败），而用户会照它
#: 去 ``/new`` 重建会话 —— 把一次可能成功的提交变成一次丢会话。
#: 判据：:mod:`tests.test_inbound_gateway` 有一条断言这句话在（删掉它，那条会红）。
UNKNOWN_OUTCOME_NOTICE = (
    "这一条的结果未知：请求已经提交给 agent，但没能确认它是否已经处理"
    "（提交时连接中断或超时）。"
    "为避免重复执行副作用（重复改文件、重复 git 操作、重复长时间构建），已不自动重发。"
    "请检查该会话是否已经处理过；如果没有，请重新发送一次。"
)

#: Values accepted in ``perm:<sessionID>:<reqID>:<decision>`` callbacks.
_PERM_DECISIONS = ("once", "always", "reject")

#: 长输入回执的字数门槛（配置缺失或非法时用这个）。180 抄自 dsh 的
#: ``longInputAckChars``；含义与理由见 :meth:`InboundGateway._acknowledge_long_input`。
DEFAULT_LONG_INPUT_ACK_CHARS = 180

#: 续行缓冲的保险丝秒数（配置缺失或非法时用这个）。**不是**合并窗口 ——
#: 没有 ``..`` 的消息不会起任何计时器，所以普通消息的额外延迟可证明是 0。
DEFAULT_MERGE_CONTINUE_TIMEOUT_SECONDS = 15.0


def _positive_float(value: Any, default: float) -> float:
    """A finite, non-negative float, else ``default`` (NaN and negatives rejected).

    ``0`` is legal (= 关掉保险丝，与 :func:`_positive_int` 同一条判据) ——
    这里唯一用到它的是 ``merge_continue_timeout_seconds``，而那是 G2 崩溃窗口的
    **唯一**旋钮：判成 ``> 0`` 会让「配 0 关掉窗口」悄悄变成「配 0 仍是 15 秒窗口」。

    ⚠️ :func:`opencode_bridge.core.BridgeCore._positive_float` 是**同名但另一件事**：
    它的判据是 ``number < 0`` 才回落 ⇒ **它也接受 ``0``**，而那里的 ``0`` 等于
    「不做节流」（每个 delta 都改写），是一个**合法且有意义的**设置。
    ⇒ 所以别把这一处的判据套过去，也别把那一处改成这里这样 ——
    **同一个数字在两个函数里语义不同**：这里 ``0`` =「关掉保险丝」= 移除一个
    **有界**的保护窗口；那里 ``0`` =「不节流」。⚠️ 而这里那个「有界」一旦被关掉，
    暴露窗口就变成**无界**（见 `tasks.md` 未完成项总表那一行的代价说明）
    ⇒ **正因为语义不同，混用判据会同时坏掉两边**。

    ⚠️ **这条函数本身不打任何告警**，而「配 ``0`` 要有一条点名该键的 WARNING」
    落在唯一那个调用点（:meth:`InboundGateway.__init__` 构造合并器的地方）——
    因为**键名只有那里知道**：把这个键名写死进本函数，第二个调用点一出现，
    那条告警就会**点名一个与实际无关的键**（谎报，正是本仓库反复记的那类错）。
    ⇒ 新增调用点时，**它自己也得发一条点名自己那个键的**。
    ⛔ 也不要为了"让告警离取值近一点"就把取值逻辑搬进来：判据是「算出来的值
    是 0」，而**合并过默认值的配置字典里这个键永远在场**（见
    :meth:`~opencode_bridge.config.Config._merge_bridge`）⇒「键在不在」是恒真的。
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in (float("inf"), float("-inf")):
        return default
    return number if number >= 0 else default


def _positive_int(value: Any, default: int) -> int:
    """A non-negative int, else ``default``. ``0`` is legal (= 该功能关掉)."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number >= 0 else default

#: 启动时等事件流确认连上的上限（秒），见 :meth:`InboundGateway.recover_inbox`。
#: 取 2 秒是因为 opencode 通常就在本机；而真的不可达时这 2 秒只换来一行告警 ——
#: 那种情况下恢复扫描本身也多半会失败，不该在这里死等。
_EVENT_STREAM_CONFIRMED_TIMEOUT_SECONDS = 2.0


# ----------------------------------------------------------------------
# 注入的协作者：签名写在这里，core 那边的实现是什么它们不关心
# ----------------------------------------------------------------------
#: 按 conversation_id 找出该回哪个适配器（找不到返回 ``None``）。
AdapterFor = Callable[[str], Adapter | None]

#: 回一条按钮回调的应答（ack）。
AnswerCallback = Callable[[Adapter | None, str, str], None]

#: 取（或建）这条会话的 opencode session；平台以关键字 ``platform`` 传入。
EnsureSession = Callable[..., str]

#: 执行一条斜杠命令。
HandleCommand = Callable[[str, Adapter, str], None]

#: 记下"这个会话属于哪个适配器"。
RememberPlatform = Callable[[str, str], None]

#: 发一条出站文本；关键字参数与 core 的发信入口一致，返回句柄或 ``None``。
SendText = Callable[..., MsgHandle | None]


# ----------------------------------------------------------------------
# 写前收件箱：从一条入站消息到收件箱里的一行
# ----------------------------------------------------------------------
# 这一段是**模块级函数**而不是 :class:`InboundGateway` 的方法：纯逻辑的构造与记账，
# 与"入站"这个类的其余职责（排队、投递、重放）无关，也不该给它再加方法
# （AGENTS.md §5.1）。
def _queued_prompt_for(inbound: Inbound, text: str) -> QueuedPrompt:
    """Build the :class:`QueuedPrompt` row for one inbound message.

    The body is carried through **verbatim** — never prefixed, never rewritten.
    That text lands in the agent's context, so anything appended here (a
    "replayed after crash" note, say) would be read by the agent as part of
    user's request. Alerts about a replay belong in the notification path, not
    in the prompt.

    Delivery id: the platform's own ``message_id`` when it has one, else
    ``sha256(platform|conversation_id|text)``. The fallback is a real
    trade-off — two byte-identical messages from a platform that reports no
    ``message_id`` collapse into one delivery — so it is logged, never silent
    (see :func:`_record_inbound`).
    """
    platform = str(inbound.platform or "")
    conversation_id = str(inbound.conversation_id or "")
    message_id = str(inbound.message_id) if inbound.message_id else None
    if message_id:
        delivery_id = "%s:%s:%s" % (platform, conversation_id, message_id)
    else:
        delivery_id = hashlib.sha256(
            ("%s|%s|%s" % (platform, conversation_id, text)).encode("utf-8")
        ).hexdigest()
    return QueuedPrompt(
        delivery_id=delivery_id,
        conversation_id=conversation_id,
        platform=platform,
        message_id=message_id,
        text=text,
    )


def _record_inbound(
    inbox: InboundInbox | None, inbound: Inbound, text: str,
) -> QueuedPrompt | None:
    """Write-ahead one inbound prompt. Returns the row to deliver, or ``None``
    to deliver nothing.

    ``None`` has **two** reasons, and conflating them misdirects whoever reads the log:

    * **dedup hit** (:attr:`~opencode_bridge.inbox.RecordOutcome.DUPLICATE`) — the same
      ``delivery_id`` is already in the inbox, so the platform re-delivered something we
      have a receipt for. Running the agent again on it is exactly what at-most-once is for.
    * **the inbox is already closed** (:attr:`~opencode_bridge.inbox.RecordOutcome.CLOSED`)
      — ``close()`` has run, so **nothing was written** for this message and
      :mod:`opencode_bridge.inbox_recovery` can never replay it.

    ⛔ Before :class:`~opencode_bridge.inbox.RecordOutcome` existed, both were just
    ``False`` and this function logged the closed one as "duplicate ignored (platform
    re-delivered a known message)" — **backwards**: it points the reader at the platform
    when the truth is "we closed". ⇒ The closed case gets its own branch, and delivery
    is unchanged in both: not written ⇒ not delivered. That half is deliberate
    (``RecordOutcome.__bool__`` keeps both "not recorded" outcomes falsy), because
    delivering with no receipt on disk is precisely what the write-ahead inbox exists
    to prevent.

    ``inbox is None`` means the inbox is switched off; delivery proceeds
    unchanged, just without a receipt.
    """
    queued = _queued_prompt_for(inbound, text)
    if inbox is None:
        return queued
    record_outcome = inbox.record(queued)
    if record_outcome is RecordOutcome.CLOSED:
        # Level is info, and that is the judgement: the severity for "nothing reached
        # the disk" belongs to the inbox, which warns per refused message (it owns the
        # facts — the path and the unreplayable consequence). A second warning here would
        # just double-count one loss once per still-running adapter thread, and shutdown
        # guarantees those threads. What only this frame knows is the delivery decision,
        # so say that, and say it truthfully.
        logger.info(
            "inbox %s: not delivered; the write-ahead inbox is already closed"
            " (shutdown in progress), so nothing was written for this message and"
            " opencode_bridge.inbox_recovery can never replay it - not deduplicated,"
            " lost. The refusal itself is logged at warning level by opencode_bridge.inbox",
            queued.delivery_id,
        )
        return None
    if record_outcome:
        # RECORDED (truthy). Kept as truthiness, not identity: test doubles across the
        # suite hand this frame a plain ``bool``, and the published contract of
        # ``record()`` is "true means a receipt exists".
        return queued
    # ⛔ Only DUPLICATE can land here: both "not recorded" outcomes are falsy and
    # CLOSED was branched off above. Adding a fourth falsy member would silently turn
    # into a dedup report again.
    if queued.message_id is None:
        # The hash-fallback collapse is the one dedup the user cannot predict
        # from the platform side, so it gets spelled out rather than left as a
        # mysterious "message ignored".
        logger.info(
            "inbox %s: duplicate ignored; this platform reports no message_id, "
            "so the dedup key fell back to a content hash — two byte-identical "
            "messages collapse into one delivery",
            queued.delivery_id,
        )
    else:
        logger.info(
            "inbox %s: duplicate ignored (platform re-delivered a known message)",
            queued.delivery_id,
        )
    return None


# ----------------------------------------------------------------------
# inbound gateway
# ----------------------------------------------------------------------
class InboundGateway:
    """Everything the adapters push in, and the machinery behind it."""

    def __init__(
        self,
        *,
        client: OpenCodeClient,
        lock: threading.RLock,
        turns: dict[str, Turn],
        inbox: InboundInbox | None,
        stream_confirmed: threading.Event,
        adapter_for: AdapterFor,
        answer_callback: AnswerCallback,
        ensure_session: EnsureSession,
        handle_command: HandleCommand,
        remember_platform: RememberPlatform,
        send_text: SendText,
        permission_ledger: PermissionLedger,
        bridge_config: Mapping[str, Any],
    ) -> None:
        """全部依赖由 core 注入，本类不自己去找。

        ``lock`` 与 ``turns`` 是**共用的状态**而不是协作者：事件流读改同一个
        ``turns``，core 那边七个方法还在用同一把锁（见模块 docstring）。注入的是
        同一个对象，所以互斥关系与 dict 身份都没变。

        ``inbox`` 是 ``None`` 时写前收件箱整个关掉 —— 投递照走，只是不留收据。

        ``stream_confirmed`` 是事件流那个"订阅已建立"的信号；恢复要等它（有上限），
        但**不归本类拥有**：设它的是事件流。

        ``permission_ledger`` 与命令侧、事件流侧**共用同一个对象**，所以
        ``perm:`` 按钮那一条路和 ``/approve`` 会对"这个请求答过了没有"得出
        同一个答案 —— 否则同一个请求从两条路各能答一次，去重就成了半拉子
        （见 :mod:`opencode_bridge.permission_ledger`）。

        ``bridge_config`` 是 ``config.bridge`` 那一段（构造时读一次，运行时不会
        被改写）。C3 只从里面取两个数：长输入回执的字数门槛、续行保险丝的秒数。
        刻意**不**注入整个 :class:`~opencode_bridge.config.Config` —— 本类用不到
        别的配置，而那两个数读不到就各自退回模块默认值（见
        :mod:`opencode_bridge.inbound_merge`）。
        """
        self._client = client
        self._lock = lock
        self._turns = turns
        self._inbox = inbox
        self._stream_confirmed = stream_confirmed
        self._adapter_for = adapter_for
        self._answer = answer_callback
        self._ensure_session = ensure_session
        self._handle_command = handle_command
        self._remember_platform = remember_platform
        self._send_text = send_text
        self._permission_ledger = permission_ledger
        self._long_input_ack_chars = _positive_int(
            bridge_config.get("long_input_ack_chars"), DEFAULT_LONG_INPUT_ACK_CHARS
        )
        #: C3 的续行缓冲。**本类自己建**（只有入站这一侧读它），所以不注入。
        #: 保险丝到点的回调是本类的方法 —— 缓冲因此不需要知道任何出站的东西。
        merge_continue_timeout_seconds = _positive_float(
            bridge_config.get("merge_continue_timeout_seconds"),
            DEFAULT_MERGE_CONTINUE_TIMEOUT_SECONDS,
        )
        # ⚠️ ``0`` 是那种「**会被接受、但语义反直觉**」的配置值（AGENTS.md §4.1：
        # 键被 ``start()`` 接受却不按字面直觉生效 ⇒ 必须有一条点名该键的 WARNING）
        # ⇒ 这里点名它，理由与代价**同一条**说出去：只写好处会被读反。
        #
        # ⛔ 判据是「**算出来的值是 0**」，不是「键在不在」——
        # :meth:`~opencode_bridge.config.Config._merge_bridge` 总把默认值补进去，
        # 所以生产上这个键**永远在场**（没配时它的值是 15.0）。
        # ⇒ 逐字对应关系：显式配 ``0`` ⇒ 合并后是 ``0.0`` ⇒ 落进这条分支；
        # 没配 ⇒ ``15.0`` ⇒ 不落；配错（非数字 / 负数 / NaN）⇒ **被
        # ``_merge_bridge`` 当场丢掉并回落成** ``15.0`` ⇒ 同样不落。
        # ⚠️ 这条判据的前提是 :data:`DEFAULT_MERGE_CONTINUE_TIMEOUT_SECONDS` **不为 0**
        # —— 它若哪天变成 0，每次启动都会多出这条噪音告警。
        if merge_continue_timeout_seconds == 0.0:
            logger.warning(
                "bridge.merge_continue_timeout_seconds=0 —— 立即返回、原样使用、"
                "不改写（没有被改写成 %r）⇒ 续行缓冲不再装计时器，于是那条消息"
                "一直等到下一条非 ``..`` 行为止。⛔ 但**配 0 并不比默认更安全**："
                "缓冲是**纯内存**，而 ConversationMerger 的 flush / stop / "
                "held_conversation_ids **生产零调用点** ⇒ 关停不排空、重启恢复不到 "
                "⇒ G2 那个丢消息窗口由「≤ 15 秒、**有界**」变成「**无界**」"
                "⇒ 文档措辞正确（关掉保险丝就是关掉保险丝）**不等于 0 更安全**。",
                DEFAULT_MERGE_CONTINUE_TIMEOUT_SECONDS,
            )
        self._merger = ConversationMerger(
            hold_timeout_seconds=merge_continue_timeout_seconds,
            on_hold_expired=self._deliver_expired_hold,
        )

        #: conversation_id -> 该会话排队等发的消息。**只有本类读写**。
        self._queues: dict[str, list[QueuedPrompt]] = {}
        #: 正在排空某个会话的 conversation_id —— 同一会话同时只允许一个排空者。
        self._draining: set[str] = set()

    # ------------------------------------------------------------------
    # Hooks: inbound
    # ------------------------------------------------------------------
    def on_inbound(self, inbound: Inbound) -> None:
        try:
            conversation_id = str(inbound.conversation_id or "")
            # ⚠️ 必须**先**记映射、再处理 is_callback 早退 —— 按钮回调那条路
            # 本身会return 掉，若在这里记就漏了它，后续 :meth:`on_callback` 只能靠
            # 前缀去猜是哪个适配器。
            self._remember_platform(conversation_id, inbound.platform)
            if inbound.is_callback:
                # Lane B fires on_inbound(kind="callback") *before*
                # on_callback(); handling it here as well would double-send.
                return
            text = _clean(inbound.text)
            if not conversation_id:
                return
            # ⚠️ 这里**不能**写 ``.strip()``：无参 strip 会去掉前导空白，于是粘贴
            # 的第一行被 dedent、后面几行没有 —— agent 拿到的是 IndentationError。
            # 全空白消息交给下面那个 ``not text`` 守卫去丢（那才是 strip 当初的
            # 作用），排版交给 :func:`~opencode_bridge.normalize.trim_outer_whitespace`。
            text = trim_outer_whitespace(text)
            if not text:
                return
            adapter = self._adapter_for(conversation_id)
            if adapter is None:
                logger.warning(
                    "no adapter attached; dropping message for %s",
                    conversation_id,
                )
                return
            if text.startswith("/") or text.lstrip().startswith("/"):
                # ⚠️ 这里**判两次**：正文不再 lstrip（缩进要留着），但命令必须
                # 仍然容得下一个前导空格 —— 那是 C3 之前 ``.strip()`` 顺带给出的
                # 行为，而"只改缩进、别的都不动"要求把它原样留下。
                # ``text.lstrip()`` 与旧代码的 ``.strip()`` 在命令这一支上等价：
                # 命令一定是一行，而 :meth:`~opencode_bridge.commands.CommandHandler.
                # handle_command` 本来就按空白切词，两侧空白它都吃。
                #
                # ⚠️ 命令**绝不**写前落盘。命令就地执行、从不经过 prompt()，
                # 所以它永远不会走到 mark_delivered —— 那一行会永远留在
                # 收件箱里，于是每次启动都被重放一遍：`/new` 每次重启都重建会话、
                # `/setup` 每次都重发引导。那比要修的丢消息 bug 更糟。
                # 落盘必须留在 else 分支里（tests/test_inbox_wiring.py 锁住这条）。
                #
                # ⚠️ 命令也**绝不**进合并窗口（与 dsh 的 gateway.ts:395-428 同一条
                # 纪律）：`/approve` 这类命令必须立刻执行，缓存它等于把 C4 刚堵上的
                # 权限路径重新打开一条延迟通道。用户给命令敲 `..` 本来就没有意义。
                self._handle_command(conversation_id, adapter, text.lstrip())
                return
            self._deliver_plain_text(conversation_id, adapter, inbound, text)
        except Exception:
            logger.exception("on_inbound failed")

    def _deliver_plain_text(
        self,
        conversation_id: str,
        adapter: Adapter,
        inbound: Inbound,
        text: str,
    ) -> None:
        """One non-command line: merge-gate it, then persist and deliver.

        拆成独立方法是因为 :meth:`on_inbound` 里已经有一条命令分支，再往那个
        ``else`` 里塞合并、回执、去重三件事会让它读不下去（AGENTS.md §5.1 的
        "只能往大方法里塞 if 分支"就是该拆的信号）。

        **顺序是有意的**：合并 → 回执 → 写前落盘 → 投递。合并必须在最前（那是
        "这几行是一件事"的判断），而落盘必须在投递前（那是"别丢这条"）。两件事
        刻意由两个模块各管各的：缓冲**不**写进收件箱（见
        :mod:`opencode_bridge.inbound_merge` 的模块 docstring）。
        """
        merged = self._merger.ingest(conversation_id, text)
        if merged.kind == IGNORED:
            return
        if merged.kind == HELD:
            # 回执是**必须**的：一行以 `..` 结尾的散文会被判成续行标记，没有这句
            # 它就是静默消失（AGENTS.md §8 点名最糟的那种代价）。
            self._send_text(
                conversation_id, BUFFERED_NOTICE, kind="text", adapter=adapter
            )
            return
        self._persist_and_enqueue(conversation_id, adapter, inbound, merged.text)

    def _persist_and_enqueue(
        self,
        conversation_id: str,
        adapter: Adapter,
        inbound: Inbound,
        text: str,
    ) -> QueuedPrompt | None:
        """Acknowledge if long, write the inbox receipt, then queue for delivery.

        合并**已经**做完，这里只处理"怎么把它变成一次投递"。拆成独立方法是
        因为保险丝那条路要走同样的三步，而它拿到的是**并集**而不是一行 ——
        若让它去调 :meth:`_deliver_plain_text`，那份并集会被再过一次合并，
        末尾的 ``..`` 会让它重新进缓冲，形成自己喂自己的循环。

        :returns: 真的进了队列的那一行；``None`` = **没有投递**（去重命中 /
            收件箱已关 / 收件箱写失败）。⚠️ 这个返回值是给
            :meth:`_deliver_expired_hold` **说真话**用的：那一帧对用户宣布
            「已把等到的内容原样发出」，而它必须先知道这件事成不成立。
        """
        self._acknowledge_long_input(conversation_id, adapter, text)
        try:
            queued = _record_inbound(self._inbox, inbound, text)
        except Exception:
            # ⛔ 落盘失败**不许**变成「静默消失」：这里恰恰是"盘上什么都没写、
            # 消息也什么都没发"的那一种 —— 收件箱存在的理由（别丢这条）当场落空。
            # 以前它一路冒到 :meth:`on_inbound` 那个兜底 ``except``，用户那边
            # **一个字都看不到**，而那条消息就此消失（盘上没有回执 ⇒ 恢复层也
            # 救不回来）。
            #
            # ⚠️ **不投递**：写前义务没履行，投出去就是"拿不到回执的一次发送"，
            # 正是 :mod:`opencode_bridge.inbox` 要防的那件事。
            # ⇒ 用户必须知道要自己重发 —— 那正是收件箱**没有**替他做到的事。
            logger.exception(
                "inbound: cannot write the inbox receipt for %s; the message was"
                " NOT delivered and cannot be replayed -- the reader has to resend it",
                inbound.message_id,
            )
            self._send_text(
                conversation_id,
                "这条消息没能落盘（收件箱写入失败），因此**没有发出去**，"
                "也不会自动重放 —— 请重新发送一次。",
                kind="text",
                adapter=adapter,
            )
            return None
        if queued is not None:
            # None = 去重命中（已投递过）**或**收件箱已关（没落盘，见 _record_inbound）
            self._enqueue(queued)
        return queued

    def _acknowledge_long_input(
        self, conversation_id: str, adapter: Adapter, text: str,
    ) -> None:
        """Tell the reader a long input arrived — **only where nothing else will**.

        判据是 :attr:`~opencode_bridge.adapters.base.Adapter.supports_message_edit`
        而不是"平台名单"：不能改写已发消息时出站那道闸门**根本不发**
        ``⏳ 处理中…``（见 :mod:`opencode_bridge.channel_profile`），于是用户粘一大段
        之后**什么迹象都没有**。能改写的那些本来就有那个占位消息，再加一句只是让
        一次提问变成三条消息。
        """
        threshold = self._long_input_ack_chars
        if threshold <= 0 or len(text) < threshold:
            return
        if getattr(adapter, "supports_message_edit", False):
            return
        self._send_text(
            conversation_id,
            "已收到 %d 字，处理中…" % len(text),
            kind="text",
            adapter=adapter,
        )

    def _deliver_expired_hold(self, conversation_id: str, held_text: str) -> None:
        """Fuse fired: deliver what was held, and say that we did.

        这一条保证缓冲的内容**任何路径都不会无声消失**：用户敲了 ``..`` 然后
        再没下文，保险丝到点替他发出去，并告诉他发出去的是什么。

        ⚠️ 合成的那条 :class:`Inbound` **没有** ``message_id`` —— 它不是平台
        交付的一条消息。给了 ``None`` 就会走 :func:`_queued_prompt_for` 的
        ``sha256(platform|conversation_id|text)`` 兜底，于是同一次超时重放两次会
        被收件箱去重挡掉（那是**要**的：同一条并集不该被 agent 跑两遍）。
        ``platform`` 取适配器自己的 ``name``，与各适配器构造 ``Inbound`` 时写的
        ``platform=self.name`` 是同一个值（见 :meth:`AdapterRouter.asking_platform`
        的说明：提问平台就是适配器自己报上来的名字）。

        ⚠️ **那句「已把等到的内容原样发出」是**投递之后**才说的**，而且只在真的
        投递出去的时候才说。⛔ 此前它无条件先发 —— 而 :meth:`_persist_and_enqueue`
        有三条正当的「没有投递」的路（去重命中 / 收件箱已关 / 收件箱写失败）⇒
        用户被明确告知「原样发出」，然后**永远等不到答复**，且那一条也不会被重放。
        ⚠️ 那不是假想：同一个会话里第二次超时发出的**同一个并集**必然撞上
        ``sha256`` 兜底（合成 Inbound 没有 ``message_id``）⇒ 确定性可复现，
        而「用户把同一段话重发一遍」是极常见的动作。
        ⇒ 所以顺序与条件都由 :meth:`_persist_and_enqueue` 的返回值决定。
        """
        adapter = self._adapter_for(conversation_id)
        inbound = Inbound(
            conversation_id=conversation_id,
            text=held_text,
            kind="text",
            platform=str(getattr(adapter, "name", "") or ""),
        )
        if adapter is None:
            logger.warning(
                "inbound merge: no adapter for %s; the held text is delivered "
                "anyway but nothing will be sent to the reader",
                conversation_id,
            )
        queued = self._persist_and_enqueue(
            conversation_id, adapter, inbound, held_text
        )
        if queued is None:
            # ⛔ 不许在这里说「已发出」（理由见 docstring）。把内容原样还给用户，
            # 好让他看见自己敲了什么、决定要不要重发。
            self._send_text(
                conversation_id,
                HELD_NOT_DELIVERED_NOTICE % held_text,
                kind="text",
                adapter=adapter,
            )
            return
        self._send_text(
            conversation_id,
            HELD_EXPIRED_NOTICE % held_text,
            kind="text",
            adapter=adapter,
        )

    def on_callback(
        self, conversation_id: str, data: str, query_id: str
    ) -> None:
        """Handle ``setup:<platform>`` and ``perm:<sessionID>:<reqID>:<decision>``."""
        adapter = None
        try:
            adapter = self._adapter_for(conversation_id)
            data = str(data or "")
            # /setup inline-button press: reply once here (on_inbound already
            # dropped the kind="callback" copy, so no double-send) and ack.
            if data.startswith("setup:"):
                platform = _SETUP_ALIASES.get(data.split(":", 1)[1].strip().lower())
                if platform is None:
                    self._answer(adapter, query_id, "未知平台")
                    return
                self._send_text(
                    conversation_id, _setup_guide(platform), kind="text",
                    adapter=adapter,
                )
                self._answer(adapter, query_id, "已打开接入引导")
                return
            parts = data.split(":", 3)
            decision_ok = len(parts) == 4 and parts[0] == "perm"
            if decision_ok:
                _, session_id, request_id, decision = parts
                decision_ok = decision in _PERM_DECISIONS
            if not decision_ok:
                logger.warning("unsupported callback payload: %r", data)
                self._answer(adapter, query_id, "失败")
                return
            # ⚠️ 迟到的第二次回答在这里被否掉（C4）。按钮可以被连点、可以被
            # Telegram 重投，于是同一个 ``perm:`` 载荷会走两遍；不否掉就会发出
            # 两次 reply_permission，第二次还能把 once 放宽成 always。
            # 与命令侧共用同一个账本，所以两条路合起来只答一次。
            status = self._permission_ledger.status_of(session_id, request_id)
            if status.already_closed:
                logger.info(
                    "ignoring a repeated permission button press for %s of "
                    "session %s (already %s); nothing is sent to the server",
                    request_id, session_id, status.state,
                )
                self._answer(adapter, query_id, REPEATED_ANSWER_ACK)
                self._send_text(
                    conversation_id,
                    repeated_answer_notice(status),
                    kind="error",
                    adapter=adapter,
                )
                return
            try:
                self._client.reply_permission(session_id, request_id, decision)
            except Exception as exc:
                logger.warning(
                    "reply_permission(%s, %s, %s) failed: %s",
                    session_id,
                    request_id,
                    decision,
                    exc,
                )
                self._answer(adapter, query_id, "失败")
                self._send_text(
                    conversation_id,
                    f"权限回复失败: {exc}",
                    kind="error",
                    adapter=adapter,
                )
                return
            # 只在**成功**之后记账（理由见 commands._apply_permission_decision）。
            self._permission_ledger.record_answered(
                session_id, request_id, decision
            )
            self._answer(adapter, query_id, "已处理")
        except Exception:
            logger.exception("on_callback failed")
            self._answer(adapter, query_id, "失败")

    # ------------------------------------------------------------------
    # prompt queue (one conversation at a time, many in parallel)
    # ------------------------------------------------------------------
    def _enqueue(self, queued: QueuedPrompt) -> None:
        conversation_id = queued.conversation_id
        with self._lock:
            self._queues.setdefault(conversation_id, []).append(queued)
            if conversation_id in self._draining:
                return  # current drainer will pick this up
            self._draining.add(conversation_id)
        self._drain(conversation_id)

    def flush_queue(self, conversation_id: str) -> None:
        """Called after a turn finalises (or on ``session.idle``)."""
        with self._lock:
            if conversation_id in self._draining:
                return
            if not self._queues.get(conversation_id):
                return
            self._draining.add(conversation_id)
        self._drain(conversation_id)

    def _drain(self, conversation_id: str) -> None:
        # ⚠️ 预绑定：下面那个 ``except`` 要用它，而弹出它的那一行**在** ``try`` 里面。
        # ``self._lock`` 是一条 RLock，拿它抛不出异常，所以真到 ``except`` 时它必然
        # 已绑定 —— 预绑定只是让这一层不依赖那个前提。
        queued: QueuedPrompt | None = None
        try:
            while True:
                with self._lock:
                    queue = self._queues.get(conversation_id) or []
                    if not queue:
                        # empty check + draining flag flip are atomic, so a
                        # concurrent enqueue can never be lost
                        self._draining.discard(conversation_id)
                        return
                    queued = queue.pop(0)
                outcome = self._dispatch_prompt(queued)
                if outcome == "busy":
                    # 409 是"还没轮到"，不是失败：不写 failed，重试预算分文未花，
                    # 收件箱那一行已退回 pending（服务端拒收 = 从未尝试过）。
                    # 这一条退回内存队列，等 :meth:`flush_queue`。
                    with self._lock:
                        queue = self._queues.setdefault(conversation_id, [])
                        queue.insert(0, queued)
                        self._draining.discard(conversation_id)
                    return
        except Exception as exc:
            logger.exception("queue drain failed for %s", conversation_id)
            with self._lock:
                self._draining.discard(conversation_id)
            # ⛔ 这一条**已经不在队列里**了（上面 pop 掉了），所以「记一笔日志、
            # 释放会话」对读者等于**静默消失**：他发过一句话，屏幕上什么都没有。
            # 唯一现实触发条件是收件箱那两次写入（``mark_attempting`` /
            # ``mark_delivered``）撞上 sqlite3 错误（盘满 / 库损坏 / 被锁）——
            # ``adapter_for`` 抛不出来，而 ``send_text`` 永远不抛
            # （:meth:`~opencode_bridge.adapters.base.Adapter.send_observed`
            # 把适配器的异常交还而不是上抛）。
            #
            # ⚠️ **刻意不碰收件箱**：抛在哪一步我们不知道，而把一个可能**已经
            # delivered** 的行改成 failed 会让下次启动重放它 ⇒ agent 对同一条指令
            # 跑两遍（那比多发一条消息贵得多）。盘上那一行维持原样，由恢复层
            # 按它自己的分档去判断 —— 这正是它存在的理由。
            if queued is not None:
                self._send_text(
                    conversation_id,
                    "这一条没能提交给 agent：%s\n"
                    "（它已离开队列。若之后没有答复，请重新发送一次。）" % exc,
                    kind="text",
                )

    def _dispatch_prompt(
        self, queued: QueuedPrompt, *, recording_delivery: bool = True,
    ) -> str:
        """Send one queued prompt. Returns ``ok`` / ``busy`` / ``error``.

        Every inbox write on this path lives here and nowhere else, because only
        this method can tell *which* of the outcomes happened — ``delivered`` /
        ``failed`` / ``attempting`` / ``outcome_unknown`` / ``pending``(409) — and
        the ``attempting`` write has to sit immediately against ``client.prompt()``:
        written earlier, a crash during ``create_session`` would leave a row
        that looks "outcome unknown" when in fact the agent never ran.

        ⚠️ ``outcome_unknown`` 与 ``attempting`` 的分工：那一档是**崩在里面**，
        这一档是**当场就知道自己不知道**（传输层失败、拿不到 status）。两者的
        用户可见后果完全相同 —— 都只告警、绝不重放 —— 区别只在盘上有没有**留下**
        那个"不知道"（见 :attr:`~opencode_bridge.inbox.DeliveryState.OUTCOME_UNKNOWN`
        与 :mod:`opencode_bridge.inbox_recovery` 的分档表）。

        ``recording_delivery=False`` skips the writes for the startup recovery
        path, where :func:`~.inbox_recovery.recover_pending` is the sole bookkeeper.
        """
        conversation_id = queued.conversation_id
        adapter = self._adapter_for(conversation_id)
        inbox = self._inbox if recording_delivery else None
        try:
            # ⚠️ ``queued.platform`` 就是当初产生这条消息的适配器报上来的，准确；
            #: 它是归属划分需要的那个"提问平台"（启动重放路径上 core 的
            #: ``_conv_adapter`` 可能还是空的，这一行就不依赖它）。
            session_id = self._ensure_session(conversation_id, platform=queued.platform)
        except Exception as exc:
            logger.exception("create_session failed for %s", conversation_id)
            self._send_text(
                conversation_id,
                f"创建会话失败: {exc}",
                kind="error",
                adapter=adapter,
            )
            if inbox is not None:
                inbox.mark_failed(queued.delivery_id, f"create_session failed: {exc}")
            return "error"

        if inbox is not None:
            inbox.mark_attempting(queued.delivery_id)
        try:
            # ⚠️ 渠道说明（C1）只在**发给 opencode 的这一刻**拼上去，而且是
            # 由 ``adapter`` 自己的能力算出来的（见 :mod:`opencode_bridge.channel_profile`
            # ——刻意没有"平台 → 上限"的对照表）。收件箱里存的、哈希算的、
            # 重放重发的都还是用户原文，那条不变量一个字没动。
            self._client.prompt(
                session_id, with_channel_hint(queued.text, adapter)
            )
        except OpenCodeError as exc:
            if exc.status == 409:
                logger.info(
                    "session %s busy; message queued for %s",
                    session_id,
                    conversation_id,
                )
                # ⚠️ 必须在这里退回 pending：409 是"服务端拒收"，agent **没跑过**，
                # 所以重放不可能重复副作用。不退回的话那一行会永远停在
                # attempting，被恢复层按"结果不可知"只告警而不重放 ——
                # 崩溃若落在 409 与进程内重投之间，这条消息就**永远送不到**。
                if inbox is not None:
                    inbox.mark_pending(queued.delivery_id)
                return "busy"
            if exc.status is None:
                # ⚠️⚠️ **这一支就是本次分出第三态的地方**。请求**已经**交给 opencode，
                # 而失败发生在**传输层**（超时 / 连接被拒 / 流中断，见
                # ``OpenCodeClient._request``）⇒ 远端**是否收到从未被记录**。
                # ⇒ ⛔ 绝不能记 ``failed``：那是把"不知道"说成"一定没送到"，而
                # :mod:`inbox_recovery` 会按退避阶梯重放 ``failed`` ⇒ **agent 对同一条
                # 指令跑两遍**（AGENTS.md §8 第 3 条：恢复一份没记录过的信息 = 猜）。
                # ⇒ 落 ``outcome_unknown``：恢复层对它**只告警、绝不重放**。
                logger.warning(
                    "prompt outcome is UNKNOWN for %s (no HTTP status from the"
                    " transport layer); not marking it failed, because the remote"
                    " side may already have run it: %s",
                    queued.delivery_id, exc,
                )
                self._send_text(
                    conversation_id,
                    f"{UNKNOWN_OUTCOME_NOTICE}（{exc}）",
                    kind="error",
                    adapter=adapter,
                )
                if inbox is not None:
                    inbox.mark_outcome_unknown(
                        queued.delivery_id, f"prompt outcome unknown: {exc}"
                    )
                return "error"
            logger.warning("prompt failed: %s", exc)
            self._send_text(
                conversation_id,
                f"发送失败: {exc}（可尝试 /new 重建会话）",
                kind="error",
                adapter=adapter,
            )
            if inbox is not None:
                inbox.mark_failed(queued.delivery_id, f"prompt failed: {exc}")
            return "error"
        except Exception as exc:
            # ⚠️ 非 ``OpenCodeError``（连 status 都没有）⇒ 我们**同样**不知道请求发出
            # 没有。记 ``failed`` 仍然是猜 —— 而且是猜错方向最贵的那种：重放会让 agent
            # 对同一条指令跑两遍。⇒ 与上面那一支落同一个第三态。
            # ⛔ 而这一支**曾经**带 ``pragma: no cover``；现在
            # ``tests/test_inbound_gateway`` 有一条用例真的走它（见
            # ``DispatchPromptTests``），那个 pragma 已经不成立，所以一并去掉 ——
            # 留着它等于对覆盖率工具说谎。
            logger.exception("prompt failed with a non-OpenCodeError; treating the"
                             " outcome as unknown rather than as a failure")
            self._send_text(
                conversation_id,
                f"{UNKNOWN_OUTCOME_NOTICE}（{exc}）",
                kind="error",
                adapter=adapter,
            )
            if inbox is not None:
                inbox.mark_outcome_unknown(
                    queued.delivery_id, f"prompt outcome unknown: {exc}"
                )
            return "error"

        if inbox is not None:
            inbox.mark_delivered(queued.delivery_id)
        # success: create / reuse this turn's progress message
        with self._lock:
            turn = self._turns.get(session_id)
            if turn is None:
                turn = Turn(conversation_id=conversation_id)
                self._turns[session_id] = turn
            # ⚠️ 「创建这一轮的那一条进度消息」是一次**跨 send 的归属**：锁内领取、
            # 锁外发、锁内归还。这个字段与事件流那一侧**共用同一个**
            # （见 :attr:`~opencode_bridge.event_stream.Turn.progress_message_creating`
            # 的完整说明）—— 此前两条路径各自的 ``progress_handle is None`` 都在锁内
            # 读、而**发送与写回都在锁外**，于是「占位消息」与「流式首片」各发一条、
            # 其中一条的句柄被丢掉（读者把同一段正文读两遍，或看见一个永远停在
            # 「⏳ 处理中…」的僵尸气泡）。
            create_progress = (
                turn.progress_handle is None
                and not turn.progress_message_creating
            )
            if create_progress:
                turn.progress_message_creating = True
        # ⚠️ ``create_progress`` 为假（这一轮已经有句柄，或**有人正在创建**它）时这里
        # **不发**占位消息，而这**不丢任何东西**：占位文案本身没有信息量（正文一直记在
        # ``turn.parts`` 里），真正在创建的那条消息会承载它；它若发失败，
        # ``progress_handle`` 仍是 ``None``，收尾那一步照旧整段发出完整答复。
        # 反过来多发一条的代价则是实打实的：多一条用户看得见、却可能永远不会被答复
        # 改写的孤儿消息（平台没有「撤回」原语，它清不掉）。
        if create_progress:
            handle = None
            try:
                handle = self._send_text(
                    conversation_id,
                    PROGRESS_TEXT,
                    kind="progress",
                    adapter=adapter,
                    session_id=session_id,
                )
            finally:
                # 成功**和**抛异常都要把创建权还回去（见事件流那一侧的同一段注释）。
                with self._lock:
                    current = self._turns.get(session_id)
                    if current is not None:
                        current.progress_message_creating = False
                        if current.progress_handle is None:
                            current.progress_handle = handle
        return "ok"

    # ------------------------------------------------------------------
    # inbox recovery (startup)
    # ------------------------------------------------------------------
    def recover_inbox(self) -> None:
        """Replay whatever the last crash left in the inbox.

        **为什么夹在 SSE 线程与适配器之间**（两个邻居都不是随便选的）：

        * **在 SSE 线程之后** —— 重放出去的 prompt 必须有人接它的回复。
          ``session.execution.started`` 整条丢掉的话就没有 :class:`Turn`，
          收尾时既不发布结果也不刷队列，用户对这条消息什么都看不到 ——
          于是"修好丢消息"变成"重放出一条没有回复的消息"。
          为此这里等事件流确认连上（有上限）：服务端握手后发的第一帧
          （``server.connected``）到达即证明订阅已建立。
        * **在适配器之前** —— 此刻还没有实况入站，重放不会和用户的新消息
          并发打同一个会话（那种交错会把其中一条变成 409，甚至两次都成功）。
        * **告警仍然送得出去** —— 这正是看上去的矛盾点：``notify`` 需要可用
          的适配器，但扫描跑在 ``adapter.start()`` 之前。两者并不冲突：
          ``Adapter.send`` 是纯出站（``start()`` 只负责入站轮询；见
          ``adapters/telegram.py`` 的 ``send`` 与 ``start``），所以适配器
          已 attach 就能发。真的发不出去时 :meth:`BridgeCore._send_text` 记警告，这里
          再记一条 —— 这段告警只有这一条路，静默丢掉等于没告警。

        ``uncertain`` / ``abandoned`` **已经**被 :func:`recover_pending`
        告警过，这里只记日志，绝不重发。
        """
        inbox = self._inbox
        if inbox is None:
            logger.info(
                "write-ahead inbox disabled (no inbox injected); "
                "a crash during delivery loses that message silently"
            )
            return
        if not self._stream_confirmed.wait(
            timeout=_EVENT_STREAM_CONFIRMED_TIMEOUT_SECONDS
        ):
            logger.warning(
                "event stream not confirmed within %.1fs; running inbox recovery "
                "anyway (replays will most likely fail too — see the logs below)",
                _EVENT_STREAM_CONFIRMED_TIMEOUT_SECONDS,
            )

        def dispatch_recovered(queued: QueuedPrompt) -> None:
            """Hand one recovered prompt to opencode.

            Goes through the very same :meth:`_dispatch_prompt` a live message
            takes, which is also why the inbox bookkeeping is split the way it
            is: :func:`~.inbox_recovery.recover_pending` owns the writes here
            (it brackets this call with ``mark_attempting`` / ``mark_delivered``),
            so a second ``mark_failed`` from inside the dispatch would burn two
            retry-budget steps for one failure.

            Anything other than ``ok`` raises so recovery records ``failed`` and
            the next boot retries on the backoff ladder. ``busy`` included: at
            startup there is no in-memory queue to fall back into.
            """
            outcome = self._dispatch_prompt(queued, recording_delivery=False)
            if outcome != "ok":
                raise OpenCodeError(
                    f"replay of {queued.delivery_id} returned {outcome!r}"
                )

        def notify_recovered(conversation_id: str, alert_text: str) -> None:
            """Send one user-visible recovery alert; never let it vanish."""
            if self._send_text(conversation_id, alert_text, kind="text") is None:
                logger.warning(
                    "inbox recovery: could not deliver the alert for %s; "
                    "the user may never learn about it",
                    conversation_id,
                )

        logger.info("write-ahead inbox enabled; scanning for rows left by a crash")
        outcome = recover_pending(
            inbox,
            dispatch=dispatch_recovered,
            notify=notify_recovered,
        )
        logger.info(
            "inbox recovery done: %d replayed, %d uncertain (alerted, NOT replayed),"
            " %d abandoned (alerted)",
            len(outcome.replayed),
            len(outcome.uncertain),
            len(outcome.abandoned),
        )

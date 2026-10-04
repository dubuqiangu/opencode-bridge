"""入站合并：**只有用户自己敲了 ``..`` 才合并**，其余消息零延迟直发。

C3。**这一段的功能名与实现都来自对 `zhuiyueya/dsh-im-gateway` 的核实**
（快照 `zhuiyueya-dsh-im-gateway-8a5edab282632443.txt`，commit `8a5edab282632443`：
`src/core/merge.ts` 的 `SessionMerger`）。下面每一条"抄/改"都标了原因。

**先说清前提，因为台账里那一行的理由是没有证据的**（见
``docs/platform-design-reference.md:167`` 声称的"IM 长按输入把一句话拆成
3~5 条"）：

* **没有任何平台会自动把长输入拆成多条。** 13 个适配器逐个核实过：每一条协议
  消息产出**恰好一个** ``Inbound``，正文逐字取自协议的单个字段。实测一次
  4 行的粘贴会变成 **4 次** ``prompt()``（同一个 opencode session）—— 代价是
  真的（agent 跑 4 轮、上下文碎裂），但它只在**一个平台消息装不下**时才会发生。
* **装不下的只有两家**：IRC 一行 512 字节（RFC 2812），多行粘贴真的会被客户端
  拆成 N 条 ``PRIVMSG``；Twitch 聊天 400 字符硬上限，想发长文只能自己分条。
  另外 11 家（Slack 40000、Nextcloud 32000、Matrix/Mattermost/ntfy 4096/4000、
  Telegram 4096、Discord/QQ 2000、邮件与 a2a 无上限）一条就装得下。
* **没有任何平台会"发出" ``..`` 或 ``!!``。** 这是 irssi / weechat 那种终端
  客户端的多行粘贴约定，由**用户自己敲**。dsh 把这个约定做成了产品功能；
  Hermes（``nousresearch-hermes-agent-8a5edab282632443.txt``）**完全没有**标记
  处理，它把批处理做在 ``BasePlatformAdapter`` 上，但只有 **2/31** 个平台启用
  （SimpleX 0.8s、WeCom 0.6s/2.0s，后者注释写着 "clients split long messages
  ~4000 chars"）。

**与 dsh 的四处刻意分歧**（每一条都有理由，不是"抄错了"）：

1. **裸文本直发，不进窗口。** dsh 把裸文本缓存 ``mergeTimeoutSecs`` 秒
   （默认 5）。那是**给每一条消息**加最多 5 秒延迟 —— 为 2 个平台的收益让 13 个
   平台买单。这里改成：没有标记 = 立刻发出去，**一条消息的额外延迟是 0**，
   而且是**可证明的**0（没有标记就不会起任何计时器），不是"权衡后接受"。
2. **超时只当保险丝，且只在缓冲存在时存在。** 没有它，一次敲了 ``..`` 就再没下文
   的那条消息会永远卡住。它**不是**主机制，所以时长可以给得比 dsh 的 5s 长得多
   （人打完一行再发出来要好几秒）；真正被它救到的只有"敲了 ``..`` 然后走开"的人。
3. **拼接用换行，不是空串。** dsh 是 ``existing + text``（``merge.ts`` 的
   ``ingest``）—— 那是把两行代码接成一行。逐行粘贴的语义是**换行**。
4. **不用 ``snapshots()`` 落盘。** 见下方"为什么不落盘"。

**为什么不落盘**：缓冲里是**用户已经被告知"收到了、在等续行"**的内容，而它的
存活期只跨两条入站消息（毫秒级，且要求进程一直活着）。dsh 做了 ``snapshots()``
但**自己没接通**（``snapshots()`` 零调用方，``docs/platform-design-reference.md:131``
也记了这条"merge 快照恢复未接通"）。落盘还会引出更糟的一问：崩在合并中途时，
那条残缺的并集算"一条完整消息"吗？算就等于替用户编了一句他没说的话。
**要持久化的是"别丢这条"，那是 `InboundInbox` 的活**（一个 `QueuedPrompt` 就是
一条已落盘的完整消息）。把并集塞进那条队列正是"别丢"与"这是一件事"被混为一谈 ——
所以本模块的缓冲**刻意不落盘、且刻意在 `InboundInbox` 之前**。

⚠️ **残留风险（明确记下来，不假装解决了）**：判据是"末尾是不是 ``..``"，所以
一句正好以 ``..`` 结尾的散文（``等等..``）会被当成续行标记而**暂时不发**。
本模块对此的处置是**每一行被缓冲时都立刻回一句**（:data:`BUFFERED_NOTICE`），
把最坏情况从"消息静默消失"降级成"用户看到一句提示、并且知道敲 ``!!`` 立刻发"。
保险丝超时也会回一句（:data:`HELD_EXPIRED_NOTICE`），所以缓冲的内容**任何路径
都不会无声消失**。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass

__all__ = [
    "ConversationMerger",
    "IngestResult",
    "OnHoldExpired",
    "DELIVER",
    "HELD",
    "IGNORED",
    "CONTINUE_NEXT",
    "SUBMIT_NOW",
    "NO_DIRECTIVE",
    "split_off_directive",
    "BUFFERED_NOTICE",
    "HELD_EXPIRED_NOTICE",
]

logger = logging.getLogger("opencode_bridge.inbound_merge")


# ----------------------------------------------------------------------
# 控制后缀（**用户敲的**，没有任何平台会替你发）
# ----------------------------------------------------------------------
#: 敲在行尾表示"还有下一行"。
CONTINUE_SUFFIX = ".."
#: 敲在行尾表示"就这些，立刻发"。
SUBMIT_SUFFIX = "!!"

#: :func:`split_off_directive` 的三种判定。
NO_DIRECTIVE = "no_directive"
CONTINUE_NEXT = "continue_next"
SUBMIT_NOW = "submit_now"

#: :meth:`ConversationMerger.ingest` 的三种结果。
DELIVER = "deliver"
HELD = "held"
IGNORED = "ignored"

#: :data:`CONTINUE_SUFFIX` / :data:`SUBMIT_SUFFIX` 的哪一个判为先。
#: 顺序与 dsh 一致（``merge.ts`` 的 ``stripControlSuffix`` 也是先试 ``!!``）：
#: ``!!`` 更明确，用户想立刻发的时候不会因为末尾还多两个点而误判成续行。
_DIRECTIVE_BY_SUFFIX = (
    (SUBMIT_SUFFIX, SUBMIT_NOW),
    (CONTINUE_SUFFIX, CONTINUE_NEXT),
)

#: 缓冲住一行之后回给用户的那句。**必须有** —— 没有它，一个被误判成续行的散文
#: 就是静默消失，而"用户静默地遇到错的东西"是 AGENTS.md §8 点名最糟的代价。
BUFFERED_NOTICE = "已收到这一行，还在等下一行（续行用 ..，立刻发送用 !!）"

#: 保险丝超时后回给用户的那句：说清"发出去了"以及"发出去的是什么"。
HELD_EXPIRED_NOTICE = "等待下一行超时，已把等到的内容原样发出：\n%s"


#: 保险丝到点时的回调：``(conversation_id, held_text) -> None``。
#: 传进来的 ``held_text`` **已经被摘走**了（回调失败也不会有第二次机会）。
OnHoldExpired = Callable[[str, str], None]


@dataclass(frozen=True)
class IngestResult:
    """What one inbound line turned into."""

    #: :data:`DELIVER` / :data:`HELD` / :data:`IGNORED`。
    kind: str
    #: :data:`DELIVER` 时**要交给 agent 的完整正文**（顺序原样，标记已剥掉）。
    text: str = ""
    #: :data:`HELD` 时**此刻缓冲里的全部内容**（给回执用，用户看得见攒到哪了）。
    held: str = ""


def split_off_directive(raw: str) -> tuple[str, str]:
    """``raw`` -> ``(body, directive)``，标记本身从 ``body`` 里剥掉。

    先去掉**尾部**空白再判后缀：手机输入法或 IRC 客户端很容易在 ``!!`` 后面留一个
    空格，而 ``"就这些!! "`` 判不出标记就等于没有这个功能。

    ⚠️ 判据是"以 ``..`` 结尾"，所以一句**正好**以 ``..`` 结尾的散文会被当成续行
    标记。缓解办法不在这里（把判据改成"整行就是标记"会牺牲"贴在行尾"的用法，
    而那种用法更常见），而在 :meth:`ConversationMerger.ingest` —— 它每缓冲一行
    就回一句，于是最坏情况是"用户看到一句提示"，不是"消息不见了"。
    """
    text = str(raw or "").rstrip()
    for suffix, directive in _DIRECTIVE_BY_SUFFIX:
        if text.endswith(suffix):
            # 切完再 rstrip：``"a.. "`` 的 body 应当是 ``"a"`` 而不是 ``"a "``。
            return text[: -len(suffix)].rstrip(), directive
    return text, NO_DIRECTIVE


class ConversationMerger:
    """Marker-gated continuation buffer, one pending line per conversation.

    **零延迟是这一类的硬要求**：没有标记的消息 :meth:`ingest` 立刻返回
    :data:`DELIVER`，不起计时器、不进缓冲、不加任何等待。缓冲只在用户敲了
    :data:`CONTINUE_SUFFIX` 之后才存在。

    线程安全：入站来自各适配器自己的线程（一个适配器一个线程，但那个线程会轮流
    处理多个会话），保险丝超时又来自 :class:`threading.Timer` 的线程。
    """

    def __init__(
        self,
        *,
        hold_timeout_seconds: float,
        on_hold_expired: OnHoldExpired,
    ) -> None:
        """``on_hold_expired(conversation_id, held_text)`` 收到的是**已经被摘走**的内容。

        回调在**锁外**调用，所以它可以再去发消息、可以再调本对象。
        """
        self._hold_timeout_seconds = max(0.0, float(hold_timeout_seconds))
        self._on_hold_expired = on_hold_expired
        self._lock = threading.Lock()
        #: conversation_id -> 缓冲正文。
        self._held: dict[str, str] = {}
        #: conversation_id -> 保险丝。只有缓冲存在时才有计时器。
        self._fuses: dict[str, threading.Timer] = {}

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------
    def ingest(self, conversation_id: str, raw: str) -> IngestResult:
        """One inbound plain-text line -> :class:`IngestResult`."""
        body, directive = split_off_directive(raw)
        key = str(conversation_id or "")

        with self._lock:
            if directive == CONTINUE_NEXT:
                merged = self._join(self._held.get(key, ""), body)
                if not merged:
                    # 光一个 `..`、前面什么都没有：没有可缓冲的内容，也就不该
                    # 起计时器（否则会凭空造出一个"缓冲"）。
                    return IngestResult(kind=IGNORED)
                self._held[key] = merged
                self._rearm_fuse(key)
                return IngestResult(kind=HELD, held=merged)

            # `!!` 与裸文本都**立刻**发出去。差别只在"有没有在等的东西"。
            held = self._held.pop(key, "")
            self._cancel_fuse(key)
            merged = self._join(held, body)
            if not merged:
                return IngestResult(kind=IGNORED)
            return IngestResult(kind=DELIVER, text=merged)

    def flush(self, conversation_id: str) -> str:
        """Take out whatever is held for that conversation (``""`` if nothing).

        给"这条入站不走合并"的地方用 —— 目前调用方是启动时的缓冲清理。
        """
        key = str(conversation_id or "")
        with self._lock:
            held = self._held.pop(key, "")
            self._cancel_fuse(key)
            return held

    def held_text(self, conversation_id: str) -> str:
        """What is held right now, without taking it (``""`` if nothing)."""
        with self._lock:
            return self._held.get(str(conversation_id or ""), "")

    def held_conversation_ids(self) -> tuple[str, ...]:
        """Every conversation with something held — for shutdown and for tests."""
        with self._lock:
            return tuple(self._held)

    def stop(self) -> None:
        """Cancel every fuse. **Does not** discard the held text."""
        with self._lock:
            for key in tuple(self._fuses):
                self._cancel_fuse(key)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    @staticmethod
    def _join(held: str, body: str) -> str:
        """Hold-then-append, joined by a **newline**.

        ⚠️ 这里与 dsh 刻意不同：它是 ``existing + text``（``merge.ts`` 的
        ``ingest``），也就是把两行接成一行。逐行粘贴的语义是换行 —— 一段
        ``def f(x):`` / ``    return x`` 被接成 ``def f(x):    return x`` 时，
        送给 agent 的就不是用户写的东西了。
        """
        if not held:
            return body
        if not body:
            return held
        return "%s\n%s" % (held, body)

    def _rearm_fuse(self, key: str) -> None:
        """Caller holds ``self._lock``. Caller must be :meth:`ingest`."""
        self._cancel_fuse(key)
        if self._hold_timeout_seconds <= 0:
            # 0 = 关掉保险丝。只在显式配置时如此；默认不为 0。
            return
        fuse = threading.Timer(self._hold_timeout_seconds, self._fuse_fired, [key])
        fuse.daemon = True
        self._fuses[key] = fuse
        fuse.start()

    def _cancel_fuse(self, key: str) -> None:
        """Caller holds ``self._lock``."""
        fuse = self._fuses.pop(key, None)
        if fuse is not None:
            fuse.cancel()

    def _fuse_fired(self, key: str) -> None:
        """Fuse thread body: take the text out, then report it **outside** the lock."""
        with self._lock:
            # 竞态：这一行可能已经被 ``ingest`` 取走并发了 —— 那就不该再发一遍。
            held = self._held.pop(key, "")
            self._fuses.pop(key, None)
        if not held:
            return
        logger.info(
            "inbound merge: hold for %s expired after %.1fs; sending %d chars",
            key, self._hold_timeout_seconds, len(held),
        )
        try:
            self._on_hold_expired(key, held)
        except Exception:  # pragma: no cover - 兜底
            logger.exception("inbound merge: reporting the expired hold failed")

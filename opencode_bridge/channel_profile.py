"""C1 —— 让 agent 知道**自己正在哪个渠道上说话**（提示由适配器能力推导，不查表）。

为什么需要它：入站正文是**逐字**转给 opencode 的（见
:func:`~opencode_bridge.inbound_gateway._queued_prompt_for`），所以 agent 眼里的
一条消息就是用户在 IRC 上敲的那几十个字，它无从得知这条回复会被拆成几条、
会不会被渲染成表格、以及读者其实还在等。于是它按"写文档"的方式作答 —— 400 字符的
IRC 上发一屏表格，或者在 Slack 上把三段话并成一行。

**这个模块只回答一个问题：这条回复发出去的时候，渠道是什么样。** 答案全部来自
**适配器自己声明的能力**（:mod:`opencode_bridge.adapters.base`）：

* :attr:`~opencode_bridge.adapters.base.Adapter.label` —— 平台展示名；
* :meth:`~opencode_bridge.adapters.base.Adapter.effective_max_length`
  —— **运行期真正生效**的单条上限，即 :meth:`~opencode_bridge.adapters.base.Adapter.send`
  实际切分的那个数（Mattermost / Nextcloud 启动后会用服务端配置把它细化，
  所以这里必须取运行期值而不是类属性 ``max_message_length``）；
* :attr:`~opencode_bridge.adapters.base.Adapter.splits_long_messages`
  —— 那个数**能不能当长度说**。邮件的 998 是 RFC 5322 的**单行**上限、A2A 的是
  **请求体字节**上限，两家都从不切片，当成"消息容量"报出去就是假话；
* :attr:`~opencode_bridge.adapters.base.Adapter.supports_message_edit`
  —— 读者在等待期间**看得见什么**。平台能把已发消息原地改写时，那条占位消息会被
  顶掉；不能改写的平台上桥**根本不发**它，于是等待期间读者什么也看不见 ——
  这两句不能共用一句，见 :meth:`ChannelProfile._progress_visibility`；
* :attr:`~opencode_bridge.adapters.base.Adapter.supports_inline_buttons`。

⚠️ **刻意没有"平台 → 上限"的对照表。** 那样一张表对第 14 个平台一定是错的，
而且没有任何东西会报错。这里每个数都问适配器本人要，所以新增平台只要照例声明
能力，提示自动跟着变 —— 这是唯一不会腐烂的形态。

⚠️ **正文里刻意不含内部信息**：没有 conversation id、没有文件路径、没有主机名、
没有令牌。唯一的外部输入是平台**展示名**，而它走
:func:`_safe_display_label` 三道闸（字符集 + 数字段 + 脱敏层自证），
任何一条不过就整个丢掉、退回"某个聊天窗口"，而不是把可疑内容带进 prompt。

**为什么用英文**：这段文字是**给模型读的**，不是给 IM 用户看的。IM 侧的用户可见
文案全是中文（``⏳ 处理中…``、``/help``），而 prompt 正文是用户用什么语言就用什么
语言；一段固定语言的元信息用英文最不容易和用户自己的语言打架。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .adapters import Adapter
from .redaction import default_redactor

__all__ = ["ChannelProfile", "channel_profile_for", "with_channel_hint"]

#: 分隔标记：把渠道说明与用户正文**明确**切开，否则模型可能把说明当成用户的话。
_HINT_SEPARATOR = "---"

#: 上限低于此值时，"尽量写短"比"别超长"更贴近那条渠道的真实处境（IRC 400、
#: Twitch 400 与 Slack 40000 需要的是两种建议）。判据是**适配器报上来的数**，
#: 不是某个平台的名单 —— 第 14 个平台同样适用。
_SHORT_MESSAGE_CHARS = 1000

#: 平台展示名允许的形状：字母开头，随后是字母 / 数字 / 空格。
#: 刻意**不含** ``.`` / ``-`` / ``:`` / ``@`` / ``/`` / ``\`` —— 于是主机名
#: （``chat.example.com``）、路径、URL、邮箱在字符集这一关就全被挡掉。
_SAFE_LABEL = re.compile(r"[A-Za-z][A-Za-z0-9 ]{0,31}")

#: 连续 6 位以上的数字：平台 id / 雪花号 / 时间戳的形状。展示名里不该出现，
#: 出现即视为不可信（这一条是给**合法字符**补的洞，纯数字串是合法的）。
_OPAQUE_DIGIT_RUN = re.compile(r"[0-9]{6,}")


def _safe_display_label(*candidates: object) -> str:
    """返回第一个**可以证明不含内部信息**的展示名，都不可信则返回空串。

    三道闸都要过，缺一不可：

    1. **字符集**（:data:`_SAFE_LABEL`）—— 挡掉路径、URL、主机名、邮箱；
    2. **数字段**（:data:`_OPAQUE_DIGIT_RUN`）—— 挡掉纯数字的 id；
    3. **脱敏层自证**（:func:`~opencode_bridge.redaction.Redactor.scrub`）——
       Slack / GitHub / OpenAI 令牌、`platform:local_id` 会话 id、手机号、邮箱
       **全是合法字符**，前两道都拦不住。判定方式是"洗过之后逐字节没变"。
       ⚠️ 所以这里**不**把 `[REDACTED:...]` 写进 prompt：那是日志的呈现方式，
       在给模型看的正文里只会是噪声。变了就说明这个名字不可信 —— 丢掉它。
    """
    for candidate in candidates:
        text = str(candidate or "").strip()
        if not text or not _SAFE_LABEL.fullmatch(text):
            continue
        if _OPAQUE_DIGIT_RUN.search(text):
            continue
        try:
            if default_redactor().scrub(text) != text:
                continue
        except Exception:  # noqa: BLE001 - 判定失败就当不可信，绝不放行
            continue
        return text
    return ""


@dataclass(frozen=True)
class ChannelProfile:
    """One channel, as far as a reply written for it needs to know.

    三个字段都是**适配器声明的能力**，没有一个是本模块自己查表得来的，所以
    ``ChannelProfile`` 是纯数据：构造出来之后就是一份可复述、可断言的**值**。
    """

    label: str
    max_message_chars: int
    splits_long_messages: bool
    supports_inline_buttons: bool
    supports_message_edit: bool

    @classmethod
    def from_adapter(cls, adapter: Adapter) -> "ChannelProfile":
        """Read the channel's shape off ``adapter``.

        ``effective_max_length`` 是**运行期**值：Mattermost / Nextcloud 启动后
        会用服务端自己的配置把它细化（见 ``adapters/mattermost.py`` 的
        ``_apply_max_post_size``），所以这里读到的就是 :meth:`Adapter.send`
        真正会切分的那个数。读类属性 ``max_message_length`` 会在那两家上
        报出一个**比实际宽**的数 —— 而"提示说 40000、实际按 400 切"比不给提示更糟。

        ``splits_long_messages`` 决定那个数**能不能当长度说**：邮件的 998 是
        RFC 5322 的单行上限、A2A 的是请求体字节上限，两家都从不切片，
        把它们当"消息容量"报出去就是在对模型说假话（见
        :attr:`~opencode_bridge.adapters.base.Adapter.splits_long_messages`）。

        ``supports_message_edit`` 决定等待期间读者**看得见什么**。平台不能原地
        改写已发消息时，出站那道闸门**根本不发**占位消息（见
        :meth:`~opencode_bridge.outbound.OutboundSender.send_text`），所以
        "读者会先看到一条占位消息"这句话在那些平台上**是假的** —— 而一条骗人的
        渠道说明比没有渠道说明更糟（§8）。
        """
        return cls(
            label=_safe_display_label(adapter.label, adapter.name),
            max_message_chars=int(adapter.effective_max_length),
            splits_long_messages=bool(adapter.splits_long_messages),
            supports_inline_buttons=bool(adapter.supports_inline_buttons),
            supports_message_edit=bool(adapter.supports_message_edit),
        )

    def render(self) -> str:
        """The channel note, as plain prose.

        每一条都必须能指着**本仓库或平台**的某个事实兑现，否则就是"愿望型提示"：
        只讲会发生什么，不讲平台未承诺的事（所以不写"这里没有任何渲染"——
        部分平台确实渲染一小部分 markdown；写的是"没有任何东西被转换，
        你打的星号就是星号"，那对每一家的 send() 都成立）。
        """
        channel = self.label or "a chat channel"
        lines = [
            "You are replying in %s." % channel,
            "",
            self._length_advice(),
            "",
            "This is a short message, not a document. Nothing you write is "
            "converted on the way out: asterisks, backticks, headings, tables "
            "and links reach the reader exactly as typed, so write plain "
            "sentences and put the answer first.",
            "",
            self._progress_visibility(),
            "",
            self._reply_expectation(),
            "",
            "These notes are about the channel only. Do not mention them, and "
            "do not spend your answer on them.",
        ]
        return "\n".join(lines).strip()

    def prepend(self, user_text: str) -> str:
        """``user_text`` with the note in front.

        ``user_text`` 逐字节保留在末尾。⚠️ **拼接只发生在这里** —— 收件箱里存的、
        去重哈希算的、重放重发的都还是用户原文（见
        :func:`~opencode_bridge.inbound_gateway._queued_prompt_for` 的不变量）。
        """
        return "%s\n\n%s\n%s" % (self.render(), _HINT_SEPARATOR, user_text)

    # ------------------------------------------------------------------
    # 三条随能力变化的句子
    # ------------------------------------------------------------------
    def _progress_visibility(self) -> str:
        """等待期间读者**看得见什么**。

        这一支是本模块里**最容易被写假**的一句，所以它必须跟着
        :attr:`~opencode_bridge.adapters.base.Adapter.supports_message_edit` 走：

        * **能改写** —— 占位消息会被最终答复顶掉，读者全程有"正在动"的迹象。
          ⚠️ 答复装不下一条消息时，那个位置装的是答复的**开头一段**、其余作为
          后续消息接在下面（见 :meth:`~opencode_bridge.outbound.OutboundSender.finalize`），
          所以这一支说的是"开头落在原处、其余在下面"，**不是**"整条答复被顶上去"。
        * **不能改写** —— 桥**根本不发**那条占位消息（见
          :meth:`~opencode_bridge.outbound.OutboundSender.send_text`），所以
          "你会先看到一条占位消息"在这里**是假的**。真话是：等待期间读者什么都
          看不见。这条对模型有用：它因此不会假设"用户在等我"，也不会写出
          "我继续看看"这种只有配一条后续更新才成立的话。
        """
        if not self.supports_message_edit:
            return (
                "The reply is not instant, and nothing at all appears while "
                "you work: until your finished answer is ready the reader has "
                "no sign that anything is happening. Do not assume they know "
                "you are still working, and do not write anything that only "
                "makes sense as an interim update. Delivery itself may add "
                "more delay on top of that."
            )
        return (
            "The reply is not instant. The reader first sees a short "
            "placeholder while you work, and the platform's own delivery may "
            "add more delay. When the answer is longer than one message, its "
            "beginning lands in that same message and the rest follows in "
            "further messages below."
        )

    def _length_advice(self) -> str:
        if not self.splits_long_messages:
            # 这一支**不给数字**：适配器自己声明了「那个数不是消息容量」
            # （邮件是 RFC 5322 的单行上限、A2A 是请求体字节上限，两家都从不切片）。
            # 说不出真话就不说 —— 一个编出来的长度比没有长度更糟。
            return (
                "A long answer is not cut into several messages here, so there "
                "is no length to stay inside."
            )
        if self.max_message_chars < _SHORT_MESSAGE_CHARS:
            return (
                "One message holds about %d characters, which is only a "
                "couple of sentences. Write brief replies and let a long "
                "answer run over several short messages rather than trying to "
                "squeeze it into one."
                % self.max_message_chars
            )
        return (
            "One message holds about %d characters, so a long answer still "
            "fits, but anything past that is split and reaches the reader as "
            "several separate messages." % self.max_message_chars
        )

    def _reply_expectation(self) -> str:
        if self.supports_inline_buttons:
            return (
                "This channel can show buttons, so an answer may come back as "
                "a tap rather than as typed text."
            )
        return (
            "There are no buttons here, so the reader answers by writing back."
        )


def channel_profile_for(adapter: Adapter) -> ChannelProfile:
    """:meth:`ChannelProfile.from_adapter`，给不想写类名的一方用。"""
    return ChannelProfile.from_adapter(adapter)


def with_channel_hint(user_text: str, adapter: Adapter | None) -> str:
    """把渠道说明拼在 ``user_text`` 前面。

    ``adapter`` 取不到时**原样返回** —— 那是路由已经坏了的场合（上游
    :class:`~opencode_bridge.inbound_gateway.InboundGateway` 会先告警再放弃），
    这时再多改一个字都只是把一个故障变成两个故障。
    """
    if adapter is None:
        return user_text
    return ChannelProfile.from_adapter(adapter).prepend(user_text)

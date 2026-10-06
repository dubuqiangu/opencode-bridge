"""Lane B — adapter ABC and registry (CONTRACT.md §2.1 / §2.4)."""

from __future__ import annotations

import abc
import importlib
import importlib.util
import logging
import os
import pkgutil
import re
import threading
from typing import Dict, Optional, Type

from ..allowlist import (
    AllowlistResolution,
    resolve_allowlist,
    warn_if_conflicting_keys,
    warn_if_no_allowlist,
)
from ..health import VERDICT_FAILED, normalize_verdict
from ..hooks import Hooks, MsgHandle, Outbound, SendError, SendResult
from ..pairing import (
    CONFIG_VERSION_KEY,
    PAIRING_SECRET_KEY,
    derive_pairing_code,
    pairing_reply_text,
    reads_pairing_trigger,
    warn_if_pairing_unavailable,
)

logger = logging.getLogger("opencode_bridge.adapters.base")

__all__ = [
    "Adapter",
    "AdapterError",
    "build",
    "register",
    "classify_http",
    "adapter_class",
    "registered_names",
]


def classify_http(status: int, detail: str = "") -> SendError:
    """把 HTTP 状态码 / 平台描述收敛成平台中立的失败分类（T1.3）。

    消费方因此不必对厂商报错文本做 substring-match。平台特有的补充判定
    （如 Telegram 400 + "message is too long" -> ``TOO_LONG``）由各适配器自己
    在 :meth:`Adapter._note_send_failure` 处传入更精确的 ``kind``。
    """
    if status <= 0:
        return SendError.TRANSIENT          # 传输层失败（未拿到状态码）
    if status == 429:
        return SendError.RATE_LIMITED
    if status in (403, 401):
        return SendError.FORBIDDEN
    if status == 404:
        return SendError.NOT_FOUND
    if status == 413:
        return SendError.TOO_LONG
    if 400 <= status < 500:
        low = (detail or "").lower()
        if "too long" in low or "too large" in low or "message is too long" in low:
            return SendError.TOO_LONG
        return SendError.BAD_FORMAT
    if status >= 500:
        return SendError.TRANSIENT
    return SendError.UNKNOWN


class AdapterError(Exception):
    """Raised for adapter configuration / registration problems."""


# Registry populated by the individual adapter modules via ``register``.
_REGISTRY: Dict[str, Type["Adapter"]] = {}


def register(name: str):
    """Class decorator: add ``cls`` to the build registry under ``name``."""

    def deco(cls: Type["Adapter"]) -> Type["Adapter"]:
        _REGISTRY[name] = cls
        return cls

    return deco


class Adapter(abc.ABC):
    """Base class for messaging platform adapters.

    Lifecycle: ``start()`` spawns a poller thread (non-blocking, never raises
    to the caller); ``stop()`` sets the stop flag and joins the thread with a
    5 second timeout.

    能力以**类属性显式声明**（T1.1），调用方据此判断"能不能发按钮 / 该不该分片"，
    不再靠 try/except 撞运气。取值必须与各平台官方限制一致。
    """

    name: str = ""

    # --- capabilities（显式声明；子类必须按平台真值覆盖）------------------
    #: 展示名（状态视图 / /setup 引导用），如 ``"Telegram"``。
    label: str = ""
    #: 单条消息的字符上限；出站分片（T1.4）以此为阈值。
    max_message_length: int = 4000
    #: **运行期细化槽**：服务端告诉我们的、比 :attr:`max_message_length` **更严**的
    #: 单条出站上限；``0`` = 服务端没说（就按 :attr:`max_message_length` 切）。
    #:
    #: 只有拿得到服务端上限的平台才写它：Mattermost 用 ``MaxPostSize``、Nextcloud 用
    #: ``spreed.config.chat.max-length``（见两家的 ``_apply_*``）。
    #:
    #: ⚠️ 默认**必须**是 ``0``，不能是 :attr:`max_message_length`。这个槽一旦自带初值，
    #: 它就和 :attr:`max_message_length` 变成两个同义不同名的数，每个新消费者都得重新
    #: 猜一遍该读哪个、每个适配器还得各自抄一份解析 —— 那正是此前**十二个**适配器
    #: 重复声明它的原因，而漏掉声明的那个会在**投递时**才 ``AttributeError``。
    #: 声明平台上限只需写 :attr:`max_message_length`（能力声明，:meth:`capabilities`
    #: 对外报的就是它）；要读"实际按多少切"一律读 :attr:`effective_max_length`。
    message_limit: int = 0
    #: 是否具备入站（接收）能力。False = 只能主动发送。
    supports_inbound: bool = False
    #: 是否支持 inline 按钮 / 卡片式交互。
    supports_inline_buttons: bool = False
    #: 是否支持发送图片 / 文件等媒体。
    supports_media: bool = False
    #: 是否能把**已经发出去的那条消息**原地改写（:meth:`edit` 真的有这个能力）。
    #:
    #: **默认 ``False``，而且这个默认值是承重的。** 它决定的不是"要不要做流式
    #: 改写"，而是**要不要创建那条占位消息**：``⏳ 处理中…`` 是一张**承诺**——
    #: 承诺收尾时会被最终答复顶掉。平台兑现不了这个承诺（``edit()`` 恒返回
    #: ``False``）时，那条气泡就永远不会消失，于是**每一轮**都留下一个
    #: "还在处理中"的僵尸气泡 + 一条真正的答复。
    #:
    #: 与本仓库状态键迁移的纪律同源（§8）：**不要写你以后要抛下的东西**。
    #: 所以默认取"**假定清不掉**"，要发占位消息必须由适配器**显式声明**自己
    #: 清得掉 —— 少声明的代价是"没有流式进度"（只是少了点好看），
    #: 多声明的代价是"每轮留下一条僵尸气泡"（用户看得见，且没有任何东西会报错）。
    #:
    #: 仓库里恒返回 ``False`` 的是 ``email``（SMTP 改不了已发邮件）、``ntfy``
    #: （无编辑端点）、``a2a``（规范无此操作）、``homeassistant``（无此概念）、
    #: ``irc`` / ``twitch``（IRC 无改消息原语）、``qqbot``（群/C2C 消息只读，
    #: 那个 ``PATCH`` 只能改 keyboard）。它们一律用基类默认值。
    supports_message_edit: bool = False
    #: 命令前缀（Telegram/Slack 用 ``/``，部分平台习惯 ``!``）。
    typed_command_prefix: str = "/"
    #: 配齐才算"该平台可用"的 token 键（``--status`` / ``--setup --json`` 消费）。
    #: 必须把**入站**必需的键也列进来：Slack 缺 ``app_token`` 会静默降级为"只发出
    #: 站"，若这里只列 ``bot_token``，状态视图就会把"入站根本没通"报成已配置。
    required_tokens: tuple[str, ...] = ("bot_token",)
    #: 只做出站所需的凭据键（``--status`` 的 ``outbound_ready`` 消费）。
    #: 默认与多数平台一致；**没有 bot_token 概念的平台必须覆盖**
    #: （Matrix 用 homeserver/access_token、IRC 用 host/nick、Mattermost 用 site_url/token），
    #: 否则状态视图会把它们一律报成"发不出去"。
    outbound_tokens: tuple[str, ...] = ("bot_token",)
    #: **本平台的「配好了没有」是否由它自己回答** —— 默认 False。
    #:
    #: ``required_tokens`` 回答的是"**你必须声明**你的配置面"（`test_cli` 守这条，
    #: 它抓到过 Matrix 忘了声明 ``required_tokens`` 导致配得完全正确的用户被判成
    #: 没配的真bug）；而 preflight 与 ``--status`` 回答的是"**用户手里这份配置
    #: 此刻够不够跑**"。这两件事此前被混为一谈，于是 a2a 被迫把 ``bind_port``
    #: 填进 ``required_tokens``。
    #:
    #: 置 ``True`` 表示"**我是这一类**：我没有凭据可填，'够不够跑'只能由我自己
    #: 回答"**。置 ``True`` 的平台，其 preflight / 两个状态视图**一律**走
    #: :meth:`config_runnable`，**不再**落回 ``required_tokens`` 那条通用规则 ——
    #: 否则我的判定只是个"提前放行"，填错的值仍会被通用规则（"键非空"）重新
    #: 判成已配置，等于把刚修好的洞从旁边再开一个。
    #:
    #: ⚠️ **必须与覆写 :meth:`config_runnable` 一起改。** 只置 ``True`` 而不覆写
    #: ⇒ 默认实现回答"否" ⇒ 该平台永远配不好（缺什么键会被如实报出来，属**失败
    #: 关闭**，但显然不是意图）；反过来只覆写 :meth:`config_runnable` 而不置这个
    #: ⇒ 判定一次都不会被问到（原因见该方法的 docstring）。
    #:
    #: 它**不豁免任何配置声明义务**：``required_tokens`` 仍须非空（守那条不变量的
    #: 测试照样通过）。
    #:
    #: ⛔ **必须保持 ``bool``，绝不能改成方法。** 消费方在**类**上读它
    #: （``getattr(cls, "config_optional", False)``），而 ``getattr`` 取到的是
    #: **未绑定函数对象** —— 永远真值 ⇒ **13 个平台会同时**变成"可省略配置"。
    #: 同一个键还是 :meth:`capabilities` 的输出项（``--setup --json`` 直接
    #: ``json.dumps`` 整个结果）⇒ 变成方法对象时序列化当场炸。
    #: 要"按配置作答"就用 :meth:`config_runnable`，两者的分工写在那里。
    config_optional: bool = False

    @classmethod
    def config_runnable(cls, entry: Optional[dict]) -> bool:
        """给定**这一份**配置条目，本平台**此刻**能不能真的跑起来。默认 ``False``。

        与 :attr:`config_optional` 的分工 —— **两个不同的问题，缺一不可**：

        * :attr:`config_optional` —— **分类**：「我是没有凭据可填的那一类平台」。
          静态、与具体配置无关，且作为 JSON 值出现在 :meth:`capabilities` 里。
        * 本方法 —— **判定**：「用户手里这份 ``entry`` 够不够跑」。它读配置，
          所以同一类平台在不同配置下答案**可以不同**：a2a 填了 ``bind_port``
          与没填，答案就是不一样。

        ⚠️ **默认 ``False``，而这个默认是承重的**：其余十二个平台的「够不够跑」
        走的是 ``required_tokens`` 那条通用规则（逐键看值非空），默认 ``False``
        让它们**一个字都不用改**地继续走那条路。默认成 ``True`` 等于一次拆掉
        全仓库所有平台的配置门槛。

        ⛔ **它只在 :attr:`config_optional` 为 ``True`` 时被问到**（判定入口在
        :mod:`opencode_bridge.__main__`：``_readiness_verdict``）—— 那个属性就是
        "这一类平台的答案由自己给"的开关。⇒ **覆写本方法就必须同时置它**，
        否则这段代码一次都不会被执行；而只置它不覆写本方法，就会拿到这里的默认
        "否"（失败关闭：宁可说"还差 bind_port"，也不放行一份起不来的配置）。

        ⛔ **消费方必须 :meth:`调用 <config_runnable>` 它**，绝不能写
        ``bool(getattr(cls, "config_runnable", False))`` —— 那取到的是类上绑定
        的函数对象（永远真值）⇒ 每个平台都会被判成"能跑"。

        ⛔ **不许要求构造适配器实例**：本方法由 preflight 与状态视图在
        **endpoint discovery 之前**、且**不建任何适配器**的那条路上被问
        （``run_check`` 的契约是「no sessions, **no adapters**」）。

        :param entry: 用户配置里 ``adapters.<本平台>`` 那棵**原始条目**
            （不是投影后的 :func:`~opencode_bridge.config.adapter_scoped_config`
            结果 —— 三条判定路径拿到的都是原始条目，本方法也就只读原始条目）。
        :return: ``True`` = 不用再填任何东西就能起来。
        """
        return False

    #: 迁移**前**本平台用的 ``conversation_id`` 前缀，**仅当它有歧义**（多家共用）
    #: 时才需要声明；``None`` 表示"没有歧义旧前缀"（那种由
    #: :meth:`~opencode_bridge.state.StateStore` 的键迁移自动处理）。
    #:
    #: ⚠️ 这不是装饰性字段：它是 :mod:`opencode_bridge.conversation_keys` 在**读取时**
    #: 回退旧键的前提 —— 没有它就没人知道本平台历史上用的是哪个前缀。
    #: 声明方**不要**拿它去仲裁（"这个 ``channel:`` 键归谁"由 :meth:`owns_local_id`
    #: 回答），更不要据此改盘上的键。
    legacy_conversation_prefix: Optional[str] = None

    #: :attr:`max_message_length` 的**语义**：``True`` = "一条出站消息装得下这么多
    #: **字符**，超了会被拆成多条"（默认，绝大多数平台如此）；``False`` = **这个数
    #: 根本不是消息容量**，拿它当"消息能装多少"说出来就是假话。
    #:
    #: ⚠️ 必须显式声明，因为仓库里就有两个反例，而它们的数**长得像**容量：
    #:
    #: * ``email`` —— :data:`~opencode_bridge.adapters.email.MESSAGE_LIMIT` 是
    #:   RFC 5322 的**单行**上限（998），出站**硬折行**；RFC 5322 对正文总长
    #:   没有任何上限，拆信还会毁掉线程（见该常量的注释）。
    #: * ``a2a`` —— :data:`~opencode_bridge.adapters.a2a.MESSAGE_LIMIT` 是
    #:   **请求体字节**上限，出站把整段文本放进一个 artifact，从不切片。
    #:
    #: 谁把它当"消息容量"报给 agent，谁就在对模型说假话。
    splits_long_messages: bool = True

    #: 本平台 **local id** 的合法形状。描述的是该平台 API 发出来的 id，所以由
    #: **拥有那个平台的适配器**自己声明，而不是集中登记在一张表里
    #: （Slack 的 id 规则变了只该碰 Slack 那一个文件）。
    #:
    #: 用途只有一个：歧义旧前缀（``channel:``）在**读取时**的归属划分，见
    #: :meth:`owns_local_id`。``None`` = 本平台不参与划分（telegram / matrix 等
    #: 本来就没有歧义旧前缀）。
    local_id_pattern: Optional[re.Pattern[str]] = None

    #: 授权配置的解析结果（:func:`opencode_bridge.allowlist.resolve_allowlist` 的产物）。
    #: **类级兜底**：声明在类上而不是 ``__init__`` 里，于是任何绕过
    #: :meth:`_init_access` 的子类也拿得到一个"未解析"的对象，而不是
    #: ``AttributeError`` —— 状态视图问的是"能不能答"，答不出不该让它崩。
    allowlist_resolution: AllowlistResolution = AllowlistResolution()

    #: 「上次启动时」本平台的自检结论，形状 ``{verdict, code, detail}``。
    #: **类级默认 ``None`` = "这次启动没有做过任何探测"** ⛔ —— 而它**不是**"没问题"。
    #:
    #: 与 :attr:`allowlist_resolution` 同为**类级兜底的运行期槽**：声明在基类上，
    #: 于是"这个平台有没有说过自己怎么样"对每个消费者都是同一个答案，不必
    #: ``getattr(..., None)`` 满仓库抄。
    #:
    #: :attr:`~opencode_bridge.health.VERDICT_OK` 表示探测**通过**，
    #: :attr:`~opencode_bridge.health.VERDICT_FAILED` 表示探测**失败且有原因**，
    #: :attr:`~opencode_bridge.health.VERDICT_SKIPPED` 表示**试过了，但没有可探测的凭据**
    #: （⛔ **不是**"压根没探测" —— 那是另一档
    #: :attr:`~opencode_bridge.health.VERDICT_DOES_NOT_PROBE`，两者的排查方向相反）
    #: （三者**必须**分开：把"没验"说成"验过"就是假话）。
    #:
    #: ⚠️ **落盘不由适配器负责**：适配器不知道 bridge 目录在哪。写入
    #: ``<bridge_dir>/platform-health.json`` 是**运行器**的事（见
    #: :mod:`opencode_bridge.health` 与 :meth:`BridgeCore.start` 那一轮收集）。
    #: 本类只负责"把结论规范化 + 记一行日志 + 暂存在实例上"。
    startup_verdict: dict | None = None

    #: 本平台**是否提供配对**（未授权会话发 ``/pair`` 能否拿到码）。默认 **False**，
    #: 逐个平台显式 opt-in —— 没人继承"支持"这个属性。
    #:
    #: 三条判据，**必须同时**成立才允许开：
    #:
    #: (a) principal 是**会话唯一且稳定**的（换台机器还是同一个值）；
    #: (b) 用户**能知道**自己那个值是什么（否则要往 ``allowed_chat_ids`` 里填一个
    #:     自己都不知道的东西，且没有任何途径查出来）；
    #: (c) **平台对发件人做过认证** —— 这一条最承重，它决定"码只对那个会话有效"
    #:     是不是一句真话。
    #:
    #: 显式关掉的四家及理由：
    #:
    #: * ``irc`` / ``twitch`` —— (c) 不成立：IRC **根本没有认证**，任何人都能声称
    #:   任何 nick。私聊的 principal 还是 bot 自己的 nick，配对无从谈起。
    #: * ``nextcloud`` —— principal 是 **OCS token**：用户不知道它，也不该知道。
    #: * ``homeassistant`` —— principal 是 ``entity_id``。
    #: * ``a2a`` —— principal 是对端 peer。
    pairing_supported: bool = False

    @property
    def effective_max_length(self) -> int:
        """**运行期真正生效**的单条出站上限，即 :meth:`send` 实际切分的那个数。

        这是**唯一**该读"我按多少切"的地方。答案 = :attr:`message_limit`（运行期细化
        槽，非 0 时胜出），否则 :attr:`max_message_length`（声明的静态下限）。

        与 :attr:`max_message_length` 分开是有原因的：Mattermost / Nextcloud 启动后
        会拿服务端的 ``MaxPostSize`` / ``max-length`` 把它细化（见
        ``adapters/mattermost.py`` 的 ``_apply_max_post_size``），所以
        **类属性是静态下限，不是真值**。

        收在基类上、且只有这一份解析，是因为这个问题此前有**四个名字**：
        ``message_limit``（类属性）、``effective_max_length``（mattermost /
        nextcloud 各自的 property）、``_effective_limit``（homeassistant /
        qqbot 各自的私有方法），而**基类自己一个答案都没有** —— 于是每个新消费者都
        得重新猜一遍该读哪个，每个适配器还得各自抄一份等价的解析。现在：槽声明在
        基类、解析在基类、读取只有本属性一处，子类不再需要覆写本属性。
        """
        declared = int(self.max_message_length)
        raw = self.message_limit
        if raw is None or isinstance(raw, bool):
            # ``bool`` 不当长度用（``True`` 不是"1 字符"）：按"没细化"处理。
            limit = 0
        else:
            try:
                limit = int(raw)
            except (TypeError, ValueError):
                limit = 0
        return limit if limit > 0 else declared

    def owns_local_id(self, local_id: str) -> bool:
        """``local_id`` 是不是落在**本平台**的 id 命名空间里。

        这是歧义旧前缀（``channel:``）归属判定的**唯一**判据。三家文法必须
        **两两不相交**（见 :mod:`opencode_bridge.conversation_keys`），正因为不相交，
        "这个 local id 属不属于我"才是**可以确定**的判断，而不是猜 ——
        「宁可不续，也不把 A 平台的会话接到 B 平台上」才真正兑现得出来。

        ⚠️ 只在**本平台自己**的文法上判断，绝不遍历别的平台：一旦拿别人的文法
        来仲裁（"谁认得多就算谁的"），就退化成了猜，而猜错的后果与猜错平台完全一样。

        声明了 :attr:`local_id_pattern` 的子类自动继承这个判定，无需各自实现。
        """
        pattern = self.local_id_pattern
        if pattern is None:
            return False
        return pattern.fullmatch(str(local_id or "")) is not None

    def __init__(self, config: dict, hooks: Hooks) -> None:
        self.config: dict = dict(config or {})
        self.hooks = hooks
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.allowed_chat_ids: set[str] = set()
        self._last_send_error: tuple[SendError, str, float | None] | None = None
        self._init_access()

    # --- capabilities ----------------------------------------------------
    def capabilities(self) -> Dict[str, object]:
        """Machine-readable capability snapshot (``--status`` / 状态视图消费）。"""
        return {
            "name": self.name,
            "label": self.label or self.name,
            "max_message_length": self.max_message_length,
            "supports_inbound": self.supports_inbound,
            "supports_inline_buttons": self.supports_inline_buttons,
            "supports_media": self.supports_media,
            "typed_command_prefix": self.typed_command_prefix,
            "allowed_chat_ids_count": len(self.allowed_chat_ids),
            "config_optional": self.config_optional,
            "running": self.running,
        }

    # --- 授权闸门（T1.2：统一到基类，所有平台同一套判定）-------------------
    def _has_any_credential(self) -> bool:
        """配置里**有没有填上**本平台的任一凭据键。

        用它给"空=全开"那条警告把门：没填凭据的适配器收不到任何消息，
        对它喊"谁都能驱动"是假话。判据取 ``required_tokens`` ∪ ``outbound_tokens``
        的并集 —— 只看前者会漏掉"出站凭据齐了但入站键没填"这种半配置状态。
        """
        keys = set(getattr(self, "required_tokens", ()) or ())
        keys |= set(getattr(self, "outbound_tokens", ()) or ())
        return any(str(self.config.get(key) or "").strip() for key in keys)

    def _init_access(self) -> None:
        """从配置读 ``allowed_chat_ids`` 到统一形态（三种键名都认）。

        ⚠️ **空 = 全开**，而 ``config.example.json`` / ``plugin/index.ts`` /
        ``install.*`` 都把 ``[]`` 原样抄进用户配置 —— 所以每个自助安装出来的桥接
        **开局就是全开的**。把默认翻过来是产品决策，不在这里；但两件事在这里做：

        1. **把现状说出来**：配好凭据却空=全开 ⇒ ``logger.warning``；
        2. **把写错的配置说出来**：多个授权键同时出现且解析结果不同 ⇒ 报出
           **实际生效的是哪个键、几项**。

        判定逻辑本身在 :func:`opencode_bridge.allowlist.resolve_allowlist`，
        ``--setup --json`` / ``--status`` 读的是**同一个**函数 —— 两处各解析一次
        就会出现"状态说有限白名单、闸门说全开"，那比没有状态视图更坏。
        """
        resolution = resolve_allowlist(self.config)
        self.allowlist_resolution = resolution
        self.allowed_chat_ids = set(resolution.entries)
        label = self.name or type(self).__name__
        warn_if_conflicting_keys(label, resolution)
        warn_if_no_allowlist(
            label,
            resolution,
            has_credentials=self._has_any_credential(),
            config_version=self.config.get(CONFIG_VERSION_KEY, 0),
        )
        warn_if_pairing_unavailable(
            label,
            self.config.get(PAIRING_SECRET_KEY),
            has_credentials=self._has_any_credential(),
        )

    def _gate_rejects(self, principal: object) -> bool:
        """闸门的**判定式本身**（:meth:`admits` 取它的反）。

        ⚠️ **单独一个私有方法，而不是让 :meth:`admits` 内联**：配对分支需要
        "这个 principal 是不是没被授权"这个答案，而如果它在
        :meth:`answer_pairing_request` 里**再调一次** :meth:`admits`，一条入站
        消息就会触发**两次**闸门调用 —— 而
        ``tests/test_slack.py`` / ``tests/test_discord_gateway.py`` 正是靠**数
        闸门调用次数**来守"闸门在产生 Inbound 之前且只跑一次"这个顺序不变量的。
        次数悄悄变成 2 会让那条不变量看起来破了，而实际上什么都没坏。

        所以两个方法共用这一份判定式，而**对外的闸门入口仍然只有** :meth:`admits`。
        """
        resolution = self.allowlist_resolution
        if resolution.gate_admits_everyone(self.config.get(CONFIG_VERSION_KEY, 0)):
            return False
        return str(principal).strip() not in self.allowed_chat_ids

    def admits(self, principal: object) -> bool:
        """入站闸门：**任何**入站消息（文本 / 命令 / 回调）都必须先过这里。

        非空清单 = 只放行列表内的 chat（**两个版本都一样，这条没变**）。

        空清单的含义**按 ``config_version`` 分两套**，判定只有
        :meth:`~opencode_bridge.allowlist.AllowlistResolution.gate_admits_everyone`
        一处（它内部走 :func:`opencode_bridge.pairing.empty_allowlist_is_open`）：

        * **没有** ``config_version``（或 < 2）⇒ **空 = 全放行**。旧文件保持原语义，
          免得配好凭据却还没配白名单的用户突然被关在门外。
        * ``config_version >= 2`` ⇒ **空 = 谁都不放行**。

        ⚠️ 调用顺序要求（对照 dsh 的反面教训）：授权判定必须在**命令解析与
        审批应答之前**，否则未授权者能用 ``/approve`` 这类命令字绕过闸门。
        入站适配器应在本方法返回 False 时**直接丢弃**，不要把消息交给上层。

        ⚠️ **本方法保持为纯函数谓词**：它**不发消息**。`--status` 那条路径会构造
        适配器（``__main__._channel_config_rows``），一个会发信的谓词在那里是纯粹的
        副作用。配对回复是**另一个**方法（:meth:`answer_pairing_request`）的事。
        """
        return not self._gate_rejects(principal)

    def answer_pairing_request(
        self, principal: object, conversation_id: str, text: object
    ) -> bool:
        """未授权且这条正文在请求配对 ⇒ 回一条码，返回 ``True``（**仍丢弃该消息**）。

        ⚠️ 返回 ``True`` 的含义是"**已经回过了，调用方照旧别把消息放进来**"：
        ``/pair`` 是一条命令，而**命令解析只发生在**
        :meth:`opencode_bridge.inbound_gateway.InboundGateway.on_inbound` ——
        未授权的消息活不到那里，所以这里**只**回信，**绝不**顺手执行命令。

        调用形态是每个平台一行机械替换::

            if not self.admits(principal) and not self.answer_pairing_request(
                    principal, conversation_id, text):
                return

        ⚠️ 那个 ``not self.admits(...)`` **不是**可以省的短路：调用点靠它把
        "已授权"这件事挡在外面，否则已授权的会话发 ``/pair`` 也会收到一条码。

        为什么不复用 ``/setup`` 作触发词：那条文案冻结在
        :mod:`opencode_bridge.commands` 里且被测试钉住，让同一个词在授权 / 未授权
        两种状态下表示两件完全不同的事，等于把一份冻结输出变成状态相关的。
        """
        if not self._gate_rejects(principal):
            # 已授权 —— 走闸门那条路，不该再回一条码。
            return False
        if not self.pairing_supported:
            # 失败关闭：本平台三条判据有一条不成立（见 :attr:`pairing_supported`）。
            # 沉默是对的 —— 回一句"这里不能配对"等于告诉未授权者去别处找办法。
            return False
        if not reads_pairing_trigger(text):
            return False
        secret = self.config.get(PAIRING_SECRET_KEY)
        code = derive_pairing_code(secret, self.name, conversation_id)
        if not code:
            # secret 空 = 不提供配对（⛔ 绝不用空串派生，见 opencode_bridge.pairing）。
            return False
        label = self.label or self.name or type(self).__name__
        self._deliver_pairing_reply(
            conversation_id, pairing_reply_text(label, conversation_id, code)
        )
        return True

    def _deliver_pairing_reply(self, conversation_id: str, text: str) -> None:
        """把配对回信投出去。失败只记日志 —— 它是**尽力而为**的旁路，不是主流程。

        抽出来是为了让各平台能覆盖投递方式（有的要复用已有的 :meth:`send`，
        有的 inbound 路径本身就有自己的发送助手），而 :meth:`answer_pairing_request`
        那一段判定与文案保持**只有一份**。
        """
        try:
            self.send(Outbound(conversation_id=conversation_id, text=text))
        except Exception as exc:  # noqa: BLE001 - 配对是旁路，炸了不许打断轮询
            logger.warning("%s: 配对回信投递失败: %s", self.name or "adapter", exc)

    # --- lifecycle -----------------------------------------------------
    @abc.abstractmethod
    def start(self) -> None:
        """Start the adapter (non-blocking). Must not raise to the caller."""

    def stop(self) -> None:
        """Request the polling thread to stop and join it (timeout 5s)."""
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            if thread is not threading.current_thread():
                thread.join(timeout=5.0)
        self._thread = None

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    # --- 启动探测上报（契约在本类，落盘在 opencode_bridge.health）----------
    def report_startup_probe(
        self,
        verdict: str,
        *,
        code: object = None,
        detail: str = "",
    ) -> dict:
        """上报「上次启动时」的自检结论：记一行日志 + 暂存在 :attr:`startup_verdict`。

        **为什么是基类上的一个方法，而不是每个适配器各写各的**：结论要一直走到
        ``--status`` / ``--setup --json``（见 :mod:`opencode_bridge.health`），
        而那条通路**只有一份** —— 各适配器自己拼 dict 的话，键名与取值域必然分叉，
        而"字段名分叉"正是本任务最初那个缺陷的形态：信息已经产生出来了，却到不
        了用户那里。

        ⚠️ **本方法绝不落盘**：适配器不知道 ``bridge_dir`` 在哪。写入
        ``<bridge_dir>/platform-health.json`` 由**运行器**在**全部**适配器都试过
        之后统一做一次（:func:`opencode_bridge.health.record_startup_probes`）。

        :param verdict: :mod:`opencode_bridge.health` 的取值。**适配器能上报的是三档**
            （``ok`` / ``failed`` / ``skipped``）；第四档 ``not_started`` 答的是
            「**整个桥**没起来」，只有运行器会写它（见
            :func:`opencode_bridge.health.bridge_refusal_probes`）
            —— ⛔ **适配器不要上报它**：你在平台里，管不到别的平台，也管不到桥起没起来。
            **认不出来的值按 ``failed`` 记** ——
            上报方说了句读不懂的话时，绝不能默认成"好"。
        :param code: 平台自己的错误码（Telegram 的 ``error_code``）。
            ⛔ 没有就传 ``None``（该键会**被省略**），别用 ``0`` 顶替 ——
            ``0`` 在本仓库里有确切含义（"没拿到 HTTP 状态码"，见
            ``TelegramAdapter._post`` 与 :func:`classify_http`），两件事不能共用一个值。
        :param detail: 一行说明；落盘前会先过 :mod:`opencode_bridge.redaction`
            的 ``scrub``（凭据片段绝不许进盘）。
        :return: 规范化后的结论（即 :attr:`startup_verdict`）。
        """
        entry = normalize_verdict(verdict, code=code, detail=detail)
        self.startup_verdict = entry
        label = self.name or type(self).__name__
        summary = entry["verdict"] + (
            "：" + entry["detail"] if entry.get("detail") else ""
        )
        if entry["verdict"] == VERDICT_FAILED:
            logger.warning("%s: 启动探测 %s", label, summary)
        else:
            logger.info("%s: 启动探测 %s", label, summary)
        return entry

    # --- messaging -----------------------------------------------------
    @abc.abstractmethod
    def send(self, out: Outbound) -> MsgHandle | None:
        """Send one message; return a handle. Failure -> log, return None."""

    @abc.abstractmethod
    def edit(self, handle: MsgHandle, out: Outbound) -> bool:
        """Edit a previously sent message. Failure -> log, return False.

        ## 正文超上限时：**五个适配器抛、一个不抛**，而这个差别是对的

        * **平台会拒收这一类** —— 改写是真的把另一条已发出去的消息重写一遍，而平台
          有自己的上限：``discord`` 回 400 ``50035``、``telegram`` 回 400
          "message is too long"、``slack`` 回 ``msg_too_long``、``mattermost`` 回
          400 ``model.post.is_valid.message_length.app_error``（``api4/post.go`` 的
          ``rejectOversizedMessage``，``updatePost`` 与 ``patchPost`` 都调）、
          ``nextcloud`` 回 413。**这五个在本地就抛** ``ValueError``、**一个请求都
          不发**，把"本地判定的超限"与"网络失败 / 权限不足"分开。
        * **平台压根没有"改写"这个概念** —— ``matrix``：Matrix 无标准编辑 API，
          它的 :meth:`~opencode_bridge.adapters.matrix.MatrixAdapter.edit` **自己
          就是一条 ``send``（``m.replace`` 回落写法），而它的上限是**我们自选**的
          保守值（按事件体 64KB 上限反推）。按保守值抛异常等于**拒绝投递一条平台
          乐意收下的消息**，所以它退化成普通 ``send``。

        ⚠️ **两种表达都被调用方接住了**：``outbound.py`` 的两条改写路都先接
        ``ValueError`` 再接 ``Exception``，``commands.py`` 那一处接 ``Exception``。
        ``tests/test_edit_length_guard.py`` 用源码结构断言把这一点钉住 ——
        **一旦有人新增一个不接的调用点，那条断言会红**，因为"抛出去会不会中断这一轮"
        是这条契约能不能成立的前提。

        ⚠️ **别把"退化成 send"推广到那五个**：占位消息是**另外发出去**的那条消息，
        它上面已经显示着一截正文；把整段再发一遍，读者就把那一截读了两遍（实测
        4000 字的答复被读成 5500 字）。补发读者没读到的那几段是
        :meth:`~opencode_bridge.outbound.OutboundSender.finalize` 的活，它手里有
        ``shown_progress_text``。
        """

    def answer(self, query_id: str, text: str = "") -> None:
        """Acknowledge an inline-keyboard callback query (optional)."""
        return None

    # --- 出站结果可观测（T1.3）-------------------------------------------
    def _note_send_failure(
        self,
        kind: SendError,
        detail: str = "",
        *,
        retry_after: float | None = None,
    ) -> None:
        """适配器在检测到失败时调用，记录**结构化**原因。``send()`` 的既有签名与
        行为保持不变。

        ⚠️ **记下来还不够**：本方法此前算出的分类**零个生产调用方**（``send_result``
        与 :attr:`last_send_error` 都没人调），于是失败原因算完就丢、用户收不到回信
        也查不到原因。现在消费者是 :meth:`~opencode_bridge.outbound.OutboundSender`
        在 :meth:`~opencode_bridge.outbound.OutboundSender.send_text` 里经
        :meth:`send_observed` 把它读出来，落到 ``outbound-failures.json`` 供
        ``--status`` 读（见 :mod:`opencode_bridge.health`）。**新增适配器不必改
        任何东西**即可自动获得这条通道。"""
        self._last_send_error: tuple[SendError, str, float | None] = (
            kind,
            str(detail)[:400],
            retry_after,
        )

    def _clear_send_failure(self) -> None:
        self._last_send_error = None

    @property
    def last_send_error(self) -> SendError | None:
        """最近一次发送失败的分类（成功过则为 ``None``）。

        ⚠️ **只有分类，没有原因** —— 而"是什么失败"（SMTP 的 ``535``、Telegram 的
        ``Forbidden: bot was blocked by the user``）才是用户看得懂的那一半。
        ⚠️ **而且它是"上一次"，不是"刚才那一次"**：:meth:`_note_send_failure` 只写不清
        （它由各适配器在 ``send()`` **内部**调用，那时"这次"与"上次"分不开）。
        ⇒ **想要"刚才那一次"的完整答案，用 :meth:`send_observed`**，别在这里加属性
        —— 这正是本任务之前的状态：一堆只写、没人读的槽。
        """
        return self._last_send_error[0] if self._last_send_error else None

    def send_observed(
        self, out: Outbound,
    ) -> tuple[SendResult, Optional[BaseException]]:
        """发一条，并交出「**这一条**发得怎么样」—— ``(结构化结果, 抛出的异常)``。

        :meth:`send_result` 的加强版：结果一样，**外加把那个异常交还调用方** ——
        因为上层要留**栈**（``logger.exception``），而只拿到一句
        ``"send() raised: …"`` 的调用方永远查不出是哪一层炸的。

        ⚠️ **它先清再读，所以它是「发一条 + 问结果」的**唯一入口**：清在前、读在后，
        调用方就**不可能**忘记清 ⇒「失败 → 成功」不会被显示成"仍在失败"。

        ⚠️ **绝不向上抛**：适配器抛了也不让上层崩，异常从第二个返回值出去，由调用方
        决定要不要留栈。

        子类要给出更精确的 ``partial`` / ``retry_after`` 就覆写本方法；只想改结果
        形状（不要那个异常）可以覆写 :meth:`send_result`。
        """
        self._clear_send_failure()
        try:
            handle = self.send(out)
        except Exception as exc:  # 适配器不应抛出；真抛了也不让上层崩
            detail = f"send() raised: {exc}"
            self._note_send_failure(SendError.TRANSIENT, detail)
            return SendResult(
                platform=self.name,
                ok=False,
                error_kind=SendError.TRANSIENT,
                error_detail=detail,
            ), exc
        if handle is not None:
            # 分片发送中"部分成功"：send() 返回了最后一个好句柄，但过程中记过失败。
            # 这种情况必须显式带出 partial，否则调用方会把未送达当成已送达。
            if self._last_send_error:
                kind, detail, retry_after = self._last_send_error
                return SendResult(
                    platform=self.name,
                    ok=True,
                    handle=handle,
                    error_kind=kind,
                    error_detail=detail,
                    retry_after=retry_after,
                    partial=True,
                ), None
            return SendResult(platform=self.name, ok=True, handle=handle), None
        kind, detail, retry_after = self._last_send_error or (
            SendError.UNKNOWN,
            "send() returned None",
            None,
        )
        return SendResult(
            platform=self.name,
            ok=False,
            error_kind=kind,
            error_detail=detail,
            retry_after=retry_after,
        ), None

    def send_result(self, out: Outbound) -> SendResult:
        """结构化出站结果（T1.3 新增，**向后兼容**）。

        默认实现包装既有的 ``send()``：拿到句柄即成功，否则回读适配器在
        ``send()`` 内部记下的 ``_note_send_failure``。子类可覆写以给出更精确的
        ``partial`` / ``retry_after``。

        ⚠️ 本方法**委托**给 :meth:`send_observed`（只丢掉那个异常）—— 判定式必须
        只有一份：两个各自实现的副本迟早会漂，而漂掉的恰恰是"用户到底收没收到
        消息"这个最要紧的判据。
        """
        result, _exception = self.send_observed(out)
        return result


def _ensure_loaded(name: str) -> None:
    """确保 ``name`` 对应的适配器模块已被导入（导入即自我注册）。

    **新增平台不再需要改本文件**：只要新建 ``adapters/<name>.py`` 并加上
    ``@register("<name>")``，``build("<name>")`` 就能找到它。阶段 3 要批量加
    Matrix / Mattermost / IRC / Twitch 等平台，这里硬编码模块名会让"每加一个
    平台改一次核心文件"成为固定摩擦。

    名字不是合法标识符、或没有同名模块时，退回导入既有三家（保持"一次 import
    全部"的老行为），随后由调用方抛出 ``KeyError``。
    """
    if name in _REGISTRY:
        return
    if not name.isidentifier():
        return
    dotted = f"{__package__}.{name}"
    try:
        found = importlib.util.find_spec(dotted) is not None
    except (ImportError, AttributeError, ValueError):
        found = False
    if found:
        # 故意不吞异常：模块存在但导入失败要报真实原因，不能伪装成"未知适配器"。
        importlib.import_module(dotted)
        return
    from . import discord, slack, telegram  # noqa: F401  (side effect)


def adapter_class(name: str) -> Type[Adapter] | None:
    """按名取**已注册的适配器类**（不实例化），没有则 ``None``。

    供 ``--status`` / ``--setup --json`` 查询各平台**声明**的必需 token 与能力。
    这样新增平台只要写好适配器就会被状态视图自动列出，不必改核心文件。
    """
    key = str(name)
    _ensure_loaded(key)
    return _REGISTRY.get(key)


def registered_names() -> tuple[str, ...]:
    """扫描本包内的模块并导入，返回全部已注册适配器名（按字母序）。

    冻结的 ``/setup`` 菜单刻意只列三平台（那是人工维护的引导文案），但
    ``--status`` / ``--setup --json`` 是运行时视图，应该**自动**反映所有可用
    平台，否则新加的平台用户在状态里根本看不到。

    某个模块导入失败只记 warning 并跳过 —— 状态视图不该被一个坏适配器整个拖垮。
    """
    # 注意：base 是**模块**而非包，没有 ``__path__``；要扫的是本包所在目录。
    for mod in pkgutil.iter_modules([os.path.dirname(os.path.abspath(__file__))]):
        if mod.name.startswith("_") or mod.name == "base":
            continue
        try:
            importlib.import_module(f"{__package__}.{mod.name}")
        except Exception as exc:  # noqa: BLE001 - 状态视图要尽量出得来
            logger.warning("adapters: 跳过无法导入的 %s: %s", mod.name, exc)
    return tuple(sorted(_REGISTRY))


def build(name: str, config: dict, hooks: Hooks) -> Adapter:
    """Registry lookup: ``telegram`` / ``slack`` / ``discord`` / ...

    Unknown ``name`` raises :class:`KeyError`. Adapter modules are imported
    lazily on first use so that importing this module alone stays free of
    circular imports.
    """
    key = str(name)
    _ensure_loaded(key)

    if key not in _REGISTRY:
        raise KeyError(f"unknown adapter: {name!r}")
    try:
        cls = _REGISTRY[key]
        return cls(config, hooks)
    except KeyError:
        raise
    except Exception as exc:  # configuration errors -> AdapterError
        raise AdapterError(f"failed to build adapter {name!r}: {exc}") from exc

"""email 适配器（``tasks.md`` B1）：IMAP 轮询入站 + SMTP 出站，**纯标准库**。

``imaplib`` / ``smtplib`` / ``email`` 全部内置，所以这个平台在"零妥协项"里的
成本只有凭据：需要一个**专用邮箱或 app password**（本机主账号的密码不要给它）。
协议本身永不废弃，且**不需要公网回调地址** —— 桥接主动去 IMAP 拉，符合本项目的
立身约束。

三个最容易做错的地方（下面每条都对应一处实现注释）
------------------------------------------------

1. **防回环靠 Subject 标记，不靠 From**
   IMAP 收件箱里必然会出现**桥接自己发出的信**（发到本地址、或 ``To`` 里带上了
   自己）。一旦把它当新消息，agent 就会无限自问自答。所以出站 Subject 固定加
   ``[opencode]`` 前缀，入站见到该前缀就丢。

   ⚠️ **不要用 ``From`` 地址判断**（这是本文件最重要的一条纪律）：邮件账号往往
   有多个地址，用户完全可能用 A 给自己发一封、再用 B 给自己发一封；按 From 丢
   会把用户自己的正常来信一起误伤。用"我们自己在出站时写的标记"才是精确判据。

   ⚠️ 但**光看 Subject 前缀会误伤用户的追问**：用户点"回复"后主题变成
   ``Re: [opencode] Re: 问题``。所以判据是三条一起（见 :meth:`EmailAdapter._is_own_echo`）：
   a) Message-ID 命中"我们发过的"→ 精确命中自己的回声；
   b) 剥掉 ``Re:``/``Fw:`` 链之后主题带标记，**且**这封信不是对它的回复
      （``In-Reply-To`` / ``References`` 没指向我们发过的 Message-ID）→ 自己的信
      被转发/自动回复回来的回声；
   c) 否则放行。第三条 (``In-Reply-To``/``References``) 是"用户追问能进来"的保证。

2. **游标用 UID，且先推进再处理**
   用 ``UID SEARCH`` / ``UID FETCH``，**不要** ``SEARCH UNSEEN`` + ``FETCH``：
   - flag 会和人工读信冲突 —— 用户在手机上点开一封，桥接就再也看不见它；
   - ``FETCH`` 的 **seq number 在 mailbox 变动后会漂移**（新邮件到达会让序号整体
     后移），只有 UID 是**单调且不漂移**的。
   - 取正文用 ``BODY.PEEK[]`` 而不是 ``BODY[]``：后者**隐式设置 ``\\Seen``**，会
     让用户的手机端突然显示"未读消失"。同时用 ``SELECT ... readonly``（即 EXAMINE）
     打开邮箱，从协议层面就不允许改 flag。**桥接对用户邮箱只读。**

   游标**先推进再处理**（与 telegram 的 offset、matrix 的 ``next_batch`` 一致）：
   一封"毒邮件"（比如触发某个 core 异常）如果处理抛异常且游标没推进，下一轮会
   再取一次、再抛一次 —— 那是**死循环**。代价是崩溃时最多丢当前批次里还没处理
   完的那几封（at-most-once），这与另外两个平台一致，取舍见 ``_pull_batch``。

   启动同样**不重放历史**：首次连上时先 ``UID SEARCH ALL`` 取当前最大 UID 直接
   当游标（一封正文都不取），语义等价于 ntfy 的 ``since=<当前时间戳>``。

3. **不用 IMAP IDLE**
   IDLE 是长连接推送，``stop()`` 要能干净打断它就得去 ``shutdown()`` 一个正卡在
   ``read()`` 上的 socket —— 纯标准库下不保证那次读被唤醒（ntfy 与 Nextcloud 都
   因此踩过，改成一次性拉取）。所以这里走普通轮询（``PollingTransport`` +
   ``idle_sleep``），代价是响应慢一点，收益是生命周期干净。

编码与行长
----------
出站**必须**用 :class:`email.message.EmailMessage` + ``charset="utf-8"``：手工拼
``Subject`` 头遇到中文会直接产出不合法的 MIME 头。行长用 ``set_content`` 的
``cte`` 控制：``7bit``（纯 ASCII 且每行 ≤ :data:`WRAP_COLUMNS`）保持人可读，其余
一律 ``quoted-printable``（RFC 2045 全兼容，且标准库会自动按 78 折行），于是
**任何路径都不可能产出超过 :data:`MESSAGE_LIMIT` 的行**（RFC 5322 §2.1.1 的
998 硬上限）。

入站用 ``email.policy.default`` 解析后 ``get_body(preferencelist=("plain",))``，
**拿不到纯文本就丢** —— 不把 HTML 当纯文本塞给 agent，也不引第三方解析库。
"""

from __future__ import annotations

import importlib
import logging
import re
import ssl
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Optional

import imaplib
import smtplib

from ..hooks import Hooks, Inbound, MsgHandle, Outbound, SendError
from ..identity import format_id
from ..transport import NOTHING, EventQueue, PollingTransport
from .base import Adapter, register

logger = logging.getLogger("opencode_bridge.adapters.email")

__all__ = [
    "EmailAdapter",
    "MESSAGE_LIMIT",
    "WRAP_COLUMNS",
    "ECHO_PREFIX",
    "DEDUPE_CAPACITY",
]

# ----------------------------------------------------------------------
# 与标准库 email 包同名，所以**不能**写裸 ``import email``（语义上会被读成
# "import 本文件自己"，将来任何人加一行裸导入就会静默拿到错误模块）。
# 统一用 importlib 按绝对路径取，取到的必然是标准库那份。
# ----------------------------------------------------------------------
_email_message = importlib.import_module("email.message")
_email_policy = importlib.import_module("email.policy")
_email_parser = importlib.import_module("email.parser")
_email_utils = importlib.import_module("email.utils")

#: 出站邮件对象（``policy=default`` 才会自动做 RFC 2047 头编码 + 折行）。
EmailMessage = _email_message.EmailMessage
#: 入站/出站共用的策略：``default`` 才能正确解码 UTF-8 头与 quoted-printable 正文。
DEFAULT_POLICY = _email_policy.default
BytesParser = _email_parser.BytesParser
make_msgid = _email_utils.make_msgid
formatdate = _email_utils.formatdate

# ----------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------
#: RFC 5322 §2.1.1 规定的**单行**硬上限：998 个字符（不含 CRLF）。
#:
#: ⚠️ 这是 :attr:`EmailAdapter.max_message_length` 的语义来源，而它**不是**"一封
#: 邮件能装多少字"：RFC 5322 对正文**总长**没有任何上限（那由服务器策略决定，实务
#: 上常见 25 MB 上限）。所以真正要处理的是**行长**，不是正文总长 —— 出站据此
#: **硬折行**（:data:`WRAP_COLUMNS`），而**不是**把一个答案拆成多封邮件（邮件不是
#: 聊天平台，拆信会毁掉线程，用户体验极差）。
MESSAGE_LIMIT = 998

#: RFC 5322 §2.1.1 的 SHOULD 级建议行长（"lines SHOULD be no more than 78
#: characters"）。纯 ASCII 且每行不超过它时才用 ``7bit``，否则交给
#: ``quoted-printable``（标准库按 76~78 折行）。
WRAP_COLUMNS = 78

#: 出站 Subject 的固定标记前缀；入站见到它（剥掉 ``Re:`` 链之后）就丢。
#: 可用配置项 ``echo_prefix`` 覆盖 —— 同一个邮箱若同时跑多个桥接实例要区分。
ECHO_PREFIX = "[opencode]"

#: Message-ID 去重集合的**上限**。超过后按 **FIFO（最旧先淘汰）** 丢。
#:
#: 为什么必须封顶：入站每封都要记一条，只增不减的话一个常年运行的桥接会把内存
#: 吃光。为什么选 FIFO 而不是 LRU：入站到达顺序与 UID 单调递增一致，**最旧的
#: 条目正是最不可能再出现的那些**（游标已经划过去）。2048 条在默认 60s 轮询下够
#: 用很久；再久远的回环风险本来就由 UID 游标兜底（游标先推进，天然幂等）。
DEDUPE_CAPACITY = 2048

#: ``In-Reply-To`` / 线程上下文缓存的条目上限（同样 FIFO 淘汰）。
THREAD_CACHE_CAPACITY = 256

#: 默认轮询间隔。邮件不是聊天：IMAP 每次轮询都是一次完整往返（还可能被服务器按
#: 连接频率限流），60s 足够，且不会像聊天平台那样错过什么要紧的东西。
DEFAULT_POLL_INTERVAL = 60.0
#: 每个 socket 操作的超时。**必须有** —— 没有它，一次卡住的 ``select`` 会让
#: ``stop()`` 白等满 join 超时。
DEFAULT_SOCKET_TIMEOUT = 30.0
#: 出站最小发送间隔：防止"每条回复都等 0 秒"把收件箱刷爆（同一封线程里 agent 可能
#: 连发多条进度/最终消息）。
MIN_SEND_INTERVAL = 1.0

DEFAULT_MAILBOX = "INBOX"
DEFAULT_IMAP_PORT_SSL = 993
DEFAULT_IMAP_PORT_STARTTLS = 143
DEFAULT_SMTP_PORT_SSL = 465
DEFAULT_SMTP_PORT_STARTTLS = 587

#: 出站默认主题（跟在 :data:`ECHO_PREFIX` 后面）。有对话上下文时用 ``Re: <原主题>``。
DEFAULT_REPLY_SUBJECT = "reply"

#: 头部换行前缀。判定"这封信是我们自己的回声"之前要先剥掉它们，否则用户的
#: ``Re: [opencode] ...`` 会被当成回声丢掉，用户就永远无法追问。
#: 中英文 + 全角冒号都收 —— 各家邮件客户端的前缀写法差异很大。
_REPLY_PREFIXES = ("re", "fw", "fwd", "aw", "回复", "转发", "答复", "回覆")
_REPLY_PREFIX_RE = re.compile(
    r"^\s*(?:" + "|".join(_REPLY_PREFIXES) + r")\s*[:：]\s*",
    re.IGNORECASE,
)
#: ``References`` / ``In-Reply-To`` 里 ``<...>`` 的提取。
_MSGID_RE = re.compile(r"<[^<>\s]+>")
#: 剥 ``Re:`` 前缀的轮数上限。纯防御（见 :func:`_strip_reply_prefixes` 的终止性论证）：
#: 循环每轮都会让字符串变短，所以必然终止；这个上限只是防畸形输入做无谓功。
_MAX_STRIP_ROUNDS = 256


def _strip_reply_prefixes(subject: str) -> str:
    """剥掉任意层数的 ``Re:`` / ``Fw:`` / ``回复:`` 前缀（大小写与全角都不敏感）。

    为什么要剥：用户点"回复"后主题是 ``Re: [opencode] Re: 问题``，直接看前缀会
    把它误判成回环，用户就再也发不出追问。剥到干净再比对前缀，回声判定才准确。

    终止性：每轮要么剥掉至少 3 个字符、要么 break，所以循环必然终止（畸形主题
    ``"Re:"*20000`` 也不例外），不存在死循环。
    """
    out = str(subject or "")
    for _ in range(_MAX_STRIP_ROUNDS):
        stripped = _REPLY_PREFIX_RE.sub("", out, count=1)
        if len(stripped) >= len(out):        # 没剥掉任何东西 → 收敛
            break
        out = stripped
    return out.strip()


def _extract_fetch_payload(data: Any) -> Optional[bytes]:
    """从 ``UID FETCH`` 的响应里取出**原始邮件字节**。

    ``imaplib`` 返回的形状是::

        [(b'12 (UID 12 FLAGS () BODY[] {38}', b'<38 字节的原文>'), b')']

    元组里**最后一个** bytes 才是正文；第一个是响应描述行（里面也含 ``BODY[]``
    字样，不能靠子串判断）。部分服务端还会少给一个尾部的 ``b')'``，所以按
    "把响应里所有 bytes 收集起来取最后一个非空的"来写，不依赖固定下标。

    ⚠️ 但**顶层**的 bytes 必须与元组里的分开收集：顶层那个 ``b')'`` 是字面量
    响应的结束标记，它比正文**更靠后**，混在一起"取最后一个"就会把 ``b')'``
    当成邮件正文返回 —— 开发期实测踩到，表现为每封邮件都解析失败、静默丢信。
    """
    from_tuples: list[bytes] = []
    from_top_level: list[bytes] = []
    for item in data or ():
        if isinstance(item, tuple):
            for part in item:
                if isinstance(part, (bytes, bytearray)) and part:
                    from_tuples.append(bytes(part))
        elif isinstance(item, (bytes, bytearray)) and item:
            from_top_level.append(bytes(item))
    if from_tuples:
        return from_tuples[-1]
    return from_top_level[-1] if from_top_level else None


def _extract_message_ids(*headers: Any) -> list[str]:
    """从 ``In-Reply-To`` / ``References`` 头里抽出 ``<...>`` 形式的 Message-ID。"""
    out: list[str] = []
    for header in headers:
        if header is None:
            continue
        out.extend(_MSGID_RE.findall(str(header)))
    return out


def _max_line_octets(raw: bytes) -> int:
    """返回报文里最长一行的**字节数**（RFC 5322 §2.1.1 的 998 按字节算）。"""
    if not raw:
        return 0
    return max(len(line) for line in raw.replace(b"\r\n", b"\n").split(b"\n"))


def _classify_smtp(exc: BaseException) -> tuple[SendError, str]:
    """把 ``smtplib`` 的异常收敛成平台中立的失败分类（T1.3）。

    邮件协议没有 HTTP 状态码，所以**不能**用 :func:`classify_http`；这里按 SMTP
    响应码判别（RFC 5321 §4.2 的 reply code 语义）。
    """
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return SendError.FORBIDDEN, f"SMTP 认证失败: {exc}"
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return SendError.NOT_FOUND, f"收件人被拒: {exc}"
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return SendError.FORBIDDEN, f"发件人被拒: {exc}"
    if isinstance(exc, smtplib.SMTPResponseException):
        code = int(getattr(exc, "smtp_code", 0) or 0)
        detail = str(getattr(exc, "smtp_error", "") or exc)
        if code == 421:
            return SendError.TRANSIENT, f"SMTP 421 服务不可用: {detail}"
        if code in (450, 451, 452):
            return SendError.RATE_LIMITED, f"SMTP {code} 临时性限流: {detail}"
        if code in (552, 554):
            return SendError.TOO_LONG, f"SMTP {code} 报文过大: {detail}"
        if code in (535, 534, 550, 553):
            return SendError.FORBIDDEN, f"SMTP {code} 拒绝: {detail}"
        if 400 <= code < 500:
            return SendError.BAD_FORMAT, f"SMTP {code}: {detail}"
        return SendError.TRANSIENT, f"SMTP {code}: {detail}"
    if isinstance(exc, smtplib.SMTPException):
        # SMTPServerDisconnected / SMTPConnectError 等：网络/服务端问题，可重试。
        return SendError.TRANSIENT, f"SMTP 异常: {exc}"
    if isinstance(exc, (TimeoutError, OSError)):
        return SendError.TRANSIENT, f"SMTP 网络错误: {exc}"
    return SendError.UNKNOWN, f"send 失败: {exc}"


@dataclass(frozen=True)
class _ParsedMail:
    """一封入站邮件里我们要用到的字段（解析失败的字段为 ``None``）。

    单独抽出来是因为防回环要用到 ``In-Reply-To``/``References``：只有它们能区分
    "用户对我们那封的回复"（要放行）和 "我们那封自己绕回来的回声"（要丢）。
    """

    sender: str
    subject: str
    message_id: str
    text: str
    in_reply_to: str = ""
    references: str = ""


class _BoundedIdSet:
    """有界字符串集合：满了就按 **FIFO（最旧先淘汰）** 丢。

    用于 Message-ID 去重（:data:`DEDUPE_CAPACITY`）与线程上下文缓存。选 FIFO 而
    不是 LRU 的理由见 :data:`DEDUPE_CAPACITY` 的注释：入站顺序与 UID 单调递增
    一致，最旧的条目正是最不可能再出现的那些。
    """

    def __init__(self, capacity: int) -> None:
        self.capacity = max(1, int(capacity))
        self._items: "OrderedDict[str, None]" = OrderedDict()

    def __contains__(self, key: object) -> bool:
        return str(key) in self._items

    def __len__(self) -> int:
        return len(self._items)

    def add(self, key: object) -> None:
        text = str(key or "")
        if not text:
            return
        self._items.pop(text, None)
        self._items[text] = None
        while len(self._items) > self.capacity:
            self._items.popitem(last=False)      # FIFO：淘汰最旧


@register("email")
class EmailAdapter(Adapter):
    """IMAP（入站）+ SMTP（出站）邮件桥接。

    **信任模型**：邮件没有平台级身份原语 —— 任何能给这个地址发信的人都会被当成
    用户。所以务必用**专用邮箱 + app password**，并配 ``allowed_chat_ids`` 限定
    发件人地址（走基类 :meth:`~Adapter.admits` 闸门）。``required_tokens`` 如实列出
    凭据与连接参数：只有地址/密码而没有 IMAP/SMTP 主机名是**连不上**的，所以主机名
    同样列为必需（与 IRC 的 ``host``、Mattermost 的 ``site_url`` 同一口径），
    否则 ``--status`` 会把一个根本用不了的配置报成"已配置"。

    **发出去的信不可编辑**：SMTP 没有"改已发邮件"的概念，:meth:`edit` 诚实返回
    ``False``，让 core 退化成发新邮件。
    """

    name = "email"
    label = "Email"
    #: 见 :data:`MESSAGE_LIMIT` 的详细说明 —— 这是**行长**预算，不是正文总量预算。
    max_message_length = MESSAGE_LIMIT
    supports_inbound = True                     # IMAP 轮询
    supports_inline_buttons = False             # 邮件没有 inline 按钮
    supports_media = False                      # v1 只发 text/plain
    #: 邮件**没有**斜杠命令：命令必须在正文里整句写。留空串表示"无类型化命令前缀"，
    #: 上层据此不去解析 ``/xxx``。
    typed_command_prefix = ""

    #: 凭据（地址 + app password）+ 两个主机名。主机名一并列出是为了让 ``--status``
    #: 如实反映"能不能用"—— 缺主机名时 :meth:`start` 会拒绝起线程。
    required_tokens = ("address", "password", "imap_host", "smtp_host")
    #: 只做出站不需要 IMAP 主机名（仓库不变量：``outbound_tokens ⊆ required_tokens``）。
    outbound_tokens = ("address", "password", "smtp_host")

    # -- 类级旋钮（测试可在实例上覆盖）---------------------------------
    message_limit = MESSAGE_LIMIT
    poll_interval = DEFAULT_POLL_INTERVAL
    min_interval = MIN_SEND_INTERVAL
    dedupe_capacity = DEDUPE_CAPACITY
    thread_cache_capacity = THREAD_CACHE_CAPACITY

    def __init__(self, config: dict, hooks: Hooks) -> None:
        super().__init__(config, hooks)
        self.address: str = str(self.config.get("address") or "").strip()
        self.password: str = str(self.config.get("password") or "")

        self.imap_host: str = str(self.config.get("imap_host") or "").strip()
        self.smtp_host: str = str(self.config.get("smtp_host") or "").strip()
        self.mailbox: str = str(self.config.get("mailbox") or DEFAULT_MAILBOX).strip()

        # TLS 方式**必须显式选**，且默认都是加密：绝不"默认关闭 TLS"。
        self.imap_security: str = self._security("imap_security", "ssl")
        self.smtp_security: str = self._security("smtp_security", "ssl")
        self.imap_port: int = self._port("imap_port", self.imap_security, True)
        self.smtp_port: int = self._port("smtp_port", self.smtp_security, False)

        #: 是否校验证书链与主机名。默认 True；关掉必须显式配置（自建/实验环境）。
        self.verify_tls: bool = self._config_verify_tls()
        self.socket_timeout: float = self._timeout()

        #: 回环标记前缀（出站写进 Subject，入站见到就丢）。
        #: **刻意不允许配成空串** —— 空前缀等于关掉防回环，那是本平台最致命的保护。
        #: 与 :meth:`_config_verify_tls` 同一纪律：非法值回落成安全值 + 告警。
        raw_prefix = self.config.get("echo_prefix")
        if raw_prefix is None:
            self.echo_prefix = ECHO_PREFIX
        elif raw_prefix is False or str(raw_prefix).strip() == "":
            logger.warning(
                "email: echo_prefix 不可为空 —— 空前缀等于关掉防回环（agent 会无限"
                "自问自答）；已按默认值 %r 处理", ECHO_PREFIX
            )
            self.echo_prefix = ECHO_PREFIX
        else:
            self.echo_prefix = str(raw_prefix).strip()

        #: 去重集合容量（可配置，测试也用它验证淘汰策略）。
        self.dedupe_capacity = max(1, int(
            self.config.get("dedupe_capacity") or self.dedupe_capacity
        ))

        self._queue = EventQueue()
        #: IMAP UID 游标。``None`` = 尚未 bootstrap（首次连上时只取"当前水位"）。
        self._uid: Optional[int] = None
        self._transport: Optional[PollingTransport] = None
        #: 当前轮询用的 IMAP 连接（``stop()`` 要能打断它）。
        self._live_conn: Any = None
        self._conn_lock = threading.Lock()
        #: 桥接自己发出过的 Message-ID（防回环的精确判据）。
        self._sent = _BoundedIdSet(self.dedupe_capacity)
        #: 已处理过的入站 Message-ID（重放去重）。
        self._seen = _BoundedIdSet(self.dedupe_capacity)
        #: 每个会话最后一次入站的 ``(Message-ID, 原主题)``，用于出站做
        #: ``In-Reply-To`` / ``Re: <原主题>``，让邮件客户端能正确归线程。
        #: 与去重集合一样封顶 + FIFO 淘汰（会话数无界，不能只增不减）。
        self._thread_cache: "OrderedDict[str, tuple[str, str]]" = OrderedDict()
        self._thread_capacity = max(1, int(self.thread_cache_capacity))

        self._throttle_lock = threading.Lock()
        self._last_send: float = 0.0

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    def _security(self, key: str, default: str) -> str:
        """读 TLS 方式（``ssl`` / ``starttls``）。**非法值回落成加密**，不回落明文。"""
        raw = str(self.config.get(key) or "").strip().lower()
        if not raw:
            return default
        if raw in ("ssl", "tls", "implicit"):
            return "ssl"
        if raw == "starttls":
            return "starttls"
        logger.warning(
            "email: %s=%r 非法，按 %r（加密）处理 —— 本适配器不支持明文 SMTP/IMAP",
            key, raw, default,
        )
        return default

    def _port(self, key: str, security: str, is_imap: bool) -> int:
        """端口：显式配置优先，否则按 TLS 方式取该协议的默认端口。"""
        raw = self.config.get(key)
        try:
            if raw not in (None, "") and not isinstance(raw, bool):
                port = int(raw)
                if 0 < port < 65536:
                    return port
                raise ValueError(port)
        except (TypeError, ValueError):
            logger.warning("email: %s=%r 不是合法端口，按协议默认端口处理", key, raw)
        if security == "starttls":
            return DEFAULT_IMAP_PORT_STARTTLS if is_imap else DEFAULT_SMTP_PORT_STARTTLS
        return DEFAULT_IMAP_PORT_SSL if is_imap else DEFAULT_SMTP_PORT_SSL

    def _config_verify_tls(self) -> bool:
        """读 ``verify_tls``（默认 ``True``）；非法值按 ``True`` 处理而不是静默降级。"""
        raw = self.config.get("verify_tls")
        if raw is None or raw == "":
            return True
        if isinstance(raw, bool):
            return raw
        low = str(raw).strip().lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off"):
            return False
        logger.warning(
            "email: verify_tls 配置非法 %r，按 True（校验证书）处理", raw
        )
        return True

    def _timeout(self) -> float:
        raw = self.config.get("socket_timeout")
        try:
            value = float(raw)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
        return DEFAULT_SOCKET_TIMEOUT

    def _tls_context(self) -> Any:
        """造 SSL context。``verify_tls=False`` 时显式降级并**大声告警**。"""
        if self.verify_tls:
            return ssl.create_default_context()
        logger.warning(
            "email: verify_tls=False —— 不校验证书链与主机名，仅限自建/实验环境"
        )
        return ssl._create_unverified_context()

    def _msgid_domain(self) -> str:
        """从发件地址推导 Message-ID 的域；取不到就用 ``localhost``。

        ``make_msgid`` 要求域匹配 ``[A-Za-z0-9.-]+``，否则会抛异常，所以这里必须
        过滤一遍（用户可能写成 ``张三 <bot@例子.中国>`` 这种）。
        """
        _, _, domain = self.address.partition("@")
        cleaned = re.sub(r"[^A-Za-z0-9.-]", "", domain or "")
        return cleaned.strip(".") or "localhost"

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        missing = [
            key for key in ("address", "imap_host")
            if not str(self.config.get(key) or "").strip()
        ]
        if missing:
            logger.warning(
                "email: 缺少必需配置 %s；adapter not started", ", ".join(missing)
            )
            return
        if not self.smtp_host:
            logger.warning(
                "email: 未配置 smtp_host —— 入站可用，但出站会全部失败"
            )
        self._stop_event.clear()
        if self._transport is None:
            self._transport = PollingTransport(
                self._fetch_one,
                idle_sleep=float(self.config.get("poll_interval") or self.poll_interval),
                name=self.name,
            )
        self._transport.start(self._on_raw)

    def stop(self, timeout: float = 5.0) -> None:
        """**先关 IMAP 连接再停传输**（顺序不能反，见 transport/base 的教训）。

        轮询线程可能正卡在 ``select`` / ``SEARCH`` 的 socket 读上；先
        ``shutdown()`` 把那次读打断，``join`` 才不用白等满超时。本方法幂等、
        不抛异常。
        """
        self._stop_event.set()
        with self._conn_lock:
            conn, self._live_conn = self._live_conn, None
        if conn is not None:
            self._shutdown_imap(conn)
        transport = self._transport
        self._transport = None
        if transport is not None:
            transport.stop(timeout=timeout)

    @staticmethod
    def _shutdown_imap(conn: Any) -> None:
        """打断可能阻塞中的 IMAP 读，再关掉。**不抛异常**且幂等。"""
        shutdown = getattr(conn, "shutdown", None)
        if callable(shutdown):
            try:
                shutdown()
            except Exception as exc:  # noqa: BLE001 - 关闭路径不许抛
                logger.debug("email: IMAP shutdown 失败（忽略）: %s", exc)
        try:
            conn.logout()
        except Exception as exc:  # noqa: BLE001
            logger.debug("email: IMAP logout 失败（忽略）: %s", exc)

    # ------------------------------------------------------------------
    # IMAP（可注入：测试只替换 _imap_connect / _imap_close）
    # ------------------------------------------------------------------
    def _imap_connect(self) -> Any:
        """建 IMAP 连接、登录、以**只读**方式选中邮箱。测试替换本方法。

        ``SELECT ... readonly``（即 EXAMINE）让服务端从协议层面就拒绝 flag 变更 ——
        这是"不改用户邮箱状态"的**结构性**保证，不是靠我们自觉不调 ``STORE``。
        """
        ctx = self._tls_context()
        if self.imap_security == "starttls":
            conn = imaplib.IMAP4(
                self.imap_host, self.imap_port, timeout=self.socket_timeout
            )
            conn.starttls(ssl_context=ctx)
        else:
            conn = imaplib.IMAP4_SSL(
                self.imap_host,
                self.imap_port,
                ssl_context=ctx,
                timeout=self.socket_timeout,
            )
        conn.login(self.address, self.password)
        status, _ = conn.select(self.mailbox, readonly=True)
        if status != "OK":
            raise RuntimeError(f"select {self.mailbox} failed: {status}")
        return conn

    def _imap_close(self, conn: Any) -> None:
        """关掉本轮用的连接。**不抛异常**（``stop()`` 与异常路径都会走这里）。"""
        if conn is None:
            return
        self._shutdown_imap(conn)

    def _search_uids(self, conn: Any) -> list[int]:
        """``UID SEARCH`` 取"游标之后"的所有 UID，升序返回。

        **不用** ``SEARCH UNSEEN``：flag 会和人工读信冲突（用户在手机点开一封，
        桥接就再也看不见它），而且 flag 语义在不同服务端之间并不一致。
        """
        if self._uid is None:
            # 首次：只取"当前水位"，一封正文都不取 —— 等价于 ntfy 的
            # since=<当前时间戳>，避免首次启动把整个收件箱历史各触发一次 agent。
            status, data = conn.uid("SEARCH", None, "ALL")
        else:
            # UID 区间搜索。UID 单调且不漂移，所以它是唯一可靠的增量游标。
            status, data = conn.uid("SEARCH", None, "UID", f"{self._uid + 1}:*")
        if status != "OK":
            raise RuntimeError(f"UID SEARCH failed: {status}")
        raw = (data[0] if data else b"") or b""
        uids: list[int] = []
        # ⚠️ 必须按 latin-1 解码再 split，**不能** ``str(raw)`` —— 那是 bytes 的
        # repr（``b'1 2 3'``），split 出来的每个 token 都带 ``b'``/``'``，于是
        # **所有** UID 都会因 int() 失败被丢掉，表现为"永远搜不到新邮件"。这正是
        # 本文件开发期实测踩到的坑。
        for token in raw.decode("latin-1", "replace").split():
            try:
                uids.append(int(token))
            except ValueError:
                logger.debug("email: UID SEARCH 返回非数字 token %r，忽略", token)
        return sorted(uids)

    def _fetch_raw(self, conn: Any, uid: int) -> Optional[bytes]:
        """取一封的**原始字节**。用 ``BODY.PEEK[]``（不是 ``BODY[]``）。

        ``BODY[]`` 会**隐式设置 ``\\Seen``**，那等于替用户把邮件标成已读 —— 手机端
        的未读数会突然少一封。``BODY.PEEK[]`` 明确不碰 flag。
        """
        status, data = conn.uid("FETCH", str(uid), "(BODY.PEEK[] FLAGS)")
        if status != "OK":
            logger.warning("email: UID FETCH %s failed: %s", uid, status)
            return None
        return _extract_fetch_payload(data)

    # ------------------------------------------------------------------
    # 入站轮询（适配 PollingTransport）
    # ------------------------------------------------------------------
    def _fetch_one(self) -> Any:
        """给 :class:`PollingTransport` 的 ``fetch``：返回**一封**原始邮件或 ``NOTHING``。"""
        if not self._queue:
            self._pull_batch()
        if not self._queue:
            return NOTHING
        return self._queue.pop()

    def _pull_batch(self) -> None:
        """连一次 IMAP、取一批原始邮件填进队列。失败时抛异常（交给传输层退避重连）。

        每轮**新建并关闭**一次连接（与 ntfy 的 ``poll=1`` 同一哲学）：``stop()`` 时
        不会残留一个半开的 socket，也不用处理"连接被服务端悄悄断掉"的状态机。
        代价是每轮多一次握手 —— 60s 轮询下可忽略。
        """
        conn: Any = None
        with self._conn_lock:
            self._live_conn = None
        try:
            conn = self._imap_connect()
            with self._conn_lock:
                self._live_conn = conn
            uids = self._search_uids(conn)
            if not uids:
                return
            if self._uid is None:
                # bootstrap：只把游标抬到当前最大值，**不取任何正文**。
                logger.info(
                    "email: 首次连接，从 UID %s 之后开始接收（不重放历史收件箱）",
                    uids[-1],
                )
                self._uid = uids[-1]
                return
            items = []
            fetched_through = self._uid
            for uid in uids:
                raw = self._fetch_raw(conn, uid)
                if raw is None:
                    # 取不到正文就**停在连续前缀的末尾**：游标绝不跳过失败的那封
                    # （否则那封信永远拿不到，且用户毫无察觉）。
                    break
                items.append(raw)
                fetched_through = uid
            # 游标**先推进再处理**：处理阶段抛异常（毒邮件）不该导致下一轮重放，
            # 否则就是死循环。代价是崩溃时最多丢本批还没处理完的那几封
            # （at-most-once）—— 与 telegram offset / matrix next_batch 的取舍一致。
            self._uid = fetched_through
            self._queue.push_many(items)
        finally:
            with self._conn_lock:
                if self._live_conn is conn:
                    self._live_conn = None
            self._imap_close(conn)

    # ------------------------------------------------------------------
    # 防回环
    # ------------------------------------------------------------------
    def _is_own_echo(self, message_id: str, subject: str, *ref_headers: Any) -> bool:
        """判断"这封是不是桥接自己发出去的信又回来了"。

        三条判据一起用（顺序即优先级），理由见模块 docstring：

        1. ``Message-ID`` 命中 :attr:`_sent` —— **精确**命中，我们发出去的那一封
           本身就是它（哪怕主题被网关改写过）。
        2. 剥掉 ``Re:``/``Fw:`` 链之后主题带 :attr:`echo_prefix`，**且**这封信不是
           对我们那封的回复（``In-Reply-To``/``References`` 都没命中 ``_sent``）
           —— 自己的信被自动回复/转发绕回来的回声。
        3. 其余情况**放行**。用户点"回复"得到的 ``Re: [opencode] ...`` 走这条，
           所以用户能正常追问。

        ⚠️ 任何情况下都**不查 From 地址** —— 那样会误伤用户多地址互发的正常来信。
        """
        if message_id and message_id in self._sent:
            return True
        if not self.echo_prefix:
            return False
        if not _strip_reply_prefixes(subject).upper().startswith(
            self.echo_prefix.upper()
        ):
            return False
        return not any(mid in self._sent for mid in _extract_message_ids(*ref_headers))

    # ------------------------------------------------------------------
    # 入站解析与投递
    # ------------------------------------------------------------------
    def _parse(self, raw: bytes) -> Optional[_ParsedMail]:
        """解析一封原始邮件。拿不到纯文本就返回 ``None``（丢弃）。

        **不把 HTML 当纯文本塞给 agent**，也不引第三方解析库：
        ``get_body(preferencelist=("plain",))`` 对 ``multipart/alternative`` 只有
        html 的场景、对 ``text/html`` 单体、对只有附件的 multipart，都会返回
        ``None`` —— 正好就是要丢的那些。

        解码失败（未知 charset、非法字节等）也返回 ``None``：宁可丢一封，也不要把
        ``LookupError`` / ``UnicodeDecodeError`` 冒到网关线程上。
        """
        try:
            msg = BytesParser(policy=DEFAULT_POLICY).parsebytes(raw)
        except Exception as exc:  # noqa: BLE001 - 畸形邮件不该打断整轮
            logger.warning("email: 解析邮件失败（丢弃）: %s", exc)
            return None
        try:
            body = msg.get_body(preferencelist=("plain",))
        except Exception as exc:  # noqa: BLE001
            logger.warning("email: 取纯文本正文失败（丢弃）: %s", exc)
            return None
        if body is None:
            logger.info("email: 邮件没有 text/plain 正文（丢弃，不把 HTML 当正文）")
            return None
        try:
            text = body.get_content()
        except Exception as exc:  # noqa: BLE001 - 未知 charset / 解码失败都丢
            logger.warning("email: 解码正文失败（丢弃）: %s", exc)
            return None
        if not isinstance(text, str):
            logger.info("email: 正文不是文本（丢弃）")
            return None
        sender = self._sender_of(msg)
        if not sender:
            logger.info("email: 邮件没有可用 From 地址（丢弃）")
            return None
        return _ParsedMail(
            sender=sender,
            subject=str(msg.get("Subject", "") or ""),
            message_id=str(msg.get("Message-ID", "") or "").strip(),
            text=text,
            in_reply_to=str(msg.get("In-Reply-To", "") or ""),
            references=str(msg.get("References", "") or ""),
        )

    @staticmethod
    def _sender_of(msg: Any) -> str:
        """取发件人地址（``Display Name <a@b.com>`` → ``a@b.com``）。"""
        try:
            header = msg.get("From")
            if header is None:
                return ""
            addresses = header.addresses
            if addresses:
                return str(addresses[0].addr_spec or "").strip()
            return str(header).strip()
        except Exception as exc:  # noqa: BLE001 - 地址头畸形不该打断
            logger.debug("email: 解析 From 失败: %s", exc)
            return ""

    def _on_raw(self, raw: bytes) -> None:
        """传输层回调：解析 → 防回环 → 授权 → 投递。"""
        if not isinstance(raw, (bytes, bytearray)) or not raw:
            return
        parsed = self._parse(bytes(raw))
        if parsed is None:
            return
        sender = parsed.sender
        subject = parsed.subject
        message_id = parsed.message_id
        text = parsed.text

        # 防回环放在最前面 —— 它是"无限自问自答"的唯一防线。
        try:
            is_echo = self._is_own_echo(
                message_id, subject, parsed.in_reply_to, parsed.references
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("email: 回环判定失败（丢弃该封）: %s", exc)
            return

        # 重放去重：同一个 Message-ID 只处理一次（集合有上限，FIFO 淘汰）。
        if message_id:
            if message_id in self._seen:
                logger.debug("email: Message-ID %s 已处理过，丢弃重放", message_id)
                return
            self._seen.add(message_id)

        if is_echo:
            logger.info("email: 丢弃自己发出的回声（subject=%r）", subject)
            return
        if not text.strip():
            return
        # 授权闸门必须在产生 Inbound **之前**（基类 docstring 的硬要求）。
        if not self.admits(sender):
            logger.info("email: 发件人 %s 不在白名单，丢弃", sender)
            return

        conversation_id = format_id(self.name, sender)
        if message_id:
            self._remember_thread(conversation_id, message_id, subject)
        try:
            # 主题对邮件来说是**问题的一部分**（用户常常把问题写在标题里），所以
            # 有主题时把它作为引用行并入正文交给 agent。
            payload = f"[{subject.strip()}]\n\n{text}" if subject.strip() else text
            self.hooks.on_inbound(
                Inbound(
                    conversation_id=conversation_id,
                    text=payload,
                    kind="text",
                    user_id=sender or None,
                    message_id=message_id or None,
                    platform=self.name,
                    raw={
                        "subject": subject,
                        "from": sender,
                        "message_id": message_id,
                    },
                )
            )
        except Exception as exc:  # noqa: BLE001 - 上层炸了也不能让网关线程退出
            logger.exception("email: on_inbound 失败: %s", exc)

    # ------------------------------------------------------------------
    # 出站
    # ------------------------------------------------------------------
    def _throttle(self) -> None:
        interval = float(getattr(self, "min_interval", MIN_SEND_INTERVAL))
        if interval <= 0:
            return
        with self._throttle_lock:
            last = self._last_send
            wait = last + interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_send = time.monotonic()

    def _cte_for(self, text: str) -> str:
        """选 ``Content-Transfer-Encoding``。

        * 纯 ASCII 且每行 ≤ :data:`WRAP_COLUMNS` → ``7bit``：人可读、零膨胀，
          而且每行本来就短于 RFC 5322 的 998 硬上限。
        * 其余 → ``quoted-printable``：RFC 2045 全兼容（比 ``base64`` 体积小、
          比 ``8bit`` 兼容性好），且**标准库会按 76~78 自动折行**，于是任何路径
          都不可能产出超长行。

        ⚠️ 刻意**不用** ``8bit``：它**不折行**，一行长正文会直接冲出 998 的硬上限。
        """
        if text.isascii() and all(
            len(line) <= WRAP_COLUMNS for line in text.splitlines()
        ):
            return "7bit"
        return "quoted-printable"

    def _remember_thread(self, conversation_id: str, message_id: str,
                         subject: str) -> None:
        """记住会话最后一次入站的上下文（封顶 + FIFO 淘汰）。"""
        self._thread_cache.pop(conversation_id, None)
        self._thread_cache[conversation_id] = (message_id, subject)
        while len(self._thread_cache) > self._thread_capacity:
            self._thread_cache.popitem(last=False)

    def _thread_refs_for(self, conversation_id: str) -> tuple[Optional[str], str]:
        """取该会话上一次入站的 ``(Message-ID, 原主题)``，用于出站归线程。"""
        value = self._thread_cache.get(conversation_id)
        if not value:
            return None, ""
        message_id, subject = value
        return message_id, subject

    def _build_message(self, recipient: str, body: str) -> Any:
        """构造一封 RFC 5322 邮件（:class:`EmailMessage` + ``policy=default``）。

        绝不手工拼 MIME 头 —— ``Subject`` 含中文时手拼会产出非法头。``policy=default``
        的 headerregistry 会自动做 RFC 2047 编码（``=?utf-8?b?...?=``）与折行，
        并且**拒绝**含 CR/LF 的头值（防头注入）。

        主题永远以 :attr:`echo_prefix` 开头：这是入站判"自己回声"的依据，漏了就
        会自问自答。
        """
        message = EmailMessage(policy=DEFAULT_POLICY)
        thread_mid, original = self._thread_refs_for(self._conversation_id(recipient))
        subject = self._outbound_subject(original)
        message["Subject"] = subject
        message["From"] = self.address
        message["To"] = recipient
        message["Date"] = formatdate(localtime=True)
        message["Message-ID"] = make_msgid(domain=self._msgid_domain())
        if thread_mid:
            # 有上游 Message-ID 就挂上，邮件客户端才能把回复归进同一个线程。
            message["In-Reply-To"] = thread_mid
            message["References"] = thread_mid
        message.set_content(
            body, subtype="plain", charset="utf-8", cte=self._cte_for(body)
        )
        return message

    def _outbound_subject(self, original: str) -> str:
        """出站主题：``[opencode] Re: <原主题>``；没有原主题时用默认文案。"""
        base = self.config.get("subject") or DEFAULT_REPLY_SUBJECT
        original = _strip_reply_prefixes(original)
        if original:
            base = f"Re: {original}"
        return f"{self.echo_prefix} {base}".strip()

    def _smtp_send(self, recipient: str, raw: bytes) -> None:
        """真正的 SMTP 会话。测试替换本方法（只暴露这一个出站调用面）。

        TLS 方式**由配置显式决定且默认加密**：``ssl`` = 隐式 TLS（``SMTP_SSL``，
        465），``starttls`` = ``STARTTLS``（587）。本适配器**不提供明文 SMTP**。
        """
        ctx = self._tls_context()
        if self.smtp_security == "starttls":
            client = smtplib.SMTP(
                self.smtp_host, self.smtp_port, timeout=self.socket_timeout
            )
        else:
            client = smtplib.SMTP_SSL(
                self.smtp_host,
                self.smtp_port,
                timeout=self.socket_timeout,
                context=ctx,
            )
        try:
            client.ehlo()
            if self.smtp_security == "starttls":
                client.starttls(context=ctx)
                client.ehlo()
            client.login(self.address, self.password)
            client.sendmail(self.address, [recipient], raw)
        finally:
            try:
                client.quit()
            except Exception as exc:  # noqa: BLE001 - 清理路径不许抛
                logger.debug("email: SMTP quit 失败（忽略）: %s", exc)

    def _conversation_id(self, recipient: str) -> str:
        return format_id(self.name, recipient)

    def send(self, out: Outbound) -> MsgHandle | None:
        """发一封邮件。**不做多封拆分** —— 邮件不是聊天平台，见 :data:`MESSAGE_LIMIT`。"""
        recipient = str(out.conversation_id or "").strip()
        if recipient.startswith(f"{self.name}:"):
            recipient = recipient.split(":", 1)[1]      # 剥掉 email: 前缀
        text = out.text or ""
        if not recipient or not text.strip():
            logger.warning(
                "email: refusing to send (recipient=%r has_text=%r)",
                recipient, bool(text.strip()),
            )
            self._note_send_failure(SendError.BAD_FORMAT, "bad recipient or empty text")
            return None
        if not self.smtp_host:
            self._note_send_failure(SendError.BAD_FORMAT, "smtp_host not configured")
            return None

        try:
            message = self._build_message(recipient, text)
        except Exception as exc:  # noqa: BLE001 - 非法头（如 CR/LF 注入）不该崩
            logger.error("email: 构造邮件失败: %s", exc)
            self._note_send_failure(SendError.BAD_FORMAT, f"build failed: {exc}")
            return None

        raw = message.as_bytes()
        widest = _max_line_octets(raw)
        if widest > MESSAGE_LIMIT:
            # 走到这里说明 _cte_for 漏了一种超长行路径 —— 明确告警，别静默发出去
            # 让对端 MTA 拒收（那会变成一个极难定位的投递失败）。
            logger.error(
                "email: 报文最长行 %d 字节 > RFC 5322 上限 %d，检查 _cte_for",
                widest, MESSAGE_LIMIT,
            )

        self._throttle()
        try:
            self._smtp_send(recipient, raw)
        except Exception as exc:  # noqa: BLE001 - 适配器不向调用方抛
            kind, detail = _classify_smtp(exc)
            self._note_send_failure(kind, detail)
            return None

        message_id = str(message["Message-ID"])
        if message_id:
            # 记下自己发过的 Message-ID：这是入站判回环最精确的依据（即使主题被
            # 网关改写过也能命中）。
            self._sent.add(message_id)
        return MsgHandle(self._conversation_id(recipient), message_id, self.name)

    def edit(self, handle: MsgHandle, out: Outbound) -> bool:
        """SMTP **没有编辑邮件的能力**（邮件发出后无法修改，只能再发一封）。

        返回 ``False`` 让 core.py 退化成"再发一封新邮件"，而不是假装成功 —— 对邮件
        来说这才是诚实的语义（用户会收到两封，其中一封是完整答案）。
        """
        logger.debug("email: edit not supported by SMTP; caller should send a new mail")
        return False

    def answer(self, query_id: str, text: str = "") -> None:
        """邮件没有 callback query 概念 —— no-op。"""
        return None
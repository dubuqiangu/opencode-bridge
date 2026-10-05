"""脱敏引擎（C2）：凭据 / 手机号 / 会话 id / 邮箱不再以明文进日志与 ``state.json``。

为什么是"一个引擎 + 两个卡口"，而不是逐个适配器改
==================================================

本仓库有 **502 处** ``logger.<level>(...)`` 调用点、13 个适配器，而且还会增加。
让每个适配器自己记得脱敏就是**症状级**修法：下一个适配器会忘，本仓库已经吃过
一次"每个平台各写各的"（``chat:`` / ``channel:`` / ``room:`` 前缀）的亏。
所以本模块只做两件事，把**值**收口在两条必经之路上：

1. :class:`RedactingFilter` —— 挂在 **handler** 上（:func:`install_redaction_filter`），
   **外加** :mod:`opencode_bridge.redaction_coverage` 那一层顺序无关的覆盖
   （记录**创建**时就脱敏）。全仓库 34 个 logger 全部是 ``opencode_bridge.*`` 的
   后代（``grep getLogger`` 可核），记录一律 ``propagate`` 到 root，而 root 的 handler
   是 :func:`logging.basicConfig` 建的**唯一** handler —— 全仓库**没有任何**
   ``addHandler``。所以要改的调用点是 0 处。

   为什么挂 handler 而不是挂 logger：这是 stdlib 的过滤语义决定的。
   ``Logger.filter()`` **只对 logger 自己那条记录**生效，祖先 logger 的 filter
   **不会**在 propagate 途中被调用（``Logger.callHandlers`` 只调
   ``handler.handle``）。所以"给 ``opencode_bridge`` 这个 logger 加个 filter 就当
   作覆盖了全部子 logger"是**错的**，而 ``Handler.filter()`` 对**每一条经过它的
   记录**都生效。

   ⚠️ **而 handler 那一层是有顺序依赖的**（实测结论见
   :func:`install_redaction_filter` 的文档字符串）：它只覆盖走位里排在它**之后**的
   handler。排在它**之前**的（典型：挂在命名空间 logger 上的本地 handler）会先拿到
   明文。``redaction_coverage`` 那一层就是为了消掉这个依赖而存在的 —— 它挂在记录
   **创建**那一刻，与 handler 的数量、时机、走位顺序**全都无关**。

2. :func:`redact_state_values` —— 挂在
   :meth:`~opencode_bridge.state.StateStore._write_locked` 的 **JSON 序列化边界**。
   落盘前扫一遍，而不是让每个写入方自己记得。

四类值分别怎么处理，以及为什么
==============================

=============  ==========================  ========================================
类别          形态                        为什么这么选
=============  ==========================  ========================================
凭据          ``[REDACTED:<类别>]``       **不可关联**。见下"凭据为什么不用摘要"。
手机号        ``phone#<6位>-<6位>``       需要**能对齐**（同一个人横跨多行日志）。
会话 id       ``<platform>:conv#<摘要>``  同上，且**平台段保留可读**。
邮箱          ``email#<摘要>``            同手机号。
=============  ==========================  ========================================

凭据为什么不用摘要
------------------

摘要能把"可对齐"也一起给凭据，但那是**错的**：§2.1 里
``(token|secret|password|api_key|app_secret) = '长串字面量'`` 这一条**必然**覆盖
**低熵**口令。一条可复现的摘要 + 一份常见口令字典 = 一个**离线验证器**：
拿到日志的人能确认"这就是那个口令"。凭据是本任务里唯一"猜错代价不可逆"的类别，
所以它**只遮蔽、不摘要**。

那"这是不是同一个 token"怎么查？——**不需要查**。同一行日志里 token 出现两次，
两次都会被标成同一个 ``[REDACTED:telegram-bot-token]``，而**它到底变没变**是配置
问题，不该靠日志回答。标签本身就是"这里有一个 telegram bot token"的全部信息。

手机号 / 邮箱 / 会话 id 为什么用**带密钥**的摘要
-----------------------------------------------

这三种都**低熵**：手机号空间约 10^10，telegram chat id 是 13 位以内整数，discord
雪花号是 17~20 位，mattermost 恰好 26 位。**裸 SHA 摘要对它们等于没有脱敏** ——
枚举一遍就能还原（GPU 上分钟级）。所以用
:func:`hmac.new` 加一个**进程内随机密钥**：

* 没有密钥就**无法**验证候选值 ⇒ 日志本身不再是验证器；
* 不同进程、不同用户的日志**不能**互相串联（这既是隐私，也避免了两份日志被
  关联成一人的行为画像）。

**代价（明说）**：跨进程不可对齐 —— 上一次运行的日志和这一次的
``conv#...`` 不同，所以"把昨天 3 点的日志和今天的对上"这件事做不到了。
这是**有意接受**的：把密钥落盘（像 Django 的 ``SECRET_KEY``）能换回跨进程对齐，
但那会在盘上多一个必须保护的秘密，是**另一个决定**，不该顺手塞进 C2。

``state.json``：**脱敏值，不脱敏键**
===================================

``state.json`` 是 ``{"sessions": {"<conversation_id>": "<session_id>"}}`` ——
**conversation id 就是索引**。把它遮成掩码文本会让每一次查找落空、每一条会话
**静默**变成孤儿，那正是 G5 花两个 commit 修回来的失败模式。所以键**一律不动**。

而值只脱敏**凭据**一类，**手机号 / 邮箱 / 会话 id 不套到值上**。原因不是保守，
是它们会**破坏行为**：``meta`` 的值是**要读回来用的**——
``directory``（``/cd`` 写的目录，:mod:`opencode_bridge.commands`）与
``stream_cursor``（整数游标，:mod:`opencode_bridge.stream_cursor`）。把手机号/邮箱
规则套到 ``directory`` 上，一个叫 ``/srv/13800138000`` 的目录会让 ``cd`` 静默
去到不存在的地方 —— 这就是 AGENTS.md §8 说的"用户**静默**遇到错的东西"，比泄漏
更难查。凭据不同：凭据**永远不是**合法的读回数据，脱敏它不可能破坏行为。

因此本仓库 ``state.json`` 的真实形状是：敏感信息**几乎全在键上**
（``email:<地址>``、``telegram:<chat_id>``），值上只有 ``session_id`` / 目录 / 游标。
键要不要一起脱敏（确定性键哈希）见本模块末尾「键哈希」一节 —— **本次没有实现**。

刻意不做的（以及为什么）
========================

* **裸的 local id 不按形状脱敏**。discord 的 17~20 位雪花号**就是**一个合法的
  纳秒时间戳，mattermost 的 26 位小写字母数字与哈希片段无法区分；按形状脱敏会
  把时间戳和日志正文一起毁掉。只脱敏**带平台前缀**的完整 ``platform:local_id``
  —— 那才是可判定、无歧义的形式。**代价（明说）**：仍打**裸** id 的那几行
  （例如 ``telegram: dropped message from non-whitelisted chat %s``）会漏。
  要根治得让适配器改打 ``conversation_id``，而 ``adapters/**`` 本次不许动。
* **连接串**（``postgres://user:pass@host/db``）不在 §2.1 的形状里，也不加 ——
  它与"路径 + 主机"高度重叠，误伤面比收益大。
* **国际手机号**只收中国大陆 ``1[3-9]`` + 9 位（可带 ``+86`` / ``86`` 前缀）。
  E.164 的国家码表要外部数据才准确，靠猜会把别的号码当手机号。
* **带分隔符的号码**（``138-0013-8000``）不收：每加一种分隔符就多一份误伤面，
  而日志里的号码绝大多数是裸的。

键哈希（``state.json`` 的键要不要一起脱敏）
============================================

**本次不实现**，结论留给决策者，见提交说明。简述：可行（同哈希进、同哈希出，
查找不受影响），但它的代价不在脱敏本身，而在
:meth:`~opencode_bridge.state.StateStore` 的**迁移面**：``state.json`` 的键 +
``inbox.db`` 的 ``conversation_id``（TEXT，``opencode_bridge/inbox.py:185``）+
``conversation_keys.py`` 的**歧义候选键**三处必须同时切换，
而``channel:`` 那批键按 G5 的结论**永不迁移**（归属不在盘上），于是"哈希键 +
遗留明文键"必须**长期共存**，读取侧要两套键空间。G5 的迁移机器正是为此而存在。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
from typing import Any, Iterable, Optional

from . import identity

__all__ = [
    "LOGGER_NAMESPACE",
    "Redactor",
    "RedactingFilter",
    "default_redactor",
    "install_redaction_filter",
    "redact_state_values",
]

#: 本仓库全部 logger 的命名空间前缀。34 个 ``getLogger`` 全部以它开头，
#: 所以挂在 root handler 上的过滤器能覆盖它们全部（见模块 docstring）。
LOGGER_NAMESPACE = "opencode_bridge"

#: 完整遮蔽后的标记外形。``[`` / ``]`` 刻意作为分隔符参与
#: ``secret-assignment-unquoted`` 那条规则的字符类判据 ——
#: 已脱敏的值（``token=[REDACTED:...]``）于是不会被二次脱敏，标签也就不被降级。
_MASK_TEMPLATE = "[REDACTED:%s]"

#: 摘要取多少位十六进制。12 位 = 48 位，碰撞概率对"对齐同一批日志行"绰绰有余，
#: 而短到肉眼无法反推。
_FINGERPRINT_HEX_CHARS = 12

#: 摘要**每 6 位插一个分隔符**。这一条是承重项，不是排版偏好：
#: 十六进制里数字占 10/16，一个 12 位的纯十六进制串**约有 1.4% 的概率**含
#: "11 位连续数字"，而 11 位连续数字正是手机号规则的形状 —— 于是脱敏层会
#: **把自己的输出再脱敏一次**，产出 ``conv#fphone#c7c2...`` 这种垃圾。
#: 隔断之后摘要里最多连续 6 位数字，结构上就永远不可能再被任何规则命中：
#: 脱敏因此是**幂等**的，不依赖"碰巧没抽到"。
_FINGERPRINT_BLOCK_SIZE = 6
_FINGERPRINT_SEPARATOR = "-"

#: 进程内 HMAC 密钥长度（字节）。
_KEY_BYTES = 32

#: 凭据形状。每条一律是 ``(标签, 已编译正则, 保留的捕获组下标)`` —— 三个元素固定，
#: 不用变长参数：``*rest`` 那种解包一旦某条少写一个元素，``preserved`` 就会**悄悄
#: 错位**，而错位的后果是标签错、或者把凭据明文留在日志里（不会抛，只会漏）。
#: ``re`` 的 flags 只能传给 :func:`re.compile`（``Pattern.sub`` 不收 flags），
#: 所以它们就写在各自的 ``re.compile`` 里面。
#:
#: 前六条逐字对应 AGENTS.md §2.1 的表格。后两条是 §2.1 之外**有意加的**超集，
#: 都有本仓库的具体证据：
#:
#: * ``bearer-token``：nextcloud / matrix / twitch 都用
#:   ``Authorization: Bearer <token>``，而适配器把异常原样打进
#:   ``logger.exception(...)``，栈里就可能出现请求头。
#: * ``secret-assignment-unquoted``：``httpsrv.log_message``（``httpsrv.py:316``）
#:   覆写了 ``BaseHTTPRequestHandler.log_message``，会把**整条请求行**（含 query
#:   string）打进 DEBUG 日志 —— webhook 平台把 token 放 query 时就漏在这里，
#:   而 §2.1 那条要求带引号，抓不到。
#:
#: ``preserved`` 是替换时**保留**的捕获组下标（1 起算），用来把 ``password=`` 这类
#: **键名**留在日志里 —— 键名不是秘密，丢掉它就看不出"这里本来有个口令"。
_CREDENTIAL_RULES = (
    (
        "private-key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?"
            r"(?:-----END [A-Z ]*PRIVATE KEY-----)?"
        ),
        (),
    ),
    (
        "telegram-bot-token",
        re.compile(r"\d{8,10}:[A-Za-z0-9_-]{35}"),
        (),
    ),
    (
        "slack-bot-token",
        re.compile(r"xox[bp]-[A-Za-z0-9-]{10,}"),
        (),
    ),
    (
        "slack-app-token",
        re.compile(r"xapp-[A-Za-z0-9-]{10,}"),
        (),
    ),
    ("github-token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), ()),
    ("openai-key", re.compile(r"sk-[A-Za-z0-9]{20,}"), ()),
    ("aws-access-key-id", re.compile(r"AKIA[0-9A-Z]{16}"), ()),
    (
        "bearer-token",
        re.compile(r"(?<![A-Za-z0-9])Bearer\s+[A-Za-z0-9._~+/=-]{16,}"),
        (),
    ),
    (
        # §2.1 原文：``(token|secret|password|api_key|app_secret)\s*[=:]\s*['"][^'"]{16,}['"]``
        # 超集三处：
        #   * 大小写不敏感 —— JSON 配置里 ``TOKEN=`` / ``Password:`` 都合法；
        #   * 名字里允许 ``-``（``api-key`` / ``x-api-key``）；
        #   * 名字后允许一个**收尾引号** —— JSON 的键是 ``"password"``，没有这一位
        #     就会漏掉 ``config.json`` 里最常见的写法。
        #   * 名字前用否定环视而不是 ``\b``：``_`` 是单词字符，``\b`` 在
        #     ``client_secret`` / ``bot_token`` 里反而**匹配不到**。
        "secret-assignment",
        re.compile(
            r"(?<![A-Za-z0-9])"
            r"((?:token|secret|password|passwd|api[_-]?key|app[_-]?secret"
            r"|access[_-]?token|client[_-]?secret)[\"']?\s*[=:]\s*)"
            r"(['\"])([^'\"\n]{16,})\2",
            re.IGNORECASE,
        ),
        (1,),
    ),
    (
        # 去掉引号要求的同一形状（URL query / ``Authorization:`` 赋值）。
        # 字符类里显式排除 ``[`` / ``]``：已脱敏的 ``token=[REDACTED:...]`` 于是
        # **不会**被二次脱敏，标签也就不被降级成更泛的那个。
        "secret-assignment-unquoted",
        re.compile(
            r"(?<![A-Za-z0-9])"
            r"((?:token|secret|password|passwd|api[_-]?key|app[_-]?secret"
            r"|access[_-]?token|client[_-]?secret)[\"']?\s*[=:]\s*)"
            r"([^\s\[\]&\"',;)\]}<>]{16,})",
            re.IGNORECASE,
        ),
        (1,),
    ),
)

#: 会话 id：``platform:local_id``。平台段**从 :data:`identity.KNOWN_PLATFORMS` **加上**
#: :data:`identity.LEGACY_PREFIXES` 生成，而不是在这里再抄一份 —— 抄的那份一定会在
#: 下一个平台落地时变成过期清单。legacy 前缀（``chat:`` / ``room:`` / ``channel:``）
#: 也必须收：它们**仍在使用**（``state.py`` 的迁移是"加载期重写"，而歧义的
#: ``channel:`` 按 G5 的结论**永不迁移**），所以它们每天都在日志里。
#: 只认带前缀的完整形式（见模块 docstring「刻意不做的」）。
_PLATFORM_ALTERNATION = "|".join(
    sorted(
        set(identity.KNOWN_PLATFORMS) | set(identity.LEGACY_PREFIXES),
        key=len,
        reverse=True,
    )
)
#: ``(?![a-z]+#)`` 是**幂等性**的承重项：本地段若是 ``conv#<摘要>`` 这种已脱敏
#: 形状就不再匹配。没有它，一条记录经过两个 handler 的过滤器时，``conv#abc`` 的
#: ``abc`` 会被**再摘要一次**，日志里出现两个不同的假摘要 —— 那比不脱敏更难查。
_CONVERSATION_ID_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])(" + _PLATFORM_ALTERNATION + r"):"
    r"((?![a-z]+#)[^\s,;)\]\}\"'<>&]+)"
)

#: 中国大陆手机号：``1[3-9]`` + 9 位，可带 ``+86`` / ``86`` 前缀。
#: 两侧都要求**不是数字**（``(?<![0-9])`` / ``(?![0-9])``）—— 这是误伤防线：
#: discord 雪花号（17~20 位）、mattermost id（26 位）、纳秒/微秒时间戳里都含
#: "11 位连续数字"子串，没有这两条断言会把它们拦腰截断。
#: 用 ``[0-9]`` 而不是 ``\d``：``\d`` 在 str 模式下还匹配阿拉伯数字等，
#: 会让边界断言与主体用两套数字类，边界就判不准了。
_PHONE_PATTERN = re.compile(
    r"(?<![0-9])(?:\+?86[- ]?)?1[3-9][0-9]{9}(?![0-9])"
)

#: 邮箱。TLD 必须是 **≥2 个字母** —— 这一条同时挡掉 ``x@1.2``（版本号/尺寸）这种
#: 紧邻形态；本地部分允许 ``.`` / ``_`` / ``%`` / ``+`` / ``-``（RFC 5322 的
#: dot-atom + quoted 里的常见字符）。
_EMAIL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9._%+\-])"
    r"[A-Za-z0-9._%+\-]{1,64}@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,63}"
    r"(?![A-Za-z0-9\-])"
)

#: 摘要不可用时的占位。**绝不能**退化成把原值放回去 —— 脱敏层宁可给出
#: "两个值看起来一样"，也不许泄漏（见 §8：宁可错，不可泄）。
_UNAVAILABLE_FINGERPRINT = "#?"


def _mask_replacement(label: str, preserved_groups: tuple):
    """构造一个 ``re.sub`` 回调：保留指定捕获组，其余整段替换成遮蔽标记。"""
    marker = _MASK_TEMPLATE % label

    def replace(matched: "re.Match[str]") -> str:
        prefix = "".join(
            matched.group(index) for index in preserved_groups
        )
        return prefix + marker

    return replace


class Redactor:
    """按类别替换文本里的敏感子串。

    :param key: HMAC 密钥。默认进程启动时随机生成，**只存在内存里**（见模块
        docstring「为什么用带密钥的摘要」）。测试传固定 ``key`` 以得到可断言的
        输出。

    本类**不持有任何状态**（除密钥），所以可以随手造一个给单测用，不必碰全局。
    """

    def __init__(self, *, key: Optional[bytes] = None) -> None:
        material = key
        if isinstance(material, str):
            material = material.encode("utf-8", "replace")
        elif isinstance(material, (bytearray, memoryview)):
            material = bytes(material)
        if not isinstance(material, bytes) or not material:
            material = os.urandom(_KEY_BYTES)
        self._key = material

    # ------------------------------------------------------------------
    # 摘要
    # ------------------------------------------------------------------
    def fingerprint(self, label: str, value: str) -> str:
        """``<label>#<6 位>-<6 位>``。同值同结果，不同值几乎不可能撞。

        分组隔断见 :data:`_FINGERPRINT_BLOCK_SIZE` —— 它让"脱敏结果不再被自己
        命中"成为**结构性质**而不是运气。

        失败时给 :data:`_UNAVAILABLE_FINGERPRINT` —— **不回退到原值**。
        """
        try:
            # surrogatepass：日志里可能有落单的代理字符（``normalize._clean``
            # 专门处理过这类输入），默认编码会抛，而脱敏层不该因此抛。
            encoded = value.encode("utf-8", "surrogatepass")
            hexdigest = hmac.new(self._key, encoded, hashlib.sha256).hexdigest()
        except Exception:  # noqa: BLE001 - 脱敏层绝不许把异常泄给调用方
            return label + _UNAVAILABLE_FINGERPRINT
        blocks = [
            hexdigest[start:start + _FINGERPRINT_BLOCK_SIZE]
            for start in range(0, _FINGERPRINT_HEX_CHARS, _FINGERPRINT_BLOCK_SIZE)
        ]
        return "%s#%s" % (label, _FINGERPRINT_SEPARATOR.join(blocks))

    # ------------------------------------------------------------------
    # 各类
    # ------------------------------------------------------------------
    def scrub_credentials(self, text: str) -> str:
        """凭据 → 整段遮蔽（见模块 docstring「凭据为什么不用摘要」）。"""
        for label, pattern, preserved in _CREDENTIAL_RULES:
            text = pattern.sub(_mask_replacement(label, preserved), text)
        return text

    def scrub_conversation_ids(self, text: str) -> str:
        """``telegram:123456`` → ``telegram:conv#<摘要>``（平台段保留可读）。"""

        def replace(matched: "re.Match[str]") -> str:
            platform = matched.group(1)
            local_id = matched.group(2)
            return "%s:%s" % (platform, self.fingerprint("conv", local_id))

        return _CONVERSATION_ID_PATTERN.sub(replace, text)

    def scrub_phones(self, text: str) -> str:
        """中国大陆手机号 → ``phone#<摘要>``。"""

        def replace(matched: "re.Match[str]") -> str:
            return self.fingerprint("phone", matched.group(0))

        return _PHONE_PATTERN.sub(replace, text)

    def scrub_emails(self, text: str) -> str:
        """邮箱 → ``email#<摘要>``。"""

        def replace(matched: "re.Match[str]") -> str:
            return self.fingerprint("email", matched.group(0))

        return _EMAIL_PATTERN.sub(replace, text)

    # ------------------------------------------------------------------
    # 组合
    # ------------------------------------------------------------------
    def scrub(self, text: Any) -> Any:
        """日志用：四类全上，**按固定顺序**。

        顺序不是随意的：先凭据（最长、最特化），再会话 id（带平台前缀，形状最
        确定），再手机号，最后邮箱。``email:bob@example.com`` 会被**会话 id**
        先吃掉、变成 ``email:conv#...`` —— 那比让邮箱规则把它切成
        ``email:email#...`` 更有用（一个标签就能说明它是哪一类会话）。

        非字符串**原样返回**：脱敏层不是强制转换器，接过什么类型都不该改。
        """
        if not isinstance(text, str):
            return text
        try:
            scrubbed = self.scrub_credentials(text)
            scrubbed = self.scrub_conversation_ids(scrubbed)
            scrubbed = self.scrub_phones(scrubbed)
            return self.scrub_emails(scrubbed)
        except Exception:  # noqa: BLE001 - 见 fingerprint()：宁可漏，不可泄
            return text

    # ------------------------------------------------------------------
    # 落盘侧（``state.json``）
    # ------------------------------------------------------------------
    def scrub_persisted_value(self, value: Any) -> Any:
        """**落盘前**扫一遍值：只脱敏凭据，其余类别一律不动。

        为什么只脱敏凭据 —— 见模块 docstring「``state.json``：脱敏值，不脱敏键」。
        简版：``meta`` 的值是要**读回来用**的（``directory`` / ``stream_cursor``），
        把手机号 / 邮箱规则套上去会让 ``/cd`` 静默去到不存在的地方。

        字典**逐项**处理、**键原样保留**；整数 / 布尔 / ``None`` 原样返回
        （``stream_cursor`` 就是整数，改它会让邮件重投逻辑失效）。
        """
        if isinstance(value, str):
            return self.scrub_credentials(value)
        if isinstance(value, dict):
            return {
                key: self.scrub_persisted_value(item) for key, item in value.items()
            }
        if isinstance(value, list):
            return [self.scrub_persisted_value(item) for item in value]
        return value


#: 进程级默认实例：日志过滤器与 ``state.json`` 落盘**共用**同一个密钥，
#: 于是"日志里的 conv# 与盘上的值"仍能对上。
_DEFAULT_REDACTOR: Optional[Redactor] = None


def default_redactor() -> Redactor:
    """进程内唯一的默认脱敏器（惰性创建）。"""
    global _DEFAULT_REDACTOR
    if _DEFAULT_REDACTOR is None:
        _DEFAULT_REDACTOR = Redactor()
    return _DEFAULT_REDACTOR


#: 渲染栈回溯用的格式化器。``Formatter.formatException`` 不看 fmt，
#: 所以拿一个无格式串的实例专用即可（它只用 ``record.exc_info``）。
_TRACEBACK_FORMATTER = logging.Formatter()


class RedactingFilter(logging.Filter):
    """在 handler 上把 :class:`logging.LogRecord` 里的敏感值换掉。

    **只改 LogRecord**，所以它能碰到的只有"即将写进日志的字节"——
    :mod:`opencode_bridge.outbound` 那种把正文交给 ``adapter.send`` 的路径
    结构上不可能被它影响（出站正文不经过 logging）。

    两处必改：

    * ``msg`` / ``args``：先把 ``%`` 格式化**渲染出来**再脱敏，然后
      ``args`` 置空。⚠️ 不能只替换模板里的子串 —— 模板里若含被脱敏片段带走的
      ``%``（邮箱的 local part 允许 ``%``！），而 ``args`` 还留着旧值，
      formatter 的 ``msg % args`` 会抛 ``TypeError: not all arguments
      converted``，整行日志**丢失**。先渲染就没有这个悬案。
    * ``exc_text``：``logger.exception(...)`` 的栈里可能有请求头 / URL。
      ``Formatter.format`` 只在 ``record.exc_text`` 为空时才自己渲染，
      所以这里**预先填好**一份脱敏过的，formatter 就会照着用。

    :func:`filter` **永远返回 True** —— 脱敏层只许改内容，不许丢记录。
    吞掉一行日志比漏掉一个值难查得多。
    """

    def __init__(self, redactor: Optional[Redactor] = None) -> None:
        super().__init__()
        self._redactor = redactor if redactor is not None else default_redactor()

    @property
    def redactor(self) -> Redactor:
        return self._redactor

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            self._scrub_message(record)
            self._scrub_traceback(record)
        except Exception:  # noqa: BLE001 - 脱敏层绝不许让日志路径抛
            return True
        return True

    def _scrub_message(self, record: logging.LogRecord) -> None:
        rendered = record.getMessage()
        if not isinstance(rendered, str):
            return
        scrubbed = self._redactor.scrub(rendered)
        if scrubbed == rendered:
            return
        record.msg = scrubbed
        # 置空而不是留元组：``getMessage`` 判的是 ``if self.args``，
        # 留着一个对不上号的元组才是那个 TypeError 的来源。
        record.args = None

    def _scrub_traceback(self, record: logging.LogRecord) -> None:
        if not record.exc_info or record.exc_text:
            return
        formatted = _TRACEBACK_FORMATTER.formatException(record.exc_info)
        if isinstance(formatted, str):
            record.exc_text = self._redactor.scrub(formatted)


def _handlers_of(logger: logging.Logger) -> list:
    return list(getattr(logger, "handlers", []) or [])


def install_redaction_filter(
    *,
    redactor: Optional[Redactor] = None,
    include_root: bool = True,
) -> list:
    """把 :class:`RedactingFilter` 挂到**已经存在**的 handler 上，并**顺带**装上
    顺序无关的那一层（:mod:`opencode_bridge.redaction_coverage`，记录创建时就脱敏）。

    :param redactor: 用哪个脱敏器；默认 :func:`default_redactor`。
    :param include_root: 是否也挂到 root logger 的 handler 上。**默认要**
        —— ``basicConfig`` 把 handler 装在 root 上，而全部 34 个 logger 都
        ``propagate`` 到 root，那是每一行日志真正落地的那一个。

    **可重复调用，且后一次赢**：本函数会先摘掉自己先前挂上去的实例再挂新的。
    所以"换个密钥重装"是确定性的，不会在同一个 handler 上叠两层。

    返回挂到的 handler 列表（便于测试断言，也便于调用方确认"确实挂上了"）。

    ⚠️ **handler 那一层依赖"它在走位里的位置"，这一点曾经被记错了**：
    此前这里写的是"之后**新挂**上去的 handler 不会被覆盖"。**实测不成立** ——
    :class:`RedactingFilter` 是**就地改**那条共享的 ``LogRecord``，而
    ``Logger.callHandlers`` 把**同一个对象**交给走位里的每个 handler，所以只要走位
    里有**任意一个**带过滤器的 handler，后面那些（挂得更晚的、级别不够的、
    ``logging.lastResort``）拿到的都已经是脱敏过的。

    真正的条件是另一条，也更窄也更险：**排在所有带过滤器 handler 之前**的那个 handler
    会先拿到明文 —— 典型就是挂在 :data:`LOGGER_NAMESPACE` 或更深的 logger 上的本地
    handler（``callHandlers`` 从发出记录的 logger 往上走，它比 root 近）。
    生产代码今天没有这种 handler（本包**零** ``addHandler``），所以那是个**潜伏**
    缺口。

    **根治**是把脱敏挪到**记录创建**那一刻 —— 那一层与 handler 的数量、挂上去的时刻、
    走位顺序都无关，见 :func:`~opencode_bridge.redaction_coverage.
    install_order_independent_coverage`。它在这里被调用，是因为
    ``__main__`` 只调本函数，而"覆盖"这件事**不该由调用点的顺序决定**。
    """
    the_redactor = redactor if redactor is not None else default_redactor()

    # 顺序无关的那一层：记录**创建**时就脱敏，于是"handler 什么时候挂、挂在哪一段
    # 走位上"都不再影响覆盖。延迟导入是为了避开本模块与它的循环依赖。
    from .redaction_coverage import install_order_independent_coverage

    install_order_independent_coverage(redactor=the_redactor)

    targets: list = []
    bridge_logger = logging.getLogger(LOGGER_NAMESPACE)
    targets.extend(_handlers_of(bridge_logger))
    if include_root:
        targets.extend(_handlers_of(logging.getLogger()))

    covered: list = []
    seen: set = set()
    for handler in targets:
        if id(handler) in seen:
            continue
        seen.add(id(handler))
        try:
            handler.filters = [
                existing
                for existing in getattr(handler, "filters", [])
                if not getattr(existing, "_opencode_bridge_redaction", False)
            ]
            installed = RedactingFilter(the_redactor)
            # 标记只给自己摘用：别把别人的 RedactingFilter 也清掉。
            installed._opencode_bridge_redaction = True
            handler.addFilter(installed)
            covered.append(handler)
        except Exception:  # noqa: BLE001 - 挂不上就当没挂，绝不打断启动
            continue
    return covered


def remove_redaction_filter() -> bool:
    """把 :func:`install_redaction_filter` 装上的东西**都**摘掉；摘掉了返回 ``True``。

    ⚠️ handler 那一层原本**没有**卸载口（只能重新 ``install`` 覆盖）。而顺序无关
    那一层挂在**进程全局**的 :func:`logging.setLogRecordFactory` 上 —— 全局的安装
    必须有配对的全局卸载，否则"曾经装过一次"的进程会**永久**带着它（测试进程里就是
    "后面每个用例的前提都被前面的用例改了，而没人会想到去查日志"）。

    只摘**带我们自己标记**的过滤器，别人的 ``RedactingFilter`` 不动。
    """
    from .redaction_coverage import remove_order_independent_coverage

    removed = remove_order_independent_coverage()
    for name in (LOGGER_NAMESPACE, None):        # None = root
        for handler in logging.getLogger(name).handlers:
            kept = [
                existing for existing in handler.filters
                if not getattr(existing, "_opencode_bridge_redaction", False)
            ]
            removed = removed or len(kept) != len(handler.filters)
            handler.filters = kept
    return removed


def redact_state_values(document: Any, *, redactor: Optional[Redactor] = None) -> Any:
    """``state.json`` 落盘前的**值**脱敏（**键原样保留**）。

    返回一份**新的**结构，调用方的内存对象不被改动 —— 所以
    ``get_meta()`` 在同一进程里读到的仍是原值，``/cd`` 这类"写进去再读出来"
    的行为零变化。
    """
    the_redactor = redactor if redactor is not None else default_redactor()
    try:
        return the_redactor.scrub_persisted_value(document)
    except Exception:  # noqa: BLE001 - 落盘不该因为脱敏失败而失败
        return document

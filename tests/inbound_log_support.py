"""共用夹具与常量：给"入站日志里的 id"这三份测试用。

**为什么单独一个模块**：三份测试按职责分工（见
:mod:`tests.test_redactable_ids` / :mod:`tests.test_inbound_log_structure` /
:mod:`tests.test_inbound_log_per_platform`），而它们共用同一批夹具与同一组常量。
放在一起是"改一处夹具只碰一个文件"；塞进其中任一份测试里就是"改夹具要碰两个
测试文件"。

⚠️ 文件名**不**以 ``test_`` 开头，所以 pytest 不会收集它 —— 里面没有用例，
只有替身。
"""

from __future__ import annotations

import logging
from email.message import EmailMessage
from email.policy import default as default_policy
from pathlib import Path

from opencode_bridge.adapters._redactable_ids import MISSING_ID  # noqa: F401
from opencode_bridge.hooks import Inbound
from opencode_bridge.redaction import Redactor, RedactingFilter
from opencode_bridge.redaction_coverage import install_order_independent_coverage

#: 固定密钥 —— 断言要的是**确定的**摘要，而不是"看起来像摘要"。
FIXED_REDACTION_KEY = b"inbound-log-ids-test-key-32b!!"

#: 闸门翻转后的配置：``config_version >= 2`` + **空**清单 = 谁都不放行。
#: 这正是把"偶发泄露"变成"系统性泄露"的那组值，也是这些测试要站在的场景里。
FLIPPED_GATE_CONFIG = {"config_version": 2, "allowed_chat_ids": []}

ADAPTERS_DIR = Path(__file__).resolve().parent.parent / "opencode_bridge" / "adapters"

#: 13 个平台的 ``name``；逐平台测试与"每个平台都被改到了"那条断言都用它。
ALL_PLATFORMS = (
    "a2a", "discord", "email", "homeassistant", "irc", "matrix",
    "mattermost", "nextcloud", "ntfy", "qqbot", "slack", "telegram", "twitch",
)


# ======================================================================
# 测试替身
# ======================================================================
class RecordingHooks:
    """只记 :meth:`on_inbound` —— 逐平台测试用它证明"消息**确实**被丢了"。"""

    def __init__(self) -> None:
        self.inbounds: list[Inbound] = []

    def on_inbound(self, inbound: Inbound) -> None:
        self.inbounds.append(inbound)

    def on_callback(self, conversation_id: str, data: str, query_id: str) -> None:
        pass


class CollectingHandler(logging.Handler):
    """装一个 :class:`RedactingFilter` 的收集器 —— 复现生产里 root handler 上
    真正发生的那一次脱敏，断言看到的就是**会落盘的那一行**。

    那个脱敏器本尊挂在 :attr:`redactor` 上，因为**期望值必须由它算**（见
    :meth:`expected_conversation_fingerprint` 与 :class:`CapturedLogs` 的 docstring）。
    """

    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self.redactor = redactor
        self.lines: list[str] = []
        self.addFilter(RedactingFilter(redactor))

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())

    def expected_conversation_fingerprint(self, platform: str, local_id: str) -> str:
        """落盘那一行里应该出现的 ``<平台>:conv#<摘要>``。

        **只能**用捕获链上那个 :attr:`redactor` 算。期望值一旦由另一个实例算出，
        它就是"同一个值的另一个 HMAC"，断言必然红，而红得看不出原因（这正是
        ``tests/test_inbound_log_per_platform.py`` 那 14 条曾经的样子）。

        ``fingerprint`` 自带 ``conv#`` 标签，所以平台段拼在**外面**。
        """
        return "%s:%s" % (platform, self.redactor.fingerprint("conv", local_id))


class CapturedLogs:
    """上下文管理器：把某个 logger 的记录收到装了脱敏过滤器的 handler 上。

    ``propagate=False`` + 自己挂 handler ⇒ 断言不依赖 root 的 handler 长什么样，
    也不会把这一行打到测试输出里。

    ## 为什么还必须占住**进程全局**的那一层
    ==========================================

    脱敏有**两层**：handler 上的 :class:`RedactingFilter`，与
    :mod:`opencode_bridge.redaction_coverage` 挂在
    :func:`logging.setLogRecordFactory` 上的那一层（生产里两层都在）。**记录创建时
    的那一层先跑**，而脱敏引擎按构造是**幂等**的（第二遍是恒等变换，见
    ``redaction_coverage`` 的模块 docstring）⇒ 进程里只要存在别的那一层，
    handler 上这个固定密钥的过滤器**再也碰不到明文**，于是「期望值（固定密钥）」
    与「实际值（别人的密钥）」就是同一个值的两个不同 HMAC。

    实测的触发者（已定位）：``tests/test_platform_pairing.py`` 的
    ``TestPairCliRedemption`` 在**同一进程**里调真的
    :func:`opencode_bridge.__main__.main`，而它会
    :func:`~opencode_bridge.redaction.install_redaction_filter` —— 那会把那层全局
    脱敏按 :func:`~opencode_bridge.redaction.default_redactor` 的**每进程随机**密钥
    装上，并且**不摘**。

    所以这里在 :meth:`__enter__` 里按**本模块的密钥**占住全局那一层，在
    :meth:`__exit__` 里把进来时看到的那一个**原样放回去** —— 只摘自己装的，不动
    别人的（那条泄漏不是我们造成的，也不该由我们替它擦）。
    """

    def __init__(self, platform: str) -> None:
        self.redactor = Redactor(key=FIXED_REDACTION_KEY)
        self.handler = CollectingHandler(self.redactor)
        self.logger = logging.getLogger("opencode_bridge.adapters.%s" % platform)
        self._previous_record_factory = None

    def __enter__(self) -> "CollectingHandler":
        self._previous_record_factory = logging.getLogRecordFactory()
        if not install_order_independent_coverage(redactor=self.redactor):
            logging.setLogRecordFactory(self._previous_record_factory)
            raise RuntimeError(
                "装不上进程全局的脱敏层 ⇒ 记录会被别的密钥先脱敏掉，本模块的"
                "期望值必然对不上（见本类 docstring）。这里必须**响亮地**失败，"
                "不能让 14 条逐平台断言红得看不出原因。"
            )
        self._previous = (self.logger.propagate, self.logger.level, self.logger.disabled)
        self.logger.propagate = False
        self.logger.disabled = False
        self.logger.setLevel(logging.DEBUG)
        self.logger.addHandler(self.handler)
        return self.handler

    def __exit__(self, *_exc: object) -> None:
        propagate, level, disabled = self._previous
        self.logger.removeHandler(self.handler)
        self.logger.propagate = propagate
        self.logger.setLevel(level)
        self.logger.disabled = disabled
        # 放回**进来时看到的**那一个（可能正是一层别人装的脱敏，见本类 docstring）。
        logging.setLogRecordFactory(self._previous_record_factory)


class SlackFakeWebSocket:
    """Slack ``_handle_envelope`` 要 ack —— 只需要一个能吞掉 ack 的对象。"""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text: str) -> None:
        self.sent.append(text)


# ---------------------------------------------------------------- 邮件夹具
def make_raw_mail(sender: str) -> bytes:
    """一封最小的原始邮件（走标准库序列化，测的才是真实报文）。"""
    message = EmailMessage(policy=default_policy)
    message["From"] = sender
    message["To"] = "bot@example.com"
    message["Subject"] = "问题"
    message["Message-ID"] = "<stranger-message-1@example.net>"
    message.set_content("hello there", subtype="plain", charset="utf-8")
    return message.as_bytes()


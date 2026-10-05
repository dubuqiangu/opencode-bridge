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
    真正发生的那一次脱敏，断言看到的就是**会落盘的那一行**。"""

    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self.lines: list[str] = []
        self.addFilter(RedactingFilter(redactor))

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


class CapturedLogs:
    """上下文管理器：把某个 logger 的记录收到装了脱敏过滤器的 handler 上。

    ``propagate=False`` + 自己挂 handler ⇒ 断言不依赖 root 的 handler 长什么样，
    也不会把这一行打到测试输出里。
    """

    def __init__(self, platform: str) -> None:
        self.redactor = Redactor(key=FIXED_REDACTION_KEY)
        self.handler = CollectingHandler(self.redactor)
        self.logger = logging.getLogger("opencode_bridge.adapters.%s" % platform)

    def __enter__(self) -> "CollectingHandler":
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


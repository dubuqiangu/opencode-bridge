"""共用夹具：给「日志实参里不该出现凭据 / 载荷内容」这几份测试用。

**为什么不复用** :mod:`tests.inbound_log_support` 里那个 ``CapturedLogs``：
它只存 ``record.getMessage()``，而 ``logger.exception`` 的 **traceback 不在其中**
（在 ``record.exc_info`` 里，要 ``Formatter().formatException`` 才拿得到）。
本次要断言"级别与 traceback 都没被这次改动碰掉"，所以这里连 ``levelname`` 与
traceback 一起存。

断言看到的是**装上 RedactingFilter 之后**的 ``getMessage()`` —— 生产里 root handler
上真正发生的那一次脱敏，也就是用户 grep 到的样子。

⚠️ 文件名**不**以 ``test_`` 开头，所以 pytest 不会收集它 —— 里面没有用例，只有替身。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from opencode_bridge.redaction import Redactor, RedactingFilter

#: 固定密钥 —— 断言要的是**确定的**摘要，而不是"看起来像摘要"。
FIXED_REDACTION_KEY = b"inbound-log-ids-test-key-32b!!"


def expected_conversation_digest(platform: str, local_id: str) -> str:
    """``<platform>:conv#<摘要>`` —— **直接从 Redactor 算**，不经被测函数。

    这是 C2 会话 id 规则的真实产物（``scrub_conversation_ids`` 就是拿
    ``fingerprint("conv", local_id)`` 拼上平台前缀），所以拿它当期望值不会跟着
    实现一起错 —— ⛔ 绝不能改成调 :func:`redactable_id` 算期望值，那样实现错了
    断言会跟着错，测试就成了恒真。
    """
    redactor = Redactor(key=FIXED_REDACTION_KEY)
    return "%s:%s" % (platform, redactor.fingerprint("conv", local_id))


class CapturedRecord:
    """一条**脱敏之后**的日志记录。

    三个字段各回答一个问题：

    * ``message`` —— 用户 grep 到的那一行（脱敏已发生）；
    * ``levelname`` —— 级别有没有被这次改动悄悄改掉；
    * ``traceback`` —— ``logger.exception`` 的异常正文（``exc_info`` 展开），
      排障要的就是它，"只改记什么"不许把它弄掉。
    """

    def __init__(self, record: logging.LogRecord) -> None:
        self.message: str = record.getMessage()
        self.levelname: str = record.levelname
        self.exc_info: Optional[Any] = record.exc_info
        self.traceback: str = (
            "" if record.exc_info is None
            else logging.Formatter().formatException(record.exc_info)
        )


class _CollectingHandler(logging.Handler):
    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self.records: list[CapturedRecord] = []
        self.addFilter(RedactingFilter(redactor))

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(CapturedRecord(record))

    def lines(self, fragment: str) -> list[str]:
        """含 ``fragment`` 的那些行（断言按文案取行，而不是按顺序）。"""
        return [r.message for r in self.records if fragment in r.message]


class captured_logs:
    """上下文管理器：把某个适配器 logger 的记录收到装了脱敏过滤器的 handler 上。

    ``propagate=False`` + 自己挂 handler ⇒ 断言不依赖 root 的 handler 长什么样，
    也不会把这一行打到测试输出里。
    """

    def __init__(self, platform: str) -> None:
        self._redactor = Redactor(key=FIXED_REDACTION_KEY)
        self.handler = _CollectingHandler(self._redactor)
        self.logger = logging.getLogger("opencode_bridge.adapters.%s" % platform)

    def __enter__(self) -> _CollectingHandler:
        self._previous = (self.logger.propagate, self.logger.level,
                          self.logger.disabled)
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


class RecordingHooks:
    """只记 :meth:`on_inbound` —— 用来证明"消息**确实**被丢了 / 照常放行了"。"""

    def __init__(self) -> None:
        self.inbounds: list = []

    def on_inbound(self, inbound: Any) -> None:
        self.inbounds.append(inbound)

    def on_callback(self, conversation_id: str, data: str, query_id: str) -> None:
        pass

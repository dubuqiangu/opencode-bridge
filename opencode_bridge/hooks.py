"""Shared wire types between the OpenCode side and the messaging adapters.

This module is pure data: no I/O, no side effects. Both lanes depend on it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Protocol, Sequence

__all__ = [
    "Button",
    "Inbound",
    "Outbound",
    "MsgHandle",
    "Hooks",
    "SendError",
    "SendResult",
]


@dataclass(frozen=True)
class Button:
    """Inline keyboard button. ``data`` is an opaque string passed back verbatim."""

    label: str
    data: str


@dataclass(frozen=True)
class Inbound:
    """An event arriving from a messaging platform.

    ``kind`` is ``"text"`` for ordinary messages and ``"callback"`` for inline
    keyboard presses. For ``"callback"`` the ``text`` field carries the button
    ``data`` payload.
    """

    conversation_id: str
    text: str
    kind: str = "text"
    user_id: Optional[str] = None
    message_id: Optional[str] = None
    callback_query_id: Optional[str] = None
    platform: str = ""
    raw: Any = None

    @property
    def is_callback(self) -> bool:
        return self.kind == "callback"


@dataclass(frozen=True)
class Outbound:
    """Something to render on a messaging platform."""

    conversation_id: str
    text: str
    kind: str = "text"  # "text" | "progress" | "final" | "error"
    buttons: tuple[Button, ...] = field(default_factory=tuple)
    session_id: Optional[str] = None


@dataclass(frozen=True)
class MsgHandle:
    """Locates a previously sent message so it can be edited or replaced."""

    conversation_id: str
    message_id: str
    platform: str


class SendError(str, Enum):
    """平台中立的发送失败分类（T1.3）。

    存在的意义：让消费方**不必**对各厂商的报错文本做 substring-match。
    适配器负责把 HTTP 状态码 / 平台错误码收敛到这 7 类。
    """

    TOO_LONG = "too_long"          # 内容超限（应改用分片）
    BAD_FORMAT = "bad_format"      # 请求格式非法 / 参数不被接受
    FORBIDDEN = "forbidden"        # 权限不足（token 无权对该会话发言）
    NOT_FOUND = "not_found"        # 会话 / 频道 / 消息已不存在
    RATE_LIMITED = "rate_limited"  # 被限流，retry_after 给出建议等待秒数
    TRANSIENT = "transient"        # 网络 / 5xx 等可重试故障
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SendResult:
    """一次出站发送的结构化结果（T1.3）。

    ``ok=False`` 时 ``error_kind`` 一定有值；``partial=True`` 表示分片发送中
    部分成功（``handle`` 指向最后一条成功的消息）——脚本不能把这种情况记成成功。
    """

    platform: str
    ok: bool
    handle: Optional[MsgHandle] = None
    error_kind: SendError = SendError.UNKNOWN
    error_detail: str = ""
    retry_after: Optional[float] = None
    partial: bool = False


class Hooks(Protocol):
    """Callbacks the adapter layer uses to push events into the core.

    All hooks are invoked from adapter-owned threads; the core must be
    internally synchronized.
    """

    def on_inbound(self, inbound: Inbound) -> None:
        """Called for every user message / button press."""

    def on_callback(self, conversation_id: str, data: str, query_id: str) -> None:
        """Called for inline keyboard presses before the adapter answers them.

        Implementations MUST NOT assume the adapter already acknowledged the
        query; call ``Adapter.answer(query_id, text)`` for that.
        """

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

    # ------------------------------------------------------------------
    # Stream cursors（**可选**实现）
    # ------------------------------------------------------------------
    # 适配器只拿到 ``config`` 与 ``hooks``，**拿不到** ``StateStore``；而轮询型
    # 平台（email 的 IMAP UID、telegram 的 offset、matrix 的 next_batch）必须把
    # "读到哪了"落盘，否则**每次重启都会重新从最新位置开始**，于是停机期间到达
    # 的消息被无声丢弃。所以持久化只经由下面两个钩子，适配器不直接接触存储。
    #
    # ⚠️ 之所以是**可选**的：测试替身、以及没有状态存储的嵌入式调用方都不实现
    # 它们。因此调用方必须用 ``getattr`` 探测，缺失时退化成"没有已存位置"，
    # **并且必须告警**——静默的历史跳过正是本仓库反复修的那类数据丢失。

    def load_stream_cursor(self, stream_scope: str) -> Optional[int]:
        """读回 ``stream_scope`` 这条消息流上次持久化的位置，没有则返回 ``None``。

        ``stream_scope`` 是适配器自选的**稳定**字符串（必须区分账号 / 频道 /
        邮箱），因为多条流会共用同一个 ``state.json``。

        ``None`` 表示"确实没有已存状态"（首次运行），调用方据此走首次启动的
        语义；不要用它表达"读失败"，读失败应抛异常或退化成 ``None`` 并告警。
        """

    def save_stream_cursor(self, stream_scope: str, position: int) -> None:
        """把 ``stream_scope`` 的位置写成 ``position``，供下次启动续跑。

        从适配器的轮询线程调用，实现必须线程安全，且**不应抛异常**：写失败的
        代价只是重启后可能重投一封（由写前日志兜底），而抛出去会打断整个
        收信循环。
        """

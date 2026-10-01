"""Shared wire types between the OpenCode side and the messaging adapters.

This module is pure data: no I/O, no side effects. Both lanes depend on it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, Sequence

__all__ = [
    "Button",
    "Inbound",
    "Outbound",
    "MsgHandle",
    "Hooks",
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

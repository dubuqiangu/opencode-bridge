"""Channel status normalisation (T1.5) — a structured, closed state machine.

Provenance
----------
`dsh-im-gateway``src/manager.ts:29-44`` (``normalizeChannelStatus``) derives a
four-value display status by running **Chinese regexes over free-form text**::

    /错误|失败|异常|缺少依赖|已断开|已停止|已登出|仅支持/.test(raw)
    /等待|登录中|连接中|重连中|鉴权中|握手|未启动/.test(raw)

That coupling is the problem: the adapter only has to reword one log line
("异常" -> "出错了") and the UI silently flips a healthy channel to 异常,
because the *rendering* layer is guessing at the *protocol* layer's meaning.

Why we do not use regex
-----------------------
1. **Wrong layer.** The decision "is this channel broken?" belongs to whoever
   owns the channel lifecycle (the adapter), not to whoever formats a table.
   Here the adapter says ``state=ChannelState.ERROR``; we never re-infer it.
2. **Non-total and unmaintainable.** A regex is a *partial* function over an
   open string space: it cannot enumerate its own gaps, so every new adapter
   wording is a latent UI bug and every "fix" grows the alternation. A closed
   :class:`ChannelState` enum is total — unknown input has one documented
   fallback (:attr:`ChannelState.DISABLED`) instead of a wrong guess.

``LEGACY_TEXT_STATES`` keeps a **narrow escape hatch** for old callers that
still hand us prose: it is an explicit, finite ``dict`` of *whole-string*
entries (no substring scanning, no regex).  Anything outside the table stays
:attr:`~ChannelState.DISABLED` with the original text preserved in
:attr:`ChannelStatus.detail`, so an unmapped string degrades to "unknown" and
never to a wrong verdict.

Design notes
------------
* :meth:`ChannelStatus.is_receiving` is **declared, never inferred**.  Inbound
  capability is a property of the transport (Socket Mode vs. REST polling),
  so the adapter that starts the listener passes it in; this module refuses
  to guess it from a state or a string.
* :attr:`ChannelStatus.detail` is *decorative*.  Nothing in this module ever
  branches on it.
* Everything here is a pure function of its arguments — no I/O, no globals
  that mutate, trivially unit-testable.
"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple, Union

__all__ = [
    "ChannelState",
    "ChannelStatus",
    "LEGACY_TEXT_STATES",
    "PLATFORM_LABELS",
    "STATE_LABELS",
    "STATE_MARKS",
    "USABLE_STATES",
    "channel_state",
    "normalize_platform_status",
    "render_table",
    "summarize",
]


class ChannelState(str, Enum):
    """Closed set of channel lifecycle states.

    Inherits from ``str`` so a state *is* its wire value: ``ChannelState.ERROR
    == "error"`` and ``json.dumps`` needs no custom encoder.
    """

    #: Transport/handshake in progress; nothing may be sent yet.
    CONNECTING = "connecting"
    #: Up and healthy.
    CONNECTED = "connected"
    #: Up, but a capability is impaired (e.g. send-only, degraded send path).
    DEGRADED = "degraded"
    #: An explicit failure the adapter knows about.
    ERROR = "error"
    #: Not configured, or explicitly turned off.
    DISABLED = "disabled"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


#: States from which messages can still flow.  ``DEGRADED`` counts: a channel
#: that can only *send* is more useful than one that is down, and hiding that
#: as "unavailable" would push callers to hide real send capability.
USABLE_STATES = frozenset({ChannelState.CONNECTED, ChannelState.DEGRADED})

#: Compact glyphs for :func:`render_table`.
STATE_MARKS: Mapping[ChannelState, str] = {
    ChannelState.CONNECTED: "\u2705",  # white heavy check mark
    ChannelState.CONNECTING: "\U0001f504",  # counterclockwise arrows
    ChannelState.DEGRADED: "\u26a0\ufe0f",  # warning sign + emoji presentation
    ChannelState.ERROR: "\u2716",  # heavy multiplication x
    ChannelState.DISABLED: "\u23f8",  # pause button
}

#: Chinese display names (kept out of the enum so the wire values stay ASCII).
STATE_LABELS: Mapping[ChannelState, str] = {
    ChannelState.CONNECTED: "已连接",
    ChannelState.CONNECTING: "连接中",
    ChannelState.DEGRADED: "功能受损",
    ChannelState.ERROR: "异常",
    ChannelState.DISABLED: "未连接",
}

#: Fallback display names for known platform keys.
PLATFORM_LABELS: Mapping[str, str] = {
    "telegram": "Telegram",
    "slack": "Slack",
    "discord": "Discord",
    "matrix": "Matrix",
    "mattermost": "Mattermost",
    "irc": "IRC",
    "twitch": "Twitch",
    "nextcloud-talk": "Nextcloud Talk",
}

#: Conservative whole-string mapping for legacy free-text status values.
#:
#: Every key is an *exact* (whitespace-normalised, case-folded) status string
#: that some historical caller may still emit.  We deliberately do **not**
#: substring-scan this table: a word appearing inside an unrelated sentence is
#: exactly the bug class that made the dsh-im-gateway regex fragile.  Prose
#: that is not listed here falls back to ``DISABLED`` with the text kept in
#: ``detail``.
LEGACY_TEXT_STATES: Mapping[str, ChannelState] = {
    # -- our own wire values / obvious ASCII aliases ---------------------
    "connecting": ChannelState.CONNECTING,
    "pending": ChannelState.CONNECTING,
    "connected": ChannelState.CONNECTED,
    "ready": ChannelState.CONNECTED,
    "degraded": ChannelState.DEGRADED,
    "error": ChannelState.ERROR,
    "failed": ChannelState.ERROR,
    "disabled": ChannelState.DISABLED,
    "not_configured": ChannelState.DISABLED,
    "offline": ChannelState.DISABLED,
    # -- dsh-im-gateway ChannelDisplayStatus ----------------------------
    "已连接": ChannelState.CONNECTED,
    "连接中": ChannelState.CONNECTING,
    "异常": ChannelState.ERROR,
    "未连接": ChannelState.DISABLED,
    # -- in-progress provisioning wording (unambiguous subset) ------------
    "登录中": ChannelState.CONNECTING,
    "鉴权中": ChannelState.CONNECTING,
    "重连中": ChannelState.CONNECTING,
    "等待扫码": ChannelState.CONNECTING,
    "扫码中": ChannelState.CONNECTING,
    # -- terminal failures ----------------------------------------------
    "连接失败": ChannelState.ERROR,
    "扫码失败": ChannelState.ERROR,
    "扫码启动失败": ChannelState.ERROR,
    "二维码已过期": ChannelState.ERROR,
    # -- explicitly off --------------------------------------------------
    "已停用": ChannelState.DISABLED,
    "未启用": ChannelState.DISABLED,
    "未配置": ChannelState.DISABLED,
    "已取消": ChannelState.DISABLED,
}

StateLike = Union[ChannelState, str, None, Any]

#: Sentinel for "key absent from LEGACY_TEXT_STATES" — distinct from a
#: legitimate mapping *to* DISABLED, which must not echo the word into detail.
_MISSING = object()


def channel_state(value: StateLike) -> ChannelState:
    """Parse ``value`` into a :class:`ChannelState`, never raising.

    Total by construction: ``ChannelState`` passes through, strings are looked
    up by wire value (case/whitespace insensitive), anything else — including
    ``None``, an unknown future state, or a wrong-typed JSON value — degrades
    to :attr:`ChannelState.DISABLED`.  "Unknown" must never crash a status
    dump.
    """
    if isinstance(value, ChannelState):
        return value
    if value is None:
        return ChannelState.DISABLED
    if not isinstance(value, str):
        # An int / dict / object slipped in from JSON: refuse to guess.
        return ChannelState.DISABLED
    key = " ".join(value.split()).casefold()
    if not key:
        return ChannelState.DISABLED
    try:
        return ChannelState(key)
    except ValueError:
        pass
    return LEGACY_TEXT_STATES.get(key, ChannelState.DISABLED)


@dataclass(frozen=True)
class ChannelStatus:
    """Immutable snapshot of one channel's health.

    ``detail`` is free text kept for humans only; no logic in this module
    reads it.  ``receiving`` is *declared* by the caller that owns the
    transport — never inferred.
    """

    platform: str  # 'telegram' | 'slack' | ... (lower-case key)
    label: str  # 'Telegram' | 'Slack' | ...
    state: ChannelState
    detail: str = ""
    receiving: bool = True

    def __post_init__(self) -> None:
        # frozen dataclass: normalise via object.__setattr__.  Direct
        # construction is as forgiving as normalize_platform_status(), so
        # ChannelStatus("slack", "Slack", "oops") is DISABLED, not a crash.
        object.__setattr__(self, "platform", str(self.platform).strip().lower())
        object.__setattr__(self, "label", str(self.label).strip() or self._default_label())
        object.__setattr__(self, "state", channel_state(self.state))
        object.__setattr__(self, "detail", "" if self.detail is None else str(self.detail))
        object.__setattr__(self, "receiving", bool(self.receiving))

    def _default_label(self) -> str:
        return PLATFORM_LABELS.get(str(self.platform).strip().lower(), str(self.platform))

    # --- derived semantics ---------------------------------------------
    @property
    def usable(self) -> bool:
        """True when messages can still flow (``CONNECTED`` or ``DEGRADED``)."""
        return self.state in USABLE_STATES

    @property
    def healthy(self) -> bool:
        """True only for a fully healthy :attr:`~ChannelState.CONNECTED`."""
        return self.state is ChannelState.CONNECTED

    @property
    def is_receiving(self) -> bool:
        """Whether inbound messages are accepted (declared, not guessed)."""
        return self.receiving

    @property
    def mark(self) -> str:
        """Single-glyph marker for :func:`render_table`."""
        return STATE_MARKS[self.state]

    @property
    def state_label(self) -> str:
        """Chinese display name for :attr:`state`."""
        return STATE_LABELS[self.state]

    # --- JSON round-trip ------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        """Plain-``dict`` payload (JSON-safe, all values serialisable)."""
        return {
            "platform": self.platform,
            "label": self.label,
            "state": self.state.value,
            "detail": self.detail,
            "receiving": self.receiving,
        }

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, Any]]) -> "ChannelStatus":
        """Rebuild from :meth:`to_dict` output.

        ``state`` goes through :func:`channel_state`, so a stale or corrupt
        payload degrades to ``DISABLED``.  A non-mapping ``data`` is a
        programming error and raises ``TypeError``.
        """
        if not isinstance(data, Mapping):
            raise TypeError(f"ChannelStatus.from_dict expects a mapping, got {type(data).__name__}")
        return cls(
            platform=data.get("platform", ""),
            label=data.get("label", ""),
            state=channel_state(data.get("state")),
            detail=data.get("detail", "") or "",
            receiving=bool(data.get("receiving", True)),
        )

    def to_json(self, **kwargs: Any) -> str:
        """Serialise to a JSON string (``ensure_ascii=False`` by default)."""
        kwargs.setdefault("ensure_ascii", False)
        return json.dumps(self.to_dict(), **kwargs)

    @classmethod
    def from_json(cls, text: Union[str, bytes, bytearray]) -> "ChannelStatus":
        """Parse :meth:`to_json` output; malformed JSON raises ``ValueError``."""
        return cls.from_dict(json.loads(text))


def normalize_platform_status(
    platform: str,
    label: str = "",
    *,
    state: StateLike = None,
    detail: str = "",
    legacy_text: Optional[str] = None,
    receiving: bool = True,
) -> ChannelStatus:
    """Build a :class:`ChannelStatus`, preferring the structured ``state``.

    Resolution order:

    1. ``state`` given and recognised -> used as-is.
    2. ``state`` given but unrecognised -> ``DISABLED`` (``detail`` kept).
    3. ``state is None`` and ``legacy_text`` present -> one conservative
       lookup in :data:`LEGACY_TEXT_STATES`; a hit is mapped, a miss yields
       ``DISABLED`` with the **original text preserved** in ``detail``.
    4. Nothing usable -> ``DISABLED``.

    ``platform`` is lower-cased; an empty ``label`` falls back to
    :data:`PLATFORM_LABELS` (then to the raw key).
    """
    resolved = channel_state(state)
    if state is None and legacy_text is not None:
        text = str(legacy_text)
        hit = LEGACY_TEXT_STATES.get(" ".join(text.split()).casefold(), _MISSING)
        if hit is _MISSING:
            # Unknown prose: keep it visible instead of guessing a verdict.
            # (A *mapped* DISABLED entry must NOT echo its word into detail.)
            resolved = ChannelState.DISABLED
            detail = detail or text
        else:
            resolved = hit
    return ChannelStatus(
        platform=platform,
        label=label,
        state=resolved,
        detail=detail,
        receiving=receiving,
    )


def summarize(states: Iterable[ChannelState]) -> Dict[str, Any]:
    """Aggregate counters plus a single ``healthy`` verdict.

    Returns ``{"total", "connected", "usable", "degraded", "error",
    "disabled", "healthy"}``.

    * ``usable`` counts ``CONNECTED + DEGRADED`` (so it is *not* additive with
      ``connected``/``degraded``).
    * ``healthy`` is ``True`` when nothing is in :attr:`ChannelState.ERROR`
      **and** at least one channel is usable — "no error" alone is not enough
      (everything switched off is not a healthy bridge).

    ``CONNECTING`` is intentionally not a separate bucket: it is counted in
    ``total`` only.  Callers that need the breakdown should keep the statuses.
    """
    items = [channel_state(s) for s in states]
    connected = sum(1 for s in items if s is ChannelState.CONNECTED)
    degraded = sum(1 for s in items if s is ChannelState.DEGRADED)
    usable = sum(1 for s in items if s in USABLE_STATES)
    error = sum(1 for s in items if s is ChannelState.ERROR)
    disabled = sum(1 for s in items if s is ChannelState.DISABLED)
    return {
        "total": len(items),
        "connected": connected,
        "usable": usable,
        "degraded": degraded,
        "error": error,
        "disabled": disabled,
        "healthy": error == 0 and usable > 0,
    }


# --------------------------------------------------------------------------
# rendering (presentation only — no state logic lives here)
# --------------------------------------------------------------------------

#: Code points that terminals draw double-width although Unicode classifies
#: them as narrow (they default to emoji presentation).  Without this the
#: table's column borders would drift right of the CJK columns.
_FORCE_WIDE = frozenset("\u2716\u23f8\u26a0")
#: Variation selectors / ZWJ occupy no cell of their own.
_ZERO_WIDTH = frozenset("\ufe0e\ufe0f\u200d")

_HEADER = ("渠道", "状态", "入站", "说明")
_GAP = "  "
_MIN_LABEL_WIDTH = 6


def _display_width(text: str) -> int:
    """Terminal cell width of ``text`` (CJK and emoji count as two)."""
    width = 0
    for char in text:
        if char in _ZERO_WIDTH or unicodedata.combining(char):
            continue
        if char in _FORCE_WIDE or unicodedata.east_asian_width(char) in ("W", "F"):
            width += 2
        else:
            width += 1
    return width


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def render_table(statuses: List[ChannelStatus]) -> str:
    """Render ``statuses`` as a fixed-width Chinese table.

    Pure display: no verdict is computed here, the rows are whatever
    :class:`ChannelStatus` objects the caller built.  Every line (header,
    rule, rows) is padded to one identical display width, so the columns stay
    aligned in a CJK terminal.  An empty input still yields the header plus
    its rule.
    """
    rows: List[Tuple[str, str, str, str]] = [
        (
            status.label or status.platform,
            f"{status.mark} {status.state_label}",
            "是" if status.is_receiving else "否",
            status.detail or "-",
        )
        for status in statuses
    ]
    widths = [
        max(
            [_display_width(_HEADER[index])]
            + [_display_width(row[index]) for row in rows]
        )
        for index in range(len(_HEADER))
    ]
    widths[0] = max(widths[0], _MIN_LABEL_WIDTH)

    def line(cells: Tuple[str, str, ...]) -> str:
        return _GAP.join(_pad(cell, width) for cell, width in zip(cells, widths))

    total = sum(widths) + len(_GAP) * (len(widths) - 1)
    out = [line(_HEADER), "-" * total]
    out.extend(line(row) for row in rows)
    return "\n".join(out)
"""Fixed-width table rendering for channel status (presentation only).

Provenance
----------
Moved verbatim from ``status.py`` (its ``rendering`` section).  The split is
structural only — §5: the closed state machine
(:class:`~opencode_bridge.status.ChannelState`, ``ChannelStatus``) stays in
``status.py``, everything that *draws* it lives here.  ``render_table`` and
the width helpers are re-exported from ``status.py`` so no import path
changes.

No state logic lives here: ``render_table`` reads whatever attributes the
caller's ``ChannelStatus`` objects expose and computes no verdicts.  This
module has **no runtime import** of ``opencode_bridge.status`` — the
``ChannelStatus`` annotation is ``TYPE_CHECKING``-only, so the dependency
stays one-directional (``status`` → ``status_rendering``).
"""

from __future__ import annotations

import unicodedata
from typing import TYPE_CHECKING, List, Tuple

if TYPE_CHECKING:  # pragma: no cover - cycle guard, never imported at runtime
    from .status import ChannelStatus

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

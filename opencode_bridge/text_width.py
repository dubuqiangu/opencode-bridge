"""终端显示宽度帮助函数（从 ``__main__.py`` 逐字移植）。

``--status`` 各表格按显示宽度对齐的共同底座：``_dwidth`` 量宽、``_pad`` 补空格。
``import unicodedata`` 是 ``_dwidth`` 函数体内逐字保留的 lazy import。

Provenance: 2026-10-12 §5 ``__main__.py`` 专轮（装配 vs 视图渲染轴向，exp-68 裁定）。逐字移植 + ``__main__`` 顶部再进口，行为零变化。
"""
from __future__ import annotations


def _dwidth(text: str) -> int:
    """终端**显示宽度**：CJK / 全角字符占 2 列（用 ``unicodedata`` 判定）。

    ``f"{s:<n}"`` 是按**字符数**补齐的，中文表头（"平台" 2 字却占 4 列）会让整张
    表在终端里错位；平台名变长（如 "Nextcloud Talk" 14 字符）时更会挤掉与下一列
    之间的空格。两者都要按显示宽度算才对齐。
    """
    import unicodedata

    return sum(
        2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1 for ch in text
    )


def _pad(text: str, width: int) -> str:
    """按显示宽度左对齐补空格。"""
    return text + " " * max(0, width - _dwidth(text))

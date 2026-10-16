"""Markdown 结构分块：把长文切成「纯文字 / 围栏 / 表格」三种块，保形切分。

**来源**：移植自 ``MichengAI/dsh-im-connect`` 的 ``src/engine/split.ts``
（Apache-2.0，锁 commit ``e271ec1``）—— ``toBlocks`` / ``tableCells`` /
``fenceMarkerOf`` / ``isFenceClose`` / ``tableEnd`` / ``splitFence`` /
``splitTable`` / ``splitBlock`` 及其碎片的合并与「纯文字才加序号」判定。
借鉴点全目录见 ``docs/borrowing-catalog.md`` §1。

**为什么不整体照抄**：参考实现的纯文字切分是**逐码点**切（``[...text]``），
本桥 ``opencode_bridge.split`` 在 emoji / 组合序列 / 国旗上更强（字素感知）。
所以本模块**只做结构识别与保形拆分**，纯文字如何按长度切由调用方以
``split_prose`` 可调用对象**注入**——两条能力各有单一来源，不互相重写。

**与参考实现的差异（§6.4 要求逐条记）**：

- ``codePointLength(s)``（= ``[...s].length``）在本模块一律用 ``len(s)``；
  Python ``str`` 本身即码点序列，等价。
- ``tableCells`` 里定位行内代码结束标记用 ``str.find``（码点）而非 JS
  ``indexOf``（UTF-16 码元）；仅当行内代码含增补平面字符（emoji）时才有差异，
  且本模块的算法更正确。
- 段片之间一律以 ``"\\n"`` 连接（与参考实现一致）；因此 ``to_blocks`` 的重建文本
  在 CR 归一化后与原文本逐字相同，而**未闭合围栏**是唯一例外（会补出结束标记）。
- 未移植 ``markdownTableOffsets``（返回表格起始偏移）：本桥无调用方（§8 不造无用件）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Literal

__all__ = [
    "PREFIX_ALLOWANCE",
    "Block",
    "BlockKind",
    "Segment",
    "fence_marker",
    "is_fence_close",
    "merge_segments",
    "prefix_allowance",
    "render_chunks",
    "split_block",
    "split_fence",
    "split_table",
    "table_cells",
    "table_end",
    "to_blocks",
]

#: 块种类：纯文字 / 代码围栏 / GFM 管道表格。
BlockKind = Literal["prose", "fence", "table"]

#: 围栏标记：行首最多 3 个空格，随后是 3 个及以上的反引号或波浪号。
FENCE_MARKER = re.compile(r"^\s{0,3}(`{3,}|~{3,})")

#: 表格分隔行单元格（如 ``---`` / ``:--:``）。
_SEPARATOR_CELL = re.compile(r"^\s*:?-+:?\s*$")

#: 序号前缀的**最小**预留空间；长文按可能的片数扩展（见 :func:`prefix_allowance`）。
PREFIX_ALLOWANCE = 8

#: 注入的纯文字切分器签名：``(text, max_len) -> 段列表``。
ProseSplitter = Callable[[str, int], list[str]]


@dataclass
class Block:
    """一个结构块。``text`` 是块的全部行以 ``"\\n"`` 连接的结果。"""

    kind: BlockKind
    text: str


@dataclass
class Segment:
    """最小投递单位。``plain`` 为真才允许加序号前缀。"""

    text: str
    #: 围栏 / 表格片加序号会破坏结构，故只有纯文字片是 ``True``。
    plain: bool


def fence_marker(line: str) -> str | None:
    """返回该行行首的围栏标记（如 `` ``` `` 或 ``~~~``），没有则 ``None``。"""
    found = FENCE_MARKER.match(line)
    return found.group(1) if found else None


def is_fence_close(line: str, marker: str) -> bool:
    """结束标记：与开始标记同字符、不短于开始标记，且行内不能有其他内容。"""
    found = fence_marker(line)
    if not found or found[0] != marker[0] or len(found) < len(marker):
        return False
    return line.strip() == found


def table_cells(line: str) -> list[str] | None:
    """拆表格单元格；跳过转义竖线与行内代码里的竖线，允许省略外围竖线。

    返回 ``None`` 表示这行不是表格行（一个未转义的竖线都没有）。
    """
    source = line[:-1] if line.endswith("\r") else line
    cells: list[str] = []
    cell = ""
    separators = 0
    index = 0
    while index < len(source):
        char = source[index]
        if char == "\\" and index + 1 < len(source):
            cell += source[index + 1]
            index += 2
            continue
        if char == "`":
            run = 1
            while index + run < len(source) and source[index + run] == "`":
                run += 1
            marker = "`" * run
            end = source.find(marker, index + run)
            if end == -1:
                cell += marker
                index += run
                continue
            cell += source[index:end + run]
            index = end + run
            continue
        if char == "|":
            cells.append(cell)
            cell = ""
            separators += 1
            index += 1
            continue
        cell += char
        index += 1
    if separators == 0:
        return None
    cells.append(cell)
    if cells and cells[0].strip() == "":
        cells.pop(0)
    if cells and cells[-1].strip() == "":
        cells.pop()
    return cells or None


def table_end(lines: list[str], start: int) -> int:
    """返回 ``start`` 处表格的结束行号（不含）；不是表格则 ``-1``。

    判据：``start`` 是表头行，``start + 1`` 是与表头**同样格数**的分隔行
    （每格形如 ``---`` / ``:--:``），随后连续若干行仍是表格行。
    """
    header = table_cells(lines[start]) if start < len(lines) else None
    if not header:
        return -1
    separator = table_cells(lines[start + 1]) if start + 1 < len(lines) else None
    if not separator or len(separator) != len(header):
        return -1
    if not all(_SEPARATOR_CELL.match(cell) for cell in separator):
        return -1
    end = start + 2
    while end < len(lines):
        if not table_cells(lines[end]):
            break
        end += 1
    return end


def to_blocks(text: str) -> list[Block]:
    """把 ``text`` 切成块序列；未闭合的围栏会补一个结束标记（内容即开始标记）。"""
    lines = re.sub(r"\r\n?", "\n", text).split("\n")
    blocks: list[Block] = []
    prose: list[str] = []

    def flush_prose() -> None:
        if not prose:
            return
        blocks.append(Block("prose", "\n".join(prose)))
        prose.clear()

    index = 0
    while index < len(lines):
        marker = fence_marker(lines[index])
        if marker:
            closing = -1
            for scan in range(index + 1, len(lines)):
                if is_fence_close(lines[scan], marker):
                    closing = scan
                    break
            flush_prose()
            if closing == -1:
                body = lines[index:] + [marker]
                index = len(lines)
            else:
                body = lines[index:closing + 1]
                index = closing + 1
            blocks.append(Block("fence", "\n".join(body)))
            continue
        end = table_end(lines, index)
        if end > index:
            flush_prose()
            blocks.append(Block("table", "\n".join(lines[index:end])))
            index = end
            continue
        prose.append(lines[index])
        index += 1
    flush_prose()
    return blocks


def prefix_allowance(text_length: int) -> int:
    """序号前缀预留：``max(8, 2 * 位数 + 3)``，位数取自总码点数（参考实现的估法）。"""
    return max(PREFIX_ALLOWANCE, 2 * len(str(text_length)) + 3)


def split_fence(lines: list[str], max_len: int, split_prose: ProseSplitter) -> list[str]:
    """超长围栏：拆正文，每片补齐自己的开始与结束标记。放不下则 ``[]``。"""
    if len(lines) < 2:
        return []
    opening = lines[0]
    closing = lines[-1]
    body_max = max_len - len(opening) - len(closing) - 2
    if body_max < 16:
        return []
    body = "\n".join(lines[1:-1])
    return [
        opening + "\n" + piece + ("" if piece.endswith("\n") else "\n") + closing
        for piece in split_prose(body, body_max)
    ]


def split_table(lines: list[str], max_len: int, split_prose: ProseSplitter) -> list[str]:
    """超长表格：按行拆，每片重复表头与分隔行。放不下则 ``[]``。"""
    prefix = lines[0] + "\n" + lines[1]
    row_max = max_len - len(prefix) - 1
    if row_max < 8:
        return []
    pieces: list[str] = []
    current = prefix
    filled = False
    for row in lines[2:]:
        row_pieces = [row] if len(row) <= row_max else split_prose(row, row_max)
        for row_piece in row_pieces:
            if filled and len(current + "\n" + row_piece) > max_len:
                pieces.append(current)
                current = prefix
                filled = False
            current = current + "\n" + row_piece
            filled = True
    if filled:
        pieces.append(current)
    return pieces


def split_block(block: Block, budget: int, split_prose: ProseSplitter) -> list[Segment]:
    """把一个块切成段片。``budget`` 由调用方按块种类给出（纯文字用扣完前缀的预算）。

    围栏 / 表格若**无法**在预算内保形（正文或表头预留都不够），退回按纯文字切，
    并把段片标为纯文字（可以加序号，但结构已不保全）——与参考实现同向。
    """
    if len(block.text) <= budget:
        return [Segment(block.text, block.kind == "prose")]
    lines = block.text.split("\n")
    if block.kind == "fence":
        pieces = split_fence(lines, budget, split_prose)
        if pieces:
            return [Segment(piece, False) for piece in pieces]
    if block.kind == "table":
        pieces = split_table(lines, budget, split_prose)
        if pieces:
            return [Segment(piece, False) for piece in pieces]
    return [
        Segment(piece, block.kind == "prose") for piece in split_prose(block.text, budget)
    ]


def merge_segments(
    segments: list[Segment], budget: int, max_len: int
) -> list[list[Segment]]:
    """把相邻段片贪心装片：纯文字片用 ``budget``，结构片用 ``max_len``。"""
    chunks: list[list[Segment]] = []
    for segment in segments:
        if segment.text == "":
            continue
        if chunks:
            last = chunks[-1]
            limit = budget if last[0].plain else max_len
            merged = "\n".join([item.text for item in last] + [segment.text])
            if len(merged) <= limit:
                last.append(segment)
                continue
        chunks.append([segment])
    return chunks


def render_chunks(chunks: list[list[Segment]]) -> list[Segment]:
    """把装好的片渲染成最终段片，并重判「纯文字」——首行是围栏 / 表格则不能加序号。"""
    parts: list[Segment] = []
    for chunk in chunks:
        text = "\n".join(item.text for item in chunk).strip("\n")
        first_line = text.split("\n", 1)[0]
        structural = (
            first_line == ""
            or FENCE_MARKER.match(first_line) is not None
            or table_end(text.split("\n"), 0) > 0
        )
        parts.append(Segment(text, chunk[0].plain and not structural))
    return [part for part in parts if part.text != ""]

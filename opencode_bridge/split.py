"""长文本分片（纯函数，无 IO / 无日志）。

算法移植自 dsh-im-gateway ``src/core/split.ts``（104 行，只读参考），在 Python
侧做了两处增强：

1. **码点切分 + 组合序列原子化**
   Python 的 ``str`` 天然按码点迭代（不做 ``encode`` / ``bytes``），但裸码点会
   把 emoji 序列切坏：``👨‍👩‍👧``（U+1F468 ZWJ U+1F469 ZWJ U+1F467）会被拆成
   5 段、国旗会被拆成两个"方块字"、``1️⃣`` 会被拆成 ``1`` + VS16 + U+20E3。
   因此先用 :func:`_grapheme_atoms` 把文本重组成"不可再分"的原子单元（近似的
   字素簇），**上限按码点计、切点按原子计**（:func:`_count_atoms`），两者解耦。

2. **前缀两遍法（收敛）**
   先用不含前缀的粗估段数 ``n`` 切一遍，再按**真实前缀长度**
   ``len(prefix_fmt.format(i=index, n=n))`` 重新切一遍；``n`` 变化就再重算，
   最多重算 :data:`_MAX_PREFIX_PASSES` 轮。正常情况下预算
   ``max_len - 前缀长度`` 随 ``n`` 单调不增、实际段数随 ``n`` 单调不减，迭代
   序列单调有界，因此不会震荡，只需 1~2 轮即收敛。

断点优先级（窗口内从后往前找，命中即切）：
    换行 ``\\n`` → 中文句末 ``。！？…；`` → 英文 ``". "`` / ``", "`` 之后 → 硬切。

退化行为：
    * ``max_len <= 0`` → 不切分，返回 ``[text]``（空串返回 ``[]``）；
    * 前缀本身就吃掉 ``max_len`` → 放弃前缀，纯硬切；
    * 某段的剩余预算连一个原子都放不下 → **该段**放弃前缀做硬切（此时前缀
      预算已无意义，硬切可保证不超 ``max_len``）；
    * 编号轮用尽仍未收敛 → 用真实段数强制重编号（必要时按原子截断兜底），
      保证不会出现"第 3/2 段"这种编号错乱。
"""

from __future__ import annotations

import unicodedata

__all__ = ["split_text"]

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
#: 默认分段前缀（``str.format`` 模板，支持 ``{i}`` / ``{n}`` 具名占位）。
DEFAULT_PREFIX_FMT = "（{i}/{n}）"

#: 前缀段数重算轮数上限（``T1.4`` 冻结需求：最多重算 3 轮）。
_MAX_PREFIX_PASSES = 3

#: 零宽连接符（家庭 / 职业 / 性别 emoji 的黏合剂）。
_ZWJ = "\u200d"
#: 变体选择符 VS15（文本样式）/ VS16（emoji 样式）。
_VARIATION_SELECTORS = ("\ufe0e", "\ufe0f")
#: Fitzpatrick 肤色修饰符 U+1F3FB..U+1F3FF。
_SKIN_TONE = range(0x1F3FB, 0x1F400)
#: Regional_Indicator 符号 U+1F1E6..U+1F1FF（国旗由两个组成）。
_REGIONAL_INDICATOR = range(0x1F1E6, 0x1F200)
#: 组合类字符类别：``Mn``（非间距标记）与 ``Me``（含 keycap 的 U+20E3）。
_COMBINING_CATEGORIES = frozenset({"Mn", "Me"})

#: 中文句末标点。
_CN_SENTENCE_END = "。！？…；"


# ---------------------------------------------------------------------------
# 组合序列原子化
# ---------------------------------------------------------------------------
def _is_skin_tone(ch: str) -> bool:
    """是否 Fitzpatrick 肤色修饰符（``✋🏽`` 的 ``🏽``）。"""
    return ord(ch) in _SKIN_TONE


def _is_regional_indicator(ch: str) -> bool:
    """是否 Regional_Indicator 符号（🇨🇳 的 ``🇨``）。"""
    return ord(ch) in _REGIONAL_INDICATOR


def _is_combining(ch: str) -> bool:
    """是否组合类字符（须与基字符同属一个原子）。

    用 ``unicodedata.category`` 判定：``Mn``（非间距标记，如声调 / 变音符）
    与 ``Me``（组合类，包含 keycap 序列的 U+20E3）都不可独立成字。
    注意：**不能**用 ``unicodedata.combining``——它对 ``Me``（U+20E3）返回 0，
    会导致 ``1️⃣`` 被拆成 ``"1️" + "⃣"``。同理 ``Mc``（间距组合标记）虽然也
    属组合类，但视觉上是独立字形，这里刻意不合并。
    """
    return unicodedata.category(ch) in _COMBINING_CATEGORIES


def _grapheme_atoms(text: str) -> list[str]:
    """把 ``text`` 重组成不可再分的原子单元列表（近似的字素簇）。

    合并规则：
        * 组合类字符（``Mn`` / ``Me``，含 keycap 的 U+20E3）；
        * 变体选择符 VS15 / VS16；
        * Fitzpatrick 肤色修饰符；
        * ZWJ（且 ZWJ 之后的字符一并吞入，故 ``👨‍👩‍👧`` 整体是一个原子）；
        * Regional_Indicator 成对（🇨🇳 是一个原子，🇨🇳🇺🇸 是两个）。
    """
    atoms: list[str] = []
    ri_count = 0   # 当前原子内 Regional_Indicator 的个数（奇数=等待配对）
    forced = False  # 上一字符是 ZWJ，下一字符无条件并入
    for ch in text:
        if not atoms:
            atoms.append(ch)
            ri_count = 1 if _is_regional_indicator(ch) else 0
            continue
        if forced:
            atoms[-1] += ch
            forced = False
            ri_count = 0
            continue
        if ch == _ZWJ:
            atoms[-1] += ch
            forced = True
            continue
        if ch in _VARIATION_SELECTORS or _is_skin_tone(ch) or _is_combining(ch):
            atoms[-1] += ch
            continue
        if _is_regional_indicator(ch):
            # 国旗 = 两个 Regional_Indicator；成对才并入当前原子。
            if ri_count == 1:
                atoms[-1] += ch
                ri_count = 2
            else:
                atoms.append(ch)
                ri_count = 1
            continue
        atoms.append(ch)
        ri_count = 1 if _is_regional_indicator(ch) else 0
    return atoms


# ---------------------------------------------------------------------------
# 断点查找
# ---------------------------------------------------------------------------
def _find_break(window: list[str]) -> int:
    """在原子窗口内从后往前找最优断点，返回断点位置（原子下标）。

    优先级：换行 → 中文句末 → 英文 ``". "`` / ``", "``。返回 ``0`` 表示窗口内
    没有自然断点（调用方退化为硬切）。
    """
    for i in range(len(window) - 1, -1, -1):
        if "\n" in window[i]:
            return i + 1
    for i in range(len(window) - 1, -1, -1):
        if any(mark in window[i] for mark in _CN_SENTENCE_END):
            return i + 1
    for i in range(len(window) - 2, -1, -1):
        if window[i + 1] == " " and window[i].endswith((".", ",")):
            # 在 ". " / ", " **之后**断行（含尾随空格，避免下段以空格开头）
            return i + 2
    return 0


# ---------------------------------------------------------------------------
# 前缀渲染
# ---------------------------------------------------------------------------
def _render(prefix_fmt: str, index: int, total: int) -> str:
    """渲染第 ``index`` / 共 ``total`` 段的前缀。"""
    try:
        return prefix_fmt.format(i=index, n=total)
    except (IndexError, KeyError):
        # 兼容 ``"{} / {}"`` 这类位置占位写法。
        return prefix_fmt.format(index, total)


def _prefix_len(prefix_fmt: str, index: int, total: int) -> int:
    """前缀占用的码点数；单段时不加前缀。"""
    return len(_render(prefix_fmt, index, total)) if total > 1 else 0


# ---------------------------------------------------------------------------
# 切分主体
# ---------------------------------------------------------------------------
def _count_atoms(atoms: list[str], start: int, budget: int) -> int:
    """从 ``atoms[start:]`` 起取尽量多的原子，返回可取的原子个数。

    上限按**码点**计（``max_len`` 的语义），切点按**原子**计（组合序列不可拆），
    两者解耦。若单个原子本身就超过 ``budget``，仍返回 1——没有更小的合法选择，
    此时该段会超出 ``max_len``。用下标游标推进而不是反复切片，避免 O(n²)。
    """
    used = 0
    count = 0
    for i in range(start, len(atoms)):
        size = len(atoms[i])
        if count and used + size > budget:
            break
        used += size
        count += 1
    return count


def _hard_chunks(atoms: list[str], max_len: int) -> list[str]:
    """纯硬切（不加分段前缀）。"""
    out: list[str] = []
    pos = 0
    while pos < len(atoms):
        count = _count_atoms(atoms, pos, max_len)
        out.append("".join(atoms[pos:pos + count]))
        pos += count
    return out


def _cut_bodies(
    atoms: list[str],
    max_len: int,
    prefix_fmt: str,
    total: int,
) -> list[tuple[str, bool]]:
    """按"预算 = ``max_len`` − 前缀长度"切正文段。

    Returns:
        ``(正文, 是否带前缀)`` 列表。某段预算连一个原子都放不下时，该段放弃
        前缀（退化为硬切），标记为 ``False``。
    """
    out: list[tuple[str, bool]] = []
    pos = 0
    index = 1
    end = len(atoms)
    while pos < end:
        budget = max_len - _prefix_len(prefix_fmt, index, total)
        prefixed = budget >= len(atoms[pos])
        if not prefixed:
            # 前缀把预算吃光：放弃前缀做硬切（规格"退化"条）。
            budget = max_len
        count = _count_atoms(atoms, pos, budget)
        cut = _find_break(atoms[pos:pos + count])
        if cut > 0:
            count = cut
        out.append(("".join(atoms[pos:pos + count]), prefixed))
        pos += count
        index += 1
    return out


def _apply_prefix(
    parts: list[tuple[str, bool]],
    max_len: int,
    prefix_fmt: str,
) -> list[str]:
    """给正文段编号前缀；编号使用**真实段数**，保证 ``i`` 永远 ``<= n``。"""
    total = len(parts)
    if total <= 1:
        return [parts[0][0]] if parts else []
    out: list[str] = []
    for index, (body, prefixed) in enumerate(parts, 1):
        if not prefixed:
            out.append(body)  # 已判定放弃前缀
            continue
        prefix = _render(prefix_fmt, index, total)
        body_atoms = _grapheme_atoms(body)
        # 按原子截断，绝不切开组合序列（仅在编号轮用尽的兜底路径触发）。
        count = _count_atoms(body_atoms, 0, max_len - len(prefix))
        out.append(prefix + "".join(body_atoms[:count]))
    return out


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------
def split_text(text: str, max_len: int, *, prefix_fmt: str = DEFAULT_PREFIX_FMT) -> list[str]:
    """把 ``text`` 切成每段码点数不超过 ``max_len`` 的序列。

    Args:
        text: 原始文本（按 Python ``str`` 码点处理）。
        max_len: 单段上限（码点数）。``<= 0`` 表示不切分。
        prefix_fmt: 分段前缀模板，如 ``"（{i}/{n}）"`` 或 ``"[{i}/{n}]"``。

    Returns:
        段列表。短文本原样返回单元素；``text`` 为空串返回 ``[]``；
        ``max_len <= 0`` 返回 ``[text]``。
    """
    if not text:
        return []
    if max_len <= 0:
        return [text]
    if len(text) <= max_len:
        return [text]
    atoms = _grapheme_atoms(text)
    if not atoms:  # 理论不可达（空串已被 len 检查拦下），纯防御
        return []

    # 前缀本身就超限 → 放弃前缀纯硬切。
    if _prefix_len(prefix_fmt, 1, 2) >= max_len:
        return _hard_chunks(atoms, max_len)

    # 第一遍：粗估段数（不考虑前缀）用于预算；之后按真实前缀长度重切。
    total = -(-len(text) // max_len)  # ceil
    parts = _cut_bodies(atoms, max_len, prefix_fmt, total)
    for _ in range(_MAX_PREFIX_PASSES):
        if len(parts) == total:
            break
        total = len(parts)
        parts = _cut_bodies(atoms, max_len, prefix_fmt, total)
    # 无论是否收敛，都用真实段数编号，杜绝"第 3/2 段"。
    return _apply_prefix(parts, max_len, prefix_fmt)
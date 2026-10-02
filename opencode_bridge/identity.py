"""会话标识：统一 ``platform:local_id`` 方案。

## 为什么需要这个模块

迁移前八个平台各写各的前缀：``chat:`` / ``channel:`` / ``room:`` / ``irc:`` /
``twitch:`` / ``nextcloud:``。问题有两个：

1. **不自描述**：``chat:55`` 看不出是哪个平台，``room:!abc`` 也看不出；
   排障时得先知道是哪个适配器才看得懂。
2. **会撞车**：``channel:`` 被 **slack / discord / mattermost 三家共用**。
   而 ``StateStore`` 用 ``conversation_id`` 当**不透明键**存
   ``conversation_id ↔ session_id``。所以一旦两个平台出现相同的 local id，
   两边会共用同一个会话 —— 用户在 A 平台的对话会串到 B 平台。

统一成 ``platform:local_id`` 之后，键自带平台信息，跨平台不可能撞车。

## 迁移期的硬要求：不许静默猜

``channel:`` 是歧义前缀，无法从字符串本身判断它是 slack 还是 discord。
本模块**拒绝猜测**：没有平台线索就抛 :class:`AmbiguousConversationId`。
猜错的后果是把用户映射到**别人的会话**上 —— 这类错误不会报错、只会表现为
"agent 突然记错了上下文"，比直接失败难查得多。

已经写入 ``state.json`` 的旧键用 :func:`normalize` 归一；调用方（适配器）知道
自己是谁，传 ``platform_hint=`` 即可。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Mapping, Optional

__all__ = [
    "ParsedId",
    "InvalidConversationId",
    "AmbiguousConversationId",
    "KNOWN_PLATFORMS",
    "LEGACY_PREFIXES",
    "AMBIGUOUS_LEGACY_PREFIXES",
    "format_id",
    "parse_id",
    "platform_of",
    "local_of",
    "is_valid",
    "normalize",
]

#: 平台键必须是小写标识符 —— 它会直接拼进 ``conversation_id`` 与 JSON 键。
_PLATFORM_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

#: 已知的平台键（含已实现与规划中）。用于把 ``channel:`` 这类**歧义**前缀判为
#: "已是新格式但平台不认识"，从而走 legacy 归一而不是当成新 id。
KNOWN_PLATFORMS: Final[frozenset[str]] = frozenset(
    {
        # 已实现（八个）
        "telegram", "slack", "discord", "matrix", "mattermost",
        "irc", "twitch", "nextcloud",
        # 路线图 B 波次
        "ntfy", "email", "a2a", "qqbot", "homeassistant",
        "feishu", "wecom", "dingtalk", "wechat", "nostr",
    }
)

#: 迁移前各平台用的前缀 → 平台键。值为 ``None`` 表示**歧义**（见下）。
LEGACY_PREFIXES: Final[Mapping[str, Optional[str]]] = {
    "chat": "telegram",          # telegram.py::_conversation_id
    "room": "matrix",            # matrix.py::_conversation_id
    "irc": "irc",                # 已是 platform: 形式
    "twitch": "twitch",          # 已是 platform: 形式
    "nextcloud": "nextcloud",    # 已是 platform: 形式
    # "channel" 三家共用：slack / discord / mattermost —— 无法从字符串判定
    "channel": None,
}

#: 需要 ``platform_hint`` 才能归一的歧义前缀。
AMBIGUOUS_LEGACY_PREFIXES: Final[frozenset[str]] = frozenset(
    key for key, value in LEGACY_PREFIXES.items() if value is None
)


class InvalidConversationId(ValueError):
    """``conversation_id`` 不合法（空、含冒号但平台段非法等）。"""


class AmbiguousConversationId(InvalidConversationId):
    """歧义前缀且未提供 ``platform_hint``。

    与 :class:`InvalidConversationId` 分开，是因为处理方式不同：
    前者"没救"，后者"补个 platform_hint 就行"。
    """


@dataclass(frozen=True)
class ParsedId:
    """``conversation_id`` 的解析结果。"""

    platform: str
    local_id: str
    #: 是否由迁移前的旧格式经 :func:`normalize` 归一而来。
    legacy: bool = False

    def __str__(self) -> str:  # pragma: no cover - 便于日志
        return f"{self.platform}:{self.local_id}"


def _check_platform(platform: str) -> str:
    key = str(platform or "").strip()
    if not _PLATFORM_RE.match(key):
        raise InvalidConversationId(
            f"平台键必须是 1~32 位小写标识符（可含数字与下划线）：{platform!r}"
        )
    return key


def _check_local(local_id: object) -> str:
    """校验 local 段。

    ⚠️ **local 段允许含冒号** —— Matrix 的房间 id 就是 ``!abcDEF:example.org``，
    IRC 的 target 也可能是 ``#chan``。解析一律按**第一个**冒号切分
    （``str.partition``），所以含冒号不会造成歧义。
    """
    if isinstance(local_id, bool) or local_id is None:
        raise InvalidConversationId(f"local_id 非法：{local_id!r}")
    text = str(local_id).strip()
    if not text:
        raise InvalidConversationId("local_id 不能为空")
    return text


def format_id(platform: str, local_id: object) -> str:
    """拼成 ``platform:local_id``。已是该形式的 ``local_id`` 请先剥前缀。"""
    return f"{_check_platform(platform)}:{_check_local(local_id)}"


def _split(cid: object) -> tuple[str, str]:
    if not isinstance(cid, str):
        raise InvalidConversationId(f"conversation_id 必须是字符串：{cid!r}")
    text = cid.strip()
    if not text:
        raise InvalidConversationId("conversation_id 不能为空")
    if ":" not in text:
        raise InvalidConversationId(
            f"缺少平台前缀（应为 platform:local_id）：{cid!r}"
        )
    platform, _, local_id = text.partition(":")
    if not local_id.strip():
        raise InvalidConversationId(f"local_id 为空：{cid!r}")
    return platform.strip(), local_id.strip()


def _is_legacy_alias(platform: str) -> bool:
    """该前缀是否是"指向别的平台的旧别名"（或歧义别名）。

    ``chat`` / ``room`` / ``channel`` 这三个在**语法上都是合法平台名**，所以光看
    字符串无法区分 ``chat:55``（旧 telegram）和"有个叫 chat 的新平台"。
    既然 :data:`LEGACY_PREFIXES` 明确登记了它们，就以登记表为准：这些前缀一律
    判为旧格式，必须走 :func:`normalize`。

    ``irc`` / ``twitch`` / ``nextcloud`` 不算别名 —— 它们映射到自身，登记只是为了
    让 :func:`normalize` 的幂等判断有据可依。
    """
    if platform not in LEGACY_PREFIXES:
        return False
    return LEGACY_PREFIXES[platform] != platform


def parse_id(cid: object) -> ParsedId:
    """解析**新格式** ``platform:local_id``。

    旧格式（含 ``chat:`` / ``room:`` / ``channel:`` 这些别名）一律拒绝并指向
    :func:`normalize` —— 这些前缀在语法上完全合法，若放行就会把旧 id 静默
    当成"某个叫 chat 的平台"处理，那正是本模块要消除的歧义。
    """
    platform, local_id = _split(cid)
    if _is_legacy_alias(platform):
        raise InvalidConversationId(
            f"{cid!r} 是迁移前的旧格式（{platform!r} 是别名，不是平台键）；"
            f"请改用 normalize() 归一"
        )
    return ParsedId(platform=_check_platform(platform), local_id=local_id)


def platform_of(cid: object) -> Optional[str]:
    """取平台段；不合法或旧格式返回 ``None``（不抛 —— 这是"看一眼"的便捷函数）。"""
    try:
        return parse_id(cid).platform
    except InvalidConversationId:
        return None


def local_of(cid: object) -> Optional[str]:
    """取 local 段；不合法或旧格式返回 ``None``。"""
    try:
        return parse_id(cid).local_id
    except InvalidConversationId:
        return None


def is_valid(cid: object) -> bool:
    """是否已是合法的新格式（**不**做旧格式归一）。"""
    try:
        parse_id(cid)
    except InvalidConversationId:
        return False
    return True


def normalize(cid: object, *, platform_hint: Optional[str] = None) -> str:
    """把任意历史格式归一成 ``platform:local_id``。

    - 已是新格式 → 原样返回（幂等）
    - 旧格式且前缀唯一（``chat:`` / ``room:``）→ 直接归一
    - 旧格式且前缀歧义（``channel:``）→ **必须**给 ``platform_hint``，
      否则抛 :class:`AmbiguousConversationId`

    归一时**不校验平台是否已实现** —— 未知平台键只要语法合法就放行，
    这样先换 id、再补适配器的分步迁移是可行的。
    """
    platform, local_id = _split(cid)

    # 已是新格式：平台段本身合法即可（不要求 KNOWN_PLATFORMS 里有它）
    if platform not in LEGACY_PREFIXES and _PLATFORM_RE.match(platform):
        return f"{platform}:{local_id}"

    resolved = LEGACY_PREFIXES.get(platform, "missing")
    if resolved == "missing":
        # 前缀不在 legacy 表里，也不是合法平台键 —— 原样保留更安全？
        # 不：静默保留会让排障时看不出问题所在。判为"已是新格式但平台名非法"。
        raise InvalidConversationId(
            f"前缀既不是已知平台也不是已知 legacy 前缀：{cid!r}"
        )
    if resolved is None:
        if not platform_hint:
            raise AmbiguousConversationId(
                f"前缀 {platform!r} 被多个平台共用，无法从 {cid!r} 判断来源；"
                f"请传 platform_hint（调用方是适配器，知道自己是谁）"
            )
        hint = _check_platform(platform_hint)
        return f"{hint}:{local_id}"
    return f"{_check_platform(resolved)}:{local_id}"

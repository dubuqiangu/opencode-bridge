"""入站白名单的**解析与诊断**：`allowed_chat_ids` 的键名歧义、键冲突、空=全开。

## 为什么单独一个模块

这摊事有两个消费者，而它们以前各写各的（或者干脆没写）：

* :meth:`opencode_bridge.adapters.base.Adapter._init_access` —— 真正决定谁能进来；
* ``--setup --json`` / ``--status``（:mod:`opencode_bridge.__main__`）—— 要**如实**
  回答"这个平台现在是不是谁都能驱动"。

把它做成**一个纯函数**是因为"某个配置解析成什么"必须只有**一个**答案：状态视图说
"有白名单"而闸门说"全开"，是比没有状态视图更坏的结果（用户信了它）。

为什么不放进 :mod:`opencode_bridge.adapters.base`：那个文件已经是「适配器 ABC +
注册表 + HTTP 错误分类」三件事，405 物理行，再塞一份配置诊断会顶破 §5 的 ~400 行。

## ⚠️ 承重的语义：**空 = 全开**

:meth:`opencode_bridge.adapters.base.Adapter.admits` 的判定是"空集合 ⇒ 放行一切"。
这不是疏忽，而是一个**被文档化、被示例配置、被安装脚本共同固化**的默认：

* ``config.example.json`` 抄的是 ``"allowed_chat_ids": []``；
* ``plugin/index.ts`` / ``install.ps1`` / ``install.sh`` 把那份示例原样落到用户配置里。

于是**每个自助安装出来的桥接开局就是全开的**。把默认翻过来是产品决策，**不在本
模块**；本模块负责的是另一半 —— 让人**看得见**它，以及在配置写错时说清楚实际生效的是
哪个键、解析出几项。

## ⚠️ 为什么键冲突只"检测并上报"、不"改判"

三种键名按 :data:`ALLOWLIST_CONFIG_KEYS` 的顺序**先出现者胜**。这条优先级是既有
行为，任何人都可能依赖它，所以**不许静默改**（§8：悄悄换掉一个已被依赖的行为，
比保留一个已知缺陷更难查）。

但它有一个真实的洞::

    {"allowed_chat_ids": [], "allowed_chats": [42]}   ->  空集合  ->  全开

用户明明写了一个**非空**白名单，却拿到全开，且**没有任何提示**。根因不是"优先级
选错了"，而是**没人告诉他哪个键赢了**。所以这里做两件事：保留原判定，同时把
:attr:`AllowlistResolution.conflict` 与实际生效值一起报出来 —— 一个写错的配置应该
**告诉用户**，而不是被替他猜掉（§8.3「警惕恢复没记录过的信息」的同一条纪律）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Final, Mapping, Optional

logger = logging.getLogger("opencode_bridge.allowlist")

__all__ = [
    "ALLOWLIST_CONFIG_KEYS",
    "AllowlistConflict",
    "AllowlistResolution",
    "resolve_allowlist",
    "describe_wide_open",
    "warn_if_wide_open",
    "warn_if_conflicting_keys",
]

#: 授权键的**接受顺序**，先出现者胜。改这个顺序就是改行为 —— 见模块 docstring。
ALLOWLIST_CONFIG_KEYS: Final[tuple[str, ...]] = (
    "allowed_chat_ids",
    "allowed_chats",
    "allowlist",
)


@dataclass(frozen=True)
class AllowlistConflict:
    """多个授权键同时出现且**解析结果互不相同**。

    只在"结果有分歧"时成立：两个键写的是同一份列表时，行为毫无歧义，报冲突只是
制造噪音。但那些键仍会出现在 :attr:`AllowlistResolution.present_keys` 里 ——
"你写了两个同义键名"这件事本身对排障有用。

    :ivar present_keys: 配置里**实际出现**的授权键，按 :data:`ALLOWLIST_CONFIG_KEYS`
        的优先级排列。
    :ivar resolved_key: 实际生效的那个键（``None`` = 一个都没写）。
    :ivar resolved_count: 生效的那个键解析出**几项**（``0`` = 全开）。
    :ivar shadowed_counts: 被忽略的键各自解析出几项，键名 → 项数。
    """

    present_keys: tuple[str, ...]
    resolved_key: Optional[str]
    resolved_count: int
    shadowed_counts: tuple[tuple[str, int], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """机器可读形态（``--setup --json`` 消费）。"""
        return {
            "keys": list(self.present_keys),
            "resolved_key": self.resolved_key,
            "resolved_count": self.resolved_count,
            "shadowed": [
                {"key": key, "entry_count": count}
                for key, count in self.shadowed_counts
            ],
            "detail": self.describe(),
        }

    def describe(self) -> str:
        """人话说明：哪个键赢了、几项、被忽略的是谁。

        **必须说出实际生效值**，否则用户读完仍然不知道自己在被什么驱动。
        """
        winner = (
            f"{self.resolved_key}（{self.resolved_count} 项）"
            if self.resolved_key is not None
            else "（一个授权键都没写）"
        )
        ignored = "、".join(
            f"{key}（{count} 项）" for key, count in self.shadowed_counts
        )
        lines = [
            f"授权配置里同时出现 {len(self.present_keys)} 个键："
            f"{'、'.join(self.present_keys)}。",
            f"按「先出现者胜」实际生效的是 {winner}"
            + (f"；被忽略的是 {ignored}（**没有**被合并进去）。" if ignored else "。"),
        ]
        if self.resolved_count == 0:
            lines.append(
                "⚠️ 0 项 = **全开**：任何能联系到 bot 的人都能驱动它。"
                "若你要的是被忽略的那些项，请只保留一个键（把值合并进去）。"
            )
        return "".join(lines)


@dataclass(frozen=True)
class AllowlistResolution:
    """一份适配器配置解析出来的授权状态。

    :ivar entries: 生效的白名单条目（已 strip、已转字符串）。
    :ivar present_keys: 配置里出现过的授权键，按优先级排列（可能为空）。
    :ivar resolved_key: 生效的键名；``None`` = 三个键都没写。
    :ivar conflict: 有分歧时的冲突记录；``None`` = 无冲突（**包括**两个键值相同）。
    """

    entries: frozenset[str] = frozenset()
    present_keys: tuple[str, ...] = ()
    resolved_key: Optional[str] = None
    conflict: Optional[AllowlistConflict] = None
    #: 每个出现过的键各自解析出几项（含生效的那个），键名 → 项数。
    per_key_counts: tuple[tuple[str, int], ...] = field(default=())

    @property
    def accepts_any_sender(self) -> bool:
        """解析结果是不是"谁都放行"。

        ⚠️ 这是**闸门的真实行为**，不是"配置写没写白名单"：写 ``[]``、写 ``null``、
        写 ``["", "  "]``、一个键都不写 —— 四种都落到这里，而它们在用户眼里是
        四种不同的配置。
        """
        return not self.entries


def _entries_of(raw: object) -> set[str]:
    """把一个授权键的值解析成条目集合。

    ⛔ 这段解析**逐字照搬** :meth:`opencode_bridge.adapters.base.Adapter._init_access`
    原有实现（含 ``False`` / ``0`` 这类"不是列表但也不为空"的标量边界）。改它就等于
    改授权行为，而本任务不动行为 —— 两处一旦分叉，"谁能进来"就取决于调用顺序了。
    """
    items: list[object] = []
    if isinstance(raw, (list, tuple, set)):
        items = list(raw)
    elif raw not in (None, ""):
        items = [raw]
    return {str(item).strip() for item in items if str(item).strip()}


def resolve_allowlist(config: Mapping[str, Any] | None) -> AllowlistResolution:
    """解析一份适配器配置的授权状态。纯函数，不读环境、不打日志、不抛。

    先出现者胜的优先级**保持不变**；本函数额外把"谁赢了、赢了几项"算出来，让调用方
    能**说出来**而不是替用户猜（见模块 docstring）。
    """
    entries_by_key: dict[str, set[str]] = {}
    present: list[str] = []
    for key in ALLOWLIST_CONFIG_KEYS:
        if isinstance(config, Mapping) and key in config:
            present.append(key)
            entries_by_key[key] = _entries_of(config.get(key))

    resolved_key = present[0] if present else None
    resolved = entries_by_key.get(resolved_key, set()) if resolved_key else set()
    shadowed = tuple((key, len(entries_by_key[key])) for key in present[1:])

    conflict: Optional[AllowlistConflict] = None
    if present and any(
        entries_by_key[key] != resolved for key in present[1:]
    ):
        conflict = AllowlistConflict(
            present_keys=tuple(present),
            resolved_key=resolved_key,
            resolved_count=len(resolved),
            shadowed_counts=shadowed,
        )

    return AllowlistResolution(
        entries=frozenset(resolved),
        present_keys=tuple(present),
        resolved_key=resolved_key,
        conflict=conflict,
        per_key_counts=tuple((key, len(entries_by_key[key])) for key in present),
    )


def describe_wide_open(platform_label: str, resolution: AllowlistResolution) -> str:
    """空=全开时那句必须让人听见的话。"""
    return (
        f"{platform_label}: 授权白名单为空 ⇒ **任何能联系到 bot 的人都能驱动它**"
        f"（agent 会以你的本地权限执行）。"
        f"请把 {ALLOWLIST_CONFIG_KEYS[0]} 填成你信任的 chat / 频道 id 列表。"
        f"（是否要把「空 = 全开」改成「空 = 全禁」是产品决策；本条只负责让现状可见。）"
    )


def warn_if_wide_open(
    platform_label: str,
    resolution: AllowlistResolution,
    *,
    has_credentials: bool,
) -> None:
    """已配置（凭据齐了、能收消息）却空=全开 ⇒ 记一条 warning。

    只在**配好凭据**时才喊：没配凭据的适配器根本收不到消息，说"谁都能驱动"是
    假话。喊了也只走日志（不打断启动）—— 把默认改成拒绝是产品决策，而本仓库的
    纪律是**说清楚**，不是**替用户决定**（``a2a._resolve_bind_host``、
    ``email._security`` 同策）。
    """
    if not (has_credentials and resolution.accepts_any_sender):
        return
    logger.warning(describe_wide_open(platform_label, resolution))


def warn_if_conflicting_keys(platform_label: str, resolution: AllowlistResolution) -> None:
    """键冲突 ⇒ 说清楚实际生效的是哪个键、几项。"""
    if resolution.conflict is None:
        return
    logger.warning("%s: %s", platform_label, resolution.conflict.describe())

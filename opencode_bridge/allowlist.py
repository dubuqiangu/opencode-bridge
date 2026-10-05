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

## ⚠️ 承重的语义：**空清单的含义按 ``config_version`` 分两套**

:meth:`opencode_bridge.adapters.base.Adapter.admits` 的判定式一直是"空集合 ⇒
**无条件下**的判定"，而"无条件"指向哪一边由 :mod:`opencode_bridge.pairing` 的
:func:`~opencode_bridge.pairing.empty_allowlist_is_open` 决定：

* 配置里**没有** ``config_version``（或 < 2）⇒ 沿用旧的「**空 = 全开**」——
  这不是疏忽，而是一个**被文档化、被示例配置、被安装脚本共同固化**的默认：
  ``config.example.json`` 抄的是 ``"allowed_chat_ids": []``，
  ``plugin/index.ts`` / ``install.ps1`` / ``install.sh`` 把那份示例原样落盘。
  保持它是为了**不让任何既有用户突然被关在门外**。
* ``config_version >= 2`` ⇒ 「**空 = 全拒**」。

⚠️ 这个版本分界是**整个发布方式的支点**，判定只有
:func:`~opencode_bridge.pairing.empty_allowlist_is_open` 一处。闸门、状态视图、
``--status`` 都读它 —— 两处各判一次就会出现"状态说一种、闸门做另一种"。

于是「空 = 全拒」对**已配对过**的用户**立即生效**（``--pair`` 那次写盘会顺手写上
``config_version: 2``），没配对的用户仍是开放语义，但**看得见预告**。
**任何人都不会被困死。**

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

from .pairing import empty_allowlist_is_open

logger = logging.getLogger("opencode_bridge.allowlist")

__all__ = [
    "ALLOWLIST_CONFIG_KEYS",
    "AllowlistConflict",
    "AllowlistResolution",
    "resolve_allowlist",
    "describe_no_allowlist",
    "warn_if_no_allowlist",
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
            # ⚠️ "0 项" 的后果**取决于 config_version**：旧文件仍然全开，新文件全拒。
            # 所以这里**不**断言是哪一种，而是把两种都摆出来让用户自己对上自己的文件
            # —— 写死一种会让另一个版本的用户读到假话，而这条文案的作用恰恰是
            # "用户读到的 == 闸门的真实行为"。
            lines.append(
                "⚠️ 0 项：若配置里**没有** `config_version`（或 < 2），"
                "空的含义是**全开** —— 任何能联系到 bot 的人都能驱动它；"
                "若 `config_version >= 2`，空的含义是**全拒**，该平台收不到任何消息，"
                "需要在 bot 内发 /pair 配对。"
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
    def admits_nobody(self) -> bool:
        """白名单为空 ⇒ **谁都不放行**。

        ⚠️ 判定式 ``not self.entries`` **一个字没动**，改的只是它的名字与解读。

        它一直回答的是「**闸门在无显名单时的行为是无条件的**」，而翻转后「无条件」
        的方向变了：**旧文件（无 ``config_version``）仍然放行一切，新文件不放行**。
        变的只是"无条件"指向哪一边，**不是**这条判定式本身。

        ⛔ **为什么不能保留旧名（``accepts_any_sender``）而只改语义**：翻转之后它
        变成**恒为 False** —— 非空清单从来不"放行所有人" —— 于是这个字段变死，
        而仓库外的工具（``bridge_setup`` 读 ``--setup --json``）还在读它。
        ⛔ **也不能改名顺手改判定式**：判定式**不用改**，改了才是 bug
        （把"清单为空"判成"放行全部"或"谁都不放行"，会让非空清单的语义塌掉）。

        配套：:meth:`gate_admits_everyone` 才是**闸门的真实行为**，因为它把
        :func:`opencode_bridge.pairing.empty_allowlist_is_open` 也算进去了。
        """
        return not self.entries

    def gate_admits_everyone(self, config_version: object) -> bool:
        """**这个配置下**，闸门会不会放行任何 principal。

        这是 :meth:`~opencode_bridge.adapters.base.Adapter.admits` 的真实答案。
        状态视图与闸门读**同一个函数** —— 两处各答一次就会出现"状态说有限白名单、
        闸门说全开"，那比没有状态视图更坏（用户信了它）。
        """
        return self.admits_nobody and empty_allowlist_is_open(config_version)


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


def describe_no_allowlist(
    platform_label: str, resolution: AllowlistResolution, config_version: object
) -> str:
    """白名单为空时必须让人听见的话。**两条文案，按配置版本分开。**

    ⚠️ 旧函数 :func:`describe_wide_open` / :func:`warn_if_wide_open` 是**删掉**的，
    不是改文案的：翻转之后「清单为空」恰恰是**最安全**的状态，而原函数会为它发一条
    **安全告警**（"谁都能驱动它"）—— 那是**对安全状态报警**，比不报警更坏：
    它教会用户忽略这条日志，于是下次真的全开时他也不会当真。
    """
    return (
        f"{platform_label}: 授权白名单为空。"
        + (
            f"你的配置里没有 `config_version`（或 < 2）⇒ 沿用**旧的「空 = 全开」**"
            f"语义：**任何能联系到 bot 的人都能驱动它**（agent 会以你的本地权限执行）。"
            f"请把 {ALLOWLIST_CONFIG_KEYS[0]} 填成你信任的 chat / 频道 id 列表。"
            f"（下一版起此处改为「空 = 全拒」；届时用 bot 内的 /pair 一步授权，"
            f"不必手改本文件。）"
            if empty_allowlist_is_open(config_version)
            else (
                f"你的配置是 `config_version >= 2` ⇒「空 = 全拒」：**谁都不能驱动它**，"
                f"该平台收不到任何消息。这是最安全的状态，"
                f"在 bot 内发 /pair 即可授权你那个会话。"
            )
        )
    )


def warn_if_no_allowlist(
    platform_label: str,
    resolution: AllowlistResolution,
    *,
    has_credentials: bool,
    config_version: object = 0,
) -> None:
    """已配置（凭据齐了、能收消息）却白名单为空 ⇒ **说清现状**。

    只在**配好凭据**时才喊：没配凭据的适配器根本收不到消息，"谁能驱动它"对它是
    假话。喊了也只走日志（不打断启动）—— 本仓库的纪律是**说清楚**，不是**替用户
    决定**（``a2a._resolve_bind_host``、``email._security`` 同策）。

    ⚠️ 新语义下这是**信息**（``logger.info``）而不是安全告警：空清单正是"拒绝一切"，
    对它发 warning 是**对安全状态报警**。判据仍是同一条
    :meth:`~opencode_bridge.allowlist.AllowlistResolution.admits_nobody`。
    """
    if not (has_credentials and resolution.admits_nobody):
        return
    message = describe_no_allowlist(platform_label, resolution, config_version)
    if empty_allowlist_is_open(config_version):
        logger.warning(message)
    else:
        logger.info(message)


def warn_if_conflicting_keys(platform_label: str, resolution: AllowlistResolution) -> None:
    """键冲突 ⇒ 说清楚实际生效的是哪个键、几项。"""
    if resolution.conflict is None:
        return
    logger.warning("%s: %s", platform_label, resolution.conflict.describe())

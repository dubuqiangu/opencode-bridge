"""会话键的**歧义归属**：``channel:`` 这类多家共用的旧前缀在读取时怎么回退（G5 第 2 步）。

## 问题是什么

``chat:`` / ``room:`` 这类旧前缀能归一，是因为 :data:`identity.LEGACY_PREFIXES`
登记了唯一指向；而 ``channel:`` 的值是 ``None`` —— slack / discord / mattermost
**三家共用**，光看字符串判不出来源。而归属**从来没有被持久化过**：真实
``state.json`` 里只有 ``{"sessions": …, "meta": …}``，``meta`` 的键也只有
``directory``（``/cd`` 写的）。所以"这个 ``channel:`` 键原属谁"在盘上**不存在**，
任何想把它推出来的方案都是在猜。

## 方案：不相交划分（disjointness partition）

不猜"历史上挂过谁"，而是让**每个平台自己声明它那一套 local id 的文法**，
并且要求三家文法**两两不相交**：

============  =========================  ====================================
平台          文法（各自在适配器里声明）   承重的那一条
============  =========================  ====================================
slack         ``^[A-Z][A-Z0-9]{5,}$``     首字符是大写字母
discord       ``^[0-9]{17,20}$``          纯数字，且不超过 20 位
mattermost    ``^[a-z0-9]{26}$``          恰好 26 位
============  =========================  ====================================

于是"这个 local id 属不属于我"是**可以确定的判断**：slack 那格首字符必须是大写
字母（discord 全是数字、mattermost 全小写）；mattermost 那格恰好 26 位，所以
26 位**纯数字**归它、不可能归 discord（后者上限 20 位）。声明位置是**拥有那个
平台的适配器**（描述的是那个平台的 API），判定入口是
:meth:`~opencode_bridge.adapters.base.Adapter.owns_local_id`。

带来的行为（对比被否掉的"唯一申报者才回退"方案）：

* **三家同时挂载、三家各自持有 ``channel:`` 会话 ⇒ 全部存活，互不串台。**
  旧方案在这种情况下直接放弃回退，把三份历史会话全丢了。
* **只挂 slack 时，discord 的 ``channel:1234…`` 不会被 slack 认领。**
  旧方案会认领 —— 而"用户曾经同时跑过 slack 和 discord、后来删掉 discord"
  这个前提**盘上无从验证**，那是旧方案真正的洞。
* 曾同时跑过两家、后来删掉一家的用户，剩下那家也**不会**认领另一家的键。

## 三条纪律

1. **只在查询方自己的文法上判断。** :meth:`ConversationState.legacy_candidate_keys`
   必须拿到"发起查询的平台"（``Inbound.platform`` / 收件箱行里的 ``platform``），
   然后**只**去问那一个适配器。它**绝不**遍历已挂载适配器去做仲裁 —— 拿别人的
   文法来挑"谁认领"，就退化成了猜。
2. **只读，永不改盘。** 歧义前缀**永不迁移**（:mod:`opencode_bridge.state` 对它
   只捕获、不归一），所以 ``channel:`` 键逐字节留在原处；本模块只是让读取多试
   一个候选键。写入**一律只落新键**。
3. **"不属于我"是常态，不是错误。** 读不到就当没有 —— 代价只是那个会话从新的
   开始，而错接的代价是用户被接到**别的平台**的会话上且不报错。
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Sequence

from . import identity
from .state import StateStore

__all__ = [
    "AMBIGUOUS_LEGACY_PREFIX",
    "ConversationState",
]

#: 迁移前 slack / discord / mattermost **三家共用**的 ``conversation_id`` 前缀。
#:
#: 刻意写成**字面量**：它就是"要判定归属的那个前缀"，而
#: :data:`identity.LEGACY_PREFIXES` 里 ``channel`` 的值是 ``None``（= 歧义），
#: 拼不出这个字符串。
AMBIGUOUS_LEGACY_PREFIX = "channel:"


class ConversationState:
    """会话级状态读写门面：读取时按**归属划分**回退到旧歧义键。

    与 :class:`~opencode_bridge.state.StateStore` 的分工：

    * ``StateStore`` 管**盘上**的键（迁移、原子落盘、备份），对歧义前缀只捕获不归一；
    * 本类管"这条会话在**本平台**名下可能还存在于哪个旧键下"，即模块 docstring
      里的那条不相交划分。

    写入**一律只落新键**：改键名是迁移，而歧义前缀**永不迁移**（见模块 docstring）。
    旧键因此留在盘上不动 —— 每次读取对"属于自己的形状"多试一次即可，不属于就
    当没有（旧键是惰性残留，不是垃圾：它还是用户唯一的回滚路径）。
    """

    def __init__(
        self,
        store: StateStore,
        mounted_adapters: Callable[[], Sequence[object]],
    ) -> None:
        """
        :param store: 底层存储（本类**不**缓存任何状态，一切以它为准）。
        :param mounted_adapters: 返回**当前已挂载**适配器的可调用对象。
            必须是惰性的：适配器在 :class:`~opencode_bridge.core.BridgeCore` 构造
            **之后**才 ``attach``。它只用来**按名字取出查询方那一个**适配器，
            绝不用于遍历仲裁（纪律 1）。
        """
        self._store = store
        self._mounted_adapters = mounted_adapters

    # ------------------------------------------------------------------
    # 归属划分
    # ------------------------------------------------------------------
    def legacy_candidate_keys(
        self, conversation_id: str, *, platform: str
    ) -> tuple[str, ...]:
        """这条会话在**提问平台**名下可能要试的旧歧义键（0 个或 1 个）。

        :param conversation_id: **新格式**的 ``platform:local_id``。
        :param platform: 发起这次读取的平台（= 产生这条入站消息的适配器，
            即 :attr:`~opencode_bridge.hooks.Inbound.platform`）。必填、且
            刻意不给默认值：漏传就等于让某一层去"猜谁在问"，而那正是本模块
            要消除的东西。
        """
        asking = str(platform or "").strip()
        if not asking:
            return ()
        claimed = identity.platform_of(conversation_id)
        if claimed is None:
            # 旧格式键（如 ``channel:C1``）本身就是答案，按精确键读即可，无需回退。
            return ()
        if claimed != asking:
            # ``conversation_id`` 声称的平台与提问方不一致 = 调用点传错了。
            # 宁可什么都不读，也不要在这种输入上认领任何旧键。
            return ()
        local_id = identity.local_of(conversation_id)
        if local_id is None:  # pragma: no cover - platform_of 过了这里必然有 local
            return ()
        owner = self._adapter_named(asking)
        if owner is None:
            return ()
        # 前缀声明只用来确认"历史上那个前缀确实是 channel:"，不参与仲裁。
        if getattr(owner, "legacy_conversation_prefix", None) != AMBIGUOUS_LEGACY_PREFIX:
            return ()
        # ⚠️ 承重的一步：只看**提问方自己**声明的文法。
        if not owner.owns_local_id(local_id):
            return ()
        return (f"{AMBIGUOUS_LEGACY_PREFIX}{local_id}",)

    def _adapter_named(self, platform: str) -> Optional[object]:
        """按名字取**那一个**适配器（取不到就是"没人认领"）。

        按名字取，不按 local id 试：后者等于让三家抢同一个键。
        """
        for adapter in self._mounted_adapters():
            if str(getattr(adapter, "name", "") or "") == platform:
                return adapter
        return None

    def _first_hit(
        self, conversation_id: str, platform: str,
        read: Callable[[str], Any], default: Any = None,
    ) -> Any:
        """精确键优先；未命中才按 :meth:`legacy_candidate_keys` 再试一次。"""
        found = read(conversation_id)
        if found is not None:
            return found
        for candidate in self.legacy_candidate_keys(
            conversation_id, platform=platform
        ):
            found = read(candidate)
            if found is not None:
                return found
        return default

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------
    def get_session(self, conversation_id: str, *, platform: str) -> Optional[str]:
        return self._first_hit(
            conversation_id, platform, self._store.get_session
        )

    def get_meta(
        self, conversation_id: str, key: str, default: Any = None, *,
        platform: str,
    ) -> Any:
        return self._first_hit(
            conversation_id,
            platform,
            lambda candidate: self._store.get_meta(candidate, key, None),
            default,
        )

    # ------------------------------------------------------------------
    # 写（只落新键：歧义旧前缀永不迁移，见模块 docstring）
    # ------------------------------------------------------------------
    def set_session(self, conversation_id: str, session_id: str) -> None:
        self._store.set_session(conversation_id, session_id)

    def set_meta(self, conversation_id: str, key: str, value: Any) -> None:
        self._store.set_meta(conversation_id, key, value)

    def drop_session(self, conversation_id: str, *, platform: str) -> None:
        """删掉这条会话的映射 —— 候选键（含旧歧义键）**逐个删**。

        旧键必须一起删：否则 ``/new`` 之后新键没了、旧键还在，下次读取又认领回那个
        **已经在服务端删掉**的会话 id，用户会看到"agent 对着一个不存在的会话说话"。

        只删**本平台文法覆盖得到**的那个旧键 —— 别人的键一个字都不许碰。
        """
        for candidate in (
            conversation_id,
            *self.legacy_candidate_keys(conversation_id, platform=platform),
        ):
            self._store.drop_session(candidate)

"""适配器归属：这个会话属于哪个适配器，以及哪些适配器挂着。

这一整块原先是 :class:`~opencode_bridge.core.BridgeCore` 的六个方法。搬出来是因为
那个类已经远超 AGENTS.md §5.1 的阈值（方法数 ~15+ / 类自身代码行 ~250+），而归属
判定是里面**自成一块**的一坨：入站时种下的准确映射、四层查找顺序、以及那条
"按 conversation_id 前缀猜"的历史兜底。

搬的时候**连私有状态一起搬**（§5.1 首选的那种形态）：``_adapters`` /
``_adapter_by_name``（:meth:`AdapterRouter.attach` 是唯一的写者）与
``_conv_adapter`` 都只有这一块碰。剩下的依赖只有一把共用的 ``lock`` ——
注入的是**同一个对象**，所以互斥关系一点没变。

⚠️ :meth:`AdapterRouter._route_by_prefix` 里的 ``channel:`` 启发式**不许**和
:mod:`opencode_bridge.conversation_keys` 读侧的归属文法合并成"一份真相"。两者解决的
不是同一个问题：这里是"我该往哪个适配器**发消息**"（前缀就是目标的一部分），那边是
"这个历史键**归谁**"（必须靠不相交的文法判定，因为键本身不记录来源）。合并会把一条
兜底猜测升格成认领规则 —— 那正是 A1 迁移决定不走的路。
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from .adapters import Adapter
from .identity import LEGACY_PREFIXES

__all__ = ["AdapterRouter"]

logger = logging.getLogger("opencode_bridge.adapter_router")


class AdapterRouter:
    """Which adapter owns a conversation -- and which adapters exist at all."""

    def __init__(self, *, lock: threading.RLock) -> None:
        """``lock`` 由 core 注入，本类不自己建 —— core 那边还在用同一把锁。"""
        self._lock = lock
        #: 挂载顺序**有意义**：``_route_by_prefix`` 的最后兜底是 ``_adapters[0]``，
        #: 而这个顺序就是配置文件里的字典顺序（用户可控）。
        self._adapters: list[Adapter] = []
        self._adapter_by_name: dict[str, Adapter] = {}
        #: conversation_id -> adapter name (learned on first inbound)
        self._conv_adapter: dict[str, str] = {}

    # ------------------------------------------------------------------
    # 挂载表
    # ------------------------------------------------------------------
    @property
    def adapters(self) -> tuple[Adapter, ...]:
        with self._lock:
            return tuple(self._adapters)

    def attach(self, adapter: Adapter) -> None:
        """Attach one messaging adapter (may be called multiple times)."""
        with self._lock:
            if any(a is adapter for a in self._adapters):
                return
            self._adapters.append(adapter)
            self._adapter_by_name.setdefault(adapter.name, adapter)
        logger.info("adapter attached: %s", adapter.name)

    # ------------------------------------------------------------------
    # 归属判定
    # ------------------------------------------------------------------
    def _route_by_prefix(
        self, conversation_id: str, adapters: list[Adapter]
    ) -> Adapter:
        """按 ``conversation_id`` 的平台段猜适配器 —— **兜底路径，不是主路径**。

        ⚠️ **主路径是 :meth:`remember_platform`**：入站时产生这条消息的适配器
        自己就知道自己是谁（``Inbound.platform``），那里没有不确定性。这里只在
        "这个目标从未收到过入站消息"（例如让agent 主动往某个 chat 发消息）时才会被走到。

        **为什么这里必须覆盖全部前缀**：A1 迁移打掉了"按调用线程判断归属"那层保护
        —— 已迁移的适配器不再持有 ``_thread``，:meth:`adapter_for` 里的线程匹配恒不命中。
        而本方法此前只硬编码了 ``chat:`` 与 ``channel:`` 两种，其余一律
        ``adapters[0]``；``attach()`` 的顺序就是**配置文件里的字典顺序**（用户可控），
        于是多平台用户可能把回复发到**错误的平台** —— 且不报错。
        """
        def named(name: str) -> Adapter | None:
            for adapter in adapters:
                if adapter.name == name:
                    return adapter
            return None

        cid = str(conversation_id or "")
        head, sep, rest = cid.partition(":")

        # 旧别名以 identity 的登记表为准，避免这里变成第二份真相。
        # 值为 None 表示**歧义**前缀（``channel:`` 被 slack/discord/mattermost 共用）。
        legacy = LEGACY_PREFIXES.get(head, "missing")
        if legacy is None:
            # 歧义前缀只能启发式：Slack id 以 C/D 开头、Discord 是纯数字、
            # Mattermost 是 26 位 base32。**这仍然可能猜错** —— 真正的确定性来自
            # :meth:`remember_platform`，这里只是没有别的办法时的兜底。
            #
            # ⚠️ 别被"我们知道各家文法不相交"带偏：那个划分只许用在
            # :mod:`opencode_bridge.conversation_keys` 的**读侧归属判定**上，
            # **绝不许**拿它来路由出站消息（那是按前缀猜目标，不是认领键）。
            if rest.isdigit():
                return named("discord") or named("slack") or adapters[0]
            return named("slack") or named("discord") or adapters[0]
        if legacy != "missing" and legacy != head:
            # 真正的别名（``chat``→telegram、``room``→matrix）
            hit = named(legacy)
            if hit is not None:
                return hit

        # 新格式 ``platform:local_id``（以及映射到自身的 irc/twitch/nextcloud）：
        # **平台段本身就是答案**，不需要任何猜测。
        if sep:
            hit = named(head)
            if hit is not None:
                return hit
        return adapters[0]

    def remember_platform(self, conversation_id: str, platform: str) -> None:
        """记下"这个会话属于哪个适配器" —— 用的是**准确**信息。

        产生这条入站消息的适配器就是它自己（``Inbound.platform``），所以这一步
        没有不确定性。对比 :meth:`_route_by_prefix` 的前缀猜测：猜错的后果是把回复
        发到**另一个平台**，且不报错、只表现为"用户发现回复跑错了地方"。

        A1 迁移之前这里还有第二个来源 —— "调用线程是否等于某适配器的 ``_thread``"。
        迁移后该字段恒为 ``None``，那条路失效了，所以必须靠本方法兜住。
        """
        name = str(platform or "").strip()
        if not name or not conversation_id:
            return
        with self._lock:
            self._conv_adapter[conversation_id] = name

    def asking_platform(self, conversation_id: str) -> str:
        """这条会话的**提问平台** —— 歧义旧键归属划定的唯一输入。

        来源是 :meth:`remember_platform`：入站时由产生这条消息的适配器**自己**报上
        来的 :attr:`~opencode_bridge.hooks.Inbound.platform`。这是准确信息，
        :meth:`adapter_for` 里那条"前缀猜出来的"兜底只会写进**旧格式** id 的条目，
        而旧格式 id 在读侧根本不会走到归属判定（它按精确键读）。

        从未收到过入站消息的会话（agent 主动外发）返回空串 —— 那时没有"提问方"，
        也就没有平台有权认领旧键，于是 :class:`ConversationState` 不做任何回退。

        调用点大多会**显式**把平台传进来（收件箱行 / 命令的适配器），本方法是那些
        拿不到上下文的路径（:class:`~opencode_bridge.session_model.SessionModelCommand`
        与 :class:`~opencode_bridge.session_registry.SessionRegistry` 通过注入的
        callable 回调进来）的兜底。
        """
        with self._lock:
            return self._conv_adapter.get(str(conversation_id or ""), "")

    def adapter_for(self, conversation_id: str) -> Optional[Adapter]:
        """Pick the adapter that owns ``conversation_id``.

        Order: remembered mapping -> calling polling thread -> conversation
        id prefix -> first attached adapter.
        """
        with self._lock:
            remembered = self._conv_adapter.get(conversation_id)
            if remembered:
                adapter = self._adapter_by_name.get(remembered)
                if adapter is not None:
                    return adapter
            adapters = list(self._adapters)
        if not adapters:
            return None

        chosen: Optional[Adapter] = None
        current = threading.current_thread()
        for adapter in adapters:
            # Lane B keeps its poller thread in ``_thread``; matching the
            # thread tells us which adapter invoked the hook.
            poller = getattr(adapter, "_thread", None)
            if poller is not None and poller is current:
                chosen = adapter
                break
        if chosen is None:
            chosen = self._route_by_prefix(conversation_id, adapters)
        with self._lock:
            self._conv_adapter[conversation_id] = chosen.name
        return chosen

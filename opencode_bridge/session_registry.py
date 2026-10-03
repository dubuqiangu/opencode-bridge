"""会话映射：这条会话对应哪个 opencode session，没有就建一个。

这一整块原先是 :class:`~opencode_bridge.core.BridgeCore` 的两个方法。搬出来是因为
那个类已经远超 AGENTS.md §5.1 的阈值（方法数 ~15+ / 类自身代码行 ~250+），而
"取或建会话"是里面**自成一块**的一坨：解析工作目录、按权限模式组装建会话参数、
400 时退回不带权限重试一次、以及删会话时连带清掉活跃 turn。

⚠️ :func:`ruleset_for` 跟着搬过来了，因为它**只有** :meth:`SessionRegistry.ensure_session`
一个调用者；它仍在 :mod:`opencode_bridge.core` 的 ``__all__`` 里按原路径再导出
（``tests/test_core.py`` 与 ``__main__`` 都从那里 import）。

依赖全部注入：``lock`` 与 ``turns`` 是**共用的状态**（事件流与入站那一侧动的是同一个
dict、同一把锁），``asking_platform`` 是一条路由协作者。注入的是**同一个对象**。
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from typing import Optional

from .config import Config
from .conversation_keys import ConversationState
from .event_stream import Turn
from .opencode_client import OpenCodeClient, OpenCodeError

__all__ = ["SessionRegistry", "ruleset_for"]

logger = logging.getLogger("opencode_bridge.session_registry")

SESSION_TITLE_PREFIX = "tg-bridge:"
SESSION_TITLE_MAX = 60


def ruleset_for(mode: str | None) -> list[dict] | None:
    """Map ``permissions_mode`` to an OpenCode permissions rule set.

    ``"ask"`` (and anything unknown) -> ``None`` (server default = ask).
    """
    normalized = str(mode or "ask").strip().lower()
    if normalized == "allow":
        return [{"action": "*", "resource": "*", "effect": "allow"}]
    if normalized == "deny":
        return [{"action": "*", "resource": "*", "effect": "deny"}]
    if normalized != "ask":
        logger.warning(
            "unknown permissions_mode %r; falling back to 'ask'", mode
        )
    return None


class SessionRegistry:
    """Get-or-create (and drop) the opencode session behind a conversation."""

    def __init__(
        self,
        *,
        client: OpenCodeClient,
        config: Config,
        conversation_state: ConversationState,
        lock: threading.RLock,
        turns: dict[str, Turn],
        asking_platform: Callable[[str], str],
    ) -> None:
        """全部依赖由 core 注入，本类不自己去找。

        ``lock`` / ``turns`` 是**共用的状态**而不是协作者：事件流与入站那一侧动的是
        同一个 ``turns``（见各自模块的说明），core 那边好几个方法还在用同一把锁。
        ``asking_platform`` 是 :class:`~opencode_bridge.adapter_router.AdapterRouter`
        的那条注入 callable —— 归属判定归那一块，本类只负责把结果**显式**传下去。
        """
        self._client = client
        self._config = config
        self._conversation_state = conversation_state
        self._lock = lock
        self._turns = turns
        self._asking_platform = asking_platform

    # ------------------------------------------------------------------
    # 会话生命周期
    # ------------------------------------------------------------------
    def ensure_session(self, conversation_id: str, *, platform: str = "") -> str:
        """取这条会话的 opencode session，没有就建一个。

        :param platform: **发起这次读取的平台**，显式传给
            :class:`~opencode_bridge.conversation_keys.ConversationState` 去判定
            ``channel:`` 旧键的归属。留空时退回
            :meth:`~opencode_bridge.adapter_router.AdapterRouter.asking_platform`
            （入站时种下的准确映射）；那条路只服务"拿不到上下文"的调用方，
            :class:`~opencode_bridge.session_model.SessionModelCommand` 就是。
        """
        asking = platform or self._asking_platform(conversation_id)
        session_id = self._conversation_state.get_session(
            conversation_id, platform=asking
        )
        if session_id:
            return session_id
        directory = (
            self._conversation_state.get_meta(
                conversation_id, "directory", None, platform=asking
            )
            or self._config.opencode_directory
            or "."
        )
        # ⚠️ 必须解析成绝对路径再发。opencode 的 `POST /api/session` 对
        # `location.directory` 的**相对路径**（含默认的 "."）一律返回 **500 且响应体为空**，
        # 错误信息因此完全丢失，桥只能报"HTTP 500"这种没有信息量的错。
        # 2026-10-03 A4 真实服务端验证时实测：绝对路径 200 / 空串 200 / "." 500（5/5 稳定复现）。
        #
        # 这里做 abspath 而不是要求用户配绝对路径，有两个理由：
        #   1. `opencode_directory` 的默认值就是 "."（见 config.py），语义是"当前目录"——
        #      把"当前目录"解析成绝对路径是它本来的意思，不该让用户为默认值买单；
        #   2. 上面那个 `or "."` 兜底意味着即使配置为空也必然踩中，不解析就必然失败。
        #
        # 回环测试抓不到这个 bug：测试都传绝对路径或临时目录，只有真实默认配置会中招。
        directory = os.path.abspath(directory)
        title = f"{SESSION_TITLE_PREFIX}{conversation_id}"[:SESSION_TITLE_MAX]
        agent = self._config.opencode_agent or None
        rules = ruleset_for(self._config.permissions_mode)
        try:
            session_id = self._client.create_session(
                directory=directory,
                title=title,
                agent=agent,
                permissions=rules,
            )
        except OpenCodeError as exc:
            if exc.status != 400 or rules is None:
                raise
            logger.warning(
                "create_session rejected permissions (%s); retrying without",
                exc,
            )
            session_id = self._client.create_session(
                directory=directory,
                title=title,
                agent=agent,
                permissions=None,
            )
        self._conversation_state.set_session(conversation_id, session_id)
        logger.info(
            "created session %s for %s (dir=%s)", session_id, conversation_id,
            directory,
        )
        return session_id

    def drop_session(self, conversation_id: str, *, platform: str = "") -> Optional[str]:
        """Delete the current session server-side and locally (never raises).

        ``platform`` 语义同 :meth:`ensure_session`：它决定 :meth:`drop_session`
        要不要连带删掉那个 ``channel:`` 旧键 —— 只删**本平台文法覆盖得到**的那个。
        """
        asking = platform or self._asking_platform(conversation_id)
        session_id = self._conversation_state.get_session(
            conversation_id, platform=asking
        )
        if session_id:
            try:
                self._client.delete_session(session_id)
            except Exception as exc:
                logger.warning("delete_session(%s) failed: %s", session_id, exc)
            self._conversation_state.drop_session(conversation_id, platform=asking)
            with self._lock:
                self._turns.pop(session_id, None)
        return session_id

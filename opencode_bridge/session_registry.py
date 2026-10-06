"""会话映射：这条会话对应哪个 opencode session，没有就建一个。

这一整块原先是 :class:`~opencode_bridge.core.BridgeCore` 的两个方法。搬出来是因为
那个类已经远超 AGENTS.md §5.1 的阈值（方法数 ~15+ / 类自身代码行 ~250+），而
"取或建会话"是里面**自成一块**的一坨：解析工作目录、按权限模式组装建会话参数、
400 时退回不带权限重试一次、以及删会话时连带清掉活跃 turn。

⚠️ :func:`ruleset_for` 跟着搬过来了，因为它**只有** :meth:`SessionRegistry.ensure_session`
一个调用者；它仍在 :mod:`opencode_bridge.core` 的 ``__all__`` 里按原路径再导出
（``tests/test_core.py`` 与 ``__main__`` 都从那里 import）。

依赖全部注入：``lock`` 与 ``turns`` 是**共用的状态**（事件流与入站那一侧动的是同一个
dict、同一把锁），``asking_platform`` 是一条路由协作者，``cancel_turn`` 是出站那一侧
的收尾通道（删会话时弹掉了在跑的一轮 ⇒ 见 :meth:`SessionRegistry.drop_session`）。
注入的是**同一个对象**。
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
        cancel_turn: Callable[..., None],
    ) -> None:
        """全部依赖由 core 注入，本类不自己去找。

        ``lock`` / ``turns`` 是**共用的状态**而不是协作者：事件流与入站那一侧动的是
        同一个 ``turns``（见各自模块的说明），core 那边好几个方法还在用同一把锁。
        ``asking_platform`` 是 :class:`~opencode_bridge.adapter_router.AdapterRouter`
        的那条注入 callable —— 归属判定归那一块，本类只负责把结果**显式**传下去。
        ``cancel_turn`` 是出站那一侧的"这一轮被丢弃了，请告诉读者"（见
        :meth:`drop_session`）—— 归属归 :mod:`opencode_bridge.outbound`，本类只负责
        在**锁外**调它。
        """
        self._client = client
        self._config = config
        self._conversation_state = conversation_state
        self._lock = lock
        self._turns = turns
        self._asking_platform = asking_platform
        self._cancel_turn = cancel_turn

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

        ## 被丢弃的那一轮必须**告诉读者**（``ora-15`` 确诊的缺陷）

        此前这里在锁内 ``self._turns.pop(session_id, None)`` 就走人，**全程无收尾**
        ⇒ 那一轮的占位消息 ``⏳ 处理中…``（或已显示半截正文的那条）永远不会被改写，
        读者于是盯着一个**僵尸气泡**；随后到达的终止事件里
        :meth:`~opencode_bridge.event_stream.EventStream._finalize_session` 拿到
        ``None``、走 ``turn end for unknown session`` 后返回 ⇒ 既不发布答复也不刷队列。
        ⚠️ **确定性触发、无竞态**：用户在模型还在回答时发 ``/new``（``/reset`` 同理）。
        对比 ``/stop``：它走 ``client.interrupt()`` ⇒ 终止事件正常到达 ⇒ 收尾正常发生。

        ⇒ 现在把弹出来的那个 turn 交给注入的 ``cancel_turn``（出站那一侧），由它把
        占位消息改写成「已取消」，或在改不动的平台上补发一句。⛔ **不发半截正文**：
        用户 2026-10-07 明确否掉了那一条，而 :meth:`EventStream._finalize_session`
        走的就是"发布完整答复"那条路 ⇒ 那正是被否掉的行为。

        ⚠️ **只有真的有 turn 被弹掉时才说话**：用户没在跑任何东西时发 ``/new``，
        多出来的任何一句都是噪音。
        """
        asking = platform or self._asking_platform(conversation_id)
        session_id = self._conversation_state.get_session(
            conversation_id, platform=asking
        )
        dropped_turn: Optional[Turn] = None
        if session_id:
            try:
                self._client.delete_session(session_id)
            except Exception as exc:
                logger.warning("delete_session(%s) failed: %s", session_id, exc)
            self._conversation_state.drop_session(conversation_id, platform=asking)
            with self._lock:
                dropped_turn = self._turns.pop(session_id, None)
        if dropped_turn is not None:
            # ⚠️ 位置有意义：**锁外**。各适配器的 ``edit()`` / ``send()`` 会 sleep，
            # 锁内发会把别的会话冻住（见 :meth:`_announce_cancelled_turn`）。
            self._announce_cancelled_turn(conversation_id, session_id, dropped_turn)
        return session_id

    def _announce_cancelled_turn(
        self, conversation_id: str, session_id: str, turn: Turn,
    ) -> None:
        """告诉读者「那一轮被丢弃了」，**绝不抛**。

        拆成独立方法是因为它与 :meth:`drop_session` 的其余部分是两种关注点
        （本地/服务端删状态 vs. 一句用户可见的话），而后者只需要 ``turn`` 里的
        ``conversation_id`` 与 ``progress_handle`` 两样。

        ⛔ **不碰 ``turn.assemble()``**：那半截正文**不发**（见 :meth:`drop_session`
        的说明）。⚠️ 也**不在锁内**发消息 —— 各适配器的 ``send()`` / ``edit()`` 会
        sleep，锁内发会把别的会话冻住（与
        :attr:`Turn.progress_message_creating` 那段「⛔ 绝不把 send 放进锁里」
        同一条纪律）。

        ⛔ **不刷队列**：丢弃是 ``/new`` 的语义（那一轮不要了），而队列里排着的那些是
        **之后**要问的问题 —— :meth:`~opencode_bridge.commands.CommandHandler.
        _cmd_new` 紧接着就会建新会话，它们会在新会话上正常发出。这里刷出去等于让它们
        落到一个刚被删掉的会话上。
        """
        try:
            self._cancel_turn(
                turn.conversation_id or conversation_id,
                turn.progress_handle,
                session_id,
            )
        except Exception:  # noqa: BLE001 - 收尾通道坏了不该让 /new 本身失败
            logger.exception(
                "could not report the cancelled turn of session %s",
                session_id,
            )

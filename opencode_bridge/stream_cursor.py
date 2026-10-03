"""消息流游标：某条消息流读到哪儿了，落盘。

这一整块原先是 :class:`~opencode_bridge.core.BridgeCore` 的两个方法。搬出来有两个
理由：那个类已经远超 AGENTS.md §5.1 的阈值（方法数 ~15+ / 类自身代码行 ~250+）；
以及**归属划分是对的** —— 这两个方法服务的是轮询型适配器（email 的 IMAP UID 等）
跨重启续跑，跟"会话 ↔ opencode session 的映射"是两回事，把它们塞进
:class:`~opencode_bridge.session_registry.SessionRegistry` 会让"改一处功能只碰一个
类"这条判据失效。

依赖只有一条：``state``。
"""

from __future__ import annotations

import logging
from typing import Optional

from .state import StateStore

__all__ = ["StreamCursorStore"]

logger = logging.getLogger("opencode_bridge.stream_cursor")

#: ``StateStore`` 里存放"消息流位置"的 meta 键。轮询型适配器（email 的 IMAP
#: UID 等）靠它跨重启续跑；见 :meth:`StreamCursorStore.load`。
_STREAM_CURSOR_META_KEY = "stream_cursor"


class StreamCursorStore:
    """One persisted read position per (opaque) message stream."""

    def __init__(self, *, state: StateStore) -> None:
        self._state = state

    def load(self, stream_scope: str) -> Optional[int]:
        """读回某条消息流上次的位置。

        ``stream_scope`` 不是会话 id，只是 ``state.json`` 里的一个**不透明键**，
        由适配器保证稳定且互不撞车（email 用"账号 + 邮箱"）。

        存的不是整数（被手改坏 / 旧版本写入的别的类型）时按"没有已存位置"
        处理并告警 —— 退化方向必须是"重新走首次启动语义"，不能是"拿着垃圾值
        去算 UID 区间"。
        """
        stored = self._state.get_meta(str(stream_scope), _STREAM_CURSOR_META_KEY, None)
        if stored is None:
            return None
        try:
            return int(stored)
        except (TypeError, ValueError):
            logger.warning("stream cursor %r is not an integer; ignoring it", stored)
            return None

    def save(self, stream_scope: str, position: int) -> None:
        """把某条消息流的位置写进 state。

        写失败只告警、不上抛：位置丢了最坏是重启后重投一封（由写前日志兜底），
        而让异常冒到适配器的轮询线程会把整个收信循环打断。
        """
        try:
            self._state.set_meta(str(stream_scope), _STREAM_CURSOR_META_KEY,
                                 int(position))
        except Exception as exc:  # noqa: BLE001 - 落盘失败不该打断收信
            logger.warning(
                "cannot persist the stream cursor for %s (%s); a restart may "
                "re-process messages that were already handled",
                stream_scope, exc,
            )

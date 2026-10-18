"""桥侧权限请求的**本地超时**看护：等满 5 分钟没答复，就在账本里标成"已取消"。

这是 dsh-im-connect 借鉴面 **D 审批超时语义**（见 ``docs/borrowing-catalog.md``）
的用户拍板落地：**到期按取消（非拒绝）**。

**"按取消（非拒绝）"到底是什么意思 —— 为什么这里只动账本、不发任何 API**：

- 上游（opencode v2.0.22）**没有"取消一个权限请求"这个 API**。向它发
  ``reply_permission(..., "reject")`` 会变成对整个会话的批量拒绝，违背拍板。
- 所以唯一忠实的实现是**账本内取消**：到期时把该请求从 :data:`ANSWER_PENDING`
  标成 :data:`ANSWER_CANCELLED`（这样迟到的第二次回答会被 :mod:`permission_ledger`
  按"已关闭"否掉），并**渲染一条 IM** 告诉用户它超时了。

⚠️ **已知代价（有意、可接受）**：服务端那个权限请求**永远挂在那里、没人回答它**
（它没有超时、也不会自己消失）。这是"非拒绝"的代价 —— 我们**不能**伪造一次回答，
所以宁可让服务端挂着一个没人答的请求，也不把一次拒绝强加给整个会话。

**本模块做 I/O、发消息，而 :mod:`permission_ledger` 保持纯内存** —— 所以超时通知
不放进账本（账本 docstring 明写"不做 I/O、不发消息"），放在这里。

线程安全：``permission.asked`` 来自 SSE 线程、``/approve`` / 按钮回调来自各适配器
的入站线程，两边都可能同时触碰同一个请求的计时器；计时器到期的线程也可能同时
与一次真回复竞态。全部计时器操作都在本类自己的锁内进行。
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

from .permission_ledger import ANSWER_PENDING, PermissionLedger

__all__ = [
    "PermissionTimeoutWatcher",
    "PERMISSION_TIMEOUT_SECONDS",
    "PERMISSION_TIMEOUT_MESSAGE",
]

logger = logging.getLogger("opencode_bridge.permission_timeout")

#: 每个挂起的权限请求等多久（秒）。5 分钟是用户拍板的上限。
PERMISSION_TIMEOUT_SECONDS = 300.0

#: 超时后渲染给用户的 IM 正文。``%s`` 是 request id（沿用 :mod:`permission_ledger`
#: 里 ``repeated_answer_notice`` 的 ``%`` 风格）。
#:
#: ⚠️ 措辞**不得**声称请求被拒绝或服务端已取消 —— 它没有。只说本地取消、以及
#: 服务端那边还挂着，用户真要重新发起就再触发一次。
PERMISSION_TIMEOUT_MESSAGE = (
    "权限请求 %s 已超时（5 分钟未回复），已在本地标记为已取消。"
    "如需重新发起，请再触发一次。"
)


class PermissionTimeoutWatcher:
    """为每个挂起的权限请求持有一个 ``threading.Timer``，到期就本地取消。

    结构镜像 :class:`opencode_bridge.inbound_merge.ConversationMerger` 的保险丝
    （``_rearm_fuse`` / ``_cancel_fuse`` / ``_fuse_fired``）：状态在锁内取、回调在
    锁外发。
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = PERMISSION_TIMEOUT_SECONDS,
        ledger: PermissionLedger,
        notify: Callable[[str, str, str], None],
    ) -> None:
        """
        ``timeout_seconds`` 是每个请求等多久；``<= 0`` = 关掉这个特性（0 = off，
        不建任何计时器）。

        ``ledger`` 是共享的权限账本 —— 到期时把它从 :data:`ANSWER_PENDING` 标成
        :data:`ANSWER_CANCELLED`（只在仍是 pending 时才标，避免覆盖真回复）。

        ``notify`` 是渲染超时 IM 的回调 ``notify(session_id, request_id, message)``，
        由调用方接到 :meth:`~opencode_bridge.event_stream.EventStream` 上。**必须在
        锁外调用**（镜像 ``_fuse_fired``），且**绝不允许让它把计时器线程带死**。
        """
        self._timeout_seconds = float(timeout_seconds)
        self._ledger = ledger
        self._notify = notify
        self._lock = threading.Lock()
        #: ``(session_id, request_id) -> Timer``。只在有未过期计时器时非空。
        self._timers: dict[tuple[str, str], threading.Timer] = {}

    def set_notify(self, notify: Callable[[str, str, str], None]) -> None:
        """把渲染超时 IM 的回调接到这个看护上。

        core 构造看护时（早于 ``EventStream`` 存在）只能先给个占位；等
        ``EventStream`` 构造好，由它把回调接成自己的 :meth:`_on_permission_timeout`。
        线程安全：回调只在计时器到点时锁外读取。
        """
        with self._lock:
            self._notify = notify

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------
    def watch(self, session_id: str, request_id: str) -> None:
        """为一个请求启动/重置超时计时器（``permission.asked`` 到达时调用）。

        若该 key 已有计时器，先取消再重排（镜像 :meth:`_rearm_fuse`）。``<= 0``
        的 ``timeout_seconds`` 直接不做。
        """
        if not request_id:
            return
        with self._lock:
            self._cancel_timer_locked((session_id, request_id))
            if self._timeout_seconds <= 0:
                return
            timer = threading.Timer(
                self._timeout_seconds,
                self._timeout_fired,
                [session_id, request_id],
            )
            timer.daemon = True
            self._timers[(session_id, request_id)] = timer
            timer.start()

    def cancel(self, session_id: str, request_id: str) -> None:
        """取消并丢弃这个请求的计时器（一条**真回复**落地时调用）。

        幂等：没有计时器就是什么都不做。
        """
        with self._lock:
            self._cancel_timer_locked((session_id, request_id))

    def stop(self) -> None:
        """取消所有待处理计时器（生命周期收尾）。幂等。"""
        with self._lock:
            keys = list(self._timers)
            for key in keys:
                self._cancel_timer_locked(key)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _cancel_timer_locked(self, key: tuple[str, str]) -> None:
        """Caller holds ``self._lock``."""
        timer = self._timers.pop(key, None)
        if timer is not None:
            timer.cancel()

    def _timeout_fired(self, session_id: str, request_id: str) -> None:
        """Timer thread body: check the ledger, then report **outside** the lock."""
        notify = False
        with self._lock:
            # 竞态：这一行可能已被 ``cancel`` 取消并丢弃 —— 那不该再发超时。
            self._timers.pop((session_id, request_id), None)
            # 只在仍是 pending（一条真回复抢先落地了）时才取消；否则不发假超时。
            status = self._ledger.status_of(session_id, request_id)
            if status.state == ANSWER_PENDING:
                self._ledger.record_cancelled(session_id, request_id)
                notify = True
        if not notify:
            return
        try:
            self._notify(
                session_id, request_id,
                PERMISSION_TIMEOUT_MESSAGE % request_id,
            )
        except Exception:
            # 计时器线程是 daemon：绝不能让一次渲染失败把它带死、还吞掉别的。
            logger.exception(
                "permission timeout notification failed for %s/%s",
                session_id, request_id,
            )
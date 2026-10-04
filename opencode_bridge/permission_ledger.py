"""权限请求的本地账本：这个桥**问过什么**、**答过什么**。

为什么需要它（这是 C4 真正的根因，不是"审批超时"）：

`permission.asked` 事件到达时，桥手里**同时**握着 session id、request id 和要发给
哪个会话 —— 那一刻信息是全的。但 :meth:`~opencode_bridge.event_stream.EventStream.
_on_permission_asked` 只把它渲染成一段文字就把 ``request_id`` 扔了。之后用户敲
``/approve <id>`` 时，桥只能把**用户手敲的那个字符串**原样转给服务端，既不知道这个
请求是不是自己问出来的，也分不清这是第一次回答还是第二次。

后果是可以测出来的：``/approve per_7 once`` 之后再敲 ``/approve per_7 always``，
会发出**两次** ``reply_permission``，第二次把一个已经答过的请求**放宽成永久放行**。
这与 ``d45440f`` 修掉的 ``/deny <id> always`` 是同一类危害：一个已经关闭的决定被
第二次、且更宽的决定覆盖掉。

所以本模块只做一件事：**在唯一知道答案的那一刻把它记下来**（AGENTS.md §8 ——
根因是"关键状态没落盘"，不是"缺一个超时"）。它不做任何推断：

* 记不下来的（进程重启前就问过的请求）一律当"没见过"，**照旧转给服务端** ——
  服务端才是"这个 id 存不存在"的权威，而在内存账本上拒绝一个**真的还活着**的请求
  是更危险的错方向（那个方向会静默卡住 agent）。
* 因此账本**只**用来否掉"本桥明确知道已经关闭"的请求，不用来猜任何一个别的状态。

⚠️ 账本是**进程内**的，重启即清空。这是有意的取舍，见 :meth:`PermissionLedger.
_status_of` 里 :data:`ANSWER_UNSEEN` 那段：清空只会让判断退回服务端，而服务端比
本模块权威。

本模块**不做 I/O、不发消息**，因此可以脱离 core 与适配器单独测
（见 ``tests/test_permission_ledger.py``）；三处消费方的接线契约分别锁在
``tests/test_commands.py`` / ``tests/test_inbound_gateway.py`` /
``tests/test_event_stream.py``。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

__all__ = [
    "PermissionLedger",
    "PermissionRequestStatus",
    "ANSWER_PENDING",
    "ANSWER_ALREADY_REPLIED",
    "ANSWER_RESOLVED_UPSTREAM",
    "ANSWER_UNSEEN",
    "REPEATED_ANSWER_ACK",
    "repeated_answer_notice",
]

logger = logging.getLogger("opencode_bridge.permission_ledger")


# ----------------------------------------------------------------------
# 四种状态
# ----------------------------------------------------------------------
#: ``permission.asked`` 已经渲染到 IM，而本桥还没为它发过决策。
ANSWER_PENDING = "pending"

#: **本桥已经**为这个请求发过一次决策 —— 再发一次就是第二次回信。
ANSWER_ALREADY_REPLIED = "already_replied"

#: 服务端广播了 ``permission.replied``，说这个请求已经结束（多半是在别处答的，
#: 或者 agent 自己走了另一条路）。这是"已经关闭"这件事的**权威**来源。
ANSWER_RESOLVED_UPSTREAM = "resolved_upstream"

#: 本进程从没见过这个请求：重启之前问的、用户手敲的、或者压根不存在。
#: ⚠️ **绝不能**据此拒绝 —— 见模块 docstring：服务端才是权威，而在这里拒绝一个
#: 还活着的请求会把 agent 永远卡住。这个状态一律**照旧转给服务端**，由它来判。
ANSWER_UNSEEN = "unseen"

#: 这两种状态意味着"再答一次就是对同一个请求的第二次回答"，必须**本地**否掉。
_CLOSED_STATES = frozenset({ANSWER_ALREADY_REPLIED, ANSWER_RESOLVED_UPSTREAM})

#: 账本最多记多少个请求。权限请求是低频事件（一个请求对应一次工具调用），
#: 这个上限只为"有界"存在 —— 一个只增不减的账本就是慢速泄漏。
#: 挤掉最老的一条会让那条退回 :data:`ANSWER_UNSEEN`，也就是**朝服务端那一边失败
#: 开放**：宁可多问一次（服务端会拒），也不能因为记久远就凭空造出"已答过"。
_DEFAULT_CAPACITY = 512

#: 按钮那条路上给平台的**短应答**（ack）。长的那一份走
#: :func:`repeated_answer_notice`，和失败路径一样：ack 短、解释走正文。
REPEATED_ANSWER_ACK = "已忽略（已回复过）"


@dataclass(frozen=True)
class PermissionRequestStatus:
    """What this bridge knows about one permission request, right now."""

    session_id: str
    request_id: str
    #: one of :data:`ANSWER_PENDING` / :data:`ANSWER_ALREADY_REPLIED` /
    #: :data:`ANSWER_RESOLVED_UPSTREAM` / :data:`ANSWER_UNSEEN`.
    state: str
    #: 本桥**已经**为它发过的决策（:data:`ANSWER_ALREADY_REPLIED` 时非空）。
    #: 留着它是为了让"已回复过"那句话能说清上次答的是什么。
    decision: str = ""

    @property
    def already_closed(self) -> bool:
        """True when answering again would be a *second* answer to one request.

        :data:`ANSWER_UNSEEN` 与 :data:`ANSWER_PENDING` 都是 False —— 前者把判断
        交给服务端，后者是真的还没答过。
        """
        return self.state in _CLOSED_STATES


def repeated_answer_notice(status: PermissionRequestStatus) -> str:
    """The user-visible line for an answer we refuse to send a second time.

    这句话存在的意义是**"报告给用户"而不是静默丢弃**（AGENTS.md：静默地让用户
    遇到错的东西，比多一处不统一糟糕得多）。被否掉的那条回答若是从按钮来的，
    平台侧还会额外收到一条短 ack（:data:`REPEATED_ANSWER_ACK`）。
    """
    if status.state == ANSWER_RESOLVED_UPSTREAM:
        return "权限请求 %s 已经结束，这条回复被忽略。" % status.request_id
    return (
        "权限请求 %s 已经回复过（%s），这条回复被忽略。"
        % (status.request_id, status.decision or "once")
    )


class PermissionLedger:
    """Which permission requests this bridge surfaced, and which it already answered.

    线程安全：``permission.asked`` / ``permission.replied`` 来自 SSE 线程，
    ``/approve`` / 按钮回调来自各适配器的入站线程，两边都会写。
    """

    def __init__(self, *, capacity: int = _DEFAULT_CAPACITY) -> None:
        self._capacity = max(1, int(capacity))
        self._lock = threading.Lock()
        #: ``(session_id, request_id) -> PermissionRequestStatus``。
        #: **按会话+请求**而不是只按请求 id：`/api/event` 是全服务器广播，
        #: 只按 id 记会把别的会话的请求混进来（还要占掉别人的槽位）。
        self._requests: dict[tuple[str, str], PermissionRequestStatus] = {}

    # ------------------------------------------------------------------
    # 写：三个时刻，各自对应一个**已知**的事实
    # ------------------------------------------------------------------
    def record_asked(self, session_id: str, request_id: str) -> None:
        """One ``permission.asked`` has been rendered to IM; remember its id.

        **不会**把一个已经关闭的请求重新打开：服务端偶尔会重播 ``permission.asked``，
        而那不能让一个刚被拒绝的重复回答重新变成合法。
        """
        key = self._key(session_id, request_id)
        if key is None:
            return
        with self._lock:
            tracked = self._requests.get(key)
            if tracked is not None and tracked.already_closed:
                return
            self._remember(key, PermissionRequestStatus(
                session_id=key[0], request_id=key[1], state=ANSWER_PENDING,
            ))

    def record_answered(
        self, session_id: str, request_id: str, decision: str,
    ) -> None:
        """本桥刚刚为这个请求**成功**发出了一次决策。

        只在 :meth:`~opencode_bridge.opencode_client.OpenCodeClient.reply_permission`
        正常返回之后才调用 —— 一次 5xx 绝不能把请求锁死成"已答过"，否则用户再也
        没有办法批准它（而那个请求在服务端还挂着）。
        """
        key = self._key(session_id, request_id)
        if key is None:
            return
        with self._lock:
            self._remember(key, PermissionRequestStatus(
                session_id=key[0], request_id=key[1],
                state=ANSWER_ALREADY_REPLIED, decision=str(decision or ""),
            ))

    def note_replied(self, session_id: str, request_id: str) -> None:
        """The server broadcast ``permission.replied``: this request is closed.

        这是唯一一个**本进程没问过**也能知道的关闭信号 —— 重启前的请求正是靠它
        被认成"已经结束"。本桥自己发过决策时**保留**那条状态：那样
        :func:`repeated_answer_notice` 能顺带说清上次答的是什么。
        """
        key = self._key(session_id, request_id)
        if key is None:
            return
        with self._lock:
            tracked = self._requests.get(key)
            if tracked is not None and tracked.state == ANSWER_ALREADY_REPLIED:
                return
            self._remember(key, PermissionRequestStatus(
                session_id=key[0], request_id=key[1],
                state=ANSWER_RESOLVED_UPSTREAM,
            ))

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------
    def status_of(self, session_id: str, request_id: str) -> PermissionRequestStatus:
        """What this bridge knows about one permission request.

        从没见过就返回 :data:`ANSWER_UNSEEN` 的状态对象，而不是 ``None`` ——
        调用方只有一条要走的路（读 ``.already_closed``），少一个分支就少一处
        "忘了判空就把未知当成已答过"的机会。
        """
        key = self._key(session_id, request_id)
        if key is None:
            return PermissionRequestStatus(
                session_id=str(session_id or ""), request_id="",
                state=ANSWER_UNSEEN,
            )
        with self._lock:
            tracked = self._requests.get(key)
        if tracked is not None:
            return tracked
        return PermissionRequestStatus(
            session_id=key[0], request_id=key[1], state=ANSWER_UNSEEN,
        )

    def __len__(self) -> int:
        with self._lock:
            return len(self._requests)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    @staticmethod
    def _key(session_id: str, request_id: str) -> tuple[str, str] | None:
        """``(session_id, request_id)``；**request id 为空就当没有这一条**。

        服务端给不出 request id 的那一帧（``permission.asked`` 的 data 里缺 ``id``）
        不能进账本：把空 id 记下来会让"没有 id"的所有请求挤同一个槽位，而它们
        彼此无关。
        """
        clean_session = str(session_id or "")
        clean_request = str(request_id or "")
        if not clean_request:
            return None
        return (clean_session, clean_request)

    def _remember(self, key: tuple[str, str], status: PermissionRequestStatus) -> None:
        """Caller holds ``self._lock``.

        ``dict`` 保序，所以"挤掉最老的一条"就是 ``next(iter(...))``。注意重复赋值
        **不会**把键挪到末尾（保留原位置）—— 这里无所谓，容量只是上界不是 LRU。
        """
        self._requests[key] = status
        while len(self._requests) > self._capacity:
            oldest = next(iter(self._requests))
            evicted = self._requests.pop(oldest)
            logger.debug(
                "permission ledger full (%d); forgetting %s/%s (was %s) — it will "
                "now be judged by the server instead",
                self._capacity, evicted.session_id, evicted.request_id,
                evicted.state,
            )

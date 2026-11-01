"""``/approve`` / ``/deny`` 的决策语义（真源）。

从 :mod:`opencode_bridge.commands` 拆出（AGENTS.md §5 与 §5.1）：两条权限命令共用
同一套「终值决策 + 迟到的第二次回答被否掉」的规则，放在一个对象里，
:class:`~opencode_bridge.commands.CommandHandler` 只留两条薄转发。

⚠️ 不变式（``tests/test_commands.py`` 钉住）：共享回信函数
:meth:`PermissionCommand._apply_permission_decision` **只接受终值决策**、没有任何
可被覆盖的 ``default_*`` 参数 —— ``/deny`` 因此结构上够不着 ``always``。

``logger`` 仍用 ``opencode_bridge.commands`` 这个通道名，日志逐字节不变。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .adapters import Adapter
from .conversation_keys import ConversationState
from .opencode_client import OpenCodeClient
from .permission_ledger import PermissionLedger, repeated_answer_notice
from .permission_timeout import PermissionTimeoutWatcher

if TYPE_CHECKING:  # 只借类型，不构成运行期回环（同 a2a_task_ledger 的先例）
    from .commands import SendText

logger = logging.getLogger("opencode_bridge.commands")


# ----------------------------------------------------------------------
# 权限回信：决策词与用法文案（两条命令共用，所以放在模块级）
# ----------------------------------------------------------------------
#: ``/approve`` 第二个 token 的取值。**只有** ``/approve`` 有这个位置；
#: ``/deny`` 的决策恒为 ``reject``，多一个 token 就是用错命令了。
_APPROVE_CHOICES = ("once", "always")

#: 参数写错时回的那一句（``/approve`` 与 ``/deny`` 共用同一份，省得两边说法不一）。
_PERMISSION_USAGE = "用法: /approve <请求ID> [always]  或  /deny <请求ID>"


class PermissionCommand:
    """``/approve`` 与 ``/deny`` 的全部逻辑：解析、定决策、回信。

    与 :class:`~opencode_bridge.commands.CommandHandler` 共用同一批协作者
    （由它在构造时注入），所以这里**没有** core 引用、也不读 core 的私有状态。

    ⭐ **终值决策只由** :meth:`_cmd_approve` **一处选定**：共享层
    :meth:`_apply_permission_decision` 收到的每一个决策都原样发出去，没有任何
    ``default_*`` 这类可被调用方覆盖的入口 —— ``/deny`` 因此在结构上够不着
    ``always``（AGENTS.md §8）。
    """

    def __init__(
        self,
        *,
        client: OpenCodeClient,
        conversation_state: ConversationState,
        permission_ledger: PermissionLedger,
        permission_timeout: PermissionTimeoutWatcher,
        send_text: SendText,
    ) -> None:
        """五个协作者由 :class:`~opencode_bridge.commands.CommandHandler` 注入。

        ``client`` 只用到 :meth:`OpenCodeClient.reply_permission``；
        ``conversation_state`` 只用到 :meth:`ConversationState.get_session`；
        ``permission_ledger`` 读 :meth:`PermissionLedger.status_of`、写
        :meth:`PermissionLedger.record_answered`；
        ``permission_timeout`` 只在一处用到：一条真回复落地后 ``cancel`` 掉本地
        超时计时器。
        """
        self._client = client
        self._conversation_state = conversation_state
        self._permission_ledger = permission_ledger
        self._permission_timeout = permission_timeout
        self._send_text = send_text


    def _cmd_approve(
        self, conversation_id: str, adapter: Adapter, args: str
    ) -> None:
        """``/approve <请求ID> [once|always]`` —— **只有**这条命令能选 ``always``。

        决策在这里定完，再连同请求 id 一起交给共享的回信函数；那个函数只负责把
        **已经定好的**决策送出去。``/deny`` 因此结构上够不着 ``always`` 这条路 ——
        之前共用一个带 ``default_decision`` 的函数，而"默认"意味着任何调用方都能
        悄悄覆盖它，``/deny per_5 always`` 于是发出了永久放行（AGENTS.md §8：
        权限路径上的 ``default_*`` 参数本身就是缺陷）。
        """
        tokens = args.split()
        if not tokens or len(tokens) > 2:
            self._send_text(
                conversation_id, _PERMISSION_USAGE, kind="error",
                adapter=adapter,
            )
            return
        request_id = tokens[0]
        decision = "once"
        if len(tokens) == 2:
            choice = tokens[1].lower()
            if choice not in _APPROVE_CHOICES:
                self._send_text(
                    conversation_id, _PERMISSION_USAGE, kind="error",
                    adapter=adapter,
                )
                return
            decision = choice
        self._apply_permission_decision(
            conversation_id, adapter, request_id, decision
        )

    def _cmd_deny(
        self, conversation_id: str, adapter: Adapter, args: str
    ) -> None:
        """``/deny <请求ID>`` —— 决策**只有** ``reject``，而且只接一个 token。

        多写一个 token 一律当用错命令处理（回用法、**不发任何决策**）：
        默默忽略那个多余的 token 会把用户的笔误藏起来，而"看起来拒绝了、其实没
        拒绝"比报错糟糕得多。
        """
        tokens = args.split()
        if len(tokens) != 1:
            self._send_text(
                conversation_id, _PERMISSION_USAGE, kind="error",
                adapter=adapter,
            )
            return
        self._apply_permission_decision(
            conversation_id, adapter, tokens[0], "reject"
        )

    def _apply_permission_decision(
        self,
        conversation_id: str,
        adapter: Adapter,
        request_id: str,
        decision: str,
    ) -> None:
        """把**终值**决策送到 opencode，并回一句确认。

        ``decision`` 是调用方已经选定的最终结果：这里不做选择、不读第二个 token，
        收到的每一个决策都原样发给服务端。这样"谁能发 ``always``"就只由
        :meth:`_cmd_approve` 一处决定 —— 共享层没有可以被覆盖的入口。

        ⚠️ **迟到的第二次回答在这里被否掉**（C4）：本桥已经为这个请求发过决策，
        或者服务端广播过 ``permission.replied``，那就**一个字节都不发**，
        并且明确告诉用户这条被忽略了。少了这一步，
        ``/approve per_7 once`` 之后再敲 ``/approve per_7 always`` 会发出两次
        ``reply_permission``，第二次把一个已经答过的请求**放宽成永久放行** ——
        与 ``d45440f`` 修掉的 ``/deny <id> always`` 同一类危害。

        ⚠️ 反过来，**"本桥没见过这个 id"不作为拒绝理由**（状态
        :data:`~opencode_bridge.permission_ledger.ANSWER_UNSEEN`）：
        内存账本活不过重启，而服务端才是"这个 id 存不存在"的权威。在这里拒绝一个
        还活着的请求会把 agent 永远卡住 —— 那个错方向是静默的，而这一边不是。
        """
        session_id = self._conversation_state.get_session(
            conversation_id, platform=adapter.name
        )
        if not session_id:
            self._send_text(
                conversation_id, "当前没有会话，无法回复权限请求。",
                kind="error", adapter=adapter,
            )
            return
        status = self._permission_ledger.status_of(session_id, request_id)
        if status.already_closed:
            logger.info(
                "ignoring a second answer to permission request %s of session %s "
                "(already %s); nothing is sent to the server",
                request_id, session_id, status.state,
            )
            self._send_text(
                conversation_id,
                repeated_answer_notice(status),
                kind="error",
                adapter=adapter,
            )
            return
        self._client.reply_permission(session_id, request_id, decision)
        # 只在**成功**之后记账：一次 5xx 若也记成"已答过"，这个还挂在服务端的请求
        # 就再也批准不了（用户唯一能做的事被我们自己锁掉了）。
        self._permission_ledger.record_answered(session_id, request_id, decision)
        # 真回复落地：撤掉本地超时计时器，免得它稍后到点再发一条假的"已超时"。
        self._permission_timeout.cancel(session_id, request_id)
        self._send_text(
            conversation_id,
            f"已回复权限请求 {request_id}: {decision}",
            adapter=adapter,
            session_id=session_id,
        )

"""IM 侧斜杠命令：``/help`` ``/setup`` ``/new`` ``/stop`` ``/status`` ``/cd``，
以及 ``/model`` ``/delivery`` ``/approve`` ``/deny`` 的转发。

这一整块原先是 :class:`~opencode_bridge.core.BridgeCore` 的十几个私有方法。搬出来
是因为那个类已经远超 AGENTS.md §5.1 的阈值，而命令处理恰好是其中**耦合最浅**的一块
（见 :class:`CommandHandler`：与 core 之间只有注入的协作者，没有 core 引用）。

AGENTS.md §5 的进一步拆分（纯结构调整，行为零变化）：**用户可见的冻结文案**
（``/help``、``/setup`` 引导）搬进 :mod:`opencode_bridge.command_text`，
**权限决策语义**（``/approve`` / ``/deny``）搬进
:mod:`opencode_bridge.command_permission`。本模块继续把文案名**再导出** ——
``opencode_bridge.core`` / ``__main__`` 与测试都按
``from opencode_bridge.commands import ...`` 取它们，导入路径不能变。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from .adapters import Adapter
from .command_permission import PermissionCommand
from .command_text import (
    HELP_TEXT,
    SETUP_INVALID_TEXT,
    SETUP_MENU_TEXT,
    _SETUP_ALIASES,
    _SETUP_BUTTON_LABELS,
    _SETUP_GUIDES,
    _setup_guide,
    setup_platforms,
    setup_reply,
)
from .config import Config
from .conversation_keys import ConversationState
from .delivery_retry import DeliveryRetryCommand
from .hooks import Button, MsgHandle, Outbound
from .normalize import _as_dict, _clean
from .opencode_client import OpenCodeClient, OpenCodeError
from .permission_ledger import PermissionLedger
from .permission_timeout import PermissionTimeoutWatcher
from .session_model import SessionModelCommand

__all__ = [
    "CommandHandler",
    "HELP_TEXT",
    "SETUP_MENU_TEXT",
    "setup_platforms",
    "setup_reply",
]

logger = logging.getLogger("opencode_bridge.commands")




# ----------------------------------------------------------------------
# 注入的协作者：签名写在这里，core 那边的实现是什么它们不关心
# ----------------------------------------------------------------------
#: 发一条出站文本。关键字参数与返回值都照着 core 的发信入口：
#: ``kind`` / ``adapter`` / ``session_id``，返回消息句柄或 ``None``。
SendText = Callable[..., MsgHandle | None]

#: 取这条会话当前的 opencode session id，没有就新建一个。
EnsureSession = Callable[..., str]

#: 删掉这条会话当前用的 opencode session，返回被删掉的 id（本来没有则 ``None``）。
DropSession = Callable[..., str | None]



# ----------------------------------------------------------------------
# 斜杠命令
# ----------------------------------------------------------------------
class CommandHandler:
    """斜杠命令的全部逻辑：认命令、分支、回话。

    与 :class:`~opencode_bridge.core.BridgeCore` 之间只有 :meth:`__init__` 里那九个
    注入的协作者 —— **没有** core 引用、也不读 core 的任何私有状态，所以这一整块
    能脱离 core 单独测（见 ``tests/test_commands.py``）。

    ``/model`` 的参数解析与模型目录缓存在
    :mod:`opencode_bridge.session_model` 里、``/delivery retry`` 的补投逻辑在
    :mod:`opencode_bridge.delivery_retry` 里，这里仍然只是转发（同一次拆分的前一步，
    边界没变）。命令**绝不**写进收件箱这件事也不在这里：落盘由
    :meth:`~opencode_bridge.core.BridgeCore.on_inbound` 决定要不要做，命令自己从不经过
    ``prompt()``。
    """

    def __init__(
        self,
        *,
        client: OpenCodeClient,
        config: Config,
        conversation_state: ConversationState,
        model_command: SessionModelCommand,
        delivery_retry: DeliveryRetryCommand,
        ensure_session: EnsureSession,
        drop_session: DropSession,
        send_text: SendText,
        permission_ledger: PermissionLedger,
        permission_timeout: PermissionTimeoutWatcher,
    ) -> None:
        """十个协作者全部由 core 注入，本类不自己去找。

        ``client`` 只用到 ``get_session`` / ``interrupt`` / ``reply_permission``；
        ``conversation_state`` 只用到 ``get_session`` / ``set_meta``；
        ``permission_ledger`` 只读到 :meth:`PermissionLedger.status_of`、写到
        :meth:`PermissionLedger.record_answered`（见
        :mod:`opencode_bridge.permission_ledger`）；
        ``permission_timeout`` 只在一个时刻用到：一条真回复落地后 `cancel` 掉本地
        超时计时器（见 :mod:`opencode_bridge.permission_timeout`）；
        后三个是 core 那边的函数（建会话、删会话、发信），按**可调用对象**注入，
        所以本类拿不到 core 本身。
        """
        self._client = client
        self._config = config
        self._conversation_state = conversation_state
        self._model_command = model_command
        self._delivery_retry = delivery_retry
        self._ensure_session = ensure_session
        self._drop_session = drop_session
        self._send_text = send_text
        self._permission_ledger = permission_ledger
        self._permission_timeout = permission_timeout
        #: ``/approve`` / ``/deny`` 的决策语义（见
        #: :mod:`opencode_bridge.command_permission`），用本类已注入的协作者
        #: 构造；对外的命令表仍指向本类的两条转发。
        self._permission = PermissionCommand(
            client=client,
            conversation_state=conversation_state,
            permission_ledger=permission_ledger,
            permission_timeout=permission_timeout,
            send_text=send_text,
        )

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------
    def handle_command(
        self, conversation_id: str, adapter: Adapter, text: str
    ) -> None:
        """认一条 ``/xxx`` 并执行；命令跑挂了也要回一句人话，不能静默。

        命令失败时统一回 ``命令执行失败: <原因>``（``kind="error"``），而不是把
        异常抛给入站线程 —— 那会让用户看到"什么都没发生"。
        """
        tokens = text.split(None, 1)
        raw_name = tokens[0][1:]  # strip "/"
        args = tokens[1].strip() if len(tokens) > 1 else ""
        # Telegram group commands may carry a bot suffix: /new@your_bot
        name = raw_name.split("@", 1)[0].lower()
        args = _clean(args)

        handler = {
            "help": self._cmd_help,
            "setup": self._cmd_setup,
            "new": self._cmd_new,
            "reset": self._cmd_new,
            "stop": self._cmd_stop,
            "status": self._cmd_status,
            "model": self._cmd_model,
            "delivery": self._cmd_delivery,
            "cd": self._cmd_cd,
            "approve": self._cmd_approve,
            "allow": self._cmd_approve,
            "deny": self._cmd_deny,
        }.get(name)
        if handler is None:
            self._send_text(
                conversation_id,
                f"未知命令 {raw_name}，发送 /help 查看用法。",
                kind="error",
                adapter=adapter,
            )
            return
        try:
            handler(conversation_id, adapter, args)
        except OpenCodeError as exc:
            logger.warning("command /%s failed: %s", name, exc)
            self._send_text(
                conversation_id,
                f"命令执行失败: {exc}",
                kind="error",
                adapter=adapter,
            )
        except Exception as exc:
            logger.exception("command /%s failed", name)
            self._send_text(
                conversation_id,
                f"命令执行失败: {exc}",
                kind="error",
                adapter=adapter,
            )

    # ------------------------------------------------------------------
    # 各条命令
    # ------------------------------------------------------------------
    def _cmd_help(self, conversation_id: str, adapter: Adapter, args: str) -> None:
        self._send_text(conversation_id, HELP_TEXT, adapter=adapter)

    def _cmd_setup(
        self, conversation_id: str, adapter: Adapter, args: str
    ) -> None:
        """``/setup`` menu / ``/setup <telegram|slack|discord|1|2|3>`` guide."""
        token = args.strip().split(None, 1)[0] if args.strip() else ""
        if not token:
            self._send_setup_menu(conversation_id, adapter)
            return
        platform = _SETUP_ALIASES.get(token.lower())
        if platform is None:
            self._send_text(
                conversation_id, SETUP_INVALID_TEXT, kind="error",
                adapter=adapter,
            )
            return
        self._send_text(
            conversation_id, _setup_guide(platform), kind="text",
            adapter=adapter,
        )

    def _send_setup_menu(self, conversation_id: str, adapter: Adapter) -> None:
        """Plain-text chooser + inline buttons (best effort, never raises).

        ``send`` only carries text; Telegram attaches the keyboard through
        ``edit``, so the menu is sent first and then re-edited with buttons.
        A failed button edit simply leaves the (already usable) text menu.
        """
        handle = self._send_text(
            conversation_id, SETUP_MENU_TEXT, kind="text", adapter=adapter
        )
        if handle is None:
            return
        out = Outbound(
            conversation_id=conversation_id,
            text=SETUP_MENU_TEXT,
            kind="text",
            buttons=tuple(
                Button(label=label, data=f"setup:{key}")
                for key, label in _SETUP_BUTTON_LABELS
            ),
        )
        try:
            adapter.edit(handle, out)
        except Exception:
            logger.exception("attaching /setup inline buttons failed")

    def _cmd_new(self, conversation_id: str, adapter: Adapter, args: str) -> None:
        self._drop_session(conversation_id, platform=adapter.name)
        session_id = self._ensure_session(conversation_id, platform=adapter.name)
        self._send_text(
            conversation_id,
            f"已新建会话 {session_id[:12]}",
            adapter=adapter,
            session_id=session_id,
        )

    def _cmd_stop(self, conversation_id: str, adapter: Adapter, args: str) -> None:
        session_id = self._conversation_state.get_session(
            conversation_id, platform=adapter.name
        )
        if not session_id:
            self._send_text(conversation_id, "当前没有会话。", adapter=adapter)
            return
        self._client.interrupt(session_id)  # OpenCodeError -> caught by caller
        self._send_text(
            conversation_id,
            "已请求中断当前任务。",
            adapter=adapter,
            session_id=session_id,
        )

    def _cmd_status(self, conversation_id: str, adapter: Adapter, args: str) -> None:
        session_id = self._conversation_state.get_session(
            conversation_id, platform=adapter.name
        )
        if not session_id:
            self._send_text(conversation_id, "当前没有会话。", adapter=adapter)
            return
        session = self._client.get_session(session_id)
        model = session.get("model")
        if isinstance(model, dict):
            model_text = "/".join(
                str(model.get(key))
                for key in ("providerID", "id")
                if model.get(key)
            ) or "?"
        else:
            model_text = str(model or "?")
        tokens = _as_dict(session.get("tokens"))
        directory = _as_dict(session.get("location")).get(
            "directory", self._config.opencode_directory
        )
        lines = [
            "会话状态",
            f"session_id: {session.get('id') or session_id}",
            f"agent: {session.get('agent') or '-'}",
            f"model: {model_text}",
            f"cost: {session.get('cost', 0)}",
            f"tokens: input={tokens.get('input', 0)} "
            f"output={tokens.get('output', 0)}",
            f"directory: {directory or '-'}",
        ]
        self._send_text(
            conversation_id,
            "\n".join(lines),
            adapter=adapter,
            session_id=session_id,
        )

    def _cmd_cd(self, conversation_id: str, adapter: Adapter, args: str) -> None:
        if not args:
            self._send_text(
                conversation_id,
                "用法: /cd <目录>   例如 /cd /data/work",
                kind="error",
                adapter=adapter,
            )
            return
        directory = args
        self._conversation_state.set_meta(conversation_id, "directory", directory)
        self._drop_session(conversation_id, platform=adapter.name)
        self._ensure_session(conversation_id, platform=adapter.name)
        self._send_text(conversation_id, f"已切换到 {directory}", adapter=adapter)

    def _cmd_model(self, conversation_id: str, adapter: Adapter, args: str) -> None:
        """``/model`` —— 逻辑在 :mod:`opencode_bridge.session_model`，这里只转发。"""
        reply = self._model_command.reply_for(conversation_id, args)
        self._send_text(
            conversation_id, reply.text, kind=reply.kind, adapter=adapter,
            session_id=reply.session_id,
        )

    def _cmd_delivery(self, conversation_id: str, adapter: Adapter, args: str) -> None:
        """``/delivery`` —— 逻辑在 :mod:`opencode_bridge.delivery_retry`，这里只转发。

        :class:`~opencode_bridge.delivery_retry.DeliveryReply` 不带 ``session_id``
        （这条命令没有会话可指），所以这里比 :meth:`_cmd_model` 少一个参数。
        """
        reply = self._delivery_retry.reply_for(conversation_id, args)
        self._send_text(conversation_id, reply.text, kind=reply.kind, adapter=adapter)


    def _cmd_approve(
        self, conversation_id: str, adapter: Adapter, args: str
    ) -> None:
        """``/approve`` —— 决策语义在 :mod:`opencode_bridge.command_permission`。"""
        self._permission._cmd_approve(conversation_id, adapter, args)

    def _cmd_deny(
        self, conversation_id: str, adapter: Adapter, args: str
    ) -> None:
        """``/deny`` —— 决策语义在 :mod:`opencode_bridge.command_permission`。"""
        self._permission._cmd_deny(conversation_id, adapter, args)

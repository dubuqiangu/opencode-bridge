"""IM 侧斜杠命令：``/help`` ``/setup`` ``/new`` ``/stop`` ``/status`` ``/cd``
``/approve`` ``/deny``，以及 ``/model`` 的转发。

这一整块原先是 :class:`~opencode_bridge.core.BridgeCore` 的十几个私有方法。搬出来
是因为那个类已经远超 AGENTS.md §5.1 的阈值（方法数 ~15+ / 类自身代码行 ~250+），
而命令处理恰好是其中**耦合最浅**的一块：它对 core 的依赖只有七个协作者
（见 :class:`CommandHandler`），搬出去之后既不用碰 core 的运行期状态
（``_turns`` / ``_sid_conv`` / ``_lock`` 那一摊），又能脱离 core 单独测试。

搬法与同一次拆分的前一步一致：``/model`` 已经在
:mod:`opencode_bridge.session_model` 里，core 侧只留一个转发；这里同理，
:class:`~opencode_bridge.core.BridgeCore` 只留 :meth:`~opencode_bridge.core.BridgeCore._handle_command`
一个转发点（``on_inbound`` 的调用点不变）。

冻结文案（``/help``、``/setup`` 引导）**跟着逻辑一起搬**，因为它们的真源就是这条命令
路径；:mod:`opencode_bridge.core` 从这里**继续再导出**这些名字
（``opencode_bridge.__main__`` 的 ``--setup`` 与测试都按
``from opencode_bridge.core import ...`` 取它们，导入路径不能变）。
:meth:`~opencode_bridge.core.BridgeCore.on_callback` 处理 ``setup:<platform>``
按钮回调时读的是同一份冻结文案，因此它也 import 这里，而不是复制一份。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable

from .adapters import Adapter
from .config import DEFAULT_CONFIG_NAME, Config
from .conversation_keys import ConversationState
from .hooks import Button, MsgHandle, Outbound
from .normalize import _as_dict, _clean
from .opencode_client import OpenCodeClient, OpenCodeError
from .session_model import SessionModelCommand

__all__ = [
    "CommandHandler",
    "HELP_TEXT",
    "SETUP_MENU_TEXT",
    "setup_platforms",
    "setup_reply",
]

logger = logging.getLogger("opencode_bridge.commands")

HELP_TEXT = """\
可用命令：
/help                       显示本帮助
/setup [平台]               三个平台接入引导（Telegram / Slack / Discord）
/new  /reset                新建会话（丢弃当前上下文）
/stop                       中断当前正在执行的任务
/status                     查看当前会话状态
/model [provider/id]         查看或切换当前会话使用的模型
/cd <目录>                  切换工作目录并新建会话
/approve <请求ID> [always]  允许权限请求（always = 总是允许）
/deny <请求ID>              拒绝权限请求
直接发送文本即可与 agent 对话。
安全提示：桥接进程拥有与你相同的本地权限，请仅在可信环境运行，
并务必为适配器配置 allowed_chat_ids 白名单。"""

#: ``/setup`` with no argument: the platform chooser (frozen copy).
SETUP_MENU_TEXT = """\
选择要接入的平台：
1) Telegram —— 支持双向对话
2) Slack —— 支持双向对话
3) Discord —— 支持双向对话

回复 /setup 1、/setup 2 或 /setup 3 也可直接查看。"""

#: ``/setup <bad>``: same list, framed as a usage hint (frozen copy).
SETUP_INVALID_TEXT = """\
无法识别的平台。可接入的平台有：
1) Telegram —— 支持双向对话
2) Slack —— 支持双向对话
3) Discord —— 支持双向对话

用法: /setup 1|2|3 或 /setup telegram|slack|discord"""

#: Accepted ``/setup`` arguments -> canonical platform key (lower-cased first).
_SETUP_ALIASES = {
    "1": "telegram",
    "telegram": "telegram",
    "2": "slack",
    "slack": "slack",
    "3": "discord",
    "discord": "discord",
}

#: Inline buttons offered on the menu (adapter may ignore them).
_SETUP_BUTTON_LABELS = (("telegram", "Telegram"), ("slack", "Slack"),
                         ("discord", "Discord"))


def _config_path_hint() -> str:
    """Absolute config-file path for the ``/setup`` guides (runtime only).

    Mirrors :meth:`Config.load`'s search chain: honour ``OPENCODE_BRIDGE_CONFIG``
    when it points at a real file, else ``<cwd>/config.json`` when present,
    else fall back to ``<package root>/config.json`` with an explanatory note.
    Never raises and never hardcodes a machine-specific path.
    """
    def bridge_root() -> str:
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    env_path = (os.environ.get("OPENCODE_BRIDGE_CONFIG") or "").strip()
    if env_path and os.path.isfile(env_path):
        return os.path.abspath(env_path)
    cwd_path = os.path.join(os.getcwd(), DEFAULT_CONFIG_NAME)
    if os.path.isfile(cwd_path):
        return os.path.abspath(cwd_path)
    return (
        os.path.join(bridge_root(), DEFAULT_CONFIG_NAME)
        + "（未找到已生效的配置文件，以上为桥接目录默认位置）"
    )


#: Frozen per-platform onboarding guides (factual copy; do not reword).
_SETUP_GUIDES = {
    "telegram": """\
1. 打开 Telegram，找 @BotFather → 发送 /newbot
2. 依次设置显示名、用户名（必须以 bot 结尾），复制返回的 token（形如 123456789:AA...)
3. 找 @userinfobot → 发送任意一句话 → 复制返回的纯数字 chat id
4. 编辑配置文件：
     "adapters": {
       "telegram": { "bot_token": "123456789:AA...", "allowed_chat_ids": [123456789] }
     }
   注意 allowed_chat_ids 是数组，数字不要加引号
5. 执行 opencode service restart
6. 在 Telegram 给你的 bot 发一句 hi，收到回复即成功""",
    "slack": """\
1. 打开 https://api.slack.com/apps → Create New App → From scratch → 选 workspace
2. 左侧 Socket Mode → 打开 Enable Socket Mode
3. 左侧 Basic Information → App-Level Tokens → Generate Token and Scopes
   → 命名 → 勾选 connections:write → 复制（xapp- 开头，入站必需）
4. 左侧 OAuth & Permissions → Scopes → Bot Token Scopes → Add an OAuth Scope，添加
   chat:write、channels:history、im:history
   （要 @ 才响应加 app_mentions:read；用私有频道加 groups:history）
5. 同页顶部 Install to Workspace → Allow → 复制 Bot User OAuth Token（xoxb- 开头）
   注意：之后每改一次 scope，都要回来点一次 Reinstall to Workspace
6. 左侧 Event Subscriptions → 打开 Enable Events
   → Subscribe to bot events → Add Bot User Event → 添加 message.channels、message.im
7. 编辑配置文件：
     "adapters": { "slack": { "bot_token": "xoxb-...", "app_token": "xapp-..." } }
8. 在目标频道输入 /invite @你的bot（私有频道同样用 /invite；私聊可直接发消息）
9. 执行 opencode service restart
10. 在频道里发一句普通文字，收到回复即成功

两个常见坑：
· Event Subscriptions 没打开、或事件没加在 bot events 下，会「静默收不到」且不报错
· 只填 bot_token 也能启动，但那只发不收（入站必须有 app_token）""",
    "discord": """\
1. 打开 https://discord.com/developers/applications → New Application → 左侧 Bot
2. Reset Token → 复制 token
3. 同一页把 Privileged Gateway Intents 下的 Message Content Intent 打开（必需）
4. 左侧 OAuth2 → URL Generator → 勾选 scope: bot → Permissions: Send Messages
5. 用生成的 URL 把 bot 邀请进你的服务器
6. 编辑配置文件：
     "adapters": { "discord": { "bot_token": "..." } }
7. 执行 opencode service restart
8. 在频道里发一句普通文字，收到回复即成功

两个常见坑：
· 第 3 步的开关不开，网关会直接拒绝连接（close 4014），日志里会写明原因
· bot 必须已被邀请进频道，否则发消息报 not_in_channel""",
}


def _setup_guide(platform: str) -> str:
    """Compose the ``/setup <platform>`` reply: path hint + frozen guide."""
    return (
        f"配置文件： {_config_path_hint()}\n"
        "改完后执行： opencode service restart\n\n"
        + _SETUP_GUIDES[platform]
    )


def setup_reply(platform: str | None = None) -> str:
    """Public entry for the ``/setup`` onboarding copy.

    Reused by :mod:`opencode_bridge.__main__` (``--setup``) so the CLI and the
    in-bot command share **one** source of truth. ``platform`` may be ``None``
    (menu), a canonical key or any alias in :data:`_SETUP_ALIASES`.
    """
    token = (platform or "").strip()
    if not token:
        return SETUP_MENU_TEXT
    key = _SETUP_ALIASES.get(token.lower())
    return _setup_guide(key) if key is not None else SETUP_INVALID_TEXT


def setup_platforms() -> tuple[tuple[str, str], ...]:
    """``(key, label)`` pairs for the platform chooser, in menu order."""
    return _SETUP_BUTTON_LABELS


# ----------------------------------------------------------------------
# 权限回信：决策词与用法文案（两条命令共用，所以放在模块级）
# ----------------------------------------------------------------------
#: ``/approve`` 第二个 token 的取值。**只有** ``/approve`` 有这个位置；
#: ``/deny`` 的决策恒为 ``reject``，多一个 token 就是用错命令了。
_APPROVE_CHOICES = ("once", "always")

#: 参数写错时回的那一句（``/approve`` 与 ``/deny`` 共用同一份，省得两边说法不一）。
_PERMISSION_USAGE = "用法: /approve <请求ID> [always]  或  /deny <请求ID>"


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

    与 :class:`~opencode_bridge.core.BridgeCore` 之间只有 :meth:`__init__` 里那七个
    注入的协作者 —— **没有** core 引用、也不读 core 的任何私有状态，所以这一整块
    能脱离 core 单独测（见 ``tests/test_commands.py``）。

    ``/model`` 的参数解析与模型目录缓存在
    :mod:`opencode_bridge.session_model` 里，这里仍然只是转发（同一次拆分的前一步，
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
        ensure_session: EnsureSession,
        drop_session: DropSession,
        send_text: SendText,
    ) -> None:
        """七个协作者全部由 core 注入，本类不自己去找。

        ``client`` 只用到 ``get_session`` / ``interrupt`` / ``reply_permission``；
        ``conversation_state`` 只用到 ``get_session`` / ``set_meta``；
        后三个是 core 那边的函数（建会话、删会话、发信），按**可调用对象**注入，
        所以本类拿不到 core 本身。
        """
        self._client = client
        self._config = config
        self._conversation_state = conversation_state
        self._model_command = model_command
        self._ensure_session = ensure_session
        self._drop_session = drop_session
        self._send_text = send_text

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
        self._client.reply_permission(session_id, request_id, decision)
        self._send_text(
            conversation_id,
            f"已回复权限请求 {request_id}: {decision}",
            adapter=adapter,
            session_id=session_id,
        )

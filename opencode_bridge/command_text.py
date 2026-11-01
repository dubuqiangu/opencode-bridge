"""``/help`` / ``/setup`` 的冻结文案与接入引导（真源）。

从 :mod:`opencode_bridge.commands` 拆出（AGENTS.md §5）：这一整块是**用户可见的
冻结文案** —— ``/help`` 文本、``/setup`` 菜单/用法文案、三个平台的接入引导。
它与命令分发逻辑无关，改动只应发生在文案上。

文案的真源就在这里；:mod:`opencode_bridge.commands` 只做再导出，
``opencode_bridge.core`` / ``__main__`` 与测试继续按原导入路径取名字。
"""

from __future__ import annotations

import os

from .config import DEFAULT_CONFIG_NAME


HELP_TEXT = """\
可用命令：
/help                       显示本帮助
/setup [平台]               三个平台接入引导（Telegram / Slack / Discord）
/new  /reset                新建会话（丢弃当前上下文）
/stop                       中断当前正在执行的任务
/status                     查看当前会话状态
/model [provider/id]         查看或切换当前会话使用的模型
/cd <目录>                  切换工作目录并新建会话
/delivery retry             列出待补投的消息，或按序号补投一条
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
#:
#: ⚠️ **2026-10-05 例外：授权那一行是"承重"的，不是措辞。**
#: Slack / Discord 的引导原先**只写 token、不写白名单**，于是照着它配完的用户得到一个
#: **完全开放**的桥接 —— 而 ``config.example.json`` 抄的就是 ``[]``，安装脚本又把它
#: 原样落盘，所以"什么都不改"就是全开。三个平台现在都给出**非空**
#: ``allowed_chat_ids``，并各带一句「留空 = 全开」。
#:
#: 这条之所以守得住：``tests/test_allowlist_visibility.py`` **把每份引导里的配置片段
#: 真的 ``json.loads`` 一遍**再断言白名单非空 —— 只对字符串常量做子串匹配的话，
#: 引导写错了测试照样绿（JSON 片段能解析这件事本身就被守住了）。
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
   ⚠️ 留空数组 = **全开**：任何能私聊 bot 的人都能驱动它
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
7. 编辑配置文件（把 C0123456789 换成你频道的真实 ID：频道名右侧「…」→ 复制频道 ID）：
     "adapters": {
       "slack": { "bot_token": "xoxb-...", "app_token": "xapp-...", "allowed_chat_ids": ["C0123456789"] }
     }
   ⚠️ allowed_chat_ids 留空数组 = **全开**：任何能私聊或 @ 到 bot 的人都能驱动它
8. 在目标频道输入 /invite @你的bot（私有频道同样用 /invite；私聊可直接发消息）
9. 执行 opencode service restart
10. 在频道里发一句普通文字，收到回复即成功

两个常见坑：
· Event Subscriptions 没打开、或事件没加在 bot events 下，会「静默收不到」且不报错
· 只填 bot_token 也能启动，但那只发不收（入站必须有 app_token）
· allowed_chat_ids 留空数组 = **全开**：任何能私聊或 @ 到 bot 的人都能驱动它""",
    "discord": """\
1. 打开 https://discord.com/developers/applications → New Application → 左侧 Bot
2. Reset Token → 复制 token
3. 同一页把 Privileged Gateway Intents 下的 Message Content Intent 打开（必需）
4. 左侧 OAuth2 → URL Generator → 勾选 scope: bot → Permissions: Send Messages
5. 用生成的 URL 把 bot 邀请进你的服务器
6. 编辑配置文件（把 123456789012345678 换成你频道的真实 ID：右键频道 → 复制频道 ID）：
     "adapters": {
       "discord": { "bot_token": "...", "allowed_chat_ids": ["123456789012345678"] }
     }
   ⚠️ allowed_chat_ids 留空数组 = **全开**：任何能私聊或 @ 到 bot 的人都能驱动它
7. 执行 opencode service restart
8. 在频道里发一句普通文字，收到回复即成功

两个常见坑：
· 第 3 步的开关不开，网关会直接拒绝连接（close 4014），日志里会写明原因
· bot 必须已被邀请进频道，否则发消息报 not_in_channel
· allowed_chat_ids 留空数组 = **全开**：任何能私聊或 @ 到 bot 的人都能驱动它""",
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

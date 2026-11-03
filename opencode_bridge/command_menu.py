"""斜杠命令的**单一真源**：命令名 → 处理方法 + Telegram 命令菜单。

从 :meth:`opencode_bridge.commands.CommandHandler.handle_command` 里那块 dict 字面量
提取出来（AGENTS.md §5：让命令清单不再散落在"分发"与"菜单"两处）。本模块**不导入**
adapters，所以适配器（``adapters/telegram.py``）可以只依赖它来构造 ``setMyCommands``
的命令表，不会和 ``commands.py`` 对 adapters 的依赖绕成回环。

单一真源 = :data:`COMMAND_HANDLERS`（命令名 → :class:`CommandHandler` 上的方法名）。
三个消费者全部由它派生，⛔ 谁也不再各记一份命令清单：

* 分发（:meth:`CommandHandler.handle_command`）经 ``getattr(self, method_name)`` 取处理
  方法；
* :data:`CANONICAL_COMMAND_NAMES` —— 去掉别名后的规范命令（别名 ``reset`` / ``allow``
  仍可分发，只是不进菜单）；
* :func:`command_menu_entries` —— Telegram ``setMyCommands`` 的 ``commands`` 数组，
  说明文案逐条取自 :data:`opencode_bridge.command_text.HELP_TEXT`（⛔ 不新造文案）。
"""

from __future__ import annotations

#: 命令名 → :class:`CommandHandler` 上处理它那个方法的**方法名**。
#:
#: 这是命令清单的**唯一真源**。别名（``reset`` → ``_cmd_new``、``allow`` →
#: ``_cmd_approve``）与规范命令指向同一个方法 —— 与迁移前的 dict 字面量逐字等价。
COMMAND_HANDLERS: dict[str, str] = {
    "help": "_cmd_help",
    "setup": "_cmd_setup",
    "new": "_cmd_new",
    "reset": "_cmd_new",
    "stop": "_cmd_stop",
    "status": "_cmd_status",
    "model": "_cmd_model",
    "delivery": "_cmd_delivery",
    "cd": "_cmd_cd",
    "approve": "_cmd_approve",
    "allow": "_cmd_approve",
    "deny": "_cmd_deny",
}


def _canonical_command_names() -> tuple[str, ...]:
    """按插入顺序取每个处理方法**首次**出现的命令名 ⇒ 别名被略去、顺序稳定。"""
    seen_method_names: set[str] = set()
    canonical_names: list[str] = []
    for command_name, method_name in COMMAND_HANDLERS.items():
        if method_name in seen_method_names:
            continue
        seen_method_names.add(method_name)
        canonical_names.append(command_name)
    return tuple(canonical_names)


#: 菜单里列出的规范命令（别名不列，免得同一个动作在菜单里出现两次）。
CANONICAL_COMMAND_NAMES: tuple[str, ...] = _canonical_command_names()

#: 规范命令 → 菜单里的简短中文说明。措辞逐条取自
#: :data:`opencode_bridge.command_text.HELP_TEXT` 里对应那一行的说明文字。
COMMAND_MENU_DESCRIPTIONS: dict[str, str] = {
    "help": "显示本帮助",
    "setup": "三个平台接入引导（Telegram / Slack / Discord）",
    "new": "新建会话（丢弃当前上下文）",
    "stop": "中断当前正在执行的任务",
    "status": "查看当前会话状态",
    "model": "查看或切换当前会话使用的模型",
    "cd": "切换工作目录并新建会话",
    "delivery": "列出待补投的消息，或按序号补投一条",
    "approve": "允许权限请求（always = 总是允许）",
    "deny": "拒绝权限请求",
}


def command_menu_entries() -> list[dict[str, str]]:
    """Telegram ``setMyCommands`` 的 ``commands`` 数组（顺序 = :data:`CANONICAL_COMMAND_NAMES`）。

    命令名全部来自 :data:`COMMAND_HANDLERS` 的规范子集；说明文案若缺失会在此处
    ``KeyError``（由 ``tests/test_telegram_command_menu.py`` 的漂移守卫当场抓住，
    ⛔ 不静默漏掉一条命令）。
    """
    return [
        {"command": command_name, "description": COMMAND_MENU_DESCRIPTIONS[command_name]}
        for command_name in CANONICAL_COMMAND_NAMES
    ]

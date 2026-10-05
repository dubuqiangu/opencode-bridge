"""日志里怎么写平台侧 id —— 唯一一种 C2 能**无歧义**识别、因而能脱敏的形式。

为什么需要这一个模块
====================

:mod:`opencode_bridge.redaction`（C2 脱敏引擎）**只**脱敏带平台前缀的
``platform:local_id``，**裸的 local id 一律按形状放行**。这不是偷懒，是那条规则
写明的取舍（原文照录 ``redaction.py`` 的「刻意不做的」）：

    discord 的 17~20 位雪花号**就是**一个合法的纳秒时间戳 ⇒ 任何涵盖它的数字
    范围也涵盖时间戳/端口/行号；按形状脱敏会把日志正文一起毁掉。

所以"适配器在拒绝一条入站消息时该打什么"**不是审美问题，是可判定的问题**：
**带前缀的打 → C2 洗成 ``<platform>:conv#<摘要>``；裸的打 → 明文留在日志里。**
同一个模块 C2 已经点名过这个漏，并把根治方式写成"让适配器改记带前缀的
``conversation_id``"（原句：``要根治得让适配器改打 conversation_id，而
adapters/** 本次不许动``）。本模块就是那句话的落地。

它必须**只有一份**的理由与 C2 拒绝"逐个适配器打补丁"同源：让 13 个适配器各自
记得加前缀，下一个适配器就会忘 —— 而"忘了"在这里的后果是静默的（测试全绿、
日志照写明文）。

换来的是**可关联性，不是拿隐私换可运维性**
========================================

C2 把 ``telegram:12345`` 洗成 ``telegram:conv#<6 位>-<6 位>``，而它对会话 id
用**带进程内随机密钥的 HMAC 摘要**（而不是遮蔽）**就是为了可关联**：同一个人
横跨多行日志仍然指向同一个 ``conv#``。所以改完之后：

* 日志里不再有**裸**的 principal / author id；
* **同一个会话**在多行日志里仍然指向同一个 ``conv#`` ⇒ 陌生人刷屏时仍能看出
  "是不是同一个人"，且"这个 chat 是不是刚被我拒过"也仍然查得到；
* 同一进程里，日志里的 ``conv#`` 与 :class:`~opencode_bridge.hooks.Inbound` 真正
  带的那串 ``conversation_id`` 指的是同一个值 ⇒ 排障时"日志说的"与"会话表说的"
  对得上。

⚠️ **诚实边界（明说）**：C2 的会话 id 规则**只有一个标签** —— ``conv#``。所以
本模块**无法**给"作者 / 发件人"这类**不是会话**的 id 换一个标签：
``discord:<author_id>`` 脱敏后同样显示成 ``discord:conv#<摘要>``。区分
"哪个字段是会话、哪个字段是人"靠的是**日志行里的字段名**（``channel=`` vs
``author=``），不是那个标签。这是本方案唯一的代价，它换来的是"作者仍可跨行
关联"；反过来把作者整个删掉才是拿可运维性换隐私 —— 一个话多的陌生人能用自己的
作者 id 灌满日志，正是这次要治的病。
"""

from __future__ import annotations

from typing import Any

from ..identity import InvalidConversationId, format_id

__all__ = ["MISSING_ID", "redactable_id"]

#: 平台侧**没给出** id 时打进日志的占位。
#:
#: 刻意**不带平台前缀**：``platform:?`` 会被脱敏成 ``conv#<摘要>("?")``，于是
#: "这里本来就没有 id"这条信息被洗成一个看起来很像真摘要的东西 —— 而
#: "缺 channel_id"那一支恰恰要靠它才看得见。裸的 ``?`` 不匹配任何规则
#: （会话 id 规则要求冒号后**不是**空格也不是非 ``[a-z]+#`` 的空），原样留下。
MISSING_ID = "?"


def redactable_id(platform: str, local_id: Any) -> str:
    """``platform:local_id`` —— 日志里唯一能被 C2 认出来的形式。

    与各适配器自己的 ``_conversation_id`` / ``conversation_id_for``
    **产出同一个字符串**，这是本函数存在的理由：日志里那一段必须与真正会进
    :attr:`Inbound.conversation_id` 的那个值一致，否则"日志与会话表对不上"
    就成了排障时的假线索（``tests/test_inbound_log_ids.py`` 把这条相等性逐平台
    钉住）。

    三处刻意的行为：

    1. **绝不抛**。适配器那些 ``_conversation_id`` 对空 local 段抛
       :class:`~opencode_bridge.identity.InvalidConversationId`（那是对的：拼不出
       会话标识就不该硬编一个），而**日志路径不能抛** —— "缺 channel_id"那一支
       恰恰要把空值打出来。空值一律给 :data:`MISSING_ID`。
    2. **失败关闭**：平台键非法时（:func:`~opencode_bridge.identity.format_id`
       抛）也返回 :data:`MISSING_ID`，**绝不**退回打原值。
       ⚠️ 所以 :data:`MISSING_ID` 有"平台没给 id"和"平台键非法"两种成因 ——
       同一个符号两种含义是不可接受的歧义，所以 ``tests/`` 里另有一条断言把
       **13 个已注册适配器的 ``name`` 全是合法平台键**钉住，让第二种成因
       **结构上不可达**。
    3. **已是 ``platform:`` 前缀的入参原样返回**，于是对已带前缀的值幂等：
       ``qqbot`` 的 conversation 段本身就是 ``qqbot:<scope>:<target>``，拼两次
       不能变成 ``qqbot:qqbot:group:...``。
       （前提：平台侧 id 本身不会以 ``<platform>:`` 开头。13 个平台的 id 形状
       —— 数字 / 大写字母数字 / 邮箱 / URL / ``#chan`` / ``light.kitchen`` /
       ``!abc:server`` / ``group:openid`` —— 都不满足这个前提；
       ``tests/test_inbound_log_ids.py`` 把已注册的取值逐个喂过。）
    """
    prefix = str(platform or "").strip()
    text = str(local_id or "").strip()
    if not text:
        return MISSING_ID
    if prefix and text.startswith(prefix + ":"):
        return text
    try:
        return format_id(prefix, text)
    except InvalidConversationId:
        return MISSING_ID

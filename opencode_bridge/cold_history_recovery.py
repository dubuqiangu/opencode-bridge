"""B 冷历史恢复答复：从 opencode 会话消息历史里找回最终 assistant 正文。

**借鉴面**（``docs/borrowing-catalog.md`` §1-C）：参考 ``deferred-delivery.ts`` 的
``recoverTurn`` —— 一次投递结果未知时，不重放（可能让 agent 跑两遍），而是回查
**冷历史**里那一轮最终 assistant 正文，直接投给用户。

**本桥的对接口径**（用户 2026-10-16 拍板）：

* ⛔ **不照搬 requestId 匹配** —— 失败路径拿不到 prompt 的 msg id，那条路不可走；
* 改为**按 ``queued.text`` + 时间序**定位：在消息列表里找一个 **user 消息**，其
  ``text`` 与收件箱里落盘的 prompt 正文**逐字节相同**，且它**之后**还有一条
  ``assistant`` 消息带着正文 —— 那一条的正文就是该轮最终答复。
* **同文本重发歧义**：同一正文出现多次时，取**时间序内首个匹配、且之后确有 assistant
  正文**的 user 消息（catalog 已确认「弱于参考、会歧义」，这是**确定性选择**并记知情代价）。

本模块是**纯函数域**：不 import 客户端、不做任何 I/O。取消息列表、投递、落盘都是
调用方（网关）注入的。
"""

from __future__ import annotations

from typing import Any

__all__ = ["find_reply_for_prompt"]


def _user_text(message: dict[str, Any]) -> str | None:
    """``{type: "user", text}`` 的正文；形状不对返回 ``None``。"""
    if message.get("type") != "user":
        return None
    text = message.get("text")
    return text if isinstance(text, str) else None


def _assistant_text(message: dict[str, Any]) -> str | None:
    """``{type: "assistant", content: [{type: "text", text}, ...]}`` 的正文。

    参考 ``assistant-text.ts`` 的拼法：取 ``content`` 里 ``type == "text"`` 的块，
    按序 join。没有文本块、或消息形状不符，返回 ``None``。
    """
    if message.get("type") != "assistant":
        return None
    content = message.get("content")
    if not isinstance(content, list):
        return None
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts) if parts else None


def find_reply_for_prompt(messages: list[dict], prompt_text: str) -> str | None:
    """按 ``prompt_text`` + 时间序，从消息历史里定位最终 assistant 正文。

    :param messages: 时间序消息列表（``client.messages()`` 的 ``data``，升序）。
    :param prompt_text: 收件箱里落盘的 prompt 正文（``QueuedPrompt.text``）。
    :returns: 定位到的 assistant 正文；**找不到返回 ``None``**（调用方回退既有告警）。

    **算法**（确定性）：

    1. 顺时间序逐条扫，记下**正在等答复**的那个候选 user 消息。
    2. 遇到 ``text == prompt_text`` 的 user 消息：若此刻没在等答复，就把它当候选
       （``awaiting_reply`` 置真）；否则保持现状（同文本重发，后发者优先级由
       「首个有后续答复」截断）。
    3. 遇到**别的** user 消息：上一段对话被切断，清掉候选。
    4. 遇到带正文的 assistant 消息：若此刻在等答复，它就是那轮最终答复，直接返回。
    5. 扫完没返回 ⇒ 找不到（回退既有「请重新发送一次」告警）。

    同文本重发取「首个匹配、且之后确有 assistant 正文」的命中 —— 这是拍板定的
    确定性选择（catalog 已记歧义知情代价）。
    """
    awaiting_reply = False
    for message in messages:
        user_text = _user_text(message)
        if user_text is not None:
            if user_text == prompt_text and not awaiting_reply:
                awaiting_reply = True
            else:
                awaiting_reply = False
            continue
        assistant_text = _assistant_text(message)
        if assistant_text is not None and awaiting_reply:
            return assistant_text
    return None
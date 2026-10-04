"""值的归一化：入站/命令/事件流三条路径**共用**的纯函数。

这三个函数原先住在 :mod:`opencode_bridge.core` 里，但它们不属于任何一边：

* :func:`_clean` —— 出站正文（``_send_text`` / ``_edit_progress`` / ``_finalize``）、
  入站正文（:meth:`~opencode_bridge.core.BridgeCore.on_inbound`）、事件里的错误文本、
  以及 ``/cd`` 这类命令的参数清洗，全都要过它；
* :func:`_as_dict` —— 事件分发读 ``event["data"]``、``/status`` 读
  ``session["tokens"]``，两边都要用它挡掉"字段缺失或类型不对"；
* :func:`trim_outer_whitespace` —— **只有**入站正文那一处用（见它自己的
  docstring：它顶替的是入站路径上那个无参 ``.strip()``）。放在这里是因为它与
  :func:`_clean` 是同一件事的两半 —— 一半管字符安全，一半管排版。

命令处理那一块搬进 :mod:`opencode_bridge.commands` 之后（AGENTS.md §5.1），
``core`` 与 ``commands`` 就**互相需要**这两个函数了：留在 ``core`` 里则
``commands`` 必须反向 import ``core``，那是个必然炸掉的循环 import
（谁先被导入，谁就拿不到 ``CommandHandler``）；两边各抄一份则是第二份真相。
所以它们落到这个叶子模块里，两边各自 import —— 单一实现、零循环，
且两边仍然能脱离对方单独测试。

函数名保持原样（``_clean`` / ``_as_dict``）而不顺手改成 ``clean_text`` 之类：
这次搬动是**纯结构改动**，名字一动，事件流路径上就有十几处调用点要跟着改，
评审就得多验一遍本该"零变化"的东西。
"""

from __future__ import annotations

from typing import Any

__all__ = ["_as_dict", "_clean", "trim_outer_whitespace"]


def _clean(text: Any) -> str:
    """Make ``text`` safe for any messaging platform.

    Drops ``\\x00`` and other C0 control characters (kept: ``\\n`` ``\\r``
    ``\\t``) and replaces undecodable byte sequences / lone surrogates.
    No markdown processing happens here — adapters send plain text.
    """
    if not isinstance(text, str):
        text = str(text)
    try:
        text = text.encode("utf-8", "replace").decode("utf-8")
    except Exception:  # pragma: no cover - extremely defensive
        text = text.encode("utf-8", "ignore").decode("utf-8", "ignore")
    return "".join(ch for ch in text if ch in "\n\r\t" or ch >= " ")


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def trim_outer_whitespace(text: Any) -> str:
    """Drop the blank padding around an inbound body, keeping real indentation.

    **这里修的是 :meth:`~opencode_bridge.inbound_gateway.InboundGateway.on_inbound`
    上那个无参 ``.strip()``。** 它去掉**前导**空白，于是粘贴的第一行被 dedent：

    ==========================  ==========================
    用户发来的                     agent 收到的（改之前）
    ==========================  ==========================
    ``'    def f():\\n ...'``    ``'def f():\\n ...'``
    ==========================  ==========================

    对一个**通往编程 agent** 的桥来说这是实打实的损坏：第一行 dedent 而第二行
    没有，于是 ``def f():`` 后面跟一个 8 空格的函数体 —— ``IndentationError``。
    agent 拿到的是**坏掉的代码**，还很可能"顺手修错"。而用户完全看不出是桥改的。

    **为什么不是 ``.lstrip()``**：那样会把第一行的缩进也吃掉 —— 正是要修的东西。
    **为什么不是 ``.rstrip()``**：那样开头的空行会留着，用户在聊天框里敲出来
    的那一段回车会变成 agent 上下文里的空行。

    所以是两件事分开做，判据是**"空白有意义，缩进更有意义"**：

    * 开头：丢掉**整行都是空白**的行，遇到第一行有内容的行就**逐字节**停下 ——
      那行自己的缩进属于用户，不动；
    * 结尾：整条尾巴上的空白（含换行）一律去掉。

    全空白的消息在这里变成 ``""``，于是 :meth:`on_inbound` 紧随其后的
    ``if not text`` 照旧把它丢掉 —— 那是这个函数**唯一真正被依赖**的旧行为，
    也是它当初存在的理由。

    ⚠️ 刻意**不**动行内的空白，也不碰 ``\\r``（:func:`_clean` 保留 ``\\r``，
    CRLF 的中间那部分与改动前一样原样送达）。窄到这个范围，才敢说是"只修了缩进"。
    """
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    if not text.strip():
        # 全空白：交给调用方的空串守卫去丢，这里不替它决定。
        return ""
    lines = text.split("\n")
    first_with_content = 0
    while not lines[first_with_content].strip():
        first_with_content += 1
    # ⚠️ 这里**不能**用 ``"".join`` 或对首行再 strip：那一行的缩进就是用户写的
    # 内容，dedent 它正是本次要修的损坏。
    return "\n".join(lines[first_with_content:]).rstrip()

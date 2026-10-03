"""值的归一化：命令路径与事件流路径**共用**的两个纯函数。

这两个函数原先住在 :mod:`opencode_bridge.core` 里，但它们不属于任何一边：

* :func:`_clean` —— 出站正文（``_send_text`` / ``_edit_progress`` / ``_finalize``）、
  入站正文（:meth:`~opencode_bridge.core.BridgeCore.on_inbound`）、事件里的错误文本、
  以及 ``/cd`` 这类命令的参数清洗，全都要过它；
* :func:`_as_dict` —— 事件分发读 ``event["data"]``、``/status`` 读
  ``session["tokens"]``，两边都要用它挡掉"字段缺失或类型不对"。

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

__all__ = ["_as_dict", "_clean"]


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

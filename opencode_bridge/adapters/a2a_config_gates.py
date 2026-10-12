"""A2A 配置闸域（§5 拆分：自 ``adapters/a2a.py`` 逐字移植）。

只承载配置取值的闸门函数：端口 / 正数旋钮 / 轮次上限 / 对端凭据表的解析与回落。
``adapters/a2a.py`` 顶部再进口全部名字 ⇒ 既有 import 路径与属性语义不变。

依赖方向：``a2a_config_gates`` → ``a2a_protocol``（只取常量）；零回 import 适配器。
"""

from __future__ import annotations

import logging
from typing import Any

from .a2a_protocol import (
    DEFAULT_MAX_TURNS,
    HARD_MAX_TURNS,
    MAX_BIND_PORT,
    MIN_BIND_PORT,
    UNCONFIGURED_PORT,
)

#: 与 ``adapters/a2a.py`` **共用同一个日志通道名**：搬走的函数原文里打的就是
#: ``a2a: …`` 前缀的告警，通道名一起搬 ⇒ 日志行为逐字节不变（§5 拆分纪律）。
logger = logging.getLogger("opencode_bridge.adapters.a2a")



# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------
def _coerce_port(value: Any) -> int:
    """配置里的端口 -> :data:`UNCONFIGURED_PORT` 表示**未配置**，``0`` 表示"由操作系统分配"。

    刻意区分 ``-1``（没配）与 ``0``（配了 0）：

    * 没配 -> :meth:`A2aAdapter.start` 拒绝启动并报错。端口是 A2A **被调方唯一不可
      省略**的东西（Agent Card 公布的 URL 含端口，随手挑一个默认值可能已被占用，
      而端口 0 每次重启都变、对端永远找不到我们）。宁可"起不来 + 明确报错"，
      也不要"偷偷挑一个"—— 状态要可观测，不能悄悄发生。
    * 配了 0 -> 绑到回环的临时端口。**这是测试用的合法配置**（与本项目 IRC / ws
      测试里 ``127.0.0.1:0`` 同一手法），生产不该用：重启即换端口。

    ⚠️ **"未配置"的取值范围只有这一个**（``-1``）：不是配置、类型非法、超出
    ``[0, 65535]``（含负数）**全部**折叠成它。因此"能不能跑"的判定只剩一条
    ``port >= 0``，而那正是 :meth:`A2aAdapter.config_runnable` 回答的依据。
    """
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return UNCONFIGURED_PORT
    return port if MIN_BIND_PORT <= port <= MAX_BIND_PORT else UNCONFIGURED_PORT


def _coerce_positive(value: Any, fallback: float, key: str = "") -> float:
    """正数旋钮的取值。**缺省** -> ``fallback``（静默）；**配了但非法** -> ``fallback``
    并**告警**。

    纪律：**回落成安全值 + 告警**（本仓库在 email 的 TLS 配置上确立的做法）。
    这里回落成默认值而不是"不限制" —— 一个写错的 ``reply_timeout`` 不该把防挂死
    的超时变成 0（那会让每个请求都立刻判失败）或无穷大（停机时才暴露）。

    ⚠️ "没配"与"配错了"必须分开：**没配时静默**，否则每次启动都会刷一条没意义的
    警告，日志里全是噪音，真正的配置错误反而看不见了。两条**配错**的路径
    （类型错 / 数值非正）都要告警 —— 只在一条上告警等于"有的配错会被告知、
    有的不会"，用户无法据此判断自己的配置是否生效。

    :param key: 配置键名，**必须出现在告警里** —— 只说"'nope' 不是数字"而不说
        是哪个键，用户得自己去猜是哪个旋钮出错了。
    """
    label = f"{key}=" if key else ""
    if value is None or (isinstance(value, str) and not value.strip()):
        return float(fallback)
    try:
        number = float(value)
    except (TypeError, ValueError):
        logger.warning(
            "a2a: 配置项 %s%r 不是数字（%r），已回落为 %s", label, value,
            type(value).__name__, fallback,
        )
        return float(fallback)
    if number != number or number <= 0:      # NaN 或非正
        logger.warning(
            "a2a: 配置项 %s%r 非法（非正数），已回落为 %s", label, value, fallback
        )
        return float(fallback)
    return number


def _coerce_turns(value: Any) -> int:
    """轮次上限。缺省 -> 默认值；**配了但非法/越界** -> 默认值/硬顶并告警。

    硬顶的理由：防回环是**为了**在配置写错时仍然生效而存在的。一个能被配置成
    "无限"的上限等于没有上限，而失效方式恰恰是最难发现的那种（agent 悄悄自问自答）。
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return DEFAULT_MAX_TURNS
    try:
        number = int(value)
    except (TypeError, ValueError):
        logger.warning(
            "a2a: max_turns=%r 不是整数，已回落为 %d", value, DEFAULT_MAX_TURNS
        )
        return DEFAULT_MAX_TURNS
    if number < 1:
        logger.warning("a2a: max_turns=%r 非法（<1），已回落为 %d", value, DEFAULT_MAX_TURNS)
        return DEFAULT_MAX_TURNS
    if number > HARD_MAX_TURNS:
        logger.warning(
            "a2a: max_turns=%d 超过硬顶 %d，已下调 —— 防回环不能被配置成无限，"
            "否则配置写错时的失效方式最隐蔽（agent 悄悄自问自答）。",
            number, HARD_MAX_TURNS,
        )
        return HARD_MAX_TURNS
    return number


def _parse_peer_tokens(value: Any) -> tuple[tuple[str, str], ...]:
    """``"alice:tok1,bob:tok2"``（或 dict）-> ``((tok1, "alice"), (tok2, "bob"))``。

    返回顺序是 ``(token, name)`` —— 比较的是 token，身份用的是 name。
    """
    pairs: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for name, token in value.items():
            if str(token or "").strip() and str(name or "").strip():
                pairs.append((str(token).strip(), str(name).strip()))
        return tuple(pairs)
    for chunk in str(value or "").split(","):
        if ":" not in chunk:
            continue
        name, _, token = chunk.partition(":")
        if name.strip() and token.strip():
            pairs.append((token.strip(), name.strip()))
    return tuple(pairs)


def _local_of(conversation_id: str) -> str:
    """从 ``conversation_id`` 取对端标识段（``a2a:<peer>`` -> ``<peer>``）。

    刻意用 ``partition`` 而不是 ``split(":")``：对端名里可能有冒号，砍掉会取错人。
    解析不出来时返回原串（宁可匹配不上、如实报"无处交付"，也不要猜错对端）。
    """
    text = str(conversation_id or "")
    platform, sep, local = text.partition(":")
    if sep and platform == "a2a":
        return local
    return text

"""Telegram 每聊天发送预算的**纯算术**域（自 ``telegram.py`` 抽出）。

## 为什么单独成模块

``telegram.py`` 已超 1100 物理行（§5 触发线 400），而"窗口预算"这块算术
**完全不需要适配器**：它只吃"每聊天的发送时刻表 + 现在时刻 + 窗口 + 上限"，
吐"还要等几秒"。⇒ 抽成纯函数的独立模块，既不继续加厚那个大文件，
也让这块算术能被单测**直接断言**（不依赖适配器、不读墙钟、无副作用）。

## 常数从哪来（⛔ 不是我们拍的）

Telegram Bot API 的限流按**每聊天每分钟新建消息数**计费，而不是按固定间隔
（我们此前用恒定 1.2s 连发分片 ≈ 每分钟 50 条，远超预算 ⇒ 长回复必撞 429）。
窗口长度与上限取自上游 ``xmanrui/dsh-im`` commit ``c36cc47`` 的
``SEND_WINDOW_MS = 60_000`` / ``DEFAULT_SEND_LIMIT_PER_MINUTE = 15``
（``MIN = 1`` / ``MAX = 30``）—— 那是平台定的预算，**不可配窗口长度**；
上限只做 1..30 的夹逼（配置写坏时回落默认，不静默采纳越界值）。

## 只有"新建消息"计费

上游列举的计费方法：``sendMessage`` / ``sendRichMessage`` / ``sendDocument``
/ ``sendPhoto``。**编辑、草稿、typing、reaction、回调应答不计费**，必须保持
不阻塞（否则一次流式改写就能把发送预算耗光）。
"""

from __future__ import annotations

import math
from typing import Any, Optional, Sequence

#: 滑动窗口长度（秒）。⛔ 不可配：Telegram 定的是「每聊天每分钟」预算。
SEND_WINDOW_SECONDS = 60.0
#: 每聊天每分钟可**新建**的消息条数（上游默认 15）。
DEFAULT_SEND_LIMIT_PER_MINUTE = 15
#: 夹逼下界。
SEND_LIMIT_MIN_PER_MINUTE = 1
#: 夹逼上界。
SEND_LIMIT_MAX_PER_MINUTE = 30
#: 一次 429 调用的总尝试次数（1 次原始 + 3 次重试）。
MAX_SEND_ATTEMPTS = 4
#: 429 重试的**累计**等待上限（秒）：再等就要超预算 ⇒ 直接报限流失败。
MAX_RATE_LIMIT_WAIT_SECONDS = 60.0


def clamp_send_limit(value: Any) -> int:
    """把配置里的每聊天每分钟上限夹到 1..30；非数值回落默认 15。

    ⛔ 不抛异常：这条闸在适配器构造路径上，抛出去会打死整个适配器
    （``base.build`` 包成 ``AdapterError`` ⇒ 用户只看到"没有可用适配器"）。
    布尔值**不算数值**（``True`` 是 ``int`` 的子类，但把它当 1 条/分钟是
    静默误读一个配置事故）⇒ 一律回落默认。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return DEFAULT_SEND_LIMIT_PER_MINUTE
    return max(SEND_LIMIT_MIN_PER_MINUTE, min(SEND_LIMIT_MAX_PER_MINUTE, value))


def prune_send_timestamps(
    timestamps: Sequence[float],
    *,
    now: float,
    window_seconds: float = SEND_WINDOW_SECONDS,
) -> list[float]:
    """裁掉窗口外的记录（保留 ``at > now - window`` 的那些）。

    ⚠️ **边界是开区间**：正好落在 ``now - window`` 上的一条**算已过期**
    （它在窗口开始那一刻发出，窗口已经整体滑过去了）。
    返回**新列表**，不改入参。
    """
    oldest_kept = now - window_seconds
    return [at for at in timestamps if at > oldest_kept]


def seconds_until_send_slot(
    timestamps: Sequence[float],
    *,
    now: float,
    limit: int,
    window_seconds: float = SEND_WINDOW_SECONDS,
) -> float:
    """距离可以再发**一条新建消息**还要等几秒（0.0 = 现在就能发）。

    先裁剪、再按**裁剪后**的长度判预算 —— 顺序不能反：不裁剪就等于窗口
    永远不滑，长会话会永久卡死。
    ``limit <= 0`` 是配置事故的防御：一律放行（0.0），⛔ 不抛。
    返回值恒 ``>= 0.0``（⛔ 不出现 ``-0.0`` 或极小负数）。
    """
    if limit <= 0:
        return 0.0
    recent = prune_send_timestamps(
        timestamps, now=now, window_seconds=window_seconds
    )
    if len(recent) < limit:
        return 0.0
    wait = window_seconds - (now - min(recent))
    return wait if wait > 0.0 else 0.0


def record_send(
    timestamps: Sequence[float],
    *,
    now: float,
    window_seconds: float = SEND_WINDOW_SECONDS,
) -> list[float]:
    """返回"裁掉窗口外的 + 追加 ``now``"之后的新列表。

    追加**排在最末**：调用方按时间先后追加，裁剪只砍前面那段，所以列表
    天然按时间升序 —— 不必在这里排序（排序会掩盖调用方的乱序 bug）。
    """
    recent = prune_send_timestamps(
        timestamps, now=now, window_seconds=window_seconds
    )
    recent.append(now)
    return recent


def rate_limit_wait_seconds(
    retry_after: Optional[float],
    *,
    already_waited: float,
    attempts_done: int,
) -> Optional[float]:
    """429 之后还要等多久；``None`` = 不要再等，直接报限流失败。

    * ``retry_after`` 非有限值 / 负数 ⇒ 按 ``0.0`` 处理（仍重试，不傻等）；
    * 已用完尝试次数 ⇒ ``None``；
    * 再等就要超出**累计**等待上限 ⇒ ``None``（不把这次关停/回复拖到 60s 之上）。
    """
    if attempts_done >= MAX_SEND_ATTEMPTS:
        return None
    delay = 0.0
    if isinstance(retry_after, (int, float)) and not isinstance(retry_after, bool):
        if math.isfinite(retry_after) and retry_after > 0.0:
            delay = float(retry_after)
    if already_waited + delay > MAX_RATE_LIMIT_WAIT_SECONDS:
        return None
    return delay
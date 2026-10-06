"""共享的配置类型强制助手（``coerce_*``）—— 全仓库一份纪律，适配器逐个迁移。

背景（为什么不是"漏了两次 try"）
--------------------------------
JSON 里的配置值可以是字符串 / 数字 / 布尔 / ``null``，而适配器要的是
``int`` / ``float`` / ``bool`` / ``str``。于是裸的 ``int(self.config.get(k))`` 在用户
填了 ``"30s"`` 时抛 ``ValueError``，而 :func:`opencode_bridge.adapters.base.build`
会把它包成 :class:`~opencode_bridge.adapters.base.AdapterError`，``__main__`` 那个
循环 ``continue`` 掉这个适配器 ⇒ 若它是唯一配置的平台，``usable == 0`` ⇒ **整个桥
``exit 1``**，而用户看到的报错是「没有任何可用适配器」—— **一个字都不提真因**（真因
只在日志的一行 ``failed to build adapter '…'; skipped`` 里）。也就是说，一个旋钮写错
能打死这条零容错关键路径。

⇒ 根因不是"两个键忘了写 ``try``"，而是**没有共享的类型强制入口**：同一份纪律在本
仓库被手抄了 6 遍（``nextcloud._config_int_value`` / ``_config_float``、
``discord._config_intents``、``irc._resolve_port``、``a2a._coerce_positive`` /
``_coerce_port``、``email._port`` / ``_timeout``、``telegram._coerce_poll_timeout``），
12 个适配器各写各的，**两份干脆没写** ⇒ 崩溃。

纪律（三条，缺一不可）
--------------------
1. **没配 ⇒ 静默用默认值。** 否则每次启动都刷一条没意义的警告，真正的配置错误
   反而被淹没（照 :func:`opencode_bridge.adapters.a2a._coerce_positive` 的原话
   「"没配"与"配错了"必须分开：**没配时静默**」）。

   ⚠️ **「没配」的判据包含「纯空白串」，而这是一条行为变化**（迁移实测，2026-10-06）：
   判据是「键不存在 / 值为 ``None`` / **值是纯空白**」，而各家改前多是
   ``raw in (None, "")`` ⇒ **一个只填了空格的键改前会告警、改后静默**。
   方向是「告警变静默」，与纪律 1 的意图一致（空白与没配对用户是同一件事），
   但**它确实是行为变了** ⇒ 迁移该键时**必须**在改前/改后对照表里逐条写出，
   ⛔ 不要因为「改后更安静」就当成无损改动。
   ⇒ 记在这里而不是让每个适配器各写一遍 —— 各写各的正是这个模块存在的理由。
2. **配了但非法（解析失败 / 越界）⇒ 回落到默认值 + 打一条 WARNING**，文案里说清
   **是哪个键、收到了什么（``%r``）、回落到什么**。只说"'nope' 不是数字"而不说键名，
   用户得自己去猜是哪个旋钮坏了（照 ``_coerce_positive`` 的 ``:param key:``）。
3. **绝不静默采纳非法值** —— 照
   :meth:`~opencode_bridge.adapters.nextcloud.NextcloudAdapter._config_int_value`
   的注释原话：「越界一律回落到默认，**不静默采纳**」。所以**区间要作为参数交给本
   函数**，⛔ 不要在调用点外面再套一层 ``max(1, ...)``：那会把越界的值静默改小，用户
   以为自己配的是另一个数，而没有任何东西会告诉他。

为什么参数是「整份 config + 键名」而不是「取出来的值」
------------------------------------------------------
因为纪律 1 与纪律 2 的分界（键**没配** vs 键**配了但非法**）必须由**同一个判据**划，
否则每个适配器都要各自重写一遍这个判据 —— 而各写各的正是这个缺陷的来源。调用点只需
一句 ``coerce_int(self.config, "poll_timeout", POLL_LONG_TIMEOUT, minimum=1,
platform=self.name)``。

类型这一档（``bool`` / ``float`` 为什么**不能**穿过任何数值强制）
--------------------------------------------------------------
``bool`` 是 Python 的 ``int`` **子类** ⇒ 它静默穿过**每一个**整数判据：
``int(True) == 1``、``float(True) == 1.0``。而 ``int(9900.7) == 9900`` —— 唯一的
转换手段是**截断**，也就是静默改掉用户配的值。两条的共同点是：**用户以为自己配的
生效了，其实拿到的是另一个数，而日志里一个字都没有**（纪律 3 反过来的一面）。
⇒ 所以**在共享层就拒**：``True`` / ``False`` 两档数值键都拒；``float`` **只有整数档
拒**（浮点档 ``float(30.5)`` 是精确的、不丢信息，拒它没有理由）。

⚠️ **代价要写明**：JSON **没有** int/float 之分 ⇒ ``9900.0`` 与 ``1e3`` 都会落到
Python 的 ``float``，这一档会被拒并告警。这是**有意**的取舍：告警里点名了键与值，
用户改成 ``9900`` 即可；而静默截断连"要改"这件事都不会告诉他。

迁移是逐个适配器、逐个验证的，不要一次性全换
------------------------------------------------
每迁移一个都要单独跑它自己的测试 —— 6 份近似实现的**语义并不完全一致**（例如
``irc._resolve_port`` 回落的是 TLS 相关的端口、``discord._config_intents`` 对非正数
**不告警**而直接回默认），一次性统一会悄悄改掉这些差异。已接上的：
``matrix.sync_timeout_ms``、``email.dedupe_capacity`` / ``imap_port`` /
``smtp_port`` / ``socket_timeout`` / ``poll_interval``。
"""

from __future__ import annotations

import logging
import math
from typing import Any, Callable, Mapping, Optional

logger = logging.getLogger("opencode_bridge.config_coerce")

__all__ = ["coerce_bool", "coerce_float", "coerce_int", "coerce_text"]

#: 布尔键在 JSON 里可能被写成 ``1`` / ``0`` / ``"yes"`` / ``"on"``（email 的
#: ``_config_verify_tls`` 早就这么认），所以这些词与数字都收。
_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
_FALSE_WORDS = frozenset({"0", "false", "no", "off"})


def _prefix(platform: str) -> str:
    """日志前缀（各适配器都是 ``"<platform>: …"`` 的写法，缺省平台名时不加前缀）。"""
    return f"{platform}: " if platform else ""


def _is_unset(raw: Any) -> bool:
    """**没配**的判据：键不存在 / ``None`` / 只含空白的字符串。

    ⚠️ 刻意**不**把 ``0`` / ``False`` 当成"没配"：它们是**配了**，而且常常是合法取值
    （Matrix 的 ``sync_timeout_ms=0`` 就是"不做长轮询，立即返回"）。照
    :meth:`~opencode_bridge.adapters.nextcloud.NextcloudAdapter._config_int_value`
    的 ``raw in (None, "")``：只有缺失与空串算没配。
    """
    return raw is None or (isinstance(raw, str) and not raw.strip())


def _bounds_note(
    minimum: Optional[float],
    maximum: Optional[float],
    exclusive_minimum: Optional[float],
) -> str:
    """把区间写成人读的形状（告警里要用；无区间时返回空串）。"""
    if minimum is not None and maximum is not None:
        return f"[{minimum}, {maximum}]"
    if minimum is not None:
        return f">= {minimum}"
    if maximum is not None:
        return f"<= {maximum}"
    if exclusive_minimum is not None:
        return f"> {exclusive_minimum}"
    return ""


def _within_bounds(
    value: float,
    minimum: Optional[float],
    maximum: Optional[float],
    exclusive_minimum: Optional[float],
) -> bool:
    """区间校验（上界闭、下界看是闭是开；缺掉的那端不参与比较）。"""
    if minimum is not None and value < minimum:
        return False
    if exclusive_minimum is not None and value <= exclusive_minimum:
        return False
    if maximum is not None and value > maximum:
        return False
    return True


def _reject(platform: str, key: str, raw: Any, default: Any, reason: str) -> None:
    """打那条**唯一**的告警：哪个键、收到了什么（``%r``）、为什么、回落到什么。

    三样都必须出现在文案里（纪律 2）：缺了键名用户得自己猜，缺了 ``%r`` 用户不知道
    自己那个值被判成了什么，缺了回落目标就无法判断自己的配置有没有生效。
    """
    logger.warning(
        "%s配置项 %s=%r %s，已回落为 %r",
        _prefix(platform), key, raw, reason, default,
    )


def _wrong_numeric_type_reason(raw: Any, kind: str, *, refuse_float: bool) -> Optional[str]:
    """这个值的**类型**压根不是目标类型 ⇒ 给出告警理由；类型没问题则返回 ``None``。

    为什么单独一层（而不是并进 ``int()`` 的 ``except``）：``int(True)`` **不会抛**，
    所以这类值永远不会走到 ``except`` 分支 —— 而它们恰恰是**最该被告警**的一类
    （用户以为配了 ``true``，实际拿到 1，且日志里一个字都没有）。

    :param refuse_float: 只有**整数档**为真。浮点档 ``float(30.5)`` 是精确的、不丢
        信息，拒它没有理由；整数档 ``int(9900.7)`` 只能**截断**，也就是静默改值。

    ⛔ 字符串**不在**这一层里判：``int(" 42 ")`` 成功是**正确**的行为（JSON 里写
    数字字符串是常事），由 ``cast`` 那一步照常处理。
    """
    if isinstance(raw, bool):
        return (
            f"不是{kind}（bool：Python 里 bool 是 int 的子类，"
            f"数值强制会把它静默读成 1/0）"
        )
    if refuse_float and isinstance(raw, float):
        return (
            f"不是{kind}（float：截断会丢掉小数部分，"
            f"等于悄悄改了用户配的值）"
        )
    return None


def _coerce_number(
    config: Mapping[str, Any],
    key: str,
    default: Any,
    *,
    cast: Callable[[Any], Any],
    kind: str,
    minimum: Optional[float],
    maximum: Optional[float],
    exclusive_minimum: Optional[float],
    refuse_float: bool,
    platform: str,
) -> Any:
    """整数 / 浮点共用的那条纪律（两个公开包装的共同实现）。

    :param kind: 类型名，只出现在告警里（"不是整数" / "不是浮点数"）。
    """
    raw = config.get(key)
    if _is_unset(raw):                      # 纪律 1：没配 ⇒ 静默
        return default
    type_reason = _wrong_numeric_type_reason(raw, kind, refuse_float=refuse_float)
    if type_reason is not None:             # 类型就不是目标类型 ⇒ 同纪律 2
        _reject(platform, key, raw, default, type_reason)
        return default
    try:
        value = cast(raw)
    except (TypeError, ValueError):        # 纪律 2：解析失败 ⇒ 告警 + 回落
        _reject(platform, key, raw, default, f"不是{kind}（{type(raw).__name__}）")
        return default
    if not _within_bounds(value, minimum, maximum, exclusive_minimum):  # 纪律 3
        _reject(
            platform, key, raw, default,
            f"越界（要求 {_bounds_note(minimum, maximum, exclusive_minimum)}）",
        )
        return default
    return value


def coerce_int(
    config: Mapping[str, Any],
    key: str,
    default: int,
    *,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
    exclusive_minimum: Optional[int] = None,
    platform: str = "",
) -> int:
    """``config[key]`` → ``int``；没配 ⇒ ``default``（静默）；非法/越界 ⇒ ``default`` + WARNING。

    :param minimum: 下界（闭）。**下界要作为区间交给本函数**，⛔ 不要在调用点外面套
        ``max(1, ...)`` —— 那会静默改小越界的值（纪律 3）。
    :param maximum: 上界（闭）。``None`` 表示不设上界（协议本身不限时就不该硬加一个）。
    :param exclusive_minimum: 下界（**开**）。需要"必须大于某值"时用它，例如
        ``socket_timeout``：``0`` 不是"很短的超时"，而是"socket 不超时"。
    :param platform: 平台名，只用于告警前缀（传 ``self.name``）。

    ⚠️ **``bool`` 与 ``float`` 一律拒**（理由见模块 docstring 的「类型这一档」）：
    ``int(True) == 1`` 与 ``int(9900.7) == 9900`` 都**不抛异常**，所以它们静默穿过
    了一切判据 —— 而那正是"用户以为配的生效了、其实拿到另一个数"。
    ⇒ 与 :func:`opencode_bridge.adapters.a2a._coerce_port`（``int(str(v).strip())``）
    在**每一个**取值上都同答。
    ⚠️ **代价**：JSON 没有 int/float 之分 ⇒ ``9900.0`` / ``1e3`` 也会被拒（+ 告警）。
    数值字符串（``"9900"`` / ``" 9900 "``）照常认 —— 那是**正确**的行为。
    """
    return _coerce_number(
        config, key, default,
        cast=int, kind="整数",
        minimum=minimum, maximum=maximum, exclusive_minimum=exclusive_minimum,
        refuse_float=True, platform=platform,
    )


def coerce_float(
    config: Mapping[str, Any],
    key: str,
    default: float,
    *,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
    exclusive_minimum: Optional[float] = None,
    platform: str = "",
) -> float:
    """``config[key]`` → ``float``，纪律与 :func:`coerce_int` 完全一致，**三处例外**：

    1. **不拒 ``float``**：``float(30.5)`` 精确，不丢信息（整数档拒它是因为它只能截断）。
    2. **额外拒 ``NaN`` / ``inf``**：它们**能**解析成功，但任何比较都是假的
       （``nan <= 0`` 为 ``False``），照
       :func:`opencode_bridge.adapters.a2a._coerce_positive` 的判据 ``number != number``
       一并回落 —— 静默采纳 NaN 的后果是"超时判断永远成立"，采纳 ``inf`` 的后果是
       "永不超时"（``stop()`` 因此白等满 join 超时）。
    3. **同样拒 ``bool``**：``float(True) == 1.0`` 与整数档是同一个谎。

    ⚠️ 例外 1 与例外 2 **方向相反却不矛盾**：判据是"这个转换会不会丢掉用户写的信息"
    —— ``float`` ← ``float`` 不丢（收），``float`` ← ``int`` 不丢（收），
    ``int`` ← ``float`` 丢（拒），而 ``NaN`` / ``inf`` 是**解析成功但不是可用数值**
    （与"类型压根不对"同类，拒）。
    """
    raw = config.get(key)
    if _is_unset(raw):
        return default
    type_reason = _wrong_numeric_type_reason(raw, "浮点数", refuse_float=False)
    if type_reason is not None:
        _reject(platform, key, raw, default, type_reason)
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        _reject(platform, key, raw, default, f"不是浮点数（{type(raw).__name__}）")
        return default
    if not math.isfinite(value):
        _reject(platform, key, raw, default, "不是有限数（NaN / inf）")
        return default
    if not _within_bounds(value, minimum, maximum, exclusive_minimum):
        _reject(
            platform, key, raw, default,
            f"越界（要求 {_bounds_note(minimum, maximum, exclusive_minimum)}）",
        )
        return default
    return value


def coerce_bool(
    config: Mapping[str, Any], key: str, default: bool, *, platform: str = ""
) -> bool:
    """``config[key]`` → ``bool``：认 JSON 的 ``true``/``false``、``1``/``0``、
    以及 ``"1"``/``"true"``/``"yes"``/``"on"`` 与 ``"0"``/``"false"``/``"no"``/``"off"``
    （大小写与空白不敏感）。

    ⚠️ **没有"第三态"**：认不出来的值**不**回落成"宽松的那一侧"，而是回落到
    ``default`` + 告警。否则一个写错的 ``verify_tls`` 会静默变成"不校验证书"。
    （认词表照 :meth:`~opencode_bridge.adapters.email.EmailAdapter._config_verify_tls`。）
    """
    raw = config.get(key)
    if _is_unset(raw):
        return default
    if isinstance(raw, bool):
        return raw
    word = str(raw).strip().lower()
    if word in _TRUE_WORDS:
        return True
    if word in _FALSE_WORDS:
        return False
    _reject(platform, key, raw, default, f"不是布尔值（{type(raw).__name__}）")
    return default


def coerce_text(
    config: Mapping[str, Any], key: str, default: str, *, platform: str = ""
) -> str:
    """``config[key]`` → ``str``，**去首尾空白**；没配 ⇒ ``default``（静默）。

    ⚠️ **刻意不提供 ``allow_empty`` 开关。** "空串是否合法"在各处**理由不同** ——
    email 的 ``echo_prefix`` 不许为空是因为空前缀等于关掉防回环，那是一条安全理由、
    不该被压成一个通用布尔量。让每个调用点在自己的注释里写清那条理由。
    """
    raw = config.get(key)
    if _is_unset(raw):
        return default
    return str(raw).strip()

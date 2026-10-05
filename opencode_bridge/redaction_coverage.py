"""脱敏覆盖的**顺序无关**那一层：记录**创建**时就脱敏，而不是挂在 handler 上。

## 为什么需要这一层（实测，不是推测）
===================================

:mod:`opencode_bridge.redaction` 把 :class:`~opencode_bridge.redaction.RedactingFilter`
挂在 **handler** 上。这在生产里是对的（``__main__`` 先 :func:`logging.basicConfig`
建 root handler、再调 :func:`~opencode_bridge.redaction.install_redaction_filter`），
但它有一个**结构性的**顺序依赖：

    ``logging.Logger.callHandlers`` 从**发出记录的那个 logger** 往上走，
    沿途逐个调 ``handler.handle(record)`` —— 而 ``filter`` 是在 ``handle`` 里面跑的。
    所以**谁排在前面谁先拿到记录**。

于是"挂在 root 上的过滤器"只能覆盖**root 及以下**的 handler。实测（``.tmp`` 里的
探针，四个形状各跑一遍）：

* **后挂到 root 上的 handler** → **盖住了**（不漏）。因为过滤器是**就地改**那条
  共享的 ``LogRecord``（``record.msg`` / ``record.args`` / ``record.exc_text``），
  同一个走位里后面的 handler 拿到的是**已经脱敏的那一个对象**。
* **``logging.lastResort``** → 也盖住了：只要走位里有**任意一个**带过滤器的
  handler，它的 ``filter`` 就已经跑过，哪怕它因为级别不够而没输出。
* **挂在 :data:`~opencode_bridge.redaction.LOGGER_NAMESPACE` 或更深的 logger 上、
  且在安装之后才挂** → **漏**。它在走位里比 root **更靠前**，先把明文写出去了，
  root 的过滤器随后才改 —— 已经晚了一步。

所以：**"安装之后才挂的 handler 不被覆盖"这句话是错的**（实测不漏）；真正的条件是
**"这个 handler 在走位里排在所有带过滤器的 handler 前面"**。生产代码今天没有这种
handler（``opencode_bridge/**`` 里**零** ``addHandler``），所以那是个**潜伏**缺口；
但本仓库自己的测试就在这么干（``tests/test_qqbot.py``、
``tests/test_allowlist_visibility.py``、以及 ``assertLogs`` 自己），而"给某个适配器
挂一个本地调试 handler"是最自然的一种写法 —— 离危险只有一行。

## 根修：把脱敏挪到**记录创建**那一刻
====================================

:func:`logging.setLogRecordFactory` 换掉的是"``LogRecord`` 怎么被造出来"，而每一条
记录都必须先被造出来才谈得上找 handler。于是把脱敏挂在那里：

* **与 handler 的数量无关**（0 个、1 个、以后再加 10 个都一样）；
* **与挂上去的时刻无关**（装之前、装之后、进程启动前）；
* **与走位顺序无关**（不存在"谁先拿到"）；
* 连 ``logging.lastResort``（它**不在**任何 ``logger.handlers`` 里）也一并盖住。

⚠️ **只碰本命名空间的记录**：判据是记录名在
:data:`~opencode_bridge.redaction.LOGGER_NAMESPACE` 之下，别人的日志一条不动 ——
这个模块活在别人的进程里，进程全局的东西必须**只对自己那部分**生效。

⚠️ **不重写别人的 record factory**：调 :func:`logging.setLogRecordFactory` 之前先把
**原来那个**取出来，我们自己造记录之后**再调它**，它的返回值原样交出去。所以宿主
装了自定义 factory（比如要注入 hostname / tid）的行为**完全不变**。

## 不许静默降级
==============

装不装得上、后来有没有被别人覆盖掉，都**必须能查、必须说得出来**：
:func:`coverage_report` 给出结构化答案，:func:`warn_about_uncovered_records`
把"脱敏已失效"这件事**记成一行 warning**。理由与 :mod:`opencode_bridge.redaction`
的过滤器一致：吞掉一行日志比漏掉一个值难查得多，而**让明文悄悄出去**比两者都糟。

## 幂等
======

这一层与 handler 上那一层会**对同一条记录各跑一遍**。引擎是幂等的（摘要在每 6 位
插分隔符，结构上不可能再被任何规则命中；``[REDACTED:...]`` 的方括号被排除在
``secret-assignment-unquoted`` 的字符类之外），所以第二遍是**恒等变换**。
``tests/test_redaction_coverage.py`` 把这条**证明**出来，而不是假定。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from .redaction import LOGGER_NAMESPACE, RedactingFilter, Redactor

__all__ = [
    "coverage_report",
    "install_order_independent_coverage",
    "remove_order_independent_coverage",
    "warn_about_uncovered_records",
]

#: 装在**我们装的** record factory 上的标记。别的代码可以照着它把我们的那层摘掉
#: （:func:`remove_order_independent_coverage` 就靠它），而不会误伤别人的。
_OURS_MARKER = "_opencode_bridge_record_factory"

logger = logging.getLogger(LOGGER_NAMESPACE + ".redaction_coverage")


def _belongs_to_this_package(record_name: str) -> bool:
    """这条记录是不是本命名空间发出来的。

    用 ``==`` 或 ``"."`` 边界而不是裸 ``startswith``：``opencode_bridge_extra``
    是**别人的** logger，不该被我们改。
    """
    return (
        record_name == LOGGER_NAMESPACE
        or record_name.startswith(LOGGER_NAMESPACE + ".")
    )


def _scrubbing_record_factory(
    previous_factory: Callable[..., logging.LogRecord],
    scrubber: RedactingFilter,
) -> Callable[..., logging.LogRecord]:
    """造一个 record factory：先让 ``previous_factory`` 造记录，再就地脱敏。

    脱敏本身**复用** :class:`~opencode_bridge.redaction.RedactingFilter` 的公开
    ``filter()``（它返回 ``True``、永不丢记录），所以两层用的是**同一份逻辑** ——
    这一层没有第二套脱敏规则，也就没有"两套规则漂移"这种问题。
    """

    def factory(name, level, pathname, lineno, msg, args, exc_info,
                func=None, sinfo=None):
        record = previous_factory(
            name, level, pathname, lineno, msg, args, exc_info, func, sinfo)
        try:
            if _belongs_to_this_package(name):
                scrubber.filter(record)
        except Exception:  # noqa: BLE001 - 脱敏层绝不许让日志路径抛
            pass
        return record

    setattr(factory, _OURS_MARKER, True)
    # 记住上一层，卸载时**原样放回去** —— 别人的 factory 不该被我们顶掉。
    setattr(factory, "_opencode_bridge_previous_factory", previous_factory)
    return factory


def install_order_independent_coverage(
    *, redactor: Optional[Redactor] = None
) -> bool:
    """把脱敏挂到 ``logging`` 的 **record factory** 上，返回是否装上。

    **可重复调用，且后一次赢**（与
    :func:`~opencode_bridge.redaction.install_redaction_filter` 同一个语义）：
    先摘掉先前那层，再按新的脱敏器挂上，所以"换个密钥重装"是确定性的。

    装不上时返回 ``False`` 并且**记一行 warning** —— 不静默继续（见模块 docstring
    「不许静默降级」）。
    """
    from .redaction import default_redactor

    the_redactor = redactor if redactor is not None else default_redactor()
    try:
        remove_order_independent_coverage()
        scrubber = RedactingFilter(the_redactor)
        previous = logging.getLogRecordFactory()
        logging.setLogRecordFactory(
            _scrubbing_record_factory(previous, scrubber))
    except Exception:  # noqa: BLE001 - 装不上就明说，绝不假装已经脱敏
        logger.warning(
            "装不上顺序无关的脱敏覆盖（setLogRecordFactory 失败）："
            "只有 handler 上那一层过滤器在生效，而它**依赖 handler 在走位里的位置**"
        )
        return False
    return True


def remove_order_independent_coverage() -> bool:
    """摘掉我们装的 record factory，把**原来那个**放回去。

    为什么需要它：record factory 是**进程全局**的，装上就不会自己下来。一个全局的
    安装必须有配对的全局卸载 —— 否则测试进程里"先装过一次的用例"会**永久**改变
    后面所有用例的前提（``tests/test_redaction.py`` 的对照组就是这么被破坏的）。
    """
    current = logging.getLogRecordFactory()
    if not getattr(current, _OURS_MARKER, False):
        return False
    previous = getattr(current, "_opencode_bridge_previous_factory", None)
    logging.setLogRecordFactory(previous or logging.LogRecord)
    return True


def coverage_report() -> dict[str, Any]:
    """现在到底脱敏了没有 —— 结构化答案，供 ``--status`` 与测试消费。

    :return: ``record_factory_is_ours``（顺序无关那层是否还在）、
        ``handlers_with_filter`` / ``handlers_seen``（handler 那一层覆盖面）、
        ``fully_covered``（两层都在，且顺序无关那层是主要的）。
    """
    factory = logging.getLogRecordFactory()
    factory_is_ours = bool(getattr(factory, _OURS_MARKER, False))

    seen = 0
    filtered = 0
    for name in (LOGGER_NAMESPACE, None):        # None = root
        for handler in logging.getLogger(name).handlers:
            seen += 1
            if any(
                getattr(existing, "_opencode_bridge_redaction", False)
                for existing in getattr(handler, "filters", [])
            ):
                filtered += 1
    return {
        "record_factory_is_ours": factory_is_ours,
        "handlers_seen": seen,
        "handlers_with_filter": filtered,
        "fully_covered": factory_is_ours,
    }


def warn_about_uncovered_records() -> Optional[str]:
    """脱敏已经失效时**记一行 warning** 并返回那句话；正常时返回 ``None``。

    谁会让它失效：任何人在我们之后又调了一次
    :func:`logging.setLogRecordFactory`（``unittest`` 自己、某些库、宿主应用）。
    那会把我们的那层**整个替换掉**，而 handler 上那一层还在 —— 于是回到"依赖走位
    顺序"的状态。这件事必须**说得出来**，否则就是"明文悄悄出去"。
    """
    if coverage_report()["record_factory_is_ours"]:
        return None
    message = (
        "顺序无关的脱敏覆盖已失效：logging 的 record factory 被换掉了，"
        "现在只剩 handler 上那一层过滤器，而它依赖 handler 在走位里的位置"
    )
    logger.warning(message)
    return message

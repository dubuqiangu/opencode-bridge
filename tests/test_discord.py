"""Discord 的**配置类型强制**测试 —— ``intents`` 一键，共享助手
:mod:`opencode_bridge.config_coerce` 迁移的**改前 / 改后**对照。

⚠️ **为什么这个文件是新建的、而不是并进 ``test_discord_gateway.py``**：本 lane 的可改
文件是 ``adapters/discord.py`` 与 ``tests/test_discord.py``，而 ``test_discord.py``
当时**并不存在**（discord 的用例全在 ``test_discord_gateway.py`` 里，那条文件不在本
lane 的可改范围）⇒ 配置强制的用例放这里，网关协议测试一条不动。

⚠️ **基线怎么取的**：改前的 ``_config_intents`` 是从**迁移前的工作树**逐行抄进本文件
的 :func:`intents_before_migration`（⛔ 没有用 ``git stash`` 取基线 —— 那会把并行 lane
的在制品一起收走）。合法值逐字节等价的断言就是拿现在的适配器与那份抄件对拍。

**改前 / 改后对照表**（:data:`INTENTS_BEFORE_AFTER`）是这份测试的骨架：非法值从
「静默」变「告警」是**修正**（纪律 2/3），不是回归，所以每一行都显式写出改前的取值与
改前的告警有无，改后再逐列断言。

⇒ 三处「静默 → 告警」都在这张表里钉着，且每条都**能靠改回原写法变红**：
``intents=0`` / ``-1``（改前那一行 ``value if value > 0`` 静默回落）、
``intents=true``（改前 ``int(True) == 1`` 静默采纳，而 1 不是任何一个 intent 位）、
``intents=4096.9``（改前 ``int()`` 截断后静默采纳 4096）。
"""

from __future__ import annotations

import logging
import unittest
from typing import Any, List, Tuple

# 让"预期内的告警"别污染测试输出（下面自己挂 handler 捕获，不受影响）
logging.getLogger("opencode_bridge").addHandler(logging.NullHandler())

from opencode_bridge.adapters.discord import (  # noqa: E402
    DEFAULT_INTENTS,
    INTENT_DIRECT_MESSAGES,
    INTENT_GUILD_MESSAGES,
    INTENT_MESSAGE_CONTENT,
    DiscordAdapter,
)

#: 共享助手那条路径的 logger。⚠️ **迁移后告警的发出方换成了它**
#: （文案前缀 ``discord: `` 由 ``platform=self.name`` 保住，所以日志**内容**不变）——
#: 与 email / ntfy / matrix 的迁移同一条约定，见 ``tests/test_email.py`` 顶部那段说明。
COERCE_LOGGER = "opencode_bridge.config_coerce"
#: 告警**内容**前缀（logger 换了，``platform=self.name`` 把它保住了）。
DISCORD_LOG_PREFIX = "discord: "

#: 不是凭据形状的占位串（discord 入站只多要 ``bot_token`` 一个键）。
BOT_TOKEN = "bot-token-not-a-credential"


class RecordingHooks:
    """最小 hooks 替身：配置读取阶段不该有任何入站投递，被调用了就是意外。"""

    def on_inbound(self, inbound: Any) -> None:
        raise AssertionError("配置读取阶段不该有入站投递")

    def on_callback(self, conversation_id: str, data: str, query_id: str) -> None:
        raise AssertionError("配置读取阶段不该有按钮回调")


def make_adapter(**config: Any) -> DiscordAdapter:
    """带 ``bot_token`` 的适配器（只跑配置读取，零网络、零线程）。"""
    base: dict = {"bot_token": BOT_TOKEN}
    base.update(config)
    return DiscordAdapter(base, RecordingHooks())


def intents_before_migration(config: dict, warned: List[Any]) -> int:
    """**改前**的 ``_config_intents``，逐行抄自迁移前的 ``adapters/discord.py``。

    :param warned: 收集改前那条 WARNING 的**触发点**（改前只有解析失败才告警；
        非正数那一支是 ``return value if value > 0 else DEFAULT_INTENTS``，**静默**）。
    """
    raw = config.get("intents")
    if raw in (None, ""):
        return DEFAULT_INTENTS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        warned.append(raw)
        return DEFAULT_INTENTS
    return value if value > 0 else DEFAULT_INTENTS


#: ``intents`` 的**改前 / 改后**对照表：
#: ``(配置值, 改前结果, 改前是否告警, 改后结果, 改后是否告警)``。
#:
#: ⚠️ "改前"两列不是凭记忆写的：由 :func:`intents_before_migration` 现场跑出来断言，
#: 所以这张表和抄件不会一起漂。
INTENTS_BEFORE_AFTER: Tuple[Tuple[Any, int, bool, int, bool], ...] = (
    # --- 解析失败：改前就告警 ⇒ 行为不变 --------------------------------
    ("oops", DEFAULT_INTENTS, True, DEFAULT_INTENTS, True),
    ("512|4096", DEFAULT_INTENTS, True, DEFAULT_INTENTS, True),
    ([512, 4096], DEFAULT_INTENTS, True, DEFAULT_INTENTS, True),
    ({"intents": 512}, DEFAULT_INTENTS, True, DEFAULT_INTENTS, True),
    # --- 非正数：改前**静默**回落，改后告警回落（纪律 2 的修正）-----------
    (0, DEFAULT_INTENTS, False, DEFAULT_INTENTS, True),
    (-1, DEFAULT_INTENTS, False, DEFAULT_INTENTS, True),
    # --- bool / float：改前**静默采纳**，改后告警回落（纪律 3 的修正）-----
    # ⚠️ ``True`` 改前拿到 **1**：不是任何 intent 位（:data:`INTENT_GUILD_MESSAGES`
    # 是 512）⇒ 一个事件都收不到，而日志里一个字都没有。
    (True, 1, False, DEFAULT_INTENTS, True),
    (False, DEFAULT_INTENTS, False, DEFAULT_INTENTS, True),
    (4096.0, 4096, False, DEFAULT_INTENTS, True),
    (4096.9, 4096, False, DEFAULT_INTENTS, True),
)


def read_intents_and_coerce_logs(**config: Any) -> Tuple[int, List[str]]:
    """读一次真拿到的 ``adapter.intents``，并带回共享助手那条路径上的告警文本。

    ⛔ 读的是**属性**（真解析结果），不是类属性、也不是配置回显。
    ⚠️ handler 必须**先挂后构造**：配置解析发生在 ``__init__`` 里，构造完再挂就
    一条都收不到（这正是第一版的错法 —— 18 条用例全红在"没告警"上）。
    """
    logger = logging.getLogger(COERCE_LOGGER)
    records: List[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        value = make_adapter(**config).intents
    finally:
        logger.setLevel(previous_level)
        logger.removeHandler(handler)
    return value, [record.getMessage() for record in records]


class TestIntentsConfigCoercion(unittest.TestCase):
    """``_config_intents`` → :func:`~opencode_bridge.config_coerce.coerce_int`。"""

    def test_legal_values_are_byte_identical_to_the_pre_migration_expression(self):
        """合法值必须与改前那份抄件**逐字节**相同，且一条告警都不打（纪律 1/2 的分界）。"""
        for raw in (
            DEFAULT_INTENTS,
            INTENT_GUILD_MESSAGES,
            INTENT_DIRECT_MESSAGES,
            INTENT_MESSAGE_CONTENT,
            512 | 4096,
            1,                       # 改前判据是 ``> 0`` ⇒ 1 合法（下界是闭的 1）
            2 ** 31,
            str(DEFAULT_INTENTS),    # JSON 里写数字字符串是常事，必须照常认
            " 4096 ",                # 带空白的数字串
        ):
            with self.subTest(raw=raw):
                expected = intents_before_migration({"intents": raw}, [])
                actual, warnings = read_intents_and_coerce_logs(intents=raw)
                self.assertEqual(actual, expected)
                self.assertIs(type(actual), int, "intents 必须是整数 bitmask，不是别的")
                self.assertEqual(warnings, [], "合法值不许告警")

    def test_the_before_and_after_table(self):
        """非法值的取值与告警有无，逐行对照改前 / 改后。"""
        for raw, before_value, before_warned, after_value, after_warned in INTENTS_BEFORE_AFTER:
            with self.subTest(raw=raw):
                warned_before: List[Any] = []
                self.assertEqual(
                    intents_before_migration({"intents": raw}, warned_before),
                    before_value,
                    "这张表的「改前结果」列与抄件对不上 —— 抄件或表漂了",
                )
                self.assertEqual(bool(warned_before), before_warned)

                actual, warnings = read_intents_and_coerce_logs(intents=raw)
                self.assertEqual(actual, after_value)
                self.assertEqual(
                    bool(warnings), after_warned,
                    "改前 %r / 改后 %r 的告警有无与表不符" % (before_warned, after_warned),
                )

    def test_an_unset_value_is_silent(self):
        """纪律 1：没配 ⇒ 静默用默认，否则每次启动都刷屏，真正的错反而被淹没。"""
        for config in ({}, {"intents": None}, {"intents": ""}):
            with self.subTest(config=sorted(config, key=repr)):
                actual, warnings = read_intents_and_coerce_logs(**config)
                self.assertEqual(actual, DEFAULT_INTENTS)
                self.assertEqual(warnings, [])

    def test_a_whitespace_only_value_counts_as_unset_and_stays_silent(self):
        """⚠️ 迁移带来的**第四处**变化，方向是"更安静"：纯空白串从"告警"变成"静默"。

        改前 ``"   " in (None, "")`` 为假 ⇒ 走 ``int("   ")`` 抛 ``ValueError`` ⇒ 告警；
        助手的「没配」判据是「``None`` 或只含空白的字符串」⇒ 静默用默认值。
        这与纪律 1 一致（空白不是配置），因此判成**修正**而不是回归。
        """
        actual, warnings = read_intents_and_coerce_logs(intents="   ")
        self.assertEqual(actual, DEFAULT_INTENTS)
        self.assertEqual(warnings, [])

    def test_the_warning_names_the_key_the_value_and_the_fallback(self):
        """纪律 2 的文案三件套：哪个键 / 收到了什么（``%r``）/ 回落到什么。"""
        _actual, warnings = read_intents_and_coerce_logs(intents="oops")
        self.assertEqual(len(warnings), 1, "同一个问题只打一条告警：%r" % (warnings,))
        text = warnings[0]
        self.assertTrue(text.startswith(DISCORD_LOG_PREFIX), text)
        for fragment in ("intents", "'oops'", str(DEFAULT_INTENTS)):
            self.assertIn(fragment, text)

    def test_a_non_positive_bitmask_is_no_longer_silently_swallowed(self):
        """⚠️ 判据反退化：把 ``coerce_int(…, minimum=1)`` 改回改前那句
        ``return value if value > 0 else DEFAULT_INTENTS``，这条**必须红**
        —— 改前 ``intents=0`` / ``-1`` 是**静默**回落，日志里一个字都没有。
        """
        for raw in (0, -1, -(2 ** 31)):
            with self.subTest(raw=raw):
                actual, warnings = read_intents_and_coerce_logs(intents=raw)
                self.assertEqual(actual, DEFAULT_INTENTS)
                self.assertEqual(
                    len(warnings), 1,
                    "非正数必须告警：用户填了 %r 却在日志里看不到任何线索" % (raw,),
                )
                self.assertIn("intents", warnings[0])

    def test_a_boolean_is_not_read_as_the_bitmask_one(self):
        """⚠️ 判据反退化：改回裸 ``int(raw)`` 会让 ``intents=true`` **静默**拿到 1。

        而 1 不是本适配器用的任何一个 intent 位（512 / 4096 / 32768）⇒ 结果是
        "网关连上了但一条消息都收不到"，且没有任何日志说明原因。
        """
        actual, warnings = read_intents_and_coerce_logs(intents=True)
        self.assertEqual(actual, DEFAULT_INTENTS)
        self.assertEqual(len(warnings), 1)
        self.assertIn("intents", warnings[0])
        self.assertNotEqual(actual, 1, "1 不是任何 intent 位，绝不能被当成合法配置采纳")

    def test_a_float_is_not_truncated_into_a_silent_bitmask(self):
        """⚠️ 判据反退化：``int(4096.9) == 4096`` 不抛异常 ⇒ 改前是**静默截断**。

        JSON 没有 int/float 之分（``4096.0`` / ``1e3`` 都落到 Python 的 ``float``），
        所以整数档在共享层拒浮点是有意的取舍：告警点名了键与值，改成 ``4096`` 即可。
        """
        for raw in (4096.0, 4096.9, 1e3):
            with self.subTest(raw=raw):
                actual, warnings = read_intents_and_coerce_logs(intents=raw)
                self.assertEqual(actual, DEFAULT_INTENTS)
                self.assertEqual(len(warnings), 1)


if __name__ == "__main__":
    unittest.main()

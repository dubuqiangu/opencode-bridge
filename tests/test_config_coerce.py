"""共享配置类型强制助手（``opencode_bridge.config_coerce``）+ 两处接入点的测试。

钉住的缺陷（真实崩溃，会打死整条链路）
----------------------------------------
``matrix.sync_timeout_ms`` 与 ``email.dedupe_capacity`` 曾经是裸 ``int()``：::

    用户填了非整数 ⇒ 裸 int() 抛 ValueError ⇒ build() 包成 AdapterError
    ⇒ 该适配器被跳过 ⇒ 若它是唯一配置的平台 ⇒ usable == 0 ⇒ 整个桥 exit 1
    ⇒ 而报错文案是「没有任何可用适配器」，一个字都不提真因

⇒ 一个旋钮写错就能打死这条零容错关键路径。本文件钉住的是**两件事**：
① 非法值不再打死适配器（回落 + WARNING + 照常启动）；
② 合法值的解析结果与改动前**逐字节一致**（回归断言，见 ``TestLegalValueEquivalence``）。

⚠️ **结构断言用 AST，不用行级正则**（``tests`` 里那条纪律）：这两处的
``int(`` 与 ``self.config.get(`` 本来就**跨行**，行级正则必然漏 —— 本项目已因此
误判过一次「代码里没有裸 ``int()``」。
"""

from __future__ import annotations

import ast
import copy
import logging
import pathlib
import unittest

# 把适配器基类那些"预期内"的告警（空 allowed_chat_ids / 未配 pairing_secret）挡在
# 测试输出之外；``assertLogs`` / ``assertNoLogs`` 自己换 handler，不受影响。
logging.getLogger("opencode_bridge").addHandler(logging.NullHandler())

from opencode_bridge.adapters.base import build
from opencode_bridge.adapters.email import (
    DEDUPE_CAPACITY,
    THREAD_CACHE_CAPACITY,
)
from opencode_bridge.adapters.matrix import SYNC_TIMEOUT_MS
from opencode_bridge.config_coerce import (
    coerce_bool,
    coerce_float,
    coerce_int,
    coerce_text,
)

COERCE_LOGGER = "opencode_bridge.config_coerce"


class RecordingHooks:
    """够用的 hooks 替身（配置解析这一步不消费它）。"""

    def __init__(self) -> None:
        self.inbounds: list = []

    def on_inbound(self, inbound) -> None:
        self.inbounds.append(inbound)


def matrix_config(**extra) -> dict:
    """一份**可用**的 matrix 配置（``required_tokens`` 配齐），再叠上被测键。"""
    config = {
        "homeserver": "https://matrix.example.org",
        "access_token": "syt_fake_token",
        "user_id": "@bot:example.org",
    }
    config.update(extra)
    return config


def email_config(**extra) -> dict:
    """一份**可用**的 email 配置（``required_tokens`` 配齐），再叠上被测键。"""
    config = {
        "address": "bot@example.com",
        "password": "app-password",
        "imap_host": "imap.example.com",
        "smtp_host": "smtp.example.com",
    }
    config.update(extra)
    return config


# ----------------------------------------------------------------------
# ⚛️ 合法值等价性：参照实现逐字节抄自**改动前**工作树里的那两行表达式
# ----------------------------------------------------------------------
def pre_change_matrix_sync_timeout_ms(config: dict) -> int:
    """改动前的 ``MatrixAdapter.__init__``（抄自当时源码）：``int(cfg.get(...) or DEF)``。"""
    return int(config.get("sync_timeout_ms") or SYNC_TIMEOUT_MS)


def pre_change_email_dedupe_capacity(config: dict) -> int:
    """改动前的 ``EmailAdapter.__init__``：``max(1, int(cfg.get(...) or DEF))``。"""
    return max(1, int(config.get("dedupe_capacity") or DEDUPE_CAPACITY))


# ----------------------------------------------------------------------
# matrix.sync_timeout_ms
# ----------------------------------------------------------------------
class TestMatrixSyncTimeoutCoercion(unittest.TestCase):
    def test_non_integer_sync_timeout_does_not_kill_the_adapter(self):
        """本批的核心缺陷：``"30s"`` 曾经打死适配器（进而可能打死整个桥）。"""
        with self.assertLogs(COERCE_LOGGER, level="WARNING") as captured:
            adapter = build("matrix", matrix_config(sync_timeout_ms="30s"), RecordingHooks())
        self.assertEqual(adapter.sync_timeout_ms, SYNC_TIMEOUT_MS)
        # 告警必须说清三样：哪个键、收到了什么（%r）、回落到什么
        joined = "\n".join(captured.output)
        self.assertIn("matrix", joined)
        self.assertIn("sync_timeout_ms", joined)
        self.assertIn(repr("30s"), joined)
        self.assertIn(repr(SYNC_TIMEOUT_MS), joined)
        # 下游用法不受影响：/sync 的 timeout 参数确实带着回落后的值
        self.assertIn(f"timeout={SYNC_TIMEOUT_MS}", adapter._sync_path())

    def test_non_integer_sync_timeout_string_that_looks_numeric_still_falls_back(self):
        """``"30000.5"``（README 里点名的那个反例）是字符串 ⇒ 解析失败 ⇒ 回落 + 告警。"""
        with self.assertLogs(COERCE_LOGGER, level="WARNING") as captured:
            adapter = build(
                "matrix", matrix_config(sync_timeout_ms="30000.5"), RecordingHooks()
            )
        self.assertEqual(adapter.sync_timeout_ms, SYNC_TIMEOUT_MS)
        self.assertIn("sync_timeout_ms", "\n".join(captured.output))

    def test_blank_sync_timeout_is_treated_as_unset_and_stays_silent(self):
        """``"   "`` 曾经**打死适配器**（真值非空 ⇒ ``int("   ")`` 抛 ValueError）。

        它是"没配"而不是"配错"，所以回落 + **静默**（每次启动都刷警告同样是缺陷）。
        """
        for blank in ("", "   ", "\t"):
            with self.subTest(blank=blank):
                with self.assertNoLogs(COERCE_LOGGER, level="WARNING"):
                    adapter = build(
                        "matrix", matrix_config(sync_timeout_ms=blank), RecordingHooks()
                    )
                self.assertEqual(adapter.sync_timeout_ms, SYNC_TIMEOUT_MS)

    def test_absent_sync_timeout_is_silent(self):
        with self.assertNoLogs(COERCE_LOGGER, level="WARNING"):
            adapter = build("matrix", matrix_config(), RecordingHooks())
        self.assertEqual(adapter.sync_timeout_ms, SYNC_TIMEOUT_MS)

    def test_zero_sync_timeout_is_a_configured_value_not_unset(self):
        """``0`` = "不做长轮询，立即返回"，是 Matrix 协议的合法取值。

        改动前的 ``or`` 把它当成"没配" ⇒ **静默**改成 30000，用户配的意图被悄悄抹掉。
        现在它是一个配了的值（照 ``NextcloudAdapter._config_int_value`` 的
        ``raw in (None, "")``：只有缺失与空串算没配）。
        """
        with self.assertNoLogs(COERCE_LOGGER, level="WARNING"):
            adapter = build("matrix", matrix_config(sync_timeout_ms=0), RecordingHooks())
        self.assertEqual(adapter.sync_timeout_ms, 0)
        self.assertIn("timeout=0", adapter._sync_path())

    def test_sync_timeout_has_no_range_so_negative_values_are_honoured(self):
        """``sync_timeout_ms`` **刻意不设区间**：加自造上界只会把合法配置变成静默回落。"""
        with self.assertNoLogs(COERCE_LOGGER, level="WARNING"):
            adapter = build("matrix", matrix_config(sync_timeout_ms=-1), RecordingHooks())
        self.assertEqual(adapter.sync_timeout_ms, -1)


# ----------------------------------------------------------------------
# email.dedupe_capacity
# ----------------------------------------------------------------------
class TestEmailDedupeCapacityCoercion(unittest.TestCase):
    def test_non_integer_dedupe_capacity_does_not_kill_the_adapter(self):
        """本批的核心缺陷：``"2048条"`` 曾经打死适配器（进而可能打死整个桥）。"""
        with self.assertLogs(COERCE_LOGGER, level="WARNING") as captured:
            adapter = build(
                "email", email_config(dedupe_capacity="2048条"), RecordingHooks()
            )
        self.assertEqual(adapter.dedupe_capacity, DEDUPE_CAPACITY)
        joined = "\n".join(captured.output)
        self.assertIn("email", joined)
        self.assertIn("dedupe_capacity", joined)
        self.assertIn(repr("2048条"), joined)
        self.assertIn(repr(DEDUPE_CAPACITY), joined)
        # 去重集合真的按回落后的容量建起来了（不是只在字段上看着对）
        self.assertEqual(adapter._seen.capacity, DEDUPE_CAPACITY)
        self.assertEqual(adapter._sent.capacity, DEDUPE_CAPACITY)

    def test_dedupe_capacity_below_lower_bound_falls_back_and_warns(self):
        """下界 ``1`` 是区间 ⇒ 越界回落到默认（而不是被 ``max(1, …)`` 悄悄改成 1）。"""
        for below in (0, -5):
            with self.subTest(dedupe_capacity=below):
                with self.assertLogs(COERCE_LOGGER, level="WARNING") as captured:
                    adapter = build(
                        "email", email_config(dedupe_capacity=below), RecordingHooks()
                    )
                self.assertEqual(adapter.dedupe_capacity, DEDUPE_CAPACITY)
                joined = "\n".join(captured.output)
                self.assertIn("dedupe_capacity", joined)
                self.assertIn("越界", joined)
                self.assertIn(repr(below), joined)

    def test_dedupe_capacity_below_bound_is_never_silently_clamped_to_one(self):
        """⛔ 反向断言：退回 ``max(1, …)`` 就会红。

        静默改成 1 的后果是"去重集合几乎不记事" ⇒ 防回环这条最致命的保护名存实亡，
        而日志里一个字都没有。
        """
        with self.assertLogs(COERCE_LOGGER, level="WARNING"):
            adapter = build(
                "email", email_config(dedupe_capacity=-5), RecordingHooks()
            )
        self.assertNotEqual(adapter.dedupe_capacity, 1)
        self.assertNotEqual(adapter._seen.capacity, 1)

    def test_absent_dedupe_capacity_is_silent(self):
        with self.assertNoLogs(COERCE_LOGGER, level="WARNING"):
            adapter = build("email", email_config(), RecordingHooks())
        self.assertEqual(adapter.dedupe_capacity, DEDUPE_CAPACITY)

    def test_blank_dedupe_capacity_is_treated_as_unset_and_stays_silent(self):
        with self.assertNoLogs(COERCE_LOGGER, level="WARNING"):
            adapter = build("email", email_config(dedupe_capacity="  "), RecordingHooks())
        self.assertEqual(adapter.dedupe_capacity, DEDUPE_CAPACITY)


# ----------------------------------------------------------------------
# ⚛️ 合法值等价性（回归断言 —— 用改动前的表达式当参照实现）
# ----------------------------------------------------------------------
class TestLegalValueEquivalence(unittest.TestCase):
    """对**仍然合法**的取值（数值字符串 / 整数 / 带空白的字符串），
    新旧解析结果必须**逐字节一致**。

    参照实现 = 改动前工作树里那两行表达式，逐字节抄在上面的
    :func:`pre_change_matrix_sync_timeout_ms` / :func:`pre_change_email_dedupe_capacity`。

    ⚠️ **JSON 浮点已从这两张表里移出去**（改判成非法，见
    :class:`TestFractionalAndBooleanValuesAreNoLongerAccepted`）—— 移出去不是"放宽
    断言"，而是那张表的**前提**（"合法值"）已经不成立了：``30000.5`` 不再是合法的
    ``sync_timeout_ms``。留在表里会让 :meth:`assertNoLogs` 与"逐字节一致"两条断言
    同时变红，而红的真实含义是"语义变了"，该由新表来说明改前/改后是什么。
    """

    #: ⚠️ **刻意不含 ``0`` 与 ``-1``**：它们在改前会被 ``or SYNC_TIMEOUT_MS`` 吞掉
    #: （``0 or 30000`` → 30000），而现在是**配了的值**（``0`` 是 Matrix 长轮询的合法
    #: 取值）。那是上一批就有的、有意的语义变更，各自有专门用例
    #: （``test_zero_sync_timeout_is_a_configured_value_not_unset`` /
    #: ``test_sync_timeout_has_no_range_so_negative_values_are_honoured``）。
    #: 放进这张表会让"逐字节一致"变成断言一个**已经被推翻过**的结论。
    LEGAL_MATRIX_VALUES = ("30000", 30000, " 30000 ", "\t30000\n", 1)
    LEGAL_EMAIL_VALUES = ("2048", 2048, " 2048 ", "\n2048\t", 5)

    def test_legal_matrix_sync_timeout_values_match_pre_change_expression(self):
        for value in self.LEGAL_MATRIX_VALUES:
            with self.subTest(sync_timeout_ms=value):
                config = matrix_config(sync_timeout_ms=value)
                expected = pre_change_matrix_sync_timeout_ms(config)
                with self.assertNoLogs(COERCE_LOGGER, level="WARNING"):
                    adapter = build("matrix", config, RecordingHooks())
                self.assertEqual(adapter.sync_timeout_ms, expected)

    def test_legal_email_dedupe_capacity_values_match_pre_change_expression(self):
        for value in self.LEGAL_EMAIL_VALUES:
            with self.subTest(dedupe_capacity=value):
                config = email_config(dedupe_capacity=value)
                expected = pre_change_email_dedupe_capacity(config)
                with self.assertNoLogs(COERCE_LOGGER, level="WARNING"):
                    adapter = build("email", config, RecordingHooks())
                self.assertEqual(adapter.dedupe_capacity, expected)


# ----------------------------------------------------------------------
# ⚛️ 缺口①：float / bool 不再被静默采纳
# ----------------------------------------------------------------------
class TestFractionalAndBooleanValuesAreNoLongerAccepted(unittest.TestCase):
    """``coerce_int`` 曾把 ``True`` 读成 1、把 ``30000.5`` **截断**成 30000。

    两者都**不抛异常**，所以它们静默穿过了每一道判据 —— 而后果与纪律 3 禁止的
    "静默改小越界的值" 同型：**用户以为自己配的生效了，实际拿到另一个数，日志里
    一个字都没有**（``socket_timeout: true`` 曾真的让 email 拿 1 秒超时）。

    ⇒ 在**共享层**拒（不是每个调用点自己挡）：下一个迁到 ``coerce_int`` 的整数键会
    继承这条语义，调用点各挡一遍就等于"纪律又变成 6 份近似实现"。

    ⚠️ 旧实现的对照值是从**当前工作树**抄出来的参照实现算出来的，不是从 ``git`` 取的
    基线 —— ``git stash`` 会连别的 lane 的在制品一起收走（AGENTS.md §9）。
    """

    #: ``(配置值, 改前 coerce_int 给的数, 改后应当给的数)``
    #: 改前一列是**实测**的（``probe_semantics.py`` 跑改动前的 ``config_coerce``），
    #: 逐条列在这里是为了让"改了什么"可核对，而不是靠读者相信 docstring。
    MATRIX_BEFORE_AFTER = [
        (True, 1, SYNC_TIMEOUT_MS),          # bool：曾读成 1（"1ms 长轮询"）
        (False, 0, SYNC_TIMEOUT_MS),         # 曾读成 0（"立即返回"，但用户配的是 false）
        (30000.0, 30000, SYNC_TIMEOUT_MS),   # 整值 float：曾"碰巧"等于默认值
        (30000.5, 30000, SYNC_TIMEOUT_MS),   # 截断后碰巧等于默认值 —— 最危险的一档
        (12345.9, 12345, SYNC_TIMEOUT_MS),   # ⭐ 截断真的改值了：12345 → 30000
        ("30000.5", "RAISE ValueError", SYNC_TIMEOUT_MS),   # 字符串本来就非法
    ]
    EMAIL_BEFORE_AFTER = [
        (True, 1, DEDUPE_CAPACITY),          # 曾读成 1 ⇒ 去重集合只记 1 个 Message-ID
        (False, 0, DEDUPE_CAPACITY),
        (2048.0, 2048, DEDUPE_CAPACITY),
        (2048.9, 2048, DEDUPE_CAPACITY),
        (17.9, 17, DEDUPE_CAPACITY),         # ⭐ 截断真的改值了
        ("2048.9", "RAISE ValueError", DEDUPE_CAPACITY),
    ]

    def test_matrix_sync_timeout_refuses_floats_and_booleans(self):
        for value, before, after in self.MATRIX_BEFORE_AFTER:
            with self.subTest(sync_timeout_ms=value):
                with self.assertLogs(COERCE_LOGGER, level="WARNING") as captured:
                    adapter = build(
                        "matrix", matrix_config(sync_timeout_ms=value), RecordingHooks()
                    )
                self.assertEqual(adapter.sync_timeout_ms, after)
                joined = "\n".join(captured.output)
                self.assertIn("sync_timeout_ms", joined)
                self.assertIn(repr(value), joined)
                self.assertIn(repr(SYNC_TIMEOUT_MS), joined)

    def test_email_dedupe_capacity_refuses_floats_and_booleans(self):
        for value, before, after in self.EMAIL_BEFORE_AFTER:
            with self.subTest(dedupe_capacity=value):
                with self.assertLogs(COERCE_LOGGER, level="WARNING") as captured:
                    adapter = build(
                        "email", email_config(dedupe_capacity=value), RecordingHooks()
                    )
                self.assertEqual(adapter.dedupe_capacity, after)
                # 去重集合真的按回落后的容量建起来了（不是只在字段上看着对）
                self.assertEqual(adapter._seen.capacity, after)
                joined = "\n".join(captured.output)
                self.assertIn("dedupe_capacity", joined)
                self.assertIn(repr(value), joined)

    def test_a_boolean_is_never_read_as_one_or_zero(self):
        """⛔ 反向断言：把类型挡板删掉，这几条立刻红。

        ``True`` 曾被读成 1 ⇒ ``dedupe_capacity`` 只记 **1** 个 Message-ID，而防回环
        的整个前提（"这封信是我们自己发出去的"）要在 2048 条的窗口里认出来。
        """
        for value, truthy_reading in ((True, 1), (False, 0)):
            with self.subTest(value=value):
                with self.assertLogs(COERCE_LOGGER, level="WARNING"):
                    self.assertEqual(
                        coerce_int({"n": value}, "n", 4242, minimum=1), 4242
                    )
                with self.assertLogs(COERCE_LOGGER, level="WARNING"):
                    self.assertNotEqual(
                        coerce_int({"n": value}, "n", 4242), truthy_reading
                    )

    def test_the_warning_says_why_the_value_was_refused(self):
        """告警必须说清"为什么"（类型不对），而不是只说"不是整数"。"""
        for value, expected_fragment in ((True, "bool"), (42.9, "float")):
            with self.subTest(value=value):
                with self.assertLogs(COERCE_LOGGER, level="WARNING") as captured:
                    coerce_int({"n": value}, "n", 7)
                self.assertIn(expected_fragment, "\n".join(captured.output))

    def test_float_tier_still_accepts_floats_but_also_refuses_bools(self):
        """浮点档的例外**只**关于 ``float``；``bool`` 两档都拒（同一个谎）。"""
        with self.assertNoLogs(COERCE_LOGGER, level="WARNING"):
            self.assertEqual(coerce_float({"t": 30.5}, "t", 1.0), 30.5)
            self.assertEqual(coerce_float({"t": 30}, "t", 1.0), 30.0)     # int 精确，无损
        for value in (True, False):
            with self.subTest(value=value):
                with self.assertLogs(COERCE_LOGGER, level="WARNING") as captured:
                    self.assertEqual(coerce_float({"t": value}, "t", 2.5), 2.5)
                self.assertIn("bool", "\n".join(captured.output))

    def test_the_shared_layer_agrees_with_the_pre_change_integer_semantics_except_for_these(self):
        """⚛️ 判据本体：改前 / 改后**只**在 bool 与 float 上分岔，其余取值完全一致。

        写法上刻意逐个取值枚举（而不是"抽查几个"）—— 本任务要回答的问题是
        "有没有别的取值被顺手改掉了"，那只有枚举能回答。

        ⚠️ 参照实现与被测**必须同区间**：``pre_change_matrix_sync_timeout_ms`` 内部没有
        区间（matrix 那个键不设界），所以这里也用不设界的 ``coerce_int``。区间是
        调用点的事，不是解析的语义。
        """
        #: ``(值, 改前裸 int() 抛不抛)``。取值**用尽**，因为本任务要回答的问题是
        #: "有没有别的取值被顺手改掉了"。
        self._check_unchanged_values(
            None, "", "   ", 1, -1, 65535, 65536, 10 ** 12,
            "0", "9900", " 9900 ", "\t42\n", "nope", "1_0_0x", object(),
        )

    def test_falsy_containers_keep_their_value_but_are_now_announced(self):
        """``[]`` / ``{}``：**取值一致，日志变了**（静默 → 告警）。

        旧 ``int([] or DEF)`` 因 ``[]`` 为假而落到默认值，**一个字都不说**；
        现在它被当成"配了但非法"⇒ 告警点名键与值。取值相同（都是默认值），变的
        只有可观测性 —— 而这正是纪律 2 要求的。
        """
        for value in ([], {}):
            with self.subTest(value=value):
                self.assertEqual(
                    pre_change_matrix_sync_timeout_ms({"sync_timeout_ms": value}),
                    SYNC_TIMEOUT_MS,
                )
                with self.assertLogs(COERCE_LOGGER, level="WARNING") as logs:
                    self.assertEqual(
                        coerce_int({"n": value}, "n", SYNC_TIMEOUT_MS), SYNC_TIMEOUT_MS
                    )
                self.assertIn(repr(value), "\n".join(logs.output))

    def _check_unchanged_values(self, *values: object) -> None:
        for value in values:
            with self.subTest(value=value):
                config = {"n": value}
                try:
                    expected = pre_change_matrix_sync_timeout_ms(
                        {"sync_timeout_ms": value}
                    )
                    raised = False
                except (TypeError, ValueError):
                    expected, raised = SYNC_TIMEOUT_MS, True
                if raised:
                    # 改前会崩的（这就是本仓库那条零容错关键路径）⇒ 改后必须回落。
                    # ⚠️ 空白串是例外：它现在算"**没配**"（纪律 1）⇒ **静默**，不告警。
                    if isinstance(value, str) and not value.strip():
                        with self.assertNoLogs(COERCE_LOGGER, level="WARNING"):
                            self.assertEqual(
                                coerce_int(config, "n", SYNC_TIMEOUT_MS),
                                SYNC_TIMEOUT_MS,
                            )
                    else:
                        with self.assertLogs(COERCE_LOGGER, level="WARNING"):
                            self.assertEqual(
                                coerce_int(config, "n", SYNC_TIMEOUT_MS),
                                SYNC_TIMEOUT_MS,
                            )
                    continue
                with self.assertNoLogs(COERCE_LOGGER, level="WARNING"):
                    self.assertEqual(
                        coerce_int(config, "n", SYNC_TIMEOUT_MS), expected
                    )

    def test_a_blank_string_is_treated_as_unset_even_though_it_used_to_crash(self):
        """⚠️ 上一条里那条例外的理由：``"   "`` 曾**打死适配器**。

        它是"没配"而不是"配错"（照 ``NextcloudAdapter._config_int_value`` 的
        ``raw in (None, "")``），所以回落 + **静默** —— 每次启动都刷警告同样是缺陷。
        矩阵与 email 各有一条端到端用例（``test_blank_*_is_treated_as_unset_and_stays_silent``）。
        """
        for blank in ("", "   ", "\t", "\n"):
            with self.subTest(blank=blank):
                with self.assertNoLogs(COERCE_LOGGER, level="WARNING"):
                    self.assertEqual(
                        coerce_int({"n": blank}, "n", SYNC_TIMEOUT_MS), SYNC_TIMEOUT_MS
                    )

    def test_the_only_intentional_divergences_from_the_old_bare_int_are_bool_and_float(self):
        """⚛️ 分岔的**完整清单**：本任务只允许 ``bool`` 与 ``float`` 两类取值与
        "改前的裸 ``int()``"分岔，其余取值见上面那条（逐值同答）。

        ⚠️ ``0`` **不在**这两类里，但确实分岔了 —— 那分岔来自**上一批**（``or`` 把配了
        的 ``0`` 当成"没配"，静默改成默认值）。本任务不撤销它：撤销等于让用户配的
        "不做长轮询"被悄悄抹掉，正是纪律 3 要防的那件事。
        """
        for value, before, after, warns in (
            (True, 1, SYNC_TIMEOUT_MS, True),        # 旧 ``int(True)`` → 1
            (False, SYNC_TIMEOUT_MS, SYNC_TIMEOUT_MS, True),  # 旧 ``or`` 当"没配"→ 静默
            (0, SYNC_TIMEOUT_MS, 0, False),          # 上一批的变更（非本任务引入）
        ):
            with self.subTest(value=value):
                before_value = pre_change_matrix_sync_timeout_ms(
                    {"sync_timeout_ms": value}
                )
                self.assertEqual(before_value, before, "改前的取值记录错了")
                if warns:
                    with self.assertLogs(COERCE_LOGGER, level="WARNING"):
                        self.assertEqual(
                            coerce_int({"n": value}, "n", SYNC_TIMEOUT_MS), after
                        )
                else:
                    with self.assertNoLogs(COERCE_LOGGER, level="WARNING"):
                        self.assertEqual(
                            coerce_int({"n": value}, "n", SYNC_TIMEOUT_MS), after
                        )


# ----------------------------------------------------------------------
# 共享助手自身
# ----------------------------------------------------------------------
class TestSharedHelperDiscipline(unittest.TestCase):
    def test_unset_keys_are_silent(self):
        """纪律 1：没配 ⇒ 静默用默认值（否则每次启动刷屏，真正的错误被淹没）。"""
        unset_cases = [
            {},                                # 键不存在
            {"port": None},
            {"port": ""},
            {"port": "   "},
        ]
        for config in unset_cases:
            with self.subTest(config=config):
                with self.assertNoLogs(COERCE_LOGGER, level="WARNING"):
                    self.assertEqual(coerce_int(config, "port", 8080), 8080)
                    self.assertEqual(coerce_float(config, "port", 1.5), 1.5)
                    self.assertIs(coerce_bool(config, "tls", True), True)
                    self.assertEqual(coerce_text(config, "host", "localhost"), "localhost")

    def test_invalid_values_warn_and_fall_back(self):
        """纪律 2：配了但非法 ⇒ 告警说清「哪个键 / 收到了什么 / 回落到什么」+ 回落。"""
        with self.assertLogs(COERCE_LOGGER, level="WARNING") as captured:
            self.assertEqual(coerce_int({"port": "8080s"}, "port", 8080, platform="irc"), 8080)
        joined = "\n".join(captured.output)
        self.assertIn("irc", joined)
        self.assertIn("port", joined)
        self.assertIn(repr("8080s"), joined)
        self.assertIn(repr(8080), joined)

    def test_invalid_values_do_not_raise(self):
        """⭐ 纪律 2 的**根因**那条：裸 ``int()`` / ``float()`` 会抛，
        而抛出去会打死整条链路（见模块 docstring）。所以这一档的值只能是容器与对象。

        ⚠️ ``42.9`` / ``True`` 曾放在这张表里被当成"非法值"处理，**其实它们
        根本不抛** —— 那正是它们危险的原因（静默穿过）。它们现在由
        :class:`TestFractionalAndBooleanValuesAreNoLongerAccepted` 单独钉。
        """
        for value in ("abc", [], {}, object(), "1_0_0x"):
            with self.subTest(value=value):
                with self.assertLogs(COERCE_LOGGER, level="WARNING"):
                    self.assertEqual(coerce_int({"k": value}, "k", 7), 7)
                with self.assertLogs(COERCE_LOGGER, level="WARNING"):
                    self.assertEqual(coerce_float({"k": value}, "k", 7.5), 7.5)

    def test_int_out_of_range_falls_back_with_range_in_the_warning(self):
        cases = [
            ({"n": 0}, 1, 10, "0"),           # 低于下界
            ({"n": 50}, 1, 10, "50"),         # 高于上界
            ({"n": -3}, 1, None, "-3"),       # 只有下界
            ({"n": 99}, None, 10, "99"),      # 只有上界
            ({"n": " 0 "}, 1, None, "' 0 '"),  # 字符串也一样被拒（先解析再校验）
        ]
        for config, minimum, maximum, repr_of_value in cases:
            with self.subTest(config=config, minimum=minimum, maximum=maximum):
                with self.assertLogs(COERCE_LOGGER, level="WARNING") as captured:
                    self.assertEqual(
                        coerce_int(config, "n", 42, minimum=minimum, maximum=maximum), 42
                    )
                joined = "\n".join(captured.output)
                self.assertIn("越界", joined)
                self.assertIn(repr_of_value, joined)
                self.assertIn(repr(42), joined)

    def test_int_without_bounds_accepts_any_integer_silently(self):
        """无区间 = 只做类型强制（matrix 的 ``sync_timeout_ms`` 就是这一档）。"""
        for value in (-1, 0, 7, 10 ** 12):
            with self.subTest(value=value):
                with self.assertNoLogs(COERCE_LOGGER, level="WARNING"):
                    self.assertEqual(coerce_int({"n": value}, "n", 99), value)

    def test_int_accepts_numeric_strings_including_padded_ones(self):
        """**数值字符串照常认** —— JSON 里写 ``"42"`` 是常事，拒它没有理由。

        ⚠️ 与之对照：``42.9`` 这种 ``float`` 被拒（见
        :class:`TestFractionalAndBooleanValuesAreNoLongerAccepted`）。两者不矛盾：
        字符串里的 ``42`` 没有小数部分要丢，``float`` 有。
        """
        with self.assertNoLogs(COERCE_LOGGER, level="WARNING"):
            self.assertEqual(coerce_int({"n": "42"}, "n", 0), 42)
            self.assertEqual(coerce_int({"n": " 42 "}, "n", 0), 42)
            self.assertEqual(coerce_int({"n": "\t-42\n"}, "n", 0), -42)

    def test_exclusive_minimum_rejects_the_boundary_value(self):
        """``socket_timeout`` 要的是"**大于** 0"：``0`` 不是"很短的超时"，是不超时。

        ⚠️ ``minimum=0`` 收下 ``0`` ⇒ ``socket.settimeout(0)`` = 非阻塞 socket，
        那正是本平台最致命的那种失效（卡住的 ``stop()`` 白等满 join 超时）。
        """
        with self.assertLogs(COERCE_LOGGER, level="WARNING") as captured:
            self.assertEqual(
                coerce_float({"t": 0}, "t", 30.0, exclusive_minimum=0.0), 30.0
            )
        joined = "\n".join(captured.output)
        self.assertIn("越界", joined)
        self.assertIn("> 0.0", joined)
        with self.assertNoLogs(COERCE_LOGGER, level="WARNING"):
            self.assertEqual(
                coerce_float({"t": 0.001}, "t", 30.0, exclusive_minimum=0.0), 0.001
            )
        # 整数档同样支持（两个公开包装的纪律必须一致）
        with self.assertLogs(COERCE_LOGGER, level="WARNING"):
            self.assertEqual(coerce_int({"n": 0}, "n", 9, exclusive_minimum=0), 9)
        with self.assertNoLogs(COERCE_LOGGER, level="WARNING"):
            self.assertEqual(coerce_int({"n": 1}, "n", 9, exclusive_minimum=0), 1)

    def test_float_rejects_nan_and_infinity(self):
        """NaN / inf 能解析成功，但任何比较都是假的 ⇒ 必须回落 + 告警。"""
        for value in ("nan", "inf", "-inf", float("nan"), float("inf")):
            with self.subTest(value=value):
                with self.assertLogs(COERCE_LOGGER, level="WARNING") as captured:
                    self.assertEqual(coerce_float({"t": value}, "t", 2.5), 2.5)
                self.assertIn("有限", "\n".join(captured.output))

    def test_float_range_is_enforced(self):
        with self.assertLogs(COERCE_LOGGER, level="WARNING"):
            self.assertEqual(coerce_float({"t": "90"}, "t", 30.0, maximum=60.0), 30.0)
        with self.assertNoLogs(COERCE_LOGGER, level="WARNING"):
            self.assertEqual(coerce_float({"t": " 30.5 "}, "t", 1.0, maximum=60.0), 30.5)

    def test_bool_accepts_words_and_numbers_and_rejects_the_rest(self):
        truthy = (True, 1, "1", "true", "TRUE", " yes ", "on")
        falsy = (False, 0, "0", "false", "No", " off ")
        for value in truthy:
            with self.subTest(value=value):
                with self.assertNoLogs(COERCE_LOGGER, level="WARNING"):
                    self.assertIs(coerce_bool({"v": value}, "v", False), True)
        for value in falsy:
            with self.subTest(value=value):
                with self.assertNoLogs(COERCE_LOGGER, level="WARNING"):
                    self.assertIs(coerce_bool({"v": value}, "v", True), False)

    def test_bool_has_no_third_state(self):
        """认不出来的值不许"宽松地"变成某一侧 —— 那会让 ``verify_tls`` 静默降级。"""
        with self.assertLogs(COERCE_LOGGER, level="WARNING") as captured:
            self.assertIs(coerce_bool({"verify_tls": "maybe"}, "verify_tls", True), True)
        joined = "\n".join(captured.output)
        self.assertIn("verify_tls", joined)
        self.assertIn(repr("maybe"), joined)

    def test_text_strips_whitespace_and_stringifies_non_strings(self):
        with self.assertNoLogs(COERCE_LOGGER, level="WARNING"):
            self.assertEqual(coerce_text({"h": "  example.org \n"}, "h", ""), "example.org")
            self.assertEqual(coerce_text({"h": 8443}, "h", ""), "8443")

    def test_helpers_do_not_mutate_config_and_are_idempotent(self):
        """一次调用不得改动传入的配置对象；同输入重复调用结果必须一致。"""
        config = {"n": "42", "f": "1.5", "b": "yes", "t": "  host  "}
        snapshot = copy.deepcopy(config)
        for _ in range(3):
            self.assertEqual(coerce_int(config, "n", 0), 42)
            self.assertEqual(coerce_float(config, "f", 0.0), 1.5)
            self.assertIs(coerce_bool(config, "b", False), True)
            self.assertEqual(coerce_text(config, "t", ""), "host")
            self.assertEqual(config, snapshot)


# ----------------------------------------------------------------------
# ⚛️ 结构断言（AST，不是行级正则）
# ----------------------------------------------------------------------
ADAPTER_DIR = pathlib.Path(__file__).resolve().parent.parent / "opencode_bridge" / "adapters"


def _reads_config(node: ast.AST) -> bool:
    """子树里是否有 ``self.config.get(...)`` 调用（**任意深度**）。

    跨行表达式（``int(\\n    self.config.get(...) or DEF\\n)``）在 AST 里是一个
    ``BoolOp`` 包着 ``Call``，所以必须递归看整棵子树。
    """
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr == "get"
            and isinstance(child.func.value, ast.Attribute)
            and child.func.value.attr == "config"
        ):
            return True
    return False


def _coercions_in_source(
    source: str, function_names: frozenset[str]
) -> list[tuple[str, str]]:
    """在一段源码文本里列出 ``(方法名, 裸转换名)``（供反退化测试喂样本用）。"""
    tree = ast.parse(source)
    found: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in function_names:
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call) or not isinstance(inner.func, ast.Name):
                continue
            if inner.func.id not in ("int", "float"):
                continue
            if any(_reads_config(argument) for argument in inner.args):
                found.append((node.name, inner.func.id))
    return found


def bare_numeric_coercions(
    path: pathlib.Path, function_names: frozenset[str]
) -> list[tuple[str, int, str]]:
    """列出指定方法里"裸 ``int()`` / ``float()`` 包住 ``self.config.get(...)``"。

    ⚠️ ``ast.walk`` 会吐出**没有** ``lineno`` 的节点（``ast.arguments`` 等），所以
    只在 ``isinstance(node, ast.Call)`` 之后才取位置信息 —— 别在它的结果上直接取
    ``.lineno``（本项目因此踩过一次）。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path.name))
    found: list[tuple[str, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in function_names:
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call) or not isinstance(inner.func, ast.Name):
                continue
            if inner.func.id not in ("int", "float"):
                continue
            if any(_reads_config(argument) for argument in inner.args):
                found.append((node.name, inner.lineno, inner.func.id))
    return found


#: ``__init__`` 与 ``start()`` **一起**看 —— 只看前者就是缺口②3 长期没被发现的
#: 原因（``email.start()`` 里那处裸 ``float()`` 就在 ``__init__`` 之外）。
LIFECYCLE_METHODS = frozenset({"__init__", "start"})


class TestSharedIntegerLayerAgreesWithThePerPlatformOldImplementations(unittest.TestCase):
    """验收判据：**共享层与各平台旧实现在每一个可枚举取值上同答**。

    这里对 a2a 的 ``_coerce_port`` 做**穷举**对照（而不是抽查几个值）—— 因为本任务
    改的正是共享层的语义，而"上一批那条一致性测试现在还红不红"是唯一的判据。

    ⚠️ **逐个取值枚举，不抽样**：抽样证不了"没有别的取值分岔了"。

    ⚠️ **``bytes`` 是唯一已知的例外**（``int(b"9900")`` 在 Python 里成功，而
    ``_coerce_port`` 走 ``int(str(v).strip())`` ⇒ ``int("b'9900'")`` 失败）。JSON
    解析器**产不出** ``bytes``（``json.loads`` 只给 dict / list / str / int / float /
    bool / None），所以它不是用户可达的取值 —— 写在这里是为了让下一个人不必重新发现。
    """

    #: 与 :data:`test_config_runnable_verdict` 那条一致性测试同源，但**取值更宽**。
    ENUMERATED_VALUES = (
        None, "", "   ", "0", "1", "65535", "65536", "-1", "9900", " 9900 ",
        "nope", 0, 1, -1, 65535, 65536, 9900, True, False,
        0.0, -0.0, 9900.0, 9900.7, 0.5, -5.0, float("nan"), float("inf"),
        1e3, [], {}, ("x",), 10 ** 20,
    )

    def test_it_answers_exactly_like_coerce_port_on_every_enumerable_value(self):
        from opencode_bridge.adapters.a2a import (
            MAX_BIND_PORT,
            MIN_BIND_PORT,
            _coerce_port,
        )

        for value in self.ENUMERATED_VALUES:
            with self.subTest(bind_port=value):
                entry = {} if value is None else {"bind_port": value}
                shared_says_runnable = (
                    coerce_int(
                        entry, "bind_port", -1,
                        minimum=MIN_BIND_PORT, maximum=MAX_BIND_PORT,
                    ) >= 0
                )
                adapter_says_runnable = (
                    _coerce_port(entry.get("bind_port")) >= 0
                )
                self.assertEqual(
                    shared_says_runnable, adapter_says_runnable,
                    f"bind_port={value!r}: 共享层与 a2a 自己的 _coerce_port 不一致",
                )

    def test_the_a2a_type_guard_becomes_redundant_after_this_change(self):
        """⚠️ 记录一条**已知的冗余**，而不是顺手去删它。

        :meth:`A2aAdapter.config_runnable` 里那个
        ``if isinstance(raw, (bool, float)): return False`` 挡板，在共享层拒掉
        bool / float 之后**不再必要** —— 上面那条已经证明两者同答。
        ⇒ 本任务**保留**它：① ``adapters/a2a.py`` 不在本任务的可改文件里；
        ② 留着它是**无害且更保守**的（两条独立判据给同一个答案）；
        ③ 删它会让 ``test_a_float_or_bool_port_is_refused_by_both_paths`` 少一重保障。
        这条测试的作用是让"将来谁删了它，会知道它已经冗余"。

        ⛔ 本条**不**断言"挡板必须存在"（那是 a2a 的实现细节，不是共享层的契约）。
        """
        from opencode_bridge.adapters.a2a import MAX_BIND_PORT, MIN_BIND_PORT

        # 证明"没有挡板也同答"：这里只调共享层，绕开 config_runnable。
        for value in (True, False, 0.0, 9900.0, 9900.7):
            with self.subTest(value=value):
                self.assertEqual(
                    coerce_int(
                        {"bind_port": value}, "bind_port", -1,
                        minimum=MIN_BIND_PORT, maximum=MAX_BIND_PORT,
                    ),
                    -1,
                    f"{value!r} 由共享层自己判成未配置 —— 挡板确实已冗余",
                )
        self.assertLess(MIN_BIND_PORT, MAX_BIND_PORT)


class TestTheTwoRemainingClampsAreNotConfigReachable(unittest.TestCase):
    """任务③（只查不改）的结论被钉住：``max(1, int(...))`` 那两处**不读配置**。

    ``EmailAdapter.__init__`` 里 ``max(1, int(self.thread_cache_capacity))`` 与
    :meth:`_BoundedIdSet.__init__` 里 ``max(1, int(capacity))`` 是同一纪律的两处，
    而结构断言抓不到它们 —— 因为它们取的是**类属性**，不是 ``self.config.get(...)``。

    ⇒ 结论：**当前无用户可见风险**，两条通路都到不了用户输入：
    ① ``thread_cache_capacity`` 是类属性 :data:`THREAD_CACHE_CAPACITY` = 256，
    只被测试在实例上覆盖，没有配置键；
    ② ``_BoundedIdSet`` 的两个调用点传的都是 ``self.dedupe_capacity``，而它已经过
    :func:`coerce_int` 且 ``minimum=1`` ⇒ **恒 ≥ 1**，那层夹取永远不生效。

    ⚠️ 唯一的残留风险是**未来**：若有人把配置键接到这两条通路上，
    ``max(1, ...)`` 会把 ``0`` / ``-5`` **静默**改成 1（纪律 3 禁的那种）。
    下面两条测试就是让"接上去"这件事必须被看见的。
    """

    def test_the_thread_capacity_clamp_does_not_read_configuration(self):
        source = (ADAPTER_DIR / "email.py").read_text(encoding="utf-8")
        self.assertIn("max(1, int(self.thread_cache_capacity))", source)
        #: 类属性的值是一个**模块常量**，不是 ``config.get`` 的结果。
        self.assertIn("thread_cache_capacity = THREAD_CACHE_CAPACITY", source)

    def test_the_bounded_id_set_clamp_only_ever_sees_an_already_bounded_capacity(self):
        """``_BoundedIdSet`` 的容量恒 ≥ 1 ⇒ 那层 ``max(1, ...)`` 永不生效。

        钉住"传进去的值恒已合法"这一**前提**：一旦有人绕过 :func:`coerce_int` 直接把
        配置值传进来，这里就会静默夹取 —— 而防回环的失效方式是**最难发现**的那种。
        """
        adapter = build("email", email_config(dedupe_capacity=1), RecordingHooks())
        self.assertEqual(adapter.dedupe_capacity, 1)
        self.assertGreaterEqual(adapter._seen.capacity, 1)
        self.assertGreaterEqual(adapter._sent.capacity, 1)
        # 默认那份也必须是正数（否则夹取一旦生效就说明上游判据坏了）
        default_adapter = build("email", email_config(), RecordingHooks())
        self.assertEqual(default_adapter._seen.capacity, DEDUPE_CAPACITY)
        self.assertEqual(default_adapter._thread_capacity, THREAD_CACHE_CAPACITY)
        self.assertEqual(THREAD_CACHE_CAPACITY, 256)


class TestNoBareNumericCoercionInAdapterLifecycles(unittest.TestCase):
    """全仓库适配器的 ``__init__`` **与** ``start()`` 里不再有裸数值强制。

    这是**结构**断言：它不点名某个键，所以下次有人新写一处同样的裸强制（跨行、
    包在 ``or`` / 条件表达式里都算）会被当场抓住，而不是等它打死某条链路。

    ⚠️ ``start()`` 里的裸强制**比** ``__init__`` 轻一等（``BridgeCore.start()`` 的
    try/except 会兜住，适配器起不来而已）—— 但那正是它容易长期存活的原因：
    "不会打死整个桥"听起来像"不算缺陷"。本断言不区分轻重。
    """

    def _offenders(self) -> list[str]:
        found: list[str] = []
        for path in sorted(ADAPTER_DIR.glob("*.py")):
            for function_name, line_number, cast_name in bare_numeric_coercions(
                path, LIFECYCLE_METHODS
            ):
                found.append(
                    f"{path.name}:{line_number}: {function_name} 里的裸 "
                    f"{cast_name}(...) 包住了 self.config.get(...)"
                )
        return found

    #: 已知的、范围外的遗留。**刻意写成"恰好这些"而不是过滤** ——
    #: 过滤会让「某某修好了」这件事**无声地**通过，而账目本身就该在债务还清时变红。
    #: ✅ **2026-10-06：已清空。** 唯一那条 `ntfy` 的 `start()` 裸 `float()` 已迁到
    #: `coerce_float`（`exclusive_minimum=0.0`）⇒ 这张表现在**必须**是空的。
    #: 下一个同类缺口进来时，请把它连同"为什么本批不能修"一起写进来。
    KNOWN_OUT_OF_SCOPE: tuple = ()

    def test_no_adapter_lifecycle_method_wraps_config_get_in_bare_int_or_float(self):
        """⚠️ 判据本身（外加一张**显式**的已知遗留表 —— 见 :attr:`KNOWN_OUT_OF_SCOPE`）。"""
        offenders = self._offenders()
        self.assertEqual(
            sorted(offenders), sorted(self.KNOWN_OUT_OF_SCOPE),
            "适配器 __init__ / start() 里的裸 int()/float() 必须走 "
            "opencode_bridge.config_coerce（否则配置写错会让适配器起不来或"
            "打死整个桥）。\n"
            "若 ntfy 已迁走，请把 KNOWN_OUT_OF_SCOPE 里那一条划掉 —— "
            "留着会让真正的存量被藏进'已知遗留'里。\n  "
            + "\n  ".join(offenders),
        )

    def test_the_start_method_really_is_covered_and_not_just_declared(self):
        """⭐ 反"恒空"测试：判据真的看得见 ``start()`` 里的裸强制。

        ⚠️ 只在 :meth:`test_no_adapter_lifecycle_method...` 里断言" offenders 为空"
        时，若 :data:`LIFECYCLE_METHODS` 漏了 ``"start"``，那条**照样全绿** ——
        而那正是缺口②3 长期没被发现的原因。喂一段带 ``start()`` 的跨行样本，
        要求判据抓到它。
        """
        sample = (
            "class Sample:\n"
            "    def start(self) -> None:\n"
            "        self.transport = PollingTransport(\n"
            "            self.fetch,\n"
            "            idle_sleep=float(\n"
            "                self.config.get('poll_interval') or 60\n"
            "            ),\n"
            "        )\n"
        )
        self.assertEqual(
            _coercions_in_source(sample, LIFECYCLE_METHODS),
            [("start", "float")],
            "判据没抓到 start() 里的跨行 float(cfg.get(...) or ...) —— "
            "LIFECYCLE_METHODS 或 AST 遍历坏了（空集 ≠ 不存在，AGENTS.md §7.1）",
        )

    def test_the_ast_check_actually_sees_through_cross_line_expressions(self):
        """反退化测试：判据本身必须能抓到跨行的 ``int(cfg.get(...) or DEF)``。

        ⚠️ 没有这条，一个"只认同一行"的判据会恒空地通过（本项目已因此误判过一次
        「代码里没有裸 ``int()``」—— 空集 ≠ 不存在，见 AGENTS.md §7.1）。
        """
        sample = (
            "class Sample:\n"
            "    def __init__(self) -> None:\n"
            "        self.timeout = int(\n"
            "            self.config.get('timeout') or 30000\n"
            "        )\n"
        )
        self.assertEqual(_coercions_in_source(sample, LIFECYCLE_METHODS), [("__init__", "int")])


if __name__ == "__main__":  # pragma: no cover - 手动跑这一个文件用
    unittest.main()

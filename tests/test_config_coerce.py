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
from opencode_bridge.adapters.email import DEDUPE_CAPACITY
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
    """对**合法值**（字符串 / 整数 / 带空白的字符串 / JSON 浮点），
    新旧解析结果必须**逐字节一致**。

    参照实现 = 改动前工作树里那两行表达式，逐字节抄在上面的
    :func:`pre_change_matrix_sync_timeout_ms` / :func:`pre_change_email_dedupe_capacity`。
    """

    LEGAL_MATRIX_VALUES = ("30000", 30000, " 30000 ", "\t30000\n", 30000.0, 30000.5, 1)
    LEGAL_EMAIL_VALUES = ("2048", 2048, " 2048 ", "\n2048\t", 2048.0, 2048.9, 5)

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
        with self.assertNoLogs(COERCE_LOGGER, level="WARNING"):
            self.assertEqual(coerce_int({"n": "42"}, "n", 0), 42)
            self.assertEqual(coerce_int({"n": " 42 "}, "n", 0), 42)
            self.assertEqual(coerce_int({"n": 42.9}, "n", 0), 42)   # 截断，与裸 int() 一致

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


def bare_numeric_coercions(path: pathlib.Path) -> list[tuple[str, int, str]]:
    """列出 ``__init__`` 里"裸 ``int()`` / ``float()`` 包住 ``self.config.get(...)``"。

    ⚠️ ``ast.walk`` 会吐出**没有** ``lineno`` 的节点（``ast.arguments`` 等），所以
    只在 ``isinstance(node, ast.Call)`` 之后才取位置信息。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path.name))
    found: list[tuple[str, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != "__init__":
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call) or not isinstance(inner.func, ast.Name):
                continue
            if inner.func.id not in ("int", "float"):
                continue
            if any(_reads_config(argument) for argument in inner.args):
                found.append((node.name, inner.lineno, inner.func.id))
    return found


class TestNoBareNumericCoercionInAdapterInits(unittest.TestCase):
    """全仓库适配器的 ``__init__`` 里不再有裸 ``int(self.config.get(...))``。

    这是**结构**断言：它不点名某个键，所以下次有人新写一处同样的裸强制（跨行、
    包在 ``or`` / 条件表达式里都算）会被当场抓住，而不是等它打死某条链路。
    """

    def test_no_adapter_init_wraps_config_get_in_bare_int_or_float(self):
        offenders: list[str] = []
        for path in sorted(ADAPTER_DIR.glob("*.py")):
            for function_name, line_number, cast_name in bare_numeric_coercions(path):
                offenders.append(
                    f"{path.name}:{line_number}: {function_name} 里的裸 "
                    f"{cast_name}(...) 包住了 self.config.get(...)"
                )
        self.assertEqual(
            offenders, [],
            "适配器 __init__ 里的裸 int()/float() 必须走 "
            "opencode_bridge.config_coerce（否则配置写错会打死整个桥）：\n  "
            + "\n  ".join(offenders),
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
        parsed = ast.parse(sample)
        found = [
            inner.func.id
            for node in ast.walk(parsed)
            if isinstance(node, ast.FunctionDef)
            for inner in ast.walk(node)
            if isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Name)
            and inner.func.id in ("int", "float")
            and any(_reads_config(argument) for argument in inner.args)
        ]
        self.assertEqual(found, ["int"])


if __name__ == "__main__":  # pragma: no cover - 手动跑这一个文件用
    unittest.main()

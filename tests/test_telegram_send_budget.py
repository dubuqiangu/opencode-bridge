"""telegram 每聊天发送预算的纯算术守门（``telegram_send_budget.py``）。

⛔ **不 import 适配器**：本文件只断言算术本身，不涉及 ``_api`` / ``_throttle``。
所有时刻都是**固定整数**，⛔ 不读墙钟 ⇒ 判据不受机器负载影响
（一旦混进 ``time.monotonic()``，「裁剪后长度才是依据」这类断言就会时红时绿）。

⚠️ 每条用例的注释都写明「什么情况下它会红」—— 说不出红法的断言就是恒真的。
"""

from __future__ import annotations

import math
import unittest

from opencode_bridge.adapters.telegram_send_budget import (
    DEFAULT_SEND_LIMIT_PER_MINUTE,
    MAX_RATE_LIMIT_WAIT_SECONDS,
    MAX_SEND_ATTEMPTS,
    SEND_LIMIT_MAX_PER_MINUTE,
    SEND_LIMIT_MIN_PER_MINUTE,
    SEND_WINDOW_SECONDS,
    clamp_send_limit,
    prune_send_timestamps,
    rate_limit_wait_seconds,
    record_send,
    seconds_until_send_slot,
)


class TheWindowBudgetTests(unittest.TestCase):
    """窗口预算：裁剪后长度才是依据，等待恰好到最早一条滑出窗口。"""

    def test_fifteen_sends_in_the_window_make_the_sixteenth_wait(self):
        """窗口内满 15 条 ⇒ 第 16 条必须等。

        红的情形：``seconds_until_send_slot`` 恒返回 0.0（预算压根没生效），
        或预算比较写成 ``<= limit``（第 16 条被放行）。
        """
        self.assertEqual(DEFAULT_SEND_LIMIT_PER_MINUTE, 15)  # 常量也要钉
        wait = seconds_until_send_slot([0.0] * 15, now=10.0, limit=15)
        self.assertGreater(wait, 0.0)

    def test_fifteen_sends_in_the_window_wait_exactly_until_the_oldest_expires(self):
        """精确值：15 条都发在 t=0、now=10、window=60 ⇒ wait == 50.0。

        红的情形：算式写成 ``window - now``（漏了最早一条的时刻），
        或写成 ``now - oldest``（方向反了 ⇒ 得 10.0）。用整数时刻断言。
        """
        wait = seconds_until_send_slot(
            [0.0] * 15, now=10.0, limit=15, window_seconds=60.0
        )
        self.assertEqual(wait, 60.0 - (10.0 - 0.0))
        self.assertEqual(wait, 50.0)

    def test_fourteen_sends_in_the_window_let_the_fifteenth_go_now(self):
        """14 条 < 15 ⇒ 不等待（0.0）。

        红的情形：差一错误使未满也判"等"（``len < limit - 1`` 才放行）
        ⇒ 正常节奏被无谓拖慢一个窗口。
        """
        self.assertEqual(
            seconds_until_send_slot([0.0] * 14, now=10.0, limit=15), 0.0
        )

    def test_records_outside_the_window_are_pruned_before_counting(self):
        """15 条全在窗口外 ⇒ 0.0（钉住「裁剪后长度才是依据」）。

        红的情形：先判 ``len(timestamps) < limit`` 再裁剪 ⇒ 窗口一满就永久
        卡死（哪怕那 15 条都是一小时前发的）。
        """
        self.assertEqual(
            seconds_until_send_slot(
                [-120.0] * 15, now=0.0, limit=15, window_seconds=60.0
            ),
            0.0,
        )

    def test_pruning_returns_a_new_list_and_leaves_the_input_untouched(self):
        """``prune`` / ``record_send`` 返回新列表、不改入参。

        ``now=60``、``window=60`` ⇒ 边界是 **0.0**（不是 1.0）。
        红的情形：实现里写了 ``timestamps.pop()`` / ``.append()`` 原地改 ⇒
        调用方持有的历史被悄悄砍掉，而预算看起来仍然正确。
        """
        timestamps = [0.0, 50.0, 59.0]
        snapshot = list(timestamps)
        pruned = prune_send_timestamps(timestamps, now=60.0, window_seconds=60.0)
        self.assertEqual(pruned, [50.0, 59.0])   # 0.0 正好在边界 ⇒ 过期
        self.assertEqual(timestamps, snapshot)
        recorded = record_send(timestamps, now=60.0, window_seconds=60.0)
        self.assertEqual(recorded, [50.0, 59.0, 60.0])
        self.assertIsNot(recorded, timestamps)
        self.assertEqual(timestamps, snapshot)

    def test_a_record_exactly_on_the_window_edge_counts_as_expired(self):
        """``now - at == window`` ⇒ 不算在用（边界是开区间）。

        红的情形：裁剪条件写成 ``at >= now - window`` ⇒ 边界那条留着，
        满预算时白等整整一个窗口（60s）。
        """
        self.assertEqual(
            prune_send_timestamps([0.0], now=60.0, window_seconds=60.0), []
        )
        self.assertEqual(
            seconds_until_send_slot([0.0] + [1.0] * 14, now=60.0, limit=15),
            0.0,
        )

    def test_zero_or_negative_limit_never_blocks(self):
        """``limit`` 为 0 / 负数 ⇒ 0.0，且⛔ 不抛（配置事故的防御路径）。

        红的情形：实现用负索引 ``recent[-limit]`` 或除零 ⇒ 抛异常打死适配器；
        或反向写成"没有预算 ⇒ 永远等"⇒ 挂死发送线程。
        """
        for degenerate_limit in (0, -1, -30):
            with self.subTest(limit=degenerate_limit):
                self.assertEqual(
                    seconds_until_send_slot(
                        [0.0] * 99, now=0.5, limit=degenerate_limit
                    ),
                    0.0,
                )


class TheSendLimitClampTests(unittest.TestCase):
    """配置上限的夹逼：数值夹到 1..30，非数值回落 15。"""

    def test_non_integer_values_fall_back_to_the_default(self):
        """``None`` / 字符串 / 浮点 / 布尔 ⇒ 默认 15，⛔ 不抛。

        红的情形：把 ``True`` 当 1（``bool`` 是 ``int`` 子类）⇒ 悄悄把预算
        砍到 1 条/分钟；或让 ``int()`` 的异常逃出去 ⇒ ``base.build`` 把整个
        适配器包成 ``AdapterError``，用户只看到「没有可用适配器」。
        """
        for non_integer in (None, "15", 15.0, True, False, [15], {"value": 15}):
            with self.subTest(value=non_integer):
                self.assertEqual(
                    clamp_send_limit(non_integer), DEFAULT_SEND_LIMIT_PER_MINUTE
                )

    def test_out_of_range_integers_are_clamped_to_the_documented_bounds(self):
        """0 / 负数夹到 1，超过 30 夹到 30；先钉常量本身。

        红的情形：只做下界、漏上界 ⇒ 配置写 999 就等于没有预算；
        或把 0 当"关掉预算"⇒ 与 ``seconds_until_send_slot`` 的 0 = 不阻塞
        语义撞车（那是**纯函数**的防御分支，⛔ 不是配置面可达的值）。
        """
        self.assertEqual(SEND_LIMIT_MIN_PER_MINUTE, 1)
        self.assertEqual(SEND_LIMIT_MAX_PER_MINUTE, 30)
        for configured, expected in (
            (0, 1),
            (-5, 1),
            (1, 1),
            (30, 30),
            (31, 30),
            (999, 30),
        ):
            with self.subTest(configured=configured):
                self.assertEqual(clamp_send_limit(configured), expected)

    def test_in_range_integers_pass_through_unchanged(self):
        """1..30 之间的整数原样返回（夹逼不许改写合法值）。

        红的情形：``min`` / ``max`` 参数顺序写反 ⇒ 合法值被压成边界。
        """
        for configured in (1, 7, 15, 29, 30):
            with self.subTest(configured=configured):
                self.assertEqual(clamp_send_limit(configured), configured)


class TheRateLimitWaitTests(unittest.TestCase):
    """429 之后等多久：尝试次数与累计等待双上限。"""

    def test_the_fourth_failed_attempt_is_the_last(self):
        """第 1~3 次失败仍可重试，第 4 次失败 ⇒ ``None``。

        红的情形：判断写成 ``attempts_done > MAX_SEND_ATTEMPTS`` ⇒ 会发第 5 次；
        或写成 ``>= MAX_SEND_ATTEMPTS - 1`` ⇒ 只发 3 次。
        """
        self.assertEqual(MAX_SEND_ATTEMPTS, 4)  # 常量也要钉
        for attempts_done in (1, 2, 3):
            with self.subTest(attempts_done=attempts_done):
                self.assertEqual(
                    rate_limit_wait_seconds(
                        1.0, already_waited=0.0, attempts_done=attempts_done
                    ),
                    1.0,
                )
        self.assertIsNone(
            rate_limit_wait_seconds(1.0, already_waited=0.0, attempts_done=4)
        )

    def test_normal_retry_after_is_reported_verbatim(self):
        """合法的 ``retry_after`` 原样返回（不提前、不四舍五入）。

        红的情形：实现顺手做 ``min(..., 1.0)`` 或取整 ⇒ 平台要求的等待被缩短，
        下一次调用必然再撞 429。
        """
        self.assertEqual(
            rate_limit_wait_seconds(5.5, already_waited=0.0, attempts_done=1), 5.5
        )

    def test_negative_or_non_finite_retry_after_means_retry_immediately(self):
        """``None`` / 负数 / NaN / inf ⇒ 0.0（仍重试，不傻等）。

        红的情形：把非有限值直接返回 ⇒ ``_stop_event.wait(nan)`` 行为未定义；
        或把 ``None`` 当"不要再试"⇒ 一个没有 ``retry_after`` 的 429 直接失败。
        """
        for malformed in (None, -3, float("nan"), float("inf"), float("-inf")):
            with self.subTest(retry_after=malformed):
                self.assertEqual(
                    rate_limit_wait_seconds(
                        malformed, already_waited=0.0, attempts_done=1
                    ),
                    0.0,
                )
        self.assertTrue(math.isnan(float("nan")))  # 防止本组被写成恒真的哑断言

    def test_boolean_and_string_retry_after_mean_retry_immediately(self):
        """``True`` / ``"5"`` ⇒ 0.0（与 ``_retry_after`` 的拒绝语义一致）。

        红的情形：``isinstance(True, int)`` 为真 ⇒ 把布尔当 1 秒睡下去，
        与 ``_retry_after`` 已经拒绝它的判据自相矛盾。
        """
        for malformed in (True, False, "5", [5]):
            with self.subTest(retry_after=malformed):
                self.assertEqual(
                    rate_limit_wait_seconds(
                        malformed, already_waited=0.0, attempts_done=1
                    ),
                    0.0,
                )

    def test_cumulative_wait_is_capped(self):
        """累计等待超过 60s ⇒ ``None``；正好 60s 仍允许。

        红的情形：只按单次 ``retry_after`` 判 ⇒ 3 次各 40s 的 429 会把一次
        关停或一条回复拖到 120s，而上游的上限是 60s。
        """
        self.assertEqual(MAX_RATE_LIMIT_WAIT_SECONDS, 60.0)  # 常量也要钉
        self.assertIsNone(
            rate_limit_wait_seconds(20.0, already_waited=50.0, attempts_done=1)
        )
        self.assertEqual(
            rate_limit_wait_seconds(20.0, already_waited=40.0, attempts_done=1),
            20.0,
        )
        self.assertEqual(
            rate_limit_wait_seconds(0.0, already_waited=60.0, attempts_done=1),
            0.0,
        )


class TheWindowConstantTests(unittest.TestCase):
    """窗口长度是平台预算，⛔ 不可配（写了也只认 60s）。"""

    def test_the_window_is_sixty_seconds(self):
        """钉住 60.0：改大等于放宽平台预算，改小等于自废吞吐。

        红的情形：有人把它改成 ``30.0`` 去"更安全"⇒ 每分钟上限实际翻倍。
        """
        self.assertEqual(SEND_WINDOW_SECONDS, 60.0)


if __name__ == "__main__":
    unittest.main()

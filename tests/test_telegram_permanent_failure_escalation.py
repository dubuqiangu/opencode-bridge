import unittest

from opencode_bridge.adapters.telegram_protocol import (
    CREDENTIAL_PERMANENT_FAILURE_COOLDOWN,
    CREDENTIAL_PROBE_PERMANENT_FAILURE_THRESHOLD,
    PERMANENT_FAILURE_LOG_MARKER,
)

from tests.test_telegram_credential_gate import (
    FakePlatform,
    fast_ladder,
    make_adapter,
    record_requested_waits,
    rejected_getme_response,
    wait_until,
)

LADDER_INITIAL_SECONDS = 0.02
LADDER_CEILING_SECONDS = 0.02
COOLDOWN_SECONDS = 2.0  # the clamp lower bound; the ladder tops out at 0.02
#: The clamp lower bound is deliberately 2.0s (user-decided), NOT the 60s default:
#: a tiny cooldown stays configurable so tests/ops can observe the fixed-cooldown
#: branch quickly. The ladder is capped at 0.02s, so 2.0 vs 0.02 tells the branches apart.


class ThresholdTests(unittest.TestCase):
    def test_threshold_is_five(self):
        self.assertEqual(CREDENTIAL_PROBE_PERMANENT_FAILURE_THRESHOLD, 5)

    def test_cooldown_is_sixty(self):
        self.assertEqual(CREDENTIAL_PERMANENT_FAILURE_COOLDOWN, 60.0)

    def test_marker_is_non_empty(self):
        self.assertTrue(PERMANENT_FAILURE_LOG_MARKER)


class FixedCooldownAfterThresholdTests(unittest.TestCase):
    """Past the threshold, waits stop doubling and settle on the fixed cooldown."""

    def start_always_failing_adapter(self):
        adapter = make_adapter()
        fast_ladder(
            adapter,
            LADDER_INITIAL_SECONDS,
            LADDER_CEILING_SECONDS,
            COOLDOWN_SECONDS,
        )
        platform = FakePlatform(
            adapter, get_me=lambda attempt: rejected_getme_response()
        )
        ledger = record_requested_waits(adapter, platform)
        adapter.start()
        self.addCleanup(adapter.stop)
        return adapter, ledger, platform

    def test_waits_after_the_threshold_are_the_fixed_cooldown(self):
        adapter, ledger, platform = self.start_always_failing_adapter()

        target_attempts = CREDENTIAL_PROBE_PERMANENT_FAILURE_THRESHOLD + 3
        self.assertTrue(
            wait_until(lambda: len(platform.get_me_at) >= target_attempts, timeout=10.0),
            "前提：探测链路失败足够多次",
        )
        adapter.stop()

        post_threshold_waits = [
            seconds
            for seconds, attempts_so_far in ledger.requested_waits
            if attempts_so_far > CREDENTIAL_PROBE_PERMANENT_FAILURE_THRESHOLD
        ]
        self.assertTrue(post_threshold_waits, "阈值之后必须至少还等过一次")
        self.assertEqual(
            set(post_threshold_waits),
            {COOLDOWN_SECONDS},
            "阈值之后的等待必须是固定冷却，不能再按阶梯翻倍。实际："
            f"{post_threshold_waits!r}",
        )


if __name__ == "__main__":
    unittest.main()

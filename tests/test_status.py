"""T1.5 tests — structured channel status normalisation (pure functions, no I/O)."""

from __future__ import annotations

import json
import unittest
from dataclasses import FrozenInstanceError

from opencode_bridge.status import (
    LEGACY_TEXT_STATES,
    ChannelState,
    ChannelStatus,
    channel_state,
    normalize_platform_status,
    render_table,
    summarize,
)
from opencode_bridge.subscription_status_view import (
    NO_LIVE_SNAPSHOT_TEXT,
    NO_SNAPSHOT_MISSING_ERROR_TEXT,
    RECONNECTING_CLAUSE,
    STREAMING_CLAUSE,
    SUBSCRIPTION_DISPLAY_NOT_STARTED,
    SUBSCRIPTION_DISPLAY_RECOVERING,
    SUBSCRIPTION_DISPLAY_STOPPED_BY_REQUEST,
    SUBSCRIPTION_DISPLAY_STREAMING,
    SUBSCRIPTION_DISPLAY_TERMINATED,
    SUBSCRIPTION_DISPLAY_UNRECOGNISED,
    TERMINATED_CLAUSE,
    render_subscription_status,
    subscription_display_state,
)
from opencode_bridge.subscription_supervisor import (
    PHASE_ENDED_WITHOUT_STOP,
    PHASE_IDLE,
    PHASE_RECONNECTING,
    PHASE_STOPPED_BY_REQUEST,
    PHASE_STREAMING,
    SubscriptionStatus,
)


class TestStateSemantics(unittest.TestCase):
    """``usable`` / ``healthy`` semantics for all five states."""

    def test_state_wire_values(self):
        self.assertEqual(
            [s.value for s in ChannelState],
            ["connecting", "connected", "degraded", "error", "disabled"],
        )

    def test_state_is_a_str(self):
        # ChannelState subclasses str: it survives json.dumps untouched and
        # compares equal to its wire value.
        self.assertEqual(ChannelState.CONNECTED, "connected")
        self.assertEqual(json.dumps({"s": ChannelState.ERROR}), '{"s": "error"}')

    def test_usable_only_for_connected_and_degraded(self):
        usable = {s: ChannelStatus("x", "X", s).usable for s in ChannelState}
        self.assertEqual(
            usable,
            {
                ChannelState.CONNECTING: False,
                ChannelState.CONNECTED: True,
                ChannelState.DEGRADED: True,
                ChannelState.ERROR: False,
                ChannelState.DISABLED: False,
            },
        )

    def test_healthy_only_for_connected(self):
        healthy = {s: ChannelStatus("x", "X", s).healthy for s in ChannelState}
        self.assertTrue(healthy[ChannelState.CONNECTED])
        self.assertFalse(healthy[ChannelState.DEGRADED])
        self.assertEqual(sum(healthy.values()), 1)

    def test_degraded_still_usable(self):
        status = ChannelStatus("slack", "Slack", ChannelState.DEGRADED, "只能发不能收")
        self.assertTrue(status.usable)
        self.assertFalse(status.healthy)

    def test_label_defaults_from_platform(self):
        self.assertEqual(ChannelStatus("Telegram", "", ChannelState.CONNECTED).label, "Telegram")
        self.assertEqual(ChannelStatus("matrix", "", ChannelState.CONNECTED).label, "Matrix")
        # Unknown key falls back to the raw platform, not to an exception.
        self.assertEqual(ChannelStatus("weird_os", "", ChannelState.CONNECTED).label, "weird_os")

    def test_platform_is_lowercased_and_stripped(self):
        status = ChannelStatus("  Telegram ", "Telegram", ChannelState.CONNECTED)
        self.assertEqual(status.platform, "telegram")

    def test_status_is_frozen(self):
        status = ChannelStatus("slack", "Slack", ChannelState.CONNECTED)
        with self.assertRaises(FrozenInstanceError):
            status.state = ChannelState.ERROR  # type: ignore[misc]

    def test_is_receiving_is_declared_not_inferred(self):
        # No guessing: the caller states inbound capability explicitly.
        self.assertTrue(ChannelStatus("slack", "Slack", ChannelState.CONNECTED).is_receiving)
        self.assertFalse(
            ChannelStatus("slack", "Slack", ChannelState.CONNECTED, receiving=False).is_receiving
        )
        # Even a connected send-only channel reports no inbound.
        send_only = ChannelStatus(
            "discord", "Discord", ChannelState.DEGRADED, "send only", receiving=False
        )
        self.assertFalse(send_only.is_receiving)
        self.assertTrue(send_only.usable)

    def test_detail_never_changes_semantics(self):
        # "已断开" inside detail must NOT flip a healthy channel to error —
        # this is the whole point of dropping the dsh-im-gateway regex.
        status = ChannelStatus(
            "telegram", "Telegram", ChannelState.CONNECTED, "上次已断开，已自动恢复"
        )
        self.assertTrue(status.usable)
        self.assertTrue(status.healthy)


class TestChannelStateParsing(unittest.TestCase):
    def test_parses_enum_and_wire_value(self):
        self.assertIs(channel_state(ChannelState.ERROR), ChannelState.ERROR)
        self.assertIs(channel_state("degraded"), ChannelState.DEGRADED)
        self.assertIs(channel_state("  CONNECTED "), ChannelState.CONNECTED)

    def test_unknown_string_falls_back_to_disabled(self):
        self.assertIs(channel_state("half-connected"), ChannelState.DISABLED)
        self.assertIs(channel_state(""), ChannelState.DISABLED)
        self.assertIs(channel_state("   "), ChannelState.DISABLED)

    def test_non_string_inputs_fall_back_without_raising(self):
        for value in (None, 42, 3.5, True, object(), ["connected"], {"state": "error"}):
            with self.subTest(value=value):
                self.assertIs(channel_state(value), ChannelState.DISABLED)


class TestNormalizePlatformStatus(unittest.TestCase):
    def test_state_wins_over_legacy_text(self):
        status = normalize_platform_status(
            "slack", "Slack", state=ChannelState.ERROR, legacy_text="已连接"
        )
        self.assertIs(status.state, ChannelState.ERROR)

    def test_unrecognised_state_falls_back_to_disabled(self):
        status = normalize_platform_status("irc", "IRC", state="sort-of-up")
        self.assertIs(status.state, ChannelState.DISABLED)

    def test_legacy_text_maps_known_words(self):
        cases = {
            "已连接": ChannelState.CONNECTED,
            "连接中": ChannelState.CONNECTING,
            "异常": ChannelState.ERROR,
            "未连接": ChannelState.DISABLED,
            "连接失败": ChannelState.ERROR,
            "二维码已过期": ChannelState.ERROR,
            "已停用": ChannelState.DISABLED,
            "connected": ChannelState.CONNECTED,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                status = normalize_platform_status("telegram", "Telegram", legacy_text=text)
                self.assertIs(status.state, expected)
                self.assertEqual(status.detail, "")

    def test_legacy_text_unknown_keeps_original_in_detail(self):
        status = normalize_platform_status("twitch", "Twitch", legacy_text="出了点问题")
        self.assertIs(status.state, ChannelState.DISABLED)
        self.assertFalse(status.usable)
        self.assertEqual(status.detail, "出了点问题")

    def test_legacy_text_is_not_substring_matched(self):
        # The failure mode we are avoiding: a keyword buried in prose used to
        # flip the verdict.  Whole-string lookup means these stay DISABLED.
        for text in ("已连接，但是收发有点慢", "刚才重连中过", "错误率 0%"):
            with self.subTest(text=text):
                status = normalize_platform_status("slack", "Slack", legacy_text=text)
                self.assertIs(status.state, ChannelState.DISABLED)
                self.assertEqual(status.detail, text)

    def test_legacy_text_whitespace_and_case_normalised(self):
        self.assertIs(
            normalize_platform_status("s", "S", legacy_text="  已连接  ").state,
            ChannelState.CONNECTED,
        )
        self.assertIs(normalize_platform_status("s", "S", legacy_text="CONNECTED").state,
                      ChannelState.CONNECTED)

    def test_explicit_detail_beats_legacy_text_in_detail(self):
        status = normalize_platform_status(
            "irc", "IRC", detail="见日志", legacy_text="看不懂的文本"
        )
        self.assertEqual(status.detail, "见日志")

    def test_no_state_no_legacy_is_disabled(self):
        self.assertIs(normalize_platform_status("matrix", "Matrix").state, ChannelState.DISABLED)

    def test_empty_legacy_text_is_disabled(self):
        status = normalize_platform_status("matrix", "Matrix", legacy_text="")
        self.assertIs(status.state, ChannelState.DISABLED)
        self.assertEqual(status.detail, "")

    def test_receiving_is_forwarded(self):
        self.assertFalse(
            normalize_platform_status("discord", "Discord", state="connected", receiving=False)
            .is_receiving
        )

    def test_table_lookup_is_a_frozen_constant(self):
        self.assertIn("已连接", LEGACY_TEXT_STATES)
        self.assertIs(LEGACY_TEXT_STATES["已连接"], ChannelState.CONNECTED)


class TestJsonRoundTrip(unittest.TestCase):
    def test_every_state_survives_dict_round_trip(self):
        for state in ChannelState:
            with self.subTest(state=state):
                original = ChannelStatus("slack", "Slack", state, "细节", receiving=False)
                restored = ChannelStatus.from_dict(original.to_dict())
                self.assertEqual(restored, original)
                self.assertIs(restored.state, state)

    def test_every_state_survives_json_round_trip(self):
        for state in ChannelState:
            with self.subTest(state=state):
                original = ChannelStatus("telegram", "Telegram", state, "轮询中")
                restored = ChannelStatus.from_json(original.to_json())
                self.assertEqual(restored, original)

    def test_json_payload_is_ascii_safe_and_flat(self):
        payload = json.loads(ChannelStatus("irc", "IRC", ChannelState.ERROR, "被踢了").to_json())
        self.assertEqual(
            payload,
            {
                "platform": "irc",
                "label": "IRC",
                "state": "error",
                "detail": "被踢了",
                "receiving": True,
            },
        )

    def test_illegal_state_string_degrades_to_disabled(self):
        restored = ChannelStatus.from_dict(
            {"platform": "slack", "label": "Slack", "state": "quantum"}
        )
        self.assertIs(restored.state, ChannelState.DISABLED)
        self.assertEqual(restored.label, "Slack")

    def test_missing_fields_get_defaults(self):
        restored = ChannelStatus.from_dict({"platform": "matrix"})
        self.assertIs(restored.state, ChannelState.DISABLED)
        self.assertEqual(restored.label, "Matrix")
        self.assertEqual(restored.detail, "")
        self.assertTrue(restored.is_receiving)

    def test_non_mapping_payload_raises(self):
        with self.assertRaises(TypeError):
            ChannelStatus.from_dict(["not", "a", "mapping"])  # type: ignore[arg-type]

    def test_malformed_json_raises_value_error(self):
        with self.assertRaises(ValueError):
            ChannelStatus.from_json("{not json")


class TestSummarize(unittest.TestCase):
    def test_empty_list(self):
        self.assertEqual(
            summarize([]),
            {
                "total": 0,
                "connected": 0,
                "usable": 0,
                "degraded": 0,
                "error": 0,
                "disabled": 0,
                "healthy": False,
            },
        )

    def test_all_disabled_is_not_healthy(self):
        result = summarize([ChannelState.DISABLED] * 3)
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["disabled"], 3)
        self.assertEqual(result["usable"], 0)
        self.assertFalse(result["healthy"])

    def test_connected_is_healthy(self):
        result = summarize([ChannelState.CONNECTED, ChannelState.DISABLED])
        self.assertEqual(result["connected"], 1)
        self.assertEqual(result["usable"], 1)
        self.assertTrue(result["healthy"])

    def test_error_anywhere_makes_it_unhealthy(self):
        result = summarize([ChannelState.CONNECTED, ChannelState.ERROR, ChannelState.CONNECTED])
        self.assertEqual(result["error"], 1)
        self.assertEqual(result["connected"], 2)
        self.assertEqual(result["usable"], 2)
        self.assertFalse(result["healthy"])

    def test_degraded_counts_as_usable_and_stays_healthy(self):
        result = summarize([ChannelState.DEGRADED, ChannelState.CONNECTED])
        self.assertEqual(result["degraded"], 1)
        self.assertEqual(result["connected"], 1)
        self.assertEqual(result["usable"], 2)  # connected + degraded
        self.assertTrue(result["healthy"])

    def test_connecting_is_counted_in_total_only(self):
        result = summarize([ChannelState.CONNECTING, ChannelState.CONNECTING])
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["usable"], 0)
        self.assertFalse(result["healthy"])

    def test_accepts_statuses_strings_and_mixed(self):
        # Anything channel_state() understands may be summarised directly.
        result = summarize(["connected", ChannelState.DEGRADED, "bogus"])
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["usable"], 2)
        self.assertEqual(result["disabled"], 1)
        self.assertTrue(result["healthy"])

    def test_generator_input_is_consumed_once(self):
        result = summarize(ChannelState for _ in range(2))
        self.assertEqual(result["total"], 2)


class TestRenderTable(unittest.TestCase):
    def setUp(self):
        self.statuses = [
            normalize_platform_status("telegram", "Telegram", state=ChannelState.CONNECTED),
            normalize_platform_status(
                "slack", "Slack", state=ChannelState.DEGRADED, detail="只能发不能收",
                receiving=False,
            ),
            normalize_platform_status("discord", "Discord", state=ChannelState.CONNECTING),
            normalize_platform_status(
                "matrix", "Matrix", state=ChannelState.ERROR, detail="token 被撤销"
            ),
            normalize_platform_status("irc", "IRC", state=ChannelState.DISABLED),
        ]
        self.table = render_table(self.statuses)

    def test_every_label_and_state_marker_present(self):
        for status in self.statuses:
            with self.subTest(platform=status.platform):
                self.assertIn(status.label, self.table)
                self.assertIn(status.mark, self.table)
                self.assertIn(status.state_label, self.table)

    def test_all_five_markers_present(self):
        for mark in ("\u2705", "\U0001f504", "\u26a0\ufe0f", "\u2716", "\u23f8"):
            self.assertIn(mark, self.table)

    def test_details_rendered_and_blank_becomes_dash(self):
        self.assertIn("只能发不能收", self.table)
        self.assertIn("token 被撤销", self.table)
        self.assertTrue(any(line.rstrip().endswith("-") for line in self.table.splitlines()))

    def test_inbound_column_reflects_declared_capability(self):
        rows = [line for line in self.table.splitlines() if "Slack" in line]
        self.assertEqual(len(rows), 1)
        self.assertIn("否", rows[0])
        telegram = [line for line in self.table.splitlines() if "Telegram" in line]
        self.assertIn("是", telegram[0])

    def test_header_present(self):
        self.assertIn("渠道", self.table)
        self.assertIn("状态", self.table)
        self.assertIn("入站", self.table)
        self.assertIn("说明", self.table)

    def test_all_lines_share_one_display_width(self):
        from opencode_bridge.status import _display_width

        widths = {_display_width(line) for line in self.table.splitlines()}
        self.assertEqual(len(widths), 1, f"misaligned widths: {sorted(widths)}")

    def test_long_platform_label_widens_first_column(self):
        wide = render_table(
            [
                normalize_platform_status(
                    "nextcloud-talk", "", state=ChannelState.CONNECTED
                ),
                normalize_platform_status("irc", "IRC", state=ChannelState.DISABLED),
            ]
        )
        self.assertIn("Nextcloud Talk", wide)
        from opencode_bridge.status import _display_width

        widths = {_display_width(line) for line in wide.splitlines()}
        self.assertEqual(len(widths), 1, f"misaligned widths: {sorted(widths)}")

    def test_empty_input_still_renders_header_and_rule(self):
        table = render_table([])
        lines = table.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("渠道", lines[0])
        self.assertTrue(set(lines[1]) == {"-"})


def a_subscription_snapshot(**changes) -> SubscriptionStatus:
    """一份「已经跑过一阵子」的订阅快照（只改用例真正关心的那几个字段）。

    ⚠️ 刻意把 ``last_error`` 设成**非空**：它证明判别**没有**拿 ``last_error`` 当信号 ——
    「上一次为什么坏」是档案，而「此刻怎么样」是 :attr:`phase`。⇒ 于是这一份快照
    在 ``streaming`` 与 ``reconnecting`` 两相下有**同样**的 ``last_error``，只靠档案
    是分不出来的。
    """
    fields = {
        "phase": PHASE_STREAMING,
        "subscriptions_started": 9,
        "reconnect_attempts": 7,
        "frames_received": 3,
        "last_error": "OpenCodeError: event stream 503",
    }
    fields.update(changes)
    return SubscriptionStatus(**fields)


class TestSubscriptionDisplayStateIsAClosedMapping(unittest.TestCase):
    """五个 phase → 五档显示，外加一档「认不出来」的兜底。"""

    def test_each_known_phase_maps_to_its_own_display_state(self):
        """⚠️ 会红的条件：任何一相被归到**另一相**的档位 —— 特别是两个坏相
        （``reconnecting`` / ``ended_without_stop``）塌进同一档 ⇒ 用户就无从分辨
        「在自愈」与「已经死了」。
        """
        self.assertEqual(
            {
                phase: subscription_display_state(a_subscription_snapshot(phase=phase))
                for phase in (
                    PHASE_IDLE,
                    PHASE_STREAMING,
                    PHASE_RECONNECTING,
                    PHASE_STOPPED_BY_REQUEST,
                    PHASE_ENDED_WITHOUT_STOP,
                )
            },
            {
                PHASE_IDLE: SUBSCRIPTION_DISPLAY_NOT_STARTED,
                PHASE_STREAMING: SUBSCRIPTION_DISPLAY_STREAMING,
                PHASE_RECONNECTING: SUBSCRIPTION_DISPLAY_RECOVERING,
                PHASE_STOPPED_BY_REQUEST: SUBSCRIPTION_DISPLAY_STOPPED_BY_REQUEST,
                PHASE_ENDED_WITHOUT_STOP: SUBSCRIPTION_DISPLAY_TERMINATED,
            },
        )

    def test_an_unrecognised_phase_lands_in_the_fallback_not_in_healthy(self):
        """⚠️ 「读不懂」必须有自己的档位。

        ⛔ 它落进 :data:`SUBSCRIPTION_DISPLAY_STREAMING` 就是本视图存在的理由本身
        （把「不知道」显示成「正常」）。会红的条件：兜底那一支被删掉 / 默认值改成
        ``SUBSCRIPTION_DISPLAY_STREAMING``。
        """
        self.assertEqual(
            subscription_display_state(a_subscription_snapshot(phase="half-open")),
            SUBSCRIPTION_DISPLAY_UNRECOGNISED,
        )


class TestTheTwoBadPhasesAreToldApart(unittest.TestCase):
    """⭐ 本任务的正身：**「正在重连」与「线程已经死了」在视图里必须可区分。**

    这两条用例各钉一个方向 ⇒ 只钉一个方向等于没钉（缺陷的形状正是这两者原本
    分不开）。每条都断言**三句**：本相那一句在、另两相那两句都不在。
    """

    def test_a_reconnecting_subscription_never_reads_as_normal_or_as_dead(self):
        body = "\n".join(
            render_subscription_status(
                a_subscription_snapshot(
                    phase=PHASE_RECONNECTING, reconnect_attempts=7
                )
            )
        )
        self.assertIn(RECONNECTING_CLAUSE, body)
        self.assertNotIn(
            STREAMING_CLAUSE, body,
            "「正在重连」被显示成「订阅正常」⇒ 用户以为答复还在回来，"
            "而此刻这条线程**不在收任何事件**",
        )
        self.assertNotIn(
            TERMINATED_CLAUSE, body,
            "「正在重连」被显示成「线程已经死了」⇒ 用户会去重启一个"
            "**自己正在恢复**的桥",
        )

    def test_a_subscription_that_ended_on_its_own_never_reads_as_normal(self):
        """⭐ 反向证明的靶心：**线程已死而视图说正常。**

        会红的条件：这一相被归到 ``streaming`` / ``recovering``（两个都试过）。
        """
        body = "\n".join(
            render_subscription_status(
                a_subscription_snapshot(phase=PHASE_ENDED_WITHOUT_STOP)
            )
        )
        self.assertIn(TERMINATED_CLAUSE, body)
        self.assertNotIn(
            STREAMING_CLAUSE, body,
            "线程已经死了而视图说「订阅正常」—— 这正是本条要消灭的静默失效",
        )
        self.assertNotIn(
            RECONNECTING_CLAUSE, body,
            "线程已经死了而视图说「正在重连」⇒ 它不会自己回来，"
            "说成自愈中就是在骗用户（两种坏必须分开）",
        )

    def test_the_two_phases_render_different_wording(self):
        """⚠️ 不只「一档 ≠ 另一档」：**逐字不同**才是用户看得见的那个区别。

        会红的条件：两相共用一句措辞（于是映射分开、界面却分不开）。
        """
        recovering = "\n".join(
            render_subscription_status(
                a_subscription_snapshot(phase=PHASE_RECONNECTING)
            )
        )
        ended = "\n".join(
            render_subscription_status(
                a_subscription_snapshot(phase=PHASE_ENDED_WITHOUT_STOP)
            )
        )
        self.assertNotEqual(recovering, ended)


class TestSubscriptionSectionNeverFakesHealth(unittest.TestCase):
    """其余几相 + 「拿不到快照」：⛔ 都不许显示成「正常」。"""

    def test_no_live_snapshot_says_so_instead_of_saying_normal(self):
        body = "\n".join(render_subscription_status(None))
        self.assertIn(NO_LIVE_SNAPSHOT_TEXT, body)
        self.assertNotIn(
            STREAMING_CLAUSE, body,
            "「读不到」被说成「订阅正常」⇒ 那正是本段要消灭的那类假话",
        )
        # ⛔ 也不能编一个「订阅 0 次 / 收到 0 帧」出来（伪造观测，AGENTS.md §8）。
        self.assertNotIn("累计", body)

    def test_an_unrecognised_phase_is_not_worded_as_healthy(self):
        """会红的条件：兜底那一支被删掉，或默认落到
        :data:`SUBSCRIPTION_DISPLAY_STREAMING` ⇒ 「读不懂」显示成「订阅正常」。
        """
        body = "\n".join(
            render_subscription_status(a_subscription_snapshot(phase="half-open"))
        )
        self.assertNotIn(STREAMING_CLAUSE, body)
        self.assertIn("认不出", body)

    def test_the_stopped_path_is_not_worded_as_a_failure(self):
        """⛔ 反方向也要钉：**正常关机不许被说成需要人管**（否则用户去重启一个
        刚停好的桥）。会红的条件：这一相被归到 ``terminated`` / ``recovering``。
        """
        body = "\n".join(
            render_subscription_status(
                a_subscription_snapshot(phase=PHASE_STOPPED_BY_REQUEST)
            )
        )
        self.assertNotIn(TERMINATED_CLAUSE, body)
        self.assertNotIn(RECONNECTING_CLAUSE, body)
        self.assertNotIn(STREAMING_CLAUSE, body)

    def test_a_streaming_subscription_is_the_only_phase_worded_as_normal(self):
        """把「只有哪一相**可以**说正常」钉住：⭐ 上面那两条反证才是有意义的
        （否则一个「对所有相都说正常」的实现也能过它们）。

        会红的条件：给 ``idle`` / 兜底档也加上「正常」那句。
        """
        streaming = "\n".join(
            render_subscription_status(a_subscription_snapshot(phase=PHASE_STREAMING))
        )
        self.assertIn(STREAMING_CLAUSE, streaming)
        for phase in (PHASE_IDLE, "half-open"):
            with self.subTest(phase=phase):
                self.assertNotIn(
                    STREAMING_CLAUSE,
                    "\n".join(
                        render_subscription_status(
                            a_subscription_snapshot(phase=phase)
                        )
                    ),
                )

    def test_the_last_failure_is_labelled_as_an_archive_not_as_now(self):
        """⚠️ 一次**早已恢复**的失败不许被读成「此刻还坏着」—— 那与把死线程读成
        正常是同一个错误的镜像（AGENTS.md §8：没有记录就分不出「还在失败」与
        「没人再发消息」）。会红的条件：去掉那句限定词。
        """
        body = "\n".join(
            render_subscription_status(a_subscription_snapshot(phase=PHASE_STREAMING))
        )
        self.assertIn("OpenCodeError: event stream 503", body)
        self.assertIn("上一次", body)

    def test_a_snapshot_without_any_failure_is_not_worded_as_never_failed(self):
        """⚠️ ``last_error`` 为空时**不许**说「没失败过」—— 分不出「没失败过」
        与「这次没记下来」。会红的条件：那一支改成「无失败」。
        """
        body = "\n".join(
            render_subscription_status(
                a_subscription_snapshot(phase=PHASE_IDLE, last_error=None)
            )
        )
        self.assertIn(NO_SNAPSHOT_MISSING_ERROR_TEXT, body)

    def test_the_section_states_that_the_capability_table_covers_something_else(self):
        """⚠️ 钉住**为什么**另起一段：能力表**刻意**不看这条线程（那是它的职责，
        ⛔ 不许改）。会红的条件：那句解释被删（于是读者会以为「表全绿 = 流健康」）。
        """
        body = "\n".join(render_subscription_status(None))
        self.assertIn("能力与健康是两件事", body)


if __name__ == "__main__":
    unittest.main()
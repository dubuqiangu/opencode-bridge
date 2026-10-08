"""Telegram 拍板一：token 在**运行期**被吊销（401）⇒ 闸门重新关上 + 有界重探 getMe ⇒ 自愈。

被测的缺陷（2026-10-08 用户拍板要修）
------------------------------------
改之前凭据闸门**验过一次就永不再探**：``_credentials_verified`` 为真时
:meth:`~opencode_bridge.adapters.telegram.TelegramAdapter._credential_gate`
直接返回 ⇒ token 在启动**之后**被吊销时，``getUpdates`` 连续回 401，每一轮都
只是「抛异常 → 传输层恒定 2.0s 退避 → 再抛」，``getMe`` **一次都不打** ⇒ 用户
把 token 换回来（改 ``config.json`` 不重启桥）也**不会自愈**，且
``--status`` 的启动结论档还写着「正常」——**说它是好的，而它坏了**。

拍板的处置
----------
① ``_poll_round`` 观测到 **401**（且只有 401，按语义判）⇒
   ``_invalidate_credentials``：运行期失效键置位、入站不放行、落盘一份
   **与启动结论分开的**记录（:mod:`opencode_bridge.credential_health`）。
② 闸门按「这是不是运行期失效」**分流**：运行段重探 ``getMe`` **有次数上界**
   （:data:`RUNTIME_CREDENTIAL_REVERIFY_LIMIT` = 5，探满 ⇒ 响亮地说
   「需要重启桥」并**静默**），⛔ 不许与启动段那个永不放弃的阶梯合成一条循环。

本文件钉住的判据（每条都要能说出「什么情况下它会红」）
----------------------------------------------------
* **计数断言对负载免疫**：上界 = ``getMe`` 的**调用次数**（5 次、再多一次都不许），
  ⛔ 不拿计时当判据（AGENTS.md §7.1）。
* **阶梯断言只认「请求值」**：等待 park 在 ``_stop_event.wait()`` 上，断言
  **请求了哪些秒数**（纯函数输出），实测量在负载下会被调度放大。
* **常量自钉**：断言「至多 5 次」必须同时断言 :data:`RUNTIME_CREDENTIAL_REVERIFY_LIMIT`
  本身的值就是 5 —— 否则那条计数断言是拿实现当期望（恒真家族）。

⚠️ 本文件**不联网**：全部用替换 ``_post`` 的办法（与
``tests/test_telegram_credential_gate.py`` 同一套 harness 惯例）。
"""

from __future__ import annotations

import logging
import tempfile
import threading
import unittest

# 期望的 warning 不刷屏；``assertLogs`` 自己换 handler，不受影响。
logging.getLogger("opencode_bridge").addHandler(logging.NullHandler())

from opencode_bridge import credential_health, health
from opencode_bridge.adapters.telegram import (
    RUNTIME_CREDENTIAL_REVERIFY_LIMIT,
    UNAUTHORIZED_ERROR_CODE,
    TelegramAdapter,
)
from opencode_bridge.hooks import Inbound
from opencode_bridge.transport import ReconnectNow

#: 一个**形状上就不是**真 token 的占位值（与 ``test_telegram_credential_gate.py``
#: 同一个理由：这里不需要真形状，也就不需要 §2.4 的拼接）。
FAKE_BOT_TOKEN = "123456789:not-a-real-token-abcdefghij"


def unauthorized_get_updates(offset: object) -> dict:
    """``getUpdates`` 回 401 —— Telegram 对「token 不被承认」的机器可读形式。"""
    return {"ok": False, "error_code": 401, "description": "Unauthorized"}


def revoked_get_me(attempt: int) -> dict:
    """``getMe`` 仍 401 —— token 还没被换回来的那一臂。"""
    return {"ok": False, "error_code": 401, "description": "Unauthorized"}


def verified_get_me(attempt: int) -> dict:
    """``getMe`` 通过 —— token 已被换回来的那一臂。"""
    return {"ok": True, "result": {"id": 1}}


class RecordingHooks:
    """只记 inbound 的最小 ``Hooks`` 实现（本文件不触发出站）。"""

    def __init__(self) -> None:
        self.inbounds: list[Inbound] = []

    def on_inbound(self, inbound: Inbound) -> None:
        self.inbounds.append(inbound)

    def on_callback(self, conversation_id: str, data: str, query_id: str) -> None:
        return None


class FakePlatform:
    """把 ``_post`` 换成可编排的假平台，并记下**每一次** ``getMe`` 的次数。

    ⚠️ ``get_me`` 收到的是**第几次**调用（从 1 起）—— 测试可以闭包改
    ``self._get_me`` 来编排「先坏、后好」的时间线。
    """

    def __init__(self, adapter, *, get_me=None, get_updates=None) -> None:
        self._get_me = get_me or verified_get_me
        self._get_updates = get_updates or (lambda offset: {"ok": True, "result": []})
        self.get_me_count = 0
        adapter._post = self.post

    def post(self, method, payload=None, *, timeout=None):
        if method == "getMe":
            self.get_me_count += 1
            return self._get_me(self.get_me_count)
        if method == "getUpdates":
            return self._get_updates((payload or {}).get("offset"))
        return {"ok": True, "result": {}}


class RequestedWaitsEvent(threading.Event):
    """记下 ``wait()`` 每次**被请求**的秒数，⛔ 不实测等待。

    与 ``test_telegram_credential_gate.py`` 的 ``RecordingStopEvent`` 同一条纪律：
    时序断言只认**请求值**（它是 :meth:`_credential_probe_backoff_for` 这个纯
    函数的输出），实测量在负载下会被调度放大（那里实测过一次 ``wait(0.2)``
    回来是 1.25s）。本类**不真等**：本文件测「运行段请求了什么阶梯」，不测
    「等没等到」；``stop()`` 的可打断性由 ``TestRuntimeBoundAndLadder`` 里那条
    用**真实** ``_stop_event`` 的用例单独钉。
    """

    def __init__(self) -> None:
        super().__init__()
        self.requested: list[float] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.requested.append(timeout)
        return self.is_set()


def make_adapter() -> TelegramAdapter:
    adapter = TelegramAdapter(
        {"bot_token": FAKE_BOT_TOKEN}, RecordingHooks()
    )
    adapter.min_interval = 0            # 测试里不要人为 sleep
    return adapter


def fast_ladder(adapter: TelegramAdapter, initial: float, ceiling: float) -> None:
    """把凭据阶梯缩到测试能等得起的尺度（**只**缩这一个旋钮）。"""
    adapter.credential_probe_initial_backoff = initial
    adapter.credential_probe_max_backoff = ceiling


class RuntimeCredentialTestCase(unittest.TestCase):
    """共享 fixture：每个用例一套临时 ``bridge_dir`` + 已装配的落盘记录器。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.bridge_dir = directory.name
        credential_health.install_credential_failure_recorder(
            credential_health.CredentialFailureRecorder(self.bridge_dir)
        )
        self.addCleanup(credential_health.install_credential_failure_recorder, None)

    def disk_entry_for(self, adapter: TelegramAdapter) -> dict:
        record = credential_health.read_credential_failures(self.bridge_dir)
        self.assertIsNotNone(record, "运行期凭据失效应该已经落盘")
        platforms = record["platforms"]
        key = health.platform_key(adapter)
        self.assertIn(key, platforms, "落盘条目必须用 --status 同源的平台键")
        return platforms[key]


class TestRuntimeInvalidation(RuntimeCredentialTestCase):

    def test_a_401_from_getupdates_closes_the_gate_and_tells_the_user_how_to_fix(self):
        """⭐ 拍板一主判据：401 ⇒ 闸门关上 + ERROR 响亮地说清修法。"""
        adapter = make_adapter()
        adapter._credentials_verified = True     # 生产里 401 只发生在「验过之后」
        FakePlatform(adapter, get_updates=unauthorized_get_updates)

        with self.assertLogs(
            "opencode_bridge.adapters.telegram", level="ERROR"
        ) as captured:
            with self.assertRaises(RuntimeError):
                adapter._poll_round()

        self.assertTrue(adapter._runtime_credentials_invalid)
        self.assertFalse(adapter._credentials_verified)
        joined = "\n".join(captured.output)
        self.assertIn("运行期凭据失效", joined)
        self.assertIn("code=401", joined)
        self.assertIn("bot_token", joined)       # 修法必须点名那个键
        # ⭐ 哨兵：改之前这行是 ``code=%s code=%s`` 重复打码 —— 半成品阶段抓出来的
        self.assertNotIn("code=401 code=401", joined)
        # 落盘那条与启动结论是两份记录（拍板一里「区分」的写入侧）
        entry = self.disk_entry_for(adapter)
        self.assertEqual(entry["code"], 401)

    def test_only_401_marks_the_runtime_gate_other_codes_do_not(self):
        """⛔ 只认 401（按语义判）：429 / 5xx / 传输错误**不**惊动凭据闸门。

        403 是"bot 被拉黑 / 无权访问该聊天"、404 是"方法或聊天不存在"——
        都不是「这个 token 不被承认」（理由见 ``UNAUTHORIZED_ERROR_CODE`` 的
        注释）。让一次网络抖动去关凭据闸门，等于把「重连」降级成「自断」。
        """
        for code in (403, 429, 500, 0, None):
            with self.subTest(error_code=code):
                adapter = make_adapter()
                adapter._credentials_verified = True
                payload = {"ok": False, "description": "boom"}
                if code is not None:
                    payload["error_code"] = code
                FakePlatform(
                    adapter,
                    get_updates=lambda offset, payload=payload: payload,
                )
                with self.assertRaises(RuntimeError):
                    adapter._poll_round()
                self.assertFalse(adapter._runtime_credentials_invalid)
                self.assertTrue(adapter._credentials_verified)
                self.assertIsNone(
                    credential_health.read_credential_failures(self.bridge_dir),
                    "非 401 的失败不许写凭据失效记录",
                )

    def test_repeated_401s_invalidate_once_not_once_per_round(self):
        """失效态里重复的 401：日志一行、落盘一次（⛔ 每轮 401 都是每轮一行=刷屏）。"""
        adapter = make_adapter()
        adapter._credentials_verified = True
        FakePlatform(adapter, get_updates=unauthorized_get_updates)

        with self.assertLogs(
            "opencode_bridge.adapters.telegram", level="ERROR"
        ) as captured:
            for _round in range(2):
                with self.assertRaises(RuntimeError):
                    adapter._poll_round()

        invalidation_lines = [
            line for line in captured.output if "运行期凭据失效" in line
        ]
        self.assertEqual(len(invalidation_lines), 1)
        entry = self.disk_entry_for(adapter)
        self.assertNotIn("recovered_at", entry)


class TestRuntimeSelfHeal(RuntimeCredentialTestCase):

    def _invalidate_then_heal(self) -> TelegramAdapter:
        """先 401 关闸，再把 token 换回来（getMe 转好），走闸门自愈。"""
        adapter = make_adapter()
        adapter._credentials_verified = True
        platform = FakePlatform(
            adapter,
            get_updates=unauthorized_get_updates,
            get_me=revoked_get_me,
        )
        with self.assertRaises(RuntimeError):
            adapter._poll_round()
        self.assertTrue(adapter._runtime_credentials_invalid)
        platform._get_me = verified_get_me      # ← 用户改好了 bot_token
        with self.assertLogs(
            "opencode_bridge.adapters.telegram", level="WARNING"
        ) as captured:
            adapter._credential_gate()            # 传输层重连后的那次 on_open
        return adapter, platform, captured

    def test_gate_reprobes_getme_and_selfheals_after_the_token_comes_back(self):
        """⭐ 拍板一的核心：改配置不重启 ⇒ 下一次重连就自愈，且说得响亮。"""
        adapter, platform, captured = self._invalidate_then_heal()

        self.assertFalse(adapter._runtime_credentials_invalid)
        self.assertTrue(adapter._credentials_verified)
        self.assertEqual(platform.get_me_count, 1)   # 重探真的打了 getMe
        joined = "\n".join(captured.output)
        self.assertIn("运行期凭据恢复", joined)
        self.assertIn("第 1 次通过", joined)
        # 落盘那条盖上恢复戳（--status 读的就是这份）
        self.assertIn("recovered_at", self.disk_entry_for(adapter))

    def test_runtime_recovery_does_not_flush_history_again(self):
        """运行期自愈**不**丢积压历史：offset 没动，服务端会重发，⇒ 不调 flush。

        ⚠️ 这条与启动段**刻意不同**（启动段的恢复会
        :meth:`_flush_history_once` —— 那是「别把断线期间的旧消息灌给 agent」；
        而运行期失效期间 offset 一直没推进，消息**该**重投）。顺手把 flush 搬进
        运行段 = 每次自愈都吃掉失效期间的消息，**而且不报错**。
        """
        adapter, _platform, _captured = self._invalidate_then_heal()
        self.assertFalse(adapter._history_flushed)

    def test_runtime_failure_and_recovery_never_touch_the_startup_verdict(self):
        """⛔ 运行期的事实走**另一个键**：``startup_verdict`` 一个字节都不许动。"""
        adapter = make_adapter()
        adapter._credentials_verified = True
        verdict_before = adapter.startup_verdict
        FakePlatform(
            adapter,
            get_updates=unauthorized_get_updates,
            get_me=revoked_get_me,
        )
        with self.assertRaises(RuntimeError):
            adapter._poll_round()
        adapter._credential_gate()            # 探满失败（初值 0 ⇒ 不真等）
        self.assertEqual(adapter.startup_verdict, verdict_before)


class TestRuntimeBoundAndLadder(RuntimeCredentialTestCase):

    def test_the_constants_are_pinned_to_their_promise(self):
        """⭐ 常量自钉：断言「至多 5 次」的用例拿的是实现值当期望 —— 这条把它钉死。

        （恒真断言家族的既有判法：断言「钳到某个常量」时，必须再有一条断言
        常量本身的值。）
        """
        self.assertEqual(RUNTIME_CREDENTIAL_REVERIFY_LIMIT, 5)
        self.assertEqual(UNAUTHORIZED_ERROR_CODE, 401)

    def test_reverify_is_strictly_bounded_then_says_restart_bridge_and_goes_quiet(self):
        """⭐ 上界主判据：探满恰 5 次 ⇒ 响亮说「需要重启桥」⇒ 之后**静默**。

        计数断言（``== 5``、再探一次都算红）对机器负载免疫；上界成立与否
        **不靠计时**。
        """
        adapter = make_adapter()
        adapter._credentials_verified = True
        platform = FakePlatform(
            adapter,
            get_updates=unauthorized_get_updates,
            get_me=revoked_get_me,
        )
        fast_ladder(adapter, 0.0, 0.0)        # 阶梯缩到 0：不真等（⛔ 不用计时判据）
        with self.assertRaises(RuntimeError):
            adapter._poll_round()

        with self.assertLogs(
            "opencode_bridge.adapters.telegram", level="ERROR"
        ) as captured:
            adapter._credential_gate()
        self.assertEqual(platform.get_me_count, RUNTIME_CREDENTIAL_REVERIFY_LIMIT)
        self.assertIn("需要重启桥", "\n".join(captured.output))
        # ⛔ 放弃之后不许偷偷恢复：两个键都留在失效侧
        self.assertTrue(adapter._runtime_credentials_invalid)
        self.assertFalse(adapter._credentials_verified)

        # 再来一次会话：探满之后必须**静默** —— getMe 总数一个都不许再涨
        quiet_get_me_count = platform.get_me_count
        adapter._credential_gate()
        self.assertEqual(platform.get_me_count, quiet_get_me_count)

    def test_runtime_waits_reuse_the_shared_ladder_with_the_same_off_by_one(self):
        """运行段**复用**启动段的阶梯（初值→×2→封顶）且第 5 次尝试**不多等一档**。

        断言「请求值」序列 ``[2, 4, 8, 16]``：4 次等待对应 5 次尝试 ——
        最后一档是 break 出去的，⛔ 不许「探满之前还先睡一觉」。
        """
        adapter = make_adapter()
        adapter._credentials_verified = True
        FakePlatform(
            adapter,
            get_updates=unauthorized_get_updates,
            get_me=revoked_get_me,
        )
        fast_ladder(adapter, 2.0, 60.0)
        ledger = RequestedWaitsEvent()
        adapter._stop_event = ledger

        with self.assertRaises(RuntimeError):
            adapter._poll_round()
        adapter._credential_gate()

        self.assertEqual(ledger.requested, [2.0, 4.0, 8.0, 16.0])

    def test_stop_interrupts_the_runtime_reverify_wait(self):
        """阶梯 park 在真实 ``_stop_event.wait()`` 上 ⇒ ``stop()`` 立刻可打断。

        用**真实** ``Event`` 且**预置位**：``wait(30.0)`` 当场返回 True ⇒
        :class:`ReconnectNow` 抛出，⛔ 全程零计时（负载免疫）。
        """
        adapter = make_adapter()
        adapter._credentials_verified = True
        FakePlatform(
            adapter,
            get_updates=unauthorized_get_updates,
            get_me=revoked_get_me,
        )
        fast_ladder(adapter, 30.0, 30.0)
        with self.assertRaises(RuntimeError):
            adapter._poll_round()

        adapter._stop_event.set()             # stop() 已请求
        with self.assertRaises(ReconnectNow):
            adapter._credential_gate()


if __name__ == "__main__":
    unittest.main()

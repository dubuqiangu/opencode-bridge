"""Telegram ``getMe`` 结论的**上报**与 ``poll_timeout`` 的解析兜底。

两个缺陷，都属于"用户零线索"那一类：

1. **结论没有通道到用户** —— ``TelegramAdapter.start()`` 里 ``getMe`` 失败只写一行
   日志然后 ``return``，而 ``--status`` / ``--setup --json`` 只检查 token 字符串
   非空 ⇒ **token 打错 / 被吊销 / 网络被墙时状态视图仍显示「已配置 / 入站就绪」**。
   现在每个失败分支都通过
   :meth:`~opencode_bridge.adapters.base.Adapter.report_startup_probe` 上报，
   由 :mod:`opencode_bridge.health` 落盘、两个视图读出。
2. **一个旋钮写错会打死整条关键路径** —— ``poll_timeout`` 填非整数时
   ``int(...)`` 抛 ``ValueError`` ⇒ ``build()`` 把它包成 ``AdapterError`` ⇒ 适配器
   被跳过 ⇒ 整个桥 ``usable == 0``，而用户看到的报错是「没有任何可用适配器」，
   **一个字都不提 ``poll_timeout``**。现在解析失败回落默认值并告警。

⚠️ 本文件**不联网**：全部用替换 ``_post`` 的办法。
"""

from __future__ import annotations

import logging
import unittest

# 期望的 warning 不刷屏；``assertLogs`` 自己换 handler，不受影响。
logging.getLogger("opencode_bridge").addHandler(logging.NullHandler())

from opencode_bridge.adapters import build
from opencode_bridge.adapters.telegram import POLL_LONG_TIMEOUT, TelegramAdapter
from opencode_bridge.health import VERDICT_FAILED, VERDICT_OK, VERDICT_SKIPPED
from opencode_bridge.hooks import Inbound, MsgHandle, Outbound

#: 一个**形状上就不是**真 token 的占位值（真形状的夹具见
#: ``tests/test_platform_health.py`` —— 那里要验脱敏，那里才需要真形状）。
FAKE_BOT_TOKEN = "123456789:not-a-real-token-abcdefghij"


class _RecordingHooks:
    def on_inbound(self, inbound: Inbound) -> None:
        return None

    def on_callback(self, conversation_id: str, data: str, query_id: str) -> None:
        return None


def make_adapter(config: dict | None = None) -> TelegramAdapter:
    settings = {"bot_token": FAKE_BOT_TOKEN}
    if config:
        settings.update(config)
    adapter = TelegramAdapter(settings, _RecordingHooks())
    adapter.min_interval = 0
    return adapter


def stub_post(adapter: TelegramAdapter, get_me):
    """把 ``_post`` 换成只对 ``getMe`` 有反应的实现（``getUpdates`` 回空批）。"""

    def fake_post(method, payload=None, *, timeout=None):
        if method == "getMe":
            return get_me()
        return {"ok": True, "result": []}

    adapter._post = fake_post


class TestGetMeVerdictIsReported(unittest.TestCase):
    """① / ② / ③ / ⑤：``getMe`` 的每一个结局都要留下可上报的结论。"""

    def test_rejected_token_is_reported_as_failed_with_the_platform_error_code(self):
        """① 平台明确拒绝（``ok:false`` + ``error_code``）⇒ ``failed`` + 那个码。

        ``code`` 必须是 **Telegram 的** ``error_code``：状态视图里显示的
        「code=401」得能让人一眼看出"token 过期/被吊销"，而不是我们自己的编号。
        """
        adapter = make_adapter()
        stub_post(adapter, lambda: {
            "ok": False, "error_code": 401, "description": "Unauthorized",
        })
        adapter.start()
        self.addCleanup(adapter.stop)

        self.assertEqual(adapter.startup_verdict["verdict"], VERDICT_FAILED)
        self.assertEqual(adapter.startup_verdict["code"], 401)
        self.assertIn("Unauthorized", adapter.startup_verdict["detail"])
        # ⚠️ **下面两条断言是随设计一起改的，不是放松**（§8：断言表达的是"不变量"，
        # 不变量变了断言就必须跟着变 —— 悄悄留着旧断言才是假绿）。
        # 旧断言是 ``assertIsNone(adapter.transport, "getMe 被拒时不得启动轮询")``。
        # 它守的不变量是「**凭据没过就不许放行入站**」—— 那条不变量**一个字没变**，
        # 但它**不再**由"传输层是 None"来表达了：
        # 改之前 ``getMe`` 一失败就 ``return`` ⇒ ``self._transport`` 恒 ``None``
        # ⇒ **入站 100% 死掉**、而生产里没有任何重试入口（``adapter.start()``
        # 只有 ``core.BridgeCore.start`` 一处调用点，且被 ``_started`` 守着）。
        # ⇒ 现在传输层**必须**起来（它是重试循环唯一的落脚点，⛔ 不新增线程），
        # 而"不许放行"由 :attr:`TelegramAdapter.running` 表达 —— 线程活着但停在
        # 凭据闸门里 ⇒ 一条 update 都不会分发 ⇒ 报成 True 就是假话。
        self.assertFalse(
            adapter.running, "凭据没过就不许报成在跑（入站一条都收不到）"
        )
        self.assertIsNotNone(
            adapter.transport, "闸门必须跑在传输线程里，否则入站永远无法自愈"
        )

    def test_valid_token_is_reported_as_ok(self):
        """② ``getMe`` 通过 ⇒ ``ok``（**没有**这条，"正常"就永远不会被记下来）。"""
        adapter = make_adapter()
        stub_post(adapter, lambda: {"ok": True, "result": {"id": 1}})
        adapter.start()
        self.addCleanup(adapter.stop)

        self.assertEqual(adapter.startup_verdict["verdict"], VERDICT_OK)
        self.assertIsNotNone(adapter.transport, "探测通过就该真的开始轮询")

    def test_transport_failure_reported_by_post_itself_carries_code_zero(self):
        """③a ``_post`` 把传输异常包成 ``{"ok": False, "error_code": 0, ...}``
        ⇒ ``failed`` + ``code == 0``。

        ``0`` 是本仓库"**没拿到 HTTP 状态码**"的既有约定（``_post`` 自己这么写、
        ``classify_http`` 对 ``status <= 0`` 也这么判），所以这里沿用同一个值 ——
        "网络被墙"这种最常见的失败恰恰不走平台错误码那条分支。
        """
        adapter = make_adapter()
        stub_post(adapter, lambda: {
            "ok": False,
            "error_code": 0,
            "description": "transport error: <urlopen error timed out>",
        })
        adapter.start()
        self.addCleanup(adapter.stop)

        self.assertEqual(adapter.startup_verdict["verdict"], VERDICT_FAILED)
        self.assertEqual(adapter.startup_verdict["code"], 0)
        self.assertIn("transport error", adapter.startup_verdict["detail"])

    def test_raising_post_is_reported_as_failed_with_code_zero(self):
        """③b ``_post`` **抛**出来（被替换的实现 / 未预料的异常）⇒ 同样 ``failed``。

        ⛔ 这条分支过去连一行"失败"都没留下（异常被接住直接 ``return``），于是
        "启动时炸了"这种最需要被看见的失败在状态视图里完全不存在。
        """
        adapter = make_adapter()

        def exploding_post(method, payload=None, *, timeout=None):
            raise RuntimeError("连接被重置")

        adapter._post = exploding_post
        adapter.start()
        self.addCleanup(adapter.stop)

        self.assertEqual(adapter.startup_verdict["verdict"], VERDICT_FAILED)
        self.assertEqual(adapter.startup_verdict["code"], 0)
        self.assertIn("连接被重置", adapter.startup_verdict["detail"])

    def test_missing_bot_token_is_reported_as_skipped_never_as_ok(self):
        """连 token 都没有 ⇒ ``skipped``，而**不是** ``ok``。

        "没验"与"验过了"必须分开：把它记成 ``ok`` 等于对着没验过的东西说没问题。
        """
        adapter = make_adapter({"bot_token": ""})
        stub_post(adapter, lambda: {"ok": True, "result": {}})
        adapter.start()
        self.addCleanup(adapter.stop)

        self.assertEqual(adapter.startup_verdict["verdict"], VERDICT_SKIPPED)
        self.assertIsNone(adapter.transport)

    def test_the_documented_getme_failure_log_line_is_still_emitted_verbatim(self):
        """⛔ ``telegram: getMe failed (code=…): …; adapter not started`` 原文保留。

        ``docs/install.md`` 与 ``plugin/README.md`` 都让用户**按这一行字**排障；
        基类那条规范化日志是**新增**的，不许取代它 —— 取代了等于让已写的排障指引失效。
        """
        adapter = make_adapter()
        stub_post(adapter, lambda: {
            "ok": False, "error_code": 401, "description": "Unauthorized",
        })
        with self.assertLogs("opencode_bridge.adapters.telegram",
                             level="WARNING") as captured:
            adapter.start()
        self.addCleanup(adapter.stop)

        documented = (
            "telegram: getMe failed (code=401): Unauthorized; adapter not started"
        )
        # ``assertLogs`` 的每行带 ``LEVEL:logger:`` 前缀，所以按"整行包含"判。
        self.assertTrue(
            any(documented in line for line in captured.output),
            f"文档引用的那一行必须逐字还在。实际：{captured.output!r}",
        )

    def test_the_reported_detail_never_carries_a_token(self):
        """⛔ 上报的 ``detail`` 一律脱敏 —— 平台回的 ``description`` 是自由文本。

        ⛔ 真形状的夹具按 AGENTS.md §2.4 **拼接**而成（完整形状的字面量会让
        整个 push 被推送保护拦下）。
        """
        from opencode_bridge import redaction

        echo = ("123456789" + ":" + ("Ab" * 17) + "Z")
        self.assertIn(
            "[REDACTED:telegram-bot-token]",
            redaction.default_redactor().scrub("bad token: " + echo),
            "夹具必须真的被脱敏引擎认出来，否则这条断言恒真",
        )
        adapter = make_adapter()
        stub_post(adapter, lambda: {
            "ok": False, "error_code": 401, "description": "Bad token: " + echo,
        })
        adapter.start()
        self.addCleanup(adapter.stop)

        self.assertNotIn(echo, adapter.startup_verdict["detail"])


class TestPollTimeoutIsNotFatal(unittest.TestCase):
    """⑤b：一个旋钮写错**不许**打死整个桥。"""

    def test_non_integer_poll_timeout_still_builds_and_falls_back(self):
        """⭐ 填 ``"25s"``：适配器**构造成功**、回落默认值、并**说清是哪个键**。

        改之前这里抛 ``ValueError`` ⇒ ``build()`` 包成 ``AdapterError`` ⇒ 适配器被
        跳过 ⇒ 整个桥 ``usable == 0``。所以断言的是 ``build()`` 不抛 —— 那才是用户
        真正遇到的那个后果。
        """
        with self.assertLogs("opencode_bridge.adapters.telegram",
                             level="WARNING") as captured:
            adapter = build(
                "telegram",
                {"bot_token": FAKE_BOT_TOKEN, "poll_timeout": "25s"},
                _RecordingHooks(),
            )
        self.assertIsInstance(adapter, TelegramAdapter)
        self.assertEqual(adapter.poll_long_timeout, POLL_LONG_TIMEOUT)
        warnings = [line for line in captured.output if "poll_timeout" in line]
        self.assertEqual(len(warnings), 1, f"该有一条 warning。实际：{captured.output!r}")
        # 告警必须**说清收到的值与回落到多少** —— 只说"配置非法"等于让用户自己找。
        self.assertIn("'25s'", warnings[0])
        self.assertIn(str(POLL_LONG_TIMEOUT), warnings[0])

    def test_non_positive_poll_timeout_falls_back_with_a_warning(self):
        adapter = make_adapter({"poll_timeout": 0})
        with self.assertLogs("opencode_bridge.adapters.telegram",
                             level="WARNING") as captured:
            adapter = TelegramAdapter(
                {"bot_token": FAKE_BOT_TOKEN, "poll_timeout": 0}, _RecordingHooks()
            )
        self.assertEqual(adapter.poll_long_timeout, POLL_LONG_TIMEOUT)
        self.assertTrue(any("poll_timeout" in line for line in captured.output))

    def test_documented_integer_forms_still_take_effect(self):
        """防回归：合法取值**照旧生效**（回落只发生在解析失败时）。"""
        self.assertEqual(
            make_adapter({"poll_timeout": 8}).poll_long_timeout, 8,
        )
        self.assertEqual(
            make_adapter({"poll_timeout": "30"}).poll_long_timeout, 30,
        )
        self.assertEqual(make_adapter({}).poll_long_timeout, POLL_LONG_TIMEOUT)

    def test_absent_poll_timeout_is_silent(self):
        """没配就**不许**告警 —— 否则每次启动都刷一条没意义的警告，真正的配置
        错误反而看不见了（与 :func:`opencode_bridge.adapters.a2a._coerce_positive`
        同一条纪律）。"""
        logging.getLogger("opencode_bridge.adapters.telegram").addHandler(
            logging.NullHandler()
        )
        with self.assertNoLogs("opencode_bridge.adapters.telegram", level="WARNING"):
            make_adapter({})


class TestAdapterContractDefaults(unittest.TestCase):
    """基类上的声明式契约：不许被某个子类悄悄改掉。"""

    def test_startup_verdict_defaults_to_none_which_means_nothing_was_probed(self):
        """⛔ 默认 ``None`` 的含义是"没探测"，**不是**"没问题"。"""
        adapter = make_adapter()
        stub_post(adapter, lambda: {"ok": True, "result": {}})
        self.assertIsNone(adapter.startup_verdict)
        adapter.start()
        self.addCleanup(adapter.stop)
        self.assertEqual(adapter.startup_verdict["verdict"], VERDICT_OK)

    def test_report_startup_probe_normalizes_and_stores_the_entry(self):
        """上报方法返回/存下的都是**规范化**后的形态（键与取值域只有一份）。"""
        adapter = make_adapter()
        entry = adapter.report_startup_probe(
            "不是三档里的任何一档", code=None, detail="x" * 500
        )
        self.assertEqual(entry["verdict"], VERDICT_FAILED)
        self.assertNotIn("code", entry, "没有平台错误码时该键必须**省略**，不许写 0")
        self.assertLessEqual(len(entry["detail"]), 301)
        self.assertEqual(adapter.startup_verdict, entry)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
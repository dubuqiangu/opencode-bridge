"""``--status`` 各段**独立渲染**的护栏：一段坏掉不许把整张视图变空。

## 为什么要有这个文件

``run_status``（``opencode_bridge/platform_status.py``）是**多段拼出来的一整页**：
§1 服务连通性、§2 渠道配置与能力、§3 授权键冲突、§4 上次启动探测、§5 上次出站失败、
§6 上次凭据失效、§7 事件流订阅、§8 bridge 运行态。

§3~§8 原来**没有任何本段 guard** —— 它们依赖各自的 reader 函数「碰巧不抛」，于是
**一段里的一个意外异常会把整页 ``--status`` 变成空/崩**（用户在网络坏、盘上文件坏、
或某个平台能力读取意外炸掉时，连别的段都看不到）。本文件给每一段各钉一条**隔离**：
把**那一段**的渲染路径打桩成抛异常，然后跑**真的** :func:`cli.run_status`，断言：

* 返回 **0**（一段坏掉不许把退出码带崩）；
* **恰好**出现一条 :data:`FAILURE_MARKER` 失败说明；
* **其余每一段的段头**都照常渲染。

再加一条**对照臂**：什么都不炸时**一条失败说明都没有**、八段段头全在。

## 判据纪律

* ⚠️ **必须真跑 ``cli.run_status``**（纯读盘、不联网那段路），⛔ 不测「打桩后函数被调了没」。
* ⚠️ §3 只在 ``conflicts`` 非空时才渲染 ⇒ :func:`_conflict_config` 造一个**真冲突**
  （空键 + 非空键），否则 §3 那条隔离断言会因为「那段压根没跑」而**空转**。
* ⚠️ 打桩目标是**各段自己的符号**（:func:`platform_status._print_allowlist_conflicts`
  等），⛔ 不给整段 ``print`` 打桩（那会误伤别的段）。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from opencode_bridge import __main__ as cli
from opencode_bridge import platform_status, subscription_status_view
from opencode_bridge.config import Config


#: 各段 guard 打印的那句失败说明里**唯一**可识别的片段。
#: ⛔ 打桩注入的异常文案里不得出现它，否则「恰好一条」的计数会失真。
FAILURE_MARKER = "本段渲染失败"

SERVICE_SECTION_HEADER = "== opencode 服务 =="
CHANNEL_SECTION_HEADER = "== 渠道配置与能力 =="
CONFLICT_SECTION_HEADER = "== ⚠ 授权键冲突"
STARTUP_SECTION_HEADER = "== 上次启动时的探测结论 =="
RUNTIME_SECTION_HEADER = "== bridge 运行态 =="

#: 八段的段头，按渲染顺序。取各自来源模块的常量，避免把措辞再抄一份。
ALL_SECTION_HEADERS = (
    SERVICE_SECTION_HEADER,
    CHANNEL_SECTION_HEADER,
    CONFLICT_SECTION_HEADER,
    STARTUP_SECTION_HEADER,
    cli._OUTBOUND_SECTION_HEADER,
    cli._CREDENTIAL_FAILURE_SECTION_HEADER,
    subscription_status_view.EVENT_SUBSCRIPTION_SECTION_HEADER,
    RUNTIME_SECTION_HEADER,
)


def _conflict_config() -> Config:
    """让 §3「授权键冲突」**真的**渲染出来。

    空 ``allowed_chat_ids`` + 非空 ``allowed_chats`` = 两个白名单键解析结果不同 ⇒
    :func:`_channel_config_rows` 的行里带上 ``allowlist_conflict``，§3 才会跑。
    （与 ``tests/test_allowlist_visibility.py`` 那条冲突用例同一份配置形状。）
    """
    return Config(adapters={"telegram": {
        "bot_token": "t", "allowed_chat_ids": [], "allowed_chats": [42],
    }})


class StatusSectionIsolationTests(unittest.TestCase):
    """照抄凭据失效段测试的 harness：临时目录 + ``OPENCODE_BRIDGE_CONFIG``。"""

    def setUp(self) -> None:
        self._previous = os.environ.get("OPENCODE_BRIDGE_CONFIG")
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.bridge_dir = self._directory.name
        config_path = os.path.join(self.bridge_dir, "config.json")
        with io.open(config_path, "w", encoding="utf-8") as handle:
            json.dump({"adapters": {}}, handle)
        os.environ["OPENCODE_BRIDGE_CONFIG"] = config_path
        self.addCleanup(self._restore_config_env)

    def _restore_config_env(self) -> None:
        if self._previous is None:
            os.environ.pop("OPENCODE_BRIDGE_CONFIG", None)
        else:
            os.environ["OPENCODE_BRIDGE_CONFIG"] = self._previous

    def render(self) -> tuple[int, str]:
        """真跑一次 ``--status``，返回 ``(退出码, 整页输出)``。"""
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            exit_code = cli.run_status(_conflict_config())
        return exit_code, buffer.getvalue()

    def assert_one_section_can_fail_without_blanking_the_rest(
        self, patch_target: str, failing_section_header: str
    ) -> None:
        injection = "注入的渲染异常：%s" % patch_target
        with mock.patch.object(
            platform_status, patch_target, side_effect=RuntimeError(injection)
        ):
            exit_code, rendered = self.render()
        self.assertEqual(
            exit_code, 0,
            "一段渲染失败不许把退出码带崩（%s）" % patch_target,
        )
        self.assertEqual(
            rendered.count(FAILURE_MARKER), 1,
            "应恰好出现一条失败说明（%s），实际输出：\n%s" % (patch_target, rendered),
        )
        # 其余每一段的段头都必须还在 —— 这正是「一段坏掉不带走别的段」的判据。
        for header in ALL_SECTION_HEADERS:
            if header == failing_section_header:
                continue
            self.assertIn(
                header, rendered,
                "另一段的段头被这段的失败带走了（%s）：%s" % (patch_target, header),
            )


class ConflictSectionIsolation(StatusSectionIsolationTests):
    """§3 授权键冲突。"""

    def test_a_failing_conflict_section_keeps_the_other_sections(self):
        # §3 只在 conflicts 非空时渲染 —— 先证明这份配置真的造出了冲突，
        # 否则下面那条隔离断言会因为「那段压根没跑」而空转。
        _exit_code, happy = self.render()
        self.assertIn(CONFLICT_SECTION_HEADER, happy, "配置没造出冲突，§3 根本没渲染")
        self.assert_one_section_can_fail_without_blanking_the_rest(
            "_print_allowlist_conflicts", CONFLICT_SECTION_HEADER
        )


class StartupProbeSectionIsolation(StatusSectionIsolationTests):
    """§4 上次启动时的探测结论。"""

    def test_a_failing_startup_probe_section_keeps_the_other_sections(self):
        self.assert_one_section_can_fail_without_blanking_the_rest(
            "_print_last_start_probes", STARTUP_SECTION_HEADER
        )


class OutboundFailureSectionIsolation(StatusSectionIsolationTests):
    """§5 上次出站失败。"""

    def test_a_failing_outbound_failure_section_keeps_the_other_sections(self):
        self.assert_one_section_can_fail_without_blanking_the_rest(
            "_print_last_outbound_failures", cli._OUTBOUND_SECTION_HEADER
        )


class CredentialFailureSectionIsolation(StatusSectionIsolationTests):
    """§6 上次凭据失效。"""

    def test_a_failing_credential_failure_section_keeps_the_other_sections(self):
        self.assert_one_section_can_fail_without_blanking_the_rest(
            "_print_last_runtime_credential_failures",
            cli._CREDENTIAL_FAILURE_SECTION_HEADER,
        )


class SubscriptionSectionIsolation(StatusSectionIsolationTests):
    """§7 事件流订阅。"""

    def test_a_failing_subscription_section_keeps_the_other_sections(self):
        self.assert_one_section_can_fail_without_blanking_the_rest(
            "_print_event_subscription",
            subscription_status_view.EVENT_SUBSCRIPTION_SECTION_HEADER,
        )


class RuntimeStateSectionIsolation(StatusSectionIsolationTests):
    """§8 bridge 运行态（文件大小/mtime 循环那一整块）。"""

    def test_a_failing_runtime_state_section_keeps_the_other_sections(self):
        self.assert_one_section_can_fail_without_blanking_the_rest(
            "_runtime_state", RUNTIME_SECTION_HEADER
        )


class ControlArmSaysNothingFailed(StatusSectionIsolationTests):
    """对照臂：什么都不炸 ⇒ 一条失败说明都没有，八段段头全在。"""

    def test_the_happy_path_reports_no_section_failure(self):
        exit_code, rendered = self.render()
        self.assertEqual(exit_code, 0)
        self.assertNotIn(
            FAILURE_MARKER, rendered,
            "没有打任何桩，却出现了失败说明：\n%s" % rendered,
        )
        for header in ALL_SECTION_HEADERS:
            self.assertIn(header, rendered, "正常路径少了段头：%s" % header)


if __name__ == "__main__":
    unittest.main()

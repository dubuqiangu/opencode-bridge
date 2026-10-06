"""CLI 视图的两个修复的回归护栏（2026-10-06 实测缺陷）。

①  ``--config`` 被加载路径尊重，却被 JSON 视图忽略
------------------------------------------------------------------
``python -m opencode_bridge --config <X> --setup --json`` 加载时**确实用了** ``--config``，
但报出来的 ``config_path`` 是**另一个文件**。

**根因不是「写错了一行」，而是「解析出来的路径从未被记录」** ——
``__main._config_file_in_use`` 于是**自己重写了第二遍搜索链**（只查 env 与 cwd）。
⇒ 修法是让 :meth:`Config.load` 把结果记在实例的 ``source_path`` 上。

②  ``capabilities()`` 从来没进过任何 CLI 输出
------------------------------------------------------------------
``homeassistant`` 默认**一个事件都不收**，``inbound_accepts_anything=False`` 才是那个
「配好了但收不到」的明确信号 —— 而它的 docstring 明写这个信号要能从 ``--setup --json``
读到，**实测任何命令都不输出它**。

⇒ 「凭据齐备」**不等于**「能收到东西」，而状态视图只报了前者。
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from opencode_bridge import __main__ as cli  # noqa: E402
from opencode_bridge.config import Config  # noqa: E402


def _write_config(directory: Path, payload: dict) -> str:
    path = directory / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


#: 一个**已配置**的 homeassistant：url + token 齐备，但**没配** entities/domains/accept_all
#: ⇒ 按它的设计，`inbound_accepts_anything` 必须是 ``False``。
#: 这正是「配好了但收不到」那个信号，也是本文件要证明能被读到的那个。
CONFIGURED_HA = {
    "config_version": 2,
    "adapters": {"homeassistant": {"url": "http://example.invalid", "token": "t"}},
}


class SourcePathIsRecordedTests(unittest.TestCase):
    """① 根修：解析出来的路径必须被记下来。"""

    def test_load_records_the_file_it_actually_read(self):
        with tempfile.TemporaryDirectory() as raw:
            path = _write_config(Path(raw), {"config_version": 2})
            cfg = Config.load(path)
            self.assertEqual(
                cfg.source_path,
                os.path.abspath(path),
                "Config.load 必须记下它实际加载的文件，否则消费者只能自己重算一遍搜索链",
            )

    def test_load_records_the_explicit_path_over_the_env_one(self):
        """`--config` 与环境变量同时存在时，**`--config` 赢** —— 顺序是 `load` 定的。"""
        with tempfile.TemporaryDirectory() as raw_env, tempfile.TemporaryDirectory() as raw_explicit:
            env_path = _write_config(Path(raw_env), {"log_level": "DEBUG"})
            chosen = _write_config(Path(raw_explicit), {"log_level": "INFO"})
            previous = os.environ.get("OPENCODE_BRIDGE_CONFIG")
            os.environ["OPENCODE_BRIDGE_CONFIG"] = env_path
            try:
                cfg = Config.load(chosen)
            finally:
                if previous is None:
                    os.environ.pop("OPENCODE_BRIDGE_CONFIG", None)
                else:
                    os.environ["OPENCODE_BRIDGE_CONFIG"] = previous
            self.assertEqual(cfg.log_level, "INFO", "本用例的前提：显式路径被真正加载了")
            self.assertEqual(
                cfg.source_path,
                os.path.abspath(chosen),
                "记下的必须是**实际加载的那个**，不是环境变量里那个",
            )

    def test_no_file_found_leaves_it_empty_rather_than_guessing(self):
        """一个文件都没加载成时 ``source_path`` 是空串 —— ⛔ 不许去猜一个路径。"""
        with tempfile.TemporaryDirectory() as raw:
            previous_cwd = os.getcwd()
            previous_env = os.environ.get("OPENCODE_BRIDGE_CONFIG")
            os.chdir(raw)
            os.environ.pop("OPENCODE_BRIDGE_CONFIG", None)
            try:
                cfg = Config.load(os.path.join(raw, "definitely-absent.json"))
            finally:
                os.chdir(previous_cwd)
                if previous_env is not None:
                    os.environ["OPENCODE_BRIDGE_CONFIG"] = previous_env
            self.assertEqual(cfg.source_path, "", "没加载到文件时 source_path 必须是空串")


class ConfigFileInUseTests(unittest.TestCase):
    """消费者必须读 ``source_path``，而不是自己再算一遍。"""

    def test_it_prefers_the_recorded_path_over_the_cwd_search(self):
        with tempfile.TemporaryDirectory() as raw:
            path = _write_config(Path(raw), {"config_version": 2})
            cfg = Config.load(path)
            self.assertEqual(
                cli._config_file_in_use(cfg),
                os.path.abspath(path),
                "⚠️ 这就是那个实测缺陷：它曾只查 env 与 cwd，于是报出另一个文件",
            )

    def test_can_still_be_called_without_a_config_object(self):
        """⛔ 参数必须可选 —— 既有调用方与既有测试都不该被迫改。"""
        self.assertIsInstance(cli._config_file_in_use(), str)


class PlatformCapabilitiesAreVisibleTests(unittest.TestCase):
    """② 适配器自己声明的「能不能收到东西」必须出现在状态视图里。"""

    def _status_for(self, payload: dict, key: str) -> dict:
        with tempfile.TemporaryDirectory() as raw:
            cfg = Config.load(_write_config(Path(raw), payload))
            for row in cli._platform_status(cfg):
                if row.get("key") == key:
                    return row
        self.fail("状态视图里找不到 " + key)

    def test_configured_platform_exposes_its_capabilities(self):
        row = self._status_for(CONFIGURED_HA, "homeassistant")
        caps = row.get("capabilities")
        self.assertIsInstance(
            caps, dict, "已配置的平台必须带 capabilities —— 那是适配器自己声明的运行判据"
        )
        self.assertIn(
            "inbound_accepts_anything",
            caps,
            "homeassistant 的 docstring 明写这个信号要从 --setup --json 读到；"
            "原来任何命令都不输出它",
        )

    def test_the_signal_actually_distinguishes_the_two_failure_modes(self):
        """⛔ 不是「字段存在」就算数 —— 它必须**真的**能区分两种「收不到」。"""
        filtered = self._status_for(CONFIGURED_HA, "homeassistant")
        accepting = self._status_for(
            {
                "config_version": 2,
                "adapters": {
                    "homeassistant": {
                        "url": "http://example.invalid",
                        "token": "t",
                        "accept_all": True,
                    }
                },
            },
            "homeassistant",
        )
        self.assertFalse(
            filtered["capabilities"]["inbound_accepts_anything"],
            "只配 url+token ⇒ 默认一个事件都不收 ⇒ 信号必须是 False",
        )
        self.assertTrue(
            accepting["capabilities"]["inbound_accepts_anything"],
            "显式 accept_all ⇒ 信号必须是 True",
        )
        self.assertTrue(
            filtered["inbound_ready"],
            "⚠️ 两个配置的 inbound_ready 都是 True —— 这正是「状态说就绪、实际不工作」"
            "那个缺陷的形状：凭据齐备被当成了能用。capabilities 是唯一的区分点。",
        )

    def test_unconfigured_platform_is_not_constructed(self):
        """未配置时不去构造适配器 —— 构造只会抛，而它掩盖的正是我们要暴露的那种情形。"""
        row = self._status_for({"config_version": 2, "adapters": {}}, "telegram")
        self.assertIsNone(
            row.get("capabilities"),
            "未配置的平台不该被构造出来（missing 已经说清了缺什么）",
        )
        self.assertEqual(row.get("missing"), ["bot_token"])


class SetupJsonCarriesItEndToEndTests(unittest.TestCase):
    """端到端：用户真敲的那条命令必须能看到那个字段。"""

    def test_setup_json_output_contains_the_field(self):
        with tempfile.TemporaryDirectory() as raw:
            cfg = Config.load(_write_config(Path(raw), CONFIGURED_HA))
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                exit_code = cli.run_setup(cfg, "", True)
            self.assertEqual(exit_code, 0)
            payload = json.loads(buffer.getvalue())
            self.assertIn(
                "config_path",
                payload,
                "机器可读视图必须说清它读的是哪个文件（① 的回归点）",
            )
            self.assertEqual(payload["config_path"], cfg.source_path)
            rows = {r["key"]: r for r in payload["platforms"]}
            self.assertIn("homeassistant", rows)
            self.assertIn(
                "inbound_accepts_anything",
                rows["homeassistant"]["capabilities"],
                "⚠️ 这条路径以前读不到该字段 —— 而文档就是让用户走这条路径的",
            )

    def test_telegram_unconfigured_is_reported_with_what_is_missing(self):
        """telegram 是零容错的关键路径：缺什么必须一眼看见。"""
        with tempfile.TemporaryDirectory() as raw:
            cfg = Config.load(_write_config(Path(raw), {"config_version": 2, "adapters": {}}))
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                cli.run_setup(cfg, "", True)
            rows = {r["key"]: r for r in json.loads(buffer.getvalue())["platforms"]}
            telegram = rows["telegram"]
            self.assertFalse(telegram["configured"])
            self.assertEqual(telegram["missing"], ["bot_token"])
            self.assertFalse(telegram["inbound_ready"])
            # config_version >= 2 且空清单 ⇒ 闸门不放行任何人 —— 这个字段必须说清
            self.assertTrue(telegram["admits_nobody"])
            self.assertFalse(
                telegram["admits_any_sender"],
                "「闸门此刻是否真的会放行一切」必须与 config_version 合成同一个答案",
            )


if __name__ == "__main__":
    unittest.main()

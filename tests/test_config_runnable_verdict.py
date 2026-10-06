"""「这份配置此刻配好了没有」这个判定（``Adapter.config_runnable``）的测试。

它修的是什么
------------
``a2a`` 声明 ``config_optional = True``，而三处判定
（:func:`~opencode_bridge.__main__._has_configured_adapter` /
:func:`~opencode_bridge.__main__._platform_status` /
:func:`~opencode_bridge.__main__._channel_config_rows`）
曾把**这个分类**当成"空配置即可运行"的**判定**无条件信任 ⇒
``config.example.json`` 里那行 ``"a2a": {"bind_port": "", …}`` 就足以让全新安装的桥
跳过 :data:`~opencode_bridge.__main__.NO_ADAPTER_MESSAGE` 那条提前退出。
而那条前提早已不成立：``_coerce_port("")`` 给的是 ``UNCONFIGURED_PORT``（-1）、
``A2aAdapter.start()`` 据此打 error 并**不绑定就 return**。

⭐ 本文件里两类测试的作用不同，别把它们当成重复
-------------------------------------------------
* 「逐个平台」那几条守的是**没变的东西**：其余十二个平台的判定一个字都不能变，
  当年 Matrix/IRC/Mattermost 被拒启动的修复也必须仍然成立。
* ``TestVerdictIsNotVacuous`` 守的是**新东西真的被调用**：一个恒真的断言比没有
  断言更危险 —— 若三处仍读 ``config_optional`` 那个属性，下面所有"空配置是未配置"
  的断言会**照样全绿**（因为它们对 a2a 与对其余平台都是同一个答案）。
  所以这里喂一个**属性为真、判定说否**的假平台：只有真去问新判定才会答"否"。
"""

from __future__ import annotations

import json
import logging
import os
import unittest

from opencode_bridge import __main__ as cli
from opencode_bridge.adapters import adapter_class, base as adapters_base
from opencode_bridge.adapters import build, registered_names
from opencode_bridge.adapters.a2a import (
    MAX_BIND_PORT,
    UNCONFIGURED_PORT,
    A2aAdapter,
    _coerce_port,
)
from opencode_bridge.config import Config

logging.getLogger("opencode_bridge").addHandler(logging.NullHandler())

#: 仓库根目录（本文件在 ``<root>/tests/`` 下）。用来读 ``config.example.json`` ——
#: 模板是**全新安装实际拿到的那份**，它的判定必须由测试守住。
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def preflight(platform: str, entry: dict) -> bool:
    return cli._has_configured_adapter(Config(adapters={platform: entry}))


def status_row(platform: str, entry: dict) -> dict:
    rows = {
        row["key"]: row
        for row in cli._platform_status(Config(adapters={platform: entry}))
    }
    if platform not in rows:
        raise AssertionError(f"平台 {platform!r} 不在 _platform_status 输出里")
    return rows[platform]


def channel_row(platform: str, entry: dict) -> tuple[bool, bool]:
    rows = {
        key: (configured, inbound_ready)
        for key, _label, configured, inbound_ready, _caps
        in cli._channel_config_rows(Config(adapters={platform: entry}))
    }
    if platform not in rows:
        raise AssertionError(f"平台 {platform!r} 不在 _channel_config_rows 输出里")
    return rows[platform]


def complete_entry(platform: str) -> dict:
    """按平台**自己声明**的 ``required_tokens`` ∪ ``outbound_tokens`` 造一份配齐的条目。

    ⛔ 不硬编码任何一张平台表：那张表一漂，这几条测试就变成"守着一个假清单"。
    a2a 的 ``bind_port`` 是**端口**，不能用凭据那套占位值 —— 填 ``"x"`` 会被
    ``coerce_int`` 判为解析失败（那本身是对的，见 ``test_a2a_*``）。
    """
    cls = adapter_class(platform)
    entry: dict = {"allowed_chat_ids": ["someone"]}
    for key in tuple(getattr(cls, "required_tokens", ()) or ()) + tuple(
        getattr(cls, "outbound_tokens", ()) or ()
    ):
        entry[key] = 9900 if key == "bind_port" else "已填写"
    return entry


def bind_port_resolved_by_adapter(value: object) -> int:
    """:meth:`A2aAdapter.__init__` 会算出的那个端口。

    ⛔ 刻意**按名取**既有的私有解析（:func:`a2a._coerce_port`）而不是重写一份 ——
    测试自己抄一份解析的话，"判定与 ``start()`` 一致"就变成两个副本之间的巧合。
    """
    return _coerce_port(value)


class _FakeHooks:
    """只为读 ``capabilities()``；不消费任何事件（同 :class:`__main__._NullHooks`）。"""

    def on_inbound(self, inbound: object) -> None:  # pragma: no cover - 空实现
        pass

    def on_send_result(self, result: object) -> None:  # pragma: no cover - 空实现
        pass


class TestA2aAnswersAboutTheActualPort(unittest.TestCase):
    """⭐ 核心：a2a 如实回答「这份 ``bind_port`` 能不能绑」。"""

    def test_a_usable_port_means_runnable(self):
        """``0`` 与正整数都是**能跑**的 —— 尤其 ``0``（由系统分配）。"""
        for entry in (
            {"bind_port": 0},              # 0 = 由操作系统分配：start() 会把实际端口写回
            {"bind_port": 9900},
            {"bind_port": "9900"},         # 字符串也认（JSON 里写字符串是常事）
            {"bind_port": MAX_BIND_PORT},
            {"bind_port": 0, "allowed_chat_ids": ["peer"]},   # 别的键不影响判定
        ):
            with self.subTest(entry=entry):
                self.assertTrue(
                    A2aAdapter.config_runnable(entry),
                    f"{entry} 有能绑的端口，判定必须是「是」",
                )

    def test_an_unusable_port_means_not_runnable(self):
        """缺键 / 空串 / 负数 / 超区间 / 解析失败 ⇒ 全是 :data:`UNCONFIGURED_PORT`。"""
        for entry in (
            {},                            # 缺键
            {"bind_port": ""},
            {"bind_port": "   "},
            {"bind_port": None},
            {"bind_port": -1},
            {"bind_port": -9900},
            {"bind_port": MAX_BIND_PORT + 1},
            {"bind_port": "nope"},
            {"bind_port": []},             # 容器不是端口
            {"bind_port": True},           # 布尔不是端口（coerce_int 会读成 1）
            {"bind_port": 9900.7},         # 小数不是端口（coerce_int 会截断成 9900）
            {"bind_port": 9900.0},         # 同上：整数值的 float 也一样不是"整数"
            {"bind_port": 0.0},            # ⚠️ 尤其这一条：0.0 截断成 0 会说"能跑"
        ):
            with self.subTest(entry=entry):
                self.assertFalse(
                    A2aAdapter.config_runnable(entry),
                    f"{entry} 起不来，判定必须是「否」",
                )

    def test_the_judgement_and_the_adapter_agree_on_every_kind_of_value(self):
        """⭐ 判定与 ``start()`` 对**每一种**取值类型都必须是同一个答案。

        风险点是 :func:`opencode_bridge.config_coerce.coerce_int` 与
        :func:`_coerce_port` 是两条独立解析，而前者会把 ``True`` 读成 1、把
        ``9900.7`` 截断成 9900（后者都判成"未配置"）⇒ 不加类型挡板的话，
        判定会对这两种取值说"能跑"而 ``start()`` 拒绝启动。
        """
        for value in (None, "", "  ", "0", "9900", 0, 9900, -1, -5, 65535, 65536,
                      "nope", [], {}, True, False, 0.0, 9900.0, 9900.7, None):
            with self.subTest(bind_port=value):
                entry = {} if value is None else {"bind_port": value}
                self.assertEqual(
                    A2aAdapter.config_runnable(entry),
                    bind_port_resolved_by_adapter(
                        entry.get("bind_port", UNCONFIGURED_PORT)
                    ) >= 0,
                    f"bind_port={value!r}: 判定与 _coerce_port 不一致",
                )

    def test_zero_and_unconfigured_are_different_values(self):
        """⚠️ **本任务的核心**：``0``（配了、由系统分配）与 ``UNCONFIGURED_PORT``。

        ``_coerce_port`` 刻意把"没配"折叠成 :data:`UNCONFIGURED_PORT`，而
        :meth:`A2aAdapter.start` 的放行判据是 ``port < 0`` ⇒ 判定若把两者混为一谈，
        要么把空模板判成能跑（本缺陷），要么把 ``bind_port: 0`` 这个合法的测试配置
        拒掉（新的行为回退）。这里把两个值各自钉住，并断言它们**不是同一个数**。
        """
        self.assertEqual(UNCONFIGURED_PORT, -1)
        self.assertEqual(bind_port_resolved_by_adapter(""), UNCONFIGURED_PORT)
        self.assertEqual(bind_port_resolved_by_adapter(0), 0)
        self.assertGreaterEqual(bind_port_resolved_by_adapter(0), 0)
        self.assertLess(bind_port_resolved_by_adapter(""), 0)

    def test_the_verdict_agrees_with_what_start_actually_accepts(self):
        """判定与 ``start()`` 的放行判据**必须一致** —— 那是同一个问题的两个答案。

        ⛔ 不是"再抄一份解析"，而是断言两条**已有**的独立路径（判定走
        ``config_coerce``、``start()`` 走 ``_coerce_port``）在每个用例上给出同一个
        布尔答案；哪天有人只改一边，这条会红。
        """
        for value in (None, "", "  ", 0, 9900, -1, -5, 65535, 65536, "nope", []):
            with self.subTest(bind_port=value):
                entry = {} if value is None else {"bind_port": value}
                verdict = A2aAdapter.config_runnable(entry)
                start_would_bind = bind_port_resolved_by_adapter(
                    entry.get("bind_port", UNCONFIGURED_PORT)
                ) >= 0
                self.assertEqual(
                    verdict, start_would_bind,
                    f"bind_port={value!r}: 判定与 start() 的放行判据不一致",
                )

    def test_a_float_or_bool_port_is_refused_by_both_paths(self):
        """⭐ 挡板本身要被钉住：``coerce_int`` 会收下这两种类型，判定不许跟着收。

        没有这条的话，把 ``if isinstance(raw, (bool, float)): return False`` 删掉
        两处测试**都还会过**（因为它们只断言"判定与 ``_coerce_port`` 一致"）——
        而那正是本挡板存在的唯一理由。
        """
        for value in (True, False, 0.0, 9900.0, 9900.7):
            with self.subTest(bind_port=value):
                self.assertEqual(
                    bind_port_resolved_by_adapter(value), UNCONFIGURED_PORT,
                    f"_coerce_port({value!r}) 应判成未配置",
                )
                self.assertFalse(A2aAdapter.config_runnable({"bind_port": value}))


def a2a_port(value: object) -> int:
    """``A2aAdapter.__init__`` 用的那个端口解析（私有函数，按名取，不重写一份）。"""
    from opencode_bridge.adapters.a2a import _coerce_port

    return _coerce_port(value)


class TestAllThreeViewsNowSayTheSameThing(unittest.TestCase):
    """⭐ 缺陷的正面：三处视图**都**改口，且 ``missing`` 里**有** ``bind_port``。"""

    def test_an_empty_a2a_entry_is_unconfigured_in_all_three_views(self):
        entry: dict = {}
        self.assertFalse(preflight("a2a", entry), "预检必须说没配好")
        row = status_row("a2a", entry)
        self.assertFalse(row["configured"])
        self.assertFalse(row["outbound_ready"])
        self.assertFalse(row["inbound_ready"])
        self.assertEqual(row["missing"], ["bind_port"], "缺的正是那个起不来的端口")
        configured, inbound_ready = channel_row("a2a", entry)
        self.assertFalse(configured)
        self.assertFalse(inbound_ready)

    def test_a_usable_port_is_configured_in_all_three_views(self):
        """守住当年那个修复的**当前形态**：只配 a2a 且端口可用 ⇒ 不许拒绝启动。"""
        for entry in ({"bind_port": 9900}, {"bind_port": 0}):
            with self.subTest(entry=entry):
                self.assertTrue(
                    preflight("a2a", entry),
                    "只配 a2a 且端口可用时，桥接不许拒绝启动",
                )
                row = status_row("a2a", entry)
                self.assertTrue(row["configured"])
                self.assertTrue(row["outbound_ready"])
                self.assertTrue(row["inbound_ready"])
                self.assertEqual(row["missing"], [])
                self.assertEqual(channel_row("a2a", entry), (True, True))

    def test_a_non_empty_but_unbindable_port_is_also_unconfigured(self):
        """⚠️ 「键非空」不等于「能绑」—— 否则状态视图会放行一份起不来的配置。

        ``bind_port: "nope"`` 是通用规则（``_token_present``：值非空）会判成
        "配好了"的输入，而 :meth:`A2aAdapter.start` 会拒绝启动 ⇒ 那种配置一旦被说成
        配好，用户得到的是一个**空转**的桥。所以平台自答的判定必须是**权威**的。
        """
        entry = {"bind_port": "nope"}
        self.assertFalse(preflight("a2a", entry))
        row = status_row("a2a", entry)
        self.assertFalse(row["configured"])
        self.assertFalse(row["outbound_ready"])
        self.assertEqual(row["missing"], ["bind_port"])
        self.assertEqual(channel_row("a2a", entry)[0], False)


class TestTheOriginalFixStillHolds(unittest.TestCase):
    """⭐ ``config_optional`` 存在的**原始原因**那一批平台：配了真凭据必须被认成已配置。"""

    def test_platforms_without_a_bot_token_are_still_recognised(self):
        """当年 Matrix / IRC / Mattermost 被硬编码的 ``bot_token`` 判据拒启动。

        它们走的是 ``required_tokens`` 通用规则分支（``config_optional`` 为 False），
        所以新判定**一个字都没改**它们的路 —— 这几条就是那道守卫。
        """
        cases = {
            "matrix": {"homeserver": "https://m.example.org",
                       "access_token": "syt", "user_id": "@a:b"},
            "irc": {"host": "irc.example.org", "nick": "bot", "channels": ["#x"]},
            "mattermost": {"site_url": "https://mm.example.com", "token": "tok"},
        }
        for platform, entry in cases.items():
            with self.subTest(platform=platform):
                self.assertTrue(
                    preflight(platform, entry),
                    f"{platform} 配齐了却被判定为未配置（当年那个 bug）",
                )
                row = status_row(platform, entry)
                self.assertTrue(row["configured"])
                self.assertEqual(row["missing"], [])
                self.assertTrue(channel_row(platform, entry)[0])

    def test_incomplete_entries_are_still_rejected(self):
        """拆掉 a2a 那条门槛之后，**拒绝**路径必须仍然有效。"""
        for platform, entry in (
            ("telegram", {"allowed_chat_ids": [1]}),      # 只有白名单没有凭据
            ("matrix", {"access_token": "x"}),            # 缺 homeserver / user_id
            ("irc", {"host": "  ", "nick": "n"}),         # 空白值
            ("ntfy", {}),                                 # 缺 topic
        ):
            with self.subTest(platform=platform):
                self.assertFalse(preflight(platform, entry))

    def test_declaration_obligations_are_untouched(self):
        """新判定**只**回答"够不够跑"，绝不能变成"不必声明配置面"。"""
        for platform in registered_names():
            cls = adapter_class(platform)
            with self.subTest(platform=platform):
                self.assertTrue(getattr(cls, "required_tokens", ()))
                self.assertTrue(getattr(cls, "outbound_tokens", ()))


class TestEveryOtherPlatformIsUnchanged(unittest.TestCase):
    """⭐ 13 个平台逐个断言：只有 a2a 的判定是"看配置"的。"""

    def test_only_a2a_answers_its_own_readiness(self):
        self_answered = [
            name for name in registered_names()
            if adapter_class(name).config_runnable({}) is True
        ]
        self.assertEqual(self_answered, [], "只有 a2a 能被新判定说成能跑")

    def test_the_other_twelve_keep_the_default_verdict(self):
        """**一个都不许改**：基类默认 ``False`` 就是那十二个平台的行为。"""
        for name in registered_names():
            if name == "a2a":
                continue
            cls = adapter_class(name)
            with self.subTest(platform=name):
                self.assertFalse(cls.config_runnable({}), "空配置不该被说成能跑")
                self.assertFalse(
                    cls.config_runnable(complete_entry(name)),
                    "配齐时也该由 required_tokens 规则回答，而不是新判定",
                )
                self.assertFalse(getattr(cls, "config_optional", False))

    def test_every_platform_is_configured_when_its_own_keys_are_present(self):
        """13 个平台逐个：按**它自己声明**的键配齐 ⇒ 预检与状态视图都必须说配好。"""
        for name in registered_names():
            with self.subTest(platform=name):
                entry = complete_entry(name)
                self.assertTrue(
                    preflight(name, entry), f"{name} 配齐了却被拒绝启动"
                )
                row = status_row(name, entry)
                self.assertTrue(row["configured"], f"{name}: {row['missing']}")
                self.assertEqual(row["missing"], [])
                self.assertTrue(row["outbound_ready"], f"{name} 明明能发出去")
                self.assertTrue(channel_row(name, entry)[0])

    def test_every_platform_is_unconfigured_when_its_keys_are_absent(self):
        """反向：空条目 ⇒ 13 个平台**全部**未配置（a2a 现在也在内）。"""
        for name in registered_names():
            with self.subTest(platform=name):
                self.assertFalse(preflight(name, {}), f"{name} 空配置却说配好了")
                row = status_row(name, {})
                self.assertFalse(row["configured"], f"{name} 空配置却说配好了")
                self.assertTrue(row["missing"], f"{name} 说未配置却没列出缺什么")
                self.assertFalse(channel_row(name, {})[0])


class TestTheDeclarationShapeIsPinned(unittest.TestCase):
    """⭐ 护栏：``config_optional`` 必须**一直是 ``bool``**（陷阱 1 与 2）。"""

    def test_config_optional_is_still_a_plain_bool(self):
        """把它改成方法 ⇒ ``getattr(cls, ...)`` 取到未绑定函数（永远真值）
        ⇒ 13 个平台同时变成"可省略配置"；而它还是 ``capabilities()`` 的输出项，
        方法对象会让 ``--setup --json`` 的 ``json.dumps`` 当场炸。"""
        for name in registered_names():
            cls = adapter_class(name)
            with self.subTest(platform=name):
                declared = getattr(cls, "config_optional", False)
                self.assertIsInstance(declared, bool, "必须是 bool，不是方法对象")
                self.assertFalse(callable(declared), "可调用就说明它被改成了方法")

    def test_config_runnable_is_a_callable_on_every_platform(self):
        """新判定必须**每个平台都能被调到**（基类给了默认实现）。"""
        for name in registered_names():
            cls = adapter_class(name)
            with self.subTest(platform=name):
                self.assertTrue(callable(cls.config_runnable))
                # 判定只看 entry，不许要求构造实例 —— 预检跑在 endpoint discovery 之前
                self.assertIsInstance(cls.config_runnable({}), bool)

    def test_capabilities_output_is_still_json_serialisable(self):
        """``--setup --json`` 会 ``dict(...)`` 整个 ``capabilities()`` 去 dumps。"""
        for name in registered_names():
            with self.subTest(platform=name):
                caps = dict(
                    build(name, complete_entry(name), _FakeHooks()).capabilities()
                )
                json.dumps(caps, ensure_ascii=False)     # 炸了就抛
                self.assertIsInstance(caps["config_optional"], bool)
        # a2a 自己那份多出来的字段也一样（``--status`` 会读它）
        a2a_caps = dict(
            build("a2a", {"bind_port": 0}, _FakeHooks()).capabilities()
        )
        json.dumps(a2a_caps, ensure_ascii=False)
        self.assertTrue(a2a_caps["config_optional"])

    def test_setup_json_payload_round_trips(self):
        """整份 ``--setup --json`` 载荷（``platforms[].capabilities`` 是嵌进去的）。"""
        payload = {
            "config_path": "config.json",
            "platforms": cli._platform_status(
                Config(adapters={"a2a": {"bind_port": 9900}})
            ),
        }
        json.dumps(payload, ensure_ascii=False)


class TestVerdictIsNotVacuous(unittest.TestCase):
    """⭐ 反退化：没有这几条，新判定可能压根没被调用而测试照样全绿。"""

    class _FakeOptionalWithoutVerdict(adapters_base.Adapter):
        """属性照实说 ``config_optional = True``，但**不覆写**新判定。"""

        name = "probe_optional_without_verdict"
        label = "Probe Optional Without Verdict"
        config_optional = True
        required_tokens = ("probe_api_key",)
        outbound_tokens = ("probe_api_key",)

        def start(self) -> None:
            pass

        def send(self, out):  # pragma: no cover - 空实现
            return None

        def edit(self, handle, out):  # pragma: no cover - 空实现
            return False

    class _FakeOptionalWithVerdict(_FakeOptionalWithoutVerdict):
        """同一个分类，但**覆写**判定并说「这份配置能跑」。"""

        name = "probe_optional_with_verdict"
        label = "Probe Optional With Verdict"

        @classmethod
        def config_runnable(cls, entry):
            return True

    def setUp(self):
        for cls in (self._FakeOptionalWithoutVerdict, self._FakeOptionalWithVerdict):
            adapters_base.register(cls.name)(cls)
            # ⛔ 必须清掉：``registered_names()`` 会把注册表里的东西全列出来，
            # 留着会污染同进程里别的测试（它按注册表枚举平台）。
            self.addCleanup(adapters_base._REGISTRY.pop, cls.name, None)

    def test_a_truthy_declaration_alone_does_not_make_a_platform_configured(self):
        """属性为真、判定说否 ⇒ 预检**必须**说未配置。

        这一条正是本缺陷的形状：若三处仍读 ``config_optional`` 那个属性，答案会是
        ``True`` —— 所以它红了就说明判定真的被调用了。
        """
        cls = self._FakeOptionalWithoutVerdict
        entry = {"allowed_chat_ids": ["someone"]}
        self.assertTrue(cls.config_optional, "前提：这个假平台属性确实为真")
        self.assertFalse(cls.config_runnable(entry))
        self.assertFalse(
            preflight(cls.name, entry),
            "只凭 config_optional 就放行 = 本缺陷原样",
        )
        # 就算凭据也填上了也一样：平台自答"跑不了"时它的答案是权威的。
        self.assertFalse(preflight(cls.name, {**entry, "probe_api_key": "x"}))
        row = status_row(cls.name, entry)
        self.assertFalse(row["configured"])
        self.assertEqual(row["missing"], ["probe_api_key"])
        self.assertFalse(channel_row(cls.name, entry)[0])

    def test_a_verdict_that_says_yes_is_honoured_with_an_empty_entry(self):
        """反向：判定说能跑 ⇒ 空配置也放行（否则上面那条可能只是"永远说不"）。"""
        cls = self._FakeOptionalWithVerdict
        entry = {"allowed_chat_ids": ["someone"]}
        self.assertTrue(cls.config_runnable(entry))
        self.assertTrue(preflight(cls.name, entry), "判定说能跑就必须放行")
        row = status_row(cls.name, entry)
        self.assertTrue(row["configured"])
        self.assertEqual(row["missing"], [])
        self.assertTrue(row["outbound_ready"])
        self.assertTrue(channel_row(cls.name, entry)[0])

    def test_all_three_call_sites_actually_call_the_verdict(self):
        """⭐ 「只改一处不算完成」这道护栏：三处**都**必须真的问到判定。

        做法是数调用次数：三个视图各走一遍，计数器必须每次都 +1。
        ⚠️ 只断言"答案是对的"不够 —— 对 a2a 而言两条路在 ``bind_port`` 填对时同向，
        那种断言恒真。
        """
        calls: list[str] = []

        class _CountingVerdict(self._FakeOptionalWithVerdict):
            name = "probe_counting_verdict"
            label = "Probe Counting Verdict"

            @classmethod
            def config_runnable(cls, entry):
                calls.append(cls.name)
                return True

        adapters_base.register(_CountingVerdict.name)(_CountingVerdict)
        self.addCleanup(adapters_base._REGISTRY.pop, _CountingVerdict.name, None)

        entry = {"allowed_chat_ids": ["someone"]}
        cfg = Config(adapters={_CountingVerdict.name: entry})

        calls.clear()
        cli._has_configured_adapter(cfg)
        self.assertEqual(len(calls), 1, "_has_configured_adapter 没有问判定")

        calls.clear()
        cli._platform_status(cfg)
        self.assertGreaterEqual(len(calls), 1, "_platform_status 没有问判定")

        calls.clear()
        cli._channel_config_rows(cfg)
        self.assertGreaterEqual(len(calls), 1, "_channel_config_rows 没有问判定")

    def test_an_unknown_platform_key_still_does_not_block_the_user(self):
        """未注册的适配器键那条分支照旧（``required_tokens`` 缺省值是 ``()``）。"""
        self.assertTrue(
            preflight("platform_that_was_never_registered", {"some_key": "value"}),
            "有像凭据的字段就不该凭空拦住用户",
        )
        self.assertFalse(preflight("platform_that_was_never_registered", {}))


class TestShippedTemplateStaysUnconfigured(unittest.TestCase):
    """⭐ 任务④的判据：``config.example.json`` 里那行 a2a 加回来了，而空模板仍判未配置。

    这就是那个**实测出来的缺陷**：加上 ``"a2a": {"bind_port": "", …}`` 之后，
    全新安装的预检曾由 ``False`` 翻成 ``True`` ⇒ 桥不再走
    :data:`~opencode_bridge.__main__.NO_ADAPTER_MESSAGE`（``docs/install.md``
    Step 4 第 4 项写给用户的行为）。
    """

    @classmethod
    def setUpClass(cls):
        path = os.path.join(REPO_ROOT, "config.example.json")
        with open(path, encoding="utf-8") as fh:
            cls.template = json.load(fh)

    def test_template_still_parses_and_lists_every_platform(self):
        adapters = self.template["adapters"]
        self.assertEqual(len(adapters), 13, "模板的平台数变了（新平台没进模板？）")
        self.assertIn("a2a", adapters)
        self.assertEqual(sorted(adapters), sorted(registered_names()))

    def test_the_shipped_a2a_entry_is_an_empty_one(self):
        """⛔ 模板里 ``bind_port`` 必须是**空串**（未填），不是 ``0``。

        填了 ``0`` 的话，空模板就会被判成"a2a 能跑"⇒ 预检为 True ⇒ 同一个缺陷
        从模板这一侧回来。
        """
        self.assertEqual(self.template["adapters"]["a2a"]["bind_port"], "")

    def test_a_fresh_install_from_the_template_is_still_reported_unconfigured(self):
        cfg = Config(adapters=self.template["adapters"])
        self.assertFalse(
            cli._has_configured_adapter(cfg),
            "空模板曾被判成已配置 —— 桥会跳过 NO_ADAPTER_MESSAGE 那条提前退出",
        )
        row = status_row("a2a", self.template["adapters"]["a2a"])
        self.assertFalse(row["configured"])
        self.assertEqual(row["missing"], ["bind_port"])

    def test_the_template_is_never_served_as_a_working_configuration(self):
        """模板里每个平台的凭据都是空 ⇒ 13 个平台逐个都必须是未配置。"""
        for platform, entry in self.template["adapters"].items():
            with self.subTest(platform=platform):
                self.assertFalse(preflight(platform, entry))
                row = status_row(platform, entry)
                self.assertFalse(row["configured"], f"{platform}: {row['missing']}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

"""``/model`` 命令测试 —— 全离线，走真实的 :class:`BridgeCore` 分发路径。

命令的三种形态（裸 ``/model`` / ``provider/id`` / 关键词）都要在这里锁住，
特别是两条"绝不能发生"的事：**目录里没有的模型不许发给服务端**、
**搜索不许顺手把模型切了**。模型目录的 819 条真实数据用 fixture 里的 4 条代替。
"""

from __future__ import annotations

import tempfile
import unittest

from opencode_bridge.core import HELP_TEXT
from opencode_bridge.opencode_client import OpenCodeError
from opencode_bridge.state import StateStore

from tests.test_core import FakeAdapter, FakeClient, inbound, make_env

CONVERSATION = "chat:55"


class ModelCommandTestCase(unittest.TestCase):
    """每个用例一套全新的 core / client / adapter。"""

    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        (
            self.core,
            self.client,
            self.adapter,
            self.state,
            self._state_path,
            self.config,
        ) = make_env(self._tempdir.name)
        self.send = lambda text: self.core.on_inbound(  # noqa: E731
            inbound(CONVERSATION, text)
        )

    def start_session(self) -> str:
        """先发一条普通消息把会话建起来，返回 session id。"""
        self.send("hello")
        return self.client.created_ids[0]

    @property
    def last_reply(self) -> str:
        return self.adapter.sent[-1].text

    @property
    def last_kind(self) -> str:
        return self.adapter.sent[-1].kind


class BareModelTests(ModelCommandTestCase):
    def test_bare_model_reports_the_current_session_model(self):
        session_id = self.start_session()

        self.send("/model")

        text = self.last_reply
        self.assertEqual(self.client.get_session_calls[-1], session_id)
        self.assertIn("当前模型: prov/model-x", text)
        # 顺带把"怎么用"说出来，用户不该靠猜。
        self.assertIn("/model <provider>/<id>", text)
        self.assertIn("/model <关键词>", text)
        # 查询不该碰目录，也不该动模型。
        self.assertEqual(self.client.list_models_calls, [])
        self.assertEqual(self.client.model_switches, [])

    def test_bare_model_says_unknown_when_the_server_omits_the_model(self):
        self.start_session()
        self.client.session_info.pop("model")

        self.send("/model")

        # 服务端没给就说没给，不能瞎猜一个，也不能报错。
        self.assertIn("当前模型: 未知", self.last_reply)
        self.assertEqual(self.last_kind, "text")

    def test_bare_model_creates_the_session_when_there_is_none_yet(self):
        self.assertEqual(self.state.get_session(CONVERSATION), None)

        self.send("/model")

        # 与 /new /cd 同一条路：复用 _ensure_session，裸 /model 不会因为
        # "还没开始对话"而失败。
        self.assertEqual(len(self.client.created_ids), 1)
        self.assertEqual(
            self.client.get_session_calls, self.client.created_ids
        )

    def test_get_session_failure_surfaces_as_the_generic_command_error(self):
        self.start_session()
        self.client.get_session_error = OpenCodeError("boom", status=500)

        self.send("/model")

        self.assertEqual(self.last_kind, "error")
        self.assertIn("命令执行失败", self.last_reply)


class SwitchModelTests(ModelCommandTestCase):
    def test_switching_reports_the_old_and_the_new_model(self):
        session_id = self.start_session()

        self.send("/model opencode/space-bunny-free")

        self.assertEqual(
            self.client.model_switches,
            [(session_id, "opencode", "space-bunny-free")],
        )
        self.assertIn(
            "已切换模型: prov/model-x -> opencode/space-bunny-free", self.last_reply
        )

    def test_switching_sends_only_provider_and_id_never_the_variant(self):
        self.start_session()

        self.send("/model anthropic/claude-sonnet-4-5")

        (_session_id, provider_id, model_id) = self.client.model_switches[0]
        # variant 交给服务端自己补（只给 providerID+id 实测返回 204）。
        self.assertEqual((provider_id, model_id), ("anthropic", "claude-sonnet-4-5"))

    def test_unknown_pair_is_refused_and_the_server_is_never_asked_to_switch(self):
        self.start_session()

        self.send("/model opencode/no-such-model")

        self.assertEqual(
            self.client.model_switches, [], "目录里没有的模型绝不能发给服务端"
        )
        text = self.last_reply
        self.assertIn("没有这个模型: opencode/no-such-model", text)
        self.assertEqual(self.last_kind, "error")
        # 连一个像的都没有时，也得告诉用户下一步怎么找，而不是干巴巴一句。
        self.assertIn("/model <关键词>", text)

    def test_a_misspelled_provider_is_corrected_by_suggestion(self):
        self.start_session()

        self.send("/model openai/claude-sonnet-4-5")

        text = self.last_reply
        # 模型 id 是对的、provider 写错了 —— 这条最常见，要直接把它指出来。
        self.assertIn("你要找的是不是下面这些：", text)
        self.assertIn("anthropic/claude-sonnet-4-5", text)
        self.assertEqual(self.client.model_switches, [])

    def test_a_nearly_right_model_id_is_suggested(self):
        self.start_session()

        self.send("/model opencode/space-bunny-fre")

        text = self.last_reply
        self.assertIn("你要找的是不是下面这些：", text)
        self.assertIn("opencode/space-bunny-free", text)
        self.assertEqual(self.client.model_switches, [])

    def test_unknown_pair_does_not_create_a_session(self):
        # 校验一个不存在的模型，不该为了校验先造一个会话出来。
        self.send("/model opencode/no-such-model")

        self.assertEqual(self.client.created_ids, [])
        self.assertEqual(self.client.model_switches, [])

    def test_switch_failure_surfaces_as_the_generic_command_error(self):
        self.start_session()
        self.client.set_session_model = (  # type: ignore[method-assign]
            lambda *a, **k: (_ for _ in ()).throw(
                OpenCodeError("nope", status=400)
            )
        )

        self.send("/model opencode/space-bunny-free")

        self.assertEqual(self.last_kind, "error")
        self.assertIn("命令执行失败", self.last_reply)


class SearchModelTests(ModelCommandTestCase):
    def test_searching_lists_matches_without_switching_or_creating_a_session(self):
        self.send("/model sonnet")

        text = self.last_reply
        self.assertIn("匹配「sonnet」", text)
        self.assertIn("anthropic/claude-sonnet-4-5", text)
        self.assertIn("Claude Sonnet 4.5", text)
        # 搜索是纯查询：不碰 session，也不换模型。
        self.assertEqual(self.client.created_ids, [])
        self.assertEqual(self.client.model_switches, [])
        self.assertEqual(self.client.get_session_calls, [])

    def test_search_matches_provider_and_name_as_well_as_the_id(self):
        self.send("/model bunny")

        text = self.last_reply
        self.assertIn("opencode/space-bunny-free", text)
        self.assertIn("opencode/space-bunny ", text)  # 前缀那条也算
        self.assertNotIn("claude-sonnet", text)

    def test_search_with_no_match_says_how_many_models_exist(self):
        self.send("/model zzz-no-such-keyword")

        self.assertEqual(self.last_kind, "error")
        self.assertIn("没有匹配", self.last_reply)
        self.assertIn("4", self.last_reply)  # fixture 里 4 个模型
        self.assertEqual(self.client.model_switches, [])


class CatalogCacheTests(ModelCommandTestCase):
    def test_the_catalog_is_fetched_once_and_reused(self):
        self.send("/model bunny")
        self.assertEqual(len(self.client.list_models_calls), 1)

        self.send("/model bunny")
        self.send("/model sonnet")
        self.send("/model opencode/space-bunny-free")

        # 819 条目录不是分页接口，每次 /model 都重拉一遍纯属浪费。
        self.assertEqual(len(self.client.list_models_calls), 1)

    def test_the_catalog_is_refetched_once_the_ttl_expires(self):
        self.core.model_command.clock = lambda: 1000.0
        self.core.model_command.ttl_seconds = 60.0

        self.send("/model bunny")
        self.assertEqual(len(self.client.list_models_calls), 1)
        self.core.model_command.clock = lambda: 1000.0 + 59.0
        self.send("/model bunny")
        self.assertEqual(len(self.client.list_models_calls), 1, "TTL 内不该重拉")

        self.core.model_command.clock = lambda: 1000.0 + 61.0
        self.send("/model bunny")
        self.assertEqual(len(self.client.list_models_calls), 2, "TTL 过期后要重拉")

    def test_a_switch_after_the_ttl_still_uses_the_fresh_catalog(self):
        now = [0.0]
        self.core.model_command.clock = lambda: now[0]
        self.core.model_command.ttl_seconds = 30.0

        self.send("/model bunny")            # populates the cache
        now[0] = 31.0
        self.send("/model opencode/space-bunny")   # still in the catalog

        self.assertEqual(len(self.client.model_switches), 1)
        self.assertEqual(len(self.client.list_models_calls), 2)


class MalformedArgumentTests(ModelCommandTestCase):
    CASES = {
        "opencode/": "缺少模型 id",
        "opencode//free": "只有一层斜杠",
        "/free": "缺少 provider",
        "a/b/c": "只有一层斜杠",
        "opencode/ space-bunny": "不能有空格",
        "open code/space-bunny": "不能有空格",
        "space bunny": "不能有空格",
    }

    def test_malformed_arguments_get_a_clear_message_not_a_traceback(self):
        for argument, expected in self.CASES.items():
            with self.subTest(argument=argument):
                self.adapter.sent.clear()
                self.send(f"/model {argument}")

                text = self.last_reply
                self.assertEqual(self.last_kind, "error")
                self.assertIn(expected, text)
                self.assertIn("用法", text)
                self.assertNotIn("Traceback", text)
                self.assertNotIn("命令执行失败", text)
                # 解析失败绝不能顺手换掉模型。
                self.assertEqual(self.client.model_switches, [])

    def test_a_leading_slash_is_reported_as_a_missing_provider(self):
        self.send("/model opencode/space-bunny/")

        self.assertIn("只有一层斜杠", self.last_reply)
        self.assertEqual(self.client.model_switches, [])


class ModelCommandWiringTests(ModelCommandTestCase):
    def test_help_lists_the_model_command(self):
        self.send("/help")

        self.assertEqual(self.last_reply, HELP_TEXT)
        self.assertIn("/model", HELP_TEXT)

    def test_bot_suffix_after_the_command_name_is_stripped(self):
        # Telegram 群里发的是 /model@your_bot，name.split("@") 在 _handle_command
        # 里已经处理过，这里只验证结果对。
        self.start_session()

        self.send("/model@my_bridge_bot")

        self.assertIn("当前模型: prov/model-x", self.last_reply)
        self.assertEqual(self.last_kind, "text")

    def test_the_command_does_not_forward_anything_to_the_model(self):
        self.send("/model bunny")

        self.assertEqual(self.client.prompts, [])

    def test_restart_keeps_the_model_because_it_lives_server_side(self):
        session_id = self.start_session()
        self.send("/model opencode/space-bunny-free")

        # 模拟重启：新的 core / client / state，session_id 从 state.json 恢复，
        # 模型本身在服务端，所以桥不需要为它记任何东西。
        restarted_client = FakeClient()
        restarted_client.session_info["model"] = {
            "providerID": "opencode", "id": "space-bunny-free",
        }
        restarted_core = type(self.core)(
            self.config, restarted_client, StateStore(self._state_path)
        )
        restarted_adapter = FakeAdapter()
        restarted_core.attach(restarted_adapter)
        restarted_core.on_inbound(inbound(CONVERSATION, "/model"))

        self.assertEqual(restarted_client.created_ids, [], "重启不该新建会话")
        self.assertIn("当前模型: opencode/space-bunny-free",
                      restarted_adapter.sent[-1].text)
        self.assertEqual(restarted_core.state.get_session(CONVERSATION), session_id)
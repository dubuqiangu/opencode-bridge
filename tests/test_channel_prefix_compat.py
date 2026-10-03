"""歧义旧前缀 ``channel:`` 的**归属划分**（G5 第 2 步）—— slack / discord / mattermost。

## 这条路径在防什么

``channel:`` 迁移前被三家共用，而键里**没有**任何能指出平台的信息
（:data:`identity.LEGACY_PREFIXES["channel"]` 的值是 ``None``）。归属**从未被持久化**
—— 真实 ``state.json`` 只有 ``{"sessions": …, "meta": …}``，``meta`` 里除游标外只有
``/cd`` 写的 ``directory``。所以"这个 ``channel:`` 键原属谁"在盘上**根本不存在**。

于是读侧不猜"历史上挂过谁"，而是靠**各家自己声明的 local id 文法**做一条
**不相交划分**（文法声明在拥有该平台的适配器里，判定入口是
:meth:`~opencode_bridge.adapters.base.Adapter.owns_local_id`）：

============  =========================  ==============================
平台          文法                       承重的那一条
============  =========================  ==============================
slack         ``^[A-Z][A-Z0-9]{5,}$``     首字符是大写字母
discord       ``^[0-9]{17,20}$``          纯数字且不超过 20 位
mattermost    ``^[a-z0-9]{26}$``          恰好 26 位
============  =========================  ==============================

## 覆盖清单

1. **三家同时挂载、三家各自持有 ``channel:`` 会话 ⇒ 全部存活、互不串台**
   （旧方案在这种情况下放弃回退，把三份历史会话全丢了）；
2. **只挂 slack 时，discord 的 ``channel:`` 键不被 slack 认领** —— 这正是旧方案
   （"唯一申报者才回退"）判错的那一格：曾经同时跑过 slack + discord、后来删掉
   discord 的用户，会被 slack 认领 discord 的键，而"曾经是否共存过"盘上无从验证；
3. 边界：过短、26 位纯数字、21 位数字、27 位小写 —— 文法一旦放松就会跨平台，
   而跨平台的症状是**静默**的（用户被接到别人的会话上，不报错）；
4. **不相交性本身**是算出来的性质，不是"我列的表好看"；
5. 提问平台必须是**那一个**平台：传错 / 传空 / 平台没挂载 ⇒ 一律不读；
6. 盘上**逐字节不变**：``channel:`` 永不迁移、零备份、两次加载幂等；
7. 写只落新键；``drop_session`` 删掉本平台文法覆盖得到的**全部**候选键，
   且**不碰**别人的键；
8. 端到端走真 :class:`~opencode_bridge.core.BridgeCore`：三家各自复用**自己**的
   历史会话，连 ``create_session`` 都不该被调用。

零真实网络、零真实 sleep。临时目录钉在仓库内的 ``.tmp/``。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from opencode_bridge.adapters.base import Adapter
from opencode_bridge.adapters.discord import DiscordAdapter
from opencode_bridge.adapters.mattermost import MattermostAdapter
from opencode_bridge.adapters.slack import SlackAdapter
from opencode_bridge.adapters.telegram import TelegramAdapter
from opencode_bridge.config import Config
from opencode_bridge.conversation_keys import ConversationState
from opencode_bridge.core import BridgeCore
from opencode_bridge.hooks import Inbound
from opencode_bridge.state import StateStore, _classify_key, _legacy_alias, _lookup
from tests.test_core import FakeAdapter, FakeClient

_REPOSITORY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: 仓库内的临时目录：即便 TEMP/TMP 指向机器别处，测试也不可能写到仓库之外。
_REPOSITORY_TEMP = os.path.join(_REPOSITORY_ROOT, ".tmp")

#: 三家真实的适配器类。**文法只能从它们身上取** —— 测试里另抄一份正则就等于
#: 少了一道"实现被改松了这里也会红"的检查。
REAL_ADAPTERS = {
    "slack": SlackAdapter,
    "discord": DiscordAdapter,
    "mattermost": MattermostAdapter,
}

#: 各家**真实形状**的 local id（⚠️ 不是 ``C1`` 这种占位串：占位串不符合任何一家的
#: 文法，拿它当夹具等于把"这个形状到底归谁"这件事测没了）。
SLACK_CHANNEL_ID = "C01ABCDEFGH"                       # 真实 Slack id 是 9~11 位
SLACK_SHORTEST_CLAIMABLE_ID = "C01ABC"                # 本家文法的下限（6 位）
DISCORD_CHANNEL_ID = "123456789012345678"              # snowflake，18 位
MATTERMOST_CHANNEL_ID = "9d5xk3pz8rj7wq2mf4nb6vhycg"  # model.NewId()：恰好 26 位

#: 切换前的键。刻意写**字面量**：断言"旧键不许被改写"必须比字面量，比常量会恒真。
LEGACY_SLACK_KEY = "channel:C01ABCDEFGH"
LEGACY_DISCORD_KEY = "channel:123456789012345678"
LEGACY_MATTERMOST_KEY = "channel:9d5xk3pz8rj7wq2mf4nb6vhycg"

#: 端到端用例里三家各自的 local id / 盘上会话 / 旧键。
PLATFORM_FIXTURES = (
    ("slack", SLACK_CHANNEL_ID, "ses-slack", LEGACY_SLACK_KEY),
    ("discord", DISCORD_CHANNEL_ID, "ses-discord", LEGACY_DISCORD_KEY),
    ("mattermost", MATTERMOST_CHANNEL_ID, "ses-mattermost", LEGACY_MATTERMOST_KEY),
)
#: 三家在盘上**同时**存在的那份旧键表（下面多个用例复用它）。
ALL_LEGACY_SESSIONS = {
    legacy_key: session_id
    for _platform, _local_id, session_id, legacy_key in PLATFORM_FIXTURES
}

#: （说明, local id, 可以认领它的平台）。**承重**的一张表：它就是"不相交"的具体形状。
OWNERSHIP_TABLE = (
    ("Slack 真实长度 11 位", SLACK_CHANNEL_ID, ("slack",)),
    ("Slack 文法下限 6 位", SLACK_SHORTEST_CLAIMABLE_ID, ("slack",)),
    ("Discord snowflake 18 位", DISCORD_CHANNEL_ID, ("discord",)),
    ("Discord 文法下限 17 位", "1" * 17, ("discord",)),
    ("Discord 文法上限 20 位", "1" * 20, ("discord",)),
    ("Mattermost 26 位字母数字", MATTERMOST_CHANNEL_ID, ("mattermost",)),
    # ↓ 下面几条是**边界**：放松任何一家的文法，它们就会指向错误的平台
    (
        "26 位纯数字归 mattermost —— base32 字母表含数字，且已超出 discord 的 20 位上限",
        "1" * 26, ("mattermost",),
    ),
    ("21 位纯数字：discord 超上限、mattermost 长度不符 ⇒ 谁都不认", "1" * 21, ()),
    ("27 位小写：mattermost 只认恰好 26 位 ⇒ 谁都不认", "a" * 27, ()),
    ("16 位纯数字：低于 discord 下限 ⇒ 谁都不认", "1" * 16, ()),
    ("25 位小写：差一位 ⇒ 谁都不认", "a" * 25, ()),
    ("``abc``：又短又小写，谁都不认", "abc", ()),
    ("telegram 的数字 chat id 不属于三家任何一家", "55", ()),
    ("matrix 房间 id 含小写与冒号，不属于三家任何一家", "!abc:example.org", ()),
)


def adapter_config_for(platform: str) -> dict:
    """三家各自的最小配置（只用来构造真适配器，不碰网络）。"""
    if platform == "slack":
        return {"bot_token": "xoxb-token-not-real", "app_token": "xapp-not-real"}
    if platform == "discord":
        return {"bot_token": "token-not-real"}
    return {"site_url": "https://example.test", "token": "token-not-real"}


def build_adapter(platform: str) -> Adapter:
    """真适配器的离线实例（本文件只用到类属性，不碰网络）。"""
    return REAL_ADAPTERS[platform](adapter_config_for(platform), hooks=None)  # type: ignore[arg-type]


class ChannelPlatformStub(FakeAdapter):
    """带**真实平台身份**的离线替身：名字、旧前缀、文法都从真适配器借来。

    走真适配器会让 :meth:`BridgeCore.on_inbound` 触发真实出站 HTTP（ack 那条
    "已接收"），测试就必须联网了 —— 所以 core 层的用例一律用这个替身。

    借的是**真适配器的类属性**（文法就是那家的 API 属性，不该在测试里另抄一份）：
    实现被改松时这些用例照样会红。
    """

    def __init__(self, platform: str) -> None:
        super().__init__()
        self.name = platform
        self.label = platform
        real = REAL_ADAPTERS.get(platform)
        if real is not None:
            self.label = real.label
            self.legacy_conversation_prefix = real.legacy_conversation_prefix
            self.local_id_pattern = real.local_id_pattern


def conversation_state_for(path: str, mounted: tuple[str, ...]) -> ConversationState:
    """装配一份门面：``mounted`` 里是当前"已挂载"的平台名。"""
    adapters = [build_adapter(platform) for platform in mounted]
    return ConversationState(
        StateStore(path, migrate_keys=True),
        lambda mounted_now=adapters: list(mounted_now),
    )


class ConversationStateTestCase(unittest.TestCase):
    """每个用例一份仓库内的临时目录 + 写盘/读盘助手。"""

    def setUp(self) -> None:
        os.makedirs(_REPOSITORY_TEMP, exist_ok=True)
        self._directory = tempfile.TemporaryDirectory(dir=_REPOSITORY_TEMP)
        self.addCleanup(self._directory.cleanup)
        self.directory = self._directory.name
        self.path = os.path.join(self.directory, "state.json")

    def write_state(self, sessions: dict, meta: dict | None = None) -> str:
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"sessions": sessions, "meta": meta or {}}, handle,
                      ensure_ascii=False)
        return self.path

    def read_bytes(self, path: str | None = None) -> bytes:
        with open(path or self.path, "rb") as handle:
            return handle.read()

    def sessions_on_disk(self) -> dict:
        return json.loads(self.read_bytes().decode("utf-8"))["sessions"]

    def backup_files(self) -> list[str]:
        import glob

        return sorted(glob.glob(os.path.join(self.directory, "state.json.bak.*")))


class OwnershipIsAPartition(unittest.TestCase):
    """文法层面的判定：谁认领哪些形状。这一层零磁盘、零装配。"""

    def setUp(self) -> None:
        self.adapters = {
            platform: build_adapter(platform) for platform in REAL_ADAPTERS
        }

    def owners_of(self, local_id: str) -> tuple[str, ...]:
        return tuple(
            platform for platform, adapter in self.adapters.items()
            if adapter.owns_local_id(local_id)
        )

    def test_each_shape_is_claimed_by_exactly_the_expected_platform(self):
        for description, local_id, expected in OWNERSHIP_TABLE:
            with self.subTest(description=description):
                self.assertEqual(
                    self.owners_of(local_id), expected,
                    f"{local_id!r} 的归属必须是 {expected or '谁都不认'}"
                    "（这一格错了的症状是用户被接到别的平台的会话上，不报错）",
                )

    def test_no_local_id_can_be_claimed_by_two_platforms(self):
        """⚠️ 不相交是整套方案的地基，所以它必须是**算出来**的性质，不能靠人肉核对表。

        扫 1~30 位 × 四种字母表：任何形状都不许出现"两家同时认领"。
        """
        alphabets = ("0123456789", "abcdefghijklmnopqrstuvwxyz",
                     "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "aB3")
        for length in range(1, 31):
            for alphabet in alphabets:
                local_id = (alphabet * length)[:length]
                with self.subTest(local_id=local_id):
                    owners = self.owners_of(local_id)
                    self.assertLessEqual(
                        len(owners), 1,
                        f"{local_id!r} 同时被 {owners} 认领 —— 文法不再不相交，"
                        "读侧就会退化成在三家之间仲裁（即猜）",
                    )

    def test_an_adapter_without_a_grammar_owns_nothing(self):
        """没有歧义旧前缀的平台（telegram 等）一条都不许认领。"""
        telegram = TelegramAdapter({"bot_token": "token-not-real"}, hooks=None)
        self.assertIsNone(telegram.local_id_pattern)
        for _description, local_id, _expected in OWNERSHIP_TABLE:
            with self.subTest(local_id=local_id):
                self.assertFalse(telegram.owns_local_id(local_id))

    def test_each_platform_declares_the_shared_legacy_prefix_and_its_own_grammar(self):
        """前缀声明让读侧知道要试哪个键，文法让它知道**该不该**试 —— 两者缺一不可。

        文法比的是**字面量正则**："能匹配上面那张表"对放松不敏感，而放松的代价
        是跨平台串台。
        """
        expected = {
            "slack": r"^[A-Z][A-Z0-9]{5,}$",
            "discord": r"^[0-9]{17,20}$",
            "mattermost": r"^[a-z0-9]{26}$",
        }
        for platform, adapter_class in REAL_ADAPTERS.items():
            with self.subTest(platform=platform):
                self.assertEqual(adapter_class.legacy_conversation_prefix, "channel:")
                self.assertIsNotNone(adapter_class.local_id_pattern)
                self.assertEqual(adapter_class.local_id_pattern.pattern,
                                 expected[platform])


class EveryMountedPlatformGetsItsOwnLegacySessionBack(ConversationStateTestCase):
    """★ 三家同时挂载、三家各自持有 ``channel:`` 会话 ⇒ 全部存活，互不串台。"""

    def test_all_three_legacy_sessions_survive_and_stay_apart(self):
        state = conversation_state_for(
            self.write_state(ALL_LEGACY_SESSIONS),
            ("slack", "discord", "mattermost"),
        )

        for platform, local_id, session_id, _legacy_key in PLATFORM_FIXTURES:
            with self.subTest(platform=platform):
                self.assertEqual(
                    state.get_session(f"{platform}:{local_id}", platform=platform),
                    session_id,
                    "三家同时挂载时每一家都该接回自己的会话 —— 旧方案在这里直接放弃回退",
                )

    def test_no_platform_reads_another_platforms_legacy_session(self):
        """反向钉死：三家都在挂时，谁也读不到别人的那个键。

        这比"读不到"更强：它明确表态**宁可空着**。症状一旦发生就是"slack 的对话
        跑进了 discord 的会话"，不报错、难复现。
        """
        state = conversation_state_for(
            self.write_state(ALL_LEGACY_SESSIONS),
            ("slack", "discord", "mattermost"),
        )

        for asking_platform, local_id, _own_session, _key in PLATFORM_FIXTURES:
            for other_platform, _other_local, other_session, _other_key in PLATFORM_FIXTURES:
                if other_platform == asking_platform:
                    continue
                with self.subTest(asking=asking_platform, foreign=other_platform):
                    found = state.get_session(
                        f"{asking_platform}:{local_id}", platform=asking_platform
                    )
                    self.assertNotEqual(
                        found, other_session,
                        f"{asking_platform} 读到了属于 {other_platform} 的会话"
                        "（不该发生：三家文法不相交，别的形状它本来就认不了）",
                    )

    def test_the_meta_of_a_foreign_shape_is_not_reachable_either(self):
        path = self.write_state({}, meta={
            LEGACY_DISCORD_KEY: {"directory": "/srv/discord"},
            LEGACY_SLACK_KEY: {"directory": "/srv/slack"},
        })
        state = conversation_state_for(path, ("slack", "discord"))

        self.assertIsNone(
            state.get_meta(f"slack:{DISCORD_CHANNEL_ID}", "directory",
                           platform="slack"),
            "meta 同样不许跨平台读到别人的 directory（``/cd`` 会真的把工作目录切过去）",
        )


class OneMountedPlatformMayNotClaimTheOthersKeys(ConversationStateTestCase):
    """★ 只挂一家时也不许越界 —— 旧方案（唯一申报者才回退）正是在这里判错。"""

    def test_slack_alone_reads_back_only_slack_shaped_keys(self):
        """``channel:C01ABC`` 接得回来（必需行为）。"""
        legacy_key = f"channel:{SLACK_SHORTEST_CLAIMABLE_ID}"
        state = conversation_state_for(self.write_state({legacy_key: "ses-slack"}),
                                       ("slack",))

        self.assertEqual(
            state.get_session(f"slack:{SLACK_SHORTEST_CLAIMABLE_ID}",
                              platform="slack"),
            "ses-slack",
        )

    def test_slack_alone_does_not_claim_discords_or_mattermosts_keys(self):
        """⚠️ 旧方案错的就是这一格：slack 是唯一申报者，于是它把 discord 的键认领了。

        而"用户曾经同时跑过 slack 和 discord、后来删掉 discord"这件事**盘上没记过**，
        任何按"当前挂了谁"仲裁的方案都判不了它 —— 划分不依赖那条无法验证的前提。
        """
        state = conversation_state_for(
            self.write_state({LEGACY_DISCORD_KEY: "ses-discord",
                              LEGACY_MATTERMOST_KEY: "ses-mattermost"}),
            ("slack",),
        )

        for asking_local in (SLACK_CHANNEL_ID, DISCORD_CHANNEL_ID,
                             MATTERMOST_CHANNEL_ID):
            with self.subTest(asking_local_id=asking_local):
                self.assertIsNone(
                    state.get_session(f"slack:{asking_local}", platform="slack"),
                    "只挂 slack 时，discord / mattermost 形状的旧键一律不许被认领",
                )

    def test_the_same_holds_with_everyone_mounted(self):
        """挂得越多越不会串：三家都在时 discord 的键也只归 discord。"""
        state = conversation_state_for(
            self.write_state({LEGACY_SLACK_KEY: "ses-slack",
                              LEGACY_DISCORD_KEY: "ses-discord"}),
            ("slack", "discord", "mattermost"),
        )

        self.assertEqual(state.get_session(f"discord:{DISCORD_CHANNEL_ID}",
                                           platform="discord"), "ses-discord")
        self.assertIsNone(
            state.get_session(f"slack:{DISCORD_CHANNEL_ID}", platform="slack"),
            "slack 不许因为是唯一申报者就认领 discord 的键",
        )

    def test_a_key_of_a_platform_that_is_not_mounted_stays_unread(self):
        """discord 键在、discord 没挂 ⇒ 没人认领（要等 discord 挂上才接得回来）。"""
        state = conversation_state_for(
            self.write_state({LEGACY_DISCORD_KEY: "ses-discord"}), ("slack",))

        self.assertIsNone(state.get_session(f"discord:{DISCORD_CHANNEL_ID}",
                                            platform="discord"))


class TheAskingPlatformMustBeThatPlatform(ConversationStateTestCase):
    """归属判定**只看查询方自己**；传错 / 传空 / 没挂载 ⇒ 一律不读。"""

    def test_a_mismatched_asking_platform_reads_nothing(self):
        state = conversation_state_for(self.write_state({LEGACY_SLACK_KEY: "ses-slack"}),
                                       ("slack", "discord"))

        self.assertIsNone(
            state.get_session(f"slack:{SLACK_CHANNEL_ID}", platform="discord"),
            "问方与 id 自称的平台不一致 = 调用点传错了，宁可什么都不读",
        )

    def test_an_empty_asking_platform_reads_nothing(self):
        state = conversation_state_for(self.write_state({LEGACY_SLACK_KEY: "ses-slack"}),
                                       ("slack",))

        for empty in ("", "   ", None):
            with self.subTest(asking_platform=empty):
                self.assertIsNone(
                    state.get_session(f"slack:{SLACK_CHANNEL_ID}", platform=empty),
                    "没有提问方就没有平台有权认领旧键",
                )

    def test_the_legacy_key_itself_is_still_read_exactly(self):
        """旧键按精确键始终读得到 —— 写前收件箱里在途的旧消息要能重放。

        这里**不需要**归属判定：它就是键本身。
        """
        state = conversation_state_for(self.write_state({LEGACY_SLACK_KEY: "ses-slack"}),
                                       ("slack", "discord"))

        self.assertEqual(state.get_session(LEGACY_SLACK_KEY, platform="slack"),
                         "ses-slack")
        self.assertEqual(state.get_session(LEGACY_SLACK_KEY, platform="discord"),
                         "ses-slack", "精确键与归属判定无关：读到的就是那一条")

    def test_meta_goes_through_the_same_partition(self):
        """``/cd`` 记的 directory 也在 meta 里，回退受同一条划分约束。"""
        path = self.write_state(
            {},
            meta={LEGACY_SLACK_KEY: {"directory": "/srv/slack"},
                  LEGACY_DISCORD_KEY: {"directory": "/srv/discord"}},
        )
        state = conversation_state_for(path, ("slack", "discord"))

        self.assertEqual(
            state.get_meta(f"slack:{SLACK_CHANNEL_ID}", "directory",
                           platform="slack"),
            "/srv/slack",
        )
        self.assertEqual(
            state.get_meta(f"discord:{DISCORD_CHANNEL_ID}", "directory",
                           platform="discord"),
            "/srv/discord",
        )


class WritesAndDropsStayInsideThePartition(ConversationStateTestCase):
    """写只落新键；删除只删**本平台文法覆盖得到**的候选键。"""

    def test_writes_only_land_on_the_new_format_key(self):
        state = conversation_state_for(self.write_state({LEGACY_SLACK_KEY: "ses-old"}),
                                       ("slack", "discord"))

        state.set_session(f"slack:{SLACK_CHANNEL_ID}", "ses-new")
        state.set_meta(f"slack:{SLACK_CHANNEL_ID}", "directory", "/srv/app")

        self.assertEqual(self.sessions_on_disk(),
                         {LEGACY_SLACK_KEY: "ses-old",
                          f"slack:{SLACK_CHANNEL_ID}": "ses-new"},
                         "歧义旧键永不迁移（改键名是 state.py 的职责，它只捕获不归一）")

    def test_dropping_removes_the_new_key_and_the_own_legacy_key(self):
        """/new 必须把旧键一起删，否则下一次读取又认领回**已在服务端删掉**的会话。"""
        state = conversation_state_for(
            self.write_state({LEGACY_SLACK_KEY: "ses-slack",
                              f"slack:{SLACK_CHANNEL_ID}": "ses-slack"}),
            ("slack", "discord"),
        )

        state.drop_session(f"slack:{SLACK_CHANNEL_ID}", platform="slack")

        self.assertIsNone(state.get_session(f"slack:{SLACK_CHANNEL_ID}",
                                            platform="slack"))
        self.assertEqual(self.sessions_on_disk(), {},
                         "新键与本平台的旧键都必须删掉")

    def test_dropping_never_touches_another_platforms_legacy_key(self):
        """删 slack 的会话只删**属于 slack 的**那个旧键；discord 的一个字都不许动
        （它是用户唯一的回滚路径）。"""
        sessions = {LEGACY_SLACK_KEY: "ses-slack",
                    LEGACY_DISCORD_KEY: "ses-discord",
                    LEGACY_MATTERMOST_KEY: "ses-mattermost",
                    f"slack:{SLACK_CHANNEL_ID}": "ses-slack"}
        state = conversation_state_for(self.write_state(sessions), ("slack", "discord"))

        state.drop_session(f"slack:{SLACK_CHANNEL_ID}", platform="slack")

        self.assertEqual(
            self.sessions_on_disk(),
            {LEGACY_DISCORD_KEY: "ses-discord",
             LEGACY_MATTERMOST_KEY: "ses-mattermost"},
            "新键与 slack 的旧键都删掉，别家（和没挂载的 mattermost）的旧键留着",
        )

    def test_dropping_with_no_asking_platform_only_removes_the_exact_key(self):
        state = conversation_state_for(
            self.write_state({LEGACY_SLACK_KEY: "ses-slack",
                              f"slack:{SLACK_CHANNEL_ID}": "ses-slack"}),
            ("slack",),
        )

        state.drop_session(f"slack:{SLACK_CHANNEL_ID}", platform="")

        self.assertEqual(self.sessions_on_disk(), {LEGACY_SLACK_KEY: "ses-slack"},
                         "没有提问方就删不了候选键：宁可不删，也不碰归属不明的键")


class NothingIsRewrittenOnDisk(ConversationStateTestCase):
    """歧义键**永不迁移**：盘上逐字节不变，且零备份。"""

    def test_channel_only_file_is_byte_identical_after_both_loads(self):
        self.write_state({LEGACY_SLACK_KEY: "ses-slack"})
        before = self.read_bytes()

        StateStore(self.path, migrate_keys=True)
        after_first = self.read_bytes()
        for _ in range(2):                       # 幂等：再加载两次
            store = StateStore(self.path, migrate_keys=True)
            self.assertEqual(store.last_migration.migrated, 0)
            self.assertEqual(store.last_migration.ambiguous_kept, 1)
            self.assertFalse(store.last_migration.changed)

        self.assertEqual(after_first, before)
        self.assertEqual(self.read_bytes(), before)
        self.assertEqual(self.backup_files(), [], "零迁移就不该有备份")

    def test_reading_through_the_partition_does_not_touch_the_file(self):
        """回退读是**纯读**：连一次落盘都不该发生。"""
        self.write_state(ALL_LEGACY_SESSIONS)
        before = self.read_bytes()
        state = conversation_state_for(self.path,
                                       ("slack", "discord", "mattermost"))

        state.get_session(f"slack:{SLACK_CHANNEL_ID}", platform="slack")
        state.get_session(f"discord:{DISCORD_CHANNEL_ID}", platform="discord")
        state.get_meta(f"mattermost:{MATTERMOST_CHANNEL_ID}", "directory",
                       platform="mattermost")

        self.assertEqual(self.read_bytes(), before)
        self.assertEqual(self.backup_files(), [])

    def test_step_one_migration_behaviour_is_unchanged(self):
        """``chat:`` / ``room:`` 仍然迁移（无歧义），``channel:`` 仍然永不迁移。"""
        self.write_state(
            {"chat:55": "ses-telegram", "room:!abc:example.org": "ses-matrix",
             LEGACY_SLACK_KEY: "ses-slack"},
            meta={"room:!abc:example.org": {"directory": "docs"}},
        )
        store = StateStore(self.path, migrate_keys=True)

        self.assertEqual(store.last_migration.migrated, 2)
        self.assertEqual(store.last_migration.ambiguous_kept, 1)
        on_disk = json.loads(self.read_bytes().decode("utf-8"))
        self.assertEqual(on_disk["sessions"],
                         {"telegram:55": "ses-telegram",
                          "matrix:!abc:example.org": "ses-matrix",
                          LEGACY_SLACK_KEY: "ses-slack"})
        # meta 只在**原来就有 meta** 的键上落条目；歧义键没有 meta，也就没有新条目。
        self.assertEqual(on_disk["meta"],
                         {"matrix:!abc:example.org": {"directory": "docs"}})


class BridgeCoreReusesTheRightLegacySession(ConversationStateTestCase):
    """端到端走真 :class:`~opencode_bridge.core.BridgeCore`。"""

    def core_with(self, mounted: tuple[str, ...]):
        state = StateStore(self.write_state(ALL_LEGACY_SESSIONS), migrate_keys=True)
        client = FakeClient()
        core = BridgeCore(Config(), client, state)
        for platform in mounted:
            core.attach(ChannelPlatformStub(platform))
        self.addCleanup(core.stop)
        return core, client

    def send(self, core, platform: str, local_id: str, text: str = "接着说") -> None:
        core.on_inbound(Inbound(conversation_id=f"{platform}:{local_id}",
                                text=text, platform=platform))

    def test_all_three_platforms_reuse_their_own_legacy_session(self):
        """★ 本步的主目标：三家同时挂载，三份历史会话**全部存活**。"""
        core, client = self.core_with(("slack", "discord", "mattermost"))

        for platform, local_id, _session_id, _legacy_key in PLATFORM_FIXTURES:
            with self.subTest(platform=platform):
                self.send(core, platform, local_id)

        self.assertEqual(client.create_attempts, [],
                         "旧会话接回来了就不许再新建（否则用户会看到两份会话）")
        self.assertEqual(
            client.prompts,
            [("ses-slack", "接着说"), ("ses-discord", "接着说"),
             ("ses-mattermost", "接着说")],
            "每条消息必须回到**自己**的会话",
        )

    def test_a_cross_platform_conversation_id_does_not_borrow_a_session(self):
        """⚠️ 静默串台的反向断言：slack 的会话 id 长得像 discord 的，也不许借用。"""
        core, client = self.core_with(("slack", "discord", "mattermost"))

        self.send(core, "slack", DISCORD_CHANNEL_ID)

        self.assertEqual(len(client.create_attempts), 1,
                         "形状不属于 slack ⇒ 读不到旧键 ⇒ 必须新建一个会话")
        self.assertNotEqual(client.created_ids[0], "ses-discord")
        self.assertEqual(client.prompts, [(client.created_ids[0], "接着说")])

    def test_slack_alone_still_recovers_its_own_legacy_session(self):
        core, client = self.core_with(("slack",))

        self.send(core, "slack", SLACK_CHANNEL_ID)

        self.assertEqual(client.create_attempts, [])
        self.assertEqual(client.prompts, [("ses-slack", "接着说")])

    def test_new_command_drops_the_legacy_key_and_starts_over(self):
        """``/new``：删掉本平台的旧键 + 新建会话；别人的旧键一个字都不动。"""
        core, client = self.core_with(("slack", "discord"))

        core.on_inbound(Inbound(conversation_id=f"slack:{SLACK_CHANNEL_ID}",
                                text="/new", platform="slack"))

        self.assertEqual(len(client.create_attempts), 1)
        self.assertNotEqual(client.created_ids[0], "ses-slack")
        self.assertEqual(client.deleted, ["ses-slack"], "先删服务端那个再重建")
        self.assertEqual(
            self.sessions_on_disk(),
            {LEGACY_DISCORD_KEY: "ses-discord",
             LEGACY_MATTERMOST_KEY: "ses-mattermost",
             f"slack:{SLACK_CHANNEL_ID}": client.created_ids[0]},
            "slack 的旧键必须删；discord 与 mattermost 的旧键必须留",
        )

    def test_slack_alone_does_not_borrow_the_discord_session(self):
        """★ 旧方案判错的那一格，端到端再钉一次：只挂 slack 时不许借用 discord 的键。"""
        core, client = self.core_with(("slack",))

        self.send(core, "slack", DISCORD_CHANNEL_ID)

        self.assertEqual(len(client.create_attempts), 1,
                         "discord 的键读不到 ⇒ 必须新建一个会话，而不是借用")
        self.assertNotIn("ses-discord", client.created_ids)
        self.assertEqual(
            self.sessions_on_disk(),
            {**ALL_LEGACY_SESSIONS, f"slack:{DISCORD_CHANNEL_ID}": client.created_ids[0]},
            "三个旧键都原样留着（用户唯一的回滚路径），新写的只落新键",
        )


class RoutingStillResolvesBothForms(ConversationStateTestCase):
    """路由：旧键（收件箱里在途的消息）与新键都必须找得到对应适配器。

    ⚠️ 旧键走的是**既有**启发式（纯数字 → discord、其余 → slack），本步一个字没改
    —— 它判的是"回复该发给谁"，与归属判定是两件事，也不该混。
    """

    def test_new_ids_route_by_platform_and_legacy_ids_by_the_existing_heuristic(self):
        core = BridgeCore(Config(), FakeClient(),
                          StateStore(os.path.join(self.directory, "state.json")))
        for platform in ("slack", "discord", "mattermost", "telegram", "matrix"):
            core.attach(ChannelPlatformStub(platform))

        def routed_to(conversation_id: str) -> str | None:
            adapter = core._adapter_for(conversation_id)
            return adapter.name if adapter is not None else None

        for platform, local_id, _session_id, _legacy_key in PLATFORM_FIXTURES:
            with self.subTest(platform=platform):
                self.assertEqual(routed_to(f"{platform}:{local_id}"), platform,
                                 "新格式：平台段本身就是答案，不需要猜")
        self.assertEqual(routed_to("telegram:55"), "telegram")
        self.assertEqual(routed_to("matrix:!abc:example.org"), "matrix")
        # 旧格式仍按既有启发式（见本类 docstring）
        self.assertEqual(routed_to("channel:C1"), "slack")
        self.assertEqual(routed_to("channel:123456789012345678"), "discord")


class StateLookupIsUntouched(unittest.TestCase):
    """``state.py`` 的兼容读**一字未改**：这一步没碰它，只是不许悄悄改掉。"""

    def test_unambiguous_alias_still_falls_back(self):
        mapping = {"telegram:55": "ses-a"}
        self.assertEqual(_lookup(mapping, "telegram:55"), "ses-a")
        self.assertEqual(_lookup(mapping, "chat:55"), "ses-a",
                         "无歧义别名回退仍在（telegram 刚切完前缀，窗口期需要它）")
        self.assertEqual(_lookup({"matrix:!a:o": "ses-b"}, "room:!a:o"), "ses-b")

    def test_exact_key_always_wins_over_the_alias(self):
        mapping = {"chat:55": "ses-legacy", "telegram:55": "ses-modern"}
        self.assertEqual(_lookup(mapping, "chat:55"), "ses-legacy")

    def test_ambiguous_channel_key_has_no_alias(self):
        """歧义键**没有**别名可回退 —— 它就是"不知道"。

        读侧那条兼容路径在 :class:`ConversationState`（带归属判定），
        ``StateStore`` 自己绝不把 ``channel:`` 当成 slack 的键。
        """
        self.assertIsNone(_legacy_alias(LEGACY_SLACK_KEY))
        self.assertEqual(_classify_key(LEGACY_SLACK_KEY), ("ambiguous", None))
        mapping = {LEGACY_SLACK_KEY: "ses-slack"}
        self.assertEqual(_lookup(mapping, LEGACY_SLACK_KEY), "ses-slack",
                         "精确键仍读得到（未投递的旧消息要能重放）")
        self.assertIsNone(_lookup(mapping, f"slack:{SLACK_CHANNEL_ID}"),
                          "state.py 自己绝不把 channel: 当成 slack 的键")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

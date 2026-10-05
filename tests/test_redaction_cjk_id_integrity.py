"""⛔ 反向闸：真的含 CJK 的 local-id 必须**完整**被摘要，一个字符都不许少。

这是防「修法过于激进」的**主闸**。缺陷与修法见
:mod:`tests.redaction_cjk_tail_support` 的模块 docstring。

为什么需要它
============

``tasks.md`` 那个缺陷记的修法方向里有一条明确的警告：⛔ **不能**「排除所有非
ASCII 字符」—— 那会把真的含 CJK 的 local-id 切碎，后果**比吞尾注更糟**（``conv#``
对不上 :attr:`~opencode_bridge.hooks.Inbound.conversation_id`，排障时两边对不上号）。

真的含 CJK 的 local-id 有哪些
=============================

| 平台 | local-id | 依据 |
|---|---|---|
| ``ntfy`` | ``topic`` | **用户自取的名字**：``ntfy.py`` 类 docstring 第 (b) 条明写「用户自己命名的 topic、知道它是什么」；代码按 ``urllib.parse.quote(self.topic, safe="")`` 编码（``ntfy.py:156``），说明作者预期它含 URL 不安全字符。⚠️ 上游 ntfy 自己把 topic 限死在 ``[-_A-Za-z0-9]{1,64}``（见 ``docs`` 里那条），但**本仓库不校验**，自建服务端也无此保证 ⇒ 桥这一侧必须能处理。 |
| ``a2a`` | 对端名 | ``peer_tokens`` 里的自由文本：``a2a._parse_peer_tokens`` 只按 ``,`` 切、按第一个 ``:`` 分 name/token，对 name **不校验字符集**。 |
| ``irc`` | 频道 / nick | RFC 2812 的 ``channel_char`` 不含非 ASCII，但 UTF-8 网络上的频道名实践上可以是中文；本仓库 ``irc._conversation_id`` 原样拼 ``target``。 |
| ``email`` | 发件人地址 | SMTPUTF8（RFC 6531）允许 UTF-8 本地部分。 |

⇒ 「排除标点、保留文字」这条线对上面每一个都成立：它们的名字里可以有汉字，
但不会因为名字里有汉字就多出括号或冒号。

幂等性也在这一组
================

``(?![a-z]+#)`` 是幂等性的承重项。加了排除集之后，一条尾巴带全角标点的输出
（``telegram:conv#xxxx（判据）``）必须**不再**被匹配 —— 否则
``conv#xxxx`` 的 ``xxxx`` 会被再摘要一次，日志里出现两个不同的假摘要。
"""

from __future__ import annotations

import unittest

from opencode_bridge import identity
from tests.redaction_cjk_tail_support import (
    ALL_PLATFORMS,
    GENUINE_CJK_ID_CASES,
    PLATFORM_SHAPES,
    assert_digest_shape,
    expected_tail_note_output,
    make_redactor,
)


class GenuineCjkIdentifierTests(unittest.TestCase):
    """含 CJK 的真 local-id 必须**完整**被摘要，尾注逐字节留下。"""

    def setUp(self) -> None:
        self.redactor = make_redactor()

    def test_a_cjk_local_id_is_still_digested_whole(self):
        """``ntfy:我的话题（备注）`` 的摘要必须等于**只**对 ``我的话题`` 求的摘要。

        ⛔ 只断言「尾注还在」的那版，在「排除所有非 ASCII」的修法下**照样全绿**
        （尾注确实在，而 id 只剩一半）—— 所以这里是逐字节等式。
        """
        for text, platform, local_id, tail_note in GENUINE_CJK_ID_CASES:
            with self.subTest(text=text):
                digest = self.redactor.fingerprint("conv", local_id)
                assert_digest_shape(self, digest)

                scrubbed = self.redactor.scrub(text)

                self.assertEqual(
                    scrubbed, expected_tail_note_output(self.redactor, platform, local_id, tail_note),
                )
                # 摘要必须是**只**对 local_id 求的：把 local_id 改一个字就该变。
                self.assertNotEqual(
                    self.redactor.fingerprint("conv", local_id + "字"), digest,
                )

    def test_a_topic_that_is_pure_chinese_is_digested_whole(self):
        """没有尾注时也必须整条摘要 —— 那是 ntfy 的日常形态。"""
        self.assertEqual(
            self.redactor.scrub("ntfy:我的话题"),
            expected_tail_note_output(self.redactor, "ntfy", "我的话题", ""),
        )

    def test_the_ntfy_allowlist_rejection_line_keeps_its_topic_whole(self):
        """``ntfy.py`` 里那条「非白名单 topic」日志的**真实渲染**。

        它的模板是 ``ntfy: dropping message from non-whitelisted topic %s``
        （行尾无尾注），所以这条守的是**另一件事**：topic 里的中文不能被当成尾注切掉。
        """
        rendered = "ntfy: dropping message from non-whitelisted topic ntfy:我的话题"

        self.assertEqual(
            self.redactor.scrub(rendered),
            "ntfy: dropping message from non-whitelisted topic "
            + expected_tail_note_output(self.redactor, "ntfy", "我的话题", ""),
        )


class ThirteenPlatformShapesTests(unittest.TestCase):
    """13 个平台的 local-id 真实形状（清单见 ``_redactable_ids.py`` 的 docstring）逐字节不变。

    ⚠️ 这是**回归闸**：改动前就全绿，改动后必须仍然全绿。
    """

    def setUp(self) -> None:
        self.redactor = make_redactor()

    def test_every_platform_shape_is_still_digested_whole(self):
        for text, platform, local_id in PLATFORM_SHAPES:
            with self.subTest(text=text):
                self.assertEqual(
                    self.redactor.scrub(text),
                    expected_tail_note_output(self.redactor, platform, local_id, ""),
                )

    def test_every_registered_adapter_platform_is_covered_by_this_table(self):
        """清单不许比「已注册适配器的平台清单」少一个。

        ⚠️ 参照物是 :data:`tests.inbound_log_support.ALL_PLATFORMS`（13 个**已落地**
        适配器的 ``name``），**不是** :data:`identity.KNOWN_PLATFORMS` ——
        后者还含 5 个「路线图 B 波次、适配器尚未落地」的平台（feishu / wecom /
        dingtalk / wechat / nostr），那些没有 local-id 形状可列。
        """
        self.assertEqual(
            sorted({platform for _, platform, _ in PLATFORM_SHAPES}), sorted(ALL_PLATFORMS),
        )
        # 那 5 个确实还没有适配器 —— 所以「清单里没有它们」是有依据的，不是漏了。
        self.assertEqual(
            sorted(identity.KNOWN_PLATFORMS - set(ALL_PLATFORMS)),
            ["dingtalk", "feishu", "nostr", "wechat", "wecom"],
        )


if __name__ == "__main__":
    unittest.main()

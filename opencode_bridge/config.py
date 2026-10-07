"""Configuration loading for opencode-bridge (Lane A).

Search order (``Config.load``):

1. explicit ``path`` argument
2. ``$OPENCODE_BRIDGE_CONFIG``
3. ``./config.json`` (current working directory)

If no candidate exists the defaults are returned (never raises).
Environment overrides: ``OPENCODE_URL`` / ``OPENCODE_PASSWORD`` /
``OPENCODE_DIRECTORY``.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Final

from .pairing import CONFIG_VERSION_KEY, PAIRING_SECRET_KEY

__all__ = [
    "DEFAULT_CONFIG_NAME",
    "ADAPTER_SCOPED_TOP_LEVEL_KEYS",
    "Config",
    "adapter_scoped_config",
]

logger = logging.getLogger("opencode_bridge.config")

DEFAULT_CONFIG_NAME = "config.json"

_KNOWN_KEYS = frozenset(
    {
        "opencode_url",
        "opencode_password",
        "opencode_directory",
        "opencode_agent",
        "adapters",
        "permissions_mode",
        "log_level",
        "state_path",
        "bridge",
        PAIRING_SECRET_KEY,
        CONFIG_VERSION_KEY,
    }
)

#: 顶层标量里**必须**传进每个适配器子树的那些。
#:
#: ⚠️ 存在的理由很具体：**适配器只看得到自己的子树**
#: （``adapters.telegram`` 那一个 dict），而 :attr:`Adapter.config` 就是它 ——
#: ``resolve_allowlist(self.config)`` 读的正是它。所以顶层键如果不投影进去，
#: 闸门就永远读不到。
#:
#: 用**投影**（把顶层标量并进子树）而不是给 :func:`~opencode_bridge.adapters.base.
#: build` 加新参数，是因为本仓库既有做法就是"适配器的配置全在那个 dict 里"，
#: 且 ``--status`` 那条路径也**自己构造适配器**（``__main__._channel_config_rows``）：
#: 两个构造点投影同一份，闸门与状态视图才不会各说各话。
ADAPTER_SCOPED_TOP_LEVEL_KEYS: Final[tuple[str, ...]] = (
    PAIRING_SECRET_KEY,
    CONFIG_VERSION_KEY,
)

#: Defaults for the optional ``bridge`` sub-section (Lane C).  The values are
#: merged with whatever the config file provides, so a partial ``bridge``
#: object such as ``{"edit_interval_seconds": 0.5}`` still yields a complete
#: mapping.  Invalid value types fall back to the default with a warning.
_BRIDGE_DEFAULTS: dict[str, Any] = {
    "edit_interval_seconds": 1.5,
    "max_message_chars": 4000,
    # C3 ①：入站**长输入回执**的字数门槛。0 = 不回执。
    # 180 抄自 dsh 的 ``longInputAckChars``（快照
    # ``zhuiyueya-dsh-im-gateway-8a5edab282632443.txt`` 的 ``src/core/config.ts``），
    # 但只用于**不能改写已发消息**的平台 —— 能改写的那些本来就有 ``⏳ 处理中…``，
    # 再回一句只是多一条消息（见 ``channel_profile.ChannelProfile._progress_visibility``）。
    "long_input_ack_chars": 180,
    # C3 ②：敲了 ``..`` 之后等下一行的**保险丝**上限（秒）。
    # ⚠️ 它**不是**合并窗口：没有 ``..`` 的消息根本不会起计时器，所以普通消息的
    # 额外延迟可证明是 0。它只是"敲了 ``..`` 然后走开"那条消息不至于永远卡住。
    # 给到 15s（而不是 dsh 的 5s）是因为人打完一行再发出来要好几秒，而这条保险丝
    # 只对少数人有用，多等一会儿对谁都无感。
    # **配 0 = 立即返回、原样使用、不改写**（不会被改写成 15）⇒ 续行缓冲不再装
    # 计时器 ⇒ 那条消息一直等到下一条非 ``..`` 行为止。
    # ⚠️ **本文档的措辞因此改成上面那句**：原先写的「0 = 关掉保险丝（不推荐：
    # 那条消息会丢）」后半句**不准确** —— 缓冲并不会把它丢掉，它只是**一直留着**
    # （见 ``tests/test_inbound_gateway.MergeFuseTimeoutConfigurationTests``）。
    # ⛔ **配 0 并不比默认更安全**：缓冲是**纯内存**，而 ``ConversationMerger`` 的
    # ``flush`` / ``stop`` / ``held_conversation_ids`` **生产零调用点** ⇒ 关停不排空、
    # 重启恢复不到 ⇒ G2 那个丢消息窗口由「≤ 15 秒、**有界**」变成「**无界**」
    # ⇒ 文档措辞正确（关掉保险丝就是关掉保险丝）**不等于 0 更安全**。
    # 显式配 0 时 ``inbound_gateway`` 会另发一条点名该键的 WARNING。
    "merge_continue_timeout_seconds": 15.0,
}

#: ``bridge`` 段里要取整的键。``0`` 对这两个键都是**合法值**（= 关掉该功能），
#: 所以取整后只挡负数，不像 ``max_message_chars`` 那样要求至少 1。
_BRIDGE_INTEGER_KEYS = frozenset(
    {"max_message_chars", "long_input_ack_chars"}
)

_ENV_OVERRIDES: dict[str, str] = {
    "OPENCODE_URL": "opencode_url",
    "OPENCODE_PASSWORD": "opencode_password",
    "OPENCODE_DIRECTORY": "opencode_directory",
}

_STR_FIELDS = frozenset(
    {
        "opencode_url",
        "opencode_password",
        "opencode_directory",
        "opencode_agent",
        "permissions_mode",
        "log_level",
        "state_path",
        PAIRING_SECRET_KEY,
    }
)


@dataclass
class Config:
    opencode_url: str = ""  # "" => auto-discover
    opencode_password: str = ""  # "" => auto-discover
    opencode_directory: str = "."  # session working directory
    opencode_agent: str = ""  # "" => server default
    adapters: dict = field(default_factory=dict)  # {"telegram": {...}, ...}
    permissions_mode: str = "ask"  # "ask" | "allow" | "deny"
    log_level: str = "INFO"
    state_path: str = "state.json"
    bridge: dict = field(
        default_factory=lambda: dict(_BRIDGE_DEFAULTS)
    )  # {"edit_interval_seconds": 1.5, "max_message_chars": 4000,
        #     "long_input_ack_chars": 180, "merge_continue_timeout_seconds": 15.0}
    #: 配对码的派生密钥。**空 = 不提供配对**（绝不是"用空串派生"，见
    #: :mod:`opencode_bridge.pairing`）。
    #:
    #: ⛔ **刻意不复用任何既有键**：
    #:
    #: * ``permissions_mode`` 是 ``"ask"|"allow"|"deny"`` 三值枚举、README 公开
    #:   写着 ⇒ 从它派生的码对读过 README 的人**可枚举**；
    #: * ``opencode_password`` 默认 ``""``（auto-discover），**那是常态不是边缘**；
    #: * 凭据类（``bot_token`` 等）会让码依赖"哪个适配器发的"，而轮换 token 会用
    #:   用户看不懂的方式杀掉在途码。
    #:
    #: ⛔ **不自动生成、也不回写** —— 那会让每次启动都依赖一个配置写者。
    pairing_secret: str = ""
    #: 配置格式版本。**0 / 缺失 = 这个文件早于「空 = 全拒」那次翻转** ⇒ 保持旧的
    #: 开放语义；``>= 2`` ⇒ 采新语义（空 = 谁都不放行）。
    #:
    #: 判定只有一处：:func:`opencode_bridge.pairing.empty_allowlist_is_open`。
    config_version: int = 0
    #: **本实例实际加载自哪个文件**（绝对路径）；**空 = 没找到任何配置文件**
    #: （:meth:`load` 的搜索链全部落空，走的是内置默认值）。
    #:
    #: **为什么必须记在实例上**（实测缺陷，2026-10-06）：
    #: ``python -m opencode_bridge --config /tmp/other.json --setup --json`` 加载时
    #: **确实用了** ``--config``，但它报出的 ``config_path`` 是**仓库里的
    #: ``config.json``** —— 因为报告路径的那段代码**自己重写了一遍搜索链**
    #: （``__main._config_file_in_use`` 只查环境变量与 cwd）。
    #: ⇒ **根因是「解析出来的路径从未被记录」**，任何消费者都只能自己重算一遍，
    #: 于是**第二份实现就出现了**（``commands._config_path_hint`` 就是）。
    source_path: str = ""

    # ------------------------------------------------------------------
    # loading
    # ------------------------------------------------------------------
    @classmethod
    def load(cls, path: str | None = None) -> "Config":
        """Build a :class:`Config`.

        Missing files are never an error: the search chain simply falls
        through to the next candidate and finally to the defaults.
        """
        cfg = cls()
        data: dict[str, Any] = {}

        candidates: list[tuple[str, bool]] = []
        if path is not None:
            candidates.append((path, True))
        env_path = os.environ.get("OPENCODE_BRIDGE_CONFIG")
        if env_path:
            candidates.append((env_path, True))
        candidates.append((os.path.join(os.getcwd(), DEFAULT_CONFIG_NAME), False))

        chosen: str | None = None
        for candidate, explicit in candidates:
            if candidate and os.path.isfile(candidate):
                chosen = candidate
                break
            if explicit:
                logger.warning("config file not found: %s", candidate)

        if chosen is not None:
            data = cls._read_file(chosen)
        # ⚠️ 记下**实际加载自哪个文件**。缺了这一句，任何消费者都得自己重算一遍搜索链
        # —— 而第二份实现真的出现了（`__main._config_file_in_use`），于是
        # `--config X --setup --json` 会报出另一个文件（实测，2026-10-06）。
        cfg.source_path = os.path.abspath(chosen) if chosen else ""

        for key, value in data.items():
            if key not in _KNOWN_KEYS:
                logger.warning("ignoring unknown config key: %r", key)
                continue
            if not cls._coerce_ok(key, value):
                logger.warning(
                    "ignoring config key %r with invalid value type %s",
                    key,
                    type(value).__name__,
                )
                continue
            setattr(cfg, key, value)

        for env_name, attr in _ENV_OVERRIDES.items():
            env_value = os.environ.get(env_name)
            if env_value:
                setattr(cfg, attr, env_value)

        # ``bridge`` is a nested sub-section: fill in missing defaults.
        cfg.bridge = cls._merge_bridge(cfg.bridge)

        return cfg

    @staticmethod
    def _merge_bridge(raw: Any) -> dict[str, Any]:
        """Merge the ``bridge`` config section over :data:`_BRIDGE_DEFAULTS`."""
        merged = dict(_BRIDGE_DEFAULTS)
        if not isinstance(raw, dict):
            if raw is not None:
                logger.warning(
                    "config key 'bridge' must be a JSON object, got %s",
                    type(raw).__name__,
                )
            return merged
        for key, value in raw.items():
            if key not in merged:
                logger.warning("ignoring unknown bridge config key: %r", key)
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                logger.warning(
                    "ignoring bridge config key %r with non-numeric value %r",
                    key,
                    value,
                )
                continue
            if number < 0 or number != number:  # negative or NaN
                logger.warning(
                    "ignoring bridge config key %r with invalid value %r", key, value
                )
                continue
            if key == "max_message_chars":
                number = int(round(number))
                if number < 1:
                    logger.warning(
                        "ignoring bridge config key %r with invalid value %r",
                        key,
                        value,
                    )
                    continue
            elif key in _BRIDGE_INTEGER_KEYS:
                # 取整即可：0 对这两个键是"关掉"，所以不能套用上面那个 ``>= 1``。
                number = int(round(number))
            merged[key] = number
        return merged

    @staticmethod
    def _read_file(path: str) -> dict[str, Any]:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except OSError as exc:
            logger.warning("cannot read config file %s: %s", path, exc)
            return {}
        except ValueError as exc:
            logger.warning("invalid JSON in config file %s: %s", path, exc)
            return {}
        if not isinstance(raw, dict):
            logger.warning("config file %s must contain a JSON object", path)
            return {}
        return raw

    @staticmethod
    def _coerce_ok(key: str, value: Any) -> bool:
        if key == "adapters":
            return isinstance(value, dict)
        if key == "bridge":
            return isinstance(value, dict)
        if key == CONFIG_VERSION_KEY:
            # ``bool`` 不是版本号（``True`` 会变成 1 = "旧语义"，纯属误导）。
            return isinstance(value, int) and not isinstance(value, bool)
        if key in _STR_FIELDS:
            return isinstance(value, str)
        return True

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def adapter_scoped_config(cfg: "Config", entry: object) -> dict:
    """把 :data:`ADAPTER_SCOPED_TOP_LEVEL_KEYS` 投影进某个适配器的子树。

    纯函数；**不改动**传入的 ``entry``（调用方的 ``cfg.adapters`` 原样保留，
    否则 ``--status`` 读到的就不再是用户写的那份配置了）。

    ⚠️ **顶层一律胜出**：子树里同名且**值不同**的键会被覆盖并记一条 warning。
    授权配置里一个"看着生效了其实没生效"的键，比没有这个键危险得多
    （与 :mod:`opencode_bridge.allowlist` 的键冲突上报同一条纪律）。

    ⚠️ **两个构造点都必须过这个函数** —— ``run_bridge`` 真正建适配器，
    ``_channel_config_rows`` 为 ``--status`` 建。漏一个，状态视图就会在一个
    适配器都没配过的桥上报出另一套语义。
    """
    subtree = dict(entry) if isinstance(entry, dict) else {}
    for key in ADAPTER_SCOPED_TOP_LEVEL_KEYS:
        value = getattr(cfg, key, None)
        if key in subtree and subtree.get(key) != value:
            logger.warning(
                "adapters 里的 %r 被**顶层同名键覆盖**（顶层 = %r）—— 删掉子树里那个。",
                key,
                value,
            )
        subtree[key] = value
    return subtree

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
from typing import Any

__all__ = ["DEFAULT_CONFIG_NAME", "Config"]

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
    }
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
    # 只对少数人有用，多等一会儿对谁都无感。0 = 关掉保险丝（不推荐：那条消息会丢）。
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
        if key in _STR_FIELDS:
            return isinstance(value, str)
        return True

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

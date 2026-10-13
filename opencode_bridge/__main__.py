"""Lane C — CLI entry point: ``python -m opencode_bridge``.

Usage::

    python -m opencode_bridge [--config PATH] [--verbose] [--check]
    python -m opencode_bridge --setup [平台] [--json]

* ``--check`` only resolves the endpoint and calls ``GET /api/info`` — it
  never creates a session and never starts an adapter.
* ``--setup`` prints the frozen per-platform onboarding copy and exits: it
  contacts nothing and needs no ``bot_token``, so it also works *before* the
  bridge is configured. It is the single source of truth behind both the
  in-bot ``/setup`` command and the ``bridge_setup`` plugin tool inside OpenCode.
* The normal run builds every configured adapter, starts the SSE reader and
  blocks until ``Ctrl+C``; shutdown always goes through ``BridgeCore.stop()``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from typing import Mapping, Sequence

from .adapters import build
from .allowlist import resolve_allowlist
from .config import Config, DEFAULT_CONFIG_NAME, adapter_scoped_config
from .core import BridgeCore, setup_platforms, setup_reply
from .diagnostics import ProcessDiagnostics, describe_environment
from . import credential_health
from . import health
from .held_buffer_store import HELD_BUFFER_FILE_NAME, HeldBufferStore
from .instance_lock import InstanceLock, pid_is_alive
from .inbox import InboundInbox
from .opencode_client import OpenCodeClient, discover_endpoint
from .outbound import install_outbound_failure_recorder
from .pairing import empty_allowlist_is_open
from .pairing_cli import run_pair
from .redaction import install_redaction_filter
from .state import StateStore
from .subscription_health import (
    SubscriptionHealthRecorder,
    install_subscription_health_recorder,
    read_subscription_status,
)
from .subscription_status_view import render_subscription_status
from .subscription_supervisor import SubscriptionStatus
from .config_readiness import (
    _NOT_READY_MISSING_CREDENTIALS,
    _NOT_READY_NO_ALLOWLIST,
    _config_file_in_use,
    _has_configured_adapter,
    _missing_required_keys,
    _not_ready_reasons,
    _readiness_verdict,
    _status_platform_keys,
    _token_present,
)
from .startup_probe_view import NO_START_PROBE_TEXT, _print_last_start_probes
from .status_failure_view import (
    NO_CREDENTIAL_FAILURE_TEXT,
    NO_OUTBOUND_FAILURE_TEXT,
    _CREDENTIAL_FAILURE_SECTION_HEADER,
    _OUTBOUND_SECTION_HEADER,
    _print_last_outbound_failures,
    _print_last_runtime_credential_failures,
)
from .text_width import _dwidth, _pad
# 第七刀拆分（2026-10-14）：以下名字已逐字移植进新模块，此处再进口
# 保住既有 import 路径与 ``cli.X`` 属性语义；行为零变化。
from .bridge_runner import (
    ALREADY_RUNNING_MESSAGE,
    NO_ADAPTER_MESSAGE,
    REFUSAL_STAGE_NO_USABLE_ADAPTER,
    REFUSAL_STAGE_PREFLIGHT,
    _REFUSAL_REASON_BY_STAGE,
    _bridge_dir,
    _held_buffer_path,
    _inbox_path,
    _record_bridge_refusal,
    _run_bridge_locked,
    run_bridge,
)
from .platform_status import (
    _NullHooks,
    _adapter_capabilities,
    _channel_config_rows,
    _pid_alive,
    _print_event_subscription,
    _runtime_state,
    run_check,
    run_status,
)
from .setup_view import (
    _platform_status,
    run_setup,
)

__all__ = ["main"]

logger = logging.getLogger("opencode_bridge")


def _setup_logging(level: str) -> None:
    numeric = getattr(logging, str(level or "INFO").upper(), logging.INFO)
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # 脱敏过滤器必须挂在 **basicConfig 之后**：它装在 handler 上，而 handler 是
    # basicConfig 建的。全仓库 34 个 logger 都是 ``opencode_bridge.*`` 的后代、
    # 记录一律 propagate 到 root，所以这一行就覆盖了全部 13 个适配器以及将来
    # 新增的任何一个 —— 而**没有任何一个调用点**需要改。
    install_redaction_filter()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m opencode_bridge",
        description=(
            "把 Telegram/Slack/Discord 消息桥接到本机 opencode 服务。"
        ),
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help="配置文件路径（默认：环境变量 OPENCODE_BRIDGE_CONFIG 或 ./config.json）",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="输出 DEBUG 级别日志"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只做与 opencode 服务的连通性自检，然后退出",
    )
    parser.add_argument(
        "--setup",
        nargs="?",
        const="",
        metavar="平台",
        default=None,
        help=(
            "打印接入引导后退出（telegram|slack|discord 或 1|2|3；不带参数则打印平台菜单）。"
            "不需要连接 opencode，也不需要配置 token"
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help=(
            "与 --setup 搭配：输出机器可读 JSON（配置路径 + 各平台是否已配 token"
            " + 授权白名单状态 + 是否谁都能驱动）"
        ),
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="汇总服务连通性 / 各平台配置与能力 / bridge 运行态证据，然后退出",
    )
    parser.add_argument(
        "--pair",
        metavar="配对码",
        help=(
            "把 bot 里 /pair 给出的那串码兑换成 allowed_chat_ids 里的一项，"
            "然后退出（要重启桥接才生效）"
        ),
    )
    parser.add_argument(
        "--conversation",
        metavar="platform:local_id",
        help=(
            "配合 --pair 使用：要授权哪个会话（形如 telegram:12345，"
            "就是 /pair 回信里那个值）。码绑定会话，只凭码无法确定是哪个。"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        cfg = Config.load(args.config)
    except Exception:
        logger.exception("加载配置失败")
        return 1
    _setup_logging("DEBUG" if args.verbose else cfg.log_level)

    try:
        if args.check:
            return run_check(cfg)
        # --setup 走最前：不连 opencode、不需要 token，接入前也能看引导
        if args.setup is not None:
            return run_setup(cfg, args.setup, bool(args.json))
        if args.status:
            return run_status(cfg)
        if args.pair is not None:
            # 改完配置就退出，不去连 opencode、不建适配器。
            return run_pair(cfg, args.pair, args.conversation)
        return run_bridge(cfg)
    except KeyboardInterrupt:
        logger.info("已中断")
        return 1
    except Exception:
        logger.exception("opencode-bridge 异常退出")
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

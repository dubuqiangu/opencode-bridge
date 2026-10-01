"""Lane C — CLI entry point: ``python -m opencode_bridge``.

Usage::

    python -m opencode_bridge [--config PATH] [--verbose] [--check]

* ``--check`` only resolves the endpoint and calls ``GET /api/info`` — it
  never creates a session and never starts an adapter.
* The normal run builds every configured adapter, starts the SSE reader and
  blocks until ``Ctrl+C``; shutdown always goes through ``BridgeCore.stop()``.
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
from typing import Sequence

from .adapters import build
from .config import Config
from .core import BridgeCore
from .opencode_client import OpenCodeClient, discover_endpoint
from .state import StateStore

__all__ = ["main"]

logger = logging.getLogger("opencode_bridge")

NO_ADAPTER_MESSAGE = "没有任何可用适配器：请在 config.json 的 adapters 中配置 bot_token"


def _setup_logging(level: str) -> None:
    numeric = getattr(logging, str(level or "INFO").upper(), logging.INFO)
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


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
    return parser


def _has_configured_adapter(cfg: Config) -> bool:
    """Cheap pre-flight: does the config mention any adapter with a token?

    Kept deliberately simple so a broken config fails with a helpful message
    *before* endpoint discovery is attempted.
    """
    entries = cfg.adapters or {}
    if not isinstance(entries, dict) or not entries:
        return False
    for entry in entries.values():
        if not isinstance(entry, dict):
            continue
        token = entry.get("bot_token")
        if token is not None and str(token).strip():
            return True
    return False


def run_check(cfg: Config) -> int:
    """Connectivity self-test: ``info()`` only, no sessions, no adapters."""
    endpoint = discover_endpoint(cfg.opencode_url, cfg.opencode_password)
    client = OpenCodeClient(endpoint)
    try:
        info = client.info()
    finally:
        client.close()
    print("opencode service OK")
    print(f"  version : {info.get('version', '?')}")
    print(f"  pid     : {info.get('pid', '?')}")
    print(f"  url     : {endpoint.url}")
    return 0


def run_bridge(cfg: Config) -> int:
    if not _has_configured_adapter(cfg):
        print(NO_ADAPTER_MESSAGE, file=sys.stderr)
        print(
            "提示：配置完成后下次启动自动生效；也可在 bot 内发送 /setup 查看分平台接入引导。",
            file=sys.stderr,
        )
        return 0

    endpoint = discover_endpoint(cfg.opencode_url, cfg.opencode_password)
    client = OpenCodeClient(endpoint)
    state = StateStore(cfg.state_path)
    core = BridgeCore(cfg, client, state)

    usable = 0
    for name, entry in list((cfg.adapters or {}).items()):
        try:
            adapter = build(name, entry if isinstance(entry, dict) else {}, core)
        except KeyError:
            logger.warning("unknown adapter %r in config; skipped", name)
            continue
        except Exception:
            logger.exception("failed to build adapter %r; skipped", name)
            continue
        token = getattr(adapter, "bot_token", None)
        if token is not None and not str(token).strip():
            logger.warning("%s: bot_token missing; adapter skipped", name)
            continue
        core.attach(adapter)
        usable += 1

    if usable == 0:
        print(NO_ADAPTER_MESSAGE, file=sys.stderr)
        client.close()
        return 1

    core.start()
    logger.info("bridge running against %s — press Ctrl+C to stop", endpoint.url)
    stop_event = threading.Event()
    try:
        while not stop_event.wait(1.0):  # periodic -> Ctrl+C always lands
            pass
    except KeyboardInterrupt:
        logger.info("收到中断信号 (Ctrl+C)，正在停止 ...")
    finally:
        core.stop()
    logger.info("已退出")
    return 0


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
        return run_bridge(cfg)
    except KeyboardInterrupt:
        logger.info("已中断")
        return 1
    except Exception:
        logger.exception("opencode-bridge 异常退出")
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

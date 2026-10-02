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
from typing import Sequence

from .adapters import build
from .config import Config, DEFAULT_CONFIG_NAME
from .core import BridgeCore, setup_platforms, setup_reply
from .opencode_client import OpenCodeClient, discover_endpoint
from .state import StateStore

__all__ = ["main"]

logger = logging.getLogger("opencode_bridge")

#: 未配置任何可用适配器时的提示。**不能**只说 ``bot_token``：Matrix / IRC /
#: Mattermost 根本没有这个键，只提它会让那三类用户以为自己配错了。
NO_ADAPTER_MESSAGE = (
    "没有任何可用适配器：请在 config.json 的 adapters 里配置对应平台的凭据"
    "（Telegram/Slack/Discord 用 bot_token，Slack 入站另需 app_token，"
    "Matrix 用 homeserver/access_token/user_id，IRC 用 host/nick/channels，"
    "Mattermost 用 site_url/token）"
)


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
        help="与 --setup 搭配：输出机器可读 JSON（配置路径 + 各平台是否已配 token）",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="汇总服务连通性 / 各平台配置与能力 / bridge 运行态证据，然后退出",
    )
    return parser


def _token_present(entry: dict, key: str) -> bool:
    return bool(str(entry.get(key) or "").strip())


def _status_platform_keys() -> list[str]:
    """状态视图要列的平台：优先取注册表里全部已注册平台。

    冻结的 ``/setup`` 菜单刻意只列三平台（人工维护的引导文案），但 ``--status``
    与 ``--setup --json`` 是运行时视图 —— 新加的平台若不在这里出现，用户就根本
    看不到它。注册表读不到时退回菜单里的三家。
    """
    try:
        from .adapters import registered_names

        names = list(registered_names())
    except Exception:  # noqa: BLE001 - 状态视图不该崩
        names = []
    if not names:
        names = [key for key, _ in setup_platforms()]
    return names


def _platform_status(cfg: Config) -> list[dict[str, object]]:
    """Per-platform 配置状态 for ``--setup --json``（数据驱动，不硬编码平台表）。

    ``configured`` 要求该平台 ``required_tokens`` **全部**齐备。Slack 缺
    ``app_token`` 时"只发出站"仍可用，但入站根本没通 —— 这种情况必须能被机器
    读出来，只看 ``bot_token`` 会把"入站没通"误报成已配置。
    """
    from .adapters import adapter_class

    entries = cfg.adapters if isinstance(cfg.adapters, dict) else {}
    labels = dict(setup_platforms())
    out: list[dict[str, object]] = []
    for key in _status_platform_keys():
        cls = adapter_class(key)
        entry = entries.get(key) if isinstance(entries.get(key), dict) else {}
        required = tuple(getattr(cls, "required_tokens", ("bot_token",)) or ("bot_token",))
        outbound = tuple(getattr(cls, "outbound_tokens", ("bot_token",)) or ("bot_token",))
        # ``config_optional``（无凭据可填、且默认值安全）→ 不看配置也能跑。
        # 见 ``adapters/base.py`` 里该属性的说明：此前这里只看 required_tokens，
        # 于是 a2a 空配置会被报成"未配置"/"发不出去"，而它本来就能跑。
        optional = bool(getattr(cls, "config_optional", False))
        missing = [] if optional else [k for k in required if not _token_present(entry, k)]
        supports_inbound = bool(getattr(cls, "supports_inbound", False))
        out.append(
            {
                "key": key,
                "label": str(getattr(cls, "label", "") or labels.get(key) or key),
                # 全部必需 token 齐备才算配好（入站必需项也算在里面）
                "configured": not missing,
                # 出站凭据各平台不同（Matrix 用 homeserver/access_token、IRC 用
                # host/nick…），必须由适配器声明，不能硬编码 bot_token。
                "outbound_ready": optional
                or all(_token_present(entry, k) for k in outbound),
                # 入站要"能力已实现"且"配置齐备"两个条件同时成立
                "inbound_ready": supports_inbound and not missing,
                "inbound_implemented": supports_inbound,
                "missing": missing,
            }
        )
    return out


def run_setup(cfg: Config, platform: str, as_json: bool) -> int:
    """Print the frozen onboarding copy. Never contacts opencode."""
    if as_json:
        payload = {
            "config_path": _config_file_in_use(),
            "platforms": _platform_status(cfg),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(setup_reply(platform))
    return 0


def _config_file_in_use() -> str:
    """Absolute path of the config the bridge would load right now."""
    env_path = (os.environ.get("OPENCODE_BRIDGE_CONFIG") or "").strip()
    if env_path and os.path.isfile(env_path):
        return os.path.abspath(env_path)
    cwd_path = os.path.join(os.getcwd(), DEFAULT_CONFIG_NAME)
    if os.path.isfile(cwd_path):
        return os.path.abspath(cwd_path)
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), DEFAULT_CONFIG_NAME)


def _has_configured_adapter(cfg: Config) -> bool:
    """Cheap pre-flight: is any adapter in the config actually usable?

    由各适配器**声明的** ``required_tokens`` 判定，而不是硬编码 ``bot_token``。
    硬编码会让"只配了 Matrix / IRC / Mattermost"的用户被判定成没配任何适配器，
    桥接直接拒绝启动 —— 这三个平台根本没有 ``bot_token`` 这个键。

    仍然刻意简单，好在 endpoint discovery 之前就给出有用的提示。
    """
    entries = cfg.adapters or {}
    if not isinstance(entries, dict) or not entries:
        return False

    from .adapters import adapter_class

    for name, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        cls = adapter_class(str(name))
        if bool(getattr(cls, "config_optional", False)):
            # 无凭据可填、且默认值安全的平台（a2a bind 127.0.0.1 + 端口由系统分配）
            # —— 空配置即可运行，**不许**因为没填 required_tokens 而拒绝启动。
            # 这与当年 Matrix/IRC/Mattermost 被拒启动是同一类 bug。
            return True
        required = tuple(getattr(cls, "required_tokens", ()) or ())
        if not required:
            # 未知/未注册的适配器：只要有像凭据的字段就别凭空拦住用户
            # （真正能不能用由后面的 build() 报明确的错）。
            if any(
                str(v).strip() for k, v in entry.items() if k != "allowed_chat_ids"
            ):
                return True
            continue
        if all(_token_present(entry, key) for key in required):
            return True
    return False


def _bridge_dir() -> str:
    """手动运行时 cwd 通常就是 bridge 目录；``OPENCODE_BRIDGE_CONFIG`` 指向别处时用其目录。"""
    env_path = (os.environ.get("OPENCODE_BRIDGE_CONFIG") or "").strip()
    if env_path and os.path.isfile(env_path):
        return os.path.dirname(os.path.abspath(env_path)) or os.getcwd()
    return os.getcwd()


def _pid_alive(pid: int) -> bool:
    """进程是否存活（Windows 上 os.kill(pid, 0) 可用）。"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
    except Exception:
        return False


class _NullHooks:
    """只为读取适配器能力快照而存在：不消费任何事件。"""

    def on_inbound(self, inbound: object) -> None:  # pragma: no cover - 空实现
        return None

    def on_callback(self, conversation_id: str, data: str, query_id: str) -> None:  # pragma: no cover
        return None


def _channel_config_rows(cfg: Config) -> list[tuple[str, str, bool, bool, dict]]:
    """``(key, label, configured, inbound_ready, capabilities)``。

    能力来自各适配器的显式声明（T1.1），必需 token 来自 ``required_tokens``，
    平台清单来自注册表 —— 三者都不在这里硬编码。
    """
    from .adapters import adapter_class, build

    entries = cfg.adapters if isinstance(cfg.adapters, dict) else {}
    rows: list[tuple[str, str, bool, bool, dict]] = []
    for key in _status_platform_keys():
        cls = adapter_class(key)
        raw = entries.get(key)
        entry = raw if isinstance(raw, dict) else {}
        required = tuple(getattr(cls, "required_tokens", ("bot_token",)) or ("bot_token",))
        # 同 _platform_status：config_optional 的平台不看配置也算就绪
        missing = [] if getattr(cls, "config_optional", False) else [
            k for k in required if not _token_present(entry, k)
        ]
        caps: dict = {}
        try:
            caps = build(key, entry, _NullHooks()).capabilities()
        except Exception as exc:  # 能力读取失败不该让 --status 崩
            caps = {"error": str(exc)[:80]}
        label = str(caps.get("label") or getattr(cls, "label", "") or key)
        supports_inbound = bool(getattr(cls, "supports_inbound", False))
        rows.append((key, label, not missing, supports_inbound and not missing, caps))
    return rows


def _runtime_state(bridge_dir: str) -> tuple[str, str]:
    """从锁文件推断 bridge 运行态，返回 ``(state, detail)``。

    只用**可验证的证据**：锁文件 + pid 存活 + failedAt。
    ``--status`` 是独立进程，看不到 bridge 进程内状态，因此这里刻意保守。
    """
    from .status import ChannelState

    lock_path = os.path.join(bridge_dir, ".bridge-plugin.lock")
    if not os.path.isfile(lock_path):
        return ChannelState.DISABLED.value, "无锁文件：bridge 未在运行"
    try:
        with open(lock_path, "r", encoding="utf-8-sig") as fh:
            lock = json.load(fh)
    except Exception as exc:
        return ChannelState.ERROR.value, f"锁文件不可读（{exc}）"
    pid = int(lock.get("pid") or 0)
    failed_at = lock.get("failedAt")
    if pid and _pid_alive(pid):
        return ChannelState.CONNECTED.value, f"bridge 运行中 (pid={pid})"
    if isinstance(failed_at, (int, float)):
        wait = max(0, int(300 - (time.time() - failed_at)))
        return (
            ChannelState.ERROR.value,
            f"上次快速失败于 {time.strftime('%H:%M:%S', time.localtime(failed_at))}，"
            f"backoff 中（约剩 {wait}s）",
        )
    return ChannelState.DEGRADED.value, f"锁存在但 pid={pid} 已不存在（上次异常退出）"


def _dwidth(text: str) -> int:
    """终端**显示宽度**：CJK / 全角字符占 2 列（用 ``unicodedata`` 判定）。

    ``f"{s:<n}"`` 是按**字符数**补齐的，中文表头（"平台" 2 字却占 4 列）会让整张
    表在终端里错位；平台名变长（如 "Nextcloud Talk" 14 字符）时更会挤掉与下一列
    之间的空格。两者都要按显示宽度算才对齐。
    """
    import unicodedata

    return sum(
        2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1 for ch in text
    )


def _pad(text: str, width: int) -> str:
    """按显示宽度左对齐补空格。"""
    return text + " " * max(0, width - _dwidth(text))


def run_status(cfg: Config) -> int:
    """汇总视图：服务连通性 + 各平台配置与能力 + bridge 运行态证据（T1.5）。"""
    bridge_dir = _bridge_dir()
    print("== opencode 服务 ==")
    try:
        endpoint = discover_endpoint(cfg.opencode_url, cfg.opencode_password)
        client = OpenCodeClient(endpoint)
        try:
            info = client.info()
        finally:
            client.close()
        print(f"  状态   : OK  version={info.get('version', '?')} pid={info.get('pid', '?')}")
        print(f"  地址   : {endpoint.url}")
    except Exception as exc:
        print(f"  状态   : 不可达 —— {exc}")

    print("")
    print("== 渠道配置与能力 ==")
    print("  说明：「配置」= 必需 token 全部齐备；「入站」= 入站已实现且配置齐备。")
    print("        两者都不代表连接状态（连接状态见下方运行态）")
    rows = _channel_config_rows(cfg)
    # 列宽按**显示宽度**自适应：平台名长短不一（"IRC" 3 字符、"Nextcloud Talk" 14），
    # 写死宽度会让长名字挤掉与下一列之间的空格。
    name_w = max([_dwidth("平台")] + [_dwidth(r[1]) for r in rows]) + 2
    print(
        "  " + _pad("平台", name_w) + _pad("配置", 8) + _pad("入站", 8)
        + _pad("按钮", 6) + _pad("媒体", 6) + _pad("长度上限", 12) + "白名单"
    )
    for key, label, configured, inbound_ready, caps in rows:
        if caps.get("error"):
            print(
                "  " + _pad(label, name_w) + _pad("已配置" if configured else "未配置", 8)
                + f"能力读取失败：{caps['error']}"
            )
            continue
        allowed = caps.get("allowed_chat_ids_count")
        wl = "未设(全开)" if not allowed else f"{allowed} 项"
        print(
            "  " + _pad(label, name_w)
            + _pad("已配置" if configured else "未配置", 8)
            + _pad("就绪" if inbound_ready else "否", 8)
            + _pad("是" if caps.get("supports_inline_buttons") else "否", 6)
            + _pad("是" if caps.get("supports_media") else "否", 6)
            + _pad(str(caps.get("max_message_length")), 12) + wl
        )

    print("")
    print("== bridge 运行态 ==")
    print(f"  bridge 目录 : {bridge_dir}")
    state, detail = _runtime_state(bridge_dir)
    print(f"  状态        : {state} —— {detail}")
    for name in ("bridge-plugin.log", "bridge-output.log", "config.json", "state.json"):
        p = os.path.join(bridge_dir, name)
        if os.path.isfile(p):
            size = os.path.getsize(p)
            mtime = time.strftime("%m-%d %H:%M", time.localtime(os.path.getmtime(p)))
            print(f"  {name:<20} {size:>8} 字节  最后修改 {mtime}")
    print("  注：--status 是独立进程，看不到 bridge 进程内状态；进程内健康请看 bridge-plugin.log")
    return 0


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
        # --setup 走最前：不连 opencode、不需要 token，接入前也能看引导
        if args.setup is not None:
            return run_setup(cfg, args.setup, bool(args.json))
        if args.status:
            return run_status(cfg)
        return run_bridge(cfg)
    except KeyboardInterrupt:
        logger.info("已中断")
        return 1
    except Exception:
        logger.exception("opencode-bridge 异常退出")
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

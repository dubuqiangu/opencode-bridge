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
from .allowlist import resolve_allowlist
from .config import Config, DEFAULT_CONFIG_NAME
from .core import BridgeCore, setup_platforms, setup_reply
from .diagnostics import ProcessDiagnostics, describe_environment
from .instance_lock import InstanceLock, pid_is_alive
from .inbox import InboundInbox
from .opencode_client import OpenCodeClient, discover_endpoint
from .redaction import install_redaction_filter
from .state import StateStore

__all__ = ["main"]

logger = logging.getLogger("opencode_bridge")

#: 已有实例在跑时的提示。**必须说清三件事**：谁占着（pid）、为什么不能并存
#: （同一 bot 的收消息接口只能有一个消费者）、以及怎么解决（停掉那个）。
#: 只说"已有实例在运行"会让人以为是崩溃或配置错误。
ALREADY_RUNNING_MESSAGE = (
    "已有另一个 bridge 实例在运行（pid={pid}），本次不启动。\n"
    "同一个 bot 的收消息接口同时只允许一个消费者：两个实例并存会导致\n"
    "  · 消息随机丢失（谁抢到算谁的）\n"
    "  · 同一条消息被处理两次，用户收到两份一样的回复\n"
    "如果你要调试，请先停掉那个实例（或停用 opencode 的 bridge 插件），\n"
    "不要让两个同时轮询。"
)

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
    return parser


def _token_present(entry: dict, key: str) -> bool:
    return bool(str(entry.get(key) or "").strip())


#: ``not_ready_reasons`` 的取值。**稳定 token**，不是给人看的话 —— 人看的是
#: ``detail`` 字段。理由码单独成常量，是为了让消费方（含测试）能按值断言，
#: 而不是去匹配会改字的提示语。
_NOT_READY_MISSING_CREDENTIALS = "missing_credentials"
_NOT_READY_NO_ALLOWLIST = "no_allowlist"


def _not_ready_reasons(
    *, configured: bool, accepts_any_sender: bool, has_allowlist: bool,
) -> list[str]:
    """这个平台能不能对用户说"配好了"。空列表 = 可以说。

    ⚠️ **判据里的关键一条：没配凭据的平台**不**报 ``no_allowlist``。**
    它收不到任何消息，"谁都能驱动"对它是假话；只报 ``missing_credentials``。
    真正的暴露只在"能跑"之后才存在。

    ⚠️ 刻意**不**把"白名单为空"当成错误 —— 它是本仓库被文档化、被示例配置固化的
    默认（``config.example.json`` 抄的就是 ``[]``），翻转它要产品决策与迁移期。
    本函数只保证**它不会被说成"配好了"**。
    """
    reasons: list[str] = []
    if not configured:
        reasons.append(_NOT_READY_MISSING_CREDENTIALS)
    elif accepts_any_sender and not has_allowlist:
        reasons.append(_NOT_READY_NO_ALLOWLIST)
    return reasons


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

    ⚠️ **本函数不新增"已配好"的含义，只新增独立的授权暴露面字段。**
    ``configured`` 的原义（凭据齐备）一个字没改，否则每个既有消费者都得重新学一遍
    这套输出。真正要挡的坑是：**凭据齐了 + 空=全开** 曾被报成"配置好了"，而
    ``bridge_setup`` 工具会照着这句话告诉用户"配好了"。所以另给一个
    :attr:`_NOT_READY_NO_ALLOWLIST` 级别的诚实判定 :func:`_not_ready_reasons`。
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
        configured = not missing
        # 授权面与闸门读**同一个**解析函数（``allowlist.resolve_allowlist``）——
        # 状态视图说"有限白名单"而闸门放行一切，比没有状态视图更坏。
        allowlist = resolve_allowlist(entry)
        not_ready = _not_ready_reasons(
            configured=configured,
            accepts_any_sender=allowlist.accepts_any_sender,
            has_allowlist=bool(allowlist.entries),
        )
        out.append(
            {
                "key": key,
                "label": str(getattr(cls, "label", "") or labels.get(key) or key),
                # 全部必需 token 齐备才算配好（入站必需项也算在里面）
                "configured": configured,
                # 出站凭据各平台不同（Matrix 用 homeserver/access_token、IRC 用
                # host/nick…），必须由适配器声明，不能硬编码 bot_token。
                "outbound_ready": optional
                or all(_token_present(entry, k) for k in outbound),
                # 入站要"能力已实现"且"配置齐备"两个条件同时成立
                "inbound_ready": supports_inbound and not missing,
                "inbound_implemented": supports_inbound,
                "missing": missing,
                # --- 授权暴露面（新增；configured 的含义不变）----------------
                # 解析出的白名单条目数。0 = 空 = **全开**（闸门的真实语义）。
                "allowed_chat_ids_count": len(allowlist.entries),
                # 有没有真的限人。读它来回答"别人能不能开我的 bot"。
                "allowlist_configured": bool(allowlist.entries),
                # 「空 = 全开」这个语义是承重的，所以显式报出来，
                # 而不是让消费者从 count == 0 反推。
                "accepts_any_sender": allowlist.accepts_any_sender,
                # 配置里出现过的授权键名（可能多于一个 → 见 allowlist_conflict）
                "allowlist_keys_present": list(allowlist.present_keys),
                # 多个授权键解析出不同结果时的诊断（含实际生效的键与项数）；
                # 无冲突为 None。两个键值相同**不算**冲突 —— 结果毫无歧义。
                "allowlist_conflict": (
                    allowlist.conflict.as_dict() if allowlist.conflict else None
                ),
                # 能不能对用户说"这个平台配好了"。空 = []。
                "not_ready_reasons": not_ready,
                "ready_for_agent": not not_ready,
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
    """进程是否存活。委托给 :func:`instance_lock.pid_is_alive`，避免两处实现。"""
    return pid_is_alive(pid)


def _inbox_path(cfg: Config) -> str:
    """写前收件箱的落盘位置：与 ``state.json`` **同一个目录**，文件名 ``inbox.db``。

    跟着 ``cfg.state_path`` 走而不是死用 :func:`_bridge_dir`：用户把
    ``state_path`` 指到别处时，待投递的行必须跟着走 —— 否则状态与"还没兑现的
    处理义务"分居两处，备份与清理都得记两遍，而漏掉哪一处都不会有人发现。
    ``state_path`` 是裸文件名（默认就是 ``state.json``，相对当前目录）时，
    它的目录恰好就是 :func:`_bridge_dir`。
    """
    return os.path.join(
        os.path.dirname(os.path.abspath(cfg.state_path)) or _bridge_dir(),
        "inbox.db",
    )


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

    ``capabilities`` 里会**额外并入**授权面的几个键（键名是否出现、是否冲突）。
    为什么不在 :meth:`opencode_bridge.adapters.base.Adapter.capabilities` 里加：
    那个快照的键集合被 ``test_adapters`` 逐个钉死，而这里是**状态视图自己的**
    组装处 —— 派生事实并进状态行，不必去动适配器的公共契约。
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
            caps = dict(build(key, entry, _NullHooks()).capabilities())
        except Exception as exc:  # 能力读取失败不该让 --status 崩
            caps = {"error": str(exc)[:80]}
        # 授权面与闸门同源：同一个 resolve_allowlist，不另写一份解析。
        allowlist = resolve_allowlist(entry)
        caps["allowlist_keys_present"] = list(allowlist.present_keys)
        caps["allowlist_conflict"] = (
            allowlist.conflict.as_dict() if allowlist.conflict else None
        )
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
    print("        「白名单」为空 = 未设 = **全开**：任何能联系到 bot 的人都能驱动它")
    rows = _channel_config_rows(cfg)
    # 列宽按**显示宽度**自适应：平台名长短不一（"IRC" 3 字符、"Nextcloud Talk" 14），
    # 写死宽度会让长名字挤掉与下一列之间的空格。
    name_w = max([_dwidth("平台")] + [_dwidth(r[1]) for r in rows]) + 2
    print(
        "  " + _pad("平台", name_w) + _pad("配置", 8) + _pad("入站", 8)
        + _pad("按钮", 6) + _pad("媒体", 6) + _pad("长度上限", 12) + "白名单"
    )
    conflicts: list[tuple[str, str]] = []
    for key, label, configured, inbound_ready, caps in rows:
        if caps.get("error"):
            print(
                "  " + _pad(label, name_w) + _pad("已配置" if configured else "未配置", 8)
                + f"能力读取失败：{caps['error']}"
            )
            continue
        allowed = caps.get("allowed_chat_ids_count")
        # ⚠️ 空白的白名单不是"没填"，是**全开** —— 原先渲染成「未设(全开)」，
        # 那句话在表格里既不显眼也没说清后果，用户扫一眼就过去了。改成带警示符的
        # 等式：「未设=全开」。仍然短（这是个状态表），但扫得出来。
        wl = f"{allowed} 项" if allowed else "⚠ 未设=全开"
        conflict_detail = caps.get("allowlist_conflict")
        if conflict_detail:
            wl += " ⚠键冲突"
            conflicts.append((label, str(conflict_detail.get("detail") or "")))
        print(
            "  " + _pad(label, name_w)
            + _pad("已配置" if configured else "未配置", 8)
            + _pad("就绪" if inbound_ready else "否", 8)
            + _pad("是" if caps.get("supports_inline_buttons") else "否", 6)
            + _pad("是" if caps.get("supports_media") else "否", 6)
            + _pad(str(caps.get("max_message_length")), 12) + wl
        )

    # 键冲突不塞进表格列里：那句说明有一百多字，塞进去会把整张表撑烂。
    # 单独列在表下 —— 用户扫表时已经看到「⚠键冲突」的标记了。
    if conflicts:
        print("")
        print("== ⚠ 授权键冲突（实际生效的是哪一个键已在此说明） ==")
        for label, detail in conflicts:
            print(f"  {label}: {detail}")

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

    # 单实例闸门：同一台机器上不允许两个桥同时轮询同一个 bot。
    # 必须在建连接之前拦下——否则消息已经被抢走一半了。
    instance_lock = InstanceLock(_bridge_dir())
    acquired, holder_pid = instance_lock.acquire()
    if not acquired:
        print(ALREADY_RUNNING_MESSAGE.format(pid=holder_pid), file=sys.stderr)
        return 0

    try:
        return _run_bridge_locked(cfg)
    finally:
        instance_lock.release()


def _run_bridge_locked(cfg: Config) -> int:
    """真正启动桥。调用方必须已持有单实例锁。"""

    endpoint = discover_endpoint(cfg.opencode_url, cfg.opencode_password)
    client = OpenCodeClient(endpoint)
    # ⚠️ ``migrate_keys=True`` 是**必须**的，不是可选项：telegram / matrix 已改用
    # ``platform:local_id``，而 StateStore 拿 conversation_id 当不透明键存
    # ``conversation_id ↔ session_id``。不开这个开关，已落盘 ``state.json`` 里的
    # ``chat:`` / ``room:`` 键会一次性变成孤儿 —— 用户升级后一次性"忘记"所有历史
    # 会话，不报错，只表现为"agent 突然记错上下文"。歧义的 ``channel:`` 键
    # （slack / discord / mattermost 共用）原样保留、继续可用。
    state = StateStore(cfg.state_path, migrate_keys=True)
    # 写前收件箱必须在 state 旁边，且**开/关都要说出来**：它是可选注入的
    # （None = 关闭），静默关闭正好让这次要修的丢消息 bug 重新变得看不见 ——
    # 所以启动日志里必须能一眼看出当前是开是关。
    inbox_path = _inbox_path(cfg)
    try:
        inbox = InboundInbox(inbox_path)
    except Exception:
        logger.exception("写前收件箱打开失败（%s）", inbox_path)
        inbox = None
    if inbox is None:
        logger.warning(
            "写前收件箱：未启用 —— 投递窗口里崩溃仍会静默丢消息"
        )
    else:
        logger.info("写前收件箱：已启用（%s）", inbox_path)
    core = BridgeCore(cfg, client, state, inbox)

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
        if inbox is not None:
            inbox.close()
        return 1

    core.start()
    logger.info("bridge running against %s — press Ctrl+C to stop", endpoint.url)
    stop_event = threading.Event()

    # 台账 G1 的取证：桥会不明原因退出，而 Python 层日志在进程被外部杀死时
    # **什么都不会留下**。这里补上崩溃栈 + 生命周期账本，**只取证不自愈**
    # （死因未知时加自愈机制等于用一层自愈掩盖真正的问题）。
    # 详见 opencode_bridge/diagnostics.py 的模块说明。
    diagnostics = ProcessDiagnostics(_bridge_dir())
    diagnostics.install()
    exit_reason = "stopped"
    try:
        while not stop_event.wait(1.0):  # periodic -> Ctrl+C always lands
            pass
    except KeyboardInterrupt:
        exit_reason = "keyboard-interrupt"
        logger.info("收到中断信号 (Ctrl+C)，正在停止 ...")
    except BaseException as exc:  # 含 SystemExit；被信号打断时可能走到这
        exit_reason = "unhandled:%s" % type(exc).__name__
        raise
    finally:
        # 顺序很关键：**先留证据再停 core**。core.stop() 会关掉线程，
        # 那时线程栈已经没有诊断价值了。
        diagnostics.record(exit_reason, detail=describe_environment())
        diagnostics.dump_stacks(exit_reason)
        diagnostics.close()
        core.stop()
        if inbox is not None:
            inbox.close()   # 生命周期清理：关掉 SQLite 连接（WAL 落盘）
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

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

    ⛔ **同一条纪律也适用于 :func:`health` 带来的 ``last_start_probe``：只增不改。**
    现有 key（``admits_nobody`` / ``admits_any_sender`` / ``not_ready_reasons`` …）
    一个都不许删、不许改名 —— 仓库外的消费者（``bridge_setup``）按**值**断言它们，
    而我们读不到它的源码。任何"顺手清理"都可能打掉别人的判据。
    """
    from .adapters import adapter_class

    entries = cfg.adapters if isinstance(cfg.adapters, dict) else {}
    labels = dict(setup_platforms())
    #: 「上次启动时」的探测结论（:mod:`opencode_bridge.health`）。⛔ **纯本地读盘**：
    #: 本函数不做任何网络请求 —— 把 ``getMe`` 之类塞进来会让 ``--setup --json``
    #: 在网络被墙时也发不出那条最该看的错误信息（且 ``run_check`` 的 docstring
    #: 明写「no sessions, **no adapters**」）。读一次就够，**不逐平台重读**。
    probe_record = health.read_platform_health(_bridge_dir())
    #: **运行期**出站失败记录（**另一个文件**，见 :mod:`opencode_bridge.health`
    #: 的「出站失败为什么另开一份文件」）。⛔ 与上面的 ``probe_record`` 时效语义
    #: 不同（一个是「上次启动那一刻」，一个是「上一次失败的那一刻」），所以连
    #: 载体都分开 —— 否则 :func:`health.record_startup_probes` 的整份替换会顺手
    #: 把它删掉。同样**纯本地读盘**：本函数不做任何网络请求。
    outbound_record = health.read_outbound_failures(_bridge_dir())
    out: list[dict[str, object]] = []
    for key in _status_platform_keys():
        cls = adapter_class(key)
        entry = entries.get(key) if isinstance(entries.get(key), dict) else {}
        outbound = tuple(getattr(cls, "outbound_tokens", ("bot_token",)) or ("bot_token",))
        # 「这份配置此刻配好了没有」问**唯一**那个判定入口（见 :func:`_readiness_verdict`）。
        # ⚠️ **不再**问 ``config_optional`` 那个静态声明：它记录的是"曾经为真"的事实
        # —— 注释曾断言"a2a 空配置即可运行（端口由系统分配）"，而 ``_coerce_port("")``
        # 给的是 ``UNCONFIGURED_PORT``（-1）、``A2aAdapter.start()`` 据此打 error
        # **不绑定就 return** ⇒ 那条前提早就不成立了。
        verdict = _readiness_verdict(cls, entry)
        missing = _missing_required_keys(cls, entry)
        supports_inbound = bool(getattr(cls, "supports_inbound", False))
        configured = not missing
        # 授权面与闸门读**同一个**解析函数（``allowlist.resolve_allowlist``）——
        # 状态视图说"有限白名单"而闸门放行一切，比没有状态视图更坏。
        allowlist = resolve_allowlist(entry)
        not_ready = _not_ready_reasons(
            configured=configured,
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
                # 平台自己作答时（``verdict`` 非 None）它的答案是权威的 ——
                # 与 :attr:`configured` 同一个来源，两列不会互相矛盾。
                "outbound_ready": verdict if verdict is not None
                else all(_token_present(entry, k) for k in outbound),
                # 入站要"能力已实现"且"配置齐备"两个条件同时成立
                "inbound_ready": supports_inbound and not missing,
                "inbound_implemented": supports_inbound,
                "missing": missing,
                # --- 授权暴露面（新增；configured 的含义不变）----------------
                # 解析出的白名单条目数。0 = 空 = **闸门不放行任何人**
                # （新语义）或 **放行所有人**（旧语义）—— 见下面两个字段。
                "allowed_chat_ids_count": len(allowlist.entries),
                # 有没有真的限人。读它来回答"别人能不能开我的 bot"。
                "allowlist_configured": bool(allowlist.entries),
                # ⛔ **键已改名**：``accepts_any_sender`` → ``admits_nobody``。
                # **不删键** —— 删了就逼消费者从 ``count == 0`` 反推策略，
                # 那正是 :mod:`opencode_bridge.allowlist` 当初被拆出来要消灭的事。
                # 新名下的判定式一个字没动，变的只是"空清单"指向哪一边。
                "admits_nobody": allowlist.admits_nobody,
                # 这个配置下闸门**是否真的会放行一切**。消费者要答"别人能不能开我的
                # bot"就读它 —— 它把 :attr:`admits_nobody` 与配置版本合成一个答案，
                # 于是它在新旧两种语义下都是**同一件事**：闸门此刻的实际行为。
                "admits_any_sender": allowlist.gate_admits_everyone(
                    cfg.config_version
                ),
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
                # --- 适配器自己声明的运行判据 ---------------------------------
                # ⚠️ **为什么必须在这儿透出 `capabilities()`**（实测缺陷，2026-10-06）：
                # 有些平台「凭据齐备」**不等于**「能收到东西」——
                # homeassistant 默认**一个事件都不收**（`capabilities()` 里的
                # `inbound_accepts_anything=False` 就是那个明确信号）。
                # `homeassistant.py` 的 docstring 写着「`--setup --json` /
                # `--status` 的 JSON 输出能直接读到」，而**本函数原来根本没调它**
                # ⇒ 文档承诺的判据任何命令都读不到。
                #
                # 只在**已配置**时构造适配器：未配置时构造只会抛，而「配齐了却收不到」
                # 正是我们要暴露的那个情形。`_NullHooks` 与构造方式沿用
                # `_channel_config_rows`，⛔ 不新造第二套。
                "capabilities": (
                    _adapter_capabilities(key, cfg, entry) if configured else None
                ),
                # --- 「上次启动」时的探测结论（新增；configured 的含义不变）----
                # ⚠️ **它回答的不是"现在能不能用"**：``configured`` / ``inbound_ready``
                # 只看凭据齐不齐，而"token 到底有效吗"只有平台能回答。桥启动时
                # 已经问过一次（Telegram 的 ``getMe``），结论落在这里（见
                # :mod:`opencode_bridge.health`），本函数只**读盘**、不联网。
                #
                # ⛔ **``None`` 的含义是「没有记录」，不是「没问题」**：桥还没以
                # 当前配置启动过、或从没启动过，两者都是 ``None``。把它显示成
                # "ok"就是把"没验"说成"验过了"。
                #
                # ⚠️ **取值域多了一档 :data:`health.VERDICT_NOT_STARTED`**（``not_started``）：
                # 它答的是「**桥这一轮压根没起来**」，不是「这个平台起不来」——
                # 拒绝启动的两条路径（见 :func:`_record_bridge_refusal`）现在也落
                # 这样的记录，所以消费方读它就知道**别**把它当成平台级线索去
                # 建议用户改凭据。
                #
                # ⚠️ **还有一档 :data:`health.VERDICT_DOES_NOT_PROBE`**（``does_not_probe``）：
                # 它答的是「**本平台压根没有启动期凭据探测这个动作**」——
                # 目前只有 telegram 会探（``getMe``）。⛔ 它**不是** ``skipped``
                # （那一档说的是「没有可探测的凭据」＝"你少配了"），也**不是**
                # ``not_started``（那一档说的是"桥没起来"）。消费方读它就该知道
                # **这条通道压根没覆盖这个平台**，而**不是**"这里没问题"。
                #
                # ⛔ 既有三档（``ok`` / ``failed`` / ``skipped``）
                # **一个字没改**：本函数对 ``platforms`` 的其它 key 也一样，只增不改。
                "last_start_probe": health.probe_from_record(probe_record, key),
                # --- 运行期出站失败（新增；outbound_ready 的含义不变）----------
                # ⚠️ **不要把它读成"发送能用"**：``outbound_ready`` 的原义是
                # 「**出站凭据齐备**」，是一个**有测试钉住的既有契约**，一个字没改
                # （改它等于让每个既有消费者重新学一遍这套输出）。而"凭据齐"与
                # "真发得出去"是两件事 —— SMTP 密码改了、Telegram 把 bot 踢了、
                # 对端限流，凭据照样齐备而**一条都发不出去**。
                # ⇒ 所以**另加**这一个字段回答后者。
                #
                # ⛔ **它回答的是"上一次失败"，不是"现在能不能发"**：``at`` 是那
                # 一刻的时刻，``recovered_at`` 缺失 = 之后**没有观测到**成功
                # （可能是仍在失败，也可能是压根没人再发消息 —— 盘上分不出是哪
                # 一种，而猜一个就是编造，见 AGENTS.md §8）。
                #
                # ⛔ **``None`` 的含义是「没有记录」，不是「没问题」**：桥还没以
                # 当前配置运行过、或运行期间一条都没发出去，两者都是 ``None``。
                "last_outbound_failure": health.outbound_failure_from_record(
                    outbound_record, key,
                ),
            }
        )
    return out


def run_setup(cfg: Config, platform: str, as_json: bool) -> int:
    """Print the frozen onboarding copy. Never contacts opencode."""
    if as_json:
        payload = {
            "config_path": _config_file_in_use(cfg),
            "platforms": _platform_status(cfg),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(setup_reply(platform))
    return 0


def _bridge_dir() -> str:
    """手动运行时 cwd 通常就是 bridge 目录；``OPENCODE_BRIDGE_CONFIG`` 指向别处时用其目录。"""
    env_path = (os.environ.get("OPENCODE_BRIDGE_CONFIG") or "").strip()
    if env_path and os.path.isfile(env_path):
        return os.path.dirname(os.path.abspath(env_path)) or os.getcwd()
    return os.getcwd()


#: 桥**拒绝启动**的两条路径。⚠️ 它们都**必须**落一条
#: :data:`~opencode_bridge.health.VERDICT_NOT_STARTED` 的结论 —— 不落的话，盘上留下的
#: 就是上一次**成功启动**的那条 ``ok``（内容与 mtime 都不变），而 ``--status`` 会
#: 把它与「未配置」并排显示 ⇒ 用户刚把配置改坏时，看到的仍是上一轮的好消息。
#: ``_has_configured_adapter`` 为否的那条在 ``discover_endpoint`` **之前**，
#: ``usable == 0`` 的那条在它**之后**、``core.start()`` **之前**。
REFUSAL_STAGE_PREFLIGHT = "preflight_no_configured_adapter"
REFUSAL_STAGE_NO_USABLE_ADAPTER = "no_usable_adapter"

#: 每条拒绝路径的**一句话原因**（全局的）。⚠️ 只说"是什么"；具体"哪些平台缺什么"
#: 由 :func:`_record_bridge_refusal` 逐平台算出来交给
#: :func:`~opencode_bridge.health.bridge_refusal_probes` 拼在后面。
_REFUSAL_REASON_BY_STAGE = {
    REFUSAL_STAGE_PREFLIGHT: "预检未通过：配置里没有任何适配器此刻够跑",
    REFUSAL_STAGE_NO_USABLE_ADAPTER: "没有任何适配器构造成功，桥因此没起来",
}


def _record_bridge_refusal(
    cfg: Config,
    stage: str,
    platform_reasons: Mapping[str, str] | None = None,
) -> None:
    """把「桥拒绝启动」这条结论写进**同一份** ``platform-health.json``。

    ⚠️ **为什么必须写**：不写的话，这条路上盘上留下的就是上一次成功启动的结论，
    而那正是用户最需要线索的时刻（他刚把配置改坏了）——状态视图在那一刻显示上一轮
    的好消息，等于在最关键的时候说假话。

    ⚠️ **落盘只有 :func:`~opencode_bridge.health.record_startup_probes` 那一个入口**：
    本函数只**造**出 ``{平台键: 结论}``（由
    :func:`~opencode_bridge.health.bridge_refusal_probes` 按
    :data:`~opencode_bridge.health.VERDICT_NOT_STARTED` 成形），不自己写文件。

    ⚠️ **保护必须包住「实参求值」，不只是那次调用** —— 与
    :func:`_run_bridge_locked` 里那次调用是**同一条**纪律、**同一个**理由：
    ``adapter_class`` 的 import 与 :func:`_missing_required_keys` 的逐平台判定都在
    这个 ``try`` 里面求值，它们抛了的话，这条**排障辅助**通路就有权把
    「拒绝启动 + 一条明明白白的提示」变成「拒绝启动 + 一个堆栈」。
    ⇒ 退化行为是「记一条 warning + 不写记录」，**不是**崩。

    ⚠️ 预检那条路**在 ``discover_endpoint`` 之前**，判断不依赖网络 ⇒ opencode 服务
    不可达也照样写。

    :param stage: :data:`REFUSAL_STAGE_PREFLIGHT` / :data:`REFUSAL_STAGE_NO_USABLE_ADAPTER`。
    :param platform_reasons: 只有 ``usable == 0`` 那条路需要 —— 「为什么构造不出来」
        只有那个构造循环知道（未注册 / ``build()`` 抛错 / ``bot_token`` 为空）。
    """
    try:
        reasons = {str(key): str(text) for key, text in (platform_reasons or {}).items()}
        if not reasons:
            # 预检那条路没有「循环」可问：缺什么只能由缺失键清单回答，而它走的是
            # **唯一**那个判定入口（``--status`` 的「配置」列也是用它）。
            from .adapters import adapter_class

            for key, entry in (cfg.adapters or {}).items():
                if not isinstance(entry, dict):
                    continue
                missing = _missing_required_keys(adapter_class(str(key)), entry)
                reasons[str(key)] = (
                    "缺 " + "、".join(missing) if missing else "未配置"
                )
        detail = _REFUSAL_REASON_BY_STAGE.get(stage, "桥拒绝启动")
        probes = health.bridge_refusal_probes(detail, reasons)
        written = health.record_startup_probes(_bridge_dir(), probes)
        # ⚠️ **别在写盘失败时还说"已记"** —— 那是把一条失败的排障记录报成成功的，
        # 与本模块「没有记录 ≠ 成功」是同一条纪律（失败时那条 warning 由
        # record_startup_probes 自己打）。
        logger.warning(
            "platform-health: %s —— %s",
            detail,
            "已记「桥未启动」" if written else "未落盘",
        )
    except Exception as exc:  # noqa: BLE001 - 排障记录绝不该决定桥的生死
        logger.warning(
            "platform-health: 「拒绝启动」这条结论未落盘（%s: %s）—— 不影响桥的退出",
            type(exc).__name__,
            exc,
        )


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


def _held_buffer_path(cfg: Config) -> str:
    """续行缓冲崩溃快照的落盘位置：与 ``state.json`` **同一个目录**，文件名
    ``held-buffer.json``（G2）。

    与 :func:`_inbox_path` 同一条纪律、同一个理由：跟着 ``cfg.state_path``
    走 —— "还没兑现的处理义务"必须跟着状态一起搬家（这份快照装的正是
    另一类还没兑现的义务：敲了 ``..`` 的半行）。文件与两份既有状态文件
    各自独立、键不互混（这里只放 ``{conversation_id: 正文}``）。
    """
    return os.path.join(
        os.path.dirname(os.path.abspath(cfg.state_path)) or _bridge_dir(),
        HELD_BUFFER_FILE_NAME,
    )


class _NullHooks:
    """只为读取适配器能力快照而存在：不消费任何事件。"""

    def on_inbound(self, inbound: object) -> None:  # pragma: no cover - 空实现
        return None

    def on_callback(self, conversation_id: str, data: str, query_id: str) -> None:  # pragma: no cover
        return None


def _adapter_capabilities(key: str, cfg: Config, entry: dict) -> dict:
    """构造适配器并读它的 ``capabilities()``。

    ⚠️ **这是 ``_channel_config_rows`` 里那段构造逻辑的提取，不是第二份实现** ——
    投影（:func:`adapter_scoped_config`）、hooks、错误兜底三处都与它一致，
    改动面才只在一个文件里（§5）。实参形态也逐字照抄：``build(key, …)``
    （第一参是**平台键字符串**，不是类 —— `build` 的签名是 ``(name, config, hooks)``）。

    为什么 ``--setup --json`` 需要它：有些平台「凭据齐备」**不等于**「能收到东西」。
    homeassistant 默认**一个事件都不收**，``inbound_accepts_anything=False`` 才是那个
    「配好了但收不到」的明确信号 —— 而 homeassistant 的 docstring 明写这个信号要能从
    ``--setup --json`` 读到，**原来却没有任何命令输出它**（实测，2026-10-06）。
    """
    try:
        # 与 run_bridge 走**同一个**投影，否则状态视图会在一个真实运行着的
        # 适配器上读出另一套语义（它也构造适配器，见 :class:`_NullHooks` 的说明）。
        return dict(
            build(key, adapter_scoped_config(cfg, entry), _NullHooks()).capabilities()
        )
    except Exception as exc:  # 能力读取失败不该让 --status / --setup --json 崩
        return {"error": str(exc)[:80]}


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
        # 同 _platform_status / _has_configured_adapter：问**唯一**那个判定入口。
        # 少改这一处的代价是它继续说"a2a 空配置已配置"，而 `--status` 的
        # 「配置 / 入站」两列正是用户决定填不填 bind_port 的依据。
        missing = _missing_required_keys(cls, entry)
        caps: dict = {}
        try:
            # 与 run_bridge 走**同一个**投影，否则状态视图会在一个真实运行着的
            # 适配器上读出另一套语义（它也构造适配器，见 :class:`_NullHooks` 的说明）。
            caps = dict(
                build(key, adapter_scoped_config(cfg, entry), _NullHooks()).capabilities()
            )
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


def _print_event_subscription(subscription_status: SubscriptionStatus | None) -> None:
    """打印「``/api/event`` 那条线程此刻怎么样」那一段。

    ⚠️ 渲染**不在**本函数里：措辞、判别与那一对判别属性的用法都在
    :mod:`opencode_bridge.subscription_status_view`（AGENTS.md §5「视图渲染」不是入口
    文件的职责）。本函数只做装配：调一次渲染器、逐行 ``print``。

    ⚠️ **传进来的可能是 ``None``，而那不是「正常」**：``--status`` 是独立进程，
    拿不到桥进程内的 :class:`~opencode_bridge.core.BridgeCore` ⇒ 渲染器在那种情形
    下输出「本进程读不到」，⛔ 而不是「一切正常」。
    """
    for line in render_subscription_status(subscription_status):
        print(line)


def run_status(
    cfg: Config,
    *,
    subscription_status: SubscriptionStatus | None = None,
) -> int:
    """汇总视图：服务连通性 + 各平台配置与能力 + bridge 运行态证据（T1.5）。

    ⚠️ ``subscription_status`` 是「事件流订阅」那一段的**直接**输入；
    ``None``（默认）⇒ **从盘上那份记录读**
    （:func:`~opencode_bridge.subscription_health.read_subscription_status`）。
    给一份快照 ⇒ 覆盖盘上那份（进程内调用与测试用）。

    ⚠️ **读不到不是正常**：写那份记录的进程已经不在了、或文件不存在/坏了
    ⇒ 那一段会说「读不到」，⛔ 不说正常（见
    :mod:`opencode_bridge.subscription_status_view`）。
    """
    bridge_dir = _bridge_dir()
    if subscription_status is None:
        subscription_status = read_subscription_status(bridge_dir)
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
    # ⚠️ **图例要按配置版本给两套文案** —— 写死一套会让另一个版本的用户读到假话，
    # 而这张表唯一的作用就是"用户读到的 == 闸门的真实行为"。
    if empty_allowlist_is_open(cfg.config_version):
        print("        「白名单」为空 = 未设 = **全开**：任何能联系到 bot 的人都能驱动它")
        print("        （你的配置没有 config_version 或 < 2 ⇒ 沿用旧的「空 = 全开」。")
        print("          下一版起此处改为「空 = 全拒」；届时在 bot 内发 /pair 一步授权，")
        print("          不必手改本文件。）")
    else:
        print("        「白名单」为空 = 未设 = **全拒**：任何人都进不来（最安全的状态）。")
        print("        （你的配置是 config_version >= 2。在 bot 内发 /pair 即可授权那个会话。）")
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
        # ⚠️ 空白的白名单不是"没填"，是**闸门的一个具体行为**，而那个行为取决于
        # 配置版本：旧语义下是**全开**（危险，要警示符），新语义下是**全拒**
        # （最安全，不该报警 —— 对安全状态报警会教会用户忽略这一列）。
        # 两套文案**都**要短：这是个状态表，长句会把表撑烂。
        if allowed:
            wl = f"{allowed} 项"
        elif empty_allowlist_is_open(cfg.config_version):
            wl = "⚠ 未设=全开"
        else:
            wl = "未设=全拒"
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

    _print_last_start_probes(rows, bridge_dir)
    _print_last_outbound_failures(rows, bridge_dir)
    _print_last_runtime_credential_failures(rows, bridge_dir)
    _print_event_subscription(subscription_status)

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
        # ⚠️ 这条路在 discover_endpoint **之前**返回 ⇒ 「桥没起来」这件事曾经
        # 完全不落盘，而判断**不依赖网络**，所以写记录也不会被"服务不可达"影响。
        _record_bridge_refusal(cfg, REFUSAL_STAGE_PREFLIGHT)
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
    # ⚠️ **装在 ``BridgeCore(...)`` 构造【之前】**（不是 ``start()`` 之前）：看护者是在
    # :class:`~opencode_bridge.event_stream.EventStream` 的构造里建的，它当场把记录器
    # 取成 ``on_status_change`` ⇒ 晚一步装上，那条通道整个不存在而**没有任何东西会
    # 报错**。⛔ 装不上不许阻断启动（排障通道不该决定桥的生死）；退化行为就是
    # 「不落盘」，而 ``--status`` 那一段会说「读不到」（⛔ 不是「正常」）。
    try:
        install_subscription_health_recorder(
            SubscriptionHealthRecorder(_bridge_dir())
        )
    except Exception as exc:  # noqa: BLE001 - 排障记录绝不该决定桥的生死
        logger.warning(
            "subscription-health: 记录器未装配（%s: %s）—— 不影响桥的订阅",
            type(exc).__name__, exc,
        )
    # G2：续行缓冲的崩溃快照层，**无条件启用**。它的构造不碰盘（读写失败
    # 各自只记 WARNING、绝不上抛，见 held_buffer_store 与
    # InboundGateway._persist_held_snapshot），所以没有 inbox 那个「打开失败
    # 降级」分支；启用与否照 inbox 的纪律**说出来**，不让人去翻代码。
    held_buffer_path = _held_buffer_path(cfg)
    held_buffer_store = HeldBufferStore(held_buffer_path)
    logger.info(
        "续行缓冲快照：已启用（%s）—— 崩溃后重启会把半行重灌回缓冲并告知",
        held_buffer_path,
    )
    core = BridgeCore(cfg, client, state, inbox, held_buffer_store=held_buffer_store)

    usable = 0
    # ⚠️ **装在 `core.start()` 之前**：适配器一启动就可能发信（探针回复、启动时
    # 排队消息的补发），而这一路此前完全没有落盘 ⇒ 那几条失败会在记录里缺一段。
    # ⛔ 装不上不许阻断启动（排障通道不该决定桥的生死）；退化行为就是「不记」，
    # 发送路径一个字不变。
    try:
        install_outbound_failure_recorder(health.OutboundFailureRecorder(_bridge_dir()))
    except Exception as exc:  # noqa: BLE001 - 排障记录绝不该决定桥的生死
        logger.warning(
            "outbound-failures: 记录器未装配（%s: %s）—— 不影响桥的发送",
            type(exc).__name__, exc,
        )
    # ⚠️ 同一条纪律、同一个时机（必须早于适配器启动）：telegram 的轮询线程在
    # **运行期**观测到 401 就会写这份记录（``_invalidate_credentials``）⇒ 晚一步
    # 装上，「启动之后 token 被吊销」这条事实就一个字都不落盘，而**没有任何东西会
    # 报错**（:func:`credential_health.install_credential_failure_recorder` 的
    # docstring 说的正是这一条）。
    try:
        credential_health.install_credential_failure_recorder(
            credential_health.CredentialFailureRecorder(_bridge_dir())
        )
    except Exception as exc:  # noqa: BLE001 - 排障记录绝不该决定桥的生死
        logger.warning(
            "credential-failures: 记录器未装配（%s: %s）—— 不影响桥的轮询",
            type(exc).__name__, exc,
        )
    #: 「这个平台为什么没能进来」—— ``usable == 0`` 时它就是那条记录的 ``detail`` 来源。
    #: ⚠️ **只在那些 ``continue`` 分支里写** ⇒ 而能 attach 的早就把 ``usable`` 加上去
    #: 了 ⇒ 所以 ``usable == 0`` 时它**必然覆盖了配置里的每一个平台**。
    unusable_reasons: dict[str, str] = {}
    #: 「这一轮真的 attach 上去了」的平台键（:func:`~opencode_bridge.health.platform_key`
    #: —— ⛔ 必须与 :meth:`BridgeCore.start` 给 ``startup_probes`` 用的**同一个**来源，
    #: 否则集合相减会把"探测过的"误判成"没探测的"）。
    #:
    #: ⚠️ 它存在的唯一理由：**只有 telegram 会 ``report_startup_probe``**（只有它调
    #: ``getMe``）⇒ 其余平台压根不往 ``platforms`` 里写条目 ⇒ ``--status`` 上恒为
    #: 「无记录」，用户**分不清**是配置坏了还是这个平台压根不上报。⇒ 用
    #: ``attach 过`` 减 ``上报过`` 反推出来，让「不做探测」有一档可读的落点。
    #: ⛔ **这是推导、不是名单**：新增第 14 个平台不需要改这里（见
    #: :func:`opencode_bridge.health.add_platforms_without_startup_probe`）。
    #: ⛔ 它**不产生任何网络请求** —— 只用"谁没说话"这个已经发生的事实。
    attached_platforms: list[str] = []
    for name, entry in list((cfg.adapters or {}).items()):
        try:
            # ⚠️ **必须过** ``adapter_scoped_config``：适配器只看得到自己的子树，
            # 而 ``pairing_secret`` / ``config_version`` 是**顶层**标量 —— 不投影
            # 进去，闸门就永远读不到（``--status`` 那条路径同样投影，两边必须一致）。
            adapter = build(name, adapter_scoped_config(cfg, entry), core)
        except KeyError:
            logger.warning("unknown adapter %r in config; skipped", name)
            unusable_reasons[str(name)] = "不是已注册的适配器键"
            continue
        except Exception as exc:
            logger.exception("failed to build adapter %r; skipped", name)
            unusable_reasons[str(name)] = "构造失败（%s: %s）" % (
                type(exc).__name__, exc,
            )
            continue
        token = getattr(adapter, "bot_token", None)
        if token is not None and not str(token).strip():
            logger.warning("%s: bot_token missing; adapter skipped", name)
            unusable_reasons[str(name)] = "bot_token 为空"
            continue
        core.attach(adapter)
        attached_platforms.append(health.platform_key(adapter))
        usable += 1

    if usable == 0:
        # ⚠️ 这条路在 discover_endpoint **之后**、``core.start()`` **之前** ⇒ 它同样
        # 曾经完全不写记录，而盘上留下的是上一次**成功启动**的结论（见
        # :func:`_record_bridge_refusal` 的 docstring）。注意此处预检**已经过了**：
        # 凭据是齐的，只是构造不出来 ⇒ 结论绝不能写成"未探测／你没填 token"。
        _record_bridge_refusal(cfg, REFUSAL_STAGE_NO_USABLE_ADAPTER, unusable_reasons)
        print(NO_ADAPTER_MESSAGE, file=sys.stderr)
        client.close()
        if inbox is not None:
            inbox.close()
        return 1

    core.start()
    # ⚠️ **整份写一次，且只在这一处写**：``core.start()`` 刚把每个适配器的启动探测
    # 结论收进 ``core.startup_probes``（见 :meth:`BridgeCore.start`），而
    # ``_bridge_dir()`` 只有这里知道 —— 适配器不知道、本类也不该自己推一遍。
    #
    # ⚠️ ⚠️ **「已 attach 却没上报」的那些平台也要补一条**（只有 telegram 会
    # ``report_startup_probe`` ⇒ 其余平台的 ``startup_probes`` 里压根没有它们）：
    # 不补的话 ``--status`` 上它们恒为「无记录」，而那一段措辞是**通用**的
    # ⇒ 用户分不清「配置坏了」与「这个平台压根不上报」。
    # ⛔ 补的这一档由 :func:`~opencode_bridge.health.add_platforms_without_startup_probe`
    # 造，是**推导**（``attach 过`` 减 ``上报过``）⇒ **新增平台不用改这里**。
    # ⛔ 它**不做任何网络请求**：⚛️「构造成功」≠「凭据有效」，替别人编一个 ``ok``
    # 就是伪造观测。
    #
    # ⚠️⚠️ **保护必须包住「实参求值」，不只是那次调用**（实测回归，2026-10-06）：
    # ``record_startup_probes`` 内部兜住了写盘失败，但 ``core.startup_probes`` 这个
    # **实参**是在它外面求值的 —— core 若是鸭子类型替身（测试里就有两个刻意最小的
    # ``patch`` 替身）没有这个属性，``AttributeError`` 就在 ``core.start()`` **成功之后**
    # 把桥打死 ⇒ **一个排障辅助功能有权杀掉正在运行的桥**，直接违反上面那句
    # 「写盘失败绝不打断启动」。
    # ⇒ 这里连求值一起兜住：退化行为是「记一条 warning + 本轮不写记录」，
    # **不是**让桥起不来。⚠️ 刻意**不用** ``getattr(core, "startup_probes", None)``：
    # 那会把「真实类改了名」变成静默写空记录；异常里带着 ``AttributeError``
    # 才是能让人查到的信号。⚠️ 同理**不**给 ``attached_platforms`` 加 ``getattr``
    # 兜底：它在上面那个循环里现填的，不是外部对象上的属性。
    try:
        probes = core.startup_probes
        health.record_startup_probes(
            _bridge_dir(),
            health.add_platforms_without_startup_probe(probes, attached_platforms),
        )
    except Exception as exc:  # noqa: BLE001 - 排障记录绝不该决定桥的生死
        logger.warning(
            "platform-health: 本轮探测结论未落盘（%s: %s）—— 不影响桥的运行",
            type(exc).__name__,
            exc,
        )
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

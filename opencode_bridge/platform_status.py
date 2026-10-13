"""状态视图域（--status / --check 的渲染与装配）。

第七刀自 ``__main__.py`` 逐字移植；行为零变化，AST 同一性由拆分脚本断言。
原文件顶部再进口本模块全部名字，既有 import 路径与 ``cli.X`` 属性语义不变。
"""

from __future__ import annotations
import json
import os
import time
from .adapters import build
from .allowlist import resolve_allowlist
from .config import Config, adapter_scoped_config
from .instance_lock import pid_is_alive
from .opencode_client import OpenCodeClient, discover_endpoint
from .pairing import empty_allowlist_is_open
from .subscription_health import read_subscription_status
from .subscription_status_view import render_subscription_status
from .subscription_supervisor import SubscriptionStatus
from .config_readiness import _missing_required_keys, _status_platform_keys
from .startup_probe_view import _print_last_start_probes
from .status_failure_view import (
    _print_last_outbound_failures,
    _print_last_runtime_credential_failures,
)
from .text_width import _dwidth, _pad
from .bridge_runner import _bridge_dir


def _pid_alive(pid: int) -> bool:
    """进程是否存活。委托给 :func:`instance_lock.pid_is_alive`，避免两处实现。"""
    return pid_is_alive(pid)


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

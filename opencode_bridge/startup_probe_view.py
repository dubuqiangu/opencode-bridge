"""启动探测记录视图（从 ``__main__.py`` 逐字移植）。

渲染 ``--status`` 的「上次启动探测」一段：无记录时的逐字文案
:data:`NO_START_PROBE_TEXT` 与 :func:`_print_last_start_probes`。
只读 ``health`` 落盘记录并 ``print``，不触任何装配。

Provenance: 2026-10-12 §5 ``__main__.py`` 专轮（装配 vs 视图渲染轴向，exp-68 裁定）。逐字移植 + ``__main__`` 顶部再进口，行为零变化。
"""
from __future__ import annotations

import time

from . import health
from .text_width import _dwidth, _pad


#: 「上次启动时没有探测记录」时的**逐字**文案。
#: ⛔ 它必须既不像"正常"也不像"失败"：那是"**没验过**"，不是"验过了、没问题"。
#: 措辞刻意把两种可能都说出来（"尚未以当前配置启动过" / "从未启动过"），
#: 因为盘上没有记录时**分不出是哪一种** —— 而猜一个就是在编造（AGENTS.md §8）。
NO_START_PROBE_TEXT = "无记录 —— 桥尚未以当前配置启动过，或从未启动过"


def _print_last_start_probes(
    rows: list[tuple[str, str, bool, bool, dict]],
    bridge_dir: str,
) -> None:
    """打印「上次启动那一刻，各平台探测出了什么」。

    ⚠️ 这一段与上面那张表**回答的不是同一个问题**，所以必须分开说：

    * 「渠道配置与能力」表答"凭据齐不齐" —— 纯本地可判（它看的是
      ``required_tokens`` 全部非空）；
    * 这一段答"上次启动时平台**认不认**这个凭据" —— 只有平台能判（Telegram 的
      ``getMe``、Slack 的 ``auth.test``）。

    两者分开是因为**失效方式不同**：token 打错 / 被吊销 / 网络被墙时，那张表仍会
    显示「已配置 / 就绪」，而桥实际上一条消息都收不到 —— 这正是本段存在的理由。

    ⚠️ **纯本地读盘**：不联网、不构造适配器（``--status`` 必须是"网络坏了也能看"
    的那条路）。结论由 :mod:`opencode_bridge.health` 在启动那一刻写好。

    ⚠️ **措辞必须带时效性**：这是「**上次启动时**」的结论，不是实时探测。
    ⚠️ 「启动」指的是**尝试启动**（两条拒绝启动的路也会记一条
    :data:`~opencode_bridge.health.VERDICT_NOT_STARTED`，见 :func:`_record_bridge_refusal`）。
    **唯一的例外**是「已有另一个实例在运行」：那次**不写**记录 —— 覆盖掉运行中那个
    实例写下的好结论，比留着一条旧的更坏。那条例外必须**在下面说出来**，
    否则「上一次启动尝试」这句话就会在这一种情形下说假话。

    ⚠️⚠️ **本段必须能看见盘上**任何**平台级结论 —— 包括未注册键的**
    （实测缺陷，2026-10-06）。用户把 ``telegram`` 拼成 ``telegramm`` 时，桥在构造
    循环里抛 ``KeyError`` 并以**那个键**把「不是已注册的适配器键」落盘，而上面那张
    「渠道配置与能力」表**只列注册表里的平台**、压根没有它那一行 ⇒ 而本段存在的
    **唯一理由**就是回答"桥为什么没起来"。⇒ 记在未注册键下的结论一律**自己成行**
    （⛔ 不折进下面那条全局 ⚠ 行 —— 那条行的"逐平台复读是噪音"的理由只对已注册的键
    成立），且本段**不许**在盘上真有结论时说出「没有得到任何平台的结论」。

    :param rows: :func:`_channel_config_rows` 的行，**复用**它而不重算平台清单与
        ``configured``（同一份判定只能有一处）。
    :param bridge_dir: :func:`_bridge_dir` 的推导结果 —— 记录就落在那里。
    """
    record = health.read_platform_health(bridge_dir)
    recorded = health.recorded_at(record)
    recorded_platform_keys = tuple(health.platforms_in_record(record))
    # 只列**已配置**或**上次探测过**的平台：把十三个平台各写一行「无记录」既吵
    # 又是废话（没配的平台压根不该被启动过）。而"上次探测过"的那些必须留着 ——
    # 用户把它移出配置之后，正是最需要看见"上次启动时说它是好的"。
    probed = set(recorded_platform_keys)
    #: 注册表里那些键（``rows`` 是**唯一**的平台清单，这里不重算一份）。
    registered_keys = {key for key, _label, _cfg, _inbound, _caps in rows}
    listed = [
        (key, label)
        for key, label, configured, _inbound_ready, _caps in rows
        if configured or key in probed
    ]
    # ⚠️⚠️ **盘上的键不限于已注册的平台名**（实测缺陷，2026-10-06）：配置里把平台键
    # 拼错（``telegram`` → ``telegramm``）时，桥走「有像凭据的字段就别凭空拦住用户」
    # 那条分支过了预检（见 :func:`_has_configured_adapter`），随后在构造循环里抛
    # ``KeyError`` ⇒ ``unusable_reasons["telegramm"] = "不是已注册的适配器键"`` ⇒
    # :func:`_record_bridge_refusal` 以**那个键**把它写进盘。
    # ⇒ 它**不在** ``rows`` 里，而上面那张「渠道配置与能力」表也压根没有它那一行 ⇒
    # 不并进来的话，「不是已注册的适配器键」这句话在这个视图里**一个字都显示不出来**
    # —— 而这一段存在的**唯一理由**正是回答「桥为什么没起来」。
    # ⇒ 实测（2026-10-06）那时的输出要么是一句「无记录」，要么是一句**假的**
    # 「盘上有记录，但这次启动没有得到任何平台的结论」（盘上恰好有一条平台级结论，
    # 而且它就是唯一能解释桥为什么没起来的那条）。
    # label 取**键本身**：没有适配器类可以问 label，而用户要认的恰恰是自己敲错的那串。
    listed += [
        (key, key) for key in recorded_platform_keys if key not in registered_keys
    ]
    # 「桥这一轮没起来」是**全局**的一件事（不是某个平台的结论）⇒ 由下面那条 ⚠ 行
    # 说一次，并且**不带平台行**：空模板那种 13 个平台全被拒的情况，逐平台再写一遍
    # 就是一屏复读，而它们各自的「缺什么」上面那张「渠道配置与能力」表已经在说了。
    refused = None
    platform_rows = []
    for key, label in listed:
        probe = health.probe_from_record(record, key)
        if (
            probe is not None
            and probe["verdict"] == health.VERDICT_NOT_STARTED
            and key in registered_keys
        ):
            # ⚠️ 记**第一个**（注册表顺序 = 上面那张表的顺序）：13 个平台各说一遍是
            # 复读，而它们各自的「缺什么」上面那张表已经在说了。
            #
            # ⚠️⚠️ 而「复读」这个理由**只对已注册的键成立**：上面那张表列的是注册表
            # 里的平台，一个**未注册**的键在那里一个字都没有 ⇒ 把它折进这条全局 ⚠ 行
            # 等于把「不是已注册的适配器键」连同它的键一起吞掉（先到的那条已注册键
            # 还会把它挤掉，于是那一轮它连 ⚠ 行都上不了）⇒ 所以未注册的键**一律自己
            # 成行**，⛔ 不参与这个「记第一个」。
            if refused is None:
                refused = probe
            continue
        platform_rows.append((label, probe))
    print("")
    print("== 上次启动时的探测结论 ==")
    print("  说明：以下结论取自**桥上一次启动尝试那一刻**的结果（落盘在")
    print("        platform-health.json）：桥起来了就写平台探测的答复；桥**拒绝启动**")
    print("        就写「桥未启动」并说明原因，**不会**留下一条上一轮的好消息。")
    print("        仅「已有另一个实例在运行」这一次**不写**记录（那种情况下盘上仍是")
    print("        那个运行中的实例启动时写下的结论）。")
    print("        **不是现在的连接状态，也不是实时探测**。")
    if recorded is not None:
        print(
            "  记录时间 : "
            + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(recorded))
        )
    if refused is not None:
        print("  ⚠ " + health.describe_verdict(refused))
    if not platform_rows:
        # ⛔ 区别「压根没有记录」与「有记录、但里面没有平台级结论」：后者是桥刚
        # 拒绝启动、或它启动了一个都不上报结论的适配器 —— 说成"没有任何探测记录"
        # 会让用户以为盘上是空的，而它其实刚被写过。
        #
        # ⚠️⚠️ **判据是「盘上真的有平台级结论」，⛔ 不是「有没有已注册平台」**
        # （实测缺陷，2026-10-06）。这两件事在缺陷现场是**反的**：文件存在、
        # 而它的键未注册（``telegramm``）时，旧判据（``listed`` 为空 ⇒ 走这一支）
        # 恰好落进「没有得到任何平台的结论」—— **而盘上有一条**，并且它是唯一能解释
        # 「桥为什么没起来」的那条。⇒ 改问 ``recorded_platform_keys``（记录自己说了
        # 什么），而不是 ``listed``（它混进了"这个平台配没配"这一层与记录无关的过滤）。
        # 上面那个 ``listed`` 现在并入了未注册的键，所以这条分支**只在盘上真的没有
        # 任何平台级结论时**才走得到 —— 也就是说那句话不再是一句断言，而是**事实**。
        if not recorded_platform_keys:
            print(
                "  （没有已配置的平台，也没有任何探测记录）" if record is None
                else "  （盘上有记录，但这次启动没有得到任何平台的结论）"
            )
        return
    name_w = max(_dwidth("平台"), max(_dwidth(label) for label, _p in platform_rows)) + 2
    for label, probe in platform_rows:
        if probe is None:
            print("  " + _pad(label, name_w) + NO_START_PROBE_TEXT)
            continue
        line = "上次启动 " + health.describe_verdict(probe)
        if probe["verdict"] == health.VERDICT_OK and recorded is not None:
            # 只给「正常」配时间戳：它是**唯一**可能被误读成"现在是好的"的那一行。
            line += "（%s）" % time.strftime("%m-%d %H:%M", time.localtime(recorded))
        print("  " + _pad(label, name_w) + line)

"""桥**上次启动时**各适配器探测结论的落盘与读取。

这个模块只做一件事：**记录与读取「上次启动时每个适配器探测出了什么」**。

为什么结论要落盘，而不是每次状态查询时重新探测
===============================================

``--status`` 与 ``--setup --json`` 是**纯本地**视图：它们不联网、不建适配器、
不起轮询 —— :func:`opencode_bridge.__main__.run_check` 的 docstring 把这条契约
写死成「no sessions, **no adapters**」。而"token 现在还有效吗"**只有平台能回答**
（Telegram 的 ``getMe``、Slack 的 ``auth.test`` 都是网络请求）。

把探测塞进状态视图，等于让一条**排障命令**依赖网络：**网络被墙时，报错本身也发不
出来**，用户拿到的还是零线索 —— 正是本模块要消灭的那种静默。所以结论在**启动那一
刻**取好、落在这里，状态视图只读盘上那份。

⚠️ 代价是这份结论**有时间戳**：它是「上次启动时」的判断，不是实时状态。
所以**每个读它的地方都必须把这件事说出来**（见 :func:`describe_verdict` 与
``__main__.run_status`` 那一段的措辞）。把一份三天前的结论显示成"正常"，
比不显示更坏 —— 它会让用户以为"现在也是好的"。

落盘形态
========

::

    {"recorded_at": <epoch float>,
     "platforms": {"<平台键>": {"verdict": "ok"|"failed"|"skipped"|"not_started",
                               "code": <int|str|省略>,
                               "detail": "<脱敏后的一行>"}}}

**四档 verdict**（:data:`VERDICT_OK` / :data:`VERDICT_FAILED` /
:data:`VERDICT_SKIPPED` / :data:`VERDICT_NOT_STARTED`）：

* ``ok`` —— 探测通过（Telegram 的 ``getMe`` 返回 ``ok``）。
* ``failed`` —— 探测失败，且**平台给了原因**（Telegram 的 ``error_code`` /
  ``description``）或传输层压根没拿到状态码。
* ``skipped`` —— **这个平台压根没探测**（连要验的东西都没有，比如 bot_token 没填）。
  ⛔ 它**不是** ``ok``：状态视图必须能把"没验"与"验过了、没问题"分开说，
  否则"没验"就会被读成"没问题"。
* ``not_started`` —— **桥这一轮压根没起来**（配置里没有任何可用适配器），
  于是**一条探测都没发生过**。见下面「拒绝启动也要落一条结论」。

⚠️ **没有记录 ≠ 成功**：读不到文件、或某个平台不在 ``platforms`` 里，返回的
都是 ``None`` —— 调用方必须把它显示成"无记录"，而**不是**"正常"。

拒绝启动也要落一条结论
======================

⚠️ 桥有两条**拒绝启动**的路径，而它们**曾经完全不写记录**：预检
（:func:`opencode_bridge.__main__._has_configured_adapter` 为否，在
``discover_endpoint`` **之前**）与 ``usable == 0``（在 ``discover_endpoint``
**之后**、``core.start()`` **之前**）。⇒ 盘上留下的就是**上一次成功启动**的
那条 ``ok``（内容与 mtime 都不变），而 ``--status`` 会把它与「未配置」并排显示
——**用户刚把配置改坏、桥拒绝启动时，看到的仍是上一轮的好消息**。

⇒ 两条路径现在都落一条 :data:`VERDICT_NOT_STARTED`（那份结论由
:func:`bridge_refusal_probes` 造出，而**落盘仍然只有**
:func:`record_startup_probes` 这一个入口）。

⚠️ **为什么不给它复用 ``skipped``**：那是两件不同的事，而 ``skipped`` 的语义是
"**这个平台**没东西可验"。在 ``usable == 0`` 那条路上预检**已经过了**（凭据是齐的，
只是构造不出来）⇒ 说"未探测"会把用户引到"你没填 token"这个**错的**方向上去。
⇒ 新增一档，让 ``--status`` 与 ``--setup --json`` **自带**「桥没起来」这个语义，
消费方不必去解析 ``detail`` 才知道那条不是平台级结论。
⛔ 既有三档**一个字没改**（只是多了一档）：仓库外的 ``bridge_setup`` 按**值**
断言现有取值。

安全红线
========

⚠️ **落盘内容里绝不许出现凭据片段**。``detail`` 一律过
:func:`opencode_bridge.redaction.default_redactor` 的 ``scrub``（按形状洗四类值），
落盘前**再**整体过一遍 ``scrub_persisted_value``（:mod:`opencode_bridge.state`
给 ``state.json`` 用的那道关卡）。理由不是洁癖：这份文件落在 bridge 目录里，与
``config.json`` 同一层，用户贴日志/贴 issue 时**整目录打包**的概率很高，而
Telegram 的 ``description`` 是平台回的自由文本 —— 它**可能**带着 token。

为什么整份**替换**而不是与上一份合并
====================================

合并会让**被移出配置**的平台永远显示一条早就过期的结论，而那与「上次启动」这个
说法直接矛盾（用户会拿三天前的成功去推断今天）。整份替换的语义只有一句话：
**盘上这份 = 最近一次启动的全部结论**，没有别的。

⚠️ 一次启动里**没有任何适配器探测**（比如只配了 irc）也会写一份空的
``platforms`` —— 这是对的：那正是"这次启动什么也没验到"这个事实。
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Iterable, Mapping, Optional

from .pairing_cli import write_config_atomically
from .redaction import default_redactor

__all__ = [
    "PLATFORM_HEALTH_FILE_NAME",
    "VERDICT_OK",
    "VERDICT_FAILED",
    "VERDICT_SKIPPED",
    "VERDICT_NOT_STARTED",
    "VERDICTS",
    "bridge_refusal_probes",
    "describe_verdict",
    "normalize_verdict",
    "platform_key",
    "probe_after_start",
    "probe_from_record",
    "read_platform_health",
    "record_startup_probes",
]

logger = logging.getLogger("opencode_bridge.health")

#: 落盘文件名，落在 ``bridge_dir`` 下（目录由调用方按
#: :func:`opencode_bridge.__main__._bridge_dir` 的推导给出 —— **那套推导只有一份**，
#: 本模块不自己再写一遍，否则"状态视图读的"与"启动时写的"会指向两个目录）。
PLATFORM_HEALTH_FILE_NAME = "platform-health.json"

#: 四档 verdict。名字用完整单词而不是 ``OK`` / ``FAIL`` —— 见名知意。
#: ⚠️ :data:`VERDICT_NOT_STARTED` 答的是**桥**（这一轮压根没起来），
#: 而前三档答的是**某个适配器** —— 混用会把用户引到错的方向上（见模块 docstring）。
VERDICT_OK = "ok"
VERDICT_FAILED = "failed"
VERDICT_SKIPPED = "skipped"
VERDICT_NOT_STARTED = "not_started"
VERDICTS = (VERDICT_OK, VERDICT_FAILED, VERDICT_SKIPPED, VERDICT_NOT_STARTED)

#: ``detail`` 是**一行**说明（:attr:`~opencode_bridge.adapters.base.Adapter.startup_verdict`
#: 的 ``detail`` 键）。上限截断的理由：Telegram 的 ``description`` 由服务端决定
#: 长度，而这份内容会被 ``--status`` 的一行表格直接展示 —— 不截断就能把表撑烂。
MAX_DETAIL_CHARS = 300


def _one_line(text: Any) -> str:
    """压成**单行**并截断。

    换行必须压掉：``detail`` 会被 ``run_status`` 逐平台打印成一行，而堆栈/多行
    描述会把那几行之后的所有输出顶成一个不可读的块。截断后再补一个省略号，
    让"这里被截了"是显式的。
    """
    flat = " ".join(str(text or "").split())
    if len(flat) <= MAX_DETAIL_CHARS:
        return flat
    return flat[:MAX_DETAIL_CHARS] + "…"


def _normalize_code(code: Any) -> Any:
    """错误码：``int`` 原样保留，其余一律转成**脱敏过**的字符串。

    保留 ``int`` 是因为 Telegram 的 ``error_code`` 本来就是整数，而
    ``--status`` 要显示 ``code=401``；非整数（``"?"``、``None``、``0.0``）转字符串
    是因为 JSON 消费者不该猜"这个键有时是数有时是串"。
    """
    if code is None:
        return None
    if isinstance(code, bool):          # bool 是 int 的子类，不当错误码用
        return str(code)
    if isinstance(code, int):
        return code
    scrubbed = default_redactor().scrub(_one_line(code))
    return scrubbed if scrubbed else None


def normalize_verdict(verdict: Any, *, code: Any = None, detail: Any = "") -> dict:
    """把上报方给的结论规范化成**唯一**的落盘形态 ``{verdict, code, detail}``。

    规范化**只有这一处** —— 上报（适配器）、落盘（:func:`record_startup_probes`）、
    读取（:func:`probe_from_record`）共用它，所以"日志里说的"、
    "盘上写的"、"状态视图读的"三者不可能对不上。

    ⚠️ **认不出来的 ``verdict`` 一律按 :data:`VERDICT_FAILED` 记**，并打一条
    warning。上报方说了句我们读不懂的话时，绝不能默认成"好" —— 那正是本任务要
    消灭的那类静默（信息产生出来了，却被当成好消息）。
    """
    text = str(verdict or "").strip().lower()
    resolved = text if text in VERDICTS else VERDICT_FAILED
    if resolved != text:
        logger.warning(
            "platform-health: 认不出的 verdict %r，按 %s 记 —— 绝不当成好",
            verdict, VERDICT_FAILED,
        )
    entry: dict = {"verdict": resolved}
    normalized_code = _normalize_code(code)
    if normalized_code is not None:
        entry["code"] = normalized_code
    scrubbed_detail = default_redactor().scrub(_one_line(detail))
    if scrubbed_detail:
        entry["detail"] = scrubbed_detail
    return entry


def describe_verdict(entry: Mapping) -> str:
    """把一条结论渲染成给人看的一小段（``--status`` 与日志共用这一份措辞）。

    ⚠️ 这里**只说结论本身**，不说"什么时候" —— 时效性由调用方（``--status`` 的段
    标题）负责说出来。混在一行里会让"三天前的成功"读起来像"现在是好的"。
    """
    verdict = str((entry or {}).get("verdict") or "")
    detail = str((entry or {}).get("detail") or "")
    if verdict == VERDICT_OK:
        return "正常"
    if verdict == VERDICT_SKIPPED:
        return f"未探测 —— {detail}" if detail else "未探测"
    if verdict == VERDICT_NOT_STARTED:
        # ⚠️ 措辞里必须自带「**桥**没起来」这个主语：``skipped`` 那行说的是
        # 「这个平台没验」，而这一行说的是「这一轮压根没有桥在跑」。
        # 两者指向的排查方向完全不同（前者去看凭据，后者去看配置有没有适配器）。
        return f"桥未启动 —— {detail}" if detail else "桥未启动"
    code = (entry or {}).get("code")
    # 没有平台错误码时**不硬凑一个括号**，而是留一个空格 ——
    # ``失败 —— <detail>`` 比 ``失败（无错误码）—— <detail>`` 短，
    # 也不假装"我们问过了、平台没给码"。
    if detail:
        return f"失败（code={code}）—— {detail}" if code is not None else f"失败 —— {detail}"
    return f"失败（code={code}）" if code is not None else "失败"


def platform_key(adapter: Any) -> str:
    """适配器在盘上/状态视图里的**平台键**。

    与 ``--setup --json`` / ``--status`` 列平台用的是**同一个**来源
    （:attr:`~opencode_bridge.adapters.base.Adapter.name`）—— 两边算出不同的键
    的话，"上次启动失败"就永远落不到用户看到的那一行上。
    """
    return str(getattr(adapter, "name", "") or type(adapter).__name__)


def probe_after_start(
    adapter: Any,
    start_result: Any = None,
    start_error: Optional[BaseException] = None,
) -> Optional[dict]:
    """一个适配器 ``start()`` 之后的结论；``None`` = **它没有上报过任何探测**。

    三种来源，按可信度排：

    1. 适配器自己通过
       :meth:`~opencode_bridge.adapters.base.Adapter.report_startup_probe` 上报的
       :attr:`~opencode_bridge.adapters.base.Adapter.startup_verdict` —— 平台自己
       知道得最清楚（它拿到了 ``error_code`` 与 ``description``）。
    2. ``start()`` 抛异常 ⇒ :data:`VERDICT_FAILED`，``detail`` 用异常信息。
       ⚠️ 这一条**不能省**：抛异常的适配器根本没有机会自己上报，而"抛了异常"正是
       最需要被用户看见的那种失败。
    3. ``start()`` 返回 ``False`` ⇒ :data:`VERDICT_FAILED`。判据用
       ``is False`` 而不是"假值"：现有适配器的 ``start()`` 一律返回 ``None``，
       而 ``None`` 是**正常**的（``Adapter.start`` 的签名就是 ``-> None``）——
       用假值判会把每个正常适配器都报成失败。

    ⚠️ 本函数**只产出结论，绝不落盘**：适配器不知道 bridge 目录在哪（落盘由
    :func:`record_startup_probes` 负责，那才是运行器的事）。
    """
    if start_error is not None:
        return normalize_verdict(
            VERDICT_FAILED,
            detail=f"start() 抛出 {type(start_error).__name__}: {start_error}",
        )
    reported = getattr(adapter, "startup_verdict", None)
    if isinstance(reported, Mapping):
        return normalize_verdict(
            reported.get("verdict"),
            code=reported.get("code"),
            detail=reported.get("detail"),
        )
    if start_result is False:
        return normalize_verdict(VERDICT_FAILED, detail="start() 返回 False")
    return None


def bridge_refusal_probes(reason: str, platform_reasons: Mapping[str, str]) -> dict:
    """「桥拒绝启动」这一轮要落盘的那份结论（**每个平台一条**）。

    :param reason: 这一轮**为什么**没起来（一句话，全局的）：预检没过 /
        没有任何适配器构造成功。
    :param platform_reasons: ``{平台键: 该平台自己的原因}``（缺什么 / 为什么构造不出来）。
    :return: 可直接交给 :func:`record_startup_probes` 的 ``{平台键: 结论}``。

    ⚠️ **每个平台的 ``detail`` 是「全局原因 + 它自己的那一条」，而不是把整个清单
    抄一遍**：:data:`MAX_DETAIL_CHARS` 会截断，一份 13 平台的清单抄 13 遍的结果是
    **靠后的平台整条被截掉**（实测：``a2a`` 的 ``bind_port`` 就这么消失了），
    而用户恰恰是照着**自己那一行**去找该填哪个键的。

    ⛔ 本函数**不落盘**：落盘只有 :func:`record_startup_probes` 那一个入口。
    """
    return {
        str(key): {
            "verdict": VERDICT_NOT_STARTED,
            "detail": "%s（%s：%s）" % (reason, key, text),
        }
        for key, text in (platform_reasons or {}).items()
    }


def record_startup_probes(bridge_dir: str, probes: Mapping) -> Optional[str]:
    """把这一轮**全部**适配器的结论写一次 ``platform-health.json``。

    :param bridge_dir: 由调用方（``__main__``）按 :func:`~opencode_bridge.__main__._bridge_dir`
        的推导给出 —— 本模块**不自己再推一遍**。
    :param probes: ``{平台键: 结论}``，由 :func:`probe_after_start` 逐个产出。
    :return: 落盘路径；写不进去时 ``None``（**已记 warning**）。

    ⚠️ **单个平台坏了不许阻断其余平台**：逐个 try/except，一个条目坏掉只丢它自己。
    这与"整份替换"的语义一致 —— 记下一份缺项的结论，好过什么都不记。

    ⚠️ **写盘失败绝不打断启动**：这份记录是排障辅助，不是桥的运行前提。
    """
    platforms: dict = {}
    for key, entry in (probes or {}).items():
        try:
            platforms[str(key)] = normalize_verdict(
                (entry or {}).get("verdict"),
                code=(entry or {}).get("code"),
                detail=(entry or {}).get("detail"),
            )
        except Exception as exc:  # noqa: BLE001 - 一个平台坏掉不许吃掉其余的结论
            logger.warning(
                "platform-health: %s 的结论无法规范化（%s），其余平台照记", key, exc
            )
    document = {"recorded_at": time.time(), "platforms": platforms}
    path = os.path.join(str(bridge_dir or ""), PLATFORM_HEALTH_FILE_NAME)
    try:
        # 原子写**复用** pairing_cli.write_config_atomically（不复制它的函数体）：
        # 那是本仓库对"改用户目录里的文件"做替换的既有做法（mkstemp → json.dump
        # → flush → fsync → os.replace，异常时删临时文件）。截断一半的
        # platform-health.json 不会让用户拿到错东西，但会让下次 `--status`
        # 读不到记录 —— 正是本任务想消灭的那种"零线索"。
        # 唯一可察觉的差别：临时文件名前缀是 `.config-`，对用户不可见。
        write_config_atomically(
            path, default_redactor().scrub_persisted_value(document)
        )
    except Exception as exc:  # noqa: BLE001 - 落盘失败不许打断桥的启动
        logger.warning("platform-health: 写入 %s 失败（%s）—— 不影响桥的启动", path, exc)
        return None
    return path


def read_platform_health(bridge_dir: str) -> Optional[dict]:
    """读回整份记录；**没有记录 / 读不出来**都是 ``None``。

    ``None`` 的含义必须是「**没有记录**」—— 它**不代表成功**。调用方要把这个
    ``None`` 显示成"无记录"，而不能显示成"正常"（见模块 docstring）。

    :param bridge_dir: 同 :func:`record_startup_probes`，由调用方给出。
    """
    path = os.path.join(str(bridge_dir or ""), PLATFORM_HEALTH_FILE_NAME)
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            document = json.load(handle)
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001 - 手改坏的文件不该让 --status 崩
        logger.warning("platform-health: %s 读不出来（%s）—— 只当它没有记录", path, exc)
        return None
    return document if isinstance(document, dict) else None


def probe_from_record(record: Optional[Mapping], key: str) -> Optional[dict]:
    """从整份记录里取**一个平台**的结论；没有就 ``None``（= 没有记录）。

    读出来的东西**再过一次** :func:`normalize_verdict`：这份文件可能被用户手改
    过（它就在 bridge 目录里），而消费方不该为"文件里有个没见过的 verdict"去
    自己兜底。认不出来的 verdict 归一化成 ``failed``。
    """
    if not isinstance(record, Mapping):
        return None
    platforms = record.get("platforms")
    if not isinstance(platforms, Mapping):
        return None
    entry = platforms.get(str(key))
    if not isinstance(entry, Mapping):
        return None
    return normalize_verdict(
        entry.get("verdict"), code=entry.get("code"), detail=entry.get("detail")
    )


def recorded_at(record: Optional[Mapping]) -> Optional[float]:
    """记录时刻（epoch 秒）；没有 / 不是数字则 ``None``。"""
    if not isinstance(record, Mapping):
        return None
    stamp = record.get("recorded_at")
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
        return None
    return float(stamp)


def platforms_in_record(record: Optional[Mapping]) -> Iterable[str]:
    """记录里出现过的平台键（读取侧用；坏文件当空的）。"""
    if not isinstance(record, Mapping):
        return ()
    platforms = record.get("platforms")
    if not isinstance(platforms, Mapping):
        return ()
    return tuple(str(key) for key in platforms)
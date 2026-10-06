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

出站失败为什么**另开一份文件**
============================

同一个模块还记第二件事：**运行期**观测到的出站发送失败（适配器已经把它算成
:class:`~opencode_bridge.hooks.SendError` 的结构化分类，却从来没有人读 ——
见 :class:`OutboundFailureRecorder`）。它落在**另一个文件**
``outbound-failures.json``，⛔ **不是** ``platform-health.json`` 的子键。三个理由，
按承重程度排：

1. ⛔ **整份替换会把它抹掉。** :func:`record_startup_probes` 每次启动都
   ``write_config_atomically`` 一份 ``{"recorded_at", "platforms"}`` 把整个文件
   替换掉（这正是「盘上这份 = 最近一次启动的全部结论」这条语义）。同键共存 ⇒
   下次启动**顺手把上一轮运行期的失败记录删了**。而那份记录恰恰是用户最需要它
   的时候（用户是在"收不到回信"之后才去查的，此刻桥已经重启过一次）。
2. ⛔ **两个全量替换的写者共用一个文件 = 丢更新。** 两边都是 read-free 的整份
   写入，谁后写谁赢，前者的字段无声消失 —— 而消失的那一半是"排障记录"，
   没有任何东西会报错。
3. **时效语义不同，而 ``--status`` 的那一段自带声明。** ``platforms`` 答的是
   「**上次启动那一刻**平台认不认这个凭据」，写在这一段的开头；运行期的出站失败
   答的是「**上一次**发送失败是什么时候、为什么」。混在一份文件里，读者就得自己
   分清哪个键是哪个时刻 —— 而这一段文案的存在理由恰恰是**不**让读者去猜。

⇒ 所以是**两个平行的文件 + 两个平行的 ``--status`` 段**，且两份记录**互不覆盖**。
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Iterable, Mapping, Optional

from .hooks import SendError
from .pairing_cli import write_config_atomically
from .redaction import default_redactor

__all__ = [
    "NO_OUTBOUND_FAILURE_TEXT",
    "OUTBOUND_FAILURES_FILE_NAME",
    "PLATFORM_HEALTH_FILE_NAME",
    "VERDICT_OK",
    "VERDICT_FAILED",
    "VERDICT_SKIPPED",
    "VERDICT_NOT_STARTED",
    "VERDICTS",
    "OutboundFailureRecorder",
    "bridge_refusal_probes",
    "describe_outbound_failure",
    "describe_verdict",
    "normalize_outbound_failure",
    "normalize_verdict",
    "outbound_failure_from_record",
    "outbound_failures_in_record",
    "outbound_failure_recorded_at",
    "platform_key",
    "probe_after_start",
    "probe_from_record",
    "read_outbound_failures",
    "read_platform_health",
    "record_startup_probes",
]

logger = logging.getLogger("opencode_bridge.health")

#: 落盘文件名，落在 ``bridge_dir`` 下（目录由调用方按
#: :func:`opencode_bridge.__main__._bridge_dir` 的推导给出 —— **那套推导只有一份**，
#: 本模块不自己再写一遍，否则"状态视图读的"与"启动时写的"会指向两个目录）。
PLATFORM_HEALTH_FILE_NAME = "platform-health.json"

#: **运行期**出站失败记录的落盘文件名，与 :data:`PLATFORM_HEALTH_FILE_NAME` **并列**
#: （⛔ 不是它的子键）—— 理由见模块 docstring「出站失败为什么另开一份文件」。
OUTBOUND_FAILURES_FILE_NAME = "outbound-failures.json"

#: 「这个平台没有出站失败记录」时的**逐字**文案（``--status`` 那一段逐行显示）。
#:
#: ⛔ 它必须**既不像"正常"也不像"失败"**，理由与 :data:`NO_START_PROBE_TEXT` 同源：
#: 盘上没有记录时分不出"确实没失败过"与"这份记录还没被写过"，而把"没观测到"
#: 说成"一切正常"就是假话（那正是本任务要消灭的那类静默）。
#: 末尾那半句（"不代表此刻可达"）是承重的：没有记录**真的**推不出现在可达 ——
#: 桥可能压根没发过消息。
NO_OUTBOUND_FAILURE_TEXT = (
    "无记录 —— 自该记录建立以来未观测到出站失败（不代表此刻可达）"
)

#: :class:`~opencode_bridge.hooks.SendError` 的取值集合，用来判定落盘的 ``kind``
#: 是不是我们认识的分类。**认不出来的按** :data:`~opencode_bridge.hooks.SendError.UNKNOWN`
#: **记**，理由与 :func:`normalize_verdict` 对未知 ``verdict`` 的处置一样：读不懂的话
#: 绝不当成"没有原因"。
_KNOWN_SEND_ERRORS = frozenset(item.value for item in SendError)

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


# ======================================================================
# 运行期出站失败记录（**另开一份文件**；理由见模块 docstring）
# ======================================================================


def _normalize_epoch(value: Any) -> Optional[float]:
    """epoch 秒；``bool`` 不是时间戳、非数字一律 ``None``（理由同 :func:`recorded_at`）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def normalize_outbound_failure(
    kind: Any,
    detail: Any = "",
    *,
    retry_after: Any = None,
    at: Any = None,
    recovered_at: Any = None,
) -> dict:
    """把一次出站失败规范化成**唯一**的落盘形态。

    ::

        {"at": <epoch float>,
         "kind": "<SendError 的取值>",
         "detail": "<脱敏后的一行>",
         "retry_after": <float|省略>,
         "recovered_at": <epoch float|省略>}

    ⚠️ **与启动探测结论分开的两件事，措辞上不许混**（见模块 docstring）：
    ``at`` 答"**上一次**出站失败是什么时候"，``recovered_at`` 答"之后有没有成功发出去过"。
    ``recovered_at`` **缺失 = 还没成功过**，而它**不是**"现在一定还坏着" ——
    可能是压根没人再发消息（桥空闲）。所以读它的人必须把两种可能都说出来。

    ⚠️ ``kind`` 认不出来一律按 ``unknown`` 记：分类丢了也必须留下"有过一次失败"
    这个事实 —— 而把它归成某个具体类别才是编造。
    """
    raw_kind = str(getattr(kind, "value", kind) or "").strip().lower()
    entry: dict = {
        "at": _normalize_epoch(at) if at is not None else time.time(),
        "kind": raw_kind if raw_kind in _KNOWN_SEND_ERRORS else SendError.UNKNOWN.value,
    }
    if raw_kind and raw_kind not in _KNOWN_SEND_ERRORS:
        logger.warning(
            "outbound-failures: 认不出的失败分类 %r，按 %s 记 —— 绝不当成没有原因",
            kind, SendError.UNKNOWN.value,
        )
    scrubbed_detail = default_redactor().scrub(_one_line(detail))
    if scrubbed_detail:
        entry["detail"] = scrubbed_detail
    seconds = _normalize_epoch(retry_after)
    if seconds is not None:
        entry["retry_after"] = seconds
    recovered = _normalize_epoch(recovered_at)
    if recovered is not None:
        entry["recovered_at"] = recovered
    return entry


def describe_outbound_failure(entry: Optional[Mapping]) -> str:
    """把一条出站失败渲染成给人看的一段（``--status`` 与日志共用这一份措辞）。

    ⚠️ 这里**只说那一次失败本身**（分类 + 原因），**不说"什么时候"** ——
    时效性由调用方的段标题与时间戳负责。理由与 :func:`describe_verdict` 相同：
    混在一行里会让"昨天那次失败"读起来像"现在是坏的"。

    ``entry`` 为 ``None`` 时返回 :data:`NO_OUTBOUND_FAILURE_TEXT` ——
    ⛔ 那句**不是**"正常"，读它的人不许这么转述（理由见该常量的注释）。
    """
    if not isinstance(entry, Mapping):
        return NO_OUTBOUND_FAILURE_TEXT
    kind = str(entry.get("kind") or SendError.UNKNOWN.value)
    detail = str(entry.get("detail") or "")
    return f"{kind}：{detail}" if detail else kind


def read_outbound_failures(bridge_dir: str) -> Optional[dict]:
    """读回整份**运行期**出站失败记录；没有 / 读不出来都是 ``None``。

    ``None`` 的含义是「**没有记录**」，而它**不代表成功**（见
    :data:`NO_OUTBOUND_FAILURE_TEXT`）。

    ⚠️ 这里的坏文件处置与 :func:`read_platform_health` 一致：记一条 warning 当它
    没有记录，而不是让 ``--status`` 崩 —— 排障通道自己坏了已经够糟。
    """
    path = os.path.join(str(bridge_dir or ""), OUTBOUND_FAILURES_FILE_NAME)
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            document = json.load(handle)
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001 - 手改坏的文件不该让 --status 崩
        logger.warning(
            "outbound-failures: %s 读不出来（%s）—— 只当它没有记录", path, exc
        )
        return None
    return document if isinstance(document, dict) else None


def outbound_failure_from_record(
    record: Optional[Mapping], key: str
) -> Optional[dict]:
    """从整份运行期记录里取**一个平台**的出站失败；没有就 ``None``（= 无记录）。

    读取侧**再过一次** :func:`normalize_outbound_failure`：这份文件可能被用户手改过，
    而消费方不该为"文件里有个没见过的字段"自己兜底。
    """
    if not isinstance(record, Mapping):
        return None
    platforms = record.get("platforms")
    if not isinstance(platforms, Mapping):
        return None
    entry = platforms.get(str(key))
    if not isinstance(entry, Mapping):
        return None
    return normalize_outbound_failure(
        entry.get("kind"),
        entry.get("detail"),
        retry_after=entry.get("retry_after"),
        at=entry.get("at"),
        recovered_at=entry.get("recovered_at"),
    )


def outbound_failures_in_record(record: Optional[Mapping]) -> Iterable[str]:
    """运行期记录里出现过的平台键（坏文件当空的）。"""
    if not isinstance(record, Mapping):
        return ()
    platforms = record.get("platforms")
    if not isinstance(platforms, Mapping):
        return ()
    return tuple(str(key) for key in platforms)


def outbound_failure_recorded_at(record: Optional[Mapping]) -> Optional[float]:
    """这份运行期记录最后一次被写入的时刻（epoch 秒）；没有 / 不是数字则 ``None``。"""
    if not isinstance(record, Mapping):
        return None
    return _normalize_epoch(record.get("recorded_at"))


class OutboundFailureRecorder:
    """运行期出站失败记录的写入方：**状态变化时才写盘**。

    存在的理由：各适配器失败时都调
    :meth:`~opencode_bridge.adapters.base.Adapter._note_send_failure` 把结构化原因
    算出来存进 ``_last_send_error``，而**在它被造出来之前没有任何人读它** ——
    ``send_result`` 与 ``last_send_error`` 在生产代码里零个调用方。于是 agent 回消息
    失败时**用户那边什么都没有**（结构化的 ``FORBIDDEN`` / ``RATE_LIMITED`` /
    ``TIMEOUT`` 被算出来后直接丢掉）。本类就是那条缺失的通道。

    ## 落盘形态

    ::

        {"recorded_at": <epoch float>,
         "platforms": {"<平台键>": {"at": …, "kind": …, "detail": …,
                                    "retry_after": …, "recovered_at": …}}}

    ⚠️ 与 :func:`record_startup_probes` 的**整份替换**相反，这里是**读改写**：
    每个平台一条、彼此独立，必须保住别的平台已经记下的内容（启动探测那份文件
    之所以能整份替换，正是因为它的语义是"最近一次启动的全部结论"，而这里不是）。

    ## 为什么只在**状态变化**时写（节流的判据）

    出站失败可以**非常频繁** —— 对端限流、用户在批量操作、一次长答复被切成几十片
    而中途中断。每次失败都写一次盘会把一次网络抖动放大成每秒几十次
    ``mkstemp`` + ``fsync`` + ``os.replace``，**在排障功能自己的位置上**制造压力。

    ⇒ 判据是**状态机**而不是时间窗（时间窗只会让记录更不准，而不会更省）：

    ==========================  ======  ====================================
    观测                          写盘?   理由
    ==========================  ======  ====================================
    「健康 → 失败」（首次失败）   是      这就是要让人看见的那一次
    「失败 → 失败」（连续失败）   **否**  记录已经写着"这个平台在失败"，
                                    再写一遍不增加任何信息
    「失败 → 健康」（恢复）      是      不写就永远显示"正在失败"，
                                    而它其实已经好了 —— 那比不显示更坏
    「健康 → 健康」              否      压根没有记录要改
    ==========================  ======  ====================================

    ⇒ **上界是「每平台每次失败连击 ≤ 2 次写盘」**（进入 + 恢复）。这个界比任何
    时间窗都紧，且不需要一个会随时钟漂移的常量。

    ⚠️ **状态是进程内的，不从盘上恢复。** 所以桥重启后第一次失败会**再写一次**
    （把 ``at`` 刷新到本次运行）—— 那一次写盘是有用的（用户查的就是"重启之后还有
    没有在失败"），而它每个进程至多一次，不构成无限写盘。

    ⛔ **写盘失败绝不影响发送路径**：每个方法都自己兜住异常、记 warning、返回
    ``False``。这与 :func:`record_startup_probes` 的不变量同源 —— **排障记录
    绝不该决定桥的生死**。
    """

    def __init__(self, bridge_dir: str) -> None:
        self._bridge_dir = str(bridge_dir or "")
        #: 正处于「失败连击」中的平台（内存态；见上面「状态是进程内的」）。
        self._failing: set[str] = set()

    # --- 观测入口 -----------------------------------------------------
    def note_failure(
        self,
        platform: str,
        kind: Any,
        detail: Any = "",
        *,
        retry_after: Any = None,
    ) -> bool:
        """记一次出站失败；**真的写了盘**才返回 ``True``。

        :param platform: 平台键（:func:`platform_key` —— 与 ``--status`` 列平台的
            同一个来源，否则这条记录永远落不到用户看到的那一行上）。
        """
        key = str(platform or "")
        if not key:
            return False
        if key in self._failing:
            return False          # 失败连击中：记录已经写着"在失败"，不重复写
        self._failing.add(key)
        return self._write(key, normalize_outbound_failure(
            kind, detail, retry_after=retry_after,
        ))

    def note_success(self, platform: str) -> bool:
        """记一次出站成功（**仅当它结束了一段失败连击**才写盘）。

        ⛔ 它**不会**删掉那条失败记录，而是给它盖一个 ``recovered_at`` 时间戳。
        理由：发送恢复**不等于**那条失败的答复补发了 —— 平台压根没有"重投"原语，
        而用户真正要问的是"我刚才那条回信去哪了"。把记录删掉会让这个问题彻底无解。
        """
        key = str(platform or "")
        if not key or key not in self._failing:
            return False          # 没有失败连击在结束：没有记录要改
        self._failing.discard(key)
        existing = self._entry_of(key) or normalize_outbound_failure(
            SendError.UNKNOWN, "（未记录细节）",
        )
        existing["recovered_at"] = time.time()
        return self._write(key, existing)

    # --- 内部 ---------------------------------------------------------
    def _entry_of(self, key: str) -> Optional[dict]:
        """盘上这个平台**已经记着**的那条（读改写的前半段；坏文件当没有）。"""
        return outbound_failure_from_record(self._read(), key)

    def _read(self) -> Optional[dict]:
        return read_outbound_failures(self._bridge_dir)

    def _write(self, key: str, entry: Mapping) -> bool:
        """读改写一次整份记录；**任何异常都在这里兜住**，返回是否真写到盘上。

        ⚠️ 与 :func:`record_startup_probes` 一样复用
        :func:`~opencode_bridge.pairing_cli.write_config_atomically`，不复制函数体：
        截断一半的 JSON 会让下次 ``--status`` 读不到记录，而读不到记录正是本任务
        想消灭的那种"零线索"。
        """
        document = self._read()
        if not isinstance(document, dict):
            document = {}
        # 只留别的平台 —— 本平台那条由本方法**整体替换**，否则残留的
        # ``recovered_at`` 会挂在一次全新的失败上（说"这次失败已恢复"）。
        platforms = {
            name: value
            for name, value in dict(document.get("platforms") or {}).items()
            if str(name) != key and isinstance(value, Mapping)
        }
        platforms[key] = dict(entry)
        payload = {"recorded_at": time.time(), "platforms": platforms}
        path = os.path.join(self._bridge_dir, OUTBOUND_FAILURES_FILE_NAME)
        try:
            write_config_atomically(
                path, default_redactor().scrub_persisted_value(payload)
            )
        except Exception as exc:  # noqa: BLE001 - 落盘失败绝不打断发送
            logger.warning(
                "outbound-failures: 写入 %s 失败（%s）—— 不影响桥的发送", path, exc
            )
            return False
        return True

    # --- 测试与诊断 ---------------------------------------------------
    def failing_platforms(self) -> tuple[str, ...]:
        """当前处于失败连击中的平台（诊断用；``--status`` 读的是盘上那份）。"""
        return tuple(sorted(self._failing))
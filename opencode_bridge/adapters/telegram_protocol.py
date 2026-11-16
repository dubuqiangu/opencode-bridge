"""Lane B — Telegram 协议原语域（§5 第八刀，自 ``telegram.py`` 逐字移植）。

Telegram Bot API 的**协议限额与 wire 纯函数**：消息/回调字节上限、轮询与退避
节拍、凭据闸门退避阶梯，以及两个不依赖适配器状态的纯函数 —— 429
``retry_after`` 解析（:func:`_retry_after`）与内联键盘按钮序列化
（:func:`_button_row`）。

## 为什么拆（刀路依据 = 缝质量与零钉点，与第五刀 a2a 同型）

``telegram.py`` 1495 物理行里，模块级协议域与 ``TelegramAdapter`` 类体是两份
可分开的职责；消费面普查（2026-10-14）证实这 19 个名字在 tests/ 里全是
import 形态、零 ``mock.patch`` 钉点 ⇒ 缝干净，照 ``ws_protocol.py`` /
``a2a_protocol.py`` 先例成模块。

⚠️ ``telegram.py`` 顶部再进口块保既有 import 路径（7 个测试模块
``from opencode_bridge.adapters.telegram import …``）与 ``telegram.X`` 属性语义；
``status_failure_view.py`` 对 ``RUNTIME_CREDENTIAL_REVERIFY_LIMIT`` 的 ``:data:``
文档角色引用同样经再进口继续可解析。

⚠️ ``_button_row`` docstring 里的「本文件 ``edit()``」在移植后指
``telegram.py``（调用方所在文件）；docstring 逐字保留（AST 同一性），指称以
本段为准。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from ..hooks import Button

API_HOST = "api.telegram.org"
API_PORT = 443
MESSAGE_LIMIT = 4096          # Telegram max message length in characters
CALLBACK_DATA_LIMIT = 64      # inline callback_data limit in bytes
#: 部分发送（前段已上线、后段失败）时追加给读者的缺口标记 —— 用户拍板 2026-10-09
#: 「加缺口标记」：读者必须知道拿到的答复不完整，不能只有日志与 ``--status`` 知道。
SEND_GAP_MARKER_TEXT = (
    "—— ⚠️ 以上回复不完整：后续部分发送失败，未送达。如需完整内容请重新提问。"
)
MIN_SEND_INTERVAL = 1.2       # per-conversation send/edit throttle (seconds)
BACKOFF_INTERVAL = 2.0        # 一次 getUpdates 失败后的重试间隔（秒，恒定）
POLL_LONG_TIMEOUT = 25        # getUpdates long-poll seconds
POLL_TIMEOUT_UPPER_LIMIT = 50  # Bot API 的服务端挂起上限（50s）：超过即回落默认并告警
POLL_SOCKET_TIMEOUT = 40.0    # socket timeout must be > POLL_LONG_TIMEOUT
#: 空轮（``ok`` 但 ``result`` 为空）后的防御性节流（秒）。
#: 迁移前是 :meth:`TelegramAdapter._poll_once` 末尾的 ``_stop_event.wait(0.05)``：
#: 服务端立刻返回空批时不做任何等待会打爆 API。走传输层后它就是
#: ``PollingTransport`` 的 ``idle_sleep``（仍然是**可被 stop 打断**的
#: ``Event.wait``，不是 ``time.sleep``）。
EMPTY_ROUND_INTERVAL = 0.05
DEFAULT_SOCKET_TIMEOUT = 30.0
MAX_RETRY_AFTER = 60.0        # cap for Telegram 429 retry_after sleeps

#: Telegram 的 ``Unauthorized`` 码。**只有它**会让运行期的凭据失效判据成立。
#:
#: ⚠️ 为什么只认 401 而不认 403/404：Bot API 的 403 是"bot 被拉黑 / 无权访问该聊天"，
#: 404 是"方法或聊天不存在"，而 ``getUpdates`` 用的是**同一个 token** ⇒ 401 才是
#: "这个 token 现在不被承认"这一件事的机器可读形式。
#: ⛔ 而"非 401 的失败会不会也是 token 吊销"**未核实**（本机无外网凭据）⇒ 所以按
#: **语义**收窄，而不是"任何失败都当成凭据失效"（那会让一次网络抖动去惊动凭据闸门）。
UNAUTHORIZED_ERROR_CODE = 401

#: 运行期重新探 ``getMe`` 的**重试上界**（§5.1 第一刀后见
#: :meth:`TelegramCredentialLifecycle._reverify_runtime_credentials`）。
#:
#: ⭐ **为什么是 5 次**（2026-10-08 拍板时的取舍，写在这里是为了可复核）：
#: 这一段重试与启动段那个**永不放弃**的阶梯是**两件事**，判据不同 ——
#:
#: * 启动段永不放弃，是因为"用户在**进程活着**的期间第一次配 token"是常态；
#: * 运行段的触发条件是"**曾经好过、现在 401 了**" ⇒ 用户极可能正在**看日志**，
#:   而每一次重试都打一行 —— 无界的重试在这里等于**刷屏**，把真正的排障现场淹掉。
#:
#: ⇒ 所以运行段给一个**明确的次数上界**，超了之后那一行会**说清**「已停止重探、
#: 需要重启桥」并**保留** :attr:`TelegramCredentialLifecycle._runtime_credentials_invalid`（⇒ ``running``
#: 继续为假、入站继续不放行，**不会**因为放弃重探而偷偷恢复）。
#: ⇒ 5 次 × 最长一档 60s ≈ **5 分钟**的自动恢复窗口：覆盖"token 被临时吊销后
#: 几分钟内换回来"这一类，而不会让一个永久吊销的 token 被探上一整夜。
RUNTIME_CREDENTIAL_REVERIFY_LIMIT = 5

# ---------------------------------------------------------------------------
# 「凭据闸门」（``getMe`` 探测）的退避阶梯
# ---------------------------------------------------------------------------
# ⚠️ **初值是候选值，不是标定出来的**：它**复用**本仓库已有的 2s
# （:data:`BACKOFF_INTERVAL`，也正是 ``PollingTransport`` 在 ``min_backoff ==
# max_backoff`` 下唯一会取的那个数），**没有**任何实测数据支撑。
# ⇒ **标定它需要哪一组数**：**恢复时延的分布** —— 首次成功时打的那行
# 「第 N 次尝试 / 历时 T 秒」（:meth:`TelegramCredentialLifecycle._announce_credential_recovery`）
# 就是为了攒这组数：攒够 **≥20 个真实事件**、其中**至少 3 次**是「失败超过 5 分钟
# 才恢复」，再看 N 与 T 的 **p50 / p90**，再回头改这两个数。
# ⛔ 在那之前**不许**把它当成"测过了"。
# ⚠️ 「无抖动」同理是**借用**既有做法而不是结论：本仓库 5 套阶梯**一套都没有**
# 抖动，且 Telegram 的限流按 bot token 计、多实例之间不共享配额。
CREDENTIAL_PROBE_INITIAL_BACKOFF = 2.0
#: 封顶 60s = 传输层的默认值（``Transport.__init__`` 的 ``max_backoff``），
#: 也是 5 个适配器的既有取值 ⇒ 「永不放弃」的代价有界：最坏每分钟一次 ``getMe``。
CREDENTIAL_PROBE_MAX_BACKOFF = 60.0

# ---------------------------------------------------------------------------
# 连续失败到阈值之后：一个固定的永久失败冷却
# ---------------------------------------------------------------------------
# ⭐ 为什么这条线画在「连续 5 次」上（2026-11-09 拍板，写在这里为了可复核）：
#
# 上面那条阶梯只负责「添一点抖动」—— 它不回答**什么时候该停止把它当成抖动**。
# 一次超时、一次代理刚起、一次 DNS 没就绪，是**同一种**失败；而 token 打错、
# 被吊销、被墙，也都长得像「一次超时」。日志层却把两者拼成一个味道：
# 每一档一条 WARNING、永远「将继续按退避重试」⇒ 用户翻故障期的日志，分不出
# 「它下一条大概率会过」和「它接下来一小时都在打同一面墙」。
#
# 判据是可复核的：看这张凭据**连续**失败了几次、中间有没有一次成功。
# 有成功 ⇒ 归零（那是抖动）；没有 ⇒ 计数往上走 ⇒ 走到阈值 = 这张凭据现在不行。
#
# ⚠️ 阈值 5 的取舍（不是「五这个数吉利」）：
#   * 太小（2/3）⇒ 一次代理重启里的两三次连续超时就被标成"永久的"，而那种故障
#     确实会在十几秒里自己好；标得越早，这个标记越不可信。
#   * 太大（20/30）⇒ 到那时短阶梯早已封顶 60s 好几轮 ⇒ 标记本身没有新信息
#     （"封顶 60s"已经发生了），它只是把"已经晚了很久"再说一遍。
#   * 5 次 × 初值 2s 起翻倍 ⇒ 累计约 2+4+8+16 =
#     30 秒量级的等待之后落点，落得下"用户改了配置还没生效"那段观察期，
#     又短到用户还没开始怀疑"它是不是根本没在重试"。
CREDENTIAL_PROBE_PERMANENT_FAILURE_THRESHOLD = 5

#: 到了阈值之后的冷却（秒）：**固定**，⛔ 不再翻倍、⛔ 也**没有放弃点**。
#:
#: ⚠️ 为什么固定而不是继续翻倍：继续翻倍的终点还是某个封顶，而它唯一的效果是
#: 让「改完配置之后下一次探测」来得更晚 —— 那正是要修的那件事。固定冷却让
#: 「改配置 → 生效」之间的最坏时延有界（≤ 这一档），且这个界不随故障时长漂移。
#: ⚠️ 为什么是 ~60s 而不是更长：一次 ``getMe`` 的代价可忽略（见
#: :data:`CREDENTIAL_PROBE_MAX_BACKOFF` 的注释），而它是**唯一**能观测到
#: 「token 被换回来了」的信号 ⇒ 拉长冷却换来的只是「更晚发现好了」。
#: ⚠️ 它与 :data:`CREDENTIAL_PROBE_MAX_BACKOFF` 恰好同值（60s），但**不是同一个旋钮**：
#: 那个是短阶梯的**封顶**（还在翻倍），这个是到阈值之后的**恒值**（已经不再翻倍）
#: ⇒ 两者要能分开调、分开测。⛔ 不许互相引用（引用了就是「改一个顺带改另一个」）。
CREDENTIAL_PERMANENT_FAILURE_COOLDOWN = 60.0

#: 永久失败那一行日志的**标记**（转折点）。只在「连续失败达到阈值」那一档出现。
#:
#: ⚠️ 它不是「文案好看」：``tests/test_telegram_startup_probe.py`` 有一条守门按字面
#: 钉住**首条失败**的两行 ``getMe failed ... adapter not started``，而首条文案是
#: **文档引用**的排障入口（``docs/install.md`` / ``plugin/README.md``）⇒ ⛔ 一条
#: "计数注记"若混进那两行，等于让既有的排障指引变成假话。所以转折点**必须**有
#: 自己的字面，且只在阈值那一档、每个连续失败链出现一次。
PERMANENT_FAILURE_LOG_MARKER = "已连续失败达到阈值"


def _clamp_permanent_failure_cooldown(
    configured_seconds: Any,
    initial_backoff_seconds: Any = CREDENTIAL_PROBE_INITIAL_BACKOFF,
) -> float:
    """把冷却常量夹到 ``[初值, 一天]``，⛔ **绝不抛 ValueError**。

    ⚠️ 为什么不抛：构造期抛的异常会被 ``build()`` 包成 ``AdapterError`` ⇒
    适配器被跳过 ⇒ 整条 telegram 通道消失，而 **bot_token 明明配好了** ——
    那比「冷却用了次优值」坏得多（AGENTS.md §病史：一个旋钮写错打死整条
    关键路径，代价是零条日志）。

    ⚠️ 下界取**生效中的**初值（``initial_backoff_seconds``，缺省
    :data:`CREDENTIAL_PROBE_INITIAL_BACKOFF`）是**有意**的：比初值还短的冷却
    = 「永久失败比一次抖动还值得多试」⇒ 这句话自相矛盾。

    ⚠️ 为什么下界是**参数**而不是直接读模块常量：初值本身是**实例旋钮**
    （测试用 ``adapter.credential_probe_initial_backoff`` 把整条阶梯缩短）。
    若这里写死模块常量，旋钮一缩，冷却就**永远**被抬回 2.0 秒 ⇒ 上面那条
    「冷却不短于初值」的不变量在**同一时间点上**变成假话（冷却 2.0 > 初值
    0.01，而判据是「短于初值才算自相矛盾」的那一侧）。
    ⇒ 不变量的两侧必须落在**同一个有效值**上：调用方把它正在用的初值传进来，
    这里就跟它比。⛔ 不是「以后再说」的折中。

    ⚠️ 上界（一天）只防手滑的大数：冷却 **不是** 放弃点（到了还是要重试），
    所以一个天文数字不会让桥自己死，只会让「换回 token 到生效」拖成天荒地老。
    """
    try:
        seconds = float(configured_seconds)
    except (TypeError, ValueError):
        seconds = CREDENTIAL_PERMANENT_FAILURE_COOLDOWN
    if seconds != seconds:            # NaN：任何比较都假 ⇒ 单列判掉
        seconds = CREDENTIAL_PERMANENT_FAILURE_COOLDOWN
    try:
        floor_seconds = float(initial_backoff_seconds)
    except (TypeError, ValueError):
        floor_seconds = CREDENTIAL_PROBE_INITIAL_BACKOFF
    if not (floor_seconds == floor_seconds) or floor_seconds <= 0.0:
        # 初值本身不可用（NaN / 非正 / 不可转）⇒ 退回模块常量：
        # ⛔ 不能拿它当下界，否则 `max(seconds, nan)` 恒回 nan、冷却变成 NaN。
        floor_seconds = CREDENTIAL_PROBE_INITIAL_BACKOFF
    ceiling_seconds = 24.0 * 60.0 * 60.0
    clamped = min(max(seconds, floor_seconds), ceiling_seconds)
    if clamped != seconds:
        logger.warning(
            "telegram: CREDENTIAL_PERMANENT_FAILURE_COOLDOWN 配成了 %r 秒 —— "
            "超出可用范围 [%s, %s]，已夹到 %s 秒。"
            "（旋钮：:data:`CREDENTIAL_PERMANENT_FAILURE_COOLDOWN`；"
            "这是连续失败到阈值之后**固定**的复查间隔，"
            "夹它只改「多久复查一次」，改不了「永不放弃」）",
            configured_seconds, floor_seconds, ceiling_seconds, clamped,
        )
    return clamped


# ---------------------------------------------------------------------------
# 凭据闸门模块的 logger（与 ``telegram_protocol`` 模块层同名；见下方说明）
# ---------------------------------------------------------------------------
# ⚠️ 这个 logger **不是**给本模块自己用的 —— 本模块没有一行运行期日志。
# 它存在是因为上面那个夹逼告警必须落在「日志逐字节不变」的那个通道上：
# 生产侧 ``telegram.py`` 与 ``telegram_credential_lifecycle.py`` 共用
# ``opencode_bridge.adapters.telegram`` 这个通道名，测试与用户排障都搜它。
# ⛔ 别为这条告警另开一个通道：那会让「搜 telegram 就能看到凭据问题」变成假话。
logger = logging.getLogger("opencode_bridge.adapters.telegram")


def _retry_after(data: Any) -> Optional[float]:
    """Extract a 429 ``retry_after`` delay from a Telegram response body.

    ⚠️ **门控是 ``error_code == 429``，不是「``ok`` 为 false」**（2026-10-08 修）。
    改之前的门控是「只要带得出 ``parameters.retry_after`` 就照等」，于是
    ``{"error_code": 400, "parameters": {"retry_after": 7}}`` 会返回 **7.0** ——
    而 400 是**请求本身不合法**，睡 7 秒再原样重试一次，100% 还是 400
    ⇒ 纯浪费一轮墙钟，且那条 ``rate-limited`` 告警会指向一个根本没被限流的调用。

    ⚠️ **为什么按语义收窄而不是「来者不拒」**：「非 429 却带 ``retry_after`` 的
    响应在生产里会不会出现」**未核实** —— 本机无外网凭据、无法对真实平台取值。
    正因为不知道，就更不该让一个**语义上与限流无关**的字段决定我们睡多久
    （AGENTS.md §8 第 3 条：靠一个没被记录的事实去猜，必然在某些情况下猜错）。
    ⇒ 门控答的是「这个码是不是限流」，而不是「来者不拒」。

    ⛔ **畸形输入的安全行为逐项保留**（它们各自都有专测）：没有 ``parameters``
    且码是 429 ⇒ **1.0**（默认等一秒）；``retry_after`` 是负数 / 布尔 / 字符串 /
    非数值 ⇒ **``None``**（调用方**不许**当成 0 秒，那会变成立刻重试的紧循环）；
    ``data`` 压根不是 dict ⇒ ``None``。
    """
    if not isinstance(data, dict):
        return None
    if data.get("error_code") != 429:
        return None
    params = data.get("parameters")
    delay: Any = None
    if isinstance(params, dict):
        delay = params.get("retry_after")
    if delay is None:
        delay = 1
    if isinstance(delay, bool) or not isinstance(delay, (int, float)):
        return None
    if delay < 0:
        return None
    return min(float(delay), MAX_RETRY_AFTER)


def _button_row(button: Button) -> dict:
    """Serialize a Button to a Telegram inline keyboard button.

    ⚠️ Telegram 把 ``callback_data`` 按 **64 字节静默截断**（切在多字节字符中间
    还会把半个字符丢掉），且**不校验、不报错** ⇒ 点击回来的 data 与按钮绑定的
    不是同一个值，用户点了却**触发另一个动作**。所以这里绝不截断：

    ``button.data`` 的 UTF-8 编码超过 :data:`CALLBACK_DATA_LIMIT` 即抛
    ``ValueError`` —— 与 ``edit()`` 对超长正文的闸同一形态（不静默降级）。
    调用方负责拒绝该按钮（本文件 ``edit()``：放弃该按钮 + WARNING，
    绝不带截断的 data 上线）。
    """
    encoded = button.data.encode("utf-8")
    if len(encoded) > CALLBACK_DATA_LIMIT:
        raise ValueError(
            "telegram callback_data too long: %d > %d bytes"
            % (len(encoded), CALLBACK_DATA_LIMIT)
        )
    return {"text": button.label, "callback_data": button.data}

"""配对码的派生、归一化、比对，以及未授权时那条回复的**文案**。

## 它解决什么

「空 = 全拒」翻转之后，一个还没配对过的用户会被自己的桥接挡在门外，而
:meth:`opencode_bridge.adapters.base.Adapter.admits` 只回答"准不准进"、不回答
"怎么进来"。本模块提供那条**唯一**的入口：未授权会话发 ``/pair``，桥接回一串
**只对那个会话有效**的码；用户在本机跑 ``--pair <码>``，把它兑换成
``allowed_chat_ids`` 里的一项。

纯函数、无 IO、不读环境、不打日志、不抛 —— 所以能被单测直接钉住。

## 派生公式

::

    base32lower(
        HMAC-SHA256(
            key=pairing_secret.encode(),
            msg=b"opencode-bridge/pair/v1\\n" + platform + "\\n" + conversation_id,
        )
    )[:8]

四个选择各有理由，少一个都不成立：

* **HMAC 而不是裸 hash** —— 这里要的是一个 PRF：同一个 secret 在**别的用途**下
  派生出的值不能当码用。裸 hash 不具备这个性质，且有长度扩展。
* **域分隔串 ``opencode-bridge/pair/v1`` 必带** —— 没有它，同一个 secret 在别的
  用途（如会话 id）下派生的值就能直接被当码用。
* **base32 而不是 base36** —— base32 的字母表是 ``A-Z2-7``，**不含** ``0/1/8/9``，
  于是 ``O/0``、``I/l/1`` 这类抄错从**根上**不存在；base36 会包含它们。
* **8 字符（40 bit）够** —— 验证是**本机**行为：``--pair`` 是本地 CLI，
  **没有网络 oracle** 可以拿来穷举。40 bit 面对本地猜要几百年，再长只会让用户抄错。

## ⚠️ ``hmac.compare_digest`` 在这里**不是**一道安全控制

它是被沿用的**惯例**（``adapters/a2a.py`` 已有同一个用法），而不是防护：
**本仓库里没有远程时序路径**可以观测比对的耗时 —— 验证跑在用户自己的 shell 里，
而**能跑 ``--pair`` 的人已经有那个 shell 了**。

⛔ **正因为如此才必须写在这里**：否则下一个人会以为这里有一道防护故事，
也就不会在将来真的把它暴露到网络面时想起要加限流。

**重访条件**：一旦验证出现在**网络面**（webhook / a2a peer / 任何 HTTP 端点），
**限流 + 恒定时间**就从"惯例"变成**必须**，且 40 bit 不再够（那时才有了 oracle）。

## ⚠️ 授权是**物化**进 ``allowed_chat_ids`` 的

码只是一张**兑换券**：兑换动作把 conversation id **抄进**白名单，之后闸门读的是
那份清单，与 secret 再无关系。因此：

* 轮换 :data:`PAIRING_SECRET_KEY` 的值 **只让未兑换的码失效**，
  **不撤销已经完成的配对** —— 已写入的条目照旧生效。
* 想撤销一个已配对的会话只能改 ``config.json``；``--pair`` **只加不减**。

## 空 secret = **不提供配对**，绝不是"用空串派生"

:func:`derive_pairing_code` 遇到空 secret 返回 ``""``。空串派生出的码是任何人都
算得出来的常量 —— 那等于没有闸门。

⛔ **本模块不自动生成、也不回写** secret：那会让**每次启动**都依赖一个配置写者
（而配置写者要处理备份、原子替换、并发），而绝大多数用户并不需要配对。
空 secret 的可见性由调用方负责（启动一行 log + ``--setup`` 里一行说明）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from typing import Final

logger = logging.getLogger("opencode_bridge.pairing")

__all__ = [
    "CONFIG_VERSION_KEY",
    "EMPTY_ALLOWLIST_DENY_FROM_VERSION",
    "PAIRING_CODE_LENGTH",
    "PAIRING_COMMAND",
    "PAIRING_SECRET_KEY",
    "PAIRING_TRIGGER",
    "derive_pairing_code",
    "empty_allowlist_is_open",
    "normalize_pairing_code",
    "pairing_code_matches",
    "pairing_is_available",
    "pairing_reply_text",
    "reads_pairing_trigger",
    "warn_if_pairing_unavailable",
]

#: 顶层配置键名。定义在这里（而不是各自模块里硬编码字面量），因为它们同时被
#: :mod:`opencode_bridge.config`、:mod:`opencode_bridge.allowlist` 与适配器读。
PAIRING_SECRET_KEY: Final[str] = "pairing_secret"
CONFIG_VERSION_KEY: Final[str] = "config_version"

#: 未授权时触发配对的那条命令。**刻意不复用** ``/setup``：``/setup`` 的文案冻结在
#: :mod:`opencode_bridge.commands` 里且被测试钉住，让同一个词在授权 / 未授权两种
#: 状态下表示两件完全不同的事，等于把一份冻结输出变成状态相关的。触发面收敛成
#: **一个字面量**也比收敛成"所有命令"小一个量级。
PAIRING_TRIGGER: Final[str] = "/pair"

#: 派生出多少个 base32 字符。见模块 docstring：40 bit 是够的，因为验证在本机。
PAIRING_CODE_LENGTH: Final[int] = 8

#: 回信里让用户敲的那条命令。⛔ **刻意包含 ``--conversation``**，而这不只是"写全"：
#: 码**绑定会话**，只凭一串码无法确定是哪个（要枚举所有可能的会话 id = 无界搜索，
#: 也等于给 40 bit 造一个 oracle）。所以**首次配对不给 ``--conversation`` 一定失败**，
#: 回信必须把两个参数都给全。
PAIRING_COMMAND: Final[str] = (
    "python -m opencode_bridge --pair {code} --conversation {conversation_id}"
)

#: HMAC 的域分隔串。**必带**，理由见模块 docstring。
_PAIRING_DOMAIN: Final[bytes] = b"opencode-bridge/pair/v1"

#: 「空清单 ⇒ 全拒」从哪个 :data:`CONFIG_VERSION_KEY` 起生效。
#:
#: ⚠️ 这个常量是**整个发布方式的支点**：
#:
#: * 配置文件**没有** ``config_version`` ⇒ 它早于这次翻转 ⇒ 保持旧的「空 = 全开」
#:   语义（配好凭据的用户开局就不是全开的，所以这不是一个更宽松的默认，
#:   只是**维持现状**）；
#: * ``config_version >= 2`` ⇒ 采新语义（**空 = 谁都不放行**）。
#:
#: 于是「空 = 全拒」对**已经配对过**的用户立即生效（``--pair`` 那次写盘会顺手
#: 写上 ``config_version: 2``），没配对的用户仍是开放语义但**看得见预告**。
#: **任何人都不会被困死。**
EMPTY_ALLOWLIST_DENY_FROM_VERSION: Final[int] = 2


def _version_of(config_version: object) -> int:
    """把 ``config_version`` 读成可比较的整数；读不出就当 ``0``（= 文件早于翻转）。

    ⚠️ 未知 / 非法值**一律当 0**，方向是"旧的开放语义"。这与
    :meth:`opencode_bridge.config.Config._coerce_ok` 对非法值的处置同一条纪律：
    读不懂一个键不许让人**猜**它的含义，而"读不懂 ⇒ 当没写"是最保守的那个解释。
    """
    if isinstance(config_version, bool):  # ``bool`` 不是版本号（``True`` != 1）
        return 0
    if isinstance(config_version, int):
        return config_version
    if not isinstance(config_version, str):
        return 0
    # ⛔ **只认十进制整数**（可带正负号）：``int(text, 0)`` 会把 ``"0x2"`` 读成 2，
    # 而"配置文件里写了什么"必须**逐字**可预测 —— 读不出就当没写。
    digits = config_version.strip()
    if not digits or digits.lstrip("+-") == "" or not digits.lstrip("+-").isdigit():
        return 0
    return int(digits)


def empty_allowlist_is_open(config_version: object) -> bool:
    """**空**授权清单是否意味着「放行一切」。

    ⚠️ 这是"这个文件早于翻转"这件事的**唯一**判定点 —— 闸门、状态视图、
    ``--status`` 都读它，不许各自判一次（两处各判一次就会出现"状态说一种、
    闸门做另一种"，那比没有状态视图更坏）。

    纯函数，所以可单测；实现在这里是为了让 :mod:`opencode_bridge.allowlist`
    与 :mod:`opencode_bridge.config` 共用**同一个**答案。
    """
    return _version_of(config_version) < EMPTY_ALLOWLIST_DENY_FROM_VERSION


def pairing_is_available(pairing_secret: object) -> bool:
    """配对**是否可用** —— 即 secret 非空。

    空 secret 的语义是「**不提供配对**」，绝不能是"用空串派生"：空串派生出的码
    是任何人都算得出来的常量。见模块 docstring。
    """
    return bool(str(pairing_secret or "").strip())


def derive_pairing_code(
    pairing_secret: object, platform: object, conversation_id: object
) -> str:
    """派生**这个** ``(platform, conversation_id)`` 的配对码；不可用时返回 ``""``。

    绑定 conversation 是承重的：同一个 secret 在另一个会话上派生出的是**另一个**
    码，所以群里看到的码不能授权群外的会话，反之亦然。

    归一化**不在**这里做 —— 派生与归一化是两件事，:func:`pairing_code_matches`
    才把两者接起来。
    """
    secret = str(pairing_secret or "").strip()
    if not secret:
        # ⛔ 绝不"用空串派生"：那是一个谁都能算出来的常量，等于没有闸门。
        return ""
    message = b"\n".join(
        (
            _PAIRING_DOMAIN,
            str(platform or "").encode("utf-8"),
            str(conversation_id or "").encode("utf-8"),
        )
    )
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest()
    encoded = base64.b32encode(digest).decode("ascii").lower()
    return encoded[:PAIRING_CODE_LENGTH]


def normalize_pairing_code(code: object) -> str:
    """把用户敲进来的码归一化到**与派生结果同一形态**。

    顺序是 :meth:`str.strip` → 转小写 → 去掉 ``-`` 与空格。**两侧必须同一函数**
    （:func:`pairing_code_matches` 用它，:func:`derive_pairing_code` 的产物本身
    已归一）。

    ⚠️ 归一化**不是**可选的美容步骤：不归一会让 ``abcd-efgh`` 失败，而用户会把
    那种失败当成 bug 报上来。**这比防时序攻击重要得多** —— 后者在这里压根不是
    威胁（见模块 docstring）。
    """
    text = str(code or "").strip().lower()
    return text.replace("-", "").replace(" ", "")


def pairing_code_matches(
    code: object, pairing_secret: object, platform: object, conversation_id: object
) -> bool:
    """``code`` 是不是**这个**会话的正确码。secret 不可用时恒为 ``False``。

    ⚠️ :func:`hmac.compare_digest` **在这里不是安全控制** —— 没有远程时序路径，
    验证在本机 CLI，而能跑它的人已经有你的 shell 了。见模块 docstring 的
    「重访条件」：一旦验证上网络面，限流 + 恒定时间一起变成必须。
    """
    expected = derive_pairing_code(pairing_secret, platform, conversation_id)
    if not expected:
        return False
    return hmac.compare_digest(normalize_pairing_code(code), expected)


def reads_pairing_trigger(text: object) -> bool:
    """这条正文是不是在请求配对（也就是那条 ``/pair``）。

    ⚠️ **命令识别在这里被复制了一遍，是有意识的**：命令解析只发生在
    :mod:`opencode_bridge.inbound_gateway`（:meth:`~opencode_bridge.inbound_gateway.
    InboundGateway.on_inbound`），而闸门在**适配器**里 —— 未授权的消息根本活不到
    命令解析那一步，所以适配器必须**自己**再认一次。

    归一刻意照抄那条既有路径（``text.lstrip()`` → 按空白切第一个词 → 去掉前导
    ``/`` → 砍掉 Telegram 的 ``@bot`` 后缀 → 转小写），**不新造第二套命令文法**。
    """
    stripped = str(text or "").lstrip()
    # ⛔ 必须真的**带上前导斜杠**：``pair``（无斜杠）不是命令，而
    # ``commands.handle_command`` 那边也是靠这个斜杠把命令与正文分开的。
    # 少了这一句，"pair your mom"这种普通句子会被当配对请求。
    if not stripped.startswith("/"):
        return False
    tokens = stripped.split(None, 1)
    if not tokens:
        return False
    return tokens[0][1:].split("@", 1)[0].lower() == PAIRING_TRIGGER[1:]


def pairing_reply_text(
    platform_label: str, conversation_id: str, code: str
) -> str:
    """未授权时回给会话的那段文本。

    ⚠️ **这段文案是安全措施，不是措辞偏好。** 底下两条是承重的：

    1. **必须写出范围声明**（这串码只授权哪个会话、群里别人看到它会怎样）。
       少了它，用户会把群里看到的码当成"随便哪个群都能用"，于是授权了一个
       自己没打算授权的群。
    2. ⛔ **不许出现任何路径**。``config_path`` 已由 ``--setup --json`` 暴露，
       但把它打进 chat 等于**把本机目录发到群里**（``AGENTS.md`` §2.2）。只回
       命令形状。

    ⛔ 同样**不含**配置回显、**不含**条目数、**不含**"这个 chat 是否已授权"
    —— 那是**枚举 oracle**。

    ⚠️ **那条命令必须是能真敲出来的**（:data:`PAIRING_COMMAND`）：本仓库
    ``pyproject.toml`` **没有** ``[project.scripts]``，所以 ``opencode-bridge``
    这个命令**根本不存在**，全仓库一律用 ``python -m opencode_bridge``。
    回一句不存在的命令，用户照抄后拿到的是 "No such file or directory" ——
    而他此刻**正被自己的桥接挡在门外**，那正是最不该让他多绕的一段路。
    这条由 ``tests/test_pairing.py`` 双向守住（含"不许出现裸形式"）。
    """
    return (
        f"{platform_label}: 这个会话还没有被授权。\n"
        f"\n"
        f"配对码：{code}\n"
        f"\n"
        f"这串码**只**授权 {conversation_id}"
        f"（就是你发 {PAIRING_TRIGGER} 的这个会话）。"
        f"群里其他人看到它也能在你机器上执行同一个 --pair 命令，"
        f"效果是把这个**群**加进白名单。"
        f"不打算授权群的话，请改在私聊里发。\n"
        f"\n"
        f"在本机跑：{PAIRING_COMMAND.format(code=code, conversation_id=conversation_id)}\n"
        f"写完要**重启桥接**才生效（改 token 同样要重启）。"
    )


def warn_if_pairing_unavailable(
    platform_label: str, pairing_secret: object, *, has_credentials: bool
) -> None:
    """配对**不可用**时记一行 —— 空 secret 的可见性。

    只在**配好凭据**时才喊：没凭据的适配器收不到任何消息，"你没法配对"对它是
    噪音（与 ``allowlist.warn_if_no_allowlist`` 同一条判据）。

    一行 log，不打断启动（``AGENTS.md`` §8：说清楚，不替用户决定）。
    """
    if pairing_is_available(pairing_secret) or not has_credentials:
        return
    logger.warning(
        "%s: 未配置 %s ⇒ **本平台不提供配对**，未授权的会话只能手改 "
        "config.json 里的 %s 才能进来。",
        platform_label,
        PAIRING_SECRET_KEY,
        "allowed_chat_ids",
    )
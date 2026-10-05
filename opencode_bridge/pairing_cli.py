"""``--pair``：把配对码兑换成 ``allowed_chat_ids`` 里的一项。

从 :mod:`opencode_bridge.__main__` **抽出来**是因为那是「参数解析 + 各子命令 +
状态视图」三件事的装配处，而本模块是**自成一块**的"改用户配置文件"职责 ——
它有备份、有原子写、有幂等判断与一串**失败关闭**的守卫，加进去会让入口文件继续
变胖（AGENTS.md §5：入口文件只做组装，不承载业务逻辑）。

⚠️ 本模块只做**一件事**：读 → 改**一个数组** → 备份 → 原子替换。任何一步没把握
就报错退出，**绝不留半个状态**。

## 三条不可让步的约束

1. ⛔ **绝不在配置文件不存在时创建它** —— 用户没有配置文件就没有可授权的桥
   （连 bot 都没跑起来），凭空造一个只会让他以为配好了。
2. ⚠️ **守卫的顺序承重**：先问"有没有配置文件"，再问"有没有 pairing_secret"。
   没有文件 ⇒ 解析不出 secret ⇒ 反过来先查 secret 的话，"文件不存在"那条分支
   **永远不可达**，而那条分支正是"绝不创建 config.json"这条承诺的所在。
3. ⛔ **不限流、不锁定**：本地执行、无 lockout 状态要保护、40 bit 且没有 oracle
   （见 :mod:`opencode_bridge.pairing`）。限流只会换来一套要维护的锁定状态机。

## 备份与原子写

:func:`_backup_config_file` 与 :func:`_write_config_atomically` **逐字照抄**
:mod:`opencode_bridge.state` 里那两段（``_backup_file`` / ``_write_locked`）——
那是本仓库对**用户配置**做备份与原子替换的既有做法。

⚠️ **刻意不把 :mod:`opencode_bridge.state` 重构成共享模块**：那是另一次改动，
混进来会让本次 diff 变成"顺手重构"，而重构失败与配对失败在结果上很难区分。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time

from .config import DEFAULT_CONFIG_NAME, Config
from .pairing import (
    EMPTY_ALLOWLIST_DENY_FROM_VERSION,
    PAIRING_SECRET_KEY,
    pairing_code_matches,
)

__all__ = ["run_pair", "split_conversation"]


def config_file_in_use() -> str | None:
    """**已存在**的配置文件路径；一个都没有则 ``None``。

    ⚠️ 路径解析**刻意与** :meth:`opencode_bridge.config.Config.load` **同一套**
    （``OPENCODE_BRIDGE_CONFIG`` → cwd → 包目录），否则 ``--pair`` 可能改一份
    桥接根本不读的文件 —— 用户会拿到"配好了"，而重启后行为没变。

    与 :func:`opencode_bridge.__main__._config_file_in_use` 是**两份**实现，且
    **故意不同**：那个回答"桥接会加载哪个路径"（没有文件时也返回一个候选路径，
    好让 ``--setup --json`` 告诉用户该建在哪），本函数回答"**现在有没有**文件可改"
    （没有就是 ``None``）。刻意不合并：把两者合成一个带 ``None`` 的函数，会让
    ``--setup --json`` 在没有配置时突然拿到 ``null``，而那是个**已发布**的字段。

    ⛔ 本函数绝不返回"将要创建"的路径 —— 见模块 docstring 第 1 条。
    """
    env_path = (os.environ.get("OPENCODE_BRIDGE_CONFIG") or "").strip()
    if env_path and os.path.isfile(env_path):
        return os.path.abspath(env_path)
    cwd_path = os.path.join(os.getcwd(), DEFAULT_CONFIG_NAME)
    if os.path.isfile(cwd_path):
        return os.path.abspath(cwd_path)
    package_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), DEFAULT_CONFIG_NAME
    )
    return package_path if os.path.isfile(package_path) else None


def split_conversation(conversation_id: str) -> tuple[str, str] | None:
    """把 ``platform:local_id`` 拆成 ``(platform, local_id)``；拆不出就 ``None``。

    ⚠️ **只在第一个冒号处切**：``matrix:!room:example.org`` 这类 id 自身含冒号
    （见 :mod:`opencode_bridge.identity`），按最后一个切会把平台名解析成
    ``matrix:!room``，于是配对码对不上（而用户看不出为什么）。
    """
    head, sep, rest = str(conversation_id or "").partition(":")
    if not sep or not head or not rest:
        return None
    return head, rest


def backup_config_file(path: str) -> str:
    """把配置另存为 ``.bak.YYYYMMDD-HHMMSS`` 备份，返回备份路径。

    ⚠️ **逐字照抄** :func:`opencode_bridge.state._backup_file`（含"同一秒内重复不
    覆盖已有备份"的计数器）。**没有备份就没有回滚路径**，那就**不该动用户的文件**。
    """
    base = f"{path}.bak.{time.strftime('%Y%m%d-%H%M%S')}"
    target = base
    counter = 1
    while os.path.exists(target):
        target = f"{base}.{counter}"
        counter += 1
    shutil.copy2(path, target)
    return target


def write_config_atomically(path: str, document: dict) -> None:
    """原子替换配置文件。异常时删掉临时文件。

    ⚠️ **逐字照抄** :meth:`opencode_bridge.state.StateStore._write_locked` 那段
    （mkstemp → ``json.dump`` → ``flush`` → ``os.fsync`` → ``os.replace``，异常
    unlink 临时文件）。原子替换的含义是"要么旧文件、要么新文件，**不存在**第三
    个状态"—— 授权文件被截断一半的后果是**用户以为配好了、实际桥接进不来**。

    唯一有意的差别：不排序键（用户手写的配置，键序是给他看的）。
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(document, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def raw_config(path: str) -> dict:
    """读配置文件的原始 JSON（保留用户写的键序与结构）。

    ⚠️ 刻意**不走** :class:`Config`：``--pair`` 必须只改**一个数组**，而
    :meth:`Config.load` 会填默认值、丢未知键的警告、覆盖环境变量 —— 用它回写
    等于把用户没配过的键**悄悄补齐**，那是"手改配置"绝不该有的副作用。
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"读配置失败：{exc}", file=sys.stderr)
        return {}
    if not isinstance(raw, dict):
        print("config.json 必须是一个 JSON 对象", file=sys.stderr)
        return {}
    return raw


def _already_authorised(cfg: Config, platform: str, conversation_id: str) -> bool:
    """这个会话是否**已经**在 ``allowed_chat_ids`` 里。"""
    entry = cfg.adapters.get(platform) if isinstance(cfg.adapters, dict) else None
    if not isinstance(entry, dict):
        return False
    return conversation_id in {
        str(value).strip() for value in (entry.get("allowed_chat_ids") or [])
    }


def run_pair(
    cfg: Config, code: str, conversation_id: str | None, platform: str | None = None
) -> int:
    """把 ``code`` 兑换成 ``allowed_chat_ids`` 里的一项。``0`` = 成功。

    ⚠️ **必须连会话一起给**（``--conversation telegram:12345``，就是回信里那个值）。
    码**绑定 conversation**，而"只拿一串码反查出是哪个会话"需要遍历**所有**可能的
    会话 id —— 那是无界搜索，也等于给 40 bit 造了一个 oracle（见
    :mod:`opencode_bridge.pairing`）。

    ``platform`` 只用于**兜底反查**已授权会话（那种情况下能匹配的只可能是已授权
    会话，而它本来就不需要再授权）；给了 ``conversation_id`` 时它被忽略。
    """
    # ⚠️ 顺序承重：先问"有没有配置文件"，再问"有没有 secret"。理由见模块 docstring。
    path = config_file_in_use()
    if path is None:
        print(
            f"找不到 {DEFAULT_CONFIG_NAME}（OPENCODE_BRIDGE_CONFIG → 当前目录 → "
            f"包目录 都没有）⇒ 没有可授权的桥接。**不会**替你创建一个："
            f"请先把桥接配起来。",
            file=sys.stderr,
        )
        return 1
    if not str(cfg.pairing_secret or "").strip():
        print(
            f"配置里没有 {PAIRING_SECRET_KEY} ⇒ 本机不提供配对，无法兑换。"
            f"请先在 {DEFAULT_CONFIG_NAME} 顶层填一个 {PAIRING_SECRET_KEY} "
            f"并重启桥接。",
            file=sys.stderr,
        )
        return 1

    candidates = _candidates(cfg, conversation_id, platform)
    if not candidates:
        print(
            "请用 --conversation 给出要授权的会话（形如 telegram:12345，"
            "就是 /pair 回信里那个值）—— 只凭一串码无法确定是哪个会话。",
            file=sys.stderr,
        )
        return 1

    matched: list[str] = [
        cid
        for cid in candidates
        if (parsed := split_conversation(cid))
        and pairing_code_matches(
            code, cfg.pairing_secret, parsed[0], f"{parsed[0]}:{parsed[1]}"
        )
    ]
    if not matched:
        print(
            "这串码与那个会话不匹配（码只对**发出它**的那个会话有效；"
            "大小写、空格、连字符都可以照抄）。",
            file=sys.stderr,
        )
        return 1
    if len(matched) > 1:
        # 理论上要 40 bit 碰撞。不猜 —— 猜错的后果是把**另一个**会话写进白名单。
        print(
            f"这串码匹配到多个会话（{'、'.join(matched)}）—— 不猜。"
            f"请把 --conversation 收窄到一个。",
            file=sys.stderr,
        )
        return 1

    target = matched[0]
    parsed = split_conversation(target)
    assert parsed is not None  # matched 全部来自 split_conversation 成功的结果
    matched_platform, _local = parsed
    if matched_platform not in (cfg.adapters if isinstance(cfg.adapters, dict) else {}):
        print(
            f"配置里没有 adapters.{matched_platform} 这一段 ⇒ 无法写入。"
            f"请在 {DEFAULT_CONFIG_NAME} 里加上该平台再试。",
            file=sys.stderr,
        )
        return 1
    if _already_authorised(cfg, matched_platform, target):
        print(f"{target} **已经**在 allowed_chat_ids 里，无需重复授权。")
        return 0

    document = raw_config(path)
    if not document:
        return 1  # raw_config 已经把原因写进 stderr
    # 只改**一个数组** + 顺手写上 config_version。其余键一律原样带回。
    platform_doc = document.setdefault("adapters", {}).setdefault(matched_platform, {})
    allowed = platform_doc.get("allowed_chat_ids")
    if not isinstance(allowed, list):
        allowed = []
    allowed.append(target)
    platform_doc["allowed_chat_ids"] = allowed
    # 同一次写盘顺手翻版本：本次授权让「空 = 全拒」对已配对用户**立即生效**，
    # 而这对**刚配上的这一个会话**无影响（它现在有了一项，不再是空清单）。
    document["config_version"] = max(
        int(cfg.config_version or 0), EMPTY_ALLOWLIST_DENY_FROM_VERSION
    )

    backup = backup_config_file(path)
    write_config_atomically(path, document)
    print(f"已授权 {target}")
    print(f"  写入 {os.path.basename(path)}（备份：{os.path.basename(backup)}）")
    print(f"  config_version 现为 {document['config_version']}")
    # ⛔ **不做 live reload**：改 bot_token 同样要重启，而 plugin/index.ts 零
    # fs.watch / statSync —— 本来就没有热重载。谎称"已生效"会让用户以为下一条消息
    # 就通了，而它其实还会被丢掉。
    print("  ⚠️ **要重启桥接才生效** —— 改 bot_token 同样要重启，没有热重载。")
    return 0


def _candidates(
    cfg: Config, conversation_id: str | None, platform: str | None
) -> list[str]:
    """要验证的候选会话。

    给了 ``conversation_id`` 就只有它；否则只在配置里**已授权**的会话里兜底反查 ——
    而那种情况基本总是空的（未授权的会话根本还没进过清单）。保留这条兜底是为了让
    ``--pair`` 在"重输一遍码"时给出"已经授权过了"而不是"码不匹配"。
    """
    if conversation_id:
        return [str(conversation_id).strip()]
    entries = cfg.adapters if isinstance(cfg.adapters, dict) else {}
    names = [str(platform)] if platform else [str(key) for key in entries]
    candidates: list[str] = []
    for name in names:
        entry = entries.get(name)
        if not isinstance(entry, dict):
            continue
        candidates.extend(
            f"{name}:{value}"
            for value in sorted(
                {
                    str(item).strip()
                    for item in (entry.get("allowed_chat_ids") or [])
                    if str(item).strip()
                }
            )
        )
    return candidates
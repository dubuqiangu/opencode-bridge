"""单实例保护：同一台机器上不允许有两个桥同时轮询同一个 bot。

## 为什么需要它

Telegram（以及多数IM 平台）的收消息接口**同一时刻只允许一个消费者**。
两个桥同时轮询会得到 `409 Conflict: terminated by other getUpdates request`，
后果是双向的：

- **消息随机丢失**——谁抢到算谁的，另一方永远等不到；
- **同一条消息被处理两次**——两边各自把prompt 发给 opencode，于是 agent 跑两遍，
  用户收到两份一样的回复；
- 日志被409 刷屏，真正的错误被埋掉。

而触发条件极其容易：opencode 插件会**自动**拉起一个桥，任何人（包括开发者自己）
为调试再跑一次 `python -m opencode_bridge`，就会撞车。这个坑在 A4 真实验证期间
反复踩到，且症状是"消息随机丢失"——极难自查。

所以：**发现已有实例在跑就干净退出，并把持有者的 pid 说清楚。**

## 锁的语义

- 首次启动用 `O_CREAT | O_EXCL` 原子创建，天然互斥；
- 锁文件里记 `pid` + `startedAt`，启动时据此判断持有者是否**真的活着**；
- 持有者已死（崩溃 / 强杀）则**接管**——不留死锁；
- 正常退出时删除锁；进程被强杀时锁文件残留，由下个实例按 pid 存活判定接管。

⚠️ 已知的小窗口：`O_EXCL` 创建与写入 pid 之间有一个瞬间，此时读到空内容的锁。
本实现把「读不出 pid」当作**失效锁并接管**——在单机场景下这个取舍是安全的：
最坏结果是两个实例短暂并存，症状退化成 409，而不会永久卡死（后者严重得多）。
"""

from __future__ import annotations

import errno
import io
import json
import os
import time
from typing import Any

#: 锁文件名。**故意不叫** `.bridge-plugin.lock` —— 那个是插件用来记录
#: "我spawn 了哪个子进程"的生命周期文件，语义不同，共用一个文件会互相覆盖。
DEFAULT_LOCK_NAME = ".bridge-instance.lock"

#: 锁的最大可信年龄（秒）。超过就当失效并接管。
#:
#: 为什么需要：进程存活判定只靠 pid，而 **Windows 的 pid 回收很积极**——
#: 持有者早就退出、pid 却恰好被某个无关进程占用时，锁会看起来"还活着"，
#: 于是桥永远起不来。给它一个上限就能自愈，不必人工删文件。
#:
#: 取24 小时：远大于任何正常桥实例的寿命（实测也就几小时），又足够短到
#: 第二天开机时不会卡住。
MAX_LOCK_AGE_SECONDS = 24 * 60 * 60


def pid_is_alive(pid: int) -> bool:
    """进程是否存活。

    Windows 上 `os.kill(pid, 0)` 可用（对存活进程抛 PermissionError 或返回 0），
    POSIX 上信号 0 同样可用于探测。两者都不真发信号。
    """
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError as exc:
        # EPERM 说明进程存在但当前用户无权发信号 —— 仍然算活着
        return exc.errno == errno.EPERM
    except Exception:
        return False


class InstanceLock:
    """跨进程的单实例锁。用作上下文管理器，退出时自动释放。"""

    def __init__(self, directory: str, name: str = DEFAULT_LOCK_NAME) -> None:
        self.path = os.path.join(directory, name)
        self._held = False
        self._holder_pid = 0

    # ---------------------------------------------------------------- 状态

    @property
    def held(self) -> bool:
        """本进程是否已持有锁。"""
        return self._held

    @property
    def holder_pid(self) -> int:
        """当前持有者 pid；``0`` 表示无有效持有者。"""
        return self._holder_pid

    def _read(self) -> dict[str, Any]:
        try:
            with io.open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception:
            # 损坏 / 空文件 / 非 JSON —— 一律当作"没有有效锁"
            return {}

    def _holder_is_alive(self) -> bool:
        lock = self._read()
        holder = int(lock.get("pid") or 0)
        if not holder:
            return False
        if holder == os.getpid():
            # 自己的残留锁（上次异常退出没清掉）—— 视为可接管
            return False

        # 先看年龄：pid 会被回收，光靠存活判定可能把陈旧锁当成"还活着"，
        # 那会让桥永远起不来、必须人工删文件。超过上限就当失效。
        started_at = lock.get("startedAt")
        if isinstance(started_at, (int, float)) and started_at > 0:
            age = time.time() - float(started_at)
            if age > MAX_LOCK_AGE_SECONDS:
                return False

        return pid_is_alive(holder)

    # ---------------------------------------------------------------- 动作

    def acquire(self) -> tuple[bool, int]:
        """尝试加锁。返回 ``(是否成功, 持有者pid)``。

        失败时 ``pid`` 是**那个占着位置的实例**，用于告诉用户"谁占着"。
        """
        if self._held:
            return True, os.getpid()

        try:
            # 先确保目录存在（锁放在 bridge 目录下）
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if self._holder_is_alive():
                self._holder_pid = int(self._read().get("pid") or 0)
                return False, self._holder_pid
            # 失效锁（持有者已死）—— 接管
            self._write()
            self._held = True
            self._holder_pid = os.getpid()
            return True, os.getpid()
        except OSError:
            # 连目录都建不出来（比如只读文件系统）—— 不因为拿不到锁就拒绝启动，
            # 那会让"配置正确却起不来"，比多实例更难排查。
            self._held = False
            return True, 0

        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                {"pid": os.getpid(), "startedAt": int(time.time())}, handle
            )
        self._held = True
        self._holder_pid = os.getpid()
        return True, os.getpid()

    def _write(self) -> None:
        with io.open(self.path, "w", encoding="utf-8") as handle:
            json.dump(
                {"pid": os.getpid(), "startedAt": int(time.time())}, handle
            )

    def release(self) -> None:
        """释放锁。只删**自己**持有的那份，避免删掉别人的。"""
        if not self._held:
            return
        self._held = False
        if int(self._read().get("pid") or 0) != os.getpid():
            # 锁已经被别人接管了，不能删
            return
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass
        except OSError:
            pass

    # ------------------------------------------------------------ 上下文

    def __enter__(self) -> "InstanceLock":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()
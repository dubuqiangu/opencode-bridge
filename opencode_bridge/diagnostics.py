"""进程级诊断：崩溃现场取证 + 生命周期账本。

## 为什么需要它

台账 G1：**桥每 5~7 分钟被外部终止一次，原因未知**。已知的是日志里没有
`已退出`——说明不是走 `main()` 的正常退出路径，即被信号或外部 kill 干掉。

"未知"之所以难以推进，是因为**现有日志无法回答"死前最后在干什么"**：
Python 层的日志只在代码显式执行时才会写，进程被外部杀死时
**什么都不会留下**。所以要先补上取证能力，再谈修复。

## 照抄参考项目的做法

`nousresearch/hermes-agent`（`hermes_startup_watchdog.py:17059-17108`）的原文：

    "A daemon thread armed at process entry and disarmed once the loop is confirmed
    live dumps all-thread stacks (**faulthandler**), records the exit in the lifecycle
    ledger (NS-608) and os._exit()s with the service-restart code so s6/systemd respawn."

即三件事：**dump 全部线程栈** + **把退出记进生命周期账本** + 用约定退出码
表达"请重启我"。本模块实现前两件；第三件属于宿主协议，暂不做
（宿主是opencode 插件而非 s6/systemd，退出码语义不同）。

## 为什么本模块用 faulthandler 而不是自己抓栈

`faulthandler` 是标准库、**C 层实现**，所以它能在 Python 代码完全不执行的情况下
留下现场——这正是"被强杀"这种场景唯一可靠的手段。自己用 `sys._current_frames()`
抓栈只在 Python 层有机会运行时才有意义。

## 边界

- **只取证，不自愈**：不做重启、不做健康检查。G1 的方案明确写了"先取证，
  有证据后再考虑看门狗"——在死因未知时加自愈机制等于用一层自愈掩盖真正的问题。
- **绝不因诊断而影响主流程**：所有函数都吞异常。看门狗式的代码一旦自己抛错，
  就会变成新的故障源（hermes 原文也强调 "a watchdog failure must never affect
  the startup it observes"）。
"""

from __future__ import annotations

import faulthandler
import io
import json
import os
import signal
import sys
import threading
import time
from typing import Any, TextIO

#: 生命周期账本文件名（JSONL，每行一条记录）。
LIFECYCLE_FILENAME = "bridge-lifecycle.jsonl"

#: 崩溃栈转储文件名。单独一个文件，因为它体积大、只在出事时才有内容。
STACK_DUMP_FILENAME = "bridge-stacks.log"

#: 账本最多保留多少字节。超了就截断——诊断文件不该把磁盘吃满。
MAX_LEDGER_BYTES = 256 * 1024

#: Windows 上 `faulthandler.register` 支持的信号集合较窄（不含 SIGUSR1/SIGUSR2），
#: 所以按平台筛选，筛不到就跳过——不能假设 POSIX 的信号集。
_REGISTERABLE_SIGNALS = ("SIGABRT", "SIGSEGV", "SIGFPE", "SIGILL", "SIGTERM", "SIGINT")


def _now() -> float:
    return time.time()


class ProcessDiagnostics:
    """进程级取证器。用作上下文管理器，退出会自动写一条生命周期记录。"""

    def __init__(self, directory: str) -> None:
        self.directory = directory
        self.ledger_path = os.path.join(directory, LIFECYCLE_FILENAME)
        self.stack_path = os.path.join(directory, STACK_DUMP_FILENAME)
        self.started_at = _now()
        self.started_monotonic = time.monotonic()
        self.pid = os.getpid()
        #: 记录一次 set 之后尚未落盘的账本句柄（保持文件对象存活）
        self._stack_file: TextIO | None = None
        self._registered: list[str] = []

    # ---------------------------------------------------------------- 崩溃栈

    def install(self) -> None:
        """开启崩溃栈转储。**任何失败都静默吞掉**——诊断不能成为故障源。"""
        try:
            os.makedirs(self.directory, exist_ok=True)
            # faulthandler 需要一个保持打开的二进制句柄；用append 模式，
            # 这样多次安装不会互相截断
            self._stack_file = io.open(
                self.stack_path, "a", encoding="utf-8", errors="replace"
            )
            faulthandler.enable(file=self._stack_file, all_threads=True)
        except Exception:
            self._stack_file = None

        for name in _REGISTERABLE_SIGNALS:
            signum = getattr(signal, name, None)
            if signum is None:
                continue
            try:
                # ⚠️ `chain=True`（faulthandler 的默认）**不能改成 False**。
                #
                # `chain=False` 表示"dump 完就不再交给默认处理器"，于是
                # SIGTERM / SIGINT 被处理后**进程不会退出**——等于让插件杀不掉
                # 这个桥、也让 Ctrl+C 失效。取证绝不能改变被观察对象的行为。
                # chain=True 是"先留证据，再按原本的方式死"。
                faulthandler.register(
                    signum, all_threads=True, chain=True
                )
                self._registered.append(name)
            except Exception:
                # 平台不支持 / 已被注册过——跳过即可
                continue

    def dump_stacks(self, reason: str) -> None:
        """把全部线程栈写到转储文件。任何异常都吞掉。"""
        try:
            self._write_stack(
                "\n===== stacks @ %s reason=%s =====\n"
                % (time.strftime("%Y-%m-%d %H:%M:%S"), reason)
            )
            faulthandler.dump_traceback(file=self._stack_file, all_threads=True)
            self._write_stack("===== end stacks =====\n")
        except Exception:
            pass

    # ------------------------------------------------------------ 生命周期

    def record(
        self,
        reason: str,
        exit_code: int | None = None,
        detail: str = "",
    ) -> None:
        """写一条生命周期记录：怎么结束的、活了多久、退出时在干什么。"""
        try:
            record: dict[str, Any] = {
                "pid": self.pid,
                "startedAt": int(self.started_at),
                "endedAt": int(_now()),
                "durationSeconds": round(time.monotonic() - self.started_monotonic, 3),
                "reason": reason,
                "exitCode": exit_code,
                "threadCount": threading.active_count(),
                "registeredSignals": list(self._registered),
            }
            if detail:
                record["detail"] = detail
            self._append_ledger(
                json.dumps(record, ensure_ascii=False) + "\n"
            )
        except Exception:
            pass

    # ---------------------------------------------------------------- 内部

    def _write_stack(self, text: str) -> None:
        """写栈转储文件。转储文件大且只在出事时增长，所以与账本分开。"""
        if self._stack_file is None:
            return
        try:
            self._stack_file.write(text)
            self._stack_file.flush()
        except Exception:
            pass

    def _append_ledger(self, line: str) -> None:
        """追加一条生命周期记录。超限就滚动一份，避免诊断文件无限增长。"""
        try:
            os.makedirs(self.directory, exist_ok=True)
            if (
                os.path.exists(self.ledger_path)
                and os.path.getsize(self.ledger_path) > MAX_LEDGER_BYTES
            ):
                os.replace(self.ledger_path, self.ledger_path + ".1")
            with io.open(self.ledger_path, "a", encoding="utf-8") as handle:
                handle.write(line)
        except Exception:
            pass

    def close(self) -> None:
        try:
            if self._stack_file is not None:
                self._stack_file.close()
                self._stack_file = None
        except Exception:
            pass

    # ------------------------------------------------------------ 上下文

    def __enter__(self) -> "ProcessDiagnostics":
        self.install()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def describe_environment() -> str:
    """一行摘要，进账本的detail 用。纯诊断，不影响任何逻辑。"""
    try:
        return "python=%s cwd=%s" % (sys.version.split()[0], os.getcwd())
    except Exception:
        return "unknown"
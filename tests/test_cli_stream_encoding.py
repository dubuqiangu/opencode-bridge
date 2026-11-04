"""``--status`` 在**非 UTF-8 重定向**下不再因字符编码崩溃的回归护栏（2026-11-05 实测缺陷）。

缺陷形状
========
直接连控制台时，Windows 的 ``sys.stdout`` 是 UTF-8 控制台 IO，什么字符都写得出去；
一旦**重定向或接管道**（``python -m opencode_bridge --status > out.txt``，或插件
spawn 后按 UTF-8 读 stdout），Python 改用 **ANSI 代码页**（本机 cp936/GBK）编码，
而 ``--status`` / ``--setup`` 的文案里有 ``⇒``（U+21D2）、``⚠``（U+26A0）这类
cp936 装不下的字符 ⇒ ``print`` 抛 ``UnicodeEncodeError``，退化成 ``main`` 的兜底
``except``、以退出码 1 收场，视图可能一段都没打出来。

复现（修复前实测，**退出码 1**）::

    python -m opencode_bridge --status > out.txt
    UnicodeEncodeError: 'gbk' codec can't encode character '\\u21d2' in position 37

本文件用**真的 cp936 文本流**精确复现那个失败现场 —— ⛔ 刻意不用 ``io.StringIO``：
它永远写得出去，抓不到这个缺陷（AGENTS.md §7.1：探针本身要先证明会红）。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from opencode_bridge import __main__ as cli  # noqa: E402
from opencode_bridge import platform_status  # noqa: E402
from opencode_bridge.opencode_client import OpenCodeError  # noqa: E402

#: 缺陷现场那两个字符：``⇒`` 在「白名单为空 = 全开」的图例里，``⚠`` 在状态表的
#: 「未设=全开」列与「授权键冲突」段里。
UNENCODABLE_SYMBOLS = ("⇒", "⚠")


def _ansi_code_page_stream() -> tuple[io.TextIOWrapper, io.BytesIO]:
    """一个**真的**装不下 :data:`UNENCODABLE_SYMBOLS` 的 cp936 严格流。

    返回 ``(stream, raw)``：``raw`` 是它底下的字节缓冲，正对着
    `python ... > out.txt` 落盘的那份字节。
    """
    raw = io.BytesIO()
    return io.TextIOWrapper(raw, encoding="cp936", errors="strict"), raw


def _open_branch_config(directory: str) -> str:
    """写一份 ``config_version: 0`` 的配置 ⇒ 走「空 = 全开」那套文案（含 ``⇒``）。

    ``config_version < 2`` 时 :func:`opencode_bridge.pairing.empty_allowlist_is_open`
    为真 ⇒ ``run_status`` 打印含 ``⇒`` 的图例；同时空适配器表里每个平台都会带
    ``⚠ 未设=全开`` ⇒ 两个缺陷字符都必然出现。
    """
    path = os.path.join(directory, "config.json")
    with io.open(path, "w", encoding="utf-8") as handle:
        json.dump({"config_version": 0, "adapters": {}}, handle)
    return path


class TheGuardStreamReallyIsTheFailureMode(unittest.TestCase):
    """⭐ 反「恒真」：先证明这个探针**真的**会红，否则下面的「不崩」是空转。

    AGENTS.md：一条恒真的守门比没有守门更危险。下面这条断言证明 cp936 严格流
    确实装不下那两个字符 —— 若哪天有人把流换成 UTF-8/StringIO，本文件会先红。
    """

    def test_the_cp936_stream_cannot_encode_the_status_symbols(self):
        for symbol in UNENCODABLE_SYMBOLS:
            with self.subTest(symbol=symbol):
                stream, _raw = _ansi_code_page_stream()
                with self.assertRaises(UnicodeEncodeError):
                    stream.write(symbol)
                    stream.flush()


class ConfigureStandardStreamsTests(unittest.TestCase):
    """``__main__._configure_standard_streams`` 本身的判据。"""

    def test_it_rewires_an_ansi_code_page_stream_to_utf8(self):
        """把 cp936 严格流换成 UTF-8 后，缺陷字符必须写得出去、且是 UTF-8 字节。"""
        stream, raw = _ansi_code_page_stream()
        previous = sys.stdout
        try:
            sys.stdout = stream
            cli._configure_standard_streams()
            self.assertEqual(stream.encoding.lower().replace("-", ""), "utf8")
            print("\u21d2 \u26a0")
            stream.flush()
        finally:
            sys.stdout = previous
        # 只断内容与编码，不钉换行：Windows 下 TextIOWrapper 默认把 \n 翻成 \r\n。
        self.assertEqual(raw.getvalue().decode("utf-8").strip(), "\u21d2 \u26a0")

    def test_it_leaves_a_stream_without_reconfigure_alone(self):
        """``redirect_stdout(io.StringIO())`` 用的替身没有 ``reconfigure`` ⇒ 跳过、不抛。"""
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            cli._configure_standard_streams()
            print("still works")
        self.assertEqual(buffer.getvalue(), "still works\n")


class StatusRedirectedToAnAnsiCodePageTests(unittest.TestCase):
    """端到端：用户真敲的那条命令在 cp936 重定向下必须退出 0、且视图完整。"""

    def _run_status_through_main(self) -> tuple[int, bytes, bytes]:
        with tempfile.TemporaryDirectory() as raw_dir:
            config_path = _open_branch_config(raw_dir)
            stdout_stream, stdout_raw = _ansi_code_page_stream()
            stderr_stream, stderr_raw = _ansi_code_page_stream()
            # ⛔ 不联网：``run_status`` 第一段就会调它。抛错会被那段自己的 except
            #    兜住、打印「不可达」并继续 —— 本用例要的是**后面的**图例与状态表。
            # 同时把 bridge 目录钉在临时目录上（``run_status`` 只读那份记录，但
            # tests/bridge_dir_isolation_scan 的结构契约要求入口调用点显式隔离）。
            with mock.patch.object(
                platform_status,
                "discover_endpoint",
                side_effect=OpenCodeError("no local opencode service"),
            ), mock.patch.object(platform_status, "_bridge_dir", lambda: raw_dir):
                with contextlib.redirect_stdout(stdout_stream), contextlib.redirect_stderr(
                    stderr_stream
                ):
                    exit_code = cli.main(["--config", config_path, "--status"])
            stdout_stream.flush()
            stderr_stream.flush()
            return exit_code, stdout_raw.getvalue(), stderr_raw.getvalue()

    def test_status_under_cp936_redirect_exits_zero_without_traceback(self):
        """⭐ 修复前：``UnicodeEncodeError`` → ``main`` 兜底 → 退出码 1 + traceback。

        修复后：退出码 0、``⇒``/``⚠`` 都真的打了出来、stderr 上没有 traceback。
        """
        exit_code, out_bytes, err_bytes = self._run_status_through_main()

        self.assertEqual(
            exit_code,
            0,
            "cp936 重定向下 --status 又崩了 —— 它会退化成 main 的兜底 except（退出码 1）",
        )
        stderr_text = err_bytes.decode("utf-8", "replace")
        self.assertNotIn(
            "Traceback",
            stderr_text,
            "兜底 except 说明 print 又抛了 —— 那正是本文件要钉的缺陷",
        )
        # 修复后写的是 UTF-8 字节（与插件 ``chunk.toString("utf8")`` 两侧一致）。
        stdout_text = out_bytes.decode("utf-8")
        self.assertIn("== opencode 服务 ==", stdout_text)
        for symbol in UNENCODABLE_SYMBOLS:
            with self.subTest(symbol=symbol):
                self.assertIn(
                    symbol,
                    stdout_text,
                    "修复后仍写不出 %r ⇒ 编码没真的换成 UTF-8" % symbol,
                )


if __name__ == "__main__":
    unittest.main()

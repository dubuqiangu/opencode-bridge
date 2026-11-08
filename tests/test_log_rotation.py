"""Fix 4: the Python process owns a size-bounded rotating log file.

Pins the four things that can silently regress:

1. records actually reach the file (it is not just declared)
2. the file rotates at max_bytes and the bound holds across restarts
   -- appending on start is not "start over", which was the whole bug
3. install_redaction_filter() scrubs what lands IN THE FILE.  The filter
   snapshots the handlers that exist when it runs, so a file handler attached
   afterwards would leak.  __main__._setup_logging must therefore attach the
   file first; this test drives that real function.
4. the off switches are honest: log_max_bytes <= 0 and an unwritable
   directory both degrade to stderr-only and never raise.
"""
from __future__ import annotations

import io
import logging
import logging.handlers
import os
import tempfile
import unittest

from opencode_bridge import logging_setup

DIGITS = "1" + "2" + "3" + "4" + "5" + "6" + "7" + "8" + "9" + "0"
TOKEN = DIGITS + ":" + "A" * 35


def remove_tree(path):
    for name in os.listdir(path):
        full = os.path.join(path, name)
        try:
            os.remove(full)
        except OSError:
            remove_tree(full)
    os.rmdir(path)


class TempLogDirTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="bridge-log-")
        self.addCleanup(remove_tree, self.directory)
        logging_setup.detach_rotating_file_handler()
        self.addCleanup(logging_setup.detach_rotating_file_handler)
        self.addCleanup(logging.getLogger().setLevel, logging.WARNING)

    def path_to(self, name):
        return os.path.join(self.directory, name)


class InstallsAndWritesTests(TempLogDirTest):
    def test_a_record_reaches_the_file(self):
        path = self.path_to("bridge.log")
        handler = logging_setup.install_rotating_file_handler(
            path, max_bytes=1024 * 1024, backup_count=2
        )
        self.assertIsNotNone(handler)
        self.assertIn(handler, logging.getLogger().handlers)
        logging.getLogger("opencode_bridge.somewhere").warning("hello-from-test")
        handler.flush()

        with io.open(path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("hello-from-test", text)
        self.assertIn("opencode_bridge.somewhere", text)
        self.assertIn("WARNING", text)

    def test_second_install_reuses_the_handler_instead_of_duplicating(self):
        path = self.path_to("bridge.log")
        first = logging_setup.install_rotating_file_handler(
            path, max_bytes=1024 * 1024, backup_count=2
        )
        second = logging_setup.install_rotating_file_handler(
            path, max_bytes=1024 * 1024, backup_count=2
        )
        self.assertIs(first, second)
        owned = [
            handler
            for handler in logging.getLogger().handlers
            if getattr(handler, logging_setup.LOG_FILE_MARKER, False)
        ]
        self.assertEqual(len(owned), 1)


class RotationTests(TempLogDirTest):
    def test_the_file_rotates_and_the_total_stays_bounded(self):
        path = self.path_to("bridge.log")
        max_bytes = 2000
        backup_count = 2
        handler = logging_setup.install_rotating_file_handler(
            path, max_bytes=max_bytes, backup_count=backup_count
        )
        self.assertIsNotNone(handler)
        record_logger = logging.getLogger("opencode_bridge.rotation-test")
        record_logger.setLevel(logging.INFO)
        payload = "x" * 200
        for index in range(200):
            record_logger.info("%03d %s", index, payload)
        handler.flush()

        names = sorted(os.listdir(self.directory))
        self.assertIn("bridge.log", names)
        rotated = [name for name in names if name.startswith("bridge.log.")]
        self.assertTrue(rotated, "file never rotated at max_bytes")
        total = 0
        for name in names:
            total += os.path.getsize(os.path.join(self.directory, name))
        bound = max_bytes * (backup_count + 1)
        self.assertLessEqual(total, bound)
        self.assertGreater(total, max_bytes)

    def test_a_second_run_does_not_grow_the_footprint_past_the_bound(self):
        path = self.path_to("bridge.log")
        max_bytes = 2000
        backup_count = 1
        bound = max_bytes * (backup_count + 1)
        record_logger = logging.getLogger("opencode_bridge.restart-test")
        record_logger.setLevel(logging.INFO)
        payload = "y" * 200
        for _ in range(3):
            handler = logging_setup.install_rotating_file_handler(
                path, max_bytes=max_bytes, backup_count=backup_count
            )
            self.assertIsNotNone(handler)
            for index in range(40):
                record_logger.info("%03d %s", index, payload)
            handler.flush()
            self.assertTrue(logging_setup.detach_rotating_file_handler())
            total = 0
            for name in os.listdir(self.directory):
                total += os.path.getsize(os.path.join(self.directory, name))
            self.assertLessEqual(total, bound)


class OffSwitchTests(TempLogDirTest):
    def test_non_positive_max_bytes_disables_the_file(self):
        path = self.path_to("bridge.log")
        self.assertIsNone(
            logging_setup.install_rotating_file_handler(path, max_bytes=0, backup_count=1)
        )
        self.assertIsNone(
            logging_setup.install_rotating_file_handler(path, max_bytes=-5, backup_count=1)
        )
        self.assertFalse(os.path.exists(path))


class RedactionOrderingTests(TempLogDirTest):
    """The file handler must be in place BEFORE the filter snapshots handlers."""

    def test_the_real_setup_logging_route_leaves_no_plaintext_token_in_the_file(self):
        from opencode_bridge.__main__ import _setup_logging

        path = self.path_to("bridge.log")
        _setup_logging(
            "INFO",
            {"log_file": path, "log_max_bytes": 1024 * 1024, "log_backup_count": 3},
        )
        logging.getLogger("opencode_bridge.adapters.telegram").warning(
            "conv telegram:chat telegram token = " + TOKEN
        )
        root_file_handlers = [
            handler
            for handler in logging.getLogger().handlers
            if isinstance(handler, logging.handlers.RotatingFileHandler)
        ]
        self.assertTrue(root_file_handlers)
        for handler in root_file_handlers:
            handler.flush()

        self.assertTrue(os.path.exists(path), "no file was created by _setup_logging")
        with io.open(path, encoding="utf-8") as handle:
            text = handle.read()

        self.assertIn("telegram", text)
        self.assertNotIn(TOKEN, text)


class UnwritableDirectoryTests(TempLogDirTest):
    def test_an_unopenable_path_degrades_to_stderr_instead_of_raising(self):
        from opencode_bridge.__main__ import _setup_logging

        blocked = self.path_to("plain-file")
        with io.open(blocked, "w", encoding="utf-8") as handle:
            handle.write("not a directory")
        nested = os.path.join(blocked, "logs", "bridge.log")

        with self.assertLogs("opencode_bridge", level="WARNING") as caught:
            _setup_logging("INFO", {"log_file": nested, "log_max_bytes": 4096})
        self.assertTrue(any("stderr" in line for line in caught.output))
        self.assertFalse(os.path.exists(nested))


if __name__ == "__main__":
    unittest.main()

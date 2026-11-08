"""Process-wide logging setup for the bridge (Fix 4).

Why this module exists
----------------------
Before 2026-11-07 the bridge only ever logged to ``stderr``: ``__main__._setup_logging``
called ``logging.basicConfig(level=..., format=...)`` with no ``filename`` and no
``handlers``, so the *only* sink was a ``StreamHandler`` writing to stderr. The
TypeScript plugin then spawned the child with

    stdio: ["ignore", fd, fd]        # plugin/index.ts, spawn()

i.e. it redirected that stderr straight into ``<logDir>/bridge-output.log`` using
``fs.openSync(..., "a")``. Nothing ever rotated that file, and on this machine it
grew to ~227 MB, which made "just open the log and look" impossible -- the very
thing you need when the bridge is misbehaving.

So the fix is a half fix, and the unowned half is stated here rather than left to
be discovered:

1. (DONE) Python opens its own size-bounded rotating file (this module). Restarting
   the process does not reset the bound: a new run appends to the same files and
   rotates them the same way, so the on-disk footprint stays
   ``log_max_bytes * (log_backup_count + 1)`` across restarts.
2. (RESIDUAL, NOT DONE) ``plugin/index.ts`` still redirects the child's stderr
   into ``bridge-output.log`` -- see the documented residual note at its
   ``spawn()`` call. That file therefore remains unbounded; it is bounded only by
   whatever separates/sizes it on the operator's side. Records that go through
   the Python ``logging`` stack are ALSO written to our rotating file (and are
   scrubbed there), so ``bridge-output.log`` is now a duplicate sink rather than
   the only one -- but a child that writes straight to ``stderr``, bypassing
   ``logging``, lands only in the unbounded file.

That residual is deliberate: fixing it requires replacing fd redirection with
"pipe + line-by-line redaction", which costs the plugin its event loop and can
drop output if the plugin itself dies. See ``plugin/index.ts`` for the full
trade-off. This module must NOT depend on that side having been changed -- both
halves are independently safe, and the bound here holds either way.

Route A (chosen by the user): the Python process owns the log file.

Redaction ordering
------------------
``install_redaction_filter()`` walks the handlers that already exist. If we
attached the file handler *after* it, every line written to the file would be
unscrubbed. So :func:`opencode_bridge.__main__._setup_logging` attaches the
handler first and only then calls ``install_redaction_filter()``. This module
deliberately does not call it, so tests can install a handler without dragging
in the redaction wiring or the reverse.

The handler is idempotent: a second ``main()`` in the same process (the test
suite does this) reuses the existing one instead of stacking a second copy,
which would otherwise duplicate every line and double the effective write rate.
"""

from __future__ import annotations

import logging
import logging.handlers
import os

__all__ = [
    "LOG_FILE_MARKER",
    "detach_rotating_file_handler",
    "install_rotating_file_handler",
    "resolve_log_path",
]

#: Attribute set on the handler we own. Used to find it again on a second call
#: instead of appending a duplicate.
LOG_FILE_MARKER = "_opencode_bridge_rotating_file"

#: Format shared with ``basicConfig`` so the stderr copy and the file copy are
#: diffable line by line.
_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def resolve_log_path(log_file: str, bridge_dir: str | None) -> str:
    """Turn the configured ``log_file`` into a path.

    Relative names are resolved against ``bridge_dir`` (the bridge directory,
    i.e. what the plugin calls ``bridgeDir``), never against the current working
    directory -- the plugin spawns the child with ``cwd`` set, but a manual
    ``python -m opencode_bridge`` from another folder must still find the same
    file that ``--status`` and the plugin look at.
    """
    if os.path.isabs(log_file):
        return log_file
    base = bridge_dir or os.getcwd()
    return os.path.join(base, log_file)


def _existing_owned_handler() -> logging.Handler | None:
    for handler in logging.getLogger().handlers:
        if getattr(handler, LOG_FILE_MARKER, False):
            return handler
    return None


def install_rotating_file_handler(
    path: str,
    *,
    max_bytes: int,
    backup_count: int,
    level: int = logging.DEBUG,
    formatter: logging.Formatter | None = None,
) -> logging.Handler | None:
    """Attach (or return the already-attached) size-bounded file handler to the root logger.

    Returns the handler that receives the records, or ``None`` when the file
    sink is disabled. ``level`` defaults to ``DEBUG`` so this handler is never
    the one that decides *what* is logged -- the root level and the stderr
    handler keep that authority, and the file simply records everything that
    passes the gate.

    ``max_bytes <= 0`` disables the file sink entirely (Fix 4's off switch);
    the caller must tolerate ``None``.
    """
    if max_bytes <= 0:
        return None

    existing = _existing_owned_handler()
    if existing is not None:
        return existing

    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    handler = logging.handlers.RotatingFileHandler(
        path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
        errors="backslashreplace",
        delay=True,
    )
    handler.setLevel(level)
    setattr(handler, LOG_FILE_MARKER, True)
    if formatter is None:
        formatter = logging.Formatter(_LOG_FORMAT)
    handler.setFormatter(formatter)
    logging.getLogger().addHandler(handler)
    return handler


def detach_rotating_file_handler() -> bool:
    """Remove our handler from the root logger; returns ``True`` if one was removed."""
    root = logging.getLogger()
    removed = False
    for handler in list(root.handlers):
        if getattr(handler, LOG_FILE_MARKER, False):
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:  # noqa: BLE001
                pass
            removed = True
    return removed

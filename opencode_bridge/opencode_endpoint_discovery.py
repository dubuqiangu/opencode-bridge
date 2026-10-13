"""Endpoint discovery for a local OpenCode service.

解析优先级（显式参数 > 环境变量 > service.json 注册文件）见
:func:`discover_endpoint` 的 docstring；本模块与客户端类无关，
logger 沿用原通道名 ``opencode_bridge.opencode_client``（日志逐字节不变）。

Provenance: 2026-10-13 §5 第六刀（``opencode_client.py`` 文件刀）：从
``opencode_client.py`` 逐字移植，行为零变化。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from .opencode_wire import Endpoint, OpenCodeError

logger = logging.getLogger("opencode_bridge.opencode_client")

DEFAULT_SERVICE_URL = "http://127.0.0.1:4096"


# ----------------------------------------------------------------------
# endpoint discovery
# ----------------------------------------------------------------------
def _service_file_candidates() -> list[str]:
    candidates: list[str] = []
    env = os.environ.get("OPENCODE_SERVICE_FILE")
    if env:
        candidates.append(env)
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        candidates.append(os.path.join(xdg, "opencode", "service.json"))
    candidates.append(
        os.path.join(os.path.expanduser("~"), ".local", "state", "opencode", "service.json")
    )
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        candidates.append(os.path.join(local_appdata, "opencode", "service.json"))
    return candidates


def _read_service_file(path: str) -> dict[str, Any] | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError) as exc:
        logger.warning("cannot read service file %s: %s", path, exc)
        return None
    if not isinstance(raw, dict):
        logger.warning("service file %s is not a JSON object", path)
        return None
    return raw


def discover_endpoint(
    url: str = "", password: str = "", *, service_file: str | None = None
) -> Endpoint:
    """Resolve the OpenCode service address.

    Priority:

    1. explicit ``url`` + ``password`` (both non-empty)
    2. each of ``url`` / ``password`` independently, non-empty value wins
    3. environment ``OPENCODE_URL`` / ``OPENCODE_PASSWORD``
    4. service registration file JSON ``{"url": ..., "password": ...}``
       searched at ``$OPENCODE_SERVICE_FILE``,
       ``$XDG_STATE_HOME/opencode/service.json``,
       ``~/.local/state/opencode/service.json``,
       ``%LOCALAPPDATA%/opencode/service.json``

    Raises :class:`OpenCodeError` when nothing resolves.
    """
    final_url = (url or "").strip() or os.environ.get("OPENCODE_URL", "").strip()
    final_password = (password or "").strip() or os.environ.get(
        "OPENCODE_PASSWORD", ""
    ).strip()

    if not (final_url and final_password):
        service_data: dict[str, Any] | None = None
        if service_file is not None:
            service_data = _read_service_file(service_file)
        else:
            for candidate in _service_file_candidates():
                if os.path.isfile(candidate):
                    service_data = _read_service_file(candidate)
                    if service_data is not None:
                        break
        if service_data is not None:
            if not final_url:
                final_url = str(service_data.get("url") or "").strip()
            if not final_password:
                final_password = str(service_data.get("password") or "").strip()

    if not final_url and final_password:
        final_url = DEFAULT_SERVICE_URL

    if not final_url or not final_password:
        raise OpenCodeError("cannot resolve opencode endpoint")

    return Endpoint(final_url, final_password)

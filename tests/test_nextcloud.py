"""T3.5 测试 —— Nextcloud Talk 入站（长轮询）+ REST 出站。**零真实网络**。

HTTP 层 monkeypatch ``_request``（照 ``tests/test_matrix.py`` 的打桩方式），但**返回的是
真的 ``urllib.error.HTTPError(304, …)`` 对象并真的走一遍 :meth:`_request` 的异常分支**
—— 因为"304 在 urllib 里是异常而不是返回值"正是本适配器最容易写错的一处，只打桩
``_request`` 的返回值根本测不到它。

对照测试清单：
能力声明 + outbound_tokens ⊆ required_tokens、缺凭据不起线程、请求头（OCS-APIRequest
小写 true / Basic / 只走 ocs/v2.php）、304 当成无新消息、游标推进与先推进再处理、
过滤矩阵（含 messageParameters 含 file 的反向陷阱）、取不到 uid 入站停摆、
发消息 URL/urlencode body/201/400/403、编辑用 PUT .../{messageId}、只读会话不发、
并发上限、周期性全量刷新、stop 及时结束。
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
import unittest
import urllib.error
import urllib.parse

logging.getLogger("opencode_bridge").addHandler(logging.NullHandler())

import opencode_bridge.adapters.nextcloud as nc_mod
from opencode_bridge.adapters import adapter_class, build, registered_names
from opencode_bridge.adapters.nextcloud import (
    CHAT_PATH,
    MAX_POLL_TIMEOUT,
    MESSAGE_LIMIT,
    OCS_APIREQUEST_VALUE,
    PERM_CHAT,
    ROOMS_PATH,
    USER_PATH,
    NextcloudAdapter,
    _Resp,
)
from opencode_bridge.hooks import Inbound, MsgHandle, Outbound, SendError

BASE_URL = "https://cloud.example.test"
SUBDIR_BASE_URL = "https://host.example.test/nextcloud"
USERNAME = "bridgebot"
PASSWORD = "app-password-0123456789abcdef"
MY_UID = "BridgeBot"
OTHER_UID = "alice"
ROOM = "r4om3t0k3n"
ROOM2 = "s3condr00m"
CID = f"nextcloud:{ROOM}"


# ----------------------------------------------------------------------
# 测试替身
# ----------------------------------------------------------------------
class RecordingHooks:
    def __init__(self) -> None:
        self.inbounds: list[Inbound] = []

    def on_inbound(self, inbound: Inbound) -> None:
        self.inbounds.append(inbound)

    def on_callback(self, conversation_id: str, data: str, query_id: str) -> None:
        pass


class ExplodingHooks(RecordingHooks):
    def on_inbound(self, inbound: Inbound) -> None:
        raise RuntimeError("上层炸了")


# ----------------------------------------------------------------------
# OCS 响应构造
# ----------------------------------------------------------------------
def ocs(data, *, status_code: int = 200, message: str = "OK") -> dict:
    return {"ocs": {"meta": {"status": "ok", "statuscode": status_code,
                             "message": message}, "data": data}}


def ocs_error(error: str, *, status_code: int = 400, message: str = "Bad Request") -> dict:
    return {"ocs": {"meta": {"status": "failure", "statuscode": status_code,
                             "message": message},
                    "data": {"error": error}}}


def room_entry(token: str = ROOM, *, read_only: int = 0, lobby_state: int = 0,
               permissions: int = 384) -> dict:
    return {
        "token": token,
        "id": 7,
        "name": "群名",
        "readOnly": read_only,
        "lobbyState": lobby_state,
        "permissions": permissions,
        "type": 2,
    }


def message(
    message_id: int = 100,
    text: str = "你好",
    *,
    actor_id: str = OTHER_UID,
    actor_type: str = "users",
    message_type: str = "comment",
    system_message: str = "",
    message_parameters=None,
    token: str = ROOM,
    timestamp: int = 1700000000,
) -> dict:
    """一条 ``lib/Model/Message::toArray()`` 形状的消息。"""
    return {
        "id": message_id,
        "token": token,
        "actorId": actor_id,
        "actorType": actor_type,
        "actorDisplayName": "Alice",
        "timestamp": timestamp,          # **秒**级
        "message": text,
        "messageParameters": message_parameters or {},
        "messageType": message_type,
        "systemMessage": system_message,
    }


# ----------------------------------------------------------------------
# HTTP 打桩
# ----------------------------------------------------------------------
class Stub:
    """替换 ``_request`` 的打桩器。

    ``script`` 可以是 ``_Resp``（直接返回），也可以是一个**可调用对象**（接收
    ``(method, path, params, form, timeout)``，返回 ``_Resp`` 或抛异常）。抛出来的
    异常会被 :meth:`NextcloudAdapter._request` 的 ``except`` 真实接住 —— 这样 304 /
    传输失败这两条异常分支才真的被覆盖到。
    """

    def __init__(self, script=None, default=None):
        self.script = list(script or [])
        self.default = default
        self.calls: list[tuple] = []
        self.inflight = 0
        self.max_inflight = 0

    def __call__(self, method, path, *, params=None, form=None, timeout=None):
        self.calls.append(
            {"method": method, "path": path, "params": dict(params or {}),
             "form": dict(form) if form is not None else None, "timeout": timeout}
        )
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            item = self.script.pop(0) if self.script else self.default
            if callable(item):
                return item(method, path, params, form, timeout)
            return item
        finally:
            self.inflight -= 1

    # -- 断言助手 --------------------------------------------------------
    def paths(self) -> list[str]:
        return [c["path"] for c in self.calls]

    def urls(self, adapter: NextcloudAdapter) -> list[str]:
        return [adapter._url(c["path"], c["params"]) for c in self.calls]

    def params_of(self, fragment: str) -> dict:
        for call in self.calls:
            if fragment in call["path"]:
                return call["params"]
        raise AssertionError(f"没有匹配 {fragment!r} 的请求：{self.paths()}")


def attach(adapter: NextcloudAdapter, stub: Stub) -> Stub:
    adapter._request = stub  # type: ignore[method-assign]
    return stub


def make_adapter(config: dict | None = None, hooks: RecordingHooks | None = None,
                 *, base_url: str = BASE_URL,
                 user_id: str = MY_UID) -> tuple[NextcloudAdapter, RecordingHooks]:
    cfg = {
        "base_url": base_url,
        "username": USERNAME,
        "password": PASSWORD,
        "user_id": user_id,
    }
    if config:
        cfg.update(config)
    hooks = hooks or RecordingHooks()
    adapter = NextcloudAdapter(cfg, hooks)
    adapter.min_interval = 0
    return adapter, hooks


def known_room(adapter: NextcloudAdapter, token: str = ROOM, *, can_send: bool = True):
    """把一个"已知会话"塞进 adapter（不发 HTTP），并同步轮询顺序表。"""
    from opencode_bridge.adapters.nextcloud import _Room

    room = _Room(token=token, cursor=100, can_send=can_send, bootstrapped=True)
    adapter.rooms[token] = room
    # 与 _refresh_rooms 的行为保持一致：轮询顺序表 = 全部已知会话的排序
    with adapter._poll_lock:
        adapter._poll_order = sorted(adapter.rooms)
    return room


def ok_chat(message_id: int = 500) -> _Resp:
    return _Resp(201, ocs(message(message_id)), {})


# ----------------------------------------------------------------------
# 1) 能力声明
# ----------------------------------------------------------------------
class TestCapabilities(unittest.TestCase):
    def test_capabilities_truthful(self):
        adapter, _ = make_adapter()
        caps = adapter.capabilities()
        self.assertEqual(caps["name"], "nextcloud")
        self.assertEqual(caps["label"], "Nextcloud Talk")
        self.assertEqual(caps["max_message_length"], MESSAGE_LIMIT)
        self.assertEqual(caps["max_message_length"], 32000)
        self.assertTrue(caps["supports_inbound"])
        self.assertFalse(caps["supports_inline_buttons"])
        self.assertFalse(caps["supports_media"])
        self.assertEqual(caps["typed_command_prefix"], "/")

    def test_32000_is_a_source_constant_not_configurable(self):
        """32000 是 ``ChatManager::MAX_CHAT_LENGTH`` —— 源码常量，不读配置项。"""
        adapter = NextcloudAdapter(
            {"base_url": BASE_URL, "username": "u", "password": "p",
             "max_message_length": 12345, "max_length": 999},
            RecordingHooks(),
        )
        self.assertEqual(adapter.max_message_length, 32000)
        self.assertEqual(adapter.effective_max_length, 32000)
        self.assertEqual(NextcloudAdapter.__dict__["max_message_length"], 32000)

    def test_outbound_tokens_subset_of_required(self):
        """仓库不变量：outbound_tokens ⊆ required_tokens。"""
        cls = adapter_class("nextcloud")
        self.assertEqual(cls.required_tokens, ("base_url", "username", "password"))
        self.assertEqual(cls.outbound_tokens, ("base_url", "username", "password"))
        self.assertLessEqual(set(cls.outbound_tokens), set(cls.required_tokens))

    def test_registered_in_registry(self):
        self.assertIn("nextcloud", registered_names())
        adapter = build(
            "nextcloud",
            {"base_url": BASE_URL, "username": "u", "password": "p"},
            RecordingHooks(),
        )
        self.assertIsInstance(adapter, NextcloudAdapter)

    def test_build_with_empty_config_does_not_raise(self):
        adapter = build("nextcloud", {}, RecordingHooks())
        self.assertEqual(adapter.base_url, "")
        self.assertFalse(adapter.running)

    def test_answer_is_noop(self):
        adapter, _ = make_adapter()
        self.assertIsNone(adapter.answer("q1"))
        self.assertIsNone(adapter.answer("q1", "text"))

    def test_subpath_base_url_is_preserved(self):
        adapter, _ = make_adapter(base_url=f"{SUBDIR_BASE_URL}/")
        self.assertEqual(adapter.base_url, SUBDIR_BASE_URL)
        self.assertEqual(
            adapter._url(USER_PATH),
            "https://host.example.test/nextcloud/ocs/v2.php/cloud/user?format=json",
        )


# ----------------------------------------------------------------------
# 2) 缺凭据
# ----------------------------------------------------------------------
class TestMissingCredentials(unittest.TestCase):
    def _assert_no_start(self, config: dict, needle: str) -> None:
        adapter = NextcloudAdapter(config, RecordingHooks())
        attach(adapter, Stub(default=_Resp(200, ocs([]))))
        with self.assertLogs("opencode_bridge.adapters.nextcloud", level="WARNING") as cap:
            adapter.start()  # 不得抛
        self.assertIn(needle, "\n".join(cap.output))
        self.assertIsNone(adapter._thread)
        self.assertFalse(adapter.running)

    def test_missing_base_url(self):
        self._assert_no_start({"username": "u", "password": "p"}, "base_url")

    def test_missing_username(self):
        self._assert_no_start({"base_url": BASE_URL, "password": "p"}, "username")

    def test_missing_password(self):
        self._assert_no_start({"base_url": BASE_URL, "username": "u"}, "password")

    def test_no_request_is_made_when_credentials_missing(self):
        stub = Stub(default=_Resp(200, ocs([])))
        adapter = NextcloudAdapter({"base_url": BASE_URL, "username": "u"}, RecordingHooks())
        attach(adapter, stub)
        adapter.start()
        self.assertEqual(stub.calls, [], "缺凭据时不该打 REST")


# ----------------------------------------------------------------------
# 3) 请求头与端点
# ----------------------------------------------------------------------
class TestRequestHeaders(unittest.TestCase):
    def test_ocs_apirequest_header_is_literal_lowercase_true(self):
        """🔴 服务端是 ``=== 'true'`` 严格比较：``True``/``TRUE``/``1`` 全被判 CSRF → 403。"""
        adapter, _ = make_adapter()
        headers = adapter._ocs_headers()
        self.assertEqual(headers["OCS-APIRequest"], "true")
        self.assertIsInstance(headers["OCS-APIRequest"], str)
        self.assertNotIsInstance(headers["OCS-APIRequest"], bool)
        self.assertEqual(OCS_APIREQUEST_VALUE, "true")
        for wrong in (True, "True", "TRUE", "1", "yes"):
            with self.subTest(value=wrong):
                self.assertNotEqual(str(wrong), OCS_APIREQUEST_VALUE)

    def test_ocs_apirequest_header_reaches_the_wire(self):
        """不只测构造方法：真的组装一次 Request，头确实在里面。"""
        adapter, _ = make_adapter()
        req = adapter._build_request("GET", USER_PATH)
        self.assertEqual(req.get_header("Ocs-apirequest"), "true")
        self.assertEqual(req.get_header("Authorization"),
                         adapter._auth_header())
        self.assertEqual(req.get_header("Accept"), "application-json".replace(
            "-", "/"))
        self.assertTrue(req.full_url.startswith(f"{BASE_URL}/ocs/v2.php/"))

    def test_authorization_is_basic(self):
        adapter, _ = make_adapter()
        header = adapter._auth_header()
        self.assertTrue(header.startswith("Basic "))
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
        self.assertEqual(decoded, f"{USERNAME}:{PASSWORD}")

    def test_url_uses_ocs_v2_only(self):
        """🔴 v1 的状态码恒为 200，不能用。"""
        adapter, _ = make_adapter()
        for path in (USER_PATH, ROOMS_PATH, f"{CHAT_PATH}/{ROOM}"):
            with self.subTest(path=path):
                url = adapter._url(path)
                self.assertIn("/ocs/v2.php/", url)
                self.assertNotIn("ocs/v1.php", url)
                self.assertNotIn("/ocs/v1.php", url)

    def test_url_always_carries_format_json(self):
        adapter, _ = make_adapter()
        url = adapter._url(f"{CHAT_PATH}/{ROOM}", {"lookIntoFuture": 1})
        self.assertIn("format=json", url)
        parsed = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertEqual(parsed["format"], ["json"])
        self.assertEqual(parsed["lookIntoFuture"], ["1"])

    def test_accept_json_header_present(self):
        adapter, _ = make_adapter()
        self.assertEqual(adapter._ocs_headers()["Accept"], "application/json")

    def test_form_body_sets_urlencoded_content_type(self):
        adapter, _ = make_adapter()
        self.assertIn("Content-Type", adapter._ocs_headers(form={"message": "x"}))
        self.assertEqual(
            adapter._ocs_headers(form={"message": "x"})["Content-Type"],
            "application/x-www-form-urlencoded",
        )
        self.assertNotIn("Content-Type", adapter._ocs_headers())

    def test_params_with_none_are_dropped(self):
        adapter, _ = make_adapter()
        url = adapter._url(ROOMS_PATH, {"modifiedSince": None, "limit": 5})
        self.assertNotIn("modifiedSince", url)
        self.assertIn("limit=5", url)

    def test_local_socket_timeout_exceeds_server_poll_timeout(self):
        """🔴 本地 socket 超时必须大于服务端 timeout，否则长轮询白挂。"""
        adapter, _ = make_adapter({"poll_timeout": 25})
        self.assertEqual(adapter.poll_timeout, 25)
        self.assertGreater(adapter._local_timeout, adapter.poll_timeout)
        default, _ = make_adapter()
        self.assertGreater(default._local_timeout, default.poll_timeout)

    def test_poll_timeout_cannot_exceed_server_max(self):
        adapter, _ = make_adapter({"poll_timeout": 120})
        self.assertLessEqual(adapter.poll_timeout, MAX_POLL_TIMEOUT)
        self.assertEqual(adapter.poll_timeout, 30)

    def test_max_concurrent_polls_config_bounds(self):
        adapter, _ = make_adapter({"max_concurrent_polls": 3})
        self.assertEqual(adapter.max_concurrent_polls, 3)
        bad, _ = make_adapter({"max_concurrent_polls": 0})
        self.assertEqual(bad.max_concurrent_polls, 5)
        non_numeric, _ = make_adapter({"max_concurrent_polls": "abc"})
        self.assertEqual(non_numeric.max_concurrent_polls, 5)


# ----------------------------------------------------------------------
# 4) 304 通过真实的 urllib 异常分支
# ----------------------------------------------------------------------
class TestNotModifiedIsNotFailure(unittest.TestCase):
    """🔴 ``urllib`` 只把 2xx 当成功 → 304 是以 ``HTTPError`` 异常形式到达的。

    这里用**真的** ``urllib.request.urlopen`` + 真的 ``_request``，只在最底下把
    ``urlopen`` 换成一个"抛 HTTPError(304)"的替身 —— 这样接住 304 的那段代码才真的
    被执行到，而不是被测试替身架空。
    """

    def _install_fake_urlopen(self, adapter, error: Exception):
        calls: list = []

        def fake_urlopen(req, timeout=None):
            calls.append(req)
            raise error

        import opencode_bridge.adapters.nextcloud as mod

        original = mod.urllib.request.urlopen
        mod.urllib.request.urlopen = fake_urlopen
        self.addCleanup(setattr, mod.urllib.request, "urlopen", original)
        return calls

    def test_304_from_urlopen_is_treated_as_no_new_messages(self):
        adapter, _ = make_adapter()
        known_room(adapter)
        error = urllib.error.HTTPError(
            adapter._url(f"{CHAT_PATH}/{ROOM}"), 304, "Not Modified", {}, None
        )
        self._install_fake_urlopen(adapter, error)
        room = adapter.rooms[ROOM]
        room.cursor = 777
        # 不抛、不记失败、游标不变
        adapter._poll_once(ROOM)
        self.assertEqual(room.cursor, 777, "304 时游标必须不变")
        self.assertIsNone(adapter.last_send_error)

    def test_304_actually_flows_through_request_exception_branch(self):
        adapter, _ = make_adapter()
        error = urllib.error.HTTPError("http://x/y", 304, "Not Modified", {}, None)
        calls = self._install_fake_urlopen(adapter, error)
        resp = adapter._request(
            "GET", f"{CHAT_PATH}/{ROOM}", params={"lookIntoFuture": 1}
        )
        self.assertEqual(resp.status, 304)
        self.assertEqual(resp.data, {})
        self.assertIsInstance(resp, _Resp)
        self.assertEqual(len(calls), 1)

    def test_304_does_not_reach_the_error_logger(self):
        adapter, _ = make_adapter()
        error = urllib.error.HTTPError("http://x/y", 304, "Not Modified", {}, None)
        self._install_fake_urlopen(adapter, error)
        with self.assertNoLogs("opencode_bridge.adapters.nextcloud", level="WARNING"):
            resp = adapter._request("GET", f"{CHAT_PATH}/{ROOM}")
        self.assertEqual(resp.status, 304)

    def test_other_http_errors_are_returned_not_raised(self):
        adapter, _ = make_adapter()
        body = json.dumps(ocs_error("forbidden", status_code=403)).encode("utf-8")
        error = urllib.error.HTTPError("http://x/y", 403, "Forbidden", {}, None)
        error.read = lambda: body  # type: ignore[method-assign]
        self._install_fake_urlopen(adapter, error)
        resp = adapter._request("GET", f"{CHAT_PATH}/{ROOM}")
        self.assertEqual(resp.status, 403)
        self.assertEqual(resp.data["ocs"]["meta"]["statuscode"], 403)

    def test_transport_failure_maps_to_status_zero(self):
        adapter, _ = make_adapter()
        self._install_fake_urlopen(adapter, urllib.error.URLError("no route to host"))
        resp = adapter._request("GET", USER_PATH)
        self.assertEqual(resp.status, 0)
        self.assertIn("transport error", resp.data["message"])

    def test_socket_timeout_maps_to_status_zero(self):
        adapter, _ = make_adapter()
        self._install_fake_urlopen(adapter, TimeoutError("timed out"))
        self.assertEqual(adapter._request("GET", USER_PATH).status, 0)

    def test_non_json_body_is_reported_not_raised(self):
        adapter, _ = make_adapter()
        error = urllib.error.HTTPError("http://x/y", 500, "Server Error", {}, None)
        error.read = lambda: b"<html>oops</html>"  # type: ignore[method-assign]
        self._install_fake_urlopen(adapter, error)
        resp = adapter._request("GET", USER_PATH)
        self.assertEqual(resp.status, 500)
        self.assertIn("non-JSON", resp.data["message"])


# ----------------------------------------------------------------------
# 5~6) 游标推进
# ----------------------------------------------------------------------
class TestCursor(unittest.TestCase):
    def test_200_cursor_taken_from_response_header(self):
        adapter, hooks = make_adapter()
        room = known_room(adapter)
        stub = attach(adapter, Stub(default=_Resp(
            200,
            ocs([message(101, "第一条")]),
            {"x-chat-last-given": "143"},
        )))
        adapter._poll_once(ROOM)
        self.assertEqual(room.cursor, 143)
        self.assertEqual(len(hooks.inbounds), 1)
        self.assertEqual(stub.params_of(f"{CHAT_PATH}/{ROOM}")["lastKnownMessageId"], 100)

    def test_long_poll_query_parameters(self):
        adapter, _ = make_adapter()
        room = known_room(adapter)
        stub = attach(adapter, Stub(default=_Resp(304, {}, {})))
        adapter._poll_once(ROOM)
        params = stub.params_of(f"{CHAT_PATH}/{ROOM}")
        self.assertEqual(params["lookIntoFuture"], 1)
        self.assertEqual(params["limit"], 100)
        self.assertEqual(params["timeout"], adapter.poll_timeout)
        self.assertEqual(params["lastKnownMessageId"], 100)
        # 只读不打扰：不替用户改已读标记 / 状态 / 通知
        self.assertEqual(params["setReadMarker"], 0)
        self.assertEqual(params["noStatusUpdate"], 1)
        self.assertEqual(params["markNotificationsAsRead"], 0)

    def test_304_leaves_cursor_untouched(self):
        adapter, hooks = make_adapter()
        room = known_room(adapter)
        attach(adapter, Stub(default=_Resp(304, {}, {})))
        adapter._poll_once(ROOM)
        self.assertEqual(room.cursor, 100)
        self.assertEqual(hooks.inbounds, [])

    def test_cursor_advances_before_messages_are_handled(self):
        """先推进再处理：处理抛异常时游标已经前进，不会重放同一批。"""
        adapter, _ = make_adapter(hooks=ExplodingHooks())
        room = known_room(adapter)
        attach(adapter, Stub(default=_Resp(
            200, ocs([message(101), message(102)]),
            {"x-chat-last-given": "555"},
        )))
        with self.assertLogs("opencode_bridge.adapters.nextcloud", level="ERROR"):
            adapter._poll_once(ROOM)
        self.assertEqual(room.cursor, 555, "处理失败也不能让游标停在原地")

    def test_cursor_survives_missing_header(self):
        """200 但没有 X-Chat-Last-Given：保守地保持旧游标（可能重复，但不丢）。"""
        adapter, _ = make_adapter()
        room = known_room(adapter)
        attach(adapter, Stub(default=_Resp(200, ocs([message(101)]), {})))
        with self.assertLogs("opencode_bridge.adapters.nextcloud", level="INFO"):
            adapter._poll_once(ROOM)
        self.assertEqual(room.cursor, 100)

    def test_cursor_taken_even_when_it_points_at_invisible_message(self):
        """该 header 可能指向一条对你不可见的消息，仍应照用（否则重复投递）。"""
        adapter, _ = make_adapter()
        room = known_room(adapter)
        attach(adapter, Stub(default=_Resp(
            200, ocs([]), {"x-chat-last-given": "9999"})))
        adapter._poll_once(ROOM)
        self.assertEqual(room.cursor, 9999)

    def test_token_is_quoted_in_the_path(self):
        adapter, _ = make_adapter()
        weird = "room/with slash?and=stuff"
        known_room(adapter, weird)
        attach(adapter, Stub(default=_Resp(304, {}, {})))
        adapter._poll_once(weird)
        call = [c for c in adapter._request.calls if CHAT_PATH in c["path"]][0]
        self.assertNotIn("with slash", call["path"])
        self.assertIn(urllib.parse.quote(weird, safe=""), call["path"])

    def test_unknown_room_is_skipped(self):
        adapter, _ = make_adapter()
        attach(adapter, Stub(default=_Resp(304, {}, {})))
        adapter._poll_once("nope")
        self.assertEqual(adapter._request.calls, [])

    def test_poll_error_does_not_advance_cursor(self):
        adapter, _ = make_adapter()
        room = known_room(adapter)
        attach(adapter, Stub(default=_Resp(500, ocs_error("boom", status_code=500))))
        with self.assertLogs("opencode_bridge.adapters.nextcloud", level="WARNING"):
            adapter._poll_once(ROOM)
        self.assertEqual(room.cursor, 100)


# ----------------------------------------------------------------------
# bootstrap 游标
# ----------------------------------------------------------------------
class TestBootstrapCursor(unittest.TestCase):
    def test_new_room_bootstraps_with_limit_1(self):
        """⚠️ ``lastKnownMessageId=0`` + ``lookIntoFuture=0`` 返回的是**最新 N 条（降序）**，
        所以 bootstrap 必须 ``limit=1`` 取响应头当游标。"""
        adapter, _ = make_adapter()
        stub = attach(adapter, Stub(default=_Resp(
            200, ocs([message(4242)]), {"x-chat-last-given": "4242"})))
        cursor = adapter._bootstrap_cursor(ROOM)
        self.assertEqual(cursor, 4242)
        params = stub.params_of(f"{CHAT_PATH}/{ROOM}")
        self.assertEqual(params["lookIntoFuture"], 0)
        self.assertEqual(params["limit"], 1)
        self.assertNotIn("lastKnownMessageId", params)

    def test_bootstrap_failure_keeps_cursor_zero(self):
        adapter, _ = make_adapter()
        attach(adapter, Stub(default=_Resp(404, ocs_error("not found", status_code=404))))
        with self.assertLogs("opencode_bridge.adapters.nextcloud", level="INFO"):
            self.assertEqual(adapter._bootstrap_cursor(ROOM), 0)

    def test_bootstrap_runs_once_per_room(self):
        adapter, hooks = make_adapter()
        stub = attach(adapter, Stub(
            [
                _Resp(200, ocs([message(9999)]), {"x-chat-last-given": "9999"}),
                _Resp(304, {}, {}),
                _Resp(304, {}, {}),
            ],
            default=_Resp(304, {}, {}),
        ))
        adapter.rooms[ROOM] = nc_mod._Room(token=ROOM)
        with adapter._poll_lock:
            adapter._poll_order = [ROOM]
        adapter._poll_once(ROOM)
        self.assertEqual(adapter.rooms[ROOM].cursor, 9999)
        adapter._poll_once(ROOM)
        adapter._poll_once(ROOM)
        self.assertEqual(len(stub.calls), 4, "3 轮 = 1 次 bootstrap + 3 次长轮询")
        self.assertEqual(
            sum(1 for c in stub.calls if c["params"].get("limit") == 1), 1,
            "bootstrap 只该做一次",
        )
        self.assertEqual(
            sum(1 for c in stub.calls if c["params"].get("lookIntoFuture") == 1), 3
        )


# ----------------------------------------------------------------------
# 7~8) 消息过滤
# ----------------------------------------------------------------------
class TestMessageFilter(unittest.TestCase):
    def _one(self, msg: dict, *, config: dict | None = None):
        adapter, hooks = make_adapter(config)
        return adapter, adapter._handle_message(ROOM, msg), hooks

    def test_normal_message_becomes_inbound(self):
        adapter, ok, hooks = self._one(message(100, "你好 world"))
        self.assertTrue(ok)
        inbound = hooks.inbounds[0]
        self.assertEqual(inbound.conversation_id, CID)
        self.assertEqual(inbound.text, "你好 world")
        self.assertEqual(inbound.user_id, OTHER_UID)
        self.assertEqual(inbound.message_id, "100")
        self.assertEqual(inbound.kind, "text")
        self.assertEqual(inbound.platform, "nextcloud")
        self.assertEqual(inbound.raw["timestamp"], 1700000000)

    def test_own_message_is_dropped(self):
        _, ok, hooks = self._one(message(actor_id=MY_UID))
        self.assertFalse(ok)
        self.assertEqual(hooks.inbounds, [])

    def test_own_uid_match_requires_actor_type_users(self):
        """Talk 还有 guests / federated_users / bots；只挡 ``users`` 类型的自己。"""
        _, ok, hooks = self._one(
            message(actor_id=MY_UID, actor_type="guests")
        )
        self.assertTrue(ok, "guests 类型的同名 actor 不该被当成自己")
        self.assertEqual(len(hooks.inbounds), 1)

    def test_uid_comparison_is_case_sensitive(self):
        """uid **大小写敏感**：``/cloud/user`` 给的 id 不能 lower() 后去比对。"""
        _, ok, hooks = self._one(message(actor_id=MY_UID.lower()))
        self.assertTrue(ok, "大小写不同的 uid 不是同一个人")
        self.assertEqual(len(hooks.inbounds), 1)

    def test_non_comment_message_types_dropped(self):
        for message_type in ("system", "command", "comment_deleted"):
            with self.subTest(message_type=message_type):
                _, ok, hooks = self._one(
                    message(message_type=message_type, system_message="")
                )
                self.assertFalse(ok)
                self.assertEqual(hooks.inbounds, [])

    def test_non_empty_system_message_dropped(self):
        _, ok, hooks = self._one(message(system_message="alice 加入了这个会话"))
        self.assertFalse(ok)
        self.assertEqual(hooks.inbounds, [])

    def test_shared_file_is_dropped_even_though_it_looks_like_a_comment(self):
        """⚠️ **反向陷阱**：``file_shared`` 被服务端改写成 ``messageType="comment"``
        **且** ``systemMessage=""`` —— 只看前两条过滤**不干净**，必须查 messageParameters。"""
        msg = message(
            text="",
            message_parameters={
                "file": {"type": "file", "id": "f1", "name": "报告.pdf"},
            },
        )
        self.assertEqual(msg["messageType"], "comment")
        self.assertEqual(msg["systemMessage"], "")
        adapter, ok, hooks = self._one(msg)
        self.assertFalse(ok, "messageParameters 含 file 必须被丢弃")
        self.assertEqual(hooks.inbounds, [])

    def test_shared_object_is_dropped(self):
        """``object_shared``（分享位置 / 投票）同样被改写成 comment + 空 systemMessage。"""
        adapter, ok, hooks = self._one(
            message(text="分享了一个位置",
                    message_parameters={"object": {"type": "location", "id": "o1"}})
        )
        self.assertFalse(ok)
        self.assertEqual(hooks.inbounds, [])

    def test_poll_object_is_dropped(self):
        adapter, ok, hooks = self._one(
            message(text="投票",
                    message_parameters={"object": {"type": "talk-poll", "id": "p1"}})
        )
        self.assertFalse(ok)
        self.assertEqual(hooks.inbounds, [])

    def test_type_in_nested_spec_is_also_detected(self):
        adapter, ok, hooks = self._one(
            message(text="", message_parameters={"share": {"type": "file", "id": "f"}})
        )
        self.assertFalse(ok)
        self.assertEqual(hooks.inbounds, [])

    def test_mention_placeholder_text_is_kept_as_is(self):
        """v1 不做提及渲染：``{mention-call1}`` 占位符原样传给上层（docstring 已写明限制）。"""
        adapter, ok, hooks = self._one(
            message(text="{mention-call1} 看一下",
                    message_parameters={"mention-call1": {"type": "mention",
                                                           "id": "u_alice"}})
        )
        self.assertTrue(ok)
        self.assertEqual(hooks.inbounds[0].text, "{mention-call1} 看一下")

    def test_empty_body_dropped(self):
        for text in ("", "   ", "\n\t "):
            with self.subTest(text=text):
                _, ok, hooks = self._one(message(text=text))
                self.assertFalse(ok)
                self.assertEqual(hooks.inbounds, [])

    def test_whitelisted_room_only(self):
        """``allowed_chat_ids`` 填**会话 token**（不是 user id）。"""
        adapter, hooks = make_adapter({"allowed_chat_ids": [ROOM]})
        self.assertTrue(adapter._handle_message(ROOM, message(1, "授权")))
        self.assertEqual(len(hooks.inbounds), 1)
        self.assertFalse(adapter._handle_message(ROOM2, message(2, "未授权")))
        self.assertEqual(len(hooks.inbounds), 1)

    def test_gate_uses_base_admits(self):
        from opencode_bridge.adapters.base import Adapter as BaseAdapter

        adapter, _ = make_adapter({"allowed_chat_ids": ["r_ok"]})
        self.assertIs(type(adapter).admits, BaseAdapter.admits)

    def test_empty_whitelist_admits_everything_without_config_version(self):
        """无 ``config_version`` ⇒ 沿用旧的「空 = 全开」（本夹具不传该键）。"""
        adapter, hooks = make_adapter()
        self.assertTrue(adapter._handle_message(ROOM2, message()))
        self.assertEqual(len(hooks.inbounds), 1)

    def test_empty_whitelist_rejects_everything_once_config_version_flips(self):
        """``config_version >= 2`` ⇒ 空 = 全拒。

        ⚠️ 顺带钉住 ``pairing_supported = False`` 对本平台的含义：即使有人发了
        ``/pair``，也不会有配对回信（principal 是 OCS token，用户不知道它）。
        """
        adapter, hooks = make_adapter({"config_version": 2})
        self.assertFalse(adapter._handle_message(ROOM2, message()))
        self.assertEqual(len(hooks.inbounds), 0)
        self.assertFalse(adapter.pairing_supported)
        self.assertFalse(
            adapter.answer_pairing_request(ROOM2, "nextcloud:tok", "/pair"),
            "配对本平台已禁用 ⇒ /pair 不得触发配对回信",
        )

    def test_missing_self_uid_stops_inbound_entirely(self):
        """拿不到 uid 就无法防回环 —— 宁可停摆也不能自问自答。"""
        adapter, hooks = make_adapter(user_id="")
        adapter.user_id = ""
        adapter._handle_message(ROOM, message(100, "谁说的"))
        self.assertEqual(hooks.inbounds, [])

    def test_on_inbound_exception_does_not_escape(self):
        adapter, _ = make_adapter(hooks=ExplodingHooks())
        with self.assertLogs("opencode_bridge.adapters.nextcloud", level="ERROR"):
            self.assertFalse(adapter._handle_message(ROOM, message()))
        self.assertFalse(adapter.running)

    def test_shares_attachment_helper(self):
        self.assertFalse(NextcloudAdapter._shares_attachment(None))
        self.assertFalse(NextcloudAdapter._shares_attachment({}))
        self.assertFalse(NextcloudAdapter._shares_attachment({"mention": {"type": "mention"}}))
        self.assertTrue(NextcloudAdapter._shares_attachment({"file": {}}))
        self.assertTrue(NextcloudAdapter._shares_attachment({"object": {}}))
        self.assertTrue(NextcloudAdapter._shares_attachment({"FILE": {}}))


# ----------------------------------------------------------------------
# 9) 自己的 uid
# ----------------------------------------------------------------------
class TestSelfUid(unittest.TestCase):
    def test_uid_from_cloud_user(self):
        adapter, _ = make_adapter(user_id="")
        adapter.user_id = ""
        stub = attach(adapter, Stub(default=_Resp(200, ocs({"id": "BridgeBot",
                                                            "displayname": "bridge"}))))
        adapter._load_self_uid()
        self.assertEqual(adapter.user_id, "BridgeBot")
        self.assertEqual(stub.paths(), [USER_PATH])

    def test_uid_case_is_preserved(self):
        adapter, _ = make_adapter(user_id="")
        adapter.user_id = ""
        attach(adapter, Stub(default=_Resp(200, ocs({"id": "BridgeBot"}))))
        adapter._load_self_uid()
        self.assertEqual(adapter.user_id, "BridgeBot")
        self.assertNotEqual(adapter.user_id, "bridgebot")

    def test_configured_uid_skips_cloud_user(self):
        adapter, _ = make_adapter()
        stub = attach(adapter, Stub())
        adapter._load_self_uid()
        self.assertEqual(stub.calls, [], "配置已给 user_id 时不必再问")

    def test_uid_failure_leaves_inbound_disabled(self):
        adapter, _ = make_adapter(user_id="")
        adapter.user_id = ""
        attach(adapter, Stub(default=_Resp(401, ocs_error("unauthorized",
                                                          status_code=401))))
        with self.assertLogs("opencode_bridge.adapters.nextcloud", level="WARNING"):
            adapter._load_self_uid()
        self.assertEqual(adapter.user_id, "")
        self.assertFalse(adapter._identity_ready)

    def test_start_keeps_running_without_uid(self):
        adapter, hooks = make_adapter(user_id="")
        adapter.user_id = ""
        attach(adapter, Stub(default=_Resp(200, ocs([]))))
        adapter._bootstrap_workers = lambda: None  # 不起 worker
        with self.assertLogs("opencode_bridge.adapters.nextcloud", level="ERROR") as cap:
            adapter.start()
        self.addCleanup(adapter.stop)
        self.assertTrue(adapter.running)
        self.assertIn("uid", "\n".join(cap.output))
        self.assertEqual(hooks.inbounds, [])


# ----------------------------------------------------------------------
# 会话表 + 周期性全量刷新
# ----------------------------------------------------------------------
class TestRoomTable(unittest.TestCase):
    def test_rooms_uses_api_v4(self):
        adapter, _ = make_adapter()
        stub = attach(adapter, Stub(default=_Resp(200, ocs([]))))
        adapter._refresh_rooms(full=True)
        self.assertEqual(stub.paths(), [ROOMS_PATH])
        self.assertIn("api/v4", stub.paths()[0])

    def test_full_refresh_sends_no_modified_since(self):
        adapter, _ = make_adapter()
        stub = attach(adapter, Stub(default=_Resp(200, ocs([]))))
        adapter._refresh_rooms(full=True)
        self.assertNotIn("modifiedSince", stub.calls[0]["params"])

    def test_incremental_refresh_sends_modified_since(self):
        adapter, _ = make_adapter()
        stub = attach(adapter, Stub(default=_Resp(200, ocs([]))))
        adapter._last_modified_since = 1700000000
        adapter._refresh_rooms(full=False)
        self.assertEqual(stub.calls[0]["params"]["modifiedSince"], 1700000000)

    def test_full_refresh_prunes_vanished_rooms(self):
        """官方明确：增量拉取检测不到"会话被删 / 我被移出"，全量刷新才对账。"""
        adapter, _ = make_adapter()
        known_room(adapter, ROOM)
        known_room(adapter, ROOM2)
        attach(adapter, Stub(default=_Resp(200, ocs([room_entry(ROOM)]))))
        with self.assertLogs("opencode_bridge.adapters.nextcloud", level="INFO"):
            adapter._refresh_rooms(full=True)
        self.assertIn(ROOM, adapter.rooms)
        self.assertNotIn(ROOM2, adapter.rooms, "消失的会话必须被踢出轮询表")
        self.assertEqual(adapter._poll_order, [ROOM])

    def test_incremental_refresh_does_not_prune(self):
        adapter, _ = make_adapter()
        known_room(adapter, ROOM)
        known_room(adapter, ROOM2)
        attach(adapter, Stub(default=_Resp(200, ocs([room_entry(ROOM)]))))
        adapter._last_modified_since = 1
        adapter._refresh_rooms(full=False)
        self.assertIn(ROOM2, adapter.rooms)

    def test_room_flags_are_recorded(self):
        adapter, _ = make_adapter()
        attach(adapter, Stub(default=_Resp(200, ocs([
            room_entry(ROOM, read_only=1),
            room_entry(ROOM2, lobby_state=1),
            room_entry("r3", permissions=0),
            room_entry("r4", permissions=PERM_CHAT),
        ]))))
        adapter._refresh_rooms(full=True)
        self.assertTrue(adapter.rooms[ROOM].read_only)
        self.assertFalse(adapter.rooms[ROOM].can_send)
        self.assertTrue(adapter.rooms[ROOM2].lobby)
        self.assertFalse(adapter.rooms[ROOM2].can_send)
        self.assertFalse(adapter.rooms["r3"].can_send, "缺 PERM_CHAT 不能发言")
        self.assertTrue(adapter.rooms["r4"].can_send, "有 PERM_CHAT 且非只读可发言")

    def test_room_list_failure_keeps_existing_table(self):
        adapter, _ = make_adapter()
        known_room(adapter, ROOM)
        attach(adapter, Stub(default=_Resp(500, ocs_error("boom", status_code=500))))
        with self.assertLogs("opencode_bridge.adapters.nextcloud", level="WARNING"):
            adapter._refresh_rooms(full=True)
        self.assertIn(ROOM, adapter.rooms)

    def test_periodic_full_refresh_happens(self):
        """全量刷新按 full_refresh_seconds **周期**发生，不是只做一次。

        用注入的单调时钟把 300 秒"跳"过去，不必真等。
        """
        adapter, _ = make_adapter({"full_refresh_seconds": 30.0})
        adapter._bootstrap_workers = lambda: None
        real_fetch = adapter._fetch_rooms
        seen: list = []

        def spy(modified_since):
            seen.append(modified_since)
            return real_fetch(modified_since)

        adapter._fetch_rooms = spy  # type: ignore[method-assign]
        stub = attach(adapter, Stub(default=_Resp(200, ocs([]))))

        now = {"t": 1000.0}
        adapter._clock = lambda: now["t"]
        old_idle = nc_mod.POLL_IDLE_WAIT
        nc_mod.POLL_IDLE_WAIT = 0.005
        self.addCleanup(setattr, nc_mod, "POLL_IDLE_WAIT", old_idle)

        def runner():
            try:
                adapter._coordinator_loop()
            finally:
                adapter._stop_event.set()

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2.0)

        def wait_for(predicate, timeout: float = 3.0) -> bool:
            deadline = time.time() + timeout
            while time.time() < deadline:
                if predicate():
                    return True
                time.sleep(0.005)
            return predicate()

        self.assertTrue(wait_for(lambda: len(seen) >= 3), "协调线程没在刷会话表")
        self.assertIsNone(seen[0], "第一轮必须是全量（不带 modifiedSince）")
        self.assertTrue(any(item is not None for item in seen[1:]),
                        "第二轮起应该是增量（带 modifiedSince）")

        # 时钟跳 31 秒 → 必然再来一次全量
        now["t"] += 31.0
        self.assertTrue(
            wait_for(lambda: seen.count(None) >= 2),
            f"到周期必须再全量刷新一次，实际 seen={seen}",
        )
        self.assertGreaterEqual(len(stub.calls), 4)
        adapter._stop_event.set()
        thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive(), "stop 事件置位后协调线程必须退出")

    def test_full_refresh_threshold_is_respected(self):
        adapter, _ = make_adapter({"full_refresh_seconds": 300.0})
        self.assertEqual(adapter.full_refresh_seconds, 300.0)
        fast, _ = make_adapter({"full_refresh_seconds": 60})
        self.assertEqual(fast.full_refresh_seconds, 60.0)
        bad, _ = make_adapter({"full_refresh_seconds": 5})  # 低于下限 30
        self.assertEqual(bad.full_refresh_seconds, 300.0)
        non_numeric, _ = make_adapter({"full_refresh_seconds": "soon"})
        self.assertEqual(non_numeric.full_refresh_seconds, 300.0)


# ----------------------------------------------------------------------
# 13) 并发轮询上限
# ----------------------------------------------------------------------
class TestConcurrencyCap(unittest.TestCase):
    def test_worker_count_equals_max_concurrent_polls(self):
        adapter, _ = make_adapter({"max_concurrent_polls": 3})
        attach(adapter, Stub(default=_Resp(304, {}, {})))
        adapter._bootstrap_workers()
        self.addCleanup(adapter._stop_workers)
        self.assertEqual(len(adapter._workers), 3)
        for thread in adapter._workers:
            self.assertTrue(thread.is_alive())

    def test_inflight_requests_never_exceed_the_cap(self):
        adapter, _ = make_adapter({"max_concurrent_polls": 2})
        adapter.poll_timeout = 1
        for token in ("r1", "r2", "r3", "r4", "r5", "r6"):
            room = known_room(adapter, token)

        def slow(*_args, **_kwargs):
            time.sleep(0.05)
            return _Resp(304, {}, {})

        stub = attach(adapter, Stub(default=slow))
        adapter._bootstrap_workers()
        self.addCleanup(adapter._stop_workers)
        deadline = time.time() + 2.0
        while len(stub.calls) < 8 and time.time() < deadline:
            time.sleep(0.01)
        adapter._stop_event.set()
        adapter._stop_workers()
        self.assertGreaterEqual(len(stub.calls), 8, "worker 应该在持续轮询")
        self.assertLessEqual(
            stub.max_inflight, 2,
            f"同时在飞的长轮询 {stub.max_inflight} 个，超过上限 2",
        )

    def test_one_worker_still_polls_every_room_by_rotation(self):
        adapter, _ = make_adapter({"max_concurrent_polls": 1})
        for token in ("r1", "r2", "r3"):
            known_room(adapter, token)
        attach(adapter, Stub(default=_Resp(304, {}, {})))
        self.assertEqual(adapter._next_room(), "r1")
        self.assertEqual(adapter._next_room(), "r2")
        self.assertEqual(adapter._next_room(), "r3")
        self.assertEqual(adapter._next_room(), "r1", "轮转：取出来的放回队尾")

    def test_not_one_thread_per_conversation(self):
        """绝不能"每个会话一个线程" —— 50 个会话也只有 max_concurrent_polls 个 worker。"""
        adapter, _ = make_adapter({"max_concurrent_polls": 2})
        for index in range(50):
            known_room(adapter, f"r{index}")
        attach(adapter, Stub(default=_Resp(304, {}, {})))
        adapter._bootstrap_workers()
        self.addCleanup(adapter._stop_workers)
        self.assertEqual(len(adapter._workers), 2)
        # worker 正在轮转（pop + append），所以读轮询表必须持锁，否则会读到中间的瞬态
        with adapter._poll_lock:
            order = list(adapter._poll_order)
        self.assertEqual(len(order), 50)
        self.assertEqual(set(order), set(adapter.rooms))

    def test_next_room_on_empty_table_returns_none(self):
        adapter, _ = make_adapter()
        self.assertIsNone(adapter._next_room())


# ----------------------------------------------------------------------
# 10) 发消息
# ----------------------------------------------------------------------
class TestSend(unittest.TestCase):
    def test_send_posts_urlencoded_message_field(self):
        adapter, _ = make_adapter()
        stub = attach(adapter, Stub(default=ok_chat(500)))
        handle = adapter.send(Outbound(conversation_id=CID, text="你好"))
        self.assertIsNotNone(handle)
        self.assertEqual(handle.message_id, "500")
        self.assertEqual(handle.platform, "nextcloud")
        call = stub.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["path"], f"{CHAT_PATH}/{ROOM}")
        self.assertEqual(call["form"], {"message": "你好"})
        # body 必须是 urlencoded（服务端显式按 urlencoded 解析）
        encoded = urllib.parse.urlencode(call["form"])
        self.assertTrue(encoded.startswith("message="), encoded)
        self.assertEqual(urllib.parse.parse_qs(encoded), {"message": ["你好"]})

    def test_send_201_is_success(self):
        adapter, _ = make_adapter()
        attach(adapter, Stub(default=_Resp(201, ocs(message(1)))))
        result = adapter.send_result(Outbound(conversation_id=CID, text="hi"))
        self.assertTrue(result.ok)
        self.assertFalse(result.partial)

    def test_send_400_empty_message_is_refused_locally(self):
        adapter, _ = make_adapter()
        stub = attach(adapter, Stub(default=ok_chat()))
        self.assertIsNone(adapter.send(Outbound(conversation_id=CID, text="")))
        self.assertEqual(stub.calls, [], "空消息本地就拦下，不该打 REST")
        self.assertIs(adapter.last_send_error, SendError.BAD_FORMAT)

    def test_send_403_read_only_is_observable(self):
        adapter, _ = make_adapter()
        attach(adapter, Stub(default=_Resp(403, ocs_error("forbidden", status_code=403))))
        result = adapter.send_result(Outbound(conversation_id=CID, text="hi"))
        self.assertFalse(result.ok)
        self.assertIs(result.error_kind, SendError.FORBIDDEN)

    def test_send_413_too_long_maps_to_too_long(self):
        adapter, _ = make_adapter()
        attach(adapter, Stub(default=_Resp(413, ocs_error("too long", status_code=413))))
        result = adapter.send_result(Outbound(conversation_id=CID, text="hi"))
        self.assertIs(result.error_kind, SendError.TOO_LONG)

    def test_send_500_maps_to_transient(self):
        adapter, _ = make_adapter()
        attach(adapter, Stub(default=_Resp(500, ocs_error("boom", status_code=500))))
        result = adapter.send_result(Outbound(conversation_id=CID, text="hi"))
        self.assertIs(result.error_kind, SendError.TRANSIENT)

    def test_ocs_statuscode_can_veto_a_2xx(self):
        adapter, _ = make_adapter()
        attach(adapter, Stub(default=_Resp(
            201, ocs({"error": "no"}, status_code=403, message="Forbidden"))))
        result = adapter.send_result(Outbound(conversation_id=CID, text="hi"))
        self.assertFalse(result.ok)
        self.assertIs(result.error_kind, SendError.FORBIDDEN)

    def test_send_bad_conversation_id(self):
        adapter, _ = make_adapter()
        stub = attach(adapter, Stub(default=ok_chat()))
        self.assertIsNone(adapter.send(Outbound(conversation_id="", text="hi")))
        self.assertIsNone(adapter.send(Outbound(conversation_id="nextcloud:", text="hi")))
        self.assertEqual(stub.calls, [])

    def test_send_refuses_without_credentials(self):
        adapter = NextcloudAdapter({"base_url": BASE_URL, "username": "u"},
                                   RecordingHooks())
        attach(adapter, Stub(default=ok_chat()))
        self.assertIsNone(adapter.send(Outbound(conversation_id=CID, text="hi")))

    def test_send_splits_by_runtime_limit(self):
        adapter, _ = make_adapter()
        adapter._apply_max_chat_length(50)
        stub = attach(adapter, Stub(default=ok_chat()))
        adapter.send(Outbound(conversation_id=CID, text="x" * 120))
        self.assertEqual(len(stub.calls), 3)
        self.assertEqual([c["form"]["message"] for c in stub.calls],
                         ["x" * 50, "x" * 50, "x" * 20])

    def test_partial_send_marks_partial(self):
        adapter, _ = make_adapter()
        adapter._apply_max_chat_length(10)
        attach(adapter, Stub([
            ok_chat(1),
            _Resp(500, ocs_error("boom", status_code=500)),
        ]))
        result = adapter.send_result(Outbound(conversation_id=CID, text="x" * 25))
        self.assertTrue(result.ok)
        self.assertTrue(result.partial)
        self.assertIs(result.error_kind, SendError.TRANSIENT)
        self.assertEqual(result.handle.message_id, "1")

    def test_conversation_id_roundtrip(self):
        self.assertEqual(NextcloudAdapter._conversation_id(ROOM), CID)
        self.assertEqual(NextcloudAdapter._token(CID), ROOM)
        self.assertEqual(NextcloudAdapter._token(ROOM), ROOM)
        self.assertIsNone(NextcloudAdapter._token(""))
        self.assertIsNone(NextcloudAdapter._token(None))

    def test_token_is_quoted_on_send(self):
        adapter, _ = make_adapter()
        stub = attach(adapter, Stub(default=ok_chat()))
        weird = "a/b c?d"
        adapter.send(Outbound(conversation_id=f"nextcloud:{weird}", text="hi"))
        self.assertEqual(stub.calls[0]["path"],
                         f"{CHAT_PATH}/" + urllib.parse.quote(weird, safe=""))


# ----------------------------------------------------------------------
# 11) 编辑
# ----------------------------------------------------------------------
class TestEdit(unittest.TestCase):
    def _handle(self) -> MsgHandle:
        return MsgHandle(conversation_id=CID, message_id="500", platform="nextcloud")

    def test_edit_uses_put_with_message_id(self):
        adapter, _ = make_adapter()
        stub = attach(adapter, Stub(default=_Resp(200, ocs(message(500, "新正文")))))
        self.assertTrue(adapter.edit(self._handle(),
                                    Outbound(conversation_id=CID, text="新正文")))
        call = stub.calls[0]
        self.assertEqual(call["method"], "PUT")
        self.assertEqual(call["path"], f"{CHAT_PATH}/{ROOM}/500")
        self.assertEqual(call["form"], {"message": "新正文"})

    def test_edit_accepts_200_and_202(self):
        for status in (200, 201, 202):
            with self.subTest(status=status):
                adapter, _ = make_adapter()
                attach(adapter, Stub(default=_Resp(status, ocs(message()))))
                self.assertTrue(adapter.edit(self._handle(),
                                            Outbound(conversation_id=CID, text="x")))

    def test_edit_too_old_returns_false_with_meaningful_kind(self):
        """400 ``{"error": "age"}`` = 超过 24 小时不能编辑。

        归类是 ``BAD_FORMAT`` 而**不是** ``TOO_LONG`` —— 是"消息太老"不是"内容太长"，
        后者会让调用方误以为再切短一点就能发。
        """
        adapter, _ = make_adapter()
        attach(adapter, Stub(default=_Resp(400, ocs_error("age"))))
        self.assertFalse(adapter.edit(self._handle(),
                                     Outbound(conversation_id=CID, text="x")))
        self.assertIsNot(adapter.last_send_error, SendError.UNKNOWN)
        self.assertEqual(adapter.last_send_error, SendError.BAD_FORMAT)
        self.assertIn("age", adapter._last_send_error[1])

    def test_age_error_is_not_confused_with_too_long(self):
        adapter, _ = make_adapter()
        attach(adapter, Stub(default=_Resp(400, ocs_error("age"))))
        adapter.edit(self._handle(), Outbound(conversation_id=CID, text="x"))
        self.assertIsNot(adapter.last_send_error, SendError.TOO_LONG)

    def test_edit_403_forbidden(self):
        adapter, _ = make_adapter()
        attach(adapter, Stub(default=_Resp(403, ocs_error("forbidden", status_code=403))))
        self.assertFalse(adapter.edit(self._handle(),
                                     Outbound(conversation_id=CID, text="x")))
        self.assertIs(adapter.last_send_error, SendError.FORBIDDEN)

    def test_edit_413_too_long(self):
        adapter, _ = make_adapter()
        attach(adapter, Stub(default=_Resp(413, ocs_error("too long", status_code=413))))
        self.assertFalse(adapter.edit(self._handle(),
                                     Outbound(conversation_id=CID, text="x")))
        self.assertIs(adapter.last_send_error, SendError.TOO_LONG)

    def test_edit_rejects_bad_handle_and_empty_text(self):
        adapter, _ = make_adapter()
        stub = attach(adapter, Stub(default=_Resp(200, ocs(message()))))
        self.assertFalse(adapter.edit(
            MsgHandle(conversation_id=CID, message_id="", platform="nextcloud"),
            Outbound(conversation_id=CID, text="x")))
        self.assertFalse(adapter.edit(self._handle(),
                                     Outbound(conversation_id=CID, text="")))
        self.assertEqual(stub.calls, [])

    def test_edit_quotes_token_and_message_id(self):
        adapter, _ = make_adapter()
        stub = attach(adapter, Stub(default=_Resp(200, ocs(message()))))
        handle = MsgHandle(conversation_id="nextcloud:a/b", message_id="id 1",
                           platform="nextcloud")
        adapter.edit(handle, Outbound(conversation_id=handle.conversation_id, text="x"))
        self.assertEqual(stub.calls[0]["path"],
                         f"{CHAT_PATH}/a%2Fb/id%201")

    def test_edit_refuses_without_credentials(self):
        adapter = NextcloudAdapter({"base_url": BASE_URL, "username": "u"},
                                   RecordingHooks())
        attach(adapter, Stub(default=_Resp(200, ocs(message()))))
        self.assertFalse(adapter.edit(self._handle(),
                                     Outbound(conversation_id=CID, text="x")))


# ----------------------------------------------------------------------
# 12) 只读会话
# ----------------------------------------------------------------------
class TestReadOnlyRoom(unittest.TestCase):
    def test_read_only_room_is_not_attempted(self):
        """``readOnly==1`` 会拿到 403 —— 发之前就拦下，别把出站搞崩。"""
        adapter, _ = make_adapter()
        room = known_room(adapter, ROOM, can_send=False)
        room.read_only = True
        stub = attach(adapter, Stub(default=ok_chat()))
        self.assertIsNone(adapter.send(Outbound(conversation_id=CID, text="hi")))
        self.assertEqual(stub.calls, [], "只读会话不该发请求")
        self.assertIs(adapter.last_send_error, SendError.FORBIDDEN)

    def test_lobby_room_is_not_attempted(self):
        adapter, _ = make_adapter()
        room = known_room(adapter, ROOM, can_send=False)
        room.lobby = True
        stub = attach(adapter, Stub(default=ok_chat()))
        self.assertIsNone(adapter.send(Outbound(conversation_id=CID, text="hi")))
        self.assertEqual(stub.calls, [])

    def test_room_without_perm_chat_is_not_attempted(self):
        adapter, _ = make_adapter()
        room = known_room(adapter, ROOM, can_send=False)
        room.read_only = False
        room.lobby = False
        stub = attach(adapter, Stub(default=ok_chat()))
        self.assertIsNone(adapter.send(Outbound(conversation_id=CID, text="hi")))
        self.assertEqual(stub.calls, [])

    def test_sendable_room_is_attempted(self):
        adapter, _ = make_adapter()
        known_room(adapter, ROOM, can_send=True)
        stub = attach(adapter, Stub(default=ok_chat()))
        self.assertIsNotNone(adapter.send(Outbound(conversation_id=CID, text="hi")))
        self.assertEqual(len(stub.calls), 1)

    def test_unknown_room_is_allowed_and_server_decides(self):
        """还没被发现的会话（没进过会话表）不预判，交给服务端裁决。"""
        adapter, _ = make_adapter()
        stub = attach(adapter, Stub(default=ok_chat()))
        self.assertIsNotNone(adapter.send(Outbound(conversation_id=CID, text="hi")))
        self.assertEqual(len(stub.calls), 1)


# ----------------------------------------------------------------------
# capabilities 交叉校验消息上限
# ----------------------------------------------------------------------
class TestMaxChatLength(unittest.TestCase):
    def test_capabilities_can_refine_the_limit(self):
        adapter, _ = make_adapter()
        body = ocs({"capabilities": {"spreed": {"config": {"chat": {"max-length": 4000}}}}})
        attach(adapter, Stub(default=_Resp(200, body)))
        self.assertEqual(adapter._fetch_max_chat_length(), 4000)
        self.assertEqual(adapter._apply_max_chat_length(4000), 4000)
        self.assertEqual(adapter.effective_max_length, 4000)

    def test_max_length_accepts_string_form(self):
        adapter, _ = make_adapter()
        body = ocs({"capabilities": {"spreed": {"config": {"chat": {"max-length": "8000"}}}}})
        attach(adapter, Stub(default=_Resp(200, body)))
        self.assertEqual(adapter._fetch_max_chat_length(), 8000)

    def test_falls_back_to_source_constant(self):
        adapter, _ = make_adapter()
        for reply in (
            _Resp(500, ocs_error("boom", status_code=500)),
            _Resp(200, ocs({})),
            _Resp(200, ocs({"capabilities": {}})),
            _Resp(200, ocs({"capabilities": {"spreed": {"config": {}}}})),
            _Resp(0, {"message": "transport error"}),
        ):
            with self.subTest(reply=reply.status):
                adapter._apply_max_chat_length(None)
                attach(adapter, Stub(default=reply))
                self.assertIsNone(adapter._fetch_max_chat_length())
                self.assertEqual(adapter._apply_max_chat_length(None), 32000)
                self.assertEqual(adapter.effective_max_length, 32000)

    def test_start_survives_probe_failure(self):
        adapter, _ = make_adapter()
        def boom(*_a, **_k):
            raise RuntimeError("network down")
        adapter._request = boom  # type: ignore[method-assign]
        adapter._bootstrap_workers = lambda: None
        attach(adapter, Stub(default=_Resp(304, {}, {})))
        adapter.start()  # 不抛
        self.addCleanup(adapter.stop)
        self.assertEqual(adapter.effective_max_length, 32000)


# ----------------------------------------------------------------------
# 15) 停止
# ----------------------------------------------------------------------
class TestStop(unittest.TestCase):
    def _running(self, **config) -> tuple[NextcloudAdapter, Stub]:
        adapter, _ = make_adapter(config)
        stub = attach(adapter, Stub(default=_Resp(304, {}, {})))
        adapter.start()
        self.addCleanup(adapter.stop)

        def wait_for(predicate, timeout: float = 3.0) -> bool:
            deadline = time.time() + timeout
            while time.time() < deadline:
                if predicate():
                    return True
                time.sleep(0.005)
            return predicate()

        self.assertTrue(wait_for(lambda: adapter.running and adapter._workers),
                        "协调线程与 worker 都该起来")
        return adapter, stub

    def test_stop_ends_all_threads_promptly(self):
        adapter, _ = self._running(max_concurrent_polls=3)
        threads = list(adapter._workers)
        started = time.time()
        adapter.stop()
        elapsed = time.time() - started
        self.assertFalse(adapter.running, "stop() 必须让协调线程结束")
        for thread in threads:
            self.assertFalse(thread.is_alive(),
                             f"{thread.name} 还在跑，说明 stop 没收干净")
        self.assertLess(elapsed, 4.0, f"stop 用了 {elapsed:.2f}s")

    def test_stop_prevents_further_polling(self):
        adapter, stub = self._running(max_concurrent_polls=1)
        adapter.stop()
        settled = len(stub.calls)
        time.sleep(0.2)
        self.assertEqual(len(stub.calls), settled, "stop 之后不该再发请求")

    def test_stop_closes_active_responses(self):
        """stop 会尽力关掉在飞的长轮询响应，好让 worker 尽快退出阻塞读。"""
        adapter, _ = make_adapter()

        class FakeResponse:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        response = FakeResponse()
        adapter._track_response(response)
        adapter._close_active_responses()
        self.assertTrue(response.closed)
        self.assertEqual(adapter._active_responses, [])

    def test_close_active_responses_survives_broken_close(self):
        adapter, _ = make_adapter()

        class Broken:
            def close(self):
                raise OSError("already gone")

        adapter._track_response(Broken())
        adapter._close_active_responses()  # 不得抛

    def test_stop_without_start_is_safe(self):
        adapter, _ = make_adapter()
        adapter.stop()
        self.assertFalse(adapter.running)

    def test_worker_loop_survives_exploding_request(self):
        adapter, _ = make_adapter({"max_concurrent_polls": 1})
        known_room(adapter, ROOM)
        calls = {"n": 0}

        def boom(*_a, **_k):
            calls["n"] += 1
            if calls["n"] >= 3:
                adapter._stop_event.set()
            raise RuntimeError("轮询炸了")

        attach(adapter, Stub(default=boom))
        adapter._bootstrap_workers()
        adapter._stop_workers()
        self.assertGreaterEqual(calls["n"], 3, "单次异常不许让 worker 退出")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
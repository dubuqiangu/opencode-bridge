"""nextcloud：**非拒绝路径**上的日志不再打裸 OCS 会话 token（9 处）。

`a6136e9` 把 :meth:`~opencode_bridge.adapters.nextcloud.NextcloudAdapter._drop_inbound`
那一条（拒绝入站）改成了 ``platform:local_id``；``fix-118`` 当时明确列了
「同类不同因、本次刻意没改」的两处，本文件收掉其中一处 —— **轮询与收发路径**。

⛔ **严重度照实说**：这不是「隐私被陌生人刷屏」。那个 token 是**用户自己配置里那几条
会话**的 OCS 会话密钥（进 URL ``/call/<token>``），而本平台
``pairing_supported = False`` 的理由之一正是「用户不该知道它」⇒ 这是
「**凭据不该进日志**」。

⚠️ 断言的是**装上 RedactingFilter 之后 = 用户 grep 到的样子**（生产里 root handler 上
真正发生的那一次脱敏）。

⚠️ 期望值**不调用被测函数算**（见 :func:`tests.log_redaction_support.
expected_conversation_digest` 的说明）。

⚠️ 每处都**顺带断言判定没变**（「只改记什么」的对照）：该丢的仍丢、该放的仍放、
``SendError`` 分类仍一致、日志级别与 traceback 仍原样。
"""

from __future__ import annotations

import logging
import unittest

logging.getLogger("opencode_bridge").addHandler(logging.NullHandler())

from opencode_bridge.adapters.nextcloud import PERM_CHAT, NextcloudAdapter, _Resp, _Room
from opencode_bridge.hooks import Outbound, SendError

from tests.log_redaction_support import (  # 绝对导入：与既有 tests.inbound_log_support 一致
    RecordingHooks,
    captured_logs,
    expected_conversation_digest,
)

BASE_URL = "https://cloud.example.test"
USERNAME = "bridgebot"
PASSWORD = "app-password-0123456789abcdef"
MY_UID = "BridgeBot"

#: 两个形状不同的会话 token。第二个用于"会话消失"那处与"不同会话不撞摘要"。
ROOM = "r4om3t0k3n"
ROOM2 = "s3condr00m"


def ocs(data, *, status_code: int = 200) -> dict:
    return {"ocs": {"meta": {"status": "ok", "statuscode": status_code,
                             "message": "OK"}, "data": data}}


def ocs_failure(status_code: int = 403, error: str = "NoPermission") -> dict:
    return {"ocs": {"meta": {"status": "failure", "statuscode": status_code,
                             "message": "Forbidden"},
                    "data": {"error": error}}}


def room_entry(token: str) -> dict:
    return {"token": token, "id": 7, "name": "群名", "readOnly": 0,
            "lobbyState": 0, "permissions": 384, "type": 2}


def inbound_message(message_id: int = 100, text: str = "你好") -> dict:
    """一条 ``lib/Model/Message::toArray()`` 形状的普通发言。"""
    return {"id": message_id, "token": ROOM, "actorId": "alice",
            "actorType": "users", "actorDisplayName": "Alice",
            "timestamp": 1700000000, "message": text, "messageParameters": {},
            "messageType": "comment", "systemMessage": ""}


def make_adapter(hooks: RecordingHooks | None = None) -> NextcloudAdapter:
    adapter = NextcloudAdapter(
        {"base_url": BASE_URL, "username": USERNAME, "password": PASSWORD,
         "user_id": MY_UID},
        hooks or RecordingHooks(),
    )
    adapter.min_interval = 0
    return adapter


def add_known_room(adapter: NextcloudAdapter, token: str = ROOM, *,
                   can_send: bool = True) -> _Room:
    """塞一个"已知会话"（不发 HTTP），并同步轮询顺序表。"""
    room = _Room(token=token, cursor=100, can_send=can_send, bootstrapped=True)
    adapter.rooms[token] = room
    with adapter._poll_lock:
        if token not in adapter._poll_order:
            adapter._poll_order.append(token)
    return room


def stop_after_one_poll(adapter: NextcloudAdapter, message: str):
    """一个"跑一轮就收工、并且抛异常"的 ``_poll_once`` 替身。"""

    def exploding_poll_once(_token):
        adapter._stop_event.set()
        raise RuntimeError(message)

    return exploding_poll_once


class RedactionAssertions(unittest.TestCase):
    """两条承重断言，逐处复用。"""

    def assert_no_bare_token(self, line: str, token: str) -> None:
        self.assertNotIn(token, line,
                         "这一行仍带**裸** OCS 会话 token：%r" % (line,))

    def assert_digest_present(self, line: str, token: str) -> None:
        self.assertIn(expected_conversation_digest("nextcloud", token), line,
                      "这一行没有可关联摘要：%r" % (line,))


# ======================================================================
# 1) _refresh_rooms —— 会话消失（被删 / 我被移出）
# ======================================================================
class RefreshRoomsEvictionLogTest(RedactionAssertions):
    def drive(self):
        adapter = make_adapter()
        add_known_room(adapter, ROOM)
        add_known_room(adapter, ROOM2, can_send=False)
        adapter._fetch_rooms = lambda _since: [room_entry(ROOM)]  # 只还回 ROOM
        with captured_logs("nextcloud") as handler:
            adapter._refresh_rooms(full=True)
        return adapter, handler

    def test_evicted_room_is_logged_as_digest_not_bare_token(self):
        _adapter, handler = self.drive()
        lines = handler.lines("已消失")
        self.assertEqual(len(lines), 1, "应当恰好一行，实际 %r" % (handler.lines(""),))
        self.assert_no_bare_token(lines[0], ROOM2)
        self.assert_digest_present(lines[0], ROOM2)
        self.assertNotIn(ROOM, lines[0],
                         "只该记被踢掉的那个会话，不该顺带带上还活着的那个")

    def test_eviction_still_deletes_the_room(self):
        adapter, _handler = self.drive()
        self.assertIn(ROOM, adapter.rooms)
        self.assertNotIn(ROOM2, adapter.rooms, "判定没变：消失的会话仍被踢出表")

    def test_incremental_refresh_does_not_log_eviction(self):
        """增量刷新**不**对账 ⇒ 不该有这一行（这行只属于全量刷新）。"""
        adapter = make_adapter()
        add_known_room(adapter, ROOM)
        add_known_room(adapter, ROOM2, can_send=False)
        adapter._fetch_rooms = lambda _since: [room_entry(ROOM)]

        with captured_logs("nextcloud") as handler:
            adapter._refresh_rooms(full=False)

        self.assertEqual(handler.lines("已消失"), [])
        self.assertIn(ROOM2, adapter.rooms, "增量刷新不该踢人")


# ======================================================================
# 2) / 3) _bootstrap_cursor —— 游标 bootstrap 失败 / 响应头缺失
# ======================================================================
class BootstrapCursorLogTest(RedactionAssertions):
    def test_http_failure_is_logged_as_digest_not_bare_token(self):
        adapter = make_adapter()
        adapter._request = lambda *a, **kw: _Resp(status=500, data={})

        with captured_logs("nextcloud") as handler:
            cursor = adapter._bootstrap_cursor(ROOM)

        lines = handler.lines("bootstrap 失败")
        self.assertEqual(len(lines), 1, "应当恰好一行，实际 %r" % (handler.lines(""),))
        self.assert_no_bare_token(lines[0], ROOM)
        self.assert_digest_present(lines[0], ROOM)
        self.assertIn("HTTP 500", lines[0], "resp.status 实参必须原样保留")
        self.assertEqual(cursor, 0, "判定没变：失败时游标保持 0")

    def test_missing_cursor_header_is_logged_as_digest_not_bare_token(self):
        adapter = make_adapter()
        adapter._request = lambda *a, **kw: _Resp(status=200, data={}, headers={})

        with captured_logs("nextcloud") as handler:
            cursor = adapter._bootstrap_cursor(ROOM)

        lines = handler.lines("X-Chat-Last-Given")
        self.assertEqual(len(lines), 1, "应当恰好一行，实际 %r" % (handler.lines(""),))
        self.assert_no_bare_token(lines[0], ROOM)
        self.assert_digest_present(lines[0], ROOM)
        self.assertEqual(cursor, 0, "判定没变：头缺失时游标保持 0")

    def test_valid_cursor_header_logs_nothing(self):
        """头合法 ⇒ 不该有那一行（否则就成了噪音）。"""
        adapter = make_adapter()
        adapter._request = lambda *a, **kw: _Resp(
            status=200, data={}, headers={"x-chat-last-given": "4242"})

        with captured_logs("nextcloud") as handler:
            cursor = adapter._bootstrap_cursor(ROOM)

        self.assertEqual(handler.lines(""), [])
        self.assertEqual(cursor, 4242)


# ======================================================================
# 4) _worker_loop —— 轮询抛异常
# ======================================================================
class WorkerLoopLogTest(RedactionAssertions):
    def drive(self):
        adapter = make_adapter()
        adapter._next_room = lambda: ROOM
        adapter._poll_once = stop_after_one_poll(adapter, "传输层炸了")
        with captured_logs("nextcloud") as handler:
            adapter._worker_loop()
        return handler

    def test_poll_failure_is_logged_as_digest_not_bare_token(self):
        handler = self.drive()
        lines = handler.lines("轮询会话")
        self.assertEqual(len(lines), 1, "应当恰好一行，实际 %r" % (handler.lines(""),))
        self.assert_no_bare_token(lines[0], ROOM)
        self.assert_digest_present(lines[0], ROOM)

    def test_level_and_traceback_are_preserved(self):
        """⛔ 级别与 traceback 一个字都不许动 —— traceback 是排障要的那一半。"""
        handler = self.drive()
        records = [r for r in handler.records if "轮询会话" in r.message]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].levelname, "ERROR",
                         "必须是 logger.exception（ERROR+exc_info），级别不许降")
        self.assertIsNotNone(records[0].exc_info)
        self.assertIn("传输层炸了", records[0].traceback)

    def test_worker_loop_exception_does_not_escape(self):
        """判定没变：worker 里的任何异常都不许逃出线程。"""
        self.drive()          # 不抛就是通过


# ======================================================================
# 5) / 6) / 7) _poll_once —— HTTP 失败 / ocs 状态码 / 响应头缺失
# ======================================================================
class PollOnceFailureLogTest(RedactionAssertions):
    def test_http_failure_is_logged_as_digest_not_bare_token(self):
        adapter = make_adapter()
        add_known_room(adapter, ROOM)
        adapter._request = lambda *a, **kw: _Resp(status=503, data={})

        with captured_logs("nextcloud") as handler:
            adapter._poll_once(ROOM)

        lines = handler.lines("长轮询")
        self.assertEqual(len(lines), 1, "应当恰好一行，实际 %r" % (handler.lines(""),))
        self.assert_no_bare_token(lines[0], ROOM)
        self.assert_digest_present(lines[0], ROOM)
        self.assertIn("HTTP 503", lines[0], "resp.status 实参必须原样保留")

    def test_ocs_failure_code_is_logged_as_digest_not_bare_token(self):
        adapter = make_adapter()
        add_known_room(adapter, ROOM)
        adapter._request = lambda *a, **kw: _Resp(
            status=200, data=ocs_failure(403))

        with captured_logs("nextcloud") as handler:
            adapter._poll_once(ROOM)

        lines = handler.lines("ocs 状态码")
        self.assertEqual(len(lines), 1, "应当恰好一行，实际 %r" % (handler.lines(""),))
        self.assert_no_bare_token(lines[0], ROOM)
        self.assert_digest_present(lines[0], ROOM)
        self.assertIn("403", lines[0], "code 实参必须原样保留")

    def test_missing_cursor_header_is_logged_as_digest_not_bare_token(self):
        """200 + 有消息但缺 x-chat-last-given ⇒ 游标保持原值那一行。"""
        adapter = make_adapter()
        add_known_room(adapter, ROOM)
        adapter._request = lambda *a, **kw: _Resp(
            status=200, data=ocs([inbound_message()]), headers={})

        with captured_logs("nextcloud") as handler:
            adapter._poll_once(ROOM)

        lines = handler.lines("缺 X-Chat-Last-Given")
        self.assertEqual(len(lines), 1, "应当恰好一行，实际 %r" % (handler.lines(""),))
        self.assert_no_bare_token(lines[0], ROOM)
        self.assert_digest_present(lines[0], ROOM)
        self.assertIn("游标保持 100", lines[0],
                      "room.cursor 实参必须原样保留（刻意**不**脱敏它）")

    def test_message_is_still_delivered_despite_the_cursor_warning(self):
        """判定没变：记了那一行之后消息**照常**投递（游标不动而已）。"""
        hooks = RecordingHooks()
        adapter = make_adapter(hooks)
        add_known_room(adapter, ROOM)
        adapter._request = lambda *a, **kw: _Resp(
            status=200, data=ocs([inbound_message()]), headers={})

        with captured_logs("nextcloud"):
            adapter._poll_once(ROOM)

        self.assertEqual([m.text for m in hooks.inbounds], ["你好"])
        self.assertEqual(hooks.inbounds[0].conversation_id, "nextcloud:%s" % ROOM)


# ======================================================================
# 8) _poll_once —— 单条消息处理抛异常
# ======================================================================
class HandleMessageFailureLogTest(RedactionAssertions):
    def drive(self):
        adapter = make_adapter()
        add_known_room(adapter, ROOM)
        adapter._request = lambda *a, **kw: _Resp(
            status=200, data=ocs([inbound_message()]),
            headers={"x-chat-last-given": "200"})

        def explode_on_message(_token, _message):
            raise RuntimeError("组装 Inbound 炸了")

        adapter._handle_message = explode_on_message
        with captured_logs("nextcloud") as handler:
            adapter._poll_once(ROOM)
        return handler

    def test_message_failure_is_logged_as_digest_not_bare_token(self):
        handler = self.drive()
        lines = handler.lines("处理会话")
        self.assertEqual(len(lines), 1, "应当恰好一行，实际 %r" % (handler.lines(""),))
        self.assert_no_bare_token(lines[0], ROOM)
        self.assert_digest_present(lines[0], ROOM)

    def test_level_and_traceback_are_preserved(self):
        handler = self.drive()
        records = [r for r in handler.records if "处理会话" in r.message]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].levelname, "ERROR", "级别不许降")
        self.assertIn("组装 Inbound 炸了", records[0].traceback)

    def test_one_bad_message_does_not_stop_the_round(self):
        """判定没变：单条异常不许拖垮整轮 —— 后一条仍被处理。"""
        hooks = RecordingHooks()
        adapter = make_adapter(hooks)
        add_known_room(adapter, ROOM)
        adapter._request = lambda *a, **kw: _Resp(
            status=200,
            data=ocs([inbound_message(100, "第一条"), inbound_message(101, "第二条")]),
            headers={"x-chat-last-given": "300"})
        seen: list[int] = []

        def flaky_handle_message(_token, message):
            from opencode_bridge.hooks import Inbound
            seen.append(message["id"])
            if message["id"] == 100:
                raise RuntimeError("第一条炸了")
            hooks.on_inbound(Inbound(conversation_id="nextcloud:%s" % ROOM,
                                     text=message["message"], platform="nextcloud"))

        adapter._handle_message = flaky_handle_message
        with captured_logs("nextcloud"):
            adapter._poll_once(ROOM)

        self.assertEqual(seen, [100, 101])
        self.assertEqual([m.text for m in hooks.inbounds], ["第二条"])


# ======================================================================
# 9) send —— 只读 / 无发言权限，不发
# ======================================================================
class ReadOnlySendLogTest(RedactionAssertions):
    def drive(self):
        adapter = make_adapter()
        add_known_room(adapter, ROOM, can_send=False)
        sent: list[str] = []
        adapter._request = lambda method, path, **kw: (
            sent.append(path) or _Resp(status=201, data=ocs({"id": 1})))
        outbound = Outbound(conversation_id="nextcloud:%s" % ROOM, text="在吗")
        with captured_logs("nextcloud") as handler:
            handle = adapter.send(outbound)
        return adapter, handler, handle, sent, outbound

    def test_read_only_room_is_logged_as_digest_not_bare_token(self):
        _adapter, handler, _handle, _sent, _out = self.drive()
        lines = handler.lines("只读")
        self.assertEqual(len(lines), 1, "应当恰好一行，实际 %r" % (handler.lines(""),))
        self.assert_no_bare_token(lines[0], ROOM)
        self.assert_digest_present(lines[0], ROOM)

    def test_nothing_is_sent_and_handle_is_none(self):
        """判定没变：只读会话压根不发。"""
        _adapter, _handler, handle, sent, _out = self.drive()
        self.assertIsNone(handle)
        self.assertEqual(sent, [], "一个请求都不该打出去")

    def test_read_only_room_still_reports_forbidden(self):
        """判定没变：SendError 分类仍是 FORBIDDEN（下游据此可见）。"""
        adapter, _handler, _handle, _sent, outbound = self.drive()
        self.assertEqual(adapter.send_result(outbound).error_kind, SendError.FORBIDDEN)

    def test_perm_chat_grants_send_so_no_refusal_line(self):
        """对照：权限含 PERM_CHAT 且非只读 ⇒ 不该有那一行，且真的发了。"""
        adapter = make_adapter()
        room = add_known_room(adapter, ROOM)
        room.can_send = bool(384 & PERM_CHAT) and not room.read_only and not room.lobby
        sent: list[str] = []
        adapter._request = lambda method, path, **kw: (
            sent.append(path) or _Resp(status=201, data=ocs({"id": 9})))

        with captured_logs("nextcloud") as handler:
            handle = adapter.send(Outbound(conversation_id="nextcloud:%s" % ROOM,
                                           text="在吗"))

        self.assertEqual(handler.lines("只读"), [])
        self.assertIsNotNone(handle)
        self.assertEqual(len(sent), 1)


# ======================================================================
# 可关联性 —— 这是脱敏方案的全部理由，丢了它就等于拿隐私换可运维性
# ======================================================================
class CorrelationTest(RedactionAssertions):
    def test_same_room_gets_the_same_digest_on_every_line(self):
        """同一个会话在多行日志里指向**同一个** conv#（哪怕文案不同）。"""
        adapter = make_adapter()
        adapter._request = lambda *a, **kw: _Resp(status=500, data={})

        with captured_logs("nextcloud") as handler:
            adapter._bootstrap_cursor(ROOM)          # "bootstrap 失败"
            adapter._bootstrap_cursor(ROOM)          # 同一会话、同一文案

        worker = make_adapter()
        worker._next_room = lambda: ROOM
        worker._poll_once = stop_after_one_poll(worker, "boom")
        with captured_logs("nextcloud") as worker_handler:
            worker._worker_loop()                    # 同一会话、不同文案

        # 3 行、2 种文案（两次 bootstrap 同一句，worker 那次另一句）。
        # 关键不是行数而是：**同一个 conv# 摘要出现在每一行里**。
        lines = handler.lines("") + worker_handler.lines("")
        self.assertEqual(len(lines), 3)
        digest = expected_conversation_digest("nextcloud", ROOM)
        for line in lines:
            self.assertIn(digest, line)
        self.assertEqual(len({line for line in lines}), 2,
                         "应当是 2 种不同文案，而不是被压成同一句")

    def test_different_rooms_get_different_digests(self):
        """反向：两个会话不能撞成同一个 conv#（否则关联性就成了混淆）。"""
        adapter = make_adapter()
        adapter._request = lambda *a, **kw: _Resp(status=500, data={})

        with captured_logs("nextcloud") as handler:
            adapter._bootstrap_cursor(ROOM)
            adapter._bootstrap_cursor(ROOM2)

        lines = handler.lines("bootstrap 失败")
        self.assertEqual(len(lines), 2)
        self.assertNotEqual(lines[0], lines[1])
        self.assertNotEqual(expected_conversation_digest("nextcloud", ROOM),
                            expected_conversation_digest("nextcloud", ROOM2))

    def test_logged_digest_is_not_the_raw_value(self):
        """防恒真：摘要必须**不等于**原值（否则说明压根没脱敏）。"""
        adapter = make_adapter()
        adapter._request = lambda *a, **kw: _Resp(status=500, data={})

        with captured_logs("nextcloud") as handler:
            adapter._bootstrap_cursor(ROOM)

        digest = expected_conversation_digest("nextcloud", ROOM)
        self.assertNotEqual(digest, "nextcloud:%s" % ROOM)
        self.assertIn("nextcloud:conv#", digest)

    def test_digest_matches_the_real_conversation_id_shape(self):
        """日志里那一段与真正进 Inbound 的 conversation_id 是同一个字符串。

        这正是 :func:`redactable_id` 存在的理由；两者对不上，"日志与会话表对不上"
        就成了排障时的假线索。
        """
        self.assertEqual(NextcloudAdapter._conversation_id(ROOM),
                         "nextcloud:%s" % ROOM)
        self.assertTrue(expected_conversation_digest("nextcloud", ROOM).startswith(
            "nextcloud:conv#"))


if __name__ == "__main__":
    unittest.main()

"""SMS（Twilio）适配器本体的行为测试（A3 第二阶段）。

被测对象与判据（与既有测试的分界）：

* **入站**：``X-Twilio-Signature`` 是唯一信任边界 —— 验签算法、端口双变体、
  回声防护、畸形载荷的 TwiML 形状（4xx 也回 TwiML，Twilio 才不会反复重投）。
  拒绝路径上**日志那行怎么脱敏**归 :mod:`tests.test_inbound_log_per_platform`
  （它的 job 是「落盘的那一行」），这里的 job 是「消息进没进来、应答长什么样」。
* **路由契约**：只交 routes 不持 server（hub 契约），入站凭据不齐 ⇒ 零路由
  （fail-closed，绝不挂一个无法验签的路径）。
* **出站**：REST 形状（端点 / Basic 认证 / form 字段）、分片、部分成功语义
  （前段已上线、后段失败 ⇒ 保留已有 handle，``send_observed`` 判 ``partial``）。
* **生命周期**：无线程 —— ``running`` 是 ``_started`` 的显式覆写，``--status``
  那格读的正是 :meth:`~opencode_bridge.adapters.base.Adapter.capabilities`
  里的 ``running`` 字段。

签名算法在本文件**独立重写一份**（Twilio 官方文档口径），断言不许调被测函数
凑期望值 —— 生产实现错了，测试才会红（§7.1：恒真的断言比没有更危险）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import unittest
import urllib.error
import urllib.parse
from unittest import mock

from opencode_bridge.adapters.sms import (
    MAX_SMS_LENGTH,
    TWILIO_WEBHOOK_MAX_BODY_BYTES,
    TWILIO_WEBHOOK_PATH,
    SmsAdapter,
    _port_variant_url,
)
from opencode_bridge.hooks import Outbound, SendError
from opencode_bridge.httpsrv import HttpRequest
from tests.inbound_log_support import RecordingHooks

ACCOUNT_SID = "ACtest"
AUTH_TOKEN = "test-auth-token"
#: 本机（bot 自己）的号码 —— 出站 From / 回声判定的那一侧。
FROM_NUMBER = "+15550000001"
#: 读者的号码 —— 入站 From / 白名单里的那一侧。
READER_NUMBER = "+15559999999"
#: 回环 webhook（可达性拍板 2026-10-10：暂不放宽，本机自测验签链路先行）。
WEBHOOK_URL = "http://127.0.0.1:8080/webhooks/twilio"
#: 端口双变体的两侧（默认端口带↔剥，非默认端口不变体）。
PORTLESS_WEBHOOK_URL = "http://127.0.0.1/webhooks/twilio"
PORT_80_WEBHOOK_URL = "http://127.0.0.1:80/webhooks/twilio"

EMPTY_TWIML = b'<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


def twilio_signature(auth_token: str, signed_url: str, params: dict) -> str:
    """Twilio 官方验签算法（独立于生产实现）：HMAC-SHA1(auth_token,
    URL + 按键排序的键值拼接) → base64。"""
    data_to_sign = signed_url + "".join(
        key + params[key] for key in sorted(params)
    )
    digest = hmac.new(
        auth_token.encode("utf-8"), data_to_sign.encode("utf-8"), hashlib.sha1,
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def twilio_request(
    params: dict | None, signature: str | None, body: bytes | None = None,
) -> HttpRequest:
    """一条 Twilio 入站请求。``body=None`` 时按 form-encoded 编码 ``params``；
    ``signature=None`` 表示不发签名头（缺头是独立分支）。"""
    if body is None:
        body = urllib.parse.urlencode(params or {}).encode("utf-8")
    # HttpRequest 的契约是「键一律小写」（HttpServer 归一后交给 handler）。
    headers = {} if signature is None else {"x-twilio-signature": signature}
    return HttpRequest(
        method="POST", path=TWILIO_WEBHOOK_PATH, query={}, headers=headers,
        body=body, client_host="203.0.113.9",
    )


def signed_twilio_request(
    auth_token: str, signed_url: str, params: dict,
) -> HttpRequest:
    """按 ``signed_url`` 签好名的请求（生产会先按配置 URL 验、再试变体）。"""
    return twilio_request(params, twilio_signature(auth_token, signed_url, params))


def sms_config(**overrides) -> dict:
    """完整可收可发的配置；读者号码在白名单里（入站用例默认「能进来」）。"""
    config = {
        "account_sid": ACCOUNT_SID,
        "auth_token": AUTH_TOKEN,
        "from_number": FROM_NUMBER,
        "webhook_url": WEBHOOK_URL,
        "allowed_chat_ids": [READER_NUMBER],
    }
    config.update(overrides)
    return config


def reader_message_params(body: str = "hello there") -> dict:
    return {"From": READER_NUMBER, "Body": body, "MessageSid": "SM123"}


class ExplodingHooks:
    """``on_inbound`` 抛异常 —— 测 handler 的异常遏制（绝不向 Twilio 泄栈）。"""

    def on_inbound(self, inbound) -> None:
        raise ValueError("hooks exploded on purpose")


def assert_twilml_response(testcase: unittest.TestCase, status: int, response) -> None:
    """所有分支（含 4xx）都必须回 TwiML —— 回别的形状 Twilio 会反复重投。"""
    testcase.assertEqual(response.status, status)
    testcase.assertEqual(response.content_type, "application/xml")
    testcase.assertEqual(response.body, EMPTY_TWIML)


# ======================================================================
# 1. 验签：唯一信任边界
# ======================================================================
class SignatureIsTheOnlyTrustBoundaryTests(unittest.TestCase):
    maxDiff = None

    def test_valid_signature_delivers_exactly_one_inbound(self) -> None:
        """白名单读者 + 合法签名 ⇒ 恰好一条 Inbound，字段逐个对得上。"""
        hooks = RecordingHooks()
        adapter = SmsAdapter(sms_config(), hooks)
        response = adapter._on_twilio_webhook(
            signed_twilio_request(AUTH_TOKEN, WEBHOOK_URL, reader_message_params())
        )
        assert_twilml_response(self, 200, response)
        self.assertEqual(len(hooks.inbounds), 1)
        inbound = hooks.inbounds[0]
        self.assertEqual(inbound.conversation_id, "sms:" + READER_NUMBER)
        self.assertEqual(inbound.text, "hello there")
        self.assertEqual(inbound.kind, "text")
        self.assertEqual(inbound.user_id, READER_NUMBER)
        self.assertEqual(inbound.message_id, "SM123")
        self.assertEqual(inbound.platform, "sms")
        self.assertEqual(inbound.raw, reader_message_params())

    def test_signature_with_wrong_token_is_rejected_403(self) -> None:
        """换一个 token 签 ⇒ 403，零投递。"""
        hooks = RecordingHooks()
        adapter = SmsAdapter(sms_config(), hooks)
        response = adapter._on_twilio_webhook(
            signed_twilio_request("wrong-token", WEBHOOK_URL, reader_message_params())
        )
        assert_twilml_response(self, 403, response)
        self.assertEqual(hooks.inbounds, [])

    def test_missing_signature_header_is_rejected_403(self) -> None:
        """连头都没有 ⇒ 403（fail-closed，不是静默丢）。"""
        hooks = RecordingHooks()
        adapter = SmsAdapter(sms_config(), hooks)
        response = adapter._on_twilio_webhook(twilio_request(reader_message_params(), None))
        assert_twilml_response(self, 403, response)
        self.assertEqual(hooks.inbounds, [])

    def test_signature_over_the_port_variant_is_also_accepted(self) -> None:
        """Twilio 可能按带/不带默认端口的 URL 计算 —— 两种都要能过（双向）。"""
        for configured, signed_over in (
            (PORTLESS_WEBHOOK_URL, PORT_80_WEBHOOK_URL),
            (PORT_80_WEBHOOK_URL, PORTLESS_WEBHOOK_URL),
        ):
            with self.subTest(configured=configured, signed_over=signed_over):
                hooks = RecordingHooks()
                adapter = SmsAdapter(
                    sms_config(webhook_url=configured), hooks,
                )
                response = adapter._on_twilio_webhook(
                    signed_twilio_request(AUTH_TOKEN, signed_over, reader_message_params())
                )
                self.assertEqual(response.status, 200)
                self.assertEqual(len(hooks.inbounds), 1)

    def test_port_variant_url_rules(self) -> None:
        """变体规则本身：默认端口带↔剥，非默认端口/未知 scheme 不变体。"""
        self.assertEqual(
            _port_variant_url("http://127.0.0.1/webhooks/twilio"),
            "http://127.0.0.1:80/webhooks/twilio",
        )
        self.assertEqual(
            _port_variant_url("https://bridge.example.org/webhooks/twilio"),
            "https://bridge.example.org:443/webhooks/twilio",
        )
        self.assertEqual(
            _port_variant_url("http://127.0.0.1:80/webhooks/twilio"),
            "http://127.0.0.1/webhooks/twilio",
        )
        self.assertEqual(
            _port_variant_url("https://bridge.example.org:443/webhooks/twilio"),
            "https://bridge.example.org/webhooks/twilio",
        )
        # 非默认端口返回 None —— 生产的「变体」分支据此跳过第二次尝试。
        self.assertIsNone(
            _port_variant_url("http://127.0.0.1:8080/webhooks/twilio")
        )
        self.assertIsNone(_port_variant_url("ftp://bridge.example.org/hook"))


# ======================================================================
# 2. handler 形状：大小 / 解析 / 必需字段 / 回声 / 闸门 / 异常遏制
# ======================================================================
class HandlerShapeTests(unittest.TestCase):

    def test_oversized_body_is_rejected_413(self) -> None:
        """超过 64 KiB ⇒ 413（签名都还不用验）。"""
        hooks = RecordingHooks()
        adapter = SmsAdapter(sms_config(), hooks)
        response = adapter._on_twilio_webhook(
            twilio_request(None, None, body=b"x" * (TWILIO_WEBHOOK_MAX_BODY_BYTES + 1))
        )
        assert_twilml_response(self, 413, response)
        self.assertEqual(hooks.inbounds, [])

    def test_undecodable_body_is_rejected_400(self) -> None:
        """非 UTF-8 字节 ⇒ 400，零投递。"""
        hooks = RecordingHooks()
        adapter = SmsAdapter(sms_config(), hooks)
        response = adapter._on_twilio_webhook(
            twilio_request(None, None, body=b"\xff\xfe not utf-8")
        )
        assert_twilml_response(self, 400, response)
        self.assertEqual(hooks.inbounds, [])

    def test_missing_from_field_is_silently_200(self) -> None:
        """缺 From ⇒ 静默 200（回 4xx 会让 Twilio 反复重投畸形载荷）。"""
        hooks = RecordingHooks()
        adapter = SmsAdapter(sms_config(), hooks)
        response = adapter._on_twilio_webhook(
            signed_twilio_request(AUTH_TOKEN, WEBHOOK_URL, {"Body": "hello"})
        )
        assert_twilml_response(self, 200, response)
        self.assertEqual(hooks.inbounds, [])

    def test_missing_body_field_is_silently_200(self) -> None:
        """缺 Body 同上 —— 畸形载荷重投也不会变好。"""
        hooks = RecordingHooks()
        adapter = SmsAdapter(sms_config(), hooks)
        response = adapter._on_twilio_webhook(
            signed_twilio_request(
                AUTH_TOKEN, WEBHOOK_URL, {"From": READER_NUMBER},
            )
        )
        assert_twilml_response(self, 200, response)
        self.assertEqual(hooks.inbounds, [])

    def test_own_number_echo_is_dropped(self) -> None:
        """运营商回环的自己短信 ⇒ 丢弃（在闸门之前，永不触发 agent）。"""
        hooks = RecordingHooks()
        # 白名单刻意包含自己的号码（config_version=2）：这样**只有**回声防护能
        # 把这条挡下。若靠空名单让闸门兜底，本用例对「回声防护被删」恒绿
        # （反向证明 M3 验过这一条）。
        adapter = SmsAdapter(
            sms_config(allowed_chat_ids=[FROM_NUMBER], config_version=2), hooks,
        )
        echo_params = {
            "From": FROM_NUMBER, "Body": "echo of my own send", "MessageSid": "SM1",
        }
        response = adapter._on_twilio_webhook(
            signed_twilio_request(AUTH_TOKEN, WEBHOOK_URL, echo_params)
        )
        assert_twilml_response(self, 200, response)
        self.assertEqual(hooks.inbounds, [])

    def test_stranger_is_dropped_by_the_gate_before_inbound(self) -> None:
        """空清单 + ``config_version>=2`` ⇒ 谁都不放行；闸门在 Inbound 之前。"""
        hooks = RecordingHooks()
        adapter = SmsAdapter(
            sms_config(allowed_chat_ids=[], config_version=2), hooks,
        )
        response = adapter._on_twilio_webhook(
            signed_twilio_request(AUTH_TOKEN, WEBHOOK_URL, reader_message_params())
        )
        assert_twilml_response(self, 200, response)
        self.assertEqual(hooks.inbounds, [])

    def test_hooks_exception_is_contained_to_a_200(self) -> None:
        """``on_inbound`` 抛异常 ⇒ handler 吃掉它、照常回 200（不向 Twilio 泄栈）。"""
        adapter = SmsAdapter(sms_config(), ExplodingHooks())
        response = adapter._on_twilio_webhook(
            signed_twilio_request(AUTH_TOKEN, WEBHOOK_URL, reader_message_params())
        )
        assert_twilml_response(self, 200, response)


# ======================================================================
# 3. 路由契约：只交 routes，入站凭据不齐 ⇒ 零路由（fail-closed）
# ======================================================================
class WebhookRoutesContractTests(unittest.TestCase):

    def test_ready_inbound_contributes_exactly_one_unsigned_route(self) -> None:
        adapter = SmsAdapter(sms_config(), RecordingHooks())
        routes = adapter.webhook_routes()
        self.assertEqual(len(routes), 1)
        route = routes[0]
        self.assertEqual(route.path, TWILIO_WEBHOOK_PATH)
        self.assertEqual(route.methods, ("POST",))
        # 契约 2：验签在本 handler 里做，hub 层不设统一鉴权。
        self.assertFalse(route.require_auth)
        self.assertEqual(route.name, "sms-webhook")
        self.assertEqual(route.handler, adapter._on_twilio_webhook)

    def test_inbound_not_ready_contributes_no_route(self) -> None:
        """缺 token（验不了签）或缺 webhook_url（不知签的是哪个 URL）⇒ 不挂路径。"""
        for missing_key in ("auth_token", "webhook_url"):
            with self.subTest(missing=missing_key):
                adapter = SmsAdapter(
                    sms_config(**{missing_key: ""}), RecordingHooks(),
                )
                self.assertEqual(adapter.webhook_routes(), ())


# ======================================================================
# 4. 出站：REST 形状 / 分片 / 部分成功
# ======================================================================
class OutboundRestShapeTests(unittest.TestCase):

    @staticmethod
    def wire_post_message(adapter: SmsAdapter, scripted_responses: list) -> list[bytes]:
        """把 ``_post_message`` 换成记录器：收下 form 体，按剧本回状态。"""
        posted_form_bodies: list[bytes] = []

        def recording_post_message(form_body: bytes):
            posted_form_bodies.append(form_body)
            status, payload = scripted_responses[len(posted_form_bodies) - 1]
            return status, payload

        adapter._post_message = recording_post_message
        return posted_form_bodies

    @staticmethod
    def posted_fields(form_body: bytes) -> dict:
        return {
            key: values[0]
            for key, values in urllib.parse.parse_qs(
                form_body.decode("utf-8"), keep_blank_values=True,
            ).items()
        }

    def test_send_posts_the_twilio_form_and_returns_the_sid_handle(self) -> None:
        """出站 form 的三个字段逐个对；2xx 的 ``sid`` 就是 MsgHandle 的身份。"""
        adapter = SmsAdapter(sms_config(), RecordingHooks())
        posted = self.wire_post_message(adapter, [(201, {"sid": "SM777"})])
        handle = adapter.send(
            Outbound(conversation_id="sms:+15551234567", text="hi there")
        )
        self.assertEqual(len(posted), 1)
        fields = self.posted_fields(posted[0])
        self.assertEqual(fields["From"], FROM_NUMBER)
        self.assertEqual(fields["To"], "+15551234567")
        self.assertEqual(fields["Body"], "hi there")
        self.assertIsNotNone(handle)
        self.assertEqual(handle.message_id, "SM777")
        self.assertEqual(handle.conversation_id, "sms:+15551234567")

    def test_over_limit_text_is_split_into_prefix_free_chunks(self) -> None:
        """超 1600 ⇒ 分片多帧，无编号前缀、逐字节拼回原文。"""
        adapter = SmsAdapter(sms_config(), RecordingHooks())
        long_text = "x" * MAX_SMS_LENGTH + "TAIL"
        scripted = [(201, {"sid": "SM1"}), (201, {"sid": "SM2"})]
        posted = self.wire_post_message(adapter, scripted)
        handle = adapter.send(
            Outbound(conversation_id="sms:+15551234567", text=long_text)
        )
        self.assertEqual(len(posted), 2)
        joined = "".join(self.posted_fields(body)["Body"] for body in posted)
        self.assertEqual(joined, long_text)
        # 句柄取**最后一次**成功帧的 sid（与 ntfy 同形）。
        self.assertEqual(handle.message_id, "SM2")

    def test_first_chunk_failure_returns_none(self) -> None:
        """第一帧就 400 ⇒ ``None`` + 结构化失败（无 partial 可言）。"""
        adapter = SmsAdapter(sms_config(), RecordingHooks())
        posted = self.wire_post_message(
            adapter, [(400, {"message": "nope"})],
        )
        result, raised = adapter.send_observed(
            Outbound(conversation_id="sms:+15551234567", text="hi")
        )
        self.assertIsNone(raised)
        self.assertFalse(result.ok)
        self.assertFalse(result.partial)
        self.assertIn("nope", result.error_detail)
        self.assertEqual(len(posted), 1)

    def test_partial_success_keeps_the_delivered_handle(self) -> None:
        """前帧已上线、后帧失败 ⇒ 保留已有 handle，``send_observed`` 判 partial。"""
        adapter = SmsAdapter(sms_config(), RecordingHooks())
        long_text = "x" * MAX_SMS_LENGTH + "TAIL"
        self.wire_post_message(
            adapter, [(201, {"sid": "SM1"}), (500, None)],
        )
        result, raised = adapter.send_observed(
            Outbound(conversation_id="sms:+15551234567", text=long_text)
        )
        self.assertIsNone(raised)
        self.assertTrue(result.ok)
        self.assertTrue(result.partial)
        self.assertEqual(result.handle.message_id, "SM1")
        self.assertIsNotNone(result.error_kind)

    def test_incomplete_outbound_credentials_refuse_to_send(self) -> None:
        """出站三键缺 from_number ⇒ 拒发（FORBIDDEN），一个字节都不上线。"""
        adapter = SmsAdapter(
            sms_config(from_number=""), RecordingHooks(),
        )
        posted = self.wire_post_message(adapter, [])
        result, raised = adapter.send_observed(
            Outbound(conversation_id="sms:+15551234567", text="hi")
        )
        self.assertIsNone(raised)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, SendError.FORBIDDEN)
        self.assertEqual(posted, [])

    def test_real_post_message_builds_the_twilio_request_shape(self) -> None:
        """真 ``_post_message``：端点 / Basic 认证 / form content-type / 超时。"""
        adapter = SmsAdapter(sms_config(), RecordingHooks())
        form_body = urllib.parse.urlencode(
            {"From": FROM_NUMBER, "To": "+15551234567", "Body": "hi"},
        ).encode("utf-8")

        class FakeTwilioResponse:
            status = 201

            def read(self) -> bytes:
                return b'{"sid": "SM9"}'

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

        with mock.patch(
            "urllib.request.urlopen", return_value=FakeTwilioResponse(),
        ) as fake_urlopen:
            status, payload = adapter._post_message(form_body)

        self.assertEqual(status, 201)
        self.assertEqual(payload, {"sid": "SM9"})
        (sent_request,) = fake_urlopen.call_args.args
        call_kwargs = fake_urlopen.call_args.kwargs
        self.assertEqual(
            sent_request.full_url,
            "https://api.twilio.com/2010-04-01/Accounts/%s/Messages.json"
            % ACCOUNT_SID,
        )
        self.assertEqual(sent_request.get_method(), "POST")
        expected_auth = base64.b64encode(
            f"{ACCOUNT_SID}:{AUTH_TOKEN}".encode("utf-8")
        ).decode("utf-8")
        self.assertEqual(
            sent_request.headers.get("Authorization"), f"Basic {expected_auth}",
        )
        self.assertEqual(
            sent_request.headers.get("Content-type"),
            "application/x-www-form-urlencoded",
        )
        self.assertEqual(sent_request.data, form_body)
        self.assertIn("timeout", call_kwargs)

    def test_real_post_message_maps_http_400_to_status_not_exception(self) -> None:
        """HTTPError 不抛：状态码与 JSON 错误体都交回调用方分类。"""
        adapter = SmsAdapter(sms_config(), RecordingHooks())
        form_body = b"From=%2B1&To=%2B2&Body=hi"
        http_error = urllib.error.HTTPError(
            "https://api.twilio.com/.Messages.json", 400, "Bad Request",
            None, io.BytesIO(b'{"message": "nope"}'),
        )
        with mock.patch("urllib.request.urlopen", side_effect=http_error):
            status, payload = adapter._post_message(form_body)
        self.assertEqual(status, 400)
        self.assertEqual(payload, {"message": "nope"})

    def test_real_post_message_raises_runtime_error_on_transport_failure(self) -> None:
        """网络故障才抛（RuntimeError）—— 与 ntfy ``_http`` 的分工同形。"""
        adapter = SmsAdapter(sms_config(), RecordingHooks())
        with mock.patch(
            "urllib.request.urlopen", side_effect=OSError("connection refused"),
        ):
            with self.assertRaises(RuntimeError):
                adapter._post_message(b"Body=hi")


# ======================================================================
# 5. 生命周期与 --status 字段
# ======================================================================
class LifecycleAndStatusTests(unittest.TestCase):

    def test_start_with_only_outbound_keys_is_running(self) -> None:
        """只配了 REST 三键（没 webhook_url）也算了在跑 —— 出站在工作。"""
        adapter = SmsAdapter(
            sms_config(webhook_url=""), RecordingHooks(),
        )
        adapter.start()
        self.assertTrue(adapter.running)
        self.assertTrue(adapter.capabilities()["running"])

    def test_start_with_only_inbound_keys_is_running(self) -> None:
        """只配了验签两键（没出站号码）⇒ 入站路由在工作，也算在跑。"""
        adapter = SmsAdapter(
            sms_config(account_sid="", from_number=""), RecordingHooks(),
        )
        adapter.start()
        self.assertTrue(adapter.running)
        self.assertTrue(adapter.capabilities()["running"])

    def test_start_with_nothing_stays_stopped(self) -> None:
        """一个键都没有 ⇒ 不算在跑（不许把「起不来」报成「在跑」）。"""
        adapter = SmsAdapter(
            {"account_sid": "", "auth_token": "", "from_number": "",
             "webhook_url": "", "allowed_chat_ids": []},
            RecordingHooks(),
        )
        adapter.start()
        self.assertFalse(adapter.running)
        self.assertFalse(adapter.capabilities()["running"])

    def test_stop_clears_running(self) -> None:
        adapter = SmsAdapter(sms_config(), RecordingHooks())
        adapter.start()
        adapter.stop()
        self.assertFalse(adapter.running)

    def test_readiness_splits_inbound_from_outbound(self) -> None:
        """入站就绪 = token+webhook_url；出站就绪 = sid+token+from。"""
        both_ready = SmsAdapter(sms_config(), RecordingHooks())
        self.assertTrue(both_ready._inbound_ready())
        self.assertTrue(both_ready._outbound_ready())

        missing_webhook = SmsAdapter(sms_config(webhook_url=""), RecordingHooks())
        self.assertFalse(missing_webhook._inbound_ready())
        self.assertTrue(missing_webhook._outbound_ready())

        missing_from_number = SmsAdapter(sms_config(from_number=""), RecordingHooks())
        self.assertTrue(missing_from_number._inbound_ready())
        self.assertFalse(missing_from_number._outbound_ready())

    def test_capabilities_declare_the_sms_surface(self) -> None:
        adapter = SmsAdapter(sms_config(), RecordingHooks())
        surface = adapter.capabilities()
        self.assertEqual(surface["max_message_length"], MAX_SMS_LENGTH)
        self.assertTrue(surface["supports_inbound"])
        self.assertFalse(surface["supports_inline_buttons"])
        self.assertFalse(surface["supports_media"])

    def test_edit_is_never_supported(self) -> None:
        """SMS 改不了已发出的短信 ⇒ 恒 False（finalize 退化为再发一条）。"""
        from opencode_bridge.hooks import MsgHandle

        adapter = SmsAdapter(sms_config(), RecordingHooks())
        handle = MsgHandle("sms:+15551234567", "SM1", "sms")
        self.assertFalse(
            adapter.edit(handle, Outbound(conversation_id="sms:+15551234567", text="new"))
        )

    def test_answer_is_a_noop(self) -> None:
        """SMS 没有 callback query 概念 —— 调了也不许炸。"""
        adapter = SmsAdapter(sms_config(), RecordingHooks())
        self.assertIsNone(adapter.answer("query-id", "text"))

    def test_pairing_is_explicitly_disabled(self) -> None:
        """(c) 不成立：From 是运营商转写、可被伪造 —— 配对码会发给可伪造的号码。"""
        adapter = SmsAdapter(sms_config(), RecordingHooks())
        self.assertFalse(adapter.pairing_supported)


if __name__ == "__main__":
    unittest.main()

"""A3 基建先行的行为测试：单端口共享 webhook server（拓扑拍板 2026-10-10）。

验收判据（台账 A3 基建项）：两个替身适配器**各交一条路由、各自独立验签**，
同一个端口上两条路径各自收到 POST，且签名互不通用 —— cross-secret 必须
403 fail-closed（验签在各自 handler、hub 层无统一鉴权，纪律对齐 hermes
sms 的 fail-closed，见 ``docs/platform-design-reference.md`` Part 11 结论 3）。

三层结构：

1. hub 三条纪律（零路由 ⇒ 不绑端口 / 冲突 ⇒ 整体拒绝并点名双方 / start 不抛）；
2. 两个 ``bridge`` 配置键的载入路径（``Config._merge_bridge`` 的字符串键例外
   —— 新键的 merge 测试放本文件而不进 ``test_config_coerce``：那边管的是
   通用矫型 helper，这两个键是 A3 自己的配置面）；
3. BridgeCore 接线（复用 ``tests.test_core.make_env`` 的离线替身）。
"""

import hashlib
import hmac
import json
import socket
import tempfile
import unittest
import urllib.error
import urllib.request

from opencode_bridge.adapters import Adapter
from opencode_bridge.config import Config
from opencode_bridge.hooks import MsgHandle, Outbound
from opencode_bridge.httpsrv import (
    DEFAULT_BIND_HOST,
    HttpRequest,
    HttpResponse,
    Route,
)
from opencode_bridge.webhook_hub import WebhookHub

from tests.test_core import make_env


def hmac_sha256_hex(secret: str, body: bytes) -> str:
    """替身的验签算法：HMAC-SHA256（Part 11 对照表里最通用的那族）。"""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


class SignedWebhookStubAdapter(Adapter):
    """交一条路由的替身：独立密钥、handler 内自验（``compare_digest``）。

    契约照抄 :meth:`Adapter.webhook_routes` docstring 的三条：只交 routes
    不持 server、验签在自己 handler、路由名能认主（``<name>-webhook``）。
    """

    def __init__(self, stub_name: str, path: str, secret: str) -> None:
        super().__init__({}, hooks=None)  # type: ignore[arg-type]
        self.name = stub_name
        self.webhook_path = path
        self.webhook_secret = secret
        self.received: list[tuple[str, bytes]] = []

    def _verify_and_record(self, request: HttpRequest) -> HttpResponse:
        provided = request.header("x-signature")
        expected = hmac_sha256_hex(self.webhook_secret, request.body)
        if not hmac.compare_digest(provided, expected):
            # fail-closed：验不过就拒绝，绝不投递（对齐 hermes sms 纪律）。
            return HttpResponse.json({"error": "invalid signature"}, 403)
        self.received.append((request.path, request.body))
        return HttpResponse.json({"ok": True, "by": self.name})

    def webhook_routes(self) -> tuple[Route, ...]:
        return (
            Route(
                self.webhook_path,
                self._verify_and_record,
                methods=("POST",),
                name=f"{self.name}-webhook",
            ),
        )

    # --- Adapter 抽象面的最小离线实现（照抄 tests.test_core.FakeAdapter）---
    def start(self) -> None:
        return None

    def send(self, out: Outbound) -> MsgHandle | None:
        return None

    def edit(self, handle: MsgHandle, out: Outbound) -> bool:
        return False


class RoutelessStubAdapter(Adapter):
    """不覆写 ``webhook_routes`` 的替身 —— 基类默认契约必须恰好是「零路由」。"""

    name = "routeless-stub"

    def __init__(self) -> None:
        super().__init__({}, hooks=None)  # type: ignore[arg-type]

    def start(self) -> None:
        return None

    def send(self, out: Outbound) -> MsgHandle | None:
        return None

    def edit(self, handle: MsgHandle, out: Outbound) -> bool:
        return False


def post_json(port: int, path: str, body: bytes, signature: str) -> tuple[int, dict]:
    """POST 并返回 ``(status, 解析后的 JSON)``；4xx/5xx 也原样返回，不抛。"""
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=body,
        method="POST",
        headers={"X-Signature": signature},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error_response:
        raw_payload = error_response.read()
        try:
            parsed_payload = json.loads(raw_payload.decode("utf-8"))
        except ValueError:
            parsed_payload = {}
        return error_response.code, parsed_payload


# ----------------------------------------------------------------------
# 1. 验收本体：单端口、两条路径、各自验签、互不通用
# ----------------------------------------------------------------------
class SharedPortAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.alpha = SignedWebhookStubAdapter("alpha", "/alpha/hook", "alpha-secret")
        self.beta = SignedWebhookStubAdapter("beta", "/beta/hook", "beta-secret")
        self.hub = WebhookHub()
        self.port = self.hub.start([self.alpha, self.beta])
        self.assertIsNotNone(self.port)
        self.addCleanup(self.hub.stop)

    def test_both_paths_receive_posts_on_one_port(self):
        alpha_body = b'{"ping": 1}'
        status, payload = post_json(
            self.port,
            "/alpha/hook",
            alpha_body,
            hmac_sha256_hex("alpha-secret", alpha_body),
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"ok": True, "by": "alpha"})
        self.assertEqual(self.alpha.received, [("/alpha/hook", alpha_body)])

        beta_body = b'{"pong": 2}'
        status, payload = post_json(
            self.port,
            "/beta/hook",
            beta_body,
            hmac_sha256_hex("beta-secret", beta_body),
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"ok": True, "by": "beta"})
        self.assertEqual(self.beta.received, [("/beta/hook", beta_body)])
        # 两条路由确实挂在**同一个** hub 上（单端口拓扑的本体断言）。
        self.assertEqual(self.hub.route_count, 2)

    def test_cross_secret_is_rejected_fail_closed(self):
        # alpha 的路径拿 beta 的签名 ⇒ 403，且 alpha 一条都没收到。
        wrong_signed_body = b'{"attempt": 1}'
        status, _ = post_json(
            self.port,
            "/alpha/hook",
            wrong_signed_body,
            hmac_sha256_hex("beta-secret", wrong_signed_body),
        )
        self.assertEqual(status, 403)
        self.assertEqual(self.alpha.received, [])

        # 空签名同理 —— fail-closed 不认「没带签名」当特例。
        status, _ = post_json(self.port, "/beta/hook", b"{}", "")
        self.assertEqual(status, 403)
        self.assertEqual(self.beta.received, [])

    def test_unknown_path_is_404(self):
        status, _ = post_json(self.port, "/never-configured", b"{}", "irrelevant")
        self.assertEqual(status, 404)

    def test_stop_releases_the_port(self):
        self.assertTrue(self.hub.bound)
        self.hub.stop()
        self.assertFalse(self.hub.bound)
        # 幂等：第二次 stop 不抛（关停序列里它排在适配器之后，必须可靠）。
        self.hub.stop()
        with self.assertRaises(urllib.error.URLError):
            post_json(self.port, "/alpha/hook", b"{}", "irrelevant")


# ----------------------------------------------------------------------
# 2. hub 三条纪律
# ----------------------------------------------------------------------
class HubDisciplineTests(unittest.TestCase):
    def test_base_adapter_default_contract_is_no_routes(self):
        self.assertEqual(tuple(RoutelessStubAdapter().webhook_routes()), ())

    def test_zero_routes_binds_no_port(self):
        hub = WebhookHub()
        self.assertIsNone(hub.start([RoutelessStubAdapter()]))
        self.assertFalse(hub.bound)

    def test_duplicate_path_refuses_whole_hub_and_names_both_adapters(self):
        first = SignedWebhookStubAdapter("alpha", "/shared/hook", "alpha-secret")
        second = SignedWebhookStubAdapter("beta", "/shared/hook", "beta-secret")
        hub = WebhookHub()
        with self.assertLogs("opencode_bridge.webhook_hub", level="ERROR") as captured:
            self.assertIsNone(hub.start([first, second]))
        self.assertFalse(hub.bound)
        joined = "\n".join(captured.output)
        self.assertIn("拒绝启动", joined)
        self.assertIn("alpha", joined)
        self.assertIn("beta", joined)

    def test_taken_port_returns_none_without_raising(self):
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        self.addCleanup(blocker.close)
        taken_port = blocker.getsockname()[1]
        hub = WebhookHub({"webhook_port": taken_port})
        stub = SignedWebhookStubAdapter("alpha", "/alpha/hook", "alpha-secret")
        self.assertIsNone(hub.start([stub]))
        self.assertFalse(hub.bound)

    def test_start_is_idempotent_while_running(self):
        stub = SignedWebhookStubAdapter("alpha", "/alpha/hook", "alpha-secret")
        hub = WebhookHub()
        self.addCleanup(hub.stop)
        port_first = hub.start([stub])
        port_again = hub.start([stub])
        self.assertEqual(port_first, port_again)
        self.assertTrue(hub.bound)


# ----------------------------------------------------------------------
# 3. hub 侧配置矫型（告警点名键名：键名只有这个调用点知道）
# ----------------------------------------------------------------------
class HubConfigCoercionTests(unittest.TestCase):
    def test_no_config_means_loopback_and_os_assigned_port(self):
        hub = WebhookHub()
        self.assertEqual(hub.host, DEFAULT_BIND_HOST)
        self.assertEqual(hub.port, 0)

    def test_bad_port_value_falls_back_with_warning_naming_the_key(self):
        with self.assertLogs("opencode_bridge.webhook_hub", level="WARNING") as captured:
            hub = WebhookHub({"webhook_port": "not-a-number"})
        self.assertEqual(hub.port, 0)
        self.assertIn("webhook_port", "\n".join(captured.output))

    def test_out_of_range_port_falls_back_with_warning_naming_the_key(self):
        with self.assertLogs("opencode_bridge.webhook_hub", level="WARNING") as captured:
            hub = WebhookHub({"webhook_port": 70000})
        self.assertEqual(hub.port, 0)
        self.assertIn("webhook_port", "\n".join(captured.output))

    def test_negative_port_falls_back_with_warning_naming_the_key(self):
        with self.assertLogs("opencode_bridge.webhook_hub", level="WARNING") as captured:
            hub = WebhookHub({"webhook_port": -1})
        self.assertEqual(hub.port, 0)
        self.assertIn("webhook_port", "\n".join(captured.output))

    def test_valid_port_is_kept(self):
        hub = WebhookHub({"webhook_port": 9950})
        self.assertEqual(hub.port, 9950)

    def test_empty_host_falls_back_with_warning_naming_the_key(self):
        for empty_value in ("", "   "):
            with self.subTest(empty_value=empty_value):
                with self.assertLogs(
                    "opencode_bridge.webhook_hub", level="WARNING"
                ) as captured:
                    hub = WebhookHub({"webhook_host": empty_value})
                self.assertEqual(hub.host, DEFAULT_BIND_HOST)
                self.assertIn("webhook_host", "\n".join(captured.output))

    def test_valid_host_is_kept(self):
        hub = WebhookHub({"webhook_host": "127.0.0.2"})
        self.assertEqual(hub.host, "127.0.0.2")


# ----------------------------------------------------------------------
# 4. Config._merge_bridge 对两个新键的载入路径
# ----------------------------------------------------------------------
class BridgeConfigMergeTests(unittest.TestCase):
    def test_defaults_are_present_after_merge(self):
        merged = Config._merge_bridge({})
        self.assertEqual(merged["webhook_port"], 0)
        self.assertEqual(merged["webhook_host"], DEFAULT_BIND_HOST)

    def test_valid_values_pass_through(self):
        merged = Config._merge_bridge(
            {"webhook_port": 9950, "webhook_host": "127.0.0.2"}
        )
        self.assertEqual(merged["webhook_port"], 9950)
        self.assertEqual(merged["webhook_host"], "127.0.0.2")

    def test_numeric_string_port_is_coerced(self):
        # 数字字符串走既有的数值路径（float → int），与 max_message_chars 同形。
        merged = Config._merge_bridge({"webhook_port": "9950"})
        self.assertEqual(merged["webhook_port"], 9950)

    def test_non_numeric_port_falls_back_to_default(self):
        merged = Config._merge_bridge({"webhook_port": "not-a-number"})
        self.assertEqual(merged["webhook_port"], 0)

    def test_non_string_host_is_rejected_with_warning_naming_the_key(self):
        with self.assertLogs("opencode_bridge.config", level="WARNING") as captured:
            merged = Config._merge_bridge({"webhook_host": 12345})
        self.assertEqual(merged["webhook_host"], DEFAULT_BIND_HOST)
        self.assertIn("webhook_host", "\n".join(captured.output))

    def test_whitespace_only_host_is_rejected_with_warning_naming_the_key(self):
        with self.assertLogs("opencode_bridge.config", level="WARNING") as captured:
            merged = Config._merge_bridge({"webhook_host": "   "})
        self.assertEqual(merged["webhook_host"], DEFAULT_BIND_HOST)
        self.assertIn("webhook_host", "\n".join(captured.output))


# ----------------------------------------------------------------------
# 5. BridgeCore 接线：start 里 hub 先于适配器、stop 里适配器先停
# ----------------------------------------------------------------------
class BridgeCoreWiringTests(unittest.TestCase):
    def test_core_wires_two_webhook_adapters_onto_one_shared_port(self):
        with tempfile.TemporaryDirectory() as td:
            core, _, _, _, _, _ = make_env(td, bridge={"webhook_port": 0})
            alpha = SignedWebhookStubAdapter("alpha", "/alpha/hook", "alpha-secret")
            beta = SignedWebhookStubAdapter("beta", "/beta/hook", "beta-secret")
            core.attach(alpha)
            core.attach(beta)
            core.start()
            self.addCleanup(core.stop)

            self.assertTrue(core.webhook_hub.bound)
            self.assertEqual(core.webhook_hub.route_count, 2)
            port = core.webhook_hub.port

            wiring_body = b'{"wiring": true}'
            status, payload = post_json(
                port,
                "/alpha/hook",
                wiring_body,
                hmac_sha256_hex("alpha-secret", wiring_body),
            )
            self.assertEqual((status, payload), (200, {"ok": True, "by": "alpha"}))
            self.assertEqual(alpha.received, [("/alpha/hook", wiring_body)])

            core.stop()
            self.assertFalse(core.webhook_hub.bound)
            with self.assertRaises(urllib.error.URLError):
                post_json(port, "/alpha/hook", b"{}", "irrelevant")

    def test_core_without_webhook_adapters_binds_no_port(self):
        # 零路由纪律在接线层再钉一次：只有普通适配器时 hub 不绑端口 ——
        # 现有 13 个适配器的部署行为零变化。
        with tempfile.TemporaryDirectory() as td:
            core, _, _, _, _, _ = make_env(td)
            core.start()
            self.addCleanup(core.stop)
            self.assertFalse(core.webhook_hub.bound)


if __name__ == "__main__":
    unittest.main()

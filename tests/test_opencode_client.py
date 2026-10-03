"""Tests for the Lane A OpenCode client, config and state store.

Unit tests are fully offline (``urllib.request.urlopen`` is mocked).
Smoke tests against the real local service only run when
``OPENCODE_BRIDGE_SMOKE=1`` is set, e.g.::

    OPENCODE_BRIDGE_SMOKE=1 python -m unittest tests.test_opencode_client -v
"""

from __future__ import annotations

import io
import json
import os
import queue
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest import mock

from opencode_bridge.config import Config
from opencode_bridge.opencode_client import (
    DEFAULT_SERVICE_URL,
    Endpoint,
    OpenCodeClient,
    OpenCodeError,
    discover_endpoint,
)
from opencode_bridge.state import StateStore

SMOKE = os.environ.get("OPENCODE_BRIDGE_SMOKE") == "1"
SMOKE_URL = "http://127.0.0.1:4097"
SMOKE_PASSWORD = "-EwcEP_L89tX8zXzw8iPoCIrqDZPiDDYcUP4tBb5XLA"
SMOKE_DIRECTORY = os.path.join(tempfile.gettempdir(), "opencode")


# ----------------------------------------------------------------------
# fakes
# ----------------------------------------------------------------------
class FakeHTTPResponse:
    """Minimal urllib response double backed by ``io.BytesIO``."""

    def __init__(self, payload: bytes = b"", status: int = 200) -> None:
        self._bio = io.BytesIO(payload)
        self.status = status
        self.code = status
        self.closed = False

    def read(self, n: int = -1) -> bytes:
        return self._bio.read(n)

    def readline(self, n: int = -1) -> bytes:
        return self._bio.readline(n)

    def close(self) -> None:
        self.closed = True
        try:
            self._bio.close()
        except Exception:
            pass

    def __enter__(self) -> "FakeHTTPResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        self.close()
        return False


class BlockingHTTPResponse:
    """A stream whose ``readline`` blocks until ``close()`` is called."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self.status = 200
        self.code = 200
        self.closed = False

    def read(self, n: int = -1) -> bytes:
        return b""

    def readline(self, n: int = -1) -> bytes:
        # Bounded so a broken close() fails the test instead of hanging it.
        self._event.wait(5.0)
        raise OSError("stream closed")

    def close(self) -> None:
        self.closed = True
        self._event.set()

    def __enter__(self) -> "BlockingHTTPResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        self.close()
        return False


def make_client(timeout: float = 5.0) -> OpenCodeClient:
    return OpenCodeClient(Endpoint("http://127.0.0.1:4097", "pw"), timeout=timeout)


def http_error(url: str, code: int, body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url, code, f"HTTP {code}", {}, io.BytesIO(body)
    )


# ----------------------------------------------------------------------
# Endpoint / discovery
# ----------------------------------------------------------------------
class EndpointTests(unittest.TestCase):
    def test_auth_header_is_basic_opencode(self) -> None:
        ep = Endpoint("http://host:1/", "secret")
        self.assertEqual(ep.url, "http://host:1")  # trailing slash stripped
        header = ep.auth_header()
        self.assertEqual(header["Authorization"], "Basic b3BlbmNvZGU6c2VjcmV0")

    def test_username_defaults_to_opencode(self) -> None:
        self.assertEqual(Endpoint("http://h", "p").username, "opencode")


class DiscoverEndpointTests(unittest.TestCase):
    def test_explicit_beats_env(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"OPENCODE_URL": "http://env:1", "OPENCODE_PASSWORD": "envpw"},
        ):
            ep = discover_endpoint("http://explicit:2", "xp")
            self.assertEqual(ep.url, "http://explicit:2")
            self.assertEqual(ep.password, "xp")
            # url explicit, password falls through to env (priority 2 then 3)
            ep2 = discover_endpoint("http://explicit:2")
            self.assertEqual(ep2.url, "http://explicit:2")
            self.assertEqual(ep2.password, "envpw")

    def test_env_only(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"OPENCODE_URL": "http://env:1", "OPENCODE_PASSWORD": "envpw"},
            clear=True,
        ):
            ep = discover_endpoint()
            self.assertEqual(ep.url, "http://env:1")
            self.assertEqual(ep.password, "envpw")

    def test_service_file_param(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "service.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"url": "http://file:9", "password": "fpw", "pid": 1}, fh)
            with mock.patch.dict(os.environ, {}, clear=True):
                ep = discover_endpoint(service_file=path)
            self.assertEqual(ep.url, "http://file:9")
            self.assertEqual(ep.password, "fpw")

    def test_service_file_env_var(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "service.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"url": "http://envfile:9", "password": "efpw"}, fh)
            with mock.patch.dict(
                os.environ, {"OPENCODE_SERVICE_FILE": path}, clear=True
            ):
                ep = discover_endpoint()
            self.assertEqual(ep.url, "http://envfile:9")
            self.assertEqual(ep.password, "efpw")

    def test_service_file_fills_only_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "service.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"url": "http://file:9", "password": "fpw"}, fh)
            with mock.patch.dict(os.environ, {}, clear=True):
                ep = discover_endpoint("http://only-url", service_file=path)
            self.assertEqual(ep.url, "http://only-url")
            self.assertEqual(ep.password, "fpw")

    def test_password_without_url_uses_default_service_url(self) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_PASSWORD": "pw"}, clear=True):
            with mock.patch(
                "opencode_bridge.opencode_client._service_file_candidates",
                return_value=[],
            ):
                ep = discover_endpoint()
        self.assertEqual(ep.url, DEFAULT_SERVICE_URL)
        self.assertEqual(ep.password, "pw")

    def test_total_failure_raises(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch(
                "opencode_bridge.opencode_client._service_file_candidates",
                return_value=[],
            ):
                with self.assertRaises(OpenCodeError) as cm:
                    discover_endpoint()
        self.assertIn("cannot resolve opencode endpoint", str(cm.exception))


# ----------------------------------------------------------------------
# OpenCodeClient — request level
# ----------------------------------------------------------------------
class RequestTests(unittest.TestCase):
    def test_http_404_raises_opencode_error_with_status_and_body(self) -> None:
        client = make_client()
        err = http_error(
            "http://x/api/session/ses_x",
            404,
            b'{"_tag":"SessionNotFoundError","message":"Session not found"}',
        )
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(OpenCodeError) as cm:
                client.get_session("ses_x")
        self.assertEqual(cm.exception.status, 404)
        self.assertIn("SessionNotFoundError", cm.exception.body or "")

    def test_prompt_busy_409_raises_with_status(self) -> None:
        client = make_client()
        err = http_error("http://x/prompt", 409, b'{"message":"busy"}')
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(OpenCodeError) as cm:
                client.prompt("ses_1", "hi")
        self.assertEqual(cm.exception.status, 409)

    def test_empty_204_body_returns_none(self) -> None:
        client = make_client()
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=lambda *a, **k: FakeHTTPResponse(b"", 204),
        ):
            self.assertIsNone(client.delete_session("ses_1"))
            self.assertIsNone(client.interrupt("ses_1"))
            self.assertIsNone(
                client.set_session_model("ses_1", "opencode", "space-bunny-free")
            )
            self.assertIsNone(
                client.reply_permission("ses_1", "req_1", "reject")
            )

    def test_interrupt_sends_post_without_json_body(self) -> None:
        client = make_client()
        seen: list = []
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=lambda req, **k: seen.append(req) or FakeHTTPResponse(b"", 200),
        ):
            client.interrupt("ses_1")
        self.assertEqual(len(seen), 1)
        req = seen[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.data, b"")
        self.assertIn("/api/session/ses_1/interrupt", req.full_url)
        self.assertEqual(req.get_header("Authorization"), "Basic b3BlbmNvZGU6cHc=")

    def test_create_session_posts_expected_payload(self) -> None:
        client = make_client()
        seen: list = []
        payload = {"data": {"id": "ses_abc"}}

        def fake(req, **kwargs):
            seen.append(req)
            return FakeHTTPResponse(json.dumps(payload).encode("utf-8"), 200)

        with mock.patch("urllib.request.urlopen", side_effect=fake):
            sid = client.create_session(
                directory=r"C:\tmp", title="t", agent="build",
                permissions=[{"action": "edit", "resource": "file", "effect": "ask"}],
            )
        self.assertEqual(sid, "ses_abc")
        body = json.loads(seen[0].data.decode("utf-8"))
        self.assertEqual(body["location"], {"directory": r"C:\tmp"})
        self.assertEqual(body["title"], "t")
        self.assertEqual(body["agent"], "build")
        self.assertEqual(body["permissions"][0]["effect"], "ask")
        self.assertEqual(seen[0].get_header("Content-type"),
                         "application/json; charset=utf-8")

    def test_list_and_messages_unwrap_data(self) -> None:
        client = make_client()
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=lambda req, **k: FakeHTTPResponse(
                json.dumps({"data": [{"id": "ses_1"}], "cursor": {}}).encode("utf-8")
            ),
        ):
            self.assertEqual(client.list_sessions(limit=5), [{"id": "ses_1"}])
            self.assertEqual(client.messages("ses_1"), [{"id": "ses_1"}])

    def test_set_session_model_posts_a_model_ref_object(self) -> None:
        # 真机 2.0.22 实测：请求体是 Model.Ref **对象**（不是字符串），
        # 只给 providerID + id 即可，variant 由服务端补成 default，成功回 204。
        client = make_client()
        seen: list = []
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=lambda req, **k: seen.append(req) or FakeHTTPResponse(b"", 204),
        ):
            client.set_session_model("ses_1", "opencode", "space-bunny-free")
        req = seen[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertTrue(req.full_url.endswith("/api/session/ses_1/model"))
        self.assertEqual(
            json.loads(req.data.decode("utf-8")),
            {"model": {"providerID": "opencode", "id": "space-bunny-free"}},
        )

    def test_list_models_unwraps_the_whole_catalog(self) -> None:
        client = make_client()
        catalog = [
            {"providerID": "opencode", "id": "space-bunny-free",
             "name": "Space Bunny Free"},
            {"providerID": "anthropic", "id": "claude-sonnet-4-5",
             "name": "Claude Sonnet 4.5"},
        ]
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=lambda req, **k: FakeHTTPResponse(
                json.dumps({"data": catalog}).encode("utf-8")
            ),
        ):
            models = client.list_models()
        self.assertEqual(models, catalog)

    def test_reply_permission_invalid_decision_raises_valueerror(self) -> None:
        client = make_client()
        with mock.patch("urllib.request.urlopen") as urlopen_mock:
            with self.assertRaises(ValueError):
                client.reply_permission("ses_1", "req_1", "yes")
            urlopen_mock.assert_not_called()

    def test_reply_permission_posts_decision(self) -> None:
        client = make_client()
        seen: list = []
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=lambda req, **k: seen.append(req) or FakeHTTPResponse(b"", 204),
        ):
            client.reply_permission("ses_1", "req_9", "once", message="ok")
        req = seen[0]
        self.assertTrue(
            req.full_url.endswith("/api/session/ses_1/permission/req_9/reply")
        )
        self.assertEqual(
            json.loads(req.data.decode("utf-8")),
            {"decision": "once", "message": "ok"},
        )


# ----------------------------------------------------------------------
# OpenCodeClient — SSE
# ----------------------------------------------------------------------
class SubscribeTests(unittest.TestCase):
    FRAME = (
        b'data: {"type":"server.connected","data":{}}\n\n'
        b": heartbeat\n\n"
        b'data: {"type":"session.text.delta","data":{"text":"hi"}}\n\n'
        b"data: this is not json\n\n"
        b"data: [1, 2, 3]\n\n"
        b"\n"
        b"event: ping\n"
        b'data: {"type":"session.idle"}\n\n'
    )

    def test_multi_frame_parse_ignores_heartbeat_and_junk(self) -> None:
        client = make_client()
        seen: list = []
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=lambda req, **k: seen.append(req)
            or FakeHTTPResponse(self.FRAME),
        ):
            events = list(client.subscribe(restart=False))

        self.assertEqual(
            [e["type"] for e in events],
            ["server.connected", "session.text.delta", "session.idle"],
        )
        self.assertEqual(events[1]["data"]["text"], "hi")
        # request shape
        self.assertEqual(len(seen), 1)
        req = seen[0]
        self.assertEqual(req.get_method(), "GET")
        self.assertTrue(req.full_url.endswith("/api/event"))
        self.assertEqual(req.get_header("Authorization"), "Basic b3BlbmNvZGU6cHc=")

    def test_stream_uses_long_socket_timeout(self) -> None:
        client = make_client()
        timeouts: list = []
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=lambda req, timeout=None, **k: (
                timeouts.append(timeout) or FakeHTTPResponse(b"")
            ),
        ):
            list(client.subscribe(restart=False))
        self.assertEqual(len(timeouts), 1)
        self.assertGreaterEqual(timeouts[0], 60)

    def test_non_200_raises_opencode_error(self) -> None:
        client = make_client()
        err = http_error("http://x/api/event", 401, b"unauthorized")
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(OpenCodeError) as cm:
                next(client.subscribe(restart=True))
        self.assertEqual(cm.exception.status, 401)
        self.assertIn("unauthorized", cm.exception.body or "")

    def test_restart_backoff_reconnects_then_raises_on_401(self) -> None:
        client = make_client()
        responses = [
            urllib.error.URLError("connection refused"),
            FakeHTTPResponse(
                b'data: {"type":"session.status","data":{"status":"busy"}}\n\n'
            ),
            http_error("http://x/api/event", 401, b"nope"),
        ]
        with mock.patch("urllib.request.urlopen", side_effect=responses) as m:
            with mock.patch("opencode_bridge.opencode_client._RECONNECT_MIN", 0.01), \
                 mock.patch("opencode_bridge.opencode_client._RECONNECT_MAX", 0.02):
                gen = client.subscribe(restart=True)
                ev = next(gen)
                self.assertEqual(ev["type"], "session.status")
                with self.assertRaises(OpenCodeError) as cm:
                    next(gen)
                self.assertEqual(cm.exception.status, 401)
        self.assertEqual(m.call_count, 3)

    def test_restart_false_stops_on_connection_error(self) -> None:
        client = make_client()
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("boom"),
        ):
            with self.assertRaises(StopIteration):
                next(client.subscribe(restart=False))

    def test_close_interrupts_blocked_subscribe_within_2s(self) -> None:
        client = make_client()
        blocking = BlockingHTTPResponse()
        result: dict = {}

        with mock.patch("urllib.request.urlopen", return_value=blocking):
            gen = client.subscribe(restart=True)

            def run() -> None:
                try:
                    next(gen)
                    result["outcome"] = "event"
                except StopIteration:
                    result["outcome"] = "stop"
                except Exception as exc:  # pragma: no cover
                    result["outcome"] = f"error: {exc}"

            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            time.sleep(0.3)  # let it block inside readline()
            started = time.time()
            client.close()
            worker.join(timeout=2.0)
            elapsed = time.time() - started

        self.assertFalse(worker.is_alive(), "subscribe did not exit after close()")
        self.assertLess(elapsed, 2.0, f"close() took {elapsed:.2f}s to interrupt")
        self.assertEqual(result["outcome"], "stop")
        self.assertTrue(blocking.closed)
        # a closed generator must not restart
        with self.assertRaises(StopIteration):
            next(gen)


# ----------------------------------------------------------------------
# config.py
# ----------------------------------------------------------------------
class ConfigTests(unittest.TestCase):
    def test_missing_file_returns_defaults(self) -> None:
        # Config.load() falls through to $OPENCODE_BRIDGE_CONFIG and then
        # ./config.json when the explicit path does not exist, so this test must
        # isolate both — otherwise it fails from an installed copy where the
        # installer has just created a real ./config.json.
        with tempfile.TemporaryDirectory() as td:
            cwd = os.getcwd()
            saved_env = os.environ.pop("OPENCODE_BRIDGE_CONFIG", None)
            try:
                os.chdir(td)
                cfg = Config.load(os.path.join(td, "nope.json"))
            finally:
                os.chdir(cwd)
                if saved_env is not None:
                    os.environ["OPENCODE_BRIDGE_CONFIG"] = saved_env
        self.assertEqual(cfg.opencode_url, "")
        self.assertEqual(cfg.opencode_directory, ".")
        self.assertEqual(cfg.permissions_mode, "ask")
        self.assertEqual(cfg.state_path, "state.json")
        self.assertEqual(cfg.adapters, {})

    def test_load_file_and_warn_on_unknown_key(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "config.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "opencode_url": "http://x:1",
                        "log_level": "DEBUG",
                        "adapters": {"telegram": {"bot_token": "t"}},
                        "mystery": 1,
                    },
                    fh,
                )
            with self.assertLogs("opencode_bridge.config", level="WARNING") as logs:
                cfg = Config.load(path)
        self.assertEqual(cfg.opencode_url, "http://x:1")
        self.assertEqual(cfg.log_level, "DEBUG")
        self.assertIn("telegram", cfg.adapters)
        self.assertTrue(any("mystery" in line for line in logs.output))
        self.assertEqual(cfg.to_dict()["state_path"], "state.json")

    def test_env_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(
                os.environ,
                {
                    "OPENCODE_URL": "http://env:1",
                    "OPENCODE_PASSWORD": "envpw",
                    "OPENCODE_DIRECTORY": r"D:\work",
                },
                clear=True,
            ):
                cfg = Config.load(os.path.join(td, "nope.json"))
        self.assertEqual(cfg.opencode_url, "http://env:1")
        self.assertEqual(cfg.opencode_password, "envpw")
        self.assertEqual(cfg.opencode_directory, r"D:\work")

    def test_open_code_bridge_config_env_var(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "config.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"opencode_agent": "plan"}, fh)
            with mock.patch.dict(
                os.environ, {"OPENCODE_BRIDGE_CONFIG": path}, clear=True
            ):
                cfg = Config.load()
        self.assertEqual(cfg.opencode_agent, "plan")


# ----------------------------------------------------------------------
# state.py
# ----------------------------------------------------------------------
class StateStoreTests(unittest.TestCase):
    def test_crud_and_reload(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "state.json")
            store = StateStore(path)
            self.assertIsNone(store.get_session("c1"))

            store.set_session("c1", "ses_1")
            store.set_meta("c1", "nick", "bob")
            store.flush()
            self.assertEqual(store.get_session("c1"), "ses_1")
            self.assertEqual(store.get_meta("c1", "nick"), "bob")
            self.assertEqual(store.get_meta("c1", "missing", 42), 42)
            self.assertEqual(store.all_sessions(), {"c1": "ses_1"})

            reloaded = StateStore(path)
            self.assertEqual(reloaded.all_sessions(), {"c1": "ses_1"})
            self.assertEqual(reloaded.get_meta("c1", "nick"), "bob")

            store.drop_session("c1")
            self.assertIsNone(store.get_session("c1"))
            StateStore(path).flush()  # no-op write must not fail
            self.assertEqual(StateStore(path).all_sessions(), {})

    def test_missing_and_corrupt_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "state.json")
            self.assertEqual(StateStore(path).all_sessions(), {})
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{ not json")
            store = StateStore(path)  # must not raise
            self.assertEqual(store.all_sessions(), {})
            store.set_session("c", "ses_c")
            self.assertEqual(StateStore(path).all_sessions(), {"c": "ses_c"})

    def test_atomic_write_leaves_no_temp_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "state.json")
            store = StateStore(path)
            for i in range(20):
                store.set_session(f"c{i}", f"ses_{i}")
            entries = os.listdir(td)
            self.assertEqual(entries, ["state.json"])
            with open(path, "rb") as fh:
                data = json.load(fh)  # valid JSON, never half-written
            self.assertEqual(len(data["sessions"]), 20)

    def test_concurrent_readers_and_writers(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "state.json")
            store = StateStore(path)
            errors: list[BaseException] = []

            def worker(idx: int) -> None:
                try:
                    for j in range(50):
                        cid = f"c{idx}-{j % 10}"
                        store.set_session(cid, f"ses_{idx}_{j}")
                        store.set_meta(cid, "n", j)
                        store.get_session(cid)
                        store.get_meta(cid, "n")
                        if j % 7 == 0:
                            store.flush()
                except BaseException as exc:  # pragma: no cover
                    errors.append(exc)

            threads = [
                threading.Thread(target=worker, args=(i,), daemon=True)
                for i in range(8)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
            self.assertFalse(errors, f"worker errors: {errors}")
            self.assertTrue(all(not t.is_alive() for t in threads))

            with open(path, "r", encoding="utf-8") as fh:
                on_disk = json.load(fh)
            self.assertEqual(on_disk["sessions"], store.all_sessions())
            # and the file must still be loadable
            self.assertEqual(StateStore(path).all_sessions(), store.all_sessions())


# ----------------------------------------------------------------------
# smoke tests against the real local service
# ----------------------------------------------------------------------
@unittest.skipUnless(SMOKE, "set OPENCODE_BRIDGE_SMOKE=1 to run smoke tests")
class SmokeTests(unittest.TestCase):
    client: OpenCodeClient
    created: list[str]

    @classmethod
    def setUpClass(cls) -> None:
        cls.client = OpenCodeClient(
            Endpoint(SMOKE_URL, SMOKE_PASSWORD), timeout=30.0
        )
        cls.created = []

    @classmethod
    def tearDownClass(cls) -> None:
        # 1. delete everything we tracked
        for sid in getattr(cls, "created", []):
            try:
                cls.client.delete_session(sid)
            except Exception as exc:  # pragma: no cover
                print(f"cleanup delete {sid} failed: {exc}")
        # 2. sweep leftovers titled "probe"
        try:
            data = cls.client._request(
                "GET", "/api/session", params={"search": "probe", "limit": 100}
            )
            for item in data.get("data") or []:
                sid = item.get("id")
                title = str(item.get("title") or "")
                if sid and "probe" in title and sid not in cls.created:
                    try:
                        cls.client.delete_session(sid)
                    except Exception as exc:  # pragma: no cover
                        print(f"sweep delete {sid} failed: {exc}")
        except Exception as exc:  # pragma: no cover
            print(f"sweep search failed: {exc}")

    def test_smoke_lifecycle(self) -> None:
        c = self.client

        # --- info ---
        info = c.info()
        self.assertIn("version", info)
        self.assertIn("pid", info)

        # --- subscribe: first event must arrive ---
        events: queue.Queue = queue.Queue()
        iterator = c.subscribe(restart=True)

        def pump() -> None:
            try:
                while True:
                    events.put(next(iterator))
            except StopIteration:
                events.put(None)
            except Exception as exc:  # pragma: no cover
                events.put(exc)

        pump_thread = threading.Thread(target=pump, daemon=True)
        pump_thread.start()

        # --- create session ---
        sid = c.create_session(
            directory=SMOKE_DIRECTORY, title="probe"
        )
        self.created.append(sid)
        self.assertTrue(sid.startswith("ses_"), sid)

        ev = events.get(timeout=15)
        self.assertIsInstance(ev, dict, f"first event: {ev!r}")
        self.assertTrue(ev.get("type"), ev)

        # --- read session state ---
        session = c.get_session(sid)
        self.assertEqual(session.get("id"), sid)
        self.assertEqual(session.get("title"), "probe")

        sessions = c.list_sessions(limit=5)
        self.assertIsInstance(sessions, list)
        self.assertTrue(any(s.get("id") == sid for s in sessions))

        messages = c.messages(sid)
        self.assertIsInstance(messages, list)

        # --- close must stop subscribe promptly ---
        started = time.time()
        c.close()
        pump_thread.join(timeout=3.0)
        elapsed = time.time() - started
        self.assertFalse(pump_thread.is_alive(), "subscribe survived close()")
        self.assertLess(elapsed, 3.0, f"close() took {elapsed:.2f}s")

        # --- delete + error path (404, no model involved) ---
        c.delete_session(sid)
        self.created.remove(sid)
        with self.assertRaises(OpenCodeError) as cm:
            c.get_session(sid)
        self.assertEqual(cm.exception.status, 404)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

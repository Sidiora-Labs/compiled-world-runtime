# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Gateway client tests against a local scripted HTTP server. No real gateway is contacted."""

import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# Make `benchmark` importable when this file runs directly under python3 -I.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from benchmark.harness import model as mm

# The machine-wide in-flight bound is shared with live campaigns; these local servers bypass it.
os.environ[mm.INFLIGHT_ENV] = "0"

KEYS = ["key-one-SECRET-1111", "key-two-SECRET-2222", "key-three-SECRET-3333"]


def ok_body(text="hello"):
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "served-model",
        "content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 12, "output_tokens": 3},
    }


class Server:
    def __init__(self, script):
        self.script = list(script)  # list of (status, body_dict_or_text, headers)
        self.seen = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("content-length", 0))
                outer.seen.append(
                    {
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": json.loads(self.rfile.read(length)),
                        "path": self.path,
                    }
                )
                status, body, headers = (
                    outer.script.pop(0) if outer.script else (200, ok_body(), {})
                )
                data = (json.dumps(body) if not isinstance(body, str) else body).encode()
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/v1/messages"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def client(url, keys=KEYS, **kw):
    """A loopback test client: plain http allowed, no jitter, round-robin from slot 1."""
    sleeps = []
    options = {"jitter": lambda delay: delay, "start_slot": 0, "allow_insecure_loopback": True}
    options.update(kw)
    c = mm.GatewayClient(url, "configured-model", keys, sleep=sleeps.append, timeout_s=5, **options)
    return c, sleeps


def key_used(request):
    return request["headers"]["x-api-key"]


class Headers(unittest.TestCase):
    def test_request_format(self):
        with Server([(200, ok_body("ok"), {})]) as server:
            c, _ = client(server.url)
            reply = c.complete([{"role": "user", "content": "hi"}], max_tokens=20)
        request = server.seen[0]
        self.assertNotIn("authorization", request["headers"])
        self.assertEqual(request["headers"]["x-api-key"], KEYS[0])
        self.assertEqual(sum(KEYS[0] in v for v in request["headers"].values()), 1)
        self.assertEqual(request["headers"]["anthropic-version"], "2023-06-01")
        self.assertEqual(request["headers"]["content-type"], "application/json")
        self.assertEqual(
            request["body"],
            {
                "model": "configured-model",
                "max_tokens": 20,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        self.assertEqual(reply.text, "ok")
        self.assertEqual(
            (reply.input_tokens, reply.output_tokens, reply.model), (12, 3, "served-model")
        )
        self.assertEqual(reply.key_slot, 1)

    def test_context_is_one_user_message_without_system(self):
        with Server([]) as server:
            c, _ = client(server.url)
            c.complete_context("MANUAL...", max_tokens=50, params={"metadata": {"user_id": "cwr"}})
        body = server.seen[0]["body"]
        self.assertNotIn("system", body)
        self.assertEqual(body["messages"], [{"role": "user", "content": "MANUAL..."}])
        self.assertEqual(body["metadata"], {"user_id": "cwr"})

    def test_params_cannot_override_core_fields(self):
        c, _ = client("http://127.0.0.1:9/")
        with self.assertRaises(mm.ModelError):
            c.complete([], max_tokens=1, params={"model": "other"})


class Retries(unittest.TestCase):
    def test_429_and_5xx_retry_with_rotation_and_backoff(self):
        script = [
            (429, {"error": "rate"}, {"retry-after": "3"}),
            (503, {"error": "down"}, {}),
            (200, ok_body(), {}),
        ]
        with Server(script) as server:
            c, sleeps = client(server.url, backoff_base_s=1.0, backoff_cap_s=10.0)
            reply = c.complete([{"role": "user", "content": "x"}], max_tokens=5)
        self.assertEqual([key_used(r) for r in server.seen], KEYS)
        self.assertEqual(sleeps, [3.0, 2.0])
        self.assertEqual(reply.attempts, 3)
        self.assertEqual(reply.key_slot, 3)

    def test_round_robin_across_requests(self):
        with Server([]) as server:
            c, _ = client(server.url)
            for _ in range(4):
                c.complete([{"role": "user", "content": "x"}], max_tokens=5)
        self.assertEqual([key_used(r) for r in server.seen], [KEYS[0], KEYS[1], KEYS[2], KEYS[0]])

    def test_400_fails_without_retry_and_never_echoes_the_body(self):
        body = {"error": {"type": "invalid_request_error", "message": f"bad request for {KEYS[0]}"}}
        with Server([(400, body, {})]) as server:
            c, sleeps = client(server.url)
            with self.assertRaises(mm.ModelError) as caught:
                c.complete([{"role": "user", "content": "x"}], max_tokens=5)
        self.assertEqual(len(server.seen), 1)
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(caught.exception.category, "http_status")
        self.assertEqual(str(caught.exception), "http_status: HTTP 400 invalid_request_error")
        self.assertNotIn(KEYS[0], str(caught.exception))
        self.assertNotIn("bad request", str(caught.exception))
        self.assertEqual(sleeps, [])

    def test_auth_failure_moves_to_next_key_then_gives_up(self):
        with Server([(401, {"error": "no"}, {})] * 3) as server:
            c, _ = client(server.url)
            with self.assertRaises(mm.ModelError) as caught:
                c.complete([{"role": "user", "content": "x"}], max_tokens=5)
        self.assertEqual([key_used(r) for r in server.seen], KEYS)
        self.assertEqual(caught.exception.status, 401)

    def test_refused_key_is_quarantined_for_the_client_lifetime(self):
        with Server([(401, {"error": "no"}, {})]) as server:
            c, _ = client(server.url)
            c.complete([{"role": "user", "content": "x"}], max_tokens=5)
            for _ in range(3):
                c.complete([{"role": "user", "content": "x"}], max_tokens=5)
        self.assertEqual(c.quarantined_slots, [1])
        used = [key_used(r) for r in server.seen]
        self.assertEqual(used[0], KEYS[0])
        self.assertNotIn(KEYS[0], used[1:])

    def test_full_jitter_stays_within_the_capped_backoff(self):
        c = mm.GatewayClient(
            "https://gateway.invalid/", "m", ["k"], backoff_base_s=1.0, backoff_cap_s=8.0
        )
        for attempt in range(6):
            delays = [c._backoff(attempt, None) for _ in range(200)]
            self.assertTrue(all(0.0 <= d <= min(8.0, 2**attempt) for d in delays))
            self.assertGreater(len(set(delays)), 1)
        self.assertGreaterEqual(c._backoff(0, "5"), 5.0)

    def test_clients_start_on_different_slots_and_share_safely(self):
        starts = {mm.GatewayClient("https://gateway.invalid/", "m", KEYS)._next for _ in range(3)}
        self.assertEqual(starts, {0, 1, 2})
        c = mm.GatewayClient("https://gateway.invalid/", "m", KEYS, start_slot=0)
        taken = []
        threads = [
            threading.Thread(target=lambda: taken.extend(c._take_slot() for _ in range(300)))
            for _ in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(taken.count(slot) for slot in range(3)), [400, 400, 400])

    def test_auth_failure_recovers_on_another_key(self):
        with Server([(403, {"error": "no"}, {}), (200, ok_body(), {})]) as server:
            c, _ = client(server.url)
            reply = c.complete([{"role": "user", "content": "x"}], max_tokens=5)
        self.assertEqual(reply.key_slot, 2)

    def test_exhausted_retries(self):
        with Server([(500, {"error": "x"}, {})] * 4) as server:
            c, sleeps = client(server.url, max_attempts=4, backoff_base_s=0.5, backoff_cap_s=1.0)
            with self.assertRaises(mm.ModelError) as caught:
                c.complete([{"role": "user", "content": "x"}], max_tokens=5)
        self.assertEqual(caught.exception.attempts, 4)
        self.assertEqual(sleeps, [0.5, 1.0, 1.0])

    def test_transport_error_retries(self):
        c, sleeps = client("http://127.0.0.1:9/v1/messages", max_attempts=2)
        with self.assertRaises(mm.ModelError) as caught:
            c.complete([{"role": "user", "content": "x"}], max_tokens=5)
        self.assertEqual(caught.exception.category, "transport")
        self.assertNotIn("127.0.0.1", str(caught.exception))
        self.assertEqual(len(sleeps), 1)

    def test_non_messages_body_is_an_error(self):
        with Server([(200, {"unexpected": True}, {})]) as server:
            c, _ = client(server.url)
            with self.assertRaises(mm.ModelError):
                c.complete([{"role": "user", "content": "x"}], max_tokens=5)


class Secrets(unittest.TestCase):
    def test_repr_never_contains_keys(self):
        c, _ = client("http://127.0.0.1:9/")
        for text in (repr(c), str(c), repr(c.__dict__)):
            for key in KEYS:
                self.assertNotIn(key, text)

    def test_from_env_reads_the_process_environment(self):
        environ = {
            "MESSAGES_URL": "https://gateway.invalid/v1/messages",
            "MODEL": "m-0",
            "CLUSTER_2_GATEWAY_API_KEY": "k2",
        }
        c = mm.GatewayClient.from_env(environ=environ)
        self.assertEqual((c.model, c.key_count), ("m-0", 1))
        with self.assertRaises(mm.ModelError) as caught:
            mm.GatewayClient.from_env(environ={"MODEL": "m-0", "UNRELATED": "secret-value"})
        self.assertEqual(caught.exception.category, "config")
        self.assertIn("MESSAGES_URL", str(caught.exception))
        self.assertIn("CLUSTER_<n>_GATEWAY_API_KEY", str(caught.exception))
        self.assertNotIn("secret-value", str(caught.exception))

    def test_explicit_env_file_outside_the_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / "gateway.env"
            env.write_text(
                "# comment\nMESSAGES_URL=https://gateway.invalid/v1/messages\n"
                "export MODEL='m-1'\n"
                'CLUSTER_1_GATEWAY_API_KEY="k1"\nCLUSTER_3_GATEWAY_API_KEY=k3\nOTHER=x\n'
            )
            values = mm.read_env_file(env)
            self.assertEqual(values["MODEL"], "m-1")
            self.assertEqual(values["CLUSTER_1_GATEWAY_API_KEY"], "k1")
            c = mm.GatewayClient.from_env(env, environ={"MODEL": "from-environment"})
        self.assertEqual(c.model, "from-environment")
        self.assertEqual(c.key_count, 2)

    def test_credentials_file_inside_the_repository_refused(self):
        with self.assertRaises(mm.ModelError) as caught:
            mm.GatewayClient.from_env(mm.REPO / ".env", environ={})
        self.assertIn("inside the repository", str(caught.exception))
        self.assertFalse(hasattr(mm, "DEFAULT_ENV"))

    def test_http_endpoint_refused_unless_loopback_test_server(self):
        for url in ("http://gateway.invalid/v1/messages", "ftp://gateway.invalid/x"):
            with self.assertRaises(mm.ModelError) as caught:
                mm.GatewayClient(url, "m", ["k"])
            self.assertIn("https", str(caught.exception))
        with self.assertRaises(mm.ModelError):
            mm.GatewayClient("http://127.0.0.1:9/", "m", ["k"])
        with self.assertRaises(mm.ModelError):
            mm.GatewayClient("http://gateway.invalid/", "m", ["k"], allow_insecure_loopback=True)
        mm.GatewayClient("http://127.0.0.1:9/", "m", ["k"], allow_insecure_loopback=True)

    def test_redirect_is_never_followed_and_the_key_never_reaches_the_target(self):
        target = Server([])
        origin = Server([(307, {"moved": True}, {"location": target.url})])
        with target, origin:
            c, sleeps = client(origin.url)
            with self.assertRaises(mm.ModelError) as caught:
                c.complete([{"role": "user", "content": "x"}], max_tokens=5)
        self.assertEqual(caught.exception.category, "redirect")
        self.assertEqual(caught.exception.status, 307)
        self.assertEqual(len(origin.seen), 1)
        self.assertEqual(target.seen, [])
        self.assertEqual(sleeps, [])

    def test_errors_never_carry_the_endpoint_host_or_body(self):
        host = "gateway.invalid"

        def failing_opener(request, timeout):
            raise mm.urllib.error.URLError(OSError(f"cannot reach {request.full_url} ({host})"))

        c, _ = client(f"https://{host}/v1/messages", opener=failing_opener, max_attempts=2)
        with self.assertRaises(mm.ModelError) as caught:
            c.complete([{"role": "user", "content": "x"}], max_tokens=5)
        self.assertEqual(caught.exception.category, "transport")
        self.assertNotIn(host, str(caught.exception))
        body = {"error": {"type": "api_error", "message": f"upstream {host} account acct-7"}}
        with Server([(500, body, {})] * 2) as server:
            c, _ = client(server.url, max_attempts=2)
            with self.assertRaises(mm.ModelError) as caught:
                c.complete([{"role": "user", "content": "x"}], max_tokens=5)
        text = str(caught.exception)
        for leaked in (host, "acct-7", "127.0.0.1", server.url):
            self.assertNotIn(leaked, text)
        self.assertIn("HTTP 500 api_error", text)

    def test_incomplete_configuration_refused(self):
        with self.assertRaises(mm.ModelError):
            mm.GatewayClient("", "m", ["k"])
        with self.assertRaises(mm.ModelError):
            mm.GatewayClient("http://x", "m", ["", ""])


class ReferenceStub(unittest.TestCase):
    @staticmethod
    def turns():
        env = yield "a = 1"
        yield f"t = {env['world']['time']}"

    def test_plays_the_solution_with_envelopes_read_from_the_context(self):
        stub = mm.ReferenceStubModel(self.turns, seed=1)
        first = stub.complete_context("MANUAL", max_tokens=10)
        envelope = {"status": "ok", "world": {"time": 7, "turn": 1}, "trace": []}
        second = stub.complete_context(
            "MANUAL\n" + json.dumps(envelope) + "\n# Task", max_tokens=10
        )
        third = stub.complete_context("MANUAL", max_tokens=10)
        self.assertEqual(first.text, "```python\na = 1\n```\n")
        self.assertEqual(second.text, "```python\nt = 7\n```\n")
        self.assertEqual(third.text, "```python\npass\n```\n")
        self.assertEqual((first.model, first.input_tokens), (mm.STUB_MODEL, None))

    def test_even_seed_probes_first_and_is_deterministic(self):
        replies = [
            [mm.ReferenceStubModel(self.turns, seed=2).complete_context("M", max_tokens=1).text]
            for _ in range(2)
        ]
        self.assertEqual(replies[0], replies[1])
        self.assertIn("/.cwr-stub-probe/seed-2", replies[0][0])

    def test_envelope_from_context_ignores_other_json(self):
        context = '{"not": "an envelope"}\nprose'
        self.assertIsNone(mm.envelope_from_context(context))

    def test_envelope_from_context_keeps_unicode_line_separators(self):
        envelope = {"status": "ok", "world": {"time": 2}, "trace": [], "out": "x\u2028y\x85z"}
        context = "MANUAL\n" + json.dumps(envelope, ensure_ascii=False) + "\n# Task"
        self.assertEqual(mm.envelope_from_context(context), envelope)


if __name__ == "__main__":
    unittest.main()

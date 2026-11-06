#!/usr/bin/env python3
# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Self-test for the refworld stub servers and the cassette tool.

Starts search_server.py and publish_server.py as subprocesses on ephemeral ports, exercises every
endpoint and every injectable fault, checks determinism, rate limits, duplicate keys, the request
log, schedule validation, and cassette record/verify/digest/replay (replay with the network
disabled). Python 3.12 standard library only.

    python3 benchmark/stubs/selftest.py [--verbose] [--keep]

Exit 0 when every check passes, 1 otherwise. This is a component self-test of benchmark tooling;
it is not CWR-009 or CWR-014 evidence (those need the kernel gateway and their registered drivers).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import cassette_tool as ct  # noqa: E402 - importable only after the sys.path entry above

FAULTS = ("UNAVAILABLE", "TIMEOUT", "RATE_LIMITED", "REJECTED", "MALFORMED", "RESET")
FAULT_WIRE_CODE = {
    "UNAVAILABLE": "UNAVAILABLE",
    "TIMEOUT": "TIMEOUT",
    "RATE_LIMITED": "RATE_LIMITED",
    "REJECTED": "REJECTED",
    "MALFORMED": "INVALID",
    "RESET": "UNAVAILABLE",
}
SAMPLE_PARAMS = {
    "search.query": {"q": "tennis", "limit": 5},
    "publish.post": {"key": "fault-key", "title": "Fault probe", "body": "body"},
    "publish.status": {"post_id": "post_unknown"},
    "publish.delete": {"post_id": "post_unknown"},
}


class SelfTest:
    def __init__(self, verbose: bool, keep: bool):
        self.verbose = verbose
        self.keep = keep
        self.passed = 0
        self.failures: list[str] = []
        self.procs: list[subprocess.Popen] = []
        self.tmp = Path(tempfile.mkdtemp(prefix="cwr-stub-selftest-"))
        self.base: dict[str, str] = {}

    # ---- plumbing -------------------------------------------------------------------------

    def check(self, name: str, condition: bool, detail: object = "") -> bool:
        if condition:
            self.passed += 1
            if self.verbose:
                print(f"ok   {name}")
        else:
            self.failures.append(f"{name}: {detail}")
            print(f"FAIL {name}: {detail}")
        return condition

    def start(self, script: str, seed: int, schedule: Path | None = None) -> str:
        cmd = [sys.executable, str(HERE / script), "--port", "0", "--seed", str(seed)]
        if schedule:
            cmd += ["--schedule", str(schedule)]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.procs.append(proc)
        line = proc.stdout.readline()
        ready = json.loads(line)
        if ready.get("event") != "ready":
            raise RuntimeError(f"{script} failed to start: {line} {proc.stderr.read()}")
        return f"http://127.0.0.1:{ready['port']}"

    def stop(self) -> None:
        for proc in self.procs:
            proc.terminate()
        for proc in self.procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def admin(self, base: str, method: str, route: str, payload: object = None) -> tuple[int, dict]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(base + route, data=data, method=method)
        try:
            with urllib.request.urlopen(request, timeout=5) as reply:
                return reply.status, json.loads(reply.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def schedule(self, base: str, faults: list[dict], allow_undeclared: bool = False) -> int:
        document = {"format": "cwr.stub.schedule", "schema_version": 1, "faults": faults}
        if allow_undeclared:
            document["allow_undeclared"] = True
        status, _ = self.admin(base, "PUT", "/_stub/schedule", document)
        return status

    def call(
        self,
        adapter: str,
        params: dict,
        instance: str,
        world_time: int,
        timeout_ms: int | None = None,
        stubs: dict[str, str] | None = None,
    ) -> dict:
        contract = ct.REFWORLD_ADAPTERS[adapter]
        url = ct.resolve_endpoint(contract["endpoint"], stubs or self.base)
        return ct.call_stub(
            url, params, instance, world_time, timeout_ms or contract["timeout_ms"], adapter
        )

    @staticmethod
    def body(response: dict) -> dict:
        return json.loads(response["body"])

    def expect(
        self,
        name: str,
        adapter: str,
        response: dict,
        status: str,
        code: str | None = None,
        retry_after: int | None = None,
    ) -> dict:
        outcome = ct.normalise(adapter, response)
        self.check(
            name,
            outcome["status"] == status
            and outcome["code"] == code
            and (retry_after is None or outcome["retry_after"] == retry_after),
            {"outcome": outcome, "response": response},
        )
        return outcome

    # ---- phases ---------------------------------------------------------------------------

    def schedule_files(self) -> None:
        kvx = self.tmp / "publish-start.kvx"
        kvx.write_text(
            "# start-up schedule: the first publish.status of Instance i-pub answers 503\n"
            '[schedule]\nformat = "cwr.stub.schedule"\nschema_version = 1\n\n'
            '[fault.first-status-down]\nadapter = "publish.status"\ninstance = "i-pub"\n'
            'index = [0]\nfault = "UNAVAILABLE"\n',
            encoding="utf-8",
        )
        self.publish_schedule = kvx
        bad = self.tmp / "bad.json"
        bad.write_text(
            json.dumps(
                {
                    "format": "cwr.stub.schedule",
                    "schema_version": 1,
                    "faults": [{"adapter": "search.query", "index": 0, "fault": "REJECTED"}],
                }
            )
        )
        for script, path, want in (
            ("publish_server.py", kvx, 0),
            ("search_server.py", bad, 2),
            ("publish_server.py", bad, 0),
        ):
            result = subprocess.run(
                [sys.executable, str(HERE / script), "--check-schedule", "--schedule", str(path)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.check(
                f"check-schedule {script} {path.name} exits {want}",
                result.returncode == want,
                result.stdout + result.stderr,
            )
        malformed = [
            {"adapter": "search.query", "fault": "UNAVAILABLE"},  # neither index nor ticks
            {"adapter": "search.query", "index": 0, "ticks": [0, 5], "fault": "UNAVAILABLE"},
            {"adapter": "search.query", "ticks": [5, 5], "fault": "UNAVAILABLE"},
            {"adapter": "search.nope", "index": 0, "fault": "UNAVAILABLE"},
            {"adapter": "search.query", "index": 0, "fault": "EXPLODE"},
            {"adapter": "search.query", "index": 0, "fault": "UNAVAILABLE", "delay_ms": 5},
            {"adapter": "search.query", "index": -1, "fault": "UNAVAILABLE"},
            {"adapter": "search.query", "index": 0, "fault": "UNAVAILABLE", "colour": "red"},
        ]
        for number, fault in enumerate(malformed):
            path = self.tmp / f"malformed-{number}.json"
            path.write_text(
                json.dumps({"format": "cwr.stub.schedule", "schema_version": 1, "faults": [fault]})
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(HERE / "search_server.py"),
                    "--check-schedule",
                    "--schedule",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.check(
                f"malformed schedule {number} refused", result.returncode == 2, result.stdout
            )

    def health(self) -> None:
        for name in ("search", "publish"):
            status, body = self.admin(self.base[name], "GET", "/_stub/health")
            self.check(
                f"{name} health",
                status == 200 and body["server"] == name and body["seed"] == 7,
                body,
            )
        status, _ = self.admin(self.base["search"], "GET", "/v1/query")
        self.check("adapter endpoints refuse GET", status == 405, status)
        response = ct.call_stub(self.base["search"] + "/v1/nope", {}, "i-x", 0, 1000)
        self.check("unknown stub path is 404", response.get("status") == 404, response)
        url = self.base["search"] + "/v1/query"
        request = urllib.request.Request(url, data=b'{"q":"a","limit":1}', method="POST")
        try:
            urllib.request.urlopen(request, timeout=5)
            missing = 200
        except urllib.error.HTTPError as error:
            missing = error.code
        self.check("missing gateway headers refused with 400", missing == 400, missing)
        # Adapters that do not declare INVALID never see a 400: protocol errors are UNAVAILABLE and a
        # malformed post_id names no post (declared codes only, reference-world.md section 4).
        request = urllib.request.Request(
            self.base["publish"] + "/v1/status", data=b'{"post_id":"p"}', method="POST"
        )
        try:
            urllib.request.urlopen(request, timeout=5)
            missing = 200
        except urllib.error.HTTPError as error:
            missing = error.code
        self.check(
            "missing gateway headers on publish.status answered 503 (INVALID undeclared)",
            missing == 503,
            missing,
        )
        for adapter in ("publish.status", "publish.delete"):
            for bad in ({"post_id": 7}, {}, {"post_id": "p", "extra": 1}):
                r = self.call(adapter, bad, "i-proto", 0)
                self.check(
                    f"{adapter} malformed params {bad} answered 404 NOT_FOUND",
                    r.get("status") == 404,
                    r,
                )

    def search(self) -> None:
        a = self.call("search.query", {"q": "tennis", "limit": 5}, "i-search", 0)
        outcome = self.expect("search ok", "search.query", a, "ok")
        self.check(
            "search returns at most limit SearchHit records",
            isinstance(outcome["value"], list) and 0 < len(outcome["value"]) <= 5,
            outcome,
        )
        b = self.call("search.query", {"q": "tennis", "limit": 5}, "i-search", 1)
        self.check("search deterministic for equal seed and query", a["body"] == b["body"], (a, b))
        other = self.call(
            "search.query",
            {"q": "tennis", "limit": 5},
            "i-search",
            0,
            stubs={"search": self.base_seed8},
        )
        self.check("search content differs between seeds", other["body"] != a["body"], other)
        self.expect("search shape holds for another seed", "search.query", other, "ok")
        empty = self.call("search.query", {"q": "golf", "limit": 0}, "i-search", 2)
        self.check("search limit 0 gives empty list", self.body(empty) == {"result": []}, empty)
        self.expect(
            "search empty q INVALID",
            "search.query",
            self.call("search.query", {"q": "", "limit": 3}, "i-search", 3),
            "error",
            "INVALID",
        )
        self.expect(
            "search limit 51 LIMIT",
            "search.query",
            self.call("search.query", {"q": "x", "limit": 51}, "i-search", 3),
            "error",
            "LIMIT",
        )
        self.expect(
            "search negative limit INVALID",
            "search.query",
            self.call("search.query", {"q": "x", "limit": -1}, "i-search", 3),
            "error",
            "INVALID",
        )
        self.expect(
            "search missing param INVALID",
            "search.query",
            self.call("search.query", {"q": "x"}, "i-search", 3),
            "error",
            "INVALID",
        )
        for tick in range(10):
            self.expect(
                f"search rate window query {tick}",
                "search.query",
                self.call("search.query", {"q": f"q{tick}", "limit": 2}, "i-rl", tick),
                "ok",
            )
        limited = self.call("search.query", {"q": "q10", "limit": 2}, "i-rl", 10)
        self.expect(
            "search 11th query in 50 ticks RATE_LIMITED retry_after 40",
            "search.query",
            limited,
            "error",
            "RATE_LIMITED",
            40,
        )
        self.check(
            "retry_after header in ticks",
            limited["headers"].get("x-cwr-retry-after-ticks") == "40",
            limited,
        )
        self.expect(
            "search allowed again when window reopens",
            "search.query",
            self.call("search.query", {"q": "q10", "limit": 2}, "i-rl", 50),
            "ok",
        )
        self.expect(
            "search rate limit is per Instance",
            "search.query",
            self.call("search.query", {"q": "q10", "limit": 2}, "i-rl-other", 10),
            "ok",
        )

    def publish(self) -> None:
        inst = "i-pub"
        self.expect(
            "start-up KVX schedule: first status UNAVAILABLE",
            "publish.status",
            self.call("publish.status", {"post_id": "post_x"}, inst, 0),
            "error",
            "UNAVAILABLE",
        )
        self.expect(
            "status unknown NOT_FOUND",
            "publish.status",
            self.call("publish.status", {"post_id": "post_x"}, inst, 0),
            "error",
            "NOT_FOUND",
        )
        p1 = {"key": "k1", "title": "Hello", "body": "first"}
        first = self.call("publish.post", p1, inst, 0)
        receipt = self.expect("post ok", "publish.post", first, "ok")["value"]
        self.check(
            "receipt accepted_at is the world tick",
            receipt and receipt["accepted_at"] == 0,
            receipt,
        )
        replay = self.call("publish.post", p1, inst, 1)
        self.check(
            "identical replay returns original receipt",
            self.body(replay) == {"result": receipt},
            replay,
        )
        dup = self.call("publish.post", dict(p1, body="different"), inst, 1)
        outcome = self.expect(
            "different payload DUPLICATE_KEY", "publish.post", dup, "error", "DUPLICATE_KEY"
        )
        self.check("DUPLICATE_KEY carries original receipt", outcome["value"] == receipt, outcome)
        self.expect(
            "title over 200 INVALID",
            "publish.post",
            self.call("publish.post", {"key": "kt", "title": "x" * 201, "body": ""}, inst, 1),
            "error",
            "INVALID",
        )
        self.expect(
            "title of exactly 200 ok",
            "publish.post",
            self.call("publish.post", {"key": "kt", "title": "x" * 200, "body": ""}, inst, 2),
            "ok",
        )
        self.expect(
            "third accepted post ok",
            "publish.post",
            self.call("publish.post", {"key": "k3", "title": "t", "body": "b"}, inst, 3),
            "ok",
        )
        self.expect(
            "fourth post in 100 ticks RATE_LIMITED retry_after 96",
            "publish.post",
            self.call("publish.post", {"key": "k4", "title": "t", "body": "b"}, inst, 4),
            "error",
            "RATE_LIMITED",
            96,
        )
        self.check(
            "identical replay is not rate limited",
            self.body(self.call("publish.post", p1, inst, 5)) == {"result": receipt},
        )
        self.expect(
            "post allowed when window reopens",
            "publish.post",
            self.call("publish.post", {"key": "k4", "title": "t", "body": "b"}, inst, 100),
            "ok",
        )
        status = self.expect(
            "status of published post ok",
            "publish.status",
            self.call("publish.status", {"post_id": receipt["post_id"]}, inst, 101),
            "ok",
        )
        self.check(
            "PostStatus fields",
            status["value"]
            and status["value"]["state"] == "published"
            and status["value"]["key"] == "k1",
            status,
        )
        self.expect(
            "delete ok",
            "publish.delete",
            self.call("publish.delete", {"post_id": receipt["post_id"]}, inst, 102),
            "ok",
        )
        self.expect(
            "status after delete NOT_FOUND",
            "publish.status",
            self.call("publish.status", {"post_id": receipt["post_id"]}, inst, 103),
            "error",
            "NOT_FOUND",
        )
        self.expect(
            "second delete NOT_FOUND",
            "publish.delete",
            self.call("publish.delete", {"post_id": receipt["post_id"]}, inst, 104),
            "error",
            "NOT_FOUND",
        )
        other = self.call("publish.post", p1, "i-pub-other", 0)
        self.check(
            "idempotency store is per Instance; post_id depends on seed and key only",
            self.body(other)["result"]["post_id"] == receipt["post_id"],
            other,
        )

    def schedule_validation(self) -> None:
        for adapter, contract in ct.REFWORLD_ADAPTERS.items():
            base = self.base[adapter.split(".")[0]]
            for fault in FAULTS:
                accepted = (
                    self.schedule(base, [{"adapter": adapter, "index": 0, "fault": fault}]) == 200
                )
                declared = FAULT_WIRE_CODE[fault] in contract["failures"]
                self.check(
                    f"schedule {adapter} {fault} accepted iff its code is declared",
                    accepted == declared,
                    (accepted, declared),
                )
        self.check(
            "wildcard schedule accepted",
            self.schedule(
                self.base["publish"], [{"adapter": "publish.*", "index": 0, "fault": "UNAVAILABLE"}]
            )
            == 200,
        )
        self.check(
            "schedule entries for the other server are skipped",
            self.schedule(
                self.base["publish"],
                [{"adapter": "search.query", "index": 0, "fault": "MALFORMED"}],
            )
            == 200,
        )
        self.check(
            "bad schedule format refused",
            self.admin(
                self.base["search"], "PUT", "/_stub/schedule", {"format": "x", "schema_version": 1}
            )[0]
            == 400,
        )

    def every_fault(self) -> None:
        """Every fault on every endpoint, with allow_undeclared so undeclared mappings are visible."""
        for adapter, contract in ct.REFWORLD_ADAPTERS.items():
            server = adapter.split(".")[0]
            base = self.base[server]
            for fault in FAULTS:
                instance = f"f-{adapter.replace('.', '-')}-{fault.lower()}"
                spec = {"adapter": adapter, "instance": instance, "index": [0], "fault": fault}
                if fault == "TIMEOUT":
                    spec["delay_ms"] = 300
                if fault == "RATE_LIMITED":
                    spec["retry_after"] = 7
                if fault == "REJECTED":
                    spec["reason"] = "probe"
                self.check(
                    f"load {adapter} {fault}",
                    self.schedule(base, [spec], allow_undeclared=True) == 200,
                )
                response = self.call(
                    adapter,
                    SAMPLE_PARAMS[adapter],
                    instance,
                    0,
                    timeout_ms=100 if fault == "TIMEOUT" else None,
                )
                wire = {
                    "UNAVAILABLE": ("http", 503),
                    "RATE_LIMITED": ("http", 429),
                    "REJECTED": ("http", 422),
                    "MALFORMED": ("http", 200),
                    "TIMEOUT": ("timeout", None),
                    "RESET": ("transport_error", None),
                }[fault]
                got = (response["kind"], response.get("status"))
                self.check(f"{adapter} {fault} wire response {wire}", got == wire, response)
                code = FAULT_WIRE_CODE[fault]
                outcome = ct.normalise(adapter, response)
                want_code = code if code in contract["failures"] else contract["undeclared_status"]
                self.check(
                    f"{adapter} {fault} normalises to {want_code}",
                    outcome["code"] == want_code
                    and (want_code != "RATE_LIMITED" or outcome["retry_after"] == 7),
                    outcome,
                )
                follow = ct.normalise(
                    adapter, self.call(adapter, SAMPLE_PARAMS[adapter], instance, 1)
                )
                natural = None if adapter in ("search.query", "publish.post") else "NOT_FOUND"
                self.check(
                    f"{adapter} {fault} fires only at its index", follow["code"] == natural, follow
                )
        self.schedule(self.base["search"], [])
        self.schedule(self.base["publish"], [])

    def fault_side_effects(self) -> None:
        base = self.base["publish"]
        inst = "i-fault"
        self.schedule(
            base,
            [
                {"adapter": "publish.post", "instance": inst, "index": [0], "fault": "UNAVAILABLE"},
                {
                    "adapter": "publish.post",
                    "instance": inst,
                    "index": [1],
                    "fault": "RATE_LIMITED",
                    "retry_after": 7,
                },
                {
                    "adapter": "publish.post",
                    "instance": inst,
                    "index": [2],
                    "fault": "TIMEOUT",
                    "delay_ms": 400,
                },
                {"adapter": "publish.post", "instance": inst, "index": [3], "fault": "MALFORMED"},
                {"adapter": "publish.post", "instance": inst, "index": [4], "fault": "RESET"},
                {
                    "adapter": "publish.post",
                    "instance": "i-ticks",
                    "ticks": [500, 510],
                    "fault": "UNAVAILABLE",
                },
            ],
        )
        params = {"key": "kA", "title": "A", "body": "a"}
        self.expect(
            "scheduled 503 is UNAVAILABLE",
            "publish.post",
            self.call("publish.post", params, inst, 0),
            "error",
            "UNAVAILABLE",
        )
        self.expect(
            "scheduled 429 carries retry_after 7",
            "publish.post",
            self.call("publish.post", params, inst, 1),
            "error",
            "RATE_LIMITED",
            7,
        )
        self.expect(
            "scheduled stall is TIMEOUT",
            "publish.post",
            self.call("publish.post", params, inst, 2, timeout_ms=150),
            "error",
            "TIMEOUT",
        )
        self.expect(
            "scheduled malformed body is INVALID",
            "publish.post",
            self.call("publish.post", params, inst, 3),
            "error",
            "INVALID",
        )
        self.expect(
            "scheduled reset is UNAVAILABLE",
            "publish.post",
            self.call("publish.post", params, inst, 4),
            "error",
            "UNAVAILABLE",
        )
        reconcile = self.call("publish.post", params, inst, 5)
        result = self.body(reconcile).get("result") or {}
        self.check(
            "TIMEOUT completed the side effect; other faults did not (accepted_at 2)",
            result.get("accepted_at") == 2,
            reconcile,
        )
        self.expect(
            "tick-range fault inside range",
            "publish.post",
            self.call("publish.post", params, "i-ticks", 505),
            "error",
            "UNAVAILABLE",
        )
        self.expect(
            "tick-range fault ends at hi (exclusive)",
            "publish.post",
            self.call("publish.post", params, "i-ticks", 510),
            "ok",
        )
        self.schedule(base, [])

    def request_log(self) -> None:
        for name in ("search", "publish"):
            self.admin(self.base[name], "POST", "/_stub/reset")
        sent = [
            ("search.query", {"q": "a", "limit": 1}, "L1", 0),
            ("search.query", {"q": "a", "limit": 1}, "L1", 1),
            ("publish.post", {"key": "x", "title": "t", "body": "b"}, "L1", 2),
            ("publish.status", {"post_id": "nope"}, "L2", 3),
        ]
        for adapter, params, instance, tick in sent:
            self.call(adapter, params, instance, tick)
        observed = []
        for name in ("search", "publish"):
            observed += self.admin(self.base[name], "GET", "/_stub/log")[1]["requests"]
        expected_rows = sorted(
            (a, i, t, hashlib.sha256(ct.canonical_json(p)).hexdigest()) for a, p, i, t in sent
        )
        observed_rows = sorted(
            (r["adapter"], r["instance"], r["world_time"], r["request_sha256"]) for r in observed
        )
        self.check(
            "stub log records exactly the requests sent, no retries",
            expected_rows == observed_rows,
            (expected_rows, observed_rows),
        )
        indices = [r["index"] for r in observed if r["adapter"] == "search.query"]
        self.check(
            "per-(Instance, adapter) request index counts from 0", indices == [0, 1], indices
        )

    def record_sequence(self, recorder: ct.Recorder) -> None:
        tick = 0
        for q in ("tennis", "golf", "tennis"):
            recorder.call("search.query", {"q": q, "limit": 3}, tick)
            tick += 1
        for number in range(5):
            recorder.call(
                "publish.post", {"key": f"r{number}", "title": f"T{number}", "body": "b"}, tick
            )
            tick += 1
        recorder.call("publish.post", {"key": "r0", "title": "T0", "body": "b"}, tick)
        recorder.call("publish.post", {"key": "r0", "title": "T0", "body": "changed"}, tick + 1)
        recorder.call("publish.status", {"post_id": "post_missing"}, tick + 2)

    def cassettes(self) -> None:
        recordings = []
        for attempt in range(2):
            for name in ("search", "publish"):
                self.admin(self.base[name], "POST", "/_stub/reset")
            cassette = ct.Cassette("selftest/seed-7")
            self.record_sequence(ct.Recorder(cassette, self.base, f"rec-{attempt}"))
            recordings.append(cassette)
        a, b = (c.dumps() for c in recordings)
        self.check(
            "re-recording against reset stubs is byte-identical (Instance id not in cassette)",
            a == b,
        )
        self.check("recorded cassette verifies", ct.verify_bytes(a) == [], ct.verify_bytes(a))
        codes = sorted(
            {
                ct.normalise(e["adapter"], e["response"])["code"] or "ok"
                for e in recordings[0].entries
            }
        )
        self.check(
            "recording reached ok, RATE_LIMITED, DUPLICATE_KEY and NOT_FOUND",
            codes == ["DUPLICATE_KEY", "NOT_FOUND", "RATE_LIMITED", "ok"],
            codes,
        )
        seqs = [(e["adapter"], e["params"].get("q"), e["seq"]) for e in recordings[0].entries[:3]]
        self.check(
            "repeated identical request gets the next sequence",
            [s[2] for s in seqs] == [0, 0, 1],
            seqs,
        )
        lines = a.decode("utf-8").splitlines()
        tampered = json.loads(lines[1])
        tampered["params"]["q"] = "hockey"
        bad = "\n".join([lines[0], ct.canonical_json(tampered).decode("utf-8"), *lines[2:]]) + "\n"
        self.check(
            "tampered params fail verification",
            any("request_hash" in p for p in ct.verify_bytes(bad.encode())),
        )
        self.check(
            "non-canonical encoding fails verification",
            bool(
                ct.verify_bytes(
                    (lines[0].replace(",", ", ", 1) + "\n" + "\n".join(lines[1:]) + "\n").encode()
                )
            ),
        )

        # CLI: record, verify, digest, replay (network disabled), replay miss.
        requests = self.tmp / "requests.jsonl"
        requests.write_text(
            "".join(
                json.dumps(
                    {"adapter": e["adapter"], "params": e["params"], "world_time": e["world_time"]}
                )
                + "\n"
                for e in recordings[0].entries
            )
        )
        for name in ("search", "publish"):
            self.admin(self.base[name], "POST", "/_stub/reset")
        set_dir = self.tmp / "set"
        out = set_dir / "selftest" / "seed-7.jsonl"
        tool = [sys.executable, str(HERE / "cassette_tool.py")]
        rec = subprocess.run(
            [
                *tool,
                "record",
                "--stub",
                f"search={self.base['search']}",
                "--stub",
                f"publish={self.base['publish']}",
                "--instance",
                "cli-1",
                "--label",
                "selftest/seed-7",
                "--requests",
                str(requests),
                "--out",
                str(out),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.check("CLI record exit 0", rec.returncode == 0, rec.stderr)
        self.check("CLI recording equals library recording", out.exists() and out.read_bytes() == a)
        ver = subprocess.run(
            [*tool, "verify", str(out)], capture_output=True, text=True, timeout=30
        )
        self.check("CLI verify exit 0", ver.returncode == 0, ver.stdout)
        dig1 = subprocess.run(
            [*tool, "digest", str(set_dir)], capture_output=True, text=True, timeout=30
        ).stdout.strip()
        self.check(
            "set digest is stable and matches library",
            dig1 == ct.set_digest([set_dir]) and dig1.startswith("sha256:"),
            dig1,
        )
        before = len(self.admin(self.base["search"], "GET", "/_stub/log")[1]["requests"]) + len(
            self.admin(self.base["publish"], "GET", "/_stub/log")[1]["requests"]
        )
        rep = subprocess.run(
            [*tool, "replay", "--cassette", str(out), "--requests", str(requests)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        rows = [json.loads(line) for line in rep.stdout.splitlines()]
        expected = [ct.normalise(e["adapter"], e["response"]) for e in recordings[0].entries]
        self.check(
            "CLI replay serves every request with identical outcomes",
            rep.returncode == 0 and [r["outcome"] for r in rows] == expected,
            rep.stdout + rep.stderr,
        )
        after = len(self.admin(self.base["search"], "GET", "/_stub/log")[1]["requests"]) + len(
            self.admin(self.base["publish"], "GET", "/_stub/log")[1]["requests"]
        )
        self.check("replay contacted no stub", before == after, (before, after))
        extra = self.tmp / "extra.jsonl"
        extra.write_text(
            requests.read_text()
            + json.dumps({"adapter": "search.query", "params": {"q": "tennis", "limit": 3}})
            + "\n"
        )
        miss = subprocess.run(
            [*tool, "replay", "--cassette", str(out), "--requests", str(extra)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        last = json.loads(miss.stdout.splitlines()[-1])
        self.check(
            "cassette miss (third tennis call) is UNAVAILABLE, exit 1",
            miss.returncode == 1
            and not last["hit"]
            and last["outcome"]["code"] == "UNAVAILABLE"
            and last["seq"] == 2,
            miss.stdout,
        )
        guard = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.path.insert(0, sys.argv[1]); import cassette_tool as ct\n"
                "ct.forbid_network()\n"
                "try:\n    ct.call_stub(sys.argv[2] + '/v1/query', {'q': 'a', 'limit': 1}, 'g', 0, 1000)\n"
                "except ct.NetworkForbidden:\n    sys.exit(0)\nsys.exit(1)\n",
                str(HERE),
                self.base["search"],
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.check("forbid_network blocks sockets in replay", guard.returncode == 0, guard.stderr)
        hashed = subprocess.run(
            [*tool, "hash", "search.query", '{"limit": 3, "q": "tennis"}'],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
        self.check(
            "CLI hash equals library hash and ignores key order",
            hashed == ct.request_hash("search.query", {"q": "tennis", "limit": 3}),
            hashed,
        )

    def run(self) -> int:
        try:
            self.schedule_files()
            self.base = {
                "search": self.start("search_server.py", 7),
                "publish": self.start("publish_server.py", 7, self.publish_schedule),
            }
            self.base_seed8 = self.start("search_server.py", 8)
            self.health()
            self.search()
            self.publish()
            self.schedule_validation()
            self.schedule(self.base["search"], [])
            self.schedule(self.base["publish"], [])
            self.every_fault()
            self.fault_side_effects()
            self.request_log()
            self.cassettes()
        except Exception as error:
            import traceback

            traceback.print_exc()
            self.failures.append(f"crash: {error!r}")
        finally:
            self.stop()
            if not self.keep:
                shutil.rmtree(self.tmp, ignore_errors=True)
        print(
            json.dumps(
                {"selftest": "benchmark/stubs", "passed": self.passed, "failed": len(self.failures)}
            )
        )
        return 0 if not self.failures else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--verbose", action="store_true", help="print every passing check")
    parser.add_argument(
        "--keep", action="store_true", help="keep the temporary directory (honours TMPDIR)"
    )
    args = parser.parse_args()
    return SelfTest(args.verbose, args.keep).run()


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Task 2.3 driver: adapter gateway with normalisation, idempotency and cassette modes.

Case CWR-009. Exercises clauses 1.3, 7.1, 7.2, 7.3, 7.4, 7.5 and 7.6 against the real implementation:
it builds the `cwr` binary, runs the `cwr-kernel` unit tests, compiles a Build from a copy of
examples/refworld with the real `cwr build`, starts the refworld stub servers of benchmark/stubs
(search and publish, plus a second search stub with another content seed), and drives
`cwr instance create` and `cwr instance turn --gateway live|record|replay` as subprocesses. It judges
only what is observable from outside the kernel: result envelopes, ledger files, the cassette bytes,
the stubs' own request logs (`GET /_stub/log`), connection counters on decoy listeners, and strace.

The copied refworld keeps the shipped adapters, record shapes and fixtures, which speak the stub wire
protocol of benchmark/stubs/README.md; the copy only adds rules and probes. The publish.post contract declares `undeclared_status = "INVALID"`
(world-source.md section 7) so both undeclared fallbacks (UNAVAILABLE and INVALID) are exercised, and a
fixture case with an undeclared status proves the compiler's contract tests apply the same fallback as
the gateway (a negative-control build that expects UNAVAILABLE must be refused).
adapters/publish/live.py (publish.delete) and lib/world.py (world.version) additionally try to open a
TCP connection to a probe listener, directly and from a child process started through
_posixsubprocess.fork_exec (which raises no audit event), and record what happened and the process's
seccomp state in a report file. The listener must never see a connection, neither while `cwr build`
runs them in its contract tests (with bubblewrap, and without it, where the kernel's seccomp network
seal must be in force) nor while the kernel runs them; a final unsandboxed control run of the same
probe code shows the listener does count both. examples/refworld itself is not modified.

Further variants of the aligned copy: retry_after_ratio = 3 (retry_after scaled to ticks), and three
adapters that omit a code the gateway would need (TIMEOUT; DUPLICATE_KEY on a key adapter; a live.py
STATUS_CODES target outside failures), each of which `cwr build` must refuse. The manual's same-key
retry after RATE_LIMITED is driven against the stub's own rate-limit window.

A second Build is compiled from examples/refworld exactly as shipped (byte-identical copy minus
README.md) and driven against the same stubs in record mode with faults scheduled. Its adapters and the
stubs may disagree on wire shapes and some statuses; the driver therefore
judges the gateway there against an independent oracle: for every recorded raw response it recomputes
the expected outcome from the shipped live.py STATUS_CODES, the shipped contracts and manual/types.md and
the shipped live.py function itself, and requires the gateway's outcome to equal it.

Independent oracles: the canonical request hash recomputed here (FORMAT.md section 3), sha256 of the
request bodies as logged by the stubs, `cassette_tool.py verify` (benchmark/stubs), the declared
failure sets read from contracts.json, durations from the ledger's host fields, and strace.

Prints one line per clause, `CLAUSE <req.ac> PASS|FAIL <evidence>`, and exits 0 only when every clause
passes. Honours CARGO_TARGET_DIR (default: the workspace target dir). Python 3.12, standard library only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parents[3]
REFWORLD = REPO / "examples/refworld"
STUBS = REPO / "benchmark/stubs"
CLAUSES = ["1.3", "7.1", "7.2", "7.3", "7.4", "7.5", "7.6"]
TIMEOUT = 900
ENDPOINT_PATHS = {
    "search.query": "/v1/query",
    "publish.post": "/v1/post",
    "publish.status": "/v1/status",
    "publish.delete": "/v1/delete",
}
SERVER_OF = {
    "search.query": "search",
    "publish.post": "publish",
    "publish.status": "publish",
    "publish.delete": "publish",
}


class Results:
    def __init__(self) -> None:
        self.ok: dict[str, list[str]] = {c: [] for c in CLAUSES}
        self.bad: dict[str, list[str]] = {c: [] for c in CLAUSES}

    def check(self, clause: str, condition, evidence: str) -> bool:
        (self.ok if condition else self.bad)[clause].append(evidence)
        return bool(condition)


R = Results()


def run(cmd, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(c) for c in cmd], capture_output=True, text=True, timeout=TIMEOUT, **kw
    )


def target_dir() -> Path:
    env = os.environ.get("CARGO_TARGET_DIR")
    return Path(env) if env else REPO / "target"


SEAL_TEST: list = [False, "not run"]


def build_binaries() -> tuple[Path, bool, str]:
    env = dict(os.environ)
    env.setdefault("CARGO_TARGET_DIR", str(target_dir()))
    b = run(["cargo", "build", "-p", "cwr-cli", "-p", "cwr-kernel"], cwd=REPO, env=env)
    if b.returncode != 0:
        print(b.stderr[-4000:], file=sys.stderr)
        raise SystemExit("cargo build failed")
    t = run(["cargo", "test", "-p", "cwr-kernel"], cwd=REPO, env=env)
    out = t.stdout + t.stderr
    passed = sum(int(n) for n in re.findall(r"test result: ok\. (\d+) passed", out))
    gw = len(re.findall(r"test gateway::\S+ \.\.\. ok", out))
    seal = (
        "test gateway::tests::seal_network_denies_sockets_to_the_process_and_its_descendants ... ok"
    )
    SEAL_TEST[:] = [
        seal in out,
        f"cargo test -p cwr-kernel: {seal if seal in out else 'seal test missing or failed'}",
    ]
    return (
        target_dir() / "debug" / "cwr",
        t.returncode == 0 and gw > 0,
        f"cargo test -p cwr-kernel rc={t.returncode} passed={passed} (gateway unit tests ok={gw})",
    )


# ------------------------------------------------------------------------------------------------
# Independent oracles
# ------------------------------------------------------------------------------------------------


def canonical(v) -> str:
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def request_hash(adapter: str, params: dict) -> str:
    """benchmark/cassettes/FORMAT.md section 3, recomputed independently."""
    return "sha256:" + sha_hex(canonical({"adapter": adapter, "params": params}).encode())


# ------------------------------------------------------------------------------------------------
# Stub servers and listeners
# ------------------------------------------------------------------------------------------------


class Stub:
    def __init__(self, name: str, seed: int, scratch: Path) -> None:
        self.name = name
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-I",
                str(STUBS / f"{name}_server.py"),
                "--port",
                "0",
                "--seed",
                str(seed),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            cwd=scratch,
        )
        line = self.proc.stdout.readline()
        ready = json.loads(line)
        if ready.get("event") != "ready":
            raise SystemExit(f"stub {name} did not start: {line}")
        self.port = ready["port"]
        self.url = f"http://127.0.0.1:{self.port}"

    def _req(self, method: str, path: str, body: dict | None = None) -> dict:
        data = None if body is None else canonical(body).encode()
        r = urllib.request.Request(
            self.url + path, data=data, method=method, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(r, timeout=10) as resp:
            return json.loads(resp.read())

    def log(self, instance: str | None = None) -> list[dict]:
        entries = self._req("GET", "/_stub/log")["requests"]
        return [e for e in entries if instance is None or e["instance"] == instance]

    def schedule(self, faults: list[dict], allow_undeclared: bool = True) -> None:
        self._req(
            "PUT",
            "/_stub/schedule",
            {
                "format": "cwr.stub.schedule",
                "schema_version": 1,
                "allow_undeclared": allow_undeclared,
                "faults": faults,
            },
        )

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


class Listener:
    """A TCP listener that only counts connections: a destination nothing may contact."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.count = 0
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.count += 1
            c.close()


# ------------------------------------------------------------------------------------------------
# The world under test
# ------------------------------------------------------------------------------------------------

# The network probe placed in lib/ and live.py. Three attempts, each recorded in a report file when the
# filesystem allows it: a direct socket; a child process started through _posixsubprocess.fork_exec,
# which raises no audit event (so an interpreter-level guard alone cannot see it), whose child connects;
# and the kernel's view of the process (/proc/self/status Seccomp and NoNewPrivs).
PROBE_FN = """def _probe_network() -> None:
    b = __builtins__ if isinstance(__builtins__, dict) else vars(__builtins__)
    imp = b["__imp" + "ort__"]
    notes = []
    try:
        s = imp("so" + "cket").create_connection(("127.0.0.1", __PROBE_PORT__), 0.3)
        s.close()
        notes.append("socket=connected")
    except BaseException as e:
        notes.append("socket=" + type(e).__name__)
    try:
        ps = imp("_posix" + "subprocess")
        o = imp("o" + "s")
        exe = imp("sy" + "s").executable.encode()
        r, w = o.pipe()
        cmd = "import socket;socket.create_connection(('127.0.0.1', __PROBE_PORT__), 1).close()"
        pid = ps.fork_exec([exe, b"-c", cmd.encode()], [exe], True, (), None, None, -1, -1, -1, -1, -1, -1, r, w, False, False, -1, None, None, None, -1, None, False)
        o.waitpid(pid, 0)
        notes.append("fork_exec=spawned")
    except BaseException as e:
        notes.append("fork_exec=" + type(e).__name__)
    try:
        st = b["op" + "en"]("/proc/self/status").read()
        notes += [l.replace("\\t", "").replace(" ", "") for l in st.splitlines() if l.startswith(("Seccomp:", "NoNewPrivs:"))]
    except BaseException as e:
        notes.append("status=" + type(e).__name__)
    try:
        with b["op" + "en"]("__PROBE_REPORT__", "a") as f:
            f.write(" ".join(notes) + "\\n")
    except BaseException:
        pass
"""


def probe_code(port: int, report: Path) -> str:
    return PROBE_FN.replace("__PROBE_PORT__", str(port)).replace("__PROBE_REPORT__", str(report))


def probe_report(report: Path) -> list[str]:
    return report.read_text().splitlines() if report.exists() else []


LIB_PROBE = (
    "\n\n"
    + PROBE_FN
    + """

def version(state: Any) -> str:"""
)

UNDECLARED_CASE = """
[case.undeclared_status]
world_time = 0
state = {}
prior = []
params = {"key": "k-u", "title": "Title", "body": "b"}
expect = "__EXPECT__"
precondition = null
request_hash = "__HASH__"
sequence = 1
response = {"status": 418, "body": {}}
"""


def diagnostics(rep: dict) -> list[str]:
    return [
        str(d.get("message")) for st in rep.get("stages", []) for d in st.get("diagnostics", [])
    ]


class World:
    def __init__(self, cwr: Path, scratch: Path) -> None:
        self.cwr = cwr
        self.scratch = scratch
        self.home = scratch / "home"
        self.n = 0
        self.stubs: dict[str, str] = {}
        self.src: Path | None = None

    def prepare(
        self,
        probe_port: int,
        name: str = "src",
        undeclared_expect: str = "INVALID",
        status_fallback: bool = False,
        retry_ratio: int | None = None,
    ) -> Path:
        """The aligned copy of refworld. `undeclared_expect`: the code the added fixture case expects for
        an undeclared status of publish.post; `status_fallback`: also give publish.status (which does not
        declare INVALID) `undeclared_status = "INVALID"`, which the compiler must refuse; `retry_ratio`:
        give search.query and publish.post `retry_after_ratio = <n>`. The probe report of this copy is
        `probe-<name>.txt` in the scratch directory."""
        src = self.scratch / name
        shutil.copytree(REFWORLD, src, ignore=shutil.ignore_patterns("README.md", "__pycache__"))
        # The shipped adapters speak the stub wire protocol; only the network probe is added,
        # called first thing in publish.delete.
        report = self.scratch / f"probe-{name}.txt"
        pl = src / "adapters/publish/live.py"
        t = pl.read_text()
        anchor = "\n\ndef delete(gateway: Any, post_id: str) -> None:\n"
        if t.count(anchor) != 1:
            raise SystemExit("could not place the network probe in adapters/publish/live.py")
        t = t.replace(
            anchor, "\n\n" + probe_code(probe_port, report).rstrip("\n") + "\n" + anchor, 1
        )
        t = re.sub(
            r"(def delete\(gateway: Any, post_id: str\) -> None:\n(?:\s+\"\"\".*?\"\"\"\n)?)",
            r"\1    _probe_network()\n",
            t,
            count=1,
            flags=re.S,
        )
        pl.write_text(t)
        lib = src / "lib/world.py"
        s = lib.read_text()
        old = "\n\ndef version(state: Any) -> str:"
        if s.count(old) != 1:
            raise SystemExit("could not place the network probe in lib/world.py")
        s = s.replace(
            old,
            LIB_PROBE.replace("__PROBE_PORT__", str(probe_port)).replace(
                "__PROBE_REPORT__", str(report)
            ),
            1,
        )
        s = re.sub(
            r"(def version\(state: Any\) -> str:\n(?:\s+\"\"\".*?\"\"\"\n)?)",
            r"\1    _probe_network()\n",
            s,
            count=1,
            flags=re.S,
        )
        lib.write_text(s)
        # The undeclared-status fallback is a contract rule (world-source.md section 7).
        kvx = src / "adapters/publish.kvx"
        k = kvx.read_text()
        for section in ["[adapter.publish.post]"] + (
            ["[adapter.publish.status]"] if status_fallback else []
        ):
            at = k.index("content_guaranteed = false\n", k.index(section))
            k = k[:at] + 'undeclared_status = "INVALID"\n' + k[at:]
        kvx.write_text(k)
        if retry_ratio is not None:
            for path, section in (
                ("adapters/publish.kvx", "[adapter.publish.post]"),
                ("adapters/search.kvx", "[adapter.search.query]"),
            ):
                f = src / path
                k = f.read_text()
                at = k.index("content_guaranteed = false\n", k.index(section))
                f.write_text(k[:at] + f"retry_after_ratio = {retry_ratio}\n" + k[at:])
        fx = src / "fixtures/publish/post.kvx"
        case = UNDECLARED_CASE.replace("__EXPECT__", undeclared_expect).replace(
            "__HASH__", request_hash("publish.post", {"key": "k-u", "title": "Title", "body": "b"})
        )
        fx.write_text(fx.read_text().rstrip("\n") + "\n" + case)
        return src

    def compile(
        self,
        src: Path,
        out: str = "builds",
        env: dict | None = None,
        strace_out: Path | None = None,
    ) -> tuple[int, dict, str]:
        cmd = [self.cwr, "build", src, "--json", "--out", self.scratch / out]
        if strace_out is not None:
            cmd = [
                "strace",
                "-f",
                "-qq",
                "-e",
                "trace=execve,connect,prctl",
                "-o",
                strace_out,
                *cmd,
            ]
        p = run(cmd, env=None if env is None else {**os.environ, **env})
        rep = json.loads(p.stdout) if p.stdout.strip().startswith("{") else {}
        return p.returncode, rep, p.stderr

    def build(self, probe_port: int) -> Path:
        src = self.prepare(probe_port)
        self.src = src
        rc, rep, err = self.compile(src)
        b = rep.get("build") or {}
        if rc != 0 or not b.get("dir"):
            raise SystemExit(
                f"cwr build failed ({rc}): {rep.get('summary')} {diagnostics(rep)[:6]} {err[-800:]}"
            )
        return Path(b["dir"])

    def create(self, build: Path, seed: int) -> str:
        p = run([self.cwr, "instance", "create", "--home", self.home, build, "--seed", str(seed)])
        if p.returncode != 0:
            raise SystemExit(f"instance create failed: {p.stderr}")
        return p.stdout.strip()

    def turn(
        self,
        iid: str,
        script: str,
        mode: str = "live",
        cassette: Path | None = None,
        stubs: dict | None = None,
        strace_out: Path | None = None,
        extra: list | None = None,
    ) -> tuple[int, dict | None, str, float]:
        self.n += 1
        f = self.scratch / f"turn-{self.n}.py"
        f.write_text(script)
        cmd = [
            self.cwr,
            "instance",
            "turn",
            iid,
            "--home",
            self.home,
            "--script",
            f,
            "--gateway",
            mode,
        ]
        for name, url in (self.stubs if stubs is None else stubs).items():
            cmd += ["--stub", f"{name}={url}"]
        if cassette is not None:
            cmd += ["--cassette", cassette]
        cmd += extra or []
        if strace_out is not None:
            cmd = [
                "strace",
                "-f",
                "-qq",
                "-e",
                "trace=socket,socketpair,connect",
                "-o",
                strace_out,
                *cmd,
            ]
        t0 = time.monotonic()
        p = run(cmd)
        took = time.monotonic() - t0
        env = None
        if p.returncode == 0:
            try:
                env = json.loads(p.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                env = None
        return p.returncode, env, p.stderr, took

    def ledger(self, iid: str) -> list[dict]:
        return [
            json.loads(ln)
            for ln in (self.home / "instances" / iid / "ledger.jsonl").read_text().splitlines()
            if ln.strip()
        ]

    def adapter_records(self, iid: str, turn: int | None = None) -> list[dict]:
        return [
            r
            for r in self.ledger(iid)
            if r["kind"] == "adapter" and (turn is None or r["turn"] == turn)
        ]


PRE = """import cwr, search, publish, world
def call(f, **kw):
    try:
        return {"ok": f(**kw)}
    except cwr.errors.WorldError as e:
        return {"code": e.code, "retry_after": e.retry_after, "detail": e.detail}
"""


def script(calls: list[tuple[str, str, dict]], tail: str = "") -> str:
    """calls: (label, adapter, params). Returns {label: outcome}."""
    lines = [PRE, "r = {}"]
    for label, adapter, params in calls:
        args = ", ".join(f"{k}={v!r}" for k, v in params.items())
        lines.append(f"r[{label!r}] = call({adapter}, {args})")
    lines.append(tail)
    lines.append("return r")
    return "\n".join(lines) + "\n"


def code(o: dict | None) -> str | None:
    if not isinstance(o, dict):
        return "missing"
    return None if "ok" in o else o.get("code")


def contracts(build: Path) -> dict[str, dict]:
    c = json.loads((build / "contracts.json").read_text())
    return {p["name"]: p for p in c["primitives"] + c["adapters"]}


def exact_traffic(
    world: World,
    stubs: dict[str, Stub],
    iid: str,
    calls: list[tuple[str, str, dict]],
    before: dict[str, int],
) -> tuple[bool, str]:
    """The stubs saw exactly the requests the ledger records as service traffic: same count, same order,
    same body bytes per server, and nothing else (no retries)."""
    params_of = {request_hash(a, p): (a, p) for _, a, p in calls}
    want: dict[str, list[tuple[str, str]]] = {"search": [], "publish": []}
    for rec in world.adapter_records(iid):
        g = rec.get("gateway") or {}
        if g.get("via") != "service":
            continue
        if g.get("request_hash") not in params_of:
            continue
        a, p = params_of[g["request_hash"]]
        want[SERVER_OF[a]].append((ENDPOINT_PATHS[a], sha_hex(canonical(p).encode())))
    ok = True
    seen = 0
    for name in ("search", "publish"):
        log = stubs[name].log(iid)[before.get(name, 0) :]
        got = [(e["path"], e["request_sha256"]) for e in log]
        seen += len(got)
        if got != want[name]:
            ok = False
    return (
        ok,
        f"{seen} stub requests == {sum(len(v) for v in want.values())} ledger service records, in order, byte-identical bodies",
    )


# ------------------------------------------------------------------------------------------------
# Scenarios
# ------------------------------------------------------------------------------------------------


def failure_paths(
    world: World, build: Path, stubs: dict[str, Stub], probe: Listener, decoy: Listener
) -> None:
    """Live mode: every declared failure of search.query and publish.post, publish.status and publish.delete,
    undeclared statuses, malformed bodies, transport errors, timeouts, rate limits, idempotency."""
    cmap = contracts(build)
    iid = world.create(build, 11)
    faults = [
        {"adapter": "search.query", "instance": iid, "index": 1, "fault": "UNAVAILABLE"},
        {"adapter": "search.query", "instance": iid, "index": 2, "fault": "RESET"},
        {"adapter": "search.query", "instance": iid, "index": 3, "fault": "MALFORMED"},
        {
            "adapter": "search.query",
            "instance": iid,
            "index": 4,
            "fault": "RATE_LIMITED",
            "retry_after": 25,
        },
        {
            "adapter": "search.query",
            "instance": iid,
            "index": 5,
            "fault": "REJECTED",
            "reason": "undeclared for search",
        },
        {
            "adapter": "search.query",
            "instance": iid,
            "index": 6,
            "fault": "TIMEOUT",
            "delay_ms": 13000,
        },
        {"adapter": "publish.post", "instance": iid, "index": 1, "fault": "UNAVAILABLE"},
        {"adapter": "publish.post", "instance": iid, "index": 3, "fault": "MALFORMED"},
        {
            "adapter": "publish.post",
            "instance": iid,
            "index": 4,
            "fault": "RATE_LIMITED",
            "retry_after": 13,
        },
        {
            "adapter": "publish.post",
            "instance": iid,
            "index": 5,
            "fault": "REJECTED",
            "reason": "undeclared for post",
        },
        {"adapter": "publish.post", "instance": iid, "index": 6, "fault": "RESET"},
        {
            "adapter": "publish.post",
            "instance": iid,
            "index": 7,
            "fault": "TIMEOUT",
            "delay_ms": 15000,
        },
        {"adapter": "publish.status", "instance": iid, "index": 3, "fault": "MALFORMED"},
        {
            "adapter": "publish.status",
            "instance": iid,
            "index": 4,
            "fault": "REJECTED",
            "reason": "undeclared for status",
        },
        {
            "adapter": "publish.status",
            "instance": iid,
            "index": 5,
            "fault": "TIMEOUT",
            "delay_ms": 12000,
        },
    ]
    for s in stubs.values():
        s.schedule(faults, allow_undeclared=True)
    before = {n: len(s.log(iid)) for n, s in stubs.items()}
    all_calls: list[tuple[str, str, dict]] = []
    outcomes: dict[str, dict] = {}

    # Turn 1: search.query.
    t1 = [
        ("s_pre_invalid", "search.query", {"q": "", "limit": 5}),
        ("s_pre_limit", "search.query", {"q": "tennis", "limit": 51}),
        ("s_ok", "search.query", {"q": "tennis", "limit": 3}),
        ("s_503", "search.query", {"q": "tennis", "limit": 3}),
        ("s_reset", "search.query", {"q": "golf", "limit": 3}),
        ("s_malformed", "search.query", {"q": "golf", "limit": 3}),
        ("s_ratelimited", "search.query", {"q": "golf", "limit": 3}),
        ("s_undeclared", "search.query", {"q": "golf", "limit": 3}),
        ("s_timeout", "search.query", {"q": "golf", "limit": 3}),
    ]
    # Turn 2: publish.post.
    t2 = [
        ("p_ok", "publish.post", {"key": "k1", "title": "T", "body": "hello"}),
        ("p_replay", "publish.post", {"key": "k1", "title": "T", "body": "hello"}),
        ("p_dup_store", "publish.post", {"key": "k1", "title": "T", "body": "other"}),
        ("p_pre_invalid", "publish.post", {"key": "k2", "title": "x" * 201, "body": "b"}),
        ("p_503", "publish.post", {"key": "k3", "title": "T", "body": "b"}),
        ("p_after_503", "publish.post", {"key": "k3", "title": "T", "body": "b"}),
        ("p_fresh", "publish.post", {"key": "k3b", "title": "T", "body": "b"}),
        ("p_malformed", "publish.post", {"key": "k4", "title": "T", "body": "b"}),
        ("p_ratelimited", "publish.post", {"key": "k5", "title": "T", "body": "b"}),
        ("p_undeclared", "publish.post", {"key": "k6", "title": "T", "body": "b"}),
        ("p_reset", "publish.post", {"key": "k7", "title": "T", "body": "b"}),
        ("p_timeout", "publish.post", {"key": "k8", "title": "T", "body": "b"}),
        ("p_dup_after_timeout", "publish.post", {"key": "k8", "title": "T", "body": "changed"}),
        ("p_repeat_timeout", "publish.post", {"key": "k8", "title": "T", "body": "b"}),
        ("p_repeat_rl", "publish.post", {"key": "k5", "title": "T", "body": "b"}),
        ("p_natural_rl", "publish.post", {"key": "k9", "title": "T", "body": "b"}),
    ]
    for calls in (t1, t2):
        rc, env, err, _took = world.turn(iid, script(calls))
        all_calls += calls
        if env is None:
            R.check("7.2", False, f"turn failed rc={rc} {err[-400:]}")
            return
        outcomes.update(env["returned"])
    receipt = outcomes["p_ok"].get("ok") or {}
    pid = receipt.get("id", "none")
    # Turn 3: publish.status (natural) and publish.delete.
    t3 = [
        ("st_ok", "publish.status", {"post_id": pid}),
        ("st_ok2", "publish.status", {"post_id": pid}),
        ("st_notfound", "publish.status", {"post_id": "no-such-post"}),
        ("st_malformed", "publish.status", {"post_id": pid}),
        ("st_undeclared", "publish.status", {"post_id": pid}),
        ("st_timeout", "publish.status", {"post_id": pid}),
        ("del_ok", "publish.delete", {"post_id": pid}),
        ("del_notfound", "publish.delete", {"post_id": pid}),
    ]
    # Turn 4: the stub's own search rate limit (10 accepted per 50 ticks), then a world primitive that
    # runs lib/ (world.version, carrying the network probe).
    t4 = [(f"q{i}", "search.query", {"q": f"topic {i}", "limit": 2}) for i in range(11)]
    for calls, tail in ((t3, ""), (t4, "r['version'] = world.version()")):
        rc, env, err, _took = world.turn(iid, script(calls, tail))
        all_calls += calls
        if env is None:
            R.check("7.2", False, f"turn failed rc={rc} {err[-400:]}")
            return
        outcomes.update(env["returned"])
    # Turn 5: the key store holds across turns: identical replay of k1 sends nothing.
    t5 = [("p_replay_turn5", "publish.post", {"key": "k1", "title": "T", "body": "hello"})]
    rc, env, err, _took = world.turn(iid, script(t5))
    all_calls += t5
    outcomes.update((env or {}).get("returned") or {})

    recs = world.adapter_records(iid)
    by_label: dict[str, dict] = {}
    # Map ledger adapter records to labels in call order (every call produces exactly one record).
    labels = [ln for ln, _, _ in all_calls]
    if len(recs) == len(labels):
        by_label = dict(zip(labels, recs, strict=False))
    R.check(
        "7.3",
        len(recs) == len(labels),
        f"one ledger record per adapter call ({len(recs)} records for {len(labels)} calls)",
    )

    expect = {
        "s_pre_invalid": "INVALID",
        "s_pre_limit": "LIMIT",
        "s_ok": None,
        "s_503": "UNAVAILABLE",
        "s_reset": "UNAVAILABLE",
        "s_malformed": "INVALID",
        "s_ratelimited": "RATE_LIMITED",
        "s_undeclared": "UNAVAILABLE",
        "s_timeout": "TIMEOUT",
        "p_ok": None,
        "p_replay": None,
        "p_dup_store": "DUPLICATE_KEY",
        "p_pre_invalid": "INVALID",
        "p_503": "UNAVAILABLE",
        "p_after_503": "UNAVAILABLE",
        "p_fresh": None,
        "p_malformed": "INVALID",
        "p_ratelimited": "RATE_LIMITED",
        "p_undeclared": "INVALID",
        "p_reset": "UNAVAILABLE",
        "p_timeout": "TIMEOUT",
        "p_dup_after_timeout": "DUPLICATE_KEY",
        "p_repeat_timeout": "TIMEOUT",
        "p_repeat_rl": "RATE_LIMITED",
        "p_natural_rl": "RATE_LIMITED",
        "st_ok": None,
        "st_ok2": None,
        "st_notfound": "NOT_FOUND",
        "st_malformed": "UNAVAILABLE",
        "st_undeclared": "UNAVAILABLE",
        "st_timeout": "TIMEOUT",
        "del_ok": None,
        "del_notfound": "NOT_FOUND",
        "p_replay_turn5": None,
    }
    expect.update({f"q{i}": None for i in range(10)})
    expect["q10"] = "RATE_LIMITED"
    wrong = {k: (code(outcomes.get(k)), v) for k, v in expect.items() if code(outcomes.get(k)) != v}
    R.check(
        "7.2",
        not wrong,
        f"{len(expect)} adapter outcomes equal the expected normalised codes"
        + (f"; wrong: {wrong}" if wrong else ""),
    )

    # Every adapter outcome is a declared code; nothing undeclared, no INVARIANT_VIOLATION.
    bad = []
    for rec in recs:
        c = cmap[rec["primitive"]]
        allowed = set(c["failures"]) | {pc[1] for pc in c["preconditions"]}
        cd = rec["outcome"].get("code")
        if cd is not None and cd not in allowed:
            bad.append((rec["primitive"], cd))
    R.check(
        "7.2",
        not bad,
        f"all {len(recs)} adapter ledger outcomes are declared codes of their contract (undeclared: {bad})",
    )
    reached = {
        a: {code(outcomes[ln]) for ln, aa, _ in all_calls if aa == a} - {None}
        for a in ("search.query", "publish.post")
    }
    for a in ("search.query", "publish.post"):
        R.check(
            "7.2",
            set(cmap[a]["failures"]) <= reached[a],
            f"every declared failure of {a} reached through the gateway: {sorted(reached[a])} ⊇ {sorted(cmap[a]['failures'])}",
        )
    R.check(
        "7.2",
        code(outcomes["s_503"]) == "UNAVAILABLE" and code(outcomes["p_503"]) == "UNAVAILABLE",
        "a stub 503 maps only to the declared code UNAVAILABLE (search.query and publish.post)",
    )
    R.check(
        "7.2",
        code(outcomes["s_undeclared"]) == "UNAVAILABLE"
        and code(outcomes["p_undeclared"]) == "INVALID"
        and code(outcomes["st_undeclared"]) == "UNAVAILABLE",
        f"an undeclared status (HTTP 422) maps to the contract's undeclared_status: UNAVAILABLE (default) for search.query and publish.status, INVALID for publish.post "
        f"(contracts.json undeclared_status: post={cmap['publish.post'].get('undeclared_status')}, status={cmap['publish.status'].get('undeclared_status')})",
    )
    R.check(
        "7.2",
        cmap["publish.post"].get("undeclared_status") == "INVALID"
        and "undeclared_status" not in cmap["publish.status"]
        and "undeclared_status" not in cmap["search.query"],
        "the fallback is read from the compiled contract (adapters/publish.kvx undeclared_status), not from adapter code",
    )
    R.check(
        "7.2",
        code(outcomes["s_malformed"]) == "INVALID"
        and code(outcomes["p_malformed"]) == "INVALID"
        and code(outcomes["st_malformed"]) == "UNAVAILABLE",
        "a malformed body maps to INVALID where declared (search.query, publish.post) and to UNAVAILABLE where INVALID is undeclared (publish.status), never to an undeclared code",
    )
    R.check(
        "7.2",
        code(outcomes["s_reset"]) == "UNAVAILABLE"
        and code(outcomes["p_reset"]) == "UNAVAILABLE"
        and "transport" in outcomes["s_reset"].get("detail", ""),
        f"a transport error (connection reset) maps to UNAVAILABLE: {outcomes['s_reset'].get('detail', '')[:80]!r}",
    )
    ok_shapes = (
        all(
            isinstance(outcomes[ln]["ok"], list)
            and all(set(h) == {"rank", "title", "url", "snippet"} for h in outcomes[ln]["ok"])
            for ln in ["s_ok"] + [f"q{i}" for i in range(10)]
        )
        and set(outcomes["p_ok"].get("ok") or {}) == {"id", "key", "accepted_at"}
        and set(outcomes["st_ok"].get("ok") or {}) == {"id", "key", "state", "accepted_at"}
        and outcomes["del_ok"].get("ok", 0) is None
    )
    R.check(
        "7.2",
        ok_shapes,
        "ok outcomes carry exactly the declared return shapes (list[SearchHit], PostReceipt, PostStatus, None)",
    )

    # 7.3: no retries; RATE_LIMITED informs the model with retry_after in ticks.
    ok, ev = exact_traffic(world, stubs, iid, all_calls, before)
    R.check(
        "7.3",
        ok,
        f"live failure-path instance: {ev}; each 503, reset, malformed, rate-limited and timed-out call appears once",
    )
    ra = (
        outcomes["s_ratelimited"].get("retry_after"),
        outcomes["p_ratelimited"].get("retry_after"),
        outcomes["q10"].get("retry_after"),
        outcomes["p_natural_rl"].get("retry_after"),
    )
    rl_recs = [
        by_label.get(ln, {}).get("retry_after")
        for ln in ("s_ratelimited", "p_ratelimited", "q10", "p_natural_rl")
    ]
    R.check(
        "7.3",
        ra[0] == 25
        and ra[1] == 13
        and all(isinstance(x, int) and x > 0 for x in ra)
        and list(ra) == rl_recs,
        f"RATE_LIMITED carries retry_after in ticks to the script and the ledger (scheduled 25 and 13; stub windows {ra[2]} and {ra[3]}); the gateway did not wait or retry",
    )
    manual = (build / "manual.md").read_text()
    R.check(
        "7.3",
        "retry_after" in manual and re.search(r"RATE_LIMITED[^\n]*retry_after", manual) is not None,
        "the manual tells the model that RATE_LIMITED carries retry_after in ticks",
    )

    # 7.4: idempotency.
    rec = receipt
    store_recs = [
        by_label.get(ln, {}).get("gateway", {}).get("via")
        for ln in ("p_replay", "p_dup_store", "p_dup_after_timeout", "p_replay_turn5")
    ]
    R.check(
        "7.4",
        outcomes["p_replay"].get("ok") == rec
        and outcomes["p_replay_turn5"].get("ok") == rec
        and store_recs == ["store"] * 4,
        f"key adapter: identical replay returns the original receipt {rec} from the store without contacting the service (same turn and a later turn; ledger via={store_recs})",
    )
    dup = outcomes["p_dup_store"].get("detail", "")
    R.check(
        "7.4",
        code(outcomes["p_dup_store"]) == "DUPLICATE_KEY" and canonical(rec) in dup,
        "key adapter: a reused key with a different payload is DUPLICATE_KEY with the original receipt in detail, nothing sent",
    )

    # The first outcome is stored whatever it is (kernel.md section 7; clause 7.4 "the original outcome").
    def sent_bodies(params: dict) -> int:
        h = sha_hex(canonical(params).encode())
        return sum(
            1
            for e in stubs["publish"].log(iid)
            if e["path"] == "/v1/post" and e["request_sha256"] == h
        )

    k3, k5, k8 = (
        {"key": "k3", "title": "T", "body": "b"},
        {"key": "k5", "title": "T", "body": "b"},
        {"key": "k8", "title": "T", "body": "b"},
    )
    vias = {
        ln: by_label.get(ln, {}).get("gateway", {}).get("via")
        for ln in (
            "p_503",
            "p_after_503",
            "p_ratelimited",
            "p_repeat_rl",
            "p_timeout",
            "p_repeat_timeout",
            "p_dup_after_timeout",
        )
    }
    R.check(
        "7.4",
        code(outcomes["p_503"]) == "UNAVAILABLE"
        and code(outcomes["p_after_503"]) == "UNAVAILABLE"
        and vias["p_503"] == "service"
        and vias["p_after_503"] == "store"
        and sent_bodies(k3) == 1
        and code(outcomes["p_fresh"]) is None,
        f"a key call that failed with UNAVAILABLE (stub 503) stored that outcome: its identical repeat returned UNAVAILABLE from the store (via {vias['p_after_503']}) and the stub saw the k3 request {sent_bodies(k3)} time; a fresh key k3b succeeded",
    )
    R.check(
        "7.4",
        code(outcomes["p_ratelimited"]) == "RATE_LIMITED"
        and outcomes["p_ratelimited"].get("retry_after") == 13
        and vias["p_ratelimited"] == "service"
        and vias["p_repeat_rl"] == "service"
        and sent_bodies(k5) == 2
        and "stored first outcome" not in outcomes["p_repeat_rl"].get("detail", ""),
        f"a RATE_LIMITED outcome is not stored as the key's outcome (the service declined to process it): the identical repeat was sent to the service again "
        f"(via {vias['p_repeat_rl']}, k5 sent {sent_bodies(k5)} times, answered {code(outcomes['p_repeat_rl'])} by the stub's own window)",
    )
    dto = outcomes["p_dup_after_timeout"].get("detail", "")
    R.check(
        "7.4",
        code(outcomes["p_timeout"]) == "TIMEOUT"
        and code(outcomes["p_repeat_timeout"]) == "TIMEOUT"
        and vias["p_repeat_timeout"] == "store"
        and code(outcomes["p_dup_after_timeout"]) == "DUPLICATE_KEY"
        and "original outcome: TIMEOUT" in dto
        and vias["p_dup_after_timeout"] == "store"
        and sent_bodies(k8) == 1
        and sent_bodies({"key": "k8", "title": "T", "body": "changed"}) == 0,
        f"after TIMEOUT the key holds TIMEOUT: the identical repeat answers TIMEOUT from the store, a different payload is DUPLICATE_KEY naming the original outcome ({dto[-70:]!r}); the stub saw k8 once and the changed payload never",
    )
    nat = [by_label.get(ln, {}).get("gateway", {}) for ln in ("st_ok", "st_ok2")]
    R.check(
        "7.4",
        all(g.get("idempotency") == "natural" and g.get("via") == "service" for g in nat)
        and nat[0].get("request_hash")
        == nat[1].get("request_hash")
        == request_hash("publish.status", {"post_id": pid})
        and nat[0].get("response_hash") is not None
        and nat[0].get("response_hash") == nat[1].get("response_hash"),
        "natural adapter: both calls forwarded; the ledger records request and response hashes as idempotency evidence (equal requests, equal responses)",
    )
    none_ev = [by_label.get(ln, {}).get("gateway", {}) for ln in ("s_ok", "s_503")]
    m = re.search(r"#### search\.query\s*```text\n(.*?)```", manual, re.S)
    R.check(
        "7.4",
        m is not None
        and "Not idempotent" in m.group(1)
        and all(g.get("idempotency") == "none" and g.get("via") == "service" for g in none_ev),
        "none adapter: labelled 'Not idempotent' in the manual; identical requests are each forwarded (ledger idempotency none)",
    )

    # 7.6: timeouts in host time from the contract, one tick of world time. Each stall is timeout_ms +
    # 10 s so that `dur < delay` proves the gateway gave up at its deadline rather than waiting for the
    # response, while leaving room for the per-primitive overhead duration_us also covers on a loaded host.
    tick = json.loads((build / "contracts.json").read_text())["clock"]["tick_per_primitive"]
    for label, adapter, delay in (
        ("s_timeout", "search.query", 13000),
        ("p_timeout", "publish.post", 15000),
        ("st_timeout", "publish.status", 12000),
    ):
        r = by_label.get(label, {})
        tmo = cmap[adapter]["timeout_ms"]
        dur = r.get("duration_us", 0) / 1000.0
        idx = recs.index(r) if r in recs else -1
        prev_wt = world.ledger(iid)[r["seq"] - 1]["world_time"] if r else -1
        R.check(
            "7.6",
            r.get("outcome", {}).get("code") == "TIMEOUT"
            and tmo <= dur < delay
            and r["world_time"] - prev_wt == tick
            and idx >= 0,
            f"{adapter}: TIMEOUT after {dur:.0f} ms of host time (contract timeout_ms {tmo}, stub stalled {delay}); world_time advanced by exactly {r.get('world_time', 0) - prev_wt} tick",
        )

    # 1.3: interface, not content.
    executed = [r for r in recs if "gateway" in r]
    cg = (
        executed
        and all(r.get("content_guaranteed") is False for r in executed)
        and not any(r.get("content_guaranteed") is True for r in recs)
    )
    R.check(
        "1.3",
        cg,
        f"every executed adapter ledger record is marked content_guaranteed false ({len(executed)} records; none of {len(recs)} claims content)",
    )

    # 7.1: adapter code and lib/ never reached the network; nothing undeclared was contacted.
    paths = {e["path"] for s in stubs.values() for e in s.log(iid)}
    R.check(
        "7.1",
        paths <= set(ENDPOINT_PATHS.values()),
        f"the stubs saw only declared endpoint paths: {sorted(paths)}",
    )
    R.check(
        "7.1",
        code(outcomes["del_ok"]) is None
        and outcomes.get("version") is not None
        and probe.count == 0,
        f"adapters/publish/live.py (publish.delete) and lib/world.py (world.version) both tried to connect to a probe listener from inside the kernel; it saw {probe.count} connections",
    )
    R.check(
        "7.1",
        decoy.count == 0,
        f"a stub binding the contracts do not name (decoy=...) was never contacted ({decoy.count} connections)",
    )


def closure_and_endpoints(world: World, build: Path, stubs: dict[str, Stub]) -> None:
    """7.1: scripts have no network; only declared, configured endpoints are contacted."""
    iid = world.create(build, 12)
    _rc, env, _err, _ = world.turn(iid, "import socket\nreturn 1\n")
    e = (env or {}).get("error") or {}
    R.check(
        "7.1",
        env is not None
        and e.get("code") == "INVALID"
        and e.get("primitive", "").startswith("sandbox."),
        f"a script importing socket is refused as a closure violation ({e.get('code')} {e.get('primitive')})",
    )
    before = len(stubs["publish"].log())
    calls = [("p", "publish.post", {"key": "u1", "title": "T", "body": "b"})]
    _rc, env, _err, _ = world.turn(iid, script(calls), stubs={"search": stubs["search"].url})
    o = ((env or {}).get("returned") or {}).get("p")
    R.check(
        "7.1",
        code(o) == "UNAVAILABLE"
        and "no address is configured" in (o or {}).get("detail", "")
        and len(stubs["publish"].log()) == before,
        "an adapter whose declared stub server has no configured address is UNAVAILABLE and nothing is sent anywhere else",
    )
    src = REPO / "crates/cwr-kernel/src"
    net = []
    for f in src.rglob("*.rs"):
        if "gateway" in f.relative_to(src).parts:
            continue
        if re.search(r"\b(TcpStream|TcpListener|UdpSocket|ToSocketAddrs)\b", f.read_text()):
            net.append(str(f.relative_to(REPO)))
    R.check(
        "7.1",
        not net,
        f"the kernel's only TCP/UDP code is crates/cwr-kernel/src/gateway/ (other files using it: {net})",
    )


def modes(world: World, build: Path, stubs: dict[str, Stub], scratch: Path) -> None:
    """7.5: live records nothing; record writes a verified cassette; replay reproduces it offline."""
    cas = scratch / "cassette-rec.jsonl"
    rid = world.create(build, 21)
    faults = [
        {"adapter": "search.query", "instance": rid, "index": 1, "fault": "UNAVAILABLE"},
        {
            "adapter": "search.query",
            "instance": rid,
            "index": 2,
            "fault": "TIMEOUT",
            "delay_ms": 3600,
        },
        {
            "adapter": "publish.post",
            "instance": rid,
            "index": 1,
            "fault": "RATE_LIMITED",
            "retry_after": 9,
        },
        {"adapter": "publish.post", "instance": rid, "index": 2, "fault": "MALFORMED"},
        {"adapter": "publish.status", "instance": rid, "index": 1, "fault": "RESET"},
    ]
    for s in stubs.values():
        s.schedule(faults, allow_undeclared=False)
    turns = [
        [
            ("a", "search.query", {"q": "tennis", "limit": 3}),
            ("b", "search.query", {"q": "tennis", "limit": 3}),
            ("c", "search.query", {"q": "tennis", "limit": 3}),
            ("d", "publish.post", {"key": "r1", "title": "Tennis", "body": "x"}),
            ("e", "publish.post", {"key": "r1", "title": "Tennis", "body": "x"}),
            ("f", "publish.post", {"key": "r2", "title": "Golf", "body": "y"}),
        ],
        [
            ("g", "publish.post", {"key": "r3", "title": "Golf", "body": "y"}),
            ("h", "publish.post", {"key": "r1", "title": "Tennis", "body": "z"}),
            ("i", "search.query", {"q": "tennis", "limit": 3}),
            ("j", "publish.status", {"post_id": "__PID__"}),
            ("k", "publish.status", {"post_id": "__PID__"}),
            ("l", "publish.post", {"key": "r2", "title": "Golf", "body": "y"}),
        ],
    ]
    rec_envs = []
    pid = None
    all_calls = []
    for i, calls in enumerate(turns):
        calls = [
            (ln, a, {k: (pid if v == "__PID__" else v) for k, v in p.items()}) for ln, a, p in calls
        ]
        all_calls += calls
        rc, env, err, _ = world.turn(
            rid,
            script(calls),
            mode="record",
            cassette=cas,
            extra=["--cassette-label", "cwr-009/seed-21"],
        )
        if env is None:
            R.check("7.5", False, f"record turn failed rc={rc} {err[-400:]}")
            return
        rec_envs.append(env)
        if i == 0:
            pid = (env["returned"]["d"].get("ok") or {}).get("id")
    got = {**rec_envs[0]["returned"], **rec_envs[1]["returned"]}
    # l repeats f (key r2), which was RATE_LIMITED: the key was released, so l is sent and accepted.
    R.check(
        "7.5",
        [code(got[k]) for k in "abcdefghijkl"]
        == [
            None,
            "UNAVAILABLE",
            "TIMEOUT",
            None,
            None,
            "RATE_LIMITED",
            "INVALID",
            "DUPLICATE_KEY",
            None,
            None,
            "UNAVAILABLE",
            None,
        ],
        f"record mode served real stub traffic: {[code(got[k]) for k in 'abcdefghijkl']}",
    )
    ok, ev = exact_traffic(world, stubs, rid, all_calls, {})
    R.check("7.3", ok, f"record instance: {ev}")
    v = run([sys.executable, "-I", STUBS / "cassette_tool.py", "verify", cas])
    lines = cas.read_text().splitlines()
    head = json.loads(lines[0])
    entries = [json.loads(ln) for ln in lines[1:]]
    service = [
        r for r in world.adapter_records(rid) if (r.get("gateway") or {}).get("via") == "service"
    ]
    keys_ok = [(e["adapter"], e["request_hash"], e["seq"]) for e in entries] == [
        (r["primitive"], r["gateway"]["request_hash"], r["gateway"]["seq"]) for r in service
    ]
    hashes_ok = all(e["request_hash"] == request_hash(e["adapter"], e["params"]) for e in entries)
    seqs = [
        e["seq"]
        for e in entries
        if e["adapter"] == "search.query" and e["params"] == {"q": "tennis", "limit": 3}
    ]
    no_host = not any(k in canonical(entries) for k in (rid, "host_ts", "duration"))
    R.check(
        "7.5",
        v.returncode == 0
        and head["label"] == "cwr-009/seed-21"
        and keys_ok
        and hashes_ok
        and seqs == [0, 1, 2, 3]
        and no_host,
        f"record mode wrote {len(entries)} cassette entries (cassette_tool.py verify rc={v.returncode}), keyed by (adapter, canonical request hash, sequence) exactly as the ledger's service records; the repeated search request has sequences {seqs} across two turns; no store-answered call, Instance id or host field is in the cassette",
    )
    kinds = sorted({e["response"]["kind"] for e in entries})
    R.check(
        "7.5",
        kinds == ["http", "timeout", "transport_error"],
        f"the cassette stores raw responses of every form: {kinds}",
    )

    # Live mode records nothing and refuses a cassette.
    lid = world.create(build, 22)
    live_files_before = sorted(p.name for p in scratch.iterdir())
    rc, env, err, _ = world.turn(lid, script([("a", "search.query", {"q": "tennis", "limit": 3})]))
    rc2, env2, _err2, _ = world.turn(
        lid,
        script([("a", "search.query", {"q": "tennis", "limit": 3})]),
        cassette=scratch / "live-should-not-exist.jsonl",
    )
    inst_files = sorted(
        p.relative_to(world.home / "instances" / lid).as_posix()
        for p in (world.home / "instances" / lid).rglob("*")
        if p.is_file()
    )
    new_scratch = sorted(
        set(p.name for p in scratch.iterdir())
        - set(live_files_before)
        - {f"turn-{world.n - 1}.py", f"turn-{world.n}.py"}
    )
    R.check(
        "7.5",
        env is not None
        and code(env["returned"]["a"]) is None
        and rc2 != 0
        and env2 is None
        and not new_scratch
        and not any("cassette" in f for f in inst_files),
        f"live mode called the stub and wrote no cassette (new files: {new_scratch}); live mode with --cassette is refused (exit {rc2})",
    )

    # Replay: a new Instance with the same seed and scripts, the stubs unreachable, strace watching.
    decoy = Listener()
    offline = {
        "search": f"http://127.0.0.1:{decoy.port}",
        "publish": f"http://127.0.0.1:{decoy.port}",
    }
    pid_ = world.create(build, 21)
    rep_envs = []
    inet = []
    traced = 0
    pidv = None
    for i, calls in enumerate(turns):
        calls = [
            (ln, a, {k: (pidv if v == "__PID__" else v) for k, v in p.items()})
            for ln, a, p in calls
        ]
        so = scratch / f"strace-replay-{i}.txt"
        rc, env, err, _ = world.turn(
            pid_, script(calls), mode="replay", cassette=cas, stubs=offline, strace_out=so
        )
        if env is None:
            R.check("7.5", False, f"replay turn failed rc={rc} {err[-400:]}")
            return
        rep_envs.append(env)
        if i == 0:
            pidv = (env["returned"]["d"].get("ok") or {}).get("id")
        lines = so.read_text().splitlines()
        traced += sum(1 for ln in lines if "socket" in ln)
        inet += [ln for ln in lines if re.search(r"AF_INET6?\b|sin6?_port", ln)]

    def cmp_view(r: dict) -> dict:
        return {
            k: v
            for k, v in r.items()
            if k not in ("host_ts", "duration_us", "instance", "chain", "prev", "id", "hash")
        }

    a = [cmp_view(r) for r in world.ledger(rid) if r["kind"] != "header"]
    b = [cmp_view(r) for r in world.ledger(pid_) if r["kind"] != "header"]
    diff = next(
        (i for i, (x, y) in enumerate(zip(a, b, strict=False)) if canonical(x) != canonical(y)),
        None,
    )
    same_env = all(
        canonical({k: e1[k] for k in ("status", "returned", "error", "trace")})
        == canonical({k: e2[k] for k in ("status", "returned", "error", "trace")})
        for e1, e2 in zip(rec_envs, rep_envs, strict=False)
    )
    R.check(
        "7.5",
        len(a) == len(b) and diff is None and same_env,
        f"replay reproduced the recorded run: {len(b)} ledger records equal field by field except host and identity fields (first divergence: {diff}); result envelopes byte-identical (status, returned, error, trace)",
    )
    R.check(
        "7.5",
        traced > 0 and not inet and decoy.count == 0,
        f"replay opened no network socket: strace observed {traced} socket/socketpair calls of the kernel process tree, {len(inet)} of them AF_INET/AF_INET6; the unreachable endpoint saw {decoy.count} connections",
    )
    so = scratch / "strace-miss.txt"
    rc, env, err, _ = world.turn(
        pid_,
        script(
            [
                ("m", "search.query", {"q": "never recorded", "limit": 3}),
                ("n", "search.query", {"q": "tennis", "limit": 3}),
            ]
        ),
        mode="replay",
        cassette=cas,
        stubs=offline,
        strace_out=so,
    )
    miss = [ln for ln in so.read_text().splitlines() if re.search(r"AF_INET6?\b", ln)]
    o = (env or {}).get("returned") or {}
    R.check(
        "7.5",
        code(o.get("m")) == "UNAVAILABLE"
        and "cassette" in o.get("m", {}).get("detail", "")
        and code(o.get("n")) == "UNAVAILABLE"
        and not miss
        and decoy.count == 0,
        "a replay cassette miss (an unrecorded request, and a fifth tennis request past the four recorded sequences) is UNAVAILABLE without any socket",
    )
    # The independent reference normaliser agrees with the gateway on every recorded entry.
    reqs = scratch / "requests.jsonl"
    reqs.write_text(
        "".join(
            canonical(
                {"adapter": e["adapter"], "params": e["params"], "world_time": e["world_time"]}
            )
            + "\n"
            for e in entries
        )
    )
    p = run(
        [
            sys.executable,
            "-I",
            STUBS / "cassette_tool.py",
            "replay",
            "--cassette",
            cas,
            "--requests",
            reqs,
        ]
    )
    ref = [json.loads(ln) for ln in p.stdout.splitlines() if ln.strip()]
    ref_codes = [r["outcome"]["code"] for r in ref]
    gw_codes = [r["outcome"]["code"] for r in service]
    R.check(
        "7.2",
        p.returncode == 0 and ref_codes == gw_codes,
        f"benchmark/stubs/cassette_tool.py's reference normaliser maps the recorded responses to the same codes as the gateway: {gw_codes}",
    )

    # 1.3: equal visible conditions, equal outcome; content differs and is not claimed.
    def outcome_tuples(iid: str) -> list:
        return [
            (
                r["primitive"],
                r["outcome"].get("status"),
                r["outcome"].get("code"),
                r["outcome"].get("shape"),
                r["outcome"].get("side_effect"),
            )
            for r in world.adapter_records(iid)
        ]

    for s in stubs.values():
        s.schedule([], allow_undeclared=False)
    calls = [
        ("a", "search.query", {"q": "tennis", "limit": 4}),
        ("b", "publish.post", {"key": "d1", "title": "T", "body": "b"}),
        ("c", "search.query", {"q": "", "limit": 4}),
    ]
    i1, i2, i3 = world.create(build, 31), world.create(build, 31), world.create(build, 31)
    e1 = world.turn(i1, script(calls))[1]
    e2 = world.turn(i2, script(calls))[1]
    other = dict(world.stubs, search=stubs["search8"].url)
    e3 = world.turn(i3, script(calls), stubs=other)[1]
    t1, t2, t3 = outcome_tuples(i1), outcome_tuples(i2), outcome_tuples(i3)
    R.check(
        "1.3",
        t1 == t2 == t3 and len(t1) == 3,
        f"three Instances under equal visible conditions give equal outcomes (status, code, shape, side-effect) for every adapter call: {t1}",
    )
    pay1 = (e1 or {}).get("returned", {}).get("a", {}).get("ok")
    pay3 = (e3 or {}).get("returned", {}).get("a", {}).get("ok")
    R.check(
        "1.3",
        pay1 is not None
        and pay3 is not None
        and pay1 != pay3
        and canonical(e1["returned"]) == canonical(e2["returned"]),
        "the payload of search.query differs under a service with another content seed while the outcome is equal: payload equality is not claimed",
    )
    tr = [
        t
        for e in (e1, e2, e3)
        for t in (e or {}).get("trace", [])
        if t["primitive"] in ("search.query", "publish.post")
    ]
    m = re.search(
        r"#### search\.query\s*```text\n(.*?)```", (build / "manual.md").read_text(), re.S
    )
    R.check(
        "1.3",
        tr
        and all(t["content_guaranteed"] is False for t in tr)
        and m is not None
        and "Content is not guaranteed" in m.group(1),
        "every adapter trace item in the result envelope says content_guaranteed false and the manual labels adapter content as not guaranteed",
    )


def compile_time(world: World, build: Path, probe: Listener) -> None:
    """7.1 at build time and 7.2 compiler/gateway agreement on the contract's undeclared_status."""
    tests = json.loads((build / "tests.json").read_text())
    cases = tests.get("cases", [])
    ran = {c["target"] for c in cases if c.get("passed")}
    und = [
        c
        for c in cases
        if c.get("target") == "publish.post" and c.get("case") == "undeclared_status"
    ]
    R.check(
        "7.2",
        len(und) == 1 and und[0].get("got") == "INVALID" and und[0].get("passed") is True,
        f"the compiler's contract tests map an undeclared status (418) of publish.post to the contract's undeclared_status: case undeclared_status got {und[0].get('got') if und else None}",
    )
    neg_src = world.prepare(probe.port, "src-neg-expect", undeclared_expect="UNAVAILABLE")
    rc, rep_, _ = world.compile(neg_src, "builds-neg")
    msgs = diagnostics(rep_)
    hit = [m for m in msgs if "undeclared_status" in m and "INVALID" in m]
    R.check(
        "7.2",
        rc != 0 and not (rep_.get("build") or {}).get("dir") and bool(hit),
        f"negative control: the same world with a fixture expecting UNAVAILABLE for that status is refused by cwr build (exit {rc}: {hit[0][:110] if hit else msgs[:2]})",
    )
    neg2 = world.prepare(probe.port, "src-neg-fallback", status_fallback=True)
    rc2, rep2, _ = world.compile(neg2, "builds-neg2")
    msgs2 = diagnostics(rep2)
    hit2 = [m for m in msgs2 if "undeclared_status" in m and "publish.status" in m]
    R.check(
        "7.2",
        rc2 != 0 and bool(hit2),
        f"an undeclared_status the adapter does not declare in failures (INVALID on publish.status) is refused at compile time, never patched at runtime (exit {rc2}: {hit2[0][:110] if hit2 else msgs2[:2]})",
    )
    main_report = probe_report(world.scratch / "probe-src.txt")
    R.check(
        "7.1",
        probe.count == 0
        and {"world.version", "publish.delete"} <= ran
        and main_report
        and all(
            "socket=PermissionError" in ln and "fork_exec=PermissionError" in ln
            for ln in main_report
        ),
        f"cwr build ran lib/world.py (world.version) and adapters/publish/live.py (publish.delete), both carrying the network probe, in its contract tests "
        f"({len(cases)} cases, three builds); the probe listener saw {probe.count} connections: the compiler's world-code worker has no network; "
        f"the probe's {len(main_report)} reports all say the direct socket and the _posixsubprocess.fork_exec child were refused ({main_report[:1]})",
    )
    # No bubblewrap (CWR_BWRAP names a missing file): the worker's audit hook plus the kernel's seccomp
    # network seal, which an escape past the hook (a process spawned without any audit event) would meet.
    nb = world.prepare(probe.port, "src-no-bwrap")
    trace = world.scratch / "strace-build-no-bwrap.txt"
    rc3, _rep3, _err3 = world.compile(
        nb,
        "builds-no-bwrap",
        env={"CWR_BWRAP": str(world.scratch / "no-such-bwrap")},
        strace_out=trace,
    )
    lines = trace.read_text().splitlines() if trace.exists() else []
    bwrap_exec = [
        ln
        for ln in lines
        if "execve(" in ln and "bwrap" in ln.split("execve(", 1)[1].split(",", 1)[0]
    ]
    py_exec = [
        ln
        for ln in lines
        if "execve(" in ln and "python" in ln.split("execve(", 1)[1].split(",", 1)[0]
    ]
    probe_connects = [ln for ln in lines if "connect(" in ln and f"htons({probe.port})" in ln]
    sealed = [
        ln for ln in lines if "PR_SET_SECCOMP" in ln and "SECCOMP_MODE_FILTER" in ln and "= 0" in ln
    ]
    nb_report = probe_report(world.scratch / "probe-src-no-bwrap.txt")
    nb_ok = bool(nb_report) and all(
        "socket=PermissionError" in ln
        and "fork_exec=PermissionError" in ln
        and "Seccomp:2" in ln
        and "NoNewPrivs:1" in ln
        for ln in nb_report
    )
    R.check(
        "7.1",
        rc3 == 0
        and lines
        and not bwrap_exec
        and py_exec
        and sealed
        and nb_ok
        and not probe_connects
        and probe.count == 0,
        f"without bubblewrap the compiler's worker still has no network: cwr build exit {rc3} ran the probed lib/ and live.py through {len(py_exec)} python execs and 0 bwrap execs; "
        f"strace saw {len(sealed)} successful prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER) before the worker's exec, {len(probe_connects)} connect() calls to the probe port, and the listener {probe.count} connections; "
        f"all {len(nb_report)} probe reports from inside the worker show the socket and the _posixsubprocess.fork_exec child refused and the process under a seccomp filter with no_new_privs ({nb_report[:1]})",
    )
    # The seal itself, without the interpreter guard: processes and grandchildren under it cannot open a socket.
    R.check(
        "7.1",
        SEAL_TEST[0],
        f"the seal holds without any audit hook (a raw socket, a subprocess child and a shell-spawned grandchild all get EPERM; the unsealed control connects): {SEAL_TEST[1]}",
    )


def rate_limit_retry(world: World, build: Path, stubs: dict[str, Stub]) -> None:
    """7.3 and 7.4: the manual's same-key retry after RATE_LIMITED works as documented. The stub's own
    window (three accepted posts per 100 ticks) rate-limits the fourth post; the script waits retry_after
    ticks of world time and repeats the identical call under the same key."""
    for s in stubs.values():
        s.schedule([], allow_undeclared=False)
    iid = world.create(build, 51)
    body = (
        PRE
        + """import world
r = {}
for i in (1, 2, 3):
    r['d%d' % i] = call(publish.post, key='d-%d' % i, title='Digest %d' % i, body='b')
r['d4'] = call(publish.post, key='digest-4', title='Digest 4', body='b')
r['retry_at'] = world.now() + r['d4']['retry_after'] if 'retry_after' in r['d4'] and r['d4']['retry_after'] else -1
r['d4_now'] = call(publish.post, key='digest-4', title='Digest 4', body='b')
while world.now() < r['retry_at']:
    pass
r['d4_after'] = call(publish.post, key='digest-4', title='Digest 4', body='b')
r['d4_again'] = call(publish.post, key='digest-4', title='Digest 4', body='b')
return r
"""
    )
    rc, env, err, _ = world.turn(iid, body)
    if not R.check(
        "7.3",
        env is not None,
        f"rate-limit retry turn ran (rc={rc}) {err[-300:] if env is None else ''}",
    ):
        return
    o = env["returned"]
    recs = world.adapter_records(iid)
    rh = request_hash("publish.post", {"key": "digest-4", "title": "Digest 4", "body": "b"})
    d4 = [r for r in recs if (r.get("gateway") or {}).get("request_hash") == rh]
    vias = [(r.get("gateway") or {}).get("via") for r in d4]
    h = sha_hex(canonical({"key": "digest-4", "title": "Digest 4", "body": "b"}).encode())
    sent = sum(
        1 for e in stubs["publish"].log(iid) if e["path"] == "/v1/post" and e["request_sha256"] == h
    )
    ra0, ra1 = o["d4"].get("retry_after"), o["d4_now"].get("retry_after")
    wt_after = d4[2]["world_time"] if len(d4) > 2 else -1
    R.check(
        "7.3",
        [code(o[k]) for k in ("d1", "d2", "d3", "d4", "d4_now")]
        == [None, None, None, "RATE_LIMITED", "RATE_LIMITED"]
        and isinstance(ra0, int)
        and isinstance(ra1, int)
        and 0 < ra1 < ra0
        and code(o["d4_after"]) is None
        and wt_after >= o["retry_at"],
        f"after RATE_LIMITED (retry_after {ra0} ticks) the model's same-key retry is honoured as the manual says: an immediate identical repeat reached the service and got a fresh "
        f"RATE_LIMITED (retry_after {ra1}), and the identical call made at world_time {wt_after} >= retry_at {o['retry_at']} succeeded",
    )
    R.check(
        "7.4",
        vias == ["service", "service", "service", "store"]
        and sent == 3
        and o["d4_again"].get("ok") == o["d4_after"].get("ok")
        and o["d4_after"].get("ok") is not None,
        f"key adapter: RATE_LIMITED released the key (the service did not process the request), so both repeats were sent (stub saw digest-4 {sent} times; ledger via {vias}); "
        f"the first non-rate-limited outcome is stored and its identical replay returns the same receipt from the store",
    )


def retry_ratio(world: World, stubs: dict[str, Stub], probe: Listener) -> None:
    """7.2: RATE_LIMITED retry_after is converted to ticks by the contract's declared retry_after_ratio."""
    src = world.prepare(probe.port, "src-ratio3", retry_ratio=3)
    rc, rep_, _err = world.compile(src, "builds-ratio3")
    bdir = (rep_.get("build") or {}).get("dir")
    if not R.check(
        "7.2",
        rc == 0 and bdir,
        f"a Build with retry_after_ratio = 3 on search.query and publish.post compiles (exit {rc}) {diagnostics(rep_)[:3]}",
    ):
        return
    build = Path(bdir)
    cmap = contracts(build)
    iid = world.create(build, 61)
    faults = [
        {
            "adapter": "search.query",
            "instance": iid,
            "index": 0,
            "fault": "RATE_LIMITED",
            "retry_after": 7,
        },
        {
            "adapter": "publish.post",
            "instance": iid,
            "index": 0,
            "fault": "RATE_LIMITED",
            "retry_after": 11,
        },
    ]
    for s in stubs.values():
        s.schedule(faults, allow_undeclared=False)
    calls = [
        ("s", "search.query", {"q": "tennis", "limit": 2}),
        ("p", "publish.post", {"key": "ratio-1", "title": "T", "body": "b"}),
    ]
    rc, env, _err, _ = world.turn(iid, script(calls))
    o = (env or {}).get("returned") or {}
    recs = world.adapter_records(iid)
    got = [(code(o.get(ln)), (o.get(ln) or {}).get("retry_after")) for ln in ("s", "p")]
    led = [r.get("retry_after") for r in recs]
    R.check(
        "7.2",
        cmap["search.query"].get("retry_after_ratio") == 3
        and cmap["publish.post"].get("retry_after_ratio") == 3
        and got == [("RATE_LIMITED", 21), ("RATE_LIMITED", 33)]
        and led == [21, 33],
        f"retry_after_ratio = 3 (contracts.json) converts the services' retry_after 7 and 11 to {got} ticks in the script and {led} in the ledger",
    )
    for s in stubs.values():
        s.schedule([], allow_undeclared=False)


def gateway_needs(world: World, probe: Listener) -> None:
    """7.2 / kernel.md section 7: an adapter whose declared failures omit a code the gateway would need is
    refused by the compiler, not patched at runtime."""
    variants = [
        (
            "src-need-timeout",
            "adapters/search.kvx",
            '"UNAVAILABLE", "TIMEOUT"]',
            '"UNAVAILABLE"]',
            "search.query: failures lacks TIMEOUT",
        ),
        (
            "src-need-dupkey",
            "adapters/publish.kvx",
            '["INVALID", "DUPLICATE_KEY", "RATE_LIMITED"',
            '["INVALID", "RATE_LIMITED"',
            "publish.post: failures lacks DUPLICATE_KEY",
        ),
        (
            "src-need-status",
            "adapters/publish/live.py",
            '"publish.status": {200: "ok", 404: "NOT_FOUND"',
            '"publish.status": {200: "ok", 409: "DUPLICATE_KEY", 404: "NOT_FOUND"',
            'publish.status: live.py STATUS_CODES maps status 409 to "DUPLICATE_KEY"',
        ),
    ]
    seen = []
    for name, rel, old, new, want in variants:
        src = world.prepare(probe.port, name)
        f = src / rel
        t = f.read_text()
        if t.count(old) != 1:
            R.check("7.2", False, f"{name}: could not edit {rel}")
            continue
        f.write_text(t.replace(old, new, 1))
        rc, rep_, _ = world.compile(src, "builds-" + name)
        msgs = diagnostics(rep_)
        hit = [m for m in msgs if want in m]
        seen.append(f"{name}: exit {rc}, {hit[0][:90] if hit else msgs[:2]}")
        R.check(
            "7.2",
            rc != 0 and not (rep_.get("build") or {}).get("dir") and bool(hit),
            f"compile-time refusal of an adapter omitting a code the gateway would need ({seen[-1]})",
        )


def read_records(manual: str) -> dict[str, dict[str, str]]:
    """Record shapes from a compiled manual ("record X" then indented "field: type  comment" lines)."""
    recs: dict[str, dict[str, str]] = {}
    cur = None
    for line in manual.splitlines():
        m = re.match(r"record (\w+)\s*$", line)
        if m:
            cur = recs.setdefault(m.group(1), {})
            continue
        if cur is not None and line[:1] in (" ", "\t") and ":" in line:
            f, rest = line.strip().split(":", 1)
            cur[f.strip()] = re.split(r"\s{2,}", rest.strip())[0].strip()
            continue
        cur = None
    return recs


def shape_ok(v, t: str, recs: dict) -> bool:
    t = t.strip()
    if " | " in t:
        return any(shape_ok(v, x, recs) for x in t.split(" | "))
    if t == "None":
        return v is None
    if t == "str":
        return isinstance(v, str)
    if t == "int":
        return isinstance(v, int) and not isinstance(v, bool)
    if t == "bool":
        return isinstance(v, bool)
    if t.startswith("list[") and t.endswith("]"):
        return isinstance(v, list) and all(shape_ok(x, t[5:-1], recs) for x in v)
    if t.startswith("dict[str, ") and t.endswith("]"):
        return isinstance(v, dict) and all(
            isinstance(k, str) and shape_ok(x, t[10:-1], recs) for k, x in v.items()
        )
    if t in recs:
        return (
            isinstance(v, dict)
            and set(v) == set(recs[t])
            and all(shape_ok(v[f], recs[t][f], recs) for f in v)
        )
    return False


def load_live(path: Path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(f"_shipped_{path.parent.name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def oracle(c: dict, mod, params: dict, raw: dict, recs: dict) -> dict:
    """The expected gateway outcome for one recorded raw response, from kernel.md section 7 and the
    shipped adapter module, computed without the kernel."""
    fails = set(c["failures"])
    und = c.get("undeclared_status", "UNAVAILABLE")
    ratio = c.get("retry_after_ratio", 1)

    def decl(x: str) -> dict:
        return {"code": x if x in fails else und}

    def needed(x: str) -> dict:
        # kernel.md section 7: a code the gateway needs is refused by the compiler when undeclared,
        # never patched at runtime; were one missing anyway, it would be a defect.
        return {"code": x if x in fails else "INVARIANT_VIOLATION"}

    if raw["kind"] == "timeout":
        return needed("TIMEOUT")
    if raw["kind"] == "transport_error":
        return decl("UNAVAILABLE")
    try:
        body = json.loads(raw["body"])
    except ValueError:
        body = None
    mapped = (getattr(mod, "STATUS_CODES", {}) or {}).get(c["name"], {}).get(raw["status"])
    if mapped is None:
        return {"code": und, "undeclared": raw["status"]}
    if mapped == "ok":
        if body is None:
            return decl("INVALID")

        class G:
            def send(self, payload):
                return json.loads(raw["body"])

        try:
            v = getattr(mod, c["name"].split(".", 1)[1])(G(), **params)
        except Exception:
            return decl("INVALID")
        return {"ok": v} if shape_ok(v, c["returns"], recs) else decl("INVALID")
    if mapped == "RATE_LIMITED":
        field = getattr(mod, "RETRY_AFTER_FIELD", None)
        b = body if isinstance(body, dict) else {}
        for cand in (
            b.get(field) if field else None,
            raw.get("headers", {}).get("x-cwr-retry-after-ticks"),
            b.get("retry_after"),
        ):
            n = (
                cand
                if isinstance(cand, int) and not isinstance(cand, bool)
                else (int(cand) if isinstance(cand, str) and cand.strip().isdigit() else None)
            )
            if n is not None and n > 0:
                return (
                    {"code": "RATE_LIMITED", "retry_after": n * ratio}
                    if "RATE_LIMITED" in fails
                    else needed("RATE_LIMITED")
                )
        return decl("INVALID")
    return needed(mapped)


def shipped_refworld(world: World, stubs: dict[str, Stub], scratch: Path) -> None:
    """7.2 on examples/refworld exactly as shipped: every gateway outcome equals the independent oracle."""
    src = scratch / "shipped"
    shutil.copytree(REFWORLD, src, ignore=shutil.ignore_patterns("README.md", "__pycache__"))

    def tree(root: Path) -> dict[str, str]:
        return {
            q.relative_to(root).as_posix(): sha_hex(q.read_bytes())
            for q in sorted(root.rglob("*"))
            if q.is_file() and q.name != "README.md" and "__pycache__" not in q.parts
        }

    same = tree(REFWORLD) == tree(src)
    rc, rep_, err = world.compile(src, "builds-shipped")
    bdir = (rep_.get("build") or {}).get("dir")
    if not R.check(
        "7.2",
        same and rc == 0 and bdir,
        f"examples/refworld as shipped ({len(tree(src))} files, byte-identical copy: {same}) compiles with cwr build (exit {rc}) {diagnostics(rep_)[:3]} {err[-300:]}",
    ):
        return
    build = Path(bdir)
    cmap = contracts(build)
    recs = read_records((build / "manual.md").read_text())
    mods = {m: load_live(src / "adapters" / m / "live.py") for m in ("search", "publish")}
    iid = world.create(build, 41)
    faults = [
        {"adapter": "search.query", "instance": iid, "index": 1, "fault": "UNAVAILABLE"},
        {"adapter": "search.query", "instance": iid, "index": 2, "fault": "RESET"},
        {"adapter": "search.query", "instance": iid, "index": 3, "fault": "MALFORMED"},
        {
            "adapter": "search.query",
            "instance": iid,
            "index": 4,
            "fault": "RATE_LIMITED",
            "retry_after": 25,
        },
        {
            "adapter": "search.query",
            "instance": iid,
            "index": 5,
            "fault": "TIMEOUT",
            "delay_ms": 3600,
        },
        {"adapter": "publish.post", "instance": iid, "index": 1, "fault": "UNAVAILABLE"},
        {
            "adapter": "publish.post",
            "instance": iid,
            "index": 2,
            "fault": "RATE_LIMITED",
            "retry_after": 13,
        },
        {"adapter": "publish.post", "instance": iid, "index": 3, "fault": "MALFORMED"},
        {
            "adapter": "publish.post",
            "instance": iid,
            "index": 4,
            "fault": "TIMEOUT",
            "delay_ms": 5600,
        },
        {"adapter": "publish.status", "instance": iid, "index": 1, "fault": "UNAVAILABLE"},
        {
            "adapter": "publish.status",
            "instance": iid,
            "index": 2,
            "fault": "TIMEOUT",
            "delay_ms": 2600,
        },
        {"adapter": "publish.delete", "instance": iid, "index": 1, "fault": "RESET"},
    ]
    for st in stubs.values():
        st.schedule(faults, allow_undeclared=False)
    cas = scratch / "cassette-shipped.jsonl"
    t1 = [
        (f"s{i}", "search.query", {"q": "tennis" if i < 6 else "golf", "limit": 3})
        for i in range(7)
    ]
    t2 = (
        [
            ("p0", "publish.post", {"key": "a1", "title": "T", "body": "b"}),
            ("p0r", "publish.post", {"key": "a1", "title": "T", "body": "b"}),
            ("p0d", "publish.post", {"key": "a1", "title": "T", "body": "other"}),
        ]
        + [
            (f"p{i}", "publish.post", {"key": f"a{i + 1}", "title": "T", "body": "b"})
            for i in range(1, 7)
        ]
        + [("p1r", "publish.post", {"key": "a2", "title": "T", "body": "b"})]
    )
    calls_all: list[tuple[str, str, dict]] = []
    returned: dict[str, dict] = {}
    for calls in (t1, t2):
        rc, env, err, _ = world.turn(iid, script(calls), mode="record", cassette=cas)
        if not R.check(
            "7.2",
            env is not None,
            f"shipped refworld record turn ran (rc={rc}) {err[-300:] if env is None else ''}",
        ):
            return
        calls_all += calls
        returned.update(env["returned"])
    entries = [json.loads(ln) for ln in cas.read_text().splitlines()[1:]]
    first_post = next(
        (e for e in entries if e["adapter"] == "publish.post" and e["params"]["key"] == "a1"), None
    )
    try:
        pid = json.loads(first_post["response"]["body"])["result"]["post_id"]
    except (TypeError, KeyError, ValueError):
        pid = "unknown"
    t3 = [
        ("st0", "publish.status", {"post_id": pid}),
        ("st1", "publish.status", {"post_id": pid}),
        ("st2", "publish.status", {"post_id": pid}),
        ("st3", "publish.status", {"post_id": "no-such-post"}),
        ("d0", "publish.delete", {"post_id": pid}),
        ("d1", "publish.delete", {"post_id": pid}),
        ("d2", "publish.delete", {"post_id": pid}),
    ]
    rc, env, err, _ = world.turn(iid, script(t3), mode="record", cassette=cas)
    if not R.check(
        "7.2",
        env is not None,
        f"shipped refworld record turn ran (rc={rc}) {err[-300:] if env is None else ''}",
    ):
        return
    calls_all += t3
    returned.update(env["returned"])
    entries = {
        (e["adapter"], e["request_hash"], e["seq"]): e
        for e in (json.loads(ln) for ln in cas.read_text().splitlines()[1:])
    }
    recs_l = world.adapter_records(iid)
    if not R.check(
        "7.2",
        len(recs_l) == len(calls_all),
        f"shipped refworld: one ledger record per adapter call ({len(recs_l)} for {len(calls_all)})",
    ):
        return
    first: dict[tuple[str, str], tuple[str, dict]] = {}
    mismatches, undeclared_statuses, empty_ok, n_service, n_store = [], set(), 0, 0, 0
    for (label, adapter, params), rec in zip(calls_all, recs_l, strict=False):
        c = cmap[adapter]
        got = returned.get(label) or {}
        g = rec.get("gateway") or {}
        kp = "key" if c.get("idempotency") == "key" else None
        if g.get("via") == "service":
            n_service += 1
            e = entries.get((adapter, g.get("request_hash"), g.get("seq")))
            if e is None or e["params"] != params:
                mismatches.append((label, "no cassette entry"))
                continue
            want = oracle(c, mods[adapter.split(".")[0]], params, e["response"], recs)
            if "undeclared" in want:
                undeclared_statuses.add((adapter, want["undeclared"]))
            # The key's first outcome, except RATE_LIMITED, which releases the key.
            if kp and (adapter, params[kp]) not in first and want.get("code") != "RATE_LIMITED":
                first[(adapter, params[kp])] = (
                    canonical({k: v for k, v in params.items() if k != kp}),
                    want,
                )
        elif g.get("via") == "store" and kp and (adapter, params[kp]) in first:
            n_store += 1
            payload, orig = first[(adapter, params[kp])]
            want = (
                orig
                if payload == canonical({k: v for k, v in params.items() if k != kp})
                else {"code": "DUPLICATE_KEY"}
            )
        else:
            mismatches.append((label, f"unexpected gateway evidence {g}"))
            continue
        if "ok" in want:
            if got.get("ok", "missing") != want["ok"]:
                mismatches.append((label, want, got))
            elif c["returns"] != "None" and (
                want["ok"] == [] or (isinstance(want["ok"], dict) and "" in want["ok"].values())
            ):
                empty_ok += 1
        elif code(got) != want["code"] or (
            want["code"] == "RATE_LIMITED" and got.get("retry_after") != want["retry_after"]
        ):
            mismatches.append((label, want, got))
    R.check(
        "7.2",
        not mismatches and n_service > 0,
        f"examples/refworld as shipped against benchmark/stubs: all {len(calls_all)} adapter outcomes ({n_service} from recorded service responses, {n_store} from the key store) "
        f"equal the oracle computed from the shipped live.py STATUS_CODES and functions, contracts.json and manual records"
        + (f"; mismatches: {mismatches[:4]}" if mismatches else "")
        + f". Observed refworld/stub disagreements the gateway normalised as declared (task 4.1): statuses the shipped STATUS_CODES do not map {sorted(undeclared_statuses)} "
        f"went to undeclared_status; {empty_ok} ok results carry empty content because the shipped live.py reads keys the stub bodies do not have",
    )
    codes = sorted({code(returned[ln]) or "ok" for ln, _, _ in calls_all})
    bad = [
        (r["primitive"], r["outcome"].get("code"))
        for r in recs_l
        if r["outcome"].get("code") not in (None, *cmap[r["primitive"]]["failures"])
    ]
    ok_t, ev = exact_traffic(world, stubs, iid, calls_all, {})
    R.check(
        "7.2",
        not bad and ok_t,
        f"shipped refworld: every outcome is ok or a declared code of its contract ({codes}; undeclared: {bad}); {ev}",
    )
    for st in stubs.values():
        st.schedule([], allow_undeclared=False)


def main() -> int:
    cwr, tests_ok, tests_ev = build_binaries()
    for c in CLAUSES:
        R.check(c, tests_ok, tests_ev)
    scratch = Path(tempfile.mkdtemp(prefix="cwr-2_3-"))
    stubs: dict[str, Stub] = {}
    try:
        world = World(cwr, scratch)
        probe = Listener()
        decoy = Listener()
        build = world.build(probe.port)
        compile_time(world, build, probe)
        stubs = {
            "search": Stub("search", 7, scratch),
            "publish": Stub("publish", 7, scratch),
            "search8": Stub("search", 8, scratch),
        }
        world.stubs = {
            "search": stubs["search"].url,
            "publish": stubs["publish"].url,
            "decoy": f"http://127.0.0.1:{decoy.port}",
        }
        failure_paths(world, build, stubs, probe, decoy)
        closure_and_endpoints(world, build, stubs)
        modes(world, build, stubs, scratch)
        rate_limit_retry(world, build, stubs)
        retry_ratio(world, stubs, probe)
        gateway_needs(world, probe)
        shipped_refworld(world, stubs, scratch)
        # Control: the very probe code, run on the host without any sandbox, does reach the listener,
        # so the zero counts above (cwr build and kernel turns, never reset) are real.
        before = probe.count
        ctl = scratch / "probe-control.txt"
        run([sys.executable, "-I", "-c", probe_code(probe.port, ctl) + "\n_probe_network()\n"])
        deadline = time.monotonic() + 3
        while probe.count < before + 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        rep_ctl = probe_report(ctl)
        R.check(
            "7.1",
            before == 0
            and probe.count == 2
            and rep_ctl
            and "socket=connected" in rep_ctl[0]
            and "fork_exec=spawned" in rep_ctl[0],
            f"control: the same probe code run unsandboxed on the host connects twice, directly and from its _posixsubprocess.fork_exec child (listener count {before} -> {probe.count}; report {rep_ctl}); "
            f"every count before it, across cwr build and all kernel turns, was 0",
        )
    finally:
        for s in stubs.values():
            s.stop()
        if os.environ.get("CWR_KEEP_SCRATCH"):
            print(f"scratch kept at {scratch}", file=sys.stderr)
        else:
            shutil.rmtree(scratch, ignore_errors=True)
    failed = 0
    for c in CLAUSES:
        if R.bad[c] or not R.ok[c]:
            failed += 1
            print(f"CLAUSE {c} FAIL " + " | ".join(R.bad[c]))
        else:
            print(f"CLAUSE {c} PASS " + " | ".join(R.ok[c]))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

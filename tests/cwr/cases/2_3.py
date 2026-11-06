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
protocol of benchmark/stubs/README.md; the copy only adds rules. The publish.post contract declares
`undeclared_status = "INVALID"` (world-source.md section 7) so both undeclared fallbacks (UNAVAILABLE
and INVALID) are exercised, and a fixture case with an undeclared status proves the compiler's contract
tests apply the same fallback as the gateway (a negative-control build that expects UNAVAILABLE must be
refused).

Network closure (7.1). A network probe tries to open a TCP connection to a probe listener, directly and
from a child process started through _posixsubprocess.fork_exec (which raises no audit event). Written
into World Source with plain reflective names, or reached through a fullwidth-spelled `exec` that Python
folds to the builtin, `cwr build` must refuse it before any world code runs. To reach the kernel's
runtime network seal anyway, the driver places the same probe in lib/world.py (world.version) and
adapters/publish/live.py (publish.delete) of the compiled Build and reseals it with the Build hash
recomputed from compiler.md section 7: a test-only Build standing for world code that a lint gap would
let through. The listener must never see a connection while the kernel runs it; a final unsandboxed
control run of the same probe code shows the listener does count both. A build without bubblewrap is
traced with strace to show every world-code worker exec is preceded by the seccomp network seal, and
the cargo unit test of the seal proves sockets are denied to a sealed process and its descendants.
examples/refworld itself is not modified.

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
import traceback
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import protocol

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parents[3]
REFWORLD = REPO / "examples/refworld"
STUBS = REPO / "benchmark/stubs"
CLAUSES = ["1.3", "7.1", "7.2", "7.3", "7.4", "7.5", "7.6"]
TIMEOUT = 900
# The driver's aligned copies of refworld raise limits.wall_ms from 30 s to this. wall_ms is a host
# timer on the whole turn (kernel.md section 3, not a clause of task 2.3); the failure-path turns
# carry many adapter calls plus timeout stalls, and on a host loaded by parallel builds one adapter
# call can take several seconds of host time, which would end a 30 s turn with LIMIT wall_ms before
# the gateway behaviour under test is observed. The byte-identical shipped refworld keeps 30 s and
# is driven with short turns instead (shipped_refworld).
WALL_MS = 300000
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
OBS = protocol.Observer("2.3", protocol.ok_bad(R))

# Releasing a `key` adapter's key on RATE_LIMITED departs from the literal text of kernel.md section 7
# (only a defect releases the key); the driver asserts that behaviour only together with this recorded
# deviation.
DEVIATION = "finding.task_2_3_rate_limited_key_release"


def deviation_recorded() -> bool:
    q = REPO / "spec/cwr/qualification.kvx"
    text = q.read_text() if q.is_file() else ""
    m = re.search(r"(?ms)^\[" + re.escape(DEVIATION) + r"\]\n(.*?)(?=^\[|\Z)", text)
    return bool(m) and "RATE_LIMITED" in m.group(1) and "kernel.md section 7" in m.group(1)


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


STUB_LAUNCH = (
    "import os, runpy, sys; p = os.environ['CWR_2_3_STUB_SERVER']; sys.argv = [p] + sys.argv[1:]; "
    "runpy.run_path(p, run_name='__main__')"
)


class Stub:
    def __init__(self, name: str, seed: int, scratch: Path) -> None:
        self.name = name
        # Started through runpy with the stub path in the environment, so the command line names no
        # stub file: a host-wide `pkill -f <name>_server.py` from a concurrent run cannot end this
        # stub mid-scenario (its schedule, request log and rate windows live in the process).
        self.proc = subprocess.Popen(
            [sys.executable, "-I", "-c", STUB_LAUNCH, "--port", "0", "--seed", str(seed)],
            stdout=subprocess.PIPE,
            stderr=(scratch / f"stub-{name}-{seed}.stderr.log").open("w"),
            text=True,
            cwd=scratch,
            env={**os.environ, "CWR_2_3_STUB_SERVER": str(STUBS / f"{name}_server.py")},
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

# The network probe placed in the test-only Build's lib/ and live.py. Three attempts, each recorded in a report file when the
# filesystem allows it: a direct socket; a child process started through _posixsubprocess.fork_exec,
# which raises no audit event (so an interpreter-level guard alone cannot see it), whose child connects;
# and the kernel's view of the process (/proc/self/status Seccomp and NoNewPrivs).
PROBE_IMPL = """def _probe_impl() -> None:
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
    if __PROBE_RAISE__:
        raise RuntimeError("probe-notes: " + " ".join(notes))
"""


def probe_impl(port: int, report: Path, loud: bool = False) -> str:
    """The probe; `loud` makes it raise its notes, so they surface in the kernel's defect trace."""
    return (
        PROBE_IMPL.replace("__PROBE_PORT__", str(port))
        .replace("__PROBE_REPORT__", str(report))
        .replace("__PROBE_RAISE__", str(loud))
    )


def probe_code(port: int, report: Path, loud: bool = False) -> str:
    return (
        probe_impl(port, report, loud)
        + "\n\ndef _probe_network() -> None:\n"
        + "    _probe_impl()\n"
    )


def probe_report(report: Path) -> list[str]:
    return report.read_text().splitlines() if report.exists() else []


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


def build_hash(root: Path) -> str:
    """The Build hash of compiler.md section 7, recomputed independently: relative paths sorted
    bytewise; per entry the path bytes, NUL, the length in decimal, NUL, the bytes; an empty
    directory is `<path>/` with length 0; manifest.kvx enters without its final `hash` line."""
    entries: list[tuple[bytes, bytes]] = []
    for d, dirs, files in os.walk(root):
        rel = Path(d).relative_to(root)
        if rel != Path(".") and not dirs and not files:
            entries.append(((rel.as_posix() + "/").encode(), b""))
        for f in files:
            r = (rel / f).as_posix()
            data = (Path(d) / f).read_bytes()
            if r == "manifest.kvx":
                lines = data.decode().splitlines(keepends=True)
                if not lines or not lines[-1].startswith('hash = "sha256:'):
                    raise SystemExit("manifest.kvx does not end with its hash line")
                data = "".join(lines[:-1]).encode()
            entries.append((r.encode(), data))
    h = hashlib.sha256()
    for path, data in sorted(entries):
        h.update(path + b"\0" + str(len(data)).encode() + b"\0" + data)
    return h.hexdigest()


def inject_probe(path: Path, anchor: str, port: int, report: Path, loud: bool = False) -> None:
    """Defines the network probe before `anchor` (a `def` line) and calls it first in that function."""
    t = path.read_text()
    if t.count(anchor) != 1:
        raise SystemExit(f"could not place the network probe in {path}")
    t = t.replace(anchor, "\n\n" + probe_code(port, report, loud).rstrip("\n") + "\n" + anchor, 1)
    head = re.escape(anchor.strip("\n"))
    t, n = re.subn(
        "(" + head + r"\n(?:\s+\"\"\".*?\"\"\"\n)?)",
        r"\1    _probe_network()\n",
        t,
        count=1,
        flags=re.S,
    )
    if n != 1:
        raise SystemExit(f"could not call the network probe in {path}")
    path.write_text(t)


def probed_build(
    clean: Path, dest: Path, port: int, report: Path, loud: bool = False
) -> tuple[Path, str]:
    """A test-only Build: the compiled `clean` Build with the network probe placed in lib/world.py
    (world.version) and adapters/publish/live.py (publish.delete) after compilation, resealed with
    the Build hash recomputed here. The compiler's closure lint refuses this probe in World Source
    (closure_lint_refuses_probe), so it can reach the kernel's runtime network seal only through a
    Build edited after compilation; it stands for world code that a lint gap would let through.
    With `loud` the probe raises its notes after its attempts, so they surface in the defect trace.
    Returns the Build directory and the recomputed hash of `clean`, which must equal its name."""
    clean_hash = build_hash(clean)
    tmp = dest / ("probed-loud" if loud else "probed")
    shutil.copytree(clean, tmp, copy_function=shutil.copyfile)
    for d, _dirs, _files in os.walk(tmp):
        os.chmod(d, 0o755)
    inject_probe(tmp / "lib/world.py", "\n\ndef version(state: Any) -> str:\n", port, report, loud)
    inject_probe(
        tmp / "adapters/publish/live.py",
        "\n\ndef delete(gateway: Any, post_id: str) -> None:\n",
        port,
        report,
        loud,
    )
    hexd = build_hash(tmp)
    m = tmp / "manifest.kvx"
    lines = m.read_text().splitlines(keepends=True)
    m.write_text("".join(lines[:-1]) + f'hash = "sha256:{hexd}"\n')
    out = dest / hexd
    tmp.rename(out)
    for d, _dirs, files in os.walk(out):
        for f in files:
            os.chmod(Path(d) / f, 0o444)
    return out, clean_hash


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
        name: str = "src",
        undeclared_expect: str = "INVALID",
        status_fallback: bool = False,
        retry_ratio: int | None = None,
    ) -> Path:
        """The aligned copy of refworld. `undeclared_expect`: the code the added fixture case expects for
        an undeclared status of publish.post; `status_fallback`: also give publish.status (which does not
        declare INVALID) `undeclared_status = "INVALID"`, which the compiler must refuse; `retry_ratio`:
        give search.query and publish.post `retry_after_ratio = <n>`."""
        src = self.scratch / name
        shutil.copytree(REFWORLD, src, ignore=shutil.ignore_patterns("README.md", "__pycache__"))
        wk = src / "world.kvx"
        w, n = re.subn(r"(?m)^wall_ms = \d+$", f"wall_ms = {WALL_MS}", wk.read_text())
        if n != 1:
            raise SystemExit("could not set limits.wall_ms in the copy of world.kvx")
        wk.write_text(w)
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

    def build(self) -> Path:
        src = self.prepare()
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


def completed(clause: str, what: str, rc: int, env: dict | None, err: str) -> dict | None:
    """The `returned` object of a turn that completed with status ok. Anything else (no envelope, or a
    partial/error envelope such as LIMIT wall_ms with `returned: null`) is recorded as a FAIL of
    `clause` with the envelope's status and error as evidence, and None is returned."""
    r = (env or {}).get("returned")
    if env is not None and env.get("status") == "ok" and isinstance(r, dict):
        return r
    R.check(
        clause,
        False,
        f"{what}: turn did not complete (rc={rc} status={(env or {}).get('status')} "
        f"error={(env or {}).get('error')}) {err[-300:] if env is None else ''}",
    )
    return None


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
    for n, calls in ((1, t1), (2, t2)):
        rc, env, err, _took = world.turn(iid, script(calls))
        all_calls += calls
        got = completed("7.2", f"failure-path turn {n}", rc, env, err)
        if got is None:
            return
        outcomes.update(got)
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
    for n, calls, tail in ((3, t3, ""), (4, t4, "r['version'] = world.version()")):
        rc, env, err, _took = world.turn(iid, script(calls, tail))
        all_calls += calls
        got = completed("7.2", f"failure-path turn {n}", rc, env, err)
        if got is None:
            return
        outcomes.update(got)
    # Turn 5: the key store holds across turns: identical replay of k1 sends nothing.
    t5 = [("p_replay_turn5", "publish.post", {"key": "k1", "title": "T", "body": "hello"})]
    rc, env, err, _took = world.turn(iid, script(t5))
    all_calls += t5
    got = completed("7.4", "failure-path turn 5", rc, env, err)
    if got is None:
        return
    outcomes.update(got)

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
        and "stored first outcome" not in outcomes["p_repeat_rl"].get("detail", "")
        and deviation_recorded(),
        f"a RATE_LIMITED outcome is not stored as the key's outcome (the service declined to process it; recorded deviation from kernel.md section 7 "
        f"in spec/cwr/qualification.kvx [{DEVIATION}]: {deviation_recorded()}): the identical repeat was sent to the service again "
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
        f"adapters/publish/live.py (publish.delete) and lib/world.py (world.version) of the test-only Build both tried to connect to a probe listener from inside the kernel; "
        f"it saw {probe.count} connections (probe reports: {probe_report(world.scratch / 'probe-kernel.txt')})",
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
        got = completed("7.5", f"record turn {i + 1}", rc, env, err)
        if got is None:
            return
        rec_envs.append(env)
        if i == 0:
            pid = ((got.get("d") or {}).get("ok") or {}).get("id")
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
        and code(((env or {}).get("returned") or {}).get("a")) is None
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
        got = completed("7.5", f"replay turn {i + 1}", rc, env, err)
        if got is None:
            return
        rep_envs.append(env)
        if i == 0:
            pidv = ((got.get("d") or {}).get("ok") or {}).get("id")
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
    pay1 = (((e1 or {}).get("returned") or {}).get("a") or {}).get("ok")
    pay3 = (((e3 or {}).get("returned") or {}).get("a") or {}).get("ok")
    R.check(
        "1.3",
        pay1 is not None
        and pay3 is not None
        and pay1 != pay3
        and canonical(e1["returned"]) == canonical((e2 or {}).get("returned")),
        "the payload of search.query differs under a service with another content seed while the outcome is equal: payload equality is not claimed",
    )
    tr = [
        t
        for e in (e1, e2, e3)
        for t in (e or {}).get("trace") or []
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


def runtime_seal(world: World, clean: Path, probe: Listener) -> None:
    """7.1 at run time: lib/ and adapter code that try the network from inside the kernel never reach
    it. The loud test-only Build raises the probe's notes after both attempts, so the defect proves the
    probe ran to its end; strace shows each attempted connect() to the probe port failing."""
    dest = world.scratch / "builds-probed-loud"
    dest.mkdir()
    build, _ = probed_build(clean, dest, probe.port, world.scratch / "probe-loud.txt", loud=True)
    iid = world.create(build, 31)
    before = probe.count
    trace = world.scratch / "strace-runtime-probe.txt"
    calls = [("del", "publish.delete", {"post_id": "post_probe"}), ("v", "world.version", {})]
    _rc, env, _err, _ = world.turn(iid, script(calls), strace_out=trace)
    o = (env or {}).get("returned") or {}
    vd = (o.get("v") or {}).get("detail") or ""
    notes = vd.split("probe-notes: ", 1)[1].split() if "probe-notes: " in vd else []
    lines = trace.read_text().splitlines() if trace.exists() else []
    attempts = [ln for ln in lines if "connect(" in ln and f"htons({probe.port})" in ln]
    connected = [ln for ln in attempts if re.search(r"\) = 0$", ln)]
    recs = {r["primitive"]: r for r in world.ledger(iid) if r.get("primitive") in ENDPOINT_PATHS}
    del_rec = recs.get("publish.delete") or {}
    R.check(
        "7.1",
        code(o.get("v")) == "INVARIANT_VIOLATION"
        and bool(notes)
        and "socket=connected" not in notes
        and code(o.get("del")) == "INVARIANT_VIOLATION"
        and (del_rec.get("gateway") or {}).get("via") == "none"
        and len(attempts) >= 4
        and not connected
        and probe.count == before,
        f"the probe ran to its end inside the kernel in lib/world.py (world.version defect: {notes}) and in adapters/publish/live.py "
        f"(publish.delete INVARIANT_VIOLATION from the raised notes, nothing sent: via {(del_rec.get('gateway') or {}).get('via')}); "
        f"strace saw {len(attempts)} connect() calls to the probe port from the kernel's process tree (direct sockets and fork_exec children), "
        f"{len(connected)} succeeded, and the listener counted {probe.count - before} connections",
    )


def compile_time(world: World, build: Path, probe: Listener) -> None:
    """7.1 at build time and 7.2 compiler/gateway agreement on the contract's undeclared_status."""
    tests = json.loads((build / "tests.json").read_text())
    cases = tests.get("cases", [])
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
    neg_src = world.prepare("src-neg-expect", undeclared_expect="UNAVAILABLE")
    rc, rep_, _ = world.compile(neg_src, "builds-neg")
    msgs = diagnostics(rep_)
    hit = [m for m in msgs if "undeclared_status" in m and "INVALID" in m]
    R.check(
        "7.2",
        rc != 0 and not (rep_.get("build") or {}).get("dir") and bool(hit),
        f"negative control: the same world with a fixture expecting UNAVAILABLE for that status is refused by cwr build (exit {rc}: {hit[0][:110] if hit else msgs[:2]})",
    )
    neg2 = world.prepare("src-neg-fallback", status_fallback=True)
    rc2, rep2, _ = world.compile(neg2, "builds-neg2")
    msgs2 = diagnostics(rep2)
    hit2 = [m for m in msgs2 if "undeclared_status" in m and "publish.status" in m]
    R.check(
        "7.2",
        rc2 != 0 and bool(hit2),
        f"an undeclared_status the adapter does not declare in failures (INVALID on publish.status) is refused at compile time, never patched at runtime (exit {rc2}: {hit2[0][:110] if hit2 else msgs2[:2]})",
    )
    # Compile-time closure: the same probe with plain reflective names (`__builtins__`, `vars`) in
    # lib/world.py is refused by cwr build before any world code runs.
    plain = world.prepare("src-plain-probe")
    lw = plain / "lib/world.py"
    lw.write_text(
        lw.read_text().rstrip("\n")
        + "\n\n\n"
        + probe_impl(probe.port, world.scratch / "probe-plain.txt")
    )
    rc4, rep4, _err4 = world.compile(plain, "builds-plain-probe")
    msgs4 = diagnostics(rep4)
    refl = [m for m in msgs4 if "__builtins__" in m]
    R.check(
        "7.1",
        rc4 != 0
        and not (rep4.get("build") or {}).get("dir")
        and bool(refl)
        and not probe_report(world.scratch / "probe-plain.txt"),
        f"closure_lint_refuses_probe: lib/world.py carrying the probe with plain reflective names is refused by cwr build (exit {rc4}, {len(msgs4)} diagnostics: {refl[:1]}) and none of it ran",
    )
    # The same probe reached through `exec` spelled in fullwidth letters, which Python folds to the
    # builtin `exec` (NFKC identifiers) while a token list would see an unknown name: also refused.
    nfkc = world.prepare("src-nfkc-probe")
    lw = nfkc / "lib/world.py"
    exec_nfkc = "\uff45\uff58\uff45\uff43"
    impl = probe_impl(probe.port, world.scratch / "probe-nfkc.txt")
    lw.write_text(
        lw.read_text().rstrip("\n")
        + "\n\n\ndef _probe_network() -> None:\n"
        + "    ns = {}\n"
        + f"    {exec_nfkc}({impl!r}, ns)\n"
        + '    ns["_probe_impl"]()\n'
    )
    rc5, rep5, _err5 = world.compile(nfkc, "builds-nfkc-probe")
    msgs5 = diagnostics(rep5)
    hit5 = [m for m in msgs5 if exec_nfkc in m or "exec" in m]
    R.check(
        "7.1",
        rc5 != 0
        and not (rep5.get("build") or {}).get("dir")
        and bool(hit5)
        and not probe_report(world.scratch / "probe-nfkc.txt"),
        f"closure_lint_refuses_probe: the probe reached through a fullwidth-spelled exec (NFKC-folded to the builtin) is refused by cwr build too (exit {rc5}: {hit5[:1]}) and none of it ran",
    )
    # No bubblewrap (CWR_BWRAP names a missing file): the compiler's world-code worker still runs
    # under the kernel's seccomp network seal, applied before the worker's exec.
    nb = world.prepare("src-no-bwrap")
    trace = world.scratch / "strace-build-no-bwrap.txt"
    rc3, _rep3, _err3 = world.compile(
        nb,
        "builds-no-bwrap",
        env={"CWR_BWRAP": str(world.scratch / "no-such-bwrap")},
        strace_out=trace,
    )
    lines = trace.read_text().splitlines() if trace.exists() else []

    def pid_of(ln: str) -> str:
        return ln.split(None, 1)[0]

    bwrap_exec = [
        ln
        for ln in lines
        if "execve(" in ln and "bwrap" in ln.split("execve(", 1)[1].split(",", 1)[0]
    ]
    py_exec = [
        ln
        for ln in lines
        if "execve(" in ln
        and "python" in ln.split("execve(", 1)[1].split(",", 1)[0]
        and "= 0" in ln
    ]
    sealed_at: dict[str, int] = {}
    for n, ln in enumerate(lines):
        if "PR_SET_SECCOMP" in ln and "SECCOMP_MODE_FILTER" in ln and "= 0" in ln:
            sealed_at.setdefault(pid_of(ln), n)
    unsealed = [
        ln
        for n, ln in enumerate(lines)
        if ln in py_exec and not (pid_of(ln) in sealed_at and sealed_at[pid_of(ln)] < n)
    ]
    connects = [ln for ln in lines if "connect(" in ln and "AF_INET" in ln]
    R.check(
        "7.1",
        rc3 == 0 and lines and not bwrap_exec and py_exec and not unsealed and not connects,
        f"without bubblewrap the compiler's world-code worker is still sealed: cwr build exit {rc3}, 0 bwrap execs, "
        f"{len(py_exec)} python worker execs each preceded in its own process by a successful prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER) "
        f"({len(sealed_at)} sealed processes, {len(unsealed)} unsealed execs), {len(connects)} inet connect() calls",
    )
    # The seal itself, without the interpreter guard: processes and grandchildren under it cannot open a socket.
    R.check(
        "7.1",
        SEAL_TEST[0],
        f"the seal holds without any audit hook (a raw socket, a subprocess child and a shell-spawned grandchild all get EPERM; the unsealed control connects): {SEAL_TEST[1]}",
    )


def rate_limit_retry(world: World, build: Path, stubs: dict[str, Stub]) -> None:
    """7.3 and 7.4: the manual's same-key retry after RATE_LIMITED works as documented. The stub's own
    window (three accepted posts per 100 ticks) rate-limits the fourth post; the script returns
    retry_at = now + retry_after, the model waits by ending turns (conventions.md: "waiting means
    ending the turn"), and a later turn repeats the identical call under the same key."""
    for s in stubs.values():
        s.schedule([], allow_undeclared=False)
    iid = world.create(build, 51)
    first = (
        PRE
        + """import world
r = {}
for i in (1, 2, 3):
    r['d%d' % i] = call(publish.post, key='d-%d' % i, title='Digest %d' % i, body='b')
r['d4'] = call(publish.post, key='digest-4', title='Digest 4', body='b')
r['retry_at'] = world.now() + r['d4']['retry_after'] if 'retry_after' in r['d4'] and r['d4']['retry_after'] else -1
r['d4_now'] = call(publish.post, key='digest-4', title='Digest 4', body='b')
return r
"""
    )
    rc, env, err, _ = world.turn(iid, first)
    o = completed("7.3", "rate-limit turn (posts d1..d4)", rc, env, err)
    if o is None:
        return
    retry_at = o.get("retry_at", -1)
    # Wait turns: each executed turn advances world time by tick_per_turn (plus one tick for now()).
    waited, now = 0, -1
    while retry_at > 0 and waited < 20:
        rc, env, err, _ = world.turn(iid, "import world\nreturn world.now()\n")
        if env is None or env.get("status") != "ok" or not isinstance(env.get("returned"), int):
            R.check(
                "7.3",
                False,
                f"rate-limit wait turn {waited + 1} did not complete (rc={rc} {(env or {}).get('error')})",
            )
            return
        waited, now = waited + 1, env["returned"]
        if now + 1 >= retry_at:
            break
    last = (
        PRE
        + f"""import world
r = {{}}
while world.now() < {retry_at}:
    pass
r['d4_after'] = call(publish.post, key='digest-4', title='Digest 4', body='b')
r['d4_again'] = call(publish.post, key='digest-4', title='Digest 4', body='b')
return r
"""
    )
    rc, env, err, _ = world.turn(iid, last)
    after = completed(
        "7.3", f"rate-limit retry turn (after {waited} wait turns, now {now})", rc, env, err
    )
    if after is None:
        return
    o.update(after)
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
        and o["d4_after"].get("ok") is not None
        and deviation_recorded(),
        f"key adapter (recorded deviation [{DEVIATION}]: {deviation_recorded()}): RATE_LIMITED released the key (the service did not process the request), so both repeats were sent (stub saw digest-4 {sent} times; ledger via {vias}); "
        f"the first non-rate-limited outcome is stored and its identical replay returns the same receipt from the store",
    )


def retry_ratio(world: World, stubs: dict[str, Stub], probe: Listener) -> None:
    """7.2: RATE_LIMITED retry_after is converted to ticks by the contract's declared retry_after_ratio."""
    src = world.prepare("src-ratio3", retry_ratio=3)
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
        src = world.prepare(name)
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

    def record(calls: list[tuple[str, str, dict]]) -> bool:
        """Drives `calls` two per turn: the shipped world's wall_ms (30 s) must hold on a loaded host
        with a timeout stall in the turn. The oracle below reads each outcome from the recorded
        response, so how the calls are spread over turns does not change what is checked."""
        nonlocal calls_all
        for at in range(0, len(calls), 2):
            chunk = calls[at : at + 2]
            rc, env, err, _ = world.turn(iid, script(chunk), mode="record", cassette=cas)
            got = completed(
                "7.2", f"shipped refworld record turn {[c[0] for c in chunk]}", rc, env, err
            )
            if got is None:
                return False
            calls_all += chunk
            returned.update(got)
        return True

    for calls in (t1, t2):
        if not record(calls):
            return
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
    if not record(t3):
        return
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
    codes = sorted({code(returned.get(ln)) or "ok" for ln, _, _ in calls_all})
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
        clean = world.build()
        try:
            compile_time(world, clean, probe)
        except Exception as exc:
            tb = traceback.format_exc().strip().splitlines()
            R.check("7.1", False, f"compile_time raised {exc!r} at {tb[-3:]}")
        (scratch / "builds-probed").mkdir()
        build, clean_hash = probed_build(
            clean, scratch / "builds-probed", probe.port, scratch / "probe-kernel.txt"
        )
        R.check(
            "7.1",
            clean_hash == clean.name and build.name != clean.name,
            f"test-only Build: the network probe was placed in the compiled lib/world.py and adapters/publish/live.py and the Build resealed "
            f"(the hash recomputed here from compiler.md section 7 equals the compiler's for the clean Build: {clean_hash == clean.name}; probed Build sha256:{build.name[:16]}...)",
        )
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
        for clause, scenario, args in (
            ("7.1", runtime_seal, (world, clean, probe)),
            ("7.2", failure_paths, (world, build, stubs, probe, decoy)),
            ("7.1", closure_and_endpoints, (world, build, stubs)),
            ("7.5", modes, (world, build, stubs, scratch)),
            ("7.3", rate_limit_retry, (world, build, stubs)),
            ("7.2", retry_ratio, (world, stubs, probe)),
            ("7.2", gateway_needs, (world, probe)),
            ("7.2", shipped_refworld, (world, stubs, scratch)),
        ):
            try:
                scenario(*args)
            except Exception as exc:  # recorded as a clause failure with evidence, never swallowed
                tb = traceback.format_exc().strip().splitlines()
                R.check(clause, False, f"{scenario.__name__} raised {exc!r} at {tb[-3:]}")
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
    OBS.claim("CWR-009.a2", match="a stub 503 maps only to the declared code UNAVAILABLE")
    OBS.claim("CWR-009.a3", match="TIMEOUT after .* ms of host time")
    OBS.claim("CWR-009.a4", match="different payload is DUPLICATE_KEY")
    OBS.claim("CWR-009.a5", match="identical replay returns the original receipt")
    OBS.claim("CWR-009.a6", match="malformed body maps to INVALID")
    OBS.claim("CWR-009.a7", match="world_time advanced by exactly")
    OBS.claim("CWR-009.a9", match="cassette miss")
    OBS.emit()
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

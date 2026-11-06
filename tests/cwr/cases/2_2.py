#!/usr/bin/env python3
# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Task 2.2 driver: sandboxed executor and atomic contract check.

Cases CWR-007 (sandbox closure and resource limits) and CWR-008 (atomic primitives, ordered preconditions
and partial scripts). Exercises clauses 1.1, 1.2, 1.3, 1.6, 2.2, 2.6, 6.1, 6.2, 6.3, 6.4 and 6.5 against the
real implementation: it builds the `cwr` binary, runs the `cwr-kernel` test suite, compiles three Builds from
copies of examples/refworld with the real `cwr build` (the control world, a variant with small limits and a
variant whose lib/ carries injected defects reachable only by inputs the fixtures never use), then drives
`cwr instance create` and `cwr instance turn` as subprocesses and judges only what the kernel leaves behind:
the result envelopes on stdout, the ledger files, the Instance state and the seal marker.

Independent oracles here: the Build hash (compiler.md section 7, recomputed in this file), the expression
hashes, the canonical form and `returned_hash`, the expected codec form of returned values, the predicted
outcome of every precondition (evaluated by the script itself through read_only primitives) and the
determinism audit over ledgers of kernel.md section 8. Durability before return is observed with strace.

Hostile scripts: network (import, object-graph socket, kernel-level connect), file escape (open, graph walk,
kernel-level view), clock (time import, graph walk to a file timestamp, the monotonic clock threading
captured, a walk of every module, class and closure for surviving host built-ins), threads, process and
frame introspection,
resource bombs (CPU loop, blocked wall clock, large and gradual memory, caught MemoryError), primitive and
adapter budgets.

Prints one line per clause, `CLAUSE <req.ac> PASS|FAIL <evidence>`, and exits 0 only when every clause
passes. Honours CARGO_TARGET_DIR (default: the workspace target dir). Python 3.12, standard library only.

The Instance home (ledger, state objects, metadata) lives on a tmpfs when the host has one (/dev/shm),
else in the default temp dir; CWR_DRIVER_HOME_ROOT overrides. Builds stay in the default temp dir. The kernel fsyncs every ledger record before it answers,
and on a shared, write-loaded disk each fsync can take most of a second. That latency counts against
every turn's wall_ms, so with enough of it a spec-default 30000 ms turn of 69 calls hits LIMIT wall_ms.
tmpfs keeps the Instance store off the host disk. Every fsync is still issued, and the 6.2 strace check
still asserts that it comes before each answer.
"""

from __future__ import annotations

import hashlib
import importlib.util
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import protocol

sys.dont_write_bytecode = (
    True  # importing the Build's codec must never write __pycache__ into a Build
)

REPO = Path(__file__).resolve().parents[3]
REFWORLD = REPO / "examples/refworld"
CLAUSES = ["1.1", "1.2", "1.3", "1.6", "2.2", "2.6", "6.1", "6.2", "6.3", "6.4", "6.5"]
ENVELOPE = [
    "NOT_FOUND",
    "LOCKED",
    "CONFLICT",
    "INVALID",
    "LIMIT",
    "UNAVAILABLE",
    "RATE_LIMITED",
    "TIMEOUT",
    "REJECTED",
    "DUPLICATE_KEY",
    "INVARIANT_VIOLATION",
]
TIMEOUT = 900
# Fallback tunnel devices the Linux kernel creates in every new network namespace when the tunnel modules
# are loaded; they are down and have no address or route.
FALLBACK_TUNNELS = {
    "tunl0",
    "sit0",
    "ip6tnl0",
    "gre0",
    "gretap0",
    "erspan0",
    "ip_vti0",
    "ip6_vti0",
    "ip6gre0",
}
# Process-limit Build: tight cpu and memory so the bombs end fast. wall_ms leaves room for a durable
# primitive before a cpu or memory bomb: each primitive fsyncs its ledger record before it is answered,
# and on a loaded host that can take about a second, which must not be mistaken for the bomb's own limit.
TIGHT = {"cpu_ms": 800, "wall_ms": 8000, "memory_mb": 96, "primitive_calls": 12, "adapter_calls": 2}
# Budget Build (clause 6.5): the same small call budgets with the spec-default process limits, so a slow
# host can never end a budget probe on wall time before the budget is reached.
BUDGET = {**TIGHT, "cpu_ms": 5000, "wall_ms": 30000, "memory_mb": 256}


class Results:
    def __init__(self) -> None:
        self.ok: dict[str, list[str]] = {c: [] for c in CLAUSES}
        self.bad: dict[str, list[str]] = {c: [] for c in CLAUSES}

    def check(self, clause: str, condition, evidence: str) -> bool:
        (self.ok if condition else self.bad)[clause].append(evidence)
        return bool(condition)


R = Results()
OBS = protocol.Observer("2.2", protocol.ok_bad(R))


def run(cmd, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(c) for c in cmd], capture_output=True, text=True, timeout=TIMEOUT, **kw
    )


def target_dir() -> Path:
    env = os.environ.get("CARGO_TARGET_DIR")
    return Path(env) if env else REPO / "target"


def build_binaries() -> tuple[Path, bool, str]:
    env = dict(os.environ)
    env.setdefault("CARGO_TARGET_DIR", str(target_dir()))
    b = run(["cargo", "build", "-p", "cwr-cli", "-p", "cwr-kernel"], cwd=REPO, env=env)
    if b.returncode != 0:
        print(b.stderr[-4000:], file=sys.stderr)
        raise SystemExit("cargo build failed")
    t = run(["cargo", "test", "-p", "cwr-kernel"], cwd=REPO, env=env)
    passed = sum(int(n) for n in re.findall(r"test result: ok\. (\d+) passed", t.stdout + t.stderr))
    return (
        target_dir() / "debug" / "cwr",
        t.returncode == 0,
        f"cargo test -p cwr-kernel rc={t.returncode} passed={passed}",
    )


# ------------------------------------------------------------------------------------------------
# Independent oracles
# ------------------------------------------------------------------------------------------------


def canonical(v) -> str:
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_hash(d: Path) -> str:
    """compiler.md section 7: sorted in-hash paths; path, NUL, decimal length, NUL, bytes; the manifest's
    final `hash = "sha256:<hex>"` line is excluded; an empty directory is the entry `<dir>/`."""
    files = [
        f
        for f in ["manifest.kvx", "contracts.json", "contract_diff.json", "manual.md", "tests.json"]
        if (d / f).is_file()
    ]
    for sub in ["sdk", "lib", "adapters", "fixtures", "migrations"]:
        root = d / sub
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*")):
            if p.is_file():
                files.append(p.relative_to(d).as_posix())
            elif p.is_dir() and not any(p.iterdir()):
                files.append(p.relative_to(d).as_posix() + "/")
        if not any(root.iterdir()):
            files.append(sub + "/")
    files.sort(key=lambda s: s.encode())
    h = hashlib.sha256()
    for f in files:
        data = b"" if f.endswith("/") else (d / f).read_bytes()
        if f == "manifest.kvx":
            body = data[:-1] if data.endswith(b"\n") else data
            start = body.rfind(b"\n") + 1
            if re.fullmatch(rb'hash = "sha256:[0-9a-f]{64}"', body[start:]):
                data = data[:start]
        h.update(f.encode() + b"\0" + str(len(data)).encode() + b"\0" + data)
    return h.hexdigest()


def tree_digest(d: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(d.rglob("*")):
        st = p.lstat()
        h.update(p.relative_to(d).as_posix().encode() + b"\0" + oct(st.st_mode).encode() + b"\0")
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


# ------------------------------------------------------------------------------------------------
# Builds and Instances
# ------------------------------------------------------------------------------------------------


class World:
    def __init__(self, cwr: Path, scratch: Path, home: Path | None = None) -> None:
        self.cwr = cwr
        self.scratch = scratch
        self.home = home or scratch / "home"
        self.n = 0
        self.envelopes: list[tuple[str, dict]] = []

    def build(self, name: str, mutate=None) -> Path:
        src = self.scratch / f"src-{name}"
        shutil.copytree(REFWORLD, src, ignore=shutil.ignore_patterns("README.md", "__pycache__"))
        if mutate:
            mutate(src)
        p = run([self.cwr, "build", src, "--json", "--out", self.scratch / f"builds-{name}"])
        rep = json.loads(p.stdout) if p.stdout.strip().startswith("{") else {}
        b = rep.get("build") or {}
        if p.returncode != 0 or not b.get("dir"):
            raise SystemExit(
                f"cwr build {name} failed ({p.returncode}): {rep.get('summary')} {p.stderr[-800:]}"
            )
        return Path(b["dir"])

    def create(self, build: Path, seed: int | None = None, initial: dict | None = None) -> str:
        args = [self.cwr, "instance", "create", "--home", self.home, build]
        if seed is not None:
            args += ["--seed", str(seed)]
        if initial is not None:
            self.n += 1
            f = self.scratch / f"initial-{self.n}.json"
            f.write_text(json.dumps(initial))
            args += ["--initial", f]
        p = run(args)
        if p.returncode != 0:
            raise SystemExit(f"instance create failed: {p.stderr}")
        return p.stdout.strip()

    def turn(
        self, iid: str, script: str, *flags: str, tag: str = ""
    ) -> tuple[int, dict | None, str]:
        self.n += 1
        f = self.scratch / f"turn-{self.n}.py"
        f.write_text(script)
        p = run([self.cwr, "instance", "turn", iid, "--home", self.home, "--script", f, *flags])
        env = None
        if p.returncode == 0:
            try:
                env = json.loads(p.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                env = None
        if env is not None:
            self.envelopes.append((iid, env))
        return p.returncode, env, p.stderr

    def ledger(self, iid: str) -> list[dict]:
        return [
            json.loads(ln)
            for ln in (self.home / "instances" / iid / "ledger.jsonl").read_text().splitlines()
            if ln.strip()
        ]

    def state(self, iid: str) -> dict:
        p = run([self.cwr, "instance", "state", iid, "--home", self.home])
        return json.loads(p.stdout) if p.returncode == 0 else {"error": p.stderr}

    def turn_records(self, iid: str, turn: int) -> list[dict]:
        return [
            r
            for r in self.ledger(iid)
            if r["turn"] == turn and r["kind"] in ("primitive", "adapter")
        ]


def err(env: dict | None) -> tuple:
    if not env or not env.get("error"):
        return (None, None, None, None)
    e = env["error"]
    return (e["code"], e["primitive"], e["precondition_index"], e["limit"])


def summary(env: dict | None) -> str:
    if env is None:
        return "no envelope"
    return f"{env['status']} {err(env)} calls={env['resources']['primitive_calls']}"


def load_codec(build: Path):
    spec = importlib.util.spec_from_file_location(
        "cwr_codec_under_test", build / "sdk/cwr/_codec.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def contracts(build: Path) -> dict[str, dict]:
    c = json.loads((build / "contracts.json").read_text())
    return {p["name"]: p for p in c["primitives"] + c["adapters"]}


# ------------------------------------------------------------------------------------------------
# Clause checks
# ------------------------------------------------------------------------------------------------


def check_closure(world: World, ctl: Path) -> None:
    """1.1 and 6.1: closure from inside the script, both layers."""
    c = json.loads((ctl / "contracts.json").read_text())
    enumerated = (
        len(c["primitives"]) > 0
        and len(c["adapters"]) > 0
        and sorted(c["limits"]) == sorted(TIGHT)
        and [e["code"] for e in c["envelope"]] == ENVELOPE
    )
    R.check(
        "1.1",
        enumerated,
        f"contracts.json enumerates {len(c['primitives'])} primitives, {len(c['adapters'])} adapters, limits {sorted(c['limits'])} and the 11 envelope codes",
    )

    iid = world.create(ctl)
    probes = {
        "unlisted_module": "import nukes",
        "unlisted_primitive": "import cwr\ncwr._rpc.call('fs.nuke', {'path': '/a'})",
        "unlisted_primitive_raw": "import cwr\ncwr._rpc.call('os.system', {'cmd': 'id'})",
        "import_socket": "import socket",
        "graph_socket": "cls = [k for k in ().__class__.__base__.__subclasses__() if k.__name__ == 'socket']\ns = cls[0](2, 1)\ns.connect(('1.1.1.1', 53))",
        "open_host_file": "open('/etc/passwd').read()",
        "open_repo_file": "open('/root/audit/Cargo.toml').read()",
        "graph_listdir": "w = [k for k in ().__class__.__base__.__subclasses__() if k.__name__ == '_wrap_close'][0]\nw.__init__.__globals__['listdir']('/')",
        "import_time": "import time\ntime.time()",
        "import_datetime": "import datetime",
        "dunder_import_time": "__import__('time')",
        "graph_clock_stat": "w = [k for k in ().__class__.__base__.__subclasses__() if k.__name__ == '_wrap_close'][0]\nw.__init__.__globals__['stat']('/').st_mtime",
        "graph_threading_clock": "cw = [k for k in ().__class__.__base__.__subclasses__() if k.__name__ == 'catch_warnings'][0]\ncw.__init__.__globals__['sys'].modules['threading']._time()",
        "graph_supports_fd_stat": "w = [k for k in ().__class__.__base__.__subclasses__() if k.__name__ == '_wrap_close'][0]\nnext(f for f in w.__init__.__globals__['supports_fd'] if f.__name__ == 'stat')('/')",
        "graph_thread_start": "cw = [k for k in ().__class__.__base__.__subclasses__() if k.__name__ == 'catch_warnings'][0]\ncw.__init__.__globals__['sys'].modules['threading'].Thread(target=int).start()",
        "import_os": "import os",
        "import_sys": "import sys",
        "import_json": "import json",
        "import_random": "import random",
        "import_subprocess": "import subprocess",
        "frame_walk": "try:\n    1 / 0\nexcept ZeroDivisionError as e:\n    e.__traceback__.tb_frame.f_globals",
    }
    body = ["import cwr", "out = {}"]
    for k, src in probes.items():
        body.append("try:")
        body.extend("    " + ln for ln in src.splitlines())
        body.append(f"    out[{k!r}] = ['not refused']")
        body.append("except cwr.errors.WorldError as e:")
        body.append(
            f"    out[{k!r}] = [e.code, e.primitive, isinstance(e, (ImportError, PermissionError))]"
        )
        body.append("except BaseException as e:")
        body.append(f"    out[{k!r}] = ['other', type(e).__name__, str(e)[:80]]")
    body.append("import fs, kv, jobs, world, search, publish")
    body.append("from cwr import errors")
    body.append("out['sdk_imports'] = ['ok']")
    body.append("return out")
    t0 = [r for r in world.ledger(iid)][-1]["world_time"]
    rc, env, _ = world.turn(iid, "\n".join(body) + "\n")
    out = (env or {}).get("returned") or {}
    expect = {
        "unlisted_module": "sandbox.import",
        "unlisted_primitive": "fs.nuke",
        "unlisted_primitive_raw": "os.system",
        "import_socket": "sandbox.import",
        "graph_socket": "sandbox.network",
        "open_host_file": "sandbox.filesystem",
        "open_repo_file": "sandbox.filesystem",
        "graph_listdir": "sandbox.os",
        "import_time": "sandbox.import",
        "import_datetime": "sandbox.import",
        "dunder_import_time": "sandbox.import",
        "graph_clock_stat": "sandbox.os",
        "graph_threading_clock": "sandbox.clock",
        "graph_supports_fd_stat": "sandbox.os",
        "graph_thread_start": "sandbox.thread",
        "import_os": "sandbox.import",
        "import_sys": "sandbox.import",
        "import_json": "sandbox.import",
        "import_random": "sandbox.import",
        "import_subprocess": "sandbox.import",
        "frame_walk": "sandbox.interpreter",
    }
    wrong = {
        k: out.get(k)
        for k, fac in expect.items()
        if not (isinstance(out.get(k), list) and out[k][:2] == ["INVALID", fac])
    }
    recs = world.turn_records(iid, env["world"]["turn"]) if env else []
    refused = [r for r in recs if r["outcome"]["code"] == "INVALID"]
    no_tick = all(r["world_time"] == t0 for r in refused)
    ok = (
        rc == 0
        and env
        and env["status"] == "ok"
        and not wrong
        and out.get("sdk_imports") == ["ok"]
        and len(refused) == len(expect)
        and no_tick
    )
    R.check(
        "1.1",
        ok,
        f"{len(expect)} closure probes (unlisted module and primitives, socket, files, clock, os, subprocess, frames) each refused INVALID with its facility, {len(refused)} ledger records, no tick; turn {summary(env)}; wrong={wrong}",
    )
    R.check(
        "6.1",
        ok,
        f"only the Build SDK is importable and no network, host filesystem or host clock is reachable from the script: {len(expect)} probes refused and recorded",
    )
    R.check(
        "6.1",
        all(
            isinstance(out.get(k), list) and out[k][2] is True
            for k in expect
            if (out.get(k) if isinstance(out.get(k), list) and len(out[k]) > 1 else [None, None])[1]
            in ("sandbox.import", "sandbox.filesystem", "sandbox.os", "sandbox.network")
        ),
        "closure refusals are typed INVALID exceptions that are also ImportError or PermissionError",
    )

    # Captured host callables: walk every module, class and closure reachable from the script and
    # list each built-in of time, posix, _thread, _signal or _imp still bound to its module, then call
    # every clock-named callable found. The same walk without the guard is the positive control.
    walk = """
cw = [k for k in ().__class__.__base__.__subclasses__() if k.__name__ == 'catch_warnings'][0]
mods = cw.__init__.__globals__['sys'].modules
host = {'time', 'posix', '_thread', '_signal', '_imp'}
found = []
seen = {}
def visit(v, where, depth):
    if id(v) in seen or depth > 5:
        return
    seen[id(v)] = v
    s = getattr(v, '__self__', None)
    if type(v).__name__ == 'builtin_function_or_method' and type(s).__name__ == 'module' and s.__name__ in host:
        found.append(where)
        return
    if isinstance(v, dict):
        for k, x in list(v.items()):
            visit(x, f'{where}[{k!r}]', depth + 1)
    elif isinstance(v, (list, tuple, set, frozenset)):
        for i, x in enumerate(list(v)):
            visit(x, f'{where}<{i}>', depth + 1)
    elif type(v).__name__ == 'function':
        for i, c in enumerate(v.__closure__ or ()):
            try:
                visit(c.cell_contents, f'{where}.cell{i}', depth + 1)
            except ValueError:
                pass
    elif isinstance(v, type):
        visit(dict(vars(v)), f'{where}.__dict__', depth + 1)
for name, m in list(mods.items()):
    visit(dict(vars(m)), name, 0)
for k in ().__class__.__base__.__subclasses__():
    visit(k, k.__qualname__, 0)
b = __builtins__
visit(dict(b) if isinstance(b, dict) else dict(vars(b)), 'builtins', 0)
clock = []
names = ('time', 'monotonic', 'perf_counter', 'process_time', 'thread_time', 'time_ns', 'monotonic_ns', 'perf_counter_ns', '_time', 'clock_gettime')
for name, m in list(mods.items()):
    for attr in names:
        fn = getattr(m, attr, None)
        if callable(fn) and not isinstance(fn, type):
            try:
                clock.append([name, attr, repr(fn(1) if attr == 'clock_gettime' else fn())[:40]])
            except BaseException:
                pass
return {'found': found[:10], 'n': len(found), 'clock': clock}
"""
    _rc, wenv, _ = world.turn(iid, walk)
    _rc, wdiag, _ = world.turn(iid, walk, "--diagnostic-no-language-guard")
    w_on = (wenv or {}).get("returned") or {}
    w_off = (wdiag or {}).get("returned") or {}
    walk_ok = (
        wenv is not None
        and wenv["status"] == "ok"
        and w_on.get("n") == 0
        and w_on.get("clock") == []
        and wdiag is not None
        and (w_off.get("n") or 0) > 0
        and any(c[1] == "_time" and c[0] == "threading" for c in w_off.get("clock") or [])
    )
    R.check(
        "6.1",
        walk_ok,
        f"no host clock or host callable survives in any module, class or closure: guard on {w_on.get('n')} host built-ins, clock reads {w_on.get('clock')}; guard off (control, kernel layer only) {w_off.get('n')} host built-ins, clock reads {[c[:2] for c in w_off.get('clock') or []]}",
    )
    R.check(
        "1.1",
        walk_ok,
        "facilities the Build does not enumerate are unreachable even through references captured before the guard (threading._time, os.supports_fd, closure cells)",
    )

    # Kernel-level layer alone (diagnostic switch disables only the in-interpreter guard).
    diag = """
import os, socket, sys, resource
r = {}
r['version'] = list(sys.version_info[:2])
r['root'] = sorted(os.listdir('/'))
r['host_paths'] = {p: os.path.exists(p) for p in ['/root', '/root/audit', '/etc', '/home', '/proc', '/dev', '/tmp', '/var', '/run']}
s = socket.socket()
try:
    s.settimeout(2); s.connect(('1.1.1.1', 53)); r['tcp'] = 'connected'
except OSError as e:
    r['tcp'] = e.errno
s.close()
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.sendto(b'x', ('8.8.8.8', 53)); r['udp'] = 'sent'
except OSError as e:
    r['udp'] = e.errno
s.close()
r['ifaces'] = [n for _, n in socket.if_nameindex()]
try:
    open('/escape', 'w'); r['rootwrite'] = 'written'
except OSError as e:
    r['rootwrite'] = e.errno
try:
    open('/cwr/sdk/fs.py', 'a'); r['sdkwrite'] = 'written'
except OSError as e:
    r['sdkwrite'] = e.errno
open('/scratch/turn_marker', 'w').write('x')
r['scratch'] = os.listdir('/scratch')
r['env'] = sorted(os.environ)
r['uid'] = os.getuid()
r['host'] = os.uname().nodename
fds = []
for fd in range(64):
    try:
        os.fstat(fd); fds.append(fd)
    except OSError:
        pass
r['fds'] = fds
r['rlimit_as'] = list(resource.getrlimit(resource.RLIMIT_AS))
return r
"""
    rc, env, stderr = world.turn(iid, diag, "--diagnostic-no-language-guard")
    d = (env or {}).get("returned") or {}
    mech = next((ln for ln in stderr.splitlines() if "sandbox=" in ln), "").split("turn: ", 1)[-1][
        :160
    ]
    allowed_root = {"cwr", "lib", "lib32", "lib64", "libx32", "scratch", "usr"}
    kernel_ok = (
        env
        and env["status"] == "ok"
        and d.get("version") == [3, 12]
        and set(d.get("root", ["?"])) <= allowed_root
        and not any(d.get("host_paths", {"x": True}).values())
        and d.get("tcp") == 101
        and d.get("udp") == 101
        and set(d.get("ifaces", ["?"])) <= {"lo"} | FALLBACK_TUNNELS
        and d.get("rootwrite") == 30
        and d.get("sdkwrite") in (13, 30)
        and set(d.get("env", ["?"]))
        <= {"CWR_RPC_FD", "PYTHONHASHSEED", "PYTHONUTF8", "PWD", "LC_CTYPE"}
        and d.get("uid") == 65534
        and d.get("host") == "cwr"
        and d.get("fds") == [0, 1, 2, 3]
        and d.get("rlimit_as") == [256 * 1024 * 1024] * 2
    )
    R.check(
        "6.1",
        kernel_ok,
        f"kernel layer alone: Python {d.get('version')}, view {d.get('root')}, host paths absent, tcp/udp errno {d.get('tcp')}/{d.get('udp')}, ifaces {d.get('ifaces')}, write / errno {d.get('rootwrite')}, SDK write errno {d.get('sdkwrite')}, env {d.get('env')}, uid {d.get('uid')}, fds {d.get('fds')}, RLIMIT_AS {d.get('rlimit_as')}; mechanism: {mech}",
    )
    R.check(
        "1.1",
        kernel_ok and d.get("tcp") == 101,
        "no network endpoint is reachable even without the language guard (empty network namespace)",
    )

    _rc2, env2, _ = world.turn(
        iid,
        "import os\nreturn os.path.exists('/scratch/turn_marker')\n",
        "--diagnostic-no-language-guard",
    )
    _rc3, env3, _ = world.turn(iid, "import cwr\ncwr._turn_mark = 1\nreturn 1\n")
    _rc4, env4, _ = world.turn(iid, "import cwr\nreturn hasattr(cwr, '_turn_mark')\n")
    fresh = (
        env2
        and env2["returned"] is False
        and env4
        and env4["returned"] is False
        and env3
        and env3["status"] == "ok"
    )
    R.check(
        "6.1",
        fresh,
        f"each turn is a fresh process: scratch marker visible next turn={env2 and env2['returned']}, module attribute survives={env4 and env4['returned']}",
    )
    # A turn run with the language guard off is visible in its ledger records, a normal turn is not.
    diag_turn, normal_turn = (
        (env2 or {}).get("world", {}).get("turn"),
        (env3 or {}).get("world", {}).get("turn"),
    )
    by_turn: dict = {}
    for r in world.ledger(iid):
        by_turn.setdefault(r["turn"], []).append(r)
    stamped = by_turn.get(diag_turn, [])
    R.check(
        "6.1",
        bool(stamped)
        and all(r.get("language_guard") is False for r in stamped)
        and all("language_guard" not in r for r in by_turn.get(normal_turn, []))
        and "language_guard=off" in _stderr_of_guard_off(world, iid),
        f"the {len(stamped)} records of diagnostic turn {diag_turn} carry language_guard=false and "
        f"normal turn {normal_turn} carries no such field; the sandbox line names the guard as off",
    )


def check_limits(world: World, tight: Path, budget: Path) -> None:
    """6.1 (cpu_ms, wall_ms, memory_mb) and 6.5 (primitive_calls, adapter_calls)."""
    iid = world.create(tight)
    cases = {
        "cpu_ms": "while True:\n    pass\n",
        "wall_ms": "import cwr\ncwr._rpc._sock.recv(1)\n",
        "memory_mb": "x = bytearray(512 * 1024 * 1024)\n",
    }
    for limit, script in cases.items():
        rc, env, stderr = world.turn(iid, script, "--verbose")
        res = (env or {}).get("resources", {})
        peak = next(
            (
                int(ln.rsplit("peak_rss_kb=", 1)[1].split()[0])
                for ln in stderr.splitlines()
                if "peak_rss_kb=" in ln
            ),
            None,
        )
        detail = ((env or {}).get("error") or {}).get("detail", "")
        bound = {
            "cpu_ms": res.get("cpu_ms", 0) >= TIGHT["cpu_ms"]
            and res.get("cpu_ms", 10**9) < TIGHT["cpu_ms"] + 700,
            "wall_ms": res.get("wall_ms", 0) >= TIGHT["wall_ms"]
            and res.get("wall_ms", 10**9) < TIGHT["wall_ms"] + 2000,
            "memory_mb": peak is not None
            and 0 < peak <= TIGHT["memory_mb"] * 1024
            and f"{TIGHT['memory_mb']} MiB" in detail,
        }[limit]
        R.check(
            "6.1",
            rc == 0
            and err(env) == ("LIMIT", "sandbox.process", None, limit)
            and env["status"] == "error"
            and bound,
            f"{limit} bomb: {summary(env)} cpu_ms={res.get('cpu_ms')} wall_ms={res.get('wall_ms')} peak_rss_kb={peak}",
        )
    under = TIGHT["memory_mb"] // 3
    rc, env, stderr = world.turn(
        iid, f"x = bytearray({under} * 1024 * 1024)\nx[-1] = 1\nreturn len(x)\n", "--verbose"
    )
    R.check(
        "6.1",
        rc == 0 and env and env["status"] == "ok" and env["returned"] == under * 1024 * 1024,
        f"memory_mb is the Build's cap of {TIGHT['memory_mb']} MiB: a {under} MiB allocation succeeds ({summary(env)}) while 512 MiB ends with LIMIT memory_mb",
    )
    rc, env, _ = world.turn(
        iid,
        "try:\n    x = bytearray(512 * 1024 * 1024)\nexcept MemoryError:\n    return 'survived'\n",
    )
    R.check(
        "6.1",
        env and err(env)[3] == "memory_mb" and env["returned"] is None,
        f"caught MemoryError still terminates: {summary(env)}",
    )
    rc, env, _ = world.turn(
        iid, "chunks = []\nwhile True:\n    chunks.append(bytearray(1024 * 1024))\n"
    )
    R.check("6.1", env and err(env)[3] == "memory_mb", f"gradual memory growth: {summary(env)}")
    rc, env, _ = world.turn(
        iid,
        "import fs\nfs.write(path='/before', content=b'1')\nwhile True:\n    pass\nfs.write(path='/after', content=b'1')\n",
    )
    files = world.state(iid).get("fs", {}).get("files", {})
    R.check(
        "6.1",
        env
        and env["status"] == "partial"
        and err(env)[3] == "cpu_ms"
        and "/before" in files
        and "/after" not in files,
        f"limit after a mutation terminates the script: {summary(env)}, files {sorted(files)}",
    )
    rc, env, _ = world.turn(iid, "return 'alive'\n")
    R.check(
        "6.1",
        env and env["returned"] == "alive",
        "the Instance is idle and usable after every limit termination",
    )

    # 6.5 primitive_calls, on the budget Build (same call budgets, default process limits)
    iid = world.create(budget)
    script = (
        "import world, cwr\nr0 = world.limits()\nn = 0\nwhile True:\n    try:\n        world.now()\n        n += 1\n"
        "    except cwr.errors.Limit as e:\n        return [r0['remaining_primitive_calls'], r0['primitive_calls'], n, e.code, e.limit, e.primitive]\n"
    )
    rc, env, _ = world.turn(iid, script)
    recs = world.turn_records(iid, env["world"]["turn"]) if env else []
    last, prev = (recs[-1], recs[-2]) if len(recs) >= 2 else ({}, {})
    ok = (
        env
        and env["returned"] == [11, 12, 11, "LIMIT", "primitive_calls", "world.now"]
        and len(recs) == 13
        and last.get("outcome", {}).get("code") == "LIMIT"
        and last.get("limit") == "primitive_calls"
        and last.get("world_time") == prev.get("world_time")
        and env["trace"][-1]["code"] == "LIMIT"
        and env["resources"]["remaining"]["primitive_calls"] == 0
    )
    R.check(
        "6.5",
        ok,
        f"primitive_calls={TIGHT['primitive_calls']}: world.limits() showed {(env['returned'] or [None, None])[:2] if env and isinstance(env.get('returned'), list) else None} before acting; 13th call LIMIT primitive_calls, not executed (no tick, ledger limit field), returned {env and env['returned']} ({summary(env)}, {len(recs)} ledger records)",
    )
    rc, env, _ = world.turn(
        iid, "import world\nreturn world.limits()['remaining_primitive_calls']\n"
    )
    R.check(
        "6.5",
        env and env["returned"] == 11,
        f"budgets are per turn: next turn remaining={env and env['returned']}",
    )
    rc, env, _ = world.turn(
        iid,
        "import world\nn = world.limits()['remaining_primitive_calls']\nfor _ in range(n):\n    world.now()\nreturn n\n",
    )
    R.check(
        "6.5",
        env
        and env["status"] == "ok"
        and env["resources"]["primitive_calls"] == 12
        and env["resources"]["remaining"]["primitive_calls"] == 0,
        f"a script planning with world.limits() uses exactly its budget: {summary(env)}",
    )
    # 6.5 adapter_calls
    script = (
        "import world, search, cwr\nr = [world.limits()['remaining_adapter_calls']]\nfor i in range(3):\n    try:\n        search.query(q='tennis', limit=5)\n"
        "    except cwr.errors.WorldError as e:\n        r.append([e.code, e.limit])\nr.append(world.limits()['remaining_adapter_calls'])\nreturn r\n"
    )
    rc, env, _ = world.turn(iid, script)
    recs = world.turn_records(iid, env["world"]["turn"]) if env else []
    ad = [r for r in recs if r["primitive"] == "search.query"]
    ok = (
        env
        and env["returned"]
        == [2, ["UNAVAILABLE", None], ["UNAVAILABLE", None], ["LIMIT", "adapter_calls"], 0]
        and [r["outcome"]["code"] for r in ad] == ["UNAVAILABLE", "UNAVAILABLE", "LIMIT"]
        and ad[2].get("limit") == "adapter_calls"
        and ad[1]["world_time"] == ad[0]["world_time"] + 1
        and ad[2]["world_time"] == ad[1]["world_time"]
        and env["resources"]["adapter_calls"] == 2
    )
    R.check(
        "6.5",
        ok,
        f"adapter_calls={TIGHT['adapter_calls']}: third external call LIMIT adapter_calls, not executed, no tick; returned {env and env['returned']}",
    )


def check_hostile_runner(world: World, ctl: Path) -> None:
    """6.1 and 6.4: state a script can reach never disarms a refusal, forges a limit or breaks the codec."""
    iid = world.create(ctl)
    walk = (
        "import cwr\n"
        "cells = []\n"
        "for f in (open, __import__, *[c.cell_contents for c in (__import__.__closure__ or ())\n"
        "          if callable(c.cell_contents)]):\n"
        "    for c in getattr(f, '__closure__', None) or ():\n"
        "        v = c.cell_contents\n"
        "        cells.append(type(v).__name__)\n"
        "        if isinstance(v, list):\n"
        "            v[:] = [True] * len(v)\n"
        "sys = cwr._rpc.json.decoder.re.enum.sys\n"
        "raw = type(sys.stdin.buffer.raw)\n"
        "try:\n"
        "    data = raw('/cwr/sdk/cwr/_rpc.py').read(64)\n"
        "except Exception as e:\n"
        "    data = type(e).__name__\n"
        "try:\n"
        "    __import__('os')\n"
        "    imported = 'os'\n"
        "except ImportError as e:\n"
        "    imported = type(e).__name__\n"
        "return {'cells': sorted(set(cells)), 'read': str(data), 'import': imported}\n"
    )
    rc, env, _ = world.turn(iid, walk)
    ret = (env or {}).get("returned") or {}
    refused = [t["primitive"] for t in (env or {}).get("trace", []) if t["code"] == "INVALID"]
    R.check(
        "6.1",
        rc == 0
        and env["status"] == "ok"
        and isinstance(ret.get("cells"), list)
        and set(ret["cells"])
        <= {"str", "builtin_function_or_method", "frozenset", "mappingproxy", "function"}
        and ret.get("read", "").startswith("Refused")
        and ret.get("import", "").startswith("Refused")
        and "sandbox.filesystem" in refused
        and "sandbox.import" in refused
        and env["resources"]["primitive_calls"] >= 2,
        f"closure walks of open.__closure__ and __import__.__closure__ reach only {ret.get('cells')}; "
        f"after flipping every reachable cell the file read is {ret.get('read')!r} and import os is "
        f"{ret.get('import')!r}, both recorded ({refused})",
    )

    forged = {
        "raise": ("raise MemoryError('not really')\n", "turn.script"),
        "nested": (
            "def f():\n    raise MemoryError\ntry:\n    f()\nexcept MemoryError:\n    pass\nf()\n",
            "turn.script",
        ),
        "throw": (
            "def g():\n    yield 1\nit = g()\nnext(it)\nit.throw(MemoryError())\n",
            "turn.script",
        ),
        "raw frame": (
            "import cwr\n"
            'p = b\'{"limit":"memory_mb","op":"limit","v":1}\'\n'
            "cwr._rpc.send_frame(cwr._rpc._sock, p)\n"
            "cwr._rpc._sock.recv(1)\n",
            "sandbox.rpc",
        ),
    }
    for name, (script, primitive) in forged.items():
        rc, env, _ = world.turn(iid, script)
        R.check(
            "6.1",
            rc == 0 and err(env)[:2] == ("INVALID", primitive) and err(env)[3] is None,
            f"a MemoryError the script forges ({name}) is not LIMIT memory_mb: {summary(env)}",
        )

    unencodable = {
        "cyclic list": "l = []\nl.append(l)\nreturn l\n",
        "deep nesting": "v = 1\nfor _ in range(500):\n    v = [v]\nreturn v\n",
        "non-finite float": "return float('nan')\n",
        "set": "return {1, 2}\n",
    }
    for name, script in unencodable.items():
        rc, env, _ = world.turn(iid, script)
        spurious = [t["primitive"] for t in (env or {}).get("trace", [])]
        R.check(
            "6.4",
            rc == 0
            and env["status"] == "error"
            and err(env)[:2] == ("INVALID", "turn.return")
            and not spurious,
            f"a returned {name} is INVALID turn.return with no spurious closure refusal: {summary(env)} trace={spurious}",
        )
    overriding = (
        "class D(dict):\n"
        "    def items(self):\n"
        "        raise RuntimeError('items')\n"
        "class I(int):\n"
        "    def __int__(self):\n"
        "        raise RuntimeError('int')\n"
        "    def __index__(self):\n"
        "        raise RuntimeError('index')\n"
        "class S(str):\n"
        "    def __str__(self):\n"
        "        return 'evil'\n"
        "return [D(a=1), I(5), S('s'), (1, 2)]\n"
    )
    rc, env, _ = world.turn(iid, overriding)
    R.check(
        "6.4",
        rc == 0
        and env["status"] == "ok"
        and env["returned"] == [{"a": 1}, 5, "s", [1, 2]]
        and not env["trace"],
        f"subclass methods a script overrides never run while its return value is encoded: {summary(env)} returned={(env or {}).get('returned')}",
    )


def check_preconditions(world: World, ctl: Path) -> None:
    """2.2: declared order, first failure, code + primitive + index, evaluation stops."""
    con = contracts(ctl)
    iid = world.create(ctl)
    setup = (
        "import fs, jobs\nfor p in ['/src', '/dst', '/s2']:\n    fs.write(path=p, content=b'x')\nfs.lock(path='/s2')\n"
        "for i in range(8):\n    jobs.submit(kind='k', n=1000)\nreturn jobs.active()\n"
    )
    _rc, env, _ = world.turn(iid, setup)
    before = world.state(iid)
    cases = [
        (
            "fs.move",
            "src='/missing', dst='/dst'",
            "NOT_FOUND",
            1,
        ),  # preconditions 1 and 3 both false
        ("fs.move", "src='/s2', dst='/dst'", "LOCKED", 2),  # 2 and 3 both false
        ("fs.move", "src='/src', dst='/dst'", "CONFLICT", 3),
        ("fs.delete", "path='/s2'", "LOCKED", 2),
        ("fs.lock", "path='/s2'", "CONFLICT", 2),
        ("fs.write", "path='/s2', content=b'y'", "LOCKED", 1),
        ("fs.write", "path='/big', content=b'x' * 1048577", "LIMIT", 2),
        ("jobs.submit", "kind='k', n=0", "INVALID", 1),
        ("jobs.submit", "kind='k', n=5", "LIMIT", 2),
        ("kv.set", "ns='a', key='k', value=b'x' * 1025", "INVALID", 1),
        ("world.history", "n=501", "INVALID", 1),
        ("search.query", "q='', limit=99", "INVALID", 1),
    ]
    lines = ["import fs, jobs, kv, world, search, cwr", "out = []"]
    for name, args, _, _ in cases:
        mod, fn = name.split(".")
        lines += [
            "try:",
            f"    {mod}.{fn}({args})",
            "    out.append(None)",
            "except cwr.errors.WorldError as e:",
            "    out.append([e.code, e.primitive, e.precondition_index])",
        ]
    lines.append("return out")
    _rc, env, _ = world.turn(iid, "\n".join(lines) + "\n")
    got = (env or {}).get("returned") or []
    recs = world.turn_records(iid, env["world"]["turn"]) if env else []
    bad = []
    for i, (name, args, code, idx) in enumerate(cases):
        rec = recs[i] if i < len(recs) else {}
        hashes = [sha(e) for e, _ in con[name]["preconditions"]]
        want_evals = [
            {"index": k + 1, "expr_hash": hashes[k], "passed": k + 1 < idx} for k in range(idx)
        ]
        if (
            i >= len(got)
            or got[i] != [code, name, idx]
            or rec.get("primitive") != name
            or rec.get("outcome", {}).get("code") != code
            or rec.get("preconditions") != want_evals
            or con[name]["preconditions"][idx - 1][1] != code
        ):
            bad.append(
                (name, args[:30], got[i] if i < len(got) else None, rec.get("preconditions"))
            )
    unchanged = world.state(iid) == before
    t_end_setup = next(r for r in world.ledger(iid) if r["kind"] == "turn")["world_time"]
    no_tick = all(r["world_time"] == t_end_setup for r in recs)
    R.check(
        "2.2",
        env and not bad and unchanged and no_tick and env["status"] == "ok",
        f"{len(cases)} calls with one or more false preconditions: each returned the first failing precondition's declared code, primitive and one-based index; ledger lists exactly the evaluated preconditions with independently recomputed expr_hash and stops at the first false; state unchanged={unchanged}, no tick={no_tick}; mismatches={bad}",
    )
    # Error-envelope form of the same rule (uncaught).
    _rc, env, _ = world.turn(iid, "import fs\nfs.move(src='/s2', dst='/dst')\n")
    R.check(
        "2.2",
        env
        and env["error"]
        == {
            "code": "LOCKED",
            "primitive": "fs.move",
            "precondition_index": 2,
            "retry_after": None,
            "limit": None,
            "detail": env["error"]["detail"],
        },
        f"uncaught: envelope error {env and env['error']}",
    )


def check_visibility(world: World, ctl: Path) -> None:
    """2.6: every precondition condition is queryable through read_only primitives; the script predicts every
    outcome from visible state before acting."""
    con = contracts(ctl)
    sdk_ok, bad = True, []
    for name, c in con.items():
        for expr, _ in c["preconditions"]:
            for call in re.findall(r"([a-z_]+\.[a-z_]+)\(", expr):
                target = con.get(call)
                mod, fn = call.split(".")
                src = (ctl / "sdk" / f"{mod}.py").read_text()
                if not target or target["side_effect"] != "read_only" or f"def {fn}(" not in src:
                    sdk_ok = False
                    bad.append((name, call))
    R.check(
        "2.6",
        sdk_ok,
        f"every primitive named in a precondition is read_only and generated into the SDK; offenders={bad}",
    )
    iid = world.create(ctl)
    world.turn(
        iid,
        "import fs, kv, jobs\nfor p in ['/a', '/b', '/locked']:\n    fs.write(path=p, content=b'x')\nfs.lock(path='/locked')\nkv.set(ns='q', key='k', value=b'x' * 1000)\njobs.submit(kind='k', n=3)\n",
    )
    cases = [
        ("fs.write", "dict(path='/locked', content=b'y')"),
        ("fs.write", "dict(path='/a', content=b'y')"),
        ("fs.write", "dict(path='/big', content=b'z' * 1048577)"),
        ("fs.delete", "dict(path='/nope')"),
        ("fs.delete", "dict(path='/locked')"),
        ("fs.delete", "dict(path='/b')"),
        ("fs.move", "dict(src='/a', dst='/locked')"),
        ("fs.move", "dict(src='/locked', dst='/c')"),
        ("fs.move", "dict(src='/a', dst='/c')"),
        ("fs.lock", "dict(path='/locked')"),
        ("fs.unlock", "dict(path='/c')"),
        ("fs.unlock", "dict(path='/locked')"),
        ("fs.size", "dict(path='/nope')"),
        ("fs.read", "dict(path='/c')"),
        ("kv.get", "dict(ns='q', key='zz')"),
        ("kv.set", "dict(ns='q', key='k2', value=b'v' * 1024)"),
        ("kv.del", "dict(ns='q', key='nope')"),
        ("kv.del", "dict(ns='q', key='k')"),
        ("jobs.status", "dict(id='j9')"),
        ("jobs.result", "dict(id='j1')"),
        ("jobs.cancel", "dict(id='j1')"),
        ("jobs.cancel", "dict(id='j1')"),
        ("jobs.submit", "dict(kind='k', n=1001)"),
        ("world.history", "dict(n=-1)"),
        ("world.propose", "dict(text='x' * 4097)"),
        ("search.query", "dict(q='tennis', limit=51)"),
        ("publish.post", "dict(key='k', title='t' * 201, body='b')"),
    ]
    pre = {n: con[n]["preconditions"] for n, _ in cases}
    script = f"""import fs, kv, jobs, world, search, publish, cwr
PRE = {pre!r}
MODS = {{'fs': fs, 'kv': kv, 'jobs': jobs, 'world': world, 'search': search, 'publish': publish}}
CASES = [{", ".join(f"({n!r}, {a})" for n, a in cases)}]
out = []
for name, params in CASES:
    ns = dict(MODS)
    ns.update(params)
    predicted = None
    for i, (expr, code) in enumerate(PRE[name]):
        if not eval(expr, {{'__builtins__': {{'len': len}}}}, ns):
            predicted = [code, i + 1]
            break
    mod, fn = name.split('.')
    fn = fn + '_' if fn in ('del', 'def', 'class', 'import', 'from', 'global') else fn
    try:
        getattr(MODS[mod], fn)(**params)
        actual = None
    except cwr.errors.WorldError as e:
        actual = [e.code, e.precondition_index]
    out.append([name, predicted, actual])
return out
"""
    _rc, env, _ = world.turn(iid, script)
    out = (env or {}).get("returned") or []
    mismatch = [
        o
        for o in out
        if o[1] != o[2] and not (o[1] is None and o[2] is not None and o[2][0] == "UNAVAILABLE")
    ]
    recs = world.turn_records(iid, env["world"]["turn"]) if env else []
    case_names = {n for n, _ in cases}
    probes = [r for r in recs if r["primitive"] not in case_names]
    probes_ro = all(con.get(r["primitive"], {}).get("side_effect") == "read_only" for r in probes)
    failures = sum(1 for o in out if o[2] is not None)
    R.check(
        "2.6",
        env and len(out) == len(cases) and not mismatch and probes_ro and failures >= 15,
        f"{len(cases)} calls ({failures} refused): the script evaluated every declared precondition itself through {len(probes)} read_only SDK calls and predicted each outcome before acting; mismatches={mismatch}",
    )


def check_atomic_and_script(world: World, ctl: Path) -> None:
    """6.2 (one RPC, order, unchanged state, tick, durable record before the answer), 6.3 (typed exceptions,
    partial), 6.4 (envelope)."""
    codec = load_codec(ctl)
    iid = world.create(ctl)
    t_start = world.ledger(iid)[-1]["world_time"]
    script = """import fs, world, cwr
fs.write(path='/a', content=b'\\x00\\x01')
fs.lock(path='/a')
try:
    fs.write(path='/a', content=b'zz')
except cwr.errors.Locked as e:
    caught = [e.code, e.primitive, e.precondition_index, e.retry_after, e.limit, type(e).__mro__[1].__name__]
h = world.history(2)
return {'bytes': fs.read(path='/a'), '$k': 1, 't': (1, 'x', None, True), 'caught': caught, 'last': [h[-1]['primitive'], h[-1]['outcome']['code'], h[-1]['preconditions'][0]['passed']], 'rpc': cwr._rpc.calls_made()}
"""
    _rc, env, _ = world.turn(iid, script, "--debug-effects")
    expected = codec.encode(
        {
            "bytes": b"\x00\x01",
            "$k": 1,
            "t": (1, "x", None, True),
            "caught": ["LOCKED", "fs.write", 1, None, None, "WorldError"],
            "last": ["fs.write", "LOCKED", False],
            "rpc": 5,
        }
    )
    recs = world.turn_records(iid, 1)
    times = [t["world_time"] for t in (env or {}).get("trace", [])]
    R.check(
        "6.3",
        env and env["status"] == "ok" and env["returned"]["caught"] == expected["caught"],
        f"a precondition failure raised the typed cwr.errors.Locked carrying code, primitive, precondition_index, retry_after and limit: {env and env['returned']['caught']}; the script caught it and ended ok",
    )
    R.check(
        "6.2",
        env
        and env["returned"]["rpc"] == 5
        and len(recs) == 5
        and env["resources"]["primitive_calls"] == 5
        and len(env["trace"]) == 5,
        f"one RPC per primitive: SDK sent {env and env['returned']['rpc']} RPCs, ledger has {len(recs)} records, envelope counts {env and env['resources']['primitive_calls']}",
    )
    R.check(
        "6.2",
        times == [t_start + 1, t_start + 2, t_start + 2, t_start + 3, t_start + 4]
        and env["world"]["time"] == t_start + 4 + 10,
        f"executed calls tick once each, the precondition failure does not: trace world_time {times}, turn end {env and env['world']['time']}",
    )
    R.check(
        "6.2",
        env and env["returned"]["last"] == ["fs.write", "LOCKED", False],
        "the ledger record of a call exists before its result returns: world.history right after the refused write shows that record",
    )
    R.check(
        "6.4",
        env and env["returned"] == expected,
        f"returned equals the script's top-level return value serialised through the Build's SDK codec: {canonical(env['returned'])[:120] if env else None}",
    )

    # Failures before execute leave state unchanged and do not tick; validation precedes preconditions.
    before = world.state(iid)
    t_before = world.ledger(iid)[-1]["world_time"]
    script = """import fs, cwr
out = []
for f in [lambda: fs.write(path='/a', content=b'q'), lambda: fs.delete(path='/missing'), lambda: fs.delete(path=7), lambda: fs.move(src='/a', dst='/b'), lambda: fs.write(path='/a', content='not bytes')]:
    try:
        f()
        out.append('ok')
    except cwr.errors.WorldError as e:
        out.append([e.code, e.precondition_index])
return out
"""
    _rc, env, _ = world.turn(iid, script)
    recs = world.turn_records(iid, env["world"]["turn"]) if env else []
    after = world.state(iid)
    ok = (
        env
        and env["returned"]
        == [["LOCKED", 1], ["NOT_FOUND", 1], ["INVALID", None], ["LOCKED", 2], ["INVALID", None]]
        and before == after
        and all(r["world_time"] == t_before for r in recs)
        and recs[2]["preconditions"] == []
        and recs[4]["preconditions"] == []
        and env["world"]["time"] == t_before + 10
    )
    R.check(
        "6.2",
        ok,
        f"failures at validation or preconditions leave state unchanged ({before == after}), do not tick, and a type error is INVALID before any precondition runs (ledger preconditions [] for fs.delete(path=7)): {env and env['returned']}",
    )

    # Durable before return, observed with strace: every response write on the RPC socket follows an fsync of
    # the ledger after the last ledger write.
    if shutil.which("strace"):
        out = world.scratch / "strace.txt"
        f = world.scratch / "strace-turn.py"
        f.write_text(
            "import fs, world\nfor i in range(4):\n    fs.write(path='/s%d' % i, content=b'1')\nworld.now()\ntry:\n    fs.delete(path='/none')\nexcept Exception:\n    pass\n"
        )
        p = run(
            [
                "strace",
                "-f",
                "-y",
                "-qq",
                "-s",
                "0",
                "-e",
                "trace=write,sendto,sendmsg,fsync,fdatasync",
                "-o",
                out,
                world.cwr,
                "instance",
                "turn",
                iid,
                "--home",
                world.home,
                "--script",
                f,
            ]
        )
        seq = []
        lines = out.read_text().splitlines()
        # The kernel is the process that writes the ledger; its RPC responses are sends on a unix socket.
        main_pid = next(
            (ln.split()[0] for ln in lines if "ledger.jsonl>" in ln and " write(" in ln), ""
        )
        for line in lines:
            m = re.match(r"(\d+)\s+(write|sendto|sendmsg|fsync|fdatasync)\((\d+)<([^>]*)>", line)
            if not m or m.group(1) != main_pid:
                continue
            what, path = m.group(2), m.group(4)
            if path.endswith("ledger.jsonl"):
                seq.append("L" if what == "write" else "F")
            elif ("UNIX" in path or path.startswith("socket:")) and what in (
                "write",
                "sendto",
                "sendmsg",
            ):
                seq.append("R")
        pending, violations, responses = False, 0, 0
        for s in seq:
            if s == "L":
                pending = True
            elif s == "F":
                pending = False
            elif s == "R":
                responses += 1
                if pending:
                    violations += 1
        R.check(
            "6.2",
            p.returncode == 0 and responses >= 7 and violations == 0 and seq.count("L") >= 6,
            f"strace: {seq.count('L')} ledger writes, {seq.count('F')} ledger fsyncs, {responses} RPC writes (start frame plus one response per call); responses sent while a ledger write was unsynced: {violations}",
        )
    else:
        R.check("6.2", False, "strace not available to observe durability before return")

    # 6.3 partial: successful mutations stand, nothing rolled back, full trace.
    iid2 = world.create(ctl)
    _rc, env, _ = world.turn(
        iid2,
        "import fs\nfs.write(path='/p1', content=b'1')\nfs.write(path='/p2', content=b'2')\nfs.delete(path='/nope')\nfs.write(path='/p3', content=b'3')\n",
    )
    files = sorted(world.state(iid2)["fs"]["files"])
    R.check(
        "6.3",
        env
        and env["status"] == "partial"
        and err(env)[:3] == ("NOT_FOUND", "fs.delete", 1)
        and files == ["/p1", "/p2"]
        and [(t["primitive"], t["code"]) for t in env["trace"]]
        == [("fs.write", None), ("fs.write", None), ("fs.delete", "NOT_FOUND")],
        f"uncaught failure after mutations: status {env and env['status']}, error {err(env)}, trace {env and [(t['primitive'], t['code']) for t in env['trace']]}, state files {files} (exactly the successful mutations)",
    )
    _rc, env, _ = world.turn(
        iid2,
        "import fs\nreturn [fs.exists(path='/p1'), fs.exists(path='/p2'), fs.exists(path='/p3')]\n",
    )
    R.check(
        "6.3",
        env and env["returned"] == [True, True, False],
        f"the next turn sees the partial turn's mutations: {env and env['returned']}",
    )
    _rc, env, _ = world.turn(iid2, "import fs\nfs.exists(path='/x')\nfs.delete(path='/nope')\n")
    R.check(
        "6.3",
        env and env["status"] == "error" and err(env)[0] == "NOT_FOUND",
        f"uncaught failure with no mutation: {summary(env)}",
    )
    _rc, env, _ = world.turn(iid2, "import fs\nfs.write(path='/q', content=b'1')\n1 / 0\n")
    R.check(
        "6.3",
        env
        and env["status"] == "partial"
        and err(env)[:2] == ("INVALID", "turn.script")
        and "ZeroDivisionError" in env["error"]["detail"],
        f"an uncaught non-world exception after a mutation is partial: {summary(env)}",
    )
    _rc, env, _ = world.turn(
        iid2, "from cwr.errors import Locked\nraise Locked('fs.write', 1, None, None, 'forged')\n"
    )
    R.check(
        "6.3",
        env and err(env)[:2] == ("INVALID", "turn.script"),
        f"a failure the kernel never returned is not reported as a world failure: {summary(env)}",
    )
    _rc, env, _ = world.turn(iid2, "import fs\nfs.write(path='/r', content=b'1')\nreturn {1, 2}\n")
    R.check(
        "6.4",
        env
        and env["status"] == "partial"
        and err(env)[:2] == ("INVALID", "turn.return")
        and [t["primitive"] for t in env["trace"]] == ["fs.write"]
        and world.state(iid2)["fs"]["files"].get("/r") is not None,
        f"a return value outside the codec after a mutation is partial: {summary(env)} with the "
        "mutation applied and in the trace (kernel.md section 5, decisions.md C01)",
    )
    _rc, env, _ = world.turn(iid2, "import fs\nfs.exists(path='/r')\nreturn {1, 2}\n")
    R.check(
        "6.4",
        env and env["status"] == "error" and err(env)[:2] == ("INVALID", "turn.return"),
        f"a return value outside the codec with nothing changed is error: {summary(env)}",
    )

    # 6.4 envelope shape over every envelope of the run, against the Build's codec and the ledger.
    shape_bad = []
    for inst, e in world.envelopes:
        try:
            codec.check_turn_envelope(e)
        except Exception as ex:
            shape_bad.append(str(ex)[:80])
            continue
        turns = [
            r for r in world.ledger(inst) if r["kind"] == "turn" and r["turn"] == e["world"]["turn"]
        ]
        b = Path(
            json.loads((world.home / "instances" / inst / "instance.json").read_text())["build_dir"]
        )
        if set(e) != {"status", "returned", "error", "trace", "world", "resources"} or not turns:
            shape_bad.append("keys")
            continue
        t = turns[0]
        mf = (b / "manifest.kvx").read_text()
        ver = re.search(r'^version = "([^"]+)"', mf, re.M)
        if (
            e["world"]["build"] != "sha256:" + build_hash(b)
            or e["world"]["time"] != t["world_time"]
            or t["status"] != e["status"]
            or t["returned_hash"] != sha(canonical(e["returned"]))
            or (ver and e["world"]["version"] != ver.group(1))
        ):
            shape_bad.append(f"world/ledger mismatch turn {e['world']['turn']}")
    R.check(
        "6.4",
        world.envelopes and not shape_bad,
        f"{len(world.envelopes)} envelopes: exactly {{status, returned, error, trace, world, resources}}, status in ok/error/partial, world = {{version, build, time, turn}} equal to the Build and the ledger turn record, returned_hash = sha256(canonical(returned)); problems={shape_bad[:5]}",
    )


def check_defects(world: World, defect: Path) -> None:
    """1.6 and the debug-mode effect assertion of 6.2."""
    iid = world.create(defect)
    _rc, env, _ = world.turn(
        iid,
        "import fs, cwr\ntry:\n    fs.exists(path='/defect/undeclared')\nexcept cwr.errors.InvariantViolation as e:\n    return [e.code, e.detail]\n",
    )
    rec = world.turn_records(iid, 1)[-1] if env else {}
    R.check(
        "1.6",
        env
        and env["returned"]
        and env["returned"][0] == "INVARIANT_VIOLATION"
        and "does not declare LOCKED" in env["returned"][1]
        and rec.get("outcome", {}).get("code") == "INVARIANT_VIOLATION"
        and "defect" in rec
        and "trace" in rec,
        f"a read_only lib defect emitting undeclared LOCKED after preconditions passed: recorded INVARIANT_VIOLATION with defect text and trace in the ledger ({rec.get('defect', '')[:70]})",
    )
    _rc, env, _ = world.turn(
        iid,
        "import fs\nfs.write(path='/fine', content=b'1')\nfs.write(path='/defect/mutating', content=b'1')\nfs.write(path='/never', content=b'1')\n",
    )
    recs = world.turn_records(iid, env["world"]["turn"]) if env else []
    seal = world.home / "instances" / iid / "sealed.json"
    files = sorted(world.state(iid)["fs"]["files"])
    rc2, env2, stderr2 = world.turn(iid, "return 1\n")
    R.check(
        "1.6",
        env
        and err(env)[:2] == ("INVARIANT_VIOLATION", "fs.write")
        and recs[-1]["outcome"]["code"] == "INVARIANT_VIOLATION"
        and recs[-1].get("sealed") is True
        and seal.is_file()
        and files == ["/fine"]
        and rc2 != 0
        and env2 is None
        and "sealed" in stderr2,
        f"a mutating lib defect: {summary(env)}, defect state not committed (files {files}), Instance sealed and the next turn refused (exit {rc2}): counted as a defect, never an ordinary outcome",
    )
    # Effects asserted only in debug mode.
    iid = world.create(defect)
    _rc, env, _ = world.turn(
        iid, "import fs\nfs.write(path='/defect/effect', content=b'1')\nreturn 'no debug'\n"
    )
    iid_d = world.create(defect)
    _rc, env_dbg, _ = world.turn(
        iid_d,
        "import fs\nfs.write(path='/defect/effect', content=b'1')\nreturn 'debug'\n",
        "--debug-effects",
    )
    rec = world.turn_records(iid_d, 1)[-1] if env_dbg else {}
    R.check(
        "6.2",
        env
        and env["status"] == "ok"
        and env_dbg
        and err(env_dbg)[0] == "INVARIANT_VIOLATION"
        and "effect `fs.exists(path)`" in rec.get("defect", ""),
        f"step 4 asserts declared effects in debug mode: without debug {summary(env)}; with --debug-effects {summary(env_dbg)} ({rec.get('defect', '')[:60]})",
    )
    R.check(
        "1.6",
        env_dbg and (world.home / "instances" / iid_d / "sealed.json").is_file(),
        "an effect that does not hold is INVARIANT_VIOLATION and seals the Instance",
    )


DETERMINISM_SCRIPT = """import fs, kv, jobs, world, search, publish, cwr
calls = [
    ('fs.exists', lambda: fs.exists(path='/a')), ('fs.exists', lambda: fs.exists(path='/a')),
    ('fs.write', lambda: fs.write(path='/a', content=b'abc')), ('fs.write', lambda: fs.write(path='/a', content=b'abc')),
    ('fs.locked', lambda: fs.locked(path='/a')), ('fs.locked', lambda: fs.locked(path='/a')),
    ('fs.size', lambda: fs.size(path='/a')), ('fs.size', lambda: fs.size(path='/a')),
    ('fs.list', lambda: fs.list(prefix='/')), ('fs.list', lambda: fs.list(prefix='/')),
    ('fs.read', lambda: fs.read(path='/a')), ('fs.read', lambda: fs.read(path='/a')),
    ('fs.read', lambda: fs.read(path='/zz')), ('fs.read', lambda: fs.read(path='/zz')),
    ('fs.lock', lambda: fs.lock(path='/a')), ('fs.lock', lambda: fs.lock(path='/a')),
    ('fs.write', lambda: fs.write(path='/a', content=b'x')), ('fs.write', lambda: fs.write(path='/a', content=b'x')),
    ('fs.move', lambda: fs.move(src='/a', dst='/b')), ('fs.move', lambda: fs.move(src='/a', dst='/b')),
    ('fs.delete', lambda: fs.delete(path='/a')), ('fs.delete', lambda: fs.delete(path='/a')),
    ('fs.unlock', lambda: fs.unlock(path='/a')), ('fs.unlock', lambda: fs.unlock(path='/a')),
    ('fs.move', lambda: fs.move(src='/a', dst='/b')), ('fs.delete', lambda: fs.delete(path='/b')), ('fs.delete', lambda: fs.delete(path='/b')),
    ('kv.has', lambda: kv.has(ns='n', key='k')), ('kv.has', lambda: kv.has(ns='n', key='k')),
    ('kv.set', lambda: kv.set(ns='n', key='k', value=b'v')), ('kv.set', lambda: kv.set(ns='n', key='k', value=b'v')),
    ('kv.get', lambda: kv.get(ns='n', key='k')), ('kv.get', lambda: kv.get(ns='n', key='k')),
    ('kv.keys', lambda: kv.keys(ns='n')), ('kv.keys', lambda: kv.keys(ns='n')), ('kv.used', lambda: kv.used(ns='n')), ('kv.used', lambda: kv.used(ns='n')),
    ('kv.del', lambda: kv.del_(ns='n', key='k')), ('kv.del', lambda: kv.del_(ns='n', key='k')),
    ('jobs.submit', lambda: jobs.submit(kind='k', n=2)), ('jobs.known', lambda: jobs.known(id='j1')), ('jobs.known', lambda: jobs.known(id='j1')),
    ('jobs.status', lambda: jobs.status(id='j1')), ('jobs.status', lambda: jobs.status(id='j1')), ('jobs.active', lambda: jobs.active()), ('jobs.active', lambda: jobs.active()),
    ('jobs.result', lambda: jobs.result(id='j1')), ('jobs.result', lambda: jobs.result(id='j1')),
    ('jobs.cancel', lambda: jobs.cancel(id='j1')), ('jobs.cancel', lambda: jobs.cancel(id='j9')), ('jobs.cancel', lambda: jobs.cancel(id='j9')),
    ('world.now', lambda: world.now()), ('world.version', lambda: world.version()), ('world.version', lambda: world.version()),
    ('world.seed', lambda: world.seed()), ('world.seed', lambda: world.seed()), ('world.limits', lambda: world.limits()),
    ('world.random', lambda: world.random()), ('world.random', lambda: world.random()),
    ('world.history', lambda: world.history(n=3)), ('world.history', lambda: world.history(n=900)), ('world.history', lambda: world.history(n=900)),
    ('world.propose', lambda: world.propose(text='more tools please')),
    ('search.query', lambda: search.query(q='tennis', limit=3)), ('search.query', lambda: search.query(q='', limit=3)), ('search.query', lambda: search.query(q='', limit=3)),
    ('publish.post', lambda: publish.post(key='k1', title='t', body='b')), ('publish.status', lambda: publish.status(post_id='p1')),
    ('publish.delete', lambda: publish.delete(post_id='p1')),
]
out = []
for name, f in calls:
    try:
        v = f()
        out.append([name, 'ok', None, type(v).__name__])
    except cwr.errors.WorldError as e:
        out.append([name, 'error', e.code, None])
return out
"""


def check_determinism(world: World, ctl: Path) -> None:
    """1.3: equal parameters under equal visible conditions give equal (status, shape, side-effect class)."""
    con = contracts(ctl)
    a, b = world.create(ctl, seed=7), world.create(ctl, seed=7)
    _, ea, _ = world.turn(a, DETERMINISM_SCRIPT)
    _, eb, _ = world.turn(b, DETERMINISM_SCRIPT)
    covered = {r[0] for r in (ea or {}).get("returned") or []}

    def outcomes(iid):
        return [
            (
                r["primitive"],
                r["params_hash"],
                r["outcome"]["status"],
                r["outcome"]["code"],
                r["outcome"]["shape"],
                r["outcome"]["side_effect"],
            )
            for r in world.turn_records(iid, 1)
        ]

    oa, ob = outcomes(a), outcomes(b)

    def strip(e: dict) -> dict:
        return {k: v for k, v in e.items() if k != "resources"} | {
            "world": {k: v for k, v in e["world"].items() if k != "build"}
        }

    R.check(
        "1.3",
        ea
        and eb
        and covered == set(con)
        and oa == ob
        and len(oa) == len(ea["returned"])
        and strip(ea) == strip(eb),
        f"every one of the {len(con)} refworld primitives and adapters called on two Instances with equal seed and visible conditions: {len(oa)} ledger outcomes (status, code, shape, side_effect) identical, envelopes identical except host resources",
    )
    # Back-to-back repetitions within one Instance: same primitive, same params, read_only, so the visible
    # conditions are equal (world time aside, which no repeated primitive here reads).
    pairs, unequal = 0, []
    for r1, r2 in itertools.pairwise(oa):
        if (
            r1[:2] == r2[:2]
            and r1[5] == "read_only"
            and r1[0] not in ("world.now", "world.limits", "world.history")
        ):
            pairs += 1
            if r1[2:] != r2[2:]:
                unequal.append(r1[0])
    R.check(
        "1.3",
        pairs >= 15 and not unequal,
        f"{pairs} back-to-back repetitions of read_only calls gave equal outcomes; unequal={unequal}",
    )
    labels = {t["primitive"]: t["content_guaranteed"] for t in (ea or {}).get("trace", [])}
    adapters = [n for n, c in con.items() if c["side_effect"] == "external"]
    R.check(
        "1.3",
        all(con[n]["content_guaranteed"] is False and labels.get(n) is False for n in adapters)
        and all(v is True for n, v in labels.items() if n not in adapters),
        f"payload equality is not claimed for adapter-backed primitives: {adapters} are content_guaranteed false in the contracts and in every envelope trace item",
    )


def _stderr_of_guard_off(world: World, iid: str) -> str:
    """The sandbox line `cwr instance turn` prints for a turn with the language guard off."""
    _rc, _env, stderr = world.turn(iid, "return 1\n", "--diagnostic-no-language-guard")
    return next((ln for ln in stderr.splitlines() if "sandbox=" in ln), "")


def check_conflict_and_stdin(world: World, ctl: Path) -> None:
    """6.4: a turn against a busy Instance is refused with CONFLICT before anything executes;
    6.1: standard input that opens a code fence is a model message under the turn protocol."""
    iid = world.create(ctl)
    world.n += 1
    spin_file = world.scratch / f"turn-{world.n}.py"
    spin_file.write_text("import fs\nfs.write(path='/spin', content=b'1')\nwhile True:\n    pass\n")
    cmd = [world.cwr, "instance", "turn", iid, "--home", world.home, "--script", spin_file]
    spin = subprocess.Popen(
        [str(c) for c in cmd], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    meta = world.home / "instances" / iid / "instance.json"
    busy = False
    for _ in range(400):
        try:
            busy = json.loads(meta.read_text()).get("status") == "in_turn"
        except (OSError, ValueError):
            busy = False
        if busy:
            break
        time.sleep(0.05)
    second = run([world.cwr, "instance", "turn", iid, "--home", world.home], input="return 1\n")
    spin.communicate(timeout=TIMEOUT)
    try:
        refusal = json.loads(second.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        refusal = {}
    err_ = refusal.get("error") or {}
    turns = [r for r in world.ledger(iid) if r["kind"] == "turn"]
    R.check(
        "6.4",
        busy
        and second.returncode == 8
        and refusal.get("status") == "error"
        and err_.get("code") == "CONFLICT"
        and err_.get("primitive") == "kernel.instance"
        and refusal.get("trace") == []
        and len(turns) == 1,
        f"a second `cwr instance turn` while the first is in its turn exits {second.returncode} with "
        f"{err_.get('code')}/{err_.get('primitive')} on stdout and records nothing ({len(turns)} turn record)",
    )
    message = "I will read the clock.\n\n```python\nimport world\nreturn world.now()\n```\n"
    ok = run([world.cwr, "instance", "turn", iid, "--home", world.home], input=message)
    two = "```python\na = 1\n```\n```python\nb = 2\n```\n"
    refused = run([world.cwr, "instance", "turn", iid, "--home", world.home], input=two)
    try:
        env_ok = json.loads(ok.stdout.strip().splitlines()[-1])
        env_refused = json.loads(refused.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        env_ok, env_refused = {}, {}
    R.check(
        "6.1",
        ok.returncode == 0
        and env_ok.get("status") == "ok"
        and isinstance(env_ok.get("returned"), int)
        and refused.returncode == 4
        and (env_refused.get("error") or {}).get("primitive") == "turn.protocol"
        and env_refused.get("world", {}).get("time") == env_ok.get("world", {}).get("time"),
        f"a fenced message on stdin runs its one python block (exit {ok.returncode}, status "
        f"{env_ok.get('status')}); two blocks on stdin are a turn.protocol refusal with exit "
        f"{refused.returncode} and no tick",
    )


def check_audit(world: World, excluded: set[str]) -> None:
    """1.3: the kernel.md section 8 determinism audit over every ledger this driver produced."""
    groups: dict[tuple, set] = {}
    n = 0
    for d in sorted((world.home / "instances").iterdir()):
        for r in world.ledger(d.name):
            if r["kind"] not in ("primitive", "adapter") or r.get("limit") in (
                "primitive_calls",
                "adapter_calls",
            ):
                continue  # budget refusals depend on the visible world.limits() counters, not on params
            if r["build"] in excluded:
                continue  # a Build with injected defects is excluded, as kernel.md section 4 excludes any defective run
            key = (r["build"], r["primitive"], r["params_hash"], canonical(r["preconditions"]))
            groups.setdefault(key, set()).add(canonical(r["outcome"]))
            n += 1
    split = [k[1] for k, v in groups.items() if len(v) > 1]
    R.check(
        "1.3",
        n > 100 and not split,
        f"audit over all ledgers: {n} primitive records in {len(groups)} (build, primitive, params_hash, preconditions) groups, each with one outcome; groups with several={split}",
    )


def check_rules_frozen(world: World, ctl: Path) -> None:
    """1.2: contracts, preconditions, effects, limits and codes do not change for a running Instance."""
    digest0, hash0 = tree_digest(ctl), build_hash(ctl)
    iid = world.create(ctl)
    world.turn(iid, "import fs\nfs.write(path='/x', content=b'1')\nfs.lock(path='/x')\n")
    probe = (
        "import fs, world, cwr\ntry:\n    fs.write(path='/x', content=b'2')\nexcept cwr.errors.WorldError as e:\n    r = [e.code, e.precondition_index]\n"
        "l = world.limits()\nreturn [r, l['primitive_calls'], l['cpu_ms'], l['tick_per_primitive']]\n"
    )
    hostile = "import fs, cwr\nfs.write = lambda **k: None\ncwr._rpc.call('fs.write', {'path': '/x', 'content': {'$bytes': 'Mg=='}})\n"
    results = []
    for _ in range(6):
        results.append(world.turn(iid, probe)[1]["returned"])
        world.turn(iid, hostile)
    same = all(r == results[0] for r in results) and results[0] == [["LOCKED", 1], 500, 5000, 1]
    unchanged = tree_digest(ctl) == digest0 and build_hash(ctl) == hash0 and ctl.name == hash0
    ro = all(not (os.stat(p).st_mode & 0o222) for p in ctl.rglob("*") if p.is_file())
    R.check(
        "1.2",
        same and unchanged and ro,
        f"across 13 turns, including scripts that monkeypatch the SDK and send raw RPCs, the rule outcome and limits stay {results[0]}; Build bytes and hash unchanged ({hash0[:12]}), every Build file read-only",
    )
    # A modified Build is refused rather than loaded into a running Instance.
    copy = world.scratch / "tampered-build"
    shutil.copytree(ctl, copy)
    for p in copy.rglob("*"):
        os.chmod(p, 0o755 if p.is_dir() else 0o644)
    iid2 = world.create(copy)
    n_before = len(world.ledger(iid2))
    c = copy / "contracts.json"
    c.write_text(c.read_text().replace('"len(content) <= 1048576"', '"len(content) <= 1048575"'))
    rc, env, stderr = world.turn(iid2, "return 1\n")
    R.check(
        "1.2",
        rc == 2 and env is None and len(world.ledger(iid2)) == n_before and "hash" in stderr,
        f"editing a precondition in a Build under a running Instance is refused by hash verification (exit {rc}), nothing executes; change requires an explicit upgrade to another Build",
    )


def home_root_dir() -> str | None:
    """Where the driver keeps its Instance home (see the module docstring)."""
    if os.environ.get("CWR_DRIVER_HOME_ROOT"):
        return os.environ["CWR_DRIVER_HOME_ROOT"]
    try:
        mounts = Path("/proc/mounts").read_text().split("\n")
    except OSError:
        return None
    for ln in mounts:
        f = ln.split()
        if (
            len(f) >= 3
            and f[1] == "/dev/shm"
            and f[2] == "tmpfs"
            and os.access("/dev/shm", os.W_OK)
        ):
            return "/dev/shm"
    return None


def main() -> int:
    cwr, tests_ok, tests_ev = build_binaries()
    for c in CLAUSES:
        R.check(c, tests_ok, tests_ev)
    scratch = Path(tempfile.mkdtemp(prefix="cwr-2_2-"))
    home_root = Path(tempfile.mkdtemp(prefix="cwr-2_2-home-", dir=home_root_dir()))
    try:
        world = World(cwr, scratch, home_root / "home")
        ctl = world.build("control")

        def set_limits(values: dict):
            def mutate(src: Path) -> None:
                w = src / "world.kvx"
                text = w.read_text()
                for k, v in values.items():
                    text, n = re.subn(rf"^{k} = \d+$", f"{k} = {v}", text, flags=re.M)
                    if n != 1:
                        raise SystemExit(f"world.kvx has no single {k} line to set")
                w.write_text(text)

            return mutate

        def inject_defects(src: Path) -> None:
            p = src / "lib/fs.py"
            s = p.read_text()
            s = s.replace(
                "def exists(state: Any, path: str) -> bool:\n    return",
                'class _InjectedDefect(Exception):\n    code = "LOCKED"\n\n\ndef exists(state: Any, path: str) -> bool:\n'
                '    if path == "/defect/undeclared":\n        raise _InjectedDefect("injected by the task 2.2 driver")\n    return',
            )
            s = s.replace(
                "def write(state: Any, path: str, content: bytes) -> None:\n",
                'def write(state: Any, path: str, content: bytes) -> None:\n    if path == "/defect/mutating":\n'
                '        raise _InjectedDefect("injected by the task 2.2 driver")\n    if path == "/defect/effect":\n        return None\n',
            )
            if "_InjectedDefect" not in s or "/defect/effect" not in s:
                raise SystemExit("defect injection did not apply to lib/fs.py")
            p.write_text(s)

        tight = world.build("tight", set_limits(TIGHT))
        budget = world.build("budget", set_limits(BUDGET))
        defect = world.build("defect", inject_defects)
        for b in (ctl, tight, budget, defect):
            if b.name != build_hash(b):
                raise SystemExit(f"independent Build hash mismatch for {b}")
        with (
            OBS.observe("CWR-007.a1", match="closure probes|only the Build SDK|closure refusals"),
            OBS.observe("CWR-007.a2", match="closure probes|unreachable even through|enumerates"),
            OBS.observe("CWR-007.a3", match="closure probes|network"),
            OBS.observe("CWR-007.a4", match="closure probes|filesystem|host paths"),
            OBS.observe("CWR-007.a5", match="closure probes|clock"),
        ):
            check_closure(world, ctl)
        with OBS.observe("CWR-007.a6"):
            check_limits(world, tight, budget)
        with OBS.observe("CWR-007.a1", match="closure walks"):
            check_hostile_runner(world, ctl)
        with OBS.observe("CWR-008.a2"):
            check_preconditions(world, ctl)
        check_visibility(world, ctl)
        with (
            OBS.observe("CWR-008.a1", match="leave state unchanged"),
            OBS.observe("CWR-008.a2", match="precondition failure raised the typed"),
            OBS.observe(
                "CWR-008.a3", match="partial|uncaught failure after mutations|next turn sees"
            ),
        ):
            check_atomic_and_script(world, ctl)
        with OBS.observe("CWR-008.a5", match="INVARIANT_V|undeclared"):
            check_defects(world, defect)
        with OBS.observe("CWR-008.a4", match="repetitions|equal se"):
            check_determinism(world, ctl)
        with OBS.observe("CWR-007.a7"):
            check_rules_frozen(world, ctl)
        check_conflict_and_stdin(world, ctl)
        check_audit(world, {"sha256:" + defect.name})
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        shutil.rmtree(home_root, ignore_errors=True)
    OBS.emit()
    failed = 0
    for c in CLAUSES:
        if R.bad[c]:
            failed += 1
            print(f"CLAUSE {c} FAIL " + " | ".join(R.bad[c]))
        else:
            print(f"CLAUSE {c} PASS " + " | ".join(R.ok[c]))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

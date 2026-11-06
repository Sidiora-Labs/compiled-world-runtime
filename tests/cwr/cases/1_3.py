#!/usr/bin/env python3
# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Task 1.3 driver: Build packaging, manual generation and Python SDK generation.

Case CWR-005; clauses 1.4, 2.4, 2.5, 4.5, 4.6 and 9.2 of spec/cwr/spec.kvx.

Builds the `cwr` binary with cargo and runs the unit tests of the crates this task touches
(cwr-compiler, cwr-cli), then drives the real `cwr build`, `cwr manual` and `cwr instance create`
binaries (and `cwr instance turn`) as subprocesses over copies of examples/refworld (README.md removed, the only file outside
the World Source grammar):

* two compilations in two differing working directories on this host (different cwd, relative
  versus absolute paths, source directory names, HOME, USER, HOSTNAME, TZ, LANG, umask, source mtimes,
  start time), and a third compilation on a second host: a Docker container with a different OS
  userland (Debian bookworm), libc, Rust toolchain (rustc and cargo built from the same source there),
  Python, hostname, time zone, locale, umask and paths. Every in-hash file must be byte-identical and
  the three hashes equal;
* variants that change one source byte outside the compiled surface (a lib/ comment) or only
  manual/ prose: the hash must change, and contracts.json, manual.md and sdk/ must change exactly
  as a function of contracts.json and manual/;
* tampered copies of a packaged Build (one byte in each in-hash file class including tests.json,
  added files and directories at the Build root and below it, empty directories, symbolic links,
  removed files and directories, the recorded hash): the Build hash is recomputed independently here
  with the compiler.md section 7 algorithm, and both `cwr manual` (exit 5) and the kernel's
  `cwr instance create` (exit 2) must refuse each copy;
* the manual is checked against the World Source read with a KVX reader of this driver's own (not
  against the compiler's output), entry by entry;
* the generated SDK is imported from the Build's sdk/ by a separate Python process and run against a
  unix-socket RPC server in this driver that plays the kernel side of the pinned framing; the server
  counts frames, so "exactly one RPC per call" is observed, not assumed. The server takes each
  primitive's content_guaranteed label from the World Source KVX files (never from contracts.json or
  the SDK), and a second run answers every primitive with the label true, false and missing in turn:
  the SDK must accept exactly the World Source label and refuse the other two;
* the label end to end: real sandboxed kernel turns (`cwr instance turn`) call every primitive and,
  through the local stub servers of benchmark/stubs (never a live service), every adapter with ok and
  error outcomes; every per-call result in the turn envelopes must carry the Build's label;
* every generated SDK file carries the template source header exactly once, at the top, with no
  duplicated boilerplate; a Build made writable again without a byte changed is refused by
  `cwr manual` (the tampered copies above must be refused by hash verification, not by that check).

Prints one line per clause, `CLAUSE <req.ac> PASS|FAIL <evidence>`, and exits 0 only when every
clause passes. Honours CARGO_TARGET_DIR; defaults to the workspace target dir.
Python 3.12, standard library only.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
REFWORLD = REPO / "examples/refworld"
CLAUSES = ["1.4", "2.4", "2.5", "4.5", "4.6", "9.2"]
TIMEOUT = 1200
CODES = [
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
SECTIONS = [
    "1. What this world is",
    "2. How a turn works",
    "3. The failure envelope",
    "4. Limits and clock",
    "5. Modules overview",
    "6. Primitive reference",
    "7. Adapters",
    "8. Changes and versions",
    "9. Proposals",
    "10. Index",
]
IN_HASH_FILES = ["manifest.kvx", "contracts.json", "contract_diff.json", "manual.md", "tests.json"]
IN_HASH_DIRS = ["sdk", "lib", "adapters", "fixtures", "migrations"]
LAYOUT = sorted(IN_HASH_FILES + IN_HASH_DIRS)
HOST2_BASE = "rust:1-slim-bookworm"
HOST2_IMAGE = "cwr-1_3-host2:bookworm"


class Results:
    def __init__(self) -> None:
        self.ok: dict[str, list[str]] = {c: [] for c in CLAUSES}
        self.bad: dict[str, list[str]] = {c: [] for c in CLAUSES}

    def check(self, clause: str, condition: bool, evidence: str) -> bool:
        (self.ok if condition else self.bad)[clause].append(evidence)
        return condition


R = Results()
SCRATCH = Path(tempfile.mkdtemp(prefix="cwr-1_3-"))
CWR = Path()


def run(cmd, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(c) for c in cmd], capture_output=True, text=True, timeout=TIMEOUT, **kw
    )


def target_dir() -> Path:
    env = os.environ.get("CARGO_TARGET_DIR")
    return Path(env) if env else REPO / "target"


def cargo() -> tuple[bool, str]:
    env = dict(os.environ)
    env.setdefault("CARGO_TARGET_DIR", str(target_dir()))
    b = run(["cargo", "build", "-p", "cwr-cli"], cwd=REPO, env=env)
    if b.returncode != 0:
        print(b.stderr[-4000:], file=sys.stderr)
        raise SystemExit("cargo build failed")
    t = run(
        ["cargo", "test", "-p", "cwr-compiler", "-p", "cwr-cli", "--", "--test-threads=1"],
        cwd=REPO,
        env=env,
    )
    out = t.stdout + t.stderr
    passed = sum(map(int, re.findall(r"test result: ok\. (\d+) passed", out)))
    return t.returncode == 0, (
        f"{passed} cwr-compiler and cwr-cli tests pass" if t.returncode == 0 else out[-3000:]
    )


# ------------------------------------------------------------------ independent oracles


def entries(d: Path) -> dict[str, bytes] | str:
    """Every hashed entry of a Build per compiler.md section 7, or the reason the directory is not a Build.

    Files under the layout map to their bytes; an empty directory maps to `<path>/` with no bytes. A
    top-level entry outside the layout, a listed entry of the wrong kind, a symbolic link or a special
    file anywhere makes the directory unverifiable."""
    out: dict[str, bytes] = {}
    for e in sorted(os.listdir(d)):
        st = os.lstat(d / e)
        if not (
            (e in IN_HASH_FILES and stat.S_ISREG(st.st_mode))
            or (e in IN_HASH_DIRS and stat.S_ISDIR(st.st_mode))
        ):
            return f"unexpected top-level entry {e!r}"
        if e in IN_HASH_FILES:
            out[e] = (d / e).read_bytes()
    for sub in IN_HASH_DIRS:
        if not (d / sub).is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(d / sub):
            rel = Path(dirpath).relative_to(d).as_posix()
            if not dirnames and not filenames:
                out[rel + "/"] = b""
            for n in dirnames + filenames:
                st = os.lstat(Path(dirpath) / n)
                if stat.S_ISLNK(st.st_mode) or not (
                    stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)
                ):
                    return f"link or special file {rel}/{n}"
            for n in filenames:
                out[f"{rel}/{n}"] = (Path(dirpath) / n).read_bytes()
    return out


RECORD_LINE = re.compile(rb'hash = "sha256:([0-9a-f]{64})"\n')


def split_manifest(data: bytes) -> tuple[bytes, str | None]:
    """compiler.md section 7 read literally: `manifest.kvx` "is hashed with its `hash` key omitted, then written
    last with the key present", and every other key enters the hash. So exactly the final line may be left out, and
    only when it is the record spelled as packaging writes it; every other byte (another `hash` key in any section,
    a re-spaced or commented record line, a missing newline) is hashed. Returns (hashed bytes, recorded hex)."""
    if not data.endswith(b"\n"):
        return data, None
    start = data.rfind(b"\n", 0, len(data) - 1) + 1
    m = RECORD_LINE.fullmatch(data, start)
    return (data[:start], m.group(1).decode()) if m else (data, None)


def manifest_record(d: Path) -> str | None:
    """The recorded Build hash, or None unless the manifest holds exactly one `hash` key, in [build], as its
    final line in the packaged spelling. Section tracking is this driver's own, line by line."""
    try:
        data = (d / "manifest.kvx").read_bytes()
    except OSError:
        return None
    _, rec = split_manifest(data)
    section, where = None, []
    for line in data.decode("utf-8", "replace").splitlines():
        s = line.strip()
        if re.fullmatch(r"\[([^\]]+)\]", s):
            section = s[1:-1]
        elif re.match(r"hash\s*=", s):
            where.append(section)
    return rec if where == ["build"] else None


def build_hash(d: Path) -> str:
    """compiler.md section 7, recomputed here: sorted in-hash paths; path, NUL, decimal length, NUL, bytes."""
    es = entries(d)
    if isinstance(es, str):
        return "unverifiable: " + es
    h = hashlib.sha256()
    for f in sorted(es, key=lambda s: s.encode()):
        data = es[f]
        if f == "manifest.kvx":
            data = split_manifest(data)[0]
        h.update(f.encode() + b"\0" + str(len(data)).encode() + b"\0" + data)
    return h.hexdigest()


def in_hash_files(d: Path) -> dict[str, bytes]:
    es = entries(d)
    if isinstance(es, str):
        raise SystemExit(f"{d}: {es}")
    return es


def kvx(path: Path) -> dict[str, dict]:
    """A minimal strict KVX reader of this driver's own: [section], key = <one JSON literal>, # comments."""
    doc: dict[str, dict] = {}
    cur = None
    for n, line in enumerate(path.read_text().splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = re.fullmatch(r"\[([^\]]+)\]", s)
        if m:
            cur = doc.setdefault(m.group(1), {})
            continue
        k, _, v = s.partition("=")
        if cur is None or not _:
            raise SystemExit(f"{path}:{n}: not KVX")
        cur[k.strip()] = json.loads(v.strip())
    return doc


def source_surface(src: Path) -> tuple[dict, list[str]]:
    """Every primitive and adapter of a World Source, read directly from its KVX files."""
    world = kvx(src / "world.kvx")
    modules = world["world"]["modules"]
    out = {}
    for kind, sub, prefix in (
        ("primitive", "contracts", "primitive."),
        ("adapter", "adapters", "adapter."),
    ):
        for f in sorted((src / sub).glob("*.kvx")):
            doc = kvx(f)
            for sec, body in doc.items():
                if sec.startswith(prefix):
                    name = sec[len(prefix) :]
                    out[name] = dict(body, name=name, kind=kind, module=doc["meta"]["module"])
    return out, modules


# ------------------------------------------------------------------ cwr helpers


def copy_world(dst: Path) -> Path:
    shutil.copytree(REFWORLD, dst, ignore=shutil.ignore_patterns("README.md", "__pycache__"))
    return dst


def cwr_build(src, out, cwd=None, env=None, extra=()) -> tuple[int, dict, str]:
    p = run([CWR, "build", src, "--json", "--out", out, *extra], cwd=cwd, env=env)
    try:
        rep = json.loads(p.stdout)
    except json.JSONDecodeError:
        rep = {}
    return p.returncode, rep, p.stdout + p.stderr


def built_dir(rep: dict) -> Path | None:
    b = rep.get("build") or {}
    return Path(b["dir"]) if b.get("dir") else None


def unseal(d: Path) -> None:
    for p in [d, *d.rglob("*")]:
        if p.is_dir():
            p.chmod(0o755)
        elif p.is_file():
            p.chmod(0o644)


def reseal(d: Path) -> None:
    """Clears every write bit again (symbolic links are left alone: chmod would follow them)."""
    for p in sorted([d, *d.rglob("*")], key=lambda x: len(x.parts), reverse=True):
        if p.is_symlink():
            continue
        p.chmod(0o555 if p.is_dir() else 0o444)


# The stderr phrase of `cwr manual` refusing a Build whose hash verifies but which is writable again.
WRITABLE_REFUSAL = "is writable after packaging"


def kernel_create(build: Path, home: Path) -> subprocess.CompletedProcess:
    return run([CWR, "instance", "create", "--home", home, "--build", build])


def manual_cmd(ref, out=None) -> subprocess.CompletedProcess:
    return run([CWR, "manual", ref, *(["--out", out] if out else [])])


# ------------------------------------------------------------------ RPC peer (kernel side of the framing)


class Peer:
    """Unix-socket server speaking the framing pinned in sdk/python/cwr/_rpc.py; answers as the kernel would."""

    def __init__(self, path: Path, contracts: dict, labels: dict[str, bool]):
        self.path = path
        self.table = {p["name"]: p for p in contracts["primitives"] + contracts["adapters"]}
        # content_guaranteed per primitive as declared in the World Source KVX (read by this driver),
        # never derived from contracts.json or from the SDK under test.
        self.labels = labels
        # When non-empty, each response takes its label from the head of this queue instead:
        # True, False or None (the key is omitted).
        self.label_queue: list[bool | None] = []
        self.requests: list[dict] = []
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(path))
        self.sock.listen(4)
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    @staticmethod
    def recv_exact(c, n):
        buf = b""
        while len(buf) < n:
            chunk = c.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    def respond(self, req: dict) -> dict:
        r = self._respond(req)
        if self.label_queue:
            v = self.label_queue.pop(0)
            if v is None:
                r.pop("content_guaranteed", None)
            else:
                r["content_guaranteed"] = v
        return r

    def _respond(self, req: dict) -> dict:
        p = self.table.get(req["primitive"])
        label = self.labels.get(req["primitive"], True)
        marker = next(
            (v for v in req["params"].values() if isinstance(v, str) and v.startswith("!")), None
        )
        if p is None:
            return {
                "status": "error",
                "content_guaranteed": True,
                "error": {
                    "code": "INVALID",
                    "primitive": req["primitive"],
                    "precondition_index": None,
                    "retry_after": None,
                    "limit": None,
                    "detail": "not in this Build",
                },
            }
        if marker == "!BADLABEL":
            return {"status": "ok", "value": [], "content_guaranteed": not label}
        if marker and marker[1:] in CODES:
            code = marker[1:]
            return {
                "status": "error",
                "content_guaranteed": label,
                "error": {
                    "code": code,
                    "primitive": req["primitive"],
                    "precondition_index": 1 if code == "NOT_FOUND" else None,
                    "retry_after": 7 if code == "RATE_LIMITED" else None,
                    "limit": "primitive_calls" if code == "LIMIT" else None,
                    "detail": f"scripted {code}",
                },
            }
        ret = p["returns"]
        value = {"$bytes": base64.b64encode(b"\x00\xff").decode()} if ret == "bytes" else None
        return {"status": "ok", "value": value, "content_guaranteed": label}

    def serve(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            with c:
                while True:
                    head = self.recv_exact(c, 4)
                    if head is None:
                        break
                    body = self.recv_exact(c, struct.unpack(">I", head)[0])
                    req = json.loads(body)
                    self.requests.append(req)
                    resp = json.dumps(
                        {"v": 1, "id": req["id"], "result": self.respond(req)}
                    ).encode()
                    c.sendall(struct.pack(">I", len(resp)) + resp)

    def close(self):
        self.sock.close()


CHILD = r"""
import importlib, inspect, json, sys
sys.path.insert(0, sys.argv[1])
plan = json.loads(sys.argv[2])
import cwr
from cwr import _rpc
DUMMY = {"str": "x", "int": 1, "bool": True, "bytes": b"\x00\xff", "Path": "/a", "None": None}
def dummy(t, i=0):
    if t == "str": return f"x{i}"
    if t == "Path": return f"/a{i}"
    if t == "int": return i + 1
    if t in DUMMY: return DUMMY[t]
    if t.startswith("list["): return []
    if t.startswith("dict["): return {}
    return {}
def resolve(name):
    mod_name, short = name.split(".", 1)
    mod = importlib.import_module(mod_name)
    return getattr(mod, short + "_" if not hasattr(mod, short) or short in ("del",) else short)
if plan.get("mode") == "labels":
    out = []
    for name, params in plan["primitives"]:
        fn = resolve(name)
        row = []
        for _ in range(3):
            try:
                try:
                    fn(*[dummy(t, i) for i, (_, t) in enumerate(params)])
                except cwr.WorldError:
                    pass
                row.append(["accepted", _rpc.last_result()["content_guaranteed"]])
            except _rpc.RpcError as e:
                row.append(["refused", str(e)[:120]])
        out.append([name, row])
    print(json.dumps({"rows": out, "total_rpcs": _rpc.calls_made()}))
    sys.exit(0)
report = {"calls": [], "errors": {}, "label": None, "badlabel": None, "signatures": {}, "rpcs_before_errors": None}
for name, params in plan["primitives"]:
    mod_name, short = name.split(".", 1)
    mod = importlib.import_module(mod_name)
    fn = getattr(mod, short + "_" if not hasattr(mod, short) or short in ("del",) else short)
    report["signatures"][name] = [fn.__name__, list(inspect.signature(fn).parameters)]
    before = _rpc.calls_made()
    try:
        value = fn(*[dummy(t, i) for i, (_, t) in enumerate(params)])
        r = ("ok", repr(type(value).__name__), value.hex() if isinstance(value, bytes) else None)
    except cwr.WorldError as e:
        r = ("raised", e.code, None)
    report["calls"].append([name, _rpc.calls_made() - before, r, _rpc.last_result()["content_guaranteed"]])
report["rpcs_before_errors"] = _rpc.calls_made()
import fs
for code in plan["codes"]:
    try:
        fs.read("!" + code)
        report["errors"][code] = None
    except cwr.WorldError as e:
        report["errors"][code] = [type(e).__name__, e.code, e.primitive, e.precondition_index, e.retry_after, e.limit,
                                  e.detail, isinstance(e, getattr(cwr, type(e).__name__))]
import search
search.query("tennis", 3)
report["label"] = _rpc.last_result()
try:
    search.query("!BADLABEL", 3)
    report["badlabel"] = "accepted"
except _rpc.RpcError as e:
    report["badlabel"] = "refused: " + str(e)
report["total_rpcs"] = _rpc.calls_made()
print(json.dumps(report))
"""


# ------------------------------------------------------------------ second host


HOST2_SCRIPT = r"""
set -e
umask 027
mkdir -p /opt/ws /home/carol9q
cp -r /repo/Cargo.toml /repo/Cargo.lock /repo/crates /repo/sdk /opt/ws/
cp -r /repo/examples/refworld /opt/carols-world
rm -f /opt/carols-world/README.md
find /opt/carols-world -name __pycache__ -prune -exec rm -rf {} +
cd /opt/ws
CARGO_TARGET_DIR=/tgt cargo build -q --locked -p cwr-cli >&2
{ echo "os=$(. /etc/os-release; echo $PRETTY_NAME)"; echo "libc=$(ldd --version | head -1)"; echo "rustc=$(rustc --version)";
  echo "python=$(python3 --version)"; echo "hostname=$(hostname)"; echo "tz=$TZ"; echo "lang=$LANG"; echo "umask=$(umask)"; } > /out/facts.txt
cd /home/carol9q
/tgt/debug/cwr build /opt/carols-world --json --out /out/builds
"""


def host_facts() -> dict[str, str]:
    def sh(c):
        return subprocess.run(c, shell=True, capture_output=True, text=True).stdout.strip()

    os_name = sh(". /etc/os-release; echo $PRETTY_NAME")
    return {
        "os": os_name,
        "libc": sh("ldd --version | head -1"),
        "rustc": sh("rustc --version"),
        "python": f"Python {sys.version.split()[0]}",
        "hostname": socket.gethostname(),
    }


def second_host_build() -> tuple[Path | None, dict, str]:
    """Clause 4.6: compile refworld on a second host, a Docker container with its own OS userland and toolchain."""
    if not shutil.which("docker") or run(["docker", "info"]).returncode != 0:
        return None, {}, "no second host: docker is not available to this driver"
    if run(["docker", "image", "inspect", HOST2_IMAGE]).returncode != 0:
        df = (
            f"FROM {HOST2_BASE}\nRUN apt-get update && apt-get install -y --no-install-recommends python3 "
            "&& rm -rf /var/lib/apt/lists/*\n"
        )
        b = subprocess.run(
            ["docker", "build", "-q", "-t", HOST2_IMAGE, "-"],
            input=df,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
        if b.returncode != 0:
            return None, {}, f"cannot build the second-host image: {b.stderr[-400:]}"
    out = SCRATCH / "host2"
    out.mkdir()
    p = run(
        [
            "docker",
            "run",
            "--rm",
            "--hostname",
            "hostgamma9q",
            "-e",
            "TZ=America/Los_Angeles",
            "-e",
            "LANG=C.UTF-8",
            "-e",
            "LC_ALL=C.UTF-8",
            "-e",
            "HOME=/home/carol9q",
            "-e",
            "USER=carol9q",
            "-v",
            f"{REPO}:/repo:ro",
            "-v",
            "cwr-1_3-host2-registry:/usr/local/cargo/registry",
            "-v",
            "cwr-1_3-host2-target:/tgt",
            "-v",
            f"{out}:/out",
            HOST2_IMAGE,
            "sh",
            "-c",
            HOST2_SCRIPT,
        ]
    )
    try:
        rep = json.loads(p.stdout)
    except json.JSONDecodeError:
        return (
            None,
            {},
            f"second-host build failed (exit {p.returncode}): {(p.stdout + p.stderr)[-600:]}",
        )
    facts = dict(
        ln.split("=", 1) for ln in (out / "facts.txt").read_text().splitlines() if "=" in ln
    )
    d = built_dir(rep)
    if p.returncode != 0 or not d:
        return None, facts, f"second-host cwr build exit {p.returncode}: {p.stdout[-400:]}"
    return out / "builds" / d.name, facts, ""


# ------------------------------------------------------------------ checks


def check_determinism() -> tuple[Path, Path, dict]:
    """Clause 4.6 (and 1.4 identity): two compilations in differing working directories."""
    a_cwd = SCRATCH / "hostA"
    a_cwd.mkdir()
    copy_world(a_cwd / "src-alpha")
    env_a = dict(
        os.environ,
        HOME="/home/alice7q",
        USER="alice7q",
        HOSTNAME="hostalpha7q",
        TZ="UTC",
        LANG="C",
        LC_ALL="C",
    )
    code_a, rep_a, raw_a = cwr_build("src-alpha", "out", cwd=a_cwd, env=env_a)
    time.sleep(1.2)
    b_cwd = SCRATCH / "hostB" / "deep" / "er"
    b_cwd.mkdir(parents=True)
    b_src = copy_world(b_cwd / "world-copy-beta")
    for i, p in enumerate(sorted(b_src.rglob("*"))):
        os.utime(p, (1_000_000_000 + i * 977, 1_000_000_000 + i * 977))
    env_b = dict(
        os.environ,
        HOME="/home/bob7q",
        USER="bob7q",
        HOSTNAME="hostbeta7q",
        TZ="Asia/Tokyo",
        LANG="en_US.UTF-8",
        LC_ALL="en_US.UTF-8",
        SOURCE_DATE_EPOCH="1",
    )
    old = os.umask(0o077)
    try:
        code_b, rep_b, raw_b = cwr_build(b_src, SCRATCH / "hostB-builds", cwd=b_cwd, env=env_b)
    finally:
        os.umask(old)
    da, db = built_dir(rep_a), built_dir(rep_b)
    if not (code_a == 0 and code_b == 0 and da and db):
        R.check("4.6", False, f"build A exit {code_a}, B exit {code_b}: {(raw_a + raw_b)[-600:]}")
        raise SystemExit("cannot continue without two Builds")
    da = (a_cwd / da) if not da.is_absolute() else da
    fa, fb = in_hash_files(da), in_hash_files(db)
    differing = sorted(k for k in set(fa) | set(fb) if fa.get(k) != fb.get(k))
    R.check(
        "4.6",
        da.name == db.name and not differing,
        f"two compilations (cwd/paths/env/umask/mtimes/time differ) -> sha256:{da.name[:16]}.. and "
        f"sha256:{db.name[:16]}..; {len(fa)} in-hash files byte-identical"
        + (f"; DIFFER {differing[:5]}" if differing else ""),
    )
    texts = b"\n".join(fa.values())
    leaks = [
        s
        for s in [
            str(SCRATCH),
            "alice7q",
            "bob7q",
            "hostalpha7q",
            "hostbeta7q",
            "hostA",
            "hostB",
            "src-alpha",
            "world-copy-beta",
        ]
        if s.encode() in texts
    ]
    R.check(
        "4.6",
        not leaks,
        f"no path, user, host or directory name in any in-hash file (searched {9} markers)"
        + (f"; LEAKED {leaks}" if leaks else ""),
    )
    # A second host: own OS userland, libc, Rust toolchain and Python; cwr compiled there from the same source.
    dc, facts, why = second_host_build()
    if dc is None:
        R.check("4.6", False, why)
    else:
        mine = host_facts()
        fc = in_hash_files(dc)
        diff_c = sorted(k for k in set(fa) | set(fc) if fa.get(k) != fc.get(k))
        differs = [
            k
            for k in ("os", "libc", "rustc", "python", "hostname")
            if facts.get(k) and facts.get(k) != mine.get(k)
        ]
        R.check(
            "4.6",
            dc.name == da.name and not diff_c and {"os", "rustc", "hostname"} <= set(differs),
            f"second host ({facts.get('os')}; {facts.get('rustc')}; {facts.get('python')}; {facts.get('libc')}; "
            f"hostname {facts.get('hostname')}; TZ {facts.get('tz')}; {facts.get('lang')}; umask {facts.get('umask')}) versus this host "
            f"({mine['os']}; {mine['rustc']}; {mine['python']}; {mine['libc']}): differing {differs}; cwr compiled on each host from the "
            f"same source; both Builds sha256:{dc.name[:16]}..; all {len(fc)} in-hash entries byte-identical"
            + (f"; DIFFER {diff_c[:5]}" if diff_c else ""),
        )
        texts_c = b"\n".join(fc.values())
        R.check(
            "4.6",
            not any(
                s.encode() in texts_c for s in ["carol9q", "hostgamma9q", "carols-world", "/opt/ws"]
            ),
            "no second-host path, user or host name in any in-hash file",
        )
    man = (da / "manifest.kvx").read_text()
    R.check(
        "4.6",
        not re.search(
            r"\d{4}-\d{2}-\d{2}T|\d{10}", re.sub(r"sha256:[0-9a-f]{64}|gen\.[0-9a-f]+", "", man)
        )
        and re.search(r'^compiler = "cwr-compiler [^"]+"$', man, re.M) is not None,
        "manifest.kvx carries no timestamp; compiler is the crate version plus generator digest",
    )
    # Rebuild into the same Build root: same hash, already built, nothing rewritten.
    code_c, rep_c, _raw_c = cwr_build("src-alpha", "out", cwd=a_cwd, env=env_a)
    R.check(
        "4.6",
        code_c == 0
        and (rep_c.get("build") or {}).get("hash") == f"sha256:{da.name}"
        and rep_c["build"]["already_built"],
        f"rebuild into the same root: exit {code_c}, same hash, reported already built",
    )
    return da, db, json.loads((da / "contracts.json").read_text())


def check_functions_of_contracts(base: Path) -> None:
    """Clauses 4.6 and 1.4: manual and SDK are functions of contracts.json and manual/; any byte changes the hash."""
    base_files = in_hash_files(base)
    sdk = {k: v for k, v in base_files.items() if k.startswith("sdk/")}
    # (a) one byte outside the compiled surface: a lib/ comment.
    src = copy_world(SCRATCH / "variant-lib")
    with open(src / "lib/fs.py", "a") as f:
        f.write("# variant\n")
    code, rep, raw = cwr_build(src, SCRATCH / "variant-lib-out")
    d = built_dir(rep)
    if code != 0 or not d:
        R.check("4.6", False, f"lib-comment variant did not build: {raw[-400:]}")
        return
    vf = in_hash_files(d)
    same = all(vf.get(k) == base_files[k] for k in ["contracts.json", "manual.md", *sdk])
    R.check(
        "4.6",
        same,
        "lib/ comment variant: contracts.json, manual.md and all sdk/ files byte-identical to the base Build",
    )
    R.check(
        "1.4",
        d.name != base.name and vf["lib/fs.py"] != base_files["lib/fs.py"],
        f"lib/ comment variant hashes differently: {d.name[:12]}.. vs {base.name[:12]}..",
    )
    # (b) manual/ prose only.
    src = copy_world(SCRATCH / "variant-prose")
    with open(src / "manual/examples.md", "a") as f:
        f.write("\nOne more plain sentence for the variant.\n")
    code, rep, raw = cwr_build(src, SCRATCH / "variant-prose-out")
    d = built_dir(rep)
    if code != 0 or not d:
        R.check("4.6", False, f"prose variant did not build: {raw[-400:]}")
        return
    vf = in_hash_files(d)
    R.check(
        "4.6",
        vf["contracts.json"] == base_files["contracts.json"]
        and all(vf.get(k) == v for k, v in sdk.items())
        and vf["manual.md"] != base_files["manual.md"]
        and b"One more plain sentence for the variant." in vf["manual.md"],
        "manual/ prose variant: contracts.json and sdk/ unchanged, manual.md changed only by the new prose",
    )
    R.check(
        "1.4", d.name != base.name, f"manual/ prose variant hashes differently: {d.name[:12]}.."
    )


def check_identity_and_tamper(build: Path) -> None:
    """Clauses 1.4 and 4.5: identity by sha256, read-only, every modification detected by verification."""
    hx = build.name
    R.check(
        "1.4",
        build_hash(build) == hx and manifest_record(build) == hx,
        f"independent recomputation (compiler.md section 7) equals the directory name and the manifest hash sha256:{hx[:16]}..",
    )
    home = SCRATCH / "kernel-home"
    k = kernel_create(build, home)
    R.check(
        "1.4",
        k.returncode == 0,
        f"kernel loads the untouched Build after verifying it (instance create exit {k.returncode})",
    )
    m = manual_cmd(f"sha256:{hx}", build.parent)
    R.check(
        "2.4",
        m.returncode == 0 and m.stdout.encode() == (build / "manual.md").read_bytes(),
        f"`cwr manual sha256:<hex>` prints manual.md byte for byte (exit {m.returncode})",
    )

    def tamper_one_byte(rel):
        def f(d: Path):
            p = d / rel
            data = bytearray(p.read_bytes())
            i = len(data) // 2
            data[i] = (data[i] + 1) % 256 if chr(data[i]).isalnum() else ord("x")
            p.write_bytes(bytes(data))

        return f

    def set_name(d: Path):
        p = d / "manifest.kvx"
        p.write_text(p.read_text().replace('name = "refworld"', 'name = "refworle"'))

    def set_hash(d: Path):
        p = d / "manifest.kvx"
        p.write_text(
            re.sub(
                r'hash = "sha256:([0-9a-f])',
                lambda m: f'hash = "sha256:{"0" if m.group(1) != "0" else "1"}',
                p.read_text(),
            )
        )

    def manifest_edit(fn):
        def f(d: Path):
            p = d / "manifest.kvx"
            p.write_bytes(fn(p.read_bytes()))

        return f

    rec = f'hash = "sha256:{hx}"\n'.encode()
    zero = b'hash = "sha256:' + b"0" * 64 + b'"\n'

    def meta_rec(b: bytes) -> bytes:
        return b.replace(b"schema_version = 1\n", b"schema_version = 1\n" + rec, 1)

    cases = [
        ("manifest.kvx name key", set_name),
        ("manifest.kvx recorded hash", set_hash),
        # The recorded-hash line is the one part of the manifest outside the hash; every way of adding, moving or
        # re-spelling a `hash` key must still be detected.
        (
            "manifest.kvx [build].hash zeroed, true hash added to [meta]",
            manifest_edit(lambda b: meta_rec(b.replace(rec, zero))),
        ),
        ("manifest.kvx hash key added to [meta]", manifest_edit(meta_rec)),
        (
            "manifest.kvx record moved from [build] to [meta]",
            manifest_edit(lambda b: meta_rec(b.replace(rec, b""))),
        ),
        (
            "manifest.kvx record moved into a new trailing section",
            manifest_edit(lambda b: b.replace(rec, b"[extra]\n" + rec)),
        ),
        ("manifest.kvx record duplicated in [build]", manifest_edit(lambda b: b + rec)),
        (
            "manifest.kvx record re-spaced `hash  =  `",
            manifest_edit(lambda b: b.replace(b"hash = ", b"hash  =  ")),
        ),
        (
            "manifest.kvx record indented",
            manifest_edit(lambda b: b.replace(b"hash = ", b" hash = ")),
        ),
        (
            "manifest.kvx record with a trailing comment",
            manifest_edit(lambda b: b.replace(rec, rec[:-1] + b" # ok\n")),
        ),
        (
            "manifest.kvx record with CRLF",
            manifest_edit(lambda b: b.replace(rec, rec[:-1] + b"\r\n")),
        ),
        ("manifest.kvx final newline removed", manifest_edit(lambda b: b[:-1])),
        ("manifest.kvx blank line after the record", manifest_edit(lambda b: b + b"\n")),
        (
            "manifest.kvx record hex upper-cased",
            manifest_edit(lambda b: b.replace(hx.encode(), hx.upper().encode())),
        ),
    ]
    for rel in [
        "contracts.json",
        "contract_diff.json",
        "manual.md",
        "sdk/cwr/_rpc.py",
        "sdk/cwr/_codec.py",
        "sdk/cwr/errors.py",
        "sdk/fs.py",
        "lib/fs.py",
        "adapters/search.kvx",
        "adapters/publish/live.py",
        "fixtures/fs/delete.kvx",
        "migrations/0_2_0_kv_namespaces.py",
        "tests.json",
    ]:
        cases.append((f"one byte of {rel}", tamper_one_byte(rel)))

    def mkdir_with_file(rel):
        def f(d: Path):
            (d / rel).mkdir()
            (d / rel / "a.py").write_text("x = 1\n")

        return f

    def forge_tests(d: Path):
        p = d / "tests.json"
        rep = json.loads(p.read_text())
        rep["cases"] = rep["cases"][:-1]  # hide one case, keep the canonical layout
        p.write_text(json.dumps(rep, indent=2, sort_keys=True) + "\n")

    def symlink_at(rel, target):
        def f(d: Path):
            p = d / rel
            if p.exists():
                p.unlink()
            p.symlink_to(target)

        return f

    cases += [
        ("added file sdk/extra.py", lambda d: (d / "sdk/extra.py").write_text("x = 1\n")),
        ("removed file sdk/kv.py", lambda d: (d / "sdk/kv.py").unlink()),
        ("added file at the Build root extra.txt", lambda d: (d / "extra.txt").write_text("x\n")),
        ("added directory with a file at the Build root newdir/a.py", mkdir_with_file("newdir")),
        ("added empty directory at the Build root", lambda d: (d / "emptydir").mkdir()),
        ("added hidden file at the Build root .note", lambda d: (d / ".note").write_text("x\n")),
        ("added empty directory lib/empty", lambda d: (d / "lib/empty").mkdir()),
        ("added directory with a file fixtures/new/a.py", mkdir_with_file("fixtures/new")),
        ("tests.json replaced by {}", lambda d: (d / "tests.json").write_text("{}\n")),
        ("tests.json edited (a forged report)", forge_tests),
        ("tests.json removed", lambda d: (d / "tests.json").unlink()),
        ("manual.md removed", lambda d: (d / "manual.md").unlink()),
        ("migrations/ removed", lambda d: shutil.rmtree(d / "migrations")),
        (
            "manual.md replaced by a symbolic link to itself elsewhere",
            symlink_at("manual.md", "/dev/null"),
        ),
        ("symbolic link added at the Build root", symlink_at("link", "manual.md")),
        ("sdk/fs.py replaced by a symbolic link", symlink_at("sdk/fs.py", "../lib/fs.py")),
        (
            "tests.json replaced by a directory",
            lambda d: ((d / "tests.json").unlink(), (d / "tests.json").mkdir()),
        ),
    ]
    refused = []
    for i, (label, fn) in enumerate(cases):
        d = SCRATCH / "tamper" / str(i) / hx
        shutil.copytree(build, d)
        unseal(d)
        fn(d)
        independent = build_hash(d) != hx or manifest_record(d) != hx
        mc = manual_cmd(d)
        kc = kernel_create(d, SCRATCH / f"kernel-home-{i}")
        # `cwr manual` verifies the hash before it looks at permissions, so the refusal of a tampered copy must
        # be the hash verification's, not the read-only check's.
        by_hash = WRITABLE_REFUSAL not in mc.stderr
        ok = independent and mc.returncode == 5 and by_hash and kc.returncode == 2
        if ok:
            refused.append(label)
        else:
            R.check(
                "1.4",
                False,
                f"{label}: independent changed={independent}, cwr manual exit {mc.returncode} (by hash {by_hash}), kernel exit {kc.returncode}",
            )
    R.check(
        "1.4",
        len(refused) == len(cases),
        f"{len(refused)}/{len(cases)} tampered copies refused by `cwr manual` (exit 5) and the kernel (instance create exit 2), "
        f"and each differs under the independent hash",
    )
    R.check(
        "4.5",
        len(refused) == len(cases),
        f"every later modification is detected by hash verification, by `cwr manual` and by the kernel: {len(cases)} cases "
        f"covering every in-hash file class, every addition, move or re-spelling of a manifest `hash` key, tests.json (edited, forged, emptied, removed), additions at the Build root and "
        f"below it (files, directories, empty directories, hidden files, symbolic links) and removals",
    )
    # A Build re-opened for writing without any byte changed: the hash still verifies, but the Build is no longer
    # read-only, so `cwr manual` refuses it; sealed again, it is served again.
    d = SCRATCH / "reopened" / hx
    shutil.copytree(build, d)
    same_before = build_hash(d) == hx and not any(
        p.lstat().st_mode & 0o222 for p in [d, *d.rglob("*")]
    )
    unseal(d)
    mc = manual_cmd(d)
    kc = kernel_create(d, SCRATCH / "kernel-home-reopened")
    reseal(d)
    mc2 = manual_cmd(d)
    R.check(
        "4.5",
        same_before
        and build_hash(d) == hx
        and mc.returncode == 5
        and WRITABLE_REFUSAL in mc.stderr
        and mc2.returncode == 0
        and mc2.stdout.encode() == (build / "manual.md").read_bytes(),
        f"a copied Build keeps every file read-only; made writable again with no byte changed it still hashes to its name "
        f"but `cwr manual` refuses it (exit {mc.returncode}, 'writable after packaging') and serves it again once sealed "
        f"(exit {mc2.returncode}); informational, kernel instance create (task 2.x) on the writable copy exits {kc.returncode}",
    )
    # tests.json is in the hash and deterministic: it carries no host durations.
    tj = json.loads((build / "tests.json").read_text())
    R.check(
        "4.5",
        "tests.json" in in_hash_files(build)
        and tj.get("passed") is True
        and tj.get("format") == "cwr.tests"
        and "duration" not in (build / "tests.json").read_text()
        and len(tj.get("runs", [])) == 2,
        "tests.json is inside the hash, reports passed with two outcome streams and records no host durations",
    )

    # Layout and read-only.
    entries = sorted(p.name for p in build.iterdir())
    R.check(
        "4.5",
        entries == LAYOUT,
        f"Build directory sha256-named with exactly {', '.join(LAYOUT)}"
        + ("" if entries == LAYOUT else f"; GOT {entries}"),
    )
    writable = [
        str(p.relative_to(build)) for p in [build, *build.rglob("*")] if p.lstat().st_mode & 0o222
    ]
    R.check(
        "4.5",
        not writable,
        f"no write permission bit on any of {sum(1 for _ in build.rglob('*')) + 1} files and directories"
        + (f"; WRITABLE {writable[:5]}" if writable else ""),
    )
    probe = (
        "import os,sys\n"
        "if os.geteuid()==0:\n os.setgid(65534); os.setuid(65534)\n"
        "b=sys.argv[1]; bad=[]\n"
        "for t in ['manual.md','contracts.json','sdk/fs.py','lib/fs.py']:\n"
        " try:\n  open(os.path.join(b,t),'a').close(); bad.append(t)\n except PermissionError: pass\n"
        "for t in ['', 'sdk']:\n"
        " try:\n  open(os.path.join(b,t,'new.txt'),'w').close(); bad.append(t+'/new.txt')\n except PermissionError: pass\n"
        "print(bad)\n"
    )
    p = run([sys.executable, "-B", "-c", probe, build])
    R.check(
        "4.5",
        p.returncode == 0 and p.stdout.strip() == "[]",
        f"an unprivileged process cannot append to Build files or create files in it ({p.stdout.strip() or p.stderr.strip()[-200:]})",
    )
    # An existing directory under the hash name that does not verify is left untouched (exit 7).
    src = copy_world(SCRATCH / "collide-src")
    out = SCRATCH / "collide-out"
    out.mkdir()
    shutil.copytree(build, out / hx)
    unseal(out / hx)
    (out / hx / "manual.md").write_text("tampered\n")
    mf = out / hx / "manifest.kvx"
    mf.write_text(mf.read_text().replace('name = "refworld"', 'name = "otherworld"'))
    before = in_hash_files(out / hx)
    code, _rep, _raw = cwr_build(src, out)
    R.check(
        "4.5",
        code == 7 and in_hash_files(out / hx) == before and not list(out.glob(".tmp-*")),
        f"target directory exists and fails verification: exit {code}, left untouched, no temporary directory left",
    )
    # Two racing builds of one source on one host.
    src = copy_world(SCRATCH / "race-src")
    out = SCRATCH / "race-out"
    procs = [
        subprocess.Popen(
            [str(CWR), "build", str(src), "--json", "--out", str(out)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    outs = [p.communicate(timeout=TIMEOUT) for p in procs]
    hashes = {
        json.loads(o[0]).get("build", {}).get("hash") for o in outs if o[0].strip().startswith("{")
    }
    R.check(
        "4.5",
        all(p.returncode == 0 for p in procs)
        and hashes == {f"sha256:{hx}"}
        and sorted(x.name for x in out.iterdir()) == [hx],
        f"two racing builds: exits {[p.returncode for p in procs]}, one hash, one directory, no partial or temporary directory"
        + "".join(
            f"; build {i} failed: {(o[0].strip()[-300:] or o[1].strip()[-300:])}"
            for i, (p, o) in enumerate(zip(procs, outs, strict=True))
            if p.returncode != 0
        ),
    )


def entry_blocks(manual: str) -> dict[str, list[str]]:
    blocks: dict[str, list[str]] = {}
    for m in re.finditer(r"^#### (\S+)\n\n```text\n(.*?)```\n", manual, re.M | re.S):
        blocks.setdefault(m.group(1), []).append(m.group(2))
    return blocks


def check_manual(build: Path, src: Path, contracts: dict) -> None:
    """Clauses 2.4, 2.5 and 9.2 against the World Source read independently."""
    manual = (build / "manual.md").read_text()
    surface, _modules = source_surface(src)
    blocks = entry_blocks(manual)
    problems = []
    for name, c in surface.items():
        heads = len(re.findall(rf"^#### {re.escape(name)}$", manual, re.M))
        sig = (
            f"{name}("
            + ", ".join(f"{k}: {t}" for k, t in c["params"].items())
            + f") -> {c['returns']}"
        )
        sig_lines = [ln for ln in manual.splitlines() if ln.startswith(sig + " ")]
        if heads != 1 or len(blocks.get(name, [])) != 1 or len(sig_lines) != 1:
            problems.append(
                f"{name}: {heads} headings, {len(blocks.get(name, []))} entries, {len(sig_lines)} signature lines"
            )
            continue
        lines = blocks[name][0].splitlines()
        tag = (
            f"[external, idempotency: {c['idempotency']}, timeout {c['timeout_ms']} ms]"
            if c["kind"] == "adapter"
            else f"[{c['side_effect']}]"
        )
        if not (lines[0].startswith(sig) and lines[0].endswith(tag)):
            problems.append(f"{name}: signature/tag line {lines[0]!r}")
        if lines[1] != "  " + c["doc"]:
            problems.append(f"{name}: doc line")
        pre = [
            (int(m.group(1)), m.group(2), m.group(3))
            for ln in lines
            if (m := re.match(r"^\s+(?:Requires\s+)?(\d+)\. (.*?)\s+else (\S+)$", ln))
        ]
        want = [(i + 1, e, code) for i, (e, code) in enumerate(c["preconditions"])]
        if pre != want:
            problems.append(f"{name}: preconditions {pre} != {want}")
        if c["kind"] == "primitive":
            ens = []
            for ln in lines:
                if ln.startswith("  Ensures    "):
                    ens.append(ln[len("  Ensures    ") :])
                elif ens and ln.startswith("             ") and not re.match(r"^\s+\d+\. ", ln):
                    ens.append(ln.strip())
            want_e = c.get("effects") or ["world state unchanged"]
            if ens != want_e:
                problems.append(f"{name}: effects {ens} != {want_e}")
            if any("Content is not guaranteed" in ln for ln in lines):
                problems.append(f"{name}: internal primitive labelled content-not-guaranteed")
        else:
            mf = [ln for ln in lines if ln.startswith("  May fail   ")]
            if mf != ["  May fail   " + ", ".join(c["failures"])]:
                problems.append(f"{name}: declared failures {mf}")
            if not any(ln.startswith("  Content is not guaranteed") for ln in lines):
                problems.append(f"{name}: adapter missing the content-not-guaranteed label")
    n_ad = sum(1 for c in surface.values() if c["kind"] == "adapter")
    R.check(
        "2.4",
        not problems,
        f"{len(surface)} contracts read from the World Source; each has exactly one manual entry whose signature, params, "
        f"return shape, side-effect class, ordered preconditions with codes, effects and adapter failures equal the source"
        + (f"; PROBLEMS {problems[:4]}" if problems else ""),
    )
    extra = sorted(set(blocks) - set(surface))
    R.check(
        "2.4",
        not extra,
        "no manual entry without a contract" + (f"; EXTRA {extra}" if extra else ""),
    )
    # contracts.json, which the kernel enforces, equals the source too: the manual is rendered from it.
    cj = {p["name"]: p for p in contracts["primitives"] + contracts["adapters"]}
    mism = [
        n
        for n, c in surface.items()
        if n not in cj
        or cj[n]["preconditions"] != c["preconditions"]
        or [list(x) for x in c["params"].items()] != cj[n]["params"]
        or cj[n]["returns"] != c["returns"]
        or cj[n]["side_effect"] != c["side_effect"]
        or (cj[n].get("effects") or []) != (c.get("effects") or [])
    ]
    R.check(
        "2.4",
        not mism and set(cj) == set(surface),
        f"contracts.json (the enforced table the kernel loads) carries the same {len(cj)} contracts, preconditions in declared order"
        + (f"; MISMATCH {mism}" if mism else ""),
    )
    # 2.5: labels.
    ad_section = manual.split("## 7. Adapters", 1)[1].split("## 8.", 1)[0]
    R.check(
        "2.5",
        n_ad > 0
        and all(f"#### {n}" in ad_section for n, c in surface.items() if c["kind"] == "adapter")
        and "content not guaranteed" in ad_section
        and "content_guaranteed: false" in manual,
        f"all {n_ad} adapters listed in section 7 under the content-not-guaranteed label; section 3 names the envelope label",
    )
    ov = manual.split("## 5. Modules overview", 1)[1].split("## 6.", 1)[0]
    ad_mods = sorted({c["module"] for c in surface.values() if c["kind"] == "adapter"})
    R.check(
        "2.5",
        all(re.search(rf"### Module {m}\n\n[^\n]*content not guaranteed", ov) for m in ad_mods),
        f"modules overview labels the adapter modules {ad_mods} content not guaranteed",
    )
    # 9.2: ten sections in order, pillars, envelope verbatim.
    heads = re.findall(r"^## (.+)$", manual, re.M)
    R.check(
        "9.2",
        heads == SECTIONS,
        f"the ten sections in order: {'; '.join(h.split('. ', 1)[0] for h in heads)}"
        + ("" if heads == SECTIONS else f"; GOT {heads}"),
    )
    s1 = manual.split("## 2.", 1)[0]
    R.check(
        "9.2",
        all(f"**{p}.**" in s1 for p in ["Stability", "Direct action", "Controlled change"])
        and f"- World: {contracts['world']['name']}" in s1
        and f"- Version: {contracts['world']['version']}" in s1,
        "section 1 states world name, version and the three pillars (stability, direct action, controlled change)",
    )
    s3 = manual.split("## 3. The failure envelope", 1)[1].split("## 4.", 1)[0]
    rows = re.findall(r"^\| `([A-Z_]+)` \| (.*?) \| (.*?) \|$", s3, re.M)
    env = contracts["envelope"]
    R.check(
        "9.2",
        [r[0] for r in rows] == CODES == [e["code"] for e in env]
        and all(
            r[1] == e["meaning"] and r[2] == e["guidance"] for r, e in zip(rows, env, strict=False)
        )
        and "retry_after" in s3
        and "it does not mean content" in s3,
        "section 3 renders the eleven codes verbatim from contracts.json (code, meaning, guidance, in envelope order), "
        "the retry_after rule and the determinism sentence",
    )
    s4 = manual.split("## 4. Limits and clock", 1)[1].split("## 5.", 1)[0]
    lim = all(f"| `{k}` | {v} |" in s4 for k, v in contracts["limits"].items())
    R.check(
        "9.2",
        lim
        and f"tick_per_primitive = {contracts['clock']['tick_per_primitive']}" in s4
        and f"tick_per_turn = {contracts['clock']['tick_per_turn']}" in s4,
        "section 4 lists every limit and clock value of the Build",
    )
    idx = manual.split("## 10. Index", 1)[1]
    names = re.findall(r"^- \[([^\]]+)\]\(#([^)]+)\)", idx, re.M)
    R.check(
        "9.2",
        [n for n, _ in names] == sorted(surface, key=str.encode)
        and all(a == re.sub(r"[^a-z0-9_-]", "", n.lower()) for n, a in names),
        f"section 10 indexes all {len(names)} primitives and adapters alphabetically with their anchors",
    )
    s8 = manual.split("## 8. Changes and versions", 1)[1].split("## 9.", 1)[0]
    vers = re.findall(r"^### (\d+\.\d+\.\d+) ", s8, re.M)
    cl = kvx(src / "CHANGELOG.kvx")
    src_vers = sorted(
        (s.split(".", 1)[1] for s in cl if s.startswith("release.")),
        key=lambda v: tuple(map(int, v.split("."))),
        reverse=True,
    )
    R.check(
        "9.2",
        vers == src_vers and "only by recompilation" in s8,
        f"section 8 renders the changelog newest first {vers}",
    )
    R.check(
        "9.2",
        manual.endswith("\n") and not manual.endswith("\n\n") and "\r" not in manual,
        "manual.md uses \\n line ends and ends with exactly one newline",
    )


def check_sdk(build: Path, src: Path, contracts: dict) -> None:
    """Clauses 2.4 (SDK signatures equal contracts; one RPC per call) and 2.5 (per-call envelope label)."""
    sdk = build / "sdk"
    sock = SCRATCH / "rpc.sock"
    surface, _ = source_surface(src)
    src_labels = {n: bool(c.get("content_guaranteed", True)) for n, c in surface.items()}
    peer = Peer(sock, contracts, src_labels)
    prims = [(p["name"], p["params"]) for p in contracts["primitives"] + contracts["adapters"]]
    plan = {"primitives": prims, "codes": CODES}
    env = {"CWR_RPC_SOCKET": str(sock), "PATH": os.environ.get("PATH", "")}
    p = run([sys.executable, "-B", "-I", "-c", CHILD, sdk, json.dumps(plan)], env=env)
    # Second run: every primitive answered with the label true, false and missing in turn.
    peer.label_queue = [v for _ in prims for v in (True, False, None)]
    n_before = len(peer.requests)
    lp = run(
        [
            sys.executable,
            "-B",
            "-I",
            "-c",
            CHILD,
            sdk,
            json.dumps({"primitives": prims, "mode": "labels"}),
        ],
        env=env,
    )
    peer.close()
    if p.returncode != 0:
        R.check("2.4", False, f"SDK client process failed: {p.stderr[-800:]}")
        return
    rep = json.loads(p.stdout)
    reqs = peer.requests[:n_before]  # the first run's frames only
    one_each = all(c[1] == 1 for c in rep["calls"]) and len(rep["calls"]) == len(prims)
    first = reqs[: len(prims)]
    order = [r["primitive"] for r in first] == [n for n, _ in prims]

    def expect(t, i):
        return {"str": f"x{i}", "Path": f"/a{i}", "int": i + 1}.get(t)

    names_ok = all(
        sorted(r["params"]) == sorted(k for k, _ in params)
        and all(
            expect(ty, i) is None or r["params"][k] == expect(ty, i)
            for i, (k, ty) in enumerate(params)
        )
        for r, (_, params) in zip(first, prims, strict=False)
    )
    R.check(
        "2.4",
        one_each and order and names_ok and rep["total_rpcs"] == len(reqs),
        f"every one of the {len(prims)} generated functions performed exactly one RPC (server saw {len(reqs)} frames for "
        f"{rep['total_rpcs']} calls), carrying the contract name, and each positional argument arrived under its contract parameter name (declared order)",
    )
    sig_bad = []
    for n, params in prims:
        fname, pnames = rep["signatures"][n]
        short = n.split(".", 1)[1]
        want = (
            short + "_"
            if short in ("del", "from", "class", "import", "global", "lambda")
            else short
        )
        if fname != want or [x.rstrip("_") for x in pnames] != [k for k, _ in params]:
            sig_bad.append(n)
    R.check(
        "2.4",
        not sig_bad,
        "generated signatures equal the contracts' parameter lists (keywords bound with a trailing "
        "underscore: kv.del as kv.del_)" + (f"; BAD {sig_bad}" if sig_bad else ""),
    )
    wr = next((r for r in first if r["primitive"] == "fs.write"), None)
    rd = next((c for c in rep["calls"] if c[0] == "fs.read"), None)
    R.check(
        "2.4",
        wr is not None
        and wr["params"].get("content") == {"$bytes": "AP8="}
        and rd is not None
        and rd[2][2] == "00ff",
        'codec: bytes travel as {"$bytes": base64} and decode back to bytes',
    )
    errs = rep["errors"]
    ok_errs = all(
        errs.get(c) and errs[c][1] == c and errs[c][2] == "fs.read" and errs[c][7] for c in CODES
    )
    special = (
        errs["RATE_LIMITED"][4] == 7
        and errs["NOT_FOUND"][3] == 1
        and errs["LIMIT"][5] == "primitive_calls"
        and errs["LOCKED"][4] is None
    )
    R.check(
        "2.4",
        ok_errs and special and len({e[0] for e in errs.values()}) == 11,
        "each of the 11 codes raises its own typed exception (cwr.NotFound ... cwr.InvariantViolation) carrying code, "
        "primitive, precondition_index, retry_after (RATE_LIMITED only, 7 ticks) and limit",
    )
    world_fns = {n for n, _ in prims if n.startswith("world.")}
    R.check(
        "2.4",
        {"world.limits", "world.random", "world.history", "world.propose"} <= world_fns,
        "the SDK provides world.limits, world.random, world.history and world.propose",
    )
    labels = {c[0]: c[3] for c in rep["calls"]}
    n_false = sum(1 for v in src_labels.values() if v is False)
    R.check(
        "2.5",
        n_false > 0
        and set(labels) == set(src_labels)
        and all(labels[n] is src_labels[n] for n in labels)
        and rep["label"]["content_guaranteed"] is False,
        f"every per-call result envelope a script sees carries content_guaranteed equal to the World Source declaration "
        f"(false for the {n_false} adapters declaring content_guaranteed = false, true for the other {len(src_labels) - n_false})",
    )
    variants = {True: 0, False: 1, None: 2}
    lab_bad = []
    if lp.returncode != 0:
        lab_bad.append(f"label run failed: {lp.stderr[-300:]}")
    else:
        lrep = json.loads(lp.stdout)
        for name, row in lrep["rows"]:
            want = variants[src_labels[name]]
            for i, (verdict, info) in enumerate(row):
                if (verdict == "accepted") != (i == want) or (
                    verdict == "accepted" and info is not src_labels[name]
                ):
                    lab_bad.append(f"{name} variant {['true', 'false', 'missing'][i]}: {verdict}")
        if len(peer.requests) - n_before != 3 * len(prims) or lrep["total_rpcs"] != 3 * len(prims):
            lab_bad.append(f"{len(peer.requests) - n_before} frames for {3 * len(prims)} calls")
    R.check(
        "2.5",
        not lab_bad,
        f"answered with label true, false and missing for each of the {len(prims)} primitives, the SDK accepts exactly the "
        f"World Source label and refuses the other two ({3 * len(prims)} calls)"
        + (f"; BAD {lab_bad[:4]}" if lab_bad else ""),
    )
    R.check(
        "2.5",
        str(rep["badlabel"]).startswith("refused"),
        f"an adapter response that drops the label is refused by the SDK against the Build's own contract ({str(rep['badlabel'])[:90]})",
    )
    adapter_doc = (sdk / "search.py").read_text()
    R.check(
        "2.5",
        "Content is not guaranteed" in adapter_doc
        and "Content is not guaranteed" not in (sdk / "fs.py").read_text(),
        "generated adapter functions document the content-not-guaranteed label; internal ones do not",
    )
    # Turn-envelope codec helper: label on every trace item.
    probe = (
        "import sys; sys.path.insert(0, sys.argv[1]); from cwr import _codec as c\n"
        "t=[c.trace_item('fs.write','ok',None,1,True), c.trace_item('publish.post','error','RATE_LIMITED',2,False)]\n"
        "e=c.turn_envelope('partial', None, c.failure('RATE_LIMITED','publish.post',retry_after=30,detail='x'), t,"
        " {'version':'0.2.0','build':'sha256:0','time':12,'turn':1}, {'cpu_ms':1,'wall_ms':1,'primitive_calls':2,'adapter_calls':1,'remaining':{}})\n"
        "bad=[]\n"
        "for f in [lambda: c.failure('NOPE','x'), lambda: c.failure('RATE_LIMITED','x'), lambda: c.failure('LOCKED','x',retry_after=3)]:\n"
        "  try: f(); bad.append(1)\n  except c.CodecError: pass\n"
        "print(c.dumps(e)); print(len(bad)); print(c.dumps({'b':b'\\x00','a':[1,'\\u00e9'],'$k':None}))\n"
    )
    q = run([sys.executable, "-B", "-I", "-c", probe, sdk])
    lines = q.stdout.splitlines()
    R.check(
        "2.5",
        q.returncode == 0
        and len(lines) == 3
        and '"content_guaranteed":false' in lines[0]
        and lines[1] == "0",
        "codec turn envelope: every trace item carries content_guaranteed; codes outside the envelope and misplaced retry_after are refused",
    )
    R.check(
        "4.6",
        q.returncode == 0
        and len(lines) == 3
        and lines[2] == '{"$$k":null,"a":[1,"\u00e9"],"b":{"$bytes":"AA=="}}',
        "codec serialisation is canonical (sorted keys, no whitespace, UTF-8, $-escaped keys)",
    )


def check_sdk_files(build: Path, contracts: dict) -> None:
    """Clauses 2.4 and 4.6: every generated SDK file has the source header once, at the top, and no duplicated boilerplate."""
    tmpl = (REPO / "sdk/python/cwr/templates/module.py.tmpl").read_text(encoding="utf-8")
    header = "".join(tmpl.splitlines(keepends=True)[:2])
    header_ok = header.startswith("# Copyright ") and "\n# SPDX-License-Identifier: " in header
    sdk = build / "sdk"
    files = sorted(p for p in sdk.rglob("*.py"))
    bad = []
    for f in files:
        rel = str(f.relative_to(sdk))
        text = f.read_text(encoding="utf-8")
        if not text.startswith(header):
            bad.append(f"{rel}: does not start with the template header")
        for marker in ("SPDX-License-Identifier", "# Copyright "):
            if text.count(marker) != 1:
                bad.append(f"{rel}: {text.count(marker)}x {marker!r}")
    by_module: dict[str, list[str]] = {}
    for p in contracts["primitives"] + contracts["adapters"]:
        by_module.setdefault(p["name"].split(".", 1)[0], []).append(p["name"].split(".", 1)[1])
    for mod, shorts in sorted(by_module.items()):
        f = sdk / f"{mod}.py"
        if not f.is_file():
            bad.append(f"{mod}.py missing")
            continue
        text = f.read_text(encoding="utf-8")
        once = [
            f'"""{mod}: generated SDK module',
            "from __future__ import annotations\n",
            "from cwr._rpc import call as _call\n",
            "\n__all__ = [",
        ]
        for m in once:
            if text.count(m) != 1:
                bad.append(f"{mod}.py: {text.count(m)}x {m.strip()!r}")
        defs = re.findall(r"^def (\w+)\(", text, flags=re.M)
        if len(defs) != len(set(defs)) or len(text.split('"""')) % 2 != 1:
            bad.append(f"{mod}.py: duplicated function or unbalanced docstrings")
        # world.seed is omitted when the seed is not visible (kernel.md section 6).
        want = [
            x
            for x in shorts
            if not (
                mod == "world"
                and x == "seed"
                and not contracts.get("rng", {}).get("seed_visible", True)
            )
        ]
        if [d.rstrip("_") for d in defs] != want:
            bad.append(f"{mod}.py: functions {defs} != contracts {want}")
    errs = (sdk / "cwr/errors.py").read_text(encoding="utf-8")
    classes = re.findall(r"^class (\w+)\(", errs, flags=re.M)
    if len(classes) != len(set(classes)) or len(classes) != len(contracts["envelope"]) + 1:
        bad.append(f"errors.py: classes {classes}")
    R.check(
        "2.4",
        header_ok and files and not bad,
        f"each of the {len(files)} generated SDK files starts with the template source header and carries it exactly once; "
        f"each module has one docstring, one import block, one __all__ and one function per contract primitive in contract "
        f"order; errors.py has one class per envelope code plus WorldError"
        + (f"; BAD {bad[:6]}" if bad else ""),
    )
    R.check(
        "4.6",
        header_ok and files and not bad,
        "the generated SDK is the template output with fragment headers stripped (no duplicated header or boilerplate)",
    )


# Prelude of every label-probe turn: `probe` records [name, status, code] of one SDK call as the script sees it.
KERNEL_PRELUDE = r"""
import cwr, {imports}
out = []
def probe(name, f):
    try:
        v = f()
        out.append([name, "ok", None, v if name == "publish.post" else None])
    except cwr.errors.WorldError as e:
        out.append([name, "error", e.code, None])
"""


def start_stub(name: str) -> tuple[subprocess.Popen, int]:
    p = subprocess.Popen(
        [
            sys.executable,
            "-B",
            str(REPO / f"benchmark/stubs/{name}_server.py"),
            "--port",
            "0",
            "--seed",
            "7",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    ready = json.loads(p.stdout.readline())
    return p, int(ready["port"])


def check_kernel_labels(build: Path, contracts: dict) -> None:
    """Clause 2.5 end to end: the kernel labels every per-call result of a real sandboxed turn with the Build's
    content_guaranteed declaration, for ok and error outcomes alike; adapters reach local stub servers only."""
    labels = {p["name"]: True for p in contracts["primitives"]}
    labels.update(
        {a["name"]: bool(a.get("content_guaranteed", True)) for a in contracts["adapters"]}
    )
    home = SCRATCH / "kernel-labels"
    c = run([CWR, "instance", "create", "--home", home, "--build", build])
    if c.returncode != 0:
        R.check("2.5", False, f"instance create for the label probe failed: {c.stderr[-300:]}")
        return
    iid = c.stdout.strip().splitlines()[-1]

    def lit(t: str, i: int):
        return {
            "str": f"x{i}",
            "Path": f"/a{i}.txt",
            "int": i + 1,
            "bool": True,
            "bytes": b"\x00\xff",
        }.get(t, [] if t.startswith("list[") else {} if t.startswith("dict[") else None)

    lines = []
    for p in contracts["primitives"]:
        mod, short = p["name"].split(".", 1)
        py = (
            short + "_"
            if short in ("del", "from", "class", "import", "global", "lambda")
            else short
        )
        args = ", ".join(f"{k}={lit(t, i)!r}" for i, (k, t) in enumerate(p["params"]))
        lines.append(f"if hasattr({mod}, {py!r}): probe({p['name']!r}, lambda: {mod}.{py}({args}))")
    mods = sorted(
        {p["name"].split(".", 1)[0] for p in contracts["primitives"] + contracts["adapters"]}
    )
    prelude = KERNEL_PRELUDE.replace("{imports}", ", ".join(mods))
    # One adapter call per turn: a call to a stub can take seconds on a loaded host, and a turn has a wall budget.
    # publish.status and publish.delete use the post id the publish.post turn returned.
    turns = [
        *("\n".join(lines[i : i + 4]) for i in range(0, len(lines), 4)),
        'probe("publish.post", lambda: publish.post(key="k-1_3", title="t", body="b"))',
        'probe("publish.status", lambda: publish.status(post_id={pid!r}))',
        'probe("publish.delete", lambda: publish.delete(post_id={pid!r}))',
        'probe("search.query", lambda: search.query(q="tennis", limit=3))',
        'probe("search.query", lambda: search.query(q="", limit=3))',
        'probe("publish.status", lambda: publish.status(post_id="p-unknown"))',
    ]
    stubs = [start_stub("search"), start_stub("publish")]
    returned: list = []
    trace: list = []
    bad = []
    pid = "p-missing"
    try:
        # Turns meant to show an adapter's ok outcome (post, status, delete, search) are run again, up to three
        # times, when the adapter answers with a transient failure (a contract timeout on a loaded host); every
        # attempt's calls still count towards the label check.
        want_ok = {
            n
            for n, b in enumerate(turns)
            if b.startswith('probe("') and 'q=""' not in b and "p-unknown" not in b
        }
        queue = [(n, body, 0) for n, body in enumerate(turns)]
        while queue:
            n, body, tries = queue.pop(0)
            f = SCRATCH / f"label-turn-{n}-{tries}.py"
            f.write_text(prelude + body.replace("{pid!r}", repr(pid)) + "\nreturn out\n")
            # A turn the kernel ends for its wall-time budget (a host-load artefact on a shared machine) is run
            # again, up to three times; its partial trace is not counted.
            for _attempt in range(3):
                t = run(
                    [
                        CWR,
                        "instance",
                        "turn",
                        iid,
                        "--home",
                        home,
                        "--script",
                        f,
                        "--stub",
                        f"search=http://127.0.0.1:{stubs[0][1]}",
                        "--stub",
                        f"publish=http://127.0.0.1:{stubs[1][1]}",
                    ]
                )
                try:
                    env = json.loads(t.stdout.strip().splitlines()[-1])
                except (ValueError, IndexError):
                    env = None
                    break
                if (env.get("error") or {}).get("limit") != "wall_ms":
                    break
            if env is None:
                bad.append(f"turn {n} printed no envelope (exit {t.returncode}): {t.stderr[-200:]}")
                continue
            got = env.get("returned") or []
            if (
                t.returncode != 0
                or env.get("status") != "ok"
                or len(got) != len(env.get("trace") or [])
                or not got
            ):
                bad.append(
                    f"turn {n}: exit {t.returncode} status {env.get('status')} error {env.get('error')}"
                )
                continue
            for row in got:
                # PostReceipt names the post id `id` (examples/refworld/manual/types.md).
                if (
                    row[0] == "publish.post"
                    and isinstance(row[3], dict)
                    and isinstance(row[3].get("id"), str)
                ):
                    pid = row[3]["id"]
            returned += [r[:3] for r in got]
            trace += env.get("trace") or []
            transient = {"TIMEOUT", "UNAVAILABLE", "RATE_LIMITED"}
            if (
                n in want_ok
                and tries < 2
                and got
                and got[-1][1] == "error"
                and got[-1][2] in transient
            ):
                queue.insert(0, (n, body, tries + 1))
    finally:
        for proc, _ in stubs:
            proc.kill()
            proc.wait()
    seen = set()
    for (name, st, code), item in zip(returned, trace, strict=False):
        cg = item.get("content_guaranteed")
        if item.get("primitive") != name or item.get("status") != st or item.get("code") != code:
            bad.append(f"trace {item} does not match the call {name} {st} {code}")
        if not isinstance(cg, bool) or cg is not labels.get(name):
            bad.append(f"{name} {st}: content_guaranteed {cg!r}, Build says {labels.get(name)}")
        seen.add((cg, st))
    adapters_ok = {n for (n, st, _c) in returned if st == "ok" and labels.get(n) is False}
    want_adapters = {n for n, v in labels.items() if v is False}
    if adapters_ok != want_adapters:
        outcomes = [r for r in returned if labels.get(r[0]) is False]
        bad.append(
            f"adapters with an ok outcome {sorted(adapters_ok)} != {sorted(want_adapters)}: {outcomes}"
        )
    if seen != {(True, "ok"), (True, "error"), (False, "ok"), (False, "error")}:
        bad.append(f"label/status combinations seen {sorted(seen)}")
    R.check(
        "2.5",
        trace and not bad,
        f"real sandboxed kernel turns ({len(turns)}) of {len(trace)} SDK calls (every primitive, every adapter ok via the local stub "
        f"servers, adapter failures): every per-call result in the turn envelopes carries content_guaranteed equal to the "
        f"Build's label (false for the {len(want_adapters)} adapters, ok and error alike; true otherwise)"
        + (f"; BAD {bad[:5]}" if bad else ""),
    )


def main() -> int:
    global CWR
    ok, ev = cargo()
    CWR = target_dir() / "debug" / "cwr"
    R.check("4.6", ok, ev)
    a, _b, contracts = check_determinism()
    check_functions_of_contracts(a)
    check_identity_and_tamper(a)
    check_manual(a, REFWORLD, contracts)
    check_sdk(a, REFWORLD, contracts)
    check_sdk_files(a, contracts)
    check_kernel_labels(a, contracts)
    allpass = True
    for c in CLAUSES:
        bad = R.bad[c]
        status = "PASS" if R.ok[c] and not bad else "FAIL"
        allpass &= status == "PASS"
        print(f"CLAUSE {c} {status} " + " | ".join(bad + R.ok[c]))
    for p in [SCRATCH, *SCRATCH.rglob("*")]:
        try:
            if p.is_dir() and not p.is_symlink():
                p.chmod(0o755)
        except OSError:
            pass
    shutil.rmtree(SCRATCH, ignore_errors=True)
    return 0 if allpass else 1


if __name__ == "__main__":
    sys.exit(main())

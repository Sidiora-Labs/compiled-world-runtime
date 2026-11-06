#!/usr/bin/env python3
# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Task 2.1 driver: Instance store, ledger, world clock, RNG and snapshots.

Case CWR-006. Exercises clauses 1.5, 6.6, 8.1, 8.2 and 9.4 against the real implementation:
it builds the `cwr` binary, runs the `cwr-kernel` test suite with cargo, materialises Builds
from the hand-written fixture crates/cwr-kernel/tests/fixtures/minbuild (hashing each one with
an independent stdlib implementation of the compiler.md section 7 algorithm), then drives
`cwr instance ...` as a subprocess and judges only what the kernel leaves behind: stdout of
the commands, the ledger file, the instance metadata, the object store and snapshot files.

Independent oracles here: the Build hash, the ledger hash chain and canonical form, the PCG32
stream (O'Neill's pcg32_srandom_r / pcg32_random_r) and the expected world_time of every
record are all recomputed in this file; none is read back from the kernel.

Durability before return is observed with strace: every primitive's ledger write must be
followed by fsync/fdatasync of the ledger before the result line reaches stdout.

Prints one line per clause, `CLAUSE <req.ac> PASS|FAIL <evidence>`, and exits 0 only when
every clause passes. Honours CARGO_TARGET_DIR; defaults to the workspace target dir.
Python 3.12, standard library only.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import protocol

REPO = Path(__file__).resolve().parents[3]
FIXTURE = REPO / "crates/cwr-kernel/tests/fixtures/minbuild"
CLAUSES = ["1.5", "6.6", "8.1", "8.2", "9.4"]
ENVELOPE = {
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
}
KINDS = {"header", "primitive", "adapter", "turn", "drift", "proposal", "upgrade", "recovery"}
SCHEMA = [
    "seq",
    "instance",
    "build",
    "turn",
    "world_time",
    "kind",
    "primitive",
    "params_hash",
    "preconditions",
    "outcome",
    "duration_us",
    "host_ts",
    "experimental",
]
PCG_STREAM = 0xDA3E39CB94B95BDB
TIMEOUT = 900


class Results:
    def __init__(self) -> None:
        self.ok: dict[str, list[str]] = {c: [] for c in CLAUSES}
        self.bad: dict[str, list[str]] = {c: [] for c in CLAUSES}

    def check(self, clause: str, condition: bool, evidence: str) -> bool:
        (self.ok if condition else self.bad)[clause].append(evidence)
        return bool(condition)


R = Results()
OBS = protocol.Observer("2.1", protocol.ok_bad(R))


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT, **kw)


def target_dir() -> Path:
    env = os.environ.get("CARGO_TARGET_DIR")
    return Path(env) if env else REPO / "target"


def build() -> tuple[Path, bool, str]:
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


# ------------------------------------------------------------------------------------------
# Independent oracles
# ------------------------------------------------------------------------------------------


def build_hash(d: Path) -> str:
    """compiler.md section 7: sorted in-hash paths; path, NUL, decimal length, NUL, bytes."""
    files = [
        f
        for f in ["manifest.kvx", "contracts.json", "contract_diff.json", "manual.md", "tests.json"]
        if (d / f).is_file()
    ]
    for sub in ["sdk", "lib", "adapters", "fixtures", "migrations"]:
        for p in sorted((d / sub).rglob("*")) if (d / sub).is_dir() else []:
            if p.is_file():
                files.append(p.relative_to(d).as_posix())
    files.sort(key=lambda s: s.encode())
    h = hashlib.sha256()
    for f in files:
        data = (d / f).read_bytes()
        if f == "manifest.kvx":
            data = b"".join(
                ln for ln in data.splitlines(keepends=True) if not re.match(rb"\s*hash\s*=", ln)
            )
        h.update(f.encode() + b"\0" + str(len(data)).encode() + b"\0" + data)
    return h.hexdigest()


def canonical(v) -> str:
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def chain_of(prev: str, rec: dict) -> str:
    r = {k: v for k, v in rec.items() if k not in ("chain", "host_ts", "duration_us", "instance")}
    return "sha256:" + hashlib.sha256(prev.encode() + b"\n" + canonical(r).encode()).hexdigest()


M64 = (1 << 64) - 1


class Pcg32:
    def __init__(self, initstate: int, initseq: int) -> None:
        self.state = 0
        self.inc = ((initseq << 1) | 1) & M64
        self.next()
        self.state = (self.state + initstate) & M64
        self.next()

    def next(self) -> int:
        old = self.state
        self.state = (old * 6364136223846793005 + self.inc) & M64
        xorshifted = (((old >> 18) ^ old) >> 27) & 0xFFFFFFFF
        rot = old >> 59
        return ((xorshifted >> rot) | (xorshifted << ((-rot) & 31))) & 0xFFFFFFFF


def pcg_draws(seed: int, n: int) -> list[int]:
    g = Pcg32(seed, PCG_STREAM)
    return [g.next() for _ in range(n)]


# ------------------------------------------------------------------------------------------
# Fixture Builds
# ------------------------------------------------------------------------------------------


def make_build(
    root: Path, sub: str, *, clock=None, seed_visible=True, experimental=None, drop=()
) -> tuple[Path, str]:
    tmp = root / f"_staging_{len(list(root.glob('_staging_*')))}"
    shutil.copytree(FIXTURE, tmp)
    c = json.loads((tmp / "contracts.json").read_text())
    if clock:
        c["clock"] = {"tick_per_primitive": clock[0], "tick_per_turn": clock[1]}
        (tmp / "manual.md").write_text(
            (tmp / "manual.md")
            .read_text()
            .replace(
                "tick_per_primitive 1, tick_per_turn 10",
                f"tick_per_primitive {clock[0]}, tick_per_turn {clock[1]}",
            )
        )
    c["rng"]["seed_visible"] = seed_visible
    c["primitives"] = [p for p in c["primitives"] if p["name"] not in drop]
    (tmp / "contracts.json").write_text(json.dumps(c, indent=2, sort_keys=True) + "\n")
    m = (tmp / "manifest.kvx").read_text()
    if experimental:
        m = m.replace("experimental = false", "experimental = true").replace(
            "experimental_flag = null", f'experimental_flag = "{experimental}"'
        )
    (tmp / "manifest.kvx").write_text(m)
    h = build_hash(tmp)
    m = re.sub(r"(?m)^hash = .*$", f'hash = "sha256:{h}"', (tmp / "manifest.kvx").read_text())
    (tmp / "manifest.kvx").write_text(m)
    assert build_hash(tmp) == h
    dest = root / sub / h
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp.rename(dest)
    for p in sorted(dest.rglob("*"), reverse=True):
        p.chmod(0o555 if p.is_dir() else 0o444)
    dest.chmod(0o555)
    return dest, h


def tree_digest(d: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(d.rglob("*")):
        st = p.stat()
        h.update(
            p.relative_to(d).as_posix().encode() + b"\0" + str(st.st_mtime_ns).encode() + b"\0"
        )
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


# ------------------------------------------------------------------------------------------
# Kernel access
# ------------------------------------------------------------------------------------------


class Kernel:
    def __init__(self, exe: Path, work: Path, home: Path) -> None:
        self.exe, self.work, self.home = exe, work, home

    def __call__(self, *args: str) -> subprocess.CompletedProcess:
        return run([str(self.exe), "instance", *args, "--home", str(self.home)], cwd=self.work)

    def create(self, *args: str) -> str:
        p = self("create", *args)
        if p.returncode != 0:
            raise RuntimeError(f"create {args} failed rc={p.returncode}: {p.stderr}")
        return p.stdout.strip()

    def probe(self, iid: str, *calls: str) -> tuple[list[dict], dict]:
        p = self("probe", iid, *calls)
        if p.returncode != 0:
            raise RuntimeError(f"probe {calls} failed rc={p.returncode}: {p.stderr}")
        lines = [json.loads(ln) for ln in p.stdout.splitlines()]
        return lines[:-1], lines[-1]

    def dir(self, iid: str) -> Path:
        return self.home / "instances" / iid

    def ledger(self, iid: str) -> list[dict]:
        return [json.loads(ln) for ln in (self.dir(iid) / "ledger.jsonl").read_text().splitlines()]

    def ledger_bytes(self, iid: str) -> bytes:
        return (self.dir(iid) / "ledger.jsonl").read_bytes()

    def meta(self, iid: str) -> dict:
        return json.loads((self.dir(iid) / "instance.json").read_text())

    def snapshot(self, iid: str) -> str:
        p = self("snapshot", iid)
        if p.returncode != 0:
            raise RuntimeError(f"snapshot failed: {p.stderr}")
        return p.stdout.strip()

    def tail(self, iid: str) -> list[str]:
        p = self("tail", iid)
        if p.returncode != 0:
            raise RuntimeError(f"tail failed: {p.stderr}")
        return p.stdout.splitlines()


def strip_host(rec: dict) -> dict:
    return {k: v for k, v in rec.items() if k not in ("host_ts", "duration_us", "instance")}


def draws_of(lines: list[dict]) -> list[int]:
    return [
        ln["returned"] for ln in lines if ln["primitive"] == "world.random" and ln["status"] == "ok"
    ]


def first_bad(xs) -> str:
    return "" if not xs else f" first={xs[0]}"


# ------------------------------------------------------------------------------------------
# Ledger schema and chain oracle
# ------------------------------------------------------------------------------------------

ULID = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
SHA = re.compile(r"^sha256:[0-9a-f]{64}$")
BLAKE = re.compile(r"^blake3:[0-9a-f]{64}$")
RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")


def schema_problems(raw: bytes) -> list[str]:
    problems: list[str] = []
    if raw and not raw.endswith(b"\n"):
        problems.append("file does not end with a newline")
    prev = ""
    for i, line in enumerate(raw.decode().splitlines()):
        r = json.loads(line)
        if line != canonical(r):
            problems.append(f"seq {i}: line is not canonical JSON")
        for f in [*SCHEMA, "chain"]:
            if f not in r:
                problems.append(f"seq {i}: missing {f}")
        if problems:
            return problems
        checks = [
            (r["seq"] == i, "seq not monotonic from 0"),
            (bool(ULID.match(r["instance"])), "instance not a ULID"),
            (bool(SHA.match(r["build"])), "build not sha256:<64hex>"),
            (isinstance(r["turn"], int) and r["turn"] >= 0, "turn not an integer"),
            (
                isinstance(r["world_time"], int) and r["world_time"] >= 0,
                "world_time not an integer",
            ),
            (r["kind"] in KINDS, "kind not in enum"),
            (
                isinstance(r["preconditions"], list)
                and all(set(p) == {"index", "expr_hash", "passed"} for p in r["preconditions"]),
                "preconditions shape",
            ),
            (isinstance(r["duration_us"], int), "duration_us not an integer"),
            (bool(RFC3339.match(r["host_ts"])), "host_ts not RFC 3339"),
            (
                r["experimental"] is None or isinstance(r["experimental"], str),
                "experimental not string or null",
            ),
            (r["chain"] == chain_of(prev, r), "hash chain does not verify"),
        ]
        if r["kind"] in ("primitive", "adapter"):
            o = r["outcome"]
            checks += [
                (isinstance(r["primitive"], str), "primitive name missing"),
                (bool(BLAKE.match(r["params_hash"] or "")), "params_hash not blake3:<hex>"),
                (
                    isinstance(o, dict) and set(o) == {"status", "code", "shape", "side_effect"},
                    "outcome shape",
                ),
                (
                    isinstance(o, dict) and ((o["status"] == "ok") == (o["code"] is None)),
                    "outcome code null iff ok",
                ),
                (
                    isinstance(o, dict) and (o["code"] is None or o["code"] in ENVELOPE),
                    "outcome code outside envelope",
                ),
                (
                    isinstance(o, dict)
                    and o["side_effect"] in ("read_only", "mutating", "external"),
                    "outcome side_effect",
                ),
            ]
            pcs = r["preconditions"]
            checks.append(
                (
                    all(p["passed"] for p in pcs[:-1])
                    and [p["index"] for p in pcs] == list(range(1, len(pcs) + 1)),
                    "preconditions not in order or continue after a failure",
                )
            )
        if r["kind"] == "header":
            checks += [(i == 0, "header not at seq 0")] + [
                (k in r, f"header lacks {k}")
                for k in ("seed", "tail_size", "arm", "model", "created_from")
            ]
        elif i == 0:
            checks.append((False, "record 0 is not a header"))
        if r["kind"] == "turn":
            checks += [
                (k in r, f"turn record lacks {k}")
                for k in ("status", "returned_hash", "resources", "calls")
            ]
            checks.append((r.get("status") in ("ok", "error", "partial"), "turn status"))
        if r["kind"] == "recovery":
            checks += [
                (k in r, f"recovery record lacks {k}")
                for k in ("snapshot", "resumed_from", "interrupted_turn")
            ]
        for ok, what in checks:
            if not ok:
                problems.append(f"seq {i}: {what}")
        prev = r["chain"]
    return problems


def expected_world_times(records: list[dict], tpp: int, tpt: int) -> list[str]:
    """Recompute world_time of every record from kinds and outcomes alone (kernel.md section 6)."""
    problems = []
    wt = None
    for r in records:
        if r["kind"] in ("header", "recovery"):
            wt = r["world_time"]
            continue
        if r["kind"] == "primitive":
            exp = wt + (tpp if r["outcome"]["status"] == "ok" else 0)
        elif r["kind"] == "turn":
            exp = wt + tpt
        else:
            exp = wt
        if r["world_time"] != exp:
            problems.append(f"seq {r['seq']} {r['kind']} world_time {r['world_time']} != {exp}")
        wt = r["world_time"]
    return problems


def host_leaks(value, now_s: float, hostname: str, pids: set[int]) -> list[str]:
    """Values in primitive results that look like host time, host identity or a process id."""
    found = []
    windows = [
        (now_s, 10 * 86400),
        (now_s * 1e3, 10 * 86400 * 1e3),
        (now_s * 1e6, 10 * 86400 * 1e6),
        (now_s * 1e9, 10 * 86400 * 1e9),
    ]

    def walk(v, path):
        if isinstance(v, bool):
            return
        if isinstance(v, (int, float)):
            for centre, width in windows:
                if abs(v - centre) <= width:
                    found.append(f"{path}={v} near host time")
            if v in pids:
                found.append(f"{path}={v} equals a kernel process id")
        elif isinstance(v, str):
            if RFC3339.search(v) or re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", v):
                found.append(f"{path} contains a timestamp")
            if hostname and hostname in v:
                found.append(f"{path} contains the hostname")
        elif isinstance(v, dict):
            for k, x in v.items():
                if k in ("host_ts", "duration_us", "instance", "arm"):
                    found.append(f"{path}.{k} is a hidden field")
                walk(x, f"{path}.{k}")
        elif isinstance(v, list):
            for i, x in enumerate(v):
                walk(x, f"{path}[{i}]")

    walk(value, "$")
    return found


# ------------------------------------------------------------------------------------------
# Scenarios
# ------------------------------------------------------------------------------------------

ALL_FACILITIES = [
    "world.now",
    "world.version",
    "world.seed",
    "world.limits",
    "world.random",
    'world.history:{"n":50}',
]


def scenario_clock_and_host_time(k: Kernel, base: Path, ticks: Path, ticks_hash: str) -> None:
    # Two Instances, same seed, created and driven at different host times by different processes.
    a = k.create("--build", str(base), "--seed", "11")
    la1, ra1 = k.probe(a, *ALL_FACILITIES)
    time.sleep(1.2)
    b = k.create("--build", str(base), "--seed", "11")
    time.sleep(1.2)
    lb1, rb1 = k.probe(b, *ALL_FACILITIES)
    same = (
        [x["returned"] for x in la1] == [x["returned"] for x in lb1]
        and ra1["returned"] == rb1["returned"]
        and ra1["world"]["time"] == rb1["world"]["time"]
    )
    R.check(
        "1.5",
        same,
        f"every world facility returns equal values in two Instances created {2.4:.1f}s apart by different processes",
    )
    # world.random results are excluded from the host-time window scan: they are random by
    # contract and are checked against the independent PCG32 oracle under clause 6.6.
    leaks = host_leaks(
        [x["returned"] for x in la1 + lb1 if x["primitive"] != "world.random"]
        + [{k2: v for k2, v in r.items() if k2 != "returned"} for r in (ra1, rb1)],
        time.time(),
        socket.gethostname(),
        set(),
    )
    R.check(
        "1.5",
        not leaks,
        f"no primitive result carries host time, hostname, timestamps or hidden fields{first_bad(leaks)}",
    )
    hist = la1[-1]["returned"]
    R.check(
        "1.5",
        all(not ({"host_ts", "duration_us", "instance", "arm"} & set(r)) for r in hist)
        and len(hist) > 0,
        f"world.history returned {len(hist)} records without host_ts, duration_us, instance or arm",
    )

    # Clock arithmetic on a Build with tick_per_primitive 3 and tick_per_turn 7.
    c = k.create(str(ticks_hash), "--seed", "5")
    l1, r1 = k.probe(c, "world.now", "world.random", "world.now", 'world.history:{"n":501}')
    l2, r2 = k.probe(c, "world.now", "world.limits")
    time.sleep(1.5)
    l3, _ = k.probe(c, "world.now")
    recs = k.ledger(c)
    R.check(
        "1.5",
        l1[0]["returned"] == 0
        and l1[2]["returned"] == 6
        and [x["world_time"] for x in l1] == [3, 6, 9, 9]
        and l1[3]["code"] == "INVALID",
        f"tick_per_primitive=3 from the Build: world.now 0, then 6 after two executed primitives; record world_times {[x['world_time'] for x in l1]}; a refused precondition does not tick",
    )
    R.check(
        "1.5",
        r1["world"]["time"] == 16
        and l2[0]["returned"] == 16
        and r2["world"]["time"] == 16 + 3 + 3 + 7,
        f"tick_per_turn=7 at turn end: turn 1 ends at {r1['world']['time']}, turn 2 at {r2['world']['time']}",
    )
    R.check(
        "1.5",
        l3[0]["returned"] == r2["world"]["time"],
        f"1.5 s of host time between turns moves world time by 0 (world.now {l3[0]['returned']} == {r2['world']['time']})",
    )
    lim = l2[1]["returned"]
    R.check(
        "1.5",
        lim["tick_per_primitive"] == 3 and lim["tick_per_turn"] == 7,
        "world.limits reports the Build's tick values",
    )
    wt_problems = []
    for iid, (tpp, tpt) in [(a, (1, 10)), (b, (1, 10)), (c, (3, 7))]:
        wt_problems += expected_world_times(k.ledger(iid), tpp, tpt)
    R.check(
        "1.5",
        not wt_problems,
        f"world_time of every ledger record recomputed from kinds and outcomes alone{first_bad(wt_problems)}",
    )
    pre = [r for r in recs if r["primitive"] == "world.history"]
    R.check(
        "1.5",
        pre
        and pre[0]["preconditions"]
        and pre[0]["preconditions"][0]["passed"] is False
        and pre[0]["outcome"]["code"] == "INVALID",
        "refused world.history records precondition 1 failed with INVALID and no tick",
    )


def scenario_rng(k: Kernel, base: Path, base_hash: str) -> dict:
    seed = 2026
    a = k.create(base_hash, "--seed", str(seed))
    b = k.create(base_hash, "--seed", str(seed))
    seq = ["world.random", "world.now", "world.random", "world.limits", "world.random"]
    la, _ = k.probe(a, *seq)
    lb, _ = k.probe(b, *seq)
    la2, _ = k.probe(a, "world.random", "world.random")
    lb2, _ = k.probe(b, "world.random", "world.random")
    da, db = draws_of(la) + draws_of(la2), draws_of(lb) + draws_of(lb2)
    R.check(
        "6.6",
        da == db and len(da) == 5,
        f"equal seed and equal ledger prefix give equal draws across two Instances ({len(da)} draws)",
    )
    ledger_a = [strip_host(r) for r in k.ledger(a)]
    ledger_b = [strip_host(r) for r in k.ledger(b)]
    R.check(
        "6.6",
        ledger_a == ledger_b,
        f"the two ledgers are identical except host_ts, duration_us and instance ({len(ledger_a)} records, chains included)",
    )
    oracle = pcg_draws(seed, 5)
    R.check(
        "6.6",
        da == oracle,
        f"draws equal an independent PCG32 (pcg32_srandom_r(seed, 0x{PCG_STREAM:x})) stream across separate kernel processes: {da[:3]}...",
    )
    # Advanced only by draws: interleaving other primitives (and a refused call) does not move the stream.
    c = k.create(base_hash, "--seed", str(seed))
    lc, _ = k.probe(
        c,
        "world.now",
        "world.limits",
        'world.history:{"n":3}',
        "world.version",
        "world.random",
        "world.seed",
        "world.random",
    )
    k.probe(c, 'world.history:{"n":9999}')
    lc2, _ = k.probe(c, "world.random", "world.now", "world.now", "world.random", "world.random")
    dc = draws_of(lc) + draws_of(lc2)
    R.check(
        "6.6",
        dc == oracle,
        "a different interleaving of non-draw primitives and a refused call leaves the draw sequence unchanged",
    )
    m = k.meta(c)
    R.check(
        "6.6",
        m["rng"]["draws"] == 5 == sum(1 for r in k.ledger(c) if r["primitive"] == "world.random"),
        f"RNG draw count in metadata ({m['rng']['draws']}) equals world.random records in the ledger",
    )
    d = k.create(base_hash, "--seed", str(seed + 1))
    ld, _ = k.probe(d, *seq)
    R.check(
        "6.6",
        draws_of(ld) != draws_of(la) and draws_of(ld) == pcg_draws(seed + 1, 3),
        "a different seed gives a different, oracle-equal stream",
    )
    big = (1 << 64) - 1
    e = k.create(base_hash, "--seed", str(big))
    le, _ = k.probe(e, "world.random", "world.random", "world.seed")
    R.check(
        "6.6",
        draws_of(le) == pcg_draws(big, 2) and le[2]["returned"] == big,
        "seed 2^64-1 is accepted, visible through world.seed and draws match the oracle",
    )
    f = k.create(base_hash)
    lf, _ = k.probe(f, "world.random", "world.seed")
    R.check(
        "6.6",
        draws_of(lf) == pcg_draws(0, 1) and k.ledger(f)[0]["seed"] == 0,
        "omitted --seed is the fixed default 0, recorded in the header",
    )
    return {"seed": seed, "oracle": oracle}


def scenario_ledger(
    k: Kernel,
    exe: Path,
    base: Path,
    base_hash: str,
    xbuild: Path,
    xflag: str,
    tampered: Path,
    work: Path,
) -> None:
    iid = k.create(base_hash, "--seed", "3", "--arm", "control", "--model", "fixture-model")
    snaps = [k.ledger_bytes(iid)]
    calls_out = []
    for calls in (
        ["world.now", "world.random"],
        ['world.history:{"n":2}', "world.limits", "world.seed"],
        ['world.history:{"n":-1}'],
        ["world.version"],
    ):
        lines, res = k.probe(iid, *calls)
        calls_out.append((lines, res))
        snaps.append(k.ledger_bytes(iid))
    snap = k.snapshot(iid)
    snaps.append(k.ledger_bytes(iid))
    R.check(
        "8.1",
        all(
            snaps[i + 1].startswith(snaps[i]) and len(snaps[i + 1]) >= len(snaps[i])
            for i in range(len(snaps) - 1)
        ),
        f"append-only: each of {len(snaps)} successive ledger states is a byte prefix of the next",
    )
    problems = schema_problems(snaps[-1])
    recs = k.ledger(iid)
    R.check(
        "8.1",
        not problems,
        f"{len(recs)} records match the kernel.md section 8 schema, canonical JSONL, seq 0..{len(recs) - 1} without gaps, hash chain verified independently{first_bad(problems)}",
    )
    h = recs[0]
    R.check(
        "8.1",
        h["kind"] == "header"
        and h["seed"] == 3
        and h["arm"] == "control"
        and h["model"] == "fixture-model"
        and h["tail_size"] == 20
        and h["build"] == f"sha256:{base_hash}"
        and h["created_from"] is None
        and h["world_time"] == 0
        and h["turn"] == 0,
        "header at seq 0 carries Build hash, seed, K, arm, model and created_from, world_time 0, turn 0",
    )
    # Completeness: every call is one primitive record; every turn has a turn record with its call count.
    complete = []
    for lines, res in calls_out:
        t = res["world"]["turn"]
        prims = [r for r in recs if r["kind"] == "primitive" and r["turn"] == t]
        turns = [r for r in recs if r["kind"] == "turn" and r["turn"] == t]
        complete.append(
            len(prims) == len(lines)
            and len(turns) == 1
            and turns[0]["calls"] == len(lines)
            and [p["primitive"] for p in prims] == [ln["primitive"] for ln in lines]
            and [p["world_time"] for p in prims] == [ln["world_time"] for ln in lines]
            and [p["outcome"]["code"] for p in prims] == [ln["code"] for ln in lines]
            and turns[0]["status"] == res["status"]
            and turns[0]["world_time"] == res["world"]["time"]
        )
    R.check(
        "8.1",
        all(complete),
        f"complete records: {sum(len(ln) for ln, _ in calls_out)} calls in {len(calls_out)} turns each have one primitive record (name, outcome, ticks, turn) and one turn record",
    )
    ph = [r["params_hash"] for r in recs if r["kind"] == "primitive"]
    hist_hashes = {r["params_hash"] for r in recs if r["primitive"] == "world.history"}
    R.check(
        "8.1",
        len(hist_hashes) == 2 and ph.count(recs[1]["params_hash"]) >= 4,
        "params_hash is a function of the parameters: equal for equal params, different for n=2 and n=-1",
    )

    # Durability before return, observed with strace.
    st = work / "strace.txt"
    p = run(
        [
            "strace",
            "-f",
            "-y",
            "-e",
            "trace=write,fsync,fdatasync",
            "-o",
            str(st),
            str(exe),
            "instance",
            "probe",
            iid,
            "world.random",
            "world.now",
            "world.random",
            "--home",
            str(k.home),
        ],
        cwd=work,
    )
    events = []
    for line in st.read_text().splitlines() if st.exists() else []:
        mm = re.match(r"^\d+\s+(write|fsync|fdatasync)\((\d+)<([^>]*)>", line)
        if not mm:
            continue
        call, fd, path = mm.group(1), int(mm.group(2)), mm.group(3)
        if path.endswith("/ledger.jsonl"):
            events.append("L" if call == "write" else "S")
        elif fd == 1 and call == "write":
            events.append("O")
    ok, pending, synced_since_out, outs = True, 0, 0, 0
    for e in events:
        if e == "L":
            pending += 1
        elif e == "S":
            synced_since_out += pending
            pending = 0
        else:
            outs += 1
            ok = ok and pending == 0 and synced_since_out >= 1
            synced_since_out = 0
    seq = "".join(events)
    R.check(
        "8.1",
        p.returncode == 0 and ok and outs == 4 and seq.count("L") == 4,
        f"strace: each of 4 records (3 primitives + turn) is written then fsynced before its result reaches stdout (event order {seq})",
    )

    # Truncated final record: refused until restore runs; restore retains the torn bytes.
    good = k.ledger_bytes(iid)
    with open(k.dir(iid) / "ledger.jsonl", "ab") as fh:
        fh.write(b'{"seq":999,"kind":"primi')
    torn = k.ledger_bytes(iid)
    snap_file = next(
        f for f in (k.dir(iid) / "snapshots").iterdir() if snap.split(":")[-1] in f.name
    )
    n_inst = len(list((k.home / "instances").iterdir()))
    # kernel.md section 8 names create --from-snapshot, turn and replay; probe, snapshot, tail and
    # the ledger check are held to the same rule. An idle Instance is not a crash state, so turn
    # must not recover it implicitly.
    refused = {cmd: k(cmd, iid) for cmd in ("probe", "snapshot", "tail", "ledger")}
    refused["probe"] = k("probe", iid, "world.now")
    refused["turn"] = run(
        [str(k.exe), "instance", "turn", iid, "--home", str(k.home)],
        cwd=k.work,
        input="import world\nreturn world.now()\n",
    )
    refused["create --from-snapshot"] = k("create", base_hash, "--from-snapshot", str(snap_file))
    refused["replay"] = k("replay", iid)
    rc = {c: (p.returncode, "truncated" in p.stderr) for c, p in refused.items()}
    R.check(
        "8.1",
        all(r[0] != 0 and r[1] for r in rc.values())
        and k.ledger_bytes(iid) == torn
        and torn.startswith(good)
        and k.meta(iid)["status"] == "idle"
        and len(list((k.home / "instances").iterdir())) == n_inst,
        "a truncated final record of an idle Instance is refused by turn, create --from-snapshot, "
        f"replay, probe, snapshot, tail and ledger check, ledger bytes untouched (no implicit recovery): {rc}",
    )
    rp = k("restore", iid, snap)
    rec = json.loads(rp.stdout) if rp.returncode == 0 else {}
    retained = (k.dir(iid) / rec["retained"]).read_bytes() if rec.get("retained") else b""
    after = k("ledger", iid)
    R.check(
        "8.1",
        rp.returncode == 0
        and rec.get("kind") == "recovery"
        and retained.endswith(b'{"seq":999,"kind":"primi')
        and after.returncode == 0,
        "restore is the explicit recovery: recovery record appended, torn bytes retained under retained/, ledger verifies again",
    )

    # Tampering anywhere is detected by the chain.
    t_id = k.create(base_hash, "--seed", "9")
    k.probe(t_id, "world.random", "world.now")
    lp = k.dir(t_id) / "ledger.jsonl"
    text = lp.read_text()
    lines = text.splitlines(keepends=True)
    r2 = json.loads(lines[2])
    r2["world_time"] += 1
    lp.write_text("".join([*lines[:2], canonical(r2) + "\n", *lines[3:]]))
    chk = k("ledger", t_id)
    pr = k("probe", t_id, "world.now")
    R.check(
        "8.1",
        chk.returncode != 0 and "chain" in chk.stderr and pr.returncode != 0,
        "an edited record (world_time +1, chain left alone) is refused by the ledger check and by the next turn",
    )
    lp.write_text("".join(lines[:1] + lines[2:]))
    chk2 = k("ledger", t_id)
    R.check("8.1", chk2.returncode != 0, "a deleted record (seq gap) is refused")
    lp.write_text(text)

    # Experimental and tampered Builds; experimental flag on every record.
    x_no = k("create", "--build", str(xbuild))
    xid = k.create("--build", str(xbuild), "--allow-experimental")
    k.probe(xid, "world.random", "world.now")
    xrecs = k.ledger(xid)
    R.check(
        "8.1",
        x_no.returncode == 3
        and all(r["experimental"] == xflag for r in xrecs)
        and all(r["experimental"] is None for r in recs),
        f"experimental Build refused without the flag (exit {x_no.returncode}); with it all {len(xrecs)} records carry experimental={xflag!r}; control records carry null",
    )
    n_before = len(list((k.home / "instances").iterdir()))
    tp = k("create", "--build", str(tampered))
    R.check(
        "8.1",
        tp.returncode == 2 and len(list((k.home / "instances").iterdir())) == n_before,
        f"a Build whose bytes do not match its manifest hash is refused before any Instance exists (exit {tp.returncode})",
    )


def scenario_snapshots(k: Kernel, base: Path, base_hash: str, work: Path) -> None:
    before = tree_digest(base)
    initial = work / "initial.json"
    initial.write_text(json.dumps({"kv": {"items": {"greeting": "hello", "n": "1"}}}))
    iid = k.create(
        base_hash, "--seed", "77", "--tail", "5", "--initial", str(initial), "--arm", "a1"
    )
    d = k.dir(iid)
    st = k("state", iid)
    state = json.loads(st.stdout)
    m = k.meta(iid)
    objs = list((d / "objects").iterdir())
    R.check(
        "8.2",
        not d.resolve().is_relative_to(base.resolve())
        and state["kv"]["items"]["greeting"] == "hello"
        and state["world"] == {}
        and "sha256:" + hashlib.sha256(canonical(state).encode()).hexdigest()
        == m["state_hash"]
        == k.ledger(iid)[0]["initial_state"],
        "typed per-module state (kv, world) lives in the Instance directory outside the Build; its sha256 is the address recorded in metadata and header",
    )
    R.check(
        "8.2",
        objs and all(hashlib.sha256(o.read_bytes()).hexdigest() == o.name for o in objs),
        f"object store is content-addressed: {len(objs)} blobs, each named by the sha256 of its bytes",
    )

    _l1, _ = k.probe(iid, "world.random", "world.random", "world.now")
    s1 = k.snapshot(iid)
    m1 = k.meta(iid)
    sp = d / "snapshots" / (s1.removeprefix("sha256:") + ".json")
    sv = json.loads(sp.read_text())
    R.check(
        "8.2",
        hashlib.sha256(sp.read_bytes()).hexdigest() == s1.removeprefix("sha256:")
        and sv["state"] == state
        and sv["seq"] == m1["seq"]
        and sv["world_time"] == m1["world_time"]
        and sv["turn"] == m1["turn"]
        and sv["rng"] == m1["rng"]
        and sv["tail_size"] == 5
        and sv["build"] == f"sha256:{base_hash}"
        and sv["seed"] == 77
        and sv["chain"] == k.ledger(iid)[-1]["chain"],
        f"snapshot {s1[:19]}... is content-addressed and captures state, seq {sv['seq']}, world_time {sv['world_time']}, rng, K=5 and Build hash",
    )
    probe_seq = [
        "world.now",
        "world.random",
        "world.limits",
        "world.random",
        "world.seed",
        "world.version",
        "world.now",
    ]
    branch1, res1 = k.probe(iid, *probe_seq)
    branch1b, res1b = k.probe(iid, "world.random", "world.now")
    abandoned_tail = k.ledger(iid)
    rp = k("restore", iid, s1)
    rec = json.loads(rp.stdout) if rp.returncode == 0 else {}
    m2 = k.meta(iid)
    same_fields = all(
        m2[f] == m1[f]
        for f in ("state_hash", "world_time", "turn", "rng", "tail_size", "build", "seed")
    )
    R.check(
        "8.2",
        rp.returncode == 0
        and same_fields
        and rec["resumed_from"] == m1["seq"]
        and rec["seq"] == m1["seq"] + 1
        and rec["snapshot"] == s1
        and rec["world_time"] == m1["world_time"]
        and json.loads(k("state", iid).stdout) == state,
        f"restore yields equal state, world_time, turn, rng, K and Build; ledger resumes from seq {m1['seq']} with a recovery record at {m1['seq'] + 1}",
    )
    live = k.ledger(iid)
    R.check(
        "8.2",
        [strip_host(r) for r in live[: m1["seq"] + 1]]
        == [strip_host(r) for r in abandoned_tail[: m1["seq"] + 1]]
        and rec.get("retained")
        and len([json.loads(x) for x in (d / rec["retained"]).read_text().splitlines()])
        == len(abandoned_tail),
        "the live ledger is the snapshot's prefix plus recovery; the abandoned lineage is retained byte-for-byte for inspection",
    )
    branch2, res2 = k.probe(iid, *probe_seq)
    branch2b, res2b = k.probe(iid, "world.random", "world.now")
    R.check(
        "8.2",
        [x["returned"] for x in branch2] == [x["returned"] for x in branch1]
        and [x["returned"] for x in branch2b] == [x["returned"] for x in branch1b]
        and res2["world"] == res1["world"]
        and res2b["world"] == res1b["world"],
        "observably identical: the same turns after restore return the same values, draws and world coordinates as before restore",
    )

    # Restore back into the retained lineage.
    s2 = k.snapshot(iid)
    m_s2 = k.meta(iid)
    k.probe(iid, "world.random")
    r_old = k("restore", iid, s1)
    r_new = k("restore", iid, s2)
    m3 = k.meta(iid)
    R.check(
        "8.2",
        r_old.returncode == 0
        and r_new.returncode == 0
        and all(m3[f] == m_s2[f] for f in ("state_hash", "world_time", "turn", "rng", "tail_size")),
        "a snapshot from a lineage abandoned by an earlier restore is found in retained/ and restored exactly",
    )
    k_ok = k("ledger", iid)
    R.check(
        "8.2",
        k_ok.returncode == 0 and not schema_problems(k.ledger_bytes(iid)),
        "after three restores the live ledger still verifies (seq, chain, schema)",
    )

    # Refusals: a tampered snapshot (exit 2) and a snapshot from another Instance.
    bad = work / "bad_snapshot"
    bad.mkdir(exist_ok=True)
    tampered = bad / sp.name
    tampered.write_text(sp.read_text().replace('"world_time":', '"world_time":1', 1))
    rt = k("restore", iid, str(tampered))
    other = k.create(base_hash, "--seed", "77")
    ro = k("restore", other, s1.removeprefix("sha256:"))
    ro2 = k("restore", other, str(d / "snapshots" / sp.name))
    R.check(
        "8.2",
        rt.returncode == 2
        and ro.returncode != 0
        and ro2.returncode != 0
        and "belongs to instance" in ro2.stderr,
        f"tampered snapshot refused with exit {rt.returncode}; another Instance's snapshot refused (exit {ro2.returncode})",
    )

    # A new Instance from a snapshot continues the captured world.
    fid = k.create(base_hash, "--from-snapshot", str(d / "snapshots" / sp.name))
    fm = k.meta(fid)
    lf, _rf = k.probe(fid, *probe_seq)
    R.check(
        "8.2",
        fm["state_hash"] == m1["state_hash"]
        and fm["world_time"] == m1["world_time"]
        and fm["rng"] == m1["rng"]
        and fm["tail_size"] == 5
        and k.ledger(fid)[0]["created_from"] == s1
        and [x["returned"] for x in lf] == [x["returned"] for x in branch1],
        "create --from-snapshot starts a new Instance with the captured state, clock, RNG and K; header created_from names the snapshot",
    )

    # Destroy: ledger sealed and retained, state and snapshots released, no further turns.
    dp = k("destroy", iid)
    sealed = d / "ledger.jsonl"
    after = k("probe", iid, "world.now")
    R.check(
        "8.2",
        dp.returncode == 0
        and sealed.is_file()
        and not (sealed.stat().st_mode & 0o222)
        and not schema_problems(sealed.read_bytes()),
        "destroy seals the ledger read-only (mode without write bits) and retains it intact",
    )
    R.check(
        "8.2",
        not (d / "objects").exists()
        and not (d / "snapshots").exists()
        and after.returncode != 0
        and json.loads((d / "instance.json").read_text())["status"] == "destroyed",
        "destroy releases state and snapshots and refuses further turns",
    )
    R.check(
        "8.2",
        tree_digest(base) == before and build_hash(base) == base_hash,
        "the Build directory is byte- and mtime-identical after every Instance operation",
    )


TAIL_RE = re.compile(
    r"^t(\d+)\s+(?:wt(\d+)\s+(\S+)|turn)\s+(ok|error|partial|drift)(?:\s+([A-Z_]+))?(?:\s+(\w+=\S+))*$"
)


def tail_matches(lines: list[str], records: list[dict], k: int | None) -> list[str]:
    shown = [
        r for r in records if r["kind"] in ("primitive", "adapter", "turn", "drift", "proposal")
    ]
    if k is not None:
        shown = shown[-k:]
    problems = []
    if len(lines) != len(shown):
        return [f"{len(lines)} lines for {len(shown)} records"]
    for line, r in zip(lines, shown, strict=False):
        m = TAIL_RE.match(line)
        if not m:
            problems.append(f"unparseable line {line!r}")
            continue
        turn, wt, name, status, code = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
        if r["kind"] == "turn":
            ok = (
                int(turn) == r["turn"]
                and wt is None
                and status == r["status"]
                and code == r.get("code")
                and f"calls={r['calls']}" in line
            )
        else:
            ok = (
                int(turn) == r["turn"]
                and int(wt) == r["world_time"]
                and name == r["primitive"]
                and status == r["outcome"]["status"]
                and code == r["outcome"]["code"]
            )
        if not ok:
            problems.append(f"line {line!r} does not render seq {r['seq']}")
    return problems


def scenario_tail(k: Kernel, base_hash: str) -> None:
    dflt = k.create(base_hash)
    five = k.create(base_hash, "--tail", "5")
    full = k.create(base_hash, "--tail", "full")
    for iid in (dflt, five, full):
        for _ in range(4):
            k.probe(
                iid,
                "world.now",
                "world.random",
                "world.limits",
                'world.history:{"n":1}',
                "world.version",
            )
        k.probe(iid, 'world.history:{"n":501}')
    hd, h5, hf = (k.ledger(i)[0]["tail_size"] for i in (dflt, five, full))
    R.check(
        "9.4",
        hd == 20 and h5 == 5 and hf == "full" and k.meta(dflt)["tail_size"] == 20,
        f"K is a per-Instance parameter recorded in the header: default {hd}, --tail 5 gives {h5}, --tail full gives {hf!r}",
    )
    td, t5, tf = k.tail(dflt), k.tail(five), k.tail(full)
    pd = tail_matches(td, k.ledger(dflt), 20)
    p5 = tail_matches(t5, k.ledger(five), 5)
    pf = tail_matches(tf, k.ledger(full), None)
    R.check(
        "9.4",
        len(td) == 20 and len(t5) == 5 and len(tf) == 26 and not (pd or p5 or pf),
        f"tail renders the last K records one per line with turn, primitive, status, code and world_time, oldest first ({len(td)}/{len(t5)}/{len(tf)} lines){first_bad(pd + p5 + pf)}",
    )
    R.check(
        "9.4",
        any("world.history" in ln and "error" in ln and "INVALID" in ln for ln in t5)
        and any(" turn " in ln and "error" in ln and "INVALID" in ln for ln in t5),
        f"failure codes are rendered: {[ln for ln in t5 if 'INVALID' in ln][:2]}",
    )
    s = k.snapshot(five)
    sv = json.loads((k.dir(five) / "snapshots" / (s.removeprefix("sha256:") + ".json")).read_text())
    k.probe(five, "world.now")
    rp = k("restore", five, s)
    rec = json.loads(rp.stdout) if rp.returncode == 0 else {}
    R.check(
        "9.4",
        sv["tail_size"] == 5
        and rec.get("tail_size") == 5
        and k.meta(five)["tail_size"] == 5
        and len(k.tail(five)) == 5,
        "K is captured in every snapshot, restored with it and recorded in the recovery record",
    )
    bad = [k("create", base_hash, "--tail", v).returncode for v in ("0", "-3", "many")]
    R.check(
        "9.4", all(rc != 0 for rc in bad), f"--tail 0, -3 and many are refused (exit codes {bad})"
    )


def main() -> int:
    exe, tests_ok, tests_evidence = build()
    work = Path(tempfile.mkdtemp(prefix="cwr-2_1-"))
    try:
        base, base_hash = make_build(work, "build")
        ticks, ticks_hash = make_build(
            work, "build", clock=(3, 7), seed_visible=False, drop=("world.seed",)
        )
        xflag = "x-drift"
        xbuild, _ = make_build(work, "xbuild", experimental=xflag)
        tampered = work / "tampered"
        shutil.copytree(base, tampered)
        tampered.chmod(0o755)
        (tampered / "manual.md").chmod(0o644)
        (tampered / "manual.md").write_text((tampered / "manual.md").read_text() + " ")
        k = Kernel(exe, work, work / "home")
        R.check(
            "8.2",
            tests_ok,
            tests_evidence
            + " (incl. module-state mutate, snapshot, mutate, restore through the library API)",
        )
        R.check(
            "8.1", tests_ok, tests_evidence + " (ledger chain, tamper and truncation unit tests)"
        )
        R.check(
            "9.4",
            tests_ok,
            tests_evidence
            + " (tail format equals the model-surface.md section 6 example byte for byte)",
        )

        with OBS.observe("CWR-006.a3", match="host"):
            scenario_clock_and_host_time(k, base, ticks, ticks_hash)
        hidden = k.create(ticks_hash, "--seed", "4")
        lh, _ = k.probe(hidden, 'world.history:{"n":5}')
        refused = k("probe", hidden, "world.seed")
        R.check(
            "1.5",
            "seed" not in lh[0]["returned"][0] and refused.returncode != 0,
            "with rng.seed_visible false world.history hides the header seed and world.seed is not enumerated, so it is refused",
        )
        with OBS.observe("CWR-006.a4"):
            scenario_rng(k, base, base_hash)
        with (
            OBS.observe("CWR-006.a2", match="fsynced|byte prefix|complete records"),
            OBS.observe("CWR-006.a5", match="truncated"),
        ):
            scenario_ledger(k, exe, base, base_hash, xbuild, xflag, tampered, work)
        with OBS.observe("CWR-006.a1", match="restore|snapshot"):
            scenario_snapshots(k, base, base_hash, work)
        scenario_tail(k, base_hash)
    except Exception as e:  # a crash is a failure of every clause not yet judged, with the reason
        import traceback

        traceback.print_exc()
        for c in CLAUSES:
            R.check(c, False, f"driver aborted: {type(e).__name__}: {e}")
    finally:
        for p in sorted(work.rglob("*"), reverse=True):
            with contextlib.suppress(OSError):
                p.chmod(0o755 if p.is_dir() else 0o644)
        shutil.rmtree(work, ignore_errors=True)

    OBS.emit()
    failed = False
    for c in CLAUSES:
        ok, bad = R.ok[c], R.bad[c]
        if bad or not ok:
            failed = True
            print(
                f"CLAUSE {c} FAIL {len(bad)} of {len(ok) + len(bad)} checks failed: "
                + " | ".join(bad)
            )
        else:
            print(f"CLAUSE {c} PASS {len(ok)} checks: " + " | ".join(ok))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

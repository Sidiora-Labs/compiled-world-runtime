#!/usr/bin/env python3
# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Record, replay and freeze the refworld cassette sets through the real kernel gateway (CWR-014).

Python 3.12 standard library only. Every adapter call goes through `cwr instance turn --gateway
record|replay`, i.e. the Rust gateway of task 2.3 running the compiled refworld Build; nothing here
talks to a stub on the gateway's behalf. Contracts: spec/cwr/reference-world.md section 4,
spec/cwr/kernel.md section 7, benchmark/cassettes/FORMAT.md.

    python3 benchmark/stubs/record_refworld.py record [--set adapters|suite|all] [--out DIR] [--jobs N]
    python3 benchmark/stubs/record_refworld.py replay [--set ...] [--cassettes DIR] --out DIR [--jobs N]
    python3 benchmark/stubs/record_refworld.py digest [--cassettes DIR] [--check]

Common options: `--cwr BIN` (default $CWR_BIN, else <CARGO_TARGET_DIR or target>/debug/cwr) and
`--build DIR` (a compiled refworld Build; default: compile examples/refworld with `cwr build` into a
temporary directory).

Two cassette sets live under benchmark/cassettes:

| Set | Content |
|---|---|
| `refworld-adapters/<scenario>/seed-<n>.jsonl` | Scripted scenarios that call every adapter primitive and reach every declared adapter failure: stub-side rate limits, duplicate keys, scheduled 503, reset, malformed body and timeout faults, preconditions, the gateway key store. |
| `suite/<task>/seed-<n>.jsonl` | Every reference solution of benchmark/solutions run once per seed (1 to 10) against the stubs started with that seed, no faults. |

Next to each cassette, `seed-<n>.expect.json` records what record mode produced: the seed, the initial
state, the stub schedule, every turn script exactly as submitted and its result envelope, and the
ledger. `cwr instance turn --gateway replay` must reproduce both from the cassette alone, with no
socket opened (`replay` writes what it observed under --out so that a caller can compare). Host
measurements and identities are left out of both: resources.cpu_ms and resources.wall_ms of the
envelope, and host_ts, duration_us, instance and chain of ledger records. So is the Build hash
(world.build, ledger build), because it includes the compiler revision; the manifest records the
hash of the Build the set was recorded with.

`benchmark/cassettes/refworld.kvx` freezes the digest of each set (FORMAT.md section 5), the cassette
count and the Build hash of the recording, and for the suite set the digests of benchmark/solutions
and benchmark/tasks it was recorded from. `record --stub-logs DIR` also writes, per adapters
scenario, the stubs' own request log (GET /_stub/log) so that a driver can compare what the stubs saw
with what the cassette holds. Recording writes no timestamp, host name or Instance id
anywhere, so re-recording the same world against the same stubs reproduces every byte.

Recording uses stub schedules with allow_undeclared false only. Exit codes: 0 success; 1 a replay
mismatch, a digest mismatch or a failed run; 2 usage or environment error.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
STUBS = REPO / "benchmark/stubs"
CASSETTES = REPO / "benchmark/cassettes"
TASKS = REPO / "benchmark/tasks"
SOLUTIONS = REPO / "benchmark/solutions"
REFWORLD = REPO / "examples/refworld"
MANIFEST = "refworld.kvx"
SUITE_SEEDS = list(range(1, 11))
TURN_LIMIT = 200
RECORD_ATTEMPTS = 3
HOST_LIMITS = ("cpu_ms", "wall_ms")
HOST_LIMIT_ERROR = "host-time limit ended a turn during recording"
ENVELOPE_HOST_FIELDS = ("cpu_ms", "wall_ms")
LEDGER_HOST_FIELDS = ("host_ts", "duration_us", "instance", "chain", "build")


def canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def set_digest(root: Path, pattern: str = "*.jsonl") -> str:
    """FORMAT.md section 5, over every file matching pattern (default *.jsonl) under root."""
    lines = []
    paths = [p for p in root.rglob(pattern) if p.is_file() and "__pycache__" not in p.parts]
    for path in sorted(paths, key=lambda p: p.relative_to(root).as_posix()):
        lines.append(
            f"{path.relative_to(root).as_posix()}\t{hashlib.sha256(path.read_bytes()).hexdigest()}\n"
        )
    return "sha256:" + hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------------------------------------
# cwr binary, Build and stubs


def cwr_binary(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    if os.environ.get("CWR_BIN"):
        return Path(os.environ["CWR_BIN"])
    target = (
        Path(os.environ["CARGO_TARGET_DIR"])
        if os.environ.get("CARGO_TARGET_DIR")
        else REPO / "target"
    )
    return target / "debug" / "cwr"


def compile_refworld(cwr: Path, out: Path) -> Path:
    p = subprocess.run(
        [str(cwr), "build", str(REFWORLD), "--out", str(out), "--json"],
        capture_output=True,
        text=True,
        timeout=900,
    )
    try:
        report = json.loads(p.stdout)
    except ValueError:
        report = {}
    build = (report.get("build") or {}).get("dir")
    if p.returncode != 0 or not build:
        raise SystemExit(
            f"record_refworld: cwr build examples/refworld failed (exit {p.returncode}): {p.stdout[-1500:]} {p.stderr[-1500:]}"
        )
    return Path(build)


def build_hash(build: Path) -> str:
    for line in (build / "manifest.kvx").read_text().splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "hash":
            return json.loads(value.strip())
    return "sha256:" + build.name


class StubPair:
    """search and publish stub servers started with one seed."""

    def __init__(self, seed: int, cwd: Path) -> None:
        self.seed = seed
        self.procs: dict[str, subprocess.Popen] = {}
        self.urls: dict[str, str] = {}
        for name in ("search", "publish"):
            proc = subprocess.Popen(
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
                cwd=cwd,
            )
            ready = json.loads(proc.stdout.readline() or "{}")
            if ready.get("event") != "ready":
                raise SystemExit(f"record_refworld: stub {name} did not start: {ready}")
            self.procs[name] = proc
            self.urls[name] = f"http://127.0.0.1:{ready['port']}"

    def admin(self, name: str, method: str, path: str, body: Any = None) -> dict:
        data = None if body is None else canonical(body).encode()
        req = urllib.request.Request(
            self.urls[name] + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    def schedule(self, faults: list[dict]) -> None:
        doc = {
            "format": "cwr.stub.schedule",
            "schema_version": 1,
            "allow_undeclared": False,
            "faults": faults,
        }
        for name in self.urls:
            self.admin(name, "PUT", "/_stub/schedule", doc)

    def log(self) -> dict[str, list[dict]]:
        return {name: self.admin(name, "GET", "/_stub/log")["requests"] for name in self.urls}

    def alive(self) -> bool:
        """Both stub processes are still running."""
        return all(proc.poll() is None for proc in self.procs.values())

    def stop(self) -> None:
        for proc in self.procs.values():
            proc.terminate()
        for proc in self.procs.values():
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()


# ------------------------------------------------------------------------------------------------
# One run: an Instance driven turn by turn through `cwr instance turn`


class Run:
    def __init__(
        self,
        cwr: Path,
        build: Path,
        home: Path,
        seed: int,
        initial: dict | None,
        mode: str,
        cassette: Path,
        label: str,
        stubs: dict[str, str],
    ) -> None:
        self.cwr, self.home, self.mode, self.cassette, self.label, self.stubs = (
            cwr,
            home,
            mode,
            cassette,
            label,
            stubs,
        )
        self.n = 0
        cmd = [str(cwr), "instance", "create", "--home", str(home), str(build), "--seed", str(seed)]
        if initial is not None:
            f = (
                home.parent
                / f"initial-{hashlib.sha256(label.encode()).hexdigest()[:12]}-{mode}.json"
            )
            f.write_text(canonical(initial))
            cmd += ["--initial", str(f)]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if p.returncode != 0:
            raise RuntimeError(f"instance create failed: {p.stderr.strip()}")
        self.iid = p.stdout.strip()
        self.turns: list[dict] = []

    def turn(self, script: str) -> dict:
        self.n += 1
        f = self.home.parent / f"turn-{self.iid}-{self.n}.py"
        f.write_text(script)
        cmd = [
            str(self.cwr),
            "instance",
            "turn",
            self.iid,
            "--home",
            str(self.home),
            "--script",
            str(f),
            "--gateway",
            self.mode,
            "--cassette",
            str(self.cassette),
        ]
        if self.mode == "record":
            cmd += ["--cassette-label", self.label]
        for name, url in sorted(self.stubs.items()):
            cmd += ["--stub", f"{name}={url}"]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if p.returncode != 0:
            print(
                f"record_refworld: {self.label} turn {self.n}: {p.stderr.strip()[-600:]}",
                file=sys.stderr,
            )
            raise RuntimeError(f"turn {self.n} exited {p.returncode}")
        envelope = json.loads(p.stdout.strip().splitlines()[-1])
        self.turns.append({"script": script, "envelope": envelope_view(envelope)})
        return envelope

    def ledger(self) -> list[dict]:
        path = self.home / "instances" / self.iid / "ledger.jsonl"
        return [
            ledger_view(json.loads(line)) for line in path.read_text().splitlines() if line.strip()
        ]


def envelope_view(envelope: dict) -> dict:
    view = json.loads(json.dumps(envelope))
    for key in ENVELOPE_HOST_FIELDS:
        view.get("resources", {}).pop(key, None)
    view.get("world", {}).pop("build", None)
    return view


def ledger_view(record: dict) -> dict:
    return {k: v for k, v in record.items() if k not in LEDGER_HOST_FIELDS}


# ------------------------------------------------------------------------------------------------
# The adapters set: scripted scenarios over every adapter primitive and declared failure

PRELUDE = """import cwr, search, publish, world
r = {}
def call(label, f, **kw):
    try:
        r[label] = {"ok": f(**kw)}
    except cwr.errors.WorldError as e:
        r[label] = {"code": e.code, "retry_after": e.retry_after}
    return r[label]
"""

# Waiting for a rate-limit window to reopen: empty turns advance world_time by tick_per_turn (10)
# each without any host-time cost, so ten of them outlast every retry_after the stubs can give
# (at most 50 ticks for search, 100 for publish).
WAIT_TURNS = ["pass\n"] * 10

SEARCH_FAULTS = [
    {"adapter": "search.query", "index": 1, "fault": "UNAVAILABLE"},
    {"adapter": "search.query", "index": 2, "fault": "RESET"},
    {"adapter": "search.query", "index": 3, "fault": "MALFORMED"},
    {"adapter": "search.query", "index": 4, "fault": "TIMEOUT"},
    {"adapter": "search.query", "index": 5, "fault": "RATE_LIMITED", "retry_after": 25},
]
SEARCH_TURNS = [
    PRELUDE
    + """call("pre_invalid", search.query, q="", limit=5)
call("pre_limit", search.query, q="tennis", limit=51)
call("ok", search.query, q="tennis", limit=3)
call("unavailable", search.query, q="tennis", limit=3)
call("reset", search.query, q="golf", limit=3)
call("malformed", search.query, q="golf", limit=3)
call("timeout", search.query, q="golf", limit=3)
call("scheduled_rate_limited", search.query, q="golf", limit=3)
call("service_invalid", search.query, q="tennis", limit=-1)
call("ok_limit_0", search.query, q="golf", limit=0)
return r
""",
    PRELUDE
    + """for i in range(12):
    o = call("window_" + str(i), search.query, q="topic " + str(i), limit=1)
    if o.get("code") == "RATE_LIMITED":
        r["rate_limited_at"] = world.now() - 1
        break
return r
""",
    *WAIT_TURNS,
    PRELUDE
    + """call("retry_after_window", search.query, q="topic after the window", limit=1)
return r
""",
]

POST_FAULTS = [
    {"adapter": "publish.post", "index": 1, "fault": "UNAVAILABLE"},
    {"adapter": "publish.post", "index": 2, "fault": "RESET"},
    {"adapter": "publish.post", "index": 3, "fault": "MALFORMED"},
    {"adapter": "publish.post", "index": 4, "fault": "TIMEOUT"},
    {"adapter": "publish.post", "index": 5, "fault": "RATE_LIMITED", "retry_after": 13},
]
POST_TURNS = [
    PRELUDE
    + """call("pre_invalid", publish.post, key="k0", title="t" * 201, body="b")
call("ok", publish.post, key="k1", title="Title", body="first")
call("identical_replay", publish.post, key="k1", title="Title", body="first")
call("duplicate_key", publish.post, key="k1", title="Title", body="second")
call("unavailable", publish.post, key="k2", title="Title", body="b")
call("unavailable_repeat", publish.post, key="k2", title="Title", body="b")
call("reset", publish.post, key="k3", title="Title", body="b")
call("malformed", publish.post, key="k4", title="Title", body="b")
call("timeout", publish.post, key="k5", title="Title", body="b")
call("timeout_repeat", publish.post, key="k5", title="Title", body="b")
call("scheduled_rate_limited", publish.post, key="k6", title="Title", body="b")
return r
""",
    PRELUDE
    + """call("retry_after_scheduled", publish.post, key="k6", title="Title", body="b")
o = call("window_rate_limited", publish.post, key="k7", title="Title", body="b")
if o.get("code") == "RATE_LIMITED":
    r["rate_limited_at"] = world.now() - 1
receipt = r["retry_after_scheduled"]["ok"]
call("status_of_retried", publish.status, post_id=receipt["id"])
return r
""",
    *WAIT_TURNS,
    PRELUDE
    + """call("retry_after_window", publish.post, key="k7", title="Title", body="b")
return r
""",
]

STATUS_FAULTS = [
    {"adapter": "publish.status", "index": 2, "fault": "UNAVAILABLE"},
    {"adapter": "publish.status", "index": 3, "fault": "RESET"},
    {"adapter": "publish.status", "index": 4, "fault": "TIMEOUT"},
    {"adapter": "publish.delete", "index": 2, "fault": "UNAVAILABLE"},
    {"adapter": "publish.delete", "index": 3, "fault": "RESET"},
    {"adapter": "publish.delete", "index": 4, "fault": "TIMEOUT"},
]
STATUS_TURNS = [
    PRELUDE
    + """a = call("post_a", publish.post, key="a", title="A", body="a")["ok"]["id"]
b = call("post_b", publish.post, key="b", title="B", body="b")["ok"]["id"]
call("status_ok", publish.status, post_id=a)
call("status_not_found", publish.status, post_id="post_0000000000000000")
call("status_unavailable", publish.status, post_id=a)
call("status_reset", publish.status, post_id=a)
call("status_timeout", publish.status, post_id=a)
call("delete_ok", publish.delete, post_id=b)
call("delete_not_found", publish.delete, post_id=b)
call("delete_unavailable", publish.delete, post_id=a)
call("delete_reset", publish.delete, post_id=a)
call("delete_timeout", publish.delete, post_id=a)
call("status_after_timeout", publish.status, post_id=a)
call("delete_after_timeout", publish.delete, post_id=a)
return r
""",
]

CONTENT_TURNS = [
    PRELUDE
    + """call("hits", search.query, q="tennis", limit=5)
call("receipt", publish.post, key="k1", title="Title", body="b")
return r
""",
]

ADAPTER_SCENARIOS = [
    # (name, seed, faults, turns)
    ("search-query", 1, SEARCH_FAULTS, SEARCH_TURNS),
    ("publish-post", 1, POST_FAULTS, POST_TURNS),
    ("publish-status-delete", 1, STATUS_FAULTS, STATUS_TURNS),
    ("content", 1, [], CONTENT_TURNS),
    ("content", 2, [], CONTENT_TURNS),
]


# ------------------------------------------------------------------------------------------------
# The suite set: reference solutions


def parse_kvx(text: str) -> dict:
    sections: dict = {}
    current = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], {})
            continue
        key, _, value = line.partition("=")
        current[key.strip()] = json.loads(value.strip())
    return sections


def as_bytes(s: str) -> dict:
    return {"$bytes": base64.b64encode(s.encode("utf-8")).decode("ascii")}


def initial_state(task: str) -> dict:
    """benchmark/tasks/<task>/initial.kvx mapped onto the refworld lib state shapes (lib/*.py
    INITIAL_STATE): file contents and kv values are UTF-8 bytes; a job's submitted_at is the
    lib's `submitted`."""
    k = parse_kvx((TASKS / task / "initial.kvx").read_text())
    jobs = {}
    for jid, j in sorted(k["jobs"]["jobs"].items()):
        jobs[jid] = {
            "kind": j["kind"],
            "n": j["n"],
            "submitted": j["submitted_at"],
            "fail_at": j.get("fail_at"),
            "cancelled_at": j.get("cancelled_at"),
        }
    return {
        "fs": {
            "files": {p: as_bytes(c) for p, c in sorted(k["fs"]["files"].items())},
            "locks": list(k["fs"]["locks"]),
        },
        "kv": {
            "namespaces": {
                ns: {key: as_bytes(v) for key, v in sorted(vals.items())}
                for ns, vals in sorted(k["kv"]["namespaces"].items())
            }
        },
        "jobs": {"next_id": 1, "jobs": jobs},
    }


def suite_tasks() -> list[str]:
    return sorted(p.name for p in TASKS.iterdir() if p.is_dir() and p.name.startswith("t"))


def load_solution(task: str):
    path = SOLUTIONS / f"{task}.py"
    spec = importlib.util.spec_from_file_location(f"cwr_solution_{task.replace('-', '_')}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------------------------------------------
# Record and replay


def write_json(path: Path, value: Any) -> None:
    path.write_text(canonical(value) + "\n", encoding="utf-8")


def record_one(
    cwr: Path,
    build: Path,
    work: Path,
    out: Path,
    set_name: str,
    name: str,
    seed: int,
    faults: list,
    scripts: list[str] | None,
    solution_task: str | None,
    stubs: StubPair,
    attempt: int = 0,
) -> dict:
    home = work / f"{set_name}-{name}-{seed}-{attempt}" / "home"
    home.mkdir(parents=True)
    dest = out / set_name / name
    dest.mkdir(parents=True, exist_ok=True)
    cassette = dest / f"seed-{seed}.jsonl"
    if cassette.exists():
        cassette.unlink()
    initial = initial_state(solution_task) if solution_task else None
    run = Run(
        cwr, build, home, seed, initial, "record", cassette, f"{name}/seed-{seed}", stubs.urls
    )
    error = None  # a turn the kernel refused (exit code not 0)
    solution_error = None  # the reference solution itself stopped (driver side; not replayed)
    try:
        if scripts is not None:
            for s in scripts:
                run.turn(s)
        else:
            gen = load_solution(solution_task).turns()
            script = next(gen)
            while True:
                env = run.turn(script)
                if len(run.turns) >= TURN_LIMIT:
                    solution_error = f"turn limit {TURN_LIMIT} reached"
                    break
                try:
                    script = gen.send(env)
                except StopIteration:
                    break
                except Exception as e:  # the reference solution itself failed on this envelope
                    solution_error = (
                        f"reference solution raised {type(e).__name__} after turn {len(run.turns)}"
                    )
                    break
    except RuntimeError as e:
        error = str(e)
    if not faults and b'"transport_error"' in (cassette.read_bytes() if cassette.exists() else b""):
        # No fault was scheduled, so a transport error means a stub was not reachable: the run is
        # not a recording of the stubs' behaviour and the set must not be frozen.
        error = (
            error or "stub unreachable during recording (transport error with no fault scheduled)"
        )
    if not stubs.alive():
        error = error or "a stub server exited during recording"
    host_limits = sorted(
        {
            str(t["envelope"]["error"].get("limit"))
            for t in run.turns
            if (t["envelope"].get("error") or {}).get("limit") in HOST_LIMITS
        }
    )
    if host_limits:
        # cpu_ms and wall_ms are measured on the host: a turn they ended under host load is not a
        # recording of the world and would not replay identically on another host.
        error = error or f"{HOST_LIMIT_ERROR} ({', '.join(host_limits)})"
    expect = {
        "format": "cwr.cassette-expect",
        "schema_version": 1,
        "set": set_name,
        "label": f"{name}/seed-{seed}",
        "seed": seed,
        "initial": initial,
        "schedule": faults,
        "turns": run.turns,
        "ledger": run.ledger(),
        "error": error,
        "solution_error": solution_error,
    }
    write_json(dest / f"seed-{seed}.expect.json", expect)
    return {
        "label": f"{set_name}/{name}/seed-{seed}",
        "turns": len(run.turns),
        "error": error or solution_error,
        "kernel_error": error,
        "instance": run.iid,
    }


def record_retrying(*args: Any) -> dict:
    """record_one, re-run from a fresh Instance when the host (not the world) spoiled the run: a
    stub that was unreachable or a host-time limit. Never more than RECORD_ATTEMPTS runs."""
    result: dict = {}
    for attempt in range(RECORD_ATTEMPTS):
        result = record_one(*args, attempt=attempt)
        transient = result["kernel_error"] and (
            result["kernel_error"].startswith(HOST_LIMIT_ERROR)
            or result["kernel_error"].startswith("stub unreachable")
        )
        if not transient:
            break
    return result


def record(args) -> int:
    cwr = cwr_binary(args.cwr)
    out = Path(args.out)
    sets = ["adapters", "suite"] if args.set == "all" else [args.set]
    with tempfile.TemporaryDirectory(prefix="cwr-record-") as tmp:
        work = Path(tmp)
        build = Path(args.build) if args.build else compile_refworld(cwr, work / "builds")
        failures = []
        manifest = read_manifest(out)
        for s in sets:
            set_dir = out / ("refworld-adapters" if s == "adapters" else "suite")
            if set_dir.exists():
                shutil.rmtree(set_dir)
            results = []
            if s == "adapters":
                pairs: dict[int, StubPair] = {}
                try:
                    for name, seed, faults, turns in ADAPTER_SCENARIOS:
                        pair = pairs.get(seed) or pairs.setdefault(seed, StubPair(seed, work))
                        pair.schedule(faults)
                        seen = {k: len(v) for k, v in pair.log().items()}
                        results.append(
                            record_retrying(
                                cwr,
                                build,
                                work,
                                out,
                                "refworld-adapters",
                                name,
                                seed,
                                faults,
                                turns,
                                None,
                                pair,
                            )
                        )
                        pair.schedule([])
                        if args.stub_logs:
                            iid = results[-1]["instance"]
                            logs = {
                                k: [e for e in v[seen[k] :] if e["instance"] == iid]
                                for k, v in pair.log().items()
                            }
                            Path(args.stub_logs).mkdir(parents=True, exist_ok=True)
                            write_json(Path(args.stub_logs) / f"{name}-seed-{seed}.json", logs)
                finally:
                    for pair in pairs.values():
                        pair.stop()
            else:
                pairs = {seed: StubPair(seed, work) for seed in SUITE_SEEDS}
                try:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
                        futs = [
                            pool.submit(
                                record_retrying,
                                cwr,
                                build,
                                work,
                                out,
                                "suite",
                                task,
                                seed,
                                [],
                                None,
                                task,
                                pairs[seed],
                            )
                            for task in suite_tasks()
                            if not args.tasks or task in args.tasks.split(",")
                            for seed in SUITE_SEEDS
                        ]
                        results = [f.result() for f in futs]
                finally:
                    for pair in pairs.values():
                        pair.stop()
            name = "refworld-adapters" if s == "adapters" else "suite"
            manifest[name] = {
                "digest": set_digest(set_dir),
                "cassettes": len(list(set_dir.rglob("*.jsonl"))),
                "build": build_hash(build),
                "runs_incomplete": sorted(r["label"] for r in results if r["error"]),
                "turns": sum(r["turns"] for r in results),
            }
            if s == "suite":
                # What the suite set was recorded from, so that a later edit of a task or a
                # reference solution is detectable as a stale cassette set.
                manifest[name]["solutions_digest"] = set_digest(SOLUTIONS, "*.py")
                manifest[name]["tasks_digest"] = set_digest(TASKS, "*")
            # Adapters scenarios must all complete; in the suite set a reference solution may stop
            # early (task 4.2 material), but a refused turn or an unreachable stub never freezes.
            failures += [
                r for r in results if (r["error"] if s == "adapters" else r["kernel_error"])
            ]
            for r in results:
                if r["error"]:
                    print(f"record_refworld: {r['label']}: {r['error']}", file=sys.stderr)
            print(
                f"record_refworld: {name}: {manifest[name]['cassettes']} cassettes, {manifest[name]['turns']} turns, digest {manifest[name]['digest']}"
            )
        if failures:
            print("record_refworld: recording incomplete; manifest not written", file=sys.stderr)
        else:
            write_manifest(out, manifest)
    return 1 if failures else 0


def replay_one(
    cwr: Path,
    build: Path,
    work: Path,
    out: Path,
    expect_path: Path,
    cassettes: Path,
    offline: dict[str, str],
) -> dict:
    expect = json.loads(expect_path.read_text())
    rel = expect_path.relative_to(cassettes)
    cassette_src = expect_path.with_name(expect_path.name.replace(".expect.json", ".jsonl"))
    home = work / ("replay-" + rel.as_posix().replace("/", "_")) / "home"
    home.mkdir(parents=True)
    cassette = home.parent / "cassette.jsonl"
    shutil.copyfile(cassette_src, cassette)
    run = Run(
        cwr,
        build,
        home,
        expect["seed"],
        expect["initial"],
        "replay",
        cassette,
        expect["label"],
        offline,
    )
    error = None
    try:
        for t in expect["turns"]:
            run.turn(t["script"])
    except RuntimeError as e:
        error = str(e)
    observed = {
        "label": expect["label"],
        "turns": run.turns,
        "ledger": run.ledger(),
        "error": error,
        "cassette_unchanged": cassette.read_bytes() == cassette_src.read_bytes(),
    }
    dest = out / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    write_json(dest.with_name(dest.name.replace(".expect.json", ".replay.json")), observed)
    same = (
        observed["turns"] == expect["turns"]
        and observed["ledger"] == expect["ledger"]
        and error == expect["error"]
    )
    return {"label": rel.as_posix(), "same": same and observed["cassette_unchanged"]}


def replay(args) -> int:
    cwr = cwr_binary(args.cwr)
    cassettes = Path(args.cassettes)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    dirs = {
        "adapters": ["refworld-adapters"],
        "suite": ["suite"],
        "all": ["refworld-adapters", "suite"],
    }[args.set]
    offline = {name: args.offline_url for name in ("search", "publish")}
    with tempfile.TemporaryDirectory(prefix="cwr-replay-") as tmp:
        work = Path(tmp)
        build = Path(args.build) if args.build else compile_refworld(cwr, work / "builds")
        expects = [p for d in dirs for p in sorted((cassettes / d).rglob("*.expect.json"))]
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
            results = list(
                pool.map(
                    lambda p: replay_one(cwr, build, work, out, p, cassettes, offline), expects
                )
            )
    bad = [r["label"] for r in results if not r["same"]]
    print(
        f"record_refworld: replayed {len(results)} cassettes offline; {len(results) - len(bad)} reproduce record mode exactly"
        + (f"; differing: {bad[:10]}" if bad else "")
    )
    return 1 if bad or not results else 0


# ------------------------------------------------------------------------------------------------
# Manifest


MANIFEST_HEADER = """# Frozen refworld cassette sets (task 4.1; spec/cwr/reference-world.md section 4, clauses 7.5 and 11.5).
# Written by benchmark/stubs/record_refworld.py record; do not edit by hand. digest is the set digest of
# benchmark/cassettes/FORMAT.md section 5 over every *.jsonl file of the set directory. Drivers and
# campaigns use replay mode only; re-recording produces a new digest and invalidates every result
# recorded under the old one. build is the refworld Build the cassettes were recorded with.
"""


def read_manifest(root: Path) -> dict:
    path = root / MANIFEST
    if not path.exists():
        return {}
    k = parse_kvx(path.read_text())
    return {name.split(".", 1)[1]: v for name, v in k.items() if name.startswith("set.")}


def write_manifest(root: Path, sets: dict) -> None:
    lines = [
        MANIFEST_HEADER,
        "[meta]",
        'format = "cwr.cassette-sets"',
        "schema_version = 1",
        'request_hash = "sha256-cwr-canonical-json-v1"',
        'recorded_with = "cwr instance turn --gateway record"',
        'stub_schedules = "allow_undeclared false"',
        "",
    ]
    for name in sorted(sets):
        lines.append(f"[set.{name}]")
        for key in sorted(sets[name]):
            lines.append(f"{key} = {canonical(sets[name][key])}")
        lines.append("")
    (root / MANIFEST).write_text("\n".join(lines))


def digest(args) -> int:
    root = Path(args.cassettes)
    manifest = read_manifest(root)
    bad = 0
    for name in ("refworld-adapters", "suite"):
        d = root / name
        if not d.exists():
            continue
        got = set_digest(d)
        want = manifest.get(name, {}).get("digest")
        same = got == want
        bad += 0 if same or not args.check else 1
        print(
            f"{name}\t{got}"
            + ("" if not args.check else ("\tfrozen" if same else f"\tDIFFERS from frozen {want}"))
        )
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("record", "replay", "digest"):
        p = sub.add_parser(name)
        p.add_argument("--cwr")
        p.add_argument("--build")
        if name != "digest":
            p.add_argument("--set", choices=["adapters", "suite", "all"], default="all")
            p.add_argument("--jobs", type=int, default=4)
        if name == "record":
            p.add_argument("--out", default=str(CASSETTES))
            p.add_argument(
                "--tasks",
                help="comma-separated task ids (diagnostics only; a frozen suite set has every task)",
            )
            p.add_argument(
                "--stub-logs",
                help="directory for the stub request logs of each adapters scenario (GET /_stub/log)",
            )
        if name in ("replay", "digest"):
            p.add_argument("--cassettes", default=str(CASSETTES))
        if name == "replay":
            p.add_argument("--out", required=True)
            p.add_argument(
                "--offline-url",
                default="http://127.0.0.1:9",
                help="stub URL bound in replay mode; nothing may contact it",
            )
        if name == "digest":
            p.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    return {"record": record, "replay": replay, "digest": digest}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())

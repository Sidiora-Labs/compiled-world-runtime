#!/usr/bin/env python3
# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Task 4.2 driver: task suite with bins, rule-reuse tasks and success predicates (CWR-015).

Clauses 11.2 and 11.3 of spec/cwr/spec.kvx, refined by spec/cwr/reference-world.md sections 5 to 8.
Everything is exercised against the real implementation, nothing is mocked:

1. `cargo build -p cwr-cli` (honours CARGO_TARGET_DIR; default: the workspace target dir).
2. A copy of examples/refworld (without __pycache__) is compiled with the real `cwr build`.
3. The refworld stub servers of benchmark/stubs are started with seed 1.
4. For every one of the 60 tasks under benchmark/tasks: an Instance is created from the Build with
   the task's initial.kvx (mapped by `common.kernel_initial_state`), and the reference solution in
   benchmark/solutions is driven turn by turn through `cwr instance turn`: once with
   `--gateway record` into a per-task cassette, which only produces the cassette, then twice with
   `--gateway replay` against that cassette with no stub bound (no socket can be opened). The two
   replay runs are the evidence.
5. The predicate is loaded with `common.load_predicate` (purity audit plus an import hook that
   admits only `common`) and evaluated with `common.evaluate_twice` on each replay run's final
   state view and ledger. The state view is built by `common.state_view_from_instance` from
   `cwr instance state`, the ledger and the gateway's key store only. The predicate must be true on
   both replay runs and false on the untouched initial Instance (header-only ledger).
6. The primitive count of each task is measured from the ledger (records of kind primitive or
   adapter) and must equal across runs, fall inside the task's declared bin, and equal the
   `primitive_count` frozen in benchmark/suite.kvx. Bins must hold 20/20/20, and at least 12 long
   tasks must be rule-reuse tasks whose reused primitive has the section 6 shape in the ledger.
7. benchmark/suite.kvx digests must match the files on disk.

Prints one line per clause, `CLAUSE <req.ac> PASS|FAIL <evidence>`, and exits 0 only when every
clause passes. `--write-counts FILE` additionally writes the measured counts as JSON for
`benchmark/tasks/_authoring/build_suite.py --kernel-counts FILE`. `--only t001,t002` restricts the
run to some tasks (diagnostics; the clause lines then FAIL on suite shape). Python 3.12, standard
library only.
"""

from __future__ import annotations

import argparse
import ast
import concurrent.futures
import importlib.util
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

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parents[3]
TASKS = REPO / "benchmark/tasks"
SOLUTIONS = REPO / "benchmark/solutions"
SUITE = REPO / "benchmark/suite.kvx"
COMMON = REPO / "benchmark/predicates/common.py"
STUBS = REPO / "benchmark/stubs"
REFWORLD = REPO / "examples/refworld"
FROZEN = REPO / "benchmark/cassettes/suite"
CLAUSES = ["11.2", "11.3"]
BINS = {"short": (0, 5), "medium": (6, 25), "long": (26, 120)}
PER_BIN = 20
MIN_REUSE = 12
TURN_LIMIT = 200
REQUIRED_FILES = ("task.md", "initial.kvx", "success.py", "bin", "rules")
LEDGER_HOST_FIELDS = ("host_ts", "duration_us", "instance", "chain")
ENVELOPE_HOST_FIELDS = ("cpu_ms", "wall_ms")
KERNEL_MODULE_NAMES = {"cwr", "cwr_kernel", "cwr_state", "cwr_contract", "cwr_compiler", "sdk"}
MODEL_TEXT_NAMES = {"envelope", "message", "transcript", "stdout", "stderr", "context", "reply"}


def load_common():
    spec = importlib.util.spec_from_file_location("common", COMMON)
    module = importlib.util.module_from_spec(spec)
    sys.modules["common"] = module
    spec.loader.exec_module(module)
    return module


c = load_common()


class Results:
    """Per-clause results: a clause passes when it ran at least one check, no check failed and it
    has evidence. Passing checks are counted; failing checks keep their text."""

    def __init__(self) -> None:
        self.passed: dict[str, int] = {k: 0 for k in CLAUSES}
        self.evidence: dict[str, list[str]] = {k: [] for k in CLAUSES}
        self.bad: dict[str, list[str]] = {k: [] for k in CLAUSES}
        # every check with its label, for the qualification protocol's attribution
        self.labels: dict[str, list[tuple[bool, str]]] = {k: [] for k in CLAUSES}

    def check(self, clause: str, condition, failure: str) -> bool:
        self.labels[clause].append((bool(condition), failure))
        if condition:
            self.passed[clause] += 1
        else:
            self.bad[clause].append(failure)
        return bool(condition)

    def note(self, clause: str, evidence: str) -> None:
        self.evidence[clause].append(evidence)

    def report(self) -> int:
        failed = 0
        for clause in CLAUSES:
            if self.bad[clause] or not self.passed[clause] or not self.evidence[clause]:
                failed += 1
                for line in self.bad[clause]:
                    print(f"4_2: {clause} FAIL {line}", file=sys.stderr)
                shown = self.bad[clause][:6] or ["no evidence collected"]
                more = len(self.bad[clause]) - len(shown)
                tail = f" (+{more} more)" if more > 0 else ""
                print(f"CLAUSE {clause} FAIL {' | '.join(shown)}{tail}")
            else:
                checks = f"{self.passed[clause]} checks"
                print(f"CLAUSE {clause} PASS {checks}: {' | '.join(self.evidence[clause])}")
        return 1 if failed else 0


R = Results()
OBS = protocol.Observer("4.2", protocol.pairs(R.labels))


def target_dir() -> Path:
    env = os.environ.get("CARGO_TARGET_DIR")
    return Path(env) if env else REPO / "target"


def run(cmd, timeout=900, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(x) for x in cmd], capture_output=True, text=True, timeout=timeout, **kw
    )


def ram_dir():
    """A RAM-backed parent for Instance homes when the host has one (the Build itself stays on
    disk, where the sandbox can bind it). Every ledger append and state
    write is fsynced (kernel.md section 8); on a shared, I/O-saturated disk that made single calls
    take seconds and large reference turns hit the 30000 ms wall limit. Durability itself is case
    CWR-010's subject, not this driver's. None means the platform temp dir."""
    shm = Path("/dev/shm")
    return str(shm) if shm.is_dir() and os.access(shm, os.W_OK) else None


def build_cli() -> Path:
    env = dict(os.environ)
    env["CARGO_TARGET_DIR"] = str(target_dir())
    for attempt in range(4):
        p = run(["cargo", "build", "-p", "cwr-cli"], timeout=3600, cwd=REPO, env=env)
        if p.returncode == 0:
            return target_dir() / "debug" / "cwr"
        print(f"4_2: cargo build failed (attempt {attempt + 1}); retrying", file=sys.stderr)
        print(p.stderr[-3000:], file=sys.stderr)
        time.sleep(120)
    raise SystemExit("4_2: cargo build -p cwr-cli failed")


def compile_refworld(cwr: Path, work: Path) -> Path:
    src = work / "refworld"
    shutil.copytree(
        REFWORLD, src, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "README.md")
    )
    p = run([cwr, "build", src, "--out", work / "build"], cwd=work)
    match = re.search(r"built (sha256:[0-9a-f]{64}) at (\S+)", p.stdout + p.stderr)
    if p.returncode != 0 or not match:
        print(p.stdout[-3000:] + p.stderr[-3000:], file=sys.stderr)
        raise SystemExit("4_2: cwr build of examples/refworld failed")
    path = Path(match.group(2))
    return path if path.is_absolute() else work / path


class Stubs:
    """The search and publish stub servers of benchmark/stubs, started with one seed."""

    def __init__(self, seed: int, cwd: Path) -> None:
        self.procs: list[subprocess.Popen] = []
        self.urls: dict[str, str] = {}
        for name in ("search", "publish"):
            server = STUBS / f"{name}_server.py"
            proc = subprocess.Popen(
                [sys.executable, "-I", str(server), "--port", "0", "--seed", str(seed)],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                cwd=cwd,
            )
            self.procs.append(proc)
            ready = json.loads(proc.stdout.readline() or "{}")
            if ready.get("event") != "ready":
                self.stop()
                raise SystemExit(f"4_2: stub {name} did not start: {ready}")
            self.urls[name] = f"http://127.0.0.1:{ready['port']}"

    def stop(self) -> None:
        for proc in self.procs:
            proc.terminate()
        for proc in self.procs:
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()


class Instance:
    """One Instance of the refworld Build, driven through the real `cwr` binary."""

    def __init__(self, cwr: Path, build: Path, home: Path, initial: dict, seed: int = 1) -> None:
        self.cwr, self.home = cwr, home
        home.mkdir(parents=True, exist_ok=True)
        init = home / "initial.json"
        init.write_text(json.dumps(initial, sort_keys=True), encoding="utf-8")
        create = ["instance", "create", "--home", home, "--build", build, "--seed", str(seed)]
        p = run([cwr, *create, "--initial", init])
        if p.returncode != 0:
            raise RuntimeError(f"instance create failed: {p.stderr.strip()[-400:]}")
        self.id = p.stdout.strip()
        self.n = 0

    def turn(self, body: str, mode: str, cassette: Path, stubs: dict, fenced: bool) -> dict:
        self.n += 1
        script = self.home / f"turn-{self.n}.txt"
        script.write_text(f"```python\n{body}```\n" if fenced else body, encoding="utf-8")
        cmd = [self.cwr, "instance", "turn", self.id, "--home", self.home, "--script", script]
        cmd += ["--gateway", mode, "--cassette", cassette]
        if mode == "record":
            cmd += ["--cassette-label", cassette.stem]
        for name, url in sorted(stubs.items()):
            cmd += ["--stub", f"{name}={url}"]
        p = run(cmd, timeout=600)
        if p.returncode != 0:
            raise RuntimeError(f"turn {self.n} exited {p.returncode}: {p.stderr.strip()[-400:]}")
        return json.loads(p.stdout.strip().splitlines()[-1])

    def kernel_state(self) -> dict:
        p = run([self.cwr, "instance", "state", self.id, "--home", self.home])
        if p.returncode != 0:
            raise RuntimeError(f"instance state failed: {p.stderr.strip()[-400:]}")
        return json.loads(p.stdout)

    @property
    def dir(self) -> Path:
        return self.home / "instances" / self.id

    def ledger(self) -> list:
        return c.load_ledger(self.dir / "ledger.jsonl")

    def view(self) -> tuple[dict, list]:
        ledger = self.ledger()
        state = c.state_view_from_instance(
            self.kernel_state(), ledger, c.read_key_store(self.dir / "gateway")
        )
        return state, ledger


def load_solution(task: str):
    path = SOLUTIONS / f"{task}.py"
    spec = importlib.util.spec_from_file_location(f"solution_{task.replace('-', '_')}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def drive(inst: Instance, task: str, mode: str, cassette: Path, stubs: dict, fenced: bool):
    """Runs the reference solution's turns() generator to completion; returns the envelopes."""
    gen = load_solution(task).turns()
    envelopes = []
    body = next(gen)
    while True:
        envelopes.append(inst.turn(body, mode, cassette, stubs, fenced))
        if len(envelopes) > TURN_LIMIT:
            raise RuntimeError(f"turn limit {TURN_LIMIT} exceeded")
        try:
            body = gen.send(envelopes[-1])
        except StopIteration:
            return envelopes


def host_free_ledger(ledger: list) -> list:
    return [{k: v for k, v in r.items() if k not in LEDGER_HOST_FIELDS} for r in ledger]


def host_free_envelopes(envelopes: list) -> list:
    out = json.loads(json.dumps(envelopes))
    for env in out:
        for key in ENVELOPE_HOST_FIELDS:
            env.get("resources", {}).pop(key, None)
    return out


def probe_fenced(cwr: Path, build: Path, work: Path) -> bool:
    """True when `cwr instance turn` accepts a fenced python block (model-surface.md section 4)."""
    inst = Instance(cwr, build, work / "probe", {})
    env = inst.turn("return 7\n", "record", work / "probe.jsonl", {}, True)
    return env.get("status") == "ok" and env.get("returned") == 7


def reuse_shape(positions: list, total: int) -> tuple[bool, dict]:
    """reference-world.md section 6: first quarter, nothing in the middle half, last quarter,
    at least ten calls between the early and the late use."""
    early = [p for p in positions if p < total / 4]
    middle = [p for p in positions if total / 4 <= p < 3 * total / 4]
    late = [p for p in positions if p >= 3 * total / 4]
    ok = bool(early) and not middle and bool(late) and late[0] - early[-1] >= 10
    return ok, {
        "first": early[0] if early else None,
        "last_early": early[-1] if early else None,
        "first_late": late[0] if late else None,
    }


def record_task(task_dir: Path, cwr: Path, build: Path, homes: Path, stubs: dict, fenced: bool):
    """Record mode against the live stubs; this run only produces the task's cassette. Returns an
    error string or None; never raises."""
    task = task_dir.name
    try:
        root = homes / task
        root.mkdir(parents=True, exist_ok=True)
        rec = Instance(cwr, build, root / "record", c.kernel_initial_state(task_dir))
        drive(rec, task, "record", root / "cassette.jsonl", stubs, fenced)
        return None
    except Exception as error:  # report every broken task, not only the first
        return f"record: {type(error).__name__}: {error}"


def replay_task(task_dir: Path, cwr: Path, build: Path, homes: Path, fenced: bool, frozen: bool):
    """Two replay-mode runs with no stub bound, plus the idle Instance, plus one run against the
    frozen suite cassette of task 4.1 when that set exists. Never raises."""
    task = task_dir.name
    out: dict = {"task": task, "errors": []}
    try:
        initial = c.kernel_initial_state(task_dir)
        predicate = c.load_predicate(task_dir)
        root = homes / task
        cassette = root / "cassette.jsonl"
        runs = []
        for label in ("replay-a", "replay-b"):
            inst = Instance(cwr, build, root / label, initial)
            envelopes = drive(inst, task, "replay", cassette, {}, fenced)
            state, ledger = inst.view()
            runs.append(
                {
                    "envelopes": envelopes,
                    "state": state,
                    "ledger": ledger,
                    "success": c.evaluate_twice(predicate, state, ledger),
                    "calls": c.calls(ledger),
                    "turn_status": sorted({e["status"] for e in envelopes}),
                }
            )
        frozen_cassette = FROZEN / task / "seed-1.jsonl"
        if frozen and frozen_cassette.is_file():
            inst = Instance(cwr, build, root / "replay-frozen", initial)
            drive(inst, task, "replay", frozen_cassette, {}, fenced)
            out["frozen_success"] = c.evaluate_twice(predicate, *inst.view())
        idle = Instance(cwr, build, root / "idle", initial)
        idle_state, idle_ledger = idle.view()
        out["idle_success"] = c.evaluate_twice(predicate, idle_state, idle_ledger)
        a, b = runs
        out["success"] = [a["success"], b["success"]]
        out["count"] = len(a["calls"])
        out["counts"] = [len(a["calls"]), len(b["calls"])]
        out["turns"] = len(a["envelopes"])
        out["max_turn_calls"] = max(e["resources"]["primitive_calls"] for e in a["envelopes"])
        out["failing_calls"] = sum(1 for r in a["calls"] if r["outcome"]["status"] != "ok")
        out["turn_status"] = a["turn_status"]
        out["replay_equal"] = host_free_ledger(a["ledger"]) == host_free_ledger(
            b["ledger"]
        ) and host_free_envelopes(a["envelopes"]) == host_free_envelopes(b["envelopes"])
        out["state_equal"] = json.dumps(a["state"], sort_keys=True) == json.dumps(
            b["state"], sort_keys=True
        )
        out["adapter_calls"] = sum(1 for r in a["calls"] if r.get("kind") == "adapter")
        out["primitives"] = [r["primitive"] for r in a["calls"]]
    except Exception as error:  # report every broken task, not only the first
        out["errors"].append(f"replay: {type(error).__name__}: {error}")
    return out


def audit_predicate(task_dir: Path) -> list:
    """Static purity audit beyond common.check_predicate_source: no kernel import, no model text,
    inputs are exactly (state, ledger)."""
    source = (task_dir / "success.py").read_text(encoding="utf-8")
    problems = list(c.check_predicate_source(source))
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] + [getattr(node, "module", None) or ""]
            for name in names:
                if name.split(".")[0] in KERNEL_MODULE_NAMES:
                    problems.append(f"imports {name}")
        if isinstance(node, ast.Name) and node.id in MODEL_TEXT_NAMES:
            problems.append(f"refers to model text {node.id}")
        if isinstance(node, ast.Constant) and node.value in ("returned", "stdout", "stderr"):
            problems.append(f"reads envelope/model field {node.value!r}")
    return problems


def audit_common() -> list:
    """common.py imports only the standard library and nothing from the kernel or SDK."""
    tree = ast.parse(COMMON.read_text(encoding="utf-8"))
    problems = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        for name in names:
            top = name.split(".")[0]
            if top in KERNEL_MODULE_NAMES or top not in sys.stdlib_module_names:
                problems.append(f"common.py imports {name}")
    return problems


def check_static(suite: dict, tasks: list) -> dict:
    """Clause 11.3 layout and purity, and the suite.kvx digests. Returns the per-task manifest."""
    manifest = {}
    for task_dir in tasks:
        name = task_dir.name
        missing = [f for f in REQUIRED_FILES if not (task_dir / f).is_file()]
        R.check("11.3", not missing, f"{name}: missing {missing}")
        problems = audit_predicate(task_dir)
        R.check("11.3", not problems, f"{name}: predicate audit {problems}")
        binname = (task_dir / "bin").read_text(encoding="utf-8").strip()
        rules = [r for r in (task_dir / "rules").read_text(encoding="utf-8").splitlines() if r]
        bad_rules = []
        for rule in rules:
            try:
                c.parse_rule(rule)
            except ValueError as error:
                bad_rules.append(str(error))
        R.check("11.3", bool(rules) and not bad_rules, f"{name}: rules {bad_rules or 'empty'}")
        entry = suite.get(f"task.{name}") or {}
        R.check("11.2", entry.get("bin") == binname, f"{name}: suite.kvx bin != bin file")
        digest = c.tree_digest(task_dir, TASKS)
        R.check("11.2", entry.get("digest") == digest, f"{name}: suite.kvx digest stale")
        if entry.get("rule_reuse"):
            R.check(
                "11.2",
                rules and rules[0] == entry.get("reused_rule"),
                f"{name}: rules file does not name the reused rule first",
            )
        manifest[name] = {"bin": binname, "rules": rules, "suite": entry}
    meta = suite.get("suite", {})
    checks = {
        "suite_digest": c.suite_digest(TASKS),
        "solutions_digest": c.tree_digest(SOLUTIONS),
        "predicates_common_digest": c.tree_digest_file(COMMON),
    }
    for key, value in checks.items():
        R.check("11.2", meta.get(key) == value, f"suite.kvx {key} {meta.get(key)} != {value}")
    R.check("11.2", meta.get("count_source") == "kernel-ledger", "suite counts not kernel-ledger")
    common_problems = audit_common()
    R.check("11.3", not common_problems, f"common.py audit {common_problems}")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-counts", help="write measured counts as JSON to this file")
    parser.add_argument("--only", help="comma-separated task id prefixes (diagnostics)")
    parser.add_argument("--jobs", type=int, default=min(8, os.cpu_count() or 2))
    parser.add_argument("--homes", help="parent of the Instance homes (default: RAM-backed)")
    args = parser.parse_args()

    started = time.monotonic()
    suite = c._kvx_sections(SUITE.read_text(encoding="utf-8"))
    tasks = c.task_dirs(TASKS)
    if args.only:
        prefixes = tuple(args.only.split(","))
        tasks = [t for t in tasks if t.name.startswith(prefixes)]
    R.check("11.2", len(c.task_dirs(TASKS)) == 60, f"{len(c.task_dirs(TASKS))} task directories")
    manifest = check_static(suite, tasks)

    frozen = suite.get("suite", {}).get("cassette_digest") is not None and FROZEN.is_dir()
    cwr = build_cli()
    work = Path(tempfile.mkdtemp(prefix="cwr-4_2-"))
    homes = Path(tempfile.mkdtemp(prefix="cwr-4_2-homes-", dir=args.homes or ram_dir()))
    stubs = None
    try:
        build = compile_refworld(cwr, work)
        fenced = probe_fenced(cwr, build, homes)
        stubs = Stubs(1, work)
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
            urls = stubs.urls
            recorded = {
                t.name: error
                for t, error in zip(
                    tasks,
                    pool.map(lambda t: record_task(t, cwr, build, homes, urls, fenced), tasks),
                    strict=True,
                )
            }
        stubs.stop()
        stubs = None
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
            replayed = pool.map(lambda t: replay_task(t, cwr, build, homes, fenced, frozen), tasks)
            results = {r["task"]: r for r in replayed}
        for name, error in recorded.items():
            if error:
                results[name]["errors"].insert(0, error)
    finally:
        if stubs is not None:
            stubs.stop()

    measured_bins: dict[str, list] = {b: [] for b in BINS}
    reuse_ok = []
    counts_out = {}
    for task_dir in tasks:
        name = task_dir.name
        res, info = results[name], manifest[name]
        if res["errors"]:
            R.check("11.3", False, f"{name}: {res['errors'][0][:300]}")
            continue
        count = res["count"]
        R.check(
            "11.3",
            res["success"] == [True, True],
            f"{name}: predicate {res['success']} on two replay runs",
        )
        R.check("11.3", res["idle_success"] is False, f"{name}: predicate true when idle")
        if "frozen_success" in res:
            R.check("11.3", res["frozen_success"], f"{name}: predicate false on frozen cassette")
        R.check("11.3", res["replay_equal"] and res["state_equal"], f"{name}: replay differs")
        R.check(
            "11.2",
            res["failing_calls"] == 0 and res["turn_status"] == ["ok"],
            f"{name}: reference has {res['failing_calls']} failing calls, turns {res['turn_status']}",
        )
        R.check("11.2", res["counts"][0] == res["counts"][1], f"{name}: counts {res['counts']}")
        in_bin = [b for b, (lo, hi) in BINS.items() if lo <= count <= hi]
        if in_bin:
            measured_bins[in_bin[0]].append(name)
        R.check("11.2", in_bin == [info["bin"]], f"{name}: {count} primitives, bin {info['bin']}")
        R.check(
            "11.2",
            info["suite"].get("primitive_count") == count,
            f"{name}: suite.kvx primitive_count {info['suite'].get('primitive_count')} != {count}",
        )
        positions = None
        if info["suite"].get("rule_reuse"):
            primitive = c.parse_rule(info["suite"]["reused_rule"])["primitive"]
            idx = [i for i, p in enumerate(res["primitives"]) if p == primitive]
            ok, positions = reuse_shape(idx, count)
            R.check(
                "11.2",
                ok and info["bin"] == "long",
                f"{name}: reuse of {primitive} at {idx} of {count}",
            )
            if ok and info["bin"] == "long":
                reuse_ok.append(name)
            R.check(
                "11.2",
                positions == info["suite"].get("reuse_positions"),
                f"{name}: suite.kvx reuse_positions {info['suite'].get('reuse_positions')} "
                f"!= {positions}",
            )
        counts_out[name] = {"count": count, "turns": res["turns"], "reuse": positions}

    sizes = {b: len(v) for b, v in measured_bins.items()}
    R.check("11.2", sizes == {b: PER_BIN for b in BINS}, f"measured bins {sizes}")
    R.check("11.2", len(reuse_ok) >= MIN_REUSE, f"{len(reuse_ok)} long rule-reuse tasks")
    clean = [r for r in results.values() if not r["errors"]]
    ranges = {b: [results[n]["count"] for n in v] for b, v in measured_bins.items() if v}
    reused = sorted({manifest[n]["suite"].get("reused_rule") for n in reuse_ok})
    R.note(
        "11.2",
        f"{len(clean)}/{len(tasks)} reference solutions replayed on the real kernel "
        f"({'fenced' if fenced else 'raw'} turn scripts) with the stub servers stopped, "
        f"{sum(r['count'] for r in clean)} ledger calls per replay, "
        f"{sum(1 for r in clean if r['adapter_calls'])} tasks with adapter calls served from "
        f"cassettes, every turn ok with no failing call",
    )
    R.note(
        "11.2",
        f"bins measured from ledgers {sizes}: "
        + ", ".join(f"{b} {min(v)}..{max(v)}" for b, v in ranges.items())
        + f"; equal to suite.kvx primitive_count and bin files; largest turn "
        f"{max((r['max_turn_calls'] for r in clean), default=0)} calls",
    )
    R.note(
        "11.2",
        f"{len(reuse_ok)} long rule-reuse tasks with the section 6 ledger shape, reusing "
        f"{len(reused)} distinct rules; suite.kvx suite, task, solutions and common digests match",
    )
    frozen_runs = [r for r in clean if "frozen_success" in r]
    R.note(
        "11.2",
        f"{len(frozen_runs)} tasks also replayed against the frozen suite cassettes of "
        "benchmark/cassettes/suite"
        if frozen
        else "suite.kvx cassette_digest is null (task 4.1 has not frozen the suite cassettes): "
        "replays use cassettes recorded by this driver from the stubs with seed 1",
    )
    R.note(
        "11.3",
        f"{len(tasks)} task directories with task.md, initial.kvx, success.py, bin and rules; "
        "every predicate passes the AST audit (imports only common, no kernel module, no model "
        "text, no I/O), loads through common.load_predicate, is true on two replay runs, false on "
        "the idle Instance and deterministic under evaluate_twice; common.py imports only the "
        "standard library",
    )
    if args.write_counts:
        Path(args.write_counts).write_text(
            json.dumps(counts_out, sort_keys=True, indent=1) + "\n", encoding="utf-8"
        )
    shutil.rmtree(work, ignore_errors=True)
    shutil.rmtree(homes, ignore_errors=True)
    print(f"4_2: {time.monotonic() - started:.0f}s", file=sys.stderr)
    OBS.claim("CWR-015.a1", match="on two replay runs|predicate false on frozen")
    OBS.claim("CWR-015.a2", match="primitives, bin|primitive_count|measured bins|: counts \\[")
    OBS.claim("CWR-015.a3", match="replay differs|predicate true when idle|on two replay runs")
    OBS.claim("CWR-015.a4", match="predicate audit|common\\.py audit")
    OBS.emit()
    return R.report()


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""H1 stability and H2 direct-action campaigns (task 5.2; spec/cwr/experiments.md sections 3.1, 3.2).

Every arm is a CWR Build of refworld: the control Build, a per-task drift Build (--x-drift) or the
one-primitive Build (--x-one-primitive). There is no external baseline.

Subcommands (run from anywhere; paths are resolved from this file):

  plan       derive the per-task drift specifications deterministically from each task's rules file
             and print them; writes nothing
  register   compile the control, one-primitive and 120 per-task drift Builds with the pinned cwr
             binary and write benchmark/campaigns/h1.kvx and h2.kvx; refuses once any result exists
  run        execute cells (hypothesis, arm, task, seed) in replay mode with parallel workers through
             benchmark/harness/run.py; seed-major order, resumable, stops submitting at --deadline
  analyse    run benchmark/analyse.py separately over H1 and H2 into results/h<N>/analysis.json
  report     render research/h1-stability.md and research/h2-direct-action.md from those analyses
  status     count present, missing and errored cells per arm

Drift schedule (experiments.md 3.1, compiler.md section 10). len is the reference solution's turn count
recorded in benchmark/suite.kvx before any run. drift-1 injects one rule at max(1, floor(len/2)); drift-3
injects three rules at max(1, floor(len/4)), max(1, floor(len/2)), max(1, floor(3*len/4)). The drift-1
injection is the drift-3 injection at floor(len/2), so drift-3 is drift-1 plus two more changes.
Selection is seeded by the task id only: the task's rules are ordered by sha256('cwr-h1-drift|<task>|
<rule>'), each rule's primitive contributes its templates level by level (template A of every rule,
then B, then C) and the first three distinct (primitive, template) pairs are taken. Every template is
a precondition_added over parameters and read_only primitives, so the injected rule is checkable by the
model although the manual does not document it.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from benchmark.harness.ledgerio import (  # noqa: E402
    dump_kvx,
    parse_kvx,
    read_kvx,
    sha256_file,
)

CAMPAIGNS = REPO / "benchmark" / "campaigns"
RESULTS = REPO / "benchmark" / "results"
RESEARCH = REPO / "research"
SUITE = REPO / "benchmark" / "suite.kvx"
PREREG = REPO / "benchmark" / "preregistration.kvx"
ARMS = REPO / "benchmark" / "harness" / "arms.kvx"
SOURCE = REPO / "examples" / "refworld"
# The campaign compiles into its own Build root so that the control Build takes the first-Build path
# (previous null) and its hash depends on the World Source and compiler only, never on whatever other
# Builds of refworld happen to exist under build/. Experimental Builds land in xbuild/ beside it.
BUILD_ROOT = Path(
    os.environ.get("CWR_CAMPAIGN_BUILD_ROOT") or REPO / ".cwr" / "campaign-1" / "build"
)
XBUILD_ROOT = BUILD_ROOT.parent / "xbuild"
CAMPAIGN_FILE = {"H1": CAMPAIGNS / "h1.kvx", "H2": CAMPAIGNS / "h2.kvx"}
ARM_KEYS = {"H1": ("frozen", "drift-1", "drift-3"), "H2": ("script", "one-primitive")}
CONTROL_ARM = {"H1": "frozen", "H2": "script"}
REPORT = {"H1": RESEARCH / "h1-stability.md", "H2": RESEARCH / "h2-direct-action.md"}
SEEDS = list(range(1, 11))
RETRY_EXITS = ("model_error", "kernel_error")

# Drift templates per primitive or adapter: (template id, expression, failure code). Each expression
# references only parameters and read_only primitives of refworld (world-source.md section 5), so a drift
# Build passes the same lint as a contract. Templates tighten the rule; none relaxes it.
TEMPLATES: dict[str, list[tuple[str, str, str]]] = {
    "fs.write": [
        ("A", "not fs.exists(path)", "CONFLICT"),
        ("B", "len(content) <= 64", "LIMIT"),
        ("C", "jobs.active() == 0", "CONFLICT"),
    ],
    "fs.read": [
        ("A", "fs.size(path) <= 32", "LIMIT"),
        ("B", "not fs.locked(path)", "LOCKED"),
        ("C", "jobs.active() == 0", "CONFLICT"),
    ],
    "fs.delete": [
        ("A", "fs.size(path) <= 32", "LIMIT"),
        ("B", "fs.size(path) >= 1", "CONFLICT"),
        ("C", "jobs.active() == 0", "CONFLICT"),
    ],
    "fs.lock": [
        ("A", "fs.size(path) <= 32", "LIMIT"),
        ("B", "fs.size(path) >= 1", "CONFLICT"),
        ("C", "jobs.active() == 0", "CONFLICT"),
    ],
    "fs.unlock": [
        ("A", "not fs.exists(path) or fs.size(path) <= 32", "LIMIT"),
        ("B", "fs.exists(path)", "NOT_FOUND"),
        ("C", "jobs.active() == 0", "CONFLICT"),
    ],
    "fs.move": [
        ("A", "fs.size(src) <= 32", "LIMIT"),
        ("B", "fs.size(src) >= 1", "CONFLICT"),
        ("C", "jobs.active() == 0", "CONFLICT"),
    ],
    "fs.size": [
        ("A", "not fs.locked(path)", "LOCKED"),
        ("B", "jobs.active() == 0", "CONFLICT"),
        ("C", "jobs.active() <= 1", "LIMIT"),
    ],
    "jobs.submit": [
        ("A", "n <= 10", "LIMIT"),
        ("B", "jobs.active() < 2", "LIMIT"),
        ("C", "len(kind) <= 6", "INVALID"),
    ],
    "jobs.status": [
        ("A", "jobs.active() <= 1", "LIMIT"),
        ("B", "len(id) <= 4", "INVALID"),
        ("C", "jobs.active() == 0", "CONFLICT"),
    ],
    "jobs.result": [
        ("A", "jobs.active() == 0", "CONFLICT"),
        ("B", "len(id) <= 4", "INVALID"),
        ("C", "jobs.active() <= 1", "LIMIT"),
    ],
    "jobs.cancel": [
        ("A", 'jobs.status(id) == "queued"', "CONFLICT"),
        ("B", "jobs.active() <= 1", "LIMIT"),
        ("C", "len(id) <= 4", "INVALID"),
    ],
    "kv.set": [
        ("A", "len(value) <= 32", "LIMIT"),
        ("B", "not kv.has(ns, key)", "CONFLICT"),
        ("C", "len(key) <= 8", "INVALID"),
    ],
    "kv.get": [
        ("A", "len(key) <= 8", "INVALID"),
        ("B", "len(kv.keys(ns)) <= 4", "LIMIT"),
        ("C", "jobs.active() == 0", "CONFLICT"),
    ],
    "kv.del": [
        ("A", "len(kv.keys(ns)) >= 2", "CONFLICT"),
        ("B", "len(key) <= 8", "INVALID"),
        ("C", "jobs.active() == 0", "CONFLICT"),
    ],
    "publish.post": [
        ("A", "len(title) <= 16", "INVALID"),
        ("B", "len(body) <= 256", "LIMIT"),
        ("C", "len(key) <= 8", "INVALID"),
    ],
    "search.query": [
        ("A", "limit <= 3", "LIMIT"),
        ("B", "len(q) <= 8", "INVALID"),
        ("C", "jobs.active() == 0", "CONFLICT"),
    ],
}
DRIFT_FRACTIONS = {"drift-1": ((1, 2),), "drift-3": ((1, 4), (1, 2), (3, 4))}


class CampaignError(RuntimeError):
    """A campaign step cannot proceed; the message names the input that blocks it."""


# ---------------------------------------------------------------------------------------------
# Inputs


def suite_tasks() -> dict[str, dict[str, Any]]:
    """Task id to its [task.<id>] section of benchmark/suite.kvx, in suite order."""
    suite = read_kvx(SUITE)
    order: list[str] = []
    for bin_name in ("short", "medium", "long"):
        order.extend(suite[f"bin.{bin_name}"]["tasks"])
    return {task: suite[f"task.{task}"] for task in order}


def rule_primitive(rule: str) -> str:
    """The qualified primitive a rule identifier names, e.g. fs.write for fs.write[1]:LOCKED."""
    return rule.split("[", 1)[0].split(":", 1)[0]


def cwr_binary() -> Path:
    """The cwr CLI: $CWR_BIN, else <CARGO_TARGET_DIR or workspace target>/release|debug/cwr."""
    if os.environ.get("CWR_BIN"):
        return Path(os.environ["CWR_BIN"])
    target = Path(os.environ.get("CARGO_TARGET_DIR") or REPO / "target")
    for profile in ("release", "debug"):
        candidate = target / profile / "cwr"
        if candidate.exists():
            return candidate
    return target / "debug" / "cwr"


# ---------------------------------------------------------------------------------------------
# Drift plan


def drift_turn(length: int, fraction: tuple[int, int]) -> int:
    """max(1, floor(len * p / q)): injections happen before a turn's script, and turns start at 1."""
    return max(1, (length * fraction[0]) // fraction[1])


def drift_picks(task: str, rules: list[str]) -> list[dict[str, str]]:
    """The three seeded (rule, template) picks of a task, primary first."""
    eligible = [r for r in rules if rule_primitive(r) in TEMPLATES]
    if not eligible:
        raise CampaignError(f"{task}: no rule in its rules file names a drift-capable primitive")
    ordered = sorted(
        eligible, key=lambda r: hashlib.sha256(f"cwr-h1-drift|{task}|{r}".encode()).hexdigest()
    )
    picks: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for level in range(3):
        for rule in ordered:
            primitive = rule_primitive(rule)
            template, expr, failure = TEMPLATES[primitive][level]
            if (primitive, template) in seen:
                continue
            seen.add((primitive, template))
            picks.append(
                {
                    "rule": rule,
                    "primitive": primitive,
                    "template": template,
                    "expr": expr,
                    "failure": failure,
                }
            )
    return picks[:3]


def drift_plan(task: str, rules: list[str], length: int) -> dict[str, list[dict[str, Any]]]:
    """Per drift arm, the ordered injections {n, turn, primitive, change, expr, failure, doc, ...}."""
    picks = drift_picks(task, rules)
    if len(picks) < 3:
        raise CampaignError(f"{task}: fewer than three distinct drift injections available")
    chosen = {"drift-1": [picks[0]], "drift-3": [picks[1], picks[0], picks[2]]}
    plan: dict[str, list[dict[str, Any]]] = {}
    for arm, fractions in DRIFT_FRACTIONS.items():
        entries = []
        for n, (pick, fraction) in enumerate(zip(chosen[arm], fractions, strict=True), start=1):
            turn = drift_turn(length, fraction)
            entries.append(
                {
                    "n": n,
                    "turn": turn,
                    "primitive": pick["primitive"],
                    "change": "precondition_added",
                    "expr": pick["expr"],
                    "failure": pick["failure"],
                    "rule": pick["rule"],
                    "template": f"{pick['primitive']}/{pick['template']}",
                    "doc": (
                        f"H1 {arm} for {task}: from turn {turn}, {pick['primitive']} also requires "
                        f"{pick['expr']} and raises {pick['failure']} otherwise; drawn from rule "
                        f"{pick['rule']}. The manual is unchanged."
                    ),
                }
            )
        plan[arm] = entries
    return plan


def drift_spec_label(entries: list[dict[str, Any]]) -> str:
    """The per-spec reporting key: the templates injected, in schedule order."""
    return " + ".join(e["template"] for e in entries)


def drift_file_text(task: str, arm: str, entries: list[dict[str, Any]]) -> str:
    """The --x-drift file (compiler.md section 10 grammar) for one task and drift arm."""
    sections: dict[str, dict[str, Any]] = {
        "meta": {"format": "cwr.drift", "schema_version": 1, "example": False}
    }
    for e in entries:
        sections[f"drift.{e['n']}"] = {
            "turn": e["turn"],
            "primitive": e["primitive"],
            "change": e["change"],
            "expr": e["expr"],
            "failure": e["failure"],
            "doc": e["doc"],
        }
    header = (
        f"H1 {arm} drift schedule for {task}, generated from benchmark/campaigns/h1.kvx by\n"
        "benchmark/campaigns/h1h2.py; the manual of the Build is unchanged."
    )
    return dump_kvx(sections, header)


# ---------------------------------------------------------------------------------------------
# Builds


def compile_build(binary: Path, flag: list[str], *, previous: str | None = None) -> dict[str, Any]:
    """Run `cwr build examples/refworld --out BUILD_ROOT` with one optional flag.

    Experimental Builds land under XBUILD_ROOT (compiler.md section 10). Returns {hash, dir,
    already_built} from the compiler's JSON report.
    """
    cmd = [str(binary), "build", str(SOURCE), "--out", str(BUILD_ROOT), "--json", *flag]
    if previous:
        cmd += ["--previous", previous]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, cwd=REPO)
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise CampaignError(
            f"cwr build {' '.join(flag)} printed no JSON report (exit {proc.returncode}): "
            f"{proc.stderr.strip()[:400]}"
        ) from None
    if proc.returncode != 0 or not report.get("build"):
        raise CampaignError(
            f"cwr build {' '.join(flag)} failed at {report.get('stopped_at')} "
            f"(exit {proc.returncode}): {json.dumps(report.get('stages'))[:800]}"
        )
    return report["build"]


def build_dir(build: str) -> Path | None:
    """The directory of a registered Build hash under build/ or xbuild/, if present."""
    digest = build.removeprefix("sha256:")
    for root in (BUILD_ROOT, XBUILD_ROOT, REPO / "build", REPO / "xbuild"):
        if (root / digest).is_dir():
            return root / digest
    return None


def manifest(build: str) -> dict[str, Any]:
    """The [build] section of a Build's manifest.kvx."""
    path = build_dir(build)
    if path is None:
        raise CampaignError(f"Build {build} is not under build/ or xbuild/")
    return read_kvx(path / "manifest.kvx")["build"]


# ---------------------------------------------------------------------------------------------
# Registration


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def any_results(hypothesis: str) -> bool:
    root = RESULTS / hypothesis.lower()
    return root.is_dir() and any(root.rglob("attempt.kvx"))


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    parse_kvx(text, str(path))
    path.write_text(text, encoding="utf-8")


def register(args: argparse.Namespace) -> int:
    for hypothesis in ("H1", "H2"):
        if any_results(hypothesis) and not args.force:
            raise CampaignError(
                f"{hypothesis} results exist; a registration cannot change after execution"
            )
    binary = Path(args.cwr_bin) if args.cwr_bin else cwr_binary()
    if not binary.exists():
        raise CampaignError(f"cwr binary {binary} does not exist; build cwr-cli first")
    prereg = read_kvx(PREREG)
    arms = read_kvx(ARMS)
    tasks = suite_tasks()
    registered_control = arms["control"].get("build")
    if registered_control:
        # Another campaign registered the shared control Build first; H1 and H2 adopt it, because
        # arms.kvx carries one control hash for every control-bound arm.
        if build_dir(registered_control) is None:
            raise CampaignError(f"arms.kvx control Build {registered_control} is not on disk")
        control = {"hash": registered_control}
    else:
        control = compile_build(binary, [])
    one = compile_build(binary, ["--x-one-primitive"])
    common = {
        "campaign": prereg["meta"]["campaign"],
        "preregistration": "benchmark/preregistration.kvx",
        "registered_at": _now(),
        "world": "refworld",
        "task_set": "suite:all",
        "tasks": len(tasks),
        "seeds": SEEDS,
        "gateway_mode": "replay",
        "tail": 20,
        "model": prereg["campaign"].get("model"),
        "sampling": prereg["campaign"].get("sampling"),
        "suite_digest": read_kvx(SUITE)["suite"]["suite_digest"],
        "cassette_digest": prereg["campaign"].get("cassette_digest"),
        "control_build": control["hash"],
        "build_roots": [str(BUILD_ROOT.relative_to(REPO)), str(XBUILD_ROOT.relative_to(REPO))],
        "cwr_binary_sha256": sha256_file(binary),
        "metrics_py": prereg["digests"]["metrics_py"],
        "arms_kvx_at_registration": prereg["digests"]["arms_kvx"],
        "no_external_baseline": True,
    }
    h2 = {
        "meta": {
            "format": "cwr.campaign",
            "schema_version": 1,
            "hypothesis": "H2",
            "status": "registered",
            "generated_by": "benchmark/campaigns/h1h2.py register",
        },
        "registration": {
            **common,
            "arms": list(ARM_KEYS["H2"]),
            "control": "script",
            "builds": {"script": control["hash"], "one-primitive": one["hash"]},
            "primary": prereg["hypothesis.H2"]["primary"],
            "secondary": prereg["hypothesis.H2"]["secondary"],
            "directions": prereg["hypothesis.H2"]["directions"],
            "arms_kvx_build_binding": {
                "script": arms["arm.h2.script"]["build_binding"],
                "one-primitive": arms["arm.h2.one-primitive"]["build_binding"],
            },
        },
    }
    h1: dict[str, dict[str, Any]] = {
        "meta": {
            "format": "cwr.campaign",
            "schema_version": 1,
            "hypothesis": "H1",
            "status": "registered",
            "generated_by": "benchmark/campaigns/h1h2.py register",
        },
        "registration": {
            **common,
            "arms": list(ARM_KEYS["H1"]),
            "control": "frozen",
            "builds": {"frozen": control["hash"], "drift-1": "per_task", "drift-3": "per_task"},
            "primary": prereg["hypothesis.H1"]["primary"],
            "secondary": prereg["hypothesis.H1"]["secondary"],
            "directions": prereg["hypothesis.H1"]["directions"],
        },
        "drift": {
            "len_basis": "benchmark/suite.kvx [task.<id>].turns, the reference solution's turn count, recorded before any run",
            "turn_rule": "drift-1 at max(1, floor(len/2)); drift-3 at max(1, floor(len/4)), max(1, floor(len/2)), max(1, floor(3*len/4))",
            "selection": "rules ordered by sha256('cwr-h1-drift|<task>|<rule>'); templates taken level by level (A, B, C) over that order; first three distinct (primitive, template) pairs; drift-1 is the first pick, drift-3 is (second, first, third)",
            "change": "precondition_added; the manual, SDK and contracts.json are unchanged",
            "templates": {p: [list(t) for t in ts] for p, ts in sorted(TEMPLATES.items())},
        },
    }
    plans = {
        task: drift_plan(task, list(info["rules"]), int(info["turns"]))
        for task, info in tasks.items()
    }
    with tempfile.TemporaryDirectory(prefix="cwr-h1-drift-") as tmp:

        def compile_drift(item: tuple[str, str]) -> tuple[str, str, str]:
            task, arm = item
            path = Path(tmp) / f"{task}.{arm}.kvx"
            path.write_text(drift_file_text(task, arm, plans[task][arm]), encoding="utf-8")
            return task, arm, compile_build(binary, ["--x-drift", str(path)])["hash"]

        items = [(task, arm) for task in tasks for arm in DRIFT_FRACTIONS]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            built = {(t, a): h for t, a, h in pool.map(compile_drift, items)}
    for task, info in tasks.items():
        section: dict[str, Any] = {
            "bin": info["bin"],
            "len": int(info["turns"]),
            "rules": info["rules"],
        }
        for arm, entries in plans[task].items():
            key = arm.replace("-", "_")
            section[key] = entries
            section[f"{key}_spec"] = drift_spec_label(entries)
            section[f"{key}_build"] = built[(task, arm)]
        h1[f"task.{task}"] = section
    header = (
        "Copyright Sidiora Labs Inc. CWR® 2026-2027\n"
        "SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0\n"
        "{h} campaign registration (task 5.2; spec/cwr/experiments.md section {s}). Generated by\n"
        "benchmark/campaigns/h1h2.py register before any {h} run; never edited by hand. Every arm is a\n"
        "CWR Build of refworld with at most one pillar ablated; there is no external baseline."
    )
    write_text(CAMPAIGN_FILE["H1"], dump_kvx(h1, header.format(h="H1", s="3.1")))
    write_text(CAMPAIGN_FILE["H2"], dump_kvx(h2, header.format(h="H2", s="3.2")))
    print(f"control {control['hash']}\none-primitive {one['hash']}")
    return 0


def plan_cmd(args: argparse.Namespace) -> int:
    for task, info in suite_tasks().items():
        plan = drift_plan(task, list(info["rules"]), int(info["turns"]))
        for arm, entries in plan.items():
            schedule = ", ".join(f"t{e['turn']} {e['template']} {e['failure']}" for e in entries)
            print(f"{task} len={info['turns']} {arm}: {schedule}")
    return 0


# ---------------------------------------------------------------------------------------------
# Execution


def load_campaign(hypothesis: str) -> dict[str, dict[str, Any]]:
    path = CAMPAIGN_FILE[hypothesis]
    if not path.exists():
        raise CampaignError(f"{path} does not exist; run `register` first")
    return read_kvx(path)


def cell_build(campaign: dict[str, dict[str, Any]], hypothesis: str, arm: str, task: str) -> str:
    """The registered Build hash of one cell."""
    builds = campaign["registration"]["builds"]
    if builds[arm] != "per_task":
        return builds[arm]
    return campaign[f"task.{task}"][f"{arm.replace('-', '_')}_build"]


def cell_path(hypothesis: str, arm: str, task: str, seed: int) -> Path:
    return RESULTS / hypothesis.lower() / arm / task / str(seed)


def cell_state(path: Path) -> str:
    """missing, done or retry (an attempt that ended in a model or kernel error)."""
    attempt = path / "attempt.kvx"
    if not attempt.exists() or not (path / "ledger.jsonl").exists():
        return "missing"
    try:
        status = read_kvx(attempt)["attempt"]["exit_status"]
    except (KeyError, ValueError):
        return "retry"
    return "retry" if status in RETRY_EXITS else "done"


def cells(
    hypotheses: list[str], arms: list[str] | None, tasks: list[str] | None, seeds: list[int]
) -> list[tuple[str, str, str, int]]:
    """Seed-major order, so a time-limited run leaves whole seeds complete across every arm."""
    order = list(suite_tasks())
    chosen = [t for t in order if tasks is None or t in tasks]
    out = []
    for seed in seeds:
        for task in chosen:
            for hypothesis in hypotheses:
                for arm in ARM_KEYS[hypothesis]:
                    if arms is None or arm in arms:
                        out.append((hypothesis, arm, task, seed))
    return out


class _Progress:
    def __init__(self, total: int):
        self.total = total
        self.done = 0
        self.lock = threading.Lock()
        self.t0 = time.monotonic()
        self.status: dict[str, int] = {}

    def record(self, label: str, status: str, turns: int) -> None:
        with self.lock:
            self.done += 1
            self.status[status] = self.status.get(status, 0) + 1
            elapsed = time.monotonic() - self.t0
            print(
                f"[{self.done}/{self.total} {elapsed / 60:.1f}m] {label}: {status} ({turns} turns)",
                flush=True,
            )


CASSETTES = Path(os.environ.get("CWR_CASSETTE_SET") or REPO / "benchmark" / "cassettes" / "suite")
HOME = REPO / ".cwr" / "campaign-1" / "home"


def run_cell(
    hypothesis: str,
    arm_key: str,
    task: str,
    seed: int,
    campaigns: dict[str, dict[str, dict[str, Any]]],
    clients: threading.local,
    results_root: Path = RESULTS,
    allow_unregistered: bool = False,
) -> dict[str, Any]:
    """Run one Instance exactly as benchmark/harness/run.py main does, plus the drift spec label."""
    from benchmark.harness import run as runner
    from benchmark.harness.arms import load_arms
    from benchmark.harness.model import GatewayClient
    from benchmark.harness.prereg import load_prereg, missing_fields

    campaign = campaigns[hypothesis]
    prereg = load_prereg(PREREG)
    meta, arms = load_arms(ARMS)
    arm = arms[(hypothesis, arm_key)]
    missing = missing_fields(prereg, arms, meta["control"])
    if missing and not allow_unregistered:
        raise CampaignError("pre-registration incomplete: " + ", ".join(missing))
    info = suite_tasks()[task]
    camp = prereg["campaign"]
    if not hasattr(clients, "model"):
        clients.model = GatewayClient.from_env()
    if camp.get("model") and clients.model.model != camp["model"] and not allow_unregistered:
        raise CampaignError("the configured model identity differs from the registered one")
    build = cell_build(campaign, hypothesis, arm_key, task)
    path = build_dir(build)
    if path is None:
        raise CampaignError(f"Build {build} is not on disk")
    refusal = runner.check_arm_build(arm, path)
    if refusal:
        raise CampaignError(refusal)
    cassette = CASSETTES / task / f"seed-{seed}.jsonl"
    if not cassette.is_file():
        raise CampaignError(f"no cassette {cassette}")
    cassette_digest = runner.cassette_set_digest(CASSETTES)
    if camp.get("cassette_digest") and cassette_digest != camp["cassette_digest"]:
        raise CampaignError("the cassette set digest differs from the registered one")
    task_dir = REPO / "benchmark" / "tasks" / task
    drift_spec = None
    if arm.flag == "--x-drift":
        drift_spec = campaign[f"task.{task}"][f"{arm_key.replace('-', '_')}_spec"]
    spec = runner.EpisodeSpec(
        hypothesis=hypothesis,
        arm=arm,
        task=task,
        seed=seed,
        build=build,
        predicate_path=task_dir / "success.py",
        initial_state=task_dir / "initial.kvx",
        turn_limit=runner._turn_limit(camp, info["bin"]),
        bin=info["bin"],
        max_tokens_per_turn=camp.get("max_tokens_per_turn", 16000),
        token_budget=camp.get("token_budget_per_instance"),
        model_params=camp.get("sampling") or {},
        registered=not missing,
        cassette_digest=cassette_digest,
        suite_digest=camp.get("suite_digest"),
        results_root=results_root,
        cassette=cassette,
        instruction=(task_dir / "task.md").read_text("utf-8"),
        reuse_rule=runner._reuse_rule(info),
        drift_spec=drift_spec,
    )
    kernel = runner.CwrCliKernel(
        gateway_mode="replay",
        home=HOME,
        builds=[BUILD_ROOT, XBUILD_ROOT, REPO / "build", REPO / "xbuild"],
    )
    try:
        return runner.run_episode(spec, kernel, clients.model)
    except (RuntimeError, OSError, ValueError) as error:
        attempt = results_root / hypothesis.lower() / arm_key / task / str(seed) / "attempt.kvx"
        if attempt.exists():
            return read_kvx(attempt)["attempt"]
        return {"exit_status": "kernel_error", "turns": 0, "detail": str(error)[:300]}


def run(args: argparse.Namespace) -> int:
    hypotheses = args.hypothesis or ["H1", "H2"]
    campaigns = {h: load_campaign(h) for h in hypotheses}
    seeds = [int(s) for s in args.seeds.split(",")] if args.seeds else SEEDS
    tasks = args.tasks.split(",") if args.tasks else None
    arms = args.arms.split(",") if args.arms else None
    root = Path(args.results_root) if args.results_root else RESULTS
    todo = [
        c
        for c in cells(hypotheses, arms, tasks, seeds)
        if cell_state(root / c[0].lower() / c[1] / c[2] / str(c[3])) != "done" or args.rerun
    ]
    deadline = time.monotonic() + args.deadline_min * 60 if args.deadline_min else None
    progress = _Progress(len(todo))
    clients = threading.local()
    print(f"{len(todo)} cells to run with {args.workers} workers", flush=True)

    def job(cell: tuple[str, str, str, int]) -> None:
        if deadline is not None and time.monotonic() > deadline:
            progress.record("/".join(map(str, cell)), "skipped_deadline", 0)
            return
        try:
            attempt = run_cell(*cell, campaigns, clients, root, args.allow_unregistered)
        except CampaignError as error:
            progress.record("/".join(map(str, cell)), f"refused: {error}", 0)
            return
        progress.record(
            "/".join(map(str, cell)), str(attempt.get("exit_status")), int(attempt.get("turns", 0))
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        for future in concurrent.futures.as_completed([pool.submit(job, c) for c in todo]):
            with contextlib.suppress(Exception):
                future.result()
            if future.exception() is not None:
                print(f"worker error: {future.exception()!r}", flush=True)
    print(json.dumps(progress.status, sort_keys=True))
    return 0


def status(args: argparse.Namespace) -> int:
    summary: dict[str, dict[str, int]] = {}
    for hypothesis, arm, task, seed in cells(["H1", "H2"], None, None, SEEDS):
        state = cell_state(cell_path(hypothesis, arm, task, seed))
        key = f"{hypothesis}/{arm}"
        summary.setdefault(key, {"done": 0, "retry": 0, "missing": 0})[state] += 1
    print(json.dumps(summary, indent=1, sort_keys=True))
    return 0


# ---------------------------------------------------------------------------------------------
# Analysis and reports


def analyse_hypothesis(hypothesis: str, out: Path, *, check: bool = False) -> int:
    """analyse.py over results/h<N> only (a private view holding just that hypothesis)."""
    with tempfile.TemporaryDirectory(prefix=f"cwr-{hypothesis.lower()}-view-") as tmp:
        view = Path(tmp) / hypothesis.lower()
        view.symlink_to(RESULTS / hypothesis.lower(), target_is_directory=True)
        cmd = [sys.executable, "-I", str(REPO / "benchmark" / "analyse.py"), "--results", tmp]
        cmd += ["--check", str(out)] if check else ["--out", str(out)]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, cwd=REPO)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    return proc.returncode


def analyse(args: argparse.Namespace) -> int:
    worst = 0
    for hypothesis in args.hypothesis or ["H1", "H2"]:
        out = RESULTS / hypothesis.lower() / "analysis.json"
        code = analyse_hypothesis(hypothesis, out, check=args.check)
        worst = worst or code
    return worst


def fmt(value: Any) -> str:
    """Report number format: four decimals, or n/a for an undefined value."""
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int):
        return str(value)
    return f"{value:.4f}"


def fmt_ci(ci: list[float] | None) -> str:
    return "n/a" if not ci else f"[{fmt(ci[0])}, {fmt(ci[1])}]"


def metric_rows(body: dict[str, Any], arms: list[str], metrics: list[str]) -> list[str]:
    rows = [
        "| Arm | Bin | Metric | Instances | n | Undefined | Mean | 95% CI |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for metric in metrics:
        for group in ("short", "medium", "long"):
            for arm in arms:
                cell = body["cells"][arm][group]
                m = cell["metrics"][metric]
                rows.append(
                    f"| {arm} | {group} | {metric} | {cell['instances']} | {m['n']} | "
                    f"{m['undefined']} | {fmt(m['mean'])} | {fmt_ci(m['ci'])} |"
                )
    return rows


def direction_rows(body: dict[str, Any]) -> list[str]:
    rows = [
        "| Direction | Group | Expected | Estimate (a - b) | 95% CI | Label |",
        "|---|---|---|---|---|---|",
    ]
    for d in body["directions"]:
        rows.append(
            f"| {d['id']} | {d['group']} | {d['expect']} | {fmt(d['estimate'])} | "
            f"{fmt_ci(d['ci'])} | {d['label']} |"
        )
    return rows


def matrix_counts(hypothesis: str) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for h, arm, task, seed in cells([hypothesis], None, None, SEEDS):
        path = cell_path(h, arm, task, seed)
        state = cell_state(path)
        entry = out.setdefault(arm, {"done": 0, "retry": 0, "missing": 0, "seeds_complete": 0})
        entry[state] += 1
    for arm in out:
        complete = 0
        for seed in SEEDS:
            if all(
                cell_state(cell_path(hypothesis, arm, t, seed)) == "done" for t in suite_tasks()
            ):
                complete += 1
        out[arm]["seeds_complete"] = complete
    return out


def exit_counts(hypothesis: str) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    root = RESULTS / hypothesis.lower()
    for arm in ARM_KEYS[hypothesis]:
        counts: dict[str, int] = {}
        for attempt in sorted((root / arm).glob("*/*/attempt.kvx")):
            status = read_kvx(attempt)["attempt"]["exit_status"]
            counts[status] = counts.get(status, 0) + 1
        out[arm] = counts
    return out


def render_report(hypothesis: str, deviations: list[str]) -> str:
    analysis_path = RESULTS / hypothesis.lower() / "analysis.json"
    report = json.loads(analysis_path.read_text(encoding="utf-8"))
    body = report["hypotheses"][hypothesis]
    prereg = read_kvx(PREREG)[f"hypothesis.{hypothesis}"]
    campaign = load_campaign(hypothesis)["registration"]
    arms = list(ARM_KEYS[hypothesis])
    metrics = [prereg["primary"], *prereg["secondary"]]
    if hypothesis == "H2":
        metrics.append("failure_rate.LIMIT")
    title = {"H1": "H1 Stability", "H2": "H2 Direct action"}[hypothesis]
    matrix = body["matrix"]
    counts = matrix_counts(hypothesis)
    exits = exit_counts(hypothesis)
    lines = [
        "<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->",
        "<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->",
        "",
        f"# {title}: campaign report",
        "",
        "Generated by `python3 benchmark/campaigns/h1h2.py report` from "
        f"`benchmark/results/{hypothesis.lower()}/analysis.json`, which `benchmark/analyse.py` "
        "computes from ledgers and final state only. Every number below is rendered from that "
        "analysis; `tests/cwr/cases/5_2.py` recomputes it from the stored ledgers and compares. "
        "Every arm is a CWR Build of refworld with at most one pillar ablated; nothing here is "
        "compared with any other agent or harness.",
        "",
        "## Claim and registration",
        "",
        f"Claim (pre-registered): {body['claim']}",
        "",
        "| Item | Value |",
        "|---|---|",
        f"| Arms | {', '.join(arms)} (control {campaign['control']}) |",
        f"| Task set | {campaign['task_set']}, {campaign['tasks']} tasks in three bins |",
        f"| Seeds registered | {len(campaign['seeds'])} per task per arm |",
        f"| Gateway mode | {campaign['gateway_mode']} |",
        f"| Ledger tail K | {campaign['tail']} |",
        f"| Model | {campaign['model']} |",
        f"| Sampling | {json.dumps(campaign['sampling'])} |",
        f"| Control Build | `{campaign['control_build']}` |",
        f"| Registration | `{report['registration']}`, registered at {campaign['registered_at']} |",
        f"| metrics.py digest | `{report['inputs']['metrics_py']}` |",
        f"| Primary metric | {body['primary']} |",
        f"| Resamples | {report['analysis']['resamples']}, confidence "
        f"{report['analysis']['confidence']} |",
        "",
        "## Matrix",
        "",
        f"Expected cells {matrix['expected']}; present {matrix['present']}; missing "
        f"{matrix['missing_count']}; complete {fmt(matrix['complete'])}. Excluded from analysis: "
        f"{len(report['excluded'])}.",
        "",
        "| Arm | Cells done | Cells to retry | Cells missing | Seeds complete over all 60 tasks |",
        "|---|---|---|---|---|",
    ]
    for arm in arms:
        c = counts[arm]
        lines.append(
            f"| {arm} | {c['done']} | {c['retry']} | {c['missing']} | {c['seeds_complete']} |"
        )
    lines += ["", "| Arm | Exit statuses |", "|---|---|"]
    for arm in arms:
        text = ", ".join(f"{k} {v}" for k, v in sorted(exits[arm].items())) or "none"
        lines.append(f"| {arm} | {text} |")
    lines += [
        "",
        "## Metrics per arm and bin",
        "",
        "Means over Instances with 95% percentile bootstrap intervals; bins are never pooled. "
        "`n` counts Instances where the metric is defined; `Undefined` counts the rest "
        "(for example turns to success on a failed Instance).",
        "",
        *metric_rows(body, arms, metrics),
        "",
        "## Pre-registered directions",
        "",
        "A label describes the interval of the difference of means: consistent when it lies wholly "
        "on the registered side, inconsistent when wholly on the other, inconclusive otherwise. "
        "A label is not a finding beyond this campaign.",
        "",
        *direction_rows(body),
    ]
    if hypothesis == "H1":
        lines += [
            "",
            "## Drift arms per drift spec",
            "",
            "Each spec is the ordered list of templates injected (benchmark/campaigns/h1.kvx). "
            "Values are means over the Instances that ran that spec.",
            "",
            "| Arm | Spec | Instances | task_success | repeated_failure_rate |",
            "|---|---|---|---|---|",
        ]
        for arm, specs in sorted(body["per_drift_spec"].items()):
            for spec, entry in sorted(specs.items()):
                m = entry["metrics"]
                lines.append(
                    f"| {arm} | {spec} | {entry['instances']} | "
                    f"{fmt(m['task_success']['mean'])} | "
                    f"{fmt(m['repeated_failure_rate']['mean'])} |"
                )
    tokens = body["harness_recorded_tokens"]
    lines += [
        "",
        "## Harness-recorded tokens",
        "",
        f"Label: {tokens['label']}. Mean input plus output tokens per Instance.",
        "",
        "| Arm | Bin | n | Mean | 95% CI |",
        "|---|---|---|---|---|",
    ]
    for arm in arms:
        for group in ("short", "medium", "long"):
            t = tokens["cells"][arm][group]
            lines.append(f"| {arm} | {group} | {t['n']} | {fmt(t['mean'])} | {fmt_ci(t['ci'])} |")
    lines += ["", "## Deviations from pre-registration", ""]
    lines += [f"- {d}" for d in deviations] or ["- None."]
    lines += [
        "",
        "## Reproduce",
        "",
        "```sh",
        f"python3 benchmark/campaigns/h1h2.py analyse --hypothesis {hypothesis} --check",
        "python3 tests/cwr/cases/5_2.py",
        "```",
        "",
    ]
    return "\n".join(lines)


def deviations_for(hypothesis: str) -> list[str]:
    path = CAMPAIGNS / "deviations.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [*data.get("common", []), *data.get(hypothesis, [])]


def report_cmd(args: argparse.Namespace) -> int:
    for hypothesis in args.hypothesis or ["H1", "H2"]:
        text = render_report(hypothesis, deviations_for(hypothesis))
        REPORT[hypothesis].parent.mkdir(parents=True, exist_ok=True)
        REPORT[hypothesis].write_text(text, encoding="utf-8")
        print(f"wrote {REPORT[hypothesis].relative_to(REPO)}")
    return 0


# ---------------------------------------------------------------------------------------------
# Command line


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CWR H1 and H2 campaigns (task 5.2).")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan", help="print the derived drift schedule")
    p = sub.add_parser("register", help="compile Builds and write h1.kvx and h2.kvx")
    p.add_argument("--cwr-bin", help="pinned cwr binary (default $CWR_BIN or the target dir)")
    p.add_argument("--force", action="store_true", help="re-register although results exist")
    p = sub.add_parser("run", help="execute campaign cells")
    p.add_argument("--hypothesis", action="append", choices=["H1", "H2"])
    p.add_argument("--arms", help="comma-separated arm keys")
    p.add_argument("--tasks", help="comma-separated task ids")
    p.add_argument("--seeds", help="comma-separated seeds (default 1..10)")
    p.add_argument("--workers", type=int, default=20)
    p.add_argument("--deadline-min", type=float, help="stop starting cells after this many minutes")
    p.add_argument("--rerun", action="store_true", help="re-run cells that are already done")
    p.add_argument("--results-root", help="pilot runs: a results root outside benchmark/results")
    p.add_argument(
        "--allow-unregistered",
        action="store_true",
        help="pilot runs only: run under a draft registration (attempts marked registered=false)",
    )
    p = sub.add_parser("analyse", help="run analyse.py per hypothesis")
    p.add_argument("--hypothesis", action="append", choices=["H1", "H2"])
    p.add_argument("--check", action="store_true", help="compare with the stored analysis")
    p = sub.add_parser("report", help="render the research reports")
    p.add_argument("--hypothesis", action="append", choices=["H1", "H2"])
    sub.add_parser("status", help="count cells per arm")
    args = parser.parse_args(argv)
    handlers = {
        "plan": plan_cmd,
        "register": register,
        "run": run,
        "analyse": analyse,
        "report": report_cmd,
        "status": status,
    }
    try:
        return handlers[args.command](args)
    except CampaignError as error:
        print(f"h1h2: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

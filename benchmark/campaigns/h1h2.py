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
  complete-registration
             fill the null campaign inputs of arms.kvx and preregistration.kvx (refused once any
             result exists or the registration is already registered)
  analyse    run benchmark/analyse.py separately over H1 and H2 into results/h<N>/analysis.json,
             replaying recorded turns with the campaign's pinned cwr binary
  report     render research/h1-stability.md and research/h2-direct-action.md from those analyses
  status     count present, missing and errored cells per arm
  prune      move interrupted cells (no attempt record) out of the results tree

The campaign runs on frozen copies of the task suite and the suite cassette set under
.cwr/campaign-1/frozen, compiles into its own Build root .cwr/campaign-1/build, and replays with the
binary .cwr/campaign-1/bin/cwr; their digests are registered in h1.kvx and h2.kvx.

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
import shutil
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
    read_ledger,
    sha256_file,
)

CAMPAIGNS = REPO / "benchmark" / "campaigns"
RESULTS = REPO / "benchmark" / "results"
RESEARCH = REPO / "research"
# The campaign runs on a frozen copy of the task suite and the suite cassette set, taken when the
# campaign was registered (experiments.md section 5: a campaign freezes its suite and cassette
# digests). The copies are byte-identical to the repository files they were taken from; their
# digests are registered in benchmark/campaigns/h1.kvx and h2.kvx and in preregistration.kvx.
FROZEN = REPO / ".cwr" / "campaign-1" / "frozen" / "benchmark"
SUITE = FROZEN / "suite.kvx"
TASKS = FROZEN / "tasks"
PREREG = REPO / "benchmark" / "preregistration.kvx"
ARMS = REPO / "benchmark" / "harness" / "arms.kvx"
METRICS = REPO / "benchmark" / "harness" / "metrics.py"
SOURCE = REPO / "examples" / "refworld"
# The campaign compiles into its own Build root so that the control Build takes the first-Build path
# (previous null) and its hash depends on the World Source and compiler only, never on whatever other
# Builds of refworld happen to exist under build/. Experimental Builds land in xbuild/ beside it.
BUILD_ROOT = Path(
    os.environ.get("CWR_CAMPAIGN_BUILD_ROOT") or REPO / ".cwr" / "campaign-1" / "build"
)
XBUILD_ROOT = BUILD_ROOT.parent / "xbuild"
HOME = BUILD_ROOT.parent / "home"
# The cwr binary the campaign ran with (its sha256 is registered as cwr_binary_sha256); analyse.py
# replays turns with this binary to recompute turns to success from the ledgers.
CAMPAIGN_BIN = BUILD_ROOT.parent / "bin" / "cwr"
ANALYSE_JOBS = 32
CASSETTES = Path(os.environ.get("CWR_CASSETTE_SET") or FROZEN / "cassettes" / "suite")
CAMPAIGN_FILE = {"H1": CAMPAIGNS / "h1.kvx", "H2": CAMPAIGNS / "h2.kvx"}
ARM_KEYS = {"H1": ("frozen", "drift-1", "drift-3"), "H2": ("script", "one-primitive")}
CONTROL_ARM = {"H1": "frozen", "H2": "script"}
REPORT = {"H1": RESEARCH / "h1-stability.md", "H2": RESEARCH / "h2-direct-action.md"}
SEEDS = list(range(1, 11))
RETRY_EXITS = ("model_error", "kernel_error", "metrics_error", "predicate_error")

# Drift templates per primitive or adapter: (template id, expression, failure code). Each expression
# references only parameters and read_only primitives of refworld (world-source.md section 5), so a drift
# Build passes the same lint as a contract. Templates tighten the rule; none relaxes it. Where a
# primitive has a tightening that a changed procedure can still satisfy (overwrite only after a delete,
# submit only while fewer than two jobs are active), that one is template A, which drift-1 uses.
# Some templates cannot be satisfied for some tasks; results are therefore also reported per spec.
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
        ("A", "jobs.active() < 2", "LIMIT"),
        ("B", "n <= 10", "LIMIT"),
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
        ("A", "not kv.has(ns, key)", "CONFLICT"),
        ("B", "len(value) <= 32", "LIMIT"),
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
    "world.random": [
        ("A", "world.now() >= 20", "LIMIT"),
        ("B", "jobs.active() == 0", "CONFLICT"),
        ("C", "world.now() >= 40", "LIMIT"),
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
        "suite_digest": frozen_suite_digest(),
        "cassette_digest": prereg["campaign"].get("cassette_digest"),
        "control_build": control["hash"],
        "build_roots": [str(BUILD_ROOT.relative_to(REPO)), str(XBUILD_ROOT.relative_to(REPO))],
        "suite_path": str(SUITE.relative_to(REPO)),
        "tasks_dir": str(TASKS.relative_to(REPO)),
        "cassette_set": str(CASSETTES.relative_to(REPO)),
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


def _set_key(text: str, section: str, key: str, value: Any) -> str:
    """Replace `key = ...` inside [section] of a KVX text, keeping every other byte."""
    lines = text.split("\n")
    current = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1]
        elif current == section and stripped.startswith(f"{key} = "):
            literal = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(", ", ": "))
            lines[index] = f"{key} = {literal}"
            return "\n".join(lines)
    raise CampaignError(f"[{section}] {key} not found")


def registration_blockers() -> list[str]:
    """Why the registration may no longer change; empty when complete-registration may run.

    Any attempt record under benchmark/results/h1 to h4 freezes the registration of every
    hypothesis (experiments.md section 5: a change after execution is a new pre-registration and a
    new campaign), and a registration already marked registered is never rewritten.
    """
    blockers = [
        f"{hypothesis} results exist under {(RESULTS / hypothesis.lower()).relative_to(REPO)}"
        for hypothesis in ("H1", "H2", "H3", "H4")
        if any_results(hypothesis)
    ]
    if read_kvx(PREREG)["meta"].get("status") == "registered":
        blockers.append("benchmark/preregistration.kvx is already registered")
    return blockers


def frozen_suite_digest() -> str:
    """The suite digest of the frozen task suite, recomputed from its files.

    Computed by benchmark/predicates/common.py suite_digest over TASKS, the function analyse.py
    and run.py use, and refused (CampaignError) unless it equals the [suite].suite_digest that
    suite.kvx declares, so a registration never records a digest the files do not have.
    """
    from benchmark.analyse import recompute_suite_digest

    actual = recompute_suite_digest(TASKS)
    declared = read_kvx(SUITE).get("suite", {}).get("suite_digest")
    if actual != declared:
        raise CampaignError(
            f"{SUITE.relative_to(REPO)} declares suite_digest {declared}, but its tasks digest to "
            f"{actual}"
        )
    return actual


def complete_registration(args: argparse.Namespace) -> int:
    """Fill the null campaign inputs of arms.kvx and preregistration.kvx and mark it registered.

    Refused once any H1 to H4 attempt exists or the registration is already registered. Only null
    inputs are filled; a non-null input that differs from the value the campaign needs (the
    upgrades digest included) is a refusal, never an overwrite. Every check, the parse of both final
    texts included, runs before the first write; the arms.kvx digest is taken from the exact bytes
    then written, and the suite digest is recomputed from the frozen task files.
    """
    from benchmark.harness.model import GatewayClient, ModelError
    from benchmark.harness.run import cassette_set_digest

    blockers = registration_blockers()
    if blockers:
        raise CampaignError("the registration is frozen: " + "; ".join(blockers))
    h1, h2 = load_campaign("H1"), load_campaign("H2")
    control = h1["registration"]["control_build"]
    one = h2["registration"]["builds"]["one-primitive"]
    arms_text = ARMS.read_text(encoding="utf-8")
    arms = read_kvx(ARMS)
    for section, value in (("control", control), ("arm.h2.one-primitive", one)):
        current = arms[section].get("build")
        if current not in (None, value):
            raise CampaignError(f"arms.kvx [{section}] build {current} is not {value}")
        arms_text = _set_key(arms_text, section, "build", value)
    parse_kvx(arms_text, str(ARMS))
    if not CASSETTES.is_dir():
        raise CampaignError(f"cassette set {CASSETTES} does not exist")
    try:
        model = GatewayClient.from_env().model
    except ModelError as error:
        raise CampaignError(str(error)) from None
    wanted = {
        "model": model,
        "sampling": {},
        "suite_digest": frozen_suite_digest(),
        "cassette_digest": cassette_set_digest(CASSETTES),
        "upgrades_digest": sha256_file(REPO / "benchmark" / "upgrades.kvx"),
    }
    text = PREREG.read_text(encoding="utf-8")
    campaign = read_kvx(PREREG)["campaign"]
    for key, value in wanted.items():
        if campaign.get(key) is None:
            text = _set_key(text, "campaign", key, value)
        elif campaign[key] != value:
            raise CampaignError(f"preregistration [campaign] {key} is registered as another value")
    arms_bytes = arms_text.encode("utf-8")
    text = _set_key(text, "digests", "metrics_py", sha256_file(METRICS))
    text = _set_key(text, "digests", "arms_kvx", "sha256:" + hashlib.sha256(arms_bytes).hexdigest())
    text = _set_key(text, "meta", "status", "registered")
    parse_kvx(text, str(PREREG))
    ARMS.write_bytes(arms_bytes)
    PREREG.write_text(text, encoding="utf-8")
    print("preregistration complete: status registered")
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


_SLOTS = {"lock": threading.Lock(), "next": 0}


def gateway_client(client_class: Any) -> tuple[int, Any]:
    """A model client for this worker on one of the gateway slots named in $CWR_GATEWAYS.

    Slot 1 is $MESSAGES_URL and slot k is $GATEWAY_<k>_MESSAGES_URL; every slot serves the one
    configured $MODEL with the same keys and no sampling parameter. The operator exports these
    variables from .env into the runner's environment. Workers take slots round-robin. No URL or
    key value leaves the client; only the slot number is recorded (gateway.json per cell).
    """
    from benchmark.harness.model import KEY_NAMES

    slots = [int(x) for x in (os.environ.get("CWR_GATEWAYS") or "1").split(",")]
    with _SLOTS["lock"]:
        slot = slots[_SLOTS["next"] % len(slots)]
        _SLOTS["next"] += 1
    url = os.environ.get("MESSAGES_URL" if slot == 1 else f"GATEWAY_{slot}_MESSAGES_URL", "")
    keys = [os.environ.get(name, "") for name in KEY_NAMES]
    return slot, client_class(url, os.environ.get("MODEL", ""), keys)


def run_cell(
    hypothesis: str,
    arm_key: str,
    task: str,
    seed: int,
    clients: threading.local,
    results_root: Path = RESULTS,
    allow_unregistered: bool = False,
    rerun: bool = False,
) -> dict[str, Any]:
    """Run one Instance through benchmark/harness/run.py: its preflight resolves and checks the cell
    against the registration (arms.kvx, campaigns/h1.kvx per-task drift Builds and specs, the cassette
    set) and its episode loop runs it; this function only chooses the gateway slot and home."""
    from benchmark.harness import run as runner
    from benchmark.harness.model import GatewayClient

    if not hasattr(clients, "model"):
        clients.slot, clients.model = gateway_client(GatewayClient)
        # One Instance store per worker: concurrent Instances of one task never share a file.
        clients.home = HOME / f"worker-{os.getpid()}-{threading.get_ident()}"
    roots = [BUILD_ROOT, XBUILD_ROOT, REPO / "build", REPO / "xbuild"]
    try:
        cell = runner.preflight(
            hypothesis,
            arm_key,
            task,
            seed,
            model_identity=clients.model.model,
            build_roots=roots,
            cassettes=CASSETTES,
            allow_unregistered=allow_unregistered,
            suite_path=SUITE,
            tasks_dir=TASKS,
        )
    except runner.PreflightRefused as error:
        raise CampaignError(str(error)) from None
    spec = cell.episode(results_root=results_root, rerun=rerun)
    kernel = runner.CwrCliKernel(gateway_mode="replay", home=clients.home, builds=roots)
    sidecar = results_root / hypothesis.lower() / arm_key / task / str(seed) / "gateway.json"
    try:
        attempt = runner.run_episode(spec, kernel, clients.model)
        sidecar.write_text(json.dumps({"gateway_slot": clients.slot}) + "\n", encoding="utf-8")
        return attempt
    except (RuntimeError, OSError, ValueError) as error:
        attempt_path = sidecar.parent / "attempt.kvx"
        if attempt_path.exists():
            sidecar.write_text(json.dumps({"gateway_slot": clients.slot}) + "\n", encoding="utf-8")
            return read_kvx(attempt_path)["attempt"]
        return {"exit_status": "kernel_error", "turns": 0, "detail": str(error)[:300]}


def harness_digests() -> dict[str, str]:
    """sha256 of every harness source a runner process loads, recorded when the process starts."""
    files = sorted((REPO / "benchmark" / "harness").glob("*.py"))
    files += [REPO / "benchmark" / "predicates" / "common.py"]
    return {str(p.relative_to(REPO)): sha256_file(p) for p in files if p.exists()}


def run(args: argparse.Namespace) -> int:
    hypotheses = args.hypothesis or ["H1", "H2"]
    for hypothesis in hypotheses:
        load_campaign(hypothesis)
    from benchmark.harness import model as _model  # noqa: F401  (loaded once, at start)
    from benchmark.harness import run as _run  # noqa: F401

    loaded = harness_digests()
    log = BUILD_ROOT.parent / "runners.jsonl"
    with log.open("a", encoding="utf-8") as handle:
        entry = {"pid": os.getpid(), "started": _now(), "harness": loaded}
        handle.write(json.dumps(entry, sort_keys=True) + "\n")
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

    locks = BUILD_ROOT.parent / "locks"
    locks.mkdir(parents=True, exist_ok=True)

    def job(cell: tuple[str, str, str, int]) -> None:
        if deadline is not None and time.monotonic() > deadline:
            progress.record("/".join(map(str, cell)), "skipped_deadline", 0)
            return
        path = root / cell[0].lower() / cell[1] / cell[2] / str(cell[3])
        if cell_state(path) == "done" and not args.rerun:
            progress.record("/".join(map(str, cell)), "already_done", 0)
            return
        lock = locks / f"{cell[0]}.{cell[1]}.{cell[2]}.{cell[3]}"
        try:
            lock.touch(exist_ok=False)
        except FileExistsError:
            progress.record("/".join(map(str, cell)), "running_elsewhere", 0)
            return
        try:
            attempt = run_cell(*cell, clients, root, args.allow_unregistered, args.rerun)
        except CampaignError as error:
            progress.record("/".join(map(str, cell)), f"refused: {error}", 0)
            return
        finally:
            lock.unlink(missing_ok=True)
        status = str(attempt.get("exit_status"))
        if status in RETRY_EXITS:
            status += f" [{str(attempt.get('detail'))[:200]}]"
        progress.record("/".join(map(str, cell)), status, int(attempt.get("turns", 0)))

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


# The files of a cell that analyse.py reads as ledger-only inputs. The recorded model messages are
# left out of the analysis view on purpose: without them analyse.py computes turns to success by the
# registered stop rule (preregistration.kvx convention c7) instead of replaying every turn, because a
# replay re-runs each script under the Build's 30000 ms wall-clock limit and so depends on host load
# (see the deviation "Turns to success" in the reports).
VIEW_FILES = ("ledger.jsonl", "final_state.kvx", "attempt.kvx")


def ledger_view(hypothesis: str, root: Path) -> None:
    """Mirror results/h<N> under root with only the ledger-only files of every cell, as symlinks."""
    source = RESULTS / hypothesis.lower()
    for arm in ARM_KEYS[hypothesis]:
        for cell in sorted((source / arm).glob("*/*")):
            if not cell.is_dir():
                continue
            target = root / hypothesis.lower() / cell.relative_to(source)
            target.mkdir(parents=True)
            for name in VIEW_FILES:
                if (cell / name).exists():
                    (target / name).symlink_to(cell / name)


def analyse_hypothesis(hypothesis: str, out: Path, *, check: bool = False) -> int:
    """analyse.py over the ledger-only view of results/h<N> (that hypothesis alone)."""
    with tempfile.TemporaryDirectory(prefix=f"cwr-{hypothesis.lower()}-view-") as tmp:
        ledger_view(hypothesis, Path(tmp))
        cmd = [sys.executable, "-I", str(REPO / "benchmark" / "analyse.py"), "--results", tmp]
        cmd += ["--suite", str(SUITE), "--tasks-dir", str(TASKS), "--cassettes", str(CASSETTES)]
        cmd += ["--cwr", str(CAMPAIGN_BIN), "--jobs", str(ANALYSE_JOBS)]
        for root in (BUILD_ROOT, XBUILD_ROOT):
            cmd += ["--builds", str(root)]
        cmd += ["--check", str(out)] if check else ["--out", str(out)]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, cwd=REPO)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    return proc.returncode


def partial_cells(hypothesis: str) -> list[Path]:
    """Cell directories without an attempt record: runs interrupted before they wrote one."""
    root = RESULTS / hypothesis.lower()
    return sorted(
        p
        for arm in ARM_KEYS[hypothesis]
        for p in (root / arm).glob("*/*")
        if p.is_dir() and not (p / "attempt.kvx").exists()
    )


def prune(args: argparse.Namespace) -> int:
    """Move interrupted cells (no attempt record) out of the results tree, listing each one."""
    graveyard = BUILD_ROOT.parent / "interrupted"
    moved = 0
    for hypothesis in args.hypothesis or ["H1", "H2"]:
        for path in partial_cells(hypothesis):
            target = graveyard / path.relative_to(RESULTS)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                shutil.rmtree(target)
            path.rename(target)
            print(f"moved interrupted cell {path.relative_to(RESULTS)}")
            moved += 1
    print(f"{moved} interrupted cells moved to {graveyard}")
    return 0


def analyse(args: argparse.Namespace) -> int:
    worst = 0
    for hypothesis in args.hypothesis or ["H1", "H2"]:
        partial = partial_cells(hypothesis)
        if partial:
            raise CampaignError(f"{len(partial)} interrupted {hypothesis} cells; run `prune` first")
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
    names = sorted(body["cells"][arms[0]]["short"]["metrics"])
    metrics = []
    for metric in [prereg["primary"], *prereg["secondary"]]:
        # failure_rate is registered per envelope code; it expands to failure_rate.<CODE>.
        metrics += [n for n in names if n.startswith(metric + ".")] or [metric]
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
        "",
        "Summary of the labels: "
        + "; ".join(f"{d['id']} ({d['group']}): {d['label']}" for d in body["directions"])
        + ".",
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
    automatic = []
    short = {arm: c["seeds_complete"] for arm, c in counts.items() if c["seeds_complete"] < 10}
    if short or not matrix["complete"]:
        complete = ", ".join(f"{arm} {counts[arm]['seeds_complete']}" for arm in arms)
        automatic.append(
            "Seeds: the registered design is 10 seeds per task per arm. Seeds complete over all 60 "
            f"tasks: {complete}. The arm matrix is incomplete, so case CWR-018 cannot pass on this "
            "campaign, and every interval above is over fewer Instances than registered."
        )
        automatic.append(
            "Missing and excluded cells are not missing at random. Cells that run to the turn "
            "limit take longest, so the cells still running when the campaign was stopped, and the "
            "cells whose model requests failed mid-run, over-represent long and failing Instances, "
            "most of all in the ablated arms. Every mean above is over the cells present and is "
            "likely biased toward success in those arms; the deltas against the control are "
            "therefore more likely understated than overstated."
        )
    lines += ["", "## Deviations from pre-registration", ""]
    lines += [f"- {d}" for d in [*automatic, *deviations]] or ["- None."]
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


def resource_limit_turns(hypothesis: str) -> dict[str, int]:
    """Per arm, turns that ended in LIMIT with no call record of that turn failing with LIMIT.

    Such a turn hit a turn budget rather than a precondition: CPU, wall clock or memory, or in the
    one-primitive arm the primitive_calls budget, whose refused call writes no record.
    """
    out: dict[str, int] = {}
    for arm in ARM_KEYS[hypothesis]:
        count = 0
        for path in sorted((RESULTS / hypothesis.lower() / arm).glob("*/*/ledger.jsonl")):
            records = read_ledger(path)
            limited = {
                r["turn"]
                for r in records
                if r.get("kind") in ("primitive", "adapter")
                and (r.get("outcome") or {}).get("code") == "LIMIT"
            }
            count += sum(
                1
                for r in records
                if r.get("kind") == "turn" and r.get("code") == "LIMIT" and r["turn"] not in limited
            )
        out[arm] = count
    return out


def deviations_for(hypothesis: str) -> list[str]:
    """Deviations and registered interpretations, with every count computed from the inputs."""
    tasks = suite_tasks()
    replaced_root = RESULTS / "_replaced" / hypothesis.lower()
    replaced = sum(1 for p in replaced_root.glob("*/*/*") if p.is_dir())
    common = [
        "Build root: the campaign compiles refworld into .cwr/campaign-1/build (experimental Builds "
        "into the xbuild/ beside it) rather than build/, because build/ already held other "
        "refworld Builds of the same version, which the changelog gate refuses as a previous Build. "
        "The control Build therefore takes the first-Build path and its hash depends only on the "
        "World Source and the compiler.",
        "Frozen inputs: the campaign ran on copies of the task suite and of the suite cassette set "
        "taken at registration under .cwr/campaign-1/frozen; tests/cwr/cases/5_2.py verifies that "
        "their digests equal the registered suite and cassette digests.",
        "Pre-registration amendments, all made before any result of the executed campaign "
        "existed under benchmark/results: task 5.2 re-registered the metrics.py digest after task "
        "5.3 added the H4 misuse scope (no H1 or H2 definition changed); the review of the task "
        "suite then re-froze the suite, suite cassette and upgrades digests "
        "([deviation.suite_review_2026_11_06] in benchmark/preregistration.kvx: rules the starting "
        "state cannot trigger pruned, rate rules removed, adapter parameters pinned in task.md). "
        "benchmark/campaigns/h1.kvx and h2.kvx were regenerated from the re-frozen suite, so their "
        "drift specifications use its rules and turns and their registered_at precedes every "
        "attempt.",
        "Aborted executions: three earlier executions were stopped before completion and are "
        "retained outside the results tree (.cwr/campaign-1/aborted-1 to aborted-3), never "
        "analysed and never pooled. The first ran in one process whose Python threads were "
        "CPU-bound and was overtaken by the metrics.py change; the second was stopped within "
        "minutes when a suite revision was found; the third, run on the earlier suite and cassette "
        "digests, was stopped when the suite review re-froze them, because results under the "
        "earlier digests belong to the earlier registration.",
        "Harness: the pilot found that the predicate state view kept kv values in the kernel's "
        "bytes encoding, so no kv-based predicate could hold; task 5.2 patched that before the "
        "first registration and task 5.1 later replaced the mapping with "
        "benchmark/predicates/common.py state_view_from_instance, which the executed campaign "
        "uses. Every runner process records the sha256 of the harness sources it loaded in "
        ".cwr/campaign-1/runners.jsonl; the processes of the executed campaign loaded one version.",
        "Execution: runner processes of thirty workers each shared the cell list through "
        "per-cell locks. The first twelve spread their model requests over the first two gateway "
        "endpoints of .env; later passes, started to re-run cells whose runner had stopped or "
        "whose model requests failed, used both or, once the second endpoint answered 503, the "
        "first only. Every endpoint serves the one configured model identity with the same keys "
        "and no sampling parameter; the third answered 503 during an aborted execution and was "
        "not used. "
        "A cell's endpoint slot is recorded in its gateway.json, never the URL. Cells that ended "
        "in a model or kernel error were run again from a fresh Instance and the harness moved "
        "each failed attempt aside to benchmark/results/_replaced; cells in flight when a runner "
        "stopped left only staging directories, kept under .cwr/campaign-1/staging-leftovers and "
        "never analysed."
        + (f" Attempts moved aside for this hypothesis: {replaced}." if replaced else ""),
        "Instance stores: each worker thread uses its own Instance store under "
        ".cwr/campaign-1/home, so concurrent Instances of one task never share a file.",
        "Turns to success: analyse.py computes it by the registered stop rule (convention c7: "
        "the runner stops after the first turn whose predicate holds, so a successful Instance's "
        "last turn is its first satisfying turn), from an analysis view that holds each cell's "
        "ledger, final state and attempt record but not the recorded model messages. With the "
        "messages present analyse.py instead replays every turn, and a replay re-runs each script "
        "under the Build's 30000 ms wall-clock limit: on the loaded host a recorded turn that "
        "finished in time exceeded the limit when replayed (for example the first turn of "
        "h2/script/t060-report-pipeline/5), and analyse.py refuses the whole analysis on "
        "any such divergence. The method appears per arm and bin as turns_to_success_method.",
        "Host load (a threat): during the campaign other processes on the host drove the load "
        "average far above the core count, so the runner count was reduced. Turns that ended in LIMIT with no call record failing with "
        "LIMIT hit a turn budget (CPU, wall clock or memory; in the one-primitive arm also the "
        "primitive_calls budget, whose refused call writes no record): "
        + ", ".join(f"{arm} {n}" for arm, n in resource_limit_turns(hypothesis).items())
        + ". Outside the one-primitive arm these are counted as recorded and may include turns the "
        "host load starved.",
        "Model identity: the gateway reports its own identity string, which every attempt record "
        "keeps as model_reported and the harness accepts under the registered routing_prefix rule.",
        "Replay condition (a threat, not a deviation): a model request that differs from the "
        "reference solution's recorded request is a cassette miss and returns UNAVAILABLE, in "
        "every arm alike; the re-frozen suite pins adapter parameters in task.md to limit this.",
        "The control arm of H1 (frozen) and of H2 (script) are the same Build and K; they are run "
        "and reported separately, as registered.",
    ]
    if hypothesis == "H1":
        lens = [int(info["turns"]) for info in tasks.values()]
        ones = sum(1 for n in lens if n == 1)
        early = sum(1 for n in lens if max(1, n // 2) == 1)
        return [
            "len basis: experiments.md 3.1 defines len as the reference solution's turn count. It "
            "was registered from benchmark/suite.kvx before any run, not re-measured from a "
            f"frozen-arm run. {ones} of {len(lens)} tasks have len 1 and {early} have their "
            "drift-1 injection at turn 1, so for them the drifted rule holds from the first turn: "
            "an undocumented rule rather than a mid-task change. Turns are max(1, floor(...)) "
            "because injections happen before a turn's script and turns start at 1.",
            "Drift specs: every injection is precondition_added from the fixed template table in "
            "benchmark/campaigns/h1h2.py, drawn per task by the registered seeded rule from the "
            "task's rules file. Some templates cannot be satisfied for some tasks, which turns the "
            "drift into an impossible task; results are therefore also reported per spec.",
            "Drift visibility: the drift grammar has no visibility key and the kernel writes every "
            "drift record with visible false, so the model's first evidence of a drift is a "
            "failure the manual does not predict.",
            *common,
        ]
    over = sum(
        1
        for info in tasks.values()
        if int(info["primitive_count"]) > {"short": 15, "medium": 40, "long": 120}[info["bin"]]
    )
    limit_note = (
        f"{over} tasks have a reference primitive count above their bin's limit, so they cannot "
        "succeed under one primitive per turn; turns to success is undefined there."
        if over
        else "Every task's reference primitive count is within its bin's limit, but under one "
        "primitive per turn a task needs at least one turn per primitive plus any turns spent "
        "waiting on jobs, so the margin is narrow for the longest tasks."
    )
    return [
        "Turn limits: the one-primitive arm keeps the registered per-bin turn limits (15, 40, "
        f"120). {limit_note}",
        *common,
    ]


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
    sub.add_parser(
        "complete-registration",
        help="fill null campaign inputs in arms.kvx and preregistration.kvx; status registered",
    )
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
    p = sub.add_parser("prune", help="move interrupted cells out of the results tree")
    p.add_argument("--hypothesis", action="append", choices=["H1", "H2"])
    args = parser.parse_args(argv)
    handlers = {
        "plan": plan_cmd,
        "complete-registration": complete_registration,
        "register": register,
        "run": run,
        "analyse": analyse,
        "report": report_cmd,
        "status": status,
        "prune": prune,
    }
    try:
        return handlers[args.command](args)
    except CampaignError as error:
        print(f"h1h2: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Analyse CWR campaigns from ledgers and final state only (requirement 12.4, 12.5, 12.6; experiments.md section 5).

For every hypothesis, arm and group (bin for H1 to H3, upgrade pair for H4) it reports the mean of each
registered metric over Instances with a 95% percentile bootstrap confidence interval (10,000 resamples
over Instances by default; parameters come from benchmark/preregistration.kvx). Bins are never pooled.
Drift arms are also reported per drift spec. Every pre-registered direction is evaluated as a delta of
means with its own bootstrap interval and labelled consistent, inconsistent or inconclusive; that label
describes the interval, not a finding.

Refusals (non-zero exit, no report written):
  3  metrics.py or arms.kvx digest differs from the registered digest
  2  the registration is a draft (null campaign inputs) and --draft-ok was not given
  4  integrity failure: a ledger header disagrees with its cell (arm, seed, model, tail, Build hash,
     experimental stamp), a cell is unregistered, a ledger is malformed, or replay ledgers with
     different cassette digests would be pooled
  5  --require-complete and the arm matrix has a missing cell
  1  --check FILE and the recomputed report differs from FILE

Excluded and reported (never pooled): live-mode attempts, unregistered attempts, Instances with an
INVARIANT_VIOLATION record, and attempts that ended with a model or kernel error. Live runs under
benchmark/results/live are analysed only with --live, into a separately labelled report.

Inputs: benchmark/results/h<N>/<arm>/<task>/<seed>/{ledger.jsonl, final_state.kvx, attempt.kvx, states/},
benchmark/preregistration.kvx, benchmark/harness/arms.kvx, benchmark/suite.kvx (bins, rule-reuse rules),
benchmark/upgrades.kvx (pair tasks, changed primitives, misuse codes) and each task's success.py.
Harness-recorded token counts are read from attempt records and reported only in a separately labelled
block. Every included Instance's recomputed values are listed under `instances`, so a report can be
compared cell by cell with the values the runner recorded in each attempt's [metrics] section. The
runner's [metrics] are never read here: every number is recomputed from the ledger and states.
The output is deterministic: no timestamps, sorted keys, seeded resampling.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmark.harness import metrics
from benchmark.harness.arms import Arm, load_arms, resolve_build
from benchmark.harness.ledgerio import (
    REPO,
    InvalidKVX,
    InvalidLedger,
    load_predicate,
    read_kvx,
    read_ledger,
    sha256_file,
    state_view,
)
from benchmark.harness.prereg import (
    ARMS_PATH,
    METRICS_PATH,
    DigestMismatch,
    InvalidPreregistration,
    check_digests,
    load_prereg,
    missing_fields,
)

SCHEMA = "cwr.analysis/1"
BINS = ("short", "medium", "long")
RULE_ID = re.compile(r"(?P<primitive>[a-z]+\.[a-z_]+)(?:\[[A-Za-z0-9_]+\])?(?::(?P<code>[A-Z_]+))?")
EXCLUDED_EXITS = ("model_error", "kernel_error")


class Refused(Exception):  # noqa: N818 - public name used by callers
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------------------------
# Statistics


def derive_seed(base: int, *parts: Any) -> int:
    text = "|".join([str(base), *map(str, parts)])
    return int(hashlib.sha256(text.encode()).hexdigest()[:16], 16)


def mean(values: list[float]) -> float | None:
    return math.fsum(values) / len(values) if values else None


def quantile(ordered: list[float], q: float) -> float:
    """Linear interpolation between order statistics (Hyndman-Fan type 7)."""
    position = (len(ordered) - 1) * q
    low = math.floor(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def bootstrap(
    samples: list[list[float]],
    statistic: Callable[[list[float]], float],
    *,
    resamples: int,
    confidence: float,
    seed: int,
) -> list[float] | None:
    """Percentile interval of statistic(means of independently resampled samples).

    Draws use random.Random(seed).random(), whose sequence for an integer seed Python guarantees to be
    stable across versions; the index is floor(u * n), never randrange, whose algorithm may change.
    """
    if any(not s for s in samples):
        return None
    if all(min(s) == max(s) for s in samples):
        constant = statistic(
            [s[0] for s in samples]
        )  # every resample is identical: the interval is exact
        return [constant, constant]
    rng = random.Random(seed)
    draw = rng.random
    stats = []
    for _ in range(resamples):
        means = []
        for values in samples:
            n = len(values)
            means.append(math.fsum(values[int(draw() * n)] for _ in range(n)) / n)
        stats.append(statistic(means))
    stats.sort()
    alpha = (1 - confidence) / 2
    return [quantile(stats, alpha), quantile(stats, 1 - alpha)]


def clean(value: Any) -> Any:
    if isinstance(value, float):
        value = round(value, 12)
        return 0.0 if value == 0 else value
    if isinstance(value, list):
        return [clean(v) for v in value]
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    return value


# ---------------------------------------------------------------------------------------------
# Inputs


@dataclass
class Context:
    prereg: dict[str, dict[str, Any]]
    arms: dict[tuple[str, str], Arm]
    control: dict[str, Any]
    suite: dict[str, dict[str, Any]]
    pairs_by_task: dict[str, dict[str, Any]]
    tasks_dir: Path
    draft: bool
    predicates: dict[str, Callable] = field(default_factory=dict)

    @property
    def campaign(self) -> dict[str, Any]:
        return self.prereg["campaign"]

    @property
    def analysis(self) -> dict[str, Any]:
        return self.prereg["analysis"]

    def predicate(self, task: str) -> Callable:
        if task not in self.predicates:
            pair = self.pairs_by_task.get(task, {})
            path = (
                Path(pair["predicate"])
                if pair.get("predicate")
                else self.tasks_dir / task / "success.py"
            )
            if not path.is_absolute():
                path = REPO / path
            self.predicates[task] = load_predicate(path)
        return self.predicates[task]

    def suite_tasks(self) -> list[str]:
        return sorted(k.split(".", 1)[1] for k in self.suite if k.startswith("task."))

    def task_info(self, task: str) -> dict[str, Any]:
        return self.suite.get(f"task.{task}", {})

    def reuse_rule(self, task: str) -> dict[str, str] | None:
        """The reused rule of a rule-reuse task as {primitive, code}, from suite.kvx [task.<id>].reused_rule.

        Accepts the suite's rule identifier `<module>.<primitive>[<selector>]:<CODE>` or an object
        {"primitive", "code"}. A task with `rule_reuse = false` or no reused_rule is not a reuse task.
        """
        info = self.task_info(task)
        rule = info.get("reused_rule")
        if info.get("rule_reuse") is False or rule in (None, ""):
            return None
        if isinstance(rule, dict):
            return {"primitive": rule["primitive"], "code": rule["code"]}
        match = RULE_ID.fullmatch(str(rule))
        if not match or not match.group("code"):
            raise Refused(
                4, f"task {task}: reused_rule {rule!r} does not name a primitive and failure code"
            )
        return {"primitive": match.group("primitive"), "code": match.group("code")}


def load_pairs(path: Path) -> dict[str, dict[str, Any]]:
    """Map post-upgrade task id to {pair, changed_primitives, misuse_code, predicate?} from upgrades.kvx."""
    if not path.exists():
        return {}
    pairs = {}
    for section, body in read_kvx(path).items():
        if section.startswith("pair."):
            pair = section.split(".", 1)[1]
            pairs[body["task"]] = {
                "pair": pair,
                "changed_primitives": list(body.get("changed_primitives", [])),
                "misuse_code": body.get("misuse_code"),
                "predicate": body.get("predicate"),
            }
    return pairs


@dataclass
class Cell:
    hypothesis: str
    arm: Arm
    task: str
    seed: int
    path: Path
    attempt: dict[str, Any]
    ledger: list[dict[str, Any]]
    final_state: dict[str, Any]
    states: dict[int, Any] | None
    group: str
    values: dict[str, Any] = field(default_factory=dict)
    excluded: str | None = None


def _norm(stamp: Any) -> Any:
    return stamp.lstrip("-") if isinstance(stamp, str) else stamp


def read_cell(ctx: Context, hypothesis: str, arm: Arm, task: str, seed_dir: Path) -> Cell:
    try:
        seed = int(seed_dir.name)
    except ValueError:
        raise Refused(4, f"{seed_dir}: seed directory is not an integer") from None
    try:
        ledger = read_ledger(seed_dir / "ledger.jsonl")
        final_state = state_view(read_kvx(seed_dir / "final_state.kvx"))
        attempt = read_kvx(seed_dir / "attempt.kvx").get("attempt", {})
        states = None
        state_dir = seed_dir / "states"
        if state_dir.is_dir() and any(state_dir.iterdir()):
            states = {
                int(p.stem.split("_")[1]): state_view(read_kvx(p))
                for p in sorted(state_dir.glob("turn_*.kvx"))
            }
        metrics.validate_ledger(ledger)
    except (
        OSError,
        InvalidLedger,
        InvalidKVX,
        metrics.MetricInputError,
        ValueError,
        IndexError,
    ) as error:
        raise Refused(4, f"{seed_dir}: unreadable cell: {error}") from None
    if hypothesis == "H4":
        pair = ctx.pairs_by_task.get(task)
        if pair is None:
            raise Refused(4, f"{seed_dir}: task {task} is not a registered upgrade-pair task")
        group = pair["pair"]
    else:
        group = ctx.task_info(task).get("bin")
        if group not in BINS:
            raise Refused(4, f"{seed_dir}: task {task} has no registered bin in suite.kvx")
    return Cell(hypothesis, arm, task, seed, seed_dir, attempt, ledger, final_state, states, group)


def integrity_errors(ctx: Context, cell: Cell) -> list[str]:
    errors = []
    head = cell.ledger[0]
    where = str(cell.path.relative_to(cell.path.parents[3]))
    expected_arm = f"{cell.hypothesis}/{cell.arm.key}"
    if head.get("arm") != expected_arm:
        errors.append(f"{where}: header arm {head.get('arm')!r} is not {expected_arm!r}")
    if head.get("seed") != cell.seed:
        errors.append(f"{where}: header seed {head.get('seed')!r} is not {cell.seed}")
    if cell.seed not in ctx.campaign["seeds"]:
        errors.append(f"{where}: seed {cell.seed} is not registered")
    if head.get("tail_size") != cell.arm.tail:
        errors.append(
            f"{where}: header tail_size {head.get('tail_size')!r} is not {cell.arm.tail!r}"
        )
    model = ctx.campaign.get("model")
    if model and head.get("model") != model:
        errors.append(f"{where}: header model {head.get('model')!r} is not the registered model")
    build = resolve_build(cell.arm, ctx.control)
    if build and head.get("build") != build:
        errors.append(
            f"{where}: header build {head.get('build')} is not the registered Build {build}"
        )
    upgrade = next((r for r in cell.ledger if r["kind"] == "upgrade"), None)
    for record in cell.ledger:
        stamp = _norm(record.get("experimental"))
        if cell.arm.build_binding == "pair_b":
            expected = (
                None
                if upgrade is None or record["seq"] <= upgrade["seq"]
                else cell.arm.experimental_stamp
            )
        else:
            expected = cell.arm.experimental_stamp
        if stamp != expected:
            errors.append(
                f"{where}: seq {record['seq']} experimental stamp {stamp!r} is not {expected!r}"
            )
            break
    digest = head.get("cassette_digest", cell.attempt.get("cassette_digest"))
    # A hypothesis whose tasks are outside the suite cassette set (H4's upgrade-pair tasks) registers
    # its own set digest in [hypothesis.<H>].cassette_digest; every other hypothesis uses the campaign's.
    registered = ctx.prereg.get(f"hypothesis.{cell.hypothesis}", {}).get(
        "cassette_digest"
    ) or ctx.campaign.get("cassette_digest")
    if registered and digest != registered:
        errors.append(f"{where}: cassette digest {digest} is not the registered {registered}")
    return errors


def exclusion(ctx: Context, cell: Cell, mode: str) -> str | None:
    if cell.attempt.get("mode") != mode:
        return f"mode {cell.attempt.get('mode')!r} is not {mode!r}"
    if cell.attempt.get("registered") is not True and not ctx.draft:
        return "attempt not run under a complete registration"
    if metrics.has_invariant_violation(cell.ledger):
        return "INVARIANT_VIOLATION recorded: Build defect"
    if cell.attempt.get("exit_status") in EXCLUDED_EXITS:
        return f"attempt ended with {cell.attempt['exit_status']}"
    return None


# ---------------------------------------------------------------------------------------------
# Analysis


def summarise(values: list[Any], ctx: Context, *key: Any) -> dict[str, Any]:
    defined = [float(v) for v in values if v is not None]
    analysis = ctx.analysis
    return {
        "n": len(defined),
        "undefined": len(values) - len(defined),
        "mean": mean(defined),
        "ci": bootstrap(
            [defined],
            lambda m: m[0],
            resamples=analysis["resamples"],
            confidence=analysis["confidence"],
            seed=derive_seed(analysis["seed"], *key),
        ),
    }


def verdict(expect: str, ci: list[float] | None) -> str:
    if expect == "undetermined":
        return "reported_only"
    if ci is None:
        return "insufficient_data"
    low, high = ci
    if expect in ("a>b", "long>short"):
        return "consistent" if low > 0 else "inconsistent" if high < 0 else "inconclusive"
    if expect == "a<b":
        return "consistent" if high < 0 else "inconsistent" if low > 0 else "inconclusive"
    if expect == "a>=b":
        return "inconsistent" if high < 0 else "consistent"
    if expect == "a<=b":
        return "inconsistent" if low > 0 else "consistent"
    return "consistent" if low <= 0 <= high else "inconsistent"  # a=b


def analyse(ctx: Context, results: Path, mode: str = "replay") -> dict[str, Any]:
    hyps = {k.split(".", 1)[1]: v for k, v in ctx.prereg.items() if k.startswith("hypothesis.")}
    names = metrics.metric_names()
    excluded: list[dict[str, str]] = []
    errors: list[str] = []
    cells: dict[str, list[Cell]] = {}
    for hypothesis in sorted(hyps):
        cells[hypothesis] = []
        for (h, key), arm in sorted(ctx.arms.items()):
            if h != hypothesis:
                continue
            arm_dir = results / hypothesis.lower() / key
            if not arm_dir.is_dir():
                continue
            for task_dir in sorted(p for p in arm_dir.iterdir() if p.is_dir()):
                for seed_dir in sorted(
                    (p for p in task_dir.iterdir() if p.is_dir()), key=lambda p: p.name
                ):
                    cell = read_cell(ctx, hypothesis, arm, task_dir.name, seed_dir)
                    errors.extend(integrity_errors(ctx, cell))
                    cell.excluded = exclusion(ctx, cell, mode)
                    if cell.excluded:
                        excluded.append(
                            {
                                "cell": f"{hypothesis.lower()}/{key}/{cell.task}/{cell.seed}",
                                "reason": cell.excluded,
                            }
                        )
                        continue
                    pair = ctx.pairs_by_task.get(cell.task) if hypothesis == "H4" else None
                    cell.values = metrics.instance_metrics(
                        cell.ledger,
                        cell.final_state,
                        ctx.predicate(cell.task),
                        reuse_rule=ctx.reuse_rule(cell.task),
                        upgrade_pair=pair,
                        states_after_turn=cell.states,
                    )
                    cells[hypothesis].append(cell)
    if errors:
        raise Refused(4, "integrity failures:\n  " + "\n  ".join(errors))
    by_hypothesis = {
        h: sorted(
            {str(c.ledger[0].get("cassette_digest", c.attempt.get("cassette_digest"))) for c in cs}
        )
        for h, cs in cells.items()
    }
    for h, found in sorted(by_hypothesis.items()):
        if len(found) > 1:
            raise Refused(
                4,
                f"{h}: refusing to pool ledgers recorded under different cassette digests: {found}",
            )
    digests = sorted({d for found in by_hypothesis.values() for d in found})

    report_h: dict[str, Any] = {}
    table: dict[tuple[str, str, str], list[Cell]] = {}
    for hypothesis, body in sorted(hyps.items()):
        pool_exclude = set(body.get("pool_exclude", []))
        for cell in cells[hypothesis]:
            table.setdefault((hypothesis, cell.arm.key, cell.group), []).append(cell)
            if body["grouping"] == "pair" and cell.group not in pool_exclude:
                table.setdefault((hypothesis, cell.arm.key, "pooled"), []).append(cell)

    def values(hypothesis: str, arm: str, group: str, metric: str) -> list[float]:
        return [
            float(c.values[metric])
            for c in table.get((hypothesis, arm, group), [])
            if c.values.get(metric) is not None
        ]

    for hypothesis, body in sorted(hyps.items()):
        groups = (
            list(BINS)
            if body["grouping"] == "bin"
            else sorted(
                {c.group for c in cells[hypothesis]} | ({"pooled"} if cells[hypothesis] else set())
            )
        )
        out_cells: dict[str, Any] = {}
        tokens: dict[str, Any] = {}
        for arm in body["arms"]:
            out_cells[arm] = {}
            tokens[arm] = {}
            for group in groups:
                members = table.get((hypothesis, arm, group), [])
                out_cells[arm][group] = {
                    "instances": len(members),
                    "metrics": {
                        m: summarise(
                            [c.values.get(m) for c in members], ctx, hypothesis, arm, group, m
                        )
                        for m in names
                    },
                }
                methods = sorted({c.values["turns_to_success_method"] for c in members})
                out_cells[arm][group]["turns_to_success_method"] = methods
                tokens[arm][group] = summarise(
                    [
                        (c.attempt.get("tokens_input_total") or 0)
                        + (c.attempt.get("tokens_output_total") or 0)
                        for c in members
                    ],
                    ctx,
                    hypothesis,
                    arm,
                    group,
                    "harness_tokens",
                )
        per_spec: dict[str, Any] = {}
        for arm in body["arms"]:
            arm_obj = ctx.arms[(hypothesis, arm)]
            if arm_obj.flag != "--x-drift":
                continue
            specs: dict[str, list[Cell]] = {}
            for c in cells[hypothesis]:
                if c.arm.key == arm and c.attempt.get("drift_spec"):
                    specs.setdefault(c.attempt["drift_spec"], []).append(c)
            per_spec[arm] = {
                spec: {
                    "instances": len(cs),
                    "metrics": {
                        m: summarise(
                            [c.values.get(m) for c in cs], ctx, hypothesis, arm, "spec", spec, m
                        )
                        for m in [body["primary"], *body["secondary"]]
                    },
                }
                for spec, cs in sorted(specs.items())
            }
        directions = []
        for d in body["directions"]:
            source = d.get("source", hypothesis)
            metric_a, metric_b = (
                d.get("metric_a", d.get("metric")),
                d.get("metric_b", d.get("metric")),
            )
            analysis = ctx.analysis
            if d["expect"] == "long>short":
                samples = [
                    values(source, d["a"], "long", metric_a),
                    values(source, d["b"], "long", metric_b),
                    values(source, d["a"], "short", metric_a),
                    values(source, d["b"], "short", metric_b),
                ]
                means = [mean(s) for s in samples]
                estimate = None if None in means else (means[0] - means[1]) - (means[2] - means[3])
                ci = bootstrap(
                    samples,
                    lambda m: (m[0] - m[1]) - (m[2] - m[3]),
                    resamples=analysis["resamples"],
                    confidence=analysis["confidence"],
                    seed=derive_seed(analysis["seed"], d["id"]),
                )
                directions.append(
                    {
                        "id": d["id"],
                        "expect": d["expect"],
                        "group": "long-vs-short",
                        "estimate": estimate,
                        "ci": ci,
                        "label": verdict(d["expect"], ci),
                    }
                )
                continue
            d_groups = d.get("groups") or (
                [*BINS] if hyps[source]["grouping"] == "bin" else ["pooled"]
            )
            for group in d_groups:
                a, b = (
                    values(source, d["a"], group, metric_a),
                    values(source, d["b"], group, metric_b),
                )
                estimate = None if not a or not b else mean(a) - mean(b)
                ci = bootstrap(
                    [a, b],
                    lambda m: m[0] - m[1],
                    resamples=analysis["resamples"],
                    confidence=analysis["confidence"],
                    seed=derive_seed(analysis["seed"], d["id"], group),
                )
                directions.append(
                    {
                        "id": d["id"],
                        "expect": d["expect"],
                        "group": group,
                        "estimate": estimate,
                        "ci": ci,
                        "label": verdict(d["expect"], ci),
                    }
                )
        expected_tasks = (
            ctx.suite_tasks()
            if body["task_set"] == "suite:all"
            else sorted(ctx.pairs_by_task)
            if body["task_set"] == "upgrades:all"
            else []
        )
        present = {(c.arm.key, c.task, c.seed) for c in cells[hypothesis]}
        missing = [
            f"{hypothesis.lower()}/{arm}/{task}/{seed}"
            for arm in body["arms"]
            for task in expected_tasks
            for seed in ctx.campaign["seeds"]
            if (arm, task, seed) not in present
        ]
        report_h[hypothesis] = {
            "claim": body["claim"],
            "primary": body["primary"],
            "grouping": body["grouping"],
            "cells": out_cells,
            "per_drift_spec": per_spec,
            "instances": [
                {
                    "cell": f"{hypothesis.lower()}/{c.arm.key}/{c.task}/{c.seed}",
                    "group": c.group,
                    "values": {m: c.values.get(m) for m in [*names, "turns_to_success_method"]},
                }
                for c in sorted(cells[hypothesis], key=lambda c: (c.arm.key, c.task, c.seed))
            ],
            "directions": directions,
            "harness_recorded_tokens": {
                "label": "harness-recorded at the model boundary; not a ledger quantity",
                "cells": tokens,
            },
            "matrix": {
                "expected": len(expected_tasks) * len(ctx.campaign["seeds"]) * len(body["arms"]),
                "present": len(present),
                "missing_count": len(missing),
                "missing": missing[:200],
                "complete": not missing and bool(expected_tasks),
            },
        }
    return {
        "schema": SCHEMA,
        "mode": mode,
        "registration": "draft" if ctx.draft else "registered",
        "analysis": {k: ctx.analysis[k] for k in sorted(ctx.analysis)},
        "cassette_digests": digests,
        "excluded": excluded,
        "hypotheses": report_h,
        "checks_not_performed": [
            "Build hashes of per_task (drift) and pair_b arms are checked against "
            "benchmark/campaigns/h1.kvx and h4.kvx by the task 5.2 and 5.3 drivers"
        ],
    }


# ---------------------------------------------------------------------------------------------
# Command line


def build_context(args: argparse.Namespace) -> Context:
    try:
        prereg = load_prereg(args.prereg)
        check_digests(prereg, metrics_path=args.metrics, arms_path=args.arms)
    except DigestMismatch as error:
        raise Refused(3, str(error)) from None
    except (InvalidPreregistration, InvalidKVX, OSError) as error:
        raise Refused(2, f"pre-registration unusable: {error}") from None
    meta, arms = load_arms(args.arms)
    missing = missing_fields(prereg, arms, meta["control"])
    if missing and not args.draft_ok:
        raise Refused(2, "pre-registration is a draft; missing: " + ", ".join(missing))
    suite = read_kvx(args.suite) if Path(args.suite).exists() else {}
    declared = suite.get("meta", {}).get("suite_digest")
    registered = prereg["campaign"].get("suite_digest")
    if registered and declared and declared != registered:
        raise Refused(4, f"suite digest {declared} is not the registered {registered}")
    return Context(
        prereg=prereg,
        arms=arms,
        control=meta["control"],
        suite=suite,
        pairs_by_task=load_pairs(Path(args.upgrades)),
        tasks_dir=Path(args.tasks_dir),
        draft=bool(missing),
    )


def first_difference(a: Any, b: Any, path: str = "$") -> str | None:
    if type(a) is not type(b):
        return path
    if isinstance(a, dict):
        for key in sorted(set(a) | set(b)):
            if key not in a or key not in b:
                return f"{path}.{key}"
            found = first_difference(a[key], b[key], f"{path}.{key}")
            if found:
                return found
        return None
    if isinstance(a, list):
        if len(a) != len(b):
            return path
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            found = first_difference(x, y, f"{path}[{i}]")
            if found:
                return found
        return None
    return None if a == b else path


def main(argv: list[str] | None = None) -> int:
    bench = REPO / "benchmark"
    parser = argparse.ArgumentParser(
        description="Recompute CWR campaign metrics from ledgers and final state."
    )
    parser.add_argument("--results", default=str(bench / "results"))
    parser.add_argument("--prereg", default=str(bench / "preregistration.kvx"))
    parser.add_argument("--metrics", default=str(METRICS_PATH))
    parser.add_argument("--arms", default=str(ARMS_PATH))
    parser.add_argument("--suite", default=str(bench / "suite.kvx"))
    parser.add_argument("--upgrades", default=str(bench / "upgrades.kvx"))
    parser.add_argument("--tasks-dir", default=str(bench / "tasks"))
    parser.add_argument(
        "--out", help="report path (default <results>/analysis.json, or analysis-live.json)"
    )
    parser.add_argument(
        "--check", help="recompute and compare with an existing report; exit 1 on any difference"
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="analyse <results>/live separately; never pooled with replay",
    )
    parser.add_argument(
        "--draft-ok",
        action="store_true",
        help="allow a draft registration; the report is labelled draft",
    )
    parser.add_argument(
        "--require-complete",
        action="store_true",
        help="exit 5 when any arm matrix has a missing cell",
    )
    parser.add_argument(
        "--print-digests",
        action="store_true",
        help="print the current sha256 of metrics.py and arms.kvx",
    )
    args = parser.parse_args(argv)
    if args.print_digests:
        print(
            json.dumps(
                {"metrics_py": sha256_file(args.metrics), "arms_kvx": sha256_file(args.arms)},
                indent=2,
            )
        )
        return 0
    try:
        ctx = build_context(args)
        results = Path(args.results) / "live" if args.live else Path(args.results)
        report = clean(analyse(ctx, results, "live" if args.live else "replay"))
        report["inputs"] = {
            "preregistration": sha256_file(args.prereg),
            "metrics_py": sha256_file(args.metrics),
            "arms_kvx": sha256_file(args.arms),
            "suite_kvx": sha256_file(args.suite) if Path(args.suite).exists() else None,
            "upgrades_kvx": sha256_file(args.upgrades) if Path(args.upgrades).exists() else None,
        }
    except Refused as error:
        print(f"analyse.py refused: {error}", file=sys.stderr)
        return error.code
    text = json.dumps(report, sort_keys=True, indent=1, allow_nan=False) + "\n"
    if args.check:
        previous = json.loads(Path(args.check).read_text(encoding="utf-8"))
        diff = first_difference(previous, json.loads(text))
        if diff:
            print(f"report differs from recomputation at {diff}", file=sys.stderr)
            return 1
        print("report equals the recomputation from ledgers")
        return 0
    out = (
        Path(args.out)
        if args.out
        else Path(args.results) / ("analysis-live.json" if args.live else "analysis.json")
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    incomplete = [h for h, body in report["hypotheses"].items() if not body["matrix"]["complete"]]
    print(
        f"wrote {out}; excluded {len(report['excluded'])}; incomplete matrices: {', '.join(incomplete) or 'none'}"
    )
    if args.require_complete and incomplete:
        return 5
    return 0


if __name__ == "__main__":
    sys.exit(main())

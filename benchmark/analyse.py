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
     experimental stamp), a cell is unregistered, a ledger is malformed, replay ledgers with
     different cassette digests would be pooled, the task suite's recomputed digest is not the
     registered one, an attempt was run under other registration digests than the current ones,
     or a campaign file (benchmark/campaigns/h<N>.kvx) registered other digests than the current
     pre-registration
  5  --require-complete and the arm matrix has a missing cell
  1  --check FILE and the recomputed report differs from FILE

Excluded and reported (never pooled): live-mode attempts, unregistered attempts, Instances with an
INVARIANT_VIOLATION record, Instances with a record stamped language_guard=false (a turn run with
part of the sandbox switched off), and attempts that ended with a model or kernel error. Live runs under
benchmark/results/live are analysed only with --live, into a separately labelled report.

Inputs: benchmark/results/h<N>/<arm>/<task>/<seed>/{ledger.jsonl, final_state.kvx, attempt.kvx},
benchmark/preregistration.kvx, benchmark/harness/arms.kvx, benchmark/suite.kvx (bins, rule-reuse rules),
benchmark/upgrades.kvx (pair tasks, changed primitives, misuse codes), each task's success.py and the
campaign registrations benchmark/campaigns/h<N>.kvx: the per-task drift Builds of H1 (h1.kvx
[task.<id>].<arm>_build) and the Build A and per-arm Build B of each H4 pair (h4.kvx [pair.<id>]),
against which every per_task and pair_b ledger is checked.
Harness-recorded token counts are read from attempt records and reported only in a separately labelled
block. Every included Instance's recomputed values are listed under `instances`, so a report can be
compared cell by cell with the values the runner recorded in each attempt's [metrics] section. The
runner's [metrics] are never read here: every number is recomputed from the ledger and final state.

Turns to success (experiments.md section 4) replays state per turn from the ledger. For an Instance
whose final state satisfies its predicate, a fresh Instance is created through the real `cwr` CLI
from the ledger header (Build, seed, tail size, arm, model, cassette digest, initial state), the turn
messages the runner submitted (messages/turn_NNNN.md) are re-submitted in order with the gateway in
replay mode, every upgrade record is re-applied where the ledger has it, and after every action the
replayed ledger must equal the recorded ledger record for record, ignoring only the fields in
REPLAY_IGNORED (host fields, the Instance id, and the snapshot address and hash chain that depend
on it). The predicate is evaluated over the replayed state after each turn and the first true turn
is the value. A message is only a replay input: any message that would produce a different state
produces a different ledger, which is refused (exit 4) as an integrity failure, so no file other than
the ledger and final state can change a reported number. A cell without recorded messages, created
from a snapshot or holding a recovery record falls back to registered convention C7 (the stop rule),
and the method used is reported per Instance. Failed Instances need no replay: the value is undefined.
The output is deterministic: no timestamps, sorted keys, seeded resampling.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import hashlib
import json
import math
import random
import re
import sys
import tempfile
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
# Fields a faithful replay cannot reproduce. host_ts and duration_us are host measurements and instance
# is the new Instance's id. An upgrade record's snapshot is the content address of the kernel's
# pre-upgrade snapshot file, which embeds the Instance id, so it differs between the original and the
# replay Instance; the hash chain covers that field and therefore differs from the upgrade record on.
# Every other field of every record is still compared, which already pins what the chain pins.
REPLAY_IGNORED = ("host_ts", "duration_us", "instance", "snapshot", "chain")
MESSAGES_DIR = "messages"
LEDGER_REPLAY = "ledger_replay"
UNDEFINED_ON_FAILURE = "undefined_on_failure"
EMPTY_CASSETTE = (
    '{"format":"cwr.cassette","label":"analyse-replay","request_hash":"sha256-cwr-canonical-json-v1",'
    '"schema_version":1}\n'
)
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
    campaigns: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    digests: dict[str, str] = field(default_factory=dict)

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

    def drift_section(self, hypothesis: str, task: str) -> dict[str, Any] | None:
        """[task.<id>] of the hypothesis's campaign file (per-task drift Builds), if registered."""
        return self.campaigns.get(hypothesis, {}).get(f"task.{task}")

    def pair_section(self, hypothesis: str, task: str) -> dict[str, Any] | None:
        """The [pair.<id>] of the hypothesis's campaign file whose task is `task`, if registered."""
        for name, body in self.campaigns.get(hypothesis, {}).items():
            if name.startswith("pair.") and body.get("task") == task:
                return body
        return None

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
    """Map post-upgrade task id to {pair, changed_primitives, misuse_code, misuse_scope, predicate?}
    from upgrades.kvx; misuse_scope "not_computable" (U2, U5) makes the misuse rate None."""
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
                "misuse_scope": body.get("misuse_scope", "call"),
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
    group: str
    values: dict[str, Any] = field(default_factory=dict)
    excluded: str | None = None
    states: dict[int, Any] | None = None
    method: str = UNDEFINED_ON_FAILURE


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
    return Cell(hypothesis, arm, task, seed, seed_dir, attempt, ledger, final_state, group)


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
    upgrade = next((r for r in cell.ledger if r["kind"] == "upgrade"), None)
    errors.extend(build_errors(ctx, cell, where, upgrade))
    errors.extend(digest_errors(ctx, cell, where))
    for record in cell.ledger:
        stamp = _norm(record.get("experimental"))
        if cell.arm.build_binding == "pair_b":
            expected = (
                None
                if upgrade is None or record["seq"] < upgrade["seq"]
                else cell.arm.experimental_stamp
            )
        else:
            expected = cell.arm.experimental_stamp
        if stamp != expected:
            errors.append(
                f"{where}: seq {record['seq']} experimental stamp {stamp!r} is not {expected!r}"
            )
            break
    if cell.attempt.get("mode") != "replay":
        return errors  # a live run has no cassette set
    digest = head.get("cassette_digest", cell.attempt.get("cassette_digest"))
    recorded = cell.attempt.get("cassette_digest")
    if "cassette_digest" in head and recorded and recorded != head["cassette_digest"]:
        errors.append(
            f"{where}: ledger header cassette digest {head['cassette_digest']} is not the "
            f"attempt's {recorded}"
        )
    # A hypothesis whose tasks are outside the suite cassette set (H4's upgrade-pair tasks) registers
    # its own set digest in [hypothesis.<H>].cassette_digest; every other hypothesis uses the campaign's.
    registered = ctx.prereg.get(f"hypothesis.{cell.hypothesis}", {}).get(
        "cassette_digest"
    ) or ctx.campaign.get("cassette_digest")
    if registered and digest != registered:
        errors.append(f"{where}: cassette digest {digest} is not the registered {registered}")
    return errors


def build_errors(ctx: Context, cell: Cell, where: str, upgrade: dict[str, Any] | None) -> list[str]:
    """The ledger's Builds against the registration of the arm's build_binding.

    control and flag arms: the header Build is the one arms.kvx registers. per_task arms (H1 drift):
    the header Build is campaigns/h<N>.kvx [task.<id>].<arm>_build. pair_b arms (H4): the header
    Build is the pair's build_a and the upgrade record's new_build is build_b[<arm>].
    """
    head = cell.ledger[0]
    binding = cell.arm.build_binding
    if binding in ("control", "flag"):
        build = resolve_build(cell.arm, ctx.control)
        if build and head.get("build") != build:
            return [
                f"{where}: header build {head.get('build')} is not the registered Build {build}"
            ]
        return []
    campaign = f"benchmark/campaigns/{cell.hypothesis.lower()}.kvx"
    if binding == "per_task":
        section = ctx.drift_section(cell.hypothesis, cell.task)
        key = f"{cell.arm.key.replace('-', '_')}_build"
        build = (section or {}).get(key)
        if not build:
            return [f"{where}: {campaign} registers no [task.{cell.task}].{key}"]
        if head.get("build") != build:
            return [
                f"{where}: header build {head.get('build')} is not the registered Build {build}"
            ]
        return []
    pair = ctx.pair_section(cell.hypothesis, cell.task)
    if pair is None:
        return [f"{where}: {campaign} registers no pair for task {cell.task}"]
    errors = []
    build_a = pair.get("build_a")
    build_b = (pair.get("build_b") or {}).get(cell.arm.key)
    if not build_a or not build_b:
        return [f"{where}: {campaign} registers no build_a or build_b[{cell.arm.key}]"]
    if head.get("build") != build_a:
        errors.append(
            f"{where}: header build {head.get('build')} is not the registered Build A {build_a}"
        )
    if upgrade is not None and upgrade.get("new_build") != build_b:
        errors.append(
            f"{where}: upgrade to {upgrade.get('new_build')} is not the registered Build B "
            f"{build_b}"
        )
    return errors


DIGEST_STAMPS = ("preregistration", "metrics_py", "arms_kvx")


def digest_errors(ctx: Context, cell: Cell, where: str) -> list[str]:
    """An attempt stamped with registration digests must have run under the current ones."""
    stamped = cell.attempt.get("registration_digests")
    if stamped is None:
        return []
    if not isinstance(stamped, dict):
        return [f"{where}: attempt registration_digests is not an object"]
    return [
        f"{where}: attempt ran under {name} {stamped.get(name)}, not the current {ctx.digests[name]}"
        for name in DIGEST_STAMPS
        if name in ctx.digests and stamped.get(name) != ctx.digests[name]
    ]


CAMPAIGN_DIGESTS = {
    "metrics_py": ("metrics_py",),
    "arms_kvx": ("arms_kvx", "arms_kvx_at_registration"),
}


def campaign_errors(ctx: Context, hypothesis: str) -> list[str]:
    """campaigns/h<N>.kvx [registration] digests against the current pre-registration digests.

    A campaign registered under another metrics.py or arms.kvx is a different pre-registration;
    its attempts are never analysed under this one.
    """
    registration = ctx.campaigns.get(hypothesis, {}).get("registration")
    if registration is None:
        return []
    errors = []
    for name, keys in CAMPAIGN_DIGESTS.items():
        key = next((k for k in keys if k in registration), None)
        current = ctx.prereg["digests"].get(name)
        if key is not None and registration[key] != current:
            errors.append(
                f"benchmark/campaigns/{hypothesis.lower()}.kvx registered {name} "
                f"{registration[key]}, not the current registration's {current}"
            )
    return errors


def exclusion(ctx: Context, cell: Cell, mode: str) -> str | None:
    if cell.attempt.get("mode") != mode:
        return f"mode {cell.attempt.get('mode')!r} is not {mode!r}"
    if cell.attempt.get("registered") is not True and not ctx.draft:
        return "attempt not run under a complete registration"
    if metrics.has_invariant_violation(cell.ledger):
        return "INVARIANT_VIOLATION recorded: Build defect"
    if any(r.get("language_guard") is False for r in cell.ledger):
        return "a turn ran with the sandbox language guard switched off (operator diagnostic)"
    if cell.attempt.get("exit_status") in EXCLUDED_EXITS:
        return f"attempt ended with {cell.attempt['exit_status']}"
    return None


# ---------------------------------------------------------------------------------------------
# Turns to success: state per turn replayed from the ledger


class ReplayDivergenceError(Exception):
    """Re-submitting the recorded turn messages through the kernel does not reproduce the ledger."""


def record_divergence(original: list[dict[str, Any]], replayed: list[dict[str, Any]]) -> str | None:
    """The first difference between the replayed records and the recorded ledger, ignoring host fields.

    The replayed ledger must be a prefix of the recorded one: every replayed record equals the
    recorded record of the same seq in every field except those in REPLAY_IGNORED. Fields
    are compared in sorted order with the hash chain last, so the report names the field that moved.
    """
    if len(replayed) > len(original):
        return f"the replay wrote {len(replayed)} records; the ledger holds {len(original)}"
    for recorded, again in zip(original, replayed, strict=False):
        keys = sorted(
            (set(recorded) | set(again)) - set(REPLAY_IGNORED), key=lambda k: (k == "chain", k)
        )
        for key in keys:
            if recorded.get(key, "<absent>") != again.get(key, "<absent>"):
                return (
                    f"seq {recorded.get('seq')} field {key!r}: ledger "
                    f"{json.dumps(recorded.get(key, '<absent>'), sort_keys=True)[:120]}, replay "
                    f"{json.dumps(again.get(key, '<absent>'), sort_keys=True)[:120]}"
                )
    return None


def cli_kernel(binary: Path | None) -> Callable[[Path, list[Path]], Any]:
    """Factory of the real kernel binding (benchmark/harness/run.py CwrCliKernel) in replay mode."""

    def make(home: Path, roots: list[Path]) -> Any:
        from benchmark.harness.run import CwrCliKernel

        return CwrCliKernel(gateway_mode="replay", binary=binary, home=home, builds=roots)

    return make


@dataclass
class Replayer:
    """Replays an Instance turn by turn from its ledger and returns the state after each turn.

    make_kernel(home, build_roots) returns a KernelAdapter (benchmark/harness/run.py). The replay
    Instance lives in a temporary store that is deleted afterwards; nothing under the results tree
    is written.
    """

    make_kernel: Callable[[Path, list[Path]], Any]
    builds: list[Path]
    cassettes: Path | None
    tasks_dir: Path

    def _cassette(self, cell: Cell) -> Path | None:
        if self.cassettes is not None:
            path = self.cassettes / cell.task / f"seed-{cell.seed}.jsonl"
            return path if path.is_file() else None
        recorded = cell.attempt.get("cassette")
        return Path(recorded) if recorded and Path(recorded).is_file() else None

    def _initial(self, cell: Cell) -> Path | None:
        recorded = cell.attempt.get("initial_state")
        if recorded and Path(recorded).is_file():
            return Path(recorded)
        default = self.tasks_dir / cell.task / "initial.kvx"
        return default if default.is_file() else None

    def _roots(self, cell: Cell) -> list[Path]:
        candidates = [*self.builds, *map(Path, cell.attempt.get("build_roots") or [])]
        candidates += [REPO / "build", REPO / "xbuild"]
        roots: list[Path] = []
        for root in candidates:
            if root.is_dir() and root not in roots:
                roots.append(root)
        return roots

    def states(self, cell: Cell, predicate: Callable) -> tuple[dict[int, Any] | None, str]:
        """(state after each turn up to the first satisfying one, "ledger_replay"), or (None, the
        reason the C7 stop rule applies). Raises ReplayDivergenceError when the replay departs from the
        ledger or never satisfies the predicate that the final state satisfies."""
        ledger, head = cell.ledger, cell.ledger[0]
        messages = cell.path / MESSAGES_DIR
        if not messages.is_dir():
            return None, "stop_rule: no recorded turn messages"
        if head.get("created_from") is not None:
            return None, "stop_rule: the Instance was created from a snapshot"
        if any(r.get("kind") == "recovery" for r in ledger):
            return None, "stop_rule: the ledger records a kernel recovery"
        files = sorted(messages.glob("turn_*.md"))
        numbers = [p.stem.removeprefix("turn_") for p in files]
        if numbers != [f"{n:04d}" for n in range(1, len(files) + 1)]:
            raise ReplayDivergenceError(
                f"recorded messages are not turn_0001 to turn_{len(files):04d}"
            )
        cassette = self._cassette(cell)
        if cassette is None and any(r.get("kind") == "adapter" for r in ledger):
            return None, "stop_rule: adapter traffic and no cassette to replay it"
        with tempfile.TemporaryDirectory(prefix="cwr-analyse-replay-") as tmp:
            if cassette is None:
                cassette = Path(tmp) / "no-adapter-traffic.jsonl"
                cassette.write_text(EMPTY_CASSETTE, encoding="utf-8")
            kernel = self.make_kernel(Path(tmp) / "home", self._roots(cell))
            try:
                instance = kernel.create_instance(
                    build=head["build"],
                    seed=head["seed"],
                    tail=head["tail_size"],
                    arm=head["arm"],
                    model=head["model"],
                    allow_experimental=head.get("experimental") is not None,
                    initial_state=self._initial(cell),
                    cassette_digest=head.get("cassette_digest"),
                )
            except Exception as error:
                raise ReplayDivergenceError(
                    f"the replay Instance cannot be created: {error}"
                ) from None
            try:
                return self._turns(cell, kernel, instance, cassette, files, predicate)
            except ReplayDivergenceError:
                raise
            except Exception as error:
                raise ReplayDivergenceError(f"the kernel refused the replay: {error}") from None
            finally:
                with contextlib.suppress(Exception):  # the temporary store is removed anyway
                    kernel.destroy(instance)

    def _turns(
        self,
        cell: Cell,
        kernel: Any,
        instance: str,
        cassette: Path,
        files: list[Path],
        predicate: Callable,
    ) -> tuple[dict[int, Any], str]:
        ledger = cell.ledger
        kernel.bind_cassette(instance, cassette)

        def advance(before: int) -> list[dict[str, Any]]:
            replayed = kernel.ledger(instance)
            diff = record_divergence(ledger, replayed)
            if diff:
                raise ReplayDivergenceError(f"replay diverges from the ledger at {diff}")
            if len(replayed) < before:
                raise ReplayDivergenceError("the replay ledger lost records")
            return replayed

        replayed = advance(0)
        states: dict[int, Any] = {}
        for path in files:
            while len(replayed) < len(ledger) and ledger[len(replayed)].get("kind") == "upgrade":
                before = len(replayed)
                upgrade = ledger[before]
                kernel.upgrade(
                    instance, upgrade["new_build"], allow_experimental=cell.arm.experimental
                )
                replayed = advance(before)
                if len(replayed) == before:
                    raise ReplayDivergenceError(f"seq {before}: the upgrade wrote no record")
            before = len(replayed)
            kernel.context(instance)
            kernel.turn(instance, path.read_bytes().decode("utf-8"))
            replayed = advance(before)
            last = replayed[-1]
            if len(replayed) == before or last.get("kind") != "turn":
                continue
            state = state_view(kernel.state(instance))
            states[last["turn"]] = state
            if predicate(state, list(ledger[: len(replayed)])) is True:
                return states, LEDGER_REPLAY
        raise ReplayDivergenceError(
            "the final state satisfies the predicate but no replayed turn state does"
        )


def replay_cells(
    ctx: Context, replayer: Replayer | None, cells: list[Cell], jobs: int
) -> list[str]:
    """Fill cell.states and cell.method for every cell; returns integrity errors (divergences)."""
    todo = []
    for cell in cells:
        predicate = ctx.predicate(cell.task)
        try:
            satisfied = predicate(cell.final_state, list(cell.ledger)) is True
        except Exception as error:
            raise Refused(4, f"{cell.path}: success predicate failed: {error!r}") from None
        if not satisfied:
            cell.method = UNDEFINED_ON_FAILURE
        elif replayer is None:
            cell.method = "stop_rule: replay disabled"
        else:
            todo.append((cell, predicate))

    def one(item: tuple[Cell, Callable]) -> str | None:
        cell, predicate = item
        try:
            cell.states, cell.method = replayer.states(cell, predicate)
        except ReplayDivergenceError as error:
            where = str(cell.path.relative_to(cell.path.parents[3]))
            return f"{where}: turns to success cannot be replayed from the ledger: {error}"
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        outcomes = list(pool.map(one, todo))
    return [o for o in outcomes if o]


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
    """The registered label of a delta interval (preregistration.kvx [analysis].direction_labels).

    consistent when the interval lies wholly on the expected side, inconsistent when wholly on the
    other side, else inconclusive. For a>=b the expected side is [0, inf) and the other side
    (-inf, 0); a<=b mirrors it. a=b is consistent when the interval contains 0.
    """
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
        return "consistent" if low >= 0 else "inconsistent" if high < 0 else "inconclusive"
    if expect == "a<=b":
        return "consistent" if high <= 0 else "inconsistent" if low > 0 else "inconclusive"
    return "consistent" if low <= 0 <= high else "inconsistent"  # a=b


def analyse(
    ctx: Context,
    results: Path,
    mode: str = "replay",
    replayer: Replayer | None = None,
    jobs: int = 1,
) -> dict[str, Any]:
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
                    # A cell of the other gateway mode is excluded before anything else: it is
                    # never pooled, and its own report (--live or not) checks its integrity.
                    if cell.attempt.get("mode") == mode:
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
                    cells[hypothesis].append(cell)
    for hypothesis in sorted(h for h, cs in cells.items() if cs):
        errors.extend(campaign_errors(ctx, hypothesis))
    if errors:
        raise Refused(4, "integrity failures:\n  " + "\n  ".join(errors))
    included = [c for h in sorted(cells) for c in cells[h]]
    errors = replay_cells(ctx, replayer, included, jobs)
    if errors:
        raise Refused(4, "integrity failures:\n  " + "\n  ".join(errors))
    for cell in included:
        pair = ctx.pairs_by_task.get(cell.task) if cell.hypothesis == "H4" else None
        try:
            cell.values = metrics.instance_metrics(
                cell.ledger,
                cell.final_state,
                ctx.predicate(cell.task),
                reuse_rule=ctx.reuse_rule(cell.task),
                upgrade_pair=pair,
                states_after_turn=cell.states,
            )
        except (metrics.MetricInputError, ValueError, KeyError, TypeError) as error:
            raise Refused(4, f"{cell.path}: metrics cannot be computed: {error}") from None
        cell.values["turns_to_success_method"] = cell.method
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
            spec_key = f"{arm.replace('-', '_')}_spec"
            for c in cells[hypothesis]:
                if c.arm.key != arm:
                    continue
                # The registered spec of the task's drift Build, whose hash the header carries;
                # the attempt's own label only where no campaign file registers one.
                label = (ctx.drift_section(hypothesis, c.task) or {}).get(spec_key)
                label = label or c.attempt.get("drift_spec")
                if label:
                    specs.setdefault(label, []).append(c)
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
        "turns_to_success": {
            "method": "state per turn replayed from the ledger through the kernel; the predicate is "
            "evaluated after each turn and the first true turn is the value",
            "instances_by_method": {
                m: sum(1 for c in included if c.method == m)
                for m in sorted({c.method for c in included})
            },
        },
        "excluded": excluded,
        "hypotheses": report_h,
        "attempts_without_registration_digests": sum(
            1 for c in included if c.attempt.get("registration_digests") is None
        ),
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
    suite = check_suite(prereg, Path(args.suite), Path(args.tasks_dir))
    return Context(
        prereg=prereg,
        arms=arms,
        control=meta["control"],
        suite=suite,
        pairs_by_task=load_pairs(Path(args.upgrades)),
        tasks_dir=Path(args.tasks_dir),
        draft=bool(missing),
        campaigns=load_campaigns(Path(args.campaigns)),
        digests={
            "preregistration": sha256_file(args.prereg),
            "metrics_py": sha256_file(args.metrics),
            "arms_kvx": sha256_file(args.arms),
        },
    )


def recompute_suite_digest(tasks_dir: Path) -> str:
    """The suite digest over tasks_dir, computed by benchmark/predicates/common.py suite_digest
    (suite.kvx [suite].suite_digest_scope)."""
    predicates = str(REPO / "benchmark" / "predicates")
    if predicates not in sys.path:
        sys.path.insert(0, predicates)
    import common  # type: ignore[import-not-found]

    return common.suite_digest(tasks_dir)


def check_suite(prereg: dict[str, dict[str, Any]], suite_path: Path, tasks_dir: Path) -> dict:
    """The suite sections, after the recomputed task-suite digest equals the registered one.

    Refuses (exit 4) when a digest is registered and the suite file or the task directory is
    missing, or when the digest recomputed from the task files differs from the registration. The
    suite's own declared digest is never trusted: an edited task with a stale declaration fails.
    """
    registered = prereg["campaign"].get("suite_digest")
    if not suite_path.exists():
        if registered:
            raise Refused(4, f"suite digest {registered} is registered but {suite_path} is missing")
        return {}
    try:
        suite = read_kvx(suite_path)
    except InvalidKVX as error:
        raise Refused(4, f"suite unreadable: {error}") from None
    if registered:
        if not tasks_dir.is_dir():
            raise Refused(4, f"suite digest {registered} is registered but {tasks_dir} is missing")
        actual = recompute_suite_digest(tasks_dir)
        if actual != registered:
            raise Refused(
                4,
                f"suite digest {actual} recomputed from {tasks_dir} is not the registered {registered}",
            )
    return suite


def load_campaigns(root: Path) -> dict[str, dict[str, dict[str, Any]]]:
    """Every campaigns/h<N>.kvx present under root, keyed by hypothesis."""
    out = {}
    for hypothesis in ("H1", "H2", "H3", "H4"):
        path = root / f"{hypothesis.lower()}.kvx"
        if path.is_file():
            try:
                out[hypothesis] = read_kvx(path)
            except InvalidKVX as error:
                raise Refused(4, f"campaign registration unreadable: {error}") from None
    return out


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


def main(
    argv: list[str] | None = None,
    make_kernel: Callable[[Path, list[Path]], Any] | None = None,
) -> int:
    """Command line. make_kernel(home, build_roots) overrides the kernel binding used for replay."""
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
        "--campaigns",
        default=str(bench / "campaigns"),
        help="campaign registrations h<N>.kvx holding the per-task and per-pair Build hashes",
    )
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
        "--cwr",
        help="cwr binary used to replay turns (default $CWR_BIN, else <CARGO_TARGET_DIR or target>)",
    )
    parser.add_argument(
        "--builds",
        action="append",
        default=[],
        help="Build root to resolve ledger Build hashes in (repeatable); the roots each attempt "
        "recorded and build/, xbuild/ are searched after these",
    )
    parser.add_argument(
        "--cassettes",
        help="cassette set directory <task>/seed-<n>.jsonl for replay (default: each attempt's cassette)",
    )
    parser.add_argument(
        "--jobs", type=int, default=4, help="Instances replayed in parallel (default 4)"
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
        replayer = Replayer(
            make_kernel=make_kernel or cli_kernel(Path(args.cwr) if args.cwr else None),
            builds=[Path(b) for b in args.builds],
            cassettes=Path(args.cassettes) if args.cassettes else None,
            tasks_dir=Path(args.tasks_dir),
        )
        report = clean(
            analyse(ctx, results, "live" if args.live else "replay", replayer, args.jobs)
        )
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

# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Ledger-only metrics for the CWR hypotheses H1 to H4 (spec/cwr/experiments.md section 4).

This file is the normative implementation of the metric definitions. Its sha256 is pre-registered in
benchmark/preregistration.kvx and benchmark/analyse.py refuses to run when the digest differs, so any
edit here after a campaign has executed is a new pre-registration and a new campaign.

Inputs are only (a) the Instance ledger as a list of record dicts in seq order (kernel.md section 8),
(b) the final Instance state and, for turns to success, the state after each turn, and (c) the task's
pure success predicate, which itself reads only state and ledger. Nothing here reads kernel
internals, sandbox stdout, raw adapter payloads, model text or harness-recorded token counts.
The module is self-contained (standard library only, no I/O) so that its digest pins every definition.

Registered conventions where experiments.md leaves a choice open (also stated in preregistration.kvx):
  C1  "Primitive calls" are ledger records of kind primitive or adapter (CALL_KINDS). experiments.md
      says "kind primitive", but its failure-rate and probing-ratio definitions count adapter-only codes
      (RATE_LIMITED, TIMEOUT, DUPLICATE_KEY) and the external side-effect class, which only adapter
      records carry; kernel.md logs adapter calls as kind adapter. Turn, drift, proposal, header,
      upgrade and recovery records are never calls.
  C2  Failure-based metrics count only call records; a turn record's status is never a failure record.
  C3  A rate whose denominator is zero is undefined (None) and is excluded from that metric's sample.
  C4  Rule reuse on an Instance that never has an ok record of the reused primitive, or never reaches a
      second encounter ten call records later, scores 0: reuse was not demonstrated.
  C5  "Last quarter" is the call-record indices k with 4*k >= 3*N, N the Instance's call-record count.
  C6  A changed-primitive entry without a dot names a whole module (U5's "kv" state-shape change).
  C7  Turns to success uses the per-turn states when supplied. Otherwise it relies on the runner's
      registered stop rule (stop after the first turn whose predicate holds), so the final turn of a
      successful Instance is its first satisfying turn; the method used is reported by analyse.py.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

METRICS_SCHEMA = "cwr.metrics/1"

ENVELOPE = (
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
)
KINDS = ("header", "primitive", "adapter", "turn", "drift", "proposal", "upgrade", "recovery")
CALL_KINDS = ("primitive", "adapter")
TURN_STATUSES = ("ok", "error", "partial")
READ_ONLY = "read_only"
ACTING = ("mutating", "external")
REUSE_GAP = 10

Record = Mapping[str, Any]
Ledger = Sequence[Record]
Predicate = Callable[[Any, list], bool]

PRIMARY_METRICS = (
    "task_success",
    "turns_to_success",
    "primitive_calls",
    "failure_rate_by_code",
    "repeated_failure_rate",
    "rule_reuse",
    "post_upgrade_misuse_rate",
)
SECONDARY_METRICS = (
    "partial_rate",
    "probing_ratio",
    "turns_to_first_correct_use",
    "proposals_after_upgrade",
    "drift_records",
)


class MetricInputError(ValueError):
    """The ledger or state does not satisfy the schema the definitions assume."""


# ---------------------------------------------------------------------------------------------
# Ledger structure


def validate_ledger(ledger: Ledger) -> None:
    if not ledger:
        raise MetricInputError("empty ledger")
    for index, record in enumerate(ledger):
        if record.get("seq") != index:
            raise MetricInputError(f"record {index}: seq {record.get('seq')!r} is not {index}")
        kind = record.get("kind")
        if kind not in KINDS:
            raise MetricInputError(f"seq {index}: unknown kind {kind!r}")
        if (index == 0) != (kind == "header"):
            raise MetricInputError(f"seq {index}: the header must be exactly record 0")
        if not isinstance(record.get("turn"), int):
            raise MetricInputError(f"seq {index}: turn is not an integer")
        if kind in CALL_KINDS:
            if not isinstance(record.get("primitive"), str) or not record["primitive"]:
                raise MetricInputError(f"seq {index}: call record without primitive")
            outcome = record.get("outcome")
            if not isinstance(outcome, Mapping) or outcome.get("status") not in ("ok", "error"):
                raise MetricInputError(f"seq {index}: call outcome status must be ok or error")
            code = outcome.get("code")
            if outcome["status"] == "ok" and code is not None:
                raise MetricInputError(f"seq {index}: ok outcome with code {code!r}")
            if outcome["status"] == "error" and code not in ENVELOPE:
                raise MetricInputError(f"seq {index}: error code {code!r} outside the envelope")
        if kind == "turn" and turn_status(record) not in TURN_STATUSES:
            raise MetricInputError(
                f"seq {index}: turn status {turn_status(record)!r} is not ok, error or partial"
            )


def header(ledger: Ledger) -> Record:
    validate_ledger(ledger)
    return ledger[0]


def turn_status(record: Record) -> Any:
    if "status" in record:
        return record["status"]
    outcome = record.get("outcome")
    return outcome.get("status") if isinstance(outcome, Mapping) else None


def call_records(ledger: Ledger) -> list[Record]:
    return [r for r in ledger if r.get("kind") in CALL_KINDS]


def turn_records(ledger: Ledger) -> list[Record]:
    return [r for r in ledger if r.get("kind") == "turn"]


def is_failure(record: Record) -> bool:
    return record.get("kind") in CALL_KINDS and record["outcome"]["status"] == "error"


def has_invariant_violation(ledger: Ledger) -> bool:
    """An Instance containing INVARIANT_VIOLATION is a Build defect and is excluded (kernel.md section 4)."""
    return any(is_failure(r) and r["outcome"]["code"] == "INVARIANT_VIOLATION" for r in ledger)


def _ratio(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else numerator / denominator


def _matches(primitive: str, targets: Iterable[str]) -> bool:
    for target in targets:
        if "." in target:
            if primitive == target:
                return True
        elif primitive.split(".", 1)[0] == target:
            return True
    return False


def _first_upgrade(ledger: Ledger) -> Record | None:
    return next((r for r in ledger if r.get("kind") == "upgrade"), None)


# ---------------------------------------------------------------------------------------------
# Primary metrics (requirement 12.4)


def task_success(final_state: Any, ledger: Ledger, predicate: Predicate) -> int:
    """success.py evaluated once over the final state and the full ledger: 1 or 0."""
    validate_ledger(ledger)
    return 1 if predicate(final_state, list(ledger)) is True else 0


def turns_to_success(
    ledger: Ledger,
    predicate: Predicate,
    *,
    final_state: Any = None,
    states_after_turn: Mapping[int, Any] | None = None,
) -> int | None:
    """Turn number of the first turn after which the predicate holds; None on failure (never a large number)."""
    validate_ledger(ledger)
    turns = [(i, r) for i, r in enumerate(ledger) if r.get("kind") == "turn"]
    if states_after_turn is not None:
        for index, record in turns:
            number = record["turn"]
            if number not in states_after_turn:
                raise MetricInputError(f"no state recorded after turn {number}")
            if predicate(states_after_turn[number], list(ledger[: index + 1])) is True:
                return number
        return None
    if predicate(final_state, list(ledger)) is not True:
        return None
    return turns[-1][1]["turn"] if turns else 0


def primitive_calls(ledger: Ledger) -> int:
    validate_ledger(ledger)
    return len(call_records(ledger))


def failure_rate_by_code(ledger: Ledger) -> dict[str, float | None]:
    """Per envelope code: failing call records with that code divided by primitive calls."""
    validate_ledger(ledger)
    calls = call_records(ledger)
    counts = {code: 0 for code in ENVELOPE}
    for record in calls:
        if record["outcome"]["status"] == "error":
            counts[record["outcome"]["code"]] += 1
    return {code: _ratio(counts[code], len(calls)) for code in ENVELOPE}


def repeated_failure_rate(ledger: Ledger) -> float | None:
    """Failing records repeating an earlier failure of the same primitive with the same code."""
    validate_ledger(ledger)
    seen: set[tuple[str, str]] = set()
    failing = repeated = 0
    for record in ledger:
        if not is_failure(record):
            continue
        failing += 1
        key = (record["primitive"], record["outcome"]["code"])
        if key in seen:
            repeated += 1
        seen.add(key)
    return _ratio(repeated, failing)


def rule_reuse(ledger: Ledger, primitive: str, code: str) -> int:
    """1 when the second encounter of the reused rule, and every later encounter in the last quarter, has no failure with the rule's code."""
    validate_ledger(ledger)
    if code not in ENVELOPE:
        raise MetricInputError(f"reused rule code {code!r} outside the envelope")
    calls = call_records(ledger)
    total = len(calls)
    first_ok = next(
        (
            i
            for i, r in enumerate(calls)
            if r["primitive"] == primitive and r["outcome"]["status"] == "ok"
        ),
        None,
    )
    if first_ok is None:
        return 0
    second = next(
        (i for i in range(first_ok + REUSE_GAP, total) if calls[i]["primitive"] == primitive), None
    )
    if second is None:
        return 0
    checked = [second] + [
        k
        for k in range(second + 1, total)
        if calls[k]["primitive"] == primitive and 4 * k >= 3 * total
    ]
    for k in checked:
        if calls[k]["outcome"]["status"] == "error" and calls[k]["outcome"]["code"] == code:
            return 0
    return 1


def post_upgrade_misuse_rate(
    ledger: Ledger, changed: Sequence[str], misuse_code: str | None
) -> float | None:
    """Post-upgrade turns containing a changed primitive failing with the new rule's code, divided by post-upgrade turns."""
    validate_ledger(ledger)
    if misuse_code is not None and misuse_code not in ENVELOPE:
        raise MetricInputError(f"misuse code {misuse_code!r} outside the envelope")
    upgrade = _first_upgrade(ledger)
    if upgrade is None:
        return None
    after = [r for r in ledger if r["seq"] > upgrade["seq"]]
    post_turns = [r["turn"] for r in after if r.get("kind") == "turn"]
    if not post_turns:
        return None
    misuse_turns = set()
    if misuse_code is not None:
        for record in after:
            if (
                is_failure(record)
                and record["outcome"]["code"] == misuse_code
                and _matches(record["primitive"], changed)
            ):
                misuse_turns.add(record["turn"])
    return _ratio(sum(1 for t in post_turns if t in misuse_turns), len(post_turns))


# ---------------------------------------------------------------------------------------------
# Secondary metrics (experiments.md sections 3 and 4)


def partial_rate(ledger: Ledger) -> float | None:
    validate_ledger(ledger)
    turns = turn_records(ledger)
    return _ratio(sum(1 for r in turns if turn_status(r) == "partial"), len(turns))


def probing_ratio(ledger: Ledger) -> float | None:
    """Read-only calls per mutating or external call; a secondary H1 proxy only."""
    validate_ledger(ledger)
    calls = call_records(ledger)
    reads = sum(1 for r in calls if r["outcome"].get("side_effect") == READ_ONLY)
    acts = sum(1 for r in calls if r["outcome"].get("side_effect") in ACTING)
    return _ratio(reads, acts)


def turns_to_first_correct_use(ledger: Ledger, changed: Sequence[str]) -> int | None:
    """Turns from the upgrade to the first ok call of a changed primitive; None if never or no upgrade."""
    validate_ledger(ledger)
    upgrade = _first_upgrade(ledger)
    if upgrade is None:
        return None
    for record in ledger:
        if (
            record["seq"] > upgrade["seq"]
            and record.get("kind") in CALL_KINDS
            and record["outcome"]["status"] == "ok"
            and _matches(record["primitive"], changed)
        ):
            return record["turn"] - upgrade["turn"]
    return None


def proposals_after_upgrade(ledger: Ledger) -> int | None:
    validate_ledger(ledger)
    upgrade = _first_upgrade(ledger)
    if upgrade is None:
        return None
    return sum(1 for r in ledger if r["seq"] > upgrade["seq"] and r.get("kind") == "proposal")


def drift_records(ledger: Ledger) -> int:
    validate_ledger(ledger)
    return sum(1 for r in ledger if r.get("kind") == "drift")


# ---------------------------------------------------------------------------------------------
# One Instance, every metric


def instance_metrics(
    ledger: Ledger,
    final_state: Any,
    predicate: Predicate,
    *,
    reuse_rule: Mapping[str, str] | None = None,
    upgrade_pair: Mapping[str, Any] | None = None,
    states_after_turn: Mapping[int, Any] | None = None,
) -> dict[str, Any]:
    """Every metric for one Instance. Values are None where undefined or not applicable.

    reuse_rule: {"primitive": ..., "code": ...} for rule-reuse tasks, else None.
    upgrade_pair: {"changed_primitives": [...], "misuse_code": code or None} for H4 tasks, else None.
    """
    validate_ledger(ledger)
    out: dict[str, Any] = {
        "task_success": task_success(final_state, ledger, predicate),
        "turns_to_success": turns_to_success(
            ledger, predicate, final_state=final_state, states_after_turn=states_after_turn
        ),
        "turns_to_success_method": "per_turn_state"
        if states_after_turn is not None
        else "stop_rule",
        "primitive_calls": primitive_calls(ledger),
        "repeated_failure_rate": repeated_failure_rate(ledger),
        "partial_rate": partial_rate(ledger),
        "probing_ratio": probing_ratio(ledger),
        "drift_records": drift_records(ledger),
        "rule_reuse": None,
        "post_upgrade_misuse_rate": None,
        "turns_to_first_correct_use": None,
        "proposals_after_upgrade": None,
    }
    for code, rate in failure_rate_by_code(ledger).items():
        out[f"failure_rate.{code}"] = rate
    if reuse_rule is not None:
        out["rule_reuse"] = rule_reuse(ledger, reuse_rule["primitive"], reuse_rule["code"])
    if upgrade_pair is not None:
        changed = list(upgrade_pair["changed_primitives"])
        out["post_upgrade_misuse_rate"] = post_upgrade_misuse_rate(
            ledger, changed, upgrade_pair.get("misuse_code")
        )
        out["turns_to_first_correct_use"] = turns_to_first_correct_use(ledger, changed)
        out["proposals_after_upgrade"] = proposals_after_upgrade(ledger)
    return out


def metric_names() -> list[str]:
    """Every scalar key instance_metrics can produce, in report order."""
    names = [
        "task_success",
        "turns_to_success",
        "primitive_calls",
        "repeated_failure_rate",
        "rule_reuse",
        "post_upgrade_misuse_rate",
        "partial_rate",
        "probing_ratio",
        "turns_to_first_correct_use",
        "proposals_after_upgrade",
        "drift_records",
    ]
    names[4:4] = [f"failure_rate.{code}" for code in ENVELOPE]
    return names

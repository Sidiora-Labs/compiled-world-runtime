#!/usr/bin/env python3
# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Validate the Compiled World Runtime specification pack without executing product code.

This checks authoring structure, references, coverage, illustrative examples and local links.
It does not compile a world, run a kernel, call a model or execute a verification driver.
--render updates only the derived implementation plan and traceability views.
No third-party packages are used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

PACK = Path(__file__).resolve().parents[1]
REPO = PACK.parents[1]
FEATURE = "cwr"
REQUIREMENTS = range(1, 15)
CASES = [f"CWR-{n:03d}" for n in range(1, 21)]
CONTRACTS = {
    1: "design.body.md",
    2: "world-source.md",
    3: "world-source.md",
    4: "compiler.md",
    5: "kernel.md",
    6: "kernel.md",
    7: "kernel.md",
    8: "kernel.md",
    9: "model-surface.md",
    10: "compiler.md",
    11: "reference-world.md",
    12: "experiments.md",
    13: "verification.md",
    14: "README.md",
}
DERIVED = {"implementation-plan.md", "traceability.md"}
REPORT = "authoring-validation.json"
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
SIDE_EFFECTS = {"read_only", "mutating", "external"}
IDEMPOTENCY = {"none", "natural", "key"}
CHANGE_KINDS = {
    "primitive_added",
    "primitive_removed",
    "precondition_added",
    "precondition_removed",
    "precondition_changed",
    "effect_changed",
    "failure_changed",
    "limit_changed",
    "state_shape",
    "adapter_changed",
    "doc_only",
}
LIMITS = ["cpu_ms", "wall_ms", "memory_mb", "primitive_calls", "adapter_calls"]
TEXT_SUFFIXES = {".md", ".kvx", ".mmd", ".py"}
# Implementation stage only: case outcomes and product evidence must be backed by the qualification
# registry (task 0.2) and by attempt records the independent checker accepts. Nothing here runs a
# driver; the checker is loaded only when a non-UNRUN status or a product-evidence claim needs it.
QUALIFICATION_DIR = "tests/cwr"
REGISTRY = "tests/cwr/registry.kvx"
OUTCOMES = {"PASS", "FAIL", "BLOCKED"}


class InvalidKVX(ValueError):  # noqa: N818 - public name caught by core/tests/test_kvx.py
    """A KVX document breaks the authoring grammar; the message names the offending line."""


def strict_json(text: str):
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON object key {key}")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError(f"non-JSON numeric constant {value}")

    return json.loads(text, object_pairs_hook=unique_pairs, parse_constant=invalid_constant)


def parse_kvx(text: str) -> dict:
    """Parse the deliberately simple one-JSON-value-per-line section format."""
    sections: dict = {}
    current = None
    for number, original in enumerate(text.splitlines(), 1):
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        section = re.fullmatch(r"\[([A-Za-z0-9_.-]+)\]", line)
        if section:
            name = section.group(1)
            if name in sections:
                raise InvalidKVX(f"line {number}: duplicate section [{name}]")
            sections[name] = {}
            current = sections[name]
            continue
        assignment = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_-]*)\s*=\s*(.+)", line)
        if not assignment or current is None:
            raise InvalidKVX(f"line {number}: unsupported or out-of-section syntax")
        key, value = assignment.groups()
        if key in current:
            raise InvalidKVX(f"line {number}: duplicate key {key}")
        try:
            current[key] = strict_json(value)
        except ValueError as error:
            raise InvalidKVX(
                f"line {number}: value must be a single JSON literal: {error}"
            ) from error
    return sections


def numeric(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def string_list(value) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and item for item in value)


def task_order(tasks: dict) -> list[str]:
    ordered: list[str] = []
    remaining = set(tasks)
    while remaining:
        ready = sorted(
            (ident for ident in remaining if set(tasks[ident]["requires"]) <= set(ordered)),
            key=numeric,
        )
        if not ready:
            raise ValueError("Task graph cannot be ordered")
        ordered.append(ready[0])
        remaining.remove(ready[0])
    return ordered


def source_digest(requirements: dict, tasks: dict, cases: dict) -> str:
    """Digest of every requirement, clause, task and case, so any edit to them stales both views."""
    canonical = json.dumps(
        {"requirements": requirements, "tasks": tasks, "cases": cases},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def task_plan(tasks: dict, digest: str) -> str:
    lines = [
        "# CWR implementation plan",
        "",
        "Generated from [spec.kvx](spec.kvx); regenerate with "
        "`python3 spec/cwr/tools/validate_spec.py --render`.",
        "",
        f"Source digest (requirements, clauses, tasks and cases): `{digest}`.",
        "",
        "Task statuses below record authoring state. Verification drivers are task deliverables; "
        "a command being listed is not evidence that it exists or passes. No compiler, kernel, "
        "model or campaign has run.",
        "",
        "Dependencies determine eligibility. Wave numbers group integration obligations: contracts "
        "and evidence (0), compiler (1), kernel (2), model surface and ablation builds (3), reference "
        "world (4), experiments (5), release (6). Each package couples its implementation with a direct "
        "consumer and positive and negative checks. Component success does not qualify a complete case "
        "or a hypothesis.",
        "",
        "| Task | Wave | Scope | Requires | Owner |",
        "|---|---:|---|---|---|",
    ]
    for ident in task_order(tasks):
        task = tasks[ident]
        lines.append(
            f"| {ident} | {task['wave']} | {task['title']} | "
            f"{', '.join(task['requires']) or 'None'} | {task['owner']} |"
        )
    for ident in task_order(tasks):
        task = tasks[ident]
        lines += [
            "",
            f"## {ident}: {task['title']}",
            "",
            f"Status: **{task['status']}**. Owner: `{task['owner']}`. "
            f"Dependencies: {', '.join(task['requires']) or 'none'}.",
            "",
            "Proposed implementation scope:",
            "",
        ]
        lines += [f"- `{path}`" for path in task["touches"]]
        lines += [
            "",
            f"Future focused gate: `{task['verify_cmd']}`.",
            "",
            "Acceptance clauses: " + ", ".join(task["reqs"]) + ".",
            "Canonical cases: " + ", ".join(task["cases"]) + " (UNRUN).",
            "",
            "Implementation and qualification obligations:",
            "",
        ]
        steps = sorted(
            (key for key in task if re.fullmatch(r"do_\d+", key)),
            key=lambda key: int(key.split("_")[1]),
        )
        lines += [f"{index}. {task[key]}" for index, key in enumerate(steps, 1)]
    return "\n".join(lines) + "\n"


def traceability(requirements: dict, tasks: dict, cases: dict, digest: str) -> str:
    lines = [
        "# CWR traceability",
        "",
        "Generated from [spec.kvx](spec.kvx). A clause `N.M` is `[req.N] ac_M`. "
        "Mappings describe coverage ownership, not execution evidence. All cases are UNRUN.",
        "",
        f"Source digest (requirements, clauses, tasks and cases): `{digest}`.",
        "",
        "| Clause | Requirement | Implementation tasks | Cases | Contract |",
        "|---|---|---|---|---|",
    ]
    for number in sorted(requirements, key=int):
        req = requirements[number]
        for field in sorted(
            (key for key in req if re.fullmatch(r"ac_\d+", key)),
            key=lambda key: int(key.split("_")[1]),
        ):
            clause = number + "." + field.split("_")[1]
            owners = [key for key in sorted(tasks, key=numeric) if clause in tasks[key]["reqs"]]
            checks = [key for key in sorted(cases) if clause in cases[key]["reqs"]]
            contract = CONTRACTS[int(number)]
            lines.append(
                f"| {clause} | {req['title']} | {', '.join(owners)} | "
                f"{', '.join(checks)} | [{contract}]({contract}) |"
            )
    lines += [
        "",
        "## Case ownership",
        "",
        "A case's tasks are the packages responsible for its driver, behaviour or final "
        "integration. Full-case completion requires every registered assertion described in "
        "[verification.md](verification.md).",
        "",
        "| Case | Scope | Tasks | Independent oracle |",
        "|---|---|---|---|",
    ]
    for ident, case in sorted(cases.items()):
        lines.append(
            f"| {ident} | {case['title']} ({case['tier']}) | "
            f"{', '.join(case['tasks'])} | {case['oracle']} |"
        )
    return "\n".join(lines) + "\n"


def validate(sections: dict) -> tuple[list[str], dict, dict, dict]:
    errors: list[str] = []
    requirements = {key[4:]: value for key, value in sections.items() if key.startswith("req.")}
    tasks = {key[5:]: value for key, value in sections.items() if key.startswith("task.")}
    cases = {key[5:]: value for key, value in sections.items() if key.startswith("case.")}
    meta = sections.get("meta", {})
    stage = meta.get("stage")
    if meta.get("feature") != FEATURE or (meta.get("status"), stage) not in {
        ("pending", "design"),
        ("in_progress", "implementation"),
    }:
        errors.append("Expected design/pending or implementation/in_progress feature state")
    for field in ("name", "version", "review_date", "intro"):
        if not isinstance(meta.get(field), str) or not meta[field]:
            errors.append(f"meta: missing {field}")
    decisions = sections.get("decisions", {})
    numbers = sorted(int(key[2:]) for key in decisions if re.fullmatch(r"d_\d+", key))
    if not numbers or numbers != list(range(1, len(numbers) + 1)):
        errors.append("decisions must be d_1..d_N consecutive")
    if not any(
        "external baseline" in str(value).lower() or "other agent" in str(value).lower()
        for value in decisions.values()
    ):
        errors.append("decisions must record the no-external-baseline rule")
    if set(requirements) != {str(n) for n in REQUIREMENTS}:
        errors.append(f"Expected exactly requirement sections 1 through {REQUIREMENTS[-1]}")
    if set(cases) != set(CASES):
        errors.append(f"Expected exactly cases {CASES[0]} through {CASES[-1]}")
    clauses: set[str] = set()
    for ident, req in requirements.items():
        for field in ("title", "story"):
            if not isinstance(req.get(field), str) or not req[field]:
                errors.append(f"req {ident}: missing {field}")
        acs = sorted(int(key[3:]) for key in req if re.fullmatch(r"ac_\d+", key))
        if not acs or acs != list(range(1, len(acs) + 1)):
            errors.append(f"req {ident}: acceptance clauses must be consecutive from 1")
        for n in acs:
            clauses.add(f"{ident}.{n}")
            if not isinstance(req[f"ac_{n}"], str) or "SHALL" not in req[f"ac_{n}"]:
                errors.append(f"req {ident}.{n}: missing normative acceptance statement")
        extra = set(req) - {"title", "story"} - {f"ac_{n}" for n in acs}
        if extra:
            errors.append(f"req {ident}: unknown keys {sorted(extra)}")
    task_coverage: set[str] = set()
    for ident, task in tasks.items():
        if not re.fullmatch(r"\d+\.\d+", ident):
            errors.append(f"invalid task ID {ident}")
            continue
        if task.get("status") not in {"pending", "in_progress", "implemented", "done", "blocked"}:
            errors.append(f"task {ident}: unknown implementation status")
        if stage == "design" and task.get("status") != "pending":
            errors.append(f"task {ident}: design-stage tasks must remain pending")
        if task.get("status") in {"implemented", "done"}:
            driver = REPO / ("tests/cwr/cases/" + ident.replace(".", "_") + ".py")
            if not driver.is_file():
                errors.append(f"task {ident}: implemented task lacks verification driver")
        if type(task.get("wave")) is not int or task["wave"] != int(ident.split(".")[0]):
            errors.append(f"task {ident}: wave must match ID prefix")
        for field in ("title", "owner"):
            if not isinstance(task.get(field), str) or not task[field]:
                errors.append(f"task {ident}: missing {field}")
        for field in ("requires", "touches", "reqs", "cases"):
            values = task.get(field)
            if not string_list(values) or (field != "requires" and not values):
                errors.append(f"task {ident}: invalid {field}")
            elif len(values) != len(set(values)):
                errors.append(f"task {ident}: duplicate {field}")
        if not all(
            isinstance(value, str) and value for key, value in task.items() if key.startswith("do_")
        ):
            errors.append(f"task {ident}: invalid implementation step")
        steps = sorted(int(key[3:]) for key in task if re.fullmatch(r"do_\d+", key))
        if len(steps) < 4 or steps != list(range(1, len(steps) + 1)):
            errors.append(f"task {ident}: need at least four consecutive implementation/test steps")
        expected = ["python3", "tests/cwr/cases/" + ident.replace(".", "_") + ".py"]
        try:
            actual = shlex.split(task.get("verify_cmd", ""))
        except (TypeError, ValueError):
            actual = []
        if actual != expected:
            errors.append(
                f"task {ident}: future verification command must be {shlex.join(expected)}"
            )
        for path in task.get("touches", []):
            if Path(path).is_absolute() or ".." in Path(path).parts:
                errors.append(f"task {ident}: scope escapes repository: {path}")
        for dep in task.get("requires", []):
            if dep not in tasks:
                errors.append(f"task {ident}: unknown dependency {dep}")
            elif tasks[dep].get("wave", -1) > task.get("wave", -1):
                errors.append(f"task {ident}: dependency {dep} is in a later wave")
        for ref in task.get("reqs", []):
            if ref not in clauses:
                errors.append(f"task {ident}: unknown acceptance clause {ref}")
            task_coverage.add(ref)
        expected_cases = {
            case_id for case_id, case in cases.items() if ident in case.get("tasks", [])
        }
        if set(task.get("cases", [])) != expected_cases:
            errors.append(f"task {ident}: asymmetric task/case mapping")
    remaining = set(tasks)
    while remaining:
        ready = {
            ident for ident in remaining if not (set(tasks[ident].get("requires", [])) & remaining)
        }
        if not ready:
            errors.append("dependency cycle: " + ", ".join(sorted(remaining)))
            break
        remaining -= ready
    case_coverage: set[str] = set()
    for ident, case in cases.items():
        if stage == "implementation" and case.get("status") in OUTCOMES:
            errors += case_evidence_errors(ident, case["status"], case.get("tasks"))
        elif stage == "implementation" and case.get("status") != "UNRUN":
            errors.append(f"case {ident}: status must be UNRUN, PASS, FAIL or BLOCKED")
        elif case.get("status") != "UNRUN":
            errors.append(f"case {ident}: product qualification must remain UNRUN")
        for field in ("title", "tier", "oracle"):
            if not isinstance(case.get(field), str) or not case[field]:
                errors.append(f"case {ident}: missing {field}")
        if case.get("tier") not in {"component", "integration", "campaign", "release"}:
            errors.append(f"case {ident}: unknown tier")
        for field in ("reqs", "tasks"):
            values = case.get(field)
            if not string_list(values) or not values or len(values) != len(set(values)):
                errors.append(f"case {ident}: invalid or duplicate {field}")
        for ref in case.get("reqs", []):
            if ref not in clauses:
                errors.append(f"case {ident}: unknown acceptance clause {ref}")
            case_coverage.add(ref)
        for task in case.get("tasks", []):
            if task not in tasks:
                errors.append(f"case {ident}: unknown task {task}")
    for label, coverage in [("task", task_coverage), ("case", case_coverage)]:
        for clause in sorted(clauses - coverage, key=numeric):
            errors.append(f"acceptance clause {clause} has no {label} coverage")
    return errors, requirements, tasks, cases


def example_errors() -> tuple[list[str], dict]:
    """Check the illustrative World Source for cross-file consistency, not compilability."""
    errors: list[str] = []
    root = PACK / "examples/refworld"
    documents = {}
    for path in sorted(root.rglob("*.kvx")):
        name = str(path.relative_to(root))
        try:
            documents[name] = parse_kvx(path.read_text())
        except InvalidKVX as error:
            errors.append(f"examples/refworld/{name}: {error}")
            documents[name] = {}
    for name, document in documents.items():
        meta = document.get("meta", {})
        if (
            not str(meta.get("format", "")).startswith("cwr.")
            or meta.get("schema_version") != 1
            or meta.get("example") is not True
        ):
            errors.append(
                f"examples/refworld/{name}: example must identify its cwr.* format, schema_version 1 and example status"
            )
    world = documents.get("world.kvx", {})
    for field in ("name", "version", "language", "runtime", "modules"):
        if field not in world.get("world", {}):
            errors.append(f"world.kvx: missing world.{field}")
    for limit in LIMITS:
        if type(world.get("limits", {}).get(limit)) is not int or world["limits"][limit] <= 0:
            errors.append(f"world.kvx: limits.{limit} must be a positive integer (no defaults)")
    for key in ("tick_per_primitive", "tick_per_turn"):
        if type(world.get("clock", {}).get(key)) is not int or world["clock"][key] < 1:
            errors.append(f"world.kvx: clock.{key} must be a positive integer")
    if not isinstance(world.get("rng", {}).get("seed_visible"), bool):
        errors.append("world.kvx: rng.seed_visible must be a boolean")
    modules = set(world.get("world", {}).get("modules", []))
    primitives: dict[str, dict] = {}
    adapters: dict[str, dict] = {}
    for name, document in documents.items():
        if name.startswith("contracts/"):
            module = document.get("meta", {}).get("module")
            if module not in modules:
                errors.append(f"{name}: module {module!r} is not declared in world.kvx")
            for section, block in document.items():
                if section.startswith("primitive."):
                    ident = section[len("primitive.") :]
                    if not ident.startswith(f"{module}."):
                        errors.append(f"{name}: primitive {ident} must belong to module {module}")
                    primitives[ident] = block
        if name.startswith("adapters/"):
            module = document.get("meta", {}).get("module")
            if module not in modules:
                errors.append(f"{name}: module {module!r} is not declared in world.kvx")
            for section, block in document.items():
                if section.startswith("adapter."):
                    adapters[section[len("adapter.") :]] = block
    read_only = {
        ident for ident, block in primitives.items() if block.get("side_effect") == "read_only"
    }
    for ident, block in {**primitives, **adapters}.items():
        if "params" not in block or not isinstance(block["params"], dict) or "returns" not in block:
            errors.append(f"{ident}: params object and returns type are required")
        if not isinstance(block.get("doc"), str) or not block["doc"]:
            errors.append(f"{ident}: doc is required")
        preconditions = block.get("preconditions")
        if not isinstance(preconditions, list):
            errors.append(f"{ident}: preconditions must be a list")
            preconditions = []
        for index, item in enumerate(preconditions, 1):
            if not (
                isinstance(item, list)
                and len(item) == 2
                and all(isinstance(part, str) for part in item)
            ):
                errors.append(f"{ident}: precondition {index} must be [expr, failure]")
                continue
            expr, failure = item
            if failure not in ENVELOPE:
                errors.append(
                    f"{ident}: precondition {index} names a code outside the envelope: {failure}"
                )
            for call in re.findall(r"\b([a-z_]+\.[a-z_]+)\s*\(", expr):
                if call not in read_only:
                    errors.append(
                        f"{ident}: precondition {index} references non-read_only or unknown primitive {call}"
                    )
    for ident, block in primitives.items():
        side_effect = block.get("side_effect")
        effects = block.get("effects")
        if side_effect not in SIDE_EFFECTS - {"external"}:
            errors.append(f"{ident}: primitive side_effect must be read_only or mutating")
        if not isinstance(effects, list) or not all(isinstance(effect, str) for effect in effects):
            errors.append(f"{ident}: effects must be a list of expressions")
        elif side_effect == "mutating" and not effects:
            errors.append(f"{ident}: mutating primitive must declare at least one effect")
        elif side_effect == "read_only" and effects:
            errors.append(f"{ident}: read_only primitive must declare no effects")
    for ident, block in adapters.items():
        if block.get("side_effect") != "external":
            errors.append(f"{ident}: adapter side_effect must be external")
        if block.get("idempotency") not in IDEMPOTENCY:
            errors.append(f"{ident}: adapter idempotency must be none, natural or key")
        if type(block.get("timeout_ms")) is not int or block["timeout_ms"] <= 0:
            errors.append(f"{ident}: adapter timeout_ms must be a positive integer")
        failures = block.get("failures")
        if not string_list(failures) or not failures or not set(failures) <= set(ENVELOPE):
            errors.append(f"{ident}: adapter failures must be a nonempty subset of the envelope")
        elif "UNAVAILABLE" not in failures:
            errors.append(
                f"{ident}: adapter failures must include UNAVAILABLE as the undeclared-outcome mapping target"
            )
        if block.get("content_guaranteed") is not False:
            errors.append(f"{ident}: adapter must declare content_guaranteed false")
        if "effects" in block:
            errors.append(f"{ident}: adapter must not declare world-state effects")
        if not str(block.get("endpoint", "")).startswith("stub://"):
            errors.append(f"{ident}: illustrative adapter endpoint must be a stub:// endpoint")
    changelog = documents.get("CHANGELOG.kvx", {})
    releases = {key: value for key, value in changelog.items() if key.startswith("release.")}
    changes = {
        key[len("change.") :]: value
        for key, value in changelog.items()
        if key.startswith("change.")
    }
    versions = []
    for key, release in releases.items():
        version = release.get("version")
        if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
            errors.append(f"CHANGELOG.kvx: {key} needs a semantic version")
            continue
        versions.append(version)
        if key != f"release.{version}":
            errors.append(f"CHANGELOG.kvx: section {key} must be named release.{version}")
        for field in ("date", "summary"):
            if not isinstance(release.get(field), str) or not release[field]:
                errors.append(f"CHANGELOG.kvx: {key} missing {field}")
        if not string_list(release.get("changes")):
            errors.append(f"CHANGELOG.kvx: {key} changes must be a list of change ids")
            continue
        for change_id in release["changes"]:
            if change_id not in changes:
                errors.append(f"CHANGELOG.kvx: {key} references unknown change {change_id}")
    referenced = {
        change_id
        for release in releases.values()
        for change_id in release.get("changes", [])
        if isinstance(release.get("changes"), list)
    }
    for change_id, change in changes.items():
        if change_id not in referenced:
            errors.append(f"CHANGELOG.kvx: change {change_id} belongs to no release")
        kind = change.get("kind")
        if kind not in CHANGE_KINDS:
            errors.append(f"CHANGELOG.kvx: change {change_id} has unknown kind {kind!r}")
        target = change.get("target")
        known = set(primitives) | set(adapters) | modules
        if kind != "primitive_removed" and target not in known:
            errors.append(f"CHANGELOG.kvx: change {change_id} targets unknown {target!r}")
        for field in ("before", "after"):
            if not isinstance(change.get(field), str):
                errors.append(f"CHANGELOG.kvx: change {change_id} missing {field}")
        migration = change.get("migration")
        if not isinstance(migration, str):
            errors.append(f"CHANGELOG.kvx: change {change_id} missing migration string")
        elif kind == "state_shape" and not migration:
            errors.append(f"CHANGELOG.kvx: state_shape change {change_id} requires a migration")
        elif migration and not (root / migration).is_file():
            errors.append(f"CHANGELOG.kvx: change {change_id} names missing migration {migration}")
    declared = world.get("world", {}).get("version")
    if versions and max(versions, key=numeric) != declared:
        errors.append(
            f"world.kvx version {declared!r} must equal the latest CHANGELOG release {max(versions, key=numeric)!r}"
        )
    drifts = 0
    for name, document in documents.items():
        if not name.startswith("drift/"):
            continue
        for section, block in document.items():
            if not section.startswith("drift."):
                continue
            drifts += 1
            if type(block.get("turn")) is not int or block["turn"] < 1:
                errors.append(f"{name}: {section} turn must be a positive integer")
            if block.get("primitive") not in primitives and block.get("primitive") not in adapters:
                errors.append(
                    f"{name}: {section} names unknown primitive {block.get('primitive')!r}"
                )
            if block.get("change") not in CHANGE_KINDS - {
                "doc_only",
                "state_shape",
                "primitive_added",
            }:
                errors.append(
                    f"{name}: {section} has a change kind that cannot be injected at runtime"
                )
            if block.get("failure") not in ENVELOPE:
                errors.append(f"{name}: {section} names a code outside the envelope")
            for call in re.findall(r"\b([a-z_]+\.[a-z_]+)\s*\(", str(block.get("expr", ""))):
                if call not in read_only:
                    errors.append(
                        f"{name}: {section} expr references non-read_only primitive {call}"
                    )
    return errors, {
        "files": len(documents),
        "primitives": len(primitives),
        "adapters": len(adapters),
        "releases": len(releases),
        "changes": len(changes),
        "drift_injections": drifts,
        "scope": "illustrative syntax and cross-file consistency",
        "compiler_implementation": "pending",
        "build_hash": "NOT_ESTABLISHED",
    }


def document_errors(rendering: bool) -> list[str]:
    errors: list[str] = []
    for path in sorted(PACK.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix not in TEXT_SUFFIXES:
            continue
        text = path.read_text()
        for number, line in enumerate(text.splitlines(), 1):
            if line.rstrip() != line:
                errors.append(f"{path.relative_to(PACK)}:{number}: trailing whitespace")
        if text and not text.endswith("\n"):
            errors.append(f"{path.relative_to(PACK)}: missing final newline")
        if path.suffix != ".md":
            continue
        for target in re.findall(r"\[[^\]\n]*\]\(([^)]+)\)", text):
            parsed = urlsplit(target.strip("<>"))
            if parsed.scheme or not parsed.path:
                continue
            resolved = (path.parent / unquote(parsed.path)).resolve()
            if rendering and resolved.parent == PACK and resolved.name in DERIVED:
                continue
            if not resolved.exists():
                errors.append(f"{path.relative_to(PACK)}: missing linked target {target}")
    return errors


_CHECKER: list = []


def evidence_checker():
    """Load tests/cwr/check_evidence.py, the independent checker, as a library (implementation stage only)."""
    if not _CHECKER:
        sys.dont_write_bytecode = True
        sys.path.insert(0, str(REPO / QUALIFICATION_DIR))
        try:
            import check_evidence
        except ImportError as error:
            raise ValueError(
                f"{QUALIFICATION_DIR}/check_evidence.py cannot be loaded: {error}"
            ) from error
        finally:
            sys.path.remove(str(REPO / QUALIFICATION_DIR))
        _CHECKER.append(check_evidence)
    return _CHECKER[0]


def evidence_record_errors(
    label: str, record_rel, *, case: str | None = None, outcome: str | None = None
) -> list[str]:
    """An evidence path must exist inside the repository and be accepted by check_evidence.py."""
    if not isinstance(record_rel, str) or not record_rel:
        return [f"{label}: names no evidence record"]
    if Path(record_rel).is_absolute() or ".." in Path(record_rel).parts:
        return [f"{label}: evidence path escapes the repository: {record_rel}"]
    path = REPO / record_rel
    if not path.is_file():
        return [f"{label}: evidence record {record_rel} does not exist"]
    verdict = evidence_checker().check_file(path, REPO)
    if not verdict.accepted:
        return [
            f"{label}: evidence record {record_rel} is rejected by check_evidence.py: "
            + "; ".join(verdict.errors[:3])
        ]
    record = verdict.record
    errors = []
    if case is not None:
        if record["case"] != case:
            errors.append(f"{label}: evidence record {record_rel} belongs to {record['case']}")
        if record["scope"] != "case":
            errors.append(
                f"{label}: evidence record {record_rel} is component (task-scope) evidence "
                "and cannot qualify the case"
            )
    if outcome is not None and record["outcome"] != outcome:
        errors.append(
            f"{label}: evidence record {record_rel} records {record['outcome']}, not {outcome}"
        )
    return errors


def case_evidence_errors(ident: str, status: str, tasks=None) -> list[str]:
    """Implementation stage: a case outcome needs the drivers of all its tasks, must equal the registry's,
    and must be the latest accepted case-scope record of the attempt index, accepted by the checker."""
    label = f"case {ident}"
    missing = [
        t
        for t in (tasks if string_list(tasks) else [])
        if not (REPO / ("tests/cwr/cases/" + t.replace(".", "_") + ".py")).is_file()
    ]
    if missing:
        return [
            f"{label}: status {status} but the verification driver of task(s) {', '.join(missing)} "
            "does not exist; a case outcome needs a real attempt of every task driver"
        ]
    path = REPO / REGISTRY
    if not path.is_file():
        return [f"{label}: status {status} needs {REGISTRY}, which is missing"]
    try:
        registry = parse_kvx(path.read_text())
    except InvalidKVX as error:
        return [f"{label}: {REGISTRY} is invalid: {error}"]
    entry = registry.get(f"case.{ident}")
    if entry is None:
        return [f"{label}: status {status} but the case is not registered in {REGISTRY}"]
    if entry.get("status") != status:
        return [
            f"{label}: status {status} is not backed by the registry, which records {entry.get('status')!r}"
        ]
    errors = evidence_record_errors(label, entry.get("evidence"), case=ident, outcome=status)
    if errors:
        return errors
    derived = evidence_checker().derive_status(ident, REPO)
    if derived["integrity_errors"]:
        return [
            f"{label}: the evidence store is damaged: " + "; ".join(derived["integrity_errors"][:3])
        ]
    if (derived["status"], derived["evidence"]) != (status, entry.get("evidence")):
        return [
            f"{label}: registry records {status} from {entry.get('evidence')} but the latest accepted "
            f"case-scope attempt in the index is {derived['status']} from {derived['evidence']}"
        ]
    return []


def qualification_errors(stage: str | None = "design") -> list[str]:
    errors: list[str] = []
    path = PACK / "qualification.kvx"
    if not path.is_file():
        return ["qualification.kvx is missing"]
    try:
        sections = parse_kvx(path.read_text())
    except InvalidKVX as error:
        return [f"qualification.kvx: {error}"]
    for name, block in sections.items():
        if not name.startswith(("finding.", "progress.")):
            errors.append(f"qualification.kvx: unknown section {name}")
            continue
        for field in ("revision", "command", "exit_code", "log", "observed", "assumption"):
            if field not in block:
                errors.append(f"qualification.kvx: {name} missing {field}")
        if type(block.get("exit_code")) is not int:
            errors.append(f"qualification.kvx: {name} exit_code must be an integer")
        if stage == "implementation" and block.get("product_evidence") is True:
            errors += evidence_record_errors(f"qualification.kvx: {name}", block.get("evidence"))
        elif stage == "implementation" and block.get("product_evidence") is not False:
            errors.append(f"qualification.kvx: {name} product_evidence must be true or false")
        elif block.get("product_evidence") is not False:
            errors.append(
                f"qualification.kvx: {name} must state product_evidence false while cases are UNRUN"
            )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--render", action="store_true", help="Update derived task plan and traceability views"
    )
    args = parser.parse_args()
    try:
        sections = parse_kvx((PACK / "spec.kvx").read_text())
        spec_errors, requirements, tasks, cases = validate(sections)
        errors = list(spec_errors)
        example_problems, example_summary = example_errors()
        errors += example_problems
        errors += qualification_errors(sections.get("meta", {}).get("stage"))
        errors += document_errors(args.render)
        # Views can be derived whenever spec.kvx itself is valid; staleness is reported even when
        # unrelated errors exist, but views are only written when the whole pack is clean.
        if not spec_errors:
            digest = source_digest(requirements, tasks, cases)
            views = {
                "implementation-plan.md": task_plan(tasks, digest),
                "traceability.md": traceability(requirements, tasks, cases, digest),
            }
            for name, content in views.items():
                path = PACK / name
                if args.render:
                    if not errors:
                        path.write_text(content)
                elif not path.is_file() or path.read_text() != content:
                    errors.append(f"{name}: derived view is missing/stale; run --render")
        result = {
            "status": "FAIL" if errors else "PASS",
            "scope": "specification-authoring",
            "checked_at": datetime.now(UTC).isoformat(),
            "feature": sections.get("meta", {}).get("feature"),
            "spec_sha256": hashlib.sha256((PACK / "spec.kvx").read_bytes()).hexdigest(),
            "requirements": len(requirements),
            "acceptance_clauses": sum(
                len([key for key in req if re.fullmatch(r"ac_\d+", key)])
                for req in requirements.values()
            ),
            "tasks": len(tasks),
            "waves": sorted(
                {task.get("wave") for task in tasks.values() if type(task.get("wave")) is int}
            ),
            "cases": len(cases),
            "stage": sections.get("meta", {}).get("stage"),
            "product_qualification": "UNRUN",
            "task_statuses": {
                status: sum(task.get("status") == status for task in tasks.values())
                for status in sorted({str(task.get("status")) for task in tasks.values()})
            },
            "rendered": args.render,
            "errors": errors,
            "examples": example_summary,
            "inputs_sha256": {
                str(path.relative_to(PACK)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(PACK.rglob("*"))
                if path.is_file()
                and path.suffix in TEXT_SUFFIXES
                and "__pycache__" not in path.parts
            },
        }
        (PACK / REPORT).write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))
        return int(bool(errors))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(
            json.dumps(
                {"status": "FAIL", "scope": "specification-authoring", "errors": [str(exc)]},
                indent=2,
            )
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""CWR evidence closure and owner acceptance (task 6.2; clauses 13.1, 13.4, 14.1 and 14.4).

  python3 tests/cwr/closure.py [--root DIR] [--json]
      Assemble closure (read-only). For every case in spec/cwr/spec.kvx the status is derived by the
      independent checker (`tests/cwr/check_evidence.py --status CWR-NNN`) from accepted case-scope
      attempt records in the hash-chained attempt index; component (task-scope) records never count.
      Closure is refused, and every blocking item listed, when any case is missing (UNRUN), not PASS,
      tampered (the checker reports damage), zero-assertion, stale (a PASS recorded from a product,
      compiler, kernel, SDK or, for integration and campaign cases, harness revision that is no longer
      the current tree; a PASS judged by a checker revision that is no longer current; for CWR-019,
      which qualifies the checker rather than the product, a registry or schema that changed since; a
      record the checker reports stale) or disagrees with
      the registry; when a clause maps to no PASS case; when a hypothesis report or its pre-registration
      is missing; or when owner acceptance is not recorded. Exit 0 ACCEPTED, 1 REFUSED, 2 unusable.

  python3 tests/cwr/closure.py --run [--case CWR-NNN ...] [--timeout S]
      Run tests/cwr/qualify.py --case for every case (or the named ones) in order, so every task
      driver runs fresh and every outcome becomes an immutable attempt record. Writes the run log
      tests/cwr/evidence/closure/run-<UTC>.json. Exit 0 when every case was attempted, 1 otherwise.
      Each qualify.py run leads its own process group, which is reaped on exit, on an interrupt and
      on SIGTERM, so no driver child outlives the run.

  python3 tests/cwr/closure.py --apply [--since SEQ] [--run-log FILE]
      Update spec/cwr/spec.kvx from registry outcomes only: meta status in_progress and stage
      implementation; every case status as the checker derives it; every task implemented when its
      driver exists and its latest run (attempt index seq >= SEQ) exited 0, otherwise blocked with
      the reason recorded in spec/cwr/qualification.kvx. Appends one qualification entry per driver
      run (and per refused qualify.py call named in the run log); product_evidence is true only
      where the entry names a record the checker accepts.

  python3 tests/cwr/closure.py --journey [--cwr BIN]
      Run the owner's acceptance journey with the real cwr CLI (build refworld, create an Instance,
      run a long task, upgrade across pair U1, replay) and record it, with the exact Build hashes,
      in spec/cwr/qualification.kvx as [progress.acceptance_journey]. Exit 0 when every step ran.

  python3 tests/cwr/closure.py --accept --owner NAME
      Record the owner's acceptance of the recorded journey as [progress.owner_acceptance]. This is
      the owner's act; nothing in this repository invokes it on the owner's behalf.

The authoring validator and its report are never an input to closure: an authoring PASS is not
product evidence. Python 3.12, standard library only; no network.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import runner_support  # noqa: E402  tests/cwr/runner_support.py, importable after the insert above

DEFAULT_ROOT = HERE.parents[1]
SPEC_REL = "spec/cwr/spec.kvx"
QUAL_REL = "spec/cwr/qualification.kvx"
REGISTRY_REL = "tests/cwr/registry.kvx"
INDEX_REL = "tests/cwr/evidence/index.jsonl"
CHECKER_REL = "tests/cwr/check_evidence.py"
QUALIFY_REL = "tests/cwr/qualify.py"
CLOSURE_LOG_REL = "tests/cwr/evidence/closure"
PREREG_REL = "benchmark/preregistration.kvx"
RELEASE_REL = "benchmark/release.kvx"
DEFAULT_REPORTS = {
    "H1": "research/h1-stability.md",
    "H2": "research/h2-direct-action.md",
    "H3": "research/h3-long-context.md",
    "H4": "research/h4-controlled-change.md",
}
SCHEMA_REL = "tests/cwr/evidence.schema.json"
# The case whose oracle is built into the checker: its evidence also depends on the registry and schema.
SELF_CASE = "CWR-019"
RUNNER_META_KEYS = {"index_length", "index_head"}
RUNNER_CASE_KEYS = {"status", "evidence"}
SHA = re.compile(r"^sha256:[0-9a-f]{64}$")
CASE_RE = re.compile(r"^CWR-\d{3}$")
ACCEPTANCE = "progress.owner_acceptance"
JOURNEY = "progress.acceptance_journey"
QUAL_HEADER = (
    "# Recorded runs for the CWR specification pack: authoring validation, every task driver run\n"
    "# through tests/cwr/qualify.py, blocked tasks and the owner's acceptance journey.\n"
    "# product_evidence is true only where an entry names an attempt record that\n"
    "# tests/cwr/check_evidence.py accepts; everything else states product_evidence = false.\n"
)
SUBPROCESS_TIMEOUT = 600


class UnusableError(Exception):
    """Closure cannot be assembled from the inputs (exit 2); the message names what is wrong."""


# ------------------------------------------------------------------ small helpers


def utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def strict_json(text: str):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError(f"duplicate JSON key {key!r}")
            out[key] = value
        return out

    def constant(value):
        raise ValueError(f"non-JSON constant {value}")

    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def parse_kvx(text: str, label: str) -> dict:
    """Strict KVX: [section] headers and one `key = <JSON literal>` per line."""
    sections: dict = {}
    current = None
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        header = re.fullmatch(r"\[([A-Za-z0-9_.-]+)\]", line)
        if header:
            if header.group(1) in sections:
                raise UnusableError(f"{label}:{number}: duplicate section [{header.group(1)}]")
            current = sections[header.group(1)] = {}
            continue
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_-]*)\s*=\s*(.+)", line)
        if not match or current is None:
            raise UnusableError(f"{label}:{number}: unsupported or out-of-section syntax")
        if match.group(1) in current:
            raise UnusableError(f"{label}:{number}: duplicate key {match.group(1)}")
        try:
            current[match.group(1)] = strict_json(match.group(2))
        except ValueError as error:
            raise UnusableError(
                f"{label}:{number}: value is not one JSON literal: {error}"
            ) from error
    return sections


def read_kvx(root: Path, rel: str) -> dict:
    path = root / rel
    if not path.is_file():
        raise UnusableError(f"{rel} is missing")
    return parse_kvx(path.read_text(), rel)


def file_digest(path: Path) -> str | None:
    return sha256_bytes(path.read_bytes()) if path.is_file() else None


def case_registry_digest(registry: dict, case_id: str) -> str:
    """The registry digest a record pins (the same definition as qualify.py and check_evidence.py)."""
    meta = {k: v for k, v in registry.get("meta", {}).items() if k not in RUNNER_META_KEYS}
    case = {
        k: v for k, v in registry.get(f"case.{case_id}", {}).items() if k not in RUNNER_CASE_KEYS
    }
    return sha256_bytes(
        json.dumps({"case": case, "meta": meta}, sort_keys=True, separators=(",", ":")).encode()
    )


def stale_items(root: Path, registry: dict, ident: str, tier, record: dict, current: dict) -> list:
    """Why a PASS record no longer describes the current tree, checker, registry or schema."""
    items = []
    # CWR-019 qualifies the checker, not the product: its basis is the checker, registry and schema
    compared = () if ident == SELF_CASE else runner_support.compared_revisions(tier)
    changed = [key for key in compared if record.get(key) != current.get(key)]
    if changed:
        items.append(f"recorded {', '.join(changed)} differ from the current tree")
    checker = record.get("checker") if isinstance(record.get("checker"), dict) else {}
    if checker.get("revision") != file_digest(root / CHECKER_REL):
        items.append("judged by a checker revision that is no longer tests/cwr/check_evidence.py")
    if ident == SELF_CASE:
        if record.get("registry_sha256") != case_registry_digest(registry, ident):
            items.append(f"the registry entry of {ident} changed since the record was written")
        if record.get("schema_sha256") != file_digest(root / SCHEMA_REL):
            items.append("the evidence schema changed since the record was written")
    return items


def clean_env() -> dict:
    """The environment for child programs: the qualification protocol variables are never inherited."""
    return {k: v for k, v in os.environ.items() if not k.startswith(("CWR_", "PYTHON"))}


def case_key(ident: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", ident))


# ------------------------------------------------------------------ assembling closure


def checker_status(root: Path, case: str) -> dict:
    """Derive one case's status with the independent checker, run as a separate program."""
    process = subprocess.run(
        [sys.executable, "-I", str(root / CHECKER_REL), "--root", str(root), "--status", case],
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT,
        env=clean_env(),
        cwd=root,
    )
    try:
        result = strict_json(process.stdout)
    except ValueError as error:
        raise UnusableError(
            f"{CHECKER_REL} --status {case} produced no verdict (exit {process.returncode}): "
            f"{error}; {process.stderr.strip()[-300:]}"
        ) from error
    if not isinstance(result, dict) or result.get("case") != case:
        raise UnusableError(f"{CHECKER_REL} --status {case} returned an unexpected verdict")
    return result


def load_record(root: Path, rel: str) -> dict | None:
    try:
        record = strict_json((root / rel).read_text())
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return record if isinstance(record, dict) else None


def hypothesis_items(root: Path, registry: dict) -> tuple[dict, list[str]]:
    """Each hypothesis report must exist and carry the registered pre-registration digest."""
    reports = dict(DEFAULT_REPORTS)
    release = root / RELEASE_REL
    if release.is_file():
        listed = parse_kvx(release.read_text(), RELEASE_REL).get("evidence", {}).get("reports")
        if (
            isinstance(listed, list)
            and len(listed) == 4
            and all(isinstance(p, str) for p in listed)
        ):
            reports = {f"H{n}": p for n, p in enumerate(listed, 1)}
    blocking: list[str] = []
    prereg_path = root / PREREG_REL
    digest = sha256_bytes(prereg_path.read_bytes()) if prereg_path.is_file() else None
    prereg_status = None
    if digest is None:
        blocking.append(f"pre-registration {PREREG_REL} is missing")
    else:
        prereg_status = parse_kvx(prereg_path.read_text(), PREREG_REL).get("meta", {}).get("status")
        if prereg_status != "registered":
            blocking.append(
                f"pre-registration {PREREG_REL} has meta.status {prereg_status!r}, not 'registered'"
            )
    registered = None
    for ident, case in registry.items():
        if ident.startswith("case.") and case.get("tier") == "campaign":
            registered = case.get("preregistration_digest")
            if registered is None:
                blocking.append(f"{ident[5:]}: no pre-registration digest is registered")
            elif registered != digest:
                blocking.append(
                    f"{ident[5:]}: registered pre-registration digest {registered} != {PREREG_REL} {digest}"
                )
    status: dict = {"preregistration": PREREG_REL, "digest": digest, "status": prereg_status}
    for hypothesis, rel in sorted(reports.items()):
        path = root / rel
        if not path.is_file():
            blocking.append(f"{hypothesis} report {rel} is missing")
            status[hypothesis] = {"report": rel, "present": False, "carries_preregistration": False}
            continue
        text = path.read_text(errors="replace")
        carries = digest is not None and PREREG_REL in text and digest in text
        if not carries:
            blocking.append(
                f"{hypothesis} report {rel} does not carry its pre-registration "
                f"({PREREG_REL} and digest {digest})"
            )
        status[hypothesis] = {"report": rel, "present": True, "carries_preregistration": carries}
    return status, blocking


def acceptance_items(root: Path, pass_builds: set[str]) -> tuple[dict, list[str]]:
    """Owner acceptance must be recorded and reference exact Build hashes that PASS evidence used."""
    qual = root / QUAL_REL
    block = parse_kvx(qual.read_text(), QUAL_REL).get(ACCEPTANCE) if qual.is_file() else None
    if block is None:
        return {"recorded": False}, [
            f"owner acceptance is not recorded ([{ACCEPTANCE}] in {QUAL_REL})"
        ]
    problems = []
    if block.get("owner_acceptance") is not True:
        problems.append("owner_acceptance is not true")
    if not isinstance(block.get("accepted_by"), str) or not block["accepted_by"].strip():
        problems.append("accepted_by names no owner")
    builds = block.get("builds")
    if (
        not isinstance(builds, list)
        or not builds
        or not all(isinstance(b, str) and SHA.match(b) for b in builds)
    ):
        problems.append("builds must be a nonempty list of sha256:<64hex> Build hashes")
        builds = []
    unbacked = sorted(b for b in builds if b not in pass_builds)
    if unbacked:
        problems.append(f"accepted Build hashes {unbacked} are not used by any PASS case evidence")
    summary = {
        "recorded": not problems,
        "accepted_by": block.get("accepted_by"),
        "accepted_on": block.get("accepted_on"),
        "builds": builds,
    }
    return summary, [f"owner acceptance in {QUAL_REL} is invalid: {p}" for p in problems]


def assemble(root: Path) -> dict:
    """Assemble closure from the registry, the attempt index and the checker; read-only."""
    spec = read_kvx(root, SPEC_REL)
    registry = read_kvx(root, REGISTRY_REL)
    spec_cases = {k[5:]: v for k, v in spec.items() if k.startswith("case.")}
    registry_cases = {k[5:]: v for k, v in registry.items() if k.startswith("case.")}
    if not spec_cases:
        raise UnusableError(f"{SPEC_REL} declares no case")
    blocking: list[str] = []
    if set(spec_cases) != set(registry_cases):
        blocking.append(
            f"registry cases {sorted(registry_cases)} differ from spec cases {sorted(spec_cases)}"
        )
    current = runner_support.revisions(root)
    cases: list[dict] = []
    pass_builds: set[str] = set()
    for ident in sorted(spec_cases, key=case_key):
        derived = checker_status(root, ident)
        entry = registry_cases.get(ident, {})
        status, evidence = derived.get("status"), derived.get("evidence")
        items: list[str] = []
        record = load_record(root, evidence) if isinstance(evidence, str) else None
        if derived.get("integrity_errors"):
            items.append(
                "tampered or damaged evidence: " + "; ".join(derived["integrity_errors"][:3])
            )
        elif derived.get("evidence_stale"):
            reasons = next(
                (
                    s.get("reasons", [])
                    for s in derived.get("stale_records") or []
                    if s.get("record") == evidence
                ),
                [],
            )
            items.append(
                f"stale evidence: {evidence} must be qualified again: " + "; ".join(reasons[:2])
            )
        elif status == "UNRUN":
            components = derived.get("component_records") or []
            items.append(
                "missing: no executed case-scope attempt"
                + (
                    f" ({len(components)} component record(s) never qualify the case)"
                    if components
                    else ""
                )
            )
        elif status != "PASS":
            reason = ""
            if record is not None:
                reason = "; ".join(str(d) for d in (record.get("diagnostics") or [])[:2])
                if record.get("blocked_reason"):
                    reason = f"blocked: {record['blocked_reason']}"
            items.append(
                f"unresolved required failure: {status} in {evidence}"
                + (f" ({reason})" if reason else "")
            )
        if (
            derived.get("integrity_errors") == []
            and not derived.get("evidence_stale")
            and ((entry.get("status"), entry.get("evidence")) != (status, evidence))
        ):
            items.append(
                f"registry records {entry.get('status')} from {entry.get('evidence')} but the "
                f"checker derives {status} from {evidence}"
            )
        if status == "PASS":
            if record is None:
                items.append(f"evidence record {evidence} is unreadable")
            else:
                if not record.get("observed"):
                    items.append(f"zero-assertion evidence: {evidence} observed no assertion")
                builds = [b for b in (record.get("builds") or []) if isinstance(b, str)]
                stale = stale_items(
                    root, registry, ident, spec_cases[ident].get("tier"), record, current
                )
                if stale:
                    items.append(
                        (
                            f"stale-Build evidence: {evidence} used Build(s) {builds}"
                            if builds
                            else f"stale evidence: {evidence}"
                        )
                        + ": "
                        + "; ".join(stale)
                    )
                else:
                    pass_builds.update(builds)
        cases.append(
            {
                "case": ident,
                "status": status,
                "evidence": evidence,
                "tier": spec_cases[ident].get("tier"),
                "reqs": spec_cases[ident].get("reqs") or [],
                "component_records": len(derived.get("component_records") or []),
                "rejected_records": len(derived.get("rejected_records") or []),
                "blocking": items,
            }
        )
        blocking += [f"{ident}: {item}" for item in items]
    qualified = {c["case"] for c in cases if c["status"] == "PASS" and not c["blocking"]}
    clauses = []
    for ident, req in sorted(
        ((k[4:], v) for k, v in spec.items() if k.startswith("req.")),
        key=lambda kv: case_key(kv[0]),
    ):
        for n in sorted(int(k[3:]) for k in req if re.fullmatch(r"ac_\d+", k)):
            clause = f"{ident}.{n}"
            mapped = sorted(
                (c for c, v in spec_cases.items() if clause in (v.get("reqs") or [])), key=case_key
            )
            tasks = sorted(
                (
                    k[5:]
                    for k, v in spec.items()
                    if k.startswith("task.") and clause in (v.get("reqs") or [])
                ),
                key=case_key,
            )
            passing = [c for c in mapped if c in qualified]
            clauses.append({"clause": clause, "tasks": tasks, "cases": mapped, "passing": passing})
            if not tasks or not mapped:
                blocking.append(f"clause {clause}: not traced to a task and a case")
            elif not passing:
                blocking.append(f"clause {clause}: no qualified PASS case among {mapped}")
    hypotheses, more = hypothesis_items(root, registry)
    blocking += more
    acceptance, more = acceptance_items(root, pass_builds)
    blocking += more
    counts: dict[str, int] = {}
    for case in cases:
        counts[str(case["status"])] = counts.get(str(case["status"]), 0) + 1
    verdict = "ACCEPTED" if not blocking else "REFUSED"
    return {
        "closure": "cwr",
        "verdict": verdict,
        "release_claim": verdict == "ACCEPTED",
        "case_counts": dict(sorted(counts.items())),
        "cases": cases,
        "clauses": clauses,
        "hypotheses": hypotheses,
        "owner_acceptance": acceptance,
        "revisions": current,
        "blocking": blocking,
    }


def render(report: dict) -> str:
    lines = []
    for case in report["cases"]:
        lines.append(
            f"CLOSURE CASE {case['case']} {case['status']} {case['evidence'] or '-'}"
            + (f" components={case['component_records']}" if case["component_records"] else "")
        )
    hyp = report["hypotheses"]
    for name in ("H1", "H2", "H3", "H4"):
        info = hyp.get(name, {})
        lines.append(
            f"CLOSURE HYPOTHESIS {name} report={info.get('report')} present={info.get('present')} "
            f"preregistration={info.get('carries_preregistration')}"
        )
    acc = report["owner_acceptance"]
    lines.append(
        "CLOSURE OWNER-ACCEPTANCE "
        + (
            f"recorded by {acc['accepted_by']} builds={acc['builds']}"
            if acc.get("recorded")
            else "not recorded"
        )
    )
    lines += [f"CLOSURE BLOCKING {item}" for item in report["blocking"]]
    counts = " ".join(f"{k}={v}" for k, v in report["case_counts"].items())
    if report["release_claim"]:
        lines.append(
            f"CLOSURE VERDICT RELEASE CLAIM ACCEPTED: {len(report['cases'])} cases ({counts}), "
            "hypothesis reports pre-registered, owner acceptance recorded"
        )
    else:
        lines.append(
            f"CLOSURE VERDICT RELEASE CLAIM REFUSED: {len(report['blocking'])} blocking item(s); "
            f"cases {counts}"
        )
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ running every driver


def read_index(root: Path) -> list[dict]:
    path = root / INDEX_REL
    if not path.is_file():
        return []
    entries = []
    for line in path.read_text().splitlines():
        try:
            entry = strict_json(line)
        except ValueError as error:
            raise UnusableError(f"{INDEX_REL} holds a line that is not JSON: {error}") from error
        entries.append(entry)
    return entries


def run_all(root: Path, cases: list[str], timeout: float) -> tuple[Path, dict]:
    stamp = utc_stamp()
    log_dir = root / CLOSURE_LOG_REL / f"run-{stamp}"
    log_dir.mkdir(parents=True, exist_ok=False)
    first_seq = len(read_index(root))
    runs = []
    for case in cases:
        started = time.time()
        command = [
            sys.executable,
            str(root / QUALIFY_REL),
            "--case",
            case,
            "--timeout",
            str(timeout),
        ]
        # qualify.py leads its own process group; the group is reaped however the run ends
        process = runner_support.run_group(
            command, cwd=root, env=dict(os.environ), timeout=None, text=True
        )
        out = log_dir / f"{case}.log"
        out.write_text(
            f"$ python3 {QUALIFY_REL} --case {case}\n--- stdout\n{process.stdout}--- stderr\n{process.stderr}"
        )
        results, refused = [], None
        for line in process.stdout.splitlines():
            try:
                results.append(strict_json(line))
            except ValueError:
                continue
        for line in process.stderr.splitlines():
            try:
                refused = strict_json(line).get("refused", refused)
            except (ValueError, AttributeError):
                continue
        runs.append(
            {
                "case": case,
                "command": f"python3 {QUALIFY_REL} --case {case}",
                "exit_code": process.returncode,
                "seconds": round(time.time() - started, 1),
                "log": str(out.relative_to(root)),
                "results": results,
                "refused": refused,
            }
        )
        print(
            f"RUN {case} exit={process.returncode} "
            + (f"refused={refused}" if refused else json.dumps(results, sort_keys=True)),
            flush=True,
        )
    log = {
        "stamp": stamp,
        "first_seq": first_seq,
        "last_seq": len(read_index(root)) - 1,
        "cases": cases,
        "runs": runs,
    }
    path = root / CLOSURE_LOG_REL / f"run-{stamp}.json"
    path.write_text(json.dumps(log, indent=2, sort_keys=True) + "\n")
    return path, log


# ------------------------------------------------------------------ applying registry outcomes


def set_status_lines(text: str, updates: dict[str, dict[str, object]]) -> str:
    """Rewrite existing `key = value` lines of named sections; every named key must exist."""
    lines = text.splitlines(keepends=True)
    section, hits = None, set()
    for index, line in enumerate(lines):
        stripped = line.strip()
        header = re.fullmatch(r"\[([A-Za-z0-9_.-]+)\]", stripped)
        if header:
            section = header.group(1)
            continue
        match = re.match(r"([A-Za-z_][A-Za-z0-9_-]*)\s*=", stripped)
        if match and section in updates and match.group(1) in updates[section]:
            lines[index] = f"{match.group(1)} = {json.dumps(updates[section][match.group(1)])}\n"
            hits.add((section, match.group(1)))
    wanted = {(s, k) for s, keys in updates.items() for k in keys}
    if hits != wanted:
        raise UnusableError(f"{SPEC_REL}: cannot locate {sorted(wanted - hits)}")
    return "".join(lines)


def kvx_block(name: str, fields: dict) -> str:
    return f"[{name}]\n" + "".join(f"{k} = {json.dumps(v)}\n" for k, v in fields.items())


def rewrite_qualification(root: Path, add: list[tuple[str, dict]], drop_prefixes: tuple) -> None:
    """Keep every existing section (except those with a dropped prefix) and append the new ones."""
    path = root / QUAL_REL
    text = path.read_text() if path.is_file() else ""
    blocks: list[tuple[str, str]] = []
    current_name, current_lines = None, []
    for line in text.splitlines(keepends=True):
        header = re.fullmatch(r"\[([A-Za-z0-9_.-]+)\]", line.strip())
        if header:
            if current_name is not None:
                blocks.append((current_name, "".join(current_lines).rstrip("\n") + "\n"))
            current_name, current_lines = header.group(1), [line]
        elif current_name is not None:
            current_lines.append(line)
    if current_name is not None:
        blocks.append((current_name, "".join(current_lines).rstrip("\n") + "\n"))
    kept = [(n, b) for n, b in blocks if not n.startswith(drop_prefixes)]
    names = {n for n, _ in kept}
    for name, fields in add:
        if name not in names:
            kept.append((name, kvx_block(name, fields)))
            names.add(name)
    licence = [
        line
        for line in text.splitlines(keepends=True)[:3]
        if line.startswith(("# Copyright", "# SPDX"))
    ]
    body = "\n".join(block for _, block in kept)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text("".join(licence) + QUAL_HEADER + "\n" + body)
    os.replace(temp, path)


def checker_accepts(root: Path, rel: str) -> bool:
    process = subprocess.run(
        [sys.executable, "-I", str(root / CHECKER_REL), "--root", str(root), str(root / rel)],
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT,
        env=clean_env(),
        cwd=root,
    )
    return process.returncode == 0


def apply(root: Path, since: int, run_log: Path | None) -> dict:
    spec_path = root / SPEC_REL
    spec = read_kvx(root, SPEC_REL)
    tasks = sorted((k[5:] for k in spec if k.startswith("task.")), key=case_key)
    cases = sorted((k[5:] for k in spec if k.startswith("case.")), key=case_key)
    derived = {}
    for case in cases:
        result = checker_status(root, case)
        if result.get("integrity_errors"):
            raise UnusableError(
                f"{case}: the evidence store is damaged; statuses are not applied: "
                + "; ".join(result["integrity_errors"][:2])
            )
        if result.get("status") is None:
            # stale evidence: no outcome is derivable under the current inputs until it is re-run
            result = dict(result, status="UNRUN")
        derived[case] = result
    entries = [e for e in read_index(root) if isinstance(e.get("seq"), int) and e["seq"] >= since]
    latest: dict[str, tuple[dict, dict, dict]] = {}
    entries_out: list[tuple[str, dict]] = []
    for entry in entries:
        record = load_record(root, entry.get("record", ""))
        if record is None:
            continue
        accepted = bool(entry.get("accepted")) and checker_accepts(root, entry["record"])
        for inv in record.get("invocations") or []:
            task = inv.get("task")
            latest[task] = (entry, record, inv)
            exit_code = inv.get("exit_code")
            observed = record.get("observed") or []
            passed = sum(1 for o in observed if o.get("outcome") == "PASS")
            fields = {
                "revision": (
                    f"index seq {entry['seq']}; spec {record.get('spec_revision')}; compiler "
                    f"{record.get('compiler_revision')}; kernel {record.get('kernel_revision')}; "
                    f"sdk {record.get('sdk_revision')}"
                ),
                "command": f"{record.get('command')} (driver: {' '.join(inv.get('command') or [])})",
                "exit_code": exit_code if isinstance(exit_code, int) else -1,
                "log": inv.get("stdout_log"),
                "observed": (
                    f"task {task} driver for {record.get('case')} ({record.get('scope')} scope): "
                    f"exit {exit_code}{' (timed out)' if inv.get('timed_out') else ''}; record outcome "
                    f"{record.get('outcome')}, checker accepted {accepted}; {passed} of "
                    f"{len(observed)} observed assertions PASS for {len(record.get('assertion_ids') or [])} "
                    "registered; "
                    + (
                        "diagnostics: "
                        + "; ".join(str(d) for d in (record.get("diagnostics") or [])[:3])
                        if record.get("diagnostics")
                        else "no diagnostics"
                    )
                ),
                "assumption": (
                    "Outcome as written by tests/cwr/qualify.py and re-checked by "
                    "tests/cwr/check_evidence.py; a driver exit code alone is never a PASS and a "
                    "component record never changes a case status."
                ),
                "product_evidence": accepted,
            }
            if accepted:
                fields["evidence"] = entry["record"]
            name = f"progress.attempt_{entry['seq']:04d}_{str(task).replace('.', '_')}"
            entries_out.append((name, fields))
    refused_runs = []
    if run_log is not None:
        log = strict_json(run_log.read_text())
        for run in log.get("runs", []):
            if run.get("refused"):
                refused_runs.append(run)
                entries_out.append(
                    (
                        f"progress.run_{log['stamp']}_{run['case'].replace('-', '_')}",
                        {
                            "revision": f"closure run {log['stamp']}",
                            "command": run["command"],
                            "exit_code": run["exit_code"],
                            "log": run["log"],
                            "observed": f"qualify.py refused {run['case']}: {run['refused']}",
                            "assumption": "A refused qualification writes no attempt record and leaves the case status unchanged.",
                            "product_evidence": False,
                        },
                    )
                )
    task_status: dict[str, str] = {}
    blocked: list[tuple[str, dict]] = []
    for task in tasks:
        driver = root / "tests/cwr/cases" / (task.replace(".", "_") + ".py")
        reason, log_path, code = None, None, 2
        if not driver.is_file():
            reason = f"driver {driver.relative_to(root)} does not exist"
        elif task not in latest:
            reason = f"driver {driver.relative_to(root)} has no run through {QUALIFY_REL} at index seq >= {since}"
        else:
            entry, record, inv = latest[task]
            log_path = inv.get("stdout_log")
            code = inv.get("exit_code") if isinstance(inv.get("exit_code"), int) else -1
            if inv.get("timed_out") or inv.get("exit_code") != 0:
                reason = (
                    f"latest driver run (index seq {entry['seq']}, {entry['record']}) exited "
                    f"{inv.get('exit_code')}{' after timing out' if inv.get('timed_out') else ''}"
                )
        if reason is None:
            task_status[task] = "implemented"
            continue
        task_status[task] = "blocked"
        blocked.append(
            (
                f"finding.blocked_task_{task.replace('.', '_')}",
                {
                    "revision": f"attempt index seq >= {since}",
                    "command": f"python3 tests/cwr/cases/{task.replace('.', '_')}.py via {QUALIFY_REL}",
                    "exit_code": code,
                    "log": log_path or (str(run_log.relative_to(root)) if run_log else INDEX_REL),
                    "observed": f"task {task} is blocked: {reason}",
                    "assumption": "A task is implemented only when its driver exists and its latest run through the qualification runner exited 0.",
                    "product_evidence": False,
                },
            )
        )
    updates: dict[str, dict[str, object]] = {
        "meta": {"status": "in_progress", "stage": "implementation"}
    }
    for task in tasks:
        updates[f"task.{task}"] = {"status": task_status[task]}
    for case in cases:
        updates[f"case.{case}"] = {"status": derived[case]["status"]}
    text = set_status_lines(spec_path.read_text(), updates)
    temp = spec_path.with_name(f".{spec_path.name}.{os.getpid()}.tmp")
    temp.write_text(text)
    os.replace(temp, spec_path)
    rewrite_qualification(root, entries_out + blocked, ("finding.blocked_task_",))
    return {
        "since": since,
        "tasks": task_status,
        "cases": {c: derived[c]["status"] for c in cases},
        "qualification_entries": len(entries_out) + len(blocked),
        "refused_runs": [r["case"] for r in refused_runs],
    }


# ------------------------------------------------------------------ the owner's acceptance journey


def load_module(path: Path, name: str):
    """Load a product helper module (the task suite's predicate helpers or a reference solution)."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise UnusableError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def pick_long_task(root: Path) -> str | None:
    """The first long rule-reuse task that calls no adapter, so the journey needs no stub server."""
    suite = parse_kvx((root / "benchmark/suite.kvx").read_text(), "benchmark/suite.kvx")
    tasks = sorted(
        (k[5:], v) for k, v in suite.items() if k.startswith("task.") and v.get("bin") == "long"
    )
    offline = [
        (name, v)
        for name, v in tasks
        if not any(str(r).startswith(("search.", "publish.")) for r in v.get("rules") or [])
    ]
    for name, value in offline:
        if value.get("rule_reuse"):
            return name
    return offline[0][0] if offline else None


def journey(root: Path, cwr: Path) -> tuple[dict, int]:
    """Build refworld, create an Instance, run a long task, upgrade across U1 and replay, all with
    the real cwr CLI. Each step records its command and exit code; a failed step stops the rest."""
    work = Path(tempfile.mkdtemp(prefix="cwr-journey-"))
    home, builds_dir, pair_dir = work / "home", work / "builds", work / "pair-builds"
    steps: list[dict] = []
    hashes: dict[str, str] = {}
    state: dict = {}

    def cli(args: list) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(cwr), *[str(a) for a in args]],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
            env=clean_env(),
        )

    def step(name: str, action) -> bool:
        if steps and steps[-1]["exit_code"] != 0:
            steps.append({"step": name, "exit_code": None, "note": "not reached"})
            return False
        entry: dict = {"step": name}
        try:
            entry.update(action())
        except (UnusableError, OSError, ValueError, KeyError, RuntimeError, StopIteration) as error:
            entry.update(exit_code=-1, note=f"{type(error).__name__}: {error}")
        steps.append(entry)
        return entry.get("exit_code") == 0

    def build(label: str, source: str, out: Path, previous: str | None = None) -> dict:
        args = ["build", source, "--out", out, "--json"]
        if previous:
            args += ["--previous", previous]
        process = cli(args)
        result = {
            "command": "cwr " + " ".join(str(a) for a in args[:2]),
            "exit_code": process.returncode,
        }
        if process.returncode == 0:
            built = strict_json(process.stdout)["build"]
            hashes[label] = built["hash"]
            state[f"{label}-dir"] = built["dir"]
            result["build"] = built["hash"]
        else:
            result["stderr_tail"] = (process.stdout + process.stderr).strip()[-400:]
        return result

    task = pick_long_task(root)
    common = load_module(root / "benchmark/predicates/common.py", "common")

    def create() -> dict:
        if task is None:
            raise UnusableError("benchmark/suite.kvx lists no offline long task")
        initial = common.kernel_initial_state(root / "benchmark/tasks" / task)
        init = work / "initial.json"
        init.write_text(json.dumps(initial, sort_keys=True))
        state["initial"] = init
        process = cli(
            [
                "instance",
                "create",
                "--home",
                home,
                "--build",
                state["refworld-dir"],
                "--seed",
                "1",
                "--initial",
                init,
            ]
        )
        state["id"] = process.stdout.strip()
        return {
            "command": "cwr instance create --build <refworld> --seed 1 --initial <task initial state>",
            "exit_code": process.returncode,
            "instance": state["id"],
            "stderr_tail": process.stderr.strip()[-400:],
        }

    def accepts_fenced() -> bool:
        """Whether `cwr instance turn` takes a fenced python block (the turn protocol), probed on a
        throwaway Instance so the journey Instance runs only the task's own turns."""
        probe_home = work / "probe"
        made = cli(["instance", "create", "--home", probe_home, "--build", state["refworld-dir"]])
        script = work / "probe.txt"
        script.write_text("```python\nreturn 7\n```\n")
        process = cli(
            ["instance", "turn", made.stdout.strip(), "--home", probe_home, "--script", script]
        )
        try:
            envelope = strict_json(process.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return False
        return envelope.get("status") == "ok" and envelope.get("returned") == 7

    def long_task() -> dict:
        solution = load_module(root / "benchmark/solutions" / f"{task}.py", f"solution_{task}")
        generator = solution.turns()
        body, envelopes = next(generator), []
        cassette = work / "cassette.jsonl"
        fenced = accepts_fenced()
        while True:
            script = work / f"turn-{len(envelopes) + 1}.txt"
            script.write_text(f"```python\n{body}```\n" if fenced else body)
            process = cli(
                [
                    "instance",
                    "turn",
                    state["id"],
                    "--home",
                    home,
                    "--script",
                    script,
                    "--gateway",
                    "record",
                    "--cassette",
                    cassette,
                    "--cassette-label",
                    "journey",
                ]
            )
            if process.returncode != 0:
                return {
                    "command": f"cwr instance turn (turn {len(envelopes) + 1})",
                    "exit_code": process.returncode,
                    "stderr_tail": process.stderr.strip()[-400:],
                }
            envelopes.append(strict_json(process.stdout.strip().splitlines()[-1]))
            if len(envelopes) > 200:
                raise RuntimeError("turn limit 200 exceeded")
            try:
                body = generator.send(envelopes[-1])
            except StopIteration:
                break
        state["cassette"] = cassette
        # the pre-upgrade Instance directory (ledger, scripts, objects) is what the replay reproduces
        shutil.copytree(home / "instances" / state["id"], work / "pre-upgrade")
        kernel_state = cli(["instance", "state", state["id"], "--home", home])
        directory = home / "instances" / state["id"]
        ledger = common.load_ledger(directory / "ledger.jsonl")
        view = common.state_view_from_instance(
            strict_json(kernel_state.stdout), ledger, common.read_key_store(directory / "gateway")
        )
        success = common.evaluate_twice(
            common.load_predicate(root / "benchmark/tasks" / task), view, ledger
        )
        return {
            "command": f"cwr instance turn x{len(envelopes)} (reference solution of {task})",
            "exit_code": 0 if success else 1,
            "turns": len(envelopes),
            "statuses": sorted({e.get("status") for e in envelopes}),
            "predicate": success,
        }

    def upgrade() -> dict:
        process = cli(
            [
                "instance",
                "upgrade",
                state["id"],
                hashes["U1-B"],
                "--home",
                home,
                "--builds",
                builds_dir,
                "--builds",
                pair_dir,
            ]
        )
        return {
            "command": "cwr instance upgrade <id> <U1-B hash>",
            "exit_code": process.returncode,
            "stderr_tail": (process.stdout + process.stderr).strip()[-400:],
        }

    def replay() -> dict:
        process = cli(
            [
                "instance",
                "replay",
                work / "pre-upgrade" / "ledger.jsonl",
                "--builds",
                builds_dir,
                "--cassette",
                state["cassette"],
                "--into-home",
                work / "replay-home",
            ]
        )
        return {
            "command": ("cwr instance replay <pre-upgrade ledger> --cassette <journey cassette>"),
            "exit_code": process.returncode,
            "stderr_tail": (process.stdout + process.stderr).strip()[-400:],
        }

    step("build refworld", lambda: build("refworld", "examples/refworld", builds_dir))
    step("create Instance", create)
    step(f"run long task {task}", long_task)
    step("build U1 side A", lambda: build("U1-A", "examples/refworld-upgrades/U1/A", pair_dir))
    step(
        "build U1 side B",
        lambda: build("U1-B", "examples/refworld-upgrades/U1/B", pair_dir, hashes.get("U1-A")),
    )
    step("upgrade across U1", upgrade)
    step("replay the pre-upgrade ledger", replay)
    shutil.rmtree(work, ignore_errors=True)
    complete = all(s.get("exit_code") == 0 for s in steps)
    result = {
        "task": task,
        "steps": steps,
        "builds": hashes,
        "instance": state.get("id"),
        "complete": complete,
    }
    return result, (0 if complete else 1)


def record_journey(root: Path, result: dict) -> None:
    summary = "; ".join(
        f"{s['step']}: exit {s.get('exit_code')}" + (f" ({s.get('note')})" if s.get("note") else "")
        for s in result["steps"]
    )
    fields = {
        "revision": "compiler {compiler_revision}; kernel {kernel_revision}; sdk {sdk_revision}".format(
            **runner_support.revisions(root)
        ),
        "command": "python3 tests/cwr/closure.py --journey",
        "exit_code": 0 if result["complete"] else 1,
        "log": QUAL_REL,
        "observed": f"acceptance journey {'complete' if result['complete'] else 'incomplete'}: {summary}",
        "assumption": "The journey is recorded for the owner to accept; it is not owner acceptance and not a checker-accepted record.",
        "product_evidence": False,
        "builds": sorted(h for h in result["builds"].values() if h),
    }
    rewrite_qualification(root, [(JOURNEY, fields)], (JOURNEY,))


def accept(root: Path, owner: str) -> None:
    qual = read_kvx(root, QUAL_REL)
    journey_block = qual.get(JOURNEY)
    if journey_block is None or journey_block.get("exit_code") != 0:
        raise UnusableError(
            f"no complete acceptance journey is recorded ([{JOURNEY}] in {QUAL_REL})"
        )
    fields = {
        "revision": journey_block["revision"],
        "command": f"python3 tests/cwr/closure.py --accept --owner {owner}",
        "exit_code": 0,
        "log": QUAL_REL,
        "observed": f"owner {owner} accepted the running journey recorded in [{JOURNEY}]",
        "assumption": "Acceptance is the owner's act over the recorded journey and its exact Build hashes.",
        "product_evidence": False,
        "owner_acceptance": True,
        "accepted_by": owner,
        "accepted_on": datetime.now(UTC).strftime("%Y-%m-%d"),
        "builds": journey_block.get("builds") or [],
    }
    rewrite_qualification(root, [(ACCEPTANCE, fields)], (ACCEPTANCE,))


# ------------------------------------------------------------------ entry point


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Assemble CWR evidence closure.")
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="repository root")
    parser.add_argument("--json", action="store_true", help="print the closure report as JSON")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--run", action="store_true", help="run qualify.py --case for every case")
    mode.add_argument("--apply", action="store_true", help="update spec and qualification files")
    mode.add_argument(
        "--journey", action="store_true", help="run and record the acceptance journey"
    )
    mode.add_argument("--accept", action="store_true", help="record the owner's acceptance")
    parser.add_argument(
        "--case", action="append", default=[], help="with --run: one case (repeatable)"
    )
    parser.add_argument(
        "--timeout", type=float, default=7200.0, help="with --run: seconds per driver"
    )
    parser.add_argument(
        "--since", type=int, default=0, help="with --apply: first attempt index seq"
    )
    parser.add_argument("--run-log", help="with --apply: the run log written by --run")
    parser.add_argument("--owner", help="with --accept: the owner's name")
    parser.add_argument("--cwr", help="with --journey: the cwr binary (default: built with cargo)")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    runner_support.raise_on_sigterm()
    try:
        if args.run:
            spec = read_kvx(root, SPEC_REL)
            cases = args.case or sorted(
                (k[5:] for k in spec if k.startswith("case.")), key=case_key
            )
            bad = [c for c in cases if not CASE_RE.match(c)]
            if bad:
                raise UnusableError(f"not case ids: {bad}")
            path, log = run_all(root, cases, args.timeout)
            print(
                f"RUN-LOG {path.relative_to(root)} index seq {log['first_seq']}..{log['last_seq']}"
            )
            return 0 if all(not r["refused"] for r in log["runs"]) else 1
        if args.apply:
            run_log = Path(args.run_log).resolve() if args.run_log else None
            print(json.dumps(apply(root, args.since, run_log), indent=2, sort_keys=True))
            return 0
        if args.journey:
            cwr = Path(args.cwr) if args.cwr else None
            if cwr is None:
                target = Path(os.environ.get("CARGO_TARGET_DIR") or root / "target")
                built = subprocess.run(
                    ["cargo", "build", "-q", "-p", "cwr-cli"],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    timeout=3600,
                )
                if built.returncode != 0:
                    raise UnusableError(
                        f"cargo build -p cwr-cli failed: {built.stderr.strip()[-400:]}"
                    )
                cwr = target / "debug" / "cwr"
            result, code = journey(root, cwr)
            record_journey(root, result)
            print(json.dumps(result, indent=2, sort_keys=True))
            return code
        if args.accept:
            if not args.owner or not args.owner.strip():
                raise UnusableError("--accept needs --owner NAME")
            accept(root, args.owner.strip())
            return 0
        report = assemble(root)
    except (UnusableError, OSError, subprocess.TimeoutExpired) as error:
        print(json.dumps({"closure": "cwr", "unusable": str(error)}), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        sys.stdout.write(render(report))
    return 0 if report["release_claim"] else 1


if __name__ == "__main__":
    sys.exit(main())

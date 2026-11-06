#!/usr/bin/env python3
# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Task 0.1 driver: freeze the CWR specification pack and structural validator.

Exercises clauses 13.1, 13.5 and 14.1 against the real validator
(spec/cwr/tools/validate_spec.py), run as a subprocess:

* the real pack validates (exit 0, PASS report, 14 requirements / 76 clauses /
  21 tasks / 20 cases; at the design stage every task pending and every case UNRUN,
  at the implementation stage task statuses equal an independent recount);
* --render leaves implementation-plan.md and traceability.md byte-identical;
* every local markdown link in the pack resolves (independent scan);
* the validator executes no compiler, kernel, model or driver: an audit hook
  records every process, socket, exec, import and file-open event (from the
  implementation stage on only the evidence checker and its vetted oracles run);
* tampered copies of the pack (case status, task status, stale views, broken
  coverage, broken references, broken examples, broken links, malformed KVX)
  make the validator exit nonzero with the expected diagnostic.

Tampering is done only on temporary copies; the real pack is checked to be
unchanged apart from the regenerated authoring-validation.json report.
This driver proves authoring consistency only, never product behaviour.
Python 3.12, standard library only. Nothing is built: the validator needs no
compilation, so CARGO_TARGET_DIR is not consulted.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import protocol

REPO = Path(__file__).resolve().parents[3]
PACK_REL = Path("spec/cwr")
PACK = REPO / PACK_REL
VALIDATOR_REL = PACK_REL / "tools/validate_spec.py"
DERIVED = ("implementation-plan.md", "traceability.md")
REPORT = "authoring-validation.json"
EXPECTED_COUNTS = {"requirements": 14, "acceptance_clauses": 76, "tasks": 21, "cases": 20}
TIMEOUT = 120

# Wrapper that runs the real validator under an audit hook. It records forbidden
# events (process creation, sockets, native library loading), every exec'd code
# object, every import and every file open, then dumps them to a JSON file once
# the validator has returned.
AUDIT_WRAPPER = r"""
import json, os, runpy, sys
validator, out_path = sys.argv[1], sys.argv[2]
sys.argv = [validator] + sys.argv[3:]
FORBIDDEN_PREFIXES = ("subprocess.", "os.system", "os.exec", "os.posix_spawn", "os.spawn",
                      "os.fork", "os.forkpty", "pty.", "socket.", "ctypes.", "os.startfile",
                      "webbrowser.", "urllib.Request", "http.client.")
record = {"forbidden": [], "exec": [], "imports": [], "reads": [], "writes": []}
active = [True]
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
def hook(event, args):
    if not active[0]:
        return
    if event.startswith(FORBIDDEN_PREFIXES):
        record["forbidden"].append([event, repr(args)[:200]])
    elif event == "exec":
        record["exec"].append(getattr(args[0], "co_filename", repr(args[0])[:100]))
    elif event == "import":
        record["imports"].append(args[0])
    elif event == "open":
        path, mode, flags = args
        if isinstance(path, int):
            return
        path = os.path.abspath(os.fsdecode(path))
        writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or \
                  (isinstance(flags, int) and bool(flags & WRITE_FLAGS))
        record["writes" if writing else "reads"].append(path)
sys.addaudithook(hook)
code = 0
try:
    runpy.run_path(validator, run_name="__main__")
except SystemExit as exc:
    code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
active[0] = False
with open(out_path, "w") as handle:
    json.dump(record, handle)
sys.exit(code)
"""


class Results:
    def __init__(self) -> None:
        self.checks: dict[str, list[tuple[bool, str]]] = {"13.1": [], "13.5": [], "14.1": []}

    def add(self, clause: str, ok: bool, label: str, detail: str = "") -> None:
        self.checks[clause].append((ok, label))
        print(
            f"CHECK {clause} {'PASS' if ok else 'FAIL'} {label}"
            + (f" :: {detail}" if detail else ""),
            flush=True,
        )


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pack_digest(pack: Path) -> dict[str, str]:
    return {
        str(p.relative_to(pack)): sha256(p)
        for p in sorted(pack.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts and p.name != REPORT
    }


def run_validator(root: Path, *extra: str, audit_out: Path | None = None):
    """Run the validator that lives inside root/spec/cwr. Return (rc, report, stdout, audit)."""
    validator = root / VALIDATOR_REL
    if audit_out is None:
        cmd = [sys.executable, str(validator), *extra]
    else:
        cmd = [sys.executable, "-c", AUDIT_WRAPPER, str(validator), str(audit_out), *extra]
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=TIMEOUT, env=env)
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError:
        report = None
    audit = (
        json.loads(audit_out.read_text()) if audit_out is not None and audit_out.is_file() else None
    )
    return proc.returncode, report, proc.stdout + proc.stderr, audit


# ---------------------------------------------------------------- independent oracles


def independent_census(spec_text: str) -> dict:
    """Count sections and keys with a parser separate from the validator's."""
    sections: dict[str, dict[str, str]] = {}
    current = None
    for line in spec_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.fullmatch(r"\[(.+)\]", line)
        if m:
            current = sections.setdefault(m.group(1), {})
            continue
        key, _, value = line.partition("=")
        if current is not None:
            current[key.strip()] = json.loads(value.strip())
    reqs = {k: v for k, v in sections.items() if k.startswith("req.")}
    tasks = {k[5:]: v for k, v in sections.items() if k.startswith("task.")}
    cases = {k[5:]: v for k, v in sections.items() if k.startswith("case.")}
    clauses = [f"{k[4:]}.{f[3:]}" for k, v in reqs.items() for f in v if re.fullmatch(r"ac_\d+", f)]
    return {
        "requirements": len(reqs),
        "acceptance_clauses": len(clauses),
        "tasks": len(tasks),
        "cases": len(cases),
        "clauses": clauses,
        "task_map": tasks,
        "case_map": cases,
        "stage": sections.get("meta", {}).get("stage"),
    }


def local_links(pack: Path) -> tuple[int, list[str]]:
    checked, broken = 0, []
    for md in sorted(pack.rglob("*.md")):
        text = md.read_text()
        # strip fenced code blocks so code samples are not mistaken for links
        text = re.sub(r"```.*?```", "", text, flags=re.S)
        for target in re.findall(r"\[[^\]\n]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)", text):
            parsed = urlsplit(target.strip("<>"))
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            checked += 1
            if not (md.parent / unquote(parsed.path)).resolve().exists():
                broken.append(f"{md.relative_to(pack)} -> {target}")
    return checked, broken


# ---------------------------------------------------------------- tampering helpers


def fresh_copy(work: Path, name: str) -> Path:
    """A temporary copy of the pack reset to the design stage (every task pending, every case UNRUN,
    only the authoring entry in qualification.kvx) with its derived views rendered, so the tamper
    matrix starts from a valid design-stage pack whatever stage the checkout has reached."""
    root = work / name
    shutil.copytree(PACK, root / PACK_REL, ignore=shutil.ignore_patterns("__pycache__", REPORT))
    spec = root / PACK_REL / "spec.kvx"
    text = spec.read_text()
    if independent_census(text)["stage"] != "design":
        text = set_key(set_key(text, "meta", "stage", "design"), "meta", "status", "pending")
        for ident in re.findall(r"^\[task\.(\d+\.\d+)\]", text, re.M):
            text = set_key(text, f"task.{ident}", "status", "pending")
        for ident in re.findall(r"^\[case\.(CWR-\d{3})\]", text, re.M):
            text = set_key(text, f"case.{ident}", "status", "UNRUN")
        spec.write_text(text)
        qual = root / PACK_REL / "qualification.kvx"
        body = qual.read_text()
        keep = re.search(r"^\[progress\.authoring_validation\]\n.*?(?=^\[|\Z)", body, re.M | re.S)
        head = "".join(ln for ln in body.splitlines(keepends=True)[:2] if ln.startswith("#"))
        qual.write_text(head + "\n" + (keep.group(0).rstrip("\n") + "\n" if keep else ""))
        run_validator(root, "--render")
    return root


def set_key(text: str, section: str, key: str, value) -> str:
    """Replace one key inside one [section] of a KVX document; exactly one hit required."""
    lines = text.splitlines(keepends=True)
    inside, hits = False, 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if re.fullmatch(r"\[.+\]", stripped):
            inside = stripped == f"[{section}]"
            continue
        if inside and re.match(rf"{re.escape(key)}\s*=", stripped):
            lines[i] = f"{key} = {json.dumps(value)}\n"
            hits += 1
    if hits != 1:
        raise AssertionError(f"tamper target [{section}] {key} matched {hits} lines")
    return "".join(lines)


def replace_once(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise AssertionError(f"tamper target {old!r} occurs {text.count(old)} times")
    return text.replace(old, new)


def main() -> int:
    results = Results()
    real_spec = (PACK / "spec.kvx").read_text()
    census = independent_census(real_spec)
    before = pack_digest(PACK)
    view_bytes = {name: (PACK / name).read_bytes() for name in DERIVED}

    tmpdir = Path(tempfile.mkdtemp(prefix="cwr_0_1_"))
    try:
        # ------------------------------------------------ real pack: plain validation under audit
        rc, report, out, audit = run_validator(REPO, audit_out=tmpdir / "audit_plain.json")
        ok = (
            rc == 0
            and report is not None
            and report.get("status") == "PASS"
            and report.get("errors") == []
        )
        results.add(
            "13.1",
            ok,
            f"real pack validates rc={rc} status={report and report.get('status')}",
            "" if ok else out[-800:],
        )
        results.add("14.1", ok, f"real pack has no stale derived view (plain run rc={rc})")

        written = json.loads((PACK / REPORT).read_text()) if (PACK / REPORT).is_file() else {}
        counts = {k: written.get(k) for k in EXPECTED_COUNTS}
        indep = {k: census[k] for k in EXPECTED_COUNTS}
        ok = counts == EXPECTED_COUNTS == indep and written.get("status") == "PASS"
        results.add(
            "14.1", ok, f"{REPORT} counts {counts} == expected == independent recount {indep}"
        )
        statuses = written.get("task_statuses")
        case_status = sorted({c.get("status") for c in census["case_map"].values()})
        task_status = sorted({t.get("status") for t in census["task_map"].values()})
        recount = {}
        for task in census["task_map"].values():
            recount[task.get("status")] = recount.get(task.get("status"), 0) + 1
        if census["stage"] == "design":
            ok = (
                statuses == {"pending": 21}
                and task_status == ["pending"]
                and case_status == ["UNRUN"]
                and written.get("product_qualification") == "UNRUN"
            )
        else:
            # implementation stage: the report's task statuses equal an independent recount, and
            # every case status is an outcome the registry can back
            ok = (
                census["stage"] == "implementation"
                and statuses == recount
                and set(case_status) <= {"UNRUN", "PASS", "FAIL", "BLOCKED"}
            )
        results.add(
            "13.1",
            ok,
            f"stage={census['stage']} tasks={task_status} report={statuses} cases={case_status}",
        )

        # ------------------------------------------------ 13.5: validator executes no product code
        def audit_ok(audit: dict | None, allowed_writes: set[Path], label: str) -> None:
            if audit is None:
                results.add("13.5", False, f"{label}: no audit record")
                return
            stdlib = set(sys.stdlib_module_names)
            bad_imports = sorted({m for m in audit["imports"] if m.split(".")[0] not in stdlib})
            stdlib_dirs = tuple(
                str(Path(p).resolve())
                for p in {sys.base_prefix, sys.prefix, os.path.dirname(os.__file__)}
            )
            validator_path = str((REPO / VALIDATOR_REL).resolve())
            # From the implementation stage on the validator has the independent evidence checker
            # verify every record it relies on; the checker and its vetted case oracles run, and
            # they read only the evidence, the registry and the campaign suite inputs. No product
            # code (crates, sdk, benchmark harness) may run or be read.
            implementation = census["stage"] != "design"
            checker_code = (
                {str((REPO / "tests/cwr/check_evidence.py").resolve())} if implementation else set()
            )
            oracle_dir = str((REPO / "tests/cwr/oracles").resolve()) + os.sep
            evidence_inputs = tuple(
                str((REPO / rel).resolve()) + suffix
                for rel, suffix in (
                    ("tests/cwr", os.sep),
                    ("benchmark/suite.kvx", ""),
                    ("benchmark/tasks", os.sep),
                    ("benchmark/cassettes/suite", os.sep),
                )
            )
            bad_exec = sorted(
                {
                    f
                    for f in audit["exec"]
                    if not (
                        f.startswith("<")
                        or str(Path(f).resolve()) == validator_path
                        or str(Path(f).resolve()).startswith(stdlib_dirs)
                        or str(Path(f).resolve()) in checker_code
                        or (implementation and str(Path(f).resolve()).startswith(oracle_dir))
                    )
                }
            )
            bad_imports = [m for m in bad_imports if not (implementation and m == "check_evidence")]
            pack = str(PACK.resolve())
            repo = str(REPO.resolve())
            outside = sorted(
                {
                    p
                    for p in audit["reads"]
                    if p.startswith(repo + os.sep)
                    and not p.startswith(pack + os.sep)
                    and not (
                        implementation
                        and (p == evidence_inputs[1] or p.startswith(evidence_inputs))
                    )
                }
            )
            writes = {Path(p).resolve() for p in audit["writes"]}
            bad_writes = sorted(str(p) for p in writes - {w.resolve() for w in allowed_writes})
            ok = not (audit["forbidden"] or bad_imports or bad_exec or outside or bad_writes)
            results.add(
                "13.5",
                ok,
                f"{label}: forbidden_events={len(audit['forbidden'])} non_stdlib_imports={bad_imports} "
                f"foreign_exec={bad_exec} reads_outside_pack={outside[:5]} "
                f"writes={sorted(str(p.relative_to(REPO)) if p.is_relative_to(REPO) else str(p) for p in writes)}",
                ""
                if ok
                else json.dumps({"forbidden": audit["forbidden"][:5], "bad_writes": bad_writes}),
            )

        audit_ok(audit, {PACK / REPORT}, "plain run")
        src = (REPO / VALIDATOR_REL).read_text()
        static_hits = sorted(
            set(
                re.findall(
                    r"\b(subprocess|os\.system|os\.exec\w*|os\.spawn\w*|socket|ctypes|runpy|importlib|cargo|"
                    r"__import__|exec\(|eval\()",
                    src,
                )
            )
        )
        results.add(
            "13.5",
            not static_hits,
            f"validator source mentions no process/exec/socket facility: {static_hits}",
        )

        # ------------------------------------------------ 14.1: --render keeps derived views byte-identical
        rc, report, out, audit = run_validator(
            REPO, "--render", audit_out=tmpdir / "audit_render.json"
        )
        identical = {name: (PACK / name).read_bytes() == view_bytes[name] for name in DERIVED}
        if not all(identical.values()):
            for name in DERIVED:  # never leave the authoritative tree modified by the driver
                (PACK / name).write_bytes(view_bytes[name])
        ok = (
            rc == 0
            and report is not None
            and report.get("status") == "PASS"
            and all(identical.values())
        )
        results.add(
            "14.1",
            ok,
            f"--render rc={rc}; byte-identical {identical}; sha256 "
            + ", ".join(f"{n}={hashlib.sha256(view_bytes[n]).hexdigest()[:12]}" for n in DERIVED),
        )
        audit_ok(audit, {PACK / REPORT, *(PACK / n for n in DERIVED)}, "--render run")

        # traceability view lists every clause exactly once with a task and a case
        rows = {}
        for line in view_bytes["traceability.md"].decode().splitlines():
            m = re.match(r"\| (\d+\.\d+) \| [^|]+ \| ([^|]*) \| ([^|]*) \|", line)
            if m:
                rows.setdefault(m.group(1), []).append((m.group(2).strip(), m.group(3).strip()))
        missing = sorted(set(census["clauses"]) - set(rows))
        dup = sorted(c for c, r in rows.items() if len(r) != 1)
        empty = sorted(c for c, r in rows.items() if not r[0][0] or not r[0][1])
        ok = not missing and not dup and not empty and len(rows) == 76
        results.add(
            "14.1",
            ok,
            f"traceability.md rows={len(rows)} missing={missing} duplicate={dup} "
            f"without_task_or_case={empty}",
        )

        # ------------------------------------------------ local links (independent scan)
        checked, broken = local_links(PACK)
        ok = checked > 0 and not broken
        results.add("13.5", ok, f"independent scan: {checked} local links in pack, broken={broken}")
        results.add("14.1", ok, f"every local link resolves ({checked} checked)")

        # ------------------------------------------------ baseline copy must pass
        base = fresh_copy(tmpdir, "baseline")
        rc, report, out, _ = run_validator(base)
        baseline_ok = rc == 0 and report is not None and report.get("status") == "PASS"
        for clause in ("13.1", "13.5", "14.1"):
            results.add(
                clause,
                baseline_ok,
                f"untampered copy passes rc={rc} (control for tamper checks)",
                "" if baseline_ok else out[-800:],
            )

        # ------------------------------------------------ tamper matrix
        def tamper(
            clause: str, label: str, mutate, expect: str, *, render_then_pass: bool = False
        ) -> None:
            root = fresh_copy(tmpdir, re.sub(r"\W+", "_", label))
            try:
                mutate(root / PACK_REL)
            except AssertionError as error:
                results.add(clause, False, f"tamper '{label}' could not be applied: {error}")
                return
            rc, report, out, _ = run_validator(root)
            errors = (report or {}).get("errors", [])
            hit = any(expect in e for e in errors)
            ok = rc != 0 and report is not None and report.get("status") == "FAIL" and hit
            detail = "" if ok else out[-600:]
            evidence = f"tamper '{label}' -> rc={rc} status={(report or {}).get('status')} expected '{expect}' found={hit}"
            if ok and render_then_pass:

                def view(n: str) -> bytes:
                    # A refused --render may leave a deleted view missing: that is a FAIL line.
                    path = root / PACK_REL / n
                    return path.read_bytes() if path.exists() else b""

                before_views = {n: view(n) for n in DERIVED}
                rc2, _report2, out2, _ = run_validator(root, "--render")
                rc3, report3, _, _ = run_validator(root)
                changed = [n for n in DERIVED if view(n) != before_views[n]]
                ok = (
                    rc2 == 0
                    and rc3 == 0
                    and bool(changed)
                    and (report3 or {}).get("status") == "PASS"
                )
                evidence += f"; after --render rc={rc2}, recheck rc={rc3}, regenerated={changed}"
                detail = "" if ok else out2[-600:]
            results.add(clause, ok, evidence, detail)

        def edit(rel: str, fn):
            def mutate(pack: Path) -> None:
                path = pack / rel
                path.write_text(fn(path.read_text()))

            return mutate

        def kv(section, key, value):
            return edit("spec.kvx", lambda t: set_key(t, section, key, value))

        # 13.1: case and task status tampering
        tamper(
            "13.1",
            "case CWR-020 status UNRUN->PASS",
            kv("case.CWR-020", "status", "PASS"),
            "case CWR-020: product qualification must remain UNRUN",
        )
        tamper(
            "13.1",
            "case CWR-001 status UNRUN->FAIL",
            kv("case.CWR-001", "status", "FAIL"),
            "case CWR-001: product qualification must remain UNRUN",
        )
        tamper(
            "13.1",
            "case CWR-019 status UNRUN->BLOCKED",
            kv("case.CWR-019", "status", "BLOCKED"),
            "case CWR-019: product qualification must remain UNRUN",
        )
        tamper(
            "13.1",
            "task 0.1 status pending->done",
            kv("task.0.1", "status", "done"),
            "task 0.1: design-stage tasks must remain pending",
        )
        tamper(
            "13.1",
            "task 2.2 status pending->in_progress",
            kv("task.2.2", "status", "in_progress"),
            "task 2.2: design-stage tasks must remain pending",
        )
        tamper(
            "13.1",
            "task 6.2 status pending->implemented",
            kv("task.6.2", "status", "implemented"),
            "task 6.2: design-stage tasks must remain pending",
        )
        tamper(
            "13.1",
            "task 1.1 status pending->bogus",
            kv("task.1.1", "status", "bogus"),
            "task 1.1: unknown implementation status",
        )
        tamper(
            "13.1",
            "meta stage design->implementation with status pending",
            kv("meta", "stage", "implementation"),
            "Expected design/pending or implementation/in_progress",
        )

        # 13.5: structure, references, coverage, examples, links
        tamper(
            "13.5",
            "structure: duplicate key in req.13",
            edit("spec.kvx", lambda t: replace_once(t, "[req.13]\n", '[req.13]\ntitle = "dup"\n')),
            "duplicate key title",
        )
        tamper(
            "13.5",
            "structure: clause 13.5 without SHALL",
            kv("req.13", "ac_5", "Authoring validation checks structure only."),
            "req 13.5: missing normative acceptance statement",
        )
        tamper(
            "13.5",
            "structure: non-consecutive clause ac_9 in req.14",
            edit(
                "spec.kvx",
                lambda t: replace_once(t, "[design]\n", 'ac_9 = "X SHALL Y."\n\n[design]\n'),
            ),
            "req 14: acceptance clauses must be consecutive from 1",
        )
        tamper(
            "13.5",
            "references: case CWR-020 names unknown task 9.9",
            kv("case.CWR-020", "tasks", ["0.1", "6.1", "6.2", "9.9"]),
            "case CWR-020: unknown task 9.9",
        )
        tamper(
            "13.5",
            "references: task 0.2 requires unknown task 7.7",
            kv("task.0.2", "requires", ["0.1", "7.7"]),
            "task 0.2: unknown dependency 7.7",
        )
        tamper(
            "13.5",
            "references: task 0.2 depends on later-wave task 1.1",
            kv("task.0.2", "requires", ["0.1", "1.1"]),
            "task 0.2: dependency 1.1 is in a later wave",
        )
        tamper(
            "13.5",
            "references: task 0.1 cites unknown clause 13.9",
            kv("task.0.1", "reqs", ["13.1", "13.5", "14.1", "13.9"]),
            "task 0.1: unknown acceptance clause 13.9",
        )
        tamper(
            "13.5",
            "references: verify_cmd points elsewhere",
            kv("task.0.1", "verify_cmd", "python3 tests/cwr/cases/other.py"),
            "task 0.1: future verification command must be",
        )
        tamper(
            "13.5",
            "coverage: clause 13.5 dropped from CWR-020",
            kv("case.CWR-020", "reqs", ["13.1", "14.1", "14.2", "14.3", "14.4"]),
            "acceptance clause 13.5 has no case coverage",
        )
        tamper(
            "13.5",
            "examples: world.kvx version disagrees with changelog",
            edit(
                "examples/refworld/world.kvx",
                lambda t: re.sub(r'(version\s*=\s*)"[^"]+"', r'\1"9.9.9"', t, count=1),
            ),
            "must equal the latest CHANGELOG release",
        )
        tamper(
            "13.5",
            "examples: adapter failure outside envelope",
            edit(
                "examples/refworld/adapters/search.kvx",
                lambda t: replace_once(t, '"UNAVAILABLE"', '"UNAVAILABLE", "EXPLODED"'),
            ),
            "adapter failures must be a nonempty subset of the envelope",
        )
        tamper(
            "13.5",
            "links: README links a missing file",
            edit("README.md", lambda t: t + "\nSee [gone](no-such-file.md).\n"),
            "README.md: missing linked target no-such-file.md",
        )

        # 14.1: traceability and stale derived views
        tamper(
            "14.1",
            "stale: task 0.1 title edited without --render",
            kv("task.0.1", "title", "Freeze the CWR specification pack (edited)"),
            "implementation-plan.md: derived view is missing/stale",
            render_then_pass=True,
        )
        tamper(
            "14.1",
            "stale: case CWR-020 oracle edited without --render",
            kv("case.CWR-020", "oracle", "Edited oracle text."),
            "traceability.md: derived view is missing/stale",
            render_then_pass=True,
        )
        tamper(
            "14.1",
            "stale: clause 14.1 text edited without --render",
            kv("req.14", "ac_1", "The pack SHALL maintain traceability (edited)."),
            "traceability.md: derived view is missing/stale",
            render_then_pass=True,
        )
        tamper(
            "14.1",
            "stale: traceability.md hand-edited",
            edit("traceability.md", lambda t: t + "| 99.1 | hand edit | 0.1 | CWR-020 | x |\n"),
            "traceability.md: derived view is missing/stale",
            render_then_pass=True,
        )
        tamper(
            "14.1",
            "stale: implementation-plan.md deleted",
            lambda pack: (pack / "implementation-plan.md").unlink(),
            "implementation-plan.md: derived view is missing/stale",
            render_then_pass=True,
        )
        tamper(
            "14.1",
            "coverage: clause 13.5 dropped from its only task 0.1",
            kv("task.0.1", "reqs", ["13.1", "14.1"]),
            "acceptance clause 13.5 has no task coverage",
        )
        tamper(
            "14.1",
            "mapping: CWR-020 dropped from task 0.1 cases",
            edit(
                "spec.kvx",
                lambda t: set_key(
                    set_key(t, "task.0.1", "cases", ["CWR-020"]),
                    "case.CWR-020",
                    "tasks",
                    ["6.1", "6.2"],
                ),
            ),
            "task 0.1: asymmetric task/case mapping",
        )
        tamper(
            "14.1",
            "mapping: task 0.1 claims CWR-019 without reciprocity",
            kv("task.0.1", "cases", ["CWR-020", "CWR-019"]),
            "task 0.1: asymmetric task/case mapping",
        )

        # ------------------------------------------------ real pack untouched; leave a plain report
        rc, report, out, _ = run_validator(REPO)
        after = pack_digest(PACK)
        changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
        ok = rc == 0 and not changed
        results.add(
            "14.1",
            ok,
            f"real pack inputs unchanged after driver ({len(after)} files hashed), "
            f"final plain validation rc={rc}, changed={changed}",
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    observer = protocol.Observer("0.1", protocol.pairs(results.checks))
    observer.claim("CWR-020.a1", match=r"real pack validates")
    observer.claim("CWR-020.a1", clause="13.5")
    observer.claim("CWR-020.a2", match=r"tamper 'case CWR-\d{3} status")
    observer.claim("CWR-020.a3", match=r"tamper 'task \S+ status")
    observer.emit()
    all_ok = True
    for clause, checks in results.checks.items():
        passed = sum(ok for ok, _ in checks)
        failed = [label for ok, label in checks if not ok]
        ok = bool(checks) and not failed
        all_ok &= ok
        evidence = f"{passed}/{len(checks)} checks passed"
        if failed:
            evidence += "; failed: " + " | ".join(failed)
        print(f"CLAUSE {clause} {'PASS' if ok else 'FAIL'} {evidence}", flush=True)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())

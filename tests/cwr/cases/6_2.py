#!/usr/bin/env python3
# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Task 6.2 driver: evidence closure and owner acceptance (clauses 13.1, 13.4, 14.1 and 14.4).

Exercises the real tests/cwr/closure.py, tests/cwr/qualify.py, tests/cwr/check_evidence.py and
spec/cwr/tools/validate_spec.py, all run as subprocesses:

* on this checkout, read-only: closure lists every case of spec.kvx exactly once with the status the
  independent checker derives, names every non-PASS case as blocking, refuses the release claim
  whenever anything blocks and changes no byte of the registry, the attempt index or spec.kvx;
  closure never imports or reads the authoring validator;
* in a temporary repository root built from copies of the pack, registry, runner, checker and
  closure, with fixture task drivers and fixture oracles standing in for the product: every case is
  qualified by the real runner except one, and closure names the missing case and refuses; a
  component (task-scope) PASS for that case still leaves it missing; once the case is qualified,
  closure lists every case PASS with owner acceptance and accepts the release claim; tampered,
  stale-Build and zero-assertion evidence, a missing hypothesis report, a draft pre-registration,
  missing or unbacked owner acceptance and an untraced clause each refuse it with a named item;
* closure --apply writes case statuses only from registry outcomes (an unexecuted case stays UNRUN),
  tasks implemented or blocked from their latest driver run, and qualification entries whose
  product evidence the validator re-checks; the validator then passes at the implementation stage,
  refuses a case status the registry does not back, refuses non-pending tasks at the design stage
  and reports stale derived views until they are re-rendered.

The fixture drivers and oracles are inputs; the programs under test are the real files of this
checkout. CWR-019 in the temporary root is the real accepted attempt record of this checkout
(copied with its attempt directory and re-indexed), or, when none exists, a fresh run of the real
task 0.2 driver. Prints "CHECK ..." per check, "ASSERT CWR-020.a6|a7 PASS|FAIL ..." (the protocol of
tests/cwr/qualify.py) and one "CLAUSE <req.ac> PASS|FAIL <evidence>" line per owned clause. Exits 0
only when every clause passes. Python 3.12, standard library only; nothing is compiled.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
PACK = REPO / "spec/cwr"
CLOSURE_REL = "tests/cwr/closure.py"
QUALIFY_REL = "tests/cwr/qualify.py"
CHECKER_REL = "tests/cwr/check_evidence.py"
SUPPORT_REL = "tests/cwr/runner_support.py"
SCHEMA_REL = "tests/cwr/evidence.schema.json"
REGISTRY_REL = "tests/cwr/registry.kvx"
INDEX_REL = "tests/cwr/evidence/index.jsonl"
SPEC_REL = "spec/cwr/spec.kvx"
QUAL_REL = "spec/cwr/qualification.kvx"
VALIDATOR_REL = "spec/cwr/tools/validate_spec.py"
TIMEOUT = 900
CLAUSES = ("13.1", "13.4", "14.1", "14.4")
B1 = "sha256:" + "b1" * 32
B2 = "sha256:" + "b2" * 32
MODEL = {"identity": "fixture-model-id", "sampling": {"temperature": 0}}
GENESIS = "sha256:" + "0" * 64
MISSING = "CWR-014"
REPORTS = (
    "research/h1-stability.md",
    "research/h2-direct-action.md",
    "research/h3-long-context.md",
    "research/h4-controlled-change.md",
)

FIXTURE_ORACLE = '''"""Fixture oracle planted by tests/cwr/cases/6_2.py in a temporary repository root.

Every assertion holds iff the attempt has an Instance, every ledger holds at least one primitive
record, every primitive outcome is ok and every Instance has a final state that is a JSON object.
"""
import json


def recompute(ctx):
    problems = []
    if not ctx.instances:
        problems.append("the attempt has no Instance")
    for inst in ctx.instances:
        ledger = ctx.ledgers.get(inst["id"])
        if ledger is None:
            problems.append("instance " + inst["id"] + ": no usable ledger")
            continue
        prims = [r for r in ledger if r.get("kind") in ("primitive", "adapter")]
        if not prims:
            problems.append("instance " + inst["id"] + ": ledger holds no primitive record")
        if any((r.get("outcome") or {}).get("status") != "ok" for r in prims):
            problems.append("instance " + inst["id"] + ": a primitive record is not ok")
        final = inst.get("final_state")
        if final is None or final["path"] not in ctx.final_states:
            problems.append("instance " + inst["id"] + ": no verified final state")
        elif not isinstance(json.loads(ctx.final_states[final["path"]].decode()), dict):
            problems.append("instance " + inst["id"] + ": final state is not an object")
    verdict = ("FAIL", "; ".join(problems)) if problems else ("PASS", "ledger ok, final state present")
    return {ident: verdict for ident in ctx.expected}
'''

DRIVER_TEMPLATE = r"""#!/usr/bin/env python3
# Fixture task driver generated by tests/cwr/cases/6_2.py for task {task}; stands in for the product.
import json, os, sys
from pathlib import Path
CONF = json.loads({conf!r})
out = Path(os.environ["CWR_EVIDENCE_DIR"])
cases = [c for c in os.environ.get("CWR_CASES", "").split(",") if c]
STATE = "sha256:" + "5" * 64
instances = []
for case in cases:
    inst_id = "01FX" + CONF["task"].replace(".", "") + case[-3:]
    ledger = out / "ledgers" / (inst_id + ".jsonl")
    ledger.parent.mkdir(parents=True, exist_ok=True)
    base = {{"instance": inst_id, "build": CONF["build"], "experimental": None}}
    # campaign ledgers carry the harness arm form H<n>/<arm> and the task's initial-state digest
    arm = "H1/frozen" if case == "CWR-018" else "frozen"
    rows = [dict(base, seq=0, turn=0, world_time=0, kind="header", primitive=None, seed=7, tail_size=20,
                 arm=arm, model=CONF["model"]["identity"], created_from=None, initial_state=STATE)]
    for n in range(1, 4):
        rows.append(dict(base, seq=n, turn=1, world_time=n, kind="primitive", primitive="fs.read",
                         params_hash=None, preconditions=[],
                         outcome={{"status": "ok", "code": None, "shape": "Bytes", "side_effect": "read_only"}},
                         duration_us=10, host_ts="2026-11-06T12:00:00Z"))
    rows.append(dict(base, seq=4, turn=1, world_time=14, kind="turn", primitive=None, status="ok"))
    ledger.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
    state = out / "state" / (inst_id + ".json")
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({{"fs": {{"/a": "x"}}, "kv": {{}}}}, sort_keys=True) + "\n")
    entry = {{"id": inst_id, "seed": 7, "tail_size": 20, "arm": arm, "case": case,
              "build": CONF["build"], "ledger": "ledgers/" + inst_id + ".jsonl",
              "final_state": "state/" + inst_id + ".json"}}
    if case == "CWR-018":
        entry.update(hypothesis="H1", task="t001-copy-readme", initial_state_digest=STATE)
    instances.append(entry)
manifest = {{"build_hash": CONF["build"], "gateway_mode": "replay", "model": CONF["model"],
             "instances": instances}}
if CONF.get("prereg"):
    manifest["preregistration_digest"] = CONF["prereg"]
(out / "attempt.json").write_text(json.dumps(manifest))
if not CONF.get("silent"):
    for case in cases:
        for key in CONF["subsets"].get(case, []):
            print("ASSERT " + case + "." + key + " PASS fixture observation")
print("fixture driver done")
"""


class Results:
    def __init__(self) -> None:
        self.clauses: dict[str, list[tuple[bool, str]]] = {c: [] for c in CLAUSES}
        self.asserts: dict[str, list[tuple[bool, str]]] = {"a6": [], "a7": []}

    def add(self, clauses, asserts, ok: bool, label: str, detail: str = "") -> bool:
        for clause in clauses:
            self.clauses[clause].append((ok, label))
        for key in asserts:
            self.asserts[key].append((ok, label))
        print(
            f"CHECK {','.join(clauses)} {'PASS' if ok else 'FAIL'} {label}"
            + (f" :: {detail}" if detail and not ok else ""),
            flush=True,
        )
        return ok


R = Results()


# ------------------------------------------------------------------ helpers


def env() -> dict:
    return {k: v for k, v in os.environ.items() if not k.startswith(("CWR_", "PYTHON"))}


def run(cmd: list[str], cwd: Path = REPO) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT, env=env())


def py(root: Path, rel: str, *args: str) -> subprocess.CompletedProcess:
    return run([sys.executable, str(root / rel), "--root", str(root), *args], cwd=root)


def closure(root: Path, *args: str) -> tuple[int, dict | None, str]:
    proc = py(root, CLOSURE_REL, "--json", *args)
    try:
        report = json.loads(proc.stdout)
    except ValueError:
        report = None
    return proc.returncode, report, (proc.stdout[-600:] + proc.stderr[-600:])


def closure_text(root: Path) -> tuple[int, str]:
    proc = py(root, CLOSURE_REL)
    return proc.returncode, proc.stdout + proc.stderr


def validator(root: Path, *args: str) -> tuple[int, dict | None, str]:
    proc = run([sys.executable, str(root / VALIDATOR_REL), *args], cwd=root)
    try:
        report = json.loads(proc.stdout)
    except ValueError:
        report = None
    return proc.returncode, report, proc.stdout[-1500:] + proc.stderr[-500:]


def kvx(text: str) -> dict:
    sections, current = {}, None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if re.fullmatch(r"\[.+\]", line):
            current = sections.setdefault(line[1:-1], {})
        elif current is not None and "=" in line:
            key, _, value = line.partition("=")
            current[key.strip()] = json.loads(value)
    return sections


def set_key(text: str, section: str, key: str, value) -> str:
    lines = text.splitlines(keepends=True)
    inside, hits, header_at = False, 0, None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if re.fullmatch(r"\[.+\]", stripped):
            inside = stripped == f"[{section}]"
            if inside:
                header_at = i
            continue
        if inside and re.match(rf"{re.escape(key)}\s*=", stripped):
            lines[i] = f"{key} = {json.dumps(value)}\n"
            hits += 1
    if header_at is None:
        raise AssertionError(f"no section [{section}]")
    if hits == 0:
        lines.insert(header_at + 1, f"{key} = {json.dumps(value)}\n")
    return "".join(lines)


def sha_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def digest(path: Path) -> str | None:
    return sha_bytes(path.read_bytes()) if path.is_file() else None


def index_line(entry: dict) -> str:
    return json.dumps(entry, sort_keys=True, separators=(",", ":"))


def clone(src: Path, dst: Path) -> Path:
    shutil.copytree(src, dst, symlinks=True)
    return dst


def blocking_for(report: dict | None, needle: str) -> list[str]:
    return [b for b in (report or {}).get("blocking", []) if needle in b]


# ------------------------------------------------------------------ part A: this checkout, read-only


def checkout_closure() -> dict | None:
    spec_cases = sorted(k[5:] for k in kvx((REPO / SPEC_REL).read_text()) if k.startswith("case."))
    watched = [REGISTRY_REL, INDEX_REL, SPEC_REL, QUAL_REL]
    before = {rel: digest(REPO / rel) for rel in watched}
    code, report, tail = closure(REPO)
    code_text, text = closure_text(REPO)
    after = {rel: digest(REPO / rel) for rel in watched}
    R.add(
        ("14.4",),
        ("a7",),
        report is not None and code in (0, 1),
        "closure assembles a report on this checkout",
        tail,
    )
    if report is None:
        return None
    listed = [c["case"] for c in report["cases"]]
    R.add(
        ("14.4",),
        ("a7",),
        sorted(listed) == spec_cases and len(listed) == len(set(listed)) == len(spec_cases),
        f"closure lists each of the {len(spec_cases)} spec cases exactly once",
        f"listed={listed}",
    )
    mismatched = []
    for case in report["cases"]:
        proc = py(REPO, CHECKER_REL, "--status", case["case"])
        derived = json.loads(proc.stdout)
        if (derived["status"], derived["evidence"]) != (case["status"], case["evidence"]):
            mismatched.append((case["case"], derived["status"], case["status"]))
    R.add(
        ("13.1", "14.4"),
        ("a7",),
        not mismatched,
        "every listed outcome equals the checker's own derivation from the attempt index "
        "(unexecuted cases stay UNRUN)",
        str(mismatched),
    )
    unnamed = [
        c["case"]
        for c in report["cases"]
        if c["status"] != "PASS" and not blocking_for(report, f"{c['case']}: ")
    ]
    R.add(
        ("14.4",),
        ("a7",),
        not unnamed,
        "every case without an executed PASS is named as a blocking item",
        str(unnamed),
    )
    consistent = (report["release_claim"] is (not report["blocking"])) and (
        code == (0 if report["release_claim"] else 1)
    )
    verdict_line = [line for line in text.splitlines() if line.startswith("CLOSURE VERDICT")]
    R.add(
        ("14.4",),
        ("a7",),
        consistent
        and code_text == code
        and len(verdict_line) == 1
        and ("REFUSED" in verdict_line[0]) == bool(report["blocking"]),
        "the verdict and exit code refuse the release claim exactly when an item blocks",
        f"exit={code} blocking={len(report['blocking'])} verdict={verdict_line}",
    )
    R.add(
        ("14.4",),
        (),
        before == after,
        "assembling closure changes no byte of the registry, attempt index, spec or qualification",
        f"before={before} after={after}",
    )
    source = (REPO / CLOSURE_REL).read_text()
    support = (REPO / SUPPORT_REL).read_text()

    def imported(text: str) -> set:
        tree = ast.parse(text)
        return {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            (node.module or "").split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }

    # closure may share tests/cwr/runner_support.py with the runner; that module is stdlib-only too
    foreign = sorted(
        i
        for i in imported(source) | imported(support)
        if i not in sys.stdlib_module_names and i not in ("__future__", "runner_support")
    )
    R.add(
        ("14.4", "13.1"),
        (),
        not foreign
        and all(
            "validate_spec" not in s and "authoring-validation" not in s for s in (source, support)
        ),
        "closure and its runner support module import only the standard library and never read "
        "the authoring validator or its report",
        f"foreign={foreign}",
    )
    return report


# ------------------------------------------------------------------ part B: a temporary repository root


def build_root(work: Path) -> tuple[Path, dict, str]:
    """A repository root with copies of the programs under test and fixture stand-ins for the product."""
    root = work / "base"
    shutil.copytree(
        PACK,
        root / "spec/cwr",
        ignore=shutil.ignore_patterns("__pycache__", "authoring-validation.json"),
    )
    spec = kvx((root / SPEC_REL).read_text())
    # the pack is copied in whatever state this checkout holds; the root starts at the design stage
    text = (root / SPEC_REL).read_text()
    text = set_key(set_key(text, "meta", "status", "pending"), "meta", "stage", "design")
    for name in spec:
        if name.startswith("task."):
            text = set_key(text, name, "status", "pending")
        elif name.startswith("case."):
            text = set_key(text, name, "status", "UNRUN")
    (root / SPEC_REL).write_text(text)
    (root / QUAL_REL).write_text(
        "# Fixture qualification file of a temporary root built by tests/cwr/cases/6_2.py.\n\n"
        "[progress.authoring_validation]\n"
        'revision = "fixture"\n'
        'command = "python3 spec/cwr/tools/validate_spec.py --render"\n'
        "exit_code = 0\n"
        'log = "spec/cwr/authoring-validation.json"\n'
        'observed = "fixture"\n'
        'assumption = "fixture"\n'
        "product_evidence = false\n"
    )
    for rel in (QUALIFY_REL, CHECKER_REL, SCHEMA_REL, CLOSURE_REL, SUPPORT_REL):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / rel, root / rel)
    for rel in (
        "crates/cwr-compiler/src/lib.rs",
        "crates/cwr-kernel/src/lib.rs",
        "sdk/python/cwr/__init__.py",
    ):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(f"# fixture source tree {rel}\n")
    prereg = (
        "# Fixture pre-registration of a temporary root.\n\n[meta]\n"
        'format = "cwr.preregistration"\nstatus = "registered"\n'
    )
    (root / "benchmark").mkdir(parents=True)
    (root / "benchmark/preregistration.kvx").write_text(prereg)
    # campaign task labels are verified against the suite
    (root / "benchmark/suite.kvx").write_text(
        '[task.t001-copy-readme]\nbin = "short"\n'
        if not (REPO / "benchmark/suite.kvx").is_file()
        else (REPO / "benchmark/suite.kvx").read_text()
    )
    prereg_digest = sha_bytes(prereg.encode())
    for n, rel in enumerate(REPORTS, 1):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(
            f"# H{n} fixture report\n\nPre-registration: benchmark/preregistration.kvx {prereg_digest}\n"
        )
    registry_text = (REPO / REGISTRY_REL).read_text()
    registry = kvx(registry_text)
    registry_text = set_key(
        set_key(registry_text, "meta", "index_length", 0), "meta", "index_head", None
    )
    subsets_by_task: dict[str, dict[str, list[str]]] = {}
    for name, case in registry.items():
        if not name.startswith("case."):
            continue
        ident = name[5:]
        registry_text = set_key(registry_text, name, "status", "UNRUN")
        registry_text = set_key(registry_text, name, "evidence", None)
        if ident == "CWR-019":
            continue
        registry_text = set_key(registry_text, name, "registered_builds", [B1])
        registry_text = set_key(registry_text, name, "registered_seeds", [])
        registry_text = set_key(registry_text, name, "cassette_digest", None)
        if case.get("tier") == "campaign":
            registry_text = set_key(
                registry_text, name, "matrix", {"H1": {"arms": ["frozen"], "tasks": 1, "seeds": 1}}
            )
            registry_text = set_key(registry_text, name, "preregistration_digest", prereg_digest)
        for task, keys in case["subsets"].items():
            subsets_by_task.setdefault(task, {})[ident] = keys
        (root / "tests/cwr/oracles").mkdir(parents=True, exist_ok=True)
        (root / "tests/cwr/oracles" / (ident.replace("-", "_") + ".py")).write_text(FIXTURE_ORACLE)
    (root / REGISTRY_REL).write_text(registry_text)
    (root / "tests/cwr/cases").mkdir(parents=True, exist_ok=True)
    for name in spec:
        if name.startswith("task."):
            task = name[5:]
            conf = {
                "task": task,
                "build": B1,
                "model": MODEL,
                "subsets": subsets_by_task.get(task, {}),
                "prereg": prereg_digest,
            }
            write_driver(root, task, conf)
    return root, subsets_by_task, prereg_digest


def write_driver(root: Path, task: str, conf: dict) -> None:
    path = root / "tests/cwr/cases" / (task.replace(".", "_") + ".py")
    path.write_text(DRIVER_TEMPLATE.format(task=task, conf=json.dumps(conf)))


def import_cwr019(root: Path) -> str:
    """Bring in this checkout's accepted CWR-019 case record (re-indexed), or qualify it afresh."""
    proc = py(REPO, CHECKER_REL, "--status", "CWR-019")
    derived = json.loads(proc.stdout) if proc.stdout.strip() else {}
    rel = derived.get("evidence") if derived.get("status") == "PASS" else None
    if rel:
        entry = next(
            json.loads(line)
            for line in (REPO / INDEX_REL).read_text().splitlines()
            if json.loads(line)["record"] == rel
        )
        shutil.copytree(REPO / rel[: -len(".json")], root / rel[: -len(".json")])
        shutil.copyfile(REPO / rel, root / rel)
        entry.update(seq=0, prev=GENESIS)
        line = index_line(entry)
        (root / INDEX_REL).write_text(line + "\n")
        text = (root / REGISTRY_REL).read_text()
        text = set_key(text, "meta", "index_length", 1)
        text = set_key(text, "meta", "index_head", sha_bytes(line.encode()))
        text = set_key(text, "case.CWR-019", "status", "PASS")
        text = set_key(text, "case.CWR-019", "evidence", rel)
        (root / REGISTRY_REL).write_text(text)
        return f"imported {rel}"
    shutil.copyfile(REPO / "tests/cwr/cases/0_2.py", root / "tests/cwr/cases/0_2.py")
    qproc = py(root, QUALIFY_REL, "--case", "CWR-019")
    return f"qualified afresh with the real task 0.2 driver: exit {qproc.returncode}"


def qualify(root: Path, case: str, task: str | None = None) -> tuple[int, str]:
    args = ["--case", case] + (["--task", task] if task else [])
    proc = py(root, QUALIFY_REL, *args)
    return proc.returncode, proc.stdout[-400:] + proc.stderr[-400:]


def record_journey_fixture(root: Path) -> None:
    text = (root / QUAL_REL).read_text()
    text += (
        "\n[progress.acceptance_journey]\n"
        'revision = "fixture"\ncommand = "python3 tests/cwr/closure.py --journey"\nexit_code = 0\n'
        f'log = "{QUAL_REL}"\nobserved = "fixture journey"\nassumption = "fixture"\n'
        f"product_evidence = false\nbuilds = {json.dumps([B1])}\n"
    )
    (root / QUAL_REL).write_text(text)


def synthetic(work: Path) -> None:
    root, subsets, _ = build_root(work)
    how = import_cwr019(root)
    status019 = json.loads(py(root, CHECKER_REL, "--status", "CWR-019").stdout)
    R.add(
        ("14.4",),
        ("a6", "a7"),
        status019["status"] == "PASS" and not status019["integrity_errors"],
        f"CWR-019 evidence in the temporary root is an accepted PASS ({how})",
        json.dumps(status019)[:400],
    )
    record_journey_fixture(root)
    accept = py(root, CLOSURE_REL, "--accept", "--owner", "fixture-owner")
    R.add(
        ("14.4",),
        ("a7",),
        accept.returncode == 0
        and kvx((root / QUAL_REL).read_text()).get("progress.owner_acceptance", {}).get("builds")
        == [B1],
        "closure --accept records owner acceptance referencing the journey's exact Build hashes",
        accept.stderr[-300:],
    )
    cases = sorted(k[5:] for k in kvx((root / SPEC_REL).read_text()) if k.startswith("case."))
    failures = []
    for case in cases:
        if case in ("CWR-019", MISSING):
            continue
        code, tail = qualify(root, case)
        if code != 0:
            failures.append((case, code, tail))
    R.add(
        ("14.4",),
        ("a6", "a7"),
        not failures,
        f"the real runner qualifies every fixture case except {MISSING} as PASS",
        str(failures)[:800],
    )

    # one case missing: closure names it and refuses
    missing_root = clone(root, work / "missing")
    code, report, tail = closure(missing_root)
    named = blocking_for(report, f"{MISSING}: missing")
    others = [b for b in (report or {}).get("blocking", []) if MISSING not in b]
    R.add(
        ("14.4",),
        ("a6",),
        code == 1
        and report is not None
        and report["verdict"] == "REFUSED"
        and not report["release_claim"]
        and len(named) == 1
        and not others,
        f"closure with {MISSING} missing names exactly that case and refuses the release claim",
        f"exit={code} named={named} others={others[:3]} {tail if report is None else ''}",
    )
    code_text, text = closure_text(missing_root)
    R.add(
        ("14.4",),
        ("a6",),
        code_text == 1
        and f"CLOSURE CASE {MISSING} UNRUN" in text
        and "CLOSURE VERDICT RELEASE CLAIM REFUSED" in text,
        "the printed closure lists the missing case as UNRUN and the release claim as REFUSED",
        text[-400:],
    )

    # component evidence never qualifies the case (13.4)
    component_root = clone(missing_root, work / "component")
    task = sorted(subsets_task for subsets_task, m in subsets.items() if MISSING in m)[0]
    qcode, qtail = qualify(component_root, MISSING, task)
    code, report, tail = closure(component_root)
    entry = next((c for c in (report or {}).get("cases", []) if c["case"] == MISSING), {})
    R.add(
        ("13.4",),
        (),
        qcode == 0
        and entry.get("status") == "UNRUN"
        and entry.get("component_records") == 1
        and code == 1
        and blocking_for(
            report, f"{MISSING}: missing: no executed case-scope attempt (1 component"
        ),
        f"a component PASS (task {task}) leaves {MISSING} missing and closure still refuses",
        f"qualify={qcode} {qtail} entry={entry} exit={code}",
    )

    # an apply over the store with one case missing: statuses only from registry outcomes (13.1)
    apply_root = clone(missing_root, work / "apply")
    apply = py(apply_root, CLOSURE_REL, "--apply")
    spec_after = kvx((apply_root / SPEC_REL).read_text())
    case_status = {k[5:]: v["status"] for k, v in spec_after.items() if k.startswith("case.")}
    task_status = {k[5:]: v["status"] for k, v in spec_after.items() if k.startswith("task.")}
    expected_cases = {c: ("UNRUN" if c == MISSING else "PASS") for c in case_status}
    R.add(
        ("13.1",),
        (),
        apply.returncode == 0
        and spec_after["meta"]["stage"] == "implementation"
        and spec_after["meta"]["status"] == "in_progress"
        and case_status == expected_cases,
        f"closure --apply writes case statuses from registry outcomes only: {MISSING} stays UNRUN",
        f"exit={apply.returncode} {apply.stderr[-300:]} statuses={case_status}",
    )
    qual = kvx((apply_root / QUAL_REL).read_text())
    evidence_entries = [v for k, v in qual.items() if k.startswith("progress.attempt_")]
    R.add(
        ("13.1",),
        (),
        set(task_status.values()) == {"implemented"}
        and evidence_entries
        and all(v["product_evidence"] is True and v.get("evidence") for v in evidence_entries),
        "every task whose driver ran and exited 0 is implemented; every driver run is recorded with "
        "its accepted evidence record",
        f"tasks={sorted(set(task_status.values()))} entries={len(evidence_entries)}",
    )
    render_code, _, render_tail = validator(apply_root, "--render")
    check_code, check_report, check_tail = validator(apply_root)
    R.add(
        ("13.1", "14.1"),
        (),
        render_code == 0 and check_code == 0 and (check_report or {}).get("status") == "PASS",
        "after --apply and --render the validator passes at the implementation stage, re-checking "
        "every case outcome and product-evidence entry against the checker",
        (render_tail + check_tail)[-1200:],
    )
    tampered = clone(apply_root, work / "tamper-status")
    text = set_key((tampered / SPEC_REL).read_text(), f"case.{MISSING}", "status", "PASS")
    (tampered / SPEC_REL).write_text(text)
    validator(tampered, "--render")
    code, vreport, vtail = validator(tampered)
    R.add(
        ("13.1",),
        (),
        code != 0 and any(f"case {MISSING}" in e for e in (vreport or {}).get("errors", [])),
        f"the validator refuses {MISSING} marked PASS without an executed attempt behind it",
        vtail[-600:],
    )
    design = clone(apply_root, work / "design")
    text = (design / SPEC_REL).read_text()
    text = set_key(set_key(text, "meta", "status", "pending"), "meta", "stage", "design")
    (design / SPEC_REL).write_text(text)
    code, vreport, vtail = validator(design)
    errors = (vreport or {}).get("errors", [])
    R.add(
        ("13.1",),
        (),
        code != 0
        and any("design-stage tasks must remain pending" in e for e in errors)
        and any("product qualification must remain UNRUN" in e for e in errors),
        "at the design stage the validator refuses non-pending tasks and executed case statuses",
        vtail[-600:],
    )
    stale = clone(apply_root, work / "stale-view")
    text = (
        (stale / SPEC_REL)
        .read_text()
        .replace('title = "Documentation and release"', 'title = "Documentation and releases"', 1)
    )
    (stale / SPEC_REL).write_text(text)
    code, vreport, vtail = validator(stale)
    R.add(
        ("14.1",),
        (),
        code != 0
        and any("derived view is missing/stale" in e for e in (vreport or {}).get("errors", [])),
        "an edited requirement makes the generated implementation plan and traceability stale",
        vtail[-600:],
    )
    untraced = clone(missing_root, work / "untraced")
    text = set_key(
        (untraced / SPEC_REL).read_text(),
        "req.14",
        "ac_5",
        "A fixture clause SHALL be traced to a task and a case.",
    )
    (untraced / SPEC_REL).write_text(text)
    code, report, tail = closure(untraced)
    R.add(
        ("14.1",),
        (),
        code == 1 and blocking_for(report, "clause 14.5: not traced to a task and a case"),
        "closure names a clause that maps to no task and no case and refuses",
        tail,
    )
    R.add(
        ("14.1",),
        (),
        report is not None
        and all(c["tasks"] and c["cases"] for c in report["clauses"] if c["clause"] != "14.5"),
        "closure traces every other clause to at least one task and one case",
        "",
    )

    # complete store: closure lists every case PASS with owner acceptance and accepts
    code, qtail = qualify(root, MISSING)
    complete = root
    code, report, tail = closure(complete)
    R.add(
        ("14.4",),
        ("a7",),
        code == 0
        and report is not None
        and report["release_claim"]
        and report["verdict"] == "ACCEPTED"
        and [c["status"] for c in report["cases"]] == ["PASS"] * len(cases)
        and all(c["evidence"] for c in report["cases"])
        and report["owner_acceptance"].get("recorded")
        and report["owner_acceptance"].get("accepted_by") == "fixture-owner"
        and all(
            report["hypotheses"][h]["carries_preregistration"] for h in ("H1", "H2", "H3", "H4")
        ),
        "with every case executed PASS, the four reports pre-registered and owner acceptance "
        "recorded, closure lists every case with its outcome and evidence and accepts the claim",
        f"exit={code} blocking={(report or {}).get('blocking', [])[:4]} {tail if report is None else ''}",
    )
    code_text, text = closure_text(complete)
    R.add(
        ("14.4",),
        ("a7",),
        code_text == 0
        and text.count("CLOSURE CASE ") == len(cases)
        and "CLOSURE OWNER-ACCEPTANCE recorded by fixture-owner" in text
        and "CLOSURE VERDICT RELEASE CLAIM ACCEPTED" in text,
        "the printed closure lists every case and the recorded owner acceptance",
        text[-400:],
    )

    # every refusal names its blocking item
    def variant(name: str, mutate, needle: str, clauses=("14.4",), asserts=()) -> None:
        target = clone(complete, work / name)
        mutate(target)
        vcode, vreport, vtail = closure(target)
        hits = blocking_for(vreport, needle)
        R.add(
            clauses,
            asserts,
            vcode == 1 and vreport is not None and not vreport["release_claim"] and bool(hits),
            f"{name}: closure refuses and names '{needle}'",
            f"exit={vcode} blocking={(vreport or {}).get('blocking', [])[:3]} {vtail if vreport is None else ''}",
        )

    def tamper_ledger(target: Path) -> None:
        rel = json.loads(py(target, CHECKER_REL, "--status", "CWR-003").stdout)["evidence"]
        ledger = sorted((target / rel[: -len(".json")]).rglob("*.jsonl"))[0]
        os.chmod(ledger, 0o644)
        ledger.write_text(ledger.read_text().replace('"fs.read"', '"fs.write"', 1))

    def stale_kernel(target: Path) -> None:
        (target / "crates/cwr-kernel/src/lib.rs").write_text("# a later kernel revision\n")

    def zero_assertion(target: Path) -> None:
        conf = {
            "task": "1.2",
            "build": B1,
            "model": MODEL,
            "subsets": subsets["1.2"],
            "silent": True,
        }
        write_driver(target, "1.2", conf)
        qualify(target, "CWR-003")

    def drop_report(target: Path) -> None:
        (target / REPORTS[2]).unlink()

    def draft_prereg(target: Path) -> None:
        path = target / "benchmark/preregistration.kvx"
        path.write_text(path.read_text().replace('"registered"', '"draft"'))

    def drop_acceptance(target: Path) -> None:
        text = (target / QUAL_REL).read_text()
        (target / QUAL_REL).write_text(text.split("[progress.owner_acceptance]")[0].rstrip() + "\n")

    def unbacked_acceptance(target: Path) -> None:
        path = target / QUAL_REL
        path.write_text(path.read_text().replace(json.dumps([B1]), json.dumps([B2])))

    variant("tampered", tamper_ledger, "CWR-003: tampered or damaged evidence", asserts=("a7",))
    variant("stale-build", stale_kernel, "stale-Build evidence", asserts=("a7",))
    variant(
        "zero-assertion",
        zero_assertion,
        "CWR-003: unresolved required failure: FAIL",
        asserts=("a7",),
    )
    variant(
        "no-report",
        drop_report,
        "H3 report research/h3-long-context.md is missing",
        ("14.4", "13.4"),
    )
    variant("draft-prereg", draft_prereg, "not 'registered'", ("14.4", "13.4"))
    variant("no-acceptance", drop_acceptance, "owner acceptance is not recorded", asserts=("a7",))
    variant(
        "unbacked-acceptance",
        unbacked_acceptance,
        "are not used by any PASS case evidence",
        asserts=("a7",),
    )


# ------------------------------------------------------------------ main


def main() -> int:
    scratch = Path(tempfile.mkdtemp(prefix="cwr-6-2-"))
    evidence_dir = os.environ.get("CWR_EVIDENCE_DIR")
    try:
        real = checkout_closure()
        synthetic(scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if evidence_dir and real is not None:
        out = Path(evidence_dir)
        (out / "closure-report.json").write_text(json.dumps(real, indent=2, sort_keys=True) + "\n")
        (out / "attempt.json").write_text(json.dumps({"final_states": ["closure-report.json"]}))
    if real is not None:
        print(
            f"VERDICT {real['verdict']} release_claim={real['release_claim']} "
            f"cases={real['case_counts']} blocking={len(real['blocking'])}"
        )
    for key, checks in R.asserts.items():
        ok = bool(checks) and all(c[0] for c in checks)
        print(
            f"ASSERT CWR-020.{key} {'PASS' if ok else 'FAIL'} {sum(c[0] for c in checks)}/{len(checks)} checks"
        )
    all_ok = True
    for clause, checks in R.clauses.items():
        ok = bool(checks) and all(c[0] for c in checks)
        all_ok &= ok
        failed = [label for passed, label in checks if not passed]
        print(
            f"CLAUSE {clause} {'PASS' if ok else 'FAIL'} "
            + (f"{len(checks)} checks passed" if ok else f"failed: {failed}")
        )
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())

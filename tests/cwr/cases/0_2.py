#!/usr/bin/env python3
# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Task 0.2 driver: qualification registry, evidence schema and independent checker.

Exercises clauses 13.2, 13.3 and 13.4 (case CWR-019) against the real programs
tests/cwr/qualify.py, tests/cwr/check_evidence.py, tests/cwr/evidence.schema.json, tests/cwr/registry.kvx
and the implementation-stage rules of spec/cwr/tools/validate_spec.py, all run as subprocesses.

Every scenario runs in a temporary repository root that holds copies of spec.kvx (or the whole pack)
and the registry (with an empty attempt index), plus small fixture task drivers that stand in for
future task drivers (they print ASSERT lines, write ledgers, final state and an attempt.json manifest)
and fixture oracles tests/cwr/oracles/CWR_NNN.py that recompute fixture assertions from ledgers and
final state. The fixtures are inputs; the runner, checker, schema, registry and validator under test
are the real files of this checkout. The real registry and evidence directory are never touched.

Forgery traps go beyond relabelling: fully self-consistent fabricated records (every digest
recomputed, even appended to the attempt index with the registry head re-pinned), ledger content
edits with recomputed digests, backdated and future-dated records, deleted records and index entries.

The driver also builds a CWR-019 trap suite (a small repository written by the real runner: registry,
attempt index, a genuine PASS, a component PASS and a zero-assertion attempt) and qualifies a stand-in
CWR-019 attempt carrying it, so the checker's built-in CWR-019 oracle is exercised end to end. Under
tests/cwr/qualify.py (CWR_EVIDENCE_DIR set) the suite is written there as attempt-level final state,
which is what the checker recomputes CWR-019 from.

Prints "CHECK <clause> PASS|FAIL ..." per check, "ASSERT CWR-019.aN PASS|FAIL ..." per registered
assertion (the qualification protocol of qualify.py) and one "CLAUSE <req.ac> PASS|FAIL <evidence>"
line per owned clause. Exits 0 only when every clause passes. Python 3.12, standard library only.
Nothing is compiled, so CARGO_TARGET_DIR is not consulted.
"""

from __future__ import annotations

import ast
import contextlib
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
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
QUALIFY = REPO / "tests/cwr/qualify.py"
CHECKER = REPO / "tests/cwr/check_evidence.py"
SCHEMA = REPO / "tests/cwr/evidence.schema.json"
REGISTRY = REPO / "tests/cwr/registry.kvx"
SPEC = REPO / "spec/cwr/spec.kvx"
PACK = REPO / "spec/cwr"
VALIDATOR_REL = "spec/cwr/tools/validate_spec.py"
TIMEOUT = 300

B1 = "sha256:" + "b1" * 32  # the Build registered for fixture cases
B2 = "sha256:" + "b2" * 32  # a different, unregistered Build
CASSETTES = "sha256:" + "c0" * 32
PREREG = "sha256:" + "e0" * 32
MODEL = {"identity": "fixture-model-id", "sampling": {"temperature": 0}}
GENESIS = "sha256:" + "0" * 64
INDEX_REL = "tests/cwr/evidence/index.jsonl"
SUITE_NAME = "cwr019-suite.json"
ORACLE_CASES = ("CWR-005", "CWR-006", "CWR-009", "CWR-013", "CWR-014", "CWR-018")
SUITE = REPO / "benchmark/suite.kvx"
TASKS = REPO / "benchmark/tasks"
QUALIFICATION = REPO / "spec/cwr/qualification.kvx"
CASES_DIR = REPO / "tests/cwr/cases"

FIXTURE_ORACLE = '''"""Fixture oracle planted by tests/cwr/cases/0_2.py in a temporary repository root.

Recomputes every assertion from ledgers and final state only: it holds iff the attempt has an Instance,
every ledger holds at least one primitive record, every primitive outcome is ok and every Instance has
a final state that is a JSON object.
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
        bad = [r.get("seq") for r in prims if (r.get("outcome") or {}).get("status") != "ok"]
        if bad:
            problems.append("instance " + inst["id"] + ": primitive records " + str(bad) + " are not ok")
        final = inst.get("final_state")
        if final is None or final["path"] not in ctx.final_states:
            problems.append("instance " + inst["id"] + ": no verified final state")
        elif not isinstance(json.loads(ctx.final_states[final["path"]].decode()), dict):
            problems.append("instance " + inst["id"] + ": final state is not an object")
    verdict = ("FAIL", "; ".join(problems)) if problems else ("PASS", "ledger primitives ok and final state present")
    return {ident: verdict for ident in ctx.expected}
'''

CLAUSES = ("13.2", "13.3", "13.4")
ASSERTIONS = {f"CWR-019.a{n}": [] for n in range(1, 9)}


class Results:
    def __init__(self) -> None:
        self.checks: dict[str, list[tuple[bool, str]]] = {c: [] for c in CLAUSES}

    def add(self, clauses, assertions, ok: bool, label: str, detail: str = "") -> bool:
        for clause in clauses:
            self.checks[clause].append((ok, label))
        for assertion in assertions:
            ASSERTIONS[f"CWR-019.{assertion}"].append((ok, label))
        print(
            f"CHECK {','.join(clauses)} {'PASS' if ok else 'FAIL'} {label}"
            + (f" :: {detail}" if detail else ""),
            flush=True,
        )
        return ok


R = Results()


# ------------------------------------------------------------------ environment builders


def set_key(text: str, section: str, key: str, value) -> str:
    """Set key in [section] of a KVX document (replace or insert after the header)."""
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


def drop_keys(text: str, section: str, pattern: str) -> str:
    out, inside = [], False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if re.fullmatch(r"\[.+\]", stripped):
            inside = stripped == f"[{section}]"
        elif inside and re.match(pattern, stripped):
            continue
        out.append(line)
    return "".join(out)


def make_root(work: Path, name: str, edits: dict | None = None, full_pack: bool = False) -> Path:
    root = work / name
    if full_pack:
        shutil.copytree(
            PACK,
            root / "spec/cwr",
            ignore=shutil.ignore_patterns("__pycache__", "authoring-validation.json"),
        )
        # hermetic: keep only the authoring entry; the checkout's attempt entries name records that
        # do not exist in this root
        qual = (root / "spec/cwr/qualification.kvx").read_text()
        keep = re.search(r"^\[progress\.authoring_validation\]\n.*?(?=^\[|\Z)", qual, re.M | re.S)
        head = "".join(ln for ln in qual.splitlines(keepends=True)[:2] if ln.startswith("#"))
        (root / "spec/cwr/qualification.kvx").write_text(
            head + "\n" + (keep.group(0).rstrip("\n") + "\n" if keep else "")
        )
    else:
        (root / "spec/cwr").mkdir(parents=True)
        shutil.copyfile(SPEC, root / "spec/cwr/spec.kvx")
    (root / "tests/cwr/cases").mkdir(parents=True)
    text = REGISTRY.read_text()
    # a fresh evidence store: empty attempt index, every case UNRUN
    text = set_key(set_key(text, "meta", "index_length", 0), "meta", "index_head", None)
    for ident in [m for m in re.findall(r"^\[case\.(CWR-\d{3})\]", text, re.M)]:
        text = set_key(
            set_key(text, f"case.{ident}", "status", "UNRUN"), f"case.{ident}", "evidence", None
        )
    for (section, key), value in (edits or {}).items():
        text = set_key(text, section, key, value)
    (root / "tests/cwr/registry.kvx").write_text(text)
    # the checker and schema travel with the registry so the validator in this root can load them
    shutil.copyfile(CHECKER, root / "tests/cwr/check_evidence.py")
    shutil.copyfile(SCHEMA, root / "tests/cwr/evidence.schema.json")
    # campaign task labels are verified against the suite
    (root / "benchmark").mkdir(parents=True)
    (root / "benchmark/suite.kvx").write_text(suite_text())
    for task in suite_task_ids():
        source = TASKS / task / "initial.kvx"
        if source.is_file():
            (root / "benchmark/tasks" / task).mkdir(parents=True)
            shutil.copyfile(source, root / "benchmark/tasks" / task / "initial.kvx")
    (root / "tests/cwr/oracles").mkdir(parents=True)
    for case in ORACLE_CASES:
        (root / "tests/cwr/oracles" / (case.replace("-", "_") + ".py")).write_text(FIXTURE_ORACLE)
    for crate in ("cwr-compiler", "cwr-kernel"):
        (root / "crates" / crate / "src").mkdir(parents=True)
        (root / "crates" / crate / "src/lib.rs").write_text(f"// fixture source tree for {crate}\n")
    return root


DRIVER_TEMPLATE = r"""#!/usr/bin/env python3
# Fixture task driver generated by tests/cwr/cases/0_2.py; stands in for a future task driver.
import json, os, subprocess, sys, time
from pathlib import Path
CONF = json.loads({conf!r})
out = Path(os.environ["CWR_EVIDENCE_DIR"])
print("evidence directory " + str(out))
instances = []
for inst in CONF.get("instances", []):
    ledger = out / "ledgers" / (inst["id"] + ".jsonl")
    ledger.parent.mkdir(parents=True, exist_ok=True)
    build = inst.get("build", CONF.get("build_hash"))
    base = {{"instance": inst["id"], "build": build, "experimental": CONF.get("experimental")}}
    rows = [dict(base, seq=0, turn=0, world_time=0, kind="header", primitive=None, seed=inst.get("ledger_seed", inst["seed"]),
                 tail_size=inst.get("tail_size", 20), arm=inst.get("arm"), model=CONF.get("model", {{}}).get("identity"),
                 created_from=None, initial_state=inst.get("initial_state"))]
    if inst.get("cassette_digest"):
        rows[0]["cassette_digest"] = inst["cassette_digest"]
    for n in range(1, 4):
        rows.append(dict(base, seq=n, turn=1, world_time=n, kind="primitive", primitive="fs.read", params_hash=None,
                         preconditions=[], outcome={{"status": "ok", "code": None, "shape": "Bytes", "side_effect": "read_only"}},
                         duration_us=10, host_ts="2026-11-06T12:00:00Z"))
    for k, request in enumerate(inst.get("requests", [])):
        rows.append(dict(base, seq=len(rows), turn=1, world_time=4 + k, kind="adapter", primitive="search.query",
                         params_hash=None, preconditions=[], gateway={{"request_hash": request, "seq": k, "via": "cassette"}},
                         outcome={{"status": "ok", "code": None, "shape": "list[SearchHit]", "side_effect": "external"}},
                         duration_us=10, host_ts="2026-11-06T12:00:00Z"))
    rows.append(dict(base, seq=len(rows), turn=1, world_time=14, kind="turn", primitive=None, status="ok"))
    ledger.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
    state = out / "state" / (inst["id"] + ".json")
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({{"fs": {{"/a": "x"}}, "kv": {{}}}}, sort_keys=True) + "\n")
    entry = {{k: inst.get(k) for k in ("id", "seed", "tail_size", "arm", "hypothesis", "task", "case") if k in inst}}
    if inst.get("initial_state"):
        entry["initial_state_digest"] = inst["initial_state"]
    entry.setdefault("tail_size", 20)
    entry.update(build=build, ledger="ledgers/" + inst["id"] + ".jsonl", final_state="state/" + inst["id"] + ".json")
    instances.append(entry)
if CONF.get("symlink") or CONF.get("hardlink"):
    (out / "ledgers").mkdir(parents=True, exist_ok=True)
    if CONF.get("symlink"):
        (out / "ledgers" / "leak.jsonl").symlink_to(CONF["symlink"])
    else:
        os.link(CONF["hardlink"], out / "ledgers" / "leak.jsonl")
    instances.append({{"id": "01LEAK", "seed": 1, "tail_size": 20, "build": CONF.get("build_hash"),
                      "ledger": "ledgers/leak.jsonl", "final_state": None}})
if CONF.get("manifest", True):
    manifest = {{k: CONF[k] for k in ("build_hash", "cassette_digest", "gateway_mode", "model", "preregistration_digest", "experimental_flags") if k in CONF}}
    manifest["instances"] = instances
    (out / "attempt.json").write_text(json.dumps(manifest))
for ident, outcome in CONF.get("asserts", []):
    print(f"ASSERT {{ident}} {{outcome}} fixture observation")
if CONF.get("blocked"):
    print("BLOCKED " + CONF["blocked"])
if CONF.get("spawn"):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"],
                             start_new_session=bool(CONF.get("own_session")))
    Path(CONF["spawn"]).write_text(str(child.pid))
    time.sleep(600)
print("fixture driver done")
sys.exit(CONF.get("exit", 0))
"""


PROTOCOL_BLOCKED_DRIVER = """#!/usr/bin/env python3
# Fixture task driver generated by tests/cwr/cases/0_2.py: speaks tests/cwr/cases/protocol.py.
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import protocol
store = {"13.2": []}
obs = protocol.Observer("1.1", protocol.pairs(store))
with obs.observe("CWR-002.a1"):
    store["13.2"].append((True, "the reachable check passed"))
if UNPLACED:
    store["13.2"].append((False, "a check the driver attributes to no assertion failed"))
obs.blocked("no Build host")
obs.emit()
sys.exit(3)
"""


def suite_text() -> str:
    """This checkout's benchmark/suite.kvx, or a two-task stand-in where the root has none."""
    if SUITE.is_file():
        return SUITE.read_text()
    return "".join(f'[task.t00{n}-fixture]\nbin = "short"\n\n' for n in (1, 2))


def initial_class(task: str) -> str | None:
    """What benchmark/tasks/<task>/initial.kvx defines, without comments and [meta]; None if absent."""
    source = TASKS / task / "initial.kvx"
    if not source.is_file():
        return None
    lines, meta = [], False
    for line in source.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if re.fullmatch(r"\[.+\]", stripped):
            meta = stripped == "[meta]"
        if not meta:
            lines.append(stripped)
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def suite_task_ids() -> list[str]:
    return sorted(re.findall(r"^\[task\.(t\d{3}-[a-z0-9-]+)\]", suite_text(), re.M))


def write_driver(root: Path, task: str, conf: dict) -> None:
    path = root / "tests/cwr/cases" / (task.replace(".", "_") + ".py")
    path.write_text(DRIVER_TEMPLATE.format(conf=json.dumps(conf)))


def passing(case: str, keys, inst_id: str | None = None, **extra) -> dict:
    conf = {
        "asserts": [[f"{case}.{k}", "PASS"] for k in keys],
        "build_hash": B1,
        "cassette_digest": CASSETTES,
        "gateway_mode": "replay",
        "model": MODEL,
        "instances": [
            {"id": inst_id or f"01FIXTURE{case[-3:]}", "seed": 7, "tail_size": 20, "arm": "frozen"}
        ],
    }
    conf.update(extra)
    return conf


# ------------------------------------------------------------------ program runners


def run(cmd, cwd=REPO, stdin=None, env=None):
    env = {
        k: v
        for k, v in (env or os.environ).items()
        if not k.startswith("PYTHON") and not k.startswith("CWR_")
    }
    return subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT, input=stdin, env=env
    )


def qualify(root: Path, *args):
    proc = run([sys.executable, str(QUALIFY), "--root", str(root), *args])
    results = []
    for line in proc.stdout.splitlines():
        with contextlib.suppress(ValueError):
            results.append(json.loads(line))
    return proc.returncode, results, proc.stdout + proc.stderr


def check(root: Path, *paths):
    proc = run([sys.executable, str(CHECKER), "--root", str(root), *map(str, paths)])
    return proc.returncode, proc.stdout + proc.stderr


def status(root: Path, case: str) -> dict:
    proc = run([sys.executable, str(CHECKER), "--root", str(root), "--status", case])
    result = json.loads(proc.stdout)
    result["rc"] = proc.returncode
    return result


def registry_case(root: Path, case: str) -> dict:
    current, out = None, {}
    for line in (root / "tests/cwr/registry.kvx").read_text().splitlines():
        line = line.strip()
        if re.fullmatch(r"\[.+\]", line):
            current = line[1:-1]
        elif current == f"case.{case}" and "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            out[key.strip()] = json.loads(value)
    return out


def registry_digest(root: Path, case: str) -> str:
    """The registry digest a record pins: [meta] without the index pin and the case section without
    status and evidence, as canonical JSON (recomputed here independently of the runner)."""
    sections, current = {}, None
    for line in (root / "tests/cwr/registry.kvx").read_text().splitlines():
        line = line.strip()
        if re.fullmatch(r"\[.+\]", line):
            current = sections.setdefault(line[1:-1], {})
        elif current is not None and "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            current[key.strip()] = json.loads(value)
    meta = {k: v for k, v in sections["meta"].items() if k not in ("index_length", "index_head")}
    body = {k: v for k, v in sections[f"case.{case}"].items() if k not in ("status", "evidence")}
    text = json.dumps({"case": body, "meta": meta}, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def load(root: Path, rel: str) -> dict:
    return json.loads((root / rel).read_text())


def sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def forge(root: Path, rel: str, mutate, case: str | None = None, stamp: str | None = None) -> Path:
    """Copy an attempt (record and artifact directory) to a fresh id, then let mutate() tamper with it."""
    record = load(root, rel)
    old_id = record["record_id"]
    case = case or record["case"]
    case_dir = root / "tests/cwr/evidence" / case
    case_dir.mkdir(parents=True, exist_ok=True)
    stamp = stamp or old_id.split("/")[1].rsplit("-", 1)[0]
    n = 50
    while (case_dir / f"{stamp}-{n}.json").exists() or (case_dir / f"{stamp}-{n}").exists():
        n += 1
    new_id = f"{case}/{stamp}-{n}"
    new_dir = case_dir / f"{stamp}-{n}"
    shutil.copytree(root / "tests/cwr/evidence" / old_id, new_dir)
    for path in new_dir.rglob("*"):
        if path.is_file():
            os.chmod(path, 0o644)
    record = json.loads(json.dumps(record).replace(old_id, new_id))
    mutate(record, new_dir)
    out = case_dir / f"{stamp}-{n}.json"
    out.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    return out


def rehash_ledgers(record: dict, root: Path) -> None:
    """What a careful forger would do: recompute every ledger digest after editing a ledger."""
    for item in record["ledgers"]:
        item["sha256"] = sha(root / item["path"])
    for inst in record["instances"]:
        inst["ledger"]["sha256"] = sha(root / inst["ledger"]["path"])
    lines = "".join(
        f"{i['path']} {i['sha256']}\n" for i in sorted(record["ledgers"], key=lambda i: i["path"])
    )
    record["ledger_digest"] = "sha256:" + hashlib.sha256(lines.encode()).hexdigest()


def attempt_digest(directory: Path) -> str:
    files = sorted(
        (p for p in directory.rglob("*") if p.is_file()),
        key=lambda p: p.relative_to(directory).as_posix(),
    )
    lines = "".join(f"{p.relative_to(directory).as_posix()} {sha(p)}\n" for p in files)
    return "sha256:" + hashlib.sha256(lines.encode()).hexdigest()


def rehash_all(record: dict, root: Path) -> None:
    """A careful forger recomputes every digest the record carries after editing any artifact."""
    for inv in record["invocations"]:
        for kind in ("stdout", "stderr"):
            inv[f"{kind}_sha256"] = sha(root / inv[f"{kind}_log"])
    record["log"], record["log_sha256"] = (
        record["invocations"][0]["stdout_log"],
        record["invocations"][0]["stdout_sha256"],
    )
    for item in record["final_states"]:
        item["sha256"] = sha(root / item["path"])
    for inst in record["instances"]:
        if inst["final_state"]:
            inst["final_state"]["sha256"] = sha(root / inst["final_state"]["path"])
    if record["ledgers"]:
        rehash_ledgers(record, root)
    record["attempt_sha256"] = attempt_digest(root / "tests/cwr/evidence" / record["record_id"])


def canonical(entry: dict) -> str:
    return json.dumps(entry, sort_keys=True, separators=(",", ":"))


def pin_head(root: Path, lines: list[str]) -> None:
    head = ("sha256:" + hashlib.sha256(lines[-1].encode()).hexdigest()) if lines else None
    path = root / "tests/cwr/registry.kvx"
    path.write_text(
        set_key(
            set_key(path.read_text(), "meta", "index_head", head),
            "meta",
            "index_length",
            len(lines),
        )
    )


def index_append(root: Path, path: Path, record: dict, appended_at: str | None = None) -> None:
    """A careful forger with write access appends the forgery to the index and re-pins the head."""
    index = root / INDEX_REL
    lines = index.read_text().splitlines() if index.is_file() else []
    prev = ("sha256:" + hashlib.sha256(lines[-1].encode()).hexdigest()) if lines else GENESIS
    entry = {
        "seq": len(lines),
        "prev": prev,
        "record": str(path.relative_to(root)),
        "record_sha256": sha(path),
        "case": record["case"],
        "scope": record["scope"],
        "task": record["task"],
        "outcome": record["outcome"],
        "accepted": True,
        "appended_at": appended_at or utc_now(),
    }
    lines.append(canonical(entry))
    index.write_text("".join(line + "\n" for line in lines))
    pin_head(root, lines)


def utc_now() -> str:
    now = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)) + f".{int(now * 1000) % 1000:03d}Z"


def index_reseal(root: Path) -> None:
    """Rewrite every index entry's record digest to the file on disk, re-chain and re-pin the head."""
    index = root / INDEX_REL
    lines, prev = [], GENESIS
    for line in index.read_text().splitlines():
        entry = json.loads(line)
        if (root / entry["record"]).is_file():
            entry["record_sha256"] = sha(root / entry["record"])
        entry["prev"] = prev
        lines.append(canonical(entry))
        prev = "sha256:" + hashlib.sha256(lines[-1].encode()).hexdigest()
    index.write_text("".join(line + "\n" for line in lines))
    pin_head(root, lines)


def stamp_of(record: dict) -> str:
    return record["started_at"][:19].replace("-", "").replace(":", "") + "Z"


def claim_pass(record: dict) -> None:
    record["outcome"] = "PASS"
    record["diagnostics"] = []
    record["blocked_reason"] = None


def expect_reject(root: Path, path: Path, needle: str, clauses, assertions, label: str) -> None:
    rc, out = check(root, path)
    ok = rc != 0 and "REJECT" in out and needle in out
    R.add(
        clauses,
        assertions,
        ok,
        f"{label}: checker rc={rc}, names '{needle}'={needle in out}",
        "" if ok else out[-700:],
    )


def validator(root: Path, *args):
    proc = run([sys.executable, str(root / VALIDATOR_REL), *args], cwd=root)
    try:
        report = json.loads(proc.stdout)
    except ValueError:
        report = {"status": None, "errors": [proc.stdout[-300:] + proc.stderr[-300:]]}
    return proc.returncode, report


# ------------------------------------------------------------------ scenarios


def scenario_records(work: Path) -> str:
    """13.2: complete, immutable, append-only attempt records; returns a genuine PASS record path."""
    root = make_root(work, "records", {("case.CWR-005", "registered_builds"): [B1]})
    write_driver(root, "1.3", passing("CWR-005", ["a1", "a2", "a3", "a4"]))
    registry_before = registry_digest(root, "CWR-005")

    rc, res, out = qualify(root, "--case", "CWR-005")
    ok = rc == 0 and len(res) == 1 and res[0]["outcome"] == "PASS" and res[0]["accepted"]
    R.add(
        ["13.2"],
        ["a7"],
        ok,
        f"qualify --case CWR-005 with a passing driver: rc={rc} outcome={res and res[0].get('outcome')}",
        "" if ok else out[-800:],
    )
    if not ok:
        return ""
    rel = res[0]["record"]
    record = load(root, rel)
    expected_rel = f"tests/cwr/evidence/CWR-005/{stamp_of(record)}-1.json"
    R.add(
        ["13.2"],
        ["a7"],
        rel == expected_rel,
        f"record written at {rel}, named by its start time (expected {expected_rel})",
    )
    index = [json.loads(line) for line in (root / INDEX_REL).read_text().splitlines()]
    reg_meta = registry_case(root, "CWR-005")
    head = (
        "sha256:"
        + hashlib.sha256((root / INDEX_REL).read_text().splitlines()[-1].encode()).hexdigest()
    )
    meta_text = (root / "tests/cwr/registry.kvx").read_text()
    ok = (
        len(index) == 1
        and index[0]["record"] == rel
        and index[0]["record_sha256"] == sha(root / rel)
        and index[0]["prev"] == GENESIS
        and index[0]["seq"] == 0
        and index[0]["accepted"] is True
        and f"index_head = {json.dumps(head)}" in meta_text
        and "index_length = 1" in meta_text
        and reg_meta["status"] == "PASS"
        and reg_meta["evidence"] == rel
    )
    R.add(
        ["13.2"],
        ["a7"],
        ok,
        "the record is entry 0 of the hash-chained attempt index with its sha256, and the "
        "registry pins the index head and length and records the case outcome",
    )

    inst = record["instances"][0] if record["instances"] else {}
    ledger = root / inst.get("ledger", {}).get("path", "missing")
    header = json.loads(ledger.read_text().splitlines()[0]) if ledger.is_file() else {}
    log = root / record["log"]
    fields = {
        "case and assertion ids": record["case"] == "CWR-005"
        and record["assertion_ids"] == [f"CWR-005.a{n}" for n in range(1, 5)],
        "clauses": record["clauses"] == ["1.4", "2.4", "2.5", "4.5", "4.6", "9.2"],
        "tier and scope": record["tier"] == "component"
        and record["scope"] == "case"
        and record["task"] is None,
        "spec revision": record["spec_revision"] == sha(root / "spec/cwr/spec.kvx"),
        "registry and schema versions": record["registry_version"] == 1
        and record["schema_version"] == 2
        and record["registry_sha256"] == registry_before
        and record["schema_sha256"] == sha(SCHEMA),
        "Build hash": record["build_hash"] == B1
        and record["builds"] == [B1]
        and header.get("build") == B1,
        "Instance id": record["instance_id"] == "01FIXTURE005" == header.get("instance"),
        "seed": record["seed"] == 7 == header.get("seed"),
        "K": record["tail_size"] == 20 == header.get("tail_size"),
        "cassette set": record["cassette_digest"] == CASSETTES
        and record["gateway_mode"] == "replay",
        "model identity": record["model"] == MODEL,
        "compiler, kernel and product revisions": all(
            isinstance(record[k], str) and record[k].startswith("sha256:")
            for k in ("compiler_revision", "kernel_revision", "product_revision")
        ),
        "no host name and no machine path": "hostname" not in record["host"]
        and str(root) not in log.read_text()
        and "evidence directory <repo>/tests/cwr/evidence/.work/" in log.read_text(),
        "exit status and command": record["exit_code"] == 0
        and record["command"].startswith("python3 tests/cwr/qualify.py")
        and record["invocations"][0]["command"] == ["python3", "tests/cwr/cases/1_3.py"],
        "log location and hash": log.is_file()
        and sha(log) == record["log_sha256"]
        and "ASSERT CWR-005.a1 PASS" in log.read_text(),
        "ledger and final state digests": record["ledgers"][0]["sha256"] == sha(ledger)
        and record["final_states"][0]["sha256"] == sha(root / record["final_states"][0]["path"])
        and isinstance(record["ledger_digest"], str),
        "observed assertions": [(o["id"], o["outcome"]) for o in record["observed"]]
        == [(f"CWR-005.a{n}", "PASS") for n in range(1, 5)],
        "UTC start, end and timestamp": all(
            re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", record[k])
            for k in ("started_at", "ended_at", "timestamp")
        ),
        "checker identity and result": record["checker"]["program"] == "tests/cwr/check_evidence.py"
        and record["checker"]["accepted"] is True
        and record["checker"]["revision"] == sha(CHECKER),
        "independent recomputation": [(a["id"], a["outcome"]) for a in record["recomputed"]]
        == [(f"CWR-005.a{n}", "PASS") for n in range(1, 5)],
        "attempt directory digest": record["attempt_sha256"]
        == attempt_digest(root / "tests/cwr/evidence" / record["record_id"]),
    }
    bad = [k for k, v in fields.items() if not v]
    R.add(
        ["13.2"],
        ["a7"],
        not bad,
        f"record carries every evidence field with the observed values ({len(fields)} groups); wrong={bad}",
    )

    rc, out = check(root, root / rel)
    R.add(
        ["13.2", "13.3"],
        ["a7"],
        rc == 0 and "ACCEPT" in out,
        f"checker accepts the genuine record rc={rc}",
        "" if rc == 0 else out[-500:],
    )

    # every required field: a record missing it is rejected
    schema = json.loads(SCHEMA.read_text())
    missing_ok = []
    for fld in schema["required"]:
        path = forge(root, rel, lambda r, d, f=fld: r.pop(f))
        rc, out = check(root, path)
        missing_ok.append((fld, rc != 0 and f"missing required field {fld}" in out))
    bad = [f for f, ok in missing_ok if not ok]
    R.add(
        ["13.2"],
        ["a7"],
        not bad,
        f"a record missing any of the {len(missing_ok)} required fields is rejected; not rejected={bad}",
    )
    path = forge(root, rel, lambda r, d: r.update(note="extra"))
    rc, out = check(root, path)
    R.add(
        ["13.2"],
        ["a7"],
        rc != 0 and "unexpected field note" in out,
        f"a record with an unregistered field is rejected rc={rc}",
    )

    # append-only: a second attempt never overwrites the first; records are read-only; names are not chosen
    first = (root / rel).read_bytes()
    rc2, res2, out2 = qualify(root, "--case", "CWR-005")
    second = res2[0]["record"] if res2 else ""
    case_dir = root / "tests/cwr/evidence/CWR-005"
    now = time.time()
    sentinels = []
    for offset in range(0, 15):
        name = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now + offset)) + "-1.json"
        if not (case_dir / name).exists():
            (case_dir / name).write_text("sentinel: occupied record name\n")
            sentinels.append(case_dir / name)
    _rc3, res3, _ = qualify(root, "--case", "CWR-005")
    third = res3[0]["record"] if res3 else ""
    index = [json.loads(line) for line in (root / INDEX_REL).read_text().splitlines()]
    ok = (
        rc2 == 0
        and second != rel
        and (root / rel).read_bytes() == first
        and int(Path(third).stem.rsplit("-", 1)[1]) >= 2
        and Path(third).name not in {p.name for p in sentinels}
        and all(p.read_text() == "sentinel: occupied record name\n" for p in sentinels)
        and [e["record"] for e in index] == [rel, second, third]
        and (os.stat(root / rel).st_mode & 0o222) == 0
    )
    R.add(
        ["13.2"],
        ["a7"],
        ok,
        f"repeat attempts append {Path(second).name} and {Path(third).name} as index entries 1 and 2; "
        f"first record unchanged; occupied name skipped; mode={oct(os.stat(root / rel).st_mode & 0o777)}",
        "" if ok else out2[-500:],
    )
    proc = run(
        [
            sys.executable,
            str(QUALIFY),
            "--root",
            str(root),
            "--case",
            "CWR-005",
            "--timestamp",
            "20200101T000000Z",
        ]
    )
    R.add(
        ["13.2"],
        ["a7"],
        proc.returncode == 2 and "unrecognized arguments" in proc.stderr,
        f"a record name cannot be chosen: qualify --timestamp is refused rc={proc.returncode}",
    )
    st = status(root, "CWR-005")
    ok = (
        st["rc"] == 0
        and st["status"] == "PASS"
        and st["evidence"] == third == registry_case(root, "CWR-005")["evidence"]
        and {str(p.relative_to(root)) for p in sentinels}
        <= {
            r["record"]
            for r in st["rejected_records"]
            if "not in the append-only attempt index" in json.dumps(r)
        }
    )
    R.add(
        ["13.2"],
        ["a7"],
        ok,
        f"registry and --status agree on the latest attempt in index order ({Path(third).name}); "
        f"the {len(sentinels)} unindexed files are reported, not counted",
    )

    # BLOCKED records the missing resource and is not a PASS
    write_driver(root, "1.3", {"asserts": [], "blocked": "no model credential", "exit": 3})
    rc, res, out = qualify(root, "--case", "CWR-005")
    ok = (
        rc == 3
        and res
        and res[0]["outcome"] == "BLOCKED"
        and res[0]["accepted"]
        and load(root, res[0]["record"])["blocked_reason"] == "no model credential"
    )
    R.add(
        ["13.2"],
        ["a7"],
        ok,
        f"a driver reporting a missing resource is recorded BLOCKED with the resource rc={rc}",
        "" if ok else out[-500:],
    )
    if ok:
        path = forge(root, res[0]["record"], lambda r, d: claim_pass(r))
        expect_reject(
            root, path, "claims PASS", ["13.3"], ["a1"], "forged PASS of a BLOCKED attempt"
        )

    # a missing driver is refused, not recorded
    (root / "tests/cwr/cases/1_3.py").unlink()
    before = sorted(p.name for p in (root / "tests/cwr/evidence/CWR-005").iterdir())
    rc, res, out = qualify(root, "--case", "CWR-005")
    after = sorted(p.name for p in (root / "tests/cwr/evidence/CWR-005").iterdir())
    R.add(
        ["13.2"],
        ["a7"],
        rc == 2 and before == after and "does not exist" in out,
        f"qualify refuses a case whose driver does not exist rc={rc}, no record written={before == after}",
    )
    return rel


def scenario_traps(work: Path) -> None:
    """13.3: false-success traps at the independent checker."""
    root = make_root(work, "traps", {("case.CWR-006", "registered_builds"): [B1]})
    keys = ["a1", "a2", "a3", "a4", "a5"]

    # control: a genuine PASS
    write_driver(root, "2.1", passing("CWR-006", keys))
    rc, res, out = qualify(root, "--case", "CWR-006")
    genuine = res[0]["record"] if res else ""
    ok = rc == 0 and res and res[0]["outcome"] == "PASS"
    R.add(
        ["13.3"],
        [],
        ok,
        f"control: genuine CWR-006 attempt is PASS and accepted rc={rc}",
        "" if ok else out[-600:],
    )
    if not ok:
        return

    # (a1) zero assertions: a driver that exits 0 and asserts nothing is never PASS; a forged PASS is rejected
    write_driver(root, "2.1", passing("CWR-006", []))
    rc, res, out = qualify(root, "--case", "CWR-006")
    ok = rc == 1 and res and res[0]["outcome"] == "FAIL" and res[0]["accepted"]
    R.add(
        ["13.3"],
        ["a1"],
        ok,
        f"driver exiting 0 with no assertions is recorded FAIL, never PASS (rc={rc} "
        f"outcome={res and res[0]['outcome']})",
        "" if ok else out[-600:],
    )
    if ok:
        path = forge(root, res[0]["record"], lambda r, d: claim_pass(r))
        expect_reject(
            root,
            path,
            "no assertions were observed",
            ["13.3"],
            ["a1"],
            "forged PASS with no assertions",
        )

        def wipe(r, d):
            claim_pass(r)
            r["observed"], r["assertion_ids"], r["coverage_gaps"] = [], [], []

        path = forge(root, res[0]["record"], wipe)
        expect_reject(
            root,
            path,
            "REJECT",
            ["13.3"],
            ["a1"],
            "forged PASS with an emptied inventory and no assertions",
        )

    # (a2) Build hash different from the registered Build
    write_driver(root, "2.1", passing("CWR-006", keys, build_hash=B2))
    rc, res, out = qualify(root, "--case", "CWR-006")
    ok = (
        rc == 1
        and res
        and res[0]["outcome"] == "FAIL"
        and any("differs from the registered Build" in d for d in res[0]["diagnostics"])
    )
    R.add(
        ["13.3"],
        ["a2"],
        ok,
        f"attempt on an unregistered Build is recorded FAIL by the checker (rc={rc})",
        "" if ok else out[-600:],
    )
    if ok:
        path = forge(root, res[0]["record"], lambda r, d: claim_pass(r))
        expect_reject(
            root,
            path,
            "differs from the registered Build",
            ["13.3"],
            ["a2"],
            "forged PASS on a stale Build",
        )

    def swap_build(r, d):
        for p in d.rglob("*.jsonl"):
            p.write_text(p.read_text().replace(B1, B2))
        r["build_hash"], r["builds"] = B2, [B2]
        for inst in r["instances"]:
            inst["build"] = B2
        rehash_ledgers(r, root)

    path = forge(root, genuine, swap_build)
    expect_reject(
        root,
        path,
        "differs from the registered Build",
        ["13.3"],
        ["a2"],
        "genuine PASS rewritten (record and ledger, digests recomputed) to another Build",
    )

    # (a3) modified ledger
    def edit_ledger(r, d):
        p = next(d.rglob("*.jsonl"))
        p.write_text(p.read_text().replace('"status": "ok"', '"status": "error"', 1))

    path = forge(root, genuine, edit_ledger)
    expect_reject(
        root,
        path,
        "modified ledger",
        ["13.3"],
        ["a3"],
        "genuine PASS with one ledger record edited",
    )

    def truncate(r, d):
        p = next(d.rglob("*.jsonl"))
        p.write_bytes(p.read_bytes()[:-1])

    path = forge(root, genuine, truncate)
    expect_reject(
        root,
        path,
        "modified ledger",
        ["13.3"],
        ["a3"],
        "genuine PASS with a truncated trailing ledger record",
    )

    def gap(r, d):
        p = next(d.rglob("*.jsonl"))
        lines = p.read_text().splitlines(keepends=True)
        del lines[2]
        p.write_text("".join(lines))
        rehash_ledgers(r, root)

    path = forge(root, genuine, gap)
    expect_reject(
        root,
        path,
        "gap or duplicate",
        ["13.3"],
        ["a3"],
        "genuine PASS with a ledger record removed and digests recomputed (seq recomputed from the ledger)",
    )

    def edit_state(r, d):
        p = next(d.rglob("state/*.json"))
        p.write_text(p.read_text().replace('"x"', '"y"'))

    path = forge(root, genuine, edit_state)
    expect_reject(
        root,
        path,
        "modified final state",
        ["13.3"],
        ["a3"],
        "genuine PASS with the final state edited",
    )
    # tampering with the real record in place is detected too
    tampered = load(root, genuine)
    ledger = root / tampered["ledgers"][0]["path"]
    os.chmod(ledger, 0o644)
    with open(ledger, "a") as handle:
        handle.write(json.dumps({"seq": 5, "kind": "turn"}) + "\n")
    expect_reject(
        root,
        root / genuine,
        "modified ledger",
        ["13.3"],
        ["a3"],
        "ledger appended to after the attempt was recorded",
    )

    # (a4) seed mismatch
    conf = passing("CWR-006", keys)
    conf["instances"][0]["ledger_seed"] = 8
    write_driver(root, "2.1", conf)
    rc, res, out = qualify(root, "--case", "CWR-006")
    ok = (
        rc == 1
        and res
        and res[0]["outcome"] == "FAIL"
        and any("seed mismatch" in d for d in res[0]["diagnostics"])
    )
    R.add(
        ["13.3"],
        ["a4"],
        ok,
        f"attempt whose declared seed 7 disagrees with ledger header seed 8 is recorded FAIL (rc={rc})",
        "" if ok else out[-600:],
    )
    if ok:
        path = forge(root, res[0]["record"], lambda r, d: claim_pass(r))
        expect_reject(
            root, path, "seed mismatch", ["13.3"], ["a4"], "forged PASS with a mismatched seed"
        )
    write_driver(root, "2.1", passing("CWR-006", keys))
    rc, res, _ = qualify(root, "--case", "CWR-006")
    fresh = res[0]["record"] if res else genuine

    def reseed(r, d):
        r["seed"] = 9
        r["instances"][0]["seed"] = 9

    path = forge(root, fresh, reseed)
    expect_reject(
        root,
        path,
        "seed mismatch",
        ["13.3"],
        ["a4"],
        "genuine PASS whose record seed is rewritten to 9",
    )

    # observed assertions are recomputed from the driver log, and outcome from the observations
    conf = passing("CWR-006", keys)
    conf["asserts"][2][1] = "FAIL"
    write_driver(root, "2.1", conf)
    rc, res, out = qualify(root, "--case", "CWR-006")
    ok = rc == 1 and res and res[0]["outcome"] == "FAIL"
    R.add(["13.3"], [], ok, f"a failed assertion makes the attempt FAIL (rc={rc})")
    if ok:

        def flip(r, d):
            claim_pass(r)
            for o in r["observed"]:
                o["outcome"] = "PASS"

        path = forge(root, res[0]["record"], flip)
        expect_reject(
            root,
            path,
            "differ from those recomputed from the driver logs",
            ["13.3"],
            ["a1"],
            "forged PASS with the failed observation flipped in the record",
        )
        path = forge(root, res[0]["record"], lambda r, d: claim_pass(r))
        expect_reject(
            root, path, "claims PASS", ["13.3"], ["a1"], "forged PASS over a failed assertion"
        )


def fabricate_cwr001(root: Path, genuine: str) -> Path:
    """forge.py-style: copy a genuine attempt to CWR-001, rewrite its log to assert every CWR-001
    assertion PASS, drop its ledgers and recompute every digest the record carries."""

    def fabricate(r, d):
        ident = r["record_id"]
        for p in d.rglob("*"):
            if p.is_file():
                os.chmod(p, 0o644)
        (d / r["invocations"][0]["task"]).rename(d / "0.3")
        r["invocations"] = r["invocations"][:1]
        log = d / "0.3/driver.stdout.log"
        log.write_text("".join(f"ASSERT CWR-001.a{n} PASS forged\n" for n in range(1, 5)))
        for path in d.rglob("*.jsonl"):
            path.unlink()
        for path in d.rglob("state/*"):
            path.unlink()
        r.update(
            case="CWR-001",
            tier="component",
            clauses=["2.3", "3.1", "3.2", "3.3", "3.4", "3.6"],
            assertion_ids=[f"CWR-001.a{n}" for n in range(1, 5)],
            build_hash=None,
            builds=[],
            instances=[],
            ledgers=[],
            final_states=[],
            ledger_digest=None,
            seed=None,
            tail_size=None,
            arm=None,
            instance_id=None,
            cassette_digest=None,
            gateway_mode=None,
            model=None,
            observed=[
                {"id": f"CWR-001.a{n}", "outcome": "PASS", "diagnostic": "forged"}
                for n in range(1, 5)
            ],
            recomputed=[
                {"id": f"CWR-001.a{n}", "outcome": "PASS", "diagnostic": "forged"}
                for n in range(1, 5)
            ],
        )
        inv = r["invocations"][0]
        inv.update(
            task="0.3",
            command=["python3", "tests/cwr/cases/0_3.py"],
            stdout_log=f"tests/cwr/evidence/{ident}/0.3/driver.stdout.log",
            stderr_log=f"tests/cwr/evidence/{ident}/0.3/driver.stderr.log",
        )
        rehash_all(r, root)

    return forge(root, genuine, fabricate, case="CWR-001")


def scenario_forgeries(work: Path) -> None:
    """13.3 and 13.2: forgeries by a careful forger with write access, and damage to the evidence store."""
    root = make_root(work, "forgeries", {("case.CWR-006", "registered_builds"): [B1]})
    keys = ["a1", "a2", "a3", "a4", "a5"]
    write_driver(root, "2.1", passing("CWR-006", keys))
    rc, res, out = qualify(root, "--case", "CWR-006")
    if not (rc == 0 and res and res[0]["outcome"] == "PASS"):
        R.add(["13.3"], [], False, "control for forgeries: genuine CWR-006 PASS", out[-600:])
        return
    genuine = res[0]["record"]

    # a fully self-consistent fabricated record for a case whose oracle does not exist (CWR-001, task 0.3)
    path = fabricate_cwr001(root, genuine)
    expect_reject(
        root,
        path,
        "is not in the append-only attempt index",
        ["13.3"],
        ["a7"],
        "fully self-consistent fabricated CWR-001 PASS (every digest recomputed), not indexed",
    )
    index_append(root, path, load(root, str(path.relative_to(root))))
    expect_reject(
        root,
        path,
        "is UNVERIFIED",
        ["13.3"],
        ["a1"],
        "the same fabrication appended to the index with the registry head re-pinned: no oracle recomputes it",
    )
    st = status(root, "CWR-001")
    R.add(
        ["13.3", "13.4"],
        ["a6"],
        st["status"] is None
        and st["rc"] == 1
        and registry_case(root, "CWR-001")["status"] == "UNRUN"
        and (
            any("is refused now" in e for e in st["integrity_errors"])
            or (st["evidence_stale"] and st["evidence"] == str(path.relative_to(root)))
        ),
        f"the indexed fabrication never qualifies CWR-001: registry UNRUN, --status refuses to derive "
        f"(rc={st['rc']}, {st['integrity_errors'][:1]})",
    )

    # a CWR-006 record dated 2099: name, times and index entry all consistent
    def future(r, d):
        for key in ("started_at", "ended_at", "timestamp"):
            r[key] = "2099-12-31T23:59:59.000Z"
        rehash_all(r, root)

    path = forge(root, genuine, future, stamp="20991231T235959Z")
    index_append(
        root, path, load(root, str(path.relative_to(root))), appended_at="2099-12-31T23:59:59.000Z"
    )
    expect_reject(
        root,
        path,
        "lies in the future",
        ["13.3", "13.2"],
        ["a7"],
        "a PASS dated 2099, indexed and pinned",
    )
    st = status(root, "CWR-006")
    R.add(
        ["13.2"],
        ["a7"],
        st["rc"] == 1
        and st["status"] is None
        and any("in the future" in e for e in st["integrity_errors"]),
        f"--status refuses an index holding a future entry: rc={st['rc']} status={st['status']}",
    )
    # repair the fixture store for the next traps: drop the two forged entries
    lines = (root / INDEX_REL).read_text().splitlines()[:-2]
    (root / INDEX_REL).write_text("".join(ln + "\n" for ln in lines))
    index_reseal(root)

    # a backdated record: renamed to an earlier time than it started
    path = forge(root, genuine, lambda r, d: rehash_all(r, root), stamp="20200101T000000Z")
    index_append(root, path, load(root, str(path.relative_to(root))))
    expect_reject(
        root,
        path,
        "is not the attempt's start time",
        ["13.2", "13.3"],
        ["a7"],
        "a genuine PASS copied under a backdated name, indexed and pinned",
    )
    lines = (root / INDEX_REL).read_text().splitlines()[:-1]
    (root / INDEX_REL).write_text("".join(ln + "\n" for ln in lines))
    index_reseal(root)

    # modified ledger with every digest recomputed, in place: first without, then with the index rewritten
    record = load(root, genuine)
    ledger = root / record["ledgers"][0]["path"]
    original_ledger, original_record = ledger.read_bytes(), (root / genuine).read_bytes()
    os.chmod(ledger, 0o644)
    os.chmod(root / genuine, 0o644)
    ledger.write_bytes(
        original_ledger.replace(
            b'"code": null, "shape": "Bytes", "side_effect": "read_only", "status": "ok"',
            b'"code": "UNAVAILABLE", "shape": "Bytes", "side_effect": "read_only", "status": "error"',
            1,
        )
    )
    rehash_all(record, root)
    (root / genuine).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    expect_reject(
        root,
        root / genuine,
        "does not match the attempt index",
        ["13.3"],
        ["a3"],
        "ledger primitive outcome edited in place with every record digest recomputed",
    )
    index_reseal(root)
    expect_reject(
        root,
        root / genuine,
        "recomputed FAIL by the independent oracle",
        ["13.3"],
        ["a3"],
        "the same edit with the index entry, chain and registry head rewritten too",
    )
    ledger.write_bytes(original_ledger.replace(b'"status": "ok"', b'"status": "error"', 1))
    rehash_all(record, root)
    (root / genuine).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    index_reseal(root)
    expect_reject(
        root,
        root / genuine,
        "violates the kernel ledger schema",
        ["13.3"],
        ["a3"],
        "ledger status flipped ok -> error with no envelope code, everything recomputed",
    )
    ledger.write_bytes(original_ledger)
    (root / genuine).write_bytes(original_record)
    index_reseal(root)
    rc, out = check(root, root / genuine)
    R.add(
        ["13.3"],
        [],
        rc == 0,
        f"restored genuine record is accepted again rc={rc}",
        "" if rc == 0 else out[-400:],
    )

    # zero assertions, wrong seed and stale Build by a careful forger (indexed, pinned)
    def no_asserts(r, d):
        claim_pass(r)
        log = d / "2.1/driver.stdout.log"
        log.write_text(
            "".join(
                ln + "\n" for ln in log.read_text().splitlines() if not ln.startswith("ASSERT ")
            )
        )
        r["observed"], r["coverage_gaps"] = [], list(r["assertion_ids"])
        rehash_all(r, root)

    def header_seed(r, d):
        p = next(d.rglob("*.jsonl"))
        p.write_text(p.read_text().replace('"seed": 7', '"seed": 8', 1))
        rehash_all(r, root)

    def stale_build(r, d):
        for p in d.rglob("*"):
            if p.is_file():
                p.write_text(p.read_text().replace(B1, B2))
        text = json.dumps(r).replace(B1, B2)
        r.clear()
        r.update(json.loads(text))
        rehash_all(r, root)

    for mutate, needle, assertion, label in (
        (no_asserts, "no assertions were observed", "a1", "PASS with every ASSERT line removed"),
        (header_seed, "seed mismatch", "a4", "PASS whose ledger header seed was changed"),
        (
            stale_build,
            "differs from the registered Build",
            "a2",
            "PASS rewritten to an unregistered Build",
        ),
    ):
        path = forge(root, genuine, mutate)
        index_append(root, path, load(root, str(path.relative_to(root))))
        expect_reject(
            root,
            path,
            needle,
            ["13.3"],
            [assertion],
            f"{label}, every digest recomputed, indexed and pinned",
        )
    st = status(root, "CWR-006")
    R.add(
        ["13.2"],
        ["a7"],
        st["rc"] == 1
        and st["status"] is None
        and len([e for e in st["integrity_errors"] if "is refused now" in e]) == 3,
        f"--status reports the three indexed forgeries and refuses to derive a status (rc={st['rc']})",
    )
    lines = (root / INDEX_REL).read_text().splitlines()[:-3]
    (root / INDEX_REL).write_text("".join(ln + "\n" for ln in lines))
    index_reseal(root)

    # registry and --status agree on the latest attempt, including a later FAIL; deletions are reported
    conf = passing("CWR-006", keys)
    conf["asserts"][0][1] = "FAIL"
    write_driver(root, "2.1", conf)
    rc, res, out = qualify(root, "--case", "CWR-006")
    latest = res[0]["record"] if res else ""
    st = status(root, "CWR-006")
    reg = registry_case(root, "CWR-006")
    ok = (
        rc == 1
        and reg["status"] == "FAIL"
        and st["status"] == "FAIL"
        and reg["evidence"] == st["evidence"] == latest
    )
    R.add(
        ["13.2"],
        ["a7"],
        ok,
        f"after a PASS and a later FAIL the registry and --status both say "
        f"{reg['status']}/{st['status']} from {Path(latest).name}",
        "" if ok else out[-500:],
    )
    shutil.copytree(root, work / "forgeries_deleted", symlinks=True)
    copy = work / "forgeries_deleted"
    for p in copy.rglob("*"):
        os.chmod(p, 0o755 if p.is_dir() else 0o644)
    (copy / latest).unlink()
    shutil.rmtree(copy / latest[:-5])
    st = status(copy, "CWR-006")
    ok = (
        st["rc"] == 1
        and st["status"] is None
        and any("is missing" in e for e in st["integrity_errors"])
    )
    R.add(
        ["13.2"],
        ["a7"],
        ok,
        f"deleting the latest attempt record and its directory is reported: rc={st['rc']} "
        f"{st['integrity_errors'][:1]}",
    )
    lines = (copy / INDEX_REL).read_text().splitlines()[:-1]
    (copy / INDEX_REL).write_text("".join(ln + "\n" for ln in lines))
    st = status(copy, "CWR-006")
    ok = (
        st["rc"] == 1
        and st["status"] is None
        and any("do not match the registry" in e for e in st["integrity_errors"])
    )
    R.add(
        ["13.2"],
        ["a7"],
        ok,
        f"deleting the record and its index entry is reported against the pinned head: rc={st['rc']}",
    )
    rc, res, out = qualify(copy, "--case", "CWR-006")
    R.add(
        ["13.2"],
        ["a7"],
        rc == 2 and "do not match the registry" in out,
        f"the runner refuses to append to a damaged index rc={rc}",
    )


def scenario_separation(work: Path, genuine_root: Path, genuine_rel: str) -> None:
    """13.3: the checker is a separate program reading only records, ledgers, final state and the registry."""
    tree = ast.parse(CHECKER.read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    foreign = sorted(m for m in imported if m not in sys.stdlib_module_names)
    banned = sorted(
        imported
        & {"subprocess", "socket", "ctypes", "runpy", "multiprocessing", "http", "urllib", "shutil"}
    )
    loader_uses = [
        f.name
        for f in ast.walk(tree)
        if isinstance(f, ast.FunctionDef)
        and any(isinstance(n, ast.Name) and n.id == "importlib" for n in ast.walk(f))
    ]
    if loader_uses != ["module_oracle"]:
        banned.append(f"importlib used in {loader_uses}, not only in module_oracle")
    calls = sorted(
        set(
            re.findall(
                r"\b(exec|eval|__import__|os\.system|os\.popen|os\.exec\w*|os\.spawn\w*)\s*\(",
                CHECKER.read_text(),
            )
        )
    )
    ok = not foreign and not banned and not calls
    R.add(
        ["13.3"],
        ["a5"],
        ok,
        f"check_evidence.py imports only the standard library {sorted(imported)}; "
        f"foreign={foreign} process/network modules={banned} dynamic code={calls}",
    )
    q_tree = ast.parse(QUALIFY.read_text())
    q_imports = {a.name for n in ast.walk(q_tree) if isinstance(n, ast.Import) for a in n.names} | {
        n.module for n in ast.walk(q_tree) if isinstance(n, ast.ImportFrom)
    }
    ok = (
        "check_evidence" not in q_imports
        and "CHECKER" in QUALIFY.read_text()
        and "--staged" in QUALIFY.read_text()
    )
    R.add(
        ["13.3"],
        ["a5"],
        ok,
        "qualify.py never imports the checker; it runs it as a separate process (--staged)",
    )
    other = (
        [
            p
            for p in (REPO / "crates").rglob("*")
            if p.is_file() and "check_evidence" in p.read_text(errors="ignore")
        ]
        if (REPO / "crates").is_dir()
        else []
    )
    R.add(
        ["13.3"],
        ["a5"],
        not other,
        f"no crate under crates/ embeds or references the checker: {[str(p) for p in other]}",
    )

    if not genuine_rel:
        R.add(["13.3"], ["a5"], False, "no genuine record available for the runtime audit")
        return
    record = load(genuine_root, genuine_rel)
    attempt_dir = (genuine_root / "tests/cwr/evidence" / record["record_id"]).resolve()
    audit_out = work / "checker_audit.json"
    wrapper = r"""
import json, os, runpy, sys
target, out_path = sys.argv[1], sys.argv[2]
sys.argv = [target] + sys.argv[3:]
seen = {"process": [], "network": [], "reads": [], "writes": []}
live = [True]
def hook(event, args):
    if not live[0]:
        return
    if event.startswith(("subprocess.", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork")):
        seen["process"].append(event)
    elif event.startswith(("socket.", "http.client.", "urllib.Request")):
        seen["network"].append(event)
    elif event == "open" and not isinstance(args[0], int):
        path = os.path.abspath(os.fsdecode(args[0]))
        mode, flags = args[1], args[2]
        writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR))
        seen["writes" if writing else "reads"].append(path)
sys.addaudithook(hook)
code = 0
try:
    runpy.run_path(target, run_name="__main__")
except SystemExit as exc:
    code = exc.code if isinstance(exc.code, int) else 0
live[0] = False
open(out_path, "w").write(json.dumps(seen))
sys.exit(code)
"""
    proc = run(
        [
            sys.executable,
            "-c",
            wrapper,
            str(CHECKER),
            str(audit_out),
            "--root",
            str(genuine_root),
            str(genuine_root / genuine_rel),
        ]
    )
    seen = json.loads(audit_out.read_text()) if audit_out.is_file() else None
    if seen is None:
        R.add(
            ["13.3"], ["a5"], False, "audit of the checker produced no record", proc.stderr[-400:]
        )
        return
    allowed = {
        str((genuine_root / genuine_rel).resolve()),
        str((genuine_root / "tests/cwr/registry.kvx").resolve()),
        str((genuine_root / INDEX_REL).resolve()),
        str((genuine_root / "tests/cwr/oracles/CWR_005.py").resolve()),
        str(SCHEMA.resolve()),
        str(CHECKER.resolve()),
    }
    stdlib = tuple(
        str(Path(p).resolve()) for p in {sys.base_prefix, sys.prefix, os.path.dirname(os.__file__)}
    )
    # importlib probes for a cached .pyc of the oracle before compiling it; a probe of a path that does not
    # exist reads nothing (and the checker writes nothing, so no cache ever appears)
    foreign_reads = sorted(
        {
            p
            for p in seen["reads"]
            if os.path.exists(p)
            and p not in allowed
            and not p.startswith(str(attempt_dir) + os.sep)
            and not str(Path(p).resolve()).startswith(stdlib)
        }
    )
    artifact_reads = sorted(
        {
            str(Path(p).relative_to(attempt_dir))
            for p in seen["reads"]
            if p.startswith(str(attempt_dir) + os.sep)
        }
    )
    ok = (
        proc.returncode == 0
        and not seen["process"]
        and not seen["network"]
        and not seen["writes"]
        and not foreign_reads
        and any(p.endswith(".jsonl") for p in artifact_reads)
    )
    R.add(
        ["13.3"],
        ["a5"],
        ok,
        f"runtime audit of the checker: rc={proc.returncode} processes={seen['process']} "
        f"network={seen['network']} writes={seen['writes']} reads beyond record/registry/schema/attempt artifacts="
        f"{foreign_reads}; artifacts read={artifact_reads}",
    )


def scenario_hardening(work: Path) -> None:
    """Runner and checker hardening: experimental flag names, FAIL before BLOCKED, symbolic links,
    process groups, malformed ledgers and index entries, an interrupted pin, stale versus damaged
    evidence and Build registration."""
    root = make_root(
        work,
        "hardening",
        {("case.CWR-005", "registered_builds"): [B1], ("case.CWR-013", "registered_builds"): [B1]},
    )
    keys13 = ["a1", "a2", "a3", "a4", "a5", "a6"]

    # experimental Builds: the ledger stamps x-<flag>, which the record declares in the same form
    write_driver(
        root,
        "3.2",
        passing("CWR-013", keys13, experimental="x-drift", experimental_flags=["x-drift"]),
    )
    rc, res, out = qualify(root, "--case", "CWR-013")
    ok = rc == 0 and res and res[0]["outcome"] == "PASS" and res[0]["accepted"]
    flags = load(root, res[0]["record"])["experimental_flags"] if res else None
    R.add(
        ["13.3"],
        [],
        ok and flags == ["x-drift"],
        f'a ledger stamped experimental "x-drift" with the flag declared as x-drift is PASS '
        f"(rc={rc}, experimental_flags={flags})",
        "" if ok else out[-600:],
    )
    write_driver(
        root,
        "3.2",
        passing("CWR-013", keys13, experimental="x-drift", experimental_flags=["--x-drift"]),
    )
    rc, res, out = qualify(root, "--case", "CWR-013")
    diags = " ".join(res[0]["diagnostics"]) if res else out
    R.add(
        ["13.3"],
        [],
        rc == 1 and "experimental_flags has an invalid value" in diags,
        f"the command line form --x-drift is not a flag name: the attempt is FAIL (rc={rc})",
        "" if rc == 1 else diags[-600:],
    )

    # FAIL is decided before BLOCKED
    conf = passing("CWR-005", ["a1", "a2"])
    conf["asserts"][1][1] = "FAIL"
    conf.update(blocked="no model credential", exit=3)
    write_driver(root, "1.3", conf)
    rc, res, out = qualify(root, "--case", "CWR-005")
    ok = rc == 1 and res and res[0]["outcome"] == "FAIL" and res[0]["accepted"]
    R.add(
        ["13.3"],
        ["a1"],
        ok,
        f"one ASSERT FAIL plus one BLOCKED line is recorded FAIL, not BLOCKED (rc={rc} "
        f"outcome={res and res[0]['outcome']})",
        "" if ok else out[-600:],
    )
    if ok:

        def claim_blocked(record: dict, _dir: Path) -> None:
            record["outcome"], record["blocked_reason"] = "BLOCKED", "no model credential"

        path = forge(root, res[0]["record"], claim_blocked)
        expect_reject(
            root,
            path,
            "claims BLOCKED but the attempt failed",
            ["13.3"],
            ["a1"],
            "a FAIL attempt relabelled BLOCKED",
        )

    # a symbolic link in the driver's output never copies a host file into evidence
    secret = work / "host-secret.txt"
    secret.write_text("HOST-SECRET-0_2\n")
    write_driver(root, "1.3", passing("CWR-005", ["a1", "a2", "a3", "a4"], symlink=str(secret)))
    rc, res, out = qualify(root, "--case", "CWR-005")
    leaked = [
        str(p.relative_to(root))
        for p in (root / "tests/cwr/evidence").rglob("*")
        if p.is_file() and b"HOST-SECRET-0_2" in p.read_bytes()
    ]
    diags = " ".join(res[0]["diagnostics"]) if res else out
    R.add(
        ["13.2"],
        ["a7"],
        rc == 1 and not leaked and "names no ledger inside CWR_EVIDENCE_DIR" in diags,
        f"a ledger that is a symbolic link to a host file is refused and never copied "
        f"(rc={rc}, files holding the secret: {leaked})",
        "" if not leaked else diags[-400:],
    )

    # a hard link in the driver's output never copies a host file into evidence either
    linked = work / "hardlink-source" / "host-secret.txt"
    linked.parent.mkdir(parents=True, exist_ok=True)
    linked.write_text("HOST-SECRET-LINK-0_2\n")
    write_driver(root, "1.3", passing("CWR-005", ["a1", "a2", "a3", "a4"], hardlink=str(linked)))
    rc, res, out = qualify(root, "--case", "CWR-005")
    leaked = [
        str(p.relative_to(root))
        for p in (root / "tests/cwr/evidence").rglob("*")
        if p.is_file() and b"HOST-SECRET-LINK-0_2" in p.read_bytes()
    ]
    diags = " ".join(res[0]["diagnostics"]) if res else out
    R.add(
        ["13.2"],
        ["a7"],
        rc == 1 and not leaked and "names no ledger inside CWR_EVIDENCE_DIR" in diags,
        f"a ledger that is a hard link to a host file is refused and never copied "
        f"(rc={rc}, files holding the secret: {leaked})",
        "" if not leaked else diags[-400:],
    )

    # a timed-out driver is killed with every child it started, also one in a session of its own
    for own_session in (False, True):
        pid_file = work / f"spawned-{own_session}.pid"
        write_driver(
            root,
            "1.3",
            dict(passing("CWR-005", []), spawn=str(pid_file), own_session=own_session),
        )
        rc, res, out = qualify(root, "--case", "CWR-005", "--timeout", "4")
        pid = int(pid_file.read_text()) if pid_file.is_file() else None
        alive = False
        if pid is not None:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
                except OSError:
                    state = "gone"
                alive = state not in ("gone", "Z", "X")
                if not alive:
                    break
                time.sleep(0.2)
        timed_out = bool(res) and load(root, res[0]["record"])["invocations"][0]["timed_out"]
        where = "in a session of its own" if own_session else "in the driver's process group"
        R.add(
            ["13.2"],
            [],
            pid is not None and not alive and timed_out and rc == 1,
            f"a driver that times out is killed with the child it started {where} (child pid "
            f"{pid} alive={alive}, timed_out={timed_out}, rc={rc})",
        )

    # a driver speaking the shared protocol can be recorded BLOCKED; FAIL still comes first (CWR-002
    # has no oracle in this root, so the recomputation leaves its assertions UNVERIFIED, never FAIL)
    shutil.copyfile(CASES_DIR / "protocol.py", root / "tests/cwr/cases/protocol.py")
    for label, unplaced, expected in (
        ("one observed passing check, then a missing resource", False, "BLOCKED"),
        ("a failing check attributed to no assertion, then a missing resource", True, "FAIL"),
    ):
        (root / "tests/cwr/cases/1_1.py").write_text(
            PROTOCOL_BLOCKED_DRIVER.replace("UNPLACED", repr(unplaced))
        )
        rc, res, out = qualify(root, "--case", "CWR-002")
        record = load(root, res[0]["record"]) if res else {}
        observed = [(o["id"], o["outcome"]) for o in record.get("observed", [])]
        want = (
            [("CWR-002.a1", "PASS")]
            if expected == "BLOCKED"
            else [(f"CWR-002.a{n}", "FAIL") for n in range(1, 7)]
        )
        ok = (
            bool(res)
            and res[0]["outcome"] == expected
            and res[0]["accepted"]
            and observed == want
            and (record.get("blocked_reason") == "no Build host" or expected == "FAIL")
        )
        R.add(
            ["13.3"],
            ["a1"],
            ok,
            f"protocol driver with {label} is recorded {expected} (outcome="
            f"{res and res[0]['outcome']}, observed={observed})",
            "" if ok else out[-600:],
        )

    # a genuine PASS for the remaining traps
    write_driver(root, "1.3", passing("CWR-005", ["a1", "a2", "a3", "a4"]))
    rc, res, out = qualify(root, "--case", "CWR-005")
    if not (rc == 0 and res):
        R.add(["13.3"], [], False, "control for hardening: genuine CWR-005 PASS", out[-600:])
        return
    genuine = res[0]["record"]

    # a ledger that is not UTF-8 is a located refusal, not a crash
    def not_utf8(record: dict, _dir: Path) -> None:
        ledger = root / record["ledgers"][0]["path"]
        data = ledger.read_bytes().replace(b'"fs.read"', b'"fs.r\xffad"', 1)
        ledger.write_bytes(data)
        rehash_all(record, root)

    path = forge(root, genuine, not_utf8)
    expect_reject(
        root, path, "not UTF-8", ["13.3"], ["a3"], "a ledger holding a byte that is not UTF-8"
    )

    # index entries of the wrong shape are located refusals, never crashes or reads outside
    index = root / INDEX_REL
    original = index.read_text()
    for label, entry_record in (
        ("an index entry whose record is a list", ["a"]),
        ("an index entry whose record escapes the repository", "../../etc/passwd"),
    ):
        lines = original.splitlines()
        last = json.loads(lines[-1])
        bad = dict(
            last,
            seq=len(lines),
            prev="sha256:" + hashlib.sha256(lines[-1].encode()).hexdigest(),
            record=entry_record,
        )
        index.write_text(original + canonical(bad) + "\n")
        proc = run([sys.executable, str(CHECKER), "--root", str(root), "--status", "CWR-005"])
        try:
            result = json.loads(proc.stdout)
        except ValueError:
            result = {}
        ok = (
            proc.returncode == 1
            and "Traceback" not in proc.stderr
            and any("field record" in e for e in result.get("integrity_errors", []))
        )
        R.add(
            ["13.2"],
            ["a7"],
            ok,
            f"{label}: --status reports a located integrity error (rc={proc.returncode})",
            "" if ok else (proc.stdout + proc.stderr)[-500:],
        )
    index.write_text(original)

    # an append whose re-pin was interrupted is refused until --repair-pin re-pins the tail
    registry = root / "tests/cwr/registry.kvx"
    pinned = registry.read_text()
    rc, res, out = qualify(root, "--case", "CWR-005")
    second = res[0]["record"] if res else None
    registry.write_text(
        set_key(
            set_key(registry.read_text(), "meta", "index_head", registry_case_meta(pinned)[0]),
            "meta",
            "index_length",
            registry_case_meta(pinned)[1],
        )
    )
    rc, res, out = qualify(root, "--case", "CWR-005")
    refused = rc == 2 and "--repair-pin" in out
    proc = run([sys.executable, str(QUALIFY), "--root", str(root), "--repair-pin"])
    st = status(root, "CWR-005")
    ok = (
        refused
        and proc.returncode == 0
        and '"repaired": true' in proc.stdout
        and st["rc"] == 0
        and st["evidence"] == second
        and registry_case(root, "CWR-005")["evidence"] == second
    )
    R.add(
        ["13.2"],
        ["a7"],
        ok,
        f"an index append without its re-pin is refused (rc={rc}) and --repair-pin re-pins the "
        f"chained tail and applies its case outcome (rc={proc.returncode}, status rc={st['rc']})",
        "" if ok else (out + proc.stdout + proc.stderr)[-600:],
    )
    lines = index.read_text().splitlines()
    fake = dict(
        json.loads(lines[-1]),
        seq=len(lines),
        prev="sha256:" + hashlib.sha256(lines[-1].encode()).hexdigest(),
        record_sha256="sha256:" + "f" * 64,
    )
    before = index.read_text()
    index.write_text(before + canonical(fake) + "\n")
    proc = run([sys.executable, str(QUALIFY), "--root", str(root), "--repair-pin"])
    R.add(
        ["13.2"],
        ["a7"],
        proc.returncode == 2 and "differs from its sha256" in proc.stderr,
        f"--repair-pin refuses a tail entry whose record does not match (rc={proc.returncode})",
        "" if proc.returncode == 2 else (proc.stdout + proc.stderr)[-400:],
    )
    index.write_text(before)

    # a registry change makes a record stale, which is not damage; an edited ledger is damage
    text = registry.read_text()
    text = set_key(text, "case.CWR-005", "a5", "A fifth assertion registered after the attempt.")
    text = set_key(text, "case.CWR-005", "subsets", {"1.3": ["a1", "a2", "a3", "a4", "a5"]})
    registry.write_text(text)
    st = status(root, "CWR-005")
    stale = [s["record"] for s in st.get("stale_records", [])]
    ok = (
        st["integrity_errors"] == []
        and second in stale
        and st["evidence_stale"] is True
        and st["evidence"] == second
        and st["status"] is None
    )
    R.add(
        ["13.2"],
        ["a7"],
        ok,
        f"after a legitimate registry change the older records are stale, not damaged, and no status "
        f"is derived from them (integrity errors {st['integrity_errors'][:1]}, stale {len(stale)})",
        "" if ok else json.dumps(st)[-600:],
    )
    ledger = root / load(root, second)["ledgers"][0]["path"]
    os.chmod(ledger, 0o644)
    ledger.write_text(ledger.read_text().replace('"status": "ok"', '"status": "error"', 1))
    st = status(root, "CWR-005")
    R.add(
        ["13.2"],
        ["a3"],
        st["rc"] == 1 and st["status"] is None and bool(st["integrity_errors"]),
        f"an edited ledger is damage, never staleness (rc={st['rc']}, status {st['status']})",
    )

    # Builds are registered only through the runner, and only when the Build directory exists
    other = make_root(work, "register")
    register = [sys.executable, str(QUALIFY), "--root", str(other), "--register-build"]
    missing = run([*register, "CWR-005", B2])
    no_build_case = run([*register, "CWR-001", B2])
    build_dir = other / "build" / B2.split(":", 1)[1]
    build_dir.mkdir(parents=True)
    (build_dir / "manifest.kvx").write_text(f'[build]\nname = "refworld"\nhash = "{B2}"\n')
    added = run([*register, "CWR-005", B2])
    ok = (
        missing.returncode == 2
        and no_build_case.returncode == 2
        and added.returncode == 0
        and registry_case(other, "CWR-005")["registered_builds"] == [B2]
    )
    R.add(
        ["13.3"],
        ["a2"],
        ok,
        f"--register-build refuses a hash with no Build directory (rc={missing.returncode}) and a "
        f"case without Builds (rc={no_build_case.returncode}), and registers an existing Build "
        f"(rc={added.returncode})",
        "" if ok else (missing.stderr + added.stdout + added.stderr)[-500:],
    )


def registry_case_meta(text: str) -> tuple:
    """(index_head, index_length) pinned in a registry text."""
    head = re.search(r"^index_head = (.+)$", text, re.M).group(1)
    length = re.search(r"^index_length = (\d+)$", text, re.M).group(1)
    return json.loads(head), int(length)


def scenario_protocol() -> None:
    """Every real task driver speaks the runner protocol for its whole registered subset, and every
    registered case has an independent oracle that passes the separation audit or a recorded deviation."""
    sections, current = {}, None
    for line in REGISTRY.read_text().splitlines():
        line = line.strip()
        if re.fullmatch(r"\[.+\]", line):
            current = sections.setdefault(line[1:-1], {})
        elif current is not None and "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            current[key.strip()] = json.loads(value)
    wanted: dict[str, set] = {}
    for name, case in sections.items():
        if name.startswith("case."):
            for task, keys in case.get("subsets", {}).items():
                wanted.setdefault(task, set()).update(f"{name[5:]}.{k}" for k in keys)
    if not (CASES_DIR / "protocol.py").is_file() and not (REPO / ".git").exists():
        # a temporary root built by another driver holds fixture drivers, not this checkout's
        print(
            "NOTE driver protocol audit not applicable: no tests/cwr/cases/protocol.py in this root"
        )
        wanted = {}
    problems = []
    for task, ids in sorted(wanted.items()):
        path = CASES_DIR / (task.replace(".", "_") + ".py")
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text())
        literals = {
            n.value
            for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        }
        uses = any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "emit"
            for n in ast.walk(tree)
        )
        imports = any(
            isinstance(n, ast.Import) and any(a.name == "protocol" for a in n.names)
            for n in ast.walk(tree)
        )
        if uses and imports:
            # protocol drivers name every assertion they observe as a literal id
            missing = sorted(ids - literals)
        else:
            # self-emitting drivers (0.2, 6.1, 6.2) print ASSERT lines from per-key tables
            text = path.read_text()
            missing = sorted(
                i
                for i in ids
                if "ASSERT " not in text
                or i.split(".")[0] not in text
                or i.split(".")[1] not in literals
            )
        if missing:
            problems.append(f"{path.name} does not report {missing}")
    R.add(
        ["13.3"],
        ["a1"],
        not problems,
        f"every existing driver named in a registry subset emits its whole subset through the "
        f"protocol ({len(wanted)} tasks)",
        "; ".join(problems)[:1500],
    )
    # the protocol itself: an unobserved assertion is reported FAIL, never omitted
    if not wanted:
        return
    probe = (
        "import sys; sys.path.insert(0, sys.argv[1]); import protocol\n"
        "class R:\n    ok = {'1.4': []}\n    bad = {'1.4': []}\n"
        "o = protocol.Observer('1.3', protocol.ok_bad(R))\n"
        "with o.observe('CWR-005.a1'):\n    R.ok['1.4'].append('a real check')\n"
        "with o.observe('CWR-005.a2'):\n    R.bad['1.4'].append('a failed check')\n"
        "o.emit()\n"
    )
    env = dict(os.environ, CWR_QUALIFY="1", CWR_EVIDENCE_DIR=tempfile.mkdtemp(prefix="cwr_0_2_p_"))
    proc = subprocess.run(
        [sys.executable, "-I", "-c", probe, str(CASES_DIR)],
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        env=env,
        check=False,
    )
    lines = [ln.split(" ")[1:3] for ln in proc.stdout.splitlines() if ln.startswith("ASSERT ")]
    ok = lines == [
        ["CWR-005.a1", "PASS"],
        ["CWR-005.a2", "FAIL"],
        ["CWR-005.a3", "FAIL"],
        ["CWR-005.a4", "FAIL"],
    ]
    R.add(
        ["13.3"],
        ["a1"],
        ok,
        f"protocol.py reports every registered assertion of the task: observed PASS, failed FAIL, "
        f"unobserved FAIL ({lines})",
        "" if ok else (proc.stdout + proc.stderr)[-500:],
    )
    # oracles: present and vetted, or the gap is a recorded deviation
    spec = importlib.util.spec_from_file_location("cwr_0_2_checker", CHECKER)
    checker = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = checker
    spec.loader.exec_module(checker)
    deviation = ""
    qual = QUALIFICATION.read_text() if QUALIFICATION.is_file() else ""
    match = re.search(
        r"^\[finding\.qualification_oracles_missing\]\n(.*?)(?=^\[|\Z)", qual, re.M | re.S
    )
    if match:
        deviation = match.group(1)
    gaps, refused = [], []
    for name in sorted(n[5:] for n in sections if n.startswith("case.")):
        if name in checker.BUILTIN_ORACLES:
            continue
        oracle = REPO / "tests/cwr/oracles" / (name.replace("-", "_") + ".py")
        if oracle.is_file():
            problems = checker.vet_source(oracle.read_text())
            if problems:
                refused.append(f"{name}: {problems}")
        elif name not in deviation:
            gaps.append(name)
    R.add(
        ["13.3"],
        [],
        not gaps and not refused,
        "every registered case has an oracle that passes the separation audit or is named in the "
        "recorded deviation [finding.qualification_oracles_missing]",
        f"no oracle and no deviation: {gaps}; refused: {refused}",
    )


def scenario_inventory(work: Path, genuine_root: Path, genuine_rel: str) -> None:
    """13.2: a case without a registered inventory cannot be marked PASS."""
    root = make_root(work, "noinventory", {("case.CWR-005", "registered_builds"): [B1]})
    text = (root / "tests/cwr/registry.kvx").read_text()
    text = drop_keys(text, "case.CWR-005", r"a\d+\s*=")
    text = set_key(text, "case.CWR-005", "subsets", {"1.3": []})
    (root / "tests/cwr/registry.kvx").write_text(text)
    write_driver(root, "1.3", passing("CWR-005", ["a1", "a2", "a3", "a4"]))
    proc = run([sys.executable, str(QUALIFY), "--root", str(root), "--check-registry"])
    ok = proc.returncode == 2 and "CWR-005: no registered assertion inventory" in proc.stdout
    R.add(
        ["13.2"],
        ["a8"],
        ok,
        f"registry check names the case without an inventory rc={proc.returncode}",
        "" if ok else proc.stdout[-400:],
    )
    rc, _res, out = qualify(root, "--case", "CWR-005")
    written = (
        list((root / "tests/cwr/evidence").rglob("*.json"))
        if (root / "tests/cwr/evidence").exists()
        else []
    )
    ok = rc == 2 and not written and "no registered assertion inventory" in out
    R.add(
        ["13.2"],
        ["a8"],
        ok,
        f"qualify refuses to qualify a case without an inventory rc={rc}, records written={len(written)}",
    )
    if genuine_rel:
        src = genuine_root / "tests/cwr/evidence/CWR-005"
        dst = root / "tests/cwr/evidence/CWR-005"
        dst.mkdir(parents=True, exist_ok=True)
        name = Path(genuine_rel).name
        shutil.copyfile(src / name, dst / name)
        shutil.copytree(src / name[:-5], dst / name[:-5])
        expect_reject(
            root,
            dst / name,
            "has no registered assertion inventory and cannot be marked PASS",
            ["13.2"],
            ["a8"],
            "a genuine PASS record presented against a registry without the case inventory",
        )
        st = status(root, "CWR-005")
        R.add(
            ["13.2"],
            ["a8"],
            st["status"] == "UNRUN",
            f"derived status without an inventory stays {st['status']}",
        )
    real = run([sys.executable, str(QUALIFY), "--check-registry"])
    ok = real.returncode == 0 and json.loads(real.stdout)["cases"] == 20
    R.add(
        ["13.2"],
        ["a8"],
        ok,
        f"the real registry has 20 cases, each with a registered inventory and task subsets rc={real.returncode}",
        "" if ok else real.stdout[-400:],
    )


def scenario_component(work: Path) -> None:
    """13.4: component evidence never qualifies the parent case."""
    root = make_root(
        work,
        "component",
        {("case.CWR-009", "registered_builds"): [B1], ("case.CWR-005", "registered_builds"): [B1]},
    )
    write_driver(
        root,
        "2.3",
        passing("CWR-009", ["a2", "a3", "a4", "a5", "a6", "a7", "a9"], inst_id="01FIXTURE009GW"),
    )
    write_driver(root, "4.1", passing("CWR-009", ["a1", "a8"], inst_id="01FIXTURE009RW"))

    rc, res, out = qualify(root, "--case", "CWR-009", "--task", "2.3")
    ok = (
        rc == 0
        and res
        and res[0]["outcome"] == "PASS"
        and res[0]["scope"] == "task"
        and res[0]["accepted"]
    )
    R.add(
        ["13.4"],
        ["a6"],
        ok,
        f"task 2.3 component PASS for its registered subset of CWR-009 rc={rc}",
        "" if ok else out[-600:],
    )
    component = res[0]["record"] if res else ""
    reg, st = registry_case(root, "CWR-009"), status(root, "CWR-009")
    R.add(
        ["13.4"],
        ["a6"],
        reg["status"] == "UNRUN" and reg["evidence"] is None and st["status"] == "UNRUN",
        f"after a component PASS the registry says {reg['status']} and the checker derives {st['status']}",
    )

    rc, res, out = qualify(root, "--task", "4.1")
    by_case = {r["case"]: r for r in res}
    ok = (
        rc in (0, 1)
        and by_case.get("CWR-009", {}).get("outcome") == "PASS"
        and "CWR-014" in by_case
    )
    R.add(
        ["13.4"],
        ["a6"],
        ok,
        f"qualify --task 4.1 writes one component record per owned case: "
        f"{ {c: r['outcome'] for c, r in by_case.items()} }",
        "" if ok else out[-600:],
    )
    reg, st = registry_case(root, "CWR-009"), status(root, "CWR-009")
    ok = (
        reg["status"] == "UNRUN"
        and st["status"] == "UNRUN"
        and len([c for c in st["component_records"] if c["outcome"] == "PASS"]) == 2
    )
    R.add(
        ["13.4"],
        ["a6"],
        ok,
        f"component PASSes covering every subset of CWR-009 still leave it UNRUN "
        f"(registry={reg['status']}, derived={st['status']}, component PASS records={len(st['component_records'])})",
    )
    R.add(
        ["13.4"],
        ["a6"],
        registry_case(root, "CWR-014")["status"] == "UNRUN",
        "task-scope FAIL for CWR-014 leaves it UNRUN too",
    )

    if component:

        def promote(r, d):
            r["scope"], r["task"] = "case", None

        path = forge(root, component, promote)
        expect_reject(
            root,
            path,
            "registered inventory for this scope",
            ["13.4"],
            ["a6"],
            "component record relabelled as case scope",
        )

        def promote_full(r, d):
            r["scope"], r["task"] = "case", None
            r["assertion_ids"] = [f"CWR-009.a{n}" for n in range(1, 10)]
            r["coverage_gaps"] = ["CWR-009.a1", "CWR-009.a8"]

        path = forge(root, component, promote_full)
        expect_reject(
            root,
            path,
            "was not observed",
            ["13.4"],
            ["a6"],
            "component record relabelled as a full-case PASS with the full inventory",
        )
        st = status(root, "CWR-009")
        R.add(
            ["13.4"],
            ["a6"],
            st["status"] == "UNRUN" and len(st["rejected_records"]) >= 2,
            f"forged case-scope records are ignored by status derivation: {st['status']}, rejected={len(st['rejected_records'])}",
        )

    # single-task case: even a component record carrying the full inventory is not a case PASS
    write_driver(root, "1.3", passing("CWR-005", ["a1", "a2", "a3", "a4"]))
    rc, res, _ = qualify(root, "--case", "CWR-005", "--task", "1.3")
    st = status(root, "CWR-005")
    R.add(
        ["13.4"],
        ["a6"],
        rc == 0 and st["status"] == "UNRUN" and registry_case(root, "CWR-005")["status"] == "UNRUN",
        f"single-task case: component PASS with the full inventory leaves CWR-005 {st['status']}",
    )

    rc, res, out = qualify(root, "--case", "CWR-009")
    reg, st = registry_case(root, "CWR-009"), status(root, "CWR-009")
    ok = (
        rc == 0
        and reg["status"] == "PASS"
        and st["status"] == "PASS"
        and reg["evidence"] == st["evidence"] == res[0]["record"]
    )
    R.add(
        ["13.4"],
        ["a6"],
        ok,
        f"only a full-case attempt running every task driver qualifies CWR-009: registry={reg['status']} "
        f"evidence={reg['evidence']}",
        "" if ok else out[-600:],
    )


def scenario_hypothesis(work: Path) -> None:
    """13.4: a hypothesis result requires the complete arm matrix and pre-registered metrics."""
    keys52, keys53 = ["a1", "a2", "a5", "a7", "a8"], ["a3", "a4", "a6", "a7", "a8"]

    suite_tasks = suite_task_ids()
    classes = {task: initial_class(task) for task in suite_tasks}
    # two suite tasks whose initial.kvx define different states, and two that define the same one
    distinct = suite_tasks[:2]
    shared = next(
        (
            [a, b]
            for a in suite_tasks
            for b in suite_tasks
            if a < b and classes[a] is not None and classes[a] == classes[b]
        ),
        None,
    )

    def request(task: str, seed: int) -> str:
        return "sha256:" + hashlib.sha256(f"fixture request {task} {seed}".encode()).hexdigest()

    def instances(
        matrix,
        drop=0,
        *,
        prefix=True,
        tasks=None,
        state_of=None,
        requests_of=None,
    ):
        # ledger header arms in the harness form H<n>/<arm>; task labels from benchmark/suite.kvx.
        # The ledger initial state of a task follows the state its initial.kvx defines (state_of
        # overrides it for the relabelling traps); requests_of gives replayed adapter requests.
        out = []
        for hyp, spec in matrix.items():
            for arm in spec["arms"]:
                label = re.sub(r"[^a-z0-9-]", "", arm.lower())
                for t in range(spec["tasks"]):
                    task = (tasks or suite_tasks)[t]
                    key = state_of(task) if state_of else (classes.get(task) or task)
                    state = "sha256:" + hashlib.sha256(key.encode()).hexdigest()
                    for seed in range(spec["seeds"]):
                        entry = {
                            "id": f"01{hyp}{label}{t}{seed}".replace("-", ""),
                            "seed": seed,
                            "arm": f"{hyp}/{label}" if prefix else arm,
                            "hypothesis": hyp,
                            "task": task,
                            "tail_size": 20,
                            "initial_state": state,
                        }
                        if requests_of is not None:
                            entry["requests"] = requests_of(task, seed)
                            entry["cassette_digest"] = CASSETTES
                        out.append(entry)
        return out[: len(out) - drop]

    # the real registry: four hypotheses, 60 x 10 matrices, no pre-registration yet
    root = make_root(work, "campaign_real", {("case.CWR-018", "registered_builds"): [B1]})
    conf = passing("CWR-018", keys52, preregistration_digest=PREREG)
    conf["instances"] = instances({"H1": {"arms": ["frozen", "drift-1"], "tasks": 1, "seeds": 2}})
    write_driver(root, "5.2", conf)
    write_driver(
        root, "5.3", dict(passing("CWR-018", keys53, preregistration_digest=PREREG), instances=[])
    )
    rc, res, out = qualify(root, "--case", "CWR-018")
    diags = " ".join(res[0]["diagnostics"]) if res else ""
    ok = (
        rc == 1
        and res[0]["outcome"] == "FAIL"
        and "incomplete arm matrix" in diags
        and "pre-registration" in diags
    )
    R.add(
        ["13.4"],
        [],
        ok,
        f"campaign with every assertion PASS but 4 of 4,980 matrix cells and no registered "
        f"pre-registration is FAIL (rc={rc})",
        "" if ok else out[-600:],
    )
    if res:
        path = forge(root, res[0]["record"], lambda r, d: claim_pass(r))
        expect_reject(
            root,
            path,
            "incomplete arm matrix",
            ["13.4"],
            [],
            "forged hypothesis PASS without the complete matrix",
        )
    before = status(root, "CWR-018")
    rc, res, _ = qualify(root, "--case", "CWR-018", "--task", "5.2")
    st = status(root, "CWR-018")
    ok = (
        res
        and res[0]["scope"] == "task"
        and (st["status"], st["evidence"]) == (before["status"], before["evidence"])
        and registry_case(root, "CWR-018")["evidence"] == before["evidence"]
    )
    R.add(
        ["13.4"],
        ["a6"],
        ok,
        f"a component campaign record leaves the CWR-018 status where the last full-case attempt "
        f"put it ({before['status']} -> {st['status']})",
    )

    # a reduced registered matrix with a registered pre-registration: the rule is the registered matrix
    small = {"H1": {"arms": ["frozen", "drift-1"], "tasks": 2, "seeds": 2}}
    root = make_root(
        work,
        "campaign_small",
        {
            ("case.CWR-018", "registered_builds"): [B1],
            ("case.CWR-018", "matrix"): small,
            ("case.CWR-018", "preregistration_digest"): PREREG,
        },
    )
    write_driver(
        root, "5.3", dict(passing("CWR-018", keys53, preregistration_digest=PREREG), instances=[])
    )
    conf = passing("CWR-018", keys52, preregistration_digest=PREREG)
    conf["instances"] = instances(small, tasks=distinct)
    write_driver(root, "5.2", conf)
    rc, res, out = qualify(root, "--case", "CWR-018")
    ok = (
        rc == 0
        and res[0]["outcome"] == "PASS"
        and registry_case(root, "CWR-018")["status"] == "PASS"
    )
    R.add(
        ["13.4"],
        [],
        ok,
        f"control: the complete registered matrix (8 Instances of {distinct}) with the registered "
        f"pre-registration is PASS rc={rc}",
        "" if ok else out[-600:],
    )
    # real suite tasks share an initial state; their labels are then bound by the replayed requests
    if shared is None:
        R.add(["13.4"], [], False, "the suite has no two tasks that define one initial state")
    else:
        for task in shared:
            for seed in range(2):
                cassette = root / "benchmark/cassettes/suite" / task / f"seed-{seed}.jsonl"
                cassette.parent.mkdir(parents=True, exist_ok=True)
                cassette.write_text(
                    json.dumps({"format": "cwr.cassette", "label": f"{task}/seed-{seed}"})
                    + "\n"
                    + json.dumps({"request_hash": request(task, seed), "seq": 0})
                    + "\n"
                )
        conf["instances"] = instances(
            small, tasks=shared, requests_of=lambda task, seed: [request(task, seed)]
        )
        write_driver(root, "5.2", conf)
        rc, res, out = qualify(root, "--case", "CWR-018")
        ok = rc == 0 and res[0]["outcome"] == "PASS"
        R.add(
            ["13.4"],
            [],
            ok,
            f"control: tasks {shared} define one initial state and start from one ledger initial "
            f"state, each replaying its own cassette: the matrix is PASS rc={rc}",
            "" if ok else " ".join(res[0]["diagnostics"])[-600:] if res else out[-600:],
        )
        swapped = {shared[0]: shared[1], shared[1]: shared[0]}
        for label, matrix_instances, needle in (
            (
                f"tasks {shared} with one defined initial state started from two ledger states",
                instances(small, tasks=shared, state_of=lambda task: task),
                "define one initial state but start from 2 different ledger initial states",
            ),
            (
                f"the labels of {shared} swapped: each Instance replays the other task's cassette",
                instances(
                    small,
                    tasks=shared,
                    requests_of=lambda task, seed: [request(swapped[task], seed)],
                ),
                "recorded adapter requests are not in the cassette of task",
            ),
        ):
            conf["instances"] = matrix_instances
            write_driver(root, "5.2", conf)
            rc, res, out = qualify(root, "--case", "CWR-018")
            diags = " ".join(res[0]["diagnostics"]) if res else out
            R.add(
                ["13.4"],
                [],
                rc == 1 and needle in diags,
                f"{label}: the task labels are not bound and the campaign is FAIL rc={rc}",
                "" if needle in diags else diags[-600:],
            )
    conf["instances"] = instances(small, tasks=distinct, drop=1)
    write_driver(root, "5.2", conf)
    rc, res, out = qualify(root, "--case", "CWR-018")
    ok = rc == 1 and "incomplete arm matrix" in " ".join(res[0]["diagnostics"])
    R.add(
        ["13.4"],
        [],
        ok,
        f"one missing cell makes the matrix incomplete and the campaign FAIL rc={rc}",
    )
    for label, matrix_instances, needle in (
        (
            f"8 ledgers of {distinct}, whose initial.kvx define different states, relabelled to "
            "start from one ledger initial state",
            instances(small, tasks=distinct, state_of=lambda task: "one"),
            "define different initial states but start from the same ledger initial state",
        ),
        (
            "ledger header arms without the H<n>/ hypothesis prefix",
            instances(small, tasks=distinct, prefix=False),
            "names no hypothesis",
        ),
    ):
        conf["instances"] = matrix_instances
        write_driver(root, "5.2", conf)
        rc, res, out = qualify(root, "--case", "CWR-018")
        diags = " ".join(res[0]["diagnostics"]) if res else out
        R.add(
            ["13.4"],
            [],
            rc == 1 and needle in diags,
            f"{label}: the matrix is not filled and the campaign is FAIL rc={rc}",
            "" if needle in diags else diags[-600:],
        )
    conf["instances"] = instances(small, tasks=distinct)
    conf["preregistration_digest"] = "sha256:" + "e1" * 32
    write_driver(root, "5.2", conf)
    write_driver(
        root,
        "5.3",
        dict(
            passing("CWR-018", keys53, preregistration_digest="sha256:" + "e1" * 32), instances=[]
        ),
    )
    rc, res, out = qualify(root, "--case", "CWR-018")
    ok = rc == 1 and "pre-registration digest" in " ".join(res[0]["diagnostics"])
    R.add(
        ["13.4"],
        [],
        ok,
        f"a pre-registration digest that differs from the registered one is FAIL rc={rc}",
    )


def scenario_validator(work: Path) -> None:
    """Implementation-stage authoring rules: statuses and product evidence need checker-accepted records."""
    root = make_root(
        work,
        "pack",
        {("case.CWR-005", "registered_builds"): [B1], ("case.CWR-009", "registered_builds"): [B1]},
        full_pack=True,
    )
    same = (root / "tests/cwr/check_evidence.py").read_bytes() == CHECKER.read_bytes()
    write_driver(root, "1.3", passing("CWR-005", ["a1", "a2", "a3", "a4"]))
    write_driver(root, "2.3", passing("CWR-009", ["a2", "a3", "a4", "a5", "a6", "a7", "a9"]))
    write_driver(root, "4.1", passing("CWR-009", ["a1", "a8"]))
    write_driver(root, "2.1", passing("CWR-006", ["a1", "a2", "a3", "a4", "a5"]))
    rc, res, out = qualify(root, "--case", "CWR-005")
    rc2, res2, _ = qualify(root, "--case", "CWR-009", "--task", "2.3")
    ok = same and rc == 0 and rc2 == 0
    R.add(
        ["13.4", "13.2"],
        [],
        ok,
        f"implementation-stage pack prepared: CWR-005 case PASS rc={rc}, CWR-009 component PASS rc={rc2}",
        "" if ok else out[-600:],
    )
    if not ok:
        return
    case_record = res[0]["record"]
    task_record = res2[0]["record"]
    spec_path = root / "spec/cwr/spec.kvx"
    base_spec = spec_path.read_text()
    base_spec = set_key(
        set_key(base_spec, "meta", "stage", "implementation"), "meta", "status", "in_progress"
    )
    # hermetic: every case UNRUN and every task pending, as in this root's fresh registry; each
    # attempt below sets the statuses it is about
    for ident in re.findall(r"^\[case\.(CWR-\d{3})\]", base_spec, re.M):
        base_spec = set_key(base_spec, f"case.{ident}", "status", "UNRUN")
    for ident in re.findall(r"^\[task\.(\d+\.\d+)\]", base_spec, re.M):
        base_spec = set_key(base_spec, f"task.{ident}", "status", "pending")
    qual_path = root / "spec/cwr/qualification.kvx"
    base_qual = qual_path.read_text()
    registry_path = root / "tests/cwr/registry.kvx"
    base_registry = registry_path.read_text()

    def attempt(
        label,
        spec_edits=(),
        qual_edits=(),
        registry_edits=(),
        expect=None,
        design=False,
        mutate=None,
    ):
        nonlocal base_registry
        text = (
            base_spec
            if not design
            else set_key(set_key(base_spec, "meta", "stage", "design"), "meta", "status", "pending")
        )
        for section, key, value in spec_edits:
            text = set_key(text, section, key, value)
        spec_path.write_text(text)
        q = base_qual
        for section, key, value in qual_edits:
            q = set_key(q, section, key, value)
        qual_path.write_text(q)
        reg = base_registry
        for section, key, value in registry_edits:
            reg = set_key(reg, section, key, value)
        registry_path.write_text(reg)
        undo = mutate() if mutate else None
        rc_r, _rep_r = validator(root, "--render")
        rc_p, rep_p = validator(root)
        if undo:
            undo()
        errors = rep_p.get("errors", [])
        if expect is None:
            ok = rc_r == 0 and rc_p == 0 and rep_p.get("status") == "PASS"
        else:
            ok = rc_p != 0 and any(expect in e for e in errors)
        R.add(
            ["13.4", "13.2"],
            [],
            ok,
            f"validator (implementation stage) {label}: rc={rc_p} "
            + (
                "PASS expected"
                if expect is None
                else f"expects '{expect}' found={any(expect in e for e in errors)}"
            ),
            "" if ok else json.dumps(errors)[:700],
        )

    attempt(
        "CWR-005 PASS backed by the registry and an accepted case record",
        [("case.CWR-005", "status", "PASS")],
    )
    attempt(
        "CWR-006 PASS with no registry backing",
        [("case.CWR-006", "status", "PASS")],
        expect="case CWR-006: status PASS is not backed by the registry",
    )
    attempt(
        "CWR-005 FAIL while the registry records PASS",
        [("case.CWR-005", "status", "FAIL")],
        expect="case CWR-005: status FAIL is not backed by the registry",
    )
    attempt(
        "CWR-009 PASS backed only by component evidence",
        [("case.CWR-009", "status", "PASS")],
        registry_edits=[
            ("case.CWR-009", "status", "PASS"),
            ("case.CWR-009", "evidence", task_record),
        ],
        expect="is component (task-scope) evidence and cannot qualify the case",
    )
    attempt(
        "CWR-005 PASS pointing at a missing record",
        [("case.CWR-005", "status", "PASS")],
        registry_edits=[
            ("case.CWR-005", "evidence", "tests/cwr/evidence/CWR-005/20991231T000000Z-1.json")
        ],
        expect="does not exist",
    )

    def corrupt_ledger():
        record = load(root, case_record)
        ledger = root / record["ledgers"][0]["path"]
        original = ledger.read_bytes()
        os.chmod(ledger, 0o644)
        ledger.write_bytes(original.replace(b'"seed": 7', b'"seed": 8'))
        return lambda: ledger.write_bytes(original)

    attempt(
        "CWR-005 PASS whose ledger was modified",
        [("case.CWR-005", "status", "PASS")],
        expect="is rejected by check_evidence.py",
        mutate=corrupt_ledger,
    )
    forged = str(fabricate_cwr001(root, case_record).relative_to(root))
    index_append(root, root / forged, load(root, forged))
    base_registry = (root / "tests/cwr/registry.kvx").read_text()
    attempt(
        "CWR-001 PASS on a fabricated, indexed record while task 0.3 has no driver",
        [("case.CWR-001", "status", "PASS")],
        registry_edits=[("case.CWR-001", "status", "PASS"), ("case.CWR-001", "evidence", forged)],
        expect="verification driver of task(s) 0.3 does not exist",
    )
    (root / "tests/cwr/cases/0_3.py").write_text("print('stand-in driver for task 0.3')\n")
    attempt(
        "CWR-001 PASS on a fabricated, indexed record with a driver present",
        [("case.CWR-001", "status", "PASS")],
        registry_edits=[("case.CWR-001", "status", "PASS"), ("case.CWR-001", "evidence", forged)],
        expect="is rejected by check_evidence.py",
    )
    (root / "tests/cwr/cases/0_3.py").unlink()
    rc, res, out = qualify(root, "--case", "CWR-005")
    newer = res[0]["record"] if res else ""
    base_registry = (root / "tests/cwr/registry.kvx").read_text()
    attempt(
        "CWR-005 PASS whose registry evidence is an older attempt than the latest in the index",
        [("case.CWR-005", "status", "PASS")],
        registry_edits=[("case.CWR-005", "evidence", case_record)],
        expect="latest accepted case-scope attempt in the index is PASS from " + newer,
    )
    attempt(
        "CWR-005 PASS backed by the latest accepted attempt", [("case.CWR-005", "status", "PASS")]
    )
    attempt(
        "CWR-005 status DONE",
        [("case.CWR-005", "status", "DONE")],
        expect="status must be UNRUN, PASS, FAIL or BLOCKED",
    )
    attempt(
        "qualification entry claims product evidence without a record",
        qual_edits=[("progress.authoring_validation", "product_evidence", True)],
        expect="names no evidence record",
    )
    attempt(
        "qualification entry claims product evidence with a missing record",
        qual_edits=[
            ("progress.authoring_validation", "product_evidence", True),
            ("progress.authoring_validation", "evidence", "tests/cwr/evidence/CWR-005/nope.json"),
        ],
        expect="does not exist",
    )
    attempt(
        "qualification entry claims product evidence with an accepted record",
        qual_edits=[
            ("progress.authoring_validation", "product_evidence", True),
            ("progress.authoring_validation", "evidence", case_record),
        ],
    )
    attempt("task 1.3 done with its driver present", [("task.1.3", "status", "done")])
    attempt(
        "task 2.2 implemented without its driver",
        [("task.2.2", "status", "implemented")],
        expect="task 2.2: implemented task lacks verification driver",
    )
    attempt(
        "design stage is unchanged: a backed CWR-005 PASS is still refused",
        [("case.CWR-005", "status", "PASS")],
        design=True,
        expect="case CWR-005: product qualification must remain UNRUN",
    )
    attempt(
        "design stage is unchanged: product_evidence true is still refused",
        qual_edits=[
            ("progress.authoring_validation", "product_evidence", True),
            ("progress.authoring_validation", "evidence", case_record),
        ],
        design=True,
        expect="must state product_evidence false while cases are UNRUN",
    )


STAND_IN_019 = r"""#!/usr/bin/env python3
# Stand-in task 0.2 driver generated by tests/cwr/cases/0_2.py: it prints PASS for every CWR-019 assertion
# and, when configured, hands a trap suite to the runner as attempt-level final state.
import json, os, shutil, sys
from pathlib import Path
CONF = json.loads({conf!r})
out = Path(os.environ["CWR_EVIDENCE_DIR"])
if CONF.get("suite"):
    shutil.copyfile(CONF["suite"], out / "cwr019-suite.json")
    (out / "attempt.json").write_text(json.dumps({{"final_states": ["cwr019-suite.json"]}}))
for n in range(1, 9):
    print(f"ASSERT CWR-019.a{{n}} PASS stand-in observation")
"""


def build_suite(work: Path) -> Path | None:
    """The CWR-019 trap suite: a small repository written by the real runner. It holds a genuine CWR-005
    case PASS (the base the checker's oracle forges), a CWR-009 component PASS and a CWR-005 attempt whose
    driver exited 0 without assertions."""
    root = make_root(
        work,
        "suite",
        {("case.CWR-005", "registered_builds"): [B1], ("case.CWR-009", "registered_builds"): [B1]},
    )
    write_driver(root, "1.3", passing("CWR-005", ["a1", "a2", "a3", "a4"]))
    rc1, res1, out1 = qualify(root, "--case", "CWR-005")
    write_driver(
        root,
        "2.3",
        passing("CWR-009", ["a2", "a3", "a4", "a5", "a6", "a7", "a9"], inst_id="01FIXTURE009GW"),
    )
    rc2, res2, out2 = qualify(root, "--case", "CWR-009", "--task", "2.3")
    write_driver(root, "1.3", passing("CWR-005", []))
    rc3, res3, out3 = qualify(root, "--case", "CWR-005")
    ok = (
        rc1 == 0
        and res1[0]["outcome"] == "PASS"
        and rc2 == 0
        and res2[0]["outcome"] == "PASS"
        and rc3 == 1
        and res3[0]["outcome"] == "FAIL"
        and res3[0]["accepted"]
    )
    R.add(
        ["13.3"],
        [],
        ok,
        "trap suite written by the real runner: CWR-005 case PASS, CWR-009 component PASS, "
        "CWR-005 zero-assertion attempt recorded FAIL",
        "" if ok else (out1 + out2 + out3)[-800:],
    )
    if not ok:
        return None
    files = {"tests/cwr/registry.kvx": (root / "tests/cwr/registry.kvx").read_text()}
    for path in sorted((root / "tests/cwr/evidence").rglob("*")):
        rel = str(path.relative_to(root))
        if path.is_file() and "/.work/" not in rel:
            files[rel] = path.read_text()
    suite = {
        "format": "cwr.cwr019-suite",
        "version": 1,
        "base": res1[0]["record"],
        "component": res2[0]["record"],
        "runner_zero": res3[0]["record"],
        "files": files,
    }
    path = work / SUITE_NAME
    path.write_text(json.dumps(suite, indent=1, sort_keys=True) + "\n")
    return path


def scenario_selfcheck(work: Path, suite: Path | None) -> None:
    """13.3: CWR-019 itself is recomputed by the checker's built-in oracle from the trap suite, never from
    the driver's ASSERT lines."""
    if suite is None:
        R.add(["13.3"], ["a1"], False, "no trap suite to qualify CWR-019 with")
        return
    clause_of = {
        "a1": "13.3",
        "a2": "13.3",
        "a3": "13.3",
        "a4": "13.3",
        "a5": "13.3",
        "a6": "13.4",
        "a7": "13.2",
        "a8": "13.2",
    }
    root = make_root(work, "selfcheck")
    driver = root / "tests/cwr/cases/0_2.py"
    driver.write_text(STAND_IN_019.format(conf=json.dumps({"suite": str(suite)})))
    rc, res, out = qualify(root, "--case", "CWR-019")
    ok = rc == 0 and res and res[0]["outcome"] == "PASS" and res[0]["accepted"]
    R.add(
        ["13.3"],
        [],
        ok,
        f"stand-in CWR-019 attempt carrying the trap suite is PASS rc={rc}",
        "" if ok else out[-900:],
    )
    record = load(root, res[0]["record"]) if res else {"recomputed": []}
    for item in record["recomputed"]:
        key = item["id"].split(".")[1]
        R.add(
            [clause_of[key]],
            [key],
            item["outcome"] == "PASS",
            f"checker's CWR-019 oracle recomputes {item['id']} {item['outcome']}: {item['diagnostic'][:300]}",
        )
    if res:
        rc_c, _out_c = check(root, root / res[0]["record"])
        st = status(root, "CWR-019")
        R.add(
            ["13.3"],
            [],
            rc_c == 0 and st["status"] == "PASS",
            f"checker accepts it (rc={rc_c}) and derives {st['status']}",
        )

    # ASSERT lines alone (no suite): recomputed FAIL, never PASS
    driver.write_text(STAND_IN_019.format(conf=json.dumps({})))
    rc, res, out = qualify(root, "--case", "CWR-019")
    rec = load(root, res[0]["record"]) if res else {"recomputed": []}
    ok = (
        rc == 1
        and res[0]["outcome"] == "FAIL"
        and res[0]["accepted"]
        and rec["recomputed"]
        and all(a["outcome"] == "FAIL" for a in rec["recomputed"])
    )
    R.add(
        ["13.3"],
        ["a1"],
        ok,
        f"a CWR-019 driver printing ASSERT PASS for a1..a8 without evidence is recorded FAIL "
        f"by recomputation rc={rc}",
        "" if ok else out[-600:],
    )
    if res:
        path = forge(
            root,
            res[0]["record"],
            lambda r, d: (
                claim_pass(r),
                r.update(recomputed=[dict(a, outcome="PASS") for a in r["recomputed"]]),
                rehash_all(r, root),
            ),
        )
        index_append(root, path, load(root, str(path.relative_to(root))))
        expect_reject(
            root,
            path,
            "recomputed FAIL by the independent oracle",
            ["13.3"],
            ["a1"],
            "that attempt forged to PASS, indexed and pinned",
        )
        st = status(root, "CWR-019")
        R.add(
            ["13.3"],
            ["a7"],
            st["rc"] == 1 and st["status"] is None,
            f"with that forgery in the index, --status refuses to derive CWR-019 rather than fall back (rc={st['rc']})",
        )
        lines = (root / INDEX_REL).read_text().splitlines()[:-1]
        (root / INDEX_REL).write_text("".join(ln + "\n" for ln in lines))
        index_reseal(root)

    # a suite whose base attempt is not genuine (its ledger edited) cannot qualify CWR-019
    data = json.loads(suite.read_text())
    base = json.loads(data["files"][data["base"]])
    ledger_rel = base["ledgers"][0]["path"]
    data["files"][ledger_rel] = data["files"][ledger_rel].replace(
        '"status": "ok"', '"status": "error"', 1
    )
    bad = work / "bad-suite" / SUITE_NAME
    bad.parent.mkdir()
    bad.write_text(json.dumps(data))
    driver.write_text(STAND_IN_019.format(conf=json.dumps({"suite": str(bad)})))
    rc, res, out = qualify(root, "--case", "CWR-019")
    rec = load(root, res[0]["record"]) if res else {"recomputed": []}
    ok = (
        rc == 1
        and res[0]["outcome"] == "FAIL"
        and all(a["outcome"] == "FAIL" for a in rec["recomputed"])
    )
    R.add(
        ["13.3"],
        ["a3"],
        ok,
        f"a trap suite whose base ledger was edited is recomputed FAIL for every assertion rc={rc}",
    )
    st = status(root, "CWR-019")
    R.add(
        ["13.3"],
        [],
        st["status"] == "FAIL" and registry_case(root, "CWR-019")["status"] == "FAIL",
        f"the latest attempt decides: registry and --status say {st['status']}",
    )


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="cwr_0_2_"))
    base = REPO / "tests/cwr/evidence"
    real_index = (REPO / INDEX_REL).read_text().splitlines() if (REPO / INDEX_REL).is_file() else []

    def registry_without_runner_lines() -> str:
        # other drivers qualified concurrently move the index pin and case outcomes; nothing else
        return "".join(
            line
            for line in REGISTRY.read_text().splitlines(keepends=True)
            if not re.match(r"(index_length|index_head|status|evidence)\s*=", line)
        )

    def evidence_snapshot() -> dict:
        # .work is the runner's staging area; closure run logs and the index belong to other runs
        out = {}
        for p in sorted(base.rglob("*")) if base.exists() else []:
            rel = p.relative_to(base)
            if {".work", "closure"} & set(rel.parts) or rel.as_posix() == "index.jsonl":
                continue
            out[rel.as_posix()] = p.stat().st_size if p.is_file() else -1
        return out

    real_registry = registry_without_runner_lines()
    real_evidence = evidence_snapshot()
    try:
        suite = build_suite(work)
        evidence_dir = os.environ.get("CWR_EVIDENCE_DIR")
        if suite is not None and evidence_dir:
            shutil.copyfile(suite, Path(evidence_dir) / SUITE_NAME)
            (Path(evidence_dir) / "attempt.json").write_text(
                json.dumps({"final_states": [SUITE_NAME]})
            )
        genuine = scenario_records(work)
        scenario_traps(work)
        scenario_forgeries(work)
        scenario_selfcheck(work, suite)
        scenario_separation(work, work / "records", genuine)
        scenario_inventory(work, work / "records", genuine)
        scenario_component(work)
        scenario_hypothesis(work)
        scenario_validator(work)
        scenario_hardening(work)
        scenario_protocol()
        # attempts other drivers record while this one runs only add runner-named files
        after = evidence_snapshot()
        index_now = (
            (REPO / INDEX_REL).read_text().splitlines() if (REPO / INDEX_REL).is_file() else []
        )
        runner_named = re.compile(r"CWR-\d{3}(/\d{8}T\d{6}Z-\d+(\.json|/.*)?)?")
        R.add(
            ["13.2"],
            ["a7"],
            registry_without_runner_lines() == real_registry
            and all(after.get(k) == v for k, v in real_evidence.items())
            and all(runner_named.fullmatch(k) for k in set(after) - set(real_evidence))
            and index_now[: len(real_index)] == real_index,
            "the real registry and evidence directory are untouched by this driver",
        )
    finally:
        for path in work.rglob("*"):
            with contextlib.suppress(OSError):
                os.chmod(path, 0o755 if path.is_dir() else 0o644)
        shutil.rmtree(work, ignore_errors=True)

    for ident, checks in ASSERTIONS.items():
        failed = [label for ok, label in checks if not ok]
        ok = bool(checks) and not failed
        print(
            f"ASSERT {ident} {'PASS' if ok else 'FAIL'} {sum(o for o, _ in checks)}/{len(checks)} checks"
            + (f"; failed: {' | '.join(failed)}" if failed else ""),
            flush=True,
        )
    all_ok = True
    for clause, checks in R.checks.items():
        failed = [label for ok, label in checks if not ok]
        ok = bool(checks) and not failed
        all_ok &= ok
        print(
            f"CLAUSE {clause} {'PASS' if ok else 'FAIL'} {sum(o for o, _ in checks)}/{len(checks)} checks passed"
            + (f"; failed: {' | '.join(failed)}" if failed else ""),
            flush=True,
        )
    all_ok &= all(checks and all(ok for ok, _ in checks) for checks in ASSERTIONS.values())
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())

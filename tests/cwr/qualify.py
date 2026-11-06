#!/usr/bin/env python3
# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""CWR qualification runner (task 0.2; spec/cwr/verification.md sections 1, 2 and 5).

  python3 tests/cwr/qualify.py --case CWR-NNN              full case: run every task driver of the case
  python3 tests/cwr/qualify.py --case CWR-NNN --task W.N   component: run one task's driver for that case
  python3 tests/cwr/qualify.py --task W.N                  component: run one driver, one record per case
                                                           whose registered subsets name the task
  python3 tests/cwr/qualify.py --check-registry            validate the registry against spec.kvx

Driver protocol. A task driver is tests/cwr/cases/W_N.py, run as `python3 <driver>` from the repository
root with these environment variables added:
  CWR_QUALIFY=1, CWR_TASK, CWR_CASES (comma separated), CWR_EVIDENCE_DIR (an empty directory the driver
  may write into). Drivers honour CARGO_TARGET_DIR themselves; the runner passes the environment through.
Its stdout lines
  ASSERT <CWR-NNN.aK> PASS|FAIL [diagnostic]      one observed assertion
  BLOCKED <missing resource>                      the attempt could not run (never lowers a target)
are the observations; every other line is ignored. A driver that touched Builds or Instances writes
$CWR_EVIDENCE_DIR/attempt.json:
  {"build_hash", "builds", "experimental_flags", "world": {"name", "version"}, "cassette_digest",
   "gateway_mode", "model": {"identity", "sampling"}, "preregistration_digest", "sandbox",
   "instances": [{"id", "seed", "tail_size", "arm", "hypothesis", "task", "build",
                  "initial_state_digest", "ledger": "<path in CWR_EVIDENCE_DIR>",
                  "final_state": "<path in CWR_EVIDENCE_DIR>" | null, "case": optional CWR-NNN}],
   "final_states": ["<path in CWR_EVIDENCE_DIR>", ...]}       attempt-level final state (no Instance)
Every key is optional; ledgers and final state must be files inside CWR_EVIDENCE_DIR.

For each case the runner reserves tests/cwr/evidence/<case>/<timestamp>-<n>/ with an exclusive mkdir,
where <timestamp> is the attempt's UTC start second (it cannot be chosen), copies the driver logs and
declared artifacts into it, writes the attempt record tests/cwr/evidence/<case>/<timestamp>-<n>.json
exactly once (O_EXCL, then read-only) and never overwrites or edits an existing record. Before writing
it runs tests/cwr/check_evidence.py on the staged record; the checker recomputes every registered
assertion with the case's independent oracle and the outcome is PASS only when the driver observed
exactly the registered inventory for the scope, all PASS, and every recomputed assertion is PASS. A
driver exit code or ASSERT line alone never makes a PASS.

Every written record is appended, under an exclusive lock, to the hash-chained attempt index
tests/cwr/evidence/index.jsonl (seq, prev = sha256 of the previous line, record path and sha256,
case, scope, task, outcome, accepted, appended_at), and the new head and length are pinned in the
registry's [meta] index_head and index_length. The index order is the attempt order. Only a
checker-accepted case-scope record updates the case's status and evidence in registry.kvx; a
task-scope (component) record never changes a case status.

Exit codes: 0 PASS, 1 FAIL, 3 BLOCKED, 2 refused (invalid registry, unknown case or task, missing
driver, no registered inventory), 4 the checker rejected the record as written.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
CHECKER = HERE / "check_evidence.py"
DEFAULT_ROOT = HERE.parents[1]
REGISTRY_REL = "tests/cwr/registry.kvx"
SPEC_REL = "spec/cwr/spec.kvx"
EVIDENCE_REL = "tests/cwr/evidence"
INDEX_REL = "tests/cwr/evidence/index.jsonl"
GENESIS = "sha256:" + "0" * 64
SCHEMA_VERSION = 1
TIERS = {"component", "integration", "campaign", "release"}
STATUSES = {"UNRUN", "PASS", "FAIL", "BLOCKED"}
ASSERT_LINE = re.compile(r"^ASSERT (\S+) (PASS|FAIL)(?: (.*))?$")
BLOCKED_LINE = re.compile(r"^BLOCKED (\S.*)$")
SHA = re.compile(r"^sha256:[0-9a-f]{64}$")
REVISION_TREES = {
    "compiler_revision": "crates/cwr-compiler",
    "kernel_revision": "crates/cwr-kernel",
    "sdk_revision": "sdk/python/cwr",
}
EXIT = {"PASS": 0, "FAIL": 1, "BLOCKED": 3}


class RefusedError(Exception):
    """The runner refuses the request and exits 2; the message says why."""


# ------------------------------------------------------------------ small helpers


def utc(ts: float | None = None) -> str:
    moment = datetime.fromtimestamp(time.time() if ts is None else ts, UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def tree_digest(directory: Path) -> str | None:
    """Content digest of a source tree (path and bytes of every file), or None when absent."""
    if not directory.is_dir():
        return None
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        rel = path.relative_to(directory)
        if not path.is_file() or {"target", "__pycache__", ".git"} & set(rel.parts):
            continue
        digest.update(
            str(rel.as_posix()).encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest()
        )
    return "sha256:" + digest.hexdigest()


def attempt_digest(directory: Path) -> str:
    """Digest of every file in an attempt directory: one 'relative-path sha256' line per file."""
    files = sorted(
        (p for p in directory.rglob("*") if p.is_file()),
        key=lambda p: p.relative_to(directory).as_posix(),
    )
    lines = "".join(f"{p.relative_to(directory).as_posix()} {sha256_file(p)}\n" for p in files)
    return sha256_bytes(lines.encode())


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
    sections: dict = {}
    current = None
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        header = re.fullmatch(r"\[([A-Za-z0-9_.-]+)\]", line)
        if header:
            if header.group(1) in sections:
                raise RefusedError(f"{label}:{number}: duplicate section [{header.group(1)}]")
            current = sections[header.group(1)] = {}
            continue
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_-]*)\s*=\s*(.+)", line)
        if not match or current is None:
            raise RefusedError(f"{label}:{number}: unsupported or out-of-section syntax")
        if match.group(1) in current:
            raise RefusedError(f"{label}:{number}: duplicate key {match.group(1)}")
        try:
            current[match.group(1)] = strict_json(match.group(2))
        except ValueError as error:
            raise RefusedError(
                f"{label}:{number}: value must be one JSON literal: {error}"
            ) from error
    return sections


# ------------------------------------------------------------------ registry loader


class Registry:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / REGISTRY_REL
        if not self.path.is_file():
            raise RefusedError(f"registry missing: {REGISTRY_REL}")
        self.text = self.path.read_text()
        self.sha256 = sha256_bytes(self.text.encode())
        self.sections = parse_kvx(self.text, REGISTRY_REL)
        self.meta = self.sections.get("meta", {})
        self.cases = {k[5:]: v for k, v in self.sections.items() if k.startswith("case.")}
        self.errors = self.validate()

    @staticmethod
    def inventory(case: dict) -> list[str]:
        keys = sorted((k for k in case if re.fullmatch(r"a[1-9]\d*", k)), key=lambda k: int(k[1:]))
        return keys if [int(k[1:]) for k in keys] == list(range(1, len(keys) + 1)) else []

    def validate(self) -> list[str]:
        errors: list[str] = []
        if (
            self.meta.get("format") != "cwr.registry"
            or type(self.meta.get("registry_version")) is not int
            or self.meta.get("schema_version") != SCHEMA_VERSION
        ):
            errors.append(
                "registry meta must declare format cwr.registry, registry_version and schema_version 1"
            )
        if (
            type(self.meta.get("index_length")) is not int
            or self.meta["index_length"] < 0
            or (self.meta.get("index_head") is None) != (self.meta.get("index_length") == 0)
            or (
                self.meta.get("index_head") is not None
                and not SHA.match(str(self.meta.get("index_head")))
            )
        ):
            errors.append(
                "registry meta must pin index_length (>= 0) and index_head (null exactly when the index is empty)"
            )
        spec_path = self.root / SPEC_REL
        spec_cases = {}
        if spec_path.is_file():
            spec = parse_kvx(spec_path.read_text(), SPEC_REL)
            spec_cases = {k[5:]: v for k, v in spec.items() if k.startswith("case.")}
            if set(spec_cases) != set(self.cases):
                errors.append(
                    f"registry cases {sorted(self.cases)} != spec cases {sorted(spec_cases)}"
                )
        else:
            errors.append(f"{SPEC_REL} is missing; the registry cannot be cross-checked")
        known = {
            "title",
            "tier",
            "reqs",
            "tasks",
            "subsets",
            "requires_build",
            "registered_builds",
            "registered_seeds",
            "cassette_digest",
            "status",
            "evidence",
            "matrix",
            "preregistration_digest",
        }
        for ident, case in sorted(self.cases.items()):
            where = f"registry case {ident}"
            if not re.fullmatch(r"CWR-\d{3}", ident):
                errors.append(f"{where}: invalid case id")
            if case.get("tier") not in TIERS:
                errors.append(f"{where}: unknown tier {case.get('tier')!r}")
            keys = self.inventory(case)
            stray = sorted(set(case) - known - set(keys))
            if stray:
                errors.append(f"{where}: unknown keys or non-consecutive assertions {stray}")
            if not keys:
                errors.append(
                    f"{where}: no registered assertion inventory (a1..aN); the case cannot be marked PASS"
                )
            for key in keys:
                if not isinstance(case[key], str) or not case[key].strip():
                    errors.append(f"{where}: assertion {key} must be nonempty text")
            spec_case = spec_cases.get(ident)
            if spec_case is not None:
                for fld in ("tier", "reqs", "tasks"):
                    if case.get(fld) != spec_case.get(fld):
                        errors.append(
                            f"{where}: {fld} {case.get(fld)!r} != spec.kvx {spec_case.get(fld)!r}"
                        )
            subsets = case.get("subsets")
            if not isinstance(subsets, dict) or set(subsets) != set(case.get("tasks") or []):
                errors.append(f"{where}: subsets must name exactly the case's tasks")
            else:
                covered = set()
                for task, subset in subsets.items():
                    if (
                        not isinstance(subset, list)
                        or not subset
                        or not set(subset) <= set(keys)
                        or len(subset) != len(set(subset))
                    ):
                        errors.append(
                            f"{where}: subset for task {task} must be a nonempty list of registered assertions"
                        )
                    else:
                        covered |= set(subset)
                if keys and covered != set(keys):
                    errors.append(
                        f"{where}: task subsets do not cover {sorted(set(keys) - covered)}"
                    )
            if type(case.get("requires_build")) is not bool:
                errors.append(f"{where}: requires_build must be a boolean")
            for fld in ("registered_builds", "registered_seeds"):
                if not isinstance(case.get(fld), list):
                    errors.append(f"{where}: {fld} must be a list")
            if not all(
                isinstance(b, str) and SHA.match(b) for b in case.get("registered_builds") or []
            ):
                errors.append(f"{where}: registered_builds must be sha256:<64hex>")
            if case.get("cassette_digest") is not None and not SHA.match(
                str(case.get("cassette_digest"))
            ):
                errors.append(f"{where}: cassette_digest must be null or sha256:<64hex>")
            if case.get("status") not in STATUSES:
                errors.append(f"{where}: status must be one of {sorted(STATUSES)}")
            elif (case.get("status") == "UNRUN") != (case.get("evidence") is None):
                errors.append(f"{where}: evidence must be null exactly when the status is UNRUN")
            if case.get("tier") == "campaign" and not isinstance(case.get("matrix"), dict):
                errors.append(f"{where}: a campaign case needs a registered arm matrix")
        return errors

    def update(self, updates: dict[str, dict]) -> None:
        """Rewrite existing key lines, {section: {key: value}}, atomically. The caller holds the index lock."""
        lines = self.path.read_text().splitlines(keepends=True)
        section, hits = None, 0
        wanted = sum(len(v) for v in updates.values())
        for index, line in enumerate(lines):
            stripped = line.strip()
            if re.fullmatch(r"\[.+\]", stripped):
                section = stripped[1:-1]
                continue
            match = re.match(r"([A-Za-z_][A-Za-z0-9_-]*)\s*=", stripped)
            if match and section in updates and match.group(1) in updates[section]:
                lines[index] = (
                    f"{match.group(1)} = {json.dumps(updates[section][match.group(1)])}\n"
                )
                hits += 1
        if hits != wanted:
            raise RefusedError(f"registry: cannot locate every line of {updates}")
        temp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        temp.write_text("".join(lines))
        os.replace(temp, self.path)


@contextmanager
def locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def index_line(entry: dict) -> str:
    return json.dumps(entry, sort_keys=True, separators=(",", ":"))


def read_index(root: Path, meta: dict) -> tuple[list[dict], str | None]:
    """Read the attempt index and verify its chain and the head pinned in the registry."""
    path = root / INDEX_REL
    data = path.read_bytes() if path.is_file() else b""
    if data and not data.endswith(b"\n"):
        raise RefusedError(
            f"{INDEX_REL}: the final entry is truncated; the evidence store needs repair"
        )
    entries, prev = [], GENESIS
    for number, line in enumerate(data.decode().splitlines()):
        try:
            entry = strict_json(line)
        except ValueError as error:
            raise RefusedError(f"{INDEX_REL}: entry {number} is not JSON: {error}") from error
        if (
            not isinstance(entry, dict)
            or entry.get("seq") != number
            or entry.get("prev") != prev
            or line != index_line(entry)
        ):
            raise RefusedError(
                f"{INDEX_REL}: entry {number} breaks the chain; the evidence store needs repair"
            )
        entries.append(entry)
        prev = sha256_bytes(line.encode())
    head = prev if entries else None
    if meta.get("index_length") != len(entries) or meta.get("index_head") != head:
        raise RefusedError(
            f"{INDEX_REL}: head {head} and length {len(entries)} do not match the registry's pinned "
            f"index_head {meta.get('index_head')} and index_length {meta.get('index_length')}"
        )
    return entries, head


# ------------------------------------------------------------------ running drivers


def driver_path(root: Path, task: str) -> Path:
    return root / "tests/cwr/cases" / (task.replace(".", "_") + ".py")


def run_driver(root: Path, task: str, cases: list[str], workdir: Path, timeout: float) -> dict:
    out_dir = workdir / task
    out_dir.mkdir(parents=True)
    driver = driver_path(root, task)
    command = [sys.executable, str(driver.relative_to(root))]
    env = dict(os.environ)
    env.update(
        {
            "CWR_QUALIFY": "1",
            "CWR_TASK": task,
            "CWR_CASES": ",".join(cases),
            "CWR_EVIDENCE_DIR": str(out_dir),
        }
    )
    started = time.monotonic()
    stdout_path, stderr_path = out_dir / "driver.stdout.log", out_dir / "driver.stderr.log"
    timed_out = False
    with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
        process = subprocess.Popen(
            command, cwd=root, env=env, stdout=out, stderr=err, stdin=subprocess.DEVNULL
        )
        try:
            exit_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            exit_code, timed_out = None, True
    return {
        "task": task,
        "command": ["python3", str(driver.relative_to(root))],
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "dir": out_dir,
        "stdout": stdout_path,
        "stderr": stderr_path,
    }


def load_manifest(run: dict) -> tuple[dict, list[str]]:
    path = run["dir"] / "attempt.json"
    if not path.is_file():
        return {}, []
    try:
        manifest = strict_json(path.read_text())
    except (ValueError, UnicodeDecodeError) as error:
        return {}, [f"task {run['task']}: attempt.json is not valid JSON: {error}"]
    if not isinstance(manifest, dict):
        return {}, [f"task {run['task']}: attempt.json must be an object"]
    return manifest, []


def sanitise(manifest: dict, task: str) -> tuple[dict, list[str]]:
    """Drop manifest values of the wrong shape, with a diagnostic, so the record stays schema-valid."""
    problems: list[str] = []

    def is_sha(v: object) -> bool:
        return isinstance(v, str) and bool(SHA.match(v))

    checks = {
        "build_hash": lambda v: v is None or is_sha(v),
        "builds": lambda v: isinstance(v, list) and all(is_sha(b) for b in v),
        "experimental_flags": lambda v: (
            isinstance(v, list)
            and all(isinstance(f, str) and re.fullmatch(r"--x-[a-z-]+", f) for f in v)
        ),
        "world": lambda v: (
            v is None
            or (
                isinstance(v, dict)
                and set(v) == {"name", "version"}
                and all(isinstance(x, str) and x for x in v.values())
            )
        ),
        "cassette_digest": lambda v: v is None or is_sha(v),
        "gateway_mode": lambda v: v in (None, "replay", "record", "live"),
        "model": lambda v: (
            v is None
            or (
                isinstance(v, dict)
                and set(v) == {"identity", "sampling"}
                and isinstance(v["identity"], str)
                and v["identity"]
                and isinstance(v["sampling"], dict)
            )
        ),
        "preregistration_digest": lambda v: v is None or is_sha(v),
        "sandbox": lambda v: v is None or isinstance(v, str),
        "instances": lambda v: isinstance(v, list),
        "final_states": lambda v: isinstance(v, list) and all(isinstance(p, str) and p for p in v),
    }
    clean = {}
    for key, value in manifest.items():
        if key not in checks:
            problems.append(f"task {task}: attempt.json has unknown key {key}")
        elif not checks[key](value):
            problems.append(f"task {task}: attempt.json {key} has an invalid value")
        else:
            clean[key] = value
    instances = []
    for index, inst in enumerate(clean.get("instances", [])):
        ok = (
            isinstance(inst, dict)
            and isinstance(inst.get("id"), str)
            and inst["id"]
            and type(inst.get("seed")) is int
            and inst["seed"] >= 0
            and (
                (type(inst.get("tail_size")) is int and inst["tail_size"] >= 1)
                or inst.get("tail_size") == "full"
            )
            and (inst.get("build") is None or is_sha(inst.get("build")))
            and (inst.get("build") is not None or is_sha(clean.get("build_hash")))
            and all(
                inst.get(k) is None or isinstance(inst.get(k), str) for k in ("arm", "task", "case")
            )
            and (
                inst.get("hypothesis") is None
                or re.fullmatch(r"H[1-4]", str(inst.get("hypothesis")))
            )
            and (
                inst.get("initial_state_digest") is None or is_sha(inst.get("initial_state_digest"))
            )
        )
        if ok:
            instances.append(inst)
        else:
            problems.append(
                f"task {task}: attempt.json instance {index} is malformed "
                "(needs id, seed >= 0, tail_size, a sha256 build and string arm/task/case)"
            )
    if "instances" in clean:
        clean["instances"] = instances
    return clean, problems


def inside(base: Path, rel) -> Path | None:
    if not isinstance(rel, str) or not rel:
        return None
    pure = PurePosixPath(rel)
    if pure.is_absolute() or ".." in pure.parts:
        return None
    path = base / rel
    return path if path.is_file() else None


# ------------------------------------------------------------------ attempt records


def reserve(evidence_dir: Path, stamp: str) -> tuple[str, Path]:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    n = 1
    while True:
        name = f"{stamp}-{n}"
        if not (evidence_dir / f"{name}.json").exists():
            try:
                (evidence_dir / name).mkdir()
                return name, evidence_dir / name
            except FileExistsError:
                pass
        n += 1


def run_checker(root: Path, record: dict) -> dict:
    process = subprocess.run(
        [sys.executable, str(CHECKER), "--root", str(root), "--staged", "-"],
        input=json.dumps(record),
        capture_output=True,
        text=True,
        timeout=600,
    )
    try:
        verdict = json.loads(process.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        verdict = {
            "accepted": False,
            "errors": [
                f"checker produced no verdict (exit {process.returncode}): "
                f"{process.stderr.strip()[-400:]}"
            ],
        }
    verdict["exit_code"] = process.returncode
    return verdict


def build_record(
    root: Path,
    registry: Registry,
    case_id: str,
    scope: str,
    task: str | None,
    runs: list[dict],
    stamp: str,
    argv: list[str],
    started: float,
    ended: float,
) -> tuple[dict, Path, str]:
    case = registry.cases[case_id]
    keys = Registry.inventory(case)
    if scope == "case":
        expected = [f"{case_id}.{k}" for k in keys]
        clauses = list(case["reqs"])
    else:
        expected = [f"{case_id}.{k}" for k in case["subsets"][task]]
        clauses = list(case["reqs"])
    case_dir = root / EVIDENCE_REL / case_id
    name, attempt_dir = reserve(case_dir, stamp)
    record_id = f"{case_id}/{name}"
    prefix = f"{EVIDENCE_REL}/{record_id}"
    diagnostics: list[str] = []

    invocations, observed, blocked = [], [], []
    scalars: dict[str, object] = {}
    builds: list[str] = []
    flags: list[str] = []
    instances, ledgers, finals = [], [], []

    def merge(key, value):
        if value is None:
            return
        if key in scalars and scalars[key] != value:
            diagnostics.append(f"conflicting {key} across drivers: {scalars[key]!r} vs {value!r}")
        scalars.setdefault(key, value)

    for run in runs:
        target = attempt_dir / run["task"]
        target.mkdir()
        shutil.copyfile(run["stdout"], target / "driver.stdout.log")
        shutil.copyfile(run["stderr"], target / "driver.stderr.log")
        for line in (target / "driver.stdout.log").read_text(errors="replace").splitlines():
            match = ASSERT_LINE.match(line)
            if match and match.group(1).startswith(case_id + "."):
                observed.append(
                    {
                        "id": match.group(1),
                        "outcome": match.group(2),
                        "diagnostic": match.group(3) or "",
                    }
                )
            match = BLOCKED_LINE.match(line)
            if match:
                blocked.append(match.group(1))
        invocations.append(
            {
                "task": run["task"],
                "command": run["command"],
                "exit_code": run["exit_code"],
                "timed_out": run["timed_out"],
                "duration_ms": run["duration_ms"],
                "stdout_log": f"{prefix}/{run['task']}/driver.stdout.log",
                "stdout_sha256": sha256_file(target / "driver.stdout.log"),
                "stderr_log": f"{prefix}/{run['task']}/driver.stderr.log",
                "stderr_sha256": sha256_file(target / "driver.stderr.log"),
            }
        )
        manifest, problems = load_manifest(run)
        manifest, more = sanitise(manifest, run["task"])
        diagnostics += problems + more
        if manifest:
            shutil.copyfile(run["dir"] / "attempt.json", target / "attempt.json")
        for key in (
            "build_hash",
            "cassette_digest",
            "gateway_mode",
            "model",
            "preregistration_digest",
            "sandbox",
            "world",
        ):
            merge(key, manifest.get(key))
        for build in [manifest.get("build_hash"), *list(manifest.get("builds") or [])]:
            if isinstance(build, str) and build not in builds:
                builds.append(build)
        for flag in manifest.get("experimental_flags") or []:
            if flag not in flags:
                flags.append(flag)
        for index, inst in enumerate(manifest.get("instances") or []):
            if not isinstance(inst, dict):
                diagnostics.append(f"task {run['task']}: instance {index} is not an object")
                continue
            if inst.get("case") not in (None, case_id):
                continue
            ledger_src = inside(run["dir"], inst.get("ledger"))
            if ledger_src is None:
                diagnostics.append(
                    f"task {run['task']}: instance {inst.get('id')!r} names no ledger inside CWR_EVIDENCE_DIR"
                )
                continue
            ledger_dst = target / inst["ledger"]
            ledger_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ledger_src, ledger_dst)
            ledger = {
                "path": f"{prefix}/{run['task']}/{inst['ledger']}",
                "sha256": sha256_file(ledger_dst),
            }
            final = None
            if inst.get("final_state") is not None:
                state_src = inside(run["dir"], inst.get("final_state"))
                if state_src is None:
                    diagnostics.append(
                        f"task {run['task']}: instance {inst.get('id')!r} final_state is not inside CWR_EVIDENCE_DIR"
                    )
                else:
                    state_dst = target / inst["final_state"]
                    state_dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(state_src, state_dst)
                    final = {
                        "path": f"{prefix}/{run['task']}/{inst['final_state']}",
                        "sha256": sha256_file(state_dst),
                    }
                    finals.append(final)
            ledgers.append(ledger)
            build = inst.get("build") or manifest.get("build_hash")
            if isinstance(build, str) and build not in builds:
                builds.append(build)
            instances.append(
                {
                    "id": inst.get("id"),
                    "seed": inst.get("seed"),
                    "tail_size": inst.get("tail_size"),
                    "arm": inst.get("arm"),
                    "hypothesis": inst.get("hypothesis"),
                    "task": inst.get("task"),
                    "build": build,
                    "initial_state_digest": inst.get("initial_state_digest"),
                    "ledger": ledger,
                    "final_state": final,
                }
            )
        for rel in manifest.get("final_states") or []:
            state_src = inside(run["dir"], rel)
            if state_src is None:
                diagnostics.append(
                    f"task {run['task']}: attempt-level final state {rel!r} is not a file inside CWR_EVIDENCE_DIR"
                )
                continue
            state_dst = target / rel
            state_dst.parent.mkdir(parents=True, exist_ok=True)
            if not state_dst.exists():
                shutil.copyfile(state_src, state_dst)
            finals.append(
                {"path": f"{prefix}/{run['task']}/{rel}", "sha256": sha256_file(state_dst)}
            )

    def unique(field_name):
        values = {json.dumps(i[field_name]) for i in instances}
        return instances[0][field_name] if len(values) == 1 else None

    observed_ids = [o["id"] for o in observed]
    gaps = [i for i in expected if i not in observed_ids]
    unexpected = sorted({i for i in observed_ids if i not in expected})
    codes = [r["exit_code"] for r in runs]
    exit_code = next((c if c is not None else -1 for c in codes if c != 0), 0)
    if not keys:
        diagnostics.append(f"case {case_id} has no registered assertion inventory")
    if not observed:
        diagnostics.append("the driver reported no assertions; an exit code alone is never a PASS")
    if gaps:
        diagnostics.append(f"coverage gaps: {gaps}")
    if unexpected:
        diagnostics.append(f"assertions outside the registered inventory: {unexpected}")
    failed = [o["id"] for o in observed if o["outcome"] != "PASS"]
    if failed:
        diagnostics.append(f"failed assertions: {failed}")
    for run in runs:
        if run["timed_out"]:
            diagnostics.append(f"task {run['task']} driver timed out")
        elif run["exit_code"] != 0:
            diagnostics.append(f"task {run['task']} driver exited {run['exit_code']}")
    if blocked:
        outcome = "BLOCKED"
    elif diagnostics:
        outcome = "FAIL"
    else:
        outcome = "PASS"

    ledger_digest = None
    if ledgers:
        lines = "".join(
            f"{i['path']} {i['sha256']}\n" for i in sorted(ledgers, key=lambda i: i["path"])
        )
        ledger_digest = sha256_bytes(lines.encode())
    model = scalars.get("model")
    world = scalars.get("world")
    record = {
        "schema_version": SCHEMA_VERSION,
        "record_id": record_id,
        "case": case_id,
        "scope": scope,
        "task": task if scope == "task" else None,
        "tier": case["tier"],
        "assertion_ids": expected,
        "clauses": clauses,
        "spec_revision": sha256_file(root / SPEC_REL),
        "registry_version": registry.meta.get("registry_version"),
        "registry_sha256": registry.sha256,
        "build_hash": scalars.get("build_hash", builds[0] if len(builds) == 1 else None),
        "builds": builds,
        "experimental_flags": flags,
        "world": world,
        "instance_id": instances[0]["id"] if len(instances) == 1 else None,
        "seed": unique("seed") if instances else None,
        "tail_size": unique("tail_size") if instances else None,
        "arm": unique("arm") if instances else None,
        "instances": instances,
        "cassette_digest": scalars.get("cassette_digest"),
        "gateway_mode": scalars.get("gateway_mode"),
        "model": model,
        "preregistration_digest": scalars.get("preregistration_digest"),
        **{key: tree_digest(root / rel) for key, rel in REVISION_TREES.items()},
        "host": {
            "hostname": platform.node(),
            "os": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "sandbox": scalars.get("sandbox"),
        },
        "started_at": utc(started),
        "ended_at": utc(ended),
        "duration_ms": int(max(0.0, ended - started) * 1000),
        "timestamp": utc(),
        "command": shlex.join(["python3", "tests/cwr/qualify.py", *argv]),
        "exit_code": exit_code,
        "invocations": invocations,
        "log": invocations[0]["stdout_log"],
        "log_sha256": invocations[0]["stdout_sha256"],
        "ledger_digest": ledger_digest,
        "ledgers": ledgers,
        "final_states": finals,
        "attempt_sha256": attempt_digest(attempt_dir),
        "observed": observed,
        "recomputed": [],
        "coverage_gaps": gaps,
        "unexpected_assertions": unexpected,
        "diagnostics": diagnostics,
        "blocked_reason": blocked[0] if outcome == "BLOCKED" else None,
        "outcome": outcome,
        "checker": {
            "program": "tests/cwr/check_evidence.py",
            "revision": sha256_file(CHECKER),
            "accepted": False,
            "errors": ["pending"],
        },
    }
    return record, attempt_dir, name


def write_once(path: Path, record: dict) -> None:
    data = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(path, 0o444)


def seal(directory: Path) -> None:
    for path in sorted(directory.rglob("*"), reverse=True):
        if path.is_file():
            os.chmod(path, 0o444)


def qualify(
    root: Path,
    registry: Registry,
    case_id: str,
    scope: str,
    task: str | None,
    runs: list[dict],
    stamp: str,
    argv: list[str],
    started: float,
    ended: float,
) -> dict:
    record, attempt_dir, name = build_record(
        root, registry, case_id, scope, task, runs, stamp, argv, started, ended
    )
    # first pass: the checker's independent recomputation of every registered assertion
    verdict = run_checker(root, record)
    record["recomputed"] = list(verdict.get("recomputed_assertions", []))
    if record["outcome"] == "PASS" and any(a["outcome"] != "PASS" for a in record["recomputed"]):
        record["outcome"] = "FAIL"
        record["diagnostics"] = record["diagnostics"] + [
            f"independent oracle: {a['id']} {a['outcome']}: {a['diagnostic']}"
            for a in record["recomputed"]
            if a["outcome"] != "PASS"
        ]
    verdict = run_checker(root, record)
    if not verdict.get("accepted") and record["outcome"] == "PASS":
        record["outcome"] = "FAIL"
        record["diagnostics"] = record["diagnostics"] + [
            "independent checker: " + e for e in verdict.get("errors", [])
        ]
        verdict = run_checker(root, record)
    if record["outcome"] != "PASS":
        record["diagnostics"] = record["diagnostics"] + [
            "independent checker: " + s
            for s in verdict.get("shortfalls", [])
            if "independent checker: " + s not in record["diagnostics"]
        ]
    record["checker"] = {
        "program": "tests/cwr/check_evidence.py",
        "revision": sha256_file(CHECKER),
        "accepted": bool(verdict.get("accepted")),
        "errors": list(verdict.get("errors", [])),
    }
    path = attempt_dir.parent / f"{name}.json"
    seal(attempt_dir)
    rel = str(path.relative_to(root))
    with locked(root / INDEX_REL):
        fresh = Registry(root)
        entries, head = read_index(root, fresh.meta)
        write_once(path, record)
        entry = {
            "seq": len(entries),
            "prev": head or GENESIS,
            "record": rel,
            "record_sha256": sha256_file(path),
            "case": case_id,
            "scope": scope,
            "task": record["task"],
            "outcome": record["outcome"],
            "accepted": record["checker"]["accepted"],
            "appended_at": utc(),
        }
        line = index_line(entry)
        with open(root / INDEX_REL, "a") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        updates = {
            "meta": {"index_head": sha256_bytes(line.encode()), "index_length": len(entries) + 1}
        }
        if scope == "case" and record["checker"]["accepted"]:
            updates[f"case.{case_id}"] = {"status": record["outcome"], "evidence": rel}
        fresh.update(updates)
    return {
        "record": rel,
        "case": case_id,
        "scope": scope,
        "task": record["task"],
        "outcome": record["outcome"],
        "accepted": record["checker"]["accepted"],
        "checker_errors": record["checker"]["errors"],
        "diagnostics": record["diagnostics"],
    }


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        description="Run CWR qualification drivers and write attempt records."
    )
    parser.add_argument("--case", help="CWR-NNN case to qualify")
    parser.add_argument("--task", help="W.N task whose driver to run (component scope)")
    parser.add_argument(
        "--check-registry", action="store_true", help="validate the registry and exit"
    )
    parser.add_argument(
        "--root", default=str(DEFAULT_ROOT), help="repository root (default: this checkout)"
    )
    parser.add_argument(
        "--timeout", type=float, default=7200.0, help="seconds per driver (default 7200)"
    )
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    try:
        registry = Registry(root)
        if args.check_registry:
            print(
                json.dumps(
                    {
                        "registry": REGISTRY_REL,
                        "errors": registry.errors,
                        "cases": len(registry.cases),
                    },
                    indent=2,
                )
            )
            return 0 if not registry.errors else 2
        if registry.errors:
            raise RefusedError("invalid registry: " + "; ".join(registry.errors))
        if not args.case and not args.task:
            parser.error("give --case, --task or both")
        read_index(root, registry.meta)
        if args.case:
            if args.case not in registry.cases:
                raise RefusedError(f"unknown case {args.case}")
            case = registry.cases[args.case]
            if args.task and args.task not in case["tasks"]:
                raise RefusedError(f"task {args.task} is not a registered task of {args.case}")
            targets = [(args.case, "task" if args.task else "case", args.task)]
            tasks = [args.task] if args.task else list(case["tasks"])
        else:
            owned = [c for c, v in sorted(registry.cases.items()) if args.task in v["tasks"]]
            if not owned:
                raise RefusedError(f"task {args.task} owns no registered case")
            targets = [(c, "task", args.task) for c in owned]
            tasks = [args.task]
        for task in tasks:
            if not driver_path(root, task).is_file():
                raise RefusedError(
                    f"driver for task {task} does not exist: {driver_path(root, task).relative_to(root)}"
                )
        started = time.time()
        stamp = datetime.fromtimestamp(started, UTC).strftime("%Y%m%dT%H%M%SZ")
        work = root / EVIDENCE_REL / ".work" / f"{stamp}-{os.getpid()}"
        work.mkdir(parents=True)
        try:
            runs = [
                run_driver(root, task, [c for c, _, _ in targets], work, args.timeout)
                for task in tasks
            ]
            ended = time.time()
            results = [
                qualify(root, registry, c, scope, t, runs, stamp, argv, started, ended)
                for c, scope, t in targets
            ]
        finally:
            shutil.rmtree(work, ignore_errors=True)
            with suppress(OSError):
                work.parent.rmdir()
    except RefusedError as error:
        print(json.dumps({"refused": str(error)}), file=sys.stderr)
        return 2
    for result in results:
        print(json.dumps(result, sort_keys=True))
    if not all(r["accepted"] for r in results):
        return 4
    return max(EXIT[r["outcome"]] for r in results)


if __name__ == "__main__":
    sys.exit(main())

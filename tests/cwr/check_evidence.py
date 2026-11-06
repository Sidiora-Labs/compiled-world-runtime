#!/usr/bin/env python3
# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Independent checker for CWR qualification attempt records (task 0.2).

Contract: spec/cwr/verification.md sections 2 and 5, requirement 13 in spec/cwr/spec.kvx.

This program is deliberately separate from the implementation under test and from the runner. It
imports only the Python standard library: never crates/cwr-*, the generated SDK under sdk/, the
runner tests/cwr/qualify.py or the authoring validator. It starts no process and opens no network
connection. It reads only attempt records, the files those records name (driver logs, ledgers and
final state under the record's own attempt directory), the append-only attempt index, the registry
tests/cwr/registry.kvx, the evidence schema that ships beside it and the case oracles.

Outcomes are recomputed, never copied. For each record the checker
  * validates the record against evidence.schema.json (every field required, nothing extra);
  * checks the record against the registry: case identity, tier, clauses and the exact expected
    assertion inventory for the record's scope (the full case, or the subset registered for a task);
  * re-hashes every log, ledger, final state and the whole attempt directory and refuses any mismatch;
  * anchors the record outside itself: it must be an entry of the hash-chained append-only attempt
    index tests/cwr/evidence/index.jsonl, whose head and length are pinned in the registry, with the
    record's own sha256; its name must carry its start time and no time may lie in the future;
  * validates every ledger against the kernel ledger schema (spec/cwr/kernel.md section 8) and
    recomputes Build hashes, Instance ids, seeds, K, arms and model identity from the ledger headers;
  * recomputes every registered assertion with the case's independent oracle, which reads only the
    ledgers, final state and attempt records (never the driver's ASSERT lines). An assertion with no
    oracle is UNVERIFIED and can never be PASS, whatever the driver printed. CWR-019 has a built-in
    oracle; the oracle of any other case is tests/cwr/oracles/CWR_NNN.py exposing recompute(ctx),
    vetted before loading (standard library only, no process, network, file or dynamic-code access);
  * recomputes the outcome (PASS, FAIL or BLOCKED) and refuses a record whose claimed outcome is not
    supported; in particular PASS with no assertions, PASS with a Build hash that is not the
    registered Build, PASS over a modified ledger, PASS with a seed that does not match the ledger
    header, PASS with an incomplete inventory and a campaign PASS without the complete arm matrix and
    pre-registration.

A task-scope (component) record never changes a case status: --status derives the status of a case
from accepted case-scope records only, in attempt-index order, so a component PASS leaves the parent
case UNRUN. A record missing from the index, or an indexed record that is missing, is reported.

Usage:
  check_evidence.py [--root DIR] RECORD.json [RECORD.json ...]   exit 0 iff every record is accepted
  check_evidence.py [--root DIR] --staged -                       check one record read from stdin
  check_evidence.py [--root DIR] --status CWR-NNN                 derive the case status
Exit codes: 0 accepted (or status derived from an intact index), 1 rejected (or the evidence store is
damaged), 2 usage or unreadable input.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
DEFAULT_ROOT = HERE.parents[1]
REGISTRY_REL = "tests/cwr/registry.kvx"
EVIDENCE_REL = "tests/cwr/evidence"
INDEX_REL = "tests/cwr/evidence/index.jsonl"
ORACLES_REL = "tests/cwr/oracles"
SCHEMA_PATH = HERE / "evidence.schema.json"
PROGRAM = "tests/cwr/check_evidence.py"
CASE_RE = re.compile(r"CWR-\d{3}")
ASSERT_LINE = re.compile(r"^ASSERT (\S+) (PASS|FAIL)(?: (.*))?$")
BLOCKED_LINE = re.compile(r"^BLOCKED (\S.*)$")
RECORD_NAME = re.compile(r"^(\d{8}T\d{6}Z)-([1-9]\d*)\.json$")
SHA = re.compile(r"^sha256:[0-9a-f]{64}$")
GENESIS = "sha256:" + "0" * 64
CLOCK_SKEW = timedelta(minutes=5)
INDEX_FIELDS = {
    "seq",
    "prev",
    "record",
    "record_sha256",
    "case",
    "scope",
    "task",
    "outcome",
    "accepted",
    "appended_at",
}
ENVELOPE = {
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
}
LEDGER_KINDS = {
    "header",
    "primitive",
    "adapter",
    "turn",
    "drift",
    "proposal",
    "upgrade",
    "recovery",
}
SIDE_EFFECTS = {"read_only", "mutating", "external"}
SUITE_NAME = "cwr019-suite.json"
# Modules and calls an oracle (and this checker) may never use: no process, network, foreign code.
BANNED_MODULES = {
    "subprocess",
    "socket",
    "ctypes",
    "multiprocessing",
    "http",
    "urllib",
    "runpy",
    "importlib",
    "asyncio",
    "ssl",
    "ftplib",
    "smtplib",
    "telnetlib",
    "xmlrpc",
    "socketserver",
    "pty",
    "shutil",
}
BANNED_CALLS = {"exec", "eval", "compile", "__import__", "open", "breakpoint", "input"}
PROCESS_ATTRS = re.compile(
    r"^(system|popen|fork|forkpty|kill|killpg|exec[lv]p?e?|spawn[lv]p?e?|posix_spawnp?)$"
)
BANNED_ATTRS = re.compile(
    r"^(remove|unlink|rmdir|rename|replace|write_text|write_bytes|mkdir|chmod|open|"
    r"read_text|read_bytes|exec_module|load_module)$"
)


def checker_revision() -> str:
    return "sha256:" + hashlib.sha256(Path(__file__).resolve().read_bytes()).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def strict_json(text):
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


def parse_utc(text) -> datetime | None:
    if not isinstance(text, str):
        return None
    match = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?Z", text)
    if not match:
        return None
    try:
        moment = datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return None
    fraction = (match.group(2) or "0")[:6].ljust(6, "0")
    return moment + timedelta(microseconds=int(fraction))


def index_line(entry: dict) -> str:
    return json.dumps(entry, sort_keys=True, separators=(",", ":"))


def index_hash(line: str) -> str:
    return sha256_bytes(line.encode())


# ------------------------------------------------------------------ evidence stores


class FsStore:
    """The repository on disk. Paths are repository-relative POSIX strings."""

    memory = False

    def __init__(self, root) -> None:
        self.root = Path(root).resolve()

    def is_file(self, rel: str) -> bool:
        return (self.root / rel).is_file()

    def read(self, rel: str) -> bytes:
        return (self.root / rel).read_bytes()

    def sha(self, rel: str) -> str:
        digest = hashlib.sha256()
        with open(self.root / rel, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return "sha256:" + digest.hexdigest()

    def files_under(self, prefix: str) -> list[str]:
        base = self.root / prefix
        if not base.is_dir():
            return []
        return sorted(p.relative_to(self.root).as_posix() for p in base.rglob("*") if p.is_file())


class MemStore:
    """An in-memory repository (a trap suite carried as final state)."""

    memory = True

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = dict(files)

    def copy(self) -> MemStore:
        return MemStore(self.files)

    def is_file(self, rel: str) -> bool:
        return rel in self.files

    def read(self, rel: str) -> bytes:
        if rel not in self.files:
            raise FileNotFoundError(rel)
        return self.files[rel]

    def sha(self, rel: str) -> str:
        return sha256_bytes(self.read(rel))

    def files_under(self, prefix: str) -> list[str]:
        return sorted(p for p in self.files if p.startswith(prefix.rstrip("/") + "/"))


def attempt_digest(store, prefix: str) -> str:
    """Digest of every file in an attempt directory: one 'relative-path sha256' line per file."""
    lines = "".join(
        f"{rel[len(prefix) + 1 :]} {store.sha(rel)}\n" for rel in store.files_under(prefix)
    )
    return sha256_bytes(lines.encode())


# ------------------------------------------------------------------ registry (independent reader)


def parse_kvx_text(text: str, label: str) -> dict:
    """Strict KVX reader: [section] headers and key = <one JSON value> lines."""
    sections: dict = {}
    current = None
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        header = re.fullmatch(r"\[([A-Za-z0-9_.-]+)\]", line)
        if header:
            if header.group(1) in sections:
                raise ValueError(f"{label}:{number}: duplicate section [{header.group(1)}]")
            current = sections[header.group(1)] = {}
            continue
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_-]*)\s*=\s*(.+)", line)
        if not match or current is None:
            raise ValueError(f"{label}:{number}: unsupported syntax")
        if match.group(1) in current:
            raise ValueError(f"{label}:{number}: duplicate key {match.group(1)}")
        current[match.group(1)] = strict_json(match.group(2))
    return sections


def read_registry(path: Path) -> dict:
    return parse_kvx_text(Path(path).read_text(), str(path))


def inventory(case: dict) -> list[str]:
    keys = sorted((k for k in case if re.fullmatch(r"a[1-9]\d*", k)), key=lambda k: int(k[1:]))
    if [int(k[1:]) for k in keys] != list(range(1, len(keys) + 1)):
        return []
    return keys


# ------------------------------------------------------------------ append-only attempt index


def load_index(store, meta: dict, now: datetime | None = None) -> tuple[list[dict], list[str]]:
    """Read and verify the hash-chained attempt index against the head pinned in the registry."""
    now = now or datetime.now(UTC)
    entries: list[dict] = []
    errors: list[str] = []
    data = store.read(INDEX_REL) if store.is_file(INDEX_REL) else b""
    if data and not data.endswith(b"\n"):
        errors.append("the final index entry is truncated")
    prev, last_time, seen = GENESIS, None, set()
    for number, line in enumerate(data.decode("utf-8", errors="replace").splitlines()):
        try:
            entry = strict_json(line)
        except ValueError as error:
            errors.append(f"entry {number} is not JSON: {error}")
            break
        if not isinstance(entry, dict) or set(entry) != INDEX_FIELDS:
            errors.append(f"entry {number} must carry exactly {sorted(INDEX_FIELDS)}")
            break
        if line != index_line(entry):
            errors.append(f"entry {number} is not in canonical form")
        if entry["seq"] != number:
            errors.append(f"entry {number} has seq {entry['seq']} (gap, duplicate or reordering)")
        if entry["prev"] != prev:
            errors.append(
                f"entry {number} does not chain to its predecessor (prev {entry['prev']} != {prev})"
            )
        moment = parse_utc(entry["appended_at"])
        if moment is None:
            errors.append(f"entry {number} has an invalid appended_at")
        else:
            if last_time is not None and moment < last_time:
                errors.append(
                    f"entry {number} was appended at {entry['appended_at']}, before its predecessor"
                )
            if moment > now + CLOCK_SKEW:
                errors.append(
                    f"entry {number} was appended at {entry['appended_at']}, in the future"
                )
            last_time = moment
        if entry["record"] in seen:
            errors.append(f"entry {number} indexes {entry['record']} a second time")
        seen.add(entry["record"])
        entries.append(entry)
        prev = index_hash(line)
    head = prev if entries else None
    if meta.get("index_length") != len(entries) or meta.get("index_head") != head:
        errors.append(
            f"index head {head} and length {len(entries)} do not match the registry's pinned "
            f"index_head {meta.get('index_head')} and index_length {meta.get('index_length')} "
            "(an entry was removed, added or rewritten)"
        )
    return entries, errors


# ------------------------------------------------------------------ minimal JSON Schema subset


class Schema:
    """The subset of JSON Schema 2020-12 used by evidence.schema.json."""

    def __init__(self, document: dict) -> None:
        self.document = document

    def resolve(self, ref: str) -> dict:
        if not ref.startswith("#/"):
            raise ValueError(f"unsupported $ref {ref}")
        node = self.document
        for part in ref[2:].split("/"):
            node = node[part]
        return node

    @staticmethod
    def type_ok(value, name: str) -> bool:
        return {
            "object": lambda v: isinstance(v, dict),
            "array": lambda v: isinstance(v, list),
            "string": lambda v: isinstance(v, str),
            "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
            "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
            "boolean": lambda v: isinstance(v, bool),
            "null": lambda v: v is None,
        }[name](value)

    def errors(self, value, schema: dict | None = None, where: str = "$") -> list[str]:
        schema = self.document if schema is None else schema
        out: list[str] = []
        if "$ref" in schema:
            out += self.errors(value, self.resolve(schema["$ref"]), where)
        if "anyOf" in schema and not any(
            not self.errors(value, option, where) for option in schema["anyOf"]
        ):
            out.append(f"{where}: matches none of the allowed forms")
        if "const" in schema and (
            value != schema["const"] or type(value) is not type(schema["const"])
        ):
            out.append(f"{where}: must be {schema['const']!r}")
        if "enum" in schema and not any(
            value == v and type(value) is type(v) for v in schema["enum"]
        ):
            out.append(f"{where}: must be one of {schema['enum']}")
        if "type" in schema:
            names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
            if not any(self.type_ok(value, name) for name in names):
                out.append(f"{where}: must be of type {'/'.join(names)}")
                return out
        if isinstance(value, str):
            if "pattern" in schema and not re.search(schema["pattern"], value):
                out.append(f"{where}: {value!r} does not match {schema['pattern']}")
            if len(value) < schema.get("minLength", 0):
                out.append(f"{where}: shorter than {schema['minLength']}")
        if self.type_ok(value, "number") and "minimum" in schema and value < schema["minimum"]:
            out.append(f"{where}: below minimum {schema['minimum']}")
        if isinstance(value, dict):
            for key in schema.get("required", []):
                if key not in value:
                    out.append(f"{where}: missing required field {key}")
            props = schema.get("properties", {})
            for key, item in value.items():
                if key in props:
                    out += self.errors(item, props[key], f"{where}.{key}")
                elif schema.get("additionalProperties") is False:
                    out.append(f"{where}: unexpected field {key}")
        if isinstance(value, list):
            if len(value) < schema.get("minItems", 0):
                out.append(f"{where}: fewer than {schema['minItems']} items")
            if "items" in schema:
                for index, item in enumerate(value):
                    out += self.errors(item, schema["items"], f"{where}[{index}]")
        return out


_SCHEMA: list = []


def default_schema() -> Schema:
    if not _SCHEMA:
        _SCHEMA.append(Schema(strict_json(SCHEMA_PATH.read_text())))
    return _SCHEMA[0]


# ------------------------------------------------------------------ ledgers


def read_ledger(data: bytes) -> tuple[list[dict], str | None]:
    if not data:
        return [], "ledger is empty"
    if not data.endswith(b"\n"):
        return [], "ledger final record is truncated (no terminating newline)"
    records = []
    for number, line in enumerate(data.decode("utf-8").splitlines(), 1):
        try:
            value = strict_json(line)
        except ValueError as error:
            return [], f"ledger line {number} is not one JSON object: {error}"
        if not isinstance(value, dict):
            return [], f"ledger line {number} is not a JSON object"
        records.append(value)
    return records, None


def ledger_schema_problems(ledger: list[dict]) -> list[str]:
    """The ledger record schema of spec/cwr/kernel.md section 8, as far as this checker relies on it."""
    problems: list[str] = []
    last_time = None
    for position, entry in enumerate(ledger):
        where = f"ledger record {position}"
        for key, ok in (
            ("seq", type(entry.get("seq")) is int),
            ("instance", isinstance(entry.get("instance"), str)),
            (
                "build",
                isinstance(entry.get("build"), str) and bool(SHA.match(entry.get("build") or "")),
            ),
            ("turn", type(entry.get("turn")) is int and entry.get("turn") >= 0),
            ("world_time", type(entry.get("world_time")) is int and entry.get("world_time") >= 0),
            ("kind", entry.get("kind") in LEDGER_KINDS),
        ):
            if not ok:
                problems.append(f"{where}: missing or invalid {key}")
        if type(entry.get("world_time")) is int:
            if last_time is not None and entry["world_time"] < last_time:
                problems.append(f"{where}: world_time {entry['world_time']} goes backwards")
            last_time = entry["world_time"]
        if entry.get("kind") in ("primitive", "adapter"):
            outcome = entry.get("outcome")
            if not isinstance(entry.get("primitive"), str) or not entry.get("primitive"):
                problems.append(f"{where}: {entry.get('kind')} record names no primitive")
            if not isinstance(outcome, dict) or outcome.get("status") not in ("ok", "error"):
                problems.append(f"{where}: outcome must be an object with status ok or error")
            elif outcome["status"] == "ok" and outcome.get("code") is not None:
                problems.append(f"{where}: an ok outcome carries code {outcome.get('code')!r}")
            elif outcome["status"] == "error" and outcome.get("code") not in ENVELOPE:
                problems.append(
                    f"{where}: an error outcome carries {outcome.get('code')!r}, not an envelope code"
                )
            elif outcome.get("side_effect") not in SIDE_EFFECTS:
                problems.append(
                    f"{where}: outcome side_effect {outcome.get('side_effect')!r} is not a contract class"
                )
        if problems:
            break
    return problems


# ------------------------------------------------------------------ independent oracles


@dataclass
class OracleContext:
    """What an oracle may read: the record, its verified ledgers and final state, nothing else."""

    case: str
    scope: str
    record: dict
    expected: list[str]
    ledgers: dict  # instance id -> list of ledger records, or None when unusable
    final_states: dict  # repository path -> bytes, for every verified final state
    instances: list
    store: object = None


def fixture_oracle(ctx: OracleContext) -> dict:
    """The oracle of fixture cases inside a CWR-019 trap suite, recomputed from ledgers and final state:
    every assertion holds iff the attempt has an Instance, every Instance ledger is usable and holds at
    least one primitive record, every primitive outcome is ok and every Instance has a final state."""
    problems = []
    if not ctx.instances:
        problems.append("the attempt has no Instance")
    for inst in ctx.instances:
        ledger = ctx.ledgers.get(inst["id"])
        if ledger is None:
            problems.append(f"instance {inst['id']}: no usable ledger")
            continue
        prims = [r for r in ledger if r.get("kind") in ("primitive", "adapter")]
        if not prims:
            problems.append(f"instance {inst['id']}: ledger holds no primitive record")
        bad = [r.get("seq") for r in prims if (r.get("outcome") or {}).get("status") != "ok"]
        if bad:
            problems.append(f"instance {inst['id']}: primitive records {bad} are not ok")
        final = inst.get("final_state")
        if final is None or final["path"] not in ctx.final_states:
            problems.append(f"instance {inst['id']}: no verified final state")
        else:
            try:
                if not isinstance(strict_json(ctx.final_states[final["path"]].decode()), dict):
                    problems.append(f"instance {inst['id']}: final state is not an object")
            except (ValueError, UnicodeDecodeError):
                problems.append(f"instance {inst['id']}: final state is not JSON")
    verdict = (
        ("FAIL", "; ".join(problems))
        if problems
        else ("PASS", "ledger primitives ok and final state present")
    )
    return {ident: verdict for ident in ctx.expected}


def vet_source(
    text: str, allowed_modules: frozenset = frozenset(), allow_io: bool = False
) -> list[str]:
    """Static separation audit of an oracle (or of this checker): standard library only, no process,
    network or dynamic code; an oracle additionally has no file access of its own."""
    try:
        tree = ast.parse(text)
    except SyntaxError as error:
        return [f"does not parse: {error}"]
    problems = []
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""] if node.level == 0 else ["<relative import>"]
        for name in names:
            top = name.split(".")[0]
            if top == "__future__" or top in allowed_modules:
                continue
            if top not in sys.stdlib_module_names:
                problems.append(f"imports non-standard module {name}")
            elif top in BANNED_MODULES:
                problems.append(f"imports {name} (process, network or dynamic-code module)")
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in BANNED_CALLS - (
                {"open"} if allow_io else set()
            ):
                problems.append(f"calls {func.id}()")
            if isinstance(func, ast.Attribute) and PROCESS_ATTRS.match(func.attr):
                problems.append(f"calls .{func.attr}()")
            elif not allow_io and isinstance(func, ast.Attribute) and BANNED_ATTRS.match(func.attr):
                problems.append(f"calls .{func.attr}() (oracles read only through their context)")
    return sorted(set(problems))


_MODULES: dict = {}


def module_oracle(store, case_id: str):
    """Load tests/cwr/oracles/CWR_NNN.py after vetting it; returns (recompute, source, problem)."""
    rel = f"{ORACLES_REL}/{case_id.replace('-', '_')}.py"
    if store.memory or not store.is_file(rel):
        return None, rel, None
    data = store.read(rel)
    key = (str(store.root / rel), sha256_bytes(data))
    if key not in _MODULES:
        problems = vet_source(data.decode("utf-8", errors="replace"))
        if problems:
            _MODULES[key] = (None, f"oracle {rel} is refused: " + "; ".join(problems))
        else:
            try:
                sys.dont_write_bytecode = True  # loading an oracle writes nothing
                spec = importlib.util.spec_from_file_location(
                    f"cwr_oracle_{case_id.replace('-', '_')}_{key[1][7:19]}", store.root / rel
                )
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                func = getattr(module, "recompute", None)
                _MODULES[key] = (
                    func,
                    None if callable(func) else f"oracle {rel} defines no recompute(ctx)",
                )
            except Exception as error:  # an oracle that cannot load verifies nothing
                _MODULES[key] = (None, f"oracle {rel} cannot be loaded: {error}")
    func, problem = _MODULES[key]
    return func, rel, problem


def resolve_oracle(store, case_id: str, suite: bool):
    if suite:
        return fixture_oracle, "built-in fixture oracle (trap suite)", None
    if case_id in BUILTIN_ORACLES:
        return BUILTIN_ORACLES[case_id], f"built-in oracle for {case_id}", None
    return module_oracle(store, case_id)


# ------------------------------------------------------------------ verdict


@dataclass
class Verdict:
    path: str
    accepted: bool = False
    claimed: str | None = None
    recomputed: str | None = None
    errors: list[str] = field(default_factory=list)  # reasons for rejection
    shortfalls: list[str] = field(default_factory=list)  # reasons the evidence is not a PASS
    assertions: list[dict] = field(default_factory=list)  # the checker's own recomputation
    record: dict | None = None

    def as_dict(self) -> dict:
        return {
            "record": self.path,
            "accepted": self.accepted,
            "claimed": self.claimed,
            "recomputed": self.recomputed,
            "errors": self.errors,
            "shortfalls": self.shortfalls,
            "recomputed_assertions": self.assertions,
        }


def safe_relative(rel: str, attempt_prefix: str) -> str | None:
    pure = PurePosixPath(rel)
    if pure.is_absolute() or ".." in pure.parts:
        return f"path escapes the repository: {rel}"
    if not rel.startswith(attempt_prefix + "/"):
        return f"path {rel} is outside the record's own attempt directory {attempt_prefix}/"
    return None


def check_record(
    record,
    root=DEFAULT_ROOT,
    *,
    record_path=None,
    staged: bool = False,
    registry: dict | None = None,
    schema: Schema | None = None,
    store=None,
    suite: bool = False,
    now: datetime | None = None,
) -> Verdict:
    store = store if store is not None else FsStore(root)
    now = now or datetime.now(UTC)
    record_rel = None
    if record_path is not None:
        if store.memory:
            record_rel = str(record_path)
        else:
            try:
                record_rel = Path(record_path).resolve().relative_to(store.root).as_posix()
            except ValueError:
                record_rel = str(record_path)
    verdict = Verdict(path=str(record_path) if record_path is not None else "<staged>")
    errors, short = verdict.errors, verdict.shortfalls
    schema = schema or default_schema()
    if not isinstance(record, dict):
        errors.append("record is not a JSON object")
        return verdict
    verdict.record = record
    verdict.claimed = record.get("outcome")
    schema_errors = schema.errors(record)
    if schema_errors:
        errors += ["schema: " + e for e in schema_errors]
        return verdict

    case_id, scope, task = record["case"], record["scope"], record["task"]
    attempt_prefix = f"{EVIDENCE_REL}/{record['record_id']}"
    if record["record_id"].split("/")[0] != case_id:
        errors.append(f"record_id {record['record_id']} does not belong to case {case_id}")
    if record_rel is not None and not staged:
        if record_rel != f"{attempt_prefix}.json":
            errors.append(f"record file must live at {attempt_prefix}.json, found {record_rel}")
        if record["checker"]["accepted"] is not True:
            errors.append("the record states that the checker rejected it when it was written")

    # -------------------------------------------------------------- time: name, order, no future
    stamp = record["record_id"].split("/")[1].rsplit("-", 1)[0]
    started, ended, written = (
        parse_utc(record[k]) for k in ("started_at", "ended_at", "timestamp")
    )
    if started is None or ended is None or written is None:
        errors.append("started_at, ended_at and timestamp must be valid UTC times")
    else:
        if started.strftime("%Y%m%dT%H%M%SZ") != stamp:
            errors.append(
                f"record name stamp {stamp} is not the attempt's start time {record['started_at']}"
            )
        if not started <= ended <= written:
            errors.append("times must satisfy started_at <= ended_at <= timestamp")
        if written > now + CLOCK_SKEW:
            errors.append(f"timestamp {record['timestamp']} lies in the future")

    # -------------------------------------------------------------- registry
    try:
        if registry is None:
            registry = parse_kvx_text(store.read(REGISTRY_REL).decode(), REGISTRY_REL)
    except (OSError, ValueError, UnicodeDecodeError) as error:
        errors.append(f"registry unreadable: {error}")
        return verdict
    meta = registry.get("meta", {})
    case = registry.get(f"case.{case_id}")
    if case is None:
        errors.append(f"case {case_id} is not registered")
        return verdict

    # -------------------------------------------------------------- external anchor: the attempt index
    if not staged:
        entries, index_errors = load_index(store, meta, now)
        errors += ["attempt index: " + e for e in index_errors]
        entry = next((e for e in entries if e["record"] == record_rel), None)
        if entry is None:
            errors.append(
                f"record {record_rel} is not in the append-only attempt index {INDEX_REL}; "
                "a record the runner did not append is never evidence"
            )
        else:
            actual = store.sha(record_rel)
            if actual != entry["record_sha256"]:
                errors.append(
                    f"record {record_rel} does not match the attempt index: sha256 {actual} != indexed "
                    f"{entry['record_sha256']} (modified after it was written)"
                )
            for key in ("case", "scope", "task", "outcome"):
                if entry[key] != record[key]:
                    errors.append(f"indexed {key} {entry[key]!r} != record {key} {record[key]!r}")
            if entry["accepted"] is not True:
                errors.append("the attempt index records that the checker rejected this record")
            appended = parse_utc(entry["appended_at"])
            if written is not None and appended is not None and written > appended:
                errors.append(
                    f"timestamp {record['timestamp']} is later than the index append {entry['appended_at']}"
                )

    if record["registry_version"] != meta.get("registry_version"):
        errors.append(
            f"registry_version {record['registry_version']} != registered {meta.get('registry_version')}"
        )
    if record["schema_version"] != meta.get("schema_version"):
        errors.append(
            f"schema_version {record['schema_version']} != registered {meta.get('schema_version')}"
        )
    if record["tier"] != case.get("tier"):
        errors.append(f"tier {record['tier']} != registered tier {case.get('tier')}")
    keys = inventory(case)
    full = [f"{case_id}.{k}" for k in keys]
    if not keys:
        short.append(
            f"case {case_id} has no registered assertion inventory and cannot be marked PASS"
        )
    subsets = case.get("subsets") if isinstance(case.get("subsets"), dict) else {}
    if scope == "case":
        expected_ids = full
        if task is not None:
            errors.append("a case-scope record must have task null")
        if sorted(record["clauses"]) != sorted(case.get("reqs", [])):
            errors.append(
                f"clauses {record['clauses']} != registered case clauses {case.get('reqs')}"
            )
    else:
        if task is None or task not in case.get("tasks", []):
            errors.append(
                f"task-scope record names task {task!r}, not a registered task of {case_id}"
            )
            expected_ids = []
        elif task not in subsets or not subsets[task]:
            short.append(f"task {task} has no registered subset of {case_id}")
            expected_ids = []
        else:
            expected_ids = [f"{case_id}.{k}" for k in subsets[task] if k in keys]
        if not set(record["clauses"]) <= set(case.get("reqs", [])):
            errors.append(f"clauses {record['clauses']} are not clauses of {case_id}")
    if record["assertion_ids"] != expected_ids:
        errors.append(
            f"assertion_ids {record['assertion_ids']} != registered inventory for this scope {expected_ids}"
        )

    # -------------------------------------------------------------- attempt directory
    if (
        store.files_under(attempt_prefix)
        and attempt_digest(store, attempt_prefix) != record["attempt_sha256"]
    ):
        errors.append(
            f"attempt directory {attempt_prefix}/ does not match attempt_sha256 "
            "(a file was added, removed or modified)"
        )
    elif not store.files_under(attempt_prefix):
        errors.append(f"attempt directory {attempt_prefix}/ is missing")

    # -------------------------------------------------------------- logs and observed assertions
    recomputed_observed: list[dict] = []
    blocked_lines: list[str] = []
    for index, inv in enumerate(record["invocations"]):
        for kind in ("stdout", "stderr"):
            rel, digest = inv[f"{kind}_log"], inv[f"{kind}_sha256"]
            problem = safe_relative(rel, attempt_prefix)
            if problem:
                errors.append(f"invocation {index} {kind}: {problem}")
                continue
            if not store.is_file(rel):
                errors.append(f"invocation {index} {kind} log missing: {rel}")
                continue
            actual = store.sha(rel)
            if actual != digest:
                errors.append(
                    f"invocation {index} {kind} log modified: {rel} sha256 {actual} != recorded {digest}"
                )
                continue
            if kind == "stdout":
                for line in store.read(rel).decode("utf-8", errors="replace").splitlines():
                    match = ASSERT_LINE.match(line)
                    if match and match.group(1).startswith(case_id + "."):
                        recomputed_observed.append(
                            {
                                "id": match.group(1),
                                "outcome": match.group(2),
                                "diagnostic": match.group(3) or "",
                            }
                        )
                    blocked = BLOCKED_LINE.match(line)
                    if blocked:
                        blocked_lines.append(blocked.group(1))
        if inv["timed_out"]:
            short.append(f"invocation {index} (task {inv['task']}) timed out")
        if inv["exit_code"] != 0:
            short.append(f"invocation {index} (task {inv['task']}) exited {inv['exit_code']}")
    if record["invocations"]:
        first = record["invocations"][0]
        if record["log"] != first["stdout_log"] or record["log_sha256"] != first["stdout_sha256"]:
            errors.append("log/log_sha256 must name the first invocation's stdout log")
        codes = [inv["exit_code"] for inv in record["invocations"]]
        expected_exit = next((c if c is not None else -1 for c in codes if c != 0), 0)
        if record["exit_code"] != expected_exit:
            errors.append(
                f"exit_code {record['exit_code']} != recomputed {expected_exit} from invocations"
            )
        tasks_run = [inv["task"] for inv in record["invocations"]]
        wanted = [task] if scope == "task" else list(case.get("tasks", []))
        if sorted(tasks_run) != sorted(wanted):
            errors.append(f"invocations ran tasks {tasks_run}, scope requires {wanted}")
    if [(o["id"], o["outcome"]) for o in record["observed"]] != [
        (o["id"], o["outcome"]) for o in recomputed_observed
    ]:
        errors.append(
            "observed assertions in the record differ from those recomputed from the driver logs: "
            f"record={[(o['id'], o['outcome']) for o in record['observed']]} "
            f"logs={[(o['id'], o['outcome']) for o in recomputed_observed]}"
        )

    observed_ids = [o["id"] for o in recomputed_observed]
    if not recomputed_observed:
        short.append("no assertions were observed")
    # Registered task subsets may overlap, so one assertion can be reported by several drivers of a
    # case; it holds only if it was observed at least once and every observation of it is PASS.
    for ident in expected_ids:
        if ident not in observed_ids:
            short.append(f"registered assertion {ident} was not observed")
    for obs in recomputed_observed:
        if obs["outcome"] != "PASS" and obs["id"] in expected_ids:
            short.append(f"assertion {obs['id']} FAIL: {obs['diagnostic']}")
    unexpected = sorted({i for i in observed_ids if i not in expected_ids})
    if unexpected:
        short.append(f"assertions outside the registered inventory for this scope: {unexpected}")
    gaps = [i for i in expected_ids if i not in observed_ids]
    if record["coverage_gaps"] != gaps:
        errors.append(f"coverage_gaps {record['coverage_gaps']} != recomputed {gaps}")
    if record["unexpected_assertions"] != unexpected:
        errors.append(
            f"unexpected_assertions {record['unexpected_assertions']} != recomputed {unexpected}"
        )

    # -------------------------------------------------------------- artifacts: ledgers and final state
    artifact_ok: dict[str, bool] = {}
    final_bytes: dict[str, bytes] = {}
    for group in ("ledgers", "final_states"):
        for item in record[group]:
            problem = safe_relative(item["path"], attempt_prefix)
            if problem:
                errors.append(f"{group}: {problem}")
                artifact_ok[item["path"]] = False
                continue
            if not store.is_file(item["path"]):
                errors.append(f"{group}: {item['path']} is missing")
                artifact_ok[item["path"]] = False
                continue
            actual = store.sha(item["path"])
            if actual != item["sha256"]:
                label = "modified ledger" if group == "ledgers" else "modified final state"
                errors.append(
                    f"{label}: {item['path']} sha256 {actual} != recorded {item['sha256']}"
                )
                artifact_ok[item["path"]] = False
            else:
                artifact_ok[item["path"]] = True
                if group == "final_states":
                    final_bytes[item["path"]] = store.read(item["path"])
    if record["ledgers"]:
        lines = "".join(
            f"{i['path']} {i['sha256']}\n"
            for i in sorted(record["ledgers"], key=lambda i: i["path"])
        )
        expected_digest = sha256_bytes(lines.encode())
    else:
        expected_digest = None
    if record["ledger_digest"] != expected_digest:
        errors.append(f"ledger_digest {record['ledger_digest']} != recomputed {expected_digest}")

    ledger_paths = {i["path"]: i["sha256"] for i in record["ledgers"]}
    state_paths = {i["path"]: i["sha256"] for i in record["final_states"]}
    instance_ids = [inst["id"] for inst in record["instances"]]
    if len(instance_ids) != len(set(instance_ids)):
        errors.append("duplicate instance ids")
    if sorted(ledger_paths) != sorted(inst["ledger"]["path"] for inst in record["instances"]):
        errors.append("ledgers must list exactly the instances' ledgers")
    builds = set(record["builds"])
    if record["build_hash"] is not None and record["build_hash"] not in builds:
        errors.append("build_hash must be one of builds")
    model_identity = record["model"]["identity"] if record["model"] else None
    parsed_ledgers: dict = {}
    for inst in record["instances"]:
        label = f"instance {inst['id']}"
        parsed_ledgers[inst["id"]] = None
        if ledger_paths.get(inst["ledger"]["path"]) != inst["ledger"]["sha256"]:
            errors.append(f"{label}: ledger entry disagrees with ledgers list")
        if (
            inst["final_state"] is not None
            and state_paths.get(inst["final_state"]["path"]) != inst["final_state"]["sha256"]
        ):
            errors.append(f"{label}: final_state entry disagrees with final_states list")
        if inst["final_state"] is None:
            short.append(f"{label}: no final state recorded")
        if inst["build"] not in builds:
            errors.append(f"{label}: build {inst['build']} is not listed in builds")
        if record["seed"] is not None and inst["seed"] != record["seed"]:
            short.append(
                f"seed mismatch: record seed {record['seed']} != {label} seed {inst['seed']}"
            )
        if not artifact_ok.get(inst["ledger"]["path"]):
            continue
        ledger, problem = read_ledger(store.read(inst["ledger"]["path"]))
        if problem:
            short.append(f"{label}: {problem}")
            continue
        header = ledger[0]
        if header.get("kind") != "header" or header.get("seq") != 0:
            short.append(f"{label}: ledger does not start with a header record at seq 0")
            continue
        if header.get("seed") != inst["seed"]:
            short.append(
                f"seed mismatch: {label} recorded seed {inst['seed']} but the ledger header seed is {header.get('seed')}"
            )
        if record["seed"] is not None and header.get("seed") != record["seed"]:
            short.append(
                f"seed mismatch: record seed {record['seed']} but {label} ledger header seed is {header.get('seed')}"
            )
        if header.get("tail_size") != inst["tail_size"]:
            short.append(
                f"{label}: K {inst['tail_size']} != ledger header tail_size {header.get('tail_size')}"
            )
        if inst["arm"] is not None and header.get("arm") != inst["arm"]:
            short.append(f"{label}: arm {inst['arm']} != ledger header arm {header.get('arm')}")
        if model_identity is not None and header.get("model") != model_identity:
            short.append(
                f"{label}: model {model_identity} != ledger header model {header.get('model')}"
            )
        if header.get("build") != inst["build"]:
            short.append(
                f"{label}: build {inst['build']} != ledger header build {header.get('build')}"
            )
        intact = True
        current = header.get("build")
        for position, entry in enumerate(ledger):
            if entry.get("seq") != position:
                short.append(
                    f"{label}: ledger seq {entry.get('seq')} at position {position} (gap or duplicate)"
                )
                intact = False
                break
            if entry.get("instance") != inst["id"]:
                short.append(
                    f"{label}: ledger record {position} belongs to instance {entry.get('instance')}"
                )
                intact = False
                break
            if entry.get("kind") == "upgrade":
                current = entry.get("build")
            elif entry.get("build") != current:
                short.append(
                    f"{label}: ledger record {position} changes Build without an upgrade record"
                )
                intact = False
                break
            if entry.get("build") not in builds:
                short.append(
                    f"{label}: ledger uses Build {entry.get('build')} not listed in the record"
                )
                intact = False
                break
            if entry.get("experimental") not in (None, *record["experimental_flags"]):
                short.append(
                    f"{label}: ledger record {position} carries undeclared experimental flag {entry.get('experimental')}"
                )
                intact = False
                break
        schema_problems = ledger_schema_problems(ledger)
        if schema_problems:
            short.append(
                f"{label}: ledger violates the kernel ledger schema: " + "; ".join(schema_problems)
            )
            intact = False
        if intact:
            parsed_ledgers[inst["id"]] = ledger

    # -------------------------------------------------------------- registered inputs
    registered_builds = case.get("registered_builds") or []
    used = set(record["builds"]) | ({record["build_hash"]} if record["build_hash"] else set())
    if case.get("requires_build") is True and record["build_hash"] is None:
        short.append(f"case {case_id} requires a registered Build and the record names none")
    for build in sorted(used):
        if build not in registered_builds:
            short.append(
                f"Build hash {build} differs from the registered Build(s) {registered_builds or 'none registered'}"
            )
    registered_seeds = case.get("registered_seeds") or []
    if registered_seeds:
        for inst in record["instances"]:
            if inst["seed"] not in registered_seeds:
                short.append(
                    f"seed {inst['seed']} of instance {inst['id']} is not a registered seed"
                )
        if record["seed"] is not None and record["seed"] not in registered_seeds:
            short.append(f"seed {record['seed']} is not a registered seed")
    if (
        case.get("cassette_digest") is not None
        and record["cassette_digest"] != case["cassette_digest"]
    ):
        short.append(
            f"cassette digest {record['cassette_digest']} != registered {case['cassette_digest']}"
        )

    # -------------------------------------------------------------- campaigns: complete arm matrix
    if case.get("tier") == "campaign" and scope == "case":
        matrix = case.get("matrix")
        prereg = case.get("preregistration_digest")
        if not isinstance(matrix, dict) or not matrix:
            short.append("campaign case has no registered arm matrix")
        else:
            for hypothesis, spec in sorted(matrix.items()):
                seeds_by_arm = {}
                for arm in spec.get("arms", []):
                    cells: dict[str, set] = {}
                    for inst in record["instances"]:
                        if (
                            inst["hypothesis"] == hypothesis
                            and inst["arm"] == arm
                            and inst["task"] is not None
                        ):
                            cells.setdefault(inst["task"], set()).add(inst["seed"])
                    complete = len(cells) == spec.get("tasks") and all(
                        len(s) == spec.get("seeds") for s in cells.values()
                    )
                    if not complete:
                        short.append(
                            f"incomplete arm matrix: {hypothesis} arm {arm} has {len(cells)} of "
                            f"{spec.get('tasks')} tasks with {spec.get('seeds')} seeds each"
                        )
                    seeds_by_arm[arm] = {t: frozenset(s) for t, s in cells.items()}
                if len({frozenset(v.items()) for v in seeds_by_arm.values()}) > 1:
                    short.append(f"{hypothesis}: arms do not use the same tasks and seeds")
        if prereg is None:
            short.append(
                "no pre-registration digest is registered; a hypothesis result requires pre-registered metrics"
            )
        elif record["preregistration_digest"] != prereg:
            short.append(
                f"pre-registration digest {record['preregistration_digest']} != registered {prereg}"
            )

    # -------------------------------------------------------------- independent recomputation of every assertion
    oracle, source, oracle_problem = resolve_oracle(store, case_id, suite)
    results: dict = {}
    if oracle is not None and expected_ids:
        context = OracleContext(
            case=case_id,
            scope=scope,
            record=record,
            expected=list(expected_ids),
            ledgers=parsed_ledgers,
            final_states=final_bytes,
            instances=record["instances"],
            store=store,
        )
        try:
            results = oracle(context) or {}
            if not isinstance(results, dict):
                raise TypeError(
                    "an oracle must return a mapping of assertion id to (outcome, diagnostic)"
                )
        except Exception as error:  # an oracle that crashes verifies nothing
            results = {
                i: ("FAIL", f"oracle {source} raised {type(error).__name__}: {error}")
                for i in expected_ids
            }
    for ident in expected_ids:
        value = results.get(ident)
        if isinstance(value, (tuple, list)) and len(value) == 2 and value[0] in ("PASS", "FAIL"):
            outcome, diagnostic = value[0], str(value[1])
        elif value is not None:
            outcome, diagnostic = "FAIL", f"oracle {source} returned an invalid result {value!r}"
        else:
            outcome = "UNVERIFIED"
            diagnostic = oracle_problem or (
                f"no independent oracle ({source} does not exist)"
                if oracle is None
                else f"oracle {source} did not recompute it"
            )
        verdict.assertions.append({"id": ident, "outcome": outcome, "diagnostic": diagnostic})
        if outcome == "UNVERIFIED":
            short.append(
                f"assertion {ident} is UNVERIFIED: {diagnostic}; a driver observation alone never makes a PASS"
            )
        elif outcome == "FAIL":
            short.append(
                f"assertion {ident} recomputed FAIL by the independent oracle: {diagnostic}"
            )
    if [(a["id"], a["outcome"]) for a in record["recomputed"]] != [
        (a["id"], a["outcome"]) for a in verdict.assertions
    ]:
        errors.append(
            "recomputed assertions in the record differ from the checker's own recomputation: "
            f"record={[(a['id'], a['outcome']) for a in record['recomputed']]} "
            f"checker={[(a['id'], a['outcome']) for a in verdict.assertions]}"
        )

    # -------------------------------------------------------------- outcome
    if blocked_lines:
        recomputed = "BLOCKED"
    elif short:
        recomputed = "FAIL"
    else:
        recomputed = "PASS"
    verdict.recomputed = recomputed
    claimed = record["outcome"]
    if claimed == "BLOCKED":
        if not blocked_lines:
            errors.append("claims BLOCKED but no driver reported a missing resource")
        elif record["blocked_reason"] not in blocked_lines:
            errors.append(
                f"blocked_reason {record['blocked_reason']!r} is not what the driver reported {blocked_lines}"
            )
    elif record["blocked_reason"] is not None:
        errors.append("blocked_reason must be null unless the outcome is BLOCKED")
    if claimed == "PASS" and recomputed != "PASS":
        errors.append(
            "claims PASS but the evidence does not support it: "
            + "; ".join(short or ["driver reported BLOCKED"])
        )
    if claimed == "FAIL" and recomputed == "PASS" and not record["diagnostics"]:
        errors.append("claims FAIL with no diagnostic although every check passes")
    if claimed == "FAIL" and recomputed == "BLOCKED":
        errors.append("claims FAIL but the driver reported BLOCKED")
    verdict.accepted = not errors
    return verdict


def check_file(path, root=DEFAULT_ROOT, **kwargs) -> Verdict:
    try:
        record = strict_json(Path(path).read_text())
    except (OSError, ValueError, UnicodeDecodeError) as error:
        return Verdict(path=str(path), errors=[f"unreadable record: {error}"])
    return check_record(record, root, record_path=Path(path), **kwargs)


def check_rel(store, rel: str, **kwargs) -> Verdict:
    try:
        record = strict_json(store.read(rel).decode())
    except (OSError, ValueError, UnicodeDecodeError) as error:
        return Verdict(path=rel, errors=[f"unreadable record: {error}"])
    return check_record(
        record, store=store, record_path=rel if store.memory else (store.root / rel), **kwargs
    )


def derive_status(case_id: str, root=DEFAULT_ROOT, *, store=None, suite: bool = False) -> dict:
    """Case status from accepted case-scope records only, in attempt-index order; task-scope evidence
    never qualifies a case. Damage to the evidence store (a broken index, an indexed record that is
    missing, an indexed record accepted when written that is refused now) is reported in
    integrity_errors and leaves the status underived (null)."""
    store = store if store is not None else FsStore(root)
    integrity: list[str] = []
    try:
        registry = parse_kvx_text(store.read(REGISTRY_REL).decode(), REGISTRY_REL)
    except (OSError, ValueError, UnicodeDecodeError) as error:
        return {
            "case": case_id,
            "status": None,
            "evidence": None,
            "component_records": [],
            "rejected_records": [],
            "integrity_errors": [f"registry unreadable: {error}"],
        }
    entries, index_errors = load_index(store, registry.get("meta", {}))
    integrity += ["attempt index: " + e for e in index_errors]
    accepted_case, accepted_task, rejected = [], [], []
    indexed = set()
    for entry in entries:
        if entry["case"] != case_id:
            continue
        indexed.add(entry["record"])
        if not store.is_file(entry["record"]):
            integrity.append(f"indexed record {entry['record']} (seq {entry['seq']}) is missing")
            continue
        verdict = check_rel(store, entry["record"], suite=suite)
        if not verdict.accepted:
            rejected.append({"record": entry["record"], "errors": verdict.errors})
            if entry["accepted"] is True:
                # accepted when it was written, refused now: the record or its artifacts were changed, so
                # the status is not derived from whatever earlier attempt happens to remain intact
                integrity.append(
                    f"indexed record {entry['record']} (seq {entry['seq']}) was accepted when written "
                    f"and is refused now: {'; '.join(verdict.errors[:2])}"
                )
        elif verdict.record["scope"] == "case":
            accepted_case.append((entry["record"], verdict.record["outcome"]))
        else:
            accepted_task.append(
                {
                    "record": entry["record"],
                    "task": verdict.record["task"],
                    "outcome": verdict.record["outcome"],
                }
            )
    for rel in store.files_under(f"{EVIDENCE_REL}/{case_id}"):
        name = rel.rsplit("/", 1)[1]
        if rel.count("/") == 4 and name.endswith(".json") and rel not in indexed:
            rejected.append(
                {
                    "record": rel,
                    "errors": [
                        f"not in the append-only attempt index {INDEX_REL}; "
                        "a record the runner did not append is never evidence"
                    ],
                }
            )
    if integrity:
        status, evidence = None, None
    elif accepted_case:
        evidence, status = accepted_case[-1]
    else:
        status, evidence = "UNRUN", None
    return {
        "case": case_id,
        "status": status,
        "evidence": evidence,
        "component_records": accepted_task,
        "rejected_records": rejected,
        "integrity_errors": integrity,
    }


# ------------------------------------------------------------------ CWR-019: the checker's own oracle


def _set_meta_head(registry_text: str, head, length: int) -> str:
    out, inside = [], False
    for line in registry_text.splitlines(keepends=True):
        stripped = line.strip()
        if re.fullmatch(r"\[.+\]", stripped):
            inside = stripped == "[meta]"
        elif inside and re.match(r"index_head\s*=", stripped):
            line = f"index_head = {json.dumps(head)}\n"
        elif inside and re.match(r"index_length\s*=", stripped):
            line = f"index_length = {length}\n"
        out.append(line)
    return "".join(out)


def forge_in_memory(
    store: MemStore, record_rel: str, mutate=None, *, digests: bool = True, index: bool = True
) -> tuple[MemStore, dict]:
    """What the most careful forger would do to a copy of the suite: tamper with a record and its
    artifacts, then recompute every digest the record carries, rewrite its index entry, re-chain the
    index and re-pin the head in the registry. Only an independent recomputation can still object."""
    forged = store.copy()
    record = strict_json(forged.read(record_rel).decode())
    if mutate:
        mutate(record, forged)
    if digests:
        reseal_digests(record, forged)
    forged.files[record_rel] = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode()
    if index:
        reseal_index(forged, record_rel, record)
    return forged, record


def reseal_digests(record: dict, forged: MemStore) -> None:
    prefix = f"{EVIDENCE_REL}/{record.get('record_id')}"
    for inv in record.get("invocations", []):
        for kind in ("stdout", "stderr"):
            if forged.is_file(inv[f"{kind}_log"]):
                inv[f"{kind}_sha256"] = forged.sha(inv[f"{kind}_log"])
    if record.get("invocations"):
        record["log"], record["log_sha256"] = (
            record["invocations"][0]["stdout_log"],
            record["invocations"][0]["stdout_sha256"],
        )
    for group in ("ledgers", "final_states"):
        for item in record.get(group, []):
            if forged.is_file(item["path"]):
                item["sha256"] = forged.sha(item["path"])
    for inst in record.get("instances", []):
        for key in ("ledger", "final_state"):
            if inst.get(key) and forged.is_file(inst[key]["path"]):
                inst[key]["sha256"] = forged.sha(inst[key]["path"])
    if record.get("ledgers"):
        lines = "".join(
            f"{i['path']} {i['sha256']}\n"
            for i in sorted(record["ledgers"], key=lambda i: i["path"])
        )
        record["ledger_digest"] = sha256_bytes(lines.encode())
    record["attempt_sha256"] = attempt_digest(forged, prefix)


def reseal_index(forged: MemStore, record_rel: str, record: dict) -> None:
    entries = (
        [strict_json(line) for line in forged.read(INDEX_REL).decode().splitlines()]
        if forged.is_file(INDEX_REL)
        else []
    )
    prev, lines = GENESIS, []
    for entry in entries:
        if entry["record"] == record_rel:
            entry["record_sha256"] = forged.sha(record_rel)
            for key in ("case", "scope", "task", "outcome"):
                if key in record:
                    entry[key] = record[key]
        entry["prev"] = prev
        line = index_line(entry)
        lines.append(line)
        prev = index_hash(line)
    forged.files[INDEX_REL] = "".join(line + "\n" for line in lines).encode()
    forged.files[REGISTRY_REL] = _set_meta_head(
        forged.read(REGISTRY_REL).decode(), prev if lines else None, len(lines)
    ).encode()


def _rejected_for(verdict: Verdict, needle: str) -> bool:
    return not verdict.accepted and any(needle in e for e in verdict.errors)


def oracle_cwr019(ctx: OracleContext) -> dict:
    """Recompute CWR-019 from the attempt's final state: a trap suite, i.e. a small repository written by
    the real runner (registry, attempt index, records, logs, ledgers and final state of fixture cases).
    The oracle verifies that the suite's base attempt is a genuine accepted PASS, then forges it in
    memory in every way the case names (always recomputing every digest, the index and its pinned
    head) and checks that this checker refuses each forgery for the named reason. It never reads the
    driver's ASSERT lines."""
    expected = {i.split(".")[1]: i for i in ctx.expected}
    suites = [p for p in ctx.final_states if p.rsplit("/", 1)[-1] == SUITE_NAME]
    if len(suites) != 1:
        return {
            i: (
                "FAIL",
                f"the attempt carries {len(suites)} trap suites ({SUITE_NAME}) as final state, needs 1",
            )
            for i in ctx.expected
        }
    try:
        suite = strict_json(ctx.final_states[suites[0]].decode())
        if suite.get("format") != "cwr.cwr019-suite" or not isinstance(suite.get("files"), dict):
            raise ValueError("not a cwr.cwr019-suite document")
        store = MemStore({k: v.encode() for k, v in suite["files"].items()})
        base_rel, comp_rel, zero_rel = suite["base"], suite["component"], suite["runner_zero"]
        registry = parse_kvx_text(store.read(REGISTRY_REL).decode(), REGISTRY_REL)
    except (
        ValueError,
        KeyError,
        AttributeError,
        TypeError,
        FileNotFoundError,
        UnicodeDecodeError,
    ) as error:
        return {i: ("FAIL", f"trap suite unreadable: {error}") for i in ctx.expected}

    out: dict = {}
    base = check_rel(store, base_rel, suite=True)
    rec = base.record or {}
    base_case = registry.get(f"case.{rec.get('case')}", {})
    base_problems = []
    if not base.accepted or base.recomputed != "PASS" or rec.get("outcome") != "PASS":
        base_problems.append(
            f"base attempt {base_rel} is not an accepted, recomputed PASS: {base.errors[:3]}"
        )
    elif rec.get("scope") != "case" or not rec.get("instances") or not rec.get("observed"):
        base_problems.append(
            "base attempt must be case scope with an Instance and observed assertions"
        )
    elif (
        not base_case.get("registered_builds")
        or rec.get("build_hash") not in base_case["registered_builds"]
    ):
        base_problems.append("base attempt must run on a registered Build")
    if base_problems:
        return {i: ("FAIL", base_problems[0]) for i in ctx.expected}
    ledger_rel = rec["instances"][0]["ledger"]["path"]

    def trial(mutate, needle, **kw) -> tuple[bool, str]:
        forged, _record = forge_in_memory(store, base_rel, mutate, **kw)
        verdict = check_rel(forged, base_rel, suite=True)
        return _rejected_for(verdict, needle), (
            verdict.errors[:2] if verdict.errors else "accepted"
        )

    def results(checks) -> tuple[str, str]:
        failed = [label for ok, label, _ in checks if not ok]
        detail = "; ".join(f"{label}: {why}" for ok, label, why in checks if not ok)
        return (
            ("FAIL", f"{len(failed)}/{len(checks)} traps not refused: {detail}")
            if failed
            else (
                "PASS",
                f"{len(checks)}/{len(checks)} traps refused: "
                + ", ".join(label for _, label, _ in checks),
            )
        )

    # a1: zero assertions
    def no_asserts(record, store_):
        for inv in record["invocations"]:
            text = store_.read(inv["stdout_log"]).decode()
            store_.files[inv["stdout_log"]] = "".join(
                ln + "\n" for ln in text.splitlines() if not ASSERT_LINE.match(ln)
            ).encode()
        record["observed"], record["coverage_gaps"] = [], list(record["assertion_ids"])

    ok, why = trial(no_asserts, "no assertions were observed")
    checks = [(ok, "PASS with every ASSERT line and observation removed", why)]
    zero = check_rel(store, zero_rel, suite=True)
    zrec = zero.record or {}
    zlogs = "".join(
        store.read(i["stdout_log"]).decode()
        for i in zrec.get("invocations", [])
        if store.is_file(i["stdout_log"])
    )
    ok = (
        zero.accepted
        and zrec.get("exit_code") == 0
        and zrec.get("outcome") == "FAIL"
        and not any(ASSERT_LINE.match(ln) for ln in zlogs.splitlines())
    )
    checks.append(
        (
            ok,
            "runner records a driver exiting 0 without assertions as FAIL",
            zero.errors[:2] or zrec.get("outcome"),
        )
    )
    out["a1"] = results(checks)

    # a2: Build hash different from the registered Build (record and ledger rewritten consistently)
    registered = rec["build_hash"]
    stale = sha256_bytes(b"stale:" + registered.encode())

    def swap_build(record, store_):
        for path in store_.files_under(f"{EVIDENCE_REL}/{record['record_id']}"):
            store_.files[path] = store_.files[path].replace(registered.encode(), stale.encode())
        text = json.dumps(record).replace(registered, stale)
        record.clear()
        record.update(strict_json(text))

    ok, why = trial(swap_build, "differs from the registered Build")
    out["a2"] = results(
        [(ok, "PASS on an unregistered Build with record and ledger rewritten", why)]
    )

    # a3: modified ledger
    def edit_outcome(record, store_):
        lines = store_.read(ledger_rel).decode().splitlines()
        for n, line in enumerate(lines):
            row = strict_json(line)
            if row.get("kind") in ("primitive", "adapter"):
                row["outcome"] = dict(row["outcome"], status="error", code="UNAVAILABLE")
                lines[n] = json.dumps(row, sort_keys=True)
                break
        store_.files[ledger_rel] = "".join(ln + "\n" for ln in lines).encode()

    def drop_record(record, store_):
        lines = store_.read(ledger_rel).decode().splitlines(keepends=True)
        del lines[len(lines) // 2]
        store_.files[ledger_rel] = "".join(lines).encode()

    def status_only(record, store_):
        store_.files[ledger_rel] = store_.read(ledger_rel).replace(
            b'"status": "ok"', b'"status": "error"', 1
        )

    checks = []
    for label, mutate, needle, kw in (
        (
            "ledger outcome edited, digests left as recorded",
            edit_outcome,
            "modified ledger",
            {"digests": False},
        ),
        (
            "ledger outcome edited, every digest, the index and its head recomputed",
            edit_outcome,
            "recomputed FAIL by the independent oracle",
            {},
        ),
        (
            "ledger status edited outside the envelope, everything recomputed",
            status_only,
            "violates the kernel ledger schema",
            {},
        ),
        ("ledger record deleted, everything recomputed", drop_record, "gap or duplicate", {}),
        (
            "ledger outcome edited, record digests recomputed but the index left alone",
            edit_outcome,
            "does not match the attempt index",
            {"index": False},
        ),
    ):
        ok, why = trial(mutate, needle, **kw)
        checks.append((ok, label, why))
    out["a3"] = results(checks)

    # a4: mismatched seed
    def header_seed(record, store_):
        lines = store_.read(ledger_rel).decode().splitlines()
        row = strict_json(lines[0])
        row["seed"] = int(row.get("seed", 0)) + 1
        lines[0] = json.dumps(row, sort_keys=True)
        store_.files[ledger_rel] = "".join(ln + "\n" for ln in lines).encode()

    def record_seed(record, store_):
        record["seed"] = (record["seed"] or 0) + 1
        for inst in record["instances"]:
            inst["seed"] = record["seed"]

    checks = []
    for label, mutate in (
        ("ledger header seed changed", header_seed),
        ("record and instance seed changed", record_seed),
    ):
        ok, why = trial(mutate, "seed mismatch")
        checks.append((ok, label, why))
    out["a4"] = results(checks)

    # a5: separation, recomputed from this program's own source
    problems = vet_source(
        Path(__file__).read_text(), allowed_modules=frozenset({"importlib"}), allow_io=True
    )
    out["a5"] = (
        ("FAIL", "check_evidence.py " + "; ".join(problems))
        if problems
        else (
            "PASS",
            "check_evidence.py imports only the standard library, starts no process, opens no socket; "
            "oracles are vetted to the same rule without file access",
        )
    )

    # a6: a component PASS leaves the parent case UNRUN
    comp = check_rel(store, comp_rel, suite=True)
    crec = comp.record or {}
    entries, _ = load_index(store, registry.get("meta", {}))
    case_scope = [e for e in entries if e["case"] == crec.get("case") and e["scope"] == "case"]
    status = derive_status(crec.get("case", "CWR-000"), store=store, suite=True)
    checks = [
        (
            comp.accepted
            and crec.get("scope") == "task"
            and crec.get("outcome") == "PASS"
            and not case_scope,
            "suite holds an accepted component PASS and no case-scope attempt of its case",
            comp.errors[:2],
        ),
        (
            status["status"] == "UNRUN"
            and any(c["outcome"] == "PASS" for c in status["component_records"]),
            "derived parent status is UNRUN",
            status["status"],
        ),
    ]

    def promote(record, store_):
        record["scope"], record["task"] = "case", None

    forged, _ = forge_in_memory(store, comp_rel, promote)
    verdict = check_rel(forged, comp_rel, suite=True)
    checks.append(
        (
            not verdict.accepted,
            "component record relabelled as case scope is refused",
            verdict.errors[:1],
        )
    )
    out["a6"] = results(checks)

    # a7: complete, append-only records
    checks = []
    missed = []
    for name in default_schema().document["required"]:
        forged, _ = forge_in_memory(
            store, base_rel, lambda r, s, n=name: r.pop(n, None), digests=False
        )
        if not _rejected_for(
            check_rel(forged, base_rel, suite=True), f"missing required field {name}"
        ):
            missed.append(name)
    checks.append(
        (
            not missed,
            f"each of {len(default_schema().document['required'])} required fields missing is refused",
            missed,
        )
    )
    forged = store.copy()
    del forged.files[base_rel]
    status = derive_status(rec["case"], store=forged, suite=True)
    checks.append(
        (
            status["status"] is None and any("is missing" in e for e in status["integrity_errors"]),
            "deleting an indexed record is reported",
            status["integrity_errors"][:1],
        )
    )
    forged = store.copy()
    old_id = rec["record_id"]
    new_id = old_id.rsplit("-", 1)[0] + "-99"
    for path in store.files_under(f"{EVIDENCE_REL}/{old_id}"):
        forged.files[path.replace(old_id, new_id, 1)] = store.files[path]
    extra = strict_json(json.dumps(rec).replace(old_id, new_id))
    reseal_digests(extra, forged)
    extra_rel = f"{EVIDENCE_REL}/{new_id}.json"
    forged.files[extra_rel] = (json.dumps(extra, indent=2, sort_keys=True) + "\n").encode()
    verdict = check_rel(forged, extra_rel, suite=True)
    checks.append(
        (
            _rejected_for(verdict, "is not in the append-only attempt index"),
            "a self-consistent record the runner never appended is refused",
            verdict.errors[:1],
        )
    )
    out["a7"] = results(checks)

    # a8: no registered inventory, no PASS
    text = store.read(REGISTRY_REL).decode()
    lines, inside = [], False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if re.fullmatch(r"\[.+\]", stripped):
            inside = stripped == f"[case.{rec['case']}]"
        elif inside and re.match(r"a\d+\s*=", stripped):
            continue
        lines.append(line)
    forged = store.copy()
    forged.files[REGISTRY_REL] = "".join(lines).encode()
    verdict = check_rel(forged, base_rel, suite=True)
    out["a8"] = results(
        [
            (
                _rejected_for(verdict, "has no registered assertion inventory"),
                "the genuine PASS against a registry without the case inventory is refused",
                verdict.errors[:1],
            )
        ]
    )
    return {expected[k]: v for k, v in out.items() if k in expected}


BUILTIN_ORACLES = {"CWR-019": oracle_cwr019}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Independent checker for CWR attempt records.")
    parser.add_argument("records", nargs="*", help="attempt record files, or - with --staged")
    parser.add_argument(
        "--root", default=str(DEFAULT_ROOT), help="repository root (default: this checkout)"
    )
    parser.add_argument(
        "--staged", action="store_true", help="check one not-yet-written record from stdin"
    )
    parser.add_argument(
        "--status", metavar="CASE", help="derive the status of a case from its records"
    )
    parser.add_argument("--json", action="store_true", help="print verdicts as JSON")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    if args.status:
        if not CASE_RE.fullmatch(args.status):
            parser.error("--status needs a CWR-NNN case id")
        result = derive_status(args.status, root)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 1 if result["integrity_errors"] else 0
    if args.staged:
        if args.records != ["-"]:
            parser.error("--staged reads exactly one record from stdin: pass -")
        try:
            record = strict_json(sys.stdin.read())
        except ValueError as error:
            print(json.dumps({"accepted": False, "errors": [f"unreadable record: {error}"]}))
            return 2
        verdicts = [check_record(record, root, staged=True)]
    else:
        if not args.records:
            parser.error("name at least one record")
        verdicts = [check_file(Path(p), root) for p in args.records]
    for verdict in verdicts:
        if args.json or args.staged:
            print(json.dumps(verdict.as_dict(), sort_keys=True))
        elif verdict.accepted:
            print(f"ACCEPT {verdict.path} outcome={verdict.claimed}")
        else:
            print(f"REJECT {verdict.path}: " + " | ".join(verdict.errors))
    return 0 if all(v.accepted for v in verdicts) else 1


if __name__ == "__main__":
    sys.exit(main())

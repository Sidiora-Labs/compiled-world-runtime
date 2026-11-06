# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Helpers for CWR task-suite success predicates (task 4.2, clauses 11.2 and 11.3).

A predicate is ``success(state, ledger) -> bool`` in ``benchmark/tasks/<id>/success.py``. It is a
pure function of two inputs and nothing else:

``state``
    The final Instance state view, a plain dict with the sections below. It is the parsed
    ``final_state.kvx`` of a run (see ``load_state``). File contents are UTF-8 strings, or
    ``{"base64": "..."}`` for bytes that are not valid UTF-8.

    =========  ============================================================================
    fs         ``{"files": {path: content}, "locks": [path, ...]}``
    kv         ``{"namespaces": {ns: {key: value}}}``
    jobs       ``{"jobs": {id: {"kind", "n", "submitted_at", "status", "result"?,
               "fail_at"?, "cancelled_at"?}}}``; ``status`` as of the final world_time,
               ``result`` present when status is ``done``
    world      ``{"time": int, "turn": int}``
    gateway    ``{"idempotency": {adapter: {key: {"receipt": {...}, "payload_hash": str,
               "seq": int}}}}``; the key-adapter idempotency store, ``seq`` being the ledger
               seq of the service call that produced the stored outcome
    =========  ============================================================================

``ledger``
    The Instance ledger as a list of dicts, one per JSONL record, with the fields defined in
    spec/cwr/kernel.md section 8 (``seq``, ``kind``, ``primitive``, ``outcome`` and so on).
    The ledger carries no parameters and no payloads, only ``params_hash`` and, for adapter
    records, the gateway's ``request_hash``; predicates never try to recover parameters from it.
    They may check that a call was made with parameters the task pins, by comparing
    ``request_hash`` with the hash of those parameters (``called_with``).

Rules every predicate follows, checked by ``check_predicate_source``:

* it imports only this module, as ``import common as c``, and uses only the names in
  ``PREDICATE_API`` as ``c.<name>``; ``load_predicate`` hands it a module holding nothing else;
* it never reads model text, sandbox stdout, kernel internals, cassettes or adapter payloads;
* it never opens files, never calls eval/exec, and never mutates its arguments;
* on any value other than ``c`` it names only the public methods of the data types the state
  view and the ledger are made of (``DATA_ATTRIBUTES``), so it cannot walk generator, frame or
  traceback attributes to another frame's globals, and it formats only literal templates whose
  fields look nothing up;
* two evaluations over one state and ledger return the same value and leave the inputs they
  were given unchanged (``evaluate_twice``).

Search hits and post bodies are content, not rules: predicates assert on the shape of the work
(files exist, lines counted, receipts stored under the right key, codes in the ledger, the pinned
request made), never on which hits came back.

Standard library only. Nothing here imports the kernel crate or any CWR implementation module.
"""

from __future__ import annotations

import ast
import base64
import copy
import hashlib
import json
import re
import string
from pathlib import Path

STATE_VIEW_VERSION = 1
CALL_KINDS = ("primitive", "adapter")

# Rule identifiers named in each task's ``rules`` file:
#   <module>.<primitive>[<N>]:<CODE>   declared precondition N (one-based) failing with CODE
#   <module>.<primitive>[rate]:RATE_LIMITED   stub-side rate limit; parsed, but no task lists one
#                                             while replay does not enforce the stub limits
#   <module>.<primitive>[key]:DUPLICATE_KEY   key-idempotency rule
#   <module>.<primitive>[shape]       interface shape guarantee, content not guaranteed
#   <module>.<primitive>[clock]       transition driven only by world_time
#   <module>.<primitive>[stream]      RNG stream advanced only by this primitive
RULE_RE = re.compile(
    r"^(?P<primitive>[a-z]+\.[a-z_]+)\[(?P<selector>[1-9][0-9]*|rate|key|shape|clock|stream)\]"
    r"(?::(?P<code>[A-Z_]+))?$"
)
ENVELOPE_CODES = frozenset(
    {
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
)


# ---------------------------------------------------------------------------------------------
# State: fs
# ---------------------------------------------------------------------------------------------


def _files(state) -> dict:
    return (state.get("fs") or {}).get("files") or {}


def _decode(content) -> bytes:
    if isinstance(content, str):
        return content.encode("utf-8")
    if isinstance(content, dict) and isinstance(content.get("base64"), str):
        return base64.b64decode(content["base64"])
    raise ValueError("file content must be a string or {'base64': ...}")


def file_exists(state, path: str) -> bool:
    return path in _files(state)


def file_bytes(state, path: str):
    """Content of ``path`` as bytes, or None when the file does not exist."""
    files = _files(state)
    return _decode(files[path]) if path in files else None


def file_text(state, path: str):
    """Content of ``path`` decoded as UTF-8 (invalid bytes replaced), or None."""
    data = file_bytes(state, path)
    return None if data is None else data.decode("utf-8", "replace")


def file_lines(state, path: str):
    """Non-empty lines of ``path`` with trailing whitespace removed, or None."""
    text = file_text(state, path)
    if text is None:
        return None
    return [line.rstrip() for line in text.splitlines() if line.strip()]


def files_under(state, prefix: str) -> list:
    """Sorted paths that start with ``prefix`` (prefixes are string conventions, not dirs)."""
    return sorted(path for path in _files(state) if path.startswith(prefix))


def is_locked(state, path: str) -> bool:
    return path in set((state.get("fs") or {}).get("locks") or [])


def locked_under(state, prefix: str) -> list:
    return sorted(p for p in (state.get("fs") or {}).get("locks") or [] if p.startswith(prefix))


# ---------------------------------------------------------------------------------------------
# State: kv
# ---------------------------------------------------------------------------------------------


def _ns(state, ns: str) -> dict:
    return ((state.get("kv") or {}).get("namespaces") or {}).get(ns) or {}


def kv_has(state, ns: str, key: str) -> bool:
    return key in _ns(state, ns)


def kv_get(state, ns: str, key: str):
    """Value stored under ``ns``/``key``, or None when absent."""
    return _ns(state, ns).get(key)


def kv_keys(state, ns: str) -> list:
    return sorted(_ns(state, ns))


def kv_used(state, ns: str) -> int:
    return sum(len(value) for value in _ns(state, ns).values())


# ---------------------------------------------------------------------------------------------
# State: jobs
# ---------------------------------------------------------------------------------------------


def _jobs(state) -> dict:
    return (state.get("jobs") or {}).get("jobs") or {}


def job_known(state, job_id: str) -> bool:
    return job_id in _jobs(state)


def job_status(state, job_id: str):
    job = _jobs(state).get(job_id)
    return None if job is None else job.get("status")


def job_result(state, job_id: str):
    """Result of a done job, or None when unknown, unfinished, failed or cancelled."""
    job = _jobs(state).get(job_id)
    if job is None or job.get("status") != "done":
        return None
    return job.get("result")


def jobs_of_kind(state, kind: str) -> list:
    """Ids of jobs of ``kind`` ordered by submission tick, then id."""
    found = [
        (job.get("submitted_at", 0), job_id)
        for job_id, job in _jobs(state).items()
        if job.get("kind") == kind
    ]
    return [job_id for _, job_id in sorted(found)]


# ---------------------------------------------------------------------------------------------
# State: gateway idempotency store (key adapters)
# ---------------------------------------------------------------------------------------------


def _idem(state, adapter: str) -> dict:
    return ((state.get("gateway") or {}).get("idempotency") or {}).get(adapter) or {}


def receipt_for_key(state, key: str, adapter: str = "publish.post"):
    """Stored receipt for an idempotency key, or None."""
    entry = _idem(state, adapter).get(key)
    return None if entry is None else entry.get("receipt")


def receipt_id(state, key: str, adapter: str = "publish.post"):
    receipt = receipt_for_key(state, key, adapter)
    return receipt.get("id") if isinstance(receipt, dict) else None


def idempotency_entry_seq(state, key: str, adapter: str = "publish.post"):
    """Ledger seq of the call that produced the stored outcome for ``key``, or None."""
    entry = _idem(state, adapter).get(key)
    return entry.get("seq") if isinstance(entry, dict) else None


def idempotency_keys(state, adapter: str = "publish.post") -> list:
    return sorted(_idem(state, adapter))


def posted_ok(state, ledger, key: str, adapter: str = "publish.post") -> bool:
    """True when ``key`` is in the idempotency store and the ledger record it points at is an
    ok call of ``adapter``. This ties state and ledger together without reading any payload."""
    entry = _idem(state, adapter).get(key)
    if entry is None or not isinstance(entry.get("seq"), int):
        return False
    record = record_at(ledger, entry["seq"])
    return (
        record is not None
        and record.get("kind") in CALL_KINDS
        and record.get("primitive") == adapter
        and (record.get("outcome") or {}).get("status") == "ok"
    )


def request_hash(adapter: str, params: dict) -> str:
    """The gateway's canonical request hash of one adapter call: sha256 over the canonical JSON
    of ``{"adapter": adapter, "params": params}`` (sorted keys, no whitespace, UTF-8), the scheme
    sha256-cwr-canonical-json-v1 of crates/cwr-kernel/src/gateway/cassette.rs."""
    text = json.dumps(
        {"adapter": adapter, "params": params},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def called_with(ledger, adapter: str, params: dict, status="ok") -> list:
    """Adapter records of ``adapter`` whose gateway request hash is that of ``params`` and whose
    outcome status is ``status`` (any status when None). This binds a call to the parameters a
    task pins without reading any parameter or payload from the ledger."""
    wanted = request_hash(adapter, params)
    return [
        record
        for record in calls(ledger, adapter, status)
        if record.get("kind") == "adapter"
        and (record.get("gateway") or {}).get("request_hash") == wanted
    ]


# ---------------------------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------------------------


def header(ledger):
    return ledger[0] if ledger and ledger[0].get("kind") == "header" else None


def record_at(ledger, seq: int):
    for record in ledger:
        if record.get("seq") == seq:
            return record
    return None


def calls(ledger, primitive=None, status=None, code=None) -> list:
    """Records of kind primitive or adapter, filtered by name, outcome status and code."""
    found = []
    for record in ledger:
        if record.get("kind") not in CALL_KINDS:
            continue
        outcome = record.get("outcome") or {}
        if primitive is not None and record.get("primitive") != primitive:
            continue
        if status is not None and outcome.get("status") != status:
            continue
        if code is not None and outcome.get("code") != code:
            continue
        found.append(record)
    return found


def count(ledger, primitive=None, status=None, code=None) -> int:
    return len(calls(ledger, primitive, status, code))


def ok_count(ledger, primitive: str) -> int:
    return count(ledger, primitive, status="ok")


def failures(ledger, primitive=None, code=None) -> list:
    return calls(ledger, primitive, status="error", code=code)


def first_seq(ledger, primitive: str, status: str = "ok"):
    found = calls(ledger, primitive, status=status)
    return found[0]["seq"] if found else None


def last_seq(ledger, primitive: str, status: str = "ok"):
    found = calls(ledger, primitive, status=status)
    return found[-1]["seq"] if found else None


def turns(ledger) -> list:
    return [record for record in ledger if record.get("kind") == "turn"]


def final_time(ledger):
    return ledger[-1].get("world_time") if ledger else None


def rate_limited_beyond_first(ledger, primitive: str) -> int:
    """Number of RATE_LIMITED records for ``primitive`` after the first one. Zero means the
    script respected the limit once it had been told about it."""
    return max(0, count(ledger, primitive, status="error", code="RATE_LIMITED") - 1)


def brackets(ledger, outer: str, inner: str) -> bool:
    """True when ok calls of ``outer`` happen both before the first and after the last ok call of
    ``inner``. Rule-reuse tasks use it to check the phase order their instruction asks for."""
    first_outer, last_outer = first_seq(ledger, outer), last_seq(ledger, outer)
    first_inner, last_inner = first_seq(ledger, inner), last_seq(ledger, inner)
    if None in (first_outer, last_outer, first_inner, last_inner):
        return False
    return first_outer < first_inner and last_inner < last_outer


def no_invariant_violation(ledger) -> bool:
    return count(ledger, code="INVARIANT_VIOLATION") == 0


# ---------------------------------------------------------------------------------------------
# Rule identifiers
# ---------------------------------------------------------------------------------------------


def parse_rule(rule: str) -> dict:
    """Split a rule identifier into primitive, selector and code; raise ValueError if bad."""
    match = RULE_RE.match(rule)
    if not match:
        raise ValueError(f"bad rule identifier {rule!r}")
    parts = match.groupdict()
    if parts["code"] is not None and parts["code"] not in ENVELOPE_CODES:
        raise ValueError(f"rule {rule!r} names a code outside the envelope")
    if parts["selector"] in ("rate",) and parts["code"] != "RATE_LIMITED":
        raise ValueError(f"rule {rule!r}: rate rules fail with RATE_LIMITED")
    if parts["selector"] in ("key",) and parts["code"] != "DUPLICATE_KEY":
        raise ValueError(f"rule {rule!r}: key rules fail with DUPLICATE_KEY")
    if parts["selector"].isdigit() and parts["code"] is None:
        raise ValueError(f"rule {rule!r}: precondition rules name their code")
    return parts


# ---------------------------------------------------------------------------------------------
# Loading inputs (driver side; predicates never call these)
# ---------------------------------------------------------------------------------------------


def _kvx_sections(text: str) -> dict:
    sections: dict = {}
    current = None
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        header_match = re.fullmatch(r"\[([A-Za-z0-9_.-]+)\]", line)
        if header_match:
            name = header_match.group(1)
            if name in sections:
                raise ValueError(f"line {number}: duplicate section [{name}]")
            current = sections[name] = {}
            continue
        assignment = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_-]*)\s*=\s*(.+)", line)
        if not assignment or current is None:
            raise ValueError(f"line {number}: unsupported or out-of-section syntax")
        key, value = assignment.groups()
        if key in current:
            raise ValueError(f"line {number}: duplicate key {key}")
        current[key] = json.loads(value)
    return sections


def load_state(path) -> dict:
    """Parse a final state file (``.kvx`` in the initial.kvx section layout, or ``.json``)."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        return json.loads(text)
    sections = _kvx_sections(text)
    sections.pop("meta", None)
    return sections


def load_ledger(path) -> list:
    """Parse a ledger JSONL file. A truncated final record is refused, as the kernel does."""
    records = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as error:
            raise ValueError(f"ledger line {number} is not a complete JSON record") from error
    for index, record in enumerate(records):
        if record.get("seq") != index:
            raise ValueError(f"ledger seq gap or duplicate at line {index + 1}")
    return records


def initial_state(task_dir) -> dict:
    """The starting state of a task in the state-view layout (world time 0, empty gateway)."""
    sections = _kvx_sections(Path(task_dir, "initial.kvx").read_text(encoding="utf-8"))
    sections.pop("meta", None)
    sections.setdefault("fs", {}).setdefault("files", {})
    sections["fs"].setdefault("locks", [])
    sections.setdefault("kv", {}).setdefault("namespaces", {})
    sections.setdefault("jobs", {}).setdefault("jobs", {})
    sections.setdefault("world", {"time": 0, "turn": 0})
    sections.setdefault("gateway", {"idempotency": {}})
    return sections


# ---------------------------------------------------------------------------------------------
# Mapping between the refworld kernel state and the state view (driver side)
# ---------------------------------------------------------------------------------------------
#
# The kernel stores refworld module state in the shapes of examples/refworld/lib/*.py: file
# contents and kv values are bytes, serialised in canonical JSON as {"$bytes": base64}; a job is
# {"kind", "n", "submitted", "fail_at", "cancelled_at"} with ids "j1", "j2", ... from next_id. The
# gateway keeps the key-adapter idempotency store as an append-only event log
# (gateway/idempotency.jsonl in the Instance directory; events pending, done, cleared) whose keys
# are the canonical JSON of the key parameter.


def _to_bytes_json(text: str) -> dict:
    return {"$bytes": base64.b64encode(text.encode("utf-8")).decode("ascii")}


def _from_bytes_json(value):
    """Kernel bytes ({"$bytes": b64}) to a state-view value: UTF-8 text or {"base64": ...}."""
    if isinstance(value, dict) and isinstance(value.get("$bytes"), str):
        data = base64.b64decode(value["$bytes"])
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return {"base64": value["$bytes"]}
    return value


def kernel_initial_state(task_dir) -> dict:
    """``initial.kvx`` of a task as the ``--initial`` JSON of ``cwr instance create``: module
    states in the refworld lib shapes. Strings become bytes; ``submitted_at`` becomes the lib's
    ``submitted``; job ids are kept, and ``next_id`` starts at 1 (submitted ids are ``jN``)."""
    view = initial_state(task_dir)
    jobs = {}
    for job_id, job in sorted(view["jobs"]["jobs"].items()):
        if "result" in job:
            raise ValueError(f"{task_dir}: job {job_id}: a job result is derived, not stored")
        jobs[job_id] = {
            "kind": job["kind"],
            "n": job["n"],
            "submitted": job["submitted_at"],
            "fail_at": job.get("fail_at"),
            "cancelled_at": job.get("cancelled_at"),
        }
    return {
        "fs": {
            "files": {p: _to_bytes_json(t) for p, t in sorted(view["fs"]["files"].items())},
            "locks": sorted(view["fs"]["locks"]),
        },
        "kv": {
            "namespaces": {
                ns: {key: _to_bytes_json(v) for key, v in sorted(values.items())}
                for ns, values in sorted(view["kv"]["namespaces"].items())
                if values
            }
        },
        "jobs": {"next_id": 1, "jobs": jobs},
    }


def job_status_at(job: dict, now: int) -> str:
    """reference-world.md section 2.3: queued while now < t + 1, running while now < t + n, then
    done; failed from fail_at (when fail_at <= t + n) or from the tick of cancellation."""
    if job.get("cancelled_at") is not None and now >= job["cancelled_at"]:
        return "failed"
    start, length, fail_at = job["submitted"], job["n"], job.get("fail_at")
    if fail_at is not None and fail_at <= start + length and now >= fail_at:
        return "failed"
    if now < start + 1:
        return "queued"
    if now < start + length:
        return "running"
    return "done"


def job_result_of(job_id: str, job: dict) -> str:
    """The value refworld's jobs.result returns for a done job (examples/refworld/lib/jobs.py)."""
    return f"{job['kind']}:{job_id}:{job['n']}"


def read_key_store(gateway_dir) -> list:
    """Events of the gateway's key idempotency store, in file order ([] when absent). A torn final
    line, the only damage a crash can leave, is ignored as the kernel ignores it."""
    path = Path(gateway_dir, "idempotency.jsonl")
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").split("\n")
    events = []
    for index, line in enumerate(lines):
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            if index + 1 == len(lines):
                break
            raise
    return events


def _store_seq(ledger, adapter: str, event: dict):
    """Ledger seq of the service call that produced the outcome a ``done`` key-store event holds.

    A key slot can be released (RATE_LIMITED, a defect) and filled again by a later identical call,
    so the first record with the request hash is not necessarily the one that produced the stored
    outcome. The bound record is the last record of ``adapter`` that the gateway sent to the
    service (not answered from the store) with the event's request hash and the stored outcome:
    status ok when the event holds no failure, otherwise the failure's code."""
    failure = event.get("failure")
    code = failure.get("code") if isinstance(failure, dict) else None
    for record in reversed(ledger):
        gateway = record.get("gateway") or {}
        outcome = record.get("outcome") or {}
        if (
            record.get("kind") != "adapter"
            or record.get("primitive") != adapter
            or gateway.get("request_hash") != event.get("request_hash")
            or gateway.get("via") == "store"
        ):
            continue
        if failure is None and outcome.get("status") == "ok":
            return record.get("seq")
        if failure is not None and outcome.get("status") == "error" and outcome.get("code") == code:
            return record.get("seq")
    return None


def state_view_from_instance(kernel_state: dict, ledger: list, key_events: list) -> dict:
    """The predicate state view of a finished refworld Instance, from ``cwr instance state``, the
    ledger and the gateway key-store events only. Job status is evaluated at the final world_time
    of the ledger; ``seq`` of an idempotency entry is the ledger seq of the adapter call that produced
    the stored outcome (``_store_seq``)."""
    final = ledger[-1] if ledger else {}
    now = final.get("world_time", 0)
    fs = kernel_state.get("fs") or {}
    kv = kernel_state.get("kv") or {}
    jobs = {}
    for job_id, job in sorted(((kernel_state.get("jobs") or {}).get("jobs") or {}).items()):
        status = job_status_at(job, now)
        entry = {"kind": job["kind"], "n": job["n"], "submitted_at": job["submitted"]}
        entry["status"] = status
        for field in ("fail_at", "cancelled_at"):
            if job.get(field) is not None:
                entry[field] = job[field]
        if status == "done":
            entry["result"] = job_result_of(job_id, job)
        jobs[job_id] = entry
    slots: dict = {}
    for event in key_events:
        slot_key = (event.get("adapter"), event.get("key"))
        if event.get("event") == "cleared":
            slots.pop(slot_key, None)
        elif event.get("event") in ("pending", "done"):
            slots[slot_key] = event
    idempotency: dict = {}
    for (adapter, raw_key), event in sorted(slots.items()):
        if event.get("event") != "done":
            continue
        key = json.loads(raw_key)
        idempotency.setdefault(adapter, {})[key] = {
            "receipt": event.get("outcome") if event.get("failure") is None else None,
            "failure": event.get("failure"),
            "payload_hash": event.get("payload_hash"),
            "seq": _store_seq(ledger, adapter, event),
        }
    return {
        "fs": {
            "files": {p: _from_bytes_json(v) for p, v in sorted((fs.get("files") or {}).items())},
            "locks": sorted(fs.get("locks") or []),
        },
        "kv": {
            "namespaces": {
                ns: {key: _from_bytes_json(v) for key, v in sorted(values.items())}
                for ns, values in sorted((kv.get("namespaces") or {}).items())
            }
        },
        "jobs": {"jobs": jobs},
        "world": {"time": now, "turn": final.get("turn", 0)},
        "gateway": {"idempotency": idempotency},
    }


# ---------------------------------------------------------------------------------------------
# Predicate loading, purity audit and double evaluation
# ---------------------------------------------------------------------------------------------

# The whole interface a predicate may use, as c.<name>. Everything else in this module (loaders,
# digests, the state-view mapping, the purity audit, and the modules it imports) is driver side:
# check_predicate_source rejects any other attribute of c, and load_predicate hands the predicate
# a module that holds only these names.
PREDICATE_API = frozenset(
    {
        "CALL_KINDS",
        "ENVELOPE_CODES",
        "brackets",
        "called_with",
        "calls",
        "count",
        "failures",
        "file_bytes",
        "file_exists",
        "file_lines",
        "file_text",
        "files_under",
        "final_time",
        "first_seq",
        "header",
        "idempotency_entry_seq",
        "idempotency_keys",
        "is_locked",
        "job_known",
        "job_result",
        "job_status",
        "jobs_of_kind",
        "kv_get",
        "kv_has",
        "kv_keys",
        "kv_used",
        "last_seq",
        "locked_under",
        "no_invariant_violation",
        "ok_count",
        "posted_ok",
        "rate_limited_beyond_first",
        "receipt_for_key",
        "receipt_id",
        "record_at",
        "request_hash",
        "turns",
    }
)
FORBIDDEN_CALLS = frozenset(
    {
        "open",
        "eval",
        "exec",
        "compile",
        "__import__",
        "input",
        "globals",
        "locals",
        "vars",
        "getattr",
        "setattr",
        "delattr",
        "breakpoint",
        "help",
        "print",
        "load_state",
        "load_ledger",
        "initial_state",
        "load_predicate",
        "kernel_initial_state",
        "read_key_store",
        "state_view_from_instance",
    }
)
FORBIDDEN_NAMES = frozenset({"__builtins__", "__loader__", "__spec__", "__file__"})


def _public_attributes(*types) -> frozenset:
    return frozenset(name for kind in types for name in dir(kind) if not name.startswith("_"))


# The only attributes a predicate may name on a value other than ``c``: the public methods and
# properties of the data types a state view and a ledger are built from. Frame, code, generator,
# coroutine and traceback attributes (gi_frame, f_back, f_globals, tb_frame, co_code, ...) are none
# of them, so no introspection chain can reach another frame's globals. format_map is left out and
# format is admitted only on a literal template whose replacement fields name no attribute or item
# (``_safe_template``), because a field such as "{0.gi_frame}" is attribute access in a string.
DATA_ATTRIBUTES = _public_attributes(
    str, bytes, dict, list, set, frozenset, tuple, int, float, bool
) - {"format_map"}


def _safe_template(node) -> bool:
    """Whether node is a str literal whose replacement fields, nested ones included, are only
    automatic, positional or keyword names (no ``.attr`` and no ``[item]`` lookups)."""
    if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
        return False

    def fields_ok(template: str) -> bool:
        try:
            parsed = list(string.Formatter().parse(template))
        except ValueError:
            return False
        for _literal, field, spec, _conversion in parsed:
            if field is not None and ("." in field or "[" in field):
                return False
            if spec and not fields_ok(spec):
                return False
        return True

    return fields_ok(node.value)


def predicate_api():
    """A module object named ``common`` that holds exactly the ``PREDICATE_API`` names of this
    module. It is what ``import common as c`` yields inside a loaded predicate."""
    import types

    api = types.ModuleType("common", "CWR predicate API (benchmark/predicates/common.py)")
    for name in sorted(PREDICATE_API):
        setattr(api, name, globals()[name])
    return api


def check_predicate_source(source: str) -> list:
    """Return a list of purity violations in a success.py source (empty when clean)."""
    problems = []
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        return [f"syntax error: {error}"]
    successes = [
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "success"
    ]
    if len(successes) != 1:
        problems.append("success.py must define exactly one top-level success(state, ledger)")
    elif [a.arg for a in successes[0].args.args] != ["state", "ledger"]:
        problems.append("success must take exactly (state, ledger)")
    api_values = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "c"
        ):
            api_values.add(id(node.value))
            if node.attr not in PREDICATE_API:
                problems.append(f"line {node.lineno}: c.{node.attr} is not in the predicate API")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name != "common":
                    problems.append(f"line {node.lineno}: import of {alias.name}")
                elif alias.asname != "c":
                    problems.append(f"line {node.lineno}: common must be imported as c")
        elif isinstance(node, ast.ImportFrom):
            problems.append(f"line {node.lineno}: from-import is not allowed")
        elif isinstance(node, ast.Name) and node.id == "c" and id(node) not in api_values:
            problems.append(f"line {node.lineno}: c is used other than as c.<name>")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in FORBIDDEN_CALLS
        ):
            problems.append(f"line {node.lineno}: call to {node.func.id}")
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_CALLS:
            problems.append(f"line {node.lineno}: use of {node.attr}")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            problems.append(f"line {node.lineno}: use of {node.id}")
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            problems.append(f"line {node.lineno}: global state")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            problems.append(f"line {node.lineno}: dunder attribute {node.attr}")
        elif isinstance(node, ast.Attribute) and id(node.value) not in api_values:
            if node.attr not in DATA_ATTRIBUTES:
                problems.append(f"line {node.lineno}: attribute {node.attr} is not a data method")
            elif node.attr == "format" and not _safe_template(node.value):
                problems.append(
                    f"line {node.lineno}: format on anything but a literal template without "
                    "attribute or item fields"
                )
        elif isinstance(node, ast.MatchClass):
            for attr in node.kwd_attrs:
                if attr not in DATA_ATTRIBUTES:
                    problems.append(f"line {node.lineno}: class pattern reads attribute {attr}")
    return problems


def load_predicate(task_dir):
    """Load ``success`` from a task directory. The source must pass ``check_predicate_source``;
    ``import common`` then yields ``predicate_api()``, and no other module can be imported."""
    import builtins

    source = Path(task_dir, "success.py").read_text(encoding="utf-8")
    problems = check_predicate_source(source)
    if problems:
        raise ValueError(f"{task_dir}: impure predicate: {'; '.join(problems)}")
    api = predicate_api()

    def only_common(name, globals_=None, locals_=None, fromlist=(), level=0):
        if name == "common" and level == 0:
            return api
        raise ImportError(f"predicates may import only common, not {name}")

    safe = {
        name: getattr(builtins, name)
        for name in (
            "abs",
            "all",
            "any",
            "bool",
            "chr",
            "dict",
            "enumerate",
            "filter",
            "frozenset",
            "int",
            "isinstance",
            "len",
            "list",
            "map",
            "max",
            "min",
            "ord",
            "range",
            "reversed",
            "set",
            "sorted",
            "str",
            "sum",
            "tuple",
            "zip",
            "ValueError",
            "KeyError",
            "TypeError",
            "IndexError",
            "Exception",
            "True",
            "False",
            "None",
        )
    }
    safe["__import__"] = only_common
    namespace = {"__builtins__": safe, "__name__": "success"}
    exec(compile(source, str(Path(task_dir, "success.py")), "exec"), namespace)
    return namespace["success"]


def evaluate_twice(predicate, state, ledger) -> bool:
    """Evaluate a predicate twice, each time on fresh deep copies, and require equal bool results
    and that neither evaluation changed the copies it was given (a predicate never mutates its
    arguments)."""
    results = []
    for _ in range(2):
        state_copy, ledger_copy = copy.deepcopy(state), copy.deepcopy(ledger)
        state_before = json.dumps(state_copy, sort_keys=True)
        ledger_before = json.dumps(ledger_copy, sort_keys=True)
        results.append(predicate(state_copy, ledger_copy))
        if (
            json.dumps(state_copy, sort_keys=True) != state_before
            or json.dumps(ledger_copy, sort_keys=True) != ledger_before
        ):
            raise ValueError("predicate inputs changed during evaluation")
    first, second = results
    if not isinstance(first, bool) or first is not second:
        raise ValueError(f"predicate is not a deterministic bool: {first!r} then {second!r}")
    return first


# ---------------------------------------------------------------------------------------------
# Digests (same byte layout as the Build hash in spec/cwr/compiler.md section 7)
# ---------------------------------------------------------------------------------------------

TASK_DIR_RE = re.compile(r"^t[0-9]{3}-[a-z0-9-]+$")


def tree_digest(root, relative_to=None) -> str:
    """sha256 over sorted relative paths: path, NUL, decimal length, NUL, bytes, per file."""
    root = Path(root)
    base = Path(relative_to) if relative_to is not None else root
    digest = hashlib.sha256()
    files = sorted(
        (p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts),
        key=lambda p: p.relative_to(base).as_posix().encode("utf-8"),
    )
    for path in files:
        data = path.read_bytes()
        digest.update(path.relative_to(base).as_posix().encode("utf-8"))
        digest.update(b"\0" + str(len(data)).encode("ascii") + b"\0")
        digest.update(data)
    return "sha256:" + digest.hexdigest()


def tree_digest_file(path) -> str:
    """sha256 of one file in the same layout, keyed by its file name."""
    path = Path(path)
    data = path.read_bytes()
    digest = hashlib.sha256()
    digest.update(path.name.encode("utf-8") + b"\0" + str(len(data)).encode("ascii") + b"\0")
    digest.update(data)
    return "sha256:" + digest.hexdigest()


def task_dirs(tasks_root) -> list:
    return sorted(p for p in Path(tasks_root).iterdir() if p.is_dir() and TASK_DIR_RE.match(p.name))


def suite_digest(tasks_root) -> str:
    """Digest over every file of every tNNN-slug directory, paths relative to tasks_root."""
    digest = hashlib.sha256()
    for task in task_dirs(tasks_root):
        for path in sorted(
            (p for p in task.rglob("*") if p.is_file() and "__pycache__" not in p.parts),
            key=lambda p: p.relative_to(tasks_root).as_posix().encode("utf-8"),
        ):
            data = path.read_bytes()
            digest.update(path.relative_to(tasks_root).as_posix().encode("utf-8"))
            digest.update(b"\0" + str(len(data)).encode("ascii") + b"\0")
            digest.update(data)
    return "sha256:" + digest.hexdigest()

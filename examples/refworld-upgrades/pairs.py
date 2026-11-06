#!/usr/bin/env python3
# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Generate and check the refworld upgrade pairs U1 to U6.

Each pair is two complete World Sources, ``UN/A`` and ``UN/B``, derived from the base World Source
in ``examples/refworld`` by exact, asserted text patches, so that the two sides differ in exactly one
rule (plus the documentation that describes that rule). ``UN/B``'s CHANGELOG.kvx is ``UN/A``'s with
one new release prepended. ``benchmark/upgrades.kvx`` freezes, per pair, the expected ContractDiff,
derived bump, recompile notice and the model-visible effect of the change.

Usage, from the repository root:

    python3 examples/refworld-upgrades/pairs.py           # check only (default)
    python3 examples/refworld-upgrades/pairs.py --write   # regenerate UN/A and UN/B, then check

The check is an authoring check, not evidence for CWR-011 or CWR-016. It:

* regenerates every side in memory and requires the files on disk to be byte-identical;
* parses every .kvx with ``parse_kvx`` from spec/cwr/tools/validate_spec.py and runs that tool's
  ``example_errors`` cross-file rules against each side (its example-status rule excepted, as for
  the base world);
* runs every fs, kv and jobs contract fixture of each side against that side's own lib/ through a
  stand-in state handle, evaluating the side's own precondition and effect expressions;
* runs every migration fixture (``migrate(before) == after`` and idempotence);
* recomputes a ContractDiff between A and B and applies the changelog gate of
  spec/cwr/compiler.md section 6 (with the coverage interpretations recorded in upgrades.kvx);
* compares the recomputed diff, bump and rendered notice with the frozen values in
  benchmark/upgrades.kvx;
* plays each pair's post-upgrade task: reference first half on A, upgrade (migrations applied),
  then the post-upgrade and pre-upgrade second halves on B, and requires the post solution to
  satisfy the task predicate and the pre solution to fail with the frozen code.

The stand-in kernel here is an authoring model. It has no sandbox, no Build hash, no SDK codec and
no adapters; it is never a substitute for the kernel drivers of tasks 2.5 and 4.3.
Standard library only.
"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import inspect
import json
import re
import shutil
import sys
import tempfile
import textwrap
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
BASE = REPO / "examples" / "refworld"
UPGRADES = REPO / "benchmark" / "upgrades.kvx"
PREDICATES = REPO / "benchmark" / "predicates"
TOOLS = REPO / "spec" / "cwr" / "tools"
PAIRS = ["U1", "U2", "U3", "U4", "U5", "U6"]
SKIP = {"README.md"}
DATE = "2026-11-06"

sys.dont_write_bytecode = True  # never leave __pycache__ inside a World Source
sys.path.insert(0, str(TOOLS))
import validate_spec  # noqa: E402  importable only after the sys.path insert above

parse_kvx = validate_spec.parse_kvx


# =================================================================================================
# Source generation
# =================================================================================================


def read_base() -> dict:
    files = {}
    for path in sorted(BASE.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        rel = str(path.relative_to(BASE))
        if rel in SKIP:
            continue
        files[rel] = path.read_text(encoding="utf-8")
    return files


def replace(files: dict, rel: str, old: str, new: str) -> None:
    text = files[rel]
    found = text.count(old)
    if found != 1:
        raise SystemExit(f"patch {rel}: expected exactly one occurrence of {old!r}, found {found}")
    files[rel] = text.replace(old, new)


def set_version(files: dict, version: str) -> None:
    replace(files, "world.kvx", 'version = "0.2.0"', f'version = "{version}"')
    # world.version returns the Build's own version, so its contract fixture follows the bump.
    replace(files, "fixtures/world/version.kvx", 'returned = "0.2.0"', f'returned = "{version}"')


def add_release(files: dict, version: str, summary: str, changes: list) -> None:
    """Prepend a release (newest first) right after the changelog's [meta] section."""
    lines = [
        "",
        f"[release.{version}]",
        f'version = "{version}"',
        f'date = "{DATE}"',
        f"summary = {json.dumps(summary)}",
        "changes = " + json.dumps([ident for ident, _ in changes]),
    ]
    for ident, change in changes:
        lines += ["", f"[change.{ident}]"]
        for key in ("kind", "target", "before", "after", "migration"):
            lines.append(f"{key} = {json.dumps(change.get(key, ''))}")
    block = "\n".join(lines) + "\n"
    marker = '[meta]\nformat = "cwr.changelog"\nschema_version = 1\n'
    replace(files, "CHANGELOG.kvx", marker, marker + block)


def remove_section(files: dict, rel: str, header: str) -> None:
    text = files[rel]
    start = text.index(header)
    following = text.find("\n[", start + len(header))
    end = len(text) if following < 0 else following + 1
    head = text[:start].rstrip("\n") + "\n"
    rest = text[end:]
    files[rel] = head + ("\n" + rest if rest else "")


def remove_function(files: dict, rel: str, signature: str) -> None:
    text = files[rel]
    start = text.index(signature)
    following = text.find("\n\n\ndef ", start)
    if following < 0:
        files[rel] = text[:start].rstrip("\n") + "\n"
    else:
        files[rel] = text[:start] + text[following + 3 :]


FIXTURE_HEADER = (
    "# Copyright Sidiora Labs Inc. CWR® 2026-2027\n"
    "# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0\n"
    "# Contract-test fixtures for {primitive}{variant}. Each [case.<name>] is one ephemeral-Instance run:\n"
    "# world_time and state are the initial conditions (state is keyed by module; modules not named start\n"
    '# from their lib INITIAL_STATE), params is the call, expect is "ok" or the envelope code, precondition\n'
    "# is the one-based index of the failing precondition or null, returned (optional) is the exact value an\n"
    "# internal primitive must return. Value codec: a JSON string in a position typed bytes is its UTF-8\n"
    '# encoding; {{"$repeat": [s, n]}} is the string s repeated n times; nothing else is special.\n'
)


def fixture_file(module: str, primitive: str, variant: str, cases: list) -> str:
    out = FIXTURE_HEADER.format(primitive=primitive, variant=variant)
    out += (
        f'\n[meta]\nformat = "cwr.fixture"\nschema_version = 1\nmodule = "{module}"\n'
        f'primitive = "{primitive}"\n'
    )
    for name, case in cases:
        out += f"\n[case.{name}]\n"
        for key in ("world_time", "state", "params", "expect", "precondition", "returned"):
            if key in case:
                out += f"{key} = {json.dumps(case[key])}\n"
    return out


# ------------------------------------------------------------------------------------------- U1


def u1(base: dict) -> tuple:
    a = dict(base)
    replace(
        a,
        "contracts/fs.kvx",
        'preconditions = [["fs.exists(path)", "NOT_FOUND"], ["not fs.locked(path)", "LOCKED"]]',
        'preconditions = [["fs.exists(path)", "NOT_FOUND"]]',
    )
    replace(
        a,
        "contracts/fs.kvx",
        'doc = "Remove the file at path. Refused when the path does not exist or is locked."',
        'doc = "Remove the file at path, releasing its lock if it holds one. Refused when the path does not exist."',
    )
    replace(
        a,
        "contracts/fs.kvx",
        "while locked it cannot be written, deleted or moved. Refused when already locked.",
        "while locked it cannot be written or moved; deleting it releases the lock. Refused when already locked.",
    )
    replace(
        a,
        "lib/fs.py",
        'def delete(state: Any, path: str) -> None:\n    del _fs(state)["files"][path]\n    return None\n',
        'def delete(state: Any, path: str) -> None:\n    fs = _fs(state)\n    del fs["files"][path]\n'
        '    fs["locks"] = sorted(item for item in fs["locks"] if item != path)\n    return None\n',
    )
    replace(
        a,
        "fixtures/fs/delete.kvx",
        '[case.pre_2_locked]\nworld_time = 0\nstate = {"fs": {"files": {"/a": "x"}, "locks": ["/a"]}}\n'
        'params = {"path": "/a"}\nexpect = "LOCKED"\nprecondition = 2\n',
        '[case.ok_locked]\nworld_time = 0\nstate = {"fs": {"files": {"/a": "x"}, "locks": ["/a"]}}\n'
        'params = {"path": "/a"}\nexpect = "ok"\nprecondition = null\n',
    )
    replace(
        a,
        "manual/conventions.md",
        "fs.exists or fs.locked before fs.delete",
        "fs.exists before fs.delete",
    )
    replace(
        a,
        "manual/examples.md",
        "Delete a file only if it exists and is not locked, unlocking it first when needed.",
        "Delete a file only if it exists.",
    )
    replace(
        a,
        "manual/examples.md",
        "if fs.exists(path=path):\n    if fs.locked(path=path):\n        fs.unlock(path=path)\n    fs.delete(path=path)\n",
        "if fs.exists(path=path):\n    fs.delete(path=path)\n",
    )
    replace(
        a,
        "CHANGELOG.kvx",
        "delete and move refuse locked paths, move refuses an existing destination.",
        "move refuses a locked source or an existing destination, and delete removes a locked file together with its lock.",
    )
    b = dict(base)
    b["CHANGELOG.kvx"] = a["CHANGELOG.kvx"]
    set_version(b, "0.3.0")
    add_release(
        b,
        "0.3.0",
        "fs.delete now refuses a locked path with LOCKED; unlock the path before deleting it.",
        [
            (
                "fs_delete_refuses_locked",
                {
                    "kind": "precondition_added",
                    "target": "fs.delete",
                    "before": "fs.delete removed a locked file and released its lock.",
                    "after": "not fs.locked(path), otherwise LOCKED (precondition 2); unlock the path before deleting it.",
                },
            ),
            (
                "fs_lock_doc_delete",
                {
                    "kind": "doc_only",
                    "target": "fs.lock",
                    "before": "The fs.lock description said that deleting a locked file releases the lock.",
                    "after": "The fs.lock description says that a locked file cannot be deleted.",
                },
            ),
        ],
    )
    return a, b


# ------------------------------------------------------------------------------------------- U2


def u2(base: dict) -> tuple:
    a = dict(base)
    b = dict(base)
    remove_section(b, "contracts/fs.kvx", "[primitive.fs.move]")
    replace(
        b,
        "contracts/fs.kvx",
        "while locked it cannot be written, deleted or moved. Refused when already locked.",
        "while locked it cannot be written or deleted. Refused when already locked.",
    )
    remove_function(b, "lib/fs.py", "def move(state: Any, src: str, dst: str) -> None:")
    del b["fixtures/fs/move.kvx"]
    set_version(b, "0.3.0")
    add_release(
        b,
        "0.3.0",
        "fs.move is removed; to rename a file, read it, write the content to the new path and delete the old path.",
        [
            (
                "fs_move_removed",
                {
                    "kind": "primitive_removed",
                    "target": "fs.move",
                    "before": "fs.move(src, dst) renamed a file; refused when src was missing or locked or dst existed.",
                    "after": "fs.move no longer exists; use fs.read, fs.write and fs.delete.",
                },
            ),
            (
                "fs_lock_doc_move",
                {
                    "kind": "doc_only",
                    "target": "fs.lock",
                    "before": "The fs.lock description said a locked file cannot be written, deleted or moved.",
                    "after": "The fs.lock description says a locked file cannot be written or deleted.",
                },
            ),
        ],
    )
    return a, b


# ------------------------------------------------------------------------------------------- U3


def u3(base: dict) -> tuple:
    a = dict(base)
    replace(
        a,
        "contracts/jobs.kvx",
        'preconditions = [["jobs.known(id)", "NOT_FOUND"], ["jobs.status(id) == \\"done\\"", "CONFLICT"]]',
        'preconditions = [["jobs.known(id)", "NOT_FOUND"], ["jobs.status(id) == \\"done\\"", "NOT_FOUND"]]',
    )
    replace(
        a,
        "contracts/jobs.kvx",
        "Refused with CONFLICT until jobs.status reports done; poll the status, do not guess.",
        "Refused with NOT_FOUND until jobs.status reports done, as for an unknown id; poll the status, do not guess.",
    )
    replace(a, "fixtures/jobs/result.kvx", "[case.pre_2_conflict]", "[case.pre_2_not_found]")
    replace(a, "fixtures/jobs/result.kvx", 'expect = "CONFLICT"', 'expect = "NOT_FOUND"')
    replace(
        a,
        "manual/conventions.md",
        "jobs.result refuses with CONFLICT",
        "jobs.result refuses with NOT_FOUND",
    )
    b = dict(base)
    set_version(b, "0.3.0")
    add_release(
        b,
        "0.3.0",
        "jobs.result on a job that is not done now fails with CONFLICT instead of NOT_FOUND; NOT_FOUND now means only an unknown id.",
        [
            (
                "jobs_result_not_done_conflict",
                {
                    "kind": "precondition_changed",
                    "target": "jobs.result",
                    "before": 'jobs.status(id) == "done", otherwise NOT_FOUND (precondition 2), the same code as an unknown id.',
                    "after": 'jobs.status(id) == "done", otherwise CONFLICT (precondition 2); NOT_FOUND only for an unknown id.',
                },
            ),
        ],
    )
    return a, b


# ------------------------------------------------------------------------------------------- U4


def u4(base: dict) -> tuple:
    a = dict(base)
    b = dict(base)
    replace(
        b,
        "contracts/kv.kvx",
        "a namespaced key/value map with a 1 KiB value limit",
        "a namespaced key/value map with a 512-byte value limit",
    )
    replace(
        b,
        "contracts/kv.kvx",
        '["len(value) <= 1024", "INVALID"]',
        '["len(value) <= 512", "INVALID"]',
    )
    replace(
        b,
        "contracts/kv.kvx",
        "Refused when the value exceeds 1024 bytes,",
        "Refused when the value exceeds 512 bytes,",
    )
    replace(b, "fixtures/kv/set.kvx", '{"$repeat": ["v", 1025]}', '{"$repeat": ["v", 513]}')
    replace(b, "fixtures/kv/set.kvx", '{"$repeat": ["y", 1024]}', '{"$repeat": ["y", 512]}')
    replace(b, "manual/examples.md", 'value = b"x" * 900', 'value = b"x" * 500')
    set_version(b, "0.3.0")
    add_release(
        b,
        "0.3.0",
        "kv.set now refuses values longer than 512 bytes with INVALID (the limit was 1024); values stored earlier are kept.",
        [
            (
                "kv_set_value_limit_512",
                {
                    "kind": "precondition_changed",
                    "target": "kv.set",
                    "before": "len(value) <= 1024, otherwise INVALID (precondition 1).",
                    "after": "len(value) <= 512, otherwise INVALID (precondition 1); values stored earlier keep their length.",
                },
            ),
        ],
    )
    return a, b


# ------------------------------------------------------------------------------------------- U5

FLAT_KV_CONTRACT = """# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
# Rule contracts for the kv module, flat variant: one key/value map with a 1 KiB value limit and a
# 64 KiB quota over the whole store. This is side A of upgrade pair U5 (examples/refworld-upgrades);
# side B is the namespaced kv of examples/refworld. Catalogue shape: spec/cwr/reference-world.md
# section 2.2 without the ns parameter.
# Python reserves the word del, so the lib function and the SDK attribute for kv.del are spelled
# del_ (the trailing-underscore rule for Python keywords); the primitive name is kv.del.

[meta]
format = "cwr.contract"
schema_version = 1
module = "kv"

[primitive.kv.has]
side_effect = "read_only"
params = {"key": "str"}
returns = "bool"
preconditions = []
effects = []
doc = "True when key is stored. Never fails."

[primitive.kv.get]
side_effect = "read_only"
params = {"key": "str"}
returns = "bytes"
preconditions = [["kv.has(key)", "NOT_FOUND"]]
effects = []
doc = "Value stored under key."

[primitive.kv.keys]
side_effect = "read_only"
params = {}
returns = "list[str]"
preconditions = []
effects = []
doc = "Every stored key, sorted bytewise; empty when nothing is stored. Never fails."

[primitive.kv.used]
side_effect = "read_only"
params = {}
returns = "int"
preconditions = []
effects = []
doc = "Total byte length of every stored value; 0 when nothing is stored. Never fails."

[primitive.kv.set]
side_effect = "mutating"
params = {"key": "str", "value": "bytes"}
returns = "None"
preconditions = [["len(value) <= 1024", "INVALID"], ["kv.used() + len(value) <= 65536", "LIMIT"]]
effects = ["kv.has(key)", "kv.get(key) == value"]
doc = "Store value under key, replacing any previous value. Refused when the value exceeds 1024 bytes, or when the store's current usage plus the value would exceed 65536 bytes."

[primitive.kv.del]
side_effect = "mutating"
params = {"key": "str"}
returns = "None"
preconditions = [["kv.has(key)", "NOT_FOUND"]]
effects = ["not kv.has(key)"]
doc = "Remove key. Refused when the key is not stored."
"""

FLAT_KV_LIB = '''# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Kernel-side implementation of the refworld kv module, flat variant (side A of upgrade pair U5).

Runs inside the kernel process against the kernel state API, never in the sandbox. Each public
function implements exactly one contract in contracts/kv.kvx and takes the kernel state handle
followed by the declared parameters. Python reserves ``del``, so the primitive kv.del is implemented
by ``del_`` (the trailing-underscore rule for Python keywords, applied alike by the compiler and the SDK generator).

State shape (the typed state record the ContractDiff compares):
    {key: bytes}   one flat map; side B of U5 migrates it to {"namespaces": {"default": {key: bytes}}}
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import cwr_state  # noqa: F401  kernel state API; permitted import


KvState = dict[str, bytes]


INITIAL_STATE: KvState = {}


def _kv(state: Any) -> KvState:
    return state.module("kv")


def has(state: Any, key: str) -> bool:
    return key in _kv(state)


def get(state: Any, key: str) -> bytes:
    return bytes(_kv(state)[key])


def keys(state: Any) -> list:
    return sorted(_kv(state))


def used(state: Any) -> int:
    return sum(len(value) for value in _kv(state).values())


# Shadows the builtin on purpose: the function name is the primitive name kv.set.
def set(state: Any, key: str, value: bytes) -> None:
    _kv(state)[key] = bytes(value)
    return None


def del_(state: Any, key: str) -> None:
    del _kv(state)[key]
    return None
'''

_FLAT = {"golf-1": "p_0001", "a": "1", "b": "22"}
_BIG = {f"k{i:02d}": {"$repeat": ["x", 1024]} for i in range(64)}


def flat_kv_fixtures() -> dict:
    s, big, empty = {"kv": _FLAT}, {"kv": _BIG}, {"kv": {}}
    ok = {"expect": "ok", "precondition": None}
    spec = {
        "has": [
            ("ok_present", {"state": s, "params": {"key": "golf-1"}, **ok, "returned": True}),
            ("ok_absent", {"state": s, "params": {"key": "c"}, **ok, "returned": False}),
        ],
        "get": [
            (
                "pre_1_not_found",
                {"state": s, "params": {"key": "c"}, "expect": "NOT_FOUND", "precondition": 1},
            ),
            ("ok", {"state": s, "params": {"key": "golf-1"}, **ok, "returned": "p_0001"}),
        ],
        "keys": [
            ("ok", {"state": s, "params": {}, **ok, "returned": ["a", "b", "golf-1"]}),
            ("ok_empty", {"state": empty, "params": {}, **ok, "returned": []}),
        ],
        "used": [
            ("ok", {"state": s, "params": {}, **ok, "returned": 9}),
            ("ok_full", {"state": big, "params": {}, **ok, "returned": 65536}),
            ("ok_empty", {"state": empty, "params": {}, **ok, "returned": 0}),
        ],
        "set": [
            (
                "pre_1_invalid",
                {
                    "state": s,
                    "params": {"key": "c", "value": {"$repeat": ["v", 1025]}},
                    "expect": "INVALID",
                    "precondition": 1,
                },
            ),
            (
                "pre_2_limit",
                {
                    "state": big,
                    "params": {"key": "one-more", "value": "z"},
                    "expect": "LIMIT",
                    "precondition": 2,
                },
            ),
            ("ok_new", {"state": s, "params": {"key": "c", "value": "v"}, **ok}),
            ("ok_replace", {"state": s, "params": {"key": "a", "value": "111"}, **ok}),
        ],
        "del": [
            (
                "pre_1_not_found",
                {"state": s, "params": {"key": "nope"}, "expect": "NOT_FOUND", "precondition": 1},
            ),
            ("ok", {"state": s, "params": {"key": "a"}, **ok}),
        ],
    }
    out = {}
    for name, cases in spec.items():
        cases = [(case_name, {"world_time": 0, **case}) for case_name, case in cases]
        out[f"fixtures/kv/{name}.kvx"] = fixture_file(
            "kv", f"kv.{name}", " (flat variant, side A of U5)", cases
        )
    return out


def u5(base: dict) -> tuple:
    a = dict(base)
    a["contracts/kv.kvx"] = FLAT_KV_CONTRACT
    a["lib/kv.py"] = FLAT_KV_LIB
    a.update(flat_kv_fixtures())
    del a["fixtures/kv/migration_0_2_0.kvx"]
    del a["migrations/0_2_0_kv_namespaces.py"]
    replace(
        a,
        "manual/intro.md",
        "a namespaced key/value store with a\nquota per namespace (kv)",
        "a key/value store with a\n64 KiB quota (kv)",
    )
    replace(
        a,
        "manual/examples.md",
        'kv.set(ns="receipts", key="golf-1", value=receipt["id"].encode())',
        'kv.set(key="receipt-golf-1", value=receipt["id"].encode())',
    )
    replace(
        a,
        "manual/examples.md",
        'kv.set(ns="jobs", key="render", value=job_id.encode())',
        'kv.set(key="job-render", value=job_id.encode())',
    )
    replace(
        a,
        "manual/examples.md",
        'kv.get(ns="jobs", key="render").decode()',
        'kv.get(key="job-render").decode()',
    )
    replace(
        a,
        "manual/examples.md",
        'if kv.used(ns="cache") + len(value) <= 65536:\n    kv.set(ns="cache", key="item-1", value=value)\nreturn kv.used(ns="cache")',
        'if kv.used() + len(value) <= 65536:\n    kv.set(key="item-1", value=value)\nreturn kv.used()',
    )
    replace(
        a,
        "manual/examples.md",
        "The receipts namespace keeps reaching its 64 KiB quota.",
        "The kv store keeps reaching its 64 KiB quota.",
    )
    replace(
        a,
        "CHANGELOG.kvx",
        "0.2.0 carries three major-kind changes",
        "0.2.0 carries two major-kind changes",
    )
    replace(
        a,
        "CHANGELOG.kvx",
        "kv keys are namespaced, with a 64 KiB quota per namespace readable through kv.used.",
        "kv has a 64 KiB quota over the whole store, readable through kv.used.",
    )
    replace(
        a,
        "CHANGELOG.kvx",
        '"fs_write_size_limit", "kv_namespaces", "kv_used_added", "kv_set_quota"',
        '"fs_write_size_limit", "kv_used_added", "kv_set_quota"',
    )
    remove_section(a, "CHANGELOG.kvx", "[change.kv_namespaces]")
    replace(
        a,
        "CHANGELOG.kvx",
        "kv.used(ns) returns the total byte length of the values stored in namespace ns.",
        "kv.used() returns the total byte length of every value stored in kv.",
    )
    replace(
        a,
        "CHANGELOG.kvx",
        "kv.set additionally requires kv.used(ns) + len(value) <= 65536, otherwise LIMIT.",
        "kv.set additionally requires kv.used() + len(value) <= 65536, otherwise LIMIT.",
    )

    b = dict(base)
    b["CHANGELOG.kvx"] = a["CHANGELOG.kvx"]
    migration = "migrations/0_2_0_0_3_0_kv_namespaces.py"
    body = base["migrations/0_2_0_kv_namespaces.py"]
    body = body[body.index("def migrate") :]
    b[migration] = (
        '''# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""refworld migration 0.2.0 -> 0.3.0: kv becomes namespaced (upgrade pair U5, side B).

Declared by change kv_namespaces (kind state_shape) in CHANGELOG.kvx. A pure function from the
0.2.0 Instance state of U5/A to the 0.3.0 state of U5/B: it receives and returns plain data, never
mutates its argument, and has no access to the kernel, the clock, the RNG, adapters or the ledger.

0.2.0 kv state: {key: bytes}                      (one flat map)
0.3.0 kv state: {"namespaces": {ns: {key: bytes}}} (existing keys move to namespace "default")

Idempotent: a state already in the 0.3.0 shape is returned unchanged, so
migrate(migrate(s)) == migrate(s). The 0.3.0 shape is recognised structurally (exactly one key,
"namespaces", whose value is a map); a 0.2.0 flat key that happens to be named "namespaces" holds
bytes, not a map, and is migrated like any other key. Fixtures: fixtures/kv/migration_0_3_0.kvx.
The function body is byte-identical to examples/refworld/migrations/0_2_0_kv_namespaces.py.
"""


'''
        + body
    )
    del b["migrations/0_2_0_kv_namespaces.py"]
    fixture = base["fixtures/kv/migration_0_2_0.kvx"]
    del b["fixtures/kv/migration_0_2_0.kvx"]
    fixture = fixture.replace("migrations/0_2_0_kv_namespaces.py", migration)
    fixture = fixture.replace(
        "# 0.1.0 Instance state (before) and the required 0.2.0 state (after).",
        "# 0.2.0 Instance state of U5/A (before) and the required 0.3.0 state of U5/B (after).",
    )
    b["fixtures/kv/migration_0_3_0.kvx"] = fixture
    replace(b, "lib/kv.py", "(0.2.0: namespaced)", "(0.3.0: namespaced)")
    replace(
        b,
        "lib/kv.py",
        "State shape (changed in 0.2.0 by migrations/0_2_0_kv_namespaces.py):",
        f"State shape (changed in 0.3.0 by {migration}):",
    )
    set_version(b, "0.3.0")
    add_release(
        b,
        "0.3.0",
        "kv is namespaced: every kv primitive takes ns first and the 64 KiB quota applies per namespace; existing keys moved to namespace default.",
        [
            (
                "kv_namespaces",
                {
                    "kind": "state_shape",
                    "target": "kv",
                    "before": "kv state is one flat map of key to value and kv primitives take no namespace.",
                    "after": "kv state is a map of namespace to map of key to value and every kv primitive takes ns first; existing keys moved to namespace default.",
                    "migration": migration,
                },
            ),
            (
                "kv_get_ns",
                {
                    "kind": "precondition_changed",
                    "target": "kv.get",
                    "before": "kv.has(key), otherwise NOT_FOUND.",
                    "after": "kv.has(ns, key), otherwise NOT_FOUND.",
                },
            ),
            (
                "kv_set_ns",
                {
                    "kind": "precondition_changed",
                    "target": "kv.set",
                    "before": "kv.used() + len(value) <= 65536 over the whole store, otherwise LIMIT (precondition 2).",
                    "after": "kv.used(ns) + len(value) <= 65536 within namespace ns, otherwise LIMIT (precondition 2).",
                },
            ),
            (
                "kv_set_ns_effects",
                {
                    "kind": "effect_changed",
                    "target": "kv.set",
                    "before": "Ensures kv.has(key) and kv.get(key) == value.",
                    "after": "Ensures kv.has(ns, key) and kv.get(ns, key) == value.",
                },
            ),
            (
                "kv_del_ns",
                {
                    "kind": "precondition_changed",
                    "target": "kv.del",
                    "before": "kv.has(key), otherwise NOT_FOUND.",
                    "after": "kv.has(ns, key), otherwise NOT_FOUND.",
                },
            ),
            (
                "kv_del_ns_effects",
                {
                    "kind": "effect_changed",
                    "target": "kv.del",
                    "before": "Ensures not kv.has(key).",
                    "after": "Ensures not kv.has(ns, key).",
                },
            ),
        ],
    )
    return a, b


# ------------------------------------------------------------------------------------------- U6


def u6(base: dict) -> tuple:
    a = dict(base)
    b = dict(base)
    replace(
        b,
        "contracts/fs.kvx",
        'doc = "Create or replace the file at path. Refused while the path is locked or the content exceeds 1 MiB."',
        'doc = "Create the file at path, or replace its whole content when it already exists; nothing is appended or merged. Refused while the path is locked or the content exceeds 1 MiB."',
    )
    set_version(b, "0.2.1")
    add_release(
        b,
        "0.2.1",
        "Documentation only: the fs.write description now states that it replaces the whole file; no rule changed.",
        [
            (
                "fs_write_doc_replace",
                {
                    "kind": "doc_only",
                    "target": "fs.write",
                    "before": "The fs.write description said create or replace the file at path.",
                    "after": "The fs.write description says the whole content is replaced and nothing is appended or merged.",
                },
            ),
        ],
    )
    return a, b


BUILDERS = {"U1": u1, "U2": u2, "U3": u3, "U4": u4, "U5": u5, "U6": u6}

# Files in which A and B are expected to differ (authoring guard against accidental drift).
EXPECTED_DIFF_FILES = {
    "U1": {
        "world.kvx",
        "CHANGELOG.kvx",
        "contracts/fs.kvx",
        "lib/fs.py",
        "fixtures/fs/delete.kvx",
        "manual/conventions.md",
        "manual/examples.md",
    },
    "U2": {"world.kvx", "CHANGELOG.kvx", "contracts/fs.kvx", "lib/fs.py", "fixtures/fs/move.kvx"},
    "U3": {
        "world.kvx",
        "CHANGELOG.kvx",
        "contracts/jobs.kvx",
        "fixtures/jobs/result.kvx",
        "manual/conventions.md",
    },
    "U4": {
        "world.kvx",
        "CHANGELOG.kvx",
        "contracts/kv.kvx",
        "fixtures/kv/set.kvx",
        "manual/examples.md",
    },
    "U5": {
        "world.kvx",
        "CHANGELOG.kvx",
        "contracts/kv.kvx",
        "lib/kv.py",
        "manual/intro.md",
        "manual/examples.md",
        "fixtures/kv/has.kvx",
        "fixtures/kv/get.kvx",
        "fixtures/kv/keys.kvx",
        "fixtures/kv/used.kvx",
        "fixtures/kv/set.kvx",
        "fixtures/kv/del.kvx",
        "fixtures/kv/migration_0_3_0.kvx",
        "migrations/0_2_0_0_3_0_kv_namespaces.py",
    },
    "U6": {"world.kvx", "CHANGELOG.kvx", "contracts/fs.kvx"},
}


def generate() -> dict:
    base = read_base()
    return {pair: BUILDERS[pair](base) for pair in PAIRS}


def write(generated: dict) -> None:
    for pair, sides in generated.items():
        for label, files in zip("AB", sides, strict=True):
            root = HERE / pair / label
            if root.exists():
                shutil.rmtree(root)
            for rel, text in files.items():
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")


def on_disk(root: Path) -> dict:
    return {
        str(p.relative_to(root)): p.read_text(encoding="utf-8")
        for p in sorted(root.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts
    }


# =================================================================================================
# Loading a side
# =================================================================================================

_module_counter = [0]


def load_py(path: Path):
    _module_counter[0] += 1
    spec = importlib.util.spec_from_file_location(f"_pairs_mod_{_module_counter[0]}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Side:
    def __init__(self, root: Path):
        self.root = root
        self.world = parse_kvx((root / "world.kvx").read_text())
        self.changelog = parse_kvx((root / "CHANGELOG.kvx").read_text())
        self.version = self.world["world"]["version"]
        self.modules = list(self.world["world"]["modules"])
        self.contracts, self.adapters, self.lib = {}, {}, {}
        for path in sorted((root / "contracts").glob("*.kvx")):
            doc = parse_kvx(path.read_text())
            for section, block in doc.items():
                if section.startswith("primitive."):
                    self.contracts[section[len("primitive.") :]] = block
        for path in sorted((root / "adapters").glob("*.kvx")):
            doc = parse_kvx(path.read_text())
            for section, block in doc.items():
                if section.startswith("adapter."):
                    self.adapters[section[len("adapter.") :]] = block
        for path in sorted((root / "lib").glob("*.py")):
            self.lib[path.stem] = load_py(path)
        self.lib_source = {p.stem: p.read_text() for p in sorted((root / "lib").glob("*.py"))}

    def newest_release(self) -> tuple:
        releases = [v for k, v in self.changelog.items() if k.startswith("release.")]
        newest = max(releases, key=lambda r: validate_spec.numeric(r["version"]))
        return newest, [(c, self.changelog[f"change.{c}"]) for c in newest["changes"]]

    def lib_function(self, name: str):
        module, prim = name.split(".", 1)
        attr = prim + "_" if prim in ("del",) else prim
        return getattr(self.lib[module], attr)

    def initial_state(self) -> dict:
        return {m: copy.deepcopy(self.lib[m].INITIAL_STATE) for m in self.lib}


# =================================================================================================
# Stand-in kernel (authoring model)
# =================================================================================================


class WorldError(Exception):
    def __init__(self, code, primitive, precondition_index=None, detail=""):
        super().__init__(f"{code} on {primitive}: {detail}")
        self.code, self.primitive = code, primitive
        self.precondition_index, self.detail, self.retry_after = precondition_index, detail, None


class Handle:
    def __init__(self, state: dict, time: int):
        self.state, self.time = state, time

    def module(self, name):
        return self.state[name]

    def world_time(self):
        return self.time


TYPES = {"Path": str, "str": str, "int": int, "bytes": bytes, "bool": bool}


def decode_value(value, typ):
    if isinstance(value, dict) and "$repeat" in value:
        text, count = value["$repeat"]
        value = text * count
    if typ == "bytes" and isinstance(value, str):
        return value.encode("utf-8")
    return value


def decode_state(state: dict) -> dict:
    """Fixture/initial state codec into lib state: fs files and kv values become bytes."""
    out = copy.deepcopy(state)
    fs = out.get("fs")
    if fs is not None:
        fs["files"] = {p: decode_value(c, "bytes") for p, c in fs.get("files", {}).items()}
        fs.setdefault("locks", [])
    kv = out.get("kv")
    if kv is not None:
        if set(kv) == {"namespaces"} and isinstance(kv["namespaces"], dict):
            kv["namespaces"] = {
                ns: {k: decode_value(v, "bytes") for k, v in keys.items()}
                for ns, keys in kv["namespaces"].items()
            }
        else:
            out["kv"] = {k: decode_value(v, "bytes") for k, v in kv.items()}
    return out


class Kernel:
    def __init__(self, side: Side, state: dict, time: int = 0):
        self.side, self.time, self.turn = side, time, 0
        self.state = side.initial_state()
        self.state.update(state)
        self.ledger = [
            {
                "seq": 0,
                "kind": "header",
                "turn": 0,
                "world_time": 0,
                "primitive": None,
                "build": side.version,
                "outcome": None,
            }
        ]

    def _append(self, record):
        record.update(
            {
                "seq": len(self.ledger),
                "turn": self.turn,
                "world_time": self.time,
                "build": self.side.version,
            }
        )
        self.ledger.append(record)

    def _env(self, params, result=None, with_result=False):
        kernel = self

        class Proxy:
            def __init__(self, module):
                self.module = module

            def __getattr__(self, attr):
                name = f"{self.module}.{'del' if attr == 'del_' else attr}"
                contract = kernel.side.contracts.get(name)
                if contract is None or contract["side_effect"] != "read_only":
                    raise NameError(f"{name} is not a read_only primitive of this Build")
                function = kernel.side.lib_function(name)
                return lambda *args: function(Handle(kernel.state, kernel.time), *args)

        env = {m: Proxy(m) for m in self.side.modules}
        env.update(params)
        env["len"] = len
        if with_result:
            env["result"] = result
        return env

    def call(self, name, kwargs, record=True):
        contract = self.side.contracts.get(name)
        if contract is None:
            if name in self.side.adapters:
                raise WorldError(
                    "UNAVAILABLE", name, None, "adapters are outside the authoring model"
                )
            raise WorldError(
                "INVALID", name, None, "closure refusal: not enumerated by the Build SDK"
            )
        declared = contract["params"]
        params = {}
        if set(kwargs) != set(declared):
            self._record(name, contract, [], "error", "INVALID", record)
            raise WorldError(
                "INVALID", name, None, f"parameters {sorted(kwargs)} != {list(declared)}"
            )
        for key, typ in declared.items():
            value = kwargs[key]
            want = TYPES[typ]
            if not isinstance(value, want) or (want is int and isinstance(value, bool)):
                self._record(name, contract, [], "error", "INVALID", record)
                raise WorldError("INVALID", name, None, f"{key} must be {typ}")
            params[key] = value
        evaluated = []
        # Contract expressions come from the side's own contracts and run without builtins.
        for index, (expr, code) in enumerate(contract["preconditions"], 1):
            passed = bool(eval(expr, {"__builtins__": {}}, self._env(params)))
            evaluated.append({"index": index, "expr": expr, "passed": passed})
            if not passed:
                self._record(name, contract, evaluated, "error", code, record)
                raise WorldError(code, name, index, f"{expr} was false")
        before = copy.deepcopy(self.state)
        result = self.side.lib_function(name)(Handle(self.state, self.time), *params.values())
        if contract["side_effect"] == "read_only" and self.state != before:
            raise AssertionError(f"{name}: read_only primitive changed state")
        for effect in contract["effects"]:
            if not eval(effect, {"__builtins__": {}}, self._env(params, result, True)):
                raise AssertionError(
                    f"{name}: effect {effect!r} does not hold (INVARIANT_VIOLATION)"
                )
        self.time += self.side.world["clock"]["tick_per_primitive"]
        self._record(name, contract, evaluated, "ok", None, record)
        return result

    def _record(self, name, contract, evaluated, status, code, record):
        if record:
            self._append(
                {
                    "kind": "primitive",
                    "primitive": name,
                    "preconditions": evaluated,
                    "outcome": {
                        "status": status,
                        "code": code,
                        "shape": contract["returns"],
                        "side_effect": contract["side_effect"],
                    },
                }
            )

    def run_turn(self, script: str) -> dict:
        self.turn += 1
        kernel = self
        mutated = [False]

        class Module:
            def __init__(self, module):
                object.__setattr__(self, "_module", module)

            def __getattr__(self, attr):
                name = f"{self._module}.{'del' if attr == 'del_' else attr}"
                contracts = kernel.side.contracts
                if name not in contracts and name not in kernel.side.adapters:
                    # The generated SDK has no such function: the script fails in the sandbox
                    # before any RPC, as the kernel reports it (turn.script, no call record).
                    raise WorldError(
                        "INVALID",
                        "turn.script",
                        None,
                        f"uncaught exception: AttributeError: module '{self._module}' has no "
                        f"attribute '{attr}'",
                    )

                def invoke(*args, **kwargs):
                    if args:
                        raise WorldError("INVALID", name, None, "keyword arguments only")
                    declared = list((contracts.get(name) or {}).get("params") or {})
                    missing = [p for p in declared if p not in kwargs]
                    unexpected = [k for k in kwargs if k not in declared]
                    if name in contracts and (missing or unexpected):
                        # The SDK function signature refuses the call in the sandbox, as in Python.
                        function = attr
                        if unexpected:
                            why = (
                                f"{function}() got an unexpected keyword argument '{unexpected[0]}'"
                            )
                        else:
                            noun = "argument" if len(missing) == 1 else "arguments"
                            names = " and ".join(f"'{p}'" for p in missing)
                            why = f"{function}() missing {len(missing)} required positional {noun}: {names}"
                        raise WorldError(
                            "INVALID", "turn.script", None, f"uncaught exception: TypeError: {why}"
                        )
                    value = kernel.call(name, kwargs)
                    if kernel.side.contracts[name]["side_effect"] != "read_only":
                        mutated[0] = True
                    return value

                return invoke

        modules = {m: Module(m) for m in self.side.modules}

        def only_sdk(name, globals_=None, locals_=None, fromlist=(), level=0):
            if name in modules and level == 0:
                return modules[name]
            raise ImportError(f"{name} is not part of the Build SDK")

        import builtins

        safe = {
            k: getattr(builtins, k)
            for k in dir(builtins)
            if k
            not in (
                "open",
                "exec",
                "eval",
                "compile",
                "input",
                "breakpoint",
                "exit",
                "quit",
                "help",
                "__import__",
                "globals",
                "locals",
                "vars",
            )
        }
        safe["__import__"] = only_sdk
        body = (
            "def __turn__():\n"
            + textwrap.indent(textwrap.dedent(script).strip("\n"), "    ")
            + "\n"
        )
        namespace = {"__builtins__": safe}
        error, returned = None, None
        try:
            # The pair's own reference turn script, with SDK-only imports and reduced builtins.
            exec(compile(body, f"<turn {self.turn}>", "exec"), namespace)
            returned = namespace["__turn__"]()
        except WorldError as failure:
            error = {
                "code": failure.code,
                "primitive": failure.primitive,
                "precondition_index": failure.precondition_index,
                "detail": failure.detail,
            }
        status = "ok" if error is None else ("partial" if mutated[0] else "error")
        self.time += self.side.world["clock"]["tick_per_turn"]
        self._append(
            {
                "kind": "turn",
                "primitive": None,
                "status": status,
                "outcome": {"status": status, "code": error and error["code"]},
            }
        )
        return {"status": status, "returned": returned, "error": error}

    def upgrade(self, target: Side) -> list:
        release, changes = target.newest_release()
        applied = []
        state = copy.deepcopy(self.state)
        for _, change in changes:
            if change["kind"] == "state_shape":
                migrate = load_py(target.root / change["migration"]).migrate
                state = migrate(state)
                applied.append(change["migration"])
        self.state, self.side = state, target
        self._append(
            {
                "kind": "upgrade",
                "primitive": None,
                "outcome": None,
                "migrations": applied,
                "to_version": release["version"],
            }
        )
        return applied

    def used_primitives(self) -> set:
        return {r["primitive"] for r in self.ledger if r["kind"] == "primitive"}

    def state_view(self) -> dict:
        def text(value):
            return value.decode("utf-8") if isinstance(value, bytes) else value

        fs = self.state["fs"]
        kv = self.state["kv"]
        if set(kv) == {"namespaces"}:
            namespaces = {
                ns: {k: text(v) for k, v in keys.items()} for ns, keys in kv["namespaces"].items()
            }
        else:
            namespaces = {"(flat)": {k: text(v) for k, v in kv.items()}}
        jobs_lib = self.side.lib["jobs"]
        jobs = {}
        for job_id, job in sorted(self.state["jobs"]["jobs"].items()):
            status = jobs_lib._status_of(job, self.time)
            view = {
                "kind": job["kind"],
                "n": job["n"],
                "submitted_at": job["submitted"],
                "status": status,
            }
            if status == "done":
                view["result"] = jobs_lib.result(Handle(self.state, self.time), job_id)
            jobs[job_id] = view
        return {
            "fs": {
                "files": {p: text(c) for p, c in sorted(fs["files"].items())},
                "locks": sorted(fs["locks"]),
            },
            "kv": {"namespaces": namespaces},
            "jobs": {"jobs": jobs},
            "world": {"time": self.time, "turn": self.turn},
            "gateway": {"idempotency": {}},
        }


# =================================================================================================
# Checks
# =================================================================================================


def structural_errors(root: Path) -> list:
    """validate_spec.example_errors pointed at a side, example-status rule excepted."""
    with tempfile.TemporaryDirectory() as tmp:
        pack = Path(tmp)
        (pack / "examples").mkdir()
        (pack / "examples" / "refworld").symlink_to(root, target_is_directory=True)
        saved = validate_spec.PACK
        validate_spec.PACK = pack
        try:
            errors, _ = validate_spec.example_errors()
        finally:
            validate_spec.PACK = saved
    return [e for e in errors if "example status" not in e]


def contract_test_errors(side: Side) -> list:
    errors = []
    for module in ("fs", "kv", "jobs"):
        for path in sorted((side.root / "fixtures" / module).glob("*.kvx")):
            doc = parse_kvx(path.read_text())
            meta = doc["meta"]
            rel = path.relative_to(side.root)
            if meta["format"] == "cwr.migration-fixture":
                migrate = load_py(side.root / meta["migration"]).migrate
                for name, case in doc.items():
                    if name.startswith("migration."):
                        before, after = decode_state(case["before"]), decode_state(case["after"])
                        if (
                            migrate(copy.deepcopy(before)) != after
                            or migrate(copy.deepcopy(after)) != after
                        ):
                            errors.append(
                                f"{rel} {name}: migrate(before) != after or not idempotent"
                            )
                continue
            primitive = meta["primitive"]
            if primitive not in side.contracts:
                errors.append(f"{rel}: fixture for undeclared primitive {primitive}")
                continue
            contract = side.contracts[primitive]
            reached = set()
            for name, case in doc.items():
                if not name.startswith("case."):
                    continue
                kernel = Kernel(side, decode_state(case["state"]), case["world_time"])
                params = {
                    k: decode_value(v, contract["params"].get(k)) for k, v in case["params"].items()
                }
                before = copy.deepcopy(kernel.state)
                try:
                    returned = kernel.call(primitive, params, record=False)
                    outcome, index = "ok", None
                except WorldError as failure:
                    outcome, index, returned = failure.code, failure.precondition_index, None
                    if kernel.state != before:
                        errors.append(f"{rel} {name}: state changed after a refused call")
                except AssertionError as failure:
                    errors.append(f"{rel} {name}: {failure}")
                    continue
                if outcome != case["expect"] or index != case.get("precondition"):
                    errors.append(
                        f"{rel} {name}: got {outcome}/{index}, fixture expects "
                        f"{case['expect']}/{case.get('precondition')}"
                    )
                if "returned" in case and outcome == "ok":
                    want = decode_value(case["returned"], contract["returns"])
                    if returned != want:
                        errors.append(
                            f"{rel} {name}: returned {returned!r}, fixture expects {want!r}"
                        )
                if index is not None:
                    reached.add((index, outcome))
            for index, (_, code) in enumerate(contract["preconditions"], 1):
                if (index, code) not in reached:
                    errors.append(
                        f"{rel}: declared failure {code} of precondition {index} has no fixture"
                    )
    for name, contract in side.contracts.items():
        module, prim = name.split(".", 1)
        if (
            module in ("fs", "kv", "jobs")
            and not (side.root / "fixtures" / module / f"{prim}.kvx").is_file()
        ):
            errors.append(f"{name}: no fixture file")
        try:
            function = side.lib_function(name)
        except AttributeError:
            errors.append(f"{name}: contract without lib function (CWR-L002)")
            continue
        code = function.__code__
        if list(code.co_varnames[1 : code.co_argcount]) != list(contract["params"]):
            errors.append(f"{name}: lib parameter list differs from contract (CWR-L002)")
    for module, lib in side.lib.items():
        for attr, value in vars(lib).items():
            if (
                inspect.isfunction(value)
                and not attr.startswith("_")
                and value.__module__ == lib.__name__
            ):
                name = f"{module}.{'del' if attr == 'del_' else attr}"
                if name not in side.contracts:
                    errors.append(
                        f"lib/{module}.py: public function {attr} without a contract (CWR-L002)"
                    )
    return errors


BUMP = {
    "primitive_added": "minor",
    "primitive_removed": "major",
    "precondition_added": "major",
    "precondition_removed": "minor",
    "precondition_changed": "major",
    "effect_changed": "major",
    "failure_changed": "major",
    "state_shape": "major",
    "adapter_changed": "major",
    "doc_only": "patch",
    "params": "major",
}
ORDER = ["none", "patch", "minor", "major"]


def state_record(source: str) -> str:
    found = re.findall(
        r"^(class \w+State\(TypedDict\):\n(?:    .*\n)+|\w+State = .*\n)", source, re.M
    )
    return "".join(found)


def contract_diff(a: Side, b: Side) -> tuple:
    """Return (entries, doc_entries) in the layout of spec/cwr/compiler.md section 5."""
    entries, docs = [], []
    for name in sorted(set(a.contracts) | set(a.adapters) | set(b.contracts) | set(b.adapters)):
        old = a.contracts.get(name) or a.adapters.get(name)
        new = b.contracts.get(name) or b.adapters.get(name)
        if old is None:
            entries.append({"kind": "primitive_added", "target": name, "field": "primitive"})
            continue
        if new is None:
            entries.append({"kind": "primitive_removed", "target": name, "field": "primitive"})
            continue
        for field in ("side_effect", "returns"):
            if old.get(field) != new.get(field):
                entries.append(
                    {
                        "kind": "precondition_changed",
                        "target": name,
                        "field": field,
                        "before": old.get(field),
                        "after": new.get(field),
                    }
                )
        if old["params"] != new["params"]:
            entries.append(
                {
                    "kind": None,
                    "target": name,
                    "field": "params",
                    "before": old["params"],
                    "after": new["params"],
                }
            )
        po, pn = old["preconditions"], new["preconditions"]
        if po != pn:
            if pn[: len(po)] == po:
                kind = "precondition_added"
            elif po[: len(pn)] == pn:
                kind = "precondition_removed"
            else:
                kind = "precondition_changed"
            entries.append(
                {"kind": kind, "target": name, "field": "preconditions", "before": po, "after": pn}
            )
        if old.get("effects") != new.get("effects"):
            entries.append(
                {
                    "kind": "effect_changed",
                    "target": name,
                    "field": "effects",
                    "before": old.get("effects"),
                    "after": new.get("effects"),
                }
            )
        for field in ("idempotency", "timeout_ms", "endpoint"):
            if old.get(field) != new.get(field):
                entries.append({"kind": "adapter_changed", "target": name, "field": field})
        if old.get("failures") != new.get("failures"):
            entries.append({"kind": "failure_changed", "target": name, "field": "failures"})
        if old.get("doc") != new.get("doc"):
            docs.append(
                {
                    "kind": "doc_only",
                    "target": name,
                    "field": "doc",
                    "before": old["doc"],
                    "after": new["doc"],
                }
            )
    for section in ("limits", "clock", "rng"):
        for key in sorted(set(a.world[section]) | set(b.world[section])):
            if a.world[section].get(key) != b.world[section].get(key):
                entries.append(
                    {"kind": "limit_changed", "target": f"{section}.{key}", "field": "value"}
                )
    for module in sorted(set(a.lib_source) | set(b.lib_source)):
        sa, sb = (
            state_record(a.lib_source.get(module, "")),
            state_record(b.lib_source.get(module, "")),
        )
        if sa != sb:
            entries.append({"kind": "state_shape", "target": module, "field": "state"})
    entries.sort(key=lambda e: (e["target"], e["field"]))
    return entries, docs


def literal_bump(old: str, new: str) -> str:
    o, n = validate_spec.numeric(old), validate_spec.numeric(new)
    if n <= o:
        return "none"
    return "major" if n[0] > o[0] else "minor" if n[1] > o[1] else "patch"


def satisfies(old: str, literal: str, derived: str) -> tuple:
    """(satisfied, pre_1_0_rule_applied) per compiler.md section 6 step 2 and decision P15."""
    if literal == "none":
        return False, False
    if ORDER.index(literal) >= ORDER.index(derived):
        return True, False
    if validate_spec.numeric(old)[0] == 0 and (literal, derived) in (
        ("minor", "major"),
        ("patch", "minor"),
    ):
        return True, True
    return False, False


def gate_errors(a: Side, b: Side, entries: list, docs: list) -> tuple:
    """Changelog gate of compiler.md section 6 with the coverage interpretations of upgrades.kvx."""
    errors = []
    release, changes = b.newest_release()
    if release["version"] != b.version:
        errors.append(f"newest release {release['version']} != world.version {b.version}")
    derived = max(
        (BUMP[e["kind"] or "params"] for e in entries + docs), key=ORDER.index, default="none"
    )
    bump = literal_bump(a.version, b.version)
    satisfied, pre10 = satisfies(a.version, bump, derived)
    if not satisfied:
        errors.append(
            f"declared {a.version} -> {b.version} ({bump}) is below the derived bump {derived}"
        )
    targets = {change["target"] for _, change in changes}
    state_modules = {change["target"] for _, change in changes if change["kind"] == "state_shape"}
    for entry in entries:
        if entry["kind"] is None:
            if entry["target"].split(".")[0] not in state_modules:
                errors.append(f"uncovered params entry {entry['target']} (no change kind exists)")
            continue
        if not any(
            c["kind"] == entry["kind"] and c["target"] == entry["target"] for _, c in changes
        ):
            errors.append(f"uncovered diff entry {entry['kind']} {entry['target']}")
    for entry in docs:
        if entry["target"] not in targets and entry["target"].split(".")[0] not in state_modules:
            errors.append(f"uncovered doc entry {entry['target']}")
    for ident, change in changes:
        pool = docs if change["kind"] == "doc_only" else entries
        if not any(e["kind"] == change["kind"] and e["target"] == change["target"] for e in pool):
            errors.append(
                f"change {ident} ({change['kind']} {change['target']}) matches no diff entry"
            )
        if change["kind"] == "state_shape" and not (b.root / change["migration"]).is_file():
            errors.append(f"change {ident}: migration {change['migration']} missing (CWR-M001)")
    return errors, derived, bump, pre10


VERB = {"primitive_removed": "removed", "primitive_added": "added"}
LABEL = {"state_shape": " (state shape)", "doc_only": " (documentation)"}


def affects(target: str, used: set) -> bool:
    return target in used or any(u.split(".")[0] == target for u in used if "." not in target)


def render_notice(b: Side, old_version: str, used: set) -> tuple:
    release, changes = b.newest_release()
    lines = [
        f"WORLD RECOMPILED  {old_version} -> {b.version}   ({{hash_a}} -> {{hash_b}})",
        f"Summary: {release['summary']}",
        "",
        "Changes affecting primitives this instance has used:",
    ]
    listed, migrated = [], False
    for _, change in changes:
        migrated = migrated or change["kind"] == "state_shape"
        if not affects(change["target"], used):
            continue
        listed.append(change["target"])
        lines.append(
            f"  {VERB.get(change['kind'], 'changed')}  {change['target']}{LABEL.get(change['kind'], '')}"
        )
        lines.append(f"    before  {change['before']}")
        lines.append(f"    after   {change['after']}")
    if not listed:
        lines.append("  (none)")
    lines.append("")
    tail = f"Your ledger is intact. The manual above describes {b.version}."
    lines.append(("State was migrated. " if migrated else "") + tail)
    return "\n".join(lines), listed


def load_state_file(path: Path) -> dict:
    doc = parse_kvx(path.read_text())
    doc.pop("meta", None)
    return doc


def play(pair: str, a: Side, b: Side, frozen: dict, second_half: list) -> dict:
    task = HERE / pair / "task"
    kernel = Kernel(a, decode_state(load_state_file(task / "initial.kvx")))
    envelopes = []
    for script in frozen["first_half"]:
        envelopes.append(kernel.run_turn((HERE / pair / script).read_text()))
    used = kernel.used_primitives()
    kernel.upgrade(b)
    for script in second_half:
        envelopes.append(kernel.run_turn((HERE / pair / script).read_text()))
    return {"kernel": kernel, "envelopes": envelopes, "used": used}


def check(generated: dict) -> list:
    errors = []
    sys.path.insert(0, str(PREDICATES))
    import common

    upgrades = parse_kvx(UPGRADES.read_text())
    base = read_base()
    for pair in PAIRS:
        a_files, b_files = generated[pair]
        for label, files in zip("AB", (a_files, b_files), strict=True):
            disk = on_disk(HERE / pair / label)
            if disk != files:
                stale = sorted(set(disk) ^ set(files)) + sorted(
                    k for k in set(disk) & set(files) if disk[k] != files[k]
                )
                errors.append(
                    f"{pair}/{label}: differs from regeneration ({stale[:5]}); run with --write"
                )
        changed = {k for k in set(a_files) | set(b_files) if a_files.get(k) != b_files.get(k)}
        # Every version bump also moves the world.version fixture (set_version).
        if changed != EXPECTED_DIFF_FILES[pair] | {"fixtures/world/version.kvx"}:
            errors.append(
                f"{pair}: A and B differ in {sorted(changed)}, expected {sorted(EXPECTED_DIFF_FILES[pair])}"
            )
        meta = upgrades[f"pair.{pair}"]
        if meta["a_equals_base"] and a_files != base:
            errors.append(f"{pair}/A must equal examples/refworld byte for byte")
        sides = []
        for label in "AB":
            root = HERE / pair / label
            for problem in structural_errors(root):
                errors.append(f"{pair}/{label} structure: {problem}")
            side = Side(root)
            for problem in contract_test_errors(side):
                errors.append(f"{pair}/{label} contract tests: {problem}")
            sides.append(side)
        a, b = sides
        if (a.version, b.version) != (meta["from_version"], meta["to_version"]):
            errors.append(f"{pair}: versions {a.version}->{b.version} differ from frozen")
        entries, docs = contract_diff(a, b)
        problems, derived, bump, pre10 = gate_errors(a, b, entries, docs)
        errors += [f"{pair} gate: {p}" for p in problems]
        diff = upgrades[f"diff.{pair}"]
        if entries != diff["entries"]:
            errors.append(
                f"{pair}: recomputed diff entries differ from frozen:\n  got    {json.dumps(entries)}\n  frozen {json.dumps(diff['entries'])}"
            )
        if docs != diff["doc_entries"]:
            errors.append(
                f"{pair}: recomputed doc entries differ from frozen:\n  got    {json.dumps(docs)}\n  frozen {json.dumps(diff['doc_entries'])}"
            )
        if (derived, bump, pre10) != (
            diff["derived_bump"],
            diff["declared_bump"],
            diff["pre_1_0_rule_applied"],
        ):
            errors.append(f"{pair}: bump {(derived, bump, pre10)} differs from frozen")
        release, changes = b.newest_release()
        if [c for c, _ in changes] != meta["changes"] or [c["kind"] for _, c in changes] != meta[
            "change_kinds"
        ]:
            errors.append(f"{pair}: changelog changes differ from frozen")
        if [c["migration"] for _, c in changes if c["migration"]] != meta["migrations"]:
            errors.append(f"{pair}: migrations differ from frozen")

        task = upgrades[f"task.{pair}"]
        predicate = common.load_predicate(HERE / pair / "task")
        post = play(pair, a, b, task, task["post_upgrade"])
        notice = upgrades[f"notice.{pair}"]
        text, listed = render_notice(b, a.version, post["used"])
        if sorted(post["used"]) != notice["used_primitives"]:
            errors.append(
                f"{pair}: first half used {sorted(post['used'])}, frozen {notice['used_primitives']}"
            )
        if listed != notice["listed_targets"] or text != notice["text"]:
            errors.append(
                f"{pair}: rendered notice differs from frozen:\n{text}\n---frozen---\n{notice['text']}"
            )
        if notice["version_only_text"] != text.split("\n")[0] or notice["silent_text"] != "":
            errors.append(f"{pair}: experimental renderings differ from the full notice header")
        if notice["summary"] != release["summary"]:
            errors.append(f"{pair}: notice summary differs from the changelog")
        count = sum(1 for record in post["kernel"].ledger if record["kind"] == "primitive")
        bin_name = (HERE / pair / "task" / "bin").read_text().strip()
        expected_bin = "short" if count <= 5 else "medium" if count <= 25 else "long"
        if (
            count != task["reference_primitive_count"]
            or bin_name != expected_bin
            or task["bin"] != bin_name
        ):
            errors.append(
                f"{pair}: reference primitive count {count} (bin {expected_bin}) differs from "
                f"frozen {task['reference_primitive_count']} / bin file {bin_name} / frozen bin {task['bin']}"
            )
        for env in post["envelopes"]:
            if env["status"] != "ok":
                errors.append(f"{pair}: reference turn ended {env['status']}: {env['error']}")
        view = post["kernel"].state_view()
        if not common.evaluate_twice(predicate, view, post["kernel"].ledger):
            errors.append(f"{pair}: post-upgrade reference solution does not satisfy the predicate")
        initial_kernel = Kernel(
            a, decode_state(load_state_file(HERE / pair / "task" / "initial.kvx"))
        )
        if common.evaluate_twice(predicate, initial_kernel.state_view(), initial_kernel.ledger):
            errors.append(f"{pair}: predicate is true on the untouched initial state")
        for variant, scripts, expect_key in (
            ("pre", task["pre_upgrade"], "pre_failure"),
            ("pre_adapted", task.get("pre_upgrade_adapted", []), "pre_adapted_failure"),
        ):
            if not scripts:
                continue
            run = play(pair, a, b, task, scripts)
            failures = [
                e["error"] for e in run["envelopes"][len(task["first_half"]) :] if e["error"]
            ]
            expected = task[expect_key]
            ok = common.evaluate_twice(predicate, run["kernel"].state_view(), run["kernel"].ledger)
            if expected is None:
                if failures or not ok:
                    errors.append(
                        f"{pair}: {variant} solution should succeed unchanged; failures {failures}, success {ok}"
                    )
                continue
            first = failures[0] if failures else None
            got = first and {
                "primitive": first["primitive"],
                "code": first["code"],
                "precondition": first["precondition_index"],
            }
            frozen = {k: v for k, v in expected.items() if k != "detail_contains"}
            detail = (first or {}).get("detail") or ""
            if got != frozen or expected.get("detail_contains", "") not in detail:
                errors.append(
                    f"{pair}: {variant} solution first failure {got} ({detail}), frozen {expected}"
                )
            if ok:
                errors.append(f"{pair}: {variant} solution unexpectedly satisfies the predicate")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--write", action="store_true", help="regenerate UN/A and UN/B before checking"
    )
    args = parser.parse_args()
    generated = generate()
    if args.write:
        write(generated)
    errors = check(generated)
    for error in errors:
        print(f"ERROR {error}")
    print(
        f"{'FAIL' if errors else 'PASS'}: {len(PAIRS)} pairs, {len(errors)} errors "
        "(authoring check only; not CWR-011 or CWR-016 evidence)"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())

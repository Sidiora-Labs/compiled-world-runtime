#!/usr/bin/env python3
# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Regenerates tests/fixtures/malformed/<class>/ from tests/fixtures/valid/.

Each malformed class is the valid World Source with exactly one defect. A class directory
holds `case.json` and `files/`, an overlay of whole files that replace or add to the valid
tree; `case.json["remove"]` lists valid files to delete. `case.json["errors"]` is the exact
set of located errors the reader must report: file, one-based line and column, kind and
lint code. Expected locations are derived here from the fixture text and the location
rules of the reader's contract (value errors point at the value or list element, missing
keys at the section header, unknown keys at the key, whole-file errors at 1:1), never
from the reader's output. Python 3.12, standard library only.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
VALID = HERE / "valid"
OUT = HERE / "malformed"

# The repository source header (tools/header.py), by comment syntax. Files a case adds carry it
# like every other fixture file, so expected locations below already account for its two lines.
HEADER_LINES = (
    "Copyright Sidiora Labs Inc. CWR® 2026-2027",
    "SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0",
)
HEADER_STYLE = {".kvx": ("# ", ""), ".py": ("# ", ""), ".txt": ("# ", ""), ".md": ("<!-- ", " -->")}


def with_header(path: str, text: str) -> str:
    prefix, suffix = HEADER_STYLE[Path(path).suffix]
    header = "".join(f"{prefix}{line}{suffix}\n" for line in HEADER_LINES)
    return header + ("\n" if suffix else "") + text


LINT = {
    "unknown_file": "CWR-C001",
    "unknown_section": "CWR-C001",
    "unknown_key": "CWR-C001",
    "shape": "CWR-C001",
    "duplicate_primitive": "CWR-C002",
    "undeclared_module": "CWR-C003",
    "envelope": "CWR-F001",
    "adapter": "CWR-F002",
    "expression": "CWR-V002",
    "doc": "CWR-D001",
    "migration": "CWR-M001",
}

NEW_CONTRACT_JOBS = """[meta]
format = "cwr.contract"
schema_version = 1
module = "jobs"

[primitive.jobs.known]
side_effect = "read_only"
params = {"id": "str"}
returns = "bool"
preconditions = []
effects = []
doc = "True when the job id is known."
"""

NEW_ADAPTER_FS = """[meta]
format = "cwr.adapter"
schema_version = 1
module = "fs"

[adapter.fs.exists]
side_effect = "external"
params = {"path": "Path"}
returns = "bool"
preconditions = []
idempotency = "natural"
timeout_ms = 1000
failures = ["UNAVAILABLE", "TIMEOUT"]
content_guaranteed = false
endpoint = "stub://fs/v1/exists"
doc = "A second declaration of fs.exists."
"""

# name, clause, description, edits [(file, old, new)], adds {file: text}, remove [files],
# expected [(file, kind, line_contains or None, col_at or None)]
CASES = [
    # 3.1: unknown files, sections, keys; duplicates; non-JSON values
    (
        "unknown_file",
        "3.1",
        "A file the grammar does not define at the source root.",
        [],
        {"notes.txt": "scratch\n"},
        [],
        [("notes.txt", "unknown_file", None, None)],
    ),
    (
        "unknown_manual_file",
        "3.1",
        "A manual prose file outside the four named sections.",
        [],
        {"manual/extra.md": "# Extra\n"},
        [],
        [("manual/extra.md", "unknown_file", None, None)],
    ),
    (
        "unknown_section",
        "3.1",
        "world.kvx declares a section the grammar does not define.",
        [
            (
                "world.kvx",
                "[rng]\nseed_visible = true\n",
                "[rng]\nseed_visible = true\n\n[network]\nallow = true\n",
            )
        ],
        {},
        [],
        [("world.kvx", "unknown_section", "[network]", None)],
    ),
    (
        "unknown_key",
        "3.1",
        "A primitive declares a key the grammar does not define.",
        [
            (
                "contracts/fs.kvx",
                'doc = "Remove the file at path."\n',
                'doc = "Remove the file at path."\nretries = 3\n',
            )
        ],
        {},
        [],
        [("contracts/fs.kvx", "unknown_key", "retries = 3", "retries")],
    ),
    (
        "duplicate_key",
        "3.1",
        "A key repeated inside one section.",
        [("world.kvx", "adapter_calls = 50\n", "adapter_calls = 50\ncpu_ms = 6000\n")],
        {},
        [],
        [("world.kvx", "duplicate_key", "cpu_ms = 6000", "cpu_ms")],
    ),
    (
        "duplicate_release",
        "3.1",
        "Two release sections declare the same version (duplicate section).",
        [
            (
                "CHANGELOG.kvx",
                "[change.initial]",
                '[release.0.1.0]\nversion = "0.1.0"\n\n[change.initial]',
            )
        ],
        {},
        [],
        [("CHANGELOG.kvx", "duplicate_section", "@2[release.0.1.0]", None)],
    ),
    (
        "non_json_value",
        "3.1",
        "A value that is not a strict JSON literal (single quotes).",
        [("contracts/fs.kvx", 'returns = "int"\n', "returns = 'int'\n")],
        {},
        [],
        [("contracts/fs.kvx", "json", "returns = 'int'", "'int'")],
    ),
    (
        "trailing_comment",
        "3.1",
        "Text after the JSON value on a key line.",
        [("world.kvx", "cpu_ms = 5000\n", "cpu_ms = 5000 # five seconds\n")],
        {},
        [],
        [("world.kvx", "json", "cpu_ms = 5000 #", "#")],
    ),
    (
        "key_before_section",
        "3.1",
        "A key line before any section header.",
        [("world.kvx", "[meta]\n", "orphan = 1\n\n[meta]\n")],
        {},
        [],
        [("world.kvx", "syntax", "orphan = 1", None)],
    ),
    (
        "float_limit",
        "3.1",
        "An integer key given a float literal.",
        [("world.kvx", "wall_ms = 30000\n", "wall_ms = 30000.0\n")],
        {},
        [],
        [("world.kvx", "bad_value", "wall_ms = 30000.0", "30000.0")],
    ),
    (
        "unknown_change_kind",
        "3.1",
        "A changelog change with a kind outside the eleven kinds.",
        [("CHANGELOG.kvx", 'kind = "precondition_added"\n', 'kind = "rule_tweak"\n')],
        {},
        [],
        [("CHANGELOG.kvx", "bad_value", 'kind = "rule_tweak"', '"rule_tweak"')],
    ),
    (
        "unknown_fixture_key",
        "3.1",
        "A fixture case declares a key the grammar does not define.",
        [("fixtures/fs/delete.kvx", "returned = null\n", 'returned = null\nnote = "x"\n')],
        {},
        [],
        [("fixtures/fs/delete.kvx", "unknown_key", 'note = "x"', "note")],
    ),
    # 3.2: primitive shape
    (
        "read_only_with_effects",
        "3.2",
        "A read_only primitive declares an effect.",
        [
            (
                "contracts/fs.kvx",
                'returns = "bool"\npreconditions = []\neffects = []\ndoc = "True when a file exists at path. Never fails."',
                'returns = "bool"\npreconditions = []\neffects = ["fs.exists(path)"]\ndoc = "True when a file exists at path. Never fails."',
            )
        ],
        {},
        [],
        [("contracts/fs.kvx", "shape", '@1effects = ["fs.exists(path)"]', "[")],
    ),
    (
        "mutating_without_effects",
        "3.2",
        "A mutating primitive declares no effect.",
        [("contracts/fs.kvx", 'effects = ["not fs.exists(path)"]\n', "effects = []\n")],
        {},
        [],
        [("contracts/fs.kvx", "shape", "@4effects = []", "[")],
    ),
    (
        "primitive_external",
        "3.2",
        "A contracts/ primitive claims side_effect external.",
        [
            (
                "contracts/fs.kvx",
                '[primitive.fs.size]\nside_effect = "read_only"',
                '[primitive.fs.size]\nside_effect = "external"',
            )
        ],
        {},
        [],
        [("contracts/fs.kvx", "shape", 'side_effect = "external"', '"external"')],
    ),
    (
        "bad_side_effect",
        "3.2",
        "A side_effect outside {read_only, mutating, external}.",
        [
            (
                "contracts/kv.kvx",
                '[primitive.kv.used]\nside_effect = "read_only"',
                '[primitive.kv.used]\nside_effect = "pure"',
            )
        ],
        {},
        [],
        [("contracts/kv.kvx", "bad_value", 'side_effect = "pure"', '"pure"')],
    ),
    (
        "missing_returns",
        "3.2",
        "A primitive omits its return type.",
        [
            (
                "contracts/kv.kvx",
                'params = {"ns": "str"}\nreturns = "int"\n',
                'params = {"ns": "str"}\n',
            )
        ],
        {},
        [],
        [("contracts/kv.kvx", "missing_key", "[primitive.kv.used]", None)],
    ),
    (
        "bad_param_type",
        "3.2",
        "A parameter type outside the closed type set.",
        [("contracts/kv.kvx", 'params = {"ns": "str"}\n', 'params = {"ns": "float"}\n')],
        {},
        [],
        [("contracts/kv.kvx", "bad_value", 'params = {"ns": "float"}', '"float"')],
    ),
    (
        "hidden_expression",
        "3.2",
        "A precondition outside the expression language (attribute access).",
        [
            (
                "contracts/fs.kvx",
                '[["fs.exists(path)", "NOT_FOUND"]]\neffects = []',
                '[["path.parent == \\"/\\"", "NOT_FOUND"]]\neffects = []',
            )
        ],
        {},
        [],
        [("contracts/fs.kvx", "expression", "path.parent", '"path.parent')],
    ),
    (
        "two_failure_codes",
        "3.2",
        "A precondition declaring two failure codes instead of exactly one.",
        [
            (
                "contracts/fs.kvx",
                '[["fs.exists(path)", "NOT_FOUND"]]\neffects = []',
                '[["fs.exists(path)", "NOT_FOUND", "LOCKED"]]\neffects = []',
            )
        ],
        {},
        [],
        [("contracts/fs.kvx", "bad_value", '"NOT_FOUND", "LOCKED"]]', '["fs.exists')],
    ),
    (
        "wrong_module_prefix",
        "3.2",
        "A contract file declares a primitive of another module.",
        [("contracts/fs.kvx", "[primitive.fs.locked]", "[primitive.kv.locked]")],
        {},
        [],
        [("contracts/fs.kvx", "shape", "[primitive.kv.locked]", None)],
    ),
    (
        "duplicate_primitive",
        "3.2",
        "The same primitive declared in contracts/ and adapters/ (CWR-C002).",
        [],
        {"adapters/fs.kvx": NEW_ADAPTER_FS},
        [],
        [("adapters/fs.kvx", "duplicate_primitive", "[adapter.fs.exists]", None)],
    ),
    (
        "undeclared_module",
        "3.2",
        "A contract file for a module absent from world.modules (CWR-C003).",
        [],
        {"contracts/jobs.kvx": NEW_CONTRACT_JOBS},
        [],
        [("contracts/jobs.kvx", "undeclared_module", 'module = "jobs"', '"jobs"')],
    ),
    (
        "empty_doc",
        "3.2",
        "A primitive with an empty doc string.",
        [("contracts/kv.kvx", 'doc = "Bytes stored in namespace ns."', 'doc = ""')],
        {},
        [],
        [("contracts/kv.kvx", "doc", 'doc = ""', '""')],
    ),
    # 2.3: closed envelope
    (
        "unknown_failure_code",
        "2.3",
        "A precondition names a code outside the envelope.",
        [
            (
                "contracts/fs.kvx",
                '[["fs.exists(path)", "NOT_FOUND"], ["not fs.locked(path)", "LOCKED"]]',
                '[["fs.exists(path)", "GONE"], ["not fs.locked(path)", "LOCKED"]]',
            )
        ],
        {},
        [],
        [("contracts/fs.kvx", "envelope", '"GONE"', '"GONE"')],
    ),
    (
        "lowercase_failure_code",
        "2.3",
        "A code in the wrong case is not an envelope code.",
        [
            (
                "contracts/kv.kvx",
                '["len(value) <= 1024", "INVALID"]',
                '["len(value) <= 1024", "invalid"]',
            )
        ],
        {},
        [],
        [("contracts/kv.kvx", "envelope", '"invalid"', '"invalid"')],
    ),
    (
        "adapter_failure_outside_envelope",
        "2.3",
        "An adapter failure set names a code outside the envelope.",
        [("adapters/search.kvx", '"RATE_LIMITED", "UNAVAILABLE"', '"THROTTLED", "UNAVAILABLE"')],
        {},
        [],
        [("adapters/search.kvx", "envelope", '"THROTTLED"', '"THROTTLED"')],
    ),
    (
        "fixture_unknown_expect",
        "2.3",
        "A fixture expects a code outside the envelope.",
        [("fixtures/fs/delete.kvx", 'expect = "NOT_FOUND"', 'expect = "MISSING"')],
        {},
        [],
        [("fixtures/fs/delete.kvx", "envelope", 'expect = "MISSING"', '"MISSING"')],
    ),
    (
        "drift_unknown_failure",
        "2.3",
        "A drift schedule injects a code outside the envelope.",
        [("drift/drift-1.kvx", 'failure = "LOCKED"', 'failure = "BLOCKED"')],
        {},
        [],
        [("drift/drift-1.kvx", "envelope", 'failure = "BLOCKED"', '"BLOCKED"')],
    ),
    # 3.3: adapters
    (
        "adapter_with_effects",
        "3.3",
        "An adapter declares effects over world state.",
        [
            (
                "adapters/search.kvx",
                "content_guaranteed = false\n",
                'content_guaranteed = false\neffects = ["len(q) > 0"]\n',
            )
        ],
        {},
        [],
        [("adapters/search.kvx", "shape", "effects = [", "effects")],
    ),
    (
        "adapter_content_guaranteed_true",
        "3.3",
        "An adapter claims its content is guaranteed.",
        [("adapters/search.kvx", "content_guaranteed = false", "content_guaranteed = true")],
        {},
        [],
        [("adapters/search.kvx", "adapter", "content_guaranteed = true", "true")],
    ),
    (
        "adapter_content_guaranteed_missing",
        "3.3",
        "An adapter omits content_guaranteed.",
        [("adapters/search.kvx", "content_guaranteed = false\n", "")],
        {},
        [],
        [("adapters/search.kvx", "adapter", "[adapter.search.query]", None)],
    ),
    (
        "adapter_without_unavailable",
        "3.3",
        "An adapter failure set without UNAVAILABLE.",
        [
            (
                "adapters/search.kvx",
                '"RATE_LIMITED", "UNAVAILABLE", "TIMEOUT"',
                '"RATE_LIMITED", "TIMEOUT"',
            )
        ],
        {},
        [],
        [("adapters/search.kvx", "adapter", "failures = [", "[")],
    ),
    (
        "adapter_empty_failures",
        "3.3",
        "An adapter with an empty failure set.",
        [
            (
                "adapters/search.kvx",
                'failures = ["INVALID", "LIMIT", "RATE_LIMITED", "UNAVAILABLE", "TIMEOUT"]',
                "failures = []",
            )
        ],
        {},
        [],
        [("adapters/search.kvx", "adapter", "failures = []", "[")],
    ),
    (
        "adapter_mutating",
        "3.3",
        "An adapter whose side_effect is not external.",
        [("adapters/search.kvx", 'side_effect = "external"', 'side_effect = "mutating"')],
        {},
        [],
        [("adapters/search.kvx", "shape", 'side_effect = "mutating"', '"mutating"')],
    ),
    (
        "adapter_bad_idempotency",
        "3.3",
        "An idempotency value outside {none, natural, key}.",
        [("adapters/search.kvx", 'idempotency = "none"', 'idempotency = "at_most_once"')],
        {},
        [],
        [("adapters/search.kvx", "bad_value", "idempotency = ", '"at_most_once"')],
    ),
    (
        "adapter_missing_timeout",
        "3.3",
        "An adapter omits timeout_ms.",
        [("adapters/search.kvx", "timeout_ms = 3000\n", "")],
        {},
        [],
        [("adapters/search.kvx", "missing_key", "[adapter.search.query]", None)],
    ),
    (
        "adapter_zero_timeout",
        "3.3",
        "An adapter timeout_ms that is not positive.",
        [("adapters/search.kvx", "timeout_ms = 3000", "timeout_ms = 0")],
        {},
        [],
        [("adapters/search.kvx", "bad_value", "timeout_ms = 0", "0")],
    ),
    (
        "adapter_missing_endpoint",
        "3.3",
        "An adapter omits its endpoint.",
        [("adapters/search.kvx", 'endpoint = "stub://search/v1/query"\n', "")],
        {},
        [],
        [("adapters/search.kvx", "missing_key", "[adapter.search.query]", None)],
    ),
    # 3.4: world.kvx, no defaults
    (
        "world_missing_memory_mb",
        "3.4",
        "world.kvx omits limits.memory_mb.",
        [("world.kvx", "memory_mb = 256\n", "")],
        {},
        [],
        [("world.kvx", "missing_key", "[limits]", None)],
    ),
    (
        "world_missing_limits",
        "3.4",
        "world.kvx omits the whole [limits] section.",
        [
            (
                "world.kvx",
                "[limits]\ncpu_ms = 5000\nwall_ms = 30000\nmemory_mb = 256\nprimitive_calls = 500\nadapter_calls = 50\n\n",
                "",
            )
        ],
        {},
        [],
        [("world.kvx", "missing_key", None, None)],
    ),
    (
        "world_zero_limit",
        "3.4",
        "A limit that is not a positive integer.",
        [("world.kvx", "primitive_calls = 500", "primitive_calls = 0")],
        {},
        [],
        [("world.kvx", "bad_value", "primitive_calls = 0", "0")],
    ),
    (
        "world_missing_tick_per_turn",
        "3.4",
        "world.kvx omits clock.tick_per_turn.",
        [("world.kvx", "tick_per_turn = 10\n", "")],
        {},
        [],
        [("world.kvx", "missing_key", "[clock]", None)],
    ),
    (
        "world_missing_seed_visible",
        "3.4",
        "world.kvx omits rng.seed_visible.",
        [("world.kvx", "seed_visible = true\n", "")],
        {},
        [],
        [("world.kvx", "missing_key", "[rng]", None)],
    ),
    (
        "world_bad_version",
        "3.4",
        "world.version is not MAJOR.MINOR.PATCH.",
        [("world.kvx", 'version = "0.2.0"', 'version = "0.2"')],
        {},
        [],
        [("world.kvx", "bad_value", 'version = "0.2"', '"0.2"')],
    ),
    (
        "world_bad_language",
        "3.4",
        "world.language other than python.",
        [("world.kvx", 'language = "python"', 'language = "javascript"')],
        {},
        [],
        [("world.kvx", "bad_value", "language = ", '"javascript"')],
    ),
    (
        "world_bad_runtime",
        "3.4",
        "world.runtime other than 3.12.",
        [("world.kvx", 'runtime = "3.12"', 'runtime = "3.11"')],
        {},
        [],
        [("world.kvx", "bad_value", "runtime = ", '"3.11"')],
    ),
    (
        "world_missing_name",
        "3.4",
        "world.kvx omits world.name.",
        [("world.kvx", 'name = "fixtureworld"\n', "")],
        {},
        [],
        [("world.kvx", "missing_key", "[world]", None)],
    ),
    (
        "world_missing_modules",
        "3.4",
        "world.kvx omits world.modules.",
        [("world.kvx", 'modules = ["fs", "kv", "search"]\n', "")],
        {},
        [],
        [("world.kvx", "missing_key", "[world]", None)],
    ),
    # 3.6: migrations and changelog
    (
        "state_shape_without_migration",
        "3.6",
        "A state_shape change with an empty migration (CWR-M001).",
        [("CHANGELOG.kvx", 'migration = "migrations/0_2_0_kv_namespaces.py"', 'migration = ""')],
        {},
        [],
        [("CHANGELOG.kvx", "migration", '@2migration = ""', '""')],
    ),
    (
        "state_shape_missing_migration_key",
        "3.6",
        "A state_shape change that omits the migration key.",
        [("CHANGELOG.kvx", 'migration = "migrations/0_2_0_kv_namespaces.py"\n', "")],
        {},
        [],
        [("CHANGELOG.kvx", "migration", "[change.kv_namespaces]", None)],
    ),
    (
        "migration_file_missing",
        "3.6",
        "A state_shape change naming a migration file that does not exist.",
        [
            (
                "CHANGELOG.kvx",
                'migration = "migrations/0_2_0_kv_namespaces.py"',
                'migration = "migrations/0_2_0_kv_missing.py"',
            )
        ],
        {},
        [],
        [("CHANGELOG.kvx", "migration", "0_2_0_kv_missing.py", '"migrations/')],
    ),
    (
        "migration_outside_dir",
        "3.6",
        "A migration path outside migrations/.",
        [
            (
                "CHANGELOG.kvx",
                'migration = "migrations/0_2_0_kv_namespaces.py"',
                'migration = "lib/kv.py"',
            )
        ],
        {},
        [],
        [("CHANGELOG.kvx", "migration", 'migration = "lib/kv.py"', '"lib/kv.py"')],
    ),
    (
        "migration_not_a_function",
        "3.6",
        "A migration file that does not define migrate(state).",
        [],
        {"migrations/0_2_0_kv_namespaces.py": '"""Not a migration."""\n\nSTATE = {}\n'},
        [],
        [
            ("migrations/0_2_0_kv_namespaces.py", "migration", None, None),
            (
                "CHANGELOG.kvx",
                "migration",
                'migration = "migrations/0_2_0_kv_namespaces.py"',
                '"migrations/',
            ),
            (
                "fixtures/kv/migration_0_2_0.kvx",
                "fixture",
                'migration = "migrations/',
                '"migrations/',
            ),
        ],
    ),
    (
        "version_mismatch",
        "3.6",
        "world.version differs from the newest changelog release.",
        [("world.kvx", 'version = "0.2.0"', 'version = "0.3.0"')],
        {},
        [],
        [("world.kvx", "changelog", 'version = "0.3.0"', '"0.3.0"')],
    ),
    (
        "change_in_no_release",
        "3.6",
        "A change that belongs to no release.",
        [
            (
                "CHANGELOG.kvx",
                "[release.0.1.0]",
                '[change.orphan]\nkind = "doc_only"\ntarget = "fs"\nbefore = "a"\nafter = "b"\nmigration = ""\n\n[release.0.1.0]',
            )
        ],
        {},
        [],
        [("CHANGELOG.kvx", "changelog", "[change.orphan]", None)],
    ),
    (
        "release_unknown_change",
        "3.6",
        "A release lists a change id with no change section.",
        [("CHANGELOG.kvx", 'changes = ["initial"]', 'changes = ["initial", "ghost"]')],
        {},
        [],
        [("CHANGELOG.kvx", "changelog", '"ghost"', '"ghost"')],
    ),
    # 3.3: adapter keys the gateway relies on
    (
        "adapter_undeclared_status_locked",
        "3.3",
        "An undeclared_status other than UNAVAILABLE or INVALID.",
        [("adapters/search.kvx", 'undeclared_status = "INVALID"', 'undeclared_status = "LOCKED"')],
        {},
        [],
        [("adapters/search.kvx", "adapter", 'undeclared_status = "LOCKED"', '"LOCKED"')],
    ),
    (
        "adapter_undeclared_status_not_declared",
        "3.3",
        "An undeclared_status that is not one of the adapter's declared failures.",
        [
            ("adapters/search.kvx", 'failures = ["INVALID", "LIMIT", ', 'failures = ["LIMIT", '),
            ("adapters/search.kvx", '["len(q) > 0", "INVALID"]', '["len(q) > 0", "LIMIT"]'),
        ],
        {},
        [],
        [("adapters/search.kvx", "adapter", 'undeclared_status = "INVALID"', '"INVALID"')],
    ),
    (
        "adapter_zero_retry_ratio",
        "3.3",
        "A retry_after_ratio that is not a positive integer.",
        [("adapters/search.kvx", "retry_after_ratio = 7", "retry_after_ratio = 0")],
        {},
        [],
        [("adapters/search.kvx", "bad_value", "retry_after_ratio = 0", "0")],
    ),
    (
        "adapter_failures_without_timeout",
        "3.3",
        "An adapter failure set without TIMEOUT, which the gateway produces itself.",
        [("adapters/search.kvx", '"UNAVAILABLE", "TIMEOUT"]', '"UNAVAILABLE"]')],
        {},
        [],
        [("adapters/search.kvx", "adapter", "failures = [", "[")],
    ),
    (
        "key_adapter_without_duplicate_key",
        "3.3",
        "A key adapter whose failure set lacks DUPLICATE_KEY.",
        [("adapters/search.kvx", 'idempotency = "none"', 'idempotency = "key"')],
        {},
        [],
        [("adapters/search.kvx", "adapter", "failures = [", "[")],
    ),
    (
        "adapter_precondition_code_undeclared",
        "3.3",
        "An adapter precondition raises a code outside its declared failures.",
        [("adapters/search.kvx", '["len(q) > 0", "INVALID"]', '["len(q) > 0", "LOCKED"]')],
        {},
        [],
        [("adapters/search.kvx", "adapter", "preconditions = ", '"LOCKED"')],
    ),
    (
        "adapter_file_endpoint",
        "3.3",
        "An endpoint with a local-file scheme.",
        [
            (
                "adapters/search.kvx",
                'endpoint = "stub://search/v1/query"',
                'endpoint = "file:///etc/passwd"',
            )
        ],
        {},
        [],
        [("adapters/search.kvx", "bad_value", "endpoint = ", '"file')],
    ),
    (
        "adapter_endpoint_newline",
        "3.3",
        "An endpoint with an embedded newline.",
        [
            (
                "adapters/search.kvx",
                'endpoint = "stub://search/v1/query"',
                'endpoint = "stub://search/v1\\nquery"',
            )
        ],
        {},
        [],
        [("adapters/search.kvx", "bad_value", "endpoint = ", '"stub')],
    ),
    # 3.2: precondition codes
    (
        "precondition_invariant_violation",
        "3.2",
        "A precondition declares INVARIANT_VIOLATION, which only the kernel raises.",
        [
            (
                "contracts/fs.kvx",
                '[["fs.exists(path)", "NOT_FOUND"]]\neffects = []',
                '[["fs.exists(path)", "INVARIANT_VIOLATION"]]\neffects = []',
            )
        ],
        {},
        [],
        [("contracts/fs.kvx", "shape", "INVARIANT_VIOLATION", '"INVARIANT_VIOLATION"')],
    ),
    # 3.1: line breaks other readers split on
    (
        "kvx_line_separator",
        "3.1",
        "A raw U+2028 inside a value, which other KVX readers treat as a line break.",
        [("world.kvx", 'name = "fixtureworld"', 'name = "fixture world"')],
        {},
        [],
        [("world.kvx", "syntax", "fixture world", " ")],
    ),
    # 3.1: fixture value types
    (
        "fixture_sequence_zero",
        "3.1",
        "A fixture sequence of 0; sequences are one-based.",
        [("fixtures/search/query.kvx", "sequence = 1\n", "sequence = 0\n")],
        {},
        [],
        [("fixtures/search/query.kvx", "bad_value", "sequence = 0", "0")],
    ),
    (
        "fixture_bad_request_hash",
        "3.1",
        "A fixture request_hash that is not sha256 and 64 lowercase hex digits.",
        [
            (
                "fixtures/search/query.kvx",
                'request_hash = "sha256:5531fa99d785a2f830769698330af36fde736a1b7166828e05d8d384819800e9"',
                'request_hash = "sha256:0000"',
            )
        ],
        {},
        [],
        [("fixtures/search/query.kvx", "bad_value", "request_hash = ", '"sha256')],
    ),
    (
        "fixture_bad_prior",
        "3.1",
        "A prior call with a key the prior-call shape does not define.",
        [
            (
                "fixtures/search/query.kvx",
                '"sequence": 1, "world_time": 0}]',
                '"sequence": 1, "world_time": 0, "note": "x"}]',
            )
        ],
        {},
        [],
        [("fixtures/search/query.kvx", "bad_value", "prior = [", "{")],
    ),
    # 3.6: migration fixtures
    (
        "migration_fixture_missing_after",
        "3.6",
        "A migration fixture case without the state migrate must return.",
        [
            (
                "fixtures/kv/migration_0_2_0.kvx",
                'after = {"kv": {"namespaces": {"default": {"a": "1"}}}}\n',
                "",
            )
        ],
        {},
        [],
        [("fixtures/kv/migration_0_2_0.kvx", "missing_key", "[migration.flat]", None)],
    ),
    (
        "migration_fixture_before_not_object",
        "3.6",
        "A migration fixture whose before state is not an object.",
        [("fixtures/kv/migration_0_2_0.kvx", 'before = {"kv": {"a": "1"}}', "before = [1]")],
        {},
        [],
        [("fixtures/kv/migration_0_2_0.kvx", "bad_value", "before = [1]", "[")],
    ),
    (
        "migration_fixture_case_section",
        "3.6",
        "A migration fixture that declares a call case section.",
        [("fixtures/kv/migration_0_2_0.kvx", "[migration.flat]", "[case.flat]")],
        {},
        [],
        [("fixtures/kv/migration_0_2_0.kvx", "unknown_section", "[case.flat]", None)],
    ),
    # 3.1: drift schedules (compiler.md section 10)
    (
        "drift_unknown_primitive",
        "3.1",
        "A drift entry names a primitive the source does not declare.",
        [("drift/drift-1.kvx", 'primitive = "fs.delete"', 'primitive = "fs.erase"')],
        {},
        [],
        [("drift/drift-1.kvx", "bad_value", 'primitive = "fs.erase"', '"fs.erase"')],
    ),
    (
        "drift_state_shape_change",
        "3.1",
        "A drift entry with a change kind that cannot apply to a running table.",
        [("drift/drift-1.kvx", 'change = "precondition_added"', 'change = "state_shape"')],
        {},
        [],
        [("drift/drift-1.kvx", "bad_value", 'change = "state_shape"', '"state_shape"')],
    ),
    (
        "drift_bad_expression",
        "3.1",
        "A drift entry whose expression is outside the precondition language.",
        [
            (
                "drift/drift-1.kvx",
                'expr = "not fs.locked(path) and fs.size(path) < 4096"',
                'expr = "path.parent"',
            )
        ],
        {},
        [],
        [("drift/drift-1.kvx", "expression", 'expr = "path.parent"', '"path')],
    ),
    (
        "drift_missing_doc",
        "3.1",
        "A drift entry without its author's note.",
        [
            (
                "drift/drift-1.kvx",
                'doc = "From turn 8, fs.delete refuses files of 4 KiB or more."\n',
                "",
            )
        ],
        {},
        [],
        [("drift/drift-1.kvx", "missing_key", "[drift.1]", None)],
    ),
]


def locate(text: str, line_contains: str | None, col_at: str | None) -> tuple[int, int]:
    if line_contains is None:
        return 1, 1
    occurrence, explicit_marker = 1, False
    if line_contains.startswith("@"):
        explicit_marker = True
        occurrence = int(line_contains[1])
        line_contains = line_contains[2:]
    matches = [i for i, line in enumerate(text.split("\n"), 1) if line_contains in line]
    explicit = explicit_marker
    if len(matches) < occurrence or (not explicit and len(matches) != 1):
        raise SystemExit(f"cannot locate {line_contains!r} (occurrence {occurrence}): {matches}")
    number = matches[occurrence - 1]
    line = text.split("\n")[number - 1]
    col = 1 if col_at is None else line.index(col_at) + 1
    if col_at is None:
        col = len(line) - len(line.lstrip()) + 1
    return number, col


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    names = set()
    for name, clause, description, edits, adds, remove, expected in CASES:
        assert name not in names, name
        names.add(name)
        files: dict[str, str] = {}
        for path, old, new in edits:
            text = files.get(path, (VALID / path).read_text())
            assert text.count(old) == 1, (name, path, old, text.count(old))
            files[path] = text.replace(old, new)
        files.update({path: with_header(path, text) for path, text in adds.items()})
        errors = []
        for path, kind, line_contains, col_at in expected:
            text = files.get(path, (VALID / path).read_text() if (VALID / path).exists() else "")
            line, col = locate(text, line_contains, col_at)
            errors.append(
                {"file": path, "line": line, "col": col, "kind": kind, "code": LINT.get(kind)}
            )
        case_dir = OUT / name
        for path, text in files.items():
            target = case_dir / "files" / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        errors.sort(key=lambda e: (e["file"], e["line"], e["col"]))
        record = {
            "class": name,
            "clause": clause,
            "description": description,
            "remove": remove,
            "errors": errors,
        }
        (case_dir / "case.json").write_text(json.dumps(record, indent=2) + "\n")
    print(f"wrote {len(CASES)} malformed classes to {OUT}")


if __name__ == "__main__":
    main()

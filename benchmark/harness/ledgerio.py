# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Readers and writers for the harness's on-disk inputs and outputs.

KVX is the repository's section format: `[section]` headers and `key = <one JSON value>` lines,
`#` comments, no duplicate sections or keys. Ledgers are append-only JSONL files written by the
kernel (kernel.md section 8). Nothing here interprets metric semantics; that is metrics.py.
Standard library only.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]

_SECTION = re.compile(r"\[([A-Za-z0-9_.-]+)\]")
_ASSIGN = re.compile(r"([A-Za-z_][A-Za-z0-9_-]*)\s*=\s*(.+)")
_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")


class InvalidKVX(ValueError):  # noqa: N818 - public name used by callers
    pass


class InvalidLedger(ValueError):  # noqa: N818 - public name used by callers
    pass


def _strict_json(text: str) -> Any:
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError(f"duplicate JSON object key {key}")
            out[key] = value
        return out

    def bad_constant(value):
        raise ValueError(f"non-JSON numeric constant {value}")

    return json.loads(text, object_pairs_hook=pairs, parse_constant=bad_constant)


def parse_kvx(text: str, origin: str = "<kvx>") -> dict[str, dict[str, Any]]:
    """Strictly parse KVX text into {section: {key: value}}."""
    sections: dict[str, dict[str, Any]] = {}
    current: dict[str, Any] | None = None
    for number, original in enumerate(text.splitlines(), 1):
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        header = _SECTION.fullmatch(line)
        if header:
            name = header.group(1)
            if name in sections:
                raise InvalidKVX(f"{origin}:{number}: duplicate section [{name}]")
            current = sections[name] = {}
            continue
        assign = _ASSIGN.fullmatch(line)
        if not assign or current is None:
            raise InvalidKVX(f"{origin}:{number}: unsupported or out-of-section syntax")
        key, raw = assign.groups()
        if key in current:
            raise InvalidKVX(f"{origin}:{number}: duplicate key {key}")
        try:
            current[key] = _strict_json(raw)
        except ValueError as error:
            raise InvalidKVX(
                f"{origin}:{number}: value must be one JSON literal: {error}"
            ) from error
    return sections


def read_kvx(path: Path | str) -> dict[str, dict[str, Any]]:
    path = Path(path)
    return parse_kvx(path.read_text(encoding="utf-8"), str(path))


def dump_kvx(sections: dict[str, dict[str, Any]], header: str = "") -> str:
    """Serialise sections deterministically (section and key order preserved, compact JSON)."""
    lines: list[str] = []
    for comment in header.splitlines():
        lines.append(f"# {comment}".rstrip())
    for name, body in sections.items():
        if not _SECTION.fullmatch(f"[{name}]"):
            raise InvalidKVX(f"invalid section name {name!r}")
        if lines:
            lines.append("")
        lines.append(f"[{name}]")
        for key, value in body.items():
            if not _KEY.fullmatch(key):
                raise InvalidKVX(f"invalid key {key!r} in [{name}]")
            lines.append(
                f"{key} = {json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(', ', ': '), allow_nan=False)}"
            )
    return "\n".join(lines) + "\n"


def write_kvx(path: Path | str, sections: dict[str, dict[str, Any]], header: str = "") -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = dump_kvx(sections, header)
    parse_kvx(text, str(path))  # round-trip guard
    path.write_text(text, encoding="utf-8")


def read_ledger(path: Path | str) -> list[dict[str, Any]]:
    """Read a ledger strictly: every line one JSON object, seq 0..n-1 without gaps.

    A truncated or unparsable final line is refused, as the kernel refuses it (kernel.md section 8).
    """
    path = Path(path)
    data = path.read_bytes()
    if data and not data.endswith(b"\n"):
        raise InvalidLedger(f"{path}: final record is truncated (no trailing newline)")
    records: list[dict[str, Any]] = []
    for number, raw in enumerate(data.decode("utf-8").splitlines(), 1):
        if not raw.strip():
            raise InvalidLedger(f"{path}:{number}: blank line in ledger")
        try:
            record = _strict_json(raw)
        except ValueError as error:
            raise InvalidLedger(f"{path}:{number}: not JSON: {error}") from error
        if not isinstance(record, dict):
            raise InvalidLedger(f"{path}:{number}: record is not an object")
        if record.get("seq") != number - 1:
            raise InvalidLedger(
                f"{path}:{number}: seq {record.get('seq')!r} breaks the 0..n-1 sequence"
            )
        records.append(record)
    if not records:
        raise InvalidLedger(f"{path}: empty ledger")
    if records[0].get("kind") != "header":
        raise InvalidLedger(f"{path}: record 0 is not the header")
    return records


def write_ledger(path: Path | str, records: list[dict[str, Any]]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")


def sha256_file(path: Path | str) -> str:
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_predicate(path: Path | str) -> Callable[[Any, list[dict[str, Any]]], bool]:
    """Load `success(state, ledger) -> bool` from a task's success.py (reference-world.md section 5).

    When the suite's helper module benchmark/predicates/common.py exists, its sandboxed loader is used
    (purity audit, `import common` as the only import). Otherwise the file is imported directly with
    benchmark/predicates on sys.path.
    """
    path = Path(path).resolve()
    predicates = REPO / "benchmark" / "predicates"
    if (predicates / "common.py").exists():
        if str(predicates) not in sys.path:
            sys.path.insert(0, str(predicates))
        import common  # type: ignore[import-not-found]

        if hasattr(common, "load_predicate"):
            return common.load_predicate(path.parent)
    name = "cwr_predicate_" + hashlib.sha256(str(path).encode()).hexdigest()[:16]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load predicate {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    function = getattr(module, "success", None)
    if not callable(function):
        raise ImportError(f"{path} defines no callable success(state, ledger)")
    return function


def state_view(sections: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The predicate's state view of a state KVX file: every section except [meta]."""
    return {name: body for name, body in sections.items() if name != "meta"}

# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Strict KVX parser and serialiser.

KVX is the line format of every CWR source, registry and campaign file:

    # a comment on its own line
    [section.name-1]
    key = <exactly one JSON literal>

The accepted language is identical to `parse_kvx` in `spec/cwr/tools/validate_spec.py`:
section names match `[A-Za-z0-9_.-]+`, keys match `[A-Za-z_][A-Za-z0-9_-]*`, every value is a
single strict JSON literal (no NaN or Infinity, no duplicate object keys), a line whose first
non-blank character is `#` is a comment, and duplicate sections, duplicate keys and assignments
before the first section are rejected. A comment after a value is not a comment: it makes the value
invalid JSON. Every error carries the source name, line and column.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

__all__ = ["KVXError", "dump", "dumps_value", "load", "loads"]

SECTION_RE = re.compile(r"\[([A-Za-z0-9_.-]+)\]")
ASSIGN_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_-]*)\s*=\s*(.+)")
KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")
# Characters str.splitlines() treats as line breaks that json.dumps(ensure_ascii=False) leaves raw.
LINE_BREAK_ESCAPES = {"\x85": "\\u0085", "\u2028": "\\u2028", "\u2029": "\\u2029"}

Sections = dict[str, dict[str, Any]]


class KVXError(ValueError):
    """A located KVX error. `str()` is `source:line:col: message`; line and column are one-based."""

    def __init__(self, source: str, line: int, col: int, message: str) -> None:
        super().__init__(f"{source}:{line}:{col}: {message}")
        self.source = source
        self.line = line
        self.col = col
        self.message = message


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key {key}")
        result[key] = value
    return result


def _invalid_constant(value: str) -> Any:
    raise ValueError(f"non-JSON numeric constant {value}")


def _strict_json(text: str) -> Any:
    return json.loads(text, object_pairs_hook=_unique_pairs, parse_constant=_invalid_constant)


def loads(text: str, source: str = "<string>") -> Sections:
    """Parse KVX text into an ordered mapping of section name to ordered key/value mapping.

    Raises KVXError on the first violation; nothing partial is returned.
    """
    sections: Sections = {}
    current: dict[str, Any] | None = None
    for number, original in enumerate(text.splitlines(), 1):
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        indent = len(original) - len(original.lstrip())
        col = indent + 1
        section = SECTION_RE.fullmatch(line)
        if section:
            name = section.group(1)
            if name in sections:
                raise KVXError(source, number, col, f"duplicate section [{name}]")
            current = sections[name] = {}
            continue
        assignment = ASSIGN_RE.fullmatch(line)
        if not assignment or current is None:
            raise KVXError(source, number, col, "unsupported or out-of-section syntax")
        key, value = assignment.groups()
        if key in current:
            raise KVXError(source, number, col, f"duplicate key {key}")
        value_col = indent + assignment.start(2) + 1
        try:
            current[key] = _strict_json(value)
        except json.JSONDecodeError as error:
            raise KVXError(
                source,
                number,
                value_col + error.pos,
                f"value must be a single JSON literal: {error.msg}",
            ) from error
        except ValueError as error:
            raise KVXError(
                source, number, value_col, f"value must be a single JSON literal: {error}"
            ) from error
    return sections


def load(path: str | Path) -> Sections:
    """Read and parse a UTF-8 KVX file; errors are located by the path as given."""
    path = Path(path)
    return loads(path.read_text(encoding="utf-8"), source=str(path))


def dumps_value(value: Any) -> str:
    """Serialise one value as the single-line JSON literal KVX requires.

    Non-ASCII text stays literal except the code points `str.splitlines` would break a line on,
    which are escaped so the literal can never span two lines.

    Raises ValueError for NaN or Infinity and TypeError for values JSON cannot represent.
    """
    text = json.dumps(value, ensure_ascii=False, allow_nan=False)
    for raw, escaped in LINE_BREAK_ESCAPES.items():
        text = text.replace(raw, escaped)
    return text


def dump(sections: Mapping[str, Mapping[str, Any]], header: Iterable[str] = ()) -> str:
    """Serialise sections to canonical KVX that `loads` reads back to an equal mapping.

    Output is deterministic for a given input: header comment lines first, then each section in
    mapping order with its keys in mapping order, one blank line between blocks, one final newline.
    Raises ValueError for a section name or key the grammar does not accept, or a multi-line
    header entry.
    """
    blocks: list[str] = []
    comments = []
    for entry in header:
        if "\n" in entry or "\r" in entry:
            raise ValueError("a header comment must be a single line")
        comments.append(f"# {entry}".rstrip())
    if comments:
        blocks.append("\n".join(comments))
    for name, values in sections.items():
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            raise ValueError(f"invalid KVX section name {name!r}")
        lines = [f"[{name}]"]
        for key, value in values.items():
            if not isinstance(key, str) or not KEY_RE.fullmatch(key):
                raise ValueError(f"invalid KVX key {key!r} in [{name}]")
            lines.append(f"{key} = {dumps_value(value)}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n" if blocks else ""

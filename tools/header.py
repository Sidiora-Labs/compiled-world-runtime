#!/usr/bin/env python3
# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Check or apply the Sidiora source header to every file in the repository.

Usage:
    python3 tools/header.py --check [PATH ...]
    python3 tools/header.py --apply [PATH ...]

Every text file whose syntax admits a comment carries the two header lines at
its top, after a shebang if there is one. Files that cannot carry a comment
(JSON, JSON Lines, lock files, binaries) and generated or recorded data
(builds, ledgers, evidence records, cassettes, results) are exempt. The exempt
set is explicit below so that silence is never mistaken for coverage: the
check prints the count of exempt files it skipped.

Exit code 0 when every covered file carries the header, 1 otherwise, 2 on a
usage error.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

LINE_1 = "Copyright Sidiora Labs Inc. CWR® 2026-2027"
LINE_2 = "SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0"

# Comment syntax by file extension or exact file name: (prefix, suffix).
STYLE_BY_EXT: dict[str, tuple[str, str]] = {
    ".rs": ("// ", ""),
    ".js": ("// ", ""),
    ".mjs": ("// ", ""),
    ".ts": ("// ", ""),
    ".c": ("// ", ""),
    ".h": ("// ", ""),
    ".py": ("# ", ""),
    ".pyi": ("# ", ""),
    ".sh": ("# ", ""),
    ".bash": ("# ", ""),
    ".toml": ("# ", ""),
    ".kvx": ("# ", ""),
    ".yaml": ("# ", ""),
    ".yml": ("# ", ""),
    ".cff": ("# ", ""),
    ".cfg": ("# ", ""),
    ".ini": ("# ", ""),
    ".txt": ("# ", ""),
    ".md": ("<!-- ", " -->"),
    ".html": ("<!-- ", " -->"),
    ".mmd": ("%% ", ""),
    ".tmpl": ("# ", ""),  # SDK templates render to Python; the header is emitted with them
}
STYLE_BY_NAME: dict[str, tuple[str, str]] = {
    "Makefile": ("# ", ""),
    ".gitignore": ("# ", ""),
    ".editorconfig": ("# ", ""),
    ".gitattributes": ("# ", ""),
    "LICENSE": ("# ", ""),
    "NOTICE": ("# ", ""),
    "Dockerfile": ("# ", ""),
    "commit-msg": ("# ", ""),  # git hooks under .githooks
    "pre-commit": ("# ", ""),
    ".gitmodules": ("# ", ""),
    ".hyperfideignore": ("# ", ""),
}

# Files that can never carry a comment or that are recorded data.
EXEMPT_EXT = {
    ".json",
    ".jsonl",
    ".lock",
    ".png",
    ".jpg",
    ".gif",
    ".pdf",
    ".bin",
    ".gz",
    ".tar",
    ".zip",
    ".whl",
}
EXEMPT_DIR_PARTS = {
    ".git",
    ".codegraph",  # Hyperfide local code-graph state
    "target",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    "vendor",  # third-party code keeps its own headers
    ".hyperfide",
    ".cwr",  # CWR runtime state (Instance homes, campaign Builds, ledgers): recorded data
    "evidence",  # tests/cwr/evidence: immutable attempt records
    "results",  # benchmark/results: campaign ledgers and analyses
    "cassettes",  # benchmark/cassettes: recorded adapter traffic
    "build",  # compiled Builds (cwr build output): recorded data
    "xbuild",
    "out",
}
# Bare-value data files: one value or one record per line, read verbatim by tooling.
# .gitkeep is an empty placeholder that keeps an otherwise ignored directory in the tree.
EXEMPT_NAMES = {".env", "bin", "rules", ".gitkeep"}
# Specific relative paths that are recorded data even though their syntax admits comments.
EXEMPT_PATHS = {
    "spec/cwr/authoring-validation.json",
    # The normative specification is frozen: its bytes are hashed into every validation report
    # (spec_sha256) and the engineering standard forbids editing it outside a spec revision.
    "spec/cwr/spec.kvx",
    # Derived views: validate_spec.py --render writes them byte-for-byte from spec.kvx and the
    # validator fails when they differ from that rendering, so they cannot carry extra lines.
    "spec/cwr/implementation-plan.md",
    "spec/cwr/traceability.md",
}


def style_for(path: Path) -> tuple[str, str] | None:
    """Return the (prefix, suffix) comment syntax for `path`, or None if its type is unstyled."""
    if path.name in STYLE_BY_NAME:
        return STYLE_BY_NAME[path.name]
    return STYLE_BY_EXT.get(path.suffix)


def is_exempt(path: Path) -> bool:
    """Return True when `path` (inside the repository) can never carry or never needs a header."""
    rel = path.relative_to(ROOT)
    if str(rel) in EXEMPT_PATHS or path.name in EXEMPT_NAMES:
        return True
    if any(part in EXEMPT_DIR_PARTS for part in rel.parts[:-1]):
        return True
    return path.suffix in EXEMPT_EXT


def header_lines(prefix: str, suffix: str) -> tuple[str, str]:
    """Return the two header lines wrapped in the given comment syntax, without newlines."""
    return f"{prefix}{LINE_1}{suffix}", f"{prefix}{LINE_2}{suffix}"


def has_header(text: str, prefix: str, suffix: str) -> bool:
    """Return True when `text` starts with the header, after a shebang line if there is one."""
    lines = text.splitlines()
    start = 1 if lines and lines[0].startswith("#!") else 0
    want = header_lines(prefix, suffix)
    return lines[start : start + 2] == list(want)


def _variant_header_length(lines: list[str], start: int, prefix: str, suffix: str) -> int:
    """Return 2 when the file starts with the copyright line and a non-canonical SPDX line.

    Such a header (for example an earlier spelling of the identifier) is rewritten in place
    rather than duplicated. Return 0 when there is no header to replace.
    """
    one, _ = header_lines(prefix, suffix)
    if len(lines) < start + 2 or lines[start].rstrip("\n") != one:
        return 0
    second = lines[start + 1].rstrip("\n")
    if second.startswith(f"{prefix}SPDX-License-Identifier:") and second.endswith(suffix):
        return 2
    return 0


def apply_header(text: str, prefix: str, suffix: str) -> str:
    """Return `text` with the header inserted after any shebang; other bytes are unchanged.

    A header whose SPDX line differs from the canonical one is replaced in place.
    """
    lines = text.splitlines(keepends=True)
    start = 1 if lines and lines[0].startswith("#!") else 0
    one, two = header_lines(prefix, suffix)
    block = [one + "\n", two + "\n"]
    variant = _variant_header_length(lines, start, prefix, suffix)
    if variant:
        return "".join(lines[:start] + block + lines[start + variant :])
    rest = lines[start:]
    # Keep one blank line between the header and the first content line for
    # files that are not Python (PEP 8 style puts the docstring right after).
    if prefix != "# " and rest and rest[0].strip():
        block.append("\n")
    return "".join(lines[:start] + block + rest)


def iter_files(paths: list[Path]) -> Iterator[Path]:
    """Yield every file under `paths` in sorted order, skipping exempt directories."""
    for base in paths:
        if base.is_file():
            yield base
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in EXEMPT_DIR_PARTS)
            for name in sorted(filenames):
                yield Path(dirpath) / name


def main(argv: list[str]) -> int:
    """Run the check or apply pass over `argv` paths; return the process exit code."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="report files without the header")
    mode.add_argument("--apply", action="store_true", help="insert the header where it is missing")
    parser.add_argument(
        "paths", nargs="*", default=[str(ROOT)], help="files or directories (default: repository)"
    )
    args = parser.parse_args(argv)
    roots = [Path(p).resolve() for p in args.paths]
    for base in roots:
        if not base.exists():
            print(f"error: {base}: no such file or directory", file=sys.stderr)
            return 2
        if not base.is_relative_to(ROOT):
            print(f"error: {base}: outside the repository {ROOT}", file=sys.stderr)
            return 2

    missing: list[Path] = []
    covered = exempt = unknown = 0
    for path in iter_files(roots):
        if is_exempt(path):
            exempt += 1
            continue
        style = style_for(path)
        if style is None:
            unknown += 1
            print(f"UNKNOWN {path.relative_to(ROOT)}", file=sys.stderr)
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            exempt += 1
            continue
        covered += 1
        if has_header(text, *style):
            continue
        if args.apply:
            path.write_text(apply_header(text, *style), encoding="utf-8")
            print(f"APPLIED {path.relative_to(ROOT)}")
        else:
            missing.append(path)

    for path in missing:
        print(f"MISSING {path.relative_to(ROOT)}")
    print(f"covered={covered} missing={len(missing)} exempt={exempt} unknown={unknown}")
    if unknown:
        print(
            "every file type must be either styled or exempt; add it to tools/header.py",
            file=sys.stderr,
        )
        return 1
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

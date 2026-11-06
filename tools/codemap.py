#!/usr/bin/env python3
# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Generate CODEMAP.md, a navigational map of the repository, from the tree itself.

Usage:
    python3 tools/codemap.py            write CODEMAP.md
    python3 tools/codemap.py --check    exit 1 when CODEMAP.md is not what the tree generates
    python3 tools/codemap.py --stdout   print the map instead of writing it

Sections, each derived from a single source so the map cannot drift from the code:

  Languages       files and lines per language over source files; hidden paths, recorded data
                  (evidence, results, cassettes), vendored code and CODEMAP.md itself are excluded
  Layout          every top-level directory (tracked hidden ones such as .githooks included) and
                  every described directory down to depth 3, one line each, from the first heading
                  of its README.md, else the first docstring line of its __init__.py, else the
                  crate description, else the layout table in CLAUDE.md, else the first sentence
                  of the leading # comment of the directory's only file; a top-level directory
                  with none of these names its described subdirectories; nothing below recorded
                  data or vendored code is listed
  Crates          workspace members from Cargo.toml with descriptions and path dependencies
  Make targets    targets, prerequisites and the descriptions in the Makefile header comment

Files are listed with `git ls-files --cached --others --exclude-standard`, so ignored build output
never appears; outside a git work tree a walk with the same fixed exclusions is used. The output
depends only on the listed files' paths and contents: no timestamps, no host data, sorted order.
Exit codes: 0 success, 1 stale map under --check, 2 usage or input error.
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = "CODEMAP.md"
HEADER = (
    "<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->",
    "<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->",
)
BANNER = (
    "<!-- Generated from the tree by tools/codemap.py. Do not edit by hand; "
    "run `python3 tools/codemap.py` to regenerate. -->"
)

LANG_BY_EXT = {
    ".rs": "Rust",
    ".py": "Python",
    ".pyi": "Python",
    ".tmpl": "Python template",
    ".sh": "Shell",
    ".kvx": "KVX",
    ".toml": "TOML",
    ".md": "Markdown",
    ".mmd": "Mermaid",
    ".json": "JSON",
    ".jsonl": "JSON Lines",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".cff": "YAML",
    ".lock": "Lock file",
    ".txt": "Text",
}
LANG_BY_NAME = {
    "Makefile": "Makefile",
    "LICENSE": "Text",
    "NOTICE": "Text",
    "bin": "Task data",
    "rules": "Task data",
}
# Directory names that hold recorded data or third-party code: listed in the layout, not counted.
DATA_DIRS = {"evidence", "results", "cassettes", "vendor"}
# Used only when git is unavailable; mirrors the build-output entries of .gitignore.
WALK_SKIP = {"target", "build", "xbuild", "out", "artifacts", "__pycache__", "node_modules"}
MAX_LAYOUT_DEPTH = 3


def list_files(root: Path, hidden: bool = False) -> list[str]:
    """Return repository-relative POSIX paths of every non-ignored file, sorted.

    Paths with a hidden component are dropped unless `hidden` is true.
    """
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            capture_output=True,
            check=True,
        )
        paths = [p for p in result.stdout.decode("utf-8").split("\0") if p]
    except (OSError, subprocess.CalledProcessError):
        paths = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in WALK_SKIP and not d.startswith(".")]
            rel = Path(dirpath).relative_to(root)
            paths += [(rel / name).as_posix() for name in filenames]
    return sorted(
        p
        for p in paths
        if (hidden or not any(part.startswith(".") for part in p.split("/")))
        and (root / p).is_file()
    )


def language(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    if name in LANG_BY_NAME:
        return LANG_BY_NAME[name]
    return LANG_BY_EXT.get(Path(name).suffix, "Other")


def count_lines(data: bytes) -> int:
    if not data:
        return 0
    return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)


def language_table(root: Path, files: list[str]) -> list[str]:
    totals: dict[str, list[int]] = {}
    for path in files:
        parts = path.split("/")
        if path == OUTPUT or any(part in DATA_DIRS for part in parts[:-1]):
            continue
        data = (root / path).read_bytes()
        lang = "Binary" if b"\0" in data else language(path)
        entry = totals.setdefault(lang, [0, 0])
        entry[0] += 1
        entry[1] += 0 if lang == "Binary" else count_lines(data)
    rows = sorted(totals.items(), key=lambda item: (-item[1][1], -item[1][0], item[0]))
    lines = ["| Language | Files | Lines |", "|---|---:|---:|"]
    lines += [f"| {lang} | {n_files} | {n_lines} |" for lang, (n_files, n_lines) in rows]
    all_files = sum(v[0] for v in totals.values())
    all_lines = sum(v[1] for v in totals.values())
    lines.append(f"| **Total** | **{all_files}** | **{all_lines}** |")
    return lines


def plain(text: str) -> str:
    """Strip inline Markdown (links, code spans, emphasis) and escape table pipes."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = text.replace("`", "").replace("**", "")
    return text.strip().replace("|", "\\|")


def readme_heading(directory: Path) -> str | None:
    readme = directory / "README.md"
    if not readme.is_file():
        return None
    for line in readme.read_text(encoding="utf-8").splitlines():
        match = re.match(r"#{1,6}\s+(.+?)\s*#*\s*$", line)
        if match:
            return plain(match.group(1))
    return None


def package_docstring(directory: Path) -> str | None:
    init = directory / "__init__.py"
    if not init.is_file():
        return None
    try:
        doc = ast.get_docstring(ast.parse(init.read_text(encoding="utf-8")))
    except SyntaxError:
        return None
    return plain(doc.strip().splitlines()[0]) if doc and doc.strip() else None


def cargo_description(directory: Path) -> str | None:
    manifest = directory / "Cargo.toml"
    if not manifest.is_file():
        return None
    package = tomllib.loads(manifest.read_text(encoding="utf-8")).get("package", {})
    description = package.get("description")
    return plain(description) if isinstance(description, str) else None


def standard_layout(root: Path) -> dict[str, str]:
    """Directory purposes from the Layout table of CLAUDE.md, keyed by path without slash."""
    standard = root / "CLAUDE.md"
    if not standard.is_file():
        return {}
    table: dict[str, str] = {}
    for line in standard.read_text(encoding="utf-8").splitlines():
        match = re.match(r"\|\s*`([^`]+)/`\s*\|\s*(.+?)\s*\|\s*$", line)
        if match:
            table[match.group(1).rstrip("/")] = plain(match.group(2))
    return table


def sole_file_comment(root: Path, directory: str, files: list[str]) -> str | None:
    """First sentence of the leading `#` comment of the only file directly in `directory`.

    Shebang and source-header lines are skipped. Describes configuration directories such as
    .cargo, which hold one commented file and no README.
    """
    own = [f for f in files if f.rsplit("/", 1)[0] == directory and "/" in f]
    if len(own) != 1:
        return None
    try:
        lines = (root / own[0]).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    words: list[str] = []
    for line in lines:
        if line.startswith("#!") or "Copyright" in line or "SPDX-License-Identifier" in line:
            continue
        if not line.startswith("#"):
            if words or line.strip():
                break
            continue
        text = line.lstrip("#").strip()
        if not text and words:
            break
        if text:
            words.append(text)
    sentence = " ".join(words).split(". ", 1)[0].rstrip(".")
    return plain(sentence) if sentence else None


def purpose_line(directory: str, text: str) -> str:
    """Normalise a directory purpose for the layout table.

    Drops a leading "<directory>:" label that repeats the row's own path, capitalising what
    follows, and drops parenthetical references to specification tasks.
    """
    name = directory.rsplit("/", 1)[-1]
    for label in (directory, name):
        if text.lower().startswith(f"{label.lower()}:"):
            text = text[len(label) + 1 :].strip()
            text = text[:1].upper() + text[1:]
            break
    return re.sub(r"\s*\([^()]*\btasks?\b[^()]*\)", "", text).strip()


def layout_directories(files: list[str]) -> set[str]:
    """Directories to consider for the layout table, from repository-relative file paths.

    Every top-level directory is kept, hidden ones included. Below the top level, hidden
    directories and anything nested inside recorded data or vendored code are dropped, so
    third-party READMEs never become purpose lines.
    """
    directories: set[str] = set()
    for path in files:
        parts = path.split("/")[:-1]
        for depth in range(1, min(len(parts), MAX_LAYOUT_DEPTH) + 1):
            prefix = parts[:depth]
            if depth > 1 and (
                any(part.startswith(".") for part in prefix)
                or any(part in DATA_DIRS for part in prefix[:-1])
            ):
                break
            directories.add("/".join(prefix))
    return directories


def layout_table(root: Path, files: list[str]) -> list[str]:
    directories = layout_directories(files)
    standard = standard_layout(root)
    purposes: dict[str, str] = {}
    for directory in sorted(directories):
        absolute = root / directory
        purpose = (
            readme_heading(absolute)
            or package_docstring(absolute)
            or cargo_description(absolute)
            or standard.get(directory)
            or sole_file_comment(root, directory, files)
        )
        if purpose:
            purposes[directory] = purpose_line(directory, purpose)
    rows = []
    for directory in sorted(directories):
        purpose = purposes.get(directory)
        if purpose is None and "/" in directory:
            continue
        if purpose is None:
            children = sorted(
                d for d in purposes if d.startswith(f"{directory}/") and d.count("/") == 1
            )
            if len(children) == 1:
                purpose = f"Holds `{children[0]}/`: {purposes[children[0]]}"
            elif children:
                purpose = "Holds " + ", ".join(f"`{d}/`" for d in children) + "."
            else:
                purpose = "Not yet described."
        rows.append(f"| `{directory}/` | {purpose} |")
    return ["| Directory | Purpose |", "|---|---|", *rows]


def crate_table(root: Path) -> list[str]:
    manifest = root / "Cargo.toml"
    if not manifest.is_file():
        return ["No Cargo workspace."]
    workspace = tomllib.loads(manifest.read_text(encoding="utf-8")).get("workspace", {})
    lines = ["| Crate | Path | Depends on (workspace) | Description |", "|---|---|---|---|"]
    for member in sorted(workspace.get("members", [])):
        crate_manifest = root / member / "Cargo.toml"
        if not crate_manifest.is_file():
            lines.append(f"| - | `{member}/` | - | Listed in Cargo.toml; manifest missing. |")
            continue
        crate = tomllib.loads(crate_manifest.read_text(encoding="utf-8"))
        name = crate.get("package", {}).get("name", member)
        deps = crate.get("dependencies", {})
        internal = sorted(
            dep
            for dep, spec in deps.items()
            if isinstance(spec, dict)
            and ("path" in spec or (spec.get("workspace") and dep.startswith("cwr-")))
        )
        description = cargo_description(root / member) or "-"
        lines.append(
            f"| `{name}` | `{member}/` | {', '.join(f'`{d}`' for d in internal) or '-'} "
            f"| {description} |"
        )
    return lines


def make_targets(root: Path) -> list[str]:
    makefile = root / "Makefile"
    if not makefile.is_file():
        return ["No Makefile."]
    text = makefile.read_text(encoding="utf-8")
    targets: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^([A-Za-z][\w-]*)\s*:(?!=)\s*(.*)$", line)
        if match:
            targets.setdefault(match.group(1), match.group(2).split("#", 1)[0].strip())
    descriptions: dict[str, str] = {}
    for line in text.splitlines():
        if not line.startswith("#"):
            continue
        match = re.match(r"^#\s+([A-Za-z][\w-]*)\s+(.+)$", line)
        if match and match.group(1) in targets:
            descriptions.setdefault(match.group(1), plain(match.group(2)))
    lines = ["| Target | Prerequisites | Does |", "|---|---|---|"]
    for name, prerequisites in targets.items():
        prereq = ", ".join(f"`{p}`" for p in prerequisites.split()) or "-"
        lines.append(f"| `make {name}` | {prereq} | {descriptions.get(name, '-')} |")
    return lines


def render(root: Path) -> str:
    files = list_files(root)
    sections = [
        *HEADER,
        "",
        BANNER,
        "",
        "# CWR code map",
        "",
        "A map of the Compiled World Runtime repository, generated from the tree. The "
        "specification is [spec/cwr/spec.kvx](spec/cwr/spec.kvx), and the rules for changing the "
        "code are in [CONTRIBUTING.md](CONTRIBUTING.md).",
        "",
        "## Languages",
        "",
        "Counts cover source files only. Hidden paths, ignored build output, recorded data "
        "(evidence, results and cassettes) and vendored code are excluded.",
        "",
        *language_table(root, files),
        "",
        "## Layout",
        "",
        *layout_table(root, list_files(root, hidden=True)),
        "",
        "## Crates",
        "",
        *crate_table(root),
        "",
        "## Make targets",
        "",
        *make_targets(root),
    ]
    return "\n".join(sections) + "\n"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="fail when CODEMAP.md is stale")
    mode.add_argument("--stdout", action="store_true", help="print instead of writing")
    args = parser.parse_args(argv)
    try:
        text = render(ROOT)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        print(f"codemap: {error}", file=sys.stderr)
        return 2
    target = ROOT / OUTPUT
    if args.stdout:
        sys.stdout.write(text)
        return 0
    if args.check:
        current = target.read_text(encoding="utf-8") if target.is_file() else ""
        if current != text:
            print(f"{OUTPUT} is stale; run python3 tools/codemap.py", file=sys.stderr)
            return 1
        print(f"{OUTPUT} is current")
        return 0
    target.write_text(text, encoding="utf-8")
    print(f"wrote {OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

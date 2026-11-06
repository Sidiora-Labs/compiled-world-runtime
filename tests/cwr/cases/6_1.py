#!/usr/bin/env python3
# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Task 6.1 driver: documentation and reproducible release artifacts.

Case CWR-020 (assertions a4 and a5). Exercises clauses 14.2 and 14.3 against the real release:

1. Builds the `cwr` binary with `cargo build --release --locked` and bundles the tree with the real
   bundler, `python3 tools/release.py --output <scratch>`, which writes `cwr-<version>.tar.gz`
   (sources plus `bin/cwr`) and its `.sha256` file.
2. Executes every command of every `sh` block in README.md, RELEASE.md and the three guides under
   docs/cwr, exactly as written, in one bash shell per document. README.md runs in the directory that
   holds the bundle (it verifies, unpacks and installs it); the others run in the unpacked bundle with
   only the installed `cwr` on PATH and a private HOME. RELEASE.md packages the CLI and SDK from the
   bundle's own sources, runs `cwr release pin --write` (which must leave benchmark/release.kvx
   byte-identical: the pins reproduce) and `cwr release check` (which must exit 0).
3. Checks every relative link of the documentation set and the status markers against the evidence
   registry. The shape is fixed: every top-level document and guide carries exactly one marker line,
   the first prose line under its title, naming its qualification cases; the experiments guide also
   carries the Verified marker of the evidence registry case in its qualification section; no other
   marker exists. A Verified marker must link the registry's accepted attempt record for its case,
   an Implemented marker must link existing source and a Proposed marker must list source that does
   not exist yet. No line claims a PASS without an attempt record.
4. Re-runs `cwr release check --json` in the bundle, then on a tampered copy of the release file in
   which every pin is nulled, altered or pointed at a missing input, and requires each diagnostic code
   by name; the codes and messages must equal the [diagnostic.*] sections and the RELEASE.md table.

Prints one line per clause, `CLAUSE <req.ac> PASS|FAIL <evidence>`, plus `ASSERT CWR-020.a4|a5` lines
for tests/cwr/qualify.py, and exits 0 only when every clause passes. Honours CARGO_TARGET_DIR (default:
the workspace target dir). Set CWR_6_1_KEEP=1 to keep the scratch directory. Python 3.12, standard
library only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "core"))

from cwr_core import kvx  # noqa: E402  importable only after the sys.path insert above

CASE = "CWR-020"
CLAUSES = ["14.2", "14.3"]
RELEASE_KVX = "benchmark/release.kvx"
REGISTRY = "tests/cwr/registry.kvx"
# Executable documents, in run order, with the directory each runs in.
RUN_ORDER = [
    ("README.md", "dist"),
    ("RELEASE.md", "bundle"),
    ("docs/cwr/world-source.md", "bundle"),
    ("docs/cwr/kernel.md", "bundle"),
    ("docs/cwr/experiments.md", "bundle"),
]
DOC_SET = [doc for doc, _ in RUN_ORDER] + ["docs/cwr/README.md", "CHANGELOG.md"]
GUIDES = ["docs/cwr/world-source.md", "docs/cwr/kernel.md", "docs/cwr/experiments.md"]
# Documents that carry exactly one status marker, the first prose line under the title, naming
# their qualification cases.
MARKED = ["README.md", "RELEASE.md", "docs/cwr/README.md", *GUIDES]
# The one marker allowed below a title: the qualification section of the experiments guide carries
# the Verified marker of the evidence registry case.
SECTION_MARKERS = {"docs/cwr/experiments.md": ["CWR-019"]}
# Source areas the documentation must describe with links (clause 14.2).
AREAS = {
    "compiler": "crates/cwr-compiler/",
    "kernel": "crates/cwr-kernel/",
    "model surface": "crates/cwr-kernel/src/surface/",
    "harness": "benchmark/harness/",
}
PER_DOC_TIMEOUT = 3600
FENCE = re.compile(r"^```(\S*)\s*$")
MARKER = re.compile(r"^> \*\*(Verified|Implemented|Proposed)\b([^*]*)\*\*(.*)$")
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
TICKED = re.compile(r"`([A-Za-z0-9_./-]+\.[A-Za-z0-9]+)`")
CASE_ID = re.compile(r"\bCWR-0[0-9]{2}\b")
HEREDOC = re.compile(r"<<-?\s*'?\"?([A-Za-z_][A-Za-z0-9_]*)'?\"?")
NEGATION = re.compile(
    r"\b(never|not|no|nor|only|until|unless|without|UNRUN|cannot|before)\b", re.IGNORECASE
)
DIAG = re.compile(r"\bCWR-R[0-9]{3}\b")
CODE_LINE = re.compile(r"^(CWR-R[0-9]{3}): ")
MARK = "@@CWR61"


class Results:
    """Accumulates evidence per clause; a clause passes when it has evidence and no failure."""

    def __init__(self) -> None:
        self.ok: dict[str, list[str]] = {c: [] for c in CLAUSES}
        self.bad: dict[str, list[str]] = {c: [] for c in CLAUSES}
        self.asserts: dict[str, list[tuple[bool, str]]] = {"a4": [], "a5": []}

    def check(self, clause: str, condition: bool, evidence: str, assertion: str = "") -> bool:
        (self.ok if condition else self.bad)[clause].append(evidence)
        print(f"check {clause} {'PASS' if condition else 'FAIL'} {evidence}", flush=True)
        if assertion:
            self.asserts[assertion].append((condition, evidence))
        return condition

    def report(self) -> int:
        for key, items in self.asserts.items():
            good = bool(items) and all(c for c, _ in items)
            detail = "; ".join(e for c, e in items if not c) or "; ".join(e for _, e in items)
            print(f"ASSERT {CASE}.{key} {'PASS' if good else 'FAIL'} {detail[:2000]}")
        failed = 0
        for c in CLAUSES:
            if self.bad[c] or not self.ok[c]:
                failed += 1
                print(f"CLAUSE {c} FAIL " + " | ".join(self.bad[c] or ["no evidence"]))
            else:
                print(f"CLAUSE {c} PASS " + " | ".join(self.ok[c]))
        return 1 if failed else 0


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def target_dir() -> Path:
    raw = os.environ.get("CARGO_TARGET_DIR")
    path = Path(raw) if raw else REPO / "target"
    return path if path.is_absolute() else REPO / path


def run(cmd: list[str], cwd: Path, env: dict[str, str] | None = None, timeout: int = 1800):
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        return 124, str(error.stdout or ""), f"timeout after {timeout}s"
    except OSError as error:
        return 127, "", str(error)
    return proc.returncode, proc.stdout, proc.stderr


def tail(text: str, lines: int = 3) -> str:
    kept = [line for line in text.strip().splitlines() if line.strip()][-lines:]
    return " / ".join(kept)[:600]


# ---------------------------------------------------------------------------------------------
# Markdown


def blocks(text: str) -> list[tuple[str, int, list[str]]]:
    """Fenced code blocks as (language, first line number, lines)."""
    out, lang, start, body = [], None, 0, []
    for number, line in enumerate(text.splitlines(), 1):
        match = FENCE.match(line.strip()) if line.strip().startswith("```") else None
        if lang is None and match:
            lang, start, body = match.group(1), number, []
        elif lang is not None and line.strip() == "```":
            out.append((lang, start, body))
            lang = None
        elif lang is not None:
            body.append(line)
    return out


def prose_lines(text: str) -> list[tuple[int, str]]:
    """Lines outside fenced code blocks, with line numbers."""
    out, fenced = [], False
    for number, line in enumerate(text.splitlines(), 1):
        if line.strip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            out.append((number, line))
    return out


def commands(lines: list[str]) -> list[str]:
    """Split an sh block into commands: backslash continuations and heredocs stay together."""
    out: list[str] = []
    current: list[str] = []
    delimiter = None
    for raw in lines:
        line = raw.rstrip()
        if delimiter is not None:
            current.append(line)
            if line.strip() == delimiter:
                delimiter = None
                out.append("\n".join(current))
                current = []
            continue
        if not current and (not line.strip() or line.lstrip().startswith("#")):
            continue
        current.append(line)
        heredoc = HEREDOC.search(line)
        if heredoc:
            delimiter = heredoc.group(1)
            continue
        if line.endswith("\\"):
            continue
        out.append("\n".join(current))
        current = []
    if current:
        out.append("\n".join(current))
    return out


def doc_commands(doc: str) -> list[tuple[int, str]]:
    text = (REPO / doc).read_text(encoding="utf-8")
    found = []
    for lang, start, body in blocks(text):
        if lang == "sh":
            found += [(start, c) for c in commands(body)]
    return found


def run_document(doc: str, cwd: Path, env: dict[str, str], log: Path) -> list[dict]:
    """Runs every sh command of `doc` in one bash shell; returns one result per command."""
    cmds = doc_commands(doc)
    script = []
    for index, (_, command) in enumerate(cmds):
        script.append(command)
        script.append(f"printf '\\n{MARK} %d %d@@\\n' {index} \"$?\"")
    script_path = log.with_suffix(".sh")
    script_path.write_text("\n".join(script) + "\n", encoding="utf-8")
    code, stdout, stderr = run(
        ["bash", "--noprofile", "--norc", str(script_path)], cwd, env, PER_DOC_TIMEOUT
    )
    log.write_text(stdout + "\n--- stderr ---\n" + stderr, encoding="utf-8")
    results: list[dict] = []
    chunk: list[str] = []
    codes: dict[int, tuple[int, str]] = {}
    for line in stdout.splitlines():
        match = re.fullmatch(rf"{MARK} (\d+) (\d+)@@", line)
        if match:
            codes[int(match.group(1))] = (int(match.group(2)), "\n".join(chunk))
            chunk = []
        else:
            chunk.append(line)
    for index, (start, command) in enumerate(cmds):
        exit_code, output = codes.get(index, (-1, f"never reached (shell exit {code})"))
        results.append(
            {"doc": doc, "line": start, "command": command, "exit": exit_code, "output": output}
        )
    if code != 0 and not results:
        results.append(
            {"doc": doc, "line": 0, "command": "<shell>", "exit": code, "output": stderr}
        )
    return results


# ---------------------------------------------------------------------------------------------
# Static documentation checks


def resolve_link(doc: str, target: str) -> Path | None:
    if re.match(r"^[a-z]+:", target) or target.startswith("#"):
        return None
    path = target.split("#", 1)[0]
    return (REPO / doc).parent.joinpath(path).resolve()


def check_links(results: Results) -> None:
    broken, evidence, count = [], [], 0
    for doc in DOC_SET:
        text = (REPO / doc).read_text(encoding="utf-8")
        for number, line in prose_lines(text):
            for target in LINK.findall(line):
                path = resolve_link(doc, target)
                if path is None:
                    continue
                count += 1
                if not path.exists():
                    broken.append(f"{doc}:{number} {target}")
                elif "tests/cwr/evidence/" in path.as_posix():
                    evidence.append(f"{doc}:{number}")
    results.check(
        "14.2",
        not broken,
        f"{count} relative links in {len(DOC_SET)} documents resolve"
        if not broken
        else f"broken links: {', '.join(broken[:20])}",
        "a5",
    )


def registry_cases() -> dict[str, dict]:
    sections = kvx.load(REPO / REGISTRY)
    return {name[5:]: body for name, body in sections.items() if name.startswith("case.")}


def title_line(text: str) -> int:
    """Line number of the document title (the first ATX level-one heading), or 0."""
    for number, line in prose_lines(text):
        if line.startswith("# "):
            return number
    return 0


def check_marker_shape(doc: str, text: str, problems: list[str]) -> list[tuple[int, str]]:
    """Returns the marker lines of `doc` after checking their placement; records any problem."""
    prose = prose_lines(text)
    markers = []
    for number, line in prose:
        if MARKER.match(line):
            markers.append((number, line))
        elif re.match(r"^> \*\*(Pending|Verified|Implemented|Proposed)", line):
            problems.append(f"{doc}:{number} malformed status marker")
    title = title_line(text)
    below = [(n, line) for n, line in prose if n > title and line.strip()]
    top = below[0] if below and MARKER.match(below[0][1]) else None
    allowed = SECTION_MARKERS.get(doc, [])
    if doc in MARKED:
        if top is None:
            problems.append(f"{doc} does not open with a status marker under its title")
        elif not CASE_ID.search(top[1]):
            problems.append(f"{doc}:{top[0]} status marker names no qualification case")
    elif top is not None:
        problems.append(f"{doc}:{top[0]} carries a status marker but is not a marked document")
    extra = [(n, line) for n, line in markers if top is None or n != top[0]]
    sections = [
        (n, line) for n, line in extra if line.startswith("> **Verified") and CASE_ID.findall(line)
    ]
    if sorted({c for _, line in sections for c in CASE_ID.findall(line)}) != sorted(allowed):
        problems.append(
            f"{doc} section markers {[n for n, _ in sections]} must be exactly the Verified "
            f"markers of {allowed or 'no case'}"
        )
    for number, line in extra:
        if (number, line) not in sections:
            problems.append(f"{doc}:{number} status marker below the title")
    return markers


def check_markers(results: Results) -> None:
    cases = registry_cases()
    problems, counts, areas = [], {"Verified": 0, "Implemented": 0, "Proposed": 0}, set()
    for doc in DOC_SET:
        text = (REPO / doc).read_text(encoding="utf-8")
        for number, line in check_marker_shape(doc, text, problems):
            match = MARKER.match(line)
            if not match:
                continue
            kind = match.group(1)
            counts[kind] += 1
            where = f"{doc}:{number}"
            links = [(t, resolve_link(doc, t)) for t in LINK.findall(line)]
            links = [(t, p) for t, p in links if p is not None]
            named = sorted(set(CASE_ID.findall(line)))
            for target, path in links:
                rel = path.relative_to(REPO).as_posix() if path.is_relative_to(REPO) else ""
                for area, prefix in AREAS.items():
                    if rel.startswith(prefix) and path.exists():
                        areas.add(area)
                if not path.exists():
                    problems.append(f"{where} links missing {target}")
            if kind == "Verified":
                records = [
                    p.relative_to(REPO).as_posix()
                    for _, p in links
                    if p.exists() and "tests/cwr/evidence/" in p.as_posix() and p.suffix == ".json"
                ]
                if not named or not records:
                    problems.append(f"{where} Verified without a case and an attempt record link")
                for case in named:
                    entry = cases.get(case, {})
                    if entry.get("status") != "PASS" or entry.get("evidence") not in records:
                        problems.append(
                            f"{where} Verified {case} but the registry holds status "
                            f"{entry.get('status')} with evidence {entry.get('evidence')}"
                        )
                    code, out, err = run(
                        [sys.executable, "-I", "tests/cwr/check_evidence.py", "--status", case],
                        REPO,
                    )
                    if code != 0:
                        problems.append(f"{where} checker refuses {case}: {tail(out + err, 2)}")
            elif kind == "Implemented":
                sources = [p for _, p in links if p.exists() and p.is_file()]
                if not sources:
                    problems.append(f"{where} Implemented without a link to existing source")
                if named and all(cases.get(c, {}).get("status") == "PASS" for c in named):
                    problems.append(
                        f"{where} says not qualified but {', '.join(named)} hold PASS: mark Verified"
                    )
            else:
                proposed = line.split("Proposed source", 1)[1] if "Proposed source" in line else ""
                for path in TICKED.findall(proposed):
                    if (REPO / path).exists():
                        problems.append(f"{where} lists {path} as proposed but it exists")
    missing_areas = sorted(set(AREAS) - areas)
    if missing_areas:
        problems.append(f"no marker links source for: {', '.join(missing_areas)}")
    results.check(
        "14.2",
        not problems,
        f"status markers consistent with {REGISTRY}: {counts['Verified']} Verified, "
        f"{counts['Implemented']} Implemented, {counts['Proposed']} Proposed; one marker under "
        f"each title of {len(MARKED)} documents; source linked for {', '.join(sorted(areas))}"
        if not problems
        else "marker problems: " + "; ".join(problems[:25]),
        "a5",
    )


def check_claims(results: Results) -> None:
    unsupported = []
    for doc in DOC_SET:
        text = (REPO / doc).read_text(encoding="utf-8")
        for number, line in prose_lines(text):
            if not re.search(r"\bPASS\b", line):
                continue
            records = [
                t for t in LINK.findall(line) if "tests/cwr/evidence/" in t and t.endswith(".json")
            ]
            if records or NEGATION.search(line):
                continue
            unsupported.append(f"{doc}:{number}")
    results.check(
        "14.2",
        not unsupported,
        "no document claims a PASS without linking its attempt record"
        if not unsupported
        else f"PASS claimed without an attempt record at {', '.join(unsupported)}",
    )


# ---------------------------------------------------------------------------------------------
# Release checks


def versions() -> dict[str, str | None]:
    workspace = tomllib.loads((REPO / "Cargo.toml").read_text(encoding="utf-8"))
    cli = tomllib.loads((REPO / "crates/cwr-cli/Cargo.toml").read_text(encoding="utf-8"))
    sdk = tomllib.loads((REPO / "sdk/python/pyproject.toml").read_text(encoding="utf-8"))
    cli_version = cli.get("package", {}).get("version")
    if isinstance(cli_version, dict) and cli_version.get("workspace"):
        cli_version = workspace.get("workspace", {}).get("package", {}).get("version")
    return {
        "crates/cwr-cli/Cargo.toml": cli_version,
        "sdk/python/pyproject.toml": sdk.get("project", {}).get("version"),
        "benchmark/release.kvx meta.release": kvx.load(REPO / RELEASE_KVX)["meta"].get("release"),
    }


def tampered_release(text: str) -> str:
    """Nulls or alters every pin; points the CLI artifact at a missing file."""
    section = ""
    out = []
    alter = {
        ("suite", "digest"): '"sha256:' + "0" * 64 + '"',
        ("pin.kernel", "revision"): '"sha256:' + "1" * 64 + '"',
        ("meta", "release"): '"9.9.9"',
        ("artifact.cli", "path"): '"artifacts/missing-cwr-binary"',
    }
    null = {
        ("pin.compiler", "revision"),
        ("pin.sdk", "revision"),
        ("pin.cli", "revision"),
        ("pin.contract", "revision"),
        ("dependencies", "cargo_lock"),
        ("build.refworld_control", "hash"),
        ("build.x_drift", "hashes"),
        ("build.x_one_primitive", "hash"),
        ("build.x_version_only", "hashes"),
        ("build.x_silent_change", "hashes"),
        ("build.upgrade_pairs", "hashes"),
        ("cassettes", "digest"),
        ("artifact.sdk", "sha256"),
        ("results", "digests"),
        ("meta", "spec_sha256"),
        ("evidence", "preregistration_sha256"),
        ("evidence", "closure"),
    }
    for line in text.splitlines():
        header = re.fullmatch(r"\[(.+)\]", line)
        if header:
            section = header.group(1)
        key = line.split(" = ", 1)[0] if " = " in line else None
        if key and (section, key) in alter:
            line = f"{key} = {alter[(section, key)]}"
        elif key and (section, key) in null:
            line = f"{key} = null"
        out.append(line)
    return "\n".join(out) + "\n"


def check_release(results: Results, cwr: Path, bundle: Path, scratch: Path, env: dict) -> list:
    """Positive and tampered `cwr release check` runs; returns the verified pins."""
    found = versions()
    distinct = {v for v in found.values()}
    results.check(
        "14.3",
        len(distinct) == 1 and None not in distinct,
        f"versions agree: {json.dumps(found, sort_keys=True)}",
    )
    code, out, err = run([str(cwr), "release", "check", "--json"], bundle, env)
    try:
        report = json.loads(out)
    except json.JSONDecodeError:
        results.check("14.3", False, f"cwr release check printed no JSON: {tail(err)}")
        return []
    failing = [p for p in report["pins"] if p["status"] != "ok"]
    results.check(
        "14.3",
        code == 0 and not failing,
        f"cwr release check in the unpacked bundle verified {len(report['pins'])} pins "
        "(revisions, Cargo.lock, control, drift, one-primitive, version-only, silent-change and "
        "upgrade-pair Build hashes, cassette and suite digests, CLI and SDK checksums, results, "
        "spec and pre-registration digests)"
        if code == 0 and not failing
        else f"cwr release check exit {code}; failing: "
        + "; ".join(f"{p['diagnostic']} {p['key']}: {str(p['detail'])[:300]}" for p in failing),
    )
    emitted = {p["diagnostic"]: p["message"] for p in report["pins"]}

    tampered = scratch / "release-tampered.kvx"
    tampered.write_text(
        tampered_release((bundle / RELEASE_KVX).read_text(encoding="utf-8")), encoding="utf-8"
    )
    code, out, err = run(
        [str(cwr), "release", "check", "--claim", "--json", "--file", str(tampered)], bundle, env
    )
    try:
        bad = json.loads(out)
    except json.JSONDecodeError:
        results.check("14.3", False, f"tampered release check printed no JSON: {tail(err)}")
        return report["pins"]
    named = {p["diagnostic"] for p in bad["pins"] if p["status"] == "fail"}
    emitted.update({p["diagnostic"]: p["message"] for p in bad["pins"]})
    expected = set(emitted)
    code_text, out_text, _ = run(
        [str(cwr), "release", "check", "--claim", "--file", str(tampered)], bundle, env
    )
    printed = {m.group(1) for m in map(CODE_LINE.match, out_text.splitlines()) if m}
    results.check(
        "14.3",
        code == 2 and code_text == 2 and named == expected and printed == expected,
        f"a release file with every input missing, altered or unreadable makes cwr release "
        f"check exit 2 naming all {len(expected)} diagnostics ({', '.join(sorted(expected))})"
        if named == expected and printed == expected
        else f"tampered check exit {code}: named {sorted(named)} printed {sorted(printed)} "
        f"expected {sorted(expected)}",
    )
    pins = kvx.load(REPO / RELEASE_KVX)
    declared = {
        name.split(".", 1)[1]: body.get("message")
        for name, body in pins.items()
        if name.startswith("diagnostic.")
    }
    release_md = (REPO / "RELEASE.md").read_text(encoding="utf-8")
    table = set(DIAG.findall(release_md))
    results.check(
        "14.3",
        declared == emitted and table == set(emitted),
        f"{len(declared)} diagnostics agree between cwr release, {RELEASE_KVX} and RELEASE.md"
        if declared == emitted and table == set(emitted)
        else f"diagnostic tables disagree: kvx-only {sorted(set(declared.items()) - set(emitted.items()))} "
        f"cli-only {sorted(set(emitted.items()) - set(declared.items()))} "
        f"RELEASE.md {sorted(table ^ set(emitted))}",
    )
    return report["pins"]


def write_attempt(pins: list) -> None:
    out = os.environ.get("CWR_EVIDENCE_DIR")
    if not out:
        return
    values = {p["key"]: p.get("computed") for p in pins}
    control = values.get("build.refworld_control.hash")
    builds = [control] if isinstance(control, str) else []
    for key in ("build.upgrade_pairs.hashes",):
        for pair in (values.get(key) or {}).values():
            builds += [pair.get("a"), pair.get("b")]
    for key in (
        "build.x_drift.hashes",
        "build.x_version_only.hashes",
        "build.x_silent_change.hashes",
    ):
        builds += list((values.get(key) or {}).values())
    one = values.get("build.x_one_primitive.hash")
    if isinstance(one, str):
        builds.append(one)
    world = kvx.load(REPO / "examples/refworld/world.kvx").get("world", {})
    attempt = {
        "build_hash": control,
        "builds": [b for b in builds if isinstance(b, str)],
        "world": {"name": world.get("name"), "version": world.get("version")},
        "cassette_digest": values.get("cassettes.digest"),
    }
    Path(out, "attempt.json").write_text(json.dumps(attempt, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------------------------


def main() -> int:
    results = Results()
    target = target_dir()
    keep = os.environ.get("CWR_6_1_KEEP") == "1"
    scratch = Path(tempfile.mkdtemp(prefix="cwr-6_1-"))
    print(f"scratch {scratch}", flush=True)
    try:
        return drive(results, target, scratch)
    finally:
        if not keep:
            shutil.rmtree(scratch, ignore_errors=True)


def drive(results: Results, target: Path, scratch: Path) -> int:
    check_links(results)
    check_markers(results)
    check_claims(results)

    base_env = {k: v for k, v in os.environ.items() if k not in {"CWR_HOME", "PYTHONPATH"}}
    base_env["CARGO_TARGET_DIR"] = str(target)
    code, out, err = run(
        ["cargo", "build", "--release", "--locked", "-p", "cwr-cli"], REPO, base_env, 3600
    )
    if code != 0:
        results.check("14.2", False, f"cargo build --release --locked failed: {tail(err)}")
        results.check("14.3", False, "no release binary to package")
        return results.report()

    dist = scratch / "dist"
    code, out, err = run(
        [sys.executable, "-I", "tools/release.py", "--output", str(dist)], REPO, base_env, 1800
    )
    wrote = re.search(r"^wrote (\S+\.tar\.gz) (sha256:[0-9a-f]{64})$", out, re.MULTILINE)
    if code != 0 or not wrote:
        results.check("14.2", False, f"tools/release.py refused: {tail(out + err, 6)}")
        results.check("14.3", False, "no release bundle")
        return results.report()
    tarball = Path(wrote.group(1))
    label = tarball.name.removesuffix(".tar.gz")
    bundle = dist / label
    pins = kvx.load(REPO / RELEASE_KVX)
    release = pins["meta"].get("release")
    results.check(
        "14.3",
        label == f"cwr-{release}",
        f"tools/release.py bundled {tarball.name} {wrote.group(2)} for release {release}",
    )

    home = scratch / "home"
    home.mkdir()
    env = dict(base_env)
    env["HOME"] = str(home)
    env["PATH"] = f"{home / '.local/bin'}:{base_env.get('PATH', '/usr/bin:/bin')}"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    real_home = Path(os.environ.get("HOME", "/root"))
    env.setdefault("CARGO_HOME", str(real_home / ".cargo"))
    env.setdefault("RUSTUP_HOME", str(real_home / ".rustup"))

    logs = scratch / "logs"
    logs.mkdir()
    all_results: list[dict] = []
    release_before = None
    for doc, where in RUN_ORDER:
        cwd = dist if where == "dist" else bundle
        if not cwd.is_dir():
            all_results.append(
                {"doc": doc, "line": 0, "command": "<cwd>", "exit": -1, "output": f"{cwd} missing"}
            )
            continue
        if doc == "RELEASE.md":
            release_before = (bundle / RELEASE_KVX).read_bytes()
        log = logs / (doc.replace("/", "_") + ".log")
        ran = run_document(doc, cwd, env, log)
        all_results += ran
        failed = [r for r in ran if r["exit"] != 0]
        results.check(
            "14.2",
            bool(ran) and not failed,
            f"{doc}: {len(ran)} documented commands ran exactly as written with exit 0"
            if ran and not failed
            else f"{doc}: {len(failed)} of {len(ran)} commands failed: "
            + "; ".join(
                f"line {r['line']} `{r['command'].splitlines()[0][:90]}` exit {r['exit']}: "
                f"{tail(r['output'], 2)}"
                for r in failed[:6]
            ),
            "a4",
        )
        if doc == "README.md":
            installed = home / ".local/bin/cwr"
            shipped = kvx.load(bundle / RELEASE_KVX) if (bundle / RELEASE_KVX).is_file() else {}
            pinned = shipped.get("artifact.cli", {}).get("sha256")
            same = installed.is_file() and sha256_file(installed) == pinned
            results.check(
                "14.3",
                same,
                f"the README install put the packaged cwr ({pinned}) on PATH"
                if same
                else f"installed cwr {installed} does not match artifact.cli.sha256 {pinned}",
            )
        if doc == "RELEASE.md" and release_before is not None:
            after = (bundle / RELEASE_KVX).read_bytes()
            results.check(
                "14.3",
                after == release_before,
                "cwr release pin --write, run from the bundle's own packaged artifacts, "
                "left benchmark/release.kvx byte-identical: every pin reproduces"
                if after == release_before
                else "cwr release pin --write changed benchmark/release.kvx in the bundle: "
                "the recorded pins do not reproduce",
            )
    out_dir = os.environ.get("CWR_EVIDENCE_DIR")
    if out_dir:
        for log in logs.iterdir():
            shutil.copyfile(log, Path(out_dir) / log.name)

    installed = home / ".local/bin/cwr"
    verified = []
    if installed.is_file() and bundle.is_dir():
        verified = check_release(results, installed, bundle, scratch, env)
    else:
        results.check("14.3", False, "no installed cwr or unpacked bundle to verify pins with")
    write_attempt(verified)
    total = len(all_results)
    print(f"documented commands executed: {total}", flush=True)
    return results.report()


if __name__ == "__main__":
    sys.exit(main())

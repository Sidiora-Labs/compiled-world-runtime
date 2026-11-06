#!/usr/bin/env python3
# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Task 1.1 driver: compiler front end (parse, resolve, lint), case CWR-002.

Exercises clauses 2.1, 2.2, 3.1, 3.5 and 4.1 against the real implementation. It builds the
`cwr` binary with cargo, runs the `cwr-compiler` unit tests, then drives `cwr lint --json` and
`cwr build --json` as subprocesses over:

* the reference world examples/refworld as it stands in the tree, and a clean copy of it with
  only the files the World Source grammar does not define removed (the copy is the "clean
  source" every mutation starts from);
* the pack's illustrative source spec/cwr/examples/refworld, with and without --allow-example;
* the five seeded defects of task 1.1 do_4 (hidden precondition, host import, unknown key,
  duplicate primitive, missing module) and further fixtures so that every lint code listed in
  spec/cwr/compiler.md section 3 (CWR-V001 through CWR-M001) is triggered at least once,
  each asserted for its exact code, stage, exit code and location;
* the routes around lib/ closure (clause 3.5): dynamic and reflective builtins, re-exported host
  modules, interpreter dunders, Unicode spellings Python NFKC-normalises to a banned name,
  string-evaluating APIs of the permitted modules (typing.get_type_hints, ForwardRef,
  dataclasses._create_fn), private module internals and class factories, each refused at its
  own line, with the main escapes first proven real by running them in an isolated interpreter;
* `cwr build --out <scratch>` over the clean source (all eight stages pass in order and the
  Build is written under the scratch root) and over a defect only contract tests can see (the
  run stops at contract_tests, later stages are not_run and nothing is written). Every build
  writes under the driver's temporary directory, never the repository's build/.

Prints one line per clause, `CLAUSE <req.ac> PASS|FAIL <evidence>`, and exits 0 only when every
clause passes. Honours CARGO_TARGET_DIR; defaults to the workspace target dir.
Python 3.12, standard library only.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import protocol

REPO = Path(__file__).resolve().parents[3]
REFWORLD = REPO / "examples/refworld"
PACK_EXAMPLE = REPO / "spec/cwr/examples/refworld"
COMPILER_MD = REPO / "spec/cwr/compiler.md"
CLAUSES = ["2.1", "2.2", "3.1", "3.5", "4.1"]
STAGES = [
    "parse",
    "resolve",
    "lint",
    "contract_tests",
    "diff",
    "changelog_gate",
    "generate",
    "package",
]
LAYOUT = [
    "world.kvx",
    "CHANGELOG.kvx",
    "lib",
    "contracts",
    "adapters",
    "migrations",
    "fixtures",
    "manual",
]
FORBIDDEN = [
    "os",
    "sys",
    "socket",
    "http",
    "urllib",
    "time",
    "datetime",
    "random",
    "secrets",
    "subprocess",
    "pathlib",
    "shutil",
    "tempfile",
    "threading",
    "multiprocessing",
    "ctypes",
    "importlib",
]
TIMEOUT = 900


class Results:
    def __init__(self) -> None:
        self.ok: dict[str, list[str]] = {c: [] for c in CLAUSES}
        self.bad: dict[str, list[str]] = {c: [] for c in CLAUSES}

    def check(self, clause: str, condition: bool, evidence: str) -> bool:
        (self.ok if condition else self.bad)[clause].append(evidence)
        return condition


R = Results()
OBS = protocol.Observer("1.1", protocol.ok_bad(R))
TRIGGERED: dict[str, list[str]] = {}


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT, **kw)


def target_dir() -> Path:
    env = os.environ.get("CARGO_TARGET_DIR")
    return Path(env) if env else REPO / "target"


def cargo() -> tuple[Path, bool, str]:
    env = dict(os.environ)
    env.setdefault("CARGO_TARGET_DIR", str(target_dir()))
    b = run(["cargo", "build", "-p", "cwr-cli"], cwd=REPO, env=env)
    if b.returncode != 0:
        print(b.stderr[-4000:], file=sys.stderr)
        raise SystemExit("cargo build failed")
    t = run(["cargo", "test", "-p", "cwr-compiler"], cwd=REPO, env=env)
    out = t.stdout + t.stderr
    m = re.findall(r"test result: ok\. (\d+) passed", out)
    return (
        target_dir() / "debug" / "cwr",
        t.returncode == 0,
        f"{sum(map(int, m))} unit tests" if t.returncode == 0 else out[-2000:],
    )


CWR: Path = Path()
SCRATCH = Path(tempfile.mkdtemp(prefix="cwr-1_1-"))


BUILD_OUT = SCRATCH / "build-out"


def cwr(sub: str, root: Path, *extra: str) -> tuple[int, dict, str]:
    # `cwr build` always writes under a scratch Build root, never the repository's build/.
    if sub == "build" and "--out" not in extra:
        extra = (*extra, "--out", str(BUILD_OUT))
    p = run([str(CWR), sub, str(root), "--json", *extra])
    try:
        rep = json.loads(p.stdout)
    except json.JSONDecodeError:
        rep = {"stages": [], "stopped_at": None, "parse_error": p.stdout + p.stderr}
    return p.returncode, rep, p.stdout


def stage_diags(rep: dict, stage: str) -> list[dict]:
    for s in rep.get("stages", []):
        if s["stage"] == stage:
            return s["diagnostics"]
    return []


def all_diags(rep: dict) -> list[dict]:
    return [d for s in rep.get("stages", []) for d in s["diagnostics"]]


def statuses(rep: dict) -> dict[str, str]:
    return {s["stage"]: s["status"] for s in rep.get("stages", [])}


def brief(rep: dict, limit: int = 4) -> str:
    ds = all_diags(rep)
    parts = [f"{d['code'] or d['kind']}@{d['file']}:{d['line']}" for d in ds[:limit]]
    return ",".join(parts) + (f"+{len(ds) - limit}" if len(ds) > limit else "")


# ------------------------------------------------------------------ source mutation helpers

CLEAN: Path = Path()
_counter = [0]


def fresh(name: str) -> Path:
    _counter[0] += 1
    dst = SCRATCH / f"{_counter[0]:02d}_{name}"
    shutil.copytree(CLEAN, dst)
    return dst


def set_key(root: Path, rel: str, section: str, key: str, value: str) -> int:
    """Replaces (or adds) `key = value` inside [section]; returns its one-based line."""
    path = root / rel
    lines = path.read_text().split("\n")
    try:
        start = lines.index(f"[{section}]")
    except ValueError:
        raise SystemExit(f"fixture error: no [{section}] in {rel}") from None
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("[")), len(lines))
    for i in range(start + 1, end):
        if re.match(rf"{re.escape(key)}\s*=", lines[i]):
            lines[i] = f"{key} = {value}"
            path.write_text("\n".join(lines))
            return i + 1
    lines.insert(start + 1, f"{key} = {value}")
    path.write_text("\n".join(lines))
    return start + 2


def section_line(root: Path, rel: str, section: str) -> int:
    return (root / rel).read_text().split("\n").index(f"[{section}]") + 1


def insert_after(root: Path, rel: str, anchor: str, text: str) -> int:
    """Inserts `text` (one or more lines) after the first line equal to `anchor`; returns the first new line."""
    path = root / rel
    lines = path.read_text().split("\n")
    i = lines.index(anchor)
    lines[i + 1 : i + 1] = text.split("\n")
    path.write_text("\n".join(lines))
    return i + 2


def replace_once(root: Path, rel: str, old: str, new: str) -> None:
    path = root / rel
    s = path.read_text()
    if s.count(old) != 1:
        raise SystemExit(f"fixture error: {old!r} occurs {s.count(old)} times in {rel}")
    path.write_text(s.replace(old, new))


def line_of(root: Path, rel: str, text: str) -> int:
    for n, line in enumerate((root / rel).read_text().split("\n"), 1):
        if text in line:
            return n
    raise SystemExit(f"fixture error: {text!r} not in {rel}")


def expect(
    clause: str,
    name: str,
    root: Path,
    *,
    stage: str,
    codes: set[str | None],
    where: list[tuple[str, str | None, int | None]],
    exit_code: int | None = None,
    extra: tuple[str, ...] = (),
    index: int | None = None,
) -> dict:
    """Runs `cwr lint` and asserts stage, exit code, exactly the given codes, and each
    (code, file, line) in `where` reported."""
    rc, rep, _ = cwr("lint", root, *extra)
    want_rc = exit_code if exit_code is not None else (3 if stage == "lint" else 2)
    ds = stage_diags(rep, stage)
    got_codes = {d["code"] for d in ds}
    later = [s for s in STAGES[STAGES.index(stage) + 1 :] if statuses(rep).get(s) != "not_run"]
    problems = []
    if rc != want_rc:
        problems.append(f"exit {rc} != {want_rc}")
    if rep.get("stopped_at") != stage:
        problems.append(f"stopped_at {rep.get('stopped_at')} != {stage}")
    if not ds or not got_codes <= codes:
        problems.append(f"codes {sorted(map(str, got_codes))} not within {sorted(map(str, codes))}")
    if later:
        problems.append(f"later stages ran: {later}")
    for code, file, line in where:
        if not any(
            d["code"] == code and d["file"] == file and (line is None or d["line"] == line)
            for d in ds
        ):
            problems.append(f"missing {code} at {file}:{line}")
    if index is not None and not any(d["index"] == index for d in ds):
        problems.append(f"no diagnostic with precondition index {index}")
    for d in ds:
        if d["code"]:
            TRIGGERED.setdefault(d["code"], []).append(name)
    shown = f"{name}: exit {rc} stage {rep.get('stopped_at')} [{brief(rep)}]"
    R.check(
        clause, not problems, shown if not problems else f"{shown} PROBLEMS {'; '.join(problems)}"
    )
    return rep


# ------------------------------------------------------------------ scenarios


def clean_sources() -> None:
    global CLEAN
    # The reference world as it stands in the tree.
    rc, rep, _ = cwr("lint", REFWORLD)
    ds = all_diags(rep)
    non_grammar = sorted(
        {d["file"] for d in ds if d["code"] == "CWR-C001" and d["kind"] == "unknown-file"}
    )
    only_unknown_files = bool(ds) and all(
        d["code"] == "CWR-C001" and d["kind"] == "unknown-file" for d in ds
    )
    if rc == 0:
        R.check("3.1", True, "examples/refworld as in the tree passes parse/resolve/lint (exit 0)")
    else:
        R.check(
            "3.1",
            rc == 2 and rep.get("stopped_at") == "parse" and only_unknown_files,
            f"examples/refworld as in the tree: exit {rc} at {rep.get('stopped_at')}; only non-grammar files refused "
            f"as CWR-C001 unknown-file at parse: {non_grammar}",
        )
    # The clean source: the same tree without the files the grammar does not define.
    CLEAN = SCRATCH / "clean"
    shutil.copytree(REFWORLD, CLEAN)
    for rel in non_grammar:
        (CLEAN / rel).unlink()
    missing = [p for p in LAYOUT if not (CLEAN / p).exists()]
    R.check(
        "3.1",
        not missing,
        f"clean source carries the full layout {LAYOUT}"
        + (f" MISSING {missing}" if missing else ""),
    )
    rc, rep, _out1 = cwr("lint", CLEAN)
    st = statuses(rep)
    ok = rc == 0 and [st[s] for s in STAGES[:3]] == ["passed"] * 3 and not all_diags(rep)
    R.check(
        "4.1",
        ok,
        f"clean refworld (minus {non_grammar or 'nothing'}): cwr lint exit {rc}, parse/resolve/lint "
        f"{[st.get(s) for s in STAGES[:3]]}",
    )
    rc, rep, _ = cwr("build", CLEAN)
    st = statuses(rep)
    order = [s["stage"] for s in rep.get("stages", [])]
    R.check("4.1", order == STAGES, f"report stage identities in order: {order}")
    built = (rep.get("build") or {}).get("hash", "")
    clean_build_rep = rep
    written = sorted(p.name for p in BUILD_OUT.iterdir()) if BUILD_OUT.is_dir() else []
    ok = (
        rc == 0
        and all(st.get(s) == "passed" for s in STAGES)
        and rep.get("stopped_at") is None
        and bool(built)
        and any(built.endswith(w) for w in written)
    )
    R.check(
        "4.1",
        ok,
        f"cwr build --out <scratch> on clean source: exit {rc}, all eight stages "
        f"{[st.get(s) for s in STAGES]}, Build {built[:19]}... written under --out",
    )
    # A defect only the contract-test stage can see: the front end passes, the run stops at
    # contract_tests (exit 4), every later stage is not_run and no Build is written.
    root = fresh("effect_unmet")
    replace_once(root, "lib/fs.py", '    del _fs(state)["files"][path]\n', "    _fs(state)\n")
    out = SCRATCH / "build-out-effect"
    rc, rep, _ = cwr("build", root, "--out", str(out))
    st = statuses(rep)
    leftover = sorted(p.name for p in out.iterdir()) if out.is_dir() else []
    ok = (
        rc == 4
        and [st[s] for s in STAGES[:3]] == ["passed"] * 3
        and st["contract_tests"] == "failed"
        and all(st[s] == "not_run" for s in STAGES[4:])
        and rep.get("stopped_at") == "contract_tests"
        and bool(stage_diags(rep, "contract_tests"))
        and not leftover
    )
    R.check(
        "4.1",
        ok,
        f"cwr build, fs.delete no longer deletes (effect unmet): exit {rc}, stopped_at "
        f"{rep.get('stopped_at')}, later stages {[st.get(s) for s in STAGES[4:]]}, written {leftover}",
    )
    # 2.1 positive side, tied to the compiler's output: every precondition the clean source
    # declares reaches the compiled contracts.json unchanged, and each one, checked here
    # independently of the compiler, uses only the entry's parameters, literals, len() and
    # calls to read_only primitives of the same Build. Then 2.1 negative side for every one
    # of them: retargeting its first call to a mutating primitive is refused as CWR-V001 at
    # that precondition's file, line and index.
    precondition_clauses(clean_build_rep, built)


EXPR_KEYWORDS = {"and", "or", "not", "True", "False", "None", "in", "is"}


def source_preconditions(root: Path) -> list[tuple[str, str, int, int, str, str]]:
    """(file, entry, line, index, expression, code) for every precondition of every contract
    and adapter file of a source, read from the KVX text."""
    out = []
    for f in sorted([*(root / "contracts").glob("*.kvx"), *(root / "adapters").glob("*.kvx")]):
        rel = f.relative_to(root).as_posix()
        section = None
        for n, line in enumerate(f.read_text().split("\n"), 1):
            m = re.match(r"^\[(?:primitive|adapter)\.(.+)\]$", line)
            if m:
                section = m.group(1)
            elif line.startswith("[") and line.endswith("]"):
                section = None
            elif section and line.startswith("preconditions ="):
                for i, (expr, code) in enumerate(json.loads(line.split("=", 1)[1]), 1):
                    out.append((rel, section, n, i, expr, code))
    return out


def expr_problems(expr: str, params: set[str], read_only: set[str]) -> list[str]:
    """Independent check of one precondition against world-source.md section 5."""
    bare = re.sub(r'"(?:[^"\\]|\\.)*"', '""', expr)
    problems = []
    for m in re.finditer(r"([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*(\()?", bare):
        name, call = m.group(1), m.group(2)
        if call and "." in name:
            if name not in read_only:
                problems.append(f"call {name} is not a read_only primitive")
        elif call:
            if name != "len":
                problems.append(f"call {name}")
        elif "." in name:
            problems.append(f"attribute {name}")
        elif name not in params and name not in EXPR_KEYWORDS:
            problems.append(f"name {name}")
    if re.search(r"\[", bare):
        problems.append("indexing")
    return problems


def precondition_clauses(build_rep: dict, built: str) -> None:
    src = source_preconditions(CLEAN)
    contracts_json = (
        next(
            (p / "contracts.json" for p in BUILD_OUT.iterdir() if built and built.endswith(p.name)),
            None,
        )
        if BUILD_OUT.is_dir()
        else None
    )
    if contracts_json is None or not contracts_json.is_file():
        R.check(
            "2.1", False, f"clean Build's contracts.json not found under --out (build {built[:19]})"
        )
        return
    cj = json.loads(contracts_json.read_text())
    entries = {e["name"]: e for e in cj["primitives"] + cj["adapters"]}
    read_only = {n for n, e in entries.items() if e["side_effect"] == "read_only"}
    compiled = sorted(
        (n, i, expr, code)
        for n, e in entries.items()
        for i, (expr, code) in enumerate(e["preconditions"], 1)
    )
    declared = sorted((sec, i, expr, code) for _, sec, _, i, expr, code in src)
    bad = []
    for name, i, expr, _code in compiled:
        params = {p[0] for p in entries[name]["params"]}
        for why in expr_problems(expr, params, read_only):
            bad.append(f"{name}#{i} {expr!r}: {why}")
    calls = sum(len(re.findall(r"[a-z_]+\.[a-z_]+\(", e)) for _, _, e, _ in compiled)
    R.check(
        "2.1",
        build_rep.get("stopped_at") is None and bool(compiled) and compiled == declared and not bad,
        f"clean source: all {len(declared)} declared preconditions compiled unchanged into contracts.json "
        f"({len(compiled)}); each uses only its parameters, literals, len() and {calls} calls to read_only "
        f"primitives of the Build (checked independently)"
        + (f" PROBLEMS {bad}" if bad else "")
        + ("" if compiled == declared else " MISMATCH between source and contracts.json"),
    )
    # Negative side for every precondition that calls a primitive: retarget the first call to
    # a mutating primitive of equal arity; the compiler must refuse each with CWR-V001.
    mutating = sorted(
        (n for n, e in entries.items() if e["side_effect"] == "mutating"),
        key=lambda n: (len(entries[n]["params"]), n),
    )
    tried, refused, misses = 0, 0, []
    for rel, sec, _line, idx, expr, _code in src:
        m = re.search(r"([a-z_]+\.[a-z_]+)\(", expr)
        if not m:
            continue
        target = m.group(1)
        arity = len(entries[target]["params"])
        swap = next(
            (
                n
                for n in mutating
                if len(entries[n]["params"]) == arity and n.split(".")[0] == target.split(".")[0]
            ),
            None,
        ) or next((n for n in mutating if len(entries[n]["params"]) == arity), None)
        if swap is None:
            misses.append(f"{sec}#{idx}: no mutating primitive of arity {arity}")
            continue
        tried += 1
        root = fresh(f"v001_{sec}_{idx}")
        new_expr = expr[: m.start(1)] + swap + expr[m.end(1) :]
        pres = [list(p) for p in [(e, c) for r2, s2, _, _, e, c in src if r2 == rel and s2 == sec]]
        pres[idx - 1][0] = new_expr
        got_line = set_key(
            root,
            rel,
            ("adapter." if rel.startswith("adapters/") else "primitive.") + sec,
            "preconditions",
            json.dumps(pres),
        )
        rc, rep, _ = cwr("lint", root)
        ds = stage_diags(rep, "lint")
        hit = any(
            d["code"] == "CWR-V001"
            and d["file"] == rel
            and d["line"] == got_line
            and d["index"] == idx
            for d in ds
        )
        if rc == 3 and rep.get("stopped_at") == "lint" and hit:
            refused += 1
            TRIGGERED.setdefault("CWR-V001", []).append(f"retarget {sec}#{idx}")
        else:
            misses.append(
                f"{sec}#{idx} {new_expr!r}: exit {rc} at {rep.get('stopped_at')} [{brief(rep)}]"
            )
    R.check(
        "2.1",
        tried > 0 and refused == tried and not misses,
        f"every precondition with a primitive call ({tried}) retargeted to a mutating primitive: "
        f"{refused} refused as CWR-V001 at file, line and index"
        + (f" MISSES {misses}" if misses else ""),
    )


def pack_example() -> None:
    # The pack's illustrative source: an excerpt (world.kvx, contracts, adapters, CHANGELOG,
    # one migration) without lib/, fixtures/ or manual/. It is refused at parse: without
    # --allow-example for meta.example and the incomplete layout, with it for the layout only.
    missing_dirs = sorted(
        f"{x}/" for x in ("lib", "fixtures", "manual") if not (PACK_EXAMPLE / x).exists()
    )
    rc, rep, _ = cwr("lint", PACK_EXAMPLE)
    d = all_diags(rep)
    kinds = sorted((x["kind"], x["file"]) for x in d)
    want = sorted([("example", "world.kvx")] + [("layout", m) for m in missing_dirs])
    ex = [x for x in d if x["kind"] == "example"]
    R.check(
        "4.1",
        rc == 2
        and rep.get("stopped_at") == "parse"
        and kinds == want
        and ex[0]["key"] == "example",
        f"spec example without --allow-example: exit {rc}, parse refusal {kinds}",
    )
    rc, rep, _ = cwr("lint", PACK_EXAMPLE, "--allow-example")
    d = stage_diags(rep, "parse")
    kinds = sorted((x["kind"], x["file"]) for x in d)
    R.check(
        "3.1",
        rc == 2
        and rep.get("stopped_at") == "parse"
        and bool(missing_dirs)
        and kinds == [("layout", m) for m in missing_dirs],
        f"spec example with --allow-example: exit {rc}, refused at parse for its incomplete layout "
        f"only: {kinds}",
    )


def five_seeded_defects() -> None:
    # 1. hidden precondition: a precondition that can only be checked by acting.
    root = fresh("hidden_precondition")
    line = set_key(
        root,
        "contracts/fs.kvx",
        "primitive.fs.delete",
        "preconditions",
        '[["fs.exists(path)", "NOT_FOUND"], ["not fs.lock(path)", "LOCKED"]]',
    )
    rep = expect(
        "2.1",
        "do_4 hidden precondition (calls mutating fs.lock)",
        root,
        stage="lint",
        codes={"CWR-V001"},
        where=[("CWR-V001", "contracts/fs.kvx", line)],
        index=2,
    )
    d = stage_diags(rep, "lint")
    R.check(
        "4.1",
        bool(d) and d[0]["section"] == "primitive.fs.delete" and d[0]["key"] == "preconditions",
        f"V001 located at file, section and key: {d[0]['file']}:{d[0]['line']}:{d[0]['col']} [{d[0]['section']}] {d[0]['key']} #{d[0]['index']}"
        if d
        else "no V001",
    )
    # 2. host import in lib/.
    root = fresh("host_import")
    line = insert_after(
        root, "lib/fs.py", "from typing import TYPE_CHECKING, Any, TypedDict", "import time"
    )
    expect(
        "3.5",
        "do_4 host import (import time in lib/fs.py)",
        root,
        stage="lint",
        codes={"CWR-L001"},
        where=[("CWR-L001", "lib/fs.py", line)],
    )
    # 3. unknown key.
    root = fresh("unknown_key")
    line = set_key(root, "contracts/fs.kvx", "primitive.fs.write", "retries", "3")
    rep = expect(
        "3.1",
        "do_4 unknown key (retries in [primitive.fs.write])",
        root,
        stage="parse",
        codes={"CWR-C001"},
        where=[("CWR-C001", "contracts/fs.kvx", line)],
    )
    d = stage_diags(rep, "parse")
    R.check(
        "4.1",
        bool(d) and d[0]["section"] == "primitive.fs.write" and d[0]["key"] == "retries",
        f"C001 located at [{d[0]['section']}] {d[0]['key']} line {d[0]['line']}"
        if d
        else "no C001",
    )
    # 4. duplicate primitive across files.
    root = fresh("duplicate_primitive")
    (root / "adapters/fs.kvx").write_text(
        '[meta]\nformat = "cwr.adapter"\nschema_version = 1\nmodule = "fs"\n\n[adapter.fs.exists]\n'
        'side_effect = "external"\nparams = {"path": "Path"}\nreturns = "bool"\npreconditions = []\n'
        'idempotency = "none"\ntimeout_ms = 1000\nfailures = ["UNAVAILABLE"]\ncontent_guaranteed = false\n'
        'endpoint = "stub://fs/v1/exists"\ndoc = "A second fs.exists."\n'
    )
    rep = expect(
        "4.1",
        "do_4 duplicate primitive (fs.exists in contracts/ and adapters/)",
        root,
        stage="resolve",
        codes={"CWR-C002"},
        where=[("CWR-C002", "adapters/fs.kvx", 6)],
    )
    d = stage_diags(rep, "resolve")
    first = section_line(root, "contracts/fs.kvx", "primitive.fs.exists")
    R.check(
        "4.1",
        bool(d)
        and d[0]["related"]
        and d[0]["related"][0]["file"] == "contracts/fs.kvx"
        and d[0]["related"][0]["line"] == first,
        f"C002 names both locations: adapters/fs.kvx:6 and contracts/fs.kvx:{first}",
    )
    # 5. missing module: contracts/jobs.kvx for a module removed from world.modules.
    root = fresh("missing_module")
    set_key(root, "world.kvx", "world", "modules", '["fs", "kv", "search", "publish", "world"]')
    expect(
        "4.1",
        "do_4 missing module (jobs absent from world.modules)",
        root,
        stage="resolve",
        codes={"CWR-C003"},
        where=[("CWR-C003", "contracts/jobs.kvx", None), ("CWR-C003", "lib/jobs.py", 1)],
    )


def visibility() -> None:
    cases = [
        (
            "external adapter call",
            "primitive.fs.write",
            '[["len(search.query(\\"x\\", 1)) >= 0", "LOCKED"]]',
            "lint",
            "CWR-V001",
            1,
        ),
        (
            "hidden name",
            "primitive.fs.write",
            '[["not fs.locked(path)", "LOCKED"], ["hidden_counter < 3", "LIMIT"]]',
            "resolve",
            "CWR-V002",
            2,
        ),
        (
            "host clock call",
            "primitive.fs.write",
            '[["time.time() > 0", "LOCKED"]]',
            "resolve",
            "CWR-V002",
            1,
        ),
        (
            "host attribute",
            "primitive.fs.write",
            '[["os.environ", "LOCKED"]]',
            "resolve",
            "CWR-V002",
            1,
        ),
        (
            "indexing",
            "primitive.fs.write",
            '[["content[0] == 1", "INVALID"]]',
            "resolve",
            "CWR-V002",
            1,
        ),
        (
            "non-parameter call argument",
            "primitive.fs.delete",
            '[["fs.exists(other)", "NOT_FOUND"]]',
            "resolve",
            "CWR-V002",
            1,
        ),
    ]
    for name, section, value, stage, code, index in cases:
        root = fresh(name.replace(" ", "_"))
        line = set_key(root, "contracts/fs.kvx", section, "preconditions", value)
        expect(
            "2.1",
            f"{name} {json.loads(value)[index - 1][0]!r}",
            root,
            stage=stage,
            codes={code},
            where=[(code, "contracts/fs.kvx", line)],
            index=index,
        )
    # A primitive this release removes.
    root = fresh("removed_primitive")
    line = set_key(
        root,
        "contracts/fs.kvx",
        "primitive.fs.delete",
        "preconditions",
        '[["not fs.archived(path)", "LOCKED"]]',
    )
    set_key(
        root,
        "CHANGELOG.kvx",
        "release.0.2.0",
        "changes",
        '["fs_write_size_limit", "kv_namespaces", "kv_used_added", "kv_set_quota", "fs_archived_removed"]',
    )
    with (root / "CHANGELOG.kvx").open("a") as f:
        f.write(
            '\n[change.fs_archived_removed]\nkind = "primitive_removed"\ntarget = "fs.archived"\n'
            'before = "fs.archived(path) existed."\nafter = "fs.archived no longer exists."\nmigration = ""\n'
        )
    rep = expect(
        "2.1",
        "removed primitive fs.archived referenced",
        root,
        stage="resolve",
        codes={"CWR-V002"},
        where=[("CWR-V002", "contracts/fs.kvx", line)],
    )
    d = stage_diags(rep, "resolve")
    R.check(
        "2.1",
        bool(d) and "removes" in d[0]["message"],
        f"removed-primitive V002 says so: {d[0]['message'][:90] if d else ''}",
    )
    # Adapter precondition calling a mutating primitive.
    root = fresh("adapter_precondition")
    line = set_key(
        root,
        "adapters/search.kvx",
        "adapter.search.query",
        "preconditions",
        '[["len(q) > 0", "INVALID"], ["not fs.lock(q)", "LIMIT"]]',
    )
    expect(
        "2.1",
        "adapter precondition calls mutating fs.lock",
        root,
        stage="lint",
        codes={"CWR-V001"},
        where=[("CWR-V001", "adapters/search.kvx", line)],
        index=2,
    )
    # Drift expression (validated with the same checks as a contract).
    root = fresh("drift_expression")
    (root / "drift").mkdir()
    (root / "drift/drift-1.kvx").write_text(
        '[meta]\nformat = "cwr.drift"\nschema_version = 1\n\n[drift.1]\nturn = 8\nprimitive = "fs.delete"\n'
        'change = "precondition_added"\nexpr = "not fs.locked(path) and fs.write(path, \\"x\\") == 0"\nfailure = "LOCKED"\n'
        'doc = "Injected rule."\n'
    )
    expect(
        "2.1",
        "drift expr calls mutating fs.write",
        root,
        stage="lint",
        codes={"CWR-V001"},
        where=[("CWR-V001", "drift/drift-1.kvx", 9)],
    )
    # All V001s of one primitive reported, in precondition order.
    root = fresh("ordered_v001")
    line = set_key(
        root,
        "contracts/fs.kvx",
        "primitive.fs.move",
        "preconditions",
        '[["not fs.lock(src)", "LOCKED"], ["fs.exists(src)", "NOT_FOUND"], ["not fs.delete(dst)", "CONFLICT"]]',
    )
    rep = expect(
        "2.2",
        "two hidden preconditions of fs.move",
        root,
        stage="lint",
        codes={"CWR-V001"},
        where=[("CWR-V001", "contracts/fs.kvx", line)],
    )
    idx = [d["index"] for d in stage_diags(rep, "lint")]
    R.check(
        "2.2", idx == [1, 3], f"preconditions reported by one-based declared index in order: {idx}"
    )


def failure_codes() -> None:
    root = fresh("two_codes")
    line = set_key(
        root,
        "contracts/fs.kvx",
        "primitive.fs.delete",
        "preconditions",
        '[["fs.exists(path)", "NOT_FOUND", "LOCKED"]]',
    )
    rep = expect(
        "2.2",
        "precondition with two failure codes",
        root,
        stage="parse",
        codes={None},
        where=[(None, "contracts/fs.kvx", line)],
    )
    msg = stage_diags(rep, "parse")[0]["message"] if stage_diags(rep, "parse") else ""
    R.check("2.2", "exactly one failure code" in msg, f"message: {msg[:100]}")
    root = fresh("no_code")
    line = set_key(
        root, "contracts/fs.kvx", "primitive.fs.delete", "preconditions", '[["fs.exists(path)"]]'
    )
    expect(
        "2.2",
        "precondition with no failure code",
        root,
        stage="parse",
        codes={None},
        where=[(None, "contracts/fs.kvx", line)],
    )
    root = fresh("unknown_code")
    line = set_key(
        root,
        "contracts/fs.kvx",
        "primitive.fs.delete",
        "preconditions",
        '[["fs.exists(path)", "NOT_FOUND"], ["not fs.locked(path)", "BUSY"]]',
    )
    expect(
        "2.2",
        "precondition code BUSY outside the envelope",
        root,
        stage="lint",
        codes={"CWR-F001"},
        where=[("CWR-F001", "contracts/fs.kvx", line)],
        index=2,
    )
    root = fresh("adapter_failures")
    l1 = set_key(
        root,
        "adapters/search.kvx",
        "adapter.search.query",
        "failures",
        '["INVALID", "LIMIT", "RATE_LIMITED", "TIMEOUT"]',
    )
    l2 = set_key(
        root, "adapters/publish.kvx", "adapter.publish.status", "content_guaranteed", "true"
    )
    l3 = set_key(
        root,
        "adapters/publish.kvx",
        "adapter.publish.delete",
        "failures",
        '["UNAVAILABLE", "GONE"]',
    )
    expect(
        "2.2",
        "adapter without UNAVAILABLE, content_guaranteed true, failure GONE",
        root,
        stage="lint",
        codes={"CWR-F002", "CWR-F001"},
        where=[
            ("CWR-F002", "adapters/search.kvx", l1),
            ("CWR-F002", "adapters/publish.kvx", l2),
            ("CWR-F001", "adapters/publish.kvx", l3),
        ],
    )
    root = fresh("adapter_empty_failures")
    l1 = set_key(root, "adapters/search.kvx", "adapter.search.query", "failures", "[]")
    expect(
        "2.2",
        "adapter with empty failures",
        root,
        stage="lint",
        codes={"CWR-F002"},
        where=[("CWR-F002", "adapters/search.kvx", l1)],
    )


def closure_layout() -> None:
    for name, rel, content in [
        ("unknown top-level file", "NOTES.txt", "notes\n"),
        ("unknown lib subpackage", "lib/helpers/util.py", "x = 1\n"),
        ("unknown manual file", "manual/extra.md", "# Extra\n"),
        ("unknown directory", "scripts/run.py", "print(1)\n"),
    ]:
        root = fresh(name.replace(" ", "_"))
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(content)
        expect(
            "3.1",
            f"{name} {rel}",
            root,
            stage="parse",
            codes={"CWR-C001"},
            where=[("CWR-C001", rel, 1)],
        )
    # Every required part of the layout (clause 3.1; world-source.md sections 2, 9, 10).
    layout_cases = (
        [
            ("manual/", lambda r: shutil.rmtree(r / "manual"), [("manual/", 1)]),
            ("fixtures/", lambda r: shutil.rmtree(r / "fixtures"), [("fixtures/", 1)]),
            ("lib/", lambda r: shutil.rmtree(r / "lib"), [("lib/", 1)]),
            ("contracts/", lambda r: shutil.rmtree(r / "contracts"), [("contracts/", 1)]),
            ("world.kvx", lambda r: (r / "world.kvx").unlink(), [("world.kvx", 1)]),
            ("CHANGELOG.kvx", lambda r: (r / "CHANGELOG.kvx").unlink(), [("CHANGELOG.kvx", 1)]),
        ]
        + [
            (f"manual/{m}", lambda r, m=m: (r / "manual" / m).unlink(), [(f"manual/{m}", 1)])
            for m in ("intro.md", "conventions.md", "types.md", "examples.md")
        ]
        + [
            (
                "fixtures/kv/",
                lambda r: shutil.rmtree(r / "fixtures" / "kv"),
                [("world.kvx", line_of(CLEAN, "world.kvx", "modules = "))],
            )
        ]
    )
    for what, mutate, where in layout_cases:
        root = fresh("missing_" + re.sub(r"[^a-z]", "_", what.lower()))
        mutate(root)
        rc, rep, _ = cwr("lint", root)
        ds = stage_diags(rep, "parse")
        st = statuses(rep)
        hit = all(any(d["file"] == f and d["line"] == ln for d in ds) for f, ln in where)
        ok = (
            rc == 2
            and rep.get("stopped_at") == "parse"
            and hit
            and all(st.get(s) == "not_run" for s in STAGES[1:])
        )
        # A missing directory stops the full build as well: nothing is written.
        if what in ("manual/", "fixtures/", "manual/intro.md"):
            out = SCRATCH / f"build-out-missing-{_counter[0]}"
            brc, brep, _ = cwr("build", root, "--out", str(out))
            leftover = sorted(p.name for p in out.iterdir()) if out.is_dir() else []
            ok = ok and brc == 2 and brep.get("stopped_at") == "parse" and not leftover
            build_note = f"; cwr build exit {brc} at {brep.get('stopped_at')}, written {leftover}"
        else:
            build_note = ""
        R.check(
            "3.1",
            ok,
            f"source without {what}: cwr lint exit {rc}, refused at {rep.get('stopped_at')} "
            f"[{', '.join(f'{d["kind"]}@{d["file"]}:{d["line"]}' for d in ds[:3])}]{build_note}",
        )
    root = fresh("unknown_section")
    with (root / "world.kvx").open("a") as f:
        f.write("\n[network]\nallow = true\n")
    line = line_of(root, "world.kvx", "[network]")
    expect(
        "3.1",
        "unknown section [network] in world.kvx",
        root,
        stage="parse",
        codes={"CWR-C001"},
        where=[("CWR-C001", "world.kvx", line)],
    )
    root = fresh("unknown_world_key")
    line = set_key(root, "world.kvx", "limits", "disk_mb", "100")
    expect(
        "3.1",
        "unknown key limits.disk_mb",
        root,
        stage="parse",
        codes={"CWR-C001"},
        where=[("CWR-C001", "world.kvx", line)],
    )
    root = fresh("shape_read_only_effects")
    line = set_key(
        root, "contracts/fs.kvx", "primitive.fs.exists", "effects", '["fs.exists(path)"]'
    )
    expect(
        "3.1",
        "shape: read_only fs.exists with effects",
        root,
        stage="parse",
        codes={"CWR-C001"},
        where=[("CWR-C001", "contracts/fs.kvx", line)],
    )
    root = fresh("missing_limit")
    text = (root / "world.kvx").read_text()
    (root / "world.kvx").write_text(re.sub(r"\nmemory_mb = \d+", "", text))
    expect(
        "4.1",
        "world.kvx without limits.memory_mb (no default)",
        root,
        stage="parse",
        codes={None},
        where=[(None, "world.kvx", None)],
    )


def imports() -> None:
    root = fresh("forbidden_imports")
    lines = [f"import {m}" for m in FORBIDDEN] + [
        "from os import path as _p",
        "import os.path as _q",
        "import requests",
        "import functools",
        "from . import sibling",
        "from ..pkg import thing",
        '_m = __import__("socket")',
        "x = 1; import shutil",
        "if True: import tempfile",
    ]
    first = insert_after(
        root, "lib/fs.py", "from typing import TYPE_CHECKING, Any, TypedDict", "\n".join(lines)
    )
    rc, rep, _ = cwr("lint", root)
    d = [x for x in stage_diags(rep, "lint") if x["file"] == "lib/fs.py"]
    flagged = {x["line"] for x in d if x["code"] == "CWR-L001"}
    want = set(range(first, first + len(lines)))
    missing = sorted(want - flagged)
    for x in d:
        TRIGGERED.setdefault(x["code"], []).append("forbidden imports")
    R.check(
        "3.5",
        rc == 3
        and rep.get("stopped_at") == "lint"
        and not missing
        and flagged == want
        and all(x["code"] == "CWR-L001" for x in stage_diags(rep, "lint")),
        f"{len(lines)} forbidden imports in lib/fs.py ({', '.join(FORBIDDEN)}, os.path, requests, functools, "
        f"relative, __import__, after ';', one-line if): {len(flagged)} CWR-L001 at their lines"
        + (f" MISSING lines {missing}" if missing else ""),
    )
    # Dynamic and reflective routes around the import list (pyimports.rs, "Dynamic and
    # reflective access"): each line must be refused as CWR-L001 at its own line.
    evasions = [
        '_e1 = exec("import os")',
        "_e2 = eval(\"__import__('os')\")",
        '_e3 = compile("import os", "x", "exec")',
        '_e4 = getattr(__builtins__, "__imp" + "ort__")("socket")',
        '_e5 = open("/etc/passwd").read',
        '_e6 = globals()["__builtins__"]',
        "_e7 = vars()",
        "_e8 = typing.sys.modules",
        "from json import codecs as _e9",
        "_e10 = ().__class__.__bases__[0].__subclasses__()",
        "_e11 = f\"{eval('1')}\"",
        "import cwr._rpc as _e12",
        "_e13 = (lambda: 0).__globals__",
        "_e14 = (x for x in ()).gi_frame.f_globals",
        "_e15 = dataclasses.inspect.stack()",
        "_e16 = re._parser",
        '_e17 = setattr(_m, "x", 1)',
        "_e18 = breakpoint",
        "_e19 = __loader__",
    ]
    root = fresh("evasions")
    first = (
        insert_after(
            root,
            "lib/kv.py",
            "from typing import TYPE_CHECKING, Any, TypedDict",
            "import typing\nimport dataclasses\nimport re\n" + "\n".join(evasions),
        )
        + 3
    )
    rc, rep, _ = cwr("lint", root)
    d = [x for x in stage_diags(rep, "lint") if x["file"] == "lib/kv.py"]
    flagged = {x["line"] for x in d if x["code"] == "CWR-L001"}
    want = set(range(first, first + len(evasions)))
    missing = sorted(want - flagged)
    for x in d:
        TRIGGERED.setdefault(x["code"], []).append("dynamic import evasions")
    R.check(
        "3.5",
        rc == 3
        and rep.get("stopped_at") == "lint"
        and not missing
        and flagged == want
        and all(x["code"] == "CWR-L001" for x in stage_diags(rep, "lint")),
        f"{len(evasions)} dynamic or reflective routes around the import list in lib/kv.py (exec, eval, "
        f"compile, getattr on __builtins__, open, globals, vars, typing.sys, from json import codecs, "
        f"__class__/__subclasses__ chain, f-string eval, cwr._rpc, __globals__, gi_frame.f_globals, "
        f"dataclasses.inspect, re._parser, setattr, breakpoint, __loader__): {len(flagged)} lines refused "
        f"as CWR-L001"
        + (f" MISSING lines {[evasions[i - first] for i in missing]}" if missing else ""),
    )
    # Second-generation routes (fix round 2): Unicode spellings that Python NFKC-normalises to
    # a banned name, string-evaluating APIs of the permitted modules, private internals of the
    # permitted modules, and class factories whose annotation keys @dataclass would exec.
    escapes = [
        '_n1 = \uff45\uff58\uff45\uff43("import os")',
        '_n2 = \uff45val("1")',
        "import \uff4f\uff53 as _n3",
        "_n4 = typing.\uff53\uff59\uff53",
        '_n5 = \uff47\uff45\uff54\uff41\uff54\uff54\uff52(typing, "sys")',
        "_s1 = typing.get_type_hints(_f)",
        "from typing import get_type_hints as _s2",
        "_s3 = typing.ForwardRef(\"__import__('os')\")",
        "_s4 = typing.get_args(typing.List[\"__import__('os').getpid\"])[0]._evaluate(None, None, recursive_guard=frozenset())",
        "_s5 = typing._eval_type",
        '_s6 = dataclasses._create_fn("g", [], ["import os"], globals={})',
        "from dataclasses import _create_fn as _s7",
        "_p1 = json._default_decoder",
        "_p2 = re._compile",
        "from collections import _count_elements as _p3",
        '_c1 = type("X", (), {"__annotations__": {"a=1": int}})',
        '_c2 = typing.TypedDict("T", {"a=1": int})',
        "_c3 = collections.abc.ABCMeta",
        "_c4 = typing.NamedTupleMeta",
        "class _C5(metaclass=_m): pass",
        "from typing import TypedDict as _c6",
        "_c7 = TypedDict",
        "from typing import *",
        'if False: raise ValueError from exec("import os")',
        "def __getattr__(name): pass",
        "def __reduce_ex__(self, p): pass",
    ]
    root = fresh("escapes")
    prefix = "import typing\nimport dataclasses\nimport re\nimport json\nimport collections.abc\n"
    first = insert_after(
        root,
        "lib/kv.py",
        "from typing import TYPE_CHECKING, Any, TypedDict",
        prefix + "\n".join(escapes),
    ) + prefix.count("\n")
    rc, rep, _ = cwr("lint", root)
    d = [x for x in stage_diags(rep, "lint") if x["file"] == "lib/kv.py"]
    flagged = {x["line"] for x in d if x["code"] == "CWR-L001"}
    want = set(range(first, first + len(escapes)))
    missing = sorted(want - flagged)
    for x in d:
        TRIGGERED.setdefault(x["code"], []).append("normalisation and string-evaluation escapes")
    R.check(
        "3.5",
        rc == 3
        and rep.get("stopped_at") == "lint"
        and not missing
        and flagged == want
        and all(x["code"] == "CWR-L001" for x in stage_diags(rep, "lint")),
        f"{len(escapes)} further routes in lib/kv.py (fullwidth exec/eval/os/sys/getattr, "
        f"typing.get_type_hints, ForwardRef, ForwardRef._evaluate via get_args, typing._eval_type, "
        f"dataclasses._create_fn, private module attributes and imports, builtin type, functional "
        f"TypedDict, ABCMeta, NamedTupleMeta, metaclass=, TypedDict aliases, star import, 'raise .. from "
        f"exec', def __getattr__/__reduce_ex__): {len(flagged)} lines refused as CWR-L001"
        + (f" MISSING lines {[escapes[i - first] for i in missing]}" if missing else ""),
    )
    # The routes above are real: under this Python each one reaches os.getpid() (run in an
    # isolated interpreter in the scratch tree, never inside World Source).
    proofs = {
        "fullwidth exec": '\uff45\uff58\uff45\uff43("import os; _r = os.getpid()")',
        "typing.get_type_hints": "import typing\ndef _f(x: \"__import__('os').getpid\"): pass\n"
        "_r = typing.get_type_hints(_f)['x']()",
        "ForwardRef._evaluate": "import typing\n_r = typing.get_args(typing.List[\"__import__('os').getpid\"])"
        "[0]._evaluate(None, None, recursive_guard=frozenset())()",
        "dataclasses._create_fn": "import dataclasses\n_r = dataclasses._create_fn('g', [], "
        "['import os', 'return os.getpid()'], globals={})()",
    }
    real = []
    for name, code in proofs.items():
        p = run(
            [sys.executable, "-I", "-c", code + "\nimport os\nprint(_r == os.getpid())"],
            cwd=str(SCRATCH),
        )
        if p.returncode == 0 and p.stdout.strip() == "True":
            real.append(name)
    R.check(
        "3.5",
        len(real) == len(proofs),
        f"each refused route is a genuine escape under Python {sys.version.split()[0]}: "
        f"{len(real)}/{len(proofs)} reach os.getpid() ({', '.join(real)})",
    )
    root = fresh("escape_lookalikes")
    insert_after(
        root,
        "lib/kv.py",
        "from typing import TYPE_CHECKING, Any, TypedDict",
        'import typing\ntype _Alias = int\n_U = "\uff45\uff58\uff45\uff43 in a string"  # \uff4f\uff53 in a comment\n'
        "_V = dict(type=1)\n\n\nclass _D(TypedDict, total=False):\n    a: int\n\n\n"
        "class _E(typing.TypedDict):\n    b: str\n\n\ndef _private_helper(x):\n    return x\n",
    )
    rc, rep, _ = cwr("lint", root)
    R.check(
        "3.5",
        rc == 0,
        f"look-alikes of the further routes (type alias statement, non-ASCII inside a string and a "
        f"comment, keyword argument type=, TypedDict class bases bare and dotted, a module-level private "
        f"def): exit {rc} [{brief(rep)}]",
    )
    root = fresh("lookalikes")
    insert_after(
        root,
        "lib/kv.py",
        "from typing import TYPE_CHECKING, Any, TypedDict",
        "import re\n_L1 = re.compile('a+')\n_L2 = dict(open=1, eval=2)\n_L3 = f\"cannot open {len('x')}\"\n"
        "_L4 = __name__\n\n\nclass _Box:\n    def __init__(self, v):\n        self.v = v\n\n"
        "    def __eq__(self, o):\n        return self.v == o.v\n",
    )
    rc, rep, _ = cwr("lint", root)
    R.check(
        "3.5",
        rc == 0,
        f"look-alikes that are not dynamic access (re.compile, keyword arguments open= and eval=, 'open' "
        f"in f-string text, __name__, __init__/__eq__ definitions): exit {rc} [{brief(rep)}]",
    )
    root = fresh("permitted_imports")
    insert_after(
        root,
        "lib/fs.py",
        "from typing import TYPE_CHECKING, Any, TypedDict",
        "import json, re, math\nimport collections.abc\nfrom itertools import chain\nfrom dataclasses import dataclass\n"
        "from cwr_state import random as _rng\n_DOC = \"import os\"  # import sys\n_T = '''\nimport socket\n'''",
    )
    rc, rep, _ = cwr("lint", root)
    R.check(
        "3.5",
        rc == 0,
        f"permitted imports (json, re, math, collections.abc, itertools, dataclasses, cwr_state) and "
        f"'import' inside strings and comments: exit {rc} [{brief(rep)}]",
    )
    root = fresh("live_import")
    line = insert_after(root, "adapters/search/live.py", "from typing import Any", "import socket")
    expect(
        "3.5",
        "adapters/search/live.py imports socket",
        root,
        stage="lint",
        codes={"CWR-L001"},
        where=[("CWR-L001", "adapters/search/live.py", line)],
    )
    root = fresh("migration_import")
    rel = "migrations/0_2_0_kv_namespaces.py"
    text = (root / rel).read_text()
    marker = "\n\ndef migrate("
    (root / rel).write_text(
        text.replace(marker, "\n\nimport cwr_state\nimport datetime\nimport cwr\n" + marker, 1)
    )
    l1 = line_of(root, rel, "import cwr_state")
    expect(
        "3.5",
        "migration imports cwr_state, datetime and the generated SDK cwr",
        root,
        stage="lint",
        codes={"CWR-L001"},
        where=[("CWR-L001", rel, l1), ("CWR-L001", rel, l1 + 1), ("CWR-L001", rel, l1 + 2)],
    )
    root = fresh("lib_sdk_import")
    line = insert_after(
        root, "lib/fs.py", "from typing import TYPE_CHECKING, Any, TypedDict", "import cwr"
    )
    expect(
        "3.5",
        "lib/fs.py imports the generated SDK cwr (world-source.md section 8)",
        root,
        stage="lint",
        codes={"CWR-L001"},
        where=[("CWR-L001", "lib/fs.py", line)],
    )


def remaining_codes() -> None:
    root = fresh("lib_mismatch")
    replace_once(
        root,
        "lib/fs.py",
        "def exists(state: Any, path: str) -> bool:",
        "def exist(state: Any, path: str) -> bool:",
    )
    replace_once(
        root,
        "lib/fs.py",
        "def size(state: Any, path: str) -> int:",
        "def size(state: Any, p: str) -> int:",
    )
    l_exist = line_of(root, "lib/fs.py", "def exist(")
    expect(
        "4.1",
        "lib/ mismatch: exists renamed, size parameter renamed",
        root,
        stage="resolve",
        codes={"CWR-L002"},
        where=[
            (
                "CWR-L002",
                "contracts/fs.kvx",
                section_line(root, "contracts/fs.kvx", "primitive.fs.exists"),
            ),
            ("CWR-L002", "lib/fs.py", l_exist),
            (
                "CWR-L002",
                "contracts/fs.kvx",
                section_line(root, "contracts/fs.kvx", "primitive.fs.size"),
            ),
        ],
    )
    root = fresh("empty_doc")
    line = set_key(root, "contracts/fs.kvx", "primitive.fs.exists", "doc", '"  "')
    expect(
        "4.1",
        "empty doc on fs.exists",
        root,
        stage="lint",
        codes={"CWR-D001"},
        where=[("CWR-D001", "contracts/fs.kvx", line)],
    )
    root = fresh("prose")
    with (root / "manual/conventions.md").open("a") as f:
        f.write(
            "\n## Extras\n\nCall fs.teleport to move a file instantly.\nIt may fail with QUOTA_EXCEEDED.\n"
            "The budget limits.disk_mb applies.\n"
        )
    l1 = line_of(root, "manual/conventions.md", "fs.teleport")
    expect(
        "4.1",
        "manual prose names fs.teleport, QUOTA_EXCEEDED, limits.disk_mb",
        root,
        stage="lint",
        codes={"CWR-D001"},
        where=[
            ("CWR-D001", "manual/conventions.md", l1),
            ("CWR-D001", "manual/conventions.md", l1 + 1),
            ("CWR-D001", "manual/conventions.md", l1 + 2),
        ],
    )
    root = fresh("m001_empty")
    line = set_key(root, "CHANGELOG.kvx", "change.kv_namespaces", "migration", '""')
    expect(
        "4.1",
        "state_shape change kv_namespaces with empty migration",
        root,
        stage="lint",
        codes={"CWR-M001"},
        where=[("CWR-M001", "CHANGELOG.kvx", line)],
    )
    root = fresh("m001_missing_file")
    line = set_key(
        root, "CHANGELOG.kvx", "change.kv_namespaces", "migration", '"migrations/0_2_0_absent.py"'
    )
    expect(
        "4.1",
        "state_shape change naming a nonexistent migration",
        root,
        stage="lint",
        codes={"CWR-M001"},
        where=[("CWR-M001", "CHANGELOG.kvx", line)],
    )


def ordering_and_stopping() -> None:
    # parse error + lint error: only the parse stage is reported.
    root = fresh("parse_and_lint")
    set_key(root, "contracts/fs.kvx", "primitive.fs.write", "retries", "3")
    insert_after(root, "lib/fs.py", "from typing import TYPE_CHECKING, Any, TypedDict", "import os")
    rc, rep, _ = cwr("lint", root)
    st = statuses(rep)
    R.check(
        "4.1",
        rc == 2
        and st["parse"] == "failed"
        and st["resolve"] == "not_run"
        and st["lint"] == "not_run"
        and not stage_diags(rep, "lint")
        and {d["code"] for d in all_diags(rep)} == {"CWR-C001"},
        f"unknown key + host import: stops at parse with only C001 [{brief(rep)}], resolve/lint not_run",
    )
    # resolve error + lint errors.
    root = fresh("resolve_and_lint")
    set_key(root, "world.kvx", "world", "modules", '["fs", "kv", "search", "publish", "world"]')
    insert_after(root, "lib/fs.py", "from typing import TYPE_CHECKING, Any, TypedDict", "import os")
    set_key(
        root,
        "contracts/fs.kvx",
        "primitive.fs.delete",
        "preconditions",
        '[["not fs.lock(path)", "LOCKED"]]',
    )
    rc, rep, _ = cwr("build", root)
    st = statuses(rep)
    R.check(
        "4.1",
        rc == 2
        and st["parse"] == "passed"
        and st["resolve"] == "failed"
        and all(st[s] == "not_run" for s in STAGES[2:])
        and {d["code"] for d in all_diags(rep)} == {"CWR-C003"},
        f"cwr build, undeclared module + host import + hidden precondition: stops at resolve with only C003, exit {rc}",
    )
    # Many lint findings at once: all reported, sorted, deterministic.
    root = fresh("many_lints")
    insert_after(
        root, "lib/kv.py", "from typing import TYPE_CHECKING, Any, TypedDict", "import random"
    )
    insert_after(
        root,
        "lib/fs.py",
        "from typing import TYPE_CHECKING, Any, TypedDict",
        "import os\nimport sys",
    )
    set_key(root, "contracts/kv.kvx", "primitive.kv.get", "doc", '""')
    set_key(
        root,
        "contracts/fs.kvx",
        "primitive.fs.delete",
        "preconditions",
        '[["not fs.lock(path)", "BUSY"]]',
    )
    set_key(root, "adapters/search.kvx", "adapter.search.query", "content_guaranteed", "true")
    runs = [cwr("lint", root) for _ in range(2)]
    text_runs = [run([str(CWR), "lint", str(root)]) for _ in range(2)]
    rep = runs[0][1]
    ds = stage_diags(rep, "lint")
    keys = [(d["file"], d["line"], d["col"], d["code"] or "", d["message"]) for d in ds]
    codes = sorted({d["code"] for d in ds})
    R.check(
        "4.1",
        runs[0][0] == 3
        and runs[0][2] == runs[1][2]
        and text_runs[0].stderr == text_runs[1].stderr
        and keys == sorted(keys)
        and codes == ["CWR-D001", "CWR-F001", "CWR-F002", "CWR-L001", "CWR-V001"]
        and len(ds) == 7,
        f"7 findings in one lint stage, codes {codes}: all reported, sorted by file/line/col/code, "
        f"JSON and text byte-identical over two runs",
    )
    located = all(d["file"] and d["line"] >= 1 and d["col"] >= 1 for d in ds)
    kvx_located = all(d["section"] and d["key"] for d in ds if d["file"].endswith(".kvx"))
    R.check(
        "4.1",
        located and kvx_located,
        "every diagnostic carries file, line and column; every KVX one its section and key",
    )
    sample = text_runs[0].stderr.splitlines()[0] if text_runs[0].stderr else ""
    R.check(
        "4.1",
        bool(re.match(r"^[\w/.-]+:\d+:\d+: lint: CWR-[A-Z]\d{3}", sample)),
        f"text form: {sample[:110]}",
    )


def lint_codes_listed() -> list[str]:
    text = COMPILER_MD.read_text()
    section = text.split("## 3. Lint codes", 1)[1].split("\n## ", 1)[0]
    return re.findall(r"^\| (CWR-[A-Z]\d{3}) \|", section, re.M)


def main() -> int:
    global CWR
    CWR, tests_ok, tests_evidence = cargo()
    R.check("4.1", tests_ok, f"cargo test -p cwr-compiler: {tests_evidence}")
    try:
        with OBS.observe("CWR-002.a1"):
            clean_sources()
        with OBS.observe("CWR-002.a1"):
            pack_example()
        with (
            OBS.observe("CWR-002.a2", match="hidden precondition|V001 located"),
            OBS.observe("CWR-002.a3", match="host import"),
            OBS.observe("CWR-002.a4", match="unknown key|C001 located"),
            OBS.observe("CWR-002.a5", match="duplicate primitive|C002 names"),
            OBS.observe("CWR-002.a6", match="missing module"),
        ):
            five_seeded_defects()
        with OBS.observe("CWR-002.a2"):
            visibility()
        failure_codes()
        closure_layout()
        with OBS.observe("CWR-002.a3"):
            imports()
        remaining_codes()
        ordering_and_stopping()
        listed = lint_codes_listed()
        missing = [c for c in listed if c not in TRIGGERED]
        R.check(
            "4.1",
            len(listed) == 11 and not missing,
            f"every lint code compiler.md lists ({', '.join(listed)}) triggered with its exact code"
            + (f" MISSING {missing}" if missing else ""),
        )
    finally:
        # Packaged Builds are read-only; make everything writable before removing it.
        for dirpath, dirnames, filenames in os.walk(SCRATCH):
            for n in dirnames + filenames:
                p = os.path.join(dirpath, n)
                if not os.path.islink(p):
                    os.chmod(p, 0o700)
        if SCRATCH.exists():
            os.chmod(SCRATCH, 0o700)
        shutil.rmtree(SCRATCH, ignore_errors=True)
    OBS.emit()
    failed = False
    for clause in CLAUSES:
        bad = R.bad[clause]
        ok = R.ok[clause]
        if bad or not ok:
            failed = True
            print(
                f"CLAUSE {clause} FAIL {len(bad)} of {len(bad) + len(ok)} checks failed: "
                + " | ".join(bad or ["no checks ran"])
            )
        else:
            print(f"CLAUSE {clause} PASS {len(ok)} checks: " + " | ".join(ok))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

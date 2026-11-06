#!/usr/bin/env python3
# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Task 1.2 driver: contract tests, ContractDiff, changelog gate and version derivation.

Cases CWR-003 and CWR-004; clauses 1.6, 3.6, 4.2, 4.3, 4.4 and 5.5 of spec/cwr/spec.kvx.

Builds the `cwr` binary with cargo, runs the unit tests of this task's `cwr-compiler` modules
(contract_tests, diff, changelog_gate, version, ephemeral; including the refworld contract suite,
run twice in the real ephemeral Instance), then drives `cwr build --json` and
`cwr diff` as subprocesses over:

* a clean copy of examples/refworld (README.md removed, the only file outside the World Source
  grammar): it must pass contract_tests, diff and changelog_gate on the first-Build path;
* copies of the six upgrade pairs examples/refworld-upgrades/UN/{A,B}. If a B side's
  fixtures/world/version.kvx still carries a `returned` value other than B's own version, the
  copies used here correct that one value (reported in the output; the generated pairs are not
  edited); a copy with a seeded stale value must fail contract tests at the fixture;
* seeded defects in copies (tampered lib, fixtures, changelogs and versions), each of which must
  stop the pipeline at the expected stage and exit code with a diagnostic located at the file,
  section and line concerned.

The ContractDiff is recomputed independently here (KVX read with a small reader of its own,
lib/ state records read with Python's ast module, manual/ prose regions rendered the way
model-surface.md section 2 places them, the kind and bump table of world-source.md
section 11, the pre-1.0 rule P15 of decisions.md) and compared with `cwr diff` output, and with
the frozen expectations in benchmark/upgrades.kvx.

Prints one line per clause, `CLAUSE <req.ac> PASS|FAIL <evidence>`, and exits 0 only when every
clause passes. Honours CARGO_TARGET_DIR; defaults to the workspace target dir.
Python 3.12, standard library only.
"""

from __future__ import annotations

import ast
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
PAIRS = REPO / "examples/refworld-upgrades"
UPGRADES = REPO / "benchmark/upgrades.kvx"
CLAUSES = ["1.6", "3.6", "4.2", "4.3", "4.4", "5.5"]
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
TIMEOUT = 900
TASK_MODULES = ["contract_tests::", "diff::", "changelog_gate::", "version::", "ephemeral::"]
PROSE_REGIONS = ["intro.md", "conventions.md", "examples.md", "types.md"]
ORDER = ["none", "patch", "minor", "major"]


class Results:
    def __init__(self) -> None:
        self.ok: dict[str, list[str]] = {c: [] for c in CLAUSES}
        self.bad: dict[str, list[str]] = {c: [] for c in CLAUSES}

    def check(self, clause: str, condition: bool, evidence: str) -> bool:
        (self.ok if condition else self.bad)[clause].append(evidence)
        return condition


R = Results()
OBS = protocol.Observer("1.2", protocol.ok_bad(R))
SCRATCH = Path(tempfile.mkdtemp(prefix="cwr-1_2-"))
CWR = Path()
SEEN: set[str] = set()
PROBE = [""]
_n = [0]


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT, **kw)


def target_dir() -> Path:
    env = os.environ.get("CARGO_TARGET_DIR")
    return Path(env) if env else REPO / "target"


def cargo() -> tuple[bool, str, str]:
    env = dict(os.environ)
    env.setdefault("CARGO_TARGET_DIR", str(target_dir()))
    b = run(["cargo", "build", "-p", "cwr-cli"], cwd=REPO, env=env)
    if b.returncode != 0:
        print(b.stderr[-4000:], file=sys.stderr)
        raise SystemExit("cargo build failed")
    # Only this task's modules: other tasks' unit tests in the crate are their drivers' business.
    t = run(
        [
            "cargo",
            "test",
            "-p",
            "cwr-compiler",
            "--lib",
            "--",
            "--nocapture",
            "--test-threads=1",
            *TASK_MODULES,
        ],
        cwd=REPO,
        env=env,
    )
    out = t.stdout + t.stderr
    passed = sum(map(int, re.findall(r"test result: ok\. (\d+) passed", out)))
    suite = re.search(r"REFWORLD_SUITE (.*)", out)
    probe = re.search(r"NONDETERMINISM_PROBE .*", out)
    PROBE[0] = probe.group(0) if probe else ""
    return (
        t.returncode == 0,
        f"{passed} cwr-compiler unit tests pass" if t.returncode == 0 else out[-3000:],
        suite.group(1) if suite else "",
    )


# ------------------------------------------------------------------ cwr invocations


def out_dir() -> Path:
    _n[0] += 1
    d = SCRATCH / f"out{_n[0]}"
    d.mkdir()
    return d


def build(src: Path, *extra: str, out: Path | None = None) -> tuple[int, dict]:
    p = run([str(CWR), "build", str(src), "--json", "--out", str(out or out_dir()), *extra])
    try:
        rep = json.loads(p.stdout)
    except json.JSONDecodeError:
        rep = {"stages": [], "stopped_at": None, "raw": p.stdout + p.stderr}
    return p.returncode, rep


def cwr_diff(a: Path | str, b: Path | str, *extra: str) -> tuple[int, dict | None, str]:
    p = run([str(CWR), "diff", str(a), str(b), *extra])
    try:
        return p.returncode, json.loads(p.stdout), p.stdout
    except json.JSONDecodeError:
        return p.returncode, None, p.stdout + p.stderr


def statuses(rep: dict) -> dict[str, str]:
    return {s["stage"]: s["status"] for s in rep.get("stages", [])}


def diags(rep: dict, stage: str | None = None) -> list[dict]:
    return [
        d
        for s in rep.get("stages", [])
        if stage is None or s["stage"] == stage
        for d in s["diagnostics"]
    ]


def passes_gate(rep: dict) -> bool:
    st = statuses(rep)
    # Only the stages up to the gate: generate and package belong to task 1.3.
    return all(st.get(s) == "passed" for s in STAGES[:6]) and not any(
        diags(rep, s) for s in STAGES[:6]
    )


def brief(rep: dict, limit: int = 3) -> str:
    ds = diags(rep)
    parts = [f"{d['code'] or d['kind']}@{d['file']}:{d['line']}" for d in ds[:limit]]
    return ",".join(parts) + (f"+{len(ds) - limit}" if len(ds) > limit else "")


# ------------------------------------------------------------------ source helpers


def copy(src: Path, name: str) -> Path:
    dst = SCRATCH / name
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("README.md", "__pycache__"))
    return dst


def edit(path: Path, old: str, new: str, count: int = 1) -> None:
    text = path.read_text()
    if text.count(old) < 1 or (count == 1 and text.count(old) != 1):
        raise SystemExit(f"tamper anchor not found exactly once in {path}: {old!r}")
    path.write_text(text.replace(old, new) if count != 1 else text.replace(old, new, 1))


def line_of(path: Path, prefix: str) -> int:
    for i, line in enumerate(path.read_text().splitlines(), 1):
        if line.startswith(prefix):
            return i
    return -1


def version_of(root: Path) -> str:
    return re.search(r'^version = "([^"]+)"', (root / "world.kvx").read_text(), re.M).group(1)


def fix_version_fixture(root: Path) -> bool:
    """Point fixtures/world/version.kvx's returned value at the source's own version."""
    p = root / "fixtures/world/version.kvx"
    text = p.read_text()
    new = re.sub(r'^returned = "[^"]*"', f'returned = "{version_of(root)}"', text, flags=re.M)
    p.write_text(new)
    return new != text


def rename_release(root: Path, old: str, new: str) -> None:
    """Renames a release (world.kvx too, when it is the current one)."""
    edit(
        root / "CHANGELOG.kvx",
        f'[release.{old}]\nversion = "{old}"',
        f'[release.{new}]\nversion = "{new}"',
    )
    if version_of(root) == old:
        edit(root / "world.kvx", f'version = "{old}"', f'version = "{new}"')
    fix_version_fixture(root)


def add_release(
    root: Path, version: str, summary: str, changes: list[tuple[str, str, str, str, str, str]]
) -> None:
    """Prepends release `version` with changes (id, kind, target, before, after, migration)."""
    log = root / "CHANGELOG.kvx"
    text = log.read_text()
    block = (
        f'[release.{version}]\nversion = "{version}"\ndate = "2026-11-06"\nsummary = {json.dumps(summary)}\n'
        f"changes = {json.dumps([c[0] for c in changes])}\n\n"
    )
    for cid, kind, target, before, after, mig in changes:
        block += (
            f"[change.{cid}]\nkind = {json.dumps(kind)}\ntarget = {json.dumps(target)}\nbefore = {json.dumps(before)}\n"
            f"after = {json.dumps(after)}\nmigration = {json.dumps(mig)}\n\n"
        )
    i = text.index("[release.")
    log.write_text(text[:i] + block + text[i:])
    edit(root / "world.kvx", f'version = "{version_of(root)}"', f'version = "{version}"')
    fix_version_fixture(root)


def remove_change(root: Path, cid: str) -> None:
    log = root / "CHANGELOG.kvx"
    text = log.read_text()
    text = re.sub(r'"' + re.escape(cid) + r'"(, )?', "", text, count=1).replace(", ]", "]")
    text = re.sub(r"\[change\." + re.escape(cid) + r"\]\n(?:[^\[\n].*\n)*\n?", "", text)
    log.write_text(text)


# ------------------------------------------------------------------ independent oracle


def kvx(path: Path) -> dict:
    """A small KVX reader of the driver's own: [section], key = <one JSON literal>, # comments."""
    out: dict = {}
    sec = None
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            sec = out.setdefault(line[1:-1], {})
            continue
        key, _, value = line.partition("=")
        sec[key.strip()] = json.loads(value.strip())
    return out


def shape_ast(text: str):
    """The typed state record of a lib/ module via Python's ast: a nested, comparable value."""
    tree = ast.parse(text)
    classes, aliases, initial = {}, {}, None
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and any(
            (isinstance(b, ast.Name) and b.id == "TypedDict")
            or (isinstance(b, ast.Attribute) and b.attr == "TypedDict")
            for b in node.bases
        ):
            classes[node.name] = [
                (s.target.id, s.annotation)
                for s in node.body
                if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)
            ]
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id.endswith("State")
        ):
            aliases[node.targets[0].id] = node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "INITIAL_STATE"
        ):
            initial = node.annotation

    def render(n, depth=0):
        if depth > 16:
            return "..."
        if isinstance(n, ast.Name):
            if n.id in classes:
                return ("record", tuple((f, render(a, depth + 1)) for f, a in classes[n.id]))
            if n.id in aliases:
                return render(aliases[n.id], depth + 1)
            return n.id
        if isinstance(n, ast.Subscript):
            return ("sub", render(n.value, depth + 1), render(n.slice, depth + 1))
        if isinstance(n, ast.Tuple):
            return tuple(render(e, depth + 1) for e in n.elts)
        if isinstance(n, ast.BinOp):
            return ("union", render(n.left, depth + 1), render(n.right, depth + 1))
        return ast.unparse(n)

    if initial is not None:
        return render(initial)
    roots = sorted(n for n in list(classes) + list(aliases) if n.endswith("State"))
    return tuple(render(ast.Name(id=n)) for n in roots)


class Side:
    def __init__(self, root: Path):
        self.root = root
        w = kvx(root / "world.kvx")
        self.version = w["world"]["version"]
        self.settings = {f"{s}.{k}": v for s in ("limits", "clock", "rng") for k, v in w[s].items()}
        self.contracts: dict[str, dict] = {}
        for d, prefix in (("contracts", "primitive."), ("adapters", "adapter.")):
            for f in sorted((root / d).glob("*.kvx")):
                for sec, block in kvx(f).items():
                    if sec.startswith(prefix):
                        self.contracts[sec[len(prefix) :]] = block
        self.state = {p.stem: shape_ast(p.read_text()) for p in sorted((root / "lib").glob("*.py"))}
        self.prose = prose_regions(
            {p.name: p.read_text() for p in sorted((root / "manual").glob("*.md"))}
        )


def demoted_block(text: str) -> str:
    """A prose file as the manual renders it: trailing whitespace trimmed, every Markdown heading
    outside code fences two levels deeper, each line ended by a newline."""
    body = text.rstrip()
    out, fence = [], False
    for line in body.split("\n") if body else []:
        t = line.rstrip()
        if t.startswith("```"):
            fence = not fence
        if not fence and t.startswith("#") and t.lstrip("#").startswith(" "):
            t = "##" + t
        out.append(t + "\n")
    return "".join(out)


def prose_regions(files: dict[str, str]) -> dict[str, str]:
    """manual/ prose by manual region (model-surface.md section 2): intro.md in section 1,
    conventions.md and examples.md in section 2 with every other prose file after examples.md under
    a '### <name>' heading, types.md in section 5. A region is its rendered text without the
    newlines that close it."""
    regions = {f"manual/{f}": demoted_block(files.get(f, "")) + "\n" for f in PROSE_REGIONS}
    for name in sorted(files):
        if name not in PROSE_REGIONS:
            regions["manual/examples.md"] += (
                f"### {name.removesuffix('.md')}\n\n" + demoted_block(files[name]) + "\n"
            )
    return {k: v.rstrip("\n") for k, v in regions.items()}


def subseq(short: list, long: list) -> bool:
    it = iter(long)
    return all(any(y == x for y in it) for x in short)


def oracle_diff(a: Side, b: Side) -> dict:
    entries, docs, prose = [], [], []

    def e(kind, target, field, before=None, after=None, bump="major", has=True):
        d = {"kind": kind, "target": target, "field": field, "bump": bump}
        if has:
            d["before"], d["after"] = before, after
        return d

    for name in sorted(set(a.contracts) | set(b.contracts)):
        o, n = a.contracts.get(name), b.contracts.get(name)
        if o is None:
            entries.append(e("primitive_added", name, "primitive", bump="minor", has=False))
            continue
        if n is None:
            entries.append(e("primitive_removed", name, "primitive", has=False))
            continue
        for f in ("side_effect", "returns"):
            if o[f] != n[f]:
                entries.append(e("precondition_changed", name, f, o[f], n[f]))
        if list(o["params"].items()) != list(n["params"].items()):
            entries.append(e(None, name, "params", o["params"], n["params"]))
        po, pn = o["preconditions"], n["preconditions"]
        if po != pn:
            kind = (
                "precondition_added"
                if subseq(po, pn)
                else "precondition_removed"
                if subseq(pn, po)
                else "precondition_changed"
            )
            entries.append(
                e(
                    kind,
                    name,
                    "preconditions",
                    po,
                    pn,
                    "minor" if kind == "precondition_removed" else "major",
                )
            )
        if o.get("effects", []) != n.get("effects", []):
            entries.append(
                e("effect_changed", name, "effects", o.get("effects", []), n.get("effects", []))
            )
        for f in ("idempotency", "timeout_ms", "endpoint"):
            if o.get(f) != n.get(f):
                entries.append(e("adapter_changed", name, f, o.get(f), n.get(f)))
        if "failures" in o or "failures" in n:
            # world-source.md section 11: an absent key compares as its default.
            for f, default in (("undeclared_status", "UNAVAILABLE"), ("retry_after_ratio", 1)):
                x, y = o.get(f, default), n.get(f, default)
                if x != y:
                    entries.append(e("adapter_changed", name, f, x, y))
        if set(o.get("failures") or []) != set(n.get("failures") or []):
            entries.append(
                e("failure_changed", name, "failures", o.get("failures"), n.get("failures"))
            )
        if o["doc"] != n["doc"]:
            docs.append(e("doc_only", name, "doc", o["doc"], n["doc"], "patch"))
    for k in sorted(set(a.settings) | set(b.settings)):
        x, y = a.settings.get(k), b.settings.get(k)
        if x != y:
            if isinstance(x, bool):
                stricter = x and not y
            else:
                stricter = (y > x) if k.startswith("clock.") else (y < x)
            entries.append(e("limit_changed", k, "value", x, y, "major" if stricter else "minor"))
    for m in sorted(set(a.state) | set(b.state)):
        if a.state.get(m) != b.state.get(m):
            entries.append(e("state_shape", m, "state", has=False))
    for r in sorted(set(a.prose) | set(b.prose)):
        if a.prose.get(r) != b.prose.get(r):
            prose.append(e("doc_only", r, "prose", a.prose.get(r), b.prose.get(r), "patch"))
    entries.sort(key=lambda d: (d["target"], d["field"], d["kind"] or "params"))
    docs.sort(key=lambda d: (d["target"], d["field"]))
    derived = max((d["bump"] for d in entries + docs + prose), key=ORDER.index, default="none")
    o_v, n_v = [tuple(map(int, v.split("."))) for v in (a.version, b.version)]
    if n_v <= o_v:
        declared = "none"
    else:
        declared = "major" if n_v[0] > o_v[0] else "minor" if n_v[1] > o_v[1] else "patch"
    literal = declared != "none" and ORDER.index(declared) >= ORDER.index(derived)
    pre = (
        declared != "none"
        and not literal
        and o_v[0] == 0
        and (declared, derived) in (("minor", "major"), ("patch", "minor"))
    )
    return {
        "entries": entries,
        "doc_entries": docs,
        "prose_entries": prose,
        "derived_bump": derived,
        "declared_bump": declared,
        "pre_1_0_rule_applied": pre,
        "accepted": literal or pre,
        "version": b.version,
        "previous_version": a.version,
    }


def compare(got: dict, want: dict) -> list[str]:
    """Compares cwr diff output with an expected diff; before/after are compared wherever the
    expectation carries them (state_shape renderings are implementation-defined)."""
    problems = []
    for lst in ("entries", "doc_entries", "prose_entries"):
        if lst not in want:
            continue
        g = {(x["kind"], x["target"], x["field"]): x for x in got.get(lst, [])}
        w = {(x["kind"], x["target"], x["field"]): x for x in want.get(lst, [])}
        if set(g) != set(w):
            problems.append(
                f"{lst}: only cwr {sorted(set(g) - set(w), key=str)}, only expected {sorted(set(w) - set(g), key=str)}"
            )
        for k in set(g) & set(w):
            for f in ("before", "after", "bump"):
                if f in w[k] and g[k].get(f) != w[k][f]:
                    problems.append(f"{lst} {k} {f}: cwr {g[k].get(f)!r} expected {w[k][f]!r}")
        if [(x["kind"], x["target"], x["field"]) for x in got.get(lst, [])] != sorted(
            g, key=lambda t: (t[1], t[2], t[0] or "params")
        ):
            problems.append(f"{lst} not sorted by target then field")
    for f in ("derived_bump", "declared_bump", "pre_1_0_rule_applied", "doc_only_entries"):
        if f in want and got.get(f) != want[f]:
            problems.append(f"{f}: cwr {got.get(f)!r} expected {want[f]!r}")
    return problems


# ------------------------------------------------------------------ checks


def check_unit_tests(ok: bool, evidence: str, suite: str) -> None:
    R.check("4.2", ok, evidence)
    m = dict(kv.split("=") for kv in suite.split()) if suite else {}
    good = (
        ok
        and m.get("deterministic") == "true"
        and int(m.get("cases", 0)) > 0
        and int(m.get("failure_cases", 0)) > 0
    )
    R.check("4.2", good, f"refworld contract suite in the ephemeral Instance: {suite or 'NOT RUN'}")
    R.check(
        "1.6",
        good and m.get("deterministic") == "true",
        f"refworld suite run twice in two fresh worker processes with the same seeds: equal outcome streams "
        f"(status, shape, side-effect class), {m.get('stream', '?')} outcomes each",
    )
    # The driver's own count of declared failures and fixture cases, to tie the suite to the source.
    decl = sum(
        len(b["preconditions"])
        for f in (REFWORLD / "contracts").glob("*.kvx")
        for s, b in kvx(f).items()
        if s.startswith("primitive.")
    )
    adapters = sum(
        len(b["failures"])
        for f in (REFWORLD / "adapters").glob("*.kvx")
        for s, b in kvx(f).items()
        if s.startswith("adapter.")
    )
    cases = sum(
        1 for f in (REFWORLD / "fixtures").rglob("*.kvx") for s in kvx(f) if s.startswith("case.")
    )
    R.check(
        "4.2",
        str(cases) == m.get("cases"),
        f"suite ran every fixture case of the source ({cases} cases counted independently); "
        f"{decl} declared primitive preconditions and {adapters} declared adapter failures are each required to be reached",
    )


def check_refworld(clean: Path) -> None:
    rc, rep = build(clean)
    st = statuses(rep)
    ok = passes_gate(rep)
    R.check(
        "4.2",
        ok,
        f"clean refworld: cwr build exit {rc}, contract_tests={st.get('contract_tests')} {brief(rep)}",
    )
    R.check(
        "4.4",
        ok and st.get("changelog_gate") == "passed",
        f"clean refworld 0.2.0 passes the changelog gate on the first-Build path (no previous Build under an empty --out): "
        f"diff={st.get('diff')} changelog_gate={st.get('changelog_gate')}, stopped_at {rep.get('stopped_at')}",
    )
    _rc2, rep2 = build(clean)
    R.check(
        "1.6",
        json.dumps(diags(rep2)) == json.dumps(diags(rep)) and statuses(rep2) == st,
        "two cwr build runs of one source give identical stage reports",
    )
    # Rebuilding against itself: equal version is refused.
    rc, rep = build(clean, "--previous", str(clean))
    ds = diags(rep, "changelog_gate")
    want_line = line_of(clean / "world.kvx", 'version = "')
    ok = rc == 6 and any(
        d["kind"] == "version_not_greater" and d["file"] == "world.kvx" and d["line"] == want_line
        for d in ds
    )
    v = [d for d in ds if d["kind"] == "version_not_greater"]
    R.check(
        "5.5",
        ok,
        f"refworld with --previous itself (0.2.0 -> 0.2.0): exit {rc}, version_not_greater at world.kvx:{want_line} [world] version: "
        f"{v[0]['message'][:90] if v else 'MISSING'}; the four 0.2.0 changes are also listed as matching no diff entry ({len(ds) - len(v)})",
    )


def check_contract_test_tampers(clean: Path) -> None:
    def expect_fail(
        name: str,
        clause: str,
        src: Path,
        file: str,
        section: str | None,
        primitive: str,
        text: str,
        kind: str | None = None,
        line_prefix: str | None = None,
    ) -> None:
        rc, rep = build(src)
        ds = diags(rep, "contract_tests")
        want_line = (
            line_of(src / file, line_prefix or f"[{section}]") if (line_prefix or section) else None
        )
        hit = [
            d
            for d in ds
            if d["file"] == file
            and (section is None or d["section"] == section)
            and d["primitive"] == primitive
            and text in d["message"]
            and (kind is None or d["kind"] == kind)
            and (want_line is None or d["line"] == want_line)
        ]
        R.check(
            clause,
            rc == 4 and statuses(rep).get("diff") == "not_run" and bool(hit),
            f"{name}: exit {rc}, stopped_at {rep.get('stopped_at')}, "
            + (
                f"{hit[0]['file']}:{hit[0]['line']} [{hit[0]['section']}] {hit[0]['primitive']}: {hit[0]['message'][:110]}"
                if hit
                else f"MISSING ({brief(rep)})"
            ),
        )

    t = copy(clean, "ct_undeclared")
    edit(
        t / "lib/kv.py",
        "def has(state: Any, ns: str, key: str) -> bool:\n",
        "import cwr_state\n\n\ndef has(state: Any, ns: str, key: str) -> bool:\n",
    )
    edit(
        t / "lib/kv.py",
        "    del namespaces[ns][key]\n",
        '    raise cwr_state.Failure("CONFLICT", "seeded defect")\n',
    )
    expect_fail(
        "kv.del raises undeclared CONFLICT after its preconditions passed",
        "1.6",
        t,
        "fixtures/kv/del.kvx",
        "case.ok",
        "kv.del",
        "does not declare CONFLICT (INVARIANT_VIOLATION)",
    )

    t = copy(clean, "ct_undeclared_ro")
    edit(
        t / "lib/world.py",
        "def history(state: Any, n: int) -> list:\n",
        'def history(state: Any, n: int) -> list:\n    import cwr_state\n    raise cwr_state.Failure("LOCKED")\n',
    )
    expect_fail(
        "read_only world.history raises undeclared LOCKED",
        "1.6",
        t,
        "fixtures/world/history.kvx",
        "case.ok",
        "world.history",
        "does not declare LOCKED (INVARIANT_VIOLATION)",
    )

    t = copy(clean, "ct_adapter_code")
    edit(t / "adapters/search/live.py", '503: "UNAVAILABLE"', '503: "REJECTED"')
    expect_fail(
        "search gateway maps 503 to undeclared REJECTED",
        "1.6",
        t,
        "fixtures/search/query.kvx",
        "case.unavailable",
        "search.query",
        "emitted REJECTED, which its contract does not declare",
    )

    t = copy(clean, "ct_effect")
    edit(
        t / "lib/kv.py",
        "    _namespaces(state).setdefault(ns, {})[key] = bytes(value)\n",
        "    _namespaces(state).setdefault(ns, {})[key] = bytes(value)[:1]\n",
    )
    expect_fail(
        "kv.set stores a truncated value",
        "4.2",
        t,
        "fixtures/kv/set.kvx",
        "case.ok_replace",
        "kv.set",
        "effect `kv.get(ns, key) == value` of kv.set does not hold after an ok outcome",
    )

    t = copy(clean, "ct_unreachable")
    p = t / "fixtures/fs/write.kvx"
    text = p.read_text()
    p.write_text(text[: text.index("[case.pre_2_limit]")] + text[text.index("[case.ok_create]") :])
    expect_fail(
        "fs.write fixture for precondition 2 removed",
        "4.2",
        t,
        "contracts/fs.kvx",
        "primitive.fs.write",
        "fs.write",
        "declared failure LIMIT of precondition 2",
        "unreachable_failure",
        line_prefix='preconditions = [["not fs.locked(path)", "LOCKED"], ["len(content)',
    )

    t = copy(clean, "ct_adapter_unreachable")
    p = t / "fixtures/search/query.kvx"
    text = p.read_text()
    p.write_text(
        text[: text.index("[case.rate_limited]")] + text[text.index("[case.unavailable]") :]
    )
    expect_fail(
        "search.query fixture for RATE_LIMITED removed",
        "4.2",
        t,
        "adapters/search.kvx",
        "adapter.search.query",
        "search.query",
        "declared failure RATE_LIMITED is not reached",
        "unreachable_failure",
        line_prefix="failures = ",
    )

    t = copy(clean, "ct_exception")
    edit(
        t / "lib/fs.py",
        '    return sorted(path for path in _fs(state)["files"] if path.startswith(prefix))',
        "    return [][0]",
    )
    expect_fail(
        "fs.list raises IndexError",
        "4.2",
        t,
        "fixtures/fs/list.kvx",
        "case.ok_prefix",
        "fs.list",
        "a Python exception escaped lib/",
    )

    t = copy(clean, "ct_expect")
    edit(
        t / "fixtures/fs/delete.kvx",
        'expect = "LOCKED"\nprecondition = 2',
        'expect = "NOT_FOUND"\nprecondition = 1',
    )
    expect_fail(
        "fs.delete fixture expecting the wrong code",
        "4.2",
        t,
        "fixtures/fs/delete.kvx",
        None,
        "fs.delete",
        "got LOCKED at precondition 2, the fixture expects NOT_FOUND at precondition 1",
    )

    t = copy(clean, "ct_nondeterministic")
    counter = SCRATCH / "ct_counter.txt"
    edit(
        t / "lib/fs.py",
        "def write(state: Any, path: str, content: bytes) -> None:\n",
        "def write(state: Any, path: str, content: bytes) -> None:\n"
        f'    with open({str(counter)!r}, "a+") as handle:\n        handle.seek(0)\n        seen = len(handle.read())\n        handle.write("x")\n'
        '    if seen >= 3:\n        raise KeyError("host-dependent outcome")\n',
    )
    # Through the CLI, host state is out of reach of lib/: `open` is refused at lint (CWR-L001),
    # before contract tests run.
    rc, rep = build(t)
    open_line = line_of(t / "lib/fs.py", "    with open(")
    hit = [
        d
        for d in diags(rep, "lint")
        if d["code"] == "CWR-L001" and d["file"] == "lib/fs.py" and d["line"] == open_line
    ]
    R.check(
        "1.6",
        rc == 3 and rep.get("stopped_at") == "lint" and bool(hit),
        f"fs.write reading host state (a counter file) through the CLI: exit {rc}, stopped at {rep.get('stopped_at')}, "
        + (f"CWR-L001 at lib/fs.py:{open_line}" if hit else f"MISSING ({brief(rep)})"),
    )
    # The same lib/ given to the contract-test stage directly (the unit test skips lint): the
    # second run differs and the stage fails at the fixture case.
    m = dict(re.findall(r"(\w+)=(\S+)", PROBE[0].split(" message=")[0])) if PROBE[0] else {}
    want_line = line_of(REFWORLD / "fixtures/fs/write.kvx", "[case.ok_create]")
    R.check(
        "1.6",
        m.get("kind") == "nondeterministic"
        and m.get("file") == "fixtures/fs/write.kvx"
        and m.get("line") == str(want_line)
        and m.get("section") == "case.ok_create"
        and m.get("primitive") == "fs.write"
        and "passed once and differ on the second run" in PROBE[0],
        "fs.write outcome depends on host state across the two runs (contract_tests unit test, lint skipped): "
        + (PROBE[0][len("NONDETERMINISM_PROBE ") :][:260] if PROBE[0] else "NOT RUN"),
    )


def check_pairs(frozen: dict) -> dict[str, tuple[Path, Path]]:
    sides = {}
    patched = []
    for u in ["U1", "U2", "U3", "U4", "U5", "U6"]:
        a, b = copy(PAIRS / u / "A", f"{u}A"), copy(PAIRS / u / "B", f"{u}B")
        if fix_version_fixture(b):
            patched.append(u)
        sides[u] = (a, b)
        rc, rep = build(b, "--previous", str(a))
        rc_d, got, raw = cwr_diff(a, b)
        exp = oracle_diff(Side(a), Side(b))
        problems = compare(got or {}, exp) if got else [f"cwr diff exit {rc_d}: {raw[:200]}"]
        fz = frozen[f"diff.{u}"]
        fprob = compare(
            got or {},
            {
                "entries": fz["entries"],
                "doc_entries": fz["doc_entries"],
                "derived_bump": fz["derived_bump"],
                "declared_bump": fz["declared_bump"],
                "pre_1_0_rule_applied": fz["pre_1_0_rule_applied"],
                "doc_only_entries": fz["doc_only_entries"],
            },
        )
        kinds = sorted({(x["kind"] or "params") for x in (got or {}).get("entries", [])})
        SEEN.update(kinds, {x["kind"] for x in (got or {}).get("doc_entries", [])})
        R.check(
            "4.3",
            rc_d == 0 and not problems,
            f"{u}: cwr diff A B equals the independent recomputation ({len(exp['entries'])} entries {kinds}, {len(exp['doc_entries'])} doc entries, "
            f"derived {exp['derived_bump']}, declared {exp['declared_bump']})"
            + (f" MISMATCH {problems[:3]}" if problems else ""),
        )
        R.check(
            "4.3",
            rc_d == 0 and not fprob,
            f"{u}: cwr diff equals frozen benchmark/upgrades.kvx [diff.{u}]"
            + (f" MISMATCH {fprob[:3]}" if fprob else ""),
        )
        R.check(
            "4.4",
            passes_gate(rep),
            f"{u}: cwr build B --previous A passes contract_tests, diff and changelog_gate "
            f"({exp['previous_version']} -> {exp['version']}, derived {exp['derived_bump']}, pre-1.0 rule "
            f"{'applied' if (got or {}).get('pre_1_0_rule_applied') else 'not applied'}) {brief(rep)}",
        )
        if u == "U1":
            R.check(
                "4.4",
                passes_gate(rep) and exp["derived_bump"] == "major",
                "U1: a documented stricter precondition (precondition_added fs.delete) passes with a major derived bump",
            )
        if u == "U6":
            R.check(
                "4.4",
                passes_gate(rep)
                and (got or {}).get("derived_bump") == "patch"
                and (got or {}).get("declared_bump") == "patch",
                "U6: a documentation-only change passes with a patch bump (0.2.0 -> 0.2.1)",
            )
        if u == "U5":
            st5 = [x for x in (got or {}).get("entries", []) if x["kind"] == "state_shape"]
            R.check(
                "3.6",
                passes_gate(rep)
                and len(st5) == 1
                and st5[0].get("migration") == "migrations/0_2_0_0_3_0_kv_namespaces.py",
                f"U5: state_shape kv entry names its migration {st5[0].get('migration') if st5 else None} and the Build passes the gate, "
                f"with the migration's fixtures checked for equality and idempotence in contract tests",
            )
        if u in ("U1", "U2", "U3", "U4", "U5"):
            R.check(
                "5.5",
                (got or {}).get("pre_1_0_rule_applied") is True
                and "pre-1.0 rule" in (got or {}).get("version_rule", "")
                and passes_gate(rep),
                f"{u}: 0.2.0 -> 0.3.0 with derived major is accepted by the pre-1.0 rule P15 and contract_diff.json states it",
            )
    rc1, _, raw1 = cwr_diff(*sides["U5"])
    moved = SCRATCH / "elsewhere" / "deeper"
    moved.mkdir(parents=True)
    a2, b2 = (
        shutil.copytree(sides["U5"][0], moved / "A"),
        shutil.copytree(sides["U5"][1], moved / "B"),
    )
    rc2, _, raw2 = cwr_diff(a2, b2)
    rc3, _, raw3 = cwr_diff(*sides["U5"])
    R.check(
        "4.3",
        rc1 == rc2 == rc3 == 0
        and raw1 == raw2 == raw3
        and raw1.endswith("}\n")
        and '\n  "' in raw1,
        f"contract_diff.json is canonical and byte-identical across repeated runs and across source locations ({len(raw1)} bytes, U5)",
    )
    # The generated pair, built as it is in the tree: when its version fixture already carries
    # B's version it must pass contract tests; when it carries a stale value it must fail them.
    rc, rep = build(PAIRS / "U1" / "B", "--previous", str(sides["U1"][0]))
    ct = statuses(rep).get("contract_tests")
    R.check(
        "4.2",
        (rc == 4 and ct == "failed") if "U1" in patched else (rc == 0 and ct == "passed"),
        f"generated examples/refworld-upgrades/U1/B as found (stale version fixture: {'U1' in patched}; "
        f"corrected in copies of {patched}): exit {rc}, contract_tests={ct}",
    )
    # Seeded defect: a stale world.version fixture in a copy is refused at the fixture's location.
    stale = copy(sides["U1"][1], "U1B_stale")
    edit(stale / "fixtures/world/version.kvx", 'returned = "0.3.0"', 'returned = "0.2.0"')
    rc, rep = build(stale, "--previous", str(sides["U1"][0]))
    hit = [
        d
        for d in diags(rep, "contract_tests")
        if d["primitive"] == "world.version" and d["file"] == "fixtures/world/version.kvx"
    ]
    R.check(
        "4.2",
        rc == 4 and bool(hit),
        f"U1/B copy with a stale world.version fixture fails contract tests at "
        f"{hit[0]['file']}:{hit[0]['line']}: {hit[0]['message'][:90]}"
        if hit
        else f"stale U1/B copy: exit {rc} {brief(rep)}",
    )
    return sides


def check_variants(clean: Path) -> None:
    """Synthesised one-change variants of refworld: each kind of entry and bump."""
    variants = []

    def variant(name, fn):
        v = copy(clean, f"var_{name}")
        fn(v)
        variants.append((name, v))

    variant("limit_looser", lambda v: edit(v / "world.kvx", "cpu_ms = 5000", "cpu_ms = 6000"))
    variant("limit_stricter", lambda v: edit(v / "world.kvx", "memory_mb = 256", "memory_mb = 128"))
    variant(
        "tick_larger", lambda v: edit(v / "world.kvx", "tick_per_turn = 10", "tick_per_turn = 20")
    )
    variant(
        "seed_hidden",
        lambda v: edit(v / "world.kvx", "seed_visible = true", "seed_visible = false"),
    )
    variant(
        "adapter_timeout",
        lambda v: edit(v / "adapters/search.kvx", "timeout_ms = 3000", "timeout_ms = 4000"),
    )
    variant(
        "adapter_failures",
        lambda v: edit(
            v / "adapters/search.kvx",
            'failures = ["INVALID", "LIMIT", "RATE_LIMITED", "UNAVAILABLE", "TIMEOUT"]',
            'failures = ["INVALID", "LIMIT", "RATE_LIMITED", "UNAVAILABLE", "TIMEOUT", "REJECTED"]',
        ),
    )
    variant(
        "precondition_removed",
        lambda v: edit(v / "contracts/fs.kvx", ', ["len(content) <= 1048576", "LIMIT"]', ""),
    )
    variant(
        "precondition_middle_removed",
        lambda v: edit(
            v / "contracts/kv.kvx",
            '[["len(value) <= 1024", "INVALID"], ["kv.used(ns) + len(value) <= 65536", "LIMIT"]]',
            '[["kv.used(ns) + len(value) <= 65536", "LIMIT"]]',
        ),
    )
    variant(
        "effect_changed",
        lambda v: edit(
            v / "contracts/kv.kvx",
            'effects = ["not kv.has(ns, key)"]',
            'effects = ["not kv.has(ns, key) and len(key) >= 0"]',
        ),
    )

    search = "[adapter.search.query]\n"
    variant(
        "adapter_undeclared_status",
        lambda v: edit(
            v / "adapters/search.kvx", search, search + 'undeclared_status = "INVALID"\n'
        ),
    )
    variant(
        "adapter_retry_ratio",
        lambda v: edit(v / "adapters/search.kvx", search, search + "retry_after_ratio = 3\n"),
    )
    variant(
        "prose_conventions",
        lambda v: (v / "manual/conventions.md").write_text(
            (v / "manual/conventions.md").read_text()
            + "\nA turn script may define helper functions.\n"
        ),
    )
    variant(
        "prose_examples_heading",
        lambda v: (v / "manual/examples.md").write_text(
            (v / "manual/examples.md").read_text()
            + "\n# One more example\n\nCall fs.exists first.\n"
        ),
    )
    variant(
        "prose_types",
        lambda v: (v / "manual/types.md").write_text(
            (v / "manual/types.md").read_text() + "\nRecords are plain dictionaries.\n"
        ),
    )

    def add_primitive(v):
        edit(
            v / "contracts/fs.kvx",
            "[primitive.fs.write]",
            '[primitive.fs.count]\nside_effect = "read_only"\nparams = {"prefix": "Path"}\nreturns = "int"\npreconditions = []\neffects = []\n'
            'doc = "Number of files whose path starts with prefix. Never fails."\n\n[primitive.fs.write]',
        )
        edit(
            v / "lib/fs.py",
            "def write(state: Any, path: str, content: bytes) -> None:\n",
            'def count(state: Any, prefix: str) -> int:\n    return sum(1 for path in _fs(state)["files"] if path.startswith(prefix))\n\n\n'
            "def write(state: Any, path: str, content: bytes) -> None:\n",
        )

    variant("primitive_added", add_primitive)

    def state_change(v):
        # NotRequired keeps every existing fixture state valid, so the change reaches the gate.
        edit(
            v / "lib/fs.py",
            "from typing import TYPE_CHECKING, Any, TypedDict",
            "from typing import TYPE_CHECKING, Any, NotRequired, TypedDict",
        )
        edit(
            v / "lib/fs.py",
            "    locks: list[str]\n",
            "    locks: list[str]\n    owners: NotRequired[dict[str, str]]\n",
        )

    variant("state_shape", state_change)
    variant(
        "doc_only",
        lambda v: edit(
            v / "contracts/fs.kvx",
            'doc = "Remove the file at path. Refused when the path does not exist or is locked."',
            'doc = "Remove the file at path; refused when the path does not exist or is locked."',
        ),
    )

    def param_order(v):
        edit(
            v / "contracts/kv.kvx",
            'params = {"ns": "str", "key": "str", "value": "bytes"}',
            'params = {"key": "str", "ns": "str", "value": "bytes"}',
        )
        edit(
            v / "lib/kv.py",
            "def set(state: Any, ns: str, key: str, value: bytes)",
            "def set(state: Any, key: str, ns: str, value: bytes)",
        )

    variant("param_order", param_order)
    want = {
        "limit_looser": ("limit_changed", "minor"),
        "limit_stricter": ("limit_changed", "major"),
        "tick_larger": ("limit_changed", "major"),
        "seed_hidden": ("limit_changed", "major"),
        "adapter_timeout": ("adapter_changed", "major"),
        "adapter_failures": ("failure_changed", "major"),
        "precondition_removed": ("precondition_removed", "minor"),
        "precondition_middle_removed": ("precondition_removed", "minor"),
        "effect_changed": ("effect_changed", "major"),
        "primitive_added": ("primitive_added", "minor"),
        "state_shape": ("state_shape", "major"),
        "doc_only": ("doc_only", "patch"),
        "param_order": ("params", "major"),
        "adapter_undeclared_status": ("adapter_changed", "major"),
        "adapter_retry_ratio": ("adapter_changed", "major"),
        "prose_conventions": ("doc_only", "patch"),
        "prose_types": ("doc_only", "patch"),
        "prose_examples_heading": ("doc_only", "patch"),
    }
    want_target = {
        "adapter_undeclared_status": ("search.query", "undeclared_status"),
        "adapter_retry_ratio": ("search.query", "retry_after_ratio"),
        "prose_conventions": ("manual/conventions.md", "prose"),
        "prose_types": ("manual/types.md", "prose"),
        "prose_examples_heading": ("manual/examples.md", "prose"),
    }
    seen = SEEN
    for name, v in variants:
        rc, got, raw = cwr_diff(clean, v)
        exp = oracle_diff(Side(clean), Side(v))
        problems = compare(got or {}, exp) if got else [f"exit {rc}: {raw[:200]}"]
        every = (
            (got or {}).get("entries", [])
            + (got or {}).get("doc_entries", [])
            + (got or {}).get("prose_entries", [])
        )
        kinds = {(x["kind"] or "params") for x in every}
        k, bump = want[name]
        ok = rc == 0 and not problems and k in kinds and (got or {}).get("derived_bump") == bump
        if name in want_target:
            ok = ok and [(x["target"], x["field"]) for x in every] == [want_target[name]]
        seen |= kinds
        R.check(
            "4.3",
            ok,
            f"variant {name}: cwr diff equals recomputation, kind {k}"
            + (f" on {'/'.join(want_target[name])}" if name in want_target else "")
            + f", derived {(got or {}).get('derived_bump')}"
            + (f" MISMATCH {problems[:2]}" if problems else ""),
        )
    # An adapter key written with its default value compares equal to the absent key.
    v = copy(clean, "var_adapter_defaults")
    edit(
        v / "adapters/search.kvx",
        search,
        search + 'undeclared_status = "UNAVAILABLE"\nretry_after_ratio = 1\n',
    )
    rc, got, raw = cwr_diff(clean, v)
    exp = oracle_diff(Side(clean), Side(v))
    problems = compare(got or {}, exp) if got else [f"exit {rc}: {raw[:200]}"]
    R.check(
        "4.3",
        rc == 0
        and not problems
        and not (got["entries"] or got["doc_entries"] or got["prose_entries"])
        and got["derived_bump"] == "none",
        "variant adapter_defaults: undeclared_status UNAVAILABLE and retry_after_ratio 1 written out "
        "give no entry (absent compares as its default)"
        + (f" MISMATCH {problems[:2]}" if problems else ""),
    )
    all_kinds = {
        "primitive_added",
        "primitive_removed",
        "precondition_added",
        "precondition_removed",
        "precondition_changed",
        "effect_changed",
        "failure_changed",
        "limit_changed",
        "state_shape",
        "adapter_changed",
        "doc_only",
        "params",
    }
    R.check(
        "4.3",
        seen >= all_kinds,
        f"every entry kind of world-source.md section 11 (plus params) exercised against the recomputation: "
        f"{sorted(seen)}"
        + (f" MISSING {sorted(all_kinds - seen)}" if not seen >= all_kinds else ""),
    )
    # Undeclared state-shape change is refused at the gate with CWR-M001.
    v = dict(variants)["state_shape"]
    add_release(
        v,
        "0.3.0",
        "fs state records owners.",
        [("fs_doc", "doc_only", "fs.write", "", "unchanged rules", "")],
    )
    rc, rep = build(v, "--previous", str(clean))
    ds = diags(rep, "changelog_gate")
    m = [
        d
        for d in ds
        if d["code"] == "CWR-M001"
        and d["primitive"] == "fs"
        and d["file"] == "CHANGELOG.kvx"
        and d["line"] == line_of(v / "CHANGELOG.kvx", "[release.0.3.0]")
    ]
    R.check(
        "3.6",
        rc == 6 and bool(m),
        f"fs state shape changed without a state_shape change and migration: exit {rc}, "
        + (
            f"CWR-M001 at CHANGELOG.kvx:{m[0]['line']} [release.0.3.0]"
            if m
            else f"MISSING ({brief(rep)})"
        ),
    )


def check_gate_tampers(sides: dict[str, tuple[Path, Path]]) -> None:
    def expect_gate(
        name: str,
        clause: str,
        src: Path,
        prev: Path,
        kind: str,
        file: str,
        line: int,
        text: str,
        code: str | None = None,
    ) -> None:
        rc, rep = build(src, "--previous", str(prev))
        ds = diags(rep, "changelog_gate")
        hit = [
            d
            for d in ds
            if d["kind"] == kind
            and d["file"] == file
            and d["line"] == line
            and text in d["message"]
            and (code is None or d["code"] == code)
        ]
        R.check(
            clause,
            rc == 6 and bool(hit),
            f"{name}: exit {rc}, "
            + (
                f"{kind}{'/' + code if code else ''} at {file}:{line}: {hit[0]['message'][:120]}"
                if hit
                else f"MISSING ({brief(rep)})"
            ),
        )
        return ds

    u1a, u1b = sides["U1"]
    t = copy(u1b, "g_undocumented")
    remove_change(t, "fs_delete_refuses_locked")
    expect_gate(
        "undocumented stricter precondition (U1/B without its precondition_added change)",
        "4.4",
        t,
        u1a,
        "uncovered_entry",
        "CHANGELOG.kvx",
        line_of(t / "CHANGELOG.kvx", "[release.0.3.0]"),
        "precondition_added fs.delete (preconditions) (bump major) is not covered",
    )

    t = copy(u1b, "g_older_release")
    remove_change(t, "fs_delete_refuses_locked")
    edit(t / "CHANGELOG.kvx", '"kv_set_quota"]', '"kv_set_quota", "fs_delete_refuses_locked"]')
    log = (u1b / "CHANGELOG.kvx").read_text()
    section = re.search(r"\[change\.fs_delete_refuses_locked\]\n(?:[^\[\n].*\n)*", log).group(0)
    (t / "CHANGELOG.kvx").write_text((t / "CHANGELOG.kvx").read_text() + "\n" + section)
    expect_gate(
        "change declared only in an older release (moved to 0.2.0)",
        "4.4",
        t,
        u1a,
        "uncovered_entry",
        "CHANGELOG.kvx",
        line_of(t / "CHANGELOG.kvx", "[release.0.3.0]"),
        "precondition_added fs.delete",
    )

    t = copy(u1b, "g_announced")
    edit(
        t / "CHANGELOG.kvx",
        'changes = ["fs_delete_refuses_locked"',
        'changes = ["kv_get_phantom", "fs_delete_refuses_locked"',
    )
    (t / "CHANGELOG.kvx").write_text(
        (t / "CHANGELOG.kvx").read_text()
        + '\n[change.kv_get_phantom]\nkind = "precondition_added"\ntarget = "kv.get"\nbefore = ""\nafter = "a rule that does not exist"\nmigration = ""\n'
    )
    expect_gate(
        "announced change that did not happen",
        "4.4",
        t,
        u1a,
        "unmatched_change",
        "CHANGELOG.kvx",
        line_of(t / "CHANGELOG.kvx", "[change.kv_get_phantom]") + 1,
        "matches no ContractDiff entry",
    )

    t = copy(u1b, "g_underbump")
    rename_release(t, "0.3.0", "0.2.1")
    expect_gate(
        "derived major declared as a patch pre-1.0 (0.2.0 -> 0.2.1)",
        "4.4",
        t,
        u1a,
        "version_below_derived_bump",
        "world.kvx",
        line_of(t / "world.kvx", 'version = "'),
        "below the derived bump even under the pre-1.0 rule",
    )

    t = copy(u1b, "g_equal")
    rename_release(t, "0.3.0", "0.2.5")
    a_hi = copy(u1a, "g_equal_prev")
    rename_release(a_hi, "0.2.0", "0.2.5")
    expect_gate(
        "version equal to the previous Build's (0.2.5 -> 0.2.5)",
        "5.5",
        t,
        a_hi,
        "version_not_greater",
        "world.kvx",
        line_of(t / "world.kvx", 'version = "'),
        "must be numerically greater",
    )

    u6a, u6b = sides["U6"]
    t = copy(u6b, "g_doc_uncovered")
    cid = re.search(r'changes = \["([^"]+)"', (u6b / "CHANGELOG.kvx").read_text()).group(1)
    remove_change(t, cid)
    edit(t / "CHANGELOG.kvx", "changes = []", 'changes = ["fs_read_doc_phantom"]')
    (t / "CHANGELOG.kvx").write_text(
        (t / "CHANGELOG.kvx").read_text()
        + '\n[change.fs_read_doc_phantom]\nkind = "doc_only"\ntarget = "fs.read"\nbefore = ""\nafter = "x"\nmigration = ""\n'
    )
    rc, rep = build(t, "--previous", str(u6a))
    ds = diags(rep, "changelog_gate")
    kinds = sorted({d["kind"] for d in ds})
    R.check(
        "4.4",
        rc == 6 and kinds == ["uncovered_entry", "unmatched_change"] and len(ds) == 2,
        f"doc_only change on the wrong target: exit {rc}, both the uncovered doc entry (fs.write) and the unmatched change (fs.read) listed in one report: {brief(rep)}",
    )

    # Literal rule from 1.0.0.
    a1 = copy(u1a, "g_v1_a")
    rename_release(a1, "0.2.0", "1.0.0")
    b_minor = copy(u1b, "g_v1_b_minor")
    rename_release(b_minor, "0.2.0", "1.0.0")
    rename_release(b_minor, "0.3.0", "1.1.0")
    expect_gate(
        "from 1.0.0 the derived major is literal (1.0.0 -> 1.1.0 refused)",
        "5.5",
        b_minor,
        a1,
        "version_below_derived_bump",
        "world.kvx",
        line_of(b_minor / "world.kvx", 'version = "'),
        "from 1.0.0 the derived bump applies literally",
    )
    b_major = copy(u1b, "g_v1_b_major")
    rename_release(b_major, "0.2.0", "1.0.0")
    rename_release(b_major, "0.3.0", "2.0.0")
    rc, rep = build(b_major, "--previous", str(a1))
    _, got, _ = cwr_diff(a1, b_major)
    R.check(
        "5.5",
        passes_gate(rep)
        and got
        and got["declared_bump"] == "major"
        and got["pre_1_0_rule_applied"] is False,
        f"1.0.0 -> 2.0.0 with derived major passes literally (pre_1_0_rule_applied false) {brief(rep)}",
    )


def check_version_rules(clean: Path) -> None:
    # Patch satisfies a derived minor before 1.0 and not after.
    loose = copy(clean, "v_loose")
    edit(loose / "world.kvx", "cpu_ms = 5000", "cpu_ms = 6000")
    add_release(
        loose,
        "0.2.1",
        "More CPU per turn.",
        [("cpu_more", "limit_changed", "limits.cpu_ms", "5000", "6000", "")],
    )
    rc, rep = build(loose, "--previous", str(clean))
    _, got, _ = cwr_diff(clean, loose)
    R.check(
        "5.5",
        passes_gate(rep)
        and got
        and got["derived_bump"] == "minor"
        and got["declared_bump"] == "patch"
        and got["pre_1_0_rule_applied"] is True,
        f"looser limit (derived minor) declared as 0.2.0 -> 0.2.1 passes by the pre-1.0 rule {brief(rep)}",
    )
    base1 = copy(clean, "v_base1")
    rename_release(base1, "0.2.0", "1.0.0")
    l1 = copy(loose, "v_loose1")
    rename_release(l1, "0.2.0", "1.0.0")
    rename_release(l1, "0.2.1", "1.0.1")
    rc, rep = build(l1, "--previous", str(base1))
    ds = diags(rep, "changelog_gate")
    R.check(
        "5.5",
        rc == 6 and any(d["kind"] == "version_below_derived_bump" for d in ds),
        f"the same change at 1.0.0 -> 1.0.1 is refused (derived minor, literal from 1.0.0): exit {rc} {brief(rep)}",
    )
    l2 = copy(loose, "v_loose2")
    rename_release(l2, "0.2.0", "1.0.0")
    rename_release(l2, "0.2.1", "1.1.0")
    rc, rep = build(l2, "--previous", str(base1))
    R.check("5.5", passes_gate(rep), f"... and accepted at 1.0.0 -> 1.1.0 {brief(rep)}")
    # Numeric, not lexical: 0.9.0 -> 0.10.0.
    a9 = copy(clean, "v_a9")
    rename_release(a9, "0.2.0", "0.9.0")
    b10 = copy(a9, "v_b10")
    edit(
        b10 / "contracts/fs.kvx",
        "[primitive.fs.write]",
        '[primitive.fs.count]\nside_effect = "read_only"\nparams = {"prefix": "Path"}\nreturns = "int"\npreconditions = []\neffects = []\n'
        'doc = "Number of files whose path starts with prefix. Never fails."\n\n[primitive.fs.write]',
    )
    edit(
        b10 / "lib/fs.py",
        "def write(state: Any, path: str, content: bytes) -> None:\n",
        'def count(state: Any, prefix: str) -> int:\n    return sum(1 for path in _fs(state)["files"] if path.startswith(prefix))\n\n\n'
        "def write(state: Any, path: str, content: bytes) -> None:\n",
    )
    (b10 / "fixtures/fs/count.kvx").write_text(
        '[meta]\nformat = "cwr.fixture"\nschema_version = 1\nmodule = "fs"\nprimitive = "fs.count"\n\n'
        '[case.ok]\nworld_time = 0\nstate = {"fs": {"files": {"/a/1": "x", "/b": "y"}, "locks": []}}\n'
        'params = {"prefix": "/a"}\nexpect = "ok"\nprecondition = null\nreturned = 1\n'
    )
    add_release(
        b10,
        "0.10.0",
        "fs.count added.",
        [
            (
                "fs_count",
                "primitive_added",
                "fs.count",
                "",
                "fs.count(prefix) counts files under prefix.",
                "",
            )
        ],
    )
    rc, rep = build(b10, "--previous", str(a9))
    _, got, _ = cwr_diff(a9, b10)
    R.check(
        "5.5",
        passes_gate(rep)
        and got
        and got["declared_bump"] == "minor"
        and got["derived_bump"] == "minor",
        f"versions compare numerically: 0.9.0 -> 0.10.0 is a minor increment and satisfies primitive_added (minor) {brief(rep)}",
    )
    R.check(
        "4.2",
        passes_gate(rep),
        "a new primitive with its own fixture (fs.count) passes contract tests (its returned value checked)",
    )


def check_migrations(sides: dict[str, tuple[Path, Path]]) -> None:
    u5a, u5b = sides["U5"]
    t = copy(u5b, "m_no_change")
    remove_change(t, "kv_namespaces")
    rc, rep = build(t, "--previous", str(u5a))
    ds = diags(rep)
    hit = [d for d in ds if d["code"] == "CWR-M001" and d["file"] == "CHANGELOG.kvx"]
    R.check(
        "3.6",
        rc in (2, 3, 4, 6)
        and bool(hit)
        and rep.get("stopped_at") in ("parse", "lint", "contract_tests", "changelog_gate"),
        f"U5/B without its state_shape change: exit {rc}, stopped_at {rep.get('stopped_at')}, "
        + (
            f"CWR-M001 at CHANGELOG.kvx:{hit[0]['line']}: {hit[0]['message'][:100]}"
            if hit
            else f"MISSING ({brief(rep)})"
        ),
    )
    t = copy(u5b, "m_empty")
    edit(
        t / "CHANGELOG.kvx",
        'migration = "migrations/0_2_0_0_3_0_kv_namespaces.py"',
        'migration = ""',
    )
    rc, rep = build(t, "--previous", str(u5a))
    want = line_of(t / "CHANGELOG.kvx", 'migration = ""')
    hit = [
        d
        for d in diags(rep)
        if d["code"] == "CWR-M001" and d["file"] == "CHANGELOG.kvx" and d["line"] == want
    ]
    R.check(
        "3.6",
        rc in (3, 6) and bool(hit),
        f'U5/B state_shape change with migration = "": exit {rc}, stopped_at {rep.get("stopped_at")}, '
        + (f"CWR-M001 at CHANGELOG.kvx:{want}" if hit else f"MISSING ({brief(rep)})"),
    )
    t = copy(u5b, "m_not_idempotent")
    mig = t / "migrations/0_2_0_0_3_0_kv_namespaces.py"
    edit(
        mig,
        "def migrate(state: dict) -> dict:\n",
        'def migrate(state: dict) -> dict:\n    state = dict(state)\n    state["kv"] = dict(state.get("kv", {}))\n    if set(state["kv"]) == {"namespaces"}:\n        state["kv"] = {"namespaces": dict(state["kv"]["namespaces"], extra={})}\n        return state\n',
    )
    rc, rep = build(t, "--previous", str(u5a))
    hit = [
        d
        for d in diags(rep, "contract_tests")
        if d["primitive"] == "migrations/0_2_0_0_3_0_kv_namespaces.py"
        and "not idempotent" in d["message"]
    ]
    R.check(
        "3.6",
        rc == 4 and bool(hit),
        f"U5/B migration that is not idempotent: exit {rc}, "
        + (
            f"{hit[0]['file']}:{hit[0]['line']} [{hit[0]['section']}]: {hit[0]['message'][:100]}"
            if hit
            else f"MISSING ({brief(rep)})"
        ),
    )
    t = copy(u5b, "m_wrong_after")
    edit(mig := t / "migrations/0_2_0_0_3_0_kv_namespaces.py", '"default"', '"main"', count=0)
    rc, rep = build(t, "--previous", str(u5a))
    hit = [d for d in diags(rep, "contract_tests") if "migrate(before) != after" in d["message"]]
    R.check(
        "3.6",
        rc == 4 and bool(hit),
        f"U5/B migration producing the wrong state: exit {rc}, "
        + (f"{hit[0]['file']}:{hit[0]['line']}" if hit else f"MISSING ({brief(rep)})"),
    )
    t = copy(u5b, "m_bad_shape")
    p = next((t / "fixtures/kv").glob("migration_*.kvx"))
    text = p.read_text()
    first = re.search(r"\[migration\.(\w+)\]", text).group(1)
    edit(
        mig := t / "migrations/0_2_0_0_3_0_kv_namespaces.py",
        "def migrate(state: dict) -> dict:\n",
        'def migrate(state: dict) -> dict:\n    return {"kv": {"spaces": {}}}\n',
    )
    rc, rep = build(t, "--previous", str(u5a))
    hit = [
        d
        for d in diags(rep, "contract_tests")
        if "does not validate against the new state shape" in d["message"]
    ]
    R.check(
        "3.6",
        rc == 4 and bool(hit),
        f"U5/B migration whose result does not fit the new typed state record: exit {rc}, "
        + (
            f"{hit[0]['file']}:{hit[0]['line']}: {hit[0]['message'][:110]}"
            if hit
            else f"MISSING ({brief(rep)}) first case {first}"
        ),
    )


def built_dir(rep: dict) -> Path | None:
    d = (rep.get("build") or {}).get("dir")
    return Path(d) if d else None


def gate_hits(rep: dict, kind: str, file: str, line: int, text: str) -> list[dict]:
    return [
        d
        for d in diags(rep, "changelog_gate")
        if d["kind"] == kind and d["file"] == file and d["line"] == line and text in d["message"]
    ]


def check_prose(clean: Path) -> None:
    """manual/ prose: a prose-only change is a doc_only patch (world-source.md section 11), it is
    seen identically from a source and from a Build, and the gate requires it to be declared."""
    out = out_dir()
    rc, rep = build(clean, out=out)
    base = built_dir(rep)
    R.check(
        "4.3",
        rc == 0 and base is not None and base.is_dir(),
        f"clean refworld built into a Build directory for the prose checks: exit {rc}, {base and base.name[:16]}",
    )
    if base is None:
        return
    prose = copy(clean, "prose_changed")
    conv = prose / "manual/conventions.md"
    conv.write_text(conv.read_text() + "\nPrefer one script that checks, acts and verifies.\n")
    exp = oracle_diff(Side(clean), Side(prose))

    # Undeclared: same version, default previous (the Build above, same name and version) and an
    # explicit previous Build. Equal contracts.json does not make a prose change a rebuild.
    rc, rep = build(prose, out=out)
    wl = line_of(prose / "world.kvx", 'version = "')
    hit = gate_hits(rep, "version_not_greater", "world.kvx", wl, "derived minimum bump patch")
    R.check(
        "5.5",
        rc == 6 and bool(hit),
        f"prose-only change at the same version 0.2.0 into the --out holding the 0.2.0 Build: exit {rc}, "
        + (
            f"version_not_greater at world.kvx:{wl}: {hit[0]['message'][:80]}"
            if hit
            else f"MISSING ({brief(rep)})"
        ),
    )

    # Declared as doc_only with each accepted target: passes; the packaged diff carries the entry.
    for target in ("manual", "manual/conventions.md", "fs"):
        v = copy(prose, f"prose_ok_{target.replace('/', '_')}")
        add_release(
            v,
            "0.2.1",
            "The conventions prose recommends one checking script.",
            [("conventions_prose", "doc_only", target, "", "one checking script", "")],
        )
        rc, rep = build(v, "--previous", str(base))
        packaged = None
        bd = built_dir(rep)
        if bd and (bd / "contract_diff.json").is_file():
            packaged = json.loads((bd / "contract_diff.json").read_text())
        exp_v = oracle_diff(Side(clean), Side(v))
        problems = compare(packaged or {}, exp_v) if packaged else ["no contract_diff.json"]
        R.check(
            "4.4",
            rc == 0 and passes_gate(rep) and not problems,
            f"prose-only release 0.2.1 declared as doc_only target {target!r} against the 0.2.0 Build: exit {rc}, "
            f"packaged contract_diff.json equals the recomputation ({len(exp_v['prose_entries'])} prose entry, derived "
            f"{exp_v['derived_bump']}, declared {exp_v['declared_bump']})"
            + (f" MISMATCH {problems[:2]}" if problems else ""),
        )
        if target == "manual" and bd:
            # The same prose difference seen from two Builds equals the one seen from two sources.
            rc_b, got_b, _ = cwr_diff(base, bd)
            rc_s, got_s, _ = cwr_diff(clean, v)
            same = (
                rc_b == rc_s == 0
                and got_b
                and got_s
                and all(
                    got_b[k] == got_s[k]
                    for k in (
                        "entries",
                        "doc_entries",
                        "prose_entries",
                        "derived_bump",
                        "declared_bump",
                    )
                )
                and got_b["prose_entries"] == (packaged or {}).get("prose_entries")
            )
            R.check(
                "4.3",
                bool(same) and not compare(got_b or {}, exp_v),
                f"cwr diff <Build 0.2.0> <Build 0.2.1> equals cwr diff over the two sources and the packaged diff: "
                f"prose entries {[e['target'] for e in (got_b or {}).get('prose_entries', [])]}",
            )
            rc_n, got_n, _ = cwr_diff(base, bd)
            R.check(
                "4.3",
                rc_n == 0 and got_n == got_b and got_b["previous"].startswith("sha256:"),
                "the Build-to-Build prose diff is deterministic across runs",
            )

    # A release with no change: the prose entry is uncovered, at the release line.
    v = copy(prose, "prose_undeclared")
    add_release(v, "0.2.1", "Nothing declared.", [])
    rc, rep = build(v, "--previous", str(base))
    rl = line_of(v / "CHANGELOG.kvx", "[release.0.2.1]")
    hit = gate_hits(rep, "uncovered_entry", "CHANGELOG.kvx", rl, "manual/conventions.md")
    R.check(
        "4.4",
        rc == 6 and bool(hit),
        f"prose-only release 0.2.1 that declares no change: exit {rc}, "
        + (
            f"uncovered_entry at CHANGELOG.kvx:{rl} [release.0.2.1]: {hit[0]['message'][:90]}"
            if hit
            else f"MISSING ({brief(rep)})"
        ),
    )
    # A doc_only change on a target that is neither manual nor a name of the world: unmatched.
    v = copy(prose, "prose_bogus_target")
    add_release(v, "0.2.1", "Bogus.", [("conventions_prose", "doc_only", "nosuch", "", "x", "")])
    rc, rep = build(v, "--previous", str(base))
    kl = line_of(v / "CHANGELOG.kvx", "[change.conventions_prose]") + 1
    hit = gate_hits(rep, "unmatched_change", "CHANGELOG.kvx", kl, "doc_only nosuch")
    R.check(
        "4.4",
        rc == 6 and bool(hit),
        f"prose change declared on target 'nosuch': exit {rc}, "
        + (f"unmatched_change at CHANGELOG.kvx:{kl} kind" if hit else f"MISSING ({brief(rep)})"),
    )
    # A prose change in a release with a rule change is covered by that change (as in U1, U3-U5).
    v = copy(prose, "prose_with_rule")
    edit(v / "world.kvx", "cpu_ms = 5000", "cpu_ms = 6000")
    add_release(
        v,
        "0.2.1",
        "More CPU per turn.",
        [("cpu_looser", "limit_changed", "limits.cpu_ms", "5000", "6000", "")],
    )
    rc, rep = build(v, "--previous", str(base))
    R.check(
        "4.4",
        rc == 0 and passes_gate(rep),
        f"prose change alongside a declared limit_changed (0.2.0 -> 0.2.1, derived minor accepted by P15): exit {rc} {brief(rep)}",
    )
    # A doc entry is covered by a doc_only change on its target or module, a change on exactly its
    # target, or its module's state_shape change; a rule change on the module does not cover it.
    # (0.2.0 -> 0.3.0: effect_changed is major, satisfied by a minor increment under P15.)
    v = copy(clean, "doc_module_rule_change")
    edit(
        v / "contracts/kv.kvx",
        'effects = ["not kv.has(ns, key)"]',
        'effects = ["not kv.has(ns, key) and len(key) >= 0"]',
    )
    edit(
        v / "contracts/kv.kvx",
        'doc = "Value stored under key in namespace ns."',
        'doc = "The value stored under key in namespace ns."',
    )
    add_release(
        v,
        "0.3.0",
        "kv.delete states its effect with the key length.",
        [("kv_delete_effect", "effect_changed", "kv", "not kv.has", "not kv.has and len", "")],
    )
    rc, rep = build(v, "--previous", str(base))
    rl = line_of(v / "CHANGELOG.kvx", "[release.0.3.0]")
    hit = gate_hits(rep, "uncovered_entry", "CHANGELOG.kvx", rl, "doc_only kv.get (doc)")
    others = [d for d in diags(rep, "changelog_gate") if d not in hit]
    R.check(
        "4.4",
        rc == 6 and len(hit) == 1 and not others,
        f"kv.get doc reworded while the release declares only effect_changed on module kv: exit {rc}, "
        + (
            f"only the doc entry is uncovered, at CHANGELOG.kvx:{rl} (the module change covers the kv effect)"
            if hit and not others
            else f"MISSING ({brief(rep)})"
        ),
    )
    with_doc = copy(v, "doc_module_rule_change_declared")
    edit(
        with_doc / "CHANGELOG.kvx",
        'changes = ["kv_delete_effect"]',
        'changes = ["kv_delete_effect", "kv_doc"]',
    )
    (with_doc / "CHANGELOG.kvx").write_text(
        (with_doc / "CHANGELOG.kvx").read_text()
        + '\n[change.kv_doc]\nkind = "doc_only"\ntarget = "kv"\nbefore = "x"\nafter = "y"\nmigration = ""\n'
    )
    rc, rep = build(with_doc, "--previous", str(base))
    R.check(
        "4.4",
        rc == 0 and passes_gate(rep),
        f"the same with a doc_only change on module kv added: exit {rc} {brief(rep)}",
    )
    R.check(
        "4.3",
        exp["derived_bump"] == "patch"
        and [e["target"] for e in exp["prose_entries"]] == ["manual/conventions.md"],
        "recomputation: a conventions.md edit alone derives a patch bump with one prose entry",
    )


def check_builds(clean: Path) -> None:
    """Previous Builds: a verified Build directory is read; a missing or unverifiable one stops the diff stage."""
    rc0, rep0 = build(clean)
    own = built_dir(rep0)
    if own is None:
        R.check("4.3", False, f"clean refworld did not build: exit {rc0} {brief(rep0)}")
        return
    mb = SCRATCH / "ownbuild"
    shutil.copytree(own, mb)
    rc, got, raw = cwr_diff(mb, mb)
    R.check(
        "4.3",
        rc == 0
        and got
        and got["entries"] == got["doc_entries"] == got["prose_entries"] == []
        and got["previous"].startswith("sha256:")
        and got["derived_bump"] == "none",
        f"cwr diff over a hash-verified Build directory (refworld built by this driver): exit {rc}, previous {got and got['previous'][:20]}..., derived none",
    )
    bad = SCRATCH / "ownbuild_tampered"
    shutil.copytree(own, bad)
    manual = bad / "manual.md"
    manual.chmod(0o644)
    manual.write_text(manual.read_text() + "x")
    rc, got, raw = cwr_diff(bad, mb)
    R.check(
        "4.3",
        rc == 5 and "hashes to" in raw and str(bad) in raw,
        f"cwr diff with a Build whose bytes changed: exit {rc} ({raw.strip()[:110]})",
    )
    rc, rep = build(clean, "--previous", "sha256:" + "0" * 64)
    ds = diags(rep, "diff")
    R.check(
        "4.4",
        rc == 5
        and ds
        and rep.get("stopped_at") == "diff"
        and statuses(rep).get("changelog_gate") == "not_run",
        f"--previous naming a hash with no directory: exit {rc}, stopped at diff, nothing later run ({brief(rep)})",
    )
    rc, rep = build(clean, "--previous", str(bad))
    ds = diags(rep, "diff")
    R.check(
        "4.4",
        rc == 5 and ds and str(bad) in ds[0]["message"],
        f"--previous naming a Build that fails hash verification: exit {rc}, names the directory",
    )


def main() -> int:
    global CWR
    ok, evidence, suite = cargo()
    CWR = target_dir() / "debug" / "cwr"
    with (
        OBS.observe("CWR-003.a1", match="contract suite|suite ran every fixture"),
        OBS.observe("CWR-003.a5", match="run twice"),
    ):
        check_unit_tests(ok, evidence, suite)
    clean = copy(REFWORLD, "refworld")
    removed = sorted(
        p.name
        for p in REFWORLD.iterdir()
        if p.is_file() and p.name not in ("world.kvx", "CHANGELOG.kvx")
    )
    frozen = kvx(UPGRADES)
    with (
        OBS.observe("CWR-003.a1", match="clean refworld: cwr build"),
        OBS.observe("CWR-003.a5", match="identical stage reports"),
    ):
        check_refworld(clean)
    with (
        OBS.observe("CWR-003.a2", match="truncated value|effect"),
        OBS.observe("CWR-003.a3", match="removed"),
        OBS.observe("CWR-003.a4", match="undeclared"),
        OBS.observe("CWR-003.a5", match="host state"),
    ):
        check_contract_test_tampers(clean)
    with (
        OBS.observe("CWR-004.a1", match="independent recomputation|equals frozen"),
        OBS.observe("CWR-004.a3", match="documented stricter precondition"),
        OBS.observe("CWR-004.a4", match="documentation-only change passes with a patch"),
        OBS.observe("CWR-004.a5", match="state_shape"),
    ):
        sides = check_pairs(frozen)
    with (
        OBS.observe("CWR-004.a1", match="recomputation"),
        OBS.observe("CWR-004.a5", match="state shape changed without"),
    ):
        check_variants(clean)
    with (
        OBS.observe("CWR-004.a2", match="undocumented stricter|uncovered"),
        OBS.observe(
            "CWR-004.a6",
            match="below the derived bump|derived major declared as a patch|version equal to the previous",
        ),
    ):
        check_gate_tampers(sides)
    with OBS.observe("CWR-004.a6", match="1\\.0\\.0|pre-1\\.0"):
        check_version_rules(clean)
    with OBS.observe("CWR-004.a5"):
        check_migrations(sides)
    with OBS.observe("CWR-004.a4", match="patch|prose-only"):
        check_prose(clean)
    check_builds(clean)
    print(f"# scratch {SCRATCH}; refworld copy without {removed}", file=sys.stderr)
    OBS.emit()
    all_ok = True
    for c in CLAUSES:
        good = R.ok[c] and not R.bad[c]
        all_ok &= bool(good)
        if good:
            print(f"CLAUSE {c} PASS {len(R.ok[c])} checks; " + " | ".join(R.ok[c]))
        else:
            print(f"CLAUSE {c} FAIL " + " | ".join(R.bad[c] or ["no checks ran"]))
    if all_ok:
        # Builds are made read-only by the compiler; make them writable to remove them.
        def writable(fn, path, _exc):
            os.chmod(os.path.dirname(path), 0o755)
            os.chmod(path, 0o755)
            fn(path)

        shutil.rmtree(SCRATCH, onexc=writable)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())

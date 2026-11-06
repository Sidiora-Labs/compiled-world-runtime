#!/usr/bin/env python3
# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: Sidiora DEV OSS-1.0
"""Task 3.2 driver: experimental ablation Builds and the kernel gate.

Case CWR-013. Exercises clauses 5.3, 10.1, 10.2, 10.3, 10.4 and 10.5 against the real
implementation. It builds the `cwr` binary, runs the unit tests of the experimental compiler
modules and of the kernel drift injector, then drives `cwr` as a subprocess only:

* the control Build of `examples/refworld` and, from the same source, the experimental Builds
  drift-1 (spec/cwr/examples/refworld/drift/drift-1.kvx, fs.delete at turn 8), drift-3 (three
  injections written here), silent-change, version-only and one-primitive;
* for the notice renderings, upgrade pair U1 of `examples/refworld-upgrades`: A, B, and B built
  with --x-version-only, --x-silent-change and --x-one-primitive.

Independent oracles in this file: the Build hash (compiler.md section 7, recomputed here), the
file-by-file comparison of every experimental Build with its control, the expected drift turns
and outcomes, the notice text derived from B's CHANGELOG.kvx, and the expected LIMIT outcome.

Prints one line per clause, `CLAUSE <req.ac> PASS|FAIL <evidence>`, and exits 0 only when
every clause passes. Honours CARGO_TARGET_DIR; defaults to the workspace target dir.
Python 3.12, standard library only.
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
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
CLAUSES = ["5.3", "10.1", "10.2", "10.3", "10.4", "10.5"]
TIMEOUT = 900
DRIFT1 = REPO / "spec/cwr/examples/refworld/drift/drift-1.kvx"
REFWORLD = REPO / "examples/refworld"
U1 = REPO / "examples/refworld-upgrades/U1"
FLAGS = ["x-drift", "x-silent-change", "x-version-only", "x-one-primitive"]


class Results:
    def __init__(self) -> None:
        self.ok: dict[str, list[str]] = {c: [] for c in CLAUSES}
        self.bad: dict[str, list[str]] = {c: [] for c in CLAUSES}

    def check(self, clause: str, condition: bool, evidence: str) -> bool:
        (self.ok if condition else self.bad)[clause].append(evidence)
        return bool(condition)

    def report(self) -> int:
        failed = 0
        for c in CLAUSES:
            if self.bad[c] or not self.ok[c]:
                failed += 1
                why = "; ".join(self.bad[c]) or "no evidence recorded"
                print(f"CLAUSE {c} FAIL {why}")
            else:
                print(f"CLAUSE {c} PASS {'; '.join(self.ok[c])}")
        return 1 if failed else 0


R = Results()


def run(cmd: list, **kw) -> subprocess.CompletedProcess:
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True, timeout=TIMEOUT, **kw)


def target_dir() -> Path:
    return Path(os.environ.get("CARGO_TARGET_DIR") or REPO / "target")


def cargo() -> tuple[Path, str]:
    env = dict(os.environ)
    env["CARGO_TARGET_DIR"] = str(target_dir())
    b = run(["cargo", "build", "-p", "cwr-cli"], cwd=REPO, env=env)
    if b.returncode != 0:
        print(b.stderr[-4000:], file=sys.stderr)
        raise SystemExit("cargo build failed")
    evidence = []
    for crate, filt in [("cwr-compiler", "experimental"), ("cwr-kernel", "drift_injector")]:
        t = run(["cargo", "test", "-p", crate, "--lib", filt], cwd=REPO, env=env)
        n = sum(int(x) for x in re.findall(r"test result: ok\. (\d+) passed", t.stdout + t.stderr))
        if t.returncode != 0 or n == 0:
            print(t.stdout[-3000:] + t.stderr[-3000:], file=sys.stderr)
            raise SystemExit(f"cargo test -p {crate} {filt} failed")
        evidence.append(f"{crate}::{filt} {n} unit tests pass")
    return target_dir() / "debug" / "cwr", ", ".join(evidence)


# ------------------------------------------------------------------------------------------
# Independent oracles
# ------------------------------------------------------------------------------------------


def build_hash(d: Path) -> str:
    """compiler.md section 7: every entry sorted bytewise; a file is path NUL length NUL bytes,
    an empty directory is `path/` with length 0; manifest.kvx without its final hash line."""
    entries: list[tuple[str, bytes]] = []
    for p in sorted(d.rglob("*")):
        rel = p.relative_to(d).as_posix()
        if p.is_dir():
            if not any(p.iterdir()):
                entries.append((rel + "/", b""))
        else:
            data = p.read_bytes()
            if rel == "manifest.kvx":
                lines = data.decode().splitlines(keepends=True)
                if lines and lines[-1].startswith('hash = "sha256:'):
                    data = "".join(lines[:-1]).encode()
            entries.append((rel, data))
    h = hashlib.sha256()
    for rel, data in sorted(entries, key=lambda e: e[0].encode()):
        h.update(rel.encode() + b"\0" + str(len(data)).encode() + b"\0" + data)
    return h.hexdigest()


def manifest(d: Path) -> dict:
    out = {}
    for line in (d / "manifest.kvx").read_text().splitlines():
        m = re.match(r"^([a-z_]+) = (.*)$", line)
        if m:
            out[m.group(1)] = json.loads(m.group(2))
    return out


def files(d: Path) -> dict[str, bytes]:
    return {p.relative_to(d).as_posix(): p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file()}


def kvx_value(path: Path, section: str, key: str):
    current = None
    for line in path.read_text().splitlines():
        if line.startswith("["):
            current = line.strip("[]")
        elif current == section and line.startswith(f"{key} = "):
            return json.loads(line.split(" = ", 1)[1])
    return None


def copy_source(src: Path, dst: Path) -> Path:
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "README.md"))
    return dst


# ------------------------------------------------------------------------------------------
# The cwr command line
# ------------------------------------------------------------------------------------------


class Cwr:
    def __init__(self, exe: Path, work: Path) -> None:
        self.exe = exe
        self.work = work
        self.home = work / "home"
        self.n = 0

    def __call__(self, *args) -> subprocess.CompletedProcess:
        return run([self.exe, *args], cwd=self.work)

    def build(self, src: Path, out: Path, *flags) -> tuple[subprocess.CompletedProcess, Path | None]:
        p = self("build", src, "--out", out, *flags)
        m = re.search(r" at (\S+)$", p.stdout.strip())
        return p, (self.work / m.group(1) if m else None)

    def create(self, build: Path, *flags) -> subprocess.CompletedProcess:
        return self("instance", "create", build, "--home", self.home, "--seed", "7", *flags)

    def turn(self, iid: str, script: str) -> dict:
        self.n += 1
        f = self.work / f"script-{self.n}.py"
        f.write_text(script)
        p = self("instance", "turn", iid, "--home", self.home, "--script", f)
        if p.returncode != 0:
            raise SystemExit(f"turn failed: {p.stderr[-2000:]}")
        return json.loads(p.stdout)

    def ledger(self, iid: str) -> list[dict]:
        path = self.home / "instances" / iid / "ledger.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def context(self, iid: str) -> tuple[str, dict]:
        text = self("instance", "context", iid, "--home", self.home).stdout
        meta = json.loads(self("instance", "context", iid, "--home", self.home, "--json").stdout)
        return text, meta


def components(ctx: tuple[str, dict]) -> dict[str, str]:
    text, meta = ctx
    raw = text.encode()
    return {
        c["name"]: raw[c["offset"] : c["offset"] + c["length"]].decode() for c in meta["components"]
    }


DELETE_BIG = """import fs
from cwr import errors
if not fs.exists("big"):
    fs.write("big", b"x" * 5000)
try:
    fs.delete("big")
    return "deleted"
except errors.WorldError as e:
    return [e.code, e.primitive, e.precondition_index]
"""

DRIFT3 = """# drift-3 schedule for the task 3.2 driver: three injections.
[meta]
format = "cwr.drift"
schema_version = 1

[drift.1]
turn = 2
primitive = "fs.write"
change = "limit_changed"
expr = "len(content) <= 2048"
failure = "LIMIT"
doc = "From turn 2 fs.write refuses content above 2 KiB."

[drift.2]
turn = 4
primitive = "fs.lock"
change = "failure_changed"
expr = "not fs.locked(path)"
failure = "LOCKED"
doc = "From turn 4 locking a locked file is LOCKED instead of CONFLICT."

[drift.3]
turn = 6
primitive = "fs.delete"
change = "precondition_added"
expr = "fs.size(path) < 100"
failure = "LOCKED"
doc = "From turn 6 fs.delete refuses files of 100 bytes or more."
"""

DRIFT3_SCRIPT = """import fs
from cwr import errors
out = {}
def attempt(name, f):
    try:
        f()
        out[name] = "ok"
    except errors.WorldError as e:
        out[name] = e.code
attempt("write", lambda: fs.write("w", b"x" * 3000))
if not fs.exists("l"):
    fs.write("l", b"x")
if not fs.locked("l"):
    fs.lock("l")
attempt("relock", lambda: fs.lock("l"))
fs.write("d", b"y" * 200)
attempt("delete", lambda: fs.delete("d"))
return out
"""


# ------------------------------------------------------------------------------------------
# Scenarios
# ------------------------------------------------------------------------------------------


def scenario_builds(k: Cwr, src: Path) -> dict:
    """10.1 and 10.5 material: the control and five experimental Builds of one source."""
    out = k.work / "build"
    control = k.build(src, out)
    drift3 = k.work / "drift-3.kvx"
    drift3.write_text(DRIFT3)
    built = {"control": control}
    for label, flags in [
        ("drift-1", ["--x-drift", DRIFT1]),
        ("drift-3", ["--x-drift", drift3]),
        ("silent", ["--x-silent-change"]),
        ("version-only", ["--x-version-only"]),
        ("one-primitive", ["--x-one-primitive"]),
    ]:
        built[label] = k.build(src, out, *flags)
    bad = [label for label, (p, d) in built.items() if p.returncode != 0 or d is None]
    if bad:
        for label in bad:
            print(built[label][0].stdout + built[label][0].stderr, file=sys.stderr)
        raise SystemExit(f"builds failed: {bad}")
    return {label: d for label, (_, d) in built.items()} | {"drift-3-file": drift3}


def check_10_1(k: Cwr, b: dict, src: Path) -> None:
    flags = {
        "drift-1": "x-drift",
        "drift-3": "x-drift",
        "silent": "x-silent-change",
        "version-only": "x-version-only",
        "one-primitive": "x-one-primitive",
    }
    control = b["control"]
    m = manifest(control)
    R.check(
        "10.1",
        control.parent.name == "build"
        and m["experimental"] is False
        and m["experimental_flag"] is None
        and build_hash(control) == control.name,
        f"control Build under build/ hashes to its name with experimental=false",
    )
    ok = []
    for label, flag in flags.items():
        d = b[label]
        m = manifest(d)
        good = (
            d.parent == k.work / "xbuild"
            and m["experimental"] is True
            and m["experimental_flag"] == flag
            and build_hash(d) == d.name
            and m["hash"] == f"sha256:{d.name}"
        )
        ok.append(good)
    R.check(
        "10.1",
        all(ok),
        "5 experimental Builds under xbuild/<sha256> with experimental=true, their flag name and an independently recomputed hash",
    )
    combo = k("build", src, "--out", k.work / "combo" / "build", "--x-silent-change", "--x-one-primitive")
    R.check(
        "10.1",
        combo.returncode == 1 and not (k.work / "combo").exists(),
        f"two --x-* flags refused at argument parsing (exit {combo.returncode}), nothing written",
    )
    again, d2 = k.build(src, k.work / "again" / "build", "--x-one-primitive")
    R.check(
        "10.1",
        again.returncode == 0 and d2 is not None and d2.name == b["one-primitive"].name,
        "rebuilding one-primitive into another root reproduces the hash",
    )
    # Kernel gate and the flag on every ledger record.
    stamps = []
    for label, flag in flags.items():
        refused = k.create(b[label])
        allowed = k.create(b[label], "--allow-experimental")
        if refused.returncode != 3 or allowed.returncode != 0:
            R.check("10.1", False, f"{label}: create exit {refused.returncode} without the flag, {allowed.returncode} with it")
            continue
        iid = allowed.stdout.strip()
        k.turn(iid, "import world\nreturn world.now()\n")
        recs = k.ledger(iid)
        stamps.append(all(r["experimental"] == flag for r in recs) and len(recs) >= 3)
    cid = k.create(b["control"]).stdout.strip()
    k.turn(cid, "import world\nreturn world.now()\n")
    control_null = all(r["experimental"] is None for r in k.ledger(cid))
    R.check(
        "10.1",
        len(stamps) == 5 and all(stamps) and control_null,
        "kernel refuses each experimental Build without --allow-experimental (exit 3), loads it with the flag, and every ledger record carries the flag name; control records carry null",
    )
    hist = k.turn(
        k.create(b["drift-1"], "--allow-experimental").stdout.strip(),
        "import world\nreturn world.history(50)\n",
    )["returned"]
    R.check(
        "10.1",
        isinstance(hist, list) and all("experimental" not in r for r in hist),
        "the flag stays in the ledger and out of world.history",
    )


def check_10_2(k: Cwr, b: dict, src: Path) -> None:
    control_manual = (b["control"] / "manual.md").read_bytes()
    for label in ("drift-1", "drift-3"):
        d = b[label]
        R.check(
            "10.2",
            (d / "manual.md").read_bytes() == control_manual
            and (d / "contracts.json").read_bytes() == (b["control"] / "contracts.json").read_bytes(),
            f"{label}: manual.md and contracts.json byte-identical to the control Build",
        )
    R.check(
        "10.2",
        (b["drift-1"] / "fixtures/x-drift.kvx").read_bytes() == DRIFT1.read_bytes(),
        "drift-1 schedule embedded byte for byte inside the Build hash",
    )
    # drift-1: fs.delete gains a third precondition at turn 8.
    iid = k.create(b["drift-1"], "--allow-experimental").stdout.strip()
    manual_sha = []
    results = []
    for _ in range(10):
        manual_sha.append(json.loads(k("instance", "context", iid, "--home", k.home, "--json").stdout)["components"][0]["sha256"])
        results.append(k.turn(iid, DELETE_BIG)["returned"])
    expected = ["deleted"] * 7 + [["LOCKED", "fs.delete", 3]] * 3
    R.check("10.2", results == expected, f"drift-1 turns 1-10 fs.delete outcomes {results[6:9]} at turns 7-9: unchanged before turn 8, the injected precondition 3 refuses with LOCKED from turn 8")
    recs = k.ledger(iid)
    drifts = [r for r in recs if r["kind"] == "drift"]
    first_t8 = next(r for r in recs if r["turn"] == 8 and r["kind"] in ("primitive", "adapter"))
    R.check(
        "10.2",
        len(drifts) == 1
        and drifts[0]["turn"] == 8
        and drifts[0]["primitive"] == "fs.delete"
        and drifts[0]["change"] == "precondition_added"
        and drifts[0]["expr"] == kvx_value(DRIFT1, "drift.1", "expr")
        and drifts[0]["failure"] == "LOCKED"
        and drifts[0]["seq"] < first_t8["seq"]
        and drifts[0]["experimental"] == "x-drift",
        f"one ledger record of kind drift at turn 8 (seq {drifts[0]['seq'] if drifts else None}) naming fs.delete, the change, expression and failure, written before the turn's first primitive (seq {first_t8['seq']})",
    )
    sha_control = "sha256:" + hashlib.sha256(control_manual).hexdigest()
    R.check(
        "10.2",
        set(manual_sha) == {sha_control},
        "the manual the model receives is byte-identical to the control manual on all 10 turns, before and after the injection",
    )
    tail = k("instance", "tail", iid, "--home", k.home).stdout
    hist = k.turn(iid, "import world\nreturn world.history(500)\n")["returned"]
    R.check(
        "10.2",
        "drift" not in tail and all(r.get("kind") != "drift" for r in hist),
        "the drift record is hidden from the ledger tail and world.history",
    )
    # drift-3: three injections at turns 2, 4, 6.
    iid = k.create(b["drift-3"], "--allow-experimental").stdout.strip()
    outs = [k.turn(iid, DRIFT3_SCRIPT)["returned"] for _ in range(7)]
    want = []
    for t in range(1, 8):
        want.append(
            {
                "write": "LIMIT" if t >= 2 else "ok",
                "relock": "LOCKED" if t >= 4 else "CONFLICT",
                "delete": "LOCKED" if t >= 6 else "ok",
            }
        )
    turns = [r["turn"] for r in k.ledger(iid) if r["kind"] == "drift"]
    R.check(
        "10.2",
        outs == want and turns == [2, 4, 6],
        f"drift-3: limit_changed, failure_changed and precondition_added fire at turns {turns} and take effect exactly from those turns",
    )
    # The drift file is validated like a contract.
    bad = {
        "unknown primitive": ("fs.nothing", "precondition_added", "fs.exists(path)", "LOCKED", 2),
        "state_shape kind": ("fs.delete", "state_shape", "fs.exists(path)", "LOCKED", 2),
        "removing an absent precondition": ("fs.delete", "precondition_removed", "fs.size(path) < 3", "LOCKED", 2),
        "mutating call in expr": ("fs.delete", "precondition_added", "fs.write(path, path)", "LOCKED", 3),
    }
    seen = []
    for name, (prim, change, expr, failure, code) in bad.items():
        f = k.work / f"bad-{len(seen)}.kvx"
        f.write_text(
            f'[meta]\nformat = "cwr.drift"\nschema_version = 1\n\n[drift.1]\nturn = 3\nprimitive = "{prim}"\nchange = "{change}"\nexpr = "{expr}"\nfailure = "{failure}"\ndoc = ""\n'
        )
        out = k.work / f"bad-{len(seen)}" / "build"
        p = k("build", src, "--out", out, "--x-drift", f)
        xroot = out.parent / "xbuild"
        written = [e for e in xroot.iterdir() if not e.name.startswith(".")] if xroot.exists() else []
        seen.append((name, p.returncode, p.returncode == code and str(f) in p.stderr and not written))
    R.check(
        "10.2",
        all(s[2] for s in seen),
        "invalid drift files refused against their own path with nothing written: "
        + ", ".join(f"{n} exit {c}" for n, c, _ in seen),
    )


def check_10_4(k: Cwr, b: dict) -> None:
    d = b["one-primitive"]
    manual = (d / "manual.md").read_text()
    limits = manual.split("## 4. Limits and clock", 1)[1].split("\n## ", 1)[0]
    sentence = "In this world `primitive_calls = 1`"
    R.check(
        "10.4",
        "| `primitive_calls` | 1 |" in limits
        and sentence in limits
        and sentence not in (b["control"] / "manual.md").read_text()
        and json.loads((d / "contracts.json").read_text())["limits"]["primitive_calls"] == 1,
        "the one-primitive manual's limits section states primitive_calls = 1 in its table and in the appended sentence; contracts.json enforces 1",
    )
    iid = k.create(d, "--allow-experimental").stdout.strip()
    one = k.turn(iid, 'import fs\nfs.write("a", b"1")\nreturn "one"\n')
    env = k.turn(iid, 'import fs\nfs.write("b", b"2")\nfs.write("c", b"3")\n')
    err = env["error"] or {}
    state = json.loads(k("instance", "state", iid, "--home", k.home).stdout)
    files_now = sorted(state.get("fs", {}).get("files", {}))
    recs = [r for r in k.ledger(iid) if r["turn"] == 2 and r["kind"] == "primitive"]
    R.check(
        "10.4",
        one["status"] == "ok"
        and env["status"] == "partial"
        and err.get("code") == "LIMIT"
        and err.get("limit") == "primitive_calls"
        and "1 primitive call" in err.get("detail", "")
        and [t["status"] for t in env["trace"]] == ["ok", "error"]
        and env["trace"][1]["code"] == "LIMIT"
        and env["resources"]["remaining"]["primitive_calls"] == 0
        and files_now == ["a", "b"]
        and [r["outcome"]["code"] for r in recs] == [None, "LIMIT"],
        f"a one-call turn is ok; the second call of a turn returns LIMIT with limit primitive_calls (budget 1), is not executed (files {files_now}) and is recorded as LIMIT; status partial",
    )


def check_10_5(k: Cwr, b: dict, src: Path) -> None:
    control = b["control"]
    expect = {
        "drift-1": ("x-drift", [], "drift_schedule", {"fixtures/x-drift.kvx"}),
        "drift-3": ("x-drift", [], "drift_schedule", {"fixtures/x-drift.kvx"}),
        "silent": ("x-silent-change", [], "notice_rendering", set()),
        "version-only": ("x-version-only", [], "notice_rendering", set()),
        "one-primitive": (
            "x-one-primitive",
            [("limit_changed", "limits.primitive_calls", 500, 1)],
            "manual_sentence",
            {"contracts.json", "manual.md", "sdk/cwr/__init__.py"},
        ),
    }
    fc = files(control)
    for label, (flag, entries, xkind, allowed) in expect.items():
        d = b[label]
        p = k("diff", control, d)
        v = json.loads(p.stdout) if p.returncode == 0 else {}
        x = v.get("experimental", {})
        got = [(e["kind"], e["target"], e.get("before"), e.get("after")) for e in v.get("entries", [])]
        fx = files(d)
        differing = {f for f in set(fc) | set(fx) if fc.get(f) != fx.get(f)} - {"manifest.kvx"}
        mc, mx = manifest(control), manifest(d)
        same_manifest = all(mc[k_] == mx[k_] for k_ in ("name", "version", "previous", "compiler", "runtime"))
        R.check(
            "10.5",
            p.returncode == 0
            and got == entries
            and v.get("doc_entries") == []
            and x.get("flag") == flag
            and x.get("attributable") is True
            and x.get("unattributable") == []
            and [e["kind"] for e in x.get("experimental_entries", [])] == [xkind]
            and differing <= allowed
            and same_manifest,
            f"{label}: cwr diff lists {got or 'no rule entries'} plus {xkind}; files differing besides the manifest {sorted(differing)}",
        )
    # The one-primitive files differ only by the limit, the sentence and the contracts digest.
    sc = "sha256:" + hashlib.sha256(fc["contracts.json"]).hexdigest()
    sx = "sha256:" + hashlib.sha256((b["one-primitive"] / "contracts.json").read_bytes()).hexdigest()
    cm = fc["manual.md"].decode().replace(sc, sx).replace("| `primitive_calls` | 500 |", "| `primitive_calls` | 1 |")
    xm = (b["one-primitive"] / "manual.md").read_text()
    sentence_lines = [line for line in xm.splitlines() if "In this world `primitive_calls = 1`" in line]
    stripped = xm.replace(sentence_lines[0] + "\n\n", "", 1) if len(sentence_lines) == 1 else ""
    R.check(
        "10.5",
        stripped == cm,
        "one-primitive manual equals the control manual with only the primitive_calls value, the contracts digest and the one sentence changed",
    )
    # A pair built from a different source is refused.
    other = copy_source(src, k.work / "other-src")
    w = other / "world.kvx"
    w.write_text(re.sub(r"(?m)^adapter_calls = \d+$", "adapter_calls = 49", w.read_text()))
    p, od = k.build(other, k.work / "other" / "build", "--x-silent-change")
    v = json.loads(k("diff", control, od).stdout) if od else {}
    x = v.get("experimental", {})
    R.check(
        "10.5",
        od is not None and x.get("attributable") is False and any("limits.adapter_calls" in u for u in x.get("unattributable", [])),
        f"a silent-change Build from a different source is flagged unattributable: {x.get('unattributable', [])[:2]}",
    )
    plain = k("diff", control, control)
    R.check(
        "10.5",
        plain.returncode == 0 and "experimental" not in json.loads(plain.stdout),
        "a control-to-control diff carries no experimental section",
    )


def check_notices(k: Cwr) -> None:
    """5.3 and 10.3 on upgrade pair U1."""
    a = copy_source(U1 / "A", k.work / "u1" / "A")
    bsrc = copy_source(U1 / "B", k.work / "u1" / "B")
    out = k.work / "u1" / "build"
    built = {}
    for label, src, flags in [
        ("A", a, []),
        ("B", bsrc, []),
        ("version-only", bsrc, ["--x-version-only"]),
        ("silent", bsrc, ["--x-silent-change"]),
        ("one-primitive", bsrc, ["--x-one-primitive"]),
    ]:
        p, d = k.build(src, out, *flags)
        if p.returncode != 0 or d is None:
            raise SystemExit(f"U1 {label} build failed: {p.stdout}{p.stderr}")
        built[label] = d
    summary = kvx_value(bsrc / "CHANGELOG.kvx", "release.0.3.0", "summary")
    use = 'import fs\nfs.write("f", b"x")\nfs.delete("f")\nreturn 1\n'
    after = 'import fs\nfs.write("g", b"x")\nfs.lock("g")\ntry:\n    fs.delete("g")\nexcept Exception as e:\n    return e.code\nreturn "deleted"\n'
    runs = {}
    for label in ("B", "version-only", "silent", "one-primitive"):
        iid = k.create(built["A"]).stdout.strip()
        k.turn(iid, use)
        target = built[label]
        gate = k("instance", "upgrade", iid, target, "--home", k.home)
        up = gate if label == "B" else k("instance", "upgrade", iid, target, "--home", k.home, "--allow-experimental")
        if up.returncode != 0:
            raise SystemExit(f"upgrade to {label} failed: {up.stderr}")
        ctx = components(k.context(iid))
        env = k.turn(iid, after)
        later = components(k.context(iid))
        runs[label] = {
            "gate": gate.returncode,
            "stored": json.loads(up.stdout)["notice"],
            "ctx": ctx,
            "later": later,
            "env": env,
            "hash": manifest(target)["hash"],
        }
    full = runs["B"]
    note = full["ctx"].get("notice", "")
    R.check(
        "5.3",
        note.startswith(f"WORLD RECOMPILED  0.2.0 -> 0.3.0   ({manifest(built['A'])['hash']} -> {full['hash']})")
        and f"Summary: {summary}" in note
        and "fs.delete" in note
        and "fs.lock" not in note
        and "notice" not in full["later"],
        "control Build: the first context after upgrade carries old and new versions and hashes, the changelog summary and the change to fs.delete (used), not the doc change to the unused fs.lock; the notice is gone after that turn",
    )
    R.check(
        "5.3",
        runs["one-primitive"]["ctx"].get("notice", "").strip()
        == runs["one-primitive"]["stored"].strip(),
        "an experimental Build of another kind (one-primitive) still renders the full notice; only silent-change and version-only alter it",
    )

    def norm(s: str, label: str) -> str:
        return s.replace(runs[label]["hash"], "<B>")

    stored = {label: norm(r["stored"], label) for label, r in runs.items()}
    R.check(
        "10.3",
        stored["version-only"] == stored["silent"] == stored["B"],
        "upgrade stores the same recompile notice into the control, version-only and silent-change Builds (target hash normalised)",
    )
    vo = runs["version-only"]["ctx"].get("notice", "")
    R.check(
        "10.3",
        vo.strip() == runs["version-only"]["stored"].splitlines()[0]
        and "notice" not in runs["silent"]["ctx"]
        and note.strip() == full["stored"].strip(),
        "rendering: control shows the full notice, version-only only its version line, silent-change no notice component",
    )
    same = True
    for label in ("version-only", "silent"):
        mine, ctrl = runs[label], full
        for comp in ("manual", "tail", "envelope"):
            same &= norm(mine["ctx"].get(comp, ""), label) == norm(ctrl["ctx"].get(comp, ""), "B")
            same &= norm(mine["later"].get(comp, ""), label) == norm(ctrl["later"].get(comp, ""), "B")
        same &= norm(json.dumps(mine["env"]), label) == norm(json.dumps(ctrl["env"]), "B")
    R.check(
        "10.3",
        same and full["env"]["returned"] == "LOCKED",
        "apart from component 2 both Builds behave byte-identically to the control: same manual, tail and envelope before and after the post-upgrade turn, same envelope (fs.delete of a locked file LOCKED under the new rule)",
    )
    R.check(
        "10.1",
        runs["version-only"]["gate"] == 5 and runs["silent"]["gate"] == 5,
        "instance upgrade into an experimental Build is refused without --allow-experimental (exit 5)",
    )
    for label in ("version-only", "silent"):
        v = json.loads(k("diff", built["B"], built[label]).stdout)["experimental"]
        R.check(
            "10.3",
            v["attributable"] and [e["after"] for e in v["experimental_entries"]] == [
                "version_only" if label == "version-only" else "silent"
            ],
            f"{label} B differs from control B only by the notice-rendering marker",
        )


def main() -> int:
    exe, unit = cargo()
    R.check("10.2", True, unit)
    with tempfile.TemporaryDirectory(prefix="cwr-3_2-") as tmp:
        work = Path(tmp)
        k = Cwr(exe, work)
        src = copy_source(REFWORLD, work / "src")
        b = scenario_builds(k, src)
        check_10_1(k, b, src)
        check_10_2(k, b, src)
        check_10_4(k, b)
        check_10_5(k, b, src)
        check_notices(k)
    return R.report()


if __name__ == "__main__":
    sys.exit(main())

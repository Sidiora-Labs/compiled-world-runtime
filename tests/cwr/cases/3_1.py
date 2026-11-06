#!/usr/bin/env python3
# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Task 3.1 driver: context assembly and turn protocol (case CWR-012).

Exercises clauses 2.4, 2.5, 9.1, 9.2, 9.3, 9.4 and 9.5 against the real implementation. It builds the `cwr`
binary, runs the `surface` unit tests of `cwr-kernel`, compiles refworld (and, for the recompile notice, the
U1 upgrade pair) with the real `cwr build`, starts the real search stub server, and drives
`cwr instance create`, `cwr instance turn --message`, `cwr instance context`, `cwr instance snapshot` and
`cwr instance upgrade` as subprocesses. It renders the context before each of 50 turns of one Instance and
judges only what the kernel leaves behind: the context bytes, the envelopes on stdout, the ledger files, the
recorded scripts, snapshots and the Instance directory.

Independent oracles here: the Build's `manual.md` and `contracts.json` (byte comparison and entries
recomputed from the compiled contracts), the ledger records (every tail line and every `world.history`
record is recomputed from the ledger file), the protocol rules of model-surface.md section 3 (each
refused message is constructed to violate exactly one rule) and the frozen recompile notice of
benchmark/upgrades.kvx.

Prints one line per clause, `CLAUSE <req.ac> PASS|FAIL <evidence>`, and exits 0 only when every clause
passes. Honours CARGO_TARGET_DIR (default: the workspace target dir). Python 3.12, standard library only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import protocol

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parents[3]
REFWORLD = REPO / "examples/refworld"
UPGRADES = REPO / "examples/refworld-upgrades"
STUBS = REPO / "benchmark/stubs"
FROZEN = REPO / "benchmark/upgrades.kvx"
CLAUSES = ["2.4", "2.5", "9.1", "9.2", "9.3", "9.4", "9.5"]
TIMEOUT = 900
TURNS = 50

SECTIONS = [
    "1. What this world is",
    "2. How a turn works",
    "3. The failure envelope",
    "4. Limits and clock",
    "5. Modules overview",
    "6. Primitive reference",
    "7. Adapters",
    "8. Changes and versions",
    "9. Proposals",
    "10. Index",
]
PILLARS = ["Stability", "Direct action", "Controlled change"]
SEP_NOTICE = "\n==== cwr context: recompile notice ====\n"
SEP_TAIL = "\n==== cwr context: ledger tail ====\n"
SEP_ENVELOPE = "\n==== cwr context: last result envelope ====\n"
SEPARATORS = {"notice": SEP_NOTICE, "tail": SEP_TAIL, "envelope": SEP_ENVELOPE}
TAIL_KINDS = ("primitive", "adapter", "turn", "drift", "proposal")
TAIL_RE = re.compile(
    r"^t(\d+)\s+wt(\d+)\s+(\S+)\s+(ok|error|partial|drift)(?:\s+([A-Z_]+))?((?:\s+\w+=\S+)*)$"
)
ENVELOPE_KEYS = ["error", "resources", "returned", "status", "trace", "world"]
WORLD_KEYS = ["build", "time", "turn", "version"]
PROSE = "PROSE-MARKER-c0ffee"


class Results:
    def __init__(self) -> None:
        self.ok: dict[str, list[str]] = {c: [] for c in CLAUSES}
        self.bad: dict[str, list[str]] = {c: [] for c in CLAUSES}

    def check(self, clause: str, condition, evidence: str) -> bool:
        (self.ok if condition else self.bad)[clause].append(evidence)
        return bool(condition)


R = Results()
OBS = protocol.Observer("3.1", protocol.ok_bad(R))


def run(cmd, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(c) for c in cmd], capture_output=True, text=True, timeout=TIMEOUT, **kw
    )


def target_dir() -> Path:
    env = os.environ.get("CARGO_TARGET_DIR")
    return Path(env) if env else REPO / "target"


def build_binaries() -> tuple[Path, bool, str]:
    env = dict(os.environ)
    env.setdefault("CARGO_TARGET_DIR", str(target_dir()))
    b = run(["cargo", "build", "-p", "cwr-cli"], cwd=REPO, env=env)
    if b.returncode != 0:
        print(b.stderr[-4000:], file=sys.stderr)
        raise SystemExit("cargo build failed")
    t = run(["cargo", "test", "-p", "cwr-kernel", "--lib", "surface::"], cwd=REPO, env=env)
    out = t.stdout + t.stderr
    passed = sum(int(n) for n in re.findall(r"test result: ok\. (\d+) passed", out))
    ok = t.returncode == 0 and passed > 0
    return (
        target_dir() / "debug" / "cwr",
        ok,
        f"cargo test -p cwr-kernel surface:: rc={t.returncode} passed={passed}",
    )


def sha(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def canonical(v) -> str:
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def tree_digest(d: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(d.rglob("*")):
        if p.name == "lock":
            continue
        h.update(p.relative_to(d).as_posix().encode() + b"\0")
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


# ------------------------------------------------------------------------------------------------
# Stub server
# ------------------------------------------------------------------------------------------------


class Stub:
    def __init__(self, name: str, scratch: Path) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-I", str(STUBS / f"{name}_server.py"), "--port", "0", "--seed", "7"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            cwd=scratch,
        )
        ready = json.loads(self.proc.stdout.readline())
        if ready.get("event") != "ready":
            raise SystemExit(f"stub {name} did not start: {ready}")
        self.url = f"http://127.0.0.1:{ready['port']}"

    def stop(self) -> None:
        self.proc.terminate()
        self.proc.wait(timeout=10)


# ------------------------------------------------------------------------------------------------
# Kernel driver
# ------------------------------------------------------------------------------------------------


class Kernel:
    def __init__(self, cwr: Path, scratch: Path, stub: Stub) -> None:
        self.cwr = cwr
        self.scratch = scratch
        self.home = scratch / "home"
        self.stub = stub
        self.n = 0

    def build(self, name: str, src: Path, *flags: str) -> Path:
        copy = self.scratch / f"src-{name}"
        shutil.copytree(src, copy, ignore=shutil.ignore_patterns("README.md", "__pycache__"))
        p = run(
            [self.cwr, "build", copy, "--json", "--out", self.scratch / f"builds-{name}", *flags]
        )
        rep = json.loads(p.stdout) if p.stdout.strip().startswith("{") else {}
        b = rep.get("build") or {}
        if p.returncode != 0 or not b.get("dir"):
            raise SystemExit(
                f"cwr build {name} failed ({p.returncode}): {rep.get('summary')} {p.stderr[-800:]}"
            )
        return Path(b["dir"])

    def create(self, build: Path, *flags: str) -> str:
        p = run([self.cwr, "instance", "create", "--home", self.home, build, *flags])
        if p.returncode != 0:
            raise SystemExit(f"instance create failed: {p.stderr}")
        return p.stdout.strip()

    def dir(self, iid: str) -> Path:
        return self.home / "instances" / iid

    def message(self, iid: str, text: str) -> tuple[int, dict | None, str]:
        self.n += 1
        f = self.scratch / f"message-{self.n}.md"
        f.write_text(text)
        p = run(
            [
                self.cwr,
                "instance",
                "turn",
                iid,
                "--home",
                self.home,
                "--message",
                f,
                "--gateway",
                "live",
                "--stub",
                f"search={self.stub.url}",
            ]
        )
        env = None
        if p.stdout.strip():
            try:
                env = json.loads(p.stdout.strip().splitlines()[-1])
            except ValueError:
                env = None
        return p.returncode, env, p.stderr

    def context(self, iid: str, *flags: str) -> tuple[int, bytes, str]:
        p = subprocess.run(
            [str(c) for c in [self.cwr, "instance", "context", iid, "--home", self.home, *flags]],
            capture_output=True,
            timeout=TIMEOUT,
        )
        return p.returncode, p.stdout, p.stderr.decode(errors="replace")

    def context_json(self, iid: str, *flags: str) -> dict:
        rc, out, err = self.context(iid, "--json", *flags)
        if rc != 0:
            raise SystemExit(f"context --json failed: {err}")
        return json.loads(out)

    def ledger(self, iid: str) -> list[dict]:
        text = (self.dir(iid) / "ledger.jsonl").read_text()
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def meta(self, iid: str) -> dict:
        return json.loads((self.dir(iid) / "instance.json").read_text())


def py(body: str, before: str = "", after: str = "") -> str:
    return f"{before}```python\n{body}```\n{after}"


# ------------------------------------------------------------------------------------------------
# Independent context parsing
# ------------------------------------------------------------------------------------------------


def split_context(raw: bytes, manual: bytes) -> tuple[list[tuple[str, str]], str | None]:
    """Splits a raw context into (name, text) components using only the manual bytes and the fixed
    separators; returns an error string when the bytes are not exactly that concatenation."""
    if not raw.startswith(manual):
        return [], "context does not start with the Build's manual bytes"
    parts = [("manual", manual.decode())]
    rest = raw[len(manual) :].decode()
    order = ["notice", "tail", "envelope"]
    while rest:
        for name in order:
            if rest.startswith(SEPARATORS[name]):
                rest = rest[len(SEPARATORS[name]) :]
                nxt = [rest.find(SEPARATORS[n]) for n in order[order.index(name) + 1 :]]
                cut = min([i for i in nxt if i >= 0], default=len(rest))
                parts.append((name, rest[:cut]))
                rest = rest[cut:]
                order = order[order.index(name) + 1 :]
                break
        else:
            return parts, f"unexpected bytes after component {parts[-1][0]}: {rest[:60]!r}"
    rebuilt = manual.decode() + "".join(SEPARATORS[n] + t for n, t in parts[1:])
    if rebuilt.encode() != raw:
        return parts, "context is not exactly the concatenation of its components"
    return parts, None


def expected_tail(records: list[dict], k) -> list[dict]:
    shown = [r for r in records if r["kind"] in TAIL_KINDS]
    return shown if k == "full" else shown[-k:]


def tail_problems(lines: list[str], records: list[dict]) -> list[str]:
    problems = []
    if len(lines) != len(records):
        return [f"{len(lines)} lines for {len(records)} records"]
    for line, r in zip(lines, records, strict=False):
        m = TAIL_RE.match(line)
        if not m:
            problems.append(f"unparseable {line!r}")
            continue
        turn, wt, name, status, code = int(m[1]), int(m[2]), m[3], m[4], m[5]
        if r["kind"] == "turn":
            want = (
                r["turn"],
                r["world_time"],
                r.get("primitive") or "turn",
                r["status"],
                r.get("code"),
            )
        elif r["kind"] == "drift":
            want = (r["turn"], r["world_time"], r.get("primitive") or "drift", "drift", None)
        else:
            want = (
                r["turn"],
                r["world_time"],
                r["primitive"],
                r["outcome"]["status"],
                r["outcome"]["code"],
            )
        if (turn, wt, name, status, code) != want:
            problems.append(f"{line!r} != seq {r['seq']} {want}")
    return problems


# ------------------------------------------------------------------------------------------------
# Manual checks (9.2, 2.4, 2.5)
# ------------------------------------------------------------------------------------------------


def section_bodies(manual: str) -> dict[str, str]:
    heads = [(m.start(), m.group(1)) for m in re.finditer(r"^## (.+)$", manual, re.M)]
    out = {}
    for i, (pos, title) in enumerate(heads):
        end = heads[i + 1][0] if i + 1 < len(heads) else len(manual)
        out[title] = manual[pos:end]
    return out


def entry_block(manual: str, name: str) -> str | None:
    m = re.search(rf"^#### {re.escape(name)}\n\n```text\n(.*?)\n```", manual, re.M | re.S)
    return m.group(1) if m else None


def requires_lines(block: str) -> list[tuple[int, str, str]]:
    return [
        (int(n), e.strip(), c)
        for n, e, c in re.findall(r"(\d+)\. (.+?)\s+else ([A-Z_]+)$", block, re.M)
    ]


def ensures_lines(block: str) -> list[str]:
    out, inside = [], False
    for line in block.splitlines():
        if line.startswith("  Ensures    "):
            inside = True
            out.append(line[13:].strip())
        elif inside and line.startswith(" " * 13) and line[13:14] != " ":
            out.append(line[13:].strip())
        else:
            inside = False
    return out


def check_manual(build: Path, other_build: Path) -> dict:
    manual = (build / "manual.md").read_text()
    c = json.loads((build / "contracts.json").read_text())
    entries = c["primitives"] + c["adapters"]
    heads = re.findall(r"^## (.+)$", manual, re.M)
    R.check("9.2", heads == SECTIONS, f"manual has exactly the ten sections in order: {heads}")
    secs = section_bodies(manual)
    s1 = secs.get(SECTIONS[0], "")
    pillar_lines = re.findall(r"^\d\. \*\*(.+?)\.\*\* (.+)$", s1, re.M)
    other = section_bodies((other_build / "manual.md").read_text()).get(SECTIONS[0], "")
    other_pillars = re.findall(r"^\d\. \*\*(.+?)\.\*\* (.+)$", other, re.M)
    R.check(
        "9.2",
        [p for p, _ in pillar_lines] == PILLARS and pillar_lines == other_pillars,
        f"section 1 states the three pillars {[p for p, _ in pillar_lines]} in fixed wording (identical in a second Build's manual)",
    )
    s3 = secs.get(SECTIONS[2], "")
    rows = re.findall(r"^\| `([A-Z_]+)` \| (.+?) \| (.+?) \|$", s3, re.M)
    want = [(e["code"], e["meaning"], e["guidance"]) for e in c["envelope"]]
    R.check(
        "9.2",
        rows == want and len(rows) == 11,
        f"section 3 renders the {len(rows)} envelope codes with meaning and guidance verbatim from contracts.json, in order",
    )
    missing, bad_sig, bad_req, bad_eff, bad_fail, labels = [], [], [], [], [], []
    for p in entries:
        block = entry_block(manual, p["name"])
        if block is None:
            missing.append(p["name"])
            continue
        first = block.splitlines()[0]
        params = ", ".join(f"{n}: {t}" for n, t in p["params"])
        sig = f"{p['name']}({params}) -> {p['returns']}"
        if not (first.startswith(sig) and re.search(rf"\s\[{p['side_effect']}[\],]", first)):
            bad_sig.append(p["name"])
        want_req = [(i + 1, e, code) for i, (e, code) in enumerate(p["preconditions"])]
        if requires_lines(block) != want_req:
            bad_req.append(p["name"])
        ensures = ensures_lines(block)
        if p.get("effects") and ensures != p["effects"]:
            bad_eff.append(p["name"])
        if p["kind"] == "adapter":
            fails = re.search(r"^  May fail   (.+)$", block, re.M)
            if not fails or fails.group(1).split(", ") != p["failures"]:
                bad_fail.append(p["name"])
        labelled = "Content is not guaranteed" in block
        if labelled != (p.get("content_guaranteed") is False):
            labels.append(p["name"])
    R.check(
        "9.2",
        not missing,
        f"every one of {len(entries)} primitive and adapter entries is present as '#### name' plus a text block; missing {missing}",
    )
    R.check(
        "2.4",
        not (bad_sig or bad_req or bad_eff or bad_fail),
        f"{len(entries)} entries render parameters, return shape and side-effect class, preconditions numbered in declared order with codes, "
        f"effects and declared adapter failures exactly as contracts.json; mismatches sig={bad_sig} req={bad_req} eff={bad_eff} fail={bad_fail}",
    )
    cng = [p["name"] for p in entries if p.get("content_guaranteed") is False]
    R.check(
        "2.5",
        not labels and cng,
        f"the manual labels exactly the {len(cng)} content-not-guaranteed primitives {cng} ('Content is not guaranteed'); wrong {labels}",
    )
    R.check(
        "2.5",
        "Results of external primitives are labelled `content_guaranteed: false`" in s3
        and "an ok result guarantees the" in manual,
        "section 3 states the content_guaranteed label in envelopes, and each adapter entry states that ok guarantees only shape and one attempted call",
    )
    idx = secs.get(SECTIONS[9], "")
    indexed = set(re.findall(r"^- \[([a-z_.]+)\]", idx, re.M))
    R.check(
        "9.2",
        indexed == {p["name"] for p in entries},
        f"section 10 indexes all {len(indexed)} primitives and adapters",
    )
    return {p["name"]: p for p in entries}


def check_enforcement(kern: Kernel, build: Path, contracts: dict) -> None:
    """2.4: the documented precondition numbers and codes are the enforced ones."""
    manual = (build / "manual.md").read_text()
    iid = kern.create(build)
    kern.message(
        iid,
        py(
            "import fs\nfs.write(path='/a', content=b'1')\nfs.write(path='/b', content=b'2')\nfs.lock(path='/b')\n"
        ),
    )
    probes = [
        ("fs.delete", "path='/none'"),
        ("fs.delete", "path='/b'"),
        ("fs.move", "src='/none', dst='/z'"),
        ("fs.move", "src='/b', dst='/z'"),
        ("fs.move", "src='/a', dst='/b'"),
        ("fs.write", "path='/b', content=b'x'"),
        ("fs.write", "path='/c', content=b'x' * 1048577"),
        ("fs.lock", "path='/none'"),
        ("fs.lock", "path='/b'"),
        ("fs.unlock", "path='/a'"),
        ("search.query", "q='', limit=5"),
        ("search.query", "q='tennis', limit=51"),
    ]
    body = ["import fs, search, cwr", "out = []"]
    for name, args in probes:
        body += [
            "try:",
            f"    {name}({args})",
            "    out.append(None)",
            "except cwr.errors.WorldError as e:",
            "    out.append([e.code, e.primitive, e.precondition_index])",
        ]
    body.append("return out")
    rc, env, _ = kern.message(iid, py("\n".join(body) + "\n"))
    got = (env or {}).get("returned") or []
    bad = []
    for (name, args), r in zip(probes, got, strict=False):
        if not r:
            bad.append(f"{name}({args}) succeeded")
            continue
        code, prim, index = r
        doc = {n: c for n, _, c in requires_lines(entry_block(manual, name) or "")}
        declared = contracts[name]["preconditions"]
        if (
            prim != name
            or index is None
            or doc.get(index) != code
            or declared[index - 1][1] != code
        ):
            bad.append(f"{name}({args}) -> {r}, manual says {doc.get(index)}")
    R.check(
        "2.4",
        rc == 0 and len(got) == len(probes) and not bad,
        f"{len(got)}/{len(probes)} refusals across fs.delete, fs.move, fs.write, fs.lock, fs.unlock and search.query carry the code and "
        f"precondition_index of the manual's numbered Requires line; mismatches {bad or 'none'}",
    )


# ------------------------------------------------------------------------------------------------
# Turn protocol (9.3)
# ------------------------------------------------------------------------------------------------

VIOLATIONS = [
    (
        "no block",
        f"I think I should write /never-a.\n{PROSE}\nimport fs\nfs.write(path='/never-a', content=b'1')\n",
    ),
    (
        "two python blocks",
        py("import fs\nfs.write(path='/never-b', content=b'1')\n", "Plan:\n", "and then\n")
        + py("import fs\nfs.write(path='/never-c', content=b'1')\n"),
    ),
    (
        "python plus text block",
        py("import fs\nfs.write(path='/never-d', content=b'1')\n") + "```text\nnotes\n```\n",
    ),
    ("non-python block", "```bash\nrm -rf /\n```\n"),
    ("untagged block", "```\nimport fs\nfs.write(path='/never-e', content=b'1')\n```\n"),
    ("unclosed fence", "```python\nimport fs\nfs.write(path='/never-f', content=b'1')\n"),
    ("python3 tag", "```python3\nimport fs\nfs.write(path='/never-g', content=b'1')\n```\n"),
]
NEVER = ["/never-a", "/never-b", "/never-c", "/never-d", "/never-e", "/never-f", "/never-g"]


def check_protocol_refusal(
    kern: Kernel, iid: str, what: str, message: str
) -> tuple[bool, str, dict | None]:
    before = kern.ledger(iid)
    meta0 = kern.meta(iid)
    rc, env, err = kern.message(iid, message)
    after = kern.ledger(iid)
    meta1 = kern.meta(iid)
    new = after[len(before) :]
    turn_no = meta0["turn"] + 1
    e = (env or {}).get("error") or {}
    ok = (
        rc == 4
        and env is not None
        and env["status"] == "error"
        and e.get("code") == "INVALID"
        and e.get("primitive") == "turn.protocol"
        and env["trace"] == []
        and env["resources"]["primitive_calls"] == 0
        and env["world"]["time"] == meta0["world_time"]
        and env["world"]["turn"] == turn_no
        and len(new) == 1
        and new[0]["kind"] == "turn"
        and new[0]["primitive"] == "turn.protocol"
        and new[0]["status"] == "error"
        and new[0]["code"] == "INVALID"
        and new[0]["calls"] == 0
        and new[0]["world_time"] == before[-1]["world_time"] == meta1["world_time"]
        and not any(r["kind"] in ("primitive", "adapter") and r["turn"] == turn_no for r in after)
        and "sandbox=" not in err
        and not (kern.dir(iid) / "scripts" / f"turn-{turn_no:06d}.py").exists()
    )
    ev = f"{what}: exit {rc}, {e.get('code')} {e.get('primitive')}, {len(new)} record(s) {[r['kind'] for r in new]}, world_time {meta0['world_time']}->{meta1['world_time']}"
    return ok, ev, env


# ------------------------------------------------------------------------------------------------
# The 50-turn run (9.1, 9.3, 9.4, 9.5, 2.5)
# ------------------------------------------------------------------------------------------------


def turn_message(i: int) -> tuple[str, str]:
    """The model message of turn i of the main run and its kind."""
    if i % 7 == 3:
        what, msg = VIOLATIONS[(i // 7) % len(VIOLATIONS)]
        return "invalid:" + what, msg
    if i % 10 == 5:
        return "adapter", py(
            "import search\nhits = search.query(q='tennis', limit=3)\nreturn hits\n",
            "Searching.\n",
            PROSE + "\n",
        )
    if i % 10 == 7:
        return "partial", py(
            f"import fs\nfs.write(path='/p{i}', content=b'p')\nfs.delete(path='/missing')\n"
        )
    if i % 10 == 8:
        return "error", py("import fs\nfs.delete(path='/missing')\n")
    if i % 10 == 9:
        return "history", py("import world\nreturn world.history(6)\n")
    return "ok", py(
        f"import fs\nfs.write(path='/f{i}', content=b'{i}')\nreturn {i}\n",
        f"Turn {i}: {PROSE}\n",
        "Done.\n",
    )


def history_problems(returned, ledger_before: list[dict]) -> list[str]:
    want = ledger_before[-len(returned) :] if returned else []
    problems = []
    if not isinstance(returned, list) or len(returned) != 6:
        return [
            f"returned {type(returned).__name__} of length {len(returned) if isinstance(returned, list) else '-'}"
        ]
    for got, r in zip(returned, want, strict=False):
        if (got["seq"], got["kind"], got["turn"], got["world_time"]) != (
            r["seq"],
            r["kind"],
            r["turn"],
            r["world_time"],
        ):
            problems.append(f"history seq {got['seq']} != ledger seq {r['seq']}")
        if (got.get("primitive") or None) != (r.get("primitive") or None) or (
            got.get("params_hash") or None
        ) != (r.get("params_hash") or None):
            problems.append(f"history seq {got['seq']} primitive/params_hash differ")
        if set(got) & {"host_ts", "duration_us", "instance", "arm"}:
            problems.append(f"history seq {got['seq']} carries host fields")
    return problems


def main_run(kern: Kernel, build: Path, contracts: dict) -> dict:
    manual = (build / "manual.md").read_bytes()
    arm = "arm-label-not-for-the-model"
    a = kern.create(build, "--arm", arm, "--model", "model-id-not-for-the-model")
    b = kern.create(build, "--tail", "5")
    full = kern.create(build, "--tail", "full")
    prefix_ok, order_bad, env_bad, tail_bad, repeat_bad, mutate_bad = 0, [], [], [], [], []
    refusals, refusal_ok, refusal_ctx_bad = [], True, []
    history_bad, history_n, envelopes, adapter_ok = [], 0, [], []
    exec_ok, exec_bad = 0, []
    prev_env = None
    forbidden = {
        a,
        str(kern.home),
        str(kern.scratch),
        arm,
        "model-id-not-for-the-model",
        socket.gethostname(),
        "host_ts",
        "duration_us",
    }
    leaks = []
    for i in range(1, TURNS + 2):
        digest0 = tree_digest(kern.dir(a))
        rc, raw, err = kern.context(a)
        rc2, raw2, _ = kern.context(a)
        if raw != raw2 or rc != 0 or rc2 != 0:
            repeat_bad.append(i)
        if tree_digest(kern.dir(a)) != digest0:
            mutate_bad.append(i)
        parts, problem = split_context(raw, manual)
        names = [n for n, _ in parts]
        if problem is None:
            prefix_ok += 1
        want_names = ["manual", "tail"] + (["envelope"] if prev_env is not None else [])
        if problem or names != want_names:
            order_bad.append(f"turn {i}: {problem or names}")
        comp = dict(parts)
        if prev_env is not None and comp.get("envelope") != canonical(prev_env) + "\n":
            env_bad.append(i)
        ledger = kern.ledger(a)
        tlines = comp.get("tail", "").splitlines()
        tail_bad += [f"turn {i}: {p}" for p in tail_problems(tlines, expected_tail(ledger, 20))]
        text = raw.decode(errors="replace")
        leaked = [s for s in forbidden if s and s in text] + [
            r["host_ts"] for r in ledger if r["host_ts"] in text
        ]
        if leaked:
            leaks.append(f"turn {i}: {leaked[:3]}")
        if i > TURNS:
            break
        kind, msg = turn_message(i)
        if kind.startswith("invalid:"):
            ok, ev, env = check_protocol_refusal(kern, a, kind[8:], msg)
            refusals.append(ev)
            refusal_ok = refusal_ok and ok
            prev_env = env
            envelopes.append(env)
            # The next context must show the refusal line and world.time of turn n-1.
            parts2, _ = split_context(kern.context(a)[1], manual)
            comp2 = dict(parts2)
            last = (comp2.get("tail") or "").splitlines()[-1:] or [""]
            m = TAIL_RE.match(last[0])
            prior_wt = [r for r in kern.ledger(a) if r["kind"] == "turn"][-2]["world_time"]
            if not (
                m
                and m[3] == "turn.protocol"
                and m[5] == "INVALID"
                and int(m[2]) == prior_wt
                and json.loads(comp2.get("envelope", "{}"))["world"]["time"] == prior_wt
            ):
                refusal_ctx_bad.append(f"turn {i}: {last[0]!r}")
            continue
        before = kern.ledger(a)
        rc, env, err = kern.message(a, msg)
        if env is None or rc != 0:
            exec_bad.append(f"turn {i} {kind}: exit {rc} {err[-200:]}")
            prev_env = None
            continue
        envelopes.append(env)
        prev_env = env
        script = (kern.dir(a) / "scripts" / f"turn-{env['world']['turn']:06d}.py").read_text()
        body = msg.split("```python\n", 1)[1].split("```\n", 1)[0]
        want_status = {
            "ok": "ok",
            "adapter": "ok",
            "partial": "partial",
            "error": "error",
            "history": "ok",
        }[kind]
        good = env["status"] == want_status and script == body
        if kind == "ok":
            good = good and env["returned"] == i
        if kind == "history":
            hp = history_problems(env["returned"], before)
            history_n += 1
            history_bad += [f"turn {i}: {p}" for p in hp]
        if kind == "adapter":
            rec = [
                r
                for r in kern.ledger(a)
                if r["turn"] == env["world"]["turn"] and r["kind"] == "adapter"
            ]
            t = env["trace"]
            adapter_ok.append(
                env["status"] == "ok"
                and len(t) == 1
                and t[0]["primitive"] == "search.query"
                and t[0]["status"] == "ok"
                and t[0]["content_guaranteed"] is False
                and isinstance(env["returned"], list)
                and len(env["returned"]) <= 3
                and len(rec) == 1
                and rec[0]["outcome"]["shape"] == contracts["search.query"]["returns"]
                and rec[0]["outcome"]["side_effect"] == "external"
            )
        if good:
            exec_ok += 1
        else:
            exec_bad.append(
                f"turn {i} {kind}: status {env['status']} returned {str(env['returned'])[:60]} script_equal={script == body}"
            )
    # Visible state after the run: none of the refused scripts' writes happened.
    rc, env, _ = kern.message(a, py(f"import fs\nreturn [fs.exists(path=p) for p in {NEVER!r}]\n"))
    never_written = rc == 0 and env and env["returned"] == [False] * len(NEVER)
    prose_files = [
        p.relative_to(kern.dir(a)).as_posix()
        for p in kern.dir(a).rglob("*")
        if p.is_file() and PROSE.encode() in p.read_bytes()
    ]

    n_ctx = TURNS + 1
    R.check(
        "9.1",
        prefix_ok == n_ctx and not order_bad,
        f"{prefix_ok}/{n_ctx} contexts over {TURNS} turns start with the Build's manual.md byte for byte and are exactly manual, "
        f"tail, then the last envelope from turn 2 on, joined by the fixed separators; bad {order_bad[:3]}",
    )
    R.check(
        "9.1",
        not env_bad,
        f"component 4 equals the previous turn's envelope (canonical JSON) in every context; mismatched at turns {env_bad}",
    )
    R.check(
        "9.5",
        not repeat_bad and not mutate_bad,
        f"context rendering is pure: {n_ctx} renderings repeated byte-identically (bad {repeat_bad}) and left the Instance directory unchanged (bad {mutate_bad})",
    )
    R.check(
        "9.5",
        not leaks,
        f"no context carries the Instance id, home or scratch path, arm label, model id, hostname, host_ts values or host field names; leaks {leaks[:3]}",
    )

    R.check(
        "9.3",
        exec_ok == TURNS - len(refusals) and not exec_bad,
        f"{exec_ok} messages with exactly one fenced python block and prose around it executed as their block only; the recorded script equals the block, "
        f"top-level return became the envelope's returned value, statuses ok/error/partial as scripted; bad {exec_bad[:3]}",
    )
    R.check(
        "9.3",
        refusal_ok and len(refusals) == len(VIOLATIONS),
        f"{len(refusals)} protocol-invalid messages refused with exit 4, INVALID turn.protocol, empty trace, no sandbox, one turn record and world_time unchanged: {refusals}",
    )
    R.check(
        "9.3",
        not refusal_ctx_bad,
        f"after each refusal the next context's tail ends with the turn.protocol INVALID line at the previous world time and component 4 is the refusal; bad {refusal_ctx_bad}",
    )
    R.check(
        "9.3",
        bool(never_written) and not prose_files,
        f"no write in any refused message reached the world ({NEVER} all absent) and the prose outside the blocks is in no Instance file (found in {prose_files})",
    )

    R.check(
        "9.4",
        not tail_bad,
        f"every tail line of {n_ctx} contexts (K=20 from the header) renders one record with turn, world_time, primitive, status and code, "
        f"oldest first, the last 20 of the renderable records; problems {tail_bad[:3]}",
    )
    R.check(
        "9.5",
        history_n >= 4 and not history_bad,
        f"world.history(6) in {history_n} turns returned exactly the last 6 ledger records (seq, kind, turn, world_time, primitive, params_hash) without host fields; problems {history_bad[:3]}",
    )

    trace_bad = []
    for env in envelopes:
        for t in env["trace"]:
            want = (
                contracts[t["primitive"]].get("content_guaranteed", True)
                if t["primitive"] in contracts
                else True
            )
            if t.get("content_guaranteed") is not want:
                trace_bad.append(f"{t['primitive']}={t.get('content_guaranteed')}")
    n_items = sum(len(e["trace"]) for e in envelopes)
    R.check(
        "2.5",
        not trace_bad and n_items > 0,
        f"every one of {n_items} trace items in {len(envelopes)} envelopes carries content_guaranteed equal to its contract (false for adapters); wrong {trace_bad[:5]}",
    )
    R.check(
        "2.5",
        len(adapter_ok) >= 4 and all(adapter_ok),
        f"{sum(adapter_ok)}/{len(adapter_ok)} ok search.query turns against the stub: status ok with content_guaranteed false, ledger outcome records the "
        f"declared shape {contracts['search.query']['returns']} and side-effect class external, and the returned value has that shape",
    )
    shape_bad = [
        e
        for e in envelopes
        if sorted(e) != ENVELOPE_KEYS
        or sorted(e["world"]) != WORLD_KEYS
        or e["status"] not in ("ok", "error", "partial")
    ]
    R.check(
        "9.5",
        not shape_bad,
        f"all {len(envelopes)} envelopes have exactly status, returned, error, trace, world{{version, build, time, turn}} and resources",
    )

    # Across Instances and K values.
    rcb, rawb, _ = kern.context(b)
    rcf, rawf, _ = kern.context(full)
    _, eb = split_context(rawb, manual)
    _, ef = split_context(rawf, manual)
    R.check(
        "9.1",
        rcb == 0
        and rcf == 0
        and eb is None
        and ef is None
        and raw[: len(manual)] == rawb[: len(manual)] == rawf[: len(manual)],
        f"three Instances of one Build share the manual prefix byte for byte ({len(manual)} bytes, {sha(manual)})",
    )
    return {"a": a, "b": b, "full": full, "manual": manual}


def check_tail_sizes(kern: Kernel, build: Path, run_ids: dict) -> None:
    manual = run_ids["manual"]
    a, b, full = run_ids["a"], run_ids["b"], run_ids["full"]
    for i in range(1, 9):
        kern.message(b, py(f"import fs\nfs.write(path='/b{i}', content=b'1')\n"))
        kern.message(full, py(f"import fs\nfs.write(path='/b{i}', content=b'1')\n"))
    heads = [kern.ledger(x)[0]["tail_size"] for x in (a, b, full)]
    R.check(
        "9.4",
        heads == [20, 5, "full"],
        f"K is recorded in each Instance's header record: default {heads[0]}, --tail 5 {heads[1]}, --tail full {heads[2]!r}",
    )
    results = []
    for iid, k in ((a, 20), (b, 5), (full, "full")):
        lines = dict(split_context(kern.context(iid)[1], manual)[0]).get("tail", "").splitlines()
        want = expected_tail(kern.ledger(iid), k)
        results.append((len(lines), len(want), tail_problems(lines, want)))
    R.check(
        "9.4",
        all(n == w and not p for n, w, p in results),
        f"each context renders K from the header: {[(n, w) for n, w, _ in results]} lines for K 20, 5 and full",
    )
    overrides = []
    full_lines = dict(split_context(kern.context(a, "--tail", "full")[1], manual)[0])[
        "tail"
    ].splitlines()
    for k in ("5", "20", "full"):
        rc, raw, _ = kern.context(a, "--tail", k)
        lines = dict(split_context(raw, manual)[0]).get("tail", "").splitlines()
        want = full_lines if k == "full" else full_lines[-int(k) :]
        overrides.append(rc == 0 and lines == want and raw.startswith(manual))
    jsn = kern.context_json(a, "--tail", "5")
    R.check(
        "9.4",
        all(overrides)
        and jsn["tail_size"] == 5
        and len(full_lines) == len(expected_tail(kern.ledger(a), "full")),
        f"--tail 5, 20 and full render the last 5, 20 and all {len(full_lines)} lines of the same tail, with the manual prefix unchanged",
    )
    snaps = []
    for iid in (a, b, full):
        p = run([kern.cwr, "instance", "snapshot", iid, "--home", kern.home])
        h = p.stdout.strip().split(":")[-1]
        snap = (
            json.loads((kern.dir(iid) / "snapshots" / f"{h}.json").read_text())
            if p.returncode == 0
            else {}
        )
        snaps.append(snap.get("tail_size"))
    R.check("9.4", snaps == [20, 5, "full"], f"every snapshot records K: {snaps}")
    bad = [
        run([kern.cwr, "instance", "context", a, "--home", kern.home, "--tail", v]).returncode
        for v in ("0", "-1", "many")
    ]
    R.check(
        "9.4", all(rc != 0 for rc in bad), f"--tail 0, -1 and many are refused (exit codes {bad})"
    )
    # Operator JSON agrees with the raw bytes: offsets and hashes cover the whole context.
    raw = kern.context(a)[1]
    j = kern.context_json(a)
    cover = all(
        sha(raw[c["offset"] : c["offset"] + c["length"]]) == c["sha256"] for c in j["components"]
    )
    R.check(
        "9.5",
        cover
        and j["length"] == len(raw)
        and j["sha256"] == sha(raw)
        and [c["name"] for c in j["components"]] == ["manual", "tail", "envelope"],
        f"--json offsets and hashes of {[c['name'] for c in j['components']]} cover the {len(raw)} context bytes exactly; nothing else is in the context",
    )


# ------------------------------------------------------------------------------------------------
# Recompile notice (9.1)
# ------------------------------------------------------------------------------------------------


def frozen_notice(pair: str) -> str | None:
    sec = None
    for line in FROZEN.read_text().splitlines():
        if line.startswith("["):
            sec = line.strip("[]")
        elif sec == f"notice.{pair}" and line.startswith("text = "):
            return json.loads(line[len("text = ") :])
    return None


def check_notice(kern: Kernel) -> None:
    a_build = kern.build("u1a", UPGRADES / "U1/A")
    b_build = kern.build("u1b", UPGRADES / "U1/B")
    ma, mb = (a_build / "manual.md").read_bytes(), (b_build / "manual.md").read_bytes()
    iid = kern.create(a_build)
    kern.message(
        iid,
        py("import fs\nfor n in ['a', 'b']:\n    fs.write(path='/batch/one/' + n, content=b'1')\n"),
    )
    first = (UPGRADES / "U1/solutions/a1.py").read_text()
    _, env_n, _ = kern.message(iid, py(first if first.endswith("\n") else first + "\n"))
    parts0, _ = split_context(kern.context(iid)[1], ma)
    up = run([kern.cwr, "instance", "upgrade", iid, b_build, "--home", kern.home])
    if up.returncode != 0:
        R.check(
            "9.1",
            False,
            f"cwr instance upgrade is unavailable or refused the U1 upgrade (exit {up.returncode}: {up.stderr.strip()[-300:]}); the notice slot could not be observed",
        )
        return
    rc1, raw1, _ = kern.context(iid)
    raw1b = kern.context(iid)[1]
    parts1, prob1 = split_context(raw1, mb)
    comp1 = dict(parts1)
    names1 = [n for n, _ in parts1]
    hash_a = kern.ledger(iid)[0]["build"]
    hash_b = kern.meta(iid)["build"]
    want = frozen_notice("U1")
    want = want.replace("{hash_a}", hash_a).replace("{hash_b}", hash_b) if want else None
    notice = comp1.get("notice", "")
    stored = run([kern.cwr, "instance", "notice", iid, "--home", kern.home])
    R.check(
        "9.1",
        rc1 == 0
        and prob1 is None
        and names1 == ["manual", "notice", "tail", "envelope"]
        and raw1 == raw1b,
        f"first context after `cwr instance upgrade`: components {names1}, manual is the new Build's manual.md byte for byte ({sha(mb)[:19]}), notice second",
    )
    R.check(
        "9.1",
        stored.returncode == 0 and notice and notice.rstrip("\n") == stored.stdout.rstrip("\n"),
        f"the notice component is the kernel's stored recompile notice for this upgrade (`cwr instance notice`), unedited ({len(notice)} bytes: {notice.splitlines()[0] if notice else 'absent'!r})",
    )
    R.check(
        "9.1",
        want is not None and notice.rstrip("\n") == want,
        "the notice component equals the frozen U1 notice of benchmark/upgrades.kvx with the two Build hashes",
    )
    R.check(
        "9.1",
        comp1.get("tail") == dict(parts0).get("tail")
        and comp1.get("envelope") == canonical(env_n) + "\n"
        and json.loads(comp1["envelope"])["world"]["version"] == "0.2.0",
        "across the upgrade the tail is unchanged and component 4 is still turn n's envelope",
    )
    _, env2, _ = kern.message(iid, py("import world\nreturn world.version()\n"))
    parts2, prob2 = split_context(kern.context(iid)[1], mb)
    names2 = [n for n, _ in parts2]
    R.check(
        "9.1",
        prob2 is None
        and names2 == ["manual", "tail", "envelope"]
        and env2
        and env2["world"]["version"] == "0.3.0",
        f"after the first post-upgrade turn the notice is gone ({names2}) and the envelope reports version {env2 and env2['world']['version']}",
    )


# ------------------------------------------------------------------------------------------------


def main() -> int:
    cwr, unit_ok, unit_ev = build_binaries()
    R.check(
        "9.3",
        unit_ok,
        f"fence extraction unit tests (CommonMark fences, CRLF, tildes, indentation, lookalikes): {unit_ev}",
    )
    scratch = Path(tempfile.mkdtemp(prefix="cwr-3_1-"))
    stub = Stub("search", scratch)
    try:
        kern = Kernel(cwr, scratch, stub)
        ctl = kern.build("control", REFWORLD)
        u6b = kern.build("u6b", UPGRADES / "U6/B")
        with OBS.observe("CWR-012.a5"):
            contracts = check_manual(ctl, u6b)
        check_enforcement(kern, ctl, contracts)
        with (
            OBS.observe("CWR-012.a1", match="start with the Build's manual\\.md byte for byte"),
            OBS.observe(
                "CWR-012.a2",
                "CWR-012.a3",
                match="protocol-invalid messages refused|no write in any refused message|after each refusal",
            ),
            OBS.observe("CWR-012.a4", match="component 4|no context carries|tail line"),
        ):
            ids = main_run(kern, ctl, contracts)
        with OBS.observe("CWR-012.a4", match="each context renders K"):
            check_tail_sizes(kern, ctl, ids)
        check_notice(kern)
    finally:
        stub.stop()
        if os.environ.get("CWR_KEEP_SCRATCH") != "1":
            shutil.rmtree(scratch, ignore_errors=True)
        else:
            print(f"scratch kept at {scratch}", file=sys.stderr)
    OBS.emit()
    failed = 0
    for c in CLAUSES:
        if R.bad[c] or not R.ok[c]:
            failed += 1
            print(f"CLAUSE {c} FAIL " + " | ".join(R.bad[c] or ["no evidence"]))
        else:
            print(f"CLAUSE {c} PASS " + " | ".join(R.ok[c]))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

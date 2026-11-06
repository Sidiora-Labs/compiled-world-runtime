#!/usr/bin/env python3
# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Task 4.1 driver: reference world modules, stub servers and cassettes.

Case CWR-014 (and the cassette half of CWR-009). Exercises clauses 7.5, 11.1 and 11.5 against the
real implementation: it builds the `cwr` binary, compiles examples/refworld exactly as shipped with
the real `cwr build` (every stage, contract tests included), starts the refworld stub servers of
benchmark/stubs, drives `cwr instance create` and `cwr instance turn --gateway live|record|replay`
as subprocesses (through benchmark/stubs/record_refworld.py, which only sequences those commands),
and judges what is observable from outside the kernel: the build report, contracts.json, tests.json,
manual.md, result envelopes, ledgers, cassette bytes, the stubs' own request logs and a decoy
listener that counts every connection made to the stub URLs bound in replay mode.

Independent oracles: the primitive catalogue of spec/cwr/reference-world.md section 2 parsed from
the Markdown tables, the limits of decision P13 parsed from spec/cwr/decisions.md, the canonical
request hash and set digest of benchmark/cassettes/FORMAT.md recomputed here, the declared failure
sets of contracts.json, the shipped adapters/*/live.py STATUS_CODES tables, and the stub logs.

    CLAUSE 7.5   live, record and replay modes; replay keyed by adapter, request hash and sequence;
                 a miss is UNAVAILABLE and nothing is contacted.
    CLAUSE 11.1  refworld ships fs, kv, jobs, search, publish and world with the catalogue's
                 primitives, rules and limits; search and publish are stub-backed adapters with
                 recorded cassettes.
    CLAUSE 11.5  the stubs implement rate limiting, duplicate keys and injected unavailability, emit
                 only declared codes, and every declared adapter failure is reached in record and in
                 replay mode.

Prints one line per clause, `CLAUSE <req.ac> PASS|FAIL <evidence>`, and exits 0 only when every
clause passes. Honours CARGO_TARGET_DIR (default: the workspace target dir). Python 3.12, standard
library only; safe under `python3 -I`.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parents[3]
REFWORLD = REPO / "examples/refworld"
SPEC_EXAMPLE = REPO / "spec/cwr/examples/refworld"
STUBS = REPO / "benchmark/stubs"
CASSETTES = REPO / "benchmark/cassettes"
SUITE_KVX = REPO / "benchmark/suite.kvx"
REFERENCE_WORLD_MD = REPO / "spec/cwr/reference-world.md"
DECISIONS_MD = REPO / "spec/cwr/decisions.md"
MODULES = ["fs", "kv", "jobs", "search", "publish", "world"]
ADAPTER_SETS = ("refworld-adapters", "suite")
CLAUSES = ["7.5", "11.1", "11.5"]


class Results:
    """Evidence per clause; a clause passes when it has evidence and no failure."""

    def __init__(self) -> None:
        self.ok: dict[str, list[str]] = {c: [] for c in CLAUSES}
        self.bad: dict[str, list[str]] = {c: [] for c in CLAUSES}

    def check(self, clause: str, cond: bool, evidence: str) -> bool:
        (self.ok if cond else self.bad)[clause].append(evidence)
        return cond


R = Results()


def canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def request_hash(adapter: str, params: dict) -> str:
    """FORMAT.md section 3, recomputed independently of the gateway and of cassette_tool."""
    body = canonical({"adapter": adapter, "params": params}).encode("utf-8")
    return "sha256:" + hashlib.sha256(body).hexdigest()


def set_digest(root: Path) -> str:
    """FORMAT.md section 5, recomputed independently."""
    paths = sorted(root.rglob("*.jsonl"), key=lambda p: p.relative_to(root).as_posix())
    lines = "".join(
        f"{p.relative_to(root).as_posix()}\t{hashlib.sha256(p.read_bytes()).hexdigest()}\n"
        for p in paths
    )
    return "sha256:" + hashlib.sha256(lines.encode("utf-8")).hexdigest()


def parse_kvx(text: str) -> dict:
    """Sections of a KVX file: one `key = <JSON>` per line under `[section]` headers."""
    sections: dict = {}
    current: dict | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], {})
            continue
        key, _, value = line.partition("=")
        if current is None:
            raise ValueError(f"key outside a section: {raw!r}")
        current[key.strip()] = json.loads(value.strip())
    return sections


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"4_1: cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------------------------------------------
# Build


def target_dir() -> Path:
    env = os.environ.get("CARGO_TARGET_DIR")
    return Path(env) if env else REPO / "target"


def build_cwr() -> Path:
    p = subprocess.run(
        ["cargo", "build", "-q", "-p", "cwr-cli"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=1800,
    )
    if p.returncode != 0:
        raise SystemExit(f"4_1: cargo build -p cwr-cli failed:\n{p.stderr[-3000:]}")
    cwr = target_dir() / "debug" / "cwr"
    if not cwr.exists():
        raise SystemExit(f"4_1: {cwr} missing after cargo build")
    return cwr


def clean_bytecode(root: Path) -> None:
    """Interpreter caches are not World Source; another process importing lib/ may leave them."""
    for cache in sorted(root.rglob("__pycache__")):
        shutil.rmtree(cache, ignore_errors=True)


def compile_refworld(cwr: Path, out: Path) -> tuple[int, dict]:
    clean_bytecode(REFWORLD)
    p = subprocess.run(
        [str(cwr), "build", str(REFWORLD), "--out", str(out), "--json"],
        capture_output=True,
        text=True,
        timeout=1800,
    )
    try:
        report = json.loads(p.stdout)
    except ValueError:
        report = {"raw": p.stdout[-2000:], "stderr": p.stderr[-2000:]}
    return p.returncode, report


# ------------------------------------------------------------------------------------------------
# Catalogue of spec/cwr/reference-world.md section 2


def md_tables(text: str) -> dict[str, list[list[str]]]:
    """Every table under a `### 2.N name` heading of section 2, rows as cell lists."""
    tables: dict[str, list[list[str]]] = {}
    heading = None
    for line in text.splitlines():
        m = re.match(r"^### 2\.\d+ (.+)$", line)
        if m:
            heading = m.group(1).strip()
            continue
        if line.startswith("## "):
            heading = None
        if heading and line.startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(set(c) <= set("-: ") for c in cells):
                continue
            tables.setdefault(heading, []).append(cells)
    return tables


def split_signature(sig: str) -> tuple[str, list[str]]:
    m = re.match(r"^([a-z_]+\.[a-z_]+)\((.*)\)$", sig)
    if not m:
        raise ValueError(f"not a primitive signature: {sig!r}")
    params = [x.strip() for x in m.group(2).split(",") if x.strip()]
    return m.group(1), params


def norm_expr(expr: str) -> str:
    """Whitespace-free expression; `x in ["a", "b"]` spelled as the equivalent disjunction."""

    def disj(m: re.Match) -> str:
        lhs = m.group(1)
        items = re.findall(r'"[^"]*"', m.group(2))
        return " or ".join(f"{lhs} == {i}" for i in items)

    expr = re.sub(r"(\S+\([^)]*\)) in \[([^\]]*)\]", disj, expr)
    return re.sub(r"\s+", "", expr)


def parse_preconditions(cell: str) -> list[tuple[str, str]]:
    if cell.startswith("none"):
        return []
    out = []
    for part in cell.split(";"):
        expr, _, code = part.partition("→")
        out.append((norm_expr(expr.strip()), code.strip()))
    return out


def catalogue() -> dict[str, dict]:
    tables = md_tables(REFERENCE_WORLD_MD.read_text(encoding="utf-8"))
    cat: dict[str, dict] = {}
    for heading, rows in tables.items():
        header, body = rows[0], rows[1:]
        for row in body:
            entry = dict(zip(header, row, strict=True))
            name, params = split_signature(entry["Primitive"])
            item: dict[str, Any] = {"params": params, "heading": heading}
            if "Idempotency" in entry:
                item["kind"] = "adapter"
                item["idempotency"] = entry["Idempotency"]
                item["preconditions"] = parse_preconditions(entry["Preconditions in order"])
                item["failures"] = [c.strip() for c in entry["Declared failures"].split(",")]
                item["timeout_ms"] = int(entry["Timeout"].split()[0])
            else:
                item["kind"] = "primitive"
                item["side_effect"] = entry["Side effect"]
                pre = entry.get("Preconditions in order", entry.get("Preconditions", ""))
                item["preconditions"] = parse_preconditions(pre)
                eff = entry["Effects"]
                item["effects"] = (
                    []
                    if eff == "none"
                    else [norm_expr(e.strip()) for e in eff.split(";")]
                    if heading != "world"
                    else None  # world effects are prose (rng stream, ledger record)
                )
            cat[name] = item
    return cat


def p13_limits() -> dict[str, int]:
    text = DECISIONS_MD.read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if ln.startswith("| P13 "))
    return {k: int(v) for k, v in re.findall(r"([a-z_]+) (\d+)", line.split("|")[2])}


def p07_clock() -> dict[str, int]:
    text = DECISIONS_MD.read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if ln.startswith("| P07 "))
    return {k: int(v) for k, v in re.findall(r"(tick_per_[a-z]+) (\d+)", line)}


# ------------------------------------------------------------------------------------------------
# Network observation


class Decoy:
    """A TCP listener that counts every connection; bound where a stub would be in replay mode."""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(64)
        self.port = self.sock.getsockname()[1]
        self.count = 0
        self.stop = False
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def loop(self) -> None:
        self.sock.settimeout(0.2)
        while not self.stop:
            try:
                conn, _ = self.sock.accept()
            except (TimeoutError, OSError):
                continue
            self.count += 1
            conn.close()

    def close(self) -> None:
        self.stop = True
        self.thread.join(2)
        self.sock.close()


def http(url: str, method: str = "GET", body: bytes | None = None, headers: dict | None = None):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# ------------------------------------------------------------------------------------------------
# Envelope helpers


def reached(turns: list[dict]) -> dict[str, set[str]]:
    """primitive -> set of outcomes ("ok" or a code) over every trace entry of every turn."""
    out: dict[str, set[str]] = {}
    for t in turns:
        for entry in t["envelope"].get("trace", []):
            code = "ok" if entry.get("status") == "ok" else entry.get("code")
            out.setdefault(entry["primitive"], set()).add(str(code))
    return out


def service_records(ledger: list[dict]) -> list[dict]:
    return [r for r in ledger if (r.get("gateway") or {}).get("via") == "service"]


# ------------------------------------------------------------------------------------------------
# 11.1: the compiled world against the catalogue


def check_world(build: Path, report: dict, rc: int) -> dict:
    stages = {s["stage"]: s["status"] for s in report.get("stages", [])}
    R.check(
        "11.1",
        rc == 0 and stages and all(v == "passed" for v in stages.values()),
        f"cwr build examples/refworld exit {rc}, stages {stages}",
    )
    contracts = json.loads((build / "contracts.json").read_text())
    tests = json.loads((build / "tests.json").read_text())
    failed = [f"{c['target']}#{c['case']}" for c in tests["cases"] if not c["passed"]]
    R.check(
        "11.1",
        tests.get("passed") is True and tests.get("deterministic") is True and not failed,
        f"contract tests: {len(tests['cases'])} cases over {tests.get('primitives')} primitives and "
        f"{tests.get('adapters')} adapters, passed={tests.get('passed')}, "
        f"deterministic={tests.get('deterministic')}, failing={failed[:5]}",
    )
    R.check(
        "11.1",
        contracts["world"]["modules"] == MODULES and contracts["world"]["name"] == "refworld",
        f"world {contracts['world']['name']} {contracts['world']['version']} modules "
        f"{contracts['world']['modules']}",
    )

    cat = catalogue()
    compiled = {p["name"]: p for p in contracts["primitives"] + contracts["adapters"]}
    R.check(
        "11.1",
        set(cat) == set(compiled),
        f"catalogue of reference-world.md section 2 has {len(cat)} entries, the Build {len(compiled)}; "
        f"missing {sorted(set(cat) - set(compiled))}, extra {sorted(set(compiled) - set(cat))}",
    )
    mismatches = []
    for name, want in sorted(cat.items()):
        got = compiled.get(name)
        if got is None:
            continue
        if [p[0] for p in got["params"]] != want["params"]:
            mismatches.append(f"{name} params {got['params']}")
        got_pre = [(norm_expr(e), c) for e, c in got["preconditions"]]
        if got_pre != want["preconditions"]:
            mismatches.append(f"{name} preconditions {got['preconditions']}")
        if want["kind"] == "adapter":
            if got.get("kind") != "adapter" or got["side_effect"] != "external":
                mismatches.append(f"{name} is not an external adapter")
            if got.get("content_guaranteed") is not False:
                mismatches.append(f"{name} content_guaranteed {got.get('content_guaranteed')}")
            if got["idempotency"] != want["idempotency"]:
                mismatches.append(f"{name} idempotency {got['idempotency']}")
            if got["failures"] != want["failures"]:
                mismatches.append(f"{name} failures {got['failures']}")
            if got["timeout_ms"] != want["timeout_ms"]:
                mismatches.append(f"{name} timeout_ms {got['timeout_ms']}")
            if not str(got.get("endpoint", "")).startswith(f"stub://{name.split('.')[0]}/"):
                mismatches.append(f"{name} endpoint {got.get('endpoint')}")
        else:
            if got["side_effect"] != want["side_effect"]:
                mismatches.append(f"{name} side_effect {got['side_effect']}")
            if (
                want["effects"] is not None
                and [norm_expr(e) for e in got["effects"]] != want["effects"]
            ):
                mismatches.append(f"{name} effects {got['effects']}")
    R.check(
        "11.1",
        not mismatches,
        "every primitive and adapter matches the catalogue entry for entry (parameters, side effect, "
        "ordered preconditions with codes, effects, idempotency, declared failures, timeout, "
        f"stub:// endpoint, content not guaranteed); mismatches {mismatches[:6]}",
    )
    want_limits, want_clock = p13_limits(), p07_clock()
    R.check(
        "11.1",
        contracts["limits"] == want_limits
        and contracts["clock"] == want_clock
        and contracts["rng"] == {"seed_visible": True},
        f"limits {contracts['limits']} equal P13 {want_limits}; clock {contracts['clock']} equals "
        f"P07 {want_clock}; rng {contracts['rng']} (world.seed present)",
    )

    # Implementation: one lib/ function per internal primitive, one live.py function per adapter.
    missing_impl = []
    for name, p in compiled.items():
        module, prim = name.split(".")
        fn = prim + "_" if prim in ("del",) else prim
        path = (
            build / "adapters" / module / "live.py"
            if p.get("kind") == "adapter"
            else build / "lib" / f"{module}.py"
        )
        tree = ast.parse(path.read_text())
        defined = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
        if fn not in defined:
            missing_impl.append(name)
    manual = (build / "manual.md").read_text()
    not_in_manual = [n for n in compiled if f"{n}(" not in manual]
    R.check(
        "11.1",
        not missing_impl and not not_in_manual,
        f"every primitive has its implementation in the Build's lib/ or adapters/*/live.py "
        f"(missing {missing_impl}) and an entry in manual.md (missing {not_in_manual})",
    )

    # The spec pack's illustrative files mirror the real world rule for rule.
    mirror_bad = []
    for rel in ("world.kvx", "contracts/fs.kvx", "adapters/search.kvx", "adapters/publish.kvx"):
        spec_k = parse_kvx((SPEC_EXAMPLE / rel).read_text())
        real_k = parse_kvx((REFWORLD / rel).read_text())
        spec_k.get("meta", {}).pop("example", None)
        if spec_k != real_k:
            mirror_bad.append(rel)
    mig = "migrations/0_2_0_kv_namespaces.py"

    def code_only(path: Path) -> str:
        tree = ast.parse(path.read_text())
        body = tree.body[1:] if tree.body and isinstance(tree.body[0], ast.Expr) else tree.body
        return ast.dump(ast.Module(body=body, type_ignores=[]))

    if code_only(SPEC_EXAMPLE / mig) != code_only(REFWORLD / mig):
        mirror_bad.append(mig)
    R.check(
        "11.1",
        not mirror_bad,
        "spec/cwr/examples/refworld world.kvx, contracts/fs.kvx, adapters/search.kvx, "
        f"adapters/publish.kvx and the 0.2.0 migration equal the real world apart from comments and "
        f"meta.example; differing {mirror_bad}",
    )
    return contracts


def check_contract_failures(build: Path, contracts: dict) -> None:
    """do_4: every declared precondition failure of every primitive is reached by a contract test
    at its declared index, every primitive has an ok case, and every adapter failure is reached."""
    tests = json.loads((build / "tests.json").read_text())
    by_target: dict[str, set] = {}
    for c in tests["cases"]:
        if c["passed"]:
            by_target.setdefault(c["target"], set()).add((c["got"], c.get("got_precondition")))
    missing = []
    for p in contracts["primitives"] + contracts["adapters"]:
        got = by_target.get(p["name"], set())
        want = {("ok", None)} | {(code, i + 1) for i, (_, code) in enumerate(p["preconditions"])}
        for code in p.get("failures", []):
            if not any(g[0] == code for g in got):
                missing.append(f"{p['name']}:{code}")
        missing += [f"{p['name']}:{w[0]}@{w[1]}" for w in sorted(want, key=str) if w not in got]
    n_pre = sum(len(p["preconditions"]) for p in contracts["primitives"] + contracts["adapters"])
    n_fail = sum(len(p.get("failures", [])) for p in contracts["adapters"])
    R.check(
        "11.1",
        not missing,
        f"contract tests reach all {n_pre} declared precondition failures at their declared index, "
        f"all {n_fail} declared adapter failures and an ok outcome for each of the "
        f"{len(contracts['primitives']) + len(contracts['adapters'])} entries; missing {missing[:8]}",
    )


# ------------------------------------------------------------------------------------------------
# 11.5: stub servers


def check_stubs_static(contracts: dict, work: Path, rr) -> None:
    sel = subprocess.run(
        [sys.executable, "-I", str(STUBS / "selftest.py")],
        capture_output=True,
        text=True,
        timeout=600,
    )
    tail = sel.stdout.strip().splitlines()[-1] if sel.stdout.strip() else sel.stderr[-300:]
    R.check("11.5", sel.returncode == 0, f"stub self-test exit {sel.returncode}: {tail}")

    declared = {a["name"]: a for a in contracts["adapters"]}
    live = {
        m: load_module(REFWORLD / "adapters" / m / "live.py", f"cwr41_live_{m}")
        for m in ("search", "publish")
    }
    status_codes = {k: v for m in live.values() for k, v in m.STATUS_CODES.items()}
    pair = rr.StubPair(9, work)
    try:
        health = {n: rr.json.loads(http(u + "/_stub/health")[1]) for n, u in pair.urls.items()}
        eps = {e["adapter"]: e for h in health.values() for e in h["endpoints"]}
        agree = all(
            name in eps
            and sorted(eps[name]["declared"]) == sorted(a["failures"])
            and eps[name]["timeout_ms"] == a["timeout_ms"]
            and eps[name]["path"] == "/" + a["endpoint"].split("/", 3)[3]
            for name, a in declared.items()
        )
        R.check(
            "11.5",
            agree and set(eps) == set(declared),
            f"stub /_stub/health endpoints {sorted(eps)} carry the compiled declared failures, "
            f"timeouts and paths of every adapter contract",
        )
        # A schedule fault whose gateway code is undeclared is refused at load time.
        refused, accepted = [], []
        for adapter, fault in [
            ("search.query", "REJECTED"),
            ("publish.post", "REJECTED"),
            ("publish.status", "REJECTED"),
            ("publish.delete", "REJECTED"),
            ("publish.status", "MALFORMED"),
            ("publish.delete", "MALFORMED"),
        ]:
            doc = {
                "format": "cwr.stub.schedule",
                "schema_version": 1,
                "allow_undeclared": False,
                "faults": [{"adapter": adapter, "index": 0, "fault": fault}],
            }
            server = adapter.split(".")[0]
            st, _ = http(
                pair.urls[server] + "/_stub/schedule",
                "PUT",
                canonical(doc).encode(),
                {"Content-Type": "application/json"},
            )
            (refused if st == 400 else accepted).append(f"{adapter}:{fault}->{st}")
        R.check(
            "11.5",
            not accepted,
            f"schedules injecting a fault whose code the adapter does not declare are refused: "
            f"{refused}; accepted {accepted}",
        )
        # Malformed adapter requests: the stub's answer still maps to a declared code.
        undeclared = []
        probes = 0
        for name, a in declared.items():
            server = name.split(".")[0]
            path = "/" + a["endpoint"].split("/", 3)[3]
            good_headers = {"X-CWR-Instance": "probe-41", "X-CWR-World-Time": "0"}
            for body, headers in [
                (b"{}", {}),
                (b"not json", good_headers),
                (b"[1]", good_headers),
                (b'{"unexpected":1}', good_headers),
                (canonical({p[0]: 1 for p in a["params"]}).encode(), good_headers),
            ]:
                probes += 1
                st, _ = http(pair.urls[server] + path, "POST", body, headers)
                code = status_codes[name].get(st)
                if code is None or (code != "ok" and code not in a["failures"]):
                    undeclared.append(f"{name}:{st}")
        R.check(
            "11.5",
            not undeclared,
            f"{probes} malformed requests (missing headers, non-JSON, non-object, unknown keys, "
            f"wrong types) to the four endpoints were answered only with statuses that live.py maps "
            f"to a declared code; undeclared {undeclared}",
        )
    finally:
        pair.stop()


def emitted_code(entry: dict, status_codes: dict) -> str | None:
    fault = entry.get("fault")
    if fault == "TIMEOUT":
        return "TIMEOUT"
    if fault == "MALFORMED":
        return "INVALID"
    if entry.get("status") is None:
        return "UNAVAILABLE"
    return status_codes.get(entry["adapter"], {}).get(entry["status"])


def check_window(entries: list[dict], cassette: list[dict], adapter: str, cap: int, span: int):
    """The stub's own sliding window: at every unscheduled 429, exactly `cap` posts or queries were
    accepted in (t - span, t] and retry_after equals the ticks until the oldest leaves it."""
    findings, bad = [], []
    accepted: list[int] = []
    for e, c in zip(entries, cassette, strict=True):
        if e["adapter"] != adapter:
            continue
        t = e["world_time"]
        if e["status"] == 429 and e.get("fault") is None:
            window = [a for a in accepted if t - span < a <= t]
            body = json.loads(c["response"]["body"])
            want = min(window) + span - t if window else None
            findings.append((t, len(window), body.get("retry_after")))
            if len(window) != cap or body.get("retry_after") != want:
                bad.append((t, len(window), body.get("retry_after"), want))
        elif e["status"] == 200 and e.get("fault") in (None, "TIMEOUT"):
            accepted.append(t)
    return findings, bad


def check_scenarios(rec_dir: Path, logs_dir: Path, rep_dir: Path, contracts: dict) -> None:
    """Fresh record of the adapters set with stub logs; its offline replay; the stub rules."""
    declared = {a["name"]: a for a in contracts["adapters"]}
    live = {
        m: load_module(REFWORLD / "adapters" / m / "live.py", f"cwr41_live2_{m}")
        for m in ("search", "publish")
    }
    status_codes = {k: v for m in live.values() for k, v in m.STATUS_CODES.items()}
    root = rec_dir / "refworld-adapters"
    rec_reach: dict[str, set] = {}
    rep_reach: dict[str, set] = {}
    seen_vs_cassette, emitted_bad, log_codes = [], [], {}
    windows: dict[str, list] = {}
    window_bad = []
    dup = ident = sched_unavail = False
    for expect_path in sorted(root.rglob("*.expect.json")):
        expect = json.loads(expect_path.read_text())
        rel = expect_path.relative_to(rec_dir)
        for k, v in reached(expect["turns"]).items():
            rec_reach.setdefault(k, set()).update(v)
        replay_path = rep_dir / rel.parent / expect_path.name.replace(".expect.", ".replay.")
        if replay_path.exists():
            for k, v in reached(json.loads(replay_path.read_text())["turns"]).items():
                rep_reach.setdefault(k, set()).update(v)
        cassette = [
            json.loads(x)
            for x in expect_path.with_name(expect_path.name.replace(".expect.json", ".jsonl"))
            .read_text()
            .splitlines()[1:]
        ]
        name = expect["label"].replace("/", "-")
        logs = json.loads((logs_dir / f"{name}.json").read_text())
        # One Instance per scenario and a sequential gateway: every service request has its own
        # world tick, so the two stubs' logs merge into call order by world_time.
        entries = sorted(
            (e for v in logs.values() for e in v), key=lambda e: (e["world_time"], e["n"])
        )
        ledger_service = service_records(expect["ledger"])
        bodies = [hashlib.sha256(canonical(c["params"]).encode()).hexdigest() for c in cassette]
        same = (
            len(entries) == len(cassette) == len(ledger_service)
            and [e["adapter"] for e in entries] == [c["adapter"] for c in cassette]
            and [e["request_sha256"] for e in entries] == bodies
        )
        seen_vs_cassette.append(
            f"{expect['label']}: stubs saw {len(entries)}, cassette {len(cassette)}, "
            f"ledger service calls {len(ledger_service)}{'' if same else ' MISMATCH'}"
        )
        if not same:
            R.check("7.5", False, seen_vs_cassette[-1])
        for e in entries:
            code = emitted_code(e, status_codes)
            log_codes.setdefault(e["adapter"], set()).add(str(code))
            if code is None or (code != "ok" and code not in declared[e["adapter"]]["failures"]):
                emitted_bad.append(f"{e['adapter']}:{e['status']}:{e.get('fault')}")
            if e.get("fault") == "UNAVAILABLE" and e["status"] == 503:
                sched_unavail = True
        if same:
            for adapter, cap, span in (("publish.post", 3, 100), ("search.query", 10, 50)):
                found, bad = check_window(entries, cassette, adapter, cap, span)
                windows.setdefault(adapter, []).extend(found)
                window_bad += bad
        returned = [t["envelope"].get("returned") or {} for t in expect["turns"]]
        for r in returned:
            if r.get("duplicate_key", {}).get("code") == "DUPLICATE_KEY":
                dup = True
            if "identical_replay" in r and r["identical_replay"].get("ok") == r["ok"].get("ok"):
                ident = True
    full = {n: {"ok"} | set(a["failures"]) for n, a in declared.items()}
    rec_gap = {n: sorted(full[n] - rec_reach.get(n, set())) for n in full}
    rep_gap = {n: sorted(full[n] - rep_reach.get(n, set())) for n in full}
    R.check(
        "11.5",
        not any(rec_gap.values()),
        "record mode through the real gateway against the stubs reached every declared failure and "
        f"ok of every adapter: {{{', '.join(f'{n}: {sorted(rec_reach.get(n, set()))}' for n in sorted(full))}}}; "
        f"gaps {rec_gap}",
    )
    R.check(
        "11.5",
        not any(rep_gap.values()) and rep_reach == rec_reach,
        f"replay mode reached the same outcome sets offline; gaps {rep_gap}",
    )
    R.check(
        "11.5",
        not emitted_bad,
        "every response the stubs logged during recording maps to a declared code of its adapter "
        f"(by live.py STATUS_CODES, reset to UNAVAILABLE, stall to TIMEOUT, malformed to INVALID): "
        f"{ {k: sorted(v) for k, v in sorted(log_codes.items())} }; undeclared {emitted_bad}",
    )
    R.check(
        "11.5",
        bool(windows.get("publish.post")) and bool(windows.get("search.query")) and not window_bad,
        "rate limiting held by the stubs in world ticks: unscheduled 429s at (tick, accepted in "
        f"window, retry_after) publish {windows.get('publish.post')} (3 per 100 ticks), search "
        f"{windows.get('search.query')} (10 per 50 ticks), each retry_after equal to the ticks until "
        f"the oldest accepted call leaves the window; violations {window_bad}",
    )
    R.check(
        "11.5",
        dup and ident and sched_unavail,
        f"duplicate keys: DUPLICATE_KEY for a reused key with another payload={dup}, identical "
        f"replay returns the original receipt={ident}; injected unavailability: scheduled 503 "
        f"served={sched_unavail}",
    )
    R.check(
        "7.5",
        all("MISMATCH" not in s for s in seen_vs_cassette),
        "record mode wrote exactly what the stubs saw, in order, once each, matching the ledger's "
        "service calls (no retries): " + "; ".join(seen_vs_cassette),
    )


# ------------------------------------------------------------------------------------------------
# 7.5 and freezing: frozen cassette sets, offline replay, misses, live mode


def check_frozen(frozen_digests: dict) -> list[Path]:
    manifest_path = CASSETTES / "refworld.kvx"
    if not R.check("7.5", manifest_path.exists(), f"{manifest_path.relative_to(REPO)} present"):
        return []
    manifest = parse_kvx(manifest_path.read_text())
    sets = {k.split(".", 1)[1]: v for k, v in manifest.items() if k.startswith("set.")}
    suite = parse_kvx(SUITE_KVX.read_text()).get("suite", {})
    paths: list[Path] = []
    for name in ADAPTER_SETS:
        d = CASSETTES / name
        got = set_digest(d) if d.exists() else None
        frozen_digests[name] = got
        n = len(list(d.rglob("*.jsonl"))) if d.exists() else 0
        R.check(
            "7.5",
            got is not None and sets.get(name, {}).get("digest") == got and n > 0,
            f"benchmark/cassettes/{name}: {n} cassettes, digest {got} equals the frozen "
            f"{sets.get(name, {}).get('digest')}",
        )
        paths += sorted(d.rglob("*.jsonl")) if d.exists() else []
    R.check(
        "7.5",
        suite.get("cassette_digest") == frozen_digests.get("suite"),
        f"benchmark/suite.kvx cassette_digest {suite.get('cassette_digest')} equals the suite set "
        f"digest",
    )
    incomplete = sets.get("suite", {}).get("runs_incomplete", [])
    R.check(
        "7.5",
        not sets.get("refworld-adapters", {}).get("runs_incomplete"),
        f"every adapters scenario recorded to completion; suite runs whose reference solution "
        f"stopped early (task 4.2 material, replayed as recorded): {len(incomplete)}",
    )
    # Keyed by adapter, canonical request hash and sequence (FORMAT.md), recomputed here.
    bad = []
    entries = 0
    for p in paths:
        lines = p.read_text(encoding="utf-8").splitlines()
        header = json.loads(lines[0])
        if header.get("format") != "cwr.cassette" or header.get("schema_version") != 1:
            bad.append(f"{p.name}: header")
        seqs: dict[tuple, int] = {}
        for line in lines[1:]:
            e = json.loads(line)
            entries += 1
            if canonical(e) != line:
                bad.append(f"{p.name}: not canonical")
            if e["request_hash"] != request_hash(e["adapter"], e["params"]):
                bad.append(f"{p.name}: hash")
            key = (e["adapter"], e["request_hash"])
            if e["seq"] != seqs.get(key, 0):
                bad.append(f"{p.name}: seq")
            seqs[key] = e["seq"] + 1
            if any(k in line for k in ("host_ts", "duration_us")):
                bad.append(f"{p.name}: host field")
    tool = subprocess.run(
        [sys.executable, "-I", str(STUBS / "cassette_tool.py"), "verify", *map(str, paths)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    R.check(
        "7.5",
        paths and not bad and tool.returncode == 0,
        f"{len(paths)} frozen cassettes, {entries} entries: every entry keyed by adapter, the "
        f"canonical request hash recomputed here and a contiguous per-(adapter, hash) sequence, "
        f"canonical JSON, no host fields; cassette_tool verify exit {tool.returncode}; "
        f"problems {bad[:5]}",
    )
    return paths


def replay_frozen(rr_path: Path, cwr: Path, build: Path, out: Path) -> None:
    decoy = Decoy()
    try:
        p = subprocess.run(
            [
                sys.executable,
                "-I",
                str(rr_path),
                "replay",
                "--set",
                "all",
                "--cwr",
                str(cwr),
                "--build",
                str(build),
                "--cassettes",
                str(CASSETTES),
                "--out",
                str(out),
                "--offline-url",
                decoy.url,
                "--jobs",
                str(min(16, os.cpu_count() or 4)),
            ],
            capture_output=True,
            text=True,
            timeout=7200,
        )
    finally:
        decoy.close()
    line = (p.stdout.strip().splitlines() or [p.stderr[-500:]])[-1]
    R.check(
        "7.5",
        p.returncode == 0 and decoy.count == 0,
        f"replay of every frozen cassette (adapters and suite sets) through `cwr instance turn "
        f"--gateway replay` with both stubs bound to a decoy listener: exit {p.returncode}, "
        f"'{line}', envelopes and ledgers equal record mode except host fields, decoy connections "
        f"{decoy.count}",
    )


def check_miss_and_live(rr, cwr: Path, build: Path, work: Path, frozen_content: Path) -> None:
    decoy = Decoy()
    try:
        cassette = work / "miss" / "cassette.jsonl"
        cassette.parent.mkdir(parents=True)
        shutil.copyfile(frozen_content, cassette)
        home = work / "miss" / "home"
        home.mkdir()
        urls = {"search": decoy.url, "publish": decoy.url}
        run = rr.Run(cwr, build, home, 1, None, "replay", cassette, "miss", urls)
        env = run.turn(
            "import cwr, search\n"
            "out = []\n"
            "for q in ['tennis', 'tennis', 'golf']:\n"
            "    try:\n"
            "        out.append(len(search.query(q=q, limit=5)))\n"
            "    except cwr.errors.WorldError as e:\n"
            "        out.append(e.code)\n"
            "return out\n"
        )
        before = cassette.read_bytes() == frozen_content.read_bytes()
    finally:
        decoy.close()
    R.check(
        "7.5",
        env.get("returned") == [5, "UNAVAILABLE", "UNAVAILABLE"] and decoy.count == 0 and before,
        f"replay of {frozen_content.relative_to(REPO)}: first search.query('tennis', 5) served "
        f"from seq 0, the same request again (seq 1, not recorded) and an unrecorded query both "
        f"UNAVAILABLE; returned {env.get('returned')}; decoy connections {decoy.count}; cassette "
        f"unchanged {before}",
    )

    # live: calls the stub, writes no cassette, same outcome as record mode for equal seed.
    pair = rr.StubPair(1, work)
    try:
        home = work / "live" / "home"
        home.mkdir(parents=True)
        p = subprocess.run(
            [str(cwr), "instance", "create", "--home", str(home), str(build), "--seed", "1"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        iid = p.stdout.strip()
        script = work / "live" / "turn.py"
        script.write_text("import search\nreturn search.query(q='tennis', limit=5)\n")
        files_before = sorted(x.relative_to(work) for x in work.rglob("*.jsonl"))
        cmd = [str(cwr), "instance", "turn", iid, "--home", str(home), "--script", str(script)]
        cmd += ["--gateway", "live"]
        for name, url in sorted(pair.urls.items()):
            cmd += ["--stub", f"{name}={url}"]
        t = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        env = json.loads(t.stdout.strip().splitlines()[-1]) if t.returncode == 0 else {}
        log = pair.log()
        files_after = sorted(x.relative_to(work) for x in work.rglob("*.jsonl"))
        new = [str(x) for x in files_after if x not in files_before and "ledger" not in str(x)]
    finally:
        pair.stop()
    recorded = json.loads(frozen_content.with_name("seed-1.expect.json").read_text())
    want = recorded["turns"][0]["envelope"]["returned"]["hits"]["ok"]
    R.check(
        "7.5",
        t.returncode == 0
        and env.get("status") == "ok"
        and env.get("returned") == want
        and len(log["search"]) == 1
        and not new,
        f"live mode: exit {t.returncode}, status {env.get('status')}, the search stub logged "
        f"{len(log['search'])} request, no cassette written (new jsonl files {new}), and the hits "
        f"equal the frozen record-mode answer for the same seed and request "
        f"{env.get('returned') == want}",
    )


# ------------------------------------------------------------------------------------------------


def main() -> int:
    cwr = build_cwr()
    rr_path = STUBS / "record_refworld.py"
    rr = load_module(rr_path, "cwr41_record_refworld")
    with tempfile.TemporaryDirectory(prefix="cwr-4_1-") as tmp:
        work = Path(tmp)
        rc, report = compile_refworld(cwr, work / "build")
        build = Path((report.get("build") or {}).get("dir", work / "missing"))
        if rc != 0 or not build.exists():
            R.check(
                "11.1", False, f"cwr build examples/refworld exit {rc}: {canonical(report)[:1500]}"
            )
        else:
            contracts = check_world(build, report, rc)
            check_contract_failures(build, contracts)
            check_stubs_static(contracts, work, rr)

            frozen: dict = {}
            paths = check_frozen(frozen)

            # A fresh record of the adapters set reproduces the frozen bytes.
            rec = work / "rec"
            logs = work / "stub-logs"
            p = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(rr_path),
                    "record",
                    "--set",
                    "adapters",
                    "--cwr",
                    str(cwr),
                    "--build",
                    str(build),
                    "--out",
                    str(rec),
                    "--stub-logs",
                    str(logs),
                ],
                capture_output=True,
                text=True,
                timeout=3600,
            )
            fresh = set_digest(rec / "refworld-adapters") if p.returncode == 0 else None
            R.check(
                "7.5",
                p.returncode == 0 and fresh == frozen.get("refworld-adapters"),
                f"record mode re-run against freshly started stubs: exit {p.returncode}, digest "
                f"{fresh} equals the frozen adapters set (byte-identical re-recording)",
            )
            rep = work / "rep"
            if p.returncode == 0:
                r2 = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        str(rr_path),
                        "replay",
                        "--set",
                        "adapters",
                        "--cwr",
                        str(cwr),
                        "--build",
                        str(build),
                        "--cassettes",
                        str(rec),
                        "--out",
                        str(rep),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=3600,
                )
                R.check(
                    "11.5",
                    r2.returncode == 0,
                    f"fresh adapters set replays offline: {r2.stdout.strip()[-200:]}",
                )
                check_scenarios(rec, logs, rep, contracts)
            else:
                R.check("11.5", False, f"record failed: {p.stderr[-800:]}")

            if paths:
                replay_frozen(rr_path, cwr, build, work / "replay-frozen")
                content = CASSETTES / "refworld-adapters/content/seed-1.jsonl"
                check_miss_and_live(rr, cwr, build, work, content)

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

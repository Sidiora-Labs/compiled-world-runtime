#!/usr/bin/env python3
# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Task 0.3 driver: contract grammar, failure envelope and World Source schema crate.

Case CWR-001. Exercises clauses 2.3, 3.1, 3.2, 3.3, 3.4 and 3.6 against the real
implementation: it builds the `cwr` binary and runs the `cwr-contract` test suite with
cargo, then drives `cwr contract ...` as a subprocess over

* the valid World Source fixture (crates/cwr-contract/tests/fixtures/valid) and the
  pack's illustrative source (spec/cwr/examples/refworld): both must be accepted, their
  typed JSON dump must agree with an independent stdlib parse of every KVX file, and the
  model must round-trip byte-identically through JSON (`dump --from-json`) and through
  canonical KVX (`emit`, then `dump` again);
* every malformed class under crates/cwr-contract/tests/fixtures/malformed, materialised
  as valid tree + overlay in a scratch directory: each must be rejected with exactly the
  located errors (file, line, column, kind, lint code) recorded in its case.json;
* defects generated here on the fly: each of the eight no-default world keys removed in
  turn, a symbolic link, every code outside the envelope tried in a precondition, and
  failure envelopes fed to `cwr contract envelope`.

Prints one line per clause, `CLAUSE <req.ac> PASS|FAIL <evidence>`, and exits 0 only when
every clause passes. Honours CARGO_TARGET_DIR; defaults to the workspace target dir.
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

REPO = Path(__file__).resolve().parents[3]
CRATE = REPO / "crates/cwr-contract"
FIXTURES = CRATE / "tests/fixtures"
VALID = FIXTURES / "valid"
MALFORMED = FIXTURES / "malformed"
PACK_EXAMPLE = REPO / "spec/cwr/examples/refworld"
SPEC = REPO / "spec/cwr/spec.kvx"
CLAUSES = ["2.3", "3.1", "3.2", "3.3", "3.4", "3.6"]
SPEC_ENVELOPE = [
    "NOT_FOUND",
    "LOCKED",
    "CONFLICT",
    "INVALID",
    "LIMIT",
    "UNAVAILABLE",
    "RATE_LIMITED",
    "TIMEOUT",
    "REJECTED",
    "DUPLICATE_KEY",
    "INVARIANT_VIOLATION",
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


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT, **kw)


def target_dir() -> Path:
    env = os.environ.get("CARGO_TARGET_DIR")
    return Path(env) if env else REPO / "target"


def build() -> tuple[Path, str]:
    env = dict(os.environ)
    env.setdefault("CARGO_TARGET_DIR", str(target_dir()))
    b = run(["cargo", "build", "-p", "cwr-cli", "-p", "cwr-contract"], cwd=REPO, env=env)
    if b.returncode != 0:
        print(b.stderr[-4000:], file=sys.stderr)
        raise SystemExit("cargo build failed")
    t = run(["cargo", "test", "-p", "cwr-contract"], cwd=REPO, env=env)
    return (
        target_dir() / "debug" / "cwr",
        t.stdout + t.stderr if t.returncode == 0 else "FAILED\n" + t.stdout + t.stderr,
    )


# ---------------------------------------------------------------- independent KVX parse


def parse_kvx(text: str) -> dict:
    sections: dict = {}
    current = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.fullmatch(r"\[([A-Za-z0-9_.-]+)\]", line)
        if m:
            current = sections.setdefault(m.group(1), {})
            continue
        key, value = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_-]*)\s*=\s*(.+)", line).groups()
        current[key] = json.loads(value)
    return sections


def compare_with_source(root: Path, dump: dict) -> list[str]:
    """Every value in the KVX files must appear unchanged, in declared order, in the dump."""
    problems: list[str] = []

    def eq(what: str, a, b) -> None:
        if a != b:
            problems.append(f"{what}: source {a!r} != model {b!r}")

    world = parse_kvx((root / "world.kvx").read_text())
    for key in ("name", "version", "language", "runtime", "modules"):
        eq(f"world.{key}", world["world"][key], dump["world"]["world"][key])
    for section in ("limits", "clock", "rng"):
        eq(section, world[section], dump["world"][section])
    eq("meta.example", world["meta"].get("example", False), dump["world"]["example"])
    for kind, prefix, items in (
        ("contracts", "primitive.", "primitives"),
        ("adapters", "adapter.", "adapters"),
    ):
        model_files = {f["path"]: f for f in dump[kind]}
        for path in sorted((root / kind).glob("*.kvx")) if (root / kind).exists() else []:
            rel = f"{kind}/{path.name}"
            doc = parse_kvx(path.read_text())
            if rel not in model_files:
                problems.append(f"{rel} missing from model")
                continue
            declared = [
                (name[len(prefix) :], body) for name, body in doc.items() if name.startswith(prefix)
            ]
            model = model_files[rel][items]
            eq(f"{rel} order", [n for n, _ in declared], [p["name"] for p in model])
            for (name, body), p in zip(declared, model, strict=False):
                for key, value in body.items():
                    if key == "params":
                        eq(f"{name}.params order", list(value.items()), list(p["params"].items()))
                    else:
                        eq(f"{name}.{key}", value, p[key])
    log = parse_kvx((root / "CHANGELOG.kvx").read_text())
    releases = [(k[len("release.") :], v) for k, v in log.items() if k.startswith("release.")]
    eq(
        "release order",
        [v for v, _ in releases],
        [r["version"] for r in dump["changelog"]["releases"]],
    )
    for (_, body), r in zip(releases, dump["changelog"]["releases"], strict=False):
        eq(f"release {r['version']}", body, r)
    changes = [(k[len("change.") :], v) for k, v in log.items() if k.startswith("change.")]
    eq("change order", [c for c, _ in changes], [c["id"] for c in dump["changelog"]["changes"]])
    for (cid, body), c in zip(changes, dump["changelog"]["changes"], strict=False):
        eq(f"change {cid}", dict(body, id=cid), c)
    on_disk = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
    eq("inventory", on_disk, [f["path"] for f in dump["files"]])
    eq(
        "migrations",
        sorted(p for p in on_disk if p.startswith("migrations/")),
        [m["path"] for m in dump["migrations"]],
    )
    return problems


# ---------------------------------------------------------------- helpers over the binary


class Cwr:
    def __init__(self, binary: Path) -> None:
        self.binary = binary

    def __call__(self, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
        return run([str(self.binary), "contract", *args], input=stdin)

    def check_json(self, root: Path) -> tuple[int, object]:
        p = self("check", str(root), "--json")
        try:
            return p.returncode, json.loads(p.stdout)
        except json.JSONDecodeError:
            return p.returncode, {"unparsable": p.stdout + p.stderr}


def materialise(case_dir: Path, scratch: Path) -> Path:
    case = json.loads((case_dir / "case.json").read_text())
    root = scratch / case_dir.name
    shutil.copytree(VALID, root)
    for rel in case["remove"]:
        (root / rel).unlink()
    files = case_dir / "files"
    if files.exists():
        shutil.copytree(files, root, dirs_exist_ok=True)
    return root


def located(errors: list[dict]) -> list[tuple]:
    return sorted((e["file"], e["line"], e["col"], e["kind"], e["code"]) for e in errors)


# ---------------------------------------------------------------- checks


def check_valid_sources(cwr: Cwr, scratch: Path) -> None:
    for label, root in (("valid fixture", VALID), ("pack example", PACK_EXAMPLE)):
        code, summary = cwr.check_json(root)
        R.check(
            "3.1",
            code == 0 and isinstance(summary, dict) and summary.get("ok") is True,
            f"{label} accepted (exit {code})",
        )
        dump_p = cwr("dump", str(root))
        if not R.check("3.1", dump_p.returncode == 0, f"{label} dump exit {dump_p.returncode}"):
            continue
        dump = json.loads(dump_p.stdout)
        problems = compare_with_source(root, dump)
        R.check(
            "3.1",
            not problems,
            f"{label}: typed model equals independent KVX parse"
            + (f" ({problems[:3]})" if problems else ""),
        )
        model_file = scratch / f"{label.replace(' ', '_')}.json"
        model_file.write_text(dump_p.stdout)
        back = cwr("dump", "--from-json", str(model_file))
        R.check(
            "3.1",
            back.returncode == 0 and back.stdout == dump_p.stdout,
            f"{label}: JSON round-trip byte-identical",
        )
        out = scratch / f"{label.replace(' ', '_')}_emitted"
        emit = cwr("emit", str(root), str(out))
        again = cwr("dump", str(out)) if emit.returncode == 0 else emit
        R.check(
            "3.1",
            emit.returncode == 0 and again.returncode == 0 and again.stdout == dump_p.stdout,
            f"{label}: canonical KVX round-trip byte-identical",
        )
        # Clause-specific facts of the accepted model.
        prims = [p for f in dump["contracts"] for p in f["primitives"]]
        adapters = [a for f in dump["adapters"] for a in f["adapters"]]
        R.check(
            "3.2",
            prims
            and all(
                (p["side_effect"] == "read_only" and not p["effects"])
                or (p["side_effect"] == "mutating" and p["effects"])
                for p in prims
            ),
            f"{label}: {len(prims)} primitives typed with side_effect/effects shape",
        )
        R.check(
            "3.3",
            adapters
            and all(
                a["side_effect"] == "external"
                and a["content_guaranteed"] is False
                and "UNAVAILABLE" in a["failures"]
                and a["idempotency"] in ("none", "natural", "key")
                and a["timeout_ms"] > 0
                and a["endpoint"]
                for a in adapters
            ),
            f"{label}: {len(adapters)} adapters typed",
        )
        limits = dump["world"]["limits"]
        R.check(
            "3.4",
            sorted(limits)
            == sorted(["cpu_ms", "wall_ms", "memory_mb", "primitive_calls", "adapter_calls"])
            and all(v > 0 for v in limits.values()),
            f"{label}: limits {limits}",
        )
        shape = [c for c in dump["changelog"]["changes"] if c["kind"] == "state_shape"]
        R.check(
            "3.6",
            shape
            and all(
                c["migration"]
                and any(
                    m["path"] == c["migration"] and c["id"] in m["changes"]
                    for m in dump["migrations"]
                )
                for c in shape
            ),
            f"{label}: {len(shape)} state_shape change(s) linked to migration files",
        )
        codes_used = {pre[1] for p in prims + adapters for pre in p["preconditions"]} | {
            c for a in adapters for c in a["failures"]
        }
        R.check(
            "2.3",
            codes_used <= set(SPEC_ENVELOPE),
            f"{label}: codes in model {sorted(codes_used)} within envelope",
        )


def check_malformed(cwr: Cwr, scratch: Path) -> None:
    dirs = sorted(p for p in MALFORMED.iterdir() if p.is_dir())
    per_clause: dict[str, list[str]] = {c: [] for c in CLAUSES}
    for case_dir in dirs:
        case = json.loads((case_dir / "case.json").read_text())
        root = materialise(case_dir, scratch / "malformed")
        code, out = cwr.check_json(root)
        expected = located(case["errors"])
        got = located(out) if isinstance(out, list) else [("unparsable", str(out))]
        ok = code == 2 and got == expected
        R.check(
            case["clause"],
            ok,
            f"{case_dir.name}: exit {code} "
            + (
                f"{expected[0][0]}:{expected[0][1]}:{expected[0][2]} {expected[0][4] or expected[0][3]}"
                if ok
                else f"expected {expected} got {got}"
            ),
        )
        per_clause[case["clause"]].append(case_dir.name)
        # The human-readable form carries the same file:line:col location.
        text = cwr("check", str(root))
        first = case["errors"][0]
        R.check(
            case["clause"],
            text.returncode == 2
            and f"{first['file']}:{first['line']}:{first['col']}: " in text.stderr,
            f"{case_dir.name}: text error located",
        )
    for clause, names in per_clause.items():
        R.check(clause, len(names) >= 1, f"{len(names)} malformed classes")
    required = [
        "adapter_with_effects",
        "read_only_with_effects",
        "unknown_failure_code",
        "world_missing_memory_mb",
        "state_shape_without_migration",
    ]
    present = {d.name for d in dirs}
    for name in required:
        clause = (
            json.loads((MALFORMED / name / "case.json").read_text())["clause"]
            if name in present
            else "3.1"
        )
        R.check(clause, name in present, f"task do_4 class {name} present")


def check_world_defaults(cwr: Cwr, scratch: Path) -> None:
    """Remove each no-default key in turn: always a located missing-key error, never a default."""
    text = (VALID / "world.kvx").read_text()
    lines = text.split("\n")
    keys = {
        "limits": ["cpu_ms", "wall_ms", "memory_mb", "primitive_calls", "adapter_calls"],
        "clock": ["tick_per_primitive", "tick_per_turn"],
        "rng": ["seed_visible"],
    }
    for section, names in keys.items():
        header = lines.index(f"[{section}]") + 1
        for key in names:
            root = scratch / "defaults" / key
            shutil.copytree(VALID, root)
            (root / "world.kvx").write_text(
                "\n".join(ln for ln in lines if not ln.startswith(f"{key} ="))
            )
            code, out = cwr.check_json(root)
            ok = (
                code == 2
                and isinstance(out, list)
                and located(out) == [("world.kvx", header, 1, "missing_key", None)]
            )
            R.check(
                "3.4",
                ok,
                f"omitted {section}.{key} -> world.kvx:{header}:1 missing_key"
                if ok
                else f"omitted {key}: {out}",
            )


def check_symlink(cwr: Cwr, scratch: Path) -> None:
    root = scratch / "symlink"
    shutil.copytree(VALID, root)
    os.symlink(root / "lib/fs.py", root / "lib/kv2.py")
    code, out = cwr.check_json(root)
    ok = (
        code == 2
        and isinstance(out, list)
        and located(out) == [("lib/kv2.py", 1, 1, "unknown_file", "CWR-C001")]
    )
    R.check("3.1", ok, "symbolic link rejected as unknown file" if ok else f"symlink: {out}")


def check_envelope(cwr: Cwr, scratch: Path, cargo_log: str) -> None:
    # The envelope set equals the spec text of clause 2.3.
    spec_text = SPEC.read_text()
    clause = re.search(r"^\[req\.2\].*?^ac_3 = \"(.*?)\"$", spec_text, re.S | re.M).group(1)
    closed_set = re.search(r"closed set (.*?);", clause).group(1)
    from_spec = re.findall(r"\b[A-Z][A-Z_]{2,}\b", closed_set)
    codes = cwr("codes")
    listed = codes.stdout.split()
    R.check(
        "2.3",
        codes.returncode == 0 and listed == from_spec == SPEC_ENVELOPE,
        "cwr contract codes lists exactly the 11 codes of req.2 ac_3",
    )
    # The Rust enum has exactly eleven fieldless or payload variants and no catch-all.
    src = (CRATE / "src/envelope.rs").read_text()
    body = re.search(r"pub enum Code \{(.*?)\}", src, re.S).group(1)
    variants = re.findall(r"^\s*([A-Z][A-Za-z]+),", body, re.M)
    R.check(
        "2.3",
        len(variants) == 11 and "(" not in body,
        f"enum Code has {len(variants)} fieldless variants",
    )
    # Every code builds a canonical envelope; retry_after appears only on RATE_LIMITED.
    for code in SPEC_ENVELOPE:
        env = {
            "code": code,
            "primitive": "fs.delete",
            "precondition": 1,
            "limit": "memory_mb" if code == "LIMIT" else None,
            "detail": "content",
        }
        if code == "RATE_LIMITED":
            env["retry_after"] = 30
        p = cwr("envelope", json.dumps(env))
        ok = (
            p.returncode == 0
            and json.loads(p.stdout) == env
            and (("retry_after" in p.stdout) == (code == "RATE_LIMITED"))
        )
        R.check(
            "2.3",
            ok,
            f"envelope {code} parsed and re-serialised canonically"
            if ok
            else f"{code}: {p.stdout}{p.stderr}",
        )
    base = {
        "code": "LOCKED",
        "primitive": "fs.delete",
        "precondition": 2,
        "limit": None,
        "detail": None,
    }
    rejects = {
        "unknown code OK": dict(base, code="OK"),
        "lowercase code": dict(base, code="locked"),
        "code with space": dict(base, code="LOCKED "),
        "numeric code": dict(base, code=3),
        "RATE_LIMITED without retry_after": dict(base, code="RATE_LIMITED", precondition=None),
        "RATE_LIMITED retry_after null": dict(base, code="RATE_LIMITED", retry_after=None),
        "RATE_LIMITED retry_after float": dict(base, code="RATE_LIMITED", retry_after=1.5),
        "RATE_LIMITED retry_after negative": dict(base, code="RATE_LIMITED", retry_after=-1),
        "RATE_LIMITED retry_after string": dict(base, code="RATE_LIMITED", retry_after="30"),
        "retry_after on TIMEOUT": dict(base, code="TIMEOUT", retry_after=3),
        "limit on LOCKED": dict(base, limit="cpu_ms"),
        "precondition 0": dict(base, precondition=0),
        "extra key": dict(base, extra=1),
        "missing detail": {k: v for k, v in base.items() if k != "detail"},
    }
    for label, env in rejects.items():
        p = cwr("envelope", json.dumps(env))
        R.check("2.3", p.returncode == 2, f"envelope rejected: {label}")
    # No code outside the envelope parses inside a World Source either.
    pre = '[["fs.exists(path)", "NOT_FOUND"]]'
    for bad in [
        "OK",
        "ERROR",
        "FORBIDDEN",
        "not_found",
        "NotFound",
        "RETRY",
        "INTERNAL",
        "LOCKED_OUT",
    ]:
        root = scratch / "codes" / bad
        shutil.copytree(VALID, root)
        path = root / "contracts/fs.kvx"
        text = path.read_text()
        assert text.count(pre) == 1
        path.write_text(text.replace(pre, f'[["fs.exists(path)", "{bad}"]]'))
        code, out = cwr.check_json(root)
        ok = (
            code == 2
            and isinstance(out, list)
            and len(out) == 1
            and out[0]["code"] == "CWR-F001"
            and out[0]["file"] == "contracts/fs.kvx"
        )
        R.check(
            "2.3",
            ok,
            f"precondition code {bad!r} rejected CWR-F001 at contracts/fs.kvx:{out[0]['line']}:{out[0]['col']}"
            if ok
            else f"{bad}: {out}",
        )
    # Cargo suite, including compile_fail doctests proving no twelfth code and no stray retry_after.
    passed = "FAILED" not in cargo_log.split("\n")[0] and "test result: FAILED" not in cargo_log
    compile_fail = len(
        re.findall(r"envelope\.rs - envelope \(line \d+\) - compile fail \.\.\. ok", cargo_log)
    )
    R.check(
        "2.3",
        passed and compile_fail == 3,
        f"cargo test -p cwr-contract passed with {compile_fail} compile_fail envelope doctests",
    )
    for clause in ("3.1", "3.2", "3.3", "3.4", "3.6"):
        R.check(
            clause,
            passed and "every_malformed_class_is_rejected_with_located_errors ... ok" in cargo_log,
            "cargo test: malformed fixtures rejected in-crate",
        )


def check_expressions(cwr: Cwr) -> None:
    good = cwr("expr", "kv.used(ns) + len(value) <= 65536")
    ok = good.returncode == 0 and json.loads(good.stdout)["calls"] == ["kv.used"]
    R.check("3.2", ok, "precondition grammar accepts kv.used(ns) + len(value) <= 65536")
    for bad in [
        "path.parent",
        "open(path)",
        "x[0] == 1",
        "a - 1 > 0",
        "a == true",
        "a < b < c",
        "fs.read(fs.list(p))",
    ]:
        p = cwr("expr", bad)
        R.check("3.2", p.returncode == 2 and "CWR-V002" in p.stderr, f"expression rejected: {bad}")


def main() -> int:
    binary, cargo_log = build()
    cwr = Cwr(binary)
    with tempfile.TemporaryDirectory(prefix="cwr-0_3-") as tmp:
        scratch = Path(tmp)
        check_valid_sources(cwr, scratch)
        check_malformed(cwr, scratch)
        check_world_defaults(cwr, scratch)
        check_symlink(cwr, scratch)
        check_envelope(cwr, scratch, cargo_log)
        check_expressions(cwr)
    all_pass = True
    for clause in CLAUSES:
        bad, ok = R.bad[clause], R.ok[clause]
        status = "PASS" if ok and not bad else "FAIL"
        all_pass &= status == "PASS"
        detail = f"{len(ok)} checks passed" + (
            f"; {len(bad)} failed: {' | '.join(bad[:5])}" if bad else f"; e.g. {' | '.join(ok[:3])}"
        )
        print(f"CLAUSE {clause} {status} {detail}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())

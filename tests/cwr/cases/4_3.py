#!/usr/bin/env python3
# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Task 4.3 driver: upgrade pairs U1 to U6 on the real compiler and kernel.

Cases CWR-016 and CWR-011 (the 4.3 part). Exercises clauses 5.2, 5.3 and 11.4 against the real
implementation. It builds the `cwr` binary, compiles both sides of every pair under
examples/refworld-upgrades with `cwr build` (side B with `--previous <hash of A>`, each pair in its
own Build root), compares `contract_diff.json` and `cwr diff` with the expectations frozen in
benchmark/upgrades.kvx, then for every pair creates a live Instance on Build A with the task's
initial state, plays the reference first half, upgrades it with `cwr instance upgrade`, reads the
next context and compares its recompile notice with the frozen text, plays the post-upgrade and the
pre-upgrade second halves on separate Instances, and evaluates the pair's success predicate.
Rollback is exercised twice: a Build whose migration refuses the task state, and a Build whose
files were modified after packaging.

Oracles read only what clause 8.5 allows: ledger files, the state printed by `cwr instance state`,
the Build directories the compiler wrote and the model-facing context. Kernel internals are never
used to judge a clause.

Prints one line per clause, `CLAUSE <req.ac> PASS|FAIL <evidence>`, and exits 0 only when every
clause passes. `--freeze` rewrites the observed Build hashes and reference primitive counts into
benchmark/upgrades.kvx (only those keys) after a run in which every clause passed. Honours
CARGO_TARGET_DIR (default: the workspace target dir). Python 3.12, standard library only.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parents[3]
PAIRS_DIR = REPO / "examples" / "refworld-upgrades"
UPGRADES = REPO / "benchmark" / "upgrades.kvx"
PREDICATES = REPO / "benchmark" / "predicates"
PAIRS = ["U1", "U2", "U3", "U4", "U5", "U6"]
CLAUSES = ["5.2", "5.3", "11.4"]
TIMEOUT = 900
REQUIRED_CLASSES = {
    "precondition_tightened",
    "primitive_removed",
    "failure_code_changed",
    "limit_changed",
    "state_shape_changed",
    "documentation_only",
}
SEP_NOTICE = "\n==== cwr context: recompile notice ====\n"
SEP_TAIL = "\n==== cwr context: ledger tail ====\n"
CALL_KINDS = ("primitive", "adapter")

sys.path.insert(0, str(REPO / "core"))
sys.path.insert(0, str(PREDICATES))
import common  # noqa: E402  benchmark/predicates/common.py, the predicate helper module
from cwr_core import kvx  # noqa: E402  importable only after the sys.path insert above


class Results:
    """Evidence per clause; a clause passes when it has evidence and no failure."""

    def __init__(self) -> None:
        self.ok: dict[str, list[str]] = {c: [] for c in CLAUSES}
        self.bad: dict[str, list[str]] = {c: [] for c in CLAUSES}

    def check(self, clause: str, condition, evidence: str) -> bool:
        evidence = " ".join(evidence.split())
        (self.ok if condition else self.bad)[clause].append(evidence)
        return bool(condition)


R = Results()
# Pre-upgrade failures the SDK raised before any RPC, so no ledger record names the primitive.
SDK_REFUSALS: list[str] = []


# ------------------------------------------------------------------------------------------------
# Processes and files
# ------------------------------------------------------------------------------------------------


def run(cmd, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(c) for c in cmd],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        cwd=REPO,
    )


def target_dir() -> Path:
    env = os.environ.get("CARGO_TARGET_DIR")
    return Path(env) if env else REPO / "target"


def build_cli() -> Path:
    env = dict(os.environ)
    env.setdefault("CARGO_TARGET_DIR", str(target_dir()))
    b = subprocess.run(
        ["cargo", "build", "-p", "cwr-cli"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
    )
    if b.returncode != 0:
        print(b.stderr[-4000:], file=sys.stderr)
        raise SystemExit("cargo build -p cwr-cli failed")
    return target_dir() / "debug" / "cwr"


def make_writable(path: Path) -> None:
    for p in [path, *path.rglob("*")]:
        if not p.is_symlink():
            p.chmod(p.stat().st_mode | stat.S_IWUSR)


def remove_tree(path: Path) -> None:
    if path.exists():
        make_writable(path)
        shutil.rmtree(path)


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def b64(text: str) -> dict:
    return {"$bytes": base64.b64encode(text.encode("utf-8")).decode("ascii")}


def unbytes(value):
    """A kernel state value as the predicate view writes it: UTF-8 text, or {"base64": ...}."""
    if isinstance(value, dict) and set(value) == {"$bytes"}:
        raw = base64.b64decode(value["$bytes"])
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return {"base64": value["$bytes"]}
    if isinstance(value, dict):
        return {k: unbytes(v) for k, v in value.items()}
    if isinstance(value, list):
        return [unbytes(v) for v in value]
    return value


# ------------------------------------------------------------------------------------------------
# The cwr command line
# ------------------------------------------------------------------------------------------------


class Cwr:
    """The real `cwr` binary bound to one Instance home."""

    def __init__(self, binary: Path, home: Path) -> None:
        self.binary = binary
        self.home = home

    def build(self, source: Path, root: Path, previous: str | None = None) -> dict:
        cmd = [self.binary, "build", source, "--out", root, "--json"]
        if previous:
            cmd += ["--previous", previous]
        p = run(cmd)
        try:
            report = json.loads(p.stdout)
        except json.JSONDecodeError:
            report = {"exit_code": p.returncode, "summary": p.stderr.strip()[-400:]}
        report["rc"] = p.returncode
        return report

    def diff(self, a: Path, b: Path) -> tuple[int, dict | None, str]:
        p = run([self.binary, "diff", a, b])
        try:
            return p.returncode, json.loads(p.stdout), p.stderr
        except json.JSONDecodeError:
            return p.returncode, None, p.stderr

    def instance(self, *args, stdin: str | None = None) -> subprocess.CompletedProcess:
        return run([self.binary, "instance", *args, "--home", self.home], stdin=stdin)

    def create(self, build_dir: Path, initial: Path) -> str:
        p = self.instance("create", "--build", build_dir, "--seed", "0", "--initial", initial)
        if p.returncode != 0:
            raise RuntimeError(f"instance create failed: {p.stderr.strip()}")
        return p.stdout.strip()

    def turn(self, ident: str, script: Path) -> dict:
        p = self.instance("turn", ident, "--script", script)
        if p.returncode != 0:
            raise RuntimeError(f"instance turn {script.name} failed rc={p.returncode}: {p.stderr}")
        return json.loads(p.stdout)

    def upgrade(self, ident: str, build_hash: str, root: Path) -> subprocess.CompletedProcess:
        return self.instance("upgrade", ident, build_hash, "--builds", root)

    def state(self, ident: str) -> dict:
        p = self.instance("state", ident)
        if p.returncode != 0:
            raise RuntimeError(f"instance state failed: {p.stderr.strip()}")
        return json.loads(p.stdout)

    def context(self, ident: str) -> tuple[str | None, str]:
        """The model-facing context for the Instance's next turn (`cwr instance context`)."""
        p = self.instance("context", ident)
        if p.returncode == 0 and p.stdout:
            return p.stdout, "cwr instance context"
        return None, f"cwr instance context rc={p.returncode}: {p.stderr.strip()[-200:]}"

    def ledger(self, ident: str) -> list:
        return common.load_ledger(self.home / "instances" / ident / "ledger.jsonl")


# ------------------------------------------------------------------------------------------------
# State view, failures and the notice
# ------------------------------------------------------------------------------------------------


class Handle:
    """The state handle the Build's pure jobs functions read (module state and world_time)."""

    def __init__(self, state: dict, time: int) -> None:
        self._state, self._time = state, time

    def module(self, name: str):
        return self._state[name]

    def world_time(self) -> int:
        return self._time


def state_view(raw: dict, ledger: list, build_dir: Path) -> dict:
    """The predicate state view (benchmark/predicates/common.py) of a kernel state and ledger."""
    time = ledger[-1]["world_time"] if ledger else 0
    turn = max((r.get("turn", 0) for r in ledger), default=0)
    fs = raw.get("fs") or {}
    kv = raw.get("kv") or {}
    namespaces = kv.get("namespaces") if set(kv) == {"namespaces"} else {"(flat)": kv}
    jobs_lib = load_module(build_dir / "lib" / "jobs.py", "cwr_4_3_jobs")
    jobs = {}
    table = (raw.get("jobs") or {}).get("jobs") or {}
    for job_id, job in sorted(table.items()):
        status = jobs_lib._status_of(job, time)
        view = {
            "kind": job["kind"],
            "n": job["n"],
            "submitted_at": job["submitted"],
            "status": status,
        }
        if status == "done":
            view["result"] = jobs_lib.result(Handle(raw, time), job_id)
        jobs[job_id] = view
    return {
        "fs": {
            "files": unbytes(fs.get("files") or {}),
            "locks": sorted(fs.get("locks") or []),
        },
        "kv": {"namespaces": unbytes(namespaces or {})},
        "jobs": {"jobs": jobs},
        "world": {"time": time, "turn": turn},
        "gateway": {"idempotency": {}},
    }


def upgrade_seq(ledger: list) -> int | None:
    seqs = [r["seq"] for r in ledger if r.get("kind") == "upgrade"]
    return seqs[0] if len(seqs) == 1 else None


def first_failure(ledger: list, after: int, envelopes: list) -> dict | None:
    """First failing call after the upgrade record, from the ledger; a refusal raised inside the
    sandbox before any RPC (an absent primitive, a missing parameter) has no call record and comes
    from the turn's envelope error instead."""
    for r in ledger:
        if r["seq"] <= after or r.get("kind") not in CALL_KINDS:
            continue
        outcome = r.get("outcome") or {}
        if outcome.get("status") != "ok":
            failed = [p["index"] for p in r.get("preconditions") or [] if not p.get("passed")]
            return {
                "primitive": r.get("primitive"),
                "code": outcome.get("code"),
                "precondition": failed[0] if failed else None,
                "source": f"ledger seq {r['seq']}",
            }
    for env in envelopes:
        err = env.get("error")
        if err:
            detail = err.get("detail") or ""
            return {
                "primitive": err.get("primitive"),
                "code": err.get("code"),
                "precondition": err.get("precondition_index"),
                "detail": detail,
                "source": f"envelope error ({err.get('primitive')}: {detail[:100]})",
            }
    return None


def names_primitive(failure: dict, primitive: str) -> bool:
    """The failure is attributed to `primitive`: by its call record, or, for a refusal raised in
    the sandbox before any RPC (`turn.script`), by the exception naming the SDK function."""
    if failure["primitive"] == primitive:
        return True
    module, function = primitive.split(".", 1)
    detail = failure.get("detail") or ""
    return failure["primitive"] == "turn.script" and (
        f"module '{module}' has no attribute '{function}'" in detail
        or re.search(rf"\b{re.escape(function)}\(\) (missing|got an unexpected)", detail)
        is not None
    )


def notice_component(context: str) -> str | None:
    if SEP_NOTICE not in context:
        return None
    body = context.split(SEP_NOTICE, 1)[1]
    return body.split(SEP_TAIL, 1)[0]


def used_primitives(ledger: list) -> list:
    """Primitives with a call record in the ledger: the notice's affecting set (kernel.md 10)."""
    return sorted({r["primitive"] for r in ledger if r.get("kind") in CALL_KINDS})


# ------------------------------------------------------------------------------------------------
# One pair
# ------------------------------------------------------------------------------------------------


def initial_json(pair: str, scratch: Path) -> Path:
    """The task's initial.kvx as the kernel's --initial JSON (bytes as {"$bytes": base64})."""
    sections = kvx.load(PAIRS_DIR / pair / "task" / "initial.kvx")
    out: dict = {}
    fs = sections.get("fs")
    if fs is not None:
        out["fs"] = {
            "files": {p: b64(t) for p, t in sorted(fs.get("files", {}).items())},
            "locks": sorted(fs.get("locks", [])),
        }
    kv = sections.get("kv")
    if kv is not None:
        if set(kv) == {"namespaces"}:
            out["kv"] = {
                "namespaces": {
                    ns: {k: b64(v) for k, v in keys.items()}
                    for ns, keys in kv["namespaces"].items()
                }
            }
        else:
            out["kv"] = {k: b64(v) for k, v in kv.items()}
    path = scratch / f"initial_{pair}.json"
    path.write_text(json.dumps(out, sort_keys=True), encoding="utf-8")
    return path


def strip_migration(diff: dict) -> dict:
    """A ContractDiff without the state_shape migration names, which only a World Source's
    CHANGELOG.kvx supplies (a Build carries contract_diff.json, not the changelog)."""
    out = dict(diff)
    out["entries"] = [{k: v for k, v in e.items() if k != "migration"} for e in diff["entries"]]
    return out


def strip_bump(entries: list) -> list:
    return [{k: v for k, v in e.items() if k != "bump"} for e in entries]


def entries_match(real: list, frozen: list, migrations: list) -> tuple[bool, str]:
    """Real compiler entries against frozen ones. A frozen state_shape entry may omit the
    compiler's before/after shape strings and migration path, which must then name a declared
    migration of the release."""
    real = strip_bump(real)
    if len(real) != len(frozen):
        return False, f"{len(real)} entries, frozen {len(frozen)}"
    for got, want in zip(real, frozen, strict=True):
        extra = set(got) - set(want)
        if any(got.get(k) != v for k, v in want.items()):
            return False, f"entry {got.get('target')}.{got.get('field')} differs: {got} vs {want}"
        if extra and not (
            got.get("kind") == "state_shape" and extra <= {"before", "after", "migration"}
        ):
            return False, f"entry {got.get('target')} has unfrozen keys {sorted(extra)}"
        if "migration" in extra and got["migration"] not in migrations:
            return False, f"state_shape migration {got['migration']} is not declared"
    return True, f"{len(real)} entries"


class Pair:
    def __init__(self, name: str, frozen: dict, cwr: Cwr, scratch: Path) -> None:
        self.name = name
        self.pair = frozen[f"pair.{name}"]
        self.diff = frozen[f"diff.{name}"]
        self.notice = frozen[f"notice.{name}"]
        self.task = frozen[f"task.{name}"]
        self.cwr = cwr
        self.scratch = scratch
        self.root = scratch / "builds" / name
        self.root.mkdir(parents=True)
        self.src_a = REPO / self.pair["source_a"]
        self.src_b = REPO / self.pair["source_b"]
        self.hash_a = self.hash_b = None
        self.dir_a = self.dir_b = None
        self.observed: dict = {}

    def tag(self, text: str) -> str:
        return f"{self.name}: {text}"

    # -- 11.4: both sides compile; diff, bump and changelog --------------------------------------

    def compile(self) -> bool:
        ra = self.cwr.build(self.src_a, self.root)
        if not R.check(
            "11.4", ra["rc"] == 0, self.tag(f"cwr build A rc={ra['rc']} {ra.get('summary')}")
        ):
            return False
        self.hash_a, self.dir_a = ra["build"]["hash"], Path(ra["build"]["dir"])
        rb = self.cwr.build(self.src_b, self.root, previous=self.hash_a)
        if not R.check(
            "11.4", rb["rc"] == 0, self.tag(f"cwr build B rc={rb['rc']} {rb.get('summary')}")
        ):
            return False
        self.hash_b, self.dir_b = rb["build"]["hash"], Path(rb["build"]["dir"])
        manifest = kvx.load(self.dir_b / "manifest.kvx")["build"]
        R.check(
            "11.4",
            manifest["version"] == self.pair["to_version"]
            and manifest["previous"] == self.hash_a
            and manifest["name"] == "refworld"
            and kvx.load(self.dir_a / "manifest.kvx")["build"]["version"]
            == self.pair["from_version"],
            self.tag(
                f"Builds {self.pair['from_version']} {self.hash_a[:19]} -> "
                f"{manifest['version']} {self.hash_b[:19]} (B.previous = A)"
            ),
        )
        return True

    def check_diff(self) -> None:
        packaged = json.loads((self.dir_b / "contract_diff.json").read_text(encoding="utf-8"))
        rc, printed, err = self.cwr.diff(self.src_a, self.src_b)
        same = printed is not None and all(
            printed.get(k) == v for k, v in packaged.items() if k != "previous"
        )
        R.check(
            "11.4",
            rc == 0 and same,
            self.tag(
                f"cwr diff <source A> <source B> rc={rc} equals B's contract_diff.json {err[-200:]}"
            ),
        )
        rc, printed, err = self.cwr.diff(self.dir_a, self.dir_b)
        R.check(
            "11.4",
            rc == 0
            and printed is not None
            and strip_migration(printed) == strip_migration(packaged),
            self.tag(
                f"cwr diff <Build A> <Build B> rc={rc} equals it (migration names aside) {err[-200:]}"
            ),
        )
        ok, why = entries_match(packaged["entries"], self.diff["entries"], self.pair["migrations"])
        R.check("11.4", ok, self.tag(f"ContractDiff entries match [diff.{self.name}]: {why}"))
        R.check(
            "11.4",
            strip_bump(packaged["doc_entries"]) == self.diff["doc_entries"]
            and packaged["doc_only_entries"] == self.diff["doc_only_entries"],
            self.tag(f"{packaged['doc_only_entries']} doc entries match"),
        )
        bumps = (
            packaged["derived_bump"],
            packaged["declared_bump"],
            packaged["pre_1_0_rule_applied"],
        )
        want = (
            self.diff["derived_bump"],
            self.diff["declared_bump"],
            self.diff["pre_1_0_rule_applied"],
        )
        R.check(
            "11.4", bumps == want, self.tag(f"bump derived/declared/P15 {bumps}, frozen {want}")
        )

    def check_changelog(self) -> None:
        log_a = kvx.load(self.src_a / "CHANGELOG.kvx")
        log_b = kvx.load(self.src_b / "CHANGELOG.kvx")
        release = log_b.get(f"release.{self.pair['to_version']}")
        if not R.check(
            "11.4", release is not None, self.tag("B's CHANGELOG.kvx has the new release")
        ):
            return
        changes = [log_b[f"change.{c}"] for c in release["changes"]]
        kinds = [c["kind"] for c in changes]
        migrations = [c["migration"] for c in changes if c.get("migration")]
        inherited = all(log_b.get(k) == v for k, v in log_a.items() if k != "meta")
        packaged = json.loads((self.dir_b / "contract_diff.json").read_text(encoding="utf-8"))
        rule = {(e["kind"], e["target"]) for e in packaged["entries"] if e["kind"]}
        docs = {("doc_only", e["target"]) for e in packaged["doc_entries"]}
        declared = {(c["kind"], c["target"]) for c in changes}
        state_modules = {c["target"] for c in changes if c["kind"] == "state_shape"}
        params = {e["target"] for e in packaged["entries"] if e["kind"] is None}
        covered = (
            rule <= declared
            and declared <= rule | docs
            and all(t.split(".")[0] in state_modules for t in params)
        )
        R.check(
            "11.4",
            covered,
            self.tag(
                f"diff matches the declared changelog: rule entries {sorted(rule)}, "
                f"declared {sorted(declared)}, params entries covered by state_shape {sorted(params)}"
            ),
        )
        present = all((self.src_b / m).is_file() and (self.dir_b / m).is_file() for m in migrations)
        R.check(
            "11.4",
            release["changes"] == self.pair["changes"]
            and kinds == self.pair["change_kinds"]
            and migrations == self.pair["migrations"]
            and inherited
            and present
            and release["summary"] == self.notice["summary"],
            self.tag(
                f"changelog {self.pair['to_version']} kinds {kinds} migrations {migrations} "
                f"(A's history kept={inherited}, migration files in source and Build={present})"
            ),
        )

    # -- 5.2 and 5.3: live upgrade, notice, second halves -----------------------------------------

    def play(self, second_half: list, label: str) -> dict:
        """First half on A, upgrade to B, then the given second half; returns the run record."""
        initial = initial_json(self.name, self.scratch)
        ident = self.cwr.create(self.dir_a, initial)
        task_dir = PAIRS_DIR / self.name
        first = [self.cwr.turn(ident, task_dir / s) for s in self.task["first_half"]]
        before_state = self.cwr.state(ident)
        before_ledger = self.cwr.ledger(ident)
        up = self.cwr.upgrade(ident, self.hash_b, self.root)
        run_record = {
            "id": ident,
            "first": first,
            "before_state": before_state,
            "before_ledger": before_ledger,
            "upgrade": up,
            "second": [],
            "contexts": [],
        }
        if up.returncode != 0:
            return run_record
        run_record["after_state"] = self.cwr.state(ident)
        run_record["after_ledger"] = self.cwr.ledger(ident)
        for script in second_half:
            run_record["contexts"].append(self.cwr.context(ident))
            run_record["second"].append(self.cwr.turn(ident, task_dir / script))
        run_record["contexts"].append(self.cwr.context(ident))
        run_record["final_state"] = self.cwr.state(ident)
        run_record["ledger"] = self.cwr.ledger(ident)
        return run_record

    def check_upgrade(self, run_record: dict) -> bool:
        up = run_record["upgrade"]
        first_ok = all(e["status"] == "ok" for e in run_record["first"])
        R.check("5.2", first_ok, self.tag(f"first half {len(run_record['first'])} turn(s) ok on A"))
        if not R.check(
            "5.2",
            up.returncode == 0,
            self.tag(f"cwr instance upgrade rc={up.returncode} {up.stderr.strip()[-300:]}"),
        ):
            return False
        ledger = run_record["after_ledger"]
        seq = upgrade_seq(ledger)
        record = next((r for r in ledger if r.get("kind") == "upgrade"), {})
        text = json.dumps(record, sort_keys=True)
        names = [Path(m).name for m in self.pair["migrations"]]
        applied = record.get("migrations") or []
        applied_names = [Path(m if isinstance(m, str) else json.dumps(m)).name for m in applied]
        R.check(
            "5.2",
            seq is not None
            and seq == ledger[-1]["seq"]
            and self.hash_a in text
            and self.hash_b in text
            and all(any(n in a for a in applied_names) for n in names)
            and len(applied) == len(names),
            self.tag(f"one upgrade record (seq {seq}) naming both Builds, migrations {applied}"),
        )
        before, after = run_record["before_state"], run_record["after_state"]
        if self.pair["migrations"]:
            expected = before
            for m in self.pair["migrations"]:
                expected = load_module(self.dir_b / m, "cwr_4_3_migration").migrate(expected)
            R.check(
                "5.2",
                after == expected and after != before,
                self.tag(
                    f"state equals the Build's migrations applied in order: kv {after.get('kv')}"
                ),
            )
        else:
            R.check(
                "5.2", after == before, self.tag("state unchanged by an upgrade with no migration")
            )
        return True

    def check_notice(self, run_record: dict) -> None:
        if not run_record["contexts"]:
            R.check("5.3", False, self.tag("no context after the upgrade"))
            return
        first, how = run_record["contexts"][0]
        if not R.check("5.3", first is not None, self.tag(how)):
            return
        expected = (
            self.notice["text"].replace("{hash_a}", self.hash_a).replace("{hash_b}", self.hash_b)
        )
        got = notice_component(first)
        got_text = None if got is None else got.rstrip("\n")
        R.check(
            "5.3",
            got_text == expected,
            self.tag(
                f"notice in the first context after the upgrade equals [notice.{self.name}].text"
                if got_text == expected
                else f"notice {got_text!r} != {expected!r}"
            ),
        )
        manual = (self.dir_b / "manual.md").read_text(encoding="utf-8")
        R.check(
            "5.3",
            first.startswith(manual),
            self.tag("the context opens with Build B's manual"),
        )
        later = [c for c, _ in run_record["contexts"][1:]]
        R.check(
            "5.3",
            bool(later) and all(c is not None and notice_component(c) is None for c in later),
            self.tag(f"no notice in the {len(later)} later context(s)"),
        )
        used = used_primitives(run_record["before_ledger"])
        R.check(
            "5.3",
            used == self.notice["used_primitives"],
            self.tag(f"first-half used primitives {used} (the notice's affecting set)"),
        )

    def check_second_half(self, run_record: dict, variant: str) -> None:
        worlds = {(e["world"]["version"], e["world"]["build"]) for e in run_record["second"]}
        R.check(
            "5.2",
            worlds == {(self.pair["to_version"], self.hash_b)},
            self.tag(
                f"{variant} turns after the upgrade run on {[(v, h[:19]) for v, h in worlds]}"
            ),
        )
        predicate = common.load_predicate(PAIRS_DIR / self.name / "task")
        view = state_view(run_record["final_state"], run_record["ledger"], self.dir_b)
        success = common.evaluate_twice(predicate, view, run_record["ledger"])
        statuses = [e["status"] for e in run_record["second"]]
        seq = upgrade_seq(run_record["ledger"])
        if variant == "post":
            count = sum(1 for r in run_record["ledger"] if r.get("kind") in CALL_KINDS)
            self.observed["count"] = count
            bin_name = "short" if count <= 5 else "medium" if count <= 25 else "long"
            R.check(
                "11.4",
                success and all(s == "ok" for s in statuses) and bin_name == self.task["bin"],
                self.tag(
                    f"post-upgrade solution succeeds (turns {statuses}, {count} primitive calls, "
                    f"bin {bin_name}, frozen count {self.task['reference_primitive_count']})"
                ),
            )
            return
        expected = self.task["pre_adapted_failure" if variant == "pre_adapted" else "pre_failure"]
        failure = first_failure(
            run_record["ledger"], seq if seq is not None else -1, run_record["second"]
        )
        got = failure and {k: failure[k] for k in ("primitive", "code", "precondition")}
        if expected is None:
            R.check(
                "11.4",
                success == self.task["pre_success"] and failure is None,
                self.tag(
                    f"{variant} solution succeeds unchanged (doc-only control), turns {statuses}"
                ),
            )
            return
        attributed = failure is not None and names_primitive(failure, expected["primitive"])
        if attributed and failure["primitive"] != expected["primitive"]:
            SDK_REFUSALS.append(
                f"{self.name} {variant}: {expected['primitive']} {failure['detail']}"
            )
        R.check(
            "11.4",
            not success
            and attributed
            and got["code"] == expected["code"]
            and got["precondition"] == expected["precondition"],
            self.tag(
                f"{variant} solution fails with the documented code: {got} from "
                f"{failure and failure['source']}, frozen {expected}; predicate {success}"
            ),
        )


# ------------------------------------------------------------------------------------------------
# Rollback (5.2)
# ------------------------------------------------------------------------------------------------


def rollback_case(cwr: Cwr, u5: Pair, scratch: Path, target_hash: str, root: Path, label: str):
    initial = initial_json("U5", scratch)
    ident = cwr.create(u5.dir_a, initial)
    task_dir = PAIRS_DIR / "U5"
    for s in u5.task["first_half"]:
        cwr.turn(ident, task_dir / s)
    before_state, before_ledger = cwr.state(ident), cwr.ledger(ident)
    up = cwr.upgrade(ident, target_hash, root)
    after_state, after_ledger = cwr.state(ident), cwr.ledger(ident)
    probe = scratch / "probe_version.py"
    probe.write_text("import world\nreturn world.version()\n", encoding="utf-8")
    env = cwr.turn(ident, probe)
    unchanged = (
        after_state == before_state
        and after_ledger[-1]["build"] == u5.hash_a
        and not any(r.get("kind") == "upgrade" for r in after_ledger)
        and after_ledger[: len(before_ledger)] == before_ledger
        and env["status"] == "ok"
        and env["world"]["version"] == u5.pair["from_version"]
        and env["world"]["build"] == u5.hash_a
    )
    R.check(
        "5.2",
        up.returncode != 0 and unchanged,
        f"rollback ({label}): upgrade rc={up.returncode} "
        f"({up.stderr.strip().splitlines()[-1][:160] if up.stderr.strip() else 'no message'}); "
        f"state, Build binding and ledger unchanged={unchanged}; next turn runs on "
        f"{env['world']['version']} {env['world']['build'][:19]}",
    )


def rollbacks(cwr: Cwr, u5: Pair, scratch: Path) -> None:
    # A Build whose migration refuses the task's state: compiles (its fixtures pass), fails mid-upgrade.
    src = scratch / "U5-broken"
    shutil.copytree(u5.src_b, src)
    migration = src / u5.pair["migrations"][0]
    text = migration.read_text(encoding="utf-8")
    hook = "def migrate(state: dict) -> dict:\n"
    poison = (
        hook + '    if "receipt-chess" in state.get("kv", {}):\n'
        '        raise ValueError("driver-injected migration failure")\n'
    )
    migration.write_text(text.replace(hook, poison, 1), encoding="utf-8")
    root = scratch / "builds" / "U5-broken"
    root.mkdir(parents=True)
    rb = cwr.build(src, root, previous=str(u5.dir_a))
    if R.check(
        "5.2", rb["rc"] == 0, f"failing-migration Build compiles: rc={rb['rc']} {rb.get('summary')}"
    ):
        rollback_case(
            cwr,
            u5,
            scratch,
            rb["build"]["hash"],
            root,
            "migration raises",
        )
    # Build B modified after packaging: hash verification must refuse it before anything changes.
    root = scratch / "builds" / "U5-tampered"
    root.mkdir(parents=True)
    copy = root / u5.dir_b.name
    shutil.copytree(u5.dir_b, copy)
    make_writable(copy)
    with open(copy / "manual.md", "a", encoding="utf-8") as handle:
        handle.write("\nedited after packaging\n")
    rollback_case(cwr, u5, scratch, u5.hash_b, root, "Build modified after packaging")


# ------------------------------------------------------------------------------------------------
# Freezing observed values
# ------------------------------------------------------------------------------------------------


def freeze(pairs: list) -> None:
    """Rewrite build_a, build_b, reference_primitive_count and count_source in upgrades.kvx."""
    lines = UPGRADES.read_text(encoding="utf-8").split("\n")
    section = None
    by_name = {p.name: p for p in pairs}
    for i, line in enumerate(lines):
        m = re.fullmatch(r"\[(pair|task)\.(U\d)\]", line)
        if m:
            section = (m.group(1), by_name.get(m.group(2)))
            continue
        if line.startswith("["):
            section = None
        if not section or section[1] is None:
            continue
        kind, p = section
        key = line.split(" = ", 1)[0]
        if kind == "pair" and key == "build_a":
            lines[i] = f"build_a = {json.dumps(p.hash_a)}"
        elif kind == "pair" and key == "build_b":
            lines[i] = f"build_b = {json.dumps(p.hash_b)}"
        elif kind == "task" and key == "reference_primitive_count" and "count" in p.observed:
            lines[i] = f"reference_primitive_count = {p.observed['count']}"
        elif kind == "task" and key == "count_source":
            lines[i] = 'count_source = "kernel"'
    UPGRADES.write_text("\n".join(lines), encoding="utf-8")


# ------------------------------------------------------------------------------------------------
# Main
# ------------------------------------------------------------------------------------------------


def main() -> int:
    freezing = "--freeze" in sys.argv[1:]
    binary = build_cli()
    frozen = kvx.load(UPGRADES)
    classes = {frozen[f"pair.{p}"]["pair_class"] for p in PAIRS}
    R.check(
        "11.4",
        frozen["meta"]["pairs"] == PAIRS and classes == REQUIRED_CLASSES,
        f"six pairs U1..U6 cover the six change classes {sorted(classes)}",
    )
    keep = os.environ.get("CWR_KEEP_SCRATCH")
    scratch = Path(tempfile.mkdtemp(prefix="cwr-4_3-"))
    pairs: list[Pair] = []
    try:
        cwr = Cwr(binary, scratch / "home")
        for name in PAIRS:
            pair = Pair(name, frozen, cwr, scratch)
            pairs.append(pair)
            if not pair.compile():
                continue
            pair.check_diff()
            pair.check_changelog()
            post = pair.play(pair.task["post_upgrade"], "post")
            if pair.check_upgrade(post):
                pair.check_notice(post)
                pair.check_second_half(post, "post")
            for variant, key in (("pre", "pre_upgrade"), ("pre_adapted", "pre_upgrade_adapted")):
                if pair.task.get(key):
                    run_record = pair.play(pair.task[key], variant)
                    if run_record["upgrade"].returncode == 0:
                        pair.check_second_half(run_record, variant)
                    else:
                        R.check("5.2", False, pair.tag(f"{variant} run: upgrade refused"))
        u5 = next(p for p in pairs if p.name == "U5")
        if u5.hash_b:
            rollbacks(cwr, u5, scratch)
        else:
            R.check("5.2", False, "rollback not exercised: U5 did not compile")
    finally:
        if keep:
            print(f"scratch kept at {scratch}", file=sys.stderr)
        else:
            remove_tree(scratch)
    for line in SDK_REFUSALS:
        print(f"NOTE sandbox-side refusal without a ledger call record: {line}")
    rc = 0
    for c in CLAUSES:
        bad, ok = R.bad[c], R.ok[c]
        if bad or not ok:
            rc = 1
            print(f"CLAUSE {c} FAIL " + " | ".join(bad or ["no evidence"]))
        else:
            print(f"CLAUSE {c} PASS " + " | ".join(ok))
    if freezing and rc == 0:
        freeze(pairs)
        print("froze Build hashes and reference primitive counts into benchmark/upgrades.kvx")
    return rc


if __name__ == "__main__":
    sys.exit(main())

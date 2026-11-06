#!/usr/bin/env python3
# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Task 5.2 driver: H1 stability and H2 direct-action campaigns (CWR-018, clauses 10.2, 10.4, 12.2, 12.5).

Everything is checked against the real implementation and the stored campaign data; nothing is mocked:

1. `cargo build -p cwr-cli` (honours CARGO_TARGET_DIR; default the workspace target dir).
2. benchmark/campaigns/h1.kvx and h2.kvx are read. The drift schedule of every task is recomputed
   here, independently of benchmark/campaigns/h1h2.py, from the rule in spec/cwr/experiments.md 3.1
   (len from benchmark/suite.kvx; turns max(1, floor(len*p/q))).
3. 10.2: every registered drift Build verifies through `cwr manual`, is stamped x-drift, embeds the
   registered schedule and has a manual byte-identical to the control Build's; a fresh Instance of one
   drift Build, driven past its scheduled turn with kernel-direct scripts, writes a kind drift record
   at that turn; and every drift-arm ledger carries exactly the scheduled drift records at the
   scheduled turns (for every scheduled turn the Instance reached) and no other drift record.
4. 10.4: the one-primitive Build verifies, is stamped x-one-primitive, its contracts set
   primitive_calls = 1 and its manual states that limit; a fresh Instance shows the second call of a
   turn returns LIMIT naming primitive_calls=1; every one-primitive ledger has no turn with a second
   ok call and every second call in a turn is LIMIT.
5. 12.2: the H1 matrix (frozen, drift-1, drift-3) and the H2 matrix (script, one-primitive) have a
   ledger for every one of the 60 tasks and 10 registered seeds, and every ledger header carries the
   registered arm, seed, model, tail and the cell's registered Build hash.
6. 12.5: the directions in the campaign files equal the pre-registered ones, registration precedes
   the first attempt, the metrics.py digest equals the registered one, analyse.py run afresh on the
   stored ledgers equals benchmark/results/h<N>/analysis.json, every arm and bin has a mean and a 95%
   interval, and every number in research/h1-stability.md and research/h2-direct-action.md equals
   the fresh recomputation.

Prints one line per clause, `CLAUSE <req.ac> PASS|FAIL <evidence>`, and exits 0 only when every clause
passes. Python 3.12, standard library only.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parents[3]
SUITE = REPO / "benchmark" / "suite.kvx"
PREREG = REPO / "benchmark" / "preregistration.kvx"
METRICS = REPO / "benchmark" / "harness" / "metrics.py"
RESULTS = REPO / "benchmark" / "results"
CAMPAIGN = {"H1": REPO / "benchmark/campaigns/h1.kvx", "H2": REPO / "benchmark/campaigns/h2.kvx"}
REPORT = {"H1": REPO / "research/h1-stability.md", "H2": REPO / "research/h2-direct-action.md"}
ARMS = {"H1": ("frozen", "drift-1", "drift-3"), "H2": ("script", "one-primitive")}
SEEDS = list(range(1, 11))
BINS = ("short", "medium", "long")
FRACTIONS = {"drift-1": [(1, 2)], "drift-3": [(1, 4), (1, 2), (3, 4)]}
CLAUSES = ("10.2", "10.4", "12.2", "12.5")
CALL_KINDS = ("primitive", "adapter")


class Clauses:
    def __init__(self) -> None:
        self.ok: dict[str, list[str]] = {c: [] for c in CLAUSES}
        self.bad: dict[str, list[str]] = {c: [] for c in CLAUSES}

    def check(self, clause: str, condition: bool, evidence: str) -> bool:
        (self.ok if condition else self.bad)[clause].append(evidence)
        return condition

    def report(self) -> int:
        failed = 0
        for c in CLAUSES:
            if self.bad[c]:
                failed += 1
                shown = "; ".join(self.bad[c][:6])
                more = f" (+{len(self.bad[c]) - 6} more)" if len(self.bad[c]) > 6 else ""
                print(f"CLAUSE {c} FAIL {shown}{more}")
            else:
                print(f"CLAUSE {c} PASS {'; '.join(self.ok[c]) or 'no evidence'}")
        return 1 if failed else 0


# ---------------------------------------------------------------------------------------------
# Strict readers (independent of the harness code under test)


def parse_kvx(text: str, origin: str) -> dict[str, dict]:
    sections: dict[str, dict] = {}
    current: dict | None = None
    for number, raw in enumerate(text.split("\n"), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1]
            if name in sections:
                raise ValueError(f"{origin}:{number}: duplicate section [{name}]")
            current = sections[name] = {}
            continue
        if current is None or " = " not in line:
            raise ValueError(f"{origin}:{number}: not a key = value line inside a section")
        key, value = line.split(" = ", 1)
        current[key.strip()] = json.loads(value)
    return sections


def read_kvx(path: Path) -> dict[str, dict]:
    return parse_kvx(path.read_text(encoding="utf-8"), str(path))


def read_ledger(path: Path) -> list[dict]:
    records = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    for index, record in enumerate(records):
        if record.get("seq") != index:
            raise ValueError(f"{path}: seq {record.get('seq')} at position {index}")
    return records


def sha256_file(path: Path) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def cwr_binary() -> Path:
    if os.environ.get("CWR_BIN"):
        return Path(os.environ["CWR_BIN"])
    target = Path(os.environ.get("CARGO_TARGET_DIR") or REPO / "target")
    return target / "debug" / "cwr"


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=False, cwd=REPO, **kwargs)


BUILD_ROOTS = [REPO / "build", REPO / "xbuild"]


def build_dir(build: str) -> Path | None:
    """The directory of a Build hash under the registered campaign roots, build/ or xbuild/."""
    digest = build.removeprefix("sha256:")
    for root in BUILD_ROOTS:
        if (root / digest).is_dir():
            return root / digest
    return None


def suite_tasks() -> dict[str, dict]:
    suite = read_kvx(SUITE)
    order = [t for b in BINS for t in suite[f"bin.{b}"]["tasks"]]
    return {t: suite[f"task.{t}"] for t in order}


# ---------------------------------------------------------------------------------------------
# Build checks


def verify_build(binary: Path, build: str) -> tuple[bool, str]:
    """`cwr manual` verifies the Build hash before printing the manual."""
    path = build_dir(build)
    if path is None:
        return False, f"{build} not under build/ or xbuild/"
    proc = run([str(binary), "manual", str(path)])
    return proc.returncode == 0, proc.stderr.strip()[:200]


def manifest(build: str) -> dict:
    path = build_dir(build)
    return read_kvx(path / "manifest.kvx")["build"] if path else {}


def embedded_drift(path: Path) -> list[dict] | None:
    """The drift schedule a drift Build carries, from contracts.json or an embedded drift file."""
    contracts = json.loads((path / "contracts.json").read_text(encoding="utf-8"))
    for key in ("drift", "drift_schedule"):
        if isinstance(contracts.get(key), list):
            return contracts[key]
        if isinstance(contracts.get(key), dict) and isinstance(contracts[key].get("entries"), list):
            return contracts[key]["entries"]
    for candidate in sorted(path.rglob("*.kvx")):
        if "drift" in candidate.name:
            doc = read_kvx(candidate)
            if doc.get("meta", {}).get("format") == "cwr.drift":
                return [
                    {"n": int(k.split(".")[1]), **v}
                    for k, v in sorted(doc.items(), key=lambda kv: kv[0])
                    if k.startswith("drift.")
                ]
    return None


def check_drift_builds(c: Clauses, binary: Path, h1: dict, control_manual: bytes) -> None:
    tasks = suite_tasks()
    plan_ok = builds_ok = manual_ok = embed_ok = 0
    for task, info in tasks.items():
        section = h1.get(f"task.{task}")
        if section is None:
            c.check("10.2", False, f"h1.kvx has no [task.{task}]")
            continue
        length = int(info["turns"])
        for arm, fractions in FRACTIONS.items():
            key = arm.replace("-", "_")
            entries = section.get(key) or []
            expected = [max(1, length * p // q) for p, q in fractions]
            turns = [e.get("turn") for e in entries]
            rules = set(info["rules"])
            sound = (
                turns == expected
                and section.get("len") == length
                and all(e.get("rule") in rules for e in entries)
                and all(e.get("primitive") == e.get("rule", "").split("[")[0] for e in entries)
            )
            if not c.check("10.2", sound, f"{task} {arm} schedule {turns} != {expected}"):
                continue
            plan_ok += 1
            build = section.get(f"{key}_build", "")
            good, why = verify_build(binary, build)
            m = manifest(build) if good else {}
            stamped = m.get("experimental") is True and str(m.get("experimental_flag", "")).lstrip(
                "-"
            ).startswith("x-drift")
            if not c.check("10.2", good and stamped, f"{task} {arm} Build {build}: {why or m}"):
                continue
            builds_ok += 1
            path = build_dir(build)
            if c.check(
                "10.2",
                (path / "manual.md").read_bytes() == control_manual,
                f"{task} {arm} manual differs from the control manual",
            ):
                manual_ok += 1
            embedded = embedded_drift(path)
            same = embedded is not None and [
                (int(e["turn"]), e["primitive"], e["change"], e["expr"], e["failure"])
                for e in embedded
            ] == [
                (e["turn"], e["primitive"], e["change"], e["expr"], e["failure"]) for e in entries
            ]
            if c.check("10.2", same, f"{task} {arm} embedded schedule {embedded} != registered"):
                embed_ok += 1
    c.check(
        "10.2",
        plan_ok == builds_ok == manual_ok == embed_ok == 2 * len(tasks),
        f"{plan_ok} schedules recomputed, {builds_ok} drift Builds verified and stamped, "
        f"{manual_ok} manuals byte-identical to control, {embed_ok} embedded schedules equal h1.kvx",
    )


def kernel_turns(binary: Path, build: str, scripts: list[str], home: Path) -> list[dict]:
    """Create an Instance of a Build and run kernel-direct scripts; returns the ledger."""
    create = run(
        [
            str(binary),
            "instance",
            "create",
            "--build",
            str(build_dir(build)),
            "--seed",
            "1",
            "--arm",
            "5.2",
            "--model",
            "driver",
            "--allow-experimental",
            "--home",
            str(home),
        ]
    )
    if create.returncode != 0:
        raise RuntimeError(f"instance create exited {create.returncode}: {create.stderr[-300:]}")
    instance = create.stdout.strip()
    for script in scripts:
        turn = run(
            [str(binary), "instance", "turn", instance, "--home", str(home)]
            + [arg for root in BUILD_ROOTS for arg in ("--builds", str(root))],
            input=script,
        )
        if turn.returncode != 0:
            raise RuntimeError(f"instance turn exited {turn.returncode}: {turn.stderr[-300:]}")
    return read_ledger(home / "instances" / instance / "ledger.jsonl")


def live_drift_probe(c: Clauses, binary: Path, h1: dict) -> None:
    """Drive one drift Build past a turn-3 or later injection with no model involved."""
    for name, section in sorted(h1.items()):
        if not name.startswith("task.") or not section.get("drift_3"):
            continue
        late = [e for e in section["drift_3"] if e["turn"] >= 2]
        if not late:
            continue
        build = section["drift_3_build"]
        last = max(e["turn"] for e in section["drift_3"])
        with tempfile.TemporaryDirectory(prefix="cwr-5_2-drift-") as tmp:
            try:
                ledger = kernel_turns(
                    binary, build, ["import world\nworld.now()\n"] * last, Path(tmp)
                )
            except RuntimeError as error:
                c.check("10.2", False, f"drift probe on {name}: {error}")
                return
        drifts = [(r.get("turn"), r.get("primitive")) for r in ledger if r.get("kind") == "drift"]
        expected = [(e["turn"], e["primitive"]) for e in section["drift_3"]]
        c.check(
            "10.2",
            sorted(drifts) == sorted(expected),
            f"fresh Instance of {name} drift-3 Build run {last} turns: drift records {drifts}, "
            f"scheduled {expected}",
        )
        return
    c.check("10.2", False, "no drift-3 schedule with an injection at turn 2 or later to probe")


def check_one_primitive(c: Clauses, binary: Path, h2: dict) -> None:
    build = h2["registration"]["builds"]["one-primitive"]
    good, why = verify_build(binary, build)
    m = manifest(build) if good else {}
    stamped = m.get("experimental") is True and str(m.get("experimental_flag", "")).lstrip(
        "-"
    ).startswith("x-one-primitive")
    if not c.check("10.4", good and stamped, f"one-primitive Build {build}: {why or m}"):
        return
    path = build_dir(build)
    contracts = json.loads((path / "contracts.json").read_text(encoding="utf-8"))
    limits = contracts.get("limits") or contracts.get("world", {}).get("limits") or {}
    c.check(
        "10.4",
        limits.get("primitive_calls") == 1,
        f"contracts.json limits.primitive_calls = {limits.get('primitive_calls')}",
    )
    manual = (path / "manual.md").read_text(encoding="utf-8")
    c.check(
        "10.4",
        re.search(r"primitive_calls\W{1,6}1\b", manual) is not None,
        "manual states primitive_calls = 1",
    )
    with tempfile.TemporaryDirectory(prefix="cwr-5_2-one-") as tmp:
        try:
            ledger = kernel_turns(
                binary, build, ["import world\nworld.now()\nworld.now()\n"], Path(tmp)
            )
        except RuntimeError as error:
            c.check("10.4", False, f"one-primitive probe: {error}")
            ledger = []
    calls = [r for r in ledger if r.get("kind") in CALL_KINDS]
    second = calls[1] if len(calls) > 1 else {}
    outcome = second.get("outcome") or {}
    c.check(
        "10.4",
        len(calls) == 2
        and calls[0]["outcome"]["status"] == "ok"
        and outcome.get("status") == "error"
        and outcome.get("code") == "LIMIT",
        f"fresh Instance: second call of a turn -> {outcome}",
    )


# ---------------------------------------------------------------------------------------------
# Ledger checks


def cell_ledger(hypothesis: str, arm: str, task: str, seed: int) -> list[dict] | None:
    path = RESULTS / hypothesis.lower() / arm / task / str(seed) / "ledger.jsonl"
    if not path.exists():
        return None
    return read_ledger(path)


def check_matrix(c: Clauses, campaigns: dict[str, dict], prereg: dict) -> dict:
    tasks = suite_tasks()
    model = prereg["campaign"].get("model")
    ledgers: dict[tuple, list[dict]] = {}
    for hypothesis, arms in ARMS.items():
        reg = campaigns[hypothesis]["registration"]
        c.check(
            "12.2",
            list(reg["arms"]) == list(arms) and reg["seeds"] == SEEDS and reg["tasks"] == 60,
            f"{hypothesis} registered arms {reg['arms']}, seeds {reg['seeds']}, tasks {reg['tasks']}",
        )
        for arm in arms:
            present = header_ok = 0
            problems: list[str] = []
            for task in tasks:
                for seed in SEEDS:
                    ledger = cell_ledger(hypothesis, arm, task, seed)
                    if ledger is None:
                        problems.append(f"missing {hypothesis.lower()}/{arm}/{task}/{seed}")
                        continue
                    present += 1
                    ledgers[(hypothesis, arm, task, seed)] = ledger
                    head = ledger[0]
                    builds = reg["builds"]
                    build = builds[arm]
                    if build == "per_task":
                        build = campaigns["H1"][f"task.{task}"][f"{arm.replace('-', '_')}_build"]
                    ok = (
                        head.get("kind") == "header"
                        and head.get("arm") == f"{hypothesis}/{arm}"
                        and head.get("seed") == seed
                        and head.get("tail_size") == reg["tail"]
                        and (not model or head.get("model") == model)
                        and head.get("build") == build
                    )
                    if ok:
                        header_ok += 1
                    else:
                        problems.append(
                            f"{hypothesis.lower()}/{arm}/{task}/{seed} header "
                            f"{ {k: head.get(k) for k in ('arm', 'seed', 'tail_size', 'build')} }"
                        )
            expected = len(tasks) * len(SEEDS)
            c.check(
                "12.2",
                present == header_ok == expected,
                f"{hypothesis}/{arm}: {present}/{expected} ledgers, {header_ok} headers match"
                + (f" ({problems[0]}; {len(problems)} problems)" if problems else ""),
            )
    return ledgers


def check_drift_ledgers(c: Clauses, ledgers: dict, h1: dict) -> None:
    checked = fired = 0
    bad: list[str] = []
    for (hypothesis, arm, task, seed), ledger in sorted(ledgers.items()):
        if hypothesis != "H1":
            continue
        drifts = [(r.get("turn"), r.get("primitive")) for r in ledger if r.get("kind") == "drift"]
        if arm == "frozen":
            if drifts:
                bad.append(f"frozen {task}/{seed} has drift records {drifts}")
            continue
        last_turn = max((r.get("turn", 0) for r in ledger if r.get("kind") == "turn"), default=0)
        entries = h1[f"task.{task}"][arm.replace("-", "_")]
        expected = sorted((e["turn"], e["primitive"]) for e in entries if e["turn"] <= last_turn)
        checked += 1
        fired += len(expected)
        stamps = {str(r.get("experimental") or "").lstrip("-") for r in ledger}
        if sorted(drifts) != expected or stamps != {"x-drift"}:
            bad.append(f"{arm} {task}/{seed}: drift {drifts} expected {expected} stamps {stamps}")
    c.check(
        "10.2",
        not bad and checked > 0,
        f"{checked} drift-arm ledgers carry exactly the scheduled drift records "
        f"({fired} injections at scheduled turns reached)" + (f"; {bad[0]}" if bad else ""),
    )


def check_one_primitive_ledgers(c: Clauses, ledgers: dict) -> None:
    turns_checked = second_calls = 0
    bad: list[str] = []
    for (hypothesis, arm, task, seed), ledger in sorted(ledgers.items()):
        if (hypothesis, arm) != ("H2", "one-primitive"):
            continue
        by_turn: dict[int, list[dict]] = {}
        for r in ledger:
            if r.get("kind") in CALL_KINDS:
                by_turn.setdefault(r["turn"], []).append(r)
        for turn, calls in by_turn.items():
            turns_checked += 1
            for later in calls[1:]:
                second_calls += 1
                outcome = later.get("outcome") or {}
                if outcome.get("status") != "error" or outcome.get("code") != "LIMIT":
                    bad.append(f"{task}/{seed} turn {turn}: later call {outcome}")
    c.check(
        "10.4",
        not bad and turns_checked > 0,
        f"{turns_checked} one-primitive turns: every one of {second_calls} later calls is LIMIT"
        + (f"; {bad[0]}" if bad else ""),
    )


# ---------------------------------------------------------------------------------------------
# Analysis and reports


def fresh_analysis(hypothesis: str) -> tuple[int, dict | None, str]:
    with tempfile.TemporaryDirectory(prefix="cwr-5_2-analysis-") as tmp:
        view = Path(tmp) / "view"
        view.mkdir()
        (view / hypothesis.lower()).symlink_to(RESULTS / hypothesis.lower())
        out = Path(tmp) / "analysis.json"
        proc = run(
            [
                sys.executable,
                "-I",
                str(REPO / "benchmark/analyse.py"),
                "--results",
                str(view),
                "--out",
                str(out),
            ]
        )
        data = json.loads(out.read_text(encoding="utf-8")) if out.exists() else None
    return proc.returncode, data, (proc.stderr or proc.stdout).strip()[-300:]


def fmt(value) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int):
        return str(value)
    return f"{value:.4f}"


def fmt_ci(ci) -> str:
    return "n/a" if not ci else f"[{fmt(ci[0])}, {fmt(ci[1])}]"


def table_rows(text: str) -> list[list[str]]:
    rows = []
    for line in text.splitlines():
        if line.startswith("| ") and not line.startswith("|---"):
            rows.append([cell.strip() for cell in line.strip().strip("|").split("|")])
    return rows


def cell_counts(hypothesis: str) -> dict[str, list[int]]:
    """Per arm: cells done, to retry (model or kernel error), missing, and seeds complete."""
    tasks = list(suite_tasks())
    out = {}
    for arm in ARMS[hypothesis]:
        done = retry = missing = 0
        seeds_done = {seed: True for seed in SEEDS}
        for task in tasks:
            for seed in SEEDS:
                cell = RESULTS / hypothesis.lower() / arm / task / str(seed)
                state = "missing"
                if (cell / "attempt.kvx").exists() and (cell / "ledger.jsonl").exists():
                    status = read_kvx(cell / "attempt.kvx")["attempt"].get("exit_status")
                    state = "retry" if status in ("model_error", "kernel_error") else "done"
                done += state == "done"
                retry += state == "retry"
                missing += state == "missing"
                seeds_done[seed] = seeds_done[seed] and state == "done"
        out[arm] = [done, retry, missing, sum(seeds_done.values())]
    return out


def check_report(c: Clauses, hypothesis: str, fresh: dict) -> None:
    path = REPORT[hypothesis]
    if not c.check("12.5", path.exists(), f"{path.relative_to(REPO)} exists"):
        return
    text = path.read_text(encoding="utf-8")
    body = fresh["hypotheses"][hypothesis]
    counts = cell_counts(hypothesis)
    compared = mismatched = 0
    problems: list[str] = []
    matrix = body["matrix"]
    summary = (
        f"Expected cells {matrix['expected']}; present {matrix['present']}; missing "
        f"{matrix['missing_count']}; complete {fmt(matrix['complete'])}. Excluded from analysis: "
        f"{len(fresh['excluded'])}."
    )
    compared += 1
    if summary not in text:
        mismatched += 1
        problems.append(f"matrix summary line differs from the fresh {summary!r}")
    for row in table_rows(text):
        if len(row) == 8 and row[0] in ARMS[hypothesis] and row[1] in BINS:
            arm, group, metric = row[0], row[1], row[2]
            cell = body["cells"][arm][group]
            m = cell["metrics"].get(metric)
            if m is None:
                problems.append(f"unknown metric {metric}")
                mismatched += 1
                continue
            want = [
                str(cell["instances"]),
                str(m["n"]),
                str(m["undefined"]),
                fmt(m["mean"]),
                fmt_ci(m["ci"]),
            ]
            compared += 1
            if row[3:] != want:
                mismatched += 1
                problems.append(f"{arm}/{group}/{metric}: report {row[3:]} fresh {want}")
        elif len(row) == 5 and row[0] in ARMS[hypothesis] and row[1] in BINS:
            t = body["harness_recorded_tokens"]["cells"][row[0]][row[1]]
            want = [str(t["n"]), fmt(t["mean"]), fmt_ci(t["ci"])]
            compared += 1
            if row[2:] != want:
                mismatched += 1
                problems.append(f"tokens {row[0]}/{row[1]}: report {row[2:]} fresh {want}")
        elif len(row) == 5 and row[0] in ARMS[hypothesis] and row[1].isdigit():
            want = [str(v) for v in counts[row[0]]]
            compared += 1
            if row[1:] != want:
                mismatched += 1
                problems.append(f"matrix {row[0]}: report {row[1:]} recomputed {want}")
        elif len(row) == 5 and row[0] in body["per_drift_spec"]:
            entry = body["per_drift_spec"][row[0]].get(row[1])
            want = (
                [
                    str(entry["instances"]),
                    fmt(entry["metrics"]["task_success"]["mean"]),
                    fmt(entry["metrics"]["repeated_failure_rate"]["mean"]),
                ]
                if entry
                else None
            )
            compared += 1
            if row[2:] != want:
                mismatched += 1
                problems.append(f"spec {row[0]}/{row[1]}: report {row[2:]} fresh {want}")
        elif len(row) == 6 and any(row[0] == d["id"] for d in body["directions"]):
            match = [d for d in body["directions"] if d["id"] == row[0] and d["group"] == row[1]]
            if not match:
                continue
            d = match[0]
            want = [d["expect"], fmt(d["estimate"]), fmt_ci(d["ci"]), d["label"]]
            compared += 1
            if row[2:] != want:
                mismatched += 1
                problems.append(f"{d['id']}/{d['group']}: report {row[2:]} fresh {want}")
    c.check(
        "12.5",
        compared > 0 and mismatched == 0,
        f"{path.relative_to(REPO)}: {compared} table rows equal the fresh recomputation"
        + (f"; {problems[0]}" if problems else ""),
    )


def check_analysis(c: Clauses, campaigns: dict, prereg: dict) -> None:
    current = sha256_file(METRICS)
    c.check(
        "12.5",
        prereg["digests"]["metrics_py"] == current
        and all(campaigns[h]["registration"]["metrics_py"] == current for h in ARMS),
        f"metrics.py {current} equals the registered digest",
    )
    first_start = None
    for hypothesis in ARMS:
        for attempt in sorted((RESULTS / hypothesis.lower()).glob("*/*/*/attempt.kvx")):
            start = read_kvx(attempt)["attempt"].get("start")
            if start and (first_start is None or start < first_start):
                first_start = start
    for hypothesis in ARMS:
        reg = campaigns[hypothesis]["registration"]
        registered = prereg[f"hypothesis.{hypothesis}"]
        c.check(
            "12.5",
            reg["directions"] == registered["directions"] and len(registered["directions"]) > 0,
            f"{hypothesis}: {len(reg['directions'])} directions equal the pre-registration",
        )
        c.check(
            "12.5",
            first_start is not None and reg["registered_at"] <= first_start,
            f"{hypothesis} registered at {reg['registered_at']}, first attempt {first_start}",
        )
        code, fresh, why = fresh_analysis(hypothesis)
        stored_path = RESULTS / hypothesis.lower() / "analysis.json"
        if not c.check("12.5", code == 0 and fresh is not None, f"analyse.py {hypothesis}: {why}"):
            continue
        stored = (
            json.loads(stored_path.read_text(encoding="utf-8")) if stored_path.exists() else None
        )
        c.check(
            "12.5",
            stored == fresh,
            f"{stored_path.relative_to(REPO)} equals a fresh analyse.py recomputation",
        )
        body = fresh["hypotheses"][hypothesis]
        complete = all(
            body["cells"][arm][group]["metrics"][registered["primary"]]["mean"] is not None
            and body["cells"][arm][group]["metrics"][registered["primary"]]["ci"] is not None
            for arm in ARMS[hypothesis]
            for group in BINS
        )
        c.check(
            "12.5",
            complete and fresh["analysis"]["confidence"] == 0.95,
            f"{hypothesis}: mean and 95% bootstrap CI of {registered['primary']} for every arm and bin",
        )
        check_report(c, hypothesis, fresh)


# ---------------------------------------------------------------------------------------------


def main() -> int:
    c = Clauses()
    build = run(["cargo", "build", "-q", "-p", "cwr-cli"])
    if build.returncode != 0:
        for clause in CLAUSES:
            c.check(clause, False, f"cargo build failed: {build.stderr.strip()[-300:]}")
        return c.report()
    binary = cwr_binary()
    missing = [str(p.relative_to(REPO)) for p in CAMPAIGN.values() if not p.exists()]
    if missing:
        for clause in CLAUSES:
            c.check(clause, False, f"campaign files missing: {', '.join(missing)}")
        return c.report()
    campaigns = {h: read_kvx(p) for h, p in CAMPAIGN.items()}
    prereg = read_kvx(PREREG)
    h1, h2 = campaigns["H1"], campaigns["H2"]
    BUILD_ROOTS[:0] = [REPO / root for root in h1["registration"].get("build_roots", [])]
    control = h1["registration"]["control_build"]
    good, why = verify_build(binary, control)
    c.check(
        "10.2",
        good and h2["registration"]["builds"]["script"] == control,
        f"control Build {control} verifies ({why or 'ok'}) and is the H1 and H2 control",
    )
    control_manual = (build_dir(control) / "manual.md").read_bytes() if good else b""
    check_drift_builds(c, binary, h1, control_manual)
    live_drift_probe(c, binary, h1)
    check_one_primitive(c, binary, h2)
    ledgers = check_matrix(c, campaigns, prereg)
    check_drift_ledgers(c, ledgers, h1)
    check_one_primitive_ledgers(c, ledgers)
    check_analysis(c, campaigns, prereg)
    return c.report()


if __name__ == "__main__":
    sys.exit(main())

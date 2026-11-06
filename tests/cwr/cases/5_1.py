#!/usr/bin/env python3
# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Task 5.1 driver: experiment harness isolation and ledger-only metrics.

Case CWR-017. Exercises clauses 8.5, 12.1, 12.3, 12.4 and 12.6 against the real implementation:
it builds the `cwr` binary, compiles the control Build of a copy of examples/refworld with the real
`cwr build` (and the --x-one-primitive Build when the compiler honours the flag), then acts as the
operator of a smoke campaign: two short tasks x two seeds x two arms, each Instance run by
benchmark/harness/run.py as a subprocess against the real kernel in replay mode with the
deterministic offline stub model, plus one labelled live-mode run whose turn is one real request to
the model gateway, with credentials read from the environment. benchmark/analyse.py then recomputes every metric from the
ledgers and states on disk, and the driver checks:

  12.1  no harness, campaign or prompt file references another agent or harness (the denylist lives
        here, outside the scanned tree), every arm in arms.kvx is a CWR Build with at most one
        experimental flag, and the runner refuses a Build whose flag is not the arm's;
  12.3  every ledger header records seed, model identity, arm and tail size; every replay Instance of
        the campaign shares model, sampling settings, cassette set and context layout, and the
        context of every turn starts with the Build manual byte for byte;
  12.4  the numbers the runner recorded per Instance equal analyse.py's independent recomputation
        from ledgers and states, the reported means equal the mean of those numbers, `analyse.py
        --check` passes on the fresh report and fails when one ledger record is altered, and the
        registered digests equal sha256 of the executed metrics.py and arms.kvx;
  8.5   the recomputation is unchanged when every non-ledger input (the runner's own numbers, fake
        sandbox stdout and adapter payload files, the whole kernel Instance store) is altered or
        removed, and metrics.py and analyse.py read no kernel internals or payloads;
  12.6  campaign runs are replay mode against one cassette set; the live run is labelled
        mode = "live", written under results/live, excluded from the replay report even when copied
        into the replay tree, and analysed only in a separately labelled report.

Prints one line per clause, `CLAUSE <req.ac> PASS|FAIL <evidence>`, and exits 0 only when every
clause passes. Honours CARGO_TARGET_DIR (default: the workspace target dir). Never prints a
credential value. Python 3.12, standard library only.
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import protocol

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parents[3]
CLAUSES = ["8.5", "12.1", "12.3", "12.4", "12.6"]
TASKS = ["t001-copy-readme", "t002-remove-old-log"]
SEEDS = [1, 2]
LIVE_SEED = 3
TIMEOUT = 900
CASSETTE_HEADER = (
    '{"format":"cwr.cassette","label":"%s","request_hash":"sha256-cwr-canonical-json-v1",'
    '"schema_version":1}\n'
)

# Named agent systems and harnesses. Any mention in a scanned file is a reference (requirement 12.1).
DENY_NAMES = [
    "langchain",
    "langgraph",
    "llamaindex",
    "llama-index",
    "autogpt",
    "auto-gpt",
    "babyagi",
    "crewai",
    "autogen",
    "metagpt",
    "swe-agent",
    "sweagent",
    "openhands",
    "opendevin",
    "devin",
    "aider",
    "gpt-engineer",
    "smolagents",
    "semantic kernel",
    "claude code",
    "codex cli",
    "copilot",
    "react agent",
    "reflexion",
    "toolformer",
    "assistants api",
    "agentbench",
    "webarena",
    "tau-bench",
    "terminal-bench",
]
# Generic comparisons. Matched only in sentences that do not negate them, so the spec's own
# statements ("there is no external baseline", "nothing ... compares against any other agent")
# are not references.
DENY_GENERIC = [
    r"\bbaselines?\b",
    r"\b(?:other|another|third[- ]party|external|competing)\s+(?:agents?|harness(?:es)?)\b",
    r"\b(?:compared?|comparison|versus|vs\.?)\s+(?:with|to|against)?\s*(?:an?|the)?\s*"
    r"(?:agents?|harness(?:es)?)\b",
]
# File names analyse.py may name: the per-cell run layout, its own reports and the registered inputs.
ALLOWED_INPUTS = {
    "ledger.jsonl",
    "final_state.kvx",
    "attempt.kvx",
    "success.py",
    "analysis.json",
    "analysis-live.json",
    "preregistration.kvx",
    "suite.kvx",
    "upgrades.kvx",
}
NEGATION = re.compile(r"\b(?:no|not|never|nothing|nor|without|none)\b|no_external", re.I)


class Results:
    def __init__(self) -> None:
        self.ok: dict[str, list[str]] = {c: [] for c in CLAUSES}
        self.bad: dict[str, list[str]] = {c: [] for c in CLAUSES}

    def check(self, clause: str, condition, evidence: str) -> bool:
        (self.ok if condition else self.bad)[clause].append(evidence)
        return bool(condition)


R = Results()
OBS = protocol.Observer("5.1", protocol.ok_bad(R))


def run(cmd, env: dict | None = None, timeout: int = TIMEOUT, **kw) -> subprocess.CompletedProcess:
    full = None if env is None else {**os.environ, **env}
    return subprocess.run(
        [str(c) for c in cmd],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=full,
        cwd=REPO,
        check=False,
        **kw,
    )


def target_dir() -> Path:
    env = os.environ.get("CARGO_TARGET_DIR")
    return Path(env) if env else REPO / "target"


def sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def harness():
    """The harness modules under test, imported from the repository."""
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from benchmark.harness import arms, ledgerio, model, run

    return arms, ledgerio, model, run


# ---------------------------------------------------------------------------------------------
# Setup: binary, Builds, cassettes


def build_binary() -> Path:
    env = dict(os.environ)
    env.setdefault("CARGO_TARGET_DIR", str(target_dir()))
    b = subprocess.run(
        ["cargo", "build", "-p", "cwr-cli"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        check=False,
    )
    if b.returncode != 0:
        print(b.stderr[-4000:], file=sys.stderr)
        raise SystemExit("cargo build -p cwr-cli failed")
    return target_dir() / "debug" / "cwr"


BUILT = re.compile(r"\bbuilt\b[^\n]*?(sha256:[0-9a-f]{64})[^\n]*? at (\S+)")


def compile_builds(cwr: Path, work: Path) -> tuple[str, str | None, Path | None, str]:
    """Operator step: compile the control Build and, if honoured, the --x-one-primitive Build.

    Returns the control hash, the experimental hash or None, the experimental Build root and a note.
    """
    source = work / "src" / "refworld"
    shutil.copytree(
        REPO / "examples" / "refworld", source, ignore=shutil.ignore_patterns("__pycache__")
    )
    built = run([cwr, "build", source, "--out", work / "build"])
    match = BUILT.search(built.stdout)
    if built.returncode != 0 or not match:
        print(built.stdout[-3000:] + built.stderr[-3000:], file=sys.stderr)
        raise SystemExit("cwr build of the refworld control Build failed")
    control = match.group(1)
    xbuilt = run([cwr, "build", source, "--out", work / "build", "--x-one-primitive"])
    xmatch = BUILT.search(xbuilt.stdout)
    if xbuilt.returncode != 0 or not xmatch:
        return control, None, None, f"--x-one-primitive build failed (rc={xbuilt.returncode})"
    xdir = Path(xmatch.group(2))
    manifest = (xdir / "manifest.kvx").read_text("utf-8")
    flagged = re.search(r'^experimental_flag = "-*x-one-primitive"$', manifest, re.M)
    if xmatch.group(1) == control or not flagged or xdir.parent.name != "xbuild":
        return control, None, None, "the compiler does not honour --x-one-primitive yet (task 3.2)"
    note = f"experimental Build {xmatch.group(1)} compiled with --x-one-primitive under xbuild/"
    return control, xmatch.group(1), xdir.parent, note


def cassette_set(work: Path) -> tuple[Path, str]:
    """The recorded cassette set when it covers the smoke cells, else header-only cassettes."""
    root = REPO / "benchmark" / "cassettes"
    needed = [f"{t}/seed-{s}.jsonl" for t in TASKS for s in SEEDS]
    for candidate in sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []:
        if all((candidate / n).is_file() for n in needed):
            return candidate, f"recorded cassette set {candidate.relative_to(REPO)}"
    own = work / "cassettes"
    for task in TASKS:
        for seed in SEEDS:
            path = own / task / f"seed-{seed}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(CASSETTE_HEADER % f"{task}/seed-{seed}", encoding="utf-8")
    return (
        own,
        "header-only cassettes (no recorded set covers the smoke cells; the tasks call no adapter)",
    )


# ---------------------------------------------------------------------------------------------
# Clause 12.1: no external references


def scan_targets(work: Path) -> list[Path]:
    bench = REPO / "benchmark"
    files = [
        p
        for p in sorted((bench / "harness").rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts
    ]
    if (bench / "campaigns").is_dir():
        files += sorted(p for p in (bench / "campaigns").rglob("*") if p.is_file())
    files += [bench / "analyse.py", bench / "preregistration.kvx"]
    files += sorted(bench.glob("tasks/*/task.md"))
    files += sorted(work.glob("build/*/manual.md")) + sorted(work.glob("xbuild/*/manual.md"))
    return files


def references(text: str) -> list[str]:
    hits = []
    lowered = text.lower()
    for name in DENY_NAMES:
        if re.search(r"(?<![a-z0-9])" + re.escape(name) + r"(?![a-z0-9])", lowered):
            hits.append(name)
    for sentence in re.split(r"(?<=[.;:!?])\s+|\n\s*\n|\n(?=\s*[-*#|])", text):
        if NEGATION.search(sentence):
            continue
        for pattern in DENY_GENERIC:
            match = re.search(pattern, sentence, re.I)
            if match:
                hits.append(match.group(0))
    return hits


def check_isolation(
    work: Path, run_mod, arms_mod, control: str, xbuild: str | None, xroot: Path | None
) -> None:
    files = scan_targets(work)
    found = {}
    for path in files:
        hits = references(path.read_text("utf-8", errors="replace"))
        if hits:
            found[str(path.relative_to(REPO) if path.is_relative_to(REPO) else path.name)] = hits
    prompts = sum(1 for p in files if p.name in ("task.md", "manual.md"))
    R.check(
        "12.1",
        not found and prompts >= 60,
        f"scanned {len(files)} harness, campaign and prompt files ({prompts} task.md and manual.md "
        f"prompts, plus run.TASK_TEMPLATE in run.py): references={found or 'none'}",
    )
    template = run_mod.TASK_TEMPLATE.format(instruction="x")
    R.check(
        "12.1",
        not references(template),
        "the harness prompt template run.TASK_TEMPLATE carries no reference",
    )
    trap = "We compare the arms with the AutoGPT harness as a baseline."
    R.check(
        "12.1",
        len(references(trap)) >= 2,
        f"negative control: an injected comparison sentence is flagged ({references(trap)})",
    )
    meta, arms = arms_mod.load_arms()
    flags = {a.flag for a in arms.values()}
    R.check(
        "12.1",
        meta["meta"].get("no_external_baseline") is True
        and len(arms) == 11
        and flags <= {None, *arms_mod.FLAGS}
        and all(
            a.flag is None or a.pillar_ablated == arms_mod.FLAGS[a.flag] for a in arms.values()
        ),
        f"arms.kvx: {len(arms)} arms, each the control Build or one CWR flag ablating its pillar "
        f"(flags {sorted(f for f in flags if f)})",
    )
    refused = run(
        [
            sys.executable,
            "-I",
            REPO / "benchmark/harness/run.py",
            "--hypothesis",
            "H2",
            "--arm",
            "one-primitive",
            "--task",
            TASKS[0],
            "--seed",
            "1",
            "--build",
            control,
            "--builds",
            work / "build",
            "--home",
            work / "home-refused",
            "--results",
            work / "refused",
            "--cassettes",
            work / "cassettes",
            "--model",
            "stub",
            "--allow-unregistered",
        ],
        env={"CWR_BIN": str(work / "cwr")},
    )
    R.check(
        "12.1",
        refused.returncode == 2
        and "needs '--x-one-primitive'" in refused.stderr
        and not (work / "refused").exists(),
        f"runner refuses the control Build for the one-primitive arm (rc={refused.returncode}) and "
        "writes nothing",
    )
    if xbuild:
        wrong = run(
            [
                sys.executable,
                "-I",
                REPO / "benchmark/harness/run.py",
                "--hypothesis",
                "H2",
                "--arm",
                "script",
                "--task",
                TASKS[0],
                "--seed",
                "1",
                "--build",
                xbuild,
                "--builds",
                xroot,
                "--home",
                work / "home-refused",
                "--results",
                work / "refused",
                "--cassettes",
                work / "cassettes",
                "--model",
                "stub",
                "--allow-unregistered",
            ],
            env={"CWR_BIN": str(work / "cwr")},
        )
        R.check(
            "12.1",
            wrong.returncode == 2,
            f"runner refuses the experimental Build for the control arm (rc={wrong.returncode})",
        )


# ---------------------------------------------------------------------------------------------
# Campaign


def run_cell(work: Path, arm: tuple[str, str, str, Path], task: str, seed: int, live: bool):
    hypothesis, key, build, roots = arm
    cmd = [
        sys.executable,
        "-I",
        REPO / "benchmark/harness/run.py",
        "--hypothesis",
        hypothesis,
        "--arm",
        key,
        "--task",
        task,
        "--seed",
        str(seed),
        "--build",
        build,
        "--builds",
        roots,
        "--home",
        work / "home",
        "--results",
        work / "results",
        "--allow-unregistered",
    ]
    if live:
        cmd += ["--live", "--model", "gateway", "--turn-limit", "1", "--max-tokens", "2048"]
    else:
        cmd += ["--cassettes", work / "cassettes", "--model", "stub"]
    return run(cmd, env={"CWR_BIN": str(work / "cwr")})


def cell_path(work: Path, hypothesis: str, key: str, task: str, seed: int, live=False) -> Path:
    root = work / "results" / ("live" if live else "")
    return root / hypothesis.lower() / key / task / str(seed)


def analyse(work: Path, *extra) -> subprocess.CompletedProcess:
    return run([sys.executable, "-I", REPO / "benchmark/analyse.py", "--draft-ok", *extra])


def close(a, b) -> bool:
    if a is None or b is None:
        return a is b
    if isinstance(a, str) or isinstance(b, str):
        return a == b
    return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-9)


def kvx(path: Path) -> dict:
    _, ledgerio, _, _ = harness()
    return ledgerio.read_kvx(path)


# ---------------------------------------------------------------------------------------------
# Main


def main() -> int:
    arms_mod, _, model_mod, run_mod = harness()
    work = Path(tempfile.mkdtemp(prefix="cwr-5_1-"))
    try:
        cwr = build_binary()
        shutil.copy2(cwr, work / "cwr")
        control, xbuild, xroot, xnote = compile_builds(work / "cwr", work)
        cassettes, cnote = cassette_set(work)
        if cassettes != work / "cassettes":
            shutil.copytree(cassettes, work / "cassettes")
        if xbuild:
            arms = [
                ("H2", "script", control, work / "build"),
                ("H2", "one-primitive", xbuild, xroot),
            ]
        else:
            arms = [("H3", "k20", control, work / "build"), ("H3", "k5", control, work / "build")]
        print(
            f"setup: control {control}; {xnote}; arms {[a[0] + '/' + a[1] for a in arms]}; {cnote}"
        )

        check_isolation(work, run_mod, arms_mod, control, xbuild, xroot)

        # Smoke campaign: tasks x seeds x arms in replay mode with the deterministic stub model.
        cells = []
        for arm in arms:
            for task in TASKS:
                for seed in SEEDS:
                    proc = run_cell(work, arm, task, seed, live=False)
                    cells.append((arm, task, seed, proc))
        ran = [c for c in cells if c[3].returncode == 0]
        R.check(
            "12.6",
            len(ran) == len(cells),
            f"{len(ran)}/{len(cells)} replay cells ran through run.py against the real kernel: "
            + "; ".join(c[3].stdout.strip() or c[3].stderr.strip()[-200:] for c in cells),
        )
        # One labelled live-mode run: one real request to the model gateway.
        live_arm = ("H2", "script", control, work / "build")
        live = run_cell(work, live_arm, TASKS[0], LIVE_SEED, live=True)
        live_dir = cell_path(work, "H2", "script", TASKS[0], LIVE_SEED, live=True)

        # Clause 12.3 and 12.6 from ledgers and attempt records.
        headers_ok, attempts = [], []
        for (hyp, key, build, _), task, seed, proc in cells:
            d = cell_path(work, hyp, key, task, seed)
            if proc.returncode != 0 or not (d / "ledger.jsonl").is_file():
                headers_ok.append(False)
                continue
            head = read_jsonl(d / "ledger.jsonl")[0]
            attempt = kvx(d / "attempt.kvx")
            attempts.append((hyp, key, task, seed, d, attempt))
            arm_obj = arms_mod.load_arms()[1][(hyp, key)]
            headers_ok.append(
                head.get("kind") == "header"
                and head.get("seed") == seed
                and head.get("arm") == f"{hyp}/{key}"
                and head.get("model") == model_mod.STUB_MODEL
                and head.get("tail_size") == arm_obj.tail
                and head.get("build") == build
            )
        stamps = {}
        for hyp, key, _, _, d, _ in attempts:
            stamp = arms_mod.load_arms()[1][(hyp, key)].experimental_stamp
            seen = {str(r.get("experimental")).lstrip("-") for r in read_jsonl(d / "ledger.jsonl")}
            stamps.setdefault(f"{hyp}/{key}", set()).update(seen)
            stamps[f"{hyp}/{key}"].add("ok" if seen == {str(stamp)} else "MISMATCH")
        R.check(
            "12.1",
            attempts and not any("MISMATCH" in v for v in stamps.values()),
            "every ledger record of each arm carries exactly the arm's experimental stamp: "
            + "; ".join(f"{k}={sorted(v - {'ok'})}" for k, v in sorted(stamps.items())),
        )
        R.check(
            "12.3",
            headers_ok and all(headers_ok),
            f"{sum(headers_ok)}/{len(headers_ok)} ledger headers record seed, model identity, arm, "
            "tail size K and the arm's Build hash",
        )
        same = {
            k: sorted({json.dumps(a["attempt"].get(k), sort_keys=True) for *_, a in attempts})
            for k in (
                "model",
                "sampling",
                "cassette_digest",
                "context_source",
                "max_tokens_per_turn",
            )
        }
        R.check(
            "12.3",
            attempts and all(len(v) == 1 for v in same.values()),
            "every replay Instance shares model, sampling, cassette set, context assembly and token "
            f"limit across arms: {same}",
        )
        prereg = kvx(REPO / "benchmark" / "preregistration.kvx")
        R.check(
            "12.3",
            prereg["campaign"]["seeds_per_task"] == 10 and len(prereg["campaign"]["seeds"]) == 10,
            "the registration fixes 10 seeds per task per arm "
            f"({prereg['campaign']['seeds']}); this smoke runs {len(SEEDS)} of them, a campaign "
            "runs all 10 (tasks 5.2, 5.3)",
        )
        check_context_layout(work, run_mod, model_mod, control)

        set_digest = run_mod.cassette_set_digest(work / "cassettes")
        R.check(
            "12.6",
            attempts
            and all(
                a["attempt"]["mode"] == "replay"
                and a["attempt"]["cassette_digest"] == set_digest
                and Path(a["attempt"]["cassette"]).parent.parent == work / "cassettes"
                for *_, a in attempts
            ),
            f"every campaign attempt is mode replay against cassette set {set_digest}",
        )
        no_cassette = run(
            [
                sys.executable,
                "-I",
                REPO / "benchmark/harness/run.py",
                "--hypothesis",
                arms[0][0],
                "--arm",
                arms[0][1],
                "--task",
                TASKS[0],
                "--seed",
                "1",
                "--build",
                control,
                "--builds",
                work / "build",
                "--home",
                work / "home",
                "--results",
                work / "nocassette",
                "--model",
                "stub",
                "--allow-unregistered",
            ],
            env={"CWR_BIN": str(work / "cwr")},
        )
        R.check(
            "12.6",
            no_cassette.returncode == 2 and not (work / "nocassette").exists(),
            f"a replay run without a cassette set is refused (rc={no_cassette.returncode})",
        )
        live_attempt = kvx(live_dir / "attempt.kvx")["attempt"] if live_dir.is_dir() else {}
        live_head = read_jsonl(live_dir / "ledger.jsonl")[0] if live_dir.is_dir() else {}
        env_model = model_mod.read_env().get("MODEL") if (REPO / ".env").exists() else None
        R.check(
            "12.6",
            live.returncode == 0
            and live_attempt.get("mode") == "live"
            and live_attempt.get("exit_status") not in ("model_error", "kernel_error")
            and live_attempt.get("turns") == 1,
            f"live-mode run (one real gateway request) rc={live.returncode} labelled "
            f"mode={live_attempt.get('mode')!r} under results/live, exit "
            f"{live_attempt.get('exit_status')!r}, harness-recorded tokens "
            f"{live_attempt.get('tokens_input_total')}+{live_attempt.get('tokens_output_total')}",
        )
        R.check(
            "12.3",
            bool(env_model) and live_head.get("model") == env_model,
            "the live run's ledger header records the configured gateway model identity "
            f"(match={bool(env_model) and live_head.get('model') == env_model})",
        )

        # Analysis: independent recomputation from ledgers.
        report_path = work / "analysis.json"
        a1 = analyse(work, "--results", work / "results", "--out", report_path)
        report = json.loads(report_path.read_text()) if a1.returncode == 0 else {}
        with OBS.observe("CWR-017.a2"):
            check_metrics(work, report, attempts, a1, report_path)
        with OBS.observe("CWR-017.a2"):
            check_ledger_only(work, report_path, attempts)
        with OBS.observe("CWR-017.a3"):
            check_live_exclusion(work, report, live_dir, report_path)
    finally:
        if os.environ.get("CWR_KEEP_WORK"):
            print(f"work directory kept: {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)

    OBS.claim("CWR-017.a1", clause="12.1")
    OBS.emit()
    failed = 0
    for clause in CLAUSES:
        ok, bad = R.ok[clause], R.bad[clause]
        if bad or not ok:
            failed += 1
            print(f"CLAUSE {clause} FAIL " + " | ".join(bad or ["no evidence"]))
        else:
            print(f"CLAUSE {clause} PASS " + " | ".join(ok))
    return 1 if failed else 0


def check_context_layout(work: Path, run_mod, model_mod, control: str) -> None:
    """Run two Instances in process and inspect every context the model received (clause 12.3)."""

    class Recording:
        def __init__(self, inner):
            self.inner, self.model, self.contexts = inner, inner.model, []

        def complete_context(self, context, *, max_tokens, params=None):
            self.contexts.append(context)
            return self.inner.complete_context(context, max_tokens=max_tokens, params=params)

    arms_mod, *_ = harness()
    arm = arms_mod.load_arms()[1][("H3", "k5")]
    task = TASKS[1]
    manual = (work / "build" / control[7:] / "manual.md").read_text("utf-8")
    instruction = (REPO / "benchmark" / "tasks" / task / "task.md").read_text("utf-8")
    contexts, refusals, envelopes = [], [], []
    for seed in SEEDS:
        kernel = run_mod.CwrCliKernel(
            home=work / "home-context", builds=[work / "build"], binary=work / "cwr"
        )
        recorder = Recording(model_mod.ReferenceStubModel.for_task(task, seed=seed))
        spec = run_mod.EpisodeSpec(
            hypothesis="H3",
            arm=arm,
            task=task,
            seed=seed,
            build=control,
            predicate_path=REPO / "benchmark" / "tasks" / task / "success.py",
            initial_state=REPO / "benchmark" / "tasks" / task / "initial.kvx",
            turn_limit=4,
            results_root=work / "results-context",
            cassette=work / "cassettes" / task / f"seed-{seed}.jsonl",
            instruction=instruction,
            registered=False,
        )
        run_mod.run_episode(spec, kernel, recorder)
        contexts += recorder.contexts
        envelopes += [
            (k > 0) == (model_mod.envelope_from_context(c) is not None)
            for k, c in enumerate(recorder.contexts)
        ]
        refusals.append(kernel.surface())
    task_text = run_mod.TASK_TEMPLATE.format(instruction=instruction)
    R.check(
        "12.3",
        len(contexts) >= 3
        and all(c.startswith(manual) and c.endswith(task_text) for c in contexts)
        and all(envelopes),
        f"{len(contexts)} contexts over 2 Instances: each starts with the Build manual byte for byte "
        f"({len(manual)} bytes), then the ledger tail and last envelope, and ends with task.md under "
        f"the fixed harness template; assembly={refusals[0]['context']}",
    )


def check_metrics(work: Path, report: dict, attempts: list, a1, report_path: Path) -> None:
    _, ledgerio, _, _ = harness()
    from benchmark.harness import metrics

    R.check("12.4", a1.returncode == 0, f"analyse.py over the smoke results rc={a1.returncode}")
    if not report:
        return
    hyp = attempts[0][0] if attempts else "H2"
    instances = {i["cell"]: i for i in report["hypotheses"][hyp]["instances"]}
    mismatches, compared = [], 0
    for h, key, task, seed, _, attempt in attempts:
        cell = f"{h.lower()}/{key}/{task}/{seed}"
        harness_values = attempt.get("metrics", {}).get("values", {})
        recomputed = instances.get(cell, {}).get("values", {})
        for name in metrics.metric_names():
            compared += 1
            if not close(harness_values.get(name), recomputed.get(name)):
                mismatches.append(f"{cell}:{name}")
    R.check(
        "12.4",
        attempts and not mismatches and len(instances) == len(attempts),
        f"{compared} per-Instance values the runner recorded equal analyse.py's recomputation from "
        f"ledgers over {len(instances)} Instances (mismatches={mismatches[:5] or 'none'})",
    )
    bad_means, means = [], 0
    for arm_key, groups in report["hypotheses"][hyp]["cells"].items():
        for group, body in groups.items():
            members = [a for a in attempts if a[1] == arm_key]
            if not body["instances"]:
                continue
            for name in metrics.metric_names():
                values = [
                    a[5]["metrics"]["values"][name]
                    for a in members
                    if a[5]["metrics"]["values"].get(name) is not None
                ]
                expected = sum(values) / len(values) if values else None
                means += 1
                if not close(body["metrics"][name]["mean"], expected):
                    bad_means.append(f"{arm_key}/{group}/{name}")
    R.check(
        "12.4",
        means > 0 and not bad_means,
        f"{means} reported means (with 95% bootstrap intervals) equal the mean of the runner's "
        f"per-Instance numbers (mismatches={bad_means[:5] or 'none'})",
    )
    present = set(metrics.metric_names())
    needed = {
        "task_success",
        "turns_to_success",
        "primitive_calls",
        "repeated_failure_rate",
        "rule_reuse",
        "post_upgrade_misuse_rate",
    } | {f"failure_rate.{c}" for c in metrics.ENVELOPE}
    sample = next(iter(instances.values()))["values"] if instances else {}
    R.check(
        "12.4",
        needed <= present and sample.get("turns_to_success_method") == "per_turn_state",
        "the seven 12.4 metrics (task success, turns to success, primitive calls, failure rate by "
        "each of 11 codes, repeated-failure rate, rule reuse, post-upgrade misuse) are computed; "
        "turns to success replays the per-turn states",
    )
    check = analyse(work, "--results", work / "results", "--check", report_path)
    tampered = work / "tampered"
    shutil.copytree(work / "results", tampered)
    target = None
    for _, _, _, _, d, _ in attempts:
        ledger = d.relative_to(work / "results") / "ledger.jsonl"
        records = read_jsonl(tampered / ledger)
        for record in records:
            if record.get("kind") == "primitive" and record["outcome"]["status"] == "ok":
                record["outcome"] = {**record["outcome"], "status": "error", "code": "LOCKED"}
                target = ledger
                break
        if target:
            ledgerio.write_ledger(tampered / ledger, records)
            break
    recheck = analyse(work, "--results", tampered, "--check", report_path)
    R.check(
        "12.4",
        check.returncode == 0 and target is not None and recheck.returncode == 1,
        f"analyse.py --check equals the fresh report (rc={check.returncode}) and differs once one "
        f"call record in {target} is altered (rc={recheck.returncode}: "
        f"{recheck.stderr.strip()[-120:]})",
    )
    prereg = kvx(REPO / "benchmark" / "preregistration.kvx")["digests"]
    metrics_py = REPO / "benchmark" / "harness" / "metrics.py"
    arms_kvx = REPO / "benchmark" / "harness" / "arms.kvx"
    changed = work / "metrics_changed.py"
    changed.write_text(metrics_py.read_text("utf-8") + "\nREUSE_GAP = 11\n", encoding="utf-8")
    refused = analyse(work, "--results", work / "results", "--metrics", changed)
    R.check(
        "12.4",
        prereg["metrics_py"] == sha256_file(metrics_py)
        and prereg["arms_kvx"] == sha256_file(arms_kvx)
        and refused.returncode == 3,
        "preregistration.kvx digests equal sha256 of the executed metrics.py and arms.kvx; a changed "
        f"metric definition is refused (rc={refused.returncode})",
    )


def check_ledger_only(work: Path, report_path: Path, attempts: list) -> None:
    """Clause 8.5: only ledgers and final state feed the metrics."""
    stripped = work / "stripped"
    shutil.copytree(work / "results", stripped)
    for _, _, _, _, d, _ in attempts:
        cell = stripped / d.relative_to(work / "results")
        text = (cell / "attempt.kvx").read_text("utf-8")
        text = re.sub(r"^values = .*$", 'values = {"task_success": 999}', text, flags=re.M)
        (cell / "attempt.kvx").write_text(text, encoding="utf-8")
        (cell / "sandbox_stdout.txt").write_text("task_success = 0\nturns 99\n", encoding="utf-8")
        (cell / "adapter_payloads.jsonl").write_text('{"body": "x"}\n', encoding="utf-8")
    for home in work.glob("home*"):
        shutil.rmtree(home)
    same = analyse(work, "--results", stripped, "--check", report_path)
    R.check(
        "8.5",
        same.returncode == 0 and not list(work.glob("home*")),
        "with the runner's own numbers overwritten, fake sandbox stdout and adapter payload files "
        "added and every kernel Instance store deleted, analyse.py reproduces the report exactly "
        f"(rc={same.returncode})",
    )
    sources = {
        "metrics.py": REPO / "benchmark" / "harness" / "metrics.py",
        "analyse.py": REPO / "benchmark" / "analyse.py",
    }
    imports = set()
    tree = ast.parse(sources["metrics.py"].read_text("utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imports.add(node.module or "")
        elif isinstance(node, ast.Call) and getattr(node.func, "id", None) in ("open", "eval"):
            imports.add("open()")
    forbidden = ("instance.json", "last_envelope", "idempotency.jsonl", "stdout.txt", "/objects")
    constants = set()
    for node in ast.walk(ast.parse(sources["analyse.py"].read_text("utf-8"))):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            constants.add(node.value)
    leaks = sorted(c for c in constants for f in forbidden if f in c)
    files = sorted(c for c in constants if re.fullmatch(r"[\w./-]+\.(?:jsonl|json|kvx|txt)", c))
    R.check(
        "8.5",
        imports <= {"__future__", "collections.abc", "typing"}
        and not leaks
        and set(files) <= ALLOWED_INPUTS,
        f"metrics.py imports only {sorted(imports)} and does no I/O; analyse.py names only "
        f"{files} as per-cell inputs and no kernel internal ({leaks or 'none'})",
    )


def check_live_exclusion(work: Path, report: dict, live_dir: Path, report_path: Path) -> None:
    if not report:
        R.check("12.6", False, "no replay report to check live exclusion against")
        return
    cells = [i["cell"] for h in report["hypotheses"].values() for i in h["instances"]]
    R.check(
        "12.6",
        live_dir.is_dir()
        and str(live_dir).startswith(str(work / "results" / "live"))
        and not any(c.endswith(f"/{LIVE_SEED}") for c in cells)
        and report["mode"] == "replay",
        f"the replay report pools {len(cells)} Instances, none from results/live",
    )
    pooled = work / "pooled"
    shutil.copytree(work / "results", pooled, ignore=shutil.ignore_patterns("live"))
    rel = live_dir.relative_to(work / "results" / "live")
    shutil.copytree(live_dir, pooled / rel)
    trap = analyse(work, "--results", pooled, "--out", work / "pooled.json")
    trapped = json.loads((work / "pooled.json").read_text()) if trap.returncode == 0 else {}
    excluded = trapped.get("excluded", [])
    same_cells = trapped and all(
        trapped["hypotheses"][h]["cells"] == report["hypotheses"][h]["cells"]
        for h in report["hypotheses"]
    )
    R.check(
        "12.6",
        trap.returncode == 0
        and any(
            e["cell"] == str(rel) and "mode 'live' is not 'replay'" in e["reason"] for e in excluded
        )
        and same_cells,
        f"a live cell copied into the replay tree is excluded ({excluded}) and every replay "
        "statistic is unchanged",
    )
    live = analyse(work, "--results", work / "results", "--live", "--out", work / "live.json")
    live_report = json.loads((work / "live.json").read_text()) if live.returncode == 0 else {}
    live_cells = [
        i["cell"] for h in live_report.get("hypotheses", {}).values() for i in h["instances"]
    ]
    R.check(
        "12.6",
        live.returncode == 0 and live_report.get("mode") == "live" and live_cells == [str(rel)],
        f"analyse.py --live writes a separately labelled report (mode={live_report.get('mode')!r}) "
        f"holding only the live Instance {live_cells}",
    )


if __name__ == "__main__":
    sys.exit(main())

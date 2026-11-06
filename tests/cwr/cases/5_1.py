#!/usr/bin/env python3
# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Task 5.1 driver: experiment harness isolation and ledger-only metrics.

Case CWR-017. Exercises clauses 8.5, 12.1, 12.3, 12.4 and 12.6 against the real implementation:
it builds the `cwr` binary, compiles the control Build of a copy of examples/refworld with the real
`cwr build` (and the --x-one-primitive Build when the compiler honours the flag), records a cassette
set for the smoke tasks through the real kernel gateway against the refworld stub servers
(benchmark/stubs/record_refworld.py), then acts as the operator of a smoke campaign: two short tasks
(one file-only, one that calls the search adapter) x all ten registered seeds x two arms, each
Instance run by benchmark/harness/run.py as a subprocess against the real kernel in replay mode with
the deterministic offline stub model, plus one labelled live-mode run whose turn is one real request
to the model gateway, with credentials read from the process environment. benchmark/analyse.py then
recomputes every metric from the ledgers and final states on disk, replaying state per turn from the
ledger through the kernel for turns to success, and the driver checks:

  12.1  no harness, campaign or prompt file references another agent or harness (the denylist lives
        here, outside the scanned tree), every arm in arms.kvx is a CWR Build with at most one
        experimental flag, and the runner refuses a Build whose flag is not the arm's;
  12.3  every ledger header records seed, model identity, arm, tail size, Build hash and the cassette
        set digest; every arm ran each task with the ten registered seeds; every replay Instance of
        the campaign shares model, sampling settings, cassette set and context layout, and the
        context of every turn starts with the Build manual byte for byte;
  12.4  the numbers the runner recorded per Instance equal analyse.py's independent recomputation
        from ledgers and final state, turns to success comes from state replayed per turn from the
        ledger, the reported means equal the mean of those numbers, `analyse.py --check` passes on
        the fresh report and fails when one ledger record is altered, and the registered digests
        equal sha256 of the executed metrics.py and arms.kvx;
  8.5   the recomputation is unchanged when every non-ledger input (the runner's own numbers, fake
        per-turn state snapshots, fake sandbox stdout and adapter payload files, the whole kernel
        Instance store) is altered or removed; a recorded turn message altered so that it would
        change a state is refused because the replay no longer reproduces the ledger; an audit hook
        on the analyse.py and run.py processes shows that the only Instance file either opens is a
        ledger and that analyse.py opens no results file other than ledgers, final states, attempt
        records and the recorded turn messages; metrics.py does no I/O;
  12.6  campaign runs are replay mode against one cassette set; the live run is labelled
        mode = "live", written under results/live, excluded from the replay report even when copied
        into the replay tree, and analysed only in a separately labelled report.

Prints one line per clause, `CLAUSE <req.ac> PASS|FAIL <evidence>`, and exits 0 only when every
clause passes. Honours CARGO_TARGET_DIR (default: the workspace target dir). Python 3.12, standard
library only.

Secrets: the live run needs the gateway variables in the process environment, which
benchmark/harness/model.py reads at runtime; the operator exports them before running the driver
(for example `set -a; . ./.env; set +a`). This file never reads a credentials file and never
prints, writes or forwards a credential value; without the variables the live clause fails.
"""

from __future__ import annotations

import ast
import concurrent.futures
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
# One file-only task and one task whose reference turns call the search adapter, so the campaign
# replays recorded adapter traffic.
TASKS = ["t001-copy-readme", "t003-tennis-note"]
SEEDS = list(range(1, 11))
# The live run uses a task outside the replay smoke so its cell path never collides with one.
LIVE_TASK = "t002-remove-old-log"
LIVE_SEED = 1
# The stub-model live cell that is copied and relabelled into the replay tree (clause 12.6).
STUB_LIVE_SEED = 2
# Smoke registrations written by main(): "replay" and "live" paths and whether they are drafts.
SMOKE: dict = {}
TIMEOUT = 900
WORKERS = 4
# Results files analyse.py may open per cell, and the replay input it re-submits.
CELL_INPUTS = {"ledger.jsonl", "final_state.kvx", "attempt.kvx"}
MESSAGE = re.compile(r"messages/turn_\d{4}\.md$")
# Audit hook wrapper: runs a script as __main__ and logs every file open and directory listing.
TRACE = """
import json, os, runpy, sys, threading
log = open(sys.argv[1], "w", encoding="utf-8")
lock = threading.Lock()
def note(kind, path):
    with lock:
        log.write(json.dumps([kind, os.path.abspath(os.fsdecode(path))]) + "\\n")
        log.flush()
def hook(event, args):
    if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
        note("open", args[0])
    elif event in ("os.listdir", "os.scandir") and args and args[0] is not None:
        if isinstance(args[0], (str, bytes, os.PathLike)):
            note("list", args[0])
sys.addaudithook(hook)
sys.argv = sys.argv[2:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""

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


def cassette_set(cwr: Path, work: Path, control: str) -> tuple[Path, str]:
    """The recorded cassette set the smoke replays, at <work>/cassettes.

    The registered frozen set (benchmark/cassettes/suite, recorded by task 4.1) is used when its
    digest equals the registered one. Otherwise the operator records the smoke tasks' cassettes
    through the real kernel gateway: benchmark/stubs/record_refworld.py runs every reference turn
    of each task once per registered seed with `cwr instance turn --gateway record` against the
    refworld stub servers. The campaign then replays the set and never opens a socket.
    """
    _, ledgerio, _, run_mod = harness()
    frozen = REPO / "benchmark" / "cassettes" / "suite"
    registered = ledgerio.read_kvx(REPO / "benchmark" / "preregistration.kvx")["campaign"].get(
        "cassette_digest"
    )
    # A snapshot copy, so that a concurrent re-recording of the repository set cannot change it.
    snapshot = work / "frozen-snapshot"
    try:
        shutil.copytree(frozen, snapshot, ignore=shutil.ignore_patterns("*.expect.json"))
    except (OSError, shutil.Error):
        shutil.rmtree(snapshot, ignore_errors=True)
    covered = all((snapshot / t / f"seed-{s}.jsonl").is_file() for t in TASKS for s in SEEDS)
    if covered and registered and run_mod.cassette_set_digest(snapshot) == registered:
        snapshot.rename(work / "cassettes")
        return work / "cassettes", f"registered frozen cassette set {registered}"
    shutil.rmtree(snapshot, ignore_errors=True)
    recorded = work / "recorded"
    proc = run(
        [
            sys.executable,
            "-I",
            REPO / "benchmark/stubs/record_refworld.py",
            "record",
            "--set",
            "suite",
            "--tasks",
            ",".join(TASKS),
            "--out",
            recorded,
            "--cwr",
            cwr,
            "--build",
            work / "build" / control.removeprefix("sha256:"),
        ]
    )
    suite = recorded / "suite"
    needed = [suite / t / f"seed-{s}.jsonl" for t in TASKS for s in SEEDS]
    if proc.returncode != 0 or not all(p.is_file() for p in needed):
        print(proc.stdout[-2000:] + proc.stderr[-2000:], file=sys.stderr)
        raise SystemExit("recording the smoke cassette set failed")
    shutil.copytree(suite, work / "cassettes")
    adapter_entries = sum(
        1
        for task in TASKS
        for seed in SEEDS
        for line in (suite / task / f"seed-{seed}.jsonl").read_text("utf-8").splitlines()[1:]
        if line.strip()
    )
    return (
        work / "cassettes",
        f"cassette set recorded through the kernel gateway against the stubs: "
        f"{len(needed)} cassettes, {adapter_entries} recorded adapter exchanges",
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


def traced(work: Path, log: Path | None) -> list:
    """Interpreter prefix: plain `python3 -I`, or the audit-hook wrapper logging to `log`."""
    if log is None:
        return [sys.executable, "-I"]
    wrapper = work / "trace.py"
    if not wrapper.exists():
        wrapper.write_text(TRACE, encoding="utf-8")
    return [sys.executable, "-I", wrapper, log]


def gateway_model() -> str | None:
    """The configured gateway model identity, or None when the gateway variables are not exported.

    Reads this process's environment only, through model.GatewayClient.from_env; run.py inherits
    the same environment. No credentials file is read and no value is printed or written.
    """
    _, _, model_mod, _ = harness()
    try:
        return model_mod.GatewayClient.from_env().model
    except model_mod.ModelError:
        return None


def run_cell(
    work: Path,
    arm: tuple[str, str, str, Path],
    task: str,
    seed: int,
    live: bool,
    trace: Path | None = None,
):
    hypothesis, key, build, roots = arm
    cmd = [
        *traced(work, trace),
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
        work / "home" / f"{hypothesis}-{key}-{task}-{seed}{'-live' if live else ''}",
        "--results",
        work / "results",
        "--prereg",
        SMOKE["live" if live else "replay"],
        *(["--allow-unregistered"] if SMOKE["draft"] else []),
    ]
    env = {"CWR_BIN": str(work / "cwr")}
    if live:
        cmd += ["--live", "--model", "gateway", "--turn-limit", "1", "--max-tokens", "2048"]
    else:
        cmd += ["--cassettes", work / "cassettes", "--model", "stub"]
    return run(cmd, env=env)


def cell_path(work: Path, hypothesis: str, key: str, task: str, seed: int, live=False) -> Path:
    root = work / "results" / ("live" if live else "")
    return root / hypothesis.lower() / key / task / str(seed)


def analyse(
    work: Path, *extra, trace: Path | None = None, live: bool = False
) -> subprocess.CompletedProcess:
    """benchmark/analyse.py under the smoke registration, with the driver's cwr for replays."""
    return run(
        [
            *traced(work, trace),
            REPO / "benchmark/analyse.py",
            "--draft-ok",
            "--prereg",
            SMOKE["live" if live else "replay"],
            "--cwr",
            work / "cwr",
            *extra,
        ]
    )


def set_kvx(text: str, section: str, key: str, value) -> str:
    """Replace `key = ...` inside `[section]` of KVX text with the JSON literal of value."""
    lines, inside = text.split("\n"), False
    for i, line in enumerate(lines):
        if re.fullmatch(r"\[[A-Za-z0-9_.-]+\]", line.strip()):
            inside = line.strip() == f"[{section}]"
        elif inside and line.startswith(f"{key} = "):
            lines[i] = f"{key} = {json.dumps(value)}"
            return "\n".join(lines)
    raise SystemExit(f"preregistration.kvx has no [{section}] {key}")


def smoke_registrations(work: Path, cassette_digest: str, stub_model: str, live_model: str) -> str:
    """The smoke's registrations: the committed one with the model identity, cassette set and task
    suite digest the smoke actually uses. The metrics.py and arms.kvx digests, seeds, sampling,
    context layout and limits stay as registered."""
    harness()
    from benchmark.harness import arms as arms_mod
    from benchmark.harness import prereg as prereg_mod

    base = (REPO / "benchmark" / "preregistration.kvx").read_text("utf-8")
    replay = set_kvx(base, "campaign", "cassette_digest", cassette_digest)
    predicates = str(REPO / "benchmark" / "predicates")
    if predicates not in sys.path:
        sys.path.insert(0, predicates)
    import common

    suite = common.suite_digest(REPO / "benchmark" / "tasks")
    replay = set_kvx(replay, "campaign", "suite_digest", suite)
    for name, model in (("replay", stub_model), ("live", live_model)):
        path = work / f"preregistration-{name}.kvx"
        path.write_text(set_kvx(replay, "campaign", "model", model), encoding="utf-8")
        SMOKE[name] = path
    meta, arms = arms_mod.load_arms()
    missing = prereg_mod.missing_fields(
        prereg_mod.load_prereg(SMOKE["replay"]), arms, meta["control"]
    )
    SMOKE["draft"] = bool(missing)
    if missing:
        return "smoke registration is a draft (missing " + ", ".join(missing) + ")"
    return "smoke registration complete: the committed registration with the stub model identity"


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
        _, cnote = cassette_set(work / "cwr", work, control)
        env_model = gateway_model()
        cnote += "; " + smoke_registrations(
            work,
            run_mod.cassette_set_digest(work / "cassettes"),
            model_mod.STUB_MODEL,
            env_model or "",
        )
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
        # The first cell runs under the audit hook (clause 8.5); the others run in parallel.
        plan = [(arm, task, seed) for arm in arms for task in TASKS for seed in SEEDS]
        first = plan[0]
        cells = [(*first, run_cell(work, *first, live=False, trace=work / "trace-run.jsonl"))]
        with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
            procs = pool.map(lambda c: run_cell(work, *c, live=False), plan[1:])
            cells += [(*c, proc) for c, proc in zip(plan[1:], procs, strict=True)]
        ran = [c for c in cells if c[3].returncode == 0]
        R.check(
            "12.6",
            len(ran) == len(cells),
            f"{len(ran)}/{len(cells)} replay cells ran through run.py against the real kernel: "
            + "; ".join(
                c[3].stdout.strip() or c[3].stderr.strip()[-200:]
                for c in cells
                if c[3].returncode != 0 or c[2] == SEEDS[0]
            ),
        )
        # One labelled live-mode run: one real request to the model gateway.
        live_arm = ("H2", "script", control, work / "build")
        live = run_cell(work, live_arm, LIVE_TASK, LIVE_SEED, live=True)
        live_dir = cell_path(work, "H2", "script", LIVE_TASK, LIVE_SEED, live=True)

        # Clause 12.3 and 12.6 from ledgers and attempt records.
        set_digest = run_mod.cassette_set_digest(work / "cassettes")
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
                and head.get("cassette_digest") == set_digest
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
            f"tail size K, the arm's Build hash and the cassette set digest {set_digest}",
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
        seeds_run: dict[str, list[int]] = {}
        for hyp, key, task, _, d, _ in attempts:
            head = read_jsonl(d / "ledger.jsonl")[0]
            seeds_run.setdefault(f"{hyp}/{key}/{task}", []).append(head["seed"])
        registered = sorted(prereg["campaign"]["seeds"])
        R.check(
            "12.3",
            prereg["campaign"]["seeds_per_task"] == 10
            and len(registered) == 10
            and len(seeds_run) == len(arms) * len(TASKS)
            and all(sorted(s) == registered for s in seeds_run.values()),
            f"each of the {len(arms)} arms ran each of the {len(TASKS)} tasks with the 10 registered "
            f"seeds {registered}, read back from the ledger headers of {len(attempts)} Instances",
        )
        check_context_layout(work, run_mod, model_mod, control)

        served, unmatched = 0, []
        for *_, d, attempt in attempts:
            cassette = Path(attempt["attempt"]["cassette"])
            entries = {
                (e["adapter"], e["request_hash"], e["seq"]): e for e in read_jsonl(cassette)[1:]
            }
            for r in read_jsonl(d / "ledger.jsonl"):
                if r.get("kind") != "adapter":
                    continue
                g = r.get("gateway") or {}
                entry = entries.get((r["primitive"], g.get("request_hash"), g.get("seq")))
                status = (entry or {}).get("response", {}).get("status")
                if entry and (r["outcome"]["status"] == "ok") == (status == 200):
                    served += 1
                else:
                    unmatched.append(f"{d.name}:seq {r['seq']}")
        R.check(
            "12.6",
            served > 0 and not unmatched,
            f"{served} adapter calls in the campaign ledgers each match an entry of the cell's "
            f"recorded cassette by (adapter, request hash, ordinal) and outcome; the gateway ran in "
            f"replay mode, which opens no socket (unmatched={unmatched[:5] or 'none'})",
        )
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
        R.check(
            "12.6",
            env_model is not None
            and live.returncode == 0
            and live_attempt.get("mode") == "live"
            and live_attempt.get("exit_status") not in ("model_error", "kernel_error")
            and live_attempt.get("turns") == 1,
            f"live-mode run (one real gateway request; gateway variables exported: "
            f"{env_model is not None}) rc={live.returncode} labelled "
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
            check_live_exclusion(work, report, live_dir, control, set_digest, model_mod.STUB_MODEL)
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
    """Run one Instance per seed in process and inspect every context the model received (12.3)."""

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
        f"{len(contexts)} contexts over {len(SEEDS)} Instances: each starts with the Build manual byte for byte "
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
    methods = {
        cell: (i["values"]["task_success"], i["values"]["turns_to_success_method"])
        for cell, i in instances.items()
    }
    wrong = [
        c
        for c, (success, method) in methods.items()
        if method != ("ledger_replay" if success == 1 else "undefined_on_failure")
    ]
    replayed = sum(1 for _, m in methods.values() if m == "ledger_replay")
    by_method = report.get("turns_to_success", {}).get("instances_by_method", {})
    R.check(
        "12.4",
        needed <= present and instances and not wrong and replayed > 0,
        "the seven 12.4 metrics (task success, turns to success, primitive calls, failure rate by "
        "each of 11 codes, repeated-failure rate, rule reuse, post-upgrade misuse) are computed; "
        f"turns to success of all {replayed} successful Instances comes from state replayed per "
        "turn from the ledger through the kernel, failed ones are undefined "
        f"({by_method}; wrong method={wrong[:3] or 'none'})",
    )
    check = analyse(work, "--results", work / "results", "--check", report_path)
    failed = [a for a in attempts if a[5]["metrics"]["values"]["task_success"] == 0]
    target_cell = (failed or attempts)[0][4]
    tampered = work / "tampered"
    shutil.copytree(work / "results", tampered)
    ledger = target_cell.relative_to(work / "results") / "ledger.jsonl"
    records = read_jsonl(tampered / ledger)
    altered = None
    for record in records:
        if record.get("kind") == "primitive" and record["outcome"]["status"] == "ok":
            record["outcome"] = {**record["outcome"], "status": "error", "code": "LOCKED"}
            altered = record["seq"]
            break
    ledgerio.write_ledger(tampered / ledger, records)
    recheck = analyse(work, "--results", tampered, "--check", report_path)
    expected = (1, "report differs") if failed else (4, "replay diverges from the ledger")
    R.check(
        "12.4",
        check.returncode == 0
        and altered is not None
        and recheck.returncode == expected[0]
        and expected[1] in recheck.stderr,
        f"analyse.py --check equals the fresh report (rc={check.returncode}) and fails once call "
        f"record seq {altered} in {ledger} is altered (rc={recheck.returncode}: "
        f"{recheck.stderr.strip()[-160:]})",
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


def trace_events(log: Path) -> list[tuple[str, str]]:
    return [tuple(e) for e in read_jsonl(log)] if log.is_file() else []


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
        # Fake per-turn snapshots claiming the final state already held after turn 1.
        (cell / "states").mkdir()
        final = (cell / "final_state.kvx").read_text("utf-8")
        for n in range(1, 4):
            (cell / "states" / f"turn_{n:04d}.kvx").write_text(final, encoding="utf-8")
    shutil.rmtree(work / "home")
    for home in work.glob("home-*"):
        shutil.rmtree(home)
    same = analyse(
        work, "--results", stripped, "--check", report_path, trace=work / "trace-analyse.jsonl"
    )
    R.check(
        "8.5",
        same.returncode == 0 and not list(work.glob("home*")),
        "with the runner's own numbers overwritten, fake per-turn state snapshots (the final state "
        "after turns 1 to 3), fake sandbox stdout and adapter payload files added and every kernel "
        f"Instance store deleted, analyse.py reproduces the report exactly (rc={same.returncode})",
    )

    # The verifier's tamper: turn 2's input copied over turn 1 in a cell whose turn 1 fails.
    target = next(
        (
            d
            for _, _, task, seed, d, a in attempts
            if seed % 2 == 0
            and a["metrics"]["values"]["task_success"] == 1
            and a["metrics"]["values"]["turns_to_success"] >= 2
        ),
        None,
    )
    forged = work / "forged"
    shutil.copytree(work / "results", forged)
    detail = "no successful even-seed cell with two or more turns"
    if target is not None:
        messages = forged / target.relative_to(work / "results") / "messages"
        (messages / "turn_0001.md").write_bytes((messages / "turn_0002.md").read_bytes())
        ledger_same = (forged / target.relative_to(work / "results") / "ledger.jsonl").read_bytes()
        unchanged = ledger_same == (target / "ledger.jsonl").read_bytes()
        forged_run = analyse(work, "--results", forged, "--check", report_path)
        detail = (
            f"rc={forged_run.returncode}, ledger and final state byte-identical={unchanged}: "
            f"{forged_run.stderr.strip()[-200:]}"
        )
        ok = (
            unchanged
            and forged_run.returncode == 4
            and "replay diverges from the ledger" in forged_run.stderr
        )
    else:
        ok = False
    R.check(
        "8.5",
        ok,
        "a recorded turn message replaced so that it would change the state after turn 1 is "
        f"refused, never reported as a different number ({detail})",
    )

    root = str(work)
    run_events = trace_events(work / "trace-run.jsonl")
    store = str(work / "home") + os.sep
    run_store = sorted(
        {
            p
            for kind, p in run_events
            if kind == "open" and p.startswith(store) and os.sep + "instances" + os.sep in p
        }
    )
    run_bad = [p for p in run_store if not p.endswith(os.sep + "ledger.jsonl")]
    an_events = trace_events(work / "trace-analyse.jsonl")
    results_root = str(stripped) + os.sep
    cell_files = sorted(
        {p for kind, p in an_events if kind == "open" and p.startswith(results_root)}
    )
    an_bad = [
        p
        for p in cell_files
        if Path(p).name not in CELL_INPUTS and not MESSAGE.search(p.replace(os.sep, "/"))
    ]
    listed = sorted({p for kind, p in an_events if kind == "list" and p.startswith(results_root)})
    an_store = sorted(
        {
            p
            for kind, p in an_events
            if kind == "open" and os.sep + "instances" + os.sep in p and not p.startswith(root)
        }
    )
    an_store_bad = [p for p in an_store if not p.endswith(os.sep + "ledger.jsonl")]
    states_touched = [p for p in cell_files + listed if os.sep + "states" in p]
    R.check(
        "8.5",
        run_events
        and an_events
        and run_store
        and not run_bad
        and cell_files
        and not an_bad
        and not states_touched
        and an_store
        and not an_store_bad,
        f"audit hook: run.py opened {len(run_store)} files of its Instance store, all ledgers "
        f"(others: {run_bad[:3] or 'none'}); analyse.py opened {len(cell_files)} results files, "
        "only ledger.jsonl, final_state.kvx, attempt.kvx and messages/turn_NNNN.md "
        f"(others: {an_bad[:3] or 'none'}), never states/ ({len(states_touched)} touches), and "
        f"from its replay Instance stores only {len(an_store)} ledgers (others: "
        f"{an_store_bad[:3] or 'none'})",
    )

    metrics_src = REPO / "benchmark" / "harness" / "metrics.py"
    imports = set()
    for node in ast.walk(ast.parse(metrics_src.read_text("utf-8"))):
        if isinstance(node, ast.Import):
            imports |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imports.add(node.module or "")
        elif isinstance(node, ast.Call) and getattr(node.func, "id", None) in ("open", "eval"):
            imports.add("open()")
    R.check(
        "8.5",
        imports <= {"__future__", "collections.abc", "typing"},
        f"metrics.py imports only {sorted(imports)} and does no I/O",
    )


def run_stub_live(work: Path, control: str, results: Path) -> tuple[Path, str]:
    """A live-mode cell with the deterministic stub model under the replay registration.

    Its model, arm and Build match the replay campaign, so only its gateway mode separates it from
    a replay cell: the kernel writes no cassette digest into a live ledger header.
    """
    proc = run(
        [
            sys.executable,
            "-I",
            REPO / "benchmark/harness/run.py",
            "--hypothesis",
            "H2",
            "--arm",
            "script",
            "--task",
            LIVE_TASK,
            "--seed",
            str(STUB_LIVE_SEED),
            "--build",
            control,
            "--builds",
            work / "build",
            "--home",
            work / "home" / "stub-live",
            "--results",
            results,
            "--prereg",
            SMOKE["replay"],
            *(["--allow-unregistered"] if SMOKE["draft"] else []),
            "--live",
            "--model",
            "stub",
        ],
        env={"CWR_BIN": str(work / "cwr")},
    )
    cell = results / "live" / "h2" / "script" / LIVE_TASK / str(STUB_LIVE_SEED)
    return cell, f"rc={proc.returncode} {proc.stderr.strip()[-200:]}"


def pooled_tree(work: Path, name: str, live_cell: Path, attempt_edit=None) -> tuple[Path, str]:
    """The replay results with live_cell copied in at its replay path, attempt.kvx edited."""
    tree = work / name
    shutil.copytree(work / "results", tree, ignore=shutil.ignore_patterns("live"))
    rel = Path("h2") / "script" / LIVE_TASK / str(STUB_LIVE_SEED)
    if (tree / rel).exists():
        shutil.rmtree(tree / rel)
    shutil.copytree(live_cell, tree / rel)
    if attempt_edit is not None:
        path = tree / rel / "attempt.kvx"
        path.write_text(attempt_edit(path.read_text("utf-8")), encoding="utf-8")
    return tree, str(rel)


def check_live_exclusion(
    work: Path, report: dict, live_dir: Path, control: str, set_digest: str, stub_model: str
) -> None:
    if not report:
        R.check("12.6", False, "no replay report to check live exclusion against")
        return
    cells = [i["cell"] for h in report["hypotheses"].values() for i in h["instances"]]
    R.check(
        "12.6",
        live_dir.is_dir()
        and str(live_dir).startswith(str(work / "results" / "live"))
        and not any(c.endswith(f"/{LIVE_TASK}/{LIVE_SEED}") for c in cells)
        and report["mode"] == "replay",
        f"the replay report pools {len(cells)} Instances, none from results/live "
        f"(live cell present: {live_dir.is_dir()})",
    )
    stub_live, how = run_stub_live(work, control, work / "results-stub-live")
    head = read_jsonl(stub_live / "ledger.jsonl")[0] if stub_live.is_dir() else {}
    attempt = kvx(stub_live / "attempt.kvx")["attempt"] if stub_live.is_dir() else {}
    R.check(
        "12.6",
        stub_live.is_dir()
        and attempt.get("mode") == "live"
        and head.get("model") == stub_model
        and "cassette_digest" not in head,
        f"a live-mode stub cell under the replay registration ({how.strip()}) is labelled "
        f"mode={attempt.get('mode')!r}, model {head.get('model')!r}, and its ledger header names no cassette digest "
        f"({'cassette_digest' not in head})",
    )
    if not stub_live.is_dir():
        return
    pooled, rel = pooled_tree(work, "pooled", stub_live)
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
        and any(e["cell"] == rel and "mode 'live' is not 'replay'" in e["reason"] for e in excluded)
        and same_cells,
        f"a live cell copied into the replay tree is excluded ({excluded}) and every replay "
        "statistic is unchanged",
    )
    relabels = {
        "mode": lambda t: set_kvx(t, "attempt", "mode", "replay"),
        "mode and cassette digest": lambda t: set_kvx(
            set_kvx(t, "attempt", "mode", "replay"), "attempt", "cassette_digest", set_digest
        ),
    }
    outcomes = []
    for n, (what, edit) in enumerate(relabels.items()):
        tree, _ = pooled_tree(work, f"relabelled-{n}", stub_live, edit)
        out = work / f"relabelled-{n}.json"
        proc = analyse(work, "--results", tree, "--out", out)
        outcomes.append(
            (
                what,
                proc.returncode,
                "names no cassette digest" in proc.stderr and not out.exists(),
            )
        )
    R.check(
        "12.6",
        all(rc == 4 and named for _, rc, named in outcomes),
        "a live ledger whose attempt.kvx is relabelled replay is refused, never pooled: "
        + "; ".join(
            f"relabelled {what}: rc={rc}, header without cassette digest named={named}"
            for what, rc, named in outcomes
        ),
    )
    live = analyse(
        work, "--results", work / "results", "--live", "--out", work / "live.json", live=True
    )
    live_report = json.loads((work / "live.json").read_text()) if live.returncode == 0 else {}
    live_cells = [
        i["cell"] for h in live_report.get("hypotheses", {}).values() for i in h["instances"]
    ]
    expected = str(live_dir.relative_to(work / "results" / "live"))
    R.check(
        "12.6",
        live.returncode == 0 and live_report.get("mode") == "live" and live_cells == [expected],
        f"analyse.py --live writes a separately labelled report (mode={live_report.get('mode')!r}) "
        f"holding only the live Instance {live_cells}",
    )


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Task 5.3 driver: H3 long-context and H4 controlled-change campaigns (case CWR-018).

Clauses owned: 5.3 (recompile notice rendering), 10.3 (silent-change and version-only Builds),
12.2 (H3 per bin and per ledger tail K, H4 per notice arm on the upgrade pairs) and 12.5 (mean and
95% bootstrap interval per arm and group, directions registered before any run, metric definitions
unchanged after execution).

Modes (all run from any working directory; python3 -I safe):

  python3 tests/cwr/cases/5_3.py                      verify every owned clause; exit 0 only on PASS
  python3 tests/cwr/cases/5_3.py --register           compile the H3/H4 Builds with the real cwr CLI and
                                                      freeze benchmark/campaigns/h3.kvx and h4.kvx;
                                                      refused once any H3/H4 result exists
  python3 tests/cwr/cases/5_3.py --execute h3|h4 --pilot | --full  [--jobs N] [--max-cells N]
                                                      run campaign cells through benchmark/harness/run.py
                                                      (run_episode) with the gateway model; resumable
  python3 tests/cwr/cases/5_3.py --analyse            run benchmark/analyse.py and render the research
                                                      reports research/h3-long-context.md and
                                                      research/h4-controlled-change.md

Verification exercises the real implementation: the cwr compiler builds every Build, the cwr kernel
runs the reference turns, upgrades and renders contexts, and the campaign ledgers are read from disk.
Every experimental arm is a CWR Build with exactly one pillar ablated; nothing here references,
invokes or compares against any other agent or harness.

Secrets: the gateway configuration is read only by benchmark/harness/model.py at runtime. This file
never reads, prints or stores a value from the environment file.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from benchmark.harness.arms import load_arms  # noqa: E402
from benchmark.harness.ledgerio import (  # noqa: E402
    load_predicate,
    read_kvx,
    read_ledger,
    sha256_file,
    state_view,
    write_kvx,
)
from benchmark.harness.prereg import load_prereg, missing_fields  # noqa: E402

BENCH = REPO / "benchmark"
CAMPAIGNS = BENCH / "campaigns"
RESULTS = BENCH / "results"
H3_KVX = CAMPAIGNS / "h3.kvx"
H4_KVX = CAMPAIGNS / "h4.kvx"
PREREG = BENCH / "preregistration.kvx"
METRICS_PY = BENCH / "harness" / "metrics.py"
ARMS_KVX = BENCH / "harness" / "arms.kvx"
SUITE = BENCH / "suite.kvx"
UPGRADES = BENCH / "upgrades.kvx"
ANALYSE = BENCH / "analyse.py"
REPORTS = {
    "H3": REPO / "research" / "h3-long-context.md",
    "H4": REPO / "research" / "h4-controlled-change.md",
}
H3_ARMS = ("k5", "k20", "kfull")
H4_ARMS = ("changelog", "version-only", "silent")
H4_FLAGS = {"changelog": None, "version-only": "--x-version-only", "silent": "--x-silent-change"}
PAIRS = ("U1", "U2", "U3", "U4", "U5", "U6")
BINS = ("short", "medium", "long")
EXCLUDED_EXITS = ("model_error", "kernel_error")
HEADER_LINE = re.compile(r"^WORLD RECOMPILED\s+(\S+)\s+->\s+(\S+)", re.M)
HASH = re.compile(r"^sha256:[0-9a-f]{64}$")


# ---------------------------------------------------------------------------------------------
# Small helpers


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def fmt(value: Any, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int):
        return str(value)
    return f"{value:.{digits}f}"


def rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO))
    except ValueError:
        return str(path)


def scratch_root() -> Path:
    base = os.environ.get("CWR_T53_SCRATCH")
    root = Path(base) if base else Path(tempfile.gettempdir()) / "cwr-t5_3"
    root.mkdir(parents=True, exist_ok=True)
    return root


def target_dir() -> Path:
    return Path(os.environ.get("CARGO_TARGET_DIR") or REPO / "target")


def cwr_binary() -> Path:
    """The cwr CLI, built with cargo into $CARGO_TARGET_DIR (default the workspace target)."""
    if os.environ.get("CWR_BIN"):
        return Path(os.environ["CWR_BIN"])
    for profile in ("release", "debug"):
        candidate = target_dir() / profile / "cwr"
        if candidate.exists():
            return candidate
    proc = subprocess.run(
        ["cargo", "build", "-q", "-p", "cwr-cli"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"cargo build -p cwr-cli failed: {proc.stderr.strip()[-400:]}")
    return target_dir() / "debug" / "cwr"


def cwr(*args: str, cwd: Path = REPO, stdin: str | None = None, env: dict | None = None):
    full_env = dict(os.environ)
    full_env.update(env or {})
    return subprocess.run(
        [str(cwr_binary()), *args],
        cwd=cwd,
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        env=full_env,
    )


def suite_tasks() -> dict[str, str]:
    """Task id to bin for the 60-task suite, from benchmark/suite.kvx."""
    suite = read_kvx(SUITE)
    return {
        name.split(".", 1)[1]: body["bin"]
        for name, body in sorted(suite.items())
        if name.startswith("task.")
    }


def upgrades() -> dict[str, dict[str, Any]]:
    return read_kvx(UPGRADES)


def campaign(path: Path) -> dict[str, dict[str, Any]]:
    return read_kvx(path) if path.exists() else {}


def result_cells(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(
        p.parent for p in root.glob("*/*/*/attempt.kvx") if not p.parts[-4].startswith("_")
    )


def any_results(hypothesis: str) -> bool:
    return bool(result_cells(RESULTS / hypothesis.lower())) or bool(
        list((RESULTS / hypothesis.lower() / "_pilot").glob("**/attempt.kvx"))
    )


# ---------------------------------------------------------------------------------------------
# Builds (the operator role: only `cwr build` creates a Build)


def pair_root(pair: str | None, experimental: bool) -> Path:
    """Build root of the control world (build/control/build) or of one pair (build/pairs/UN/build).

    Each upgrade pair has its own root because the compiler's default previous Build is the newest
    Build of the same world name under the output root (side A of a pair is a first Build), and the
    compiler writes experimental Builds to the xbuild/ directory beside the root.
    """
    base = REPO / "build" / "pairs" / pair if pair else REPO / "build" / "control"
    return base / ("xbuild" if experimental else "build")


def build_roots() -> list[Path]:
    """Every root a registered H3/H4 Build hash resolves in."""
    roots = [REPO / "build", REPO / "xbuild", pair_root(None, False), pair_root(None, True)]
    for pair in PAIRS:
        roots += [pair_root(pair, False), pair_root(pair, True)]
    return roots


def build(source: Path, *flags: str, previous: str | None = None, pair: str | None = None) -> str:
    """Compile a World Source with the real compiler and return its sha256:<hex> hash."""
    root = pair_root(pair, False)
    root.mkdir(parents=True, exist_ok=True)
    args = ["build", str(source), "--out", str(root)]
    if previous:
        found = build_dir(previous)
        args += ["--previous", str(found) if found else previous.removeprefix("sha256:")]
    args += list(flags)
    proc = cwr(*args)
    match = re.search(
        r"built(?: experimental)?:?\s+(sha256:[0-9a-f]{64})", proc.stdout + "\n" + proc.stderr
    )
    if proc.returncode != 0 or not match:
        raise RuntimeError(
            f"cwr build {rel(source)} {' '.join(flags)} exited {proc.returncode}: "
            f"{(proc.stdout + proc.stderr).strip()[-600:]}"
        )
    return match.group(1)


def build_dir(hash_: str) -> Path | None:
    hexpart = hash_.removeprefix("sha256:")
    for root in build_roots():
        candidate = root / hexpart
        if candidate.is_dir():
            return candidate
    return None


def manifest(hash_: str) -> dict[str, Any]:
    """The [build] section of a Build's manifest.kvx, or {} when the Build is absent."""
    directory = build_dir(hash_)
    if directory is None:
        return {}
    return read_kvx(directory / "manifest.kvx").get("build", {})


# ---------------------------------------------------------------------------------------------
# Registration


H4_CASSETTES = "benchmark/results/h4/_cassettes"


def suite_cassette_digest() -> str | None:
    """Digest of the suite cassette set (task 4.1), or None while it has not been recorded."""
    from benchmark.harness import run as harness

    root = BENCH / "cassettes" / "suite"
    return harness.cassette_set_digest(root) if root.is_dir() else None


def record_pair_cassettes(pair: str, a: str, b: str, seeds: list[int]) -> str:
    """Record the replay cassette set of one pair task through the real kernel gateway.

    For every seed the pair's reference turns (first half on Build A, upgrade, post-upgrade turns
    on Build B) run with `cwr instance turn --gateway record`; the cassette holds whatever adapter
    traffic the gateway saw (none for the six pair tasks, whose reference turns use fs, kv and jobs
    only). Returns the cassette set digest over every recorded pair cassette.
    """
    from benchmark.harness import run as harness

    up = upgrades()
    task, body = up[f"task.{pair}"], up[f"pair.{pair}"]
    pair_dir = (REPO / body["task_dir"]).parent
    home = scratch_root() / "record-home"
    roots = [arg for root in build_roots() for arg in ("--builds", str(root))]
    initial = harness.kernel_initial_state(
        state_view(read_kvx(REPO / body["task_dir"] / "initial.kvx"))
    )
    initial_path = home / f"{pair}-initial.json"
    initial_path.parent.mkdir(parents=True, exist_ok=True)
    initial_path.write_text(json.dumps(initial, sort_keys=True, separators=(",", ":")), "utf-8")
    for seed in seeds:
        cassette = REPO / H4_CASSETTES / body["task"] / f"seed-{seed}.jsonl"
        if cassette.exists():
            cassette.unlink()
        cassette.parent.mkdir(parents=True, exist_ok=True)
        made = cwr(
            "instance",
            "create",
            a,
            "--seed",
            str(seed),
            "--arm",
            "H4/record",
            "--model",
            "reference-scripts",
            "--initial",
            str(initial_path),
            "--home",
            str(home),
            *roots,
        )
        if made.returncode != 0:
            raise RuntimeError(f"{pair} seed {seed}: instance create failed: {made.stderr[-300:]}")
        instance = made.stdout.strip()
        record = [
            "--gateway",
            "record",
            "--cassette",
            str(cassette),
            "--cassette-label",
            f"{body['task']}/seed-{seed}",
        ]
        steps = [("turn", s) for s in task["first_half"]] + [("upgrade", b)]
        steps += [("turn", s) for s in task["post_upgrade"]]
        for kind, arg in steps:
            if kind == "upgrade":
                proc = cwr("instance", "upgrade", instance, arg, "--home", str(home), *roots)
            else:
                script = (pair_dir / arg).read_text("utf-8")
                proc = cwr("instance", "turn", instance, "--home", str(home), *record, stdin=script)
            if proc.returncode != 0:
                raise RuntimeError(f"{pair} seed {seed}: {kind} failed: {proc.stderr[-300:]}")
        cwr("instance", "destroy", instance, "--home", str(home))
    return harness.cassette_set_digest(REPO / H4_CASSETTES)


def register(args: argparse.Namespace) -> int:
    for hypothesis in ("H3", "H4"):
        if any_results(hypothesis) and not args.force_reregister_without_results:
            print(f"refused: {hypothesis} results exist; a registration precedes every run")
            return 2
    meta, arms = load_arms(ARMS_KVX)
    control = meta["control"].get("build")
    if control and build_dir(control) is None:
        print(f"refused: the registered control Build {control} is not on disk")
        return 2
    control = control or build(REPO / "examples" / "refworld")
    prereg = load_prereg(PREREG)
    stamp = now()
    digests = {
        "preregistration_kvx": sha256_file(PREREG),
        "metrics_py": sha256_file(METRICS_PY),
        "arms_kvx": sha256_file(ARMS_KVX),
        "analyse_py": sha256_file(ANALYSE),
        "suite_kvx": sha256_file(SUITE),
        "upgrades_kvx": sha256_file(UPGRADES),
    }
    if digests["metrics_py"] != prereg["digests"]["metrics_py"]:
        print("refused: metrics.py differs from its pre-registered digest")
        return 3
    status = (
        "registered"
        if not missing_fields(prereg, arms, meta["control"])
        else "registered-draft-prereg"
    )
    tasks = suite_tasks()
    h3_dirs = {
        f"arm.{key}": {
            "arm_id": f"H3/{key}",
            "label": arms[("H3", key)].label,
            "tail": arms[("H3", key)].tail,
            "build": control,
            "results_dir": f"benchmark/results/h3/{key}",
        }
        for key in H3_ARMS
    }
    h3 = {
        "meta": {
            "format": "cwr.campaign",
            "schema_version": 1,
            "hypothesis": "H3",
            "world": "refworld",
            "status": status,
            "registered_at": stamp,
            "no_external_baseline": True,
        },
        "registration": {
            **digests,
            "control_build": control,
            "prereg_status": prereg["meta"]["status"],
        },
        **h3_dirs,
        "task_set": {
            "source": "benchmark/suite.kvx",
            "set": "suite:all",
            "tasks": sorted(tasks),
            "bins": {b: sorted(t for t, v in tasks.items() if v == b) for b in BINS},
            "rule_reuse_tasks": sorted(
                t
                for t, body in (
                    (n.split(".", 1)[1], b)
                    for n, b in read_kvx(SUITE).items()
                    if n.startswith("task.")
                )
                if body.get("reused_rule")
            ),
        },
        "seeds": {
            "seeds": prereg["campaign"]["seeds"],
            "per_task": prereg["campaign"]["seeds_per_task"],
        },
        "pilot": {
            "tasks": ["t001-copy-readme", "t021-sports-report", "t041-poll-then-reuse"],
            "seeds": [1],
            "arms": list(H3_ARMS),
            "results_root": "benchmark/results/h3/_pilot",
            "pooled": False,
            "gate": "the full campaign starts only after every pilot cell ended without kernel_error or model_error",
        },
        "execution": {
            "gateway_mode": "replay",
            "results_root": "benchmark/results",
            "layout": "benchmark/results/h3/<arm>/<task>/<seed>/{ledger.jsonl, final_state.kvx, attempt.kvx, states/}",
            "turn_limit_by_bin": prereg["campaign"]["turn_limit_by_bin"],
            "turn_limit_max": prereg["campaign"]["turn_limit_max"],
            "rate_limit": "one gateway client per process with a bounded number of concurrent requests (--jobs); a shared limiter in benchmark/harness/model.py is used when present",
            "cassette_set": "benchmark/cassettes/suite",
            "cassette_digest": suite_cassette_digest(),
        },
        "directions": {
            "source": "benchmark/preregistration.kvx [hypothesis.H3]",
            "ids": [d["id"] for d in prereg["hypothesis.H3"]["directions"]],
        },
    }
    up = upgrades()
    h4_pairs: dict[str, dict[str, Any]] = {}
    for pair in PAIRS:
        body, task = up[f"pair.{pair}"], up[f"task.{pair}"]
        a = build(REPO / body["source_a"], pair=pair)
        b = {
            arm: build(REPO / body["source_b"], *([flag] if flag else []), previous=a, pair=pair)
            for arm, flag in H4_FLAGS.items()
        }
        for arm, flag in H4_FLAGS.items():
            man = manifest(b[arm])
            if flag and (b[arm] == b["changelog"] or man.get("experimental") is not True):
                print(
                    f"refused: {pair} Build B with {flag} is not an experimental Build ({b[arm]})"
                )
                return 2
        h4_pairs[f"pair.{pair}"] = {
            "task": body["task"],
            "task_dir": body["task_dir"],
            "predicate": body["predicate"],
            "initial": f"{body['task_dir']}/initial.kvx",
            "bin": task["bin"],
            "from_version": body["from_version"],
            "to_version": body["to_version"],
            "upgrade_after_turn": task["upgrade_after_turn"],
            "changed_primitives": body["changed_primitives"],
            "misuse_code": body["misuse_code"],
            "build_a": a,
            "build_b": b,
        }
    h4_digest = None
    for pair in PAIRS:
        body = h4_pairs[f"pair.{pair}"]
        h4_digest = record_pair_cassettes(
            pair, body["build_a"], body["build_b"]["changelog"], prereg["campaign"]["seeds"]
        )
    h4 = {
        "meta": {**h3["meta"], "hypothesis": "H4"},
        "registration": dict(h3["registration"]),
        **{
            f"arm.{key}": {
                "arm_id": f"H4/{key}",
                "flag": H4_FLAGS[key],
                "experimental_stamp": arms[("H4", key)].experimental_stamp,
                "tail": arms[("H4", key)].tail,
                "results_dir": f"benchmark/results/h4/{key}",
            }
            for key in H4_ARMS
        },
        **h4_pairs,
        "seeds": dict(h3["seeds"]),
        "pilot": {
            "pairs": list(PAIRS),
            "seeds": [1],
            "arms": list(H4_ARMS),
            "results_root": "benchmark/results/h4/_pilot",
            "pooled": False,
            "gate": h3["pilot"]["gate"],
        },
        "execution": {
            **h3["execution"],
            "cassette_set": H4_CASSETTES,
            "cassette_digest": h4_digest,
            "cassette_note": "pair tasks are not in the suite cassette set; this set is recorded through the real kernel gateway from each pair's reference turns, one cassette per pair task and seed",
            "layout": "benchmark/results/h4/<arm>/<task>/<seed>/{ledger.jsonl, final_state.kvx, attempt.kvx, states/}",
        },
        "directions": {
            "source": "benchmark/preregistration.kvx [hypothesis.H4]",
            "ids": [d["id"] for d in prereg["hypothesis.H4"]["directions"]],
        },
    }
    note = (
        "CWR campaign registration written by tests/cwr/cases/5_3.py --register before any {h} run.\n"
        "Every arm is a CWR Build of refworld; there is no external baseline. Build hashes come from the\n"
        "real compiler; digests freeze the metric definitions, arms, suite and analysis at registration."
    )
    write_kvx(
        H3_KVX,
        h3,
        header="Copyright Sidiora Labs Inc. CWR® 2026-2027\nSPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0\n"
        + note.format(h="H3"),
    )
    write_kvx(
        H4_KVX,
        h4,
        header="Copyright Sidiora Labs Inc. CWR® 2026-2027\nSPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0\n"
        + note.format(h="H4"),
    )
    print(f"registered H3 and H4 at {stamp} (status {status}); control {control}")
    return 0


# ---------------------------------------------------------------------------------------------
# Execution


def kernel(home: Path | None = None) -> Any:
    """The harness binding to the real cwr CLI, resolving every H3/H4 Build root, in replay mode."""
    from benchmark.harness import run as harness

    return harness.CwrCliKernel(gateway_mode="replay", home=home, builds=build_roots())


def cassette_for(camp: dict[str, Any], task: str, seed: int) -> Path:
    """The registered replay cassette of one Instance: <cassette_set>/<task>/seed-<n>.jsonl."""
    cassette_set = camp.get("execution", {}).get("cassette_set")
    if not cassette_set:
        raise RuntimeError("no cassette set is registered for this campaign")
    path = REPO / cassette_set / task / f"seed-{seed}.jsonl"
    if not path.is_file():
        raise RuntimeError(f"no cassette {rel(path)} in the registered set")
    return path


class LimitedModel:
    """The gateway client behind a bounded number of concurrent requests (one per worker)."""

    def __init__(self, client: Any, concurrency: int):
        self._client = client
        self._gate = threading.BoundedSemaphore(max(1, concurrency))
        self.model = client.model

    def complete_context(self, context: str, *, max_tokens: int, params: dict | None = None):
        with self._gate:
            return self._client.complete_context(context, max_tokens=max_tokens, params=params)


def plan_cells(hypothesis: str, pilot: bool) -> list[dict[str, Any]]:
    camp = campaign(H3_KVX if hypothesis == "H3" else H4_KVX)
    if not camp:
        raise RuntimeError(f"{hypothesis} is not registered; run --register first")
    seeds = camp["pilot"]["seeds"] if pilot else camp["seeds"]["seeds"]
    root = REPO / camp["pilot"]["results_root"] if pilot else RESULTS
    cells = []
    if hypothesis == "H3":
        tasks = camp["pilot"]["tasks"] if pilot else camp["task_set"]["tasks"]
        bins = suite_tasks()
        for seed in seeds:
            for task in tasks:
                for key in H3_ARMS:
                    cells.append(
                        {
                            "hypothesis": "H3",
                            "arm": key,
                            "task": task,
                            "seed": seed,
                            "bin": bins[task],
                            "build": camp[f"arm.{key}"]["build"],
                            "root": root,
                            "upgrade": None,
                            "task_dir": BENCH / "tasks" / task,
                            "predicate": BENCH / "tasks" / task / "success.py",
                        }
                    )
    else:
        pairs = camp["pilot"]["pairs"] if pilot else list(PAIRS)
        for seed in seeds:
            for pair in pairs:
                body = camp[f"pair.{pair}"]
                for key in H4_ARMS:
                    cells.append(
                        {
                            "hypothesis": "H4",
                            "arm": key,
                            "task": body["task"],
                            "seed": seed,
                            "bin": body["bin"],
                            "build": body["build_a"],
                            "root": root,
                            "pair": pair,
                            "upgrade": (body["upgrade_after_turn"], body["build_b"][key]),
                            "task_dir": REPO / body["task_dir"],
                            "predicate": REPO / body["predicate"],
                        }
                    )
    return cells


def cell_path(cell: dict[str, Any]) -> Path:
    return (
        cell["root"] / cell["hypothesis"].lower() / cell["arm"] / cell["task"] / str(cell["seed"])
    )


def cell_done(cell: dict[str, Any]) -> bool:
    attempt = cell_path(cell) / "attempt.kvx"
    if not attempt.exists():
        return False
    exit_status = read_kvx(attempt).get("attempt", {}).get("exit_status")
    return exit_status not in EXCLUDED_EXITS


def execute(args: argparse.Namespace) -> int:
    from benchmark.harness import run as harness

    hypothesis = args.execute.upper()
    pilot = args.pilot
    camp = campaign(H3_KVX if hypothesis == "H3" else H4_KVX)
    if not camp:
        print(f"{hypothesis} is not registered; run --register first")
        return 2
    if not pilot:
        pilot_cells = plan_cells(hypothesis, True)
        unclean = [c for c in pilot_cells if not cell_done(c)]
        if unclean and not args.skip_pilot_gate:
            print(f"refused: pilot gate open; {len(unclean)} pilot cells missing or failed")
            return 2
    if sha256_file(METRICS_PY) != camp["registration"]["metrics_py"]:
        print("refused: metrics.py changed after registration")
        return 3
    prereg = load_prereg(PREREG)
    meta, arms = load_arms(ARMS_KVX)
    registered = not missing_fields(prereg, arms, meta["control"])
    from benchmark.harness.model import GatewayClient

    client = GatewayClient.from_env()
    if prereg["campaign"].get("model") and client.model != prereg["campaign"]["model"]:
        print("refused: the configured model identity differs from the registered one")
        return 2
    model = LimitedModel(client, args.jobs)
    cells = [c for c in plan_cells(hypothesis, pilot) if not cell_done(c)]
    if args.max_cells:
        cells = cells[: args.max_cells]
    print(
        f"{hypothesis} {'pilot' if pilot else 'full'}: {len(cells)} cells to run with {args.jobs} workers",
        flush=True,
    )
    lock = threading.Lock()
    tally: dict[str, int] = {}

    suite_info = read_kvx(SUITE)

    def one(cell: dict[str, Any]) -> str:
        arm = arms[(hypothesis, cell["arm"])]
        upgrade = None
        if cell["upgrade"]:
            upgrade = harness.UpgradePlan(
                after_turn=cell["upgrade"][0], build_b=cell["upgrade"][1], pair=cell["pair"]
            )
        spec = harness.EpisodeSpec(
            hypothesis=hypothesis,
            arm=arm,
            task=cell["task"],
            seed=cell["seed"],
            build=cell["build"],
            predicate_path=cell["predicate"],
            initial_state=cell["task_dir"] / "initial.kvx",
            turn_limit=harness._turn_limit(prereg["campaign"], cell["bin"]),
            bin=cell["bin"],
            max_tokens_per_turn=prereg["campaign"].get("max_tokens_per_turn", 16000),
            token_budget=prereg["campaign"].get("token_budget_per_instance"),
            model_params=prereg["campaign"].get("sampling") or {},
            upgrade=upgrade,
            results_root=cell["root"],
            registered=registered,
            cassette_digest=camp["execution"].get("cassette_digest"),
            suite_digest=prereg["campaign"].get("suite_digest"),
            cassette=cassette_for(camp, cell["task"], cell["seed"]),
            instruction=(cell["task_dir"] / "task.md").read_text("utf-8"),
            reuse_rule=harness._reuse_rule(suite_info.get(f"task.{cell['task']}", {})),
            upgrade_pair=(
                {
                    "changed_primitives": camp[f"pair.{cell['pair']}"]["changed_primitives"],
                    "misuse_code": camp[f"pair.{cell['pair']}"]["misuse_code"],
                }
                if cell["upgrade"]
                else None
            ),
        )
        try:
            attempt = harness.run_episode(spec, kernel(), model)
            outcome = attempt["exit_status"]
        except Exception as error:  # recorded and counted; the cell stays open for a rerun
            outcome = f"kernel_error ({type(error).__name__}: {str(error)[:160]})"
        with lock:
            tally[outcome.split(" ")[0]] = tally.get(outcome.split(" ")[0], 0) + 1
            print(
                f"{hypothesis}/{cell['arm']} {cell['task']} seed {cell['seed']}: {outcome}",
                flush=True,
            )
        return outcome

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        list(pool.map(one, cells))
    print(f"{hypothesis} done: {json.dumps(tally, sort_keys=True)}")
    return 0


# ---------------------------------------------------------------------------------------------
# Analysis and reports


def run_analyse(out: Path, results: Path = RESULTS) -> tuple[int, str]:
    prereg = load_prereg(PREREG)
    meta, arms = load_arms(ARMS_KVX)
    draft = bool(missing_fields(prereg, arms, meta["control"]))
    cmd = [sys.executable, "-I", str(ANALYSE), "--results", str(results), "--out", str(out)]
    if draft:
        cmd.append("--draft-ok")
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, cwd=REPO)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def table(rows: list[list[str]], head: list[str]) -> str:
    lines = ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(lines)


def cell_text(summary: dict[str, Any]) -> str:
    ci = summary.get("ci")
    interval = f"[{fmt(ci[0])}, {fmt(ci[1])}]" if ci else "[n/a]"
    return f"{fmt(summary.get('mean'))} {interval} n={summary.get('n', 0)}"


def matrix_counts(hypothesis: str) -> dict[str, Any]:
    """Expected and present cells per arm from the campaign registration and the results tree."""
    camp = campaign(H3_KVX if hypothesis == "H3" else H4_KVX)
    if not camp:
        return {"registered": False}
    seeds = camp["seeds"]["seeds"]
    if hypothesis == "H3":
        tasks, arm_keys = camp["task_set"]["tasks"], H3_ARMS
    else:
        tasks, arm_keys = [camp[f"pair.{p}"]["task"] for p in PAIRS], H4_ARMS
    out: dict[str, Any] = {"registered": True, "arms": {}}
    for key in arm_keys:
        present = 0
        for task in tasks:
            for seed in seeds:
                cell = RESULTS / hypothesis.lower() / key / task / str(seed)
                if (cell / "ledger.jsonl").exists() and (cell / "attempt.kvx").exists():
                    present += 1
        out["arms"][key] = {"expected": len(tasks) * len(seeds), "present": present}
    out["expected"] = sum(v["expected"] for v in out["arms"].values())
    out["present"] = sum(v["present"] for v in out["arms"].values())
    return out


def pilot_counts(hypothesis: str) -> dict[str, Any]:
    root = RESULTS / hypothesis.lower() / "_pilot" / hypothesis.lower()
    tally: dict[str, int] = {}
    for attempt in sorted(root.glob("*/*/*/attempt.kvx")):
        status = read_kvx(attempt).get("attempt", {}).get("exit_status", "unknown")
        tally[status] = tally.get(status, 0) + 1
    return tally


def render_report(hypothesis: str, report: dict[str, Any] | None) -> str:
    """The research report: a deterministic function of the registration and the analysis report."""
    camp = campaign(H3_KVX if hypothesis == "H3" else H4_KVX)
    counts = matrix_counts(hypothesis)
    pilot = pilot_counts(hypothesis)
    title = (
        "H3 Long context: ledger tail sweep K in {5, 20, full}"
        if hypothesis == "H3"
        else "H4 Controlled change: changelog, version-only and silent notice arms"
    )
    out = [
        "<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->",
        "<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->",
        "",
        f"# {title}",
        "",
        "Generated by `python3 tests/cwr/cases/5_3.py --analyse` from the registration in "
        f"`benchmark/campaigns/{hypothesis.lower()}.kvx` and the ledger-only analysis in "
        f"`benchmark/results/{hypothesis.lower()}/analysis.json` (computed by `benchmark/analyse.py`). "
        "Every number below is recomputed from ledgers and final state by the task 5.3 driver; the file "
        "is regenerated, never edited by hand.",
        "",
        "Every arm is a CWR Build of refworld with at most one pillar ablated; there is no external "
        "baseline and nothing here compares CWR with anything that is not a CWR Build.",
        "",
        "## Status",
        "",
    ]
    complete = bool(counts.get("registered")) and counts.get("present") == counts.get("expected")
    if not counts.get("registered"):
        out.append(
            "Not registered: `benchmark/campaigns/"
            + hypothesis.lower()
            + ".kvx` does not exist. No result exists."
        )
    else:
        out.append(
            f"Registration: status `{camp['meta']['status']}` at {camp['meta']['registered_at']}. "
            f"Arm matrix: {counts['present']} of {counts['expected']} cells present "
            f"({'complete' if complete else 'INCOMPLETE'}). "
            + (
                "A hypothesis result requires the complete matrix with 10 seeds per task; this report is "
                "therefore NOT a hypothesis result and every interval below is descriptive of the cells "
                "present only."
                if not complete
                else "The matrix is complete."
            )
        )
    out += [
        "",
        "Pilot cells (never pooled): "
        + (", ".join(f"{k} {v}" for k, v in sorted(pilot.items())) or "none")
        + ".",
        "",
    ]
    if counts.get("registered"):
        out += ["## Arm matrix", ""]
        rows = []
        for key, v in counts["arms"].items():
            arm_body = camp[f"arm.{key}"]
            extra = (
                f"K={arm_body['tail']}"
                if hypothesis == "H3"
                else f"flag {arm_body['flag'] or 'none (control)'}"
            )
            rows.append([f"`{hypothesis}/{key}`", extra, str(v["present"]), str(v["expected"])])
        out += [table(rows, ["Arm", "Setting", "Cells present", "Cells expected"]), ""]
    if report is None or hypothesis not in report.get("hypotheses", {}):
        out += ["## Results", "", "No analysis report exists: no ledger has been analysed.", ""]
    else:
        body = report["hypotheses"][hypothesis]
        out += [
            "## Results",
            "",
            f"Registration label of the analysis: `{report['registration']}`. Excluded cells: "
            f"{len(report['excluded'])}. Mean over Instances with a 95% percentile bootstrap interval "
            f"({report['analysis']['resamples']} resamples over Instances); `n` counts Instances with a "
            "defined value.",
            "",
        ]
        if hypothesis == "H3":
            metric_list = [
                "rule_reuse",
                "task_success",
                "turns_to_success",
                "probing_ratio",
                "repeated_failure_rate",
            ]
            groups = list(BINS)
        else:
            metric_list = [
                "post_upgrade_misuse_rate",
                "turns_to_first_correct_use",
                "task_success",
                "proposals_after_upgrade",
            ]
            groups = [*PAIRS, "pooled"]
        for metric in metric_list:
            rows = []
            for arm_key, arm_cells in body["cells"].items():
                rows.append(
                    [f"`{arm_key}`"]
                    + [
                        cell_text(arm_cells.get(g, {}).get("metrics", {}).get(metric, {}))
                        for g in groups
                    ]
                )
            out += [f"### {metric}", "", table(rows, ["Arm", *groups]), ""]
        out += ["## Pre-registered directions", ""]
        rows = []
        for d in body["directions"]:
            ci = d.get("ci")
            rows.append(
                [
                    d["id"],
                    d["group"],
                    d["expect"],
                    fmt(d.get("estimate")),
                    f"[{fmt(ci[0])}, {fmt(ci[1])}]" if ci else "[n/a]",
                    d["label"],
                ]
            )
        out += [
            table(rows, ["Direction", "Group", "Expected", "Estimate", "95% interval", "Label"]),
            "",
        ]
        out += [
            "A label describes where the interval lies relative to the registered direction; it is not a "
            "finding while the matrix is incomplete.",
            "",
        ]
        if report["excluded"]:
            out += ["## Excluded cells", ""]
            reasons: dict[str, int] = {}
            for e in report["excluded"]:
                if e["cell"].startswith(hypothesis.lower() + "/"):
                    reasons[e["reason"]] = reasons.get(e["reason"], 0) + 1
            out += [f"- {r}: {n}" for r, n in sorted(reasons.items())] or [
                "- none in this hypothesis"
            ]
            out.append("")
    out += ["## Deviations from pre-registration", ""]
    devs = list((camp.get("deviations") or {}).get("items", [])) if camp else []
    if counts.get("registered") and not complete:
        devs.append(
            f"The arm matrix is incomplete ({counts['present']} of {counts['expected']} cells); fewer "
            "seeds or tasks than registered is a recorded deviation, never a pass."
        )
    out += [f"- {d}" for d in devs] or ["- none"]
    out += ["", "## Method", ""]
    if hypothesis == "H3":
        out.append(
            "H3 has no new Build: all three arms run the control refworld Build and differ only in the "
            "ledger tail K the Instance is created with (5, 20 or the whole ledger), recorded in every ledger "
            "header. Rule reuse (primary) is defined in spec/cwr/experiments.md section 4 and computed by "
            "benchmark/harness/metrics.py, whose sha256 is pre-registered. The cross-hypothesis deltas "
            "(long bin versus short bin for H1 and H2) are read from the H1 and H2 ledgers of task 5.2."
        )
    else:
        out.append(
            "Each upgrade pair runs its post-upgrade task: the Instance runs on Build A until the frozen "
            "midpoint, is upgraded to Build B by `cwr instance upgrade`, and continues. The changelog arm "
            "upgrades into the control Build B; the version-only and silent arms upgrade into Build B "
            "compiled from the same source with `--x-version-only` or `--x-silent-change`, which change only "
            "the rendering of the recompile notice. U6 (documentation only) is the notice-noise control and "
            "is excluded from the pooled group."
        )
    out.append("")
    return "\n".join(out)


def analyse_and_report(args: argparse.Namespace) -> int:
    worst = 0
    for hypothesis in ("H3", "H4"):
        out = RESULTS / hypothesis.lower() / "analysis.json"
        report = None
        if result_cells(RESULTS / hypothesis.lower()):
            code, text = run_analyse(out)
            print(f"{hypothesis}: analyse.py exit {code}: {text[-300:]}")
            worst = max(worst, code)
            if code in (0, 5) and out.exists():
                report = json.loads(out.read_text(encoding="utf-8"))
        REPORTS[hypothesis].parent.mkdir(parents=True, exist_ok=True)
        REPORTS[hypothesis].write_text(render_report(hypothesis, report), encoding="utf-8")
        print(f"wrote {rel(REPORTS[hypothesis])}")
    return worst


# ---------------------------------------------------------------------------------------------
# Kernel probes for clauses 5.3 and 10.3 (no model: reference turn scripts)


class Probe:
    """Drives the real kernel through the harness binding (benchmark/harness/run.py CwrCliKernel)."""

    def __init__(self):
        from benchmark.harness import run as harness

        self.harness = harness
        self.kernel = kernel(home=scratch_root() / "probe-home")

    def script(self, pair_dir: Path, rel_path: str) -> str:
        text = (pair_dir / rel_path).read_text(encoding="utf-8")
        return "```python\n" + text + "```\n"

    def run_pair(self, pair: str, arm: str, camp: dict[str, Any]) -> dict[str, Any]:
        up = upgrades()
        body, task = camp[f"pair.{pair}"], up[f"task.{pair}"]
        pair_dir = REPO / up[f"pair.{pair}"]["task_dir"]
        pair_dir = pair_dir.parent
        k = self.kernel
        instance = k.create_instance(
            build=body["build_a"],
            seed=1,
            tail=20,
            arm=f"H4/{arm}",
            model="probe:reference-scripts",
            allow_experimental=arm != "changelog",
            initial_state=REPO / body["initial"],
        )
        k.bind_cassette(instance, cassette_for(camp, body["task"], 1))
        try:
            contexts, envelopes = [], []
            for script in task["first_half"]:
                contexts.append(k.context(instance))
                envelopes.append(k.turn(instance, self.script(pair_dir, script)))
            k.upgrade(instance, body["build_b"][arm], allow_experimental=arm != "changelog")
            for script in task["post_upgrade"]:
                contexts.append(k.context(instance))
                envelopes.append(k.turn(instance, self.script(pair_dir, script)))
            contexts.append(k.context(instance))
            ledger = k.ledger(instance)
            state = k.state(instance)
        finally:
            with contextlib.suppress(Exception):  # destroy failure leaves the evidence intact
                k.destroy(instance)
        return {
            "contexts": contexts,
            "envelopes": envelopes,
            "ledger": ledger,
            "state": state,
            "first_after": len(task["first_half"]),
        }


def notice_block(context: str) -> str:
    match = HEADER_LINE.search(context)
    if not match:
        return ""
    tail = context[match.start() :]
    end = tail.find("The manual above describes")
    if end >= 0:
        end = tail.find("\n", end)
        return tail[: end if end >= 0 else len(tail)]
    return tail.split("\n\n", 1)[0]


# Ledger fields that legitimately differ between the control and an experimental arm: host fields,
# identities, the Build hash and everything hashed over it (the record chain, the snapshot and the
# stored notice, whose header names Build B), and the experimental stamp itself.
ARM_FIELDS = {
    "host_ts",
    "duration_us",
    "instance",
    "build",
    "experimental",
    "arm",
    "model",
    "chain",
    "new_build",
    "old_build",
    "snapshot",
    "notice_hash",
}


def normalise_record(record: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in record.items() if k not in ARM_FIELDS}


NOTICE_SECTION = re.compile(r"==== cwr context: recompile notice ====\n.*?(?=====|\Z)", re.S)
HOST_NUMBERS = re.compile(r'"(cpu_ms|wall_ms)":\d+')


def context_without_notice(context: str) -> str:
    """Component 2 removed, Build hashes and host durations masked: what must equal across arms."""
    text = NOTICE_SECTION.sub("", context)
    block = notice_block(text)
    if block:
        text = text.replace(block, "")
    text = HOST_NUMBERS.sub(r'"\1":0', text)
    return re.sub(r"sha256:[0-9a-f]{64}", "H", text)


def normalise_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    env = json.loads(json.dumps(envelope))
    if isinstance(env.get("world"), dict):
        env["world"].pop("build", None)
    if isinstance(env.get("resources"), dict):
        env["resources"] = {
            k: v for k, v in env["resources"].items() if not k.endswith(("_ms", "_us", "_mb"))
        }
    return env


def contract_diff(a: str, b: str) -> tuple[int, dict[str, Any]]:
    """`cwr diff <control> <experimental>` over the two Build directories, parsed."""
    da, db = build_dir(a), build_dir(b)
    proc = cwr("diff", str(da or a), str(db or b))
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        data = {}
    return proc.returncode, data if isinstance(data, dict) else {}


def diff_problems(data: dict[str, Any], flag: str) -> list[str]:
    """Everything in a control-versus-experimental ContractDiff not attributable to a notice flag."""
    problems = []
    if data.get("entries") or data.get("doc_entries"):
        problems.append(
            f"rule or doc entries {(data.get('entries') or data.get('doc_entries'))[:2]}"
        )
    x = data.get("experimental") or {}
    if x.get("flag") != flag.lstrip("-"):
        problems.append(f"diff flag {x.get('flag')!r}")
    if x.get("attributable") is not True or x.get("unattributable"):
        problems.append(f"unattributable entries {x.get('unattributable')}")
    if x.get("entries") or x.get("doc_entries"):
        problems.append("experimental rule entries")
    kinds = {(e.get("kind"), e.get("target")) for e in x.get("experimental_entries", [])}
    if kinds != {("notice_rendering", "notice")}:
        problems.append(f"experimental entries {sorted(map(str, kinds))} are not the notice marker")
    return problems


# ---------------------------------------------------------------------------------------------
# Clause checks


def check_5_3(camp4: dict[str, Any], probes: dict) -> tuple[bool, str]:
    if not camp4:
        return False, "benchmark/campaigns/h4.kvx is not registered"
    up = upgrades()
    problems, checked = [], 0
    for pair in PAIRS:
        notice = up[f"notice.{pair}"]
        run = probes.get((pair, "changelog"))
        if isinstance(run, str):
            problems.append(f"{pair}: {run}")
            continue
        first = run["contexts"][run["first_after"]]
        later = run["contexts"][run["first_after"] + 1 :]
        block = notice_block(first)
        before = run["contexts"][: run["first_after"]]
        if any(HEADER_LINE.search(c) for c in before):
            problems.append(f"{pair}: a notice appears before the upgrade")
        if not block:
            problems.append(f"{pair}: no notice on the first turn after upgrade")
            continue
        head = HEADER_LINE.search(block)
        if (head.group(1), head.group(2)) != (notice["old_version"], notice["new_version"]):
            problems.append(
                f"{pair}: header versions {head.groups()} are not {notice['old_version']}->{notice['new_version']}"
            )
        if notice["summary"] not in block:
            problems.append(f"{pair}: summary missing from the notice")
        used = {
            r.get("primitive") for r in run["ledger"] if r.get("kind") in ("primitive", "adapter")
        }
        upgrade_seq = next((r["seq"] for r in run["ledger"] if r.get("kind") == "upgrade"), None)
        used_before = {
            r.get("primitive")
            for r in run["ledger"]
            if r.get("kind") in ("primitive", "adapter")
            and upgrade_seq is not None
            and r["seq"] < upgrade_seq
        }
        for target in notice["listed_targets"]:
            affects = any(p == target or (p or "").split(".")[0] == target for p in used_before)
            if affects and target not in block:
                problems.append(f"{pair}: change to used target {target} missing from the notice")
        expected = (
            notice["text"]
            .replace("{hash_a}", camp4[f"pair.{pair}"]["build_a"])
            .replace("{hash_b}", camp4[f"pair.{pair}"]["build_b"]["changelog"])
        )
        exact = expected.strip() == block.strip()
        if any(HEADER_LINE.search(c) for c in later):
            problems.append(
                f"{pair}: the notice is rendered again after the first post-upgrade turn"
            )
        del used
        checked += 1
        if not exact:
            problems.append(
                f"{pair}: rendered notice differs from the frozen text in benchmark/upgrades.kvx"
            )
    ok = not problems and checked == len(PAIRS)
    return ok, (
        f"real kernel, {checked} pairs: notice on first post-upgrade turn with old/new version, summary and "
        f"every used-target change, rendered once, equal to the frozen text"
        if ok
        else "; ".join(problems[:8])
    )


def check_10_3(camp4: dict[str, Any], probes: dict) -> tuple[bool, str]:
    if not camp4:
        return False, "benchmark/campaigns/h4.kvx is not registered"
    problems = []
    for pair in PAIRS:
        body = camp4[f"pair.{pair}"]
        control_b = body["build_b"]["changelog"]
        for arm in ("version-only", "silent"):
            xb = body["build_b"][arm]
            if xb == control_b:
                problems.append(
                    f"{pair}/{arm}: Build B with the flag hashes equal to the control B"
                )
                continue
            man = manifest(xb)
            if man.get("experimental") is not True or man.get("experimental_flag") not in (
                H4_FLAGS[arm],
                H4_FLAGS[arm].lstrip("-"),
            ):
                problems.append(
                    f"{pair}/{arm}: manifest experimental={man.get('experimental')} flag={man.get('experimental_flag')}"
                )
            cdir, xdir = build_dir(control_b), build_dir(xb)
            if cdir is None or xdir is None:
                problems.append(f"{pair}/{arm}: Build directory missing")
                continue
            for name in ("contracts.json", "manual.md"):
                if (cdir / name).read_bytes() != (xdir / name).read_bytes():
                    problems.append(f"{pair}/{arm}: {name} differs from the control Build B")
            code, data = contract_diff(control_b, xb)
            if code != 0:
                problems.append(f"{pair}/{arm}: cwr diff exited {code}")
            for problem in diff_problems(data, H4_FLAGS[arm]):
                problems.append(f"{pair}/{arm}: ContractDiff: {problem}")
            ctl, exp = probes.get((pair, "changelog")), probes.get((pair, arm))
            if isinstance(ctl, str) or isinstance(exp, str) or ctl is None or exp is None:
                problems.append(
                    f"{pair}/{arm}: probe failed: {exp if isinstance(exp, str) else ctl}"
                )
                continue
            if [normalise_record(r) for r in ctl["ledger"]] != [
                normalise_record(r) for r in exp["ledger"]
            ]:
                problems.append(
                    f"{pair}/{arm}: ledger differs from the control arm beyond Build hash and stamp"
                )
            if [normalise_envelope(e) for e in ctl["envelopes"]] != [
                normalise_envelope(e) for e in exp["envelopes"]
            ]:
                problems.append(f"{pair}/{arm}: result envelopes differ from the control arm")
            if ctl["state"] != exp["state"]:
                problems.append(f"{pair}/{arm}: final state differs from the control arm")
            upgrade_seq = next(
                (r["seq"] for r in exp["ledger"] if r.get("kind") == "upgrade"), None
            )
            stamp = H4_FLAGS[arm].lstrip("-")
            after = [r for r in exp["ledger"] if upgrade_seq is not None and r["seq"] > upgrade_seq]
            if (
                upgrade_seq is None
                or not after
                or any((r.get("experimental") or "").lstrip("-") != stamp for r in after)
            ):
                problems.append(
                    f"{pair}/{arm}: post-upgrade records do not all carry the {stamp} stamp"
                )
            first = exp["contexts"][exp["first_after"]]
            block = notice_block(first)
            if arm == "silent" and HEADER_LINE.search(first):
                problems.append(f"{pair}/silent: a notice is rendered")
            if arm == "version-only":
                lines = [ln for ln in block.splitlines() if ln.strip()]
                if len(lines) != 1 or not HEADER_LINE.match(lines[0]):
                    problems.append(
                        f"{pair}/version-only: rendered notice is not the version line alone"
                    )
            ctl_ctx = [context_without_notice(c) for c in ctl["contexts"]]
            exp_ctx = [context_without_notice(c) for c in exp["contexts"]]
            if ctl_ctx != exp_ctx:
                problems.append(f"{pair}/{arm}: context differs from control outside the notice")
    ok = not problems
    return ok, (
        "real compiler and kernel, 6 pairs x 2 flags: Build B differs only by the notice marker "
        "(contracts.json and manual.md byte-identical, ContractDiff notice-only), reference turns give "
        "identical ledgers, envelopes and state, silent renders no notice, version-only the version line alone"
        if ok
        else "; ".join(problems[:8])
    )


def camp4_summary(pair: str) -> str:
    return upgrades()[f"notice.{pair}"]["summary"]


def check_12_2(camp3: dict[str, Any], camp4: dict[str, Any]) -> tuple[bool, str]:
    problems = []
    if not camp3 or not camp4:
        return False, "campaigns not registered (benchmark/campaigns/h3.kvx, h4.kvx)"
    tails = {k: camp3[f"arm.{k}"]["tail"] for k in H3_ARMS}
    if tails != {"k5": 5, "k20": 20, "kfull": "full"}:
        problems.append(f"H3 arms register K {tails}, not 5/20/full")
    if len(camp3["task_set"]["tasks"]) != 60 or set(suite_tasks()) != set(
        camp3["task_set"]["tasks"]
    ):
        problems.append("H3 task set is not the 60-task suite")
    seeds = camp3["seeds"]["seeds"]
    if len(seeds) != 10:
        problems.append(f"{len(seeds)} seeds registered, not 10")
    bins = suite_tasks()
    missing3, bad3, k_ok = 0, [], 0
    for key in H3_ARMS:
        for task in camp3["task_set"]["tasks"]:
            for seed in seeds:
                cell = RESULTS / "h3" / key / task / str(seed)
                if not (cell / "ledger.jsonl").exists():
                    missing3 += 1
                    continue
                head = read_ledger(cell / "ledger.jsonl")[0]
                if (
                    head.get("tail_size") != tails[key]
                    or head.get("arm") != f"H3/{key}"
                    or head.get("seed") != seed
                ):
                    bad3.append(
                        f"h3/{key}/{task}/{seed}: header tail {head.get('tail_size')!r} arm {head.get('arm')!r} seed {head.get('seed')!r}"
                    )
                elif head.get("build") != camp3[f"arm.{key}"]["build"]:
                    bad3.append(
                        f"h3/{key}/{task}/{seed}: header Build is not the registered control Build"
                    )
                else:
                    k_ok += 1
    missing4, bad4 = 0, []
    for key in H4_ARMS:
        for pair in PAIRS:
            body = camp4[f"pair.{pair}"]
            for seed in seeds:
                cell = RESULTS / "h4" / key / body["task"] / str(seed)
                if not (cell / "ledger.jsonl").exists():
                    missing4 += 1
                    continue
                ledger = read_ledger(cell / "ledger.jsonl")
                head = ledger[0]
                if (
                    head.get("arm") != f"H4/{key}"
                    or head.get("seed") != seed
                    or head.get("build") != body["build_a"]
                ):
                    bad4.append(f"h4/{key}/{body['task']}/{seed}: header arm/seed/Build A mismatch")
                    continue
                if not any(
                    json.dumps(r).find(body["build_b"][key]) >= 0
                    for r in ledger
                    if r.get("kind") == "upgrade"
                ):
                    attempt = read_kvx(cell / "attempt.kvx").get("attempt", {})
                    if attempt.get("turns", 0) > body["upgrade_after_turn"]:
                        bad4.append(
                            f"h4/{key}/{body['task']}/{seed}: no upgrade record into the registered Build B"
                        )
    analyses_ok = True
    for hyp, groups in (("H3", list(BINS)), ("H4", [*PAIRS, "pooled"])):
        path = RESULTS / hyp.lower() / "analysis.json"
        if not path.exists():
            problems.append(f"{hyp}: no analysis report")
            analyses_ok = False
            continue
        body = json.loads(path.read_text(encoding="utf-8"))["hypotheses"].get(hyp, {})
        arms_expected = H3_ARMS if hyp == "H3" else H4_ARMS
        for arm in arms_expected:
            for g in groups:
                if g not in body.get("cells", {}).get(arm, {}):
                    problems.append(f"{hyp}: analysis lacks arm {arm} group {g}")
                    analyses_ok = False
    total3, total4 = 3 * 60 * len(seeds), 3 * 6 * len(seeds)
    if missing3:
        problems.append(f"H3 matrix incomplete: {total3 - missing3} of {total3} cells")
    if missing4:
        problems.append(f"H4 matrix incomplete: {total4 - missing4} of {total4} cells")
    problems += bad3[:3] + bad4[:3]
    if bad3 or bad4:
        problems.append(f"{len(bad3)} H3 and {len(bad4)} H4 cells with header mismatches")
    ok = not problems
    return ok, (
        f"H3 {total3}/{total3} cells with K recorded in every header ({k_ok} checked) and per-bin per-K analysis; "
        f"H4 {total4}/{total4} cells with Build A header and upgrade into the registered per-arm Build B, per-pair analysis"
        if ok
        else "; ".join(problems[:10])
        + f" (bins registered: {sorted(set(bins.values()))}; analyses present: {analyses_ok})"
    )


def independent_task_success(
    hypothesis: str, camp4: dict[str, Any]
) -> dict[tuple[str, str], list[int]]:
    """Task success per (arm, group) from ledgers and final state only, without analyse.py."""
    out: dict[tuple[str, str], list[int]] = {}
    bins = suite_tasks()
    pair_of = {camp4[f"pair.{p}"]["task"]: p for p in PAIRS} if camp4 else {}
    for cell in result_cells(RESULTS / hypothesis.lower()):
        attempt = read_kvx(cell / "attempt.kvx").get("attempt", {})
        if attempt.get("mode") != "replay" or attempt.get("exit_status") in EXCLUDED_EXITS:
            continue
        ledger = read_ledger(cell / "ledger.jsonl")
        if any((r.get("outcome") or {}).get("code") == "INVARIANT_VIOLATION" for r in ledger):
            continue
        arm, task = cell.parts[-3], cell.parts[-2]
        if hypothesis == "H4":
            group = pair_of.get(task)
            predicate_path = REPO / camp4[f"pair.{group}"]["predicate"]
        else:
            group = bins.get(task)
            predicate_path = BENCH / "tasks" / task / "success.py"
        state = state_view(read_kvx(cell / "final_state.kvx"))
        value = 1 if load_predicate(predicate_path)(state, ledger) is True else 0
        out.setdefault((arm, group), []).append(value)
        if hypothesis == "H4" and group != "U6":
            out.setdefault((arm, "pooled"), []).append(value)
    return out


def check_12_5(camp3: dict[str, Any], camp4: dict[str, Any]) -> tuple[bool, str]:
    problems = []
    prereg = load_prereg(PREREG)
    for hyp in ("H3", "H4"):
        if not prereg.get(f"hypothesis.{hyp}", {}).get("directions"):
            problems.append(f"{hyp}: no pre-registered directions")
    current = sha256_file(METRICS_PY)
    if current != prereg["digests"]["metrics_py"]:
        problems.append("metrics.py differs from the pre-registered digest")
    for hyp, camp in (("H3", camp3), ("H4", camp4)):
        if not camp:
            problems.append(f"{hyp}: not registered")
            continue
        if camp["registration"]["metrics_py"] != current:
            problems.append(f"{hyp}: metrics.py changed after the campaign registration")
        registered_at = camp["meta"]["registered_at"]
        early = []
        for cell in result_cells(RESULTS / hyp.lower()) + sorted(
            p.parent for p in (RESULTS / hyp.lower() / "_pilot").glob("**/attempt.kvx")
        ):
            start = read_kvx(cell / "attempt.kvx").get("attempt", {}).get("start")
            if not start or start < registered_at:
                early.append(rel(cell))
        if early:
            problems.append(
                f"{hyp}: {len(early)} attempts started before registration, e.g. {early[0]}"
            )
        stored = RESULTS / hyp.lower() / "analysis.json"
        if not stored.exists():
            problems.append(f"{hyp}: no stored analysis report")
            continue
        report = json.loads(stored.read_text(encoding="utf-8"))
        body = report["hypotheses"].get(hyp, {})
        groups = list(BINS) if hyp == "H3" else [*PAIRS, "pooled"]
        primary = prereg[f"hypothesis.{hyp}"]["primary"]
        for arm, arm_cells in body.get("cells", {}).items():
            for g in groups:
                s = arm_cells.get(g, {}).get("metrics", {}).get(primary, {})
                if "mean" not in s or "ci" not in s:
                    problems.append(f"{hyp}/{arm}/{g}: no mean and interval for {primary}")
        fresh = scratch_root() / f"fresh-{hyp.lower()}.json"
        code, text = run_analyse(fresh)
        if code not in (0, 5) or not fresh.exists():
            problems.append(f"{hyp}: fresh analyse.py run exited {code}: {text[-200:]}")
            continue
        recomputed = json.loads(fresh.read_text(encoding="utf-8"))
        if recomputed["hypotheses"].get(hyp) != body:
            problems.append(
                f"{hyp}: stored analysis differs from a fresh recomputation from the ledgers"
            )
        if (
            render_report(hyp, report) != REPORTS[hyp].read_text(encoding="utf-8")
            if REPORTS[hyp].exists()
            else True
        ):
            problems.append(
                f"{hyp}: {rel(REPORTS[hyp])} is not the rendering of the stored analysis"
            )
        if render_report(hyp, recomputed) != render_report(hyp, report):
            problems.append(f"{hyp}: report numbers differ from the recomputation")
        indep = independent_task_success(hyp, camp4)
        for (arm, group), values in indep.items():
            s = (
                body.get("cells", {})
                .get(arm, {})
                .get(group, {})
                .get("metrics", {})
                .get("task_success", {})
            )
            mean = sum(values) / len(values)
            if s.get("n") != len(values) or s.get("mean") is None or abs(s["mean"] - mean) > 1e-9:
                problems.append(
                    f"{hyp}/{arm}/{group}: task_success {s.get('mean')} n={s.get('n')} != independent {mean} n={len(values)}"
                )
                break
    ok = not problems
    return ok, (
        "directions registered before every attempt; metrics.py digest unchanged since pre-registration; "
        "mean and 95% bootstrap interval per arm and group; stored reports equal a fresh analyse.py run and "
        "an independent task-success recomputation; research reports render from the recomputed numbers"
        if ok
        else "; ".join(problems[:8])
    )


def verify(args: argparse.Namespace) -> int:
    camp3, camp4 = campaign(H3_KVX), campaign(H4_KVX)
    probes: dict[tuple[str, str], Any] = {}
    if camp4:
        try:
            probe = Probe()
            for pair in PAIRS:
                for arm in H4_ARMS:
                    try:
                        probes[(pair, arm)] = probe.run_pair(pair, arm, camp4)
                    except Exception as error:  # reported as clause evidence
                        probes[(pair, arm)] = f"{type(error).__name__}: {str(error)[:200]}"
        except Exception as error:  # reported as clause evidence
            for pair in PAIRS:
                for arm in H4_ARMS:
                    probes[(pair, arm)] = f"{type(error).__name__}: {str(error)[:200]}"
    results = [
        ("5.3", *check_5_3(camp4, probes)),
        ("10.3", *check_10_3(camp4, probes)),
        ("12.2", *check_12_2(camp3, camp4)),
        ("12.5", *check_12_5(camp3, camp4)),
    ]
    for clause, ok, evidence in results:
        print(f"CLAUSE {clause} {'PASS' if ok else 'FAIL'} {evidence}")
    return 0 if all(ok for _, ok, _ in results) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--register", action="store_true")
    parser.add_argument(
        "--force-reregister-without-results", action="store_true", help=argparse.SUPPRESS
    )
    parser.add_argument("--execute", choices=["h3", "h4"])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--pilot", action="store_true")
    mode.add_argument("--full", action="store_true")
    parser.add_argument("--skip-pilot-gate", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument("--max-cells", type=int, default=0)
    parser.add_argument("--analyse", action="store_true")
    args = parser.parse_args(argv)
    if args.register:
        return register(args)
    if args.execute:
        if not (args.pilot or args.full):
            parser.error("--execute needs --pilot or --full")
        return execute(args)
    if args.analyse:
        return analyse_and_report(args)
    return verify(args)


if __name__ == "__main__":
    sys.exit(main())

# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""A complete, registered analysis workspace in a temporary directory, built from synthetic ledgers."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

from benchmark.harness.ledgerio import REPO, read_kvx, sha256_file, write_kvx, write_ledger
from benchmark.harness.tests.synth import BUILD, CASSETTE, MODEL, LedgerBuilder

ONE_PRIMITIVE_BUILD = "sha256:" + "d" * 64
TASKS = {"t001-short": "short", "t002-medium": "medium", "t003-long": "long"}
PAIRS = {"U1": ("u1-task", ["fs.delete"], "LOCKED"), "U6": ("u6-task", ["fs.write"], None)}
PREDICATE = 'def success(state, ledger):\n    return state.get("task", {}).get("done") == 1\n'
H1_SUCCESS = {
    "frozen": {1: True, 2: True},
    "drift-1": {1: True, 2: False},
    "drift-3": {1: False, 2: False},
}
H4_MISUSE_TURNS = {"changelog": 0, "version-only": 1, "silent": 2}
STAMPS = {
    "drift-1": "x-drift",
    "drift-3": "x-drift",
    "one-primitive": "x-one-primitive",
    "version-only": "x-version-only",
    "silent": "x-silent-change",
}
# Per-task drift Builds (campaigns/h1.kvx) and per-arm B sides of each pair (campaigns/h4.kvx).
DRIFT_BUILDS = {
    (task, arm): "sha256:" + f"{n:x}" * 64
    for n, (task, arm) in enumerate(
        ((t, a) for t in TASKS for a in ("drift-1", "drift-3")), start=1
    )
}
PAIR_BUILDS_B = {
    "changelog": "sha256:" + "b" * 64,
    "version-only": "sha256:" + "7" * 64,
    "silent": "sha256:" + "8" * 64,
}


def suite_digest(tasks: Path) -> str:
    """The suite digest as benchmark/predicates/common.py computes it (suite_digest_scope)."""
    predicates = str(REPO / "benchmark" / "predicates")
    if predicates not in sys.path:
        sys.path.insert(0, predicates)
    import common  # type: ignore[import-not-found]

    return common.suite_digest(tasks)


def _set(text: str, section: str, key: str, value: str) -> str:
    """Replace `key = ...` inside `[section]` of KVX text (line based; values may contain brackets)."""
    lines = text.split("\n")
    inside = False
    for i, line in enumerate(lines):
        if re.fullmatch(r"\[[A-Za-z0-9_.-]+\]", line.strip()):
            inside = line.strip() == f"[{section}]"
            continue
        if inside and line.startswith(f"{key} = "):
            lines[i] = f"{key} = {value}"
            return "\n".join(lines)
    raise AssertionError((section, key))


class Workspace:
    def __init__(self, root: Path, *, resamples: int = 1000, status: str = "registered"):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.results = root / "results"
        self.tasks = root / "tasks"
        self.arms = root / "arms.kvx"
        self.prereg = root / "preregistration.kvx"
        self.suite = root / "suite.kvx"
        self.upgrades = root / "upgrades.kvx"
        self.metrics = REPO / "benchmark" / "harness" / "metrics.py"
        arms = (REPO / "benchmark" / "harness" / "arms.kvx").read_text()
        arms = _set(arms, "control", "build", f'"{BUILD}"')
        arms = _set(arms, "arm.h2.one-primitive", "build", f'"{ONE_PRIMITIVE_BUILD}"')
        self.arms.write_text(arms)
        for task in [*TASKS, *(t for t, _, _ in PAIRS.values())]:
            (self.tasks / task).mkdir(parents=True, exist_ok=True)
            (self.tasks / task / "success.py").write_text(PREDICATE)
        self.suite_digest = suite_digest(self.tasks)
        prereg = (REPO / "benchmark" / "preregistration.kvx").read_text()
        for section, key, value in [
            ("meta", "status", f'"{status}"'),
            ("campaign", "model", f'"{MODEL}"'),
            ("campaign", "sampling", "{}"),
            ("campaign", "seeds", "[1, 2]"),
            ("campaign", "suite_digest", f'"{self.suite_digest}"'),
            ("campaign", "cassette_digest", f'"{CASSETTE}"'),
            ("hypothesis.H4", "cassette_digest", f'"{CASSETTE}"'),
            ("campaign", "upgrades_digest", '"sha256:' + "f" * 64 + '"'),
            ("analysis", "resamples", str(resamples)),
            ("digests", "metrics_py", f'"{sha256_file(self.metrics)}"'),
            ("digests", "arms_kvx", f'"{sha256_file(self.arms)}"'),
        ]:
            prereg = _set(prereg, section, key, value)
        self.prereg.write_text(prereg)
        # The committed suite layout: the digest lives in [suite], never in [meta].
        suite: dict[str, dict[str, Any]] = {
            "meta": {"format": "cwr.suite", "schema_version": 1},
            "suite": {"task_count": len(TASKS), "suite_digest": self.suite_digest},
        }
        for task, bin_name in TASKS.items():
            reuse = bin_name == "long"
            suite[f"task.{task}"] = {
                "bin": bin_name,
                "rule_reuse": reuse,
                "reused_rule": {"primitive": "jobs.result", "code": "CONFLICT"} if reuse else None,
            }
        write_kvx(self.suite, suite)
        write_kvx(
            self.upgrades,
            {
                f"pair.{p}": {"task": t, "changed_primitives": c, "misuse_code": code}
                for p, (t, c, code) in PAIRS.items()
            },
        )
        self.campaigns = root / "campaigns"
        self.write_campaigns()

    def write_campaigns(self, **digests: str) -> None:
        """campaigns/h1.kvx and h4.kvx as the campaign drivers register them."""
        registration = {
            "metrics_py": digests.get("metrics_py", sha256_file(self.metrics)),
            "arms_kvx": digests.get("arms_kvx", sha256_file(self.arms)),
        }
        h1: dict[str, dict[str, Any]] = {
            "meta": {"format": "cwr.campaign", "hypothesis": "H1"},
            "registration": dict(registration),
        }
        for task in TASKS:
            h1[f"task.{task}"] = {
                f"{arm.replace('-', '_')}_build": DRIFT_BUILDS[(task, arm)]
                for arm in ("drift-1", "drift-3")
            }
        write_kvx(self.campaigns / "h1.kvx", h1)
        h4: dict[str, dict[str, Any]] = {
            "meta": {"format": "cwr.campaign", "hypothesis": "H4"},
            "registration": dict(registration),
        }
        for pair, (task, _, _) in PAIRS.items():
            h4[f"pair.{pair}"] = {"task": task, "build_a": BUILD, "build_b": PAIR_BUILDS_B}
        write_kvx(self.campaigns / "h4.kvx", h4)

    def argv(self, *extra: str) -> list[str]:
        return [
            "--results",
            str(self.results),
            "--prereg",
            str(self.prereg),
            "--arms",
            str(self.arms),
            "--metrics",
            str(self.metrics),
            "--suite",
            str(self.suite),
            "--upgrades",
            str(self.upgrades),
            "--tasks-dir",
            str(self.tasks),
            "--campaigns",
            str(self.campaigns),
            *extra,
        ]

    def write_cell(
        self,
        hyp: str,
        arm: str,
        task: str,
        seed: int,
        ledger: list,
        state: dict,
        live: bool = False,
        **attempt: Any,
    ) -> Path:
        root = self.results / "live" if live else self.results
        cell = root / hyp.lower() / arm / task / str(seed)
        write_ledger(cell / "ledger.jsonl", ledger)
        write_kvx(cell / "final_state.kvx", state)
        record = {
            "hypothesis": hyp,
            "arm": f"{hyp}/{arm}",
            "task": task,
            "seed": seed,
            "mode": "live" if live else "replay",
            "registered": True,
            "exit_status": "success",
            "cassette_digest": CASSETTE,
            "tokens_input_total": 100,
            "tokens_output_total": 10,
        }
        record.update(attempt)
        write_kvx(cell / "attempt.kvx", {"attempt": record})
        return cell

    # -- synthetic campaigns ---------------------------------------------------------------------

    def h1_ledger(self, arm: str, task: str, seed: int, success: bool) -> list:
        build = DRIFT_BUILDS.get((task, arm), BUILD)
        b = LedgerBuilder(arm=f"H1/{arm}", seed=seed, stamp=STAMPS.get(arm), build=build)
        if TASKS[task] == "long":
            b.call("jobs.result", "CONFLICT").call("jobs.result").turn("error")
            b.calls("fs.write", 10, "mutating").turn()
            if arm != "frozen":
                b.drift()
            b.call("jobs.result", None if success else "CONFLICT").turn(
                "ok" if success else "error"
            )
        else:
            b.call("fs.exists").call("fs.delete", None if success else "LOCKED", "mutating").turn(
                "ok" if success else "error"
            )
            if not success:
                b.call("fs.delete", "LOCKED", "mutating").turn("error")
        return b.build_ledger()

    def populate_h1(self) -> None:
        for arm, outcomes in H1_SUCCESS.items():
            for task in TASKS:
                for seed, success in outcomes.items():
                    self.write_cell(
                        "H1",
                        arm,
                        task,
                        seed,
                        self.h1_ledger(arm, task, seed, success),
                        {"task": {"done": 1 if success else 0}},
                        exit_status="success" if success else "turn_limit",
                        drift_spec=f"{task}-spec" if arm != "frozen" else None,
                    )

    def h4_ledger(self, arm: str, pair: str, seed: int) -> list:
        _task, changed, code = PAIRS[pair]
        b = LedgerBuilder(arm=f"H4/{arm}", seed=seed)
        b.call("fs.write", side_effect="mutating").turn()
        b.upgrade(new_build=PAIR_BUILDS_B[arm], stamp=STAMPS.get(arm))
        for turn in range(2):
            if code and turn < H4_MISUSE_TURNS[arm]:
                b.call(changed[0], code, "mutating").turn("error")
            else:
                b.call(changed[0], side_effect="mutating").turn()
        return b.build_ledger()

    def populate_h4(self) -> None:
        for arm in H4_MISUSE_TURNS:
            for pair, (task, _, _) in PAIRS.items():
                for seed in (1, 2):
                    self.write_cell(
                        "H4",
                        arm,
                        task,
                        seed,
                        self.h4_ledger(arm, pair, seed),
                        {"task": {"done": 1}},
                        pair=pair,
                    )


def load_report(path: Path) -> dict:
    import json

    return json.loads(path.read_text())


def kvx(path: Path) -> dict:
    return read_kvx(path)

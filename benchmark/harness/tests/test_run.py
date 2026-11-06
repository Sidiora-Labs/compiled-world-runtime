# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Runner episode loop against an in-memory kernel and a scripted model; then recomputation by analyse.py."""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

# Make `benchmark` importable when this file runs directly under python3 -I.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from benchmark import analyse
from benchmark.harness import metrics, run
from benchmark.harness.arms import load_arms
from benchmark.harness.ledgerio import read_kvx, read_ledger
from benchmark.harness.tests.synth import BUILD, BUILD_B, FakeKernel, FakeModel, progress_predicate
from benchmark.harness.tests.workspace import Workspace

_, ARMS = load_arms()
STEP1 = "CALL fs.exists ok read_only\nSET task progress 1"
STEP2 = "CALL fs.write ok mutating\nSET task progress 2"


def spec(root, hypothesis="H1", arm="frozen", task="t-short", seed=1, limit=5, **kw):
    return run.EpisodeSpec(
        hypothesis=hypothesis,
        arm=ARMS[(hypothesis, arm)],
        task=task,
        seed=seed,
        build=BUILD,
        predicate_path=Path("unused"),
        turn_limit=limit,
        results_root=root,
        **kw,
    )


class Episode(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_stops_at_first_success_and_writes_layout(self):
        kernel, model = FakeKernel(), FakeModel([STEP1, STEP2, STEP2])
        attempt = run.run_episode(spec(self.root), kernel, model, progress_predicate(2))
        cell = self.root / "h1" / "frozen" / "t-short" / "1"
        self.assertEqual((attempt["exit_status"], attempt["turns"]), ("success", 2))
        self.assertEqual(model.calls, 2)
        ledger = read_ledger(cell / "ledger.jsonl")
        self.assertEqual(ledger, kernel.ledger(next(iter(kernel.instances))))
        self.assertEqual(ledger[0]["arm"], "H1/frozen")
        self.assertEqual(ledger[0]["model"], "test-model")
        self.assertEqual(read_kvx(cell / "final_state.kvx"), {"task": {"progress": 2}})
        self.assertEqual(
            sorted(p.name for p in (cell / "states").iterdir()), ["turn_0001.kvx", "turn_0002.kvx"]
        )
        record = read_kvx(cell / "attempt.kvx")
        self.assertEqual(record["attempt"]["mode"], "replay")
        self.assertEqual(record["attempt"]["tokens_input_total"], 200)
        self.assertEqual(len(record["tokens"]["per_turn"]), 2)
        self.assertEqual(len(kernel.destroyed), 1)
        self.assertTrue(all(c.startswith("MANUAL") for c in model.contexts))

    def test_turn_limit(self):
        attempt = run.run_episode(
            spec(self.root, limit=3), FakeKernel(), FakeModel([STEP1]), progress_predicate(2)
        )
        self.assertEqual((attempt["exit_status"], attempt["turns"]), ("turn_limit", 3))

    def test_token_budget(self):
        attempt = run.run_episode(
            spec(self.root, token_budget=150),
            FakeKernel(),
            FakeModel([STEP1]),
            progress_predicate(2),
        )
        self.assertEqual((attempt["exit_status"], attempt["turns"]), ("budget", 2))

    def test_model_error_ends_run_and_is_recorded(self):
        attempt = run.run_episode(
            spec(self.root), FakeKernel(), FakeModel([STEP1], fail_at=2), progress_predicate(2)
        )
        self.assertEqual((attempt["exit_status"], attempt["turns"]), ("model_error", 1))
        self.assertIn("gateway unavailable", attempt["detail"])

    def test_invariant_violation_stops(self):
        model = FakeModel(["CALL fs.write INVARIANT_VIOLATION mutating"])
        attempt = run.run_episode(spec(self.root), FakeKernel(), model, progress_predicate(2))
        self.assertEqual(attempt["exit_status"], "invariant_violation")

    def test_live_mode_is_labelled_and_separated(self):
        attempt = run.run_episode(
            spec(self.root), FakeKernel("live"), FakeModel([STEP2]), progress_predicate(2)
        )
        self.assertEqual(attempt["mode"], "live")
        self.assertTrue(
            (self.root / "live" / "h1" / "frozen" / "t-short" / "1" / "ledger.jsonl").exists()
        )
        self.assertFalse((self.root / "h1").exists())

    def test_record_mode_refused(self):
        with self.assertRaises(ValueError):
            run.run_episode(
                spec(self.root), FakeKernel("record"), FakeModel([STEP2]), progress_predicate(2)
            )

    def test_experimental_arm_requests_allow_experimental(self):
        kernel = FakeKernel()
        run.run_episode(
            spec(self.root, arm="drift-1"), kernel, FakeModel([STEP2]), progress_predicate(2)
        )
        instance = next(iter(kernel.instances.values()))
        self.assertTrue(instance["allow"])
        self.assertTrue(all(r["experimental"] == "x-drift" for r in instance["ledger"].records))

    def test_upgrade_between_turns(self):
        plan = run.UpgradePlan(after_turn=1, build_b=BUILD_B, pair="U1")
        attempt = run.run_episode(
            spec(self.root, hypothesis="H4", arm="changelog", task="u1-task", upgrade=plan),
            FakeKernel(),
            FakeModel([STEP1, STEP2]),
            progress_predicate(2),
        )
        ledger = read_ledger(self.root / "h4" / "changelog" / "u1-task" / "1" / "ledger.jsonl")
        kinds = [r["kind"] for r in ledger]
        self.assertEqual(kinds.index("upgrade"), kinds.index("turn") + 1)
        self.assertEqual(attempt["pair"], "U1")


class Recomputation(unittest.TestCase):
    """Metrics recomputed by analyse.py from the runner's ledgers equal metrics computed directly."""

    def test_runner_output_recomputes(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            ws = Workspace(tmp)
            scripts = {
                1: [STEP1, STEP2],
                2: [
                    "CALL fs.delete LOCKED mutating",
                    STEP1,
                    "CALL fs.read NOT_FOUND read_only",
                    STEP2,
                ],
            }

            def pred(state, ledger):
                return state.get("task", {}).get("progress", 0) >= 2

            direct = {}
            for seed, steps in scripts.items():
                run.run_episode(
                    spec(ws.results, seed=seed, limit=10), FakeKernel(), FakeModel(steps), pred
                )
                cell = ws.results / "h1" / "frozen" / "t-short" / str(seed)
                direct[seed] = metrics.instance_metrics(
                    read_ledger(cell / "ledger.jsonl"), read_kvx(cell / "final_state.kvx"), pred
                )
            (ws.tasks / "t-short" / "success.py").write_text(
                'def success(state, ledger):\n    return state.get("task", {}).get("progress", 0) >= 2\n'
            )
            out = tmp / "r.json"
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
                code = analyse.main(ws.argv("--out", str(out)))
            self.assertEqual(code, 0, err.getvalue())
            cell = json.loads(out.read_text())["hypotheses"]["H1"]["cells"]["frozen"]["short"]
            self.assertEqual(cell["instances"], 2)
            self.assertEqual(cell["turns_to_success_method"], ["per_turn_state"])
            for name in (
                "turns_to_success",
                "primitive_calls",
                "repeated_failure_rate",
                "failure_rate.LOCKED",
            ):
                values = [direct[s][name] for s in (1, 2) if direct[s][name] is not None]
                self.assertAlmostEqual(
                    cell["metrics"][name]["mean"], sum(values) / len(values), msg=name
                )
            self.assertEqual(cell["metrics"]["turns_to_success"]["mean"], 3.0)  # (2 + 4) / 2
        finally:
            shutil.rmtree(tmp)


class CommandLine(unittest.TestCase):
    def test_cwr_binary_honours_cargo_target_dir(self):
        with mock.patch.dict(os.environ, {"CARGO_TARGET_DIR": "/tmp/x-target"}, clear=False):
            os.environ.pop("CWR_BIN", None)
            self.assertEqual(run.cwr_binary(), Path("/tmp/x-target/debug/cwr"))
        with mock.patch.dict(os.environ, {"CWR_BIN": "/opt/cwr"}):
            self.assertEqual(run.cwr_binary(), Path("/opt/cwr"))

    def test_cli_kernel_names_a_missing_binary(self):
        with self.assertRaisesRegex(run.KernelCommandError, "cwr binary not found"):
            run.CwrCliKernel(binary=Path("/nonexistent/cwr")).create_instance(
                build=BUILD,
                seed=1,
                tail=20,
                arm="H1/frozen",
                model="m",
                allow_experimental=False,
                initial_state=None,
            )

    def test_episode_without_a_kernel_writes_nothing(self):
        root = Path(tempfile.mkdtemp())
        try:
            kernel = run.CwrCliKernel(binary=Path("/nonexistent/cwr"))
            with self.assertRaises(run.KernelCommandError):
                run.run_episode(spec(root), kernel, FakeModel([STEP2]), progress_predicate(2))
            self.assertEqual(list(root.iterdir()), [])
        finally:
            shutil.rmtree(root)


class CliBindingHelpers(unittest.TestCase):
    def test_extract_blocks(self):
        one = "say\n```python\nimport fs\n```\nbye"
        self.assertEqual(run.extract_blocks(one), [("python", "import fs\n")])
        self.assertEqual(len(run.extract_blocks(one + "\n```python\nx\n```")), 2)
        self.assertEqual(run.extract_blocks("```python\nopen"), [("python", None)])
        self.assertEqual(run.extract_blocks("no fence"), [])

    def test_state_round_trip_between_view_and_kernel(self):
        view = {
            "fs": {"files": {"/a": "text", "/b": {"base64": "/w=="}}, "locks": ["/a"]},
            "kv": {"namespaces": {"n": {"k": 1}}},
            "jobs": {"jobs": {"j4": {"kind": "w", "n": 5, "submitted_at": -2, "fail_at": 9}}},
        }
        kernel = run.kernel_initial_state(view)
        self.assertEqual(kernel["fs"]["files"]["/a"], {"$bytes": "dGV4dA=="})
        self.assertEqual(kernel["jobs"]["next_id"], 5)
        back = run.predicate_state(kernel, {"time": 4, "turn": 1}, [], [{"seq": 0}])
        self.assertEqual(back["fs"], view["fs"])
        self.assertEqual(back["kv"], view["kv"])
        self.assertEqual(back["jobs"]["jobs"]["j4"]["status"], "done")
        self.assertEqual(back["jobs"]["jobs"]["j4"]["result"], "w:j4:5")
        self.assertEqual(back["world"], {"time": 4, "turn": 1})

    def test_idempotency_store_in_the_view(self):
        events = [
            {"event": "pending", "adapter": "publish.post", "key": "k", "request_hash": "h"},
            {
                "event": "done",
                "adapter": "publish.post",
                "key": "k",
                "payload_hash": "p",
                "request_hash": "h",
                "outcome": {"post_id": "x"},
            },
        ]
        ledger = [
            {"seq": 0, "kind": "header"},
            {
                "seq": 1,
                "kind": "adapter",
                "primitive": "publish.post",
                "gateway": {"request_hash": "h"},
            },
        ]
        view = run.predicate_state({}, {"time": 1, "turn": 1}, events, ledger)
        self.assertEqual(
            view["gateway"]["idempotency"]["publish.post"]["k"],
            {"receipt": {"post_id": "x"}, "payload_hash": "p", "seq": 1},
        )

    def test_arm_build_must_carry_exactly_the_arm_flag(self):
        root = Path(tempfile.mkdtemp())
        try:
            manifest = '[build]\nname = "refworld"\nexperimental = %s\nexperimental_flag = %s\n'
            (root / "manifest.kvx").write_text(manifest % ("false", "null"))
            self.assertIsNone(run.check_arm_build(ARMS[("H2", "script")], root))
            self.assertIsNotNone(run.check_arm_build(ARMS[("H2", "one-primitive")], root))
            (root / "manifest.kvx").write_text(manifest % ("true", '"x-one-primitive"'))
            self.assertIsNone(run.check_arm_build(ARMS[("H2", "one-primitive")], root))
            self.assertIsNotNone(run.check_arm_build(ARMS[("H2", "script")], root))
        finally:
            shutil.rmtree(root)


if __name__ == "__main__":
    unittest.main()

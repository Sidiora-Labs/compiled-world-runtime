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
from benchmark.harness.tests.workspace import Workspace, suite_digest

_, ARMS = load_arms()


def replay_kernel(home, roots):
    """The in-memory kernel as analyse.py's replay binding."""
    del home, roots
    return FakeKernel()


STEP1 = "CALL fs.exists ok read_only\nSET task progress 1"
STEP2 = "CALL fs.write ok mutating\nSET task progress 2"


def spec(root, hypothesis="H1", arm="frozen", task="t001-short", seed=1, limit=5, **kw):
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
        cell = self.root / "h1" / "frozen" / "t001-short" / "1"
        self.assertEqual((attempt["exit_status"], attempt["turns"]), ("success", 2))
        self.assertEqual(model.calls, 2)
        ledger = read_ledger(cell / "ledger.jsonl")
        self.assertEqual(ledger, kernel.ledger(next(iter(kernel.instances))))
        self.assertEqual(ledger[0]["arm"], "H1/frozen")
        self.assertEqual(ledger[0]["model"], "test-model")
        self.assertEqual(read_kvx(cell / "final_state.kvx"), {"task": {"progress": 2}})
        self.assertFalse((cell / "states").exists())
        messages = sorted((cell / "messages").iterdir())
        self.assertEqual([p.name for p in messages], ["turn_0001.md", "turn_0002.md"])
        self.assertEqual(messages[1].read_text(), f"```python\n{STEP2}\n```")
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
        # Only the error class is recorded; an arbitrary exception message may name an endpoint.
        self.assertEqual(attempt["detail"], "model_error: unexpected RuntimeError")
        self.assertNotIn("gateway unavailable", attempt["detail"])

    def test_model_error_records_category_and_status_only(self):
        from benchmark.harness.model import ModelError

        class Failing(FakeModel):
            def complete_context(self, context, *, max_tokens, params=None):
                raise ModelError("transport", "URLError (gaierror) at gateway.invalid", None, 8)

        attempt = run.run_episode(spec(self.root), FakeKernel(), Failing([]), progress_predicate(2))
        self.assertEqual(attempt["detail"], "model_error: transport, status None, attempts 8")
        record = (self.root / "h1" / "frozen" / "t001-short" / "1" / "attempt.kvx").read_text()
        self.assertNotIn("gateway.invalid", record)

    def test_reported_model_identity_must_match(self):
        class Rerouted(FakeModel):
            def complete_context(self, context, *, max_tokens, params=None):
                reply = super().complete_context(context, max_tokens=max_tokens, params=params)
                reply.model = "some-other-model"
                return reply

        attempt = run.run_episode(
            spec(self.root), FakeKernel(), Rerouted([STEP2]), progress_predicate(2)
        )
        self.assertEqual(attempt["exit_status"], "model_error")
        self.assertIn("identity", attempt["detail"])
        self.assertIs(attempt["model_reported_match"], False)
        self.assertNotIn("some-other-model", json.dumps(attempt))

    def test_routing_prefix_rule_accepts_the_gateway_identity(self):
        self.assertTrue(run.model_matches("m-1", "cx/m-1", "routing_prefix"))
        self.assertFalse(run.model_matches("m-1", "cx/m-1", "exact"))
        self.assertFalse(run.model_matches("m-2", "cx/m-1", "routing_prefix"))
        self.assertTrue(run.model_matches("cx/m-1", "cx/m-1", "exact"))
        model = FakeModel([STEP2], model="cx/test-model")

        class Prefixless(FakeModel):
            def complete_context(self, context, *, max_tokens, params=None):
                reply = model.complete_context(context, max_tokens=max_tokens, params=params)
                reply.model = "test-model"
                return reply

        prefixless = Prefixless([STEP2], model="cx/test-model")
        attempt = run.run_episode(
            spec(self.root, model_rule="routing_prefix"),
            FakeKernel(),
            prefixless,
            progress_predicate(2),
        )
        self.assertEqual(attempt["exit_status"], "success")
        self.assertIs(attempt["model_reported_match"], True)

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
            (self.root / "live" / "h1" / "frozen" / "t001-short" / "1" / "ledger.jsonl").exists()
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


class CellSafety(unittest.TestCase):
    """Existing cells are never deleted, the Instance is always destroyed, the record always written."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.cell = self.root / "h1" / "frozen" / "t001-short" / "1"

    def tearDown(self):
        shutil.rmtree(self.root)

    def run_ok(self, steps=(STEP1, STEP2), **kw):
        return run.run_episode(
            spec(self.root, **kw), FakeKernel(), FakeModel(list(steps)), progress_predicate(2)
        )

    def test_finished_cell_is_refused_without_rerun_and_moved_aside_with_it(self):
        self.run_ok()
        first = (self.cell / "attempt.kvx").read_bytes()
        kernel = FakeKernel()
        with self.assertRaises(run.CellExists):
            run.run_episode(spec(self.root), kernel, FakeModel([STEP2]), progress_predicate(2))
        self.assertEqual(kernel.instances, {})
        self.assertEqual((self.cell / "attempt.kvx").read_bytes(), first)
        self.run_ok(rerun=True)
        aside = list((self.root / "_replaced" / "h1" / "frozen" / "t001-short").iterdir())
        self.assertEqual(len(aside), 1)
        self.assertEqual((aside[0] / "attempt.kvx").read_bytes(), first)
        self.assertEqual(list((self.root / "_staging").iterdir()), [])

    def test_retryable_cell_is_replaced_and_kept_aside(self):
        run.run_episode(
            spec(self.root), FakeKernel(), FakeModel([STEP1], fail_at=1), progress_predicate(2)
        )
        self.assertEqual(run.cell_status(self.cell), "model_error")
        self.run_ok()
        self.assertEqual(run.cell_status(self.cell), "success")
        self.assertTrue((self.root / "_replaced").is_dir())

    def test_unsafe_cell_identity_refused_before_any_instance(self):
        kernel = FakeKernel()
        for task in ("../escape", "a/b", "..", ""):
            with self.assertRaises(ValueError):
                run.run_episode(
                    spec(self.root, task=task), kernel, FakeModel([STEP2]), progress_predicate(2)
                )
        self.assertEqual(kernel.instances, {})
        self.assertEqual(list(self.root.iterdir()), [])

    def test_export_failure_still_destroys_the_instance_and_records(self):
        class BadExport(FakeKernel):
            def export_ledger(self, instance, path):
                raise ValueError("truncated ledger")

        kernel = BadExport()
        with self.assertRaisesRegex(RuntimeError, "export: ValueError"):
            run.run_episode(spec(self.root), kernel, FakeModel([STEP2]), progress_predicate(2))
        self.assertEqual(len(kernel.destroyed), 1)
        self.assertEqual(run.cell_status(self.cell), "kernel_error")

    def test_kvx_round_trip_failure_text_still_records(self):
        class Breaker(FakeKernel):
            def state(self, instance):
                return {"fs": {"files": {"/note": "line\x85next\u2028end"}}}

        def done(state, ledger):
            return True

        kernel = Breaker()
        attempt = run.run_episode(spec(self.root), kernel, FakeModel([STEP2]), done)
        self.assertEqual(attempt["exit_status"], "success")
        self.assertEqual(len(kernel.destroyed), 1)
        state = read_kvx(self.cell / "final_state.kvx")
        self.assertEqual(state["fs"]["files"]["/note"], "line\x85next\u2028end")

    def test_metric_failure_is_recorded_as_metrics_error(self):
        with mock.patch.object(
            metrics, "instance_metrics", side_effect=metrics.MetricInputError("no state")
        ):
            attempt = self.run_ok()
        self.assertEqual(attempt["exit_status"], "metrics_error")
        self.assertIn("after success", attempt["detail"])
        self.assertTrue((self.cell / "attempt.kvx").is_file())

    def test_predicate_exception_is_not_a_kernel_error(self):
        def broken(state, ledger):
            raise KeyError("task")

        kernel = FakeKernel()
        attempt = run.run_episode(spec(self.root), kernel, FakeModel([STEP2]), broken)
        self.assertEqual(attempt["exit_status"], "predicate_error")
        self.assertEqual(len(kernel.destroyed), 1)

    def test_reply_without_text_is_a_model_error(self):
        class Empty(FakeModel):
            def complete_context(self, context, *, max_tokens, params=None):
                reply = super().complete_context(context, max_tokens=max_tokens, params=params)
                reply.text = None
                return reply

        attempt = run.run_episode(
            spec(self.root), FakeKernel(), Empty([STEP2]), progress_predicate(2)
        )
        self.assertEqual(attempt["exit_status"], "model_error")

    def test_attempt_carries_revisions_log_and_digests(self):
        digests = {"preregistration": "sha256:1", "metrics_py": "sha256:2", "arms_kvx": "sha256:3"}
        attempt = self.run_ok(
            registration_digests=digests,
            build_revisions={"compiler": "cwr-compiler 9", "sdk": "sha256:5"},
        )
        self.assertEqual(attempt["revision_compiler"], "cwr-compiler 9")
        self.assertEqual(attempt["revision_sdk"], "sha256:5")
        self.assertEqual(attempt["revision_kernel"], "test")
        self.assertEqual(attempt["registration_digests"], digests)
        self.assertEqual(Path(attempt["log"]), (self.cell / "kernel.log").resolve())
        self.assertIn("## turn 2", (self.cell / "kernel.log").read_text())


class Preflight(unittest.TestCase):
    """The one pre-flight every entry point runs (run.py main, h1h2.py, the task 5.3 driver)."""

    def setUp(self):
        from benchmark.harness.tests.workspace import (
            DRIFT_BUILDS,
            ONE_PRIMITIVE_BUILD,
            PAIR_BUILDS_B,
        )

        self.tmp = Path(tempfile.mkdtemp())
        self.ws = ws = Workspace(self.tmp)
        self.builds = self.tmp / "builds"
        flags = {BUILD: None, ONE_PRIMITIVE_BUILD: "x-one-primitive"}
        flags.update({h: "x-drift" for h in DRIFT_BUILDS.values()})
        flags.update({PAIR_BUILDS_B[a]: ARMS[("H4", a)].experimental_stamp for a in PAIR_BUILDS_B})
        for build, flag in flags.items():
            root = self.builds / build.removeprefix("sha256:")
            (root / "sdk").mkdir(parents=True)
            (root / "sdk" / "fs.py").write_text("x = 1\n")
            (root / "manifest.kvx").write_text(
                '[build]\nname = "refworld"\ncompiler = "cwr-compiler 0.1.0+test"\n'
                f"experimental = {'true' if flag else 'false'}\n"
                f"experimental_flag = {json.dumps(flag)}\n"
            )
        for task in ["t001-short", "t002-medium", "t003-long", "u1-task", "u6-task"]:
            (ws.tasks / task / "task.md").write_text(f"Do {task}.\n")
        self.cassettes = self.tmp / "cassettes"
        for task in ["t001-short", "u1-task"]:
            (self.cassettes / task).mkdir(parents=True)
            (self.cassettes / task / "seed-1.jsonl").write_text('{"format":"cwr.cassette"}\n')
        digest = run.cassette_set_digest(self.cassettes)
        text = ws.prereg.read_text().replace("sha256:" + "c" * 64, digest)
        text = text.replace("sha256:" + "f" * 64, run.sha256_file(ws.upgrades))
        ws.prereg.write_text(text)
        h4 = read_kvx(ws.campaigns / "h4.kvx")
        for name, body in h4.items():
            if name.startswith("pair."):
                body.update(
                    upgrade_after_turn=1,
                    task_dir=str(ws.tasks / body["task"]),
                    changed_primitives=["fs.delete"],
                    misuse_code="LOCKED",
                    bin="medium",
                )
        from benchmark.harness.ledgerio import write_kvx

        write_kvx(ws.campaigns / "h4.kvx", h4)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def check(self, hypothesis="H2", arm="script", task="t001-short", seed=1, **kw):
        options = {
            "model_identity": "test-model",
            "build_roots": [self.builds],
            "cassettes": self.cassettes,
            "prereg_path": self.ws.prereg,
            "arms_path": self.ws.arms,
            "suite_path": self.ws.suite,
            "tasks_dir": self.ws.tasks,
            "upgrades_path": self.ws.upgrades,
            "campaigns_dir": self.ws.campaigns,
        }
        options.update(kw)
        return run.preflight(hypothesis, arm, task, seed, **options)

    def refused(self, code=2, **kw):
        with self.assertRaises(run.PreflightRefused) as caught:
            self.check(**kw)
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        return str(caught.exception)

    def test_control_arm_resolves_the_registered_build_and_stamps_digests(self):
        checked = self.check()
        self.assertEqual(checked.build, BUILD)
        self.assertTrue(checked.registered)
        self.assertEqual(checked.revisions["compiler"], "cwr-compiler 0.1.0+test")
        self.assertTrue(checked.revisions["sdk"].startswith("sha256:"))
        episode = checked.episode(results_root=self.tmp / "results", turn_limit=3)
        self.assertEqual(episode.turn_limit, 3)
        self.assertEqual(
            sorted(episode.registration_digests), ["arms_kvx", "metrics_py", "preregistration"]
        )
        self.assertEqual(episode.instruction, "Do t001-short.\n")

    def test_build_override_must_equal_the_registered_build(self):
        from benchmark.harness.tests.workspace import DRIFT_BUILDS, ONE_PRIMITIVE_BUILD

        other = DRIFT_BUILDS[("t001-short", "drift-1")]
        self.assertIn("is not the registered Build", self.refused(build="sha256:" + "9" * 64))
        self.assertIn("needs None", self.refused(build=other))
        text = self.refused(arm="one-primitive", build=BUILD)
        self.assertIn("needs '--x-one-primitive'", text)
        self.assertEqual(self.check(arm="one-primitive").build, ONE_PRIMITIVE_BUILD)

    def test_drift_arm_build_and_spec_come_from_the_campaign_file(self):
        from benchmark.harness.tests.workspace import DRIFT_BUILDS

        checked = self.check(hypothesis="H1", arm="drift-1")
        self.assertEqual(checked.build, DRIFT_BUILDS[("t001-short", "drift-1")])
        other = DRIFT_BUILDS[("t002-medium", "drift-1")]
        self.refused(hypothesis="H1", arm="drift-1", build=other)
        (self.ws.campaigns / "h1.kvx").unlink()
        self.assertIn("is not registered", self.refused(hypothesis="H1", arm="drift-1"))

    def test_pair_arm_upgrade_plan_from_h4(self):
        from benchmark.harness.tests.workspace import PAIR_BUILDS_B

        checked = self.check(hypothesis="H4", arm="silent", task="u1-task")
        self.assertEqual(checked.build, BUILD)
        self.assertEqual(checked.upgrade.build_b, PAIR_BUILDS_B["silent"])
        self.assertEqual((checked.upgrade.after_turn, checked.upgrade.pair), (1, "U1"))
        self.assertEqual(checked.upgrade_pair["misuse_code"], "LOCKED")
        self.refused(hypothesis="H4", arm="silent", task="t001-short")
        self.ws.upgrades.write_text(self.ws.upgrades.read_text() + "\n")
        self.assertIn(
            "upgrades.kvx digest", self.refused(hypothesis="H4", arm="silent", task="u1-task")
        )

    def test_registration_gates(self):
        self.ws.arms.write_text(self.ws.arms.read_text() + "\n")
        self.assertIn("arms_kvx digest", self.refused(code=3))

    def test_seed_model_cassette_and_task_gates(self):
        self.assertIn("not a registered seed", self.refused(seed=7))
        self.assertFalse(self.check(seed=7, allow_unregistered=True, cassettes=None).registered)
        self.assertIn("model identity", self.refused(model_identity="other"))
        self.assertIn("not a valid cell identity", self.refused(task="../t001-short"))
        self.assertIn("is not a task", self.refused(task="t999-missing"))
        (self.cassettes / "t001-short" / "seed-2.jsonl").write_text("{}\n")
        self.assertIn("cassette set digest", self.refused())


class LedgerReplay(unittest.TestCase):
    """analyse.Replayer re-submits the recorded messages and re-applies upgrades from the ledger."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        plan = run.UpgradePlan(after_turn=1, build_b=BUILD_B, pair="U1")
        episode = spec(self.root, hypothesis="H4", arm="changelog", task="u1-task", upgrade=plan)
        run.run_episode(episode, FakeKernel(), FakeModel([STEP1, STEP2]), progress_predicate(2))
        self.dir = self.root / "h4" / "changelog" / "u1-task" / "1"

    def tearDown(self):
        shutil.rmtree(self.root)

    def cell(self, ledger=None):
        return analyse.Cell(
            "H4",
            ARMS[("H4", "changelog")],
            "u1-task",
            1,
            self.dir,
            read_kvx(self.dir / "attempt.kvx")["attempt"],
            ledger or read_ledger(self.dir / "ledger.jsonl"),
            read_kvx(self.dir / "final_state.kvx"),
            "U1",
        )

    def replayer(self):
        return analyse.Replayer(replay_kernel, [], None, self.root)

    def test_upgrade_is_reapplied_and_states_follow_each_turn(self):
        states, method = self.replayer().states(self.cell(), progress_predicate(2))
        self.assertEqual(method, "ledger_replay")
        self.assertEqual(states, {1: {"task": {"progress": 1}}, 2: {"task": {"progress": 2}}})

    def test_altered_upgrade_record_diverges(self):
        ledger = read_ledger(self.dir / "ledger.jsonl")
        upgrade = next(r for r in ledger if r["kind"] == "upgrade")
        upgrade["new_build"] = "sha256:" + "9" * 64
        with self.assertRaisesRegex(
            analyse.ReplayDivergenceError, "diverges from the ledger at seq"
        ):
            self.replayer().states(self.cell(ledger), progress_predicate(2))


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
                cell = ws.results / "h1" / "frozen" / "t001-short" / str(seed)
                direct[seed] = metrics.instance_metrics(
                    read_ledger(cell / "ledger.jsonl"), read_kvx(cell / "final_state.kvx"), pred
                )
            (ws.tasks / "t001-short" / "success.py").write_text(
                'def success(state, ledger):\n    return state.get("task", {}).get("progress", 0) >= 2\n'
            )
            # The predicate is part of the registered suite: register the new suite digest.
            digest = suite_digest(ws.tasks)
            for path in [*ws.root.glob("*.kvx"), *ws.root.rglob("campaigns/*.kvx")]:
                path.write_text(path.read_text().replace(ws.suite_digest, digest))
            out = tmp / "r.json"
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
                code = analyse.main(ws.argv("--out", str(out)), make_kernel=replay_kernel)
            self.assertEqual(code, 0, err.getvalue())
            cell = json.loads(out.read_text())["hypotheses"]["H1"]["cells"]["frozen"]["short"]
            self.assertEqual(cell["instances"], 2)
            self.assertEqual(cell["turns_to_success_method"], ["ledger_replay"])
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

            # A recorded message that would change the state changes the ledger: refused, exit 4.
            seed2 = ws.results / "h1" / "frozen" / "t001-short" / "2" / "messages"
            (seed2 / "turn_0001.md").write_text((seed2 / "turn_0004.md").read_text())
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
                code = analyse.main(ws.argv("--check", str(out)), make_kernel=replay_kernel)
            self.assertEqual(code, 4, err.getvalue())
            self.assertIn("replay diverges from the ledger", err.getvalue())

            # Without recorded messages the registered stop rule (C7) applies and is labelled.
            shutil.rmtree(seed2)
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
                code = analyse.main(ws.argv("--out", str(out)), make_kernel=replay_kernel)
            self.assertEqual(code, 0, err.getvalue())
            cell = json.loads(out.read_text())["hypotheses"]["H1"]["cells"]["frozen"]["short"]
            self.assertEqual(
                cell["turns_to_success_method"],
                ["ledger_replay", "stop_rule: no recorded turn messages"],
            )
            self.assertEqual(cell["metrics"]["turns_to_success"]["mean"], 3.0)
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
        back = run.predicate_state(kernel, [{"seq": 0, "world_time": 4, "turn": 1}], [])
        self.assertEqual(back["meta"]["format"], run.STATE_VIEW_FORMAT)
        self.assertEqual(back["fs"], view["fs"])
        self.assertEqual(back["kv"], view["kv"])
        self.assertEqual(back["jobs"]["jobs"]["j4"]["status"], "done")
        self.assertEqual(back["jobs"]["jobs"]["j4"]["result"], "w:j4:5")
        self.assertEqual(back["world"], {"time": 4, "turn": 1})

    def test_idempotency_store_in_the_view(self):
        # `cwr instance keys` lists done slots; keys are the canonical JSON of the key parameter.
        events = [
            {
                "event": "done",
                "adapter": "publish.post",
                "key": '"k"',
                "payload_hash": "p",
                "request_hash": "h",
                "outcome": {"post_id": "x"},
                "failure": None,
            },
        ]
        ledger = [
            {"seq": 0, "kind": "header", "world_time": 0, "turn": 0},
            {
                "seq": 1,
                "kind": "adapter",
                "primitive": "publish.post",
                "gateway": {"request_hash": "h"},
                "world_time": 1,
                "turn": 1,
            },
        ]
        view = run.predicate_state({}, ledger, events)
        self.assertEqual(
            view["gateway"]["idempotency"]["publish.post"]["k"],
            {"receipt": {"post_id": "x"}, "failure": None, "payload_hash": "p", "seq": 1},
        )
        self.assertEqual(view["world"], {"time": 1, "turn": 1})

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

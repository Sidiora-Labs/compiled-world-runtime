# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""analyse.py: deterministic bootstrap, recomputation from ledgers, and every refusal gate."""

import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

# Make `benchmark` importable when this file runs directly under python3 -I.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from benchmark import analyse
from benchmark.harness.ledgerio import read_kvx, read_ledger, sha256_file, write_kvx, write_ledger
from benchmark.harness.tests.synth import LedgerBuilder
from benchmark.harness.tests.workspace import Workspace


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = analyse.main(argv)
    return code, out.getvalue(), err.getvalue()


class Statistics(unittest.TestCase):
    def test_quantile_type7(self):
        self.assertEqual(analyse.quantile([1.0, 2.0, 3.0, 4.0], 0.5), 2.5)
        self.assertAlmostEqual(analyse.quantile([0.0, 10.0], 0.025), 0.25)

    def test_bootstrap_is_deterministic_and_seed_sensitive(self):
        values = [0.13, 1.7, 2.2, 0.4, 3.9, 1.1, 5.6, 0.05, 2.8, 4.4]
        a = analyse.bootstrap([values], lambda m: m[0], resamples=2000, confidence=0.95, seed=7)
        b = analyse.bootstrap([values], lambda m: m[0], resamples=2000, confidence=0.95, seed=7)
        c = analyse.bootstrap([values], lambda m: m[0], resamples=2000, confidence=0.95, seed=8)
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertLessEqual(a[0], analyse.mean(values))
        self.assertGreaterEqual(a[1], analyse.mean(values))

    def test_bootstrap_constant_and_empty(self):
        self.assertEqual(
            analyse.bootstrap([[2.0, 2.0]], lambda m: m[0], resamples=10, confidence=0.95, seed=1),
            [2.0, 2.0],
        )
        self.assertIsNone(
            analyse.bootstrap([[]], lambda m: m[0], resamples=10, confidence=0.95, seed=1)
        )

    def test_derived_seed_is_stable(self):
        self.assertEqual(
            analyse.derive_seed(20261106, "H1", "frozen", "short", "task_success"),
            analyse.derive_seed(20261106, "H1", "frozen", "short", "task_success"),
        )

    def test_verdict_labels(self):
        self.assertEqual(analyse.verdict("a>b", [0.1, 0.5]), "consistent")
        self.assertEqual(analyse.verdict("a>b", [-0.5, -0.1]), "inconsistent")
        self.assertEqual(analyse.verdict("a<b", [-0.2, 0.3]), "inconclusive")
        self.assertEqual(analyse.verdict("a=b", [-0.2, 0.3]), "consistent")
        self.assertEqual(analyse.verdict("undetermined", [0.1, 0.2]), "reported_only")
        self.assertEqual(analyse.verdict("a>b", None), "insufficient_data")

    def test_verdict_non_strict_expectations_follow_the_registered_rule(self):
        # direction_labels: consistent only when the interval lies wholly on the expected side.
        self.assertEqual(analyse.verdict("a>=b", [-0.9, 0.01]), "inconclusive")
        self.assertEqual(analyse.verdict("a>=b", [0.0, 0.4]), "consistent")
        self.assertEqual(analyse.verdict("a>=b", [0.1, 0.4]), "consistent")
        self.assertEqual(analyse.verdict("a>=b", [-0.4, -0.1]), "inconsistent")
        self.assertEqual(analyse.verdict("a<=b", [-0.01, 0.9]), "inconclusive")
        self.assertEqual(analyse.verdict("a<=b", [-0.4, 0.0]), "consistent")
        self.assertEqual(analyse.verdict("a<=b", [-0.4, -0.1]), "consistent")
        self.assertEqual(analyse.verdict("a<=b", [0.1, 0.4]), "inconsistent")


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.ws = Workspace(self.tmp)
        self.ws.populate_h1()
        self.ws.populate_h4()
        self.out = self.tmp / "report.json"

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def analyse_ok(self, *extra):
        code, _out, err = run(self.ws.argv("--out", str(self.out), *extra))
        self.assertEqual(code, 0, err)
        return json.loads(self.out.read_text())

    def test_means_recomputed_from_ledgers(self):
        report = self.analyse_ok()
        h1 = report["hypotheses"]["H1"]["cells"]
        for bin_name in ("short", "medium", "long"):
            self.assertEqual(h1["frozen"][bin_name]["metrics"]["task_success"]["mean"], 1.0)
            self.assertEqual(h1["drift-1"][bin_name]["metrics"]["task_success"]["mean"], 0.5)
            self.assertEqual(h1["drift-3"][bin_name]["metrics"]["task_success"]["mean"], 0.0)
            self.assertEqual(h1["frozen"][bin_name]["metrics"]["task_success"]["ci"], [1.0, 1.0])
        self.assertEqual(h1["frozen"]["long"]["metrics"]["rule_reuse"]["mean"], 1.0)
        self.assertEqual(h1["drift-3"]["long"]["metrics"]["rule_reuse"]["mean"], 0.0)
        self.assertEqual(h1["drift-3"]["short"]["metrics"]["turns_to_success"]["n"], 0)
        self.assertEqual(h1["drift-3"]["short"]["metrics"]["turns_to_success"]["undefined"], 2)
        # short drift-3 ledgers: 3 calls, 2 failing (fs.delete, LOCKED) -> repeated 1/2
        self.assertEqual(h1["drift-3"]["short"]["metrics"]["repeated_failure_rate"]["mean"], 0.5)
        self.assertEqual(h1["drift-1"]["short"]["metrics"]["drift_records"]["mean"], 0.0)
        self.assertEqual(h1["drift-1"]["long"]["metrics"]["drift_records"]["mean"], 1.0)
        directions = {(d["id"], d["group"]): d for d in report["hypotheses"]["H1"]["directions"]}
        self.assertEqual(directions[("H1.success.frozen-vs-drift-1", "short")]["estimate"], 0.5)
        self.assertEqual(directions[("H1.success.drift-1-vs-drift-3", "long")]["estimate"], 0.5)
        self.assertIn("t003-long-spec", report["hypotheses"]["H1"]["per_drift_spec"]["drift-1"])
        self.assertIn(
            "harness-recorded", report["hypotheses"]["H1"]["harness_recorded_tokens"]["label"]
        )

    def test_reused_rule_identifier_string(self):
        text = self.ws.suite.read_text().replace(
            'reused_rule = {"code": "CONFLICT", "primitive": "jobs.result"}',
            'reused_rule = "jobs.result[2]:CONFLICT"',
        )
        self.assertIn("jobs.result[2]:CONFLICT", text)
        self.ws.suite.write_text(text)
        report = self.analyse_ok()
        h1 = report["hypotheses"]["H1"]["cells"]
        self.assertEqual(h1["frozen"]["long"]["metrics"]["rule_reuse"]["mean"], 1.0)
        self.assertEqual(h1["drift-3"]["long"]["metrics"]["rule_reuse"]["mean"], 0.0)
        self.assertEqual(h1["frozen"]["short"]["metrics"]["rule_reuse"]["n"], 0)

    def test_h4_pairs_and_pooling(self):
        report = self.analyse_ok()
        h4 = report["hypotheses"]["H4"]["cells"]
        self.assertEqual(h4["changelog"]["U1"]["metrics"]["post_upgrade_misuse_rate"]["mean"], 0.0)
        self.assertEqual(
            h4["version-only"]["U1"]["metrics"]["post_upgrade_misuse_rate"]["mean"], 0.5
        )
        self.assertEqual(h4["silent"]["U1"]["metrics"]["post_upgrade_misuse_rate"]["mean"], 1.0)
        self.assertEqual(h4["silent"]["U6"]["metrics"]["post_upgrade_misuse_rate"]["mean"], 0.0)
        self.assertEqual(h4["silent"]["pooled"]["instances"], 2)  # U6 excluded from the pool
        self.assertEqual(
            h4["changelog"]["U1"]["metrics"]["turns_to_first_correct_use"]["mean"], 1.0
        )
        self.assertEqual(h4["silent"]["U1"]["metrics"]["turns_to_first_correct_use"]["n"], 0)
        u6 = [d for d in report["hypotheses"]["H4"]["directions"] if d["group"] == "U6"]
        self.assertTrue(all(d["label"] == "consistent" for d in u6))

    def test_matrix_completeness(self):
        report = self.analyse_ok()
        self.assertTrue(report["hypotheses"]["H1"]["matrix"]["complete"])
        self.assertTrue(report["hypotheses"]["H4"]["matrix"]["complete"])
        self.assertFalse(report["hypotheses"]["H2"]["matrix"]["complete"])
        self.assertEqual(report["hypotheses"]["H2"]["matrix"]["missing_count"], 2 * 3 * 2)
        code, _, _ = run(self.ws.argv("--out", str(self.out), "--require-complete"))
        self.assertEqual(code, 5)

    def test_deterministic_and_check_mode(self):
        self.analyse_ok()
        first = self.out.read_bytes()
        self.analyse_ok()
        self.assertEqual(first, self.out.read_bytes())
        code, _out, _ = run(self.ws.argv("--check", str(self.out)))
        self.assertEqual(code, 0)
        report = json.loads(first)
        report["hypotheses"]["H1"]["cells"]["frozen"]["short"]["metrics"]["task_success"][
            "mean"
        ] = 0.9
        self.out.write_text(json.dumps(report))
        code, _, err = run(self.ws.argv("--check", str(self.out)))
        self.assertEqual(code, 1)
        self.assertIn("task_success", err)

    def test_metrics_digest_mismatch_refused(self):
        changed = self.tmp / "metrics.py"
        changed.write_text(self.ws.metrics.read_text() + "\n# edited after execution\n")
        argv = self.ws.argv("--out", str(self.out))
        argv[argv.index("--metrics") + 1] = str(changed)
        code, _, err = run(argv)
        self.assertEqual(code, 3)
        self.assertIn("metrics_py digest", err)
        self.assertFalse(self.out.exists())

    def test_arms_digest_mismatch_refused(self):
        self.ws.arms.write_text(self.ws.arms.read_text() + "\n")
        code, _, _ = run(self.ws.argv("--out", str(self.out)))
        self.assertEqual(code, 3)

    def test_draft_registration_refused_unless_draft_ok(self):
        ws = Workspace(self.tmp / "draft", status="draft")
        ws.populate_h1()
        code, _, err = run(ws.argv("--out", str(self.out)))
        self.assertEqual(code, 2)
        self.assertIn("draft", err)
        code, _, err = run(ws.argv("--out", str(self.out), "--draft-ok"))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(self.out.read_text())["registration"], "draft")

    def tamper(self, cell_path, mutate):
        ledger = read_ledger(cell_path / "ledger.jsonl")
        mutate(ledger)
        write_ledger(cell_path / "ledger.jsonl", ledger)

    def test_header_seed_mismatch_refused(self):
        cell = self.ws.results / "h1" / "frozen" / "t001-short" / "1"
        self.tamper(cell, lambda ledger: ledger[0].update(seed=2))
        code, _, err = run(self.ws.argv("--out", str(self.out)))
        self.assertEqual(code, 4)
        self.assertIn("header seed", err)

    def test_header_arm_and_model_mismatch_refused(self):
        cell = self.ws.results / "h1" / "frozen" / "t001-short" / "1"
        self.tamper(cell, lambda ledger: ledger[0].update(arm="H1/drift-1", model="other"))
        code, _, err = run(self.ws.argv("--out", str(self.out)))
        self.assertEqual(code, 4)
        self.assertIn("header arm", err)
        self.assertIn("header model", err)

    def test_missing_experimental_stamp_refused(self):
        cell = self.ws.results / "h1" / "drift-1" / "t001-short" / "1"
        self.tamper(cell, lambda ledger: [r.update(experimental=None) for r in ledger])
        code, _, err = run(self.ws.argv("--out", str(self.out)))
        self.assertEqual(code, 4)
        self.assertIn("experimental stamp", err)

    def test_control_build_mismatch_refused(self):
        cell = self.ws.results / "h1" / "frozen" / "t001-short" / "1"
        self.tamper(cell, lambda ledger: ledger[0].update(build="sha256:" + "9" * 64))
        code, _, err = run(self.ws.argv("--out", str(self.out)))
        self.assertEqual(code, 4)
        self.assertIn("registered Build", err)

    def test_cassette_digest_mismatch_refused(self):
        cell = self.ws.results / "h1" / "frozen" / "t001-short" / "1"
        self.tamper(cell, lambda ledger: ledger[0].update(cassette_digest="sha256:" + "0" * 64))
        code, _, _err = run(self.ws.argv("--out", str(self.out)))
        self.assertEqual(code, 4)

    def test_mixed_cassettes_never_pooled_even_in_draft(self):
        ws = Workspace(self.tmp / "mix", status="draft")
        text = ws.prereg.read_text().replace(
            'cassette_digest = "sha256:' + "c" * 64 + '"', "cassette_digest = null"
        )
        ws.prereg.write_text(text)
        ws.populate_h1()
        cell = ws.results / "h1" / "frozen" / "t001-short" / "1"
        other = "sha256:" + "0" * 64
        self.tamper(cell, lambda ledger: ledger[0].update(cassette_digest=other))
        attempt = cell / "attempt.kvx"
        attempt.write_text(attempt.read_text().replace("c" * 64, "0" * 64))
        code, _, err = run(ws.argv("--out", str(self.out), "--draft-ok"))
        self.assertEqual(code, 4)
        self.assertIn("different cassette digests", err)

    def test_header_and_attempt_cassette_digests_must_agree(self):
        cell = self.ws.results / "h1" / "frozen" / "t001-short" / "1"
        attempt = cell / "attempt.kvx"
        attempt.write_text(attempt.read_text().replace("c" * 64, "0" * 64))
        code, _, err = run(self.ws.argv("--out", str(self.out)))
        self.assertEqual(code, 4)
        self.assertIn("is not the attempt's", err)

    def test_unregistered_seed_refused(self):
        self.ws.write_cell(
            "H1",
            "frozen",
            "t001-short",
            3,
            LedgerBuilder(arm="H1/frozen", seed=3).call("fs.exists").turn().build_ledger(),
            {"task": {"done": 1}},
        )
        code, _, err = run(self.ws.argv("--out", str(self.out)))
        self.assertEqual(code, 4)
        self.assertIn("not registered", err)

    def live_ledger(self, seed: int = 1) -> list:
        """A live ledger: the kernel writes no cassette digest into its header."""
        builder = LedgerBuilder(arm="H1/frozen", seed=seed, cassette=None)
        return builder.call("fs.exists").turn().build_ledger()

    def test_live_runs_are_excluded_and_analysed_separately(self):
        cell = self.ws.results / "h1" / "frozen" / "t001-short" / "1"
        shutil.rmtree(cell)
        self.ws.write_cell(
            "H1", "frozen", "t001-short", 1, self.live_ledger(), {"task": {"done": 1}}
        )
        attempt = (cell / "attempt.kvx").read_text().replace('mode = "replay"', 'mode = "live"')
        (cell / "attempt.kvx").write_text(attempt)
        self.ws.write_cell(
            "H1", "frozen", "t001-short", 1, self.live_ledger(), {"task": {"done": 1}}, live=True
        )
        report = self.analyse_ok()
        self.assertIn(
            {"cell": "h1/frozen/t001-short/1", "reason": "mode 'live' is not 'replay'"},
            report["excluded"],
        )
        self.assertEqual(report["hypotheses"]["H1"]["cells"]["frozen"]["short"]["instances"], 1)
        self.assertFalse(report["hypotheses"]["H1"]["matrix"]["complete"])
        live = self.analyse_ok("--live")
        self.assertEqual(live["mode"], "live")
        self.assertEqual(live["hypotheses"]["H1"]["cells"]["frozen"]["short"]["instances"], 1)
        self.assertEqual(live["hypotheses"]["H1"]["cells"]["drift-1"]["short"]["instances"], 0)

    def test_live_ledger_relabelled_as_replay_is_refused(self):
        cell = self.ws.write_cell(
            "H1", "frozen", "t001-short", 1, self.live_ledger(), {"task": {"done": 1}}
        )
        code, _, err = run(self.ws.argv("--out", str(self.out)))
        self.assertEqual(code, 4, err)
        self.assertIn("attempt mode 'replay' but the ledger header names no cassette digest", err)
        self.assertFalse(self.out.exists())
        self.assertTrue((cell / "ledger.jsonl").exists())

    def test_replay_ledger_relabelled_as_live_is_refused(self):
        cell = self.ws.results / "h1" / "frozen" / "t001-short" / "1"
        attempt = (cell / "attempt.kvx").read_text().replace('mode = "replay"', 'mode = "live"')
        (cell / "attempt.kvx").write_text(attempt)
        code, _, err = run(self.ws.argv("--out", str(self.out)))
        self.assertEqual(code, 4, err)
        self.assertIn("attempt mode 'live' but the ledger header names a cassette digest", err)

    def test_invariant_violation_and_model_error_excluded(self):
        bad = (
            LedgerBuilder(arm="H1/frozen", seed=2)
            .call("fs.write", "INVARIANT_VIOLATION", "mutating")
            .turn("error")
        )
        self.ws.write_cell(
            "H1", "frozen", "t001-short", 2, bad.build_ledger(), {"task": {"done": 0}}
        )
        cell = self.ws.results / "h1" / "frozen" / "t002-medium" / "2"
        attempt = (
            (cell / "attempt.kvx")
            .read_text()
            .replace('exit_status = "success"', 'exit_status = "model_error"')
        )
        (cell / "attempt.kvx").write_text(attempt)
        report = self.analyse_ok()
        reasons = {e["cell"]: e["reason"] for e in report["excluded"]}
        self.assertIn("INVARIANT_VIOLATION", reasons["h1/frozen/t001-short/2"])
        self.assertIn("model_error", reasons["h1/frozen/t002-medium/2"])
        self.assertEqual(report["hypotheses"]["H1"]["matrix"]["missing_count"], 2)

    def test_edited_task_file_refused_by_recomputed_suite_digest(self):
        self.analyse_ok()
        # The suite's own declared digest stays stale; only the recomputation can catch the edit.
        (self.ws.tasks / "t001-short" / "success.py").write_text(
            "def success(state, ledger):\n    return True\n"
        )
        code, _, err = run(self.ws.argv("--out", str(self.out)))
        self.assertEqual(code, 4)
        self.assertIn("suite digest", err)
        self.assertIn("recomputed", err)

    def test_registered_suite_digest_without_suite_file_refused(self):
        self.ws.suite.unlink()
        code, _, err = run(self.ws.argv("--out", str(self.out)))
        self.assertEqual(code, 4)
        self.assertIn("is missing", err)

    def test_drift_ledger_from_another_tasks_drift_build_refused(self):
        from benchmark.harness.tests.workspace import DRIFT_BUILDS

        cell = self.ws.results / "h1" / "drift-1" / "t001-short" / "1"
        other = DRIFT_BUILDS[("t002-medium", "drift-1")]
        self.tamper(cell, lambda ledger: [r.update(build=other) for r in ledger])
        code, _, err = run(self.ws.argv("--out", str(self.out)))
        self.assertEqual(code, 4)
        self.assertIn("h1/drift-1/t001-short/1: header build", err)

    def test_drift_arm_without_campaign_registration_refused(self):
        (self.ws.campaigns / "h1.kvx").unlink()
        code, _, err = run(self.ws.argv("--out", str(self.out)))
        self.assertEqual(code, 4)
        self.assertIn("registers no [task.t001-short].drift_1_build", err)

    def test_h4_build_a_and_build_b_checked_against_campaign(self):
        cell = self.ws.results / "h4" / "silent" / "u1-task" / "1"
        self.tamper(cell, lambda ledger: ledger[0].update(build="sha256:" + "9" * 64))
        code, _, err = run(self.ws.argv("--out", str(self.out)))
        self.assertEqual(code, 4)
        self.assertIn("is not the registered Build A", err)

        from benchmark.harness.tests.workspace import PAIR_BUILDS_B, Workspace

        ws = Workspace(self.tmp / "b-side")
        ws.populate_h4()
        cell = ws.results / "h4" / "silent" / "u1-task" / "1"

        def wrong_b(ledger):
            for record in ledger:
                if record["kind"] == "upgrade":
                    record["new_build"] = PAIR_BUILDS_B["changelog"]

        self.tamper(cell, wrong_b)
        code, _, err = run(ws.argv("--out", str(self.out)))
        self.assertEqual(code, 4)
        self.assertIn("is not the registered Build B", err)

    def test_attempt_stamped_with_other_digests_refused(self):
        cell = self.ws.results / "h1" / "frozen" / "t001-short" / "1"
        attempt = read_kvx(cell / "attempt.kvx")
        attempt["attempt"]["registration_digests"] = {
            "preregistration": sha256_file(self.ws.prereg),
            "metrics_py": sha256_file(self.ws.metrics),
            "arms_kvx": "sha256:" + "0" * 64,
        }
        write_kvx(cell / "attempt.kvx", attempt)
        code, _, err = run(self.ws.argv("--out", str(self.out)))
        self.assertEqual(code, 4)
        self.assertIn("attempt ran under arms_kvx", err)
        attempt["attempt"]["registration_digests"]["arms_kvx"] = sha256_file(self.ws.arms)
        write_kvx(cell / "attempt.kvx", attempt)
        report = self.analyse_ok()
        self.assertEqual(report["attempts_without_registration_digests"], 0)

    def test_unstamped_attempt_refused_unless_draft(self):
        cell = self.ws.results / "h1" / "frozen" / "t001-short" / "1"
        attempt = read_kvx(cell / "attempt.kvx")
        del attempt["attempt"]["registration_digests"]
        write_kvx(cell / "attempt.kvx", attempt)
        code, _, err = run(self.ws.argv("--out", str(self.out)))
        self.assertEqual(code, 4)
        self.assertIn("attempt carries no registration_digests", err)

        ws = Workspace(self.tmp / "unstamped-draft", status="draft")
        ws.populate_h1()
        cell = ws.results / "h1" / "frozen" / "t001-short" / "1"
        attempt = read_kvx(cell / "attempt.kvx")
        del attempt["attempt"]["registration_digests"]
        write_kvx(cell / "attempt.kvx", attempt)
        code, _, err = run(ws.argv("--out", str(self.out), "--draft-ok"))
        self.assertEqual(code, 0, err)
        self.assertEqual(
            json.loads(self.out.read_text())["attempts_without_registration_digests"], 1
        )

    def test_campaign_registered_under_other_arms_refused(self):
        self.ws.write_campaigns(arms_kvx="sha256:" + "0" * 64)
        code, _, err = run(self.ws.argv("--out", str(self.out)))
        self.assertEqual(code, 4)
        self.assertIn("campaigns/h1.kvx registered arms_kvx", err)
        self.assertIn("campaigns/h4.kvx registered arms_kvx", err)

    def test_truncated_ledger_refused(self):
        path = self.ws.results / "h1" / "frozen" / "t001-short" / "1" / "ledger.jsonl"
        path.write_bytes(path.read_bytes()[:-5])
        code, _, err = run(self.ws.argv("--out", str(self.out)))
        self.assertEqual(code, 4)
        self.assertIn("unreadable cell", err)


class RepositoryRegistration(unittest.TestCase):
    """The committed registration and arms must match the committed metrics.py and arms.kvx."""

    def test_committed_digests_match(self):
        from benchmark.harness.prereg import check_digests, load_prereg

        check_digests(load_prereg())

    def test_analyse_refuses_the_committed_registration_only_while_it_is_a_draft(self):
        from benchmark.harness.arms import load_arms
        from benchmark.harness.prereg import load_prereg, missing_fields

        meta, arms = load_arms()
        draft = bool(missing_fields(load_prereg(), arms, meta["control"]))
        empty = tempfile.mkdtemp()
        try:
            code, _, err = run(["--results", empty, "--out", str(Path(empty) / "a.json")])
        finally:
            shutil.rmtree(empty)
        if draft:
            self.assertEqual(code, 2)
            self.assertIn("draft", err)
        else:
            self.assertEqual(code, 0, err)


if __name__ == "__main__":
    unittest.main()

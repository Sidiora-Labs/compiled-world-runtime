# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Hand-computed checks of every metric definition in experiments.md section 4 on synthetic ledgers."""

import sys
import unittest
from pathlib import Path

# Make `benchmark` importable when this file runs directly under python3 -I.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from benchmark.harness import metrics as m
from benchmark.harness.tests.synth import LedgerBuilder


def always(value):
    return lambda state, ledger: value


class LedgerStructure(unittest.TestCase):
    def test_valid_ledger_passes(self):
        m.validate_ledger(LedgerBuilder().call("fs.exists").turn().build_ledger())

    def test_rejects_seq_gap(self):
        ledger = LedgerBuilder().call("fs.exists").turn().build_ledger()
        ledger[1]["seq"] = 5
        with self.assertRaises(m.MetricInputError):
            m.validate_ledger(ledger)

    def test_rejects_code_outside_envelope(self):
        ledger = LedgerBuilder().call("fs.read", "SOMETHING_ELSE").turn().build_ledger()
        with self.assertRaises(m.MetricInputError):
            m.validate_ledger(ledger)

    def test_rejects_ok_with_code(self):
        ledger = LedgerBuilder().call("fs.read").turn().build_ledger()
        ledger[1]["outcome"]["code"] = "LOCKED"
        with self.assertRaises(m.MetricInputError):
            m.validate_ledger(ledger)

    def test_header_must_be_first_and_only(self):
        ledger = LedgerBuilder().call("fs.read").turn().build_ledger()
        ledger[1]["kind"] = "header"
        with self.assertRaises(m.MetricInputError):
            m.validate_ledger(ledger)

    def test_envelope_has_eleven_codes(self):
        self.assertEqual(len(m.ENVELOPE), 11)
        self.assertEqual(len(set(m.ENVELOPE)), 11)


class Counts(unittest.TestCase):
    def setUp(self):
        self.ledger = (
            LedgerBuilder()
            .call("fs.exists")
            .call("fs.delete", "LOCKED", "mutating")
            .turn("error")
            .drift()
            .call("search.query", side_effect="external")
            .call("fs.write", side_effect="mutating")
            .call("publish.post", "RATE_LIMITED", "external")
            .turn("partial")
            .proposal()
            .turn("ok")
            .build_ledger()
        )

    def test_primitive_calls_counts_primitive_and_adapter_records_only(self):
        self.assertEqual(m.primitive_calls(self.ledger), 5)

    def test_failure_rate_by_code(self):
        rates = m.failure_rate_by_code(self.ledger)
        self.assertEqual(set(rates), set(m.ENVELOPE))
        self.assertAlmostEqual(rates["LOCKED"], 1 / 5)
        self.assertAlmostEqual(rates["RATE_LIMITED"], 1 / 5)
        self.assertEqual(rates["NOT_FOUND"], 0.0)

    def test_failure_rate_undefined_without_calls(self):
        ledger = LedgerBuilder().turn("error").build_ledger()
        self.assertTrue(all(v is None for v in m.failure_rate_by_code(ledger).values()))

    def test_partial_rate(self):
        self.assertAlmostEqual(m.partial_rate(self.ledger), 1 / 3)

    def test_probing_ratio(self):
        # read_only: fs.exists; acting: fs.delete (mutating), search.query, publish.post (external), fs.write
        self.assertAlmostEqual(m.probing_ratio(self.ledger), 1 / 4)

    def test_drift_records(self):
        self.assertEqual(m.drift_records(self.ledger), 1)

    def test_turn_records_are_never_failures(self):
        ledger = LedgerBuilder().turn("error").turn("error").build_ledger()
        self.assertIsNone(m.repeated_failure_rate(ledger))
        self.assertEqual(m.primitive_calls(ledger), 0)


class RepeatedFailures(unittest.TestCase):
    def test_hand_computed(self):
        ledger = (
            LedgerBuilder()
            .call("fs.delete", "LOCKED", "mutating")
            .call("fs.delete", "LOCKED", "mutating")
            .turn("error")
            .call("fs.delete", "NOT_FOUND", "mutating")
            .call("kv.set", "LOCKED", "mutating")
            .turn("error")
            .call("fs.delete", "LOCKED", "mutating")
            .call("fs.exists")
            .turn("error")
            .build_ledger()
        )
        # failing: 5; repeated: 2nd and 5th (fs.delete, LOCKED)
        self.assertAlmostEqual(m.repeated_failure_rate(ledger), 2 / 5)

    def test_undefined_without_failures(self):
        self.assertIsNone(
            m.repeated_failure_rate(LedgerBuilder().call("fs.exists").turn().build_ledger())
        )


class RuleReuse(unittest.TestCase):
    def build(self, second_code=None, late_code=None, filler=10, tail_filler=2):
        b = LedgerBuilder()
        b.call("jobs.result", "CONFLICT").call("jobs.result")  # first ok at call index 1
        b.calls("fs.write", filler, "mutating")  # indices 2..filler+1
        b.call("jobs.result", second_code)  # second encounter
        b.calls("fs.read", tail_filler)
        b.call("jobs.result", late_code)  # in the last quarter
        return b.turn().build_ledger()

    def test_reuse_without_failure(self):
        self.assertEqual(m.rule_reuse(self.build(), "jobs.result", "CONFLICT"), 1)

    def test_second_encounter_fails_with_rule_code(self):
        self.assertEqual(
            m.rule_reuse(self.build(second_code="CONFLICT"), "jobs.result", "CONFLICT"), 0
        )

    def test_late_failure_in_last_quarter(self):
        self.assertEqual(
            m.rule_reuse(self.build(late_code="CONFLICT"), "jobs.result", "CONFLICT"), 0
        )

    def test_failure_with_other_code_is_not_rule_failure(self):
        self.assertEqual(
            m.rule_reuse(self.build(second_code="NOT_FOUND"), "jobs.result", "CONFLICT"), 1
        )

    def test_no_second_encounter_scores_zero(self):
        ledger = (
            LedgerBuilder()
            .call("jobs.result")
            .calls("fs.read", 3)
            .call("jobs.result")
            .turn()
            .build_ledger()
        )
        self.assertEqual(m.rule_reuse(ledger, "jobs.result", "CONFLICT"), 0)

    def test_never_ok_scores_zero(self):
        ledger = LedgerBuilder().call("jobs.result", "CONFLICT").turn().build_ledger()
        self.assertEqual(m.rule_reuse(ledger, "jobs.result", "CONFLICT"), 0)

    def test_failure_outside_last_quarter_after_second_is_ignored(self):
        b = (
            LedgerBuilder()
            .call("jobs.result")
            .calls("fs.write", 10, "mutating")
            .call("jobs.result")
        )
        b.call("jobs.result", "CONFLICT")  # index 12: not in the last quarter of 40 calls (k >= 30)
        b.calls("fs.read", 26).call("jobs.result")  # index 39
        ledger = b.turn().build_ledger()
        self.assertEqual(m.primitive_calls(ledger), 40)
        self.assertEqual(m.rule_reuse(ledger, "jobs.result", "CONFLICT"), 1)

    def test_rejects_code_outside_envelope(self):
        with self.assertRaises(m.MetricInputError):
            m.rule_reuse(self.build(), "jobs.result", "WRONG")


class Upgrade(unittest.TestCase):
    def build(self):
        b = LedgerBuilder(arm="H4/changelog")
        b.call("fs.delete", side_effect="mutating").turn().call("fs.exists").turn()  # turns 1, 2
        b.upgrade(stamp=None)
        b.call("fs.delete", "LOCKED", "mutating").turn("error")  # turn 3 misuse
        b.call("fs.delete", "LOCKED", "mutating").call("fs.delete", "LOCKED", "mutating").turn(
            "error"
        )  # turn 4 misuse
        b.call("fs.unlock", side_effect="mutating").call(
            "fs.delete", side_effect="mutating"
        ).proposal().turn()  # turn 5
        b.call("kv.get", "NOT_FOUND").turn("error")  # turn 6
        return b.build_ledger()

    def test_misuse_rate(self):
        self.assertAlmostEqual(
            m.post_upgrade_misuse_rate(self.build(), ["fs.delete"], "LOCKED"), 2 / 4
        )

    def test_module_entry_matches_every_primitive_of_the_module(self):
        self.assertAlmostEqual(m.post_upgrade_misuse_rate(self.build(), ["kv"], "NOT_FOUND"), 1 / 4)

    def test_doc_only_pair_has_no_misuse(self):
        self.assertEqual(m.post_upgrade_misuse_rate(self.build(), ["fs.write"], None), 0.0)

    def test_undefined_without_upgrade(self):
        self.assertIsNone(
            m.post_upgrade_misuse_rate(
                LedgerBuilder().turn().build_ledger(), ["fs.delete"], "LOCKED"
            )
        )

    def test_turns_to_first_correct_use(self):
        self.assertEqual(m.turns_to_first_correct_use(self.build(), ["fs.delete"]), 3)

    def test_pre_upgrade_use_does_not_count(self):
        self.assertIsNone(m.turns_to_first_correct_use(self.build(), ["fs.move"]))

    def test_proposals_after_upgrade(self):
        self.assertEqual(m.proposals_after_upgrade(self.build()), 1)


class UpgradeMisuseScope(unittest.TestCase):
    """U3 counts only an escaped CONFLICT; U2 and U5 misuse is not computable from a ledger."""

    def build(self):
        b = LedgerBuilder(arm="H4/changelog")
        b.call("jobs.submit", side_effect="mutating").turn()  # turn 1
        b.upgrade(stamp=None)
        b.call("jobs.result", "CONFLICT").call("jobs.status").turn()  # turn 2: caught, polled
        b.call("jobs.result", "CONFLICT").turn("error")  # turn 3: escaped
        b.call("jobs.result").turn()  # turn 4
        ledger = b.build_ledger()
        for record in ledger:
            if record.get("kind") == "turn" and record.get("status") == "error":
                record["code"] = "CONFLICT"
        return ledger

    def test_call_scope_counts_the_caught_poll(self):
        self.assertAlmostEqual(
            m.post_upgrade_misuse_rate(self.build(), ["jobs.result"], "CONFLICT"), 2 / 3
        )

    def test_uncaught_scope_counts_only_the_escaped_failure(self):
        self.assertAlmostEqual(
            m.post_upgrade_misuse_rate(self.build(), ["jobs.result"], "CONFLICT", "uncaught_turn"),
            1 / 3,
        )

    def test_not_computable_scope_is_none(self):
        self.assertIsNone(
            m.post_upgrade_misuse_rate(self.build(), ["fs.move"], "INVALID", "not_computable")
        )

    def test_unknown_scope_is_refused(self):
        with self.assertRaises(m.MetricInputError):
            m.post_upgrade_misuse_rate(self.build(), ["jobs.result"], "CONFLICT", "maybe")


class Success(unittest.TestCase):
    def ledger(self):
        return (
            LedgerBuilder()
            .call("fs.write", side_effect="mutating")
            .turn()
            .turn()
            .turn()
            .build_ledger()
        )

    def test_task_success(self):
        self.assertEqual(m.task_success({}, self.ledger(), always(True)), 1)
        self.assertEqual(m.task_success({}, self.ledger(), always(False)), 0)

    def test_task_success_requires_true_not_truthy(self):
        self.assertEqual(m.task_success({}, self.ledger(), always(1)), 0)

    def test_turns_to_success_stop_rule(self):
        self.assertEqual(m.turns_to_success(self.ledger(), always(True), final_state={}), 3)
        self.assertIsNone(m.turns_to_success(self.ledger(), always(False), final_state={}))

    def test_turns_to_success_per_turn_states(self):
        states = {1: {"p": 0}, 2: {"p": 1}, 3: {"p": 1}}

        def pred(state, ledger):
            return state["p"] == 1

        self.assertEqual(m.turns_to_success(self.ledger(), pred, states_after_turn=states), 2)

    def test_predicate_sees_only_the_ledger_prefix(self):
        seen = []

        def pred(state, ledger):
            seen.append(len(ledger))
            return False

        m.turns_to_success(self.ledger(), pred, states_after_turn={1: {}, 2: {}, 3: {}})
        self.assertEqual(seen, [3, 4, 5])

    def test_missing_turn_state_is_an_error(self):
        with self.assertRaises(m.MetricInputError):
            m.turns_to_success(self.ledger(), always(False), states_after_turn={1: {}})


class InstanceMetrics(unittest.TestCase):
    def test_keys_and_applicability(self):
        ledger = LedgerBuilder().call("fs.exists").turn().build_ledger()
        out = m.instance_metrics(ledger, {}, always(True))
        self.assertEqual(set(out), set(m.metric_names()) | {"turns_to_success_method"})
        self.assertIsNone(out["rule_reuse"])
        self.assertIsNone(out["post_upgrade_misuse_rate"])
        self.assertEqual(out["turns_to_success_method"], "stop_rule")

    def test_invariant_violation_detected(self):
        ledger = (
            LedgerBuilder()
            .call("fs.write", "INVARIANT_VIOLATION", "mutating")
            .turn("error")
            .build_ledger()
        )
        self.assertTrue(m.has_invariant_violation(ledger))


if __name__ == "__main__":
    unittest.main()

# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Tests of the predicate helpers: key-store binding, request hashes, purity audit, evaluation.

python3 -m unittest discover -s benchmark/predicates/tests -t .
"""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

COMMON = Path(__file__).resolve().parents[1] / "common.py"
_spec = importlib.util.spec_from_file_location("common", COMMON)
c = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(c)

POST = {"key": "n1", "title": "Notice", "body": "text\n"}
RH = c.request_hash("publish.post", POST)


def adapter(seq, status="ok", code=None, via="service", request_hash=RH):
    return {
        "seq": seq,
        "kind": "adapter",
        "primitive": "publish.post",
        "turn": 1,
        "world_time": seq,
        "outcome": {"status": status, "code": code},
        "gateway": {"request_hash": request_hash, "via": via},
    }


def ledger(*records):
    return [{"seq": 0, "kind": "header", "world_time": 0}, *records]


def events(*names, failure=None):
    """Key-store events for key n1, in order: pending, done (with failure), cleared."""
    out = []
    for name in names:
        event = {"event": name, "adapter": "publish.post", "key": json.dumps("n1")}
        if name in ("pending", "done"):
            event.update({"payload_hash": "p", "request_hash": RH})
        if name == "done":
            event.update({"outcome": None if failure else {"id": "post-1"}, "failure": failure})
        out.append(event)
    return out


def view(records, key_events):
    return c.state_view_from_instance({}, records, key_events)


class KeyStoreBinding(unittest.TestCase):
    """The idempotency entry points at the record that produced the stored outcome."""

    def test_ok_retry_after_rate_limit_is_posted(self):
        records = ledger(adapter(1, "error", "RATE_LIMITED"), adapter(2))
        state = view(records, events("pending", "cleared", "pending", "done"))
        self.assertEqual(c.idempotency_entry_seq(state, "n1"), 2)
        self.assertTrue(c.posted_ok(state, records, "n1"))

    def test_ok_retry_after_invariant_violation_is_posted(self):
        records = ledger(adapter(1, "error", "INVARIANT_VIOLATION"), adapter(2))
        state = view(records, events("pending", "cleared", "pending", "done"))
        self.assertTrue(c.posted_ok(state, records, "n1"))

    def test_store_replays_do_not_move_the_entry(self):
        records = ledger(adapter(1), adapter(2, via="store"))
        state = view(records, events("pending", "done"))
        self.assertEqual(c.idempotency_entry_seq(state, "n1"), 1)

    def test_stored_failure_binds_to_the_failing_record(self):
        failure = {"code": "TIMEOUT", "retry_after": None, "detail": "late"}
        records = ledger(adapter(1, "error", "TIMEOUT"), adapter(2, "error", "TIMEOUT", "store"))
        state = view(records, events("pending", "done", failure=failure))
        self.assertEqual(c.idempotency_entry_seq(state, "n1"), 1)
        self.assertFalse(c.posted_ok(state, records, "n1"))


class RequestHash(unittest.TestCase):
    def test_matches_the_kernel_scheme(self):
        # crates/cwr-kernel/src/gateway/cassette.rs, test request_hash_matches_format_md.
        text = '{"adapter":"search.query","params":{"limit":3,"q":"tennis"}}'
        import hashlib

        expected = "sha256:" + hashlib.sha256(text.encode()).hexdigest()
        self.assertEqual(c.request_hash("search.query", {"q": "tennis", "limit": 3}), expected)

    def test_called_with_binds_parameters(self):
        records = ledger(adapter(1))
        self.assertEqual(len(c.called_with(records, "publish.post", POST)), 1)
        other = dict(POST, body="text")
        self.assertEqual(c.called_with(records, "publish.post", other), [])


class PurityAudit(unittest.TestCase):
    def audit(self, body):
        return c.check_predicate_source(
            "import common as c\n\n\ndef success(state, ledger):\n" + body
        )

    def test_api_use_is_clean(self):
        self.assertEqual(self.audit("    return c.file_exists(state, '/a')\n"), [])

    def test_module_internals_are_refused(self):
        problems = self.audit("    return len(c.Path('/etc/hostname').read_text()) > 0\n")
        self.assertTrue(any("c.Path" in p for p in problems), problems)
        self.assertTrue(self.audit("    return c.json is None\n"))

    def test_aliasing_the_module_is_refused(self):
        self.assertTrue(self.audit("    m = c\n    return m is None\n"))

    def test_loaded_predicate_sees_only_the_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "success.py").write_text(
                "import common as c\n\n\ndef success(state, ledger):\n"
                "    return c.file_exists(state, '/a')\n",
                encoding="utf-8",
            )
            predicate = c.load_predicate(tmp)
            self.assertTrue(predicate({"fs": {"files": {"/a": "x"}}}, []))
        api = c.predicate_api()
        self.assertEqual(
            set(vars(api)) - {"__name__", "__doc__", "__package__", "__loader__", "__spec__"},
            set(c.PREDICATE_API),
        )


class EvaluateTwice(unittest.TestCase):
    def test_mutation_of_the_arguments_is_detected(self):
        def mutating(state, ledger):
            state["touched"] = True
            return True

        with self.assertRaises(ValueError):
            c.evaluate_twice(mutating, {}, [])

    def test_pure_predicate_passes(self):
        self.assertTrue(c.evaluate_twice(lambda state, ledger: True, {}, []))

    def test_non_bool_is_refused(self):
        with self.assertRaises(ValueError):
            c.evaluate_twice(lambda state, ledger: 1, {}, [])


if __name__ == "__main__":
    unittest.main()

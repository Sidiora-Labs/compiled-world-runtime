# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""cwr_core.envelope: the closed code set and the envelope shape rules."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from cwr_core import envelope

REPO = Path(__file__).resolve().parents[2]
SPEC_ORDER = [
    "NOT_FOUND",
    "LOCKED",
    "CONFLICT",
    "INVALID",
    "LIMIT",
    "UNAVAILABLE",
    "RATE_LIMITED",
    "TIMEOUT",
    "REJECTED",
    "DUPLICATE_KEY",
    "INVARIANT_VIOLATION",
]


def env(code: str, **overrides):
    value = {
        "code": code,
        "primitive": "fs.read",
        "precondition": None,
        "limit": None,
        "detail": None,
    }
    if code == "RATE_LIMITED":
        value["retry_after"] = 30
    value.update(overrides)
    return value


class CodeTests(unittest.TestCase):
    def test_exactly_eleven_in_spec_order(self):
        self.assertEqual([code.name for code in envelope.CODES], SPEC_ORDER)
        self.assertEqual(envelope.RETRY_AFTER_CODES, frozenset({"RATE_LIMITED"}))

    def test_matches_world_source_table(self):
        chapter = REPO / "spec" / "cwr" / "world-source.md"
        if not chapter.is_file():
            self.skipTest("world-source.md not present")
        rows = re.findall(
            r"^\| `([A-Z_]+)` \| .*? \| (.*?) \| (absent|required[^|]*) \|$",
            chapter.read_text(encoding="utf-8"),
            flags=re.MULTILINE,
        )
        self.assertEqual([name for name, _g, _r in rows], SPEC_ORDER)
        for name, guidance, retry in rows:
            code = envelope.BY_NAME[name]
            self.assertEqual(code.guidance, guidance.replace("`", ""), name)
            self.assertEqual(code.retry_after, retry.startswith("required"), name)


class ValidateTests(unittest.TestCase):
    def test_every_code_valid(self):
        for name in SPEC_ORDER:
            with self.subTest(name):
                self.assertEqual(envelope.validate(env(name)), [])

    def test_world_source_example(self):
        text = (
            '{"code": "RATE_LIMITED", "primitive": "publish.post", "precondition": null, '
            '"retry_after": 30, "limit": null, "detail": "<content, not guaranteed>"}'
        )
        self.assertEqual(envelope.loads(text)["retry_after"], 30)

    def test_turn_level_and_limit(self):
        self.assertEqual(envelope.validate(env("LIMIT", primitive=None, limit="cpu_ms")), [])
        self.assertEqual(envelope.validate(env("LOCKED", precondition=2)), [])

    def test_rejections(self):
        cases = {
            "unknown code": env("OK"),
            "lowercase code": env("not_found"),
            "code not a string": env(["LOCKED"]),
            "retry_after on TIMEOUT": env("TIMEOUT", retry_after=5),
            "RATE_LIMITED without retry_after": {
                k: v for k, v in env("RATE_LIMITED").items() if k != "retry_after"
            },
            "negative retry_after": env("RATE_LIMITED", retry_after=-1),
            "bool retry_after": env("RATE_LIMITED", retry_after=True),
            "float retry_after": env("RATE_LIMITED", retry_after=1.5),
            "limit on LOCKED": env("LOCKED", limit="cpu_ms"),
            "precondition zero": env("LOCKED", precondition=0),
            "precondition bool": env("LOCKED", precondition=True),
            "primitive number": env("LOCKED", primitive=3),
            "detail object": env("LOCKED", detail={}),
            "unknown key": env("LOCKED", extra=1),
            "missing detail": {k: v for k, v in env("LOCKED").items() if k != "detail"},
            "not an object": ["LOCKED"],
        }
        for label, value in cases.items():
            with self.subTest(label):
                self.assertNotEqual(envelope.validate(value), [], label)

    def test_loads_is_strict(self):
        good = json.dumps(env("LOCKED"))
        self.assertEqual(envelope.loads(good)["code"], "LOCKED")
        for text in ('{"code": "LOCKED", "code": "LOCKED"}', "NaN", "{", json.dumps(env("OK"))):
            with self.subTest(text), self.assertRaises(envelope.EnvelopeError):
                envelope.loads(text)

    def test_all_errors_reported(self):
        errors = envelope.validate({"code": "TIMEOUT", "retry_after": 1, "limit": "x"})
        self.assertGreaterEqual(len(errors), 5, errors)


if __name__ == "__main__":
    unittest.main()

# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""cwr_core.kvx: grammar, located errors, canonical dump, and equivalence with the spec validator."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

from cwr_core import kvx

REPO = Path(__file__).resolve().parents[2]
VALIDATOR = REPO / "spec" / "cwr" / "tools" / "validate_spec.py"


def load_validator():
    spec = importlib.util.spec_from_file_location("cwr_validate_spec", VALIDATOR)
    if spec is None or spec.loader is None:
        raise unittest.SkipTest("spec validator not present")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VALID = """# header comment
[meta]
format = "cwr.example"
schema_version = 1

  [nested.name-1_x]
  list = ["a", 1, 2.5, true, null, {"k": [1]}]
empty_obj = {}
text = "café # not a comment"
"""

INVALID = {
    "inline comment": ("[a]\nk = 1 # trailing\n", 2, 7),
    "duplicate section": ("[a]\n[a]\n", 2, 1),
    "duplicate key": ("[a]\nk = 1\nk = 2\n", 3, 1),
    "out of section": ("k = 1\n", 1, 1),
    "bad section name": ("[a b]\n", 1, 1),
    "key starts with digit": ("[a]\n1k = 1\n", 2, 1),
    "dotted key": ("[a]\na.b = 1\n", 2, 1),
    "bare word": ("[a]\nk = auto\n", 2, 5),
    "NaN": ("[a]\nk = NaN\n", 2, 5),
    "Infinity": ("[a]\nk = -Infinity\n", 2, 5),
    "duplicate object key": ('[a]\nk = {"x": 1, "x": 2}\n', 2, 5),
    "two literals": ("[a]\nk = 1 2\n", 2, 7),
    "missing value": ("[a]\nk =\n", 2, 1),
    "single quotes": ("[a]\nk = 'x'\n", 2, 5),
    "indented error": ("[a]\n    k = [1,\n", 2, 12),
}


class ParseTests(unittest.TestCase):
    def test_valid_document(self):
        data = kvx.loads(VALID)
        self.assertEqual(list(data), ["meta", "nested.name-1_x"])
        self.assertEqual(data["meta"], {"format": "cwr.example", "schema_version": 1})
        self.assertEqual(data["nested.name-1_x"]["list"], ["a", 1, 2.5, True, None, {"k": [1]}])
        self.assertEqual(data["nested.name-1_x"]["text"], "café # not a comment")

    def test_empty_and_comment_only(self):
        self.assertEqual(kvx.loads(""), {})
        self.assertEqual(kvx.loads("# only\n\n   # indented comment\n"), {})

    def test_errors_are_located(self):
        for label, (text, line, col) in INVALID.items():
            with self.subTest(label), self.assertRaises(kvx.KVXError) as caught:
                kvx.loads(text, source="f.kvx")
            error = caught.exception
            self.assertEqual((error.line, error.col), (line, col), str(error))
            self.assertTrue(str(error).startswith(f"f.kvx:{line}:{col}: "), str(error))

    def test_load_reports_path(self):
        path = Path(__file__).resolve().parent / "missing-file-for-test.kvx"
        with self.assertRaises(FileNotFoundError):
            kvx.load(path)


class DumpTests(unittest.TestCase):
    def test_round_trip_and_determinism(self):
        data = {
            "meta": {"format": "cwr.example", "n": -3, "f": 0.5, "ok": False, "none": None},
            "b.c-d_e": {"obj": {"z": 1, "a": [1, {"q": "r"}]}, "s": "line\nbreak\té"},
            "empty": {},
        }
        text = kvx.dump(data, header=["Copyright line", "second"])
        self.assertEqual(kvx.loads(text), data)
        self.assertEqual(text, kvx.dump(data, header=["Copyright line", "second"]))
        self.assertTrue(text.startswith("# Copyright line\n# second\n\n[meta]\n"))
        self.assertTrue(text.endswith("\n") and not text.endswith("\n\n"))
        self.assertIn("\n\n[empty]\n", text)

    def test_unicode_line_separators_stay_on_one_line(self):
        data = {"a": {"s": "x y z\x85w"}}
        text = kvx.dump(data)
        self.assertEqual(len(text.splitlines()), 2)
        self.assertEqual(kvx.loads(text), data)

    def test_rejects_what_the_grammar_rejects(self):
        for bad in ({"a b": {}}, {"": {}}, {"a": {"1k": 1}}, {"a": {"a.b": 1}}):
            with self.subTest(bad), self.assertRaises(ValueError):
                kvx.dump(bad)
        with self.assertRaises(ValueError):
            kvx.dump({"a": {"k": float("nan")}})
        with self.assertRaises(ValueError):
            kvx.dump({}, header=["two\nlines"])
        with self.assertRaises(TypeError):
            kvx.dump({"a": {"k": {1, 2}}})

    def test_empty(self):
        self.assertEqual(kvx.dump({}), "")


class ValidatorEquivalenceTests(unittest.TestCase):
    """Every accept and every reject agrees with parse_kvx in spec/cwr/tools/validate_spec.py."""

    @classmethod
    def setUpClass(cls):
        cls.validator = load_validator()

    def assert_same(self, text: str, label: str) -> None:
        try:
            expected = self.validator.parse_kvx(text)
        except self.validator.InvalidKVX as error:
            with self.assertRaises(kvx.KVXError, msg=label) as caught:
                kvx.loads(text)
            line = str(error).split(":", 1)[0]
            self.assertEqual(line, f"line {caught.exception.line}", label)
            return
        self.assertEqual(kvx.loads(text), expected, label)

    def test_corpus(self):
        self.assert_same(VALID, "VALID")
        for label, (text, _line, _col) in INVALID.items():
            self.assert_same(text, label)

    def test_repository_kvx_files(self):
        roots = [REPO / "spec", REPO / "benchmark", REPO / "examples", REPO / "tests"]
        files = sorted(p for root in roots if root.is_dir() for p in root.rglob("*.kvx"))
        self.assertTrue(files, "no KVX files found to compare")
        for path in files:
            self.assert_same(path.read_text(encoding="utf-8"), str(path.relative_to(REPO)))


if __name__ == "__main__":
    unittest.main()

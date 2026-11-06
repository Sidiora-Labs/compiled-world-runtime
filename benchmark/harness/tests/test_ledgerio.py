# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""ledgerio: LF-only line splitting for ledgers and KVX, and the write_kvx round-trip guard."""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

# Make `benchmark` importable when this file runs directly under python3 -I.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from benchmark.harness.ledgerio import (
    InvalidKVX,
    InvalidLedger,
    dump_kvx,
    parse_kvx,
    read_kvx,
    read_ledger,
    write_kvx,
    write_ledger,
)

# Characters str.splitlines() treats as line breaks that JSON allows unescaped inside a string.
BREAKERS = "  \x85"


class LineBreaks(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_ledger_line_with_raw_unicode_breaks_is_one_record(self):
        path = self.tmp / "ledger.jsonl"
        text = (
            '{"kind":"header","seq":0}\n'
            f'{{"kind":"turn","note":"a{BREAKERS}b","seq":1}}\n'
            f'{{"kind":"turn","note":"x y","seq":2}}\n'
        )
        path.write_bytes(text.encode("utf-8"))
        records = read_ledger(path)
        self.assertEqual([r["seq"] for r in records], [0, 1, 2])
        self.assertEqual(records[1]["note"], f"a{BREAKERS}b")

    def test_ledger_still_refuses_truncation_and_blank_lines(self):
        path = self.tmp / "ledger.jsonl"
        path.write_bytes(b'{"kind":"header","seq":0}\n{"kind":"turn","seq":1}')
        with self.assertRaisesRegex(InvalidLedger, "truncated"):
            read_ledger(path)
        path.write_bytes(b'{"kind":"header","seq":0}\n\n')
        with self.assertRaisesRegex(InvalidLedger, "blank line"):
            read_ledger(path)

    def test_ledger_round_trip_through_write_ledger(self):
        path = self.tmp / "ledger.jsonl"
        records = [{"kind": "header", "seq": 0, "text": f"p{BREAKERS}q"}]
        write_ledger(path, records)
        self.assertEqual(read_ledger(path), records)

    def test_parse_kvx_keeps_raw_breaks_inside_a_value(self):
        text = f'[fs]\nfiles = {{"/a": "line{BREAKERS}next"}}\n'
        self.assertEqual(parse_kvx(text), {"fs": {"files": {"/a": f"line{BREAKERS}next"}}})

    def test_write_kvx_accepts_model_text_with_u0085(self):
        path = self.tmp / "fs.kvx"
        sections = {"fs": {"files": {"/a": "line\x85next", "/b": "x y z"}}}
        write_kvx(path, sections)
        self.assertEqual(read_kvx(path), sections)
        raw = path.read_text(encoding="utf-8")
        self.assertEqual(raw.splitlines(), raw.split("\n")[:-1])

    def test_dump_kvx_is_deterministic_and_rejects_bad_names(self):
        sections = {"s": {"b": 1, "a": {"y": 2, "x": 1}}}
        self.assertEqual(dump_kvx(sections), dump_kvx(sections))
        self.assertEqual(dump_kvx(sections), '[s]\nb = 1\na = {"x": 1, "y": 2}\n')
        with self.assertRaises(InvalidKVX):
            dump_kvx({"bad name": {}})

    def test_parse_kvx_errors_carry_a_line(self):
        with self.assertRaisesRegex(InvalidKVX, "<kvx>:3: duplicate key a"):
            parse_kvx("[s]\na = 1\na = 2\n")


if __name__ == "__main__":
    unittest.main()

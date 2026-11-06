# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""benchmark/campaigns/h1h2.py: the registration freezes once any campaign result exists."""

import argparse
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Make `benchmark` importable when this file runs directly under python3 -I.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from benchmark.analyse import recompute_suite_digest
from benchmark.campaigns import h1h2
from benchmark.harness.ledgerio import REPO

DRAFT = """[meta]
format = "cwr.preregistration"
status = "draft"

[campaign]
model = null
sampling = null
suite_digest = null
cassette_digest = null
upgrades_digest = null
"""


class CompleteRegistration(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(dir=REPO / "benchmark" / "harness" / "tests"))
        self.results = self.tmp / "results"
        self.prereg = self.tmp / "preregistration.kvx"
        self.prereg.write_text(DRAFT)
        self.arms = self.tmp / "arms.kvx"
        self.arms.write_text("[control]\nbuild = null\n")
        patches = [
            mock.patch.object(h1h2, "RESULTS", self.results),
            mock.patch.object(h1h2, "PREREG", self.prereg),
            mock.patch.object(h1h2, "ARMS", self.arms),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def refused(self) -> str:
        before = (self.prereg.read_bytes(), self.arms.read_bytes())
        with self.assertRaises(h1h2.CampaignError) as caught:
            h1h2.complete_registration(argparse.Namespace())
        self.assertEqual((self.prereg.read_bytes(), self.arms.read_bytes()), before)
        return str(caught.exception)

    def test_any_h3_or_h4_attempt_freezes_the_registration(self):
        for hypothesis in ("h3", "h4"):
            cell = self.results / hypothesis / "arm" / "task" / "1"
            cell.mkdir(parents=True)
            (cell / "attempt.kvx").write_text("[attempt]\n")
            self.assertIn(f"{hypothesis.upper()} results exist", self.refused())
            shutil.rmtree(self.results)

    def test_an_already_registered_preregistration_is_never_rewritten(self):
        self.prereg.write_text(DRAFT.replace('status = "draft"', 'status = "registered"'))
        self.assertIn("already registered", self.refused())

    def test_an_upgrades_digest_that_differs_is_refused_not_tolerated(self):
        self.prereg.write_text(
            DRAFT.replace("upgrades_digest = null", 'upgrades_digest = "sha256:' + "0" * 64 + '"')
        )
        cassettes = self.tmp / "cassettes"
        cassettes.mkdir()
        tasks = self.tmp / "tasks"
        (tasks / "t001-task").mkdir(parents=True)
        (tasks / "t001-task" / "success.py").write_text(
            "def success(state, ledger):\n    return True\n"
        )
        suite = self.tmp / "suite.kvx"
        suite.write_text(f'[suite]\nsuite_digest = "{recompute_suite_digest(tasks)}"\n')
        build = "sha256:" + "a" * 64
        campaigns = {
            "H1": {"registration": {"control_build": build}},
            "H2": {"registration": {"builds": {"one-primitive": build}}},
        }
        self.arms.write_text("[control]\nbuild = null\n\n[arm.h2.one-primitive]\nbuild = null\n")
        environ = {
            "MESSAGES_URL": "https://gateway.invalid/v1/messages",
            "MODEL": "m",
            "CLUSTER_1_GATEWAY_API_KEY": "k",
        }
        with (
            mock.patch.object(h1h2, "load_campaign", campaigns.__getitem__),
            mock.patch.object(h1h2, "CASSETTES", cassettes),
            mock.patch.object(h1h2, "SUITE", suite),
            mock.patch.object(h1h2, "TASKS", tasks),
            mock.patch.dict("os.environ", environ),
        ):
            self.assertIn("upgrades_digest is registered as another value", self.refused())
            suite.write_text('[suite]\nsuite_digest = "sha256:1"\n')
            self.assertIn("but its tasks digest to", self.refused())

    def test_blockers_are_empty_for_a_draft_without_results(self):
        self.assertEqual(h1h2.registration_blockers(), [])


if __name__ == "__main__":
    unittest.main()

# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""tools/release.py: version agreement, refusals and byte-reproducible bundles."""

from __future__ import annotations

import gzip
import hashlib
import io
import tarfile
import tempfile
import unittest
from pathlib import Path

import release

WORKSPACE = '[workspace]\nmembers = ["crates/cwr-cli"]\n[workspace.package]\nversion = "{v}"\n'
CLI = '[package]\nname = "cwr-cli"\nversion.workspace = true\n'
PYPROJECT = '[project]\nname = "cwr"\nversion = "{v}"\n'
PINS = '[meta]\nformat = "cwr.release"\nrelease = {v}\n'


def make_root(base: Path, cli: str | None, sdk: str | None, pin: str | None) -> Path:
    if cli is not None:
        (base / "crates" / "cwr-cli").mkdir(parents=True)
        (base / "Cargo.toml").write_text(WORKSPACE.format(v=cli))
        (base / "crates" / "cwr-cli" / "Cargo.toml").write_text(CLI)
    if sdk is not None:
        (base / "sdk" / "python").mkdir(parents=True)
        (base / "sdk" / "python" / "pyproject.toml").write_text(PYPROJECT.format(v=sdk))
    if pin is not None:
        (base / "benchmark").mkdir()
        (base / "benchmark" / "release.kvx").write_text(PINS.format(v=pin))
    return base


class VersionTests(unittest.TestCase):
    def check(self, cli, sdk, pin, snapshot=False):
        with tempfile.TemporaryDirectory() as tmp:
            return release.check_versions(make_root(Path(tmp), cli, sdk, pin), snapshot)

    def test_agreement(self):
        self.assertEqual(self.check("0.2.0", "0.2.0", '"0.2.0"'), ("0.2.0", []))

    def test_missing_sources_are_skipped(self):
        self.assertEqual(self.check("0.2.0", None, None), ("0.2.0", []))

    def test_disagreement_is_refused(self):
        version, errors = self.check("0.2.0", "0.3.0", '"0.2.0"')
        self.assertIsNone(version)
        self.assertTrue(any("disagree" in e for e in errors), errors)

    def test_unpinned_release_refused_unless_snapshot(self):
        _, errors = self.check("0.2.0", None, "null")
        self.assertTrue(any("not pinned" in e for e in errors), errors)
        self.assertEqual(self.check("0.2.0", None, "null", snapshot=True), ("0.2.0", []))

    def test_malformed_version(self):
        _, errors = self.check("0.2", None, None)
        self.assertTrue(any("MAJOR.MINOR.PATCH" in e for e in errors), errors)

    def test_no_sources(self):
        version, errors = self.check(None, None, None)
        self.assertIsNone(version)
        self.assertTrue(errors)

    def test_invalid_kvx_is_reported_not_raised(self):
        _, errors = self.check("0.2.0", None, '"0.2.0" # inline')
        self.assertTrue(any("cannot read" in e for e in errors), errors)


class BundleTests(unittest.TestCase):
    def test_reproducible_with_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "a.txt").write_text("alpha\n")
            (base / "run.sh").write_text("#!/bin/sh\n")
            (base / "run.sh").chmod(0o755)
            members = [("run.sh", base / "run.sh"), ("a.txt", base / "a.txt")]
            first = release.build_bundle("cwr-0.2.0", members)
            second = release.build_bundle("cwr-0.2.0", list(reversed(members)))
        self.assertEqual(first, second)
        with tarfile.open(fileobj=io.BytesIO(gzip.decompress(first))) as archive:
            names = archive.getnames()
            infos = {info.name: info for info in archive.getmembers()}
            manifest = archive.extractfile("cwr-0.2.0/MANIFEST.sha256").read().decode()
        self.assertEqual(
            names, ["cwr-0.2.0/a.txt", "cwr-0.2.0/run.sh", "cwr-0.2.0/MANIFEST.sha256"]
        )
        self.assertEqual(infos["cwr-0.2.0/run.sh"].mode, 0o755)
        self.assertEqual(infos["cwr-0.2.0/a.txt"].mode, 0o644)
        self.assertTrue(all(info.mtime == 0 and info.uid == 0 for info in infos.values()))
        digest = hashlib.sha256(b"alpha\n").hexdigest()
        self.assertIn(f"{digest}  a.txt\n", manifest)

    def test_secret_patterns(self):
        for path in (".env", "x/.env.local", "prod.env", "certs/server.pem", "k.key", "secrets/a"):
            self.assertTrue(release.SECRET_RE.search(path), path)
        for path in ("environment.md", "src/env.rs", "keys.md", "docs/.envrc.md"):
            self.assertFalse(release.SECRET_RE.search(path), path)


if __name__ == "__main__":
    unittest.main()

# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""tools/codemap.py: parsers and deterministic rendering."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import codemap

MAKEFILE = """# Targets:
#   build      compile everything
#   check-all  run the gate
X := 1
.PHONY: build check-all
build:
\techo build
check-all: build
\techo check
"""


class ParserTests(unittest.TestCase):
    def test_make_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "Makefile").write_text(MAKEFILE)
            rows = codemap.make_targets(Path(tmp))
        self.assertIn("| `make build` | - | compile everything |", rows)
        self.assertIn("| `make check-all` | `build` | run the gate |", rows)
        self.assertFalse(any("make X" in row or "PHONY" in row for row in rows))

    def test_readme_heading_strips_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "README.md").write_text("<!-- c -->\n\n# The `cwr` [tool](x.md) | a\n")
            self.assertEqual(codemap.readme_heading(Path(tmp)), "The cwr tool \\| a")

    def test_purpose_line_drops_labels_and_task_references(self):
        self.assertEqual(
            codemap.purpose_line("artifacts", "artifacts: release bundles"), "Release bundles"
        )
        self.assertEqual(codemap.purpose_line("docs/cwr", "docs/cwr: user guides"), "User guides")
        self.assertEqual(
            codemap.purpose_line("benchmark/tasks", "CWR task suite (task 4.2)"), "CWR task suite"
        )
        self.assertEqual(codemap.purpose_line("examples", "refworld pairs"), "refworld pairs")

    def test_layout_directories_skip_vendored_and_hidden_subdirectories(self):
        files = [
            ".githooks/pre-commit",
            ".githooks/.cache/x",
            "vendor/README.md",
            "vendor/cargo/serde/README.md",
            "tests/cwr/evidence/CWR-001/a.json",
            "docs/cwr/guide.md",
        ]
        self.assertEqual(
            codemap.layout_directories(files),
            {".githooks", "vendor", "tests", "tests/cwr", "tests/cwr/evidence"}
            | {"docs", "docs/cwr"},
        )

    def test_layout_table_names_the_described_child_of_a_bare_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs" / "cwr").mkdir(parents=True)
            (root / "docs" / "cwr" / "README.md").write_text("# User guides\n")
            (root / "empty").mkdir()
            rows = codemap.layout_table(root, ["docs/cwr/README.md", "empty/x.txt"])
        self.assertIn("| `docs/` | Holds `docs/cwr/`: User guides |", rows)
        self.assertIn("| `docs/cwr/` | User guides |", rows)
        self.assertIn("| `empty/` | Not yet described. |", rows)

    def test_sole_file_comment_describes_a_configuration_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".cargo").mkdir()
            (root / ".cargo" / "config.toml").write_text(
                "# Copyright X\n# SPDX-License-Identifier: Y\n\n"
                "# Build from vendored sources. Refresh often.\n# Second line.\n[source]\n"
            )
            files = [".cargo/config.toml"]
            self.assertEqual(
                codemap.sole_file_comment(root, ".cargo", files), "Build from vendored sources"
            )
            self.assertIsNone(codemap.sole_file_comment(root, ".cargo", [*files, ".cargo/b"]))

    def test_line_count(self):
        self.assertEqual(codemap.count_lines(b""), 0)
        self.assertEqual(codemap.count_lines(b"a\nb\n"), 2)
        self.assertEqual(codemap.count_lines(b"a\nb"), 2)


class RenderTests(unittest.TestCase):
    def test_repository_render_is_deterministic(self):
        first = codemap.render(codemap.ROOT)
        self.assertEqual(first, codemap.render(codemap.ROOT))
        self.assertTrue(first.startswith(codemap.HEADER[0] + "\n" + codemap.HEADER[1] + "\n"))
        self.assertIn(codemap.BANNER, first)
        for section in ("## Languages", "## Layout", "## Crates", "## Make targets"):
            self.assertIn(section, first)


if __name__ == "__main__":
    unittest.main()

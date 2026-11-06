# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Turn script, U6 second half on side B: write both final notes (doc-only control)."""

import fs

fs.write(path="/notes/today.md", content=b"final")
fs.write(path="/notes/status.md", content=b"done")
return fs.read(path="/notes/today.md").decode()

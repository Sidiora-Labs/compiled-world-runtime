# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Turn script, U6 first half on side A: write and read back a draft note."""

import fs

fs.write(path="/notes/today.md", content=b"draft")
return fs.read(path="/notes/today.md").decode()

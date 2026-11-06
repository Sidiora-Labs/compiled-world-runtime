# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Turn script, U2 first half on side A: move two inbox files into /done/ with fs.move."""

import fs

for name in ["1.txt", "2.txt"]:
    fs.move(src="/inbox/" + name, dst="/done/" + name)
return fs.list(prefix="/done/")

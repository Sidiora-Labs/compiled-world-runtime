# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Turn script, U1 first half on side A: delete every file under /batch/one/."""

import fs

deleted = []
for path in fs.list(prefix="/batch/one/"):
    fs.delete(path=path)
    deleted.append(path)
return deleted

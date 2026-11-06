# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Turn script, U1 second half on side B after the notice: unlock, then delete /batch/two/."""

import fs

deleted = []
for path in fs.list(prefix="/batch/two/"):
    if fs.locked(path=path):
        fs.unlock(path=path)
    fs.delete(path=path)
    deleted.append(path)
return deleted

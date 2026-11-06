# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Turn script, U1 second half ignoring the notice: delete /batch/two/ without unlocking."""

import fs

deleted = []
for path in fs.list(prefix="/batch/two/"):
    fs.delete(path=path)
    deleted.append(path)
return deleted

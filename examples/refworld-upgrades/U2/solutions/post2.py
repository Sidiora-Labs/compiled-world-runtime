# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Turn script, U2 second half on side B after the notice: read, write, delete."""

import fs

for path in fs.list(prefix="/inbox/"):
    target = "/done/" + path[len("/inbox/") :]
    fs.write(path=target, content=fs.read(path=path))
    fs.delete(path=path)
return fs.list(prefix="/done/")

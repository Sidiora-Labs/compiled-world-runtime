# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Turn script, U2 second half ignoring the notice: still calls the removed fs.move."""

import fs

for path in fs.list(prefix="/inbox/"):
    fs.move(src=path, dst="/done/" + path[len("/inbox/") :])
return fs.list(prefix="/done/")

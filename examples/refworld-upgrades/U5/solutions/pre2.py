# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Turn script, U5 second half ignoring the notice: calls kv.get without ns."""

import fs
import kv

written = []
for key in ["receipt-tennis", "receipt-chess"]:
    fs.write(path="/receipts/" + key + ".txt", content=kv.get(key=key))
    written.append(key)
return written

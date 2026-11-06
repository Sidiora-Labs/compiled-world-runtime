# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Turn script, U5 first half on side A: copy one receipt from the flat kv to a file."""

import fs
import kv

value = kv.get(key="receipt-golf")
fs.write(path="/receipts/receipt-golf.txt", content=value)
return value.decode()

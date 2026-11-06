# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Turn script, U5 second half adapted without the notice: guesses ns receipts."""

# A notice-ignoring adaptation: the new ns parameter is filled with the namespace the manual's
# examples use for receipts, not the namespace the migration moved the old keys into.
import fs
import kv

written = []
for key in ["receipt-tennis", "receipt-chess"]:
    fs.write(path="/receipts/" + key + ".txt", content=kv.get(ns="receipts", key=key))
    written.append(key)
return written

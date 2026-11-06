# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Turn script, U4 second half ignoring the notice: still 1024-byte chunks."""

import fs
import kv

LIMIT = 1024


def store(name, content):
    if len(content) <= LIMIT:
        kv.set(ns="receipts", key=name, value=content)
        return 1
    parts = [content[start : start + LIMIT] for start in range(0, len(content), LIMIT)]
    for index, part in enumerate(parts, 1):
        kv.set(ns="receipts", key=name + "." + str(index), value=part)
    return len(parts)


stored = {}
for name in ["r3.txt"]:
    stored[name] = store(name, fs.read(path="/receipts/" + name))
return stored

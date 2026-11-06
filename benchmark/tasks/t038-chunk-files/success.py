# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t038-chunk-files: Store files in kv chunks.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    sizes = {"x.txt": 2500, "y.txt": 1100, "z.txt": 3000}
    for name, size in sizes.items():
        expected = "".join(chr(97 + (i * 7 + len(name)) % 26) for i in range(size))
        parts, i = [], 0
        while c.kv_has(state, "chunks", f"{name}.{i}"):
            parts.append(c.kv_get(state, "chunks", f"{name}.{i}"))
            i += 1
        if "".join(parts) != expected:
            return False
    return True

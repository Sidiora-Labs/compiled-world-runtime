# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t011-split-blob: Store a large file in kv.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    expected = "".join(
        f"blob line {i:03d}: the quick brown fox jumps over the lazy dog\n" for i in range(30)
    )
    parts, index = [], 0
    while c.kv_has(state, "blobs", f"blob.{index}"):
        parts.append(c.kv_get(state, "blobs", f"blob.{index}"))
        index += 1
    others = [k for k in c.kv_keys(state, "blobs") if k not in [f"blob.{i}" for i in range(index)]]
    return index > 0 and "".join(parts) == expected and not others

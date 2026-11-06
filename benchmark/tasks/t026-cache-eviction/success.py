# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t026-cache-eviction: Fill a cache.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    keys = c.kv_keys(state, "cache")
    fresh = [f"n{i}.txt" for i in range(1, 7)]
    if not all(c.kv_get(state, "cache", k) == ((f"{i}") * 1000) for i, k in enumerate(fresh, 1)):
        return False
    return (
        all(k in fresh or k == "pinned" or k.startswith("stale-") for k in keys)
        and c.kv_get(state, "cache", "pinned") == "keep"
    )

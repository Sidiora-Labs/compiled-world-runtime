# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t025-kv-migrate: Move a kv namespace.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    expected = {f"k{i}": f"legacy value {i}" for i in range(1, 7)}
    return (
        c.kv_keys(state, "legacy") == []
        and c.kv_keys(state, "current") == sorted(expected)
        and all(c.kv_get(state, "current", k) == v for k, v in expected.items())
    )

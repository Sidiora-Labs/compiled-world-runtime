# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t039-lock-audit: Audit vault locks.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    locked = ["/vault/v2.dat", "/vault/v4.dat", "/vault/v5.dat", "/vault/v8.dat"]
    paths = [f"/vault/v{i}.dat" for i in range(1, 10)]
    return (
        c.locked_under(state, "/vault/") == locked
        and c.kv_keys(state, "vault") == paths
        and all(
            c.kv_get(state, "vault", p) == ("locked" if p in locked else "unlocked") for p in paths
        )
    )

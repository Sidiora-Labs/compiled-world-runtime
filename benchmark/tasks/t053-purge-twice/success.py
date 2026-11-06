# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t053-purge-twice: Purge a queue around a namespace move.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    expected = {f"job-{i}": f"payload {i}" for i in range(1, 8)}
    return (
        c.files_under(state, "/queue/") == ["/queue/active.job"]
        and c.kv_keys(state, "jobs-v1") == []
        and c.kv_keys(state, "jobs-v2") == sorted(expected)
        and all(c.kv_get(state, "jobs-v2", k) == v for k, v in expected.items())
        and c.brackets(ledger, "fs.delete", "kv.set")
    )

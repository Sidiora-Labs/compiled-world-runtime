# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t022-notice-versions: Publish three notice versions.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    by_id = {c.receipt_id(state, k): k for k in c.idempotency_keys(state)}
    keys = []
    for name in ["notice-v1", "notice-v2", "notice-v3"]:
        value = c.kv_get(state, "receipts", name)
        if value not in by_id or not c.posted_ok(state, ledger, by_id[value]):
            return False
        keys.append(by_id[value])
    return len(set(keys)) == 3 and c.ok_count(ledger, "publish.delete") == 0

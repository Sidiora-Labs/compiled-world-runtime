# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t036-retract-post: Publish and retract.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    if not all(c.posted_ok(state, ledger, k) for k in ["a", "b", "c"]):
        return False
    ids = {k: c.receipt_id(state, k) for k in ["a", "b", "c"]}
    deleted = {
        k: c.called_with(ledger, "publish.delete", {"post_id": ids[k]})
        for k in ids
        if isinstance(ids[k], str)
    }
    b_seq = c.idempotency_entry_seq(state, "b")
    return (
        len(deleted) == 3
        and len(deleted["b"]) >= 1
        and b_seq is not None
        and deleted["b"][0]["seq"] > b_seq
        and deleted["a"] == []
        and deleted["c"] == []
        and c.kv_get(state, "receipts", "a") == ids["a"]
        and c.kv_get(state, "receipts", "c") == ids["c"]
        and not c.kv_has(state, "receipts", "b")
    )

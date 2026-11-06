# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t036-retract-post: Publish and retract.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    if not all(c.posted_ok(state, ledger, k) for k in ["a", "b", "c"]):
        return False
    deletes = c.calls(ledger, "publish.delete", status="ok")
    b_seq = c.idempotency_entry_seq(state, "b")
    return (
        len(deletes) >= 1
        and b_seq is not None
        and deletes[0]["seq"] > b_seq
        and c.kv_get(state, "receipts", "a") == c.receipt_id(state, "a")
        and c.kv_get(state, "receipts", "c") == c.receipt_id(state, "c")
        and not c.kv_has(state, "receipts", "b")
    )

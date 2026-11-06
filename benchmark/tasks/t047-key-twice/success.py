# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t047-key-twice: Two documents in two versions.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    by_id = {c.receipt_id(state, k): k for k in c.idempotency_keys(state)}
    for doc in ["plan", "budget"]:
        v1 = c.kv_get(state, "receipts", doc + "-v1")
        v2 = c.kv_get(state, "receipts", doc + "-v2")
        if v1 is None or v1 != c.receipt_id(state, doc) or not c.posted_ok(state, ledger, doc):
            return False
        if v2 not in by_id or by_id[v2] == doc or not c.posted_ok(state, ledger, by_id[v2]):
            return False
    copies = all(
        c.file_text(state, f"/minutes-archive/m{i:02d}.txt") == f"minutes {i}\n"
        for i in range(1, 11)
    )
    return copies and c.brackets(ledger, "publish.post", "fs.write")

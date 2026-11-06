# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t047-key-twice: Two documents in two versions.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    for doc, title in [("plan", "Plan"), ("budget", "Budget")]:
        for v, key in [("v1", doc), ("v2", doc + "-v2")]:
            body = f"{doc}, version {v[1:]}\n"
            post = {"key": key, "title": title, "body": body}
            if not c.posted_ok(state, ledger, key):
                return False
            if c.kv_get(state, "receipts", f"{doc}-{v}") != c.receipt_id(state, key):
                return False
            if len(c.called_with(ledger, "publish.post", post)) < 1:
                return False
    copies = all(
        c.file_text(state, f"/minutes-archive/m{i:02d}.txt") == f"minutes {i}\n"
        for i in range(1, 11)
    )
    return copies and c.brackets(ledger, "publish.post", "fs.write")

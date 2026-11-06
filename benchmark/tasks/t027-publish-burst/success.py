# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t027-publish-burst: Publish a batch of posts.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    for i in range(1, 6):
        key = f"a{i}"
        if not c.posted_ok(state, ledger, key):
            return False
        if c.kv_get(state, "receipts", key) != c.receipt_id(state, key):
            return False
    return c.rate_limited_beyond_first(ledger, "publish.post") == 0

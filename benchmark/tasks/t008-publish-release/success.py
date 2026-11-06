# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t008-publish-release: Publish a release note.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    return c.posted_ok(state, ledger, "rel-1.0") and c.kv_get(
        state, "receipts", "rel-1.0"
    ) == c.receipt_id(state, "rel-1.0")

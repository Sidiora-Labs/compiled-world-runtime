# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t022-notice-versions: Publish three notice versions.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    for name in ["notice-v1", "notice-v2", "notice-v3"]:
        body = c.file_text(state, "/posts/" + name + ".txt")
        post = {"key": name, "title": "Maintenance notice", "body": body}
        if body is None or not c.posted_ok(state, ledger, name):
            return False
        if c.kv_get(state, "receipts", name) != c.receipt_id(state, name):
            return False
        if len(c.called_with(ledger, "publish.post", post)) < 1:
            return False
    return c.ok_count(ledger, "publish.delete") == 0

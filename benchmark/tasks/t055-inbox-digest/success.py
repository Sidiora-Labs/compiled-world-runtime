# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t055-inbox-digest: Enrich and digest an inbox.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    if c.files_under(state, "/inbox/") != []:
        return False
    for i in range(1, 16):
        text = c.file_text(state, f"/done/m{i:02d}.txt")
        original = f"topic {i}\nbody of message {i}\n"
        if text is None or not text.startswith(original):
            return False
        extra = text[len(original) :].splitlines()
        if len(extra) != 1 or not extra[0].startswith("top: "):
            return False
    for n in (1, 2, 3):
        key = f"digest-{n}"
        if not c.posted_ok(state, ledger, key) or c.kv_get(state, "digests", key) != c.receipt_id(
            state, key
        ):
            return False
    return (
        c.ok_count(ledger, "search.query") >= 15
        and c.rate_limited_beyond_first(ledger, "publish.post") == 0
    )

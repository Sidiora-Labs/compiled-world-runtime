# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t046-burst-twice: Two publishing bursts around backups.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    keys = ["e1", "e2", "e3", "e4", "l1", "l2", "l3", "l4"]
    backups = all(
        c.file_text(state, f"/pages/p{i:02d}.txt.bak") == f"page {i}\n" for i in range(1, 11)
    )
    return (
        all(c.posted_ok(state, ledger, k) for k in keys)
        and backups
        and c.rate_limited_beyond_first(ledger, "publish.post") == 0
        and c.brackets(ledger, "publish.post", "fs.write")
    )

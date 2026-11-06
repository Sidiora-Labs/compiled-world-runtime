# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t048-search-twice: Two search batches around a filing run.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    early = c.file_lines(state, "/out/early.md")
    late = c.file_lines(state, "/out/late.md")
    moved = all(
        c.file_text(state, f"/sorted/i{i:02d}.txt") == f"incoming {i}\n"
        and c.is_locked(state, f"/sorted/i{i:02d}.txt")
        for i in range(1, 16)
    )
    return (
        early is not None
        and len(early) == 12
        and late is not None
        and len(late) == 12
        and moved
        and c.files_under(state, "/incoming/") == []
        and c.ok_count(ledger, "search.query") >= 24
        and c.rate_limited_beyond_first(ledger, "search.query") == 0
        and c.brackets(ledger, "search.query", "fs.move")
    )

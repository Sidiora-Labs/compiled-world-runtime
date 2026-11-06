# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t048-search-twice: Two search batches around a filing run.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    early_q = [
        "river deltas",
        "moss",
        "lighthouses",
        "chess endgames",
        "saffron",
        "comets",
        "canal locks",
        "harpsichords",
        "permafrost",
        "falconry",
    ]
    late_q = [
        "sundials",
        "kelp forests",
        "bridges",
        "fountain pens",
        "magnetars",
        "windmills",
        "obsidian",
        "lantern festivals",
        "cave paintings",
        "glassblowing",
        "tea ceremony",
        "auroras",
    ]
    early = c.file_lines(state, "/out/early.md")
    late = c.file_lines(state, "/out/late.md")
    moved = all(
        c.file_text(state, f"/sorted/i{i:02d}.txt") == f"incoming {i}\n"
        and c.is_locked(state, f"/sorted/i{i:02d}.txt")
        for i in range(1, 16)
    )
    searched = all(
        len(c.called_with(ledger, "search.query", {"q": q, "limit": 50})) >= 1
        for q in early_q + late_q
    )
    return (
        early is not None
        and len(early) == len(early_q)
        and late is not None
        and len(late) == len(late_q)
        and moved
        and searched
        and c.files_under(state, "/incoming/") == []
        and c.rate_limited_beyond_first(ledger, "search.query") == 0
        and c.brackets(ledger, "search.query", "fs.move")
    )

# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t030-search-sweep: Search sweep.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    queries = [
        "mountain huts",
        "rust borrow checker",
        "fermentation",
        "kite surfing",
        "lichens",
        "tram networks",
        "ceramics glazes",
        "solar sails",
        "origami",
        "beekeeping",
        "glacier retreat",
        "typesetting",
    ]
    lines = c.file_lines(state, "/sweep/top.md")
    searched = all(
        len(c.called_with(ledger, "search.query", {"q": q, "limit": 2})) >= 1 for q in queries
    )
    return (
        lines is not None
        and len(lines) == 12
        and searched
        and c.rate_limited_beyond_first(ledger, "search.query") == 0
    )

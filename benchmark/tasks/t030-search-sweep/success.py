# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t030-search-sweep: Search sweep.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    lines = c.file_lines(state, "/sweep/top.md")
    return (
        lines is not None
        and len(lines) == 12
        and c.ok_count(ledger, "search.query") >= 12
        and c.rate_limited_beyond_first(ledger, "search.query") == 0
    )

# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t021-sports-report: Sports roundup.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    golf = c.file_lines(state, "/reports/golf.md")
    tennis = c.file_lines(state, "/reports/tennis.md")
    sports = c.file_lines(state, "/reports/sports.md")
    return (
        golf is not None
        and tennis is not None
        and 1 <= len(golf) <= 3
        and 1 <= len(tennis) <= 3
        and sports == golf + tennis
        and c.is_locked(state, "/reports/sports.md")
        and c.posted_ok(state, ledger, "sports-1")
        and c.kv_get(state, "receipts", "sports-1") == c.receipt_id(state, "sports-1")
        and c.ok_count(ledger, "search.query") >= 2
    )

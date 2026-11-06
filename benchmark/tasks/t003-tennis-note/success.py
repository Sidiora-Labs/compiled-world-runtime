# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t003-tennis-note: Note the top tennis hit.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    lines = c.file_lines(state, "/notes/tennis.md")
    search_seq = c.first_seq(ledger, "search.query")
    write_seq = c.last_seq(ledger, "fs.write")
    return (
        lines is not None
        and len(lines) == 1
        and 0 < len(lines[0]) <= 1000
        and search_seq is not None
        and write_seq is not None
        and search_seq < write_seq
    )

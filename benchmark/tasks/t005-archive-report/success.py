# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t005-archive-report: Archive a report.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    return c.file_text(
        state, "/archive/report.txt"
    ) == "Q3 report, version 2\n" and not c.file_exists(state, "/inbox/report.txt")

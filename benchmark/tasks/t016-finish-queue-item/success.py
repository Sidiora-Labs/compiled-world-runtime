# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t016-finish-queue-item: Move a queue item.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    return c.file_text(
        state, "/done/job-17.txt"
    ) == "job 17: resize images\n" and not c.file_exists(state, "/queue/job-17.txt")

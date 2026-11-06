# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t007-lock-swap: Swap locks.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    return (
        c.is_locked(state, "/contracts/final.txt")
        and not c.is_locked(state, "/contracts/draft.txt")
        and c.file_text(state, "/contracts/final.txt") == "Final terms.\n"
        and c.file_text(state, "/contracts/draft.txt") == "Draft terms.\n"
    )

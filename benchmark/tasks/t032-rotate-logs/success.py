# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t032-rotate-logs: Rotate logs.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    for log in ["app", "db"]:
        base = f"/logs/{log}.log"
        if c.file_text(state, base) != "" or not c.is_locked(state, base):
            return False
        if c.file_text(state, base + ".1") != f"{log} live\n":
            return False
        if c.file_text(state, base + ".2") != f"{log} gen 1\n":
            return False
        if c.file_text(state, base + ".3") != f"{log} gen 2\n":
            return False
    return True

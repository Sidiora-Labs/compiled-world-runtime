# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t042-cleanup-twice: Clean reports between two cleanups.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    for i in range(1, 13):
        raw = "".join(f"row {i} col {k}   \n" for k in range(3))
        clean = "".join(f"row {i} col {k}\n" for k in range(3))
        if c.file_text(state, f"/reports/raw/r{i:02d}.txt") != raw:
            return False
        if c.file_text(state, f"/reports/clean/r{i:02d}.txt") != clean:
            return False
    return c.files_under(state, "/scratch/") == ["/scratch/keep.txt"] and c.brackets(
        ledger, "fs.delete", "fs.write"
    )

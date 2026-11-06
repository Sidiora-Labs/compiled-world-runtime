# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t043-archive-twice: Archive quarters around a summary.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    for q in ["q1", "q2", "q3"]:
        if c.file_text(state, f"/archive/{q}.txt") != f"{q} figures, final\n":
            return False
    for i in range(1, 13):
        name = f"d{i:02d}.txt"
        if c.file_text(state, "/summary/" + name) != f"# {name}\nmeasurement {(i * 3)}\n":
            return False
    return c.files_under(state, "/inbox/") == [] and c.brackets(ledger, "fs.move", "fs.write")

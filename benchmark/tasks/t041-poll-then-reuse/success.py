# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t041-poll-then-reuse: Two index jobs around a rewrite.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    notes = {
        f"/notes/n{i:02d}.txt": (f"note {i:02d}: follow up on item {i}\n").upper()
        for i in range(1, 13)
    }

    def stored(key, n):
        value = c.kv_get(state, "results", key)
        return value is not None and any(
            state["jobs"]["jobs"][j]["n"] == n and c.job_result(state, j) == value
            for j in c.jobs_of_kind(state, "index")
        )

    return (
        stored("index-1", 6)
        and stored("index-2", 9)
        and all(c.file_text(state, p) == t for p, t in notes.items())
        and c.brackets(ledger, "jobs.result", "fs.write")
    )

# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t040-assign-reviewers: Assign reviewers at random.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    reviewers = ["ines", "kofi", "mei", "rafael"]
    tasks = [f"/tasks/task-{i}.txt" for i in range(1, 7)]
    return (
        all(c.kv_get(state, "assign", t) in reviewers for t in tasks)
        and c.ok_count(ledger, "world.random") >= 6
    )

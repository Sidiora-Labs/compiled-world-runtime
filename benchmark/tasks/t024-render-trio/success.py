# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t024-render-trio: Run three render jobs.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    for n in [5, 12, 20]:
        value = c.kv_get(state, "results", f"render-{n}")
        matches = [
            j
            for j in c.jobs_of_kind(state, "render")
            if state["jobs"]["jobs"][j]["n"] == n and c.job_result(state, j) == value
        ]
        if value is None or not matches:
            return False
    return True

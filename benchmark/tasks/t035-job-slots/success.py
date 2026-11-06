# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t035-job-slots: Run four scan jobs.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    scans = [j for j in c.jobs_of_kind(state, "scan") if state["jobs"]["jobs"][j]["n"] == 10]
    if len(scans) < 4:
        return False
    return all(
        c.kv_get(state, "scans", f"scan-{(i + 1)}") == c.job_result(state, j)
        and c.job_result(state, j) is not None
        for i, j in enumerate(scans[:4])
    )

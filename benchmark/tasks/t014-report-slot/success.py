# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t014-report-slot: Run a report job.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    value = c.kv_get(state, "results", "report")
    for job_id in c.jobs_of_kind(state, "report"):
        if state["jobs"]["jobs"][job_id]["n"] == 2 and c.job_result(state, job_id) == value:
            return value is not None
    return False

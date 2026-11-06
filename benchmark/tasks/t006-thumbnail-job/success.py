# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t006-thumbnail-job: Run one job.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    value = c.kv_get(state, "results", "thumbnail")
    for job_id in c.jobs_of_kind(state, "thumbnail"):
        if (
            c.job_status(state, job_id) == "done"
            and c.job_result(state, job_id) == value
            and state["jobs"]["jobs"][job_id]["n"] == 3
        ):
            return True
    return False

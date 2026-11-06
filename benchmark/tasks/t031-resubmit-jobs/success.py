# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t031-resubmit-jobs: Resubmit mis-sized jobs.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    if any(c.job_status(state, j) != "failed" for j in ["enc-a", "enc-b", "enc-c"]):
        return False
    good = {
        c.job_result(state, j)
        for j in c.jobs_of_kind(state, "encode")
        if state["jobs"]["jobs"][j]["n"] == 4 and c.job_result(state, j) is not None
    }
    values = [c.kv_get(state, "results", k) for k in ["enc-a", "enc-b", "enc-c"]]
    return all(v in good for v in values) and len(set(values)) == 3

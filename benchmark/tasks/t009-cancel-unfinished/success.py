# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t009-cancel-unfinished: Cancel unfinished jobs.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    return (
        c.job_status(state, "job-reindex") == "failed"
        and c.job_status(state, "job-cleanup") == "failed"
        and c.job_status(state, "job-backup") == "done"
        and c.job_result(state, "job-backup") is not None
    )

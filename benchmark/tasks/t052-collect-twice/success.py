# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t052-collect-twice: Collect nightly jobs around a conversion.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    jobs_ok = all(
        c.kv_get(state, "nightly", j) == c.job_result(state, j)
        and c.job_result(state, j) is not None
        for j in ["nightly-a", "nightly-b", "nightly-c"]
    )
    tsv = all(
        c.file_text(state, f"/tsv/t{i:02d}.tsv") == f"id\tvalue\n{i}\t{(i * i)}\n"
        for i in range(1, 13)
    )
    return jobs_ok and tsv and c.brackets(ledger, "jobs.result", "fs.write")

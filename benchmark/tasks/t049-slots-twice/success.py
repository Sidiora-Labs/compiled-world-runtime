# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t049-slots-twice: Two job batches around a conversion.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    etl = [j for j in c.jobs_of_kind(state, "etl") if state["jobs"]["jobs"][j]["n"] == 5]
    if len(etl) < 6:
        return False
    stored = all(
        c.kv_get(state, "etl", f"etl-{(i + 1)}") == c.job_result(state, j)
        and c.job_result(state, j) is not None
        for i, j in enumerate(etl[:6])
    )
    clean = all(
        c.file_text(state, f"/csv-clean/c{i:02d}.csv") == f"a,b,{i}\n" for i in range(1, 11)
    )
    return stored and clean and c.brackets(ledger, "jobs.submit", "fs.write")

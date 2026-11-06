# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t057-job-farm: Run a shard farm.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    lines = []
    for i in range(1, 13):
        value = c.kv_get(state, "shards", f"shard-{i}")
        if value is None or not any(
            state["jobs"]["jobs"][j]["n"] == i + 2 and c.job_result(state, j) == value
            for j in c.jobs_of_kind(state, "shard")
        ):
            return False
        lines.append(f"shard-{i}={value}")
    return c.file_lines(state, "/shards/summary.txt") == lines

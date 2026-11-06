# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t060-report-pipeline: Quarterly report pipeline.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    regions = ["north", "south", "east", "west", "central", "coastal", "highland", "island"]
    lines = c.file_lines(state, "/report/summary.md")
    results = []
    for n in [20, 25, 30, 35]:
        value = c.kv_get(state, "stats", f"stats-{n}")
        if value is None or not any(
            state["jobs"]["jobs"][j]["n"] == n and c.job_result(state, j) == value
            for j in c.jobs_of_kind(state, "stats")
        ):
            return False
        results.append(f"stats-{n}={value}")
    for region in regions:
        region_lines = c.file_lines(state, f"/report/{region}.md")
        if region_lines is None or not 1 <= len(region_lines) <= 3:
            return False
    return (
        lines == results + [f"{r}.md" for r in regions]
        and c.is_locked(state, "/report/summary.md")
        and c.posted_ok(state, ledger, "report-q")
        and c.kv_get(state, "receipts", "report-q") == c.receipt_id(state, "report-q")
    )

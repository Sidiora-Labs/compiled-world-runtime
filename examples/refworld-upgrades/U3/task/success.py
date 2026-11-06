# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Post-upgrade task of pair U3: both job results stored and no turn ended in error or partial.

The jobs must be the ones the task names (render with n 5, index with n 40), so that the index job
is still unfinished at the upgrade and its result can only be fetched on Build B, where a
not-done jobs.result answers CONFLICT. At least one ok jobs.result must follow the upgrade record.
"""

import common as c


def success(state, ledger):
    upgrades = [record for record in ledger if record.get("kind") == "upgrade"]
    if len(upgrades) != 1:
        return False
    if any(record.get("status") != "ok" for record in c.turns(ledger)):
        return False
    jobs = (state.get("jobs") or {}).get("jobs") or {}
    for name, n in [("render", 5), ("index", 40)]:
        job_id = c.kv_get(state, "jobs", name)
        value = c.kv_get(state, "results", name)
        job = jobs.get(job_id) if isinstance(job_id, str) else None
        if job is None or job.get("kind") != name or job.get("n") != n:
            return False
        if value is None or value != c.job_result(state, job_id):
            return False
    after = [
        r for r in c.calls(ledger, "jobs.result", status="ok") if r["seq"] > upgrades[0]["seq"]
    ]
    return len(after) >= 1

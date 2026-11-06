# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Post-upgrade task of pair U3: both job results stored and no turn ended in error or partial."""

import common as c


def success(state, ledger):
    if len([record for record in ledger if record.get("kind") == "upgrade"]) != 1:
        return False
    if any(record.get("status") != "ok" for record in c.turns(ledger)):
        return False
    for name in ["render", "index"]:
        job_id = c.kv_get(state, "jobs", name)
        value = c.kv_get(state, "results", name)
        if job_id is None or value is None or value != c.job_result(state, job_id):
            return False
    return True

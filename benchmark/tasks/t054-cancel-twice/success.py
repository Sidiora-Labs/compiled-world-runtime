# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t054-cancel-twice: Cancel two job sets around a publication.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    failed = ["w1", "w3", "z2", "z3"]
    done = ["w2", "z1", "z4"]
    public = all(
        c.file_text(state, f"/minutes-public/m{i:02d}.txt") == f"PUBLIC\nminutes {i}\n"
        for i in range(1, 11)
    )
    return (
        all(c.job_status(state, j) == "failed" for j in failed)
        and all(c.job_status(state, j) == "done" for j in done)
        and public
        and c.brackets(ledger, "jobs.cancel", "fs.write")
    )

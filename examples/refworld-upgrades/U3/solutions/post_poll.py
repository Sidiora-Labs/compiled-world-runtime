# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Turn script, U3 second half on side B after the notice: poll jobs.status first."""

import jobs
import kv

stored = {}
for name in ["render", "index"]:
    if kv.has(ns="results", key=name):
        continue
    job_id = kv.get(ns="jobs", key=name).decode()
    if jobs.status(id=job_id) != "done":
        stored[name] = "pending"
        continue
    value = jobs.result(id=job_id)
    kv.set(ns="results", key=name, value=value.encode())
    stored[name] = value
return stored

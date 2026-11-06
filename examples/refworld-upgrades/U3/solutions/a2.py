# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Turn script, U3 second turn on side A: collect results, NOT_FOUND means pending."""

import jobs
import kv

stored = {}
for name in ["render", "index"]:
    if kv.has(ns="results", key=name):
        continue
    job_id = kv.get(ns="jobs", key=name).decode()
    try:
        value = jobs.result(id=job_id)
    except Exception as failure:
        if failure.code != "NOT_FOUND":
            raise
        stored[name] = "pending"
        continue
    kv.set(ns="results", key=name, value=value.encode())
    stored[name] = value
return stored

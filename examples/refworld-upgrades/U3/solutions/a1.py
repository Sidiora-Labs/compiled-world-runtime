# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Turn script, U3 first turn on side A: submit two jobs and store their ids in kv."""

import jobs
import kv

render = jobs.submit(kind="render", n=5)
index = jobs.submit(kind="index", n=40)
kv.set(ns="jobs", key="render", value=render.encode())
kv.set(ns="jobs", key="index", value=index.encode())
try:
    value = jobs.result(id=render)
except Exception as failure:
    if failure.code != "NOT_FOUND":
        raise
    return {"render": "pending"}
kv.set(ns="results", key="render", value=value.encode())
return {"render": value}

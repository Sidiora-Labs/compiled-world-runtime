<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

## Check, then act

Delete a file only if it exists and is not locked, unlocking it first when needed.

```python
import fs

path = "/reports/old.md"
if fs.exists(path=path):
    if fs.locked(path=path):
        fs.unlock(path=path)
    fs.delete(path=path)
return fs.exists(path=path)
```

## Search, write, publish and record the receipt

```python
import fs, kv, search, publish

hits = search.query(q="golf", limit=3)
lines = [hit["title"] for hit in hits]
fs.write(path="/reports/golf.md", content="\n".join(lines).encode())
fs.lock(path="/reports/golf.md")
receipt = publish.post(key="golf-1", title="Golf report", body="\n".join(lines))
kv.set(ns="receipts", key="golf-1", value=receipt["id"].encode())
return receipt
```

The hits may be about anything; only their shape is guaranteed. If this script stops at
publish.post, the file stays written and locked, and the next turn can repeat publish.post with the
same key and the same content safely.

## Respecting the publishing rate limit

```python
import publish, world

try:
    receipt = publish.post(key="digest-4", title="Digest 4", body="...")
except Exception as failure:
    if failure.code != "RATE_LIMITED":
        raise
    return {"retry_at": world.now() + failure.retry_after}
return receipt
```

## Waiting for a job across turns

```python
import jobs, kv, world

job_id = jobs.submit(kind="render", n=30)
kv.set(ns="jobs", key="render", value=job_id.encode())
return {"job": job_id, "submitted_at": world.now()}
```

On a later turn:

```python
import jobs, kv

job_id = kv.get(ns="jobs", key="render").decode()
if jobs.status(id=job_id) != "done":
    return jobs.status(id=job_id)
return jobs.result(id=job_id)
```

## Staying within the kv quota

```python
import kv

value = b"x" * 900
if kv.used(ns="cache") + len(value) <= 65536:
    kv.set(ns="cache", key="item-1", value=value)
return kv.used(ns="cache")
```

## Asking the owner for a change

```python
import world

return world.propose(text="The receipts namespace keeps reaching its 64 KiB quota.")
```

A proposal is read by a human outside the world. It changes nothing in this Build.

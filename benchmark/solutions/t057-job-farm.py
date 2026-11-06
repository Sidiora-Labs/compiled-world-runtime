# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Reference solution for t057-job-farm (long). Never shown to the model.

Protocol: turns() is a generator. The driver calls next() for the first turn script and
send(envelope) with each result envelope afterwards. Every yielded string is the body of
exactly one fenced python block; the driver wraps it in a ```python fence and submits it
with `cwr instance turn`. The helpers below run on the driver side only.
"""

import textwrap

TASK = "t057-job-farm"
PRIMITIVES = 37  # recorded by the authoring model; CWR-015 recounts on the kernel
TURNS = 4


def script(body, **data):
    """One turn script: literal data assignments followed by the dedented body."""
    head = "".join(f"{name} = {value!r}\n" for name, value in sorted(data.items()))
    return head + textwrap.dedent(body).strip("\n") + "\n"


def wait_until(env, tick):
    """Yield empty turns (zero primitives, tick_per_turn each) until world time reaches tick."""
    while env["world"]["time"] < tick:
        env = yield "pass"
    return env


def ok_times(env, primitive):
    """World times at which ok calls of primitive started in this envelope's trace."""
    return [
        t["world_time"] - 1
        for t in env["trace"]
        if t["primitive"] == primitive and t["status"] == "ok"
    ]


def turns():
    env = yield script("""
        import jobs
        return [jobs.submit(kind="shard", n=i + 2) for i in range(1, 9)]
    """)
    ids = env["returned"]
    env = yield from wait_until(env, max(ok_times(env, "jobs.submit")) + 10)
    env = yield script(
        """
        import jobs, kv
        results = []
        for i, job in enumerate(IDS, 1):
            value = jobs.result(id=job)
            kv.set(ns="shards", key="shard-" + str(i), value=value.encode())
            results.append(value)
        return {"results": results, "ids": [jobs.submit(kind="shard", n=i + 2) for i in range(9, 13)]}
    """,
        IDS=ids,
    )
    first = env["returned"]
    env = yield from wait_until(env, max(ok_times(env, "jobs.submit")) + 14)
    env = yield script(
        """
        import fs, jobs, kv
        results = list(FIRST)
        for i, job in enumerate(IDS, 9):
            value = jobs.result(id=job)
            kv.set(ns="shards", key="shard-" + str(i), value=value.encode())
            results.append(value)
        text = "".join("shard-" + str(i) + "=" + v + "\\n" for i, v in enumerate(results, 1))
        fs.write(path="/shards/summary.txt", content=text.encode())
    """,
        FIRST=first["results"],
        IDS=first["ids"],
    )

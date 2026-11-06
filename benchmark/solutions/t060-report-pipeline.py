# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Reference solution for t060-report-pipeline (long). Never shown to the model.

Protocol: turns() is a generator. The driver calls next() for the first turn script and
send(envelope) with each result envelope afterwards. Every yielded string is the body of
exactly one fenced python block; the driver wraps it in a ```python fence and submits it
with `cwr instance turn`. The helpers below run on the driver side only.
"""

import textwrap

TASK = "t060-report-pipeline"
PRIMITIVES = 32  # recorded by the authoring model; CWR-015 recounts on the kernel
TURNS = 3


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
    env = yield script(
        """
        import fs, jobs, search
        ids = [jobs.submit(kind="stats", n=n) for n in [20, 25, 30, 35]]
        for region in REGIONS:
            titles = [h["title"] for h in search.query(q=region, limit=3)]
            fs.write(path="/report/" + region + ".md", content=("\\n".join(titles) + "\\n").encode())
        return ids
    """,
        REGIONS=["north", "south", "east", "west", "central", "coastal", "highland", "island"],
    )
    ids = env["returned"]
    env = yield from wait_until(env, max(ok_times(env, "jobs.submit")) + 35)
    env = yield script(
        """
        import fs, jobs, kv, publish
        lines = []
        for n, job in zip([20, 25, 30, 35], IDS):
            value = jobs.result(id=job)
            kv.set(ns="stats", key="stats-" + str(n), value=value.encode())
            lines.append("stats-" + str(n) + "=" + value)
        lines += [r + ".md" for r in REGIONS]
        body = "\\n".join(lines) + "\\n"
        fs.write(path="/report/summary.md", content=body.encode())
        fs.lock(path="/report/summary.md")
        receipt = publish.post(key="report-q", title="Quarterly report", body=body)
        kv.set(ns="receipts", key="report-q", value=receipt["id"].encode())
    """,
        IDS=ids,
        REGIONS=["north", "south", "east", "west", "central", "coastal", "highland", "island"],
    )

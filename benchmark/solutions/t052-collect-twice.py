# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Reference solution for t052-collect-twice (long). Never shown to the model.

Protocol: turns() is a generator. The driver calls next() for the first turn script and
send(envelope) with each result envelope afterwards. Every yielded string is the body of
exactly one fenced python block; the driver wraps it in a ```python fence and submits it
with `cwr instance turn`. The helpers below run on the driver side only.
"""

import textwrap

TASK = "t052-collect-twice"
PRIMITIVES = 31  # recorded by the authoring model; CWR-015 recounts on the kernel
TURNS = 13


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
    env = yield "pass"
    env = yield script("""
        import fs, jobs, kv
        kv.set(ns="nightly", key="nightly-a", value=jobs.result(id="nightly-a").encode())
        for path in fs.list(prefix="/csv/"):
            name = path[len("/csv/"):-len(".csv")]
            fs.write(path="/tsv/" + name + ".tsv", content=fs.read(path=path).replace(b",", b"\\t"))
    """)
    env = yield from wait_until(env, 140)
    env = yield script("""
        import jobs, kv
        for job in ["nightly-b", "nightly-c"]:
            kv.set(ns="nightly", key=job, value=jobs.result(id=job).encode())
    """)

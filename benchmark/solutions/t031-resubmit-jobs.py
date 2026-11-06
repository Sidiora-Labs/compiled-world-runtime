# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Reference solution for t031-resubmit-jobs (medium). Never shown to the model.

Protocol: turns() is a generator. The driver calls next() for the first turn script and
send(envelope) with each result envelope afterwards. Every yielded string is the body of
exactly one fenced python block; the driver wraps it in a ```python fence and submits it
with `cwr instance turn`. The helpers below run on the driver side only.
"""

import textwrap

TASK = "t031-resubmit-jobs"
PRIMITIVES = 12  # measured from kernel replay ledgers by tests/cwr/cases/4_2.py
TURNS = 2


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
        for job in ["enc-a", "enc-b", "enc-c"]:
            jobs.cancel(id=job)
        return [jobs.submit(kind="encode", n=4) for _ in range(3)]
    """)
    ids = env["returned"]
    env = yield script(
        """
        import jobs, kv
        for name, job in zip(["enc-a", "enc-b", "enc-c"], IDS):
            kv.set(ns="results", key=name, value=jobs.result(id=job).encode())
    """,
        IDS=ids,
    )

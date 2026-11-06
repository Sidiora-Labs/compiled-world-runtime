# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Reference solution for t024-render-trio (medium). Never shown to the model.

Protocol: turns() is a generator. The driver calls next() for the first turn script and
send(envelope) with each result envelope afterwards. Every yielded string is the body of
exactly one fenced python block; the driver wraps it in a ```python fence and submits it
with `cwr instance turn`. The helpers below run on the driver side only.
"""

import textwrap

TASK = "t024-render-trio"
PRIMITIVES = 9  # measured from kernel replay ledgers by tests/cwr/cases/4_2.py
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
    env = yield script("""
        import jobs
        return [jobs.submit(kind="render", n=n) for n in [5, 12, 20]]
    """)
    ids = env["returned"]
    env = yield "pass"
    env = yield script(
        """
        import jobs, kv
        for n, job in zip([5, 12, 20], IDS):
            kv.set(ns="results", key="render-" + str(n), value=jobs.result(id=job).encode())
    """,
        IDS=ids,
    )

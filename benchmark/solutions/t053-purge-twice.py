# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Reference solution for t053-purge-twice (long). Never shown to the model.

Protocol: turns() is a generator. The driver calls next() for the first turn script and
send(envelope) with each result envelope afterwards. Every yielded string is the body of
exactly one fenced python block; the driver wraps it in a ```python fence and submits it
with `cwr instance turn`. The helpers below run on the driver side only.
"""

import textwrap

TASK = "t053-purge-twice"
PRIMITIVES = 30  # measured from kernel replay ledgers by tests/cwr/cases/4_2.py
TURNS = 1


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
    yield script("""
        import fs, kv
        def purge(names):
            for name in names:
                fs.unlock(path="/queue/" + name)
                fs.delete(path="/queue/" + name)
        purge(["stale-1.job", "stale-2.job"])
        kv_del = kv.del_
        for key in kv.keys(ns="jobs-v1"):
            kv.set(ns="jobs-v2", key=key, value=kv.get(ns="jobs-v1", key=key))
            kv_del(ns="jobs-v1", key=key)
        purge(["stale-3.job", "stale-4.job"])
    """)

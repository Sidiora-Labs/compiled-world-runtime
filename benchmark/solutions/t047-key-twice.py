# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Reference solution for t047-key-twice (long). Never shown to the model.

Protocol: turns() is a generator. The driver calls next() for the first turn script and
send(envelope) with each result envelope afterwards. Every yielded string is the body of
exactly one fenced python block; the driver wraps it in a ```python fence and submits it
with `cwr instance turn`. The helpers below run on the driver side only.
"""

import textwrap

TASK = "t047-key-twice"
PRIMITIVES = 33  # recorded by the authoring model; CWR-015 recounts on the kernel
TURNS = 9


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
        import fs, kv, publish
        for v, key in [("v1", "plan"), ("v2", "plan-v2")]:
            body = fs.read(path="/drafts/plan-" + v + ".txt").decode()
            receipt = publish.post(key=key, title="Plan", body=body)
            kv.set(ns="receipts", key="plan-" + v, value=receipt["id"].encode())
        for path in fs.list(prefix="/minutes/"):
            fs.write(path="/minutes-archive/" + path[len("/minutes/"):], content=fs.read(path=path))
        return [fs.read(path="/drafts/budget-" + v + ".txt").decode() for v in ["v1", "v2"]]
    """)
    budget, last = env["returned"], max(ok_times(env, "publish.post"))
    env = yield from wait_until(env, last + 101)
    env = yield script(
        """
        import kv, publish
        for v, key, body in [("v1", "budget", BUDGET[0]), ("v2", "budget-v2", BUDGET[1])]:
            receipt = publish.post(key=key, title="Budget", body=body)
            kv.set(ns="receipts", key="budget-" + v, value=receipt["id"].encode())
    """,
        BUDGET=budget,
    )

# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Reference solution for t036-retract-post (medium). Never shown to the model.

Protocol: turns() is a generator. The driver calls next() for the first turn script and
send(envelope) with each result envelope afterwards. Every yielded string is the body of
exactly one fenced python block; the driver wraps it in a ```python fence and submits it
with `cwr instance turn`. The helpers below run on the driver side only.
"""

import textwrap

TASK = "t036-retract-post"
PRIMITIVES = 9  # measured from kernel replay ledgers by tests/cwr/cases/4_2.py
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
        import fs, kv, publish
        receipts = {}
        for name in ["a", "b", "c"]:
            body = fs.read(path="/outbox/" + name + ".txt").decode()
            receipts[name] = publish.post(key=name, title=name + ".txt", body=body)["id"]
        publish.delete(post_id=receipts["b"])
        kv.set(ns="receipts", key="a", value=receipts["a"].encode())
        kv.set(ns="receipts", key="c", value=receipts["c"].encode())
    """)

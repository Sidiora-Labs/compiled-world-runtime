# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Reference solution for t027-publish-burst (medium). Never shown to the model.

Protocol: turns() is a generator. The driver calls next() for the first turn script and
send(envelope) with each result envelope afterwards. Every yielded string is the body of
exactly one fenced python block; the driver wraps it in a ```python fence and submits it
with `cwr instance turn`. The helpers below run on the driver side only.
"""

import textwrap

TASK = "t027-publish-burst"
PRIMITIVES = 16  # measured from kernel replay ledgers by tests/cwr/cases/4_2.py
TURNS = 11


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
        paths = fs.list(prefix="/posts/")
        texts = {p: fs.read(path=p).decode() for p in paths}
        for p in paths[:3]:
            key = p[len("/posts/"):-len(".txt")]
            receipt = publish.post(key=key, title=texts[p].splitlines()[0], body=texts[p])
            kv.set(ns="receipts", key=key, value=receipt["id"].encode())
        return {p: texts[p] for p in paths[3:]}
    """)
    rest = env["returned"]
    env = yield from wait_until(env, max(ok_times(env, "publish.post")) + 101)
    env = yield script(
        """
        import kv, publish
        for p, text in sorted(REST.items()):
            key = p[len("/posts/"):-len(".txt")]
            receipt = publish.post(key=key, title=text.splitlines()[0], body=text)
            kv.set(ns="receipts", key=key, value=receipt["id"].encode())
    """,
        REST=rest,
    )

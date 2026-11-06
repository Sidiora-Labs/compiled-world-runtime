# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Reference solution for t021-sports-report (medium). Never shown to the model.

Protocol: turns() is a generator. The driver calls next() for the first turn script and
send(envelope) with each result envelope afterwards. Every yielded string is the body of
exactly one fenced python block; the driver wraps it in a ```python fence and submits it
with `cwr instance turn`. The helpers below run on the driver side only.
"""

import textwrap

TASK = "t021-sports-report"
PRIMITIVES = 8  # measured from kernel replay ledgers by tests/cwr/cases/4_2.py
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
        import fs, kv, search, publish
        golf = [h["title"] for h in search.query(q="golf", limit=3)]
        tennis = [h["title"] for h in search.query(q="tennis", limit=3)]
        fs.write(path="/reports/golf.md", content=("\\n".join(golf) + "\\n").encode())
        fs.write(path="/reports/tennis.md", content=("\\n".join(tennis) + "\\n").encode())
        body = "\\n".join(golf + tennis) + "\\n"
        fs.write(path="/reports/sports.md", content=body.encode())
        fs.lock(path="/reports/sports.md")
        receipt = publish.post(key="sports-1", title="Sports roundup", body=body)
        kv.set(ns="receipts", key="sports-1", value=receipt["id"].encode())
    """)

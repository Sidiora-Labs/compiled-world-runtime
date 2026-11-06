# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Reference solution for t029-topic-digest (medium). Never shown to the model.

Protocol: turns() is a generator. The driver calls next() for the first turn script and
send(envelope) with each result envelope afterwards. Every yielded string is the body of
exactly one fenced python block; the driver wraps it in a ```python fence and submits it
with `cwr instance turn`. The helpers below run on the driver side only.
"""

import textwrap

TASK = "t029-topic-digest"
PRIMITIVES = 15  # measured from kernel replay ledgers by tests/cwr/cases/4_2.py
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
        import fs, search
        topics = [t for t in fs.read(path="/topics.txt").decode().splitlines() if t]
        names = []
        for topic in topics:
            hits = search.query(q=topic, limit=5)
            name = topic.replace(" ", "-") + ".md"
            fs.write(path="/digest/" + name,
                     content=("\\n".join(h["title"] for h in hits) + "\\n").encode())
            names.append(name)
        fs.unlock(path="/digest/index.md")
        fs.write(path="/digest/index.md", content=("\\n".join(names) + "\\n").encode())
    """)

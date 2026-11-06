# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Reference solution for t043-archive-twice (long). Never shown to the model.

Protocol: turns() is a generator. The driver calls next() for the first turn script and
send(envelope) with each result envelope afterwards. Every yielded string is the body of
exactly one fenced python block; the driver wraps it in a ```python fence and submits it
with `cwr instance turn`. The helpers below run on the driver side only.
"""

import textwrap

TASK = "t043-archive-twice"
PRIMITIVES = 30  # measured from kernel replay ledgers by tests/cwr/cases/4_2.py
TURNS = 4


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
        import fs
        fs.delete(path="/archive/q1.txt")
        fs.move(src="/inbox/q1.txt", dst="/archive/q1.txt")
    """)
    yield script("""
        import fs
        for path in fs.list(prefix="/data/"):
            name = path[len("/data/"):]
            fs.write(path="/summary/" + name, content=("# " + name + "\\n").encode()
                     + fs.read(path=path))
    """)
    yield script("""
        import fs
        fs.delete(path="/archive/q2.txt")
        fs.move(src="/inbox/q2.txt", dst="/archive/q2.txt")
    """)
    yield script("""
        import fs
        fs.move(src="/inbox/q3.txt", dst="/archive/q3.txt")
    """)

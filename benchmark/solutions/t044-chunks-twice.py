# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Reference solution for t044-chunks-twice (long). Never shown to the model.

Protocol: turns() is a generator. The driver calls next() for the first turn script and
send(envelope) with each result envelope afterwards. Every yielded string is the body of
exactly one fenced python block; the driver wraps it in a ```python fence and submits it
with `cwr instance turn`. The helpers below run on the driver side only.
"""

import textwrap

TASK = "t044-chunks-twice"
PRIMITIVES = 33  # measured from kernel replay ledgers by tests/cwr/cases/4_2.py
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
    store = """
        import fs, kv
        def store(path):
            text = fs.read(path=path).decode()
            name = path[len("/big/"):]
            for i in range(0, len(text), 1024):
                kv.set(ns="chunks", key=name + "." + str(i // 1024), value=text[i:i + 1024].encode())
    """
    yield script(
        store
        + """
        store("/big/a.txt")
    """
    )
    for half in [0, 1]:
        yield script(
            """
            import fs
            paths = fs.list(prefix="/docs/")
            cut = len(paths) // 2
            for path in (paths[:cut] if half == 0 else paths[cut:]):
                fs.write(path=path, content=fs.read(path=path).lower())
        """,
            half=half,
        )
    yield script(
        store
        + """
        store("/big/b.txt")
    """
    )

# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Reference solution for t049-slots-twice (long). Never shown to the model.

Protocol: turns() is a generator. The driver calls next() for the first turn script and
send(envelope) with each result envelope afterwards. Every yielded string is the body of
exactly one fenced python block; the driver wraps it in a ```python fence and submits it
with `cwr instance turn`. The helpers below run on the driver side only.
"""

import textwrap

TASK = "t049-slots-twice"
PRIMITIVES = 39  # recorded by the authoring model; CWR-015 recounts on the kernel
TURNS = 5


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
        return [jobs.submit(kind="etl", n=5) for _ in range(2)]
    """)
    ids = env["returned"]
    env = yield from wait_until(env, max(ok_times(env, "jobs.submit")) + 5)
    env = yield script(
        """
        import jobs, kv
        for i, job in enumerate(IDS, 1):
            kv.set(ns="etl", key="etl-" + str(i), value=jobs.result(id=job).encode())
        return jobs.submit(kind="etl", n=5)
    """,
        IDS=ids,
    )
    third = env["returned"]
    env = yield from wait_until(env, max(ok_times(env, "jobs.submit")) + 5)
    env = yield script(
        """
        import fs, jobs, kv
        kv.set(ns="etl", key="etl-3", value=jobs.result(id=THIRD).encode())
        for path in fs.list(prefix="/csv/"):
            fs.write(path="/csv-clean/" + path[len("/csv/"):],
                     content=fs.read(path=path).replace(b";", b","))
        return [jobs.submit(kind="etl", n=5) for _ in range(2)]
    """,
        THIRD=third,
    )
    ids = env["returned"]
    env = yield from wait_until(env, max(ok_times(env, "jobs.submit")) + 5)
    env = yield script(
        """
        import jobs, kv
        for i, job in enumerate(IDS, 4):
            kv.set(ns="etl", key="etl-" + str(i), value=jobs.result(id=job).encode())
        return jobs.submit(kind="etl", n=5)
    """,
        IDS=ids,
    )
    last = env["returned"]
    env = yield from wait_until(env, max(ok_times(env, "jobs.submit")) + 5)
    env = yield script(
        """
        import jobs, kv
        kv.set(ns="etl", key="etl-6", value=jobs.result(id=LAST).encode())
    """,
        LAST=last,
    )

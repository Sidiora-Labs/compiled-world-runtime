# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Reference solution for t051-lock-sets-twice (long). Never shown to the model.

Protocol: turns() is a generator. The driver calls next() for the first turn script and
send(envelope) with each result envelope afterwards. Every yielded string is the body of
exactly one fenced python block; the driver wraps it in a ```python fence and submits it
with `cwr instance turn`. The helpers below run on the driver side only.
"""

import textwrap

TASK = "t051-lock-sets-twice"
PRIMITIVES = 41  # measured from kernel replay ledgers by tests/cwr/cases/4_2.py
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
    lock_all = """
        import fs
        def lock_all(prefix):
            for path in fs.list(prefix=prefix):
                if not fs.locked(path=path):
                    fs.lock(path=path)
    """
    yield script(
        lock_all
        + """
        lock_all("/legal/")
    """
    )
    for half in [0, 1]:
        yield script(
            """
            import fs, kv
            paths = fs.list(prefix="/memos/")
            cut = len(paths) // 2
            for path in (paths[:cut] if half == 0 else paths[cut:]):
                kv.set(ns="memos", key=path, value=fs.read(path=path))
        """,
            half=half,
        )
    yield script(
        lock_all
        + """
        lock_all("/finance/")
    """
    )


def near_miss_memos_dropped():
    lock_all = """
        import fs
        def lock_all(prefix):
            for path in fs.list(prefix=prefix):
                if not fs.locked(path=path):
                    fs.lock(path=path)
    """
    yield script(
        lock_all
        + """
        lock_all("/legal/")
    """
    )
    yield script("""
        import fs, kv
        kv.set(ns="scratch", key="started", value=b"1")
        for path in fs.list(prefix="/memos/"):
            fs.delete(path=path)
    """)
    yield script(
        lock_all
        + """
        lock_all("/finance/")
    """
    )


# Near misses: each drops or changes one step; the predicate is false after each.
NEAR_MISSES = {
    "memos-dropped": near_miss_memos_dropped,
}

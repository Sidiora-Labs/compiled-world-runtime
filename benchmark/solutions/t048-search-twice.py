# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Reference solution for t048-search-twice (long). Never shown to the model.

Protocol: turns() is a generator. The driver calls next() for the first turn script and
send(envelope) with each result envelope afterwards. Every yielded string is the body of
exactly one fenced python block; the driver wraps it in a ```python fence and submits it
with `cwr instance turn`. The helpers below run on the driver side only.
"""

import textwrap

TASK = "t048-search-twice"
PRIMITIVES = 57  # measured from kernel replay ledgers by tests/cwr/cases/4_2.py
TURNS = 8


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
        import fs, search
        queries = [q for q in fs.read(path="/q/early.txt").decode().splitlines() if q]
        titles = [search.query(q=q, limit=50)[0]["title"] for q in queries]
        fs.write(path="/out/early.md", content=("\\n".join(titles) + "\\n").encode())
    """)
    last = max(ok_times(env, "search.query"))
    env = yield script("""
        import fs
        for src in fs.list(prefix="/incoming/"):
            dst = "/sorted/" + src[len("/incoming/"):]
            fs.move(src=src, dst=dst)
            fs.lock(path=dst)
        return [q for q in fs.read(path="/q/late.txt").decode().splitlines() if q]
    """)
    late = env["returned"]
    env = yield from wait_until(env, last + 51)
    env = yield script(
        """
        import search
        return [search.query(q=q, limit=50)[0]["title"] for q in QUERIES]
    """,
        QUERIES=late[:10],
    )
    titles = env["returned"]
    env = yield from wait_until(env, max(ok_times(env, "search.query")) + 51)
    env = yield script(
        """
        import fs, search
        titles = TITLES + [search.query(q=q, limit=50)[0]["title"] for q in REST]
        fs.write(path="/out/late.md", content=("\\n".join(titles) + "\\n").encode())
    """,
        TITLES=titles,
        REST=late[10:],
    )


def near_miss_limit_one():
    """The reference with limit=1 everywhere: the titles are right, the pinned limit is not."""
    env = yield script("""
        import fs, search
        queries = [q for q in fs.read(path="/q/early.txt").decode().splitlines() if q]
        titles = [search.query(q=q, limit=1)[0]["title"] for q in queries]
        fs.write(path="/out/early.md", content=("\\n".join(titles) + "\\n").encode())
    """)
    last = max(ok_times(env, "search.query"))
    env = yield script("""
        import fs
        for src in fs.list(prefix="/incoming/"):
            dst = "/sorted/" + src[len("/incoming/"):]
            fs.move(src=src, dst=dst)
            fs.lock(path=dst)
        return [q for q in fs.read(path="/q/late.txt").decode().splitlines() if q]
    """)
    late = env["returned"]
    env = yield from wait_until(env, last + 51)
    env = yield script(
        """
        import search
        return [search.query(q=q, limit=1)[0]["title"] for q in QUERIES]
    """,
        QUERIES=late[:10],
    )
    titles = env["returned"]
    env = yield from wait_until(env, max(ok_times(env, "search.query")) + 51)
    env = yield script(
        """
        import fs, search
        titles = TITLES + [search.query(q=q, limit=1)[0]["title"] for q in REST]
        fs.write(path="/out/late.md", content=("\\n".join(titles) + "\\n").encode())
    """,
        TITLES=titles,
        REST=late[10:],
    )


# Near misses: each drops or changes one step; the predicate is false after each.
NEAR_MISSES = {
    "limit-one": near_miss_limit_one,
}

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
PRIMITIVES = 59  # recorded by the authoring model; CWR-015 recounts on the kernel
TURNS = 14


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
    def batch(env, queries, titles, path, then):
        env = yield script(
            """
            import fs, search
            return [search.query(q=q, limit=1)[0]["title"] for q in QUERIES]
        """,
            QUERIES=queries[:10],
        )
        titles = titles + env["returned"]
        env = yield from wait_until(env, max(ok_times(env, "search.query")) + 51)
        env = yield script(
            """
            import fs, search
            titles = TITLES + [search.query(q=q, limit=1)[0]["title"] for q in REST]
            fs.write(path=PATH, content=("\\n".join(titles) + "\\n").encode())
        """
            + then,
            TITLES=titles,
            REST=queries[10:],
            PATH=path,
        )
        return env

    env = yield script("""
        import fs
        return [q for q in fs.read(path="/q/early.txt").decode().splitlines() if q]
    """)
    then = """
            for src in fs.list(prefix="/incoming/"):
                dst = "/sorted/" + src[len("/incoming/"):]
                fs.move(src=src, dst=dst)
                fs.lock(path=dst)
            return [q for q in fs.read(path="/q/late.txt").decode().splitlines() if q]
    """
    env = yield from batch(env, env["returned"], [], "/out/early.md", then)
    late = env["returned"]
    env = yield from wait_until(env, max(ok_times(env, "search.query")) + 51)
    env = yield from batch(env, late, [], "/out/late.md", "")

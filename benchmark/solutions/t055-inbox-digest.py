# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Reference solution for t055-inbox-digest (long). Never shown to the model.

Protocol: turns() is a generator. The driver calls next() for the first turn script and
send(envelope) with each result envelope afterwards. Every yielded string is the body of
exactly one fenced python block; the driver wraps it in a ```python fence and submits it
with `cwr instance turn`. The helpers below run on the driver side only.
"""

import textwrap

TASK = "t055-inbox-digest"
PRIMITIVES = 67  # measured from kernel replay ledgers by tests/cwr/cases/4_2.py
TURNS = 6


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
    body = """
        import fs, kv, publish, search
        def process(paths, digest_from):
            moved = []
            for path in paths:
                text = fs.read(path=path).decode()
                hits = search.query(q=text.splitlines()[0], limit=1)
                title = hits[0]["title"] if hits else "(none)"
                fs.write(path=path, content=(text + "top: " + title + "\\n").encode())
                name = path[len("/inbox/"):]
                fs.move(src=path, dst="/done/" + name)
                moved.append(name)
                if len(moved) % 5 == 0:
                    n = digest_from + len(moved) // 5
                    receipt = publish.post(key="digest-" + str(n), title="Digest " + str(n),
                                           body="\\n".join(moved[-5:]) + "\\n")
                    kv.set(ns="digests", key="digest-" + str(n), value=receipt["id"].encode())
    """
    env = yield script(
        body
        + """
        paths = fs.list(prefix="/inbox/")
        process(paths[:10], 0)
        return paths[10:]
    """
    )
    rest = env["returned"]
    env = yield from wait_until(env, max(ok_times(env, "search.query")) + 51)
    env = yield script(
        body
        + """
        process(REST, 2)
    """,
        REST=rest,
    )


def near_miss_extra_post():
    yield from turns()
    for _ in range(11):  # past the publishing window, so the extra post is accepted
        yield "pass"
    yield script("""
        import publish
        publish.post(key="digest-4", title="Digest 4", body="none\\n")
    """)


# Near misses: each drops or changes one step; the predicate is false after each.
NEAR_MISSES = {
    "extra-post": near_miss_extra_post,
}

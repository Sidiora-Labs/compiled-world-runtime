# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Reference solution for t046-burst-twice (long). Never shown to the model.

Protocol: turns() is a generator. The driver calls next() for the first turn script and
send(envelope) with each result envelope afterwards. Every yielded string is the body of
exactly one fenced python block; the driver wraps it in a ```python fence and submits it
with `cwr instance turn`. The helpers below run on the driver side only.
"""

import textwrap

TASK = "t046-burst-twice"
PRIMITIVES = 38  # measured from kernel replay ledgers by tests/cwr/cases/4_2.py
TURNS = 29


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
        import fs, publish
        texts = {p: fs.read(path=p).decode() for p in fs.list(prefix="/early/")}
        for p in sorted(texts)[:3]:
            key = p[len("/early/"):-len(".txt")]
            publish.post(key=key, title=texts[p].splitlines()[0][:200], body=texts[p])
        return {p: texts[p] for p in sorted(texts)[3:]}
    """)
    rest, last = env["returned"], max(ok_times(env, "publish.post"))
    env = yield from wait_until(env, last + 101)
    env = yield script(
        """
        import fs, publish
        for p, text in sorted(REST.items()):
            publish.post(key=p[len("/early/"):-len(".txt")], title=text.splitlines()[0][:200],
                         body=text)
        for path in fs.list(prefix="/pages/"):
            fs.write(path=path + ".bak", content=fs.read(path=path))
        return {p: fs.read(path=p).decode() for p in ["/late/l1.txt", "/late/l2.txt",
                                                      "/late/l3.txt", "/late/l4.txt"]}
    """,
        REST=rest,
    )
    late, last = env["returned"], max(ok_times(env, "publish.post"))
    for group in (sorted(late)[:3], sorted(late)[3:]):
        env = yield from wait_until(env, last + 101)
        env = yield script(
            """
            import publish
            for p, text in sorted(GROUP.items()):
                publish.post(key=p[len("/late/"):-len(".txt")], title=text.splitlines()[0][:200],
                             body=text)
        """,
            GROUP={p: late[p] for p in group},
        )
        last = max(ok_times(env, "publish.post"))

# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Reference solution for t059-search-catalogue (long). Never shown to the model.

Protocol: turns() is a generator. The driver calls next() for the first turn script and
send(envelope) with each result envelope afterwards. Every yielded string is the body of
exactly one fenced python block; the driver wraps it in a ```python fence and submits it
with `cwr instance turn`. The helpers below run on the driver side only.
"""

import textwrap

TASK = "t059-search-catalogue"
PRIMITIVES = 42  # recorded by the authoring model; CWR-015 recounts on the kernel
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
        import fs, search
        def catalogue(topics):
            counts = []
            for topic in topics:
                titles = [h["title"] for h in search.query(q=topic, limit=3)]
                fs.write(path="/catalogue/" + topic.replace(" ", "-") + ".md",
                         content=("\\n".join(titles) + "\\n").encode())
                counts.append(topic + ": " + str(len(titles)))
            return counts
    """
    env = yield script(
        body
        + """
        topics = [t for t in fs.read(path="/topics.txt").decode().splitlines() if t]
        return {"counts": catalogue(topics[:10]), "rest": topics[10:]}
    """
    )
    done = env["returned"]
    env = yield from wait_until(env, max(ok_times(env, "search.query")) + 51)
    env = yield script(
        body
        + """
        counts = COUNTS + catalogue(REST)
        fs.write(path="/catalogue/README.md", content=("\\n".join(counts) + "\\n").encode())
    """,
        COUNTS=done["counts"],
        REST=done["rest"],
    )

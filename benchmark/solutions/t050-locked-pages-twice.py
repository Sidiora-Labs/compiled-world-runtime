# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Reference solution for t050-locked-pages-twice (long). Never shown to the model.

Protocol: turns() is a generator. The driver calls next() for the first turn script and
send(envelope) with each result envelope afterwards. Every yielded string is the body of
exactly one fenced python block; the driver wraps it in a ```python fence and submits it
with `cwr instance turn`. The helpers below run on the driver side only.
"""

import textwrap

TASK = "t050-locked-pages-twice"
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
    page = """
        import fs, kv
        def publish_page(name):
            content = fs.read(path="/drafts/" + name + ".html")
            fs.unlock(path="/site/" + name + ".html")
            fs.write(path="/site/" + name + ".html", content=content)
            fs.lock(path="/site/" + name + ".html")
    """
    yield script(
        page
        + """
        publish_page("home")
    """
    )
    yield script("""
        import fs, kv
        for path in fs.list(prefix="/docs/"):
            kv.set(ns="sizes", key=path, value=str(fs.size(path=path)).encode())
    """)
    yield script(
        page
        + """
        publish_page("about")
    """
    )
    yield script(
        page
        + """
        publish_page("contact")
    """
    )

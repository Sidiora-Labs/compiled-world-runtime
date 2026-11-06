# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Reference solution for t058-site-build (long). Never shown to the model.

Protocol: turns() is a generator. The driver calls next() for the first turn script and
send(envelope) with each result envelope afterwards. Every yielded string is the body of
exactly one fenced python block; the driver wraps it in a ```python fence and submits it
with `cwr instance turn`. The helpers below run on the driver side only.
"""

import textwrap

TASK = "t058-site-build"
PRIMITIVES = 36  # recorded by the authoring model; CWR-015 recounts on the kernel
TURNS = 1


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
        import fs, kv, publish
        names = []
        fs.unlock(path="/site/about.html")
        for path in fs.list(prefix="/src/pages/"):
            name = path[len("/src/pages/"):-len(".md")]
            html = b"<html><body>\\n" + fs.read(path=path) + b"</body></html>\\n"
            fs.write(path="/site/" + name + ".html", content=html)
            fs.lock(path="/site/" + name + ".html")
            names.append(name)
        index = "\\n".join(sorted(names)) + "\\n"
        fs.write(path="/site/index.html", content=index.encode())
        fs.lock(path="/site/index.html")
        receipt = publish.post(key="site-1", title="Site rebuilt", body=index)
        kv.set(ns="receipts", key="site-1", value=receipt["id"].encode())
    """)

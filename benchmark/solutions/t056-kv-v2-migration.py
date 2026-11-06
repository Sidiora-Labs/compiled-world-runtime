# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Reference solution for t056-kv-v2-migration (long). Never shown to the model.

Protocol: turns() is a generator. The driver calls next() for the first turn script and
send(envelope) with each result envelope afterwards. Every yielded string is the body of
exactly one fenced python block; the driver wraps it in a ```python fence and submits it
with `cwr instance turn`. The helpers below run on the driver side only.
"""

import textwrap

TASK = "t056-kv-v2-migration"
PRIMITIVES = 47  # recorded by the authoring model; CWR-015 recounts on the kernel
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
        import fs, kv
        kv_del = kv.del_
        migrated = []
        for key in kv.keys(ns="v1"):
            name, email = kv.get(ns="v1", key=key).decode().split("|", 1)
            new_key = key[len("user:"):]
            kv.set(ns="v2", key=new_key, value=(email + "|" + name).encode())
            kv_del(ns="v1", key=key)
            migrated.append(new_key)
        fs.write(path="/migrations/v2.txt", content=("\\n".join(sorted(migrated)) + "\\n").encode())
    """)

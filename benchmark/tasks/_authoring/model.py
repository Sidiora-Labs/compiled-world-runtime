# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Task record type and the preamble shared by every generated reference solution."""

from __future__ import annotations

import textwrap
from dataclasses import dataclass, field


@dataclass
class Task:
    id: str
    bin: str
    title: str
    instruction: str
    initial: dict
    rules: list
    success: object
    turns: object
    reused_rule: str | None = None
    notes: str = ""
    extra: dict = field(default_factory=dict)


# Emitted verbatim after the module constants of every benchmark/solutions/<id>.py, which import
# textwrap first. The driver never sends this code to the kernel; only the strings the turns()
# generator yields are turn scripts. The text is already in ruff format style so that generated
# files pass `ruff format --check` unchanged.
PREAMBLE = '''def script(body, **data):
    """One turn script: literal data assignments followed by the dedented body."""
    head = "".join(f"{name} = {value!r}\\n" for name, value in sorted(data.items()))
    return head + textwrap.dedent(body).strip("\\n") + "\\n"


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
'''

exec(textwrap.dedent(PREAMBLE), globals())

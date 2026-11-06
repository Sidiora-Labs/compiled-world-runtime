# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""CWR SDK world coordinates and the upward channel: world.version, world.build, world.time, world.propose.

Spec: spec/cwr/kernel.md sections 6 and 11, spec/cwr/model-surface.md sections 4 and 5; task 2.5, clause 5.4.

Two kinds of reader use this module.

* A turn script, through the RPC channel of ``cwr._rpc``: ``version()``, ``time()`` and ``propose(text)`` each
  perform exactly one RPC to the Build's ``world.version``, ``world.now`` and ``world.propose`` primitives. The
  kernel validates and records every call like any other primitive; a Build that does not declare one of them
  refuses the call as a closure violation. ``propose`` appends a ``proposal`` ledger record and one line to the
  Instance's outbox, outside the Build and the state store, and returns the proposal id; it changes no
  contract, state, limit or schedule, and the world will not answer it within this Build.
* Anything that holds a turn result envelope (the model surface, a harness reading ledgers and envelopes):
  ``coordinates(envelope)`` returns the envelope's ``world`` object as ``Coordinates(version, build, time,
  turn)``. ``build`` is the hash of the Build the Instance is bound to; it changes only at an upgrade, so the
  first envelope after ``cwr instance upgrade`` carries the new version and build.

The Build hash is not a primitive: the SDK and the manual are inside the hash, so they cannot state it, and the
envelope is the only place a script's result reports it.
"""

from __future__ import annotations

import re
from typing import NamedTuple

__all__ = [
    "PRIMITIVES",
    "Coordinates",
    "CoordinatesError",
    "coordinates",
    "propose",
    "time",
    "version",
]

# The Build primitives behind the RPC helpers, by helper name.
PRIMITIVES = {"version": "world.version", "time": "world.now", "propose": "world.propose"}

_SEMVER = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
_BUILD = re.compile(r"sha256:[0-9a-f]{64}")


class CoordinatesError(ValueError):
    """An envelope whose ``world`` object is not ``{version, build, time, turn}`` as model-surface.md fixes it."""


class Coordinates(NamedTuple):
    """Where a turn happened: the bound Build's version and hash, the world time in ticks and the turn number."""

    version: str
    build: str
    time: int
    turn: int


def _count(value, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise CoordinatesError(f"world.{name} must be a non-negative integer, found {value!r}")
    return value


def coordinates(envelope: dict) -> Coordinates:
    """The ``world`` object of a turn result envelope, checked; raises CoordinatesError otherwise."""
    if not isinstance(envelope, dict) or not isinstance(envelope.get("world"), dict):
        raise CoordinatesError("a turn result envelope carries a world object")
    world = envelope["world"]
    if set(world) != {"version", "build", "time", "turn"}:
        raise CoordinatesError(
            f"world must have exactly version, build, time and turn, found {sorted(world)}"
        )
    if not isinstance(world["version"], str) or not _SEMVER.fullmatch(world["version"]):
        raise CoordinatesError(
            f"world.version must be MAJOR.MINOR.PATCH, found {world['version']!r}"
        )
    if not isinstance(world["build"], str) or not _BUILD.fullmatch(world["build"]):
        raise CoordinatesError(f"world.build must be sha256:<64 hex>, found {world['build']!r}")
    return Coordinates(
        world["version"],
        world["build"],
        _count(world["time"], "time"),
        _count(world["turn"], "turn"),
    )


def version() -> str:
    """Semantic version of the Build this Instance is bound to (one RPC to world.version)."""
    from . import _rpc

    return _rpc.call(PRIMITIVES["version"], {})


def time() -> int:
    """Current world time in ticks (one RPC to world.now); never host time."""
    from . import _rpc

    return _rpc.call(PRIMITIVES["time"], {})


def propose(text: str) -> str:
    """Send text to the human owner (one RPC to world.propose); returns the proposal id and changes nothing."""
    from . import _rpc

    return _rpc.call(PRIMITIVES["propose"], {"text": text})

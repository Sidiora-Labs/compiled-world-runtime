# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""CWR world coordinates read from a turn result envelope: version, Build hash, world time, turn.

Spec: spec/cwr/kernel.md sections 6 and 11, spec/cwr/model-surface.md sections 4 and 5; task 2.5, clause 5.4.

This module is tooling-side: the model surface, a harness or a driver that holds a turn result envelope
calls ``coordinates(envelope)`` and gets ``Coordinates(version, build, time, turn)``. It is not shipped in a
Build and a turn script cannot import it. A turn script reaches the same facts through the Build's generated
``world`` module, which performs one RPC per call: ``world.version()``, ``world.now()`` and
``world.propose(text=...)``. ``PRIMITIVES`` names those generated primitives so tooling can check that a
Build declares them.

``build`` is the hash of the Build the Instance is bound to; it changes only at an upgrade, so the first
envelope after ``cwr instance upgrade`` carries the new version and build. The Build hash is not a primitive:
the SDK and the manual are inside the hash, so they cannot state it, and the envelope is the only place a
script's result reports it.
"""

from __future__ import annotations

import re
from typing import NamedTuple

try:
    from . import _codec
except ImportError:  # loaded as a standalone file by tooling: use the codec beside it
    import importlib.util as _util
    from pathlib import Path as _Path

    _spec = _util.spec_from_file_location("cwr_sdk_codec", _Path(__file__).with_name("_codec.py"))
    _codec = _util.module_from_spec(_spec)
    _spec.loader.exec_module(_codec)

__all__ = [
    "PRIMITIVES",
    "Coordinates",
    "CoordinatesError",
    "coordinates",
]

# The generated Build primitives a turn script calls for these coordinates, by coordinate.
PRIMITIVES = {"version": "world.version", "time": "world.now", "propose": "world.propose"}

_SEMVER = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
_BUILD = re.compile(r"sha256:[0-9a-f]{64}")


class CoordinatesError(ValueError):
    """An envelope that is not a turn result envelope, or whose ``world`` object is malformed."""


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
    """The ``world`` object of a turn result envelope, checked; raises CoordinatesError otherwise.

    The envelope must pass ``cwr._codec.check_turn_envelope`` (exactly the turn keys, a world object with
    exactly ``version``, ``build``, ``time`` and ``turn``); the version is ``MAJOR.MINOR.PATCH``, the build
    is ``sha256:<64 hex>`` and time and turn are non-negative integers.
    """
    try:
        _codec.check_turn_envelope(envelope)
    except _codec.CodecError as e:
        raise CoordinatesError(f"not a turn result envelope: {e}") from None
    world = envelope["world"]
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

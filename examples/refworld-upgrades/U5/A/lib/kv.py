# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Kernel-side implementation of the refworld kv module, flat variant (side A of upgrade pair U5).

Runs inside the kernel process against the kernel state API, never in the sandbox. Each public
function implements exactly one contract in contracts/kv.kvx and takes the kernel state handle
followed by the declared parameters. Python reserves ``del``, so the primitive kv.del is implemented
by ``del_`` (the trailing-underscore rule for Python keywords, applied alike by the compiler and the SDK generator).

State shape (the typed state record the ContractDiff compares):
    {key: bytes}   one flat map; side B of U5 migrates it to {"namespaces": {"default": {key: bytes}}}
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import cwr_state  # noqa: F401  kernel state API; permitted import


KvState = dict[str, bytes]


INITIAL_STATE: KvState = {}


def _kv(state: Any) -> KvState:
    return state.module("kv")


def has(state: Any, key: str) -> bool:
    return key in _kv(state)


def get(state: Any, key: str) -> bytes:
    return bytes(_kv(state)[key])


def keys(state: Any) -> list:
    return sorted(_kv(state))


def used(state: Any) -> int:
    return sum(len(value) for value in _kv(state).values())


# Shadows the builtin on purpose: the function name is the primitive name kv.set.
def set(state: Any, key: str, value: bytes) -> None:
    _kv(state)[key] = bytes(value)
    return None


def del_(state: Any, key: str) -> None:
    del _kv(state)[key]
    return None

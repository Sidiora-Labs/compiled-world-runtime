# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Kernel-side implementation of the refworld kv module (0.3.0: namespaced).

Runs inside the kernel process against the kernel state API, never in the sandbox. Each public
function implements exactly one contract in contracts/kv.kvx and takes the kernel state handle
followed by the declared parameters. Python reserves ``del``, so the primitive kv.del is implemented
by ``del_`` (the trailing-underscore rule for Python keywords, applied alike by the compiler and the SDK generator).

State shape (changed in 0.3.0 by migrations/0_2_0_0_3_0_kv_namespaces.py):
    {"namespaces": {ns: {key: bytes}}}   an empty namespace is never stored
"""

from typing import TYPE_CHECKING, Any, TypedDict

if TYPE_CHECKING:
    import cwr_state  # noqa: F401  kernel state API; permitted import


class KvState(TypedDict):
    namespaces: dict[str, dict[str, bytes]]


INITIAL_STATE: KvState = {"namespaces": {}}


def _namespaces(state: Any) -> dict:
    return state.module("kv")["namespaces"]


def has(state: Any, ns: str, key: str) -> bool:
    return key in _namespaces(state).get(ns, {})


def get(state: Any, ns: str, key: str) -> bytes:
    return bytes(_namespaces(state)[ns][key])


def keys(state: Any, ns: str) -> list:
    return sorted(_namespaces(state).get(ns, {}))


def used(state: Any, ns: str) -> int:
    return sum(len(value) for value in _namespaces(state).get(ns, {}).values())


# Shadows the builtin on purpose: the function name is the primitive name kv.set.
def set(state: Any, ns: str, key: str, value: bytes) -> None:
    _namespaces(state).setdefault(ns, {})[key] = bytes(value)
    return None


def del_(state: Any, ns: str, key: str) -> None:
    namespaces = _namespaces(state)
    del namespaces[ns][key]
    if not namespaces[ns]:
        del namespaces[ns]
    return None

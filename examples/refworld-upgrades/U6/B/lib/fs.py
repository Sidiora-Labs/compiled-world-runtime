# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Kernel-side implementation of the refworld fs module.

Runs inside the kernel process against the kernel state API, never in the sandbox. Each public
function implements exactly one contract in contracts/fs.kvx and takes the kernel state handle
followed by the declared parameters in declared order. Preconditions are evaluated by the kernel
before the function runs, so the bodies assume they hold. The kernel commits the module state only
when the function returns normally.

State shape (the typed state record the ContractDiff compares):
    {"files": {path: bytes}, "locks": [path, ...]}   locks sorted bytewise, every lock names an existing file
"""

from typing import TYPE_CHECKING, Any, TypedDict

if TYPE_CHECKING:
    import cwr_state  # noqa: F401  kernel state API; permitted import


class FsState(TypedDict):
    files: dict[str, bytes]
    locks: list[str]


INITIAL_STATE: FsState = {"files": {}, "locks": []}


def _fs(state: Any) -> FsState:
    return state.module("fs")


def exists(state: Any, path: str) -> bool:
    return path in _fs(state)["files"]


def locked(state: Any, path: str) -> bool:
    return path in _fs(state)["locks"]


def size(state: Any, path: str) -> int:
    return len(_fs(state)["files"][path])


# Shadows the builtin on purpose: the function name is the primitive name fs.list.
def list(state: Any, prefix: str) -> list:
    return sorted(path for path in _fs(state)["files"] if path.startswith(prefix))


def read(state: Any, path: str) -> bytes:
    return bytes(_fs(state)["files"][path])


def write(state: Any, path: str, content: bytes) -> None:
    _fs(state)["files"][path] = bytes(content)
    return None


def delete(state: Any, path: str) -> None:
    del _fs(state)["files"][path]
    return None


def lock(state: Any, path: str) -> None:
    fs = _fs(state)
    fs["locks"] = sorted(set(fs["locks"]) | {path})
    return None


def unlock(state: Any, path: str) -> None:
    fs = _fs(state)
    fs["locks"] = sorted(item for item in fs["locks"] if item != path)
    return None


def move(state: Any, src: str, dst: str) -> None:
    files = _fs(state)["files"]
    files[dst] = files.pop(src)
    return None

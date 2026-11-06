# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Kernel-side implementation of the refworld world module.

Runs inside the kernel process. The world module owns no module state; every primitive reads or
advances kernel-owned facilities through the state handle: the tick clock, the Build identity, the
configured limits and per-turn budgets, the Instance PCG stream, the ledger view and the proposal
outbox. None of them exposes host time, host identity or process identity.

State shape: {} (no module state).
"""

from typing import TYPE_CHECKING, Any, TypedDict

if TYPE_CHECKING:
    import cwr_state  # noqa: F401  kernel state API; permitted import


class WorldState(TypedDict):
    pass


INITIAL_STATE: WorldState = {}


def now(state: Any) -> int:
    return state.world_time()


def version(state: Any) -> str:
    return state.build_version()


def seed(state: Any) -> int:
    return state.seed()


def limits(state: Any) -> dict:
    return dict(state.limits())


def random(state: Any) -> int:
    return state.random()


def history(state: Any, n: int) -> list:
    return [dict(record) for record in state.history(n)]


def propose(state: Any, text: str) -> str:
    return state.propose(text)

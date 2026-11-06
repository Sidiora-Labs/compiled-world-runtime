# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Illustrative migration for refworld 0.1.0 -> 0.2.0: kv becomes namespaced.

A migration is a pure function from the prior Build's instance state to the new Build's state.
It receives and returns plain data; it has no access to the kernel, the clock, the RNG or adapters.
This example has not been executed by any compiler or kernel.
"""


def migrate(state: dict) -> dict:
    kv = state.get("kv", {})
    if set(kv) == {"namespaces"} and isinstance(kv["namespaces"], dict):
        return dict(state)
    migrated = dict(state)
    migrated["kv"] = {"namespaces": {"default": dict(kv)} if kv else {}}
    return migrated

# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""refworld migration 0.1.0 -> 0.2.0: kv becomes namespaced.

Declared by change kv_namespaces (kind state_shape) in CHANGELOG.kvx. A pure function from the
0.1.0 Instance state to the 0.2.0 state: it receives and returns plain data, never mutates its
argument, and has no access to the kernel, the clock, the RNG, adapters or the ledger.

0.1.0 kv state: {key: bytes}                      (one flat map)
0.2.0 kv state: {"namespaces": {ns: {key: bytes}}} (existing keys move to namespace "default")

Idempotent: a state already in the 0.2.0 shape is returned unchanged, so
migrate(migrate(s)) == migrate(s). The 0.2.0 shape is recognised structurally (exactly one key,
"namespaces", whose value is a map); a 0.1.0 flat key that happens to be named "namespaces" holds
bytes, not a map, and is migrated like any other key. Fixtures: fixtures/kv/migration_0_2_0.kvx.
"""


def migrate(state: dict) -> dict:
    kv = state.get("kv", {})
    if set(kv) == {"namespaces"} and isinstance(kv["namespaces"], dict):
        return dict(state)
    migrated = dict(state)
    migrated["kv"] = {"namespaces": {"default": dict(kv)} if kv else {}}
    return migrated

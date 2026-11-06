# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Migration fixture: kv becomes namespaced."""


def migrate(state: dict) -> dict:
    kv = state.get("kv", {})
    if "namespaces" in kv:
        return state
    migrated = dict(state)
    migrated["kv"] = {"namespaces": {"default": dict(kv)}}
    return migrated

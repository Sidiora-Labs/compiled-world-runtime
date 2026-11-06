# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Fixture kv module: state shape {"items": {str: str}}."""


def get(state, key):
    return state.get("kv").get("items", {}).get(key)


def set(state, key, value):
    state.get("kv").setdefault("items", {})[key] = value

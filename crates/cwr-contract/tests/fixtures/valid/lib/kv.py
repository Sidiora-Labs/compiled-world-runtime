# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""kv implementation fixture."""


def has(state, ns, key):
    return key in state["namespaces"].get(ns, {})

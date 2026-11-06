# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Gateway-side implementation fixture for search.query."""


def query(gateway, q, limit):
    return gateway.call("search.query", {"q": q, "limit": limit})

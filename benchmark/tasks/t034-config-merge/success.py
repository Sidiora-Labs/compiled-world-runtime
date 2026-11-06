# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t034-config-merge: Merge configuration.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    merged = [
        ("host", "db.internal"),
        ("port", "6543"),
        ("user", "svc"),
        ("pool", "8"),
        ("timeout", "30"),
        ("tls", "on"),
        ("region", "eu-west"),
    ]
    lines = c.file_lines(state, "/config/app.ini")
    return (
        lines == ["{}={}".format(*kv) for kv in merged]
        and c.is_locked(state, "/config/app.ini")
        and c.kv_keys(state, "config") == sorted(k for k, _ in merged)
        and all(c.kv_get(state, "config", k) == v for k, v in merged)
    )

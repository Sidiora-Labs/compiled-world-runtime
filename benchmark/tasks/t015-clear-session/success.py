# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t015-clear-session: Clear a kv namespace.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    return (
        c.kv_keys(state, "session") == []
        and c.kv_get(state, "users", "u1") == "ada"
        and c.kv_get(state, "users", "u2") == "grace"
    )

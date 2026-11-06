# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t045-quota-twice: Cache entries around a mail merge.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    for i in (1, 2, 3):
        if c.kv_get(state, "cache", f"e{i}") != (f"e{i}") * 500:
            return False
    keys = c.kv_keys(state, "cache")
    letters = all(
        c.file_text(state, f"/letters/l{i:02d}.txt") == f"Dear team,\nletter {i} body\n"
        for i in range(1, 13)
    )
    return (
        c.kv_get(state, "cache", "pinned") == "p" * 800
        and all(k in ("e1", "e2", "e3", "pinned") or k.startswith("stale-") for k in keys)
        and letters
        and c.brackets(ledger, "kv.set", "fs.write")
    )

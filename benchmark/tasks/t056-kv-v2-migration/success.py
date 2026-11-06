# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t056-kv-v2-migration: Migrate user records.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    people = [
        "ada",
        "alan",
        "barbara",
        "claude",
        "dennis",
        "donald",
        "edsger",
        "frances",
        "grace",
        "john",
        "ken",
        "leslie",
        "margaret",
        "niklaus",
        "radia",
    ]
    keys = [f"{i:02d}" for i in range(1, 16)]
    return (
        c.kv_keys(state, "v1") == []
        and c.kv_keys(state, "v2") == keys
        and all(
            c.kv_get(state, "v2", k) == f"{p}@example.org|{p}"
            for k, p in zip(keys, people, strict=False)
        )
        and c.file_lines(state, "/migrations/v2.txt") == keys
    )

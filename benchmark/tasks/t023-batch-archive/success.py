# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t023-batch-archive: Archive an inbox.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    expected = {n: f"inbox {n}, current\n" for n in "abcdef"}
    return c.files_under(state, "/inbox/") == [] and all(
        c.file_text(state, f"/archive/{n}.txt") == t for n, t in expected.items()
    )

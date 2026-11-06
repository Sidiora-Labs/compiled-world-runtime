# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t020-unlock-shared: Unlock a shared area.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    expected = {f"/shared/{n}.txt": f"shared file {n}\n" for n in "abcde"}
    return c.locked_under(state, "/shared/") == [] and all(
        c.file_text(state, p) == t for p, t in expected.items()
    )

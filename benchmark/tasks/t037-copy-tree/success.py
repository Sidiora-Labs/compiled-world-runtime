# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t037-copy-tree: Copy a tree.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    expected = {f"f{i}.txt": f"source file {i}\n" for i in range(1, 9)}
    return (
        all(
            c.file_text(state, "/src/" + n) == t and c.file_text(state, "/dst/" + n) == t
            for n, t in expected.items()
        )
        and len(c.files_under(state, "/src/")) == 8
    )

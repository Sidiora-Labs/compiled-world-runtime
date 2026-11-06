# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t001-copy-readme: Copy a file.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    original = "CWR refworld readme\nVersion 0.2\nKeep this file.\n"
    return (
        c.file_text(state, "/backup/readme.txt") == original
        and c.file_text(state, "/docs/readme.txt") == original
    )

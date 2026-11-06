# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t013-clear-cache: Clear cache files.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    return (
        not any(c.file_exists(state, p) for p in ["/cache/a.tmp", "/cache/b.tmp", "/cache/c.tmp"])
        and c.file_text(state, "/cache/keep.dat") == "persistent\n"
        and c.files_under(state, "/cache/") == ["/cache/keep.dat"]
    )

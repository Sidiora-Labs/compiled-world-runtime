# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t033-size-index: Index file sizes.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    paths = c.files_under(state, "/docs/")
    return (
        len(paths) == 10
        and c.kv_keys(state, "index") == paths
        and all(c.kv_get(state, "index", p) == str(len(c.file_bytes(state, p))) for p in paths)
    )

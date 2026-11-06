# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t050-locked-pages-twice: Publish pages around a size index.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    for page in ["home", "about", "contact"]:
        path = f"/site/{page}.html"
        if c.file_text(state, path) != f"<p>{page}, revised</p>\n" or not c.is_locked(state, path):
            return False
    paths = [f"/docs/d{i:02d}.txt" for i in range(1, 11)]
    return (
        c.kv_keys(state, "sizes") == paths
        and all(c.kv_get(state, "sizes", p) == str(len(c.file_bytes(state, p))) for p in paths)
        and c.brackets(ledger, "fs.write", "kv.set")
    )

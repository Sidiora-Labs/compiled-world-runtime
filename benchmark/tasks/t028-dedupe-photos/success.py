# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t028-dedupe-photos: Remove duplicate photos.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    keep = {
        "/photos/p1.jpg": "A",
        "/photos/p2.jpg": "B",
        "/photos/p4.jpg": "C",
        "/photos/p6.jpg": "D",
        "/photos/p8.jpg": "E",
    }
    return c.files_under(state, "/photos/") == sorted(keep) and all(
        c.file_text(state, p) == "image bytes " + v + "\n" for p, v in keep.items()
    )

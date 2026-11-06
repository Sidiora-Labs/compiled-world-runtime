# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t044-chunks-twice: Store two large files around a rewrite.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    def joined(name):
        parts, i = [], 0
        while c.kv_has(state, "chunks", f"{name}.{i}"):
            parts.append(c.kv_get(state, "chunks", f"{name}.{i}"))
            i += 1
        return "".join(parts)

    a = "".join(f"alpha record {i:04d}\n" for i in range(111))
    b = "".join(f"bravo record {i:04d}\n" for i in range(144))
    docs_ok = all(
        c.file_text(state, f"/docs/doc{i:02d}.txt") == "Document %d About The World\n".lower() % i
        for i in range(1, 13)
    )
    return (
        joined("a.txt") == a
        and joined("b.txt") == b
        and docs_ok
        and c.brackets(ledger, "kv.set", "fs.write")
    )

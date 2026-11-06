# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t051-lock-sets-twice: Lock two folders around a memo import.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    legal = [f"/legal/l{i}.pdf" for i in range(1, 6)]
    finance = [f"/finance/f{i}.xls" for i in range(1, 7)]
    memos = {f"/memos/m{i:02d}.txt": f"memo {i}\n" for i in range(1, 11)}
    return (
        all(c.is_locked(state, p) for p in legal + finance)
        and all(c.file_text(state, p) == text for p, text in memos.items())
        and all(c.kv_get(state, "memos", p) == text for p, text in memos.items())
        and c.brackets(ledger, "fs.lock", "kv.set")
    )

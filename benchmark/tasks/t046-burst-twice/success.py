# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t046-burst-twice: Two publishing bursts around backups.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    def text(group, i):
        head = f"{group.title()} {i}"
        if (group, i) in (("early", 2), ("late", 3)):
            head += ": " + " ".join(f"headline word {k}" for k in range(1, 21))
        return f"{head}\n{group} body {i}\n"

    posts = [("early", "e", i) for i in range(1, 5)] + [("late", "l", i) for i in range(1, 5)]
    for group, letter, i in posts:
        key, body = f"{letter}{i}", text(group, i)
        post = {"key": key, "title": body.split("\n")[0][:200], "body": body}
        if (
            not c.posted_ok(state, ledger, key)
            or len(c.called_with(ledger, "publish.post", post)) < 1
        ):
            return False
    backups = all(
        c.file_text(state, f"/pages/p{i:02d}.txt.bak") == f"page {i}\n" for i in range(1, 11)
    )
    return (
        backups
        and c.rate_limited_beyond_first(ledger, "publish.post") == 0
        and c.brackets(ledger, "publish.post", "fs.write")
    )

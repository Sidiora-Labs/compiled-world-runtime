# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t058-site-build: Build and announce a site.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    pages = [
        "about",
        "blog",
        "careers",
        "contact",
        "docs",
        "faq",
        "home",
        "press",
        "pricing",
        "team",
    ]
    for page in pages:
        path = f"/site/{page}.html"
        expected = f"<html><body>\n# {page}\n</body></html>\n"
        if c.file_text(state, path) != expected or not c.is_locked(state, path):
            return False
    index = "".join(page + "\n" for page in pages)
    post = {"key": "site-1", "title": "Site rebuilt", "body": index}
    return (
        c.file_text(state, "/site/index.html") == index
        and c.is_locked(state, "/site/index.html")
        and c.posted_ok(state, ledger, "site-1")
        and len(c.called_with(ledger, "publish.post", post)) >= 1
        and c.kv_get(state, "receipts", "site-1") == c.receipt_id(state, "site-1")
    )

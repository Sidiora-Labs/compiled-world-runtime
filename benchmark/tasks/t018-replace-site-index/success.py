# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t018-replace-site-index: Replace the site index.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    draft = "<html><body>New front page</body></html>\n"
    return (
        c.file_text(state, "/site/index.html") == draft
        and c.is_locked(state, "/site/index.html")
        and c.file_text(state, "/drafts/index.html") == draft
    )

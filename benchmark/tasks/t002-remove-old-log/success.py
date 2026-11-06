# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t002-remove-old-log: Remove a log file.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    return (
        not c.file_exists(state, "/tmp/old.log")
        and c.file_text(state, "/tmp/keep.log") == "keep me\n"
        and c.file_text(state, "/tmp/session.log") == "session 42\n"
    )

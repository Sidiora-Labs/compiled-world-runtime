# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t012-config-fallback: Back up a config file if present.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    return (
        c.file_lines(state, "/config/STATUS") == ["missing"]
        and not c.file_exists(state, "/config/app.ini.bak")
        and not c.file_exists(state, "/config/app.ini")
    )

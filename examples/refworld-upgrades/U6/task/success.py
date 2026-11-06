# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Post-upgrade task of pair U6 (documentation-only control): both notes hold their final text."""

import common as c


def _after_upgrade(ledger, primitive):
    upgrades = [record["seq"] for record in ledger if record.get("kind") == "upgrade"]
    if len(upgrades) != 1:
        return None
    return [
        record for record in c.calls(ledger, primitive, status="ok") if record["seq"] > upgrades[0]
    ]


def success(state, ledger):
    writes = _after_upgrade(ledger, "fs.write")
    if writes is None or len(writes) < 2:
        return False
    return (
        c.file_text(state, "/notes/today.md") == "final"
        and c.file_text(state, "/notes/status.md") == "done"
    )

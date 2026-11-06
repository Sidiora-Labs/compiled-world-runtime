# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Post-upgrade task of pair U1: every /batch/ file deleted, the second half after the upgrade."""

import common as c


def _after_upgrade(ledger, primitive):
    upgrades = [record["seq"] for record in ledger if record.get("kind") == "upgrade"]
    if len(upgrades) != 1:
        return None
    return [
        record for record in c.calls(ledger, primitive, status="ok") if record["seq"] > upgrades[0]
    ]


def success(state, ledger):
    deletes = _after_upgrade(ledger, "fs.delete")
    if deletes is None or len(deletes) < 3:
        return False
    return c.files_under(state, "/batch/") == [] and c.file_exists(state, "/keep/readme.md")

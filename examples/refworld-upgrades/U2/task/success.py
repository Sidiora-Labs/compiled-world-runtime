# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Post-upgrade task of pair U2: /inbox/ emptied into /done/, the second half after the upgrade."""

import common as c


def _after_upgrade(ledger, primitive):
    upgrades = [record["seq"] for record in ledger if record.get("kind") == "upgrade"]
    if len(upgrades) != 1:
        return None
    return [
        record for record in c.calls(ledger, primitive, status="ok") if record["seq"] > upgrades[0]
    ]


EXPECTED = {
    "/done/1.txt": "one",
    "/done/2.txt": "two",
    "/done/3.txt": "three",
    "/done/4.txt": "four",
}


def success(state, ledger):
    deletes = _after_upgrade(ledger, "fs.delete")
    if deletes is None or len(deletes) < 2:
        return False
    if c.files_under(state, "/inbox/") != []:
        return False
    return all(c.file_text(state, path) == text for path, text in EXPECTED.items())

# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Post-upgrade task of pair U5: three receipts written to files, two after the upgrade."""

import common as c


def _after_upgrade(ledger, primitive):
    upgrades = [record["seq"] for record in ledger if record.get("kind") == "upgrade"]
    if len(upgrades) != 1:
        return None
    return [
        record for record in c.calls(ledger, primitive, status="ok") if record["seq"] > upgrades[0]
    ]


EXPECTED = {"receipt-golf": "p_0001", "receipt-tennis": "p_0002", "receipt-chess": "p_0003"}


def success(state, ledger):
    writes = _after_upgrade(ledger, "fs.write")
    if writes is None or len(writes) < 2:
        return False
    return all(
        c.file_text(state, "/receipts/" + key + ".txt") == value for key, value in EXPECTED.items()
    )

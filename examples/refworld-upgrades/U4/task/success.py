# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Post-upgrade task of pair U4: every /receipts/ file reassembles from kv, the last after the upgrade."""

import common as c


def _after_upgrade(ledger, primitive):
    upgrades = [record["seq"] for record in ledger if record.get("kind") == "upgrade"]
    if len(upgrades) != 1:
        return None
    return [
        record for record in c.calls(ledger, primitive, status="ok") if record["seq"] > upgrades[0]
    ]


def _assembled(state, name):
    whole = c.kv_get(state, "receipts", name)
    if whole is not None:
        return whole
    parts = []
    index = 1
    while c.kv_has(state, "receipts", name + "." + str(index)):
        parts.append(c.kv_get(state, "receipts", name + "." + str(index)))
        index += 1
    return "".join(parts) if parts else None


def success(state, ledger):
    sets = _after_upgrade(ledger, "kv.set")
    if sets is None or len(sets) < 1:
        return False
    paths = c.files_under(state, "/receipts/")
    if len(paths) != 3:
        return False
    return all(
        _assembled(state, path[len("/receipts/") :]) == c.file_text(state, path) for path in paths
    )

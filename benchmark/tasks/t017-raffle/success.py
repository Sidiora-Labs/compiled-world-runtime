# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t017-raffle: Draw a raffle winner.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    entrants = ["ada", "barbara", "edsger", "grace", "john", "ken", "margaret"]
    lines = c.file_lines(state, "/contest/winner.txt")
    random_seq = c.first_seq(ledger, "world.random")
    write_seq = c.last_seq(ledger, "fs.write")
    return (
        lines is not None
        and len(lines) == 1
        and lines[0] in entrants
        and random_seq is not None
        and write_seq is not None
        and random_seq < write_seq
    )

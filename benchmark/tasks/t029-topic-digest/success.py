# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t029-topic-digest: Topic digest.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    topics = ["chess openings", "tide tables", "sourdough", "bike repair", "jazz", "orbits"]
    names = [t.replace(" ", "-") + ".md" for t in topics]
    for topic, name in zip(topics, names, strict=True):
        lines = c.file_lines(state, "/digest/" + name)
        if lines is None or not 1 <= len(lines) <= 5:
            return False
        if len(c.called_with(ledger, "search.query", {"q": topic, "limit": 5})) < 1:
            return False
    return c.file_lines(state, "/digest/index.md") == names

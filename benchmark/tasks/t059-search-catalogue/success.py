# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Success predicate for t059-search-catalogue: Build a search catalogue.

Pure over the final state view and the ledger; imports only benchmark/predicates/common.py.
"""

import common as c


def success(state, ledger):
    topics = [
        "alpine lakes",
        "basalt",
        "cello",
        "dune ecology",
        "estuaries",
        "fjords",
        "geysers",
        "harbour seals",
        "ice cores",
        "jet streams",
        "karst",
        "lava tubes",
        "monsoons",
        "nebulae",
        "oxbow lakes",
        "peat bogs",
        "quasars",
        "reefs",
        "salt flats",
        "tundra",
    ]
    expected = []
    for topic in topics:
        lines = c.file_lines(state, "/catalogue/{}.md".format(topic.replace(" ", "-")))
        if lines is None or not 1 <= len(lines) <= 3:
            return False
        if len(c.called_with(ledger, "search.query", {"q": topic, "limit": 3})) < 1:
            return False
        expected.append(f"{topic}: {len(lines)}")
    return (
        c.file_lines(state, "/catalogue/README.md") == expected
        and c.rate_limited_beyond_first(ledger, "search.query") == 0
    )

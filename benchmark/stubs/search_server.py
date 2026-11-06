#!/usr/bin/env python3
# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""refworld search stub server: backs the search.query adapter (stub://search/v1/query).

Python 3.12 standard library only. Contract: spec/cwr/reference-world.md sections 2.4 and 4,
spec/cwr/kernel.md section 7. Wire protocol and fault schedule: benchmark/stubs/README.md.

    python3 benchmark/stubs/search_server.py --port 0 --seed 7 [--schedule FILE]

Hits are content, not interface: they are generated from (seed, q) so they are stable for one
seed and intentionally differ between seeds. The SearchHit shape is the guaranteed part.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import stubcore
from stubcore import Endpoint, StubCore, WindowLimiter, failure, success

QUERY_LIMIT_MAX = 50  # contract precondition: limit <= 50 else LIMIT
RATE_LIMIT = 10  # queries ...
RATE_WINDOW_TICKS = 50  # ... per 50 ticks per Instance, then RATE_LIMITED
SEARCH_DECLARED = frozenset({"INVALID", "LIMIT", "RATE_LIMITED", "UNAVAILABLE", "TIMEOUT"})
SEARCH_TIMEOUT_MS = 3000

WORDS = (
    "amber",
    "atlas",
    "basin",
    "beacon",
    "birch",
    "canyon",
    "cedar",
    "cipher",
    "comet",
    "coral",
    "delta",
    "drift",
    "ember",
    "fable",
    "fern",
    "fjord",
    "garnet",
    "glacier",
    "harbor",
    "hollow",
    "iris",
    "jade",
    "juniper",
    "kelp",
    "lagoon",
    "lantern",
    "maple",
    "meadow",
    "mesa",
    "nectar",
    "nimbus",
    "oasis",
    "onyx",
    "orchid",
    "pebble",
    "prairie",
    "quartz",
    "quill",
    "raven",
    "reef",
    "ridge",
    "saffron",
    "sierra",
    "spruce",
    "summit",
    "thistle",
    "tundra",
    "umber",
    "valley",
    "vale",
    "willow",
    "wren",
    "yarrow",
    "zephyr",
    "notes",
    "guide",
    "report",
    "index",
    "survey",
    "digest",
    "primer",
    "ledger",
    "compass",
    "review",
)


def generate_hits(seed: int, q: str, limit: int) -> list[dict]:
    """Deterministic SearchHit list for (seed, q), truncated to limit."""
    root = hashlib.sha256(f"cwr-search\x00{seed}\x00{q}".encode()).digest()
    available = 3 + root[0] % 10  # 3..12 hits exist for any query
    hits = []
    for rank in range(1, min(limit, available) + 1):
        digest = hashlib.sha256(f"cwr-search\x00{seed}\x00{q}\x00{rank}".encode()).digest()
        words = [WORDS[b % len(WORDS)] for b in digest[1:4]]
        # Some titles mention the query, some do not: relevance is content, never a rule.
        if digest[0] % 2 == 0:
            words.insert(digest[4] % 4, q[:40])
        title = " ".join(words).title()
        snippet_words = [WORDS[b % len(WORDS)] for b in digest[5:17]]
        hits.append(
            {
                "rank": rank,
                "title": title,
                "url": f"https://search.stub.invalid/doc/{digest.hex()[:16]}",
                "snippet": " ".join(snippet_words).capitalize() + ".",
            }
        )
    return hits


class SearchState:
    def __init__(self) -> None:
        self.limiter = WindowLimiter(RATE_LIMIT, RATE_WINDOW_TICKS)


class SearchCore(StubCore):
    server_name = "search"

    def build_endpoints(self) -> list[Endpoint]:
        return [
            Endpoint("search.query", "/v1/query", SEARCH_DECLARED, SEARCH_TIMEOUT_MS, self.query)
        ]

    def new_instance_state(self) -> SearchState:
        return SearchState()

    def query(self, state: SearchState, params: dict, world_time: int):
        if set(params) != {"q", "limit"}:
            return failure("INVALID", "search.query takes exactly q and limit")
        q, limit = params["q"], params["limit"]
        if not isinstance(q, str) or not isinstance(limit, int) or isinstance(limit, bool):
            return failure("INVALID", "q must be a string and limit an integer")
        if len(q) == 0:
            return failure("INVALID", "q must be nonempty")
        if limit < 0:
            return failure("INVALID", "limit must be non-negative")
        if limit > QUERY_LIMIT_MAX:
            return failure("LIMIT", f"limit must be at most {QUERY_LIMIT_MAX}")
        retry_after = state.limiter.retry_after(world_time)
        if retry_after is not None:
            return failure(
                "RATE_LIMITED",
                f"{RATE_LIMIT} queries per {RATE_WINDOW_TICKS} ticks",
                retry_after=retry_after,
            )
        state.limiter.accept(world_time)
        return success(generate_hits(self.seed, q, limit))


if __name__ == "__main__":
    sys.exit(stubcore.main(SearchCore))

# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Gateway-side implementation of the search adapter for refworld.

Runs inside the kernel's adapter gateway, never in the sandbox. It has no socket of its own: the
only way out is ``gateway.send(payload)``, which the gateway delivers to the endpoint declared in
adapters/search.kvx (``stub://search/v1/query``) in live, record or replay mode, under the
contract's timeout_ms, and never retries.

Contract between this module and the gateway:

* Each public function implements exactly one adapter contract and takes the gateway client
  handle plus the declared parameters, in declared order.
* ``gateway.send(payload)`` returns the decoded body of a response whose service status is in
  ``STATUS_CODES[<adapter>]`` with value ``"ok"``. Every other service status is mapped by the
  gateway through ``STATUS_CODES``; a status that is not listed there, a transport error or a replay
  miss becomes UNAVAILABLE; an exceeded timeout_ms becomes TIMEOUT; a 429 carries
  ``retry_after`` in ticks from ``RETRY_AFTER_FIELD`` of the response body.
* The function only reshapes the body into the declared return shape. A body that cannot be read
  into that shape is handed back unchanged; it then fails the gateway's check of the declared
  return shape, which reports INVALID (a malformed response). The function never raises on a
  service body. Content is never guaranteed.

Wire protocol (benchmark/stubs/README.md): the body of a success is ``{"result": <value>}``; a
failure is ``{"code": <code>, "detail": <text>}`` under the statuses listed in STATUS_CODES.
"""

from typing import Any

# Service status -> envelope outcome, per adapter. Every value is "ok" or a code in the adapter's
# declared failures (INVALID, LIMIT, RATE_LIMITED, UNAVAILABLE, TIMEOUT for search.query).
STATUS_CODES = {
    "search.query": {
        200: "ok",
        400: "INVALID",
        413: "LIMIT",
        429: "RATE_LIMITED",
        503: "UNAVAILABLE",
    },
}

# The stub reports rate-limit back-off already in world ticks (it reads the gateway's world_time
# header), so the conversion ratio from service units to ticks is 1.
RETRY_AFTER_FIELD = "retry_after"


def query(gateway: Any, q: str, limit: int) -> list:
    """search.query(q, limit) -> list[SearchHit]."""
    body = gateway.send({"q": q, "limit": limit})
    items = body.get("result") if isinstance(body, dict) else None
    if not isinstance(items, list):
        return body
    hits = []
    for hit in items[:limit]:
        if not isinstance(hit, dict) or set(hit) != {"rank", "title", "url", "snippet"}:
            return body
        hits.append(
            {
                "rank": hit["rank"],
                "title": hit["title"],
                "url": hit["url"],
                "snippet": hit["snippet"],
            }
        )
    return hits

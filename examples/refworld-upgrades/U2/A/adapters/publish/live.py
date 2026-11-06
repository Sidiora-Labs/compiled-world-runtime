# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Gateway-side implementation of the publish adapters for refworld.

Runs inside the kernel's adapter gateway, never in the sandbox, and has no socket of its own; see
adapters/search/live.py for the gateway contract and the stub wire protocol. publish.post is a
``key`` adapter: the gateway stores the first outcome per (adapter, key, payload hash) before the
request is sent, returns the stored outcome for an identical replay without contacting the service,
and answers DUPLICATE_KEY for a different payload under the same key. The stub keeps its own store
as well and answers 409 for a reused key with a different payload.

The service names the post id ``post_id``; the world's PostReceipt and PostStatus records call it
``id`` (manual/types.md). Only the field name is translated; the value is the service's. A body
that is not of the declared form is handed back unchanged, so the gateway's check of the declared
return shape reports INVALID; nothing here raises on a service body.
"""

from typing import Any

# Service status -> envelope outcome, per adapter. Every value is "ok" or a code in that adapter's
# declared failures in adapters/publish.kvx.
STATUS_CODES = {
    "publish.post": {
        200: "ok",
        400: "INVALID",
        409: "DUPLICATE_KEY",
        429: "RATE_LIMITED",
        503: "UNAVAILABLE",
    },
    "publish.status": {200: "ok", 404: "NOT_FOUND", 503: "UNAVAILABLE"},
    "publish.delete": {200: "ok", 404: "NOT_FOUND", 503: "UNAVAILABLE"},
}

# Rate-limit back-off is reported by the stub in world ticks; the ratio to ticks is 1.
RETRY_AFTER_FIELD = "retry_after"

# For key adapters, the parameter that carries the idempotency key.
IDEMPOTENCY_KEY_PARAM = {"publish.post": "key"}


def _result(body: Any, fields: set) -> Any:
    """The service record inside a success body, or None when the body is not of that form."""
    r = body.get("result") if isinstance(body, dict) else None
    return r if isinstance(r, dict) and set(r) == fields else None


def post(gateway: Any, key: str, title: str, body: str) -> dict:
    """publish.post(key, title, body) -> PostReceipt."""
    answer = gateway.send({"key": key, "title": title, "body": body})
    r = _result(answer, {"post_id", "key", "accepted_at"})
    if r is None:
        return answer  # not a PostReceipt: the gateway's shape check reports INVALID
    return {"id": r["post_id"], "key": r["key"], "accepted_at": r["accepted_at"]}


def status(gateway: Any, post_id: str) -> dict:
    """publish.status(post_id) -> PostStatus."""
    answer = gateway.send({"post_id": post_id})
    r = _result(answer, {"post_id", "key", "state", "accepted_at"})
    if r is None:
        return answer  # not a PostStatus: the gateway's shape check reports INVALID
    return {
        "id": r["post_id"],
        "key": r["key"],
        "state": r["state"],
        "accepted_at": r["accepted_at"],
    }


def delete(gateway: Any, post_id: str) -> None:
    """publish.delete(post_id) -> None."""
    answer = gateway.send({"post_id": post_id})
    if not isinstance(answer, dict) or answer != {"result": None}:
        return answer  # not None: the gateway's shape check reports INVALID
    return None

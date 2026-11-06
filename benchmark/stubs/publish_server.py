#!/usr/bin/env python3
# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""refworld publish stub server: backs publish.post, publish.status and publish.delete
(stub://publish/v1/post, stub://publish/v1/status, stub://publish/v1/delete).

Python 3.12 standard library only. Contract: spec/cwr/reference-world.md sections 2.4 and 4,
spec/cwr/kernel.md section 7. Wire protocol and fault schedule: benchmark/stubs/README.md.

    python3 benchmark/stubs/publish_server.py --port 0 --seed 7 [--schedule FILE]

Per Instance (X-CWR-Instance header) the stub keeps an idempotency store of key -> payload hash
and receipt: an identical replay returns the original receipt, a different payload under the same
key is DUPLICATE_KEY carrying the original receipt. Three accepted posts per 100 ticks, then
RATE_LIMITED with retry_after equal to the ticks until the window reopens.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import stubcore
from stubcore import Endpoint, StubCore, WindowLimiter, canonical_json, failure, success

TITLE_MAX = 200  # contract precondition: len(title) <= 200 else INVALID
RATE_LIMIT = 3  # accepted posts ...
RATE_WINDOW_TICKS = 100  # ... per 100 ticks per Instance
POST_DECLARED = frozenset({"INVALID", "DUPLICATE_KEY", "RATE_LIMITED", "UNAVAILABLE", "TIMEOUT"})
STATUS_DECLARED = frozenset({"NOT_FOUND", "UNAVAILABLE", "TIMEOUT"})
DELETE_DECLARED = frozenset({"NOT_FOUND", "UNAVAILABLE", "TIMEOUT"})


class PublishState:
    def __init__(self) -> None:
        self.limiter = WindowLimiter(RATE_LIMIT, RATE_WINDOW_TICKS)
        self.keys: dict[str, tuple[str, dict]] = {}  # key -> (payload sha256, receipt)
        self.posts: dict[str, dict] = {}  # post_id -> {key, accepted_at, state}


def _strings(params: dict, names: set[str]) -> bool:
    return set(params) == names and all(isinstance(params[name], str) for name in names)


class PublishCore(StubCore):
    server_name = "publish"

    def build_endpoints(self) -> list[Endpoint]:
        return [
            Endpoint("publish.post", "/v1/post", POST_DECLARED, 5000, self.post),
            Endpoint("publish.status", "/v1/status", STATUS_DECLARED, 2000, self.status),
            Endpoint("publish.delete", "/v1/delete", DELETE_DECLARED, 2000, self.delete),
        ]

    def new_instance_state(self) -> PublishState:
        return PublishState()

    def post_id(self, key: str) -> str:
        return (
            "post_"
            + hashlib.sha256(f"cwr-publish\x00{self.seed}\x00{key}".encode()).hexdigest()[:16]
        )

    def post(self, state: PublishState, params: dict, world_time: int):
        if not _strings(params, {"key", "title", "body"}):
            return failure("INVALID", "publish.post takes string key, title and body")
        if len(params["title"]) > TITLE_MAX:
            return failure("INVALID", f"title longer than {TITLE_MAX} characters")
        key = params["key"]
        payload = hashlib.sha256(
            canonical_json({"title": params["title"], "body": params["body"]})
        ).hexdigest()
        if key in state.keys:
            stored_payload, receipt = state.keys[key]
            if stored_payload == payload:
                return success(receipt)  # identical replay: original receipt, no new post
            return failure(
                "DUPLICATE_KEY", "key already used with a different payload", receipt=receipt
            )
        retry_after = state.limiter.retry_after(world_time)
        if retry_after is not None:
            return failure(
                "RATE_LIMITED",
                f"{RATE_LIMIT} accepted posts per {RATE_WINDOW_TICKS} ticks",
                retry_after=retry_after,
            )
        state.limiter.accept(world_time)
        receipt = {"post_id": self.post_id(key), "key": key, "accepted_at": world_time}
        state.keys[key] = (payload, receipt)
        state.posts[receipt["post_id"]] = {
            "key": key,
            "accepted_at": world_time,
            "state": "published",
        }
        return success(receipt)

    def status(self, state: PublishState, params: dict, world_time: int):
        if not _strings(params, {"post_id"}):
            # publish.status does not declare INVALID: a post_id that is not a single string names no post.
            return failure(
                "NOT_FOUND", "publish.status takes a string post_id; no published post has this id"
            )
        record = state.posts.get(params["post_id"])
        if record is None or record["state"] != "published":
            return failure("NOT_FOUND", "no published post with this id")
        return success(
            {
                "post_id": params["post_id"],
                "key": record["key"],
                "state": record["state"],
                "accepted_at": record["accepted_at"],
            }
        )

    def delete(self, state: PublishState, params: dict, world_time: int):
        if not _strings(params, {"post_id"}):
            # publish.delete does not declare INVALID: a post_id that is not a single string names no post.
            return failure(
                "NOT_FOUND", "publish.delete takes a string post_id; no published post has this id"
            )
        record = state.posts.get(params["post_id"])
        if record is None or record["state"] != "published":
            return failure("NOT_FOUND", "no published post with this id")
        record["state"] = "deleted"
        return success(None)


if __name__ == "__main__":
    sys.exit(stubcore.main(PublishCore))

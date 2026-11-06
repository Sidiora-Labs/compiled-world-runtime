<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Stub servers for the reference world's adapters

Two deterministic HTTP servers stand in for the external services behind the reference world's
adapters, `search` and `publish`. With them, every declared adapter failure can be reached without a
third-party service, and adapter traffic can be recorded once into cassettes and then frozen. The
adapter gateway talks to them exactly as it would talk to a real service.

The servers implement [reference-world.md](../../spec/cwr/reference-world.md) sections 2.4 and 4
and the gateway side of [kernel.md](../../spec/cwr/kernel.md) section 7. They are Python 3.12 and
use only the standard library.

## Files

| File | Purpose |
| --- | --- |
| `search_server.py` | Serves `search.query` |
| `publish_server.py` | Serves `publish.post`, `publish.status` and `publish.delete` |
| `stubcore.py` | Code shared by both servers: wire protocol, fault schedule, sliding-window rate limiter, request log and HTTP handling |
| `cassette_tool.py` | Cassette hashing, recording, replay, verification and digests, in the format of [FORMAT.md](../cassettes/FORMAT.md) |
| `record_refworld.py` | Records, replays and digests the reference world's cassette sets through the real adapter gateway |
| `selftest.py` | Starts both servers on ephemeral ports and exercises every endpoint, fault, rate limit, duplicate key, the request log, schedule validation and the cassette tool |

## Run

```sh
python3 benchmark/stubs/search_server.py --port 0 --seed 7
python3 benchmark/stubs/publish_server.py --port 0 --seed 7 --schedule FILE
python3 benchmark/stubs/publish_server.py --check-schedule --schedule FILE
python3 benchmark/stubs/selftest.py
```

`--port 0` picks an ephemeral port, and `--port-file F` writes the bound port to `F`. The default
bind address is `127.0.0.1`. A listening server prints one JSON line to stdout:

```json
{"event": "ready", "server": "search", "host": "127.0.0.1", "port": 39347, "seed": 7, "schedule_sha256": "..."}
```

A refused schedule prints an `"event": "error"` line and exits 2. `--check-schedule` validates a
schedule for one server, prints its digest and exits 0 or 2. `selftest.py` exits 0 when every check
passes.

## Determinism

Given the same seed, schedule and request sequence (body, `X-CWR-Instance` and `X-CWR-World-Time`),
a stub returns byte-identical responses. Responses never depend on host time, host identity, process
identity or thread timing. Bodies are canonical JSON: sorted keys, no spaces, UTF-8.

Host time enters only through the `TIMEOUT` fault, which delays a response but never changes its
content. Search hits derive from `(seed, q)`. A post id derives from `(seed, key)` and not from the
Instance id, so re-recording a run under a new Instance gives the same cassette bytes.

## Wire protocol

`stub://<server>/<path>` in an adapter contract resolves to `http://<host>:<port>/<path>` on the
configured server. Adapter endpoints accept only `POST`.

| Request part | Rule |
| --- | --- |
| Body | The adapter parameters as one JSON object, for example `{"q":"tennis","limit":5}`. Extra keys, missing keys or wrong types get `INVALID`. |
| `X-CWR-Instance` | The Instance id, matching `[A-Za-z0-9._:-]{1,128}`. All stub state (rate windows, idempotency store, posts, request indices) is kept per Instance. |
| `X-CWR-World-Time` | The world time in ticks, a non-negative integer. Rate limits and tick-range faults read this header and never host time. |
| `X-CWR-Adapter` | Optional and informational. |

A request without both `X-CWR-` headers, or whose body is not a JSON object, gets `400 INVALID` when
the adapter declares `INVALID`, and `503 UNAVAILABLE` otherwise. The stub therefore never emits a code
the adapter does not declare. Only a defective gateway sends such a request.

| Response | Meaning |
| --- | --- |
| `200 {"result": <value>}` | Success. `<value>` has the declared return shape. |
| `400 {"code":"INVALID","detail":...}` | Invalid parameters |
| `404 {"code":"NOT_FOUND","detail":...}` | No published post has this id, or the path is unknown |
| `409 {"code":"DUPLICATE_KEY","detail":...,"receipt":{...}}` | The key was used with a different payload. The original receipt is included. |
| `413 {"code":"LIMIT","detail":...}` | `limit` is over 50 (search) |
| `422 {"code":"REJECTED","detail":<reason>}` | Sent only when a schedule injects it |
| `429 {"code":"RATE_LIMITED","detail":...,"retry_after":N}` and header `X-CWR-Retry-After-Ticks: N` | `N` is in world ticks |
| `503 {"code":"UNAVAILABLE","detail":...}` | Sent only when a schedule injects it |

`TIMEOUT` is not a status code. It is a response held past the contract's `timeout_ms`, measured on
the host clock.

### Endpoints

| Adapter | Path | Parameters | Success `result` | Codes the stub can produce | Contract timeout |
| --- | --- | --- | --- | --- | --- |
| `search.query` | `/v1/query` (search server) | `q: str`, `limit: int` | `list[SearchHit]` with at most `limit` items; 3 to 12 hits exist per query | INVALID (`q` empty, `limit` below 0, bad parameters), LIMIT (`limit` above 50), RATE_LIMITED, UNAVAILABLE, TIMEOUT | 3000 ms |
| `publish.post` | `/v1/post` (publish server) | `key: str`, `title: str`, `body: str` | `PostReceipt` | INVALID (title over 200 characters, bad parameters), DUPLICATE_KEY, RATE_LIMITED, UNAVAILABLE, TIMEOUT | 5000 ms |
| `publish.status` | `/v1/status` | `post_id: str` | `PostStatus` | NOT_FOUND, UNAVAILABLE, TIMEOUT | 2000 ms |
| `publish.delete` | `/v1/delete` | `post_id: str` | `null` | NOT_FOUND, UNAVAILABLE, TIMEOUT | 2000 ms |

The wire shapes below are checked by `cassette_tool.SHAPES`. The reference world's adapter code maps
the wire field `post_id` to the `id` field of its `PostReceipt` and `PostStatus` records.

| Wire shape | Fields |
| --- | --- |
| `SearchHit` | `rank: int`, `title: str`, `url: str` (under `search.stub.invalid`), `snippet: str` |
| `PostReceipt` | `post_id: str`, `key: str`, `accepted_at: int` (world tick of acceptance) |
| `PostStatus` | `post_id: str`, `key: str`, `state: str` (`"published"`), `accepted_at: int` |

### Rules held by the stubs

| Rule | Behaviour |
| --- | --- |
| Search rate limit | Ten accepted queries per Instance in any sliding window `(t-50, t]` of world ticks. The next query gets `RATE_LIMITED` with `retry_after` equal to the ticks until the window reopens. Invalid and faulted requests do not count. |
| Publish rate limit | Three accepted posts in any sliding window `(t-100, t]`, then `RATE_LIMITED` with `retry_after` equal to the ticks until the oldest counted post leaves the window. Posts accepted at ticks 0, 2 and 3 give `retry_after = 96` at tick 4, and a post at tick 100 is accepted. |
| Duplicate keys | Each Instance has an idempotency store mapping a key to the sha256 of `{"title","body"}` and the receipt. An identical replay returns the original receipt, creates no post and is never rate limited. A different payload gets `DUPLICATE_KEY` with the original receipt. Deleting a post does not free its key. |
| `publish.post` check order | Parameters, then the duplicate-key store, then the rate limit, then acceptance |
| Status and delete | Naturally idempotent. Deleting a post marks it deleted, and later `status` and `delete` calls on that id get `NOT_FOUND`. |

`publish.status` and `publish.delete` do not declare `INVALID`, so the stub never answers them with
`400`. A parameter object that is not exactly one string `post_id` names no post and gets
`404 NOT_FOUND`. The World Kernel checks parameter types before any adapter call, so only a defective
gateway can send one.

## Fault schedule

A schedule is loaded at startup with `--schedule FILE` (`.json` or `.kvx`) or replaced at runtime with
`PUT /_stub/schedule`. Both servers can share one schedule file. Each server keeps only the entries
for its own adapters.

```json
{"format": "cwr.stub.schedule", "schema_version": 1, "allow_undeclared": false,
 "faults": [
   {"adapter": "publish.post", "index": [2], "fault": "UNAVAILABLE"},
   {"adapter": "publish.post", "ticks": [500, 510], "fault": "TIMEOUT", "delay_ms": 6000},
   {"adapter": "search.*", "index": 0, "fault": "RATE_LIMITED", "retry_after": 25}
 ]}
```

```text
[schedule]
format = "cwr.stub.schedule"
schema_version = 1

[fault.first-status-down]
adapter = "publish.status"
index = [0]
fault = "UNAVAILABLE"
```

| Key | Rule |
| --- | --- |
| `adapter` | A qualified adapter name, or `search.*` or `publish.*`. An unknown adapter is refused. |
| `instance` | Optional. Without it the fault applies to every Instance. |
| `index` | A non-negative integer or a list of them, matched against the request ordinal per (Instance, adapter), counted from 0. Every request counts, faulted ones included. |
| `ticks` | `[lo, hi)` in world ticks from `X-CWR-World-Time`. An entry gives exactly one of `index` or `ticks`. |
| `fault` | One of the faults below. When several entries match, the first in file order wins. |
| `delay_ms` | `TIMEOUT` only. Defaults to the contract's `timeout_ms` plus 1000. |
| `retry_after` | `RATE_LIMITED` only, in ticks. Defaults to 10. |
| `reason` | `REJECTED` only |
| `allow_undeclared` | Top-level, default `false`. See below. |

| Fault | Wire behaviour | Side effect | Code the gateway reports |
| --- | --- | --- | --- |
| `UNAVAILABLE` | `503` | None | UNAVAILABLE |
| `TIMEOUT` | The request is processed, then the response is held for `delay_ms` | Completed. The caller must reconcile, for example by replaying the same key or calling `publish.status`. | TIMEOUT |
| `RATE_LIMITED` | `429` with `retry_after` | None | RATE_LIMITED |
| `REJECTED` | `422` with the reason | None | REJECTED, if declared |
| `MALFORMED` | `200` with a body that breaks the declared shape | None | INVALID |
| `RESET` | Connection reset without a response | None | UNAVAILABLE |

A schedule is refused at load time (exit 2, or HTTP 400 from `PUT /_stub/schedule`) when a fault's
resulting code is not among the targeted adapter's declared failures. For the reference world,
`REJECTED` is refused on every adapter, and `MALFORMED` is refused on `publish.status` and
`publish.delete`. Under default rules the stubs therefore emit only statuses that map to declared
codes. `allow_undeclared: true` exists only to show the gateway's fallback for an undeclared status. A
schedule that sets it is never used to record a cassette.

## Admin routes

Admin routes are not adapter traffic. They are not logged and do not advance request indices.

| Route | Effect |
| --- | --- |
| `GET /_stub/health` | Server name, seed, schedule digest, and the endpoints with their declared failures and timeouts |
| `GET /_stub/log` | Every adapter request received, in order: `n`, `path`, `adapter`, `instance`, `index`, `world_time`, `request_sha256` (sha256 of the body bytes), `status` (`null` for a reset) and `fault`. It shows that the stub saw exactly the requests in the ledger and no retries. |
| `POST /_stub/reset` | Clears all Instance state, request indices and the log, and keeps the schedule |
| `PUT /_stub/schedule` | Replaces the schedule after validation and returns `{"schedule_sha256": ...}` |

## Agreement with the adapter gateway

The Rust adapter gateway is normative. `cassette_tool.normalise` mirrors its response mapping for the
reference world's adapters, and any disagreement between the two is a defect. The request hash and
cassette format are shared with [FORMAT.md](../cassettes/FORMAT.md). A change to either changes the
cassette bytes, and with them the frozen set digest, so both change together and the sets are
re-recorded.

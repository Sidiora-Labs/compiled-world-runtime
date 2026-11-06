<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# refworld stub servers

Status: the self-test below passes. That is not CWR-009 or CWR-014 evidence; those cases run through the kernel's adapter gateway in their registered drivers.

These two servers back the refworld adapters `search` and `publish` ([reference-world.md](../../spec/cwr/reference-world.md) sections 2.4 and 4, [kernel.md](../../spec/cwr/kernel.md) section 7). With them, every declared adapter failure can be reached without a real third-party service, and cassettes can be recorded once and then frozen. They are written for Python 3.12 and use only the standard library.

| File | Purpose |
|---|---|
| `search_server.py` | Serves `search.query`. |
| `publish_server.py` | Serves `publish.post`, `publish.status` and `publish.delete`. |
| `stubcore.py` | Code shared by both servers: wire protocol, fault schedule, sliding-window rate limiter, request log, HTTP plumbing. |
| `cassette_tool.py` | Record/replay helper and reference normaliser. The cassette format is specified in [../cassettes/FORMAT.md](../cassettes/FORMAT.md). |
| `selftest.py` | Starts both servers on ephemeral ports and exercises every endpoint, every fault, the rate limits, duplicate keys, the request log, schedule validation and cassette record/verify/digest/replay. |

```sh
python3 benchmark/stubs/search_server.py  --port 0 --seed 7 [--schedule FILE] [--port-file F] [--verbose]
python3 benchmark/stubs/publish_server.py --port 0 --seed 7 [--schedule FILE] [--port-file F] [--verbose]
python3 benchmark/stubs/publish_server.py --check-schedule --schedule FILE   # validate only, exit 0 or 2
python3 benchmark/stubs/selftest.py [--verbose] [--keep]                     # exit 0 when every check passes
```

When a server is listening it prints one JSON line to stdout: `{"event":"ready","server":"search","host":"127.0.0.1","port":N,"seed":7,"schedule_sha256":"..."}`. If the schedule is refused it prints `{"event":"error",...}` and exits with code 2. The default bind address is `127.0.0.1`.

## Determinism

With the same seed, the same schedule and the same sequence of requests (body, `X-CWR-Instance`, `X-CWR-World-Time`), a stub returns byte-identical responses. Responses never depend on host time, host identity, process identity or thread timing. Bodies use canonical JSON: sorted keys, no spaces, UTF-8. The only place host time enters is the `TIMEOUT` fault, which stalls the response, and it never changes the response content. Search hits are generated from `(seed, q)`. A post id is generated from `(seed, key)` and does not depend on the Instance id, so re-recording a run under a new Instance ULID gives the same cassette bytes.

## Wire protocol

`stub://<server>/<path>` in an adapter contract resolves to `http://<host>:<port>/<path>` for the configured server. All adapter endpoints accept only `POST`.

| Request part | Rule |
|---|---|
| Body | The adapter parameters as one JSON object, e.g. `{"q":"tennis","limit":5}`. Extra or missing keys, or wrong types, get `INVALID`. |
| `X-CWR-Instance` | The Instance id, matching `[A-Za-z0-9._:-]{1,128}`. All stub state (rate windows, idempotency store, posts, request indices) is kept separately for each Instance. |
| `X-CWR-World-Time` | The gateway's world_time in ticks, as a non-negative integer. Rate limits and tick-range faults read this header, never host time. |
| `X-CWR-Adapter` | Optional and informational. |

A request missing either `X-CWR-` header, or whose body is not a JSON object, gets `400` with code `INVALID` when the adapter declares `INVALID`. Otherwise (`publish.status`, `publish.delete`) it gets `503` with code `UNAVAILABLE`, so the stub never emits a code the adapter does not declare. Only a gateway defect can cause this.

| Response | Meaning |
|---|---|
| `200 {"result": <value>}` | Success. `<value>` has the declared return shape. |
| `400 {"code":"INVALID","detail":...}` | Parameters invalid. |
| `404 {"code":"NOT_FOUND","detail":...}` | No published post with this id. |
| `409 {"code":"DUPLICATE_KEY","detail":...,"receipt":{...}}` | Key reused with a different payload. The original receipt is included. |
| `413 {"code":"LIMIT","detail":...}` | `limit` is over 50 (search). |
| `422 {"code":"REJECTED","detail":<reason>}` | Only sent when a schedule injects it (see below). |
| `429 {"code":"RATE_LIMITED","detail":...,"retry_after":N}` plus header `X-CWR-Retry-After-Ticks: N` | `N` is in world ticks, so the contract's tick ratio is 1. |
| `503 {"code":"UNAVAILABLE","detail":...}` | Only sent when a schedule injects it. |

`TIMEOUT` is not a status code. It is a response delayed past the contract's `timeout_ms`, measured on the host clock.

### Endpoints

| Adapter | Path | Params | Success `result` | Codes the stub can produce | Contract timeout |
|---|---|---|---|---|---|
| `search.query` | `/v1/query` (search server) | `q: str`, `limit: int` | `list[SearchHit]`, at most `limit` items (3 to 12 hits exist per query) | INVALID (`q` empty, `limit` < 0, bad params), LIMIT (`limit` > 50), RATE_LIMITED, UNAVAILABLE, TIMEOUT | 3000 ms |
| `publish.post` | `/v1/post` (publish server) | `key: str`, `title: str`, `body: str` | `PostReceipt` | INVALID (title over 200 characters, bad params), DUPLICATE_KEY, RATE_LIMITED, UNAVAILABLE, TIMEOUT | 5000 ms |
| `publish.status` | `/v1/status` | `post_id: str` | `PostStatus` | NOT_FOUND, UNAVAILABLE, TIMEOUT | 2000 ms |
| `publish.delete` | `/v1/delete` | `post_id: str` | `null` (`None`) | NOT_FOUND, UNAVAILABLE, TIMEOUT | 2000 ms |

The refworld `manual/types.md` must state the same record shapes as the table below. `cassette_tool.SHAPES` checks them.

| Shape | Fields |
|---|---|
| `SearchHit` | `rank: int`, `title: str`, `url: str` (under `search.stub.invalid`), `snippet: str` |
| `PostReceipt` | `post_id: str`, `key: str`, `accepted_at: int` (world tick of acceptance) |
| `PostStatus` | `post_id: str`, `key: str`, `state: str` (`"published"`), `accepted_at: int` |

### Rules held by the stubs

| Rule | Behaviour |
|---|---|
| search rate limit | Ten accepted queries in any sliding window `(t-50, t]` of world ticks for each Instance. The next query gets `RATE_LIMITED` with `retry_after` equal to the ticks until the window reopens. Invalid requests and faulted requests do not count. |
| publish rate limit | Three accepted posts in any sliding window `(t-100, t]`, then `RATE_LIMITED` with `retry_after` equal to the ticks until the oldest counted post leaves the window. Example: posts accepted at ticks 0, 2 and 3 give `retry_after = 96` at tick 4, and a post at tick 100 is accepted. |
| Duplicate keys | The stub keeps an idempotency store for each Instance, mapping key to sha256 of `{"title","body"}` and the receipt. An identical replay returns the original receipt, creates no new post and is never rate limited. A different payload gets `DUPLICATE_KEY` with the original receipt. Deleting a post does not free its key. |
| Check order for `publish.post` | Parameters, then the duplicate-key store, then the rate limit, then acceptance. |
| status/delete | Natural idempotency. Deleting a published post marks it deleted. Later `status` and `delete` calls on that id get `NOT_FOUND`. |
| Unknown path | `404` with `NOT_FOUND`. |

`publish.status` and `publish.delete` do not declare `INVALID`, so the stub never answers them with `400`. A parameter object that is not exactly one string `post_id` names no post and gets `404 NOT_FOUND`. Only a gateway that skipped parameter validation can send one, because the kernel checks parameter types at step 1.

## Fault schedule

A schedule is given at startup with `--schedule FILE` (`.json` or `.kvx`) or replaced at runtime with `PUT /_stub/schedule`. Both servers can share one schedule file: each server keeps only the entries for its own adapters.

```json
{"format": "cwr.stub.schedule", "schema_version": 1, "allow_undeclared": false,
 "faults": [
   {"adapter": "publish.post", "instance": "01J...", "index": [2], "fault": "UNAVAILABLE"},
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
|---|---|
| `adapter` | A qualified adapter name, or `search.*` / `publish.*`. An unknown adapter is refused. |
| `instance` | Optional. When omitted, the fault applies to every Instance. |
| `index` | A non-negative integer or a list of them. Matched against the request ordinal for each (Instance, adapter), counted from 0. Every request counts, including faulted ones. |
| `ticks` | `[lo, hi)` in world ticks from `X-CWR-World-Time`. Give exactly one of `index` or `ticks`. |
| `fault` | One of the faults in the table below. When several entries match, the first in file order wins. |
| `delay_ms` | `TIMEOUT` only. Defaults to the contract's `timeout_ms + 1000`. |
| `retry_after` | `RATE_LIMITED` only, in ticks. Defaults to 10. |
| `reason` | `REJECTED` only. |
| `allow_undeclared` | Top-level setting, default `false`. See below. |

| Fault | Wire behaviour | Side effect | Gateway code expected |
|---|---|---|---|
| `UNAVAILABLE` | `503` | none | UNAVAILABLE |
| `TIMEOUT` | The request is processed normally, then the response is held for `delay_ms` | **completed**: the model must reconcile, e.g. by replaying the same key or calling `publish.status` | TIMEOUT |
| `RATE_LIMITED` | `429` with `retry_after` | none | RATE_LIMITED |
| `REJECTED` | `422` with the reason | none | REJECTED, if declared |
| `MALFORMED` | `200` with a body that breaks the declared shape | none | INVALID |
| `RESET` | Connection reset without a response | none | UNAVAILABLE (transport error) |

**Declared codes only.** A fault is refused at load time (exit code 2, or HTTP 400 from `PUT /_stub/schedule`) when its expected gateway code is not in the targeted adapter's declared `failures`. For refworld this means `REJECTED` is refused on every adapter, and `MALFORMED` is refused on `publish.status` and `publish.delete`. Under the default schedule rules the stubs therefore emit only statuses that map to declared codes (reference-world.md section 4). `allow_undeclared: true` exists only so that a gateway driver can show an undeclared status falling back to `undeclared_status`. A schedule that sets it must never be used for a cassette recording.

## Admin routes

Admin routes are not adapter traffic. They are not logged and do not advance request indices.

| Route | Effect |
|---|---|
| `GET /_stub/health` | Server name, seed, schedule digest, endpoints with declared failures and timeouts. |
| `GET /_stub/log` | Every adapter request received, in order: `n`, `path`, `adapter`, `instance`, `index`, `world_time`, `request_sha256` (sha256 of the body bytes), `status` (`null` for a reset), `fault`. CWR-009 and CWR-010 use it to show that the stub saw exactly the requests in the ledger and no retries. |
| `POST /_stub/reset` | Clears all Instance state, request indices and the log. The schedule is kept. |
| `PUT /_stub/schedule` | Replaces the schedule after validation. Returns `{"schedule_sha256": ...}`. |

## Integration contract

- **Adapter gateway.** Send the `X-CWR-Instance` and `X-CWR-World-Time` headers on every call, with the body set to the codec-serialised parameters. Resolve `stub://` through a configured server map. Map responses with the table above, which `cassette_tool.normalise` mirrors as a reference. Use the canonical request hash and cassette format in [FORMAT.md](../cassettes/FORMAT.md), or change both documents together. Enforce `timeout_ms` as a total deadline. `call_stub` applies Python's per-operation socket timeout, which is enough for the stub's single stall. The CWR-009 driver should use `PUT /_stub/schedule` and `GET /_stub/log`.
- **refworld.** Make the adapter contracts and `manual/types.md` agree with the endpoint and shape tables here. Keep `publish.delete`'s contract identical to `publish.status` apart from its name and endpoint. Generate the adapter fixtures (one response per declared failure plus one success) as cassette entries. Record the suite cassette set with a schedule that has `allow_undeclared` false, and freeze `cassette_tool.py digest benchmark/cassettes/<set>` in `benchmark/suite.kvx`. The CWR-014 driver can run `selftest.py` as a preflight, but its own assertions must go through the kernel gateway.
- **Recovery.** Use `POST /_stub/reset` between interleavings and assert on `GET /_stub/log` that the stub saw one request.
- **Contract mismatch.** If the gateway's chosen hash or codec differs from FORMAT.md, update `cassette_tool.request_hash` and FORMAT.md together and re-record. Any change to the cassette bytes changes the set digest.

<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Cassette format (`cwr.cassette`, schema version 1)

Status: two refworld sets are recorded and frozen (task 4.1): `refworld-adapters/` (scripted scenarios over every adapter primitive and declared failure) and `suite/` (every reference solution of the task suite, seeds 1 to 10). Their digests are frozen in `refworld.kvx`, and the suite digest also in `../suite.kvx`. `../stubs/record_refworld.py` writes them. Each `seed-<n>.jsonl` has a `seed-<n>.expect.json` next to it, holding the turn scripts, result envelopes and ledger that record mode produced and that replay mode must reproduce. Expect files are not cassettes and are outside the set digest.

A cassette records the adapter traffic of one Instance so that the run can be replayed with no
network access and the same outcomes. The adapter gateway in the World Kernel reads and writes this
format in record and replay mode, and [cassette_tool.py](../stubs/cassette_tool.py) implements it in
Python. A change to the format changes this document, the gateway and the tool together.

The format implements [kernel.md](../../spec/cwr/kernel.md) section 7 and
[reference-world.md](../../spec/cwr/reference-world.md) section 4.

## 1. What a cassette holds

A cassette holds the adapter traffic of one Instance, in call order. It stores the raw service
response (HTTP status, kept headers and body text, or a timeout or transport error), not the
normalised outcome. In replay mode the gateway runs the same normalisation it runs live, so both
modes produce identical outcomes. Contract tests in the World Compiler feed fixture responses through
the same normaliser.

The gateway's idempotency-key store answers some calls without contacting the service. Those calls
are not service traffic, and they never appear in a cassette.

No host time, duration, Instance id or host identity is stored anywhere in a cassette.

## 2. File layout

A cassette is a UTF-8 JSON Lines file that ends with a newline. Every line is the canonical JSON of
its value: sorted keys, separators `,` and `:` with no spaces, non-ASCII characters unescaped, and no
NaN or Infinity. Equal traffic therefore always produces equal bytes. In Python:

```python
json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
```

Line 1 is the header:

```json
{"format":"cwr.cassette","label":"t003-tennis-note/seed-1","request_hash":"sha256-cwr-canonical-json-v1","schema_version":1}
```

| Key | Rule |
| --- | --- |
| `format` | `"cwr.cassette"` |
| `schema_version` | `1` |
| `request_hash` | `"sha256-cwr-canonical-json-v1"`, the scheme in section 3 |
| `label` | A string or `null`, by convention `<task>/seed-<n>`. It never contains an Instance id, a host name or a timestamp, which would make re-recording non-reproducible. |

Every following line is one entry:

```json
{"adapter":"search.query","params":{"limit":3,"q":"tennis"},"request_hash":"sha256:d9043de80c423ff0828437ab405f207540573cb4f19f764ff622e1ad461b934c","response":{"body":"{\"result\":[...]}","headers":{"content-type":"application/json"},"kind":"http","status":200},"seq":0,"world_time":0}
```

| Key | Rule |
| --- | --- |
| `adapter` | The qualified adapter name, `module.name` |
| `params` | The parameters object exactly as sent |
| `request_hash` | `sha256:<64 hex>` over the adapter and parameters (section 3). It matches `params`. |
| `seq` | The ordinal of this `(adapter, request_hash)` pair within the cassette, from 0 with no gaps. A request made twice replays its first and second responses in that order. |
| `world_time` | The world tick sent in `X-CWR-World-Time` at record time, or `null`. It is informational, not part of the key, and never decreases within a cassette. |
| `response` | One of the three forms below |

| `response.kind` | Other keys | Result on replay |
| --- | --- | --- |
| `http` | `status` (integer), `headers` (only `content-type` and `x-cwr-retry-after-ticks`, when present), `body` (the exact body text) | Normalised by the table in kernel.md section 7 |
| `timeout` | None | `TIMEOUT`, returned at once. The recorded stall is not reproduced, and no duration is stored. |
| `transport_error` | `error`, the exception class name, for example `ConnectionResetError` | `UNAVAILABLE` |

## 3. Canonical request hash

```text
request_hash = "sha256:" + hex(sha256(canonical_json({"adapter": <adapter>, "params": <params>})))
```

The specification computes the hash over the adapter name and the parameters serialised through the
SDK codec with sorted keys. This scheme pins that serialisation as canonical JSON, which the SDK codec
produces for every value without bytes. The ledger's `params_hash` is a separate `blake3:` field with
its own purpose and is not reused here.

Key order in the input does not change the hash:

```sh
python3 benchmark/stubs/cassette_tool.py hash search.query '{"q":"tennis","limit":3}'
```

## 4. Replay rules

1. Compute `request_hash` for the call, take the next `seq` for `(adapter, request_hash)` in this
   Instance, and look up `(adapter, request_hash, seq)`.
2. On a hit, normalise the stored response.
3. On a miss, return `UNAVAILABLE`. No socket is opened.
4. Entries are never consumed out of order, and the gateway never retries.

The Python tool enforces rule 3 with `forbid_network()`, which makes any connection attempt raise
`NetworkForbidden`. The `replay` subcommand installs it before serving.

## 5. Cassette sets and digest

A cassette set is a directory, normally `benchmark/cassettes/<set-name>/<task>/seed-<n>.jsonl`. Its
digest is:

```text
digest = "sha256:" + hex(sha256( concat over every *.jsonl file, sorted by relative POSIX path, of
                                  "<relpath>\t<sha256 hex of file bytes>\n" ))
```

The suite's set digest is frozen in `benchmark/suite.kvx`. Re-recording produces a new digest and
invalidates every result recorded under the old one. A recording always uses a stub schedule with
`allow_undeclared` set to false, as described in [the stubs README](../stubs/README.md).

## 6. Tool

| Command | Effect | Exit |
| --- | --- | --- |
| `cassette_tool.py hash ADAPTER PARAMS_JSON` | Prints the canonical request hash | 0 |
| `cassette_tool.py record --stub search=URL --stub publish=URL --instance ID --requests R.jsonl --out C.jsonl [--label L] [--timeout-ms ADAPTER=MS]` | Sends each request (`{"adapter","params","world_time"}` per line) to the stubs once, with no retries, and writes the cassette | 0, or 2 on a usage or I/O error |
| `cassette_tool.py replay --cassette C.jsonl --requests R.jsonl` | Serves each request from the cassette with the network disabled, printing one canonical JSON line per request: `adapter`, `request_hash`, `seq`, `hit` and `outcome` | 0, or 1 if any request missed |
| `cassette_tool.py verify C.jsonl...` | Checks the header, entry keys, canonical encoding, hash and parameter agreement, contiguous `seq`, non-decreasing `world_time` and response forms | 0, or 1 on any problem |
| `cassette_tool.py digest PATH...` | Prints the set digest | 0 |

The library API is `Cassette`, `Recorder`, `Replayer`, `normalise`, `request_hash`, `set_digest`,
`verify_bytes` and `forbid_network`. `normalise` mirrors the gateway's response table for the
reference world's adapters. The Rust gateway is normative, and a disagreement between the two is a
defect.

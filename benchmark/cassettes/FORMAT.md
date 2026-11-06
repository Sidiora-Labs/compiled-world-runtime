# Cassette format (`cwr.cassette`, schema version 1)

Status: proposed format, implemented by [../stubs/cassette_tool.py](../stubs/cassette_tool.py). No cassette set has been recorded or frozen yet. The kernel gateway (task 2.3) must read and write exactly this format, or change this document and the tool together.

Normative sources: [kernel.md](../../spec/cwr/kernel.md) section 7 (live, record and replay modes; key = adapter, canonical request hash, sequence; a miss is `UNAVAILABLE` and opens no socket), [reference-world.md](../../spec/cwr/reference-world.md) section 4 (recorded once, digest frozen in `benchmark/suite.kvx`, replay only in drivers and campaigns), and spec.kvx clauses 7.5 and 11.5.

## 1. What a cassette is

A cassette holds the adapter traffic of **one Instance**, in call order. It stores the **raw service response** (HTTP status, kept headers and body text, or a timeout or transport error), not the normalised outcome. In replay mode the gateway runs the same normalisation it runs live, so both modes produce identical outcomes, and compiler contract tests can feed fixture responses through the real normaliser ([compiler.md](../../spec/cwr/compiler.md), contract tests).

The gateway's `key`-idempotency store answers some calls without contacting the service. Those calls are not service traffic, so they never appear in a cassette.

## 2. File layout

A cassette is a UTF-8 JSONL file ending in a newline. Every line is the **canonical JSON** of its value: keys sorted, separators `,` and `:` with no spaces, non-ASCII characters left unescaped, no NaN or Infinity. Python reference: `json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)`. A line in any other encoding fails `verify`. Because of this rule, equal traffic always produces equal bytes.

Line 1 is the header:

```json
{"format":"cwr.cassette","label":"t003-tennis-note/seed-1","request_hash":"sha256-cwr-canonical-json-v1","schema_version":1}
```

| Key | Rule |
|---|---|
| `format` | `"cwr.cassette"` |
| `schema_version` | `1` |
| `request_hash` | `"sha256-cwr-canonical-json-v1"`, the hash scheme in section 3 |
| `label` | String or `null`. By convention `<task>/seed-<n>`. It must not contain an Instance ULID, a host name or a timestamp, because those would make re-recording non-reproducible. |

Every following line is one entry:

```json
{"adapter":"search.query","params":{"limit":3,"q":"tennis"},"request_hash":"sha256:9f...","response":{"body":"{\"result\":[...]}","headers":{"content-type":"application/json"},"kind":"http","status":200},"seq":0,"world_time":0}
```

| Key | Rule |
|---|---|
| `adapter` | Qualified adapter name `module.name`. |
| `params` | The parameters object exactly as sent. |
| `request_hash` | `sha256:<64 hex>` over the adapter and params (section 3). It must match `params`. |
| `seq` | The ordinal of this `(adapter, request_hash)` pair within the cassette, starting at 0 with no gaps. If the same request is made twice, its first and second responses replay in that order. |
| `world_time` | The world tick sent in `X-CWR-World-Time` at record time, or `null`. Informational only and **not part of the key**. Must not decrease within a cassette. Useful when diagnosing a replay divergence. |
| `response` | One of the three forms below. |

| `response.kind` | Other keys | Gateway result on replay |
|---|---|---|
| `http` | `status` (int), `headers` (object containing only `content-type` and `x-cwr-retry-after-ticks`, if present), `body` (string, exact body text) | Normalised by the kernel.md section 7 table |
| `timeout` | none | `TIMEOUT`, returned immediately. The recorded stall is not reproduced, and no host duration is stored. |
| `transport_error` | `error` (exception class name, e.g. `ConnectionResetError`) | `UNAVAILABLE` |

No host time, duration, Instance id or host identity is stored anywhere in a cassette.

## 3. Canonical request hash

```text
request_hash = "sha256:" + hex(sha256(canonical_json({"adapter": <adapter>, "params": <params>})))
```

kernel.md says the hash is "computed over the adapter name and the parameters serialised through the SDK codec with sorted keys". This scheme pins that as canonical JSON. If the SDK codec (`sdk/python/cwr/_codec.py`, task 1.3) chooses another serialisation, change the `request_hash` header scheme name, `cassette_tool.request_hash` and this section together, then re-record. Note that ledger `params_hash` is `blake3:`, a separate field with its own purpose, and is not reused here.

`cassette_tool.py hash search.query '{"q":"tennis","limit":3}'` prints the hash. Key order in the input makes no difference.

## 4. Replay rules

1. Compute `request_hash` for the call, take the next `seq` for `(adapter, request_hash)` in this Instance, and look up `(adapter, request_hash, seq)`.
2. On a hit, normalise the stored response.
3. On a miss, return `UNAVAILABLE` (detail: cassette miss). No socket is opened. `cassette_tool.forbid_network()` makes any connection attempt raise `NetworkForbidden`, and the `replay` subcommand installs it before serving.
4. Entries are never consumed out of order, and the gateway never retries.

## 5. Cassette sets and digest

A cassette set is a directory, normally `benchmark/cassettes/<set-name>/<task>/seed-<n>.jsonl`. Its digest is:

```text
digest = "sha256:" + hex(sha256( concat over every *.jsonl file, sorted by relative POSIX path, of
                                  "<relpath>\t<sha256 hex of file bytes>\n" ))
```

`cassette_tool.py digest benchmark/cassettes/<set-name>` prints it. The digest of the recorded suite set is frozen in `benchmark/suite.kvx`. Re-recording produces a new digest, and that invalidates every result recorded under the old one (reference-world.md section 4). Recording must use a stub schedule with `allow_undeclared` false (see [../stubs/README.md](../stubs/README.md)).

## 6. Tool

| Command | Effect | Exit |
|---|---|---|
| `cassette_tool.py hash ADAPTER PARAMS_JSON` | Print the canonical request hash | 0 |
| `cassette_tool.py record --stub search=URL --stub publish=URL --instance ID --requests R.jsonl --out C.jsonl [--label L] [--timeout-ms ADAPTER=MS]` | Send each request (`{"adapter","params","world_time"}` per line) to the stubs once, with no retries, and write the cassette | 0; 2 on usage or I/O error |
| `cassette_tool.py replay --cassette C.jsonl --requests R.jsonl` | Serve each request from the cassette with the network disabled. Prints one canonical JSON line per request: `adapter`, `request_hash`, `seq`, `hit`, `outcome` | 0; 1 if any request missed |
| `cassette_tool.py verify C.jsonl...` | Check the header, entry keys, canonical encoding, hash and params agreement, contiguous `seq`, non-decreasing `world_time` and response forms | 0; 1 on any problem |
| `cassette_tool.py digest PATH...` | Print the set digest | 0 |

The library API (`Cassette`, `Recorder`, `Replayer`, `normalise`, `request_hash`, `set_digest`, `verify_bytes`, `forbid_network`) is what `benchmark/stubs/selftest.py` uses, and drivers can import it the same way. `normalise` is a reference mirror of the gateway table for the refworld adapters (`REFWORLD_ADAPTERS`). The Rust gateway is normative, and a disagreement between them is a defect to resolve.

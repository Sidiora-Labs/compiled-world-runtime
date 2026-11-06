<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# examples: the refworld World Source

`refworld/` is the reference World Source (requirement 11, [reference-world.md](../spec/cwr/reference-world.md)). `refworld-upgrades/` holds the upgrade pairs U1 to U6 derived from it (see its own README).

This README lives here, outside `refworld/`, on purpose. Clause 3.1 makes every file outside the World Source grammar a CWR-C001 compile error. A `refworld/README.md` would stop `cwr build examples/refworld` at parse, and `benchmark/stubs/record_refworld.py` builds the directory in place. Keep `refworld/` free of anything outside the layout below.

## Layout of refworld/

| Path | Content |
|---|---|
| `world.kvx` | name refworld, version 0.2.0, Python 3.12, modules fs, kv, jobs, search, publish, world; every limit, clock and RNG key explicit (P07, P13) |
| `CHANGELOG.kvx` | releases 0.1.0 and 0.2.0; 0.2.0 has the fs.write size precondition, the kv namespace state_shape change with its migration, kv.used and the kv quota |
| `contracts/{fs,kv,jobs,world}.kvx` | the 29 internal primitives of reference-world.md section 2, preconditions in catalogue order |
| `adapters/{search,publish}.kvx` | search.query, publish.post, publish.status, publish.delete: external, idempotency, timeout_ms, failures including UNAVAILABLE, content_guaranteed false, `stub://` endpoints |
| `adapters/*/live.py` | gateway-side mapping of the stub wire protocol into the declared record shapes; no socket of its own |
| `lib/*.py` | kernel-side primitive implementations `f(state, *params)` against the kernel state API (world-source.md section 8). They are not importable by sandbox scripts. Scripts see only the generated SDK. |
| `fixtures/<module>/<primitive>.kvx` | one case per declared precondition failure in order, at least one ok case per primitive, and one recorded stub response per declared adapter failure; `fixtures/kv/migration_0_2_0.kvx` pins the migration |
| `manual/{intro,conventions,types,examples}.md` | hand-written manual prose and the named record types |
| `migrations/0_2_0_kv_namespaces.py` | pure, idempotent kv namespace migration |

## Checking it

```sh
cwr build examples/refworld --out <dir> --json   # all eight stages, contract tests included
python3 spec/cwr/tools/validate_spec.py          # the pack copy spec/cwr/examples/refworld stays valid
```

## Maintenance rules

1. **Cassettes.** The cassette set is recorded with `benchmark/stubs/record_refworld.py` against the compiled Build, and its digest is frozen in `benchmark/suite.kvx`. `tests/cwr/cases/4_1.py` (CWR-014) checks that every declared failure is reached, that the stubs emit only declared codes, and that the cassettes replay offline.
2. **After any byte change under `refworld/`**, such as a header or formatter pass, run `python3 examples/refworld-upgrades/pairs.py --write`. The pairs are derived from `refworld/` by exact text patches, and `U*/A` must equal `refworld/` byte for byte. Then re-record any cassette or evidence tied to the old Build hash.
3. **Fixture request hashes** in `fixtures/{search,publish}/*.kvx` follow `benchmark/cassettes/FORMAT.md` section 3. If that canonical hash changes, recapture them from the stubs. Do not edit them by hand.
4. **Never add `meta.example`** to `refworld/world.kvx`. That marker belongs only to the illustrative pack copy under `spec/cwr/examples/refworld`.

<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Reference world and upgrade pairs

`refworld/` is the reference World Source: a small world with files, key-value storage, background
jobs, a search service and a publishing service. It exercises every part of CWR that a world can
use, including external adapters with idempotency keys, rate limits and timeouts, and a state
migration. The task suite and every experiment run on it. Its contract is
[reference-world.md](../spec/cwr/reference-world.md).

[refworld-upgrades/](refworld-upgrades/README.md) holds six upgrade pairs derived from it. Each pair
is two World Sources that differ in exactly one rule.

## Build it

```sh
cargo build --workspace --release
target/release/cwr build examples/refworld --out build
```

The World Compiler runs all eight stages, including every contract test, and writes the Build to
`build/<hash>/`. `--json` prints the full stage report as JSON. When the output directory already
holds a different Build of the same world, the new source is diffed against it and must pass the
changelog gate: a version bump and a changelog entry for every change.
To create an Instance and run a turn, continue with the [crates README](../crates/README.md).

## Layout of refworld/

| Path | Content |
| --- | --- |
| `world.kvx` | Identity (`refworld` 0.2.0, Python 3.12), the modules `fs`, `kv`, `jobs`, `search`, `publish` and `world`, and every limit, clock and random-number key, all explicit |
| `CHANGELOG.kvx` | Releases 0.1.0 and 0.2.0. Release 0.2.0 adds the `fs.write` size precondition, `kv` namespaces with their migration, `kv.used` and the `kv` quota. |
| `contracts/{fs,kv,jobs,world}.kvx` | The 29 internal primitives, with preconditions in catalogue order |
| `adapters/{search,publish}.kvx` | The adapters `search.query`, `publish.post`, `publish.status` and `publish.delete`, with idempotency, `timeout_ms`, declared failures and `stub://` endpoints. Their content is not guaranteed. |
| `adapters/*/live.py` | Maps the stub wire protocol into the declared record shapes. It opens no socket of its own. |
| `lib/*.py` | The primitive implementations, `f(state, *params)`, run by the World Kernel. Turn scripts cannot import them and see only the generated SDK. |
| `fixtures/<module>/<primitive>.kvx` | Contract-test fixtures: one case per declared precondition failure, at least one success per primitive and one recorded stub response per declared adapter failure. `fixtures/kv/migration_0_2_0.kvx` pins the migration. |
| `manual/{intro,conventions,types,examples}.md` | The hand-written manual prose and the named record types |
| `migrations/0_2_0_kv_namespaces.py` | The pure, idempotent `kv` namespace migration |

The adapters are served in development and experiments by the
[stub servers](../benchmark/stubs/README.md).

## Rules for changing refworld/

- `refworld/` holds World Source and nothing else. The World Compiler rejects any file outside the
  World Source grammar with `CWR-C001`, so this README lives one level up and `refworld/` has none.
- `refworld/world.kvx` never carries `meta.example`. That marker belongs only to the illustrative
  copy under `spec/cwr/examples/refworld`.
- Any byte change under `refworld/` changes its Build hash. Regenerate the upgrade pairs with
  `python3 examples/refworld-upgrades/pairs.py --write`, then re-record every cassette and evidence
  record tied to the old hash.
- The request hashes in `fixtures/{search,publish}/*.kvx` follow
  [the cassette format](../benchmark/cassettes/FORMAT.md) section 3. They are captured from the
  stubs and never edited by hand.

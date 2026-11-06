<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Running CWR worlds: kernel and operator guide

Audience: operators who create, drive, snapshot, upgrade and replay Instances. Status: **pending verification**. As of 2026-11-06 implementation code for waves 0 to 2 is landing in the tree, but no qualification case that covers this guide has an executed attempt in the evidence registry, so nothing here is verified behaviour. This guide is derived from [spec/cwr/kernel.md](../../spec/cwr/kernel.md), [spec/cwr/model-surface.md](../../spec/cwr/model-surface.md) and the command table in [spec/cwr/compiler.md](../../spec/cwr/compiler.md). Where this guide and the contract disagree, the contract wins.

## How to read this guide

Sections that describe kernel behaviour open with a block naming the qualification case that would show it, the driver its task creates and the source files that implement it, split into files already in the tree (unverified) and files not yet written:

> **Pending verification.** Evidence: CWR-006 (driver `tests/cwr/cases/2_1.py`, no attempt recorded). Source in tree, unverified: `crates/cwr-kernel/src/ledger.rs`.

Task 6.1 removes a block only when its case has an executed attempt in the evidence registry. Commands are shown exactly as the specification names them; a command shown here is not evidence that it exists or passes.

## 1. Concepts

> **Pending verification.** Evidence: CWR-011 (driver `tests/cwr/cases/2_5.py`) for "only `cwr build` creates a Build" and the outbox, CWR-006 (driver `tests/cwr/cases/2_1.py`) for Instance state and ledger, no attempt recorded. Source in tree, unverified: `crates/cwr-kernel/src/lib.rs`.

- A **Build** is the frozen, content-addressed output of `cwr build`, a read-only directory `build/<sha256>/` (or `xbuild/<sha256>/` for experimental Builds). Only `cwr build` creates one; the kernel never writes into either tree.
- An **Instance** is one running world bound to one Build. It has a ULID identity, typed state, an append-only ledger, snapshots, a tick clock and an RNG stream. Many Instances can share one Build and share nothing else.
- A **turn** is one Python 3.12 script executed in a fresh sandbox. Each call the script makes into the world is one atomic primitive.
- The **ledger** is the complete record of an Instance and the only input experiments and oracles read.
- The **outbox** is where `world.propose` text goes. It is for a human and changes nothing inside the world.

## 2. Instance lifecycle

> **Pending verification.** Evidence: CWR-006 (driver `tests/cwr/cases/2_1.py`) and CWR-011 (driver `tests/cwr/cases/2_5.py`), no attempt recorded. Source in tree, unverified: `crates/cwr-cli/src/instance.rs`.

| State | Entered by | Can move to |
|---|---|---|
| `created` | `create`, before the header record is durable | `idle` |
| `idle` | Header durable, or end of a turn, upgrade or recovery | `in_turn`, `upgrading`, `destroyed` |
| `in_turn` | `turn`, after the protocol check passes | `idle`; `recovering` on kernel crash |
| `upgrading` | `upgrade`, after the pre-upgrade snapshot | `idle` on commit or rollback; `recovering` on crash |
| `recovering` | Kernel restart with an Instance left `in_turn` or `upgrading` | `idle` after the recovery record |
| `destroyed` | `destroy` | None |

A turn against an Instance that is not `idle` is refused with `CONFLICT` before anything executes. There are no concurrent turns within one Instance.

## 3. Checking a Build before use

> **Pending verification.** Evidence: CWR-005 (driver `tests/cwr/cases/1_3.py`), no attempt recorded. Source in tree, unverified: `crates/cwr-cli/src/manual.rs`, `crates/cwr-compiler/src/package.rs`.

The kernel recomputes a Build's hash before every load and refuses a mismatch. To read the manual the model will see, after hash verification:

```sh
cwr manual <hash>
```

Exit 0 on success, 5 on verification failure. To see what changed between two Builds and the derived version bump:

```sh
cwr diff <hash-a> <hash-b>
```

## 4. Creating an Instance

> **Pending verification.** Evidence: CWR-006 (driver `tests/cwr/cases/2_1.py`), CWR-013 (driver `tests/cwr/cases/3_2.py`) for the experimental gate, no attempt recorded. Source in tree, unverified: `crates/cwr-cli/src/instance.rs`, `crates/cwr-kernel/src/state.rs`.

```sh
cwr instance create <build-hash> [--seed N] [--tail K] [--arm LABEL] [--from-snapshot S] [--allow-experimental]
```

| Option | Meaning |
|---|---|
| `--seed N` | Seeds the Instance's RNG stream; always recorded in the header |
| `--tail K` | Ledger tail size rendered into the model's context; default 20; fixed for the Instance's life |
| `--arm LABEL` | Experiment arm label recorded in the header; never shown to the model |
| `--from-snapshot S` | Start from a snapshot instead of the Build's initial state |
| `--allow-experimental` | Required to load an experimental Build from `xbuild/` |

The new Instance is `idle` at turn 0 and world time 0, with a header record at seq 0 carrying the Build hash, seed, K, arm, model identity and `created_from`. Exit codes: 0 created; 2 Build hash mismatch; 3 experimental Build without `--allow-experimental`.

## 5. Running a turn

> **Pending verification.** Evidence: CWR-007 and CWR-008 (driver `tests/cwr/cases/2_2.py`), CWR-012 (driver `tests/cwr/cases/3_1.py`), no attempt recorded. Source in tree, unverified: `crates/cwr-cli/src/turn.rs`, `crates/cwr-kernel/src/executor.rs`. Proposed source, not yet written: `crates/cwr-kernel/src/surface/protocol.rs`.

```sh
cwr instance turn <id> < script.py
```

Standard input holds exactly one fenced python block or a raw script. The result envelope is printed on standard output. Exit 0 for status ok, error or partial; exit 4 for a protocol-invalid turn, in which nothing executes, world time does not advance and a `turn` record with status `error` and primitive `turn.protocol` is appended.

A script imports the world's modules from the generated SDK and nothing else:

```python
import fs, kv, search, publish, world

hits = search.query(q="tennis", limit=5)
fs.write(path="/notes/tennis.md", content=hits[0]["snippet"].encode())
receipt = publish.post(key="t-1", title=hits[0]["title"], body="see notes")
kv.set(key="last_receipt", value=receipt["id"])
return receipt
```

A top-level `return` value becomes `returned` in the envelope. A failing primitive raises a typed exception, one class per envelope code, carrying `code`, `primitive`, `precondition_index`, `retry_after` (only for `RATE_LIMITED`) and `detail`; the script may catch it and continue.

| Status | Meaning |
|---|---|
| `ok` | The script ended without an uncaught exception |
| `error` | Uncaught exception or limit termination, and no mutating or external primitive succeeded |
| `partial` | Uncaught exception or limit termination after at least one mutating or external primitive succeeded; those effects stand, nothing is rolled back |

The envelope always has the same shape:

```json
{
  "status": "ok | error | partial",
  "returned": "codec value or null",
  "error": {"code": "ENVELOPE_CODE", "primitive": "mod.name", "precondition_index": 2, "retry_after": null, "detail": "string"},
  "trace": [{"primitive": "mod.name", "status": "ok | error", "code": null, "world_time": 0}],
  "world": {"version": "0.2.0", "build": "sha256:...", "time": 416, "turn": 38},
  "resources": {"cpu_ms": 41, "wall_ms": 120, "primitive_calls": 4, "adapter_calls": 2, "remaining": {"primitive_calls": 496, "adapter_calls": 48}}
}
```

The trace lists every primitive call of the turn in order, so nobody has to infer which effects happened. Worked ok, error and partial envelopes are in [spec/cwr/model-surface.md](../../spec/cwr/model-surface.md) section 4.

Turns driven by a model go through the context assembler, which renders, in order, the Build's manual byte for byte, the recompile notice when one is pending, the last K ledger records and the previous envelope. A model message with zero, two or more fenced python blocks, or a block in another language, is refused as protocol-invalid. The command that renders this context for an operator is not named in the specification; task 3.1 proposes `crates/cwr-cli/src/context.rs` and must name it before this guide can show it.

## 6. Sandbox and per-turn limits

> **Pending verification.** Evidence: CWR-007 (driver `tests/cwr/cases/2_2.py`), no attempt recorded. Source in tree, unverified: `crates/cwr-kernel/src/sandbox.rs`, `crates/cwr-kernel/src/limits.rs`.

Each turn runs in a fresh process with no network, a read-only view containing only the interpreter and the Build's SDK, no host environment, no host clock and one RPC socket. A private scratch area is discarded at turn end. An import of anything outside the SDK is recorded as a closure refusal with code `INVALID` and primitive `sandbox.import`.

| Limit (from the Build's `world.kvx`) | When exceeded |
|---|---|
| `cpu_ms`, `wall_ms`, `memory_mb` | `LIMIT` naming the limit; the process is terminated |
| `primitive_calls`, `adapter_calls` | `LIMIT` naming the limit; the exceeding call is not executed |

Scripts can plan with `world.limits()`, which returns remaining budgets and configured values.

## 7. What a failure means

> **Pending verification.** Evidence: CWR-008 (driver `tests/cwr/cases/2_2.py`), no attempt recorded. Source in tree, unverified: `crates/cwr-kernel/src/contract_check.rs`.

Every primitive call is processed in a fixed order: validate parameters, evaluate preconditions in declared order, execute, assert effects (debug mode), advance world time, append and fsync the ledger record, then return.

- A failure at validation or at a precondition is an ordinary outcome. State is unchanged, no tick is spent, and the envelope names the primitive, the precondition index and its declared code.
- `INVARIANT_VIOLATION` is never ordinary. It means a mutating primitive failed during execution, an effect did not hold, or an undeclared code was raised. The Instance is sealed for inspection, the Build is considered defective and any experiment run containing it is excluded and reported.

## 8. World clock and randomness

> **Pending verification.** Evidence: CWR-006 (driver `tests/cwr/cases/2_1.py`), no attempt recorded. Source in tree, unverified: `crates/cwr-kernel/src/clock.rs`, `crates/cwr-kernel/src/rng.rs`.

World time is an integer tick counter starting at 0. It advances by `tick_per_primitive` for each executed primitive (including adapter calls that fail at the gateway) and by `tick_per_turn` at the end of each executed turn. Protocol-invalid turns and calls refused at validation or a precondition do not tick. `world.now()` returns the counter; no primitive returns host time.

`world.random()` is the only randomness: a PCG stream seeded from the Instance seed, advanced only by draws and captured in snapshots. Equal seeds and equal ledger prefixes give equal draws. `world.seed()` exists only when the Build sets `rng.seed_visible = true`.

## 9. Adapter gateway

> **Pending verification.** Evidence: CWR-009 (driver `tests/cwr/cases/2_3.py`), no attempt recorded. Source in tree, unverified: `crates/cwr-kernel/src/gateway/mod.rs`, `crates/cwr-kernel/src/gateway/cassette.rs`.

The gateway is the kernel's only network capability and contacts only endpoints declared in adapter contracts. It never retries; a retry is a second call made by the script and recorded as such.

| Mode | Behaviour |
|---|---|
| `live` | Calls the declared endpoint and records nothing |
| `record` | Calls the declared endpoint and writes a cassette entry keyed by (adapter, canonical request hash, sequence) |
| `replay` | Serves cassette entries; a miss is `UNAVAILABLE` and no socket is opened |

Service responses map onto declared codes only: declared failures to their mapped code, transport errors to `UNAVAILABLE`, no answer within `timeout_ms` to `TIMEOUT`, a malformed body to `INVALID`, throttling to `RATE_LIMITED` with `retry_after` in ticks. For `key` adapters the first outcome per (adapter, key, payload hash) is stored: an identical repeat returns it without contacting the service, a different payload is `DUPLICATE_KEY`.

How an operator selects the gateway mode and the cassette set is not named in the specification's command tables. Task 2.3 must pin it; until then this guide shows no flag for it.

## 10. The ledger

> **Pending verification.** Evidence: CWR-006 (driver `tests/cwr/cases/2_1.py`), no attempt recorded. Source in tree, unverified: `crates/cwr-kernel/src/ledger.rs`.

One append-only JSONL file per Instance. Each record is fsynced before any result returns. A ledger whose last record is truncated is refused by `create --from-snapshot`, `turn` and `replay` until `restore` or recovery has run. Where the file lives on disk is not fixed by the specification.

| Field | Meaning |
|---|---|
| `seq` | Monotonic from 0, no gaps |
| `instance`, `build` | Owning Instance (ULID) and Build hash at the time of the record |
| `turn`, `world_time` | Turn counter (0 for the header) and tick counter after this record |
| `kind` | `header`, `primitive`, `adapter`, `turn`, `drift`, `proposal`, `upgrade` or `recovery` |
| `primitive`, `params_hash` | Qualified name and hash of the serialised parameters, for primitive and adapter records |
| `preconditions` | One `{index, expr_hash, passed}` per evaluated precondition |
| `outcome` | `{status, code, shape, side_effect}` |
| `duration_us`, `host_ts` | Host measurements; never shown to the model and ignored by replay comparison |
| `experimental` | The experimental flag name on every record of an experimental Build, else null |

The determinism audit is a ledger query: group `primitive` records by (`build`, `primitive`, `params_hash`, `preconditions`) and expect one `outcome` per group.

## 11. Snapshots and restore

> **Pending verification.** Evidence: CWR-006 (driver `tests/cwr/cases/2_1.py`), no attempt recorded. Source in tree, unverified: `crates/cwr-kernel/src/snapshot.rs`.

```sh
cwr instance snapshot <id>
cwr instance restore <id> <snapshot>
```

`snapshot` requires an `idle` Instance and prints the snapshot's content hash (exit 0). A snapshot holds typed state, ledger seq, world time, turn, RNG state, K and the Build hash. `restore` requires a snapshot from this Instance's lineage whose Build hash verifies; it appends a `recovery` record and leaves later ledger records on disk for inspection outside the live lineage. Exit 0, or 2 on hash mismatch.

## 12. Crash recovery

> **Pending verification.** Evidence: CWR-010 (driver `tests/cwr/cases/2_4.py`), no attempt recorded. Source in tree, unverified: `crates/cwr-kernel/src/recovery.rs`.

A kernel crash is never shown to the model as a world event. On restart the kernel restores the last snapshot, applies the ledger prefix and records the interrupted turn as `error` with `UNAVAILABLE`, or `partial` if a mutating or external call had already completed. A primitive with a durable ledger record is never re-executed and the rest of the interrupted script is not resumed. For `key` adapters the idempotency entry is written before the request is sent, so the next identical call replays the stored outcome instead of re-sending. A crash during an upgrade restores the pre-upgrade snapshot and leaves the old Build bound. The full interleaving table is in [spec/cwr/kernel.md](../../spec/cwr/kernel.md) section 9.

## 13. Replay

> **Pending verification.** Evidence: CWR-010 (driver `tests/cwr/cases/2_4.py`), no attempt recorded. Source in tree, unverified: `crates/cwr-cli/src/replay.rs`, `crates/cwr-kernel/src/replay.rs`.

```sh
cwr instance replay <id> --into <new-id>
```

Recreates the Instance from seed, initial state, Build and cassettes, re-runs every recorded turn script and compares ledgers field by field, ignoring `host_ts` and `duration_us`. Requires a complete original ledger and a cassette for every adapter record. Exit 0 when identical; exit 7 on divergence, with the first divergent record printed and the new Instance sealed.

## 14. Upgrading an Instance

> **Pending verification.** Evidence: CWR-011 (driver `tests/cwr/cases/2_5.py`) and CWR-016 (driver `tests/cwr/cases/4_3.py`), no attempt recorded. Proposed source, not yet written: `crates/cwr-cli/src/upgrade.rs`, `crates/cwr-kernel/src/upgrade.rs`, `crates/cwr-kernel/src/notice.rs`.

Only a human changes a world: a world author edits the source and runs `cwr build`; an operator then moves each Instance explicitly.

```sh
cwr instance upgrade <id> <new-build-hash> [--allow-experimental]
```

In order, the kernel is proposed to: verify the target hash; refuse a different world name or a version that is not strictly greater (unless `--allow-experimental` and the target is experimental); require a migration for every state-shape change; snapshot and enter `upgrading`; apply migrations in changelog order; commit by rebinding the Build, appending an `upgrade` record and storing the recompile notice; return to `idle`. Any failure restores the snapshot with the old Build bound and nothing partial visible.

Exit codes: 0 upgraded; 5 Build refused; 6 migration failure after rollback.

On the Instance's next turn the model sees the notice, for example:

```text
WORLD RECOMPILED  0.1.0 -> 0.2.0   (sha256:ab12... -> sha256:cd34...)
Summary: fs.write now refuses content above 1 MiB with LIMIT; kv keys are namespaced per job.

Changes affecting primitives this instance has used:
  changed  fs.write
    before  accepted any content length
    after   len(content) <= 1048576, otherwise LIMIT
  changed  kv (state shape)
    before  one flat map of key to value
    after   namespaces; existing keys moved to the default namespace

State was migrated. Your ledger is intact. The manual above describes 0.2.0.
```

Only Builds compiled with `--x-version-only` (header line only) or `--x-silent-change` (no notice) render it differently.

## 15. Experimental Builds

> **Pending verification.** Evidence: CWR-013 (driver `tests/cwr/cases/3_2.py`), no attempt recorded. Proposed source, not yet written: `crates/cwr-kernel/src/drift_injector.rs`.

Experimental Builds live under `xbuild/`, carry `experimental = true` and the flag name in their manifest, and are refused without `--allow-experimental` (exit 3 on `create`). Every ledger record of such an Instance carries the flag name, so an ablation run can never be mistaken for a control run. How they are built and used is in [experiments.md](experiments.md).

## 16. Proposals

> **Pending verification.** Evidence: CWR-011 (driver `tests/cwr/cases/2_5.py`), no attempt recorded. Proposed source, not yet written: `crates/cwr-kernel/src/outbox.rs`.

A script may call `world.propose(text)`. The kernel appends a `proposal` ledger record and the text to `proposals.jsonl`, outside the Build and outside the state store, and returns an id. Nothing in the kernel reads the outbox; reading it and deciding whether to recompile is the owner's job. Where `proposals.jsonl` lives on disk is not fixed by the specification.

## 17. Destroying an Instance

> **Pending verification.** Evidence: none registered beyond the lifecycle cases above. Source in tree, unverified: `crates/cwr-cli/src/instance.rs`.

```sh
cwr instance destroy <id>
```

Requires an Instance that is not `in_turn`. The ledger is sealed and retained; state and snapshots are released. Exit 0.

## 18. Exit code reference

> **Pending verification.** All codes below are proposed in the specification and unexercised by any recorded case. The code in the tree differs in places; see [the implementation snapshot](README.md#implementation-snapshot-2026-11-06).

| Command | Exit codes |
|---|---|
| `cwr instance create` | 0; 2 hash mismatch; 3 experimental without flag |
| `cwr instance turn` | 0 ok, error or partial; 4 protocol INVALID |
| `cwr instance snapshot` | 0 |
| `cwr instance restore` | 0; 2 hash mismatch |
| `cwr instance upgrade` | 0; 5 refused Build; 6 migration failure after rollback |
| `cwr instance replay` | 0; 7 divergence |
| `cwr instance destroy` | 0 |
| `cwr manual <hash>` | 0; 5 verification failure |
| `cwr diff <hash-a> <hash-b>` | 0; 5 Build missing or failing verification |

## 19. Open points in the contract

Differences between specification chapters that this guide does not resolve:

- `cwr instance turn` accepts "exactly one fenced python block or raw script" ([spec/cwr/kernel.md](../../spec/cwr/kernel.md) section 2), while [spec/cwr/model-surface.md](../../spec/cwr/model-surface.md) section 3 refuses a model message with no fenced block. Whether raw scripts are an operator-only path is for task 3.1 to pin.
- `world.propose` is described as "read_only-classed with respect to world state" in [spec/cwr/kernel.md](../../spec/cwr/kernel.md) section 11 but listed as `mutating` in [spec/cwr/reference-world.md](../../spec/cwr/reference-world.md) section 2.5. Owners: tasks 2.5 and 4.1.
- The example script in section 5 is copied from [spec/cwr/model-surface.md](../../spec/cwr/model-surface.md) section 3 and calls `kv.set(key=..., value=...)`, whereas [spec/cwr/reference-world.md](../../spec/cwr/reference-world.md) section 2.2 declares `kv.set(ns, key, value)`. Owner: task 4.1.
- No command or flag selects the gateway mode or cassette set, renders the model context, or locates ledgers, snapshots and the outbox on disk. Owners: tasks 2.1, 2.3 and 3.1. The code in the tree has made provisional choices for some of these (`--home`, `--gateway`, `--cassette`); they are listed in [the implementation snapshot](README.md#implementation-snapshot-2026-11-06) and must be recorded in the specification before this guide documents them.

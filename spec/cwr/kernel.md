<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# World Kernel: instances, sandboxed turns, adapters, ledger and controlled change

Status: proposed contract; no kernel exists and every case is UNRUN. Requirements 5, 6, 7 and 8 in [spec.kvx](spec.kvx) apply together with the Build artifact contract in [compiler.md](compiler.md) and the context contract in [model-surface.md](model-surface.md). The sequence in [diagrams/turn.mmd](diagrams/turn.mmd) illustrates one turn. Nothing in this document is evidence that a kernel behaves as written.

## 1. Responsibility boundaries

The kernel runs Builds. One Build serves many Instances; everything the model does passes through the kernel, and everything that happens inside an Instance is in its ledger. The kernel has no write path into `build/` or `xbuild/`: it loads Builds, verifies their hash and never modifies them.

| Component | Owns | Does not establish |
|---|---|---|
| Kernel core | Instance lifecycle, atomic contract check, world clock, RNG, limits, snapshots, upgrade transaction | Rules: it enforces compiled contracts and cannot add, remove or relax one |
| Sandbox process | Execution of one script for one turn with the Build SDK | Any effect that did not cross the RPC boundary as a primitive call |
| Adapter gateway | The only network capability; normalisation, idempotency store, cassette modes, host-time timeouts | Retries, content guarantees or any failure code outside the envelope |
| State store | Typed per-module Instance state, content-addressed and separate from the Build | Durability of a mutation before its ledger record is written |
| Ledger | Append-only record of every header, primitive, adapter, turn, drift, proposal, upgrade and recovery event | Metrics or oracles; those are computed outside the kernel from the ledger and final state |
| Proposal outbox | Text the model wants a human to read, outside the world | Any effect on contracts, state, limits or scheduling |
| Human operator | Recompilation through `cwr build`, instance upgrade, experimental flags | Anything inside a running Instance; the operator cannot edit rules in place |

## 2. Instance lifecycle

An Instance is bound to one Build for its life unless explicitly upgraded. Its identity is a ULID; its header ledger record carries Build hash, seed, tail size K, arm label and model identity.

| Command | Arguments | Preconditions | Outcome | Exit code |
|---|---|---|---|---|
| `cwr instance create` | `<build-hash> [--seed N] [--tail K] [--arm LABEL] [--from-snapshot S] [--allow-experimental]` | Build directory exists and its hash verifies; experimental Build requires the flag | New Instance in state `idle`, header record at seq 0, world_time 0, turn 0 | 0; 2 on hash mismatch; 3 on experimental without flag |
| `cwr instance turn` | `<id> < script.py` | Instance `idle`; exactly one fenced python block or raw script on stdin | One turn executed; result envelope on stdout; Instance back to `idle` | 0 for ok, error or partial; 4 for protocol INVALID (nothing executed) |
| `cwr instance snapshot` | `<id>` | Instance `idle` | Snapshot written; content hash printed | 0 |
| `cwr instance restore` | `<id> <snapshot>` | Snapshot belongs to this Instance's lineage; Build hash in snapshot verifies | State, ledger seq, world_time, rng state and K restored; a `recovery` record appended | 0; 2 on hash mismatch |
| `cwr instance upgrade` | `<id> <new-build-hash> [--allow-experimental]` | Instance `idle`; same world name; strictly greater version unless experimental; migrations declared for every state-shape change | Migrated state, `upgrade` record, recompile notice queued for next turn | 0; 5 on refused Build; 6 on migration failure after rollback |
| `cwr instance replay` | `<id> --into <new-id>` | Original ledger complete; cassettes available for every adapter record | New Instance whose ledger equals the original except `host_ts` and `duration_us`; first divergence printed otherwise | 0; 7 on divergence |
| `cwr instance destroy` | `<id>` | Instance not `in_turn` | Ledger sealed and retained; state and snapshots released | 0 |

| State | Entered by | Allowed transitions |
|---|---|---|
| `created` | `create` before the header record is durable | `idle` |
| `idle` | Header durable, or end of any turn, upgrade or recovery | `in_turn`, `upgrading`, `destroyed` |
| `in_turn` | `turn` after the protocol check passes | `idle`; `recovering` on kernel crash |
| `upgrading` | `upgrade` after the pre-upgrade snapshot | `idle` on commit or rollback; `recovering` on crash |
| `recovering` | Kernel restart with an Instance left in `in_turn` or `upgrading` | `idle` after recovery record |
| `destroyed` | `destroy` | None |

A `turn` request against an Instance that is not `idle`, or whose lock another operation holds, is refused with `CONFLICT` before anything executes, with exit code 8 ([decisions.md](decisions.md) C02); no concurrent turns exist within one Instance. Instances of one Build share nothing but the Build.

## 3. Sandbox contract

Each turn runs in a fresh Python 3.12 process. The process is created in an empty network namespace, with a read-only filesystem view containing only the Build's generated SDK and the interpreter, no environment variables from the host, no host clock access and no inherited descriptors except the RPC socket. A tmpfs scratch area is private to the process and discarded at turn end; nothing written there survives or is visible to the next turn.

The SDK's only side door is one unix socket. Every public SDK function performs exactly one RPC call, and the kernel validates every call against the Build's contract table. A script that imports anything outside the SDK fails the import inside the process; the kernel records the attempt as a closure refusal with code `INVALID` and primitive name `sandbox.import`.

| Limit | Source | Enforcement | Reported as |
|---|---|---|---|
| `cpu_ms` | `world.kvx` | Process CPU time accounting | `LIMIT` with `limit = "cpu_ms"`; process terminated |
| `wall_ms` | `world.kvx` | Host wall timer on the turn | `LIMIT` with `limit = "wall_ms"`; process terminated |
| `memory_mb` | `world.kvx` | Resident memory cap on the process | `LIMIT` with `limit = "memory_mb"`; process terminated |
| `primitive_calls` | `world.kvx` | Counter per turn at the RPC boundary | `LIMIT` with `limit = "primitive_calls"`; the exceeding call is not executed |
| `adapter_calls` | `world.kvx` | Counter per turn at the gateway | `LIMIT` with `limit = "adapter_calls"`; the exceeding call is not executed |

A process termination ends the turn with status `error` or `partial` by the rules in section 5; the ledger records every primitive that completed before termination. `world.limits()` is a read_only primitive returning the remaining budget of each counter and the configured values of the process limits, so a script can plan before acting.

Stricter isolation such as a WebAssembly host is a future implementation choice; the contract is the observable closure, not the mechanism. Any mechanism must satisfy CWR-007.

## 4. Atomic contract check

Every primitive call crosses the boundary as one RPC and is processed in a fixed order. Steps 1 and 2 are what make rules enforced rather than merely documented.

| Step | Action | State visible to the script if this step fails |
|---|---|---|
| 1 | Validate parameters against the contract's typed `params`; mismatch is `INVALID` | Unchanged; no tick; ledger record with `outcome.code = INVALID` |
| 2 | Evaluate preconditions in declared order using read_only primitives; stop at the first failure | Unchanged; no tick; ledger record naming the primitive, the failing precondition index and its declared code |
| 3 | Execute against the state store (read_only, mutating) or through the gateway (external) | A read_only failure here leaves state unchanged; a mutating failure here is a defect recorded as `INVARIANT_VIOLATION` and the Instance is sealed; an external call that fails returns the normalised declared code with no world-state change |
| 4 | In debug mode, assert every declared effect | Effects hold or the call is recorded as `INVARIANT_VIOLATION` with the full trace and the Instance is sealed for inspection |
| 5 | Advance `world_time` by `tick_per_primitive` | Not a failure point |
| 6 | Append the ledger record and fsync | The result does not return until the record is durable; a crash here is handled by section 9 |

A failure at step 1 or 2 is an ordinary outcome and an expected part of the model's experience; the manual documents every code a primitive can return at those steps. A failure at step 3 for a mutating primitive, or at step 4, is never an ordinary outcome. `INVARIANT_VIOLATION` is in the envelope so that the model is told the truth, but a Build that produces it has a defect and any experiment run containing it is excluded and reported.

Preconditions are evaluated against the current state in order; because they reference only read_only primitives, the script could have evaluated the same expressions itself and reached the same answer. That is the visibility guarantee of requirement 2 restated as a kernel obligation.

## 5. Script semantics

Scripts are not transactional. Each primitive is atomic; a script is a sequence of atomic steps whose completed effects stand.

- A failing primitive raises a typed exception in the script, one class per envelope code, carrying `code`, `primitive`, `precondition_index`, `retry_after` (only for `RATE_LIMITED`) and `detail`. The script may catch it and continue.
- Status `ok`: the script ended without an uncaught exception.
- Status `error`: the script ended with an uncaught exception, or was terminated by a limit, and no mutating or external primitive succeeded during the turn.
- Status `partial`: the script ended with an uncaught exception, or was terminated by a limit, after at least one mutating or external primitive succeeded. Successful mutations stand; nothing is rolled back.
- The trace lists every primitive call of the turn in order with its outcome, so the model never has to infer which effects happened.
- The optional top-level `return` value is serialised through the SDK codec into `returned`; a value the codec cannot serialise is a failure with code `INVALID` naming `turn.return`, raised after all effects have been applied, and the trace still shows them. The status follows the two rules above: `error` when no mutating or external primitive succeeded during the turn, `partial` otherwise ([decisions.md](decisions.md) C01).

The result envelope shape is fixed and specified with examples in [model-surface.md](model-surface.md).

## 6. World clock and randomness

The world clock is an integer tick counter starting at 0 for each Instance. It advances by `tick_per_primitive` at step 5 of every executed primitive, including primitives that failed at step 3 through the gateway, and by `tick_per_turn` at the end of every executed turn. A turn refused by the protocol check (`INVALID`, see model-surface) executes nothing and does not tick. A primitive refused at step 1 or 2 does not tick.

Adapter timeouts are the only place host time enters a rule: `timeout_ms` is measured on the host clock because the external service lives on the host clock. World time still advances by exactly `tick_per_primitive` for that call however long the host waited. Job durations, lock expiries and `retry_after` are expressed in ticks, so a strategy of waiting and retrying is deterministic within the world. `world.now()` returns the tick counter. No primitive returns host time, host identity or process identity; `host_ts` in the ledger is never rendered to the model.

`world.random()` is the only source of randomness. It is a PCG stream seeded from the Instance seed at creation, advanced only by draws, and its state is part of every snapshot. Two Instances with equal seed and equal ledger prefix produce equal draws. `world.seed()` returns the seed when `rng.seed_visible` is true in the Build and is absent from the SDK otherwise.

## 7. Adapter gateway

The gateway is the kernel's only network capability. Neither `lib/` code nor the sandbox process has a socket. The gateway contacts only the `endpoint` declared in an adapter contract and nothing else.

| Mode | Behaviour | Writes |
|---|---|---|
| `live` | Calls the declared endpoint; records nothing | None |
| `record` | Calls the declared endpoint; writes a cassette entry per call | Cassette keyed by (adapter, canonical request hash, sequence) |
| `replay` | Serves cassette entries; a miss is `UNAVAILABLE` and no socket is opened | None |

The canonical request hash is computed over the adapter name and the parameters serialised through the SDK codec with sorted keys; the sequence is the ordinal of that (adapter, hash) pair within the Instance, so an adapter called twice with the same parameters replays its first and second recorded responses in order.

| Service response | Gateway result |
|---|---|
| Declared success status with a body matching the declared return shape | `ok` with the declared shape; content not guaranteed |
| Declared failure status mapped in the contract | The mapped declared code |
| Status not declared in the contract | `UNAVAILABLE` or `INVALID` as the contract's `undeclared_status` field selects ([world-source.md](world-source.md) section 7); default `UNAVAILABLE` |
| Transport error, connection refused, reset, DNS failure | `UNAVAILABLE` |
| No response within `timeout_ms` of host time | `TIMEOUT` |
| Body that does not match the declared return shape | `INVALID` |
| Rate limit signalled by the service | `RATE_LIMITED` with `retry_after` converted to ticks by the contract's declared ratio |

Every mapping above lands on a code the adapter contract declares in `failures`; an adapter whose declared list omits a code the gateway would need is refused by the compiler, not patched at runtime. The gateway never retries. A retry is a model decision, informed by `retry_after` and the manual, and appears in the ledger as a second call.

| Idempotency | Rule | Second call with same key or request |
|---|---|---|
| `key` | The gateway stores the first outcome per (adapter, key, payload hash) across modes, including live; the outcome is the ok result or the declared failure the call produced (a defect releases the key) | Identical payload: the stored outcome is returned without contacting the service. Different payload: `DUPLICATE_KEY` with the original receipt in `detail` |
| `natural` | The service is idempotent by its own semantics; the gateway records the request hash and outcome as evidence in the ledger record | Forwarded to the service; the ledger shows both calls with their outcomes |
| `none` | No idempotency; the manual labels the adapter non-idempotent | Forwarded; a duplicate effect is possible and the model has been told so |

## 8. Ledger

One Instance, one append-only JSONL file. Records are written at step 6 of every primitive, at turn end and at every lifecycle event, and fsynced before any result returns to the script or the operator. A ledger whose final record is truncated is refused by `create --from-snapshot`, `turn` and `replay` until `restore` or recovery has run.

| Field | Type | Meaning |
|---|---|---|
| `seq` | integer | Monotonic from 0; gaps are impossible, duplicates are a defect |
| `instance` | ULID string | Owning Instance |
| `build` | `sha256:<64hex>` | Build hash at the time of the record; changes only at an `upgrade` record |
| `turn` | integer | Turn counter; 0 for the header |
| `world_time` | integer | Tick counter after this record's advance |
| `kind` | enum | `header`, `primitive`, `adapter`, `turn`, `drift`, `proposal`, `upgrade`, `recovery` |
| `primitive` | string or null | Qualified primitive or adapter name for `primitive` and `adapter` kinds |
| `params_hash` | `blake3:<hex>` or null | Hash of the codec-serialised parameters |
| `preconditions` | list | One `{index, expr_hash, passed}` per evaluated precondition in order; evaluation stops at the first `passed = false` |
| `outcome` | object | `{status, code, shape, side_effect}`; `code` null on `ok`; `shape` is the declared return type name; `side_effect` is the contract's class |
| `duration_us` | integer | Host microseconds for the call; excluded from replay comparison |
| `host_ts` | RFC 3339 string | Host timestamp; never rendered to the model; excluded from replay comparison |
| `experimental` | string or null | Experimental flag name stamped on every record of an experimental Build, else null |

The header record (`seq = 0`, `kind = header`) carries `build`, `seed`, `tail_size` (K), `arm`, `model` (identity string supplied by the harness or operator) and `created_from` (snapshot hash or null). A `turn` record carries `status`, `returned_hash`, `resources` and the count of calls. A `drift` record carries the injected change from the drift specification. A `proposal` record carries the text hash. An `upgrade` record carries old and new Build hashes and the list of migrations applied. A `recovery` record carries the snapshot restored, the ledger seq resumed from and the interrupted turn number.

The experiment harness and every case oracle read only the ledger and the final state. The determinism audit of requirement 1 is a query over ledgers: group `primitive` records by (`build`, `primitive`, `params_hash`, `preconditions`) and assert one `outcome`.

## 9. Snapshots, crash recovery and replay

A snapshot captures the typed state store, the ledger `seq` at capture, `world_time`, `turn`, the RNG state, K and the Build hash. Restoring a snapshot yields an Instance that is observably identical to the one captured; the ledger beyond the captured `seq` is retained for inspection but no longer part of the live lineage, and a `recovery` record says so.

A kernel crash is never reported to the model as a world event. On restart the kernel finds Instances in `in_turn` or `upgrading`, restores the last snapshot, applies the ledger prefix up to the last durable record and then applies the rules below.

| Interleaving | Required observable result |
|---|---|
| Crash before step 3 of a primitive | No record exists; the turn is recorded as `error` with code `UNAVAILABLE`; state equals the state after the last durable record |
| Crash after step 3 and before step 6 of a mutating primitive | The mutation is not durable because the state store commits with the ledger record; state and ledger agree at the previous record; the turn is `error`/`UNAVAILABLE` |
| Crash after step 3 and before step 6 of an external primitive | The external effect may have happened; the gateway's idempotency store for `key` adapters is written before the request is sent, so recovery finds the pending key, marks the call `UNAVAILABLE` in a reconstructed record and the next identical call replays the stored outcome instead of re-sending; for `natural` and `none` adapters the record is `UNAVAILABLE` and the manual has told the model a duplicate is possible |
| Crash after step 6 | The record exists; the primitive is complete and is never re-executed; the remainder of the script is not resumed and the turn is recorded as `partial` if any mutating or external call succeeded, else `error`, with code `UNAVAILABLE` |
| Crash during an upgrade migration | The pre-upgrade snapshot is restored, the old Build remains bound, no `upgrade` record exists and the operator sees exit code 6 on the next `upgrade` attempt's report of the earlier failure |
| Crash during snapshot write | The partial snapshot file is discarded by hash verification; the previous snapshot remains the last good one |

`cwr instance replay` recreates an Instance from the seed, the initial state, the Build and the cassette set, re-running every turn's script as recorded, and compares ledgers field by field ignoring `host_ts` and `duration_us`. The first divergent record is a determinism defect; the new Instance is sealed and the divergence is printed. Replay is the mechanism that makes CWR-010 falsifiable and the basis for every campaign in [experiments.md](experiments.md).

## 10. Instance upgrade and controlled change

Recompilation is human-only by construction: the compiler is invoked only through `cwr build`, the kernel process has no write path into `build/` or `xbuild/`, the harness invokes the kernel but not the compiler, and the SDK has no primitive that reaches either. Editing World Source does nothing to a running Instance; only a new Build plus an explicit upgrade does.

`cwr instance upgrade` performs, in order:

1. Verify the target Build's hash over its contents.
2. Refuse a Build whose `world.name` differs, or whose version is not strictly greater than the running version, unless `--allow-experimental` is given and the target is an experimental Build.
3. Require that every state-shape entry in the target's `contract_diff.json` names a migration present in the target Build.
4. Take a snapshot and enter `upgrading`.
5. Apply the declared migrations in changelog order as pure functions over the typed state; each must return a state that validates against the target's state schema.
6. Commit: rebind the Instance to the new Build, append the `upgrade` record, store the recompile notice.
7. Return to `idle`; the notice is rendered on the next turn by the rules in [model-surface.md](model-surface.md).

Any failure in steps 1 through 6 restores the snapshot from step 4 and leaves the old Build bound; no partially migrated state is ever visible to a turn. The recompile notice is derived from the target's `CHANGELOG.kvx` entries between the two versions and lists the old and new versions and hashes, the human summary and every change whose target appears in this Instance's ledger as a used primitive. Only `--x-silent-change` and `--x-version-only` Builds alter the rendering, and they do so only in the surface, not in the stored notice.

## 11. Proposal outbox

`world.propose(text: str) -> ProposalId` is a read_only-classed primitive with respect to world state: it appends a `proposal` record to the ledger and appends the text to `proposals.jsonl` outside the Build and outside the state store, then returns an id. It has no other effect. Nothing in the kernel reads the outbox; the manual states plainly that a proposal is for a human and that the world will not change in response within this Build. The outbox is the only upward channel and is not visible to the model after writing except through the ledger record's existence.

## 12. Required future evidence

- CWR-006 (ledger, snapshot, clock, RNG): restored snapshots observably identical; records durable before results return; no host time through any primitive; equal seeds and prefixes draw equal randoms.
- CWR-007 (sandbox closure and limits): every unlisted capability refused from inside the script; each budget breach returns `LIMIT` naming the limit; rules unchanged across an Instance's life.
- CWR-008 (atomic primitives and partial scripts): state unchanged after a failed precondition; first failure in declared order reported with index; partial turns apply exactly the successful mutations; equal visible conditions give equal outcomes.
- CWR-009 (gateway): stub servers observe exactly the requests the ledger records with no retries; every response maps to a declared code; replay opens no socket.
- CWR-010 (recovery and replay): every interleaving in section 9 yields its required result with the stub server seeing one request; a full replay matches except host fields.
- CWR-011 (upgrade, notice, outbox): only `cwr build` creates Builds; upgrades are atomic with rollback; notices match frozen expectations; proposals change nothing observable inside the world.

Until these cases have executed attempts recorded in [verification.md](verification.md) and [qualification.kvx](qualification.kvx), every statement above is a design obligation, not a property of any program.

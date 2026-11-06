<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: Sidiora DEV OSS-1.0 -->

# Running CWR worlds: kernel and operator guide

Audience: operators who create, drive, snapshot, upgrade and replay Instances. Status on 2026-11-06: **implemented, not yet qualified**. The kernel, model surface and `cwr instance` commands described here are in the tree and every command in this guide runs against the packaged release, but no qualification case that covers this guide has an accepted attempt in the evidence registry, so nothing here is verified behaviour. This guide is derived from [spec/cwr/kernel.md](../../spec/cwr/kernel.md), [spec/cwr/model-surface.md](../../spec/cwr/model-surface.md) and the command table in [spec/cwr/compiler.md](../../spec/cwr/compiler.md). Where this guide and the contract disagree, the contract wins.

## How to read this guide

Sections that describe kernel behaviour open with a status marker, using the convention of [world-source.md](world-source.md#how-to-read-this-guide): **Verified** links an accepted attempt record, **Implemented, not yet qualified** links shipped source whose case has no accepted attempt, **Proposed, not implemented** names source that does not exist yet.

Every `sh` block runs, in order and in one shell, from the root of an unpacked release bundle with the packaged `cwr` on `PATH` (see [Install](../../README.md#install)); the release driver [tests/cwr/cases/6_1.py](../../tests/cwr/cases/6_1.py) executes each command exactly as written and fails if any exits non-zero. `text` blocks show syntax and are not executed. Instances live under `--home <dir>`, default `$CWR_HOME`, else `.cwr` in the working directory; a Build hash resolves under `--builds <dir>` (repeatable), default `build` and `xbuild`.

The examples share a refworld Build and one Instance:

```sh
build_hash() { python3 -c 'import json, sys; print(json.load(sys.stdin)["build"]["hash"])'; }
BUILD=$(cwr build examples/refworld --json | build_hash)
echo "$BUILD"
```

## 1. Concepts

> **Implemented, not yet qualified.** Source: [crates/cwr-kernel/src/lib.rs](../../crates/cwr-kernel/src/lib.rs), [crates/cwr-kernel/src/state.rs](../../crates/cwr-kernel/src/state.rs). Driver: [tests/cwr/cases/2_1.py](../../tests/cwr/cases/2_1.py), [tests/cwr/cases/2_5.py](../../tests/cwr/cases/2_5.py). CWR-006 and CWR-011 have no accepted attempt in the evidence registry, so this section describes shipped code, not verified behaviour.

- A **Build** is the frozen, content-addressed output of `cwr build`, a read-only directory `build/<sha256>/` (or `xbuild/<sha256>/` for experimental Builds). Only `cwr build` creates one; the kernel never writes into either tree.
- An **Instance** is one running world bound to one Build. It has a ULID identity, typed state, an append-only ledger, snapshots, a tick clock and an RNG stream, stored in `<home>/instances/<ULID>/`. Many Instances can share one Build and share nothing else.
- A **turn** is one Python 3.12 script executed in a fresh sandbox. Each call the script makes into the world is one atomic primitive.
- The **ledger** is the complete record of an Instance and the only input experiments and oracles read.
- The **outbox** is where `world.propose` text goes, `proposals.jsonl` in the Instance directory. It is for a human and changes nothing inside the world.

## 2. Instance lifecycle

> **Implemented, not yet qualified.** Source: [crates/cwr-cli/src/instance.rs](../../crates/cwr-cli/src/instance.rs), [crates/cwr-kernel/src/lib.rs](../../crates/cwr-kernel/src/lib.rs). Driver: [tests/cwr/cases/2_1.py](../../tests/cwr/cases/2_1.py), [tests/cwr/cases/2_5.py](../../tests/cwr/cases/2_5.py). CWR-006 and CWR-011 have no accepted attempt in the evidence registry, so this section describes shipped code, not verified behaviour.

| State | Entered by | Can move to |
|---|---|---|
| `created` | `create`, before the header record is durable | `idle` |
| `idle` | Header durable, or end of a turn, upgrade or recovery | `in_turn`, `upgrading`, `destroyed` |
| `in_turn` | `turn`, after the protocol check passes | `idle`; `recovering` on kernel crash |
| `upgrading` | `upgrade`, after the pre-upgrade snapshot | `idle` on commit or rollback; `recovering` on crash |
| `recovering` | Kernel restart with an Instance left `in_turn` or `upgrading` | `idle` after the recovery record |
| `destroyed` | `destroy` | None |

A turn against an Instance that is not `idle` is refused before anything executes. There are no concurrent turns within one Instance.

## 3. Checking a Build before use

> **Implemented, not yet qualified.** Source: [crates/cwr-cli/src/manual.rs](../../crates/cwr-cli/src/manual.rs), [crates/cwr-cli/src/diff.rs](../../crates/cwr-cli/src/diff.rs), [crates/cwr-compiler/src/package.rs](../../crates/cwr-compiler/src/package.rs). Driver: [tests/cwr/cases/1_3.py](../../tests/cwr/cases/1_3.py). CWR-005 has no accepted attempt in the evidence registry, so this section describes shipped code, not verified behaviour. The commands below are executed as written by the release driver.

The kernel recomputes a Build's hash before every load and refuses a mismatch. Read the manual the model will see, after hash verification:

```sh
cwr manual "$BUILD" > manual.md
sed -n '1,3p' manual.md
```

```text
cwr manual <hash|dir> [--out <dir>]
cwr diff <a> <b> [--out <dir>] [--allow-example]
```

`cwr manual` exits 0, or 5 when the Build fails verification. `cwr diff` takes two Build hashes, Build directories or World Sources and prints the ContractDiff with the derived and declared bumps; a worked example is in [world-source.md](world-source.md) section 14.

## 4. Creating an Instance

> **Implemented, not yet qualified.** Source: [crates/cwr-cli/src/instance.rs](../../crates/cwr-cli/src/instance.rs), [crates/cwr-kernel/src/state.rs](../../crates/cwr-kernel/src/state.rs). Driver: [tests/cwr/cases/2_1.py](../../tests/cwr/cases/2_1.py), [tests/cwr/cases/3_2.py](../../tests/cwr/cases/3_2.py). CWR-006 and CWR-013 have no accepted attempt in the evidence registry, so this section describes shipped code, not verified behaviour. The commands below are executed as written by the release driver.

```sh
ID=$(cwr instance create "$BUILD" --seed 7 --tail 20 --arm docs)
cwr instance show "$ID" > instance.json
```

```text
cwr instance create (<build-hash|dir> | --build <dir>) [--seed N] [--tail K|full] [--arm LABEL]
                    [--model M] [--from-snapshot S] [--initial <state.json>] [--allow-experimental]
```

| Option | Meaning |
|---|---|
| `--seed N` | Seeds the Instance's RNG stream; always recorded in the header |
| `--tail K\|full` | Ledger tail size rendered into the model's context; default 20; fixed for the Instance's life |
| `--arm LABEL` | Experiment arm label recorded in the header; never shown to the model |
| `--model M` | Model identity recorded in the header |
| `--from-snapshot S` | Start from a snapshot instead of the Build's initial state |
| `--initial <state.json>` | Start from an explicit initial state (the task suite's initial states) |
| `--allow-experimental` | Required to load an experimental Build from `xbuild/` |

`create` prints the new Instance id. The Instance is `idle` at turn 0 and world time 0, with a header record at seq 0 carrying the Build hash, seed, K, arm, model identity and origin. Exit codes: 0 created; 1 usage or other error; 2 Build hash mismatch; 3 experimental Build without `--allow-experimental`.

## 5. Running a turn

> **Implemented, not yet qualified.** Source: [crates/cwr-cli/src/turn.rs](../../crates/cwr-cli/src/turn.rs), [crates/cwr-kernel/src/executor.rs](../../crates/cwr-kernel/src/executor.rs), [crates/cwr-kernel/src/surface/protocol.rs](../../crates/cwr-kernel/src/surface/protocol.rs), [crates/cwr-kernel/src/surface/envelope_render.rs](../../crates/cwr-kernel/src/surface/envelope_render.rs). Driver: [tests/cwr/cases/2_2.py](../../tests/cwr/cases/2_2.py), [tests/cwr/cases/3_1.py](../../tests/cwr/cases/3_1.py). CWR-007 and CWR-008 and CWR-012 have no accepted attempt in the evidence registry, so this section describes shipped code, not verified behaviour. The commands below are executed as written by the release driver.

A script imports the world's modules from the generated SDK and nothing else. A top-level `return` value becomes `returned` in the envelope:

```sh
cat > hello.py <<'EOF'
import fs
fs.write(path="/notes/hello.md", content=b"hello")
return fs.read(path="/notes/hello.md").decode()
EOF
cwr instance turn "$ID" < hello.py > envelope.json
python3 -c 'import json; e = json.load(open("envelope.json")); print(e["status"], e["returned"], e["world"]["turn"])'
```

```text
cwr instance turn <id> [--script <file> | --message <file> | < script.py] [--debug-effects]
                       [--gateway live|record|replay] [--cassette <file>] [--cassette-label <label>]
                       [--stub NAME=URL]... [--home <dir>] [--builds <dir>]
```

Standard input or `--script` holds a raw script, the operator path. `--message <file>` holds a model message: the turn protocol extracts its one fenced python block, and a message with zero or more than one fenced block, a block in another language or an unclosed fence is refused as `INVALID` naming `turn.protocol`, with nothing executed and world time unchanged (exit 4):

```sh
printf 'No code here.\n' > message.txt
cwr instance turn "$ID" --message message.txt > refused.json || echo "refused with exit $?"
```

The result envelope is printed as one line of canonical JSON on standard output; standard error names the sandbox mechanism. Exit 0 for status ok, error or partial; 1 usage or kernel error (an Instance that is not idle, sealed, or a sandbox that cannot be provided: the kernel never runs a script unsandboxed); 2 Build hash mismatch; 3 experimental Build; 4 protocol-invalid message. A failing primitive raises a typed exception, one class per envelope code, carrying `code`, `primitive`, `precondition_index`, `retry_after` (only for `RATE_LIMITED`) and `detail`; the script may catch it and continue.

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

The trace lists every primitive call of the turn in order, so nobody has to infer which effects happened. Worked ok, error and partial envelopes are in [spec/cwr/model-surface.md](../../spec/cwr/model-surface.md) section 4. `--debug-effects` asserts every declared effect after an ok mutating call.

## 6. The model's context

> **Implemented, not yet qualified.** Source: [crates/cwr-cli/src/context.rs](../../crates/cwr-cli/src/context.rs), [crates/cwr-kernel/src/surface/context.rs](../../crates/cwr-kernel/src/surface/context.rs), [crates/cwr-kernel/src/surface/tail.rs](../../crates/cwr-kernel/src/surface/tail.rs). Driver: [tests/cwr/cases/3_1.py](../../tests/cwr/cases/3_1.py). CWR-012 has no accepted attempt in the evidence registry, so this section describes shipped code, not verified behaviour. The commands below are executed as written by the release driver.

Turns driven by a model go through the context assembler, which renders, in order, the Build's manual byte for byte, the recompile notice when one is pending, the last K ledger records and the previous envelope. The operator sees exactly what the model will see next:

```sh
cwr instance context "$ID" > context.txt
cwr instance context "$ID" --json > context.json
cwr instance tail "$ID"
```

```text
cwr instance context <id> [--tail K|full] [--json] [--home <dir>]
```

`--json` prints each component with its length and sha256 and the digest of the whole context; the manual component is byte-identical on every turn of one Build.

## 7. Sandbox and per-turn limits

> **Implemented, not yet qualified.** Source: [crates/cwr-kernel/src/sandbox.rs](../../crates/cwr-kernel/src/sandbox.rs), [crates/cwr-kernel/src/limits.rs](../../crates/cwr-kernel/src/limits.rs), [crates/cwr-kernel/SANDBOX.md](../../crates/cwr-kernel/SANDBOX.md). Driver: [tests/cwr/cases/2_2.py](../../tests/cwr/cases/2_2.py). CWR-007 has no accepted attempt in the evidence registry, so this section describes shipped code, not verified behaviour.

Each turn runs in a fresh process with no network, a read-only view containing only the interpreter and the Build's SDK, no host environment, no host clock and one RPC socket. A private scratch area is discarded at turn end. An import of anything outside the SDK is recorded as a closure refusal with code `INVALID` and primitive `sandbox.import`. The mechanism in use (bubblewrap namespaces plus rlimits and an in-interpreter guard) is named on standard error at every turn and documented in [crates/cwr-kernel/SANDBOX.md](../../crates/cwr-kernel/SANDBOX.md).

| Limit (from the Build's `world.kvx`) | When exceeded |
|---|---|
| `cpu_ms`, `wall_ms`, `memory_mb` | `LIMIT` naming the limit; the process is terminated |
| `primitive_calls`, `adapter_calls` | `LIMIT` naming the limit; the exceeding call is not executed |

Scripts can plan with `world.limits()`, which returns remaining budgets and configured values.

## 8. What a failure means

> **Implemented, not yet qualified.** Source: [crates/cwr-kernel/src/contract_check.rs](../../crates/cwr-kernel/src/contract_check.rs). Driver: [tests/cwr/cases/2_2.py](../../tests/cwr/cases/2_2.py). CWR-008 has no accepted attempt in the evidence registry, so this section describes shipped code, not verified behaviour.

Every primitive call is processed in a fixed order: validate parameters, evaluate preconditions in declared order, execute, assert effects (debug mode), advance world time, append and fsync the ledger record, then return.

- A failure at validation or at a precondition is an ordinary outcome. State is unchanged, no tick is spent, and the envelope names the primitive, the precondition index and its declared code.
- `INVARIANT_VIOLATION` is never ordinary. It means a mutating primitive failed during execution, an effect did not hold, or an undeclared code was raised. The Instance is sealed for inspection, the Build is considered defective and any experiment run containing it is excluded and reported.

## 9. World clock and randomness

> **Implemented, not yet qualified.** Source: [crates/cwr-kernel/src/clock.rs](../../crates/cwr-kernel/src/clock.rs), [crates/cwr-kernel/src/rng.rs](../../crates/cwr-kernel/src/rng.rs). Driver: [tests/cwr/cases/2_1.py](../../tests/cwr/cases/2_1.py). CWR-006 has no accepted attempt in the evidence registry, so this section describes shipped code, not verified behaviour. The commands below are executed as written by the release driver.

World time is an integer tick counter starting at 0. It advances by `tick_per_primitive` for each executed primitive (including adapter calls that fail at the gateway) and by `tick_per_turn` at the end of each executed turn. Protocol-invalid turns and calls refused at validation or a precondition do not tick. `world.now()` returns the counter; no primitive returns host time.

`world.random()` is the only randomness: a PCG stream seeded from the Instance seed, advanced only by draws and captured in snapshots. Equal seeds and equal ledger prefixes give equal draws. `world.seed()` exists only when the Build sets `rng.seed_visible = true`. The operator can query the kernel-owned world facilities (`world.now`, `world.version`, `world.seed`, `world.limits`, `world.random`, `world.history`) in one kernel-direct turn; a call is `name` or `name:<json params>`:

```sh
cwr instance probe "$ID" world.now world.version > probe.jsonl
```

## 10. Adapter gateway

> **Implemented, not yet qualified.** Source: [crates/cwr-kernel/src/gateway/mod.rs](../../crates/cwr-kernel/src/gateway/mod.rs), [crates/cwr-kernel/src/gateway/cassette.rs](../../crates/cwr-kernel/src/gateway/cassette.rs), [crates/cwr-kernel/src/gateway/normalise.rs](../../crates/cwr-kernel/src/gateway/normalise.rs). Driver: [tests/cwr/cases/2_3.py](../../tests/cwr/cases/2_3.py). CWR-009 has no accepted attempt in the evidence registry, so this section describes shipped code, not verified behaviour.

The gateway is the kernel's only network capability and contacts only endpoints declared in adapter contracts. It never retries; a retry is a second call made by the script and recorded as such. The mode is chosen per turn with `cwr instance turn --gateway`:

| Mode | Behaviour |
|---|---|
| `live` | Calls the declared endpoint (`--stub NAME=URL` maps a `stub://` endpoint to a local stub server) and records nothing |
| `record` | Calls the declared endpoint and writes a cassette entry keyed by (adapter, canonical request hash, sequence) to `--cassette <file>` |
| `replay` | Serves entries from `--cassette <file>`; a miss is `UNAVAILABLE` and no socket is opened |

Service responses map onto declared codes only: declared failures to their mapped code, transport errors to `UNAVAILABLE`, no answer within `timeout_ms` to `TIMEOUT`, a malformed body to `INVALID`, a status the adapter does not map to its `undeclared_status`, throttling to `RATE_LIMITED` with `retry_after` in ticks. For `key` adapters the first outcome per (adapter, key, payload hash) is stored: an identical repeat returns it without contacting the service, a different payload is `DUPLICATE_KEY`. The cassette format is [benchmark/cassettes/FORMAT.md](../../benchmark/cassettes/FORMAT.md).

## 11. The ledger

> **Implemented, not yet qualified.** Source: [crates/cwr-kernel/src/ledger.rs](../../crates/cwr-kernel/src/ledger.rs). Driver: [tests/cwr/cases/2_1.py](../../tests/cwr/cases/2_1.py). CWR-006 has no accepted attempt in the evidence registry, so this section describes shipped code, not verified behaviour. The commands below are executed as written by the release driver.

One append-only JSONL file per Instance, `<home>/instances/<id>/ledger.jsonl`, hash-chained record to record. Each record is fsynced before any result returns. A ledger whose last record is truncated is refused by `create --from-snapshot`, `turn` and `replay` until `restore` or recovery has run. `cwr instance ledger` checks the sequence, chain and truncation strictly:

```sh
cwr instance ledger "$ID"
```

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

## 12. Replay

> **Implemented, not yet qualified.** Source: [crates/cwr-cli/src/replay.rs](../../crates/cwr-cli/src/replay.rs), [crates/cwr-kernel/src/replay.rs](../../crates/cwr-kernel/src/replay.rs). Driver: [tests/cwr/cases/2_4.py](../../tests/cwr/cases/2_4.py). CWR-010 has no accepted attempt in the evidence registry, so this section describes shipped code, not verified behaviour. The commands below are executed as written by the release driver.

Replay recreates the Instance from its seed, initial state, Build, cassette and recorded turn scripts, re-runs every turn and compares the new ledger with the original field by field, ignoring `host_ts`, `duration_us` and `instance`. It reproduces the records turns produce; a ledger that contains lifecycle records (a restore, recovery or upgrade) is refused. A fresh Instance with one turn replays identically:

```sh
RID=$(cwr instance create "$BUILD" --seed 11)
cwr instance turn "$RID" < hello.py > replay-turn.json
cwr instance replay "$RID" > replay.json
```

```text
cwr instance replay (<id> | <ledger.jsonl>) [--build <dir>] [--cassette <file>] [--scripts <dir>]
                    [--initial <state.json>] [--home <dir>] [--into-home <dir>] [--debug-effects]
cwr instance compare <ledger-a> <ledger-b>
```

Exit 0 when identical; 7 on divergence, with the first divergent record printed and the new Instance sealed; 2 on a Build or initial-state hash mismatch; 1 when refused (tampered or truncated ledger, missing input). `cwr instance compare` applies the same comparison to two ledger files.

## 13. Snapshots and restore

> **Implemented, not yet qualified.** Source: [crates/cwr-kernel/src/snapshot.rs](../../crates/cwr-kernel/src/snapshot.rs). Driver: [tests/cwr/cases/2_1.py](../../tests/cwr/cases/2_1.py). CWR-006 has no accepted attempt in the evidence registry, so this section describes shipped code, not verified behaviour. The commands below are executed as written by the release driver.

```sh
SNAP=$(cwr instance snapshot "$ID")
cwr instance restore "$ID" "$SNAP" > restore.json
```

`snapshot` requires an `idle` Instance and prints the snapshot's content hash. A snapshot holds typed state, ledger seq, world time, turn, RNG state, K and the Build hash. `restore` requires a snapshot from this Instance's lineage whose Build hash verifies; it appends a `recovery` record and prints it, and leaves later ledger records on disk for inspection outside the live lineage. Exit 0, or 2 on hash mismatch.

## 14. Crash recovery

> **Implemented, not yet qualified.** Source: [crates/cwr-kernel/src/recovery.rs](../../crates/cwr-kernel/src/recovery.rs). Driver: [tests/cwr/cases/2_4.py](../../tests/cwr/cases/2_4.py). CWR-010 has no accepted attempt in the evidence registry, so this section describes shipped code, not verified behaviour. The commands below are executed as written by the release driver.

A kernel crash is never shown to the model as a world event. On restart the kernel restores the last snapshot, applies the ledger prefix and records the interrupted turn as `error` with `UNAVAILABLE`, or `partial` if a mutating or external call had already completed. A primitive with a durable ledger record is never re-executed and the rest of the interrupted script is not resumed. For `key` adapters the idempotency entry is written before the request is sent, so the next identical call replays the stored outcome instead of re-sending. A crash during an upgrade restores the pre-upgrade snapshot and leaves the old Build bound. The full interleaving table is in [spec/cwr/kernel.md](../../spec/cwr/kernel.md) section 9. Recovery runs on the next `cwr instance turn`, or explicitly; on a healthy Instance it reports that nothing needed recovery:

```sh
cwr instance recover "$ID"
```

## 15. Upgrading an Instance

> **Implemented, not yet qualified.** Source: [crates/cwr-cli/src/upgrade.rs](../../crates/cwr-cli/src/upgrade.rs), [crates/cwr-kernel/src/upgrade.rs](../../crates/cwr-kernel/src/upgrade.rs), [crates/cwr-kernel/src/notice.rs](../../crates/cwr-kernel/src/notice.rs), [crates/cwr-kernel/src/migrate.rs](../../crates/cwr-kernel/src/migrate.rs). Driver: [tests/cwr/cases/2_5.py](../../tests/cwr/cases/2_5.py), [tests/cwr/cases/4_3.py](../../tests/cwr/cases/4_3.py). CWR-011 and CWR-016 have no accepted attempt in the evidence registry, so this section describes shipped code, not verified behaviour. The commands below are executed as written by the release driver.

Only a human changes a world: a world author edits the source and runs `cwr build`; an operator then moves each Instance explicitly. Upgrade pair U1 (side B adds a `LOCKED` precondition to `fs.delete`) shows the whole path:

```sh
A=$(cwr build examples/refworld-upgrades/U1/A --out pairs/U1-A --json | build_hash)
B=$(cwr build examples/refworld-upgrades/U1/B --out pairs/U1-B --previous "pairs/U1-A/${A#sha256:}" --json | build_hash)
UPID=$(cwr instance create "$A" --builds pairs/U1-A --builds pairs/U1-B --seed 1)
cwr instance turn "$UPID" --builds pairs/U1-A --builds pairs/U1-B < hello.py > before.json
cwr instance upgrade "$UPID" "$B" --builds pairs/U1-A --builds pairs/U1-B > upgrade.json
cwr instance notice "$UPID" --builds pairs/U1-A --builds pairs/U1-B
```

```text
cwr instance upgrade <id> (<build-hash|dir> | --build <dir>) [--allow-experimental]
cwr instance notice <id>
```

In order, the kernel verifies the target hash; refuses a different world name or a version that is not strictly greater (unless `--allow-experimental` and the target is experimental); requires a migration for every state-shape change; snapshots and enters `upgrading`; applies migrations in changelog order; commits by rebinding the Build, appending an `upgrade` record and storing the recompile notice; and returns to `idle`. Any failure restores the snapshot with the old Build bound and nothing partial visible. `upgrade` prints one JSON line with the status, the upgrade record and the notice. Exit codes: 0 upgraded; 5 Build refused, nothing changed; 6 migration or commit failure after rollback.

`cwr instance notice` prints context component 2 for the next turn. For U1 it reads:

```text
WORLD RECOMPILED  0.2.0 -> 0.3.0   (sha256:<hash A> -> sha256:<hash B>)
Summary: fs.delete now refuses a locked path with LOCKED; unlock the path before deleting it.

Changes affecting primitives this instance has used:
  (none)

Your ledger is intact. The manual above describes 0.3.0.
```

The Instance above used only `fs.write` and `fs.read`, so no change is listed; an Instance that had called `fs.delete` would see its before and after rule. Only Builds compiled with `--x-version-only` (header line only) or `--x-silent-change` (no notice) render it differently; see [experiments.md](experiments.md) section 3.

## 16. Experimental Builds

> **Implemented, not yet qualified.** Source: [crates/cwr-compiler/src/experimental/mod.rs](../../crates/cwr-compiler/src/experimental/mod.rs), [crates/cwr-kernel/src/drift_injector.rs](../../crates/cwr-kernel/src/drift_injector.rs). Driver: [tests/cwr/cases/3_2.py](../../tests/cwr/cases/3_2.py). CWR-013 has no accepted attempt in the evidence registry, so this section describes shipped code, not verified behaviour. The commands below are executed as written by the release driver.

Experimental Builds live under `xbuild/`, carry `experimental = true` and the flag name in their manifest, and are refused without `--allow-experimental` (exit 3 on `create`). Every ledger record of such an Instance carries the flag name, so an ablation run can never be mistaken for a control run:

```sh
X=$(cwr build examples/refworld --x-one-primitive --json | build_hash)
cwr instance create "$X" --seed 1 || echo "refused with exit $?"
XID=$(cwr instance create "$X" --seed 1 --arm one-primitive --allow-experimental)
```

How the arms are built and used is in [experiments.md](experiments.md).

## 17. Proposals

> **Implemented, not yet qualified.** Source: [crates/cwr-kernel/src/outbox.rs](../../crates/cwr-kernel/src/outbox.rs), [crates/cwr-cli/src/upgrade.rs](../../crates/cwr-cli/src/upgrade.rs). Driver: [tests/cwr/cases/2_5.py](../../tests/cwr/cases/2_5.py). CWR-011 has no accepted attempt in the evidence registry, so this section describes shipped code, not verified behaviour. The commands below are executed as written by the release driver.

A script may call `world.propose(text=...)`. The kernel appends a `proposal` ledger record and the text to `proposals.jsonl` in the Instance directory, outside the Build and outside the state store, and returns an id. Nothing in the kernel reads the outbox; reading it and deciding whether to recompile is the owner's job:

```sh
cat > propose.py <<'EOF'
import world
return world.propose(text="please allow deleting locked drafts")
EOF
cwr instance turn "$UPID" --builds pairs/U1-A --builds pairs/U1-B < propose.py > proposal.json
cwr instance outbox "$UPID"
```

## 18. Destroying an Instance

> **Implemented, not yet qualified.** Source: [crates/cwr-cli/src/instance.rs](../../crates/cwr-cli/src/instance.rs). Driver: [tests/cwr/cases/2_1.py](../../tests/cwr/cases/2_1.py). CWR-006 has no accepted attempt in the evidence registry, so this section describes shipped code, not verified behaviour. The commands below are executed as written by the release driver.

```sh
cwr instance destroy "$ID"
```

Requires an Instance that is not `in_turn`. The ledger is sealed and retained; state and snapshots are released. Exit 0.

## 19. Exit code reference

> **Implemented, not yet qualified.** Source: [crates/cwr-kernel/src/lib.rs](../../crates/cwr-kernel/src/lib.rs), [crates/cwr-cli/src/main.rs](../../crates/cwr-cli/src/main.rs). Driver: [tests/cwr/cases/2_1.py](../../tests/cwr/cases/2_1.py), [tests/cwr/cases/2_4.py](../../tests/cwr/cases/2_4.py), [tests/cwr/cases/2_5.py](../../tests/cwr/cases/2_5.py). CWR-006 and CWR-010 and CWR-011 have no accepted attempt in the evidence registry, so this section describes shipped code, not verified behaviour.

Every `cwr instance` command exits 1 on a usage error or any kernel error not listed here.

| Command | Exit codes |
|---|---|
| `cwr instance create` | 0; 2 hash mismatch; 3 experimental without flag |
| `cwr instance turn` | 0 ok, error or partial; 2 hash mismatch; 3 experimental Build; 4 protocol INVALID (`--message`) |
| `cwr instance snapshot` | 0 |
| `cwr instance restore` | 0; 2 hash mismatch |
| `cwr instance upgrade` | 0; 5 refused Build; 6 migration or commit failure after rollback |
| `cwr instance replay` | 0; 2 hash mismatch; 7 divergence |
| `cwr instance compare` | 0 identical; 7 divergent |
| `cwr instance destroy` | 0 |
| `cwr manual <hash>` | 0; 5 verification failure |
| `cwr diff <a> <b>` | 0; 5 Build missing or failing verification |

## 20. Where the command line departs from the specification

The specification's command tables ([spec/cwr/compiler.md](../../spec/cwr/compiler.md) section 9, [spec/cwr/kernel.md](../../spec/cwr/kernel.md) section 2) name the commands; the shipped binary adds options and subcommands. This guide documents the shipped form. Additions: `--home` and `--builds` on every `cwr instance` command; `create --build`, `--model`, `--initial` and `--tail full`; `turn --script`, `--message`, `--debug-effects`, `--gateway`, `--cassette`, `--cassette-label`, `--stub` and `--diagnostic-no-language-guard`; `replay` taking an Instance or a ledger file, with `--into-home` instead of `--into <new-id>`; the inspection commands `show`, `state`, `ledger`, `tail`, `probe`, `context`, `notice`, `outbox`, `recover` and `compare`; `cwr contract` for the typed contract model; and `cwr release` ([RELEASE.md](../../RELEASE.md)). `cwr manual` and `cwr diff` accept Build directories as well as hashes.

## 21. Open points in the contract

- `world.propose` is described as "read_only-classed with respect to world state" in [spec/cwr/kernel.md](../../spec/cwr/kernel.md) section 11 but listed as `mutating` in [spec/cwr/reference-world.md](../../spec/cwr/reference-world.md) section 2.5. Owners: tasks 2.5 and 4.1.
- `replay` refuses a ledger containing restore, recovery or upgrade records, so an upgraded Instance is replayed per Build segment by the harness rather than with one command. [spec/cwr/kernel.md](../../spec/cwr/kernel.md) section 2 does not state this limit. Owner: task 2.4.

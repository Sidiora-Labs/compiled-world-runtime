<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Running a world: the World Kernel

Status: CWR has not yet passed qualification. This guide describes what the current World Kernel,
its Model Surface and the `cwr` binary do.

The World Kernel runs Instances of a Build. Each turn it executes one Python script in a sandbox,
checks every primitive call against the Build's contracts, records every action in an append-only,
hash-chained ledger and passes every call to an outside service through the adapter gateway.

The kernel guarantees:

| Guarantee | What it means for an operator |
|---|---|
| Closure | A script reaches only the SDK of its Build. No network, host filesystem or host clock. |
| Atomic primitives | Every call happens completely or not at all, and a refusal names one of eleven codes. |
| A frozen Build | An Instance stays on its Build until an operator upgrades it. The kernel refuses a Build whose bytes do not match its hash. |
| An honest ledger | Every call, turn, upgrade and recovery is a ledger record. A tampered or truncated ledger is refused. |
| Reproducible turns | Replaying recorded scripts with recorded adapter responses produces an identical ledger. |
| Mediated side effects | Adapter calls follow their declared timeout, idempotency and failure rules, and are never retried silently. |

This guide runs the reference world, `refworld`, and walks through every operator task.

## Before you start

Install the `cwr` binary as described in [Install](../../README.md#install). The kernel needs
Python 3.12 and bubblewrap. It refuses to run a script when it cannot build the sandbox.

The commands below run in one shell. They start in the repository root and then work in a scratch
directory. Hashes and Instance ids in the output excerpts are shortened.

```sh
REPO=$PWD
cd "$(mktemp -d)"
cwr build "$REPO/examples/refworld"
BUILD=$(ls build)
```

## Concepts

| Term | Meaning |
|---|---|
| Build | The read-only output of `cwr build`, stored as `build/<sha256>/`. Experimental Builds are stored under `xbuild/`. |
| Instance | One running world: a state store, a ledger, a world clock, a seeded random stream and snapshots, bound to one Build |
| Turn | One script, run once, in a fresh sandboxed process |
| Ledger | The Instance's append-only, hash-chained record of everything that happened |
| Model Surface | What the model receives each turn: the manual, a pending recompile notice, the ledger tail and the last result envelope |
| Adapter gateway | The kernel component that performs every external call, in live, record or replay mode |
| Recompile notice | The text the model receives on its first turn after an upgrade |
| Proposal outbox | Where `world.propose` writes. Only the human owner reads it. |

Instances live under `.cwr/instances/<id>/` in the working directory. `--home <dir>` or the
`CWR_HOME` environment variable names another location. A Build hash resolves against `build/` and
`xbuild/`, or against each `--builds <dir>` given. Every command that takes a Build also accepts a
Build directory.

## Create an Instance

`cwr instance create` prints the new Instance id.

```sh
ID=$(cwr instance create "$BUILD" --seed 7)
cwr instance show "$ID"
cwr instance state "$ID"
```

```text
{
  "build": "sha256:b3b9d2f3...",
  "id": "01M6RJAB...",
  "seed": 7,
  "status": "idle",
  "tail_size": 20,
  "turn": 0,
  "world": {"name": "refworld", "version": "0.2.0"},
  "world_time": 0,
  ...
}
{"fs":{},"jobs":{},"kv":{},"publish":{},"search":{},"world":{}}
```

| Option | Meaning |
|---|---|
| `--seed N` | Seed of the Instance's random stream |
| `--tail K` or `--tail full` | Ledger records shown to the model each turn. Default 20. |
| `--initial <state.json>` | Initial state, a JSON object of module states |
| `--from-snapshot <file>` | Start from a snapshot, with its seed, state, clock and turn |
| `--arm <label>`, `--model <name>` | Labels written to the ledger header for experiments |
| `--allow-experimental` | Required for an experimental Build |

## Run a turn

`cwr instance turn` reads a script from `--script <file>` or from standard input. A top-level
`return` becomes the turn's `returned` value.

```sh
cwr instance turn "$ID" <<'SCRIPT'
import fs, world

fs.write(path="/notes/a.md", content=b"hello")
fs.lock(path="/notes/a.md")
return {"size": fs.size(path="/notes/a.md"), "now": world.now()}
SCRIPT
```

The kernel prints one result envelope as JSON on standard output. On standard error it prints one
line that names the sandbox in use.

```text
{"error":null,"resources":{"adapter_calls":0,"cpu_ms":30,"primitive_calls":4,"remaining":{"adapter_calls":50,"primitive_calls":496},"wall_ms":8496},"returned":{"now":3,"size":5},"status":"ok","trace":[{"code":null,"content_guaranteed":true,"primitive":"fs.write","status":"ok","world_time":1},...],"world":{"build":"sha256:b3b9d2f3...","time":14,"turn":1,"version":"0.2.0"}}
```

A refused call raises an exception in the script. Uncaught, it ends the turn:

```sh
cwr instance turn "$ID" <<'SCRIPT'
import fs

fs.delete(path="/notes/a.md")
SCRIPT
```

```text
{"error":{"code":"LOCKED","detail":"precondition 2 `not fs.locked(path)` was false","limit":null,"precondition_index":2,"primitive":"fs.delete","retry_after":null},...,"status":"error",...}
```

| Status | Meaning |
|---|---|
| `ok` | The script finished without an uncaught exception |
| `error` | An uncaught exception or a limit ended the turn, and nothing changed |
| `partial` | An uncaught exception or a limit ended the turn after at least one mutating or external call succeeded. Those effects stand. |

The command exits 0 for all three statuses. A non-zero exit means the kernel did not run the turn.

### Model messages

A model answers with a message, not a bare script. `--message <file>` applies the turn protocol:
the message must hold exactly one fenced `python` block.

```sh
printf '%s\n' 'I will unlock the file and delete it.' '' '```python' 'import fs' '' \
  'fs.unlock(path="/notes/a.md")' 'fs.delete(path="/notes/a.md")' \
  'return fs.exists(path="/notes/a.md")' '```' > reply.md
cwr instance turn "$ID" --message reply.md
```

A message with no block, more than one block or another language is refused with `INVALID`
naming `turn.protocol`. Nothing runs, world time does not advance and the command exits 4:

```text
{"error":{"code":"INVALID","detail":"the message holds 2 fenced blocks (opening fences at lines 3, 7); a turn is exactly one fenced python block, nothing was executed","limit":null,"precondition_index":null,"primitive":"turn.protocol","retry_after":null},...}
```

## What the model sees

`cwr instance context` prints the context for the next turn exactly as the model receives it: the
manual, a pending recompile notice, the ledger tail and the last result envelope. `--json` prints
the offset, length and sha256 of each part instead. Repeating the command prints the same bytes.

```sh
cwr instance context "$ID" --json
cwr instance tail "$ID"
```

```text
{"components":[{"length":31918,"name":"manual",...},{"length":451,"name":"tail",...},{"length":600,"name":"envelope",...}],"format":"cwr.context",...,"next_turn":4,...}
t1   wt1    fs.write             ok
t1   wt2    fs.lock              ok
t1   wt3    fs.size              ok
t1   wt4    world.now            ok
t1   turn                        ok     calls=4
t2   wt14   fs.delete            error  LOCKED
t2   turn                        error  LOCKED  calls=1
t3   wt25   fs.unlock            ok
t3   wt26   fs.delete            ok
t3   wt27   fs.exists            ok
t3   turn                        ok     calls=3
```

## Sandbox and limits

Each turn runs in a fresh Python 3.12 process inside bubblewrap, with new user, mount, network,
PID, IPC, UTS and cgroup namespaces and resource limits on address space, CPU time, file size,
open files and core dumps. A guard inside the interpreter admits only the Build's SDK. The script
has no network and no clock. Every primitive call crosses one RPC boundary to the kernel.

The Build's limits apply per turn. A call beyond `primitive_calls` or `adapter_calls` is not
executed and fails with `LIMIT` naming the limit. CPU, wall-clock and memory limits end the turn.
[crates/cwr-kernel/SANDBOX.md](../../crates/cwr-kernel/SANDBOX.md) describes each layer.

| Variable | Effect |
|---|---|
| `CWR_HOME` | Instance store. Default `.cwr`. |
| `CWR_PYTHON` | Interpreter for turn scripts. Default `python3.12`. |
| `CWR_BWRAP` | bubblewrap binary. Default `bwrap` on `PATH`. |

## Read the ledger

`cwr instance ledger` checks sequence numbers, the hash chain and truncation, and prints a summary.
A failed check is an error.

```sh
cwr instance ledger "$ID"
```

```text
{"last_chain":"sha256:...","last_seq":11,"ok":true,"records":12}
```

The ledger is `ledger.jsonl` in the Instance directory, one JSON record per line. Every record
carries `seq`, `kind`, `turn`, `world_time`, `build` and `chain`, the hash that links it to the
record before. A primitive record also carries the primitive, a hash of its parameters, the
outcome and the result of each precondition evaluated:

```text
{"build":"sha256:b3b9d2f3...","chain":"sha256:...","kind":"primitive","outcome":{"code":"LOCKED","shape":"None","side_effect":"mutating","status":"error"},"params_hash":"blake3:...","preconditions":[{"expr_hash":"sha256:...","index":1,"passed":true},{"expr_hash":"sha256:...","index":2,"passed":false}],"primitive":"fs.delete","seq":6,"turn":2,"world_time":14,...}
```

| Kind | Written when |
|---|---|
| `header` | The Instance is created. Holds the seed, initial state hash, tail size, arm and model labels. |
| `primitive`, `adapter` | A call is executed or refused |
| `turn` | A turn ends. Holds its status, code and resources. |
| `proposal` | The model calls `world.propose` |
| `upgrade` | The Instance moves to another Build |
| `drift` | An experimental drift Build changes a rule |
| `recovery` | The Instance is restored or recovered |

Records hold hashes of parameters and returned values, not the values. The state lives in the
Instance's content-addressed object store.

## Call an outside service

The adapter gateway runs in one of three modes, chosen by the operator per turn:

| Mode | Behaviour |
|---|---|
| `live` | Calls the declared endpoint. The default. |
| `record` | Calls the endpoint and appends each raw response to a cassette |
| `replay` | Answers from the cassette. A request with no entry is `UNAVAILABLE`, and no socket is opened. |

refworld's adapters point at `stub://` endpoints, served by the stub servers in
[benchmark/stubs](../../benchmark/stubs/README.md). Start the search stub and record a turn:

```sh
python3 "$REPO/benchmark/stubs/search_server.py" --port 0 --seed 7 --port-file search.port > search.log &
STUB=$!
until [ -s search.port ]; do sleep 0.2; done
SID=$(cwr instance create "$BUILD" --seed 7)
cwr instance turn "$SID" --gateway record --cassette search.jsonl --stub "search=http://127.0.0.1:$(cat search.port)" <<'SCRIPT'
import search

hits = search.query(q="tennis", limit=3)
return [h["title"] for h in hits]
SCRIPT
kill "$STUB"
```

```text
{"error":null,"resources":{"adapter_calls":1,...},"returned":["Report Basin Birch","Prairie Wren Willow","Fjord Drift Valley"],"status":"ok","trace":[{"code":null,"content_guaranteed":false,"primitive":"search.query","status":"ok","world_time":1}],...}
```

`content_guaranteed: false` marks every adapter result: the shape is guaranteed, the content is
not. With the stub stopped, a new Instance gets the same answer from the cassette:

```sh
RID=$(cwr instance create "$BUILD" --seed 7)
cwr instance turn "$RID" --gateway replay --cassette search.jsonl <<'SCRIPT'
import search

hits = search.query(q="tennis", limit=3)
return [h["title"] for h in hits]
SCRIPT
```

A request the cassette does not hold fails with `UNAVAILABLE` and the detail `replay cassette has
no entry for this request; no socket was opened`. The cassette format is specified in
[benchmark/cassettes/FORMAT.md](../../benchmark/cassettes/FORMAT.md).

## Replay and compare

`cwr instance replay` recreates an Instance from its seed, initial state, Build, recorded turn
scripts and cassette, re-runs every turn and compares the new ledger with the original. Host
timestamps, durations and the Instance id are ignored.

```sh
cwr instance replay "$SID" --cassette search.jsonl --into-home replayed
```

```text
{"divergence":null,"identical":true,"ignored_fields":["host_ts","duration_us","instance"],"into":"01M6RJJ2...","original":"01M6RJHR...","records":3,"state_digest":"sha256:504be005...","turns":1}
```

It exits 0 when the ledgers are identical and 7 on the first divergence, which it prints. Replay
reproduces script turns. It refuses a ledger that holds a probe turn, a refused message turn or a
lifecycle record such as an upgrade.

`cwr instance compare <ledger-a> <ledger-b>` compares two ledgers field by field with the same
rules and the same exit codes.

## Snapshots and recovery

A snapshot captures state, clock, turn and random stream under a content hash.

```sh
SNAP=$(cwr instance snapshot "$ID")
cwr instance restore "$ID" "$SNAP"
```

`restore` rolls the Instance back and appends a `recovery` record. The records after the snapshot
are retained in the Instance directory, not deleted.

A snapshot file can also start a new Instance:

```sh
cwr instance create "$BUILD" --from-snapshot ".cwr/instances/$ID/snapshots/${SNAP#sha256:}.json"
```

After a kernel crash, the next turn recovers the Instance before it runs. `cwr instance recover`
does it on demand and reports `{"recovered": false}` when nothing needed recovery.

```sh
cwr instance recover "$ID"
```

## Upgrade an Instance

An Instance moves to a new Build only when an operator upgrades it. The upgrade snapshots the
Instance, runs the target Build's migrations, checks the new state against the target's state
shapes and commits. Any failure rolls the Instance back to its old Build.

The upgrade pair U1 in [examples/refworld-upgrades](../../examples/refworld-upgrades/README.md)
adds a rule: from 0.3.0, `fs.delete` refuses a locked file. Build both sides into their own Build
root and create an Instance on the old side:

```sh
cwr build "$REPO/examples/refworld-upgrades/U1/A" --out u1
OLD=$(ls u1)
cwr build "$REPO/examples/refworld-upgrades/U1/B" --out u1 --previous "$OLD"
NEW=$(ls u1 | grep -v "$OLD")
UP=$(cwr instance create "u1/$OLD")
cwr instance turn "$UP" <<'SCRIPT'
import fs

fs.write(path="/tmp/x", content=b"x")
fs.delete(path="/tmp/x")
SCRIPT
```

Upgrade it. The command prints the upgrade record and the recompile notice. `cwr instance notice`
prints what the model will see on its next turn:

```sh
cwr instance upgrade "$UP" "u1/$NEW"
cwr instance notice "$UP"
```

```text
WORLD RECOMPILED  0.2.0 -> 0.3.0   (sha256:a4e291c7... -> sha256:39c6e259...)
Summary: fs.delete now refuses a locked path with LOCKED; unlock the path before deleting it.

Changes affecting primitives this instance has used:
  changed  fs.delete
    before  fs.delete removed a locked file and released its lock.
    after   not fs.locked(path), otherwise LOCKED (precondition 2); unlock the path before deleting it.

Your ledger is intact. The manual above describes 0.3.0.
```

The notice lists only changes to primitives the Instance has used. After the next turn it is no
longer pending.

The kernel refuses, with exit code 5 and no change, a target of another world, a version that is
not strictly greater, the Build already bound, and an experimental Build without
`--allow-experimental`:

```text
$ cwr instance upgrade "$UP" "u1/$OLD"
cwr instance upgrade: upgrade refused: target version 0.2.0 is not greater than the running version 0.3.0
```

A failed migration or commit exits 6 after rollback. Pair U5 shows a migration: its upgrade
record lists `migrations/0_2_0_0_3_0_kv_namespaces.py` and moves every key into the `default`
namespace.

## The proposal outbox

The model cannot change a rule. It can write to the owner with `world.propose`, which returns a
proposal id and changes nothing inside the Build. The owner reads the outbox:

```sh
cwr instance turn "$UP" <<'SCRIPT'
import world

return world.propose(text="Allow fs.delete to release a lock, as in 0.2.0.")
SCRIPT
cwr instance outbox "$UP"
```

```text
{"id":"p2.1","text":"Allow fs.delete to release a lock, as in 0.2.0.","turn":2}
```

## Destroy an Instance

`destroy` releases the state and seals the ledger. The ledger stays readable and verifiable. A
later turn is refused.

```sh
cwr instance destroy "$RID"
cwr instance ledger "$RID"
```

```text
destroyed 01M6RJ... (ledger sealed and retained)
```

## Command reference

| Command | Purpose | Exit codes |
|---|---|---|
| `cwr instance create` | Create an Instance | 0; 2 Build hash mismatch; 3 experimental Build without the flag |
| `cwr instance show`, `state`, `tail` | Print metadata, state or the model's ledger tail | 0; 1 error |
| `cwr instance ledger` | Check and summarise the ledger | 0; 1 tampered or truncated |
| `cwr instance turn` | Run one turn | 0 for `ok`, `error` and `partial`; 1 kernel error; 2 hash mismatch; 3 experimental; 4 protocol refusal |
| `cwr instance probe <id> <call>...` | Run a kernel-direct turn of `world` read calls, for diagnostics | 0; 1 error |
| `cwr instance context` | Print the next turn's context | 0; 1 error; 2 hash mismatch |
| `cwr instance snapshot`, `restore` | Snapshot and roll back | 0; 1 error; 2 hash mismatch |
| `cwr instance recover` | Recover after a crash | 0; 1 error |
| `cwr instance replay` | Re-run and compare | 0 identical; 7 divergence; 2 hash mismatch; 1 refused |
| `cwr instance compare` | Compare two ledgers | 0 identical; 7 divergence |
| `cwr instance upgrade` | Move to another Build | 0; 5 refused; 6 rolled back |
| `cwr instance notice` | Print a pending recompile notice | 0 |
| `cwr instance outbox` | Print the proposal outbox | 0 |
| `cwr instance destroy` | Seal the ledger and release state | 0 |

| Turn option | Meaning |
|---|---|
| `--script <file>` | Read the script from a file instead of standard input |
| `--message <file>` | Read a model message and apply the turn protocol |
| `--gateway live\|record\|replay` | Adapter gateway mode |
| `--cassette <file>` | The Instance's cassette, for `record` and `replay` |
| `--cassette-label <label>` | Header label of a new cassette |
| `--stub NAME=URL` | Base URL of a `stub://NAME` endpoint |
| `--debug-effects` | Assert every declared effect after each successful mutating call |

The normative contract is [spec/cwr/kernel.md](../../spec/cwr/kernel.md) for the kernel and
[spec/cwr/model-surface.md](../../spec/cwr/model-surface.md) for what the model receives.

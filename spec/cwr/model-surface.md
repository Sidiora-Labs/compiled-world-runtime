<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Model surface: context, manual, turn protocol and result envelope

Status: proposed contract; no kernel or compiler exists and every case is UNRUN. Requirement 9 in [spec.kvx](spec.kvx) applies, together with the manual generation rules in [compiler.md](compiler.md) and the turn semantics in [kernel.md](kernel.md). This document specifies what the model sees and emits; it establishes nothing about how any model behaves when it sees it.

## 1. Context composition

The context is the whole interface. There is no system prompt other than the manual, and nothing reaches the model except the four components below plus what its own scripts return through `world.history`.

| Order | Component | Source | Stability |
|---|---|---|---|
| 1 | Manual | `manual.md` from the Build, copied byte for byte | Byte-identical across every turn of every Instance of one Build; a cache-stable prefix |
| 2 | Recompile notice | Stored by `cwr instance upgrade`, rendered once | Present only on the first turn after an upgrade; absent otherwise |
| 3 | Ledger tail | Last K ledger records rendered one per line | Changes every turn; K fixed per Instance |
| 4 | Last result envelope | The previous turn's envelope as JSON | Changes every turn; absent on turn 1 |

The assembler concatenates these with fixed separators and never edits the manual bytes. Because the manual is first and unchanged, the prefix shared by all turns is identical, which is what H3 in [experiments.md](experiments.md) relies on when it varies only K. Experimental `--x-silent-change` and `--x-version-only` Builds alter component 2 only.

## 2. The manual

The compiler generates the manual from `contracts.json` and `manual/`; it is a pure function of the Build and is never edited by hand. Its ten sections appear in this order.

| Section | Content |
|---|---|
| 1. What this world is | World name, version, the sha256 of `contracts.json` (the manual is inside the Build hash, so it cannot state that hash; `world.build` in every result envelope reports it), the intro paragraph from `manual/intro.md`, and the three pillars (stability, direct action, controlled change) in fixed wording |
| 2. How a turn works | One fenced python block per turn; primitives are atomic; scripts are not; the envelope; partial status; limits |
| 3. The failure envelope | The eleven codes verbatim from the Build with one-line meanings and the `retry_after` rule for `RATE_LIMITED` |
| 4. Limits and clock | Every limit from `world.kvx` with its value; `tick_per_primitive`, `tick_per_turn`; that time is ticks; the seed if visible |
| 5. Modules overview | One paragraph per module and its state shape |
| 6. Primitive reference | One entry per read_only or mutating primitive, grouped by module, in the rendered entry format below |
| 7. Adapters | One entry per adapter with idempotency, timeout, declared failures and the content-not-guaranteed label |
| 8. Changes and versions | The full `CHANGELOG.kvx` rendered newest first, with the statement that rules change only by recompilation into a new version |
| 9. Proposals | How to call `world.propose` and that it has no effect within this Build |
| 10. Index | Alphabetical list of every primitive and adapter with its section anchor |

Section 3 carries the sentence the world makes to the model about determinism: the same primitive under the same visible conditions always produces the same outcome; outcome means status, shape and side-effect class; it does not mean content.

A primitive entry as rendered:

```text
fs.delete(path: Path) -> None                                   [mutating]
  Remove the file at path. Refused when the path does not exist or is locked.
  Requires   1. fs.exists(path)              else NOT_FOUND
             2. not fs.locked(path)          else LOCKED
  Ensures    not fs.exists(path)

publish.post(key: str, title: str, body: str) -> PostReceipt    [external, idempotency: key, timeout 5000 ms]
  Publish a post under an idempotency key. ...
  Requires   1. len(title) <= 200             else INVALID
  May fail   DUPLICATE_KEY, RATE_LIMITED, UNAVAILABLE, TIMEOUT
  Content is not guaranteed: an ok result guarantees the PostReceipt shape and that one external post
  was attempted, not what the service stored.
```

Preconditions are numbered in evaluation order and the numbers are the `precondition_index` the envelope reports. Under `--x-one-primitive` section 4 additionally states `primitive_calls = 1`. Under `--x-drift` the manual is deliberately unchanged; that is the ablation.

## 3. Turn protocol

The model emits exactly one fenced `python` block. Text outside the block is ignored by the kernel and not recorded. A top-level `return` is permitted and its value becomes `returned` in the envelope. Scripts may define functions and loop; the kernel enforces limits, not structure.

| Turn content | Kernel action |
|---|---|
| Exactly one fenced python block | Executed as the turn's script |
| No fenced python block | Refused as `INVALID` with `primitive = "turn.protocol"`; nothing executes; `world_time` does not advance; a `turn` record with status `error` is appended |
| Two or more fenced python blocks | Same refusal; the kernel does not pick one |
| A fenced block with a language other than python | Same refusal |

A valid turn:

````text
```python
import fs, kv, search, publish, world

hits = search.query(q="tennis", limit=5)
fs.write(path="/notes/tennis.md", content=hits[0]["snippet"].encode())
receipt = publish.post(key="t-1", title=hits[0]["title"], body="see notes")
kv.set(key="last_receipt", value=receipt["id"])
return receipt
```
````

An invalid turn is any message that, after fence extraction, yields zero or more than one python block, for example a message that explains a plan in one block and acts in a second. The refusal is itself in the ledger tail, so the model sees why nothing happened.

## 4. Result envelope

Always the same shape. Field rules are in section 5 of [kernel.md](kernel.md).

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

An `ok` turn:

```json
{"status": "ok", "returned": {"id": "p_91ad"}, "error": null,
 "trace": [{"primitive": "search.query", "status": "ok", "code": null, "world_time": 413},
           {"primitive": "fs.write", "status": "ok", "code": null, "world_time": 414},
           {"primitive": "publish.post", "status": "ok", "code": null, "world_time": 415},
           {"primitive": "kv.set", "status": "ok", "code": null, "world_time": 416}],
 "world": {"version": "0.2.0", "build": "sha256:ab12...", "time": 426, "turn": 38},
 "resources": {"cpu_ms": 41, "wall_ms": 120, "primitive_calls": 4, "adapter_calls": 2, "remaining": {"primitive_calls": 496, "adapter_calls": 48}}}
```

An `error` turn (the first mutating call was refused at a precondition, so nothing changed):

```json
{"status": "error", "returned": null,
 "error": {"code": "LOCKED", "primitive": "fs.delete", "precondition_index": 2, "retry_after": null, "detail": "not fs.locked(path) was false"},
 "trace": [{"primitive": "fs.exists", "status": "ok", "code": null, "world_time": 427},
           {"primitive": "fs.delete", "status": "error", "code": "LOCKED", "world_time": 427}],
 "world": {"version": "0.2.0", "build": "sha256:ab12...", "time": 437, "turn": 39},
 "resources": {"cpu_ms": 9, "wall_ms": 30, "primitive_calls": 2, "adapter_calls": 0, "remaining": {"primitive_calls": 498, "adapter_calls": 50}}}
```

A `partial` turn (a mutation succeeded, then an adapter was rate limited and the exception was not caught):

```json
{"status": "partial", "returned": null,
 "error": {"code": "RATE_LIMITED", "primitive": "publish.post", "precondition_index": null, "retry_after": 30, "detail": "service signalled rate limit"},
 "trace": [{"primitive": "fs.write", "status": "ok", "code": null, "world_time": 438},
           {"primitive": "publish.post", "status": "error", "code": "RATE_LIMITED", "world_time": 439}],
 "world": {"version": "0.2.0", "build": "sha256:ab12...", "time": 449, "turn": 40},
 "resources": {"cpu_ms": 12, "wall_ms": 5020, "primitive_calls": 2, "adapter_calls": 1, "remaining": {"primitive_calls": 498, "adapter_calls": 49}}}
```

The file written in the partial turn exists in the next turn; the trace says so, and the model is not expected to guess.

## 5. Recompile notice

Rendered once, as component 2, on the first turn after `cwr instance upgrade`. The stored notice is the same in every Build; only the rendering differs under experimental flags.

Full rendering (control Build and the H4 `changelog` arm):

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

| Rendering | Build | Component 2 content |
|---|---|---|
| Full | Control, H4 `changelog` arm | Header, summary, every affecting change, migration statement |
| Version only | `--x-version-only` | The header line alone |
| Silent | `--x-silent-change` | Nothing; the manual is the new Build's manual but no notice says so |

"Affecting" means the change's target appears in this Instance's ledger as a used primitive or module; changes to primitives never used are listed only in manual section 8.

## 6. Ledger tail and history

Component 3 renders the last K ledger records of kinds `primitive`, `adapter`, `turn`, `drift` and `proposal`, one line each, oldest first:

```text
t36  wt410  fs.delete            error  LOCKED
t37  wt412  fs.unlock            ok
t37  wt413  fs.delete            ok
t37  turn                        ok     calls=2
t38  wt415  publish.post         error  RATE_LIMITED  retry_after=30
```

K is a per-Instance parameter set at `create --tail K`, default 20, recorded in the header record and in every snapshot. H3 runs K in {5, 20, full}, where `full` renders every record. The parameters of calls are not rendered in the tail; `params_hash` is, so the model can correlate with `world.history`.

`world.history(n)` is a read_only primitive returning the last `n` ledger records as codec values, including `params_hash`, `preconditions` and `outcome`, and excluding `host_ts` and `duration_us`. The model therefore never needs anything outside the world to remember what it did; what it is shown by default is K lines, and what it can query is everything.

## 7. Visibility boundary

| Visible to the model | Not visible to the model |
|---|---|
| The manual | Kernel internals, debug effect assertions, sealing reasons |
| The recompile notice | Host time, host identity, process identity, `host_ts`, `duration_us` |
| The ledger tail and `world.history(n)` | Other Instances and their ledgers |
| The result envelope including the trace | The outbox file and whether anyone read it |
| Every read_only primitive, including `world.limits`, `world.now`, `world.seed` when visible | Cassettes, gateway mode, stub server state |
| Section 8 of the manual (changelog) | The experiment harness, the arm label, seeds of other runs |

Everything in the right column is either outside the world or is kernel machinery; a Build in which any of it leaks through a primitive violates closure and is a CWR-007 failure.

## 8. Interleavings

| Interleaving | Required observable result |
|---|---|
| Upgrade between turn n and n+1 | Turn n+1 context: new manual, full notice (control), tail unchanged, envelope of turn n; `world.version` in the envelope of turn n+1 is the new version |
| Drift injection at turn n (`--x-drift`) | Manual byte-identical before and after; a `drift` line appears in the tail only if the drift grammar marks it visible, otherwise the first visible evidence is a failure code the manual does not predict; the ledger records the injection either way |
| Partial turn n, then turn n+1 | Turn n+1 envelope slot shows status `partial` with the trace; state reflects the successful mutations; nothing was rolled back |
| K smaller than the number of records | The tail shows the most recent K; earlier records remain reachable through `world.history` |
| Protocol-invalid turn n | Turn n+1 context: tail contains the `turn.protocol` refusal line; `world.time` equals the value after turn n-1 |
| `--x-one-primitive` turn with two calls | Trace shows the first call `ok` and the second `LIMIT primitive_calls`; status `partial` if the first was mutating or external |

## 9. Required future evidence

- CWR-012 (context composition and turn protocol): manual prefix byte-identical across 50 turns and across Instances; the ten sections and every primitive entry present; a two-block turn refused with no execution record; the only context sources are the four components plus `world.history`.
- CWR-013 (experimental builds): the three notice renderings observed on the same stored notice; drift fires at the scheduled turn with an unchanged manual; the one-primitive manual states its limit.

Until those attempts exist in [verification.md](verification.md) and [qualification.kvx](qualification.kvx), this surface is a design, not a description of running software.

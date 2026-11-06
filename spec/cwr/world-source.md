<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# World Source and rule contracts

Status: proposed contract; no compiler exists and every case is UNRUN. Requirements 2 and 3 in [spec.kvx](spec.kvx) apply. The grammar below is what the compiler in [compiler.md](compiler.md) is proposed to accept; nothing in this document has been parsed by an implementation. The illustrative source under [examples/refworld](examples/refworld/world.kvx) passes only the structural checks of the authoring validator.

## 1. What the World Source is

The World Source is the only layer a human edits. Everything the model experiences, the manual, the SDK, the kernel's enforcement table and the recompile notice, is derived from it by the compiler. The source therefore has to carry three things that are normally scattered across a harness: what actions exist, what rule governs each action, and what changed since the previous version.

A rule is a precondition with a named failure. The chair example: "a chair cannot be moved while someone sits on it" becomes a precondition `not room.occupied(chair)` with failure `LOCKED` on the primitive `room.move`. The same text is rendered in the manual, evaluated by the kernel and named in the failure envelope. There is no second place where the rule could be stated differently.

Every file is KVX: sections in brackets, one key per line, one JSON literal per value, `#` comments. The grammar is deliberately small so the compiler can reject anything unknown rather than guess. Unknown files, sections or keys are compile errors (clause 3.1, lint CWR-C001), not warnings.

## 2. Source layout

| Path | Purpose | Edited by | Compiled into |
|---|---|---|---|
| `world.kvx` | Identity, version, language, runtime, module list, limits, clock, RNG visibility | World author | `manifest.kvx`, limits in `contracts.json` |
| `CHANGELOG.kvx` | One release section per version with its change sections | World author, per release | `contract_diff.json` coverage, recompile notice |
| `lib/<module>.py` | Kernel-side implementation of each primitive against the kernel state API | World author | `lib/` in the Build, executed by the kernel, never by the sandbox |
| `contracts/<module>.kvx` | Rule contracts for the module's primitives | World author | `contracts.json`, manual entries, SDK stubs, enforcement table |
| `adapters/<module>.kvx` and `adapters/<module>/live.py` | Adapter contracts and gateway-side implementation | World author | `contracts.json`, `adapters/` in the Build |
| `migrations/<from>_<to>_<name>.py` | Pure state functions between Builds | World author, when state shape changes | `migrations/` in the Build, applied by `cwr instance upgrade` |
| `fixtures/<module>/*.kvx` | Initial states and adapter responses that reach every declared failure | World author | `fixtures/` in the Build, consumed by contract tests |
| `manual/*.md` | Hand-written prose sections merged into the generated manual | World author | `manual.md` |

The model never sees this directory. It sees the Build. A source edit without `cwr build` has no effect on any Instance (clause 5.1); task 2.5 is proposed to prove that.

## 3. world.kvx

| Section.key | Type | Rule |
|---|---|---|
| `meta.format` | string | Must be `"cwr.world-source"`. |
| `meta.schema_version` | integer | Must be `1` for this grammar. |
| `meta.example` | boolean | Optional. `true` marks an illustrative file that is not a compiled world; the compiler refuses to build a source whose `meta.example` is `true` unless `--allow-example` is passed, so that a pack example can never become a Build by accident. |
| `world.name` | string | Lowercase identifier. The kernel refuses an upgrade between Builds with different names (clause 5.5). |
| `world.version` | string | Semantic version `MAJOR.MINOR.PATCH`. Must equal the newest release in `CHANGELOG.kvx` and must satisfy the derived minimum bump (clause 4.4). |
| `world.language` | string | `"python"` is the only value this grammar defines. |
| `world.runtime` | string | `"3.12"`; the kernel refuses a Build whose runtime it cannot provide. |
| `world.modules` | list of strings | Every module that has a `contracts/` or `adapters/` file; a contract file for an undeclared module is CWR-C003. |
| `limits.cpu_ms` | positive integer | CPU budget per turn script. |
| `limits.wall_ms` | positive integer | Wall budget per turn script, host time. |
| `limits.memory_mb` | positive integer | Resident memory budget for the sandbox process. |
| `limits.primitive_calls` | positive integer | Primitive calls per turn, counted at the RPC boundary. |
| `limits.adapter_calls` | positive integer | External calls per turn, counted at the gateway. |
| `clock.tick_per_primitive` | positive integer | World time advanced per primitive call, success or failure. |
| `clock.tick_per_turn` | positive integer | World time advanced at the end of each turn. |
| `rng.seed_visible` | boolean | Whether `world.seed()` is a primitive. The seed is always recorded in the ledger header. |

There are no defaults. An omitted limit, clock or RNG key is a compile error (clause 3.4). The reason is visibility: a limit the model can hit must be printed in the manual, and a value the author never wrote cannot be presented as a rule the author chose.

## 4. Primitive contracts

A contract file declares one module and one section per primitive.

| Key | Type | Rule |
|---|---|---|
| `meta.module` | string | Must be in `world.modules`; every primitive in the file is named `<module>.<name>`. |
| `side_effect` | string | `read_only` or `mutating`. `external` is reserved for adapter contracts. |
| `params` | object of name to type | Ordered; the SDK stub uses the same order. Types are the closed set `str`, `int`, `bool`, `bytes`, `Path`, `None`, `list[T]`, `dict[str, T]` and record names declared in `manual/types.md`. |
| `returns` | string | A type from the same set. |
| `preconditions` | list of `[expr, failure]` pairs | Evaluated in order; see section 5. `[]` is allowed and means the primitive never fails after parameter validation. |
| `effects` | list of expressions | Postconditions over read_only primitives. Mutating primitives declare at least one; read_only primitives declare none (clause 3.2). |
| `doc` | string | One or two sentences rendered verbatim in the manual. |

Example, from [examples/refworld/contracts/fs.kvx](examples/refworld/contracts/fs.kvx):

```text
[primitive.fs.delete]
side_effect = "mutating"
params = {"path": "Path"}
returns = "None"
preconditions = [["fs.exists(path)", "NOT_FOUND"], ["not fs.locked(path)", "LOCKED"]]
effects = ["not fs.exists(path)"]
doc = "Remove the file at path. Refused when the path does not exist or is locked."
```

Shape rules the compiler enforces before anything else:

- A primitive name appears once across all contract files (CWR-C002).
- Every primitive in `contracts/` has an implementation in `lib/` with the same name and parameter list, and every public function in `lib/` has a contract (CWR-L002). The SDK is generated, so there is no third copy of the signature.
- A read_only primitive that declares effects, or a mutating primitive that declares none, is rejected (CWR-C001 shape error, located at the section).
- A precondition's failure code is a member of the envelope in section 6 (CWR-F001).

## 5. Precondition expression language

Preconditions are the visible half of every rule. The language is restricted so that every term is something the model can evaluate itself before acting (clauses 2.1 and 2.6).

| Term | Allowed | Notes |
|---|---|---|
| Parameter names | yes | The primitive's own `params`, by name. |
| Calls to read_only primitives of the same Build | yes | `fs.exists(path)`, `fs.size(path)`. Arguments are parameters or literals. |
| `len(x)` | yes | On `str`, `bytes`, lists and dicts. |
| Comparison `== != < <= > >=` | yes | Between values of equal type. |
| Boolean `and`, `or`, `not`, parentheses | yes | Short-circuit, left to right. |
| Integer and string literals | yes | JSON literals only. |
| Calls to mutating or external primitives | no | CWR-V001. A rule that could only be checked by acting is not a visible rule. |
| Any other name, attribute, index, host value or clock | no | CWR-V002. |

Evaluation rules the kernel is proposed to follow (clause 2.2):

1. Parameters are validated against `params` first; a type or missing-argument error is `INVALID` and no precondition runs.
2. Preconditions are evaluated in declared order. The first false expression stops evaluation and the call returns its failure code with the primitive name and the one-based precondition index.
3. A read_only call inside a precondition does not advance world time and is not written to the ledger as a separate record; it is listed in the failing record's `preconditions` field so a reader can see what was checked.
4. If every precondition holds, the primitive executes. Effects are asserted after execution in debug mode and by contract tests always.

Every precondition names exactly one failure code. Two preconditions may name the same code; a model that sees `LOCKED` then has at most the listed expressions to re-check.

## 6. Failure envelope

The envelope is closed. Eleven codes exist; a primitive, adapter or kernel path that needs another code is a specification change, not a local addition (clause 2.3).

| Code | Meaning | Guidance rendered in the manual | `retry_after` |
|---|---|---|---|
| `NOT_FOUND` | The target the parameters name does not exist in the world. | Query the visible existence primitive first. | absent |
| `LOCKED` | The target is held by a lock the caller does not hold. | Unlock, or wait for the holder. | absent |
| `CONFLICT` | A visible condition changed between the caller's check and this call, or the action would create a duplicate world object. | Re-check the named conditions and retry once. | absent |
| `INVALID` | Parameters fail the contract's types, ranges or declared validity preconditions. | Fix the call; retrying unchanged cannot succeed. | absent |
| `LIMIT` | A declared limit was reached: a world limit in `world.kvx`, a size rule or the per-turn budgets. The envelope names the limit. | Split the work across turns or reduce the size. | absent |
| `UNAVAILABLE` | An external endpoint could not be reached, returned an undeclared status, or a replay cassette has no entry. | The world does not retry; the script may, on a later turn. | absent |
| `RATE_LIMITED` | An external endpoint throttled the call. | Wait `retry_after` ticks before calling the same adapter. | required, integer ticks |
| `TIMEOUT` | An external call exceeded the contract's `timeout_ms`. | The effect may or may not have happened; use the adapter's status primitive if it has one. | absent |
| `REJECTED` | An external endpoint refused the content for a reason it reported. | Change the content; `detail` carries the service's reason as content, not guaranteed. | absent |
| `DUPLICATE_KEY` | An idempotency key was reused with a different payload. | Reuse the original receipt or choose a new key. | absent |
| `INVARIANT_VIOLATION` | The kernel observed a rule outcome the contract does not allow: an undeclared code after preconditions passed, or an effect that did not hold. | Not an ordinary outcome. The ledger carries the trace and the Build is considered defective. | absent |

The envelope value is always the same shape:

```text
{"code": "RATE_LIMITED", "primitive": "publish.post", "precondition": null,
 "retry_after": 30, "limit": null, "detail": "<content, not guaranteed>"}
```

`precondition` is the one-based index when a declared precondition failed and `null` otherwise. `limit` is the limit name for `LIMIT`. `retry_after` is present only for `RATE_LIMITED`. `detail` is free text from the world or the service and is explicitly content, not interface.

## 7. Adapter contracts

An adapter is the only place the world touches anything outside itself. Its contract guarantees the interface and declares that it guarantees nothing about content.

| Key | Type | Rule |
|---|---|---|
| `meta.module` | string | Declared in `world.modules`. |
| `side_effect` | string | Must be `external`. |
| `params`, `returns`, `doc` | as for primitives | `returns` names a record shape; the gateway validates the shape, not the values. |
| `preconditions` | list of pairs | Same language as section 5; evaluated before any network call. |
| `idempotency` | string | `none`, `natural` or `key`; see the table below. |
| `timeout_ms` | positive integer | Host-time budget for the call; exceeding it is `TIMEOUT`. World time advances by `tick_per_primitive` regardless. |
| `endpoint` | string | The only destination the gateway may contact for this adapter. Illustrative sources use `stub://`. |
| `failures` | nonempty list of codes | Subset of the envelope. The gateway maps every response to one of these, `INVALID` for shape failures or the adapter's `undeclared_status` for a status it does not map; an adapter that omits `UNAVAILABLE` is rejected (CWR-F002) because undeclared transport outcomes would have nowhere to map. |
| `undeclared_status` | optional code | `UNAVAILABLE` (the default) or `INVALID`: the code a service status the adapter does not map falls back to. It must be one of `failures`. Transport errors are always `UNAVAILABLE`. The compiler's contract tests and the kernel gateway apply the same value. |
| `retry_after_ratio` | optional positive integer | World ticks per unit of the service's `retry_after` when it signals a rate limit (default 1). `RATE_LIMITED` carries the converted value in ticks. |
| `content_guaranteed` | boolean | Must be `false` (CWR-F002). The key exists so the manual can print the label from the contract rather than from a convention. |
| `effects` | forbidden | An adapter may not claim effects over world state; its only world-side effects are the ledger record and, for `key` adapters, the idempotency store. |

| `idempotency` | Gateway behaviour | What the manual tells the model |
|---|---|---|
| `none` | Every call is sent. No store. | Non-idempotent: a `TIMEOUT` may have had an effect. |
| `natural` | Every call is sent; the gateway records request and response hashes so the ledger shows that equal requests produced equal outcomes when they did. | Safe to repeat; equal requests are expected to have equal effects. |
| `key` | The first outcome per `(adapter, key)`, ok or a declared failure, is stored with the payload hash. An identical replay returns the stored outcome without a network call; a different payload under the same key is `DUPLICATE_KEY`. | Repeat with the same key to recover a lost receipt; never reuse a key for different content. |

Interface-guaranteed versus content-not-guaranteed, using [examples/refworld/adapters/search.kvx](examples/refworld/adapters/search.kvx): calling `search.query("tennis", 10)` is guaranteed to return either a list of at most ten `SearchHit` records or one of `INVALID`, `LIMIT`, `RATE_LIMITED`, `UNAVAILABLE` or `TIMEOUT`. That is the rule, and it holds for every Build of the world. Whether the hits are about tennis, or about golf, or empty, is content. The model is told this in the manual with the label `content not guaranteed`, and the result envelope repeats the label on every such result (clause 2.5). A world author who needs tennis results to be about tennis has no contract to express that, and that is intended.

Each adapter module also ships `adapters/<module>/live.py`, the gateway-side implementation. It may use only the gateway client; it has no socket of its own (clause 7.1).

## 8. lib/

`lib/<module>.py` implements the module's primitives. The code runs inside the kernel process, never in the sandbox, so it is trusted with state but not with the host.

- Each public function implements exactly one contract and takes the kernel state handle plus the declared parameters.
- Permitted imports: the kernel state API (`cwr_state`), `typing`, `dataclasses`, `json`, `re`, `math`, `collections` and `itertools`.
- Forbidden imports, each CWR-L001: `os`, `sys`, `socket`, `http`, `urllib`, `time`, `datetime`, `random`, `secrets`, `subprocess`, `pathlib`, `shutil`, `tempfile`, `threading`, `multiprocessing`, `ctypes`, `importlib` and any third-party package. The list is closed in the compiler; it is not a heuristic.
- A primitive that needs randomness takes it from `cwr_state.random()`, which is the Instance's PCG stream; one that needs time reads `cwr_state.world_time()`.
- A primitive may raise only envelope codes declared in its contract. Any other exception, or a declared-but-undeclared-for-this-primitive code, is `INVARIANT_VIOLATION` at runtime and a contract-test failure at build time.

## 9. fixtures/

Contract tests are generated from the contracts, but they need states to run against. For every primitive and adapter the author provides, in `fixtures/<module>/`:

- One initial state per declared precondition in which exactly that precondition fails while all earlier ones hold, so that each failure code is reachable in declared order.
- One initial state in which every precondition holds, so that effects can be asserted after an ok outcome.
- For adapters, one recorded response per declared failure and one success response, in cassette form keyed by canonical request hash.

A declared failure with no fixture fails the contract-test stage; the compiler does not search for a state on its own.

## 10. manual/

The manual is generated, but four of its ten sections are prose the generator cannot invent: the world's purpose, its conventions, its named types and worked examples. Those live as `manual/intro.md`, `manual/conventions.md`, `manual/types.md` and `manual/examples.md`. The generator merges them at fixed positions listed in [model-surface.md](model-surface.md). A prose file that names a primitive which does not exist is CWR-D001, so prose cannot describe a rule the contracts do not have.

## 11. CHANGELOG.kvx

| Section.key | Type | Rule |
|---|---|---|
| `release.<version>.version` | string | Equal to the section suffix. |
| `release.<version>.date` | string | ISO date. |
| `release.<version>.summary` | string | One human sentence; rendered first in the recompile notice. |
| `release.<version>.changes` | list of change ids | Each must exist; every change must belong to exactly one release. |
| `change.<id>.kind` | string | One of the kinds below. |
| `change.<id>.target` | string | A primitive, adapter or module name. For `primitive_removed` the name no longer exists in the new source and is checked against the previous Build instead. |
| `change.<id>.before` | string | Human description of the old rule; empty for additions. |
| `change.<id>.after` | string | Human description of the new rule. |
| `change.<id>.migration` | string | Path under `migrations/`; required and nonempty for `state_shape`, empty otherwise unless the author chooses to migrate anyway. |

| Kind | Meaning | Derived minimum bump |
|---|---|---|
| `primitive_added` | A primitive or adapter exists that did not. | minor |
| `primitive_removed` | A primitive or adapter no longer exists. | major |
| `precondition_added` | A primitive refuses something it used to accept. | major |
| `precondition_removed` | A primitive accepts something it used to refuse. | minor |
| `precondition_changed` | An expression or its failure code changed. | major |
| `effect_changed` | A declared postcondition changed. | major |
| `failure_changed` | An adapter's declared failure set changed. | major |
| `limit_changed` | A value in `limits` or `clock` changed. | major when stricter, minor when looser |
| `state_shape` | The shape of a module's state changed; a migration is required. | major |
| `adapter_changed` | `idempotency`, `timeout_ms`, `endpoint`, `undeclared_status` or `retry_after_ratio` changed (an absent key compares as its default). | major |
| `doc_only` | Only `doc` strings or `manual/` prose changed. | patch |

Illustration in [examples/refworld/CHANGELOG.kvx](examples/refworld/CHANGELOG.kvx): release 0.2.0 adds a size precondition to `fs.write` and namespaces `kv`. Both are major by the table, and the example is at 0.2.0 only because it is pre-1.0; the compiler's pre-1.0 rule treats a minor bump as satisfying a derived major bump, and that rule is itself printed in the diff output so nobody mistakes 0.2.0 for a compatible release.

## 12. Migrations

A migration is a pure function `migrate(state: dict) -> dict` in `migrations/<from>_<to>_<name>.py`, illustrated by [examples/refworld/migrations/0_2_0_kv_namespaces.py](examples/refworld/migrations/0_2_0_kv_namespaces.py).

- It receives the full prior Instance state as plain data and returns the full new state. It never mutates its argument.
- It has the same forbidden-import list as `lib/` and additionally may not import `cwr_state`: no clock, no RNG, no adapters, no ledger.
- It is idempotent: `migrate(migrate(s)) == migrate(s)`, which contract tests check against the fixtures.
- The kernel applies migrations inside a snapshot transaction during `cwr instance upgrade` (clause 5.2); an exception rolls the Instance back to the prior snapshot and Build.
- A migration never changes the ledger; the ledger records that the upgrade happened and which migrations ran.

## 13. Versioning

| Bump | When |
|---|---|
| major | A primitive or adapter is removed, any outcome changes for conditions that were previously visible and accepted, a limit tightens, a failure set changes, or state shape changes. |
| minor | Primitives, adapters, failure codes on new preconditions, or looser limits are added; every previously valid call keeps its outcome. |
| patch | Only prose changes; `contracts.json` is byte-identical to the previous Build's. |

The compiler derives the minimum bump from the ContractDiff and refuses a `world.version` below it (clause 4.4). A declared bump above the minimum is accepted. Version strings are compared numerically, never lexically.

## 14. Edge cases and required compiler result

| Situation | Required result |
|---|---|
| Two preconditions of one primitive are both false | The first in declared order is reported; the second is not evaluated or listed as failing. |
| A precondition references a primitive that this release removes | CWR-V002 at resolve; the removal is not silently applied. |
| A precondition references a mutating primitive | CWR-V001 at lint. |
| An adapter contract declares `effects` | CWR-C001 shape error; adapters may not claim world effects. |
| An adapter declares `content_guaranteed = true` or omits it | CWR-F002. |
| An adapter declares `failures` without `UNAVAILABLE` | CWR-F002; the gateway would have no declared code for undeclared transport outcomes. |
| `limits.memory_mb` omitted | Compile error at parse; no default is substituted. |
| A `lib/` module imports `time` | CWR-L001 with file and line. |
| A contract exists without a `lib/` function, or the reverse | CWR-L002. |
| A primitive is declared in two contract files | CWR-C002 naming both locations. |
| `contracts/jobs.kvx` exists but `jobs` is not in `world.modules` | CWR-C003. |
| A rule changes with no matching `change.<id>` in the newest release | Changelog gate refusal listing the uncovered ContractDiff entries. |
| `state_shape` change with `migration = ""` | CWR-M001. |
| Two `release.*` sections declare the same version | Parse error: duplicate section. |
| `world.version` equals the previous Build's version while `contracts.json` differs | Changelog gate refusal: derived bump is at least patch. |
| A fixture reaches no declared failure, or a declared failure has no fixture | Contract-test stage failure naming the primitive and code. |

## 15. Required future evidence

Nothing above is demonstrated. The following cases, both UNRUN, are the proposed evidence; their drivers do not yet exist.

- **CWR-001** (task 0.3) must show that the typed reader accepts the illustrative source, rejects each malformed fixture in section 14 with a located error, and that no value outside the eleven-code envelope can be constructed or parsed by the contract crate.
- **CWR-002** (task 1.1) must show that the resolve and lint stages stop the pipeline at the expected stage with the expected lint code for a hidden precondition, a host import, an unknown key, a duplicate primitive and an undeclared module, and that the clean source passes every stage up to lint.

A compiler that accepts this grammar is a prerequisite for every later case; acceptance of the grammar is not evidence that a kernel enforces it, which is the subject of [kernel.md](kernel.md).

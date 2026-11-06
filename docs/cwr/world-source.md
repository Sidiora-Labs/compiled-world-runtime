<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Writing a CWR world: World Source guide

Audience: world authors. Status: **pending verification**. As of 2026-11-06 implementation code for waves 0 to 2 is landing in the tree, but no qualification case that covers this guide has an executed attempt in the evidence registry, so nothing here is verified behaviour. This guide is derived from the contract chapter [spec/cwr/world-source.md](../../spec/cwr/world-source.md) and the command table in [spec/cwr/compiler.md](../../spec/cwr/compiler.md). Where this guide and the contract disagree, the contract wins and the difference is a defect in this guide.

## How to read this guide

Every section that describes what the compiler or kernel does carries a block like this one:

> **Pending verification.** Evidence: CWR-002 (driver `tests/cwr/cases/1_1.py`, no attempt recorded). Source in tree, unverified: `crates/cwr-compiler/src/lint.rs`.

The block names the qualification case that would show the behaviour, the driver its task creates and the source files that implement it. A source file listed as "in tree, unverified" exists but its behaviour has not been shown by an executed case; one listed as "proposed" has not been written. Paths stay in backticks rather than links while implementation is in flux. A block is removed only by task 6.1, and only when the named case has an executed attempt in the evidence registry; until then every statement in the section is the design contract, not a description of running software. Sections without such a block describe the files you write, not program behaviour.

Commands are shown exactly as the specification names them. A command shown here is not evidence that it exists or passes.

## 1. What you are writing

> **Pending verification.** Evidence: CWR-011 (driver `tests/cwr/cases/2_5.py`) for "a source edit changes nothing until rebuilt and upgraded", CWR-005 (driver `tests/cwr/cases/1_3.py`) for generation from one contract, no attempt recorded. Source in tree, unverified: `crates/cwr-compiler/src/generate/manual.rs`, `crates/cwr-compiler/src/generate/sdk.rs`.

The World Source is the only layer a human edits. The manual the model reads, the SDK its scripts import, the kernel's enforcement table and the recompile notice shown after an upgrade are all generated from it. A source edit changes nothing for any running Instance until you run `cwr build` and then explicitly upgrade the Instance.

A rule is a precondition with a named failure. "A chair cannot be moved while someone sits on it" becomes the precondition `not room.occupied(chair)` with the failure `LOCKED` on the primitive `room.move`. That one line is rendered in the manual, evaluated by the kernel and named in the failure envelope, so there is no second place where the rule could be stated differently.

Every file except Python code and manual prose is KVX: `[section]` headers, one `key = <JSON literal>` per line, `#` comments. Unknown files, sections or keys are errors, never warnings.

## 2. Source layout

| Path | What you put there |
|---|---|
| `world.kvx` | Identity, version, language, runtime, module list, limits, clock and RNG visibility |
| `CHANGELOG.kvx` | One release section per version and one change section per rule change |
| `contracts/<module>.kvx` | Rule contracts for the module's primitives |
| `lib/<module>.py` | Kernel-side implementation of each primitive |
| `adapters/<module>.kvx`, `adapters/<module>/live.py` | Adapter contracts and their gateway-side implementation |
| `migrations/<from>_<to>_<name>.py` | Pure state functions between Builds, when state shape changes |
| `fixtures/<module>/*.kvx` | Initial states and recorded adapter responses that reach every declared failure |
| `manual/intro.md`, `manual/conventions.md`, `manual/types.md`, `manual/examples.md` | Prose merged into the generated manual |

The illustrative source under [spec/cwr/examples/refworld](../../spec/cwr/examples/refworld/world.kvx) shows the grammar. It has `meta.example = true`, is not a release input and establishes no registered Build hash. The authored reference world for `examples/refworld/` (task 4.1) is in the tree but unverified: CWR-014 has no recorded attempt.

## 3. world.kvx

> **Pending verification.** Evidence: CWR-001 (driver `tests/cwr/cases/0_3.py`) for required keys and missing-limit errors, no attempt recorded. Source in tree, unverified: `crates/cwr-contract/src/world.rs`.

Every key is required. There are no defaults: an omitted limit, clock or RNG key is a compile error, because a limit the model can hit must be printed in the manual and a value you never wrote cannot be presented as a rule you chose.

```text
[meta]
format = "cwr.world-source"
schema_version = 1

[world]
name = "refworld"
version = "0.2.0"
language = "python"
runtime = "3.12"
modules = ["fs", "kv", "jobs", "search", "publish", "world"]

[limits]
cpu_ms = 5000
wall_ms = 30000
memory_mb = 256
primitive_calls = 500
adapter_calls = 50

[clock]
tick_per_primitive = 1
tick_per_turn = 10

[rng]
seed_visible = true
```

| Key | Rule |
|---|---|
| `meta.format`, `meta.schema_version` | `"cwr.world-source"` and `1` |
| `meta.example` | Optional. `true` marks an illustrative source; see section 13 |
| `world.name` | Lowercase identifier; Instances cannot be upgraded between Builds with different names |
| `world.version` | `MAJOR.MINOR.PATCH`; equal to the newest release in `CHANGELOG.kvx` and at least the derived bump |
| `world.language`, `world.runtime` | `"python"` and `"3.12"` are the only values this grammar defines |
| `world.modules` | Every module that has a `contracts/` or `adapters/` file |
| `limits.*` | Positive integers: per-turn CPU, wall and memory budgets for the script, primitive calls and adapter calls per turn |
| `clock.*` | Positive integers: world ticks per primitive call and per turn |
| `rng.seed_visible` | Whether `world.seed()` exists as a primitive |

The limit values above are the proposed refworld values from [spec/cwr/decisions.md](../../spec/cwr/decisions.md) (P13); they are choices, not measurements.

## 4. Primitive contracts

> **Pending verification.** Evidence: CWR-001 (driver `tests/cwr/cases/0_3.py`) and CWR-002 (driver `tests/cwr/cases/1_1.py`), no attempt recorded. Source in tree, unverified: `crates/cwr-contract/src/primitive.rs`, `crates/cwr-compiler/src/resolve.rs`.

A contract file declares one module and one section per primitive.

```text
[meta]
module = "fs"

[primitive.fs.delete]
side_effect = "mutating"
params = {"path": "Path"}
returns = "None"
preconditions = [["fs.exists(path)", "NOT_FOUND"], ["not fs.locked(path)", "LOCKED"]]
effects = ["not fs.exists(path)"]
doc = "Remove the file at path. Refused when the path does not exist or is locked."
```

| Key | Rule |
|---|---|
| `side_effect` | `read_only` or `mutating`; `external` is reserved for adapters |
| `params` | Ordered object of name to type. Types: `str`, `int`, `bool`, `bytes`, `Path`, `None`, `list[T]`, `dict[str, T]` and record names declared in `manual/types.md` |
| `returns` | One type from the same set |
| `preconditions` | Ordered list of `[expression, failure code]` pairs; `[]` means the primitive never fails after parameter validation |
| `effects` | Postconditions over read_only primitives. Mutating primitives declare at least one; read_only primitives declare none |
| `doc` | One or two sentences, rendered verbatim in the manual; must not be empty |

The compiler is proposed to refuse, before anything else runs: a primitive name declared twice (CWR-C002, naming both places); a contract without a `lib/` function of the same name and parameter list, or the reverse (CWR-L002); effects on a read_only primitive or none on a mutating one (CWR-C001); a failure code outside the envelope (CWR-F001).

## 5. Preconditions

> **Pending verification.** Evidence: CWR-002 (driver `tests/cwr/cases/1_1.py`) for the language, CWR-008 (driver `tests/cwr/cases/2_2.py`) for evaluation order, no attempt recorded. Source in tree, unverified: `crates/cwr-compiler/src/expr.rs`, `crates/cwr-kernel/src/contract_check.rs`.

Preconditions are the visible half of every rule, so every term must be something the model can evaluate itself before acting.

| You may use | You may not use |
|---|---|
| The primitive's own parameter names | Calls to mutating or external primitives (CWR-V001) |
| Calls to read_only primitives of the same Build, with parameters or literals as arguments | Any other name, attribute access, indexing, host value or clock (CWR-V002) |
| `len(x)` on `str`, `bytes`, lists and dicts | |
| `== != < <= > >=` between values of equal type | |
| `and`, `or`, `not`, parentheses (short-circuit, left to right) | |
| JSON integer and string literals | |

How the kernel is proposed to evaluate them:

1. Parameters are type-checked first; a mismatch or missing argument is `INVALID` and no precondition runs.
2. Preconditions run in declared order. The first false expression stops evaluation and the call fails with that precondition's code, the primitive name and the one-based precondition index.
3. Read_only calls made inside a precondition do not advance world time and are not separate ledger records; they appear in the failing record's `preconditions` field.
4. If every precondition holds, the primitive executes.

Order matters to the model: list the precondition it should check first, first. Two preconditions may share a code; a model that receives `LOCKED` then has at most the listed expressions to re-check.

## 6. The failure envelope

> **Pending verification.** Evidence: CWR-001 (driver `tests/cwr/cases/0_3.py`), no attempt recorded. Source in tree, unverified: `crates/cwr-contract/src/envelope.rs`.

There are exactly eleven failure codes. You cannot add one; a world that needs another is a specification change.

| Code | Use it when |
|---|---|
| `NOT_FOUND` | The target named by the parameters does not exist |
| `LOCKED` | The target is held by a lock the caller does not hold |
| `CONFLICT` | A visible condition changed between check and call, or the action would duplicate a world object |
| `INVALID` | Parameters fail types, ranges or declared validity preconditions |
| `LIMIT` | A declared limit was reached; the envelope names the limit |
| `UNAVAILABLE` | An external endpoint was unreachable, answered with an undeclared status, or a replay cassette had no entry |
| `RATE_LIMITED` | An external endpoint throttled the call; carries `retry_after` in ticks |
| `TIMEOUT` | An external call exceeded its `timeout_ms` |
| `REJECTED` | An external endpoint refused the content and reported why |
| `DUPLICATE_KEY` | An idempotency key was reused with a different payload |
| `INVARIANT_VIOLATION` | Never declared by you. The kernel uses it when your Build breaks its own contract; such a Build is defective |

Every failure the model sees has this shape; `precondition` is the one-based index or `null`, `limit` is set only for `LIMIT`, `retry_after` only for `RATE_LIMITED`, and `detail` is content, not interface:

```text
{"code": "RATE_LIMITED", "primitive": "publish.post", "precondition": null,
 "retry_after": 30, "limit": null, "detail": "<content, not guaranteed>"}
```

## 7. Adapters

> **Pending verification.** Evidence: CWR-002 (driver `tests/cwr/cases/1_1.py`) for the contract checks, CWR-009 (driver `tests/cwr/cases/2_3.py`) for gateway behaviour, no attempt recorded. Source in tree, unverified: `crates/cwr-contract/src/adapter.rs`, `crates/cwr-kernel/src/gateway/mod.rs`.

An adapter is the only place your world touches anything outside itself. Its contract guarantees the interface and declares that it guarantees nothing about content.

| Key | Rule |
|---|---|
| `side_effect` | Must be `external` |
| `params`, `returns`, `doc` | As for primitives; `returns` names a record shape that the gateway validates |
| `preconditions` | Same language as section 5; evaluated before any network call |
| `idempotency` | `none`, `natural` or `key` |
| `timeout_ms` | Positive integer, host time; world time still advances by `tick_per_primitive` |
| `endpoint` | The only destination the gateway may contact; illustrative sources use `stub://` |
| `failures` | Nonempty subset of the envelope, and must include `UNAVAILABLE` (CWR-F002) |
| `content_guaranteed` | Must be present and `false` (CWR-F002) |
| `effects` | Forbidden (CWR-C001) |

| `idempotency` | What the manual tells the model |
|---|---|
| `none` | Non-idempotent: a `TIMEOUT` may have had an effect |
| `natural` | Safe to repeat; equal requests are expected to have equal effects |
| `key` | Repeat with the same key to recover a lost receipt; never reuse a key for different content (`DUPLICATE_KEY`) |

`adapters/<module>/live.py` may use only the gateway client; it has no socket of its own. Example contracts: [search](../../spec/cwr/examples/refworld/adapters/search.kvx) and [publish](../../spec/cwr/examples/refworld/adapters/publish.kvx).

What "content not guaranteed" means in practice: `search.query("tennis", 10)` is guaranteed to return a list of at most ten `SearchHit` records or one of its declared failure codes. Whether the hits are about tennis is content, and no contract can express it.

## 8. lib/

> **Pending verification.** Evidence: CWR-002 (driver `tests/cwr/cases/1_1.py`) for the import scan, CWR-003 (driver `tests/cwr/cases/1_2.py`) for undeclared codes, no attempt recorded. Source in tree, unverified: `crates/cwr-compiler/src/pyimports.rs`, `crates/cwr-compiler/src/contract_tests.rs`.

`lib/<module>.py` runs inside the kernel process, never in the script sandbox. Each public function implements exactly one contract and takes the kernel state handle plus the declared parameters.

- Allowed imports: `cwr_state`, `typing`, `dataclasses`, `json`, `re`, `math`, `collections`, `itertools`.
- Forbidden imports, each CWR-L001 with file and line: `os`, `sys`, `socket`, `http`, `urllib`, `time`, `datetime`, `random`, `secrets`, `subprocess`, `pathlib`, `shutil`, `tempfile`, `threading`, `multiprocessing`, `ctypes`, `importlib` and every third-party package.
- Randomness comes from `cwr_state.random()`; time comes from `cwr_state.world_time()`.
- Raise only codes your contract declares for that primitive. Anything else is a contract-test failure at build time and `INVARIANT_VIOLATION` at run time.

## 9. fixtures/

> **Pending verification.** Evidence: CWR-003 (driver `tests/cwr/cases/1_2.py`), no attempt recorded. Source in tree, unverified: `crates/cwr-compiler/src/contract_tests.rs`.

The compiler does not search for states on its own. For every primitive and adapter provide, under `fixtures/<module>/`:

- one initial state per declared precondition in which exactly that precondition fails and all earlier ones hold;
- one initial state in which every precondition holds, so effects can be asserted after an ok call;
- for adapters, one recorded response per declared failure and one success response, in cassette form.

A declared failure without a fixture, or a fixture that reaches no declared failure, fails the contract-test stage naming the primitive and code.

## 10. manual/

> **Pending verification.** Evidence: CWR-005 (driver `tests/cwr/cases/1_3.py`) for generation, CWR-002 (driver `tests/cwr/cases/1_1.py`) for CWR-D001, no attempt recorded. Source in tree, unverified: `crates/cwr-compiler/src/generate/manual.rs`.

The manual has ten generated sections; four of them merge your prose: `manual/intro.md`, `manual/conventions.md`, `manual/types.md` and `manual/examples.md`. Everything else, including every primitive entry, the failure envelope, limits and the changelog, is rendered from the compiled contracts. Prose that names a primitive, code or limit the contracts do not define is CWR-D001. Section order and the rendered entry format are in [spec/cwr/model-surface.md](../../spec/cwr/model-surface.md) section 2.

## 11. CHANGELOG.kvx and versions

> **Pending verification.** Evidence: CWR-004 (driver `tests/cwr/cases/1_2.py`), no attempt recorded. Source in tree, unverified: `crates/cwr-compiler/src/diff.rs`, `crates/cwr-compiler/src/changelog_gate.rs`, `crates/cwr-compiler/src/version.rs`.

Every rule change ships with a human-written change entry, or the Build is refused. See the [example changelog](../../spec/cwr/examples/refworld/CHANGELOG.kvx).

```text
[release.0.2.0]
version = "0.2.0"
date = "2026-11-06"
summary = "fs.write now refuses content above 1 MiB with LIMIT; kv keys are namespaced per job."
changes = ["fs_write_size_limit", "kv_namespaces"]

[change.fs_write_size_limit]
kind = "precondition_added"
target = "fs.write"
before = "fs.write accepted any content length."
after = "len(content) <= 1048576, otherwise LIMIT."
migration = ""
```

The `summary` is the first line the model reads in the recompile notice; write it for the model.

| Change kind | Minimum bump |
|---|---|
| `primitive_added` | minor |
| `primitive_removed` | major |
| `precondition_added` | major |
| `precondition_removed` | minor |
| `precondition_changed` | major |
| `effect_changed` | major |
| `failure_changed` | major |
| `limit_changed` | major when stricter, minor when looser |
| `state_shape` | major, and `migration` must name an existing file |
| `adapter_changed` | major |
| `doc_only` | patch |

The compiler is proposed to compute a ContractDiff against the previous Build, derive the minimum bump and refuse the Build (exit 6) when any diff entry has no matching change in the newest release, when the newest release announces a change the diff does not contain, when `world.version` is not greater than the previous version or is below the derived bump, or when a `state_shape` change has no migration (CWR-M001). Versions compare numerically, never lexically.

Pre-1.0 rule: while the major version is 0, a minor increment satisfies a derived major bump, and the diff output says the rule was applied. That is why the example is at 0.2.0 despite two major-kind changes. [spec/cwr/decisions.md](../../spec/cwr/decisions.md) (P15) additionally states that a patch increment satisfies a derived minor while the major is 0; the compiler chapter states only the first half. Treat the second half as unresolved until task 1.2 pins it.

## 12. Migrations

> **Pending verification.** Evidence: CWR-003 (driver `tests/cwr/cases/1_2.py`) for idempotency at build time, CWR-011 (driver `tests/cwr/cases/2_5.py`) for upgrade rollback, no attempt recorded. Proposed source, not yet written: `crates/cwr-kernel/src/migrate.rs`.

A migration is a pure function `migrate(state: dict) -> dict` in `migrations/<from>_<to>_<name>.py`; see the [example](../../spec/cwr/examples/refworld/migrations/0_2_0_kv_namespaces.py).

- It receives the full prior Instance state as plain data and returns the full new state without mutating its argument.
- It has the `lib/` forbidden-import list and may not import `cwr_state`.
- It must be idempotent: `migrate(migrate(s)) == migrate(s)`.
- It runs inside the upgrade transaction; an exception rolls the Instance back to its prior snapshot and Build.

## 13. Checking and building your world

> **Pending verification.** Evidence: CWR-002 (driver `tests/cwr/cases/1_1.py`), CWR-004 (driver `tests/cwr/cases/1_2.py`) and CWR-005 (driver `tests/cwr/cases/1_3.py`), no attempt recorded. Source in tree, unverified: `crates/cwr-cli/src/build.rs`, `crates/cwr-compiler/src/package.rs`.

Check the source without building (parse, resolve and lint only):

```sh
cwr lint <source>
```

Build it (all eight stages: parse, resolve, lint, contract tests, diff, changelog gate, generate, package):

```sh
cwr build <source>
cwr build <source> --out <dir> --previous <hash>
```

| `cwr build` option | Meaning |
|---|---|
| `--out <dir>` | Output root; default `build/` |
| `--previous <hash>` | Build to diff against; default the newest Build of the same world name under `--out` |
| `--debug-effects` | Assert declared effects after every primitive at run time |
| `--allow-example` | Permit a source whose `meta.example` is `true` |
| `--x-*` | One experimental flag; see [experiments.md](experiments.md). Two `--x-*` flags together are refused with exit 1 |

| Exit | `cwr build` | `cwr lint` |
|---|---|---|
| 0 | Built, or the identical Build already exists ("already built") | Clean |
| 1 | Any other error, including combined `--x-*` flags | |
| 2 | Parse or resolve failure | Parse or resolve failure |
| 3 | Lint failure | Lint failure |
| 4 | Contract-test failure | |
| 5 | Diff failure: previous Build missing or failing hash verification | |
| 6 | Changelog gate refusal | |
| 7 | Package failure, including an existing output directory whose content does not verify | |

The first failing stage stops the run and the report lists every error of that stage and nothing from later stages. A successful build writes a read-only directory `build/<sha256>/` whose name is the content hash. Inspect it with:

```sh
cwr manual <hash>
cwr diff <hash-a> <hash-b>
```

Lint codes you may see:

| Code | Meaning |
|---|---|
| CWR-V001 | A precondition calls a primitive that is not read_only |
| CWR-V002 | A precondition references a name that is not a parameter or a read_only primitive of this Build |
| CWR-C001 | Unknown file, section or key, or a shape rule violated |
| CWR-C002 | A primitive or adapter declared more than once |
| CWR-C003 | A contract or adapter file for a module missing from `world.modules` |
| CWR-F001 | A failure code outside the eleven-code envelope |
| CWR-F002 | An adapter with empty `failures`, without `UNAVAILABLE`, or without `content_guaranteed = false` |
| CWR-D001 | Manual prose names something the contracts do not define, or a `doc` is empty |
| CWR-L001 | Forbidden import in `lib/`, `adapters/*/live.py` or `migrations/` |
| CWR-L002 | Contract and `lib/` function do not match one to one |
| CWR-M001 | `state_shape` change without an existing migration |

There is no warning level; every code fails the build.

## 14. Shipping a change to running Instances

> **Pending verification.** Evidence: CWR-011 (driver `tests/cwr/cases/2_5.py`), no attempt recorded. Proposed source, not yet written: `crates/cwr-kernel/src/upgrade.rs`, `crates/cwr-kernel/src/notice.rs`.

1. Edit the source and add a release to `CHANGELOG.kvx` covering every rule change.
2. Run `cwr build <source>`; on success a new Build hash appears.
3. Review the change with `cwr diff <hash-a> <hash-b>`.
4. The operator moves each Instance with `cwr instance upgrade <id> <new-build-hash>`; see [kernel.md](kernel.md).
5. On the Instance's next turn the model sees the recompile notice: old and new versions, your summary and every change touching a primitive that Instance has used.

## 15. Quick reference: situations and results

> **Pending verification.** Evidence: CWR-001, CWR-002 and CWR-004, no attempt recorded.

| Situation | Proposed result |
|---|---|
| Two preconditions of one primitive are both false | The first in declared order is reported; the second is not evaluated |
| A precondition references a primitive this release removes | CWR-V002 |
| A precondition references a mutating primitive | CWR-V001 |
| An adapter declares `effects` | CWR-C001 |
| An adapter declares `content_guaranteed = true` or omits it | CWR-F002 |
| An adapter's `failures` lacks `UNAVAILABLE` | CWR-F002 |
| `limits.memory_mb` omitted | Parse error; no default substituted |
| `lib/` imports `time` | CWR-L001 with file and line |
| Contract without `lib/` function, or the reverse | CWR-L002 |
| Primitive declared in two contract files | CWR-C002 naming both |
| `contracts/jobs.kvx` exists but `jobs` is not in `world.modules` | CWR-C003 |
| A rule changes with no matching change entry | Changelog gate refusal listing the uncovered entries |
| `state_shape` change with `migration = ""` | CWR-M001 |
| Two `release.*` sections with the same version | Parse error: duplicate section |
| Same version as the previous Build but `contracts.json` differs | Changelog gate refusal |
| A declared failure has no fixture | Contract-test failure naming primitive and code |

## 16. Open points in the contract

These are differences between specification chapters that this guide does not resolve. Each must be settled by the owning task and recorded in the specification before this guide can describe it as behaviour.

- [spec/cwr/kernel.md](../../spec/cwr/kernel.md) section 7 refers to an adapter field `undeclared_status` and to a declared ratio for converting `retry_after` into ticks. Neither key appears in the adapter key table of [spec/cwr/world-source.md](../../spec/cwr/world-source.md) section 7, and unknown keys are CWR-C001. Owner: tasks 0.3 and 2.3.
- The pre-1.0 patch-satisfies-minor rule appears in decisions P15 but not in the compiler chapter (section 11 above). Owner: task 1.2.

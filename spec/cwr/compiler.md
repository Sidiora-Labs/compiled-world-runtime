<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# World Compiler and Build artifact

Status: proposed contract; no compiler exists and every case is UNRUN. Requirements 4 and 10 in [spec.kvx](spec.kvx) apply, with the source grammar in [world-source.md](world-source.md). Commands, lint codes and file layouts below are proposals; listing them is not evidence that any of them exists.

## 1. Role

The compiler is where the three pillars are checked before a world can exist. A source that could present a rule the kernel would not enforce, hide a condition the model cannot query, or change a rule without a human-written changelog entry does not become a Build. The compiler's only output is an immutable, content-addressed directory; its only caller is a human running `cwr build` (clause 5.1).

The compiler is proposed as the Rust crate `crates/cwr-compiler` behind the `crates/cwr-cli` binary. That is an implementation choice recorded in [decisions.md](decisions.md), not a measured one.

## 2. Pipeline

Stages run in this order and the first failure stops the run (clause 4.1). A failure report names the stage, every error found in that stage, and nothing from later stages, so that an author fixing a parse error is not shown lint output computed on a partial tree.

| Stage | Input | Output | Stops when |
|---|---|---|---|
| parse | The source directory | Typed sections for `world.kvx`, contracts, adapters, `CHANGELOG.kvx`; file inventory | Any KVX syntax error, unknown file, section or key, duplicate key, missing limit |
| resolve | Typed sections, `lib/` and `adapters/*/live.py` ASTs | Symbol table: every primitive, adapter, failure code, module, migration, manual prose file | An undefined reference, a contract without implementation or the reverse, a module not declared |
| lint | Symbol table | Empty on success; otherwise a list of lint codes with locations | Any lint in section 3 fires |
| contract tests | Symbol table, `lib/`, adapters, fixtures | `tests.json` report | Any declared failure unreachable, any effect unmet after ok, any undeclared code emitted, any migration not idempotent |
| diff | `contracts.json` of this source and of the previous Build | `contract_diff.json`, derived minimum bump | Previous Build named but missing or failing hash verification |
| changelog gate | ContractDiff, `CHANGELOG.kvx`, `world.version` | Empty on success | Any uncovered diff entry, a version below the derived bump, a `state_shape` entry without migration |
| generate | Symbol table, `manual/`, templates | `manual.md`, `sdk/`, `contracts.json` | Never on valid input; a generator exception is a compiler defect |
| package | Everything above | `build/<sha256>/` with `manifest.kvx` written last | Target directory exists, or a file fails to become read-only |

`cwr lint` runs the first three stages only. `cwr build` runs all eight.

## 3. Lint codes

| Code | Pillar | Check | Location reported |
|---|---|---|---|
| CWR-V001 | Visibility | A precondition expression calls a primitive whose `side_effect` is not `read_only` | contract file, primitive, precondition index |
| CWR-V002 | Visibility | A precondition expression references a name that is not a parameter or a read_only primitive of this Build, including removed primitives, attribute access and anything host-derived | same |
| CWR-C001 | Closure | A file, section or key the grammar does not define, or a shape rule violated (effects on read_only, no effects on mutating, effects on an adapter) | file, section, key |
| CWR-C002 | Closure | A primitive or adapter name declared more than once | both locations |
| CWR-C003 | Closure | A `contracts/` or `adapters/` file for a module absent from `world.modules` | file |
| CWR-F001 | Predictable failure | A precondition or adapter `failures` entry names a code outside the eleven-code envelope | contract file, primitive, index |
| CWR-F002 | Predictable failure | An adapter has an empty `failures` list, lacks `UNAVAILABLE` (the mapping target for undeclared transport outcomes), or does not declare `content_guaranteed = false` | adapter file, adapter |
| CWR-D001 | Documented equals enforced | A `manual/` prose file names a primitive, code or limit that the contracts do not define, or a `doc` string is empty | prose file and line, or contract section |
| CWR-L001 | Closure | A forbidden import in `lib/`, `adapters/*/live.py` or `migrations/` | file, line |
| CWR-L002 | Closure | A public function in `lib/` without a contract, a contract without a `lib/` function, or a parameter list that differs between them | both locations |
| CWR-M001 | Controlled change | The diff contains a `state_shape` entry and the matching change has an empty or missing migration, or the named migration file does not exist | `CHANGELOG.kvx` change section |

CWR-M001 is detected at the changelog gate but reported under the lint namespace so that every refusal an author can see has one code. There is no warning level; every code fails the build.

## 4. Contract tests

Contract tests are generated from the contracts and run against the fixtures in an ephemeral Instance that is created and discarded inside the compiler process (clause 4.2). The ephemeral Instance uses the same kernel state API that the real kernel exposes, which is why `crates/cwr-compiler` depends on `crates/cwr-kernel` for its state and contract-check modules and not the reverse.

For each primitive:

1. For each declared precondition index `i`, load the fixture in which preconditions `1..i-1` hold and `i` fails; call the primitive; require the failure code of precondition `i`, the primitive name and index `i` in the envelope, and state unchanged.
2. Load the all-hold fixture; call the primitive; require status ok, the declared return type, and every `effects` expression true afterwards.
3. Require that no call produced a code the contract does not declare and no Python exception escaped `lib/`.

For each adapter: for each declared failure, replay the fixture response through the gateway in replay mode and require that exact code; replay the success fixture and require the declared return shape. The gateway is run in replay mode only; a compiler never opens a network connection.

For each migration: apply it to the previous Build's fixtures and require that the result validates against the new state shape and that applying it twice equals applying it once.

The whole suite runs twice with the same seed; any difference between the two outcome streams, compared as (status, shape, side-effect class), fails the stage. This is the compile-time half of clause 1.6; the runtime half is `INVARIANT_VIOLATION` in [kernel.md](kernel.md).

## 5. ContractDiff

The diff compares this source's `contracts.json` with the previous Build's. Compared surface: primitive and adapter names, `side_effect`, `params`, `returns`, ordered preconditions with codes, effects, adapter `idempotency`, `timeout_ms`, `endpoint`, `failures`, `undeclared_status` and `retry_after_ratio`, every value in `limits` and `clock`, `rng.seed_visible`, and the state shape declared by `lib/`'s typed state records. Documentation strings are compared separately so a `doc_only` diff can be told apart from a rule diff.

Entries are sorted by target then field and serialised as canonical JSON with no whitespace variation, so two compilers on two hosts produce the same bytes.

```text
{
  "previous": "sha256:3f0c...",
  "entries": [
    {"kind": "precondition_added", "target": "fs.write", "field": "preconditions",
     "before": [["not fs.locked(path)", "LOCKED"]],
     "after": [["not fs.locked(path)", "LOCKED"], ["len(content) <= 1048576", "LIMIT"]],
     "bump": "major"},
    {"kind": "state_shape", "target": "kv", "field": "state",
     "before": "dict[str, bytes]", "after": "dict[str, dict[str, bytes]]",
     "bump": "major", "migration": "migrations/0_2_0_kv_namespaces.py"}
  ],
  "derived_bump": "major",
  "doc_only_entries": 0
}
```

A first Build has `previous` null and an empty `entries` list; its derived bump is `none` and any version is accepted.

## 6. Version derivation and the changelog gate

The derived bump is the maximum over entries using the kind table in [world-source.md](world-source.md) section 11: major beats minor beats patch beats none. `limit_changed` is major when the new value is smaller for a limit or larger for a tick, minor otherwise.

The gate algorithm, proposed:

1. Read the newest `release.<version>` section; require `version` equal to `world.version`.
2. Require that `world.version` is numerically greater than the previous Build's version and that the difference is at least the derived bump. Pre-1.0 rule: while the major is 0, a minor increment satisfies a derived major, and the diff output states that this rule was applied.
3. For every ContractDiff entry, require a change in the newest release whose `kind` and `target` match. One change may cover several entries with the same target and kind; an entry covered by a change in an older release is still uncovered.
4. For every `state_shape` entry, require the matching change's `migration` to name an existing file (otherwise CWR-M001).
5. A change in the newest release that matches no diff entry is also a refusal: the changelog may not announce a change that did not happen.

Refusal output lists every uncovered entry and every unmatched change together, so one edit of `CHANGELOG.kvx` can fix the whole set.

## 7. Build artifact

| Path under `build/<sha256>/` | Content | In hash |
|---|---|---|
| `manifest.kvx` | `name`, `version`, `previous` hash, `compiler` revision, `runtime`, `experimental` flag and name, `hash` | partly: the `hash` key is excluded, every other key included |
| `contracts.json` | The complete compiled surface the kernel enforces and the diff compares | yes |
| `contract_diff.json` | Section 5 output against `previous` | yes |
| `manual.md` | Generated manual per [model-surface.md](model-surface.md) | yes |
| `sdk/` | Generated Python package the sandbox imports | yes |
| `lib/` | Kernel-side primitive implementations, byte-copied | yes |
| `adapters/` | Gateway-side implementations and contracts, byte-copied | yes |
| `fixtures/` | Fixtures and cassettes, byte-copied | yes |
| `migrations/` | Byte-copied | yes |
| `tests.json` | Contract-test report with the compiler's two-run outcome streams, without host durations | yes |

Hash computation: the relative paths of every in-hash file are sorted bytewise; for each, the path bytes, a zero byte, the file length as decimal, a zero byte and the file bytes are fed to SHA-256 in that order. An empty directory under an in-hash directory, or an empty in-hash directory itself, is an entry of its own: its path followed by `/`, fed with length 0 and no bytes. `manifest.kvx` is hashed with its `hash` key omitted, then written last with the key present. The directory name is the hex digest. After `manifest.kvx` is written every file and the directory are made read-only; the kernel recomputes the hash before loading and refuses a mismatch (clauses 1.4 and 4.5).

Every entry of the Build directory is in the hash. Verification refuses a top-level entry not listed in the table above, a listed entry of the wrong kind (a directory where a file is listed, or the reverse), a symbolic link and any special file anywhere in the Build, so an added, removed or edited entry is always detected. `cwr manual` and the compiler additionally require all ten listed entries to be present.

`tests.json` is inside the hash, as clause 4.5 and task 1.3 require: the evidence that the Build passed its contract tests travels with it and cannot be edited or forged afterwards. It therefore records no host durations; it holds only the deterministic two-run outcome streams, case results and counts, so it is a function of the World Source like every other in-hash file.

## 8. Determinism of generation

Two compilations of one source, on different hosts and at different times, must produce identical bytes for every in-hash file (clause 4.6). Rules the generators follow:

- Iteration over modules, primitives, parameters, preconditions and changelog entries is in declared order where the source has one and sorted bytewise otherwise.
- No timestamps, hostnames, usernames, absolute paths, environment values or compiler build dates are written into any in-hash file. The compiler revision in `manifest.kvx` is the crate version string, not a git describe with a dirty flag.
- The manual and SDK are rendered from templates shipped inside the compiler crate; a template change is a compiler release and shows up as a different `compiler` value.
- Line endings are `\n`; files end with one newline; JSON uses two-space indentation with sorted keys.

Task 1.3 is proposed to prove this by compiling on two differing working directories and comparing hashes; until then the rule is a design intent.

## 9. Command line

| Command | Arguments | Effect | Exit codes |
|---|---|---|---|
| `cwr build <source>` | `--out <dir>` default `build/`; `--previous <hash>` default the newest Build of the same name under `--out`; `--debug-effects`; `--allow-example`; any `--x-*` flag | Runs all eight stages and writes the Build | 0 built; 2 parse or resolve; 3 lint; 4 contract tests; 5 diff; 6 changelog gate; 7 package; 1 any other error |
| `cwr lint <source>` | none | Runs parse, resolve and lint | 0 clean; 2 or 3 as above |
| `cwr diff <hash-a> <hash-b>` | `--out <dir>` | Prints the ContractDiff between two Builds and the derived bump | 0; 5 when a Build is missing or fails hash verification |
| `cwr manual <hash>` | `--out <dir>` | Prints `manual.md` of a Build after verifying its hash | 0; 5 on verification failure |

Nothing else creates or modifies a Build directory. The kernel, the harness and the SDK have no write path into `build/` or `xbuild/` (clause 5.1); task 2.5 proposes to check that by inspection of their capabilities and by a running-Instance test.

## 10. Experimental Builds

The hypotheses in [experiments.md](experiments.md) need worlds with exactly one pillar removed. The compiler produces those only when asked by flag, writes them under `xbuild/<sha256>/` rather than `build/`, sets `experimental = true` and `experimental_flag = "<flag>"` in `manifest.kvx`, and the kernel refuses to load them without `--allow-experimental` (clause 10.1). Every ledger record of an experimental Instance carries the flag name, so a ledger can never be mistaken for a control run.

| Flag | What it changes | What it must not change | Arm |
|---|---|---|---|
| `--x-drift <drift.kvx>` | Embeds a drift schedule; at each scheduled turn the kernel's drift injector swaps the named precondition or failure in the running enforcement table and writes a ledger record of kind `drift` | The manual, the SDK, `contracts.json`, limits; the Build otherwise hashes over the same inputs plus the drift file | H1 drift-1, drift-3 |
| `--x-silent-change` | Marks the Build so that `cwr instance upgrade` into it stores the recompile notice but renders nothing | Contracts, manual, migrations, the changelog gate, which still runs | H4 silent |
| `--x-version-only` | Marks the Build so that the rendered notice is only the version line | As above | H4 version-only |
| `--x-one-primitive` | Sets `limits.primitive_calls = 1` in the compiled surface and appends the sentence stating that limit to the manual's limits section | Every other contract, every other limit | H2 one-primitive |

Drift grammar, illustrated by [examples/refworld/drift/drift-1.kvx](examples/refworld/drift/drift-1.kvx):

| Key | Type | Meaning |
|---|---|---|
| `meta.format` | string | `"cwr.drift"`, `schema_version` 1. |
| `drift.<n>.turn` | positive integer | First turn at which the injected rule applies. Injection happens before the turn's script starts. |
| `drift.<n>.primitive` | string | An existing primitive or adapter. |
| `drift.<n>.change` | string | `precondition_added`, `precondition_removed`, `precondition_changed`, `effect_changed`, `failure_changed`, `limit_changed` or `adapter_changed`; kinds that cannot be applied to a running table (`primitive_added`, `state_shape`, `doc_only`) are rejected. |
| `drift.<n>.expr` | string | The precondition expression to add or substitute, in the language of [world-source.md](world-source.md) section 5; must reference read_only primitives only, so that the injected rule is checkable even though it is undocumented. |
| `drift.<n>.failure` | string | The envelope code the injected precondition raises. |
| `drift.<n>.doc` | string | Author's note; never rendered to the model. |

The drift injector is a kernel component, but the compiler validates the drift file at parse time with the same checks as a contract, so a drift Build cannot inject a rule that would itself be a lint error.

Attribution rule (clause 10.5): `cwr diff <control> <experimental>` must list only entries attributable to the flag: the embedded drift schedule for `--x-drift`, the notice-rendering marker for the two H4 flags, and the single `limit_changed` entry plus the manual sentence for `--x-one-primitive`. Any other entry means the experimental Build was built from a different source and the harness refuses the pair. Task 3.2 proposes to check this for every flag.

## 11. Compiler failure and interleaving table

| Situation | Required result |
|---|---|
| `--previous` names a hash with no directory | diff stage failure, exit 5; no Build written |
| The previous Build's recomputed hash differs from its directory name | diff stage failure, exit 5, naming the directory; the compiler never trusts a Build it cannot verify |
| No previous Build exists and `--previous` is absent | First-Build path: empty diff, any version accepted, `previous` null in the manifest |
| `world.version` is lower than or equal to the previous version | changelog gate refusal, exit 6 |
| `world.version` is higher but below the derived bump | changelog gate refusal, exit 6, stating the derived bump and the rule applied |
| Two `release.*` sections carry the same version | parse failure, exit 2 (duplicate section) |
| The newest release announces a change the diff does not contain | changelog gate refusal, exit 6 |
| The output directory for the computed hash already exists | package stage: if its content verifies, report "already built" and exit 0 without writing; otherwise exit 7 and leave it untouched |
| Two `cwr build` runs of the same source race on one host | Both compute the same hash; the second observes the first's directory as above; no partial directory is ever visible because packaging writes into a temporary sibling and renames |
| A `--x-*` flag is combined with another `--x-*` flag | Refused at argument parsing, exit 1; one pillar per experimental Build |
| `--x-drift` names a primitive that does not exist | parse failure of the drift file, exit 2 |
| Source has `meta.example = true` and `--allow-example` is absent | parse failure, exit 2 |
| Contract tests pass once and differ on the second run | contract-test failure, exit 4, with the first divergent outcome |
| A generator writes a timestamp or absolute path | Detected by task 1.3's two-host comparison; treated as a compiler defect, not as an acceptable Build |

## 12. Required future evidence

Everything above is a proposal. The cases below are UNRUN and their drivers do not exist.

- **CWR-002** (task 1.1): the clean illustrative source passes parse, resolve and lint; five seeded defects stop at the expected stage with the expected code.
- **CWR-003** (task 1.2): every declared precondition failure and effect is exercised in the ephemeral Instance; an undeclared code or an unmet effect fails the build naming the primitive; two runs produce identical outcome streams.
- **CWR-004** (task 1.2): an independent recomputation of the diff from both contract sets matches `contract_diff.json`; uncovered entries, announced-but-absent changes, under-bumped versions and migration-less state-shape changes are refused with the listed exit codes.
- **CWR-005** (task 1.3): two compilations on different working directories hash identically; a one-byte change to any in-hash file is detected by verification; every primitive appears once in the manual with its ordered preconditions and codes.
- **CWR-013** (task 3.2): each experimental Build's diff against its control lists only flag-attributable entries; the kernel gate holds; drift fires at the scheduled turn with a ledger record while the manual stays byte-identical.

Passing these would show that a Build is what this document says it is. It would not show that the kernel enforces the Build, that the model surface renders it, or that any hypothesis holds; those are the subjects of [kernel.md](kernel.md), [model-surface.md](model-surface.md) and [experiments.md](experiments.md).

# CWR implementation plan

Generated from [spec.kvx](spec.kvx); regenerate with `python3 spec/cwr/tools/validate_spec.py --render`.

Source digest (requirements, clauses, tasks and cases): `sha256:3fb72a8c1861553c7aabce94a450c9ace4a7353559327b5da8342f2a0b214bbc`.

Task statuses below record authoring state. Verification drivers are task deliverables; a command being listed is not evidence that it exists or passes. No compiler, kernel, model or campaign has run.

Dependencies determine eligibility. Wave numbers group integration obligations: contracts and evidence (0), compiler (1), kernel (2), model surface and ablation builds (3), reference world (4), experiments (5), release (6). Each package couples its implementation with a direct consumer and positive and negative checks. Component success does not qualify a complete case or a hypothesis.

| Task | Wave | Scope | Requires | Owner |
|---|---:|---|---|---|
| 0.1 | 0 | Freeze the CWR specification pack and structural validator | None | spec |
| 0.2 | 0 | Qualification registry, evidence schema and independent checker | 0.1 | qualification |
| 0.3 | 0 | Contract grammar, failure envelope and World Source schema crate | 0.1 | contracts |
| 1.1 | 1 | Compiler front end: parse, resolve and lint for closure and visibility | 0.3 | compiler |
| 1.2 | 1 | Contract tests, ContractDiff, changelog gate and version derivation | 1.1 | compiler |
| 1.3 | 1 | Build packaging, manual generation and Python SDK generation | 1.2 | compiler |
| 2.1 | 2 | Instance store, ledger, world clock, RNG and snapshots | 0.3 | kernel |
| 2.2 | 2 | Sandboxed executor and atomic contract check | 2.1, 1.3 | kernel |
| 2.3 | 2 | Adapter gateway with normalisation, idempotency and cassette modes | 2.2 | kernel |
| 2.4 | 2 | Crash recovery and ledger replay | 2.3 | kernel |
| 2.5 | 2 | Instance upgrade, migrations, recompile notice and proposal outbox | 2.4, 1.2 | kernel |
| 3.1 | 3 | Context assembly and turn protocol | 2.5 | surface |
| 3.2 | 3 | Experimental ablation builds and kernel gate | 3.1 | compiler |
| 4.1 | 4 | Reference world modules, stub servers and cassettes | 2.3, 1.3 | refworld |
| 4.2 | 4 | Task suite with bins, rule-reuse tasks and success predicates | 4.1, 3.1 | refworld |
| 4.3 | 4 | Upgrade pairs U1 through U6 | 4.1, 2.5 | refworld |
| 5.1 | 5 | Experiment harness runner and ledger-only metrics | 0.2, 3.2, 4.2, 4.3 | experiments |
| 5.2 | 5 | H1 stability and H2 direct-action campaigns | 5.1 | experiments |
| 5.3 | 5 | H3 long-context and H4 controlled-change campaigns and analysis | 5.1 | experiments |
| 6.1 | 6 | Documentation and reproducible release artifacts | 5.2, 5.3 | release |
| 6.2 | 6 | Evidence closure and owner acceptance | 6.1 | release |

## 0.1: Freeze the CWR specification pack and structural validator

Status: **pending**. Owner: `spec`. Dependencies: none.

Proposed implementation scope:

- `spec/cwr/spec.kvx`
- `spec/cwr/tools/validate_spec.py`
- `spec/cwr/README.md`
- `spec/cwr/implementation-plan.md`
- `spec/cwr/traceability.md`
- `tests/cwr/cases/0_1.py`

Future focused gate: `python3 tests/cwr/cases/0_1.py`.

Acceptance clauses: 13.1, 13.5, 14.1.
Canonical cases: CWR-020 (UNRUN).

Implementation and qualification obligations:

1. Run the structural validator over the pack and confirm strict KVX parsing, consecutive acceptance clauses containing SHALL, symmetric task and case mappings, wave-ordered dependencies and complete clause coverage by both a task and a case.
2. Confirm the example World Source under examples/refworld parses and satisfies the cross-file checks: version agrees with the latest changelog release, preconditions reference only read_only primitives, every failure code is in the envelope and every state-shape change names a migration.
3. Regenerate implementation-plan.md and traceability.md and confirm a modified clause, task or case makes the derived views stale and fails validation until re-rendered.
4. Create the task driver to run the validator as a subprocess, assert exit code zero and a PASS report, and assert that tampering with a case status or a task status away from UNRUN or pending fails nonzero. The driver proves authoring consistency only, never product behaviour.

## 0.2: Qualification registry, evidence schema and independent checker

Status: **pending**. Owner: `qualification`. Dependencies: 0.1.

Proposed implementation scope:

- `tests/cwr/registry.kvx`
- `tests/cwr/qualify.py`
- `tests/cwr/check_evidence.py`
- `tests/cwr/evidence.schema.json`
- `tests/cwr/cases/0_2.py`

Future focused gate: `python3 tests/cwr/cases/0_2.py`.

Acceptance clauses: 13.2, 13.3, 13.4.
Canonical cases: CWR-019 (UNRUN).

Implementation and qualification obligations:

1. Define the append-only attempt record: case and assertion ids, clauses, Build hash, Instance id, seed, cassette set digest, model identity, compiler and kernel revisions, arm, UTC start and end, exit status, log path and observed assertions. Reject records missing any field.
2. Implement the registry loader for CWR-NNN identities with per-case expected assertion inventories and tiers component, integration, campaign and release; a case without a registered inventory cannot be marked PASS.
3. Implement the independent checker as a separate program that reads only ledgers, final state and attempt records, recomputes each assertion and refuses zero-assertion, tampered, stale-Build or mismatched-seed evidence.
4. Create the task driver with false-success traps: an attempt claiming PASS with no assertions, an attempt whose Build hash differs from the registered Build and an attempt with a modified ledger must each fail nonzero. Component PASS must not change the parent case status.

## 0.3: Contract grammar, failure envelope and World Source schema crate

Status: **pending**. Owner: `contracts`. Dependencies: 0.1.

Proposed implementation scope:

- `crates/cwr-contract/Cargo.toml`
- `crates/cwr-contract/src/lib.rs`
- `crates/cwr-contract/src/kvx.rs`
- `crates/cwr-contract/src/envelope.rs`
- `crates/cwr-contract/src/world.rs`
- `crates/cwr-contract/src/primitive.rs`
- `crates/cwr-contract/src/adapter.rs`
- `crates/cwr-contract/src/changelog.rs`
- `crates/cwr-contract/tests/fixtures/**`
- `tests/cwr/cases/0_3.py`

Future focused gate: `python3 tests/cwr/cases/0_3.py`.

Acceptance clauses: 2.3, 3.1, 3.2, 3.3, 3.4, 3.6.
Canonical cases: CWR-001 (UNRUN).

Implementation and qualification obligations:

1. Implement the strict KVX reader for World Source files and typed models for world.kvx, primitive contracts, adapter contracts, CHANGELOG.kvx and migrations. Unknown sections or keys, duplicate keys and non-JSON values are errors with file and line.
2. Encode the closed failure envelope as a Rust enum with retry_after only on RATE_LIMITED; serialisation of any other code is impossible by construction and deserialisation of an unknown code fails.
3. Enforce the shape rules: side_effect classes, mutating primitives declare at least one effect, read_only primitives declare none, adapters declare external, idempotency, timeout_ms, endpoint, failures within the envelope and content_guaranteed false; a missing limit in world.kvx is an error.
4. Create the task driver to build the crate, run its unit tests and feed malformed fixtures: an adapter with effects, a read_only primitive with effects, a precondition with an unknown code, a world file missing memory_mb and a changelog state-shape change without a migration. Each must be rejected with a located error.

## 1.1: Compiler front end: parse, resolve and lint for closure and visibility

Status: **pending**. Owner: `compiler`. Dependencies: 0.3.

Proposed implementation scope:

- `crates/cwr-compiler/Cargo.toml`
- `crates/cwr-compiler/src/lib.rs`
- `crates/cwr-compiler/src/parse.rs`
- `crates/cwr-compiler/src/resolve.rs`
- `crates/cwr-compiler/src/lint.rs`
- `crates/cwr-compiler/src/expr.rs`
- `crates/cwr-compiler/src/pyimports.rs`
- `crates/cwr-cli/src/build.rs`
- `tests/cwr/cases/1_1.py`

Future focused gate: `python3 tests/cwr/cases/1_1.py`.

Acceptance clauses: 2.1, 2.2, 3.1, 3.5, 4.1.
Canonical cases: CWR-002 (UNRUN).

Implementation and qualification obligations:

1. Implement the ordered pipeline skeleton with stage identities parse, resolve, lint, contract_tests, diff, changelog_gate, generate and package; the pipeline stops at the first failing stage and reports every lint code with file, section and key.
2. Implement the precondition expression language: identifiers resolve to parameters or to read_only primitives of the same Build; any reference to a mutating or external primitive, an unknown name or a host facility is CWR-V001 or CWR-V002.
3. Implement the lib/ import scanner that rejects os, sys, socket, time, datetime, random, subprocess, pathlib and any module outside the generated SDK as CWR-L001, and rejects unknown source files, sections or keys as CWR-C001.
4. Create the task driver that compiles the example World Source through the lint stage, then mutates it in five ways (hidden precondition, host import, unknown key, duplicate primitive, missing module) and asserts the exact lint code and stage for each failure.

## 1.2: Contract tests, ContractDiff, changelog gate and version derivation

Status: **pending**. Owner: `compiler`. Dependencies: 1.1.

Proposed implementation scope:

- `crates/cwr-compiler/src/contract_tests.rs`
- `crates/cwr-compiler/src/diff.rs`
- `crates/cwr-compiler/src/changelog_gate.rs`
- `crates/cwr-compiler/src/version.rs`
- `crates/cwr-compiler/src/ephemeral.rs`
- `crates/cwr-cli/src/diff.rs`
- `tests/cwr/cases/1_2.py`

Future focused gate: `python3 tests/cwr/cases/1_2.py`.

Acceptance clauses: 1.6, 3.6, 4.2, 4.3, 4.4, 5.5.
Canonical cases: CWR-003, CWR-004 (UNRUN).

Implementation and qualification obligations:

1. Implement the ephemeral Instance used by contract tests and run each primitive against its fixtures: every declared precondition failure must be reachable, every declared effect must hold after ok, and any undeclared code or an effect that does not hold is a failure naming the primitive.
2. Implement ContractDiff over primitives, preconditions, effects, limits, failures, adapters and state shapes with stable ordering and canonical JSON output, and derive the minimum bump: major for removal or stricter rule, minor for addition or looser rule, patch for documentation.
3. Implement the changelog gate: every diff entry must be covered by a CHANGELOG.kvx change for the declared version, every state-shape change must name an existing migration and the declared version must be at least the derived bump; otherwise fail with the uncovered entries listed.
4. Create the task driver that compiles the upgrade pair fixtures and asserts: an undocumented stricter precondition is refused, a documented one passes with a major bump, a documentation-only change passes with a patch bump, a state-shape change without migration is refused and a primitive emitting an undeclared code fails contract tests.

## 1.3: Build packaging, manual generation and Python SDK generation

Status: **pending**. Owner: `compiler`. Dependencies: 1.2.

Proposed implementation scope:

- `crates/cwr-compiler/src/generate/manual.rs`
- `crates/cwr-compiler/src/generate/sdk.rs`
- `crates/cwr-compiler/src/package.rs`
- `crates/cwr-compiler/src/manifest.rs`
- `sdk/python/cwr/_codec.py`
- `sdk/python/cwr/_rpc.py`
- `sdk/python/cwr/templates/**`
- `crates/cwr-cli/src/manual.rs`
- `tests/cwr/cases/1_3.py`

Future focused gate: `python3 tests/cwr/cases/1_3.py`.

Acceptance clauses: 1.4, 2.4, 2.5, 4.5, 4.6, 9.2.
Canonical cases: CWR-005 (UNRUN).

Implementation and qualification obligations:

1. Generate manual.md from contracts.json and manual/ with the ten fixed sections and the rendered primitive entry format, including the content-not-guaranteed label, the verbatim failure envelope and the three pillars; the generator is a pure function with sorted iteration and no timestamps.
2. Generate the Python SDK: one typed function per primitive that performs exactly one RPC, typed exceptions per failure code, world.limits, world.random, world.history and world.propose, and a codec for the result envelope.
3. Package the Build directory keyed by sha256 over manifest.kvx, contracts.json, contract_diff.json, manual.md, sdk/, lib/, adapters/, fixtures/, migrations/ and tests.json; set every file read-only and write the hash into the manifest last.
4. Create the task driver that compiles the example world twice on two differing working directories and asserts identical hashes, asserts that every primitive appears exactly once in the manual with its ordered preconditions and codes, and asserts that modifying one byte of a packaged file is detected by hash verification.

## 2.1: Instance store, ledger, world clock, RNG and snapshots

Status: **pending**. Owner: `kernel`. Dependencies: 0.3.

Proposed implementation scope:

- `crates/cwr-kernel/Cargo.toml`
- `crates/cwr-kernel/src/lib.rs`
- `crates/cwr-kernel/src/state.rs`
- `crates/cwr-kernel/src/ledger.rs`
- `crates/cwr-kernel/src/clock.rs`
- `crates/cwr-kernel/src/rng.rs`
- `crates/cwr-kernel/src/snapshot.rs`
- `crates/cwr-cli/src/instance.rs`
- `tests/cwr/cases/2_1.py`

Future focused gate: `python3 tests/cwr/cases/2_1.py`.

Acceptance clauses: 1.5, 6.6, 8.1, 8.2, 9.4.
Canonical cases: CWR-006 (UNRUN).

Implementation and qualification obligations:

1. Implement the content-addressed Instance state store separate from the Build, with typed module state, the per-Instance tail size K recorded at creation and a header record carrying Build hash, seed, K and arm.
2. Implement the append-only JSONL ledger with the schema in kernel.md, fsync before the result is returned, monotonic seq and a corruption check that refuses to open a ledger whose last record is truncated without an explicit recovery.
3. Implement the tick clock advancing only by tick_per_primitive and tick_per_turn, and the PCG RNG seeded per Instance and advanced only by world.random draws, with state captured in snapshots.
4. Create the task driver that creates an Instance, appends records, snapshots, mutates further, restores and asserts identical state, seq, world_time, rng state and K; asserts that no primitive exposes host time; and asserts equal random draws across two Instances with equal seed and ledger prefix.

## 2.2: Sandboxed executor and atomic contract check

Status: **pending**. Owner: `kernel`. Dependencies: 2.1, 1.3.

Proposed implementation scope:

- `crates/cwr-kernel/src/sandbox.rs`
- `crates/cwr-kernel/src/rpc.rs`
- `crates/cwr-kernel/src/executor.rs`
- `crates/cwr-kernel/src/contract_check.rs`
- `crates/cwr-kernel/src/limits.rs`
- `crates/cwr-kernel/src/envelope.rs`
- `sdk/python/cwr/_runner.py`
- `crates/cwr-cli/src/turn.rs`
- `tests/cwr/cases/2_2.py`

Future focused gate: `python3 tests/cwr/cases/2_2.py`.

Acceptance clauses: 1.1, 1.2, 1.3, 1.6, 2.2, 2.6, 6.1, 6.2, 6.3, 6.4, 6.5.
Canonical cases: CWR-007, CWR-008 (UNRUN).

Implementation and qualification obligations:

1. Implement the per-turn sandbox: a fresh Python 3.12 process with no network namespace, an empty read-only filesystem view containing only the Build SDK, no host clock access, and cpu_ms, wall_ms and memory_mb enforcement that terminates with LIMIT naming the limit.
2. Implement one RPC per primitive over a unix socket and the atomic contract check in the order validate, preconditions in declared order, execute, assert effects in debug mode, advance world_time, append ledger; a failure before execute leaves state unchanged and returns the first failing precondition's code with primitive name and index.
3. Implement script result semantics: typed exceptions carrying the envelope, status partial when an uncaught failure follows a successful mutating or external primitive, the result envelope with returned, error, trace, world and resources, and world.limits reporting remaining budgets.
4. Create the task driver that runs scripts calling an unlisted module, an unlisted primitive, a socket, the host filesystem and os.time and asserts each is refused as closure violations; runs an exceeding loop and asserts LIMIT; and asserts a partial script leaves exactly the successful mutations applied.
5. Extend the driver with an interface-determinism check: repeat each refworld primitive under equal visible conditions and assert equal status, shape and side-effect class; inject a lib defect that emits an undeclared code and assert it is recorded as INVARIANT_VIOLATION.

## 2.3: Adapter gateway with normalisation, idempotency and cassette modes

Status: **pending**. Owner: `kernel`. Dependencies: 2.2.

Proposed implementation scope:

- `crates/cwr-kernel/src/gateway/mod.rs`
- `crates/cwr-kernel/src/gateway/normalise.rs`
- `crates/cwr-kernel/src/gateway/idempotency.rs`
- `crates/cwr-kernel/src/gateway/cassette.rs`
- `crates/cwr-kernel/src/gateway/live.rs`
- `crates/cwr-kernel/src/gateway/timeout.rs`
- `tests/cwr/cases/2_3.py`

Future focused gate: `python3 tests/cwr/cases/2_3.py`.

Acceptance clauses: 1.3, 7.1, 7.2, 7.3, 7.4, 7.5, 7.6.
Canonical cases: CWR-009 (UNRUN).

Implementation and qualification obligations:

1. Implement the gateway as the sole network capability of the kernel process, restricted to endpoints declared in adapter contracts; lib/ and the sandbox have no socket capability.
2. Implement normalisation of every adapter response into the declared return shape or the envelope; undeclared statuses, transport errors and malformed bodies map to UNAVAILABLE or INVALID as declared; timeouts use timeout_ms in host time and report TIMEOUT while world_time advances by the declared tick.
3. Implement idempotency enforcement per contract: key adapters store the first outcome per key and payload hash, replay identical requests from the store and return DUPLICATE_KEY for a different payload; natural adapters record evidence; none adapters are labelled. The gateway never retries.
4. Implement live, record and replay modes with cassettes keyed by adapter, canonical request hash and sequence; replay misses return UNAVAILABLE and never open a socket, which the driver proves with an unreachable endpoint.
5. Create the task driver against the refworld stub servers: exercise every declared failure of publish.post and search.query, prove DUPLICATE_KEY and identical-replay behaviour, prove a stub 503 maps only to the declared code, and prove the recorded cassette replays byte-identical outcomes offline.

## 2.4: Crash recovery and ledger replay

Status: **pending**. Owner: `kernel`. Dependencies: 2.3.

Proposed implementation scope:

- `crates/cwr-kernel/src/recovery.rs`
- `crates/cwr-kernel/src/replay.rs`
- `crates/cwr-cli/src/replay.rs`
- `tests/cwr/cases/2_4.py`

Future focused gate: `python3 tests/cwr/cases/2_4.py`.

Acceptance clauses: 6.6, 8.3, 8.4, 8.5.
Canonical cases: CWR-010 (UNRUN).

Implementation and qualification obligations:

1. Implement recovery from the last snapshot plus ledger prefix after a kernel crash: the interrupted turn is recorded as status error with code UNAVAILABLE, and any external primitive with an existing ledger record is marked complete rather than re-executed.
2. Implement cwr instance replay from seed, initial state, Build and cassettes, re-running every turn's recorded script and producing a new ledger.
3. Implement the ledger comparison that ignores host_ts and duration_us and reports the first divergent record with both values.
4. Create the task driver that kills the kernel at every boundary of a turn with one external call (before execute, after execute before ledger append, after append) and asserts the recovery outcome and that the stub server saw exactly one request; then replays a 40-turn Instance and asserts an identical ledger except host fields.

## 2.5: Instance upgrade, migrations, recompile notice and proposal outbox

Status: **pending**. Owner: `kernel`. Dependencies: 2.4, 1.2.

Proposed implementation scope:

- `crates/cwr-kernel/src/upgrade.rs`
- `crates/cwr-kernel/src/migrate.rs`
- `crates/cwr-kernel/src/notice.rs`
- `crates/cwr-kernel/src/outbox.rs`
- `crates/cwr-cli/src/upgrade.rs`
- `sdk/python/cwr/_world.py`
- `tests/cwr/cases/2_5.py`

Future focused gate: `python3 tests/cwr/cases/2_5.py`.

Acceptance clauses: 3.6, 5.1, 5.2, 5.3, 5.4, 5.5.
Canonical cases: CWR-011 (UNRUN).

Implementation and qualification obligations:

1. Implement cwr instance upgrade: verify the target Build hash, refuse a different world name or a non-increasing version unless --allow-experimental, snapshot, apply declared migrations in order as pure state functions, then commit; any failure rolls back to the prior snapshot and Build with nothing partial visible.
2. Implement the recompile notice derived from CHANGELOG.kvx: old and new versions, human summary and every contract change touching a primitive present in the Instance ledger, stored in the Instance and rendered on the next turn.
3. Implement world.propose(text) as a ledger record of kind proposal plus an append to the outbox file outside the Build and state; confirm nothing in the kernel reads the outbox.
4. Prove that nothing but the cwr build command can create or modify a Build: the kernel, harness and SDK have no write path into build/ or xbuild/, and the driver asserts that a running Instance observes no rule change when the source is edited without recompilation.
5. Create the task driver that upgrades Instances across the six fixture pairs, asserts migration results and notice content, injects a failing migration and asserts rollback, asserts the outbox changes no contract or state, and asserts that a lower-version Build is refused.

## 3.1: Context assembly and turn protocol

Status: **pending**. Owner: `surface`. Dependencies: 2.5.

Proposed implementation scope:

- `crates/cwr-kernel/src/surface/context.rs`
- `crates/cwr-kernel/src/surface/tail.rs`
- `crates/cwr-kernel/src/surface/protocol.rs`
- `crates/cwr-kernel/src/surface/envelope_render.rs`
- `crates/cwr-cli/src/context.rs`
- `tests/cwr/cases/3_1.py`

Future focused gate: `python3 tests/cwr/cases/3_1.py`.

Acceptance clauses: 2.4, 2.5, 9.1, 9.2, 9.3, 9.4, 9.5.
Canonical cases: CWR-012 (UNRUN).

Implementation and qualification obligations:

1. Implement context assembly in the fixed order manual, recompile notice when present, ledger tail of K records, last result envelope; the manual bytes come unchanged from the Build and the assembler never edits them.
2. Implement the ledger tail renderer with one line per record carrying turn, primitive, status, code and world_time, with K read from the Instance header; implement world.history(n) over the same records.
3. Implement the turn protocol: extract exactly one fenced python block; zero or more than one block is INVALID with no execution and no world_time advance; the optional top-level return becomes the envelope's returned value.
4. Create the task driver that renders 50 turns and asserts the manual prefix is byte-identical across turns and Instances, the ten manual sections and every primitive entry are present, a two-block turn is refused without a ledger execution record, and the only context sources are the four components plus world.history.

## 3.2: Experimental ablation builds and kernel gate

Status: **pending**. Owner: `compiler`. Dependencies: 3.1.

Proposed implementation scope:

- `crates/cwr-compiler/src/experimental/mod.rs`
- `crates/cwr-compiler/src/experimental/drift.rs`
- `crates/cwr-compiler/src/experimental/notice.rs`
- `crates/cwr-compiler/src/experimental/one_primitive.rs`
- `crates/cwr-kernel/src/drift_injector.rs`
- `crates/cwr-cli/src/build.rs`
- `tests/cwr/cases/3_2.py`

Future focused gate: `python3 tests/cwr/cases/3_2.py`.

Acceptance clauses: 5.3, 10.1, 10.2, 10.3, 10.4, 10.5.
Canonical cases: CWR-013 (UNRUN).

Implementation and qualification obligations:

1. Implement the four flags producing Builds under xbuild/ with experimental true and the flag name in the manifest; the kernel refuses to load them without --allow-experimental and stamps the flag into every ledger record.
2. Implement the drift injector from the drift grammar in compiler.md: at the scheduled turn the named precondition or failure is swapped in the running contract table while the manual stays unchanged, and a ledger record of kind drift logs each injection.
3. Implement silent-change and version-only notice rendering and the one-primitive limit returning LIMIT with primitive_calls=1 on the second call, with the one-primitive manual stating the limit.
4. Create the task driver that builds each experimental Build from the control source, asserts the ContractDiff between control and experimental lists only flag-attributable entries, asserts the kernel gate, asserts the drift injection fires at the scheduled turn with a ledger record while the manual is byte-identical, and asserts the three notice renderings.

## 4.1: Reference world modules, stub servers and cassettes

Status: **pending**. Owner: `refworld`. Dependencies: 2.3, 1.3.

Proposed implementation scope:

- `examples/refworld/world.kvx`
- `examples/refworld/CHANGELOG.kvx`
- `examples/refworld/contracts/**`
- `examples/refworld/adapters/**`
- `examples/refworld/lib/**`
- `examples/refworld/fixtures/**`
- `examples/refworld/manual/**`
- `benchmark/stubs/search_server.py`
- `benchmark/stubs/publish_server.py`
- `benchmark/cassettes/**`
- `tests/cwr/cases/4_1.py`

Future focused gate: `python3 tests/cwr/cases/4_1.py`.

Acceptance clauses: 7.5, 11.1, 11.5.
Canonical cases: CWR-009, CWR-014 (UNRUN).

Implementation and qualification obligations:

1. Author the refworld World Source with modules fs, kv, jobs, search, publish and world and the rules in reference-world.md, including locks, quotas, job states, rate limits and duplicate keys, with fixtures reaching every declared failure.
2. Implement the search and publish stub servers against the adapter contracts with deterministic rate limiting, duplicate-key detection and schedulable unavailability, driven by a seed.
3. Record the cassette set for the task suite in record mode and freeze its digest; the harness and drivers use replay mode only.
4. Create the task driver that compiles refworld, asserts every declared failure of every primitive and adapter is reached by contract tests or stub schedules, asserts the stub servers emit only declared codes, and asserts the cassette set replays offline with the frozen digest.

## 4.2: Task suite with bins, rule-reuse tasks and success predicates

Status: **pending**. Owner: `refworld`. Dependencies: 4.1, 3.1.

Proposed implementation scope:

- `benchmark/tasks/**`
- `benchmark/suite.kvx`
- `benchmark/predicates/common.py`
- `tests/cwr/cases/4_2.py`

Future focused gate: `python3 tests/cwr/cases/4_2.py`.

Acceptance clauses: 11.2, 11.3.
Canonical cases: CWR-015 (UNRUN).

Implementation and qualification obligations:

1. Author 60 tasks, 20 per bin, each with task.md, initial.kvx, success.py, bin and rules; at least 12 long tasks require reuse of a rule first encountered earlier in the same Instance and name that rule.
2. Implement success predicates as pure functions over final state and ledger using benchmark/predicates/common.py helpers; predicates never read model text or kernel internals.
3. Write a reference solution script per task and record the minimum primitive count establishing its bin; freeze the suite digest in benchmark/suite.kvx.
4. Create the task driver that runs every reference solution in replay mode and asserts success, asserts the bin boundaries from recorded primitive counts, asserts predicates are deterministic across two runs, and asserts no predicate imports the kernel crate or parses text.

## 4.3: Upgrade pairs U1 through U6

Status: **pending**. Owner: `refworld`. Dependencies: 4.1, 2.5.

Proposed implementation scope:

- `examples/refworld-upgrades/U1/**`
- `examples/refworld-upgrades/U2/**`
- `examples/refworld-upgrades/U3/**`
- `examples/refworld-upgrades/U4/**`
- `examples/refworld-upgrades/U5/**`
- `examples/refworld-upgrades/U6/**`
- `benchmark/upgrades.kvx`
- `tests/cwr/cases/4_3.py`

Future focused gate: `python3 tests/cwr/cases/4_3.py`.

Acceptance clauses: 5.2, 5.3, 11.4.
Canonical cases: CWR-011, CWR-016 (UNRUN).

Implementation and qualification obligations:

1. Author six source pairs covering precondition tightening, primitive removal, failure code change, limit change, state shape change with migration and documentation-only change, each with a valid CHANGELOG.kvx and the derived version bump.
2. Author one post-upgrade task per pair whose success requires acting on the changed rule, with success predicates in the suite format.
3. Record for each pair the expected recompile notice content and the expected ContractDiff and freeze them in benchmark/upgrades.kvx.
4. Create the task driver that compiles both sides of each pair, asserts the ContractDiff and bump, upgrades a live Instance, asserts the notice matches the frozen expectation, and asserts the post-upgrade reference solution succeeds while the pre-upgrade solution fails with the documented code.

## 5.1: Experiment harness runner and ledger-only metrics

Status: **pending**. Owner: `experiments`. Dependencies: 0.2, 3.2, 4.2, 4.3.

Proposed implementation scope:

- `benchmark/harness/run.py`
- `benchmark/harness/arms.kvx`
- `benchmark/harness/metrics.py`
- `benchmark/harness/model.py`
- `benchmark/analyse.py`
- `benchmark/preregistration.kvx`
- `tests/cwr/cases/5_1.py`

Future focused gate: `python3 tests/cwr/cases/5_1.py`.

Acceptance clauses: 8.5, 12.1, 12.3, 12.4, 12.6.
Canonical cases: CWR-017 (UNRUN).

Implementation and qualification obligations:

1. Implement the runner: for an arm, task and seed it creates an Instance of the arm's Build with --allow-experimental where needed, runs turns through the model adapter with the fixed context layout in replay mode, and stops at success, turn limit or budget exhaustion.
2. Implement metrics strictly over ledgers and final state: task success, turns to success, primitive calls, failure rate by code, repeated-failure rate, rule reuse rate and post-upgrade misuse rate, with the definitions in experiments.md and no other inputs.
3. Implement benchmark/analyse.py with mean and 95% bootstrap confidence intervals per arm and bin, reading the pre-registered directions from benchmark/preregistration.kvx and refusing to run if metric definitions differ from the registered digest.
4. Create the task driver that greps the harness, arms and prompts for any reference to another agent or harness and fails on a match, runs two seeds of one short task in replay mode, asserts the metrics are recomputable from the ledgers alone, and asserts a live-mode run is labelled and excluded from pooling.

## 5.2: H1 stability and H2 direct-action campaigns

Status: **pending**. Owner: `experiments`. Dependencies: 5.1.

Proposed implementation scope:

- `benchmark/campaigns/h1.kvx`
- `benchmark/campaigns/h2.kvx`
- `benchmark/results/h1/**`
- `benchmark/results/h2/**`
- `research/h1-stability.md`
- `research/h2-direct-action.md`
- `tests/cwr/cases/5_2.py`

Future focused gate: `python3 tests/cwr/cases/5_2.py`.

Acceptance clauses: 10.2, 10.4, 12.2, 12.5.
Canonical cases: CWR-018 (UNRUN).

Implementation and qualification obligations:

1. Register the H1 arms frozen, drift-1 and drift-3 and the H2 arms script and one-primitive with their Build hashes, the full 60-task suite, 10 seeds and the pre-registered directions before any run.
2. Execute H1 and H2 in replay mode, writing one ledger per arm, task and seed under benchmark/results with the attempt record in the registry.
3. Run benchmark/analyse.py and write the two research reports with the metric tables, confidence intervals and every deviation from pre-registration stated explicitly.
4. Create the task driver that verifies the complete arm matrix exists, every ledger carries the registered arm and seed, the drift arms contain the expected drift records at the scheduled turns, and the reports' numbers equal a fresh recomputation from the ledgers.

## 5.3: H3 long-context and H4 controlled-change campaigns and analysis

Status: **pending**. Owner: `experiments`. Dependencies: 5.1.

Proposed implementation scope:

- `benchmark/campaigns/h3.kvx`
- `benchmark/campaigns/h4.kvx`
- `benchmark/results/h3/**`
- `benchmark/results/h4/**`
- `research/h3-long-context.md`
- `research/h4-controlled-change.md`
- `tests/cwr/cases/5_3.py`

Future focused gate: `python3 tests/cwr/cases/5_3.py`.

Acceptance clauses: 5.3, 10.3, 12.2, 12.5.
Canonical cases: CWR-018 (UNRUN).

Implementation and qualification obligations:

1. Register the H3 arms over bins and ledger tail K in {5, 20, full} and the H4 arms changelog, version-only and silent over the six upgrade pairs, with Build hashes, seeds and pre-registered directions before any run.
2. Execute H3 on the full suite and H4 on the upgrade-pair tasks in replay mode, writing ledgers and attempt records.
3. Run benchmark/analyse.py, including rule reuse rate per bin and post-upgrade misuse rate per notice arm, and write the two research reports.
4. Create the task driver that verifies the arm matrices, asserts that the three H4 arms differ only in notice rendering by comparing their Build ContractDiffs, asserts K is recorded in every ledger header, and asserts report numbers equal a fresh recomputation.

## 6.1: Documentation and reproducible release artifacts

Status: **pending**. Owner: `release`. Dependencies: 5.2, 5.3.

Proposed implementation scope:

- `docs/cwr/world-source.md`
- `docs/cwr/kernel.md`
- `docs/cwr/experiments.md`
- `README.md`
- `CHANGELOG.md`
- `RELEASE.md`
- `crates/cwr-cli/Cargo.toml`
- `sdk/python/pyproject.toml`
- `benchmark/release.kvx`
- `tests/cwr/cases/6_1.py`

Future focused gate: `python3 tests/cwr/cases/6_1.py`.

Acceptance clauses: 14.2, 14.3.
Canonical cases: CWR-020 (UNRUN).

Implementation and qualification obligations:

1. Document the implemented World Source grammar, compiler, kernel, model surface and harness with links to source and evidence, marking proposed or unimplemented behaviour explicitly.
2. Pin the release: compiler, kernel and SDK revisions, refworld control and experimental Build hashes, cassette set digest and task suite digest in benchmark/release.kvx; a missing input produces a named diagnostic.
3. Package the cwr CLI and Python SDK with pinned dependencies and checksums, and exercise cwr build, instance create, turn, upgrade and replay from the packaged artifacts.
4. Create the task driver that checks every documented command runs against the packaged artifacts, every evidence link resolves, the release file is complete and no document claims a result without an attempt record.

## 6.2: Evidence closure and owner acceptance

Status: **pending**. Owner: `release`. Dependencies: 6.1.

Proposed implementation scope:

- `tests/cwr/closure.py`
- `tests/cwr/cases/6_2.py`
- `spec/cwr/qualification.kvx`
- `spec/cwr/spec.kvx`

Future focused gate: `python3 tests/cwr/cases/6_2.py`.

Acceptance clauses: 13.1, 13.4, 14.1, 14.4.
Canonical cases: CWR-020 (UNRUN).

Implementation and qualification obligations:

1. Assemble closure from the registry: every case CWR-001 through CWR-020 has an executed attempt with an independent checker result, every clause maps to an executed case and the four hypothesis reports carry their pre-registration.
2. Refuse closure on any missing, tampered, stale-Build or zero-assertion evidence and list the blocking items.
3. Record owner acceptance of the running journey: build refworld, create an Instance, run a long task, upgrade across one pair and replay, with the acceptance record referencing exact Build hashes.
4. Create the closure driver and update task and case statuses only from registry outcomes; the authoring validator's PASS is never an input to closure.

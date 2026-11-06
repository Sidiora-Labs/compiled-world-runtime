<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# CWR verification and qualification

Status: **design draft; all cases CWR-001 through CWR-020 are UNRUN.** No compiler, kernel, model run, campaign or hypothesis result is asserted by this document. Structural validation of the specification establishes authoring consistency only.

## 1. Qualification seams and required work

No qualification runner exists in this repository. The `tests/` directory is empty, there is no registry, no evidence schema and no checker. Task 0.2 creates `tests/cwr/registry.kvx` (case identities, tiers and expected assertion inventories), `tests/cwr/qualify.py` (the runner), `tests/cwr/check_evidence.py` (the independent checker) and `tests/cwr/evidence.schema.json` (the attempt record schema). Per-task drivers live at `tests/cwr/cases/W_N.py` and are created by their own tasks; the `verify_cmd` of each task in [spec.kvx](spec.kvx) names its driver, and a command being listed is not evidence that the driver exists or passes.

The planned runner invocation is:

```text
python3 tests/cwr/qualify.py --case CWR-NNN
```

This command does not exist. Its arguments, output format and exit codes are fixed by task 0.2 before any driver registers against it. The runner executes drivers as subprocesses, captures Build hash, seed, cassette digest and environment, checks the exact assertion inventory registered for the case, invokes the checker and writes one append-only attempt record. It never marks a case PASS from a driver's exit code alone.

The checker is a separate program from the compiler and kernel crates. It reads ledgers, final Instance state and attempt records. It does not import `crates/cwr-kernel` or the generated SDK, and it does not parse model text. Requirement 13 in [spec.kvx](spec.kvx) applies; the ledger schema it reads is in [kernel.md](kernel.md), and the metric definitions it recomputes are in [experiments.md](experiments.md).

## 2. Evidence contract

Every attempt creates an append-only record with:

- Case and assertion ids; requirement clauses; tier (component, integration, campaign, release); task or full-case scope; specification revision; registry and schema versions.
- Build hash as `sha256:<64hex>` for every Build used, including control and experimental Builds; experimental flag names where applicable; world name and version.
- Instance id, seed, ledger tail size K and arm; initial state digest; cassette set digest and gateway mode (replay, record or live).
- Model identity and sampling settings for any case that drives a model; compiler and kernel revisions; SDK version; host, operating system and sandbox implementation.
- UTC start and end, monotonic durations, invocation and exit status; complete log path with content hash; raw ledgers and final state with content hashes.
- Observed assertions with their individual outcomes, diagnostics, coverage gaps and cleanup outcomes; checker identity, revision and result.

The kernel's own result envelope is one observation, not the correctness oracle. The checker recomputes each assertion from ledgers, final state and attempt records: it reconstructs state from the ledger, compares outcomes against the compiled contracts of the recorded Build, recomputes metrics from the definitions registered before execution and compares them to the reported numbers. A driver that reports PASS without assertions, with a Build hash different from the registered Build, with a modified ledger or with a seed mismatch fails nonzero at the checker.

Case state starts UNRUN. A real attempt yields PASS, FAIL or BLOCKED; historical records remain immutable. BLOCKED records the missing resource (for example, no model credential for a campaign) and never lowers a target. A component PASS never automatically marks the parent case, requirement, hypothesis or release PASS. A hypothesis result requires the complete arm matrix with 10 seeds per task and the pre-registered metric definitions.

Evidence reuse: a change in Build hash, cassette digest, model identity or sampling settings, K, compiler or kernel revision, suite digest or metric definitions invalidates affected evidence. Reuse of unaffected receipts requires an explicit comparison of their recorded inputs; a matching case title is not a matching input.

## 3. Proposed workload and campaign profiles

These are proposals, not measurements. Campaign configuration binds them to concrete resources before execution; inability to provision a profile is BLOCKED, not permission to reduce it.

| Profile | Proposed value and interpretation |
|---|---|
| Task suite | 60 tasks in bins short (at most 5 primitives, 20 tasks), medium (6 to 25, 20 tasks) and long (26 to 120, 20 tasks); at least 12 long tasks require reusing a rule first met earlier in the Instance. Suite digest frozen in `benchmark/suite.kvx`. |
| Seeds | 10 seeds per arm and task, recorded in the ledger header. The same seeds are used across arms of one hypothesis. |
| H1 Stability | Arms frozen, drift-1, drift-3 on the full suite: 3 x 60 x 10 = 1,800 Instances. |
| H2 Direct action | Arms script, one-primitive on the full suite: 2 x 60 x 10 = 1,200 Instances. |
| H3 Long context | Ledger tail K in {5, 20, full} on the full suite, analysed per bin: 3 x 60 x 10 = 1,800 Instances. |
| H4 Controlled change | Arms changelog, version-only, silent on the six upgrade-pair tasks: 3 x 6 x 10 = 180 Instances. |
| Turn limit per bin | Proposed 15 turns short, 40 medium, 120 long; an Instance that reaches the limit without satisfying its predicate is a failure, not a timeout exclusion. |
| Per-turn budgets | From `world.kvx` of the arm's Build: cpu_ms 5000, wall_ms 30000, memory_mb 256, primitive_calls 500, adapter_calls 50 in the illustrative [example](examples/refworld/world.kvx); the one-primitive arm declares primitive_calls 1. |
| Gateway mode | Replay against the frozen cassette set for every campaign. A live run is a separately labelled profile and is never pooled with replay. |

Totals per hypothesis count Instances, not model calls; the number of turns and therefore model calls depends on outcomes and is reported from the ledgers. The suite and seed counts are the pre-registered minimum. Running fewer seeds produces a BLOCKED or partial record, never a pooled result at a smaller N.

## 4. Proposed budgets

The following provisional budgets make the design falsifiable. They are recommendations for implementation-time qualification, not achieved numbers. Every sample must be reported with hardware, operating system, sandbox implementation and the Build in use.

| Measurement | Proposed budget | Boundary | Provisional status |
|---|---|---|---|
| Primitive RPC overhead | p95 under 2 ms | Sandbox call to kernel return for a read_only primitive with trivial state, excluding adapter time. | Provisional; report with host CPU and socket implementation. |
| Turn startup | p95 under 300 ms | From accepted fenced block to first script statement, including fresh process and SDK import. | Provisional; report with Python version and sandbox technique. |
| Ledger append durability | Record fsynced before the result returns; p95 under 5 ms | Per primitive, measured at the kernel. | Provisional; report with storage device. |
| Snapshot and restore | p95 under 500 ms each for a state of 10,000 fs entries and 10,000 kv keys | Includes rng state and ledger seq. | Provisional; report state size. |
| Replay speed | At least 20 turns per second for a 40-turn Instance without a model | Replays recorded scripts through the real kernel against cassettes. | Provisional; report hardware. |
| Manual size | Under 24,000 bytes for refworld | Byte-identical prefix across all turns of a Build. | Provisional; report exact bytes per Build. |
| Context size | Manual plus notice plus K tail lines plus envelope, reported per turn | K = 20 default; K = full is unbounded by design and is reported, not budgeted. | Provisional; report per arm. |

Failed and partial turns are included in all latency accounting. Injected faults have separate unavailable intervals rather than being excluded from the report.

## 5. Stable case registry

All cases below remain **UNRUN**. The case identity covers the complete observable outcome; component tasks may implement registered subsets without qualifying the entire case. Clause ownership is recorded in [spec.kvx](spec.kvx) and the generated [traceability](traceability.md).

| Case | Scenario and fault schedule | Required independent oracle |
|---|---|---|
| CWR-001 | Parse the illustrative World Source and malformed variants: an adapter with effects, a read_only primitive with effects, a precondition with an unknown code, a world file missing memory_mb, a changelog state_shape change without a migration, duplicate keys and non-JSON values. Attempt to construct and to deserialise a failure code outside the envelope. | Every malformed input is rejected with file, section and line; valid input round-trips through the typed model byte-for-byte; no code outside the eleven-member envelope can exist in memory or on the wire. |
| CWR-002 | Compile the example source through the lint stage, then apply five seeded defects one at a time: a hidden precondition referencing a mutating primitive, a host import in lib/, an unknown key, a duplicate primitive, a missing module declaration. | Each defect stops the pipeline at the expected stage with the expected lint code; the clean source passes parse, resolve and lint. |
| CWR-003 | Run contract tests over refworld fixtures; inject a fixture that leaves a declared effect unmet, a precondition failure with no reaching fixture, and a lib defect that emits an undeclared code. | Every declared precondition failure and effect is exercised in an ephemeral Instance; each injected defect fails the build naming the primitive. Repeating each primitive under equal visible conditions yields equal status, shape and side-effect class. |
| CWR-004 | Compile each upgrade-pair fixture: undocumented stricter precondition, documented stricter precondition, documentation-only change, state_shape change without migration, under-bumped version. | An independent recomputation of the ContractDiff from both contract sets matches the compiler's; uncovered entries and under-bumped versions are refused; state-shape changes require a named existing migration; derived bumps are major, minor, patch as declared. |
| CWR-005 | Compile refworld twice on two hosts with different working directories, locales and clock settings; flip one byte of a packaged file; render the manual. | Identical Build hashes across hosts; the one-byte change is detected at load; every primitive appears exactly once in the manual with ordered preconditions, codes, side-effect class and the content-not-guaranteed label where declared. |
| CWR-006 | Create an Instance, append records, snapshot, mutate, restore; attempt to read host time through every primitive; draw randoms in two Instances with equal seed and ledger prefix; crash the writer mid-append. | Restored state, seq, world_time, rng state and K are identical; no primitive reveals host time; equal seeds and prefixes draw equal randoms; a truncated trailing record is refused without explicit recovery. |
| CWR-007 | From inside a turn script: import an unlisted module, call an unlisted primitive, open a socket, read the host filesystem, read the host clock, exceed cpu_ms, wall_ms, memory_mb, primitive_calls and adapter_calls. Edit the World Source while the Instance runs. | Every unlisted capability is refused as a closure violation; each exceeded budget returns LIMIT naming the limit; the running Instance observes no rule change. |
| CWR-008 | Call mutating primitives with a failing first, second and last precondition; run a script that fails after one successful mutation; call fs.write twice under equal visible conditions; inject a lib defect that passes preconditions and then emits an undeclared code. | State after a failed precondition equals state before; the first failing precondition in declared order is reported with primitive and index; the partial script applied exactly the successful mutations and returned status partial with trace; equal outcomes under equal conditions; the lib defect is recorded as INVARIANT_VIOLATION. |
| CWR-009 | Against the refworld stub servers: schedule a 503, a stalled response past timeout_ms, a duplicate key with different payload, an identical replay of a keyed request, a malformed body; then run replay mode with the network namespace unreachable. | Stub servers observed exactly the requests the ledger records with no retries; each response maps only to the declared code (UNAVAILABLE, TIMEOUT, DUPLICATE_KEY, original receipt, INVALID); world_time advanced by the declared tick regardless of host duration; replay produced identical outcomes and opened no socket; a cassette miss returned UNAVAILABLE. |
| CWR-010 | Run a turn with one external call and kill the kernel at three boundaries: before execute, after execute before ledger append, after append. Then replay a 40-turn Instance from seed, initial state, Build and cassettes. | After each crash the stub server saw exactly one request, the ledger records the interrupted turn as status error code UNAVAILABLE and no external primitive re-executes; the replayed ledger is identical except host_ts and duration_us. |
| CWR-011 | Upgrade live Instances across U1 through U6; inject a failing migration; call world.propose repeatedly; attempt an upgrade to a lower version and to a different world name; search the kernel, harness and SDK for write paths into build/ or xbuild/. | Only cwr build creates Builds; upgrades are atomic with rollback to the prior snapshot and Build on failure; notices match the frozen expectations and name every changed primitive the Instance used; proposals change nothing observable inside the world; lower-version and foreign-name upgrades are refused. |
| CWR-012 | Render 50 turns across two Instances of one Build; submit a turn with zero blocks and a turn with two blocks; compare the rendered manual against the Build's manual.md; enumerate every byte of context. | The manual prefix is byte-identical across turns and Instances; invalid turns execute nothing and advance no tick; the context contains only manual, notice, ledger tail of K lines and the envelope; the ten sections and every primitive entry are present. |
| CWR-013 | Build drift-1 (injection at turn 8 on fs.delete), drift-3, silent-change, version-only and one-primitive Builds from the control source; load each with and without --allow-experimental; run past turn 8. | Each experimental Build's ContractDiff against control lists only flag-attributable entries; loading without the gate is refused; the drift fires at turn 8 with a ledger record of kind drift while the manual is byte-identical; the three notice renderings differ only as declared; the second primitive call in the one-primitive arm returns LIMIT with primitive_calls=1. |
| CWR-014 | Compile refworld; run contract tests and stub schedules covering every declared failure of every primitive and adapter; replay the frozen cassette set offline. | Every declared failure is reached; stub servers emit only declared codes; the cassette digest matches `benchmark/suite.kvx` and replays without network. |
| CWR-015 | Run every reference solution in replay mode twice; recount primitive calls from ledgers; inspect predicate imports. | Reference solutions satisfy their predicates; bin membership follows recorded primitive counts; predicates are deterministic across runs and import neither the kernel crate nor any text parser. |
| CWR-016 | Compile both sides of each pair U1 through U6; upgrade a live Instance per pair; run the pre-upgrade and post-upgrade reference solutions. | Each pair compiles with the derived bump and the frozen ContractDiff; the post-upgrade solution succeeds and the pre-upgrade solution fails with the documented code; notices match the frozen expectations. |
| CWR-017 | Scan `benchmark/harness`, arms, prompts and campaign files for references to any system outside CWR; run two seeds of one short task with a real model in replay mode; run one live-mode turn. | No match in the scan; metrics recomputed from the ledgers alone equal the harness output; the live-mode run is labelled and excluded from pooled results. |
| CWR-018 | Execute the full matrices: H1 frozen, drift-1, drift-3 on 60 tasks x 10 seeds; H2 script, one-primitive on 60 x 10; H3 K in {5, 20, full} on 60 x 10; H4 changelog, version-only, silent on 6 pairs x 10. Recompute every report number from the ledgers. | Complete matrices exist with registered arms and seeds in every ledger header; drift records appear at scheduled turns; the three H4 Builds differ only in notice rendering by ContractDiff; report numbers equal the independent recomputation; pre-registration digests match the executed metric definitions. |
| CWR-019 | Submit attempt records claiming PASS with no assertions, with a Build hash different from the registered Build, with a modified ledger and with a mismatched seed; submit a component PASS for a registered subset. | Each trap fails nonzero at the checker; the checker is a separate program from the kernel; the component PASS leaves the parent case UNRUN. |
| CWR-020 | Run the authoring validator; tamper a case status and a task status; run every documented command against the packaged artifacts; assemble closure with one case missing. | Validation passes without executing product code and fails on tampering; every documented command runs and every evidence link resolves; closure names the missing case and refuses a release claim. |

Cases CWR-001 through CWR-016, CWR-019 and CWR-020 are deterministic drivers that need no model. CWR-017 drives a real model for its harness smoke and CWR-018 drives a real model for every campaign Instance; both run in replay mode against the frozen cassette set, and a missing model credential blocks those cases without invalidating any separately established component result. No case compares a CWR arm with anything that is not a CWR Build.

## 6. Ordered execution and closure

| Stage | Work | Exit requirement |
|---|---|---|
| A: authoring | Strict KVX parsing, clause coverage, symmetric task and case mappings, example cross-file checks, links and derived views. | Reviewable design; every task pending and every case UNRUN. |
| B: contract crate and compiler components | Task 0.3 crate, task 0.2 registry and checker, tasks 1.1 through 1.3 pipeline, contract tests, diff, gate, packaging and generation. | CWR-001 through CWR-005 and CWR-019 PASS through real crate code paths. |
| C: kernel components and sandbox closure | Tasks 2.1 and 2.2: state, ledger, clock, RNG, snapshots, sandbox, RPC, atomic contract check, limits. | CWR-006, CWR-007 and CWR-008 PASS; no unlisted capability reachable from a script. |
| D: integration | Tasks 2.3, 2.4, 2.5, 3.1 and 3.2: gateway modes, recovery and replay, upgrade and notice, context and protocol, ablation Builds. | CWR-009 through CWR-013 PASS; drift, silent, version-only and one-primitive Builds differ from control only as declared. |
| E: reference world, suite and upgrade pairs | Tasks 4.1, 4.2 and 4.3: refworld, stub servers, cassettes, 60 tasks with predicates, U1 through U6. | CWR-014, CWR-015 and CWR-016 PASS; suite, cassette and upgrade digests frozen. |
| F: campaigns | Task 5.1 harness and metrics, tasks 5.2 and 5.3 campaigns H1 through H4 with a real model in replay mode, pre-registration before any run. | CWR-017 and CWR-018 PASS; four research reports whose numbers equal recomputation from ledgers. |
| G: documentation, release and closure | Task 6.1 documentation and release pins, task 6.2 closure and owner acceptance. | CWR-020 PASS; every case has an executed outcome; unresolved required failures block the release claim. |

Stage order controls qualification; implementation tasks still follow their declared dependency graph in [implementation-plan.md](implementation-plan.md). Task 5.2 owns H1 and H2 execution, task 5.3 owns H3 and H4 execution, and task 6.2 owns closure. No campaign, Build, cassette or model call is created by specification authoring.

## 7. Authoring validation

Run from the repository root:

```sh
python3 spec/cwr/tools/validate_spec.py
```

Regenerate the derived views after changing clauses, tasks or cases:

```sh
python3 spec/cwr/tools/validate_spec.py --render
```

The validator parses `spec.kvx` strictly, checks consecutive acceptance clauses containing SHALL, task identity and wave agreement, dependency order and cycles, symmetric task and case mappings, clause coverage by both a task and a case, the fixed verify_cmd form, scope paths, the example World Source under [examples/refworld](examples/refworld/world.kvx) for cross-file consistency, [qualification.kvx](qualification.kvx) structure, trailing whitespace, local links and stale derived views. It writes `authoring-validation.json`.

It does not compile a world, run a kernel, call a model, execute a driver or read a ledger. Its PASS is an authoring receipt and is never product evidence; closure in task 6.2 does not accept it as an input.

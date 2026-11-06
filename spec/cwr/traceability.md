# CWR traceability

Generated from [spec.kvx](spec.kvx). A clause `N.M` is `[req.N] ac_M`. Mappings describe coverage ownership, not execution evidence. All cases are UNRUN.

Source digest (requirements, clauses, tasks and cases): `sha256:3fb72a8c1861553c7aabce94a450c9ace4a7353559327b5da8342f2a0b214bbc`.

| Clause | Requirement | Implementation tasks | Cases | Contract |
|---|---|---|---|---|
| 1.1 | Closure and stable rules | 2.2 | CWR-007 | [design.body.md](design.body.md) |
| 1.2 | Closure and stable rules | 2.2 | CWR-007 | [design.body.md](design.body.md) |
| 1.3 | Closure and stable rules | 2.2, 2.3 | CWR-008, CWR-009 | [design.body.md](design.body.md) |
| 1.4 | Closure and stable rules | 1.3 | CWR-005 | [design.body.md](design.body.md) |
| 1.5 | Closure and stable rules | 2.1 | CWR-006 | [design.body.md](design.body.md) |
| 1.6 | Closure and stable rules | 1.2, 2.2 | CWR-003, CWR-008 | [design.body.md](design.body.md) |
| 2.1 | Visibility and predictable failure | 1.1 | CWR-002 | [world-source.md](world-source.md) |
| 2.2 | Visibility and predictable failure | 1.1, 2.2 | CWR-002, CWR-008 | [world-source.md](world-source.md) |
| 2.3 | Visibility and predictable failure | 0.3 | CWR-001 | [world-source.md](world-source.md) |
| 2.4 | Visibility and predictable failure | 1.3, 3.1 | CWR-005, CWR-012 | [world-source.md](world-source.md) |
| 2.5 | Visibility and predictable failure | 1.3, 3.1 | CWR-005, CWR-012 | [world-source.md](world-source.md) |
| 2.6 | Visibility and predictable failure | 2.2 | CWR-008 | [world-source.md](world-source.md) |
| 3.1 | World Source and rule contracts | 0.3, 1.1 | CWR-001, CWR-002 | [world-source.md](world-source.md) |
| 3.2 | World Source and rule contracts | 0.3 | CWR-001 | [world-source.md](world-source.md) |
| 3.3 | World Source and rule contracts | 0.3 | CWR-001 | [world-source.md](world-source.md) |
| 3.4 | World Source and rule contracts | 0.3 | CWR-001 | [world-source.md](world-source.md) |
| 3.5 | World Source and rule contracts | 1.1 | CWR-002 | [world-source.md](world-source.md) |
| 3.6 | World Source and rule contracts | 0.3, 1.2, 2.5 | CWR-001, CWR-004, CWR-011 | [world-source.md](world-source.md) |
| 4.1 | Compiler pipeline and immutable Build artifact | 1.1 | CWR-002 | [compiler.md](compiler.md) |
| 4.2 | Compiler pipeline and immutable Build artifact | 1.2 | CWR-003 | [compiler.md](compiler.md) |
| 4.3 | Compiler pipeline and immutable Build artifact | 1.2 | CWR-004 | [compiler.md](compiler.md) |
| 4.4 | Compiler pipeline and immutable Build artifact | 1.2 | CWR-004 | [compiler.md](compiler.md) |
| 4.5 | Compiler pipeline and immutable Build artifact | 1.3 | CWR-005 | [compiler.md](compiler.md) |
| 4.6 | Compiler pipeline and immutable Build artifact | 1.3 | CWR-005 | [compiler.md](compiler.md) |
| 5.1 | Controlled change | 2.5 | CWR-011 | [kernel.md](kernel.md) |
| 5.2 | Controlled change | 2.5, 4.3 | CWR-011, CWR-016 | [kernel.md](kernel.md) |
| 5.3 | Controlled change | 2.5, 3.2, 4.3, 5.3 | CWR-011, CWR-013, CWR-016, CWR-018 | [kernel.md](kernel.md) |
| 5.4 | Controlled change | 2.5 | CWR-011 | [kernel.md](kernel.md) |
| 5.5 | Controlled change | 1.2, 2.5 | CWR-004, CWR-011 | [kernel.md](kernel.md) |
| 6.1 | Kernel execution and script semantics | 2.2 | CWR-007 | [kernel.md](kernel.md) |
| 6.2 | Kernel execution and script semantics | 2.2 | CWR-008 | [kernel.md](kernel.md) |
| 6.3 | Kernel execution and script semantics | 2.2 | CWR-008 | [kernel.md](kernel.md) |
| 6.4 | Kernel execution and script semantics | 2.2 | CWR-008 | [kernel.md](kernel.md) |
| 6.5 | Kernel execution and script semantics | 2.2 | CWR-007 | [kernel.md](kernel.md) |
| 6.6 | Kernel execution and script semantics | 2.1, 2.4 | CWR-006, CWR-010 | [kernel.md](kernel.md) |
| 7.1 | Adapter gateway and external effects | 2.3 | CWR-009 | [kernel.md](kernel.md) |
| 7.2 | Adapter gateway and external effects | 2.3 | CWR-009 | [kernel.md](kernel.md) |
| 7.3 | Adapter gateway and external effects | 2.3 | CWR-009 | [kernel.md](kernel.md) |
| 7.4 | Adapter gateway and external effects | 2.3 | CWR-009 | [kernel.md](kernel.md) |
| 7.5 | Adapter gateway and external effects | 2.3, 4.1 | CWR-009, CWR-014 | [kernel.md](kernel.md) |
| 7.6 | Adapter gateway and external effects | 2.3 | CWR-009 | [kernel.md](kernel.md) |
| 8.1 | Instance state, ledger and recovery | 2.1 | CWR-006 | [kernel.md](kernel.md) |
| 8.2 | Instance state, ledger and recovery | 2.1 | CWR-006 | [kernel.md](kernel.md) |
| 8.3 | Instance state, ledger and recovery | 2.4 | CWR-010 | [kernel.md](kernel.md) |
| 8.4 | Instance state, ledger and recovery | 2.4 | CWR-010 | [kernel.md](kernel.md) |
| 8.5 | Instance state, ledger and recovery | 2.4, 5.1 | CWR-010, CWR-017 | [kernel.md](kernel.md) |
| 9.1 | Model surface | 3.1 | CWR-012 | [model-surface.md](model-surface.md) |
| 9.2 | Model surface | 1.3, 3.1 | CWR-005, CWR-012 | [model-surface.md](model-surface.md) |
| 9.3 | Model surface | 3.1 | CWR-012 | [model-surface.md](model-surface.md) |
| 9.4 | Model surface | 2.1, 3.1 | CWR-006, CWR-012 | [model-surface.md](model-surface.md) |
| 9.5 | Model surface | 3.1 | CWR-012 | [model-surface.md](model-surface.md) |
| 10.1 | Experimental ablation builds | 3.2 | CWR-013 | [compiler.md](compiler.md) |
| 10.2 | Experimental ablation builds | 3.2, 5.2 | CWR-013, CWR-018 | [compiler.md](compiler.md) |
| 10.3 | Experimental ablation builds | 3.2, 5.3 | CWR-013, CWR-018 | [compiler.md](compiler.md) |
| 10.4 | Experimental ablation builds | 3.2, 5.2 | CWR-013, CWR-018 | [compiler.md](compiler.md) |
| 10.5 | Experimental ablation builds | 3.2 | CWR-013 | [compiler.md](compiler.md) |
| 11.1 | Reference world and task suite | 4.1 | CWR-014 | [reference-world.md](reference-world.md) |
| 11.2 | Reference world and task suite | 4.2 | CWR-015 | [reference-world.md](reference-world.md) |
| 11.3 | Reference world and task suite | 4.2 | CWR-015 | [reference-world.md](reference-world.md) |
| 11.4 | Reference world and task suite | 4.3 | CWR-016 | [reference-world.md](reference-world.md) |
| 11.5 | Reference world and task suite | 4.1 | CWR-014 | [reference-world.md](reference-world.md) |
| 12.1 | Experiment harness and hypotheses | 5.1 | CWR-017 | [experiments.md](experiments.md) |
| 12.2 | Experiment harness and hypotheses | 5.2, 5.3 | CWR-018 | [experiments.md](experiments.md) |
| 12.3 | Experiment harness and hypotheses | 5.1 | CWR-017 | [experiments.md](experiments.md) |
| 12.4 | Experiment harness and hypotheses | 5.1 | CWR-017 | [experiments.md](experiments.md) |
| 12.5 | Experiment harness and hypotheses | 5.2, 5.3 | CWR-018 | [experiments.md](experiments.md) |
| 12.6 | Experiment harness and hypotheses | 5.1 | CWR-017 | [experiments.md](experiments.md) |
| 13.1 | Qualification and evidence | 0.1, 6.2 | CWR-020 | [verification.md](verification.md) |
| 13.2 | Qualification and evidence | 0.2 | CWR-019 | [verification.md](verification.md) |
| 13.3 | Qualification and evidence | 0.2 | CWR-019 | [verification.md](verification.md) |
| 13.4 | Qualification and evidence | 0.2, 6.2 | CWR-019 | [verification.md](verification.md) |
| 13.5 | Qualification and evidence | 0.1 | CWR-020 | [verification.md](verification.md) |
| 14.1 | Documentation and release | 0.1, 6.2 | CWR-020 | [README.md](README.md) |
| 14.2 | Documentation and release | 6.1 | CWR-020 | [README.md](README.md) |
| 14.3 | Documentation and release | 6.1 | CWR-020 | [README.md](README.md) |
| 14.4 | Documentation and release | 6.2 | CWR-020 | [README.md](README.md) |

## Case ownership

A case's tasks are the packages responsible for its driver, behaviour or final integration. Full-case completion requires every registered assertion described in [verification.md](verification.md).

| Case | Scope | Tasks | Independent oracle |
|---|---|---|---|
| CWR-001 | Contract grammar and closed failure envelope (component) | 0.3 | Malformed source is rejected with located errors; valid source round-trips through the typed model; no code outside the envelope can be constructed or parsed. |
| CWR-002 | Compiler closure and visibility lints (component) | 1.1 | Each seeded defect stops the pipeline at the expected stage with the expected lint code; a clean source passes every stage up to lint. |
| CWR-003 | Contract tests and rule determinism (component) | 1.2 | Every declared precondition failure and effect is exercised in an ephemeral Instance; an undeclared code or unmet effect fails the build naming the primitive. |
| CWR-004 | ContractDiff, changelog gate and version derivation (component) | 1.2 | Independent recomputation of the diff from both contract sets matches; uncovered diff entries and under-bumped versions are refused; state-shape changes require a migration. |
| CWR-005 | Immutable, deterministic Build artifacts and generated manual (component) | 1.3 | Two compilations on different hosts hash identically; a one-byte change is detected; every primitive's manual entry equals its compiled contract. |
| CWR-006 | Ledger, snapshot, world clock and RNG (component) | 2.1 | Restored snapshots are observably identical; ledger records are durable before results return; no primitive reveals host time; equal seeds and prefixes draw equal randoms. |
| CWR-007 | Sandbox closure and resource limits (component) | 2.2 | Every unlisted capability is refused from inside the script; exceeding cpu, wall, memory, primitive or adapter budgets returns LIMIT naming the limit; rules are unchanged across the Instance's life. |
| CWR-008 | Atomic primitives, ordered preconditions and partial scripts (integration) | 2.2 | State after a failed precondition equals state before; the first failing precondition in declared order is reported; partial scripts apply exactly the successful mutations; repeated calls under equal visible conditions yield equal outcomes. |
| CWR-009 | Adapter gateway normalisation, idempotency and cassette replay (integration) | 2.3, 4.1 | Stub servers observe exactly the requests the ledger records with no retries; every response maps to a declared code; replay produces identical outcomes with no socket opened. |
| CWR-010 | Crash recovery and ledger replay (integration) | 2.4 | After a crash at any turn boundary the stub server saw one request and the ledger records the interrupted turn as UNAVAILABLE; a full replay reproduces the ledger except host fields. |
| CWR-011 | Controlled upgrade, migrations, recompile notice and outbox (integration) | 2.5, 4.3 | Only cwr build creates Builds; upgrades are atomic with rollback on failure; notices match the frozen expectation; proposals change nothing observable inside the world. |
| CWR-012 | Context composition and turn protocol (integration) | 3.1 | The manual prefix is byte-identical across turns and Instances; the context contains only the four components; invalid turns execute nothing. |
| CWR-013 | Experimental ablation builds and kernel gate (integration) | 3.2 | Each experimental Build's ContractDiff against control lists only flag-attributable entries; drift fires at the scheduled turn with a ledger record and an unchanged manual; the kernel gate holds. |
| CWR-014 | Reference world rules, stub servers and cassettes (integration) | 4.1 | Every declared failure of every refworld primitive and adapter is reachable; stub servers emit only declared codes; the cassette digest is frozen and replays offline. |
| CWR-015 | Task suite bins and success predicates (integration) | 4.2 | Reference solutions succeed under their predicates; bin membership follows recorded primitive counts; predicates are deterministic and read only state and ledger. |
| CWR-016 | Upgrade pairs U1 through U6 (integration) | 4.3 | Each pair compiles with the derived bump and frozen diff; the post-upgrade solution succeeds and the pre-upgrade solution fails with the documented code. |
| CWR-017 | Harness isolation and ledger-only metrics (integration) | 5.1 | No harness file references another agent or harness; metrics recomputed from ledgers alone match the harness output; live runs are excluded from pooled replay results. |
| CWR-018 | Hypothesis campaigns H1 through H4 (campaign) | 5.2, 5.3 | Complete arm matrices with 10 seeds per task exist; report numbers equal an independent recomputation from ledgers; pre-registration digests match the executed metric definitions. |
| CWR-019 | Evidence registry and false-success traps (component) | 0.2 | Zero-assertion, tampered, stale-Build and mismatched-seed evidence fails nonzero; the checker is a separate program from the kernel; component PASS leaves the parent case UNRUN. |
| CWR-020 | Specification, documentation and release closure (release) | 0.1, 6.1, 6.2 | Authoring validation passes without executing product code; documented commands run against packaged artifacts; closure lists every case with an executed outcome and owner acceptance, or names what blocks it. |

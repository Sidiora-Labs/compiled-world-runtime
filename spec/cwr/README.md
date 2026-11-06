<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Compiled World Runtime (CWR)

Status: **design draft; every task is pending and every case is UNRUN in `spec.kvx`; nothing is qualified**. Implementation work may be present in the tree while tasks are in progress.

This feature specifies the Compiled World Runtime: the harness around the model becomes a frozen, rule-governed *world* the model lives in, rather than a body the model operates. A human-edited World Source is compiled into an immutable, content-addressed Build. A kernel runs many Instances of one Build, executing one sandboxed script per turn through atomic primitives whose outcomes depend only on visible conditions. The only path to a different world is human recompilation into a new versioned Build with a changelog the model is shown.

Three pillars govern the design. **Stability**: rules are fixed for the life of a Build, and the same action under the same visible conditions yields the same outcome. **Direct action**: tools, CLI and scripting are native to the world, so one script replaces a long chain of tool calls. **Controlled change**: only a human recompiles, every Build is versioned and every change is documented and announced. Four hypotheses are tested, H1 Stability, H2 Direct action, H3 Long context and H4 Controlled change, using internal ablation Builds only. No arm, metric or document in this feature compares against or depends on any system outside CWR.

## Read in this order

| Document | Purpose |
|---|---|
| [Specification](spec.kvx) | Normative requirements, acceptance clauses, proposed implementation tasks, dependencies and UNRUN qualification cases. |
| [Design](design.body.md) | Layers, responsibility boundaries, governing invariants, one turn end to end, one upgrade end to end. |
| [Decisions](decisions.md) | Fixed constraints, proposed implementation choices with the evidence each needs, and decisions deferred to implementation. |
| [World Source](world-source.md) | Source layout, rule and adapter contract grammar, precondition language, failure envelope, changelog and migrations. |
| [Compiler](compiler.md) | Pipeline, lint codes, contract tests, ContractDiff, changelog gate, Build artifact, experimental ablation flags. |
| [Kernel](kernel.md) | Instances, sandboxed turns, atomic primitives, adapter gateway, ledger, snapshots, recovery, upgrade and outbox. |
| [Model surface](model-surface.md) | Context composition, manual structure, turn protocol, result envelope, recompile notice, ledger tail. |
| [Reference world](reference-world.md) | refworld modules and rules, stub servers and cassettes, task suite and bins, upgrade pairs U1 to U6. |
| [Experiments](experiments.md) | Hypotheses H1 to H4, arms, seeds, ledger-only metrics, analysis and pre-registration. |
| [Verification](verification.md) | Evidence contract, proposed profiles and budgets, the stable case registry and ordered execution stages. |
| [Implementation plan](implementation-plan.md) | Generated task order, scope and ownership from the specification. |
| [Traceability](traceability.md) | Generated clause links to tasks, cases and contract documents. |
| [Qualification](qualification.kvx) | Recorded authoring runs and their explicit non-evidence status. |

Diagrams: [architecture](diagrams/architecture.mmd) and [one turn](diagrams/turn.mmd). Illustrative World Source files live under [examples/refworld](examples/refworld/world.kvx); they are syntax examples checked for cross-file consistency, not a compiled Build.

## Authority and evidence

`spec.kvx` is the normative requirements and task catalogue. The contract documents refine it; a contradiction between them is a defect to resolve, not a licence to pick the easier reading. `implementation-plan.md` and `traceability.md` are generated views and are never edited by hand.

The proposed implementation lives in Rust crates under `crates/` (`cwr-contract`, `cwr-compiler`, `cwr-kernel`, `cwr-cli`), a generated Python SDK under `sdk/python/cwr`, the reference world under `examples/refworld`, and the task suite and experiment harness under `benchmark/`. Those paths are recorded in task scopes; code under them may exist while tasks are in progress, but its presence is not evidence that a task is complete or a case passes. Verification drivers at `tests/cwr/cases/W_N.py` are created by their tasks, and a listed command is not evidence that it exists or passes.

Run the authoring check from the repository root:

```sh
python3 spec/cwr/tools/validate_spec.py
```

Regenerate the two derived views after changing clauses, task mappings or cases:

```sh
python3 spec/cwr/tools/validate_spec.py --render
```

The check parses KVX strictly, validates references and dependency order, checks that every acceptance clause is covered by a task and a case, checks the illustrative World Source for cross-file consistency, checks local links and whitespace, and detects stale derived views; each view records a source digest over every requirement, clause, task and case, so any edit to them fails validation until re-rendered. It writes `authoring-validation.json`. It does not compile a world, run a kernel, call a model or execute a driver, and its PASS is never product evidence.

Resource limits, tick values, context sizes, seed counts, task counts and the budgets in `verification.md` are proposed choices, not measurements. A hypothesis is supported only by the complete arm matrix with pre-registered metrics computed from ledgers; component evidence never implies a hypothesis result.

<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Decisions and unresolved qualifications

Status: product design, not implementation evidence. Requirements and task statuses are in [spec.kvx](spec.kvx).

## Fixed constraints

| ID | Constraint | Consequence |
|---|---|---|
| D01 | The harness is a world, not a body | The model acts inside fixed rules; it does not operate a tool surface that changes under it. |
| D02 | A Build is frozen for its lifetime | Contracts, limits and failure codes cannot change for a running Instance without an explicit upgrade to another Build. |
| D03 | Outcome determinism over the interface, not the payload | Outcome is status, result shape and side-effect class. Adapter-backed primitives are content-not-guaranteed by contract. |
| D04 | Every rule is visible and enforced from one compiled contract | The manual is generated from contracts; preconditions reference only read_only primitives; the first failing precondition names the failure. |
| D05 | Closed failure envelope | Eleven codes. Nothing else can be emitted; an undeclared code after passed preconditions is INVARIANT_VIOLATION, a defect. |
| D06 | Direct action through one script per turn | A Python 3.12 script in a sandbox with the Build SDK only; one RPC per primitive; primitives atomic, scripts non-transactional. |
| D07 | Human-only recompilation | Only `cwr build` creates a Build; the kernel, harness and SDK have no write path into `build/` or `xbuild/`. |
| D08 | Versioned Builds with an enforced changelog | The compiler derives the minimum version bump from the ContractDiff and refuses an undocumented change. |
| D09 | The model is told about change | The recompile notice is rendered on the first turn after upgrade; only an experimental Build may alter that rendering. |
| D10 | One upward channel, text only | `world.propose` writes to the ledger and the outbox and changes nothing inside the world. |
| D11 | The ledger is the only experimental input | Metrics and oracles read ledgers and final state; kernel internals, stdout and raw payloads are not evidence. |
| D12 | No external baseline | Every arm is a CWR Build with one pillar ablated. Nothing references, invokes or compares against another agent or harness. |

## Proposed implementation choices

| ID | Proposal | Evidence needed before treating it as settled |
|---|---|---|
| P01 | Rust crates for contract model, compiler, kernel and CLI; Python 3.12 for scripts; generated Python SDK | Build determinism across hosts (CWR-005), sandbox closure and limit enforcement (CWR-007), RPC overhead within the budgets in [verification.md](verification.md). |
| P02 | KVX as the World Source format: one JSON value per line inside `[section]` blocks | Strict parser rejects every malformed fixture (CWR-001); authors can express every refworld rule without escape hatches (CWR-014). |
| P03 | Precondition expression language limited to parameters, read_only primitive calls, comparison, boolean and `len` | Every refworld rule is expressible (CWR-014); the lint catches every hidden-state reference (CWR-002). |
| P04 | Per-turn sandbox as a fresh process with no network namespace and a read-only view containing only the SDK; RPC over a unix socket | Closure probes from inside the script are refused (CWR-007); startup cost fits the turn budget. |
| P05 | Per-primitive atomicity with debug-mode effect assertion; status partial for scripts that fail after a successful mutation | State after a failed precondition equals state before; partial applies exactly the successful mutations (CWR-008). |
| P06 | Adapter gateway with live, record and replay modes and cassettes keyed by adapter, canonical request hash and sequence; never retry | Replay produces identical outcomes with no socket opened; stub servers observe exactly the ledger's requests (CWR-009). |
| P07 | PCG RNG per Instance, advanced only by `world.random`; world clock in ticks with tick_per_primitive 1 and tick_per_turn 10 | Equal seeds and prefixes draw equal randoms; no primitive reveals host time (CWR-006). |
| P08 | Content-addressed Instance store and append-only JSONL ledger with fsync before return; snapshots carry state, seq, time, rng, K and Build hash | Restore is observably identical; recovery at every turn boundary is correct (CWR-006, CWR-010). |
| P09 | Context layout manual, notice, ledger tail K, result envelope; manual byte-identical per Build; K default 20 | Manual prefix is byte-identical across turns and Instances (CWR-012); H3 tail sweep K in {5, 20, full} (CWR-018). |
| P10 | Experimental flags `--x-drift`, `--x-silent-change`, `--x-version-only`, `--x-one-primitive` under `xbuild/` behind `--allow-experimental` | Control versus experimental ContractDiff lists only flag-attributable entries (CWR-013). |
| P11 | refworld with fs, kv, jobs, search, publish and world; search and publish as adapters over stub servers | Every declared failure is reachable; cassette digest frozen (CWR-014). |
| P12 | 60 tasks, 20 per bin, at least 12 long rule-reuse tasks; 10 seeds per task; six upgrade pairs | Reference solutions succeed and bins follow recorded primitive counts (CWR-015); pairs compile with the derived bump (CWR-016). |
| P13 | Limits cpu_ms 5000, wall_ms 30000, memory_mb 256, primitive_calls 500, adapter_calls 50 | Reference solutions for long tasks complete within budget; LIMIT is reachable and named (CWR-007, CWR-015). |
| P14 | Mean and 95% bootstrap confidence interval per arm and bin with pre-registered directions | Reports equal an independent recomputation from ledgers; pre-registration digest matches (CWR-018). |
| P15 | Pre-1.0 versioning: while the major version is 0, a minor bump satisfies a derived major bump and a patch bump satisfies a derived minor; from 1.0.0 the derived bump applies literally | The changelog gate refuses an under-bumped version on both sides of 1.0.0 (CWR-004); the example world's 0.2.0 release with two major-kind changes passes the gate. |

## Decisions required during implementation

The model identity, sampling settings and turn limits per bin used in the campaigns are campaign inputs recorded in `benchmark/preregistration.kvx` before any run. The exact unix socket framing, the SDK codec for the result envelope, the canonical request hash for cassettes and the sandbox mechanism on the chosen host are implementation choices that the drivers must pin and report. None is guessed from this pack.

Contract, ledger and snapshot schemas are versioned before any Instance is created. A schema change after that point needs a migration and a compatibility note in the same way a world change does.

A campaign freezes its arms, seeds, cassette digest, suite digest and metric definitions before execution. A result cannot be turned into a pass by changing a metric or a limit after the run. A revised campaign is a new recorded profile with the original result retained.

## Relationship to the earlier draft

The first draft of this feature was seven narrative documents, `00-overview.md` through `06-reference-world.md`, written on 2026-11-06. This pack replaces them. Three things changed in the rewrite. World Source files use KVX rather than TOML, matching the repository's own configuration format and allowing one strict parser for the whole pack. The failure envelope gained INVARIANT_VIOLATION so that a kernel or Build defect can never be reported as an ordinary outcome. Every hypothesis arm is now stated as an internal ablation Build; the earlier draft's language about comparison baselines is withdrawn entirely.

<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Running CWR experiments: hypotheses, arms and analysis

Audience: experimenters. Status: **pending verification**. Harness code is landing in the tree, but no campaign has run and no hypothesis has a result; of the cases this guide relies on, only CWR-019 (the evidence checker) has an executed attempt. This guide is derived from [spec/cwr/experiments.md](../../spec/cwr/experiments.md), [spec/cwr/verification.md](../../spec/cwr/verification.md) and the experimental-build rules in [spec/cwr/compiler.md](../../spec/cwr/compiler.md) section 10. Where this guide and the contract disagree, the contract wins.

## How to read this guide

Sections that describe tool or harness behaviour open with a block naming the qualification case that would show it, the driver its task creates and the source files that implement it, split into files already in the tree (unverified) and files not yet written:

> **Pending verification.** Evidence: CWR-017 (driver `tests/cwr/cases/5_1.py`, no attempt recorded). Source in tree, unverified: `benchmark/harness/metrics.py`.

Task 6.1 removes a block only when its case has an executed attempt in the evidence registry. The expected directions in this guide are pre-registrations, not findings. Commands are shown exactly as the specification names them; a command shown here is not evidence that it exists or passes.

## 1. What is compared

> **Pending verification.** Evidence: CWR-017 (driver `tests/cwr/cases/5_1.py`) for the reference scan, no attempt recorded. Source in tree, unverified: `benchmark/harness/run.py`.

Every arm is a CWR Build. Each hypothesis removes one pillar of CWR with one experimental build flag and measures the difference against the control Build on the same task suite, the same cassettes and the same model. Nothing is compared with any system outside CWR, and no harness file, prompt, arm or metric may reference one; task 5.1's driver scans `benchmark/harness/`, `benchmark/campaigns/` and every prompt template and fails on a match. Results say what each pillar contributes inside CWR and nothing more.

## 2. Hypotheses and arms

> **Pending verification.** Evidence: CWR-013 (driver `tests/cwr/cases/3_2.py`) for drift injection and notice rendering, CWR-018 (drivers `tests/cwr/cases/5_2.py` and `tests/cwr/cases/5_3.py`) for the arm matrices, no attempt recorded. The directions are registrations, not findings.

| Hypothesis | Arms | Build flag per arm | Primary metric | Pre-registered direction |
|---|---|---|---|---|
| H1 Stability | frozen, drift-1, drift-3 | none; `--x-drift <drift.kvx>` with one injection; `--x-drift <drift.kvx>` with three | task success per bin | frozen > drift-1 > drift-3 on success; the reverse on repeated-failure rate |
| H2 Direct action | script, one-primitive | none; `--x-one-primitive` | turns to success per bin | script needs fewer turns in every bin with success at least as high |
| H3 Long context | K=5, K=20, K=full | none; the Instance is created with `--tail K` | rule reuse rate on rule-reuse tasks | H1 and H2 deltas larger in the long bin; reuse higher under frozen than drift at every K; K=full versus K=20 registered as undetermined |
| H4 Controlled change | changelog, version-only, silent | none; `--x-version-only` on Build B; `--x-silent-change` on Build B | post-upgrade misuse rate | changelog < version-only < silent; no difference on the documentation-only pair U6 |

Drift injections for H1 fall at turn floor(len/2) for drift-1 and at floor(len/4), floor(len/2) and floor(3len/4) for drift-3, where `len` is the reference solution's turn count in the frozen arm's first seed. Drift specifications per task are proposed for `benchmark/campaigns/h1.kvx`. H4 runs each upgrade pair U1 to U6: the Instance runs on Build A for the first half of the reference turn count, is upgraded to Build B at the midpoint and continues; pairs, midpoints and expected notices are proposed for `benchmark/upgrades.kvx`.

## 3. Building the arms

> **Pending verification.** Evidence: CWR-013 (driver `tests/cwr/cases/3_2.py`), no attempt recorded. Source in tree, unverified: `crates/cwr-cli/src/build.rs`. Proposed source, not yet written: `crates/cwr-compiler/src/experimental/mod.rs`.

Build the control and one experimental Build per flag from the same source. Each experimental Build is written under `xbuild/<sha256>/`; only one `--x-*` flag is allowed per Build (two are refused with exit 1).

```sh
cwr build <source>
cwr build <source> --x-drift <drift.kvx>
cwr build <source> --x-one-primitive
cwr build <source> --x-version-only
cwr build <source> --x-silent-change
```

| Flag | Changes | Must not change |
|---|---|---|
| `--x-drift <drift.kvx>` | Embeds a drift schedule; at each scheduled turn the kernel swaps the named precondition or failure in the running enforcement table and writes a `drift` ledger record | Manual, SDK, `contracts.json`, limits |
| `--x-silent-change` | `cwr instance upgrade` into this Build stores the notice but renders nothing | Contracts, manual, migrations, the changelog gate |
| `--x-version-only` | The rendered notice is the version line only | As above |
| `--x-one-primitive` | `limits.primitive_calls = 1` and one sentence stating it in the manual's limits section | Every other contract and limit |

Check attribution before using a pair; the diff between control and experimental must contain only entries attributable to the flag, and the harness refuses any other pair:

```sh
cwr diff <hash-a> <hash-b>
```

The drift file grammar (`meta.format = "cwr.drift"`, `drift.<n>.turn`, `primitive`, `change`, `expr`, `failure`, `doc`) is in [spec/cwr/compiler.md](../../spec/cwr/compiler.md) section 10, with an [example](../../spec/cwr/examples/refworld/drift/drift-1.kvx).

Every Instance of an experimental Build needs `--allow-experimental`:

```sh
cwr instance create <build-hash> [--seed N] [--tail K] [--arm LABEL] [--from-snapshot S] [--allow-experimental]
```

## 4. Shared setup

These values are proposed choices fixed before execution. They are not derived from measurements and are not power calculations.

| Item | Proposed value | Recorded in |
|---|---|---|
| Control Build | refworld with no experimental flag | `benchmark/harness/arms.kvx` |
| Model and sampling settings | One fixed identity and fixed settings for all arms | Ledger header and attempt record |
| Gateway mode | replay against the frozen cassette set | Cassette digest in the ledger header |
| Context layout | manual, notice, ledger tail K, last envelope; K = 20 unless the arm changes it | K in the ledger header |
| Seeds | 10 per task per arm, the same seeds across arms of one hypothesis | Ledger header |
| Task suite | 60 tasks: 20 short, 20 medium, 20 long, at least 12 long rule-reuse tasks | Suite digest in `benchmark/suite.kvx` |
| Success | `success.py` predicate over final state and ledger | Attempt record |

Proposed Instance counts per hypothesis: H1 1,800 (3 arms x 60 tasks x 10 seeds), H2 1,200, H3 1,800, H4 180 (3 arms x 6 pairs x 10 seeds). Running fewer seeds yields a BLOCKED or partial record, never a pooled result at a smaller N. The model identity, sampling settings and turn limits are campaign inputs recorded in `benchmark/preregistration.kvx` before any run.

## 5. Pre-registration

> **Pending verification.** Evidence: CWR-018 (drivers `tests/cwr/cases/5_2.py` and `tests/cwr/cases/5_3.py`), no attempt recorded. Source in tree, unverified: `benchmark/preregistration.kvx`, `benchmark/analyse.py`.

`benchmark/preregistration.kvx` is committed before any campaign. Per hypothesis it records the arms with their Build hashes, the task set, the primary and secondary metrics, the expected direction and the sha256 of `benchmark/harness/metrics.py`. The analysis refuses to run when the current metrics file digest differs from the registered one. A metric change after execution is a new pre-registration and a new campaign; earlier results stay under their original registration.

## 6. Running a campaign

> **Pending verification.** Evidence: CWR-017 (driver `tests/cwr/cases/5_1.py`) and CWR-018, no attempt recorded. Source in tree, unverified: `benchmark/harness/run.py`, `benchmark/harness/model.py`.

The runner `benchmark/harness/run.py` is proposed to create, for each arm, task and seed, an Instance of the arm's Build (with `--allow-experimental` where needed), drive turns through the model adapter with the fixed context layout in replay mode and stop at success, turn limit or budget exhaustion. Its command-line interface is not named in the specification; task 5.1 must pin it before this guide can show an invocation.

One ledger per arm, task and seed:

```text
benchmark/results/h<N>/<arm>/<task>/<seed>/ledger.jsonl
benchmark/results/h<N>/<arm>/<task>/<seed>/final_state.kvx
benchmark/results/h<N>/<arm>/<task>/<seed>/attempt.kvx
```

The attempt record holds Build hash, Instance id, seed, cassette digest, model identity, compiler and kernel revisions, arm, start and end, exit status, log path and the harness-recorded token counts. A missing cell makes the matrix incomplete and the campaign cannot close.

## 7. Metrics

> **Pending verification.** Evidence: CWR-017 (driver `tests/cwr/cases/5_1.py`), no attempt recorded. Source in tree, unverified: `benchmark/harness/metrics.py`.

Every metric is computed from ledgers and final state only; the full normative definitions are in [spec/cwr/experiments.md](../../spec/cwr/experiments.md) section 4.

| Metric | In one line |
|---|---|
| task success | `success.py` true for the final state and ledger; 1 or 0 per Instance |
| turns to success | First turn after which the predicate holds; failures reported separately, never as a large number |
| primitive calls | Count of `primitive` records |
| failure rate by code | Failing records per code divided by primitive calls |
| repeated-failure rate | Failing records that repeat an earlier failure of the same primitive and code in the same Instance, over all failing records |
| rule reuse rate | Rule-reuse tasks whose later encounter of the reused rule shows no failure on that rule |
| post-upgrade misuse rate | Post-upgrade turns calling a changed primitive under its old rule, over post-upgrade turns |
| partial rate | Turns with status partial over all turns |
| probing ratio | read_only calls per mutating or external call; a secondary H1 proxy only |

Tokens at the model boundary are harness-recorded per turn in the attempt record, not a ledger quantity, and are reported only as a secondary H2 measure labelled as harness-recorded.

## 8. Analysis

> **Pending verification.** Evidence: CWR-018 (drivers `tests/cwr/cases/5_2.py` and `tests/cwr/cases/5_3.py`), no attempt recorded. Source in tree, unverified: `benchmark/analyse.py`.

```sh
python3 benchmark/analyse.py
```

The analysis reads ledgers and attempt records under `benchmark/results/` and the registered definitions in `benchmark/preregistration.kvx`. For every arm and bin it reports the mean of each metric across tasks and seeds with a 95% bootstrap confidence interval from 10,000 resamples over Instances. Bins are never pooled. Drift arms are reported per drift specification and pooled. Ledgers with different cassette digests are never pooled. The command's arguments are not named in the specification; task 5.1 must pin them. The four research reports are proposed for `research/h1-stability.md`, `research/h2-direct-action.md`, `research/h3-long-context.md` and `research/h4-controlled-change.md`; none exists.

## 9. Live versus replay

> **Pending verification.** Evidence: CWR-017 (driver `tests/cwr/cases/5_1.py`), no attempt recorded.

All campaigns run in replay mode. A live-mode run of the frozen arm against the stub servers is permitted as a robustness check; its attempt records carry `mode = "live"`, it is written under `benchmark/results/live/`, and it is never pooled with replay results. Live runs against real external services are out of scope for the first campaign.

## 10. What counts as a result

> **Verified, component tier.** Evidence: CWR-019, attempt record [tests/cwr/evidence/CWR-019/20261106T100236Z-1.json](../../tests/cwr/evidence/CWR-019/20261106T100236Z-1.json), outcome PASS, accepted by the independent checker and written to [tests/cwr/registry.kvx](../../tests/cwr/registry.kvx) by the runner on 2026-11-06. Source: [tests/cwr/qualify.py](../../tests/cwr/qualify.py), [tests/cwr/check_evidence.py](../../tests/cwr/check_evidence.py), driver [tests/cwr/cases/0_2.py](../../tests/cwr/cases/0_2.py). The case status in `spec/cwr/spec.kvx` stays UNRUN until task 6.2 updates statuses from the registry. This covers the runner and checker only; nothing it shows says anything about a campaign or a hypothesis.

Qualification attempts run through one runner, which executes a case's task drivers, invokes the independent checker and appends one immutable attempt record to the hash-chained index `tests/cwr/evidence/index.jsonl`:

```text
python3 tests/cwr/qualify.py --case CWR-NNN
python3 tests/cwr/qualify.py --case CWR-NNN --task W.N
python3 tests/cwr/qualify.py --task W.N
python3 tests/cwr/qualify.py --check-registry
```

The first form qualifies a whole case; the `--task` forms record a component attempt for one task's registered subset, which never changes the case status; `--check-registry` validates the registry against the specification. The checker is a separate program that reads only attempt records, ledgers, final state and the registry, and refuses zero-assertion, tampered, stale-Build and mismatched-seed evidence. It can be run on its own:

```text
python3 tests/cwr/check_evidence.py <record.json>
python3 tests/cwr/check_evidence.py --status CWR-NNN
```

- A hypothesis result requires the complete arm matrix with 10 seeds per task and the pre-registered metric definitions. A component PASS never implies a hypothesis result.
- A campaign whose result contradicts its registered direction is reported as such.
- A changed Build hash, cassette digest, model identity or sampling settings, K, compiler or kernel revision, suite digest or metric definition invalidates the affected evidence.
- Any run containing `INVARIANT_VIOLATION` is excluded and reported, because the Build that produced it is defective.

## 11. Threats to validity

| Threat | Mitigation |
|---|---|
| Model nondeterminism | 10 seeds per task; fixed, recorded sampling settings; variance reported |
| Cassette staleness | Cassette digest in every ledger header; ledgers with different digests are not pooled |
| Task leakage | Reference solutions show each task needs its listed rules; tasks with no rule-dependent primitive are excluded |
| K confounds | K fixed at 20 for H1 and H2; swept only in H3 |
| Drift schedule choice | Specifications drawn from each task's `rules` file by a seeded rule, committed before the run and reported per specification |
| One-primitive arm is artificial | Stated as the cleanest in-world ablation of direct action; no claim beyond CWR |
| Notice wording | The version-only arm isolates the version signal; U6 controls for notice noise |
| Replay removes external variance | Replay is the registered condition; the live robustness run is reported separately |

## 12. Open points in the contract

Differences between specification chapters that this guide does not resolve:

- Turn limit: [spec/cwr/experiments.md](../../spec/cwr/experiments.md) section 2 proposes 200 turns per Instance; [spec/cwr/verification.md](../../spec/cwr/verification.md) section 3 proposes 15 short, 40 medium and 120 long. Owner: task 5.1, recorded in `benchmark/preregistration.kvx`.
- H3 task set: [spec/cwr/experiments.md](../../spec/cwr/experiments.md) section 3.3 runs the K sweep on the 20 long tasks; [spec/cwr/verification.md](../../spec/cwr/verification.md) section 3 counts H3 as 3 x 60 x 10 Instances on the full suite. Owner: task 5.3.
- No command-line interface is named for `benchmark/harness/run.py` or for the arguments of `benchmark/analyse.py`. Owner: task 5.1.

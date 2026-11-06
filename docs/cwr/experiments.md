<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: Sidiora DEV OSS-1.0 -->

# Running CWR experiments: hypotheses, arms and analysis

Audience: experimenters. Status on 2026-11-06: **implemented, not yet qualified; no campaign result exists**. The experimental Builds, the harness, the pre-registration and the analysis are in the tree and the commands in this guide run against the packaged release, but no campaign has completed, no hypothesis has a result, and of the cases this guide relies on only CWR-019 (the evidence checker) has an accepted attempt. This guide is derived from [spec/cwr/experiments.md](../../spec/cwr/experiments.md), [spec/cwr/verification.md](../../spec/cwr/verification.md) and the experimental-build rules in [spec/cwr/compiler.md](../../spec/cwr/compiler.md) section 10. Where this guide and the contract disagree, the contract wins.

## How to read this guide

Sections that describe tool or harness behaviour open with a status marker, using the convention of [world-source.md](world-source.md#how-to-read-this-guide): **Verified** links an accepted attempt record, **Implemented, not yet qualified** links shipped source whose case has no accepted attempt, **Proposed, not implemented** names source that does not exist yet. The expected directions in this guide are pre-registrations, not findings.

Every `sh` block runs, in order and in one shell, from the root of an unpacked release bundle with the packaged `cwr` on `PATH` (see [Install](../../README.md#install)); the release driver [tests/cwr/cases/6_1.py](../../tests/cwr/cases/6_1.py) executes each command exactly as written and fails if any exits non-zero. `text` blocks show syntax and are not executed.

## 1. What is compared

> **Implemented, not yet qualified.** Source: [benchmark/harness/arms.py](../../benchmark/harness/arms.py), [benchmark/harness/arms.kvx](../../benchmark/harness/arms.kvx), [benchmark/harness/run.py](../../benchmark/harness/run.py). Driver: [tests/cwr/cases/5_1.py](../../tests/cwr/cases/5_1.py). CWR-017 has no accepted attempt in the evidence registry, so this section describes shipped code, not verified behaviour.

Every arm is a CWR Build. Each hypothesis removes one pillar of CWR with one experimental build flag and measures the difference against the control Build on the same task suite, the same cassettes and the same model. Nothing is compared with any system outside CWR, and no harness file, prompt, arm or metric may reference one; task 5.1's driver scans `benchmark/harness/`, `benchmark/campaigns/` and every prompt template and fails on a match. Results say what each pillar contributes inside CWR and nothing more.

## 2. Hypotheses and arms

> **Implemented, not yet qualified.** Source: [benchmark/harness/arms.kvx](../../benchmark/harness/arms.kvx), [benchmark/preregistration.kvx](../../benchmark/preregistration.kvx), [benchmark/campaigns/h1.kvx](../../benchmark/campaigns/h1.kvx), [benchmark/campaigns/h2.kvx](../../benchmark/campaigns/h2.kvx). Driver: [tests/cwr/cases/3_2.py](../../tests/cwr/cases/3_2.py), [tests/cwr/cases/5_2.py](../../tests/cwr/cases/5_2.py), [tests/cwr/cases/5_3.py](../../tests/cwr/cases/5_3.py). CWR-013 and CWR-018 have no accepted attempt in the evidence registry, so this section describes shipped code, not verified behaviour. The directions are registrations, not findings.

| Hypothesis | Arms | Build flag per arm | Primary metric | Pre-registered direction |
|---|---|---|---|---|
| H1 Stability | frozen, drift-1, drift-3 | none; `--x-drift <drift.kvx>` with one injection; `--x-drift <drift.kvx>` with three | task success per bin | frozen > drift-1 > drift-3 on success; the reverse on repeated-failure rate |
| H2 Direct action | script, one-primitive | none; `--x-one-primitive` | turns to success per bin | script needs fewer turns in every bin with success at least as high |
| H3 Long context | K=5, K=20, K=full | none; the Instance is created with `--tail K` | rule reuse rate on rule-reuse tasks | H1 and H2 deltas larger in the long bin; reuse higher under frozen than drift at every K; K=full versus K=20 registered as undetermined |
| H4 Controlled change | changelog, version-only, silent | none; `--x-version-only` on Build B; `--x-silent-change` on Build B | post-upgrade misuse rate | changelog < version-only < silent; no difference on the documentation-only pair U6 |

Drift injections for H1 fall at turn max(1, floor(len/2)) for drift-1 and at max(1, floor(len/4)), max(1, floor(len/2)) and max(1, floor(3len/4)) for drift-3, where `len` is the reference solution's turn count recorded in [benchmark/suite.kvx](../../benchmark/suite.kvx). The drift specification of every task, the rule it was drawn from and its Build hash are registered in [benchmark/campaigns/h1.kvx](../../benchmark/campaigns/h1.kvx). H4 runs each upgrade pair U1 to U6: the Instance runs on Build A for the first half of the reference turn count, is upgraded to Build B at the midpoint and continues; pairs, midpoints and expected notices are frozen in [benchmark/upgrades.kvx](../../benchmark/upgrades.kvx).

## 3. Building the arms

> **Implemented, not yet qualified.** Source: [crates/cwr-compiler/src/experimental/mod.rs](../../crates/cwr-compiler/src/experimental/mod.rs), [crates/cwr-compiler/src/experimental/drift.rs](../../crates/cwr-compiler/src/experimental/drift.rs), [crates/cwr-compiler/src/experimental/one_primitive.rs](../../crates/cwr-compiler/src/experimental/one_primitive.rs), [crates/cwr-compiler/src/experimental/notice.rs](../../crates/cwr-compiler/src/experimental/notice.rs), [crates/cwr-kernel/src/drift_injector.rs](../../crates/cwr-kernel/src/drift_injector.rs). Driver: [tests/cwr/cases/3_2.py](../../tests/cwr/cases/3_2.py). CWR-013 has no accepted attempt in the evidence registry, so this section describes shipped code, not verified behaviour. The commands below are executed as written by the release driver.

Build the control and one experimental Build per flag from the same source. An experimental Build is written under `xbuild/` beside the Build root (`xbuild/` in the working directory for the default root); only one `--x-*` flag is allowed per Build (two are refused with exit 1). Two helpers read the hash and directory from the compiler's JSON report:

```sh
build_hash() { python3 -c 'import json, sys; print(json.load(sys.stdin)["build"]["hash"])'; }
build_dir() { python3 -c 'import json, sys; print(json.load(sys.stdin)["build"]["dir"])'; }
CONTROL=$(cwr build examples/refworld --json | build_dir)
ONE=$(cwr build examples/refworld --x-one-primitive --json | build_dir)
DRIFT=$(cwr build examples/refworld --x-drift spec/cwr/examples/refworld/drift/drift-1.kvx --json | build_dir)
```

The notice ablations are built from side B of an upgrade pair, against side A:

```sh
A=$(cwr build examples/refworld-upgrades/U1/A --out pairs/U1-A --json | build_hash)
PREV="pairs/U1-A/${A#sha256:}"
B=$(cwr build examples/refworld-upgrades/U1/B --out pairs/U1-B --previous "$PREV" --json | build_dir)
VERSION_ONLY=$(cwr build examples/refworld-upgrades/U1/B --out pairs/U1-B --previous "$PREV" --x-version-only --json | build_dir)
SILENT=$(cwr build examples/refworld-upgrades/U1/B --out pairs/U1-B --previous "$PREV" --x-silent-change --json | build_dir)
```

```text
cwr build <source> [--out <dir>] [--previous <hash|dir>]
                   [--x-drift <drift.kvx> | --x-one-primitive | --x-version-only | --x-silent-change]
```

| Flag | Changes | Must not change |
|---|---|---|
| `--x-drift <drift.kvx>` | Embeds a drift schedule; at each scheduled turn the kernel swaps the named precondition or failure in the running enforcement table and writes a `drift` ledger record | Manual, SDK, `contracts.json`, limits |
| `--x-silent-change` | `cwr instance upgrade` into this Build stores the notice but renders nothing | Contracts, manual, migrations, the changelog gate |
| `--x-version-only` | The rendered notice is the version line only | As above |
| `--x-one-primitive` | `limits.primitive_calls = 1` and one sentence stating it in the manual's limits section | Every other contract and limit |

Check attribution before using a pair; the diff between control and experimental must contain only entries attributable to the flag, and the harness refuses any other pair:

```sh
cwr diff "$CONTROL" "$ONE" > one-primitive-diff.json
cwr diff "$B" "$SILENT" > silent-change-diff.json
```

The drift file grammar (`meta.format = "cwr.drift"`, `drift.<n>.turn`, `primitive`, `change`, `expr`, `failure`, `doc`) is in [spec/cwr/compiler.md](../../spec/cwr/compiler.md) section 10, with an [example](../../spec/cwr/examples/refworld/drift/drift-1.kvx). The H1 campaign compiles one drift Build per task and drift arm from the schedules registered in [benchmark/campaigns/h1.kvx](../../benchmark/campaigns/h1.kvx); the release pins every one of them ([RELEASE.md](../../RELEASE.md)).

Every Instance of an experimental Build needs `--allow-experimental`:

```sh
XID=$(cwr instance create "$ONE" --seed 1 --arm H2/one-primitive --allow-experimental)
cwr instance show "$XID" > one-primitive-instance.json
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

> **Implemented, not yet qualified.** Source: [benchmark/preregistration.kvx](../../benchmark/preregistration.kvx), [benchmark/harness/prereg.py](../../benchmark/harness/prereg.py), [benchmark/analyse.py](../../benchmark/analyse.py). Driver: [tests/cwr/cases/5_1.py](../../tests/cwr/cases/5_1.py), [tests/cwr/cases/5_2.py](../../tests/cwr/cases/5_2.py). CWR-017 and CWR-018 have no accepted attempt in the evidence registry, so this section describes shipped code, not verified behaviour. The commands below are executed as written by the release driver.

`benchmark/preregistration.kvx` is committed before any campaign. Per hypothesis it records the arms with their Build hashes, the task set, the primary and secondary metrics, the expected direction and the sha256 of `benchmark/harness/metrics.py` and `benchmark/harness/arms.kvx`. The analysis refuses to run when the current digests differ from the registered ones. A metric change after execution is a new pre-registration and a new campaign; earlier results stay under their original registration. Print the digests the registration must carry:

```sh
python3 benchmark/analyse.py --print-digests
```

## 6. Running a campaign

> **Implemented, not yet qualified.** Source: [benchmark/harness/run.py](../../benchmark/harness/run.py), [benchmark/harness/model.py](../../benchmark/harness/model.py), [benchmark/campaigns/h1h2.py](../../benchmark/campaigns/h1h2.py). Driver: [tests/cwr/cases/5_1.py](../../tests/cwr/cases/5_1.py), [tests/cwr/cases/5_2.py](../../tests/cwr/cases/5_2.py). CWR-017 and CWR-018 have no accepted attempt in the evidence registry, so this section describes shipped code, not verified behaviour. The commands below are executed as written by the release driver.

`benchmark/harness/run.py` runs one Instance for one hypothesis, arm, task and seed: it creates the Instance of the arm's Build (with `--allow-experimental` where needed), drives turns through the model adapter with the fixed context layout in replay mode against the cassette set and stops at success, turn limit or budget exhaustion. `benchmark/campaigns/h1h2.py` registers and executes the H1 and H2 matrices. Both need the model gateway, whose credentials the harness reads from the environment at run time, so the release driver does not execute them; it does run the campaign status report:

```sh
python3 benchmark/campaigns/h1h2.py status > campaign-status.json
```

```text
python3 benchmark/harness/run.py --hypothesis H1|H2|H3|H4 --arm ARM --task TASK --seed SEED
        [--build HASH] [--cassettes DIR] [--results DIR] [--home DIR] [--builds DIR]
        [--model gateway|stub] [--turn-limit N] [--live] [--allow-unregistered]
python3 benchmark/campaigns/h1h2.py plan | register | run | status | analyse | report
```

One ledger per arm, task and seed:

```text
benchmark/results/h<N>/<arm>/<task>/<seed>/ledger.jsonl
benchmark/results/h<N>/<arm>/<task>/<seed>/final_state.kvx
benchmark/results/h<N>/<arm>/<task>/<seed>/attempt.kvx
```

The attempt record holds Build hash, Instance id, seed, cassette digest, model identity, compiler and kernel revisions, arm, start and end, exit status, log path and the harness-recorded token counts. A missing cell makes the matrix incomplete and the campaign cannot close.

## 7. Metrics

> **Implemented, not yet qualified.** Source: [benchmark/harness/metrics.py](../../benchmark/harness/metrics.py). Driver: [tests/cwr/cases/5_1.py](../../tests/cwr/cases/5_1.py). CWR-017 has no accepted attempt in the evidence registry, so this section describes shipped code, not verified behaviour. The commands below are executed as written by the release driver.

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
The metric functions are pure and unit-tested on synthetic ledgers:

```sh
python3 -m unittest discover -s benchmark/harness/tests -t .
```

## 8. Analysis

> **Implemented, not yet qualified.** Source: [benchmark/analyse.py](../../benchmark/analyse.py). Driver: [tests/cwr/cases/5_2.py](../../tests/cwr/cases/5_2.py), [tests/cwr/cases/5_3.py](../../tests/cwr/cases/5_3.py). CWR-018 has no accepted attempt in the evidence registry, so this section describes shipped code, not verified behaviour.

```text
python3 benchmark/analyse.py [--results DIR] [--prereg FILE] [--metrics FILE] [--arms FILE] [--suite FILE]
        [--upgrades FILE] [--tasks-dir DIR] [--out FILE] [--check REPORT] [--live] [--draft-ok]
        [--require-complete] [--print-digests]
```

The analysis reads ledgers and attempt records under `benchmark/results/` and the registered definitions in `benchmark/preregistration.kvx`. For every arm and bin it reports the mean of each metric across tasks and seeds with a 95% bootstrap confidence interval from 10,000 resamples over Instances. Bins are never pooled. Drift arms are reported per drift specification and pooled. Ledgers with different cassette digests are never pooled. `--check` recomputes and compares with an existing report. The four research reports go to `research/h1-stability.md`, `research/h2-direct-action.md`, `research/h3-long-context.md` and `research/h4-controlled-change.md`; the release pins the digest of each result directory ([RELEASE.md](../../RELEASE.md)).

## 9. Live versus replay

> **Implemented, not yet qualified.** Source: [benchmark/harness/run.py](../../benchmark/harness/run.py). Driver: [tests/cwr/cases/5_1.py](../../tests/cwr/cases/5_1.py). CWR-017 has no accepted attempt in the evidence registry, so this section describes shipped code, not verified behaviour.

All campaigns run in replay mode. A live-mode run of the frozen arm against the stub servers is permitted as a robustness check; its attempt records carry `mode = "live"`, it is written under `benchmark/results/live/`, and it is never pooled with replay results. Live runs against real external services are out of scope for the first campaign.

## 10. What counts as a result

> **Verified, component tier.** Evidence: CWR-019, attempt record [tests/cwr/evidence/CWR-019/20261106T133347Z-1.json](../../tests/cwr/evidence/CWR-019/20261106T133347Z-1.json), outcome PASS, accepted by the independent checker and written to [tests/cwr/registry.kvx](../../tests/cwr/registry.kvx) by the runner. Source: [tests/cwr/qualify.py](../../tests/cwr/qualify.py), [tests/cwr/check_evidence.py](../../tests/cwr/check_evidence.py), driver [tests/cwr/cases/0_2.py](../../tests/cwr/cases/0_2.py). The case status in `spec/cwr/spec.kvx` stays UNRUN until task 6.2 updates statuses from the registry. This covers the runner and checker only; it says nothing about a campaign or a hypothesis.

Qualification attempts run through one runner, which executes a case's task drivers, invokes the independent checker and appends one immutable attempt record to the hash-chained index `tests/cwr/evidence/index.jsonl`:

```text
python3 tests/cwr/qualify.py --case CWR-NNN
python3 tests/cwr/qualify.py --case CWR-NNN --task W.N
python3 tests/cwr/qualify.py --task W.N
python3 tests/cwr/qualify.py --check-registry
```

The first form qualifies a whole case; the `--task` forms record a component attempt for one task's registered subset, which never changes the case status; `--check-registry` validates the registry against the specification. The checker is a separate program that reads only attempt records, ledgers, final state and the registry, and refuses zero-assertion, tampered, stale-Build and mismatched-seed evidence. It can be run on its own; for CWR-019 it reports the accepted attempt:

```sh
python3 tests/cwr/check_evidence.py --status CWR-019
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

The three points this guide left open on its first edition are settled by registration in [benchmark/preregistration.kvx](../../benchmark/preregistration.kvx) and the harness README ([benchmark/harness/README.md](../../benchmark/harness/README.md)): turn limits are registered per bin with 200 as the cap, the H3 K sweep runs on the full suite (a superset of both readings), and `run.py` and `analyse.py` have the command lines shown above. Two registered choices remain worth knowing: "primitive calls" counts `primitive` and `adapter` records (convention C1), and the H3 frozen-versus-drift reuse direction is registered at K = 20 only, because the drift arms run only at K = 20.

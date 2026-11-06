<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Experiments: hypotheses H1 to H4 as internal ablations

Status: proposed contract; nothing has been built or run and every case is UNRUN. Requirement 12 in [spec.kvx](spec.kvx) applies, together with the experimental-build rules in [compiler.md](compiler.md), the ledger schema in [kernel.md](kernel.md) and the task suite in [reference-world.md](reference-world.md).

## 1. No external baseline

Every experimental arm is a CWR Build. A hypothesis is tested by ablating one pillar of CWR with an experimental build flag and measuring the difference against the control Build on the same task suite, the same cassettes and the same model. The harness has no adapter, prompt, configuration file or metric that references, invokes or compares against any system outside CWR, and no document in this pack makes such a comparison. Task 5.1's driver scans `benchmark/harness/`, `benchmark/campaigns/` and every prompt template for such references and fails on a match. The results therefore say what each pillar contributes inside CWR and nothing else.

## 2. Shared setup

| Item | Proposed value | Recorded where |
|---|---|---|
| Control Build | refworld, compiled with no experimental flag | Build hash in `benchmark/harness/arms.kvx` |
| Model | One fixed model identity for all arms | Ledger header of every Instance |
| Sampling settings | Fixed and identical across arms; temperature and any seed parameter recorded | Ledger header and attempt record |
| Gateway mode | replay against the frozen cassette set | Cassette digest in the ledger header |
| Context layout | manual, notice, ledger tail K, result envelope, in that order; K = 20 unless the arm changes it | K in ledger header |
| Seeds | 10 per task per arm; seed selects RNG stream and cassette sequence | Ledger header |
| Turn limit | 200 turns per Instance; exceeding it is a failed run | Attempt record |
| Per-turn budget | world.kvx limits of the Build | Build manifest |
| Task suite | 60 tasks across three bins | Suite digest in `benchmark/suite.kvx` |
| Success | success.py predicate over final state and ledger | Attempt record |

The numbers are choices made before execution. They are not derived from any measurement and are not power calculations.

## 3. Hypotheses

### 3.1 H1 Stability

Claim: a world whose rules are fixed for the life of the Build yields higher task success and fewer repeated failures than an otherwise identical world whose rules drift mid-task without notice.

| Arm | Build flag | What differs | What is identical |
|---|---|---|---|
| frozen | none (control) | nothing | manual, SDK, cassettes, tasks, model |
| drift-1 | --x-drift with one injection at turn floor(len/2) | one rule from the task's `rules` file changes at the scheduled turn; the manual is unchanged | everything else |
| drift-3 | --x-drift with injections at floor(len/4), floor(len/2), floor(3len/4) | three rules change | everything else |

`len` is the reference solution's turn count in the frozen arm's first seed, recorded before the drift arms run. Drift specs are listed per task under `benchmark/campaigns/h1.kvx` and follow the grammar in [compiler.md](compiler.md); each injection is logged as a ledger record of kind drift.

Task set: all 60 tasks. Pre-registered direction: frozen is higher on task success and lower on repeated-failure rate than drift-1, and drift-1 is better than drift-3. Primary metric: task success per bin. Secondary: repeated-failure rate, turns to success, read-only calls per mutating call as a probing proxy.

### 3.2 H2 Direct action

Claim: one script per turn completes tasks in fewer turns than one primitive per turn, without a higher partial-failure cost.

| Arm | Build flag | What differs | What is identical |
|---|---|---|---|
| script | none (control) | nothing | everything |
| one-primitive | --x-one-primitive | the second primitive call in a turn returns LIMIT with primitive_calls=1; the manual states the limit | manual otherwise, SDK, cassettes, tasks, model |

Task set: all 60 tasks. Pre-registered direction: script has fewer turns to success in every bin and task success at least as high; partial status rate in script is reported and expected to stay below the one-primitive arm's LIMIT rate. Primary metric: turns to success per bin. Secondary: task success, primitive calls, partial rate, model-boundary tokens per task recorded by the harness.

### 3.3 H3 Long context

Claim: the advantages measured in H1 and H2 grow with task length, and a stable world lets the model reuse a rule learned earlier without re-probing.

| Arm | Build flag | What differs | What is identical |
|---|---|---|---|
| K=5 | none; Instance created with K=5 | ledger tail length | Build, cassettes, tasks, model |
| K=20 | none; K=20 (the control) | nothing | everything |
| K=full | none; K equal to the ledger length | ledger tail length | everything else |

H3 has no new Build. It reports the H1 and H2 deltas per bin and adds the K sweep on the long bin and the rule-reuse tasks. Task set: the K sweep runs on the 20 long tasks; the per-bin analysis uses the H1 and H2 ledgers. Pre-registered direction: the frozen-versus-drift and script-versus-one-primitive deltas are larger in the long bin than in the short bin; rule reuse rate is higher under frozen than under drift at every K; K=full is not required to be better than K=20 and the direction is registered as undetermined. Primary metric: rule reuse rate on the rule-reuse tasks. Secondary: the H1 and H2 primary metrics per bin, re-probe count on the reused rule.

### 3.4 H4 Controlled change

Claim: after a versioned recompilation with a changelog, the model adopts the changed rule in fewer turns and with fewer old-rule errors than after a silent change, and the changelog carries more of the effect than the version signal alone.

| Arm | Build flag | What the model sees after upgrade | What is identical |
|---|---|---|---|
| changelog | none; ordinary cwr instance upgrade | the full recompile notice | Builds A and B, cassettes, tasks, model |
| version-only | --x-version-only on Build B | only the version line of the notice | everything else |
| silent | --x-silent-change on Build B | nothing; the manual of B is shown but no notice | everything else |

Protocol: each of the six upgrade pairs U1 to U6 in [reference-world.md](reference-world.md) runs its post-upgrade task. The Instance runs on Build A for the first half of the reference turn count, is upgraded to Build B at the midpoint, and continues. The pair, the midpoint and the expected notice are frozen in `benchmark/upgrades.kvx`. Pre-registered direction: changelog has the lowest post-upgrade misuse rate and fewest turns to first correct use, then version-only, then silent; U6, the documentation-only pair, is expected to show no difference between arms and serves as the notice-noise control. Primary metric: post-upgrade misuse rate. Secondary: turns from upgrade to first correct use of the changed primitive, second-half success, number of proposals filed and whether a proposal names the changed primitive.

## 4. Metric definitions

Every metric is computed from ledgers and final state only. The computation below is normative for `benchmark/harness/metrics.py`; its source digest is pre-registered.

| Metric | Definition | Ledger-only computation |
|---|---|---|
| task success | success.py returns true for the final state and ledger | Evaluate the predicate once per Instance; 1 or 0. |
| turns to success | the turn number of the first turn after which the predicate holds; undefined on failure | Replay state per turn from the ledger, evaluate the predicate after each turn, take the first true; failures are reported separately, never as a large number. |
| primitive calls | count of ledger records of kind primitive | Count records with kind primitive, excluding kind turn, drift, proposal and header. |
| failure rate by code | per code, failures divided by primitive calls | For each envelope code, count records with outcome.status error and outcome.code equal to that code, divided by primitive calls. |
| repeated-failure rate | fraction of failing records that repeat an earlier failure of the same primitive with the same code in the same Instance | Walk records in seq order; a failing record is repeated when an earlier record has equal primitive and equal outcome.code; repeated divided by total failing records. |
| rule reuse rate | fraction of rule-reuse tasks where the second encounter of the reused rule produces no failure on that rule | For each rule-reuse task, find the first ok record of the reused primitive, then the first record of that primitive at least ten primitive records later; the task counts as reuse when that record and all subsequent records of that primitive in the last quarter have no failure with the rule's code. |
| post-upgrade misuse rate | fraction of post-upgrade turns that call a changed primitive under its old rule | After the ledger's upgrade record, a turn is a misuse when it contains a record of a primitive named in the pair's ContractDiff with the failure code that Build B's new rule raises on an old-style call; misuse turns divided by post-upgrade turns. |
| partial rate | fraction of turns with result status partial | Count turn records with status partial divided by turn records. |
| probing ratio | read-only calls per mutating call | Count records whose primitive side_effect is read_only divided by those with mutating or external; a secondary H1 proxy only. |

Tokens at the model boundary are recorded by the harness per turn in the attempt record. They are not a ledger quantity and are reported only as a secondary H2 measure, labelled as harness-recorded.

## 5. Analysis

`benchmark/analyse.py` reads the ledgers and attempt records under `benchmark/results/` and the registered definitions in `benchmark/preregistration.kvx`. For every arm and bin it reports the mean of each metric across tasks and seeds with a 95% bootstrap confidence interval using 10,000 resamples over Instances; the resample count is a proposed choice. Bins are never pooled. Drift arms are reported per drift spec as well as pooled, because the specs are hand-chosen.

`benchmark/preregistration.kvx` is committed before any campaign and records, per hypothesis, the arms with their Build hashes, the task set, the primary and secondary metrics, the expected direction and the sha256 of `benchmark/harness/metrics.py`. `analyse.py` refuses to run when the current metrics file digest differs from the registered one. A change to a metric after execution is a new pre-registration and a new campaign; the earlier results are retained under their original registration.

## 6. Run layout

```text
benchmark/results/h<N>/<arm>/<task>/<seed>/ledger.jsonl
benchmark/results/h<N>/<arm>/<task>/<seed>/final_state.kvx
benchmark/results/h<N>/<arm>/<task>/<seed>/attempt.kvx
```

The attempt record is the registry entry described in [verification.md](verification.md): Build hash, Instance id, seed, cassette digest, model identity, compiler and kernel revisions, arm, start and end, exit status, log path and the harness-recorded token counts. One ledger per arm, task and seed; a missing cell makes the matrix incomplete and the campaign cannot close.

## 7. Live versus replay

All campaigns run in replay mode. A live-mode run of the frozen arm against the stub servers is permitted as a robustness check; its attempt records carry `mode = "live"`, it is written under `benchmark/results/live/`, and `analyse.py` never pools it with replay results. A live run against any real external service is out of scope for the first campaign.

## 8. Threats to validity

| Threat | Effect | Mitigation |
|---|---|---|
| Model nondeterminism | Seeds control the world, not the model; two runs with one seed may differ | 10 seeds per task; the model's own sampling settings are fixed and recorded; variance is reported, not hidden. |
| Cassette staleness | A re-recorded cassette set changes adapter content and invalidates comparisons | The cassette digest is in every ledger header; `analyse.py` refuses to pool ledgers with different digests. |
| Task leakage between arms | A task solvable from the instruction alone shows no pillar effect | Reference solutions establish that each task needs the rules in its `rules` file; tasks with zero rule-dependent primitives are excluded from the suite. |
| K confounds in H1 and H2 | A tail too short or too long could hide or create an effect | K is fixed at 20 for H1 and H2 and swept separately in H3. |
| Drift schedule choice | Hand-chosen injections could favour the hypothesis | Specs are drawn from the task's own `rules` file by a seeded rule, committed before the run and reported per spec. |
| One-primitive arm is artificial | It is the cleanest in-world ablation of pillar 2, not a model of any other system | Stated as such; no claim beyond CWR is made. |
| Notice wording | The H4 effect could come from wording rather than versioning | The version-only arm separates the version signal; U6 controls for notice noise; wording variants are a follow-up campaign. |
| Replay removes external variance | Results do not cover a live, changing service | Replay is the registered condition; the live robustness run is reported separately. |

## 9. Required future evidence

- CWR-017 (task 5.1): no harness file references a system outside CWR; metrics recomputed from ledgers alone equal the harness output; a live-mode run is labelled and excluded from pooling.
- CWR-018 (tasks 5.2 and 5.3): complete arm matrices with 10 seeds per task exist for every hypothesis; the research reports' numbers equal an independent recomputation from the ledgers; the pre-registration digests match the executed metric definitions.

No hypothesis result exists. The expected directions above are registrations, not findings, and a campaign that contradicts them is reported as such.

<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Experiments: task suite, stubs, harness and analysis

Status: the pre-registration is still a draft and no cassette set is frozen, so no campaign result
qualifies as evidence yet.

This directory tests four hypotheses about CWR, H1 Stability, H2 Direct action, H3 Long context and
H4 Controlled change, with a fixed task suite on the reference world. Every arm is a CWR Build of
that world with at most one pillar ablated by an experimental flag. Nothing here compares CWR with
any other system. Every reported number is recomputed from ledgers and final state alone.

The contract is [experiments.md](../spec/cwr/experiments.md). The task suite follows
[reference-world.md](../spec/cwr/reference-world.md).

## Layout

| Path | Holds |
|---|---|
| [tasks/](tasks/README.md) | The 60 tasks, each with its instruction, initial state and success predicate |
| `solutions/` | One reference turn-script solution per task, kept outside the task directories |
| `predicates/common.py` | The predicate helpers, the final-state view and the predicate purity audit |
| `suite.kvx` | Bins, rules, reuse positions and the suite, solution and cassette digests |
| [stubs/](stubs/README.md) | Deterministic stub servers for the reference world's adapters, and the cassette tool |
| [cassettes/](cassettes/FORMAT.md) | Recorded adapter traffic and the cassette format |
| [harness/](harness/README.md) | Arm definitions, the episode runner, the model client and the metric definitions |
| `analyse.py` | Metrics, bootstrap intervals and direction labels, recomputed from ledgers |
| `preregistration.kvx` | Hypotheses, arms, task sets, metrics, analysis parameters and frozen digests |
| `campaigns/` | Per-hypothesis campaign registrations and the H1 and H2 campaign driver |
| `upgrades.kvx` | Frozen expectations for the upgrade pairs in [examples/refworld-upgrades](../examples/refworld-upgrades/README.md) |
| `release.kvx` | Release pins, described in [RELEASE.md](../RELEASE.md) |
| `results/` | Ledgers, final states, attempt records and analyses written by campaign runs |

## Arms

| Hypothesis | Arm | Role | What differs from the control Build |
|---|---|---|---|
| H1 | `frozen` | control | Nothing: rules are fixed for the life of the Build |
| H1 | `drift-1` | ablation | One rule from the task's rules changes mid-task without notice (`--x-drift`) |
| H1 | `drift-3` | ablation | Three rules change at the quartile turns without notice (`--x-drift`) |
| H2 | `script` | control | Nothing: one script per turn with the control limits |
| H2 | `one-primitive` | ablation | The second primitive call in a turn returns `LIMIT` (`--x-one-primitive`) |
| H3 | `k5` | variant | The ledger tail shown to the model holds 5 records |
| H3 | `k20` | control | The default ledger tail of 20 records |
| H3 | `kfull` | variant | The ledger tail holds every record |
| H4 | `changelog` | control | An ordinary upgrade with the full recompile notice |
| H4 | `version-only` | ablation | The notice shows only the version line (`--x-version-only`) |
| H4 | `silent` | ablation | The new manual is shown with no notice (`--x-silent-change`) |

## How a campaign runs

1. An operator compiles the control Build and each experimental Build with `cwr build`. The harness
   never invokes the World Compiler.
2. The reference solutions are run once against the stub servers in record mode, and the digest of
   the resulting cassette set is frozen in `suite.kvx`.
3. The campaign inputs and digests are written into `preregistration.kvx`, and its status becomes
   `registered`. After that, any change to the metric definitions or the arm table makes the
   analysis refuse.
4. Each cell (hypothesis, arm, task, seed) runs in its own Instance, in replay mode, and writes its
   ledger, final state and attempt record under `results/`.
5. `analyse.py` recomputes every metric from those files and refuses on any integrity failure.

## Checks

```sh
python3 benchmark/stubs/selftest.py
python3 benchmark/tasks/_authoring/build_suite.py --check
python3 -m unittest discover -s benchmark/harness/tests -t .
python3 benchmark/analyse.py --help
```

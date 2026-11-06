<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Hypothesis reports

Status: no report exists yet. No campaign has completed under a registered pre-registration.

This directory holds one report per hypothesis. Each report states what was measured, the result
and every deviation from the pre-registration. Every number in a report equals an independent
recomputation from the campaign ledgers by `benchmark/analyse.py`.

| Report | Claim under test | Arms compared |
| --- | --- | --- |
| `h1-stability.md` | H1 Stability: rules fixed for the life of a Build give higher task success and fewer repeated failures than rules that drift without notice | `frozen` against `drift-1` and `drift-3` |
| `h2-direct-action.md` | H2 Direct action: one script per turn completes tasks in fewer turns than one primitive per turn, without a higher partial-failure cost | `script` against `one-primitive` |
| `h3-long-context.md` | H3 Long context: the H1 and H2 advantages grow with task length, and a stable world lets the model reuse a rule without re-probing | ledger tails `k5`, `k20` and `kfull`, plus the H1 and H2 deltas per bin |
| `h4-controlled-change.md` | H4 Controlled change: after a versioned recompilation with a changelog, the model adopts the changed rule faster and with fewer old-rule errors than after a silent change | `changelog` against `version-only` and `silent` |

The pre-registered directions are registrations, not findings. A campaign that contradicts them is
reported as such. Every arm is a CWR Build with at most one pillar ablated, and no report compares
CWR with any other system.

## How a report is produced

1. The campaign runs every registered cell in replay mode and writes its ledgers under
   `benchmark/results/`.
2. `benchmark/analyse.py` recomputes every metric from the ledgers and final state.
3. The report is rendered from that analysis. `python3 benchmark/campaigns/h1h2.py report` renders
   the H1 and H2 reports.
4. Release closure refuses while any of the four reports is missing.

A campaign with fewer seeds than pre-registered is reported as a deviation and never as a PASS. The
experiment design is in [experiments.md](../spec/cwr/experiments.md), and the harness is described in
[benchmark/harness](../benchmark/harness/README.md).

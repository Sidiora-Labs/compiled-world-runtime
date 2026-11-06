<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Running experiments

> **Implemented, not yet qualified (CWR-013, CWR-017, CWR-018).** CWR has not yet passed qualification. The campaign pre-registration is registered, a campaign result counts only once its qualification case passes, and this guide describes what the current [experiment harness](../../benchmark/harness/run.py) and the experimental Builds of `cwr` do.

CWR rests on three pillars: stable rules, direct action through scripts, and change only by human
recompilation. Each experiment removes one pillar from an otherwise identical world and measures
what the model loses. Every arm is a CWR Build of the reference world `refworld`: the control
Build, or the control source compiled with exactly one experimental flag. CWR is never compared
with another system, and no arm, prompt or metric refers to one.

Every metric is computed from ledgers and final state only. Anyone holding the ledgers can
recompute every number.

## Hypotheses and arms

The hypotheses, arms, metrics and expected directions are frozen in
[benchmark/preregistration.kvx](../../benchmark/preregistration.kvx) before any run. The arms
and their flags are in [benchmark/harness/arms.kvx](../../benchmark/harness/arms.kvx).

| Hypothesis | Arms | What the ablation changes | Primary metric | Expected direction |
|---|---|---|---|---|
| H1 Stability | `frozen`, `drift-1`, `drift-3` | `--x-drift`: one or three rules change mid-task without notice | task success | frozen > drift-1 > drift-3 |
| H2 Direct action | `script`, `one-primitive` | `--x-one-primitive`: one primitive call per turn | turns to success | script needs fewer turns, with success at least as high |
| H3 Long context | `k5`, `k20`, `kfull` | No flag. The Instance shows the model 5, 20 or all ledger records. | rule reuse | H1 and H2 effects grow with task length |
| H4 Controlled change | `changelog`, `version-only`, `silent` | `--x-version-only` or `--x-silent-change` on the upgraded Build | post-upgrade misuse rate | changelog < version-only < silent |

H4 runs on the six upgrade pairs in
[examples/refworld-upgrades](../../examples/refworld-upgrades/README.md). The documentation-only
pair U6 is the control for notice noise and is reported separately.

## Before you start

Install the `cwr` binary as described in [Install](../../README.md#install). The harness is Python
3.12 with the standard library only. It finds the binary through `CWR_BIN`.

The commands below run in one shell. They start in the repository root and then work in a scratch
directory. Hashes in the output excerpts are shortened.

```sh
REPO=$PWD
export CWR_BIN="$(command -v cwr)"
cd "$(mktemp -d)"
```

## Build the arms

Compile the control Build and one experimental Build per flag from the same source. Experimental
Builds are written under `xbuild/`, beside the Build root. A Build takes one flag at most.

```sh
cwr build "$REPO/examples/refworld"
cwr build "$REPO/examples/refworld" --x-one-primitive
CONTROL=$(ls build)
ONE=$(ls xbuild)
```

```text
built experimental sha256:6b2593cf... (x-one-primitive) at xbuild/6b2593cf...
```

`cwr diff` checks attribution: the difference between the control and an experimental Build must
consist only of what the flag changes.

```sh
cwr diff "$CONTROL" "$ONE"
```

```text
  "experimental": {
    "attributable": true,
    ...
    "experimental_entries": [
      {
        "after": "In this world `primitive_calls = 1`: a turn may make exactly one primitive call; a second call in the same turn is not executed and fails with LIMIT naming `primitive_calls`.",
        "field": "limits",
        "kind": "manual_sentence",
        "target": "manual"
      }
    ],
    "flag": "x-one-primitive",
    "unattributable": []
  },
```

| Flag | Changes | Leaves unchanged |
|---|---|---|
| `--x-drift <file>` | Embeds a drift schedule. At each scheduled turn the kernel changes one rule in the running table and writes a `drift` ledger record that the model never sees. | Manual, SDK, contracts, limits |
| `--x-one-primitive` | Sets `primitive_calls` to 1 and adds one sentence saying so to the manual | Every other contract and limit |
| `--x-version-only` | An upgrade into this Build shows the model only the version line of the recompile notice | Contracts, manual, migrations, changelog gate |
| `--x-silent-change` | An upgrade into this Build shows the model no notice | As above |

Every experimental Build is stamped in its manifest and on every ledger record of its Instances.
The kernel refuses to run one without `--allow-experimental`, with exit code 3:

```text
$ cwr instance create "$ONE"
cwr instance: experimental build refused: Build sha256:6b2593cf... is experimental (x-one-primitive); pass --allow-experimental
```

### One primitive per turn

The second call of a turn is not executed, so the turn ends `partial`:

```sh
OI=$(cwr instance create "$ONE" --allow-experimental --arm H2/one-primitive)
cwr instance turn "$OI" <<'SCRIPT'
import fs

fs.write(path="/notes/a.md", content=b"hello")
fs.lock(path="/notes/a.md")
SCRIPT
```

```text
{"error":{"code":"LIMIT","detail":"the turn budget of 1 primitive calls is spent; this call was not executed","limit":"primitive_calls","precondition_index":null,"primitive":"fs.lock","retry_after":null},...,"status":"partial",...}
```

### Drift

A drift schedule names the turn, the primitive, the kind of change, the new expression, its failure
code and a description. It uses the precondition language and the same checks as a contract. The
change kinds `primitive_added`, `primitive_removed`, `state_shape` and `doc_only` cannot drift.
This schedule adds a rule to `fs.delete` from turn 2:

```sh
cat > drift.kvx <<'EOF'
[meta]
format = "cwr.drift"
schema_version = 1

[drift.1]
turn = 2
primitive = "fs.delete"
change = "precondition_added"
expr = "fs.size(path) < 4"
failure = "LOCKED"
doc = "From turn 2, fs.delete also refuses files of 4 bytes or more with LOCKED."
EOF
cwr build "$REPO/examples/refworld" --x-drift drift.kvx
DRIFT=$(ls xbuild | grep -v "$ONE")
DI=$(cwr instance create "$DRIFT" --allow-experimental --arm H1/drift-1)
cwr instance turn "$DI" <<'SCRIPT'
import fs

fs.write(path="/a", content=b"hello")
fs.write(path="/b", content=b"hello")
fs.delete(path="/a")
SCRIPT
cwr instance turn "$DI" <<'SCRIPT'
import fs

fs.delete(path="/b")
SCRIPT
cwr instance tail "$DI"
grep '"kind":"drift"' ".cwr/instances/$DI/ledger.jsonl"
```

The same call succeeds in turn 1 and fails in turn 2. The manual still documents the old rule, and
the model's ledger tail does not show the drift. The ledger records it:

```text
t1   wt1    fs.write             ok
t1   wt2    fs.write             ok
t1   wt3    fs.delete            ok
t1   turn                        ok     calls=3
t2   wt13   fs.delete            error  LOCKED
t2   turn                        error  LOCKED  calls=1
{...,"change":"precondition_added","drift":1,"experimental":"x-drift","expr":"fs.size(path) < 4","failure":"LOCKED","first_turn":2,"kind":"drift",...,"primitive":"fs.delete",...,"visible":false,...}
```

The H1 campaign derives one drift schedule per task from the task's rules. `python3
"$REPO/benchmark/campaigns/h1h2.py" plan` prints them.

### Notice ablations

For H4, the upgraded side of a pair is compiled with the flag. The upgrade carries the full notice
in its ledger record. The model sees only what the flag allows:

```sh
cwr build "$REPO/examples/refworld-upgrades/U1/A" --out u1/build
PAIR_A=$(ls u1/build)
cwr build "$REPO/examples/refworld-upgrades/U1/B" --out u1/build --previous "$PAIR_A" --x-version-only
PAIR_B=$(ls u1/xbuild)
VI=$(cwr instance create "u1/build/$PAIR_A")
cwr instance upgrade "$VI" "u1/xbuild/$PAIR_B" --allow-experimental
cwr instance notice "$VI"
```

```text
WORLD RECOMPILED  0.2.0 -> 0.3.0   (sha256:a4e291c7... -> sha256:a01965b8...)
```

## Shared setup

These values come from the `[campaign]` section of the pre-registration. They are choices fixed
before execution, not measurements.

| Item | Value |
|---|---|
| World | `refworld`, the control Build for every hypothesis |
| Task suite | 60 tasks in [benchmark/suite.kvx](../../benchmark/suite.kvx): 20 short, 20 medium, 20 long |
| Seeds | 1 to 10 for every task and arm |
| Gateway mode | replay, against recorded cassettes |
| Context | manual, recompile notice, ledger tail, last result envelope |
| Ledger tail | 20 records, except in H3 |
| Turn limit | 15 short, 40 medium, 120 long, never above 200 |
| Output limit | 16,000 tokens per turn |
| Stop rule | first turn whose success predicate holds, the turn limit, budget exhaustion, a model error or an `INVARIANT_VIOLATION` |

Each task directory under [benchmark/tasks](../../benchmark/tasks/README.md) holds the instruction,
the initial state, the rules the task depends on and a `success.py` predicate over final state and
ledger.

## Record the cassettes

Campaigns run in replay mode, so every arm sees identical adapter responses. The recorder runs each
task's reference solution through the real kernel against the stub servers, once per seed. Here it
records one task:

```sh
python3 "$REPO/benchmark/stubs/record_refworld.py" record --set suite --tasks t003-tennis-note --out cassettes
```

```text
record_refworld: suite: 10 cassettes, 10 turns, digest sha256:7bd66d41...
```

The set is written under `cassettes/suite`. A full set holds every task. Its digest is
registered, and ledgers recorded against different cassette digests are never pooled. `run.py`
refuses a set whose digest differs from the registered one. A campaign replays the registered set
under [benchmark/cassettes/suite](../../benchmark/cassettes/suite). The pilot below replays the set
you just recorded under a draft registration that registers no cassette digest.

## Run cells

`run.py` runs one cell: one hypothesis, arm, task and seed. It creates the Instance, renders the
context, sends it to the model, runs the reply as a turn, exports state after every turn and stops
by the stop rule. It never compiles a Build.

| `--model` | Meaning |
|---|---|
| `gateway` | The registered model. Credentials are read from the environment. |
| `stub` | A deterministic offline player that replays the task's reference solution and opens no socket. Use it to test the pipeline. |

`--allow-unregistered` runs under a draft registration and marks each attempt
`registered = false`. The pilot writes its own draft registration in the scratch directory, so its
attempts can never be pooled with a campaign and it does not depend on the repository's frozen
digests:

| Draft file | Differs from the registration in |
|---|---|
| `arms.kvx` | The control and one-primitive Builds are the ones you just compiled |
| `draft.kvx` | Status `draft`; the stub player `cwr-stub/reference-player-1` as the model, because `analyse.py` refuses a ledger whose model is not the registered one; the digests of the draft `arms.kvx` and the current `metrics.py`; no suite or cassette digest |

`--prereg` and `--arms` pass the draft to `run.py`. The pilot runs two seeds of both H2 arms with
the stub player:

```sh
sed -e "/^\[control\]$/,/^$/ s/^build = .*/build = \"sha256:$CONTROL\"/" -e "/^\[arm.h2.one-primitive\]$/,/^$/ s/^build = .*/build = \"sha256:$ONE\"/" "$REPO/benchmark/harness/arms.kvx" > arms.kvx
sed -e 's/^status = "registered"$/status = "draft"/' -e 's|^model = .*|model = "cwr-stub/reference-player-1"|' -e '/^\(suite\|cassette\)_digest = /d' -e "s/^arms_kvx = .*/arms_kvx = \"sha256:$(sha256sum arms.kvx | cut -c1-64)\"/" -e "s/^metrics_py = .*/metrics_py = \"sha256:$(sha256sum "$REPO/benchmark/harness/metrics.py" | cut -c1-64)\"/" "$REPO/benchmark/preregistration.kvx" > draft.kvx
pilot() { python3 "$REPO/benchmark/harness/run.py" --hypothesis H2 --task t003-tennis-note --model stub --allow-unregistered --prereg draft.kvx --arms arms.kvx --cassettes cassettes/suite --results results --home runs "$@"; }
pilot --arm script --seed 1 --builds build --build "$CONTROL"
pilot --arm script --seed 2 --builds build --build "$CONTROL"
pilot --arm one-primitive --seed 1 --builds xbuild --build "$ONE"
pilot --arm one-primitive --seed 2 --builds xbuild --build "$ONE"
```

```text
H2/script t003-tennis-note seed 1 mode replay: success after 1 turns
H2/script t003-tennis-note seed 2 mode replay: success after 2 turns
H2/one-primitive t003-tennis-note seed 1 mode replay: turn_limit after 15 turns
H2/one-primitive t003-tennis-note seed 2 mode replay: turn_limit after 15 turns
```

The stub player replays scripts written for the control world, so these outcomes test the
pipeline. They say nothing about a model.

Each cell writes one directory:

```text
results/h2/script/t003-tennis-note/1/
  ledger.jsonl          the Instance's ledger
  final_state.kvx       the final state as the success predicate sees it
  messages/turn_0001.md the model message submitted as each turn, the input analyse.py replays
  kernel.log            the kernel's stderr of each turn
  attempt.kvx           Build, Instance, seed, cassette digest, model, revisions, log path, registration digests, timing, exit status, token counts, metrics
```

## Metrics

The definitions are normative in the pre-registration and implemented once in
[benchmark/harness/metrics.py](../../benchmark/harness/metrics.py), whose digest is registered.

| Metric | Definition |
|---|---|
| `task_success` | `success.py` on the final state and full ledger: 1 or 0 |
| `turns_to_success` | First turn after which the predicate holds. Undefined on failure. |
| `primitive_calls` | Call records: kinds `primitive` and `adapter` |
| `failure_rate.<CODE>` | Failing call records with that code, divided by primitive calls |
| `repeated_failure_rate` | Failing calls that repeat an earlier failing (primitive, code) pair, divided by failing calls |
| `rule_reuse` | Whether a rule learned early is respected when the primitive is used again later |
| `post_upgrade_misuse_rate` | Post-upgrade turns that call a changed primitive under its old rule, divided by post-upgrade turns |
| `turns_to_first_correct_use` | Turns from the upgrade to the first successful call of a changed primitive |
| `partial_rate` | Turns with status `partial`, divided by turns |
| `probing_ratio` | Read-only calls divided by mutating and external calls |
| `proposals_after_upgrade` | Proposal records after the upgrade |
| `drift_records` | Records of kind `drift` |

A rate with a zero denominator is undefined and left out of that metric's sample. Token counts are
recorded by the harness at the model boundary. They are not ledger quantities and are reported
separately.

## Analyse

`analyse.py` recomputes every metric from the ledgers. For each arm and group it reports the mean
over Instances with a 95% percentile bootstrap interval from 10,000 resamples. It labels each
registered direction `consistent`, `inconsistent` or `inconclusive` from the interval of the
difference.

The pilot's draft has no campaign registrations, so the analysis reads an empty `campaigns`
directory instead of the registered campaign files such as
[benchmark/campaigns/h2.kvx](../../benchmark/campaigns/h2.kvx):

```sh
mkdir campaigns
python3 "$REPO/benchmark/analyse.py" --results results --prereg draft.kvx --arms arms.kvx --campaigns campaigns --draft-ok
python3 "$REPO/benchmark/analyse.py" --results results --prereg draft.kvx --arms arms.kvx --campaigns campaigns --draft-ok --check results/analysis.json
```

```text
wrote results/analysis.json; excluded 0; incomplete matrices: H1, H2, H3, H4
report equals the recomputation from ledgers
```

The analysis never pools bins, cassette digests or live and replay runs. It excludes, and reports,
unregistered attempts, Instances with an `INVARIANT_VIOLATION` record, Instances with a record
stamped `language_guard: false` (a turn run with the sandbox language guard switched off) and
attempts that ended in a model or kernel error. Without `--draft-ok` it refuses a draft
registration, such as the pilot's:

```text
$ python3 "$REPO/benchmark/analyse.py" --results results --prereg draft.kvx --arms arms.kvx --campaigns campaigns
analyse.py refused: pre-registration is a draft; missing: campaign.suite_digest, campaign.cassette_digest, meta.status (must be "registered")
```

| Exit | Meaning |
|---|---|
| 0 | Report written, or `--check` matched |
| 1 | `--check` found a difference |
| 2 | Draft registration without `--draft-ok` |
| 3 | `metrics.py` or `arms.kvx` differs from its registered digest |
| 4 | Integrity failure: a ledger disagrees with its cell, or cassette digests would be pooled |
| 5 | `--require-complete` and a cell of the arm matrix is missing |

## Full campaigns

[benchmark/campaigns/h1h2.py](../../benchmark/campaigns/h1h2.py) drives H1 and H2 over the whole
matrix: 60 tasks, 10 seeds and every arm.

| Subcommand | Purpose |
|---|---|
| `plan` | Print the derived drift schedule of every task |
| `register` | Compile the Builds and write the campaign files |
| `run` | Execute missing cells, in parallel, against the registered Builds |
| `status` | Count done and missing cells per arm |
| `analyse` | Run the analysis per hypothesis |
| `report` | Render the research reports |

A campaign runs on a frozen copy of the task suite and the suite cassette set, taken when the
campaign was registered and kept under `.cwr/campaign-1/frozen`. Their paths and digests are
registered in [benchmark/campaigns/h1.kvx](../../benchmark/campaigns/h1.kvx) and
[benchmark/campaigns/h2.kvx](../../benchmark/campaigns/h2.kvx), so a later edit under
`benchmark/tasks` never reaches a running campaign. The copy is local state of the
checkout that registered the campaign. A release bundle does not carry it, so the subcommands run
in that checkout:

```text
$ python3 benchmark/campaigns/h1h2.py status
{
 "H1/drift-1": {
  "done": 0,
  "missing": 600,
  "retry": 0
 },
 ...
```

A hypothesis result needs the complete arm matrix under a registered pre-registration. A changed
Build hash, cassette digest, model identity, tail size, compiler or kernel revision, suite digest
or metric definition invalidates the affected results. A metric changed after execution is a new
pre-registration and a new campaign.

The normative contract for experiments is [spec/cwr/experiments.md](../../spec/cwr/experiments.md).

## What counts as a result

A claim counts only when a qualification attempt backs it. The runner executes a case's task
drivers, the independent checker recomputes every registered assertion, and only an accepted
case-scope attempt changes a case's status in the registry:

```text
python3 tests/cwr/qualify.py --case CWR-NNN
python3 tests/cwr/qualify.py --case CWR-NNN --task W.N
python3 tests/cwr/qualify.py --check-registry
```

The checker also runs on its own and reports the recorded status of a case:

```sh
python3 "$REPO/tests/cwr/check_evidence.py" --status CWR-019
```

> **Verified (CWR-019).** The independent checker [tests/cwr/check_evidence.py](../../tests/cwr/check_evidence.py) accepts the registry's latest CWR-019 record, [20261106T192125Z-1.json](../../tests/cwr/evidence/CWR-019/20261106T192125Z-1.json), written by the runner [tests/cwr/qualify.py](../../tests/cwr/qualify.py) under evidence schema version 2. The checker lists the records written under version 1 as stale.

A hypothesis result needs the complete arm matrix with the pre-registered metric definitions; a
component attempt never implies one, and a run containing `INVARIANT_VIOLATION` is excluded.

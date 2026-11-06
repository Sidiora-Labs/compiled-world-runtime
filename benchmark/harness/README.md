<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Experiment harness

Status: no campaign registered under a final pre-registration has run yet. The harness is covered
by unit tests over synthetic ledgers.

The harness runs one Instance per cell (hypothesis, arm, task, seed) and measures it from the ledger
alone. Each arm is a CWR Build of the reference world with at most one pillar ablated by an
experimental flag. The harness never compiles a Build, never writes into a Build directory and never
references or runs any system outside CWR. Its contract is
[experiments.md](../../spec/cwr/experiments.md).

## Files

| File | Role |
| --- | --- |
| `metrics.py` | The normative metric definitions plus registered conventions C1 to C7. Pure functions with no I/O. Its sha256 is pre-registered. |
| `arms.kvx`, `arms.py` | The eleven arms of H1 to H4, and a validator that refuses any arm that is not a CWR Build with exactly its flag's pillar ablated. The sha256 of `arms.kvx` is pre-registered. |
| `prereg.py` | Loads `benchmark/preregistration.kvx`, checks the `metrics.py` and `arms.kvx` digests and lists campaign inputs that are still unset. |
| `run.py` | The episode loop for one cell. It creates the Instance, composes the context, submits each model reply as one turn, upgrades at the midpoint for H4, applies the stop rule and writes the run layout below. |
| `model.py` | The model client. It reads its endpoint and credentials from the environment, retries with deterministic backoff and never prints, logs or returns a credential. It also provides a deterministic offline model that replays a task's reference solution and opens no socket. |
| `ledgerio.py` | Strict KVX and ledger readers and writers, and predicate loading through `benchmark/predicates/common.py`. |
| `../analyse.py` | Means and 95% percentile bootstrap intervals per arm and bin (per pair for H4), direction labels, arm-matrix completeness and every refusal gate. |
| `tests/` | Unit tests over synthetic ledgers, an in-memory kernel and a local scripted HTTP server. |

## Run one cell

```sh
python3 benchmark/harness/run.py --help
```

`run.py` binds to the `cwr` CLI. It resolves the binary from `$CWR_BIN`, else from
`$CARGO_TARGET_DIR` (default `target/`) under `release/` or `debug/`. It refuses a Build whose
manifest flag is not exactly the arm's flag. It also refuses to run while the pre-registration has
unset inputs, or for a seed that is not registered, unless `--allow-unregistered` is given. Such an
attempt is marked unregistered, and the analysis excludes it.

Each cell writes:

```text
benchmark/results/h<N>/<arm>/<task>/<seed>/
  ledger.jsonl              the exported ledger, byte for byte as the World Kernel wrote it
  final_state.kvx           the final state, in the predicate state view
  states/turn_NNNN.kvx      the state after each turn, used for turns to success
  attempt.kvx               the attempt record, with the harness's own metrics in [metrics]
```

Live-mode runs go under `benchmark/results/live/` and are never pooled with replay runs.

## Stop rule

A run stops after the first turn whose success predicate holds, at the turn limit, when the token
budget is exhausted, on a model error, or when the ledger records `INVARIANT_VIOLATION`.

## Registered choices

The specification leaves some definitions open or states them inconsistently. These choices are
registered in `preregistration.kvx`.

| Topic | Registered choice |
| --- | --- |
| Primitive calls (C1) | Ledger records of kind `primitive` or `adapter`. Failure-rate and probing-ratio definitions only make sense with adapter calls included. |
| Turn limits | The per-bin limits of 15, 40 and 120 turns and the per-Instance limit of 200 are both registered, with 200 as the cap. |
| H3 task set | The full suite, a superset of both task sets the specification names. |
| H3 direction on rule reuse | Registered at K = 20 only, because the drift arms run only at K = 20. |
| H4 "proposal names the changed primitive" | Not registered. A proposal record carries only a text hash, so the ledger cannot answer it. |

## Integrity

`preregistration.kvx` pins every byte of `metrics.py` and `arms.kvx`. Any edit to either file,
including a formatting pass, requires new digests from `python3 benchmark/analyse.py
--print-digests`. That update is allowed only while the pre-registration status is `draft`. Once it
is `registered`, `analyse.py` refuses with exit 3 on any digest mismatch.

## Tests

```sh
python3 -m unittest discover -s benchmark/harness/tests -t .
```

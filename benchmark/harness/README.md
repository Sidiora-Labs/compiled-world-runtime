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
| `run.py` | The shared pre-flight and the episode loop for one cell. It checks the cell against the registration, creates the Instance, composes the context, submits each model reply as one turn, upgrades at the midpoint for H4, applies the stop rule and writes the run layout below. |
| `model.py` | The model client. It reads its endpoint, model identity and keys from the process environment, requires https, never follows a redirect, sends each key once, retries with jittered backoff, quarantines refused keys and never prints, logs or returns a credential, the endpoint or a server message. It also provides a deterministic offline model that replays a task's reference solution and opens no socket. |
| `ledgerio.py` | Strict KVX and ledger readers and writers, and predicate loading through `benchmark/predicates/common.py`. |
| `../analyse.py` | Means and 95% percentile bootstrap intervals per arm and bin (per pair for H4), direction labels, arm-matrix completeness and every refusal gate. |
| `tests/` | Unit tests over synthetic ledgers, an in-memory kernel and a local scripted HTTP server. |

## Run one cell

```sh
python3 benchmark/harness/run.py --help
```

`run.py` binds to the `cwr` CLI. It resolves the binary from `$CWR_BIN`, else from
`$CARGO_TARGET_DIR` (default `target/`) under `release/` or `debug/`. The gateway model reads
`MESSAGES_URL`, `MODEL` and `CLUSTER_<n>_GATEWAY_API_KEY` from the environment of the process.

Every entry point (`run.py`, `benchmark/campaigns/h1h2.py` and the task 5.3 driver) runs the same
pre-flight, `run.preflight`. It refuses a cell when:

- `metrics.py` or `arms.kvx` differs from its registered digest (exit 3);
- the pre-registration has unset inputs, the seed is not registered or the configured model is not
  the registered one, unless `--allow-unregistered` is given, which marks the attempt unregistered;
- the task is not a suite task (or, for H4, a registered upgrade-pair task);
- `--build` differs from the registered Build of the cell: `arms.kvx` for control and flag arms,
  `benchmark/campaigns/h1.kvx` for the per-task drift arms, Build A and the arm's Build B in
  `benchmark/campaigns/h4.kvx` for H4;
- a Build is not on disk or does not carry exactly the arm's flag;
- the cassette set digest or, for H4, the `upgrades.kvx` digest differs from the registration;
- the cell already holds a finished attempt and `--rerun` is not given.

Refusals exit 2 and write nothing. A failed episode exits 4. `run.py --help` lists the codes.

The reported model identity must match the configured one under `[campaign].model_reported_rule`;
any other identity ends the run as `model_error`, and the attempt records only a match flag.

Each cell writes:

```text
benchmark/results/h<N>/<arm>/<task>/<seed>/
  ledger.jsonl              the exported ledger, byte for byte as the World Kernel wrote it
  final_state.kvx           the final state, in the predicate state view
  messages/turn_NNNN.md     the model message submitted as turn N, byte for byte
  kernel.log                the kernel's stderr of every turn
  attempt.kvx               the attempt record, with the harness's own metrics in [metrics]
```

A cell is assembled under `benchmark/results/_staging/` and moved into place only when complete.
An existing cell is never deleted. A cell that ended in `model_error`, `kernel_error`,
`metrics_error` or `predicate_error`, or that has no attempt record, is moved to
`benchmark/results/_replaced/` before a new run takes its place. So is a finished cell rerun with
`--rerun`. A model error is recorded by category and status only.

The runner reads the final state only through `cwr instance state`, `cwr instance keys` and the
ledger. The ledger header records the seed, model identity, arm, tail size, Build hash and cassette
set digest. `analyse.py` computes turns to success by replaying the state after each turn from the
ledger: it creates a fresh Instance from the ledger header, re-submits the recorded messages with
the adapter gateway in replay mode, and requires every replayed record to equal the ledger except
`host_ts`, `duration_us` and `instance`. A message that would change a state changes the ledger, so
the cell is refused with exit 4. No file other than the ledger and the final state can change a
reported number.

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

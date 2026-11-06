<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# CWR experiment harness

Status: unit-tested on synthetic ledgers only. No campaign has run, no Build exists, and nothing here is evidence for CWR-017 or CWR-018. The contract is [experiments.md](../../spec/cwr/experiments.md); requirement 12 in [spec.kvx](../../spec/cwr/spec.kvx) applies.

Every arm is a CWR Build of refworld with at most one pillar ablated by an experimental flag. Nothing in this directory references, invokes or compares against any other agent or harness.

## Files

| File | Role |
|---|---|
| `metrics.py` | Normative ledger-only metric definitions (experiments.md section 4) plus registered conventions C1 to C7. Pure functions, no I/O. Its sha256 is pre-registered. |
| `arms.kvx` / `arms.py` | The eleven arms of H1 to H4 and a validator that refuses any arm that is not a CWR Build with exactly the flag's pillar ablated. |
| `prereg.py` | Loads `benchmark/preregistration.kvx`, checks the metrics.py and arms.kvx digests, lists campaign inputs that are still null. |
| `model.py` | Gateway client: Messages JSON over HTTPS, stdlib only. It reads credentials from the environment at runtime, rotates keys, retries 429 and 5xx with deterministic backoff, and never exposes key values. |
| `run.py` | Episode loop for one (hypothesis, arm, task, seed): create the Instance, run turns through the model, upgrade at the H4 midpoint, stop by the registered stop rule, write the run layout. |
| `ledgerio.py` | Strict KVX and ledger readers and writers; predicate loading through `benchmark/predicates/common.py` when present. |
| `../analyse.py` | Means and 95% percentile bootstrap intervals per arm and bin (per pair for H4), direction labels, matrix completeness, and every refusal gate. |
| `../preregistration.kvx` | H1 to H4, arms, task sets, metric definitions, analysis parameters, directions, digests. **Status draft.** |
| `tests/` | 90 unit tests over synthetic ledgers, a fake kernel and a local scripted HTTP server. |

Run the tests from the repository root:

```sh
python3 -m unittest discover -s benchmark/harness/tests -t .
```

Smoke-check the gateway (one request, max_tokens 20; prints the served model and token counts, never a key):

```sh
python3 benchmark/harness/model.py --smoke
```

Every byte of `metrics.py` and `arms.kvx` is pinned by `preregistration.kvx [digests]`, so any later header, formatting or lint pass that touches them must be followed by `python3 benchmark/analyse.py --print-digests` and an update of `[digests]`. That is allowed only while `meta.status` is `"draft"`. `RepositoryRegistration.test_committed_digests_match` fails until the digests are updated.

## Open items before a campaign

### Runner binding (CWR-017)

1. The runner passes `--arm <H>/<arm-key>` (for example `H1/drift-1`) and the model identity. analyse.py requires these exact values in the ledger header fields `arm` and `model`. It also requires `seed`, `tail_size` (an integer or `"full"`), `build`, `cassette_digest` and the `experimental` stamp on every record (the flag name without dashes, for example `x-drift`). If the kernel uses other spellings, change the kernel binding, not the checks.
2. The state export must be the predicate state view of `benchmark/predicates/common.py` (sections fs, kv, jobs, world, gateway). The runner writes it after each turn to `states/turn_NNNN.kvx`, and analyse.py computes turns to success from those files.
3. Before the model is registered, confirm that the served model identity matches the registered one and that nothing other than the manual acts as a system prompt (model-surface.md section 1). The runner records both `model` and `model_reported` in the attempt record.

### Registration and campaigns (CWR-018)

1. Compile the control and experimental Builds and write their hashes into `arms.kvx` (`[control].build`, `[arm.h2.one-primitive].build`). Freeze the per-task drift Builds in `benchmark/campaigns/h1.kvx` and the per-pair B Builds in `benchmark/campaigns/h4.kvx`. analyse.py does not check per-task or per-pair hashes; the 5.2 and 5.3 drivers must.
2. Fill `[campaign]` in `preregistration.kvx`: `model`, `sampling` (`{}` means no sampling parameter is sent; current models reject `temperature`), `suite_digest`, `cassette_digest`, `upgrades_digest`. Then recompute the digests with `python3 benchmark/analyse.py --print-digests`, write them into `[digests]` and set `meta.status = "registered"`. Do all of this before any run. After execution, any edit to `metrics.py` or `arms.kvx` makes analyse.py refuse (exit 3).
3. H4 runs need an upgrade plan (`run.UpgradePlan`) built from `benchmark/upgrades.kvx`. The CLI refuses H4 until it is wired in. analyse.py reads `[pair.UN]` sections with `task`, `changed_primitives` (a module name without a dot means the whole module), `misuse_code` (null for U6) and an optional `predicate` path.
4. analyse.py reads `benchmark/suite.kvx` `[task.<id>]` sections with `bin` and `reused_rule`, where `reused_rule` is a rule identifier such as `jobs.result[2]:CONFLICT` or an object `{primitive, code}`. If `[meta].suite_digest` is present it must equal the registered digest.
5. Write `drift_spec` into each drift-arm attempt record (`EpisodeSpec.drift_spec`) so drift arms are reported per spec as well as pooled.

## Registered choices that the spec leaves open or states inconsistently

* C1: "primitive calls" counts records of kind `primitive` and `adapter`. experiments.md literally says "kind primitive", but its failure-rate and probing-ratio definitions only make sense with adapter records included. kernel.md logs adapter calls as kind `adapter`, and the suite's `common.py` uses the same pair.
* Turn limits: verification.md gives 15, 40 and 120 per bin; experiments.md gives 200 per Instance. Both are registered, with 200 as the cap.
* H3 task set: experiments.md runs the K sweep on the 20 long tasks; verification.md runs it on the full suite. The full suite is registered, which is a superset of both.
* H3 "rule reuse higher under frozen than drift at every K": the drift arms run only at K = 20, so this direction is registered at K = 20.
* H4 "whether a proposal names the changed primitive": a proposal record carries only a text hash, so this is not computable from the ledger and is not registered.

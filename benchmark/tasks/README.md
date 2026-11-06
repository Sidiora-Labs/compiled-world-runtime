<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# CWR task suite

Status: CWR-015 is UNRUN. Every primitive count below comes from the authoring model in
`_authoring/refsim.py`, not from a kernel ledger, and no cassette set has been recorded. Nothing
here is product evidence.

This directory implements clauses 11.2 and 11.3 of [spec.kvx](../../spec/cwr/spec.kvx), refined
by [reference-world.md](../../spec/cwr/reference-world.md) sections 5 to 8.

## Layout

| Path | Content |
|---|---|
| `tNNN-slug/task.md` | The instruction given to the model, verbatim and identical across arms. It names no rule the task is meant to make the model discover. |
| `tNNN-slug/initial.kvx` | The starting Instance state: sections `[fs]` (`files`, `locks`), `[kv]` (`namespaces`) and `[jobs]` (`jobs`). |
| `tNNN-slug/success.py` | `success(state, ledger) -> bool`. It imports only `benchmark/predicates/common.py`, as `import common as c`. |
| `tNNN-slug/bin` | `short`, `medium` or `long`. |
| `tNNN-slug/rules` | One rule identifier per line. For a rule-reuse task the reused rule is on the first line. |
| `tNNN-slug/task.kvx` | Harness manifest: id, title, bin, rules, `rule_reuse`, `reused_rule` and solution path. Never rendered to the model. |
| `../solutions/tNNN-slug.py` | Reference solution, kept outside the task directory as reference-world.md section 5 requires. |
| `../suite.kvx` | Bins, per-task counts, turns, rules, reuse positions, per-task digests, suite digest, solutions digest, digest of `common.py`, and the cassette digest (null until a cassette set is recorded). |
| `../predicates/common.py` | Predicate helpers, state-view definition, predicate loader and purity audit, double evaluation, digests. |
| `_authoring/` | Generator and checker (`build_suite.py`), task definitions (`tasks_short.py`, `tasks_medium.py`, `tasks_long.py`) and the authoring model (`refsim.py`). This is not a task directory. Consumers select task directories with `common.TASK_DIR_RE` (`^t[0-9]{3}-[a-z0-9-]+$`). |

Edit the definitions in `_authoring/tasks_*.py` and regenerate. Do not edit generated files by hand.

```sh
python3 benchmark/tasks/_authoring/build_suite.py          # regenerate and check
python3 benchmark/tasks/_authoring/build_suite.py --check  # fail if anything on disk is stale
```

The checker runs every reference solution on the authoring model with seeds 1, 2 and 3. It
requires that every turn ends `ok` with no failing call; that the predicate is true, evaluated
twice on deep copies with equal results and unchanged inputs; that the predicate is false when
nothing was done; that counts fall inside the bin and are equal across seeds; that solutions use
at most 200 turns; and that rule-reuse tasks have the required shape. Every `.kvx` file is parsed
with `parse_kvx` from `spec/cwr/tools/validate_spec.py`.

## Suite shape (authoring-model counts)

| Bin | Tasks | Reference primitives | Rule-reuse |
|---|---|---|---|
| short | t001 to t020 | 2 to 4 | 0 |
| medium | t021 to t040 | 8 to 21 | 0 |
| long | t041 to t060 | 30 to 67 | 14 (t041 to t054) |

Counts are kept away from the bin edges (5/6 and 25/26), so a small difference between this model
and the kernel should not move a task between bins. The count basis is ledger records of kind
`primitive` or `adapter`. t003 (search plus write = 2) in reference-world.md section 8 counts
adapter calls, so this suite does too.

Reference solutions are written with knowledge of `initial.kvx`. They make no failing call. They
read only what they must: file contents they transform, search hits, job results, receipts, and
the state of a set the task describes ("every file under ..."). They wait with empty turns, which
cost zero primitives and `tick_per_turn` ticks each.

A rule-reuse task (t041 to t054) uses its reused primitive in the first quarter of the reference
calls, not at all in the middle half, and again in the last quarter at least ten calls later.
`suite.kvx` records the positions. Each instruction fixes the phase order. Each predicate checks
that order from the ledger with `common.brackets(ledger, reused_primitive, middle_primitive)`,
where the middle primitive appears only in the middle phase. Reused rules covered:
`jobs.result[2]:CONFLICT` (twice), `fs.delete[2]:LOCKED` (twice), `fs.move[3]:CONFLICT`,
`kv.set[1]:INVALID`, `kv.set[2]:LIMIT`, `publish.post[rate]:RATE_LIMITED`,
`publish.post[key]:DUPLICATE_KEY`, `search.query[rate]:RATE_LIMITED`, `jobs.submit[2]:LIMIT`,
`fs.write[1]:LOCKED`, `fs.lock[2]:CONFLICT` and `jobs.cancel[2]:CONFLICT`.

## Rule identifiers

`<module>.<primitive>[<selector>][:<CODE>]`, parsed by `common.parse_rule`:

- `[N]:CODE`: declared precondition N (one-based, catalogue order in reference-world.md section 2)
  failing with CODE. These are the only rules a `--x-drift` spec can target.
- `[rate]:RATE_LIMITED`, `[key]:DUPLICATE_KEY`: stub-side rate limit and gateway key idempotency.
- `[shape]`, `[clock]`, `[stream]`: interface shape guarantee, job transitions on world_time, and
  the RNG stream.

The rule reuse metric (experiments.md section 4) needs the primitive and the code. Both are in the
identifier.

## Driver

`tests/cwr/cases/4_2.py` qualifies the suite (CWR-015). For each task it creates an Instance from
the compiled refworld Build, drives `benchmark/solutions/<id>.py` turn by turn through
`cwr instance turn` once in record mode against the stubs, then twice in replay mode against the
resulting cassette. It counts primitives from the ledger, asserts the bin, loads the predicate
only through `common.load_predicate` and evaluates it with `common.evaluate_twice`. If the kernel counts differ from the authoring model, the suite is
regenerated from the kernel ledgers and the new suite digest is recorded. `suite.kvx` is never
edited by hand.

## Kernel assumptions

The reference solutions and predicates rely on the following. A kernel that differs must change,
or the scripts must.

- **Initial state.** `cwr instance create --initial` loads the state that
  `common.kernel_initial_state` maps from `initial.kvx`. Pre-existing jobs carry
  `kind`, `n` and `submitted_at` (may be negative), with optional `result` and `fail_at`.
- **Final state view.** Predicates read the state view documented at the top of `common.py`: fs
  files and locks; kv namespaces; jobs with `status` as of the final world_time and `result` once
  done; world time and turn; and `gateway.idempotency[adapter][key] = {receipt, payload_hash,
  seq}`, the key-adapter store with the ledger seq of the call that produced it. Without the
  gateway section, every publish predicate fails closed.
- **SDK surface.**
  - the modules import as `fs`, `kv`, `jobs`, `search`, `publish` and `world`;
  - calls take keyword arguments named as in the catalogue;
  - a failure raises an exception with `.code`;
  - `fs.read` returns bytes and `fs.write` takes bytes; kv values are `str`;
  - `jobs.submit` and `jobs.result` return `str`;
  - a `SearchHit` has `title`; a `PostReceipt` has `id`;
  - `world.random()` returns a float in [0, 1) or an int;
  - `kv.del` is a Python keyword, so the scripts call `getattr(kv, "del")(...)`.
- **Semantics.**
  - a job is queued while T < t+1, running while T < t+n, and done from t+n;
  - `kv.used(ns)` is the sum of value lengths;
  - search returns at least one hit for limit >= 1 (predicates accept 1 to `limit` hits and
    never compare hit content);
  - the reference solutions wait 101 ticks after the last accepted post and 51 ticks after the
    last accepted query, which keeps them clear of RATE_LIMITED under either a fixed-window or a
    sliding-window stub.

## Cassettes

The cassette set is recorded by running these reference solutions against the stubs. Its digest
is written into `suite.kvx` `cassette_digest`; `build_suite.py` keeps an existing value when it
regenerates.

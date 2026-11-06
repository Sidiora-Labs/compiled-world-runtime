<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Task suite

Status: the primitive counts, turns and reuse positions in `suite.kvx` are measured from the
ledgers of replay-mode runs of the reference solutions on the World Kernel by
`tests/cwr/cases/4_2.py` (case CWR-015) and imported from `_authoring/kernel_counts.json`.
The reference model in `_authoring/refsim.py` is only an authoring pre-check.

The suite is 60 tasks on the reference world, in three bins by length. Each task gives the model one
instruction, starts from a fixed state and is judged by a pure success predicate over the final
state and the ledger. The instruction is identical across arms and never names the rule the task is
meant to make the model discover. The suite implements
[reference-world.md](../../spec/cwr/reference-world.md) sections 5 to 8.

## Layout

| Path | Content |
|---|---|
| `tNNN-slug/task.md` | The instruction given to the model, verbatim |
| `tNNN-slug/initial.kvx` | The starting Instance state: sections `[fs]` (`files`, `locks`), `[kv]` (`namespaces`) and `[jobs]` (`jobs`) |
| `tNNN-slug/success.py` | `success(state, ledger) -> bool`. It imports only `benchmark/predicates/common.py`, as `import common as c`. |
| `tNNN-slug/bin` | `short`, `medium` or `long` |
| `tNNN-slug/rules` | One rule identifier per line. For a rule-reuse task the reused rule comes first. |
| `tNNN-slug/task.kvx` | The harness manifest: id, title, bin, rules, `rule_reuse`, `reused_rule` and solution path. It is never shown to the model. |
| `../solutions/tNNN-slug.py` | The reference solution, kept outside the task directory |
| `../suite.kvx` | Bins, per-task counts and turns, rules, reuse positions, and the suite, solution, predicate-helper and cassette digests |
| `../predicates/common.py` | Predicate helpers, the final-state view, the predicate loader and purity audit, double evaluation and digests |
| `_authoring/` | The suite generator and checker (`build_suite.py`), the task definitions (`tasks_short.py`, `tasks_medium.py`, `tasks_long.py`) and the reference model (`refsim.py`) |

Consumers select task directories with `common.TASK_DIR_RE` (`^t[0-9]{3}-[a-z0-9-]+$`), which
excludes `_authoring/`.

## Change the suite

Edit the definitions in `_authoring/tasks_*.py` and regenerate. Generated files, including
`suite.kvx`, are never edited by hand.

```sh
python3 benchmark/tasks/_authoring/build_suite.py          # regenerate and check
python3 benchmark/tasks/_authoring/build_suite.py --check  # fail if anything on disk is stale
```

After a change that can move a count, measure on the kernel and import the counts:

```sh
python3 tests/cwr/cases/4_2.py --write-counts /tmp/counts.json
python3 benchmark/tasks/_authoring/build_suite.py --kernel-counts /tmp/counts.json
python3 tests/cwr/cases/4_2.py
```

The checker runs every reference solution on the reference model with seeds 1, 2 and 3. It requires
that:

- every turn ends `ok` with no failing call;
- the predicate is true, evaluated twice on deep copies with equal results and unchanged inputs;
- the predicate is false when nothing was done;
- primitive counts fall inside the bin and are equal across seeds;
- a solution uses at most 200 turns;
- rule-reuse tasks have the required shape.

Every `.kvx` file is parsed with the specification validator's strict KVX parser. Regeneration keeps
an existing `cassette_digest` in `suite.kvx`.

## Suite shape

| Bin | Tasks | Reference primitives | Rule-reuse tasks |
|---|---|---|---|
| short | t001 to t020 | 2 to 4 | 0 |
| medium | t021 to t040 | 8 to 21 | 0 |
| long | t041 to t060 | 30 to 67 | 14 (t041 to t054) |

Counts stay away from the bin edges (5 and 6, 25 and 26), so a small difference between the reference
model and the World Kernel does not move a task between bins. A count is the number of ledger
records of kind `primitive` or `adapter`.

Reference solutions are written with knowledge of `initial.kvx` and make no failing call. They read
only what they must: file contents they transform, search hits, job results, receipts, and the state
of any set the task describes. They wait with empty turns, which cost no primitives and
`tick_per_turn` ticks each. They wait 101 ticks after the last accepted post and 51 ticks after the
last accepted query, which keeps them clear of `RATE_LIMITED`.

## Rule reuse

A rule-reuse task (t041 to t054) uses its reused primitive in the first quarter of the reference
calls, not at all in the middle half, and again in the last quarter at least ten calls later.
`suite.kvx` records the positions. Each instruction fixes the phase order, and each predicate checks
it from the ledger with `common.brackets(ledger, reused_primitive, middle_primitive)`, where the
middle primitive appears only in the middle phase.

The reused rules are `jobs.result[2]:CONFLICT` (twice), `fs.delete[2]:LOCKED` (twice),
`fs.move[3]:CONFLICT`, `kv.set[1]:INVALID`, `kv.set[2]:LIMIT`, `publish.post[rate]:RATE_LIMITED`,
`publish.post[key]:DUPLICATE_KEY`, `search.query[rate]:RATE_LIMITED`, `jobs.submit[2]:LIMIT`,
`fs.write[1]:LOCKED`, `fs.lock[2]:CONFLICT` and `jobs.cancel[2]:CONFLICT`.

## Rule identifiers

A rule identifier has the form `<module>.<primitive>[<selector>][:<CODE>]` and is parsed by
`common.parse_rule`.

| Selector | Meaning |
|---|---|
| `[N]:CODE` | Declared precondition N (one-based, in catalogue order) failing with CODE. Only these rules can be targeted by a `--x-drift` Build. |
| `[rate]:RATE_LIMITED` | The stub-side rate limit |
| `[key]:DUPLICATE_KEY` | Idempotency-key reuse at the adapter gateway |
| `[shape]` | The interface shape guarantee |
| `[clock]` | Job transitions on world time |
| `[stream]` | The random number stream |

The rule-reuse metric needs both the primitive and the code, and both are in the identifier.

## Final-state view

Predicates read the state view documented at the top of `common.py`:

| Section | Content |
|---|---|
| `fs` | Files and locks |
| `kv` | Namespaces |
| `jobs` | Jobs, with `status` as of the final world time and `result` once done |
| `world` | World time and turn |
| `gateway` | `idempotency[adapter][key] = {receipt, payload_hash, seq}`, where `seq` is the ledger sequence number of the call that produced the receipt |

Without the `gateway` section, every publish predicate fails closed. `cwr instance create --initial`
loads the state that `common.kernel_initial_state` derives from `initial.kvx`, and
`common.state_view_from_instance` builds the view from `cwr instance state`, the ledger and the
gateway key store (`common.read_key_store`) only. A job `result` is the value refworld's
`jobs.result` returns, `<kind>:<id>:<n>`, so `initial.kvx` never stores one.

## Script conventions

The reference solutions use the generated SDK as follows:

- the modules import as `fs`, `kv`, `jobs`, `search`, `publish` and `world`;
- calls take keyword arguments named as in the contract;
- a refused call raises an exception with `.code`;
- `fs.read` and `kv.get` return bytes; `fs.write` and `kv.set` take bytes;
- `jobs.submit` and `jobs.result` return `str`;
- `kv.del` is a Python keyword; the generated SDK exposes it as `kv.del_`.

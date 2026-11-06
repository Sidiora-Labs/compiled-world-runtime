<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# refworld upgrade pairs U1 to U6

This directory holds six upgrade pairs. Each pair is two complete World Sources, `UN/A` and `UN/B`,
for the world `refworld`. The two sides differ in one rule, as reference-world.md section 9 requires,
plus the prose that describes that rule. Each pair also has a post-upgrade task (`UN/task/`) and
reference turn scripts (`UN/solutions/`). The expected ContractDiff, bump, recompile notice, misuse
signature and model-visible effect for every pair are frozen in `benchmark/upgrades.kvx`.

The frozen values in `benchmark/upgrades.kvx` carry no Build hash (`build_a` and `build_b` are
null), so the notice texts carry `{hash_a}` and `{hash_b}`. Nothing in this directory is evidence
for CWR-011 or CWR-016.

| Pair | Class (reference-world.md section 9) | A to B | Changelog kinds in B's newest release | Pre-upgrade second half fails with |
|---|---|---|---|---|
| U1 | precondition tightened | 0.2.0 to 0.3.0 | precondition_added fs.delete, doc_only fs.lock | fs.delete LOCKED, precondition 2 |
| U2 | primitive removed | 0.2.0 to 0.3.0 | primitive_removed fs.move, doc_only fs.lock | fs.move INVALID (closure refusal) |
| U3 | failure code changed | 0.2.0 to 0.3.0 | precondition_changed jobs.result | jobs.result CONFLICT, precondition 2 |
| U4 | limit changed | 0.2.0 to 0.3.0 | precondition_changed kv.set | kv.set INVALID, precondition 1 |
| U5 | state shape changed, with migration | 0.2.0 to 0.3.0 | state_shape kv (migration), precondition_changed and effect_changed on kv.get, kv.set, kv.del | kv.get INVALID (no ns); a guessed ns gives NOT_FOUND |
| U6 | documentation only (control) | 0.2.0 to 0.2.1 | doc_only fs.write | nothing; both solutions succeed |

## How the sides relate to examples/refworld

- U2/A, U4/A and U6/A are byte-identical copies of `examples/refworld` (0.2.0). A README.md in the
  base, if one reappears, is never copied (`SKIP` in pairs.py).
- U1/A, U3/A and U5/A are 0.2.0 variants that carry the old rule. U1/A has no LOCKED precondition on
  fs.delete. U3/A has NOT_FOUND on jobs.result. U5/A has a flat kv. In each, the docs, fixtures, manual
  prose and changelog history are changed to match. U1/B, U3/B and U5/B equal the base in every rule.
- In every pair, B's CHANGELOG.kvx is A's with one release prepended. B's version satisfies the
  derived bump. In five pairs this uses the pre-1.0 rule P15, where a minor increment satisfies a
  derived major. U6 uses a plain patch.
- U5 moves the base's namespace migration to `migrations/0_2_0_0_3_0_kv_namespaces.py`, following the
  `<from>_<to>_<name>` layout, with the same function body. Its fixtures move to
  `fixtures/kv/migration_0_3_0.kvx`.

The sides are generated, so do not edit `UN/A` or `UN/B` by hand. `pairs.py` builds them from
`examples/refworld` with exact text patches that fail when they do not apply, then checks them:

```sh
python3 examples/refworld-upgrades/pairs.py           # check (exit 0 = PASS)
python3 examples/refworld-upgrades/pairs.py --write   # regenerate after examples/refworld changes, then check
```

The check covers the following:

- regeneration equality and the set of files that may differ in each pair
- `parse_kvx`, and `validate_spec.example_errors` pointed at each side (example-status rule excepted)
- every fs, kv and jobs fixture of each side, run against that side's own lib with its own
  precondition and effect expressions, plus lib/contract parity
- migration fixtures and idempotence
- a recomputed ContractDiff with the changelog gate and bump
- equality with every frozen value in `benchmark/upgrades.kvx`
- each task played on a stand-in kernel. The first half runs on A, then the upgrade runs with its
  migrations, then the post and pre second halves run on B. The predicate must hold for the post
  run and fail for the pre run, and the pre run must fail with the frozen code. The predicate must
  also be false on the initial state.

That stand-in kernel is an authoring model. It has no sandbox, SDK codec, adapters or hashes.

## Open integration points

1. **Kernel driver** (`tests/cwr/cases/4_3.py`, CWR-016):
   - Compile both sides of each pair with `cwr build UN/A`, then `cwr build UN/B --previous <hash A>`.
     Pass `--previous` explicitly, because every A is named refworld at 0.2.0.
   - Compare `contract_diff.json` with `[diff.UN]` and fill `build_a`/`build_b`.
   - Upgrade a live Instance at `upgrade_after_turn`. Compare the stored notice with `[notice.UN].text`
     once the hashes are substituted.
   - Run `post_upgrade` and `pre_upgrade` from `[task.UN]`.
   - Re-freeze `reference_primitive_count` from the kernel and replace `count_source = "authoring-model"`.
2. **ContractDiff and gate.** Adopt or overrule the two coverage rules in
   `[meta]` of upgrades.kvx:
   - world-source.md section 11 has no change kind for a params change, which affects U5.
   - Doc entries next to a rule change are assumed to be covered by that change.

   If either rule is overruled, update the changelogs in `add_release` calls of pairs.py and
   re-freeze.
3. **Notice renderer.** model-surface.md section 5 does not pin the verbs, labels, the
   `(none)` line or the final line for an upgrade without a migration. upgrades.kvx `[meta].notice_template`
   records what is frozen here. If the renderer differs, re-freeze the `text` fields before any
   campaign. Do not change the renderer to match a frozen string that disagrees with the spec.
4. **Closure refusal (U2).** U2's misuse metric needs a ledger record that names
   `fs.move` with code INVALID when a script calls the removed primitive. kernel.md defines closure
   refusals only for imports (`sandbox.import`). The generated SDK must report an unknown attribute
   of a module as such a refusal, or U2's misuse rate cannot be computed from the ledger.
5. **H4 campaign.** Use `[pair.UN]`: `task`, `predicate`, `after_turn`,
   `changed_primitives`, `misuse_code` and `misuse_secondary` for U5. benchmark/analyse.py already
   reads that section. Record `upgrades_digest` in the preregistration over `benchmark/upgrades.kvx`
   and this directory.
6. **Suite tooling.** The pair tasks live here and not under `benchmark/tasks/`. Their
   `initial.kvx` describes state on Build A. U5's `[kv]` section is the flat key map itself, which
   `common.initial_state` does not model. The predicates import only `common`.
7. **README.md files** are outside the World Source layout (CWR-C001). This file sits above the
   sides, and `pairs.py` skips any README.md in the base when it generates them.

## Deviations from the spec text, recorded rather than resolved

- **U4 kind.** reference-world.md calls U4 a "limit changed" pair. The 1024-byte value limit is
  a kv.set precondition, not a `limits` key, so the changelog kind is `precondition_changed` (major).
  `limit_changed` covers only world.kvx `limits` and `clock`.
- **U5 failure code.** reference-world.md lists "NOT_FOUND on kv.get without namespace". world-source.md
  section 5 rule 1 makes a call with a missing parameter INVALID before any precondition runs, so the
  literal pre-upgrade script fails with INVALID, and that is the frozen code and `misuse_code`. The
  NOT_FOUND path is kept as `pre_upgrade_adapted`: a script that adds a guessed namespace instead of
  the migrated `default`.
- **U3 predicate.** The old handler fails only while the job is running, so a pre-upgrade script
  would succeed one turn later. The task therefore requires every turn to end ok, and the predicate
  checks this from the ledger's turn records.
- **world-source.md section 13** says that for a patch release `contracts.json` is byte-identical.
  U6 changes a contract `doc` string, which is in contracts.json. The changelog kind table allows
  doc strings under `doc_only`.
- **An earlier base README** said U5 needs a 0.1.0 flat-kv source. 0.1.0 differs from 0.2.0 in four changes,
  so U5/A is instead a 0.2.0 variant with only kv namespacing reverted. Its quota is global, which
  keeps the difference to one rule.

<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Upgrade pairs U1 to U6

Status: the expected Build hashes in `benchmark/upgrades.kvx` are not frozen yet, so its notice texts
carry `{hash_a}` and `{hash_b}` placeholders.

Each upgrade pair is two complete World Sources of the reference world, `UN/A` and `UN/B`, that differ
in exactly one rule and in the prose that describes it. The pairs exercise controlled change: a human
recompiles A into B with a changelog and a version bump, an Instance is upgraded from A to B, and the
model sees a recompile notice. Each pair also carries a post-upgrade task (`UN/task/`) and reference
turn scripts (`UN/solutions/`).

For every pair, `benchmark/upgrades.kvx` freezes the expected ContractDiff, derived bump, recompile
notice, misuse signature and model-visible effect. The pairs implement
[reference-world.md](../../spec/cwr/reference-world.md) section 9.

## Pairs

| Pair | Change class | A to B | Changelog kinds in B's newest release | Pre-upgrade script fails with |
|---|---|---|---|---|
| U1 | Precondition tightened | 0.2.0 to 0.3.0 | `precondition_added` fs.delete, `doc_only` fs.lock | fs.delete `LOCKED`, precondition 2 |
| U2 | Primitive removed | 0.2.0 to 0.3.0 | `primitive_removed` fs.move, `doc_only` fs.lock | fs.move `INVALID` (closure refusal) |
| U3 | Failure code changed | 0.2.0 to 0.3.0 | `precondition_changed` jobs.result | jobs.result `CONFLICT`, precondition 2 |
| U4 | Limit changed | 0.2.0 to 0.3.0 | `precondition_changed` kv.set | kv.set `INVALID`, precondition 1 |
| U5 | State shape changed, with migration | 0.2.0 to 0.3.0 | `state_shape` kv (migration), `precondition_changed` and `effect_changed` on kv.get, kv.set and kv.del | kv.get `INVALID` (no namespace). A guessed namespace gives `NOT_FOUND`. |
| U6 | Documentation only (control) | 0.2.0 to 0.2.1 | `doc_only` fs.write | Nothing. Both scripts succeed. |

## Compile and upgrade a pair

Every A side is `refworld` 0.2.0, so compile each pair into its own output directory and name the
previous Build explicitly:

```sh
cwr build examples/refworld-upgrades/U1/A --out pair-u1
cwr build examples/refworld-upgrades/U1/B --out pair-u1 --previous <hash-a>
cwr instance create <hash-a> --builds pair-u1 --seed 1
cwr instance upgrade <id> <hash-b> --builds pair-u1
cwr instance notice <id>
```

`cwr build` prints `built sha256:<hash> at <dir>` on success. B passes the changelog gate because its
version satisfies the derived bump: below 1.0.0, a minor increment satisfies a derived major. The
notice opens with `WORLD RECOMPILED  0.2.0 -> 0.3.0` and the two Build hashes.

## How the sides relate to refworld

- U2/A, U4/A and U6/A are byte-identical copies of `examples/refworld`.
- U1/A, U3/A and U5/A are 0.2.0 variants that carry the old rule. U1/A has no `LOCKED` precondition
  on fs.delete, U3/A answers jobs.result with `NOT_FOUND`, and U5/A has a flat kv. Their docs,
  fixtures, manual prose and changelog history match. U1/B, U3/B and U5/B equal refworld in every rule.
- In every pair, B's `CHANGELOG.kvx` is A's with one release prepended.
- U5 moves the namespace migration to `migrations/0_2_0_0_3_0_kv_namespaces.py`, in the
  `<from>_<to>_<name>` layout, with the same function body. Its fixtures move to
  `fixtures/kv/migration_0_3_0.kvx`.

## Generate and check

The sides are generated from `examples/refworld` by exact text patches that fail when they do not
apply. `UN/A` and `UN/B` are never edited by hand.

```sh
python3 examples/refworld-upgrades/pairs.py           # check; exit 0 on PASS
python3 examples/refworld-upgrades/pairs.py --write   # regenerate after a refworld change, then check
```

The check covers:

- byte equality with a fresh regeneration, and the set of files that may differ in each pair;
- strict KVX parsing and the specification validator's cross-file rules on each side;
- every fs, kv and jobs fixture of each side against that side's own `lib/`, preconditions and
  effects, and parity between `lib/` and the contracts;
- migration fixtures and idempotence;
- a recomputed ContractDiff, changelog gate and bump;
- equality with every frozen value in `benchmark/upgrades.kvx`;
- each task played on a Python reference model: the first half on A, the upgrade with its
  migrations, then the post-upgrade and pre-upgrade second halves on B. The predicate holds for the
  post-upgrade run, fails for the pre-upgrade run and is false on the initial state, and the
  pre-upgrade run fails with the frozen code.

The reference model has no sandbox, codec, adapters or hashes. The World Kernel is the authority.

This README sits above the pair sides because the World Compiler rejects any file outside the World
Source grammar. `pairs.py` never copies a README into a side.

## Interpretations of the specification

| Topic | Interpretation |
|---|---|
| U4 change kind | The specification calls U4 a "limit changed" pair. The 1024-byte value limit is a kv.set precondition, not a `limits` key, so the changelog kind is `precondition_changed`. `limit_changed` covers only the `limits` and `clock` sections of `world.kvx`. |
| U5 failure code | The specification lists `NOT_FOUND` for kv.get without a namespace. A call with a missing parameter is `INVALID` before any precondition runs, so the literal pre-upgrade script fails with `INVALID`, and that is the frozen code. The `NOT_FOUND` path is kept as `pre_upgrade_adapted`, a script that guesses a namespace instead of the migrated `default`. |
| U3 predicate | The old rule fails only while the job is running, so a pre-upgrade script would succeed one turn later. The task therefore requires every turn to end `ok`, and the predicate checks this from the ledger. |
| U6 and patch releases | The specification says a patch release leaves `contracts.json` byte-identical. U6 changes a contract `doc` string, which `contracts.json` contains. The changelog kind table allows doc strings under `doc_only`. |
| U5 base | U5/A is a 0.2.0 variant with only kv namespacing reverted, not the 0.1.0 source, which differs from 0.2.0 in four changes. Its quota is global, which keeps the difference to one rule. |
| U2 and U5 misuse records | On the World Kernel, the pre-upgrade call to the removed fs.move (U2) and the kv.get without a namespace (U5) are refused inside the sandbox before any call reaches the kernel. The turn ends `INVALID` naming `turn.script`, and no ledger record names the primitive, so the misuse rate for these two pairs cannot yet be computed from the ledger. |
| Coverage rules | `upgrades.kvx` `[meta]` records two coverage rules for the changelog gate. A parameter entry is covered by a `state_shape` change of the primitive's module in the same release (U5). A doc entry is covered by any change with the same target in the newest release, or by its module's `state_shape` change. |

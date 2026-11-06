<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Reusable World Source module packs

Status: this directory holds no pack yet. The reference world in [examples/refworld](../examples/README.md)
defines its modules inline.

A module pack is a self-contained slice of World Source that more than one world can include: a set
of primitives, the adapters they need, the fixtures their contract tests run against and the manual
sections that describe them. One copy of each contract then serves every world that uses it.

A pack adds no syntax of its own. Every file follows the World Source grammar in
[world-source.md](../spec/cwr/world-source.md).

## Layout

```text
modules/<module>/
  README.md     what the module models, the world limits it assumes, the failure codes it raises
  contracts/    primitive contracts (<module>.kvx): parameters, preconditions, effects, failures
  adapters/     adapter contracts for the external services the module calls, if any
  fixtures/     initial state and contract-test fixtures, run by the World Compiler in an ephemeral Instance
  manual/       manual prose; the rule text of the manual is generated from the compiled contracts
```

A module name is a single lowercase identifier and is the namespace of its primitives, as in
`fs.read` or `kv.set`.

## Rules

- A pack holds World Source only. It never holds compiled output, a Build or anything under `build/`
  or `xbuild/`.
- A pack's library code never reaches outside the Build.
- The World Compiler, not this directory, decides whether a world is valid. A pack is qualified only
  through a world that includes it.
- A pack enters this directory only after a world that includes it compiles with every contract test
  passing.

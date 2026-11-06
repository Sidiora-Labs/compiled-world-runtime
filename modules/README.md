<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# modules: reusable World Source module packs

A module pack is a self-contained slice of World Source (a set of primitives, the adapters
they need, the fixtures their contract tests run against and the manual sections that describe
them) that more than one world can include. The grammar of every file is defined by
[world-source.md](../spec/cwr/world-source.md); a pack adds no syntax of its own.

## Layout

```text
modules/<module>/
  README.md     what the module models, which world limits it assumes, which envelope codes it raises
  contracts/    primitive contracts (<module>.kvx): parameters, preconditions, effects, failures
  adapters/     adapter contracts for the external services the module touches, if any
  fixtures/     initial state and contract-test fixtures the compiler runs in an ephemeral Instance
  manual/       manual prose sections; the generated rule text comes from the compiled contracts
```

A module name is a single lowercase identifier and is the namespace of its primitives
(`fs.read`, `kv.put`). A pack never contains Python library code that reaches outside the
Build, compiled output, or anything under `build/` or `xbuild/`.

## Consumers

[examples/refworld](../examples/refworld) is the world that consumes these packs: its modules
are factored out here so that the reference world, its upgrade pairs and later worlds share one
copy of each contract. The compiler, not this directory, decides whether a world is valid; a
pack is only qualified through a world that includes it.

## Status

Empty. No pack has been factored out of refworld yet. A pack lands here only as a contract the
compiler has already accepted inside a world.

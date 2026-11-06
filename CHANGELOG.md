<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Changelog

All notable changes to the Compiled World Runtime are recorded in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and version numbers follow the rules in
[RELEASE.md](RELEASE.md).

This file covers the runtime, its tools and its documentation. A world records its own rule
changes in its `CHANGELOG.kvx`, and the World Compiler's changelog gate enforces them.

No version has been released, and 19 of the 20 qualification cases have no accepted attempt, so the
entries below describe what the tree contains, not qualified behaviour.

## [Unreleased]

### Added

- The specification in [spec/cwr](spec/cwr/README.md): 14 requirements, 21 implementation tasks and
  20 qualification cases, contract chapters for World Source, the World Compiler, the World Kernel,
  the Model Surface, the reference world, experiments and verification, and a structural
  validator.
- The contract crate `cwr-contract`: a strict KVX reader, the typed World Source model, the
  eleven-code failure envelope and the precondition grammar.
- The World Compiler `cwr-compiler`: parsing, resolution, lint, contract tests, the ContractDiff,
  the changelog gate, SDK generation and Build packaging.
- The World Kernel `cwr-kernel`: Instances, sandboxed turns, atomic primitives, the adapter
  gateway, the ledger, snapshots and upgrades.
- The `cwr` command line, with the subcommands `build`, `lint`, `diff`, `manual`, `contract`,
  `instance` and `release`. `cwr instance` creates Instances, runs turns, takes and restores
  snapshots, upgrades, replays and destroys them. `cwr release` pins and checks release inputs.
- The Python SDK runtime `cwr-sdk` in [sdk/python](sdk/python/pyproject.toml): the codec, the RPC
  client, the sandbox runner and the templates each Build's generated SDK is rendered from. It uses
  the standard library only.
- The reference world in [examples/refworld](examples/README.md), with `fs`, `kv`, `jobs` and
  `world` modules and `search` and `publish` adapters, and the six upgrade pairs U1 to U6.
- The experiment suite in [benchmark](benchmark/): 60 tasks with reference solutions and success
  predicates, stub servers for the `search` and `publish` adapters, cassette tooling, the
  experiment harness, the pre-registration file, campaign definitions for H1 and H2 and the
  ledger analysis `benchmark/analyse.py`.
- Qualification tooling in [tests/cwr](tests/cwr/): the evidence registry for the 20 cases, the
  runner `qualify.py`, the independent checker `check_evidence.py`, the closure tool `closure.py`
  and the task drivers under `cases/`.
- Release tooling: [tools/release.py](tools/release.py) writes byte-reproducible source bundles,
  and [benchmark/release.kvx](benchmark/release.kvx) names every release pin with its diagnostic.
- Repository tooling: the source header checker and the code map generator in
  [tools](tools/README.md), the operator scripts in [scripts](scripts/README.md) and a
  [Makefile](Makefile) with `check`, `test`, `qualify`, `closure`, `vendor` and `release` targets.
- Documentation: the [README](README.md), the guides in [docs/cwr](docs/cwr/README.md), the
  [release procedure](RELEASE.md), the [security policy](SECURITY.md) and the
  [contribution](CONTRIBUTING.md) and [governance](GOVERNANCE.md) documents.

<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

<!-- Generated from the tree by tools/codemap.py. Do not edit by hand; run `python3 tools/codemap.py` to regenerate. -->

# CWR code map

A map of the Compiled World Runtime repository, generated from the tree. The specification is [spec/cwr/spec.kvx](spec/cwr/spec.kvx), and the rules for changing the code are in [CONTRIBUTING.md](CONTRIBUTING.md).

## Languages

Counts cover source files only. Hidden paths, ignored build output, recorded data (evidence, results and cassettes) and vendored code are excluded.

| Language | Files | Lines |
|---|---:|---:|
| Python | 317 | 58437 |
| Rust | 74 | 33991 |
| KVX | 752 | 32050 |
| Markdown | 161 | 8517 |
| JSON | 62 | 1219 |
| Text | 3 | 296 |
| Lock file | 1 | 278 |
| Task data | 132 | 216 |
| Shell | 3 | 174 |
| TOML | 8 | 144 |
| Python template | 5 | 97 |
| Makefile | 1 | 81 |
| Mermaid | 2 | 51 |
| YAML | 1 | 31 |
| **Total** | **1522** | **135582** |

## Layout

| Directory | Purpose |
|---|---|
| `artifacts/` | Release bundles |
| `benchmark/` | Experiments: task suite, stubs, harness and analysis |
| `benchmark/harness/` | Experiment harness |
| `benchmark/harness/tests/` | Unit tests for the CWR experiment harness. Synthetic ledgers only; no kernel, no network. |
| `benchmark/stubs/` | Stub servers for the reference world's adapters |
| `benchmark/tasks/` | Task suite |
| `core/` | Shared Python reference implementations |
| `core/cwr_core/` | Shared Python reference implementations for CWR tooling. |
| `core/tests/` | Unit tests for cwr_core.kvx and cwr_core.envelope; run from the root as the README shows. |
| `crates/` | Rust workspace: cwr-contract, cwr-compiler, cwr-kernel, cwr-cli |
| `crates/cwr-cli/` | The cwr command line: one module per subcommand. |
| `crates/cwr-compiler/` | CWR World Compiler: parse, resolve, lint, contract tests, diff, changelog gate, generate, package. |
| `crates/cwr-contract/` | CWR contract crate: strict KVX reader, typed World Source model, closed failure envelope, precondition grammar. |
| `crates/cwr-kernel/` | CWR World Kernel: Instances, sandboxed turns, atomic primitives, adapter gateway, ledger, snapshots, upgrade. |
| `docs/` | See `docs/cwr/`. |
| `docs/cwr/` | CWR user guides |
| `examples/` | Reference world and upgrade pairs |
| `examples/refworld-upgrades/` | refworld upgrade pairs U1 to U6 |
| `modules/` | Reusable World Source module packs |
| `scripts/` | Operator scripts |
| `sdk/` | See `sdk/python/`. |
| `sdk/python/` | Generated-SDK runtime for sandboxed scripts |
| `spec/` | See `spec/cwr/`. |
| `spec/cwr/` | Compiled World Runtime (CWR) |
| `tests/` | See `tests/cwr/`. |
| `tests/cwr/` | Qualification registry, runner, checker, drivers, evidence |
| `tools/` | Repository tooling |
| `tools/tests/` | Unit tests for tools/codemap.py and tools/release.py; run from the root as the README shows. |
| `vendor/` | Vendored crate sources |

## Crates

| Crate | Path | Depends on (workspace) | Description |
|---|---|---|---|
| `cwr-cli` | `crates/cwr-cli/` | `cwr-compiler`, `cwr-contract`, `cwr-kernel` | The cwr command line: one module per subcommand. |
| `cwr-compiler` | `crates/cwr-compiler/` | `cwr-contract`, `cwr-kernel` | CWR World Compiler: parse, resolve, lint, contract tests, diff, changelog gate, generate, package. |
| `cwr-contract` | `crates/cwr-contract/` | - | CWR contract crate: strict KVX reader, typed World Source model, closed failure envelope, precondition grammar. |
| `cwr-kernel` | `crates/cwr-kernel/` | `cwr-contract` | CWR World Kernel: Instances, sandboxed turns, atomic primitives, adapter gateway, ledger, snapshots, upgrade. |

## Make targets

| Target | Prerequisites | Does |
|---|---|---|
| `make all` | `build` | - |
| `make build` | - | compile the Rust workspace in release mode |
| `make test` | `build` | cargo tests plus harness and stub self-tests |
| `make fmt` | - | rewrite Rust and Python sources into the repository format |
| `make fmt-check` | - | fail when any source is not formatted |
| `make lint` | - | clippy (warnings are errors) and ruff |
| `make headers` | - | fail when any file lacks the Sidiora source header |
| `make headers-apply` | - | insert the header where it is missing |
| `make validate` | - | structural validation of the specification pack |
| `make check` | `headers`, `fmt-check`, `lint`, `validate` | headers, fmt-check, lint, validate (the pre-merge gate) |
| `make qualify` | `build` | run every task driver through the qualification runner |
| `make closure` | - | assemble evidence for all cases and print the release verdict |
| `make vendor` | - | vendor crate sources for offline reproducible builds |
| `make release` | `check`, `test`, `qualify` | build, qualify and bundle release artifacts into artifacts/ |
| `make clean` | - | remove build outputs |

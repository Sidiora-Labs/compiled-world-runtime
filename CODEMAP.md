<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

<!-- Generated from the tree by tools/codemap.py. Do not edit by hand; run `python3 tools/codemap.py` to regenerate. -->

# CWR code map

A map of the Compiled World Runtime repository, generated from the tree. The specification is [spec/cwr/spec.kvx](spec/cwr/spec.kvx), and the rules for changing the code are in [CONTRIBUTING.md](CONTRIBUTING.md).

## Languages

Counts cover source files only. Hidden paths, ignored build output, recorded data (evidence, results and cassettes) and vendored code are excluded.

| Language | Files | Lines |
|---|---:|---:|
| Python | 327 | 71282 |
| Rust | 79 | 41160 |
| KVX | 774 | 33850 |
| Markdown | 168 | 10574 |
| JSON | 83 | 1885 |
| Text | 4 | 317 |
| Lock file | 1 | 278 |
| Shell | 3 | 190 |
| Task data | 132 | 186 |
| TOML | 8 | 146 |
| Python template | 5 | 97 |
| Makefile | 1 | 83 |
| Mermaid | 2 | 51 |
| YAML | 1 | 31 |
| **Total** | **1588** | **160130** |

## Layout

| Directory | Purpose |
|---|---|
| `.cargo/` | Resolve every crates.io dependency from the sources pinned in vendor/cargo, so the workspace builds without network access |
| `.githooks/` | Git hooks that enforce the commit rules |
| `artifacts/` | Release bundles |
| `benchmark/` | Experiments: task suite, stubs, harness and analysis |
| `benchmark/harness/` | Experiment harness |
| `benchmark/harness/tests/` | Unit tests for the CWR experiment harness. Synthetic ledgers only; no kernel, no network. |
| `benchmark/predicates/tests/` | Tests of benchmark/predicates/common.py. |
| `benchmark/stubs/` | Stub servers for the reference world's adapters |
| `benchmark/tasks/` | Task suite |
| `core/` | Shared Python reference implementations |
| `core/cwr_core/` | Shared Python reference implementations for CWR tooling. |
| `core/tests/` | Unit tests for cwr_core.kvx and cwr_core.envelope; run from the root as the README shows. |
| `crates/` | Rust workspace: World Compiler, World Kernel and the cwr CLI |
| `crates/cwr-cli/` | The cwr command line: one module per subcommand. |
| `crates/cwr-compiler/` | CWR World Compiler: parse, resolve, lint, contract tests, diff, changelog gate, generate, package. |
| `crates/cwr-contract/` | CWR contract crate: strict KVX reader, typed World Source model, closed failure envelope, precondition grammar. |
| `crates/cwr-kernel/` | CWR World Kernel: Instances, sandboxed turns, atomic primitives, adapter gateway, ledger, snapshots, upgrade. |
| `docs/` | Holds `docs/cwr/`: CWR user guides |
| `docs/cwr/` | CWR user guides |
| `examples/` | Reference world and upgrade pairs |
| `examples/refworld-upgrades/` | Upgrade pairs U1 to U6 |
| `modules/` | Reusable World Source module packs |
| `research/` | Hypothesis reports |
| `scripts/` | Operator scripts |
| `sdk/` | Holds `sdk/python/`: Python SDK runtime |
| `sdk/python/` | Python SDK runtime |
| `spec/` | Holds `spec/cwr/`: Compiled World Runtime (CWR) |
| `spec/cwr/` | Compiled World Runtime (CWR) |
| `tests/` | Holds `tests/cwr/`: Qualification: registry, runner, checker and evidence |
| `tests/cwr/` | Qualification: registry, runner, checker and evidence |
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

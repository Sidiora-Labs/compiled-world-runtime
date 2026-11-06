<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# docs/cwr: status and hand-off

These user guides were first written on 2026-11-06 by pre-task A6, before any CWR implementation existed, and refreshed the same day once code from waves 0 to 2 began landing. They are derived from the contract chapters in [spec/cwr](../../spec/cwr/README.md). They describe the specified behaviour, not the code in the tree. They mark which source files exist, and they record differences between the code and the specification in the snapshot below so that nobody mistakes the code for the contract.

| Guide | Audience | Derived from |
|---|---|---|
| [world-source.md](world-source.md) | World authors | `spec/cwr/world-source.md`, `spec/cwr/compiler.md` |
| [kernel.md](kernel.md) | Operators | `spec/cwr/kernel.md`, `spec/cwr/model-surface.md`, `spec/cwr/compiler.md` section 9 |
| [experiments.md](experiments.md) | Experimenters | `spec/cwr/experiments.md`, `spec/cwr/verification.md`, `spec/cwr/compiler.md` section 10 |

Related release scaffolding at the repository root: [README.md](../../README.md), [CHANGELOG.md](../../CHANGELOG.md), [RELEASE.md](../../RELEASE.md) and [benchmark/release.kvx](../../benchmark/release.kvx).

## Marker convention

Each section that describes program behaviour opens with one of two blockquotes:

- `> **Pending verification.**` names the evidence case, the task driver, and the implementing source split into "Source in tree, unverified" (the file exists but no executed case shows its behaviour) and "Proposed source, not yet written". A file being present is not evidence. These paths stay in backticks rather than links while implementation is in flux, so the link check does not depend on files other tasks are still moving.
- `> **Verified, <tier>.**` is used only when the named case has an attempt record that the independent checker accepted and that the runner wrote into `tests/cwr/registry.kvx`. It links the record, the source and the driver. On 2026-11-06 this applies to one section only: experiments.md section 10, covered by CWR-019.

Every guide ends with an "Open points in the contract" section listing disagreements between specification chapters that the guide does not resolve.

## Implementation snapshot (2026-11-06)

This snapshot was read from the tree at authoring time. Other tasks were editing the tree concurrently, so treat it as a starting checklist for task 6.1, not as a description of current behaviour. None of the items below has been exercised by a recorded case.

Evidence registry: of CWR-001 to CWR-020, only CWR-019 has an executed attempt. It is a PASS at component tier, and `python3 tests/cwr/check_evidence.py --status CWR-019` reports PASS with no integrity errors. Every other case is UNRUN. `spec/cwr/spec.kvx` still lists every case as UNRUN because only task 6.2 updates statuses there.

Present in the tree: `crates/cwr-contract`, `crates/cwr-compiler`, `crates/cwr-kernel`, `crates/cwr-cli`, `sdk/python/cwr`, `examples/refworld`, `examples/refworld-upgrades`, the stub servers, the 60 tasks and reference solutions, `benchmark/harness`, `benchmark/analyse.py`, `benchmark/preregistration.kvx`, `tests/cwr/qualify.py`, `tests/cwr/check_evidence.py`, and drivers `tests/cwr/cases/0_1.py` to `2_4.py`. Not yet written: drivers for tasks 2.5 and later, `research/`, `benchmark/campaigns/`, `benchmark/results/`, `sdk/python/pyproject.toml`, and the source files marked "proposed" in the guides.

Differences between the `cwr` binary in `crates/cwr-cli/src` and the command tables in `spec/cwr/compiler.md` section 9 and `spec/cwr/kernel.md` section 2:

| Area | Specification | Code in tree | Owner |
|---|---|---|---|
| `cwr instance upgrade` | Specified with exit codes 0, 5 and 6 | No such subcommand | Task 2.5 |
| `cwr build --x-*` | Writes an experimental Build under `xbuild/`, marked so the kernel refuses it without `--allow-experimental` | Flags are parsed and checked for mutual exclusion, then not passed to the compiler. A flagged build is therefore indistinguishable from the control Build | Task 3.2 |
| `cwr build --debug-effects` | Build option | Accepted and ignored. Effect assertion is a `cwr instance turn` option instead | Tasks 1.3 and 2.2 |
| `cwr instance turn` | `<id> < script.py`; exit 4 on protocol INVALID | Adds `--script`, `--debug-effects`, `--gateway live\|record\|replay`, `--cassette`, `--cassette-label`, `--stub NAME=URL` and `--diagnostic-no-language-guard`; no exit 4 | Tasks 2.2, 2.3 and 3.1 |
| `cwr instance create` | `<build-hash> [--seed N] [--tail K] [--arm LABEL] [--from-snapshot S] [--allow-experimental]` | Also accepts `--build <dir>`, a directory in place of a hash, `--tail full`, `--model M` and `--initial <state.json>` | Task 2.1 |
| `cwr instance replay` | `<id> --into <new-id>` | `(<id> \| <ledger.jsonl>) [--build] [--cassette] [--scripts] [--initial] [--into-home]`; exit 7 on divergence, 2 on hash mismatch | Task 2.4 |
| Extra subcommands | Not specified | `cwr contract check\|dump\|emit\|codes\|envelope\|expr`; `cwr instance show\|state\|ledger\|tail\|probe\|recover\|compare` | Tasks 0.3, 2.1 and 2.4 |
| On-disk locations | Not fixed (kernel guide open point) | Instances under `--home`, default `$CWR_HOME` else `.cwr`, in `instances/<ULID>/`; the outbox is `proposals.jsonl` inside the Instance directory; Builds resolve from `build/` and `xbuild/` or from `--builds` | Tasks 2.1 and 2.5 |
| `cwr diff`, `cwr manual` | Arguments are Build hashes | Each argument is also accepted as a Build directory, and `cwr diff` also takes a World Source directory | Tasks 1.2 and 1.3 |

Each of these is either a specification change, recorded in `spec/cwr/` by the owning task, or a code fix. Until one of those happens the guides keep showing the specified form.

## What later tasks must do

- **Each implementation task** that changes a CLI flag, exit code, file location or behaviour described here reports the change to task 6.1 instead of editing these guides piecemeal, and adds one line to `CHANGELOG.md` under Unreleased.
- **Tasks 0.3, 1.2, 2.1, 2.3, 2.5, 3.1, 3.2, 4.1, 5.1 and 5.3** own the open points at the end of each guide and the rows of the snapshot table above: undeclared adapter keys, the pre-1.0 patch rule, raw-script turns, the `world.propose` side-effect class, gateway-mode selection, the context rendering command, on-disk locations, the `kv.set` signature, turn limits, the H3 task set, the harness CLI and experimental-flag plumbing. Each resolution goes into the specification first.
- **Task 6.1** finalises the guides. For each section whose case has an accepted attempt in the registry, it replaces the pending block with a verified block that links the source and the record. It keeps the pending block for anything still proposed. It turns the backticked source paths into links once they are stable, makes every command shown run against the packaged artifacts, documents packaging and installation, fills every pin in `benchmark/release.kvx`, and settles the diagnostic names CWR-R001 to CWR-R015 in that file, in `RELEASE.md` and in its driver `tests/cwr/cases/6_1.py`.
- **Task 6.2** fills `evidence.closure` and `evidence.owner_acceptance` in `benchmark/release.kvx` from the registry. It cuts the release section of `CHANGELOG.md` only when closure lists no blocking item.

## Checks run at authoring time

A6 ran these from a scratch script; they are not part of the product. `benchmark/release.kvx` parses with the strict KVX reader in `spec/cwr/tools/validate_spec.py`. Every local markdown link in these files and in the root documents resolves. No file has trailing whitespace. Every `cwr` command shown in the guides and root documents appears in the specification's command tables. Every path labelled "in tree" exists and every path labelled "not yet written" does not. These checks are authoring receipts, not evidence.

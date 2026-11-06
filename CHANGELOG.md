<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Changelog

All notable changes to the Compiled World Runtime repository are recorded here. This file tracks the repository and its tools. A world's own rule changes are recorded in that world's `CHANGELOG.kvx` and are enforced by the compiler's changelog gate; they do not belong here.

Entries describe what was added to the tree. An entry is not evidence that the added code behaves as specified; behaviour is established only by executed qualification cases recorded in the evidence registry. Until a release is cut, every entry sits under Unreleased. The release procedure is in [RELEASE.md](RELEASE.md).

## [Unreleased]

### Added

- 2026-11-06: Specification pack `spec/cwr/` for the Compiled World Runtime: 14 requirements, 21 pending implementation tasks in waves 0 to 6, 20 UNRUN qualification cases, contract chapters for World Source, compiler, kernel, model surface, reference world, experiments and verification, an illustrative refworld World Source and the authoring validator `spec/cwr/tools/validate_spec.py`. This pack replaces the earlier seven-document narrative draft.
- 2026-11-06: User guides `docs/cwr/world-source.md`, `docs/cwr/kernel.md` and `docs/cwr/experiments.md`, the repository `README.md`, this changelog, `RELEASE.md` and the unfilled release pin skeleton `benchmark/release.kvx`. Every behavioural statement in the guides is marked pending verification, except the runner and checker section, which links the accepted CWR-019 attempt. `docs/cwr/README.md` records a snapshot of where the code in the tree departs from the specified command tables. Task 6.1 finalises them.

<!-- Implementation tasks add one line each under "Added" or "Changed" as they land, naming the task id and the paths they created. Task 6.1 moves the section under a version heading when a release is cut. -->

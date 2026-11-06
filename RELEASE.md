<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Releasing CWR

Status: **no release exists and none can be claimed yet.** This document is the procedure a release must follow; it is derived from requirement 14 in [spec/cwr/spec.kvx](spec/cwr/spec.kvx), tasks 6.1 and 6.2 and stage G of [spec/cwr/verification.md](spec/cwr/verification.md). Every step that runs a program is **pending verification**. Some of the programs it names are in the tree (the `cwr` binary from `crates/cwr-cli`, without `cwr instance upgrade` and without working experimental flags as of 2026-11-06), but none has been exercised by a recorded case, and the release drivers `tests/cwr/cases/6_1.py` and `tests/cwr/cases/6_2.py` do not exist yet. Task 6.1 finalises the packaging steps and task 6.2 the closure steps.

## What a release is

A release is a set of pinned inputs from which every documented result can be reproduced, plus a closure record showing that every qualification case has been executed and the owner has accepted the result. A release is not a version number on its own, and it is not a passing authoring validation.

## Conditions

A release claim requires all of the following. An unresolved required failure blocks the claim; a missing input never lowers a requirement.

1. **Pinned inputs** (clause 14.3): compiler, kernel and SDK revisions, the refworld control and experimental Build hashes, the cassette set digest and the task suite digest are recorded in [benchmark/release.kvx](benchmark/release.kvx). A missing input produces the named diagnostic listed in that file.
2. **Documentation** (clause 14.2): the guides under [docs/cwr](docs/cwr/world-source.md) and the [README](README.md) describe implemented behaviour with links to source and evidence, and still mark anything proposed or unimplemented as such.
3. **Traceability** (clause 14.1): the authoring validator passes and the derived views in `spec/cwr/` are current. This is necessary but never sufficient; its PASS is not an input to closure.
4. **Closure** (clause 14.4): every case CWR-001 to CWR-020 has an executed attempt with an independent checker result, the four hypothesis reports carry their pre-registration, and owner acceptance is recorded against exact Build hashes.

## Procedure

> **Pending verification.** Evidence: CWR-020 (drivers `tests/cwr/cases/6_1.py` and `tests/cwr/cases/6_2.py`, no attempt recorded). The commands below are named in the specification; none has run.

1. Check the specification pack:

   ```sh
   python3 spec/cwr/tools/validate_spec.py
   ```

2. Build the control world and every experimental Build from the same source, recording each hash:

   ```sh
   cwr build <source>
   cwr build <source> --x-drift <drift.kvx>
   cwr build <source> --x-one-primitive
   cwr build <source> --x-version-only
   cwr build <source> --x-silent-change
   ```

3. Confirm that each experimental Build differs from its control only by flag-attributable entries:

   ```sh
   cwr diff <hash-a> <hash-b>
   ```

4. Record in `benchmark/release.kvx`: the compiler, kernel, SDK and CLI revisions; the control, experimental and upgrade-pair Build hashes; the cassette set digest and task suite digest, each equal to the value frozen in `benchmark/suite.kvx`; the specification digest.
5. Package the `cwr` CLI and the Python SDK with pinned dependencies and record their checksums under `artifact.cli` and `artifact.sdk`. The packaging format and install instructions are decided by task 6.1.
6. From the packaged artifacts, exercise the running journey: `cwr build`, `cwr instance create`, `cwr instance turn`, `cwr instance upgrade` and `cwr instance replay`.
7. Run the documentation and release driver, which checks that every documented command runs against the packaged artifacts, every evidence link resolves, the release file is complete and no document claims a result without an attempt record:

   ```sh
   python3 tests/cwr/cases/6_1.py
   ```

8. Assemble closure and record owner acceptance (task 6.2). Closure refuses missing, tampered, stale-Build or zero-assertion evidence and lists every blocking item:

   ```sh
   python3 tests/cwr/cases/6_2.py
   ```

9. Only when closure lists no blocking item: move the Unreleased section of [CHANGELOG.md](CHANGELOG.md) under the release version, set `meta.status` in `benchmark/release.kvx` to its final value and fill `evidence.closure` and `evidence.owner_acceptance`.

## Diagnostics

`benchmark/release.kvx` proposes one named diagnostic per pin, CWR-R001 to CWR-R015. Each names the missing key and blocks the release claim. Task 6.1 may renumber them; whatever it settles on is the list its driver emits and this section must match.

| Diagnostic | Missing or failing input |
|---|---|
| CWR-R001 to CWR-R004 | Compiler, kernel, SDK and CLI revisions |
| CWR-R005 | refworld control Build hash |
| CWR-R006 to CWR-R009 | Drift, one-primitive, version-only and silent-change Build hashes |
| CWR-R010 | Upgrade pair Build hashes |
| CWR-R011, CWR-R012 | Cassette set and task suite digests |
| CWR-R013, CWR-R014 | Packaged CLI and SDK checksums |
| CWR-R015 | Closure or owner acceptance |

## What never counts

- The authoring validator's PASS.
- A component PASS standing in for its parent case, a hypothesis or the release.
- A driver's exit code without an attempt record and an independent checker result.
- Evidence recorded against a different Build hash, cassette digest, suite digest, model identity, K, compiler or kernel revision, or metric definition than the release pins.
- Any comparison with a system outside CWR; every arm is a CWR Build.

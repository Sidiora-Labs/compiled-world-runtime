<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Governance

CWR is owned by Sidiora Labs Inc. The project is governed by the rule CWR applies to its own
worlds: nothing changes silently. Every change to behaviour is written into the specification
first, recorded in the changelog and checked by the specification validator before code follows.

## Roles

| Role | Held by | Responsibilities |
|---|---|---|
| Owner | Sidiora Labs Inc. | Accepts or rejects changes to the specification, approves pre-registrations before any campaign runs, records owner acceptance at release closure and decides what is published |
| Maintainers | Sidiora Development Team ([CONTRIBUTORS.md](CONTRIBUTORS.md)) | Carry out day-to-day development and review, and handle security reports as described in [SECURITY.md](SECURITY.md) |
| Task owner | The role named by a task's `owner` key in the specification | Answers for the paths the task may change and for its driver |
| Contributor | Anyone who follows [CONTRIBUTING.md](CONTRIBUTING.md) | Proposes changes |

The task owner roles are `spec`, `contracts`, `compiler`, `kernel`, `surface`, `refworld`,
`experiments`, `qualification` and `release`.

## The specification comes first

The normative specification is [spec/cwr/spec.kvx](spec/cwr/spec.kvx), refined by the contract
chapters next to it in `spec/cwr/`. Behaviour that the specification does not state is not a
feature. A contradiction between code and specification is a defect in one of them, and the owner
decides which.

A change to a requirement, clause, task or case follows one path:

1. Edit `spec/cwr/spec.kvx` and, where the contract changes, the chapter that refines it.
2. Regenerate the derived views, then validate:

   ```sh
   python3 spec/cwr/tools/validate_spec.py --render
   python3 spec/cwr/tools/validate_spec.py
   ```

   Each derived view records a digest of every requirement, clause, task and case, so an edit
   made without `--render` fails validation.
3. Record the change in [CHANGELOG.md](CHANGELOG.md).

A deviation from a clause, such as fewer seeds than registered or a deferred check, is recorded in
`spec/cwr/qualification.kvx`. It is never absorbed silently.

## Status changes

Task and case statuses are evidence, not opinion. While the specification's stage is `design`,
every task stays `pending` and the validator refuses any other status. A task advances only with a
driver that exits 0. A case leaves `UNRUN` only through an attempt record that the independent
checker has accepted into [tests/cwr/registry.kvx](tests/cwr/registry.kvx).

The validator's PASS shows that the specification is internally consistent. It is not evidence
that the software behaves as specified, and no release closure accepts it as such.

## Research rules

- CWR is never compared with, modelled on or run against any other system. Every experimental arm
  is a CWR Build with one pillar removed.
- A metric, threshold or seed count is fixed by pre-registration before a campaign runs. Changing
  one after execution creates a new pre-registration and a new campaign. Earlier results stay under
  their original registration.
- A reported number must equal an independent recomputation from ledgers by
  `benchmark/analyse.py`.
- A hypothesis is supported only by the complete pre-registered arm matrix. Evidence about one
  component never implies a hypothesis result.

## Releases

A release is a set of pinned inputs plus a closure record that carries owner acceptance. The
procedure is in [RELEASE.md](RELEASE.md), and the pins live in `benchmark/release.kvx`.

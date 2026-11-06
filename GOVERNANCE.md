<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Governance

## Ownership

The Compiled World Runtime is a research system owned by Sidiora Labs Inc. Sidiora Labs is the
owner the specification refers to: it accepts or rejects changes to the specification, approves
pre-registrations before any campaign runs, records owner acceptance at release closure, and
decides what is published. Day-to-day work is carried out by the Sidiora Development Team
([CONTRIBUTORS.md](CONTRIBUTORS.md)).

Each task in the specification names an owning role (`owner = "spec"`, `"qualification"`,
`"release"` and so on). The role owns the task's `touches` and answers for its driver.

## Spec first

The normative specification is [spec/cwr/spec.kvx](spec/cwr/spec.kvx) with the contract chapters
next to it. Behaviour that is not in the specification is not a feature, and a contradiction
between code and specification is a defect in one of them, resolved by the owner.

A change to requirements, clauses, tasks or cases follows one path:

1. Edit `spec/cwr/spec.kvx` and, where the contract changes, the chapter that refines it.
2. Regenerate the derived views and validate:

   ```sh
   python3 spec/cwr/tools/validate_spec.py --render
   python3 spec/cwr/tools/validate_spec.py
   ```

   Both views record a digest of every requirement, clause, task and case, so an edit that skips
   `--render` fails validation.
3. Keep task statuses honest. While `[meta] stage` is `"design"`, every task stays `"pending"` and
   the validator refuses anything else. In the implementation stage a task moves through
   `in_progress`, `implemented` and `done` only with its driver, and a case leaves `UNRUN` only
   through a checker-accepted attempt record in `tests/cwr/registry.kvx`.
4. Record the change in [CHANGELOG.md](CHANGELOG.md). A deviation from a clause (fewer seeds, a
   deferred check, a renumbered diagnostic) is recorded in the task result and in
   `spec/cwr/qualification.kvx`; it is never silently absorbed.

The authoring validator's PASS shows that the pack is consistent. It is not product evidence and
no closure accepts it.

## No silent change

CWR's thesis is that a world changes only through a human recompilation that is versioned,
documented and announced to the model. The repository is governed the same way: no rule, metric,
threshold, failure code or pre-registered parameter changes without a specification edit, a
regenerated view and a changelog entry. A metric changed after execution is a new
pre-registration and a new campaign; earlier results stay under their original registration.

## Research rules

- No external baselines: CWR is never compared with, modelled on, or run against any other agent
  or harness. Every arm is a CWR Build with one pillar ablated.
- Reported numbers equal an independent recomputation from ledgers by `benchmark/analyse.py`.
- A hypothesis is supported only by the complete pre-registered arm matrix; component evidence
  never implies a hypothesis result.

## Releases

A release is a set of pinned inputs plus a closure record with owner acceptance. The procedure is
[RELEASE.md](RELEASE.md); the pins live in `benchmark/release.kvx`.

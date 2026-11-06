<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Security Policy

CWR is the runtime in which an AI model acts by running scripts inside a compiled,
rule-governed world. It isolates those scripts, enforces the world's contracts, records every action
in a ledger and mediates every call to an outside service. Treat any defect that lets a script
escape its sandbox, changes a rule or a Build without a recompilation, corrupts or forges a ledger,
makes a replay diverge, breaks an upgrade, duplicates an external side effect or exposes a
credential as a security issue.

## Report privately

Report suspected vulnerabilities by email to <security@sidioralabs.com>. A reporter seeking a
bounty must submit by email only, with the full report and credentials required under Bug bounty
below.

GitHub private vulnerability reporting remains available: open the repository's Security tab and
choose "Report a vulnerability". Only the maintainers see a private report. Reports made through
GitHub, including private vulnerability reporting, or any other public surface (issue, discussion,
pull request, advisory or social post) are taken up by the repair programme and fixed without
attribution or reward. Do not disclose suspected vulnerabilities publicly.

Include:

- the affected commit and component
- prerequisites and an end-to-end reproduction (a World Source, turn script, adapter response or
  ledger that triggers the defect)
- the violated invariant and the plausible impact
- the privileges exploitation needs, such as write access to a World Source, a Build directory,
  an instance directory, the adapter stubs or the model gateway credentials
- any proposed fix or test case

Test only against systems and credentials you own or have written permission to test. Do not probe
the hosted model gateway or any adapter endpoint that is not your own stub, do not use credentials
you obtain during research, and do not keep secrets you encounter.

## Handling and disclosure

The maintainers confirm receipt by email or in the private advisory, assess scope and severity, and
work on the fix in a private advisory fork when one is needed. The fix lands on `main`, and when
it changes the compiler, kernel, CLI or SDK it ships in a release as described in
[RELEASE.md](RELEASE.md). The advisory is published once the fix is available, crediting reporters
who submitted by email only unless they ask otherwise. Reports through GitHub or another public
surface are fixed without attribution or reward. A report found to be out of scope is closed with
the reason given by email or in the advisory. Please keep details private until the advisory is
published.

## Response targets

These response targets are best-effort commitments. The maintainers aim to acknowledge a report
within three business days of receipt and complete triage with a scope and severity decision
within ten business days of receipt. While remediation is in progress, they provide a status update
at least every fourteen days.

Remediation targets, counted from the severity decision, are thirty days for Critical issues,
sixty days for High issues, ninety days for Medium issues and the next scheduled release for Low
issues. Coordinated disclosure is targeted within fourteen days of the fix being available.

If a target is missed, follow up by email or comment in the private advisory. After two further
business days without a response, email <security@sidioralabs.com> with the report or advisory
identifier.

## Bug bounty

This repository text is the bug bounty policy. The programme rewards vulnerabilities in the
in-scope code listed below, subject to the out-of-scope exclusions. Rewards are paid once per
unique vulnerability to the first reporter. The amount is determined by Sidiora Labs Inc. from the
affected subsystem, its role in the runtime, the severity and effect on the guarantees listed at
the top of this policy, and the risk to users who run CWR against real adapters and real model
credentials.

Bounty eligibility requires all three of the following:

1. Submission by email to <security@sidioralabs.com> only.
2. A full and clear report covering every item in the Include list under Report privately above,
   including an end-to-end reproduction.
3. Credentials demonstrating that the submitter is legitimate and competent, for example prior
   published disclosures or CVE credits, a professional or audit-firm affiliation, a verifiable
   identity, and an account of how the issue was found.

A report missing any of these three requirements is handled as an ordinary report with no reward.
Reports submitted through GitHub or another public surface are taken up by the repair programme and
fixed without attribution or reward.

Stop testing at a proof of concept. Never use credentials or endpoints that are not your own, and
follow the testing restrictions above. Keep the issue private until it is fixed and the advisory
is published.

## Secrets

Credentials are never part of the tree. They are read from the environment at runtime, and
`.gitignore` excludes every environment, key and certificate file. Nothing prints, logs or records
a credential, including test output, evidence records, campaign results and error messages. If a
secret is exposed, treat it as compromised: report it, rotate it and remove it from history.
Deleting the file in a later commit is not enough.

## Guarantees under test

The guarantees a report may claim to break are the ones the specification states. The sandbox
guarantees actually enforced on a given platform are documented in
`crates/cwr-kernel/SANDBOX.md`; a gap between that document and the specification is itself a
defect to report.

- Closure: a turn script reaches nothing but the generated SDK; no network, no filesystem outside
  its scratch directory, no wall clock.
- Visibility and predictable failure: every outcome is one of the eleven envelope codes and depends
  only on visible conditions; no hidden state changes a result.
- Immutability: a Build is content-addressed; any change to its bytes changes its identity and is
  refused by the kernel.
- Ledger integrity and replay: the ledger is append-only and hash-chained; replay from a ledger and
  recorded cassettes reproduces the same state; a tampered ledger is refused.
- Controlled change: rules change only through a human recompilation with a declared changelog, a
  version bump and a notice the model sees.
- Gateway: adapter failures are normalised into declared codes only, idempotency keys prevent
  duplicate side effects, and no retry is ever performed silently.

## Known exemptions

None. An exemption is recorded here only after an owner ruling that names the affected dependency,
the mitigation, the review date and the file that records the rationale.

## Supported versions

The supported line is `main` and the current release named in `benchmark/release.kvx`. Fixes land
on `main` and ship in the next release.

## Scope

In scope is the code in this repository, including:

- the Rust workspace: contract crate, compiler, kernel and CLI (`crates/`)
- the generated-SDK runtime used inside sandboxed scripts (`sdk/`)
- the shared Python core and repository tooling (`core/`, `tools/`, `scripts/`)
- the experiment harness, adapter stubs, cassette tooling and predicates (`benchmark/`)
- the qualification runner, evidence checker and closure (`tests/cwr/`)
- the reference world, its module packs and upgrade pairs (`examples/`, `modules/`)

Out of scope:

- hosted model services and other third-party services and integrations
- issues already known or reported
- social engineering
- defects that need write access to a Build directory, an instance directory or the process environment, unless
  the code fails to enforce a limit it claims to enforce
- denial of service by sheer traffic volume against hosted endpoints
- findings from automated scanners without a demonstrated impact
- defects in an upstream crate that do not affect how this repository uses it; report those
  upstream

A conduct concern that is not a vulnerability follows [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

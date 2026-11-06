<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# CWR engineering standard

This repository is the Compiled World Runtime (CWR), a Sidiora Labs research
system: the agent harness becomes a frozen, rule-governed world that only a
human recompiles. The normative specification is `spec/cwr/spec.kvx`; the
contract chapters next to it are binding. Read `spec/cwr/README.md` first.

Every rule below is enforced by `make check`. Code that does not pass
`make check` is not finished.

## Source header

Every file that admits a comment carries these two lines at the very top,
after a shebang if there is one, in the comment syntax of the file:

```
Copyright Sidiora Labs Inc. CWR® 2026-2027
SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
```

Rust, JavaScript and C use `//`. Python, shell, TOML, KVX, YAML, Makefile,
`.gitignore` and `.editorconfig` use `#`. Markdown and HTML use `<!-- -->`.
Mermaid uses `%%`. JSON, JSON Lines, lock files and recorded data (builds,
ledgers, evidence, cassettes, results, vendored code) are exempt.
`python3 tools/header.py --check` lists what is missing; `--apply` inserts it.

## Formatting

- Rust: `cargo fmt --all` with the repository `rustfmt.toml`; `cargo clippy
  --workspace --all-targets -- -D warnings` must be clean.
- Python: `ruff format` and `ruff check` with the repository `ruff.toml`
  (line length 100, Python 3.12, stdlib only unless a task explicitly allows a
  dependency).
- KVX: one `key = <JSON literal>` per line, `[section]` headers with one
  blank line between sections, `#` comments only above the line they describe.
- Markdown: ATX headings, one sentence per line is not required, no trailing
  whitespace, exactly one final newline, relative links that resolve.
- `.editorconfig` sets UTF-8, LF, final newline, 4-space indent (2 for YAML,
  JSON and TOML).

## Code quality

- Public items carry a doc comment that says what the item guarantees, not
  what it is named.
- Library code never panics on input: no `unwrap`, `expect`, `panic!` or
  indexing that can fail outside tests; errors are typed enums with
  locations (`file:line:col`) where the input is a file.
- Failure codes are the closed eleven-code envelope from the contract crate.
  Nothing else is constructible.
- No dead code, no commented-out code, no `TODO`, `FIXME` or `XXX`. If
  something is deferred, it is a deviation recorded in the task result and
  in `spec/cwr/qualification.kvx`, not a comment.
- Determinism is a feature: anything the spec calls deterministic is
  byte-identical across runs and machines, and a test proves it.
- Every module has tests next to it; every task has a driver under
  `tests/cwr/cases/` that exercises the real implementation, prints one line
  per acceptance clause and exits non-zero on any failure.
- Python scripts are `python3 -I` safe: no reliance on the current directory
  being on `sys.path`, no imports from the spec tooling except
  `spec/cwr/tools/validate_spec.py` through its documented functions.

## Secrets and publication

- Credentials are read from the environment at runtime, never from the tree.
  Nothing prints, logs, copies or commits a value; `.gitignore` excludes
  every environment, key and certificate file.
- Only ledgers, evidence records, analyses and reports are published under
  `benchmark/results` and `tests/cwr/evidence`; never credentials, endpoints
  or model identities beyond what the pre-registration names.

## Documentation

- Every document is written for engineers outside Sidiora. It leads with
  what the thing is and guarantees, in product terms, then how to use it.
  Implementation detail appears only where the reader needs it to act.
- Documents never mention credential files, service vendors, machine paths,
  the tools or models that produced the code, or production narrative.
- No repeated hedging. One status sentence at the top where something is
  not yet qualified; the rest states what the software does.
- Every command shown exists and runs as written; every relative link
  resolves; terminology is the spec's: World Source, World Compiler, Build,
  World Kernel, Instance, Model Surface, ledger, adapter gateway, failure
  envelope, recompile notice, proposal outbox, upgrade pair, campaign.
- Short sentences, active voice, ATX headings, tables for parallel facts,
  no em dashes, no marketing adjectives. `SECURITY.md` is the reference tone.

## Research rules

- No external baselines. CWR is never compared with, modelled on, or run
  against any other agent or harness. Every experimental arm is a CWR build
  with one pillar ablated.
- Numbers in a report equal an independent recomputation from ledgers by
  `benchmark/analyse.py`. Fewer seeds than pre-registered is a recorded
  deviation, never a PASS.

## Commits

Every completed unit of work ends with a commit and a push by the agent that
did the work. Enable the hooks once: `git config core.hooksPath .githooks`.

- Stage explicit paths (`git add <paths>`), never `git add -A`. Commit only
  the files of the unit you finished.
- Message format, enforced by `tools/commit_msg.py` as the commit-msg hook:

  ```
  [spec:cwr/2.4] Recover a killed turn and replay the ledger

  Recovery keeps every primitive the ledger committed and discards the one
  in flight; replay rebuilds state from the ledger and cassettes.

  Task: 2.4
  Cases: CWR-010
  Verify: python3 tests/cwr/cases/2_4.py (exit 0)
  Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
  ```

  Scopes: `spec:cwr/W.N` for a specification task (Task, Cases and Verify
  trailers required), `spec:cwr` for the pack, `docs`, `tooling`,
  `review:<area>`, `suite`, `campaign`, `release`, `chore`. Subject is
  imperative, capitalised, 72 characters at most, no final period. The body
  says what changed and why. An AI agent always adds its Co-Authored-By line.
- Push immediately: `git pull --rebase --autostash origin main && git push
  origin main`. When the remote has not moved, `git push origin main` alone
  is enough. On an index lock or a rejected push, wait and retry; never
  force-push.
- The pre-commit hook refuses credential files, credential-looking hunks and
  files without the source header.

## The loop

1. Read the task in `spec/cwr/spec.kvx` and the clauses it owns.
2. Implement only that task; touch only its `touches` plus minimal
   integration edits.
3. Run its driver until it exits 0, then `make check`.
4. Never hand-edit `implementation-plan.md` or `traceability.md`; run
   `python3 spec/cwr/tools/validate_spec.py --render`.

## Layout

| Directory | Holds |
|---|---|
| `spec/cwr/` | Normative pack: `spec.kvx`, contract chapters, validator, example world |
| `crates/` | Rust workspace: `cwr-contract`, `cwr-compiler`, `cwr-kernel`, `cwr-cli` |
| `sdk/python/` | Generated-SDK runtime for sandboxed scripts |
| `core/` | Shared reference implementations used by Python tooling (KVX grammar, envelope) |
| `modules/` | Reusable World Source module packs (contracts, fixtures, manual sections) |
| `examples/` | `refworld` World Source and the `refworld-upgrades` pairs |
| `benchmark/` | Task suite, predicates, stubs, cassettes, harness, campaigns, results |
| `tests/cwr/` | Qualification registry, runner, checker, drivers, evidence |
| `docs/cwr/` | User guides derived from the contract chapters |
| `research/` | Hypothesis reports H1 to H4 |
| `tools/` | Repository tooling: headers, format, codemap, release checks |
| `scripts/` | Operator scripts: bootstrap, campaigns, release |
| `artifacts/` | Release bundles produced by `make release` (contents ignored) |
| `vendor/` | `cargo vendor` output for offline, reproducible builds |

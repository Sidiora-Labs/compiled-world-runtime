<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Contributing to CWR

CWR is built from a normative specification, [spec/cwr/spec.kvx](spec/cwr/spec.kvx). Every change
implements, corrects or clarifies something the specification states, and every claim about
behaviour rests on an executed qualification case. This file covers setup, how to make a change,
the engineering standard, the review bar and the licensing of contributions. How the
specification itself changes is described in [GOVERNANCE.md](GOVERNANCE.md).

## Set up

CWR builds on Linux. Install a Rust toolchain and Python 3.12, then run:

```sh
scripts/bootstrap.sh
```

The script checks `cargo`, `rustfmt`, `clippy`, Python 3.12, `ruff` and bubblewrap (`bwrap`), and
installs `rustfmt`, `clippy` or `ruff` when one is missing. It also checks that model credentials
are configured. Only model campaigns need credentials, so a contributor without them can ignore
that one failure.

## Make a change

1. Find the task in `spec/cwr/spec.kvx` that owns the behaviour. Each `[task.W.N]` section lists
   the clauses it implements (`reqs`), the paths it may change (`touches`) and its driver
   (`verify_cmd`). Read those clauses and the contract chapter in `spec/cwr/` that refines them.
2. Change only the task's paths, plus the smallest integration edit a consumer needs.
3. Run the task's driver, `python3 tests/cwr/cases/W_N.py`, until it exits 0. Then run
   `make check`.
4. If you changed the specification, regenerate its derived views. Never edit
   `spec/cwr/implementation-plan.md` or `spec/cwr/traceability.md` by hand.

   ```sh
   python3 spec/cwr/tools/validate_spec.py --render
   ```

5. Add one line under `## [Unreleased]` in [CHANGELOG.md](CHANGELOG.md) naming the task and the
   paths the change created.

Work that is deferred is recorded as a deviation in `spec/cwr/qualification.kvx`. It is never a
code comment.

## Make targets

| Target | Effect |
|---|---|
| `make build` | Builds the Rust workspace in release mode |
| `make test` | Runs the cargo tests, the Python unit tests and the adapter stub self-test |
| `make fmt` | Rewrites Rust and Python sources into the repository format |
| `make fmt-check` | Fails when a Rust or Python source is not formatted |
| `make lint` | Runs `cargo clippy` with warnings as errors, and `ruff check` |
| `make headers` | Fails when a file lacks the source header |
| `make headers-apply` | Inserts the source header where it is missing |
| `make validate` | Validates the specification |
| `make check` | Runs `headers`, `fmt-check`, `lint` and `validate`. This is the gate before merge. |
| `make qualify` | Runs every qualification case, CWR-001 to CWR-020, through the runner |
| `make closure` | Assembles the evidence for every case and prints the release verdict |
| `make vendor` | Copies the source of every locked crate into `vendor/cargo` |
| `make release` | Runs `check`, `test` and `qualify`, then writes a release bundle into `artifacts/` |
| `make clean` | Removes build outputs |

Cargo allows one build per target directory at a time. To run a second build alongside the first,
give it its own directory: `make TARGET_DIR=target-alt <target>`.

## Engineering standard

`make check` enforces the formatting and header rules below. Reviewers enforce the rest.

### Source header

Every file that admits a comment starts with these two lines, after a shebang if there is one, in
the comment syntax of the file:

```text
Copyright Sidiora Labs Inc. CWR® 2026-2027
SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
```

Rust uses `//`. Python, shell, TOML, KVX, YAML and the Makefile use `#`. Markdown uses `<!-- -->`
and Mermaid uses `%%`. JSON, lock files and recorded data such as Builds, ledgers, evidence and
cassettes are exempt. Insert the header into a new file with:

```sh
python3 tools/header.py --apply <file>
```

### Formatting

| Language | Rule |
|---|---|
| Rust | `cargo fmt` with the repository `rustfmt.toml`; `cargo clippy` clean with warnings as errors |
| Python | Python 3.12, standard library only unless the task allows a dependency; `ruff format` and `ruff check` with the repository `ruff.toml` |
| KVX | One `key = <JSON literal>` per line, `[section]` headers, comments on their own line above the key |
| Markdown | ATX headings, no trailing whitespace, one final newline, relative links that resolve |

### Code

- A public item carries a doc comment that states what it guarantees.
- Library code never panics on input. Outside tests there is no `unwrap`, `expect`, `panic!` or
  indexing that can fail. Errors are typed, and an error about a file carries `file:line:col`.
- Failure codes come only from the eleven-code failure envelope in `crates/cwr-contract`.
- Anything the specification calls deterministic is byte-identical across runs and machines, and
  a test proves it.
- There is no dead code, no commented-out code and no `TODO`, `FIXME` or `XXX`.
- Python scripts run under `python3 -I` and do not depend on the current directory.

## Drivers

Each task has a driver at `tests/cwr/cases/W_N.py`; task 2.3 is `2_3.py`. A driver:

- exercises the real implementation: built binaries, real Builds and real Instances, never a mock
  of the component under test;
- uses Python 3.12 and the standard library only, and runs from the repository root;
- prints one line per acceptance clause it owns, with the clause id, `PASS` or `FAIL` and what it
  observed, and exits non-zero when any clause fails;
- under the qualification runner, follows the `ASSERT` and `BLOCKED` protocol documented at the
  top of `tests/cwr/qualify.py` and writes artifacts only into `$CWR_EVIDENCE_DIR`;
- honours `CARGO_TARGET_DIR`;
- reports a missing resource, such as model credentials or the sandbox binary, as `BLOCKED`, never
  as `PASS`.

A driver's exit code is not evidence. A case outcome comes only from an attempt record that the
independent checker, `tests/cwr/check_evidence.py`, has accepted. Never edit `tests/cwr/evidence/`
or a case status by hand.

## Review bar

A change is ready for review when every item below holds.

- `make check` passes and the task driver exits 0 on a clean tree.
- The change implements exactly the task's clauses. Anything else is a separate change.
- Every new file carries the source header.
- The change follows the engineering standard above.
- No credential, private endpoint or key appears in code, logs, evidence or results. Credentials
  are read from the environment and nothing prints or records one.
- CWR is not compared with, modelled on or run against any other system. Every experimental arm
  is a CWR Build with one pillar removed.
- Documentation states what the software does. A behaviour that is not yet qualified is named as
  such once, in the document's status line.

## Commit and push

Every completed unit of work ends with a commit and a push. Enable the repository hooks once:

```sh
git config core.hooksPath .githooks
```

The pre-commit hook refuses credential files, hunks that look like a credential value and files
without the source header. The commit-msg hook enforces the message format:

```
[spec:cwr/2.4] Recover a killed turn and replay the ledger

Recovery keeps every primitive the ledger committed and discards the one
in flight; replay rebuilds state from the ledger and cassettes.

Task: 2.4
Cases: CWR-010
Verify: python3 tests/cwr/cases/2_4.py (exit 0)
```

| Rule | Detail |
|---|---|
| Scope | `spec:cwr/W.N` for a specification task; `spec:cwr` for the pack; `docs`, `tooling`, `review:<area>`, `suite`, `campaign`, `release`, `chore` |
| Subject | Imperative, capitalised, 72 characters at most, no final period |
| Body | What changed and why, wrapped at 72 columns |
| Trailers | `Task`, `Cases` and `Verify` are required for a `spec:cwr/W.N` scope; a change written by an automated assistant adds `Co-Authored-By` |

Stage explicit paths with `git add <paths>` and commit only the files of the unit you finished.
Then push:

```sh
git pull --rebase --autostash origin main && git push origin main
```

On an index lock or a rejected push, wait and retry. Never force-push.
`python3 tools/commit_msg.py --example` prints a valid message and
`python3 tools/commit_msg.py <file>` checks one.

## Licensing of contributions

CWR is licensed under Sidiora DEV OSS-1.0 (`LicenseRef-Sidiora-DEV-OSS-1.0`). The full text is in
[LICENSE](LICENSE). By submitting a contribution you offer it under the same license. The
development permissions apply to it now. The automatic, irrevocable MIT conversion in section 5 of
the license applies to it on the Conversion Date, whether or not the contribution is merged.
Section 2 permits paid work done to prepare and submit a contribution. Keep every file's source
header, and do not add terms that narrow or widen the license.

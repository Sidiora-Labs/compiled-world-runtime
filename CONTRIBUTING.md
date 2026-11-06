<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Contributing to CWR

The Compiled World Runtime is built task by task from a normative specification. The engineering
standard is [CLAUDE.md](CLAUDE.md) (mirrored in [AGENTS.md](AGENTS.md)); this file is the working
procedure. How the specification itself changes is in [GOVERNANCE.md](GOVERNANCE.md).

## Setup

```sh
scripts/bootstrap.sh
```

It installs `rustfmt`, `clippy` and `ruff`, checks for Python 3.12 and cargo, and checks that
credentials are configured without printing them. Code reads credentials from the environment at
runtime; never print, log, copy or commit a value.

## The loop

1. Read the task in [spec/cwr/spec.kvx](spec/cwr/spec.kvx) (`[task.W.N]`) and every clause listed in
   its `reqs`. The contract chapters next to the spec are binding.
2. Implement only that task. Touch only the paths in its `touches`, plus the minimal integration
   edits a consumer needs.
3. Run the task's driver (its `verify_cmd`, `python3 tests/cwr/cases/W_N.py`) until it exits 0,
   then run `make check`.
4. Never hand-edit `spec/cwr/implementation-plan.md` or `spec/cwr/traceability.md`. Regenerate them:

   ```sh
   python3 spec/cwr/tools/validate_spec.py --render
   ```

5. Add one line under `## [Unreleased]` in [CHANGELOG.md](CHANGELOG.md) naming the task id and the
   paths it created.

Anything deferred is a deviation recorded in the task result and in
`spec/cwr/qualification.kvx`, never a `TODO` comment.

## Make targets

| Target | Does |
|---|---|
| `make build` | `cargo build --workspace --release` |
| `make test` | cargo tests, harness unit tests, stub self-test |
| `make fmt` / `make fmt-check` | rewrite / verify Rust (`cargo fmt`) and Python (`ruff format`) |
| `make lint` | `cargo clippy --workspace --all-targets -- -D warnings` and `ruff check` |
| `make headers` / `make headers-apply` | verify / insert the source header (`tools/header.py`) |
| `make validate` | structural validation of the specification pack |
| `make check` | `headers`, `fmt-check`, `lint`, `validate`: the pre-merge gate |
| `make qualify` | every case CWR-001 to CWR-020 through `tests/cwr/qualify.py` |
| `make closure` | assemble evidence for all cases and print the release verdict |
| `make vendor` | `cargo vendor` into `vendor/cargo` |
| `make release` | `check`, `test`, `qualify`, then `tools/release.py` |

`CODEMAP.md` lists the same targets, generated from the Makefile by `python3 tools/codemap.py`.
Cargo serialises on its target directory; when two builds run at once, give one its own
directory with `make TARGET_DIR=target-alt <target>` instead of waiting on the lock.

## Drivers

Every task has a driver at `tests/cwr/cases/W_N.py` (task 2.3 is `2_3.py`). A driver:

- exercises the real implementation (built binaries, real Builds, real Instances), never a
  re-implementation or a mock of the component under test;
- is Python 3.12, stdlib only, and runs from the repository root under `python3 -I`;
- prints one line per acceptance clause it owns, with the clause id, `PASS` or `FAIL`, and the
  evidence it observed, and exits non-zero if any clause fails;
- under the qualification runner, emits the `ASSERT` / `BLOCKED` protocol documented at the top of
  `tests/cwr/qualify.py` and writes its artifacts only into `$CWR_EVIDENCE_DIR`;
- honours `CARGO_TARGET_DIR`;
- reports a missing resource (a model credential, a sandbox binary) as `BLOCKED`, never as `PASS`.

A driver's exit code is not evidence. A case outcome comes only from an attempt record that the
independent checker (`tests/cwr/check_evidence.py`) has accepted; never edit
`tests/cwr/evidence/` or a case status by hand.

## Review bar

A change is ready for review when all of the following hold. Reviewers check each one.

- `make check` passes and the task driver exits 0 on a clean tree.
- The change implements exactly the task's clauses; anything else is a separate change.
- Every new file carries the source header; every public item has a doc comment stating what it
  guarantees.
- Library code does not panic on input: no `unwrap`, `expect`, `panic!` or fallible indexing
  outside tests. Errors are typed and located (`file:line:col`) when the input is a file.
- Failure codes come only from the closed eleven-code envelope.
- Anything the spec calls deterministic is byte-identical across runs, and a test proves it.
- No dead or commented-out code, no `TODO`, `FIXME` or `XXX`.
- No secret, endpoint or key appears in code, logs, evidence or results.
- No comparison with, or dependency on, any other agent or harness. Every experimental arm is a
  CWR Build with one pillar ablated.
- Documentation states what is implemented with a link to its evidence. A document whose subject
  is not yet qualified says so once, in a status sentence at the top.

## Licensing of contributions

CWR is licensed under Sidiora DEV OSS-1.0 (identifier `LicenseRef-Sidiora-DEV-OSS-1.0`), the
full text of which is in [LICENSE](LICENSE). By submitting a contribution to the Project you
offer it under that same license: the development permissions apply to it now, and the automatic,
irrevocable MIT conversion in section 5 of the license applies to it on the Conversion Date,
whether or not the contribution is incorporated upstream. Work done to prepare and submit a
contribution may be paid work; see section 2 of the license. Keep every file's source header and
do not add terms that narrow or widen the license for the covered Software.

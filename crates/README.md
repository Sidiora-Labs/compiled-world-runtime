<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Rust workspace: World Compiler, World Kernel and the cwr CLI

The four crates here are the CWR runtime. The World Compiler turns a World Source into an immutable,
content-addressed Build. The World Kernel runs Instances of a Build: it executes each turn script in
a sandbox, enforces every contract atomically, mediates every adapter call and records everything in
a hash-chained ledger. The `cwr` binary exposes both.

| Crate | Role | What it guarantees |
|---|---|---|
| `cwr-contract` | Strict KVX reader, typed World Source model, failure envelope, precondition grammar | Only the eleven envelope codes can be constructed. Every parse error carries a `file:line:col` location. |
| `cwr-compiler` | The World Compiler: parse, resolve, lint, contract tests, diff, changelog gate, generate, package | Stages run in a fixed order and the first failing stage stops the run with nothing written. Diagnostics and Build bytes are deterministic. A changed rule passes only with a version bump and a changelog entry. |
| `cwr-kernel` | The World Kernel: Instances, sandboxed turns, atomic primitives, adapter gateway, ledger, snapshots, recovery, replay and upgrade | A script reaches only the generated SDK. A Build whose bytes do not match its hash is refused. Replay from a ledger reproduces the same state, and a tampered ledger is refused. |
| `cwr-cli` | The `cwr` binary, one module per subcommand | Every subcommand has documented exit codes |

## Requirements

- A Rust toolchain with cargo
- Linux with bubblewrap (`bwrap`) and Python 3.12 for running turns. Without them the World Kernel
  refuses to run a turn and never runs a script unsandboxed.

`scripts/bootstrap.sh` checks all of these.

## Build and test

```sh
cargo build --workspace --release
cargo test --workspace
make check
```

The binary is `target/release/cwr`. `make check` runs the header check, formatting, clippy with
warnings as errors, ruff and the specification validator.

## First run

From the repository root, with `target/release` on `PATH`:

```sh
cwr build examples/refworld --out build
cwr instance create <hash> --seed 7
cwr instance turn <id> < turn.py
cwr instance tail <id>
cwr instance replay <id>
```

`cwr build` prints `built sha256:<hash> at build/<hash>`. `cwr instance create` prints the new
Instance id. With this `turn.py`:

```python
import fs

fs.write(path="/notes/hello.md", content=b"hello")
return fs.read(path="/notes/hello.md")
```

the turn prints one line of canonical JSON with `"status":"ok"`, the returned bytes, a trace of both
calls and the world coordinates (version, Build hash, world time and turn). `cwr instance tail`
prints the ledger tail the model sees. `cwr instance replay` re-runs every turn in a fresh Instance
and reports `"identical":true`.

Instances live under `--home` (default `$CWR_HOME`, else `.cwr`). A Build hash resolves in the
`--builds` roots (default `build` and `xbuild`).

## Commands

| Command | Purpose |
|---|---|
| `cwr lint <source> [--json]` | Parse, resolve and lint a World Source |
| `cwr build <source> [--out <dir>] [--previous <hash>] [--json]` | Compile a World Source into a Build. Without `--previous`, the Build is diffed against the newest Build of the same world under `--out`. |
| `cwr build <source> --x-drift <file> \| --x-silent-change \| --x-version-only \| --x-one-primitive` | Compile an experimental Build with one pillar ablated, written under `xbuild/` |
| `cwr diff <a> <b>` | Print the ContractDiff between two Builds or World Sources |
| `cwr manual <hash\|dir>` | Print a Build's manual after verifying its hash |
| `cwr contract check\|dump\|emit\|codes\|envelope\|expr` | Inspect contracts, failure codes, envelopes and precondition expressions |
| `cwr instance create <hash\|dir> [--seed N] [--tail K\|full] [--initial <state.json>]` | Create an Instance and print its id |
| `cwr instance turn <id> [--script <file> \| --message <file> \| < script.py]` | Run one sandboxed turn and print the result envelope |
| `cwr instance turn <id> --gateway live\|record\|replay [--cassette <file>] [--stub NAME=URL]` | Run a turn with the adapter gateway in the given mode |
| `cwr instance context <id>` | Print the composed context for the next turn |
| `cwr instance show\|state\|ledger\|tail <id>` | Print metadata, world state, the ledger check or the model-facing ledger tail |
| `cwr instance snapshot <id>`, `cwr instance restore <id> <snapshot>` | Take and restore content-addressed snapshots |
| `cwr instance upgrade <id> <hash\|dir>` | Upgrade an Instance to a new Build, running migrations and storing a recompile notice |
| `cwr instance notice <id>`, `cwr instance outbox <id>` | Print the pending recompile notice or the proposal outbox |
| `cwr instance replay <id\|ledger.jsonl>` | Re-run an Instance from its seed, initial state, Build, cassette and recorded scripts, and compare ledgers |
| `cwr instance recover <id>`, `cwr instance compare <a> <b>` | Recover after a crash, or compare two ledgers |
| `cwr instance destroy <id>` | Seal and retain the ledger and release the state |
| `cwr release pin\|check` | Pin or verify release inputs |

## Exit codes

| Command | Codes |
|---|---|
| `cwr build` | 0 success, 2 parse or resolve, 3 lint, 4 contract tests, 5 diff, 6 changelog gate, 7 package, 1 usage or other error |
| `cwr instance turn` | 0 for a turn that ended `ok`, `error` or `partial`, 1 usage or kernel error, 2 Build hash mismatch, 3 experimental Build, 4 message refused by the turn protocol |
| `cwr instance upgrade` | 0 success, 5 Build refused with nothing changed, 6 migration or commit failure after rollback |
| `cwr instance replay`, `cwr instance compare` | 0 identical, 7 divergence, 2 Build or initial-state hash mismatch (replay), 1 refused input |

The full operator guide is [docs/cwr/kernel.md](../docs/cwr/kernel.md). The World Source format is in
[docs/cwr/world-source.md](../docs/cwr/world-source.md). The sandbox is documented in
[cwr-kernel/SANDBOX.md](cwr-kernel/SANDBOX.md).

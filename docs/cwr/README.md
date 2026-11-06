<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# CWR user guides

> **Implemented, not yet qualified (CWR-020).** CWR has no release and has not yet passed qualification. The guides describe the shipped [World Compiler](../../crates/cwr-compiler/src/lib.rs), [World Kernel](../../crates/cwr-kernel/src/lib.rs), [Model Surface](../../crates/cwr-kernel/src/surface/context.rs) and [experiment harness](../../benchmark/harness/run.py).

The Compiled World Runtime (CWR) is the runtime in which an AI model acts by running scripts inside
a compiled, rule-governed world. A human writes the rules as World Source. The World Compiler turns
the source into an immutable Build. The World Kernel runs Instances of that Build, executes one
sandboxed script per turn, records every action in a ledger and mediates every call to an outside
service. The rules change only when a human recompiles.

## Guides

| Guide | Read it to |
|---|---|
| [world-source.md](world-source.md) | Write a World Source, build it, read the diagnostics and ship a rule change |
| [kernel.md](kernel.md) | Create Instances, run turns, read the ledger, call outside services, replay, snapshot and upgrade |
| [experiments.md](experiments.md) | Build experimental arms, record cassettes, run cells and campaigns and analyse the results |

Each guide is a walk-through. Its `sh` blocks run in order in one shell, starting from the root of
a checkout or an unpacked bundle, and work in a scratch directory. `text` blocks show output excerpts and
commands that fail on purpose.

Install the `cwr` binary first, as described in [Install](../../README.md#install).

## Terms

| Term | Meaning |
|---|---|
| World Source | The directory of rules a human writes: world settings, contracts, library code, fixtures, manual prose, changelog |
| World Compiler | `cwr build`: checks a World Source and packages a Build, or refuses it |
| Build | An immutable, content-addressed directory holding the manual, the SDK and the enforcement table |
| World Kernel | The runtime that executes Instances of a Build |
| Instance | One running world: state, ledger, clock, random stream and snapshots, bound to one Build |
| Model Surface | What the model receives each turn: the manual, a recompile notice, the ledger tail and the last result envelope |
| ledger | The append-only, hash-chained record of everything an Instance did |
| adapter | A declared call to a service outside the world |
| adapter gateway | The kernel component that performs adapter calls in live, record or replay mode |
| failure envelope | The closed set of eleven failure codes and the shape every refusal takes |
| recompile notice | What the model reads on its first turn after an upgrade |
| proposal outbox | Where the model writes to the human owner. It changes nothing in the world. |
| upgrade pair | Two World Sources that differ in one rule, used to test upgrades |
| campaign | The full run of one hypothesis: every arm, task and seed |

## The specification

The guides describe the software. The normative contract is the specification in
[spec/cwr](../../spec/cwr/README.md). Where a guide and the specification disagree, the
specification wins.

| Chapter | Covers |
|---|---|
| [world-source.md](../../spec/cwr/world-source.md) | The World Source grammar |
| [compiler.md](../../spec/cwr/compiler.md) | The compiler pipeline and command line |
| [kernel.md](../../spec/cwr/kernel.md) | Instances, turns, the sandbox, the gateway and upgrades |
| [model-surface.md](../../spec/cwr/model-surface.md) | The context, turn protocol and result envelope |
| [experiments.md](../../spec/cwr/experiments.md) | Hypotheses, arms, metrics and analysis |
| [verification.md](../../spec/cwr/verification.md) | Qualification cases and evidence |

Security reports follow [SECURITY.md](../../SECURITY.md).

<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Writing a World Source

> **Implemented, not yet qualified (CWR-002, CWR-003, CWR-004, CWR-005).** CWR has not yet passed qualification. This guide describes what the current [World Compiler](../../crates/cwr-compiler/src/lib.rs) and [`cwr` binary](../../crates/cwr-cli/src/main.rs) do.

A World Source is the directory of rules a human writes for a CWR world. The World Compiler turns
it into a Build: an immutable, content-addressed directory that holds the manual the model reads,
the SDK its scripts import and the table the World Kernel enforces. All three come from one
compiled contract, so the rule the model reads is the rule the kernel applies.

The compiler refuses a source rather than guess. There are no defaults, no warnings and no
undeclared failure codes. Every rule must be reachable by a test fixture, and every rule change
must ship with a version bump and a changelog entry written by a human.

This guide builds a small world from nothing, changes one of its rules, and then documents every
file and check in reference form.

## Before you start

Install the `cwr` binary as described in [Install](../../README.md#install). CWR runs on Linux and
needs Python 3.12 as `python3.12` and bubblewrap as `bwrap`.

The commands in this guide run in one shell, in an empty scratch directory. Hashes in the output
excerpts are shortened.

```sh
cd "$(mktemp -d)"
mkdir -p tally/contracts tally/lib tally/fixtures/counter tally/manual
```

## Tutorial: a world of counters

The world `tally` has one module, `counter`, with four primitives: `counter.exists`,
`counter.value`, `counter.create` and `counter.add`.

### Declare the world

`world.kvx` names the world, its version, its modules and every limit. An omitted key is an error.
The compiler never substitutes a value you did not write.

```sh
cat > tally/world.kvx <<'EOF'
[meta]
format = "cwr.world-source"
schema_version = 1

[world]
name = "tally"
version = "0.1.0"
language = "python"
runtime = "3.12"
modules = ["counter"]

[limits]
cpu_ms = 2000
wall_ms = 10000
memory_mb = 128
primitive_calls = 100
adapter_calls = 1

[clock]
tick_per_primitive = 1
tick_per_turn = 10

[rng]
seed_visible = false
EOF
```

### Declare the primitives

A contract file declares one module. Each primitive states its side-effect class, typed
parameters, return type, ordered preconditions with the failure code each one produces, the
effects that hold after success, and one or two sentences of documentation.

```sh
cat > tally/contracts/counter.kvx <<'EOF'
[meta]
format = "cwr.contract"
schema_version = 1
module = "counter"

[primitive.counter.exists]
side_effect = "read_only"
params = {"name": "str"}
returns = "bool"
preconditions = []
effects = []
doc = "True when a counter with this name exists. Never fails."

[primitive.counter.value]
side_effect = "read_only"
params = {"name": "str"}
returns = "int"
preconditions = [["counter.exists(name)", "NOT_FOUND"]]
effects = []
doc = "The current value of the counter. Refused when the counter does not exist."

[primitive.counter.create]
side_effect = "mutating"
params = {"name": "str"}
returns = "None"
preconditions = [["len(name) > 0", "INVALID"], ["not counter.exists(name)", "CONFLICT"]]
effects = ["counter.exists(name)", "counter.value(name) == 0"]
doc = "Create a counter at zero. Refused when the name is empty or already taken."

[primitive.counter.add]
side_effect = "mutating"
params = {"name": "str", "n": "int"}
returns = "int"
preconditions = [["counter.exists(name)", "NOT_FOUND"], ["n > 0", "INVALID"], ["counter.value(name) + n <= 1000", "LIMIT"]]
effects = ["counter.exists(name)"]
doc = "Add n to the counter and return the new value. Refused when the counter does not exist, n is not positive, or the result would exceed 1000."
EOF
```

Every precondition calls only `read_only` primitives and the primitive's own parameters. That is
what makes a rule visible: a model can evaluate it before acting.

### Implement them

`lib/counter.py` runs inside the kernel, never in the script sandbox. Each public function
implements one contract and takes the kernel state handle followed by the declared parameters. It
does not check preconditions: the kernel has already done so. The `TypedDict` declares the
module's state shape, which the compiler records and the kernel checks on upgrade.

```sh
cat > tally/lib/counter.py <<'EOF'
"""Kernel-side implementation of the tally counter module."""

from typing import Any, TypedDict


class CounterState(TypedDict):
    values: dict[str, int]


INITIAL_STATE: CounterState = {"values": {}}


def exists(state: Any, name: str) -> bool:
    return name in state.module("counter")["values"]


def value(state: Any, name: str) -> int:
    return state.module("counter")["values"][name]


def create(state: Any, name: str) -> None:
    state.module("counter")["values"][name] = 0
    return None


def add(state: Any, name: str, n: int) -> int:
    values = state.module("counter")["values"]
    values[name] += n
    return values[name]
EOF
```

### Prove every rule with fixtures

The compiler runs contract tests at build time. For each primitive, write one case per
precondition in which exactly that precondition fails and every earlier one holds, plus at least
one case in which the call succeeds. A declared failure that no fixture reaches fails the build.

```sh
cat > tally/fixtures/counter/exists.kvx <<'EOF'
[meta]
format = "cwr.fixture"
schema_version = 1
module = "counter"
primitive = "counter.exists"

[case.ok]
world_time = 0
state = {"counter": {"values": {"a": 1}}}
params = {"name": "a"}
expect = "ok"
precondition = null
returned = true
EOF
cat > tally/fixtures/counter/value.kvx <<'EOF'
[meta]
format = "cwr.fixture"
schema_version = 1
module = "counter"
primitive = "counter.value"

[case.pre_1_not_found]
world_time = 0
state = {"counter": {"values": {}}}
params = {"name": "a"}
expect = "NOT_FOUND"
precondition = 1

[case.ok]
world_time = 0
state = {"counter": {"values": {"a": 4}}}
params = {"name": "a"}
expect = "ok"
precondition = null
returned = 4
EOF
cat > tally/fixtures/counter/create.kvx <<'EOF'
[meta]
format = "cwr.fixture"
schema_version = 1
module = "counter"
primitive = "counter.create"

[case.pre_1_invalid]
world_time = 0
state = {"counter": {"values": {}}}
params = {"name": ""}
expect = "INVALID"
precondition = 1

[case.pre_2_conflict]
world_time = 0
state = {"counter": {"values": {"a": 1}}}
params = {"name": "a"}
expect = "CONFLICT"
precondition = 2

[case.ok]
world_time = 0
state = {"counter": {"values": {}}}
params = {"name": "a"}
expect = "ok"
precondition = null
EOF
cat > tally/fixtures/counter/add.kvx <<'EOF'
[meta]
format = "cwr.fixture"
schema_version = 1
module = "counter"
primitive = "counter.add"

[case.pre_1_not_found]
world_time = 0
state = {"counter": {"values": {}}}
params = {"name": "a", "n": 1}
expect = "NOT_FOUND"
precondition = 1

[case.pre_2_invalid]
world_time = 0
state = {"counter": {"values": {"a": 0}}}
params = {"name": "a", "n": 0}
expect = "INVALID"
precondition = 2

[case.pre_3_limit]
world_time = 0
state = {"counter": {"values": {"a": 999}}}
params = {"name": "a", "n": 2}
expect = "LIMIT"
precondition = 3

[case.ok]
world_time = 0
state = {"counter": {"values": {"a": 1}}}
params = {"name": "a", "n": 2}
expect = "ok"
precondition = null
returned = 3
EOF
```

### Write the changelog and the manual prose

`CHANGELOG.kvx` holds one section per release. The first release has no changes to list.

```sh
cat > tally/CHANGELOG.kvx <<'EOF'
[meta]
format = "cwr.changelog"
schema_version = 1

[release.0.1.0]
version = "0.1.0"
date = "2026-11-06"
summary = "First release: named counters that can be created, read and incremented."
changes = []
EOF
```

The four prose files under `manual/` are merged into the generated manual. They may be empty.

```sh
printf 'tally is a world of named counters.\n' > tally/manual/intro.md
touch tally/manual/conventions.md tally/manual/types.md tally/manual/examples.md
```

### Check and build

`cwr lint` runs the parse, resolve and lint stages without writing anything.

```sh
cwr lint tally
```

```text
stages: parse=passed resolve=passed lint=passed contract_tests=not_run diff=not_run changelog_gate=not_run generate=not_run package=not_run
ok: stages parse through lint passed
```

`cwr build` runs all eight stages and writes the Build under `build/`, named by its content hash.

```sh
cwr build tally
V1=$(ls build)
```

```text
stages: parse=passed resolve=passed lint=passed contract_tests=passed diff=passed changelog_gate=passed generate=passed package=passed
built sha256:0d11ea05... at build/0d11ea05...
```

Building an unchanged source again prints `already built` and writes nothing.

### Read what the model reads

`cwr manual` verifies the Build's hash and prints its manual. Each primitive entry is rendered from
the contract you wrote.

```sh
cwr manual "$V1"
```

```text
#### counter.add

counter.add(name: str, n: int) -> int                           [mutating]
  Add n to the counter and return the new value. Refused when the counter does not exist, n is not positive, or the result would exceed 1000.
  Requires   1. counter.exists(name)             else NOT_FOUND
             2. n > 0                            else INVALID
             3. counter.value(name) + n <= 1000  else LIMIT
  Ensures    counter.exists(name)
```

### Try a turn

Create an Instance of the Build and run one script. The third call breaks rule 3 of
`counter.add`, so the kernel refuses it with `LIMIT` and names the precondition. The two earlier
calls stand, so the turn ends `partial`. [kernel.md](kernel.md) explains Instances and turns in
full.

```sh
ID=$(cwr instance create "$V1")
cwr instance turn "$ID" <<'SCRIPT'
import counter

counter.create(name="visits")
counter.add(name="visits", n=3)
return counter.add(name="visits", n=1000)
SCRIPT
```

```text
{"error":{"code":"LIMIT","detail":"precondition 3 `counter.value(name) + n <= 1000` was false","limit":null,"precondition_index":3,"primitive":"counter.add","retry_after":null},...,"status":"partial",...}
```

## Changing a rule

A rule changes only when a human edits the source and recompiles. Lower the cap of `counter.add`
from 1000 to 500, in the contract and in the fixture that reaches it:

```sh
sed -i 's/+ n <= 1000/+ n <= 500/; s/would exceed 1000/would exceed 500/' tally/contracts/counter.kvx
sed -i 's/"a": 999}/"a": 499}/' tally/fixtures/counter/add.kvx
```

The compiler compares the source with the previous Build of the same world, derives the change and
refuses it until the changelog covers it and the version is high enough. The build stops at the
changelog gate with exit code 6:

```text
$ cwr build tally
CHANGELOG.kvx:5:1: changelog_gate: uncovered_entry [release.0.1.0]: ContractDiff doc entry doc_only counter.add (doc) (bump patch) is not covered by any change in release 0.1.0: add a doc_only change with target counter.add
CHANGELOG.kvx:5:1: changelog_gate: uncovered_entry [release.0.1.0]: ContractDiff entry precondition_changed counter.add (preconditions) (bump major) is not covered by any change in release 0.1.0: add a change with kind precondition_changed and target counter.add
world.kvx:7:11: changelog_gate: version_not_greater [world] version: 0.1.0 -> 0.1.0: declared bump none, derived minimum bump major; refused: world.version must be numerically greater than the previous Build's version 0.1.0 (see CHANGELOG.kvx:5:1)
stages: parse=passed resolve=passed lint=passed contract_tests=passed diff=passed changelog_gate=failed generate=not_run package=not_run
failed at stage changelog_gate: 3 diagnostics; nothing written
```

Bump the version and add a release that names both changes. The `summary` is the first line the
model reads in the recompile notice, so write it for the model.

```sh
sed -i 's/^version = "0.1.0"/version = "0.2.0"/' tally/world.kvx
cat > tally/CHANGELOG.kvx <<'EOF'
[meta]
format = "cwr.changelog"
schema_version = 1

[release.0.2.0]
version = "0.2.0"
date = "2026-11-06"
summary = "counter.add now refuses a result above 500 instead of 1000."
changes = ["add_cap_500", "add_doc"]

[change.add_cap_500]
kind = "precondition_changed"
target = "counter.add"
before = "counter.value(name) + n <= 1000, otherwise LIMIT."
after = "counter.value(name) + n <= 500, otherwise LIMIT."
migration = ""

[change.add_doc]
kind = "doc_only"
target = "counter.add"
before = "The doc named a cap of 1000."
after = "The doc names a cap of 500."
migration = ""

[release.0.1.0]
version = "0.1.0"
date = "2026-11-06"
summary = "First release: named counters that can be created, read and incremented."
changes = []
EOF
cwr build tally
V2=$(ls build | grep -v "$V1")
```

`cwr diff` prints the ContractDiff between two Builds as JSON:

```sh
cwr diff "$V1" "$V2"
```

```text
  "declared_bump": "minor",
  "derived_bump": "major",
  ...
      "bump": "major",
      "field": "preconditions",
      "kind": "precondition_changed",
      "target": "counter.add"
  ...
  "pre_1_0_rule_applied": true,
  "version_rule": "0.1.0 -> 0.2.0: declared bump minor, derived minimum bump major; accepted by the pre-1.0 rule ..."
```

The running Instance is still bound to 0.1.0. A source edit never reaches it. The operator moves
it explicitly, and the model sees the recompile notice on its next turn:

```sh
cwr instance upgrade "$ID" "$V2"
cwr instance notice "$ID"
```

```text
WORLD RECOMPILED  0.1.0 -> 0.2.0   (sha256:0d11ea05... -> sha256:c96e79d4...)
Summary: counter.add now refuses a result above 500 instead of 1000.

Changes affecting primitives this instance has used:
  changed  counter.add
    before  counter.value(name) + n <= 1000, otherwise LIMIT.
    after   counter.value(name) + n <= 500, otherwise LIMIT.
  changed  counter.add (documentation)
    before  The doc named a cap of 1000.
    after   The doc names a cap of 500.

Your ledger is intact. The manual above describes 0.2.0.
```

## Reading diagnostics

Every diagnostic names a file, line and column, the stage, a code where one applies, and the
section and key. This is what `cwr lint` reports for a `lib/` file that imports `time` and a
precondition that calls a mutating primitive (exit code 3):

```text
contracts/counter.kvx:34:80: lint: CWR-V001 [primitive.counter.add] preconditions #3: counter.add: precondition 3 "counter.create(name)" calls counter.create, whose side_effect is mutating; a rule that can only be checked by acting is not visible
lib/counter.py:3:8: lint: CWR-L001: time: import of host facility time is forbidden; use the kernel state API (cwr_state) instead
stages: parse=passed resolve=passed lint=failed contract_tests=not_run diff=not_run changelog_gate=not_run generate=not_run package=not_run
failed at stage lint: 2 diagnostics; nothing written
```

`--json` prints the same report as JSON, with one object per diagnostic. The first failing stage
stops the run. That stage reports all of its diagnostics, sorted by file, line, column and code.

## Reference

The normative grammar is [spec/cwr/world-source.md](../../spec/cwr/world-source.md). The reference
world [examples/refworld](../../examples/refworld/world.kvx) uses every feature below.

### Source layout

| Path | Contents | Required |
|---|---|---|
| `world.kvx` | Identity, version, modules, limits, clock and RNG visibility | Yes |
| `CHANGELOG.kvx` | One release section per version and one change section per rule change | Yes |
| `contracts/<module>.kvx` | Primitive contracts of one module | Yes |
| `lib/<module>.py` | Kernel-side implementation of each primitive | Yes |
| `fixtures/<module>/*.kvx` | Contract-test cases for every declared failure | Yes |
| `manual/intro.md`, `conventions.md`, `types.md`, `examples.md` | Prose merged into the generated manual | Yes |
| `adapters/<module>.kvx`, `adapters/<module>/live.py` | Adapter contracts and their gateway-side mapping | When the world calls outside services |
| `migrations/<name>.py` | Pure state functions between versions, named `<from>_<to>_<name>.py` by convention | When a state shape changes |

Any other file, section or key is an error (CWR-C001). Keep a README outside the source directory.

Every file except Python and manual prose is KVX: `[section]` headers, one `key = <JSON literal>`
per line and `#` comments.

### world.kvx

| Key | Rule |
|---|---|
| `meta.format`, `meta.schema_version` | `"cwr.world-source"` and `1` |
| `world.name` | Lowercase identifier. An Instance upgrades only between Builds of the same name. |
| `world.version` | `MAJOR.MINOR.PATCH`. Equal to the newest release in `CHANGELOG.kvx`. |
| `world.language`, `world.runtime` | `"python"` and `"3.12"` |
| `world.modules` | Every module that has a contract or adapter file |
| `limits.cpu_ms`, `wall_ms`, `memory_mb` | Per-turn budgets of the script process |
| `limits.primitive_calls`, `adapter_calls` | Calls allowed per turn |
| `clock.tick_per_primitive`, `tick_per_turn` | World ticks per executed primitive and per executed turn |
| `rng.seed_visible` | Whether the model may read the Instance seed |

Limits and clock values are positive integers. The manual prints every one of them.

### Primitive contracts

| Key | Rule |
|---|---|
| `side_effect` | `read_only` or `mutating`. `external` is reserved for adapters. |
| `params` | Ordered object of parameter name to type |
| `returns` | One type |
| `preconditions` | Ordered list of `[expression, failure code]` pairs. `[]` means the call never fails after its parameters are valid. |
| `effects` | Expressions that hold after a successful call. A mutating primitive declares at least one. A read_only primitive declares none. |
| `doc` | One or two sentences, rendered verbatim. Must not be empty. |

Types are `str`, `int`, `bool`, `bytes`, `Path`, `None`, `list[T]`, `dict[str, T]` and the
record names declared in `manual/types.md`.

The SDK exposes each primitive as a function of its module. A primitive named after a Python
keyword is called with a trailing underscore: `kv.del` is `kv.del_`.

### Precondition language

| Allowed | Refused |
|---|---|
| The primitive's own parameter names | Any other name (CWR-V002) |
| Calls to `read_only` primitives of the same Build, with parameters or literals as arguments | Calls to mutating or external primitives (CWR-V001) |
| `len(x)` | Attribute access, indexing, floats, `true`, `false`, `null`, subtraction (CWR-V002) |
| Integer addition with `+` | |
| `==`, `!=`, `<`, `<=`, `>`, `>=`, not chained | |
| `and`, `or`, `not`, parentheses | |
| JSON integer and string literals | |

At run time the kernel first checks parameter types. A missing or mistyped argument fails with
`INVALID` and no precondition runs. Preconditions then run in declared order. The first one that is
false stops evaluation, and the failure names the primitive, the code and the one-based
precondition index. Calls made inside a precondition do not advance world time.

List the precondition a model should check first, first.

### The failure envelope

There are exactly eleven failure codes. A world cannot add one.

| Code | Use it when |
|---|---|
| `NOT_FOUND` | The target the parameters name does not exist |
| `LOCKED` | The target is held by a lock the caller does not hold |
| `CONFLICT` | A visible condition changed between check and call, or the action would duplicate a world object |
| `INVALID` | Parameters fail types, ranges or declared validity rules |
| `LIMIT` | A declared limit was reached |
| `UNAVAILABLE` | An external endpoint was unreachable, answered with an undeclared status, or a replay cassette had no entry |
| `RATE_LIMITED` | An external endpoint throttled the call. Carries `retry_after` in ticks. |
| `TIMEOUT` | An external call exceeded its `timeout_ms` |
| `REJECTED` | An external endpoint refused the content and gave a reason |
| `DUPLICATE_KEY` | An idempotency key was reused with a different payload |
| `INVARIANT_VIOLATION` | Never declared. The kernel raises it when a Build breaks its own contract. |

A failure carries `code`, `primitive`, `precondition_index`, `retry_after`, `limit` and `detail`.
`limit` is set on every `LIMIT` failure: the per-turn budget or process limit by name, or
`precondition:<index>` when a declared precondition raised `LIMIT`. `detail` is free text. It is
content, not interface.

### Adapters

An adapter is the only way a world reaches an outside service. Its contract guarantees the shape of
the result and the set of failures. It guarantees nothing about content.

| Key | Rule |
|---|---|
| `side_effect` | `external` |
| `params`, `returns`, `preconditions`, `doc` | As for primitives. Preconditions run before any network call. |
| `idempotency` | `none`, `natural` or `key` |
| `timeout_ms` | Positive integer, host milliseconds |
| `endpoint` | The only destination the adapter gateway may contact |
| `failures` | The codes the call may produce. Must include `UNAVAILABLE` and `TIMEOUT`, and `DUPLICATE_KEY` for a `key` adapter (CWR-F002). |
| `content_guaranteed` | Present and `false` (CWR-F002) |
| `effects` | Not allowed (CWR-C001) |

| `idempotency` | What the manual tells the model |
|---|---|
| `none` | A `TIMEOUT` may have had an effect |
| `natural` | Safe to repeat |
| `key` | Repeat with the same key to recover a lost receipt. A different payload under the same key is `DUPLICATE_KEY`. |

`adapters/<module>/live.py` maps the service's response into the declared shape. It has no
socket of its own. See [examples/refworld/adapters/publish.kvx](../../examples/refworld/adapters/publish.kvx).

### Python in lib/, live.py and migrations/

| File | May import |
|---|---|
| `lib/<module>.py`, `adapters/<module>/live.py` | `cwr_state`, `typing`, `dataclasses`, `json`, `re`, `math`, `collections`, `itertools` |
| `migrations/*.py` | The same, without `cwr_state` |

Every other import is CWR-L001, with file and line, including the generated SDK `cwr`: it is the
turn scripts' interface, and World Source code runs beside the kernel, not in the sandbox. So are `exec`, `eval`, `open`, `getattr` and
the other reflective builtins. Time and randomness come from the state handle, never from the host.

A `lib/` function raises only the codes its contract declares. Anything else fails the contract
tests at build time and is `INVARIANT_VIOLATION` at run time.

### Fixtures

A fixture file covers one primitive. Each `[case.<name>]` gives `world_time`, `state`, `params`,
`expect` (`"ok"` or a failure code), `precondition` (the index expected to fail, or `null`) and,
optionally, `returned`. The `ok` cases also assert the contract's effects. Adapter fixtures add a
recorded service response; see
[examples/refworld/fixtures/search/query.kvx](../../examples/refworld/fixtures/search/query.kvx).

### The manual

The generated manual has ten fixed sections: what the world is, how a turn works, the failure
envelope, limits and clock, the modules overview, the primitive reference, adapters, changes and
versions, proposals and an index. Your four prose files are merged into it. Prose that names a primitive, failure code or limit the
contracts do not define is CWR-D001.

### Changelog and versions

Each release lists its changes by name. Each change has a `kind`, a `target`, a `before` and an
`after` text and a `migration` path (empty unless the kind is `state_shape`).

| Kind | Minimum bump |
|---|---|
| `primitive_added` | minor |
| `primitive_removed` | major |
| `precondition_added` | major |
| `precondition_removed` | minor |
| `precondition_changed` | major |
| `effect_changed` | major |
| `failure_changed` | major |
| `limit_changed` | major when stricter, minor when looser |
| `state_shape` | major, with a migration |
| `adapter_changed` | major |
| `doc_only` | patch |

The changelog gate refuses a Build when a derived change has no matching entry, when an entry
announces a change that did not happen, when the version is not numerically greater than the
previous Build's, or when it is below the derived bump. While the major version is 0, a minor
increment satisfies a derived major bump and a patch increment satisfies a derived minor bump.

### Migrations

A migration is a pure function `migrate(state: dict) -> dict` in a file under `migrations/`. The
changelog entry's `migration` path names the file, and that path is what binds a migration to its
change; the compiler does not read versions from the file name. Name the file
`<from>_<to>_<name>.py` by convention, as the upgrade pairs do. The refworld example predates the
convention and names only its target version. A migration receives the whole prior state as plain
data and returns the new state without mutating its argument. It must be idempotent. It runs
inside the upgrade transaction, and an exception rolls the Instance back. See
[examples/refworld/migrations/0_2_0_kv_namespaces.py](../../examples/refworld/migrations/0_2_0_kv_namespaces.py).

### cwr build and cwr lint

```text
cwr build <source> [--out <dir>] [--previous <hash>] [--debug-effects] [--allow-example] [--json]
                   [--x-drift <file> | --x-silent-change | --x-version-only | --x-one-primitive]
cwr lint  <source> [--allow-example] [--json]
```

| Option | Meaning |
|---|---|
| `--out <dir>` | Build root. Default `build/` in the working directory. |
| `--previous <hash>` | Build to diff against. Default: the newest Build of the same world under the Build root. |
| `--debug-effects` | Accepted and leaves the Build unchanged. Contract tests always assert declared effects. To assert them on every call of a turn, pass `--debug-effects` to `cwr instance turn`. |
| `--allow-example` | Accept a source marked `meta.example = true` |
| `--json` | Print the report as JSON |
| `--x-*` | Build an experimental Build under `xbuild/`. One flag per Build. See [experiments.md](experiments.md). |

| Exit | Meaning |
|---|---|
| 0 | Built, or already built |
| 1 | Usage error or any other error |
| 2 | Parse or resolve failure |
| 3 | Lint failure |
| 4 | Contract-test failure |
| 5 | Diff failure |
| 6 | Changelog gate refusal |
| 7 | Package failure |

| Code | Meaning |
|---|---|
| CWR-C001 | Unknown file, section or key, or a shape rule violated |
| CWR-C002 | A primitive or adapter declared twice |
| CWR-C003 | A file for a module missing from `world.modules` |
| CWR-V001 | A precondition calls a primitive that is not `read_only` |
| CWR-V002 | A precondition names something undefined or leaves the expression language |
| CWR-F001 | A failure code outside the envelope |
| CWR-F002 | An adapter's `failures` or `content_guaranteed` breaks the adapter rules |
| CWR-D001 | An empty `doc`, or prose naming something the contracts do not define |
| CWR-L001 | A forbidden import or builtin in World Source Python |
| CWR-L002 | Contracts and `lib/` functions do not match one to one |
| CWR-M001 | A `state_shape` change without an existing migration |

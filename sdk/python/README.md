<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Python SDK runtime

A turn script reaches the world only through the generated SDK of its Build. This package holds the
parts of that SDK that are the same for every world: the value codec, the RPC client, the sandbox
runner and the templates from which the World Compiler generates each Build's world modules.

The SDK holds no rule. Every public SDK function performs exactly one call to the World Kernel, which
validates the parameters, evaluates the preconditions in order, executes the primitive and records
the call in the ledger. The SDK never refuses a call locally and never retries.

## Write a turn script

A turn script imports the world's modules by name, calls primitives with keyword arguments and may
`return` a value. A refused call raises the typed exception of its failure code from `cwr.errors`.

```python
import fs
from cwr.errors import NotFound

try:
    fs.delete(path="/missing.md")
except NotFound as failure:
    return {"code": failure.code, "precondition": failure.precondition_index}
```

Run against the reference world with `cwr instance turn <id> < script.py`, this script ends `ok`
and returns `{"code": "NOT_FOUND", "precondition": 1}`. Every exception carries `code`, `primitive`,
`precondition_index`, `retry_after` (ticks, `RATE_LIMITED` only), `limit` (`LIMIT` only) and `detail`.
Catch `cwr.errors.WorldError` to handle every code.

An import of anything other than the Build's world modules, `cwr` and `cwr.errors` is refused and
recorded. [SANDBOX.md](../../crates/cwr-kernel/SANDBOX.md) lists every facility the sandbox closes.

## Files

| File | Role | Where it runs |
| --- | --- | --- |
| `cwr/_codec.py` | The value codec and the result envelopes | Copied byte for byte into every Build's `sdk/cwr/`, so it is part of the Build hash |
| `cwr/_rpc.py` | The RPC client: one request and one response per primitive call over one Unix socket | Copied byte for byte into every Build's `sdk/cwr/` |
| `cwr/templates/*.tmpl` | Templates for the world modules, `cwr/__init__.py` and `cwr/errors.py`, rendered from `contracts.json` | Rendered by the World Compiler into every Build |
| `cwr/_runner.py` | Runs exactly one turn script inside the sandbox and enforces the in-interpreter closure layer | Embedded in the World Kernel. It is not part of a Build and no script imports it. |
| `cwr/_world.py` | World coordinates: `version()`, `time()` and `propose(text)` as single calls, and `coordinates(envelope)` to read a result envelope's world version, Build hash, time and turn | Used by tooling that reads result envelopes |

## Value codec

| Python value | On the wire |
| --- | --- |
| `None`, `bool`, `int`, `str` | JSON literals |
| Finite `float` | A JSON number. No contract type is a float, so the kernel refuses it as `INVALID`. |
| `bytes`, `bytearray` | `{"$bytes": "<standard base64>"}` |
| `list`, `tuple` | JSON arrays |
| `dict` with `str` keys | A JSON object. A key that starts with `$` gains one more leading `$`. |
| Anything else | `{"$invalid": "<type name>"}`, which the kernel refuses as `INVALID` and records |

The serialised form is canonical JSON: keys sorted by code point, separators `,` and `:` with no
whitespace, and non-ASCII characters written as UTF-8.

## RPC framing

Every message is a 4-byte unsigned big-endian length followed by that many bytes of canonical JSON. A
request is `{"v": 1, "id": <n>, "primitive": "<module.name>", "params": {...}}`. A response is
`{"v": 1, "id": <n>, "result": <per-call result envelope>}`. A Python keyword parameter such as
`from_` travels under its contract name, `from`.

## Package

`pyproject.toml` declares the package `cwr-sdk`, version 0.1.0, for Python 3.12 or later, with no
runtime dependencies. Its version must equal the CLI crate's version and the release in
`benchmark/release.kvx`, which `tools/release.py` checks.

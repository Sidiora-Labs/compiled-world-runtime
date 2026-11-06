<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Shared Python reference implementations

`cwr_core` is the one Python implementation of two CWR contracts: the KVX grammar and the failure
envelope. It accepts and rejects exactly what the specification validator and the contract crate
accept and reject, so Python tooling and the Rust runtime never disagree about a file or a failure.

Both modules use the standard library only and are safe under `python3 -I`.

## Modules

| Module | Provides | Agrees with |
|---|---|---|
| `cwr_core.kvx` | `loads`, `load`, `dump`, `dumps_value` and `KVXError`, which carries a `file:line:col` location | `parse_kvx` in [validate_spec.py](../spec/cwr/tools/validate_spec.py) |
| `cwr_core.envelope` | `CODES` (the eleven failure codes with their guidance), `BY_NAME`, `RETRY_AFTER_CODES`, `validate` and `loads` | [world-source.md](../spec/cwr/world-source.md) section 6 and the contract crate's envelope type |

## Guarantees

- `kvx.loads` accepts exactly the input the specification validator accepts. A test compares the two
  on a fixed corpus and on every `.kvx` file under `spec/`, `benchmark/`, `examples/` and `tests/`.
- `kvx.dump` is deterministic, and its output round-trips through `kvx.loads`.
- `envelope.validate` applies the contract crate's rules. An envelope has exactly the keys `code`,
  `primitive`, `precondition`, `limit` and `detail`, plus `retry_after` on `RATE_LIMITED` and on no
  other code. `limit` is non-null only on `LIMIT`. The function returns every violation it finds, and
  an empty list for a valid envelope.

## Use

A script adds `core/` to `sys.path` explicitly and never relies on the current directory:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
from cwr_core import envelope, kvx
```

`parents[1]` is correct for a script one directory below the repository root, such as
`tools/release.py`. Adjust the index to the script's depth.

From the repository root:

```sh
python3 -I -c 'import sys; sys.path.insert(0, "core"); from cwr_core import kvx; print(kvx.load("examples/refworld/world.kvx")["world"]["version"])'
```

## Tests

```sh
python3 -m unittest discover -s core/tests -t .
```

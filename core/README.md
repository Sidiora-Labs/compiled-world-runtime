<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# core: shared Python reference implementations

`core/cwr_core` holds the one Python implementation of two contracts that several tools need:

| Module | Contract | Normative source |
|---|---|---|
| `cwr_core.kvx` | Strict KVX grammar: `loads`, `load`, `dump`, `dumps_value`, located `KVXError` (`file:line:col`) | `parse_kvx` in [validate_spec.py](../spec/cwr/tools/validate_spec.py) |
| `cwr_core.envelope` | The closed eleven-code failure envelope: `CODES`, `RETRY_AFTER_CODES`, `validate`, `loads` | [world-source.md](../spec/cwr/world-source.md) section 6 and `crates/cwr-contract/src/envelope.rs` |

Both are stdlib only. `kvx.loads` accepts and rejects exactly what the spec validator does; a test
compares the two on a fixed corpus and on every `.kvx` file under `spec/`, `benchmark/`, `examples/`
and `tests/`. `kvx.dump` is deterministic and round-trips through `loads`. `envelope.validate`
applies the same rules as the contract crate's deserialiser: exactly the keys `code`, `primitive`,
`precondition`, `limit`, `detail`, plus `retry_after` only and always on `RATE_LIMITED`, and
`limit` non-null only on `LIMIT`.

## Use

Scripts are `python3 -I` safe, so import by adding `core/` to `sys.path` explicitly:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[N] / "core"))
from cwr_core import envelope, kvx
```

## Consumers

The spec tooling, the experiment harness (`benchmark/harness/`), the stub servers
(`benchmark/stubs/`) and the qualification runner and checker (`tests/cwr/`) each carry a private
KVX reader today. They are to import from `cwr_core`
instead; that migration is a separate pass and has not been made. Until it is, the equivalence
test is what keeps the copies from drifting.

## Tests

```sh
python3 -m unittest discover -s core/tests -t .
```

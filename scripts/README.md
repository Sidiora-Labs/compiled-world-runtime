<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# scripts: operator scripts

Bash entry points for the operations a person runs by hand. Each script changes to the repository
root first, uses `set -euo pipefail`, never reads or prints credentials, and exits 2 on a usage error.
They pass `bash -n` and `shellcheck` at its default severity.

| Script | Does |
|---|---|
| `bootstrap.sh` | Installs what `make check` needs if missing (`rustfmt` and `clippy` through rustup, `ruff` through `pip --user --break-system-packages`), checks for cargo, `python3.12` and Python 3.12+ as `python3`, warns when `bwrap` (the turn sandbox) is absent, and checks that the credentials file exists and is not group- or world-readable, without reading it. |
| `campaign.sh h1\|h2\|h3\|h4 [--seeds N]` | Runs one hypothesis campaign: `benchmark/harness/run.py --hypothesis HN --campaign benchmark/campaigns/hN.kvx`. Fails with a clear message while the campaign file (tasks 5.2 and 5.3) or the runner's `--campaign` option (task 5.1) does not exist. |
| `release.sh [--dry-run] [VAR=value ...]` | Runs `make release` and keeps the log in `artifacts/release.log`; `--dry-run` runs only the checks in `tools/release.py`. |

```sh
scripts/bootstrap.sh
scripts/campaign.sh h1 --seeds 5
scripts/release.sh --dry-run
```

Campaigns run in replay mode against frozen cassettes, and every arm is a CWR Build with one pillar
ablated ([spec/cwr/experiments.md](../spec/cwr/experiments.md)). A campaign with fewer seeds than
pre-registered is a recorded deviation, never a PASS.

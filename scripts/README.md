<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Operator scripts

Status: the experiment harness does not yet accept campaign files, so `campaign.sh` refuses every
hypothesis. The H1 and H2 campaigns run through `benchmark/campaigns/h1h2.py` instead.

These are the Bash entry points for operations a person runs by hand. Each script changes to the
repository root first and runs under `set -euo pipefail`. `campaign.sh` and `release.sh` exit 2 on a
usage error. No script reads, prints or copies a credential.

| Script | Purpose |
|---|---|
| `bootstrap.sh` | Prepares a development host. It installs `rustfmt` and `clippy` through rustup and `ruff` through pip when they are missing. It checks for cargo, `python3.12` and a `python3` of version 3.12 or later, and it warns when `bwrap` is absent, because the World Kernel refuses to run a turn without it. It confirms that model credentials are configured and private, without reading them. Exit 0 when every required check passes. |
| `campaign.sh h1\|h2\|h3\|h4 [--seeds N]` | Runs one pre-registered campaign through the experiment harness, from `benchmark/campaigns/hN.kvx`. It refuses with exit 2 and a reason when the campaign file is missing or the harness cannot read campaign files. |
| `release.sh [--dry-run] [VAR=value ...]` | Runs `make release` and keeps the full log in `artifacts/release.log`. `--dry-run` runs only the release checks and writes nothing. |

```sh
scripts/bootstrap.sh
scripts/release.sh --dry-run
```

Campaigns run in replay mode against frozen cassettes. Every arm is a CWR Build with at most one pillar
ablated, as [experiments.md](../spec/cwr/experiments.md) defines. A campaign with fewer seeds than
pre-registered is a recorded deviation and never a PASS.

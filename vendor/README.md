<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Vendored crate sources

This directory makes the Rust workspace buildable without network access. `make vendor` runs
`cargo vendor vendor/cargo`, which copies the source of every crate pinned in `Cargo.lock` into
`vendor/cargo/`. A build that reads from there resolves exactly the locked versions.

## Build offline

Day-to-day development reads crates from crates.io, so a dependency change does not require
re-vendoring. To build from the vendored sources, add this source replacement to
`.cargo/config.toml` at the repository root:

```toml
[source.crates-io]
replace-with = "vendored-sources"

[source.vendored-sources]
directory = "vendor/cargo"
```

When `.cargo/config.toml` is present, the release bundle includes it together with `vendor/cargo/`.

## Licensing

Vendored code keeps its own license headers. It is exempt from the Sidiora source header check and
from ruff. Third-party crates and their licenses are listed in [NOTICE](../NOTICE).

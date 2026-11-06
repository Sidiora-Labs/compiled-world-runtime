<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# vendor: crate sources for offline, reproducible builds

`make vendor` runs `cargo vendor vendor/cargo`, copying the source of every crate pinned in
`Cargo.lock` into `vendor/cargo/`. A build that reads from there needs no network and resolves
exactly the locked versions.

During development cargo still reads crates.io. The switch to the vendored sources is a
`.cargo/config.toml` source replacement applied at release time, so day-to-day dependency
changes do not require re-vendoring:

```toml
[source.crates-io]
replace-with = "vendored-sources"

[source.vendored-sources]
directory = "vendor/cargo"
```

Vendored code keeps its own license headers and is exempt from the Sidiora header check and from
ruff. Third-party crates and their licenses are listed in [NOTICE](../NOTICE).

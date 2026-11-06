// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! `--x-silent-change` and `--x-version-only`: the notice-rendering marker (compiler.md section
//! 10; clauses 5.3 and 10.3; task 3.2).
//!
//! Both flags leave every Build file except `manifest.kvx` byte-identical to the control Build:
//! contracts, manual, SDK, migrations and the changelog gate (which still runs) are untouched. The
//! marker is the manifest's `experimental_flag`. `cwr instance upgrade` into such a Build stores
//! the same recompile notice as into the control Build; only the model surface's rendering of
//! component 2 differs ([`cwr_kernel::notice::Rendering::for_build`] and
//! [`cwr_kernel::notice::render`]):
//!
//! | Flag | Rendering of the stored notice |
//! |---|---|
//! | none (control) | the whole notice |
//! | `x-version-only` | its first line, the version header, alone |
//! | `x-silent-change` | nothing; component 2 is absent |

use serde_json::{json, Value};

/// Flag name of a silent-change Build.
pub const SILENT_CHANGE: &str = "x-silent-change";
/// Flag name of a version-only Build.
pub const VERSION_ONLY: &str = "x-version-only";

/// The rendering a Build's flag selects: `"silent"`, `"version_only"` or `"full"`.
pub fn rendering(flag: Option<&str>) -> &'static str {
    match flag {
        Some(SILENT_CHANGE) => "silent",
        Some(VERSION_ONLY) => "version_only",
        _ => "full",
    }
}

/// The attribution entry of a silent-change or version-only Build: the rendering marker.
pub fn entry(flag: &str) -> Value {
    json!({
        "kind": "notice_rendering",
        "target": "notice",
        "field": "rendering",
        "before": rendering(None),
        "after": rendering(Some(flag)),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use cwr_kernel::notice::Rendering;

    fn build(flag: Option<&str>) -> cwr_kernel::Build {
        cwr_kernel::Build {
            dir: std::path::PathBuf::from("/nonexistent"),
            hash: "sha256:00".into(),
            name: "w".into(),
            version: "0.2.0".into(),
            experimental: flag.map(str::to_string),
            modules: Vec::new(),
            limits: Default::default(),
            clock: cwr_kernel::TickConfig::new(1, 10).expect("ticks"),
            seed_visible: true,
            primitives: Default::default(),
        }
    }

    #[test]
    fn the_kernel_surface_selects_the_rendering_each_marker_declares() {
        for (flag, kernel, marker) in [
            (None, Rendering::Full, "full"),
            (Some(VERSION_ONLY), Rendering::VersionOnly, "version_only"),
            (Some(SILENT_CHANGE), Rendering::Silent, "silent"),
            (Some("x-one-primitive"), Rendering::Full, "full"),
            (Some("x-drift"), Rendering::Full, "full"),
        ] {
            assert_eq!(Rendering::for_build(&build(flag)), kernel, "{flag:?}");
            assert_eq!(rendering(flag), marker);
        }
    }

    #[test]
    fn entries_name_the_rendering() {
        assert_eq!(entry(SILENT_CHANGE)["after"], "silent");
        assert_eq!(entry(VERSION_ONLY)["after"], "version_only");
        assert_eq!(entry(VERSION_ONLY)["before"], "full");
    }
}

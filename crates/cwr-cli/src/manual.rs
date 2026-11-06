// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! `cwr manual <build>` (spec/cwr/compiler.md section 9; task 1.3).
//!
//! ```text
//! cwr manual <hash|dir> [--out <dir>]
//! ```
//!
//! Verifies the Build's hash (compiler.md section 7: recomputed over every in-hash file and
//! compared with the manifest's recorded hash and the directory name), then prints its
//! `manual.md` byte for byte on stdout. A hash reference (`sha256:<hex>` or `<hex>`) is looked up
//! under `--out` (default `build/`) and the experimental root `xbuild/` beside it.
//!
//! A Build is read-only after packaging (clause 4.5): a Build whose hash verifies but in which any
//! file or directory carries a write permission bit (or a symbolic link) is refused as well, so a
//! re-opened Build is not served even before anyone has changed a byte of it.
//!
//! Exit codes: 0 manual printed; 5 Build missing, failing verification or writable; 1 usage error.

use std::path::PathBuf;

use cwr_compiler::diff::build_roots;
use cwr_compiler::package::{resolve, sealed, verify};
use cwr_compiler::Options;

use crate::cli::{self, Spec};

const SPEC: Spec = Spec {
    values: &["--out"],
    repeatable: &[],
    flags: &[],
};

/// One-line help text for `cwr manual`, shown by `cwr help`.
pub const SUMMARY: &str =
    "print a Build's manual after verifying its hash: cwr manual <hash|dir> [--out <dir>]";

fn usage_error(message: &str) -> i32 {
    eprintln!("cwr manual: {message}");
    eprintln!("usage: {SUMMARY}");
    1
}

/// Runs `cwr manual`; `args` follow `manual`. Prints `manual.md` only after the Build's hash
/// verifies; returns 0 on success, 5 when the Build is missing or fails verification and 1 on a
/// usage or write error.
pub fn run(args: &[String]) -> i32 {
    let p = match cli::parse(args, &SPEC) {
        Ok(p) => p,
        Err(m) => return usage_error(&m),
    };
    let reference = match p.positional.as_slice() {
        [r] => r.clone(),
        [] => return usage_error("missing <hash|dir>"),
        [_, extra, ..] => return usage_error(&format!("unexpected argument {extra:?}")),
    };
    let out = p.value("--out").map(PathBuf::from);
    let options = Options {
        out,
        ..Options::default()
    };
    let dir = match resolve(&reference, &build_roots(&options)) {
        Ok(d) => d,
        Err(m) => {
            eprintln!("cwr manual: {m}");
            return 5;
        }
    };
    if let Err(m) = verify(&dir) {
        eprintln!("cwr manual: {m}");
        return 5;
    }
    // The reference itself may be a symbolic link to the Build; its contents may not.
    if !sealed(&dir.canonicalize().unwrap_or_else(|_| dir.clone())) {
        eprintln!(
            "cwr manual: Build {} is writable after packaging: a file or directory carries a write permission bit; refusing a Build that is no longer read-only",
            dir.display()
        );
        return 5;
    }
    match std::fs::read(dir.join("manual.md")) {
        Ok(bytes) => cli::emit_bytes(&bytes),
        Err(e) => {
            eprintln!("cwr manual: {}: {e}", dir.join("manual.md").display());
            5
        }
    }
}

#[cfg(test)]
mod tests {
    use super::run;

    fn args(v: &[&str]) -> Vec<String> {
        v.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn usage_errors_exit_one_and_a_missing_build_exits_five() {
        assert_eq!(run(&args(&[])), 1);
        assert_eq!(run(&args(&["a", "b"])), 1);
        assert_eq!(run(&args(&["a", "--out", "x", "--out", "y"])), 1);
        assert_eq!(run(&args(&["a", "--out"])), 1);
        assert_eq!(run(&args(&["a", "--nope"])), 1);
        assert_eq!(run(&args(&["/nonexistent/build"])), 5);
    }
}

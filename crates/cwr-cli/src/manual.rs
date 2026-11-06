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

use std::io::Write;
use std::path::PathBuf;

use cwr_compiler::diff::build_roots;
use cwr_compiler::package::{resolve, sealed, verify};
use cwr_compiler::Options;

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
    let mut reference: Option<String> = None;
    let mut out: Option<PathBuf> = None;
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--out" => {
                i += 1;
                match args.get(i) {
                    Some(v) => out = Some(PathBuf::from(v)),
                    None => return usage_error("--out needs a value"),
                }
            }
            a if a.starts_with('-') => return usage_error(&format!("unknown option {a}")),
            a => {
                if reference.is_some() {
                    return usage_error(&format!("unexpected argument {a:?}"));
                }
                reference = Some(a.to_string());
            }
        }
        i += 1;
    }
    let Some(reference) = reference else {
        return usage_error("missing <hash|dir>");
    };
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
        Ok(bytes) => {
            let mut stdout = std::io::stdout().lock();
            if stdout
                .write_all(&bytes)
                .and_then(|_| stdout.flush())
                .is_err()
            {
                return 1;
            }
            0
        }
        Err(e) => {
            eprintln!("cwr manual: {}: {e}", dir.join("manual.md").display());
            5
        }
    }
}

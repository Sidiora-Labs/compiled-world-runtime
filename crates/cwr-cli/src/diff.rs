// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! `cwr diff <a> <b>` (spec/cwr/compiler.md sections 5, 6 and 9; task 1.2).
//!
//! ```text
//! cwr diff <a> <b> [--out <dir>] [--allow-example]
//! ```
//!
//! Prints `contract_diff.json` for the change from `a` to `b` on stdout: every rule entry, every
//! doc entry, every `manual/` prose entry (`prose_entries`), the derived minimum bump, the declared bump between the two versions and whether
//! the pre-1.0 rule (decision P15) was applied. Each argument is a Build directory, a Build hash
//! (`sha256:<hex>` or `<hex>`, looked up under `--out`, default `build/`, and `xbuild/`), or a
//! World Source directory, which is compiled through lint first. A Build is used only after its
//! hash verifies. Output is canonical JSON (sorted keys, two-space indentation), byte-identical
//! across hosts.
//!
//! Exit codes: 0 diff printed; 5 when a Build is missing or fails hash verification, or a World
//! Source does not pass lint (diagnostics on stderr); 1 usage error.

use std::path::PathBuf;

use cwr_compiler::diff::{build_roots, diff_references};
use cwr_compiler::Options;

use crate::cli::{self, Spec};

const SPEC: Spec = Spec {
    values: &["--out"],
    repeatable: &[],
    flags: &["--allow-example"],
};

/// One-line help text for `cwr diff`, shown by `cwr help`.
pub const SUMMARY: &str = "print the ContractDiff between two Builds or World Sources: cwr diff <a> <b> [--out <dir>] [--allow-example]";

fn usage_error(message: &str) -> i32 {
    eprintln!("cwr diff: {message}");
    eprintln!("usage: {SUMMARY}");
    1
}

/// Runs `cwr diff`; `args` follow `diff`. Returns 0 with the canonical diff on stdout, 5 when a
/// Build or World Source cannot be used and 1 on a usage error.
pub fn run(args: &[String]) -> i32 {
    let p = match cli::parse(args, &SPEC) {
        Ok(p) => p,
        Err(m) => return usage_error(&m),
    };
    let [a, b] = p.positional.as_slice() else {
        return usage_error("expected exactly two Builds or World Sources");
    };
    let out = p.value("--out").map(PathBuf::from);
    let allow_example = p.flag("--allow-example");
    let options = Options {
        out,
        allow_example,
        ..Options::default()
    };
    let roots = build_roots(&options);
    match diff_references(a, b, &roots, allow_example) {
        // Task 3.2: a control/experimental pair also prints the attribution (clause 10.5).
        Ok(d) => match cwr_compiler::experimental::diff_output(&d, a, b, &roots) {
            Ok(text) => cli::emit(&text),
            Err(m) => {
                eprintln!("cwr diff: {m}");
                5
            }
        },
        Err(diags) => {
            for d in diags {
                eprintln!("{d}");
            }
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
    fn usage_errors_exit_one_and_missing_builds_exit_five() {
        assert_eq!(run(&args(&[])), 1);
        assert_eq!(run(&args(&["a"])), 1);
        assert_eq!(run(&args(&["a", "b", "c"])), 1);
        assert_eq!(run(&args(&["a", "b", "--out", "x", "--out", "y"])), 1);
        assert_eq!(
            run(&args(&["a", "b", "--allow-example", "--allow-example"])),
            1
        );
        assert_eq!(run(&args(&["a", "b", "--nope"])), 1);
        assert_eq!(run(&args(&["/nonexistent/a", "/nonexistent/b"])), 5);
    }
}

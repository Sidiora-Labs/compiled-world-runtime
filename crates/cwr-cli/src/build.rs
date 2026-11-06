// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! `cwr build <source>` and `cwr lint <source>` (spec/cwr/compiler.md section 9).
//!
//! ```text
//! cwr build <source> [--out <dir>] [--previous <hash>] [--debug-effects] [--allow-example]
//!                    [--x-drift <file> | --x-silent-change | --x-version-only | --x-one-primitive]
//!                    [--json]
//! cwr lint  <source> [--allow-example] [--json]
//! ```
//!
//! `build` runs every pipeline stage in order and stops at the first failing stage; `lint`
//! runs parse, resolve and lint only. `--json` prints the machine-readable report (stages,
//! statuses and every diagnostic with file, line, column, section, key and lint code) on
//! stdout instead of the text report. Diagnostics are deterministic: sorted by file, line,
//! column, code and message.
//!
//! Exit codes: 0 success; 2 parse or resolve; 3 lint; 4 contract tests; 5 diff; 6 changelog
//! gate; 7 package; 1 usage or any other error. A stage the compiler does not implement is
//! reported as `pending`: the run stops there, writes nothing and exits 1. `--out` is the
//! Build root (default `build/` relative to the working directory); nothing is written
//! unless every stage up to package passes.
//!
//! `--debug-effects` is accepted as compiler.md section 9 lists it and leaves the Build unchanged:
//! contract tests always assert every declared effect, and the per-call assertion during a turn
//! is the turn option `cwr instance turn --debug-effects`.
//!
//! One `--x-*` flag builds an experimental Build (compiler.md section 10, task 3.2) under
//! `xbuild/` beside the Build root, with `experimental = true` and the flag name in
//! `manifest.kvx` ([`cwr_compiler::experimental`]); two `--x-*` flags are refused at argument
//! parsing (exit 1), one pillar per experimental Build.

use std::path::PathBuf;

use cwr_compiler::experimental::{self, Flag};
use cwr_compiler::{compile, Options, Report, Stage};

use crate::cli::{self, Spec};

/// One-line help text for `cwr build`, shown by `cwr help`.
pub const SUMMARY: &str =
    "compile a World Source: cwr build <source> [--out <dir>] [--previous <hash>] [--debug-effects] [--allow-example] [--x-drift <file> | --x-silent-change | --x-version-only | --x-one-primitive] [--json]";
/// One-line help text for `cwr lint`, shown by `cwr help`.
pub const LINT_SUMMARY: &str =
    "parse, resolve and lint a World Source: cwr lint <source> [--allow-example] [--json]";

const X_FLAGS: [&str; 4] = [
    "--x-drift",
    "--x-silent-change",
    "--x-version-only",
    "--x-one-primitive",
];

struct Args {
    source: PathBuf,
    json: bool,
    allow_example: bool,
    previous: Option<String>,
    out: Option<PathBuf>,
    experimental: Option<Flag>,
}

fn usage_error(cmd: &str, message: &str) -> i32 {
    eprintln!("cwr {cmd}: {message}");
    eprintln!(
        "usage: {}",
        if cmd == "build" {
            SUMMARY
        } else {
            LINT_SUMMARY
        }
    );
    1
}

const BUILD_SPEC: Spec = Spec {
    values: &["--out", "--previous", "--x-drift"],
    repeatable: &[],
    flags: &[
        "--json",
        "--allow-example",
        "--debug-effects",
        "--x-silent-change",
        "--x-version-only",
        "--x-one-primitive",
    ],
};
const LINT_SPEC: Spec = Spec {
    values: &[],
    repeatable: &[],
    flags: &["--json", "--allow-example"],
};

/// Parses the arguments of `cwr build` or `cwr lint` (`cmd`); the error is the usage message.
fn parse_args(cmd: &str, args: &[String]) -> Result<Args, String> {
    let p = cli::parse(
        args,
        if cmd == "build" {
            &BUILD_SPEC
        } else {
            &LINT_SPEC
        },
    )?;
    let given: Vec<&str> = X_FLAGS.iter().copied().filter(|f| p.given(f)).collect();
    if given.len() > 1 {
        return Err(format!(
            "one pillar per experimental Build: {} cannot be combined",
            given.join(" and ")
        ));
    }
    let experimental = given
        .first()
        .and_then(|f| Flag::from_arg(f, p.value("--x-drift")));
    let source = match p.positional.as_slice() {
        [s] => PathBuf::from(s),
        [] => return Err("missing <source> directory".into()),
        [_, extra, ..] => return Err(format!("unexpected argument {extra:?}")),
    };
    Ok(Args {
        source,
        json: p.flag("--json"),
        allow_example: p.flag("--allow-example"),
        // The previous Build feeds the diff stage; the output directory is the Build root (where
        // the default previous Build is looked up, and where package writes).
        previous: p.value("--previous").map(str::to_string),
        out: p.value("--out").map(PathBuf::from),
        experimental,
    })
}

fn emit(report: &Report, json: bool) -> i32 {
    let written = if json {
        cli::emit(&report.to_json())
    } else if report.exit_code == 0 {
        cli::emit(&report.to_text())
    } else {
        eprint!("{}", report.to_text());
        0
    };
    if written != 0 {
        return written;
    }
    report.exit_code
}

fn run_through(cmd: &str, args: &[String], through: Stage) -> i32 {
    let parsed = match parse_args(cmd, args) {
        Ok(a) => a,
        Err(m) => return usage_error(cmd, &m),
    };
    let options = Options {
        allow_example: parsed.allow_example,
        through,
        previous: parsed.previous,
        out: parsed.out,
    };
    let report = match &parsed.experimental {
        Some(flag) => experimental::build(&parsed.source, &options, flag),
        None => compile(&parsed.source, &options),
    };
    emit(&report, parsed.json)
}

/// Runs `cwr build`; `args` follow `build`. Runs every stage through package, stops at the
/// first failing stage and returns that stage's exit code (0 on success).
pub fn run(args: &[String]) -> i32 {
    run_through("build", args, Stage::Package)
}

/// Runs `cwr lint`; `args` follow `lint`. Runs parse, resolve and lint only and returns 0, 2
/// (parse or resolve) or 3 (lint); 1 on a usage error.
pub fn run_lint(args: &[String]) -> i32 {
    run_through("lint", args, Stage::Lint)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(v: &[&str]) -> Vec<String> {
        v.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn build_takes_one_source_and_its_options() {
        let a = parse_args(
            "build",
            &args(&["src", "--out", "o", "--previous", "sha256:ab", "--json"]),
        )
        .unwrap();
        assert_eq!(a.source, PathBuf::from("src"));
        assert_eq!(a.out, Some(PathBuf::from("o")));
        assert_eq!(a.previous.as_deref(), Some("sha256:ab"));
        assert!(a.json && !a.allow_example && a.experimental.is_none());
    }

    #[test]
    fn repeats_missing_values_and_extra_sources_are_usage_errors() {
        for bad in [
            &["src", "--out", "a", "--out", "b"][..],
            &["src", "--previous", "a", "--previous", "b"],
            &["src", "--json", "--json"],
            &["src", "--out"],
            &["src", "other"],
            &[],
            &["src", "--nope"],
        ] {
            assert!(parse_args("build", &args(bad)).is_err(), "{bad:?}");
        }
    }

    #[test]
    fn two_experimental_flags_are_refused() {
        let e = parse_args(
            "build",
            &args(&["src", "--x-silent-change", "--x-version-only"]),
        )
        .err()
        .unwrap();
        assert!(e.contains("one pillar"), "{e}");
        let a = parse_args("build", &args(&["src", "--x-drift", "d.kvx"])).unwrap();
        assert!(matches!(a.experimental, Some(Flag::Drift(_))));
    }

    #[test]
    fn lint_refuses_build_only_options() {
        assert!(parse_args("lint", &args(&["src", "--out", "o"])).is_err());
        assert!(parse_args("lint", &args(&["src", "--x-version-only"])).is_err());
        assert!(parse_args("lint", &args(&["src", "--allow-example"])).is_ok());
    }
}

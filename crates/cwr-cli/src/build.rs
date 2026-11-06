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
//! One `--x-*` flag builds an experimental Build (compiler.md section 10, task 3.2) under
//! `xbuild/` beside the Build root, with `experimental = true` and the flag name in
//! `manifest.kvx` ([`cwr_compiler::experimental`]); two `--x-*` flags are refused at argument
//! parsing (exit 1), one pillar per experimental Build.

use std::path::PathBuf;

use cwr_compiler::experimental::{self, Flag};
use cwr_compiler::{compile, Options, Report, Stage};

/// One-line help text for `cwr build`, shown by `cwr help`.
pub const SUMMARY: &str =
    "compile a World Source: cwr build <source> [--out <dir>] [--previous <hash>] [--allow-example] [--x-drift <file> | --x-silent-change | --x-version-only | --x-one-primitive] [--json]";
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

fn parse_args(cmd: &str, args: &[String]) -> Result<Args, i32> {
    let mut source = None;
    let mut json = false;
    let mut allow_example = false;
    let mut previous = None;
    let mut out = None;
    let mut experimental: Vec<String> = Vec::new();
    let mut flag = None;
    let mut i = 0;
    while i < args.len() {
        let a = args[i].as_str();
        match a {
            "--json" => json = true,
            "--allow-example" => allow_example = true,
            "--out" | "--previous" if cmd == "build" => {
                if i + 1 >= args.len() {
                    return Err(usage_error(cmd, &format!("{a} needs a value")));
                }
                // The previous Build feeds the diff stage; the output directory is the Build root
                // (where the default previous Build is looked up, and where package writes).
                i += 1;
                if a == "--previous" {
                    previous = Some(args[i].clone());
                } else {
                    out = Some(PathBuf::from(&args[i]));
                }
            }
            "--debug-effects" if cmd == "build" => {}
            "--x-drift" if cmd == "build" => {
                if i + 1 >= args.len() {
                    return Err(usage_error(cmd, "--x-drift needs a drift file"));
                }
                experimental.push(a.to_string());
                i += 1;
                flag = Flag::from_arg(a, Some(&args[i]));
            }
            _ if cmd == "build" && X_FLAGS.contains(&a) => {
                experimental.push(a.to_string());
                flag = Flag::from_arg(a, None);
            }
            _ if a.starts_with('-') => {
                return Err(usage_error(cmd, &format!("unknown option {a}")))
            }
            _ => {
                if source.is_some() {
                    return Err(usage_error(cmd, &format!("unexpected argument {a:?}")));
                }
                source = Some(PathBuf::from(a));
            }
        }
        i += 1;
    }
    if experimental.len() > 1 {
        return Err(usage_error(
            cmd,
            &format!(
                "one pillar per experimental Build: {} cannot be combined",
                experimental.join(" and ")
            ),
        ));
    }
    let Some(source) = source else {
        return Err(usage_error(cmd, "missing <source> directory"));
    };
    Ok(Args {
        source,
        json,
        allow_example,
        previous,
        out,
        experimental: flag,
    })
}

fn emit(report: &Report, json: bool) -> i32 {
    if json {
        print!("{}", report.to_json());
    } else if report.exit_code == 0 {
        print!("{}", report.to_text());
    } else {
        eprint!("{}", report.to_text());
    }
    report.exit_code
}

fn run_through(cmd: &str, args: &[String], through: Stage) -> i32 {
    let parsed = match parse_args(cmd, args) {
        Ok(a) => a,
        Err(code) => return code,
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

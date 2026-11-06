// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! `cwr`: the CWR command line.
//!
//! Hand-rolled dispatcher: `cwr <subcommand> [args...]`. Each subcommand lives in its own
//! module file exposing `pub fn run(args: &[String]) -> i32` and `pub const SUMMARY: &str`;
//! adding a subcommand is one `mod` line and one match arm (plus one help line).
//!
//! Exit codes: 0 success; 1 usage, an unwritable stdout ([`cli::emit`]) or any other error;
//! subcommands document their own codes (spec/cwr/compiler.md section 9: 2 parse or resolve,
//! 3 lint, and so on). Arguments follow one rule ([`cli::parse`]): a value option or flag
//! given twice is a usage error.

mod build;
mod cli;
mod context;
mod contract;
mod diff;
mod instance;
mod manual;
mod release;
mod replay;
mod turn;
mod upgrade;

fn usage() -> String {
    let mut s = String::from("usage: cwr <subcommand> [args...]\n\nsubcommands:\n");
    for (name, summary) in [
        ("build", build::SUMMARY),
        ("contract", contract::SUMMARY),
        ("diff", diff::SUMMARY),
        ("instance", instance::SUMMARY),
        ("lint", build::LINT_SUMMARY),
        ("manual", manual::SUMMARY),
        ("release", release::SUMMARY),
    ] {
        s.push_str(&format!("  {name:<10} {summary}\n"));
    }
    s
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let code = match args.first().map(String::as_str) {
        Some("build") => build::run(&args[1..]),
        Some("contract") => contract::run(&args[1..]),
        Some("diff") => diff::run(&args[1..]),
        Some("instance") => instance::run(&args[1..]),
        Some("lint") => build::run_lint(&args[1..]),
        Some("manual") => manual::run(&args[1..]),
        Some("release") => release::run(&args[1..]),
        Some("help") | Some("--help") | Some("-h") => cli::emit(&usage()),
        None => {
            eprint!("{}", usage());
            1
        }
        Some(other) => {
            eprintln!("cwr: unknown subcommand {other:?}");
            eprint!("{}", usage());
            1
        }
    };
    std::process::exit(code);
}

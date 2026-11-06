// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! `cwr instance turn`: run one sandboxed turn script on an Instance (task 2.2; spec/cwr/kernel.md
//! sections 2 to 5, model-surface.md section 4).
//!
//! ```text
//! cwr instance turn <id> [--script <file> | < script.py] [--debug-effects]
//!                        [--home <dir>] [--builds <dir>] [--diagnostic-no-language-guard]
//! ```
//!
//! Prints the result envelope `{status, returned, error, trace, world, resources}` as one line of
//! canonical JSON on stdout and exits 0 for `ok`, `error` and `partial` alike. On stderr it prints
//! one line naming the sandbox mechanism in use. Exit codes: 1 usage or any kernel error (an
//! Instance that is not idle, sealed, or a sandbox that cannot be provided; the kernel never runs
//! a script unsandboxed), 2 Build hash mismatch, 3 experimental Build.
//!
//! `--message <file>` (task 3.1, model-surface.md section 3) reads a raw model message instead of a
//! script: the turn protocol extracts its one fenced python block, and a message with zero or more
//! than one fenced block, a non-python block or an unclosed fence is refused as `INVALID` naming
//! `turn.protocol` without executing anything or advancing world time; the refusal envelope is
//! printed and the exit code is 4.
//!
//! `--debug-effects` turns on step 4 of the atomic contract check (every declared effect is
//! asserted after an ok mutating call). `--diagnostic-no-language-guard` switches off only the
//! in-interpreter layer of the sandbox so that the kernel-level layer (namespaces, read-only view,
//! rlimits) can be demonstrated on its own; it is an operator diagnostic documented in
//! crates/cwr-kernel/SANDBOX.md and never used for a model turn.

use std::io::Read;
use std::path::PathBuf;

use cwr_kernel::gateway::{AdapterGateway, GatewayConfig};
use cwr_kernel::{canonical_json, run_turn, KernelError, TurnOptions};

const USAGE: &str = "usage: cwr instance turn <id> [--script <file> | --message <file> | < script.py] [--debug-effects] [--home <dir>] [--builds <dir>] [--diagnostic-no-language-guard] [--gateway live|record|replay] [--cassette <file>] [--cassette-label <label>] [--stub NAME=URL]...";

fn usage_error(m: &str) -> i32 {
    eprintln!("cwr instance turn: {m}");
    eprintln!("{USAGE}");
    1
}

fn fail(e: KernelError) -> i32 {
    eprintln!("cwr instance turn: {e}");
    e.exit_code()
}

/// `args` are the arguments after `turn`.
pub fn run(args: &[String]) -> i32 {
    let mut home = std::env::var_os("CWR_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(".cwr"));
    let mut script_path: Option<String> = None;
    // Task 3.1: the input is a raw model message that must pass the turn protocol.
    let mut is_message = false;
    let mut id: Option<String> = None;
    let mut opts = TurnOptions::default();
    let mut verbose = false;
    // Task 2.3: adapter gateway mode, cassette and stub bindings (spec/cwr/kernel.md section 7).
    let mut gw = GatewayConfig::default();
    let mut it = args.iter();
    while let Some(a) = it.next() {
        match a.as_str() {
            "--home" => match it.next() {
                Some(v) => home = PathBuf::from(v),
                None => return usage_error("--home needs a directory"),
            },
            "--builds" => {
                if it.next().is_none() {
                    return usage_error("--builds needs a directory");
                }
            }
            "--script" => match it.next() {
                Some(v) if script_path.is_none() => script_path = Some(v.clone()),
                Some(_) => return usage_error("--script given twice"),
                None => return usage_error("--script needs a file"),
            },
            "--message" => match it.next() {
                Some(v) if script_path.is_none() => {
                    script_path = Some(v.clone());
                    is_message = true;
                }
                Some(_) => return usage_error("give one of --script and --message, once"),
                None => return usage_error("--message needs a file"),
            },
            "--debug-effects" => opts.debug_effects = true,
            "--diagnostic-no-language-guard" => opts.language_guard = false,
            "--verbose" => verbose = true,
            "--gateway" => match it.next().map(|v| v.parse()) {
                Some(Ok(m)) => gw.mode = m,
                Some(Err(e)) => return usage_error(&e),
                None => return usage_error("--gateway needs live, record or replay"),
            },
            "--cassette" => match it.next() {
                Some(v) => gw.cassette = Some(PathBuf::from(v)),
                None => return usage_error("--cassette needs a file"),
            },
            "--cassette-label" => match it.next() {
                Some(v) => gw.label = Some(v.clone()),
                None => return usage_error("--cassette-label needs a label"),
            },
            "--stub" => match it.next().map(|v| gw.add_stub(v)) {
                Some(Ok(())) => {}
                Some(Err(e)) => return usage_error(&e),
                None => return usage_error("--stub needs NAME=URL"),
            },
            o if o.starts_with("--") => return usage_error(&format!("unknown option {o}")),
            _ if id.is_none() => id = Some(a.clone()),
            _ => return usage_error("turn takes exactly one <id>"),
        }
    }
    let Some(id) = id else {
        return usage_error("turn needs an <id>");
    };
    let script = match script_path.as_deref() {
        None | Some("-") => {
            let mut s = String::new();
            if let Err(e) = std::io::stdin().read_to_string(&mut s) {
                return usage_error(&format!("cannot read the script from stdin: {e}"));
            }
            s
        }
        Some(p) => match std::fs::read(p) {
            Ok(b) => match String::from_utf8(b) {
                Ok(s) => s,
                Err(_) => return usage_error(&format!("{p} is not UTF-8")),
            },
            Err(e) => return usage_error(&format!("cannot read {p}: {e}")),
        },
    };
    let (script, violation) = if is_message {
        match cwr_kernel::surface::protocol::extract_script(&script) {
            Ok(s) => (s, None),
            Err(v) => (String::new(), Some(v)),
        }
    } else {
        (script, None)
    };
    // Task 2.4: an Instance a crashed kernel left mid-turn is recovered on this open.
    let mut inst = match cwr_kernel::recovery::open_recovering(&home, &id) {
        Ok((i, rep)) => {
            if let Some(r) = rep {
                eprintln!(
                    "cwr instance turn: recovered {}",
                    canonical_json(&r.to_json())
                );
            }
            i
        }
        Err(e) => return fail(e),
    };
    if let Some(v) = violation {
        return match cwr_kernel::surface::protocol::refuse(&mut inst, &v) {
            Ok(env) => {
                println!("{}", canonical_json(&env));
                4
            }
            Err(e) => fail(e),
        };
    }
    eprintln!("cwr instance turn: {}", cwr_kernel::sandbox::describe());
    let gateway = match AdapterGateway::new(gw, &inst) {
        Ok(g) => g,
        Err(e) => return fail(e),
    };
    // Task 2.4: the script is recorded for replay and external calls are journalled for recovery.
    if let Err(e) = cwr_kernel::replay::record_script(&inst, &script) {
        return fail(e);
    }
    let mut gateway = cwr_kernel::recovery::Journaled::new(gateway, &inst);
    match run_turn(&mut inst, &script, &opts, &mut gateway) {
        Ok(report) => {
            if verbose {
                eprintln!(
                    "cwr instance turn: ended {} peak_rss_kb={}",
                    report.ended, report.peak_rss_kb
                );
                if !report.sandbox_stderr.is_empty() {
                    eprintln!(
                        "cwr instance turn: sandbox stderr:\n{}",
                        report.sandbox_stderr
                    );
                }
            }
            println!("{}", canonical_json(&report.envelope));
            0
        }
        Err(e) => fail(e),
    }
}

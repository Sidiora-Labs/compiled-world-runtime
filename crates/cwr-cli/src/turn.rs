// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! `cwr instance turn`: run one sandboxed turn script on an Instance (task 2.2; spec/cwr/kernel.md
//! sections 2 to 5, model-surface.md section 4).
//!
//! ```text
//! cwr instance turn <id> [--script <file> | --message <file> | < input] [--debug-effects]
//!                        [--home <dir>] [--builds <dir>]... [--diagnostic-no-language-guard]
//! ```
//!
//! Prints the result envelope `{status, returned, error, trace, world, resources}` as one line of
//! canonical JSON on stdout and exits 0 for `ok`, `error` and `partial` alike. On stderr it prints
//! one line naming the sandbox mechanism in use. Exit codes: 1 usage or any kernel error (a sealed
//! Instance, or a sandbox that cannot be provided; the kernel never runs a script unsandboxed),
//! 2 Build hash mismatch, 3 experimental Build, 4 protocol refusal, 8 `CONFLICT`.
//!
//! `--message <file>` (task 3.1, model-surface.md section 3) reads a raw model message instead of a
//! script: the turn protocol extracts its one fenced python block, and a message with zero or more
//! than one fenced block, a non-python block or an unclosed fence is refused as `INVALID` naming
//! `turn.protocol` without executing anything or advancing world time; the refusal envelope is
//! printed and the exit code is 4. Standard input that holds a code fence line (at most three
//! spaces, then three or more backticks or tildes), before or after prose, is a model message too and goes through the same protocol (kernel.md section 2: "exactly one
//! fenced python block or raw script on stdin"); any other standard input, and `--script -`, is a
//! raw script. A refused message leaves a refusal marker next to the turn scripts (the rule broken
//! and its fence lines, never the message text), so that replay reproduces the refusal.
//!
//! A turn requested while another operation holds the Instance's lock, or while the Instance is
//! not `idle`, is refused before anything executes: the refusal envelope `{status: error, error:
//! {code: CONFLICT, primitive: kernel.instance}}` is printed on stdout, nothing is recorded and the
//! exit code is 8.
//!
//! `--builds <dir>` (repeatable) names the Build roots the operator expects the Instance's Build
//! in: when given, the turn is refused (exit 1) unless the Build the Instance is bound to is the
//! directory its hash resolves to in one of those roots.
//!
//! `--debug-effects` turns on step 4 of the atomic contract check (every declared effect is
//! asserted after an ok mutating call). `--diagnostic-no-language-guard` switches off only the
//! in-interpreter layer of the sandbox so that the kernel-level layer (namespaces, read-only view,
//! rlimits) can be demonstrated on its own; it is an operator diagnostic documented in
//! crates/cwr-kernel/SANDBOX.md and never used for a model turn.

use std::io::Read;
use std::path::{Path, PathBuf};

use cwr_kernel::executor::conflict_envelope;
use cwr_kernel::gateway::{AdapterGateway, GatewayConfig};
use cwr_kernel::{canonical_json, run_turn, Instance, KernelError, TurnOptions};

use crate::cli::{self, Spec};

const SPEC: Spec = Spec {
    values: &[
        "--home",
        "--script",
        "--message",
        "--gateway",
        "--cassette",
        "--cassette-label",
    ],
    repeatable: &["--builds", "--stub"],
    flags: &[
        "--debug-effects",
        "--diagnostic-no-language-guard",
        "--verbose",
    ],
};

const USAGE: &str = "usage: cwr instance turn <id> [--script <file> | --message <file> | < input] [--debug-effects] [--home <dir>] [--builds <dir>]... [--diagnostic-no-language-guard] [--gateway live|record|replay] [--cassette <file>] [--cassette-label <label>] [--stub NAME=URL]...";

fn usage_error(m: &str) -> i32 {
    eprintln!("cwr instance turn: {m}");
    eprintln!("{USAGE}");
    1
}

fn fail(e: KernelError) -> i32 {
    eprintln!("cwr instance turn: {e}");
    e.exit_code()
}

/// Reports a kernel error of the turn path: a `CONFLICT` also prints its refusal envelope on
/// stdout, so a harness reads the refusal like any other turn result.
fn fail_turn(home: &Path, id: &str, e: KernelError) -> i32 {
    if let KernelError::Conflict(detail) = &e {
        let meta = Instance::read_meta(home, id).ok();
        let written = cli::emit_line(&canonical_json(&conflict_envelope(meta.as_ref(), detail)));
        if written != 0 {
            eprintln!("cwr instance turn: {e}");
            return written;
        }
    }
    fail(e)
}

/// Whether standard input is a model message rather than a raw script: any of its lines is a code
/// fence line (at most three spaces, then three or more backticks or tildes), wherever it stands,
/// so a message that opens with prose still goes through the turn protocol.
fn looks_like_message(input: &str) -> bool {
    input.lines().any(|l| {
        let indent = l.len() - l.trim_start_matches(' ').len();
        let rest = &l[indent..];
        indent <= 3 && (rest.starts_with("```") || rest.starts_with("~~~"))
    })
}

/// `args` are the arguments after `turn`.
pub fn run(args: &[String]) -> i32 {
    let p = match cli::parse(args, &SPEC) {
        Ok(p) => p,
        Err(m) => return usage_error(&m),
    };
    let home = p.home();
    let id = match p.positional.as_slice() {
        [id] => id.clone(),
        [] => return usage_error("turn needs an <id>"),
        _ => return usage_error("turn takes exactly one <id>"),
    };
    if p.given("--script") && p.given("--message") {
        return usage_error("give one of --script and --message, once");
    }
    // Task 3.1: the input is a raw model message that must pass the turn protocol.
    let mut is_message = p.given("--message");
    let script_path = p.value("--script").or(p.value("--message"));
    let opts = TurnOptions {
        debug_effects: p.flag("--debug-effects"),
        language_guard: !p.flag("--diagnostic-no-language-guard"),
    };
    let verbose = p.flag("--verbose");
    // Task 2.3: adapter gateway mode, cassette and stub bindings (spec/cwr/kernel.md section 7).
    let mut gw = GatewayConfig::default();
    if let Some(m) = p.value("--gateway") {
        match m.parse() {
            Ok(m) => gw.mode = m,
            Err(e) => return usage_error(&e),
        }
    }
    gw.cassette = p.value("--cassette").map(PathBuf::from);
    gw.label = p.value("--cassette-label").map(str::to_string);
    for stub in p.values("--stub") {
        if let Err(e) = gw.add_stub(stub) {
            return usage_error(&e);
        }
    }
    let roots: Vec<PathBuf> = p
        .values("--builds")
        .into_iter()
        .map(PathBuf::from)
        .collect();
    let script = match script_path {
        None | Some("-") => {
            let mut s = String::new();
            if let Err(e) = std::io::stdin().read_to_string(&mut s) {
                return usage_error(&format!("cannot read the script from stdin: {e}"));
            }
            // An explicit `--script -` is a raw script; bare stdin is a message when it is fenced.
            if script_path.is_none() && looks_like_message(&s) {
                is_message = true;
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
        Err(e) => return fail_turn(&home, &id, e),
    };
    if !roots.is_empty() {
        let hex = inst.build.hash.trim_start_matches("sha256:");
        let under_root = roots
            .iter()
            .any(|r| std::fs::canonicalize(r.join(hex)).is_ok_and(|d| d == inst.build.dir));
        if !under_root {
            return fail(KernelError::Refused(format!(
                "the Instance's Build {} ({}) is not under any --builds root ({})",
                inst.build.hash,
                inst.build.dir.display(),
                roots
                    .iter()
                    .map(|r| r.display().to_string())
                    .collect::<Vec<_>>()
                    .join(", ")
            )));
        }
    }
    if let Some(v) = violation {
        if let Err(e) = cwr_kernel::replay::record_refusal(&inst, &v) {
            return fail(e);
        }
        return match cwr_kernel::surface::protocol::refuse(&mut inst, &v) {
            Ok(env) => match cli::emit_line(&canonical_json(&env)) {
                0 => 4,
                lost => lost,
            },
            Err(e) => fail_turn(&home, &id, e),
        };
    }
    eprintln!(
        "cwr instance turn: {}",
        cwr_kernel::sandbox::describe(opts.language_guard)
    );
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
            cli::emit_line(&canonical_json(&report.envelope))
        }
        Err(e) => fail_turn(&home, &id, e),
    }
}

#[cfg(test)]
mod tests {
    use super::{looks_like_message, run};

    fn args(v: &[&str]) -> Vec<String> {
        v.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn usage_errors_exit_one_before_anything_runs() {
        for bad in [
            &[][..],
            &["a", "b"],
            &["a", "--script", "x", "--message", "y"],
            &["a", "--script", "x", "--script", "y"],
            &["a", "--home", "h1", "--home", "h2"],
            &["a", "--cassette", "c1", "--cassette", "c2"],
            &["a", "--cassette-label", "l1", "--cassette-label", "l2"],
            &["a", "--gateway", "live", "--gateway", "replay"],
            &["a", "--gateway", "sideways"],
            &["a", "--stub", "no-equals-sign"],
            &["a", "--debug-effects", "--debug-effects"],
            &["a", "--nope"],
            &["a", "--home"],
        ] {
            assert_eq!(run(&args(bad)), 1, "{bad:?}");
        }
    }

    #[test]
    fn fenced_stdin_is_a_message() {
        assert!(looks_like_message("```python\nreturn 1\n```\n"));
        assert!(looks_like_message("\n  ~~~python\nx = 1\n~~~\n"));
        assert!(!looks_like_message("import fs\nreturn 1\n"));
        assert!(!looks_like_message("    ```python\n"));
        assert!(looks_like_message("s = 1\n```\n"));
        assert!(looks_like_message(
            "I will read the clock.\n\n```python\nreturn 1\n```\n"
        ));
    }
}

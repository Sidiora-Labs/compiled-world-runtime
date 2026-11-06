// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! `cwr instance context`: the model context for the next turn of an Instance (task 3.1;
//! spec/cwr/model-surface.md section 1, clauses 9.1 and 9.5).
//!
//! ```text
//! cwr instance context <id> [--tail K|full] [--json] [--home <dir>]
//! ```
//!
//! Prints the context exactly as the model receives it: the Build's manual byte for byte, the
//! recompile notice when one is pending, the ledger tail of K records (K from the Instance header
//! unless `--tail` overrides it for this rendering) and the last result envelope, joined by the
//! fixed separators of `cwr_kernel::surface::context`. Nothing is written; repeating the command
//! prints the same bytes. `--json` prints the operator view instead: the offset, length and sha256
//! of every component and of the whole context. Exit codes: 0 success; 1 usage, an Instance that
//! is not idle or any other error; 2 Build hash mismatch.

use cwr_kernel::surface::context::assemble;
use cwr_kernel::{Instance, KernelError, TailSize};

use crate::cli::{self, Spec};

const SPEC: Spec = Spec {
    values: &["--home", "--tail"],
    repeatable: &[],
    flags: &["--json"],
};

const USAGE: &str = "usage: cwr instance context <id> [--tail K|full] [--json] [--home <dir>]";

fn usage_error(m: &str) -> i32 {
    eprintln!("cwr instance context: {m}");
    eprintln!("{USAGE}");
    1
}

fn fail(e: KernelError) -> i32 {
    eprintln!("cwr instance context: {e}");
    e.exit_code()
}

/// Runs `cwr instance context`; `args` follow `context`. Returns the process exit code.
pub fn run(args: &[String]) -> i32 {
    let p = match cli::parse(args, &SPEC) {
        Ok(p) => p,
        Err(m) => return usage_error(&m),
    };
    let home = p.home();
    let id = match p.positional.as_slice() {
        [id] => id.clone(),
        [] => return usage_error("context needs an <id>"),
        _ => return usage_error("context takes exactly one <id>"),
    };
    let tail = match p.value("--tail").map(str::parse::<TailSize>) {
        None => None,
        Some(Ok(k)) => Some(k),
        Some(Err(e)) => return usage_error(&e.to_string()),
    };
    let as_json = p.flag("--json");
    let inst = match Instance::open(&home, &id) {
        Ok(i) => i,
        Err(e) => return fail(e),
    };
    if inst.meta.status != "idle" {
        return fail(KernelError::State(format!(
            "instance {id} is {}, not idle; a context is composed only between turns",
            inst.meta.status
        )));
    }
    let ctx = match assemble(&inst, tail) {
        Ok(c) => c,
        Err(e) => return fail(e),
    };
    if as_json {
        cli::emit_line(&ctx.to_json().to_string())
    } else {
        cli::emit(&ctx.render())
    }
}

#[cfg(test)]
mod tests {
    use super::run;

    fn args(v: &[&str]) -> Vec<String> {
        v.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn usage_errors_exit_one() {
        assert_eq!(run(&args(&[])), 1);
        assert_eq!(run(&args(&["a", "b"])), 1);
        assert_eq!(run(&args(&["a", "--tail", "3", "--tail", "4"])), 1);
        assert_eq!(run(&args(&["a", "--json", "--json"])), 1);
        assert_eq!(run(&args(&["a", "--tail", "zero"])), 1);
        assert_eq!(run(&args(&["a", "--nope"])), 1);
    }
}

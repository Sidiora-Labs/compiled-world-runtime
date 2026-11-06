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

use std::io::Write;
use std::path::PathBuf;

use cwr_kernel::surface::context::assemble;
use cwr_kernel::{Instance, KernelError, TailSize};

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
    let mut home = std::env::var_os("CWR_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(".cwr"));
    let mut id: Option<String> = None;
    let mut tail: Option<TailSize> = None;
    let mut as_json = false;
    let mut it = args.iter();
    while let Some(a) = it.next() {
        match a.as_str() {
            "--home" => match it.next() {
                Some(v) => home = PathBuf::from(v),
                None => return usage_error("--home needs a directory"),
            },
            "--tail" => match it.next().map(|v| v.parse::<TailSize>()) {
                Some(Ok(k)) if tail.is_none() => tail = Some(k),
                Some(Ok(_)) => return usage_error("--tail given twice"),
                Some(Err(e)) => return usage_error(&e.to_string()),
                None => return usage_error("--tail needs a positive integer or full"),
            },
            "--json" => as_json = true,
            o if o.starts_with("--") => return usage_error(&format!("unknown option {o}")),
            _ if id.is_none() => id = Some(a.clone()),
            _ => return usage_error("context takes exactly one <id>"),
        }
    }
    let Some(id) = id else {
        return usage_error("context needs an <id>");
    };
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
    let mut out = std::io::stdout().lock();
    let written = if as_json {
        writeln!(out, "{}", ctx.to_json())
    } else {
        out.write_all(ctx.render().as_bytes())
    };
    match written.and_then(|()| out.flush()) {
        Ok(()) => 0,
        Err(e) => {
            eprintln!("cwr instance context: cannot write the context: {e}");
            1
        }
    }
}

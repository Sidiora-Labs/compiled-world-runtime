// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! `cwr instance upgrade`, `cwr instance notice` and `cwr instance outbox` (task 2.5;
//! spec/cwr/kernel.md sections 2, 10 and 11; model-surface.md section 5).
//!
//! ```text
//! cwr instance upgrade <id> (<build-hash|dir> | --build <dir>) [--allow-experimental]
//!                                     upgrades the Instance to the target Build; prints one JSON
//!                                     line {"status", "record", "notice"}; exit 0, 5 refused Build
//!                                     (nothing changed), 6 migration or commit failure after
//!                                     rollback, or the report of an earlier interrupted upgrade
//!                                     that was rolled back
//! cwr instance notice <id>            prints context component 2 for the next turn: every pending
//!                                     recompile notice rendered under the bound Build (nothing when
//!                                     none is pending or the Build renders it silently)
//! cwr instance outbox <id>            prints the proposal outbox, one JSON line per proposal; this
//!                                     is the owner's read path, outside the world
//! ```
//!
//! Global options: `--home <dir>` (default `$CWR_HOME`, else `.cwr`); `--builds <dir>`
//! (repeatable; default `build` and `xbuild`) is where a Build hash resolves. Exit 1 on usage and
//! any other error.

use std::path::PathBuf;

use cwr_kernel::notice;
use cwr_kernel::outbox::OUTBOX_FILE;
use cwr_kernel::upgrade::{self, UpgradeError, UpgradeOptions};
use cwr_kernel::{Instance, KernelError};
use serde_json::json;

const USAGE: &str = "usage:
  cwr instance upgrade <id> (<build-hash|dir> | --build <dir>) [--allow-experimental]
  cwr instance notice <id>
  cwr instance outbox <id>
global options: --home <dir>, --builds <dir>";

fn usage_error(cmd: &str, m: &str) -> i32 {
    eprintln!("cwr instance {cmd}: {m}");
    eprintln!("{USAGE}");
    1
}

fn fail(cmd: &str, e: KernelError) -> i32 {
    eprintln!("cwr instance {cmd}: {e}");
    e.exit_code()
}

struct Args {
    home: PathBuf,
    builds: Vec<PathBuf>,
    build: Option<String>,
    allow_experimental: bool,
    positional: Vec<String>,
}

fn parse(cmd: &str, args: &[String]) -> Result<Args, String> {
    let mut a = Args {
        home: std::env::var_os("CWR_HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from(".cwr")),
        builds: Vec::new(),
        build: None,
        allow_experimental: false,
        positional: Vec::new(),
    };
    let mut it = args.iter();
    while let Some(x) = it.next() {
        match x.as_str() {
            "--home" => a.home = PathBuf::from(it.next().ok_or("--home needs a directory")?),
            "--builds" => a.builds.push(PathBuf::from(
                it.next().ok_or("--builds needs a directory")?,
            )),
            "--build" if cmd == "upgrade" => {
                if a.build.is_some() {
                    return Err("--build given twice".into());
                }
                a.build = Some(it.next().ok_or("--build needs a directory")?.clone());
            }
            "--allow-experimental" if cmd == "upgrade" => a.allow_experimental = true,
            o if o.starts_with("--") => return Err(format!("unknown option {o}")),
            _ => a.positional.push(x.clone()),
        }
    }
    if a.builds.is_empty() {
        a.builds = vec![PathBuf::from("build"), PathBuf::from("xbuild")];
    }
    Ok(a)
}

fn run_upgrade(a: &Args) -> i32 {
    let (id, reference) =
        match (a.positional.as_slice(), &a.build) {
            ([id], Some(b)) => (id, b),
            ([id, b], None) => (id, b),
            _ => return usage_error(
                "upgrade",
                "upgrade takes an <id> and exactly one Build: <build-hash|dir> or --build <dir>",
            ),
        };
    let target = match upgrade::resolve_target(reference, &a.builds) {
        Ok(t) => t,
        Err(e) => {
            eprintln!("cwr instance upgrade: {e}");
            return e.exit_code();
        }
    };
    let opts = UpgradeOptions {
        allow_experimental: a.allow_experimental,
    };
    match upgrade::upgrade(&a.home, id, &target, opts) {
        Ok(rep) => {
            let text = notice::render(&rep.notice, notice::Rendering::Full);
            let mut out = json!({"status": "upgraded", "record": rep.record, "snapshot": rep.snapshot, "notice": text});
            if let Some(r) = rep.resolved {
                let earlier = UpgradeError::Interrupted(r).to_string();
                eprintln!("cwr instance upgrade: {earlier}");
                out["completed_interrupted"] = json!(earlier);
            }
            println!("{out}");
            0
        }
        Err(e @ UpgradeError::Interrupted(_)) => {
            eprintln!("cwr instance upgrade: {e}");
            println!(
                "{}",
                json!({"status": if e.exit_code() == 0 { "completed_interrupted" } else { "rolled_back_interrupted" }, "detail": e.to_string()})
            );
            e.exit_code()
        }
        Err(e) => {
            eprintln!("cwr instance upgrade: {e}");
            e.exit_code()
        }
    }
}

/// Runs `cwr instance upgrade|notice|outbox`; `args` follow the command name.
pub fn run(cmd: &str, args: &[String]) -> i32 {
    let a = match parse(cmd, args) {
        Ok(a) => a,
        Err(m) => return usage_error(cmd, &m),
    };
    match cmd {
        "upgrade" => run_upgrade(&a),
        "notice" | "outbox" if a.positional.len() == 1 => {
            let id = &a.positional[0];
            if !Instance::dir_of(&a.home, id).is_dir() {
                return fail(cmd, KernelError::Refused(format!("no instance {id}")));
            }
            if cmd == "notice" {
                match Instance::open(&a.home, id).and_then(|i| notice::component(&i)) {
                    Ok(Some(text)) => {
                        println!("{text}");
                        0
                    }
                    Ok(None) => 0,
                    Err(e) => fail(cmd, e),
                }
            } else {
                let path = Instance::dir_of(&a.home, id).join(OUTBOX_FILE);
                match std::fs::read_to_string(&path) {
                    Ok(text) => {
                        print!("{text}");
                        0
                    }
                    Err(e) if e.kind() == std::io::ErrorKind::NotFound => 0,
                    Err(e) => {
                        eprintln!("cwr instance outbox: {}: {e}", path.display());
                        1
                    }
                }
            }
        }
        _ => usage_error(cmd, &format!("wrong arguments for {cmd}")),
    }
}

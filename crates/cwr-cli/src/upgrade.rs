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

use crate::cli::{self, Spec};

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

const UPGRADE_SPEC: Spec = Spec {
    values: &["--home", "--build"],
    repeatable: &["--builds"],
    flags: &["--allow-experimental"],
};
const READ_SPEC: Spec = Spec {
    values: &["--home"],
    repeatable: &["--builds"],
    flags: &[],
};

/// Parses the arguments of `cmd` (`upgrade`, `notice` or `outbox`); the error is the usage message.
fn parse(cmd: &str, args: &[String]) -> Result<Args, String> {
    let p = cli::parse(
        args,
        if cmd == "upgrade" {
            &UPGRADE_SPEC
        } else {
            &READ_SPEC
        },
    )?;
    let mut builds: Vec<PathBuf> = p
        .values("--builds")
        .into_iter()
        .map(PathBuf::from)
        .collect();
    if builds.is_empty() {
        builds = vec![PathBuf::from("build"), PathBuf::from("xbuild")];
    }
    Ok(Args {
        home: p.home(),
        builds,
        build: p.value("--build").map(str::to_string),
        allow_experimental: p.flag("--allow-experimental"),
        positional: p.positional.clone(),
    })
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
            cli::emit_line(&out.to_string())
        }
        Err(e @ UpgradeError::Interrupted(_)) => {
            eprintln!("cwr instance upgrade: {e}");
            let written = cli::emit_line(
                &json!({"status": if e.exit_code() == 0 { "completed_interrupted" } else { "rolled_back_interrupted" }, "detail": e.to_string()})
                    .to_string(),
            );
            if written != 0 {
                return written;
            }
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
            let dir = match Instance::checked_dir(&a.home, id) {
                Ok(d) => d,
                Err(e) => return fail(cmd, e),
            };
            if !dir.is_dir() {
                return fail(cmd, KernelError::Refused(format!("no instance {id}")));
            }
            if cmd == "notice" {
                match Instance::open(&a.home, id).and_then(|i| notice::component(&i)) {
                    Ok(Some(text)) => cli::emit_line(&text),
                    Ok(None) => 0,
                    Err(e) => fail(cmd, e),
                }
            } else {
                let path = dir.join(OUTBOX_FILE);
                match std::fs::read_to_string(&path) {
                    Ok(text) => cli::emit(&text),
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

#[cfg(test)]
mod tests {
    use super::*;

    fn args(v: &[&str]) -> Vec<String> {
        v.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn upgrade_options_parse_and_builds_repeat() {
        let a = parse(
            "upgrade",
            &args(&[
                "i",
                "--build",
                "b",
                "--builds",
                "r1",
                "--builds",
                "r2",
                "--allow-experimental",
            ]),
        )
        .unwrap();
        assert_eq!(a.positional, args(&["i"]));
        assert_eq!(a.build.as_deref(), Some("b"));
        assert_eq!(a.builds, vec![PathBuf::from("r1"), PathBuf::from("r2")]);
        assert!(a.allow_experimental);
        let d = parse("notice", &args(&["i"])).unwrap();
        assert_eq!(
            d.builds,
            vec![PathBuf::from("build"), PathBuf::from("xbuild")]
        );
    }

    #[test]
    fn repeats_and_foreign_options_are_usage_errors() {
        assert!(parse("upgrade", &args(&["i", "--build", "a", "--build", "b"])).is_err());
        assert!(parse("upgrade", &args(&["i", "--home", "a", "--home", "b"])).is_err());
        assert!(parse(
            "upgrade",
            &args(&["i", "--allow-experimental", "--allow-experimental"])
        )
        .is_err());
        assert!(parse("notice", &args(&["i", "--build", "b"])).is_err());
        assert!(parse("outbox", &args(&["i", "--allow-experimental"])).is_err());
        assert_eq!(run("notice", &args(&["a", "b"])), 1);
        assert_eq!(run("upgrade", &args(&["only-id"])), 1);
    }
}

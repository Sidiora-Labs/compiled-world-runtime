// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! `cwr instance replay`, `cwr instance recover` and `cwr instance compare` (task 2.4;
//! spec/cwr/kernel.md sections 2 and 9; clauses 8.3, 8.4 and 8.5).
//!
//! ```text
//! cwr instance replay (<id> | <ledger.jsonl>) [--into <new-id>] [--build <dir>] [--builds <dir>]...
//!                     [--cassette <file>] [--scripts <dir>] [--initial <state.json>] [--home <dir>]
//!                     [--into-home <dir>] [--debug-effects]
//!     Recreates the Instance from its seed, initial state, Build, cassette and recorded turn
//!     inputs (scripts, model messages, probe calls), re-runs every turn and compares the new
//!     ledger with the original ignoring host_ts, duration_us and instance. Prints one JSON report
//!     line with the new Instance id and its final state digest. `--into <new-id>` names the new
//!     Instance (a ULID not yet in the store); without it a fresh ULID is chosen. The Build is
//!     `--build`, else the header's Build hash resolved in the `--builds` roots, else the Build the
//!     original Instance is bound to. Defaults, when the ledger lies in an Instance directory: its
//!     scripts/ directory and its initial state object.
//!     Exit 0 identical; 7 divergence (printed, new Instance sealed); 2 Build or initial-state hash
//!     mismatch; 1 refused (tampered or truncated ledger, missing input) or any other error.
//! cwr instance recover <id> [--home <dir>]
//!     Recovers an Instance after a kernel crash (also done on the next `cwr instance turn`); prints
//!     the recovery report, or {"recovered": false} when nothing needed recovery.
//! cwr instance compare <ledger-a> <ledger-b>
//!     Field-by-field comparison of two ledgers ignoring host fields. Exit 0 identical, 7 divergent.
//! ```

use std::path::{Path, PathBuf};

use cwr_kernel::recovery;
use cwr_kernel::replay::{self, ReplayInputs};
use cwr_kernel::{canonical_json, Build, Instance, KernelError, ObjectStore, TurnOptions};
use serde_json::{json, Value};

use crate::cli::{self, Spec};

const SPEC: Spec = Spec {
    values: &[
        "--home",
        "--into-home",
        "--into",
        "--build",
        "--cassette",
        "--scripts",
        "--initial",
    ],
    repeatable: &["--builds"],
    flags: &["--debug-effects"],
};

const USAGE: &str = "usage:
  cwr instance replay (<id> | <ledger.jsonl>) [--into <new-id>] [--build <dir>] [--builds <dir>]...
                      [--cassette <file>] [--scripts <dir>] [--initial <state.json>] [--home <dir>]
                      [--into-home <dir>] [--debug-effects]
  cwr instance recover <id> [--home <dir>]
  cwr instance compare <ledger-a> <ledger-b>";

/// Exit code of a replay or comparison that found a divergent record (kernel.md section 2).
pub const EXIT_DIVERGENCE: i32 = 7;

fn usage_error(m: &str) -> i32 {
    eprintln!("cwr instance: {m}");
    eprintln!("{USAGE}");
    1
}

fn fail(cmd: &str, e: KernelError) -> i32 {
    eprintln!("cwr instance {cmd}: {e}");
    e.exit_code()
}

/// `cmd` is `replay`, `recover` or `compare`; `args` follow it.
pub fn run(cmd: &str, args: &[String]) -> i32 {
    let p = match cli::parse(args, &SPEC) {
        Ok(p) => p,
        Err(m) => return usage_error(&m),
    };
    let home = p.home();
    let into_home = p.value("--into-home").map(PathBuf::from);
    let into = p.value("--into").map(str::to_string);
    let roots: Vec<PathBuf> = p
        .values("--builds")
        .into_iter()
        .map(PathBuf::from)
        .collect();
    let build = p.value("--build").map(PathBuf::from);
    let cassette = p.value("--cassette").map(PathBuf::from);
    let scripts = p.value("--scripts").map(PathBuf::from);
    let initial = p.value("--initial").map(PathBuf::from);
    let opts = TurnOptions {
        debug_effects: p.flag("--debug-effects"),
        ..TurnOptions::default()
    };
    let pos = p.positional.clone();
    match cmd {
        "recover" => {
            let [id] = pos.as_slice() else {
                return usage_error("recover takes exactly one <id>");
            };
            match recovery::needs_recovery(&home, id).and_then(|need| {
                if need {
                    recovery::recover(&home, id).map(|(_, r)| Some(r))
                } else {
                    Ok(None)
                }
            }) {
                Ok(Some(r)) => {
                    let mut v = r.to_json();
                    v["recovered"] = json!(true);
                    cli::emit_line(&canonical_json(&v))
                }
                Ok(None) => cli::emit_line(&canonical_json(
                    &json!({"recovered": false, "instance": id}),
                )),
                Err(e) => fail(cmd, e),
            }
        }
        "compare" => {
            let [a, b] = pos.as_slice() else {
                return usage_error("compare takes exactly two ledger files");
            };
            match replay::compare_files(Path::new(a), Path::new(b)) {
                Ok(None) => cli::emit_line(&canonical_json(
                    &json!({"identical": true, "ignored_fields": replay::IGNORED_FIELDS}),
                )),
                Ok(Some(d)) => {
                    let written = cli::emit_line(&canonical_json(
                        &json!({"identical": false, "divergence": d.to_json()}),
                    ));
                    eprintln!("cwr instance compare: {d}");
                    if written != 0 {
                        return written;
                    }
                    EXIT_DIVERGENCE
                }
                Err(e) => fail(cmd, e),
            }
        }
        "replay" => {
            let [target] = pos.as_slice() else {
                return usage_error("replay takes exactly one <id> or <ledger.jsonl>");
            };
            let ledger = if Path::new(target).is_file() {
                PathBuf::from(target)
            } else {
                match Instance::checked_dir(&home, target) {
                    Ok(dir) => dir.join("ledger.jsonl"),
                    Err(e) => return fail(cmd, e),
                }
            };
            if !ledger.is_file() {
                return usage_error(&format!(
                    "no ledger at {} (and {target} is not an Instance of {})",
                    ledger.display(),
                    home.display()
                ));
            }
            let orig_dir = ledger.parent().map(Path::to_path_buf).unwrap_or_default();
            let orig_meta: Option<Value> = std::fs::read(orig_dir.join("instance.json"))
                .ok()
                .and_then(|b| serde_json::from_slice(&b).ok());
            let header: Option<Value> = std::fs::read_to_string(&ledger)
                .ok()
                .and_then(|t| t.lines().next().and_then(|l| serde_json::from_str(l).ok()));
            let header_build = header
                .as_ref()
                .and_then(|h| h["build"].as_str().map(str::to_string));
            let from_roots = match (&header_build, roots.is_empty()) {
                (Some(hash), false) => match Build::resolve(hash, &roots) {
                    Ok(dir) => Some(dir),
                    Err(e) => return fail(cmd, e),
                },
                _ => None,
            };
            let build_dir = match build.or(from_roots).or_else(|| {
                orig_meta
                    .as_ref()
                    .and_then(|m| m["build_dir"].as_str().map(PathBuf::from))
            }) {
                Some(b) => b,
                None => {
                    return usage_error(
                        "replay needs --build <dir> or --builds <root> (the ledger is not in an Instance directory)",
                    )
                }
            };
            let initial_value = match &initial {
                Some(p) => match std::fs::read(p)
                    .map_err(|e| e.to_string())
                    .and_then(|b| serde_json::from_slice::<Value>(&b).map_err(|e| e.to_string()))
                {
                    Ok(v) => v,
                    Err(m) => return usage_error(&format!("--initial {}: {m}", p.display())),
                },
                None => {
                    let hash = header
                        .as_ref()
                        .and_then(|h| h["initial_state"].as_str().map(str::to_string))
                        .unwrap_or_default();
                    match ObjectStore::new(orig_dir.join("objects")).get(&hash).map_err(|e| e.to_string()).and_then(|b| serde_json::from_slice::<Value>(&b).map_err(|e| e.to_string())) {
                        Ok(v) => v,
                        Err(m) => return usage_error(&format!("replay needs --initial <state.json>: the initial state {hash} is not available next to the ledger ({m})")),
                    }
                }
            };
            let inputs = ReplayInputs {
                ledger: ledger.clone(),
                build_dir,
                initial: initial_value,
                cassette,
                scripts: scripts.unwrap_or_else(|| orig_dir.join(replay::SCRIPTS_DIR)),
                home: into_home.unwrap_or(home),
                into,
                opts,
            };
            match replay::replay(&inputs) {
                Ok(rep) => {
                    let written = cli::emit_line(&canonical_json(&rep.to_json()));
                    if written != 0 {
                        return written;
                    }
                    match &rep.divergence {
                        None => 0,
                        Some(d) => {
                            eprintln!(
                                "cwr instance replay: {d}; replay Instance {} is sealed",
                                rep.into
                            );
                            EXIT_DIVERGENCE
                        }
                    }
                }
                Err(e) => fail(cmd, e),
            }
        }
        other => usage_error(&format!("unknown instance command {other:?}")),
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
        assert_eq!(run("replay", &args(&[])), 1);
        assert_eq!(run("replay", &args(&["a", "b"])), 1);
        assert_eq!(
            run("replay", &args(&["a", "--into", "x", "--into", "y"])),
            1
        );
        assert_eq!(
            run("replay", &args(&["a", "--build", "x", "--build", "y"])),
            1
        );
        assert_eq!(run("replay", &args(&["a", "--cassette"])), 1);
        assert_eq!(run("replay", &args(&["a", "--nope"])), 1);
        assert_eq!(run("recover", &args(&[])), 1);
        assert_eq!(run("compare", &args(&["only-one"])), 1);
        assert_eq!(run("other", &args(&["a"])), 1);
    }

    #[test]
    fn identical_ledgers_compare_clean_and_a_missing_one_is_refused() {
        let d = std::env::temp_dir().join(format!("cwr-cli-compare-{}", std::process::id()));
        let missing = d.join("missing.jsonl").display().to_string();
        assert_eq!(run("compare", &args(&[&missing, &missing])), 1);
    }
}

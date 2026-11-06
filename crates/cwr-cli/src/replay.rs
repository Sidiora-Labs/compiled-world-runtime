// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! `cwr instance replay`, `cwr instance recover` and `cwr instance compare` (task 2.4;
//! spec/cwr/kernel.md sections 2 and 9; clauses 8.3, 8.4 and 8.5).
//!
//! ```text
//! cwr instance replay (<id> | <ledger.jsonl>) [--build <dir>] [--cassette <file>] [--scripts <dir>]
//!                     [--initial <state.json>] [--home <dir>] [--into-home <dir>] [--debug-effects]
//!     Recreates the Instance from its seed, initial state, Build, cassette and recorded turn
//!     scripts, re-runs every turn and compares the new ledger with the original ignoring host_ts,
//!     duration_us and instance. Prints one JSON report line with the new Instance id and its final
//!     state digest. Defaults, when the ledger lies in an Instance directory: the Build that
//!     Instance is bound to, its scripts/ directory and its initial state object.
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
use cwr_kernel::{canonical_json, Instance, KernelError, ObjectStore, TurnOptions};
use serde_json::{json, Value};

const USAGE: &str = "usage:
  cwr instance replay (<id> | <ledger.jsonl>) [--build <dir>] [--cassette <file>] [--scripts <dir>]
                      [--initial <state.json>] [--home <dir>] [--into-home <dir>] [--debug-effects]
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
    let mut home = std::env::var_os("CWR_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(".cwr"));
    let mut into_home: Option<PathBuf> = None;
    let mut build: Option<PathBuf> = None;
    let mut cassette: Option<PathBuf> = None;
    let mut scripts: Option<PathBuf> = None;
    let mut initial: Option<PathBuf> = None;
    let mut opts = TurnOptions::default();
    let mut pos: Vec<String> = Vec::new();
    let mut it = args.iter();
    while let Some(a) = it.next() {
        let mut val = |name: &str| {
            it.next()
                .cloned()
                .ok_or_else(|| format!("{name} needs a value"))
        };
        let r: Result<(), String> = match a.as_str() {
            "--home" => val("--home").map(|v| home = PathBuf::from(v)),
            "--into-home" => val("--into-home").map(|v| into_home = Some(PathBuf::from(v))),
            "--build" => val("--build").map(|v| build = Some(PathBuf::from(v))),
            "--cassette" => val("--cassette").map(|v| cassette = Some(PathBuf::from(v))),
            "--scripts" => val("--scripts").map(|v| scripts = Some(PathBuf::from(v))),
            "--initial" => val("--initial").map(|v| initial = Some(PathBuf::from(v))),
            "--builds" => val("--builds").map(|_| ()),
            "--debug-effects" => {
                opts.debug_effects = true;
                Ok(())
            }
            o if o.starts_with("--") => Err(format!("unknown option {o}")),
            _ => {
                pos.push(a.clone());
                Ok(())
            }
        };
        if let Err(m) = r {
            return usage_error(&m);
        }
    }
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
                    println!("{}", canonical_json(&v));
                    0
                }
                Ok(None) => {
                    println!(
                        "{}",
                        canonical_json(&json!({"recovered": false, "instance": id}))
                    );
                    0
                }
                Err(e) => fail(cmd, e),
            }
        }
        "compare" => {
            let [a, b] = pos.as_slice() else {
                return usage_error("compare takes exactly two ledger files");
            };
            match replay::compare_files(Path::new(a), Path::new(b)) {
                Ok(None) => {
                    println!(
                        "{}",
                        canonical_json(
                            &json!({"identical": true, "ignored_fields": replay::IGNORED_FIELDS})
                        )
                    );
                    0
                }
                Ok(Some(d)) => {
                    println!(
                        "{}",
                        canonical_json(&json!({"identical": false, "divergence": d.to_json()}))
                    );
                    eprintln!("cwr instance compare: {d}");
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
                Instance::dir_of(&home, target).join("ledger.jsonl")
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
            let build_dir =
                match build.or_else(|| {
                    orig_meta
                        .as_ref()
                        .and_then(|m| m["build_dir"].as_str().map(PathBuf::from))
                }) {
                    Some(b) => b,
                    None => return usage_error(
                        "replay needs --build <dir> (the ledger is not in an Instance directory)",
                    ),
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
                opts,
            };
            match replay::replay(&inputs) {
                Ok(rep) => {
                    println!("{}", canonical_json(&rep.to_json()));
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
